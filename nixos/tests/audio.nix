# ═══════════════════════════════════════════════════════════════
# HART OS - boot-time audio rescue (unmute + sane default volume) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves hart-audio.nix fixes the steward's real-HW "no audio out": a default
# sink that EXISTS but is MUTED / at volume 0 on boot. It is BEHAVIOURAL, not
# grep-on-source: it brings up a real PipeWire instance for a lingering user,
# loads a null sink (a real sink with no hardware), MUTES it + zeroes it, runs
# the ACTUAL rescue script the module ships, and asserts the sink came back
# UNMUTED + at the configured 60% floor.
#
# It also asserts the degrade contract on the real artifact:
#   * with NO PipeWire reachable, `hart-audio-unmute` exits 0 (clean no-op) - the
#     "no audio device" path never fails the session.
#   * a deliberate non-zero level is NOT clobbered by the rescue.
# And the WIRING: the graphical-session user unit is ordered after pipewire /
# wireplumber, runs the script with the configured volume, and is a oneshot.
#
# WHY [VM]-gated: a real PipeWire + a null sink need a Linux audio stack; it
# cannot run on the Windows dev box. The rescue's DECISION LOGIC is additionally
# covered by a portable behavioural unit test (tests/unit/test_hart_audio_unmute.py)
# that runs the same script against stub wpctl/pactl on ANY POSIX host.
#
# #70 discipline: built from `hartModules` alone via the shared `mkNode`
# (./lib.nix); self-contained - imports ../modules/hart-audio.nix directly so it
# runs whether or not flake.nix has registered the module yet.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-audio = pkgs.testers.runNixOSTest {
    name = "hart-audio";
    # Same runtime-injected node global the floor-lock / notify tests document:
    # the driver binds `machines` at runtime; skip the static passes.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.audionode = mkNode "desktop" {
      imports = [ ../modules/hart-audio.nix ];

      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };

      # A real PipeWire stack (the module gates on services.pipewire.enable).
      services.pipewire = {
        enable = true;
        alsa.enable = true;
        pulse.enable = true;
      };

      # The boot-time rescue under test (default-ON where PipeWire is on; explicit
      # here for clarity, with a known floor the assertions check).
      hart.audio.bootUnmute = {
        enable = true;
        bootVolumePercent = 60;
      };

      # A non-login user whose systemd instance + PipeWire we start via linger, so
      # we get a real per-user PipeWire socket without a graphical session.
      users.users.audiouser = {
        isNormalUser = true;
        uid = 1001;
        extraGroups = [ "audio" ];
      };

      # wpctl (wireplumber) + pactl (pulseaudio) on PATH for the test body to drive
      # + read the sink. (pulse.enable provides the pactl client; wireplumber the wpctl.)
      environment.systemPackages = [ pkgs.wireplumber pkgs.pulseaudio ];
    };

    testScript = ''
      audionode = machines[0]
      audionode.start()
      audionode.wait_for_unit("multi-user.target")

      uid = audionode.succeed("id -u audiouser").strip()
      RT = f"/run/user/{uid}"
      # Run a command inside audiouser's session with its runtime dir bound.
      def asuser(cmd):
          return audionode.succeed(
              f"runuser -u audiouser -- env XDG_RUNTIME_DIR={RT} sh -lc {cmd!r}")

      # ── 1. WIRING: the graphical-session user unit is correctly ordered ──
      with subtest("the user unit runs the rescue after pipewire/wireplumber, oneshot"):
          unit = audionode.succeed("cat /etc/systemd/user/hart-audio-unmute.service")
          assert "pipewire.service" in unit, "rescue must order after pipewire:\n" + unit
          assert "wireplumber.service" in unit, "rescue must order after wireplumber:\n" + unit
          assert "hart-audio-unmute 60" in unit, \
              "rescue ExecStart must pass the configured 60% floor:\n" + unit
          assert "Type=oneshot" in unit, "rescue must be a oneshot:\n" + unit
          # The hand-run CLI is on PATH for a recovery context.
          audionode.succeed("command -v hart-audio-unmute")

      # ── 2. DEGRADE: with NO PipeWire reachable the artifact exits 0 ──
      with subtest("no audio device -> hart-audio-unmute is a clean no-op (exit 0)"):
          # Run as root with an empty runtime dir: no PipeWire socket -> no sink.
          out = audionode.succeed(
              "XDG_RUNTIME_DIR=/run hart-audio-unmute 60 2>&1; echo RC=$?")
          assert "RC=0" in out, f"no-sink rescue must exit 0, got: {out!r}"
          assert "no default sink" in out or "no usable" in out, \
              f"no-sink rescue must log the clean no-op, got: {out!r}"

      # ── 3. Bring up the user's PipeWire + a real (null) sink ──
      audionode.succeed("loginctl enable-linger audiouser")
      # The user manager starts pipewire (socket-activated) under linger.
      audionode.wait_until_succeeds(f"test -S {RT}/pipewire-0", timeout=90)
      # WirePlumber needs a beat to enumerate; wait for wpctl to talk to the daemon.
      audionode.wait_until_succeeds("runuser -u audiouser -- "
          f"env XDG_RUNTIME_DIR={RT} wpctl status >/dev/null 2>&1", timeout=60)
      # A null sink = a real sink object with no hardware; make it the default.
      asuser("pactl load-module module-null-sink sink_name=harttest "
             "sink_properties=device.description=harttest")
      asuser("pactl set-default-sink harttest")
      audionode.wait_until_succeeds("runuser -u audiouser -- "
          f"env XDG_RUNTIME_DIR={RT} wpctl get-volume @DEFAULT_AUDIO_SINK@ "
          ">/dev/null 2>&1", timeout=30)

      # ── 4. THE bug: a MUTED, volume-0 default sink is rescued ──
      with subtest("muted + volume-0 default sink -> rescued to unmuted + 60%"):
          asuser("wpctl set-mute @DEFAULT_AUDIO_SINK@ 1")
          asuser("wpctl set-volume @DEFAULT_AUDIO_SINK@ 0")
          before = asuser("wpctl get-volume @DEFAULT_AUDIO_SINK@")
          assert "MUTED" in before, f"precondition: sink should be muted, got {before!r}"
          # Run the REAL rescue as the session user.
          asuser("hart-audio-unmute 60")
          after = asuser("wpctl get-volume @DEFAULT_AUDIO_SINK@")
          assert "MUTED" not in after, f"rescue must UNMUTE, got {after!r}"
          assert "0.60" in after, f"rescue must set the 60% floor, got {after!r}"

      # ── 5. NEVER CLOBBER: a deliberate non-zero level survives the rescue ──
      with subtest("a deliberate level is not clobbered (unmute only)"):
          asuser("wpctl set-mute @DEFAULT_AUDIO_SINK@ 1")
          asuser("wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.30")
          asuser("hart-audio-unmute 60")
          after = asuser("wpctl get-volume @DEFAULT_AUDIO_SINK@")
          assert "MUTED" not in after, f"rescue must still unmute, got {after!r}"
          assert "0.30" in after, \
              f"a deliberate 0.30 level must NOT be clobbered to 0.60, got {after!r}"
    '';
  };
}
