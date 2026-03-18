# PageIndex — Streamlit UI + Anthropic Integration

## What changed

**Before:** CLI-only pipeline, single OpenAI provider, no UI, fragile JSON parsing, no rate-limit handling.

**After:**
- **Streamlit UI** — upload PDF, index with live progress, ask questions via RAG chat
- **LLM abstraction layer** — pluggable providers (Anthropic, OpenAI) with retry, caching, and rate-limiting middleware
- **Pipeline hardening** — robust JSON parsing (truncated/malformed responses), batched concurrent LLM calls, configurable per-provider rate-limit delays
- **Anthropic optimizations** — smart retry with `Retry-After` header support, proactive inter-call delays to avoid 429s on Tier-1 limits

## Quick start

```bash
cd doc-agent
pip install -r requirements.txt
python3 -m streamlit run app.py
```

Then in the browser:
1. Pick **Anthropic**, paste your API key, click **Apply settings**
2. Upload a PDF, click **Index document**
3. Ask questions once indexing completes
