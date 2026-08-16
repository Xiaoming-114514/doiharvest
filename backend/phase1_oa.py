"""
Phase 1: Open Access paper downloader.

Queries Unpaywall + Crossref for OA availability, downloads PDFs,
and renames them using metadata-based filenames.

Exports:
    run_phase1(dois: list[dict]) -> list[dict]
"""

import csv
import hashlib
import logging
import os
import time
from pathlib import Path

import requests

from .metadata import batch_prefetch, get_filename

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
PAPERS_DIR = BASE_DIR / "papers"
PAPERS_DIR.mkdir(parents=True, exist_ok=True)

# --- Config defaults (overridable via function params) ---
UNPAYWALL_EMAIL = ""
ENABLE_UNPAYWALL = True
API_DELAY = 3.0
DOWNLOAD_DELAY = 3.0
MAX_RETRIES = 2

UNPAYWALL_URL = "https://api.unpaywall.org/v2/{}"
CROSSREF_URL = "https://api.crossref.org/works/{}"
TIMEOUT = 20


def configure(email: str = "", enable_unpaywall: bool = True,
              api_delay: float = 0.5, download_delay: float = 1.0):
    """Configure Phase 1 settings before running."""
    global UNPAYWALL_EMAIL, ENABLE_UNPAYWALL, API_DELAY, DOWNLOAD_DELAY
    UNPAYWALL_EMAIL = email
    ENABLE_UNPAYWALL = enable_unpaywall
    API_DELAY = api_delay
    DOWNLOAD_DELAY = download_delay


def _safe_filename(text: str, max_len: int = 180) -> str:
    """Sanitize a string for use as filename."""
    import re
    safe = re.sub(r'[\\/:*?"<>|]', "_", text)
    safe = re.sub(r"[_\s]+", "_", safe)
    safe = safe.strip("._ ")
    if len(safe) > max_len:
        safe = safe[:max_len].rsplit("_", 1)[0]
    return safe


def _sha256(filepath: str) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _query_unpaywall(doi: str) -> dict | None:
    """Query Unpaywall API for OA status."""
    if not ENABLE_UNPAYWALL or not UNPAYWALL_EMAIL:
        return None
    url = UNPAYWALL_URL.format(doi) + f"?email={UNPAYWALL_EMAIL}"
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {"is_oa": False}
        if resp.status_code == 422:
            logger.warning("Unpaywall rejected email — set a real email in config")
            return {"is_oa": False}
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"Unpaywall query failed for {doi}: {e}")
        return {"is_oa": False}


def _query_crossref_oa(doi: str) -> list[str]:
    """Query Crossref for OA PDF links."""
    url = CROSSREF_URL.format(doi)
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        msg = data.get("message", {})
        pdf_urls = []

        # Check license for OA indicators
        for lic in msg.get("license", []):
            url_val = lic.get("URL", "")
            if any(tag in url_val.lower() for tag in
                   ["creativecommons", "cc-by", "open-access"]):
                pass  # Marked as OA, now look for links

        # Collect PDF links
        for link in msg.get("link", []):
            content_type = link.get("content-type", "")
            url_val = link.get("URL", "")
            if "pdf" in content_type and url_val:
                pdf_urls.append(url_val)

        return pdf_urls
    except requests.RequestException:
        return []


def _download_file(url: str, filepath: str, max_retries: int = MAX_RETRIES) -> bool:
    """Download a file with retries. Returns True on success."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "html" in content_type and "pdf" not in content_type:
                logger.debug(f"URL returned HTML, not PDF: {url}")
                return False

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Verify it's actually a PDF
            with open(filepath, "rb") as f:
                header = f.read(4)
            if header == b"%PDF":
                return True
            else:
                os.remove(filepath)
                return False

        except requests.RequestException as e:
            logger.debug(f"Download attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
    return False


def _process_single(doi: str, title_from_csv: str = "", papers_dir: Path | None = None) -> dict:
    """
    Process a single DOI through Phase 1.

    Returns a result dict with keys:
        doi, title, first_author, year, journal, filename, status, phase, filepath, message
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    doi_clean = doi.strip()
    status = "processing"
    filepath = ""
    message = ""

    # Get metadata and target filename
    filename = get_filename(doi_clean)
    target_path = papers_dir / filename

    # Check if already downloaded
    if target_path.exists() and target_path.stat().st_size > 1024:
        meta = __import__("backend.metadata", fromlist=["get_metadata"]).get_metadata(doi_clean)
        if meta:
            return {
                "doi": doi_clean, "title": meta.get("title", title_from_csv),
                "first_author": meta.get("first_author", ""),
                "year": meta.get("year", ""),
                "journal": meta.get("journal", ""),
                "filename": filename, "status": "already_downloaded",
                "phase": 1, "filepath": str(target_path), "message": "Previously downloaded"
            }

    # Build metadata dict (from Crossref — batch_prefetch fills cache)
    meta = __import__("backend.metadata", fromlist=["get_metadata"]).get_metadata(doi_clean)
    paper_title = meta["title"] if meta else title_from_csv
    first_author = meta.get("first_author", "") if meta else ""
    year = meta.get("year", "") if meta else ""
    journal = meta.get("journal", "") if meta else ""

    base_result = {
        "doi": doi_clean,
        "title": paper_title,
        "first_author": first_author,
        "year": year,
        "journal": journal,
        "filename": filename,
        "status": status,
        "phase": 1,
        "filepath": "",
        "message": "",
    }

    # Try Unpaywall
    time.sleep(API_DELAY)
    upw = _query_unpaywall(doi_clean)
    if upw and upw.get("is_oa"):
        best = upw.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url") or ""
        if pdf_url:
            time.sleep(DOWNLOAD_DELAY)
            if _download_file(pdf_url, str(target_path)):
                base_result["status"] = "downloaded"
                base_result["phase"] = 1
                base_result["filepath"] = str(target_path)
                base_result["message"] = "Downloaded via Unpaywall"
                return base_result

    # Try Crossref OA links
    time.sleep(API_DELAY)
    cr_pdfs = _query_crossref_oa(doi_clean)
    for pdf_url in cr_pdfs:
        time.sleep(DOWNLOAD_DELAY)
        if _download_file(pdf_url, str(target_path)):
            base_result["status"] = "downloaded"
            base_result["phase"] = 1
            base_result["filepath"] = str(target_path)
            base_result["message"] = "Downloaded via Crossref OA"
            return base_result

    # Not OA
    base_result["status"] = "not_oa"
    base_result["message"] = "No OA PDF found — needs Phase 2 (WebVPN)"
    return base_result


def run_phase1(
    papers: list[dict],
    papers_dir: str = "",
    progress_callback = None,
) -> tuple[list[dict], dict]:
    """
    Run Phase 1 on a list of papers.

    Args:
        papers: list of dicts with at least 'doi' key, optionally 'title'
        papers_dir: Directory to save PDFs (default: BASE_DIR/papers)
        progress_callback: Optional callback(status, data) for real-time updates.

    Returns:
        (results, stats) where results is a list of result dicts and stats
        is a summary dict with counts by status.
    """
    # Resolve papers directory
    if papers_dir:
        local_papers = Path(papers_dir)
    else:
        local_papers = PAPERS_DIR
    local_papers.mkdir(parents=True, exist_ok=True)

    # Pre-fetch all metadata to warm the cache
    dois = [p["doi"].strip() for p in papers]
    logger.info(f"Phase 1: Pre-fetching metadata for {len(dois)} DOIs...")
    if progress_callback:
        progress_callback("phase1_prefetching", {
            "message": f"Pre-fetching metadata for {len(dois)} papers...",
            "total": len(dois),
        })
    batch_prefetch(dois, progress_callback=progress_callback)
    logger.info("Phase 1: Metadata pre-fetch complete")

    results = []
    stats = {"total": len(papers), "downloaded": 0, "not_oa": 0,
             "already_downloaded": 0, "download_failed": 0}

    for i, paper in enumerate(papers):
        doi = paper["doi"].strip()
        title = paper.get("title", "")
        logger.info(f"Phase 1 [{i+1}/{len(papers)}]: {doi}")

        result = _process_single(doi, title, papers_dir=local_papers)
        results.append(result)

        if result["status"] == "downloaded":
            stats["downloaded"] += 1
        elif result["status"] == "already_downloaded":
            stats["already_downloaded"] += 1
        elif result["status"] == "not_oa":
            stats["not_oa"] += 1
        elif result["status"] == "download_failed":
            stats["download_failed"] += 1

        # Emit per-paper progress
        if progress_callback:
            pct = int((i + 1) / len(papers) * 100)
            progress_callback("phase1_progress", {
                "current": i + 1,
                "total": len(papers),
                "pct": pct,
                "doi": doi[:50],
                "status": result["status"],
                "title": result.get("title", "")[:80],
            })

    # Sort: downloaded first, then not_oa, then failed
    status_order = {"downloaded": 0, "already_downloaded": 1,
                    "not_oa": 2, "download_failed": 3}
    results.sort(key=lambda r: status_order.get(r["status"], 99))

    return results, stats


def results_to_csv(results: list[dict], filepath: str) -> None:
    """Write results to a CSV file."""
    fieldnames = ["doi", "title", "first_author", "year", "journal",
                  "filename", "status", "phase", "filepath", "message"]
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
