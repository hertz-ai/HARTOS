# Feature Catalog

All major features of HART OS (Hevolve Agentic Runtime). Each entry links to a dedicated detail page.

| Feature | Description | Details |
|---------|-------------|---------|
| **Agent Engine** | Unified goal engine where GoalManager creates goals, AgentDaemon processes them, and SpeculativeDispatcher runs budget-gated LLM calls. | [agent-engine.md](agent-engine.md) |
| **Recipe Pattern** | Learn task execution once (CREATE mode), then replay efficiently (REUSE mode) without repeated LLM calls -- 90% faster. | [recipe-pattern.md](recipe-pattern.md) |
| **Revenue Model** | 90/9/1 split: 90% to users (contribution-proportional), 9% to infrastructure, 1% to central. | [revenue-model.md](revenue-model.md) |
| **Compute Policies** | Three policies (local_only, local_preferred, any) that control which models a node may use and how metered costs are handled. | [compute-policies.md](compute-policies.md) |
| **Budget Gating** | Pre-dispatch cost control combining per-goal Spark budgets, per-model token cost estimates, and platform-level affordability checks. | [budget-gating.md](budget-gating.md) |
| **Contribution Scoring** | Weighted scoring system that quantifies each node's contributions (uptime, GPU hours, content, API costs absorbed) for revenue distribution. | [contribution-scoring.md](contribution-scoring.md) |
| **Metered API Recovery** | Tracks and compensates nodes that spend real money on paid APIs while serving hive or idle tasks for other users. | [metered-api-recovery.md](metered-api-recovery.md) |
| **Federation & Gossip** | Decentralized peer discovery via UDP gossip, hierarchical sync (local to regional to central), and Ed25519-signed messages. | [federation.md](federation.md) |
| **Social Platform (82 endpoints)** | Full social platform with communities, posts, comments, votes, karma, encounters, referrals, gamification, and ad revenue sharing. | [social-platform.md](social-platform.md) |
| **Thought Experiments** | Structured thought experiments with peer voting and constitutional governance for high-stakes decisions like live trading. | [thought-experiments.md](thought-experiments.md) |
| **Vision / VLM** | Vision sidecar supporting MiniCPM (GPU) and MobileVLM ONNX (CPU) for visual understanding and embodied AI learning. | [vision.md](vision.md) |
| **Coding Agent (idle compute)** | Dispatches coding tasks to the CREATE/REUSE pipeline during node idle time, with three pluggable tool backends. | [coding-agent.md](coding-agent.md) |
| **Trading Agents (paper then live)** | Paper trading with PaperPortfolio and PaperTrade models; live trading requires constitutional vote approval. | [trading-agents.md](trading-agents.md) |
| **Channel Adapters (30+)** | Over 30 channel adapters (Discord, Telegram, Slack, Matrix, WhatsApp, Email, SMS, and more) with a unified send/receive interface. | [channels.md](channels.md) |
