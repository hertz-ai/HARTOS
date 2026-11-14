"""A person shows an agent how to do something: the demonstration recorder.

Owner 2026-11-11: "so you can start and complete it", on the design he agreed
on 11-10 -- when the person says "I'll show you how", the agent invokes the
demonstration tool; the same computer-use ribbon shows a recording symbol so
the person knows their screen actions are being recorded; it is time-boxed,
has an obvious Stop, keeps no key content, saves nothing to disk, and extends
the existing resource_governor hooks (no second listener).

"Local only" was said here before and overstated: the recorder itself keeps no
file and sends nothing anywhere, but the words it returns (window titles and
control names, never typed text) are handed to the model that runs the agent's
turn, and that model may be a hosted one.  The start tool tells the person
and the agent so (tests/unit/test_demo_session.py).  Whether the recorder
should refuse when the model in use is hosted is the owner's decision.

What a recording IS: WORDS.  integrations/vlm/local_loop.py re-grounds every
step from a fresh screenshot and OmniParser parse; coordinates are born and die
inside one iteration and nothing replays a stored position (claude-autostart-77
read it end to end, 2026-11-11).  So a step keeps the window, the element and
the action, in the action names the loop already has (activity_stream's
_ACTION_LABELS); a position is kept only as a tiebreak and never reaches the
words.  The words are what CREATE's existing gather step and the loop's prompt
already consume: there is no new executor and no replay path here.

What it never holds: what was typed.  core.resource_governor classifies a key
inside the hook and hands this module the token 'char'; the key code, which is
the character, never leaves the hook.  A run of typing is recorded as a count
and whether it went into a secret (a native password box, or a control
Accessibility calls protected; one that cannot be told counts as a secret).
Windows in front that safety.is_window_blocked names (password managers,
banking titles, UAC) are not recorded at all, and neither is a window that
cannot be identified.

The ribbon: nothing is captured until the ribbon is showing (show_ribbon says
whether Nunba answered), it is kept alive while recording, and the recording
ends if the ribbon is lost: a recording the person cannot see is not allowed.
Stop: the session registers in local_loop's stop registry, so the ribbon's own
Stop button (Nunba's indicator stop, which POSTs /api/vlm/stop scope=node) ends
it with no new route.  This module never talks to the ribbon itself: it asks
activity_stream, the one writer (tests/unit/test_one_announcement_two_surfaces).  Windows only in this version; other platforms refuse and
say so.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from integrations.vlm.activity_stream import show_ribbon

logger = logging.getLogger('hevolve.vlm.demo')

#: How long a demonstration may run when nobody says, and the most it may ever
#: run.  The recording ends by itself: a forgotten one must not keep watching.
DEFAULT_MAX_SECONDS = 300.0
HARD_MAX_SECONDS = 900.0
#: The ribbon hides itself 15 s after its last show request (Nunba
#: indicator_window.get_activity_timeout), so a recording asks again well inside.
RIBBON_KEEPALIVE_SECONDS = 5.0
#: Consecutive ribbon answers of "not showing" that end the recording.
RIBBON_MISSES_ALLOWED = 2
DOUBLE_CLICK_SECONDS = 0.5
DOUBLE_CLICK_PIXELS = 4
SCROLL_MERGE_SECONDS = 1.5
TYPING_GAP_SECONDS = 1.5
#: Rapid clicking for the 900 s hard box must not flood the model's context
#: (n_ctx 12288 on a desktop): the recording stops taking steps at MAX_STEPS
#: and says so, and the words show the first MAX_TRANSCRIPT_STEPS and how many
#: were left out.
MAX_STEPS = 400
MAX_TRANSCRIPT_STEPS = 60

_NO_RIBBON = ('the ribbon is not available in this build, so a recording cannot '
              'show you it is running')
_SETTLE = '__settle__'


class DemoRefused(Exception):
    """A recording that cannot start: what is wrong and what to do (owner rule
    11-09: every refusal says both)."""

    def __init__(self, problem: str, fix: str):
        super().__init__(f'{problem}. {fix}')
        self.problem, self.fix = problem, fix


# ── The operating system, kept small and replaceable ─────────────────────

def _on_windows() -> bool:
    return sys.platform == 'win32'


_ROLE_WORDS = {
    1: 'title bar', 2: 'menu bar', 9: 'window', 10: 'area', 12: 'menu item',
    15: 'document', 16: 'pane', 18: 'dialog', 22: 'toolbar', 29: 'cell',
    30: 'link', 33: 'list', 34: 'list item', 36: 'tree item', 37: 'tab',
    41: 'label', 42: 'text field', 43: 'button', 44: 'checkbox',
    45: 'radio button', 46: 'combo box', 47: 'drop-down', 51: 'slider',
    56: 'drop-down button', 62: 'split button'}


def _role_word(role) -> str:
    """An MSAA role number in words; anything unlisted is just a control."""
    try:
        return _ROLE_WORDS.get(int(role), 'control')
    except (TypeError, ValueError):
        return 'control'


def _process_name(pid) -> str:
    """The executable behind a process id: window_capture's one implementation
    (process_name_for_pid), not a second copy."""
    try:
        from integrations.remote_desktop.window_capture import process_name_for_pid
        return process_name_for_pid(int(pid)) or ''
    except Exception:
        return ''


def _window_of(hwnd) -> Optional[Dict[str, Any]]:
    """{hwnd, title, process} of the top-level window ``hwnd`` belongs to."""
    import win32gui
    import win32process
    root = win32gui.GetAncestor(hwnd, 2) or hwnd         # GA_ROOT
    _tid, pid = win32process.GetWindowThreadProcessId(root)
    return {'hwnd': int(root), 'title': win32gui.GetWindowText(root),
            'process': _process_name(pid)}


def _msaa_call(obj, dispid, child):
    import pythoncom
    return obj._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYGET, 1, child)


def _element_at(x, y) -> Optional[Dict[str, str]]:
    """The accessible control under a screen point: its role in words and its
    name.  Microsoft Active Accessibility through ctypes and pywin32 (UI
    Automation cannot be driven from win32com here: measured 2026-11-11)."""
    import ctypes
    from ctypes import byref, c_void_p, wintypes
    import pythoncom
    import win32com.client
    oleacc = ctypes.windll.oleacc
    oleacc.AccessibleObjectFromPoint.argtypes = [
        wintypes.POINT, ctypes.POINTER(c_void_p), c_void_p]
    oleacc.AccessibleObjectFromPoint.restype = ctypes.c_long
    pointer, child = c_void_p(), (ctypes.c_ubyte * 24)()
    if oleacc.AccessibleObjectFromPoint(wintypes.POINT(int(x), int(y)),
                                        byref(pointer), child) != 0 or not pointer.value:
        return None
    acc = win32com.client.Dispatch(
        pythoncom.ObjectFromAddress(pointer.value, pythoncom.IID_IDispatch))
    child_id = int.from_bytes(bytes(child[8:12]), 'little', signed=True)
    role = _msaa_call(acc, -5006, child_id)
    name = str(_msaa_call(acc, -5003, child_id) or '').strip()
    return {'role': _role_word(role), 'name': name[:80]}


def _describe_point(x, y) -> Optional[Dict[str, Any]]:
    """The window and control under a screen point, or None if neither can be
    identified."""
    import win32gui
    hwnd = win32gui.WindowFromPoint((int(x), int(y)))
    if not hwnd:
        return None
    return {'window': _window_of(hwnd), 'element': _element_at(x, y)}


def _describe_focus(hwnd) -> Optional[Dict[str, Any]]:
    """The window a keystroke went to, or None when it cannot be identified."""
    if not hwnd:
        return None
    return {'window': _window_of(hwnd), 'element': None}


def _msaa_focus_is_protected(hwnd) -> Optional[bool]:
    """Whether the control with the keyboard focus inside ``hwnd`` is a protected
    (password) field, by Accessibility; None when that cannot be told."""
    import ctypes
    from ctypes import byref, c_void_p, wintypes
    import pythoncom
    import win32com.client

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]
    iid = GUID()
    ctypes.windll.ole32.CLSIDFromString(
        '{618736E0-3C3D-11CF-810C-00AA00389B71}', byref(iid))     # IID_IAccessible
    pointer = c_void_p()
    oleacc = ctypes.windll.oleacc
    oleacc.AccessibleObjectFromWindow.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p)]
    oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
    if oleacc.AccessibleObjectFromWindow(
            hwnd, 0xFFFFFFFC, byref(iid), byref(pointer)) != 0 or not pointer.value:   # OBJID_CLIENT
        return None
    acc = win32com.client.Dispatch(
        pythoncom.ObjectFromAddress(pointer.value, pythoncom.IID_IDispatch))
    focused = acc._oleobj_.Invoke(-5011, 0, pythoncom.DISPATCH_PROPERTYGET, 1)  # accFocus
    if hasattr(focused, '_oleobj_') or hasattr(focused, 'Invoke'):
        target, child = win32com.client.Dispatch(focused), 0
    elif isinstance(focused, int) and focused != 0:
        target, child = acc, focused
    else:
        target, child = acc, 0
    state = int(_msaa_call(target, -5007, child))
    return bool(state & 0x20000000)                                # STATE_SYSTEM_PROTECTED


def _com_init():
    try:
        import pythoncom
        pythoncom.CoInitialize()
        return True
    except Exception:
        return False


def _com_done(initialised) -> None:
    if initialised:
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass


# ── The decision about a keystroke ───────────────────────────────────────

def _keystroke_is_secret(hwnd, native_password) -> bool:
    """Whether ONE keystroke, at the moment it was pressed, went into a secret.

    A classic Edit control answers itself (the hook read its ES_PASSWORD
    style).  Anything else -- a browser's own widget -- is asked of
    Accessibility, and a control that cannot be told (None, or any failure)
    counts as a secret: the guard errs towards hiding.
    """
    if native_password is True:
        return True
    if native_password is False:
        return False
    try:
        return _msaa_focus_is_protected(hwnd) is not False
    except Exception:
        return True


# ── Words ─────────────────────────────────────────────────────────────────

def _chord_words(chord: str) -> str:
    return '+'.join(p.upper() if len(p) == 1 else p.capitalize()
                    for p in str(chord).split('+'))


def _words(step: Dict[str, Any]) -> str:
    where = ''
    if step.get('app') or step.get('window'):
        where = f"In {step.get('app') or 'an app'}"
        if step.get('window'):
            where += f" ({step['window']})"
        where += ': '
    action = step['action']
    element = step.get('element') or {}
    target = (f'the {element["role"]} "{element["name"]}"'
              if element.get('name') else
              f'the {element["role"]}' if element.get('role') else 'in the window')
    if action in ('left_click', 'right_click', 'double_click'):
        verb = {'left_click': 'clicked', 'right_click': 'right-clicked',
                'double_click': 'double-clicked'}[action]
        body = f'{verb} {target}'
    elif action in ('scroll_up', 'scroll_down'):
        times = step.get('times') or 1
        body = (f"scrolled {'up' if action == 'scroll_up' else 'down'}"
                + (f' ({times} times)' if times > 1 else ''))
    elif action == 'type':
        if step.get('secret'):
            body = ('typed a secret (value not recorded; when this is replayed, ask '
                    'the person for it or use their vault)')
        else:
            body = f"typed {step.get('chars', 0)} characters (value not recorded)"
    elif action == 'hotkey':
        body = f"pressed {_chord_words(step.get('value', ''))}"
    else:
        body = f"pressed {_chord_words(step.get('value', ''))}"
    return where + body


def render_transcript(steps: List[Dict[str, Any]],
                      limit: int = None) -> str:
    """Numbered words for the steps, notes in brackets, no positions.  At most
    ``limit`` steps are shown (MAX_TRANSCRIPT_STEPS) and the rest are counted:
    the words go straight into a model's context."""
    limit = MAX_TRANSCRIPT_STEPS if limit is None else limit
    lines, late, n = [], [], 0
    for step in steps:
        if 'note' in step:
            text = f"(note: {step['note']})"
            # A note after the shown steps is NOT dropped: "not recorded" (the cap)
            # is a different fact from "not shown", and only the first loses what
            # the person demonstrated.  Once each, and no more than a few.
            if n <= limit:
                lines.append(text)
            elif text not in late and len(late) < 3:
                late.append(text)
            continue
        n += 1
        if n <= limit:
            lines.append(f'{n}. {_words(step)}')
    if n == 0:
        lines.insert(0, 'Nothing was recorded: no click, scroll or key press reached '
                        'the recorder while it ran.')
    elif n > limit:
        lines.append(f'...and {n - limit} more steps, not shown')
    return '\n'.join(lines + late)


class DemoResult:
    """What a finished demonstration hands back."""

    def __init__(self, steps, transcript, ended_by, seconds):
        self.steps, self.transcript = steps, transcript
        self.ended_by, self.seconds = ended_by, seconds


# ── The session ───────────────────────────────────────────────────────────

_active_lock = threading.Lock()
_active: Optional['DemoSession'] = None

# A recording the PERSON ended (the ribbon's Stop, the time box, a lost ribbon)
# is over before the agent asks for it: the agent's stop_demonstration comes on
# the person's next message.  Its result is kept here, per user, until it is
# read once or this long passes (a recording read half an hour later belongs to
# a different conversation), and a new recording drops it.  A recording the
# agent itself stopped is returned to it directly and is not kept.
_FINISHED_SECONDS = 1800.0
_finished: Dict[str, Tuple[float, 'DemoResult']] = {}


def active_session() -> Optional['DemoSession']:
    """The recording in progress on this machine, if any."""
    with _active_lock:
        return _active if _active is not None and _active.ended_by is None else None


def take_finished(user_id) -> Optional['DemoResult']:
    """What a recording that ended by itself (the person's Stop, the time box,
    a lost ribbon) saw, for ``user_id`` only, once: it is removed as it is
    returned, and None when there is none or it is older than
    _FINISHED_SECONDS."""
    with _active_lock:
        kept = _finished.pop(str(user_id), None)
    if kept is None or time.monotonic() - kept[0] > _FINISHED_SECONDS:
        return None
    return kept[1]


class DemoSession:
    """One person's demonstration: events in, steps out, words at the end."""

    def __init__(self, user_id, prompt_id, max_seconds):
        self.id = 'demo-' + uuid.uuid4().hex[:10]
        self.user_id, self.prompt_id = str(user_id), str(prompt_id)
        self.max_seconds = max_seconds
        self.steps: List[Dict[str, Any]] = []
        self.ended_by: Optional[str] = None
        self._t0 = time.monotonic()
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._queue: 'queue.SimpleQueue' = queue.SimpleQueue()
        # ONE object for attach and detach: the monitor takes its sink off by
        # comparison, and a fresh bound method is a new object every time.
        self._sink = self._queue.put
        self._done = threading.Event()
        self._closing = False
        self._stop_event: Optional[threading.Event] = None
        self._run: Optional[Dict[str, Any]] = None
        self._last_click = None
        self._in_sensitive = False
        self._capped = False
        self._cache: Dict[Any, Any] = {}
        self._threads: List[threading.Thread] = []
        self._result: Optional[DemoResult] = None

    # -- opening ----------------------------------------------------------

    def _ribbon_text(self) -> str:
        n = sum(1 for s in self.steps if 'action' in s)
        return (f'● REC  showing the agent how ({n} steps so far)  '
                f'press Stop AI control to end')

    def _open(self) -> None:
        """Start recording, in the order that keeps the person informed: the
        monitor can run, THEN the ribbon is up, THEN the stop is registered,
        THEN input is captured."""
        from core import resource_governor as rg
        from integrations.vlm import local_loop
        if rg.get_physical_input_state(start=True) is None:
            raise DemoRefused(
                "this computer's input monitor is not available, so a recording "
                'cannot watch your mouse and keyboard',
                'Run Nunba as a normal desktop app on Windows and try again; '
                'nothing was recorded')
        if not show_ribbon(True, self._ribbon_text()):
            raise DemoRefused(
                _NO_RIBBON,
                'Use the Nunba desktop app (its ribbon shows while a recording '
                'runs), or tell the agent the steps in words instead; nothing '
                'was recorded')
        self._stop_event = local_loop.register_stop_session(self.user_id, self.id)
        if self._stop_event is None:
            show_ribbon(False)
            raise DemoRefused('this recording could not be made stoppable',
                              'Try again; nothing was recorded')
        if not rg.attach_physical_input_sink(self._sink):
            local_loop.unregister_stop_session(self.user_id, self.id)
            show_ribbon(False)
            raise DemoRefused(
                'another part of this app is already listening to your input',
                'Wait a moment and try again; nothing was recorded')
        for name, target in (('consume', self._consume), ('watch', self._watch)):
            thread = threading.Thread(target=target, daemon=True,
                                      name=f'hart-demo-{name}')
            self._threads.append(thread)
            thread.start()
        logger.info('demonstration %s started (max %.0fs)', self.id, self.max_seconds)

    # -- threads ----------------------------------------------------------

    def _consume(self) -> None:
        com = _com_init()
        try:
            while not self._done.is_set():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item and item[0] == _SETTLE:
                    item[1].set()
                    continue
                try:
                    self.feed(item)
                except Exception:
                    # One bad event never ends a recording the person is
                    # in the middle of.
                    logger.exception('demonstration event skipped')
        finally:
            _com_done(com)

    def _watch(self) -> None:
        misses = 0
        while True:
            pressed = self._stop_event.wait(RIBBON_KEEPALIVE_SECONDS)
            if self._done.is_set():
                return
            if pressed:
                self.stop('stop button')
                return
            if time.monotonic() - self._t0 >= self.max_seconds:
                self.stop('time box')
                return
            if show_ribbon(True, self._ribbon_text()):
                misses = 0
            else:
                misses += 1
                if misses >= RIBBON_MISSES_ALLOWED:
                    self.stop('ribbon lost')
                    return

    def settle(self, timeout: float = 2.0) -> bool:
        """Wait until every event queued so far has been turned into steps."""
        marker = threading.Event()
        self._queue.put((_SETTLE, marker))
        return marker.wait(timeout)

    # -- events -> steps --------------------------------------------------

    def _since(self, t) -> float:
        return round(max(0.0, t - self._t0), 2)

    def _note(self, text, t=0.0) -> None:
        self.steps.append({'note': text, 't': self._since(t)})

    def _identify(self, describe) -> Optional[Dict[str, Any]]:
        try:
            return describe()
        except Exception:
            logger.debug('demonstration: nothing could be identified', exc_info=True)
            return None

    @staticmethod
    def _blocked(info) -> Optional[str]:
        """Why this event's window may not be recorded (safety.is_window_blocked:
        password managers, banking titles, UAC), or None.  THE one place it is
        decided; _probe asks it once and _admit reads the answer."""
        window = (info or {}).get('window')
        if not window:
            return None
        from integrations.vlm.safety import is_window_blocked
        return is_window_blocked({'title': window.get('title', ''),
                                  'process_name': window.get('process', '')})

    def _admit(self, info, t, blocked) -> Optional[Dict[str, Any]]:
        """The window of an event if it may be recorded, else None (after
        saying so ONCE per stretch: a sensitive window, or one that cannot be
        identified).  The title of a sensitive window is never kept."""
        window = (info or {}).get('window')
        if not window:
            self._flush_run(t)
            if not self._in_sensitive:
                self._note('a window could not be identified, so nothing was '
                           'recorded there', t)
                self._in_sensitive = True
            return None
        if blocked:
            self._flush_run(t)
            if not self._in_sensitive:
                self._note('a sensitive window was in front, so nothing was '
                           'recorded there', t)
                self._in_sensitive = True
            return None
        self._in_sensitive = False
        return window

    def _add(self, step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Append a step, or None (saying so once) when the recording already
        holds MAX_STEPS: the words go into a model's context."""
        if sum(1 for s in self.steps if 'action' in s) >= MAX_STEPS:
            if not self._capped:
                self._capped = True
                self.steps.append({
                    'note': f'the step limit ({MAX_STEPS}) was reached, so later '
                            f'steps were not recorded', 't': step.get('t', 0.0)})
            return None
        self.steps.append(step)
        return step

    def feed(self, event) -> None:
        """Turn one hardware event into at most one step.  The calls into other
        processes (which window, which control, whether it is a password field)
        are made BEFORE the lock is taken, so an application that has stopped
        answering can never make the person's Stop wait.  Never raises on an
        operating-system failure: what cannot be identified is not recorded."""
        t, kind, message, detail = event
        if self.ended_by is not None or self._closing:
            return
        probe = self._probe(t, kind, message, detail)
        with self._lock:
            if self.ended_by is not None or self._closing:
                return
            if kind == 'mouse':
                self._on_mouse(t, message, detail, probe)
            elif kind == 'keyboard':
                self._on_key(t, detail, probe)

    def _probe(self, t, kind, message, detail) -> Optional[Dict[str, Any]]:
        """Everything an event needs from other processes, asked outside the lock."""
        if kind == 'mouse':
            x, y, delta = detail
            if (x is None or message not in (0x20A, 0x201, 0x204)
                    or (message == 0x20A and not delta)):
                return None
            info = self._identify(lambda: _describe_point(x, y))
            return {'info': info, 'blocked': self._blocked(info)}
        if kind == 'keyboard':
            token, hwnd, native = detail
            info = self._identify(lambda: _describe_focus(hwnd))
            blocked = self._blocked(info)
            probe = {'info': info, 'blocked': blocked}
            if token == 'char' and (info or {}).get('window') and not blocked:
                probe['secret'] = self._cached(
                    (hwnd, native), t, lambda: _keystroke_is_secret(hwnd, native))
            return probe
        return None

    def _on_mouse(self, t, message, detail, probe) -> None:
        if probe is None:
            return
        x, y, delta = detail
        if message == 0x20A:                                   # wheel
            action = 'scroll_up' if delta > 0 else 'scroll_down'
        else:
            action = 'left_click' if message == 0x201 else 'right_click'
        info = probe['info']
        window = self._admit(info, t, probe.get('blocked'))
        if window is None:
            return
        self._flush_run(t)
        if not action.startswith('scroll'):
            # A click can move the focus into another field of the same window
            # (a browser's username and password are one window): the last
            # keystroke's secret answer is not this field's.
            self._cache.clear()
        if action.startswith('scroll'):
            last = self.steps[-1] if self.steps else None
            if (last and last.get('action') == action
                    and t - last['_t_last'] <= SCROLL_MERGE_SECONDS):
                last['times'] += 1
                last['_t_last'] = t
                return
            self._add(self._step(action, window, info, (x, y), t, times=1, _t_last=t))
            return
        previous = self._last_click
        if (action == 'left_click' and previous is not None
                and t - previous[0] <= DOUBLE_CLICK_SECONDS
                and abs(x - previous[1]) <= DOUBLE_CLICK_PIXELS
                and abs(y - previous[2]) <= DOUBLE_CLICK_PIXELS
                and self.steps and self.steps[-1] is previous[3]):
            previous[3]['action'] = 'double_click'
            self._last_click = None
            return
        step = self._add(self._step(action, window, info, (x, y), t))
        if step is None:
            return
        self._last_click = (t, x, y, step) if action == 'left_click' else None

    def _step(self, action, window, info, point, t, **extra) -> Dict[str, Any]:
        step = {'action': action, 'window': window.get('title', ''),
                'app': window.get('process', ''),
                'element': (info or {}).get('element'),
                'at': point, 't': self._since(t)}
        step.update(extra)
        return step

    def _on_key(self, t, detail, probe) -> None:
        token, hwnd, native = detail
        info = (probe or {}).get('info')
        window = self._admit(info, t, (probe or {}).get('blocked'))
        if window is None:
            return
        if token == 'char':
            # Asked of Accessibility outside the lock; a keystroke whose answer
            # is missing is a secret (the guard errs towards hiding).
            secret = (probe or {}).get('secret', True)
            run = self._run
            if run is not None and (run['hwnd'] != hwnd
                                    or t - run['t_last'] > TYPING_GAP_SECONDS):
                self._flush_run(t)
                run = None
            if run is None:
                run = self._run = {'hwnd': hwnd, 'window': window, 'info': info,
                                   'chars': 0, 'secret': False, 't': t, 't_last': t}
            run['chars'] += 1
            run['secret'] = run['secret'] or bool(secret)
            run['t_last'] = t
            return
        # Tab, Enter, Esc, the arrows and chords are how the focus moves: the
        # next keystroke's secret answer is asked again, not reused.
        self._cache.clear()
        self._flush_run(t)
        if token.startswith('hotkey:'):
            self._add(self._step('hotkey', window, info, None, t, value=token[7:]))
        elif token.startswith('key:'):
            self._add(self._step('key', window, info, None, t, value=token[4:]))

    def _cached(self, key, t, compute):
        """A keystroke's secret check, reused for a quarter second: typing is
        fast and the focus does not change between two letters."""
        hit = self._cache.get(key)
        if hit is not None and 0 <= t - hit[0] < 0.25:
            return hit[1]
        value = compute()
        self._cache[key] = (t, value)
        return value

    def _flush_run(self, t=0.0) -> None:
        """Close the typed run, if one is open, as ONE step: how many
        characters, whether a secret, and never what they were."""
        run, self._run = self._run, None
        if run is None:
            return
        self._add(self._step('type', run['window'], run['info'], None, run['t'],
                             chars=run['chars'], secret=run['secret']))

    # -- ending -----------------------------------------------------------

    def stop(self, why: str = 'person') -> DemoResult:
        """End the recording and return what it saw.  Idempotent: the first
        reason stands.  Capture stops first, then the ribbon comes down, so the
        ribbon never says 'not recording' while something still is."""
        from core import resource_governor as rg
        from integrations.vlm import local_loop
        global _active
        with self._stop_lock:
            if self._result is not None:
                return self._result
            rg.detach_physical_input_sink(self._sink)
            if any(t.is_alive() for t in self._threads):
                self.settle(1.0)
            with self._lock:
                self._closing = True
                self._flush_run()
            self._done.set()
            show_ribbon(False)
            local_loop.unregister_stop_session(self.user_id, self.id)
            if self._stop_event is not None:
                self._stop_event.set()
            seconds = round(time.monotonic() - self._t0, 1)
            steps = [{k: v for k, v in s.items() if not k.startswith('_')}
                     for s in self.steps]
            self._result = DemoResult(steps, render_transcript(steps), why, seconds)
            self.ended_by = why
            with _active_lock:
                if _active is self:
                    _active = None
                if why != 'person':
                    _finished[self.user_id] = (time.monotonic(), self._result)
            logger.info('demonstration %s ended by %s: %d steps in %.0fs',
                        self.id, why, sum(1 for s in steps if 'action' in s), seconds)
            return self._result


def begin(user_id, prompt_id, *, max_seconds: Optional[float] = None) -> DemoSession:
    """Start recording a demonstration, or raise DemoRefused saying what is
    wrong and what to do."""
    global _active
    if not _on_windows():
        raise DemoRefused(
            'a recording of what you do is only built for Windows so far',
            'Show the steps on a Windows desktop, or tell the agent the steps '
            'in words')
    try:
        seconds = float(max_seconds) if max_seconds is not None else DEFAULT_MAX_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_MAX_SECONDS
    if seconds <= 0:
        seconds = DEFAULT_MAX_SECONDS
    seconds = min(seconds, HARD_MAX_SECONDS)
    with _active_lock:
        if _active is not None and _active.ended_by is None:
            raise DemoRefused(
                'a demonstration is already being recorded on this computer',
                'Finish it first (the agent can stop it, or press Stop AI '
                'control on the ribbon)')
        session = DemoSession(user_id, prompt_id, seconds)
        session._open()
        _active = session
        _finished.pop(str(user_id), None)      # a new recording supersedes an unread old one
    return session
