"""A daemon goal turn's plan is written with the tools that turn will have.

Measured on central 2026-11-20, goal 5ba50f98 (rice order desk, a lane goal
of a sales/outreach type): its turns run with the outreach CRM and journey
tools, chosen by goal_manager.daemon_turn_stored_tags (the goal TYPE's tags,
only on this node's own daemon turn).  Its plan writer was shown none of
them: the CREATE plan (/app/data/data/prompts/89682852097.json) sent the
outreach steps to execute_coding_task "because the dedicated outreach CRM
tools are not in the available tool list".  _autonomous_gather_info asked
gather_info with no tags.

These drive the real _autonomous_gather_info and the real
daemon_turn_stored_tags; replaced: the goal row read (db_session), the held
plan lookup, and gather_info itself (the plan LLM), which records the tags it
was asked with and stops the turn.
"""
import os
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')


class _Asked(Exception):
    """gather_info was reached: the turn stops here."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


def _goal_row(goal_type):
    """db_session over one goal row of ``goal_type``."""
    class _Query:
        def filter(self, *_):
            return self

        def first(self):
            return (goal_type,)

    class _Db:
        def query(self, *_):
            return _Query()

    @contextmanager
    def db_session(commit=True):
        yield _Db()
    return db_session


def _asked_with(hie, request_id, goal_type='outreach', own=True):
    """The goal tags _autonomous_gather_info asks gather_info with."""
    from core.chat_client import own_daemon_turn
    asked = []

    def gather_info(user_id, message, prompt_id, autonomous=False, goal_tags=()):
        asked.append(list(goal_tags or ()))
        raise _Asked()

    with patch('hartos.gather_agentdetails.gather_info', side_effect=gather_info), \
            patch('integrations.social.models.db_session', _goal_row(goal_type)), \
            patch.object(hie, '_held_plan_turn', return_value=None), \
            (own_daemon_turn(request_id) if own else nullcontext()), \
            pytest.raises(_Asked):
        hie._autonomous_gather_info('10202', 'find rice buyers', 'g-5ba50f98',
                                    background=True, request_id=request_id)
    assert len(asked) == 1
    return asked[0]


@pytest.mark.parametrize('goal_type', ['outreach', 'sales'])
def test_a_sales_or_outreach_goal_turn_asks_with_its_goal_types_tags(hie, goal_type):
    """FAILS BEFORE THE FIX: gather_info was asked with no tags."""
    from integrations.agent_engine.goal_manager import get_tool_tags
    tags = _asked_with(hie, 'daemon_5ba50f98', goal_type=goal_type)

    assert tags == sorted(get_tool_tags(goal_type))
    assert {'outreach', 'sales'} <= set(tags)


def test_its_plan_writer_is_shown_the_crm_tools_its_turn_gets(hie):
    """The one selector feeds both readers: the tags the turn asks the
    writer with put send_outreach_email and create_prospect in its tool
    list (FAILS BEFORE THE FIX)."""
    from hartos.gather_agentdetails import plan_writer_tools
    tools = plan_writer_tools(_asked_with(hie, 'daemon_5ba50f98'))

    for name in ('send_outreach_email', 'create_prospect', 'get_pipeline_status',
                 'view_journey_pipeline'):
        assert name in tools, name


@pytest.mark.parametrize('request_id, own', [
    (str(uuid.uuid4()), False),       # a person's own turn
    ('daemon_5ba50f98', False),       # a client naming the goal's turn id
])
def test_any_other_turn_asks_with_no_tags(hie, request_id, own):
    """A person's turn, and a daemon id this node's dispatch did not start,
    ask as before (passes before the fix too)."""
    assert _asked_with(hie, request_id, own=own) == []
