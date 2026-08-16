"""
Metadata resolution and safe filename generation.

Queries Crossref for Author, Year, Journal, Title.
Caches results in cache/metadata_cache.json to avoid repeated API calls.
Batch mode uses concurrent requests (ThreadPoolExecutor) for speed.
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_FILE = CACHE_DIR / "metadata_cache.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Crossref polite pool: 50 req/s. With 6 workers and 0.2s per-thread delay,
# max throughput = 6/0.2 = 30 req/s — well within the polite limit.
CROSSREF_DELAY = 0.2
CROSSREF_TIMEOUT = 15
BATCH_WORKERS = 6

# Crossref API polite pool endpoint
CROSSREF_URL = "https://api.crossref.org/works/{}"
CROSSREF_TITLE_SEARCH_URL = "https://api.crossref.org/works"

# Crossref polite pool headers (recommended by Crossref docs)
_CROSSREF_HEADERS = {
    "User-Agent": "DoiHarvest/1.0 (mailto:doiharvest@example.com)",
}

# Thread-safe cache lock (for batch prefetch)
_cache_lock = threading.Lock()


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    with _cache_lock:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)


def _query_crossref(doi: str) -> dict | None:
    """Query Crossref for a single DOI. Returns parsed metadata dict or None."""
    url = CROSSREF_URL.format(doi)
    try:
        # trust_env=False: bypass leftover proxy env vars (http_proxy=127.0.0.1:7892)
        # which otherwise make requests fail with ProxyError / connection refused.
        resp = requests.get(url, timeout=CROSSREF_TIMEOUT, trust_env=False)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {})
    except requests.RequestException as e:
        logger.warning(f"Crossref query failed for {doi}: {e}")
        return None


def _extract_first_author(msg: dict) -> str:
    """Extract surname of the first author."""
    authors = msg.get("author", [])
    if not authors:
        return "Unknown"
    first = authors[0]
    family = first.get("family", "")
    if family:
        return family
    given = first.get("given", "")
    return given or "Unknown"


def _extract_journal_abbrev(msg: dict) -> str:
    """Extract a short journal identifier."""
    # Prefer short container titles
    container = msg.get("container-title", [])
    short = msg.get("short-container-title", [])
    title = (short[0] if short else "") or (container[0] if container else "")
    if not title:
        return "UnknownJournal"

    # Abbreviate common long names
    abbrev_map = {
        "proceedings of the national academy of sciences": "PNAS",
        "nature communications": "NatCommun",
        "scientific reports": "SciRep",
        "plos one": "PLoSONE",
        "nucleic acids research": "NucleicAcidsRes",
        "journal of biological chemistry": "JBiolChem",
        "the journal of ": "J",
        "journal of ": "J",
    }
    lower = title.lower().strip(".")
    for full, abbrev in abbrev_map.items():
        if lower.startswith(full):
            return abbrev

    # Take first 3 significant words, max 20 chars
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", title) if w]
    return "".join(words[:3])[:20] or "Journal"


def _extract_year(msg: dict) -> str:
    """Extract publication year."""
    # Prefer issued date, fall back to created
    issued = msg.get("issued", {})
    date_parts = issued.get("date-parts", [[None]])[0]
    if date_parts and date_parts[0]:
        return str(date_parts[0])

    created = msg.get("created", {})
    date_parts = created.get("date-parts", [[None]])[0]
    if date_parts and date_parts[0]:
        return str(date_parts[0])

    return "Unknown"


def _extract_title(msg: dict) -> str:
    """Extract paper title."""
    titles = msg.get("title", [])
    return titles[0] if titles else "Untitled"


def _sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename."""
    # Replace path separators and special chars
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    # Collapse multiple spaces/underscores
    safe = re.sub(r"[_\s]+", "_", safe)
    # Trim leading/trailing dots and underscores
    safe = safe.strip("._ ")
    # Max 180 chars to leave room for extension
    if len(safe) > 180:
        safe = safe[:180].rsplit("_", 1)[0]
    return safe


def _make_short_title(title: str, max_words: int = 6) -> str:
    """Truncate title to first N meaningful words."""
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", title) if w]
    return "_".join(words[:max_words])


def get_metadata(doi: str, cache: dict | None = None) -> dict | None:
    """
    Get metadata for a DOI from cache or Crossref.

    When ``cache`` is provided (batch mode), it is used as a shared read/write
    cache and the caller is responsible for saving it to disk later.  In
    standalone mode (cache=None), the function loads / saves per invocation.

    Returns dict with keys: doi, title, first_author, year, journal, filename.
    Returns None if DOI cannot be resolved.
    """
    doi_clean = doi.strip().lower()

    own_cache = cache is None
    if own_cache:
        cache = _load_cache()

    if doi_clean in cache:
        return cache[doi_clean]

    msg = _query_crossref(doi)
    if msg is None:
        cache[doi_clean] = None
        if own_cache:
            _save_cache(cache)
        return None

    title = _extract_title(msg)
    first_author = _extract_first_author(msg)
    year = _extract_year(msg)
    journal = _extract_journal_abbrev(msg)
    short_title = _make_short_title(title)

    safe_author = _sanitize_filename(first_author)
    safe_journal = _sanitize_filename(journal)
    safe_short = _sanitize_filename(short_title)

    filename = f"{safe_author}_{year}_{safe_journal}_{safe_short}.pdf"

    meta = {
        "doi": doi_clean,
        "title": title,
        "first_author": first_author,
        "year": year,
        "journal": journal,
        "filename": filename,
    }

    cache[doi_clean] = meta
    if own_cache:
        _save_cache(cache)
    return meta


def get_filename(doi: str) -> str:
    """Get the target filename for a DOI. Falls back to DOI-based name."""
    meta = get_metadata(doi)
    if meta:
        return meta["filename"]
    safe_doi = _sanitize_filename(doi.replace("/", "_"))
    return f"{safe_doi}.pdf"


def batch_prefetch(
    dois: list[str],
    progress_callback=None,
    max_workers: int = BATCH_WORKERS,
) -> dict[str, dict | None]:
    """
    Pre-fetch metadata for a list of DOIs using concurrent requests.

    - Uses ThreadPoolExecutor for speed (6 workers, ~30 req/s — polite)
    - Reports progress via ``progress_callback("phase1_prefetching", ...)``
    - Batch-saves the cache file every 50 completed DOIs (instead of per-DOI)
    - Falls back to sequential processing when the thread pool fails

    Returns dict mapping doi -> metadata dict (or None for unresolvable).
    """
    cache = _load_cache()
    total = len(dois)
    result = {}
    completed = 0

    def _fetch_one(doi: str) -> tuple[str, dict | None]:
        """Fetch a single DOI with per-thread polite delay."""
        doi_clean = doi.strip().lower()
        meta = get_metadata(doi_clean, cache=cache)
        time.sleep(CROSSREF_DELAY)
        return doi_clean, meta

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_one, doi): i for i, doi in enumerate(dois)
            }
            for future in as_completed(futures):
                doi_clean, meta = future.result()
                result[doi_clean] = meta
                completed += 1

                # Progress callback every 10 or final
                if progress_callback and (completed % 10 == 0 or completed == total):
                    progress_callback("phase1_prefetching", {
                        "message": f"Pre-fetching metadata: {completed}/{total} papers",
                        "current": completed,
                        "total": total,
                    })

                # Batch-save cache every 50 DOIs (and on final) to avoid per-DOI disk writes
                if completed % 50 == 0 or completed == total:
                    _save_cache(cache)

    except Exception as e:
        logger.warning(f"Concurrent prefetch failed ({e}), falling back to sequential")
        # Fallback: sequential processing
        for i, doi in enumerate(dois):
            doi_clean = doi.strip().lower()
            if doi_clean not in cache:
                result[doi_clean] = get_metadata(doi_clean, cache=cache)
                time.sleep(CROSSREF_DELAY * 2)  # gentler delay in fallback
            else:
                result[doi_clean] = cache[doi_clean]
            completed = i + 1

            if progress_callback and (completed % 5 == 0 or completed == total):
                progress_callback("phase1_prefetching", {
                    "message": f"Pre-fetching metadata: {completed}/{total} papers (sequential)",
                    "current": completed,
                    "total": total,
                })

            if completed % 30 == 0 or completed == total:
                _save_cache(cache)

    return result


# ── Title → DOI lookup ──────────────────────────────

def _search_crossref_by_title(title: str, rows: int = 5) -> list[dict]:
    """
    Search Crossref by paper title. Returns a list of candidate works,
    each with doi, title, author, year, container-title.

    Uses Crossref's ``query.bibliographic`` field which searches across
    title, author, DOI and other bibliographic metadata for better precision.
    """
    import urllib.parse
    query = urllib.parse.quote(title)
    url = (
        f"{CROSSREF_TITLE_SEARCH_URL}"
        f"?query.bibliographic={query}"
        f"&rows={rows}"
    )
    try:
        resp = requests.get(url, headers=_CROSSREF_HEADERS, timeout=CROSSREF_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("message", {}).get("items", [])
        candidates = []
        for item in items:
            doi = item.get("DOI", "").strip()
            if not doi:
                continue
            msg = item
            candidates.append({
                "doi": doi.lower(),
                "title": _extract_title(msg),
                "first_author": _extract_first_author(msg),
                "year": _extract_year(msg),
                "journal": _extract_journal_abbrev(msg),
            })
        return candidates
    except requests.RequestException as e:
        logger.warning(f"Crossref title search failed for '{title[:60]}': {e}")
        return []


def _normalize_title(t: str) -> str:
    """Normalize title for comparison: lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r'[^\w\s]', '', t.lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    """
    Compute a simple similarity score between two titles (0-1).
    Uses normalized word-overlap Jaccard coefficient.
    """
    na = _normalize_title(a)
    nb = _normalize_title(b)
    if not na or not nb:
        return 0.0
    wa = set(na.split())
    wb = set(nb.split())
    if not wa or not wb:
        return 0.0
    intersection = wa & wb
    union = wa | wb
    return len(intersection) / len(union)


def find_doi_by_title(
    title: str,
    year_hint: str | None = None,
    candidates: int = 5,
) -> list[dict]:
    """
    Search Crossref for a DOI matching the given paper title.

    Returns a list of candidates sorted by confidence (best first).
    Each candidate is a dict with:
      - doi, title, first_author, year, journal
      - ``confidence``: "high" (>0.8), "medium" (0.5-0.8), "low" (0.3-0.5)
      - ``score``: raw similarity float 0-1

    Returns empty list if no candidates found.
    """
    if not title or not title.strip():
        return []

    raw_candidates = _search_crossref_by_title(title, rows=candidates)
    if not raw_candidates:
        return []

    scored = []
    for c in raw_candidates:
        score = _title_similarity(title, c["title"])
        # Year bonus: if year matches, boost score slightly
        if year_hint and c["year"] != "Unknown" and str(year_hint).strip() == c["year"]:
            score = min(1.0, score + 0.1)

        if score >= 0.2:  # minimum threshold to be considered a candidate
            if score >= 0.8:
                confidence = "high"
            elif score >= 0.5:
                confidence = "medium"
            else:
                confidence = "low"
            c["score"] = round(score, 3)
            c["confidence"] = confidence
            scored.append(c)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def batch_find_dois(
    papers: list[dict],
    progress_callback=None,
    max_candidates: int = 5,
) -> dict:
    """
    Given a list of papers (each with at least a ``title`` key),
    attempt to find DOIs for those missing one.

    Papers must have:
      - ``title`` (str): paper title
      - ``row_index`` (int, optional): original row number for reporting

    Returns dict with:
      - ``results``: list of {row_index, title, candidates: [...], resolved_doi}
      - ``stats``: {total_without_doi, matched_high, matched_medium, matched_low, no_match}
    """
    missing = [
        p for p in papers
        if not p.get("doi", "").strip() and p.get("title", "").strip()
    ]
    if not missing:
        return {"results": [], "stats": {
            "total_without_doi": 0,
            "matched_high": 0, "matched_medium": 0, "matched_low": 0,
            "no_match": 0,
        }}

    total = len(missing)
    results: list[dict] = []
    stats = {
        "total_without_doi": total,
        "matched_high": 0, "matched_medium": 0, "matched_low": 0,
        "no_match": 0,
    }

    for i, paper in enumerate(missing):
        title = paper.get("title", "").strip()
        year_hint = str(paper.get("year", "")).strip() or None
        row_index = paper.get("row_index", i)

        # Notify frontend: searching this specific title
        if progress_callback:
            progress_callback("doi_completion", {
                "message": f"[{i + 1}/{total}] Searching: {title[:80]}{'...' if len(title) > 80 else ''}",
                "current": i + 1,
                "total": total,
                "phase": "searching",
            })

        candidates = find_doi_by_title(title, year_hint=year_hint, candidates=max_candidates)

        entry = {
            "row_index": row_index,
            "title": title,
            "year_hint": year_hint,
            "candidates": candidates,
            "resolved_doi": "",
        }
        if candidates:
            best = candidates[0]
            conf = best["confidence"]
            if conf == "high":
                stats["matched_high"] += 1
                entry["resolved_doi"] = best["doi"]  # auto-accept high confidence
            elif conf == "medium":
                stats["matched_medium"] += 1
            else:
                stats["matched_low"] += 1
            # Notify frontend: result found
            if progress_callback:
                progress_callback("doi_completion", {
                    "message": f"[{i + 1}/{total}] {conf.upper()} match: {best['doi']} — {best.get('title', '')[:60]}",
                    "current": i + 1,
                    "total": total,
                    "phase": "found",
                    "confidence": conf,
                    "doi": best["doi"],
                    "stats": stats,
                })
        else:
            stats["no_match"] += 1
            if progress_callback:
                progress_callback("doi_completion", {
                    "message": f"[{i + 1}/{total}] NO MATCH — could not find DOI for this title",
                    "current": i + 1,
                    "total": total,
                    "phase": "not_found",
                    "stats": stats,
                })

        results.append(entry)

        time.sleep(CROSSREF_DELAY)

    return {"results": results, "stats": stats}
