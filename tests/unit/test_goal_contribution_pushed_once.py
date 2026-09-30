"""A goal-contribution notification reaches the person's devices ONCE.

NotificationService.create pushes the notification itself once its row
commits (models.after_commit, 79928c2fc).  _notify_goal_contribution then
called realtime.on_notification again with the same row, so every device got
the card twice (review F11, measured with this file at HEAD: 2 pushes).

Real SQLite file, the real coordinator, NotificationService and
after_commit; only the realtime push is recorded.

    python -m pytest tests/unit/test_goal_contribution_pushed_once.py -q
"""
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from tests.unit.test_coordinator_dedup import _coordinator, _ledger  # noqa: E402


@pytest.fixture
def Session(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import Base, User
    eng = create_engine(f"sqlite:///{tmp_path / 'goal.db'}")
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    s = factory()
    s.add(User(id='human-1', username='u_human-1', display_name='h',
               user_type='human'))
    s.commit()
    s.close()
    yield factory
    eng.dispose()


def test_one_contribution_is_one_push(Session):
    from integrations.social.models import Notification

    led = _ledger()
    coord = _coordinator(led)
    coord.submit_goal('objective', [{'task_id': 'g1_task_0',
                                      'description': 'd'}],
                      {'user_id': 'human-1'}, goal_id='g1')
    pushes = []
    with patch('integrations.social.models.get_db', side_effect=Session), \
         patch('integrations.social.realtime.on_notification',
               side_effect=lambda uid, d: pushes.append((uid, d['id']))):
        coord._notify_goal_contribution('g1_task_0', agent_id='node-abc',
                                        task_description='d')

    s = Session()
    rows = [n.id for n in s.query(Notification).filter_by(
        user_id='human-1', type='goal_contribution')]
    s.close()
    assert len(rows) == 1
    assert pushes == [('human-1', rows[0])], (
        f'expected one push of the committed row, got {pushes}')
