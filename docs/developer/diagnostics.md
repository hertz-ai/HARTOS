# Runtime Diagnostics

Diagnostic instrumentation in HART OS and the Nunba desktop bundle is
**opt-in**. Nothing here runs unless you turn it on.

That is a rule, not a description. Instrumentation that ships enabled is paid
for by every user on every boot, and it is invisible precisely because it
looks like normal slowness. If you add a tracer, profiler, or verbose audit
path, gate it here and document it on this page.

---

## `NUNBA_TRACE_IMPORTS`

Traces every Python `import` performed by the frozen Nunba process and writes
it to a file, so a hang or crash can be attributed to the last module reached.

```bash
# Windows (PowerShell)
$env:NUNBA_TRACE_IMPORTS = "1"; & "C:\Program Files (x86)\HevolveAI\Nunba\Nunba.exe"

# Linux / macOS
NUNBA_TRACE_IMPORTS=1 nunba
```

| | |
|---|---|
| **Default** | off |
| **Accepted** | `1`, `true`, `yes`, `on` (case-insensitive) |
| **Output** | `~/Documents/Nunba/logs/import_trace.log` |
| **Format** | `<depth> ENTER <module>` / `<depth> LEAVE <module>` |
| **Scope** | frozen builds only (`sys.frozen`) |
| **Implemented** | `Nunba-HART-Companion/app.py`, `_trace_import` |

Read it from the tail — the last `ENTER` without a matching `LEAVE` is where
the process stopped.

```bash
tail -20 ~/Documents/Nunba/logs/import_trace.log
```

### Why it is off by default

It shipped always-on. One measured session, 2026-08-08:

```
172,075 import events across 1,734 distinct modules   (~99x re-entry)
6.5 MB log, line-buffered -> approximately one disk flush per import statement

top re-entrants (all cached-module lookups, free on their own):
  security.node_watchdog              11,494
  psutil                              10,291
  winreg                              10,104
  flask                                8,435
  integrations.google_a2a.peer_reuse   5,712
```

Python caches modules, so the second through eleven-thousandth
`import security.node_watchdog` is a `sys.modules` dict lookup. Under the
tracer each one additionally costs a Python function call, a stack push/pop,
an f-string format, and a line-buffered write. **The instrumentation cost more
than the thing it measured**, and it was the reason a boot looked slow while
the genuinely expensive imports were all correctly lazy — `torch`, `autogen`,
`chromadb`, `google.api_core`, `flaml` and `llmlingua` were each imported
**zero** times at boot.

It is also a duplicate. TrueFlow ships its own runtime injector for this,
deployed by its plugin (`.pycharm_plugin/`, gitignored) rather than from
source, so full tracing is available on demand without a resident cost.

### What is NOT gated, and why

The `_trace_import` wrapper and its **circuit breaker** remain unconditional.

The breaker is a hang *guard*, not a tracer. `transformers` 5.x re-imports
`convert_slow_tokenizer` 60M+ times under cx_Freeze; the breaker detects a
runaway re-import of the same module at the same depth, dumps the Python call
stack to `nunba_import_loop_traceback.txt`, and exits 98 rather than letting
the process die of stack exhaustion. It costs a counter comparison per import.

Gating it behind the same flag would trade a boot-time *cost* for a boot-time
*hang*, so only the file logging is gated. Every write site is guarded by
`if _imp_log is not None`, so leaving the handle unset disables tracing
cleanly without touching the guard.

A related dump, `~/Documents/Nunba/logs/import_recursion.txt`, is written when
import depth exceeds 900 — also unconditional, and also a guard rather than a
trace.

---

## Related logs

Written unconditionally; these are event logs, not per-operation traces.

| File | Contents |
|---|---|
| `frozen_debug.log` | Main Nunba + HARTOS log — timestamps, request IDs, SSE broadcasts |
| `hartos_init_error.log` | **Tier-1 import failures.** Small and easy to miss; check it first when the UI says "Loading tools… try again in a moment", which means the real `/chat` route never registered because HARTOS Tier-1 did not initialise |
| `llama_server_8080.log` | llama-server stdout/stderr, correlated to request IDs |
| `draft_decision.jsonl` | Per-request draft-model boot decisions |
| `import_trace.log` | Only when `NUNBA_TRACE_IMPORTS` is set (above) |

---

## Adding new instrumentation

1. Gate it behind an environment variable, default **off**.
2. Document it on this page: the variable, its accepted values, where output
   lands, and what the output means.
3. Keep guards separate from traces. A guard that prevents a crash should not
   be switched off by a flag whose purpose is to reduce logging.
4. State the cost. If you cannot measure it, it is not ready to ship enabled.
