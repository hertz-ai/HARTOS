# HART OS — Rust-Native OS Layer Migration (architecture spec)

**Steward directive 2026-07-01:** "Rust native, fastest code possible, for all OS
code we have written — with ZERO regression, following the existing design where it
exists, aligned properly, like an actual OS architect would. HARTOS is used and
packed within Nunba — enforce the tiers + functional parity when creating Rust
equivalents. Verify where it can be verified, 100% new-code coverage."

This is the binding plan. It is a **strangler-fig re-platforming**, NOT a rewrite.

## 1. Boundary — what becomes Rust (steward-confirmed)
**RUST (OS / hardware / hot paths):**
- `hart-comp` compositor — already Rust (smithay). DONE.
- `hart-os-native` (NEW crate) — the system ops: power (logind), disk (udisks2),
  network (NetworkManager), display (wlr-output-management), behind the EXISTING
  contracts (`os_bridge.contract`, `POST /api/os/invoke`, the shell route JSON).
- session / seat / input / render hot paths (in the compositor).

**STAYS PYTHON (intelligence / glue — Rust would be wasted or harmful):**
- the AI / recipe / agent engine (the ML ecosystem lives in Python).
- the Model Bus ROUTING (a router; inference itself is already native llama.cpp).
- the WebView-serving Flask shell (glue; the UI is web tech).

## 2. Tiers + functional parity (LOAD-BEARING — do not break Nunba)
HARTOS runs in TWO tiers; parity is enforced per-op:

| Tier | Platform | Rust system-ops daemon | Fallback |
|---|---|---|---|
| **Standalone HART OS** | NixOS / Linux / real HW | YES — native logind/udisks/NM D-Bus | Python (busctl/nmcli) still present |
| **Packed in Nunba** (cx_Freeze frozen bundle) | Windows / macOS | NO logind/udisks/NM exist | **Python platform path is the ONLY path — MUST stay** |
| **Packed in Nunba** | Linux desktop | Rust daemon MAY run if bundled | Python fallback |

**Rule:** the Rust equivalent NEVER removes a capability. It ACCELERATES the
Linux-OS tier; the Python path remains the cross-tier fallback so the Nunba-packed
Win/macOS bundle is byte-for-byte unchanged. Every op returns the SAME contract
result on every tier. A parity-matrix test (op x tier) gates each domain's flip.
Packaging: the Rust binary ships in the NixOS closure (standalone) + optionally in
the Nunba Linux bundle; it is NEVER required for the Python-only tiers.

## 3. Strangler-fig migration (zero regression, existing-design-aligned)
For each domain, in order:
1. The Rust op implements the SAME contract the Python already exposes (no caller
   change: the WebView SDK, `/api/os/invoke`, the route JSON are unchanged).
2. It is proven against the SAME behavioural tests — **the Python tests are the
   PARITY ORACLE**: Rust output must equal the Python output for identical input.
3. It ships BEHIND the Python (feature-flag / capability probe): the Rust is used
   only when present + proven on that tier; the Python is the fallback.
4. Only after real-HW proof does the Rust become the primary for that tier. The
   Python is never deleted while any tier still needs it.

**Migration order (hottest / highest value first):**
`power` (smallest, native D-Bus already started in os_bridge) → `disk` → `network`
→ `display`. Compositor GLES/perf continues in parallel (already Rust).

## 4. Verification + 100% new-code coverage
- Rust: `cargo test` + `cargo clippy -D warnings` + `cargo llvm-cov` **100% on the
  new crate** (lines + branches). Property tests for the wire/arg translation.
- **Parity oracle:** a cross-language test asserts the Rust op's contract result ==
  the Python op's for the same input (the existing `test_os_bridge_power.py` etc.
  become the golden spec the Rust must match).
- Tiers: a parity-matrix test proves each op behaves per the table in section 2.
- Layered: dev-box (cargo/clippy/coverage) → CI (`nix build` the crate + nixosTests
  boot a node + exercise the op) → real-HW (the actual D-Bus call). Native D-Bus is
  CI/real-HW-gated; the Python fallback carries every tier until then (no regression).

## 5. Non-negotiables
- ZERO regression: the Python fallback stays until the Rust is real-HW-proven per
  tier; existing tests stay green throughout.
- Respect the concurrent sharded-inference session (`core/shard_runtime/` etc.) —
  do NOT touch or duplicate its files.
- #132 never-brick, the never-black tier ladder, the hang-free baseline all hold.
