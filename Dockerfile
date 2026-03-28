# ============================================================================
# PageIndex — Docker image for FastAPI backend + Next.js frontend
# ============================================================================
# Multi-stage build:
#   1. Build frontend (Next.js)
#   2. Install Python deps
#   3. Runtime: serve both FastAPI and Next.js
# ============================================================================

# Stage 1: Build Next.js frontend
FROM node:20-slim AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .

ENV NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ENV NEXT_PUBLIC_SUPABASE_URL=${NEXT_PUBLIC_SUPABASE_URL}
ENV NEXT_PUBLIC_SUPABASE_ANON_KEY=${NEXT_PUBLIC_SUPABASE_ANON_KEY}
RUN npm run build

# Stage 2: Build Python dependencies
FROM python:3.11-slim AS backend-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 3: Runtime
FROM python:3.11-slim

# Install Node.js for Next.js server
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Python packages
COPY --from=backend-builder /install /usr/local

# Copy backend code
COPY backend/ backend/
COPY pageindex/ pageindex/
COPY storage/ storage/

# Copy built frontend
COPY --from=frontend-builder /frontend/.next frontend/.next
COPY --from=frontend-builder /frontend/package.json frontend/package.json
COPY --from=frontend-builder /frontend/next.config.ts frontend/next.config.ts
COPY --from=frontend-builder /frontend/node_modules frontend/node_modules

# Environment
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV NEXT_PUBLIC_API_URL=""

# Copy start script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

EXPOSE 8080
CMD ["/app/start.sh"]
