# Future Scope — Q&A

Questions asked during development and detailed answers for future reference.

---

## Q1: Can you create a high-level diagram of the complete pipeline flow?

### Answer

```
┌─────────────────────────────────────────────────────────────┐
│                    STREAMLIT UI (app.py)                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌──────────┐  │
│  │ Upload   │  │ Provider │  │  Pipeline   │  │  RAG     │  │
│  │ PDF(s)   │  │ Selector │  │  Settings   │  │  Chat    │  │
│  └────┬─────┘  └────┬─────┘  └─────┬──────┘  └────┬─────┘  │
└───────┼──────────────┼──────────────┼──────────────┼────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────┐
│                   MIDDLEWARE STACK (llm/)                    │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  RateLimitedProvider  (asyncio.Semaphore)           │   │
│   │  └─ CachingProvider   (SHA-256 disk cache, TTL)     │   │
│   │     └─ RetryProvider  (exp backoff + Retry-After)   │   │
│   │        └─ BaseProvider (Anthropic│OpenAI│Gemini│..)  │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                             │
│   Providers: Anthropic, OpenAI, Gemini, Groq,               │
│              OpenRouter, Mistral, Ollama                     │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              INDEXING PIPELINE (page_index.py)               │
│                                                             │
│  PDF ──► get_page_tokens() ──► page_index_builder()         │
│                                       │                     │
│          ┌────────────────────────────┘                     │
│          ▼                                                  │
│  ┌──────────────────────────────────────────────────┐       │
│  │ STAGE 1: TOC Detection                           │       │
│  │  find_toc_pages() ──► toc_extractor()            │       │
│  │  [1-20 LLM calls, batched by concurrency]        │       │
│  └──────────┬───────────────────────────────────────┘       │
│             │                                               │
│             ▼  (decides branch)                             │
│  ┌──────────────────────────────────────────────────┐       │
│  │ STAGE 2: Structure Extraction (3 branches)       │       │
│  │                                                  │       │
│  │  A: TOC + page nums ──► toc_transformer()        │       │
│  │     + toc_index_extractor()  [11-21 calls]       │       │
│  │                                                  │       │
│  │  B: TOC, no page nums ──► toc_transformer()      │       │
│  │     + add_page_number_to_toc()  [1-7 calls]     │       │
│  │                                                  │       │
│  │  C: No TOC ──► generate_toc_init/continue()      │       │
│  │     [1-5 calls per chunk group]                  │       │
│  └──────────┬───────────────────────────────────────┘       │
│             ▼                                               │
│  ┌──────────────────────────────────────────────────┐       │
│  │ STAGE 3: Verification & Correction               │       │
│  │  verify_toc() ──► sample check titles on pages   │       │
│  │  fix_incorrect_toc_with_retries() if needed      │       │
│  │  [0-3N calls, batched 5/batch, 1.5s gap]         │       │
│  └──────────┬───────────────────────────────────────┘       │
│             ▼                                               │
│  ┌──────────────────────────────────────────────────┐       │
│  │ STAGE 4: Tree Building                           │       │
│  │  post_processing() ──► hierarchical tree         │       │
│  │  check_title_appearance_in_start_concurrent()    │       │
│  │  process_large_node_recursively()                │       │
│  │  [recursive: subdivide nodes > 10 pages]         │       │
│  └──────────┬───────────────────────────────────────┘       │
│             ▼                                               │
│  ┌──────────────────────────────────────────────────┐       │
│  │ STAGE 5: Enrichment (optional)                   │       │
│  │  write_node_id()                                 │       │
│  │  generate_summaries_for_structure() [N calls]    │       │
│  │  generate_doc_description()         [1 call]     │       │
│  └──────────┬───────────────────────────────────────┘       │
│             ▼                                               │
│         Final Tree JSON                                     │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    RAG Q&A PIPELINE                          │
│                                                             │
│  User Query                                                 │
│    ──► _search_nodes()      [1 LLM call: find relevant IDs]│
│    ──► _collect_node_text() [0 calls: extract page text]    │
│    ──► _generate_answer()   [1 LLM call: answer from ctx]  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### LLM Call Count Summary (per PDF)

| Stage | Best case | Worst case | Notes |
|-------|-----------|------------|-------|
| 1. TOC Detection | 2 | 22 | Parallel scan + extraction |
| 2. Structure Extraction | 1 | 21 | Depends on TOC type |
| 3. Verification | 0 | 30+ | Sample-based, batched |
| 4. Tree Building | 0 | 20+ | Recursive for large nodes |
| 5. Enrichment | 0 | 500+ | Only if summaries enabled |
| **Total** | **3** | **50-100** | **Without summaries** |

- Typical 50-page PDF with TOC: ~15-30 LLM calls
- Typical 200-page PDF without TOC: ~30-60 LLM calls

### Current Bottlenecks

1. **Sequential stages** — Stage 1 through 5 run in series, cannot overlap
2. **LLM latency dominates** — each call is 1-5s (API) or 5-30s (local)
3. **Verification is expensive** — checks every TOC item against page text
4. **Large node recursion** — re-runs entire Stage 2 pipeline per oversized node
5. **No cross-document parallelism** — only 1 PDF at a time
6. **No incremental indexing** — re-indexes from scratch every time

---

## Q2: Currently we can add only one PDF. How do we support multiple PDFs?

### Answer

Current system processes exactly 1 PDF. To support multiple:

**Architecture change needed:**
```
Current:  Upload 1 PDF ──► Index ──► 1 Tree ──► Chat about 1 doc
Future:   Upload N PDFs ──► Index each ──► N Trees ──► Chat across all docs
```

**What needs to change:**
- `st.file_uploader` — add `accept_multiple_files=True`
- `session_state` — store a **list of trees** instead of 1 tree
- Background indexing — queue multiple PDFs, index concurrently or sequentially
- RAG search — search across ALL trees, merge context from multiple docs
- UI — show which docs are indexed, allow removing individual docs

**This is a medium-sized change** — the pipeline itself stays the same, only the UI orchestration and RAG layer change.

---

## Q3: How can we reduce indexing latency? Make it work in seconds instead of minutes?

### Answer

### A. Enable Caching (instant win, zero code change)
The `CachingProvider` already exists but is **disabled by default**. If the user re-indexes the same PDF or a similar one, cached LLM responses skip the API entirely.

**Impact:** Re-indexing same doc = nearly instant. Similar docs = partial cache hits.

### B. Smarter TOC Detection (cut Stage 1 from 20 calls to 1-3)
Currently: scans up to 20 pages with LLM calls.
**Fix:** Use the heuristic (`_toc_page_heuristic`) **first** for ALL pages. Only call LLM for ambiguous pages. Most PDFs have obvious TOC pages (regex catches "Table of Contents", numbered entries).

**Impact:** Stage 1 drops from 2-20 calls to 0-3 calls.

### C. Batch TOC Transformation (cut Stage 2 from 17 calls to 1-2)
Currently: `toc_transformer` makes 1 initial call + up to 8 continuations + 8 completeness checks = 17 calls.
**Fix:** Use models with larger output windows (Gemini 2.5 Pro has 64k output). Send the entire TOC in one shot — no continuations needed.

**Impact:** Stage 2 drops from 17 calls to 1-2 calls for most docs.

### D. Parallel Verification (cut Stage 3 time by 5x)
Currently: 5 items per batch, 1.5s gap between batches.
**Fix:** For providers with high rate limits (Gemini, OpenAI), increase batch size to 20+ and reduce gap to 0.3s.

**Impact:** Stage 3 time drops from 30s to 6s for generous providers.

### E. Skip Redundant Verification
Currently: verifies EVERY TOC item.
**Fix:** Only verify a random sample (e.g., 30% of items). If 90%+ of the sample is correct, trust the rest.

**Impact:** Stage 3 calls drop by 70%.

### F. Use Faster Models for Mechanical Tasks
Not all stages need the smartest model. Stage 1 (TOC detection) and Stage 3 (verification) are mechanical yes/no tasks:

| Stage | Needs | Best model choice |
|---|---|---|
| 1. TOC detection | Pattern matching | gemini-2.0-flash-lite, gpt-4o-mini |
| 2. Structure extraction | Reasoning | gemini-2.5-pro, claude-sonnet |
| 3. Verification | Fuzzy matching | gemini-2.0-flash-lite, gpt-4o-mini |
| 4. Large node split | Reasoning | Same as Stage 2 |
| 5. Summaries | Understanding | Main model |

**Impact:** Stages 1+3 run 3-5x faster with a cheap fast model.

### G. Combined Impact

| Optimization | Time saved |
|---|---|
| Enable caching | 100% on re-index |
| Heuristic-first TOC scan | 30-60s |
| Larger output models | 30-90s |
| Parallel verification | 20-40s |
| Sample verification | 20-30s |
| Fast models for mechanical stages | 40-60s |
| **Total for 50-page PDF** | **~3-5 min down to ~30-60 seconds** |

---

## Q4: Which smaller/local models work best through APIs for reducing latency?

### Answer

All providers in our system use APIs. Speed vs quality comparison:

| Provider | Fastest model | Speed | Quality | Free? | Best for |
|---|---|---|---|---|---|
| **Groq** | llama-3.1-8b-instant | Very fast (~200ms) | Medium | Yes | Mechanical stages (1, 3) |
| **Gemini** | gemini-2.0-flash-lite | Fast (~500ms) | Good | Yes | All stages |
| **OpenRouter** | llama-3.2-3b:free | Fast | Lower | Yes | Simple tasks only |
| **Mistral** | open-mistral-nemo | Medium | Good | Yes | General use |
| **Gemini** | gemini-2.5-pro | Medium | Excellent | Yes | Complex structure extraction |

**Best strategy for speed:** Use **Groq llama-3.1-8b-instant** for Stages 1+3 (detection/verification) and **Gemini 2.5 Pro** for Stages 2+4 (structure extraction). This gives:
- Near-zero cost (both free)
- Fast mechanical tasks (Groq: ~200ms/call)
- High quality structure extraction (Gemini: best free reasoning model)

---

## Q5: The actual plan — feed code + standard guides + PDF content, then combine everything for complete output. How?

### Answer

What this describes is a multi-source knowledge system:

```
┌─────────────┐  ┌──────────────┐  ┌────────────────┐
│  Code Repo  │  │  Standard    │  │  PDF Documents  │
│  (.py, .js) │  │  Guides/Docs │  │  (manuals, etc) │
└──────┬──────┘  └──────┬───────┘  └───────┬─────────┘
       │                │                  │
       ▼                ▼                  ▼
┌──────────────────────────────────────────────────────┐
│              MULTI-SOURCE INDEXER                      │
│                                                       │
│  Code Parser        Guide Indexer      PDF Indexer     │
│  (AST, imports,     (same pipeline     (current        │
│   functions,         as PDF, or        PageIndex)      │
│   classes)           markdown parser)                  │
│       │                  │                  │          │
│       ▼                  ▼                  ▼          │
│  ┌───────────────────────────────────────────────┐    │
│  │         UNIFIED KNOWLEDGE GRAPH                │    │
│  │                                                │    │
│  │  Code nodes  ◄──links──►  Guide sections       │    │
│  │       ↕                        ↕               │    │
│  │  Function docs  ◄──links──►  PDF content       │    │
│  └───────────────────────────────────────────────┘    │
└───────────────────────────┬──────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────┐
│              CROSS-SOURCE RAG                         │
│                                                       │
│  "How does function X implement the standard Y?"      │
│  ──► Search code tree + guide tree + PDF tree          │
│  ──► Merge context from all sources                    │
│  ──► Generate answer with full context                 │
└──────────────────────────────────────────────────────┘
```

**To build this, you need:**

1. **Code Indexer** — parse code into a tree (AST for Python/JS), extract functions, classes, docstrings, imports. This is simpler than PDF indexing — no LLM needed for structure extraction, just AST parsing.

2. **Guide/Doc Indexer** — if guides are PDFs, use existing PageIndex. If markdown/HTML, parse structure directly (headers = tree nodes, no LLM needed).

3. **Unified Storage** — store all trees in a common format. Current PageIndex tree format works: `{node_id, title, start_index, end_index, nodes: [...]}`. Just add a `source_type` field ("code", "guide", "pdf").

4. **Cross-Source RAG** — modify `_search_nodes` to search across ALL indexed trees, not just one. Merge context from code + guide + PDF before generating answer.

---

## Q6: Can we improve performance by using GPUs instead of local machines or APIs?

### Answer

### When GPUs help

- **Running local models** (Ollama with llama, mistral, etc.) — GPU inference is 10-50x faster than CPU. A 7B model on GPU gives ~200ms/call vs 5-10s on CPU.
- **Self-hosted model servers** on cloud GPUs (AWS g5, GCP L4, RunPod) using vLLM or TGI — you control rate limits, zero API throttling, unlimited calls.

### When GPUs DON'T help

- **API-based providers** (Anthropic, OpenAI, Gemini, Groq, etc.) — the LLM runs on THEIR hardware, not yours. Your local machine just sends HTTP requests. A Raspberry Pi would be equally fast as an RTX 4090 for API calls.
- **PDF text extraction** (PyMuPDF) — CPU-bound, finishes in milliseconds. GPU adds nothing here.
- **Our current bottleneck** is API latency + rate limits, not local compute power.

### GPU options if you want to go local

| Option | Cost | Speed | Quality | Notes |
|---|---|---|---|---|
| **Local RTX 4090** | $1600 one-time | ~200ms/call (7B) | Good | 24GB VRAM, runs up to 13B models well |
| **Mac M-series** | Already have it | ~500ms/call (7B) | Good | Metal acceleration, unified memory helps |
| **RunPod (cloud)** | $0.40/hr (A100) | ~100ms/call (70B) | Excellent | Pay per hour, run biggest models |
| **AWS g5.xlarge** | $1.00/hr | ~150ms/call (13B) | Good | Reliable, auto-scaling possible |

### Best hybrid approach

- Use **API providers** (Gemini, Groq) for normal use — free, fast, no setup
- Use **local GPU models** (via Ollama) as fallback when:
  - You need unlimited calls with zero rate limits
  - You're working offline
  - You're processing sensitive documents that can't leave your machine
- Use **Groq** for speed-critical mechanical tasks — their LPU hardware gives ~500 tokens/sec, faster than most GPU setups

### Bottom line

For this system, investing in **faster API providers** (Groq, Gemini Flash) gives more speedup than buying a GPU. GPUs only matter if you want fully offline operation or unlimited throughput with no rate limits.

---

## Implementation Roadmap

| Phase | What | Effort | Impact |
|---|---|---|---|
| **PR2 (now)** | All providers working, concurrency slider fix | Small | Enables free-tier testing |
| **PR3** | Latency optimizations (caching, heuristics, batching) | Medium | 3-5x faster indexing |
| **PR4** | Multiple PDF support | Medium | Core feature expansion |
| **PR5** | Code indexing (AST parser + tree builder) | Medium | Enables code+docs vision |
| **PR6** | Cross-source RAG (unified search + merged context) | Medium | Full vision realized |
| **PR7** | Guide/standard doc parser (markdown/HTML) | Small | Complete multi-source |
