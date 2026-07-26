# Is it an OS?

Someone on Hacker News asked it plainly: *"Is it a model orchestrator? or a
wrapper around docker/nix/cgroups+chroot? Like what makes it an OS?"* Someone
else answered for us: *"Calling this shit an OS is a logic jump that no
computer science engineer would do."*

It is a fair question and it deserves evidence rather than adjectives. So
this page is the receipts. Every claim below names the file or the CI check
you can go read, and the last section is the part that is not proven yet.

## The short answer

It boots, it has its own compositor, and it supervises its own session. Those
are the three things a wrapper does not do.

## 1. It is a distribution, not a container

Five NixOS system configurations, in `nixos/configurations/`:

```
desktop.nix   edge.nix   phone.nix   server.nix   server-minimal-test.nix
```

These build bootable systems. Not images that run inside a host, and not a
chroot. The boot path itself is under test, which is the part worth checking
if you suspect otherwise:

| CI check | What it asserts |
|---|---|
| `hart-boot-root-initrd` | The initrd path. This is the earliest userspace there is. |
| `hart-boot-continuity` | The system comes back the way it went down. |
| `hart-boot-continuity-poweroff-gate` | Power-off ordering holds. |
| `hart-boot-log` | Boot logging is present and correct. |

You do not write an initrd test for a Docker wrapper.

## 2. It has its own Wayland compositor

`compositor/`, 22 Rust files. Smithay based, talking DRM and KMS directly
rather than running as a client inside someone else's session.

The build is deliberately split, and the split is worth explaining because
reading `Cargo.toml` alone gives the wrong impression:

- The **default** `cargo build` is pure logic with no Wayland linkage. The
  Smithay handler bodies are `todo!()` shims. This exists so a developer on a
  Windows or macOS box can `cargo check` without resolving a git dependency.
- The **real** compositor lives in `src/wayland.rs` and `src/udev.rs` and
  compiles under `--features smithay`, with git Smithay pinned to a revision
  and 245 crates vendored.

That second build is what CI runs. `nixos/modules/hart-comp.nix` sets
`buildFeatures = [ "smithay" ]`, `nix-build-matrix.yml` names `hart-comp` as
the M9 gate, and `flake-checks.yml` reports `Compositor build + cargo-test
(doCheck, --features smithay) GREEN`.

DRM, GBM, libinput, udev, libseat, pixman, XWayland. That is a display
server, not a wrapper around one.

## 3. It supervises its own session

This is the part that settles the argument, because session supervision is an
OS responsibility and nothing else has a reason to implement it.

Ten VM tests, all under `checks.x86_64-linux.`:

```
hart-session-supervisor-start-tier
hart-session-supervisor-tier-drop
hart-session-supervisor-paint-watchdog
hart-session-supervisor-paint-watchdog-keep
hart-session-supervisor-input-watchdog
hart-session-supervisor-input-watchdog-disabled
hart-session-supervisor-input-watchdog-keep
hart-session-supervisor-reboot-latch
hart-session-supervisor-recovery-tty
hart-session-supervisor-unhealthy-flag
```

Read what those names describe. A paint watchdog notices the screen has
stopped updating. An input watchdog notices the machine has stopped
responding. A tier drop falls back to a lesser session when the better one
fails. A recovery TTY is what you get when the graphical session cannot be
saved. A reboot latch stops a boot loop.

That is a list of ways a desktop dies and what happens next in each case. It
is the same problem set GNOME's session manager and systemd's logind exist to
handle. You do not need any of it to orchestrate models.

Plus `hart-floor-lock`, `hart-desktop-shell-boot`, `hart-layer-shell-host`,
`hart-layer-shell-host-paint` and `hart-hartlog-create`. Nineteen booted VM
checks in total, run under KVM by `nixos-vm-tests.yml`.

## 4. It can see and drive its own desktop

`integrations/vlm/local_computer_tool.py` is computer use running locally. A
vision model takes a screenshot, and the same action vocabulary you would
expect (`key`, `type`, `left_click`, and the rest) drives the real desktop
through pyautogui. Two tiers: in-process with no network at all, or HTTP to a
local GUI server.

The grounding detail is the part that shows it has been used rather than
written. Screenshots go to the model at a 1280px long edge with aspect ratio
preserved, because an earlier version forced 1024x576 and squashed 16:10
screens into 16:9, which made the model's vertical coordinates drift. That is
a bug you only find by driving a real screen and watching clicks land in the
wrong place.

There is a benchmark for it at `tests/vlm_grounding_benchmark.py`.

So the machine can operate its own GUI, including a browser, with the model
doing the seeing locally. Nothing is sent anywhere to decide where to click.

## 5. Inference is a system service

The part that earns the "AI-native" half of the label rather than the "OS"
half. An application does not bundle a model or hold an API key. It asks the
Model Bus over socket, D-Bus or HTTP, the same way it would ask any other
system service, and the OS decides which model answers.

The consequence is that ten applications on one machine do not load ten
copies of a model, and code written against `:6777/v1/chat/completions` runs
unchanged on a laptop and on a robot.

That is the claim in the name. Not that it orchestrates models, but that
inference is a system service instead of something every app carries itself.

## What is not proven

Held to the same standard as everything above.

- **Hardware paint on the target GPU.** Worth stating precisely, because
  there are two renderers and they are at different stages. The winit backend
  renders through `GlesRenderer`, which is GPU accelerated, and is the
  development and WSLg path. The DRM/udev backend does KMS scanout through
  `PixmanRenderer`, which is software, and that path is VM-proven with a real
  virgl-QEMU scanout PNG. What the compositor's own header at `main.rs:11`
  still marks as being verified is real-hardware paint on the target GPU. So
  there is a GPU path and there is proven KMS scanout, and the two have not
  yet been demonstrated together on the target device.
- **The full ISO build.** Flake evaluation is green. The complete
  `iso-desktop` build is the real gate for closure size and it is pending.
- **Daily-driver readiness.** Nothing here says this replaces your OS today.
  It says the work is OS work.

## Where that leaves it

If your objection is "this is not ready to be my operating system", you are
right, and nothing above disputes it.

If your objection is "this is not operating system work, it is a wrapper with
a grand name", the initrd test, the DRM compositor and the ten session
supervisor tests are the answer, and they are in CI where you can check them
rather than in a README where you have to believe them.
