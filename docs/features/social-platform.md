# Social Platform

82-endpoint social platform integrated into HART OS.

## Core Modules

| Module | Endpoints | Description |
|--------|-----------|-------------|
| **Communities** | CRUD, membership, moderation | Create and manage communities with roles and permissions. |
| **Posts** | Create, edit, delete, list, feed | User-generated content within communities. |
| **Comments** | Threaded replies | Nested comment trees on posts. |
| **Votes** | Upvote, downvote | Voting on posts and comments; drives karma calculation. |
| **Karma** | Score queries | Aggregated reputation score derived from votes and activity. |
| **Encounters** | Create, list, match | Peer-to-peer encounter system for agent and user matching. |
| **Referrals** | Generate, track, redeem | Referral codes with Spark rewards for both referrer and referee. |

## Gamification

| Feature | Description |
|---------|-------------|
| **Achievements** | Unlockable badges for milestones (first post, 100 upvotes, etc.). |
| **Seasons** | Time-bounded competitive periods with leaderboards. |
| **Challenges** | Task-based challenges with Spark rewards. |

## Ad System

The ad system follows the platform-wide 90/9/1 revenue split (see [revenue-model.md](revenue-model.md)):

- Advertisers purchase impressions with Spark.
- 90% of ad revenue flows to the user pool.
- 9% to infrastructure.
- 1% to central.

## Additional Features

- **Thought experiments** -- Structured proposals with peer voting (see [thought-experiments.md](thought-experiments.md)).
- **Fleet OTA updates** -- Over-the-air update distribution to managed node fleets.

## Source Files

- `integrations/social/models.py`
- `integrations/social/services.py`
- `integrations/social/__init__.py` (route registration)
