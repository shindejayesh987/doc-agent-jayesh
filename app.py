"""
app.py — PageIndex Streamlit UI with Supabase persistence.

Features:
  - Multi-provider LLM support (Anthropic, Gemini, Groq, OpenRouter, Mistral, OpenAI)
  - Multi-document indexing with Supabase storage
  - Persistent chat history across sessions
  - Anonymous session management
  - RAG Q&A across multiple documents
"""

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PageIndex",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.main .block-container { max-width: 860px; padding: 2rem 2rem 6rem; }
#MainMenu, footer, header { visibility: hidden; }
section[data-testid="stSidebar"] {
    background: #f9f9f9;
    border-right: 1px solid #e5e5e5;
}
.progress-box {
    background: #111;
    color: #d4d4d4;
    border-radius: 8px;
    padding: 0.75rem 1rem;
    font-size: 0.82rem;
    font-family: "SF Mono", "Fira Code", monospace;
    max-height: 260px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.6;
}
.doc-item {
    padding: 6px 10px;
    border-radius: 6px;
    margin: 4px 0;
    font-size: 0.85rem;
}
.doc-indexed { background: #e6f4ea; border: 1px solid #b7e1c0; }
.doc-indexing { background: #fff8e1; border: 1px solid #ffe082; }
.doc-failed { background: #fce4ec; border: 1px solid #ef9a9a; }
</style>
""", unsafe_allow_html=True)

# ── Configure logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)


# ── Supabase client (optional — works without it for local dev) ──────────────
@st.cache_resource
def _get_supabase():
    """Initialize Supabase client. Returns None if not configured."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        client = create_client(url, key)
        logging.getLogger(__name__).info("Supabase connected: %s", url)
        return client
    except Exception as e:
        logging.getLogger(__name__).warning("Supabase connection failed: %s", e)
        return None


def _get_or_create_user(sb):
    """Get or create an anonymous user. Returns user_id string."""
    if "user_id" in st.session_state:
        return st.session_state.user_id

    user_id = str(uuid4())

    if sb:
        try:
            # Create user via auth first (anonymous)
            # Since we use service_role key, insert directly into users table
            sb.table("users").insert({
                "id": user_id,
                "email": f"anon-{user_id[:8]}@pageindex.local",
                "display_name": f"Anonymous {user_id[:8]}",
            }).execute()
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to create user: %s", e)

    st.session_state.user_id = user_id
    return user_id


# ── Session-state defaults ────────────────────────────────────────────────────
def _init_state():
    defaults = {
        # Active document data (in-memory cache of Supabase data)
        "loaded_docs": {},          # {doc_id: {"tree": ..., "pages": ..., "name": ...}}
        "active_doc_ids": [],       # doc_ids selected for current chat
        "provider_obj": None,
        "provider_key": "gemini",   # default provider
        "provider_model": "",
        # Chat
        "messages": [],
        "active_conversation_id": None,
        # Indexing state
        "index_status": "idle",     # idle | running | done | error
        "index_log": [],
        "index_error": "",
        "log_queue": None,
        "indexing_doc_id": None,    # doc_id being indexed
        "indexing_doc_name": None,  # filename being indexed
        "indexing_start_time": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()
sb = _get_supabase()


# ── Custom log handler ────────────────────────────────────────────────────────
class _QueueHandler(logging.Handler):
    _SUPPRESSED = frozenset({"httpx", "httpcore", "urllib3", "openai._base_client"})

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            if any(record.name.startswith(p) for p in self._SUPPRESSED):
                return
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


# ── Provider catalogue ────────────────────────────────────────────────────────
_PROVIDERS = {
    "gemini": {
        "label": "Google Gemini  🆓", "factory": "gemini", "base_url": "",
        "key_hint": "AIza...  — aistudio.google.com",
        "models": ["gemini-2.5-flash", "gemini-2.0-flash",
                   "gemini-2.5-pro", "gemini-2.5-flash-lite",
                   "gemini-2.0-flash-lite"],
        "free": True, "chunk_budget": 16_000, "concurrency": 4, "inter_call_delay": 0.3,
    },
    "groq": {
        "label": "Groq  🆓", "factory": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "key_hint": "gsk_...  — console.groq.com",
        "models": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile",
                   "gemma2-9b-it", "mixtral-8x7b-32768"],
        "free": True, "chunk_budget": 6_000, "concurrency": 1, "inter_call_delay": 3.0,
    },
    "openrouter": {
        "label": "OpenRouter  🆓", "factory": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "key_hint": "sk-or-...  — openrouter.ai",
        "models": ["meta-llama/llama-3.1-8b-instruct:free",
                   "meta-llama/llama-3.2-3b-instruct:free",
                   "google/gemma-2-9b-it:free",
                   "mistralai/mistral-7b-instruct:free",
                   "microsoft/phi-3-mini-128k-instruct:free"],
        "free": True, "chunk_budget": 8_000, "concurrency": 2, "inter_call_delay": 1.0,
    },
    "mistral": {
        "label": "Mistral AI  🆓", "factory": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "key_hint": "...  — console.mistral.ai",
        "models": ["mistral-small-latest", "open-mistral-nemo",
                   "mistral-large-latest", "codestral-latest"],
        "free": True, "chunk_budget": 12_000, "concurrency": 2, "inter_call_delay": 0.5,
    },
    "openai": {
        "label": "OpenAI", "factory": "openai", "base_url": "",
        "key_hint": "sk-...  — platform.openai.com",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini"],
        "free": False, "chunk_budget": 20_000, "concurrency": 8, "inter_call_delay": 0.1,
    },
    "anthropic": {
        "label": "Anthropic", "factory": "anthropic", "base_url": "",
        "key_hint": "sk-ant-...  — console.anthropic.com",
        "models": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"],
        "free": False, "chunk_budget": 20_000, "concurrency": 2, "inter_call_delay": 2.0,
    },
}


# ── User-friendly error mapper ────────────────────────────────────────────────
_PROVIDER_KEY_URLS = {
    "gemini": "aistudio.google.com/apikey",
    "groq": "console.groq.com/keys",
    "openrouter": "openrouter.ai/keys",
    "mistral": "console.mistral.ai/api-keys",
    "openai": "platform.openai.com/api-keys",
    "anthropic": "console.anthropic.com/settings/keys",
}


def _friendly_error(exc: Exception, provider_key: str = "", model: str = "") -> str:
    """Map raw exceptions to user-friendly messages with actionable suggestions."""
    err = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    key_url = _PROVIDER_KEY_URLS.get(provider_key, "")
    provider_label = _PROVIDERS.get(provider_key, {}).get("label", provider_key).split()[0]
    available_models = _PROVIDERS.get(provider_key, {}).get("models", [])

    # ── API key errors ────────────────────────────────────────────────────
    if any(k in err for k in ("authentication", "unauthorized", "invalid api key",
                               "invalid x-api-key", "api key not valid",
                               "api_key_invalid", "invalid_api_key", "401")):
        msg = f"**Invalid API key** for {provider_label}."
        if key_url:
            msg += f"\n\nGet a valid key at **{key_url}**"
        msg += "\n\nMake sure you're using the right key for this provider — keys from one provider won't work with another."
        return msg

    # ── Model not found ───────────────────────────────────────────────────
    if any(k in err for k in ("not found", "not_found", "does not exist",
                               "no longer available", "model not found")):
        msg = f"**Model `{model}` is not available.**"
        if available_models:
            others = [m for m in available_models if m != model]
            if others:
                msg += f"\n\nTry one of these instead:\n" + "\n".join(f"- `{m}`" for m in others[:3])
        return msg

    # ── Rate limiting ─────────────────────────────────────────────────────
    if any(k in err for k in ("rate limit", "ratelimit", "rate_limit",
                               "too many requests", "429", "quota",
                               "resource_exhausted", "resource exhausted")):
        msg = f"**Rate limited** by {provider_label}."
        msg += "\n\n**What to do:**"
        msg += "\n- Wait 30–60 seconds and try again"
        msg += "\n- Use a smaller PDF (fewer pages = fewer API calls)"
        if provider_key == "groq":
            msg += "\n- Groq has strict free-tier limits — try **Gemini** instead"
        elif provider_key == "openrouter":
            msg += "\n- Free models on OpenRouter have tight limits — try **Gemini** for larger docs"
        return msg

    # ── Network / connection errors ───────────────────────────────────────
    if any(k in err for k in ("timeout", "timed out", "connect", "connection",
                               "network", "unreachable", "dns", "ssl")):
        if "pipeline" in err or "page_index_main" in err:
            msg = "**Indexing timed out** — the document is too large for the current timeout."
            msg += "\n\n**What to do:**"
            msg += "\n- Try a smaller PDF"
            msg += "\n- Use a faster model like `gemini-2.5-flash`"
        else:
            msg = f"**Connection failed** to {provider_label} API."
            msg += "\n\n**What to do:**"
            msg += "\n- Check your internet connection"
            msg += "\n- The API may be temporarily down — wait a minute and retry"
        return msg

    # ── Empty/scanned PDF ─────────────────────────────────────────────────
    if any(k in err for k in ("no pages found", "pdf appears to be empty",
                               "no pages could be extracted")):
        return ("**This PDF has no extractable text.**"
                "\n\nThis usually means it's a scanned document (images of pages, not real text)."
                "\n\n**What to do:**"
                "\n- Run the PDF through an OCR tool (like Adobe Acrobat or ocrmypdf) first"
                "\n- Try a different version of the document")

    # ── Invalid PDF ───────────────────────────────────────────────────────
    if any(k in err for k in ("unsupported input", "invalid pdf", "not a pdf",
                               "pdf header")):
        return ("**Invalid PDF file.**"
                "\n\nThe uploaded file doesn't appear to be a valid PDF."
                "\n\n**What to do:**"
                "\n- Make sure you're uploading a `.pdf` file"
                "\n- Try re-downloading or re-exporting the document as PDF")

    # ── Content safety / blocked ──────────────────────────────────────────
    if any(k in err for k in ("safety", "blocked", "content_filter", "harmful",
                               "recitation")):
        return ("**Content blocked** by the model's safety filter."
                "\n\nThe model refused to process part of the document."
                "\n\n**What to do:**"
                "\n- Try a different model"
                "\n- If the document contains sensitive content, try a provider with more permissive filters")

    # ── Token/context limit exceeded ──────────────────────────────────────
    if any(k in err for k in ("token", "context length", "max_tokens",
                               "context_length_exceeded")):
        return ("**Document exceeds the model's context limit.**"
                "\n\n**What to do:**"
                "\n- Try a model with a larger context window (e.g. `gemini-2.5-pro`)"
                "\n- Use a smaller PDF")

    # ── JSON parsing failures (from pipeline) ─────────────────────────────
    if any(k in err for k in ("json", "parse_json", "expecting value",
                               "unterminated string")):
        return ("**The model returned an invalid response.**"
                "\n\nThis sometimes happens with smaller/free models."
                "\n\n**What to do:**"
                "\n- Try again — it often works on retry"
                "\n- Switch to a more capable model like `gemini-2.5-flash`")

    # ── Fallback — unknown error ──────────────────────────────────────────
    msg = f"**Something went wrong:** {type(exc).__name__}"
    msg += f"\n\n`{str(exc)[:200]}`"
    msg += "\n\n**What to do:**"
    msg += "\n- Try again — transient errors are common with LLM APIs"
    msg += "\n- Try a different model or provider"
    if key_url:
        msg += f"\n- Verify your API key at **{key_url}**"
    return msg


# ── Provider builder ──────────────────────────────────────────────────────────
def _build_provider(provider_key: str, model: str, api_key: str = "") -> object:
    from pageindex.llm.factory import build_provider
    cfg = _PROVIDERS[provider_key]
    llm_cfg = {"provider": cfg["factory"], "model": model}
    api_key = api_key.strip()
    if api_key:
        llm_cfg["api_key"] = api_key
    if cfg["base_url"]:
        llm_cfg["base_url"] = cfg["base_url"]
    return build_provider(
        llm_cfg,
        retry_config={"max_attempts": 3, "base_delay_seconds": 2.0,
                      "max_delay_seconds": 60.0, "backoff_factor": 2.0},
        pipeline_config={"concurrency": cfg["concurrency"]},
    )


# ── Supabase document operations ─────────────────────────────────────────────
def _load_user_documents():
    """Load all documents for the current user from Supabase."""
    if not sb or "user_id" not in st.session_state:
        return []
    try:
        result = (
            sb.table("documents")
            .select("id, name, page_count, total_tokens, status, provider_used, "
                    "model_used, indexing_duration_ms, created_at, indexed_at, error_message")
            .eq("user_id", st.session_state.user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to load documents: %s", e)
        return []


def _load_document_data(doc_id: str):
    """Load tree_json and pages_json for a document from Supabase into memory."""
    if doc_id in st.session_state.loaded_docs:
        return st.session_state.loaded_docs[doc_id]
    if not sb:
        return None
    try:
        result = (
            sb.table("documents")
            .select("name, tree_json, pages_json")
            .eq("id", doc_id)
            .eq("status", "indexed")
            .single()
            .execute()
        )
        if result.data and result.data.get("tree_json"):
            doc_data = {
                "tree": result.data["tree_json"],
                "pages": result.data["pages_json"],
                "name": result.data["name"],
            }
            st.session_state.loaded_docs[doc_id] = doc_data
            return doc_data
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to load document %s: %s", doc_id[:8], e)
    return None


def _save_document_to_supabase(doc_id, tree, page_list, duration_ms):
    """Save indexed tree and pages to Supabase."""
    if not sb:
        return
    try:
        pages_data = [[p[0], p[1]] for p in page_list]
        sb.table("documents").update({
            "status": "indexed",
            "tree_json": tree,
            "pages_json": pages_data,
            "page_count": len(page_list),
            "total_tokens": sum(p[1] for p in page_list),
            "indexing_duration_ms": duration_ms,
            "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).eq("id", doc_id).execute()
        logging.getLogger(__name__).info("Saved document %s to Supabase", doc_id[:8])
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to save document: %s", e)


def _delete_document(doc_id):
    """Delete a document from Supabase."""
    if not sb:
        return
    try:
        sb.table("documents").delete().eq("id", doc_id).execute()
        # Remove from loaded cache
        st.session_state.loaded_docs.pop(doc_id, None)
        if doc_id in st.session_state.active_doc_ids:
            st.session_state.active_doc_ids.remove(doc_id)
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to delete document: %s", e)


# ── Supabase conversation/message operations ─────────────────────────────────
def _ensure_conversation():
    """Get or create the active conversation."""
    if st.session_state.active_conversation_id:
        return st.session_state.active_conversation_id

    if not sb or "user_id" not in st.session_state:
        st.session_state.active_conversation_id = str(uuid4())
        return st.session_state.active_conversation_id

    try:
        conv_id = str(uuid4())
        doc_names = []
        for did in st.session_state.active_doc_ids:
            doc = st.session_state.loaded_docs.get(did)
            if doc:
                doc_names.append(doc["name"])
        title = ", ".join(doc_names)[:100] if doc_names else "New conversation"

        sb.table("conversations").insert({
            "id": conv_id,
            "user_id": st.session_state.user_id,
            "title": title,
            "doc_ids": st.session_state.active_doc_ids,
        }).execute()
        st.session_state.active_conversation_id = conv_id
        return conv_id
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to create conversation: %s", e)
        st.session_state.active_conversation_id = str(uuid4())
        return st.session_state.active_conversation_id


def _save_message(role, content, sources=None, model_used=None, latency_ms=None):
    """Save a message to Supabase."""
    if not sb:
        return
    conv_id = _ensure_conversation()
    try:
        row = {
            "conversation_id": conv_id,
            "role": role,
            "content": content,
            "sources": sources or [],
        }
        if model_used:
            row["model_used"] = model_used
        if latency_ms is not None:
            row["latency_ms"] = latency_ms
        sb.table("messages").insert(row).execute()
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to save message: %s", e)


def _load_conversation_messages(conv_id):
    """Load messages for a conversation from Supabase."""
    if not sb:
        return []
    try:
        result = (
            sb.table("messages")
            .select("role, content, sources, created_at")
            .eq("conversation_id", conv_id)
            .order("created_at", desc=False)
            .execute()
        )
        return [{"role": m["role"], "content": m["content"]} for m in result.data]
    except Exception:
        return []


def _load_user_conversations():
    """Load all conversations for the current user."""
    if not sb or "user_id" not in st.session_state:
        return []
    try:
        result = (
            sb.table("conversations")
            .select("id, title, message_count, last_message_at")
            .eq("user_id", st.session_state.user_id)
            .order("last_message_at", desc=True)
            .limit(20)
            .execute()
        )
        return result.data
    except Exception:
        return []


# ── Background indexing thread ────────────────────────────────────────────────
def _run_indexing(pdf_bytes, provider_obj, opt, q, doc_id=None, sb_ref=None):
    """Runs in a daemon thread. Saves results to Supabase if available."""
    handler = _QueueHandler(q)
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    root_pi = logging.getLogger("pageindex")
    root_pi.addHandler(handler)
    root_pi.setLevel(logging.INFO)

    start_time = time.time()

    try:
        from pageindex.page_index import page_index_main
        from pageindex.utils import get_page_tokens

        q.put(("log", "Extracting pages from PDF…"))
        page_list = get_page_tokens(BytesIO(pdf_bytes))

        if not page_list:
            q.put(("error", "No pages found in PDF — is it a scanned/image PDF?"))
            if sb_ref and doc_id:
                sb_ref.table("documents").update({
                    "status": "failed", "error_message": "No pages found",
                }).eq("id", doc_id).execute()
            return

        total_tokens = sum(p[1] for p in page_list)
        q.put(("log", f"Found {len(page_list)} pages · {total_tokens:,} tokens total"))
        q.put(("log", "Building document tree — this can take several minutes…"))

        # Update status in Supabase
        if sb_ref and doc_id:
            sb_ref.table("documents").update({
                "status": "indexing",
                "page_count": len(page_list),
                "total_tokens": total_tokens,
            }).eq("id", doc_id).execute()

        result = page_index_main(BytesIO(pdf_bytes), opt=opt)
        duration_ms = int((time.time() - start_time) * 1000)

        # Save to Supabase
        if sb_ref and doc_id:
            try:
                pages_data = [[p[0], p[1]] for p in page_list]
                sb_ref.table("documents").update({
                    "status": "indexed",
                    "tree_json": result,
                    "pages_json": pages_data,
                    "indexing_duration_ms": duration_ms,
                    "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }).eq("id", doc_id).execute()
                q.put(("log", f"✓ Saved to database ({duration_ms / 1000:.1f}s)"))
            except Exception as e:
                q.put(("log", f"⚠ Database save failed: {e}"))

        q.put(("log", "✓ Indexing complete!"))
        q.put(("done", result, page_list, doc_id))

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        q.put(("log", f"✗ {exc}"))
        q.put(("log", tb))
        q.put(("error", str(exc)))
        if sb_ref and doc_id:
            try:
                sb_ref.table("documents").update({
                    "status": "failed", "error_message": str(exc)[:2000],
                }).eq("id", doc_id).execute()
            except Exception:
                pass
    finally:
        root_pi.removeHandler(handler)


# ── Drain the log queue ──────────────────────────────────────────────────────
def _drain_queue():
    q = st.session_state.log_queue
    if q is None:
        return
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            break
        kind = msg[0]
        if kind == "log":
            st.session_state.index_log.append(msg[1])
        elif kind == "done":
            tree = msg[1]
            page_list = msg[2]
            doc_id = msg[3] if len(msg) > 3 else None

            # Cache in memory
            if doc_id:
                st.session_state.loaded_docs[doc_id] = {
                    "tree": tree,
                    "pages": [[p[0], p[1]] for p in page_list],
                    "name": st.session_state.get("indexing_doc_name", "document"),
                }
                if doc_id not in st.session_state.active_doc_ids:
                    st.session_state.active_doc_ids.append(doc_id)

            st.session_state.index_status = "done"
            st.session_state.log_queue = None
        elif kind == "error":
            st.session_state.index_status = "error"
            st.session_state.index_error = msg[1]
            st.session_state.log_queue = None

_drain_queue()


# ── RAG pipeline (multi-document) ────────────────────────────────────────────
async def _search_nodes(tree: dict, query: str, provider) -> list:
    from pageindex.llm.base import Message

    def strip_text(node):
        if isinstance(node, list):
            return [strip_text(item) for item in node]
        if not isinstance(node, dict):
            return node
        n = {k: v for k, v in node.items() if k != "text"}
        if "nodes" in n:
            n["nodes"] = [strip_text(c) for c in n["nodes"]]
        return n

    structure = tree.get("structure", tree) if isinstance(tree, dict) else tree
    tree_lite = strip_text(structure)
    prompt = (
        "You are a document search assistant.\n"
        "Given the document tree and user question, return ONLY a JSON array "
        "of the most relevant node_id strings (max 5).\n"
        'Example: ["1", "1.2", "3"]\n\n'
        f"Question: {query}\n\n"
        f"Document tree:\n{json.dumps(tree_lite, indent=2)}"
    )
    resp = await provider.complete([Message(role="user", content=prompt)])
    raw = resp.content or "[]"
    try:
        from pageindex.utils import parse_json_robust
        ids = parse_json_robust(raw)
        if isinstance(ids, list):
            return [str(i) for i in ids]
    except Exception:
        pass
    return re.findall(r'"([^"]+)"', raw)[:5]


def _collect_node_text(tree, node_ids: list, page_list: list) -> str:
    chunks = []

    def walk(node):
        if not isinstance(node, dict):
            return
        nid = str(node.get("node_id", ""))
        if not node_ids or nid in node_ids:
            start = node.get("start_index", 1)
            end = node.get("end_index", start)
            text = "\n".join(p[0] for p in page_list[start - 1: end])
            chunks.append(f"[{node.get('title', 'Section')}]\n{text}")
        for child in node.get("nodes", []):
            walk(child)

    root = tree.get("structure", tree) if isinstance(tree, dict) else tree
    if isinstance(root, list):
        for n in root:
            walk(n)
    elif isinstance(root, dict):
        walk(root)

    return "\n\n".join(chunks)[:12_000]


async def _generate_answer(context: str, query: str, history: list, provider) -> str:
    from pageindex.llm.base import Message
    # Build messages with chat history for context
    messages = []
    messages.append(Message(
        role="system",
        content=(
            "You are a document Q&A assistant. Answer questions using only the "
            "document context provided. Be concise and accurate. If the context "
            "doesn't contain the answer, say so. When referencing information, "
            "mention which document section it came from."
        ),
    ))
    # Add recent chat history for conversational context
    for h in history[-6:]:
        messages.append(Message(role=h["role"], content=h["content"]))
    # Add current query with context
    messages.append(Message(
        role="user",
        content=f"Question: {query}\n\nDocument context:\n{context}",
    ))
    resp = await provider.complete(messages)
    return resp.content or "No answer generated."


def _run_rag_multi(query: str, doc_data_list: list, provider, history: list) -> str:
    """Run RAG across multiple documents."""
    async def _go():
        all_context_parts = []

        # Search each document in parallel
        search_tasks = []
        for doc_data in doc_data_list:
            search_tasks.append(_search_nodes(doc_data["tree"], query, provider))
        all_node_ids = await asyncio.gather(*search_tasks)

        # Collect text from each document
        for doc_data, node_ids in zip(doc_data_list, all_node_ids):
            text = _collect_node_text(doc_data["tree"], node_ids, doc_data["pages"])
            if text.strip():
                all_context_parts.append(f"[Document: {doc_data['name']}]\n{text}")

        if not all_context_parts:
            # Fallback: first 5 pages of first doc
            first = doc_data_list[0]
            fallback = "\n".join(p[0] for p in first["pages"][:5])
            all_context_parts.append(f"[Document: {first['name']}]\n{fallback}")

        # Truncate total context
        merged = "\n\n---\n\n".join(all_context_parts)
        if len(merged) > 15_000:
            merged = merged[:15_000] + "\n...(truncated)"

        return await _generate_answer(merged, query, history, provider)

    return asyncio.run(_go())


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    # ── Model settings ────────────────────────────────────────────────────────
    st.markdown("## ⚙️ Model settings")

    provider_key = st.selectbox(
        "Provider",
        list(_PROVIDERS.keys()),
        format_func=lambda k: _PROVIDERS[k]["label"],
    )
    cfg = _PROVIDERS[provider_key]
    model = st.selectbox("Model", cfg["models"])
    api_key = st.text_input("API key", type="password", placeholder=cfg["key_hint"])

    if cfg["free"]:
        st.caption("✓ Free tier available")

    if st.button("Apply settings", use_container_width=True):
        if not api_key.strip() and not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
            key_url = _PROVIDER_KEY_URLS.get(provider_key, "")
            st.error(f"**API key required.** Enter your {cfg['label'].split()[0]} key above."
                     + (f"\n\nGet one free at **{key_url}**" if key_url else ""))
        else:
            with st.spinner("Connecting…"):
                try:
                    st.session_state.provider_obj = _build_provider(provider_key, model, api_key)
                    st.session_state.provider_key = provider_key
                    st.session_state.provider_model = model
                    st.success(f"✓ {cfg['label'].split()[0]} / {model}")
                except Exception as e:
                    st.error(_friendly_error(e, provider_key, model))

    st.divider()

    # ── Pipeline settings ─────────────────────────────────────────────────────
    with st.expander("Pipeline settings"):
        timeout_val = st.number_input("Timeout (s, 0=none)", 0, value=3600, step=60)
        prov_default_conc = _PROVIDERS.get(
            st.session_state.get("provider_key", "gemini"), {}
        ).get("concurrency", 4)
        concurrency_val = st.number_input(
            "Max concurrency", 1, value=prov_default_conc, step=1,
            help="Parallel LLM calls. Lower = safer for free-tier rate limits.",
        )

    st.divider()

    # ── My Documents (from Supabase) ──────────────────────────────────────────
    st.markdown("## 📁 My Documents")

    # Ensure user exists
    if sb:
        _get_or_create_user(sb)
        user_docs = _load_user_documents()
    else:
        user_docs = []

    # Show in-memory docs if no Supabase
    if not sb and st.session_state.loaded_docs:
        for doc_id, doc_data in st.session_state.loaded_docs.items():
            is_active = doc_id in st.session_state.active_doc_ids
            label = f"{'✅' if is_active else '⬜'} {doc_data['name']}"
            if st.button(label, key=f"toggle_{doc_id}", use_container_width=True):
                if is_active:
                    st.session_state.active_doc_ids.remove(doc_id)
                else:
                    st.session_state.active_doc_ids.append(doc_id)
                st.session_state.active_conversation_id = None
                st.session_state.messages = []
                st.rerun()

    # Show Supabase docs
    if user_docs:
        indexed_docs = [d for d in user_docs if d["status"] == "indexed"]
        other_docs = [d for d in user_docs if d["status"] != "indexed"]

        for doc in indexed_docs:
            doc_id = doc["id"]
            is_active = doc_id in st.session_state.active_doc_ids
            col1, col2 = st.columns([5, 1])
            with col1:
                label = f"{'✅' if is_active else '⬜'} {doc['name']}"
                if st.button(label, key=f"toggle_{doc_id}", use_container_width=True):
                    if is_active:
                        st.session_state.active_doc_ids.remove(doc_id)
                        st.session_state.loaded_docs.pop(doc_id, None)
                    else:
                        # Load tree from Supabase into memory
                        loaded = _load_document_data(doc_id)
                        if loaded:
                            st.session_state.active_doc_ids.append(doc_id)
                    st.session_state.active_conversation_id = None
                    st.session_state.messages = []
                    st.rerun()
            with col2:
                if st.button("🗑", key=f"del_{doc_id}"):
                    _delete_document(doc_id)
                    st.rerun()

        for doc in other_docs:
            status_icon = "⏳" if doc["status"] == "indexing" else "❌"
            st.caption(f"{status_icon} {doc['name']} — {doc['status']}")
    elif not st.session_state.loaded_docs:
        st.caption("No documents yet. Upload a PDF above.")

    # Show active doc count
    active_count = len(st.session_state.active_doc_ids)
    if active_count > 0:
        st.success(f"● {active_count} document{'s' if active_count > 1 else ''} selected for Q&A")
    elif st.session_state.index_status == "running":
        st.warning("● Indexing in progress…")
    else:
        st.caption("○ Select documents to start chatting")

    st.divider()

    # ── Chat History ──────────────────────────────────────────────────────────
    if sb:
        with st.expander("Chat history"):
            conversations = _load_user_conversations()
            for conv in conversations[:10]:
                conv_label = f"{conv['title'][:40]} ({conv['message_count']} msgs)"
                if st.button(conv_label, key=f"conv_{conv['id']}", use_container_width=True):
                    st.session_state.active_conversation_id = conv["id"]
                    st.session_state.messages = _load_conversation_messages(conv["id"])
                    st.rerun()

            if st.button("+ New conversation", use_container_width=True):
                st.session_state.active_conversation_id = None
                st.session_state.messages = []
                st.rerun()

    st.divider()
    db_status = "🟢 Supabase" if sb else "🔴 Local only"
    st.caption(f"{db_status} · Logs: run `python3 -m streamlit run app.py`")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN CONTENT
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("# 📄 PageIndex")
st.markdown("Index PDF documents then ask questions across all of them.")
st.divider()

# ── Upload section ────────────────────────────────────────────────────────────
if st.session_state.index_status not in ("running",):
    uploaded = st.file_uploader(
        "Upload a PDF",
        type=["pdf"],
        label_visibility="collapsed",
        accept_multiple_files=False,
    )

    if uploaded:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(f"**{uploaded.name}** — {uploaded.size / 1024:.0f} KB")
        with col2:
            provider_ready = st.session_state.provider_obj is not None
            go = st.button(
                "Index document", type="primary", use_container_width=True,
                disabled=not provider_ready,
            )

        if not provider_ready:
            st.info("**Configure your LLM provider first** — select a provider, enter your API key, and click **Apply settings** in the sidebar.")

        if go:

            pdf_bytes = uploaded.read()
            provider_obj = st.session_state.provider_obj
            prov_cfg = _PROVIDERS.get(
                st.session_state.get("provider_key", "gemini"), _PROVIDERS["gemini"]
            )

            # Create document record in Supabase
            doc_id = str(uuid4())
            if sb and "user_id" in st.session_state:
                try:
                    sb.table("documents").insert({
                        "id": doc_id,
                        "user_id": st.session_state.user_id,
                        "name": uploaded.name,
                        "file_size_bytes": len(pdf_bytes),
                        "status": "uploaded",
                        "provider_used": st.session_state.get("provider_key", "gemini"),
                        "model_used": prov_cfg.get("models", ["unknown"])[0],
                    }).execute()
                except Exception as e:
                    logging.getLogger(__name__).warning("Failed to create doc record: %s", e)

            opt = SimpleNamespace(
                provider=provider_obj,
                toc_check_page_num=20,
                max_page_num_each_node=10,
                max_token_num_each_node=prov_cfg["chunk_budget"],
                if_add_node_id="yes",
                if_add_node_text="no",
                if_add_node_summary="no",
                if_add_doc_description="no",
                pipeline=SimpleNamespace(
                    timeout_seconds=timeout_val if timeout_val > 0 else None,
                    concurrency=concurrency_val,
                    chunk_token_budget=prov_cfg["chunk_budget"],
                    inter_call_delay=prov_cfg.get("inter_call_delay", 0.5),
                ),
            )

            q = queue.Queue()
            st.session_state.log_queue = q
            st.session_state.index_status = "running"
            st.session_state.index_log = []
            st.session_state.index_error = ""
            st.session_state.indexing_doc_id = doc_id
            st.session_state.indexing_doc_name = uploaded.name
            st.session_state.indexing_start_time = time.time()

            t = threading.Thread(
                target=_run_indexing,
                args=(pdf_bytes, provider_obj, opt, q, doc_id, sb),
                daemon=True,
            )
            t.start()
            st.rerun()

# ── Progress display ──────────────────────────────────────────────────────────
if st.session_state.index_status == "running":
    doc_name = st.session_state.get("indexing_doc_name", "document")
    st.markdown(f"### Indexing: {doc_name}")

    log_lines = st.session_state.index_log
    display = "\n".join(log_lines) if log_lines else "Starting…"
    st.markdown(f'<div class="progress-box">{display}</div>', unsafe_allow_html=True)
    st.caption("Tip: full LLM call-level logs are printed in your terminal.")
    time.sleep(1.5)
    st.rerun()

elif st.session_state.index_status == "error":
    # Show user-friendly error with suggestions
    raw_error = st.session_state.index_error
    pkey = st.session_state.get("provider_key", "")
    pmodel = st.session_state.get("provider_model", "")
    friendly_msg = _friendly_error(Exception(raw_error), pkey, pmodel)
    st.error(friendly_msg)
    if st.session_state.index_log:
        with st.expander("Technical details (for debugging)", expanded=False):
            st.markdown(
                f'<div class="progress-box">{"<br>".join(st.session_state.index_log[-20:])}</div>',
                unsafe_allow_html=True,
            )
    if st.button("Try again", type="primary"):
        st.session_state.index_status = "idle"
        st.rerun()

# ── Completion log ────────────────────────────────────────────────────────────
if st.session_state.index_status == "done":
    if st.session_state.index_log:
        with st.expander("Indexing log", expanded=False):
            st.markdown(
                f'<div class="progress-box">{"<br>".join(st.session_state.index_log)}</div>',
                unsafe_allow_html=True,
            )
    # Reset index status so user can upload more
    st.session_state.index_status = "idle"

# ── Chat ──────────────────────────────────────────────────────────────────────
if st.session_state.active_doc_ids:
    st.divider()

    # Show which docs are active
    active_names = []
    for did in st.session_state.active_doc_ids:
        doc = st.session_state.loaded_docs.get(did)
        if doc:
            active_names.append(doc["name"])
    if active_names:
        st.markdown(f"### Ask about: {', '.join(active_names)}")
    else:
        st.markdown("### Ask a question")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    question = st.chat_input("Ask anything about your documents…")

    if question:
        if st.session_state.provider_obj is None:
            st.warning("**Provider not configured.** Select a provider, enter your API key, and click **Apply settings** in the sidebar.")
        else:
            # Save user message
            st.session_state.messages.append({"role": "user", "content": question})
            _save_message("user", question)

            with st.chat_message("user"):
                st.write(question)

            with st.chat_message("assistant"):
                with st.spinner("Searching documents…"):
                    start_t = time.time()
                    try:
                        # Gather all active document data
                        doc_data_list = []
                        for did in st.session_state.active_doc_ids:
                            doc = st.session_state.loaded_docs.get(did)
                            if not doc:
                                doc = _load_document_data(did)
                            if doc:
                                doc_data_list.append(doc)

                        if not doc_data_list:
                            answer = "No documents loaded. Select documents in the sidebar."
                        else:
                            answer = _run_rag_multi(
                                question,
                                doc_data_list,
                                st.session_state.provider_obj,
                                st.session_state.messages[:-1],  # history without current Q
                            )
                    except Exception as exc:
                        pkey = st.session_state.get("provider_key", "")
                        pmodel = st.session_state.get("provider_model", "")
                        answer = _friendly_error(exc, pkey, pmodel)

                    latency = int((time.time() - start_t) * 1000)

                st.write(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})
                _save_message("assistant", answer, latency_ms=latency)

elif st.session_state.index_status == "idle":
    st.markdown("""
**Get started:**
1. Configure your LLM provider in the sidebar → **Apply settings**
2. Upload a PDF above → **Index document**
3. Select indexed documents in the sidebar (✅)
4. Ask questions across all selected documents
""")
