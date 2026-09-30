"""``get_flow_number`` must not 500 the whole /chat POST on a prompt file
that has no ``personas``.

THE LIVE FAILURE THIS ENCODES (2026-09-11 02:50:43, installed build):

    Some ERROR IN REUSE RECIPE 'personas'
    Traceback (most recent call last):
      ...
      File "hartos/reuse_recipe.py", line 6788, in chat_agent
        create_schedule(prompt_id, user_id)
      File "hartos/reuse_recipe.py", line 5925, in create_schedule
        role_number, role = get_flow_number(user_id, prompt_id)
      File "hartos/reuse_recipe.py", line 5876, in get_flow_number
        available_roles = [x['name'] for x in data['personas']]
    KeyError: 'personas'

Flask logged it as "Exception on /chat [POST]", i.e. the user's turn died
before any agent work began.

BLAST RADIUS, MEASURED (not estimated).  Counting only real agent prompt
files -- ``<digits>.json``, the shape ``helper_fun.safe_prompt_path(prompt_id)``
resolves -- because the prompts directory is majority non-agent artifacts
(#774) and the unfiltered number is misleading (172 files, 118 "missing",
which would have read as 69%):

    agent prompt files          736
    have 'personas'             702
    MISSING 'personas'           34   <- every one of these 500s on /chat

The 34 come in two shapes, and neither is malformed by accident:
    ['prompt_id','goal','user_id','name','is_active','image_url','synced_at']
        -- a cloud-synced stub: no flows, no personas
    ['status','agent_name','goal','flows','extra_information','prompt_id',...]
        -- authored, has flows, but no personas
So the function must survive BOTH 'personas' and 'flows' being absent.

WHY (0, None) IS THE RIGHT DEGRADATION.  All four call sites
(reuse_recipe.py :1123, :1746, :1901, :5925) consume the result identically --
``helper_fun.safe_prompt_path(prompt_id, role_number, 'recipe')`` -- so flow 0
is the same recipe they would have picked for a single-flow agent, which is
what an agent with no persona list is. Returning instead of raising turns an
un-chattable agent into one that at least starts.

WHAT THIS GUARD DOES NOT CLAIM: that those 34 agents then reach their goals.
It claims only that the turn is no longer killed before it begins.
"""

import io
import json
import os
import re
import sys
import tempfile
import types
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_HARTOS, 'hartos', 'reuse_recipe.py')


def _extract(func_name):
    """Return the source of one top-level function from reuse_recipe.py.

    reuse_recipe imports a large runtime graph at module scope, so the module
    cannot be imported in a unit test. Rather than fall back to asserting on
    source TEXT -- which cannot prove behaviour, and which this suite has twice
    caught going vacuous -- we lift the one function out and exec it against
    stubs. That makes the assertions below run the REAL code.
    """
    src = io.open(_SRC, encoding='utf-8', errors='replace').read()
    m = re.search(r'^def %s\(.*?(?=^def |\Z)' % re.escape(func_name),
                  src, re.S | re.M)
    assert m, '%s not found; re-point this guard' % func_name
    return m.group(0)


class _Logger(object):
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    error = warning
    debug = info


def _load_get_flow_number(role_value=None):
    """exec the real get_flow_number with the few globals it touches."""
    logger = _Logger()
    app = types.SimpleNamespace(logger=logger)
    helper = types.SimpleNamespace(safe_prompt_path=lambda pid, *a: pid)
    ns = {
        'json': json,
        'open': io.open,
        'current_app': app,
        'helper_fun': helper,
        'get_role': lambda u, p: role_value,
        '_ctx_safe_log': lambda level, msg: getattr(logger, level)(msg),
    }
    exec(compile(_extract('get_flow_number'), _SRC, 'exec'), ns)
    return ns['get_flow_number'], logger


def _prompt_file(payload):
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    return path


class TestMissingPersonasDoesNotKillTheTurn(unittest.TestCase):
    """RED until the unguarded subscripts are gone."""

    def tearDown(self):
        for p in getattr(self, '_paths', []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _run(self, payload, role_value=None):
        path = _prompt_file(payload)
        self._paths = getattr(self, '_paths', []) + [path]
        fn, logger = _load_get_flow_number(role_value)
        return fn('u1', path), logger

    def test_authored_agent_with_flows_but_no_personas(self):
        """The 2026-09-11 02:50:43 crash shape, verbatim."""
        (num, role), logger = self._run({
            'status': 'done', 'agent_name': 'x', 'goal': 'g',
            'flows': [{'persona': 'Executor'}], 'prompt_id': 1,
        })
        self.assertEqual(num, 0,
                         'a prompt file with no personas must degrade to flow '
                         '0, not raise KeyError and 500 the /chat POST')
        self.assertTrue(
            any('persona' in w.lower() for w in logger.warnings),
            'the degradation is silent; a prompt file missing personas is a '
            'data defect and must say so, or 34 un-chattable agents stay '
            'invisible')

    def test_cloud_synced_stub_has_neither_personas_nor_flows(self):
        """34 measured files; this shape has no 'flows' key either."""
        (num, role), _ = self._run({
            'prompt_id': 12345, 'goal': 'g', 'user_id': 'u',
            'name': 'n', 'is_active': True, 'image_url': '',
            'synced_at': '2026-09-11',
        })
        self.assertEqual(num, 0)

    def test_empty_personas_list_does_not_IndexError(self):
        """`role = available_roles[0]` is the third unguarded access."""
        (num, role), _ = self._run({'personas': [], 'flows': []})
        self.assertEqual(num, 0)

    def test_healthy_agent_still_selects_its_persona_flow(self):
        """The fix must not cost the working path — 702 of 736 agents."""
        (num, role), _ = self._run(
            {
                'personas': [{'name': 'Researcher'}, {'name': 'Executor'}],
                'flows': [{'persona': 'Researcher'}, {'persona': 'Executor'}],
            },
            role_value='Executor')
        self.assertEqual(num, 1, 'healthy persona->flow selection regressed')
        self.assertEqual(role, 'Executor')

    def test_no_role_falls_back_to_first_persona(self):
        """get_role returning None is normal, not an error."""
        (num, role), _ = self._run({
            'personas': [{'name': 'Researcher'}],
            'flows': [{'persona': 'Researcher'}],
        })
        self.assertEqual(role, 'Researcher')
        self.assertEqual(num, 0)


if __name__ == '__main__':
    unittest.main()
