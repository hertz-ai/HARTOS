"""P3 (#688) — the chat agent must be able to enumerate agents.

Live 2026-08-23 15:02 (installed build): asked "list the agents you have
available", the model answered "I am Qwen3.5... I do not have separate
agents" — a fabricated denial, because the enumeration implementation
(integrations/mcp/_tool_impls.list_agents — 4 expert + 2,633 dynamic
recipes) is wired only to the MCP bridge, and NO agent-listing tool is
registered in the chat-facing registry.

    python -m pytest tests/unit/test_list_agents_tool.py --noconftest -q
"""
import json
import re
from pathlib import Path

import pytest

_HIE_SRC = (Path(__file__).resolve().parents[2] /
            'hart_intelligence_entry.py').read_text(encoding='utf-8')


@pytest.fixture
def _seeded_trained_agent():
    """Hermetic seed for the trained-agent count.

    list_agents' trained/hive buckets read the social DB via _get_db(). CI
    starts from an EMPTY DB, so the old `trained_agents >= 1` assertion — which
    silently assumed the ambient dev DB's 100+ agent rows — failed in CI for
    lack of data, not a real bug (#29: a test must carry its own state, never
    inherit the host's). Ensure the schema exists and seed ONE trained agent
    through the canonical UserService.register_agent (user_type='agent' +
    api_token = the trained/local marker), so the REAL DB query runs against
    REAL data on any machine. Idempotent: a re-run against the same DB hits the
    global-name-uniqueness guard, which means the row is already present.
    """
    from integrations.social.models import Base, get_engine, get_db
    from integrations.social.services import UserService
    Base.metadata.create_all(get_engine())
    db = get_db()
    try:
        try:
            UserService.register_agent(
                db, 'ci seed agent', skip_name_validation=True)
            db.commit()
        except ValueError:
            db.rollback()  # already seeded (name taken) — still >= 1 trained
    finally:
        db.close()
    yield


def test_impl_enumerates_real_agents():
    """The canonical impl works standalone (shared with the MCP bridge)."""
    from integrations.mcp._tool_impls import list_agents
    data = json.loads(list_agents())
    assert data.get('expert_agents', 0) >= 1
    assert isinstance(data.get('agents'), list) and data['agents']


def test_impl_reports_hive_and_trained_buckets(_seeded_trained_agent):
    """Owner design 2026-08-24: 'list agents shd give local agents and
    hive agents that are peerlink exchanged or pulled from central'.
    The sync engine lands those as user_type='agent' User rows with
    api_token=None (_handle_sync_agent: 'never credential a synced
    mirror'), so that existing contract is the origin marker.  The keys
    must ALWAYS be present — zero counts when nothing was exchanged yet
    or the DB is unavailable, never absent."""
    from integrations.mcp._tool_impls import list_agents
    data = json.loads(list_agents())
    for key in ('trained_agents', 'hive_agents', 'trained', 'hive'):
        assert key in data, f'missing {key} — hive enumeration dropped'
    assert isinstance(data['hive'], list)
    for entry in data['hive']:
        assert entry.get('origin') == 'hive'
    # FALSIFIABLE against the real dev DB (review finding #5: the earlier
    # version passed even with the DB leg dead).  The dev DB carries 100+
    # agent-type users; zero trained here means the leg silently threw.
    assert data['trained_agents'] >= 1, (
        'DB leg returned zero trained agents on a DB that has them — '
        'the except/pass swallowed a real failure')
    # Review finding #4: the boot-time hevolve_system_agent bootstrap has
    # no api_token but is LOCAL — it must never be labeled hive.
    hive_names = {e['name'] for e in data['hive']}
    assert 'HART System Agent' not in hive_names, (
        'local bootstrap row mislabeled as a hive mirror')


def test_langchain_registry_carries_list_agents_tool():
    """The fix: a List_Agents labeled_tool wired to the SAME canonical
    impl (no parallel enumeration) must exist in the chat registry."""
    m = re.search(r'labeled_tool\(\s*name="List_Agents".*?\)', _HIE_SRC, re.DOTALL)
    assert m, (
        "no List_Agents tool registered — the chat agent fabricates "
        "'I have no separate agents' while 2,637 sit in the registry")


def test_both_get_tools_branches_carry_list_agents():
    """get_tools has TWO tool lists (is_first=True labeled_tool registry +
    the is_first=False exhaustive list).  Live 2026-08-24 07:01: the tool
    was registered only in the first, so every casual_conv=False turn —
    the branch enumeration questions actually reach since the classifier
    override — got a registry WITHOUT it and the model truthfully said
    'I do not have access to a List_Agents tool'.  Pin: one registration
    per branch, both reading the shared description constant (the
    _CREATE_AGENT_TOOL_DESCRIPTION pattern)."""
    assert _HIE_SRC.count('name="List_Agents"') >= 2, (
        "List_Agents registered in only one get_tools branch — "
        "casual_conv=False turns get a registry without it")
    # definition + >=2 uses; a copy-pasted literal in one branch would drift
    assert _HIE_SRC.count('_LIST_AGENTS_TOOL_DESCRIPTION') >= 3, (
        "both registrations must share _LIST_AGENTS_TOOL_DESCRIPTION")


def test_wrapper_delegates_to_canonical_impl():
    """The wrapper must import the mcp impl, not re-implement it."""
    m = re.search(r'def _parse_list_agents.*?(?=\ndef |\nclass )', _HIE_SRC, re.DOTALL)
    assert m, "_parse_list_agents wrapper missing"
    assert '_tool_impls import list_agents' in m.group(0), (
        "wrapper must delegate to integrations.mcp._tool_impls.list_agents "
        "— a second enumeration would be a parallel path")
