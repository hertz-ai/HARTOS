"""Scheduled agent actions must dial the LIVE local backend, not a blind :6777.

Measured 2026-09-13 on the bundled desktop: execute_python_file POSTed to
http://localhost:6777/time_agent -- connection refused, because a bundled
desktop serves HARTOS in-process on :5000 and never binds :6777 -- so every
scheduled action was dropped while the function still returned 'done'.

Both schedulers must take the base from core.port_registry.get_local_backend_url(),
the single resolver the channel bridge and dispatch fallback already use.
(Nunba serves the path itself via create_inprocess_dispatch_blueprint.)
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Both modules type-annotate caches with autogen classes at import time.
pytest.importorskip('autogen', reason='autogen not installed')


@pytest.mark.parametrize('modname', ['hartos.reuse_recipe', 'hartos.create_recipe'])
def test_time_agent_url_comes_from_the_local_backend_resolver(modname):
    mod = importlib.import_module(modname)
    with patch.object(mod, 'get_local_backend_url',
                      return_value='http://localhost:5000') as resolver, \
         patch.object(mod, 'pooled_post') as mk_post:
        mod.execute_python_file('water the plants', 'user-1', 'pid-1')
    assert resolver.called, f'{modname} must ask get_local_backend_url() for the base'
    assert mk_post.call_args[0][0] == 'http://localhost:5000/time_agent'


@pytest.mark.parametrize('modname', ['hartos.reuse_recipe', 'hartos.create_recipe'])
def test_no_scheduler_url_hardcodes_the_backend_port(modname):
    """Source guard: the blind f'localhost:{get_port("backend")}' form must not return."""
    mod = importlib.import_module(modname)
    with open(mod.__file__, encoding='utf-8', errors='replace') as fh:
        src = fh.read()
    for path in ('/time_agent', '/visual_agent'):
        assert f'_get_llm_port("backend")}}{path}' not in src, (
            f'{modname} builds {path} from the blind backend port again -- that is '
            f'a dead :6777 on a bundled desktop')
