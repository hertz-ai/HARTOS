"""goal_owner_user_id must not return a machine author as a user identity.

Measured on the live node 2026-08-16 (105 active goals):

    owner_id    populated 0/105
    user_id     populated 0/105
    created_by  populated 90/105 ->  error_advice      x52
                                     system_bootstrap  x32
                                     <a real uuid>     x6
                              None                     x15

Because owner_id and user_id are never set, `created_by` decides EVERY
case -- but it holds a PROVENANCE label, not an identity.  `error_advice`
and `system_bootstrap` are process names; /api/social/users/system_bootstrap
returns 404.

Consequences before this guard, at all three call sites:

  1. core.event_attribution.owner_user_id -> the P3a SSE guard sees a
     truthy user_id, PASSES, and broadcasts to a user that cannot exist.
     Delivered to nobody, and nothing logged.  99/105 goals failed this
     way -- SILENTLY -- while only the 15 with created_by=None produced
     the visible "SSE broadcast refused" warning.  The loud case was the
     small one.
  2. liquid_ui_service._home_resolve_owner_earnings -> looks up a Spark
     wallet for 'system_bootstrap', breaking its own docstring promise of
     "no shadow ledger, no invented figure ... honest-empty".
  3. dashboard_service steering bridge -> attributes to a phantom owner.

None is the designed, handled outcome at all three sites, so returning it
is strictly safer than returning a label.  This does NOT change who
receives anything: it converts a silent misroute into the existing,
already-correct refusal path.
"""
import ast
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from core.constants import MACHINE_GOAL_AUTHORS          # noqa: E402
from core.event_attribution import goal_owner_user_id    # noqa: E402

_REAL_UID = 'b5a304bf-7ed5-47ba-ae86-23367dbf04e7'   # seen live, 6 goals


def _goal(**kw):
    kw.setdefault('owner_id', None)
    kw.setdefault('created_by', None)
    kw.setdefault('user_id', None)
    return SimpleNamespace(**kw)


class TestMachineAuthorsRejected(unittest.TestCase):
    """A process name is not a user id."""

    def test_every_known_machine_author_resolves_to_none(self):
        for author in MACHINE_GOAL_AUTHORS:
            with self.subTest(author=author):
                self.assertIsNone(
                    goal_owner_user_id(_goal(created_by=author)),
                    f"created_by={author!r} is a process name; returning it "
                    f"routes events at a user that does not exist")

    def test_the_two_authors_seen_live_are_covered(self):
        # Guards the set against being trimmed below what production emits.
        for author in ('system_bootstrap', 'error_advice'):
            self.assertIn(author, MACHINE_GOAL_AUTHORS)


class TestRealOwnersStillResolve(unittest.TestCase):
    """The guard must not cost us the cases that already worked."""

    def test_real_uuid_in_created_by_is_returned(self):
        self.assertEqual(
            goal_owner_user_id(_goal(created_by=_REAL_UID)), _REAL_UID)

    def test_owner_id_still_wins(self):
        self.assertEqual(
            goal_owner_user_id(
                _goal(owner_id='owner9', created_by='system_bootstrap')),
            'owner9')

    def test_user_id_used_when_created_by_is_a_machine_author(self):
        # created_by is skipped, so precedence must fall THROUGH to user_id
        # rather than stopping at the rejected label.
        self.assertEqual(
            goal_owner_user_id(
                _goal(created_by='system_bootstrap', user_id='u7')), 'u7')

    def test_short_human_ids_are_not_collateral_damage(self):
        # Existing tests use ids like 'owner9'/'o'.  The guard is a
        # name-based denylist, NOT a uuid-shape heuristic, precisely so
        # these keep working.
        for uid in ('owner9', 'o', '42'):
            self.assertEqual(goal_owner_user_id(_goal(created_by=uid)), uid)

    def test_none_and_empty_still_none(self):
        self.assertIsNone(goal_owner_user_id(_goal()))
        self.assertIsNone(goal_owner_user_id(None))


class TestNoNewProducerDrifts(unittest.TestCase):
    """Mechanical drift guard.

    MACHINE_GOAL_AUTHORS was derived by reading the producers, not
    invented.  If someone adds a new `created_by='some_daemon'` literal
    without registering it, this fails -- otherwise the new label would
    silently become a phantom user id again.
    """

    def test_every_literal_created_by_in_source_is_registered(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..'))
        skip = {'tests', '.venv', 'node_modules', '__pycache__',
                'python-embed', 'agent-ledger-opensource'}
        # Scoped to the two packages that construct AgentGoal rows -- every
        # producer in the table above lives under one of them.  Walking the
        # whole tree parses thousands of files and times the suite out.
        scan_roots = [os.path.join(root, p) for p in ('core', 'integrations')]
        offenders = []
        for scan_root in scan_roots:
            for dirpath, dirnames, filenames in os.walk(scan_root):
                dirnames[:] = [d for d in dirnames if d not in skip
                               and not d.startswith('.')]
                self._scan_dir(dirpath, filenames, root, offenders)
        self.assertEqual(
            offenders, [],
            "New machine author(s) not registered in "
            "core.constants.MACHINE_GOAL_AUTHORS -- unregistered labels are "
            "returned as user ids and silently misroute events:\n  "
            + "\n  ".join(offenders))

    @staticmethod
    def _scan_dir(dirpath, filenames, root, offenders):
            for fn in filenames:
                if not fn.endswith('.py') or fn.startswith('test_'):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    tree = ast.parse(open(full, encoding='utf-8').read())
                except (SyntaxError, UnicodeDecodeError, OSError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    for kw in node.keywords:
                        if kw.arg != 'created_by':
                            continue
                        if not isinstance(kw.value, ast.Constant):
                            continue
                        val = kw.value.value
                        # Only bare snake_case labels are identity-shaped
                        # false positives; a uuid/number is a real id.
                        if (isinstance(val, str) and val
                                and val.replace('_', '').isalpha()
                                and val not in MACHINE_GOAL_AUTHORS):
                            offenders.append(
                                f"{os.path.relpath(full, root)}:"
                                f"{kw.value.lineno} created_by={val!r}")


if __name__ == '__main__':
    unittest.main()
