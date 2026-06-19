# `com.hart.Compositor` — Agent ↔ Compositor IPC Protocol

> **Scope:** the contract by which the HART OS brain (the Nunba/agent runtime, L3) and agents
> through it ask the compositor (HART-comp, L1) to place / tile / summon / arrange **real**
> native windows. Message shapes, the request/response/event model, and the **security
> boundary**.
>
> **Companions:** [`../docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md`](../docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md)
> (§5 AI-native WM IPC design) and [`ROADMAP.md`](./ROADMAP.md) (Phase 6, where this lands).
>
> **Status:** contract spec. No compositor source is authored here. This document is what the
> Rust server and the Python `HartWmClient` must both satisfy, and what the Phase-6 tests assert.

---

## 1. Design principles

1. **The brain drives; the compositor obeys within the constitution.** HARTOS is the heart and
   brain. `com.hart.Compositor` is a privileged surface the brain uses; it is **not** a new
   authority. No compositor verb can re-enable a cut AI sense, weaken a guardrail, or place a
   window the constitution forbids.
2. **`window.*` extends A2UI, never replaces it.** The verbs are a new family on the existing
   `agent_ui_update` contract — but only **after** Phase 2 makes that contract actually carry
   guardrail + circuit-breaker + immutable-audit + sanitization (today it carries none of these;
   see §6). The "inherits the pipeline" claim is true **only** once those controls exist.
3. **Fail-closed by default.** Every mutating method is refused if the constitutional layer is
   unavailable (`HiveCircuitBreaker.is_halted` is true, or `GuardrailEnforcer` cannot be
   consulted). A compositor that cannot reach its gate does nothing destructive.
4. **No phantom success.** A handle is returned **only** when a real toplevel mapped (§4.6). The
   installer/launcher exit code is never treated as success.
5. **Additive across tiers.** On sway Tier-2 the verbs map onto `swaymsg` (degraded moat); on
   cage Tier-3 there is no native-window management and the brain feature-detects its absence —
   shell-panel workspace switching still works client-side.

---

## 2. Transport

Two transports expose the **same** method/event surface:

| Transport | Bus name / path | Use |
|---|---|---|
| **D-Bus** | `com.hart.Compositor` on the session bus, object `/com/hart/Compositor`, interface `com.hart.Compositor1` | standard control plane; introspectable; dbus-policy-gated |
| **Unix socket (twin)** | `$XDG_RUNTIME_DIR/hart-comp.sock` (mode `0600`, owner = session user) | low-latency / in-process use by `HartWmClient`; same framed messages as D-Bus, JSON bodies |

The brain reaches the transport via a single `HartWmClient` (Python) registered in
`ServiceRegistry` under `"HartWmClient"`. The client is co-located with `LiquidUIService` per the
Phase-2 topology resolution (so `ServiceRegistry.get("LiquidUIService")` and
`ServiceRegistry.get("HartWmClient")` resolve in one heap). If the deployment keeps the brain and
LiquidUI split, `HartWmClient` uses the D-Bus transport across the process boundary — it never
relies on an in-process registry lookup it cannot guarantee.

**Framing (Unix-socket twin):** length-prefixed JSON. `4-byte big-endian uint32 length` followed
by a UTF-8 JSON object. One request → one response; events are unsolicited JSON objects on a
subscription.

---

## 3. Common message envelope

Every request and response shares an envelope so the gate, the audit log, and the caller can
correlate.

### 3.1 Request

```json
{
  "v": 1,
  "id": "req_<uuid>",
  "method": "window.place",
  "agent_id": "<prompt_id-or-uuid>",
  "request_id": "<thread-local request_id, for log correlation>",
  "origin": "agent | mcp | shell | daemon",
  "args": { "...": "method-specific, see §4" }
}
```

- `agent_id` — the placing/closing actor. Stamped into the audit record. For the human co-pilot
  over MCP, `origin="mcp"` and `agent_id` is the co-pilot's agent identity.
- `request_id` — the same thread-local request id used across autogen / langchain / dispatch so a
  window op correlates 1:1 with the originating `/chat` turn in `frozen_debug.log`.
- `origin` — provenance, used by the gate's policy (e.g. `daemon`-origin destructive geometry is
  held to the same PREVIEW gate as `agent`).

### 3.2 Response

```json
{
  "v": 1,
  "id": "req_<uuid>",
  "ok": true,
  "result": { "...": "method-specific" },
  "error": null
}
```

On refusal or failure:

```json
{
  "v": 1,
  "id": "req_<uuid>",
  "ok": false,
  "result": null,
  "error": {
    "code": "guardrail_refused | circuit_breaker_halted | preview_required | not_found | timeout | unsupported | rate_limited | invalid_args",
    "message": "human-readable, never leaks secrets",
    "detail": { "...": "optional, e.g. the approval_id for preview_required" }
  }
}
```

**Honest-failure rule:** `unsupported` is returned for inert subsystems (Android `exec sleep
infinity`, macOS/Darling stub) and for Wine launches that returned 0 but mapped no toplevel —
**never** a fabricated handle. `timeout` is returned when `SummonApp` does not see a map event in
the window.

---

## 4. Methods

All geometry is in logical pixels on the named output's current mode. A `handle` is an opaque
string minted by the compositor on toplevel map; it is stable for the toplevel's lifetime and
invalid after `window.closed`.

### 4.1 `window.list` — `ListWindows()`
Read-only. No gate beyond `is_halted` short-circuit (a halted constitution returns
`circuit_breaker_halted` even for reads, so the brain never acts on stale state during a halt).

**args:** `{}`
**result:**
```json
{
  "windows": [
    {
      "handle": "win_7f3a",
      "app_id": "org.blender.Blender",
      "title": "Blender — untitled.blend",
      "workspace": 2,
      "geometry": { "x": 0, "y": 0, "w": 1280, "h": 1024 },
      "manifest_id": "blender",
      "focused": false,
      "mapped": true,
      "unresponsive": false
    }
  ]
}
```
`manifest_id` is the `AppRegistry`/`AppManifest` id when the toplevel was launched via
`SummonApp`; `null` for windows opened outside the brain. This is the manifest ↔ toplevel map that
lets agents reason about which app a window is.

### 4.2 `window.focus` — `FocusWindow(handle)`
**args:** `{ "handle": "win_7f3a" }` · **result:** `{ "handle": "win_7f3a", "focused": true }`
Mutating (changes input focus). Gated (§6). Not "destructive geometry" — no PREVIEW required.

### 4.3 `window.close` — `CloseWindow(handle)`
**args:** `{ "handle": "win_7f3a" }`
**Destructive geometry** when the target is the **user-focused** window → routes through the
PREVIEW gate (§6.3) and returns `error.code = "preview_required"` with `detail.approval_id` until
approved. Closing a non-focused, agent-summoned window the agent itself opened is permitted
directly (still audited).
**result:** `{ "handle": "win_7f3a", "closed": true }`

### 4.4 `window.place` — `PlaceWindow(handle, target)`
**args:**
```json
{ "handle": "win_7f3a", "target": { "x": 0, "y": 0, "w": 1280, "h": 1024 } }
```
or a named zone:
```json
{ "handle": "win_7f3a", "target": { "zone": "left-half | right-half | top-left | center | maximize | ..." } }
```
`maximize`/fullscreen-takeover over a user-focused window is **destructive geometry** → PREVIEW.
**result:** `{ "handle": "win_7f3a", "geometry": { "x":0,"y":0,"w":1280,"h":1024 } }`

### 4.5 `window.tile` — `TileLayout(workspace, layout)`
**args:** `{ "workspace": 2, "layout": "cols | rows | grid | master-stack | fullscreen | bsp" }`
Arranges all mapped toplevels on the named workspace. `fullscreen` over a workspace that holds the
user-focused window is **destructive geometry** → PREVIEW.
**result:** `{ "workspace": 2, "layout": "master-stack", "arranged": ["win_7f3a","win_8b21"] }`

### 4.6 `window.summon` — `SummonApp(manifest_id)`
Launches via `AppRegistry`/`AppInstaller`, then **awaits a real toplevel-map event** before
returning success.
**args:** `{ "manifest_id": "blender", "place": { "zone": "right-half" } }` (optional `place`
applies after map)
**result (mapped):**
```json
{ "manifest_id": "blender", "handle": "win_9c04", "mapped": true }
```
**result (no map within timeout):** `ok=false`, `error.code = "timeout"` — **no handle**. For
inert subsystems: `error.code = "unsupported"`. This is the no-phantom-windows guarantee (§1.4):
`SummonApp` keys success on `wlr-foreign-toplevel`/xdg-shell **map**, never on
`app_installer.py:_install_*` return-True (Wine returns 0 even when nothing mapped; Android
`_install` only copies the apk).

### 4.7 `window.move_to_workspace` — `MoveToWorkspace(handle, n)`
**args:** `{ "handle": "win_7f3a", "workspace": 3 }` · **result:** `{ "handle": "win_7f3a",
"workspace": 3 }`

### 4.8 `workspace.switch` — `SwitchWorkspace(n)`
**args:** `{ "workspace": 3 }` · **result:** `{ "workspace": 3 }`
**Augment, not replace:** the shell's `hartWorkspaces.js` keeps its client-side panel show/hide on
every tier; this verb moves **real native windows** only and is feature-detected. Shell panels =
client state; native windows = compositor. One source of truth per object class.

### 4.9 `output.set_mode` — `SetOutputMode(output, mode)`
**args:** `{ "output": "DP-1", "mode": "2560x1440@144" }` · **result:** `{ "output": "DP-1",
"mode": "2560x1440@144", "applied": true }`
Backs the shell's display-resolution/scale system panels with a real backend.

### 4.10 `events.subscribe` — `Subscribe(events)`
**args:** `{ "events": ["window.opened","window.closed","window.focused","window.moved","window.unresponsive"] }`
**result:** `{ "subscription": "sub_4a", "events": [ ... ] }`
Thereafter the compositor sends unsolicited event frames (§5) on that subscription.

### 4.11 `screen.kill` — `ScreenKill(on)` *(M6 — the constitutional screen kill-switch)*
**args:** `{ "on": true }` (omit → defaults `true`)
**result:** `{ "blocked": true }`
The brain pushes this when the human cuts (or restores) the `screen` sense (§6.4). It sets
**one** compositor flag that simultaneously:
1. draws a full-output **opaque black** surface ABOVE every window/layer (privacy);
2. **stops forwarding input** to clients (control); and
3. **refuses every `zwlr_screencopy` `copy`** — the frame is `failed()`, so no native
   capture can read the screen while cut (no-native-capture invariant).

This keeps the gate **at the compositor with zero per-frame IPC**: the brain-side authority
(`core.ai_sensing`) pushes the *edge* over this socket rather than being polled every frame.
Until the Phase-7 portal + cross-process lock land, `zwlr_screencopy` (served against
HART-comp's own framebuffer so `grim`/`wf-recorder` capture it directly) is the governed
capture path and honours this same gate. The brain-side caller is
`integrations/agent_engine/hart_wm_client.py` (the same singleton that drives every other
verb); it maps a `screen` cut/restore from `core.ai_sensing` to `screen.kill {on}`.

---

## 5. Events

Emitted to subscribers. Same envelope minus `id`/`method`; `event` names the type.

```json
{ "v": 1, "event": "window.opened",
  "window": { "handle": "win_9c04", "app_id": "org.blender.Blender", "title": "Blender",
              "workspace": 2, "geometry": {"x":0,"y":0,"w":1280,"h":1024}, "manifest_id": "blender" } }
```

| Event | When | Brain consumer |
|---|---|---|
| `window.opened` | a toplevel mapped | resolves a pending `SummonApp`; updates `ListWindows` cache |
| `window.closed` | a toplevel destroyed | invalidates the handle |
| `window.focused` | input focus changed | feeds `core.foreground` as an **additive** signal (never replaces the HTTP-driven `enter_foreground` writer) |
| `window.moved` | geometry changed | lets agents re-pin A2UI overlays beside the window |
| `window.unresponsive` | no frame/ping within threshold | the self-healing daemon may offer to close+relaunch via the same IPC, audited |

**Foreground rule (load-bearing):** `window.focused` + a last-input timestamp **augment**
`should_yield_to_user`. They do **not** become the source of truth — the HTTP-request-driven
`is_genuine_user_request` writer remains canonical and must function with **zero compositor
present** (headless central Docker `:6777`, `server.nix`, `edge.nix`).

---

## 6. The security boundary

This is the heart of the contract. The IPC grants agents OS-level, desktop-mutating power.
Window ops are **far more dangerous** than the cosmetic cards the A2UI path was built for, so the
gate is explicit and fail-closed — it is **not** assumed inherited.

### 6.1 The pre-existing-gap correction (why the gate is built, not assumed)

`integrations/agent_engine/liquid_ui_service.py:394` `agent_ui_update` today performs **only**:
a `COMPONENT_TYPES` allowlist check, a 5-item ring-buffer truncation (a **display** cap, not a
security control), and an `EventBus` emit. There is **no** `GuardrailEnforcer` call, **no**
`ImmutableAuditLog` write, and **no** server-side escaping. Therefore `window.*` **must not** be
layered onto this path as-is. Phase 2 adds the missing controls to `agent_ui_update` (and the
`HartWmClient` boundary duplicates them as a defense-in-depth gate) **before** any `window.*`
verb ships.

### 6.2 The gate every mutating method passes (fail-closed)

In order, at the `HartWmClient` boundary **and** re-checked server-side:

1. **`HiveCircuitBreaker.is_halted`** — if the constitution is halted, refuse with
   `circuit_breaker_halted`. The brain does not touch windows during a constitutional halt.
2. **`GuardrailEnforcer.before_dispatch(agent_id, method, args)`** — the 33-rule guardrail
   network vets the op. Refusal → `guardrail_refused`. If the guardrail layer cannot be consulted,
   the op is refused (fail-closed), never allowed.
3. **Per-agent rate cap** — a real cap on window ops per agent per window (NOT the 5-item display
   ring buffer). Exceeded → `rate_limited`.
4. **Server-side argument sanitization** — `title`/`app_id`/string fields are sanitized
   server-side (the existing escaping is client-side in `renderAgentOverlay` and does not protect
   the IPC path).
5. **Immutable audit** — every mutating op is logged via `get_audit_log().log_event()` with
   `agent_id`, `method`, target `handle`/`manifest_id`, `origin`, and the decision
   (allowed/refused/preview-held). An agent placing or closing a window is audited **exactly like**
   an agent pushing a card — once §6.1's controls exist.

### 6.3 Destructive geometry → PREVIEW gate

Operations that can disrupt the user — **close a user-focused window**, **fullscreen takeover** of
a focused window, **tile-fullscreen** over the focused workspace — route through the **existing**
`PREVIEW_PENDING` / `PREVIEW_APPROVED` FSM gate (`lifecycle_hooks`) and surface an **A2UI approval
component** wired to the **real** audit sink. The method returns `ok=false`,
`error.code="preview_required"`, `detail.approval_id`. The op executes only after the human
approves; the approval and the eventual execution are both audited.

### 6.4 AI senses remain orthogonal and supreme

- No WM verb can re-enable a cut AI sense. `core.ai_sensing` is the single supreme gate with no
  AI write path.
- When a human cuts `screen`, the compositor (via the L4 portal screencast gate, Phase 7) really
  stops every app's capture — agents **observe** the cut (e.g. via a status read) but have **no
  verb to reverse it**. The orb closes its eyes.
- The screencast gate is a **cross-process** authority the portal must consult fail-closed
  (Phase 7) — until it exists, no third-party screencast surface ships, preserving the
  no-native-capture invariant the cage floor gives for free.

### 6.5 dbus-policy / socket boundary

- The D-Bus interface is reachable only by the session user via an explicit dbus policy shipped in
  `hart-comp.nix`; no other bus name may call mutating methods.
- The Unix-socket twin is `0600`, owner = the session user.
- The compositor binary itself is in the trust manifest (Phase 3): its content-addressed Nix store
  hash joins the signed release manifest checked by `verify_local_code_matches_manifest`, and it is
  a brand/origin-attested artifact so a forked/replaced compositor fails peer attestation.
  `full_boot_verification` gates HART-comp on its own signature — the constitution gates the
  display.

### 6.6 Tier degradation of the boundary

| Tier | WM IPC available | Gate behavior |
|---|---|---|
| **1** HART-comp | full `com.hart.Compositor` | full gate (§6.2–§6.4) |
| **2** sway | `tile`/`summon`/`move`/`switch` mapped onto `swaymsg` (degraded moat) | gate enforced brain-side at the `HartWmClient` boundary; PREVIEW + audit still apply |
| **3** cage | **none** — single fullscreen WebView, no native-window management | brain feature-detects absence; shell-panel workspace switching still works client-side; no `window.*` mutation possible |

---

## 7. MCP co-pilot surface

New optional MCP tools — `place_window`, `tile`, `summon`, `list_windows` — let the human's Claude
co-pilot steer the live desktop layout. They flow through the **same** `HartWmClient` and the
**same** gate (§6): guardrail + circuit-breaker + per-agent rate cap + immutable audit + PREVIEW
for destructive geometry. A co-pilot arranging windows is audited **identically** to an in-hive
agent (`origin="mcp"`, `agent_id` = the co-pilot identity). The moat extends to the co-pilot
without weakening any control.

---

## 8. Recipe integration (the moat, banked)

A CREATE-mode action can bank `window.*` steps (`summon A + B`, `tile grid`, `switch workspace`)
into a recipe so REUSE replays the **exact** desktop layout without LLM calls — the Recipe Pattern
extended to window management. Banking `window.*` steps:

- does **not** alter the recipe on-disk format, the `prompt_id`/`flow_id`/`action_id`/`session_id`
  identifier semantics, or dashboard grouping;
- replays through the same gate (§6) on REUSE — a banked `window.close` of a focused window still
  requires PREVIEW at replay time;
- treats a `SummonApp` that times out (no map) at replay as an **honest failure** the recipe
  surfaces, never a phantom-success no-op.

---

## 9. What this protocol must NOT do (invariants)

1. Never expose a verb that re-enables a cut AI sense.
2. Never treat an installer/launcher exit code as window-launch success (§1.4, §4.6).
3. Never mutate a window without guardrail + circuit-breaker + per-agent cap + immutable audit
   (§6.2); never run destructive geometry without PREVIEW approval (§6.3).
4. Never become the source of truth for the foreground yield gate — it augments, never replaces,
   the HTTP-driven writer (§5).
5. Never assume in-process co-location it cannot guarantee — use the D-Bus transport across a
   real process split (§2).
6. Never weaken or bypass the guardrail kernel, the master-key boundary, or the immutable audit
   chain. The compositor adds no new trust assumptions; it runs as a hardened systemd unit gated
   by `full_boot_verification` like every other `hart-*` service.

---

*This contract is faithful to the three invariants: HARTOS is the heart and brain (the brain
drives, the compositor obeys within the constitution); nothing built is lost (`window.*` extends
A2UI, never replaces it); OS-native, not kiosk (agents arrange real windows) — and never at the
cost of who controls the machine (every mutation is gated, audited, and human-overridable).*
