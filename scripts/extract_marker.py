"""
Run marker-pdf on a representative sample of text_pdf documents (12 per state).
Uses Python 3.12 (marker requires 3.10+ due to surya dependency).

Full corpus run is infeasible (~97s+/doc on ML inference). This script samples
60 docs for quality comparison against pymupdf4llm and docling.

Outputs:
  output/extracted_marker/<local_path>.extracted.md  — markdown per document
  output/extraction_marker_stats.json                — per-doc timing/memory
  output/extraction_marker_summary.json              — aggregate averages
"""
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import psutil

DOCUMENTS_DIR   = Path("documents")
OUTPUT_BASE     = Path("output/extracted_marker")
VALIDATION_PATH = Path("output/validation_results.json")
STATS_PATH      = Path("output/extraction_marker_stats.json")
SUMMARY_PATH    = Path("output/extraction_marker_summary.json")

SAMPLE_PER_STATE = 12
MAX_PAGES        = 8


def resolve_path(local_path):
    p = DOCUMENTS_DIR / local_path
    if p.exists():
        return p
    bare = Path(str(p).rstrip("/"))
    return bare if bare.exists() else None


def pick_samples(results):
    by_state = defaultdict(list)
    for r in results:
        if r["doc_type"] == "text_pdf":
            by_state[r["state_code"]].append(r)
    samples = []
    for state, docs in sorted(by_state.items()):
        step = max(1, len(docs) // SAMPLE_PER_STATE)
        picked = docs[::step][:SAMPLE_PER_STATE]
        samples.extend(picked)
        print(f"  {state}: {len(picked)} docs sampled from {len(docs)}")
    return samples


def main():
    with open(VALIDATION_PATH) as f:
        results = json.load(f)["results"]

    print("Picking representative sample…")
    samples = pick_samples(results)
    print(f"Total sample: {len(samples)} docs\n")

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    print("Loading marker models (one-time)…")
    t_load = time.perf_counter()
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    model_dict = create_model_dict()
    converter  = PdfConverter(artifact_dict=model_dict)
    print(f"Models ready in {time.perf_counter()-t_load:.1f}s\n")

    proc   = psutil.Process(os.getpid())
    stats  = []
    errors = []

    for i, item in enumerate(samples, 1):
        local_path = item["local_path"]
        path       = resolve_path(local_path)
        rera_id    = item["rera_reg_id"]
        state      = item["state_code"]
        pages      = item.get("pages") or MAX_PAGES

        out_path = OUTPUT_BASE / (local_path + ".extracted.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"[{i}/{len(samples)}] skip (exists)  {state}  {out_path.name[:50]}")
            continue

        if not path:
            errors.append({"rera_id": rera_id, "reason": "file not found"})
            print(f"[{i}/{len(samples)}] MISSING  {rera_id}")
            continue

        t0   = time.perf_counter()
        cpu0 = proc.cpu_times()
        mem0 = proc.memory_info().rss

        try:
            rendered = converter(str(path))
            md       = rendered.markdown
            ok       = True
        except Exception as exc:
            md  = f"[EXTRACTION ERROR: {exc}]"
            ok  = False
            errors.append({"rera_id": rera_id, "reason": str(exc)})

        t1   = time.perf_counter()
        cpu1 = proc.cpu_times()
        mem1 = proc.memory_info().rss

        elapsed = t1 - t0
        out_path.write_text(md, encoding="utf-8")

        eta = (len(samples) - i) * elapsed
        print(f"[{i}/{len(samples)}] {state}  {elapsed:.1f}s  {len(md):,} chars  "
              f"ETA {eta/60:.0f}m  {path.name[:45]}")

        stats.append({
            "rera_id":    rera_id,
            "state":      state,
            "local_path": local_path,
            "pages":      min(pages, MAX_PAGES),
            "md_chars":   len(md),
            "ok":         ok,
            "elapsed_s":  round(elapsed, 4),
            "cpu_user_s": round(cpu1.user   - cpu0.user,   4),
            "cpu_sys_s":  round(cpu1.system - cpu0.system, 4),
            "mem_rss_mb": round(mem1 / 1024 / 1024, 2),
        })

    with open(STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)

    ok_stats = [s for s in stats if s["ok"]]
    if ok_stats:
        times   = [s["elapsed_s"]  for s in ok_stats]
        mems    = [s["mem_rss_mb"] for s in ok_stats]
        chars   = [s["md_chars"]   for s in ok_stats]
        by_state = {}
        for s in ok_stats:
            by_state.setdefault(s["state"], []).append(s["elapsed_s"])

        summary = {
            "tool":              "marker",
            "sample_size":       len(ok_stats),
            "total_text_pdfs":   1245,
            "projected_total_h": round(statistics.mean(times) * 1245 / 3600, 1),
            "time_avg_s":        round(statistics.mean(times),   2),
            "time_median_s":     round(statistics.median(times), 2),
            "time_max_s":        round(max(times),               2),
            "time_total_s":      round(sum(times),               1),
            "cpu_avg_s":         round(statistics.mean([s["cpu_user_s"]+s["cpu_sys_s"] for s in ok_stats]), 2),
            "mem_peak_rss_mb":   round(max(mems),                2),
            "mem_avg_rss_mb":    round(statistics.mean(mems),    2),
            "md_chars_avg":      round(statistics.mean(chars),   0),
            "md_chars_median":   round(statistics.median(chars), 0),
            "errors":            len(errors),
            "by_state": {
                st: {"count": len(ts), "avg_s": round(statistics.mean(ts), 2)}
                for st, ts in sorted(by_state.items())
            },
        }
    else:
        summary = {"tool": "marker", "sample_size": 0, "errors": len(errors)}

    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'─'*52}")
    if ok_stats:
        print(f"  Extracted : {len(ok_stats)}/{len(samples)}")
        print(f"  Time      : avg {summary['time_avg_s']}s  median {summary['time_median_s']}s")
        print(f"  Projected : {summary['projected_total_h']}h for full corpus")
        print(f"  Memory    : avg {summary['mem_avg_rss_mb']} MB  peak {summary['mem_peak_rss_mb']} MB")
        print(f"  Markdown  : avg {summary['md_chars_avg']:.0f} chars/doc")
    print(f"  Stats  → {STATS_PATH}")
    print(f"  Output → {OUTPUT_BASE}/")


if __name__ == "__main__":
    main()
