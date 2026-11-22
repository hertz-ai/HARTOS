"""A consent ask names the agent, not its prompt id.

The card used to chip the raw agent_id (a prompt id such as 79211163351),
which means nothing to the owner.  ConsentService.agent_display_name resolves
the agent's own name where the ask is built: the social mirror of the agent
(User.agent_id == prompt id, display_name) first, then prompts/<id>.json's
"name".  The name rides the ask and the frontend note as ``agent_name``; an
agent nobody can name carries no key, so the card says "An agent".
"""
import json
import os
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

from integrations.social import consent_service as cs  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def _mirror(prompt_id, display_name, username=None):
    """The row _create_social_agent_from_prompt writes: a 3-word username, or
    its 'agent-<id>' fallback when no name could be generated."""
    with db_session() as db:
        db.add(User(id=f'agent-{prompt_id}',
                    username=username or f'swift.amber.p{prompt_id}',
                    user_type='agent', agent_id=str(prompt_id),
                    display_name=display_name))


def test_ask_carries_the_agents_name_from_the_social_mirror():
    _mirror(79211163351, 'Spider-Man')
    with patch.object(cs, '_emit') as emit:
        with db_session() as db:
            ConsentService.request_consent(db, 'owner', 'computer_control',
                                           agent_id='79211163351')
    topic, ask = emit.call_args.args
    assert topic == 'consent.request'
    assert ask['agent_id'] == '79211163351'
    assert ask['agent_name'] == 'Spider-Man'


def test_name_falls_back_to_the_prompt_file(tmp_path):
    (tmp_path / '5150.json').write_text(json.dumps({'name': 'Recipe Helper'}),
                                        encoding='utf-8')
    with patch('core.platform_paths.get_recipe_prompts_dir',
               return_value=str(tmp_path)):
        with db_session() as db:
            assert cs.agent_display_name(db, 5150) == 'Recipe Helper'


def test_a_placeholder_built_from_the_id_is_not_a_name(tmp_path):
    """_create_social_agent_from_prompt mirrors a nameless prompt as
    display_name 'Agent <id>' and username 'agent-<id>'; the prompt file
    itself has no "name".  Every one of those is the id again, so the card
    must say "An agent", not "Agent 79211163351"."""
    _mirror(79211163351, 'Agent 79211163351', username='agent-79211163351')
    with db_session() as db:
        db.add(User(id='agent-2', username='agent-4242', user_type='agent',
                    agent_id='4242', display_name=None))
    (tmp_path / '79211163351.json').write_text(json.dumps({'goal': 'help'}),
                                               encoding='utf-8')
    (tmp_path / '4242.json').write_text(json.dumps({'name': 'agent-4242'}),
                                        encoding='utf-8')
    with patch('core.platform_paths.get_recipe_prompts_dir',
               return_value=str(tmp_path)), patch.object(cs, '_emit') as emit:
        with db_session() as db:
            assert cs.agent_display_name(db, 79211163351) is None
            assert cs.agent_display_name(db, '4242') is None
            ConsentService.request_consent(db, 'owner', 'computer_control',
                                           agent_id='79211163351')
    assert 'agent_name' not in emit.call_args.args[1]


def test_a_placeholder_display_name_falls_back_to_the_handle():
    """display_name 'Agent <id>' with a real 3-word username: the handle is
    the name the social pages show, so the card shows it too."""
    _mirror(31, 'Agent 31', username='swift.amber.falcon')
    with db_session() as db:
        assert cs.agent_display_name(db, 31) == 'swift.amber.falcon'


def test_a_real_name_that_contains_the_id_is_still_a_name():
    _mirror(1, 'Studio 1 Assistant')
    with db_session() as db:
        assert cs.agent_display_name(db, 1) == 'Studio 1 Assistant'
    assert cs._is_a_name('1', '1') is False
    assert cs._is_a_name('AGENT 1', '1') is False


def test_only_a_mirror_row_that_is_an_agent_names_it():
    with db_session() as db:
        db.add(User(id='human-1', username='bsathish', user_type='human',
                    agent_id='77', display_name='Sathish'))
    with db_session() as db:
        assert cs.agent_display_name(db, '77') is None


def test_an_id_that_is_not_a_plain_id_is_never_used_as_a_path(tmp_path):
    """The id becomes a file name; '../x' must not read x.json."""
    (tmp_path / 'secret.json').write_text(json.dumps({'name': 'Leaked'}),
                                          encoding='utf-8')
    with patch('core.platform_paths.get_recipe_prompts_dir',
               return_value=str(tmp_path / 'prompts')):
        with db_session() as db:
            for bad in ('../secret', '..\\secret', '/secret', 'a b', '', None):
                assert cs.agent_display_name(db, bad) is None


def test_an_agent_nobody_can_name_carries_no_name(tmp_path):
    with patch('core.platform_paths.get_recipe_prompts_dir',
               return_value=str(tmp_path)), patch.object(cs, '_emit') as emit:
        with db_session() as db:
            assert cs.agent_display_name(db, '404') is None
            assert cs.agent_display_name(db, None) is None
            ConsentService.request_consent(db, 'owner', 'screen_capture',
                                           agent_id='404')
    assert 'agent_name' not in emit.call_args.args[1]


def test_the_frontend_note_carries_the_name_only_when_there_is_one():
    seen = []
    with patch('integrations.social.realtime.on_notification',
               lambda uid, note: seen.append(note)):
        cs._emit('consent.request', {'user_id': 'owner', 'consent_type': 'data_access',
                                     'agent_id': '1', 'agent_name': 'Spider-Man'})
        cs._emit('consent.request', {'user_id': 'owner', 'consent_type': 'data_access',
                                     'agent_id': '2', 'agent_name': None})
    assert seen[0]['agent_name'] == 'Spider-Man'
    assert 'agent_name' not in seen[1]


def test_the_notices_name_the_agent_too_and_share_the_asks_shape(tmp_path):
    _mirror(7, 'Spider-Man')
    with patch.object(cs, '_emit') as emit:
        with db_session() as db:
            assert ConsentService.auto_grant_with_notice(
                db, 'owner', 'data_access', agent_id='7') is True
    topic, data = emit.call_args.args
    assert topic == 'consent.auto_granted'
    assert data['agent_name'] == 'Spider-Man'
    # an unnamed agent: no key at all, as on the ask (not agent_name: None)
    with patch('core.platform_paths.get_recipe_prompts_dir',
               return_value=str(tmp_path)), patch.object(cs, '_emit') as emit:
        with db_session() as db:
            ConsentService.auto_grant_with_notice(db, 'owner', 'data_access',
                                                  agent_id='8')
    assert 'agent_name' not in emit.call_args.args[1]


# -- agent_asker: the name as a card says it, said once, by HARTOS ----------
# The page (Nunba constants/consentAsks.askerName) worded "Agent <name>" with
# its own copy of agent_as_asker.  The ask and the notices now carry the
# wording itself beside the bare name, so the page shows what it is sent.

@pytest.mark.parametrize('name, asker', [
    ('Vijai', 'Agent Vijai'),
    ('Agent Smith', 'Agent Smith'),      # already says it: not doubled
    ('agent smith', 'agent smith'),      # whatever the case
    ('Agentic', 'Agent Agentic'),        # only the whole word counts
    ('Disk Watch Dan', 'Agent Disk Watch Dan'),
])
def test_the_ask_says_who_asks_as_a_card_says_it(name, asker):
    _mirror(61, name)
    with patch.object(cs, '_emit') as emit:
        with db_session() as db:
            ConsentService.request_consent(db, 'owner', 'computer_control',
                                           agent_id='61')
    ask = emit.call_args.args[1]
    assert ask['agent_name'] == name      # the bare name stays, unchanged
    assert ask['agent_asker'] == asker


def test_a_name_with_spaces_around_it_is_said_trimmed():
    """agent_display_name hands back a trimmed name; the wording is built
    from that one, so it cannot carry the spaces either."""
    _mirror(62, '  Vijai  ')
    with patch.object(cs, '_emit') as emit:
        with db_session() as db:
            ConsentService.request_consent(db, 'owner', 'computer_control',
                                           agent_id='62')
    ask = emit.call_args.args[1]
    assert ask['agent_asker'] == 'Agent Vijai'


def test_an_agent_nobody_can_name_carries_no_asker_wording(tmp_path):
    """No name, no key: the page then says "An agent" / "this agent"."""
    with patch('core.platform_paths.get_recipe_prompts_dir',
               return_value=str(tmp_path)), patch.object(cs, '_emit') as emit:
        with db_session() as db:
            ConsentService.request_consent(db, 'owner', 'screen_capture',
                                           agent_id='404')
    assert 'agent_asker' not in emit.call_args.args[1]


def test_a_phones_ask_carries_no_agent_wording():
    """A device ask names a phone (requester_name), never an agent."""
    with patch.object(cs, '_emit') as emit:
        with db_session() as db:
            ConsentService.request_consent(
                db, 'owner', 'device_access', scope='device:' + 'ab' * 32,
                requester_name='Giri')
    ask = emit.call_args.args[1]
    assert ask['requester_name'] == 'Giri'
    assert 'agent_asker' not in ask and 'agent_name' not in ask


def test_the_frontend_note_carries_the_wording_beside_the_name():
    seen = []
    with patch('integrations.social.realtime.on_notification',
               lambda uid, note: seen.append(note)):
        cs._emit('consent.request', {
            'user_id': 'owner', 'consent_type': 'data_access', 'agent_id': '1',
            'agent_name': 'Spider-Man', 'agent_asker': 'Agent Spider-Man'})
        cs._emit('consent.request', {
            'user_id': 'owner', 'consent_type': 'data_access', 'agent_id': '2'})
    assert seen[0]['agent_name'] == 'Spider-Man'
    assert seen[0]['agent_asker'] == 'Agent Spider-Man'
    assert 'agent_asker' not in seen[1]


def test_the_notices_carry_the_wording_too():
    _mirror(7, 'Spider-Man')
    with patch.object(cs, '_emit') as emit:
        with db_session() as db:
            assert ConsentService.auto_grant_with_notice(
                db, 'owner', 'data_access', agent_id='7') is True
    data = emit.call_args.args[1]
    assert data['agent_name'] == 'Spider-Man'
    assert data['agent_asker'] == 'Agent Spider-Man'


def test_a_name_of_marks_alone_carries_no_asker_wording():
    """agent_as_asker has nothing to say for a name nobody can see, and an
    empty 'agent_asker' is not sent: the key is there only with words in it
    (review rv7095; "Agent \\ufeff" was sent)."""
    _mirror(63, '﻿', username='​')
    _mirror(64, '﻿Agent Smith')
    asks = []
    for agent_id in ('63', '64'):
        with patch.object(cs, '_emit') as emit:
            with db_session() as db:
                ConsentService.request_consent(db, 'owner', 'computer_control',
                                               agent_id=agent_id)
        asks.append(emit.call_args.args[1])
    assert 'agent_asker' not in asks[0]
    assert asks[1]['agent_asker'] == 'Agent Smith'
    assert asks[1]['agent_name'] == '﻿Agent Smith'   # the bare name, as kept
