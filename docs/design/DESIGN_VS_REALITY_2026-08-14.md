# Design vs Reality — full-system audit, 2026-08-14

Steward directive: "all the designed functionalities should work as per their
design intent — understand full design and check where things stand."

Evidence base: two real-hardware boots (Samsung 550P5C history + Lenovo 80XL
trial with 15-min soak), five qemu boots of the physical stick, a 35-minute
instrumented leak soak, the HARTJRNL black-box journal (1.8 MB), the module
tree's own design headers (70 modules), docs/architecture + the repo's honest
Q1–Q87 backlog (docs/design/HARTOS_FUNCTIONAL_GAPS.md), and the release-gate
CI results. This document records the DELTA between stated intent and observed
reality; the Q-backlog remains the feature-level worklist.

## 1. Verified working as designed (real evidence, this week)

| Subsystem | Design intent | Evidence |
|---|---|---|
| Boot chain (UEFI→systemd-boot→UKI→stage1→stage2) | never-brick USB boot | 2 real machines + 5 VM boots |
| Session tier ladder (degrade + latch) | never-blank-screen, monotonic drop | latched sway→cage across VM boots exactly per SESSION_TIER_CONTRACT |
| greetd autologin (no greeter) | appliance experience | real boot, user-observed |
| Glass shell renders (tier-2/3) | desktop paints on GPU floor | real HW `shell-ready`/`shell-render` markers, hardware GL verdict |
| memwatch | catch the slow-degrade leak class | caught the 9.2 GB leak with a clean time series |
| Boot-log / HARTJRNL / journal-export / shutdown capture | journal walks out of a wedged box | the black box solved the whole week |
| hartlog-create boot-disk guard | never complete GPT of the boot medium | correct NOOP decision logged |
| state-persist bind machinery | stateful live-OS paths | backing seed + binds observed |
| DB + migrations + agent_goals | schema converges on every boot | fixed tonight; 107 tables, zero errors, verified live |
| Audio unmute, settings pages, Wi-Fi UI | never boot silent; settings work | user-verified on real HW |
| dirty-bytes writeback, journald caps, RustDesk guard | slow-media stability | measured during the live-fix day |
| Panic self-reporting (kmsg stderr + pstore + panic=30) | no silent boot death | pstore mounts; cmdline armed; initrd patched |

## 2. Fixed in repo tonight — awaiting the next image to verify

| Fix | Commit | What it restores |
|---|---|---|
| hart.target ordering cycles (9 units) | 8db8d24 | systemd stops deleting an arbitrary boot job per boot; wine-model-bridge and other cycle victims actually run |
| Copilot PYTHONPATH as real env | 8db8d24 | the constitutional halt gate becomes readable (fails open by design, so it was silently absent) |
| DB ownership ExecStartPre | a2ddab0 | migrations run on every install forever (self-healing) |
| Wine smoketest → 10-min timer | a2ddab0 | boot loses the cold Wine prefix CPU storm |
| Per-build filesystem labels | 62de882 | two sticks / two builds can never be confused again |
| switch_root stderr→kmsg + efi_pstore in initrd | 62de882 | stage-1 deaths are never silent or unrecorded |
| power.enable (power-profiles-daemon, thermald, suspend+checkpoint) | 734a42f | designed power management stops being dead code; kills the PowerProfiles dbus timeout; thermald is the DESIGNED throttle mitigation for the box |
| hart.printing via its module | 734a42f | driver packs, print-to-PDF, hart-print CLI actually ship |
| Nunba daemon + LightYourHART preinstalled | f7f6bf0 | the canonical onboarding ships in-image (parity program P9) |

## 3. Broken or gapped vs design — open, ranked

1. **Release gate — WORKED THROUGH 2026-08-16.** 25 red families were
   enumerated and fixed (see the `test(gate)` / `fix(gate)` commits between
   268c90c and 3aaa9e2). The composition is the finding: exactly ONE
   production logic bug (dispatch.is_current_request_autonomous treated a
   missing request_id as autonomous — the unsafe direction), ONE real
   user-facing race (the onboarding probe, below), TWO undeclared
   dependencies production imported anyway (autobahn, pyautogen), and 21
   guards that had outlived their world: hardened security defaults the
   tests never arranged, a designed dedup wire-stamp byte-compared against
   pre-stamp literals, sharpened error messages grepped by their old vague
   wording, runtime schema mutation racing table creation, and runner
   environments leaking through test seams.
   The REFEREE was reformed alongside (962bcf9): each test FILE now runs in
   its own interpreter (cross-file pollution decided verdicts before —
   test_auth_local_csrf 11 red / test_remote_desktop_cli 9 errors both pass
   perfectly alone), red files land in the job summary + annotations, the VM
   fleet waits on the Python verdict instead of burning ~4 runner-hours
   after the run is already decided, and nightlies stamp the gate verdict in
   their release notes so a red-gated preview is visible at download time.
2. **Tier-1 hart-comp has never run on real hardware.** The latch carried
   sway/cage from GPU-less VM boots; now cleared. The box boot is the test.
   Design also promises latched-tier surfacing + `hartctl session reset-tier`;
   tonight required manual latch surgery — verify the designed reset path.
2b. **Onboarding + first-run password screens never appeared (FIXED
   e34486a).** hartOnboarding.js probed /api/onboarding/status + /start
   exactly ONCE at DOMContentLoaded and gave up silently on failure. The
   shell paints in seconds; the backend behind those endpoints imports the
   ML stack first and takes minutes on this hardware (hart-backend.nix pins
   TimeoutStartSec=600, documents ~170s, "far slower on USB"). So the
   ceremony asked before the backend could answer and never asked again —
   and because hartSessionUI.js adds the first-run password SETUP right
   after the ceremony, BOTH screens vanished on a fully healthy machine. Now
   a bounded retry (5s x 90) in the codebase's own documented
   "bounded retry, not one-shot" idiom; the invisible-overlay guard stands.

3. **Wi-Fi persistence — NOT a gap (my error, corrected 2026-08-16).** The
   credentials DO persist: the box holds
   `/etc/NetworkManager/system-connections/Lawliet-Giga.nmconnection` dated
   exactly when the steward typed the password, and the box is on Wi-Fi now.
   The original "EMPTY" reading was taken inside the QEMU VM, not on the box,
   and attributed to the box. On a raw-image install the whole root is
   writable, so NM keyfiles persist without any help.
   What IS inert here: `hart-state-persist` logs
   `DECISION=NOOP reason=no 'HARTSTATE'` every boot, because
   hart-repart-image.nix builds ESP + root only — there is no HARTSTATE
   partition on a raw image. That module exists for the read-only LIVE ISO
   case; on the installed image its job is already done by the writable root.
   Decide: create the partition for raw images too, or scope the module to
   the ISO and stop logging a NOOP that reads like a failure.
4. **Copilot daemon config is decorative.** The daemon reads no env vars;
   the unit's `HART_COPILOT_BACKEND/REPO/STOP` settings are inert (python
   constants win). Also unverified on-image: `claude` binary + authed `gh`
   (the resident-copilot design presumes both).
5. **OTA end-to-end unproven** on the current image lineage (its own header
   records the 2026-07-29 silent-canary defect as fixed, but no observed
   full cycle since).
6. **SSE fan-out**: `broadcast_sse_safe` is a designed silent no-op without
   Nunba's main module. With the Nunba daemon now preinstalled this path
   changes — verify agent.ui.update reaches the shell on the next image.
7. **Designed-but-never-demonstrated VM proofs**: the 19 manual-dispatch VM
   checks (session ladder among them) have no passing run on record
   (docs/IS_IT_AN_OS.md) and the four nixosTests shards fail every run.
8. **Orphan modules (design intent with no consumer)**: hart-gaming,
   hart-dlna, hart-peripheral-bridge. Decide: wire them or mark deliberate.
9. **Deliberate-off, correctly documented**: hart-sso (site config needed),
   hart-ideTools (+3 GiB), LUKS (passphrase decision) — now documented
   truthfully in the profile.
10. **Naming hazard**: hart-dev-tools.nix vs hart-devtools.nix remains.
11. **Multi-writer overrides on boot-critical units** (portal → liquid-ui,
    supervisor → greetd): by design today, but each is a place where two
    modules write one unit — audit-listed for single-writer refactor.
12. **Functional backlog**: the honest Q1–Q87 ledger stands (home composed by
    LLM: MISSING; unified Settings hub: MISSING; realtime voice: PARTIAL;
    power ops: STUB → partially addressed by power.enable tonight; Wi-Fi
    Settings panel: MISSING).

## 4. Shell leak — closed with data

Bare-metal trial (churn active): webkit_rss 0.77→9.2 GB in 30 min, GUI dead.
Same stick after the DB fix: 0.89→1.25 GB in 35 min, flat plateau, zero
`no such table` errors. The churn (readonly root-owned DB → dead migrations →
agent-daemon tick errors + SSE refusal storm every 15 s) was the driver. The
box boot's memwatch series is the free bare-metal confirmation.

## 5. Verification protocol for the next image boot

On the first boot of the 734a42f-lineage image (or the current stick on the
box): (1) `cat /var/lib/hart/session-tier` — expect `hart-comp` held, else
read `/run/hart/display-health`; (2) memwatch series stays flat over 30 min;
(3) `systemctl --failed` = 0; (4) wine-model-bridge + app-bridge +
compute-mesh + agent-monitor all active (cycle victims restored);
(5) powerprofilesctl works, thermald active; (6) journalctl has no
PowerProfiles dbus timeout; (7) `hart-print` exists; (8) LightYourHART
onboarding appears on first login; (9) deepbox hartos-watch.log records the
node; (10) copilot daemon ticks with halt gate READABLE (grep its log for the
gate read, not an ImportError swallow).
