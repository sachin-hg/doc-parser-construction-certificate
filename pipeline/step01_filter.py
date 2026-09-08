"""
Step 1: Read Excel, filter target states, deduplicate per RERA ID, output manifest.

Selection logic when multiple docs exist for a RERA ID:
  1. Deduplicate by exact URL
  2. Prefer URLs containing form/architect keywords
  3. Deprioritize photo/image URLs
  4. Among tied candidates, prefer latest date pattern in filename
  5. Fall back to first candidate, log warning
"""
import re
import json
import logging
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from pipeline.config import (
    EXCEL_PATH, EXCEL_SHEET, TARGET_STATES, EXCEL_COLUMNS,
    MANIFEST_PATH, DROPPED_PATH, OUTPUT_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

PREFER_KEYWORDS = [
    '_Form', '(Form', 'Form.pdf', 'Form8', 'Form1', 'Form_1',
    'Form_8', 'Form-8', 'Architect', 'Form-1', 'Form4', 'Form_4', 'Form-4',
]
DEPRIORITIZE_KEYWORDS = [
    'Photo', 'photo', 'Image', 'image', 'Front', 'front', 'Back', 'back',
    'WhatsApp', 'Layout', 'layout',
    # UP RERA officer site inspection reports (not architect certificates)
    'Site_Inspection', 'site_inspection', 'Inspection_Document',
]

# Matches YYYY_MM or YYYY-MM optionally followed by -DD or _DD
_DATE_RE = re.compile(r'(\d{4})[_-](\d{2})(?:[_-](\d{2}))?')


def _score_url(url: str) -> tuple:
    """Return (prefer_score, deprioritize_score, date_tuple) for ranking.

    Sort key: prefer_score DESC, deprioritize_score ASC, date DESC.
    """
    prefer = sum(1 for kw in PREFER_KEYWORDS if kw in url)
    deprioritize = sum(1 for kw in DEPRIORITIZE_KEYWORDS if kw in url)

    # Look for dates in the filename portion only (skip bucket prefix like /2026_03/)
    path_parts = urlparse(url).path.strip('/').split('/')
    filename_part = '/'.join(path_parts[1:]) if len(path_parts) > 1 else path_parts[0]
    m = _DATE_RE.search(filename_part)
    date = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (0, 0, 0)

    return prefer, deprioritize, date


def _select_url(urls, rera_id: str) -> str:
    if len(urls) == 1:
        return urls[0]

    scored = sorted(
        urls,
        key=lambda u: (-_score_url(u)[0], _score_url(u)[1], tuple(-x for x in _score_url(u)[2])),
    )

    best_prefer, best_dep, _ = _score_url(scored[0])
    second_prefer, second_dep, _ = _score_url(scored[1])

    if best_prefer == second_prefer and best_dep == second_dep:
        logger.warning(
            'Ambiguous selection for rera_id=%s (%d docs), using first: %s',
            rera_id, len(urls), scored[0],
        )
    return scored[0]


def _local_path(url: str) -> str:
    """Strip scheme+host to get the saveable path under documents/."""
    return urlparse(url).path.lstrip('/')


def run() -> list[dict]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info('Reading %s', EXCEL_PATH)
    df = pd.read_excel(EXCEL_PATH, sheet_name=EXCEL_SHEET)
    logger.info('Total rows in sheet: %d', len(df))

    # Keep only needed columns
    missing = [c for c in EXCEL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f'Missing columns in Excel: {missing}')
    df = df[EXCEL_COLUMNS].copy()

    # Filter target states
    df = df[df['state_code'].isin(TARGET_STATES)]
    logger.info('Rows after state filter (%s): %d', TARGET_STATES, len(df))

    df = df.dropna(subset=['url_modified'])

    manifest = []
    warning_count = 0

    for rera_id, group in df.groupby('rera_reg_id'):
        unique_urls = group['url_modified'].unique().tolist()
        selected = _select_url(unique_urls, str(rera_id))
        row = group[group['url_modified'] == selected].iloc[0]

        if len(unique_urls) > 1 and _score_url(selected)[0] == 0:
            warning_count += 1

        manifest.append({
            'rera_reg_id': str(rera_id),
            'project_id': str(row['project_id']) if pd.notna(row['project_id']) else None,
            'project_name': str(row['project_name']) if pd.notna(row['project_name']) else None,
            'state_code': str(row['state_code']),
            'url': selected,
            'local_path': _local_path(selected),
            'total_docs_available': len(unique_urls),
            'all_urls': unique_urls,
        })

    logger.info('Selected documents: %d', len(manifest))
    logger.info('State breakdown: %s', dict(Counter(r['state_code'] for r in manifest)))
    if warning_count:
        logger.warning('%d RERA IDs had ambiguous selections', warning_count)

    # Post-filter: drop entries whose selected URL still matches a deprioritize keyword.
    # This catches projects where *every* available doc is deprioritized (e.g. UP projects
    # that only have Site_Inspection_Document and no architect certificate).
    # Check only the filename portion so CDN hostnames (cloudfront.net) don't false-match
    # keywords like 'front', 'back', 'image'.
    clean_manifest = []
    dropped = []
    for entry in manifest:
        filename = urlparse(entry['url']).path.split('/')[-1]
        hit = next((kw for kw in DEPRIORITIZE_KEYWORDS if kw in filename), None)
        if hit:
            dropped.append({**entry, 'drop_reason': f'selected URL contains deprioritize keyword "{hit}"'})
        else:
            clean_manifest.append(entry)

    if dropped:
        logger.warning(
            'Dropped %d entries whose only available docs match deprioritize keywords '
            '(see %s)', len(dropped), DROPPED_PATH,
        )
        logger.warning('State breakdown of dropped: %s',
                       dict(Counter(r['state_code'] for r in dropped)))
        with open(DROPPED_PATH, 'w') as f:
            json.dump(dropped, f, indent=2, ensure_ascii=False)
        logger.info('Dropped entries written → %s', DROPPED_PATH)

    logger.info('Final manifest: %d documents', len(clean_manifest))
    logger.info('Final state breakdown: %s', dict(Counter(r['state_code'] for r in clean_manifest)))

    with open(MANIFEST_PATH, 'w') as f:
        json.dump(clean_manifest, f, indent=2, ensure_ascii=False)
    logger.info('Manifest written → %s', MANIFEST_PATH)

    return clean_manifest


if __name__ == '__main__':
    run()
