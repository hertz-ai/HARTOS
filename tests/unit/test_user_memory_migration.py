"""#117: guest->account memory migration. Behavioural — mock the DB + SimpleMem
boundaries, call the REAL migrate_user_memory / is_claimable_guest, and assert the
non-destructive re-key, the buffer merge + source-clear (idempotency), and that
the claimability guard fails CLOSED (an account / an error is never claimable).
No grep tests.
"""
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.user_memory_migration as umm  # noqa: E402  (top imports are stdlib only)


class _Col:
    def __eq__(self, o): return ('eq', o)
    def __hash__(self): return 1


class _Query:
    def __init__(self, rec): self._rec = rec
    def filter(self, *a, **k): self._rec['filtered'] = True; return self
    def update(self, values, synchronize_session=None):
        self._rec['update'] = values
        return self._rec.get('n', 0)


class _DB:
    def __init__(self, rec): self._rec = rec; self.committed = False; self.closed = False
    def query(self, model): return _Query(self._rec)
    def commit(self): self.committed = True
    def close(self): self.closed = True


class _FakeChat:
    def __init__(self, messages):
        self.messages = list(messages); self.added = []; self.cleared = False
    def add_messages(self, msgs): self.added.extend(msgs)
    def clear(self): self.cleared = True


class _FakeMem:
    def __init__(self, messages): self.chat_memory = _FakeChat(messages)


class _DBSession:
    def __init__(self, user): self._user = user
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def query(self, model): return self
    def filter(self, *a, **k): return self
    def first(self): return self._user


def _inject(mods):
    saved = {}
    for n, m in mods.items():
        saved[n] = sys.modules.get(n); sys.modules[n] = m
    return saved


def _restore(saved):
    for n, m in saved.items():
        if m is None: sys.modules.pop(n, None)
        else: sys.modules[n] = m


def _models(get_db_fn=None, db_session_fn=None, with_user=False):
    m = types.ModuleType('integrations.social.models')
    if get_db_fn: m.get_db = get_db_fn
    if db_session_fn: m.db_session = db_session_fn
    m.User = type('User', (), {'id': _Col()})
    return m


def _models_local():
    m = types.ModuleType('integrations.social._models_local')
    m.ConversationEntry = type('CE', (), {'user_id': _Col()})
    return m


def _simplemem(mems):
    m = types.ModuleType('integrations.channels.memory.simplemem_langchain')

    class _SMC:
        @staticmethod
        def load_or_create(uid, prompt_id=None):
            return mems[str(uid)]
    m.SimpleMemChatMemory = _SMC
    return m


def test_rekeys_conversation_entries_and_merges_buffer():
    rec = {'n': 4}
    guest = _FakeMem(['m1', 'm2']); acct = _FakeMem([])
    saved = _inject({
        'integrations.social.models': _models(get_db_fn=lambda: _DB(rec)),
        'integrations.social._models_local': _models_local(),
        'integrations.channels.memory.simplemem_langchain':
            _simplemem({'guest-uuid': guest, '10202': acct}),
    })
    try:
        out = umm.migrate_user_memory('guest-uuid', '10202')
        assert out['conversation_entries'] == 4          # rows re-keyed (non-destructive)
        assert rec.get('update') is not None
        assert out['buffer_messages'] == 2
        assert acct.chat_memory.added == ['m1', 'm2']     # merged onto the account
        assert guest.chat_memory.cleared is True          # source cleared -> idempotent
    finally:
        _restore(saved)


def test_noop_when_same_id():
    # Must not even touch the DB when from == to (no get_db injected -> would
    # raise if it tried).
    assert umm.migrate_user_memory('x', 'x') == {'conversation_entries': 0, 'buffer_messages': 0}


def test_is_claimable_true_for_anonymous_guest():
    saved = _inject({'integrations.social.models':
                     _models(db_session_fn=lambda commit=False: _DBSession(None))})
    try:
        assert umm.is_claimable_guest('guest-uuid') is True      # no account row
    finally:
        _restore(saved)


def test_is_claimable_false_for_real_account():
    user = type('U', (), {'email': 'a@b.com'})()
    saved = _inject({'integrations.social.models':
                     _models(db_session_fn=lambda commit=False: _DBSession(user))})
    try:
        assert umm.is_claimable_guest('10202') is False          # has an email -> account
    finally:
        _restore(saved)


def test_is_claimable_fails_closed_on_error():
    def _boom(commit=False): raise RuntimeError('db down')
    saved = _inject({'integrations.social.models': _models(db_session_fn=_boom)})
    try:
        assert umm.is_claimable_guest('x') is False              # error -> deny
    finally:
        _restore(saved)
