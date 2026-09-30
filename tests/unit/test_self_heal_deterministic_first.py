"""Self-heal is deterministic-FIRST, agentic-FALLBACK.

Guards the 2026-06-24 fix for the pyloudnorm self-heal loop.  A missing
dependency (``ModuleNotFoundError``) is remediated by a deterministic
``pip install``; the agentic code-agent goal is dispatched ONLY if that
install fails (rc != 0) — because editing source can never summon a
package and the code agent just loops, churning the GIL.  On success the
worker is reaped so the next call respawns with the dep present.

Regression targets:
  * gpu_worker.GPUWorker._maybe_self_heal_from_line   (P1 gate, P2 respawn)
  * goal_manager._build_self_heal_prompt              (P4 prompt routing)
"""
import sys
import types

from unittest.mock import MagicMock

import pytest

from integrations.service_tools import gpu_worker
from core.error_advice import FAILED_STEP_PIP_INSTALL, FAILED_STEP_SETUP_OFFER
from integrations.agent_engine.goal_manager import _build_self_heal_prompt


_MODNOTFOUND_LINE = "ModuleNotFoundError: No module named 'pyloudnorm'"


class _SyncThread:
    """Run the target inline so the daemon-threaded ``_install_async`` is
    deterministic in tests (no join/sleep race)."""

    def __init__(self, target=None, daemon=None, name=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


def _make_worker():
    # chatterbox_ml runs on python-embed (tts_router install_target='main'),
    # so its heal goes to the user site.  An engine with its own venv takes
    # the setup offer instead (tests at the end of this file).
    return gpu_worker.GPUWorker(
        name='chatterbox_ml',
        module='integrations.service_tools.gpu_worker',
    )


def _fake_run(returncode):
    def _run(*a, **k):
        m = MagicMock()
        m.returncode = returncode
        return m
    return _run


@pytest.fixture
def he_mock(monkeypatch):
    """Install a mockable ``core.error_advice.handle_exception`` and run
    the self-heal install thread inline.  Returns the handle_exception
    mock so tests can assert dispatch / non-dispatch."""
    monkeypatch.setattr(gpu_worker.threading, 'Thread', _SyncThread)

    mock = MagicMock()
    if 'core' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'core', types.ModuleType('core'))
    ea = types.ModuleType('core.error_advice')
    ea.handle_exception = mock
    monkeypatch.setitem(sys.modules, 'core.error_advice', ea)
    return mock


# ── P1 + P2 : gpu_worker gating + respawn ──────────────────────────────

def test_install_success_reaps_worker_and_skips_agentic(monkeypatch, he_mock):
    """rc == 0  →  worker reaped (P2), NO agentic goal (P1, loop killed)."""
    monkeypatch.setattr(gpu_worker.subprocess, 'run', _fake_run(0))
    w = _make_worker()
    stop_mock = MagicMock()
    monkeypatch.setattr(w, 'stop', stop_mock)

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    stop_mock.assert_called_once()
    he_mock.assert_not_called()


def test_install_failure_dispatches_agentic_fallback(monkeypatch, he_mock):
    """rc != 0  →  agentic fallback fired (P1), worker NOT reaped."""
    monkeypatch.setattr(gpu_worker.subprocess, 'run', _fake_run(1))
    w = _make_worker()
    stop_mock = MagicMock()
    monkeypatch.setattr(w, 'stop', stop_mock)

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    stop_mock.assert_not_called()
    he_mock.assert_called_once()
    _, kwargs = he_mock.call_args
    assert kwargs.get('category') == 'subprocess.tool_load'
    assert kwargs.get('context', {}).get('missing_package') == 'pyloudnorm'


def test_self_heal_idempotent_per_package(monkeypatch, he_mock):
    """A flood of identical tracebacks triggers exactly one install."""
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    w = _make_worker()
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)
    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    assert run_mock.call_count == 1


def test_non_modulenotfound_line_is_ignored(monkeypatch, he_mock):
    """Unrelated stderr must not trigger any remediation."""
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    w = _make_worker()
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line("INFO: loading model weights...")

    run_mock.assert_not_called()
    he_mock.assert_not_called()


# ── P4 : goal_manager prompt routing ───────────────────────────────────

def _goal(category, ctx):
    return {
        'title': 'Self-heal test',
        'description': 'desc',
        'config': {'category': category, 'context': ctx},
    }


def test_prompt_missing_package_routes_to_dependency_remediation():
    """subprocess.tool_load + missing_package (no backend) must NOT fall
    through to the generic 'read source, write fix' loop."""
    p = _build_self_heal_prompt(
        _goal('subprocess.tool_load', {'missing_package': 'pyloudnorm'})
    )
    assert 'pyloudnorm' in p
    assert 'NOT a source-code bug' in p
    assert 'Read the source file' not in p   # generic path must be skipped


def test_prompt_backend_case_still_repairs_venv():
    """Regression guard: the existing backend-repair branch is untouched."""
    p = _build_self_heal_prompt(
        _goal('subprocess.tool_load', {'backend': 'chatterbox'})
    )
    assert 'repair_backend_venv' in p


def test_prompt_generic_exception_still_edits_source():
    """A real code bug (non-dep category) still routes to source editing."""
    p = _build_self_heal_prompt(_goal('runtime.assertion', {}))
    assert 'Read the source file' in p


# ── 2026-09-20: heal where the child reads, and never "install" the app ──
#
# Measured on the installed build (frozen_debug.log 16:09:55-16:10:00): the
# chatterbox_turbo venv worker died with "No module named 'integrations'",
# the self-heal ran `pip install integrations --target ~/.nunba/site-packages`
# (rc=1, no such package), then dispatched an agentic self-heal goal and a
# crash report, all for a module this very process was running.  And a
# per-backend venv never reads the user site at all (its interpreter is
# isolated), so even a successful --target install there is invisible to it.

import logging


def test_a_module_the_parent_itself_runs_is_a_path_defect_not_a_dependency(
        monkeypatch, he_mock, caplog):
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    w = _make_worker()
    stop_mock = MagicMock()
    monkeypatch.setattr(w, 'stop', stop_mock)

    with caplog.at_level(logging.ERROR, logger=gpu_worker.logger.name):
        w._maybe_self_heal_from_line(
            "ModuleNotFoundError: No module named 'integrations'")

    run_mock.assert_not_called()          # nothing to pip
    he_mock.assert_not_called()           # nothing for a code agent to do
    stop_mock.assert_not_called()
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("'integrations'" in m and "module path" in m for m in said), said


def _venv_layout(tmp_path):
    py = tmp_path / 'venvs' / 'chatterbox_turbo' / 'Scripts' / 'python.exe'
    py.parent.mkdir(parents=True)
    py.write_text('')
    return str(py)


def _capture_pip(monkeypatch):
    captured = {}

    def _run(args, **kwargs):
        captured['args'] = list(args)
        m = MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr(gpu_worker.subprocess, 'run', _run)
    return captured


def test_a_venv_worker_heals_into_its_own_interpreter_not_the_user_site(
        monkeypatch, he_mock, tmp_path):
    venv_py = _venv_layout(tmp_path)
    monkeypatch.setattr(
        'core.venv_paths.venv_python_if_exists',
        lambda backend: venv_py if backend == 'chatterbox_turbo' else None)
    captured = _capture_pip(monkeypatch)
    w = gpu_worker.GPUWorker(
        name='chatterbox_turbo',
        module='integrations.service_tools.gpu_worker',
        python_exe=venv_py,
    )
    monkeypatch.setattr(w, 'stop', MagicMock())
    monkeypatch.setattr(w, '_user_site_packages_dir',
                        lambda: str(tmp_path / 'usersite'))

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    assert captured['args'][0] == venv_py, "pip must run under the child's python"
    assert '--target' not in captured['args'], captured['args']
    assert captured['args'][-1] == 'pyloudnorm'
    he_mock.assert_not_called()


def test_a_python_embed_worker_still_heals_into_the_user_site(
        monkeypatch, he_mock, tmp_path):
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    captured = _capture_pip(monkeypatch)
    w = _make_worker()                      # interpreter = the default resolver
    monkeypatch.setattr(w, 'stop', MagicMock())
    user_site = str(tmp_path / 'usersite')
    monkeypatch.setattr(w, '_user_site_packages_dir', lambda: user_site)

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    args = captured['args']
    assert args[0] == w.python_exe
    assert args[args.index('--target') + 1] == user_site
    assert args[-1] == 'pyloudnorm'


def test_the_heal_installs_the_distribution_nunbas_table_names(
        monkeypatch, he_mock, tmp_path):
    """`import coqpit` failed on the installed build 2026-09-20; the heal
    ran `pip install coqpit` (the abandoned original) where coqui-tts needs
    the fork `coqpit-config`, and the engine then refused to import at all.
    The pip name comes from Nunba's one alias table when it is present."""
    table = types.ModuleType('tts.package_installer')
    table.pip_name_for_import = (
        lambda name: 'coqpit-config' if name == 'coqpit' else name)
    table.get_user_site_packages = lambda: str(tmp_path / 'usersite')
    monkeypatch.setitem(sys.modules, 'tts', types.ModuleType('tts'))
    monkeypatch.setitem(sys.modules, 'tts.package_installer', table)
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    captured = _capture_pip(monkeypatch)
    w = _make_worker()
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line("ModuleNotFoundError: No module named 'coqpit'")

    assert captured['args'][-1] == 'coqpit-config', captured['args']
    assert 'coqpit' not in captured['args']


# ── 2026-09-28: an engine that lives in its own venv is never healed into ──
# ── the shared user site; its setup is offered through the owner's consent ──
#
# Measured on the installed build (gui_app.log.4, 2026-09-25 16:48:30-46):
# f5_tts is declared install_target='venv' (tts_router) but had no venv, so
# the spawn fell back to python-embed, died with "No module named 'f5_tts'",
# and the heal ran `pip install f5_tts --target ~/.nunba/site-packages` --
# the shared site that engine's venv exists to keep it out of (rc=1), then
# raised an agentic goal.  xtts_v2 did the same for 'TTS' (its venv was
# built by another interpreter, so the spawn refused it).  The engine is not
# installed; installing it is capability_setup's consent-gated path.

def _offer_mock(monkeypatch):
    offer = MagicMock(return_value='asked')
    monkeypatch.setattr(
        'integrations.agent_engine.capability_setup.request_capability_setup',
        offer)
    return offer


@pytest.mark.parametrize('engine, missing', [
    ('f5_tts', 'f5_tts'),      # no venv at all
    ('xtts_v2', 'TTS'),        # venv refused (another interpreter built it)
    ('kokoro', 'torch'),       # a transitive of the engine, same answer
])
def test_a_venv_engine_outside_its_venv_offers_setup_never_pips_the_user_site(
        monkeypatch, he_mock, tmp_path, engine, missing):
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    offer = _offer_mock(monkeypatch)
    w = gpu_worker.GPUWorker(name=engine,
                             module='integrations.service_tools.gpu_worker')
    stop_mock = MagicMock()
    monkeypatch.setattr(w, 'stop', stop_mock)
    monkeypatch.setattr(w, '_user_site_packages_dir',
                        lambda: str(tmp_path / 'usersite'))

    w._maybe_self_heal_from_line(
        f"ModuleNotFoundError: No module named '{missing}'")

    run_mock.assert_not_called()          # nothing pip'd anywhere
    he_mock.assert_not_called()           # no "pip failed" agentic goal
    stop_mock.assert_not_called()
    offer.assert_called_once()
    args, kwargs = offer.call_args
    assert args == (f'tts:{engine}',)
    assert kwargs['category'] == 'subprocess.tool_load'
    assert kwargs['context']['backend'] == engine
    assert kwargs['context']['missing_package'] == missing
    assert kwargs['reason'].strip()


def test_a_venv_engine_offer_is_made_once_per_worker(monkeypatch, he_mock):
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    monkeypatch.setattr(gpu_worker.subprocess, 'run',
                        MagicMock(side_effect=_fake_run(0)))
    offer = _offer_mock(monkeypatch)
    w = gpu_worker.GPUWorker(name='f5_tts',
                             module='integrations.service_tools.gpu_worker')
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line("ModuleNotFoundError: No module named 'torch'")
    w._maybe_self_heal_from_line("ModuleNotFoundError: No module named 'f5_tts'")

    assert offer.call_count == 1


def test_a_venv_engine_in_its_own_venv_still_heals_into_that_venv(
        monkeypatch, he_mock, tmp_path):
    py = tmp_path / 'venvs' / 'f5_tts' / 'Scripts' / 'python.exe'
    py.parent.mkdir(parents=True)
    py.write_text('')
    venv_py = str(py)
    monkeypatch.setattr(
        'core.venv_paths.venv_python_if_exists',
        lambda backend: venv_py if backend == 'f5_tts' else None)
    captured = _capture_pip(monkeypatch)
    offer = _offer_mock(monkeypatch)
    w = gpu_worker.GPUWorker(name='f5_tts',
                             module='integrations.service_tools.gpu_worker',
                             python_exe=venv_py)
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line("ModuleNotFoundError: No module named 'vocos'")

    assert captured['args'][0] == venv_py
    assert '--target' not in captured['args']
    offer.assert_not_called()


def test_an_engine_installed_into_python_embed_still_heals_the_user_site(
        monkeypatch, he_mock, tmp_path):
    """chatterbox_ml is install_target='main': python-embed + the user site
    IS where it lives, so the heal there is unchanged."""
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    captured = _capture_pip(monkeypatch)
    offer = _offer_mock(monkeypatch)
    w = gpu_worker.GPUWorker(name='chatterbox_ml',
                             module='integrations.service_tools.gpu_worker')
    monkeypatch.setattr(w, 'stop', MagicMock())
    user_site = str(tmp_path / 'usersite')
    monkeypatch.setattr(w, '_user_site_packages_dir', lambda: user_site)

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    assert captured['args'][captured['args'].index('--target') + 1] == user_site
    offer.assert_not_called()


def test_an_offer_that_cannot_be_made_raises_the_missing_package_goal(
        monkeypatch, he_mock, caplog):
    """Review of 7810b4352: the once-flag is set before the offer runs, so an
    offer that raises (capability_setup not importable, a consent-layer bug)
    was only a WARNING and the worker never offered again: no card, no goal,
    and the engine silently fell back forever.  Now the owner is told through
    the missing-package goal the old path raised, at ERROR, still once.  The
    goal carries no 'backend', so it cannot route to repair_backend_venv and
    install the engine without the owner's yes."""
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    run_mock = MagicMock(side_effect=_fake_run(0))
    monkeypatch.setattr(gpu_worker.subprocess, 'run', run_mock)
    offer = MagicMock(side_effect=ImportError("no capability_setup here"))
    monkeypatch.setattr(
        'integrations.agent_engine.capability_setup.request_capability_setup',
        offer)
    w = gpu_worker.GPUWorker(name='f5_tts',
                             module='integrations.service_tools.gpu_worker')
    monkeypatch.setattr(w, 'stop', MagicMock())

    with caplog.at_level(logging.ERROR, logger=gpu_worker.logger.name):
        w._maybe_self_heal_from_line(
            "ModuleNotFoundError: No module named 'vocos'")
        w._maybe_self_heal_from_line(
            "ModuleNotFoundError: No module named 'torch'")

    offer.assert_called_once()            # the once-flag still holds
    run_mock.assert_not_called()          # and still no pip into the site
    he_mock.assert_called_once()
    (exc,), kwargs = he_mock.call_args
    assert isinstance(exc, ModuleNotFoundError)
    assert exc.name == 'vocos'
    assert kwargs['category'] == 'subprocess.tool_load'
    assert kwargs['agent_remediation'] is True
    ctx = kwargs['context']
    assert ctx['missing_package'] == 'vocos'
    assert ctx['worker_name'] == 'f5_tts'
    assert 'backend' not in ctx
    assert 'no capability_setup here' in ctx['remediation_hint']
    assert ctx['failed_step'] == FAILED_STEP_SETUP_OFFER
    said = [r.getMessage() for r in caplog.records
            if r.levelno == logging.ERROR and 'could not be offered' in r.getMessage()]
    assert said and 'no capability_setup here' in said[0], said


def _offer_returning(monkeypatch, outcome):
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: None)
    monkeypatch.setattr(gpu_worker.subprocess, 'run',
                        MagicMock(side_effect=_fake_run(0)))
    offer = MagicMock(return_value=outcome)
    monkeypatch.setattr(
        'integrations.agent_engine.capability_setup.request_capability_setup',
        offer)
    w = gpu_worker.GPUWorker(name='f5_tts',
                             module='integrations.service_tools.gpu_worker')
    monkeypatch.setattr(w, 'stop', MagicMock())
    return w, offer


def test_an_offer_nobody_could_be_asked_raises_the_goal(
        monkeypatch, he_mock, caplog):
    """request_capability_setup turns most failures (no owner set, consent
    DB unreachable) into the return 'unavailable', not an exception.  That
    is the same silent fallback as a raised offer, so it gets the same ERROR
    and the same goal."""
    w, offer = _offer_returning(monkeypatch, 'unavailable')

    with caplog.at_level(logging.ERROR, logger=gpu_worker.logger.name):
        w._maybe_self_heal_from_line(
            "ModuleNotFoundError: No module named 'vocos'")

    offer.assert_called_once()
    he_mock.assert_called_once()
    (exc,), kwargs = he_mock.call_args
    assert exc.name == 'vocos'
    ctx = kwargs['context']
    assert ctx['missing_package'] == 'vocos'
    assert ctx['failed_step'] == FAILED_STEP_SETUP_OFFER
    assert 'backend' not in ctx
    assert "'unavailable'" in ctx['remediation_hint']
    said = [r.getMessage() for r in caplog.records
            if r.levelno == logging.ERROR and 'could not be offered' in r.getMessage()]
    assert said and "'unavailable'" in said[0], said


@pytest.mark.parametrize('outcome', ['asked', 'declined', 'provisioning'])
def test_an_offer_that_reached_the_owner_raises_no_goal(
        monkeypatch, he_mock, outcome):
    """The card is up, the owner said no, or the work is raised: nothing is
    silent, and a goal after a 'declined' would override the owner's no."""
    w, offer = _offer_returning(monkeypatch, outcome)

    w._maybe_self_heal_from_line(
        "ModuleNotFoundError: No module named 'vocos'")

    offer.assert_called_once()
    he_mock.assert_not_called()


def test_the_pip_failed_goal_says_pip_was_the_step_that_failed(
        monkeypatch, he_mock):
    monkeypatch.setattr(gpu_worker.subprocess, 'run', _fake_run(1))
    w = _make_worker()
    monkeypatch.setattr(w, 'stop', MagicMock())

    w._maybe_self_heal_from_line(_MODNOTFOUND_LINE)

    assert he_mock.call_args[1]['context']['failed_step'] == FAILED_STEP_PIP_INSTALL


def test_prompt_for_a_failed_setup_offer_does_not_claim_a_pip_install():
    p = _build_self_heal_prompt(_goal('subprocess.tool_load', {
        'missing_package': 'vocos', 'failed_step': FAILED_STEP_SETUP_OFFER}))
    assert 'vocos' in p
    assert 'NOT a source-code bug' in p
    assert 'pip install' not in p
    assert 'was already attempted' not in p
    assert 'capability_setup' in p
    assert 'repair_backend_venv' not in p     # no install without the owner
    assert 'Read the source file' not in p


@pytest.mark.parametrize('ctx', [
    {'missing_package': 'pyloudnorm', 'failed_step': FAILED_STEP_PIP_INSTALL},
    {'missing_package': 'pyloudnorm'},          # goals filed before the key
])
def test_prompt_for_a_failed_pip_install_still_says_so(ctx):
    p = _build_self_heal_prompt(_goal('subprocess.tool_load', ctx))
    assert '`pip install pyloudnorm` was already attempted' in p
    assert 'capability_setup' not in p


def test_a_goal_that_cannot_be_raised_is_logged_at_error(monkeypatch, caplog):
    """The last step of a failure that would otherwise be silent: if the
    goal itself cannot be raised, that is said at ERROR, with the package
    and the cause, never a DEBUG line."""
    import types
    ea = types.ModuleType('core.error_advice')

    def handle_exception(*a, **k):
        raise RuntimeError('goal store unreachable')

    ea.handle_exception = handle_exception
    monkeypatch.setitem(sys.modules, 'core.error_advice', ea)
    w = _make_worker()

    with caplog.at_level(logging.DEBUG, logger=gpu_worker.logger.name):
        w._raise_missing_package_goal('vocos', 'hint',
                                      failed_step=FAILED_STEP_PIP_INSTALL)

    said = [r for r in caplog.records if 'goal store unreachable' in r.getMessage()]
    assert said, [r.getMessage() for r in caplog.records]
    assert all(r.levelno == logging.ERROR for r in said)
    assert "'vocos'" in said[0].getMessage()


def test_the_step_names_are_the_ones_the_prompt_reads():
    """One value per step, shared by the writer and the reader."""
    assert FAILED_STEP_PIP_INSTALL != FAILED_STEP_SETUP_OFFER
    offer = _build_self_heal_prompt(_goal('subprocess.tool_load', {
        'missing_package': 'vocos', 'failed_step': FAILED_STEP_SETUP_OFFER}))
    pip = _build_self_heal_prompt(_goal('subprocess.tool_load', {
        'missing_package': 'vocos', 'failed_step': FAILED_STEP_PIP_INSTALL}))
    assert 'could not be offered' in offer
    assert 'was already attempted' in pip
