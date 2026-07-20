"""
Power-session-suspend dimension — the suspend/resume HOOK <-> backend ROUTE
contract (audit failure mode #6, the dormant hart-power module).

These tests are the PORTABLE (dev-box) half of nixos/tests/power-suspend-resume.nix
(the VM half). They cannot boot a systemd suspend pipeline, so instead they prove
the cross-file CONTRACT that the VM test exercises end to end:

  * The suspend-checkpoint + resume hooks in nixos/modules/hart-power.nix curl a
    backend URL. That URL's PATH must be a route the shell server actually
    registers, or the hook silently 404s (the `|| echo` degrade branch masks it)
    and agent state is never checkpointed / the backend never reconnects. The
    original code curled /api/power/{checkpoint,resume} while the real routes are
    /api/shell/power/{checkpoint,resume} — a dormant-but-wrong path (Fix A).

  * The hooks must call an ABSOLUTE curl (Fix C): a bare `curl` is not guaranteed
    on the systemd unit PATH, so even with the right path + the backend up the
    POST would ENOENT into the degrade branch.

  * hart.power must be ENABLEABLE at all (Fix B): the module enabled BOTH
    power-profiles-daemon and TLP, which nixpkgs hard-asserts are mutually
    exclusive, so `hart.power.enable = true` failed eval — which is WHY the whole
    module sat dormant.

The route-existence + shape half is BEHAVIOURAL (builds the real Flask shell app
and drives it). The hart-power.nix-shape half is a clearly-labelled cross-file
source guard (test_source_guard_*) — the only way to catch a divergence between a
Nix systemd unit's curl string and the Python route map without a full VM boot; it
is paired with the behavioural route check so it is never the sole proof.
"""

import os
import re
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HART_POWER_NIX = os.path.join(_REPO_ROOT, "nixos", "modules", "hart-power.nix")


def _make_shell_app():
    """A fresh Flask app with every shell OS route registered (returns the app so
    the url_map can be inspected, plus a test client)."""
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    from integrations.agent_engine.shell_os_apis import register_shell_os_routes
    register_shell_os_routes(app)
    return app, app.test_client()


def _registered_paths(app):
    """The set of static route paths the shell server exposes."""
    return {rule.rule for rule in app.url_map.iter_rules()}


def _read_hart_power_nix():
    with open(_HART_POWER_NIX, "r", encoding="utf-8") as f:
        return f.read()


def _hook_post_paths(nix_src):
    """Pull the URL PATH out of every `curl ... -X POST "http://localhost:.../PATH"`
    in hart-power.nix (the checkpoint + resume hooks). Returns a list of paths like
    '/api/shell/power/checkpoint'."""
    # The URL is host:${toString cfg.ports.backend}<PATH> inside a double-quoted
    # string; capture the leading /api... path component.
    paths = re.findall(r'http://localhost:\$\{[^}]+\}(/[^"\s]+)"', nix_src)
    return paths


class TestSuspendResumeRouteContract(unittest.TestCase):
    """BEHAVIOURAL: the routes the hooks target exist + return the documented shape."""

    def test_checkpoint_route_is_live(self):
        _app, client = _make_shell_app()
        r = client.post("/api/shell/power/checkpoint")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["checkpointed"])

    def test_resume_route_is_live(self):
        _app, client = _make_shell_app()
        r = client.post("/api/shell/power/resume")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["resumed"])

    def test_every_hart_power_hook_path_maps_to_a_real_route(self):
        """THE cross-file contract: each POST URL the hart-power.nix hooks curl must
        be a route the shell server actually registers. A drift here (the original
        /api/power/* vs /api/shell/power/*) means the hook 404s and agent state is
        never checkpointed — silently, because the hook's `|| echo` masks it."""
        app, _client = _make_shell_app()
        registered = _registered_paths(app)
        hook_paths = _hook_post_paths(_read_hart_power_nix())
        # The two suspend/resume hooks must each contribute a POST path.
        self.assertGreaterEqual(
            len(hook_paths), 2,
            f"expected the checkpoint + resume hook POST URLs, found {hook_paths!r}")
        for path in hook_paths:
            self.assertIn(
                path, registered,
                f"hart-power.nix hook POSTs to {path!r}, which is NOT a registered "
                f"shell route -> the hook would 404 on a real box. Registered power "
                f"routes: {sorted(p for p in registered if 'power' in p)}")

    def test_checkpoint_and_resume_paths_are_among_the_hook_paths(self):
        """Belt-and-suspenders: the canonical checkpoint + resume routes are exactly
        the ones the hooks reference (so the contract test above is anchored to the
        right two endpoints, not vacuously satisfied)."""
        hook_paths = _hook_post_paths(_read_hart_power_nix())
        self.assertIn("/api/shell/power/checkpoint", hook_paths)
        self.assertIn("/api/shell/power/resume", hook_paths)


class TestSourceGuardHartPowerNix(unittest.TestCase):
    """Cross-file SOURCE GUARDS (paired with the behavioural route checks above) —
    they catch a regression in the Nix systemd units that no Python-only behavioural
    test can see because the units never run on the dev box."""

    def test_source_guard_no_legacy_unprefixed_power_route(self):
        """Fix A regression guard: every URL the hooks actually CURL must be under
        /api/shell/power/ — never the OLD unprefixed /api/power/{checkpoint,resume}
        that 404s. Checks the extracted curl URLs (not raw text), so an explanatory
        comment that mentions the old path does not false-trip it."""
        hook_paths = _hook_post_paths(_read_hart_power_nix())
        self.assertTrue(hook_paths, "no hook POST URLs found in hart-power.nix")
        for path in hook_paths:
            self.assertTrue(
                path.startswith("/api/shell/power/"),
                f"hart-power.nix hook curls {path!r}, not under /api/shell/power/ "
                "(the canonical route prefix) -> the hook 404s on a real box")

    def test_source_guard_hooks_use_absolute_curl(self):
        """Fix C regression guard: the suspend/resume hooks must call curl by an
        ABSOLUTE store path (${pkgs.curl}/bin/curl), never a bare `curl` that may be
        absent from the systemd unit PATH."""
        src = _read_hart_power_nix()
        # Every curl invocation in the file must be an absolute /bin/curl, not a
        # bare `curl ...` at a command position.
        bare = re.findall(r'(?<![\w/.${}])curl\s+-', src)
        self.assertEqual(
            bare, [],
            "hart-power.nix invokes a BARE `curl` (not ${pkgs.curl}/bin/curl) — it "
            "may not be on the systemd unit PATH, so the POST ENOENTs into the "
            "degrade branch and never reaches the backend")
        # And the absolute form is present for the checkpoint + resume hooks.
        self.assertGreaterEqual(
            src.count("/bin/curl"), 2,
            "expected an absolute /bin/curl in both the checkpoint and resume hooks")

    def test_source_guard_tlp_gated_off_when_power_profiles_daemon_on(self):
        """Fix B regression guard: power-profiles-daemon + TLP are mutually
        exclusive (nixpkgs hard-asserts it). hart-power.nix enables ppd, so its TLP
        block must be gated on ppd being OFF, otherwise `hart.power.enable = true`
        fails eval — the reason the whole module was un-enableable / dormant."""
        src = _read_hart_power_nix()
        self.assertIn(
            "services.power-profiles-daemon.enable = true", src,
            "hart-power.nix must enable power-profiles-daemon (the HART shell's "
            "profile daemon)")
        # The TLP mkIf condition must reference ppd being disabled so the two never
        # co-enable.
        self.assertRegex(
            src,
            r"services\.tlp\s*=\s*lib\.mkIf\s*\([^;]*!\s*config\.services\.power-profiles-daemon\.enable",
            "hart-power.nix enables TLP without gating it on power-profiles-daemon "
            "being off -> both enable -> the nixpkgs mutual-exclusion assertion "
            "fails eval (the module can never be turned on)")


if __name__ == "__main__":
    unittest.main()
