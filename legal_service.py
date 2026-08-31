"""
legal_service.py

Two-source legal intelligence:
  1. Google News RSS  — free, no key, excellent Indian news coverage
  2. GNews API        — used if GNEWS_API_KEY is set (adds more results)
  3. Indian Kanoon    — court records scraping

Google News RSS is the primary source since it consistently returns
results for Indian companies where GNews free tier often returns 0.
"""

import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

GNEWS_SEARCH_URL = "https://gnews.io/api/v4/search"

LEGAL_KEYWORDS = [
    "lawsuit", "litigation", "court", "fraud", "SEBI", "penalty",
    "FIR", "CBI", "ED", "tribunal", "NCLT", "NCLAT", "dispute",
    "legal notice", "investigation", "insolvency", "bankruptcy",
    "arrest", "chargesheet", "scam", "violation", "fine", "default",
    "show cause", "money laundering", "contempt", "PIL", "arbitration",
    "regulatory", "probe", "raid", "cheating", "forgery",
]

IK_BASE       = "https://indiankanoon.org"
IK_SEARCH_URL = f"{IK_BASE}/search/"
IK_HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; LegalBot/1.0)"}
RSS_HEADERS   = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── Company-name relevance scoring ────────────────────────────────────────
# The old filter only checked "does any single word (len>3) from the
# company name appear anywhere in the article" -- which is why a company
# named e.g. "TCI" matched articles about a completely different company
# like "Sun TCI Express" (the article DOES contain the word "TCI", it's
# just referring to someone else's company). This scorer is stricter:
# it requires whole-word matches, gives a much higher score to an exact
# multi-word phrase match, and actively DOWN-weights a short/ambiguous
# match when it's sitting right next to another capitalized word that
# looks like it's actually naming a different company ("Sun TCI").
_LEGAL_SUFFIXES = {"limited", "ltd", "pvt", "private", "llp", "inc", "corp", "corporation", "co", "company"}
_GENERIC_STOPWORDS = {"the", "and", "of", "for", "group", "india", "holdings", "&"}


def _core_name_tokens(name: str) -> list:
    words = re.findall(r"[A-Za-z]+", name or "")
    return [w for w in words if w.lower() not in _LEGAL_SUFFIXES and w.lower() not in _GENERIC_STOPWORDS]


def _word_positions(word: str, text: str) -> list:
    return [m.start() for m in re.finditer(r"\b" + re.escape(word) + r"\b", text, flags=re.IGNORECASE)]


def _adjacent_word(text: str, pos: int, word_len: int, direction: int):
    if direction == -1:
        before = text[:pos].rstrip()
        m = re.search(r"([A-Za-z][A-Za-z&.'-]*)\s*$", before)
    else:
        after = text[pos + word_len:].lstrip()
        m = re.match(r"([A-Za-z][A-Za-z&.'-]*)", after)
    return m.group(1) if m else None


# Common words that legitimately sit next to a company name in a headline
# ("TCI reports...", "TCI Ltd faces..."). Anything NOT in this list sitting
# directly next to a short/ambiguous match is treated as likely being part
# of a DIFFERENT company's name ("Sun TCI Express"). Deliberately does not
# rely on capitalization -- RSS article descriptions are frequently
# lowercase, which silently defeated an earlier, capitalization-based
# version of this check.
_HEADLINE_FOLLOWERS = {
    "reports", "report", "reported", "faces", "gets", "wins", "loses", "lost",
    "announces", "announced", "says", "said", "denies", "denied", "launches",
    "launched", "posts", "posted", "sees", "seen", "hits", "hit", "shares",
    "share", "stock", "stocks", "results", "earnings", "profit", "profits",
    "loss", "losses", "revenue", "net", "quarterly", "annual", "vs", "and",
    "or", "in", "on", "at", "to", "from", "with", "by", "is", "was", "are",
    "were", "under", "over", "amid", "after", "before", "following", "sebi",
    "nclt", "nclat", "court", "tribunal", "case", "lawsuit", "probe",
    "penalty", "fine", "notice", "investigation", "chargesheet", "fir",
    "cbi", "ed", "arrest", "board", "ceo", "md", "chairman", "chief", "ipo",
    "q1", "q2", "q3", "q4", "fy23", "fy24", "fy25", "fy26", "stake",
    "acquires", "acquired", "signs", "wins", "bags", "secures", "files",
    "filed", "moves", "moved", "'s",
}


def company_relevance(company_name: str, short_name: str, title: str, description: str) -> int:
    """Returns a 0-100 confidence score that an article is actually about
    THIS company, not a same/similar-named different one.

    Multi-word company names (the common case) require the exact phrase
    to appear, in order -- e.g. "Techno Electric" must appear together.
    Matching only one of the words ("Techno" alone, in an article about
    techno music or "techno-legal hurdles") is NOT accepted as evidence;
    that used to score a nonzero "low confidence" match, which is exactly
    the kind of generic-keyword false positive this function exists to
    filter out, not produce.

    Single-token company names (e.g. "TCI") have no multi-word phrase to
    require, so they fall back to an adjacency check: if the matched word
    is sitting directly next to another capitalized-looking word that
    isn't a common headline filler word (see _HEADLINE_FOLLOWERS), that's
    treated as evidence the match is actually part of a DIFFERENT
    company's name (e.g. "Sun TCI Express"), and scored low.
    """
    text = f"{title or ''} {description or ''}"
    tokens = _core_name_tokens(short_name) or _core_name_tokens(company_name)
    if not tokens:
        return 0

    if len(tokens) > 1:
        pattern = r"\b" + r"\s+".join(re.escape(t) for t in tokens) + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return 96
        # No exact phrase match -- a partial/single-word hit is too weak
        # to trust for a multi-word company name, so this article is not
        # considered a match at all.
        return 0

    # Single-token company name: adjacency-based false-positive guard.
    tok = tokens[0]
    positions = _word_positions(tok, text)
    if not positions:
        return 0
    risky = False
    for pos in positions:
        for direction in (-1, 1):
            adj = _adjacent_word(text, pos, len(tok), direction)
            if (adj and adj.lower() not in _GENERIC_STOPWORDS
                    and adj.lower() not in _LEGAL_SUFFIXES
                    and adj.lower() not in _HEADLINE_FOLLOWERS
                    and adj.lower() != tok.lower()):
                risky = True
    if risky:
        # Only ever found glued to what looks like a different company's name
        return 5
    return 82


class LegalServiceError(Exception):
    pass


# ── SOURCE 1: Google News RSS (primary — free, great India coverage) ──────────
def _fetch_gnews_rss(query: str, max_results: int = 10) -> list:
    encoded = quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, headers=RSS_HEADERS, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:max_results]:
            title  = (item.findtext("title") or "").strip()
            desc   = (item.findtext("description") or "").strip()
            link   = (item.findtext("link") or "").strip()
            pub    = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            source = src_el.text.strip() if src_el is not None else "Google News"
            # Strip HTML tags from description
            desc = re.sub(r"<[^>]+>", " ", desc).strip()
            items.append({
                "title":        title,
                "description":  desc,
                "url":          link,
                "source":       source,
                "published_at": pub,
            })
        return items
    except Exception:
        return []


# ── SOURCE 2: GNews API (supplement — needs API key) ─────────────────────────
def _fetch_gnews_api(query: str, api_key: str, max_results: int = 10) -> list:
    params = {
        "q":      query,
        "lang":   "en",
        "max":    max_results,
        "sortby": "publishedAt",
        "apikey": api_key,
    }
    try:
        r = requests.get(GNEWS_SEARCH_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return [
            {
                "title":        (a.get("title") or "").strip(),
                "description":  (a.get("description") or "").strip(),
                "url":          a.get("url"),
                "source":       (a.get("source") or {}).get("name"),
                "published_at": a.get("publishedAt"),
            }
            for a in data.get("articles", [])
        ]
    except Exception:
        return []


def get_legal_news(company_name: str, days_back: int = 365) -> dict:
    api_key    = os.getenv("GNEWS_API_KEY")
    short_name = re.sub(r'\b(limited|ltd|pvt|private|llp|inc|corp)\b', '',
                        company_name, flags=re.IGNORECASE).strip().strip(".,")
    if len(short_name) < 4:
        short_name = company_name

    # Build search queries
    # Company name is quoted so Google News RSS / GNews API do an exact
    # phrase search, not a loose match on the individual words -- without
    # this, a multi-word name like "Techno Electric" pulls in anything
    # containing "techno" OR "electric" separately (techno music news,
    # electric-vehicle news, etc.) before relevance scoring even runs.
    queries = [
        f'"{short_name}" legal case court',
        f'"{short_name}" SEBI fraud penalty',
        f'"{short_name}" FIR investigation ED CBI',
        f'"{short_name}" lawsuit dispute tribunal',
    ]

    raw_articles = []
    rss_count = 0

    # PRIMARY: Google News RSS (always run)
    for q in queries:
        got = _fetch_gnews_rss(q, max_results=8)
        rss_count += len(got)
        raw_articles.extend(got)

    # SUPPLEMENT: GNews API (if key available)
    gnews_count = 0
    if api_key:
        for q in queries[:2]:  # limit to 2 queries to save API quota
            got = _fetch_gnews_api(q, api_key, max_results=5)
            gnews_count += len(got)
            raw_articles.extend(got)

    # Deduplicate by title
    seen, articles = set(), []
    n_duplicate, n_no_match, n_low_conf = 0, 0, 0
    for a in raw_articles:
        title = a.get("title", "").strip()
        if not title or title in seen:
            if title:
                n_duplicate += 1
            continue
        seen.add(title)

        desc = a.get("description") or ""

        # Strict company-relevance scoring (see company_relevance() above) --
        # replaces the old "any single name word appears anywhere" check,
        # which is what let a different, similarly-named company's news
        # through (e.g. company "TCI" matching articles about "Sun TCI").
        match_pct = company_relevance(company_name, short_name, title, desc)
        if match_pct <= 0:
            n_no_match += 1
            continue

        combined = f"{title} {desc}".lower()
        matched_kw = [kw for kw in LEGAL_KEYWORDS if kw.lower() in combined]
        relevance = "relevant" if match_pct >= 40 else "low_confidence"
        if relevance == "low_confidence":
            n_low_conf += 1

        articles.append({
            "title":            title,
            "description":      desc,
            "url":              a.get("url"),
            "source":           a.get("source"),
            "published_at":     a.get("published_at"),
            "matched_keywords": matched_kw,
            "company_match_pct": match_pct,
            "relevance":        relevance,
            "risk_level": (
                "high"   if len(matched_kw) >= 3 else
                "medium" if len(matched_kw) >= 1 else
                "low"
            ),
        })

    articles.sort(key=lambda a: a["company_match_pct"], reverse=True)
    debug = {
        "queries_used": queries,
        "raw_from_rss": rss_count,
        "raw_from_gnews_api": gnews_count,
        "gnews_api_key_set": bool(api_key),
        "dropped_as_duplicate": n_duplicate,
        "dropped_as_no_match": n_no_match,
        "kept_low_confidence": n_low_conf,
        "kept_relevant": len(articles) - n_low_conf,
    }
    return {"company": company_name, "count": len(articles), "articles": articles, "debug": debug}


# ── SOURCE 3: Indian Kanoon ───────────────────────────────────────────────────
def _ik_get(url, params=None, retries=2):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=IK_HEADERS, timeout=15)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def _clean_case_text(text: str, dedupe_prefix: str = None) -> str:
    """Cleans extracted Indian Kanoon text: strips any leftover tag-like
    fragments (defensive -- guards against a source page embedding raw
    markup as literal text, which showed up as visible '</div>' strings
    in the UI), collapses whitespace/newlines to single spaces, and
    optionally removes a leading duplicate of the case title (Indian
    Kanoon's snippet typically repeats "Title on Date" as its own first
    line, which is redundant once the UI already shows the title as the
    card heading)."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if dedupe_prefix:
        prefix = re.sub(r"\s+", " ", dedupe_prefix).strip().lower()
        check_len = min(len(prefix), 60)
        if prefix and text.lower().startswith(prefix[:check_len]):
            text = text[check_len:].lstrip(" .-:")
    return text


def _norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _recover_spaced_title(raw_title: str, snippet_raw: str) -> str:
    """Indian Kanoon's title field sometimes has no space characters at
    all between party names (a source-data quirk, not something a
    separator argument on get_text() can fix). The snippet field usually
    repeats the same case name as its own first line, but WITH normal
    spacing -- so if that lead-in normalizes (ignoring spaces/punctuation)
    to the exact same characters as the raw title, it's safe to use the
    properly-spaced version for display instead."""
    if not snippet_raw:
        return raw_title
    lead_match = re.match(r"^(.*?)(?:\s+Author:|\s+Bench:|\s+Case\s*:-)", snippet_raw)
    lead = lead_match.group(1).strip() if lead_match else None
    if lead and _norm_alnum(lead) == _norm_alnum(raw_title):
        return re.sub(r"\s+", " ", lead).strip()
    return raw_title


def get_court_cases(company_name: str, max_pages: int = 2) -> dict:
    results = []
    debug = {
        "source": "indiankanoon.org", "http_status": None, "html_bytes": None,
        "pages_fetched": 0, "error": None, "note": None,
    }
    for page in range(max_pages):
        try:
            r = _ik_get(IK_SEARCH_URL, {"formInput": f'"{company_name}"', "pagenum": page})
            debug["http_status"] = r.status_code
            debug["html_bytes"] = len(r.content)
            debug["pages_fetched"] += 1
            soup = BeautifulSoup(r.text, "html.parser")
            # Try the original tag-qualified selector first (in case Indian
            # Kanoon reverts), then fall back to a bare class selector --
            # CSS class selectors match the exact class TOKEN regardless of
            # tag name, so ".result" is safe (won't accidentally also match
            # "result_title" or "results-list", those are different tokens).
            # This fixes the case where the "result" class still exists but
            # is now on a <li>/<article>/etc. instead of a <div>.
            selector_used = None
            divs = soup.select("div.result")
            if divs:
                selector_used = "div.result"
            else:
                divs = soup.select(".result")
                if divs:
                    selector_used = ".result"
            debug["selector_used"] = selector_used
            if not divs:
                if page == 0:
                    # Distinguish "genuinely zero cases" from "markup
                    # changed" automatically instead of asking the user to
                    # guess. Indian Kanoon's zero-results page has its own
                    # explicit copy for this; if that phrase ISN'T present,
                    # but the page is a normal size, the selectors are
                    # almost certainly stale rather than the search being
                    # empty. We also scan for any class attribute containing
                    # "result" so the actual current class name (if renamed)
                    # shows up directly in the debug output.
                    no_results_phrase = bool(re.search(
                        r"no matching documents found|no results found|did not match any documents",
                        r.text, flags=re.IGNORECASE,
                    ))
                    candidate_classes = sorted(set(re.findall(r'class="([^"]*result[^"]*)"', r.text, flags=re.IGNORECASE)))

                    if no_results_phrase:
                        diagnosis = "Indian Kanoon's own page explicitly says no matching documents were found -- this looks like a genuine zero, not a scraper bug."
                    elif candidate_classes:
                        diagnosis = (
                            f"The page DOES contain class names with 'result' in them "
                            f"({', '.join(candidate_classes[:8])}), but none matched the "
                            f"exact selector 'div.result' this scraper uses -- Indian Kanoon "
                            f"most likely renamed/restructured this markup. Update the CSS "
                            f"selectors in get_court_cases() to match one of the classes above."
                        )
                    else:
                        diagnosis = (
                            "No 'no results found' message AND no class name containing "
                            "'result' anywhere on the page. This suggests the page returned "
                            "isn't the search results page the scraper expects at all -- "
                            "possibly an interstitial/consent page, a redirect, or a "
                            "significantly restructured template. Inspect debug['html_snippet'] "
                            "below (or open the search URL in a real browser) to confirm."
                        )

                    debug["note"] = (
                        f"Request succeeded (HTTP {r.status_code}, {len(r.content)} bytes) but no "
                        f"'div.result' elements were found on the page. {diagnosis}"
                    )
                    debug["no_results_phrase_found"] = no_results_phrase
                    debug["candidate_result_classes_found"] = candidate_classes[:15]
                    debug["html_snippet"] = r.text[:1500]
                break
            for div in divs:
                title_tag = div.select_one("div.result_title a") or div.select_one(".result_title a")
                if not title_tag:
                    continue
                title = title_tag.get_text(" ", strip=True)
                href  = title_tag.get("href", "")
                link  = href if href.startswith("http") else IK_BASE + href
                snip_tag = (div.select_one("div.snippet") or div.select_one("div.headline")
                            or div.select_one(".snippet") or div.select_one(".headline"))
                snippet_raw = snip_tag.get_text(" ", strip=True)[:600] if snip_tag else ""
                title    = _recover_spaced_title(title, snippet_raw)
                snippet  = _clean_case_text(snippet_raw, dedupe_prefix=title)
                src_tag  = div.select_one("div.docsource") or div.select_one(".docsource")
                court    = src_tag.get_text(" ", strip=True) if src_tag else None
                dm       = re.search(r"on (\d{1,2} \w+, \d{4})", snippet_raw)
                results.append({
                    "title":   title,
                    "url":     link,
                    "court":   court,
                    "date":    dm.group(1) if dm else None,
                    "snippet": snippet,
                })
            time.sleep(1)
        except Exception as e:
            debug["error"] = f"{type(e).__name__}: {e}"
            break

    return {"company": company_name, "count": len(results), "cases": results, "debug": debug}


# ── COMBINED SUMMARY ──────────────────────────────────────────────────────────
def get_legal_summary(company_name: str, days_back: int = 365,
                       max_court_pages: int = 2) -> dict:
    news  = get_legal_news(company_name, days_back=days_back)
    cases = get_court_cases(company_name, max_pages=max_court_pages)

    relevant = [a for a in news["articles"] if a["relevance"] == "relevant"]
    low_conf = [a for a in news["articles"] if a["relevance"] == "low_confidence"]

    # Risk categorization only counts RELEVANT articles -- a low-confidence
    # match (likely a different company) shouldn't move the risk needle.
    high = [a for a in relevant if a["risk_level"] == "high"]
    med  = [a for a in relevant if a["risk_level"] == "medium"]
    low  = [a for a in relevant if a["risk_level"] == "low"]

    legal_risk_score = min(100, cases["count"] * 25 + len(high) * 20 + len(med) * 10 + len(low) * 2)
    risk_band = "low" if legal_risk_score < 34 else ("moderate" if legal_risk_score < 67 else "high")

    key_findings = []
    if med:
        top = med[0]
        kws = ", ".join(top["matched_keywords"][:3]) or "a legal/regulatory keyword"
        key_findings.append({
            "type": "warning", "icon": "⚠️",
            "title": f"{len(med)} Moderate Signal{'s' if len(med) != 1 else ''}",
            "body": f"\u201c{top['title']}\u201d mentions {kws}. Recommended for manual review.",
        })
    if high:
        top = high[0]
        kws = ", ".join(top["matched_keywords"][:3]) or "multiple legal/regulatory keywords"
        key_findings.append({
            "type": "critical", "icon": "🚨",
            "title": f"{len(high)} High-Risk Signal{'s' if len(high) != 1 else ''}",
            "body": f"\u201c{top['title']}\u201d mentions {kws}. Requires immediate review.",
        })
    if cases["count"] > 0:
        key_findings.append({
            "type": "info", "icon": "🏛️",
            "title": f"{cases['count']} Court Record{'s' if cases['count'] != 1 else ''}",
            "body": f"Found on Indian Kanoon under an exact-name search for {company_name}.",
        })
    else:
        key_findings.append({
            "type": "success", "icon": "✅",
            "title": "No Court Records",
            "body": "No direct court proceedings matched the company during the review period.",
        })
    if low_conf:
        key_findings.append({
            "type": "info", "icon": "ℹ️",
            "title": f"{len(low_conf)} Low-Relevance Mention{'s' if len(low_conf) != 1 else ''}",
            "body": "These results share a name fragment with the company but likely refer to a "
                    "different, similarly-named entity — shown for transparency, not counted in the risk score.",
        })
    if not med and not high and not cases["count"] and not low_conf:
        key_findings.append({
            "type": "success", "icon": "✅",
            "title": "No Legal Signals Found",
            "body": "No litigation, regulatory action, or adverse news matched this company in the selected period.",
        })

    signal_distribution = {
        "high": len(high), "moderate": len(med), "low": len(low),
        "court_records": cases["count"],
    }
    signal_distribution["total"] = sum(signal_distribution.values())

    return {
        "company":            company_name,
        "legal_risk_score":   legal_risk_score,
        "risk_band":          risk_band,
        "kpis": {
            "court_records":      cases["count"],
            "adverse_news_total": news["count"],
            "relevant_signals":   len(relevant),
            "low_confidence_count": len(low_conf),
            "critical_flags":     len(high),
        },
        "signal_distribution": signal_distribution,
        "key_findings":       key_findings,
        "articles":           news["articles"],
        "cases":              cases["cases"],
        # Legacy fields kept for backward compatibility with any older caller.
        "overall_risk": ("high" if high and cases["count"] > 0 else
                          "medium" if high or cases["count"] > 2 else
                          "low" if med or cases["count"] > 0 else "none_detected"),
        "news_summary":  {"total": news["count"], "high_risk": len(high), "med_risk": len(med)},
        "court_summary": {"total": cases["count"]},
        "flags": [f"{f['title']}: {f['body']}" for f in key_findings],
        "debug": {
            "news": news.get("debug", {}),
            "court_records": cases.get("debug", {}),
        },
    }