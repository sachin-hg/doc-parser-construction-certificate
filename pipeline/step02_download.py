"""
Step 2: Rate-limited, parallel download of all documents from the manifest.

- Saves files under documents/<url-path> preserving the original directory structure
- Skips already-downloaded files
- Retries with exponential backoff on transient failures
- Writes a results JSON with status per RERA ID
"""
import json
import time
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm

from pipeline.config import (
    MANIFEST_PATH, DOCUMENTS_DIR, DOWNLOAD_RESULTS_PATH, OUTPUT_DIR,
    DOWNLOAD_MAX_WORKERS, DOWNLOAD_RATE_LIMIT, DOWNLOAD_MAX_RETRIES, DOWNLOAD_TIMEOUT,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

_rate_lock = threading.Lock()
_last_request_time = 0.0
_min_interval = 1.0 / DOWNLOAD_RATE_LIMIT

_session = requests.Session()
_session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; RERA-DocParser/1.0)'


def _throttled_get(url: str) -> requests.Response:
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        wait = _min_interval - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()
    return _session.get(url, allow_redirects=True, timeout=DOWNLOAD_TIMEOUT)


def _download_one(item: dict) -> dict:
    rera_id = item['rera_reg_id']
    url = item['url']
    dest = DOCUMENTS_DIR / item['local_path']

    if dest.exists() and dest.stat().st_size > 0:
        return {'status': 'skipped', 'rera_reg_id': rera_id, 'path': str(dest)}

    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(DOWNLOAD_MAX_RETRIES):
        try:
            resp = _throttled_get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return {
                'status': 'ok',
                'rera_reg_id': rera_id,
                'path': str(dest),
                'size_bytes': len(resp.content),
                'content_type': resp.headers.get('Content-Type', ''),
            }
        except Exception as exc:
            backoff = 2 ** attempt
            if attempt < DOWNLOAD_MAX_RETRIES - 1:
                logger.debug('Retry %d/%d for %s after %ds: %s', attempt + 1, DOWNLOAD_MAX_RETRIES, rera_id, backoff, exc)
                time.sleep(backoff)
            else:
                logger.error('Failed %s (%s): %s', rera_id, url, exc)
                return {
                    'status': 'error',
                    'rera_reg_id': rera_id,
                    'url': url,
                    'error': str(exc),
                }


def run(manifest=None) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

    if manifest is None:
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)

    logger.info('Downloading %d documents with %d workers @ %.1f req/s', len(manifest), DOWNLOAD_MAX_WORKERS, DOWNLOAD_RATE_LIMIT)

    counts = {'ok': 0, 'skipped': 0, 'error': 0}
    all_results = []

    with ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS) as pool:
        futures = {pool.submit(_download_one, item): item for item in manifest}
        with tqdm(total=len(manifest), unit='doc') as bar:
            for future in as_completed(futures):
                result = future.result()
                counts[result['status']] = counts.get(result['status'], 0) + 1
                all_results.append(result)
                bar.update(1)
                bar.set_postfix(counts)

    logger.info('Download complete: %s', counts)

    with open(DOWNLOAD_RESULTS_PATH, 'w') as f:
        json.dump({'summary': counts, 'results': all_results}, f, indent=2)
    logger.info('Results written → %s', DOWNLOAD_RESULTS_PATH)

    return counts


if __name__ == '__main__':
    run()
