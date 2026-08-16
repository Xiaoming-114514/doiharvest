"""
Phase 2: Multi-source downloader for non-OA papers.

Tries multiple sources in order:
  1. Sci-Hub (primary source for paywalled papers)
  2. Open Access mirrors (unpaywall, EuropePMC)
  3. Direct DOI resolution (may work on-campus)

No browser automation, no Chrome CDP, no Node.js required.
WebVPN login is optional — use Step 1 button to open BJMU login page
in your default browser if institutional access is needed.

The downloader also auto-retries with captcha handling for Sci-Hub.
"""

import logging
import os
import re
import shutil
import time
import webbrowser
from pathlib import Path
from typing import Callable

import altcha as _altcha
import requests
from bs4 import BeautifulSoup

from .metadata import get_metadata

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
PAPERS_DIR = BASE_DIR / "papers"
PHASE2_DIR = BASE_DIR / "phase2_output"

# BJMU WebVPN config
BJMU_PORTAL = "https://webvpn.bjmu.edu.cn/"

# Sci-Hub domains (tried in order)
# .ru works with Python SSL; .st uses DDoS-Guard which returns 403 for Python
SCIHUB_DOMAINS = [
    "https://sci-hub.ru",
    "https://sci-hub.ee",
    "https://sci-hub.st",
]

# Common headers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/pdf,*/*",
}

# Cached authenticated Sci-Hub sessions per domain
_scihub_sessions: dict[str, requests.Session] = {}


# ── WebVPN (Step 1) ─────────────────────────────────

def open_webvpn_login() -> bool:
    """Open the BJMU WebVPN login page in the default browser.

    The user completes CAS login manually.  We don't extract cookies
    because the main download strategy is Sci-Hub, not WebVPN.
    """
    try:
        webbrowser.open(BJMU_PORTAL)
        logger.info(f"Opened browser: {BJMU_PORTAL}")
        return True
    except Exception as e:
        logger.error(f"Failed to open browser: {e}")
        return False


# ── Sci-Hub download with ALTCHA captcha solving ────

def _get_session(domain: str) -> requests.Session:
    """Get or create a requests.Session for a Sci-Hub domain."""
    if domain not in _scihub_sessions:
        s = requests.Session()
        s.headers.update({
            **_HEADERS,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{domain}/",
        })
        _scihub_sessions[domain] = s
    return _scihub_sessions[domain]


def _detect_captcha(html: str) -> str | None:
    """Detect if the page is a captcha challenge. Returns challenge ID or None."""
    m = re.search(r'/captcha/solution/(\d+)', html)
    if m:
        return m.group(1)
    # Fallback: search for the robot text + challenge URL
    if "你是机器人吗" in html or "robot" in html.lower():
        m = re.search(r'/captcha/challenge/(\d+)', html)
        if m:
            return m.group(1)
    return None


def _solve_altcha(session: requests.Session, domain: str, challenge_id: str, doi_url: str, timeout: int = 15) -> bool:
    """Solve ALTCHA proof-of-work challenge using the official altcha library.

    Returns True if the solution was accepted by Sci-Hub.
    """
    # Step 1: Fetch the challenge
    challenge_url = f"{domain}/captcha/challenge/{challenge_id}"
    try:
        resp = session.get(challenge_url, timeout=timeout)
        if resp.status_code != 200 or not resp.text.strip():
            logger.debug(f"ALTCHA challenge fetch failed: HTTP {resp.status_code}")
            return False
        challenge_data = resp.json()
    except Exception as e:
        logger.debug(f"ALTCHA challenge error: {e}")
        return False

    alg = challenge_data.get("algorithm", "SHA-256")
    chal = challenge_data.get("challenge", "")
    max_n = challenge_data.get("maxNumber", 200000)
    salt = challenge_data.get("salt", "")
    signature = challenge_data.get("signature", "")

    if not chal or not salt:
        logger.debug("ALTCHA challenge missing required fields")
        return False

    # Step 2: Solve using the official altcha library (correct payload encoding)
    try:
        ch = _altcha.ChallengeV1(alg, chal, max_n, salt, signature)
        sol = _altcha.solve_challenge_v1(ch, salt, alg, max_n, 0)
        if sol is None:
            logger.debug("ALTCHA solve returned None")
            return False
        pv = _altcha.PayloadV1(alg, chal, sol.number, salt, signature)
        payload = pv.to_base64()
        logger.debug(f"ALTCHA solved: n={sol.number}")
    except Exception as e:
        logger.debug(f"ALTCHA solve error: {e}")
        return False

    # Step 3: Submit solution with proper Origin and Referer headers
    solution_url = f"{domain}/captcha/solution/{challenge_id}"
    try:
        resp = session.post(
            solution_url,
            json={"captcha": payload},
            headers={
                "Content-Type": "application/json",
                "Origin": domain,
                "Referer": doi_url,
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            try:
                result = resp.json()
                if result.get("success"):
                    logger.info(f"ALTCHA captcha solved successfully for {domain}")
                    return True
                logger.debug(f"ALTCHA solution rejected: {result}")
            except Exception:
                # Some servers return empty 200 → also success
                logger.info(f"ALTCHA solution submitted (HTTP 200)")
                return True
        else:
            logger.debug(f"ALTCHA solution failed: HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"ALTCHA solution POST error: {e}")

    return False


def _scihub_download(doi: str, output_dir: Path, timeout: int = 60) -> Path | None:
    """Try to download a PDF from Sci-Hub with captcha handling.

    Returns the path to the downloaded PDF, or None on failure.
    """
    for domain in SCIHUB_DOMAINS:
        try:
            result = _scihub_try_domain(doi, domain, output_dir, timeout)
            if result:
                return result
        except Exception as e:
            logger.debug(f"Sci-Hub {domain} failed for {doi}: {e}")
    return None


def _scihub_try_domain(doi: str, domain: str, output_dir: Path, timeout: int) -> Path | None:
    """Try a single Sci-Hub domain with captcha solving."""
    session = _get_session(domain)
    url = f"{domain}/{doi}"

    resp = session.get(url, timeout=timeout)
    if resp.status_code != 200:
        logger.debug(f"Sci-Hub {domain}: HTTP {resp.status_code} for {doi}")
        return None

    # Check for captcha page — solve with official altcha library + correct headers
    captcha_id = _detect_captcha(resp.text)
    if captcha_id:
        logger.info(f"Sci-Hub {domain}: captcha detected (id={captcha_id}), solving...")
        if _solve_altcha(session, domain, captcha_id, doi_url=url, timeout=timeout):
            # Retry with Referer pointing to captcha solution page
            time.sleep(1)
            resp = session.get(url, timeout=timeout, headers={
                "Referer": f"{domain}/captcha/solution/{captcha_id}",
            })
            if resp.status_code != 200:
                logger.debug(f"Sci-Hub {domain}: HTTP {resp.status_code} after captcha for {doi}")
                return None
            # Double-check we're not still on captcha
            if _detect_captcha(resp.text):
                logger.warning(f"Sci-Hub {domain}: still on captcha page after solving")
                return None
        else:
            logger.warning(f"Sci-Hub {domain}: captcha solve failed for {doi}")
            return None

    # Parse the page for PDF
    pdf_url = _extract_scihub_pdf(session, resp, domain, timeout)
    if not pdf_url:
        logger.debug(f"Sci-Hub {domain}: no PDF URL found for {doi}")
        return None

    # Download the PDF
    return _download_pdf(session, pdf_url, doi, output_dir, domain, timeout)


def _extract_scihub_pdf(
    session: requests.Session,
    resp: requests.Response,
    domain: str,
    timeout: int,
) -> str | None:
    """Extract PDF URL from a Sci-Hub paper page."""
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_url = None

    # Method 1: iframe/embed with PDF
    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src", "")
        if src:
            pdf_url = src
            break

    # Method 2: Button/links with onclick="location.href='...'"
    if not pdf_url:
        for tag in soup.find_all(["button", "a"]):
            onclick = tag.get("onclick", "")
            if onclick:
                m = re.search(r"""location(?:\\.href)?\s*=\s*['"]([^'"]+)['"]""", onclick, re.IGNORECASE)
                if m:
                    pdf_url = m.group(1)
                    break
            href = tag.get("href", "")
            if href and ".pdf" in href.lower():
                pdf_url = href
                break

    # Method 3: #pdf element
    if not pdf_url:
        pdf_elem = soup.find(id="pdf")
        if pdf_elem:
            pdf_url = pdf_elem.get("src") or pdf_elem.get("data")

    # Method 4: Text-based PDF URL extraction
    if not pdf_url:
        text = soup.get_text()
        m = re.search(r"""(https?://[^\s"'<>]+\.pdf)""", text)
        if m:
            pdf_url = m.group(1)

    if not pdf_url:
        # Method 5: Look for any embed/iframe even without pdf in src
        for tag in soup.find_all(["iframe", "embed"]):
            pdf_url = tag.get("src") or tag.get("data")
            if pdf_url:
                break

    if not pdf_url:
        return None

    # Make URL absolute
    if pdf_url.startswith("//"):
        pdf_url = "https:" + pdf_url
    elif pdf_url.startswith("/"):
        pdf_url = domain + pdf_url

    return pdf_url


def _download_pdf(
    session: requests.Session,
    pdf_url: str,
    doi: str,
    output_dir: Path,
    domain: str,
    timeout: int,
) -> Path | None:
    """Download a PDF from the given URL and verify it's valid."""
    try:
        pdf_resp = session.get(pdf_url, timeout=timeout, stream=True)
    except Exception as e:
        logger.debug(f"PDF download error: {e}")
        return None

    if pdf_resp.status_code != 200:
        logger.debug(f"PDF download failed: HTTP {pdf_resp.status_code}")
        return None

    # Save to temp path
    doi_slug = doi.replace("/", "_").replace(".", "-")
    out_path = output_dir / f"_{doi_slug}.pdf"

    with open(out_path, "wb") as f:
        for chunk in pdf_resp.iter_content(8192):
            f.write(chunk)

    # Verify it's a valid PDF (first 4 bytes = %PDF)
    with open(out_path, "rb") as f:
        header = f.read(5)
        if not header.startswith(b"%PDF"):
            out_path.unlink()
            logger.debug(f"Sci-Hub {domain}: downloaded file is not a PDF (header={header[:20]})")
            return None

    logger.info(f"Sci-Hub: downloaded {doi} via {domain}")
    return out_path


# ── OA mirror download ──────────────────────────────

_oa_session: requests.Session | None = None


def _get_oa_session() -> requests.Session:
    """Get or create a session for OA mirror requests."""
    global _oa_session
    if _oa_session is None:
        _oa_session = requests.Session()
        _oa_session.headers.update(_HEADERS)
    return _oa_session


def _oa_download(doi: str, output_dir: Path, timeout: int = 30) -> Path | None:
    """Try to find an OA version via unpaywall and other sources."""
    session = _get_oa_session()

    # Try unpaywall
    try:
        email = "doiharvest@example.com"
        url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
        resp = session.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            oa_loc = data.get("best_oa_location")
            if oa_loc and oa_loc.get("url_for_pdf"):
                pdf_url = oa_loc["url_for_pdf"]
                pdf_resp = session.get(pdf_url, timeout=timeout, stream=True)
                if pdf_resp.status_code == 200:
                    ct = pdf_resp.headers.get("Content-Type", "")
                    if "pdf" in ct.lower():
                        doi_slug = doi.replace("/", "_").replace(".", "-")
                        out_path = output_dir / f"_{doi_slug}_oa.pdf"
                        with open(out_path, "wb") as f:
                            for chunk in pdf_resp.iter_content(8192):
                                f.write(chunk)
                        logger.info(f"OA mirror: downloaded {doi}")
                        return out_path
    except Exception as e:
        logger.debug(f"Unpaywall failed for {doi}: {e}")

    # Try Europe PMC for life sciences papers
    try:
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{doi}/fullTextXML"
        resp = session.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            pdf_url = (data.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0]
                       .get("url", ""))
            if pdf_url:
                pdf_resp = session.get(pdf_url, timeout=timeout, stream=True)
                if pdf_resp.status_code == 200:
                    ct = pdf_resp.headers.get("Content-Type", "")
                    if "pdf" in ct.lower() or True:  # Accept regardless
                        doi_slug = doi.replace("/", "_").replace(".", "-")
                        out_path = output_dir / f"_{doi_slug}.pdf"
                        with open(out_path, "wb") as f:
                            for chunk in pdf_resp.iter_content(8192):
                                f.write(chunk)
                        # Verify
                        with open(out_path, "rb") as f:
                            if f.read(4) == b"%PDF":
                                logger.info(f"EuropePMC: downloaded {doi}")
                                return out_path
                        out_path.unlink()
    except Exception as e:
        logger.debug(f"EuropePMC failed for {doi}: {e}")

    return None


# ── Main download function ──────────────────────────

def _download_single(doi: str, output_dir: Path) -> dict:
    """Download a single DOI, trying all strategies."""
    result = {
        "doi": doi,
        "title": "",
        "status": "download_failed",
        "route": "none",
        "filepath": "",
        "filename": "",
        "message": "",
    }

    strategies = [
        ("scihub", _scihub_download),
        ("oa_mirror", _oa_download),
    ]

    for strategy_name, strategy_fn in strategies:
        try:
            pdf_path = strategy_fn(doi, output_dir)
            if pdf_path:
                result["status"] = "downloaded"
                result["route"] = strategy_name
                result["filepath"] = str(pdf_path)
                result["filename"] = pdf_path.name
                return result
        except Exception as e:
            logger.debug(f"Strategy {strategy_name} failed for {doi}: {e}")

    result["message"] = "All download strategies failed"
    return result


# ── Phase 2 orchestrator ────────────────────────────

def run_phase2(
    dois: list[str],
    output_dir: str = "",
    progress_callback: Callable | None = None,
) -> tuple[list[dict], dict]:
    """
    Run Phase 2 on a list of non-OA DOIs.

    Tries Sci-Hub first, then OA mirrors.  No browser automation required.
    WebVPN login is handled separately via open_webvpn_login().

    Args:
        dois: list of DOI strings
        output_dir: Root output directory
        progress_callback: Optional callback(status, data)

    Returns:
        (results, stats) — each result has: doi, title, status, route, filepath, filename, message
    """
    if output_dir:
        out_root = Path(output_dir).resolve()
    else:
        out_root = BASE_DIR

    phase2_dir = out_root / "phase2_output"
    papers_dir = out_root / "papers"
    phase2_dir.mkdir(parents=True, exist_ok=True)
    papers_dir.mkdir(parents=True, exist_ok=True)

    if not dois:
        return [], {"total": 0, "downloaded": 0, "download_failed": 0}

    logger.info(f"Phase 2: Processing {len(dois)} DOIs (multi-source)")
    total = len(dois)

    if progress_callback:
        progress_callback("phase2_start", {"non_oa_count": total})

    all_results = []
    stats = {"total": total, "downloaded": 0, "download_failed": 0}

    for i, doi in enumerate(dois):
        logger.info(f"Phase 2 [{i+1}/{total}]: {doi}")
        result = _download_single(doi, phase2_dir)

        if result["status"] == "downloaded":
            # Copy to papers directory with proper name
            meta = get_metadata(doi)
            if meta:
                target_name = meta.get("filename", result["filename"])
                result["title"] = meta.get("title", "")
            else:
                target_name = result["filename"]

            target_path = papers_dir / target_name
            try:
                shutil.copy2(result["filepath"], str(target_path))
                result["filepath"] = str(target_path)
                result["filename"] = target_name
                stats["downloaded"] += 1
            except OSError as e:
                logger.error(f"Copy failed: {e}")
                result["status"] = "download_failed"
                result["message"] = f"Copy error: {e}"
                stats["download_failed"] += 1
        else:
            stats["download_failed"] += 1

        all_results.append(result)

        if progress_callback:
            progress_callback("phase2_progress", {
                "batch": i + 1, "total_batches": total,
                "batch_size": 1,
                "batch_downloaded": stats["downloaded"],
                "batch_failed": stats["download_failed"],
                "total_downloaded": stats["downloaded"],
                "total_processed": i + 1,
                "total_dois": total,
            })

        # Brief delay between requests to be polite
        if i < total - 1:
            time.sleep(1)

    logger.info(f"Phase 2 complete: {stats}")
    return all_results, stats


# ── Backward-compat stubs ───────────────────────────

def _find_scansci_cli() -> bool:
    """Phase 2 no longer requires scansci-pdf.  Always True."""
    return True


def setup_school(school_name: str = "") -> bool:
    """No-op: scansci-pdf is not used."""
    return True


def start_webvpn_session() -> bool:
    """Open WebVPN login page in default browser."""
    return open_webvpn_login()


def _has_node() -> bool:
    """No longer required."""
    return True


def _check_cdp_proxy() -> bool:
    """No longer required."""
    return True


def start_cdp_proxy(*args, **kwargs) -> bool:
    """No-op."""
    return True


def get_cdp_proxy_output(*args, **kwargs) -> str:
    return "(multi-source mode — no CDP proxy)"


def _stop_cdp_proxy() -> None:
    pass
