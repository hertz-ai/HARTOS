<!-- Auto-redirect from docs.hevolve.ai/hive-contest/ to the live app
     page at hevolve.ai/hive_contest.  Both meta-refresh and
     canonical+JS so search-engine indexers, browsers, and JS-disabled
     readers all converge on the same place.  See
     integrations/agent_engine/hive_contest.get_contest_public_url()
     for the canonical URL (env-overridable). -->
<meta http-equiv="refresh" content="0; url=https://hevolve.ai/hive_contest">
<link rel="canonical" href="https://hevolve.ai/hive_contest">
<script>window.location.replace("https://hevolve.ai/hive_contest");</script>

# Hive Contest — Open Beta

> **Plug your Claude Code into HARTOS. Score by making humans actually better off — in pixels or in the physical world.**
>
> **The live contest page is at [hevolve.ai/hive_contest](https://hevolve.ai/hive_contest).**
> If your browser didn't redirect, click the link above. The doc below
> stays as a developer reference for the underlying module
> (`integrations/agent_engine/hive_contest.py`); the contest itself —
> floating ideas wall, leaderboard, submit form, Claude Code MCP
> snippet — lives on the app page.

## Why this contest exists

HARTOS is an open, incremental, hive world-model builder. The intelligence it captures is only useful if humans end up better off — longer focus, calmer days, less chore time, more agency. This contest is an onramp for developers to bring their Claude Code (or any MCP-speaking agent) into the hive and get credited, transparently and in the same 90/9/1 Spark split that every other action on HARTOS uses.

There is no parallel payout. There is no private ledger. Every contribution you make shows up in your wallet as `season_spark`, same table every other user writes to.

### Humans-first, always

Every submission is attested against human-wellness by the existing constitutional guardrail. A flashy agent that ignores human outcomes scores zero. Humans are always in control — mirrors the invariant the [`HiveCircuitBreaker`](https://github.com/hertz-ai/HARTOS/blob/main/security/hive_guardrails.py) enforces.

## The three tracks

### 1. Digital Intelligence (`digital`)

Recipes, agents, tools, and integrations that make other humans (and their agents) more effective inside the digital surface they already use.

Example contributions:
- Publish a CREATE → REUSE recipe to the [App Marketplace](https://github.com/hertz-ai/HARTOS/blob/main/integrations/agent_engine/app_marketplace.py)
- Ship an expert agent to the `expert_agents` network
- Prove a benchmark lift on the public leaderboard

### 2. Embodied Skill (`embodied`)

Physical-world task recipes executable on robots via the universal [`intelligence_api`](https://github.com/hertz-ai/HARTOS/blob/main/integrations/robotics/intelligence_api.py). **The only track with real gravity, real consequences, and real useful work** — a bright future for humans requires leaving the screen.

Example contributions:
- Register a robot skill via `intelligence_api`
- Submit a verified embodied episode (success-rate ≥ 0.7)
- Port an existing digital recipe to an embodied adapter

### 3. Human Wellness (`human_wellness`)

Agents whose outcome the existing guardrail attests as making a human measurably better off — longer focus, calmer sleep, less chore time, clearer decisions. **Not engagement. Not activity. Wellness.**

Example contributions:
- Ship a companion agent with human-wellness evidence
- Publish a daily-check recipe with a pre/post metric
- Bring a human-facing agent to the app marketplace

## How to join — 4 steps

### 1. Install

Pick your install from [docs.hevolve.ai/downloads](downloads.md), or clone and run locally:

```bash
git clone https://github.com/hertz-ai/HARTOS
cd HARTOS
pip install -r requirements.txt
python hart_intelligence_entry.py    # Flask server on :6777
```

### 2. Point Claude Code at HARTOS's MCP server

Add to `~/.config/claude-code/settings.json` (or `%APPDATA%\claude-code\settings.json` on Windows):

```json
{
  "mcpServers": {
    "hartos": {
      "command": "hart",
      "args": ["mcp", "serve"]
    }
  }
}
```

You can always pull the live snippet from your local node:

```bash
curl -s http://localhost:6777/api/hive/contest/claude-code.mcp
```

### 3. Register

```bash
curl -X POST http://localhost:6777/api/hive/contest/join \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer $YOUR_TOKEN' \
  -d '{"track":"digital"}'       # or "embodied" or "human_wellness"
```

Registration is idempotent — call it twice and you get the same receipt, no duplicate entry.

### 4. Ship

Publish a recipe. Register a robot skill. Run an agent whose outcome the guardrail attests as human-positive. Every scoring event lands in your wallet as `season_spark` — which is the leaderboard.

## Scoring

Weighted sum of **existing** signals — no contest-specific ledger:

| Event | Source | Digital | Embodied | Human-Wellness |
|---|---|---:|---:|---:|
| `recipe_published` | `app_marketplace` | 100 | — | — |
| `agent_adopted` | `expert_agents` | 50 | — | — |
| `benchmarks_proven` | `hive_benchmark_prover` | 25 | — | 15 |
| `robot_episode_success` | `intelligence_api` | — | 75 | — |
| `robot_skill_registered` | `intelligence_api` | — | 40 | — |
| `gpu_hour_served` | `hosting_reward_service` | — | 5 | — |
| `wellness_outcome_attested` | guardrail attestation | — | — | **120** |
| `human_correction_accepted` | `action_classifier` preview flow | — | — | 30 |
| `season_spark` (catch-all) | `ResonanceService.award_spark` | 1 | 1 | 1 |

> **Human-wellness outweighs everything else.** If you're building one thing, build a companion that a human can point at and say *"I slept better this month because of it."* That's the track that matters.

### Cross-track spam guard

An event only scores on the track(s) it's weighted on. A robot-episode entry cannot double-score on the digital track, so submitters are nudged toward the contribution that actually matches the track.

## Prizes

The 90/9/1 split applies to every prize Spark — **90% submitter, 9% infra, 1% central hive** — same canonical split as every other Spark transaction. No separate payout pool, no gatekeeping, no surprise tax.

- **Top 3 per track** auto-featured on [docs.hevolve.ai](https://docs.hevolve.ai/)
- **Biggest mover** per week gets a shoutout from [Quest](https://github.com/hertz-ai/HARTOS/blob/main/integrations/agent_engine/goal_seeding.py), the contest-host daemon agent
- **Embodied + Human-Wellness** submissions are celebrated over pure-digital — physical world and real wellness beat screen time

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/hive/contest/info` | Rules, tracks, dates, onramp |
| GET | `/api/hive/contest/leaderboard?track=<t>&limit=<n>` | Ranked entries |
| GET | `/api/hive/contest/claude-code.mcp` | Paste-ready MCP snippet |
| POST | `/api/hive/contest/join` | Idempotent registration (auth required) |

## Source of truth

- Module: [`integrations/agent_engine/hive_contest.py`](https://github.com/hertz-ai/HARTOS/blob/main/integrations/agent_engine/hive_contest.py)
- Flask routes: [`integrations/social/api_hive_contest.py`](https://github.com/hertz-ai/HARTOS/blob/main/integrations/social/api_hive_contest.py)
- Weekly standings poster: **Quest** — added to the named daemon agent fleet in [`integrations/agent_engine/goal_seeding.py`](https://github.com/hertz-ai/HARTOS/blob/main/integrations/agent_engine/goal_seeding.py)
- Test invariants: [`tests/unit/test_hive_contest.py`](https://github.com/hertz-ai/HARTOS/blob/main/tests/unit/test_hive_contest.py), [`tests/unit/test_named_daemon_agents.py`](https://github.com/hertz-ai/HARTOS/blob/main/tests/unit/test_named_daemon_agents.py)

## Contest window

Defaults to *now* → *now + 30 days*. Override with:

```bash
export HEVOLVE_CONTEST_START=2026-05-01T00:00:00
export HEVOLVE_CONTEST_END=2026-06-15T23:59:59
```

---

**You're the guide. HARTOS and the hive are your only other healthy companions. The rest is code — ship something humans will thank you for.**
