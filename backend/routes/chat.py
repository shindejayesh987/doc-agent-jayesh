"""
chat.py — RAG Q&A endpoint.
"""
import asyncio
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from backend.services.rag import run_rag_multi
from backend.routes.providers import friendly_error

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    doc_ids: List[str]
    conversation_id: Optional[str] = None


@router.post("")
async def chat(body: ChatRequest, request: Request):
    """Run RAG pipeline and return an answer."""
    sb = request.app.state.supabase
    user_id = request.state.user_id
    sessions = request.app.state.sessions
    user_session = sessions.get(user_id, {})

    provider_obj = user_session.get("provider_obj")
    if not provider_obj:
        raise HTTPException(status_code=400, detail="No provider configured. Connect a provider first.")

    if not body.doc_ids:
        raise HTTPException(status_code=400, detail="No documents selected.")

    # Load document data for each doc_id
    loaded_docs = user_session.get("loaded_docs", {})
    doc_data_list = []
    for doc_id in body.doc_ids:
        if doc_id in loaded_docs:
            doc_data_list.append(loaded_docs[doc_id])
            continue
        # Try loading from Supabase (only user's own docs or global docs)
        if sb:
            try:
                result = (
                    sb.table("documents")
                    .select("name, tree_json, pages_json")
                    .eq("id", doc_id)
                    .eq("status", "indexed")
                    .or_(f"is_global.eq.true,user_id.eq.{user_id}")
                    .single()
                    .execute()
                )
                if result.data and result.data.get("tree_json"):
                    doc_data = {
                        "tree": result.data["tree_json"],
                        "pages": result.data["pages_json"],
                        "name": result.data["name"],
                    }
                    sessions.setdefault(user_id, {})
                    sessions[user_id].setdefault("loaded_docs", {})[doc_id] = doc_data
                    doc_data_list.append(doc_data)
                    continue
            except Exception:
                pass

    if not doc_data_list:
        raise HTTPException(status_code=400, detail="No indexed documents found for the given IDs.")

    # Extract the latest user message as the query
    query = ""
    history = []
    for msg in body.messages:
        if msg.role == "user":
            query = msg.content
        history.append({"role": msg.role, "content": msg.content})

    if not query:
        raise HTTPException(status_code=400, detail="No user message found.")

    provider_key = user_session.get("provider_key", "")
    provider_model = user_session.get("provider_model", "")
    lexical_grounding_score = 0.0

    start_t = time.time()
    try:
        result = await run_rag_multi(query, doc_data_list, provider_obj, history[:-1])
        answer = result["answer"]
        lexical_grounding_score = result["lexical_grounding_score"]
        retrieval_telemetry = result.get("retrieval_telemetry", [])
        logger.info(
            "Chat telemetry user=%s docs=%d grounding=%0.3f retrieval=%s",
            user_id[:8],
            len(doc_data_list),
            lexical_grounding_score,
            retrieval_telemetry,
        )
    except Exception as exc:
        answer = friendly_error(exc, provider_key, provider_model)

    latency_ms = int((time.time() - start_t) * 1000)

    # Save messages to Supabase (only if conversation belongs to this user)
    conv_id = body.conversation_id
    if sb and conv_id:
        try:
            owner_check = (
                sb.table("conversations")
                .select("id")
                .eq("id", conv_id)
                .eq("user_id", user_id)
                .execute()
            )
            if owner_check.data:
                sb.table("messages").insert({
                    "conversation_id": conv_id,
                    "role": "user",
                    "content": query,
                    "sources": [],
                }).execute()
                sb.table("messages").insert({
                    "conversation_id": conv_id,
                    "role": "assistant",
                    "content": answer,
                    "sources": [],
                    "model_used": provider_model,
                    "latency_ms": latency_ms,
                }).execute()
        except Exception:
            pass

    return {
        "role": "assistant",
        "content": answer,
        "lexical_grounding_score": lexical_grounding_score,
        "latency_ms": latency_ms,
    }
