"""ONE approval rule for a thought experiment: the owner's quorum, the
decision context's threshold, and its steward requirement, together.

49f777442 named voting_rules.approval_verdict "the ONE approval rule", but it
read only the quorum and the 2/3 super-majority, while tally_votes'
decision_recommendation applied the context's approval_threshold (0.8 for
security_guardrail) and decide() the steward requirement.  Two rules, and
the one that starts agents was the weaker: 2 FOR / 1 AGAINST on a
security_guardrail experiment passed approval_verdict and
request_agent_evaluation started an evaluation goal, with no steward and
below the 0.8 the context demands (review F6, measured with this file at
HEAD).

Now approval_verdict is the only rule, and it requires all of:
  - the owner quorum: >= 3 distinct identities, >= 2 FOR, an agent counting
    as its owner (voting_rules.quorum_met, counted by tally_votes);
  - FOR share of the decisive weight >= max(2/3, context approval_threshold):
    a context can make approval harder, never easier;
  - the steward's FOR vote when the context says steward_required.
tally_votes' decision_recommendation is read from it, so the two cannot
disagree.

Real SQLite, the real service, the real REST-independent writer.
Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import ast
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)
from integrations.social.voting_rules import approval_verdict  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]

SECURITY = ('Tighten the security guardrail',
            'A stricter guardrail blocks the vulnerability')
DEFAULT = ('Cache warmup', 'Faster cache warmup lowers latency')


@pytest.fixture
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'votes.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _user(db):
    u = User(username=f'livetest_f6_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(u)
    db.flush()
    return u


def _experiment(db, title_hypothesis):
    title, hypothesis = title_hypothesis
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title=title,
        hypothesis=hypothesis, expected_outcome='o', status='voting')
    db.add(e)
    db.commit()
    return e


def _votes(db, exp_id, values, steward=None):
    for v in values:
        db.add(ExperimentVote(
            experiment_id=exp_id, voter_id=_user(db).id, voter_type='human',
            vote_value=v, confidence=1.0))
    if steward is not None:
        # The steward is a human account holding the central role
        # (voting_rules.is_steward; test_steward_vote_needs_the_steward_role).
        s = User(username=f'livetest_f6s_{uuid.uuid4().hex[:8]}',
                 user_type='human', role='central', is_admin=True)
        db.add(s)
        db.flush()
        db.add(ExperimentVote(
            experiment_id=exp_id, voter_id=s.id,
            voter_type='human', vote_value=steward, confidence=1.0))
    db.commit()


def _evaluate(db, e):
    result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
    db.commit()
    return result, db.query(AgentGoal).count()


def test_the_context_is_what_the_fixtures_say(db):
    """Guards the fixtures: the titles classify as intended."""
    sec = _experiment(db, SECURITY)
    dfl = _experiment(db, DEFAULT)
    assert ThoughtExperimentService.tally_votes(
        db, sec.id)['decision_context'] == 'security_guardrail'
    assert ThoughtExperimentService.tally_votes(
        db, dfl.id)['decision_context'] == 'technical_improvement'


def test_two_for_one_against_on_a_security_experiment_starts_no_goal(db):
    """The review's case exactly: quorum met, 2/3 met, 0.8 not met."""
    e = _experiment(db, SECURITY)
    _votes(db, e.id, [2, 2, -1], steward=2)
    result, goals = _evaluate(db, e)
    assert result['success'] is False and result['reason'] == 'not_approved'
    assert result['verdict']['reason'] == 'below_threshold'
    assert goals == 0


def test_a_security_experiment_needs_the_steward(db):
    e = _experiment(db, SECURITY)
    _votes(db, e.id, [2, 2, 2])
    result, goals = _evaluate(db, e)
    assert result['success'] is False
    assert result['verdict']['reason'] == 'steward_required'
    assert goals == 0


def test_a_steward_against_is_not_a_steward_approval(db):
    e = _experiment(db, SECURITY)
    _votes(db, e.id, [2, 2, 2, 2, 2], steward=-2)
    result, goals = _evaluate(db, e)
    assert result['success'] is False
    assert goals == 0


def test_a_security_experiment_over_its_threshold_with_the_steward_starts(db):
    """Control: 4 FOR / 1 AGAINST among people plus the steward FOR is
    5/6 >= 0.8, quorum met, steward FOR."""
    e = _experiment(db, SECURITY)
    _votes(db, e.id, [2, 2, 2, 2, -1], steward=2)
    result, goals = _evaluate(db, e)
    assert result['success'] is True and result['goal_id']
    assert goals == 1


def test_just_below_a_security_threshold_is_refused_even_with_the_steward(db):
    """3 FOR / 2 AGAINST among people plus the steward FOR: 4/6 = 0.667."""
    e = _experiment(db, SECURITY)
    _votes(db, e.id, [2, 2, 2, -1, -1], steward=2)
    result, goals = _evaluate(db, e)
    assert result['success'] is False
    assert result['verdict']['reason'] == 'below_threshold'
    assert goals == 0


def test_the_default_context_still_approves_at_two_thirds(db):
    """No regression of 49f777442: technical_improvement (threshold 0.5,
    no steward) approves 2 FOR / 1 AGAINST, the owner's floor."""
    e = _experiment(db, DEFAULT)
    _votes(db, e.id, [2, 2, -1])
    result, goals = _evaluate(db, e)
    assert result['success'] is True
    assert goals == 1


TUNING = ('Tune the retry timeout', 'A longer retry timeout cuts polling')


@pytest.mark.parametrize('context, values', [
    (DEFAULT, [2, 2, 2, -1, -1]),   # 3/5 = 0.6: over 0.5, under 2/3
    (TUNING, [2, 2, -1, -1]),       # 2/4 = 0.5: over 0.3, under 2/3
])
def test_a_low_context_threshold_never_lowers_the_two_thirds_floor(
        db, context, values):
    """A context may raise the bar (security 0.8), never lower it below the
    owner's 2/3: technical_improvement says 0.5 and operational_tuning 0.3."""
    e = _experiment(db, context)
    if context is TUNING:
        assert ThoughtExperimentService.tally_votes(
            db, e.id)['decision_context'] == 'operational_tuning'
    _votes(db, e.id, values)
    result, goals = _evaluate(db, e)
    assert result['success'] is False
    assert result['verdict']['reason'] == 'below_threshold'
    assert goals == 0


def test_one_voter_never_approves_whatever_the_context(db):
    e = _experiment(db, DEFAULT)
    _votes(db, e.id, [2], steward=2)
    result, goals = _evaluate(db, e)
    assert result['success'] is False
    assert result['verdict']['reason'] == 'no_quorum'
    assert goals == 0


@pytest.mark.parametrize('context, values, steward', [
    (SECURITY, [2, 2, -1], 2),
    (SECURITY, [2, 2, 2], None),
    (SECURITY, [2, 2, 2, 2, -1], 2),
    (DEFAULT, [2, 2, -1], None),
    (DEFAULT, [2, -2, -2], None),
    (DEFAULT, [2], None),
])
def test_the_tally_recommendation_is_the_verdict(db, context, values, steward):
    """decision_recommendation says 'approve' exactly when the one rule
    approves; it used to apply its own weighted-score threshold."""
    e = _experiment(db, context)
    _votes(db, e.id, values, steward=steward)
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert (tally['decision_recommendation'] == 'approve') == \
        approval_verdict(tally)['approved']


# ── the guard that keeps it at one ──────────────────────────────────────
# The parameters of the approval rule may be READ only in voting_rules.py
# (which holds the rule).  Anywhere else, a reference to one of them, or an
# inline FOR / (FOR + AGAINST) ratio, is a second approval rule.

_RULE_VOCABULARY = frozenset({
    'approval_threshold', 'steward_required', 'SUPERMAJORITY_RATIO',
    'AUTO_EVOLVE_SUPERMAJORITY_RATIO', 'MIN_DISTINCT_VOTERS',
    'MIN_DISTINCT_SUPPORTERS',
})
_RULE_HOME = Path('integrations') / 'social' / 'voting_rules.py'
_SCANNED = ('integrations', 'core', 'hartos', 'security')


def _second_rules(root: Path):
    """(file, line, what) for every approval-rule reference outside the
    rule's home.  Import statements are allowed (a re-export is not a
    rule); names, attributes and string keys are not."""
    found = []
    for top in _SCANNED:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob('*.py'):
            rel = path.relative_to(root)
            if rel == _RULE_HOME or '__pycache__' in rel.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                word = None
                if isinstance(node, ast.Name):
                    word = node.id
                elif isinstance(node, ast.Attribute):
                    word = node.attr
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    word = node.value
                if word in _RULE_VOCABULARY:
                    found.append((str(rel), node.lineno, word))
                elif (isinstance(node, ast.BinOp)
                      and isinstance(node.op, ast.Div)):
                    names = {n.id if isinstance(n, ast.Name) else
                             n.value if isinstance(n, ast.Constant) else None
                             for n in ast.walk(node)}
                    if {'total_for', 'total_against'} <= names:
                        found.append((str(rel), node.lineno, 'for/against ratio'))
    return found


def test_source_guard_there_is_one_approval_rule():
    found = _second_rules(_ROOT)
    assert found == [], (
        'an approval rule outside voting_rules.approval_verdict: '
        f'{found} -- ask approval_verdict(tally) instead')


def test_source_guard_catches_a_second_rule(tmp_path):
    """The guard can fail: a planted threshold check and an inline ratio are
    both found."""
    pkg = tmp_path / 'integrations' / 'social'
    pkg.mkdir(parents=True)
    (pkg / 'voting_rules.py').write_text(
        "approval_threshold = 0.8\n", encoding='utf-8')
    (pkg / 'rogue.py').write_text(
        "def ok(t, rules):\n"
        "    r = t['total_for'] / (t['total_for'] + t['total_against'])\n"
        "    return r > rules['approval_threshold']\n", encoding='utf-8')
    found = _second_rules(tmp_path)
    assert {w for _, _, w in found} == {'approval_threshold',
                                         'for/against ratio'}
    assert all(f.endswith('rogue.py') for f, _, _ in found)
