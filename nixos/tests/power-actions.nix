# ═══════════════════════════════════════════════════════════════
# HART OS — Power-action polkit grant nixosTest  (#133)
# ═══════════════════════════════════════════════════════════════
#
# THE never-silent-no-op power guarantee, the half the Python unit tests CANNOT
# cover.
#
# WHY THIS EXISTS — the honest gap in the existing coverage:
#   tests/unit/test_shell_os_apis.py + test_shell_firmware_setup.py +
#   test_ws12_security_wiring.py prove the SHELL SERVER half of #133 thoroughly:
#   the power-action handler invokes the native logind Manager method via
#   `_logind_call` (busctl), CHECKS the exit status, and surfaces a real error on
#   a denial instead of the old fire-and-forget `{'initiated': True}` mask. BUT
#   every one of those tests MOCKS `_logind_call` — they assert the shell server
#   would REACT correctly to an allow/deny, NOT that the box actually AUTHORIZES
#   the call. The piece that decides allow-vs-deny on real hardware is the polkit
#   rule in nixos/modules/hart-base.nix (security.polkit.extraConfig) that grants
#   the `hart` service user the org.freedesktop.login1 power actions. NOTHING
#   tested that rule — so a regression in it (drop a verb, break the subject.user
#   check, mis-name an action id) would pass 100% of the Python suite while the
#   real box silently fell back to the polkit default (auth_admin → DENIED for a
#   sessionless daemon) and never powered down again — the EXACT #133 symptom.
#
# WHAT IT PROVES (behaviourally, on a real VM, NON-DESTRUCTIVELY — it never
# actually reboots/powers off):
#   1. polkit is up.
#   2. The `hart` service user (the uid the shell server runs as — a system
#      daemon with NO active graphical session) is AUTHORIZED for the login1
#      power actions: logind's Can{Reboot,PowerOff,Suspend,Hibernate} returns
#      "yes" (never "challenge"/"no") when asked AS hart. "yes" is exactly what
#      lets `_logind_call`'s busctl invocation exit 0; "challenge" is the #133
#      denial the masked-success bug hid.
#   3. The grant is SCOPED, not a blanket allow: a plain unprivileged user with
#      no session is NOT granted (it gets the polkit default "challenge"), so the
#      rule did not silently widen authority to everyone.
#
# We assert via logind's Can* probes (CanReboot/CanPowerOff/CanSuspend/
# CanHibernate) rather than the actual Reboot/PowerOff methods because Can* runs
# the IDENTICAL polkit decision for the SAME action ids against the SAME calling
# subject WITHOUT executing the action — a real authorization check that can never
# brick the test VM. CanReboot/CanPowerOff are always available; CanSuspend/
# CanHibernate may be "na" (capability absent) in a headless QEMU guest, so for
# those we assert only the safe invariant "hart is never CHALLENGED" (result in
# {yes, na}, never the #133 "challenge"/"no").
#
# [VM]-gated per the honest-hardware rule: a polkit+logind authorization decision
# cannot be exercised on the Windows dev box. This gates in CI (`nix flake check`
# / local QEMU), never grep.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-power-action-polkit-grant = pkgs.testers.runNixOSTest {
    name = "hart-power-action-polkit-grant";
    # Same false-positive static passes as the other desktop tests: the driver
    # injects the per-node Machine global at runtime; mypy/pyflakes can't see it.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.node = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
      # A plain unprivileged user with NO session — the negative control that
      # proves the hart grant is SCOPED. A sessionless non-hart caller must hit
      # the polkit default (challenge), not the hart YES grant. Pinning a uid
      # keeps it deterministic.
      users.users.powerprobe = {
        isNormalUser = true;
        uid = 4321;
      };
    };

    testScript = ''
      node = machines[0]
      node.start()
      node.wait_for_unit("multi-user.target")

      # polkit (the authority that evaluates the hart-base grant) must be up, and
      # logind (the seat/power manager the shell server's busctl call targets).
      with subtest("polkit + logind are up (the authorization + power authorities)"):
          node.wait_for_unit("polkit.service", timeout=60)
          node.wait_for_unit("systemd-logind.service", timeout=60)

      LOGIN1 = ("org.freedesktop.login1 /org/freedesktop/login1 "
                "org.freedesktop.login1.Manager")

      def can(user, method):
          # Ask logind to run the polkit decision for `method`'s action id against
          # `user` as the calling subject — WITHOUT performing the action. busctl
          # prints e.g.  s "yes" .
          out = node.succeed(
              f"runuser -u {user} -- busctl call --system {LOGIN1} {method}"
          ).strip()
          # Pull the quoted token out of  s "yes" .
          if '"' in out:
              return out.split('"')[1]
          return out

      # ── 2. The hart service user IS authorized (the #133 grant works) ──
      # This is the subject the LiquidUI shell server runs as: a SYSTEM daemon
      # with no active graphical session. Without the hart-base polkit rule it
      # would fall to the login1 default (auth_admin → "challenge") and the box
      # would silently never power down. The rule must flip it to "yes".
      with subtest("the hart user is GRANTED reboot + power-off (login1 returns yes, not challenge)"):
          for method in ("CanReboot", "CanPowerOff"):
              verdict = can("hart", method)
              assert verdict == "yes", (
                  f"hart {method} = {verdict!r}, expected 'yes' — the #133 polkit "
                  "grant for the hart shell user is missing or broken; on real HW "
                  "the power action would be DENIED and masked as a silent no-op"
              )

      with subtest("the hart user is NEVER challenged for suspend/hibernate (yes or na, never challenge/no)"):
          # Suspend/hibernate capability may be absent in a headless QEMU guest
          # ("na"); the invariant that matters for #133 is that hart is never met
          # with a "challenge"/"no" denial — the masked-success failure mode.
          for method in ("CanSuspend", "CanHibernate"):
              verdict = can("hart", method)
              assert verdict in ("yes", "na"), (
                  f"hart {method} = {verdict!r}; the grant must authorize hart "
                  "(yes) or the capability be absent (na), never deny (challenge/no)"
              )

      # ── 3. The grant is SCOPED to hart, not a blanket allow-everyone ──
      with subtest("a plain sessionless user is NOT granted (the rule did not widen authority)"):
          verdict = can("powerprobe", "CanReboot")
          assert verdict != "yes", (
              f"powerprobe CanReboot = {verdict!r} — an unprivileged sessionless "
              "user must NOT be authorized; the hart-base rule must grant ONLY the "
              "hart service user (or an active local seat), not everyone"
          )
    '';
  };
}
