"""
documents.py — Document upload, listing, loading, deletion, and indexing progress SSE.
"""
import asyncio
import json
import logging
import time
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile, File
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

from backend.services.indexing import (
    run_indexing, get_progress, get_job_queue, get_all_active_jobs,
)
from backend.routes.providers import PROVIDERS, friendly_error

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("")
async def list_documents(
    request: Request,
    collection_id: Optional[str] = Query(None),
):
    """List documents for the current user (own docs + global docs)."""
    sb = request.app.state.supabase
    if not sb:
        return []

    user_id = request.state.user_id

    try:
        columns = (
            "id, name, page_count, total_tokens, status, provider_used, "
            "model_used, indexing_duration_ms, created_at, indexed_at, "
            "collection_id, is_global"
        )
        query = sb.table("documents").select(columns)

        if collection_id:
            query = query.eq("collection_id", collection_id)

        # Return user's own docs + all global docs
        # Use or_ filter: is_global=true OR user_id=current_user
        query = query.or_(f"is_global.eq.true,user_id.eq.{user_id}")
        result = query.order("created_at", desc=True).execute()
        return result.data
    except Exception as e:
        logger.exception("Failed to load documents for user %s", user_id[:8])
        raise HTTPException(status_code=500, detail="Failed to load documents")


@router.get("/indexing-progress/{doc_id}")
async def indexing_progress(doc_id: str, request: Request):
    """SSE endpoint streaming indexing progress for a document owned by the current user."""
    # Verify the document belongs to this user or is global
    sb = request.app.state.supabase
    user_id = request.state.user_id
    if sb:
        owner_check = (
            sb.table("documents")
            .select("id")
            .eq("id", doc_id)
            .or_(f"is_global.eq.true,user_id.eq.{user_id}")
            .execute()
        )
        if not owner_check.data:
            async def denied_stream():
                yield {"event": "error", "data": json.dumps({"error": "Document not found"})}
            return EventSourceResponse(denied_stream())

    async def event_stream():
        q = get_job_queue(doc_id)
        if not q:
            yield {"event": "error", "data": json.dumps({"error": "No active indexing job for this document"})}
            return

        log_lines = []
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield {"event": "heartbeat", "data": "ping"}
                continue

            kind = msg[0]
            if kind == "log":
                log_lines.append(msg[1])
                pct, label = get_progress(log_lines)
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "percentage": pct,
                        "step": label,
                        "log": msg[1],
                    }),
                }
            elif kind == "done":
                yield {
                    "event": "done",
                    "data": json.dumps({"status": "indexed"}),
                }
                return
            elif kind == "error":
                yield {
                    "event": "error",
                    "data": json.dumps({"error": msg[1]}),
                }
                return

    return EventSourceResponse(event_stream())


@router.get("/{doc_id}")
async def get_document(doc_id: str, request: Request):
    """Load tree_json and pages_json for a document."""
    sb = request.app.state.supabase
    if not sb:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Check in-memory cache first
    sessions = request.app.state.sessions
    user_id = request.state.user_id
    user_session = sessions.get(user_id, {})
    loaded_docs = user_session.get("loaded_docs", {})

    if doc_id in loaded_docs:
        return loaded_docs[doc_id]

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
            # Cache in session
            sessions.setdefault(user_id, {})
            sessions[user_id].setdefault("loaded_docs", {})[doc_id] = doc_data
            return doc_data
        raise HTTPException(status_code=404, detail="Document not found or not indexed")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to load document %s", doc_id[:8])
        raise HTTPException(status_code=500, detail="Failed to load document")


@router.post("/upload")
async def upload_documents(
    request: Request,
    files: List[UploadFile] = File(...),
    collection_id: str = Form("user_uploads"),
):
    """Upload one or more PDFs, create Supabase records, start background indexing."""
    sb = request.app.state.supabase
    user_id = request.state.user_id
    sessions = request.app.state.sessions
    user_session = sessions.get(user_id, {})

    provider_obj = user_session.get("provider_obj")
    if not provider_obj:
        raise HTTPException(status_code=400, detail="No provider configured. Connect a provider first.")

    provider_key = user_session.get("provider_key", "gemini")
    provider_cfg = PROVIDERS.get(provider_key, PROVIDERS["gemini"])

    # Only admins can upload to global collections.
    # Regular users are forced to user_uploads.
    _GLOBAL_COLLECTIONS = {"curam_web_client", "curam_web_server"}
    user_role = getattr(request.state, "role", "user")
    if collection_id in _GLOBAL_COLLECTIONS and user_role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can upload to shared collections")
    is_global = collection_id in _GLOBAL_COLLECTIONS

    doc_ids = []
    for file in files:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"Only PDF files are accepted: {file.filename}")

        pdf_bytes = await file.read()
        doc_id = str(uuid4())

        # Create Supabase record
        if sb:
            try:
                sb.table("documents").insert({
                    "id": doc_id,
                    "user_id": user_id,
                    "name": file.filename,
                    "file_size_bytes": len(pdf_bytes),
                    "status": "uploaded",
                    "provider_used": provider_key,
                    "model_used": user_session.get("provider_model", ""),
                    "collection_id": collection_id,
                    "is_global": is_global,
                }).execute()
            except Exception as e:
                logger.exception("Failed to create document record")
                raise HTTPException(status_code=500, detail="Failed to create document record")

        # Start background indexing
        asyncio.create_task(
            run_indexing(pdf_bytes, provider_obj, provider_cfg, doc_id, sb)
        )
        doc_ids.append({"doc_id": doc_id, "name": file.filename})

    return {"documents": doc_ids}


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    """Delete a user's own document from Supabase. Global docs cannot be deleted."""
    sb = request.app.state.supabase
    if not sb:
        raise HTTPException(status_code=500, detail="Database not configured")

    user_id = request.state.user_id

    user_role = getattr(request.state, "role", "user")

    try:
        # Check ownership — owner or admin can delete
        check = (
            sb.table("documents")
            .select("id, user_id, is_global")
            .eq("id", doc_id)
            .single()
            .execute()
        )
        if not check.data:
            raise HTTPException(status_code=404, detail="Document not found")
        is_owner = check.data.get("user_id") == user_id
        is_admin = user_role == "admin"
        if not is_owner and not is_admin:
            raise HTTPException(status_code=403, detail="Cannot delete another user's document")

        sb.table("documents").delete().eq("id", doc_id).execute()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete document %s", doc_id[:8])
        raise HTTPException(status_code=500, detail="Failed to delete document")

    # Remove from session cache
    sessions = request.app.state.sessions
    user_session = sessions.get(user_id, {})
    user_session.get("loaded_docs", {}).pop(doc_id, None)

    return {"status": "deleted"}
