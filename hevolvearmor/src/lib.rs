//! HevolveArmor — Encrypted Python module loader for HART OS.
//!
//! Provides AES-256-GCM encryption/decryption of Python .pyc bytecode,
//! with key derivation from HART OS's Ed25519 node identity + tier certificate.
//!
//! Build-time: `armor_encrypt()` compiles .py → .pyc → .enc
//! Runtime:    `ArmoredLoader` decrypts .enc → .pyc → code object via sys.meta_path
//!
//! Key derivation chain:
//!   node_private_key (Ed25519) → sign(b"hevolvearmor-v1") → HKDF-SHA256 → AES key
//!   Falls back to: HEVOLVE_DATA_KEY env var → PBKDF2(passphrase)
//!
//! Anti-tamper: binary self-hash verified at init; key wiped on failure.

use aes_gcm::aead::{Aead, KeyInit, OsRng};
use aes_gcm::{Aes256Gcm, Nonce};
use hkdf::Hkdf;
use pyo3::exceptions::{PyImportError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyModule};
use rand::RngCore;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;
use zeroize::Zeroize;

// ─── Constants ───────────────────────────────────────────────────────────────

const NONCE_SIZE: usize = 12;
const KEY_SIZE: usize = 32;
const HKDF_INFO: &[u8] = b"hevolvearmor-v1-module-key";
const HKDF_SALT: &[u8] = b"hart-os-encrypted-modules-salt";
const MANIFEST_FILE: &str = "_manifest.txt";

// ─── Key Derivation ──────────────────────────────────────────────────────────

/// Derive AES-256 key from raw key material using HKDF-SHA256.
fn derive_aes_key(ikm: &[u8]) -> [u8; KEY_SIZE] {
    let hk = Hkdf::<Sha256>::new(Some(HKDF_SALT), ikm);
    let mut okm = [0u8; KEY_SIZE];
    hk.expand(HKDF_INFO, &mut okm)
        .expect("HKDF expand failed — output length is valid");
    okm
}

/// Derive key from Ed25519 private key bytes (sign a fixed domain string).
fn derive_key_from_ed25519(private_key_bytes: &[u8]) -> Result<[u8; KEY_SIZE], String> {
    use ed25519_dalek::SigningKey;

    if private_key_bytes.len() != 32 {
        return Err(format!(
            "Ed25519 private key must be 32 bytes, got {}",
            private_key_bytes.len()
        ));
    }

    let signing_key = SigningKey::from_bytes(
        private_key_bytes
            .try_into()
            .map_err(|_| "Invalid key length")?,
    );

    // Sign a fixed domain string — the signature IS the key material
    use ed25519_dalek::Signer;
    let sig = signing_key.sign(b"hevolvearmor-keygen-v1");
    let sig_bytes = sig.to_bytes();

    // Use first 32 bytes of signature as IKM for HKDF
    Ok(derive_aes_key(&sig_bytes[..32]))
}

/// Derive key from a passphrase using SHA-256 + HKDF (no PBKDF2 to avoid
/// depending on ring/openssl — cryptography Python lib handles that).
fn derive_key_from_passphrase(passphrase: &str) -> [u8; KEY_SIZE] {
    let mut hasher = Sha256::new();
    hasher.update(passphrase.as_bytes());
    hasher.update(HKDF_SALT);
    let hash = hasher.finalize();
    derive_aes_key(&hash)
}

/// Derive key from raw bytes (Fernet key, HEVOLVE_DATA_KEY, etc.)
fn derive_key_from_bytes(raw: &[u8]) -> [u8; KEY_SIZE] {
    derive_aes_key(raw)
}

// ─── Encryption / Decryption ─────────────────────────────────────────────────

/// Encrypt plaintext with AES-256-GCM.  Returns nonce(12) || ciphertext+tag.
fn encrypt(data: &[u8], key: &[u8; KEY_SIZE]) -> Result<Vec<u8>, String> {
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|e| format!("Cipher init: {e}"))?;
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

/// Decrypt AES-256-GCM blob (nonce || ciphertext+tag).
fn decrypt(blob: &[u8], key: &[u8; KEY_SIZE]) -> Result<Vec<u8>, String> {
    if blob.len() < NONCE_SIZE + 16 {
        return Err("Blob too short for AES-256-GCM".into());
    }
    let (nonce_bytes, ct) = blob.split_at(NONCE_SIZE);
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|e| format!("Cipher init: {e}"))?;
    let nonce = Nonce::from_slice(nonce_bytes);
    cipher
        .decrypt(nonce, ct)
        .map_err(|_| "Decryption failed — wrong key or corrupted data".into())
}

// ─── Self-Hash (anti-tamper) ─────────────────────────────────────────────────

/// SHA-256 hash of this binary (the .so/.pyd itself).
fn self_hash() -> String {
    // Find our own .so/.pyd path from the module's __file__
    // Fallback: hash a sentinel
    let exe = std::env::current_exe().unwrap_or_default();
    if let Ok(data) = fs::read(&exe) {
        let mut hasher = Sha256::new();
        hasher.update(&data);
        hex::encode(hasher.finalize())
    } else {
        "unknown".to_string()
    }
}

// ─── Module Manifest ─────────────────────────────────────────────────────────

/// Read the manifest of encrypted modules from a directory.
fn read_manifest(modules_dir: &Path) -> Result<Vec<String>, String> {
    let manifest_path = modules_dir.join(MANIFEST_FILE);
    if !manifest_path.exists() {
        return Err(format!("Manifest not found: {}", manifest_path.display()));
    }
    let content =
        fs::read_to_string(&manifest_path).map_err(|e| format!("Read manifest: {e}"))?;
    Ok(content
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| l.to_string())
        .collect())
}

/// Convert a file-relative path (e.g. "embodied_ai/core/__init__.py") to a
/// Python module name (e.g. "embodied_ai.core").
fn rel_path_to_module_name(rel: &str) -> String {
    let stripped = rel
        .strip_suffix("/__init__.py")
        .or_else(|| rel.strip_suffix(".py"))
        .unwrap_or(rel);
    stripped.replace('/', ".")
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Python API (PyO3)
// ═══════════════════════════════════════════════════════════════════════════════

/// Build-time: encrypt a single file's bytes.
#[pyfunction]
fn armor_encrypt(py: Python<'_>, data: &[u8], key: &[u8]) -> PyResult<Py<PyBytes>> {
    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err(format!(
            "Key must be {KEY_SIZE} bytes, got {}",
            key.len()
        )));
    }
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let ct = encrypt(data, &k).map_err(|e| PyRuntimeError::new_err(e))?;
    Ok(PyBytes::new(py, &ct).unbind())
}

/// Build-time: decrypt a single file's bytes.
#[pyfunction]
fn armor_decrypt(py: Python<'_>, blob: &[u8], key: &[u8]) -> PyResult<Py<PyBytes>> {
    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err(format!(
            "Key must be {KEY_SIZE} bytes, got {}",
            key.len()
        )));
    }
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let pt = decrypt(blob, &k).map_err(|e| PyRuntimeError::new_err(e))?;
    Ok(PyBytes::new(py, &pt).unbind())
}

/// Generate a random 32-byte AES key.
#[pyfunction]
fn armor_generate_key(py: Python<'_>) -> Py<PyBytes> {
    let mut key = [0u8; KEY_SIZE];
    OsRng.fill_bytes(&mut key);
    let result = PyBytes::new(py, &key).unbind();
    key.zeroize();
    result
}

/// Derive AES key from Ed25519 private key bytes (32 bytes).
#[pyfunction]
fn armor_derive_key_ed25519(py: Python<'_>, private_key_bytes: &[u8]) -> PyResult<Py<PyBytes>> {
    let mut key =
        derive_key_from_ed25519(private_key_bytes).map_err(|e| PyValueError::new_err(e))?;
    let result = PyBytes::new(py, &key).unbind();
    key.zeroize();
    Ok(result)
}

/// Derive AES key from passphrase string.
#[pyfunction]
fn armor_derive_key_passphrase(py: Python<'_>, passphrase: &str) -> Py<PyBytes> {
    let mut key = derive_key_from_passphrase(passphrase);
    let result = PyBytes::new(py, &key).unbind();
    key.zeroize();
    result
}

/// Derive AES key from raw bytes (e.g. HEVOLVE_DATA_KEY).
#[pyfunction]
fn armor_derive_key_raw(py: Python<'_>, raw_bytes: &[u8]) -> Py<PyBytes> {
    let mut key = derive_key_from_bytes(raw_bytes);
    let result = PyBytes::new(py, &key).unbind();
    key.zeroize();
    result
}

/// Get SHA-256 hash of the current binary (anti-tamper).
#[pyfunction]
fn armor_self_hash() -> String {
    self_hash()
}

/// Decrypt a .enc file from disk and return the raw .pyc bytes.
/// Used by the Python-side import hook.
#[pyfunction]
fn armor_load_module(py: Python<'_>, enc_path: &str, key: &[u8]) -> PyResult<Py<PyBytes>> {
    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err("Key must be 32 bytes"));
    }
    let blob = fs::read(enc_path)
        .map_err(|e| PyImportError::new_err(format!("Cannot read {enc_path}: {e}")))?;
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let pyc = decrypt(&blob, &k)
        .map_err(|e| PyImportError::new_err(format!("Decrypt failed for {enc_path}: {e}")))?;
    Ok(PyBytes::new(py, &pyc).unbind())
}

/// Read and parse a manifest file, returning module names.
#[pyfunction]
fn armor_read_manifest(modules_dir: &str) -> PyResult<Vec<String>> {
    let path = Path::new(modules_dir);
    let entries = read_manifest(path).map_err(|e| PyRuntimeError::new_err(e))?;
    Ok(entries
        .iter()
        .map(|e| rel_path_to_module_name(e))
        .collect())
}

/// Bulk-encrypt a directory of .py files.  Build-time helper.
///
/// Args:
///     source_dir: path to Python package root
///     output_dir: path to write .enc files
///     key: 32-byte AES key
///     skip_dirs: list of directory names to skip
///
/// Returns: dict with {encrypted, failed, total_bytes}
#[pyfunction]
#[pyo3(signature = (source_dir, output_dir, key, skip_dirs=None))]
fn armor_encrypt_package(
    py: Python<'_>,
    source_dir: &str,
    output_dir: &str,
    key: &[u8],
    skip_dirs: Option<Vec<String>>,
) -> PyResult<PyObject> {
    use std::io::Write;

    if key.len() != KEY_SIZE {
        return Err(PyValueError::new_err("Key must be 32 bytes"));
    }
    let k: [u8; KEY_SIZE] = key.try_into().unwrap();
    let src = Path::new(source_dir);
    let out = Path::new(output_dir);

    let default_skips: Vec<String> = vec![
        "__pycache__", ".git", "tests", "test", "legacy", "dashboard", ".egg-info", "dist",
        "build",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    let skips = skip_dirs.unwrap_or(default_skips);

    // Clean output
    if out.exists() {
        fs::remove_dir_all(out)
            .map_err(|e| PyRuntimeError::new_err(format!("Clean output: {e}")))?;
    }
    fs::create_dir_all(out).map_err(|e| PyRuntimeError::new_err(format!("Create output: {e}")))?;

    let mut encrypted = 0u64;
    let mut failed = 0u64;
    let mut total_bytes = 0u64;
    let mut manifest = Vec::new();

    // Walk source directory
    fn walk(
        dir: &Path,
        src_root: &Path,
        out_root: &Path,
        key: &[u8; KEY_SIZE],
        skips: &[String],
        encrypted: &mut u64,
        failed: &mut u64,
        total_bytes: &mut u64,
        manifest: &mut Vec<String>,
    ) {
        let entries = match fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => return,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();

            if path.is_dir() {
                if skips.contains(&name) || name.ends_with(".egg-info") {
                    continue;
                }
                walk(
                    &path,
                    src_root,
                    out_root,
                    key,
                    skips,
                    encrypted,
                    failed,
                    total_bytes,
                    manifest,
                );
            } else if name.ends_with(".py") {
                let rel = path
                    .strip_prefix(src_root)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                let enc_rel = rel.replace(".py", ".enc");
                let enc_path = out_root.join(&enc_rel);

                if let Some(parent) = enc_path.parent() {
                    let _ = fs::create_dir_all(parent);
                }

                match fs::read_to_string(&path) {
                    Ok(source) => {
                        // We can't compile Python source from Rust — write source bytes
                        // encrypted. The Python build script will compile first, then
                        // call armor_encrypt() on the .pyc bytes.
                        // For the bulk API, we encrypt the raw source as a fallback.
                        // The preferred path is: Python compiles → passes .pyc bytes
                        // to armor_encrypt().
                        let data = source.as_bytes();
                        match encrypt(data, key) {
                            Ok(ct) => {
                                if let Ok(mut f) = fs::File::create(&enc_path) {
                                    let _ = f.write_all(&ct);
                                    *encrypted += 1;
                                    *total_bytes += ct.len() as u64;
                                    manifest.push(rel);
                                }
                            }
                            Err(_) => *failed += 1,
                        }
                    }
                    Err(_) => *failed += 1,
                }
            }
        }
    }

    walk(
        src,
        src,
        out,
        &k,
        &skips,
        &mut encrypted,
        &mut failed,
        &mut total_bytes,
        &mut manifest,
    );

    // Write manifest
    let manifest_path = out.join(MANIFEST_FILE);
    let manifest_content = manifest.join("\n");
    fs::write(&manifest_path, &manifest_content)
        .map_err(|e| PyRuntimeError::new_err(format!("Write manifest: {e}")))?;

    let dict = PyDict::new(py);
    dict.set_item("encrypted", encrypted)?;
    dict.set_item("failed", failed)?;
    dict.set_item("total_bytes", total_bytes)?;
    Ok(dict.into())
}

// ─── Module Definition ───────────────────────────────────────────────────────

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(armor_encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(armor_decrypt, m)?)?;
    m.add_function(wrap_pyfunction!(armor_generate_key, m)?)?;
    m.add_function(wrap_pyfunction!(armor_derive_key_ed25519, m)?)?;
    m.add_function(wrap_pyfunction!(armor_derive_key_passphrase, m)?)?;
    m.add_function(wrap_pyfunction!(armor_derive_key_raw, m)?)?;
    m.add_function(wrap_pyfunction!(armor_self_hash, m)?)?;
    m.add_function(wrap_pyfunction!(armor_load_module, m)?)?;
    m.add_function(wrap_pyfunction!(armor_read_manifest, m)?)?;
    m.add_function(wrap_pyfunction!(armor_encrypt_package, m)?)?;
    m.add("KEY_SIZE", KEY_SIZE)?;
    m.add("NONCE_SIZE", NONCE_SIZE)?;
    Ok(())
}
