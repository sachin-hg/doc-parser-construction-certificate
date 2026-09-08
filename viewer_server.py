#!/usr/bin/env python3
"""API server for the RERA document viewer + benchmark runs.

Endpoints:
  GET  /api/docs                         — JSON list of all documents
  GET  /api/docs?state=Gujarat           — docs filtered by state, with already_ran flag
  GET  /api/doc/{idx}/pdf                — binary PDF
  GET  /api/doc/{idx}/md                 — pymupdf4llm markdown
  GET  /api/doc/{idx}/docling            — docling markdown (404 if absent)
  GET  /api/doc/{idx}/marker             — marker markdown  (404 if absent)

  GET  /api/prompt-versions              — list available prompt versions
  GET  /api/runs                         — list all benchmark runs
  GET  /api/runs/{run_id}                — run detail + doc list
  GET  /api/runs/{run_id}/results        — full JSONL results as JSON array
  GET  /api/runs/{run_id}/log            — process / fetch stdout log

  POST /api/runs                         — start a new benchmark run
  POST /api/runs/{run_id}/fetch          — trigger fetch for a batch run

Run from the project root:
  python viewer_server.py
"""
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT            = Path(__file__).parent
sys.path.insert(0, str(ROOT / "scripts"))
from prompt_registry import VERSIONS, CURRENT_VERSION  # noqa: E402

# Load .env from project root (only sets vars not already in the environment)
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
DOCS_DIR        = ROOT / "output" / "extracted_md"
DOCLING_DIR     = ROOT / "output" / "extracted_docling"
MARKER_DIR      = ROOT / "output" / "extracted_marker"
FILES_DIR       = ROOT / "documents"
VALIDATION_PATH = ROOT / "output" / "validation_results.json"
RUN_DIR         = ROOT / "output" / "benchmark_results" / "runs"
SCRIPT_PATH     = ROOT / "scripts" / "benchmark.py"
PORT = int(os.environ.get("PORT", 8765))


def _build_docs():
    extracted_set = {
        str(f.relative_to(DOCS_DIR))[: -len(".extracted.md")]
        for f in DOCS_DIR.rglob("*.extracted.md")
    } if DOCS_DIR.exists() else set()

    docling_paths = {
        str(f.relative_to(DOCLING_DIR))
        for f in DOCLING_DIR.rglob("*.extracted.md")
    } if DOCLING_DIR.exists() else set()

    marker_paths = {
        str(f.relative_to(MARKER_DIR))
        for f in MARKER_DIR.rglob("*.extracted.md")
    } if MARKER_DIR.exists() else set()

    with open(VALIDATION_PATH) as f:
        validation = json.load(f)["results"]

    docs = []
    for r in sorted(validation, key=lambda x: x["local_path"]):
        local_path = r["local_path"]
        rel_md = local_path + ".extracted.md"
        parts = Path(local_path).parts
        period = parts[0] if len(parts) > 0 else ""
        state  = parts[1] if len(parts) > 1 else ""
        stem   = parts[-1] if parts else ""

        docs.append({
            "idx":         len(docs),
            "rera_id":     r.get("rera_reg_id", stem.split("_")[0]),
            "state":       state,
            "period":      period,
            "filename":    stem,
            "rel_md":      rel_md,
            "rel_doc":     local_path,
            "has_doc":     (FILES_DIR / local_path).exists(),
            "has_md":      local_path in extracted_set,
            "has_docling": rel_md in docling_paths,
            "has_marker":  rel_md in marker_paths,
            "doc_type":    r.get("doc_type", "unknown"),
            "pages":       r.get("pages", 0),
        })
    return docs


DOCS      = _build_docs()
DOCS_JSON = json.dumps(DOCS).encode()
PATH_TO_IDX = {d["rel_doc"]: d["idx"] for d in DOCS}

# In-memory subprocess registry — survives across requests in the same process
RUNNING_PROCS: dict = {}  # run_id → {"proc": Popen, "log_path": str}
FETCH_PROCS:   dict = {}  # run_id → {"proc": Popen, "log_path": str}

_CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
_ALT_DIRS = {"docling": DOCLING_DIR, "marker": MARKER_DIR}


# ── Run helpers (module-level) ─────────────────────────────────────────────────

def _get_ran_paths() -> set:
    """Return local_paths that had at least one successful extraction across all runs.
    A result is successful if it has output and no error (skipped/failed rows excluded)."""
    ran = set()
    if not RUN_DIR.exists():
        return ran
    for run_dir in RUN_DIR.iterdir():
        rfile = run_dir / "results.jsonl"
        if not rfile.exists():
            continue
        for line in rfile.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if (
                    obj.get("local_path")
                    and not obj.get("skipped")
                    and not obj.get("error")
                    and obj.get("output") is not None
                ):
                    ran.add(obj["local_path"])
            except Exception:
                pass
    return ran


def _run_status(manifest: dict, run_id: str) -> str:
    """Derive a human-readable status from a run manifest."""
    if manifest.get("results_path"):
        return "complete"
    mode = manifest.get("mode", "async")
    if mode == "async":
        proc_info = RUNNING_PROCS.get(run_id)
        if proc_info and proc_info["proc"].poll() is None:
            return "running"
        return "error"
    # batch mode
    chunks = manifest.get("chunks", [])
    if not chunks:
        return "pending"
    n_pending  = sum(1 for c in chunks if c["status"] == "pending")
    n_complete = sum(1 for c in chunks if c["status"] == "complete")
    if n_pending == 0:
        return "complete"
    if n_complete == 0:
        return "pending"
    return "partial"


def _read_run_docs(run_dir: Path, manifest: dict) -> list:
    """Build a lightweight doc-status list from all available chunk/result files."""
    docs  = []
    seen  = set()

    def add(local_path, rera_id, status, *, error=None,
            has_output=False, cost_usd=None, latency_s=None):
        if local_path in seen:
            return
        seen.add(local_path)
        docs.append({
            "local_path": local_path,
            "rera_id":    rera_id or "",
            "status":     status,
            "error":      error,
            "has_output": has_output,
            "cost_usd":   cost_usd,
            "latency_s":  latency_s,
        })

    def _add_result(r: dict):
        if r.get("success"):
            status = "success"
        elif r.get("skipped"):
            status = "skipped"
        else:
            status = "error"
        add(r["local_path"], r.get("rera_id"), status,
            error=r.get("error"), has_output=bool(r.get("output")),
            cost_usd=r.get("cost_usd"), latency_s=r.get("latency_s"))

    # Skipped results stored in manifest
    for r in manifest.get("skipped_results", []):
        _add_result(r)

    # Read results.jsonl — exists for complete runs and partial async runs (streamed live)
    results_path = run_dir / "results.jsonl"
    if results_path.exists():
        try:
            with open(results_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        _add_result(json.loads(line))
        except Exception:
            pass
        # For a completed run the file has everything; return early.
        if manifest.get("results_path"):
            return docs
        # For an in-progress async run the file is partial — fall through to
        # mark remaining docs as "running" below.

    # Partial run — read completed chunk files + scan pending job files
    for chunk in manifest.get("chunks", []):
        if chunk["status"] == "complete" and chunk.get("results_file"):
            cf = run_dir / chunk["results_file"]
            if cf.exists():
                try:
                    with open(cf) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                _add_result(json.loads(line))
                except Exception:
                    pass
        elif chunk["status"] == "pending" and chunk.get("job_file"):
            jf = run_dir / chunk["job_file"]
            if jf.exists():
                try:
                    with open(jf) as f:
                        job = json.load(f)
                    for doc_entry in job.get("docs", []):
                        add(doc_entry["local_path"], doc_entry.get("rera_id"), "pending")
                except Exception:
                    pass

    return docs


def _read_run_results(run_dir: Path, manifest: dict) -> list:
    """Return full result objects (including output field) from all completed chunks."""
    results = list(manifest.get("skipped_results", []))

    results_path = run_dir / "results.jsonl"
    if results_path.exists():
        try:
            with open(results_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        results.append(json.loads(line))
        except Exception:
            pass
        return results

    for chunk in manifest.get("chunks", []):
        if chunk["status"] == "complete" and chunk.get("results_file"):
            cf = run_dir / chunk["results_file"]
            if cf.exists():
                try:
                    with open(cf) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                results.append(json.loads(line))
                except Exception:
                    pass

    return results


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in _CORS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs, unquote
        parsed = urlparse(self.path)
        path   = unquote(parsed.path)
        qs     = parse_qs(parsed.query)

        if path == "/api/docs":
            state_filter = (qs.get("state") or [None])[0]
            if state_filter:
                ran  = _get_ran_paths()
                docs = [
                    {**d, "already_ran": d["rel_doc"] in ran}
                    for d in DOCS
                    if d["state"].lower() == state_filter.lower()
                ]
                self._respond(200, "application/json", json.dumps(docs).encode())
            else:
                self._respond(200, "application/json", DOCS_JSON)
        elif m := re.match(r"^/api/doc/(\d+)/pdf$", path):
            self._serve_file(int(m.group(1)))
        elif m := re.match(r"^/api/doc/(\d+)/md$", path):
            self._serve_md(int(m.group(1)), DOCS_DIR)
        elif m := re.match(r"^/api/doc/(\d+)/(docling|marker)$", path):
            self._serve_md(int(m.group(1)), _ALT_DIRS[m.group(2)])
        elif path == "/api/states":
            self._list_states()
        elif path == "/api/prompt-versions":
            self._list_prompt_versions()
        elif path == "/api/doc-runs":
            self._doc_runs(qs)
        elif path == "/api/runs":
            self._list_runs()
        elif m := re.match(r"^/api/runs/([^/]+)/results$", path):
            self._run_results(m.group(1))
        elif m := re.match(r"^/api/runs/([^/]+)/result$", path):
            self._run_doc_result(m.group(1), qs)
        elif m := re.match(r"^/api/runs/([^/]+)/log$", path):
            self._run_log(m.group(1))
        elif m := re.match(r"^/api/runs/([^/]+)$", path):
            self._run_detail(m.group(1))
        else:
            self._status(404)

    def do_POST(self):
        from urllib.parse import unquote
        path = unquote(self.path.split("?")[0])

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/runs":
            self._create_run(data)
        elif m := re.match(r"^/api/runs/([^/]+)/stop$", path):
            self._stop_run(m.group(1))
        elif m := re.match(r"^/api/runs/([^/]+)/retry$", path):
            self._retry_run(m.group(1), data)
        elif m := re.match(r"^/api/runs/([^/]+)/fetch$", path):
            self._fetch_run(m.group(1), data)
        else:
            self._status(404)

    # ── Docs endpoints ─────────────────────────────────────────────────────────

    def _serve_file(self, idx):
        if idx >= len(DOCS):
            self._status(404); return
        doc   = DOCS[idx]
        fpath = (FILES_DIR / doc["rel_doc"]).resolve()
        try:
            fpath.relative_to(FILES_DIR.resolve())
        except ValueError:
            self._status(403); return
        if not fpath.exists():
            self._status(404); return
        self._respond(200, "application/pdf", fpath.read_bytes())

    def _serve_md(self, idx, base_dir: Path):
        if idx >= len(DOCS):
            self._status(404); return
        doc   = DOCS[idx]
        fpath = (base_dir / doc["rel_md"]).resolve()
        try:
            fpath.relative_to(base_dir.resolve())
        except ValueError:
            self._status(403); return
        if not fpath.exists():
            self._status(404); return
        self._respond(200, "text/plain; charset=utf-8", fpath.read_bytes())

    # ── States list ────────────────────────────────────────────────────────────

    def _list_states(self):
        states = sorted({d["state"] for d in DOCS if d["state"]})
        self._json(200, states)

    def _list_prompt_versions(self):
        self._json(200, {
            "current": CURRENT_VERSION,
            "versions": [
                {"id": k, **v}
                for k, v in VERSIONS.items()
            ],
        })

    # ── Doc-runs index (which runs have a result for this doc) ────────────────

    def _doc_runs(self, qs):
        local_path = (qs.get("path") or [None])[0]
        if not local_path:
            self._json(400, {"error": "path required"}); return
        if not RUN_DIR.exists():
            self._json(200, []); return
        matches = []
        for run_dir in sorted(RUN_DIR.iterdir()):
            mp = run_dir / "run.json"
            if not mp.exists():
                continue
            try:
                with open(mp) as f:
                    manifest = json.load(f)
            except Exception:
                continue
            rfile = run_dir / "results.jsonl"
            if not rfile.exists():
                continue
            try:
                for line in rfile.read_text(errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("local_path") == local_path and r.get("output") is not None:
                            matches.append({
                                "run_id":         manifest.get("run_id", run_dir.name),
                                "variant":        manifest.get("variant"),
                                "state":          manifest.get("state"),
                                "prompt_version": manifest.get("prompt_version", "v0"),
                                "created_at":     manifest.get("created_at"),
                            })
                            break
                    except Exception:
                        pass
            except Exception:
                pass
        matches.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, matches)

    # ── Single-doc result lookup ───────────────────────────────────────────────

    def _run_doc_result(self, run_id: str, qs):
        local_path = (qs.get("path") or [None])[0]
        if not local_path:
            self._json(400, {"error": "path required"}); return
        run_dir = RUN_DIR / run_id
        if not run_dir.exists():
            self._json(404, {"error": "Run not found"}); return
        try:
            with open(run_dir / "run.json") as f:
                manifest = json.load(f)
        except Exception as e:
            self._json(500, {"error": str(e)}); return
        for r in _read_run_results(run_dir, manifest):
            if r.get("local_path") == local_path:
                self._json(200, r)
                return
        self._json(404, {"error": "Doc not in run"})

    # ── Runs list ──────────────────────────────────────────────────────────────

    def _list_runs(self):
        if not RUN_DIR.exists():
            self._json(200, [])
            return
        runs = []
        def _sort_key(d):
            mp = d / "run.json"
            if not mp.exists():
                return ""
            try:
                return json.load(open(mp)).get("created_at", "") or d.name
            except Exception:
                return d.name

        for run_dir in sorted(RUN_DIR.iterdir(), key=_sort_key, reverse=True):
            manifest_path = run_dir / "run.json"
            if not manifest_path.exists():
                continue
            try:
                with open(manifest_path) as f:
                    m = json.load(f)
            except Exception:
                continue
            run_id = m.get("run_id", run_dir.name)
            status = _run_status(m, run_id)
            chunks = m.get("chunks", [])
            runs.append({
                "run_id":            run_id,
                "variant":           m.get("variant"),
                "state":             m.get("state"),
                "mode":              m.get("mode"),
                "prompt_version":    m.get("prompt_version", "v0"),
                "created_at":        m.get("created_at"),
                "status":            status,
                "n_chunks":          len(chunks),
                "n_chunks_complete": sum(1 for c in chunks if c["status"] == "complete"),
                "n_chunks_pending":  sum(1 for c in chunks if c["status"] == "pending"),
            })
        self._json(200, runs)

    # ── Run detail ─────────────────────────────────────────────────────────────

    def _run_detail(self, run_id: str):
        run_dir       = RUN_DIR / run_id
        manifest_path = run_dir / "run.json"
        if not manifest_path.exists():
            self._json(404, {"error": "Run not found"})
            return
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        status = _run_status(manifest, run_id)
        chunks = manifest.get("chunks", [])
        docs   = _read_run_docs(run_dir, manifest)

        total     = len(docs)
        n_success = sum(1 for d in docs if d["status"] == "success")
        n_error   = sum(1 for d in docs if d["status"] == "error")
        n_skipped = sum(1 for d in docs if d["status"] == "skipped")
        n_pending = sum(1 for d in docs if d["status"] == "pending")
        costs     = [d["cost_usd"]  for d in docs if d.get("cost_usd")  is not None]
        lats      = [d["latency_s"] for d in docs if d.get("latency_s") is not None]

        # Include log for running/error runs, and for complete runs where nothing succeeded
        log = None
        all_skipped = (n_success == 0 and n_skipped > 0 and status == "complete")
        if status in ("running", "error") or all_skipped:
            for log_name in ("process.log",):
                log_path = run_dir / log_name
                if log_path.exists():
                    try:
                        log = log_path.read_text(errors="replace")[-20_000:]
                    except Exception:
                        pass
                    break

        self._json(200, {
            "run_id":            manifest.get("run_id", run_id),
            "variant":           manifest.get("variant"),
            "state":             manifest.get("state"),
            "mode":              manifest.get("mode"),
            "prompt_version":    manifest.get("prompt_version", "v0"),
            "created_at":        manifest.get("created_at"),
            "status":            status,
            "n_chunks":          len(chunks),
            "n_chunks_complete": sum(1 for c in chunks if c["status"] == "complete"),
            "n_chunks_pending":  sum(1 for c in chunks if c["status"] == "pending"),
            "log":               log,
            "docs":              docs,
            "stats": {
                "total":        total,
                "success":      n_success,
                "error":        n_error,
                "skipped":      n_skipped,
                "pending":      n_pending,
                "cost_total":   round(sum(costs), 6) if costs else None,
                "avg_latency_s": round(sum(lats) / len(lats), 2) if lats else None,
            },
        })

    # ── Run results ────────────────────────────────────────────────────────────

    def _run_results(self, run_id: str):
        run_dir       = RUN_DIR / run_id
        manifest_path = run_dir / "run.json"
        if not manifest_path.exists():
            self._json(404, {"error": "Run not found"})
            return
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        results = _read_run_results(run_dir, manifest)
        self._json(200, results)

    # ── Run log ────────────────────────────────────────────────────────────────

    def _run_log(self, run_id: str):
        run_dir = RUN_DIR / run_id
        if not run_dir.exists():
            self._json(404, {"error": "Run not found"})
            return
        for log_name in ("fetch.log", "process.log"):
            log_path = run_dir / log_name
            if log_path.exists():
                try:
                    content = log_path.read_text(errors="replace")
                    self._respond(200, "text/plain; charset=utf-8", content.encode())
                    return
                except Exception:
                    pass
        self._respond(200, "text/plain; charset=utf-8", b"No log available.")

    # ── Create run ─────────────────────────────────────────────────────────────

    def _create_run(self, data: dict):
        variant        = data.get("variant", "A")
        state          = data.get("state", "GJ")
        mode           = data.get("mode", "async")
        limit          = data.get("limit")
        concurrency    = data.get("concurrency", 10)
        poll_tries     = data.get("poll_tries", 8)
        poll_interval  = data.get("poll_interval", 15)
        paths          = data.get("paths")  # optional list of local_path strings
        prompt_version = data.get("prompt_version", CURRENT_VERSION)

        if variant not in ("A", "B", "C", "D", "E", "F"):
            self._json(400, {"error": f"Invalid variant: {variant}"})
            return
        if prompt_version not in VERSIONS:
            self._json(400, {"error": f"Invalid prompt version: {prompt_version}"})
            return

        run_id  = f"{variant}_{state}_{time.strftime('%Y%m%d_%H%M%S')}"
        run_dir = RUN_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Write a minimal manifest so the UI can poll immediately
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
                "subset_paths":    paths,  # saved for UI reference
            }, f, indent=2)

        cmd = [sys.executable, str(SCRIPT_PATH), "run",
               "--variant", variant, "--state", state, "--mode", mode,
               "--run-id", run_id, "--prompt-version", prompt_version]
        if limit:
            cmd += ["--limit", str(limit)]
        if paths:
            subset_file = run_dir / "subset.json"
            with open(subset_file, "w") as f:
                json.dump(paths, f)
            cmd += ["--subset", str(subset_file)]
        if mode == "async":
            cmd += ["--concurrency", str(concurrency)]
        else:
            cmd += ["--batch-poll-tries", str(poll_tries),
                    "--batch-poll-interval", str(poll_interval)]

        log_path = run_dir / "process.log"
        try:
            log_file = open(log_path, "w")
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
            )
            RUNNING_PROCS[run_id] = {"proc": proc, "log_path": str(log_path)}
            self._json(200, {"run_id": run_id})
        except Exception as e:
            self._json(500, {"error": str(e)})

    # ── Retry run ─────────────────────────────────────────────────────────────

    def _retry_run(self, run_id: str, data: dict):
        """Create a new run retrying docs from a previous run.
        data.scope: "all" (default) — retry every doc in the original run
                    "failed"        — retry only docs with error or missing output
        """
        run_dir = RUN_DIR / run_id
        if not run_dir.exists():
            self._json(404, {"error": f"Run {run_id!r} not found"}); return

        try:
            with open(run_dir / "run.json") as f:
                manifest = json.load(f)
        except Exception as e:
            self._json(500, {"error": f"Cannot read manifest: {e}"}); return

        scope = data.get("scope", "all")  # "all" | "failed"

        if scope == "failed":
            # Collect paths that either errored or never produced output
            completed_ok = set()
            rfile = run_dir / "results.jsonl"
            if rfile.exists():
                for line in rfile.read_text(errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if not r.get("error") and not r.get("skipped") and r.get("output"):
                            completed_ok.add(r["local_path"])
                    except Exception:
                        pass
            # All docs in the original run minus those that succeeded
            original_paths = list(manifest.get("subset_paths") or [])
            if not original_paths:
                # Run had no subset — all docs for that state
                ran = _get_ran_paths()  # broad set; we need per-run granularity instead
                # Fall back: read all lines from results.jsonl to get attempted paths
                attempted = set()
                if rfile.exists():
                    for line in rfile.read_text(errors="replace").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                            if r.get("local_path"):
                                attempted.add(r["local_path"])
                        except Exception:
                            pass
                retry_paths = list(attempted - completed_ok) if attempted else None
            else:
                retry_paths = [p for p in original_paths if p not in completed_ok]
        else:
            retry_paths = list(manifest.get("subset_paths") or []) or None

        # Allow caller to override prompt_version; otherwise inherit from original run.
        inherited_version = manifest.get("prompt_version", "v0")
        new_data = {
            "variant":        manifest.get("variant", "A"),
            "state":          manifest.get("state", ""),
            "mode":           manifest.get("mode", "async"),
            "prompt_version": data.get("prompt_version") or inherited_version,
            "concurrency":    data.get("concurrency", 10),
            "poll_tries":     data.get("poll_tries", 8),
            "poll_interval":  data.get("poll_interval", 15),
        }
        if retry_paths is not None:
            new_data["paths"] = retry_paths

        self._create_run(new_data)

    # ── Stop run ───────────────────────────────────────────────────────────────

    def _stop_run(self, run_id: str):
        proc_info = RUNNING_PROCS.get(run_id)
        if not proc_info:
            self._json(404, {"error": f"No running process found for {run_id!r}"}); return
        proc = proc_info["proc"]
        if proc.poll() is not None:
            self._json(200, {"status": "already_stopped"}); return
        import signal as _signal
        try:
            proc.send_signal(_signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        self._json(200, {"status": "stopped"})

    # ── Fetch run ──────────────────────────────────────────────────────────────

    def _fetch_run(self, run_id: str, data: dict):
        run_dir = RUN_DIR / run_id
        if not run_dir.exists():
            self._json(404, {"error": f"Run {run_id!r} not found"})
            return

        poll_tries    = data.get("poll_tries", 4)
        poll_interval = data.get("poll_interval", 10)

        cmd = [sys.executable, str(SCRIPT_PATH), "fetch",
               "--run-id", run_id,
               "--poll-tries", str(poll_tries),
               "--poll-interval", str(poll_interval)]

        log_path = run_dir / "fetch.log"
        try:
            log_file = open(log_path, "w")
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
            )
            FETCH_PROCS[run_id] = {"proc": proc, "log_path": str(log_path)}
            self._json(200, {"run_id": run_id, "status": "started"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    # ── Low-level helpers ──────────────────────────────────────────────────────

    def _respond(self, code, content_type, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in _CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        body = json.dumps(obj, default=str).encode()
        self._respond(code, "application/json", body)

    def _status(self, code: int):
        self.send_response(code)
        for k, v in _CORS.items():
            self.send_header(k, v)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress per-request stdout noise


if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), Handler)
    n_md      = sum(1 for d in DOCS if d["has_md"])
    n_docling = sum(1 for d in DOCS if d["has_docling"])
    n_marker  = sum(1 for d in DOCS if d["has_marker"])
    print(f"RERA viewer API  →  http://localhost:{PORT}")
    print(f"  {len(DOCS)} documents  |  {n_md} extracted  |  "
          f"{n_docling} docling  |  {n_marker} marker")
    print(f"  Runs dir: {RUN_DIR}")
    print("  Press Ctrl-C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
