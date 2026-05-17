# Android Companion Wire Protocol

Single-source-of-truth spec for the Kotlin AccessibilityService companion that ships in `Nunba-HART-Companion/android/` and the HARTOS Python client in `integrations/vlm/mobile.py`.

Phase 8 of `memory/vlm_best_of_all_worlds_plan.md` §6/§7.

## Transport

Two transports both use the **same JSON-line wire format**.

1. **PeerLink compute channel** (typical) — desktop Nunba dispatches to a paired Android over the existing `compute` channel (id `0x01`, `PRIVATE`, reliable). Same channel as inference offload — VLM grounding fits "inference offload" semantically.
2. **Local UNIX socket on Android** (Termux / on-device) — `/data/data/com.termux/files/usr/var/run/nunba-companion.sock`. One JSON object per line, request-then-response, half-duplex.

## Envelope

Every request:
```json
{
  "type":       "android_list_windows | android_capture_window | android_get_node_tree | android_dispatch_action",
  "request_id": "uuid-...",
  "ts":         1746331200.0,
  "...":        "type-specific fields"
}
```

Every response:
```json
{
  "type":       "<request_type>_result",
  "request_id": "uuid-... (echo)",
  "status":     "ok | error | platform_unsupported",
  "error":      "human-readable reason (when status=error)",
  "data":       { "type-specific shape" }
}
```

## Request types

### `android_list_windows`

**Request:** envelope only — no extra fields.

**Response data:**
```json
{
  "windows": [
    {
      "window_id":     "int (AccessibilityWindowInfo.id)",
      "package":       "com.spotify.music",
      "activity":      "com.spotify.music.MainActivity",
      "title":         "Spotify",
      "rect":          [x, y, w, h],
      "monitor_idx":   0,
      "is_foreground": true,
      "is_accessible": true
    }
  ]
}
```

`is_accessible: false` means the window is rendered as a non-accessible surface (canvas game, custom Compose without semantics) — VLM grounding is required, the node tree won't help.

### `android_capture_window`

**Request:** `{ "window_id": "int" }`

**Response data:** `{ "jpeg_base64": "..." }`

Companion uses MediaProjection (one-time user permission via Intent the first time). Crops to the window's bounds via the AccessibilityNodeInfo rect. **Cannot** capture occluded background windows on Android without root — the companion returns `status: "platform_unsupported"` with `reason: "background_window_not_capturable"` in that case.

### `android_get_node_tree`

**Request:** `{ "window_id": "int (optional — defaults to foreground)" }`

**Response data:**
```json
{
  "tree": {
    "class":               "android.widget.LinearLayout",
    "text":                "",
    "content_description": "Main screen",
    "clickable":           false,
    "bounds":              [x, y, w, h],
    "children":            [ { "...recursive..." } ]
  }
}
```

**Often the better signal than VLM grounding** — text/contentDescription/clickable flags are exposed directly, no pixel reasoning needed. Most production agents operate primarily by tree matching and only fall back to VLM when `is_accessible: false` from `android_list_windows`.

### `android_dispatch_action`

**Request:** `{ "action": { /* VLM action dict */ } }`

Action mapping the companion handles:

| VLM action          | Android dispatch                                                                |
| ------------------- | ------------------------------------------------------------------------------- |
| `left_click [x,y]`  | `dispatchGesture(GestureDescription tap)` OR `node.performAction(ACTION_CLICK)` if a node matches |
| `type "text"`       | `node.performAction(ACTION_SET_TEXT)` with text bundle                          |
| `key "BACK"`        | `performGlobalAction(GLOBAL_ACTION_BACK)`                                       |
| `key "HOME"`        | `performGlobalAction(GLOBAL_ACTION_HOME)`                                       |
| `key "RECENTS"`     | `performGlobalAction(GLOBAL_ACTION_RECENTS)`                                    |
| `scroll_down`       | `dispatchGesture(swipe up)` OR `node.performAction(ACTION_SCROLL_FORWARD)`      |
| `scroll_up`         | `dispatchGesture(swipe down)` OR `node.performAction(ACTION_SCROLL_BACKWARD)`   |
| `open_file_gui "X"` | `Intent(ACTION_VIEW)` via launcher package — much cleaner than VLM-grounded taskbar click |

**Response data:** `{ "executed": true, "method": "gesture | node_action | intent" }`

## Coordinates

Android uses physical pixels. DPI varies wildly (160-720 dpi). When the request includes pixel coords from a MediaProjection capture, the companion translates from source-image space to actual display via the captured `rect` baseline. Companion is the source of truth for the active display's resolution.

## Failure modes

| Scenario                              | Response                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------------ |
| AccessibilityService disabled         | `status: "error", error: "accessibility_service_disabled"` — Nunba prompts user |
| MediaProjection permission denied     | `status: "error", error: "media_projection_denied"`                            |
| App not foreground when click fired   | Companion brings target app forward via `Intent.ACTION_VIEW` if user permits   |
| Node not clickable                    | Falls back to gesture tap at coords                                            |
| Background window pixel capture       | `status: "platform_unsupported", reason: "background_window_not_capturable"`   |

## Trust + privacy

Screenshots may contain banking, password manager, private chat. Companion respects the same `cloud_data_consent` gate as `WorldModelBridge`. Audit log on the **companion side** (not the desktop) records every capture/dispatch with timestamp + window meta + screenshot SHA-256 — same shape as `integrations/vlm/safety.py:AuditLogger`.

## Versioning

Add `"protocol_version": int` to the envelope when introducing a breaking change. Current version: implicit `1`. Companion must reject requests with `protocol_version > known_max` with `status: "error", error: "unsupported_protocol_version"`.
