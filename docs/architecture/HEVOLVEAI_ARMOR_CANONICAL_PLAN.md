# HevolveAI Armor — Canonical, Single-Path, Zero-Regression Plan

**Status:** proposed 2026-06-01. Execute phase-by-phase; each phase is additive,
flag-gated, independently revertible, and MUST leave every existing build green.

---

## ⚠ RECONCILED 2026-06-01 — the flag-gated phases below are SUPERSEDED

While executing P1–P4 (flag `HEVOLVE_HEVOLVEAI_ARMORED` + `_loader.install_loader`
spawn), an audit found a **canonical armor path already existed** and my
flag-gated work was a **parallel path** (the very thing principle #4 forbids):

- `app.py` already exports `HEVOLVE_ARMORED_DIR` / `HEVOLVE_ARMOR_KEY_FILE` and
  `security/native_hive_loader.py` already consults them to install the armor
  hook for the **in-process** hevolveai import. The Nunba freeze already stages
  the `.enc` bundle + installs the cp312 loader wheel **unconditionally**.
- My P2/P4 added a SECOND mechanism: a `HEVOLVE_HEVOLVEAI_ARMORED` flag + a
  different bundle dir convention. Different env vars, different gating.

**Reconciled design (one path, no flag):** the hevolveai **server subprocess**
installs the hook via the SAME contract as in-process — read
`HEVOLVE_ARMORED_DIR` + `HEVOLVE_ARMOR_KEY_FILE`, call
`hevolvearmor._loader.install_loader(dir, raw_key)` (the **raw-key** loader —
our producer writes a random `_key.bin`; the Rust `hevolvearmor.install` takes a
*passphrase* string and CANNOT open these bundles), **presence-gated, no flag**.
No-op when no bundle (dev unchanged). Lives in `_ARMOR_INSTALL_SNIPPET` in
`hevolveai_supervisor.py`; pinned by `tests/unit/test_supervisor_armored_spawn.py`
(env-var contract + a real producer→loader round-trip).

**Latent bug found (task #67):** `native_hive_loader.py:392` calls
`hevolvearmor.install(modules_dir, bytes(key))` — passing the raw key where a
passphrase string is expected → `TypeError`, caught silently → **in-process
armor has never actually worked**; it silently fell back to the `.pyd`. Fix is
to switch it to the same `install_loader(dir, raw_key)` (with a `.pyd` fallback
if the `.enc` is bad). Verify in a 3.12 build before relying on it.

**Net effect on the phases below:** P5's "flip the flag" is **moot** (no flag).
Remaining hardening = remove the stale committed `vendor/hevolveai_armored/`
bundle + gitignore it (build-produced), drop the raw-`.pyd` wheel fallback, and
fix #67. Commits: supervisor reconcile + flag removal (this change); P1–P4 flag
machinery reverted.

---

## Goal (the canonical principles)
1. **Zero file shadowing** — never both `.py` and `.pyd`/`.pyc` for the same module
   in a *shippable/importable* tree (root cause of the stale-`.pyd`-shadows-fixed-`.py`
   crash: 75 of 139 hevolveai `.pyd` are stale).
2. **No decompilable artifacts shipped** — protected code exists in the shipped tree
   **only** as Hevolvearmor AES-256-GCM `.enc`; no plain `.py`/`.pyc`/unencrypted `.pyd`.
3. **Only via Hevolvearmor** — one compile → encrypt → armored-load pipeline; nothing
   imports hevolveai any other way.
4. **No parallel build paths** — not "Nunba freeze armors / dev runs `.pyd` / central
   runs source." ONE path.
5. **Canonical scripts in HARTOS** — every variant (central docker, dev, Nunba bundle)
   invokes the SAME HARTOS-owned install script.

## Current state (grounded anchors)
- `scripts/armor_hevolveai.py` (HARTOS-owned ✅) — compiles current `.py` → `.pyc` →
  AES-256-GCM → `vendor/hevolveai_armored/modules/`; runtime import hook at
  `vendor/hevolveai_armored/_runtime.py`. **Fresh-from-source already.**
- `integrations/agent_engine/hevolveai_supervisor.py:_build_cmd` — spawns the **plain**
  path: `python -c "from hevolveai.server.api_server import app; uvicorn.run(...)"`
  (loads `.pyd` in bundle, `.py` via PYTHONPATH in dev). **No armored spawn.**
- `setup_freeze_nunba.py:1722` — the ONLY caller of `armor_hevolveai.py`; produces the
  `.enc` bundle into `build/Nunba/vendor/hevolveai_armored/` but the loader is
  **"not switched over yet"** → the produced bundle is **unused**; runtime still loads
  plain stale `.pyd` → the 14–22s `TypeError` crash-loop.
- `hevolveai/setup_cython.py` — separate `.pyd` compiler (parallel path #2).

## Builds that MUST NOT regress at ANY commit
| Variant | hevolveai today | invariant |
|---|---|---|
| **Nunba bundle** (frozen 3.12) | plain `.pyd` via supervisor | keeps booting + spawning hevolveai (or cleanly skipping it) |
| **Dev HARTOS** (3.11 conda) | `.py` via PYTHONPATH (cp312 `.pyd` ABI-skipped) | unchanged until opt-in |
| **Central/cloud HARTOS** (docker) | plain import | unchanged until opt-in |

## Phased plan (each: additive → tested → committed → revertible)

### Phase 0 — Harden the producer (HARTOS, additive, NO caller change)
- `armor_hevolveai.py`: operate on a **staging copy**, never the source repo; after
  producing `.enc`, **strip plain `.py`/`.pyc`/`.pyd` from the STAGED output only**
  (principle 1+2) so the armored bundle has zero shadow/decompilable. Idempotent.
- **Zero-regression:** no existing caller invokes the new strip behavior on a shipped
  tree yet; the produced bundle is still unused. Behaviour of all 3 builds unchanged.
- Test: producer on a fixture pkg → `.enc` present, no `.py/.pyc/.pyd` in staged out,
  source tree untouched.

### Phase 1 — Canonical HARTOS install entry + flag (additive, default OFF)
- One HARTOS function `ensure_hevolveai_armored()` (in `armor_hevolveai.py` or a thin
  `scripts/install_hevolveai.py`) = produce bundle + install loader + key. Gated by
  `HEVOLVE_HEVOLVEAI_ARMORED` (default **off**).
- **Zero-regression:** flag off → nobody calls it → no behaviour change anywhere.
- Test: flag off → no-op; flag on (fixture) → bundle+loader+key materialize.

### Phase 2 — Supervisor armored-spawn behind flag, plain fallback PRESERVED
- `_build_cmd`: if `HEVOLVE_HEVOLVEAI_ARMORED` on **AND** `vendor/hevolveai_armored/`
  + key present → boot with the armored import-hook preamble then the SAME
  `from hevolveai.server.api_server import app; uvicorn.run(...)`; **ELSE the existing
  plain boot, byte-for-byte.**
- **Zero-regression:** default (flag off / bundle absent) → identical plain cmd. The
  new branch only activates under explicit opt-in.
- Test: flag-on+bundle-present → armored cmd; flag-off OR bundle-absent → existing
  plain cmd (assert string-identical to today's).

### Phase 3 — Loader end-to-end verification (GATE; coordinate hevolveai session)
- Prove `vendor/hevolveai_armored/_runtime.py` decrypts + runs the `.enc` bundle: a
  real boot of `api_server` under the armored hook serves `/health` and does NOT
  `TypeError`. **No default flip until this passes.** Owned jointly with the
  hevolveai/armor session (they own keygen + `_loader`/`_runtime`).

### Phase 4 — Wire all variants to the canonical path (still fallback-preserved)
- HARTOS bootstrap (`hartos_bootstrap.py` / agent-engine init) calls
  `ensure_hevolveai_armored()` — variant-agnostic (central/dev/Nunba).
- `setup_freeze_nunba.py` reduced to **call the HARTOS canonical entry** (delete its
  own parallel armor logic) — but still also stages the plain path until Phase 5.
- **Zero-regression:** flag still default-off OR armored-verified-on; plain fallback
  remains in the supervisor.

### Phase 5 — Flip default + remove the parallel/plain path (final, only after P3 green)
- Default `HEVOLVE_HEVOLVEAI_ARMORED` on; supervisor armored-only (drop plain branch);
  canonical install strips plain artifacts from the shipped tree (principles 1–4 fully
  enforced); retire `hevolveai/setup_cython.py` as a build path (armor producer is the
  one compiler).
- **Rollback:** flip flag off → plain path returns (kept in git history one phase back).

## Zero-regression guarantees (the contract)
- **Source is never deleted** — only staging/output copies are stripped.
- **Every phase 0–4 keeps the plain path live**; only Phase 5 removes it, and only
  after Phase 3 proves the armored path end-to-end.
- **Flag default OFF** until verified → no variant changes behaviour by merely pulling.
- **Per-commit invariant:** all three builds boot + run hevolveai (armored if verified,
  else plain) at every commit. CI/behavioural test asserts `_build_cmd` falls back to
  the exact current plain string when armored is unavailable.

## Coordination
hevolveai/armor session owns: `hevolveai/*.py` source-of-truth, `hevolvearmor/_loader.py`
+ `_runtime.py`, keygen. HARTOS session owns: `armor_hevolveai.py` producer, supervisor
spawn, HARTOS bootstrap invocation, Nunba-freeze-calls-HARTOS. Phase 3 is the joint gate.

## Independent of Nunba bundling (the user's requirement)
Because the canonical entry lives in HARTOS bootstrap (Phase 4), **central/cloud/dev**
HARTOS get fresh-compiled + armored hevolveai too — the Nunba freeze becomes just one
caller, not the owner. Satisfies "any variant of HARTOS should do these."
