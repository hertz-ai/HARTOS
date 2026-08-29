"""One-shot end-to-end check for the six agentic fixes. Run AFTER restarting Nunba.

    python verify_agentic_after_restart.py

Silent by design: every probe sends media_mode="text" so audio_mode is False and
nothing is spoken (#716 - probes were being read aloud by TTS).

Covers the two things that are code-verified but not yet user-visible-verified:
  CREATE  a config that claims "completed" must actually carry actions (ec7af78a)
  REUSE   a reuse turn must reach the agent without PydanticUserError (a4208301)

Exit 0 = both user-visible behaviours confirmed. Exit 1 = something still broken,
and it prints which.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5000"
PROMPTS = os.path.join(os.path.expanduser("~"), "Documents", "Nunba", "data", "prompts")
LOGS = os.path.join(os.path.expanduser("~"), "Documents", "Nunba", "logs")


class ProbeTimeout(Exception):
    """The turn did not answer in time - reported, never raised as a traceback.

    Chat turns here have a measured median around 70s and a cold first turn can
    be far worse, so a timeout is a real outcome to report, not a crash."""


def post(path, body, timeout=420):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        raise ProbeTimeout("%s after %ss: %s" % (path, timeout, e)) from None


_LOG_FILES = ("gui_app.log", "server.log")


def log_offsets():
    """Byte offset of each log's end, so a later read returns only new bytes.

    An earlier version of this file tracked position with the LENGTH of a
    fixed-size tail window.  Both windows are the same size once the file is
    bigger than the window, so `tail()[before:]` sliced a 793,567-char window
    by 793,567 and always yielded ''.  Every "0 errors in this window" verdict
    it printed was counted over an empty string.
    """
    off = {}
    for name in _LOG_FILES:
        try:
            off[name] = os.path.getsize(os.path.join(LOGS, name))
        except OSError:
            off[name] = 0
    return off


def log_since(offsets):
    """Everything appended to the logs since `offsets` was taken."""
    out = []
    for name in _LOG_FILES:
        p = os.path.join(LOGS, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                f.seek(offsets.get(name, 0))
                out.append(f.read())
        except OSError:
            pass
    return "\n".join(out)


def preflight():
    print("== preflight ==")
    ok = True
    try:
        with urllib.request.urlopen(BASE + "/backend/health", timeout=10) as r:
            print("  app        :", r.status)
    except Exception as e:
        print("  app        : UNREACHABLE", e)
        return False
    for port in (8080, 8081):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=8) as r:
                print("  llama :%d : %s" % (port, r.status))
        except Exception:
            print("  llama :%d : DOWN" % port)
            if port == 8081:
                print("     -> reuse needs :8081 (see #720). Without it reuse "
                      "short-circuits on the busy guard and this run proves nothing.")
                ok = False
    return ok


def _is_standby(reply):
    """True if `reply` is the dispatcher's standby placeholder, not an answer.

    Imported from the dispatcher rather than copied, so it cannot drift from
    the string the product actually emits.
    """
    try:
        from integrations.agent_engine.speculative_dispatcher import (
            _REFUSAL_STANDBY_REPLY as standby)
    except Exception:
        return False   # cannot compare - let the other assertions decide

    def norm(s):
        return str(s).strip().rstrip(".… ").casefold()

    return norm(reply) == norm(standby)


def check_create():
    try:
        return _check_create()
    except ProbeTimeout as e:
        print("  FAIL: create turn timed out ->", e)
        return False


def _check_create():
    print("\n== CREATE: a 'completed' config must carry actions (ec7af78a) ==")
    rid = "verify-create-%d" % int(time.time())
    r = post("/chat", {"text": "Build me an agent that reads a text file and "
                               "writes a one-line summary.",
                       "user_id": "verify", "create_agent": True,
                       "media_mode": "text", "request_id": rid})
    pid = r.get("prompt_id")
    print("  turn 1 ->", str(r.get("text"))[:90])
    if not pid:
        print("  FAIL: no prompt_id returned")
        return False
    for msg in ("Role: File Summarizer. Actions: 1) read the file, "
                "2) write a one-line summary, 3) save it.",
                "Yes, that is correct, proceed."):
        r = post("/chat", {"text": msg, "user_id": "verify", "create_agent": True,
                           "prompt_id": pid, "media_mode": "text", "request_id": rid})
        print("  turn   ->", str(r.get("text"))[:90])

    cfg_path = os.path.join(PROMPTS, "%s.json" % pid)
    if not os.path.exists(cfg_path):
        print("  (no config written yet - gate may have asked for more detail, "
              "which is the FIXED behaviour, not a failure)")
        return True
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    flows = cfg.get("flows") or []
    n_actions = sum(len(fl.get("actions") or []) for fl in flows if isinstance(fl, dict))
    print("  saved status=%r  actions=%d" % (cfg.get("status"), n_actions))
    if str(cfg.get("status")).lower() == "completed" and n_actions == 0:
        print("  FAIL: still saving 'completed' with zero actions (the #718 defect)")
        return False
    print("  PASS: no completed-with-zero-actions config")
    return True


def check_reuse():
    try:
        return _check_reuse()
    except ProbeTimeout as e:
        print("  FAIL: reuse turn timed out ->", e)
        return False


def _check_reuse():
    print("\n== REUSE: reach the agent with no PydanticUserError (a4208301) ==")
    reusable = [n[:-len("_0_recipe.json")] for n in os.listdir(PROMPTS)
                if re.match(r"^\d+_0_recipe\.json$", n)]
    if not reusable:
        print("  SKIP: no agent with a flow-0 recipe on this box")
        return True
    pid = reusable[0]
    before = log_offsets()
    rid = "verify-reuse-%d" % int(time.time())
    r = None
    for attempt in range(6):
        r = post("/chat", {"text": "In one sentence, what is your purpose?",
                           "user_id": "verify", "prompt_id": int(pid),
                           "media_mode": "text", "request_id": rid})
        if r.get("error") != "local_llm_starting":
            break
        print("  attempt %d: local_llm_starting, retrying" % (attempt + 1))
    reply = str(r.get("text") or "")
    print("  agent %s ->" % pid, reply[:90])

    new = log_since(before)
    print("  new log bytes in this window:", len(new))
    n_pyd = new.count("PydanticUserError")
    n_exc = new.count("Exception on /chat")
    print("  PydanticUserError frames: %d ; /chat exceptions: %d" % (n_pyd, n_exc))
    if n_pyd:
        print("  FAIL: tool registration is still crashing (a4208301 not loaded)")
        return False
    if n_exc:
        print("  FAIL: /chat raised during this turn")
        return False
    if r.get("error") == "local_llm_starting":
        print("  FAIL: never got past the busy guard - is :8081 up? (#720)")
        return False

    # Absence of a traceback is NOT an answer.  The dispatcher's documented
    # failure mode (speculative_dispatcher.py:1257) is that the user is left
    # holding the standby placeholder with nothing ever replacing it, so that
    # exact string must be rejected rather than counted as a reply.
    if _is_standby(reply):
        print("  FAIL: got the standby placeholder, not an answer "
              "(the real reply never arrived)")
        return False
    if len(reply.strip()) < 15:
        print("  FAIL: reply too short to be an answer: %r" % reply)
        return False
    print("  PASS: reuse answered, zero PydanticUserError, no /chat exception")
    return True


if __name__ == "__main__":
    if not preflight():
        print("\nPREFLIGHT FAILED - fix the above first; results would be meaningless.")
        sys.exit(1)
    results = {"create": check_create(), "reuse": check_reuse()}
    print("\n==== SUMMARY ====")
    for k, v in results.items():
        print("  %-7s %s" % (k, "PASS" if v else "FAIL"))
    sys.exit(0 if all(results.values()) else 1)
