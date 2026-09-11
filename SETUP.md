# Setup & Server

---

## View existing results (no credentials needed)

The repo includes 936 PDFs and all benchmark run results. To browse everything locally on a fresh Mac:

```bash
# 1. Homebrew (skip if already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"

# 2. Python and Node
brew install python@3.12 node@20
echo 'export PATH="/opt/homebrew/opt/node@20/bin:$PATH"' >> ~/.zprofile
export PATH="/opt/homebrew/opt/node@20/bin:$PATH"

# 3. Clone and set up
git clone https://github.com/sachin-hg/doc-parser-construction-certificate
cd doc-parser-construction-certificate
bash scripts/install.sh

# Terminal 1 — viewer (default port 5173, override with VITE_PORT=XXXX)
cd viewer && npm run dev

# Terminal 2 — API server from project root (default port 8765, override with PORT=XXXX)
.venv/bin/python viewer_server.py
```

Open **http://localhost:5173** — all benchmark runs, extracted JSON, and source PDFs are available immediately.

---

## Full setup (to run new benchmarks)

### Prerequisites

- Python 3.9+
- Node.js 18+
- Google Cloud CLI (`gcloud`) — only needed for Vertex AI / batch mode with your own GCP account (not required if using shared credentials)

### 1. Install dependencies

```bash
bash scripts/install.sh
```

This script creates `.venv/`, installs `requirements.txt`, runs `npm install` inside `viewer/`, and writes a `.env` template.

### 2. Credentials

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

# ── GCP credentials file (optional — skip if using gcloud auth) ─────────────
# Point to a service account key or shared ADC file in the project root.
# GOOGLE_APPLICATION_CREDENTIALS=google_creds.json

# ── OpenAI (variants E and F) ───────────────────────────────────────────────
# OPENAI_API_KEY=your-openai-key

# ── GLM-OCR via z.ai (variant E only) ───────────────────────────────────────
# GLM_OCR_API_KEY=your-glm-ocr-key

# ── OpenRouter / Qwen3-VL (variant F only) ──────────────────────────────────
# OPENROUTER_API_KEY=your-openrouter-key
```

| Goal | Required keys |
|------|--------------|
| View existing results only | **none** |
| Run variants A–D (Gemini) | `GEMINI_PROJECT` **or** `GEMINI_API_KEY` |
| Run variant E (GLM-OCR + Luna) | `OPENAI_API_KEY` + `GLM_OCR_API_KEY` |
| Run variant F (Qwen3-VL + Luna) | `OPENAI_API_KEY` + `OPENROUTER_API_KEY` |
| Batch mode (any variant) | above + `GCS_BUCKET` |

#### How to get each key

**`GEMINI_PROJECT` (Vertex AI — recommended)**
1. Create a GCP project at console.cloud.google.com
2. Enable the **Vertex AI API**
3. Set `GEMINI_PROJECT` to your project ID
4. Authenticate: `gcloud auth application-default login`
5. Set quota project: `gcloud auth application-default set-quota-project YOUR_PROJECT`

**`GEMINI_API_KEY` (AI Studio — simpler)**
1. Go to aistudio.google.com → API Keys → Create a key

**`OPENAI_API_KEY`**
1. Go to platform.openai.com → API Keys → Create a new secret key

**`GLM_OCR_API_KEY`**
1. Register at bigmodel.cn or z.ai → create an API key

**`OPENROUTER_API_KEY`**
1. Go to openrouter.ai → Keys → Create a key

### 3. GCP authentication (Vertex AI only)

**Option A — your own GCP account**

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project your-gcp-project-id
```

Then create your own GCS bucket for batch mode:

```bash
gcloud storage buckets create gs://your-bucket-name \
  --project=your-gcp-project-id \
  --location=us-central1
```

**Option B — shared credentials (colleague gave you a credentials file)**

Drop the file in the project root as `google_creds.json`, then uncomment in `.env`:

```bash
GOOGLE_APPLICATION_CREDENTIALS=google_creds.json
```

No `gcloud` commands needed. The shared credentials can be either:
- A **service account key** JSON (scoped to specific roles — preferred for sharing)
- An **ADC file** (`~/.config/gcloud/application_default_credentials.json` from the owner's machine — grants the owner's full GCP access, so only share with trusted collaborators)

To create a scoped service account key to share with others:

```bash
# Create service account
gcloud iam service-accounts create rera-benchmark-sa \
  --display-name="RERA Benchmark" \
  --project=your-gcp-project-id

# Grant Vertex AI access
gcloud projects add-iam-policy-binding your-gcp-project-id \
  --member="serviceAccount:rera-benchmark-sa@your-gcp-project-id.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Grant GCS access on the bucket
gcloud storage buckets add-iam-policy-binding gs://your-bucket-name \
  --member="serviceAccount:rera-benchmark-sa@your-gcp-project-id.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Download the key — share this file as google_creds.json
gcloud iam service-accounts keys create google_creds.json \
  --iam-account=rera-benchmark-sa@your-gcp-project-id.iam.gserviceaccount.com
```

`google_creds.json` is in `.gitignore` and will never be committed.

### 4. Download additional PDFs (optional)

The repo includes PDFs for all successfully extracted documents (936 files, ~820 MB).
To download the remaining ~2,100 docs that errored or were never run:

```bash
# Download all docs not yet in the repo (~1.1 GB more)
.venv/bin/python scripts/download_docs.py --all

# Filter by state
.venv/bin/python scripts/download_docs.py --state UP

# See what would be downloaded
.venv/bin/python scripts/download_docs.py --dry-run
```

---

## Start the viewer

**Terminal 1** — React dev server:

```bash
cd viewer && npm run dev    # http://localhost:5173
```

**Terminal 2** — API server (must run from project root):

```bash
.venv/bin/python viewer_server.py
```

The Vite dev server proxies all `/api` requests to port 8765 automatically.

### Override ports

If the default ports are already in use, override them with env vars:

```bash
# API server on a different port
PORT=8766 .venv/bin/python viewer_server.py

# Viewer on a different port, pointing at the API on 8766
cd viewer && VITE_PORT=5174 VITE_API_PORT=8766 npx vite
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

| Variant | Model | Approach |
|---------|-------|----------|
| A | Gemini 2.5 Flash | 2-call: extract MD → JSON |
| B | Gemini 2.5 Flash | single-call: raw PDF → JSON |
| C | Gemini 2.5 Flash-Lite | 2-call |
| D | Gemini 2.5 Flash-Lite | single-call |
| E | GLM-OCR + GPT-5.6 Luna | 2-call |
| F | Qwen3-VL-32B + GPT-5.6 Luna | 2-call |

### State codes

`GJ` Gujarat · `HR` Haryana · `MH` Maharashtra · `UP` Uttar Pradesh · `TS` Telangana

---

## Re-generate markdown extractions (optional)

Only needed to re-run variant A or C (2-call approach). Already present for 471 docs in git.

```bash
.venv/bin/python scripts/extract_text_pdfs.py    # PyMuPDF, fast
.venv/bin/python scripts/extract_docling.py      # higher quality, slower
```

---

## What's in git

| Path | In git? | Notes |
|------|---------|-------|
| `scripts/`, `pipeline/`, `viewer/src/` | ✅ | All source code |
| `output/benchmark_results/runs/` | ✅ | Run metadata + JSON extraction results |
| `documents/` (936 files) | ✅ | PDFs for all successfully extracted docs |
| `output/extracted_md/` (471 files) | ✅ | Markdown extractions for 2-call variants |
| `output/manifest.json` | ✅ | Download URLs for all 3,087 docs |
| `.env` | ❌ | Credentials — never committed |
| `.venv/`, `viewer/node_modules/` | ❌ | Installed dependencies |
| Remaining ~2,100 PDFs | ❌ | Download with `scripts/download_docs.py` |
