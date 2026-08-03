"""Every workflow declares how it shares the runner pool (task #38).

WHY THIS EXISTS
───────────────
CI was starving its own gates. A single push fanned out to eight workflows, and
the ones with no `concurrency:` block kept running after they had been
superseded, so runners went to answers nobody wanted while the long release gate
sat queued behind them. The observed end state on 77a7c2ac was the Flake
Evaluation Gate dying mid-evaluation with "The runner has received a shutdown
signal" — not an eval error, a reclaimed runner — while flake-checks had been
queued for hours and six VM tests had never executed at all.

That is a verification outage, not a CI style nit: while it lasts, NOTHING can
be proven in a VM.

WHAT IS PINNED
──────────────
1. Every workflow declares a concurrency group. Silence means "run unbounded",
   which is how the pool got exhausted.
2. Group names are UNIQUE per workflow. Two workflows sharing a group cancel
   each other — a booby trap that looks like a flaky gate.
3. Deploy/sign workflows never cancel in progress. Superseding a gate is
   correct; killing a half-finished signature or deploy is not.
4. Every group is keyed by ref, so branches do not cancel each other.

These parse the real YAML and assert on the parsed structure — not on whether a
string survived the commit.
"""
import glob
import os
import re
import unittest

import yaml

# tests/unit/<this file> -> repo root is THREE levels up.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW_DIR = os.path.join(REPO, '.github', 'workflows')

# Workflows that change the outside world. Cancelling one mid-flight can leave a
# half-published release or a half-applied deploy, so they QUEUE instead.
MUST_NOT_CANCEL = {'release-sign', 'docker-deploy', 'remote-deploy'}

# ── Deliberate exemptions ────────────────────────────────────────────────────
# Both rules below are right for the general case and WRONG for a workflow whose
# target is a single shared resource. Recorded here with the reason so the next
# person reads the argument instead of "fixing" a correct workflow into a race.

# A group with no ref key serializes ALL refs onto one queue. That is a bug for
# a gate (branches cancelling each other) and the POINT for a single target.
GLOBAL_GROUP_IS_DELIBERATE = {
    'deploy-hartos-deepbox':
        "there is exactly ONE deepbox. Keying by ref would let a branch push "
        "and a main push build on the same physical box at once, which is the "
        "OOM/SIGKILL-137 the workflow header documents.",
    'nunba-hash-pin':
        "it PUSHES a commit re-pinning the hash; two concurrent runs would "
        "race to write the same file, so all refs must serialize.",
}

# Cancelling a deploy is safe only when nothing is torn down until the
# replacement is built and healthy.
CANCELLABLE_DEPLOY_IS_DELIBERATE = {
    'deploy-hartos-deepbox':
        "build-then-swap ordering: cancelling mid-build leaves the previous "
        "release still serving :6777. Documented in the workflow header; it "
        "was NOT safe under the old remove-first ordering.",
    'release':
        "nothing is published until the sign+publish step, and the group "
        "already carries a run_id escape hatch so a manual dispatch is never "
        "cancelled by someone else's push.",
}


def _workflows():
    out = {}
    for path in sorted(glob.glob(os.path.join(WORKFLOW_DIR, '*.yml'))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding='utf-8') as fh:
            out[name] = yaml.safe_load(fh)
    return out


class EveryWorkflowDeclaresItsShareOfThePool(unittest.TestCase):

    def setUp(self):
        self.wf = _workflows()
        self.assertTrue(self.wf, "no workflows found — wrong path?")

    def test_every_workflow_has_a_concurrency_group(self):
        missing = [n for n, d in self.wf.items()
                   if not (d or {}).get('concurrency')]
        self.assertEqual(
            [], missing,
            "these workflows run unbounded and starve the gates that matter; "
            "give each a concurrency group (see #38): " + ", ".join(missing))

    def test_no_two_workflows_share_a_concurrency_group(self):
        """A shared group makes unrelated workflows cancel each other."""
        seen = {}
        clashes = []
        for name, data in self.wf.items():
            conc = (data or {}).get('concurrency')
            group = conc.get('group') if isinstance(conc, dict) else conc
            if not group:
                continue
            # Compare the STATIC prefix: the ${{ }} parts evaluate per-run, so
            # two workflows whose literal prefix matches will collide at runtime.
            prefix = re.split(r'\$\{\{', str(group))[0].strip(' -')
            if prefix in seen:
                clashes.append(f"{name} and {seen[prefix]} both use '{prefix}'")
            seen[prefix] = name
        self.assertEqual([], clashes,
                         "workflows sharing a group cancel each other: "
                         + "; ".join(clashes))

    def test_deploy_and_signing_workflows_never_cancel_in_flight(self):
        offenders = []
        for name, data in self.wf.items():
            if name not in MUST_NOT_CANCEL:
                continue
            conc = (data or {}).get('concurrency') or {}
            if isinstance(conc, dict) and conc.get('cancel-in-progress') is True:
                offenders.append(name)
        self.assertEqual(
            [], offenders,
            "these publish or deploy; cancelling mid-flight can leave a "
            "half-applied change. They must QUEUE (cancel-in-progress: false): "
            + ", ".join(offenders))

    def test_a_cancellable_deploy_has_a_recorded_reason(self):
        """Cancelling a deploy is allowed, but never by accident."""
        for name in CANCELLABLE_DEPLOY_IS_DELIBERATE:
            self.assertIn(name, self.wf,
                          f"exemption names '{name}', which no longer exists — "
                          "drop the stale exemption")
            conc = (self.wf[name] or {}).get('concurrency') or {}
            self.assertTrue(
                conc.get('cancel-in-progress'),
                f"{name} no longer cancels in progress, so its exemption is "
                "dead weight — remove it and let the default rule apply")

    def test_every_group_is_keyed_by_ref_so_branches_do_not_fight(self):
        unkeyed = []
        for name, data in self.wf.items():
            if name in GLOBAL_GROUP_IS_DELIBERATE:
                continue
            conc = (data or {}).get('concurrency')
            group = conc.get('group') if isinstance(conc, dict) else conc
            if group and 'github.ref' not in str(group):
                unkeyed.append(name)
        self.assertEqual(
            [], unkeyed,
            "a group not keyed by ref makes a push to one branch cancel "
            "another branch's run. If that is deliberate (a single shared "
            "target), add it to GLOBAL_GROUP_IS_DELIBERATE with the reason: "
            + ", ".join(unkeyed))

    def test_the_global_group_exemptions_are_still_global(self):
        """A stale exemption silently un-guards a workflow."""
        for name, why in GLOBAL_GROUP_IS_DELIBERATE.items():
            self.assertIn(name, self.wf,
                          f"exemption names '{name}', which no longer exists")
            conc = (self.wf[name] or {}).get('concurrency') or {}
            group = str(conc.get('group', ''))
            self.assertNotIn(
                'github.ref', group,
                f"{name} is now keyed by ref, so its exemption is stale — "
                f"remove it. (It was exempt because: {why})")


if __name__ == '__main__':
    unittest.main()
