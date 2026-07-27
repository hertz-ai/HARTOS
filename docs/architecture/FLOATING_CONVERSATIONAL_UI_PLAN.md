# Floating Conversational UI — Plan

**Goal:** a VUI‑style floating conversational presence (talk to HART by voice,
hear it back, interrupt it) that **floats on top of the desktop** on every HART
surface — the HART OS glass shell *and* the Nunba desktop — by **enhancing the
existing floating agentic UI on each, not nesting a chat in a page**.

**Borrowed from** [fluxions‑ai/vui](https://github.com/fluxions-ai/vui)
(Apache‑2.0 **code**; model weights gated/Alibaba‑owned — *not* pulled in): the
*patterns* only — barge‑in, an always‑on floating conversational presence,
continuous VAD, the OpenAI‑Realtime protocol. **No ML enters HARTOS** (models
stay in HevolveAI / the Nunba TTS engine).

## Standing constraints (every phase)
1. **Reuse, don't reinvent.** Extend the existing floating component + existing
   hooks/endpoints/colors. Prove the equivalent doesn't already exist first.
2. **No nesting.** The conversational UI floats on top; it is not embedded in a
   page (e.g. NOT a chat mounted inside `Demopage`).
3. **No parallel paths.** One send path, one TTS‑stop, one color source.
4. **Reuse the palette.** Glass shell → `--hart-*` / `--ds-*`. Nunba → `GLASS` +
   `ACCENT`. Never introduce a color.
5. **Zero regression.** Additive + fenced; existing push‑to‑talk / chat
   untouched; feature‑detect browser APIs (WebKitGTK floor — see the
   `AbortSignal` incident).
6. **Backend only when a phase needs it** (streaming TTS, Realtime WS). Phases
   1–2 need none.
7. **Verify per surface:** glass shell = inline‑render (`render_desktop_shell()`,
   this box OOMs pytest); Nunba = React lint/build + component tests. JS runtime
   for the shell is only provable on the ISO.

## Reuse inventory (what already exists)

### Backend (HARTOS, shared by both surfaces)
| Endpoint | Purpose |
|---|---|
| `/api/voice/speak` | server TTS (returns `audio_url`) |
| `/api/voice/transcribe`, `/api/voice` | STT (audio → text) |
| `/api/voice/clone`, `/api/voice/engines`, `/api/voice/voices` | voice cloning + engine/voice lists |
| `/chat`, `/api/agent/ask` | the agent turn |

### Glass shell — `integrations/agent_engine/liquid_ui_service.py`
- Floating assistant chat: `#assistant-chat` + `#agent-pill` (≈3257), 10 cap
  tabs incl. Voice, drag, minimize.
- `speakText()` (≈3612) hybrid TTS; `startRecording()`/`stopRecording()`
  (≈3569) mic→`/api/voice`; `acSend()` (≈3356) → `/api/agent/ask`.
- Colors: `--hart-accent:#00D4AA`, `--hart-glass-*`, `--hart-active`, `--ds-*`
  (≈502/600); `.ac-*` CSS (≈1053).

### Nunba desktop — `landing-page/src/`
- **Existing float:** `components/AgentOverlay/AgentOverlay.jsx` — `position:fixed`,
  `zIndex:9998`, bottom‑right, `GLASS` (L28) + `ACCENT='#6C63FF'` (L38). Renders
  agent action cards; **ephemeral** (`return null` when no cards, L834). Mounted
  on top at `pages/Demopage.js:5147`.
- **Voice machinery (in the page):** `Demopage.js` — `useTTS` (L629),
  `handleSend` (L3318), barge‑in wake‑listener `tts.stop()` (L3031/3335).
- **Reusable hooks:** `hooks/useTTS.js` (`stop` L451/646), `hooks/useSpeechRecognition.js`
  (`startListening`/`transcript`/`onResult`), `hooks/useMicAmplitude.js`
  (RMS amplitude/decibels = client‑side VAD, no ML).
- **Canonical chat:** `Social/shared/NunbaChat/{NunbaChatPanel,NunbaChatProvider}.jsx`
  (`useNunbaChat` → `sendMessage` L732, `tts` L321). NOTE: `Demopage` is **not**
  inside `NunbaChatProvider`, so AgentOverlay reuses **Demopage's** machinery via
  props, not the context.

## Phases

### Phase 1 — Glass shell — ✅ DONE (`966f32c`)
Enhanced the *existing* `#assistant-chat`:
- **Voice → conversation:** `startRecording.onstop` transcript now routes to
  `#ac-input → acSend → #ac-messages` (was the hidden collapsed pill, so spoken
  turns were invisible). Collapsed the parallel path: removed `askAgent()` (a
  duplicate of `acSend`) — `acSend` is the single send path.
- **Barge‑in:** tracked the TTS `<audio>` in `_acAudio` + one canonical
  `acStopSpeaking()`; called at the top of `startRecording` (talk → it stops)
  and `speakText` (no overlap).
- Reuse‑only (`acSend`/`speakText`/`startRecording`/`showToast`, `--hart-*`).
  Verified by inline‑render. Lands on next ISO build.

### Phase 2 — Nunba floating orb — ⏳ NEXT (awaiting shape decision)
Make the existing `AgentOverlay` float a **persistent VUI‑style mic orb** (don't
nest in Demopage):
- `AgentOverlay.jsx`: render one orb in the fixed `GLASS` container (reuse
  `GLASS` + `ACCENT`). It must NOT call `useSpeechRecognition`/TTS itself — that
  is a second STT/TTS path; the orb is STATE-ONLY and reflects/toggles Demopage's
  EXISTING loop. Pulse with `ACCENT` on `voiceState`; render even when `overlays`
  is empty (cards stack
  above). On mic‑start → `onBargeIn()`; on final transcript → `onVoiceText()`.
- `Demopage.js` @ `<AgentOverlay>` (5147): pass `onBargeIn={tts.stop}` (reuse
  L3335) + `onVoiceText={handleSend}` (reuse L3318). ~2 lines.
- Backend: none. Verify: React lint/build + existing AgentOverlay test.
- **OPEN DECISION:** persistent orb (always on top, VUI‑style) vs only while an
  agent/chat context is active. *Recommend persistent* (matches "float like
  VUI").

### Phase 3 — Continuous + streaming + realtime — ✅ ALREADY EXISTS (surface, don't build)
CORRECTION: these are **not** new backend work — the canonical interfaces are
already shipped. The float **reflects/toggles** them; it must not re‑implement
any (that would be a parallel path):
- **Streaming ASR:** `hooks/useSpeechRecognition.js` streams to
  `ws://127.0.0.1:8005` ("HARTOS streaming Whisper", `onPartialResult`). The
  **same `:8005` sink** the LiveKit `integrations/social/livekit_transcript_subscriber.py`
  feeds — ONE STT interface, browser mic + LiveKit both into it.
- **Realtime voice bridge:** `integrations/social/agent_voice_bridge.py` +
  `call_service.py` + `livekit_audio_publisher.py` (LiveKit). Already the
  realtime transport.
- **Streaming TTS push:** `realtimeService.on('tts')` + the persistent
  `#nunba-tts-audio` element (`NunbaChatProvider` / `Demopage`).
- **Continuous listen + barge‑in:** `Demopage` wake‑listener loop
  (`wakeListenerRef`, L2991‑3054; `tts.stop()` barge‑in L3031).
- **Client VAD level (optional visual):** `hooks/useMicAmplitude.js` (RMS, no ML).

## Canonical interfaces — ONE per function (zero redundancy)
The floating UI binds ONLY to these single sources. A second mic/STT/TTS/VAD/send
in the orb is a parallel path — forbidden.

| Function | Single canonical interface |
|---|---|
| Streaming STT | `useSpeechRecognition` -> `ws://127.0.0.1:8005` (also fed by LiveKit `livekit_transcript_subscriber`) |
| TTS + stop/barge-in | `useTTS` (`stop`) + realtime `'tts'` push -> `#nunba-tts-audio` |
| Continuous listen + barge-in | Demopage wake-listener loop (`wakeListenerRef`) |
| Send a turn | Demopage `handleSend` / NunbaChat `sendMessage` |
| Realtime voice transport | `agent_voice_bridge` / LiveKit |
| Client VAD level | `useMicAmplitude` (RMS, no ML) |

Glass-shell mirrors of the same functions: `/api/voice/transcribe` (STT),
`/api/voice/speak` (TTS), `speakText`/`acStopSpeaking`, `startRecording`.

**DRY item (resolve before the orb):** Nunba has TWO voice-input entries —
`useSpeechRecognition` (NunbaChatPanel mic, :8005 streaming) and Demopage's
`wakeListenerRef` (continuous). Confirm the canonical one; the orb reflects THAT,
and the two get consolidated if redundant. No third path is added.

## Status / next action
- Phase 1 shipped (`966f32c`).
- Phase 2 ready to build the moment the orb‑shape decision is made.
- `navigator.clipboard` (shell, no `.catch`) + the debug‑extras gating from the
  `AbortSignal` review remain separate follow‑ups.
