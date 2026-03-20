# PageIndex — Architecture & Pipeline Flow

## High-Level System Diagram

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
│             ▼  ← decides branch                             │
│  ┌──────────────────────────────────────────────────┐       │
│  │ STAGE 2: Structure Extraction (3 branches)       │       │
│  │                                                  │       │
│  │  ┌─ A: TOC + page nums ──► toc_transformer()    │       │
│  │  │     + toc_index_extractor()  [11-21 calls]    │       │
│  │  │                                               │       │
│  │  ├─ B: TOC, no page nums ──► toc_transformer()  │       │
│  │  │     + add_page_number_to_toc()  [1-7 calls]  │       │
│  │  │                                               │       │
│  │  └─ C: No TOC ──► generate_toc_init/continue()  │       │
│  │         [1-5 calls per chunk group]              │       │
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


## LLM Call Count Summary (per PDF)

| Stage | Best case | Worst case | Notes |
|-------|-----------|------------|-------|
| 1. TOC Detection | 2 | 22 | Parallel scan + extraction |
| 2. Structure Extraction | 1 | 21 | Depends on TOC type |
| 3. Verification | 0 | 30+ | Sample-based, batched |
| 4. Tree Building | 0 | 20+ | Recursive for large nodes |
| 5. Enrichment | 0 | 500+ | Only if summaries enabled |
| **Total** | **3** | **50-100** | **Without summaries** |

Typical 50-page PDF with TOC: ~15-30 LLM calls
Typical 200-page PDF without TOC: ~30-60 LLM calls


## Current Bottlenecks

1. **Sequential stages** — Stage 1→2→3→4→5 run in series, cannot overlap
2. **LLM latency dominates** — each call is 1-5s (API) or 5-30s (local)
3. **Verification is expensive** — checks every TOC item against page text
4. **Large node recursion** — re-runs entire Stage 2 pipeline per oversized node
5. **No cross-document parallelism** — only 1 PDF at a time
6. **No incremental indexing** — re-indexes from scratch every time
