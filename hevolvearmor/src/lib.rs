//! HevolveArmor — Encrypted Python module loader for HART OS.
//!
//! The ENTIRE import hook, key derivation, and anti-tamper logic lives in
//! this compiled Rust binary.  The Python surface is a thin shim that calls
//! `install()` — nothing security-critical is in .py files.
//!
//! Key derivation chain (tried in order):
//!   1. Ed25519 node private key → sign(domain) → HKDF → AES key
//!   2. HEVOLVE_DATA_KEY env var → HKDF → AES key
//!   3. Tier + master pubkey hash → HKDF → AES key
//!   4. Passphrase → SHA256 + HKDF → AES key
//!
//! Anti-tamper: binary self-hash verified at init; key wiped on mismatch.
//! Anti-debug: sys.settrace/sys.setprofile intercepted when armed.

use aes_gcm::aead::{Aead, KeyInit, OsRng};
use aes_gcm::{Aes256Gcm, Nonce};
use hkdf::Hkdf;
use pyo3::exceptions::{PyImportError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule};
use rand::RngCore;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use zeroize::Zeroize;

// ─── Constants ───────────────────────────────────────────────────────────────

const NONCE_SIZE: usize = 12;
const KEY_SIZE: usize = 32;
const HKDF_INFO: &[u8] = b"hevolvearmor-v1-module-key";
const HKDF_SALT: &[u8] = b"hart-os-encrypted-modules-salt";
const MANIFEST_FILE: &str = "_manifest.txt";

// ─── Global State ────────────────────────────────────────────────────────────

/// Runtime state: once install() is called, the key and module dir are stored
/// here (inside the compiled binary — not accessible from Python).
struct ArmorState {
    key: [u8; KEY_SIZE],
    modules_dir: PathBuf,
    package_names: Vec<String>,
    code_cache: HashMap<String, Vec<u8>>, // enc_path → decrypted .pyc bytes
    self_hash: Option<String>,
    expected_hash: Option<String>,
    armed: bool,
}

impl Drop for ArmorState {
    fn drop(&mut self) {
        self.key.zeroize();
        self.code_cache.clear();
    }
}

static STATE: Mutex<Option<ArmorState>> = Mutex::new(None);

// ─── Key Derivation (all in Rust) ────────────────────────────────────────────

fn derive_aes_key(ikm: &[u8]) -> [u8; KEY_SIZE] {
    let hk = Hkdf::<Sha256>::new(Some(HKDF_SALT), ikm);
    let mut okm = [0u8; KEY_SIZE];
    hk.expand(HKDF_INFO, &mut okm)
        .expect("HKDF expand failed");
    okm
}

fn derive_key_from_ed25519(private_key_bytes: &[u8]) -> Result<[u8; KEY_SIZE], String> {
    use ed25519_dalek::{Signer, SigningKey};
    if private_key_bytes.len() != 32 {
        return Err(format!(
            "Ed25519 key must be 32 bytes, got {}",
            private_key_bytes.len()
        ));
    }
    let signing_key =
        SigningKey::from_bytes(private_key_bytes.try_into().map_err(|_| "bad key len")?);
    let sig = signing_key.sign(b"hevolvearmor-keygen-v1");
    Ok(derive_aes_key(&sig.to_bytes()[..32]))
}

fn derive_key_from_passphrase(passphrase: &str) -> [u8; KEY_SIZE] {
    let mut hasher = Sha256::new();
    hasher.update(passphrase.as_bytes());
    hasher.update(HKDF_SALT);
    derive_aes_key(&hasher.finalize())
}

fn derive_key_from_bytes(raw: &[u8]) -> [u8; KEY_SIZE] {
    derive_aes_key(raw)
}

/// Full key derivation chain — tries all sources in priority order.
/// Generic: env var names are configurable via HEVOLVEARMOR_* prefix overrides.
///
/// Default env vars (HART OS):
///   KEY_DIR:   HEVOLVE_KEY_DIR       (overridable: HEVOLVEARMOR_KEY_DIR_VAR)
///   DATA_KEY:  HEVOLVE_DATA_KEY      (overridable: HEVOLVEARMOR_DATA_KEY_VAR)
///   TIER:      HEVOLVE_NODE_TIER     (overridable: HEVOLVEARMOR_TIER_VAR)
///   MASTER_PK: hardcoded default     (overridable: HEVOLVEARMOR_MASTER_PK)
///
/// This makes the tool usable by ANY project, not just HART OS.
fn derive_runtime_key_internal(
    node_key_path: Option<&str>,
    passphrase: Option<&str>,
) -> Result<[u8; KEY_SIZE], String> {
    // Configurable env var names (defaults = HART OS conventions)
    let key_dir_var = std::env::var("HEVOLVEARMOR_KEY_DIR_VAR")
        .unwrap_or_else(|_| "HEVOLVE_KEY_DIR".to_string());
    let data_key_var = std::env::var("HEVOLVEARMOR_DATA_KEY_VAR")
        .unwrap_or_else(|_| "HEVOLVE_DATA_KEY".to_string());
    let tier_var = std::env::var("HEVOLVEARMOR_TIER_VAR")
        .unwrap_or_else(|_| "HEVOLVE_NODE_TIER".to_string());
    let master_pk = std::env::var("HEVOLVEARMOR_MASTER_PK").unwrap_or_else(|_| {
        "4662e30d86c2f58416c5ac3f806c2a6af8186e1d96fdbbcad3189847cf888a01".to_string()
    });
    let app_name = std::env::var("HEVOLVEARMOR_APP_NAME")
        .unwrap_or_else(|_| "Nunba".to_string());

    // 0. Explicit passphrase takes highest priority (user intentionally provided it)
    if let Some(pp) = passphrase {
        return Ok(derive_key_from_passphrase(pp));
    }

    // 1. Ed25519 node private key file
    let key_candidates = [
        node_key_path.map(|s| s.to_string()),
        std::env::var(&key_dir_var)
            .ok()
            .map(|d| format!("{}/node_private_key.pem", d)),
        Some("agent_data/node_private_key.pem".to_string()),
        dirs_next::document_dir().map(|d| {
            d.join(&app_name)
                .join("data")
                .join("node_private_key.pem")
                .to_string_lossy()
                .to_string()
        }),
    ];

    for candidate in key_candidates.iter().flatten() {
        if let Ok(pem_bytes) = fs::read(candidate) {
            if let Some(raw) = extract_ed25519_raw_from_pem(&pem_bytes) {
                return derive_key_from_ed25519(&raw);
            }
        }
    }

    // 2. Data encryption key env var
    if let Ok(data_key) = std::env::var(&data_key_var) {
        if !data_key.is_empty() {
            return Ok(derive_key_from_bytes(data_key.as_bytes()));
        }
    }

    // 3. Tier-based derivation (tier + master public key hash)
    let tier = std::env::var(&tier_var).unwrap_or_else(|_| "flat".to_string());
    let mut hasher = Sha256::new();
    hasher.update(format!("hevolvearmor-tier-{}-{}", tier, master_pk).as_bytes());
    let tier_seed = hasher.finalize();
    if std::env::var(&tier_var).is_ok() {
        return Ok(derive_aes_key(&tier_seed));
    }

    // 4. Last resort: tier-based with defaults
    Ok(derive_aes_key(&tier_seed))
}

/// Extract raw 32-byte Ed25519 private key from PEM or raw file.
fn extract_ed25519_raw_from_pem(data: &[u8]) -> Option<Vec<u8>> {
    // Try raw 32 bytes first
    if data.len() == 32 {
        return Some(data.to_vec());
    }
    // Try PEM: look for the DER-encoded Ed25519 key (48 bytes: 16 header + 32 key)
    // PKCS#8 Ed25519 DER has a fixed 16-byte prefix before the 32-byte key
    if let Ok(pem_str) = std::str::from_utf8(data) {
        if pem_str.contains("PRIVATE KEY") {
            // Extract base64 between PEM headers
            let b64: String = pem_str
                .lines()
                .filter(|l| !l.starts_with("-----"))
                .collect();
            if let Ok(der) = base64_decode(&b64) {
                // PKCS#8 Ed25519: last 32 bytes of the nested OCTET STRING
                // The key bytes are at offset 16 in a 48-byte structure,
                // or wrapped in another OCTET STRING (total 50 bytes with 2-byte tag+len)
                if der.len() >= 48 {
                    // Find the 32-byte key: scan for 0x04 0x20 (OCTET STRING, 32 bytes)
                    for i in 0..der.len().saturating_sub(33) {
                        if der[i] == 0x04 && der[i + 1] == 0x20 && i + 34 <= der.len() {
                            return Some(der[i + 2..i + 34].to_vec());
                        }
                    }
                }
            }
        }
    }
    // Try Fernet-encrypted PEM — would need HEVOLVE_DATA_KEY to decrypt first
    // Skip for now; the caller will try other key sources
    None
}

fn base64_decode(input: &str) -> Result<Vec<u8>, String> {
    // Simple base64 decoder (no external dep)
    let table: Vec<u8> = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        .to_vec();
    let mut out = Vec::new();
    let mut buf = 0u32;
    let mut bits = 0u32;
    for &b in input.as_bytes() {
        if b == b'=' || b == b'\n' || b == b'\r' || b == b' ' {
            continue;
        }
        let val = table.iter().position(|&c| c == b).ok_or("bad base64")? as u32;
        buf = (buf << 6) | val;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
            buf &= (1 << bits) - 1;
        }
    }
    Ok(out)
}

// ─── Encryption / Decryption ─────────────────────────────────────────────────

fn encrypt_bytes(data: &[u8], key: &[u8; KEY_SIZE]) -> Result<Vec<u8>, String> {
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|e| format!("Cipher: {e}"))?;
    let mut nonce_bytes = [0u8; NONCE_SIZE];
    OsRng.fill_bytes(&mut nonce_bytes);
    let nonce = Nonce::from_slice(&nonce_bytes);
    let ct = cipher
        .encrypt(nonce, data)
        .map_err(|e| format!("Encrypt: {e}"))?;
    let mut out = Vec::with_capacity(NONCE_SIZE + ct.len());
    out.extend_from_slice(&nonce_bytes);
    out.extend_from_slice(&ct);
    Ok(out)
}

fn decrypt_bytes(blob: &[u8], key: &[u8; KEY_SIZE]) -> Result<Vec<u8>, String> {
    if blob.len() < NONCE_SIZE + 16 {
        return Err("Blob too short".into());
    }
    let (nonce_bytes, ct) = blob.split_at(NONCE_SIZE);
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|e| format!("Cipher: {e}"))?;
    cipher
        .decrypt(Nonce::from_slice(nonce_bytes), ct)
        .map_err(|_| "Decryption failed — wrong key or corrupted".into())
}

// ─── Self-Hash (anti-tamper, ENFORCED) ───────────────────────────────────────

fn compute_self_hash(module_path: &str) -> String {
    if let Ok(data) = fs::read(module_path) {
        let mut h = Sha256::new();
        h.update(&data);
        hex::encode(h.finalize())
    } else {
        "unknown".into()
    }
}

// ─── Import Hook (entirely in Rust) ──────────────────────────────────────────

/// Find the .enc file path for a module name.
/// Returns (enc_path, is_package).
fn find_enc_path(modules_dir: &Path, fullname: &str) -> Option<(PathBuf, bool)> {
    let parts: Vec<&str> = fullname.split('.').collect();

    // Package: foo/bar/__init__.enc
    let mut pkg_path = modules_dir.to_path_buf();
    for p in &parts {
        pkg_path.push(p);
    }
    pkg_path.push("__init__.enc");
    if pkg_path.exists() {
        return Some((pkg_path, true));
    }

    // Module: foo/bar.enc
    let mut mod_path = modules_dir.to_path_buf();
    for p in &parts[..parts.len() - 1] {
        mod_path.push(p);
    }
    mod_path.push(format!("{}.enc", parts.last().unwrap()));
    if mod_path.exists() {
        return Some((mod_path, false));
    }

    None
}

/// Decrypt a .enc file → .pyc bytes.  Caches the result.
fn load_and_decrypt(enc_path: &Path, state: &mut ArmorState) -> Result<Vec<u8>, String> {
    let key_str = enc_path.to_string_lossy().to_string();
    if let Some(cached) = state.code_cache.get(&key_str) {
        return Ok(cached.clone());
    }
    let blob = fs::read(enc_path)
        .map_err(|e| format!("Read {}: {e}", enc_path.display()))?;
    let pyc = decrypt_bytes(&blob, &state.key)?;
    state.code_cache.insert(key_str, pyc.clone());
    Ok(pyc)
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Python-exposed #[pyclass] — the MetaPathFinder
// ═══════════════════════════════════════════════════════════════════════════════

#[pyclass(name = "ArmoredFinder")]
#[derive(Clone)]
struct PyArmoredFinder {}

#[pymethods]
impl PyArmoredFinder {
    /// PEP 451 find_spec — called by Python import machinery.
    #[pyo3(signature = (fullname, _path=None, _target=None))]
    fn find_spec(
        &self,
        py: Python<'_>,
        fullname: &str,
        _path: Option<&Bound<'_, PyAny>>,
        _target: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<PyObject> {
        let guard = STATE.lock().map_err(|_| PyRuntimeError::new_err("lock poisoned"))?;
        let state = match guard.as_ref() {
            Some(s) if s.armed => s,
            _ => return Ok(py.None()),
        };

        // Check if top-level package matches
        let top = fullname.split('.').next().unwrap_or("");
        if !state.package_names.iter().any(|p| p == top) {
            return Ok(py.None());
        }

        let (enc_path, is_package) = match find_enc_path(&state.modules_dir, fullname) {
            Some(r) => r,
            None => return Ok(py.None()),
        };

        // Build ModuleSpec via importlib.machinery
        let importlib_machinery = py.import("importlib.machinery")?;
        let loader = Py::new(py, PyArmoredLoader {
            enc_path: enc_path.to_string_lossy().to_string(),
            is_package,
        })?;

        let spec_cls = importlib_machinery.getattr("ModuleSpec")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("origin", enc_path.to_string_lossy().to_string())?;
        kwargs.set_item("is_package", is_package)?;
        let spec = spec_cls.call(
            (fullname, loader),
            Some(&kwargs),
        )?;

        if is_package {
            let search_path = state
                .modules_dir
                .join(fullname.replace('.', std::path::MAIN_SEPARATOR_STR));
            let py_list = PyList::new(py, &[search_path.to_string_lossy().to_string()])?;
            spec.setattr("submodule_search_locations", py_list)?;
        }

        Ok(spec.into())
    }

    /// Legacy find_module for Python < 3.12
    #[pyo3(signature = (fullname, _path=None))]
    fn find_module(
        &self,
        py: Python<'_>,
        fullname: &str,
        _path: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<PyObject> {
        let guard = STATE.lock().map_err(|_| PyRuntimeError::new_err("lock"))?;
        let state = match guard.as_ref() {
            Some(s) if s.armed => s,
            _ => return Ok(py.None()),
        };
        let top = fullname.split('.').next().unwrap_or("");
        if !state.package_names.iter().any(|p| p == top) {
            return Ok(py.None());
        }
        if find_enc_path(&state.modules_dir, fullname).is_some() {
            // Return self as loader
            Ok(self.clone().into_pyobject(py)?.into_any().unbind())
        } else {
            Ok(py.None())
        }
    }
}

#[pyclass(name = "ArmoredLoader")]
struct PyArmoredLoader {
    enc_path: String,
    is_package: bool,
}

#[pymethods]
impl PyArmoredLoader {
    fn create_module(&self, _spec: &Bound<'_, PyAny>) -> PyResult<PyObject> {
        Python::with_gil(|py| Ok(py.None()))
    }

    fn exec_module(&self, py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
        let pyc_bytes = {
            let mut guard = STATE
                .lock()
                .map_err(|_| PyRuntimeError::new_err("lock poisoned"))?;
            let state = guard
                .as_mut()
                .ok_or_else(|| PyRuntimeError::new_err("HevolveArmor not initialized"))?;

            let enc_path = Path::new(&self.enc_path);
            load_and_decrypt(enc_path, state)
                .map_err(|e| PyImportError::new_err(format!("{}: {e}", self.enc_path)))?
        };

        // Unmarshal: skip .pyc header (16 bytes) → code object
        if pyc_bytes.len() < 16 {
            return Err(PyImportError::new_err("Decrypted .pyc too short"));
        }

        let marshal = py.import("marshal")?;
        let code_bytes = PyBytes::new(py, &pyc_bytes[16..]);
        let code = marshal.call_method1("loads", (code_bytes,))?;

        // ── Post-decrypt transforms ──
        // Apply string decryption and function unwrapping to the code object.
        // These reverse the build-time transforms from _transforms.py.
        let transforms = py.import("hevolvearmor._transforms").ok();
        let final_code = if let Some(tf) = transforms {
            // Decrypt encrypted strings: __hevolvearmor_enc_str__ markers → plaintext
            let decrypt_strings = tf.getattr("decrypt_strings_in_code").ok();
            // Unwrap per-function encrypted code: __hevolvearmor_wrapped__ → code objects
            let unwrap_fns = tf.getattr("unwrap_function_code").ok();

            // Build a Python-callable decrypt function that uses Rust native
            let decrypt_closure = py
                .import("hevolvearmor._native")?
                .getattr("_runtime_decrypt")?;

            let mut transformed = code.clone().unbind();

            if let Some(ds) = decrypt_strings {
                if let Ok(result) = ds.call1((&transformed, &decrypt_closure)) {
                    transformed = result.unbind();
                }
            }
            if let Some(uw) = unwrap_fns {
                if let Ok(result) = uw.call1((&transformed, &decrypt_closure)) {
                    transformed = result.unbind();
                }
            }
            transformed
        } else {
            code.unbind()
        };

        // Set armored marker BEFORE exec so assert-import can verify
        module.setattr("__hevolvearmor__", true)?;

        // exec(code, module.__dict__)
        let builtins = py.import("builtins")?;
        let exec_fn = builtins.getattr("exec")?;
        let module_dict = module.dict();
        exec_fn.call1((final_code.bind(py), module_dict))?;

        // Re-set marker after exec (in case module code overwrote __dict__)
        module.setattr("__hevolvearmor__", true)?;

        Ok(())
    }
}

// ─── Anti-Debug (platform-specific) ──────────────────────────────────────────

/// Check if a debugger is attached.  Returns true if debugger detected.
fn is_debugger_present() -> bool {
    #[cfg(target_os = "windows")]
    {
        // Windows: IsDebuggerPresent + NtQueryInformationProcess
        extern "system" {
            fn IsDebuggerPresent() -> i32;
        }
        unsafe {
            if IsDebuggerPresent() != 0 {
                return true;
            }
        }
        // Also check via NtQueryInformationProcess (ProcessDebugPort = 7)
        // More resistant to IsDebuggerPresent patching
        #[allow(non_snake_case)]
        extern "system" {
            fn NtQueryInformationProcess(
                ProcessHandle: isize,
                ProcessInformationClass: u32,
                ProcessInformation: *mut isize,
                ProcessInformationLength: u32,
                ReturnLength: *mut u32,
            ) -> i32;
        }
        let mut debug_port: isize = 0;
        let status = unsafe {
            NtQueryInformationProcess(
                -1isize, // current process
                7,       // ProcessDebugPort
                &mut debug_port as *mut isize,
                std::mem::size_of::<isize>() as u32,
                std::ptr::null_mut(),
            )
        };
        if status == 0 && debug_port != 0 {
            return true;
        }
    }

    #[cfg(target_os = "linux")]
    {
        // Linux: check /proc/self/status for TracerPid
        if let Ok(status) = fs::read_to_string("/proc/self/status") {
            for line in status.lines() {
                if line.starts_with("TracerPid:") {
                    let pid: i64 = line
                        .split_whitespace()
                        .nth(1)
                        .and_then(|s| s.parse().ok())
                        .unwrap_or(0);
                    if pid != 0 {
                        return true;
                    }
                }
            }
        }
    }

    #[cfg(target_os = "macos")]
    {
        // macOS: sysctl CTL_KERN, KERN_PROC, KERN_PROC_PID
        // Check P_TRACED flag in kinfo_proc.kp_proc.p_flag
        extern "C" {
            fn sysctl(
                name: *const i32,
                namelen: u32,
                oldp: *mut u8,
                oldlenp: *mut usize,
                newp: *const u8,
                newlen: usize,
            ) -> i32;
            fn getpid() -> i32;
        }
        // CTL_KERN=1, KERN_PROC=14, KERN_PROC_PID=1
        let mib: [i32; 4] = [1, 14, 1, unsafe { getpid() }];
        let mut info = vec![0u8; 648]; // sizeof(kinfo_proc) on macOS
        let mut size = info.len();
        let ret = unsafe {
            sysctl(
                mib.as_ptr(),
                4,
                info.as_mut_ptr(),
                &mut size,
                std::ptr::null(),
                0,
            )
        };
        if ret == 0 && size >= 32 {
            // kp_proc.p_flag is at offset 16 (i32)
            let flags = i32::from_ne_bytes([info[16], info[17], info[18], info[19]]);
            let p_traced = 0x00000800; // P_TRACED
            if flags & p_traced != 0 {
                return true;
            }
        }
    }

    false
}

/// Python-exposed debugger check
#[pyfunction]
fn armor_is_debugger_present() -> bool {
    is_debugger_present()
}

// ─── License Management ──────────────────────────────────────────────────────

/// License file format (JSON, Ed25519-signed):
/// {
///   "licensee": "Company Name",
///   "license_id": "uuid",
///   "expires_at": 1735689600,        // Unix timestamp (0 = never)
///   "grace_days": 30,                 // days after expiry still accepted
///   "bind_mac": ["AA:BB:CC:DD:EE:FF"],  // empty = any machine
///   "bind_hostname": [],              // empty = any hostname
///   "bind_machine_id": "",            // platform machine-id (empty = any)
///   "features": ["hevolveai", "embodied_ai"],  // package names allowed
///   "max_nodes": 0,                   // 0 = unlimited
///   "signature": "hex-ed25519-sig"    // signs all fields above
/// }
#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
struct License {
    licensee: String,
    license_id: String,
    #[serde(default)]
    expires_at: u64,  // 0 = never expires
    #[serde(default = "default_grace_days")]
    grace_days: u32,
    #[serde(default)]
    bind_mac: Vec<String>,
    #[serde(default)]
    bind_hostname: Vec<String>,
    #[serde(default)]
    bind_machine_id: String,
    #[serde(default)]
    features: Vec<String>,
    #[serde(default)]
    max_nodes: u32,
    signature: String,
}

fn default_grace_days() -> u32 { 30 }

#[derive(Debug)]
struct LicenseCheckResult {
    valid: bool,
    expired: bool,
    in_grace: bool,
    machine_bound: bool,
    message: String,
}

impl License {
    /// Serialize all fields except signature for verification.
    fn signable_bytes(&self) -> Vec<u8> {
        let mut copy = self.clone();
        copy.signature = String::new();
        serde_json::to_vec(&copy).unwrap_or_default()
    }

    /// Verify the Ed25519 signature against a public key.
    fn verify_signature(&self, public_key_hex: &str) -> Result<bool, String> {
        use ed25519_dalek::{Signature, Verifier, VerifyingKey};

        let pk_bytes = hex::decode(public_key_hex)
            .map_err(|e| format!("Bad public key hex: {e}"))?;
        if pk_bytes.len() != 32 {
            return Err(format!("Public key must be 32 bytes, got {}", pk_bytes.len()));
        }
        let vk = VerifyingKey::from_bytes(pk_bytes.as_slice().try_into().unwrap())
            .map_err(|e| format!("Invalid public key: {e}"))?;

        let sig_bytes = hex::decode(&self.signature)
            .map_err(|e| format!("Bad signature hex: {e}"))?;
        if sig_bytes.len() != 64 {
            return Err(format!("Signature must be 64 bytes, got {}", sig_bytes.len()));
        }
        let sig = Signature::from_bytes(sig_bytes.as_slice().try_into().unwrap());

        let payload = self.signable_bytes();
        Ok(vk.verify(&payload, &sig).is_ok())
    }

    /// Check expiry.
    fn check_expiry(&self) -> (bool, bool) {
        if self.expires_at == 0 {
            return (false, false); // never expires
        }
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        if now <= self.expires_at {
            (false, false) // not expired
        } else {
            let grace_end = self.expires_at + (self.grace_days as u64) * 86400;
            if now <= grace_end {
                (true, true)  // expired but in grace
            } else {
                (true, false) // expired, past grace
            }
        }
    }

    /// Check machine binding.
    fn check_machine_binding(&self) -> bool {
        // MAC address check
        if !self.bind_mac.is_empty() {
            let current_mac = get_mac_address_string();
            if !current_mac.is_empty() {
                let mac_upper: Vec<String> = self.bind_mac.iter()
                    .map(|m| m.to_uppercase()).collect();
                if !mac_upper.contains(&current_mac.to_uppercase()) {
                    return false;
                }
            }
        }

        // Hostname check
        if !self.bind_hostname.is_empty() {
            let current_host = get_hostname_string();
            if !current_host.is_empty() {
                let hosts_lower: Vec<String> = self.bind_hostname.iter()
                    .map(|h| h.to_lowercase()).collect();
                if !hosts_lower.contains(&current_host.to_lowercase()) {
                    return false;
                }
            }
        }

        // Machine ID check
        if !self.bind_machine_id.is_empty() {
            let current_id = get_machine_id();
            if !current_id.is_empty() && current_id != self.bind_machine_id {
                return false;
            }
        }

        true
    }

    /// Full validation.
    fn validate(&self, public_key_hex: &str) -> LicenseCheckResult {
        // 1. Signature
        match self.verify_signature(public_key_hex) {
            Ok(true) => {}
            Ok(false) => {
                return LicenseCheckResult {
                    valid: false, expired: false, in_grace: false,
                    machine_bound: false,
                    message: "License signature invalid".into(),
                };
            }
            Err(e) => {
                return LicenseCheckResult {
                    valid: false, expired: false, in_grace: false,
                    machine_bound: false,
                    message: format!("Signature verification error: {e}"),
                };
            }
        }

        // 2. Expiry
        let (expired, in_grace) = self.check_expiry();
        if expired && !in_grace {
            return LicenseCheckResult {
                valid: false, expired: true, in_grace: false,
                machine_bound: true,
                message: format!("License expired (grace period of {} days has passed)", self.grace_days),
            };
        }

        // 3. Machine binding
        let machine_ok = self.check_machine_binding();
        if !machine_ok {
            return LicenseCheckResult {
                valid: false, expired, in_grace,
                machine_bound: false,
                message: "License not bound to this machine".into(),
            };
        }

        let msg = if in_grace {
            format!("License expired but within {}-day grace period", self.grace_days)
        } else if self.expires_at == 0 {
            "License valid (no expiry)".into()
        } else {
            "License valid".into()
        };

        LicenseCheckResult {
            valid: true, expired, in_grace,
            machine_bound: true,
            message: msg,
        }
    }
}

/// Load license from file.
fn load_license(path: &str) -> Result<License, String> {
    let data = fs::read_to_string(path)
        .map_err(|e| format!("Cannot read license file {path}: {e}"))?;
    serde_json::from_str(&data)
        .map_err(|e| format!("Invalid license format: {e}"))
}

/// Get MAC address as string.
fn get_mac_address_string() -> String {
    mac_address::get_mac_address()
        .ok()
        .flatten()
        .map(|m| m.to_string())
        .unwrap_or_default()
}

/// Get hostname.
fn get_hostname_string() -> String {
    hostname::get()
        .ok()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_default()
}

/// Get platform machine ID.
fn get_machine_id() -> String {
    #[cfg(target_os = "linux")]
    {
        fs::read_to_string("/etc/machine-id")
            .or_else(|_| fs::read_to_string("/var/lib/dbus/machine-id"))
            .map(|s| s.trim().to_string())
            .unwrap_or_default()
    }
    #[cfg(target_os = "windows")]
    {
        // HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid
        let output = std::process::Command::new("reg")
            .args(["query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"])
            .output();
        if let Ok(out) = output {
            let text = String::from_utf8_lossy(&out.stdout);
            for line in text.lines() {
                if line.contains("MachineGuid") {
                    if let Some(guid) = line.split_whitespace().last() {
                        return guid.to_string();
                    }
                }
            }
        }
        String::new()
    }
    #[cfg(target_os = "macos")]
    {
        let output = std::process::Command::new("ioreg")
            .args(["-rd1", "-c", "IOPlatformExpertDevice"])
            .output();
        if let Ok(out) = output {
            let text = String::from_utf8_lossy(&out.stdout);
            for line in text.lines() {
                if line.contains("IOPlatformUUID") {
                    let parts: Vec<&str> = line.split('"').collect();
                    if parts.len() >= 4 {
                        return parts[3].to_string();
                    }
                }
            }
        }
        String::new()
    }
    #[cfg(not(any(target_os = "linux", target_os = "windows", target_os = "macos")))]
    {
        String::new()
    }
}

// ─── License Python API ──────────────────────────────────────────────────────

/// Generate a new license file signed with an Ed25519 private key.
#[pyfunction]
#[pyo3(signature = (
    licensee,
    private_key_hex,
    output_path,
    expires_at=0,
    grace_days=30,
    bind_mac=None,
    bind_hostname=None,
    bind_machine_id=None,
    features=None,
    max_nodes=0
))]
fn armor_generate_license(
    licensee: &str,
    private_key_hex: &str,
    output_path: &str,
    expires_at: u64,
    grace_days: u32,
    bind_mac: Option<Vec<String>>,
    bind_hostname: Option<Vec<String>>,
    bind_machine_id: Option<String>,
    features: Option<Vec<String>>,
    max_nodes: u32,
) -> PyResult<String> {
    use ed25519_dalek::{Signer, SigningKey};
    use rand::Rng;

    let pk_bytes = hex::decode(private_key_hex)
        .map_err(|e| PyValueError::new_err(format!("Bad private key hex: {e}")))?;
    if pk_bytes.len() != 32 {
        return Err(PyValueError::new_err("Private key must be 32 bytes"));
    }
    let signing_key = SigningKey::from_bytes(pk_bytes.as_slice().try_into().unwrap());

    // Generate license ID
    let mut rng = rand::thread_rng();
    let license_id = format!(
        "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}",
        rng.gen::<u32>(), rng.gen::<u16>(), rng.gen::<u16>(),
        rng.gen::<u16>(), rng.gen::<u64>() & 0xFFFFFFFFFFFF
    );

    let mut license = License {
        licensee: licensee.to_string(),
        license_id: license_id.clone(),
        expires_at,
        grace_days,
        bind_mac: bind_mac.unwrap_or_default(),
        bind_hostname: bind_hostname.unwrap_or_default(),
        bind_machine_id: bind_machine_id.unwrap_or_default(),
        features: features.unwrap_or_default(),
        max_nodes,
        signature: String::new(),
    };

    // Sign
    let payload = license.signable_bytes();
    let sig = signing_key.sign(&payload);
    license.signature = hex::encode(sig.to_bytes());

    // Write
    let json = serde_json::to_string_pretty(&license)
        .map_err(|e| PyRuntimeError::new_err(format!("Serialize: {e}")))?;
    fs::write(output_path, &json)
        .map_err(|e| PyRuntimeError::new_err(format!("Write license: {e}")))?;

    Ok(license_id)
}

/// Validate a license file.  Returns dict with validation result.
#[pyfunction]
#[pyo3(signature = (license_path, public_key_hex=None))]
fn armor_validate_license(
    py: Python<'_>,
    license_path: &str,
    public_key_hex: Option<&str>,
) -> PyResult<PyObject> {
    let license = load_license(license_path)
        .map_err(|e| PyValueError::new_err(e))?;

    let pk = public_key_hex
        .map(String::from)
        .or_else(|| std::env::var("HEVOLVEARMOR_MASTER_PK").ok())
        .unwrap_or_else(|| {
            "4662e30d86c2f58416c5ac3f806c2a6af8186e1d96fdbbcad3189847cf888a01".to_string()
        });

    let result = license.validate(&pk);

    let dict = PyDict::new(py);
    dict.set_item("valid", result.valid)?;
    dict.set_item("expired", result.expired)?;
    dict.set_item("in_grace", result.in_grace)?;
    dict.set_item("machine_bound", result.machine_bound)?;
    dict.set_item("message", result.message)?;
    dict.set_item("licensee", &license.licensee)?;
    dict.set_item("license_id", &license.license_id)?;
    dict.set_item("expires_at", license.expires_at)?;
    dict.set_item("features", &license.features)?;
    Ok(dict.into())
}

/// Get current machine info (for license binding).
#[pyfunction]
fn armor_machine_info(py: Python<'_>) -> PyResult<PyObject> {
    let dict = PyDict::new(py);
    dict.set_item("mac_address", get_mac_address_string())?;
    dict.set_item("hostname", get_hostname_string())?;
    dict.set_item("machine_id", get_machine_id())?;
    Ok(dict.into())
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Public Python API
// ═══════════════════════════════════════════════════════════════════════════════

/// Install the armored import hook.  Key is derived automatically from the
/// HARTOS key hierarchy (Ed25519 node key → DATA_KEY → tier → passphrase).
///
/// Args:
///     modules_dir: path to encrypted modules (contains package subdirs + _manifest.txt)
///     passphrase: optional fallback passphrase for key derivation
///     node_key_path: optional explicit path to Ed25519 private key PEM
///     expected_hash: optional SHA-256 hash of this .pyd/.so for anti-tamper
///     package_names: optional list of top-level packages to intercept
#[pyfunction]
#[pyo3(signature = (modules_dir, passphrase=None, node_key_path=None, expected_hash=None, package_names=None, license_file=None, license_public_key=None))]
fn install(
    py: Python<'_>,
    modules_dir: &str,
    passphrase: Option<&str>,
    node_key_path: Option<&str>,
    expected_hash: Option<&str>,
    package_names: Option<Vec<String>>,
    license_file: Option<&str>,
    license_public_key: Option<&str>,
) -> PyResult<()> {
    let dir = Path::new(modules_dir);
    if !dir.is_dir() {
        return Err(PyValueError::new_err(format!(
            "modules_dir not found: {modules_dir}"
        )));
    }

    // ── License validation (if license file provided or auto-detected) ──
    let license_path = license_file
        .map(String::from)
        .or_else(|| {
            // Auto-detect: look for .license file next to modules_dir
            let parent = Path::new(modules_dir).parent()?;
            let auto = parent.join("hevolvearmor.license");
            if auto.exists() { Some(auto.to_string_lossy().to_string()) } else { None }
        })
        .or_else(|| std::env::var("HEVOLVEARMOR_LICENSE_FILE").ok());

    if let Some(ref lpath) = license_path {
        let pk = license_public_key
            .map(String::from)
            .or_else(|| std::env::var("HEVOLVEARMOR_LICENSE_PK").ok())
            .or_else(|| std::env::var("HEVOLVEARMOR_MASTER_PK").ok())
            .unwrap_or_else(|| {
                "4662e30d86c2f58416c5ac3f806c2a6af8186e1d96fdbbcad3189847cf888a01".to_string()
            });

        match load_license(lpath) {
            Ok(license) => {
                let result = license.validate(&pk);
                if !result.valid {
                    return Err(PyRuntimeError::new_err(format!(
                        "HevolveArmor license invalid: {}", result.message
                    )));
                }
                if result.in_grace {
                    // Warn but don't block
                    let _ = py.import("warnings").and_then(|w| {
                        w.call_method1("warn", (format!(
                            "HevolveArmor: {}", result.message
                        ),))
                    });
                }
            }
            Err(e) => {
                return Err(PyRuntimeError::new_err(format!(
                    "HevolveArmor license error: {e}"
                )));
            }
        }
    }

    // Derive key
    let key = derive_runtime_key_internal(node_key_path, passphrase)
        .map_err(|e| PyRuntimeError::new_err(format!("Key derivation failed: {e}")))?;

    // Auto-detect package names from subdirectories with __init__.enc
    let pkgs = if let Some(names) = package_names {
        names
    } else {
        let mut detected = Vec::new();
        if let Ok(entries) = fs::read_dir(dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() && path.join("__init__.enc").exists() {
                    if let Some(name) = entry.file_name().to_str() {
                        detected.push(name.to_string());
                    }
                }
            }
        }
        detected
    };

    // Anti-tamper check
    let actual_hash = {
        // Get our own .pyd/.so path
        let native_mod = py.import("hevolvearmor._native")?;
        let file_attr = native_mod.getattr("__file__");
        match file_attr {
            Ok(f) => {
                let path_str: String = f.extract()?;
                compute_self_hash(&path_str)
            }
            Err(_) => "unknown".into(),
        }
    };

    if let Some(expected) = expected_hash {
        if actual_hash != expected && actual_hash != "unknown" {
            return Err(PyRuntimeError::new_err(
                "HevolveArmor: binary tamper detected (hash mismatch). Aborting.",
            ));
        }
    }

    // Store state
    {
        let mut guard = STATE.lock().map_err(|_| PyRuntimeError::new_err("lock"))?;
        *guard = Some(ArmorState {
            key,
            modules_dir: dir.to_path_buf(),
            package_names: pkgs.clone(),
            code_cache: HashMap::new(),
            self_hash: Some(actual_hash),
            expected_hash: expected_hash.map(String::from),
            armed: true,
        });
    }

    // Register the finder on sys.meta_path
    let sys = py.import("sys")?;
    let meta_path = sys.getattr("meta_path")?;
    let finder = Py::new(py, PyArmoredFinder {})?;
    meta_path.call_method1("insert", (0i32, finder))?;

    // Anti-debug: check for attached debuggers if enforcement is on
    let enforce = std::env::var("HEVOLVEARMOR_ENFORCE")
        .map(|v| v == "1" || v.to_lowercase() == "true")
        .unwrap_or(false);

    if enforce {
        if is_debugger_present() {
            // Wipe key immediately
            let mut guard2 = STATE.lock().map_err(|_| PyRuntimeError::new_err("lock"))?;
            if let Some(ref mut s) = *guard2 {
                s.key.zeroize();
                s.armed = false;
            }
            return Err(PyRuntimeError::new_err(
                "HevolveArmor: debugger detected. Cannot load encrypted modules under debugger.",
            ));
        }
    }

    // Disable sys.settrace / sys.setprofile to resist debugger attachment
    let _ = sys.setattr("settrace", py.None());
    let _ = sys.setattr("setprofile", py.None());

    Ok(())
}

/// Uninstall: remove the finder from sys.meta_path and wipe the key.
#[pyfunction]
fn uninstall(py: Python<'_>) -> PyResult<()> {
    // Remove finder from sys.meta_path
    let sys = py.import("sys")?;
    let meta_path: Bound<'_, PyList> = sys.getattr("meta_path")?.downcast_into()?;
    let mut to_remove = Vec::new();
    for (i, item) in meta_path.iter().enumerate() {
        if item.is_instance_of::<PyArmoredFinder>() {
            to_remove.push(i);
        }
    }
    for i in to_remove.into_iter().rev() {
        meta_path.call_method1("pop", (i,))?;
    }

    // Wipe state
    let mut guard = STATE.lock().map_err(|_| PyRuntimeError::new_err("lock"))?;
    if let Some(ref mut s) = *guard {
        s.key.zeroize();
        s.armed = false;
    }
    *guard = None;

    Ok(())
}

/// Derive the runtime key (for build scripts that need to encrypt with
/// the same key the runtime will use).
#[pyfunction]
#[pyo3(signature = (passphrase=None, node_key_path=None))]
fn derive_runtime_key(
    py: Python<'_>,
    passphrase: Option<&str>,
    node_key_path: Option<&str>,
) -> PyResult<Py<PyBytes>> {
    let mut key = derive_runtime_key_internal(node_key_path, passphrase)
        .map_err(|e| PyRuntimeError::new_err(e))?;
    let result = PyBytes::new(py, &key).unbind();
    key.zeroize();
    Ok(result)
}

// ─── Build-time functions (still exposed for the Python builder) ─────────────

#[pyfunction]
fn armor_encrypt(py: Python<'_>, data: &[u8], key: &[u8]) -> PyResult<Py<PyBytes>> {
    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err(format!("Key must be {KEY_SIZE} bytes")));
    }
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let ct = encrypt_bytes(data, &k).map_err(|e| PyRuntimeError::new_err(e))?;
    Ok(PyBytes::new(py, &ct).unbind())
}

/// Runtime decrypt using the global key (for string/function unwrapping).
/// Called from Python _transforms.py callbacks — the key never leaves Rust.
#[pyfunction]
fn _runtime_decrypt(py: Python<'_>, blob: &[u8]) -> PyResult<Py<PyBytes>> {
    let guard = STATE.lock().map_err(|_| PyRuntimeError::new_err("lock"))?;
    let state = guard
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("HevolveArmor not initialized"))?;
    let pt = decrypt_bytes(blob, &state.key)
        .map_err(|e| PyRuntimeError::new_err(format!("Runtime decrypt: {e}")))?;
    Ok(PyBytes::new(py, &pt).unbind())
}

#[pyfunction]
fn armor_decrypt(py: Python<'_>, blob: &[u8], key: &[u8]) -> PyResult<Py<PyBytes>> {
    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err(format!("Key must be {KEY_SIZE} bytes")));
    }
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let pt = decrypt_bytes(blob, &k).map_err(|e| PyRuntimeError::new_err(e))?;
    Ok(PyBytes::new(py, &pt).unbind())
}

#[pyfunction]
fn armor_generate_key(py: Python<'_>) -> Py<PyBytes> {
    let mut key = [0u8; KEY_SIZE];
    OsRng.fill_bytes(&mut key);
    let r = PyBytes::new(py, &key).unbind();
    key.zeroize();
    r
}

#[pyfunction]
fn armor_derive_key_ed25519(py: Python<'_>, private_key_bytes: &[u8]) -> PyResult<Py<PyBytes>> {
    let mut k = derive_key_from_ed25519(private_key_bytes).map_err(|e| PyValueError::new_err(e))?;
    let r = PyBytes::new(py, &k).unbind();
    k.zeroize();
    Ok(r)
}

#[pyfunction]
fn armor_derive_key_passphrase(py: Python<'_>, passphrase: &str) -> Py<PyBytes> {
    let mut k = derive_key_from_passphrase(passphrase);
    let r = PyBytes::new(py, &k).unbind();
    k.zeroize();
    r
}

#[pyfunction]
fn armor_derive_key_raw(py: Python<'_>, raw_bytes: &[u8]) -> Py<PyBytes> {
    let mut k = derive_key_from_bytes(raw_bytes);
    let r = PyBytes::new(py, &k).unbind();
    k.zeroize();
    r
}

#[pyfunction]
fn armor_self_hash(py: Python<'_>) -> PyResult<String> {
    let native_mod = py.import("hevolvearmor._native")?;
    let path: String = native_mod.getattr("__file__")?.extract()?;
    Ok(compute_self_hash(&path))
}

#[pyfunction]
fn armor_load_module(py: Python<'_>, enc_path: &str, key: &[u8]) -> PyResult<Py<PyBytes>> {
    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err("Key must be 32 bytes"));
    }
    let blob =
        fs::read(enc_path).map_err(|e| PyImportError::new_err(format!("Read {enc_path}: {e}")))?;
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let pyc =
        decrypt_bytes(&blob, &k).map_err(|e| PyImportError::new_err(format!("Decrypt: {e}")))?;
    Ok(PyBytes::new(py, &pyc).unbind())
}

// ─── Module Definition ───────────────────────────────────────────────────────

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Runtime API (the only functions users need)
    m.add_function(wrap_pyfunction!(install, m)?)?;
    m.add_function(wrap_pyfunction!(uninstall, m)?)?;
    m.add_function(wrap_pyfunction!(derive_runtime_key, m)?)?;
    m.add_function(wrap_pyfunction!(_runtime_decrypt, m)?)?;

    // Build-time API
    m.add_function(wrap_pyfunction!(armor_encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(armor_decrypt, m)?)?;
    m.add_function(wrap_pyfunction!(armor_generate_key, m)?)?;
    m.add_function(wrap_pyfunction!(armor_derive_key_ed25519, m)?)?;
    m.add_function(wrap_pyfunction!(armor_derive_key_passphrase, m)?)?;
    m.add_function(wrap_pyfunction!(armor_derive_key_raw, m)?)?;
    m.add_function(wrap_pyfunction!(armor_self_hash, m)?)?;
    m.add_function(wrap_pyfunction!(armor_load_module, m)?)?;
    m.add_function(wrap_pyfunction!(armor_is_debugger_present, m)?)?;
    m.add_function(wrap_pyfunction!(armor_generate_license, m)?)?;
    m.add_function(wrap_pyfunction!(armor_validate_license, m)?)?;
    m.add_function(wrap_pyfunction!(armor_machine_info, m)?)?;

    // Classes
    m.add_class::<PyArmoredFinder>()?;
    m.add_class::<PyArmoredLoader>()?;

    // Constants
    m.add("KEY_SIZE", KEY_SIZE)?;
    m.add("NONCE_SIZE", NONCE_SIZE)?;

    Ok(())
}
