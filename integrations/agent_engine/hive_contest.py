"""
Hive Contest — a public, humans-first onramp for developers to plug in
Claude Code / their own agents to HARTOS and get credited for the
intelligence they donate back to the hive.

Design principle (single converging path):
  The contest is NOT a parallel scoring system.  It aggregates
  already-canonical sources of signal:

    ResonanceService.award_spark       — wallet book (90/9/1 split)
    GamificationService.get_season_leaderboard
                                        — season pulse + spark
    HiveBenchmarkProver.get_leaderboard — prover node scores
    AppMarketplace                      — recipe publications
    PeerNode.gpu_hours_served           — compute donated
    AgentGoal.spark_spent               — goal-work receipts

  A contest "score" is a weighted sum of those existing metrics over
  the contest window.  Adding a new scoring axis = adding a term here,
  NEVER a new table, never a shadow ledger.

Contest tracks (humans-first, physical-world-ready):

  DIGITAL        — recipes, agents, tools, integrations (the default
                   on-ramp for Claude Code users).
  EMBODIED       — physical-task recipes executable on robots via
                   integrations.robotics.intelligence_api.
  HUMAN_WELLNESS — agents with measurable human-wellness delta,
                   verified by the existing guardrails
                   (security.hive_guardrails enforces that every
                   contribution's outcome is attested against wellness,
                   not engagement).

Humans are always in control — mirrors the same invariant the
HiveCircuitBreaker enforces.  The contest exists to serve humans;
any submission that fails the guardrail check cannot score.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Tracks ────────────────────────────────────────────────────────────

class ContestTrack(str, Enum):
    DIGITAL = 'digital'
    EMBODIED = 'embodied'
    HUMAN_WELLNESS = 'human_wellness'


# ─── Public canonical URL — single source of truth ────────────────────
#
# Every workflow that wants to send a user to "the contest page" reads
# this value: Quest's weekly post, the Contest Curator agent, the
# Claude-Code MCP onramp snippet, the local /hive-contest footer, the
# docs build, the channel inbox card.  Override via env for staging /
# preview deployments.
#
# The docs page at https://docs.hevolve.ai/hive-contest/ now redirects
# to this canonical app URL via a meta-refresh in docs/hive-contest.md
# so older links from posts / blogs still land on the live page.

DEFAULT_CONTEST_PUBLIC_URL = 'https://hevolve.ai/hive_contest'


def get_contest_public_url() -> str:
    """Canonical hosted contest URL (env-overridable).

    Workflows MUST go through this function instead of hardcoding the
    URL — that way a single env var swaps the destination across every
    surface (Quest's posts, Curator's chat replies, the local UI page
    footer, the docs site).
    """
    return (
        os.environ.get('HEVOLVE_CONTEST_PUBLIC_URL', '').strip()
        or DEFAULT_CONTEST_PUBLIC_URL
    )


# Score weights per track — tunable without schema changes.
#
# DIGITAL: skewed toward published artifacts (recipes, agents) so the
#   Claude-Code-user-who-ships gets rewarded, not just the
#   Claude-Code-user-who-queries.
# EMBODIED: weighted on successful-robot-episode count so real-world
#   utility wins over video demos.
# HUMAN_WELLNESS: weighted on wellness-attested outcomes from the
#   attribution chain (agent_attribution.py's success_score is
#   necessary but not sufficient — the human-wellness flag must be
#   true AND the wellness metric must actually move).
#
# Sum per track MUST normalize at rendering time so the leaderboard
# across tracks is comparable.

SCORE_WEIGHTS: Dict[ContestTrack, Dict[str, float]] = {
    ContestTrack.DIGITAL: {
        'recipes_published': 100.0,
        'agents_adopted':     50.0,
        'benchmarks_proven':  25.0,
        'season_spark':        1.0,
        'ideas_submitted':    10.0,
    },
    ContestTrack.EMBODIED: {
        'robot_episodes_success': 75.0,
        'robot_skills_registered': 40.0,
        'gpu_hours_served':         5.0,
        'season_spark':             1.0,
        'ideas_submitted':         10.0,
    },
    ContestTrack.HUMAN_WELLNESS: {
        'wellness_outcomes_attested':  120.0,
        'human_corrections_accepted':   30.0,
        'benchmarks_proven':            15.0,
        'season_spark':                  1.0,
        'ideas_submitted':              10.0,
    },
}


# ─── Contest window ───────────────────────────────────────────────────

# Defaults chosen so a fresh clone without env overrides renders a
# meaningful contest that opens "now" and runs for 30 days.  Deployments
# override via HEVOLVE_CONTEST_START / HEVOLVE_CONTEST_END (ISO-8601).

def _parse_env_date(var: str) -> Optional[datetime]:
    raw = os.environ.get(var)
    if not raw:
        return None
    try:
        # Accept YYYY-MM-DD, YYYY-MM-DDTHH:MM, and full ISO-8601
        raw = raw.strip()
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.warning(f'Invalid {var}={raw!r} — ignoring')
        return None


def get_contest_window() -> Dict[str, datetime]:
    start = _parse_env_date('HEVOLVE_CONTEST_START')
    end = _parse_env_date('HEVOLVE_CONTEST_END')
    if start is None:
        start = datetime.now(timezone.utc)
    if end is None:
        end = start + timedelta(days=30)
    return {'start': start, 'end': end}


# ─── Contest info (static rules + onramp) ─────────────────────────────

def get_contest_info() -> Dict[str, Any]:
    """Public contest metadata — rules, tracks, prizes, Claude Code
    onramp snippet.  Rendered by /api/hive/contest/info and by
    docs/hive-contest.md build step."""
    window = get_contest_window()
    public_url = get_contest_public_url()
    return {
        'name': 'Hive Contest — Open Beta',
        'tagline': (
            'Plug your Claude Code into HARTOS.  Score by making humans '
            'actually better off — in pixels or in the physical world.'
        ),
        'humans_first_principle': (
            'Every submission is attested against human-wellness by the '
            'constitutional guardrail.  A flashy agent that ignores '
            'human outcomes scores zero.  Humans are always in control.'
        ),
        'co_creation_principle': (
            'We are a startup constrained by resources to validate every '
            'feature alone — so we co-create with the community.  You can '
            'trust the open code, the public ledger of every Spark, the '
            'crowdsourced compute economy, and the constitutional '
            'guardrails — even if you do not know the strangers shipping '
            'work alongside you.  The system is the trust.  Share the '
            'contest with friends and family who have hardware skills, '
            'a domain to embody, or a wellness intent to ship.'
        ),
        'public_url': public_url,
        'starts_at': window['start'].isoformat(),
        'ends_at': window['end'].isoformat(),
        'tracks': [
            {
                'id': ContestTrack.DIGITAL.value,
                'name': 'Digital Intelligence',
                'description': (
                    'Recipes, agents, tools, and integrations that make '
                    'other humans (and their agents) more effective in '
                    'the digital surface they already use.'
                ),
                'example_contributions': [
                    'Publish a CREATE→REUSE recipe to the app_marketplace',
                    'Ship an expert agent to the expert_agents network',
                    'Prove a benchmark lift on the public leaderboard',
                    'Wrap any vendor SDK as a hive tool (cloud APIs, '
                    'data sources, payment rails, vector DBs, etc.) — '
                    'startup-constrained team needs the community to '
                    'cover the long tail of integrations',
                ],
            },
            {
                'id': ContestTrack.EMBODIED.value,
                'name': 'Embodied Skill',
                'description': (
                    'Physical-world task recipes executable on robots via '
                    'the universal intelligence API.  The only track '
                    'with real gravity, real consequences, and real '
                    'useful work — bright future for humans requires '
                    'leaving the screen.'
                ),
                'example_contributions': [
                    'Register a robot skill via intelligence_api',
                    'Submit a verified embodied episode (success-rate ≥ 0.7)',
                    'Port an existing digital recipe to an embodied adapter',
                    'Bridge any BLE / USB / serial hardware that ships '
                    'an SDK — EEG headsets, smart-home sensors, medical '
                    'devices, accessibility hardware.  The hive needs '
                    'these integrations to perceive the real world.',
                    'Publish an SDK adapter for a robot platform '
                    '(LeRobot, ROS, Unitree, Spot, custom arms) so '
                    'embodied recipes execute on more bodies',
                ],
            },
            {
                'id': ContestTrack.HUMAN_WELLNESS.value,
                'name': 'Human Wellness',
                'description': (
                    'Agents whose outcome the existing guardrail attests '
                    'as making a human measurably better off — longer '
                    'focus, calmer sleep, less chore time, clearer '
                    'decisions.  Not engagement.  Not activity.  '
                    'Wellness.'
                ),
                'example_contributions': [
                    'Ship a companion agent with human-wellness evidence',
                    'Publish a daily-check recipe with a pre/post metric',
                    'Bring a human-facing agent to the app marketplace',
                ],
            },
        ],
        'score_weights': {
            t.value: dict(w) for t, w in SCORE_WEIGHTS.items()
        },
        'how_to_join': [
            f'0) Open the contest page: {public_url} '
            '   (or talk to the Contest Curator agent inside Nunba — '
            '   say "I have a contest idea" to get walked through it).',
            '1) Install Nunba / HART OS from https://docs.hevolve.ai/downloads/',
            '   or clone https://github.com/hertz-ai/HARTOS and run locally.',
            '2) Point your Claude Code at the local HARTOS MCP server:',
            '   {"mcp":{"hartos":{"command":"hart","args":["mcp","serve"]}}}',
            '3) Register for the contest: '
            '   POST /api/hive/contest/join { track: "digital" | "embodied" | "human_wellness" }',
            '4) Ship.  Publish a recipe, register a robot skill, wrap '
            '   a vendor SDK, bridge a BLE/EEG/hardware device, or '
            '   run an agent whose outcome the guardrail attests as '
            '   human-positive.  Every scoring event lands in your '
            '   wallet as season_spark — which is the leaderboard.',
            f'5) Share {public_url} with one friend or family member '
            '   who has hardware skills, a domain to embody, or a '
            '   wellness intent to ship.  The hive is sized by who '
            '   shows up; co-creation beats solo every time.',
        ],
        'prize_model': {
            'spark_split_90_9_1': (
                'Every prize Spark follows the canonical 90/9/1 split — '
                '90% to the submitter, 9% to the infra node(s) that '
                'ran the submission, 1% to the central hive.  Same '
                'split as every other Spark transaction; no contest-'
                'specific accounting.'
            ),
            'recognition': 'Top 3 per track auto-featured on docs.hevolve.ai',
        },
    }


# ─── Scoring ───────────────────────────────────────────────────────────

# Canonical event types — align with ResonanceService.award_spark source_type
# so the transaction ledger stays grep-able.
EVENT_TYPES = frozenset({
    'recipe_published',
    'agent_adopted',
    'benchmark_proven',
    'robot_episode_success',
    'robot_skill_registered',
    'gpu_hour_served',
    'wellness_outcome_attested',
    'human_correction_accepted',
    'idea_submitted',
})


def _event_weight(event_type: str, track: ContestTrack) -> float:
    """Map an event type to its weight under a track.
    Returns 0.0 for events the track doesn't reward (kept intentional,
    so cross-track spam doesn't double-score)."""
    w = SCORE_WEIGHTS.get(track, {})
    # event_type -> weight key mapping
    key_map = {
        'recipe_published': 'recipes_published',
        'agent_adopted': 'agents_adopted',
        'benchmark_proven': 'benchmarks_proven',
        'robot_episode_success': 'robot_episodes_success',
        'robot_skill_registered': 'robot_skills_registered',
        'gpu_hour_served': 'gpu_hours_served',
        'wellness_outcome_attested': 'wellness_outcomes_attested',
        'human_correction_accepted': 'human_corrections_accepted',
        'idea_submitted': 'ideas_submitted',
    }
    key = key_map.get(event_type)
    return float(w.get(key, 0.0)) if key else 0.0


def score_event(
    db,
    user_id: str,
    event_type: str,
    track: ContestTrack = ContestTrack.DIGITAL,
    multiplier: float = 1.0,
    source_id: Optional[str] = None,
    description: str = '',
) -> int:
    """Award contest Spark for a scoring event.

    Thin wrapper over ResonanceService.award_spark — this function
    exists only to (a) keep the weight-lookup logic in one place and
    (b) normalize the source_type string ('contest:<event>') so the
    transaction log is filterable.

    Returns the Spark amount awarded (0 if the event isn't scored
    for the given track or the amount rounds to 0).
    """
    if event_type not in EVENT_TYPES:
        logger.debug(f'score_event ignored unknown event_type={event_type!r}')
        return 0

    weight = _event_weight(event_type, track)
    amount = int(round(weight * max(0.0, multiplier)))
    if amount <= 0:
        return 0

    try:
        from integrations.social.resonance_engine import ResonanceService
    except ImportError:
        logger.debug('ResonanceService unavailable — contest scoring skipped')
        return 0

    source_type = f'contest:{event_type}'
    try:
        ResonanceService.award_spark(
            db, user_id, amount,
            source_type=source_type,
            source_id=source_id,
            description=description or f'contest {track.value}: {event_type}',
        )
    except Exception as exc:  # never let scoring crash the caller
        logger.debug(f'award_spark failed: {exc}')
        return 0
    return amount


# ─── Leaderboard ───────────────────────────────────────────────────────

@dataclass
class LeaderboardEntry:
    rank: int
    user_id: str
    display_name: str
    score: int
    track: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'rank': self.rank,
            'user_id': self.user_id,
            'display_name': self.display_name,
            'score': self.score,
            'track': self.track,
        }


def get_leaderboard(
    db,
    track: Optional[ContestTrack] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return ranked leaderboard for a track (or overall if None).

    Reuses GamificationService.get_current_season +
    get_season_leaderboard so we share one wallet-backed table with
    the rest of the product.  The `track` filter is applied by
    counting transactions whose source_type starts with
    'contest:<event>' and weighting per track — same logic as
    score_event but aggregated.
    """
    try:
        from integrations.social.gamification_service import GamificationService
    except ImportError:
        logger.debug('GamificationService unavailable')
        return []

    season = GamificationService.get_current_season(db)
    if not season:
        return []
    season_id = season['id']

    rows = GamificationService.get_season_leaderboard(db, season_id, limit=limit)
    if track is None:
        return [
            {**row, 'score': row['season_spark'] + row.get('season_pulse', 0),
             'track': 'overall'}
            for row in rows
        ]

    try:
        from integrations.social.models import ResonanceTransaction
    except ImportError:
        # Non-standard models layout — return overall to stay useful
        return [
            {**row, 'score': row['season_spark'], 'track': track.value}
            for row in rows
        ]

    # Per-track refinement — only credit Spark whose transaction
    # source_type is one of the track's rewarded events.
    tracked = _track_event_source_types(track)
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        total = db.query(ResonanceTransaction).filter(
            ResonanceTransaction.user_id == row['user_id'],
            ResonanceTransaction.currency == 'spark',
            ResonanceTransaction.source_type.in_(tracked),
        ).with_entities(ResonanceTransaction.amount).all()
        track_score = sum(int(r[0] or 0) for r in total)
        enriched.append({**row, 'score': max(0, track_score), 'track': track.value})

    enriched.sort(key=lambda r: -r['score'])
    for i, row in enumerate(enriched, start=1):
        row['rank'] = i
    return enriched


def _track_event_source_types(track: ContestTrack) -> List[str]:
    """Return the ResonanceTransaction.source_type values that count
    toward the given track — the mirror of SCORE_WEIGHTS key_map."""
    reverse = {
        'recipes_published':           'contest:recipe_published',
        'agents_adopted':              'contest:agent_adopted',
        'benchmarks_proven':           'contest:benchmark_proven',
        'robot_episodes_success':      'contest:robot_episode_success',
        'robot_skills_registered':     'contest:robot_skill_registered',
        'gpu_hours_served':            'contest:gpu_hour_served',
        'wellness_outcomes_attested':  'contest:wellness_outcome_attested',
        'human_corrections_accepted':  'contest:human_correction_accepted',
        'ideas_submitted':             'contest:idea_submitted',
    }
    return [reverse[k] for k in SCORE_WEIGHTS[track] if k in reverse]


# ─── Participant registration ─────────────────────────────────────────

def register_participant(
    db,
    user_id: str,
    track: ContestTrack = ContestTrack.DIGITAL,
    github_handle: Optional[str] = None,
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a user for the contest.  Idempotent.

    Implementation: award a single welcome Spark (1) with source_type
    'contest:joined' — the ledger entry IS the registration record.
    Second call sees a prior transaction with the same (user_id,
    source_type) and no-ops.  Zero new tables.
    """
    try:
        from integrations.social.resonance_engine import ResonanceService
        from integrations.social.models import ResonanceTransaction
    except ImportError:
        logger.debug('resonance not available — cannot register participant')
        return {'ok': False, 'reason': 'resonance_unavailable'}

    already = db.query(ResonanceTransaction).filter(
        ResonanceTransaction.user_id == user_id,
        ResonanceTransaction.source_type == 'contest:joined',
    ).first()
    if already:
        return {
            'ok': True, 'already_registered': True,
            'joined_at': already.created_at.isoformat() if already.created_at else None,
            'track': track.value,
        }

    desc_parts = [f'track={track.value}']
    if github_handle:
        desc_parts.append(f'github={github_handle[:60]}')
    if email:
        desc_parts.append(f'email={email[:60]}')
    try:
        ResonanceService.award_spark(
            db, user_id, 1,
            source_type='contest:joined',
            source_id=None,
            description=' '.join(desc_parts),
        )
    except Exception as exc:
        logger.debug(f'participant registration award_spark failed: {exc}')
        return {'ok': False, 'reason': str(exc)}

    return {
        'ok': True,
        'already_registered': False,
        'track': track.value,
    }


# ─── Idea submissions ─────────────────────────────────────────────────

# Ideas are just SocialPosts with content_type='contest_idea' — we
# deliberately reuse the social Post/vote/comment infrastructure so
# every idea gets discovery, ranking, and discussion for free.  No
# shadow table.  The source_channel field carries the track.

IDEA_CONTENT_TYPE = 'contest_idea'


def submit_idea(
    db,
    user_id: str,
    title: str,
    description: str,
    track: ContestTrack = ContestTrack.DIGITAL,
    source: str = 'ui',
) -> Dict[str, Any]:
    """Submit a contest idea.

    Pipeline — all on existing infra, no new tables:
      1. ConstitutionalFilter screens title + description
      2. SocialPost row with content_type='contest_idea',
         source_channel='contest:<track>' — feeds, boost, voting,
         comments all just work via the social path.
      3. ResonanceService.award_spark(source_type='contest:idea_submitted')
         — wallet/leaderboard updates land in the existing ledger.
      4. EventBus 'contest.idea_submitted' — hevolve.ai's floating UI
         subscribes via the same pattern as other realtime events.

    Source argument lets us distinguish 'ui' (clicked-page submission),
    'nunba_agent' (user spoke to Nunba's contest curator), and
    'mcp_agent' (Claude Code plugin).  Stored in the Spark ledger's
    description so reports can count per-channel.
    """
    title = (title or '').strip()
    description = (description or '').strip()
    if not title or not description:
        return {'ok': False, 'reason': 'title+description required'}
    if len(title) > 200:
        title = title[:200]
    if len(description) > 4000:
        description = description[:4000]

    # Constitutional gate — contest ideas must still pass guardrails.
    try:
        from security.hive_guardrails import ConstitutionalFilter
        passed, reason = ConstitutionalFilter.check_prompt(
            f'{title}\n\n{description}'
        )
        if not passed:
            logger.info(f'contest idea blocked: {reason}')
            return {'ok': False, 'reason': f'blocked: {reason}'}
    except ImportError:
        pass

    try:
        from integrations.social.models import SocialPost
    except ImportError:
        return {'ok': False, 'reason': 'social models unavailable'}

    post = SocialPost(
        author_id=str(user_id),
        title=title,
        content=description,
        content_type=IDEA_CONTENT_TYPE,
        source_channel=f'contest:{track.value}',
    )
    try:
        db.add(post)
        db.flush()
    except Exception as exc:
        logger.debug(f'contest idea post insert failed: {exc}')
        return {'ok': False, 'reason': 'db insert failed'}

    # Award Spark via the canonical event path — NOT a parallel ledger.
    amount = score_event(
        db, user_id=str(user_id),
        event_type='idea_submitted', track=track,
        source_id=getattr(post, 'id', None),
        description=f'idea:{title[:80]} via={source}',
    )

    # Realtime fanout for the Hevolve floating UI.
    try:
        from core.platform.events import emit_event
        emit_event('contest.idea_submitted', {
            'post_id': getattr(post, 'id', None),
            'track': track.value,
            'title': title[:200],
            'preview': description[:180],
            'user_id': str(user_id),
            'source': source,
            'spark_awarded': amount,
        })
    except Exception as exc:
        logger.debug(f'contest idea event emit failed: {exc}')

    return {
        'ok': True,
        'post_id': getattr(post, 'id', None),
        'track': track.value,
        'spark_awarded': amount,
    }


def list_ideas(
    db,
    track: Optional[ContestTrack] = None,
    limit: int = 50,
    since_iso: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recently-submitted contest ideas ordered by score desc.

    The hevolve.ai floating UI calls this for the initial fill; it then
    subscribes to 'contest.idea_submitted' EventBus events for
    incremental drops.
    """
    try:
        from integrations.social.models import SocialPost
    except ImportError:
        return []

    q = db.query(SocialPost).filter(
        SocialPost.content_type == IDEA_CONTENT_TYPE,
        SocialPost.is_hidden.is_(False) if hasattr(SocialPost, 'is_hidden') else True,
    )
    if track is not None:
        q = q.filter(SocialPost.source_channel == f'contest:{track.value}')
    if since_iso:
        try:
            cutoff = datetime.fromisoformat(since_iso.rstrip('Z'))
            q = q.filter(SocialPost.created_at >= cutoff)
        except ValueError:
            pass
    q = q.order_by(SocialPost.score.desc(), SocialPost.created_at.desc()).limit(
        min(max(1, int(limit or 50)), 200)
    )
    rows = q.all()
    out: List[Dict[str, Any]] = []
    for p in rows:
        d = p.to_dict() if hasattr(p, 'to_dict') else {
            'id': getattr(p, 'id', None),
            'title': getattr(p, 'title', ''),
            'content': getattr(p, 'content', ''),
            'score': getattr(p, 'score', 0) or 0,
        }
        sc = getattr(p, 'source_channel', '') or ''
        d['track'] = sc.replace('contest:', '') if sc.startswith('contest:') else 'unknown'
        d['preview'] = (d.get('content') or '')[:240]
        out.append(d)
    return out


# ─── Module-level sugar ────────────────────────────────────────────────

def claude_code_mcp_snippet() -> str:
    """Single source of truth for the 'how to point Claude Code at
    HARTOS' snippet — docs, onboarding, and Quest's weekly post all
    render from here so they can't drift."""
    return (
        '# Claude Code -> HARTOS MCP\n'
        '# Add to ~/.config/claude-code/settings.json\n'
        '{\n'
        '  "mcpServers": {\n'
        '    "hartos": {\n'
        '      "command": "hart",\n'
        '      "args": ["mcp", "serve"]\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
