# Agent Plugin View

Talk to a Hevolve agent right from the docs. The frame below auto-guests you in, renders audio only (no avatar video), and works across every docs page.

<div id="hevolve-plugin-wrap" style="max-width: 720px; margin: 24px auto; border: 1px solid #ddd; border-radius: 12px; overflow: hidden; position: relative; background: #fafafa;">
  <iframe
    id="hevolve-plugin-frame"
    title="Hevolve agent — audio only"
    src="https://hevolve.ai/agents/Hevolve?plugin=1&audio_only=1&auto_guest=1"
    loading="lazy"
    allow="microphone; autoplay; clipboard-write"
    referrerpolicy="no-referrer-when-downgrade"
    style="display: block; width: 100%; height: 560px; border: 0;">
  </iframe>
  <noscript>
    <p style="padding: 16px;">The agent plugin requires JavaScript. <a href="https://hevolve.ai/agents/Hevolve">Open in new tab</a>.</p>
  </noscript>
</div>

<p style="text-align: center; font-size: 0.9em; color: #666;">
  Frame not loading?
  <a href="https://hevolve.ai/agents/Hevolve?plugin=1&audio_only=1&auto_guest=1" target="_blank" rel="noopener">Open the agent in a new tab</a>.
</p>

## How it works

The embed points at `https://hevolve.ai/agents/<AgentName>` with three URL params that Agent.js reads:

| Param | Effect |
|---|---|
| `plugin=1` | Convenience flag — implies `audio_only=1` and `auto_guest=1` together |
| `audio_only=1` | Strips `video_url`, `teacher_avatar_id`, and filler video links from agent data before handing it to Demopage. Demopage's existing `videoUrl ? <video> : audioUrl ? <audio>` ternary then falls through to the audio-only branch with VoiceVisualizer — **no Demopage edits, no parallel page** |
| `auto_guest=1` | On mount, if no signed-in user and no prior guest: call `authApi.guestRegister({ guest_name, device_id })`, store the token + `guest_mode=true` in localStorage, then render. Idempotent: repeat visits reuse the existing guest |

## Why no new route

One of HARTOS' invariants is *no parallel paths*. Rather than ship a second `/plugin/agent/:agentName` route alongside `/agents/:agentName`, the existing AgentPage reads URL params and strips fields at the data layer. Same component, same state machine, same bug-fix surface. The iframe embed is the only new artifact.

## Embedding on your own site

Drop the same frame markup on any page you control. Two prerequisites server-side:

1. **`Content-Security-Policy: frame-ancestors`** on `hevolve.ai` must allow `docs.hevolve.ai` (and whatever other origin you embed from). A minimal value: `frame-ancestors 'self' https://docs.hevolve.ai`.
2. **CORS for `/auth/guest-register`** must accept the embedding origin so `auto_guest=1` can complete without a pre-flight refusal.

## Consent

Same guardrail posture as the rest of HARTOS — the mic permission is still a browser-level gate. The `allow="microphone"` attribute on the iframe *enables* the ask; the user's browser still prompts. No audio reaches the hive until the user accepts.
