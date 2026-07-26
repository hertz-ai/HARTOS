"""
A4 compat-shim behavioural tests — local LLM reachability/provisioning (P0b) and
the realtime-origin wiring (P0a) for the two OS-side NixOS modules this change
owns:
    nixos/modules/hart-llm.nix
    nixos/modules/hart-backend.nix

These are NixOS *modules* (no Python object to import for the unit itself), so the
tests reach the actual behaviour two ways and fall back to clearly-labelled
source-shape guards only for the systemd invariants that genuinely need Nix:

  1. REAL resolver:  core.port_registry.get_local_llm_url() / is_local_llm() prove
     that the env the backend module pins (HEVOLVE_LOCAL_LLM_URL -> local llama)
     makes an agent/chat call resolve to ON-DEVICE inference, never a remote proxy
     (P0b "never proxied out").
  2. REAL provisioner: the hart-llm-provision shell script is EXTRACTED from
     hart-llm.nix and executed under a mocked `curl`, asserting its observable
     behaviour — skip-if-present, atomic publish (.part -> mv), and offline =
     exit 0 + no clobber (P0b "provisioned, best-effort, offline-safe").
  3. Source guards (labelled `test_guard_*`): the CAP_NET_BIND_SERVICE grant on the
     hart-llm unit, the legacy download-mechanism reuse, and the non-boot-critical
     ordering — cross-file Nix invariants a behavioural test cannot reach without
     a VM (those are covered by nixos/tests/llm-provision.nix, CI-gated).

No grep-as-test: #1 calls the real resolver, #2 runs the real script.
"""

import os
import re
import shutil
import stat
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HART_LLM_NIX = os.path.join(REPO_ROOT, "nixos", "modules", "hart-llm.nix")
HART_BACKEND_NIX = os.path.join(REPO_ROOT, "nixos", "modules", "hart-backend.nix")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# 1. REAL resolver — the backend env pin keeps inference LOCAL (P0b)
# ═══════════════════════════════════════════════════════════════

class TestLocalLlmNeverProxied:
    """The HEVOLVE_LOCAL_LLM_URL the backend module sets must make the in-process
    agent_engine resolve to the local llama, never a remote endpoint."""

    def test_pinned_local_llm_url_resolves_local(self, monkeypatch, tmp_path):
        # Reproduce the env hart-backend.nix sets for the OS-mode backend.
        monkeypatch.setenv("HEVOLVE_LOCAL_LLM_URL", "http://127.0.0.1:808/v1")
        # Isolate from any real ~/.nunba/llama_config.json on the dev box so the
        # resolver can't pick up a developer's external endpoint.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser
        for noise in ("CUSTOM_LLM_BASE_URL", "LLAMA_CPP_PORT",
                      "HEVOLVE_LOCAL_LLM_MODEL"):
            monkeypatch.delenv(noise, raising=False)

        from core import port_registry
        port_registry.invalidate_llm_url()

        url = port_registry.get_local_llm_url()
        # Pinned URL is the top resolver candidate; on a box with nothing
        # listening it still returns as the stable placeholder.
        assert "127.0.0.1:808" in url, url
        assert port_registry.is_local_llm() is True

    def test_resolver_url_is_loopback_only(self, monkeypatch, tmp_path):
        """A loopback pin must never resolve to a non-loopback host."""
        monkeypatch.setenv("HEVOLVE_LOCAL_LLM_URL", "http://127.0.0.1:808/v1")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        for noise in ("CUSTOM_LLM_BASE_URL", "LLAMA_CPP_PORT",
                      "HEVOLVE_LOCAL_LLM_MODEL"):
            monkeypatch.delenv(noise, raising=False)
        from core import port_registry
        port_registry.invalidate_llm_url()
        url = port_registry.get_local_llm_url()
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        assert host in ("127.0.0.1", "localhost", "0.0.0.0"), host

    def test_backend_module_pins_local_llm_env(self):
        """The backend module sets HEVOLVE_LOCAL_LLM_URL to the local llama port —
        the env the resolver test above proves resolves local."""
        backend = _read(HART_BACKEND_NIX)
        m = re.search(r'HEVOLVE_LOCAL_LLM_URL\s*=\s*"([^"]+)"', backend)
        assert m, "backend must pin HEVOLVE_LOCAL_LLM_URL"
        val = m.group(1)
        assert "127.0.0.1" in val
        assert "cfg.ports.llm" in val, \
            "must target the local llama port, not a literal/remote host"


# ═══════════════════════════════════════════════════════════════
# 2. REAL provisioner script — run the extracted shell under mocked curl
# ═══════════════════════════════════════════════════════════════

def _extract_provision_script():
    """Pull the actual hart-llm-provision body out of hart-llm.nix and render the
    Nix `''` string to runnable bash (un-escape ''${ -> ${, substitute the two Nix
    interpolations with test placeholders the harness controls via $TEST_*)."""
    src = _read(HART_LLM_NIX)
    m = re.search(
        r'writeShellScript "hart-llm-provision" \'\'\n(.*?)\n[ \t]*\'\';',
        src, re.S)
    assert m, "could not locate hart-llm-provision script in hart-llm.nix"
    body = m.group(1)
    body = body.replace("''${", "${")  # Nix antiquote escape -> shell ${
    # The two Nix interpolations -> harness-provided env, so the test drives the
    # paths/URL without re-implementing the algorithm.
    body = body.replace("${config.hart.llm.modelPath}", "${TEST_MODEL_PATH}")
    body = body.replace("${config.hart.llm.modelUrl}", "${TEST_MODEL_URL}")
    return body


def _bash():
    return shutil.which("bash") or shutil.which("sh")


def _run_provision(tmp_path, curl_behaviour, preexisting=None):
    """Execute the real provisioner with a fake `curl` on PATH.

    curl_behaviour: 'success' (writes the -o target) or 'fail' (exit 1).
    preexisting:    content to pre-place at the model path (skip-if-present case).
    Returns (returncode, model_path, part_path, stdout+stderr).
    """
    bash = _bash()
    if not bash:
        pytest.skip("no bash/sh on PATH (runs in CI)")

    models = tmp_path / "models"
    models.mkdir()
    model_path = models / "default.gguf"
    if preexisting is not None:
        model_path.write_text(preexisting)

    # Fake curl: success writes a fake gguf to the -o target; fail exits non-zero
    # WITHOUT writing (mirrors a real failed/offline transfer).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    if curl_behaviour == "success":
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "out=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-o\" ]; then out=\"$2\"; shift 2; continue; fi\n"
            "  shift\n"
            "done\n"
            "printf 'FAKE-GGUF-BYTES' > \"$out\"\n"
            "exit 0\n"
        )
    else:
        curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    script = tmp_path / "provision.sh"
    script.write_text(_extract_provision_script())

    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["TEST_MODEL_PATH"] = str(model_path)
    env["TEST_MODEL_URL"] = "http://example.invalid/model.gguf"
    # Ensure the env-override branch is exercised deterministically.
    env.pop("HART_DEFAULT_MODEL_URL", None)

    proc = subprocess.run(
        [bash, str(script)], env=env, capture_output=True, text=True, timeout=60)
    return (proc.returncode, model_path, models / "default.gguf.part",
            proc.stdout + proc.stderr)


class TestProvisionerScript:
    """Observable behaviour of the REAL extracted provisioner shell."""

    def test_success_publishes_model_atomically(self, tmp_path):
        rc, model, part, out = _run_provision(tmp_path, "success")
        assert rc == 0, out
        assert model.exists() and model.read_text() == "FAKE-GGUF-BYTES"
        assert not part.exists(), "the .part temp must be moved away, never left"

    def test_skip_when_model_already_present(self, tmp_path):
        rc, model, part, out = _run_provision(
            tmp_path, "fail", preexisting="USER-PROVIDED-MODEL")
        # curl would FAIL, but the script must never reach it — model untouched.
        assert rc == 0, out
        assert model.read_text() == "USER-PROVIDED-MODEL"
        assert "already present" in out

    def test_offline_failure_is_non_fatal_and_no_clobber(self, tmp_path):
        rc, model, part, out = _run_provision(tmp_path, "fail")
        # Offline must NOT be a unit failure (exit 0) and must leave NO partial
        # file for llama-server to choke on.
        assert rc == 0, out
        assert not model.exists(), "no model on a failed download"
        assert not part.exists(), "no leftover .part on failure"


# ═══════════════════════════════════════════════════════════════
# 3. Source guards — Nix-only systemd invariants (VM test covers behaviour)
# ═══════════════════════════════════════════════════════════════

class TestNixServiceGuards:
    """Cross-file invariants on the two owned modules that need Nix to *run*;
    the behavioural counterpart is nixos/tests/llm-provision.nix (CI/[VM])."""

    def test_guard_llm_binds_privileged_port_with_cap(self):
        """The default OS-mode LLM port is privileged (<1024) and the unit runs as
        the unprivileged hart user — it MUST carry CAP_NET_BIND_SERVICE or it
        crash-loops on bind."""
        llm = _read(HART_LLM_NIX)
        assert 'AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ]' in llm
        assert 'CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ]' in llm

    def test_guard_provisioner_reuses_legacy_download_mechanism(self):
        """Reuse the legacy first-boot contract (HART_DEFAULT_MODEL_URL), do not
        invent a new downloader."""
        llm = _read(HART_LLM_NIX)
        assert "HART_DEFAULT_MODEL_URL" in llm
        # Same default model the legacy deploy/distro/first-boot/hart-first-boot.sh
        # fetched (TinyLlama Q4_K_M GGUF).
        assert "TinyLlama-1.1B-Chat-v1.0-GGUF" in llm

    def test_guard_provisioner_is_idempotent_and_atomic(self):
        llm = _read(HART_LLM_NIX)
        assert 'ConditionPathExists = "!${config.hart.llm.modelPath}"' in llm
        assert ".part" in llm and "mv -f" in llm  # atomic publish

    def test_guard_provisioner_not_boot_critical(self):
        """The provisioner declares its OWN network-online wait and only orders
        BEFORE hart-llm — never before anything the shell needs (so a slow model
        download cannot stall the desktop)."""
        llm = _read(HART_LLM_NIX)
        block = llm.split("hart-llm-provision = ", 1)[1].split("systemd.services.hart-llm =", 1)[0]
        assert 'wants = [ "network-online.target" ]' in block
        assert 'before = [ "hart-llm.service" ]' in block
        # Must NOT order before the realtime/shell-critical units.
        assert "hart-backend.service" not in block.split("before =", 1)[1].split(";", 1)[0]
        assert "hart-liquid-ui" not in block

    def test_guard_provisioner_gated_on_autoprovision(self):
        llm = _read(HART_LLM_NIX)
        assert "autoProvision" in llm
        assert "lib.mkIf config.hart.llm.autoProvision" in llm
