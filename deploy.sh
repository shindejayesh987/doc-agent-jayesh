#!/bin/bash
# ============================================================================
# PageIndex — Deploy to Google Cloud Run
# ============================================================================
# Prerequisites:
#   1. gcloud CLI installed: https://cloud.google.com/sdk/docs/install
#   2. Logged in: gcloud auth login
#   3. Project set: gcloud config set project YOUR_PROJECT_ID
#   4. Supabase project created with schema.sql applied
# ============================================================================

set -e

# ── Configuration ────────────────────────────────────────────────────────────
PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
SERVICE_NAME="pageindex"
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "╔════════════════════════════════════════════╗"
echo "║  PageIndex — Cloud Run Deployment          ║"
echo "╠════════════════════════════════════════════╣"
echo "║  Project:  ${PROJECT_ID}"
echo "║  Region:   ${REGION}"
echo "║  Service:  ${SERVICE_NAME}"
echo "║  Image:    ${IMAGE_NAME}"
echo "╚════════════════════════════════════════════╝"
echo ""

# ── Step 1: Enable required APIs ─────────────────────────────────────────────
echo "→ Enabling Cloud Run and Container Registry APIs..."
gcloud services enable run.googleapis.com containerregistry.googleapis.com secretmanager.googleapis.com

# ── Step 2: Create secrets (first time only) ─────────────────────────────────
echo ""
echo "→ Setting up secrets..."

# Check if secrets exist, create if not
for SECRET in SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_KEY; do
    if ! gcloud secrets describe ${SECRET} --project=${PROJECT_ID} 2>/dev/null; then
        echo "  Creating secret: ${SECRET}"
        echo "  Enter value for ${SECRET}:"
        read -s SECRET_VALUE
        echo -n "${SECRET_VALUE}" | gcloud secrets create ${SECRET} --data-file=- --project=${PROJECT_ID}
        echo "  ✓ Created ${SECRET}"
    else
        echo "  ✓ Secret ${SECRET} already exists"
    fi
done

# ── Step 3: Build Docker image ───────────────────────────────────────────────
echo ""
echo "→ Building Docker image..."
docker build --platform linux/amd64 \
    --build-arg NEXT_PUBLIC_SUPABASE_URL=https://tqmcdgmpgogzvxcbpqys.supabase.co \
    --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRxbWNkZ21wZ29nenZ4Y2JwcXlzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQyMTE3NzgsImV4cCI6MjA4OTc4Nzc3OH0.Dyz5ztx6VvqG45dz1A4KEeysZjWg3z7rtBI7-bTN3CY \
    -t ${IMAGE_NAME} .

# ── Step 4: Push to Container Registry ───────────────────────────────────────
echo ""
echo "→ Pushing image to GCR..."
docker push ${IMAGE_NAME}

# ── Step 5: Deploy to Cloud Run ──────────────────────────────────────────────
echo ""
echo "→ Deploying to Cloud Run..."
gcloud run deploy ${SERVICE_NAME} \
    --image=${IMAGE_NAME} \
    --region=${REGION} \
    --platform=managed \
    --allow-unauthenticated \
    --port=8080 \
    --memory=2Gi \
    --cpu=2 \
    --timeout=3600 \
    --min-instances=0 \
    --max-instances=3 \
    --set-secrets="SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_ANON_KEY=SUPABASE_ANON_KEY:latest,SUPABASE_SERVICE_KEY=SUPABASE_SERVICE_KEY:latest,SUPABASE_JWT_SECRET=SUPABASE_JWT_SECRET:latest" \
    --set-env-vars="ADMIN_EMAILS=jay98shinde@gmail.com,kadirlofca@outlook.com"

# ── Step 6: Get URL ──────────────────────────────────────────────────────────
echo ""
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} --region=${REGION} --format="value(status.url)")
echo "╔════════════════════════════════════════════╗"
echo "║  ✓ Deployment complete!                    ║"
echo "║                                            ║"
echo "║  URL: ${SERVICE_URL}"
echo "║                                            ║"
echo "║  Cloud Run Console:                        ║"
echo "║  https://console.cloud.google.com/run      ║"
echo "╚════════════════════════════════════════════╝"
