#!/usr/bin/env python3
"""
Benchmark RERA construction certificate extraction across model variants.

Variants:
  A — Gemini 2.5 Flash,      2-call  (pymupdf4llm markdown → JSON)
  B — Gemini 2.5 Flash,      single  (raw PDF bytes → JSON)
  C — Gemini 2.5 Flash-Lite, 2-call
  D — Gemini 2.5 Flash-Lite, single
  E — GLM-OCR → GPT-5.6 Luna, 2-call  (GLM-OCR extracts text, Luna structures)
  F — Qwen3-VL-32B → GPT-5.6 Luna, 2-call  (Qwen VL reads PDF pages, Luna structures)

"2-call": local/specialized OCR runs first, then one LLM call converts text → JSON.
"single": one LLM call reads the raw PDF directly (OCR + structure combined).

Variants E and F require extra env vars.
  E: OPENAI_API_KEY, GLM_OCR_API_KEY  (z.ai hosted GLM-OCR REST API)
  F: OPENAI_API_KEY, OPENROUTER_API_KEY

Modes:
  async — concurrent API calls, semaphore-limited; measures per-doc latency.
  batch — Batch API (~50% cheaper on structure step); polls up to N times then exits.
          Gemini Batch API for A–D; OpenAI Batch API for E–F (OCR still runs async first).
          Use the `fetch` subcommand to retrieve results later.

Subcommands:
  run    Run extraction (async or batch).
  fetch  Check a batch job's status and download results when done.

Examples:
  python scripts/benchmark.py run --variant A --state GJ
  python scripts/benchmark.py run --variant A --state GJ --mode batch
  python scripts/benchmark.py run --variant E --state GJ --limit 20
  python scripts/benchmark.py run --variant F --state GJ --subset path/to/candidates.json
  python scripts/benchmark.py fetch --run-id A_GJ_20260806_120000

Every run (async or batch) creates a directory:
  output/benchmark_results/runs/{run_id}/
    run.json          ← manifest (mode, variant, chunk list + status)
    chunk_N.jsonl     ← results for completed chunk N
    chunk_N.json      ← pending job info for chunk N (batch mode only)
    results.jsonl     ← final merged results (written when all chunks done)
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed as futures_as_completed
from pathlib import Path
from typing import Optional

# Load .env from project root (only sets vars not already in the environment)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).parent))

from prompt_registry import Prompts, get_prompts, CURRENT_VERSION  # noqa: E402

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent.parent
DOCS_DIR = ROOT / "documents"
MD_DIR   = ROOT / "output" / "extracted_md"
VAL_PATH = ROOT / "output" / "validation_results.json"
OUT_DIR  = ROOT / "output" / "benchmark_results"
RUN_DIR  = OUT_DIR / "runs"   # every run (async or batch) gets a subdirectory here

# ── Variant config ────────────────────────────────────────────────────────────
# Gemini model IDs: https://ai.google.dev/gemini-api/docs/models
# GPT-5.6 Luna: https://platform.openai.com/docs/models
# Qwen3-VL-32B via OpenRouter: https://openrouter.ai/qwen/qwen3-vl-32b-instruct

VARIANT_MODEL = {
    "A": "gemini-2.5-flash",
    "B": "gemini-2.5-flash",
    "C": "gemini-2.5-flash-lite",
    "D": "gemini-2.5-flash-lite",
    "E": "gpt-5.6-luna",                    # structure step; OCR by GLM-OCR
    "F": "gpt-5.6-luna",                    # structure step; OCR by Qwen3-VL
}

VARIANT_OCR_MODEL = {                       # only for variants with a separate OCR step
    "E": "glm-ocr",
    "F": "qwen/qwen3-vl-32b-instruct",
}

VARIANT_CALL = {
    "A": "2-call",
    "B": "single",
    "C": "2-call",
    "D": "single",
    "E": "glmocr+gpt-luna",
    "F": "qwen-vl+gpt-luna",
}

GEMINI_VARIANTS  = {"A", "B", "C", "D"}

# ── Gemini client factory ─────────────────────────────────────────────────────
# Priority:
#   1. GEMINI_PROJECT set → Vertex AI (uses Application Default Credentials)
#   2. GEMINI_API_KEY set → AI Studio public endpoint
# GEMINI_LOCATION defaults to "asia-south2" when using Vertex AI.

def _make_gemini_client() -> "genai.Client":
    """Inference client — uses Vertex AI if GEMINI_PROJECT is set, else AI Studio."""
    project  = os.environ.get("GEMINI_PROJECT")
    location = os.environ.get("GEMINI_LOCATION", "us-central1")
    api_key  = os.environ.get("GEMINI_API_KEY")
    if project:
        return genai.Client(vertexai=True, project=project, location=location)
    if api_key:
        return genai.Client(api_key=api_key)
    sys.exit(
        "Error: set GEMINI_PROJECT (Vertex AI) or GEMINI_API_KEY (AI Studio) "
        "before running Gemini variants."
    )


def _make_gemini_devapi_client() -> "genai.Client":
    """AI Studio client for Files API and Batch API — Vertex AI does not support these.
    Always uses GEMINI_API_KEY regardless of whether GEMINI_PROJECT is set."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key)
    sys.exit(
        "Error: GEMINI_API_KEY is required for batch mode (Files API and Batch API "
        "are not supported on Vertex AI)."
    )


def _gemini_configured() -> bool:
    return bool(os.environ.get("GEMINI_PROJECT") or os.environ.get("GEMINI_API_KEY"))
OPENAI_VARIANTS  = {"E", "F"}

# $/1M tokens — update when pricing changes
# Gemini: https://ai.google.dev/pricing
# OpenAI: https://platform.openai.com/docs/pricing
# OpenRouter (Qwen): https://openrouter.ai/qwen/qwen3-vl-32b-instruct
PRICE = {
    "gemini-2.5-flash": {
        "input":        0.15,
        "input_cached": 0.0375,
        "output":       0.60,
    },
    "gemini-2.5-flash-lite": {
        "input":        0.075,
        "input_cached": 0.01875,
        "output":       0.30,
    },
    "gpt-5.6-luna": {
        "input":        0.20,
        "input_cached": 0.10,   # 50% of input (OpenAI prompt caching discount)
        "output":       1.20,
    },
    "qwen/qwen3-vl-32b-instruct": {
        "input":        0.104,
        "input_cached": 0.052,  # 50% of input (estimate)
        "output":       0.416,
    },
    # GLM-OCR is open-source/self-hosted — cost tracked as 0
    "glm-ocr": {"input": 0.0, "input_cached": 0.0, "output": 0.0},
}
BATCH_DISCOUNT = 0.5  # Gemini batch API is ~50% cheaper

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GLM_OCR_API_URL     = "https://api.z.ai/api/paas/v4/layout_parsing"

# Working batch chunk sizes (conservative, for reliability — not the hard provider limits)
#
# Gemini Batch API — binding constraint is 20 MB *total inline payload* per job
#   (Google docs: "suitable for smaller batches that keep the total request size under 20MB")
#   - TEXT batches (A/C, 2-call): markdown embedded inline, ~100–200 KB/doc.
#     50 docs × 200 KB ≈ 10 MB — leaves comfortable headroom under the 20 MB ceiling.
#   - URI batches (B/D, single-call): PDFs pre-uploaded to Files API; each InlinedRequest
#     carries only a URI string + text prompt (a few KB). 500 is safe.
# OpenAI Batch API — hard limits: 50,000 requests, 100–200 MB JSONL file.
#   Structure requests (E/F) are text-only, ~5–20 KB each. 200 per batch is fine.
GEMINI_BATCH_MAX_TEXT = 50    # inline markdown — size-sensitive (20 MB payload cap)
GEMINI_BATCH_MAX_URI  = 500   # file URI refs only — effectively size-free
OPENAI_BATCH_MAX      = 200   # text-only JSONL — well under 100 MB file limit

# Minimum markdown tables to consider extraction valid.
# If pre-extracted markdown has fewer tables than this, we fall back to the raw PDF.
MIN_TABLES_IN_MD = 2


# ── Doc loading ───────────────────────────────────────────────────────────────

# Accepts both short codes and full names (case-insensitive)
STATE_ALIASES: dict = {
    "GJ": "Gujarat",
    "MH": "Maharashtra",
    "HR": "Haryana",
    "UP": "Uttar Pradesh",
    "TS": "Telangana",
    "TG": "Telangana",
    "KA": "Karnataka",
    "TN": "Tamil Nadu",
    "WB": "West Bengal",
    "RJ": "Rajasthan",
    "MP": "Madhya Pradesh",
}


def load_docs(
    state: str,
    subset_path: Optional[str],
    limit: Optional[int],
) -> list:
    with open(VAL_PATH) as f:
        results = json.load(f)["results"]

    # Accept both short code (GJ) and full name (Gujarat)
    state_full = STATE_ALIASES.get(state.upper(), state)
    accepted = {state.upper(), state_full.upper()}

    docs = []
    for r in sorted(results, key=lambda x: x["local_path"]):
        parts = Path(r["local_path"]).parts
        doc_state = parts[1] if len(parts) > 1 else ""
        if doc_state.upper() not in accepted:
            continue
        rel = r["local_path"]
        md_path  = MD_DIR   / (rel + ".extracted.md")
        pdf_path = DOCS_DIR / rel
        docs.append({
            "local_path": rel,
            "rera_id":    r.get("rera_reg_id", Path(rel).stem),
            "pdf_path":   str(pdf_path),
            "md_path":    str(md_path),
            "has_md":     md_path.exists(),
            "has_pdf":    pdf_path.exists(),
        })

    if subset_path:
        with open(subset_path) as f:
            subset = json.load(f)
        if subset and isinstance(subset[0], dict):
            keep = {s["local_path"] for s in subset}
        else:
            keep = set(subset)
        docs = [d for d in docs if d["local_path"] in keep]

    if limit:
        docs = docs[:limit]

    return docs


# ── Shared helpers ────────────────────────────────────────────────────────────

def _count_tables(md: str) -> int:
    """Count markdown tables by counting header-separator rows (|---|)."""
    return len(re.findall(r"^\s*\|[-|: ]+\|\s*$", md, re.MULTILINE))


def _base_result(doc: dict, variant: str, model: str, call_type: str) -> dict:
    return {
        "local_path":     doc["local_path"],
        "rera_id":        doc["rera_id"],
        "variant":        variant,
        "model":          model,
        "call_type":      call_type,
        "has_md":         doc["has_md"],
        "pdf_fallback":   False,   # True when markdown had <2 tables and PDF was used instead
        "success":        False,
        "skipped":        False,
        "output":         None,
        "error":          None,
        "input_tokens":   None,   # structure-step tokens (or total for single-call)
        "output_tokens":  None,
        "cached_tokens":  None,
        "cost_usd":       None,   # total cost across all steps
        "cost_breakdown": None,   # populated for E/F (ocr_cost + struct_cost)
        "latency_s":      None,
    }


def _calc_cost_gemini(model: str, usage, batch: bool) -> float:
    p    = PRICE.get(model, {"input": 0.0, "input_cached": 0.0, "output": 0.0})
    disc = BATCH_DISCOUNT if batch else 1.0
    cached   = getattr(usage, "cached_content_token_count", 0) or 0
    total_in = getattr(usage, "prompt_token_count", 0) or 0
    uncached = max(0, total_in - cached)
    out      = getattr(usage, "candidates_token_count", 0) or 0
    return disc * (
        uncached * p["input"]        / 1_000_000
        + cached * p["input_cached"] / 1_000_000
        + out    * p["output"]       / 1_000_000
    )


def _calc_cost_openai(model: str, usage) -> float:
    """Cost from an openai Usage object (has .prompt_tokens, .completion_tokens)."""
    p        = PRICE.get(model, {"input": 0.0, "input_cached": 0.0, "output": 0.0})
    details  = getattr(usage, "prompt_tokens_details", None)
    cached   = getattr(details, "cached_tokens", 0) if details else 0
    total_in = getattr(usage, "prompt_tokens", 0) or 0
    uncached = max(0, total_in - cached)
    out      = getattr(usage, "completion_tokens", 0) or 0
    return (
        uncached * p["input"]        / 1_000_000
        + cached * p["input_cached"] / 1_000_000
        + out    * p["output"]       / 1_000_000
    )


def _calc_cost_glmocr(usage: dict) -> float:
    """Cost from a GLM-OCR API usage dict {prompt_tokens, completion_tokens}.
    Update PRICE['glm-ocr'] once z.ai publishes per-token rates."""
    p = PRICE.get("glm-ocr", {"input": 0.0, "output": 0.0})
    return (
        (usage.get("prompt_tokens", 0) or 0)     * p["input"]  / 1_000_000
        + (usage.get("completion_tokens", 0) or 0) * p["output"] / 1_000_000
    )


# ── Run tracking ──────────────────────────────────────────────────────────────
# Every invocation of `run` creates a run directory under RUN_DIR/{run_id}/.
# The manifest (run.json) records each batch chunk's status so `fetch --run-id`
# can resolve all pending chunks without the user tracking individual job IDs.

def _make_run_id(variant: str, state: str) -> str:
    return f"{variant}_{state}_{time.strftime('%Y%m%d_%H%M%S')}"


def _init_run(run_id: str, variant: str, state: str, mode: str, prompt_version: str = CURRENT_VERSION) -> Path:
    """Create run directory + initial run.json. Returns run_dir."""
    run_dir = RUN_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "run.json", "w") as f:
        json.dump({
            "run_id":          run_id,
            "variant":         variant,
            "state":           state,
            "mode":            mode,
            "prompt_version":  prompt_version,
            "created_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
            "chunks":          [],
            "skipped_results": [],
            "results_path":    None,
        }, f, indent=2)
    return run_dir


def _update_run_skipped(run_dir: Path, skipped_results: list) -> None:
    """Persist skipped_results (known after partitioning, before any batch submit)."""
    p = run_dir / "run.json"
    with open(p) as f:
        manifest = json.load(f)
    manifest["skipped_results"] = skipped_results
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)


def _register_chunk(
    run_dir: Path,
    chunk_idx: int,
    *,
    status: str,
    job_file: Optional[str]     = None,
    results_file: Optional[str] = None,
) -> None:
    """Upsert a chunk entry in run.json."""
    p = run_dir / "run.json"
    with open(p) as f:
        manifest = json.load(f)
    chunks = [c for c in manifest["chunks"] if c["idx"] != chunk_idx]
    entry  = {"idx": chunk_idx, "status": status}
    if job_file:
        entry["job_file"] = job_file
    if results_file:
        entry["results_file"] = results_file
    manifest["chunks"] = sorted(chunks + [entry], key=lambda c: c["idx"])
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)


def _save_chunk_results(run_dir: Path, chunk_idx: int, results: list) -> str:
    """Save chunk results to chunk_{idx}.jsonl. Returns filename."""
    filename = f"chunk_{chunk_idx}.jsonl"
    with open(run_dir / filename, "w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")
    return filename


def _mark_run_complete(run_dir: Path, results_file: str, **extra) -> None:
    p = run_dir / "run.json"
    with open(p) as f:
        manifest = json.load(f)
    manifest["results_path"] = results_file
    manifest.update(extra)
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)


def _save_chunk_job_file(
    run_dir: Path,
    chunk_idx: int,
    job_name: str,
    variant: str,
    model: str,
    call_type: str,
    state: str,
    active_docs: list,
    provider: str = "gemini",
    extra: Optional[dict] = None,
) -> str:
    """Persist a still-running batch job's metadata. Returns filename."""
    filename = f"chunk_{chunk_idx}.json"
    payload  = {
        "provider":     provider,
        "job_name":     job_name,
        "chunk_idx":    chunk_idx,
        "variant":      variant,
        "model":        model,
        "call_type":    call_type,
        "state":        state,
        "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "active_paths": [d["local_path"] for d in active_docs],
    }
    if extra:
        payload.update(extra)
    with open(run_dir / filename, "w") as f:
        json.dump(payload, f, indent=2)
    return filename


def _parse_gemini_response(response, result: dict, model: str, batch: bool) -> dict:
    usage = response.usage_metadata
    result["input_tokens"]  = getattr(usage, "prompt_token_count", None)
    result["output_tokens"] = getattr(usage, "candidates_token_count", None)
    result["cached_tokens"] = getattr(usage, "cached_content_token_count", None)
    result["cost_usd"]      = _calc_cost_gemini(model, usage, batch=batch)
    result["output"]        = json.loads(response.text)
    result["success"]       = True
    return result


# ── OCR helpers for variants E and F ─────────────────────────────────────────

async def _ocr_via_glmocr_api(pdf_path: str) -> tuple:
    """
    Send PDF to z.ai GLM-OCR hosted API (no local GPU needed).
    Returns (markdown_text: str, usage_dict: dict).
    Requires GLM_OCR_API_KEY env var.
    Docs: https://docs.z.ai/api-reference/tools/layout-parsing
    """
    import httpx

    pdf_b64  = base64.b64encode(Path(pdf_path).read_bytes()).decode()
    headers  = {
        "Authorization": f"Bearer {os.environ['GLM_OCR_API_KEY']}",
        "Content-Type":  "application/json",
    }
    payload  = {
        "model": "glm-ocr",
        "file":  f"data:application/pdf;base64,{pdf_b64}",
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(GLM_OCR_API_URL, headers=headers, json=payload)
        resp.raise_for_status()

    data = resp.json()
    return data.get("md_results", ""), data.get("usage", {})


async def _ocr_via_qwen_vl(pdf_path: str, client) -> tuple:
    """
    Render PDF pages as PNG images (300 DPI) and send to Qwen3-VL-32B via OpenRouter.
    OpenRouter does not support PDF files directly — page rendering is required.
    300 DPI is the standard for document OCR (150 DPI loses fine table detail).
    Returns (extracted_text: str, usage).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError("PyMuPDF not installed — run: pip install pymupdf")

    doc_pdf    = fitz.open(pdf_path)
    page_count = doc_pdf.page_count

    # Render pages in parallel — each worker opens its own handle to avoid
    # PyMuPDF threading issues with a shared Document object.
    def _render_page(page_num: int) -> str:
        d   = fitz.open(pdf_path)
        pix = d[page_num].get_pixmap(dpi=300)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        d.close()
        return b64

    workers = min(page_count, os.cpu_count() or 4)
    loop    = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        img_b64s = await asyncio.gather(
            *[loop.run_in_executor(pool, _render_page, i) for i in range(page_count)]
        )
    doc_pdf.close()

    content = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}}
        for b in img_b64s
    ]

    content.append({
        "type": "text",
        "text": (
            "Extract all text from these document pages accurately. "
            "Preserve table structure using markdown tables. "
            "Return only the extracted content with no commentary."
        ),
    })

    response = await client.chat.completions.create(
        model="qwen/qwen3-vl-32b-instruct",
        messages=[{"role": "user", "content": content}],
        temperature=0,
    )
    return response.choices[0].message.content, response.usage


async def _structure_via_gpt_luna(ocr_text: str, client, prompts: Prompts) -> tuple:
    """
    Send OCR text to GPT-5.6 Luna for structure extraction.
    Returns (json_output: dict, usage).
    """
    response = await client.chat.completions.create(
        model="gpt-5.6-luna",
        messages=[
            {"role": "system", "content": prompts.structure_system},
            {"role": "user",   "content": prompts.structure_user.format(markdown_content=ocr_text)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content), response.usage


# ── Async mode — Gemini variants (A B C D) ───────────────────────────────────

async def _run_one_gemini(
    doc: dict,
    variant: str,
    model: str,
    call_type: str,
    sem: asyncio.Semaphore,
    client: genai.Client,
    prompts: Prompts,
) -> dict:
    result = _base_result(doc, variant, model, call_type)

    if call_type == "2-call" and not doc["has_md"]:
        result["skipped"] = True
        result["error"]   = "no markdown — scanned PDF, use single-call variant"
        return result

    if not doc["has_pdf"] and call_type == "single":
        result["skipped"] = True
        result["error"]   = "PDF file not found on disk"
        return result

    async with sem:
        t0 = time.monotonic()
        try:
            use_pdf = False  # will be set True when markdown falls back to PDF

            if call_type == "2-call":
                md = Path(doc["md_path"]).read_text(encoding="utf-8", errors="replace")
                # If extracted markdown has too few tables it likely failed OCR/layout.
                # Fall back to the raw PDF so the model can parse it directly.
                if _count_tables(md) < MIN_TABLES_IN_MD:
                    if doc["has_pdf"]:
                        use_pdf = True
                    else:
                        result["skipped"] = True
                        result["error"]   = (
                            f"markdown has <{MIN_TABLES_IN_MD} tables and PDF unavailable for fallback"
                        )
                        return result

            if call_type == "single" or use_pdf:
                pdf_bytes = Path(doc["pdf_path"]).read_bytes()
                pdf_part  = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
                response  = await client.aio.models.generate_content(
                    model=model,
                    contents=[pdf_part, prompts.single_call_user],
                    config=types.GenerateContentConfig(
                        system_instruction=prompts.single_call_system,
                        temperature=0,
                        response_mime_type="application/json",
                    ),
                )
                result["pdf_fallback"] = use_pdf
            else:
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=[prompts.structure_user.format(markdown_content=md)],
                    config=types.GenerateContentConfig(
                        system_instruction=prompts.structure_system,
                        temperature=0,
                        response_mime_type="application/json",
                    ),
                )

            result["latency_s"] = round(time.monotonic() - t0, 2)
            _parse_gemini_response(response, result, model, batch=False)
        except Exception as e:
            result["latency_s"] = round(time.monotonic() - t0, 2)
            result["error"]     = str(e)

    return result


# ── Async mode — OpenAI variants (E F) ───────────────────────────────────────

async def _run_one_openai(
    doc: dict,
    variant: str,
    sem: asyncio.Semaphore,
    openai_client,
    openrouter_client,
    prompts: Prompts,
) -> dict:
    model     = VARIANT_MODEL[variant]
    ocr_model = VARIANT_OCR_MODEL[variant]
    call_type = VARIANT_CALL[variant]
    result    = _base_result(doc, variant, model, call_type)

    if not doc["has_pdf"]:
        result["skipped"] = True
        result["error"]   = "PDF file not found on disk"
        return result

    async with sem:
        t0 = time.monotonic()
        try:
            # ── Step 1: OCR ───────────────────────────────────────────────────
            if variant == "E":
                ocr_text, ocr_usage_dict = await _ocr_via_glmocr_api(doc["pdf_path"])
                ocr_cost = _calc_cost_glmocr(ocr_usage_dict)
            else:  # F — Qwen3-VL-32B via OpenRouter
                ocr_text, ocr_usage = await _ocr_via_qwen_vl(
                    doc["pdf_path"], openrouter_client
                )
                ocr_cost = _calc_cost_openai(ocr_model, ocr_usage)

            # ── Step 2: Structure via GPT-5.6 Luna ───────────────────────────
            output, struct_usage = await _structure_via_gpt_luna(ocr_text, openai_client, prompts)
            struct_cost = _calc_cost_openai(model, struct_usage)

            result["latency_s"]      = round(time.monotonic() - t0, 2)
            result["output"]         = output
            result["success"]        = True
            result["input_tokens"]   = getattr(struct_usage, "prompt_tokens", None)
            result["output_tokens"]  = getattr(struct_usage, "completion_tokens", None)
            result["cached_tokens"]  = getattr(
                getattr(struct_usage, "prompt_tokens_details", None),
                "cached_tokens", None,
            )
            result["cost_usd"]       = ocr_cost + struct_cost
            result["cost_breakdown"] = {
                "ocr_model":      ocr_model,
                "ocr_cost_usd":   round(ocr_cost, 6),
                "struct_cost_usd": round(struct_cost, 6),
            }
        except Exception as e:
            result["latency_s"] = round(time.monotonic() - t0, 2)
            result["error"]     = str(e)

    return result


# ── run_async dispatcher ──────────────────────────────────────────────────────

async def run_async(docs: list, variant: str, concurrency: int,
                    run_dir: Optional[Path] = None,
                    prompts: Optional[Prompts] = None) -> list:
    sem   = asyncio.Semaphore(concurrency)
    total = len(docs)

    if variant in GEMINI_VARIANTS:
        model     = VARIANT_MODEL[variant]
        call_type = VARIANT_CALL[variant]
        client    = _make_gemini_client()
        tasks = [
            asyncio.ensure_future(
                _run_one_gemini(doc, variant, model, call_type, sem, client, prompts)
            )
            for doc in docs
        ]
    else:  # E or F
        import openai as _openai
        openai_client     = _openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        openrouter_client = (
            _openai.AsyncOpenAI(
                api_key=os.environ.get("OPENROUTER_API_KEY", ""),
                base_url=OPENROUTER_BASE_URL,
            )
            if variant == "F"
            else None
        )
        tasks = [
            asyncio.ensure_future(
                _run_one_openai(doc, variant, sem, openai_client, openrouter_client, prompts)
            )
            for doc in docs
        ]

    # Stream results to disk as each doc completes so the UI can show progress.
    partial_path = (run_dir / "results.jsonl") if run_dir else None
    partial_file = open(partial_path, "w") if partial_path else None

    results    = []
    done_count = 0
    try:
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            done_count += 1
            tag = "ok" if r["success"] else ("skip" if r["skipped"] else "ERR")
            lat = f"  {r['latency_s']:.1f}s" if r["latency_s"] is not None else ""
            print(f"  [{done_count:3d}/{total}] {tag}{lat}  {r['local_path']}", flush=True)
            if partial_file:
                partial_file.write(json.dumps(r, default=str) + "\n")
                partial_file.flush()
    finally:
        if partial_file:
            partial_file.close()

    return results


# ── Batch mode helpers ────────────────────────────────────────────────────────

def _upload_pdf(
    doc: dict,
    client: genai.Client,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> str:
    """Upload PDF to Gemini Files API with exponential backoff retry."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            with open(doc["pdf_path"], "rb") as f:
                resp = client.files.upload(
                    file=f,
                    config=types.UploadFileConfig(
                        mime_type="application/pdf",
                        display_name=doc["local_path"],
                    ),
                )
            return resp.uri
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff_base ** attempt  # 2s, 4s, 8s …
                print(
                    f"      upload attempt {attempt} failed ({e}), "
                    f"retrying in {wait:.0f}s…",
                    flush=True,
                )
                time.sleep(wait)
    raise RuntimeError(f"upload failed after {max_retries} attempts: {last_exc}")


def _upload_to_gcs(
    doc: dict,
    bucket_name: str,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> str:
    """Upload PDF to GCS and return gs:// URI. Uses ADC — works with Vertex AI."""
    from google.cloud import storage as gcs_storage
    gcs_client = gcs_storage.Client()
    bucket   = gcs_client.bucket(bucket_name)
    blob_name = doc["local_path"].lstrip("/")
    blob      = bucket.blob(blob_name)
    last_exc  = None
    for attempt in range(1, max_retries + 1):
        try:
            blob.upload_from_filename(doc["pdf_path"], content_type="application/pdf")
            return f"gs://{bucket_name}/{blob_name}"
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff_base ** attempt
                print(
                    f"      GCS upload attempt {attempt} failed ({e}), "
                    f"retrying in {wait:.0f}s…",
                    flush=True,
                )
                time.sleep(wait)
    raise RuntimeError(f"GCS upload failed after {max_retries} attempts: {last_exc}")


def _batch_backend(call_type: str) -> str:
    """Return 'vertex' or 'aistudio' for Gemini batch jobs.

    BATCH_BACKEND env var overrides auto-detection:
      vertex   — Vertex AI + GCS JSONL (uses GCP credits; default for 2-call when configured)
      aistudio — AI Studio inline InlinedRequests (uses AI Studio quota, free tier)

    Auto: 'vertex' when GEMINI_PROJECT + GCS_BUCKET are both set, else 'aistudio'.
    """
    explicit = os.environ.get("BATCH_BACKEND", "").lower()
    if explicit in ("vertex", "aistudio"):
        return explicit
    if os.environ.get("GEMINI_PROJECT") and os.environ.get("GCS_BUCKET"):
        return "vertex"
    return "aistudio"


def _make_vertex_jsonl_line(
    doc: dict,
    call_type: str,
    prompts: "Prompts",
    file_uri: Optional[str] = None,
) -> dict:
    """One request line for a Vertex AI batch prediction input JSONL file."""
    if call_type == "2-call":
        md = Path(doc["md_path"]).read_text(encoding="utf-8", errors="replace")
        user_parts  = [{"text": prompts.structure_user.format(markdown_content=md)}]
        system_text = prompts.structure_system
    else:
        if not file_uri:
            raise ValueError(
                f"GCS file URI required for single-call Vertex batch: {doc['local_path']}"
            )
        user_parts  = [
            {"fileData": {"fileUri": file_uri, "mimeType": "application/pdf"}},
            {"text": prompts.single_call_user},
        ]
        system_text = prompts.single_call_system
    return {
        "request": {
            "contents": [{"role": "user", "parts": user_parts}],
            "systemInstruction": {"parts": [{"text": system_text}]},
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
    }


def _upload_jsonl_to_gcs(lines: list, gcs_bucket: str, blob_name: str) -> str:
    """Serialize a list of dicts as JSONL and upload to GCS. Returns gs:// URI."""
    from google.cloud import storage as gcs_storage
    content    = "\n".join(json.dumps(line) for line in lines)
    gcs_client = gcs_storage.Client()
    blob       = gcs_client.bucket(gcs_bucket).blob(blob_name)
    blob.upload_from_string(content, content_type="application/jsonl")
    return f"gs://{gcs_bucket}/{blob_name}"


def _parse_vertex_gcs_response(resp_dict: dict, result: dict, model: str) -> dict:
    """Parse a Vertex AI GCS batch output response dict into a result record."""
    usage = resp_dict.get("usageMetadata", {})

    class _U:
        prompt_token_count         = usage.get("promptTokenCount")
        candidates_token_count     = usage.get("candidatesTokenCount")
        cached_content_token_count = usage.get("cachedContentTokenCount")

    result["input_tokens"]  = _U.prompt_token_count
    result["output_tokens"] = _U.candidates_token_count
    result["cached_tokens"] = _U.cached_content_token_count
    result["cost_usd"]      = _calc_cost_gemini(model, _U(), batch=True)

    candidates = resp_dict.get("candidates", [])
    if not candidates:
        raise ValueError("Empty candidates in Vertex AI batch response")
    text = "".join(
        p.get("text", "")
        for p in candidates[0].get("content", {}).get("parts", [])
    )
    result["output"]  = json.loads(text)
    result["success"] = True
    return result


def _collect_vertex_gcs_batch_results(
    output_gcs_prefix: str,
    active_docs: list,
    skipped_results: list,
    variant: str,
    model: str,
    call_type: str,
) -> list:
    """Download output JSONL from a Vertex AI GCS batch job and map to result records."""
    from google.cloud import storage as gcs_storage
    stripped    = output_gcs_prefix.removeprefix("gs://")
    bucket_name, _, prefix = stripped.partition("/")
    gcs_client  = gcs_storage.Client()
    bucket_obj  = gcs_client.bucket(bucket_name)

    all_lines: list = []
    for blob in sorted(bucket_obj.list_blobs(prefix=prefix), key=lambda b: b.name):
        if blob.name.endswith(".jsonl"):
            all_lines.extend(
                line for line in blob.download_as_text().splitlines() if line.strip()
            )

    parsed = [json.loads(line) for line in all_lines]

    results = list(skipped_results)
    for doc, entry in zip(active_docs, parsed):
        r = _base_result(doc, variant, model, call_type)
        status = entry.get("status")
        if isinstance(status, dict) and status:
            # Non-empty dict status means Vertex reported an error for this request.
            r["error"] = status.get("message") or str(status)
        else:
            try:
                _parse_vertex_gcs_response(entry.get("response", {}), r, model)
            except Exception as exc:
                r["error"] = str(exc)
        print(f"  {'ok' if r.get('success') else 'ERR'}  {r['local_path']}", flush=True)
        results.append(r)
    return results


def _make_inlined_request(
    doc: dict,
    model: str,
    call_type: str,
    prompts: Prompts,
    file_uri: Optional[str] = None,
) -> types.InlinedRequest:
    if call_type == "2-call":
        md       = Path(doc["md_path"]).read_text(encoding="utf-8", errors="replace")
        contents = [prompts.structure_user.format(markdown_content=md)]
        system   = prompts.structure_system
    else:
        if file_uri:
            pdf_part = types.Part(
                file_data=types.FileData(file_uri=file_uri, mime_type="application/pdf")
            )
        else:
            pdf_bytes = Path(doc["pdf_path"]).read_bytes()
            pdf_part  = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        contents = [pdf_part, prompts.single_call_user]
        system   = prompts.single_call_system

    return types.InlinedRequest(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
        ),
    )


def _collect_batch_results(
    job,
    active_docs: list,
    skipped_results: list,
    variant: str,
    model: str,
    call_type: str,
) -> list:
    results           = list(skipped_results)
    inlined_responses = (job.dest and job.dest.inlined_responses) or []

    for doc, inlined in zip(active_docs, inlined_responses):
        r = _base_result(doc, variant, model, call_type)
        if inlined.error:
            r["error"] = getattr(inlined.error, "message", None) or str(inlined.error)
        else:
            try:
                _parse_gemini_response(inlined.response, r, model, batch=True)
            except Exception as e:
                r["error"] = str(e)
        tag = "ok" if r["success"] else "ERR"
        print(f"  {tag}  {r['local_path']}", flush=True)
        results.append(r)

    return results


# ── Batch submit — OpenAI variants (E F) ─────────────────────────────────────

def _submit_ef_batch(
    docs: list,
    variant: str,
    state: str,
    run_dir: Path,
    poll_tries: int,
    poll_interval: int,
    prompts: Optional[Prompts] = None,
) -> Optional[list]:
    """
    Hybrid batch for E/F: runs OCR concurrently (async), then submits ALL
    structure chunks to the OpenAI Batch API simultaneously so they process
    in parallel on OpenAI's side.
    Returns results list if all batches finish within the poll window,
    or None if any are still running (chunk job files saved under run_dir).
    """
    import io
    import openai as _openai

    model     = VARIANT_MODEL[variant]
    ocr_model = VARIANT_OCR_MODEL[variant]
    call_type = VARIANT_CALL[variant]

    # ── Partition ─────────────────────────────────────────────────────────────
    skipped_results: list = []
    active_docs:     list = []
    for doc in docs:
        if not doc["has_pdf"]:
            r = _base_result(doc, variant, model, call_type)
            r["skipped"] = True
            r["error"]   = "PDF file not found on disk"
            skipped_results.append(r)
        else:
            active_docs.append(doc)

    _update_run_skipped(run_dir, skipped_results)

    if not active_docs:
        print("  No docs to submit — all skipped.")
        return skipped_results

    # ── Step 1: Concurrent OCR (async, all docs at once) ─────────────────────
    print(f"  Running OCR ({ocr_model}) on {len(active_docs)} docs…", flush=True)

    openrouter_client = (
        _openai.AsyncOpenAI(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=OPENROUTER_BASE_URL,
        )
        if variant == "F"
        else None
    )

    async def _run_ocr_all():
        sem = asyncio.Semaphore(10)

        async def _ocr_one(doc):
            async with sem:
                if variant == "E":
                    return await _ocr_via_glmocr_api(doc["pdf_path"])
                else:
                    return await _ocr_via_qwen_vl(doc["pdf_path"], openrouter_client)

        return await asyncio.gather(*[_ocr_one(d) for d in active_docs], return_exceptions=True)

    ocr_results = asyncio.run(_run_ocr_all())

    # ── Build JSONL lines ─────────────────────────────────────────────────────
    batch_lines:  list[str] = []
    ocr_costs:    dict      = {}
    failed_paths: set       = set()

    for doc, ocr_result in zip(active_docs, ocr_results):
        if isinstance(ocr_result, Exception):
            r = _base_result(doc, variant, model, call_type)
            r["error"] = str(ocr_result)
            skipped_results.append(r)
            failed_paths.add(doc["local_path"])
            print(f"  OCR ERR  {doc['local_path']}: {ocr_result}", flush=True)
            continue

        ocr_text, ocr_usage_raw = ocr_result
        ocr_cost = (
            _calc_cost_glmocr(ocr_usage_raw)
            if variant == "E"
            else _calc_cost_openai(ocr_model, ocr_usage_raw)
        )
        ocr_costs[doc["local_path"]] = ocr_cost

        batch_lines.append(json.dumps({
            "custom_id": doc["local_path"],
            "method":    "POST",
            "url":       "/v1/chat/completions",
            "body": {
                "model":           model,
                "messages":        [
                    {"role": "system", "content": prompts.structure_system},
                    {"role": "user",   "content": prompts.structure_user.format(markdown_content=ocr_text)},
                ],
                "temperature":     0,
                "response_format": {"type": "json_object"},
            },
        }))

    active_docs = [d for d in active_docs if d["local_path"] not in failed_paths]
    _update_run_skipped(run_dir, skipped_results)   # refresh with OCR failures

    if not batch_lines:
        print("  No docs remaining after OCR step.")
        return skipped_results

    # ── Step 2: Submit ALL chunks to OpenAI Batch API at once ────────────────
    sync_client = _openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    _DONE       = {"completed", "failed", "expired", "cancelled"}

    line_chunks = [
        batch_lines[i : i + OPENAI_BATCH_MAX]
        for i in range(0, len(batch_lines), OPENAI_BATCH_MAX)
    ]
    doc_chunks  = [
        active_docs[i : i + OPENAI_BATCH_MAX]
        for i in range(0, len(active_docs), OPENAI_BATCH_MAX)
    ]
    n_chunks    = len(line_chunks)

    if n_chunks > 1:
        print(f"\n  {len(batch_lines)} requests → {n_chunks} OpenAI batch jobs "
              f"(chunk_max={OPENAI_BATCH_MAX}).", flush=True)

    submitted = []   # (chunk_idx, batch_obj, chunk_docs, chunk_costs)

    for chunk_idx, (lines, chunk_docs) in enumerate(zip(line_chunks, doc_chunks), 1):
        suffix      = f" (chunk {chunk_idx}/{n_chunks})" if n_chunks > 1 else ""
        jsonl_bytes = "\n".join(lines).encode()
        chunk_costs = {d["local_path"]: ocr_costs.get(d["local_path"], 0.0) for d in chunk_docs}

        print(f"  Uploading + submitting JSONL ({len(lines)} requests){suffix}…", flush=True)
        file_obj = sync_client.files.create(
            file=("batch.jsonl", io.BytesIO(jsonl_bytes), "application/jsonl"),
            purpose="batch",
        )
        batch = sync_client.batches.create(
            input_file_id=file_obj.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
        print(f"    Batch ID: {batch.id}  Status: {batch.status}", flush=True)
        submitted.append((chunk_idx, batch, chunk_docs, chunk_costs))

    # ── Poll all chunks together ──────────────────────────────────────────────
    for attempt in range(1, poll_tries + 1):
        pending = [(i, b, d, c) for i, b, d, c in submitted if b.status not in _DONE]
        if not pending:
            break
        print(f"  Polling attempt {attempt}/{poll_tries}: "
              f"{len(pending)} batch(es) running… (waiting {poll_interval}s)", flush=True)
        time.sleep(poll_interval)
        submitted = [
            (i, sync_client.batches.retrieve(b.id) if b.status not in _DONE else b, d, c)
            for i, b, d, c in submitted
        ]
        for i, b, _, _ in submitted:
            if b.status not in _DONE:
                print(f"    Chunk {i}: {b.status}", flush=True)

    # ── Collect results / save pending job files ──────────────────────────────
    all_results  = []
    any_pending  = False

    for chunk_idx, batch, chunk_docs, chunk_costs in submitted:
        suffix = f" (chunk {chunk_idx}/{n_chunks})" if n_chunks > 1 else ""
        if batch.status not in _DONE:
            any_pending = True
            job_file = _save_chunk_job_file(
                run_dir, chunk_idx, batch.id, variant, model, call_type, state,
                chunk_docs, provider="openai",
                extra={"ocr_costs": chunk_costs},
            )
            _register_chunk(run_dir, chunk_idx, status="pending", job_file=job_file)
            print(f"  Chunk {chunk_idx} still running — saved {job_file}", flush=True)
        elif batch.status != "completed":
            print(f"  Chunk {chunk_idx} FAILED: {batch.status}", flush=True)
            for doc in chunk_docs:
                r = _base_result(doc, variant, model, call_type)
                r["error"] = f"OpenAI batch {batch.status}"
                all_results.append(r)
        else:
            print(f"\n  Batch complete{suffix}. Collecting results…", flush=True)
            chunk_results = _collect_openai_batch_results(
                batch, chunk_docs, [], variant, model, call_type,
                ocr_model, chunk_costs, sync_client,
            )
            results_file = _save_chunk_results(run_dir, chunk_idx, chunk_results)
            _register_chunk(run_dir, chunk_idx, status="complete", results_file=results_file)
            all_results.extend(chunk_results)

    if any_pending:
        return None

    return skipped_results + all_results


def _collect_openai_batch_results(
    batch,
    active_docs: list,
    skipped_results: list,
    variant: str,
    model: str,
    call_type: str,
    ocr_model: str,
    ocr_costs: dict,
    sync_client,
) -> list:
    """Parse OpenAI batch output JSONL and build result dicts."""
    results      = list(skipped_results)
    path_to_doc  = {d["local_path"]: d for d in active_docs}
    p            = PRICE.get(model, {"input": 0.0, "input_cached": 0.0, "output": 0.0})

    file_content = sync_client.files.content(batch.output_file_id).text
    for line in file_content.strip().split("\n"):
        if not line.strip():
            continue
        entry     = json.loads(line)
        custom_id = entry["custom_id"]
        doc       = path_to_doc.get(custom_id)
        if doc is None:
            continue

        r        = _base_result(doc, variant, model, call_type)
        ocr_cost = ocr_costs.get(custom_id, 0.0)

        if entry.get("error"):
            r["error"] = str(entry["error"])
        else:
            try:
                rb                = entry["response"]["body"]
                choice            = rb["choices"][0]
                su                = rb.get("usage", {})
                prompt_tokens     = su.get("prompt_tokens", 0)
                completion_tokens = su.get("completion_tokens", 0)
                cached_tokens     = (su.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                uncached          = max(0, prompt_tokens - cached_tokens)
                struct_cost       = (
                    uncached      * p["input"]        / 1_000_000
                    + cached_tokens * p["input_cached"] / 1_000_000
                    + completion_tokens * p["output"]   / 1_000_000
                ) * BATCH_DISCOUNT

                r["output"]         = json.loads(choice["message"]["content"])
                r["success"]        = True
                r["input_tokens"]   = prompt_tokens
                r["output_tokens"]  = completion_tokens
                r["cached_tokens"]  = cached_tokens
                r["cost_usd"]       = ocr_cost + struct_cost
                r["cost_breakdown"] = {
                    "ocr_model":       ocr_model,
                    "ocr_cost_usd":    round(ocr_cost, 6),
                    "struct_cost_usd": round(struct_cost, 6),
                }
            except Exception as e:
                r["error"] = str(e)

        tag = "ok" if r["success"] else "ERR"
        print(f"  {tag}  {r['local_path']}", flush=True)
        results.append(r)

    return results


# ── Batch submit — Gemini variants (A B C D) ──────────────────────────────────

def submit_batch(
    docs: list,
    variant: str,
    state: str,
    run_dir: Path,
    poll_tries: int,
    poll_interval: int,
    prompts: Optional[Prompts] = None,
) -> Optional[list]:
    """
    Submit batch jobs. For E/F delegates to _submit_ef_batch (OpenAI Batch API);
    for A/D submits all Gemini Batch API chunks simultaneously then polls together.
    Returns merged results list if all chunks finish within the poll window,
    or None if any are still running (chunk files saved under run_dir).
    """
    if variant in OPENAI_VARIANTS:
        return _submit_ef_batch(docs, variant, state, run_dir, poll_tries, poll_interval, prompts)

    model     = VARIANT_MODEL[variant]
    call_type = VARIANT_CALL[variant]
    # backend: 'vertex' → Vertex AI + GCS JSONL (uses $300 GCP credits, default for 2-call)
    #          'aistudio' → AI Studio inline InlinedRequests (free tier)
    # Override with BATCH_BACKEND=vertex|aistudio in .env
    backend    = _batch_backend(call_type)
    gcs_bucket = os.environ.get("GCS_BUCKET") if backend == "vertex" else None
    if backend == "vertex":
        client = _make_gemini_client()
    else:
        client = _make_gemini_devapi_client()

    # ── Partition docs ────────────────────────────────────────────────────────
    # For 2-call variants: docs with markdown are sent inline (no upload needed).
    # If markdown has too few tables (likely a failed extraction), fall back to
    # the raw PDF — treat those docs as single-call for this batch.
    skipped_results: list = []
    md_docs:         list = []   # 2-call path: send extracted markdown inline
    pdf_docs:        list = []   # single-call path: upload PDF then reference URI
    file_uris:       dict = {}   # local_path → Gemini Files URI
    pdf_fallbacks:   set  = set()  # local_paths where we fell back to PDF

    for doc in docs:
        if call_type == "single":
            if not doc["has_pdf"]:
                r = _base_result(doc, variant, model, call_type)
                r["skipped"] = True
                r["error"]   = "PDF file not found on disk"
                skipped_results.append(r)
            else:
                pdf_docs.append(doc)
        else:  # 2-call
            if not doc["has_md"]:
                r = _base_result(doc, variant, model, call_type)
                r["skipped"] = True
                r["error"]   = "no markdown — scanned PDF, use single-call variant"
                skipped_results.append(r)
                continue
            md = Path(doc["md_path"]).read_text(encoding="utf-8", errors="replace")
            if _count_tables(md) < MIN_TABLES_IN_MD:
                # Markdown extraction poor — fall back to PDF
                if doc["has_pdf"]:
                    pdf_docs.append(doc)
                    pdf_fallbacks.add(doc["local_path"])
                else:
                    r = _base_result(doc, variant, model, call_type)
                    r["skipped"] = True
                    r["error"]   = (
                        f"markdown has <{MIN_TABLES_IN_MD} tables and PDF unavailable for fallback"
                    )
                    skipped_results.append(r)
            else:
                md_docs.append(doc)

    _update_run_skipped(run_dir, skipped_results)

    active_docs = md_docs + pdf_docs
    if not active_docs:
        print("  No docs to submit — all skipped.")
        return skipped_results

    # ── Upload PDFs in parallel ───────────────────────────────────────────────
    # GCS mode: upload to gs://{GCS_BUCKET} (Vertex AI compatible, uses ADC).
    # Fallback: Gemini Files API (AI Studio only).
    if pdf_docs:
        n = len(pdf_docs)
        workers = min(n, 10)
        if backend == "vertex":
            print(f"  Uploading {n} PDFs to GCS gs://{gcs_bucket} (parallel, {workers} workers)…", flush=True)
        else:
            print(f"  Uploading {n} PDFs to Gemini Files API (parallel, {workers} workers)…", flush=True)
        failed: set = set()

        def _upload_one(doc):
            if gcs_bucket:
                return doc, _upload_to_gcs(doc, gcs_bucket)
            return doc, _upload_pdf(doc, client)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_upload_one, doc): doc for doc in pdf_docs}
            done_n  = 0
            for fut in futures_as_completed(futures):
                done_n += 1
                doc = futures[fut]
                try:
                    _, uri = fut.result()
                    file_uris[doc["local_path"]] = uri
                    print(f"    [{done_n:3d}/{n}] ok  {doc['local_path']}", flush=True)
                except Exception as e:
                    print(f"    [{done_n:3d}/{n}] ERR {doc['local_path']}: {e}", flush=True)
                    r = _base_result(doc, variant, model, call_type)
                    r["skipped"] = True
                    r["error"]   = f"upload failed: {e}"
                    skipped_results.append(r)
                    failed.add(doc["local_path"])

        pdf_docs    = [d for d in pdf_docs    if d["local_path"] not in failed]
        active_docs = md_docs + pdf_docs
        pdf_fallbacks -= failed

    if not active_docs:
        print("  No docs remaining after upload step.")
        return skipped_results

    # ── Build requests ────────────────────────────────────────────────────────
    # Vertex AI: build JSONL dicts (uploaded to GCS per chunk).
    # AI Studio: build InlinedRequest objects (sent inline in the API call).
    def _doc_call_type(doc):
        return "single" if doc["local_path"] in pdf_fallbacks or call_type == "single" else "2-call"

    if backend == "vertex":
        all_requests = [
            _make_vertex_jsonl_line(d, _doc_call_type(d), prompts, file_uris.get(d["local_path"]))
            for d in active_docs
        ]
    else:
        all_requests = [
            _make_inlined_request(d, model, _doc_call_type(d), prompts, file_uris.get(d["local_path"]))
            for d in active_docs
        ]

    # ── Chunk size ────────────────────────────────────────────────────────────
    # Vertex AI GCS: no inline payload cap — always use the larger limit.
    # AI Studio inline TEXT (A/C 2-call): 20 MB payload cap applies.
    # AI Studio inline URI (B/D single-call): only tiny URI strings — cap not a concern.
    if backend == "vertex":
        chunk_max = GEMINI_BATCH_MAX_URI
    else:
        chunk_max = GEMINI_BATCH_MAX_TEXT if bool(md_docs) else GEMINI_BATCH_MAX_URI

    chunks = [
        all_requests[i : i + chunk_max]
        for i in range(0, len(all_requests), chunk_max)
    ]
    doc_chunks = [
        active_docs[i : i + chunk_max]
        for i in range(0, len(active_docs), chunk_max)
    ]

    n_chunks = len(chunks)
    if n_chunks > 1:
        print(f"\n  {len(active_docs)} requests → {n_chunks} jobs "
              f"(chunk_max={chunk_max}).", flush=True)

    # ── Submit ALL chunks simultaneously (they run in parallel on Gemini) ─────
    submit_start = time.time()
    submitted = []   # (chunk_idx, job, chunk_docs, output_gcs_prefix_or_none)
    for chunk_idx, (chunk_reqs, chunk_docs) in enumerate(zip(chunks, doc_chunks), 1):
        suffix = f" (chunk {chunk_idx}/{n_chunks})" if n_chunks > 1 else ""
        print(f"  Submitting {len(chunk_reqs)} requests{suffix}…", flush=True)

        output_prefix = None
        if backend == "vertex":
            # Serialize requests to JSONL, upload to GCS, submit via GCS URI
            input_blob    = f"batch_inputs/{run_dir.name}/chunk_{chunk_idx}.jsonl"
            output_prefix = f"gs://{gcs_bucket}/batch_outputs/{run_dir.name}/chunk_{chunk_idx}/"
            input_uri     = _upload_jsonl_to_gcs(chunk_reqs, gcs_bucket, input_blob)
            print(f"    JSONL → {input_uri}", flush=True)
            job = client.batches.create(
                model=model,
                src=input_uri,
                config=types.CreateBatchJobConfig(dest=output_prefix),
            )
        else:
            job = client.batches.create(model=model, src=chunk_reqs)

        submitted.append((chunk_idx, job, chunk_docs, output_prefix))
        print(f"    Job: {job.name}  State: {job.state}", flush=True)

    # ── Poll all jobs together ────────────────────────────────────────────────
    for attempt in range(1, poll_tries + 1):
        pending = [(i, j, d, op) for i, j, d, op in submitted if not j.done]
        if not pending:
            break
        print(f"  Polling attempt {attempt}/{poll_tries}: "
              f"{len(pending)} job(s) running… (waiting {poll_interval}s)", flush=True)
        time.sleep(poll_interval)
        submitted = [
            (i, client.batches.get(name=j.name) if not j.done else j, d, op)
            for i, j, d, op in submitted
        ]
        for i, j, _, _ in submitted:
            if not j.done:
                print(f"    Chunk {i}: {j.state}", flush=True)

    # ── Collect results / save pending chunk files ────────────────────────────
    all_results = []
    any_pending = False

    for chunk_idx, job, chunk_docs, output_prefix in submitted:
        suffix = f" (chunk {chunk_idx}/{n_chunks})" if n_chunks > 1 else ""
        if not job.done:
            any_pending = True
            extra = {"vertexai": backend == "vertex", "gcs_bucket": gcs_bucket}
            if output_prefix:
                extra["gcs_output_prefix"] = output_prefix
            job_file = _save_chunk_job_file(
                run_dir, chunk_idx, job.name, variant, model, call_type, state,
                chunk_docs, provider="gemini", extra=extra,
            )
            _register_chunk(run_dir, chunk_idx, status="pending", job_file=job_file)
            print(f"  Chunk {chunk_idx} still running — saved {job_file}", flush=True)
        elif job.state not in types.JOB_STATES_SUCCEEDED:
            err = getattr(job.error, "message", str(job.error)) if job.error else "unknown"
            print(f"  Chunk {chunk_idx} FAILED: {job.state} — {err}", flush=True)
            for doc in chunk_docs:
                r = _base_result(doc, variant, model, call_type)
                r["error"] = f"Gemini batch {job.state}: {err}"
                all_results.append(r)
        else:
            try:
                elapsed = round((job.end_time - job.start_time).total_seconds())
                elapsed_str = (f"Google: {job.start_time.strftime('%H:%M:%S')} → "
                               f"{job.end_time.strftime('%H:%M:%S')} UTC, {elapsed/60:.1f} min")
            except Exception:
                elapsed = round(time.time() - submit_start)
                elapsed_str = f"{elapsed/60:.1f} min (wall-clock fallback)"
            print(f"\n  Job complete{suffix}. Collecting results… ({elapsed_str})", flush=True)
            if output_prefix:
                chunk_results = _collect_vertex_gcs_batch_results(
                    output_prefix, chunk_docs, [], variant, model, call_type
                )
            else:
                chunk_results = _collect_batch_results(job, chunk_docs, [], variant, model, call_type)
            for r in chunk_results:
                if r.get("local_path") in pdf_fallbacks:
                    r["pdf_fallback"] = True
            results_file = _save_chunk_results(run_dir, chunk_idx, chunk_results)
            _register_chunk(run_dir, chunk_idx, status="complete", results_file=results_file)
            all_results.extend(chunk_results)

    if any_pending:
        return None

    return skipped_results + all_results


# ── Batch fetch ───────────────────────────────────────────────────────────────

def _fetch_one_chunk(
    chunk_file: Path,
    poll_tries: int,
    poll_interval: int,
) -> Optional[list]:
    """
    Resolve a single pending chunk job file. Returns results list if done,
    or None if still running. Used by fetch_run().
    """
    with open(chunk_file) as f:
        info = json.load(f)

    provider     = info.get("provider", "gemini")
    job_name     = info["job_name"]
    variant      = info["variant"]
    model        = info["model"]
    call_type    = info["call_type"]
    active_paths = info["active_paths"]

    # Reconstruct active_docs
    with open(VAL_PATH) as f:
        val_results = json.load(f)["results"]
    path_to_val = {r["local_path"]: r for r in val_results}

    active_docs = []
    for p in active_paths:
        r = path_to_val.get(p, {})
        md_path  = MD_DIR   / (p + ".extracted.md")
        pdf_path = DOCS_DIR / p
        active_docs.append({
            "local_path": p,
            "rera_id":    r.get("rera_reg_id", Path(p).stem),
            "pdf_path":   str(pdf_path),
            "md_path":    str(md_path),
            "has_md":     md_path.exists(),
            "has_pdf":    pdf_path.exists(),
        })

    # ── OpenAI provider ───────────────────────────────────────────────────────
    if provider == "openai":
        import openai as _openai
        if "OPENAI_API_KEY" not in os.environ:
            sys.exit("Error: OPENAI_API_KEY is required.")
        ocr_model   = VARIANT_OCR_MODEL.get(variant, "")
        ocr_costs   = info.get("ocr_costs", {})
        sync_client = _openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        _DONE       = {"completed", "failed", "expired", "cancelled"}

        batch = sync_client.batches.retrieve(job_name)
        print(f"    Batch ID: {job_name}  Status: {batch.status}", flush=True)
        for attempt in range(1, poll_tries + 1):
            if batch.status in _DONE:
                break
            print(f"    Polling {attempt}/{poll_tries} (waiting {poll_interval}s)…", flush=True)
            time.sleep(poll_interval)
            batch = sync_client.batches.retrieve(job_name)
            print(f"    Status: {batch.status}", flush=True)

        if batch.status not in _DONE:
            return None
        if batch.status != "completed":
            raise RuntimeError(f"OpenAI batch failed: {batch.status}")

        return _collect_openai_batch_results(
            batch, active_docs, [], variant, model, call_type,
            ocr_model, ocr_costs, sync_client,
        )

    # ── Gemini provider ───────────────────────────────────────────────────────
    # GCS-based batch jobs were submitted via Vertex AI — use Vertex client to fetch.
    if info.get("vertexai"):
        client = _make_gemini_client()
    else:
        client = _make_gemini_devapi_client()

    job = client.batches.get(name=job_name)
    print(f"    Job: {job_name}  State: {job.state}", flush=True)
    for attempt in range(1, poll_tries + 1):
        if job.done:
            break
        print(f"    Polling {attempt}/{poll_tries} (waiting {poll_interval}s)…", flush=True)
        time.sleep(poll_interval)
        job = client.batches.get(name=job_name)
        print(f"    State: {job.state}", flush=True)

    if not job.done:
        return None
    if job.state not in types.JOB_STATES_SUCCEEDED:
        err = getattr(job.error, "message", str(job.error)) if job.error else "unknown"
        raise RuntimeError(f"Gemini batch failed: {job.state} — {err}")

    elapsed_s = None
    try:
        if job.start_time and job.end_time:
            elapsed_s = round((job.end_time - job.start_time).total_seconds())
            print(f"  Batch job took {elapsed_s/60:.1f} min "
                  f"(Google: {job.start_time.strftime('%H:%M:%S')} → "
                  f"{job.end_time.strftime('%H:%M:%S')} UTC)", flush=True)
    except Exception:
        pass

    # Persist timing back to the chunk file so fetch_run can aggregate it.
    if elapsed_s is not None:
        try:
            info["batch_elapsed_s"] = elapsed_s
            with open(chunk_file, "w") as _cf:
                json.dump(info, _cf, indent=2)
        except Exception:
            pass

    gcs_output_prefix = info.get("gcs_output_prefix")
    if gcs_output_prefix:
        return _collect_vertex_gcs_batch_results(
            gcs_output_prefix, active_docs, [], variant, model, call_type
        )
    return _collect_batch_results(job, active_docs, [], variant, model, call_type)


def fetch_run(run_id: str, poll_tries: int, poll_interval: int) -> None:
    """
    Resolve all pending chunks for a run. For each pending chunk: polls the
    provider API, saves results if done, updates run.json. When all chunks are
    complete, merges everything (skipped + all chunk results) into results.jsonl.
    """
    run_dir = RUN_DIR / run_id
    manifest_path = run_dir / "run.json"
    if not manifest_path.exists():
        sys.exit(f"Error: run {run_id!r} not found at {run_dir}")

    with open(manifest_path) as f:
        manifest = json.load(f)

    variant  = manifest["variant"]
    mode     = manifest["mode"]

    if mode != "batch":
        results_path = manifest.get("results_path")
        print(f"Run {run_id} is async mode — results already at:")
        print(f"  {run_dir / (results_path or 'results.jsonl')}")
        return

    print(f"\nFetch run: {run_id}", flush=True)
    print(f"  variant  : {variant}  ({VARIANT_CALL.get(variant, '?')})", flush=True)
    print(f"  model    : {VARIANT_MODEL.get(variant, '?')}", flush=True)
    print(f"  state    : {manifest['state']}", flush=True)
    print(f"  created  : {manifest.get('created_at', '?')}", flush=True)
    print(f"  chunks   : {len(manifest['chunks'])}  "
          f"({sum(1 for c in manifest['chunks'] if c['status']=='pending')} pending)", flush=True)
    print(f"  poll     : {poll_tries} × {poll_interval}s", flush=True)
    print()

    # ── Resolve each pending chunk ────────────────────────────────────────────
    still_pending = []
    for chunk_info in manifest["chunks"]:
        if chunk_info["status"] != "pending":
            continue
        chunk_idx  = chunk_info["idx"]
        chunk_file = run_dir / chunk_info["job_file"]
        print(f"  Chunk {chunk_idx}…", flush=True)
        try:
            results = _fetch_one_chunk(chunk_file, poll_tries, poll_interval)
        except Exception as e:
            print(f"    ERR: {e}", flush=True)
            still_pending.append(chunk_idx)
            continue

        if results is None:
            print(f"    Still running.", flush=True)
            still_pending.append(chunk_idx)
        else:
            results_file = _save_chunk_results(run_dir, chunk_idx, results)
            _register_chunk(run_dir, chunk_idx, status="complete", results_file=results_file)
            print(f"    Done — {len(results)} results saved to {results_file}", flush=True)

    # Reload manifest (may have been updated)
    with open(manifest_path) as f:
        manifest = json.load(f)

    if still_pending:
        print(f"\n  {len(still_pending)} chunk(s) still running: {still_pending}")
        print(f"  Re-run when ready:")
        print(f"    python scripts/benchmark.py fetch --run-id {run_id}")
        return

    # ── All chunks complete — merge and save results.jsonl ────────────────────
    skipped_results = manifest.get("skipped_results", [])
    all_results     = list(skipped_results)

    elapsed_vals = []
    for chunk_info in sorted(manifest["chunks"], key=lambda c: c["idx"]):
        chunk_file = run_dir / chunk_info["results_file"]
        with open(chunk_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    all_results.append(json.loads(line))
        # Read elapsed from the corresponding job file if available.
        job_file = chunk_info.get("job_file")
        if job_file:
            try:
                with open(run_dir / job_file) as jf:
                    jdata = json.load(jf)
                if jdata.get("batch_elapsed_s"):
                    elapsed_vals.append(jdata["batch_elapsed_s"])
            except Exception:
                pass

    out_path = run_dir / "results.jsonl"
    with open(out_path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r, default=str) + "\n")

    extra = {}
    if elapsed_vals:
        extra["batch_elapsed_s"] = max(elapsed_vals)  # wall-clock = slowest chunk
    _mark_run_complete(run_dir, "results.jsonl", **extra)

    print_summary(all_results, "batch")
    print(f"Results → {out_path}")


# ── Output ────────────────────────────────────────────────────────────────────

def save_results(results: list, run_dir: Path) -> Path:
    out_path = run_dir / "results.jsonl"
    with open(out_path, "w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")
    return out_path


def print_summary(results: list, mode: str) -> None:
    total   = len(results)
    skipped = sum(1 for r in results if r.get("skipped"))
    success = sum(1 for r in results if r.get("success"))
    errors  = total - skipped - success

    costs     = [r["cost_usd"]  for r in results if r.get("cost_usd")  is not None]
    latencies = [r["latency_s"] for r in results if r.get("latency_s") is not None]

    print(f"\n{'─' * 52}")
    print(f"  Total   : {total}")
    print(f"  Success : {success}")
    print(f"  Skipped : {skipped}")
    print(f"  Errors  : {errors}")
    if costs:
        print(f"  Cost    : ${sum(costs):.4f} total  (avg ${sum(costs)/len(costs):.5f}/doc)")
    if latencies and mode == "async":
        avg = sum(latencies) / len(latencies)
        print(f"  Latency : avg {avg:.1f}s  min {min(latencies):.1f}s  max {max(latencies):.1f}s")
    elif mode == "batch":
        print(f"  Latency : n/a (batch mode — no per-doc timing)")
    print(f"{'─' * 52}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark RERA extraction variants.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── run subcommand ────────────────────────────────────────────────────────
    run_p = sub.add_parser(
        "run",
        help="Run extraction (async or batch).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument(
        "--variant", required=True, choices=["A", "B", "C", "D", "E", "F"],
        help=(
            "A=Flash 2-call  B=Flash single  C=Flash-Lite 2-call  D=Flash-Lite single  "
            "E=GLM-OCR+Luna  F=Qwen3-VL+Luna"
        ),
    )
    run_p.add_argument("--state", default="GJ", help="State code (default: GJ)")
    run_p.add_argument(
        "--run-id", default=None,
        help="Pre-generated run ID (used when launched programmatically, e.g. from the UI)",
    )
    run_p.add_argument(
        "--mode", default="async", choices=["async", "batch"],
        help="async=concurrent calls (default)  batch=Gemini Batch API",
    )
    run_p.add_argument(
        "--concurrency", type=int, default=10,
        help="Max concurrent requests in async mode (default: 10)",
    )
    run_p.add_argument("--limit", type=int, default=None, help="Process at most N docs")
    run_p.add_argument(
        "--subset", default=None,
        help="Path to JSON with list of {local_path} dicts to restrict docs",
    )
    run_p.add_argument(
        "--batch-poll-tries", type=int, default=8,
        help="Max poll attempts before saving job file and exiting (default: 8)",
    )
    run_p.add_argument(
        "--batch-poll-interval", type=int, default=15,
        help="Seconds between batch polls (default: 15)",
    )
    run_p.add_argument(
        "--prompt-version", default=CURRENT_VERSION,
        help=f"Prompt version to use (default: {CURRENT_VERSION})",
    )

    # ── fetch subcommand ──────────────────────────────────────────────────────
    fetch_p = sub.add_parser(
        "fetch",
        help="Check a run's batch jobs and download results when all are done.",
    )
    fetch_p.add_argument(
        "--run-id", required=True,
        help="Run ID printed by `run --mode batch` (e.g. A_GJ_20260806_120000)",
    )
    fetch_p.add_argument(
        "--poll-tries", type=int, default=8,
        help="Max poll attempts per chunk before giving up (default: 8)",
    )
    fetch_p.add_argument(
        "--poll-interval", type=int, default=15,
        help="Seconds between polls (default: 15)",
    )

    args = parser.parse_args()

    # ── run ───────────────────────────────────────────────────────────────────
    if args.command == "run":
        # Validate required env vars for this variant
        if args.variant in GEMINI_VARIANTS and not _gemini_configured():
            sys.exit("Error: set GEMINI_PROJECT (Vertex AI) or GEMINI_API_KEY (AI Studio) for variants A–D.")
        if args.variant in OPENAI_VARIANTS and "OPENAI_API_KEY" not in os.environ:
            sys.exit("Error: OPENAI_API_KEY is required for variants E and F.")
        if args.variant == "E" and "GLM_OCR_API_KEY" not in os.environ:
            sys.exit("Error: GLM_OCR_API_KEY is required for variant E (GLM-OCR via z.ai API).")
        if args.variant == "F" and "OPENROUTER_API_KEY" not in os.environ:
            sys.exit("Error: OPENROUTER_API_KEY is required for variant F (Qwen3-VL via OpenRouter).")

        model     = VARIANT_MODEL[args.variant]
        call_type = VARIANT_CALL[args.variant]
        docs      = load_docs(args.state, args.subset, args.limit)
        prompts   = get_prompts(args.state, args.prompt_version)

        run_id  = args.run_id or _make_run_id(args.variant, args.state)
        run_dir = _init_run(run_id, args.variant, args.state, args.mode, args.prompt_version)

        print(f"\nBenchmark")
        print(f"  run ID      : {run_id}")
        print(f"  variant     : {args.variant}  ({call_type})")
        print(f"  model       : {model}")
        print(f"  mode        : {args.mode}")
        print(f"  state       : {args.state}")
        print(f"  prompts     : {args.prompt_version}")
        print(f"  docs        : {len(docs)}")
        if args.mode == "async":
            print(f"  concurrency : {args.concurrency}")
        else:
            print(f"  poll tries  : {args.batch_poll_tries} × {args.batch_poll_interval}s")
        print()

        if args.mode == "async":
            results = asyncio.run(run_async(docs, args.variant, args.concurrency, run_dir=run_dir, prompts=prompts))
        else:
            results = submit_batch(
                docs, args.variant, args.state, run_dir,
                args.batch_poll_tries, args.batch_poll_interval,
                prompts=prompts,
            )
            if results is None:
                # Some chunks still running — tell user how to retrieve later
                print(f"\n  Run ID : {run_id}")
                print(f"  Retrieve results when ready:")
                print(f"    python scripts/benchmark.py fetch --run-id {run_id}")
                return

        out_path = save_results(results, run_dir)
        _mark_run_complete(run_dir, "results.jsonl")
        print_summary(results, args.mode)
        print(f"  run ID   : {run_id}")
        print(f"  Results  → {out_path}")

    # ── fetch ─────────────────────────────────────────────────────────────────
    elif args.command == "fetch":
        fetch_run(args.run_id, args.poll_tries, args.poll_interval)


if __name__ == "__main__":
    main()
