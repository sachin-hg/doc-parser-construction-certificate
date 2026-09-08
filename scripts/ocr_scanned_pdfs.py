"""
OCR scanned PDFs using Gemini vision via the Google Gemini API.

Sends each PDF directly as an inline document block — no page rendering needed.
Gemini handles OCR, layout, and table structure internally.

Usage:
  export GOOGLE_API_KEY=your_key
  python3 scripts/ocr_scanned_pdfs.py [--workers N] [--model MODEL]

  --workers N    parallel threads (default: 20, I/O-bound so threads work)
  --model MODEL  Gemini model ID (default: gemini-2.5-flash)
  --limit N      process only first N docs (for testing)

Outputs:
  output/extracted_md/<local_path>.extracted.md   — markdown per document
  output/extraction_scanned_stats.json            — per-doc timing stats
  output/extraction_scanned_summary.json          — aggregate summary

Skips docs that already have an .extracted.md file (resumable).
"""
import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google.genai import types
import google.genai as genai
from tqdm import tqdm

DOCUMENTS_DIR   = Path("documents")
OUTPUT_BASE     = Path("output/extracted_md")
VALIDATION_PATH = Path("output/validation_results.json")
STATS_PATH      = Path("output/extraction_scanned_stats.json")
SUMMARY_PATH    = Path("output/extraction_scanned_summary.json")

DEFAULT_MODEL   = "gemini-2.5-flash"
MAX_PAGES       = 20   # Gemini supports up to 1000; we cap at 20 for these docs

_PROMPT = """\
You are extracting text from an Indian RERA (Real Estate Regulatory Authority) \
construction progress certificate. These documents contain structured tables \
reporting the percentage completion of construction activities for each tower/wing \
of a real-estate project.

Extract ALL text from this document and return it as clean markdown.

Rules:
- Preserve table structure using markdown pipe tables (| col | col |)
- Keep all numbers, percentages, and dates exactly as they appear
- Keep all tower/wing/block names exactly as they appear
- Preserve table titles (e.g. "Table – A", "Table B", "Table C") as headings
- If a table spans multiple pages, merge it into one continuous table
- Do not summarise, interpret, or skip any content
- Return only the extracted markdown — no preamble, no explanation
"""


def resolve_path(local_path):
    p = DOCUMENTS_DIR / local_path
    if p.exists():
        return p
    bare = Path(str(p).rstrip("/"))
    return bare if bare.exists() else None


def ocr_doc(item, client, model):
    """OCR one scanned PDF with Gemini. Returns stats dict or raises."""
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

    t0 = time.perf_counter()
    try:
        pdf_bytes = path.read_bytes()
        pdf_part  = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        response  = client.models.generate_content(
            model=model,
            contents=[pdf_part, _PROMPT],
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=8192,
            ),
        )
        md = response.text or ""
        ok = True
    except Exception as exc:
        md = f"[OCR ERROR: {exc}]"
        ok = False

    elapsed = round(time.perf_counter() - t0, 4)
    out_path.write_text(md, encoding="utf-8")

    return {
        "rera_id":    rera_id,
        "state":      state,
        "local_path": local_path,
        "pages":      min(pages, MAX_PAGES),
        "md_chars":   len(md),
        "ok":         ok,
        "elapsed_s":  elapsed,
        "model":      model,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workers", type=int, default=20,
                        help="Parallel threads (default: 20)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Gemini model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N docs (for testing)")
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    with open(VALIDATION_PATH) as f:
        results = json.load(f)["results"]

    scanned = [r for r in results if r["doc_type"] in ("scanned_pdf", "garbled_pdf")]
    if args.limit:
        scanned = scanned[:args.limit]

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    # Count already-done for display
    already_done = sum(
        1 for r in scanned
        if (OUTPUT_BASE / (r["local_path"] + ".extracted.md")).exists()
    )

    print(f"scanned/garbled docs : {len(scanned)}")
    print(f"already extracted    : {already_done}")
    print(f"to process           : {len(scanned) - already_done}")
    print(f"model                : {args.model}")
    print(f"workers              : {args.workers}")
    print()

    stats  = []
    errors = []
    wall_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(ocr_doc, item, client, args.model): item
            for item in scanned
        }
        with tqdm(total=len(futures), unit="doc", desc="OCR") as pbar:
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

    with open(STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)

    ok_stats = [s for s in stats if s.get("ok")]
    if ok_stats:
        times  = [s["elapsed_s"] for s in ok_stats]
        chars  = [s["md_chars"]  for s in ok_stats]
        by_state = {}
        for s in ok_stats:
            by_state.setdefault(s["state"], []).append(s["elapsed_s"])

        summary = {
            "total_docs":    len(scanned),
            "extracted":     len(ok_stats),
            "errors":        len(errors),
            "workers":       args.workers,
            "model":         args.model,
            "wall_time_s":   round(wall_total, 1),
            "time_avg_s":    round(statistics.mean(times),   4),
            "time_median_s": round(statistics.median(times), 4),
            "time_p95_s":    round(sorted(times)[int(len(times) * 0.95)], 4),
            "time_max_s":    round(max(times), 4),
            "time_total_s":  round(sum(times), 1),
            "md_chars_avg":  round(statistics.mean(chars), 0),
            "by_state": {
                st: {"count": len(ts), "avg_s": round(statistics.mean(ts), 4)}
                for st, ts in sorted(by_state.items())
            },
        }
    else:
        summary = {"extracted": 0, "errors": len(errors)}

    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'─'*52}")
    print(f"  Extracted  : {summary.get('extracted', 0)} / {summary.get('total_docs', '?')}")
    print(f"  Errors     : {summary.get('errors', 0)}")
    if ok_stats:
        print(f"  Wall time  : {wall_total:.0f}s")
        print(f"  Per-doc    : avg {summary['time_avg_s']}s  "
              f"median {summary['time_median_s']}s  "
              f"p95 {summary['time_p95_s']}s")
        print(f"  Markdown   : avg {summary['md_chars_avg']:.0f} chars")
        print(f"\n  By state:")
        for st, d in summary["by_state"].items():
            print(f"    {st}: {d['count']} docs  avg {d['avg_s']}s")
    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e.get('rera_id', '?')}: {e.get('reason', e)}")
        if len(errors) > 5:
            print(f"    ... and {len(errors)-5} more")
    print(f"{'─'*52}")
    print(f"  Stats   → {STATS_PATH}")
    print(f"  Summary → {SUMMARY_PATH}")
    print(f"  Output  → {OUTPUT_BASE}/")


if __name__ == "__main__":
    main()
