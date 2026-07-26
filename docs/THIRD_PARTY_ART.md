# HART OS - Third-Party Art Credits & License Ledger

**Binding rule (steward 2026-07-01): every third-party image bundled into the
shipped OS is recorded here with its source + license, and where a license
requires attribution, the credit line ships in the OS "About / Credits" surface.**
Verify terms BEFORE bundling; if the terms forbid redistribution-in-a-product,
do not bundle it.

This ledger is the single source of truth for the OS credits screen. Our OWN
generated art (`integrations/agent_engine/static/app_art/` via
`generate_posters.py`) is first-party and needs no entry.

## Source license summary (checked 2026-07-01)

| Source | Commercial | Redistribute in OS | Attribution | How we use it |
|---|---|---|---|---|
| Central-instance agent images | yes (owned) | yes | none | PRIMARY agent art, matched by name |
| Flathub / official app logos | yes | yes (redistributable) | per-project (usually none) | app card icons |
| lummi.ai | yes | yes, as an **End Product** (UI art). Barred: reselling the images or building a competing stock site. | **not required** (shoutout appreciated) | AI-stock fill for cards/wallpapers |
| magnific.com (Freepik) FREE | yes | **rasterized only** - never ship the editable SVG/EPS/AI; do not sublicense | **REQUIRED: "Designed by Magnific" + link to magnific.com** | rasterized vectors/icons |
| magnific.com PREMIUM (paid) | yes | rasterized | none | rasterized vectors/icons |

**Operational rules that fall out of the above:**
- magnific FREE assets: **rasterize to PNG/WebP before bundling** (the editable
  SVG must never be in the shipped filesystem) AND add the "Designed by Magnific"
  credit below. If the steward holds a magnific/Freepik Premium+ subscription,
  the attribution is not required - note that here and drop the credit.
- lummi + central + Flathub: bundle directly (raster or vector), no credit owed.
- Never expose bundled third-party art as an extractable "stock library" surface
  (that is the one use lummi/magnific bar).

## Bundled art locations + drop-in seams (#143 offline-art)

All bundled art lives under `integrations/agent_engine/static/app_art/` and is
served offline at `/shell/static/app_art/...` (no network to look rich):

| Location | Contents | Drop-in seam (override by filename, zero code change) |
|---|---|---|
| `app_art/*.svg` | first-party generated brand POSTERS (agents + system apps) | regenerate via `generate_posters.py` |
| `app_art/apps/<flathub_id>.svg` | first-party generated app LOGO tiles (marketplace + catalog) | drop a redistributable **official/Flathub logo** as `<flathub_id>.svg\|png\|webp` here to override the tile; resolver = `shell_manifest.bundled_app_logo` |
| `app_art/agents/<slug>.*` (or `$HART_AGENT_ART_DIR`) | CENTRAL-owned agent images, matched by name-slug | central drops `<slug>.png\|webp\|jpg\|svg` (slug = agent name lowercased, non-alnum → `-`); resolver = `app_poster.central_agent_art`, served at `/shell/agent-art/<slug>` |

A dropped-in asset that requires attribution (e.g. a magnific-free logo) MUST get
a row in the attribution table below; first-party tiles, central-owned images,
and redistributable Flathub logos owe none.

## Attribution credits (shown in the OS About / Credits)

**This table is LIVE in the OS.** It is served by `GET /api/shell/credits`
(parsed from this file) and rendered in **Settings > About > Credits** (the
`credits` system panel → `hartCredits.js`). Each row: asset file, source, license,
the exact credit line if any.

| Bundled asset | Source | License | Credit line shown in OS |
|---|---|---|---|
| _(none bundled yet from attribution-required sources)_ | | | |

> When the first magnific-free asset is added, add its row here; it appears in the
> OS credits screen automatically (the route re-parses this file). Until then, the
> only bundled art is first-party generated posters + logo tiles + (pending) owned
> central images + (pending) redistributable Flathub logos, none of which owe
> attribution.
