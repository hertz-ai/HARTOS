{ pkgs, hartSrc, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Phone Variant
# ═══════════════════════════════════════════════════════════════
#
# Native multi-platform phone OS:
#   - Linux apps (native, touch-adaptive via Phosh)
#   - Android apps (native ART — runs WhatsApp, banking, maps natively)
#   - AI agent (native, offloads LLM to hive peers)
#   - Nunba as primary management app
#   - Conky dashboard overlay
#
# For: PinePhone, PinePhone Pro, future ARM phones

{
  imports = [ ../profiles/phone.nix ];  # variant feature profile (hart.* block)

  # ─── HART OS Core Services: moved to ../profiles/phone.nix ───
  # The hart.* feature block (what makes the phone a phone) now lives in
  # profiles/phone.nix, imported above, so the SAME block can also drive the
  # nixosTest nodes (#15) and the installer (#17) without duplicating it here.
  # This file keeps only what is image/media-specific plus hart.package below.

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ─── Phone experience: moved to ../profiles/phone.nix (task #21) ───
  # The ENTIRE phone experience (apps, Phosh/greetd, phoc.ini, cellular,
  # power/tlp, pipewire, peripherals, autologin, tuning, journald caps) is
  # variant surface — phone.nix carries no live-CD machinery, so this file
  # is now just the profile import + hart.package.
}
