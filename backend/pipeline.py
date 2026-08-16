"""
Pipeline orchestrator: Phase 1 (Sci-Hub) → Phase 2 (PKU Library WebVPN).

Reads CSV, runs both phases, and produces unified results with
status column and metadata-based PDF filenames.
"""

import csv
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .phase1_scihub import run_phase1
from .phase2_webvpn import run_phase2

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "pipeline_state.json"

StatusCallback = Callable[[str, dict], None] | None


def _read_csv(filepath: str) -> list[dict]:
    """Read DOI list from CSV. Detects DOI column by name."""
    papers = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        doi_col = None
        for name in reader.fieldnames or []:
            if "doi" in (name or "").lower():
                doi_col = name
                break
        if not doi_col:
            doi_col = reader.fieldnames[0] if reader.fieldnames else "doi"

        title_col = None
        for name in reader.fieldnames or []:
            if "title" in (name or "").lower():
                title_col = name
                break

        for i, row in enumerate(reader):
            doi = row.get(doi_col, "").strip()
            title = row.get(title_col, "") if title_col else ""
            if not doi:
                # No DOI — assign placeholder so paper flows through Phase 1/2
                # and appears in results for manual PDF supplementation before Phase 3
                doi = f"NO_DOI_{i}"
            papers.append({"doi": doi, "title": title.strip()})

    return papers


def _save_state(state: dict) -> None:
    state["updated_at"] = datetime.now().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _merge_results(p1_results: list[dict], p2_results: list[dict]) -> list[dict]:
    """Merge Phase 1 and Phase 2 results."""
    merged = {}

    for r in p1_results:
        doi = r["doi"].lower()
        merged[doi] = {
            "doi": r["doi"],
            "title": r.get("title", ""),
            "filename": r.get("filename", ""),
            "status": r["status"],
            "phase": 1,
            "filepath": r.get("filepath", ""),
            "message": r.get("message", ""),
            "route": r.get("route", "none"),
        }

    for r in p2_results:
        doi = r.get("doi", "").lower()
        if not doi:
            continue
        if doi in merged:
            existing = merged[doi]
            if r.get("status") == "downloaded" and existing["status"] != "downloaded":
                existing["status"] = "downloaded"
                existing["phase"] = 2
                existing["filepath"] = r.get("filepath", "")
                existing["message"] = r.get("message", "")
                existing["route"] = r.get("route", "webvpn")
        else:
            merged[doi] = {
                "doi": r.get("doi", ""),
                "title": r.get("title", ""),
                "filename": r.get("filename", ""),
                "status": r.get("status", "unknown"),
                "phase": 2,
                "filepath": r.get("filepath", ""),
                "message": r.get("message", ""),
                "route": r.get("route", "webvpn"),
            }

    status_order = {"downloaded": 0, "already_downloaded": 1,
                    "download_failed": 2, "no_doi": 3, "not_found": 4}
    result_list = list(merged.values())
    result_list.sort(key=lambda r: status_order.get(r["status"], 99))
    return result_list


def _write_final_csv(results: list[dict], filepath: str) -> None:
    fieldnames = ["doi", "title", "filename", "status", "phase",
                  "filepath", "message", "route"]
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


# ── Intermediate state for Phase-1→Phase-2 handoff ──

def _phase1_results_path(out_root: Path) -> Path:
    return out_root / "phase1_results.json"


def _save_phase1_results(
    out_root: Path, p1_results: list[dict], p1_stats: dict, csv_path: str,
) -> None:
    payload = {
        "p1_results": p1_results,
        "p1_stats": p1_stats,
        "csv_path": csv_path,
        "out_root": str(out_root),
        "saved_at": datetime.now().isoformat(),
    }
    path = _phase1_results_path(out_root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"Phase 1 results saved to {path}")


def _load_phase1_results(out_root: Path) -> dict | None:
    path = _phase1_results_path(out_root)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Phase 1: Sci-Hub download ───────────────────────

def run_phase1_pipeline(
    csv_path: str,
    unpaywall_email: str = "",
    enable_unpaywall: bool = True,
    output_dir: str = "",
    progress_callback: StatusCallback = None,
) -> dict:
    """Run Phase 1 (Sci-Hub) on all DOIs from the CSV.

    Saves intermediate results so Phase 2 can pick up failed DOIs.
    """
    if output_dir:
        out_root = Path(output_dir).resolve()
    else:
        out_root = BASE_DIR / "output"
    papers_dir = out_root / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    state = {
        "status": "starting",
        "phase": 0,
        "csv_path": csv_path,
        "started_at": datetime.now().isoformat(),
    }
    _save_state(state)

    if progress_callback:
        progress_callback("reading_csv", {"file": csv_path})

    papers = _read_csv(csv_path)

    # Separate no-DOI papers (from "ignored" rows) — they'll be skipped in Phase 1
    # but still appear in results for manual PDF supplementation before Phase 3
    no_doi_papers = [p for p in papers if p["doi"].startswith("NO_DOI_")]
    regular_papers = [p for p in papers if not p["doi"].startswith("NO_DOI_")]
    dois = [p["doi"] for p in regular_papers]
    logger.info(f"Pipeline Phase 1 (Sci-Hub): loaded {len(dois)} DOIs"
                f" (+ {len(no_doi_papers)} no-DOI) from CSV")

    state["status"] = "phase1_running"
    state["total_papers"] = len(regular_papers)
    _save_state(state)

    if progress_callback:
        progress_callback("phase1_start", {"total": len(dois)})

    p1_results, p1_stats = run_phase1(
        dois, output_dir=str(out_root), progress_callback=progress_callback
    )

    # Append no-DOI papers to Phase 1 results with "no_doi" status
    for p in no_doi_papers:
        p1_results.append({
            "doi": p["doi"],
            "title": p.get("title", ""),
            "status": "no_doi",
            "route": "none",
            "filepath": "",
            "filename": "",
            "message": "No DOI available — needs manual PDF",
        })

    state["phase1_complete"] = True
    state["phase1_stats"] = p1_stats
    _save_state(state)

    # Collect failed DOIs for Phase 2 (skip no-DOI papers — they can't be retried)
    failed = [r for r in p1_results
              if r["status"] != "downloaded" and r["status"] != "no_doi"]
    failed_dois = [r["doi"] for r in failed]

    _save_phase1_results(out_root, p1_results, p1_stats, csv_path)

    p1_downloaded = p1_stats.get("downloaded", 0)
    total_all = len(p1_results)  # includes downloaded + failed + no_doi

    if failed_dois:
        logger.info(f"Phase 1 done: {p1_downloaded} downloaded, "
                    f"{len(failed_dois)} need Phase 2, "
                    f"{len(no_doi_papers)} no-DOI")

        state["phase2_ready"] = False
        state["failed_count"] = len(failed_dois)
        state["status"] = "awaiting_phase2"
        _save_state(state)

        summary = {
            "paused": True,
            "total_papers": total_all,
            "phase1_downloaded": p1_downloaded,
            "failed_count": len(failed_dois),
            "final_csv": "",
        }

        if progress_callback:
            progress_callback("phase1_done", p1_stats)
            progress_callback("phase1_complete_awaiting_p2", {
                "failed_count": len(failed_dois),
                "p1_downloaded": p1_downloaded,
            })

        return summary

    # All papers downloaded in Phase 1 (no-DOI papers still need manual attention)
    no_doi_warning = (
        f" ({len(no_doi_papers)} no-DOI papers need manual PDFs)"
        if no_doi_papers else ""
    )
    logger.info(f"Phase 1: all DOIs downloaded, no Phase 2 needed{no_doi_warning}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_csv = out_root / f"results_final_{timestamp}.csv"
    _write_final_csv(p1_results, str(final_csv))

    summary = {
        "paused": False,
        "total_papers": total_all,
        "downloaded": p1_downloaded,
        "download_failed": p1_stats.get("download_failed", 0),
        "phase1_downloaded": p1_downloaded,
        "phase2_downloaded": 0,
        "final_csv": str(final_csv),
    }

    state["status"] = "completed"
    state["summary"] = summary
    _save_state(state)

    if progress_callback:
        progress_callback("phase1_done", p1_stats)
        progress_callback("completed", summary)

    return summary


# ── Phase 2: PKU Library WebVPN ──────────────────────

def run_phase2_pipeline(
    output_dir: str = "",
    progress_callback: StatusCallback = None,
) -> dict:
    """Run Phase 2 (PKU Library WebVPN) on DOIs that Phase 1 failed to download."""
    if output_dir:
        out_root = Path(output_dir).resolve()
    else:
        out_root = BASE_DIR / "output"

    papers_dir = out_root / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    saved = _load_phase1_results(out_root)
    if not saved:
        raise RuntimeError("No Phase 1 results found. Run Phase 1 (Sci-Hub) first.")

    p1_results: list[dict] = saved["p1_results"]
    p1_stats: dict = saved["p1_stats"]

    # Find DOIs that Phase 1 couldn't download (skip no-DOI papers)
    failed = [r for r in p1_results
              if r["status"] != "downloaded" and r["status"] != "no_doi"]
    failed_dois = [r["doi"] for r in failed]

    if not failed_dois:
        no_doi_count = sum(1 for r in p1_results if r["status"] == "no_doi")
        logger.info(f"Phase 2: nothing to download"
                    + (f" ({no_doi_count} no-DOI papers need manual PDFs)"
                       if no_doi_count else ""))
        return {
            "total_papers": len(p1_results),
            "downloaded": p1_stats.get("downloaded", 0),
            "phase1_downloaded": p1_stats.get("downloaded", 0),
            "phase2_downloaded": 0,
            "final_csv": "",
        }

    logger.info(f"Phase 2 (WebVPN): {len(failed_dois)} failed DOIs to retry")

    from .phase2_webvpn import get_active_provider, PROVIDERS
    provider = get_active_provider()

    state = _load_state()
    state["status"] = "phase2_running"
    state["failed_count"] = len(failed_dois)
    state["phase2_provider"] = provider
    # Ensure Phase 1 fields survive a Phase 2 start.  If a previous run
    # left the state file missing these (e.g. Phase 1's final state write
    # was interrupted), the UI would lose Phase 1's results display.
    # Recover them from the authoritative phase1_results.json here.
    state["phase1_complete"] = True
    state["phase1_stats"] = p1_stats
    _save_state(state)

    if progress_callback:
        progress_callback("phase2_prerequisites", {
            "failed_count": len(failed_dois),
            "provider": provider,
            "message": f"Retrying {len(failed_dois)} papers via {PROVIDERS[provider]['label']}",
        })

    try:
        p2_results, p2_stats = run_phase2(
            failed_dois,
            output_dir=str(out_root),
            progress_callback=progress_callback,
        )
    except Exception as e:
        logger.error(f"Phase 2 error: {e}")
        state["phase2_error"] = str(e)
        _save_state(state)
        if progress_callback:
            progress_callback("phase2_error", {"error": str(e)})
        raise

    state["phase2_complete"] = True
    state["phase2_stats"] = p2_stats
    _save_state(state)

    if progress_callback:
        progress_callback("phase2_done", p2_stats)

    # Merge and write final CSV
    if progress_callback:
        progress_callback("merging", {})

    final_results = _merge_results(p1_results, p2_results)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_csv = out_root / f"results_final_{timestamp}.csv"
    _write_final_csv(final_results, str(final_csv))

    total_downloaded = sum(1 for r in final_results if r["status"] == "downloaded")
    summary = {
        "total_papers": len(final_results),
        "downloaded": total_downloaded,
        "download_failed": sum(1 for r in final_results if r["status"] == "download_failed"),
        "phase1_downloaded": p1_stats.get("downloaded", 0),
        "phase2_downloaded": p2_stats.get("downloaded", 0),
        "final_csv": str(final_csv),
    }

    state["status"] = "completed"
    state["summary"] = summary
    _save_state(state)

    if progress_callback:
        progress_callback("completed", summary)

    logger.info(f"Pipeline complete: {total_downloaded}/{len(final_results)} downloaded")
    return summary


# Backward-compatible alias
run_pipeline = run_phase1_pipeline
