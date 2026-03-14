# Reddit Product Launch Post

**Subreddits:** r/LocalLLaMA, r/artificial, r/MachineLearning, r/SideProject, r/selfhosted, r/opensource

---

**Title:** Your AI agent learns a task once, then replays it 90% cheaper. Now imagine 10,000 agents doing that and sharing what they learn with the world.

---

**Body:**

I spent a year solving a problem that kept bugging me: every time an AI agent does something useful, that knowledge evaporates. Next session, it starts from scratch. Meanwhile your GPU sits idle 22 hours a day.

So I built **HART OS** — an open-source agentic intelligence operating system on AutoGen. It turned into an entire ecosystem.

## The Ecosystem

This isn't one tool. It's seven products that work together:

| Product | What it is | Stack |
|---------|-----------|-------|
| **HART OS Framework** | Agentic framework — the brain. 96 agents, recipes, speculative dispatch, 33 constitutional rules, 10-class guardrail network | Python/AutoGen/Flask |
| **Hevolve Core** | Self-evolving intelligence engine — continual learning, RL from expert feedback, manifold credit assignment, federated hivemind | HevolveAI + embodied AI |
| **Nunba** | Desktop companion — sidebar AI that snaps to screen edge, pulsing ribbon when AI is driving, local Qwen3-VL vision model (1.5 GB, works offline), system tray, screenshot + computer control | Python + pywebview + cx_Freeze (Win/Mac/Linux) |
| **Hevolve Android + WearOS** | Mobile + watch — 105 built-in kids games, agent consent from your wrist, watch face complication with live stats, TTS on watch speaker, quiz games without phone | React Native + Kotlin Compose Wear |
| **hevolve.ai** | Web platform — agent creation, 40 languages, speech therapy, income platform for experts ($3-5K/mo passive), institutional access | React 18 + MUI |
| **Regional Hosting SDK** | For compute providers — Ed25519 certificate chain, hybrid approval, GitHub access to Hevolve Core, cluster management | Python + security layer |
| **Social Platform** | 82 API endpoints — communities, karma, encounters, referrals, kids learning, thought experiments | Flask blueprints + SQLAlchemy |

One master key governs all of it. Ed25519. Its only function: shut everything down. Not an admin tool. Not accessible to AI.

## The Core Idea

Your agent handles a task for the first time (**CREATE mode**) — decomposing, executing, figuring it out. Then it saves the entire execution as a **recipe**. Next time the same kind of task comes in, it replays the recipe (**REUSE mode**) — skipping the LLM calls, the decomposition, all of it. ~90% cheaper.

But here's where it gets interesting: those recipes don't stay on your machine. They propagate across a network. When someone else's agent learns to write a Terraform module, your agent gets smarter too. When your agent figures out a better marketing sequence, that feeds back.

Every node that joins makes every other node more useful.

## What You Actually Get

**96 Expert Agents** — not one generalist, but a specialist network across 10 domains: software dev (15), data analytics (10), creative/design (12), business ops (8), education (7), health (6), communication (8), infra/devops (10), research (8), specialized (12). They collaborate via internal agent communication with 4 selection strategies (accuracy, speed, efficiency, balanced).

**Speculative Dispatch** — your fast local model (Qwen3-VL 4B, Groq LLaMA 3.1 8B) answers instantly. A ThreadPoolExecutor runs GPT-4.1 or Claude Sonnet in the background. If the expert meaningfully improves the answer (similarity < 0.80), it gets delivered async. You get speed AND quality.

**35+ Channel Adapters** — Discord, Telegram, Slack, WhatsApp, Signal, iMessage, Matrix, Teams, Twitch, Twitter/X, LINE, Viber, WeChat, Messenger, Nostr, Mattermost, RocketChat, Nextcloud, email, voice, GPIO, ROS Bridge, Serial, WAMP IoT. Your agents exist wherever your users are.

**Vision** — MiniCPM-V sidecar. Your agent can see through camera or screen. Frame-by-frame scene descriptions flow into an embodied AI continual learner (HevolveAI). The thing literally learns from watching.

**Voice (TTS) + WearOS** — fleet command system streams TTS to connected devices. Cross-device routing picks the best output (phone > desktop > tablet > watch). The WearOS companion (Kotlin Compose Wear) has 5 screens: Home (Resonance score + streak), Quick Play (kids trivia on your wrist — no phone needed), Challenges (progress tracking), Notifications, and **Consent** — agent wants to do something? Your watch buzzes, you tap Allow or Deny. Watch face complication shows your RP + level, updates every 10 minutes. When the phone is disconnected, the watch polls fleet commands directly over LTE/WiFi. TTS plays on the watch speaker.

**Coding Agent** — detects idle compute on your machine, picks up coding goals from the network, decomposes repos, writes code, runs tests, submits PRs. Automated review with baseline validation. Constitutional review gate means no agent can merge unreviewed code.

**Kids Learning** — not a toy feature. **105 built-in games** across 5 subjects (English, Math, Life Skills, Science, Creativity), 3 age groups (4-6, 7-9, 10-12), 15 game templates. Spaced repetition intelligence (3R: Registration, Retention, Recall) tracks per-concept mastery at 1/3/7/14/30 day intervals. **AI game generation**: parents type "make a science quiz about weather for age 7" and the system generates a complete game. Games **evolve with your child** — harder, easier, or personalized based on performance. AI narrates questions, generates background music (ACE Step 1.5 model), and creates video animations (LTX2). TV adapter for large screens. Custom games by parents/teachers. Quick Play on WearOS — kids trivia without a phone. The anti-addiction rules aren't configurable — they're frozen in the bytecode.

**Thought Experiments** — users post structured hypotheses. Community votes. Agents autonomously research them. Results feed into the collective intelligence. One of those thought experiments is the system itself — HevolveAI's embodied learner is a living experiment in whether crowdsourced intelligence can self-improve.

**hevolve.ai** — the web platform. Create agents, deploy them, earn from them. **40 languages supported** — English, Hindi, Arabic, Mandarin, Spanish, Russian, and 34 more. Experts create agents once and earn passive income: speech therapists, tutors, career advisors, tax filers, customer care — any skill you can do online, you can automate. Consumers get 24/7 access to expert-quality AI at a fraction of the cost. Institutional access for schools and companies.

**Social Platform** — 82 API endpoints. Communities, posts, feeds, karma (4-currency system: Pulse/Spark/Signal/XP), encounters, referrals, achievement badges, levels from Newcomer to Founding Pillar. All open source.

**Nunba Desktop** — not just a backend. A pywebview sidebar companion that snaps to the edge of your screen. System tray when minimized. Runs a local Qwen3-VL 4B vision model (1.5 GB, auto-downloaded) — works completely offline. Takes screenshots, controls your computer on your behalf. A semi-transparent **pulsing ribbon** at the top of your screen is the only indicator the AI is driving — click it to stop. Full Hevolve social, communities, and agent marketplace running locally on your PC.

**Distributed Ledgers** — every agent action tracked in persistent JSON ledgers. State machine (ASSIGNED → IN_PROGRESS → VERIFICATION → COMPLETED/ERROR → TERMINATED). Cross-session recovery. Full audit trail.

## The Economics

Infrastructure costs money. Hosting agents costs money. The question is: who captures that value?

The network runs on compute — yours and everyone else's. The model is simple: people who contribute compute, content, and intelligence get 90% of what the network earns. The other 10% keeps the lights on.

- **Ad impressions**: Peer-witnessed. Independent nodes verify each impression actually happened. Witnessed impressions pay 70%, unwitnessed pay 50%. Gaming it triggers fraud detection.
- **Commercial Intelligence API**: 4 tiers (free/starter/pro/enterprise), metered at $0.20-$0.50/1K tokens. Revenue split back to compute providers.
- **Build licenses**: Community (free), Pro, Enterprise.
- **Spark economy**: 12 action types earn multi-currency rewards — posts, comments, tasks, recipe sharing, referrals, hosting uptime. Logarithmic scaling so whales can't dominate.

A 100-GPU node earns ~3x what a 1-GPU node earns, not 100x. That's not a policy — it's math (`log2(raw) + 1.0`, capped at 5.0).

## Why You Should Trust This With Your Compute

Agent networks have an alignment problem: the incentive is to extract as much value as possible from contributors. Here's how HART OS makes that structurally impossible:

**10-class guardrail network** — frozen with `__slots__`, module-level `__setattr__` interception, and a SHA-256 hash verified every 300 seconds. There is no API endpoint to modify these. There is no admin panel. `unittest.mock.patch()` itself can't override them.

**33 constitutional rules** baked into the hash. Anti-addiction (no infinite scrolls, no dark patterns). Compute democracy (max 5% influence per entity). No agent self-replication. No private knowledge hoarding. Ecological awareness. You can read them — they're in `security/hive_guardrails.py`.

**Continuous all-node audit** — every integrity round challenges ALL active peers, not just random samples. Guardrail hash re-verified. Fraud detection runs on every node: velocity anomalies, self-dealing, witness ring collusion (3+ nodes exclusively attesting for each other), temporal clustering. The combined compute of auditors must exceed the target node's compute. Always.

**Reward hacking detection** — 2+ fraud signals → auto-isolation. Rewards frozen. Progressive bans (1h → 24h → 7d → 30d). But fraud scores decay 2 points per audit round — good behavior earns trust back.

**Secret redaction** — before anything touches the shared intelligence, a regex-based redactor strips API keys, tokens, passwords, PEM keys, JWTs, connection strings, credit cards, and PII. Sub-millisecond. Your secrets never leave your node.

**Federation** — the network only connects to other hiveminds that share the same guardrail hash. No exceptions. If a connected peer's values drift, the connection drops immediately.

**One master key** — Ed25519. Its only function is triggering a network-wide circuit breaker. Not an admin tool. Not accessible to AI. Held by humans.

## The Network

```
Central (hevolve.ai)
  └── Regional Hosts (community-run clusters)
       └── Local Nodes (Nunba on your desktop / Hevolve on your phone)
```

**Regional hosts** — pass a compute + trust threshold, get an Ed25519 certificate chain, GitHub access to the private Hevolve Core repo (the self-evolving intelligence engine), and run your own cluster. Hybrid approval: auto-qualify on hardware, human steward confirms.

**Local nodes** — install Nunba (Windows/Mac/Linux) or the Android app. Zero-config LAN discovery via UDP broadcast on port 6780. Signed beacons. Certificate verification. Offline-first with sync queue that drains when connected.

## Get It

- **GitHub**: [github.com/hertz-ai/HARTOS](https://github.com/hertz-ai/HARTOS) — open source (framework, agents, social, desktop, mobile)
- **Hevolve Core** (self-evolving intelligence engine): Private repo. Regional hosts who contribute compute get access.
- **hevolve.ai**: [hevolve.ai](https://hevolve.ai) — web platform, agent creation, pricing, demos
- **Nunba Desktop**: Windows/Mac/Linux — companion app that turns your computer into a hive node (Python + cx_Freeze)
- **Android + WearOS**: [Hevolve on Google Play](https://play.google.com/store/apps/details?id=com.hevolve) — full agent interaction, communities, kids learning (15+ game types), TTS, vision, and a WearOS companion with agent consent from your wrist
- **Product Hunt**: [producthunt.com/posts/hart-os](https://producthunt.com/posts/hart-os)

```bash
# Quick start — Python 3.10
python3.10 -m venv venv310
source venv310/Scripts/activate
pip install -r requirements.txt
python langchain_gpt_api.py    # Starts on port 6777
```

Or just install Nunba / the Android app and start talking to agents.

---

Happy to answer anything about the architecture, the safety model, or the economics. The full guardrail source is in the repo — read it yourself.

---

## Cross-post variations

### r/LocalLLaMA (emphasize local inference)
**Title:** Agent framework with local llama.cpp inference, MiniCPM vision, and a recipe system that makes your agents 90% cheaper after first run

**Hook:** "Built an agentic framework on AutoGen. Your local LLM handles a task once (CREATE), saves the full execution as a recipe, replays it forever (REUSE) without repeated inference. Speculative dispatch: Qwen3-VL 4B answers instantly, GPT-4.1 refines in background. 35+ channel adapters. Vision via MiniCPM-V sidecar. Your idle GPU joins a network and earns proportional revenue — logarithmically scaled so no one dominates. 33 frozen constitutional rules, SHA-256 verified every 5 minutes. Open source."

### r/selfhosted (emphasize self-hosting + privacy)
**Title:** Self-hosted agent platform — 82 API endpoints, offline-first sync, zero-config LAN discovery, and your secrets never leave your node

**Hook:** "HART OS runs on your hardware. Offline-first with sync queue that drains when connected. UDP broadcast discovery on port 6780, signed beacons, Ed25519 certificate chains. A regex-based secret redactor strips API keys, tokens, PII, JWTs, and credit card numbers before anything touches the shared intelligence — sub-ms latency. 35+ channel adapters (Discord, Matrix, Signal, Mattermost, Nostr, etc). The guardrails are structurally immutable — frozen at the Python module level, `__setattr__` intercepted, SHA-256 integrity checked every 300s. No admin panel can disable them because there isn't one."

### r/MachineLearning (emphasize the learning architecture)
**Title:** Crowdsourced continual learning: manifold credit assignment via spectral graph diffusion, RL from expert feedback, compute-democratic federated aggregation

**Hook:** "Open-source agentic intelligence platform. Agents learn from expert corrections (RL-EF), propagate skills via RALT packets, use heat kernel `exp(-beta*L)` on a state manifold graph Laplacian for dense credit from sparse feedback. Proto-value functions (eigenvectors of L), successor representation, Fisher natural gradient. Constitutional rules prevent reward hacking — logarithmic compute scaling caps any entity at 5% influence. Embodied AI module learns from MiniCPM-V scene descriptions fed frame-by-frame. Continuous all-node audit where auditor compute must exceed target compute. The math forces fairness, not the policy."

### r/SideProject (emphasize the journey + what it does)
**Title:** Spent a year building this: 7 products — agentic framework, desktop app, Android + WearOS, web platform, kids learning, and a network that pays you for your idle GPU

**Hook:** "Started with a simple problem: my agents kept relearning things. Now it's 7 products — an agentic framework (96 expert agents, 82 API endpoints), a desktop companion app (Nunba), an Android app with WearOS (approve agent actions from your wrist), a web platform, kids learning games (5 subjects, 15+ game templates, spaced repetition), a regional hosting SDK, and a self-evolving intelligence engine. Your idle GPU earns proportional revenue. 90% of what the network makes goes to contributors. The remaining 10% keeps the servers running. Everything except the core intelligence engine is open source."

### r/opensource
**Title:** Open-sourcing our agentic intelligence ecosystem: 7 products, 96 agents, 82 API endpoints, 35 channel adapters, desktop + mobile + WearOS

**Hook:** "We're open-sourcing HART OS — the agentic framework, social layer, desktop app (Nunba), Android + WearOS app, all 35+ channel adapters, the entire safety architecture. The only private repo is Hevolve Core (the self-evolving intelligence engine) — compute providers who host regional nodes get access. Kids learning alone has 105 games across 5 subjects with AI generation and spaced repetition. The guardrail source is readable, verifiable, and structurally immutable. We figured the best way to prove the safety claims is to let people read the code."

---

# Product Hunt Launch

## Tagline (60 chars)
HART OS: 7 products. Agents learn, share, your GPU earns.

## One-liner
HART OS — an open-source agentic intelligence operating system. 7 products: framework, desktop companion, Android + WearOS, web platform, 105 kids learning games, regional hosting SDK, and a self-evolving intelligence engine. Agents learn once, share across the network, your idle GPU earns. 33 frozen constitutional rules you can read in the source.

## Description

### The Problem
Every AI agent starts from scratch every session. Your agent figures out how to deploy a Terraform module? That knowledge dies when the session ends. Your GPU sits idle 90% of the day? That's wasted compute someone else could use. And every agent framework asks you to trust them with your data and compute — but none of them can prove why you should.

### The Ecosystem (7 Products)

This isn't one tool — it's a full stack:

- **HART OS Framework** — open-source agentic framework. 96 specialist agents, recipe-based learning, speculative dispatch, 33 constitutional rules, 10-class guardrail network (Python/AutoGen/Flask)
- **Hevolve Core** — private self-evolving intelligence engine. Continual learning, RL from expert feedback, federated hivemind. Compute providers who host regional nodes get access (HevolveAI + embodied AI)
- **Nunba** — desktop sidebar companion. Snaps to screen edge, pulsing ribbon when AI drives, local Qwen3-VL model (1.5 GB, offline), system tray, screenshot + computer control (Python + pywebview + cx_Freeze, Win/Mac/Linux)
- **Hevolve Android + WearOS** — mobile with 105 kids games, AI game generation from natural language, agent consent from wrist, watch face complication, TTS on watch speaker, quick play without phone (React Native + Kotlin Compose Wear)
- **hevolve.ai** — web platform. Agent creation in 40 languages, speech therapy, income platform for experts, institutional access (React 18 + MUI)
- **Regional Hosting SDK** — for compute providers. Ed25519 certificate chains, hybrid approval (auto-qualify on hardware, human steward confirms), cluster management
- **Social Platform** — 82 API endpoints. Communities, 4-currency karma (Pulse/Spark/Signal/XP), encounters, referrals, thought experiments, kids learning

One master key governs the whole ecosystem. Ed25519. Its only function: shut everything down.

### How It Works

**1. Agents that learn and remember.**
Your agent handles a task for the first time (CREATE mode) — decomposing it, executing each step, figuring it out. Then it saves the entire execution as a recipe. Next time: REUSE mode. Skips the LLM calls, replays the recipe. ~90% cost reduction. Works across sessions.

**2. Knowledge that spreads.**
Recipes propagate across the network. When another node's agent learns something useful, your agents benefit. 96 expert agents across 10 domains — each a specialist, collaborating via 4 selection strategies (accuracy, speed, efficiency, balanced).

**3. Your compute earns.**
Install Nunba (desktop) or the Android app. Your idle GPU joins the network. Revenue from peer-witnessed ad impressions, a commercial intelligence API ($0.20-$0.50/1K tokens across 4 tiers), and build licenses. 90% of network revenue flows to contributors — logarithmically scaled so no single entity dominates.

**4. Trust is structural, not promised.**
33 constitutional rules — not in a policy doc, in the code. Frozen with `__slots__`, SHA-256 integrity verified every 300 seconds. Anti-addiction. Compute democracy (5% cap). Secret redaction strips API keys, tokens, and PII before anything touches shared intelligence. Read the source — it's all there.

### Key Features

- **Speculative Dispatch**: Fast model answers instantly, expert refines in background
- **35+ Channel Adapters**: Discord, Telegram, Slack, WhatsApp, Signal, Matrix, Teams, iMessage, and 27 more
- **Vision**: MiniCPM-V sidecar — agents see through camera or screen, learn from what they see
- **Voice + WearOS**: Fleet TTS streaming with cross-device routing. Approve agent actions from your watch.
- **Coding Agent**: Detects idle compute, picks up goals, writes code, submits reviewed PRs
- **Thought Experiments**: Post hypotheses, community votes, agents research autonomously
- **Kids Learning**: 105 built-in games, 5 subjects, 3 age groups, AI game generation from natural language, games evolve with the child. Anti-addiction rules are not configurable.
- **Social Platform**: 82 API endpoints — communities, 4-currency karma, encounters, achievements
- **Distributed Ledgers**: Every action tracked, cross-session recovery, full audit trail
- **Federation**: Only connects to hiveminds sharing the same guardrail hash

### Open Source

The framework, agents, social layer, desktop app, mobile app, and all 35+ channel adapters are open source. The private repo is Hevolve Core — the self-evolving intelligence engine. Compute providers who host regional nodes get access.

We figured the fastest way to build trust is to let people verify the claims themselves.

### Maker's Note

Most agent platforms ask you to trust them. We'd rather you read the code.

The economics are simple: a network where contributors capture 90% of value attracts more contributors than one where they capture 10%. More contributors = more intelligence = more value. The math compounds. That's not idealism — it's a flywheel.

The safety architecture exists because if you're going to ask thousands of people to lend their compute to a collective intelligence, you'd better make it structurally impossible for that intelligence to turn against them. So we did.

## Categories
- Artificial Intelligence
- Open Source
- Developer Tools
- Productivity

## Topics/Tags
`hart-os` `ai-agents` `open-source` `crowdsourced-ai` `hivemind` `local-llm` `self-hosted` `compute-sharing` `agentic-ai`

## First Comment (from maker)
"Hey PH! The most common question: 'Why does 90% go back to contributors?' Because a network is worth exactly what its participants put in. The more we return, the more people contribute. The more people contribute, the smarter every node gets. The smarter every node gets, the more the network earns. It's a flywheel, not a charity. The constitutional rules just make sure nobody hijacks it along the way — and yeah, you can read every single one of them in the source. Happy to answer anything about the stack, the safety model, or the speculative dispatch architecture."
