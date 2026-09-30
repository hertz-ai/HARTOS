"""models.after_commit runs an effect once, on the OUTERMOST commit, never on
a savepoint, and never for work that rolled back.

Measured at 79928c2fc on SQLAlchemy 2.0.16 (review F5,
scratchpad/rv/probe_ac.py), because SQLAlchemy dispatches after_commit when a
SAVEPOINT is released too (orm/session.py SessionTransaction.commit:
``if self._parent is None or self.nested``):

  - queued, then a savepoint released: the effect ran at once, and it still
    had run after the outer transaction rolled back;
  - a savepoint released, then the outer commit: the effect ran TWICE;
  - an effect that queued another effect while the queue ran: the second
    one was dropped without a word.

Plus the case the promise "never if it rolls back" also covers: an effect
queued inside a savepoint that then rolls back belongs to rows that are gone.

Every case runs on a real file-backed SQLite database.

    python -m pytest tests/unit/test_after_commit_runs_on_the_outer_commit_only.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def factory(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'ac.db'}")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE t (i INTEGER)'))
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _write(db, i):
    from sqlalchemy import text
    db.execute(text('INSERT INTO t VALUES (:i)'), {'i': i})


def _rows(factory):
    from sqlalchemy import text
    s = factory()
    try:
        return sorted(r[0] for r in s.execute(text('SELECT i FROM t')))
    finally:
        s.close()


def test_a_savepoint_release_runs_nothing_and_an_outer_rollback_drops_it(factory):
    from integrations.social.models import after_commit

    ran = []
    db = factory()
    try:
        _write(db, 1)
        sp = db.begin_nested()
        after_commit(db, lambda: ran.append('A'))
        _write(db, 2)
        sp.commit()
        assert ran == [], 'ran when a savepoint was released'
        db.rollback()
        db.commit()
    finally:
        db.close()

    assert ran == []
    assert _rows(factory) == []


def test_a_savepoint_release_then_the_outer_commit_runs_it_once(factory):
    from integrations.social.models import after_commit

    ran = []
    db = factory()
    try:
        _write(db, 3)
        sp = db.begin_nested()
        after_commit(db, lambda: ran.append('B'))
        sp.commit()
        db.commit()
        db.commit()
    finally:
        db.close()

    assert ran == ['B']
    assert _rows(factory) == [3]


def test_an_effect_queued_while_the_queue_runs_also_runs_once(factory):
    from integrations.social.models import after_commit

    ran = []
    db = factory()

    def first():
        ran.append('e1')
        after_commit(db, lambda: ran.append('e2'))

    try:
        after_commit(db, first)
        _write(db, 4)
        db.commit()
        assert ran == ['e1', 'e2'], 'the effect queued during the run was lost'
        _write(db, 5)
        db.commit()
    finally:
        db.close()

    assert ran == ['e1', 'e2']


def test_an_effect_queued_in_a_rolled_back_savepoint_never_runs(factory):
    from integrations.social.models import after_commit

    ran = []
    db = factory()
    try:
        _write(db, 6)
        after_commit(db, lambda: ran.append('kept'))
        sp = db.begin_nested()
        _write(db, 7)
        after_commit(db, lambda: ran.append('gone'))
        sp.rollback()
        db.commit()
    finally:
        db.close()

    assert _rows(factory) == [6]
    assert ran == ['kept']


def test_a_released_inner_savepoint_dies_with_the_savepoint_around_it(factory):
    from integrations.social.models import after_commit

    ran = []
    db = factory()
    try:
        _write(db, 8)
        outer = db.begin_nested()
        inner = db.begin_nested()
        _write(db, 9)
        after_commit(db, lambda: ran.append('inner'))
        inner.commit()
        outer.rollback()
        after_commit(db, lambda: ran.append('after'))
        db.commit()
    finally:
        db.close()

    assert _rows(factory) == [8]
    assert ran == ['after']


def test_a_released_savepoint_keeps_its_effect_for_the_outer_commit(factory):
    """Control for the two cases above: a savepoint that is released (not
    rolled back) keeps what it queued, and it runs once the outer commits."""
    from integrations.social.models import after_commit

    ran = []
    db = factory()
    try:
        with db.begin_nested():
            _write(db, 10)
            after_commit(db, lambda: ran.append('released'))
        with db.begin_nested():
            _write(db, 11)
        db.commit()
    finally:
        db.close()

    assert _rows(factory) == [10, 11]
    assert ran == ['released']
