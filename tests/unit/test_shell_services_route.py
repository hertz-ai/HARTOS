"""/api/shell/services — systemd unit state for the WHOLE OS (#25).

WHAT WAS WRONG
──────────────
The route knew seven hardcoded hart-* unit names and nothing else, so the
OS's own subsystems had no surface at all: "is sshd up", "is Samba sharing",
"is podman running" were unanswerable on an OS whose premise is that agents
drive it. It also spent one `systemctl is-active` SUBPROCESS PER UNIT — seven
processes per request to answer what systemd answers in one — using a raw
subprocess.run instead of the repo's canonical bounded probe.

WHAT THESE TESTS PIN
────────────────────
1. NOT INSTALLED is distinguishable from stopped. `systemctl is-active`
   collapses both to "inactive"; reporting an uninstalled Samba as merely
   stopped invites an agent to try starting it forever. This is why the
   implementation uses `systemctl show` and reads LoadState.
2. ONE systemctl invocation regardless of how many units are asked about.
   Asserted by counting calls, so a regression to per-unit probing fails here.
3. The default view still returns the hart-* names with the same
   {'name','status'} keys — the old callers' contract survives.
4. Honest degrade: systemd absent is available=False + 503, never an empty
   list, because "no services" and "could not ask" are opposite facts.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.agent_engine.shell_system_apis import (  # noqa: E402
    SERVICE_CATALOG, parse_systemctl_show, service_status)

# Real `systemctl show` output shape: KEY=VALUE blocks, blank-line separated.
SHOW_OUT = """\
Id=sshd.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled

Id=smbd.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=

Id=nfs-server.service
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=disabled
"""


class _R:
    """Minimal stand-in for the bounded probe's BoundedResult."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class TheParseIsPure(unittest.TestCase):

    def test_splits_blocks_into_units(self):
        units = parse_systemctl_show(SHOW_OUT)
        self.assertEqual(['sshd', 'smbd', 'nfs-server'],
                         [u['name'] for u in units])

    def test_not_found_is_NOT_installed(self):
        """The distinction is-active cannot express."""
        by = {u['name']: u for u in parse_systemctl_show(SHOW_OUT)}
        self.assertFalse(by['smbd']['installed'],
                         "a not-found unit was reported as installed")
        self.assertTrue(by['nfs-server']['installed'],
                        "nfs-server IS installed, merely stopped")
        # And the two must not look alike to a caller.
        self.assertNotEqual(by['smbd']['installed'],
                            by['nfs-server']['installed'])

    def test_keeps_active_and_sub_state_apart(self):
        by = {u['name']: u for u in parse_systemctl_show(SHOW_OUT)}
        self.assertEqual('active', by['sshd']['status'])
        self.assertEqual('running', by['sshd']['sub_state'])
        self.assertEqual('enabled', by['sshd']['enabled'])

    def test_empty_input_is_empty(self):
        self.assertEqual([], parse_systemctl_show(''))
        self.assertEqual([], parse_systemctl_show(None))


class ItAsksSystemdExactlyOnce(unittest.TestCase):

    def test_one_call_for_many_units(self):
        """A regression to per-unit probing fails right here."""
        calls = []

        def spy(argv, **kw):
            calls.append(argv)
            return _R(SHOW_OUT)

        with patch('integrations.agent_engine.shell_system_apis._run', spy):
            service_status(['sshd', 'smbd', 'nfs-server', 'podman', 'docker'])
        self.assertEqual(1, len(calls),
                         f"expected ONE systemctl call, got {len(calls)}")
        self.assertEqual('systemctl', calls[0][0])
        self.assertIn('sshd.service', calls[0])
        self.assertIn('podman.service', calls[0])

    def test_it_uses_the_canonical_bounded_probe(self):
        """Not a fresh subprocess.run — that is the repo-wide rule."""
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=_R(SHOW_OUT)) as probe:
            service_status(['sshd'])
        self.assertTrue(probe.called,
                        "service_status bypassed core.subprocess_safe")

    def test_a_bare_name_is_resolved_to_a_service_unit(self):
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=_R(SHOW_OUT)) as probe:
            service_status(['sshd'])
        self.assertIn('sshd.service', probe.call_args[0][0])

    def test_an_explicit_unit_type_is_preserved(self):
        """A .socket or .timer must not be rewritten to .service."""
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=_R(SHOW_OUT)) as probe:
            service_status(['sshd.socket', 'hart-ota.timer'])
        argv = probe.call_args[0][0]
        self.assertIn('sshd.socket', argv)
        self.assertIn('hart-ota.timer', argv)
        self.assertNotIn('sshd.socket.service', argv)

    def test_a_hostile_unit_name_never_reaches_systemctl(self):
        """Names come off a query string; shell metacharacters are dropped."""
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=_R('')) as probe:
            service_status(['sshd; rm -rf /', 'ok-unit', '$(whoami)'])
        argv = probe.call_args[0][0]
        self.assertIn('ok-unit.service', argv)
        for bad in argv:
            self.assertNotIn(';', bad)
            self.assertNotIn('$', bad)


class ItDegradesHonestly(unittest.TestCase):

    def test_absent_systemd_is_unavailable_not_an_empty_list(self):
        """'No services' and 'could not ask' are opposite facts."""
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=None):        # run_probe: absent or hung
            body = service_status(['sshd'])
        self.assertFalse(body['available'])
        self.assertEqual([], body['services'])
        self.assertIn('systemctl', body['error'])

    def test_an_oserror_is_reported_with_its_cause(self):
        with patch('integrations.agent_engine.shell_system_apis._run',
                   side_effect=OSError('boom-systemctl')):
            body = service_status(['sshd'])
        self.assertFalse(body['available'])
        self.assertIn('boom-systemctl', body['error'])

    def test_the_degrade_is_logged(self):
        from integrations.agent_engine import shell_system_apis
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=None), \
                patch.object(shell_system_apis.logger, 'warning') as warn:
            service_status(['sshd'])
        self.assertTrue(warn.called, "silent degrade")

    def test_no_names_is_an_empty_answer_not_a_systemctl_call(self):
        with patch('integrations.agent_engine.shell_system_apis._run') as probe:
            body = service_status([])
        self.assertTrue(body['available'])
        self.assertEqual([], body['services'])
        self.assertFalse(probe.called, "spawned systemctl for zero units")


class TheCatalogCoversTheGapsTaskTwentyFiveNamed(unittest.TestCase):

    def test_the_os_subsystems_are_reachable_by_name(self):
        flat = {n for g in SERVICE_CATALOG.values() for n in g}
        for expected in ('sshd', 'smbd', 'nfs-server', 'podman'):
            self.assertIn(expected, flat,
                          f"#25 named {expected} as a gap; the catalog must "
                          "make it askable without knowing HART's unit names")

    def test_the_hart_group_is_unchanged_for_existing_callers(self):
        self.assertEqual(
            ['hart-backend', 'hart-agent-daemon', 'hart-vision',
             'hart-llm', 'hart-discovery', 'hart-liquid-ui', 'hart-conky'],
            SERVICE_CATALOG['hart'],
            "the default view changed — old callers would see different names")


if __name__ == '__main__':
    unittest.main()
