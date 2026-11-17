"""A tool family that fails to register is skipped, and the agent is built.

REUSE's create_agents_for_user registers the channel, media and news tool
families each inside ``try: ... except Exception as e: tool_logger.debug(
"... registration skipped: ...")``.  ``tool_logger`` was never defined in
hartos/reuse_recipe.py (CREATE defines it, create_recipe.py), so any failure
in one of those families raised NameError out of the very except meant to log
it, and the person's agent was never built.  The resonance-profile guard next
to the REUSE self line (33d7dea57) logged through a function-local logger for
the same reason; it now uses the same module logger.

Real create_agents_for_user over saved files, through the fixture of
tests/unit/test_a_reuse_turn_never_tells_the_agent_it_is_the_person.py; each
family is replaced by a module whose register function raises.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_a_reuse_turn_never_tells_the_agent_it_is_the_person import (  # noqa: E402,F401
    KEPT, reuse_turn)
from tests.unit.test_create_loop_end_to_end import create_env  # noqa: E402,F401  (fixture)


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def agent_log():
    """Every line CREATE's and REUSE's "agent_logger" writes."""
    logger = logging.getLogger('agent_logger')
    handler, level = _Lines(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(level)


FAMILIES = [
    ('integrations.channels.agent_tools', 'register_channel_tools',
     'Channel tools registration skipped'),
    ('integrations.service_tools.media_agent', 'register_media_tools',
     'Media tools registration skipped'),
    ('integrations.agent_engine.news_tools', 'register_news_tools',
     'News tools registration skipped'),
]


@pytest.mark.parametrize('module,function,line', FAMILIES)
def test_a_family_that_fails_to_register_is_skipped_and_the_agent_is_built(
        reuse_turn, agent_log, monkeypatch, module, function, line):
    def broken(*args, **kwargs):
        raise RuntimeError('family unavailable')

    # The real module when it imports here (its register function is what
    # create_agents_for_user calls); a stand-in module when it cannot.
    try:
        real = importlib.import_module(module)
    except Exception:
        real = None
    if real is not None:
        monkeypatch.setattr(real, function, broken)
    else:
        fake = types.ModuleType(module)
        setattr(fake, function, broken)
        monkeypatch.setitem(sys.modules, module, fake)

    msg, kept = reuse_turn('executor_only')       # built, not NameError

    assert len(kept) == KEPT['executor_only'], kept
    assert any(line in entry and 'family unavailable' in entry
               for entry in agent_log), agent_log


def test_an_unreadable_profile_is_logged_where_the_other_skips_are(
        reuse_turn, agent_log, monkeypatch):
    from unittest import mock
    from core import agent_personality as ap
    from core import resonance_profile as rp
    # The profile is read only for an agent with a saved personality, as in
    # test_an_unreadable_profile_keeps_the_agents_personality.
    saved = ap.AgentPersonality(agent_name='speech.local.coach', role=ap.AGENT_ROLE,
                                persona_name='Coach', primary_traits=[], tone='warm-casual',
                                greeting_style='Hi!', self_awareness_prompt='')
    monkeypatch.setattr(ap, 'load_personality', lambda *a, **k: saved)
    monkeypatch.setattr(rp, 'get_or_create_profile',
                        mock.Mock(side_effect=RuntimeError('profile unreadable')))
    reuse_turn('executor_only')
    assert any('Resonance profile loading skipped' in entry
               and 'profile unreadable' in entry for entry in agent_log), agent_log
