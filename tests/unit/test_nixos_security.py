"""
HART OS - hart-security.nix structural + safety guard (portable, no Nix needed)

The BEHAVIOURAL proof for hart-security (clamd/freshclam units come up, the
hardening sysctls take effect on the live kernel, the shell/SSH/netdiag ports
survive) lives in the booted-VM nixosTest nixos/tests/security.nix - a real
nftables ruleset and a real running kernel cannot be exercised on the Windows dev
box. THIS test is the portable companion: it runs on every platform (CI included)
and guards the source-shape invariants the VM test cannot cheaply assert per-line,
most importantly the SAFETY invariants:

  - the module never touches any master-key / guardrail / immutable material
    (AI-exclusion zone - CLAUDE.md);
  - the firewall hardening is purely ADDITIVE: it never manages (and so can never
    strip) the firewall port list, and it carries the eval-time assertion that the
    shell + SSH ports survive;
  - the antivirus is LOCAL with a single, toggleable network egress (signatures);
  - the OTA delivery path for OS/app security fixes is documented in the module.

This is a clearly-labelled source guard (CLAUDE.md Gate 5: acceptable for a
many-files / VM-only-behaviour concern, never the ONLY test for a change - the
nixosTest is the behavioural one).
"""

import os
import re
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "nixos", "modules", "hart-security.nix")
NIXTEST = os.path.join(REPO_ROOT, "nixos", "tests", "security.nix")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def mod():
    return _read(MODULE)


@pytest.fixture(scope="module")
def nixtest():
    return _read(NIXTEST)


# ── File existence ────────────────────────────────────────────

def test_module_file_exists():
    assert os.path.isfile(MODULE), "nixos/modules/hart-security.nix is missing"


def test_nixos_test_file_exists():
    assert os.path.isfile(NIXTEST), "nixos/tests/security.nix is missing"


# ── Module shape ──────────────────────────────────────────────

def test_module_is_a_nixos_module(mod):
    assert re.search(r"\{[^}]*config[^}]*lib[^}]*pkgs[^}]*\}", mod[:200]), \
        "module must take { config, lib, pkgs, ... }"


def test_module_is_opt_in_gated(mod):
    # Pure no-op unless BOTH hart.enable and hart.security.enable are on.
    assert "lib.mkIf (cfg.enable && sec.enable)" in mod, \
        "config must be gated on cfg.enable && sec.enable (no-op otherwise)"


def test_defines_security_options(mod):
    assert "options.hart.security" in mod
    assert "mkEnableOption" in mod  # master enable


@pytest.mark.parametrize("opt", [
    "antivirus",
    "updates",
    "frequency",
    "firewallHardening",
])
def test_defines_suboptions(mod, opt):
    assert opt in mod, f"hart.security option '{opt}' missing"


# ── (1) ClamAV: daemon + freshclam updater, local + single egress ──

def test_enables_clamd_daemon(mod):
    assert "services.clamav" in mod
    assert "daemon.enable = true" in mod


def test_enables_freshclam_updater(mod):
    # The updater (the ONE network egress) is wired, and gated behind its toggle.
    assert "updater" in mod
    assert "sec.antivirus.updates.enable" in mod, \
        "freshclam updates must be gated on antivirus.updates.enable (egress toggle)"


def test_installs_clamav_package(mod):
    assert "pkgs.clamav" in mod, "the ClamAV CLIs must be in the closure"


def test_antivirus_is_local_default_on(mod):
    # antivirus.enable defaults true (local protection ships on); the egress toggle
    # also defaults true but is its own switch.
    assert re.search(r"antivirus\s*=\s*\{", mod)
    # The daemon block is gated so it is a no-op when antivirus.enable = false.
    assert "lib.mkIf sec.antivirus.enable" in mod


# ── (2) Firewall hardening: additive, port-preserving ─────────

@pytest.mark.parametrize("sysctl", [
    "net.ipv4.tcp_syncookies",
    "net.ipv4.conf.all.send_redirects",
    "net.ipv4.conf.all.accept_source_route",
    "net.ipv6.conf.all.accept_source_route",
    "net.ipv4.conf.all.log_martians",
    "kernel.yama.ptrace_scope",
])
def test_hardening_sysctl_present(mod, sysctl):
    assert sysctl in mod, f"firewall-hardening sysctl '{sysctl}' missing"


def test_hardening_sysctls_use_mkdefault(mod):
    # Every hardening sysctl must be mkDefault so it can never CONFLICT with
    # hart-base/hart-kernel/hart-devtools (which own some of these keys).
    block = mod[mod.index("boot.kernel.sysctl"):]
    block = block[:block.index("};")]
    set_lines = [ln for ln in block.splitlines()
                 if "=" in ln and "lib.mkDefault" not in ln and '"' in ln]
    assert not set_lines, \
        f"hardening sysctls must all be lib.mkDefault (conflict-safe), got: {set_lines}"


def _strip_comments(nix_src):
    # Drop the `# ...` tail of every line so the guards below match CODE, not the
    # explanatory comments (which legitimately name mkForce / allowedTCPPorts when
    # describing what the module deliberately avoids).
    out = []
    for ln in nix_src.splitlines():
        h = ln.find("#")
        out.append(ln if h < 0 else ln[:h])
    return "\n".join(out)


def test_hardening_never_manages_port_list(mod):
    # The hardening layer is ADDITIVE: it must NOT set the firewall port list at
    # all (that belongs to hart-base/hart-firewall). If it never writes the list,
    # it can never strip the shell / SSH / netdiag ports.
    code = _strip_comments(mod)
    assert "allowedTCPPorts =" not in code, \
        "hart-security must not manage networking.firewall.allowedTCPPorts (additive only)"
    assert "allowedUDPPorts =" not in code
    assert "mkForce" not in code, \
        "hart-security must not mkForce any firewall/port value (would risk stripping ports)"


def test_has_port_preservation_assertion(mod):
    # The eval-time tripwire: the shell/backend port + SSH must remain open.
    assert "assertions" in mod
    assert "cfg.ports.backend" in mod
    assert "config.networking.firewall.allowedTCPPorts" in mod
    assert "lib.elem" in mod


# ── (3) OTA note: OS/app security fixes ship over-the-air ──────

def test_documents_ota_delivery(mod):
    low = mod.lower()
    assert "over-the-air" in low or "hart-ota" in low, \
        "module must document that OS/app security fixes arrive via OTA"
    # And it must NOT build a second auto-patcher (parallel update path - Gate 4).
    assert "nixos-rebuild" not in mod, \
        "hart-security must not run its own OS rebuild (OTA owns code patching)"


def test_status_cli_present(mod):
    assert 'writeShellScriptBin "hart-security"' in mod


# ── SAFETY: AI-exclusion zone is never touched ────────────────

@pytest.mark.parametrize("forbidden", [
    "HEVOLVE_MASTER_PRIVATE_KEY",
    "get_master_private_key",
    "sign_child_certificate",
    "MASTER_PUBLIC_KEY_HEX",
    "_FrozenValues",
    "HiveCircuitBreaker",
    "master_private_key",
])
def test_module_never_touches_master_key(mod, forbidden):
    assert forbidden not in mod, \
        f"hart-security must NEVER reference master-key/guardrail material: {forbidden}"


def test_module_does_not_weaken_guardrails(mod):
    low = mod.lower()
    # Sanity: this is a hardening module - it should not disable hardening primitives.
    assert "hive_guardrails" not in low
    assert "security/master_key" not in low


# ── nixosTest is behavioural (boots a VM, proves the live invariants) ──

def test_nixostest_boots_a_vm(nixtest):
    assert "runNixOSTest" in nixtest
    assert "wait_for_unit" in nixtest and "multi-user.target" in nixtest


def test_nixostest_proves_ports_preserved(nixtest):
    # The load-bearing behavioural assertion: shell + netdiag ports survive.
    assert "6777" in nixtest, "test must prove the shell/backend port survives"
    assert "6699" in nixtest, "test must prove the netdiag port survives"
    assert "nft list ruleset" in nixtest


def test_nixostest_proves_clamav_and_sysctls(nixtest):
    assert "clamav-daemon.service" in nixtest
    assert "clamav-freshclam.service" in nixtest
    assert "tcp_syncookies" in nixtest
    assert "hart-security" in nixtest  # the status CLI is exercised
