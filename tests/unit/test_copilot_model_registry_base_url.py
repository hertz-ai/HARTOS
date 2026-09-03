"""The Claude co-pilot MODEL-registry backend must dial where HARTOS SERVES.

Sibling of test_copilot_backend_resolution.py, which covered the co-pilot
DAEMON (hart_copilot_daemon.backend()).  This file covers the SECOND site that
carried the same #71 bug and was missed by the daemon fix (a34b6244):

  integrations/agent_engine/model_registry.py registers the `claude-code`
  frontier backend with a config_list_entry.base_url.  It built that URL from
  get_port('backend') == 6777, which is DEAD on the bundled desktop (HARTOS
  serves in-process on the Flask port 5000).  A reuse turn routed to this
  backend then dialled a closed socket:

      POST http://127.0.0.1:6777/api/claude/v1/chat/completions
      -> WinError 10061 -> openai.APIConnectionError -> '_tier: direct'

  (live-pinned 2026-09-03 with a :6777 socket sniffer: the reuse agent's
  autogen call landed on exactly that path).

The fix is the ONE canonical resolver core.port_registry.get_local_backend_url,
which probes 'backend' then 'flask' and returns the first ACTUALLY LISTENING.

These tests drive _register_defaults() against simulated listeners rather than
asserting on source text (CLAUDE.md Gate 5).

Run:
  pytest tests/unit/test_copilot_model_registry_base_url.py -v
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _port_of(url):
    # url like http://127.0.0.1:5000/api/claude/v1
    host_port = url.split('//', 1)[1].split('/', 1)[0]
    return int(host_port.rsplit(':', 1)[1])


@pytest.fixture
def env(monkeypatch):
    """claude_code_available -> True, and control which ports 'listen'."""
    import core.port_registry as pr
    import integrations.coding_agent.claude_code_backend as ccb
    import integrations.agent_engine.model_registry as mr

    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(ccb, 'claude_code_available', lambda: True)

    state = {'open': set()}
    monkeypatch.setattr(pr, '_is_port_listening', lambda p: p in state['open'])

    # Start from a clean registry each run so a prior default registration
    # doesn't mask the re-registration under test.
    try:
        mr.model_registry._models.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    return pr, mr, state


def _registered_base_url(mr):
    b = mr.model_registry.get_model('claude-code')
    assert b is not None, "claude-code backend was not registered"
    return (b.config_list_entry or {}).get('base_url', '')


def test_bundled_desktop_uses_the_flask_port(env):
    """THE REGRESSION TEST. Only Flask listens (bundled desktop). The old code
    built the URL from get_port('backend')=6777 and every copilot-routed reuse
    turn hit a dead socket."""
    pr, mr, state = env
    state['open'] = {pr.get_port('flask')}

    mr._register_defaults()
    url = _registered_base_url(mr)

    assert url.endswith('/api/claude/v1'), url
    assert _port_of(url) == pr.get_port('flask'), (
        'copilot backend base_url dialled %d while HARTOS serves on %d '
        '(flask) -- reuse turns routed here hit a closed socket'
        % (_port_of(url), pr.get_port('flask')))


def test_appliance_uses_the_backend_port(env):
    """Samsung .69 appliance: 6777 is bound. Must still resolve there."""
    pr, mr, state = env
    state['open'] = {pr.get_port('backend')}

    mr._register_defaults()
    url = _registered_base_url(mr)
    assert _port_of(url) == pr.get_port('backend'), url


def test_explicit_base_url_wins(env, monkeypatch):
    """Remote/cloud deploys set HEVOLVE_BASE_URL; probing must not override it."""
    pr, mr, state = env
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://10.0.0.5:9999')
    state['open'] = {pr.get_port('backend')}

    mr._register_defaults()
    url = _registered_base_url(mr)
    assert url == 'http://10.0.0.5:9999/api/claude/v1', url


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
