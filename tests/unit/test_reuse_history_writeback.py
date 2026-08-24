"""#686 — reuse role-group turns must persist to the shared chat history.

Live 2026-08-23 (installed build, user validate-0823b, agent 90916249292):
the 11:43 role-group turn replied "I have saved the codename BLUEFIN6 to my
memory for this conversation", yet simplemem_db/user_validate-0823b/
buffer.json holds only the langchain turns (hi / weather) — the group's
exchange was never written.  At 12:14 the agent truthfully answered
"I don't have access to our past conversations".

Root cause: reuse_recipe seeds the role group FROM the shared buffer
(seed_autogen_from_shared_history, :796) but installs no write-back hook —
that hook exists only on the visual group family (:3059-3120), where
"autogen writes go to the SAME PersistentChatHistory" is implemented
inline.  create_autogen_history_hook's own docstring usage (assigning to
list.append) raises AttributeError on a plain list, which is why the
working install needs a wrapper list.

    python -m pytest tests/unit/test_reuse_history_writeback.py --noconftest -q
"""
import importlib
from pathlib import Path
from types import SimpleNamespace

_REUSE_SRC = (Path(__file__).resolve().parents[2] /
              'reuse_recipe.py').read_text(encoding='utf-8')


def test_install_history_writeback_persists_appends(tmp_path, monkeypatch):
    import integrations.channels.memory.simplemem_langchain as sml
    monkeypatch.setattr(sml, 'SIMPLEMEM_DB_ROOT', str(tmp_path))
    # a fresh instance per user_id — clear any cross-test cache
    if hasattr(sml.SimpleMemChatMemory, '_instances'):
        sml.SimpleMemChatMemory._instances.clear()

    import integrations.channels.memory.shared_history as sh
    importlib.reload(sh)  # rebind after monkeypatch so lazy roots agree

    gc = SimpleNamespace(messages=[{'role': 'user', 'content': 'seeded',
                                    '_from_shared': True}])
    installed = sh.install_history_writeback(gc, 'wbtest-user')
    assert installed is True

    gc.messages.append({'role': 'user', 'content': 'codename BLUEFIN7',
                        'name': 'User'})
    gc.messages.append({'role': 'assistant', 'content': 'TERMINATE'})
    gc.messages.append({'role': 'user', 'content': 'seed echo',
                        '_from_shared': True})

    # The buffer flush is a 150ms coalesced Timer (_FLUSH_DELAY); the next
    # turn reads via a FRESH load_or_create instance, so assert the same
    # disk round-trip the production read performs.
    import time
    time.sleep(0.8)
    hist = sh._get_persistent_history('wbtest-user')
    contents = [m.content for m in hist.messages]
    assert 'codename BLUEFIN7' in contents, (
        "appended group message did not reach PersistentChatHistory — the "
        "write-back is the missing half of the seed/write contract")
    assert 'TERMINATE' not in contents
    assert 'seed echo' not in contents, "_from_shared seeds must not re-write"
    # the raw list still accumulates everything for autogen itself
    assert len(gc.messages) == 4


def test_role_group_installs_writeback():
    """The seeded role group in create_agents_for_role must install the
    canonical write-back — seeding without write-back is a one-way valve
    that loses every reuse conversation (live 11:43 turn)."""
    import re
    m = re.search(
        r"seed_autogen_from_shared_history\(user_id.*?"
        r"return assistant, user_proxy, group_chat, manager, helper, False",
        _REUSE_SRC, re.DOTALL)
    assert m, "seeded role-group region not found in reuse_recipe"
    assert 'install_history_writeback(' in m.group(0), (
        "role group seeds FROM the shared buffer but never writes back — "
        "install the canonical shared_history.install_history_writeback")
