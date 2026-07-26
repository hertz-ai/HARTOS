# HART-comp Milestone 2 — liquid-glass shell as a wlr-layer-shell client

These scripts prove, end-to-end and on real pixels, that **HART-comp paints the
HART OS liquid-glass shell as a `zwlr_layer_shell_v1` BACKGROUND surface**, nested
in WSL. No faking — a real layer-shell map + composite, captured with `grim`.

## Topology (proven on the WSL Ubuntu-22.04 dev box)

```
headless sway 1.7 (llvmpipe/pixman, user `sathish`, socket wayland-1)   ← Host
  └── HART-comp (winit/GlesRenderer, --features winit, socket wayland-2) ← our compositor
        └── layer-shell client on wayland-2:
              • swaybg                         (STAGE B — simple-client proof)
              • GTK4 + WebKitGTK-6.0 +         (STAGE F — the REAL glass shell)
                gtk4-layer-shell host, pointed at the in-process
                LiquidUIService glass shell (:6800 render_desktop_shell + /shell/static)
grim screenshots the SWAY host (it implements zwlr_screencopy_v1; HART-comp does not),
which composites HART-comp full-screen → the capture shows HART-comp painting the shell.
```

`grim`-on-the-host is the capture path because HART-comp's winit backend renders its
clients into its own GL framebuffer (submitted as HART-comp's window to sway); sway's
screencopy of that window IS HART-comp's composited output.

## Run order

| # | Script | Proves |
|---|--------|--------|
| 0 | `m2_check_deps.sh` | GTK4 / WebKit-6.0 / meson / gtk4-layer-shell present |
| 1 | `m2_launch_host.sh` | headless sway host up (socket under /run/user/1000) |
| 2 | `m2_nest_hartcomp.sh <hostsock>` | HART-comp nests + its socket advertises `zwlr_layer_shell_v1 v5` (weston-info) |
| 3 | `m2_swaybg_probe.sh <hartsock>` | swaybg maps a BACKGROUND layer → HART-comp logs `layer.mapped` + `layer.composited layers_painted=1` |
| 4 | `m2_run_glass_root.sh <hartsock>` | the REAL glass shell: serves LiquidUIService in-process, GTK4/WebKit host maps as a layer surface (`is_layer_window=True`), self-screenshots |

`m2_min_layer.py` is a HARTOS-free minimal gtk4-layer-shell client for isolating
"is gtk4-layer-shell correct" from the heavy WebKit/glass stack.

## Environment gotchas (recorded so the next run is fast)

- **HART-comp binary** lives under `/root` (mode 700, `sathish` cannot traverse) — the
  scripts stage it to `/tmp/hart-comp`. Edit the source on the Windows mount, `cp` to
  `/root/hart-comp/src/`, `cargo build --features winit`.
- **gtk4-layer-shell is NOT in apt** (only the GTK3 one) — build from source
  (`wmww/gtk4-layer-shell`, meson `-Dintrospection=true -Dvapi=false`, installs
  `Gtk4LayerShell-1.0.typelib` + `libgtk4-layer-shell.so` into `/usr/local`).
- **LD_PRELOAD is mandatory** for the GTK4 host: gtk4-layer-shell loaded late via
  Python GI links *after* libwayland-client and `gtk_layer_init_for_window()`
  silently no-ops ("GtkWindow is not a layer surface"). Preload
  `/usr/local/lib/x86_64-linux-gnu/libgtk4-layer-shell.so` — then
  `is_layer_window=True`. grim must run with LD_PRELOAD **stripped** (else grim
  itself tries to become a layer surface).
- **WebKitGTK sandbox**: the bwrap Web/Network-process sandbox can't set up mounts in
  the nested root→sathish WSL namespace (`readPIDFromPeer: short read` → core dump).
  Set `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` for the dev box (the shipped nix
  host runs under a normal session where the sandbox works).
- **Software GL floor**: `WEBKIT_DISABLE_DMABUF_RENDERER=1` +
  `WEBKIT_DISABLE_COMPOSITING_MODE=1` + `LIBGL_ALWAYS_SOFTWARE=1` (the cage/sway/
  hart-comp contract — a kiosk MUST paint on llvmpipe).
- **`python3-gi-cairo`** is needed for the DrawingArea cairo bridge (the minimal
  client); install via apt.
- **WSL `/tmp` DAC quirk**: root cannot redirect into a `sathish`-owned file on this
  mount — let the owning user's shell open the fd (`runuser -u sathish -- bash -c
  '... > log'`). The compositor / Python write their OWN status files (fsync'd) so
  evidence survives the harness's shell-stdout flakiness.

## Evidence

`../m2_artifacts/m2-glass-shell.png` — HART-comp displaying the glass shell (voice
orb + onboarding language picker) as a layer-shell BACKGROUND surface, 1280×800.
`../m2_artifacts/m2-baseline.png` — the swaybg STAGE-B solid-blue layer (simple-client
proof). `../m2_artifacts/m2-glass-status.txt` — the host's own milestone log
(`is_layer_window=True`, `WebView load FINISHED`, `grim rc=0`).
