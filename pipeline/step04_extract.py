"""
Step 4: Extract structured data from validated PDFs using a two-tier approach.

Tier 1 (text_pdf):
  pymupdf4llm markdown extraction → Claude Haiku 4.5 text API
  Falls back to Tier 2 if extracted markdown is too sparse (< 150 chars/page
  average), which catches text_pdfs with scrambled/mirrored font encoding
  that slip through the garbled heuristic (e.g. some Haryana docs).

Tier 2 (scanned_pdf, garbled_pdf, sparse text_pdf):
  PyMuPDF render at 150 DPI → Claude Haiku 4.5 Vision API

Uses the Anthropic Batch API for 50% cost savings on bulk processing.
Batch creation is resumable: if extraction_batch_id.txt exists the script
skips to polling and result collection.

Outputs:
  output/extraction_results.json   — list of per-document structured extractions
  output/extraction_batch_id.txt   — saved batch ID for resuming interrupted runs
"""
import base64
import json
import logging
import re
import time
from pathlib import Path

import anthropic
import fitz  # PyMuPDF
import pymupdf4llm
from tqdm import tqdm

from pipeline.config import (
    DOCUMENTS_DIR, OUTPUT_DIR,
    VALIDATION_RESULTS_PATH,
    EXTRACTION_MODEL, EXTRACTION_MAX_TOKENS,
    EXTRACTION_RENDER_DPI, EXTRACTION_MAX_PAGES,
    EXTRACTION_MIN_CHARS_PER_PAGE,
    EXTRACTION_RESULTS_PATH, EXTRACTION_BATCH_ID_PATH,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are extracting structured data from an Indian RERA (Real Estate Regulatory
Authority) construction progress certificate (also called an architect
certificate or engineer certificate). These are quarterly reports filed by
licensed architects/engineers certifying construction progress for real-estate
projects registered under RERA.

Different Indian states use different form formats:
- Gujarat (GJ): Form-8, sections A–K
- Haryana (HR): Annexure A, multi-tower layout
- Maharashtra (MH): Form 1
- Uttar Pradesh (UP): activity-wise table
- Telangana (TS): F1 multi-tower columns

Extract the fields below and return ONLY valid JSON — no markdown fences,
no prose, no explanation:

{
  "project_name": "string or null",
  "rera_id": "string or null",
  "developer_name": "string or null",
  "architect_name": "string or null",
  "architect_registration": "string or null",
  "report_date": "YYYY-MM-DD or YYYY-MM or any date string found, or null",
  "report_quarter": "e.g. Q3 2024-25, or null",
  "table_a": [
    {
      "tower_name": "string (Tower 1 / Wing A / Building / etc.)",
      "activities": [
        {
          "activity_name": "string",
          "completion_pct": number_0_to_100_or_null,
          "remarks": "string or null"
        }
      ]
    }
  ],
  "table_b_c": [
    {
      "area_name": "string (Common Area / Amenity / Infrastructure / etc.)",
      "activity_name": "string or null",
      "completion_pct": number_0_to_100_or_null,
      "remarks": "string or null"
    }
  ],
  "overall_completion_pct": number_0_to_100_or_null,
  "extraction_notes": "any caveats, ambiguities, or important observations"
}

Rules:
- table_a: tower/wing/building-wise construction activities and their completion %.
- table_b_c: common areas, amenities, infrastructure, clubhouse, etc.
- For a single-tower project, table_a has one entry with tower_name = "Main Building"
  or whatever the document calls it.
- Return [] for a table that is absent, never omit the key.
- completion_pct must be a number (0-100) or null if genuinely not found.
- Do not infer or hallucinate values not present in the document.\
"""

_USER_PROMPT_TEXT = """\
Below is the extracted text content of the document (one section per page).
Extract the structured data as described.

{text}\
"""

_USER_PROMPT_VISION = """\
The following images are pages of a RERA construction certificate document.
Extract the structured data as described.\
"""

# ---------------------------------------------------------------------------
# Path resolver (mirrors step03 to keep steps independent)
# ---------------------------------------------------------------------------

def _resolve_path(item):
    candidate = DOCUMENTS_DIR / item['local_path']
    if candidate.exists():
        return candidate
    bare = Path(str(candidate).rstrip('/'))
    if bare.exists():
        return bare
    return candidate


# ---------------------------------------------------------------------------
# Tier 1: structured markdown extraction via pymupdf4llm
# ---------------------------------------------------------------------------

def _clean_markdown(md):
    """Remove pymupdf4llm rendering artifacts.

    - <br> tags: represent wrapped lines within PDF table cells; replace with
      a space so words don't run together.
    - ~~strikethrough~~: font-flag misdetection on some PDFs (row numbers,
      certain cell text rendered with an unusual glyph flag). Strip markers,
      keep content.
    """
    md = re.sub(r'\s*<br\s*/?>\s*', ' ', md, flags=re.IGNORECASE)
    md = re.sub(r'~~([^~\n]*)~~', r'\1', md)
    return md


def _get_missing_bottom_text(doc, page_idx, page_md, clip_frac=0.18):
    """Raw text from the bottom clip_frac of a page that pymupdf4llm didn't emit.

    When a table title sits at the bottom of page N with the body on page N+1,
    pymupdf4llm sees a header-only table and drops it. This grabs that text
    from PyMuPDF's raw block extraction so we can prepend it to page N+1.
    """
    page = doc[page_idx]
    r = page.rect
    clip = fitz.Rect(r.x0, r.y0 + r.height * (1.0 - clip_frac), r.x1, r.y1)
    raw = page.get_text('text', clip=clip).strip()
    if not raw:
        return ''
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ''
    # Compare against the TAIL of page markdown only (~300 chars) so body text
    # elsewhere on the page doesn't cause false-positive matches.
    tail = page_md[-300:] if len(page_md) > 300 else page_md
    missing = []
    for ln in lines:
        # Cell-aware pattern: ln must appear as a standalone word/cell token,
        # not embedded inside a longer word (e.g. "C" won't match "Certificate").
        pattern = r'(?:^|[|\s])' + re.escape(ln) + r'(?:$|[|\s])'
        if not re.search(pattern, tail, re.IGNORECASE | re.MULTILINE):
            missing.append(ln)
    return ('\n'.join(missing) + '\n\n') if missing else ''


def _extract_markdown(path, pages):
    """Return markdown from a text PDF using pymupdf4llm, capped at EXTRACTION_MAX_PAGES.

    Processes page-by-page via page_chunks=True, then stitches raw text from
    the bottom of each page to recover table titles that get dropped when a
    table header straddles a page break (title on page N, body on page N+1).

    Returns (markdown_str, is_sparse) where is_sparse=True means the doc
    should be routed to Tier 2 instead (scrambled font encoding slipped through
    the garbled heuristic).
    """
    page_list = list(range(min(pages, EXTRACTION_MAX_PAGES)))
    try:
        chunks = pymupdf4llm.to_markdown(str(path), pages=page_list, page_chunks=True)
        page_mds = [c['text'] for c in chunks]
    except Exception as exc:
        logger.warning('pymupdf4llm failed on %s: %s', path, exc)
        return '', True

    if not any(c.strip() for c in page_mds):
        return '', True, []

    # Identify image-only pages (0 chars from pymupdf4llm — scanned page in an otherwise text PDF)
    image_pages = [page_list[i] for i, md in enumerate(page_mds) if not md.strip()]

    doc = fitz.open(str(path))
    try:
        stitched = [page_mds[0]]
        for i in range(1, len(page_mds)):
            prefix = _get_missing_bottom_text(doc, page_list[i - 1], page_mds[i - 1])
            stitched.append(prefix + page_mds[i])
    finally:
        doc.close()

    combined = _clean_markdown('\n\n'.join(stitched))
    avg_chars = len(combined) / max(len(page_list), 1)
    is_sparse = avg_chars < EXTRACTION_MIN_CHARS_PER_PAGE
    return combined, is_sparse, image_pages


def _build_tier1_content(md):
    return [
        {
            'type': 'text',
            'text': _USER_PROMPT_TEXT.format(text=md),
        }
    ]


# ---------------------------------------------------------------------------
# Tier 2: image rendering via PyMuPDF
# ---------------------------------------------------------------------------

def _render_images(path):
    """Render PDF pages to base64-encoded PNG strings at EXTRACTION_RENDER_DPI."""
    images = []
    try:
        doc = fitz.open(str(path))
        scale = EXTRACTION_RENDER_DPI / 72.0
        mat = fitz.Matrix(scale, scale)
        for i, page in enumerate(doc):
            if i >= EXTRACTION_MAX_PAGES:
                break
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            images.append(base64.b64encode(pix.tobytes('png')).decode())
        doc.close()
    except Exception as exc:
        logger.warning('PyMuPDF render failed on %s: %s', path, exc)
    return images


def _render_specific_pages(path, page_indices):
    """Render specific page indices to base64 PNG (for image-only pages in mixed PDFs)."""
    images = []
    try:
        doc = fitz.open(str(path))
        scale = EXTRACTION_RENDER_DPI / 72.0
        mat = fitz.Matrix(scale, scale)
        for idx in page_indices:
            if idx < len(doc):
                pix = doc[idx].get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                images.append(base64.b64encode(pix.tobytes('png')).decode())
        doc.close()
    except Exception as exc:
        logger.warning('PyMuPDF render failed on %s pages %s: %s', path, page_indices, exc)
    return images


def _build_tier2_content(images):
    content = [{'type': 'text', 'text': _USER_PROMPT_VISION}]
    for img_b64 in images:
        content.append({
            'type': 'image',
            'source': {
                'type': 'base64',
                'media_type': 'image/png',
                'data': img_b64,
            },
        })
    return content


def _build_mixed_content(md, extra_images):
    """Text markdown for text pages + rendered images for scanned pages in the same doc."""
    content = [{'type': 'text', 'text': _USER_PROMPT_TEXT.format(text=md)}]
    if extra_images:
        content.append({
            'type': 'text',
            'text': 'The following page images are from the same document and contain additional table data not captured in the text above (scanned pages):',
        })
        for img_b64 in extra_images:
            content.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/png',
                    'data': img_b64,
                },
            })
    return content


# ---------------------------------------------------------------------------
# Batch request construction
# ---------------------------------------------------------------------------

def _make_batch_request(custom_id, content):
    return {
        'custom_id': custom_id,
        'params': {
            'model': EXTRACTION_MODEL,
            'max_tokens': EXTRACTION_MAX_TOKENS,
            'system': _SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': content}],
        },
    }


def _prepare_requests(valid_items):
    """Build batch requests for all extractable documents.

    Returns (requests_list, items_by_id) where items_by_id maps custom_id →
    manifest+validation fields so results can be annotated.
    """
    requests = []
    items_by_id = {}
    skipped = 0

    logger.info('Preparing batch requests for %d items', len(valid_items))

    for item in tqdm(valid_items, unit='doc', desc='Preparing'):
        rera_id = item['rera_reg_id']
        doc_type = item['doc_type']

        if doc_type in ('invalid', 'missing'):
            skipped += 1
            continue

        path = _resolve_path(item)
        if not path.exists():
            skipped += 1
            continue

        pages = item.get('pages', EXTRACTION_MAX_PAGES) or EXTRACTION_MAX_PAGES

        if doc_type == 'text_pdf':
            md, is_sparse, image_pages = _extract_markdown(path, pages)
            if is_sparse:
                logger.warning(
                    'text_pdf %s has sparse/scrambled text (%.0f chars/pg avg) — routing to Tier 2',
                    rera_id, len(md) / max(pages, 1),
                )
                # fall through to Tier 2 below
            elif image_pages:
                # Mixed PDF: some pages are text, others are scanned images
                extra_imgs = _render_specific_pages(path, image_pages)
                content = _build_mixed_content(md, extra_imgs)
                tier = 1
                logger.debug('%s: mixed PDF — %d image-only page(s): %s', rera_id, len(image_pages), image_pages)
            else:
                content = _build_tier1_content(md)
                tier = 1

        if doc_type != 'text_pdf' or is_sparse:
            # scanned_pdf, garbled_pdf, or sparse text_pdf → vision
            images = _render_images(path)
            if not images:
                logger.warning('No images rendered from %s (%s)', rera_id, path)
                skipped += 1
                continue
            content = _build_tier2_content(images)
            tier = 2

        items_by_id[rera_id] = {
            'rera_reg_id': rera_id,
            'state_code': item['state_code'],
            'local_path': item['local_path'],
            'doc_type': doc_type,
            'tier': tier,
        }
        requests.append(_make_batch_request(rera_id, content))

    logger.info('Prepared %d requests; skipped %d (invalid/missing/unreadable)', len(requests), skipped)
    return requests, items_by_id


# ---------------------------------------------------------------------------
# Batch lifecycle
# ---------------------------------------------------------------------------

def _submit_batch(client, requests):
    logger.info('Submitting batch of %d requests to Claude API...', len(requests))
    batch = client.messages.batches.create(requests=requests)
    batch_id = batch.id
    EXTRACTION_BATCH_ID_PATH.write_text(batch_id)
    logger.info('Batch submitted: %s  (ID saved to %s)', batch_id, EXTRACTION_BATCH_ID_PATH)
    return batch_id


def _poll_batch(client, batch_id):
    """Block until the batch ends, logging progress every 30 s."""
    logger.info('Polling batch %s (this may take minutes to hours for large batches)...', batch_id)
    poll_interval = 30
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = batch.processing_status
        counts = batch.request_counts
        logger.info(
            'Batch status: %s | succeeded=%s errored=%s processing=%s canceling=%s expired=%s',
            status,
            counts.succeeded, counts.errored, counts.processing,
            counts.canceling, counts.expired,
        )
        if status == 'ended':
            break
        time.sleep(poll_interval)

    logger.info(
        'Batch complete: succeeded=%d errored=%d',
        batch.request_counts.succeeded,
        batch.request_counts.errored,
    )


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_json_response(text):
    """Extract JSON from Claude's response, stripping any accidental markdown fences."""
    # strip ```json ... ``` fences if present
    text = text.strip()
    fence = re.match(r'^```(?:json)?\s*(.*?)```\s*$', text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first {...} block
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


def _collect_results(client, batch_id, items_by_id):
    results = []
    counts = {'success': 0, 'parse_error': 0, 'api_error': 0}

    logger.info('Collecting batch results for %s', batch_id)
    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        meta = items_by_id.get(custom_id, {'rera_reg_id': custom_id})

        if result.result.type == 'succeeded':
            raw_text = result.result.message.content[0].text
            parsed = _parse_json_response(raw_text)
            if parsed is not None:
                counts['success'] += 1
                results.append({
                    **meta,
                    'status': 'success',
                    **parsed,
                })
            else:
                counts['parse_error'] += 1
                logger.warning('JSON parse failed for %s; storing raw response', custom_id)
                results.append({
                    **meta,
                    'status': 'parse_error',
                    'raw_response': raw_text,
                })
        else:
            counts['api_error'] += 1
            error_info = getattr(result.result, 'error', None)
            logger.warning('API error for %s: %s', custom_id, error_info)
            results.append({
                **meta,
                'status': 'api_error',
                'error': str(error_info),
            })

    logger.info('Result collection complete: %s', counts)
    return results, counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not VALIDATION_RESULTS_PATH.exists():
        raise FileNotFoundError(
            f'Validation results not found at {VALIDATION_RESULTS_PATH}. '
            'Run step 3 first.'
        )
    with open(VALIDATION_RESULTS_PATH) as f:
        validation = json.load(f)

    valid_items = validation['results']
    logger.info('Loaded %d validated items', len(valid_items))

    client = anthropic.Anthropic()

    # Resumable: skip batch creation if we already have a batch ID
    if EXTRACTION_BATCH_ID_PATH.exists():
        batch_id = EXTRACTION_BATCH_ID_PATH.read_text().strip()
        logger.info('Resuming existing batch %s', batch_id)
    else:
        requests, items_by_id = _prepare_requests(valid_items)
        if not requests:
            logger.warning('No extractable documents found; nothing to do')
            return {'success': 0, 'parse_error': 0, 'api_error': 0}

        batch_id = _submit_batch(client, requests)
        # Save items_by_id for result annotation after polling
        _items_meta_path = OUTPUT_DIR / 'extraction_items_meta.json'
        with open(_items_meta_path, 'w') as f:
            json.dump(items_by_id, f, indent=2)

    # Load items_by_id (may be from a previous interrupted run)
    _items_meta_path = OUTPUT_DIR / 'extraction_items_meta.json'
    if _items_meta_path.exists():
        with open(_items_meta_path) as f:
            items_by_id = json.load(f)
    else:
        # Rebuild from validation if meta file is missing
        items_by_id = {
            item['rera_reg_id']: {
                'rera_reg_id': item['rera_reg_id'],
                'state_code': item['state_code'],
                'local_path': item['local_path'],
                'doc_type': item['doc_type'],
            }
            for item in valid_items
        }

    _poll_batch(client, batch_id)

    results, counts = _collect_results(client, batch_id, items_by_id)

    with open(EXTRACTION_RESULTS_PATH, 'w', encoding='utf-8') as f:
        json.dump({'summary': counts, 'results': results}, f, indent=2, ensure_ascii=False)
    logger.info('Extraction results written → %s', EXTRACTION_RESULTS_PATH)

    # Clean up batch ID file so a re-run starts fresh
    EXTRACTION_BATCH_ID_PATH.unlink(missing_ok=True)

    return counts


if __name__ == '__main__':
    run()
