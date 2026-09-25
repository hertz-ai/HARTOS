"""The input-alive marker is written by Rust and read by three other languages.

compositor/src/main.rs owns the default path and the heartbeat interval. The
session supervisor (Nix) reads the file by EXISTENCE to decide whether a painted
tier is input-dead; the boot log (Nix) prints its mtime; and the resource
governor's Linux idle backend (Python, stream S1) reads the mtime as "when a
person last touched this box", accurate to one heartbeat. None of those readers
can be checked by a Rust unit test, and a Rust constant that drifts from the
Nix path is exactly the class of bug that only ever shows up on a booted box.

So this pins the spellings to each other, reading each bound from the file that
owns it rather than restating a number here.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN_RS = os.path.join(REPO, "compositor", "src", "main.rs")
COMP_CORE_RS = os.path.join(REPO, "compositor", "src", "comp_core.rs")
SUPERVISOR = os.path.join(REPO, "nixos", "modules", "hart-session-supervisor.nix")
BOOT_LOG = os.path.join(REPO, "nixos", "modules", "hart-boot-log.nix")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rust_default_path():
    m = re.search(r'const INPUT_ALIVE_MARKER_DEFAULT: &str = "([^"]+)";', _read(MAIN_RS))
    assert m, "main.rs no longer declares INPUT_ALIVE_MARKER_DEFAULT"
    return m.group(1)


def rust_heartbeat_seconds():
    src = _read(MAIN_RS)
    m = re.search(r"const INPUT_ALIVE_HEARTBEAT: Duration = Duration::from_(secs|millis)\((\d+)\);", src)
    assert m, "main.rs no longer declares INPUT_ALIVE_HEARTBEAT as a Duration literal"
    n = int(m.group(2))
    return n if m.group(1) == "secs" else n / 1000.0


def test_the_rust_default_path_is_the_path_the_supervisor_and_boot_log_read():
    sup = _read(SUPERVISOR)
    run_dir = re.search(r'sessionRunDir\s*=\s*"([^"]+)"', sup)
    assert run_dir, "supervisor no longer names sessionRunDir"
    flag = re.search(r'inputAliveFlag\s*=\s*"\$\{sessionRunDir\}/([^"]+)"', sup)
    assert flag, "supervisor no longer derives inputAliveFlag from sessionRunDir"
    supervisor_path = run_dir.group(1) + "/" + flag.group(1)

    boot = re.search(r'inputAliveFlag\s*=\s*"([^"]+)"', _read(BOOT_LOG))
    assert boot, "boot log no longer names inputAliveFlag"

    assert rust_default_path() == supervisor_path, (
        "the compositor writes %s but the supervisor waits on %s"
        % (rust_default_path(), supervisor_path))
    assert rust_default_path() == boot.group(1), (
        "the compositor writes %s but the boot log reads %s"
        % (rust_default_path(), boot.group(1)))


def test_the_compositor_honours_the_override_the_supervisor_exports():
    # The supervisor exports HART_INPUT_ALIVE_FLAG next to HART_SHELL_READY_FLAG so
    # writer and reader share one path. Before the heartbeat landed the compositor
    # ignored it and wrote a hardcoded path; this keeps that from coming back.
    assert 'export HART_INPUT_ALIVE_FLAG="$INPUT_ALIVE"' in _read(SUPERVISOR)
    assert 'std::env::var("HART_INPUT_ALIVE_FLAG")' in _read(MAIN_RS)
    assert "crate::input_alive_marker_path()" in _read(COMP_CORE_RS), (
        "note_input_alive must resolve its path through the shared resolver")


def test_the_heartbeat_is_a_usable_idle_grain_for_the_governor():
    # The governor reads the marker's mtime as idle time. A heartbeat coarser than its
    # own tick would make "active" indistinguishable from "idle for a tick"; finer
    # than 100 ms would put a tmpfs write inside a 1 kHz mouse's cadence. Read from
    # the Rust, not restated: a retune there is caught here, and a reader who needs
    # the grain (S1's _get_idle_ms_linux) can import this helper.
    seconds = rust_heartbeat_seconds()
    assert 0.1 <= seconds <= 10, "INPUT_ALIVE_HEARTBEAT=%ss is outside the usable band" % seconds


def test_the_journal_line_is_emitted_only_on_the_first_write():
    # The once-only line is what the boot log and every real-hardware verification row
    # grep for. The heartbeat must never repeat it: only the First arm may log.
    src = _read(COMP_CORE_RS)
    fn = re.search(r"fn note_input_alive\(\) \{(.*?)\n\}", src, re.S)
    assert fn, "comp_core.rs no longer has note_input_alive"
    body = fn.group(1)
    assert body.count("#134 liveness beacon") == 1
    first_arm = re.search(r"InputAliveWrite::First => \{(.*?)\}", body, re.S)
    assert first_arm and "#134 liveness beacon" in first_arm.group(1), (
        "the liveness line must live in the First arm and nowhere else")
    touch_arm = re.search(r"InputAliveWrite::Touch => \{(.*?)\}", body, re.S)
    assert touch_arm and "info!" not in touch_arm.group(1), "a heartbeat touch must not journal"
