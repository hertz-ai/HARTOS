"""The demonstration recorder's hook: what the physical-input monitor hands a sink.

Owner 2026-11-11 ("so you can start and complete it"): a person can show an
agent how to do something and the agent learns from it.  The recorder must not
be a second keyboard listener: it is an optional sink on the ONE hardware
input monitor core.resource_governor already runs for takeover detection
(it already tells hardware input from injected input).

The rule these tests hold, structurally and not by discipline: WHAT WAS TYPED
NEVER REACHES THE SINK.  A character-producing key arrives as the token
'char'; its virtual-key code, which is the character, never leaves the hook.
So no consumer, log line or crash dump of the sink's queue can contain a
password.  (Review by claude-autostart-77, 2026-11-11: classify in the hook,
enqueue a token, decide secret / text / unknown per keystroke at record time.)

Run: python -m pytest tests/unit/test_demo_recording_hook.py -q
"""
import re
from types import SimpleNamespace

import pytest

from core import resource_governor as rg

LEFT_DOWN, RIGHT_DOWN, MOUSE_MOVE, WHEEL = 0x201, 0x204, 0x200, 0x20A
KEY_DOWN, SYS_KEY_DOWN, KEY_UP = 0x100, 0x104, 0x101
VK_A, VK_Z, VK_0, VK_9 = 0x41, 0x5A, 0x30, 0x39
VK_C, VK_RETURN, VK_TAB, VK_ESCAPE, VK_SPACE = 0x43, 0x0D, 0x09, 0x1B, 0x20
VK_BACK, VK_LEFT, VK_F5, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x25, 0x74, 0x10, 0x11, 0x12
VK_PACKET, VK_OEM_1, VK_NUMPAD5 = 0xE7, 0xBA, 0x65

TOKEN = re.compile(
    r'^(char|key:[a-z0-9_]{1,16}|hotkey:(?:(?:ctrl|alt|win)\+)+[a-z0-9_]{1,16})$')


@pytest.fixture
def monitor():
    """A monitor with no hook thread: record_event is driven directly."""
    return rg._WindowsPhysicalInputMonitor()


@pytest.fixture
def seen(monitor):
    events = []
    assert monitor.attach_sink(events.append) is True
    yield events
    monitor.detach_sink(events.append)


def _flat(value):
    """Every scalar inside a nested tuple/list, for 'is X anywhere in it'."""
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _flat(item)
    else:
        yield value


# ── What may reach the sink ───────────────────────────────────────────────

def test_a_hardware_click_reaches_the_sink_with_its_point(monitor, seen):
    monitor.record_event('mouse', 0, LEFT_DOWN, (10, 20, 0))
    assert len(seen) == 1
    _t, kind, message, detail = seen[0]
    assert (kind, message, tuple(detail)[:2]) == ('mouse', LEFT_DOWN, (10, 20))


@pytest.mark.parametrize('kind,flags,message', [
    ('mouse', 0x01, LEFT_DOWN),      # LLMHF_INJECTED: the agent's own click
    ('keyboard', 0x10, KEY_DOWN),    # LLKHF_INJECTED: the agent's own typing
])
def test_injected_input_never_reaches_the_sink(monitor, seen, kind, flags, message):
    monitor.record_event(kind, flags, message, ('char', 0, False)
                         if kind == 'keyboard' else (1, 2, 0))
    assert seen == []


def test_mouse_moves_never_reach_the_sink_but_still_count_as_activity(monitor, seen):
    """0x200 is in the monitor's 'presses' set, so takeover detection needs it;
    a 300 s demo would otherwise queue tens of thousands of moves."""
    monitor.record_event('mouse', 0, MOUSE_MOVE, (5, 5, 0))
    assert seen == []
    assert monitor._generation == 1


def test_a_key_release_never_arrives(monitor, seen):
    monitor.record_event('keyboard', 0, KEY_UP, ('char', 0, False))
    assert seen == []


@pytest.mark.parametrize('message', [0x207, 0x20B, 0x20E])
def test_gestures_the_recorder_has_no_word_for_are_not_offered(monitor, seen, message):
    """Middle and X buttons and the horizontal wheel: activity_stream's action
    names (the ones the computer-use loop has) have no word for them, so the
    recorder would do the work and throw it away (review, claude-autostart-77).
    They still count as takeover activity."""
    monitor.record_event('mouse', 0, message, (3, 4, 120))
    assert seen == []
    assert monitor._generation == 1


def test_the_wheel_reaches_the_sink_with_its_direction(monitor, seen):
    monitor.record_event('mouse', 0, WHEEL, (30, 40, -120))
    assert [tuple(e[3])[2] for e in seen] == [-120]


def test_takeover_detection_is_unchanged_with_a_sink_attached(monitor, seen):
    monitor.record_event('keyboard', 0, KEY_DOWN, ('char', 0, False))
    assert monitor._generation == 1 and monitor._last_input is not None


# ── What was typed never reaches the sink ─────────────────────────────────

CHARACTER_KEYS = (list(range(VK_A, VK_Z + 1)) + list(range(VK_0, VK_9 + 1))
                  + list(range(0x60, 0x70))          # numpad digits and operators
                  + list(range(0xBA, 0xC1)) + list(range(0xDB, 0xE0))   # OEM punctuation
                  + [VK_SPACE, 0xE2, VK_PACKET, 0xE5])  # OEM_102, IME/Unicode, PROCESSKEY


@pytest.mark.parametrize('vk', CHARACTER_KEYS)
def test_every_character_producing_key_is_only_the_token_char(vk):
    assert rg._classify_key(vk, ctrl=False, alt=False, win=False) == 'char'


def test_an_unknown_key_is_treated_as_a_character():
    """Fail closed: a code nobody classified might be anything."""
    assert rg._classify_key(0xFE, ctrl=False, alt=False, win=False) == 'char'


@pytest.mark.parametrize('vk,name', [
    (VK_RETURN, 'enter'), (VK_TAB, 'tab'), (VK_ESCAPE, 'esc'),
    (VK_BACK, 'backspace'), (VK_LEFT, 'left'), (VK_F5, 'f5')])
def test_a_non_character_key_is_named(vk, name):
    assert rg._classify_key(vk, ctrl=False, alt=False, win=False) == f'key:{name}'


@pytest.mark.parametrize('vk', [VK_SHIFT, VK_CONTROL, VK_MENU, 0x5B, 0xA0, 0xA3])
def test_a_modifier_alone_is_not_an_event(vk):
    assert rg._classify_key(vk, ctrl=False, alt=False, win=False) is None


def test_a_ctrl_chord_is_a_hotkey_not_content():
    assert rg._classify_key(VK_C, ctrl=True, alt=False, win=False) == 'hotkey:ctrl+c'
    assert rg._classify_key(VK_RETURN, ctrl=True, alt=False, win=False) == 'hotkey:ctrl+enter'
    assert rg._classify_key(VK_C, ctrl=False, alt=True, win=True) == 'hotkey:alt+win+c'


def test_ctrl_plus_alt_is_altgr_and_stays_a_character():
    """AltGr produces characters on many layouts (Ctrl+Alt+Q is '@' on a German
    keyboard): the chord reading would put the password's '@' in the log."""
    assert rg._classify_key(0x51, ctrl=True, alt=True, win=False) == 'char'


def test_shift_does_not_make_a_chord():
    assert rg._classify_key(VK_A, ctrl=False, alt=False, win=False) == 'char'


def _fed(monitor, vk, *, ctrl=False, alt=False, win=False,
         focus=(777, False), message=KEY_DOWN):
    monitor._modifier_state = lambda: (ctrl, alt, win)
    monitor._focus_state = lambda: focus
    monitor._on_hook_event('keyboard', SimpleNamespace(
        vkCode=vk, scanCode=0x1E, flags=0), message)


def test_the_hook_callback_hands_over_a_token_and_never_the_key_code(monitor, seen):
    _fed(monitor, VK_A)
    assert len(seen) == 1
    detail = seen[0][3]
    assert detail[0] == 'char'
    flat = list(_flat(seen[0]))
    assert VK_A not in flat and 0x1E not in flat and 'a' not in flat and 'A' not in flat


def test_every_keystroke_event_carries_only_a_token_even_if_the_hook_misbehaves(
        monitor, seen):
    """record_event refuses a detail that is not a token: a raw code, a
    character, nothing at all, all arrive as 'char'."""
    for bad in (None, (VK_A, 1, False), ('a', 1, False), ('KEY:Enter', 1, False),
                (65, None, None), ('hotkey:ctrl+', 1, False)):
        monitor.record_event('keyboard', 0, KEY_DOWN, bad)
    assert [e[3][0] for e in seen] == ['char'] * 6


def test_the_hook_callback_names_a_hotkey_from_a_key_down(monitor, seen):
    _fed(monitor, VK_C, ctrl=True)
    assert seen[0][3][0] == 'hotkey:ctrl+c'


def test_a_modifier_press_alone_enqueues_nothing(monitor, seen):
    _fed(monitor, VK_CONTROL)
    assert seen == []


def test_every_token_the_hook_can_produce_has_the_closed_shape(monitor, seen):
    for vk in range(256):
        for mods in ((False, False, False), (True, False, False),
                     (False, True, False), (True, True, False), (False, False, True)):
            _fed(monitor, vk, ctrl=mods[0], alt=mods[1], win=mods[2])
    assert seen, 'the sweep produced no events at all'
    assert all(TOKEN.match(e[3][0]) for e in seen)


def test_the_hook_callback_reads_a_mouse_point_and_a_wheel_delta(monitor, seen):
    monitor._on_hook_event('mouse', SimpleNamespace(
        point=SimpleNamespace(x=100, y=200), data=0, flags=0), LEFT_DOWN)
    monitor._on_hook_event('mouse', SimpleNamespace(
        point=SimpleNamespace(x=1, y=2), data=(-120 & 0xFFFF) << 16, flags=0), WHEEL)
    assert tuple(seen[0][3])[:2] == (100, 200)
    assert tuple(seen[1][3])[2] == -120


# ── The focus decision is made per keystroke, at the keystroke ────────────

def test_each_keystroke_carries_the_focus_it_had_when_it_was_pressed(monitor, seen):
    """Focus can change mid-run: a decision made later, at step assembly,
    would describe the wrong control (review, claude-autostart-77)."""
    _fed(monitor, VK_A, focus=(111, False))
    _fed(monitor, VK_A, focus=(222, True))
    assert [(e[3][1], e[3][2]) for e in seen] == [(111, False), (222, True)]


# ── The sink never breaks the hook, and there is only one ─────────────────

def test_a_sink_that_raises_does_not_break_takeover_detection(monitor):
    def broken(_event):
        raise RuntimeError('consumer bug')
    assert monitor.attach_sink(broken) is True
    monitor.record_event('mouse', 0, LEFT_DOWN, (1, 1, 0))
    assert monitor._generation == 1


def test_a_second_sink_is_refused_and_the_first_keeps_receiving(monitor):
    first, second = [], []
    assert monitor.attach_sink(first.append) is True
    assert monitor.attach_sink(second.append) is False
    monitor.record_event('mouse', 0, LEFT_DOWN, (1, 1, 0))
    assert len(first) == 1 and second == []


def test_detach_by_a_stranger_does_not_remove_the_sink(monitor):
    mine = []
    monitor.attach_sink(mine.append)
    monitor.detach_sink([].append)
    monitor.record_event('mouse', 0, LEFT_DOWN, (1, 1, 0))
    assert len(mine) == 1


def test_after_detach_nothing_is_delivered(monitor):
    sink = []
    put = sink.append
    monitor.attach_sink(put)
    monitor.detach_sink(put)
    monitor.record_event('mouse', 0, LEFT_DOWN, (1, 1, 0))
    assert sink == []


# ── Other platforms refuse instead of recording ───────────────────────────

@pytest.mark.parametrize('cls', [rg._MacPhysicalInputMonitor,
                                 rg._LinuxPhysicalInputMonitor])
def test_other_platforms_cannot_attach_a_sink(cls):
    assert cls().attach_sink([].append) is False


# ── The public door the recorder uses ─────────────────────────────────────

class _Stub:
    def __init__(self, available=True, supports=True):
        self.available, self.supports, self.sink = available, supports, None
        self.started = False

    def snapshot(self, start=True):
        self.started = self.started or start
        return (0, None) if self.available else None

    def attach_sink(self, sink):
        if not self.supports or self.sink is not None:
            return False
        self.sink = sink
        return True

    def detach_sink(self, sink):
        if self.sink is sink:
            self.sink = None


def test_attach_starts_the_one_monitor_and_attaches(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(rg, '_physical_input_monitor', stub)
    sink = [].append
    assert rg.attach_physical_input_sink(sink) is True
    assert stub.started and stub.sink is sink
    rg.detach_physical_input_sink(sink)
    assert stub.sink is None


def test_attach_is_refused_when_the_monitor_is_unavailable(monkeypatch):
    stub = _Stub(available=False)
    monkeypatch.setattr(rg, '_physical_input_monitor', stub)
    assert rg.attach_physical_input_sink([].append) is False
    assert stub.sink is None


def test_attach_is_refused_when_there_is_no_monitor_on_this_platform(monkeypatch):
    monkeypatch.setattr(rg, '_physical_input_monitor', None)
    assert rg.attach_physical_input_sink([].append) is False
    rg.detach_physical_input_sink([].append)   # must not raise
