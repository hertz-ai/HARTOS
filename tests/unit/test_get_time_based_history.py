"""#121 — get_time_based_history is unified onto SimpleMem/ConversationEntry
(the dead-Zep fork is gone). Behavioural: mock the DB + SimpleMem boundaries,
call the REAL helper.get_time_based_history, and assert it does the
ConversationEntry date-range query for a time window / the SimpleMem semantic
search for a bare query — and never instantiates ZepMemory. No grep tests.
"""
import json
import os
import sys
import types
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import helper  # noqa: E402


class _Col:
    """Stand-in for a SQLAlchemy Column: comparisons/desc return placeholders
    the fake query ignores (instead of raising on None >= datetime)."""
    def __ge__(self, o): return ('ge', o)
    def __le__(self, o): return ('le', o)
    def __eq__(self, o): return ('eq', o)
    def desc(self): return self


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def limit(self, n): return self
    def all(self): return self._rows


class _FakeDB:
    def __init__(self, rows): self._rows = rows; self.closed = False
    def query(self, model): return _FakeQuery(self._rows)
    def close(self): self.closed = True


class _Row:
    def __init__(self, content, created_at):
        self.content = content
        self.role = 'user'
        self.created_at = created_at
        self.channel_type = 'chat'


def _inject(mods):
    saved = {}
    for name, mod in mods.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved


def _restore(saved):
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _zep_landmine():
    """A fake langchain_classic.memory whose ZepMemory raises — proves the code
    path never reaches Zep."""
    m = types.ModuleType('langchain_classic.memory')

    class ZepMemory:
        def __init__(self, *a, **k):
            raise AssertionError("ZepMemory must never be instantiated (#121)")
    m.ZepMemory = ZepMemory
    return m


def test_date_range_queries_conversation_entry():
    rows = [_Row('we discussed the roadmap', datetime(2026, 5, 22, 10, 0))]
    models = types.ModuleType('integrations.social.models')
    models.get_db = lambda: _FakeDB(rows)
    mlocal = types.ModuleType('integrations.social._models_local')
    mlocal.ConversationEntry = type('CE', (), {'user_id': _Col(), 'created_at': _Col()})
    saved = _inject({
        'integrations.social.models': models,
        'integrations.social._models_local': mlocal,
        'langchain_classic.memory': _zep_landmine(),
    })
    try:
        out = json.loads(helper.get_time_based_history(
            'roadmap', 'user_5', '2026-05-22', '2026-05-23'))
        assert 'res_in_filter' in out, out
        assert len(out['res_in_filter']) == 1
        assert 'roadmap' in out['res_in_filter'][0]['message']['content']
        # timestamp must ride along with each returned turn
        assert out['res_in_filter'][0].get('created_at'), 'created_at must be attached'
    finally:
        _restore(saved)


def test_bare_query_falls_back_to_simplemem():
    class _Mem:
        def semantic_search(self, prompt):
            return [{'content': 'older note about the migration plan'}]

    class _SMC:
        @staticmethod
        def load_or_create(uid):
            return _Mem()

    smc = types.ModuleType('integrations.channels.memory.simplemem_langchain')
    smc.SimpleMemChatMemory = _SMC
    saved = _inject({
        'integrations.channels.memory.simplemem_langchain': smc,
        'langchain_classic.memory': _zep_landmine(),
    })
    try:
        out = json.loads(helper.get_time_based_history('migration', 'user_5', '', ''))
        assert out.get('res_in_filter'), out
        assert 'migration plan' in out['res_in_filter'][0]['message']['content']
    finally:
        _restore(saved)


def test_bad_session_id_is_safe():
    out = json.loads(helper.get_time_based_history('x', 'not-a-user', '', ''))
    assert isinstance(out, dict)  # no crash, returns a JSON envelope


# ── resolve_recall_window: the deterministic window math (pure, no DB) ────────

def test_window_single_date_pads_two_days():
    from datetime import datetime
    lo, hi = helper.resolve_recall_window('2026-05-22', '2026-05-22')
    assert lo == datetime(2026, 5, 20, 0, 0, 0)                 # -2 days
    assert hi == datetime(2026, 5, 24, 23, 59, 59, 999999)      # +2 days, end-of-day


def test_window_one_sided_is_centered_not_since():
    # one-sided used to mean "everything since" (returned newest-50); now it is a
    # padded window centred on the date — what "15 days back" actually means.
    from datetime import datetime
    lo, hi = helper.resolve_recall_window('2026-05-22', '')
    assert (lo, hi) == (datetime(2026, 5, 20, 0, 0, 0),
                        datetime(2026, 5, 24, 23, 59, 59, 999999))


def test_window_bare_two_sided_range_covers_full_days():
    from datetime import datetime
    lo, hi = helper.resolve_recall_window('2026-05-22', '2026-05-25')
    assert lo == datetime(2026, 5, 22, 0, 0, 0)                 # bare start -> day start
    assert hi == datetime(2026, 5, 25, 23, 59, 59, 999999)      # bare end -> day end


def test_window_none_for_dateless():
    assert helper.resolve_recall_window('', '') is None
    assert helper.resolve_recall_window(None, None) is None
    assert helper.resolve_recall_window('garbage', 'nonsense') is None


def test_window_explicit_timestamps_not_padded():
    from datetime import datetime
    lo, hi = helper.resolve_recall_window('2026-05-22T09:00:00', '2026-05-22T17:00:00')
    assert lo == datetime(2026, 5, 22, 9, 0, 0)
    assert hi == datetime(2026, 5, 22, 17, 0, 0)


def test_window_inverted_range_is_ordered():
    lo, hi = helper.resolve_recall_window('2026-05-25', '2026-05-20')
    assert lo < hi


if __name__ == '__main__':
    test_date_range_queries_conversation_entry(); print('PASS date-range -> ConversationEntry (+created_at)')
    test_bare_query_falls_back_to_simplemem(); print('PASS bare-query -> SimpleMem')
    test_bad_session_id_is_safe(); print('PASS bad-session-id safe')
    test_window_single_date_pads_two_days(); print('PASS single date -> ±2d window')
    test_window_one_sided_is_centered_not_since(); print('PASS one-sided -> centred window')
    test_window_bare_two_sided_range_covers_full_days(); print('PASS bare range -> full days')
    test_window_none_for_dateless(); print('PASS dateless -> None (semantic)')
    test_window_explicit_timestamps_not_padded(); print('PASS explicit timestamps honoured')
    test_window_inverted_range_is_ordered(); print('PASS inverted range ordered')
    print('OK 9/9')
