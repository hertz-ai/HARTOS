#!/usr/bin/env python3
"""Local-2B AGENT/SURFACE BASELINE profiler.

Steward mandate (2026-07-22): "create tests on surfaces which are not tested, on
agents which are not profiled with the llama.cpp 2B running locally, so we have a
baseline in this potato machine."

WHAT IT DOES: enumerates the REAL agent tasks (goal_seeding.SEED_BOOTSTRAP_GOALS
+ the flagship catalog) plus the UI-adjacent surfaces, maps each to a probe +
budget from spectrum.json, and runs a single short turn against the LOCAL
llama-server OpenAI endpoint the agents already use (port_registry 'llm'). It
records first-token latency, total latency, char count, and pass/fail per entry
into a baseline JSON. That is the potato-machine baseline: what the 2B produces,
and how fast, for each KIND of agent work.

WHY THIS IS NOT A PARALLEL PATH (Gate 4 / the concurrent-session rule): it does
NOT reimplement inference and does NOT invent a second agent list. It ROUTES to
the shared llama-server (the same endpoint model_bus_service / the agents use)
and DERIVES the task list from the one canonical seed list. spectrum.json holds
only probe text + budgets.

WHERE IT RUNS: on the NODE, where llama-server is up. On a box with no local
model (the dev box) it prints a clear "no local model -- baseline deferred" and
exits 0, so it is safe in CI and never fabricates numbers.

Usage:
  python scripts/agent_baseline/profile_local_2b.py            # profile + write baseline
  python scripts/agent_baseline/profile_local_2b.py --check    # env only, no calls (exit 0)
  python scripts/agent_baseline/profile_local_2b.py --json     # print the result JSON
"""
import argparse
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPECTRUM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spectrum.json")


def _llm_port():
    """The canonical local-model port -- port_registry 'llm' (env HART_LLM_PORT),
    falling back to the well-known 8080 the harness docs cite."""
    try:
        sys.path.insert(0, REPO)
        from core.port_registry import get_port
        return get_port("llm")
    except Exception:
        return int(os.environ.get("HART_LLM_PORT", "8080"))


def _load_spectrum():
    with open(SPECTRUM, "r", encoding="utf-8") as f:
        return json.load(f)


# ── the ONE canonical task list -> (name, category) tuples ───────────────────
_CAT_KEYWORDS = [
    ("marketing", ("marketing", "awareness")),
    ("referral", ("referral",)),
    ("video", ("video", "demo")),
    ("intelligence", ("intelligence", "crowdsource", "thought")),
    ("monitor", ("monitor", "health", "watcher", "exception")),
    ("analytics", ("analytics", "growth")),
    ("coding", ("coding", "codebase", "recipe")),
    ("audit", ("audit", "embed")),
    ("revenue", ("revenue", "pricing")),
    ("finance", ("finance", "business")),
    ("exception", ("exception",)),
]


def _categorize(slug, title):
    hay = (slug + " " + title).lower()
    for cat, kws in _CAT_KEYWORDS:
        if any(k in hay for k in kws):
            return cat
    return "generic"


def _agent_tasks():
    """Derive the agent spectrum from the SINGLE canonical seed list; degrade to a
    minimal built-in set if the import is unavailable (keeps --check usable)."""
    tasks = []
    try:
        sys.path.insert(0, REPO)
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        for g in SEED_BOOTSTRAP_GOALS:
            slug = g.get("slug", "")
            title = g.get("title", "")
            if slug:
                tasks.append(("goal:" + slug, _categorize(slug, title)))
    except Exception as e:
        print("profile_local_2b: seed list unavailable (%s); using a minimal set"
              % e, file=sys.stderr)
        tasks = [("goal:generic_probe", "generic")]
    return tasks


def _endpoint_up(port, spec):
    ep = spec["endpoint"]
    url = "http://127.0.0.1:%d%s" % (port, ep["path"])
    try:
        req = urllib.request.Request(
            url, data=b'{"messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}',
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=ep.get("connect_timeout_s", 3)) as r:
            return r.status == 200
    except Exception:
        return False


def _run_probe(port, spec, probe, budget):
    """One streamed turn. Returns (first_token_ms, total_ms, chars, error)."""
    ep = spec["endpoint"]
    url = "http://127.0.0.1:%d%s" % (port, ep["path"])
    body = json.dumps({
        "messages": [{"role": "user", "content": probe}],
        "max_tokens": 256, "temperature": 0.2, "stream": True,
    }).encode("utf-8")
    t0 = time.time()
    first_ms = None
    chars = 0
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=ep.get("request_timeout_s", 90)) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content", "")
                except Exception:
                    delta = ""
                if delta:
                    if first_ms is None:
                        first_ms = int((time.time() - t0) * 1000)
                    chars += len(delta)
        total_ms = int((time.time() - t0) * 1000)
        return first_ms if first_ms is not None else total_ms, total_ms, chars, None
    except Exception as e:
        return None, int((time.time() - t0) * 1000), chars, "%s: %s" % (type(e).__name__, e)


def _budget_for(spec, budget_name):
    b = spec["budgets"]
    return b.get(budget_name, b["default"])


def _items(spec):
    """The work list: every agent task + every surface, each with its probe and
    budget class. ONE builder, so the planned duration below and the run itself
    can never disagree about what this baseline actually does."""
    cats = spec["categories"]
    surfaces = spec["surfaces"]
    items = []
    for name, cat in _agent_tasks():
        c = cats.get(cat, cats["generic"])
        items.append((name, cat, c["probe"], c["budget"]))
    for sname, s in surfaces.items():
        if sname.startswith("_"):
            continue
        items.append(("surface:" + sname, "surface", s["probe"], s["budget"]))
    return items


def plan_seconds(spec, items=None):
    """How long this baseline can legitimately need, derived from the SAME
    per-item budgets the PASS/FAIL verdicts are judged against.

    The unit used to allow a flat TimeoutStartSec=300 for this. That number was
    never related to the work: measured on the fleet box 2026-08-26 the run is
    72 agent tasks + 3 surfaces = 75 probes whose own declared budgets are
    25-45s each, i.e. 37-56 minutes. So systemd killed it with SIGTERM after
    roughly seven probes, every time, and because the kill lands mid-run the
    node recorded a FAILED unit and wrote NO baseline at all -- the one artifact
    the whole exercise exists to produce.

    A timeout for this job has to be a function of the number of actions, not a
    constant, which is what this returns.
    """
    if items is None:
        items = _items(spec)
    total_ms = sum(_budget_for(spec, b)["total_ms"] for (_n, _c, _p, b) in items)
    return total_ms / 1000.0


def profile(spec, port, deadline=None):
    results = []
    items = _items(spec)

    for name, cat, probe, budget_name in items:
        # Past our own budget: record the remainder honestly and return what we
        # measured. A partial baseline is data; a SIGTERM is not.
        if deadline is not None and time.monotonic() >= deadline:
            results.append({
                "name": name, "category": cat, "budget": budget_name,
                "first_token_ms": None, "total_ms": None, "chars": 0,
                "error": "skipped: run exceeded its budget-derived deadline",
                "verdict": "SKIPPED",
            })
            continue
        first_ms, total_ms, chars, err = _run_probe(port, spec, probe, budget_name)
        bud = _budget_for(spec, budget_name)
        ok = (err is None and first_ms is not None
              and first_ms <= bud["first_token_ms"]
              and total_ms <= bud["total_ms"]
              and chars >= bud["min_chars"])
        results.append({
            "name": name, "category": cat, "budget": budget_name,
            "first_token_ms": first_ms, "total_ms": total_ms, "chars": chars,
            "error": err, "verdict": "PASS" if ok else "FAIL",
        })
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="env only, no model calls")
    ap.add_argument("--json", action="store_true", help="print the result JSON")
    ap.add_argument("--plan", action="store_true",
                    help="print the budget-derived duration in seconds and exit "
                         "(what a unit timeout should be sized from)")
    args = ap.parse_args(argv)

    spec = _load_spectrum()
    port = _llm_port()
    tasks = _agent_tasks()
    items = _items(spec)
    planned = plan_seconds(spec, items)
    print("profile_local_2b: %d agent tasks + %d surfaces, llm port %d"
          % (len(tasks), len([k for k in spec["surfaces"] if not k.startswith("_")]), port))
    # Print the derived budget BEFORE doing any work, so an operator (and any
    # unit timeout) can see what this run actually needs rather than guessing.
    print("profile_local_2b: %d probes, budget-derived plan %.0fs (%.1f min)"
          % (len(items), planned, planned / 60.0))

    if args.plan:
        print("%d" % int(planned))
        return 0

    if args.check:
        print("profile_local_2b: --check OK (no calls made)")
        return 0

    if not _endpoint_up(port, spec):
        print("profile_local_2b: NO LOCAL MODEL reachable on 127.0.0.1:%d -- "
              "baseline DEFERRED to a node with llama-server up (this is not a "
              "failure; the dev box has no 2B)." % port)
        return 0

    # Self-bound to the plan. The unit's timeout is a backstop; THIS is the
    # deadline that keeps a slow node's partial results instead of losing them.
    results = profile(spec, port, deadline=time.monotonic() + planned)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    skipped = sum(1 for r in results if r["verdict"] == "SKIPPED")
    if skipped:
        print("profile_local_2b: %d/%d probes SKIPPED (ran out of the %.0fs "
              "budget). Partial baseline written." % (skipped, len(results), planned))
    ts = int(time.time())
    out_dir = os.path.join(REPO, "agent_data", "baselines", "local_2b")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "baseline_%d.json" % ts), "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "port": port, "results": results,
                       "passed": passed, "total": len(results)}, f, indent=2)
    except OSError as e:
        print("profile_local_2b: could not write baseline: %s" % e, file=sys.stderr)

    for r in results:
        print("  %-34s %-11s first=%-6s total=%-6s chars=%-4s %s%s" % (
            r["name"], r["category"], r["first_token_ms"], r["total_ms"],
            r["chars"], r["verdict"], (" [" + r["error"] + "]") if r["error"] else ""))
    print("BASELINE: %d/%d passed on port %d" % (passed, len(results), port))
    if args.json:
        print(json.dumps(results, indent=2))
    # Non-zero ONLY when the model was reachable but underperformed the budget, so
    # this can gate a node's baseline without ever failing on a modelless box.
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
