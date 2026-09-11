#!/usr/bin/env bash
# Install all dependencies for the doc-parser-construction-certificate project.
# Run from the project root: bash scripts/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Project root: $ROOT ==="

# ── 1. Python virtual environment ───────────────────────────────────────────
echo ""
echo "=== 1/3 Python virtual environment ==="

PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" --version 2>&1 | awk '{print $2}')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
            PYTHON="$candidate"
            echo "Using $candidate ($version)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.9+ not found. Install it from https://python.org or your system package manager."
    exit 1
fi

if [ -d ".venv" ]; then
    echo ".venv already exists — skipping creation"
else
    "$PYTHON" -m venv .venv
    echo "Created .venv"
fi

# Activate
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "Python dependencies installed."

# ── 2. Node / npm ────────────────────────────────────────────────────────────
echo ""
echo "=== 2/3 Node dependencies ==="

if ! command -v node &>/dev/null; then
    echo "ERROR: Node.js not found. Install Node 18+ from https://nodejs.org"
    exit 1
fi

NODE_VER=$(node --version | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VER" -lt 18 ]; then
    echo "WARNING: Node $(node --version) found but Node 18+ is recommended."
fi

echo "Installing viewer npm dependencies..."
cd viewer
npm install --silent
cd ..
echo "npm install done."

# ── 3. Environment file ───────────────────────────────────────────────────────
echo ""
echo "=== 3/3 Environment variables ==="

if [ -f ".env" ]; then
    echo ".env already exists — skipping template creation."
else
    cat > .env << 'ENV'
# ── Gemini / Vertex AI (required for variants A–D) ──────────────────────────
# Option A: Vertex AI (recommended — free tier available)
GEMINI_PROJECT=your-gcp-project-id
GEMINI_LOCATION=us-central1
GOOGLE_CLOUD_QUOTA_PROJECT=your-gcp-project-id

# Option B: AI Studio (simple API key, no GCP needed)
# GEMINI_API_KEY=your-ai-studio-key

# ── GCS bucket (batch mode only) ─────────────────────────────────────────────
GCS_BUCKET=rera-benchmark-pdfs

# ── GCP credentials file (optional — skip if using gcloud auth) ──────────────
# Drop a service account key or ADC file in the project root as google_creds.json
# GOOGLE_APPLICATION_CREDENTIALS=google_creds.json

# ── Batch backend (optional) ─────────────────────────────────────────────────
# vertex   → Vertex AI + GCS JSONL (default for 2-call variants A/C when GEMINI_PROJECT+GCS_BUCKET set)
# aistudio → AI Studio inline requests (uses AI Studio quota, not GCP credits)
# BATCH_BACKEND=vertex

# ── OpenAI (required for variants E and F) ───────────────────────────────────
# OPENAI_API_KEY=your-openai-key

# ── GLM-OCR via z.ai (required for variant E only) ───────────────────────────
# GLM_OCR_API_KEY=your-glm-ocr-key

# ── OpenRouter (required for variant F only) ─────────────────────────────────
# OPENROUTER_API_KEY=your-openrouter-key
ENV
    echo ".env template created — fill in your credentials before running benchmarks."
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your credentials (see docs/CREDENTIALS.md for details)"
if command -v gcloud &>/dev/null; then
    echo "  2. Authenticate with GCP: gcloud auth application-default login"
else
    echo "  2. Install gcloud CLI (if using Vertex AI): https://cloud.google.com/sdk/docs/install"
fi
echo "  3. Download run-referenced PDFs: .venv/bin/python scripts/download_docs.py"
echo "  4. Start the viewer:  cd viewer && npm run dev"
echo "  5. Start the API:     .venv/bin/python viewer_server.py"
echo "  6. Open:              http://localhost:5173"
