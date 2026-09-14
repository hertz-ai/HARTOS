# Book / learning pipeline ↔ cloud parity matrix

**What this is:** every hop the cloud book pipeline performs, and whether the
local stack has an equivalent — on BOTH agent channels, the way
`OS_PARITY_MATRIX.md` requires.

**Why it exists:** the cloud path is eight model endpoints across six hostnames
that no longer resolve. "Do we have parity locally" was being answered from
memory. Rows were first checked against the tree on 2026-09-10 and re-checked
on 2026-09-13, when the pipeline moved from Nunba into HARTOS, and on
2026-09-14, when progress moved onto the MessageBus.

**How to read a row.** *Route* = an HTTP endpoint exists. *Agent* = an LLM tool
in the registry (`core/agent_tools.py`) can invoke it during a turn. Per the OS
matrix's own warning — *"Before marking any row ❌, check BOTH channels"* — a
row is only ❌ when neither channel has it.

---

## Where the local pipeline runs

On **every HARTOS node**, desktop client or not.
`integrations/learning/book_pipeline.py` is the one implementation, and
`integrations/social/__init__.py:init_social` registers its routes
(`integrations/learning/api_books.py`) — the one step both the standalone
server and the desktop bootstrap run. Until 2026-09-13 the pipeline lived in
Nunba (`routes/upload_routes.py`, `routes/db_routes.py`), reachable only
through Nunba's consumer-routes hook, so a node without the desktop client
could not parse a book at all.

One PDF engine, **pypdfium2** (PDFium): it renders each page, reads the page's
text layer and the document outline, from prebuilt wheels for Windows, macOS,
Linux (glibc and musl, x86 and ARM) and Android. It replaced the three
libraries the old code tried in turn — pdf2image+poppler, PyMuPDF, PyPDF2 —
none of which any target shipped; PyPDF2 3.0.1 also loops forever on a crafted
content stream (CVE-2023-36464, reproduced 2026-09-13).

Every entry point calls the same pipeline:

| Entry point | Where |
|---|---|
| `POST /upload/parse_pdf` (the web app's `BOOK_PARSING_URL`) | `integrations/learning/api_books.py` |
| the `parse_book_pdf` agent tool | `integrations/learning/book_tools.py` |
| a PDF link the agent reads | `hart_intelligence_entry._parse_pdf_in_process` |
| a PDF uploaded to Nunba's `/upload/file` or `/upload/native` | `routes/upload_routes.py:_start_book_parse` |

Two limits apply on every node today:

* **Size.** A multipart upload is bounded by the node's request-size limit,
  `HEVOLVE_MAX_PAYLOAD_BYTES` (2 MB by default), not by the pipeline's own
  100 MB `MAX_PDF_BYTES`. A desktop takes larger books through `/upload/native`
  (it sends a path); any node takes them through a PDF link the agent reads.
* **Remote clients.** Off loopback every book route, page images included,
  needs a user token. The desktop's own SPA calls from loopback; today's web
  and Android clients do not send a token to these routes yet.

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
| PDF ingest | `book_parsing_upload_api` | `POST /upload/parse_pdf` — 202 at once, parse runs in the background | ✅ `parse_book_pdf` | ✅ |
| Page rasterize | (server-side) | `book_pipeline._page_image` — pypdfium2, 200 DPI, at most 2400 px on the long side | via `parse_book_pdf` | ✅ |
| Layout + OCR | docTR + PubLayNet + SegFormer + Detectron + Nougat + LLaVA (**6 models**) | `book_pipeline._parse_page_via_vision` — **ONE VLM call/page**; the page's own text layer when the model is absent or answers blank | ✅ `get_text_from_image` (img2txt) for a single image | ✅ **better than cloud** |
| Chapter segmentation | pipeline TOC logic | `book_pipeline.assign_chapters` — the PDF outline (top level = chapters, the level beneath = topics), else the ToC pages the model read | ✅ `list_book_chapters`, `read_book_chapter` | ✅ |
| Book naming | `createbookcourse` | `book_pipeline._generate_book_name` (LLM), else the PDF's own title | n/a | ✅ |
| File registry | `adduserfile` (mailer) | `BookFile` (table `pdf_files`, `integrations/social/models.py`); `GET /db/pdf_files` | ✅ `list_books` | ✅ |
| Layout persistence | `add_batch_layouts` (cloud DB) | `BookPageLayout` (table `page_layouts`); `GET /db/layouts` | ✅ `read_book_page`, `read_book_chapter` | ✅ |
| Page image | — | `GET /uploads/pdf_parse/<file_id>/page_<n>.jpg` | ✅ `page_image_url` on each page a tool returns — only when that image exists | ✅ **new** |
| Progress → client | WAMP `com.hertzai.bookparsing.{uid}` | `book_pipeline._publish` on the MessageBus topic `book.parsing`: Crossbar on the same `com.hertzai.bookparsing.{uid}`, plus SSE and PeerLink; `percentage`, the book's real `file_id` and a `msg_id` per message. A repeat upload of a book already read goes straight to 100, as central's did | n/a | ✅ |
| Agent sees upload | — | — | ✅ `get_user_uploaded_file` (returns file_id **only**) | ⚠ thin |
| **Course / subject registration** | `createbookcourse`, `createbooksubject` | ❌ no table | ❌ none | **GAP** |
| **QA creation** | `qgen`, `chatbot.py:7655 /create_qa` | ❌ | ❌ (but `tutor` expert agent declares it — below) | **GAP** |
| **Revision / retention** | `chatbot.py:4697 /revision` | ❌ | ❌ | **GAP** |
| **Assessment** | `chatbot.py:2682 /assessments`, `:2673 get_assessement_id` | ❌ | ❌ | **GAP** |
| **Recall metrics** | (cloud DB aggregate) | ❌ | ❌ | **GAP — and BLOCKED, see below** |

Over the network the routes are `require_local_or_auth`: the desktop's own SPA
calls them from loopback with no session, as it always has; any other caller
must present a user token and only ever sees its own books.

---

## What the local stack already does BETTER

Six specialist models → one VLM call per page. That is the one-model-does-all
direction, and for parsing it is **shipped** — and a node with no vision model
still produces a readable book from each page's text layer. The frontend points
at it: `landing-page/src/config/apiBase.js:96`
`BOOK_PARSING_URL = ${API_BASE_URL}/upload/parse_pdf` — the same URL, now served
by whichever HARTOS node the client talks to.

---

## The agent surface (was the gap that blocked an agentic demo)

Until 2026-09-11 PDF parsing had a route but no agent tool, so the agentic loop
broke at exactly one link. `integrations/learning/book_tools.py` now gives the
agent five tools — `list_books`, `list_book_chapters`, `read_book_page`,
`read_book_chapter`, `parse_book_pdf` — each a thin in-process call onto the
pipeline, all in `MAIN_LEG_CORE_TOOLS`.

They return data, never lessons: page text, chapter names, page numbers and
`page_image_url`. The LLM decides what to teach and when to move on — default
page-wise learning is the model reading page N, seeing `has_next`, and choosing
to continue. Quoting a page is the `page_image_url` field the chat wire schema
already carries (`core/peer_link/crossbar_publish.py`) and every client already
renders (web + desktop `Demopage.js`, Android `CrossbarAnalogyResponse.java`).

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

Re-checked 2026-09-13 by reading the named files, and by tests that drive the
real code: `tests/unit/test_book_pipeline.py` (real PDFs, real pypdfium2
rendering, text and outline, the real models on a real SQLite file),
`tests/unit/test_api_books.py` (the real routes, local and remote callers) and
`tests/unit/test_book_navigation_tools.py` (the agent tools).

A live drive also ran: a HARTOS tree booted through the real `init_social`
served a real PDF upload over HTTP, and the agent's tools read its chapters
and quoted a page image (no LLM contacted; pages from the text layer).

**Not** verified:
- a live drive of a real textbook through a running node with a vision model;
- a book over the node's 2 MB request limit, and any remote (token-carrying)
  client, since none exists yet;
- how Qwen3-VL layout output compares with PubLayNet — an open quality
  question this matrix does not settle;
- **HART OS nodes** (Nix): `nixos/packages/hart-app.nix` does not yet package
  pypdfium2 (the nixpkgs pin has no derivation for it), so there the pipeline
  answers *"PDF support is not installed on this node"* until one lands;
- **the installed desktop**: pypdfium2 ships in Nunba's python-embed
  (`scripts/deps.py` `EMBED_DEPS`), so an installer built before 2026-09-13
  does not carry it.
