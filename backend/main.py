"""
main.py — FastAPI application for Doc Agent.

Serves REST endpoints for document management, RAG chat, provider configuration,
and conversation history. Uses Supabase for persistence and Google OAuth via JWT.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.auth import verify_token
from backend.routes import documents, chat, providers, conversations, collections

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── FastAPI app ──────────────────────────────────────────────────────────────
_is_production = os.environ.get("ENV", "").lower() == "production" or bool(os.environ.get("SUPABASE_SERVICE_KEY"))

app = FastAPI(
    title="Doc Agent API",
    version="1.0.0",
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)

# CORS — allow the Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        os.environ.get("FRONTEND_URL", "http://localhost:3000"),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── App state ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # Supabase client (optional)
    sb = None
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if url and key:
        try:
            from supabase import create_client
            sb = create_client(url, key)
            logger.info("Supabase connected: %s", url)
        except Exception as e:
            logger.warning("Supabase connection failed: %s", e)

    app.state.supabase = sb
    # Server-side session store: {user_id: {provider_obj, provider_key, ...}}
    app.state.sessions = {}


# ── JWT auth middleware ────────────────────────────────────────────────────────
# Routes that don't require authentication
_PUBLIC_PATHS = {"/api/health"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Skip auth for public endpoints
    if path in _PUBLIC_PATHS:
        request.state.user_id = None
        request.state.email = None
        request.state.role = "user"
        return await call_next(request)

    # Extract Bearer token
    auth_header = request.headers.get("authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""

    if not token:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required. Please sign in."},
        )

    # Validate JWT
    auth_data = verify_token(token)
    if not auth_data:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or expired token. Please sign in again."},
        )

    request.state.user_id = auth_data["user_id"]
    request.state.email = auth_data["email"]
    request.state.role = auth_data["role"]

    response: Response = await call_next(request)
    return response


# ── Routes ───────────────────────────────────────────────────────────────────
app.include_router(providers.router)
app.include_router(collections.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(conversations.router)


@app.get("/api/health")
async def health():
    sb = getattr(app.state, "supabase", None)
    return {
        "status": "ok",
        "supabase": "connected" if sb else "not configured",
    }
