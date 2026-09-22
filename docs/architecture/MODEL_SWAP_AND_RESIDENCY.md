# Model swap and the residency record

Owner directives, 2026-09-22:

> "if there is a job which needs another model loading then we shd
> dynamically change model to free up headroom"
> "at any point in time some LLM shd be running for the nunba to work
> locally except central"
> "reclaim figure shd be derived from what we brought up, kinda Model aware"
> "eviction of main model and swapping main model are two different concerns"
> "The framework shd be model and serving engine agnostic ... do not over
> engineer now"

Tracked as #111. Status of each piece is marked below; nothing here claims
to be wired unless it says so.

---

## 1. Two concerns, deliberately not merged

| | EVICTION | SWAP |
|---|---|---|
| trigger | memory pressure | a job needs a different model |
| nature | involuntary | deliberate |
| main LLM | **excluded** (`state.pinned = True`) | **the whole point** |
| machinery | `_detect_*_pressure`, `_respond_to_*_pressure`, `_evict_idle_models` | does not exist yet |

The pin is not an obstacle to the swap. The swap never asks the evictor, so
there is nothing to bypass. The pin earned its place: it fixed an incident
where a false `device=UNLOADED` stamp bounced a foreground turn to the
LangChain-local "Loading tools..." rung and evicted piper TTS in the same
sweep.

They share only the footprint **data**, and they ask opposite questions of
it:

- eviction: *what do I get back if this goes?*
- swap: *can the newcomer come up **alongside**?*

Which is why the reclaim must **not** be added to a main-LLM swap budget.
Spending it is precisely what makes the node dark.

The pinned main-LLM state books `vram_gb = 0.0` deliberately ("zero
pinned-budget cost"). That is correct for eviction budgeting and useless
for swap planning — a second reason the two must not share one number.

---

## 2. The residency record — SHIPPED (`4c3d19102`), NOT WIRED

`ModelCatalog.record_residency()` / `.residency()`, keyed by **model**, on
the model's own row beside the predicted facts `read_gguf_facts` took from
the file. Predicted and observed in one place.

Why it could not reuse `vram_manager`'s ledger: that is keyed by **tool**.
`VRAM_BUDGETS` holds `"acestep"`/`"diffrhythm"`/`"wan2gp"`;
`record_actual_usage` takes the GPUWorker's name. Every model loading into
the `llm` slot overwrites one entry, so `can_fit('llm')` cannot tell "can
the 4B fit" from "can the 35B fit", and the reclaim figure describes
whichever model loaded last — persisted across restarts, so it can describe
a model that is not loaded.

Properties:

- **quant-aware** — the weight file is recorded and checked on read; a row
  re-pointed from Q4 to Q8 reads as unknown
- **unknown is `None`, never zero** — an unknown footprint is not a free model
- **polluted deltas are dropped** — a bad number would be persisted and
  planned against; the next load measures again
- **does not move selection** — `vram_gb`/`ram_gb`/`priority` untouched
- **stays local** — absent from the mesh `_FACT_KEYS` whitelist: a
  measurement on an 8 GB card is a fact about this machine, not the model

**Source of the numbers.** A delta around the spawn — free before, free once
the server answers. There is no alternative:
`nvidia-smi --query-compute-apps=pid,used_memory` returns `[N/A]` on Windows
WDDM.

**Not unified with `record_actual_usage`.** Deliberate, per "do not over
engineer now". The overlap is noted so a later merge is obvious; building a
bridge for one caller is not worth it today.

---

## 3. Engine agnosticism — the adapters already exist

`ModelOrchestrator._loaders` is a per-model-type registry, and
`ModelLoader` is documented as *"Applications implement this to teach the
orchestrator how to load/unload models of a specific type."* `LlamaLoader`,
`TTSLoader`, `STTLoader`, `VLMLoader` already implement `download()`,
`is_loaded()` and `validate()`.

**No new framework is needed.** The live catalog carries seven backends
across ~40 rows — `torch`, `onnx`, `sidecar`, `api`, `piper`, `in_process`,
plus runtime-populated `llama.cpp` — including two `torch` LLMs. The
acestep / diffrhythm / wan2gp family are `sidecar`.

The seam for "where did the artifact land" is one optional method with a
`None` default, so the six non-llama backends need **zero** bespoke code:

```
ModelLoader.artifact_path(entry) -> Optional[str]    # default None
```

`read_gguf_facts` stays the single reader and returns `{}` for anything it
does not understand — already its contract. A second reader is added when
there is a second reader, not before.

---

## 4. The swap sequence

Make-before-break is the **only** admissible form for the main LLM, and it
is measured to work on this hardware. During the `--cpu-moe` spike two
llama-servers were resident on one 8 GB card:

```
baseline        3321 MiB used / 4698 free    (the 4B)
spike running   6256 MiB used / 1763 free    (4B + Tiel-35B)
after teardown  3321 MiB used / 4698 free    (4B untouched throughout)
```

Break-before-make is not an option: the same model took **~7 minutes** to
become ready off the external drive. Stopping the incumbent first leaves
the node dark for minutes.

### The repoint is one value

`_find_live_llama_port` probes `[config_port, 8082, 8081, 8080]` and returns
the **first** healthy one, cached for `_LLAMA_PORT_TTL_S = 3.0`.
`invalidate_llama_port_cache()` is already described in-code as "the single
chokepoint for both transitions".

So while `config['server_port']` names the incumbent, the incumbent serves —
even with the newcomer already up and healthy on another port.

```
1. ADMISSION      free_vram >= residency(new).vram_gb
                  free_ram  >= residency(new).ram_gb
                  (catalog estimate when residency is unknown; NEVER zero)
                  the incumbent STAYS UP, so its reclaim is NOT in this budget

2. SPAWN          newcomer on a free port, not the incumbent's.
                  moe_offload_args gets the PLANNED budget, not a driver
                  sample (see Known defects).

3. VERIFY         loader.validate(entry) -- the capability probe, not process
                  liveness. A sidecar is running when it SERVES (#99).

4. REPOINT        config['server_port'] = new_port; _save_config()
                  invalidate_llama_port_cache()

5. RETIRE         stop the incumbent; record_residency(new, measured delta)
```

**Never-dark falls out of the ordering.** Config is written only after the
newcomer answers. Failure at 2 or 3: kill the newcomer, config untouched,
the incumbent never stopped.

### This dissolves the refuse-vs-floor question

If admission fails, there is no swap and the incumbent keeps serving.
Refuse-by-default is free. A small always-resident floor model is only
needed to **force** a swap that does not fit, which is a later opt-in rather
than a prerequisite.

### MoE-specific

Admission must check the right **kind** of memory. Experts want RAM, not
VRAM. Getting it wrong does not fail the load — the measured consequence is
**0.95 tok/s** (18.64 GiB of experts paged from USB against ~6.5 GiB free).

---

## 5. Scope of the never-dark claim

The swap does not go dark. That is **not** the same as "the node always has
an LLM".

#76 records the watchdog returning `False` at 14:17 and only succeeding at
19:00 — roughly five hours with no local model, from a crash-restart path
that has nothing to do with swapping. `_record_llm_alive` does not account
for it: it only heals bookkeeping **after** a successful probe, so it
records rather than enforces.

Read the watchdog restart path before asserting any broader guarantee, or
the guarantee is on paper.

Central is exempt throughout.

---

## 6. Known defects in what is already shipped

- **The MoE chain is inert** (#110). No caller passes the artifact path to
  `mark_downloaded`, so `read_gguf_facts` never runs in production,
  `capabilities['moe']` is never set, the sizing correction never fires, and
  the `matches_compute` RAM check can never trigger. `record_residency` has
  no caller either. The "0 rows with `moe:True`" reading is this defect, not
  safety. Fixed by §3's four-piece wiring.
- **`moe_offload_args` samples the driver at spawn time**, so `--cpu-moe`
  depends on timing relative to the unload. Stable for Tiel
  (21.19 × 1.35 = 28.6, above both 4.59 and 7.83) but a borderline model
  would flip. The planner must pass the planned budget.
- **A tautological test** —
  `test_dense_selection_is_unchanged_by_the_moe_flag` calls
  `pick(v, r, moe=False)` twice and asserts equality. It can never fail.

---

## 7. Build order

1. Wire the artifact path (§3) — makes the MoE chain live, and makes
   `/api/models/onboard`, the task-based setup path, work without anything
   bespoke.
2. Call `record_residency` after a successful load, from the spawn delta.
3. Swap planner (§4), in the orchestrator, using the loaders.
4. Verify end-to-end through the **product** path, not a harness.
