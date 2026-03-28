"""
collections.py — Collection listing and collection-scoped document queries.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collections", tags=["collections"])

# Column list shared by document queries
_DOC_COLUMNS = (
    "id, name, page_count, total_tokens, status, provider_used, "
    "model_used, indexing_duration_ms, created_at, indexed_at, "
    "collection_id, is_global"
)


@router.get("")
async def list_collections(request: Request):
    """Return all collections with document counts."""
    sb = request.app.state.supabase
    if not sb:
        return []

    user_id = request.state.user_id

    try:
        # Fetch all collections
        result = sb.table("collections").select("*").order("created_at").execute()
        collections = result.data or []

        # Enrich each collection with its doc count
        for coll in collections:
            coll_id = coll["id"]
            query = (
                sb.table("documents")
                .select("id", count="exact")
                .eq("collection_id", coll_id)
                .eq("status", "indexed")
            )
            # For user_uploads, only count the current user's docs
            if not coll.get("is_global"):
                query = query.eq("user_id", user_id)

            count_result = query.execute()
            coll["doc_count"] = count_result.count if count_result.count is not None else 0

        # Add can_upload flag based on role
        user_role = getattr(request.state, "role", "user")
        for coll in collections:
            if coll.get("is_global"):
                coll["can_upload"] = user_role == "admin"
            else:
                coll["can_upload"] = True

        return collections

    except Exception as e:
        logger.exception("Failed to load collections")
        raise HTTPException(status_code=500, detail="Failed to load collections")


@router.get("/{collection_id}/documents")
async def list_collection_documents(collection_id: str, request: Request):
    """List indexed documents in a collection."""
    sb = request.app.state.supabase
    if not sb:
        return []

    user_id = request.state.user_id

    try:
        # Verify collection exists
        coll_result = (
            sb.table("collections")
            .select("id, is_global")
            .eq("id", collection_id)
            .single()
            .execute()
        )
        if not coll_result.data:
            raise HTTPException(status_code=404, detail=f"Collection '{collection_id}' not found")

        is_global = coll_result.data.get("is_global", False)

        # Query documents
        query = (
            sb.table("documents")
            .select(_DOC_COLUMNS)
            .eq("collection_id", collection_id)
            .eq("status", "indexed")
            .order("created_at", desc=True)
        )

        # For non-global collections, scope to current user
        if not is_global:
            query = query.eq("user_id", user_id)

        result = query.execute()
        return result.data or []

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to load collection documents for %s", collection_id)
        raise HTTPException(status_code=500, detail="Failed to load collection documents")
