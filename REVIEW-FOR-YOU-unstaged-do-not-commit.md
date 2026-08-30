# `hart-desktop-shell-boot` — cause in hand (but keep running the diagnostics)

**Correcting my own earlier framing**: I headed this "STOP, you do not need
another CI run". That was too strong, and `dee36039` proved it — the first
surviving run showed five checks PASSING and exactly ONE ❌
(`hart-layer-shell-host-paint`), with `hart-desktop-shell-boot` still evaluating
when the cap hit. So #15's "25 red" premise was wrong, and only the run could
show that. The observability work was worth more than I credited.

What still holds is the attribution below — it came from a log that was not
cancelled, so it does not depend on any future run.

You are adding the dispatch carve-out to nix-check.yml / nixos-vm-tests.yml
(correct — both expressions verified, same `run_id` shape as
nix-build-matrix.yml) so a manual run survives long enough to print
Determinate Nix's end-of-run summary. Six attempts, five cancellations, task #15
red since 07-26.

**The cause is already in hand. It came from a DIFFERENT log that did not get
cancelled** — the nixosTests shard 1/4 job (90978031171, run 30574137287)
printed it in full:

```
error: The option `nodes.shell.boot.kernel.sysctl."fs.inotify.max_user_watches"'
       is defined multiple times while it's expected to be unique.
Definition values:
  - In `.../nixos/modules/hart-kernel.nix':                 1048576
  - In `the argument that was passed to pkgs.runNixOSTest':   524288
```

Mapping verified before sending: `flake.nix:934`
`desktopShellBoot = import ./tests/desktop-boot.nix` (its comment names the attr
`hart-desktop-shell-boot`), and `desktop-boot.nix:80` is
`nodes.shell = mkNode "desktop" {...}` — the `nodes.shell` in the error.

Both sides are `mkForce` (priority 50). Equal priority, so neither wins and eval
aborts. Still live on the tip:

```
nixos/modules/hart-kernel.nix:128     lib.mkForce      1048576
nixos/tests/desktop-boot.nix:137      pkgs.lib.mkForce  524288
```

Unblock now: `lib.mkOverride 10 524288` in the test. Durable fix: one canonical
writer — there are FOUR for this single sysctl (hart-base:398 mkDefault,
hart-kernel:128 mkForce, hart-session-supervisor:1068 mkOverride 90,
desktop-boot:137 mkForce), and `hart-subsystems.nix:333` already carries a
comment saying it deliberately does not re-declare it.

Keep the carve-out fix anyway — it is right, and it is what will let the summary
print for `hart-layer-shell-host-paint`, which I do NOT have a cause for.

---

# Open review findings — rewritten 12:42, verified live against the tip (9e852b3c)

**Do not commit.** Untracked so it shows in `git status`. Delete once read.
Durable copy: `memory/review_open_findings_for_working_agent.md`.

Previous rounds accumulated; this replaces them. Everything below was
re-checked against the working tree just now — nothing here is stale, and
anything you already fixed has been dropped.

Credit where due first: `9e852b3c` is the right resolution of the revert —
tests patch the SEAM, both modules alias the one helper, and the identity guard
is the assertion the first attempt lacked. Verified on a clean tree: 371 passed.

---

## 1. The Device Manager ships INERT — `lspci -mm -k` prints no driver data

`liquid_ui_service.py:7203` still runs `_probe(['lspci', '-mm', '-k'])`.

pciutils `lspci.c::show_machine()` calls `show_kernel_machine()` only inside
`if (verbose)`. `-m`, `-v`, `-k` are three independent flags, so `-mm -k` has
`verbose == 0` and the `-k` block never prints. Every device parses with no
`Kernel driver in use` line, and your fail-safe `unclaimed=True` default turns
that into a 100% false alarm — the AC 3165 firmware case is indistinguishable
from a working NIC again, inverted. The fail-safe default is what hides it.

`-v` does not fix it: machine mode prints `Driver:` / `Module:` at column 0, one
line per module, so `if raw[0].isspace()` never fires (2 real devices → 14
parsed).

The 15 fixtures cannot catch it — `test_shell_device_manager.py:20` claims "real
`lspci -mm -k` shapes" while the same docstring says the dev box has no PCI bus.
They are a `-mm` device line spliced onto `-k` NON-machine continuation lines, a
hybrid no invocation produces.

**Capture `lspci -mm -k`, `lspci -mmv -k` and `lspci -k` on a node, verbatim, and
rebuild the fixtures from that.** `lspci -k` is the smaller change and matches
what the tests already encode.

## 2. `test_subprocess_safe.py:224` will CRASH Linux CI — open since 11:49

Still no `skipif` (0 occurrences in the file). It forces `sys.platform="win32"`,
calls `hidden_popen_kwargs()` → `subprocess.STARTUPINFO()`, and asserts on
`subprocess.CREATE_NO_WINDOW`. Verified against CPython source: both are inside
`if _mswindows:` (lines 89 and 192). Green here, `AttributeError` on ubuntu.

```python
@pytest.mark.skipif(sys.platform != "win32",
                    reason="subprocess.STARTUPINFO / CREATE_NO_WINDOW are Windows-only")
```

## 3. `/api/shell/antivirus/scan` accepts ANY path — and the file already has the fix

`shell_system_apis.py:685`: `os.path.abspath(target)`, then existence check.
Confirmed just now: no `realpath`, no `_is_path_allowed` anywhere in that
endpoint. `abspath` NORMALISES, it does not reject — `../../etc/shadow` →
`/etc/shadow`, accepted — and it does not resolve symlinks.

```
{"path": "/"}     -> 900s full-filesystem scan on demand
{"path": "<any>"} -> 202 vs 404 is a file-existence oracle
```

Three answers to one question now live in this file:

```
line  685  AV scan        abspath, no roots         accepts anything
line  812  storage/usage  isdir only, no roots      accepts any directory
line 1679  audio play     realpath + roots + 403    correct
```

`shell_os_apis.py:82` `_is_path_allowed()` is the canonical one. One line at 685
and one at 812.

## 4. `hart-desktop-shell-boot` — the cause you gave up attributing

Still live: `nixos/tests/desktop-boot.nix:137` `mkForce 524288` vs
`hart-kernel.nix:128` `mkForce 1048576`. Equal priority (50), neither wins:

```
error: The option `nodes.shell.boot.kernel.sysctl."fs.inotify.max_user_watches"'
       is defined multiple times
```

From nixosTests shard 1/4 (job 90978031171). Mapping verified: `flake.nix:934`
imports `tests/desktop-boot.nix` as that attr; `desktop-boot.nix:80` is
`nodes.shell`. Unblock with `lib.mkOverride 10 524288`; four writers of this one
sysctl is the real problem.

## 5. `hart-copilot-verify` is unbounded, and it is the heavy one

`hart-copilot.nix:343` — no CPUQuota, no MemoryMax, no TasksMax, no Nice
(confirmed: 0 bounds in that block). It runs `nixos-rebuild test`, a full closure
build, `TimeoutStartSec = 45min`, while its sibling daemon is capped at one core
and 2 GB. The polkit rule at `:360-368` exists so the daemon can start it — so
the bounded agent triggers the unbounded build.

Your file-level guard cannot see it (`NICE_MARKER.search(src) and "CPUQuota" not
in src` passes as soon as ANY unit in the file has CPUQuota). Block-level, per
`systemd.services.<name>`, catches this and the next one.

## 6. Coverage still measures 5 of 25 integrations packages

`dynamic_context = test_function` was the right idea, but contexts only record
for measured lines. Still 5 listed of 25 existing; `desktop/` and `hart_sdk/`
absent entirely. So `--show-contexts` answers correctly for five packages and
returns a confident EMPTY set for twenty — including `remote_desktop`,
`internal_comm`, `mcp`, `expert_agents` — which is the same NO-DATA-reads-as-
nothing shape that made you add `core`.

`source = .` with your existing `omit` closes it and inverts the default.

## 7. The dep-skip swallows the regression it guards

`test_latency_budgets.py` — `except ModuleNotFoundError` skips for BOTH causes:

```
e.name == 'dateutil'          environment gap      -> skip is right
e.name == 'core.user_context' the module VANISHED  -> skip is WRONG
```

Delete `core/user_context.py` and it goes green-by-skip while the DRY invariant
stops being enforced.

```python
if e.name and e.name.split('.')[0] == 'core':
    raise
```

## 8. Minor — `3e092867` says "one view of the disk tree, not a second"

It is the second. `_lsblk_devices()` (line 289) already requests
`NAME,PATH,TYPE,SIZE,ROTA,MODEL,MOUNTPOINT,FSTYPE` — every column the new
endpoint needs — while the endpoint runs its own `lsblk -o ... -P` with a
separate parser. Fourth lsblk parse path in that file (202, 292, 347, new).
The logic is correct (`TYPE == 'crypt'` is dm-crypt, not LVM — I checked); only
the sentence is wrong, and the sentence is what the next person trusts.
