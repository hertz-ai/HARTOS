"""Parallel-path fix #5: ``security/audit_log.py`` carried its OWN copy of the
vendor secret-redaction regexes and they DRIFTED from the canonical
``security/secret_redactor.py`` — its Google pattern demanded an ``AIzaSy``
prefix (``AIzaSy…{33}``) and its OpenAI pattern was ``sk-[alnum]{20,}``, so Google
keys not starting ``Sy`` and every ``sk-proj-`` / ``sk-ant-`` key LEAKED into the
audit log.

These tests prove ``audit_log`` now delegates to the canonical redactor: the
previously-leaking keys are redacted, Groq + bare-hex still are, and the drifted
local vendor pattern list is gone.
"""
from security.audit_log import SensitiveFilter

redact = SensitiveFilter._redact

# Fake but structurally-valid secrets (NOT real credentials).
GOOGLE_NON_SY = "AIzaB" + "0" * 34          # AIza + 35 chars, 5th char != 'S'
OPENAI_PROJ = "sk-proj-" + "a" * 40
ANTHROPIC = "sk-ant-" + "b" * 40
GROQ = "gsk_" + "c" * 24
BARE_HEX = "a1b2c3d4" * 6                    # 48 lowercase-hex chars, no keyword


def test_google_key_without_Sy_prefix_now_redacted():
    # The exact leak the old AIzaSy…{33} pattern missed.
    assert GOOGLE_NON_SY not in redact(f"token={GOOGLE_NON_SY} done")


def test_openai_proj_key_now_redacted():
    assert OPENAI_PROJ not in redact(f"key {OPENAI_PROJ}")


def test_anthropic_key_now_redacted():
    assert ANTHROPIC not in redact(f"key {ANTHROPIC}")


def test_groq_key_still_redacted():
    # Groq was the one pattern the canonical lacked; this fix adds it there.
    assert GROQ not in redact(f"key {GROQ}")


def test_bare_hex_token_still_redacted():
    # Audit-log-specific supplemental (kept local on purpose).
    assert BARE_HEX not in redact(f"sig {BARE_HEX}")


def test_drifted_local_vendor_pattern_copy_is_gone():
    import security.audit_log as m
    assert not hasattr(m, '_REDACTION_PATTERNS')


# ── The delegation must FAIL LOUDLY, not silently ──────────────────────────
# If the canonical redactor ever breaks, the only remaining coverage is the bare
# long-hex pattern — every vendor key would flow into the audit log verbatim.
# That degradation used to be swallowed by `except Exception: pass`.

def _break_redactor(monkeypatch):
    """Make `from security.secret_redactor import redact_secrets` blow up."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == 'security.secret_redactor':
            raise ImportError("simulated canonical-redactor failure")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', boom)


def _reset_state():
    import security.audit_log as m
    m._redactor_failure = None
    m._redactor_failure_count = 0


def test_redactor_failure_is_recorded_not_swallowed(monkeypatch, capsys):
    import security.audit_log as m
    _reset_state()
    try:
        _break_redactor(monkeypatch)
        out = m.SensitiveFilter._redact("key sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA")

        healthy, err, count = m.canonical_redactor_status()
        assert healthy is False, (
            "the canonical redactor failed and the module still reports healthy — "
            "vendor keys are silently no longer being scrubbed")
        assert 'ImportError' in (err or ''), err
        assert count >= 1
        # It must still not RAISE — a filter that raises drops the log line.
        assert isinstance(out, str)
    finally:
        _reset_state()


def test_redactor_failure_announces_once_on_stderr(monkeypatch, capsys):
    """It cannot use logging to report a logging fault — emitting a record from
    inside a Filter re-enters the filters and recurses. So the one-time signal
    goes to stderr."""
    import security.audit_log as m
    _reset_state()
    try:
        _break_redactor(monkeypatch)
        for _ in range(5):
            m.SensitiveFilter._redact("noise")
        err = capsys.readouterr().err
        assert err.count("CANONICAL REDACTOR FAILED") == 1, (
            "expected exactly one announcement across repeated failures, got:\n%s" % err)
        assert m.canonical_redactor_status()[2] == 5, "every failure must still be counted"
    finally:
        _reset_state()


def test_healthy_by_default():
    import security.audit_log as m
    _reset_state()
    m.SensitiveFilter._redact("nothing secret here")
    assert m.canonical_redactor_status() == (True, None, 0)
