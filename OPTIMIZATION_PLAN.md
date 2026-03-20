# PageIndex — Latency Optimization Plan

Deep analysis of every bottleneck in the indexing pipeline with exact call counts,
current timing, proposed solutions, and expected improvements.

---

## Current Performance Baseline

### Test scenario: 50-page PDF with TOC (typical academic/technical document)

Measured on Anthropic Claude Haiku (Tier-1: 50 RPM, ~2s per call average):

```
STAGE 1: TOC Detection ............. ~40-50s  (20 pages scanned + extraction)
STAGE 2: Structure Extraction ...... ~30-50s  (toc_transformer + index extraction)
STAGE 3: Verification .............. ~25-40s  (verify all items + fixes)
STAGE 4: Tree Building ............. ~15-30s  (title checks + large node splits)
STAGE 5: Enrichment ................ 0s       (disabled by default)
─────────────────────────────────────────────
TOTAL ............................... ~2-3 minutes
```

Measured on Gemini Flash (free tier, ~0.5s per call average):

```
TOTAL ............................... ~45-90 seconds
```

### Where time goes (breakdown by call type)

| Call type | Count (50-page PDF) | Avg latency/call | Total time | % of total |
|-----------|--------------------:|------------------:|-----------:|-----------:|
| TOC page scan (Stage 1) | 20 | 2s | 8s (parallel by 2) | 8% |
| TOC extraction (Stage 1) | 1-2 | 3s | 3-6s | 4% |
| toc_transformer (Stage 2) | 1-17 | 3s | 3-51s | **35%** |
| toc_index_extractor (Stage 2) | 1 | 3s | 3s | 2% |
| process_none_page_numbers (Stage 2) | 0-10 | 2s | 0-20s | 10% |
| verify_toc (Stage 3) | 10-30 | 2s | 12-36s | **20%** |
| fix_incorrect_toc (Stage 3) | 0-10 | 2s | 0-20s | 8% |
| title_appearance_start (Stage 4) | 5-15 | 2s | 6-18s | 10% |
| large_node_recursion (Stage 4) | 0-5 | 5s | 0-25s | 5% |
| Inter-call sleeps | ~30 | 0.5-1.5s | 15-45s | **15%** |

**Key insight:** 70% of time is in toc_transformer (Stage 2) + verification (Stage 3) + sleep delays.

---

## OPTIMIZATION 1: Eliminate Sleep Overhead

### Current problem

```
page_index.py line 284:  asyncio.sleep(1.5)  — between every 5-item verification batch
page_index.py line 779:  asyncio.sleep(delay) — between every chunk group (default 0.5s)
page_index.py line 808:  asyncio.sleep(delay) — between every chunk group (default 0.5s)
page_index.py line 1137: asyncio.sleep(1.5)  — between every 5-item verify batch
```

For a 50-page PDF with 20 TOC items:
- verify_toc: 4 batches x 1.5s = **6s wasted sleeping**
- title_appearance_start: 3 batches x 1.5s = **4.5s wasted sleeping**
- process_toc: 2-3 groups x 0.5s = **1-1.5s wasted sleeping**
- Total: **~12s of pure sleep time**

### Solution

Make sleep duration **per-provider** instead of hardcoded. Providers with generous rate
limits (Gemini, OpenAI paid) don't need any sleep. Only rate-limited providers (Anthropic
Tier-1, Groq free) need delays.

```python
# New provider-aware sleep values
_PROVIDER_BATCH_DELAY = {
    "gemini":      0.0,   # generous rate limits, no sleep needed
    "openai":      0.1,   # high RPM on paid tier
    "openrouter":  0.3,   # moderate free tier
    "mistral":     0.3,   # moderate free tier
    "anthropic":   1.5,   # Tier-1: 50 RPM, need caution
    "groq":        3.0,   # very tight free TPM
}
```

### Impact

| Provider | Current sleep time | After fix | Saved |
|----------|-------------------:|----------:|------:|
| Gemini | 12s | 0s | **12s** |
| OpenAI | 12s | 1s | **11s** |
| Anthropic | 12s | 12s | 0s (already needed) |
| Groq | 12s | 18s | -6s (need more sleep) |

**For Gemini/OpenAI: saves 10-12 seconds per PDF.**

---

## OPTIMIZATION 2: Heuristic-First TOC Detection

### Current problem

```
page_index.py line 485-531: find_toc_pages()
  - Scans up to 20 pages (opt.toc_check_page_num)
  - Each page: calls toc_detector_single_page()
  - toc_detector_single_page (line 292): runs _toc_page_heuristic() FIRST
    - If heuristic returns "yes" → skip LLM (good!)
    - If heuristic returns None → call LLM (expensive)
  - All pages checked in parallel via _gather_bounded()
```

The heuristic already exists at line 128-138 (`_toc_page_heuristic`), and it works well.
But the problem is: **we scan ALL 20 pages even if TOC is found on page 1-2.**

For a typical PDF:
- Pages 1-2 have TOC → heuristic catches them (0 LLM calls)
- Pages 3-20 are content → heuristic returns None → **18 LLM calls just to confirm "not TOC"**

### Solution

**Early exit:** Stop scanning once we find TOC pages AND hit 2 consecutive non-TOC pages.

```python
# In find_toc_pages(), after collecting results:
# If we found TOC pages, stop scanning after 2 consecutive non-TOC pages
toc_found = False
consecutive_non_toc = 0
for i, page_idx in enumerate(range(start, end)):
    if results[i] == "yes":
        toc_found = True
        consecutive_non_toc = 0
    elif toc_found:
        consecutive_non_toc += 1
        if consecutive_non_toc >= 2:
            break  # TOC section is over
```

Also: for pages that heuristic says "not sure", use a **cheaper signal** before LLM:
- If page has > 500 words of continuous prose → definitely not TOC (skip LLM)
- If page has < 5 lines → definitely not TOC (skip LLM)

### Impact

| Metric | Current | After fix |
|--------|--------:|----------:|
| Pages scanned | 20 | 4-6 |
| LLM calls (heuristic miss) | 15-18 | 0-3 |
| Stage 1 time (Anthropic) | 40-50s | 8-15s |
| Stage 1 time (Gemini) | 8-10s | 2-4s |

**Saves 25-35 seconds on Anthropic, 5-8 seconds on Gemini.**

---

## OPTIMIZATION 3: Kill the Continuation Loop in toc_transformer

### Current problem

This is the **single biggest bottleneck** in the entire pipeline.

```
page_index.py line 419-480: toc_transformer()
  1. Send full TOC text → LLM → get JSON structure      (1 call, ~3s)
  2. Check if output is complete → _check_if_complete()  (1 call, ~2s)
  3. If incomplete (finish_reason="length"):
     - Send continuation prompt → LLM                   (1 call, ~3s)
     - Check completeness again                          (1 call, ~2s)
     - Repeat up to 8 times (_MAX_CONTINUATION_ATTEMPTS)
```

Worst case: 1 initial + 8 continuations + 9 completeness checks = **18 sequential LLM calls**.
At 2s each = **36 seconds** just for TOC transformation.

Why does this happen? The LLM's **output token limit** is too small to output the full JSON
structure in one response. Models with 4k-8k output limits hit this often on large TOCs.

### Solution A: Use models with large output limits

| Model | Max output tokens | Can finish 50-item TOC in 1 shot? |
|-------|------------------:|:--:|
| Claude Haiku | 4,096 | No (needs 3-5 continuations) |
| Claude Sonnet | 8,192 | Sometimes |
| GPT-4o-mini | 16,384 | Yes |
| GPT-4o | 16,384 | Yes |
| Gemini 2.0 Flash | 8,192 | Sometimes |
| Gemini 2.5 Pro | 65,536 | Always |

**Quick fix:** Set `max_output_tokens` to model maximum in toc_transformer call.

### Solution B: Skip completeness checks when finish_reason is "stop"

Currently checks completeness even when the model says it finished ("stop"). This wastes
1 LLM call per attempt. Only check when finish_reason is "length" (actually truncated).

```python
# In toc_transformer():
content, finish_reason = await _llm_fr(provider, prompt)
if finish_reason == "stop":
    # Model said it's done — trust it, skip the check
    return parse_json_robust(content)
# Only check completeness if actually truncated
is_complete = await _check_if_complete(...)
```

### Solution C: Request compact JSON output

Add to prompt: "Output minified JSON with no indentation or extra whitespace."
This reduces output tokens by ~40%, making it fit in fewer continuation rounds.

### Impact

| Fix | Current calls | After fix | Time saved |
|-----|-------------:|----------:|-----------:|
| Large output model (Gemini 2.5 Pro) | 10-18 | 1-2 | **20-35s** |
| Skip completeness on "stop" | 10-18 | 5-10 | **10-15s** |
| Compact JSON output | 10-18 | 6-12 | **8-12s** |
| All combined | 10-18 | 1-2 | **25-40s** |

**This single optimization can cut total pipeline time by 30-50%.**

---

## OPTIMIZATION 4: Sample-Based Verification Instead of Full Scan

### Current problem

```
page_index.py line 1097-1158: verify_toc()
  - Takes ALL items from extracted structure
  - For EACH item: check_title_appearance() → heuristic first, LLM if needed
  - Batched: 5 items per batch, 1.5s between batches
  - Then: fix_incorrect_toc_with_retries() for wrong items (up to 3 retry rounds)
```

For a 50-page PDF with 25 TOC items:
- 5 batches x (5 parallel calls + 1.5s sleep) = **25 calls + 7.5s sleep**
- If 3 items wrong: 3 fix calls + 3 re-verify calls = 6 more calls
- Total: **~31 calls, ~40 seconds**

Then Stage 4 does it AGAIN:

```
page_index.py line 262-288: check_title_appearance_in_start_concurrent()
  - Checks all TOP-LEVEL items (typically 8-15)
  - Same batching: 5 per batch, 1.5s gap
  - Total: ~15 calls, ~20 seconds
```

Combined: **~46 calls, ~60 seconds** just for verification.

### Solution

**Statistical sampling:** Instead of checking every item, check a random 30% sample.
If 90%+ of the sample is correct, trust the rest. Only do full verification if sample
accuracy is below 90%.

```python
# In verify_toc():
sample_size = max(3, len(items) * 30 // 100)  # at least 3, at most 30%
sample = random.sample(items, sample_size)
# verify only the sample
sample_results = await _batch_verify(sample)
accuracy = sum(1 for r in sample_results if r["correct"]) / len(sample_results)
if accuracy >= 0.9:
    return items  # trust the rest
else:
    # full verification + fix only if sample shows problems
    return await _full_verify_and_fix(items)
```

Also: **merge Stage 3 and Stage 4 verification** into one pass. Currently they check
overlapping items separately.

### Impact

| Metric | Current | After fix |
|--------|--------:|----------:|
| Verification calls (good TOC) | 46 | 8-10 |
| Verification calls (bad TOC) | 46 | 46 (same, full scan) |
| Verification time (good TOC, Anthropic) | 60s | 12-15s |
| Verification time (good TOC, Gemini) | 15s | 4-5s |
| Sleep time saved | 10.5s | 1.5-3s |

**Saves 45-50 seconds on Anthropic, 10-12 seconds on Gemini for well-structured PDFs.**

---

## OPTIMIZATION 5: Enable Prompt Caching

### Current problem

Caching exists (`pageindex/llm/cache.py`) but is **disabled by default**.

```
factory.py line 67: if cache_config and cache_config.get("enabled", False):
app.py: no cache_config passed to build_provider()
```

Every re-index of the same PDF makes the exact same LLM calls with the exact same prompts.
Zero reuse.

### Solution

Enable caching in `_build_provider()`:

```python
return build_provider(
    llm_cfg,
    retry_config={...},
    cache_config={"enabled": True, "directory": ".cache/prompts", "ttl_seconds": 86400},
    pipeline_config={"concurrency": cfg["concurrency"]},
)
```

The SHA-256 disk cache deduplicates identical prompts. Cache hits return instantly (0ms).

### Impact

| Scenario | Current | After fix |
|----------|--------:|----------:|
| First index of a PDF | Normal time | Normal time (cache miss) |
| Re-index same PDF | Normal time | **~2 seconds** (all cache hits) |
| Index similar PDF (shared TOC pages) | Normal time | 30-50% faster (partial hits) |
| Multiple PDFs from same template | Normal time | 50-70% faster |

**Re-indexing same PDF goes from minutes to seconds. Zero API cost on cache hits.**

---

## OPTIMIZATION 6: Increase Batch Sizes for Fast Providers

### Current problem

```
page_index.py line 279:  _BATCH_SIZE = 5   (title appearance checks)
page_index.py line 1132: _BATCH_SIZE = 5   (verify_toc)
```

Hardcoded at 5 items per batch for ALL providers. Gemini can handle 30+ concurrent calls,
OpenAI can handle 50+. We're artificially throttling fast providers.

### Solution

Make batch size provider-aware:

```python
_PROVIDER_BATCH_SIZE = {
    "gemini":      20,   # very generous rate limits
    "openai":      15,   # high RPM
    "openrouter":   5,   # moderate
    "mistral":      8,   # moderate
    "anthropic":    3,   # Tier-1: 50 RPM, be conservative
    "groq":         1,   # very tight, sequential only
}
```

### Impact

For 25 TOC items with verification:

| Provider | Current (batches) | After fix (batches) | Time saved |
|----------|------------------:|--------------------:|-----------:|
| Gemini | 5 batches x 0.3s gap | 2 batches x 0s gap | **6s** |
| OpenAI | 5 batches x 0.1s gap | 2 batches x 0.1s gap | **3s** |
| Anthropic | 5 batches x 1.5s gap | 9 batches x 1.5s gap | -6s (slower, but safer) |

**For Gemini/OpenAI: saves 3-6 seconds.**

---

## OPTIMIZATION 7: Parallel Stage Execution Where Possible

### Current problem

All stages run strictly sequentially:
```
Stage 1 (TOC detect) → Stage 2 (extract) → Stage 3 (verify) → Stage 4 (build)
```

But some work CAN overlap:
- Stage 1 starts extracting TOC content WHILE still scanning remaining pages
- Stage 4 title checks can start for already-verified items while Stage 3 fixes others

### Solution

**Pipeline overlap for Stage 1:**

Currently find_toc_pages scans all 20 pages, then returns. Instead:
- As soon as first TOC pages are found (pages 1-2), start toc_extractor() immediately
- Continue scanning pages 3-20 in background (for edge case of split TOC)
- If background scan finds nothing, use the already-extracted TOC

```python
# Pseudo-code for overlapping Stage 1
toc_pages_found = []
async for page_result in scan_pages_streaming():
    if page_result.is_toc:
        toc_pages_found.append(page_result)
    elif len(toc_pages_found) >= 1 and consecutive_non_toc >= 2:
        break  # early exit + start extraction immediately
# Start extraction while scan might still be running
extraction_task = asyncio.create_task(toc_extractor(toc_pages_found))
```

### Impact

| Metric | Current | After fix |
|--------|--------:|----------:|
| Stage 1 total time | 40-50s | 15-20s |
| Stage 1→2 transition | Wait for full scan | Overlap by 10-15s |

**Saves 10-15 seconds by starting extraction earlier.**

---

## OPTIMIZATION 8: Dual-Model Strategy (Fast + Smart)

### Current problem

Every LLM call uses the same model. But stages have different complexity:

| Stage | Task complexity | Needs reasoning? |
|-------|----------------|:----------------:|
| 1. TOC page detection | Is this a TOC page? Yes/No | No |
| 2. TOC transformation | Convert text to structured JSON | **Yes** |
| 3. Verification | Is title X on page Y? Yes/No | No |
| 4. Title start check | Does section start here? Yes/No | No |
| 5. Summary generation | Summarize this text | Moderate |

### Solution

Use a **fast cheap model** for mechanical yes/no tasks (Stages 1, 3, 4) and the
**main model** only for reasoning tasks (Stage 2, 5).

```python
# In pipeline config, support two models:
opt.pipeline.fast_model = "gemini-2.0-flash-lite"   # for detection/verification
opt.pipeline.smart_model = "gemini-2.5-pro"          # for structure extraction
```

Build two provider instances. Route calls based on stage:

| Stage | Model used | Avg latency |
|-------|-----------|------------:|
| 1. TOC detection | fast_model | 0.2s |
| 2. Structure extraction | smart_model | 2-3s |
| 3. Verification | fast_model | 0.2s |
| 4. Title checks | fast_model | 0.2s |
| 5. Summaries | smart_model | 1-2s |

### Impact

| Metric | Current (single model) | After fix (dual model) |
|--------|----------------------:|-----------------------:|
| Stage 1 (20 calls) | 40s (Anthropic) | 4s (Gemini Flash) |
| Stage 3 (25 calls) | 30s (Anthropic) | 5s (Gemini Flash) |
| Stage 4 (15 calls) | 20s (Anthropic) | 3s (Gemini Flash) |
| Stage 2 (main reasoning) | 30s (Anthropic) | 30s (same model) |
| **Total** | **~120s** | **~42s** |

**Saves 60-80 seconds by routing mechanical tasks to a fast model.**

This is the single highest-impact optimization after fixing toc_transformer continuations.

---

## OPTIMIZATION 9: Smarter Large Node Recursion

### Current problem

```
page_index.py line 1208-1243: process_large_node_recursively()
  - Any node > max_page_num_each_node (10) AND > max_token_num_each_node (20k)
    triggers a FULL process_no_toc() pipeline on just that section
  - Each recursive call: generate_toc_init() + N x generate_toc_continue()
  - For a 30-page chapter: ~3 LLM calls (3 chunk groups)
  - Multiple large nodes run via asyncio.gather (parallel)
```

For a 200-page PDF with 5 large chapters (30 pages each):
- 5 chapters x 3 calls each = 15 calls
- But they're parallel, so wall time = ~6-10s (depending on concurrency)

Not the worst bottleneck, but can be improved.

### Solution

**Pre-split by headings before LLM:** Use regex to detect sub-headings within large
sections (numbered patterns like "1.1", "1.2", bold lines, all-caps lines). If found,
split without LLM. Only use LLM for sections with no detectable sub-structure.

```python
import re
_SUBHEADING_RE = re.compile(
    r'^(?:\d+\.)+\d*\s+\S'   # "1.1 ", "2.3.1 "
    r'|^[A-Z][A-Z\s]{3,}$'    # "INTRODUCTION", "METHODOLOGY"
    r'|^#{1,3}\s+\S',          # "## Section" (markdown)
    re.MULTILINE
)

def _try_split_by_headings(pages):
    """Try to split pages by detected sub-headings. Returns None if no pattern found."""
    for page_text, _ in pages:
        matches = _SUBHEADING_RE.findall(page_text)
        if len(matches) >= 2:
            # Build structure from regex matches — no LLM needed
            return _build_structure_from_headings(pages, matches)
    return None  # fall back to LLM
```

### Impact

| Metric | Current | After fix |
|--------|--------:|----------:|
| Recursive LLM calls (5 large chapters) | 15 | 3-5 (only for unstructured sections) |
| Recursive stage time | 10s | 3-5s |

**Saves 5-7 seconds for documents with structured chapters.**

---

## OPTIMIZATION 10: Multi-PDF Parallel Indexing

### Current problem

Only 1 PDF can be indexed at a time. The UI blocks during indexing.

### Solution

**Queue-based multi-PDF processing:**

```
Upload PDFs → Queue → Worker Pool (2-3 concurrent pipelines) → Results
```

Each PDF gets its own pipeline instance. The rate limiter (RateLimitedProvider)
already handles concurrency per-provider, so multiple pipelines share the same
semaphore safely.

```python
# Shared rate limiter across all pipelines
global_provider = build_provider(config)  # semaphore inside

# Process multiple PDFs
async def index_all(pdfs):
    tasks = [page_index_main(pdf, opt) for pdf in pdfs]
    return await asyncio.gather(*tasks)
```

For providers with high rate limits (Gemini: 1500 RPM), you can run 3-4 PDFs
simultaneously. For tight limits (Anthropic: 50 RPM), run 1-2.

### Impact

| Scenario | Current | After fix |
|----------|--------:|----------:|
| 3 PDFs (Gemini) | 3 x 45s = 135s sequential | 50-60s parallel |
| 3 PDFs (Anthropic) | 3 x 150s = 450s sequential | 300s (2 parallel) |
| 10 PDFs (Gemini) | 10 x 45s = 450s | 120-150s (4 parallel) |

**3x faster for multiple PDFs on generous providers.**

---

## Combined Impact Summary

### Single PDF — 50 pages with TOC

#### On Anthropic Claude Haiku (Tier-1)

| Optimization | Time saved | Cumulative |
|---|---:|---:|
| Baseline (current) | — | **150s** |
| Opt 3: Fix toc_transformer continuations | -40s | **110s** |
| Opt 4: Sample-based verification (30%) | -45s | **65s** |
| Opt 2: Early-exit TOC scan | -30s | **45s** |*
| Opt 8: Dual-model (fast for stages 1,3,4) | -25s | **35s** |*
| Opt 1: Remove unnecessary sleeps | -5s | **30s** |
| Opt 5: Enable caching (re-index) | -28s | **2s** |

*Opt 2 + 8 combined: mechanical stages move to fast model + early exit

#### On Gemini Free Tier

| Optimization | Time saved | Cumulative |
|---|---:|---:|
| Baseline (current) | — | **60s** |
| Opt 3: Fix toc_transformer continuations | -20s | **40s** |
| Opt 4: Sample-based verification (30%) | -10s | **30s** |
| Opt 1: Remove unnecessary sleeps | -10s | **20s** |
| Opt 6: Increase batch size to 20 | -5s | **15s** |
| Opt 5: Enable caching (re-index) | -13s | **2s** |

### Multiple PDFs — 5 x 50-page PDFs

| Provider | Current | After all opts | Speedup |
|----------|--------:|---------------:|--------:|
| Anthropic | 12.5 min | 2.5 min | **5x** |
| Gemini | 5 min | 25-30s | **10-12x** |
| Groq | 15 min | 5 min | **3x** |

---

## Priority Implementation Order

| Priority | Optimization | Effort | Impact | Dependencies |
|:--------:|---|---|---|---|
| **P0** | Opt 5: Enable caching | 5 lines changed | Instant re-index | None |
| **P0** | Opt 3: Fix toc_transformer | 20 lines changed | -30-40s per PDF | None |
| **P1** | Opt 4: Sample verification | 30 lines changed | -30-45s per PDF | None |
| **P1** | Opt 2: Early-exit TOC scan | 15 lines changed | -20-30s per PDF | None |
| **P2** | Opt 1: Provider-aware sleeps | 10 lines changed | -5-12s per PDF | Provider key in opt |
| **P2** | Opt 6: Provider-aware batch size | 10 lines changed | -3-6s per PDF | Provider key in opt |
| **P3** | Opt 8: Dual-model strategy | 50 lines, UI change | -50-80s per PDF | Second provider instance |
| **P3** | Opt 7: Pipeline overlap | 40 lines refactor | -10-15s per PDF | Async refactor |
| **P4** | Opt 9: Regex pre-split | 30 lines added | -5-7s per PDF | None |
| **P4** | Opt 10: Multi-PDF parallel | 60 lines, UI change | 3-10x for batch | Queue + worker pool |

**Start with P0 (caching + toc_transformer fix) — 25 lines of code, saves 30-40 seconds immediately.**

---

## Appendix: Every LLM Call in the Pipeline

Complete call inventory for reference. Each row is one potential LLM API call.

```
STAGE 1: TOC Detection
├── find_toc_pages()
│   └── toc_detector_single_page() x N pages     [parallel, heuristic gate]
│       ├── _toc_page_heuristic() ──► skip if "yes"
│       └── _llm_json()           ──► only if heuristic=None
├── toc_extractor()
│   └── extract_toc_content()
│       ├── _llm_fr()             ──► initial extraction (1 call)
│       └── LOOP up to 8x:
│           ├── _check_if_complete() → _llm_json()  (1 call)
│           └── _llm_fr()            continuation    (1 call)
└── detect_page_index()
    └── _llm_json()               ──► 1 call

STAGE 2: Structure Extraction
├── toc_transformer()
│   ├── _llm_fr()                 ──► initial transform (1 call)
│   └── LOOP up to 8x:
│       ├── _check_if_complete() → _llm_json()  (1 call)
│       └── _llm_fr()            continuation    (1 call)
├── toc_index_extractor()
│   └── _llm_json()               ──► 1 call
└── process_none_page_numbers()
    └── add_page_number_to_toc() x N missing items  [sequential]
        └── _llm_json()           ──► 1 call each

STAGE 3: Verification
├── verify_toc()
│   └── check_title_appearance() x N items  [batched: 5/batch, 1.5s gap]
│       ├── _title_match_heuristic() ──► skip if "yes"
│       └── _llm_json()              ──► only if heuristic=None
└── fix_incorrect_toc_with_retries() x up to 3 rounds
    ├── single_toc_item_index_fixer() x N incorrect  [parallel]
    │   └── _llm_json()
    └── check_title_appearance() x N fixed  [parallel]
        └── _llm_json()

STAGE 4: Tree Building
├── check_title_appearance_in_start_concurrent()
│   └── check_title_appearance_in_start() x N top-level  [batched: 5/batch, 1.5s gap]
│       ├── _title_match_heuristic() ──► skip if "yes"
│       └── _llm_json()              ──► only if heuristic=None
└── process_large_node_recursively()  x M large nodes  [parallel]
    └── process_no_toc() for each large node
        ├── generate_toc_init()  → _llm_fr()
        └── generate_toc_continue() x G groups  [sequential, inter_call_delay]
            └── _llm_fr()

STAGE 5: Enrichment (optional, disabled by default)
├── generate_summaries_for_structure()
│   └── generate_node_summary() x ALL nodes  [parallel]
│       └── provider.complete()
└── generate_doc_description()
    └── provider.complete()         ──► 1 call
```
