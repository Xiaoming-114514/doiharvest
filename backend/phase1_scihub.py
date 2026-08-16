"""
Phase 1: Sci-Hub downloader for all papers.

Directly downloads PDFs from Sci-Hub with ALTCHA captcha solving.
No OA pre-check — faster than the old Phase 1 pipeline.

Strategy:
  1. Sci-Hub (3 mirror domains, with captcha auto-solving)
  2. Open Access mirrors (Unpaywall, EuropePMC) as fallback

Uses the official ``altcha`` library for proof-of-work captcha.
"""

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable

import altcha as _altcha
import requests
from bs4 import BeautifulSoup

from .metadata import get_metadata

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

# Sci-Hub domains (tried in order)
SCIHUB_DOMAINS = [
    "https://sci-hub.ru",
    "https://sci-hub.ee",
    "https://sci-hub.st",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/pdf,*/*",
}

_scihub_sessions: dict[str, requests.Session] = {}


# ── Sci-Hub session management ──────────────────────

def _get_session(domain: str) -> requests.Session:
    if domain not in _scihub_sessions:
        s = requests.Session()
        s.headers.update({
            **_HEADERS,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{domain}/",
        })
        _scihub_sessions[domain] = s
    return _scihub_sessions[domain]


# ── Captcha detection & solving ─────────────────────

def _detect_captcha(html: str) -> str | None:
    m = re.search(r'/captcha/solution/(\d+)', html)
    if m:
        return m.group(1)
    if "你是机器人吗" in html or "robot" in html.lower():
        m = re.search(r'/captcha/challenge/(\d+)', html)
        if m:
            return m.group(1)
    return None


def _solve_altcha(
    session: requests.Session, domain: str, challenge_id: str,
    doi_url: str, timeout: int = 15,
) -> bool:
    challenge_url = f"{domain}/captcha/challenge/{challenge_id}"
    try:
        resp = session.get(challenge_url, timeout=timeout)
        if resp.status_code != 200 or not resp.text.strip():
            return False
        challenge_data = resp.json()
    except Exception:
        return False

    alg = challenge_data.get("algorithm", "SHA-256")
    chal = challenge_data.get("challenge", "")
    max_n = challenge_data.get("maxNumber", 200000)
    salt = challenge_data.get("salt", "")
    signature = challenge_data.get("signature", "")

    if not chal or not salt:
        return False

    try:
        ch = _altcha.ChallengeV1(alg, chal, max_n, salt, signature)
        sol = _altcha.solve_challenge_v1(ch, salt, alg, max_n, 0)
        if sol is None:
            return False
        pv = _altcha.PayloadV1(alg, chal, sol.number, salt, signature)
        payload = pv.to_base64()
        logger.debug(f"ALTCHA solved: n={sol.number}")
    except Exception:
        return False

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
            return True
    except Exception:
        pass

    return False


# ── PDF extraction & download ───────────────────────

def _extract_scihub_pdf(
    session: requests.Session, resp: requests.Response,
    domain: str, timeout: int,
) -> str | None:
    soup = BeautifulSoup(resp.text, "html.parser")
    pdf_url = None

    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src", "")
        if src:
            pdf_url = src
            break

    if not pdf_url:
        for tag in soup.find_all(["button", "a"]):
            onclick = tag.get("onclick", "")
            if onclick:
                m = re.search(r"""location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
                              onclick, re.IGNORECASE)
                if m:
                    pdf_url = m.group(1)
                    break
            href = tag.get("href", "")
            if href and ".pdf" in href.lower():
                pdf_url = href
                break

    if not pdf_url:
        pdf_elem = soup.find(id="pdf")
        if pdf_elem:
            pdf_url = pdf_elem.get("src") or pdf_elem.get("data")

    if not pdf_url:
        text = soup.get_text()
        m = re.search(r"""(https?://[^\s"'<>]+\.pdf)""", text)
        if m:
            pdf_url = m.group(1)

    if not pdf_url:
        for tag in soup.find_all(["iframe", "embed"]):
            pdf_url = tag.get("src") or tag.get("data")
            if pdf_url:
                break

    if not pdf_url:
        return None

    if pdf_url.startswith("//"):
        pdf_url = "https:" + pdf_url
    elif pdf_url.startswith("/"):
        pdf_url = domain + pdf_url

    return pdf_url


def _download_pdf(
    session: requests.Session, pdf_url: str, doi: str,
    output_dir: Path, domain: str, timeout: int,
) -> Path | None:
    try:
        pdf_resp = session.get(pdf_url, timeout=timeout, stream=True)
    except Exception:
        return None

    if pdf_resp.status_code != 200:
        return None

    doi_slug = doi.replace("/", "_").replace(".", "-")
    out_path = output_dir / f"_{doi_slug}.pdf"

    with open(out_path, "wb") as f:
        for chunk in pdf_resp.iter_content(8192):
            f.write(chunk)

    with open(out_path, "rb") as f:
        if not f.read(5).startswith(b"%PDF"):
            out_path.unlink()
            return None

    logger.info(f"Sci-Hub: downloaded {doi} via {domain}")
    return out_path


def _scihub_try_domain(
    doi: str, domain: str, output_dir: Path, timeout: int,
) -> Path | None:
    session = _get_session(domain)
    url = f"{domain}/{doi}"

    resp = session.get(url, timeout=timeout)
    if resp.status_code != 200:
        return None

    captcha_id = _detect_captcha(resp.text)
    if captcha_id:
        logger.info(f"Sci-Hub {domain}: captcha detected (id={captcha_id}), solving...")
        if _solve_altcha(session, domain, captcha_id, doi_url=url, timeout=timeout):
            time.sleep(10)
            resp = session.get(url, timeout=timeout, headers={
                "Referer": f"{domain}/captcha/solution/{captcha_id}",
            })
            if resp.status_code != 200 or _detect_captcha(resp.text):
                return None
        else:
            return None

    pdf_url = _extract_scihub_pdf(session, resp, domain, timeout)
    if not pdf_url:
        return None

    return _download_pdf(session, pdf_url, doi, output_dir, domain, timeout)


def _scihub_download(doi: str, output_dir: Path, timeout: int = 60) -> Path | None:
    for domain in SCIHUB_DOMAINS:
        try:
            result = _scihub_try_domain(doi, domain, output_dir, timeout)
            if result:
                return result
        except Exception as e:
            logger.debug(f"Sci-Hub {domain} failed for {doi}: {e}")
    return None


# ── OA mirror fallback ──────────────────────────────

_oa_session: requests.Session | None = None


def _get_oa_session() -> requests.Session:
    global _oa_session
    if _oa_session is None:
        _oa_session = requests.Session()
        _oa_session.headers.update(_HEADERS)
    return _oa_session


def _oa_download(doi: str, output_dir: Path, timeout: int = 30) -> Path | None:
    session = _get_oa_session()

    # Unpaywall
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
    except Exception:
        pass

    # Europe PMC
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
                    doi_slug = doi.replace("/", "_").replace(".", "-")
                    out_path = output_dir / f"_{doi_slug}.pdf"
                    with open(out_path, "wb") as f:
                        for chunk in pdf_resp.iter_content(8192):
                            f.write(chunk)
                    with open(out_path, "rb") as f:
                        if f.read(4) == b"%PDF":
                            logger.info(f"EuropePMC: downloaded {doi}")
                            return out_path
                    out_path.unlink()
    except Exception:
        pass

    return None


# ── Single DOI download ─────────────────────────────

def _download_single(doi: str, output_dir: Path) -> dict:
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


# ── Phase 1 orchestrator ────────────────────────────

def run_phase1(
    dois: list[str],
    output_dir: str = "",
    progress_callback: Callable | None = None,
) -> tuple[list[dict], dict]:
    """Run Phase 1 (Sci-Hub) on a list of DOIs.

    Returns (results, stats).
    """
    if output_dir:
        out_root = Path(output_dir).resolve()
    else:
        out_root = BASE_DIR

    phase1_dir = out_root / "phase1_output"
    papers_dir = out_root / "papers"
    phase1_dir.mkdir(parents=True, exist_ok=True)
    papers_dir.mkdir(parents=True, exist_ok=True)

    if not dois:
        return [], {"total": 0, "downloaded": 0, "download_failed": 0}

    total = len(dois)
    logger.info(f"Phase 1 (Sci-Hub): Processing {total} DOIs")

    if progress_callback:
        progress_callback("phase1_start", {"total": total})

    all_results = []
    stats = {"total": total, "downloaded": 0, "download_failed": 0}

    for i, doi in enumerate(dois):
        logger.info(f"Phase 1 [{i+1}/{total}]: {doi}")
        result = _download_single(doi, phase1_dir)

        if result["status"] == "downloaded":
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
            progress_callback("phase1_progress", {
                "current": i + 1, "total": total,
                "downloaded": stats["downloaded"],
                "failed": stats["download_failed"],
                "doi": doi,
                "status": result["status"],
                "route": result.get("route", "none"),
            })

        if i < total - 1:
            time.sleep(10)

    logger.info(f"Phase 1 complete: {stats}")
    return all_results, stats
