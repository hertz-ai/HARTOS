# OTA on hardware: the part that still needs a box

Everything upstream of the reflash is proven and in `VERIFICATION.md`. This file
is the remaining steps, written so they are a paste rather than a re-derivation
the next time the node is up. Nothing here needs to be worked out again.

## What is already settled, so do not redo it

- The fleet cache is live and signed: 5,575 paths, 12G, 57 `nixos-system-hart-node`
  toplevels, newest 2026-09-04.
- 28 of those mount `/` by `hart-root` and `/boot` by `HART-ESP`. None carry the
  per-rev labels that made every cross-rev generation unmountable at stage 1.
- Both configs reach the cache each matrix run (they arrive in pairs).
- The raw toplevel's `systemd-boot` builder carries `bootctl install/update
  --graceful --no-variables`.
- `http://etime.hertzai.com:8093`, the substituter `hart-base.nix` pins, serves
  that toplevel's narinfo over the public internet, signed with the key pinned
  beside it.

So the build and the distribution are not in question. What is unproven is
whether a node BOOTS what it fetched, and whether it can go back.

## 0. Before the box is even up

The queue cannot reach CI until the GitHub email is verified at
<https://github.com/settings/emails>. Until then the node can only be given a rev
that CI already built, which is any of the 28 above.

## 1. Reflash

The stable-label image is the cutover. A node flashed with an OLDER image cannot be
saved by any of this: its `hart-ota` and its partition labels are baked in. That is
why it is a reflash and not an update.

## 2. Prove the fetch, before trusting the apply

On the node, with a rev the cache holds:

    nix path-info --store http://etime.hertzai.com:8093 \
      /nix/store/jzhhbf4d9hb2pdnis12cqlaz1myryyhj-nixos-system-hart-node-24.11.20250630.50ab793

A hit here means the substituter is reachable AND its signature verifies against
the pinned key. A miss is a cache or key problem, not an OTA problem, and the two
fail identically from the outside.

## 3. Apply

    systemctl start hart-ota-apply
    journalctl -u hart-ota-apply -b --no-pager | tail -40

Expect `staging <flake>#hart-desktop-raw for next boot`. The attr matters: it used
to build `hart-desktop`, the ISO-kind config, which can never mount a raw root.
Then expect `generation registered as boot default; reboot to activate` and
`/run/hart/ota-reboot-required` to exist.

`boot`, not `switch`, is deliberate: a compositor and shell change only takes
effect on reboot, and `switch` only made that look immediate while leaving the
booted kernel and half the units on the old generation.

Two things to check BEFORE rebooting, because they are cheap now and expensive
after a failed boot:

    ls /boot/loader/entries/            # a new entry appeared without hand-editing
    bootctl status | head -20           # no "not installed"; the ESP has its marker
    nix-env --list-generations -p /nix/var/nix/profiles/system | tail -3

## 4. Reboot, and read what actually happened

    systemctl reboot

If it comes up: `nixos-version`, `uname -a`, and confirm the generation number
moved. If it does NOT come up, the failure mode this whole cutover exists to fix
looks like a scripted stage 1 stopping at "must mount the root filesystem on
/mnt-root". That means the labels did not match, and the photo of that screen is
the useful artifact.

Recovery is the boot menu: `r`, then the previous generation.

## 5. Prove it can go BACK

An update that cannot roll back is not an update.

    nixos-rebuild switch --rollback
    # or, from the boot menu, the previous generation

Then confirm the generation number went back down and the desktop still comes up.

`configurationLimit = 2` is in the source and was NOT confirmed in the built
artifact, because it is passed into the builder rather than baked into the file
that was read. The ESP is 1G and a generation is roughly 90M, so if entries are
accumulating past two, that is where to look.

## What to paste back

The `journalctl -u hart-ota-apply -b` block, `bootctl status`, the generation list
before and after, and whether the rollback returned a working desktop. Pass or
fail both settle a row in `VERIFICATION.md`; a failure is worth as much as a pass
and moves up as a verified negative.

## Step 1 is DONE (2026-09-05)

The stick was reflashed from the 2026-09-04 nightly and verified three ways:

- the flasher's stream sha256 equals the published `.raw.sha256`
  (`ae718e6f4bfe...`), so the bytes decompressed were the published image;
- its full device read-back equals that stream, taken while it held the drive
  exclusively, which is the only moment a read of that disk is trustworthy;
- the UKI now on the ESP names
  `jzhhbf4d9hb2pdnis12cqlaz1myryyhj-nixos-system-hart-node-...` in its
  `.cmdline`, which is the SAME toplevel already verified in the fleet cache as
  mounting `/` by `hart-root` and `/boot` by `HART-ESP`.

Before: `ESP` / `nixos` labels and a UKI naming
`di98dhvpx7cx9q6lf9qb2v8jyl0z31q7`, a generation not present in the cache at all
and predating both label changes (62de882 per-build, c918a18 stable).

**A warning for whoever verifies this next.** Ad-hoc raw reads of
`\.\PhysicalDrive2` were STALE and disagreed with reality in both directions
during this work: before the flash Windows reported a cached `HART-ESP` on a stick
that raw bytes showed as `ESP`; after the flash the raw reader still showed the old
partition names while the filesystem showed the new ones. Neither is reliable on a
removable device that something else has had open. What settled it was reading the
ESP through the filesystem after `Update-Disk`, where the UKI is a different size
(76,337,664 vs 76,287,488) and `loader/random-seed` is gone.

So the remaining steps are 2 onward: boot it at the box, then apply, reboot,
rollback.
