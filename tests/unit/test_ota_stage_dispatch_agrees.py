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
