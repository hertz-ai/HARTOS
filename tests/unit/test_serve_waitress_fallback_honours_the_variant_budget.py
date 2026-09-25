"""The Waitress fallback serves with the variant's thread budget, not a hard-coded 50.

hart-backend.nix exports HEVOLVE_WORKER_THREADS per variant and says that export is
what makes ExecStart safe against TasksMax (edge: 64). hart-app ships no hypercorn, so
every OS node takes _serve_app's Waitress fallback, which until 2026-09-24 ignored the
export and asked for 50 handler threads. Measured in CI (nixosTests hart-peer-discovery,
run 35911836772): the edge backend died five times with "can't start new thread" and
"fork rejected by pids controller", then hit its start limit, so the edge variant could
not boot its backend anywhere. These pin the fallback to the budget, and pin the old 50
for a box that sets nothing (the dev checkout and the Nunba bundle), so nothing else
moved.
"""
import os
import sys
from unittest import mock

import pytest

import hart_intelligence_entry as hie


def _run_fallback(monkeypatch, budget):
    """Force the Waitress branch (no hypercorn) and capture what serve() was asked."""
    if budget is None:
        monkeypatch.delenv('HEVOLVE_WORKER_THREADS', raising=False)
    else:
        monkeypatch.setenv('HEVOLVE_WORKER_THREADS', budget)
    # An entry of None in sys.modules makes `from hypercorn.asyncio import serve`
    # raise ImportError, exactly what a hart-app python env without hypercorn does.
    monkeypatch.setitem(sys.modules, 'hypercorn', None)
    monkeypatch.setitem(sys.modules, 'hypercorn.asyncio', None)
    monkeypatch.setitem(sys.modules, 'core.serve', None)
    calls = []
    with mock.patch.object(hie, 'serve', side_effect=lambda *a, **k: calls.append(k)):
        hie._serve_app(object(), '127.0.0.1', 0)
    assert len(calls) == 1, calls
    return calls[0]


def test_the_fallback_serves_with_the_variant_budget(monkeypatch):
    kw = _run_fallback(monkeypatch, '24')
    assert kw['threads'] == 24, kw


def test_a_box_that_sets_no_budget_keeps_the_old_fifty(monkeypatch):
    kw = _run_fallback(monkeypatch, None)
    assert kw['threads'] == 50, kw


def test_a_budget_that_is_not_a_number_falls_back_rather_than_crashing(monkeypatch):
    kw = _run_fallback(monkeypatch, 'many')
    assert kw['threads'] == 50, kw


def test_the_edge_budget_the_unit_exports_fits_under_its_tasks_max():
    """The unit's own numbers: whatever hart-backend.nix hands edge must leave room
    for the import threads under TasksMax=64. Read from the module, not restated."""
    here = os.path.dirname(os.path.abspath(__file__))
    nix = open(os.path.join(here, '..', '..', 'nixos', 'modules', 'hart-backend.nix'),
               encoding='utf-8').read()
    import re
    m = re.search(r'TasksMax\s*=\s*if cfg\.variant == "edge" then (\d+)', nix)
    assert m, 'hart-backend.nix no longer declares a TasksMax'
    tasks_max = int(m.group(1))
    # The export is a multi-line `if variant then "4" else if ... else "50";`
    block = re.search(r'HEVOLVE_WORKER_THREADS\s*=(.*?);', nix, re.S)
    budgets = [int(x) for x in re.findall(r'"(\d+)"', block.group(1))] if block else []
    assert budgets, 'hart-backend.nix no longer exports HEVOLVE_WORKER_THREADS'
    assert min(budgets) < tasks_max, (budgets, tasks_max)
