"""
Step 3: Validate downloaded PDFs and classify them for the extraction pipeline.

doc_type classification:
  text_pdf    : valid PDF with clean parseable text (→ Tier 1: pdfplumber + Claude text API)
  garbled_pdf : valid PDF with text but encoding is corrupted (→ Tier 2: render + Claude Vision)
  scanned_pdf : valid PDF, pages are images with no text layer (→ Tier 2: render + Claude Vision)
  invalid     : corrupt, empty, or unreadable file
  missing     : file was not downloaded

Outputs validation_results.json.
"""
import json
import logging
from pathlib import Path

import fitz  # PyMuPDF

from pipeline.config import (
    MANIFEST_PATH, DOCUMENTS_DIR, VALIDATION_RESULTS_PATH, OUTPUT_DIR,
    TEXT_CHARS_PER_PAGE_THRESHOLD,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


def _is_garbled(text: str) -> bool:
    """Detect font-corrupted text extraction (CID fonts with missing ToUnicode map).

    Returns True if the extracted text is likely garbage despite being non-empty.
    """
    if not text or len(text) < 50:
        return False  # too short to judge; let char count determine doc_type

    # Ratio of printable ASCII or common whitespace to total chars
    printable = sum(1 for c in text if 32 <= ord(c) < 127 or c in '\n\r\t')
    printable_ratio = printable / len(text)

    # Ratio of word-like tokens (alpha, length >= 3)
    tokens = text.split()
    word_ratio = sum(1 for t in tokens if t.isalpha() and len(t) >= 3) / max(len(tokens), 1)

    return printable_ratio < 0.85 or word_ratio < 0.10


def _classify_pdf(path: Path) -> dict:
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        return {'doc_type': 'invalid', 'error': str(exc), 'pages': 0}

    pages = len(doc)
    if pages == 0:
        doc.close()
        return {'doc_type': 'invalid', 'error': 'empty PDF', 'pages': 0}

    total_chars = 0
    pages_with_text = 0
    pages_image_only = 0
    all_text = []

    for page in doc:
        text = page.get_text('text')
        stripped = text.strip()
        total_chars += len(stripped)
        all_text.append(stripped)
        if len(stripped) > 20:
            pages_with_text += 1
        if page.get_images(full=False) and len(stripped) < 20:
            pages_image_only += 1

    doc.close()

    avg_chars = total_chars / pages
    full_text = ' '.join(all_text)

    if avg_chars < TEXT_CHARS_PER_PAGE_THRESHOLD:
        doc_type = 'scanned_pdf'
    elif _is_garbled(full_text):
        doc_type = 'garbled_pdf'
    else:
        doc_type = 'text_pdf'

    return {
        'doc_type': doc_type,
        'pages': pages,
        'total_chars': total_chars,
        'avg_chars_per_page': round(avg_chars, 1),
        'pages_with_text': pages_with_text,
        'pages_image_only': pages_image_only,
    }


def _resolve_path(item: dict) -> Path:
    """Find the actual file on disk — handles TS files without .pdf extension."""
    candidate = DOCUMENTS_DIR / item['local_path']
    if candidate.exists():
        return candidate
    # TS URLs have no extension; try the bare path
    bare = Path(str(candidate).rstrip('/'))
    if bare.exists():
        return bare
    return candidate  # return non-existent path so caller can mark as missing


def run(manifest=None) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if manifest is None:
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)

    logger.info('Validating %d documents', len(manifest))

    results = []
    counts = {'text_pdf': 0, 'garbled_pdf': 0, 'scanned_pdf': 0, 'invalid': 0, 'missing': 0}

    for item in manifest:
        path = _resolve_path(item)

        if not path.exists():
            classification = {'doc_type': 'missing'}
        else:
            classification = _classify_pdf(path)

        doc_type = classification['doc_type']
        counts[doc_type] = counts.get(doc_type, 0) + 1

        results.append({
            'rera_reg_id': item['rera_reg_id'],
            'state_code': item['state_code'],
            'local_path': item['local_path'],
            **classification,
        })

    logger.info('Validation complete: %s', counts)

    # Tier breakdown for extraction planning
    tier1 = counts.get('text_pdf', 0)
    tier2 = counts.get('scanned_pdf', 0) + counts.get('garbled_pdf', 0)
    logger.info('Tier 1 (text → Claude text API): %d | Tier 2 (image → Claude Vision): %d', tier1, tier2)

    with open(VALIDATION_RESULTS_PATH, 'w') as f:
        json.dump({'summary': counts, 'results': results}, f, indent=2)
    logger.info('Results written → %s', VALIDATION_RESULTS_PATH)

    return counts


if __name__ == '__main__':
    run()
