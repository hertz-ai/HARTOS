"""The OTA shell dispatch and the orchestrator must agree on the stage set.

WHAT WENT WRONG
  upgrade_orchestrator.py defines 11 stages. advance_pipeline() has handlers for
  only the 7 ACTIVE ones; idle/completed/rolled_back/failed are terminal and have
  none. start_upgrade() knows this and accepts all four terminal states as
  startable.

  The shell dispatch in hart-ota.nix had drifted. It routed `idle` to the update
  check, `completed` to the apply, and EVERYTHING ELSE to a catch-all that called
  advance. So a node whose pipeline ended in `failed` did this on every tick:

      [HART OTA] Pipeline stage: failed
      [HART OTA] Pipeline in progress (failed), advancing...
      [HART OTA] Advanced: {'success': False, 'error': 'No handler for stage: failed'}

  and exited 0. One failed upgrade wedged the node permanently: it could never
  accept another update, and it reported success the whole time. Found on the
  real box 2026-08-27, sitting on b229551 with four newer commits waiting.

  The cruel part is that start_upgrade would have accepted that state happily.
  The state simply could never reach it.

THE INVARIANT
  Every stage the orchestrator can be in must be routed somewhere by the shell,
  and a stage with no advance handler must NEVER be routed to advance. Both
  directions matter: an unrouted stage wedges the node, and a wrongly-advanced
  one wedges it while claiming success.

Run:
  pytest tests/unit/test_ota_stage_dispatch_agrees.py -v --noconftest
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORCH = os.path.join(REPO, "integrations", "agent_engine", "upgrade_orchestrator.py")
OTA_NIX = os.path.join(REPO, "nixos", "modules", "hart-ota.nix")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def all_stages():
    """Every value in the UpgradeStage enum."""
    src = _read(ORCH)
    body = src[src.index("class UpgradeStage"):]
    body = body[: body.index("\n\n\n")]
    return set(re.findall(r"^\s+[A-Z_]+\s*=\s*'([a-z_]+)'", body, re.M))


def advanceable_stages():
    """The stages advance_pipeline actually has handlers for."""
    src = _read(ORCH)
    body = src[src.index("handlers = {"):]
    body = body[: body.index("}")]
    return set(re.findall(r"UpgradeStage\.([A-Z_]+)\.value:", body))


def dispatch_branches():
    """The stage literals the shell dispatch tests for, per branch."""
    src = _read(OTA_NIX)
    i = src.index("[HART OTA] Pipeline stage:")
    # To the end of this unit's script block, not a fixed byte window: the
    # advance branch is the LAST arm of the chain, and a window that stopped
    # short of it made the dispatch look as though it never mentioned the seven
    # active stages at all.
    end = src.index("\n        '';", i)
    body = src[i:end]
    # Strip shell comments so the prose describing the bug is not parsed as code.
    code = "\n".join(
        l for l in body.splitlines() if not l.lstrip().startswith("#")
    )
    # Join backslash line-continuations so each condition sits on one line.
    code = re.sub(r"\\\n\s*", " ", code)

    markers = (("check", "New version available"),
               ("apply", "Update completed"),
               ("advance", "advancing..."))

    # Split on TOP-LEVEL if/elif heads only. The branches contain nested ifs
    # (`if [[ "$STAGE" != "idle" ]]`, central-endpoint probing, ...), and
    # splitting on those too ends each body at the first nested condition, so a
    # branch's marker text falls outside the body it belongs to. Anchor on the
    # exact indentation of the chain itself.
    m = re.search(r'^([ \t]*)if \[\[ "\$STAGE"', code, re.M)
    assert m, "could not locate the stage dispatch chain in hart-ota.nix"
    indent = m.group(1)
    parts = re.split(
        r"\n%s(?:el)?if (\[\[[^\n]*?\]\]); then\n" % re.escape(indent), code)
    out = {"check": set(), "apply": set(), "advance": set()}
    # parts = [preamble, cond1, body1, cond2, body2, ...]
    for cond, body in zip(parts[1::2], parts[2::2]):
        stages = set(re.findall(r'"\$STAGE" == "([a-z_]+)"', cond))
        if not stages:
            continue
        for kind, marker in markers:
            if marker in body:
                out[kind] |= stages
                break
    return out


def test_every_stage_is_routed_somewhere():
    """An unrouted stage falls to the catch-all, which is what wedged the box."""
    routed = set().union(*dispatch_branches().values())
    missing = all_stages() - routed
    assert not missing, (
        "these orchestrator stages are not named by any branch of the hart-ota "
        "dispatch, so they hit the catch-all: %s" % sorted(missing))


def test_no_unadvanceable_stage_is_sent_to_advance():
    """THE regression. advance_pipeline has no handler for terminal stages, so
    routing one there is a guaranteed no-op that still exits 0."""
    advance_names = {s.lower() for s in advanceable_stages()}
    sent = dispatch_branches()["advance"]
    wrong = sent - advance_names
    assert not wrong, (
        "the dispatch sends %s to advance_pipeline, which has no handler for "
        "them. Every tick will print 'No handler for stage' and exit 0, and the "
        "node can never update again." % sorted(wrong))


def test_failed_and_rolled_back_start_a_fresh_check():
    """They are terminal and start_upgrade accepts them, so they must reach the
    check branch rather than being treated as in-flight."""
    check = dispatch_branches()["check"]
    for stage in ("failed", "rolled_back", "idle"):
        assert stage in check, (
            "'%s' must route to the update check: start_upgrade already accepts "
            "it as startable, so leaving it out means the node stays stuck in a "
            "state the orchestrator would gladly move on from" % stage)


def test_start_upgrade_still_accepts_the_terminal_states():
    """Guard the other half of the contract. If start_upgrade stopped accepting
    FAILED, routing failed->check would just move the dead end one step later."""
    src = _read(ORCH)
    body = src[src.index("def start_upgrade"):]
    body = body[: body.index("def ", 10)]
    for name in ("IDLE", "COMPLETED", "ROLLED_BACK", "FAILED"):
        assert "UpgradeStage.%s.value" % name in body, (
            "start_upgrade no longer treats %s as startable; the dispatch routes "
            "it to a fresh check and would now be rejected there" % name)


class TestNoMeasurementIsNotAZeroPassRate:
    """The test gate must not score an unmeasured run as a total failure.

    _stage_test read its metrics with `.get('value', 0)`, so an adapter that
    returned no metrics produced pass_rate=0.0, tripped the `< 0.95` gate, and
    failed the pipeline with:

        pass_rate=0.00%, fail=0

    Zero percent passing alongside zero failures is not a quality result, it is
    the shape of a run that never happened. That exact string is what the real
    box recorded on 2026-08-24, and combined with the dispatch bug above it left
    the node unable to update for three days.

    It was inconsistent too: a MISSING adapter returns True and skips, while an
    adapter returning nothing failed hard. Both are the same condition, no
    evidence, and must not read as opposite verdicts.

    The gate still FAILS on no measurement, because passing an unverified build
    is the silent-success lie this codebase keeps deleting. What changes is that
    it says so, instead of inventing a 0% score.
    """

    def _stage_test_src(self):
        src = _read(ORCH)
        i = src.index("def _stage_test")
        return src[i: src.index("def _stage_audit")]

    def test_metrics_are_not_defaulted_to_zero(self):
        body = self._stage_test_src()
        assert ".get('value', 0)" not in body, (
            "_stage_test defaults a missing metric to 0, which makes an "
            "unmeasured run indistinguishable from a 0% pass rate")

    def test_absent_pass_rate_is_detected_explicitly(self):
        body = self._stage_test_src()
        assert "pass_rate is None" in body, (
            "_stage_test must distinguish an absent pass_rate from a real 0.0")

    def test_the_failure_message_names_the_real_cause(self):
        """An operator reading the journal must see an infrastructure problem,
        not a code-quality verdict."""
        body = self._stage_test_src()
        assert "NO pass_rate metric" in body, (
            "the no-measurement failure must say that nothing was measured; "
            "'pass_rate=0.00%, fail=0' reads as though every test failed")

    def test_a_real_zero_pass_rate_still_fails(self):
        """Guard the other direction: this must not become a way to pass a build
        where the tests genuinely all failed."""
        body = self._stage_test_src()
        assert "pass_rate < 0.95" in body, (
            "the quality gate itself must survive; only the UNMEASURED case is "
            "reclassified, never a measured failure")


class TestRegressionAdapterReportsWhyItFoundNothing:
    """Zero tests collected must not be reported as a 0% pass rate.

    RegressionAdapter shells out to `venv310/bin/python -m pytest tests/`. A dev
    checkout has that interpreter. A NixOS node does not: hart-app runs from the
    store and there is no venv anywhere. So on the real box the subprocess raised
    FileNotFoundError, the adapter returned {'metrics': {}, 'error': ...}, and
    the pipeline scored the absence as 0%.

    There were TWO routes to the same bad verdict:
      1. the interpreter is missing      -> exception -> empty metrics
      2. pytest runs but collects nothing -> total == 0 -> passed/max(1,total)
         == 0.0, a REAL-looking metric meaning "nothing passed"

    Route 2 is the nastier one, because it produces a number rather than an
    absence, so a caller checking `is None` would never catch it.

    And the adapter's 'error' was never read by _stage_test, so the diagnosis was
    discarded every time: the box logged three days of "pass_rate=0.00%, fail=0"
    while the adapter had been saying, on each run, exactly what was wrong.
    """

    def _adapter_src(self, code_only=False):
        p = os.path.join(REPO, "integrations", "agent_engine",
                         "benchmark_registry.py")
        src = _read(p)
        i = src.index("class RegressionAdapter")
        body = src[i: src.index("class GuardrailAdapter")]
        if code_only:
            # Drop comments: the comment explaining this very bug necessarily
            # quotes the expression the guard asserts is gone.
            body = "\n".join(
                l for l in body.splitlines() if not l.lstrip().startswith("#"))
        return body

    def test_zero_tests_does_not_become_a_zero_pass_rate(self):
        body = self._adapter_src(code_only=True)
        assert "max(1, total)" not in body, (
            "passed/max(1, total) turns zero collected tests into a 0.0 pass "
            "rate, which reads as a catastrophic build instead of a missing "
            "measurement")
        assert "if total == 0:" in body, (
            "the adapter must detect that no tests ran and report no metric")

    def test_the_adapter_falls_back_to_a_real_interpreter(self):
        body = self._adapter_src()
        assert "sys.executable" in body, (
            "with no venv310 on a NixOS node the adapter must fall back to the "
            "interpreter already running it, or it can never run the suite")

    def test_the_failure_carries_evidence(self):
        body = self._adapter_src()
        assert "'error'" in body and "output_tail" in body, (
            "the adapter must return why it failed and what pytest last said")

    def test_stage_test_actually_reads_that_error(self):
        """The half that made this invisible for three days."""
        src = _read(ORCH)
        body = src[src.index("def _stage_test"): src.index("def _stage_audit")]
        assert "result.get('error')" in body, (
            "_stage_test must surface the adapter's own error; discarding it is "
            "why the box reported a fake 0% instead of 'no interpreter found'")
