"""Device Manager parity — device tree WITH driver binding and status.

WHY
───
The steward compared HART's "Drivers & Devices" panel against Windows'
Device Manager (2026-07-31) and the gap was real: `/api/shell/drivers` ran
`lspci -mm` + `lsusb`, joined the lines as opaque strings, and truncated at
50 with no indication anything was hidden. It reported hardware but not
whether the kernel had CLAIMED it — so the panel could not express the one
thing Device Manager exists to show: the yellow bang.

That matters concretely on this fleet. An Intel Wireless 3165 with no
firmware and a working NIC rendered identically, which is exactly the class
of bug that shipped server/edge images with no redistributable firmware at
all. `unclaimed` is the signal that would have surfaced it, and it is
machine-readable so an agent can act on it, not just a human.

`parse_lspci_k` is a pure function precisely so this is testable: the dev
box is Windows and has no PCI bus to enumerate, and CI has no real hardware
either. Fixtures below are real `lspci -mm -k` shapes.
"""
import pytest

from integrations.agent_engine.liquid_ui_service import (
    SHELL_DEVICE_CAP,
    parse_lspci_k,
)

# Real-shaped `lspci -mm -k`: a claimed bridge, an UNCLAIMED wifi NIC (the
# AC 3165 firmware case), and a claimed NVMe controller.
LSPCI_K = '''00:00.0 "Host bridge" "Intel Corporation" "Xeon E3-1200 Host Bridge" -r05 "Lenovo" "Device 2258"
\tKernel driver in use: skl_uncore
\tKernel modules: skl_uncore
02:00.0 "Network controller" "Intel Corporation" "Wireless 3165" -r79 "Intel" "Device 4010"
\tKernel modules: iwlwifi
03:00.0 "Non-Volatile memory controller" "Samsung" "NVMe SSD 970" -r00 "Samsung" "Device a801"
\tKernel driver in use: nvme
\tKernel modules: nvme
'''


class TestDriverBinding:
    """The half that was missing entirely."""

    def test_parses_every_device(self):
        assert len(parse_lspci_k(LSPCI_K)) == 3

    def test_claimed_device_reports_its_driver(self):
        nvme = parse_lspci_k(LSPCI_K)[2]
        assert nvme['driver'] == 'nvme'
        assert nvme['unclaimed'] is False

    def test_unclaimed_device_is_flagged(self):
        """THE yellow-bang case: modules exist but nothing is bound.

        `Kernel modules: iwlwifi` without `Kernel driver in use` means the
        driver is available but did not attach — the firmware-missing
        signature. Reporting this as claimed would hide the failure.
        """
        wifi = parse_lspci_k(LSPCI_K)[1]
        assert wifi['driver'] is None
        assert wifi['unclaimed'] is True
        assert wifi['modules'] == ['iwlwifi']

    def test_multiple_modules_are_split(self):
        out = parse_lspci_k('01:00.0 "VGA" "NVIDIA" "GA106"\n'
                            '\tKernel modules: nvidiafb, nouveau, nvidia_drm\n')
        assert out[0]['modules'] == ['nvidiafb', 'nouveau', 'nvidia_drm']

    def test_driver_value_is_stripped(self):
        out = parse_lspci_k('00:1f.3 "Audio" "Intel" "PCH"\n'
                            '\tKernel driver in use:   snd_hda_intel  \n')
        assert out[0]['driver'] == 'snd_hda_intel'


class TestParseRobustness:
    """Edge cases — a probe that raises takes the whole panel down."""

    def test_empty_output(self):
        assert parse_lspci_k('') == []

    def test_none_output(self):
        """run_probe returns None when lspci is absent; the route passes ''
        but the parser must tolerate None rather than depend on the caller."""
        assert parse_lspci_k(None) == []

    def test_stray_continuation_before_any_device(self):
        """Leading indented junk must not crash or invent a device."""
        assert parse_lspci_k('\tKernel driver in use: orphan\n') == []

    def test_continuation_without_colon_ignored(self):
        out = parse_lspci_k('00:00.0 "Host bridge" "Intel" "X"\n'
                            '\tsome malformed line with no separator\n')
        assert len(out) == 1
        assert out[0]['unclaimed'] is True

    def test_unknown_continuation_key_ignored(self):
        out = parse_lspci_k('00:00.0 "Host bridge" "Intel" "X"\n'
                            '\tPhysical Slot: 3\n')
        assert out[0]['driver'] is None
        assert out[0]['modules'] == []

    def test_blank_lines_between_devices(self):
        out = parse_lspci_k('00:00.0 "A" "v" "d"\n\n\n01:00.0 "B" "v" "d"\n')
        assert len(out) == 2

    def test_crlf_line_endings(self):
        """lspci output captured through a Windows-side pipe keeps \\r."""
        out = parse_lspci_k('00:00.0 "A" "v" "d"\r\n\tKernel driver in use: foo\r\n')
        assert out[0]['driver'] == 'foo'

    def test_every_device_has_the_full_shape(self):
        """The shell renders these keys unconditionally; a missing key is a
        client-side crash, so the contract is per-device, not best-effort."""
        for d in parse_lspci_k(LSPCI_K):
            assert set(d) == {'type', 'info', 'driver', 'modules', 'unclaimed'}
            assert d['type'] == 'pci'
            assert isinstance(d['modules'], list)


class TestDeviceCap:
    """The old cap of 50 silently hid a normal machine's tail."""

    def test_cap_is_above_a_real_machine(self):
        """A loaded workstation enumerates ~100 PCI+USB entries; 50 truncated
        real hardware with no signal to the user that it had done so."""
        assert SHELL_DEVICE_CAP >= 500

    def test_parser_itself_does_not_truncate(self):
        """Capping belongs to the route (which reports `truncated`), not the
        parser — a silently-lossy parser cannot be audited by its caller."""
        big = ''.join(f'00:{i:02x}.0 "Class" "Vendor" "Dev"\n' for i in range(600))
        assert len(parse_lspci_k(big)) == 600
