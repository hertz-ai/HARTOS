"""A recipe pull central refused is not asked again for ten minutes.

A desktop pulls a central agent's recipe before its turn so the agent runs
here on the free local model (Nunba _resolve_agent step 4, and HARTOS's own
/chat pull-on-demand).  Owner rule 11-23, latency aware for real-time
conversations: a pull central refuses must not cost every turn a round
trip of up to 13 s.  core.recipe_sync.pull_recipe remembers a DEFINITIVE
refusal (central answered 404, 403 or 410 for that account and id) in
memory for 10 minutes and answers False at once in that window.  Network
errors, timeouts and any other answer are not remembered; a pull that
lands clears the entry.

Run as one file: python -m pytest <this file> --noconftest -q
"""
import json
import os
import sys
import tempfile

os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
import requests  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import recipe_sync  # noqa: E402

CENTRAL = 'https://central.example'
PID, ACCOUNT, OTHER = '344', '10202', '20303'
LOGIN = 'fake-login'


@pytest.fixture
def central(monkeypatch, tmp_path):
    """A parent whose next answers the test queues (a status, or an
    exception to raise), every GET counted, and a clock the test moves."""
    state = {'answers': [], 'asked': [], 'now': 1000.0}
    # raising=False: on a HARTOS without the memory these are inert, so the
    # tests there fail by what was asked, not by a missing name.
    monkeypatch.setattr(recipe_sync, '_refused_pulls', {}, raising=False)
    monkeypatch.setattr(recipe_sync, '_monotonic', lambda: state['now'],
                        raising=False)

    files = {f'{PID}.json': json.dumps({'name': 'Cafe', 'flows': []}),
             f'{PID}_0_recipe.json': json.dumps({'actions': ['greet']})}

    class Session:
        def get(self, url, params=None, **_kw):
            state['asked'].append((url, (params or {}).get('user_id')))
            answer = state['answers'].pop(0)
            if isinstance(answer, Exception):
                raise answer
            resp = MagicMock(status_code=answer)
            resp.json.return_value = {
                'schema_version': recipe_sync.SCHEMA_VERSION,
                'prompt_id': PID, 'user_id': ACCOUNT, 'files': files,
                'checksum': recipe_sync.envelope_checksum(files),
                'uploaded_at': 1}
            return resp
    monkeypatch.setattr('core.http_pool.get_http_session', lambda: Session())

    def pull(account=ACCOUNT, pid=PID):
        return recipe_sync.pull_recipe(str(tmp_path), pid, account,
                                       central_url=CENTRAL, bearer=LOGIN)
    state['pull'] = pull
    state['dir'] = tmp_path
    return state


@pytest.mark.parametrize('status', [403, 404, 410])
def test_a_refused_pull_is_not_asked_again(central, status):
    central['answers'] = [status]
    assert central['pull']() is False
    assert central['pull']() is False
    assert len(central['asked']) == 1, 'the refusal was asked again'


def test_a_refusal_is_remembered_for_ten_minutes_only(central):
    central['answers'] = [404, 404]
    central['pull']()
    central['now'] += 599
    central['pull']()
    assert len(central['asked']) == 1
    central['now'] += 2                     # 601 s after the refusal
    central['pull']()
    assert len(central['asked']) == 2


@pytest.mark.parametrize('answer', [
    requests.ConnectionError('offline'), requests.Timeout('slow'),
    TimeoutError('slow'), 500, 502, 401])
def test_a_network_error_or_another_answer_is_not_remembered(central, answer):
    central['answers'] = [answer, answer]
    assert central['pull']() is False
    assert central['pull']() is False
    assert len(central['asked']) == 2


def test_the_memory_is_per_account_and_id(central):
    central['answers'] = [404, 404, 404]
    central['pull']()
    central['pull'](account=OTHER)
    central['pull'](pid='345')
    assert len(central['asked']) == 3
    central['pull']()
    assert len(central['asked']) == 3


def test_a_pull_that_lands_clears_the_refusal(central):
    central['answers'] = [404, 200]
    central['pull']()
    assert (ACCOUNT, PID) in recipe_sync._refused_pulls
    central['now'] += 601
    assert central['pull']() is True
    assert (central['dir'] / f'{PID}.json').is_file()
    assert (ACCOUNT, PID) not in recipe_sync._refused_pulls


def test_no_parent_asks_nothing_and_remembers_nothing(central, tmp_path):
    assert recipe_sync.pull_recipe(str(tmp_path), PID, ACCOUNT,
                                   central_url='', bearer=LOGIN) is False
    assert central['asked'] == [] and recipe_sync._refused_pulls == {}
