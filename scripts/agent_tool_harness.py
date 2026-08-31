"""Live harness: tool registration, invocation, and missed-call accounting.

    python scripts/agent_tool_harness.py            # report mode (never fails)
    python scripts/agent_tool_harness.py --strict   # post-fix acceptance (misses fail)

Extends scripts/verify_agentic_after_restart.py (same /chat driving, same
byte-offset log windows, media_mode="text" on every turn) with the tool axis:

WHY THIS EXISTS (measured 2026-08-31 on the installed build, llama :8080):
  - llama-server SILENTLY IGNORES the legacy `functions` field: identical
    prompt_tokens (30) with and without it, model replies "I cannot execute
    external tools".  With `tools` the same definition renders (287 tok) and
    the model emits a real tool_call on the first try.
  - autogen 0.3.2 register_for_llm(api_style="function") populates `functions`;
    the default api_style="tool" populates `tools`.  reuse_recipe pins
    api_style="function" at 26 sites -> live reuse bodies carry a split brain:
    functions[]=28 core tools (memory/messaging/vision/search/execute - ALL
    INVISIBLE) alongside tools[]=39 service tools (visible).  Overlap: none.
  - 0 tool/function-call emissions in the last 1,601 wire calls; the only
    function-role messages in histories are framework-injected results.

MISS TAXONOMY the harness counts per turn window:
  structural_invisible   advertised only in functions[] -> model cannot call it
  expected_not_emitted   harness asked for tool X, no emission in the window
  emitted_unresolved     model called a name the executor does not know
  executed_err           tool ran and raised (TOOL EXECUTION ERROR / Error: ...)
  executed_ok            tool ran clean

Probes:
  P0 protocol   :8080 direct - is `functions` rendered? is `tools` rendered?
  P1 visible    /chat reuse turn eliciting a read-only tools[] tool
  P2 invisible  /chat reuse turn eliciting a core functions[] tool
  P3 dynamic    create an agent (real actions), then reuse THAT agent and
                verify its recipe actions actually engage
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_agentic_after_restart as base  # noqa: E402  (post/log windows/preflight)

JSONL = os.path.join(base.LOGS, "llm_outbound.jsonl")
# tool_logger output may land beside the base pair; include it in windows
LOG_FILES = ("gui_app.log", "server.log", "langchain.log")

# Read-only representatives.  NEVER drive payments/posting tools from a probe.
VISIBLE_TOOL = "get_system_health"
INVISIBLE_TOOL = "get_user_id"

_EMIT_RE = re.compile(r'"name"\s*:\s*"([A-Za-z_0-9]+)"')


def _offsets(paths):
    out = {}
    for p in paths:
        try:
            out[p] = os.path.getsize(p)
        except OSError:
            out[p] = 0
    return out


def _since(offsets):
    chunks = []
    for p, off in offsets.items():
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                f.seek(off)
                chunks.append(f.read())
        except OSError:
            pass
    return "\n".join(chunks)


def window_start():
    return {
        "jsonl": _offsets([JSONL]),
        "logs": _offsets([os.path.join(base.LOGS, n) for n in LOG_FILES]),
    }


def analyze_window(w):
    """Everything tool-shaped that happened on the wire + in logs since `w`."""
    res = {
        "calls": 0, "visible": set(), "invisible": set(),
        "emitted": [], "unresolved": 0, "results_ok": 0, "results_err": 0,
        "exec_start": [], "exec_err": [], "log_text": "", "sources": {},
    }
    for line in _since(w["jsonl"]).splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        res["calls"] += 1
        src = r.get("source", "?")
        res["sources"][src] = res["sources"].get(src, 0) + 1
        body = r.get("body") or {}
        res["invisible"] |= {f.get("name") for f in body.get("functions") or []}
        res["visible"] |= {t.get("function", {}).get("name")
                           for t in body.get("tools") or []}
        rt = r.get("response_text") or ""
        if "tool_calls" in rt or "function_call" in rt or "<tool_call>" in rt:
            res["emitted"] += _EMIT_RE.findall(rt)
        for msg in body.get("messages") or []:
            if msg.get("role") in ("function", "tool"):
                c = msg.get("content")
                c = c if isinstance(c, str) else str(c)
                if c.startswith("Error: Function"):
                    res["unresolved"] += 1
                elif c.startswith("Error") or '"error"' in c[:60]:
                    res["results_err"] += 1
                else:
                    res["results_ok"] += 1
    text = _since(w["logs"])
    res["log_text"] = text
    res["exec_start"] = re.findall(r"TOOL EXECUTION START: (\S+)", text)
    res["exec_err"] = re.findall(r"TOOL EXECUTION ERROR: (\S+)", text)
    return res


def probe_protocol():
    """P0: which schema field does the live llama server actually render?"""
    print("\n== P0 protocol: does :8080 render `functions` / `tools`? ==")
    fdef = {"name": "get_user_id", "description": "Return the user's id.",
            "parameters": {"type": "object", "properties": {}, "required": []}}
    msgs = [{"role": "user", "content":
             "Use the get_user_id tool to fetch my user id. You must call the tool."}]

    def llama(body):
        req = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())

    try:
        bare = llama({"messages": msgs, "max_tokens": 60})
        legacy = llama({"messages": msgs, "max_tokens": 60, "functions": [fdef]})
        modern = llama({"messages": msgs, "max_tokens": 60,
                        "tools": [{"type": "function", "function": fdef}]})
    except Exception as e:
        print("  SKIP: llama unreachable ->", e)
        return None
    p0, p1, p2 = (r["usage"]["prompt_tokens"] for r in (bare, legacy, modern))
    functions_rendered = p1 > p0
    tools_rendered = p2 > p0
    emitted = bool(modern["choices"][0]["message"].get("tool_calls"))
    print(f"  prompt_tokens bare={p0} functions={p1} tools={p2}")
    print(f"  functions rendered: {functions_rendered}   tools rendered: "
          f"{tools_rendered}   tool_call emitted via tools: {emitted}")
    if not functions_rendered:
        print("  -> every tool advertised via `functions` is INVISIBLE to the model")
    return {"functions_rendered": functions_rendered,
            "tools_rendered": tools_rendered, "emitted": emitted}


def _first_reusable_pid():
    names = [n[:-len("_0_recipe.json")] for n in os.listdir(base.PROMPTS)
             if re.match(r"^\d+_0_recipe\.json$", n)]
    return int(names[0]) if names else None


def _chat(text, pid, rid, create=False):
    body = {"text": text, "user_id": "toolharness", "media_mode": "text",
            "request_id": rid}
    if create:
        body["create_agent"] = True
    if pid is not None:
        body["prompt_id"] = pid
    return base.post("/chat", body)


def probe_static(tool, prompt, pid, misses):
    print(f"\n== static tool: {tool} ==")
    w = window_start()
    rid = "toolharness-%s-%d" % (tool, int(time.time()))
    try:
        r = _chat(prompt, pid, rid)
    except base.ProbeTimeout as e:
        print("  turn timed out ->", e)
        misses["expected_not_emitted"].append(tool)
        return
    reply = str(r.get("text") or "")
    a = analyze_window(w)
    advertised = ("tools" if tool in a["visible"] else
                  "functions" if tool in a["invisible"] else "ABSENT")
    emitted = tool in a["emitted"]
    # framework-injected function-role results in CONCURRENT background
    # traffic land in the same window, so results_ok cannot prove OUR tool
    # ran - only the TOOL EXECUTION marker with the right name can.
    executed = tool in a["exec_start"]
    print(f"  advertised via: {advertised}   emitted: {emitted}   "
          f"executed: {executed}   reply: {reply[:70]!r}")
    print(f"  window: {a['calls']} llm calls by source {a['sources']}, "
          f"emissions={a['emitted']}, exec_start={a['exec_start']}, "
          f"exec_err={a['exec_err']}")
    if advertised == "functions":
        misses["structural_invisible"].append(tool)
    if not emitted:
        misses["expected_not_emitted"].append(tool)
    misses["emitted_unresolved"] += [tool] * a["unresolved"]
    misses["executed_err"] += a["exec_err"]
    if emitted and executed:
        misses["executed_ok"].append(tool)


def probe_dynamic(misses):
    """P3: creation-time registration consumed at reuse - on the SAME agent."""
    print("\n== dynamic: create agent -> reuse its registered actions ==")
    rid = "toolharness-dyn-%d" % int(time.time())
    try:
        r = _chat("Build me an agent that reads a text file and writes a "
                  "one-line summary.", None, rid, create=True)
        pid = r.get("prompt_id")
        print("  create turn 1 ->", str(r.get("text"))[:70])
        if not pid:
            print("  FAIL: no prompt_id from create")
            misses["dynamic"] = "no prompt_id"
            return
        # the create gate asks for the agent's NAME first (#690 HITL flow) -
        # rounds 2-3 stalled at that question because the script never
        # answered it, so creation registered 0 actions
        for msg in ("Name it FileSummarizerHT.",
                    "Role: File Summarizer. Actions: 1) read the file, "
                    "2) write a one-line summary, 3) save it.",
                    "Yes, that is correct, proceed."):
            r = _chat(msg, pid, rid, create=True)
            print("  create turn   ->", str(r.get("text"))[:70])
    except base.ProbeTimeout as e:
        print("  create timed out ->", e)
        misses["dynamic"] = "create timeout"
        return

    cfg_path = os.path.join(base.PROMPTS, "%s.json" % pid)
    n_actions = 0
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        n_actions = sum(len(fl.get("actions") or [])
                        for fl in (cfg.get("flows") or []) if isinstance(fl, dict))
    print(f"  registered at creation: config exists={os.path.exists(cfg_path)} "
          f"actions={n_actions}")
    if not n_actions:
        misses["dynamic"] = "creation registered 0 actions"
        return

    w = window_start()
    try:
        r = _chat("Begin your first action now.", int(pid),
                  rid + "-reuse")
    except base.ProbeTimeout as e:
        print("  reuse timed out ->", e)
        misses["dynamic"] = "reuse timeout"
        return
    reply = str(r.get("text") or "")
    # /chat acks fast ('Let me check that for you') while the reuse pipeline
    # continues ASYNC - baseline showed 1 llm call at HTTP return.  Grace-poll
    # the same window so the async continuation is judged, not the ack.
    engaged = False
    for _ in range(9):
        a = analyze_window(w)
        engaged = "inside reuse while1" in a["log_text"]
        if engaged:
            break
        time.sleep(10)
    print(f"  reuse reply: {reply[:70]!r}")
    print(f"  action loop engaged: {engaged}   window calls: {a['calls']} "
          f"by source {a['sources']}")
    if base._is_standby(reply) or not engaged:
        misses["dynamic"] = "reuse did not engage the created actions"
    else:
        misses["dynamic"] = "ok"


def preflight():
    """App + :8080 are required.  :8081 (draft) is OPTIONAL here: #714
    measured reuse dialing :8080, and live reuse traffic runs with the
    draft server down (VRAM tiering evicts it by design) - base.preflight's
    hard :8081 gate is from #720's era and would veto a valid run."""
    print("== preflight ==")
    try:
        with urllib.request.urlopen(base.BASE + "/backend/health", timeout=10) as r:
            print("  app        :", r.status)
    except Exception as e:
        print("  app        : UNREACHABLE", e)
        return False
    for port, required in ((8080, True), (8081, False)):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                        timeout=8) as r:
                print("  llama :%d : %s" % (port, r.status))
        except Exception:
            print("  llama :%d : DOWN%s" % (port, "" if required else " (draft - optional)"))
            if required:
                return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on any miss (post-fix acceptance)")
    args = ap.parse_args()

    if not preflight():
        print("\nPREFLIGHT FAILED - results would be meaningless.")
        return 1

    misses = {"structural_invisible": [], "expected_not_emitted": [],
              "emitted_unresolved": [], "executed_err": [],
              "executed_ok": [], "dynamic": "not run"}

    proto = probe_protocol()
    pid = _first_reusable_pid()
    if pid is None:
        print("\nno reusable agent on disk - static probes need one")
    else:
        probe_static(VISIBLE_TOOL,
                     "Call the get_system_health tool and tell me what it "
                     "reports. You must invoke the tool.", pid, misses)
        probe_static(INVISIBLE_TOOL,
                     "Call the get_user_id tool and tell me the exact id it "
                     "returns. You must invoke the tool.", pid, misses)
    probe_dynamic(misses)

    print("\n==== MISSED-CALL LEDGER ====")
    if proto and not proto["functions_rendered"]:
        print("  protocol: `functions` NOT rendered by llama -> every "
              "functions[]-advertised tool is a structural miss")
    for k in ("structural_invisible", "expected_not_emitted",
              "emitted_unresolved", "executed_err", "executed_ok"):
        print("  %-22s %d  %s" % (k, len(misses[k]), misses[k]))
    print("  %-22s %s" % ("dynamic", misses["dynamic"]))

    n_miss = (len(misses["structural_invisible"])
              + len(misses["expected_not_emitted"])
              + len(misses["emitted_unresolved"])
              + len(misses["executed_err"])
              + (0 if misses["dynamic"] == "ok" else 1))
    print("\ntotal misses: %d" % n_miss)
    return 1 if (args.strict and n_miss) else 0


if __name__ == "__main__":
    sys.exit(main())
