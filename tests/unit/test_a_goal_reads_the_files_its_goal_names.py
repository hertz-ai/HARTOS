"""A goal's own turn reads the files its goal names, from the folder its
config names (config files_root), read-only and inside that folder.

WHAT WENT WRONG (central, 2026-11-20)
  Every lane goal on central carries config.files_root
  '/app/agent_data/lanes', and 30 of the 39 lane definitions tell the agent
  "Facts only from EMAIL_FACTS.md and this goal".  Goal 61e48abc (textile
  compliance) decomposed the step "get_user_uploaded_file: load
  EMAIL_FACTS.md as the permitted commercial fact base" and paused
  needs_help ("the conversation looped without finishing it"): no tool an
  agent is given read a file from disk, and get_user_uploaded_file read
  only a person's uploads.

THE RULE
  - get_user_uploaded_file(name=...) on this node's own goal turn reads
    `name` from that goal's files_root, a page at a time like
    get_data_by_key;
  - ../ and an absolute path outside the folder, and a sibling folder
    sharing its prefix, are refused with a hint;
  - a missing file is refused naming the files that do exist there, and
    the step may finish without it (RETRY_FINISH_WITHOUT);
  - a goal with no files_root is refused with a hint;
  - a goal made by an account this node does not hold opens nothing: any
    signed-in account writes a goal's config (POST /api/goals);
  - a person's turn, and a turn that only names a goal's id, are as before.

    python -m pytest tests/unit/test_a_goal_reads_the_files_its_goal_names.py -q
"""
from unittest import mock

import pytest

from core.constants import tool_reply_failed, tool_reply_is_step_work
from core.tool_refusal import refusal_lets_step_finish

TOOL = 'get_user_uploaded_file'
OWNER = 'owner-lanes'
STRANGER = 'someone-signed-in'
FACTS = '# Email facts\nHevolve is based in Chennai.\nPilot price: on request.\n'


@pytest.fixture
def lanes(tmp_path):
    """central's lane tree in miniature, a sibling folder that only shares
    its prefix, and a file beside both."""
    root = tmp_path / 'lanes'
    (root / 'trade' / 'catalogs').mkdir(parents=True)
    (root / 'EMAIL_FACTS.md').write_text(FACTS, encoding='utf-8')
    (root / 'trade' / 'catalogs' / 'textile_uk_compliance.md').write_text(
        'UK textile labelling rules', encoding='utf-8')
    (root / 'outreach_log.csv').write_text('company,stage\n', encoding='utf-8')
    sibling = tmp_path / 'lanes_private'
    sibling.mkdir()
    (sibling / 'secret.txt').write_text('SIBLING SECRET', encoding='utf-8')
    (tmp_path / 'outside.txt').write_text('OUTSIDE SECRET', encoding='utf-8')
    return root


@pytest.fixture
def goals(lanes, monkeypatch):
    """Goal rows in the real social DB; who this node holds is the boundary
    (ThoughtExperimentService.node_held_ids: the owner, the system account)."""
    from integrations.social.models import AgentGoal, Base, db_session, get_engine
    from integrations.social.thought_experiment_service import (
        ThoughtExperimentService)
    rows = {
        'g-textile': ('Textile UK compliance', {'files_root': str(lanes)}, OWNER),
        'g-nofolder': ('A goal with no folder', {}, OWNER),
        'g-stranger': ('A stranger names the folder', {'files_root': str(lanes)},
                       STRANGER),
        'g-relative': ('A relative folder', {'files_root': 'agent_data/lanes'}, OWNER),
    }
    Base.metadata.create_all(get_engine())
    with db_session() as db:
        for gid, (title, cfg, creator) in rows.items():
            db.merge(AgentGoal(id=gid, goal_type='outreach', title=title,
                               config_json=dict(cfg), created_by=creator))
    monkeypatch.setattr(ThoughtExperimentService, 'node_held_ids',
                        staticmethod(lambda db, ids: {i for i in ids if i == OWNER}))
    yield rows
    with db_session() as db:
        for gid in rows:
            db.query(AgentGoal).filter_by(id=gid).delete()


def _tool(monkeypatch, recent_file_id=None):
    from core import agent_tools
    import core.chat_attachments as ca
    monkeypatch.setattr(ca, 'latest_inbound', lambda *a, **k: [])
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id=1, prompt_id='p1', agent_data={}, user_prompt='s1',
               request_id_list={}, recent_file_id=recent_file_id or {},
               memory_graph=None, simplemem_store=None,
               helper_fun=mock.Mock(), send_message_to_user1=mock.Mock(),
               retrieve_json=lambda v: v)
    return {n: f for n, _, f in agent_tools.build_core_tool_closures(ctx)}[TOOL]


def _own_turn(goal_id, fn, **kw):
    """``fn(**kw)`` in ``goal_id``'s own turn, bound and marked the way
    dispatch.local_chat_dispatch runs it."""
    from core.chat_client import own_daemon_turn
    from core.llm_outbound_logger import request_id_context
    rid = f'daemon_{goal_id}'
    with request_id_context(rid), own_daemon_turn(rid):
        return fn(**kw)


def _named_turn(turn_id, fn, **kw):
    """``fn(**kw)`` in a turn whose id is ``turn_id`` and that this node's
    dispatch did not start (a person's, or a client naming a goal)."""
    from core.llm_outbound_logger import request_id_context
    with request_id_context(turn_id):
        return fn(**kw)


def _refused(out):
    assert out.startswith(f'Error: the {TOOL} call could not be done:'), out
    assert tool_reply_failed(out), out
    return out


# ── a contained read ───────────────────────────────────────────────

def test_the_goal_reads_email_facts_from_its_folder(goals, monkeypatch):
    out = _own_turn('g-textile', _tool(monkeypatch), name='EMAIL_FACTS.md')
    assert out == FACTS
    assert not tool_reply_failed(out)


def test_a_file_in_a_subfolder_and_the_absolute_path_the_config_names(
        goals, lanes, monkeypatch):
    tool = _tool(monkeypatch)
    rel = _own_turn('g-textile', tool, name='trade/catalogs/textile_uk_compliance.md')
    absolute = _own_turn('g-textile', tool, name=str(
        lanes / 'trade' / 'catalogs' / 'textile_uk_compliance.md'))
    assert rel == absolute == 'UK textile labelling rules'


def test_a_long_file_is_read_a_page_at_a_time(goals, lanes, monkeypatch):
    from core.constants import TOOL_OBSERVATION_MAX_CHARS as page
    body = ''.join(f'line {i:05d}\n' for i in range(page // 5))
    (lanes / 'long.md').write_text(body, encoding='utf-8')
    tool = _tool(monkeypatch)
    first = _own_turn('g-textile', tool, name='long.md')
    assert first.startswith(body[:page])
    assert f"name='long.md' and offset={page}" in first, first[-200:]
    second = _own_turn('g-textile', tool, name='long.md', offset=page)
    assert second.startswith(body[page:page + 50])


# ── escapes ────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['../outside.txt', '../lanes_private/secret.txt',
                                  'trade/../../outside.txt'])
def test_a_path_out_of_the_folder_is_refused(goals, monkeypatch, name):
    out = _refused(_own_turn('g-textile', _tool(monkeypatch), name=name))
    assert 'SECRET' not in out
    assert 'outside' in out and 'EMAIL_FACTS.md' in out, out


def test_an_absolute_path_outside_and_a_prefix_sibling_are_refused(
        goals, lanes, monkeypatch):
    tool = _tool(monkeypatch)
    for path in (lanes.parent / 'outside.txt',
                 lanes.parent / 'lanes_private' / 'secret.txt'):
        out = _refused(_own_turn('g-textile', tool, name=str(path)))
        assert 'SECRET' not in out, out


# ── a missing file, a folder, a goal with no folder ───────────────

def test_a_missing_file_names_what_is_there_and_lets_the_step_finish(
        goals, monkeypatch):
    out = _refused(_own_turn('g-textile', _tool(monkeypatch), name='EMAIL_FACT.md'))
    assert 'EMAIL_FACTS.md' in out and 'outreach_log.csv' in out and 'trade/' in out, out
    assert refusal_lets_step_finish(out, TOOL) and tool_reply_is_step_work(out, TOOL), out


def test_a_missing_file_in_a_subfolder_names_that_subfolders_files(goals, monkeypatch):
    out = _refused(_own_turn('g-textile', _tool(monkeypatch),
                             name='trade/catalogs/textile_eu.md'))
    assert 'trade/catalogs/textile_uk_compliance.md' in out, out
    assert refusal_lets_step_finish(out, TOOL), out


def test_an_empty_file_is_said_so_and_lets_the_step_finish(goals, lanes, monkeypatch):
    (lanes / 'blank.md').write_text('  \n', encoding='utf-8')
    out = _refused(_own_turn('g-textile', _tool(monkeypatch), name='blank.md'))
    assert 'empty' in out and refusal_lets_step_finish(out, TOOL), out


def test_a_goal_with_no_files_root_is_told_why(goals, monkeypatch):
    out = _refused(_own_turn('g-nofolder', _tool(monkeypatch), name='EMAIL_FACTS.md'))
    assert 'files_root' in out, out
    assert refusal_lets_step_finish(out, TOOL), out


def test_a_relative_files_root_is_told_why(goals, monkeypatch):
    out = _refused(_own_turn('g-relative', _tool(monkeypatch), name='EMAIL_FACTS.md'))
    assert 'files_root' in out and 'absolute' in out, out


def test_a_goal_a_stranger_made_opens_nothing(goals, monkeypatch):
    out = _refused(_own_turn('g-stranger', _tool(monkeypatch), name='EMAIL_FACTS.md'))
    assert 'Hevolve is based' not in out
    assert refusal_lets_step_finish(out, TOOL), out


def test_no_name_on_a_goal_with_a_folder_says_which_files_it_can_read(
        goals, monkeypatch):
    out = _refused(_own_turn('g-textile', _tool(monkeypatch)))
    assert 'EMAIL_FACTS.md' in out and 'name' in out, out


# ── turns that are not the goal's own ──────────────────────────────

def test_a_persons_turn_is_unchanged(goals, monkeypatch):
    tool = _tool(monkeypatch)
    assert _named_turn('msg_1794670427623', tool) == 'No file uploaded from user'
    # a name does not open the lane folder on her turn
    assert _named_turn('msg_1794670427623', tool,
                       name='EMAIL_FACTS.md') == 'No file uploaded from user'
    up = _tool(monkeypatch, recent_file_id={1: 'file-77'})
    assert 'file-77' in _named_turn('msg_1794670427623', up, name='EMAIL_FACTS.md')


def test_a_turn_that_only_names_the_goal_reads_nothing(goals, monkeypatch):
    out = _named_turn('daemon_g-textile', _tool(monkeypatch), name='EMAIL_FACTS.md')
    assert 'Hevolve is based' not in out
    assert refusal_lets_step_finish(out, TOOL), out
