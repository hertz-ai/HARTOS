"""No caller may append /v1 to a base that already ends in /v1.

THE DEFECT
  security/secret_redactor.py read HEVOLVE_LOCAL_LLM_URL straight from the
  environment and appended '/v1/chat/completions'. The deployed unit sets that
  variable WITH the suffix already on it:

      nixos/modules/hart-backend.nix:62
      HEVOLVE_LOCAL_LLM_URL = "http://127.0.0.1:${cfg.ports.llm}/v1";

  so every PII-detection call went to http://127.0.0.1:808/v1/v1/chat/completions
  and 404'd. Silently: the surrounding `except` swallowed it and detection simply
  returned nothing, so the redactor looked like it was working while doing no
  model-based PII detection at all.

WHY A TEST AND NOT JUST A FIX
  This is a whole CLASS. core/port_registry.get_local_llm_url() deliberately
  returns a '/v1'-suffixed base (documented at core/health_probe.py:109), so
  every consumer that appends a path is a candidate. Several already strip it
  correctly (port_registry:412, llama_scheduler:93, hart_intelligence_entry's
  forwarder); this pins that they keep doing so.

Run:
  pytest tests/unit/test_llm_url_no_doubled_v1.py -v --noconftest
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(src):
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def test_the_redactor_does_not_double_the_v1():
    """THE regression, as a test."""
    code = _strip_comments(_read("security/secret_redactor.py"))
    assert not re.search(r"llm_url\.rstrip\([^)]*\)\s*\}?/v1/chat/completions", code), (
        "secret_redactor appends /v1 to a base that already carries it on the "
        "deployed unit; every call 404s and the except swallows it")
    assert "endswith('/v1')" in code, (
        "the redactor must branch on whether the base already ends in /v1")


def test_the_env_var_really_carries_the_suffix():
    """The fix is only correct because of this. If the unit ever stops setting
    the suffix, the branch still works, but the reasoning in the comment would
    be stale — so pin the fact the fix depends on."""
    nix = _read("nixos/modules/hart-backend.nix")
    assert re.search(r'HEVOLVE_LOCAL_LLM_URL\s*=\s*"[^"]*/v1"', nix), (
        "hart-backend.nix no longer sets HEVOLVE_LOCAL_LLM_URL with a /v1 "
        "suffix; re-check every consumer that branches on it")


def test_known_good_consumers_still_strip_before_appending():
    """Guard the rest of the class from regressing the other way."""
    for rel, marker in (
        ("core/port_registry.py", "removesuffix('/v1')"),
        ("core/llama_scheduler.py", "endswith('/v1')"),
    ):
        code = _strip_comments(_read(rel))
        assert marker in code, (
            "%s no longer strips /v1 before appending a path; that is the "
            "doubled-URL bug in a new place" % rel)
