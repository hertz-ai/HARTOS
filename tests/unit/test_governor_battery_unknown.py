"""A node must not suspend itself because a probe could not tell.

THE DEFECT, measured on the Samsung box 2026-09-09 with mains plugged in the
whole time:

    /sys/class/power_supply/ADP1/online     1            <- the kernel KNOWS
    /sys/class/power_supply/BAT1/status     Not charging
    /sys/class/power_supply/BAT1/capacity   1
    psutil.sensors_battery()  -> power_plugged=None, percent=1.0
    governor                  -> mode=sleep, throttle=0.0

psutil sets power_plugged=None when it CANNOT DETERMINE mains state, and
`not None` is True -- so `not battery.power_plugged` reported a plugged-in
machine as running on battery. Paired with a WORN battery reading 1%, that
reaches MODE_SLEEP, whose throttle is 0.0. Everything downstream stops: the
dispatch yield gate closes ('governor_throttle'), _proactive_check_tasks
returns before offering a single hive task, gpu_allowed goes False and
cpu_limit 0.0.

That one None is why the node's agent daemon logged a STARVATION OVERRIDE
every 30 seconds for its entire uptime, why six queued hive tasks were never
dispatched, and why the forced ticks kept a single-slot llama-server over
100% subscribed.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import core.resource_governor as resource_governor


def _battery(percent, plugged):
    return SimpleNamespace(percent=percent, power_plugged=plugged, secsleft=-1)


def _psutil_reporting(battery):
    return SimpleNamespace(sensors_battery=lambda: battery)


@pytest.fixture
def gov():
    return resource_governor.ResourceGovernor()


def test_a_definite_unplugged_is_still_respected(gov):
    with patch.object(resource_governor, '_try_import_psutil',
                      return_value=_psutil_reporting(_battery(50.0, False))):
        level, on_battery = gov._get_battery_status()
    assert on_battery is True
    assert abs(level - 0.5) < 1e-6


def test_a_definite_plugged_is_still_respected(gov):
    with patch.object(resource_governor, '_try_import_psutil',
                      return_value=_psutil_reporting(_battery(50.0, True))):
        _, on_battery = gov._get_battery_status()
    assert on_battery is False


def test_unknown_consults_the_kernel_and_believes_it(gov):
    """psutil could not tell, but /sys/class/power_supply usually can."""
    unsure = _psutil_reporting(_battery(1.0, None))
    for ac_online, expected_on_battery in ((True, False), (False, True)):
        with patch.object(resource_governor, '_try_import_psutil',
                          return_value=unsure), \
             patch.object(resource_governor.ResourceGovernor,
                          '_ac_online_from_sysfs',
                          staticmethod(lambda v=ac_online: v)):
            _, on_battery = gov._get_battery_status()
        assert on_battery is expected_on_battery, (
            'mains online=%s should mean on_battery=%s'
            % (ac_online, expected_on_battery))


def test_unknown_with_no_kernel_answer_assumes_mains(gov):
    """The asymmetry picks the default: guessing "on battery" suspends the
    whole node, guessing "plugged" merely lets background work run."""
    with patch.object(resource_governor, '_try_import_psutil',
                      return_value=_psutil_reporting(_battery(1.0, None))), \
         patch.object(resource_governor.ResourceGovernor,
                      '_ac_online_from_sysfs', staticmethod(lambda: None)):
        _, on_battery = gov._get_battery_status()
    assert on_battery is False


def test_the_box_scenario_no_longer_reaches_sleep(gov):
    """End to end on the exact readings the box produced."""
    with patch.object(resource_governor, '_try_import_psutil',
                      return_value=_psutil_reporting(_battery(1.0, None))), \
         patch.object(resource_governor.ResourceGovernor,
                      '_ac_online_from_sysfs', staticmethod(lambda: True)):
        level, on_battery = gov._get_battery_status()
    mode = gov._target_mode_for(True, 0.46, 0.30, level, on_battery)
    assert mode != resource_governor.MODE_SLEEP, (
        'a plugged-in node with a worn battery must not suspend itself')
    assert mode == resource_governor.MODE_IDLE


def test_a_genuinely_flat_battery_on_battery_still_sleeps(gov):
    """The protection this defends must keep working: really unplugged and
    really critical is exactly when SLEEP is correct."""
    with patch.object(resource_governor, '_try_import_psutil',
                      return_value=_psutil_reporting(_battery(1.0, False))):
        level, on_battery = gov._get_battery_status()
    mode = gov._target_mode_for(True, 0.10, 0.30, level, on_battery)
    assert mode == resource_governor.MODE_SLEEP
