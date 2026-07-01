# Bundled central agent art (offline, by name) — #143

Real, owned agent images the **central instance** ships or drops so its product
agents (Auto Research, Trading, Tutor, ...) show real art with the network OFF.

## How it is resolved

`integrations/agent_engine/app_poster.py` → `central_agent_art(name)` is consulted
**before** the generated `agent_art_url`, and the producer stamps the result on
`card.image` (which the client prefers over the network `card.image_url`). Files
are served same-origin by the `/shell/agent-art/<slug>` route.

## The naming contract (one seam, zero code change)

Drop an image named by the **slug of the agent name**:

- slug = the agent name lowercased, every run of non-alphanumeric characters
  collapsed to a single `-`, trimmed (see `app_poster._art_slug`).
- e.g. agent **"Auto Research"** → `auto-research.png`; **"Spoken English"** →
  `spoken-english.webp`.
- accepted extensions (first found wins): `.png .webp .jpg .jpeg .svg`.

## Where central drops them

Two locations are searched, in order:

1. **`HART_AGENT_ART_DIR`** — the central drop location, e.g.
   `export HART_AGENT_ART_DIR=/var/lib/hart/agent-art`. Central pushes owned
   images here by name; they show up offline on the next home compose.
2. **this bundled dir** (`static/app_art/agents/`) — images committed with the OS.

Licensing: central-owned images are redistributable in the OS with no credit
owed (see `docs/THIRD_PARTY_ART.md`). Any third-party-sourced agent art that
requires attribution must have its credit row added to that ledger, which is
surfaced in the OS About > Credits screen.
