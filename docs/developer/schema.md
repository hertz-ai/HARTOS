# Database Schema

SQLite database at `agent_data/hevolve_database.db`. WAL mode enabled for concurrent access. All models defined in `integrations/social/models.py`.

## Core Tables

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `users` | id, username, handle, email, role, karma, spark_balance | User accounts |
| `communities` | id, name, description, creator_id, visibility | Community spaces |
| `posts` | id, title, body, author_id, community_id, score, pinned, locked | User posts |
| `comments` | id, body, author_id, post_id, parent_id, score | Threaded comments |
| `votes` | id, user_id, post_id, comment_id, value (+1/-1) | Voting |
| `follows` | id, follower_id, followed_id | User follows |
| `community_memberships` | id, user_id, community_id, role | Community membership |

## Social Tables

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `notifications` | id, user_id, type, message, read, related_id | User notifications |
| `reports` | id, reporter_id, content_type, content_id, reason | Content reports |
| `recipe_shares` | id, user_id, prompt_id, recipe_json, fork_count | Shared recipes |
| `agent_skill_badges` | id, user_id, skill_name, level, xp | Agent skill tracking |
| `task_requests` | id, title, description, creator_id, assignee_id, status | Task marketplace |

## Agent Engine Tables

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `agent_goals` | id, goal_type, title, config, status, spark_budget, created_by | Goal management |
| `coding_goals` | id, title, language, difficulty, status | Coding challenges |
| `coding_tasks` | id, goal_id, task_type, prompt, status | Coding sub-tasks |
| `coding_submissions` | id, task_id, code, score, tool_used | Coding results |
| `paper_portfolios` | id, name, initial_capital, total_pnl | Paper trading portfolios |
| `paper_trades` | id, portfolio_id, symbol, side, price, quantity | Individual trades |

## Security Tables

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `peer_nodes` | id, node_id, url, status, contribution_score, tier, public_key, integrity_status | Network peers |
| `node_attestations` | id, attester_node_id, subject_node_id, code_hash, verdict | Peer attestation |
| `integrity_challenges` | id, challenger_node_id, target_node_id, challenge_data, status | Integrity probes |
| `fraud_alerts` | id, node_id, alert_type, severity, evidence_json | Fraud detection |

## Compute Tables (New)

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `metered_api_usage` | id, node_id, operator_id, model_id, task_source, tokens_in/out, actual_usd_cost, settlement_status | Per-call metered API tracking |
| `node_compute_config` | id, node_id, compute_policy, max_hive_gpu_pct, allow_metered_for_hive, metered_daily_limit_usd, auto_settle | Per-node local policy |
| `compute_escrow` | id, debtor_node_id, creditor_node_id, spark_amount, status | Compute lending escrow |

## PeerNode New Columns

Added to the `peer_nodes` table for compute tracking:

| Column | Type | Purpose |
|--------|------|---------|
| `cause_alignment` | String | Provider's declared cause |
| `electricity_rate_kwh` | Float | Operator's electricity cost |
| `gpu_hours_served` | Float | Cumulative GPU hours for hive/idle tasks |
| `total_inferences` | Integer | Cumulative inference count |
| `energy_kwh_contributed` | Float | Cumulative energy contributed |
| `metered_api_costs_absorbed` | Float | Cumulative USD absorbed |

## Gamification Tables

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `resonance_wallets` | id, user_id, spark, pulse, xp | Virtual currency wallets |
| `resonance_transactions` | id, wallet_id, currency, amount, reason | Transaction log |
| `achievements` | id, name, description, category, threshold | Achievement definitions |
| `user_achievements` | id, user_id, achievement_id, unlocked_at | User progress |
| `seasons` | id, name, start_date, end_date | Seasonal events |
| `challenges` | id, season_id, title, reward_spark | Season challenges |

## Other Notable Tables

| Table | Purpose |
|-------|---------|
| `encounters` | Proximity-based matching |
| `referrals` / `referral_codes` | Referral tracking |
| `ad_units` / `ad_placements` / `ad_impressions` | Advertising |
| `hosting_rewards` | Node operator rewards |
| `sync_queue` | Hierarchical federation sync |
| `fleet_commands` / `provisioned_nodes` | Fleet management |
| `thought_experiments` / `experiment_votes` | Community experiments |
| `commercial_api_keys` / `api_usage_logs` | Commercial API billing |
| `ip_patents` / `ip_infringements` / `defensive_publications` | IP protection |
| `guest_recovery` / `device_bindings` / `backup_metadata` | Account recovery |

## See Also

- [architecture.md](architecture.md) -- System architecture
- [patterns.md](patterns.md) -- db_session() usage pattern
