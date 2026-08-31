"""
screener_service.py
--------------------
Two independent failure modes were found in production logs and are both
handled here:

1. Screener's JSON search API (`/api/company/search/?q=...&v=3`) is NOT
   blocked — it returns clean 200 JSON — but it does near-exact/substring
   matching against a *short* company name, not the full registered legal
   name (e.g. "Techno Electric & Engineering Company Ltd." matches
   nothing, but "Techno Electric" does). Fixed by generating progressively
   shorter candidate queries (stripping "Limited"/"Ltd"/"Company"/"Pvt"
   suffixes, then falling back to just the first word(s)) and trying each
   until one returns a match.

2. The Playwright fallback used to simulate a click into the on-page
   search box, which is unreliable (hidden behind a responsive
   breakpoint/nav state depending on viewport). Fixed by having Playwright
   navigate directly to the JSON API URL instead — a real browser request,
   no UI interaction needed, and it still gets past bot-detection that a
   plain `requests` call might trip that the direct API path doesn't.

Requires Playwright + a Chromium install (same dependency the rest of this
app already uses for its own crawler).
"""

from __future__ import annotations

import re
import time
import json
import logging
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE_URL = "https://www.screener.in"

# Screener requires a session cookie (csrftoken) even for public pages.
# We get it by hitting the homepage first, exactly like a browser would.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
TIMEOUT = 20

# ── Single shared session (warm once, reuse) ──────────────────────────────────
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        # Warm the session — gets csrftoken cookie
        try:
            s.get(f"{BASE_URL}/", timeout=15)
            time.sleep(0.3)
        except Exception:
            pass
        _session = s
    return _session


class ScreenerError(Exception):
    pass


# ── Search ────────────────────────────────────────────────────────────────────

_LEGAL_SUFFIX_RE = re.compile(
    r"\s*(private\s+limited|pvt\.?\s*ltd\.?|public\s+limited|"
    r"limited|ltd\.?|company|co\.?)\s*\.?\s*$",
    re.IGNORECASE,
)


def _generate_search_candidates(query: str) -> list[str]:
    """Screener's search matches against short/trading names, not full
    registered legal names — 'Techno Electric & Engineering Company Ltd.'
    matches nothing, but 'Techno Electric' does. Generate progressively
    shorter candidates by stripping legal suffixes one at a time, then
    fall back to just the first couple of significant words."""
    q = query.strip()
    candidates = [q]
    current = q
    while True:
        new = _LEGAL_SUFFIX_RE.sub("", current).strip(" .,")
        if new and new != current:
            candidates.append(new)
            current = new
        else:
            break

    words = re.findall(r"[A-Za-z0-9]+", q)
    if len(words) >= 2:
        candidates.append(" ".join(words[:2]))
    if words:
        candidates.append(words[0])

    seen, out = set(), []
    for c in candidates:
        c = c.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def _query_json_api(s: requests.Session, query: str, errors: list) -> list | None:
    """One JSON-API attempt for a single candidate query string. Returns
    the results list on success, or None (with the reason appended to
    `errors`) on failure/empty."""
    for params in ({"q": query, "v": "3"}, {"q": query}):
        try:
            r = s.get(
                f"{BASE_URL}/api/company/search/",
                params=params,
                headers={**HEADERS, "Accept": "application/json",
                         "X-Requested-With": "XMLHttpRequest",
                         "Referer": f"{BASE_URL}/"},
                timeout=TIMEOUT,
            )
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" in ctype:
                data = r.json()
                if isinstance(data, list) and data:
                    return data
                if isinstance(data, dict) and data.get("results"):
                    return data["results"]
                errors.append(f"q={query!r} {params} → empty result")
            else:
                errors.append(
                    f"q={query!r} {params} → HTTP {r.status_code}, "
                    f"content-type={ctype!r}, body[:150]={r.text[:150]!r}"
                )
        except Exception as e:
            errors.append(f"q={query!r} {params} → raised: {e}")
    return None


def search_company(query: str) -> list:
    """
    Search Screener.in for a company name.
    Tries the JSON API across several progressively-shortened candidate
    queries (full legal names often don't match Screener's index), then
    falls back to Playwright directly hitting the same JSON API (a real
    browser request, useful if `requests` alone is getting bot-blocked).
    Returns list of: {'id', 'name', 'url'}
    Every attempt logs *why* it failed instead of silently swallowing it.
    """
    s = _get_session()
    errors = []
    candidates = _generate_search_candidates(query)

    # ── Attempt 1: JSON API via requests, across all candidate queries ────────
    for candidate in candidates:
        result = _query_json_api(s, candidate, errors)
        if result:
            return result

    logger.warning("[screener_service] JSON search API failed for all candidates %s: %s",
                    candidates, " | ".join(errors))

    # ── Attempt 2: Playwright hitting the JSON API directly (bypasses bot-detection) ──
    if PLAYWRIGHT_AVAILABLE:
        for candidate in candidates:
            try:
                result = _search_via_playwright(candidate)
                if result:
                    return result
                errors.append(f"Playwright q={candidate!r} → empty result")
            except Exception as e:
                errors.append(f"Playwright q={candidate!r} → raised: {e}")
    else:
        errors.append("Playwright not installed — skipped browser fallback")

    raise ScreenerError(
        f"No results found for '{query}' on Screener.in after trying "
        f"{len(candidates)} query variants {candidates} via JSON API and Playwright. "
        f"Details: {' || '.join(errors)}"
    )


def _search_via_playwright(query: str) -> list:
    """Has a real headless browser navigate directly to the JSON API URL
    and reads the response body. This is a real browser request (passes
    TLS/JS fingerprint checks a raw `requests` call can't) but avoids all
    the fragility of simulating clicks/typing into an on-page search box
    (which can be hidden depending on viewport/responsive breakpoint)."""
    url = f"{BASE_URL}/api/company/search/?q={quote(query)}&v=3"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.set_default_timeout(15000)
        resp = page.goto(url, wait_until="domcontentloaded")
        body_text = resp.text() if resp else ""
        browser.close()

    if not body_text:
        return []
    try:
        data = json.loads(body_text)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data.get("results"):
        return data["results"]
    return []


def _best_match(query: str, results: list) -> dict | None:
    """Pick closest name match — avoids getting subsidiaries/ETFs first."""
    if not results:
        return None
    q = query.strip().lower()
    for r in results:
        if (r.get("name") or "").strip().lower() == q:
            return r
    for r in results:
        if q in (r.get("name") or "").strip().lower():
            return r
    return results[0]


# ── Page fetch ────────────────────────────────────────────────────────────────

def _fetch_html(url_path: str) -> str:
    s = _get_session()
    full_url = f"{BASE_URL}{url_path}" if url_path.startswith("/") else url_path
    try:
        r = s.get(full_url, timeout=TIMEOUT)
        r.raise_for_status()
        # A 200 with a tiny/near-empty body is usually a bot-check
        # interstitial, not the real page — fall through to Playwright.
        if len(r.text) > 2000:
            return r.text
        logger.warning(
            "[screener_service] %s returned suspiciously short body (%d chars), "
            "trying Playwright fallback", full_url, len(r.text)
        )
    except Exception as e:
        logger.warning("[screener_service] requests fetch of %s failed: %s", full_url, e)

    if PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.set_default_timeout(20000)
                page.goto(full_url, wait_until="domcontentloaded")
                page.wait_for_timeout(800)
                html = page.content()
                browser.close()
            if len(html) > 2000:
                return html
            raise ScreenerError(f"Playwright fetch of {url_path} also returned an empty/short page")
        except Exception as e:
            raise ScreenerError(f"Could not fetch {url_path} (requests and Playwright both failed): {e}")

    raise ScreenerError(f"Could not fetch {url_path} and Playwright is not installed for fallback")


# ── Table parsing ─────────────────────────────────────────────────────────────

def _clean_label(cell) -> str:
    text = cell.get_text(" ", strip=True)
    text = re.sub(r"\s*\+\s*$", "", text)
    return text.strip()


def _parse_section_table(soup: BeautifulSoup, section_id: str) -> dict:
    """
    Parses one <section id="..."> financial table.
    Returns {'years': [...], 'rows': [{'particular': str, 'values': {year: val}}]}
    Returns empty structure (not exception) if section not found.
    """
    section = soup.find(id=section_id)
    if not section:
        return {"years": [], "rows": []}

    table = section.find("table")
    if not table:
        return {"years": [], "rows": []}

    # ── Header row → year labels ──────────────────────────────────────────────
    years = []
    thead = table.find("thead")
    if thead:
        header_cells = thead.find_all("th")
        years = [_clean_label(th) for th in header_cells[1:]]

    # ── Body rows ─────────────────────────────────────────────────────────────
    rows = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        particular = _clean_label(cells[0])
        if not particular:
            continue
        values = {}
        for i, year in enumerate(years):
            if i + 1 < len(cells):
                values[year] = cells[i + 1].get_text(" ", strip=True)
        rows.append({"particular": particular, "values": values})

    return {"years": years, "rows": rows}


def _parse_company_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    return {
        "profit_loss":    _parse_section_table(soup, "profit-loss"),
        "balance_sheet":  _parse_section_table(soup, "balance-sheet"),
        "cash_flow":      _parse_section_table(soup, "cash-flow"),
        "ratios":         _parse_section_table(soup, "ratios"),
        "quarterly":      _parse_section_table(soup, "quarters"),
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def get_company_financials(company_name: str) -> dict:
    """
    Given a free-text company name, finds the best Screener.in match
    and returns consolidated + standalone financials.

    Returns:
    {
      "matched_name": str,
      "screener_id": str|int,
      "screener_url": str,
      "consolidated": {profit_loss, balance_sheet, cash_flow, ratios, quarterly} | None,
      "standalone":   {same} | None,
    }
    """
    results = search_company(company_name)
    match = _best_match(company_name, results)
    if not match:
        raise ScreenerError(f"No Screener.in match found for '{company_name}'")

    # Build URLs
    raw_url = match.get("url") or ""
    if not raw_url:
        slug = match.get("id", "")
        raw_url = f"/company/{slug}/consolidated/"

    # Ensure consolidated URL has the right suffix
    if not raw_url.rstrip("/").endswith("consolidated"):
        consolidated_url = raw_url.rstrip("/") + "/consolidated/"
    else:
        consolidated_url = raw_url

    standalone_url = consolidated_url.replace("/consolidated/", "/")

    out = {
        "matched_name": match.get("name"),
        "screener_id":  match.get("id"),
        "screener_url": f"{BASE_URL}{consolidated_url}",
        "consolidated": None,
        "standalone":   None,
    }

    fetch_errors = []

    # ── Fetch consolidated ────────────────────────────────────────────────────
    try:
        html = _fetch_html(consolidated_url)
        data = _parse_company_page(html)
        if any(data[k]["rows"] for k in data):
            out["consolidated"] = data
        else:
            fetch_errors.append(f"consolidated page fetched ({len(html)} chars) but no known table sections matched")
    except ScreenerError as e:
        fetch_errors.append(f"consolidated: {e}")

    # ── Fetch standalone ──────────────────────────────────────────────────────
    try:
        html = _fetch_html(standalone_url)
        data = _parse_company_page(html)
        if any(data[k]["rows"] for k in data):
            out["standalone"] = data
        else:
            fetch_errors.append(f"standalone page fetched ({len(html)} chars) but no known table sections matched")
    except ScreenerError as e:
        fetch_errors.append(f"standalone: {e}")

    if not out["consolidated"] and not out["standalone"]:
        logger.warning("[screener_service] financials fetch failed for '%s': %s",
                        match.get("name"), " || ".join(fetch_errors))
        raise ScreenerError(
            f"Found '{match.get('name')}' on Screener.in but could not parse "
            f"any financial tables. Details: {' || '.join(fetch_errors)}"
        )

    return out