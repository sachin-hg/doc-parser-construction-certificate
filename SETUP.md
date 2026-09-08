# Setup & Server

## Quick start (new machine after `git clone`)

```bash
bash scripts/install.sh            # create venv, install deps, generate .env template
# Edit .env with your credentials (see §Credentials below)
gcloud auth application-default login   # if using Vertex AI / Gemini
.venv/bin/python scripts/download_docs.py   # download PDFs for all benchmark runs (~2.4 GB)

# Terminal 1 — viewer dev server
cd viewer && npm run dev            # http://localhost:5173

# Terminal 2 — API server (run from project root)
.venv/bin/python viewer_server.py
```

Open **http://localhost:5173** — all existing run results and PDFs will be visible.

---

## Prerequisites

- Python 3.9+
- Node.js 18+ with npm
- Google Cloud CLI (`gcloud`) — only needed for Vertex AI / batch mode

---

## 1. Install dependencies

```bash
bash scripts/install.sh
```

This script:
- Creates `.venv/` with the first Python 3.9+ found on your `PATH`
- Runs `pip install -r requirements.txt`
- Runs `npm install` inside `viewer/`
- Writes a `.env` template if one doesn't exist

---

## 2. Credentials

Edit the `.env` file created by `install.sh`:

```bash
# ── Gemini / Vertex AI (variants A–D) ──────────────────────────────────────
# Option A — Vertex AI (recommended; free tier available via GCP)
GEMINI_PROJECT=your-gcp-project-id
GEMINI_LOCATION=us-central1
GOOGLE_CLOUD_QUOTA_PROJECT=your-gcp-project-id

# Option B — AI Studio key (no GCP account needed)
# GEMINI_API_KEY=your-ai-studio-key

# ── GCS bucket (batch mode only) ────────────────────────────────────────────
GCS_BUCKET=rera-benchmark-pdfs

# ── OpenAI (variants E and F) ───────────────────────────────────────────────
# OPENAI_API_KEY=your-openai-key

# ── GLM-OCR via z.ai (variant E only) ───────────────────────────────────────
# GLM_OCR_API_KEY=your-glm-ocr-key

# ── OpenRouter / Qwen3-VL (variant F only) ───────────────────────────────────
# OPENROUTER_API_KEY=your-openrouter-key
```

### Minimum required

| Goal | Required keys |
|------|--------------|
| View existing results only | none |
| Run variants A–D (Gemini) | `GEMINI_PROJECT` **or** `GEMINI_API_KEY` |
| Run variant E (GLM-OCR + Luna) | `OPENAI_API_KEY` + `GLM_OCR_API_KEY` |
| Run variant F (Qwen3-VL + Luna) | `OPENAI_API_KEY` + `OPENROUTER_API_KEY` |
| Batch mode (any variant) | above + `GCS_BUCKET` |

### How to get each key

**`GEMINI_PROJECT` (Vertex AI — recommended)**
1. Create a GCP project at console.cloud.google.com
2. Enable the **Vertex AI API**
3. Set `GEMINI_PROJECT` to your project ID
4. Authenticate: `gcloud auth application-default login`
5. Set quota project: `gcloud auth application-default set-quota-project YOUR_PROJECT`

**`GEMINI_API_KEY` (AI Studio — simpler)**
1. Go to aistudio.google.com → API Keys
2. Create a key and paste it as `GEMINI_API_KEY`

**`OPENAI_API_KEY`**
1. Go to platform.openai.com → API Keys
2. Create a new secret key

**`GLM_OCR_API_KEY`**
1. Register at bigmodel.cn or z.ai
2. Create an API key for the layout parsing endpoint

**`OPENROUTER_API_KEY`**
1. Go to openrouter.ai → Keys
2. Create a key (Qwen3-VL-32B is available free/cheap)

---

## 3. GCP authentication (Vertex AI only)

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project your-gcp-project-id
```

### GCS bucket (batch mode only)

```bash
gcloud storage buckets create gs://rera-benchmark-pdfs \
  --project=your-gcp-project-id \
  --location=us-central1
```

---

## 4. Download PDFs

The repository contains benchmark run results (JSON extractions) but not the original PDFs (2.4 GB).
Download URLs for all documents are stored in `output/manifest.json`.

```bash
# Download only docs referenced by existing benchmark runs (~2.4 GB)
.venv/bin/python scripts/download_docs.py

# Download everything in the manifest (~2.7 GB)
.venv/bin/python scripts/download_docs.py --all

# Filter by state
.venv/bin/python scripts/download_docs.py --state HR

# See what would be downloaded without fetching
.venv/bin/python scripts/download_docs.py --dry-run

# Parallel workers (default 5)
.venv/bin/python scripts/download_docs.py --workers 10
```

---

## 5. Start the viewer

**Terminal 1** — React dev server:

```bash
cd viewer && npm run dev    # http://localhost:5173
```

**Terminal 2** — API server (run from project root):

```bash
.venv/bin/python viewer_server.py
```

The Vite dev server proxies all `/api` requests to port 8765 automatically.

> **Important**: always start `viewer_server.py` from the project root, not from inside `viewer/`.

---

## 6. Re-generate markdown extractions (optional)

The benchmark pipeline stores its raw JSON results in the run directories.
Markdown extractions (`output/extracted_md/`) are only needed to re-run variant A or C (2-call approach).

```bash
# Extract text from PDFs (uses PyMuPDF)
.venv/bin/python scripts/extract_text_pdfs.py

# Extract with Docling (optional, higher quality but slower)
.venv/bin/python scripts/extract_docling.py
```

---

## Running a benchmark

```bash
# Async mode — concurrent Gemini calls (recommended)
.venv/bin/python scripts/benchmark.py run --variant B --state HR

# Batch mode — PDFs uploaded to GCS, processed via Vertex AI Batch API
.venv/bin/python scripts/benchmark.py run --variant B --state HR --mode batch --limit 20

# Fetch results for a running/paused batch job
.venv/bin/python scripts/benchmark.py fetch --run-id B_HR_20260814_120000
```

### Variants

| Variant | Model | Approach | PDF access |
|---------|-------|----------|------------|
| A | Gemini 2.5 Flash | 2-call: extract MD → JSON | via markdown |
| B | Gemini 2.5 Flash | single-call: raw PDF → JSON | direct upload |
| C | Gemini 2.5 Flash-Lite | 2-call | via markdown |
| D | Gemini 2.5 Flash-Lite | single-call | direct upload |
| E | GLM-OCR + GPT-5.6 Luna | 2-call | OCR → markdown → JSON |
| F | Qwen3-VL-32B + GPT-5.6 Luna | 2-call | vision → markdown → JSON |

### State codes

`GJ` Gujarat · `HR` Haryana · `MH` Maharashtra · `UP` Uttar Pradesh · `TS` Telangana

---

## What's in git / what's not

| Path | In git? | Notes |
|------|---------|-------|
| `scripts/`, `pipeline/`, `viewer/src/` | ✅ | All source code |
| `output/benchmark_results/runs/` | ✅ | Run metadata + JSON extraction results |
| `output/manifest.json` | ✅ | PDF download URLs |
| `output/validation_results.json` | ✅ | Document metadata |
| `documents/` | ❌ | PDFs — download with `scripts/download_docs.py` |
| `output/extracted_md/` | ❌ | Markdown extractions — regenerate if needed |
| `.env` | ❌ | Credentials — never committed |
| `.venv/`, `viewer/node_modules/` | ❌ | Installed dependencies |
