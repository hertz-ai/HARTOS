"""
Thought Experiment Agent Tools — AutoGen tools for thought experiment coordination.

8 tools for creating, voting, evaluating, and managing thought experiments.
Includes autoresearch integration: software thought experiments can spawn
autonomous edit→run→score→iterate loops at hive scale.
Tier 2 tools (agent_engine context). Same pattern as learning_tools.py.
"""
import json
import logging
import threading

try:
    from core.session_cache import TTLCache
    _file_locks = TTLCache(ttl_seconds=86400, max_size=50000, name='thought_exp_locks')
except ImportError:
    _file_locks = {}
_file_locks_guard = threading.Lock()

logger = logging.getLogger('hevolve_social')


def create_thought_experiment(creator_id: str, title: str,
                               hypothesis: str,
                               expected_outcome: str = '',
                               intent_category: str = 'technology',
                               is_core_ip: bool = False) -> str:
    """Create a new constitutional thought experiment."""
    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session() as db:
            result = ThoughtExperimentService.create_experiment(
                db, creator_id, title, hypothesis,
                expected_outcome=expected_outcome,
                intent_category=intent_category,
                is_core_ip=is_core_ip)
            if result:
                return json.dumps({'success': True, 'experiment': result})
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Blocked by ConstitutionalFilter or invalid input',
                })
    except Exception as e:
        return json.dumps({'error': str(e)})


def cast_experiment_vote(experiment_id: str, voter_id: str,
                          vote_value: int = 0,
                          reasoning: str = '',
                          suggestion: str = '',
                          voter_type: str = 'agent',
                          confidence: float = 0.8) -> str:
    """Cast a vote on a thought experiment (as agent or human)."""
    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session() as db:
            result = ThoughtExperimentService.cast_vote(
                db, experiment_id, voter_id,
                vote_value=int(vote_value),
                reasoning=reasoning,
                suggestion=suggestion,
                voter_type=voter_type,
                confidence=float(confidence))
            if result:
                return json.dumps({'success': True, 'vote': result})
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Experiment not found or not in voting phase',
                })
    except Exception as e:
        return json.dumps({'error': str(e)})


def evaluate_thought_experiment(experiment_id: str, agent_id: str,
                                  score: float = 0.0,
                                  confidence: float = 0.8,
                                  reasoning: str = '',
                                  evidence: str = '') -> str:
    """Record an agent evaluation for a thought experiment."""
    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session() as db:
            result = ThoughtExperimentService.record_agent_evaluation(
                db, experiment_id, agent_id,
                score=float(score),
                confidence=float(confidence),
                reasoning=reasoning,
                evidence=evidence)
            if result:
                return json.dumps({'success': True, 'experiment': result})
            else:
                return json.dumps({'success': False, 'reason': 'not_found'})
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_experiment_status(experiment_id: str = '',
                           status_filter: str = '') -> str:
    """Get experiment detail or list experiments by status."""
    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session(commit=False) as db:
            if experiment_id:
                result = ThoughtExperimentService.get_experiment_detail(
                    db, experiment_id)
                return json.dumps({'success': True, 'experiment': result})
            else:
                results = ThoughtExperimentService.get_active_experiments(
                    db, status=status_filter or None)
                return json.dumps({
                    'success': True,
                    'experiments': results,
                    'count': len(results),
                })
    except Exception as e:
        return json.dumps({'error': str(e)})


def tally_experiment_votes(experiment_id: str) -> str:
    """Get the current vote tally for an experiment."""
    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session(commit=False) as db:
            tally = ThoughtExperimentService.tally_votes(db, experiment_id)
            return json.dumps({'success': True, 'tally': tally})
    except Exception as e:
        return json.dumps({'error': str(e)})


def advance_experiment(experiment_id: str,
                        target_status: str = '') -> str:
    """Advance experiment to next lifecycle phase or specific status."""
    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session() as db:
            result = ThoughtExperimentService.advance_status(
                db, experiment_id,
                target_status=target_status or None)
            if result:
                return json.dumps({'success': True, 'experiment': result})
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Cannot advance (invalid status or not found)',
                })
    except Exception as e:
        return json.dumps({'error': str(e)})


def iterate_hypothesis(experiment_id: str, hypothesis: str,
                       approach: str = '', evidence: str = '',
                       iteration: int = 0) -> str:
    """Propose and evaluate a hypothesis iteration for ANY thought experiment.

    This is the generic iteration tool — works for traditional, research,
    physical_ai, or any experiment type. The agent calls this in a loop:
    propose hypothesis → gather evidence → score → refine → repeat.

    For software experiments, use launch_experiment_autoresearch instead.

    Respects owner pause — if the experiment creator paused evolution,
    this tool returns a pause signal and the agent should stop iterating.

    Args:
        experiment_id: The ThoughtExperiment ID
        hypothesis: The refined hypothesis for this iteration
        approach: How you plan to test/evaluate this hypothesis
        evidence: Evidence or reasoning supporting this iteration
        iteration: Current iteration number (for tracking)

    Returns:
        JSON with iteration record and experiment context
    """
    # Check owner pause
    try:
        from integrations.agent_engine.auto_evolve import is_experiment_paused
        if is_experiment_paused(experiment_id):
            return json.dumps({
                'success': False,
                'paused': True,
                'reason': 'Experiment paused by owner. Stop iterating.',
                'instruction': 'The experiment owner has paused evolution. '
                               'Do NOT continue iterating. Wait for resume.',
            })
    except ImportError:
        pass

    try:
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        with db_session(commit=False) as db:
            detail = ThoughtExperimentService.get_experiment_detail(
                db, experiment_id)
            if not detail:
                return json.dumps({'error': 'Experiment not found'})

            # Build iteration record
            iteration_record = {
                'iteration': iteration,
                'hypothesis': hypothesis,
                'approach': approach,
                'evidence': evidence,
                'status': 'proposed',
            }

            # Return context for the agent to evaluate
            return json.dumps({
                'success': True,
                'iteration': iteration_record,
                'experiment': {
                    'id': experiment_id,
                    'title': detail.get('title', ''),
                    'original_hypothesis': detail.get('hypothesis', ''),
                    'expected_outcome': detail.get('expected_outcome', ''),
                    'intent_category': detail.get('intent_category', ''),
                    'status': detail.get('status', ''),
                },
                'instruction': (
                    'Now evaluate this hypothesis. Use score_hypothesis_result '
                    'to record your score (-2 to +2). Consider: evidence quality, '
                    'clarity, feasibility, and expected impact.'
                ),
            })
    except Exception as e:
        return json.dumps({'error': str(e)})


def score_hypothesis_result(experiment_id: str, iteration: int,
                             score: float, reasoning: str,
                             evidence_quality: float = 0.0,
                             clarity: float = 0.0,
                             feasibility: float = 0.0,
                             impact: float = 0.0) -> str:
    """Score a hypothesis iteration using a structured rubric.

    Generic scoring tool for all experiment types. The agent uses this
    after evaluating a hypothesis to decide whether to keep iterating
    or converge on a conclusion.

    Args:
        experiment_id: The ThoughtExperiment ID
        iteration: Iteration number being scored
        score: Overall score (-2 to +2)
        reasoning: Why this score
        evidence_quality: Sub-score for evidence (0-1)
        clarity: Sub-score for hypothesis clarity (0-1)
        feasibility: Sub-score for feasibility (0-1)
        impact: Sub-score for expected impact (0-1)

    Returns:
        JSON with score record, trend analysis, and continuation advice
    """
    import os
    import tempfile

    score = max(-2.0, min(2.0, float(score)))

    # Load or create iteration history file
    data_dir = os.path.join(
        os.path.dirname(__file__), '..', '..', 'agent_data', 'experiment_iterations')
    os.makedirs(data_dir, exist_ok=True)
    history_path = os.path.join(data_dir, f'{experiment_id}.json')

    # Per-experiment lock prevents read-modify-write race
    with _file_locks_guard:
        if experiment_id not in _file_locks:
            _file_locks[experiment_id] = threading.Lock()
        lock = _file_locks[experiment_id]

    with lock:
        history = []
        if os.path.isfile(history_path):
            try:
                with open(history_path, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except Exception:
                history = []

        record = {
            'iteration': iteration,
            'score': score,
            'reasoning': reasoning,
            'rubric': {
                'evidence_quality': max(0.0, min(1.0, float(evidence_quality))),
                'clarity': max(0.0, min(1.0, float(clarity))),
                'feasibility': max(0.0, min(1.0, float(feasibility))),
                'impact': max(0.0, min(1.0, float(impact))),
            },
        }
        history.append(record)

        # Atomic write: temp file + rename prevents partial writes
        try:
            fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(history, f, indent=2, default=str)
                os.replace(tmp_path, history_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            pass

    # Trend analysis
    scores = [h['score'] for h in history]
    best_score = max(scores)
    best_iter = scores.index(best_score)
    improving = len(scores) >= 2 and scores[-1] > scores[-2]
    stagnant = len(scores) >= 3 and len(set(scores[-3:])) == 1

    # Convergence advice
    if stagnant:
        advice = 'CONVERGE — 3 consecutive same scores. Record final evaluation.'
    elif len(scores) >= 10:
        advice = 'BUDGET — 10 iterations reached. Record final evaluation with best hypothesis.'
    elif improving:
        advice = 'CONTINUE — score is improving. Iterate with refined hypothesis.'
    elif score >= 1.5:
        advice = 'STRONG — high score. Consider recording final evaluation.'
    else:
        advice = 'CONTINUE — try a different approach or angle.'

    return json.dumps({
        'success': True,
        'record': record,
        'trend': {
            'total_iterations': len(scores),
            'best_score': best_score,
            'best_iteration': best_iter,
            'improving': improving,
            'stagnant': stagnant,
        },
        'advice': advice,
    })


def get_iteration_history(experiment_id: str, last_n: int = 10) -> str:
    """Get the iteration history for a thought experiment.

    Returns past hypothesis iterations with scores and trend analysis.
    The agent uses this to inform its next hypothesis refinement.

    Args:
        experiment_id: The ThoughtExperiment ID
        last_n: Number of recent iterations to return (default 10)

    Returns:
        JSON with iteration history and summary statistics
    """
    import os

    data_dir = os.path.join(
        os.path.dirname(__file__), '..', '..', 'agent_data', 'experiment_iterations')
    history_path = os.path.join(data_dir, f'{experiment_id}.json')

    if not os.path.isfile(history_path):
        return json.dumps({
            'success': True,
            'history': [],
            'summary': 'No iterations yet. Use iterate_hypothesis to start.',
        })

    try:
        with open(history_path, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except Exception:
        history = []

    last_n = min(int(last_n), len(history))
    recent = history[-last_n:] if last_n > 0 else history

    scores = [h['score'] for h in history]
    summary = {
        'total_iterations': len(history),
        'best_score': max(scores) if scores else None,
        'worst_score': min(scores) if scores else None,
        'avg_score': round(sum(scores) / len(scores), 2) if scores else None,
        'improving_trend': (
            len(scores) >= 2 and scores[-1] > scores[-2]
        ),
    }

    return json.dumps({
        'success': True,
        'history': recent,
        'summary': summary,
    })


def launch_experiment_autoresearch(experiment_id: str,
                                    repo_path: str,
                                    target_file: str,
                                    run_command: str,
                                    metric_name: str = 'score',
                                    metric_pattern: str = '',
                                    metric_direction: str = 'higher_is_better',
                                    max_iterations: int = 50,
                                    time_budget_s: int = 300,
                                    hive_parallel: bool = False) -> str:
    """Launch an autoresearch loop for a software thought experiment.

    When a thought experiment has experiment_type='software' and reaches the
    evaluating phase, this tool starts the autonomous edit→run→score→iterate
    loop. The engine modifies target_file, runs run_command, extracts the
    metric, keeps improvements, and iterates until budget or max_iterations.

    At hive scale: when hive_parallel=True, multiple hypothesis variants run
    simultaneously across compute mesh peers (tournament selection picks best).

    Args:
        experiment_id: The ThoughtExperiment ID to attach results to
        repo_path: Path to the git repository
        target_file: The file to modify (relative to repo_path)
        run_command: Shell command to run the experiment
        metric_name: Name of the metric to optimize
        metric_pattern: Regex with group(1) to extract metric from output
        metric_direction: 'higher_is_better' or 'lower_is_better'
        max_iterations: Max iterations before stopping
        time_budget_s: Per-iteration time budget in seconds
        hive_parallel: If True, run parallel variants across hive peers

    Returns:
        JSON with session_id and status
    """
    try:
        from integrations.coding_agent.autoevolve_code_tools import start_autoresearch
        return start_autoresearch(
            repo_path=repo_path,
            target_file=target_file,
            run_command=run_command,
            metric_name=metric_name,
            metric_pattern=metric_pattern,
            metric_direction=metric_direction,
            max_iterations=max_iterations,
            time_budget_s=time_budget_s,
            experiment_id=experiment_id,
            hive_parallel=hive_parallel,
        )
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_experiment_research_status(session_id: str = '') -> str:
    """Get autoresearch loop progress for a thought experiment.

    Args:
        session_id: The autoresearch session ID (returned by launch_experiment_autoresearch)

    Returns:
        JSON with iteration count, best metric, improvements, budget consumed
    """
    try:
        from integrations.coding_agent.autoevolve_code_tools import get_autoresearch_status
        return get_autoresearch_status(session_id)
    except Exception as e:
        return json.dumps({'error': str(e)})


# ─── Tool Registration ───

THOUGHT_EXPERIMENT_TOOLS = [
    {
        'name': 'create_thought_experiment',
        'func': create_thought_experiment,
        'description': 'Create a new constitutional thought experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'cast_experiment_vote',
        'func': cast_experiment_vote,
        'description': 'Cast a vote on a thought experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'evaluate_thought_experiment',
        'func': evaluate_thought_experiment,
        'description': 'Record an agent evaluation for a thought experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'get_experiment_status',
        'func': get_experiment_status,
        'description': 'Get experiment detail or list experiments by status',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'tally_experiment_votes',
        'func': tally_experiment_votes,
        'description': 'Get the current vote tally for an experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'advance_experiment',
        'func': advance_experiment,
        'description': 'Advance experiment to next lifecycle phase',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'iterate_hypothesis',
        'func': iterate_hypothesis,
        'description': (
            'Propose and track a hypothesis iteration for any thought experiment. '
            'Use in a loop: propose → evidence → score → refine → repeat.'
        ),
        'tags': ['thought_experiment', 'iteration'],
    },
    {
        'name': 'score_hypothesis_result',
        'func': score_hypothesis_result,
        'description': (
            'Score a hypothesis iteration with structured rubric (evidence, '
            'clarity, feasibility, impact). Returns trend analysis and '
            'continuation advice.'
        ),
        'tags': ['thought_experiment', 'iteration'],
    },
    {
        'name': 'get_iteration_history',
        'func': get_iteration_history,
        'description': (
            'Get past hypothesis iterations with scores and trends. '
            'Use to inform the next hypothesis refinement.'
        ),
        'tags': ['thought_experiment', 'iteration'],
    },
    {
        'name': 'launch_experiment_autoresearch',
        'func': launch_experiment_autoresearch,
        'description': (
            'Launch an autoresearch loop for a SOFTWARE thought experiment: '
            'edit code, run experiments, score, keep best, iterate at hive scale. '
            'For non-code experiments, use iterate_hypothesis instead.'
        ),
        'tags': ['thought_experiment', 'autoresearch'],
    },
    {
        'name': 'get_experiment_research_status',
        'func': get_experiment_research_status,
        'description': 'Get autoresearch loop progress for a thought experiment',
        'tags': ['thought_experiment', 'autoresearch'],
    },
]
