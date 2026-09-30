"""Active takeover detection is independent of daemon idle scheduling."""
from unittest.mock import Mock
import pytest
from core import resource_governor as rg
from integrations.vlm import local_computer_tool as tool

@pytest.mark.parametrize('kind,flags,message', [
    ('keyboard',0x10,0x100), ('mouse',1,0x200),
    ('keyboard',0,0x101), ('mouse',0,0x202)])
def test_synthetic_and_release_events_do_not_take_over(kind,flags,message):
    monitor=rg._WindowsPhysicalInputMonitor()
    monitor.record_event(kind,flags,message)
    assert monitor._generation == 0

@pytest.mark.parametrize('kind,message',[('keyboard',0x100),('mouse',0x200),('mouse',0x201)])
def test_physical_input_advances_takeover_generation(kind,message):
    monitor=rg._WindowsPhysicalInputMonitor()
    monitor.record_event(kind,0,message)
    assert monitor._generation == 1
    assert monitor._last_input is not None

@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(rg,'get_physical_input_state',lambda **kw:(3,None))
    monkeypatch.setattr(tool,'foreground_window_handle',lambda:100)
    gui=Mock()
    monkeypatch.setattr(tool,'pyautogui',gui)
    return gui

def test_user_takeover_blocks_before_typing(guarded):
    result=tool._execute_inprocess({'action':'type','text':'never',
        '_human_input_token':2,'_expected_foreground':100})
    assert result['status']=='user_active'
    assert not guarded.method_calls

def test_focus_drift_requires_recapture(guarded):
    result=tool._execute_inprocess({'action':'left_click','coordinate':[1,2],
        '_human_input_token':3,'_expected_foreground':200})
    assert result['status']=='context_changed'
    assert not guarded.method_calls

def test_unchanged_context_allows_control(guarded):
    result=tool._execute_inprocess({'action':'left_click','coordinate':[1,2],
        '_human_input_token':3,'_expected_foreground':100})
    assert not result.get('error')
    assert guarded.method_calls

@pytest.mark.parametrize('action',['read_file_and_understand','write_file','list_folders_and_files','wait'])
def test_background_file_work_does_not_require_idle(guarded,action):
    assert tool._input_context_block({'action':action,'_human_input_token':1}) is None

def test_unavailable_monitor_withholds_interactive_input(guarded,monkeypatch):
    monkeypatch.setattr(rg,'get_physical_input_state',lambda **kw:None)
    result=tool._execute_inprocess({'action':'type','text':'never','_human_input_token':None})
    assert result['status']=='blocked'
    assert not guarded.method_calls

def test_unrelated_callers_keep_existing_contract(guarded):
    assert tool._input_context_block({'action':'type'}) is None

def test_character_typing_yields_after_takeover(guarded,monkeypatch):
    monkeypatch.setattr(tool,'pyperclip',None)
    generation=[3]
    monkeypatch.setattr(rg,'get_physical_input_state',lambda **kw:(generation[0],None))
    guarded.typewrite.side_effect=lambda *a,**kw:generation.__setitem__(0,4)
    result=tool._execute_inprocess({'action':'type','text':'abc','_human_input_token':3})
    assert result['status']=='user_active'
    assert guarded.typewrite.call_count==1

def test_clipboard_is_not_overwritten_after_user_copy(guarded,monkeypatch):
    clip=Mock()
    contents=['before']
    clip.paste.side_effect=lambda:contents[0]
    clip.copy.side_effect=lambda value:contents.__setitem__(0,value)
    monkeypatch.setattr(tool,'pyperclip',clip)
    monkeypatch.setattr(tool.time,'sleep',lambda _:contents.__setitem__(0,'user copied'))
    tool._type_text('agent text',{'action':'type','_human_input_token':3})
    assert contents[0]=='user copied'

@pytest.mark.parametrize('action',['screenshot','cursor_position'])
def test_passive_observation_does_not_require_idle(guarded,action):
    assert tool._input_context_block({'action':action,'_human_input_token':1}) is None

def test_clipboard_read_failure_does_not_cancel_a_completed_paste(guarded,monkeypatch):
    clip=Mock()
    clip.paste.side_effect=RuntimeError('clipboard busy')
    monkeypatch.setattr(tool,'pyperclip',clip)
    monkeypatch.setattr(tool.time,'sleep',lambda _:None)
    result=tool._execute_inprocess({'action':'type','text':'hello','_human_input_token':3})
    assert not result.get('error')
    guarded.hotkey.assert_called_once_with('ctrl','v')

def test_takeover_stops_real_loop_without_claiming_completion(monkeypatch,computer_control_granted):
    from integrations.vlm import local_loop,qwen3vl_backend
    class Backend:
        def __init__(self): self.calls=0
        def route_task(self,instruction): return 'single_shot'
        def try_taskbar_pre_check(self,*a,**kw): return None
        def detect_grounding_bias(self,*a,**kw): return None
        def _call_api(self,messages):
            self.calls+=1
            return '{"Reasoning":"type","Next Action":"type","value":"hello","Status":"IN_PROGRESS"}'
    backend=Backend()
    monkeypatch.setattr(qwen3vl_backend,'get_qwen3vl_backend',lambda:backend)
    monkeypatch.setattr(tool,'take_screenshot',lambda tier:'ZmFrZQ==')
    monkeypatch.setattr(tool,'execute_action',lambda *a,**kw:{
        'status':'user_active','error':'User took control','output':'','block_reason':'User took control'})
    monkeypatch.setenv('HEVOLVE_VLM_UNIFIED','1')
    result=local_loop.run_local_agentic_loop({
        'instruction_to_vlm_agent':'type hello',
        'enhanced_instruction':'type hello','user_id':'takeover-test',
        'prompt_id':'takeover-test','max_ETA_in_seconds':30},tier='inprocess')
    assert result['status']=='incomplete'
    assert result['exit_reason']=='user_active'
    assert backend.calls==1
    assert not result['extracted_responses'][0]['content']['ok']

@pytest.mark.parametrize('event_type,pid,expected',[
    (10,0,1),(5,0,1),(22,0,1),(10,123,0),(5,123,0),(11,0,0),(2,0,0)])
def test_macos_filters_posted_events_and_releases(event_type,pid,expected):
    monitor=rg._MacPhysicalInputMonitor()
    monitor.record_event(event_type,pid)
    assert monitor._generation==expected

@pytest.mark.parametrize('event_type,code,value,expected',[
    (1,30,1,1),(1,30,2,1),(1,30,0,0),
    (2,0,4,1),(2,0,0,0),(3,0,55,1),(0,0,0,0)])
def test_linux_hardware_event_filter(event_type,code,value,expected):
    monitor=rg._LinuxPhysicalInputMonitor()
    monitor.record_event(event_type,code,value)
    assert monitor._generation==expected

def test_unavailable_adapter_is_not_reported_as_idle(monkeypatch):
    monkeypatch.setattr(rg,'_physical_input_monitor',None)
    assert rg.get_physical_input_state() is None

def test_monitor_snapshot_does_not_start_for_nonstarting_read(monkeypatch):
    monitor=rg._MacPhysicalInputMonitor()
    assert monitor.snapshot(start=False) is None
    assert monitor._thread is None

def test_platform_monitor_does_not_change_daemon_idle_detection(monkeypatch):
    gov=rg.ResourceGovernor()
    gov._last_user_activity=0
    monkeypatch.setattr(gov,'_get_os_idle_ms',lambda:0)
    monkeypatch.setattr(rg,'get_physical_input_state',
        Mock(side_effect=AssertionError('takeover signal is not an idle clock')))
    assert gov._detect_user_idle() is False


def test_macos_modifier_release_after_approval_does_not_pause():
    monitor = rg._MacPhysicalInputMonitor()
    monitor.record_event(12, 0, 1 << 18)  # Physical Ctrl press.
    token = monitor._generation
    monitor.record_event(12, 0, 0)  # Ctrl release after approval.
    assert monitor._generation == token
    monitor.record_event(12, 0, 1 << 17)  # A fresh Shift press.
    assert monitor._generation == token + 1


def test_macos_injected_modifiers_do_not_poison_physical_state():
    monitor = rg._MacPhysicalInputMonitor()
    monitor.record_event(12, 123, 1 << 18)
    assert monitor._modifier_flags == 0
    assert monitor._generation == 0


@pytest.mark.parametrize('code,value,expected', [
    (47, 1, 0), (57, -1, 0), (24, 0, 0), (58, 0, 0),
    (57, 1, 1), (53, 20, 1), (54, 0, 1), (24, 10, 1)])
def test_linux_touch_slot_metadata_and_releases_are_not_takeover(code, value, expected):
    monitor = rg._LinuxPhysicalInputMonitor()
    monitor.record_event(3, code, value)
    assert monitor._generation == expected


def test_linux_monitor_survives_a_device_going_away(monkeypatch):
    """An unplugged device (EOF, or ENODEV on read) used to end the monitor
    thread, and nothing restarts it: every GUI action then read "monitoring
    unavailable" until the process restarted.  Drives the real _listen loop
    over real pipes standing in for /dev/input nodes."""
    import errno
    import os as _os
    import struct
    import time as _time

    pipes = {name: _os.pipe() for name in ('eof', 'enodev', 'kept')}
    for r, _w in pipes.values():
        _os.set_blocking(r, False)
    present = ['eof', 'enodev', 'kept']
    finished = []

    def devices():
        if finished:   # end the thread before the patches are undone
            raise RuntimeError('test over')
        return list(present)
    monkeypatch.setattr(rg._LinuxPhysicalInputMonitor, '_devices',
                        staticmethod(devices))
    real_open, real_read = _os.open, _os.read
    monkeypatch.setattr(rg.os, 'open',
                        lambda path, flags, *a: pipes[path][0] if path in pipes
                        else real_open(path, flags, *a))
    enodev_fd = pipes['enodev'][0]

    def read(fd, n):
        if fd == enodev_fd and read.armed:
            raise OSError(errno.ENODEV, 'No such device')
        return real_read(fd, n)
    read.armed = False
    monkeypatch.setattr(rg.os, 'read', read)

    monitor = rg._LinuxPhysicalInputMonitor()
    assert monitor.snapshot(start=True) is not None
    press = struct.pack('@llHHi', 0, 0, 1, 30, 1)

    # Device 1 unplugged: EOF.  Device 2 unplugged: ENODEV on read.  Each
    # leaves the device list BEFORE its failure is triggered, as a real
    # unplug does, so the next scan cannot reopen the dead node.
    present.remove('eof')
    _os.close(pipes['eof'][1])
    present.remove('enodev')
    read.armed = True
    _os.write(pipes['enodev'][1], press)
    _time.sleep(0.6)

    before = monitor._generation
    _os.write(pipes['kept'][1], press)
    deadline = _time.monotonic() + 3
    while monitor._generation == before and _time.monotonic() < deadline:
        _time.sleep(0.05)

    try:
        assert monitor._thread.is_alive()
        assert monitor._generation > before       # the kept device still counts
        assert monitor.snapshot(start=False) is not None
    finally:
        finished.append(True)
        monitor._thread.join(timeout=3)
    assert not monitor._thread.is_alive()
