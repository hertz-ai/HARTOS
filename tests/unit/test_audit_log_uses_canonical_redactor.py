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
