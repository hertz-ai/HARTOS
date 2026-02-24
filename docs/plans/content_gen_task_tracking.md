# Plan: Content Generation Task Tracking & Watchdog System

## Problem

Games in the Kids Learning Zone that need server-side content (images, TTS, music, video) currently show "Coming soon!" with no visibility into what's happening. Developers can't see job IDs, progress, or why things are stuck. The in-memory `_async_jobs` dict in `kids_media_routes.py` is lost on restart and has no progress snapshots.

## Architecture

Reuse existing patterns — no new daemons, no new tables needed.

### Key Insight: Use `AgentGoal` + `SmartLedger` + `DashboardService`

A content generation pipeline for a game is just another goal (`goal_type='content_gen'`) with subtasks in a SmartLedger. The existing AgentDaemon dispatches it, NodeWatchdog monitors it, DashboardService surfaces stalls. We only add:

1. **ContentGenTracker** — a thin service that links games to their content gen goals
2. **Content gen monitor** — periodic check in AgentDaemon (like auto_remediate_loopholes)
3. **API endpoints** — query per-game task status with job IDs and 24h delta
4. **Frontend overlay** — game cards show progress instead of "Coming soon!"

---

## Implementation Steps

### Step 1: `ContentGenTracker` service (Backend — new file)

**File**: `integrations/agent_engine/content_gen_tracker.py` (~200 lines)

Creates and tracks content generation goals per game. Each game gets one `AgentGoal` with `goal_type='content_gen'` and a SmartLedger breaking it into subtasks (image gen, TTS, music, etc.).

```python
class ContentGenTracker:
    """Track content generation tasks per game."""

    @staticmethod
    def get_or_create_game_goal(db, game_id, game_config) -> Dict:
        """Find existing content_gen goal for game or create one.
        Uses config_json.game_id for lookup. Idempotent (slug pattern)."""

    @staticmethod
    def get_game_progress(db, game_id) -> Dict:
        """Returns {game_id, status, progress_pct, tasks: [{task_id, job_id,
        type, status, progress_pct, delta_24h}], created_at, updated_at}.
        Computes 24h delta from progress_snapshots in config_json."""

    @staticmethod
    def record_progress_snapshot(db, game_id):
        """Append {timestamp, progress_pct} to config_json.progress_snapshots.
        Called once per daemon tick. Keeps last 7 days of snapshots."""

    @staticmethod
    def get_stuck_games(db, stall_threshold_hours=24) -> List[Dict]:
        """Games where progress hasn't changed in stall_threshold_hours.
        Computes delta from progress_snapshots."""

    @staticmethod
    def attempt_unblock(db, game_id) -> Dict:
        """Retry failed subtasks, restart stalled services, escalate.
        Returns {action_taken, success, detail}."""

    @staticmethod
    def get_all_game_tasks(db) -> List[Dict]:
        """All content_gen goals with per-task breakdown for admin dashboard."""
```

**Progress snapshots**: Stored in `AgentGoal.config_json.progress_snapshots` as:
```json
{
  "game_id": "eng-spell-animals-01",
  "media_requirements": {"images": 8, "tts": 24, "music": 1},
  "progress_snapshots": [
    {"ts": "2026-02-20T10:00:00", "pct": 45.0},
    {"ts": "2026-02-21T10:00:00", "pct": 72.0}
  ]
}
```

24h delta = latest snapshot pct - snapshot closest to 24h ago. If delta == 0 for >24h → stuck.

### Step 2: Register `content_gen` goal type (Backend — edit existing)

**File**: `integrations/agent_engine/goal_manager.py` (add ~30 lines)

```python
def _build_content_gen_prompt(goal_dict, product_dict=None):
    """Build prompt for content generation monitor agent."""
    config = goal_dict.get('config_json', {})
    game_id = config.get('game_id', 'unknown')
    return f"""You are a content generation monitor for game '{game_id}'.
    Check status of all media generation tasks (images, TTS, music, video).
    For stuck tasks: retry the generation, check if the service is running,
    escalate if retry fails. Report progress percentage and any blockers."""

register_goal_type('content_gen', _build_content_gen_prompt, tool_tags=['content_gen'])
```

This single line makes the entire daemon + watchdog + dashboard infrastructure aware of content gen goals.

### Step 3: Content gen tools (Backend — new file)

**File**: `integrations/agent_engine/content_gen_tools.py` (~150 lines)

AutoGen tools for the content_gen goal type:

```python
def get_content_gen_status(game_id: str) -> str:
    """Get content generation status for a game. Returns JSON with
    per-task breakdown, progress_pct, 24h delta, stuck_tasks."""

def retry_stuck_task(game_id: str, task_type: str) -> str:
    """Retry a stuck content generation task (image/tts/music/video).
    Checks if the service tool is running, restarts if needed."""

def check_media_services() -> str:
    """Check health of all media generation services
    (txt2img, TTS-Audio-Suite, AceStep, Wan2GP/LTX2).
    Returns which are running, which need restart."""

def force_regenerate(game_id: str, asset_type: str, prompt: str) -> str:
    """Force regeneration of a specific asset. Clears cache first."""
```

### Step 4: API endpoints (Backend — new file)

**File**: `integrations/agent_engine/api_content_gen.py` (~120 lines)

Blueprint at `/api/social/content-gen/`:

```
GET  /api/social/content-gen/games                  — all games with content gen status
GET  /api/social/content-gen/games/<game_id>         — single game task breakdown
GET  /api/social/content-gen/games/<game_id>/progress — progress + 24h delta
GET  /api/social/content-gen/stuck                   — all stuck games (delta=0 for >24h)
POST /api/social/content-gen/games/<game_id>/retry    — manual retry of stuck tasks
GET  /api/social/content-gen/services                — media service health check
```

Register in `integrations/social/__init__.py` via `init_social()`.

### Step 5: Wire monitor into AgentDaemon (Backend — edit existing)

**File**: `integrations/agent_engine/agent_daemon.py` (add ~20 lines)

In `_tick()`, add periodic content gen monitoring (every 5 ticks = ~2.5 min):

```python
# Content generation watchdog — detect stuck jobs, attempt unblock
if self._tick_count % 5 == 0:
    try:
        from .content_gen_tracker import ContentGenTracker
        stuck = ContentGenTracker.get_stuck_games(db, stall_threshold_hours=24)
        for game in stuck:
            result = ContentGenTracker.attempt_unblock(db, game['game_id'])
            if result['action_taken']:
                logger.info(f"Content gen unblock: game={game['game_id']} "
                           f"action={result['action_taken']}")
        # Record progress snapshots for all active content gen goals
        active_goals = GoalManager.list_goals(db, goal_type='content_gen', status='active')
        for goal in active_goals:
            ContentGenTracker.record_progress_snapshot(db, goal['config_json'].get('game_id'))
    except Exception as e:
        logger.debug(f"Content gen monitor tick skipped: {e}")
```

### Step 6: Persist async jobs from kids_media_routes (Nunba backend — edit)

**File**: `C:\Users\sathi\PycharmProjects\Nunba\kids_media_routes.py` (add ~40 lines)

Replace in-memory `_async_jobs` dict with a simple JSON file persistence:

```python
_JOBS_FILE = os.path.join(os.path.expanduser('~/Documents/Nunba/data'), 'media_jobs.json')

def _save_jobs():
    """Persist active jobs to disk for restart resilience."""
    with _jobs_lock:
        with open(_JOBS_FILE, 'w') as f:
            json.dump(_async_jobs, f, default=str)

def _load_jobs():
    """Restore jobs on startup. Mark incomplete jobs as 'interrupted'."""
    global _async_jobs
    if os.path.exists(_JOBS_FILE):
        with open(_JOBS_FILE) as f:
            _async_jobs = json.load(f)
        # Mark any 'pending'/'generating' jobs as 'interrupted' (server restarted)
        for job in _async_jobs.values():
            if job.get('status') in ('pending', 'generating'):
                job['status'] = 'interrupted'
                job['error'] = 'Server restarted during generation'
```

Add `game_id` field to each job when created, link to the Hevolve backend content gen tracker via POST on status changes.

### Step 7: Frontend — ContentGenStatusOverlay (Nunba — new file)

**File**: `Nunba/landing-page/src/components/Social/KidsLearning/shared/ContentGenStatus.jsx` (~180 lines)

A reusable component showing content generation progress:

```jsx
function ContentGenStatus({ gameId, compact = false }) {
  // Fetches GET /api/social/content-gen/games/{gameId}
  // Shows: overall progress bar, per-task status with job IDs
  // Color coding: green (progressing), amber (slow, delta < 5%), red (stuck, delta = 0)
  // Compact mode: just progress bar + "Creating game... 72%" for game cards
  // Full mode: task breakdown table with job IDs for developer view
}
```

**Compact mode** (game cards in KidsLearningHub):
```
┌──────────────────────┐
│ 🎵 Animal Spelling   │
│ ━━━━━━━━━━░░░ 72%   │
│ Creating game...     │
│ ▲ +15% from yesterday│
└──────────────────────┘
```

**Full mode** (developer view / KidsGameScreen placeholder):
```
┌────────────────────────────────────┐
│ Game Creation In Progress — 72%    │
│ ━━━━━━━━━━━━━━━━░░░░░░ 72%       │
│ ▲ +15% vs yesterday               │
│                                    │
│ Task          Job ID      Status   │
│ ───────────── ─────────── ──────── │
│ Images (6/8)  img_a3f2..  ⏳ 75%  │
│ TTS (20/24)   tts_b1c4..  ⏳ 83%  │
│ Music (0/1)   mus_d5e6..  🔴 stuck│
│   └─ No progress for 26h          │
│   └─ AceStep service: offline     │
│   └─ [Retry] [Restart Service]    │
└────────────────────────────────────┘
```

### Step 8: Wire into KidsLearningHub game cards (Nunba — edit)

**File**: `KidsLearningHub.jsx` (add ~15 lines)

In the game card render, check if game has active content gen tasks:

```jsx
// Inside the Card for each game
{gameContentStatus[game.id]?.status === 'in_progress' && (
  <ContentGenStatus gameId={game.id} compact />
)}
```

Fetch content gen status for all games once on mount:
```jsx
useEffect(() => {
  contentGenApi.getAllGames().then(setGameContentStatus).catch(() => {});
}, []);
```

### Step 9: Wire into KidsGameScreen placeholder (Nunba — edit)

**File**: `KidsGameScreen.jsx` (add ~10 lines)

Replace the static "Coming soon!" PlaceholderTemplate with ContentGenStatus:

```jsx
function PlaceholderTemplate({ config, onComplete }) {
  return (
    <Box sx={{ textAlign: 'center', py: 4 }}>
      <ContentGenStatus gameId={config?.id} compact={false} />
      <Typography variant="body2" onClick={() => onComplete?.({correct:0,total:0})}
        sx={{ color: kidsColors.primary, cursor: 'pointer', mt: 2 }}>
        Go back to hub
      </Typography>
    </Box>
  );
}
```

### Step 10: Admin developer page (Nunba — new file)

**File**: `Nunba/landing-page/src/components/Admin/ContentTasksPage.js` (~250 lines)

Full developer dashboard for content generation:

```
┌─────────────────────────────────────────────────────────┐
│ Content Generation Tasks                    [Refresh 🔄]│
│                                                         │
│ Services Health:                                        │
│   txt2img: ✅ running  │ TTS: ✅ running                │
│   AceStep: ❌ offline  │ Wan2GP: ⚠️ starting            │
│                                                         │
│ ┌─ Stuck Games (2) ────────────────────────────────────┐│
│ │ eng-spell-animals-01  │ 72% │ Δ 0% (26h) │ 🔴 STUCK││
│ │   └─ music task: AceStep offline                     ││
│ │   └─ [Retry] [Restart AceStep] [Skip Music]         ││
│ │ create-word-art-07    │ 45% │ Δ 0% (18h) │ ⚠️ SLOW  ││
│ └──────────────────────────────────────────────────────┘│
│                                                         │
│ ┌─ All Games (24) ─────────────────────────────────────┐│
│ │ Game ID          │ Progress │ 24h Δ  │ Status        ││
│ │ eng-quiz-01      │ 100%     │ —      │ ✅ complete   ││
│ │ math-count-01    │ 95%      │ +12%   │ ⏳ generating ││
│ │ eng-spell-01     │ 72%      │  0%    │ 🔴 stuck     ││
│ │ ...              │          │        │               ││
│ └──────────────────────────────────────────────────────┘│
│                                                         │
│ ┌─ Monitor Agent ──────────────────────────────────────┐│
│ │ Last check: 2m ago │ Next: 30s │ Unblocks today: 3  ││
│ │ Auto-retried: 5 │ Escalated: 1 │ Stuck resolved: 2  ││
│ └──────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
```

Register in MainRoute.js as `/admin/content-tasks` and add to AdminLayout nav.

### Step 11: Frontend API client (Nunba — edit)

**File**: `Nunba/landing-page/src/services/socialApi.js` (add ~20 lines)

```javascript
export const contentGenApi = {
  getAllGames: () => socialApi.get('/content-gen/games'),
  getGame: (gameId) => socialApi.get(`/content-gen/games/${gameId}`),
  getProgress: (gameId) => socialApi.get(`/content-gen/games/${gameId}/progress`),
  getStuck: () => socialApi.get('/content-gen/stuck'),
  retryGame: (gameId) => socialApi.post(`/content-gen/games/${gameId}/retry`),
  getServices: () => socialApi.get('/content-gen/services'),
};
```

### Step 12: Tests (Backend)

**File**: `tests/test_content_gen_tracker.py` (~300 lines)

Test classes:
- `TestContentGenTracker` — CRUD, progress snapshots, delta computation
- `TestStuckDetection` — stall detection with configurable thresholds
- `TestUnblockLogic` — retry, service restart, escalation
- `TestContentGenAPI` — all 6 endpoints
- `TestDaemonIntegration` — monitor tick creates/tracks/unblocks goals

---

## File Summary

| # | File | Repo | Action | Lines |
|---|------|------|--------|-------|
| 1 | `integrations/agent_engine/content_gen_tracker.py` | Backend | NEW | ~200 |
| 2 | `integrations/agent_engine/goal_manager.py` | Backend | EDIT | +30 |
| 3 | `integrations/agent_engine/content_gen_tools.py` | Backend | NEW | ~150 |
| 4 | `integrations/agent_engine/api_content_gen.py` | Backend | NEW | ~120 |
| 5 | `integrations/social/__init__.py` | Backend | EDIT | +6 |
| 6 | `integrations/agent_engine/agent_daemon.py` | Backend | EDIT | +20 |
| 7 | `kids_media_routes.py` | Nunba | EDIT | +40 |
| 8 | `KidsLearning/shared/ContentGenStatus.jsx` | Nunba | NEW | ~180 |
| 9 | `KidsLearningHub.jsx` | Nunba | EDIT | +15 |
| 10 | `KidsGameScreen.jsx` | Nunba | EDIT | +10 |
| 11 | `Admin/ContentTasksPage.js` | Nunba | NEW | ~250 |
| 12 | `MainRoute.js` + `AdminLayout.js` | Nunba | EDIT | +10 |
| 13 | `services/socialApi.js` | Nunba | EDIT | +20 |
| 14 | `tests/test_content_gen_tracker.py` | Backend | NEW | ~300 |

**Total**: ~1,350 lines across 14 files (5 new, 9 edits)

## Design Decisions

1. **No new DB table** — Uses existing `AgentGoal` with `goal_type='content_gen'` and `config_json` for snapshots. Zero schema migration.
2. **No new daemon** — Content gen monitoring runs inside existing `AgentDaemon._tick()`. NodeWatchdog already monitors the daemon.
3. **24h delta** — Stored as timestamped snapshots in `config_json.progress_snapshots`. Delta computed client-side from last two relevant snapshots. Snapshots pruned to 7 days.
4. **Stuck detection** — A game is "stuck" when `delta_24h == 0` AND `progress_pct < 100`. "Slow" when `delta_24h < 5%`.
5. **Auto-unblock strategy**: (a) Retry failed subtask, (b) Check if media service is running → restart via RuntimeToolManager, (c) If service can't start → mark task as `deferred` and log escalation.
6. **Frontend compact/full modes** — Game cards show compact progress bar. PlaceholderTemplate and admin page show full task breakdown with job IDs.
