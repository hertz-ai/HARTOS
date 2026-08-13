"""
Proof that the auto-evolve loop is no longer starved, run against the REAL
experiment database.

Works on a COPY of agent_data/hevolve_database.db — 142 rows, every one at
`proposed`, 132 overdue to open voting, zero votes ever cast. The real
SQLAlchemy models, the real service, the real auto_evolve gather.

It proves the two ends that both refused `proposed`:

  before: _gather_candidates() sees 0        -> nothing to ever dispatch
          cast_vote() -> experiment_not_in_voting_phase
  after:  _gather_candidates() sees N > 0
          cast_vote() records a real vote

Run:  python tests/standalone/experiment_lifecycle_proof.py
Exit 0 = proven.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

_REAL_DB = _REPO / 'agent_data' / 'hevolve_database.db'


def _breakdown(path):
    con = sqlite3.connect(path)
    rows = con.execute(
        'SELECT status, COUNT(*) FROM thought_experiments '
        'GROUP BY status ORDER BY 2 DESC').fetchall()
    votes = con.execute('SELECT COUNT(*) FROM experiment_votes').fetchone()[0]
    con.close()
    return dict(rows), votes


def main() -> int:
    if not _REAL_DB.exists():
        print(f'SKIP: no database at {_REAL_DB}')
        return 0

    work = Path(tempfile.mkdtemp(prefix='lifecycle-proof-')) / 'db.sqlite'
    shutil.copy(_REAL_DB, work)
    os.environ['HEVOLVE_DB_URL'] = f'sqlite:///{work.as_posix()}'
    os.environ['DATABASE_URL'] = f'sqlite:///{work.as_posix()}'

    before, votes_before = _breakdown(work)
    print(f'[1] before: {before}  votes={votes_before}')
    if 'proposed' not in before:
        print('    (nothing at proposed — nothing to prove)')

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import ThoughtExperiment, ExperimentVote
    from integrations.social.thought_experiment_service import (
        ThoughtExperimentService)

    engine = create_engine(f'sqlite:///{work.as_posix()}')
    Session = sessionmaker(bind=engine)

    failures = []

    # --- The gather that auto_evolve does, BEFORE ---------------------
    db = Session()
    votable_before = db.query(ThoughtExperiment).filter(
        ThoughtExperiment.status.in_(['voting', 'evaluating'])).count()
    print(f'[2] auto_evolve gather statuses (voting/evaluating) before: '
          f'{votable_before}')

    # --- cast_vote on a proposed experiment, BEFORE -------------------
    stuck = db.query(ThoughtExperiment).filter(
        ThoughtExperiment.status == 'proposed').first()
    if stuck is not None:
        result = ThoughtExperimentService.cast_vote(
            db, stuck.id, 'proof-voter', 1, voter_type='agent')
        print(f'[3] cast_vote while proposed -> {result}')
        if not (result or {}).get('error') == 'experiment_not_in_voting_phase':
            failures.append('a proposed experiment unexpectedly accepted a '
                            'vote — the premise of this proof is wrong')
    db.rollback()
    db.close()

    # --- Run the driver, repeatedly, as the daemon would --------------
    total = {'discussing': 0, 'voting': 0, 'evaluating': 0}
    for _ in range(40):
        db = Session()
        moved = ThoughtExperimentService.advance_due_experiments(db, limit=25)
        db.commit()
        db.close()
        for k, v in moved.items():
            total[k] += v
        if not any(moved.values()):
            break
    print(f'[4] driver moved: {total}')
    if total['voting'] == 0:
        failures.append('nothing reached voting — 132 rows were overdue')

    after, _ = _breakdown(work)
    print(f'[5] after: {after}')

    # --- The gather auto_evolve does, AFTER ---------------------------
    db = Session()
    votable_after = db.query(ThoughtExperiment).filter(
        ThoughtExperiment.status.in_(['voting', 'evaluating'])).count()
    print(f'[6] auto_evolve gather statuses (voting/evaluating) after: '
          f'{votable_after}')
    if votable_after <= votable_before:
        failures.append('auto_evolve still has no candidates to gather')

    # --- cast_vote now that the window is open ------------------------
    open_exp = db.query(ThoughtExperiment).filter(
        ThoughtExperiment.status.in_(['discussing', 'voting'])).first()
    if open_exp is None:
        failures.append('no experiment is in a votable phase')
    else:
        result = ThoughtExperimentService.cast_vote(
            db, open_exp.id, 'proof-voter', 2,
            reasoning='lifecycle proof', voter_type='agent', confidence=0.9)
        db.commit()
        ok = bool(result) and not (result or {}).get('error')
        print(f'[7] cast_vote after advance -> '
              f'{"recorded" if ok else result}')
        if not ok:
            failures.append(f'vote still refused: {result}')
        else:
            n = db.query(ExperimentVote).count()
            print(f'[8] experiment_votes rows now: {n}')
            if n < 1:
                failures.append('vote reported success but nothing persisted')

    # --- decided must NOT have been manufactured ----------------------
    decided = after.get('decided', 0)
    print(f'[9] decided rows: {decided} (must stay 0 — a timer must not '
          f'manufacture an outcome)')
    if decided:
        failures.append('the driver auto-decided experiments')

    db.close()

    print()
    if failures:
        print('NOT PROVEN:')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('PROVEN: the lifecycle advances on its own stored schedule, '
          'auto_evolve now has candidates, and votes are castable.')
    print(f'(worked on a copy — {_REAL_DB.name} untouched)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
