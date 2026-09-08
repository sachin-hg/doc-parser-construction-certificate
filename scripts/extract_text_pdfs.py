"""
Run pymupdf4llm on every text_pdf from validation_results.json.

Uses ProcessPoolExecutor for parallel extraction across CPU cores.

Usage:
  python3 scripts/extract_text_pdfs.py [--workers N]

  --workers N   parallel worker processes (default: all CPU cores)

Outputs:
  output/extracted_md/<local_path>.extracted.md   — markdown per document
  output/extraction_text_stats.json               — per-doc timing/memory stats
  output/extraction_text_summary.json             — aggregate averages

Skips docs that already have an .extracted.md file (resumable).
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import fitz
import psutil
import pymupdf4llm
from tqdm import tqdm

# Ensure project root is on sys.path in both main and worker processes
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DOCUMENTS_DIR   = Path("documents")
OUTPUT_BASE     = Path("output/extracted_md")
VALIDATION_PATH = Path("output/validation_results.json")
STATS_PATH      = Path("output/extraction_text_stats.json")
SUMMARY_PATH    = Path("output/extraction_text_summary.json")
MAX_PAGES       = 8


# ---------------------------------------------------------------------------
# Helpers — at module level so worker processes can import them
# ---------------------------------------------------------------------------

def resolve_path(local_path):
    p = DOCUMENTS_DIR / local_path
    if p.exists():
        return p
    bare = Path(str(p).rstrip("/"))
    return bare if bare.exists() else None


def _get_missing_bottom_text(doc, page_idx, page_md, clip_frac=0.18):
    """Raw text from the bottom clip_frac of a page that pymupdf4llm dropped.

    Catches table titles that sit at the bottom of page N while the table body
    continues on page N+1 — pymupdf4llm drops them as header-only tables.
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


def _clean_markdown(md):
    """Remove pymupdf4llm rendering artifacts.

    - <br> tags: wrapped lines within PDF table cells → space.
    - ~~strikethrough~~: font-flag misdetection on row numbers / cell text → strip markers.
    """
    md = re.sub(r'\s*<br\s*/?>\s*', ' ', md, flags=re.IGNORECASE)
    md = re.sub(r'~~([^~\n]*)~~', r'\1', md)
    return md


def extract_markdown(path, pages, max_pages):
    """Extract markdown page-by-page, stitching orphaned table titles at page breaks.

    Pages that return 0 chars from pymupdf4llm (scanned images within an otherwise
    text PDF) are annotated with a [SCANNED PAGE] marker so the viewer shows the gap.
    """
    page_list = list(range(min(pages, max_pages)))
    chunks = pymupdf4llm.to_markdown(str(path), pages=page_list, page_chunks=True)
    page_mds = [c['text'] for c in chunks]

    doc = fitz.open(str(path))
    try:
        stitched = [page_mds[0]]
        for i in range(1, len(page_mds)):
            page_md = page_mds[i]
            if not page_md.strip():
                page_md = (
                    f'\n\n> **[Page {page_list[i] + 1}: scanned image'
                    f' — not extractable via text layer]**\n\n'
                )
            else:
                prefix = _get_missing_bottom_text(doc, page_list[i - 1], page_mds[i - 1])
                page_md = prefix + page_md
            stitched.append(page_md)
    finally:
        doc.close()

    return _clean_markdown('\n\n'.join(stitched))


# ---------------------------------------------------------------------------
# Worker — must be at module level for ProcessPoolExecutor (spawn mode)
# ---------------------------------------------------------------------------

def process_doc(item):
    """Extract one document and write its .extracted.md file.

    Returns a stats dict on success, None if skipped (already exists),
    or a dict with ok=False on error.
    """
    local_path = item["local_path"]
    rera_id    = item["rera_reg_id"]
    state      = item["state_code"]
    pages      = item.get("pages") or MAX_PAGES

    out_path = OUTPUT_BASE / (local_path + ".extracted.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and out_path.stat().st_size > 0:
        return None  # resumable: already done

    path = resolve_path(local_path)
    if not path:
        return {"rera_id": rera_id, "state": state, "local_path": local_path,
                "ok": False, "elapsed_s": 0, "reason": "file not found"}

    proc = psutil.Process(os.getpid())
    t0   = time.perf_counter()
    cpu0 = proc.cpu_times()

    try:
        md = extract_markdown(path, pages, MAX_PAGES)
        ok = True
    except Exception as exc:
        md = f"[EXTRACTION ERROR: {exc}]"
        ok = False

    t1   = time.perf_counter()
    cpu1 = proc.cpu_times()
    mem1 = proc.memory_info().rss

    out_path.write_text(md, encoding="utf-8")

    return {
        "rera_id":    rera_id,
        "state":      state,
        "local_path": local_path,
        "pages":      min(pages, MAX_PAGES),
        "md_chars":   len(md),
        "ok":         ok,
        "elapsed_s":  round(t1 - t0, 4),
        "cpu_user_s": round(cpu1.user   - cpu0.user,   4),
        "cpu_sys_s":  round(cpu1.system - cpu0.system, 4),
        "mem_rss_mb": round(mem1 / 1024 / 1024, 2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--workers", type=int, default=os.cpu_count(),
        help=f"Parallel worker processes (default: {os.cpu_count()} — all CPU cores)",
    )
    args = parser.parse_args()

    with open(VALIDATION_PATH) as f:
        results = json.load(f)["results"]

    text_pdfs = [r for r in results if r["doc_type"] == "text_pdf"]
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    print(f"text_pdf docs : {len(text_pdfs)}")
    print(f"workers       : {args.workers}")
    print()

    stats  = []
    errors = []
    wall_start = time.perf_counter()

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_doc, item): item for item in text_pdfs}
        with tqdm(total=len(futures), unit="doc", desc="Extracting") as pbar:
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    item = futures[future]
                    errors.append({"rera_id": item["rera_reg_id"], "reason": str(exc)})
                else:
                    if result is None:
                        pass  # skipped (resumable)
                    elif not result.get("ok"):
                        errors.append(result)
                    else:
                        stats.append(result)
                pbar.update(1)

    wall_total = time.perf_counter() - wall_start

    # ── persist per-doc stats ─────────────────────────────────────────────────
    with open(STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)

    # ── aggregate summary ─────────────────────────────────────────────────────
    ok_stats = [s for s in stats if s.get("ok")]
    if ok_stats:
        times      = [s["elapsed_s"]                       for s in ok_stats]
        cpu_totals = [s["cpu_user_s"] + s["cpu_sys_s"]     for s in ok_stats]
        mem_rss    = [s["mem_rss_mb"]                      for s in ok_stats]
        chars      = [s["md_chars"]                        for s in ok_stats]

        by_state = {}
        for s in ok_stats:
            by_state.setdefault(s["state"], []).append(s["elapsed_s"])

        summary = {
            "total_docs":        len(text_pdfs),
            "extracted":         len(ok_stats),
            "errors":            len(errors),
            "workers":           args.workers,
            "wall_time_s":       round(wall_total, 1),
            "time_avg_s":        round(statistics.mean(times),       4),
            "time_median_s":     round(statistics.median(times),     4),
            "time_p95_s":        round(sorted(times)[int(len(times) * 0.95)], 4),
            "time_max_s":        round(max(times),                   4),
            "time_total_s":      round(sum(times),                   1),
            "cpu_avg_s":         round(statistics.mean(cpu_totals),  4),
            "mem_peak_rss_mb":   round(max(mem_rss),                 2),
            "mem_avg_rss_mb":    round(statistics.mean(mem_rss),     2),
            "md_chars_avg":      round(statistics.mean(chars),       0),
            "md_chars_median":   round(statistics.median(chars),     0),
            "by_state": {
                st: {
                    "count":   len(ts),
                    "avg_s":   round(statistics.mean(ts), 4),
                    "total_s": round(sum(ts), 2),
                }
                for st, ts in sorted(by_state.items())
            },
        }
    else:
        summary = {"extracted": 0, "errors": len(errors)}

    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    # ── print summary ─────────────────────────────────────────────────────────
    print(f"\n{'─'*52}")
    print(f"  Extracted  : {summary.get('extracted', 0)} / {summary.get('total_docs', '?')}")
    print(f"  Errors     : {summary.get('errors', 0)}")
    if ok_stats:
        speedup = round(summary['time_total_s'] / wall_total, 1)
        print(f"  Wall time  : {wall_total:.0f}s  (per-doc sum {summary['time_total_s']}s"
              f"  →  {speedup}× speedup)")
        print(f"  Per-doc    : avg {summary['time_avg_s']}s  "
              f"median {summary['time_median_s']}s  "
              f"p95 {summary['time_p95_s']}s")
        print(f"  CPU/doc    : avg {summary['cpu_avg_s']}s (user+sys)")
        print(f"  Memory     : avg RSS {summary['mem_avg_rss_mb']} MB  "
              f"peak RSS {summary['mem_peak_rss_mb']} MB")
        print(f"  Markdown   : avg {summary['md_chars_avg']:.0f} chars  "
              f"median {summary['md_chars_median']:.0f}")
        print(f"\n  By state:")
        for st, d in summary["by_state"].items():
            print(f"    {st}: {d['count']} docs  avg {d['avg_s']}s")
    print(f"{'─'*52}")
    print(f"  Stats   → {STATS_PATH}")
    print(f"  Summary → {SUMMARY_PATH}")
    print(f"  Output  → {OUTPUT_BASE}/")


if __name__ == "__main__":
    main()
