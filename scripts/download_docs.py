#!/usr/bin/env python3
"""
Download PDF documents from their source URLs.

All document URLs are stored in output/manifest.json (CloudFront CDN).
By default, downloads only documents that appear in at least one benchmark run.
Use --all to download the complete document set.

Usage:
    python scripts/download_docs.py               # run-referenced docs only
    python scripts/download_docs.py --all         # every doc in manifest
    python scripts/download_docs.py --state HR    # filter by state code
    python scripts/download_docs.py --workers 8   # parallel workers (default 5)
    python scripts/download_docs.py --dry-run     # print what would be downloaded
"""

import argparse
import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("requests is not installed — run: pip install requests")

ROOT = Path(__file__).parent.parent
MANIFEST_PATH = ROOT / "output" / "manifest.json"
RUN_DIR = ROOT / "output" / "benchmark_results" / "runs"
DOCUMENTS_DIR = ROOT / "documents"


def load_manifest() -> dict[str, str]:
    """Return {local_path: url} from output/manifest.json."""
    if not MANIFEST_PATH.exists():
        sys.exit(f"Manifest not found: {MANIFEST_PATH}")
    with open(MANIFEST_PATH) as f:
        data = json.load(f)
    return {entry["local_path"]: entry["url"] for entry in data if "local_path" in entry and "url" in entry}


def load_run_paths() -> set[str]:
    """Return the set of local_paths referenced by any completed run."""
    if not RUN_DIR.exists():
        return set()
    paths: set[str] = set()
    for run_dir in RUN_DIR.iterdir():
        for fname in ["results.jsonl", "chunk_0.jsonl", "chunk_1.jsonl", "chunk_2.jsonl", "chunk_3.jsonl"]:
            rfile = run_dir / fname
            if not rfile.exists():
                continue
            with open(rfile) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        lp = json.loads(line).get("local_path")
                        if lp:
                            paths.add(lp)
                    except json.JSONDecodeError:
                        pass
    return paths


def download_one(local_path: str, url: str, skip_existing: bool = True) -> tuple[str, str]:
    """Download a single PDF. Returns (local_path, status)."""
    dest = DOCUMENTS_DIR / local_path
    if skip_existing and dest.exists():
        return local_path, "skipped"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return local_path, "ok"
    except Exception as exc:
        return local_path, f"error: {exc}"


def main():
    parser = argparse.ArgumentParser(description="Download RERA construction certificate PDFs")
    parser.add_argument("--all", action="store_true", help="Download all docs in manifest (not just run-referenced)")
    parser.add_argument("--state", metavar="CODE", help="Filter by state code, e.g. HR, GJ, MH, UP, TS")
    parser.add_argument("--workers", type=int, default=5, metavar="N", help="Parallel download workers (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be downloaded without downloading")
    parser.add_argument("--force", action="store_true", help="Re-download even if file already exists")
    args = parser.parse_args()

    manifest = load_manifest()
    print(f"Manifest: {len(manifest)} total documents")

    if args.all:
        candidates = manifest
    else:
        run_paths = load_run_paths()
        print(f"Run-referenced: {len(run_paths)} documents")
        candidates = {p: u for p, u in manifest.items() if p in run_paths}

    if args.state:
        state_filter = f"/{args.state}/"
        candidates = {p: u for p, u in candidates.items() if state_filter in p or f"/{args.state.title()}/" in p}
        # Also handle full state name patterns from local_path
        state_name_map = {"HR": "Haryana", "GJ": "Gujarat", "MH": "Maharashtra", "UP": "Uttar Pradesh", "TS": "Telangana"}
        state_name = state_name_map.get(args.state.upper(), "")
        if state_name:
            extra = {p: u for p, u in manifest.items() if state_name in p}
            if args.all:
                candidates.update(extra)
            else:
                run_paths = load_run_paths() if not args.all else set()
                candidates.update({p: u for p, u in extra.items() if p in run_paths})

    already_present = sum(1 for p in candidates if (DOCUMENTS_DIR / p).exists())
    to_download = {p: u for p, u in candidates.items() if not (DOCUMENTS_DIR / p).exists() or args.force}

    print(f"To download: {len(to_download)}  |  Already present: {already_present}  |  Total target: {len(candidates)}")

    if args.dry_run:
        for p in sorted(to_download):
            print(f"  {p}")
        return

    if not to_download:
        print("Nothing to download.")
        return

    ok = skipped = errors = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, p, u, not args.force): p for p, u in to_download.items()}
        for i, future in enumerate(as_completed(futures), 1):
            local_path, status = future.result()
            if status == "ok":
                ok += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
                print(f"  FAIL {local_path}: {status}")
            if i % 50 == 0 or i == len(futures):
                elapsed = time.time() - start
                print(f"  {i}/{len(futures)} done  ({ok} ok, {errors} errors)  [{elapsed:.0f}s]")

    print(f"\nDone: {ok} downloaded, {skipped} skipped, {errors} errors in {time.time()-start:.0f}s")


if __name__ == "__main__":
    main()
