# Book / learning pipeline ↔ cloud parity matrix

**What this is:** every hop the cloud book pipeline performs, and whether the
local (HART OS + Nunba) stack has an equivalent — on BOTH agent channels, the
way `OS_PARITY_MATRIX.md` requires.

**Why it exists:** the cloud path is eight model endpoints across six hostnames
that no longer resolve. "Do we have parity locally" was being answered from
memory. Every row below was checked against the tree on 2026-09-10.

**How to read a row.** *Route* = an HTTP endpoint exists. *Agent* = an LLM tool
in the registry (`core/agent_tools.py`) can invoke it during a turn. Per the OS
matrix's own warning — *"Before marking any row ❌, check BOTH channels"* — a
row is only ❌ when neither channel has it.

---

## The cloud pipeline, as traced

```
Android  ChatAttachImageUploadApi.java:21  @POST("book_parsing/book_parsing_upload_api")
Web      Hevolve/src/configData.js:35      ${AZURE_BASE}/book_parsing/book_parsing_upload_api
   ↓ Kong  /book_parsing → 172.21.0.1:5456
pipeline/upload_api.py:645  @app.route('/book_parsing_upload_api')
   ↓ fans out to EIGHT model endpoints (pipeline/config.json):
     firstURLDoctr / secondURLDoctr   docTR OCR         :5998  (2 VMs)
     publay / new_publay / tooni_publay  PubLayNet      (3 VMs)
     process_image                    SegFormer         :9090
     nougat                           Nougat PDF→md
     llava_url                        LLaVA VLM
   + detectron_db_update.py, bert/, crnn/, pixel_link/  (in-repo)
   ↓ result to phone
WAMP  com.hertzai.bookparsing.{user_id}   (AutobahnConnectionManager.java:1223)
```

**None of those six hostnames exist on the consolidated VM.** Even with ports
fixed, that path cannot run — which is why the local stack is the only route to
a working demo.

---

## Parity matrix

| Hop | Cloud | Local route | Local agent tool | Status |
|---|---|---|---|---|
| PDF ingest | `book_parsing_upload_api` | `upload_routes.py:743` `/upload/parse_pdf` | ❌ none | **route only** |
| Page rasterize | (server-side) | `:380` `_pdf_to_images` / `:405` `_pdf_to_images_fitz` | ❌ internal | **route only** |
| Layout + OCR | docTR + PubLayNet + SegFormer + Detectron + Nougat + LLaVA (**6 models**) | `:427` `_parse_page_via_vision` — **ONE VLM call/page** | ✅ `get_text_from_image` (img2txt) → `get_vision_api()` → `/upload/vision` | ✅ **better than cloud** |
| Chapter segmentation | pipeline TOC logic | `:491` `_assign_chapters_to_pages` | ❌ internal | **route only** |
| Book naming | `createbookcourse` | `:536` `_generate_book_name` (LLM) | ❌ internal | **route only** |
| File registry | `adduserfile` (mailer) | `db_routes.py:591` `/db/pdf_file`, table `pdf_files` | ❌ none | **route only** |
| Layout persistence | `add_batch_layouts` (cloud DB) | `:644` `/db/layout`, `:751` `/add_batch_layouts`, table `page_layouts` | ❌ none | **route only** |
| Result → client | WAMP `com.hertzai.bookparsing.{uid}` | `chatbot_routes.py:921` `publish_to_crossbar` → WAMP + **SSE fallback** | n/a | ✅ |
| Agent sees upload | — | — | ✅ `get_user_uploaded_file` (returns file_id **only**) | ⚠ thin |
| **Course / subject registration** | `createbookcourse`, `createbooksubject` | ❌ no table | ❌ none | **GAP** |
| **QA creation** | `qgen`, `chatbot.py:7655 /create_qa` | ❌ | ❌ (but `tutor` expert agent declares it — below) | **GAP** |
| **Revision / retention** | `chatbot.py:4697 /revision` | ❌ | ❌ | **GAP** |
| **Assessment** | `chatbot.py:2682 /assessments`, `:2673 get_assessement_id` | ❌ | ❌ | **GAP** |
| **Recall metrics** | (cloud DB aggregate) | ❌ | ❌ | **GAP — and BLOCKED, see below** |

---

## What the local stack already does BETTER

`upload_routes.py:747` states it in its own docstring:

> *"Replaces the cloud pipeline (pipeline repo: PubLayNet + DocTR + PixelLink +
> CRNN + Segformer + Detectron2 + SetFit) with a single VLM call per page."*

Six specialist models → one VLM. That is the one-model-does-all direction, and
for parsing it is **already shipped**. The frontend already points at it:
`landing-page/src/config/apiBase.js:96` `BOOK_PARSING_URL = ${API_BASE_URL}/upload/parse_pdf`
— the cloud constant at `:99` is still defined but is not what the picker calls.

---

## The one precisely-scoped gap that blocks an agentic demo

**PDF parsing has a route but no agent tool.** An agent in a `/chat` turn can:

- learn a file was uploaded — `get_user_uploaded_file` (returns *only* a
  file_id string, `core/agent_tools.py:1049-1058`)
- read an **image** — `get_text_from_image` → local Qwen Vision

…but it cannot turn a **PDF** into pages, because `_pdf_to_images` is internal
to Nunba's blueprint and `/upload/parse_pdf` has no tool wrapper. So the agentic
loop breaks at exactly one link.

This is task #25's rule ("expose each as an agent-actionable surface") applied
to the book pipeline: the capability exists, the agent surface does not.

**Fix shape (no new service, no new endpoint):** one tool wrapping the existing
`/upload/parse_pdf`, registered in `core/agent_tools.py` beside
`get_user_uploaded_file`. `goal_manager.py:2164` states the constraint verbatim —
*"TOOLS (use existing — DO NOT create new endpoints)"*.

---

## QA / retention: recipes, not endpoints

`integrations/expert_agents/registry.py:949-971` already ships **7 Education &
Learning agents**, including `tutor` with:

```python
AgentCapability("assessment", "Create assessments", "Tests/quizzes")
```

"Generate QA from chapter N" is the Recipe Pattern's ideal case: **CREATE** once
on chapter 1, **REUSE** for the remaining chapters at ~90% less. A 20-chapter
textbook pays for one decomposition instead of twenty `qgen` calls.

Retention scheduling is `dispatch.py::dispatch_goal` + SmartLedger, which
already persist recurring task state (`agent_data/ledger_{user_id}_{prompt_id}.json`).

---

## Recall metrics are BLOCKED, not merely missing

Outcomes reach HevolveAI through one bridge, `world_model_bridge.py`. Two
independent findings:

1. `record_interaction` (`:654`) builds an experience dict carrying prompt,
   response, model_id, latency_ms, user_id, prompt_id, node_id, goal_id,
   timestamp, escalation_reason — and **no success / quality / outcome /
   verified / reward / score field at all**.
2. Production logs show `[WorldModelBridge] HTTP mode` with `wm_flush = 0` on
   every day measured (reported by the hevolveai lane; not verified here).

So "did the learner retain this" has nowhere to land. The bridge is filed as
**task #51 — unowned by two sessions**. Every other gap in this matrix is
buildable today; this one needs an owner first.

---

## Verification status of this document

Checked in-tree 2026-09-10 by reading the named files. **Not** verified: whether
`/upload/parse_pdf` produces correct output on a real textbook page — that needs
a live drive with an actual PDF, which has not been run. Accuracy of Qwen3-VL
layout output vs PubLayNet is an open quality question, not settled by this
matrix.
