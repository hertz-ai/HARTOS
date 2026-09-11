"""The wire budget must MEASURE the server's n_ctx, never declare it.

THE DEFECT, measured live 2026-09-11 on the installed build.  The walk of
agent 18163818525 (and 1923323102, and every agent walked after ~21:08) died
here:

    llm_outbound  ERROR  wire-trim: the TOOL SCHEMA alone is 8482 tokens
                         against an n_ctx of 12288 (70 tool(s))
    hart_intelligence_entry ERROR  robust completion-advance FAILED for
                         action 1 — the pipeline did not advance for session
                         6c2dc0fc-...-f7466ff63f29_18163818525:
                         Error code: 400 - {'error': {'code': 400,
                         'message': 'request (11817 tokens) exceeds the
                         available context size (8192 tokens)',
                         'type': 'exceed_context_size_error',
                         'n_prompt_tokens': 11817, 'n_ctx': 8192}}

The wire layer budgeted against 12288.  The server was running 8192 and said
so in its own rejection.  Every request was over-budgeted by 4,096 tokens,
so the "zero-tolerance context overflow" guard passed bodies the server then
refused, and the reuse pointer never advanced.  Live `/props` on the same
box: {"n_ctx":8192, "total_slots":1}.

WHY IT WAS INVISIBLE.  `_get_budget_per_slot()` reads
``HEVOLVE_LLAMA_CTX_SIZE`` and falls back to
``core.constants.LLAMA_CTX_SIZE_DEFAULT`` (12288).  Measured across BOTH
repos, that env var has exactly three references: the comment at
core/constants.py:71 and the two lines inside the function itself.  NOTHING
EVER SETS IT.  The constant therefore always wins, and constants.py:71's
claim that it "must match the --ctx-size cmdline" is a declaration with no
enforcement — the shape recorded in memory/feedback_declaration_is_not_a_guard.md.

WHAT THIS GUARD REQUIRES.  That the budget come from the running server when
the server can be reached, and fall back to the constant when it cannot, so
an unreachable server is exactly as safe as today.

THE D53 TRAP, pinned by the third test.  This code path has been fixed wrong
before: #818/D53 sized n_ctx from a 117-second-old VRAM memo read across a
llama-server teardown and pinned 4096 for a whole session, which is why
CREATE was dead.  A naive TTL cache here re-creates that bug exactly.  The
third test fails if a memo makes the reader keep returning a value the
server no longer reports.

RED BEFORE GREEN: against HEAD `_get_budget_per_slot` never consults the
server, so test 1 returns 12288 instead of 8192 and test 3 cannot even find
a seam to vary.

    python -m pytest tests/unit/test_wire_budget_reads_the_live_server.py --noconftest -q
"""
import importlib
import os

import pytest

MOD = 'core.llm_outbound_logger'


@pytest.fixture
def wire(monkeypatch):
    """Import the wire module with a clean env (no operator override)."""
    monkeypatch.delenv('HEVOLVE_LLAMA_CTX_SIZE', raising=False)
    monkeypatch.delenv('HEVOLVE_LLAMA_SLOTS', raising=False)
    mod = importlib.import_module(MOD)
    importlib.reload(mod)
    return mod


def test_budget_reflects_the_live_server_not_a_constant(wire, monkeypatch):
    """The measured case: server at 8192 while the constant says 12288.

    Live 2026-09-11 — the 4,096-token gap between these two numbers is the
    whole defect; it is what let an 11,817-token body through a guard that
    believed it had 12,288 to spend.
    """
    monkeypatch.setattr(wire, '_live_ctx_geometry',
                        lambda: (8192, 1), raising=False)
    assert wire._get_budget_per_slot() == 8192, (
        "_get_budget_per_slot() must report the n_ctx the SERVER is running "
        "(8192), not core.constants.LLAMA_CTX_SIZE_DEFAULT (12288). Nothing "
        "in either repo ever sets HEVOLVE_LLAMA_CTX_SIZE, so the constant "
        "always wins and every request is over-budgeted by 4096 tokens.")


def test_the_server_value_is_not_divided_a_second_time(wire, monkeypatch):
    """`/props` already reports the PER-SLOT limit — do not partition again.

    Measured on this box: `/props` carries
    ``default_generation_settings.n_ctx = 8192`` and ``total_slots = 1``, and
    the server's own rejection quotes the same 8192 as the ceiling for ONE
    request (``'n_ctx': 8192, 'n_prompt_tokens': 11817``).  So the number the
    server hands back is the budget a single request may spend, already
    divided across slots by llama-server itself.

    The env path divides by ``HEVOLVE_LLAMA_SLOTS`` because its constant is a
    TOTAL.  Carrying that division onto the probe would halve a budget that
    was never doubled — a fresh defect wearing the fix's clothes.

    NOT VERIFIED HERE: the multi-slot case cannot be measured on this box
    (total_slots is 1).  This test pins the behaviour that follows from what
    WAS measured; the operator override stays available for a multi-slot
    deployment that disagrees.
    """
    monkeypatch.setattr(wire, '_live_ctx_geometry',
                        lambda: (8192, 2), raising=False)
    assert wire._get_budget_per_slot() == 8192, (
        'the probe value is already per-slot; dividing it by total_slots '
        'again under-budgets every request on a multi-slot server')


def test_no_memo_pins_a_value_the_server_no_longer_reports(wire, monkeypatch):
    """Guards the SHAPE of the fix against the D53 regression.

    #818/D53: n_ctx was taken from a 117s-old memo read across a llama-server
    teardown and pinned 4096 for an entire session. A TTL does not save you —
    the stale read is inside the window. llama-server respawns (VRAM tier
    changes, model switches, watchdog restarts) and the budget must follow it
    within the same process.
    """
    seen = iter([(8192, 1), (12288, 1)])
    monkeypatch.setattr(wire, '_live_ctx_geometry',
                        lambda: next(seen), raising=False)
    first = wire._get_budget_per_slot()
    second = wire._get_budget_per_slot()
    assert (first, second) == (8192, 12288), (
        'the reader memoised the first geometry (%r then %r) — a llama-server '
        'respawn changes n_ctx and a pinned value is exactly the D53 defect '
        'that killed CREATE for a whole session.' % (first, second))


def test_unreachable_server_falls_back_to_todays_behaviour(wire, monkeypatch):
    """No regression: when /props cannot be read, behave exactly as before.

    The fallback is what makes this safe to land on the chat hot path — a
    down or starting llama-server must not make the budget zero or raise.
    """
    from core.constants import LLAMA_CTX_SIZE_DEFAULT
    monkeypatch.setattr(wire, '_live_ctx_geometry',
                        lambda: None, raising=False)
    assert wire._get_budget_per_slot() == LLAMA_CTX_SIZE_DEFAULT


def test_operator_override_still_wins(wire, monkeypatch):
    """HEVOLVE_LLAMA_CTX_SIZE is documented as an override; keep it one.

    Nothing sets it today, but constants.py:71 advertises it and an operator
    pinning a value must not be silently overruled by the probe.
    """
    monkeypatch.setenv('HEVOLVE_LLAMA_CTX_SIZE', '4096')
    monkeypatch.setattr(wire, '_live_ctx_geometry',
                        lambda: (8192, 1), raising=False)
    assert wire._get_budget_per_slot() == 4096
