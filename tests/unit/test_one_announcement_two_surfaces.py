"""What the AI is doing is announced ONCE and reaches BOTH surfaces.

Measured on main 2026-09-21, before this: the AI-control ribbon was poked
directly at five sites and record_activity was called at four, and the two
agreed at exactly ONE of them.  The consequences the owner could see:

* ``local_loop:456`` -- the run's opening "Starting: <task>" line went to the
  ribbon only, so the floating companion window (which renders the
  ``computer_use.update`` topic) never heard a run begin.
* ``hart_intelligence_entry:3342`` -- shell commands went to the ribbon only,
  so that window never heard them at all.  Its ``finally`` hide also took the
  ribbon down mid-run when the shell ran INSIDE a VLM run, which is the
  common case (the tool reaches that function on the loop's own thread).
* ``local_loop:904`` -- a step that was REFUSED by the safety guard or that
  failed went to the topic only, so the ribbon sat there showing the step's
  intent as though the click had happened.

The fix is not a second feed; it is one announcer.  ``record_activity`` owns
both surfaces, ``finish_run`` closes both, and ``open_run`` resolves whether a
caller owns its run or joined one.  These guards fail against the pre-fix
tree -- the ribbon assertions see zero HTTP calls, and the source guard sees
the two direct writers.
"""
import os
import sys

import pytest

from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.vlm import activity_stream as acts  # noqa: E402

_BASE = 'http://127.0.0.1:5000'


class _Ledger:
    """Enough ledger for record_activity's already-has-a-task branch, which
    is the one every step after the first takes."""

    def __init__(self, ok=True):
        self.ok = ok
        self.contexts = []

    def get_task(self, task_id):
        return object()

    def update_task_context(self, task_id, context, defer_save=False):
        self.contexts.append(context)
        return self.ok


@pytest.fixture
def ribbon():
    """Collect what the ribbon is actually told, at the HTTP boundary.

    Asserted here rather than by patching a helper by name, so the guard is
    about the request Nunba receives -- it would still fail if the call were
    renamed or re-routed through a second helper.
    """
    calls = []
    with patch('core.config_cache.is_bundled', return_value=True), \
            patch('core.config_cache._local_base', return_value=_BASE), \
            patch('core.http_pool.pooled_get',
                  lambda url, timeout=None, **kw: calls.append(
                      (url.rsplit('/', 1)[-1],
                       (kw.get('params') or {}).get('text')))):
        yield calls


@pytest.fixture(autouse=True)
def _no_run_on_this_thread():
    """open_run publishes the run on the thread; one test's run must not leak
    into the next and silently turn an owner into a joiner."""
    from hartos.threadlocal import thread_local_data
    thread_local_data.clear_activity_run()
    yield
    thread_local_data.clear_activity_run()


def _record(**kw):
    kw.setdefault('user_id', 'u1')
    kw.setdefault('prompt_id', 'p1')
    kw.setdefault('run_id', 'r1')
    kw.setdefault('iteration', 1)
    kw.setdefault('action', 'left_click')
    return acts.record_activity(**kw)


def test_one_announcement_reaches_both_surfaces(ribbon):
    """The ribbon and the topic carry the SAME caption from ONE call.

    RED pre-fix: record_activity never touched the ribbon, so `ribbon` was
    empty and only the topic leg fired.
    """
    sent = []
    with patch.object(acts, '_ledger_for', return_value=_Ledger()), \
            patch.object(acts, '_fan_out',
                         lambda user_id, payload: sent.append(payload)):
        payload = _record(phase='executing',
                          caption='Open Settings from the Start menu')

    assert ribbon == [('show', 'Open Settings from the Start menu')]
    assert sent and sent[0]['caption'] == 'Open Settings from the Start menu'
    assert payload is not None


def test_a_refused_step_corrects_the_ribbon(ribbon):
    """A blocked step tells the ribbon the OUTCOME, not the stale intent.

    The loop keeps running after a blocked or failed step, so the ribbon is
    still on screen.  RED pre-fix: nothing was sent, and the ribbon kept
    showing the caption written before the action ran.
    """
    with patch.object(acts, '_ledger_for', return_value=_Ledger()), \
            patch.object(acts, '_fan_out', lambda user_id, payload: None):
        _record(phase='blocked', caption='Delete the folder',
                error='destructive command refused')

    assert len(ribbon) == 1
    where, text = ribbon[0]
    assert where == 'show'
    assert 'refused' in text and 'destructive command refused' in text
    assert text != 'Delete the folder'      # it does not repeat the intent


def test_a_completed_step_does_not_churn_the_ribbon(ribbon):
    """Success is silent: the next step's intent replaces the line anyway, so
    a tick per step would be noise.  This pins the CURRENT ribbon behaviour
    (only executing/blocked/failed speak) so a later widening is deliberate."""
    with patch.object(acts, '_ledger_for', return_value=_Ledger()), \
            patch.object(acts, '_fan_out', lambda user_id, payload: None):
        _record(phase='completed', caption='Open Settings')
    assert ribbon == []


def test_the_ribbon_is_not_hostage_to_the_ledger(ribbon):
    """A ledger failure must not leave the screen silent while the AI drives.

    The ribbon is the owner's only always-on signal that something else has
    their mouse and keyboard, so it is written BEFORE the durable leg and
    never gated on it.  RED pre-fix for a different reason: the ribbon was
    written by the caller, so this ordering was not expressible at all.
    """
    def _boom(*a, **kw):
        raise RuntimeError('ledger unavailable')

    with patch.object(acts, '_ledger_for', _boom):
        payload = _record(phase='executing', caption='Typing the address')

    assert ribbon == [('show', 'Typing the address')]
    assert payload is None          # the durable leg honestly reports failure


def test_an_unrecordable_step_still_reaches_the_ribbon(ribbon):
    """A caller with no run to record against -- the shell tool outside any
    VLM run, before open_run gives it one -- still gets the ribbon.  That is
    exactly what the old direct poke did, and dropping it would be the
    regression this refactor must not introduce."""
    acts.record_activity(user_id='', prompt_id='', run_id='', iteration=1,
                         action='shell', phase='executing',
                         caption='Shell command: dir')
    assert ribbon == [('show', 'Shell command: dir')]


def test_an_unknown_phase_announces_nothing(ribbon):
    with patch.object(acts, '_fan_out', lambda user_id, payload: None):
        assert _record(phase='not_a_phase', caption='x') is None
    assert ribbon == []


def test_finish_run_takes_the_ribbon_down(ribbon):
    """The run is over, so the ribbon must stop claiming the AI has the
    machine.  RED pre-fix: the hide lived in the caller (local_loop:970), so
    any other run-closer left it up until the 15 s inactivity timer."""
    with patch.object(acts, '_ledger_for', _Ledger()):
        pass
    acts.finish_run(user_id='', prompt_id='', run_id='', exit_reason='done')
    assert ribbon == [('hide', None)]


def test_a_joined_run_does_not_close_the_run_it_joined(ribbon):
    """The shell tool runs INSIDE a VLM run on the loop's own thread.  Its
    close must be a no-op there, or it takes the ribbon down and moves the
    run's task to a terminal status while the loop is still driving -- which
    is what its old `finally` hide did."""
    owner = acts.open_run(user_id='u1', prompt_id='p1')
    joiner = acts.current_run(user_id='u1', prompt_id='p1')

    assert joiner.run_id == owner.run_id, 'the tool did not join the live run'
    assert owner.owns is True and joiner.owns is False

    assert joiner.finish(exit_reason='done') is None
    assert ribbon == [], 'a joined run took the ribbon down'


def test_a_tool_with_no_run_around_it_gets_one(ribbon):
    """Same call, no enclosing run: it opens one and closes it.  One opener,
    one closer, and no branch at the call site."""
    run = acts.current_run(user_id='u1', prompt_id='p1')
    assert run.owns is True
    run.finish(exit_reason='done')
    assert ribbon == [('hide', None)]

    from hartos.threadlocal import thread_local_data
    assert thread_local_data.get_activity_run() is None, (
        'a closed run stayed on the thread; the next tool would join a '
        'finished run')


def test_a_crashed_run_does_not_poison_the_next_one(ribbon):
    """A run that dies before its close leaves its stamp on the thread.

    run_local_agentic_loop reaches run.finish() straight-line, with no
    `finally` -- the same shape its stop-registry pair already has -- so a
    raise above that line ends the run with the thread still stamped.  The
    next run must NOT inherit it: a joined run opens no ledger task of its
    own and its close is a no-op, so the ribbon would stay up through every
    later run on that thread.  open_run therefore always takes ownership.

    RED against my own first cut of this change, where open_run was the
    join-or-open call: `second.owns` came back False and `second.run_id`
    was the dead run's.
    """
    dead = acts.open_run(user_id='u1', prompt_id='p1')   # never finished

    nxt = acts.open_run(user_id='u1', prompt_id='p1')
    assert nxt.owns is True, 'a new run inherited a crashed run instead of owning one'
    assert nxt.run_id != dead.run_id

    nxt.finish(exit_reason='done')
    assert ribbon == [('hide', None)], 'the new run could not lower the ribbon'


def test_source_guard_only_the_announcer_writes_a_surface():
    """No module may reach the ribbon behind record_activity/finish_run.

    This is the invariant the whole change buys: surfaces have one writer, so
    they cannot tell the owner two different stories.  RED pre-fix: local_loop
    and hart_intelligence_entry both wrote /indicator/ directly.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    offenders = []
    for path in root.rglob('*.py'):
        parts = path.parts
        if any(p in ('venv', '.venv', 'node_modules', '__pycache__',
                     'tests', '.pycharm_plugin') for p in parts):
            continue
        if path.name == 'activity_stream.py':
            continue            # the one writer
        try:
            body = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if '/indicator/' in body:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        'these write the AI-control ribbon behind the announcer: '
        f'{offenders}')
