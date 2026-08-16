"""
Phase 2: Library WebVPN downloader for papers Sci-Hub couldn't get.

PKU Library WebVPN only (2026-08-15 refactor, modeled on scansci-pdf):

pku  : 北京大学图书馆 WebVPN — https://wpn.pku.edu.cn
       wrdvpn-style path proxy (hostname encrypted with AES-CFB):
         https://wpn.pku.edu.cn/{scheme}/{hex(iv)}{hex(AES-CFB(hostname))}{path}?{query}
       Default key/iv: "wrdvpnisthebest!" (same key used by scansci-pdf's
       school database for schools without custom crypto keys).
       Downloads run requests-first (fast), with a Playwright browser retry
       for pages that need JavaScript.

Config file: webvpn_config.json
    {
      "provider": "pku",
      "pku": {"cookies": {...}, "cookie_objects": [...]}
    }
Legacy sections ("bjmu" or flat {"base_url", "cookies"}) are ignored/migrated.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .metadata import get_metadata

# The machine may carry residual HTTP(S)_PROXY env vars (e.g. from a
# stopped Clash-like app). They break direct / campus-tunnel requests
# with ProxyError and also leak into curl_cffi (libcurl reads the env).
# Neutralize them once, for this process. (requests trusts them only
# when trust_env is left on; curl_cffi cannot be told to ignore them.)
for _proxy_k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                 "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_k, None)

# curl_cffi — Chrome TLS fingerprint. Several publishers (Ovid,
# Springer-link, MDPI fallback, ...) answer 403 to python-requests'
# TLS fingerprint but serve the real page to a Chrome fingerprint.
_CURL_AVAILABLE = False
try:
    from curl_cffi import requests as crequests  # noqa: F401
    _CURL_AVAILABLE = True
except ImportError:
    pass

# Playwright is optional — only needed for auto-login (async, for FastAPI)
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

# Playwright sync API — used in Phase 2 downloads (runs in a thread)
_HAS_SYNC_PLAYWRIGHT = False
try:
    from playwright.sync_api import sync_playwright
    _HAS_SYNC_PLAYWRIGHT = True
except ImportError:
    pass

# AES-CFB for wrdvpn-style URL encryption (PKU)
try:
    from Crypto.Cipher import AES as _AES
    _HAS_AES = True
except ImportError:
    _HAS_AES = False

# undetected-chromedriver — bypasses Cloudflare JS challenges that Playwright
# (even with channel='chrome') cannot. Used as a mid-tier fallback between
# requests/curl_cffi (fast) and Playwright (slow, VPN-only).
# Passes Cloudflare → extracts cf_clearance cookies → downloads PDF via curl_cffi.
_HAS_UC = False
try:
    import undetected_chromedriver as _uc
    from selenium.webdriver.common.by import By as _By
    _HAS_UC = True
except ImportError:
    pass

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
WEBVPN_CONFIG_FILE = BASE_DIR / "webvpn_config.json"

BJMU_WEBVPN_BASE = "webvpn.bjmu.edu.cn"

# ── Provider registry ───────────────────────────────

PROVIDERS: dict[str, dict] = {
    "pku": {
        "label": "PKU Library WebVPN (北大图书馆)",
        "style": "wrdvpn",
        "host": "wpn.pku.edu.cn",
        "portal": "https://wpn.pku.edu.cn",
        "crypto_key": "wrdvpnisthebest!",
        "crypto_iv": "wrdvpnisthebest!",
        # URL fragments that mean "still on the login page"
        "login_url_hints": ["iaaa.pku.edu.cn", "/login", "/cas", "portal211"],
    },
    "pku_client": {
        "label": "PKU VPN Client (校内VPN直连)",
        "style": "direct",
        "host": "",
        "portal": "",
        "crypto_key": "",
        "crypto_iv": "",
        # When the campus VPN tunnel is down, requests get bounced to sign-in pages
        "login_url_hints": [
            "iaaa.pku.edu.cn", "/login", "/cas", "sso", "signin",
            "account.", "auth.", "logon",
        ],
    },
}

DEFAULT_PROVIDER = "pku"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ── Timeout tuning (generous — WebVPN relays can be slow, PDFs can be large) ──
# requests tuples: (connect_timeout, read_timeout) in seconds.
HTTP_TIMEOUT = (30, 180)       # WebVPN GETs: slow relay + big PDF downloads
RESOLVE_TIMEOUT = (20, 60)     # DOI resolution (doi.org redirect chain)
PROBE_TIMEOUT = (15, 60)       # session probes / quick checks
NAV_TIMEOUT_MS = 120_000       # Playwright page.goto (was 30s — too tight)


# ── wrdvpn URL encryption (PKU) ─────────────────────

def _wrdvpn_encrypt_host(hostname: str, key: str, iv: str) -> str:
    """Encrypt a hostname with AES-CFB (segment 128) → hex(iv) + hex(ciphertext)."""
    if not _HAS_AES:
        raise RuntimeError(
            "pycryptodome required for PKU WebVPN. Install: pip install pycryptodome"
        )
    key_b = key.encode("utf-8")
    iv_b = iv.encode("utf-8")
    cipher = _AES.new(key_b, _AES.MODE_CFB, iv_b, segment_size=128)
    encrypted = cipher.encrypt(hostname.encode("utf-8"))
    return binascii.hexlify(iv_b).decode() + binascii.hexlify(encrypted).decode()


def to_wrdvpn_url(
    url: str,
    host: str = "wpn.pku.edu.cn",
    crypto_key: str = "wrdvpnisthebest!",
    crypto_iv: str = "wrdvpnisthebest!",
) -> str:
    """Convert a regular URL to PKU wrdvpn-style WebVPN URL.

    https://www.nature.com/articles/xxx
      → https://wpn.pku.edu.cn/https/{hex(iv)}{hex(ct)}/articles/xxx
    Only the hostname is encrypted; path & query are preserved.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if not hostname:
        return url

    enc = _wrdvpn_encrypt_host(hostname, crypto_key, crypto_iv)

    scheme_part = parsed.scheme.lower() or "https"
    if parsed.port:
        scheme_part = f"{scheme_part}-{parsed.port}"

    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"https://{host}/{scheme_part}/{enc}{path}{query}"


# ── Sangfor URL conversion (BJMU) ────────────────────

def to_webvpn_url(url: str, base: str = BJMU_WEBVPN_BASE) -> list[str]:
    """Convert a regular URL to BJMU WebVPN format.

    Returns a list of candidate URLs to try (in priority order).
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if not hostname:
        return []

    host_dashed = hostname.replace(".", "-")
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""

    candidates = []
    candidates.append(f"https://{host_dashed}-443.{base}{path}{query}")
    if not hostname.endswith(".443"):
        candidates.append(f"https://{host_dashed}-s.{base}{path}{query}")
    return candidates


def to_webvpn_doi_url(doi: str, base: str = BJMU_WEBVPN_BASE) -> list[str]:
    """Generate WebVPN URLs for a DOI."""
    return to_webvpn_url(f"https://doi.org/{doi}", base)


# ── Unified provider helpers ─────────────────────────

def convert_url(url: str, provider: str) -> str | None:
    """Convert a regular URL to the active provider's WebVPN format.

    Returns the VPN URL as a string, or None if not convertible.
    (BJMU's exact suffix resolution happens via /quick API in the
    Playwright flow; here we return the first candidate.)
    """
    prov = PROVIDERS.get(provider)
    if not prov:
        return None
    if prov["style"] == "direct":
        return url  # campus VPN client — IP-based auth, connect directly
    if prov["style"] == "wrdvpn":
        return to_wrdvpn_url(url, prov["host"], prov["crypto_key"], prov["crypto_iv"])
    cands = to_webvpn_url(url, prov["host"])
    return cands[0] if cands else None


def _is_login_page(url: str, html: str, provider: str) -> bool:
    """Detect whether we've been bounced to a login page."""
    prov = PROVIDERS.get(provider, PROVIDERS["pku"])
    lower = url.lower()
    vpn_host = (prov.get("host") or "").lower()

    # On the VPN host itself, but not on a login subpath → authenticated
    if vpn_host and vpn_host in lower:
        return any(hint in lower for hint in prov["login_url_hints"])

    # Still on a CAS / IdP host (also covers direct-mode bounces to sign-in)
    if any(hint in lower for hint in ("iaaa.pku.edu.cn", "authserver", "/users/sign_in", "/login", "/cas", "sso", "signin")):
        return True

    # HTML sniffing
    if html:
        for marker in ("统一身份认证", "请登录", "用户登录", "Login - PKU", "Sign in"):
            if marker in html:
                return True
    return False


# ── Config management ────────────────────────────────

def load_webvpn_config() -> dict:
    """Load WebVPN configuration from disk (with legacy migration)."""
    cfg = {}
    if WEBVPN_CONFIG_FILE.exists():
        with open(WEBVPN_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    # Legacy formats: drop old flat keys / stale "bjmu" section is left
    # untouched on disk but never used (only "pku" is a registered provider).
    cfg.pop("base_url", None)
    cfg.pop("cookies", None)

    cfg.setdefault("provider", DEFAULT_PROVIDER)
    if cfg["provider"] not in PROVIDERS:
        cfg["provider"] = DEFAULT_PROVIDER
    for pid in PROVIDERS:
        cfg.setdefault(pid, {})
    return cfg


def save_webvpn_config(cfg: dict) -> None:
    """Save WebVPN configuration."""
    with open(WEBVPN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_active_provider() -> str:
    return load_webvpn_config().get("provider", DEFAULT_PROVIDER)


def set_active_provider(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    cfg = load_webvpn_config()
    cfg["provider"] = provider
    save_webvpn_config(cfg)
    return cfg


def get_provider_cookies(provider: str) -> dict:
    cfg = load_webvpn_config()
    return cfg.get(provider, {}).get("cookies", {})


def configure_webvpn(
    cookies: dict[str, str] | None = None,
    base_url: str = "",
    provider: str = "",
) -> dict:
    """Configure WebVPN settings for a provider. Returns current config."""
    cfg = load_webvpn_config()

    if provider:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")
        cfg["provider"] = provider

    pid = cfg["provider"]
    sect = cfg.setdefault(pid, {})

    if cookies is not None:
        sect["cookies"] = cookies
    if base_url:
        sect["base_url"] = base_url

    save_webvpn_config(cfg)

    # Backward-compatible flat view for legacy callers
    view = dict(cfg)
    view["base_url"] = sect.get("base_url", PROVIDERS[pid]["host"])
    view["cookies"] = sect.get("cookies", {})
    return view


# ── Auto-login via Playwright ────────────────────────

async def login_via_browser(timeout: int = 600, provider: str = "") -> dict:
    """Launch Chrome, open the PKU WebVPN portal, wait for the user to
    **manually close** the browser, then capture cookies.

    The user sees a normal Chrome window, logs in with PKU unified
    authentication (iaaa.pku.edu.cn) at their own pace. A background task
    polls cookies every 3 s so the latest snapshot is always available.
    When the user closes the browser window, the last snapshot is saved
    to webvpn_config.json.

    Args:
        timeout: Max seconds to wait for the user to close the browser
                 (default 10 min).
        provider: must be "pku" (defaults to active provider).

    Returns:
        dict with keys: success, cookies_count, message, provider
    """
    if not provider:
        provider = get_active_provider()
    prov = PROVIDERS.get(provider)
    if not prov:
        return {"success": False, "cookies_count": 0, "message": f"Unknown provider: {provider}", "provider": provider}

    if prov["style"] == "direct":
        # Campus VPN client — authentication happens in the VPN client itself
        return {
            "success": True,
            "cookies_count": 0,
            "provider": provider,
            "message": (
                "校内 VPN 客户端直连模式无需浏览器登录。"
                "请确保 VPN 客户端已连接并完成登录，然后直接执行 Step 2。"
            ),
        }

    if not _HAS_PLAYWRIGHT:
        return {
            "success": False,
            "cookies_count": 0,
            "provider": provider,
            "message": "Playwright is not installed. Run: pip install playwright && playwright install chromium",
        }

    portal = prov["portal"]
    vpn_host = prov["host"]

    try:
        async with async_playwright() as p:
            logger.info(f"Launching Chromium for {prov['label']} login...")
            browser = await p.chromium.launch(
                headless=False,
                args=["--start-maximized", "--no-proxy-server"],
            )
            context = await browser.new_context(no_viewport=True, user_agent=_HEADERS["User-Agent"])
            page = await context.new_page()

            # Navigate to WebVPN portal — will redirect to the login page
            await page.goto(portal, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            # Inject a persistent banner so the user knows what to do
            try:
                await page.evaluate("""() => {
                    const banner = document.createElement('div');
                    banner.id = '__doi_harvest_banner';
                    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                        + 'background:#1a73e8;color:white;padding:10px 16px;font-size:14px;'
                        + 'font-family:sans-serif;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);'
                        + 'line-height:1.5';
                    banner.innerHTML = 'DoiHarvest 登录模式：请在此窗口完成北大登录。'
                        + '登录完成后，<b>关闭此浏览器窗口</b>即可自动保存凭据。';
                    document.body && document.body.prepend(banner);
                }""")
            except Exception:
                pass  # page may not support evaluate (e.g. during redirect)

            logger.info(
                f"Browser opened for {prov['label']}. "
                f"Please complete login and CLOSE the browser when done."
            )

            # ── Wait for the user to manually close the browser ──
            # Poll browser.is_connected() instead of relying on the
            # "disconnected" event callback — Playwright's async API may
            # fire the callback from a different thread, causing
            # asyncio.Event.set() to not wake up the waiter on the main
            # event loop.  Polling is_connected() is reliable and also
            # lets us grab fresh cookies in the same loop.
            latest = {"cookies": []}
            deadline = time.monotonic() + timeout
            while browser.is_connected():
                try:
                    latest["cookies"] = await context.cookies()
                except Exception:
                    pass  # browser/context may be gone during shutdown
                if time.monotonic() >= deadline:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    return {
                        "success": False,
                        "cookies_count": 0,
                        "provider": provider,
                        "message": (
                            f"Timed out after {timeout}s waiting for you to close the browser. "
                            "Please try again and close the browser window when login is complete."
                        ),
                    }
                await asyncio.sleep(3)

            # Use the last cookie snapshot captured before close
            cookies = latest["cookies"]
            cookie_dict = {}
            vpn_cookies = []
            for c in cookies:
                name = c.get("name", "")
                value = c.get("value", "")
                if name and value:
                    cookie_dict[name] = value
                if vpn_host in (c.get("domain") or ""):
                    vpn_cookies.append(c)

            if not vpn_cookies:
                return {
                    "success": False,
                    "cookies_count": 0,
                    "provider": provider,
                    "message": (
                        "No portal cookies were captured. You may have closed the browser "
                        "before completing login. Please try again — complete the login first, "
                        "then close the browser window."
                    ),
                }

            # Save full cookie objects (for Playwright) + flat dict (for requests)
            cfg = load_webvpn_config()
            sect = cfg.setdefault(provider, {})
            sect["cookies"] = cookie_dict
            sect["cookie_objects"] = cookies
            sect["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_webvpn_config(cfg)

            # ── Verify the captured session actually works before reporting success ──
            try:
                probe = requests.Session()
                probe.trust_env = False  # campus tunnel / VPN — no HTTP proxy
                probe.headers.update(_HEADERS)
                for n, v in cookie_dict.items():
                    probe.cookies.set(n, v, domain=prov["host"])
                probe_url = convert_url("https://www.nature.com", provider)
                pr = probe.get(probe_url, timeout=PROBE_TIMEOUT, allow_redirects=True)
                if _is_login_page(pr.url, "", provider):
                    return {
                        "success": False,
                        "cookies_count": 0,
                        "provider": provider,
                        "message": (
                            "Cookies captured but the ticket was rejected by WebVPN "
                            "(redirected to the login page). This usually means the "
                            "session expired or the terminal fingerprint changed. "
                            "Please re-run auto-login."
                        ),
                    }
            except Exception as e:
                logger.warning(f"Post-login probe failed (will still save cookies): {e}")

            logger.info(f"{prov['label']} login successful — captured {len(vpn_cookies)} portal cookies")
            return {
                "success": True,
                "cookies_count": len(vpn_cookies),
                "provider": provider,
                "message": f"Login successful. Captured {len(vpn_cookies)} cookies for {prov['label']}.",
            }

    except Exception as e:
        logger.error(f"WebVPN auto-login failed: {e}")
        return {
            "success": False,
            "cookies_count": 0,
            "provider": provider,
            "message": f"Login failed: {e}",
        }


# ── Session helpers ──────────────────────────────────

def _create_webvpn_session(provider: str) -> requests.Session | None:
    """Create a requests.Session for the provider.

    wrdvpn (pku):      needs the WebVPN cookies.
    direct (pku_client): campus VPN client — IP-based auth, no cookies.
    """
    cfg = load_webvpn_config()
    prov = PROVIDERS[provider]
    sect = cfg.get(provider, {})

    # Never route library access through the system HTTP proxy — the
    # campus VPN tunnel is a system-level route and doesn't need one.
    # (Residual proxy env vars like http_proxy=127.0.0.1:7892 break
    # requests with ProxyError when the proxy process isn't running.)
    def _new_requests_session() -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers.update(_HEADERS)
        return session

    def _new_direct_session():
        """Session for direct (campus VPN client) mode.

        Prefers curl_cffi with a Chrome TLS fingerprint — several
        publishers (Ovid, Springer-link, ...) reject python-requests'
        fingerprint with 403 but serve fine to a real Chrome one.
        Falls back to requests (trust_env off) when curl_cffi is absent.
        """
        if _CURL_AVAILABLE:
            try:
                s = crequests.Session(impersonate="chrome")
                s.headers.update(_HEADERS)
                return s
            except Exception as e:
                logger.warning(f"curl_cffi session failed, falling back to requests: {e}")
        return _new_requests_session()

    if prov["style"] == "direct":
        return _new_direct_session()

    cookies = sect.get("cookies", {})
    if not cookies:
        logger.warning(f"No {provider} WebVPN cookies configured")
        return None

    session = _new_requests_session()
    domain = sect.get("base_url", prov["host"])
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=domain)
    return session


def check_session(provider: str = "") -> str:
    """Probe the session: 'none' | 'valid' | 'expired' | 'unreachable'.

    direct mode (pku_client): probe a publisher site straight over the
    campus VPN tunnel — if we reach ScienceDirect without being bounced
    to a sign-in page, the tunnel + subscription IP are working.
    """
    if not provider:
        provider = get_active_provider()
    prov = PROVIDERS[provider]
    cfg = load_webvpn_config()

    if prov["style"] == "direct":
        session = requests.Session()
        session.trust_env = False  # campus tunnel — no HTTP proxy
        session.headers.update(_HEADERS)
        try:
            resp = session.get(
                "https://www.sciencedirect.com/",
                timeout=PROBE_TIMEOUT,
                allow_redirects=True,
            )
            final = resp.url.lower()
            if _is_login_page(final, "", provider):
                return "expired"   # bounced to sign-in → tunnel down / not authorized
            if resp.status_code == 200:
                return "valid"
            # 403/4xx is often just anti-bot UA blocking — if the body is
            # still the real publisher page, the tunnel + IP auth work.
            body = (resp.text or "")[:2000].lower()
            if any(m in body for m in ("sciencedirect", "elsevier", "nature.com", "springer")):
                return "valid"
            return "unreachable"
        except Exception:
            return "unreachable"

    if not cfg.get(provider, {}).get("cookies"):
        return "none"

    session = requests.Session()
    session.trust_env = False
    session.headers.update(_HEADERS)
    for name, value in cfg[provider].get("cookies", {}).items():
        session.cookies.set(name, value, domain=prov["host"])

    test_url = convert_url("https://www.nature.com", provider)
    try:
        resp = session.get(test_url, timeout=PROBE_TIMEOUT, allow_redirects=True)
        if _is_login_page(resp.url, "", provider):
            return "expired"
        return "valid" if resp.status_code == 200 else "unreachable"
    except Exception:
        return "unreachable"


# ── No-proxy helper ──────────────────────────────────
# The campus VPN tunnel (and library WebVPN) is a system-level route —
# it must NOT go through the system HTTP proxy. Residual proxy env vars
# (e.g. http_proxy=127.0.0.1:7892 from a stopped Clash-like app) make
# requests fail with ProxyError unless trust_env is disabled.

def _no_proxy_get(url: str, timeout, headers: dict | None = None, allow_redirects: bool = True) -> requests.Response:
    """GET with the system HTTP proxy disabled (direct / tunnel route)."""
    session = requests.Session()
    session.trust_env = False
    if headers:
        session.headers.update(headers)
    return session.get(url, timeout=timeout, allow_redirects=allow_redirects)


# ── PDF extraction from HTML ─────────────────────────

def _find_pdf_on_page(html: str, page_url: str) -> str | None:
    """Extract PDF URL from a publisher page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    pdf_url = None

    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").lower()
        content = meta.get("content", "")
        if name == "citation_pdf_url" and content:
            pdf_url = content
            break

    if not pdf_url:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text().lower()
            if ".pdf" in href.lower() or "pdf" in text:
                pdf_url = href
                break

    if not pdf_url:
        m = re.search(r"""(https?://[^\s"'<>]+\.pdf)""", html)
        if m:
            pdf_url = m.group(1)

    if not pdf_url:
        return None

    if pdf_url.startswith("//"):
        pdf_url = "https:" + pdf_url
    elif pdf_url.startswith("/"):
        parsed = urlparse(page_url)
        pdf_url = f"{parsed.scheme}://{parsed.netloc}{pdf_url}"

    return pdf_url


def _publisher_pdf_hint(pub_url: str, doi: str) -> list[str]:
    """Known direct-PDF URL patterns per publisher (from scansci-pdf)."""
    hints: list[str] = []
    parsed = urlparse(pub_url)
    host = parsed.netloc.lower()
    path = parsed.path

    if "sciencedirect.com" in host:
        m = re.search(r"/pii/([^/?#]+)", path)
        if m:
            hints.append(f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}/pdfft")
    elif "onlinelibrary.wiley.com" in host and doi:
        hints.append(f"https://onlinelibrary.wiley.com/doi/pdf-direct/{doi}")
        hints.append(f"https://onlinelibrary.wiley.com/doi/pdf/{doi}")
    elif "ovid.com" in host and "/fulltext/" in path:
        # Ovid journals (Wolters Kluwer LWW, Medknow, ...):
        # doi.org often lands on /jnls/{journal}/fulltext/{doi}~{slug};
        # the PDF endpoint mirrors that path with /pdf/.
        hints.append(pub_url.replace("/fulltext/", "/pdf/"))
    elif "link.springer.com" in host and doi:
        hints.append(f"https://link.springer.com/content/pdf/{doi}.pdf")
    elif "nature.com" in host:
        hints.append(pub_url.rstrip("/") + ".pdf")
    elif "frontiersin.org" in host and doi:
        hints.append(f"https://www.frontiersin.org/articles/{doi}/pdf")
    elif "tandfonline.com" in host and "/doi/full/" in path:
        pdf_path = path.replace("/doi/full/", "/doi/pdf/")
        hints.append(f"{parsed.scheme}://{parsed.netloc}{pdf_path}")
    return [h for h in hints if h and isinstance(h, str)]


# ── PKU: requests-based download ─────────────────────

# ── Elsevier Article Retrieval API (bypasses ScienceDirect/Cloudflare entirely) ──
# Strategy (from scansci-pdf's elsevier_api.py, battle-tested on 32-paper batch):
#   1. GET /content/article/doi/{doi}?view=FULL  → XML with attachment metadata
#   2. Parse XML for MAIN PDF attachment-eid (1-s2.0-*-main.pdf)
#   3. GET /content/object/eid/{eid}             → official publisher PDF
#   4. Fallback: GET /content/article/doi/{doi} + Accept: application/pdf
# Requires an API key whose requestor config includes Article Retrieval
# (dev.elsevier.com → edit key → tick Article Retrieval API). A key without
# it returns 403 AUTHENTICATION_ERROR even for OA articles.

ELSEVIER_API_BASE = "https://api.elsevier.com/content"
_ELSEVIER_PREFIXES = ("10.1016/",)  # Elsevier / ScienceDirect / Cell Press DOIs


def _elsevier_api_enabled() -> bool:
    cfg = load_webvpn_config()
    return bool(cfg.get("elsevier_api_key"))


def _elsevier_api_headers(accept: str) -> dict[str, str]:
    cfg = load_webvpn_config()
    h = {
        "X-ELS-APIKey": cfg.get("elsevier_api_key", ""),
        "Accept": accept,
    }
    inst = cfg.get("elsevier_inst_token") or ""
    if inst:
        h["X-ELS-InstToken"] = inst
    return h


def _elsevier_extract_pdf_eids(xml_text: str) -> list[str]:
    """Extract main-PDF attachment EIDs from Elsevier FULL XML (best first).

    Simplified from scansci-pdf: score .pdf EIDs, prefer *-main.pdf,
    reject supplements (mmc/graphical/appendix).
    """
    import xml.etree.ElementTree as ET

    def local(tag) -> str:
        return str(tag).rsplit("}", 1)[-1].split(":", 1)[-1].lower()

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    found: list[str] = []
    seen: set[str] = set()
    for el in root.iter():
        tag = local(el.tag)
        if tag not in ("attachment-eid", "object-eid", "eid"):
            continue
        text = " ".join(" ".join(el.itertext()).split()).strip()
        if not text:
            continue
        cand = text[4:] if text.lower().startswith("eid:") else text
        if not cand.startswith("1-s2.0-"):
            continue
        # article EID → main.pdf; keep explicit .pdf EIDs as-is
        if not cand.lower().endswith(".pdf"):
            cand = f"{cand}-main.pdf"
        if cand not in seen:
            seen.add(cand)
            found.append(cand)

    def score(eid: str) -> int:
        s = 0
        low = eid.lower()
        if low.endswith("main.pdf") or "-main" in low:
            s += 100
        if low.endswith(".pdf"):
            s += 20
        if any(m in low for m in ("mmc", "graphical", "appendix", "suppl")):
            s -= 200
        return s

    return sorted(found, key=score, reverse=True)


def _elsevier_api_download(doi: str, output_dir: Path) -> dict | None:
    """Try downloading an Elsevier PDF via the Article Retrieval API.

    Returns a result dict on success, None if the API route is unavailable
    (no key, 403 config error, 404, non-PDF response, ...). Never raises.
    """
    if not _elsevier_api_enabled():
        return None

    result = {
        "doi": doi, "title": "", "status": "download_failed",
        "route": "elsevier_api", "filepath": "", "filename": "", "message": "",
    }
    s = requests.Session()
    s.trust_env = False

    def _save(data: bytes) -> dict:
        doi_slug = doi.replace("/", "_").replace(".", "-")
        out_path = output_dir / f"_{doi_slug}_elsevier_api.pdf"
        out_path.write_bytes(data)
        result["status"] = "downloaded"
        result["filepath"] = str(out_path)
        result["filename"] = out_path.name
        return result

    # 1. FULL XML → attachment EIDs
    eids: list[str] = []
    try:
        r = s.get(
            f"{ELSEVIER_API_BASE}/article/doi/{doi}",
            headers=_elsevier_api_headers("application/xml"),
            params={"view": "FULL"}, timeout=(20, 90),
        )
        if r.status_code == 403:
            # Key lacks Article Retrieval entitlement — remember it, don't
            # hammer the API for every remaining DOI this run.
            result["message"] = "Elsevier API key lacks Article Retrieval entitlement (403)"
            logger.warning("Elsevier API: 403 config error — key needs Article Retrieval enabled")
            return result
        if r.status_code == 200:
            eids = _elsevier_extract_pdf_eids(r.text)
    except Exception as e:
        logger.debug(f"Elsevier API XML fetch failed: {e}")

    # 2. attachment EIDs → PDF bytes
    for eid in eids:
        try:
            r = s.get(
                f"{ELSEVIER_API_BASE}/object/eid/{eid}",
                headers=_elsevier_api_headers("application/pdf"),
                timeout=(20, 300),
            )
            if r.status_code == 200 and r.content.startswith(b"%PDF") and len(r.content) > 10000:
                logger.info(f"Elsevier API: {doi} via attachment {eid} ({len(r.content)} bytes)")
                return _save(r.content)
        except Exception as e:
            logger.debug(f"Elsevier API attachment {eid} failed: {e}")

    # 3. Fallback: direct PDF endpoint (OA articles)
    try:
        r = s.get(
            f"{ELSEVIER_API_BASE}/article/doi/{doi}",
            headers=_elsevier_api_headers("application/pdf"),
            timeout=(20, 300),
        )
        if r.status_code == 200 and r.content.startswith(b"%PDF") and len(r.content) > 10000:
            logger.info(f"Elsevier API: {doi} direct PDF ({len(r.content)} bytes)")
            return _save(r.content)
    except Exception as e:
        logger.debug(f"Elsevier API direct PDF failed: {e}")

    result["message"] = result["message"] or f"Elsevier API: no PDF (eids={len(eids)})"
    return result if eids else None


def _looks_like_pdf(resp) -> bool:
    """True when the response body is a real PDF file.

    Accepts curl_cffi or requests responses (both expose .headers/.content).
    NOTE: use startswith(b"%PDF") — PDF files begin with b"%PDF-1.x";
    comparing a 5-byte slice to b"%PDF" would reject every real PDF.
    """
    body = resp.content or b""
    ct = (resp.headers.get("Content-Type") or "").lower()
    return body.startswith(b"%PDF") or ("pdf" in ct and len(body) > 5000)


def _pku_download_single(
    session: requests.Session, doi: str, output_dir: Path, provider: str = "pku",
) -> tuple[dict, bool]:
    """Download one DOI through the PKU wrdvpn proxy using plain requests.

    Returns (result, needs_browser). needs_browser=True means the page
    was reachable but a JS-capable browser should retry (anti-bot / JS).
    """
    prov = PROVIDERS[provider]
    result = {
        "doi": doi, "title": "", "status": "download_failed",
        "route": f"webvpn_{provider}", "filepath": "", "filename": "",
        "message": "",
    }
    needs_browser = False

    def _save(resp_or_bytes) -> bool:
        data = resp_or_bytes.content if hasattr(resp_or_bytes, "content") else resp_or_bytes
        if not data or len(data) < 500 or not data.startswith(b"%PDF"):
            return False
        doi_slug = doi.replace("/", "_").replace(".", "-")
        out_path = output_dir / f"_{doi_slug}_webvpn.pdf"
        out_path.write_bytes(data)
        result["status"] = "downloaded"
        result["filepath"] = str(out_path)
        result["filename"] = out_path.name
        return True

    def _vpn_get(url: str, referer: str | None = None) -> requests.Response | None:
        headers = {}
        if referer:
            # Ovid (Wolters Kluwer / Medknow / LWW) serves the article HTML
            # instead of the PDF when the /pdf/ request lacks a Referer.
            headers["Referer"] = referer
        if PROVIDERS[provider]["style"] == "direct":
            try:
                return session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True, headers=headers)
            except Exception as e:
                logger.debug(f"Direct GET failed: {e}")
                return None
        vpn_url = convert_url(url, provider)
        if not vpn_url:
            return None
        try:
            return session.get(vpn_url, timeout=HTTP_TIMEOUT, allow_redirects=True, headers=headers)
        except Exception as e:
            logger.debug(f"PKU webvpn GET failed: {e}")
            return None

    # 1. Resolve DOI → publisher URL (direct, no proxy)
    pub_url = f"https://doi.org/{doi}"
    try:
        r = _no_proxy_get(pub_url, RESOLVE_TIMEOUT, headers={"User-Agent": _HEADERS["User-Agent"]})
        if r.url and not r.url.startswith("https://doi.org"):
            pub_url = r.url
    except Exception as e:
        result["message"] = f"DOI resolution failed: {e}"
        return result, False

    # 2. Publisher direct-PDF hints first (fastest path)
    for hint in _publisher_pdf_hint(pub_url, doi):
        resp = _vpn_get(hint, referer=pub_url)
        if resp is not None and resp.status_code == 200 and _looks_like_pdf(resp):
            if _save(resp):
                logger.info(f"PKU webvpn: {doi} via publisher hint")
                return result, False

    # 3. Fetch the article landing page through the proxy
    resp = _vpn_get(pub_url)
    if resp is None:
        result["message"] = "WebVPN request failed"
        return result, False

    if _is_login_page(resp.url, "", provider):
        result["message"] = "VPN session expired"
        return result, False
    body_head = (resp.text or "")[:3000]
    if "Just a moment" in body_head or "cf-chl" in resp.url or "cf_chl" in resp.url:
        result["message"] = "Blocked by Cloudflare captcha"
        return result, False

    if resp.status_code == 200 and _looks_like_pdf(resp):
        if _save(resp):
            logger.info(f"PKU webvpn: direct PDF {doi}")
            return result, False

    # 4. Parse the landing page for a PDF link (also on 401/403 — several
    # publishers serve the real page with a non-200 status to non-browser
    # fingerprints; only give up after the page yields no PDF)
    html = resp.text or ""
    pdf_url = _find_pdf_on_page(html, pub_url) if html else None
    if pdf_url:
        # Links inside wrdvpn pages are usually already rewritten to the
        # proxy; detect that so we don't double-convert.
        if prov["host"] not in pdf_url:
            pdf_resp = _vpn_get(pdf_url, referer=pub_url)
        else:
            try:
                pdf_resp = session.get(pdf_url, timeout=HTTP_TIMEOUT, allow_redirects=True,
                                       headers={"Referer": pub_url})
            except Exception:
                pdf_resp = None
        if pdf_resp is not None and _looks_like_pdf(pdf_resp):
            if _save(pdf_resp):
                logger.info(f"PKU webvpn: {doi} via page link (status {resp.status_code})")
                return result, False

    if resp.status_code != 200:
        result["message"] = f"Publisher page HTTP {resp.status_code}"
        needs_browser = resp.status_code in (401, 403)
        return result, needs_browser

    result["message"] = "No PDF link found on publisher page"
    return result, True  # page loaded but no PDF — let the browser retry


# ── undetected-chromedriver download (Cloudflare bypass) ────────

# Chrome version detection (cached)
_CHROME_VERSION_MAJOR: int | None = None


def _detect_chrome_version() -> int | None:
    """Detect the installed Chrome major version for undetected-chromedriver."""
    global _CHROME_VERSION_MAJOR
    if _CHROME_VERSION_MAJOR is not None:
        return _CHROME_VERSION_MAJOR
    import subprocess
    for exe in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        if Path(exe).exists():
            try:
                # Use --version with --headless to avoid opening a window
                r = subprocess.run(
                    [exe, "--headless", "--disable-gpu", "--dump-dom", "about:blank"],
                    capture_output=True, text=True, timeout=10,
                )
                # Parse version from stderr
                m = re.search(r"(\d+)\.", r.stderr or "")
                if m:
                    _CHROME_VERSION_MAJOR = int(m.group(1))
                    logger.info(f"Detected Chrome version: {_CHROME_VERSION_MAJOR}")
                    return _CHROME_VERSION_MAJOR
            except Exception:
                pass
    # Fallback: try registry
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
            ver, _ = winreg.QueryValueEx(key, "version")
            m = re.match(r"(\d+)\.", ver)
            if m:
                _CHROME_VERSION_MAJOR = int(m.group(1))
                logger.info(f"Detected Chrome version (registry): {_CHROME_VERSION_MAJOR}")
                return _CHROME_VERSION_MAJOR
    except Exception:
        pass
    return None


def _restore_native_delete():
    """Undo sitecustomize's recycle-bin delete shims, if present.

    Some sandboxed hosts inject a sitecustomize.py that replaces os.remove /
    os.unlink / os.rmdir / shutil.rmtree with "move to Recycle Bin" versions
    which FAIL CLOSED when the bin is unavailable. undetected-chromedriver's
    patcher deletes and rewrites its chromedriver binary AND extracts/cleans
    temp directories, so a fail-closed delete breaks UC launch entirely.
    Restore the native nt.* functions and a native-behavior rmtree.
    """
    try:
        import nt  # POSIX layer on Windows, holds the original functions
        if os.remove is not nt.remove:
            os.remove = nt.remove
            os.unlink = nt.unlink
            os.rmdir = nt.rmdir
    except Exception:
        pass
    import shutil
    if getattr(shutil.rmtree, "__name__", "") != "rmtree":
        def _native_rmtree(path, ignore_errors=False, onerror=None, **kw):
            try:
                if os.path.islink(path):
                    os.unlink(path)
                    return
                for root, dirs, files in os.walk(path, topdown=False):
                    for name in files:
                        p = os.path.join(root, name)
                        try:
                            os.unlink(p)
                        except OSError as e:
                            if onerror is not None:
                                onerror(os.unlink, p, e)
                            else:
                                raise
                    for name in dirs:
                        p = os.path.join(root, name)
                        try:
                            os.rmdir(p)
                        except OSError as e:
                            if onerror is not None:
                                onerror(os.rmdir, p, e)
                            else:
                                raise
                try:
                    os.rmdir(path)
                except OSError as e:
                    if onerror is not None:
                        onerror(os.rmdir, path, e)
                    else:
                        raise
            except OSError:
                if not ignore_errors:
                    raise
        shutil.rmtree = _native_rmtree


def _uc_chrome_build_version() -> str | None:
    """Full Chrome build string (e.g. '151.0.7922.76') for mirror downloads."""
    for base in (
        Path(r"C:\Program Files\Google\Chrome\Application"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application"),
    ):
        if not base.exists():
            continue
        for child in sorted(base.iterdir(), reverse=True):
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", child.name):
                return child.name
    return None


_UC_DRIVER_PATH = Path(
    os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
) / "undetected_chromedriver" / "undetected_chromedriver.exe"

_UC_DRIVER_PATCHED: bool | None = None


def _ensure_uc_driver_patched() -> str | None:
    """Return a locally-patched chromedriver path, patching once if needed.

    UC's default auto() deletes the patched binary and re-downloads it from
    googleapis on EVERY launch — which fails behind the GFW (WinError 10054).
    Passing driver_executable_path takes the custom-path branch in Patcher.
    auto(): if the binary is already patched, no network access happens.
    """
    global _UC_DRIVER_PATCHED
    if _UC_DRIVER_PATCHED is True:
        return str(_UC_DRIVER_PATH)
    if not _HAS_UC:
        return None

    if not _UC_DRIVER_PATH.exists():
        # auto() may have deleted the exe before failing its download.
        # Restore from the npmmirror China mirror (googleapis is blocked).
        try:
            import zipfile, io
            ver = _detect_chrome_version()
            build = _uc_chrome_build_version()
            if not ver or not build:
                return None
            url = (f"https://registry.npmmirror.com/-/binary/chrome-for-testing/"
                   f"{build}/win64/chromedriver-win64.zip")
            data = None
            if _CURL_AVAILABLE:
                r = crequests.get(url, timeout=120, impersonate="chrome")
                if r.status_code == 200:
                    data = r.content
            if not data:
                return None
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                exe = z.read("chromedriver-win64/chromedriver.exe")
            _UC_DRIVER_PATH.parent.mkdir(parents=True, exist_ok=True)
            _UC_DRIVER_PATH.write_bytes(exe)
            logger.info(f"UC: restored chromedriver {build} from mirror")
        except Exception as e:
            logger.debug(f"UC: mirror restore failed: {e}")
            return None

    try:
        patcher = _uc.Patcher(executable_path=str(_UC_DRIVER_PATH))
        if not patcher.is_binary_patched():
            patcher.patch_exe()
        if patcher.is_binary_patched():
            _UC_DRIVER_PATCHED = True
            return str(_UC_DRIVER_PATH)
    except Exception as e:
        logger.debug(f"UC driver pre-patch failed: {e}")
    return None


# ── Hard timeout wrapper for UC Chrome downloads ──────────────────
# Chrome can become unresponsive (crash, memory pressure, infinite JS
# loop) and cause driver.get() / driver.page_source to hang forever.
# This wrapper runs the download in a daemon thread with a hard timeout;
# if exceeded, Chrome is killed to unblock the thread.
_UC_HARD_TIMEOUT_S = 300  # 5 minutes max per DOI


def _uc_download_with_timeout(
    doi: str, output_dir: Path, provider: str,
    timeout: int = _UC_HARD_TIMEOUT_S,
) -> dict:
    """Run _uc_download_single with a hard timeout.

    If the download exceeds *timeout* seconds, all chrome.exe processes
    are killed (which unblocks the WebDriver socket) and a failure
    result is returned immediately.
    """
    result_box: list[dict | None] = [None]

    def _worker() -> None:
        try:
            result_box[0] = _uc_download_single(doi, output_dir, provider)
        except Exception as e:
            result_box[0] = {
                "doi": doi, "title": "", "status": "download_failed",
                "route": f"uc_{provider}", "filepath": "", "filename": "",
                "message": f"UC exception: {e}",
            }

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        logger.warning(
            f"UC: hard timeout ({timeout}s) for {doi} — killing Chrome..."
        )
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
        # Give the worker a moment to react to Chrome being killed
        t.join(timeout=10)
        return {
            "doi": doi, "title": "", "status": "download_failed",
            "route": f"uc_{provider}", "filepath": "", "filename": "",
            "message": f"UC hard timeout ({timeout}s) — Chrome killed",
        }

    return result_box[0] or {
        "doi": doi, "title": "", "status": "download_failed",
        "route": f"uc_{provider}", "filepath": "", "filename": "",
        "message": "UC returned None",
    }


def _uc_download_single(
    doi: str, output_dir: Path, provider: str = "pku_client",
) -> dict:
    """Download a single DOI via undetected-chromedriver.

    Strategy:
    1. Launch real Chrome (anti-detection patches)
    2. Navigate to doi.org/{doi} → Cloudflare challenge auto-resolves (5-10s)
    3. Extract all browser cookies (incl. cf_clearance)
    4. Find PDF URLs on the article page
    5. Download PDF via in-browser fetch() (primary — request originates
       from the browser context that passed the challenge)
    6. Fallback: curl_cffi with the extracted cookies
    """
    result = {
        "doi": doi, "title": "", "status": "download_failed",
        "route": f"uc_{provider}", "filepath": "", "filename": "",
        "message": "",
    }

    if not _HAS_UC:
        result["message"] = "undetected-chromedriver not installed"
        return result

    _restore_native_delete()  # UC patcher needs a working os.unlink

    if PROVIDERS[provider]["style"] != "direct":
        result["message"] = "UC download only for direct (campus VPN) mode"
        return result

    def _save_pdf(data: bytes) -> bool:
        if not data or len(data) < 500 or not data[:5].startswith(b"%PDF"):
            return False
        doi_slug = doi.replace("/", "_").replace(".", "-")
        out_path = output_dir / f"_{doi_slug}_uc.pdf"
        out_path.write_bytes(data)
        result["status"] = "downloaded"
        result["filepath"] = str(out_path)
        result["filename"] = out_path.name
        return True

    # Launch undetected-chromedriver
    options = _uc.ChromeOptions()
    options.add_argument("--no-proxy-server")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1920,1080")

    # PDF auto-download: some publishers (MDPI, SAGE cnpereading) block
    # in-browser fetch() but allow top-level navigation. Setting
    # always_open_pdf_externally makes Chrome download the PDF instead of
    # opening the built-in viewer.
    # IMPORTANT: download dir MUST be ASCII-only — Chrome on Windows fails
    # silently when the path contains non-ASCII chars (e.g. E:\工作\...).
    import tempfile as _tf
    _nav_dl_dir = Path(_tf.gettempdir()) / f"uc_navdl_{doi[:20].replace('/', '-')}"
    _nav_dl_dir.mkdir(parents=True, exist_ok=True)
    options.add_experimental_option("prefs", {
        "download.default_directory": str(_nav_dl_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    })

    ver = _detect_chrome_version()
    driver_path = _ensure_uc_driver_patched()
    try:
        driver = _uc.Chrome(
            options=options, version_main=ver,
            driver_executable_path=driver_path or None,
        )
    except Exception as e:
        result["message"] = f"UC launch failed: {e}"
        return result

    try:
        driver.set_page_load_timeout(90)
        driver.set_script_timeout(120)  # for execute_async_script (in-browser fetch)
        # CDP: set download behavior (belt + suspenders with prefs)
        try:
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(_nav_dl_dir)
            })
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(_nav_dl_dir)
            })
        except Exception:
            pass
        doi_url = f"https://doi.org/{doi}"
        driver.get(doi_url)
        logger.info(f"UC: navigated to {driver.current_url[:80]}")

        # Wait for Cloudflare challenge to resolve
        cf_passed = False
        for i in range(12):
            time.sleep(5)
            try:
                title = (driver.title or "").lower()
                body = (driver.page_source or "")[:500].lower()
            except Exception:
                # Chrome may have crashed — break out and let the
                # hard-timeout wrapper handle cleanup
                logger.warning(f"UC: Chrome unresponsive at CF check {i+1} for {doi}")
                break
            is_challenge = any(sig in body or sig in title for sig in [
                "just a moment", "attention required", "请稍候",
                "正在验证", "checking your browser", "captcha",
                "cf-browser-verification", "challenge-platform",
            ])
            if not is_challenge:
                cf_passed = True
                logger.info(f"UC: Cloudflare passed in {(i+1)*5}s for {doi}")
                break

        if not cf_passed:
            result["message"] = "Cloudflare challenge did not resolve"
            return result

        # Extract cookies
        cookies = {}
        for c in driver.get_cookies():
            cookies[c["name"]] = c["value"]
        cf_cookies = [k for k in cookies if "cf" in k.lower() or "clearance" in k.lower()]
        logger.info(f"UC: {len(cookies)} cookies, CF: {cf_cookies}")

        current_url = driver.current_url
        parsed = urlparse(current_url)
        host = parsed.netloc

        # Collect PDF URL candidates
        from urllib.parse import urlparse as _up
        pdf_urls: list[str] = []

        # From DOM links
        try:
            elements = driver.find_elements(_By.CSS_SELECTOR,
                'a[href*="pdf"], a[href*="PDF"], a[aria-label*="PDF" i], '
                'a[aria-label*="download" i]'
            )
            for el in elements:
                href = el.get_attribute("href") or ""
                text = (el.text or "").lower()
                if "supplement" in href.lower() or "supplement" in text:
                    continue
                if href.startswith("http") and href not in pdf_urls:
                    pdf_urls.append(href)
        except Exception:
            pass

        # From citation_pdf_url meta tag
        try:
            meta = driver.find_element(_By.CSS_SELECTOR, 'meta[name="citation_pdf_url"]')
            href = meta.get_attribute("content")
            if href and href not in pdf_urls:
                pdf_urls.insert(0, href)
        except Exception:
            pass

        # Publisher-specific direct PDF URLs
        if "onlinelibrary.wiley.com" in host:
            pdf_urls.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}")
            pdf_urls.append(f"https://onlinelibrary.wiley.com/doi/pdf/{doi}")
        elif "academic.oup.com" in host:
            pdf_urls.append(f"https://academic.oup.com/downloadpdf/{doi}")
        elif "tandfonline.com" in host:
            pdf_urls.append(f"https://www.tandfonline.com/doi/pdf/{doi}?download=true")
            pdf_urls.append(f"https://www.tandfonline.com/doi/epdf/{doi}")
        elif "mdpi.com" in host:
            pdf_path = parsed.path.rstrip("/") + "/pdf"
            pdf_urls.append(f"https://www.mdpi.com{pdf_path}")
        elif "hogrefe.com" in host or "econtent.hogrefe.com" in host:
            pdf_urls.append(f"https://econtent.hogrefe.com/doi/pdf/{doi}")
            pdf_urls.append(current_url.rstrip("/") + "/pdf")
        elif "bmj.com" in host:
            pdf_urls.append(current_url.rstrip("/") + ".full.pdf")
            pdf_urls.append(current_url.rstrip("/") + "/full.pdf")
        elif "jmir.org" in host:
            pdf_urls.append(current_url.rstrip("/") + "/PDF")
        elif "sagepub.com" in host:
            # SAGE: /doi/pdf/<doi> 直接下载, /doi/epdf/<doi> 兜底
            pdf_urls.append(f"https://journals.sagepub.com/doi/pdf/{doi}?download=true")
            pdf_urls.append(f"https://journals.sagepub.com/doi/epdf/{doi}")
        elif "cnpereading.com" in host:
            # 中图公司 SAGE 镜像 — 校园 IP 下 doi.org 会解析跳转到
            # sage.cnpereading.com, PDF 路径格式与 sagepub 相同
            pdf_urls.append(f"https://{host}/doi/pdf/{doi}?download=true")
            pdf_urls.append(f"https://{host}/doi/epdf/{doi}")
        elif "sciencedirect.com" in host:
            m = re.search(r"/pii/([^/?#]+)", parsed.path)
            if m:
                pdf_urls.append(
                    f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}/pdfft"
                )

        logger.info(f"UC: {len(pdf_urls)} PDF candidates for {doi}: {[u[:70] for u in pdf_urls[:6]]}")

        # Primary: in-browser fetch — the request originates from the same
        # Chrome context that passed the Cloudflare challenge, so it carries
        # cf_clearance, correct Referer and matching TLS fingerprint.
        # (scansci-pdf strategy: "This bypasses Cloudflare because the request
        # comes from the browser context".)
        _FETCH_PDF_JS = """
            var url = arguments[0];
            var cb = arguments[arguments.length - 1];
            fetch(url, {
                credentials: 'include',
                headers: {'Accept': 'application/pdf,*/*'}
            })
            .then(function(r) {
                if (!r.ok) { cb('status:' + r.status); return; }
                var ct = r.headers.get('content-type') || '';
                if (ct.indexOf('text/html') !== -1) { cb('ct:' + ct); return; }
                return r.arrayBuffer().then(function(buf) {
                    var bytes = new Uint8Array(buf);
                    var bin = '';
                    var chunk = 32768;
                    for (var i = 0; i < bytes.length; i += chunk) {
                        bin += String.fromCharCode.apply(
                            null, bytes.subarray(i, Math.min(i + chunk, bytes.length)));
                    }
                    cb('data:' + btoa(bin));
                });
            })
            .catch(function(e) { cb('error:' + e.message); });
        """

        def _uc_fetch_pdf(pdf_url: str) -> bytes | None:
            try:
                raw = driver.execute_async_script(_FETCH_PDF_JS, pdf_url)
            except Exception as e:
                logger.info(f"UC: fetch error for {pdf_url[:60]}: {e}")
                return None
            if not isinstance(raw, str):
                return None
            if raw.startswith("data:"):
                b64 = raw.split(",", 1)[1] if "," in raw else raw[5:]
                try:
                    return base64.b64decode(b64)
                except Exception:
                    return None
            logger.debug(f"UC: fetch not PDF ({raw[:60]}) for {pdf_url[:60]}")
            return None

        for pdf_url in pdf_urls[:6]:
            pdf_bytes = _uc_fetch_pdf(pdf_url)
            logger.info(f"UC: fetch {pdf_url[:80]} -> {'PDF '+str(len(pdf_bytes))+'B' if pdf_bytes and pdf_bytes.startswith(b'%PDF') else 'no ('+(str(len(pdf_bytes))+'B non-PDF)' if pdf_bytes else 'None')}")
            if pdf_bytes and pdf_bytes.startswith(b"%PDF") and len(pdf_bytes) > 5000:
                if _save_pdf(pdf_bytes):
                    logger.info(
                        f"UC: downloaded {doi} via in-browser fetch "
                        f"({len(pdf_bytes)} bytes, {pdf_url[:60]})"
                    )
                    return result

        # Fallback: curl_cffi with cookies extracted from the browser.
        # cf_clearance may be TLS-fingerprint-bound, so this can fail where
        # the in-browser fetch succeeds — keep it only as a second chance.
        if _CURL_AVAILABLE:
            for pdf_url in pdf_urls[:6]:
                logger.info(f"UC: curl_cffi trying {pdf_url[:80]}")
                try:
                    s = crequests.Session(impersonate="chrome")
                    s.headers.update({
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            f"Chrome/{ver or 120}.0.0.0 Safari/537.36"
                        ),
                        "Accept": "application/pdf,text/html,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Referer": current_url,
                    })
                    s.cookies.update(cookies)
                    r = s.get(pdf_url, timeout=40, allow_redirects=True)
                    if r.content.startswith(b"%PDF") and len(r.content) > 5000:
                        if _save_pdf(r.content):
                            logger.info(f"UC: downloaded {doi} via curl_cffi ({len(r.content)} bytes)")
                            return result
                except Exception as e:
                    logger.debug(f"UC: curl_cffi error: {e}")

        # Last resort: navigate to PDF URL directly.  Some publishers
        # (MDPI, SAGE/cnpereading) block in-browser fetch() subresource
        # requests but allow top-level navigation — the browser sends
        # correct Sec-Fetch-Dest=headers and cookies, so Cloudflare/JS
        # bot protection lets it through.  Chrome auto-downloads the PDF
        # because we set plugins.always_open_pdf_externally=true.
        def _uc_navigate_download(pdf_url: str) -> bool:
            try:
                # Clean stale files
                for f in _nav_dl_dir.glob("*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                driver.get(pdf_url)
            except Exception:
                pass  # page-load timeout is OK — download may still finish
            for _ in range(30):  # up to 60s
                time.sleep(2)
                pdfs = list(_nav_dl_dir.glob("*.pdf"))
                crdl = list(_nav_dl_dir.glob("*.crdownload"))
                if pdfs and not crdl:
                    data = pdfs[0].read_bytes()
                    if data[:5] == b"%PDF" and len(data) > 5000:
                        if _save_pdf(data):
                            logger.info(
                                f"UC: downloaded {doi} via navigate-download "
                                f"({len(data)} bytes, {pdf_url[:60]})"
                            )
                            try:
                                pdfs[0].unlink()
                            except Exception:
                                pass
                            return True
                    # invalid — clean up to avoid confusion
                    try:
                        pdfs[0].unlink()
                    except Exception:
                        pass
            logger.info(f"UC: navigate-download no file for {pdf_url[:70]}")
            return False

        for pdf_url in pdf_urls[:6]:
            if _uc_navigate_download(pdf_url):
                return result

        # ── click-download fallback ──
        # Some publishers (MDPI, SAGE/cnpereading) redirect direct
        # navigation to PDF URLs back to the article page.  The only
        # way to trigger the download is to click the on-page button.
        def _uc_click_download() -> bool:
            """Click Download PDF button on the article page."""
            try:
                for f in _nav_dl_dir.glob("*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            except Exception:
                pass

            # Make sure we're on the article page
            cur = driver.current_url or ""
            if "doi.org" in cur or not cur:
                driver.get(f"https://doi.org/{doi}")
                time.sleep(3)

            # Platform-specific click selectors
            host = (driver.current_url or "").lower()
            click_steps = []  # list of CSS selector sequences to try

            if "mdpi.com" in host:
                # MDPI: "Download" dropdown → "Download PDF"
                click_steps.append([
                    'a.UALocations[href*="/pdf"]',
                    'a[onclick*="pdf"]',
                    'div.download_bubble a[href*="/pdf"]',
                ])
                click_steps.append([
                    '#download-button',
                    '.download_bubble',
                    'a:has-text("Download PDF")',
                ])

            elif "cnpereading.com" in host or "sagepub.com" in host:
                # SAGE/cnpereading: direct download link or button
                click_steps.append([
                    'a[href*="/pdf/"]',
                    'a[href*="download"]',
                    'a.download_pdf',
                    'button.download-pdf',
                ])

            # Generic fallbacks
            click_steps.append([
                'a[href*=".pdf"]',
                'a[download]',
            ])

            for step_idx, selectors in enumerate(click_steps):
                for sel in selectors:
                    try:
                        # Use JS to find and click. querySelector may throw
                        # on invalid selectors — catch and try text-based fallback.
                        found = driver.execute_script("""
                            var sel = arguments[0];
                            var el = null;
                            try { el = document.querySelector(sel); } catch(e) {}
                            if (!el) {
                                // text-based fallback: scan all <a> for matching text
                                var links = document.querySelectorAll('a, button');
                                for (var i=0; i<links.length; i++) {
                                    var t = links[i].textContent.trim().toLowerCase();
                                    if (t === 'download pdf' || t === 'pdf') { el = links[i]; break; }
                                }
                            }
                            if (el) { el.click(); return true; }
                            return false;
                        """, sel)
                        if found:
                            logger.info(f"UC: clicked '{sel}' (step {step_idx})")
                            # Wait for download
                            for _ in range(30):  # up to 60s
                                time.sleep(2)
                                pdfs = list(_nav_dl_dir.glob("*.pdf"))
                                crdl = list(_nav_dl_dir.glob("*.crdownload"))
                                if pdfs and not crdl:
                                    data = pdfs[0].read_bytes()
                                    if data[:5] == b"%PDF" and len(data) > 5000:
                                        if _save_pdf(data):
                                            logger.info(
                                                f"UC: click-download success "
                                                f"{doi} ({len(data)} bytes)"
                                            )
                                            try:
                                                pdfs[0].unlink()
                                            except Exception:
                                                pass
                                            return True
                                        try:
                                            pdfs[0].unlink()
                                        except Exception:
                                            pass
                            logger.info(f"UC: click '{sel}' produced no PDF")
                    except Exception as e:
                        logger.info(f"UC: click '{sel}' error: {e}")

            return False

        if _uc_click_download():
            return result

        result["message"] = "UC: Cloudflare passed but no PDF obtained"
        return result

    except Exception as e:
        result["message"] = f"UC error: {e}"
        return result
    finally:
        try:
            driver.quit()
        except Exception:
            pass



def _vpn_url_for(page, url: str, provider: str) -> str | None:
    """Get the VPN URL for a publisher URL (PKU wrdvpn conversion)."""
    return convert_url(url, provider)


def _webvpn_download_single_playwright(
    doi: str, output_dir: Path, page, provider: str = "pku", timeout: int = 60,
) -> dict:
    """Download a single DOI through WebVPN using a Playwright page.

    Works for both providers — only the URL conversion differs.
    """
    prov = PROVIDERS[provider]
    result = {
        "doi": doi, "title": "", "status": "download_failed",
        "route": f"webvpn_{provider}", "filepath": "", "filename": "",
        "message": "",
    }

    def _save_pdf_content(pdf_bytes: bytes) -> bool:
        if not pdf_bytes or len(pdf_bytes) < 500:
            return False
        if not pdf_bytes[:5].startswith(b"%PDF"):
            return False
        doi_slug = doi.replace("/", "_").replace(".", "-")
        out_path = output_dir / f"_{doi_slug}_webvpn.pdf"
        out_path.write_bytes(pdf_bytes)
        result["status"] = "downloaded"
        result["filepath"] = str(out_path)
        result["filename"] = out_path.name
        return True

    def _try_fetch_pdf_from_url(url: str) -> bytes | None:
        try:
            raw = page.evaluate(
                """async (url) => {
                    const resp = await fetch(url, {credentials: "include"});
                    if (!resp.ok) return null;
                    const buf = await resp.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let bin = "";
                    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                    return btoa(bin);
                }""",
                url,
            )
            if raw and isinstance(raw, str):
                return base64.b64decode(raw)
        except Exception as e:
            logger.debug(f"fetch PDF failed: {e}")
        return None

    def _check_page_for_pdf() -> bool:
        prev_url = page.url
        for _ in range(10):
            time.sleep(2)
            try:
                curr_url = page.url
                if curr_url == prev_url:
                    break
                prev_url = curr_url
            except Exception:
                pass

        page_text = ""
        for attempt in range(3):
            try:
                page_text = page.content()
                break
            except Exception:
                time.sleep(2)

        if not page_text:
            result["message"] = "Page content unavailable (navigation timeout)"
            return False

        if "没有权限访问" in page_text:
            result["message"] = "VPN access denied"
            return False
        if "统一身份认证" in page_text or "请登录" in page_text or "用户登录" in page_text:
            result["message"] = "VPN session expired"
            return False
        if "Are you a robot" in page_text or "cf_chl" in page.url or "Just a moment" in page_text[:3000]:
            result["message"] = "Blocked by Cloudflare captcha"
            logger.warning(f"WebVPN: Cloudflare captcha for {doi}")
            return False
        if "Error - Springer Identity" in page_text or "location you are being redirected to is not recognised" in page_text:
            result["message"] = "Springer IdP rejected VPN URL"
            return False

        # Strategy 1: Direct PDF response
        ct = None
        try:
            ct = page.evaluate("() => document.contentType")
        except Exception:
            pass
        if ct and "pdf" in ct.lower():
            pdf_bytes = _try_fetch_pdf_from_url(page.url)
            if pdf_bytes and _save_pdf_content(pdf_bytes):
                logger.info(f"WebVPN: direct PDF {doi}")
                return True

        # Strategy 2: citation_pdf_url meta tag
        meta_pdf = page.query_selector('meta[name="citation_pdf_url"]')
        if meta_pdf:
            pdf_url = meta_pdf.get_attribute("content")
            if pdf_url:
                logger.debug(f"WebVPN: found citation_pdf_url: {pdf_url}")
                if prov["host"] not in pdf_url:
                    vpn_pdf = _vpn_url_for(page, pdf_url, provider)
                    if vpn_pdf:
                        pdf_url = vpn_pdf
                try:
                    page.goto(pdf_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    time.sleep(3)
                    pdf_bytes = _try_fetch_pdf_from_url(page.url)
                    if pdf_bytes and _save_pdf_content(pdf_bytes):
                        logger.info(f"WebVPN: downloaded {doi} via citation_pdf_url")
                        return True
                except Exception as e:
                    logger.debug(f"WebVPN: PDF download failed: {e}")

        # Strategy 3: PDF links on page
        pdf_links = page.query_selector_all('a[href*=".pdf"]')
        for link in pdf_links[:5]:
            href = link.get_attribute("href")
            if href:
                if href.startswith("/"):
                    parsed = urlparse(page.url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                elif not href.startswith("http"):
                    continue
                logger.debug(f"WebVPN: trying PDF link: {href}")
                try:
                    page.goto(href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    time.sleep(2)
                    pdf_bytes = _try_fetch_pdf_from_url(page.url)
                    if pdf_bytes and _save_pdf_content(pdf_bytes):
                        logger.info(f"WebVPN: downloaded {doi} via PDF link")
                        return True
                except Exception:
                    pass

        # Strategy 4: Click PDF/Download buttons
        for sel in ['a:has-text("PDF")', 'button:has-text("PDF")',
                     'a:has-text("Download")', 'a:has-text("Full Text")',
                     'a:has-text("Full Article")']:
            try:
                btn = page.query_selector(sel)
                if btn:
                    href = btn.get_attribute("href")
                    if href and ".pdf" in href.lower():
                        if href.startswith("/"):
                            parsed = urlparse(page.url)
                            href = f"{parsed.scheme}://{parsed.netloc}{href}"
                        try:
                            page.goto(href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                            time.sleep(2)
                            pdf_bytes = _try_fetch_pdf_from_url(page.url)
                            if pdf_bytes and _save_pdf_content(pdf_bytes):
                                logger.info(f"WebVPN: downloaded {doi} via button")
                                return True
                        except Exception:
                            pass
            except Exception:
                pass

        result["message"] = "No PDF link found on publisher page"
        return False

    # ── Main flow ──
    doi_url = f"https://doi.org/{doi}"
    style = PROVIDERS[provider]["style"]

    if style == "direct":
        # Campus VPN client — resolve DOI directly (tunnel handles auth)
        vpn_url = doi_url
        try:
            r = _no_proxy_get(doi_url, RESOLVE_TIMEOUT, headers={"User-Agent": _HEADERS["User-Agent"]})
            if r.url and r.url != doi_url:
                vpn_url = r.url
                logger.debug(f"Direct: resolved {doi} → {vpn_url}")
        except Exception as e:
            logger.debug(f"Direct: DOI resolution failed: {e}")
    elif style == "wrdvpn":
        vpn_url = convert_url(doi_url, provider)
        # wrdvpn proxies doi.org fine, but resolving first gives publisher hints
        if not vpn_url:
            result["message"] = "URL conversion failed"
            return result
    else:
        vpn_url = _quick_api_convert(page, doi_url)
        if not vpn_url:
            try:
                r = _no_proxy_get(doi_url, RESOLVE_TIMEOUT, headers={"User-Agent": _HEADERS["User-Agent"]})
                if r.url and r.url != doi_url:
                    pub_url = r.url
                    logger.debug(f"WebVPN: resolved {doi} → {pub_url}")
                    vpn_url = _quick_api_convert(page, pub_url)
            except Exception as e:
                logger.debug(f"WebVPN: DOI resolution failed: {e}")

    if not vpn_url:
        result["message"] = "Site not supported by WebVPN"
        return result

    logger.debug(f"WebVPN: navigating to {vpn_url}")
    try:
        page.goto(vpn_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if _check_page_for_pdf():
            return result
    except Exception as e:
        logger.debug(f"WebVPN: navigation error: {e}")
        result["message"] = f"Navigation error: {e}"

    return result


# ── Phase 2 orchestrator ─────────────────────────────

# ── Elsevier semi-manual download ─────────────────────────────────────────────
# CPE00001 / crasolve blocks every programmatic route (requests, curl_cffi, UC,
# real-Edge programmatic pdfft fetch). Final strategy: open the article page in
# a VISIBLE Edge window, the user manually passes the captcha and clicks
# "Download PDF" (real human gesture), and we capture the PDF bytes from the
# network layer via CDP response interception — zero programmatic PDF requests.

_ELSEVIER_MANUAL_PORT = 9223
_ELSEVIER_MANUAL_TIMEOUT_S = 420  # per-DOI wait for the user

# Platforms that need semi-manual flow (program opens browser, user passes
# verification + clicks download, program captures PDF via CDP network layer).
_MANUAL_PREFIXES = (
    "10.1016/", "10.1056/",   # Elsevier / ScienceDirect / NEJM
    "10.3390/",               # MDPI (Cloudflare JS challenge)
    "10.1037/", "10.1176/",   # APA (psycnet.apa.org, psychiatryonline.org)
    "10.1111/", "10.1002/",   # Wiley (Cloudflare "Just a moment")
    "10.1093/",               # Oxford (Cloudflare)
    "10.2196/",               # JMIR (Cloudflare)
)

_MANUAL_PLATFORM_NAMES = {
    "10.1016": "Elsevier", "10.1056": "Elsevier(NEJM)",
    "10.3390": "MDPI", "10.1037": "APA(psycnet)", "10.1176": "APA(psychiatryonline)",
    "10.1111": "Wiley", "10.1002": "Wiley",
    "10.1093": "Oxford", "10.2196": "JMIR",
}

def _manual_platform_name(doi: str) -> str:
    d = doi.lower()
    for prefix, name in _MANUAL_PLATFORM_NAMES.items():
        if d.startswith(prefix):
            return name
    return "Unknown"
_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
_PII_RE = re.compile(r"(?:/pii/|1-s2\.0-)([A-Z0-9]+)", re.I)


def _pii_from_url(url: str) -> str | None:
    m = _PII_RE.search(url or "")
    return m.group(1).upper() if m else None


def _ensure_manual_browser(profile_dir: Path):
    """Launch (or attach to) a visible Chromium browser with CDP enabled.

    Returns (playwright, browser, cleanup_fn). Uses Edge (real user profile
    seed) so the ScienceDirect session/institutional access survives.
    """
    import subprocess

    pw = sync_playwright().start()
    endpoint = f"http://127.0.0.1:{_ELSEVIER_MANUAL_PORT}"

    def _try_connect():
        try:
            return pw.chromium.connect_over_cdp(endpoint, timeout=3000)
        except Exception:
            return None

    browser = _try_connect()
    if browser is not None:
        return pw, browser, lambda: None

    exe = next((p for p in _EDGE_PATHS if Path(p).exists()), None)
    if exe is None:
        pw.stop()
        raise RuntimeError("No Edge/Chrome found for manual verification")

    profile_dir.mkdir(parents=True, exist_ok=True)
    # Seed login state from the captured Edge profile on first run (best-effort;
    # institutional IP access works even without cookies).
    cookies_dst = profile_dir / "Default" / "Network" / "Cookies"
    if not cookies_dst.exists():
        seed = profile_dir.parent / "_uc_profile"
        try:
            cookies_src = seed / "Default" / "Network" / "Cookies"
            if cookies_src.exists():
                cookies_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cookies_src, cookies_dst)
            ls_src = seed / "Local State"
            if ls_src.exists():
                shutil.copy2(ls_src, profile_dir / "Local State")
        except OSError:
            pass  # locked by a running browser — IP-based access still works

    subprocess.Popen([
        exe,
        f"--remote-debugging-port={_ELSEVIER_MANUAL_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-popup-blocking", "--window-size=1400,1000",
        "--no-proxy-server",
        "about:blank",
    ])
    for _ in range(25):
        time.sleep(1)
        browser = _try_connect()
        if browser is not None:
            return pw, browser, lambda: None
    pw.stop()
    raise RuntimeError("Manual-verification browser CDP did not come up")


def _manual_batch_download(
    dois: list[str],
    phase2_dir: Path,
    papers_dir: Path,
    stats: dict,
    all_results: list[dict],
    progress_callback,
    provider: str,
) -> None:
    """Open each article in a visible browser for semi-manual download.

    Supports Elsevier (Cloudflare "Are you a robot"), MDPI (Cloudflare JS
    challenge, user clicks Download dropdown → Download PDF), and APA
    (Incapsula protection).

    For every DOI: navigate to the article page → prompt the user to pass
    the verification and click "Download PDF" → capture the PDF response
    via CDP network layer → save + finalize.
    """
    logger.info(f"Phase 2 (semi-manual): {len(dois)} DOIs")

    def _note(msg: str) -> None:
        logger.info(f"Semi-manual: {msg}")
        if progress_callback:
            progress_callback("phase2_warning", {"message": msg})

    # Signal the transition to semi-manual phase
    if progress_callback:
        progress_callback("phase2_progress", {
            "current": len(all_results), "total": stats["total"],
            "downloaded": stats["downloaded"],
            "failed": stats["download_failed"],
            "doi": "", "title": "",
            "status": "manual_phase_start",
            "provider": provider, "stage": "semi_manual",
            "manual_total": len(dois),
        })

    pw, browser, _cleanup = _ensure_manual_browser(phase2_dir.parent / "_edge_manual_profile")
    # We process DOIs one at a time, so any PDF captured while a tab is
    # open belongs to that DOI — no need for PII/domain matching.
    captured_pdf: list[bytes] = []

    # ── Download-directory fallback ─────────────────────────────────
    # Many publisher "Download PDF" buttons trigger a *browser download*
    # (Content-Disposition: attachment / <a download>), not an in-page
    # fetch.  In that case resp.body() returns empty and the response
    # handler above never fires.  To catch these, point the browser's
    # downloads at a known ASCII-only directory and poll it for PDFs.
    import tempfile as _tf
    _manual_dl_dir = Path(_tf.gettempdir()) / f"manual_dl_{int(time.time())}"
    _manual_dl_dir.mkdir(parents=True, exist_ok=True)

    def _scan_download_dir() -> bytes | None:
        """Return a finished PDF from the download dir, or None."""
        try:
            if list(_manual_dl_dir.glob("*.crdownload")):
                return None  # a download is still in progress
            for pdf in sorted(_manual_dl_dir.glob("*.pdf")):
                data = pdf.read_bytes()
                if data.startswith(b"%PDF") and len(data) > 10000:
                    return data
        except Exception:
            pass
        return None

    def _clear_download_dir() -> None:
        try:
            for f in _manual_dl_dir.glob("*"):
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _on_response(resp) -> None:
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            if "pdf" not in ct and "octet" not in ct:
                return
            body = resp.body()
            if not body.startswith(b"%PDF") or len(body) < 10000:
                return
            captured_pdf.append(body)
            logger.info(f"Semi-manual: captured PDF response ({len(body)} bytes) from {resp.url[:80]}")
        except Exception:
            pass

    try:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ctx.on("response", _on_response)

        for i, doi in enumerate(dois):
            plat = _manual_platform_name(doi)
            _note(f"[{i + 1}/{len(dois)}] {plat} 人工下载：正在打开 {doi} —— "
                  f"请在弹出的浏览器窗口中完成验证，然后点击页面上的 Download PDF 按钮")

            # Send a progress event so the UI shows this DOI is being processed
            if progress_callback:
                progress_callback("phase2_progress", {
                    "current": len(all_results), "total": stats["total"],
                    "downloaded": stats["downloaded"],
                    "failed": stats["download_failed"],
                    "doi": doi, "title": "",
                    "status": "manual_waiting",
                    "provider": provider, "stage": "semi_manual",
                    "manual_index": i + 1, "manual_total": len(dois),
                })

            result = {
                "doi": doi, "title": "", "status": "download_failed",
                "route": f"manual_edge_{provider}", "filepath": "", "filename": "",
                "message": "manual verification timed out",
            }
            tab = None
            try:
                tab = ctx.new_page()
                captured_pdf.clear()
                _clear_download_dir()
                # Point this browser's downloads at our ASCII dir so the
                # download-directory fallback can find them.
                try:
                    _cdp = ctx.new_cdp_session(tab)
                    _cdp.send("Browser.setDownloadBehavior", {
                        "behavior": "allow",
                        "downloadPath": str(_manual_dl_dir),
                        "eventsEnabled": True,
                    })
                except Exception:
                    pass  # CDP may be unavailable on some connect_over_cdp setups

                try:
                    tab.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded", timeout=90000)
                except Exception:
                    pass

                asked_click = False
                last_heartbeat = time.time()
                deadline = time.time() + _ELSEVIER_MANUAL_TIMEOUT_S
                while time.time() < deadline:
                    # Prefer the in-page response capture; fall back to a
                    # finished download in the download dir.
                    if not captured_pdf:
                        _dl_body = _scan_download_dir()
                        if _dl_body:
                            captured_pdf.append(_dl_body)
                            logger.info(
                                f"Semi-manual: captured PDF from download dir ({len(_dl_body)} bytes)"
                            )
                    if captured_pdf:
                        body = captured_pdf[0]
                        slug = doi.replace("/", "_").replace(".", "-")
                        out = phase2_dir / f"_{slug}_manual.pdf"
                        out.write_bytes(body)
                        try:
                            result["title"] = (tab.title() or "")[:200]
                        except Exception:
                            pass
                        result.update({
                            "status": "downloaded", "filepath": str(out),
                            "filename": out.name, "message": "semi-manual capture",
                        })
                        _note(f"[{i + 1}/{len(dois)}] {doi} 已捕获 PDF ({len(body)} bytes)")
                        break
                    try:
                        title = (tab.title() or "").lower()
                    except Exception:
                        title = ""
                    if not asked_click and title and not any(
                        s in title for s in ("moment", "稍候", "robot", "attention", "verifying", "incapsula")
                    ):
                        asked_click = True
                        _note(f"[{i + 1}/{len(dois)}] {plat} 验证已通过 —— 请点击 Download PDF 按钮（等待中，超时 7 分钟）")
                    # Send a heartbeat every 30 seconds so the user knows we're still waiting
                    if time.time() - last_heartbeat > 30:
                        last_heartbeat = time.time()
                        remain_min = int((deadline - time.time()) / 60)
                        _note(f"[{i + 1}/{len(dois)}] {doi} 仍在等待人工操作... (剩余超时 {remain_min} 分钟)")
                    time.sleep(2)
            except Exception as e:
                result["message"] = f"manual stage error: {e}"
            finally:
                if tab is not None:
                    try:
                        tab.close()
                    except Exception:
                        pass

            all_results[:] = [r for r in all_results if r["doi"] != doi]
            all_results.append(result)
            _finalize_result(result, papers_dir, stats)
            if progress_callback:
                progress_callback("phase2_progress", {
                    "current": len(all_results), "total": stats["total"],
                    "downloaded": stats["downloaded"],
                    "failed": stats["download_failed"],
                    "doi": doi, "title": result.get("title", ""),
                    "status": result["status"], "provider": provider,
                    "stage": "semi_manual",
                })
        _note("半自动下载阶段结束 —— 浏览器窗口可以关闭了")
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()


def _finalize_result(result: dict, papers_dir: Path, stats: dict) -> None:
    """Copy a downloaded PDF into papers/ with metadata filename; update stats."""
    if result["status"] == "downloaded":
        meta = get_metadata(result["doi"])
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


def _run_pku(dois: list[str], out_root: Path, progress_callback, provider: str = "pku") -> tuple[list[dict], dict]:
    """PKU flow: requests-first, Playwright retry for JS-heavy pages.

    Works for both "pku" (wrdvpn proxy) and "pku_client" (campus VPN
    client, direct requests over the tunnel).
    """
    phase2_dir = out_root / "phase2_output"
    papers_dir = out_root / "papers"
    total = len(dois)

    stats = {"total": total, "downloaded": 0, "download_failed": 0}
    all_results: list[dict] = []
    browser_retry: list[str] = []

    session = _create_webvpn_session(provider)
    if session is None:
        for doi in dois:
            all_results.append({
                "doi": doi, "title": "", "status": "download_failed",
                "route": f"webvpn_{provider}", "filepath": "", "filename": "",
                "message": f"No {PROVIDERS[provider]['label']} session available",
            })
        stats["download_failed"] = total
        return all_results, stats

    logger.info(f"Phase 2 ({provider} requests): {total} DOIs")

    # ── Checkpoint: skip DOIs that already have a PDF from a previous run ──
    # phase2_output/ filenames follow _{doi_slug}_{suffix}.pdf where
    # doi_slug = doi.replace("/", "_").replace(".", "-")
    existing_pdfs: dict[str, Path] = {}
    if phase2_dir.exists():
        for pdf in phase2_dir.glob("*.pdf"):
            if pdf.stat().st_size < 500:
                continue
            name = pdf.stem
            if name.startswith("_"):
                name = name[1:]
            for suffix in ("_webvpn", "_uc", "_elsevier_api", "_manual"):
                if name.endswith(suffix):
                    slug = name[:-len(suffix)]
                    existing_pdfs[slug] = pdf
                    break

    dois_to_process: list[str] = []
    skipped_checkpoint = 0
    for doi in dois:
        slug = doi.replace("/", "_").replace(".", "-")
        if slug in existing_pdfs:
            pdf_path = existing_pdfs[slug]
            meta = get_metadata(doi)
            title = meta.get("title", "") if meta else ""
            result = {
                "doi": doi, "title": title, "status": "downloaded",
                "route": "checkpoint_skip", "filepath": str(pdf_path),
                "filename": pdf_path.name, "message": "already downloaded (skipped)",
            }
            all_results.append(result)
            _finalize_result(result, papers_dir, stats)
            skipped_checkpoint += 1
            if progress_callback:
                progress_callback("phase2_progress", {
                    "current": len(all_results), "total": total,
                    "downloaded": stats["downloaded"],
                    "failed": stats["download_failed"],
                    "doi": doi, "title": title,
                    "status": "already_downloaded",
                    "provider": provider, "stage": "checkpoint",
                })
        else:
            dois_to_process.append(doi)

    if skipped_checkpoint:
        logger.info(f"Phase 2: skipped {skipped_checkpoint} DOIs (already downloaded), "
                     f"{len(dois_to_process)} remaining")

    for i, doi in enumerate(dois_to_process):
        logger.info(f"Phase 2 [{provider} {i+1}/{len(dois_to_process)}]: {doi}")

        # ── Skip publishers that require manual supplement ──
        # SAGE (10.1177) and Hogrefe (10.1026, 10.1027) cannot be
        # downloaded automatically: SAGE resolves to cnpereading.com
        # mirror that requires click-triggered session cookies; Hogrefe
        # needs institutional subscription that PKU doesn't have.
        # NOTE: Hogrefe uses TWO prefixes — 10.1027 (English-language
        # psychology journals) and 10.1026 (German-language journals).
        _MANUAL_SKIP_PREFIXES = ("10.1177/", "10.1027/", "10.1026/")
        if doi.lower().startswith(_MANUAL_SKIP_PREFIXES):
            logger.info(f"Phase 2: skipping {doi} (manual supplement)")
            result = {
                "doi": doi, "title": "", "status": "manual_supplement",
                "route": f"webvpn_{provider}", "filepath": "", "filename": "",
                "message": "Manual supplement required (SAGE/Hogrefe)",
            }
            all_results.append(result)
            stats.setdefault("manual_supplement", 0)
            stats["manual_supplement"] += 1
            stats["download_failed"] += 1
            if progress_callback:
                progress_callback("phase2_progress", {
                    "current": i + 1, "total": total,
                    "downloaded": stats["downloaded"],
                    "failed": stats["download_failed"],
                    "doi": doi, "title": "", "status": "manual_supplement",
                    "provider": provider, "stage": "skipped",
                })
            continue
        # Cloudflare / CPE00001 entirely (when the key is entitled).
        if doi.lower().startswith(_ELSEVIER_PREFIXES):
            api_result = _elsevier_api_download(doi, phase2_dir)
            if api_result is not None and api_result["status"] == "downloaded":
                all_results.append(api_result)
                _finalize_result(api_result, papers_dir, stats)
                if progress_callback:
                    progress_callback("phase2_progress", {
                        "current": i + 1, "total": total,
                        "downloaded": stats["downloaded"],
                        "failed": stats["download_failed"],
                        "doi": doi, "title": api_result.get("title", ""),
                        "status": api_result["status"], "provider": provider,
                        "stage": "elsevier_api",
                    })
                time.sleep(1)
                continue
            # API unavailable / no entitlement / no PDF → normal web routes

        # ── Defer semi-manual platforms ─────────────────────────────
        # Elsevier (after API failed above), NEJM, MDPI and APA are all
        # Cloudflare/Incapsula-protected.  Auto-download here (requests +
        # UC Chrome) almost always fails and burns a lot of time, and the
        # user has explicitly chosen to handle them in the semi-manual
        # browser flow.  Skip straight there.
        if doi.lower().startswith(_MANUAL_PREFIXES):
            logger.info(f"Phase 2: deferring {doi} to semi-manual")
            result = {
                "doi": doi, "title": "", "status": "download_failed",
                "route": f"manual_edge_{provider}", "filepath": "", "filename": "",
                "message": "deferred to semi-manual (Cloudflare/Incapsula)",
            }
            all_results.append(result)
            stats["download_failed"] += 1
            if progress_callback:
                progress_callback("phase2_progress", {
                    "current": i + 1, "total": total,
                    "downloaded": stats["downloaded"],
                    "failed": stats["download_failed"],
                    "doi": doi, "title": "", "status": "manual_waiting",
                    "provider": provider, "stage": "deferred_semi_manual",
                })
            continue

        result, needs_browser = _pku_download_single(session, doi, phase2_dir, provider)
        # Auto-download failed with a 401/403 (Cloudflare/anti-bot) → mark
        # for manual supplement right away.  The UC Chrome retry would just
        # burn ~5 minutes on challenge pages it can't pass, so skip it.
        if result["status"] != "downloaded" and needs_browser:
            result["status"] = "manual_supplement"
            result["message"] = "manual supplement required (auto-download failed)"
            result["route"] = f"manual_{provider}"
        all_results.append(result)
        if result["status"] == "manual_supplement":
            stats.setdefault("manual_supplement", 0)
            stats["manual_supplement"] += 1

        _finalize_result(result, papers_dir, stats)
        if progress_callback:
            progress_callback("phase2_progress", {
                "current": i + 1, "total": total,
                "downloaded": stats["downloaded"],
                "failed": stats["download_failed"],
                "doi": doi, "title": result.get("title", ""),
                "status": result["status"], "provider": provider,
                "stage": "requests",
            })
        time.sleep(2)

    # ── undetected-chromedriver retry for Cloudflare-protected publishers ──
    # Only for direct (campus VPN) mode — wrdvpn mode needs URL conversion
    uc_retry: list[str] = []
    if browser_retry and _HAS_UC and PROVIDERS[provider]["style"] == "direct":
        logger.info(f"Phase 2 ({provider} UC retry): {len(browser_retry)} DOIs")
        if progress_callback:
            progress_callback("phase2_warning", {
                "message": f"Retrying {len(browser_retry)} papers with anti-bot browser (Cloudflare bypass)",
            })
        for i, doi in enumerate(browser_retry[:]):
            logger.info(f"Phase 2 [UC {i+1}/{len(browser_retry)}]: {doi}")
            uc_result = _uc_download_with_timeout(doi, phase2_dir, provider)
            if uc_result["status"] == "downloaded":
                # Replace the earlier failed result
                all_results = [r for r in all_results if r["doi"] != doi]
                all_results.append(uc_result)
                _finalize_result(uc_result, papers_dir, stats)
                browser_retry.remove(doi)
                if progress_callback:
                    progress_callback("phase2_progress", {
                        "current": len(all_results), "total": total,
                        "downloaded": stats["downloaded"],
                        "failed": stats["download_failed"],
                        "doi": doi, "title": uc_result.get("title", ""),
                        "status": uc_result["status"], "provider": provider,
                        "stage": "uc_browser",
                    })
            else:
                uc_retry.append(doi)
            time.sleep(3)
        # DOIs that UC couldn't solve either → fall through to Playwright
        browser_retry = uc_retry

    # ── Playwright retry for DOIs that need a real browser ──
    if browser_retry and _HAS_SYNC_PLAYWRIGHT:
        logger.info(f"Phase 2 ({provider} browser retry): {len(browser_retry)} DOIs")
        if progress_callback:
            progress_callback("phase2_warning", {
                "message": f"Retrying {len(browser_retry)} papers in browser mode (JS/anti-bot pages)",
            })
        cfg = load_webvpn_config().get(provider, {})
        cookie_objects = cfg.get("cookie_objects")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-proxy-server"],
                )
                context = browser.new_context(user_agent=_HEADERS["User-Agent"])
                # Generous defaults: slow VPN relays + in-page fetch() of large PDFs
                context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                context.set_default_timeout(NAV_TIMEOUT_MS)

                if cookie_objects:
                    context.add_cookies(cookie_objects)
                elif PROVIDERS[provider]["style"] != "direct":
                    context.add_cookies([{
                        "name": n, "value": v,
                        "domain": f".{PROVIDERS[provider]['host']}", "path": "/",
                    } for n, v in (cfg.get("cookies") or {}).items()])

                page = context.new_page()
                for doi in browser_retry:
                    result = _webvpn_download_single_playwright(doi, phase2_dir, page, provider)
                    # replace earlier failed result
                    all_results = [r for r in all_results if r["doi"] != doi]
                    all_results.append(result)
                    _finalize_result(result, papers_dir, stats)
                    if progress_callback:
                        progress_callback("phase2_progress", {
                            "current": len(all_results), "total": total,
                            "downloaded": stats["downloaded"],
                            "failed": stats["download_failed"],
                            "doi": doi, "title": result.get("title", ""),
                            "status": result["status"], "provider": provider,
                            "stage": "browser",
                        })
                    time.sleep(2)
                page.close()
                browser.close()
        except Exception as e:
            logger.error(f"Phase 2 {provider} browser retry error: {e}")

    # ── Elsevier semi-manual stage: user passes verification in visible Edge ──
    # Every programmatic route is blocked by CPE00001/crasolve; the only viable
    # path is a real human gesture. We open article pages in a visible browser,
    # the user clicks through, and we capture the PDF from the network layer.
    # Supports Elsevier (Cloudflare), MDPI (Cloudflare JS), APA (Incapsula).
    if load_webvpn_config().get("elsevier_manual", True):
        manual_failed = [
            r["doi"] for r in all_results
            if r["doi"].lower().startswith(_MANUAL_PREFIXES)
            and r["status"] != "downloaded"
        ]
        if manual_failed and progress_callback:
            progress_callback("phase2_warning", {
                "message": (
                    f"{len(manual_failed)} 篇文献需要人工验证（Elsevier/MDPI/APA）：即将打开浏览器窗口，"
                    "请在每篇文章页面完成验证并点击 Download PDF，程序会自动保存 PDF"
                ),
            })
        if manual_failed:
            try:
                _manual_batch_download(
                    manual_failed, phase2_dir, papers_dir, stats,
                    all_results, progress_callback, provider,
                )
            except Exception as e:
                logger.error(f"Semi-manual stage error: {e}")
                if progress_callback:
                    progress_callback("phase2_warning", {
                        "message": f"半自动下载阶段失败: {e}",
                    })

    return all_results, stats


def run_phase2(
    dois: list[str],
    output_dir: str = "",
    progress_callback: Callable | None = None,
    provider: str = "",
) -> tuple[list[dict], dict]:
    """Run Phase 2 (library access) on a list of DOIs.

    Supported providers:
      pku        : PKU Library WebVPN (wpn.pku.edu.cn, wrdvpn proxy),
                   requests-first with automatic browser retry.
      pku_client : PKU campus VPN client (direct requests over the tunnel;
                   IP-based auth, no cookies, no URL rewriting).

    Returns (results, stats).
    """
    if not provider:
        provider = get_active_provider()
    if provider not in PROVIDERS or PROVIDERS[provider]["style"] not in ("wrdvpn", "direct"):
        raise ValueError(f"Unsupported provider: {provider}. Supported: pku, pku_client")
    prov = PROVIDERS[provider]

    if output_dir:
        out_root = Path(output_dir).resolve()
    else:
        out_root = BASE_DIR

    (out_root / "phase2_output").mkdir(parents=True, exist_ok=True)
    (out_root / "papers").mkdir(parents=True, exist_ok=True)

    if not dois:
        return [], {"total": 0, "downloaded": 0, "download_failed": 0}

    total = len(dois)
    logger.info(f"Phase 2 ({prov['label']}): Processing {total} DOIs")

    if progress_callback:
        progress_callback("phase2_start", {"total": total, "provider": provider})

    return _run_pku(dois, out_root, progress_callback, provider)
