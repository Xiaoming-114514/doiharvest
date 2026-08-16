#!/usr/bin/env python3
"""
DoiHarvest OA Downloader - Phase 1
===================================
Reads a CSV of DOIs, queries Unpaywall + Crossref for Open Access PDFs,
downloads them, and produces a clean list of non-OA DOIs for Phase 2 (WebVPN).

Usage:
    python oa_downloader.py                    # run with config.py defaults
    python oa_downloader.py --input my.csv     # override input file
    python oa_downloader.py --resume            # skip already downloaded PDFs
    python oa_downloader.py --stats             # show download statistics only
"""

import csv
import os
import re
import sys
import time
import json
import logging
import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm

try:
    import config as cfg
except ImportError:
    print("Error: config.py not found. Make sure you're running from the project directory.")
    sys.exit(1)


# ============================================================
# Logging Setup
# ============================================================
def setup_logging():
    os.makedirs(cfg.LOGS_DIR, exist_ok=True)
    log_file = os.path.join(
        cfg.LOGS_DIR, f"oa_download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


# ============================================================
# CSV Reading
# ============================================================
def read_csv_doi_list(csv_path):
    """
    Read DOIs from a CSV file.
    Returns a list of dicts: [{"doi": "...", "title": "...", "row": N}, ...]
    Handles various column name conventions (DOI, Doi, doi, etc.).
    """
    papers = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            # Try to detect the delimiter
            sample = f.read(2048)
            f.seek(0)
            delimiter = "\t" if "\t" in sample else ","
            reader = csv.DictReader(f, delimiter=delimiter)

            # Find the DOI column (case-insensitive)
            doi_col = None
            title_col = None
            for col in reader.fieldnames or []:
                col_clean = col.strip()
                if col_clean.lower() == "doi":
                    doi_col = col
                elif col_clean.lower() in ("title", "article title", "articletitle", "paper title"):
                    title_col = col

            if not doi_col:
                logging.error(
                    f"No 'DOI' column found in CSV. Columns: {reader.fieldnames}"
                )
                return []

            for i, row in enumerate(reader):
                doi = (row.get(doi_col) or "").strip()
                title = (row.get(title_col) or "").strip() if title_col else ""
                if doi:
                    # Normalize DOI: remove URL prefix if present
                    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
                    papers.append({"doi": doi, "title": title, "row": i})
                else:
                    # No DOI, will be marked for Phase 2
                    papers.append({"doi": "", "title": title, "row": i})

    except FileNotFoundError:
        logging.error(f"CSV file not found: {csv_path}")
        return []
    except Exception as e:
        logging.error(f"Error reading CSV: {e}")
        return []

    has_doi = sum(1 for p in papers if p["doi"])
    no_doi = sum(1 for p in papers if not p["doi"])
    logging.info(f"Read {len(papers)} papers from CSV ({has_doi} with DOI, {no_doi} without DOI)")
    return papers


# ============================================================
# Unpaywall API
# ============================================================
def query_unpaywall(doi, email):
    """
    Query Unpaywall API for a single DOI.
    Returns dict with OA info, or None if query fails.
    """
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    try:
        resp = requests.get(url, timeout=cfg.TIMEOUT, proxies=cfg.PROXIES)
        if resp.status_code == 404:
            return {"is_oa": False, "oa_locations": [], "best_oa_location": None,
                    "message": "DOI not found in Unpaywall"}
        if resp.status_code == 422:
            # Unpaywall rejects fake/blacklisted emails
            logging.warning(f"Unpaywall 422: likely invalid email '{email}'. "
                            "Set a real email in config.py: UNPAYWALL_EMAIL")
            return None
        if resp.status_code == 429:
            logging.warning("Unpaywall rate limit hit, waiting 10s...")
            time.sleep(10)
            return query_unpaywall(doi, email)  # retry once
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        logging.warning(f"Unpaywall timeout for {doi}")
        return None
    except Exception as e:
        logging.warning(f"Unpaywall error for {doi}: {e}")
        return None


def get_best_pdf_url(unpaywall_data):
    """
    Extract the best PDF URL from Unpaywall response.
    Returns (pdf_url, oa_source) or (None, None).
    """
    if not unpaywall_data:
        return None, None

    best = unpaywall_data.get("best_oa_location")
    if best and best.get("url_for_pdf"):
        return best["url_for_pdf"], best.get("host_type", "unknown")

    # Try all OA locations if best doesn't have a direct PDF URL
    for loc in unpaywall_data.get("oa_locations", []):
        if loc.get("url_for_pdf"):
            return loc["url_for_pdf"], loc.get("host_type", "unknown")

    return None, None


# ============================================================
# Crossref API (Fallback)
# ============================================================
def query_crossref(doi):
    """
    Query Crossref API for a single DOI.
    Looks for OA full-text links in the response.
    Returns (pdf_url, license_url) or (None, None).
    """
    url = f"https://api.crossref.org/works/{doi}"
    headers = {
        "User-Agent": f"DoiHarvestOA/1.0 (mailto:{cfg.UNPAYWALL_EMAIL})"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=cfg.TIMEOUT, proxies=cfg.PROXIES)
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        data = resp.json().get("message", {})

        # Check license field for OA indicators
        licenses = data.get("license", [])
        is_oa_license = any(
            "creativecommons" in lic.get("URL", "").lower()
            for lic in licenses
        )

        # Check link array for PDF links
        for link in data.get("link", []):
            if link.get("content-type") == "application/pdf":
                pdf_url = link.get("URL")
                if pdf_url and is_oa_license:
                    return pdf_url, "crossref_oa"

        # If no PDF link but has CC license, return landing page
        if is_oa_license and data.get("URL"):
            return data["URL"], "crossref_landing"

        return None, None
    except Exception as e:
        logging.debug(f"Crossref error for {doi}: {e}")
        return None, None


# ============================================================
# PDF Download
# ============================================================
def sanitize_filename(text, max_len=80):
    """Clean text for use as a filename."""
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text[:max_len] if len(text) > max_len else text


def make_filename(doi, title, naming_mode=cfg.FILE_NAMING):
    """Generate a PDF filename based on the naming mode."""
    doi_safe = doi.replace("/", "_").replace("\\", "_")
    title_safe = sanitize_filename(title) if title else ""

    if naming_mode == "doi" or not title_safe:
        return f"{doi_safe}.pdf"
    elif naming_mode == "title":
        return f"{title_safe}.pdf"
    elif naming_mode == "doi_title":
        return f"{doi_safe}_{title_safe}.pdf"
    else:
        return f"{doi_safe}.pdf"


def is_already_downloaded(doi, title, papers_dir):
    """Check if a PDF for this DOI has already been downloaded."""
    doi_safe = doi.replace("/", "_").replace("\\", "_")
    # Check all possible naming patterns
    patterns = [
        f"{doi_safe}.pdf",
        f"{doi_safe}_*.pdf" if title else None,
    ]
    for pattern in patterns:
        if pattern is None:
            continue
        matches = list(Path(papers_dir).glob(pattern))
        if matches:
            return str(matches[0])
    # Also check by DOI hash for safety
    return None


def download_pdf(url, filepath, source, max_retries=cfg.MAX_RETRIES):
    """
    Download a PDF from a URL with retry logic.
    Returns True on success, False on failure.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                url, headers=headers, timeout=cfg.TIMEOUT,
                proxies=cfg.PROXIES, stream=True, allow_redirects=True
            )
            resp.raise_for_status()

            # Verify it's actually a PDF (check magic bytes)
            first_chunk = next(resp.iter_content(chunk_size=1024), b"")
            if not first_chunk.startswith(b"%PDF"):
                # Some OA links redirect to HTML landing pages
                logging.debug(f"Not a PDF response from {url} (attempt {attempt})")
                if attempt < max_retries:
                    time.sleep(cfg.RETRY_DELAY)
                    continue
                return False

            # Write the file
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "wb") as f:
                f.write(first_chunk)
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            # Verify file size (> 1KB to avoid empty/error pages)
            file_size = os.path.getsize(filepath)
            if file_size < 1024:
                os.remove(filepath)
                logging.debug(f"File too small ({file_size}B), likely error page")
                if attempt < max_retries:
                    time.sleep(cfg.RETRY_DELAY)
                    continue
                return False

            return True

        except requests.exceptions.Timeout:
            logging.debug(f"Download timeout (attempt {attempt}/{max_retries})")
        except requests.exceptions.RequestException as e:
            logging.debug(f"Download error (attempt {attempt}/{max_retries}): {e}")
        except Exception as e:
            logging.debug(f"Unexpected error (attempt {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            time.sleep(cfg.RETRY_DELAY)

    return False


# ============================================================
# Main Processing Logic
# ============================================================
def process_single_paper(paper, email, papers_dir, stats):
    """
    Process a single paper: check OA, download if available.
    Returns a result dict with status.
    """
    doi = paper["doi"]
    title = paper["title"]

    # No DOI -> goes to Phase 2
    if not doi:
        return {
            "doi": "", "title": title, "status": "no_doi",
            "oa_source": "", "filepath": "", "message": "No DOI, needs Phase 2"
        }

    # Check if already downloaded (resume mode)
    existing = is_already_downloaded(doi, title, papers_dir)
    if existing:
        stats["skipped"] += 1
        return {
            "doi": doi, "title": title, "status": "already_downloaded",
            "oa_source": "", "filepath": existing, "message": "Skipped (already exists)"
        }

    filename = make_filename(doi, title)
    filepath = os.path.join(papers_dir, filename)

    # --- Step 1: Try Unpaywall (if enabled) ---
    if cfg.ENABLE_UNPAYWALL:
        time.sleep(cfg.API_DELAY)
        upw_data = query_unpaywall(doi, email)

        if upw_data:
            pdf_url, oa_source = get_best_pdf_url(upw_data)
            is_oa = upw_data.get("is_oa", False)

            if pdf_url:
                # Found OA PDF via Unpaywall, download it
                time.sleep(cfg.DOWNLOAD_DELAY)
                success = download_pdf(pdf_url, filepath, oa_source)
                if success:
                    stats["downloaded"] += 1
                    return {
                        "doi": doi, "title": title, "status": "downloaded",
                        "oa_source": f"unpaywall/{oa_source}", "filepath": filepath,
                        "message": "Downloaded via Unpaywall"
                    }
                else:
                    stats["download_failed"] += 1
                    return {
                        "doi": doi, "title": title, "status": "download_failed",
                        "oa_source": f"unpaywall/{oa_source}", "filepath": "",
                        "message": f"OA PDF found but download failed: {pdf_url}"
                    }
            elif is_oa:
                # OA but no direct PDF URL (e.g., only HTML full text)
                stats["oa_no_pdf"] += 1
                return {
                    "doi": doi, "title": title, "status": "oa_no_pdf",
                    "oa_source": "unpaywall", "filepath": "",
                    "message": "OA but no direct PDF link, needs Phase 2"
                }

    # --- Step 2: Try Crossref (if enabled and Unpaywall didn't find OA) ---
    if cfg.ENABLE_CROSSREF:
        time.sleep(cfg.CROSSREF_DELAY)
        cr_url, cr_source = query_crossref(doi)
        if cr_url and cr_source == "crossref_oa":
            time.sleep(cfg.DOWNLOAD_DELAY)
            success = download_pdf(cr_url, filepath, cr_source)
            if success:
                stats["downloaded"] += 1
                return {
                    "doi": doi, "title": title, "status": "downloaded",
                    "oa_source": "crossref", "filepath": filepath,
                    "message": "Downloaded via Crossref"
                }

    # --- Step 3: Not OA, needs Phase 2 ---
    stats["not_oa"] += 1
    return {
        "doi": doi, "title": title, "status": "not_oa",
        "oa_source": "", "filepath": "",
        "message": "No OA version found, needs Phase 2 (WebVPN)"
    }


def run_download(csv_path, resume=False):
    """Main entry point for the download process."""
    log_file = setup_logging()
    logging.info("=" * 60)
    logging.info("DoiHarvest OA Downloader - Phase 1")
    logging.info(f"Input CSV: {csv_path}")
    logging.info(f"Output PDFs: {cfg.PAPERS_DIR}")
    logging.info(f"Log file: {log_file}")
    logging.info(f"Unpaywall email: {cfg.UNPAYWALL_EMAIL}")
    logging.info(f"Crossref enabled: {cfg.ENABLE_CROSSREF}")
    logging.info(f"Resume mode: {resume}")
    logging.info("=" * 60)

    # Validate email (only if Unpaywall is enabled)
    if cfg.ENABLE_UNPAYWALL:
        fake_emails = ("your_email@example.com", "test@example.com", "email@example.com",
                       "user@example.com", "test@test.com", "")
        if not cfg.UNPAYWALL_EMAIL or cfg.UNPAYWALL_EMAIL.strip() in fake_emails:
            logging.error(
                "Invalid email in config.py (UNPAYWALL_EMAIL). "
                "Please set your real email before running.\n"
                "Unpaywall is free and only requires an email for identification.\n"
                "Register at: https://unpaywall.org/products/api\n"
                "Or set ENABLE_UNPAYWALL = False to use Crossref only."
            )
            sys.exit(1)
    else:
        logging.info("Unpaywall disabled. Will use Crossref only for OA detection.")

    # Read CSV
    papers = read_csv_doi_list(csv_path)
    if not papers:
        logging.error("No papers found in CSV. Exiting.")
        return

    # Create output directories
    os.makedirs(cfg.PAPERS_DIR, exist_ok=True)
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)

    # Stats
    stats = {
        "total": len(papers),
        "downloaded": 0,
        "skipped": 0,
        "not_oa": 0,
        "oa_no_pdf": 0,
        "download_failed": 0,
    }

    # Results list
    results = []

    # Process papers sequentially (API rate limit) with progress bar
    logging.info(f"Processing {len(papers)} papers...")
    for paper in tqdm(papers, desc="OA Download", unit="paper"):
        result = process_single_paper(paper, cfg.UNPAYWALL_EMAIL, cfg.PAPERS_DIR, stats)
        results.append(result)

        # Log every 100 papers
        if (len(results) % 100) == 0:
            logging.info(
                f"Progress: {len(results)}/{len(papers)} | "
                f"Downloaded: {stats['downloaded']} | "
                f"Not OA: {stats['not_oa']} | "
                f"Failed: {stats['download_failed']} | "
                f"Skipped: {stats['skipped']}"
            )

    # --- Write output CSVs ---
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. Full results CSV
    full_csv = os.path.join(cfg.OUTPUT_DIR, f"results_full_{timestamp}.csv")
    with open(full_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "doi", "title", "status", "oa_source", "filepath", "message"
        ])
        writer.writeheader()
        writer.writerows(results)
    logging.info(f"Full results written to: {full_csv}")

    # 2. Non-OA list for Phase 2
    not_oa_list = [r for r in results if r["status"] in ("not_oa", "oa_no_pdf", "download_failed", "no_doi")]
    phase2_csv = os.path.join(cfg.OUTPUT_DIR, f"phase2_dois_{timestamp}.csv")
    with open(phase2_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["doi", "title", "status", "message"],
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(not_oa_list)
    logging.info(f"Phase 2 DOI list ({len(not_oa_list)} papers): {phase2_csv}")

    # 3. Successfully downloaded list
    downloaded_list = [r for r in results if r["status"] in ("downloaded", "already_downloaded")]
    downloaded_csv = os.path.join(cfg.OUTPUT_DIR, f"downloaded_{timestamp}.csv")
    with open(downloaded_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["doi", "title", "oa_source", "filepath"],
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(downloaded_list)
    logging.info(f"Downloaded list ({len(downloaded_list)} papers): {downloaded_csv}")

    # --- Print summary ---
    logging.info("=" * 60)
    logging.info("PHASE 1 SUMMARY")
    logging.info("=" * 60)
    logging.info(f"Total papers:          {stats['total']}")
    logging.info(f"OA downloaded:         {stats['downloaded']}")
    logging.info(f"Already downloaded:    {stats['skipped']}")
    logging.info(f"OA but no PDF link:    {stats['oa_no_pdf']}")
    logging.info(f"Download failed:       {stats['download_failed']}")
    logging.info(f"Not OA (needs Phase2): {stats['not_oa']}")
    logging.info(f"")
    oa_count = stats["downloaded"] + stats["skipped"]
    coverage = (oa_count / stats["total"] * 100) if stats["total"] > 0 else 0
    phase2_count = stats["not_oa"] + stats["oa_no_pdf"] + stats["download_failed"]
    logging.info(f"OA coverage:           {oa_count}/{stats['total']} ({coverage:.1f}%)")
    logging.info(f"Phase 2 candidates:    {phase2_count}")
    logging.info(f"")
    logging.info(f"Phase 2 input file:    {phase2_csv}")
    logging.info("=" * 60)


def show_stats():
    """Show statistics from existing downloads."""
    papers_dir = Path(cfg.PAPERS_DIR)
    pdf_count = len(list(papers_dir.glob("*.pdf"))) if papers_dir.exists() else 0

    output_dir = Path(cfg.OUTPUT_DIR)
    result_files = sorted(output_dir.glob("results_full_*.csv")) if output_dir.exists() else []

    print(f"\nCurrent download statistics:")
    print(f"  PDFs in {cfg.PAPERS_DIR}/: {pdf_count}")
    print(f"  Result files: {len(result_files)}")

    if result_files:
        latest = result_files[-1]
        print(f"  Latest result file: {latest}")

        with open(latest, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            status_counts = {}
            for row in rows:
                s = row.get("status", "unknown")
                status_counts[s] = status_counts.get(s, 0) + 1
            print(f"\n  Status breakdown ({len(rows)} papers):")
            for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
                print(f"    {s:30s} {c:5d}")


# ============================================================
# CLI Entry Point
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="DoiHarvest OA Downloader - Phase 1 (Unpaywall + Crossref)"
    )
    parser.add_argument(
        "--input", "-i",
        default=cfg.INPUT_CSV,
        help=f"Input CSV file (default: {cfg.INPUT_CSV})"
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        help="Skip already downloaded PDFs"
    )
    parser.add_argument(
        "--stats", "-s",
        action="store_true",
        help="Show download statistics only"
    )

    args = parser.parse_args()

    if args.stats:
        show_stats()
    else:
        run_download(args.input, resume=args.resume)


if __name__ == "__main__":
    main()
