"""
app.py — AI Business Due Diligence Assistant
RAG Pipeline: Playwright Crawler → Section Chunker → sentence-transformers → ChromaDB → Groq LLM

Install dependencies:
    pip install flask flask-cors httpx beautifulsoup4 requests groq python-dotenv
    pip install playwright chromadb sentence-transformers
    pip install reportlab
    playwright install chromium

Run: python app.py
Open: http://localhost:5000
"""

import re
import os
import io
import math
import time
import json
import asyncio
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from collections import deque
from flask import Flask, render_template_string, jsonify, request, send_from_directory
from flask_cors import CORS
import warnings
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

try:
    import sys as _sys
    _sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ── Optional imports with graceful fallback ──────────────────────────────────

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    print("⚠  pip install beautifulsoup4")

try:
    import requests as req_lib
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False
    print("⚠  pip install groq")

try:
    import chromadb
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False
    print("⚠  pip install chromadb")

try:
    from sentence_transformers import SentenceTransformer
    ST_AVAILABLE = True
except ImportError:
    ST_AVAILABLE = False
    print("⚠  pip install sentence-transformers")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("⚠  pip install playwright && playwright install chromium")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Standalone scraping backends (Screener.in / NSE / PDF) ──────────────────
try:
    from services import screener_service, nse_service, pdf_extractor as pdf_extractor_service
    SCREENER_SERVICE_AVAILABLE = True
except ImportError as e:
    SCREENER_SERVICE_AVAILABLE = False
    print(f"⚠  services/ package not importable ({e}) — falling back to mock financials")

try:
    from services.legal_service import get_legal_summary, get_legal_news, get_court_cases
    LEGAL_SERVICE_AVAILABLE = True
except ImportError as e:
    LEGAL_SERVICE_AVAILABLE = False
    print(f"⚠  legal_service not importable ({e}) — legal routes will return errors")

try:
    from services import analysis_service
    ANALYSIS_SERVICE_AVAILABLE = True
except ImportError as e:
    ANALYSIS_SERVICE_AVAILABLE = False
    print(f"⚠  analysis_service not importable ({e}) — Analysis tab will return errors")

# ═══════════════════════════════════════════════════════════════════════════
# NEW — PDF report generator (reportlab-based CareEdge-style due diligence PDF)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from services.report_generator import generate_due_diligence_report
    REPORT_SERVICE_AVAILABLE = True
except ImportError as e:
    REPORT_SERVICE_AVAILABLE = False
    print(f"⚠  report_generator not importable ({e}) — PDF export will return errors")


try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("⚠  pip install yfinance")

try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False
    print("⚠  pip install pymupdf")

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False
    print("⚠  pip install pdfplumber")

# ── Configuration ─────────────────────────────────────────────────────────────

GST_API_BASE  = os.getenv("GST_API_BASE_URL", "https://sheet.gstincheck.co.in/check")
GST_API_KEY   = os.getenv("GST_API_KEY", "")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", " ")

CHROMA_DIR    = os.path.join(os.path.dirname(__file__), "chroma_store")
os.makedirs(CHROMA_DIR, exist_ok=True)

MAX_PAGES     = 25
MAX_CHUNK_LEN = 1500
MIN_CHUNK_LEN = 120
TOP_K         = 6
EMBED_MODEL   = "all-MiniLM-L6-v2"

groq_client = None
if GROQ_AVAILABLE and GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)

embed_model = None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
CORS(app)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — GST VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

GST_REGEX = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$')

def validate_gstin(gstin):
    return bool(GST_REGEX.match(gstin.strip().upper()))

def _flatten_address(pradr):
    addr = pradr.get("addr", {})
    parts = [addr.get("bnm",""), addr.get("st",""), addr.get("loc",""),
             addr.get("dst",""), addr.get("stcd",""), addr.get("pncd","")]
    return ", ".join(p for p in parts if p)

def _parse_directors(raw):
    return [{"name": d.get("nm",""), "din": d.get("din",""), "designation": d.get("dsg","")} for d in raw]

def _parse_filing_status(raw):
    out = []
    for f in raw:
        rt = f.get("ret_typ","")
        out.append({"period": f.get("period",""), "type": rt.replace("GSTR","GSTR-") if "GSTR" in rt else rt,
                    "status": f.get("status",""), "date": f.get("dof","")})
    return out

def _parse_gst_response(raw, gstin):
    if not raw.get("flag", False):
        return {"success": False, "gstin": gstin, "error": raw.get("message","Invalid GSTIN or API error")}
    d = raw.get("data", {})
    return {
        "success": True, "gstin": gstin, "legal_name": d.get("lgnm",""), "trade_name": d.get("tradeNam",""),
        "reg_date": d.get("rgdt",""), "status": d.get("sts",""),
        "state": d.get("pradr",{}).get("addr",{}).get("stcd",""), "state_code": gstin[:2],
        "address": _flatten_address(d.get("pradr",{})), "business_type": d.get("ctb",""),
        "pan": gstin[2:12] if len(gstin) >= 12 else "", "email": d.get("email",""), "phone": d.get("phone",""),
        "directors": _parse_directors(d.get("directors",[])),
        "filings": _parse_filing_status(d.get("filingStatus",[])),
        "hsn_codes": d.get("hsnsac",[]), "_mock": False,
    }

def _mock_gst_response(gstin):
    return {
        "success": True, "gstin": gstin, "legal_name": "DEMO TECHNOLOGIES PRIVATE LIMITED",
        "trade_name": "DEMO TECH", "reg_date": "01/04/2015", "status": "Active",
        "state": "Maharashtra", "state_code": gstin[:2],
        "address": "Plot 42, Industrial Area, Mumbai, Maharashtra, 400001",
        "business_type": "Private Limited Company", "pan": gstin[2:12] if len(gstin)>=12 else "AAAAA0000A",
        "email": "", "phone": "",
        "directors": [{"name": "RAHUL SHARMA", "din": "01234567", "designation": "Director"}],
        "filings": [{"period": "Mar 2024", "type": "GSTR-3B", "status": "Filed", "date": ""}],
        "hsn_codes": ["8471","8473"], "_mock": True,
    }

async def _verify_gstin_async(gstin):
    gstin = gstin.strip().upper()
    if not GST_API_KEY or not HTTPX_AVAILABLE:
        return _mock_gst_response(gstin)
    url = f"{GST_API_BASE}/{GST_API_KEY}/{gstin}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return _parse_gst_response(resp.json(), gstin)
    except Exception as e:
        return {"success": False, "gstin": gstin, "error": f"API error: {str(e)}"}

def verify_gst(gstin):
    if not gstin.strip():
        return {"success": False, "error": "GSTIN is required"}
    if not validate_gstin(gstin):
        return {"success": False, "error": "Invalid GSTIN format"}
    return asyncio.run(_verify_gstin_async(gstin))

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1B — FINANCIALS
# ═══════════════════════════════════════════════════════════════════════════════

if SCREENER_SERVICE_AVAILABLE:
    ScreenerError = screener_service.ScreenerError
else:
    class ScreenerError(Exception):
        pass

def get_company_financials(company_name):
    if not SCREENER_SERVICE_AVAILABLE:
        raise ScreenerError("services.screener_service not installed (missing requests/bs4/lxml?)")
    result = screener_service.get_company_financials(company_name)
    result["success"] = True
    return result

SCREENER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def _mock_screener_financials(company_name):
    years = ["Mar 2021", "Mar 2022", "Mar 2023", "Mar 2024", "Mar 2025"]
    def row(name, base, growth):
        return {"particular": name, "values": {y: f"{base * (growth ** i):,.0f}" for i, y in enumerate(years)}}
    pl = {"years": years, "rows": [row("Revenue", 1200, 1.14), row("Net Profit", 140, 1.18), row("EBITDA", 260, 1.15)]}
    bs = {"years": years, "rows": [row("Total Assets", 2400, 1.12), row("Total Liabilities", 900, 1.08), row("Net Worth", 1500, 1.15)]}
    cf = {"years": years, "rows": [row("Cash from Operations", 210, 1.12)]}
    ratios = {"years": years, "rows": [row("ROE %", 16, 1.02), row("Debt to Equity", 0.4, 0.97)]}
    return {"success": True, "matched_name": company_name or "ACME TECHNOLOGIES PRIVATE LIMITED",
            "screener_id": "mock", "screener_url": "", "consolidated": {
                "profit_loss": pl, "balance_sheet": bs, "cash_flow": cf, "ratios": ratios, "quarterly": {"years": [], "rows": []}},
            "standalone": None, "_mock": True}


VALID_YF_PERIODS = nse_service.VALID_PERIODS if SCREENER_SERVICE_AVAILABLE else \
    {"1d","5d","1mo","3mo","6mo","1y","2y","5y","10y","ytd","max"}

def get_nse_quote(symbol, period="6mo"):
    if period not in VALID_YF_PERIODS:
        period = "6mo"
    if not YFINANCE_AVAILABLE or not SCREENER_SERVICE_AVAILABLE:
        return _mock_nse_quote(symbol, period)

    try:
        raw_info = nse_service._run_nse_quote(symbol, period=period)
    except Exception as e:
        return {"success": False, "error": str(e)}

    parsed = nse_service.parse_quote_summary(raw_info)

    return {
        "success":       True,
        "symbol":        parsed.get("symbol") or symbol.upper().replace(".NS", ""),
        "company_name":  parsed.get("company_name") or symbol,
        "sector":        parsed.get("sector"),
        "industry":      parsed.get("industry"),
        "price":         parsed.get("last_price"),
        "change":        parsed.get("change"),
        "change_pct":    parsed.get("pct_change"),
        "prev_close":    parsed.get("prev_close"),
        "week52_high":   parsed.get("week52_high"),
        "week52_low":    parsed.get("week52_low"),
        "volume":        parsed.get("volume"),
        "market_cap_cr": parsed.get("market_cap_cr"),
        "pe_ratio":      parsed.get("pe_ratio"),
        "pb_ratio":      parsed.get("pb_ratio"),
        "eps":           parsed.get("eps"),
        "beta":          parsed.get("beta"),
        "price_history": parsed.get("price_history", []),
        "period":        period,
        "_mock":         False,
    }

def _mock_nse_quote(symbol, period="6mo"):
    clean = symbol.upper().replace(".NS", "").replace(".BO", "").strip() or "ACME"
    n_points = {"1d":1,"5d":5,"1mo":22,"3mo":63,"6mo":126,"1y":252,"2y":260,"5y":260,"10y":260,"ytd":150,"max":260}.get(period, 126)
    import random
    random.seed(hash(clean) % (2**31))
    price, hist = 2500.0, []
    from datetime import date, timedelta
    today = date.today()
    for i in range(n_points):
        price *= (1 + random.uniform(-0.018, 0.02))
        d = today - timedelta(days=(n_points - i))
        o = price * (1 + random.uniform(-0.006, 0.006))
        hi = max(o, price) * (1 + random.uniform(0, 0.008))
        lo = min(o, price) * (1 - random.uniform(0, 0.008))
        hist.append({"date": str(d), "open": round(o,2), "high": round(hi,2), "low": round(lo,2),
                     "close": round(price,2), "volume": random.randint(400000, 2200000)})
    last = hist[-1]["close"] if hist else 2840.55
    prev = hist[-2]["close"] if len(hist) > 1 else last * 0.99
    return {"success": True, "symbol": clean, "company_name": f"{clean.title()} Ltd",
            "sector": "Technology", "industry": "IT Services", "price": last, "change": round(last-prev,2),
            "change_pct": round((last-prev)/prev*100, 2) if prev else 0, "prev_close": round(prev,2),
            "week52_high": round(max(h["high"] for h in hist), 2) if hist else 3120.00,
            "week52_low": round(min(h["low"] for h in hist), 2) if hist else 2210.00,
            "volume": hist[-1]["volume"] if hist else 1834210, "market_cap_cr": 105230.5, "pe_ratio": 27.8, "pb_ratio": 6.2,
            "eps": 102.3, "beta": 0.85, "price_history": hist, "period": period, "_mock": True}


PDF_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(PDF_UPLOAD_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# NEW — output dir for generated due-diligence PDF reports
# ═══════════════════════════════════════════════════════════════════════════
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "generated_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


def extract_pdf_text(file_path, max_pages=60):
    if not FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) not installed — run: pip install pymupdf")
    if not SCREENER_SERVICE_AVAILABLE:
        raise RuntimeError("services.pdf_extractor not importable")

    text = pdf_extractor_service.extract_pdf_text(file_path, max_pages=max_pages)
    pages_analyzed = text.count("[Page ")
    return text, pages_analyzed

FIN_ANALYSIS_PROMPT = """You are a credit analyst reviewing a company's financial statements
for a due-diligence/creditworthiness assessment. Below are excerpts extracted from an
annual report / balance sheet PDF.

{context}

Return ONLY a JSON object (no markdown, no explanation) with this exact structure:
{{
    "report_type": "e.g. Annual Report / Balance Sheet / Quarterly Result",
    "report_period": "e.g. FY 2023-24",
    "key_financials": {{
        "revenue":    {{"value": "e.g. ₹1,240 Cr", "yoy_change": "e.g. +12.4%"}},
        "net_profit": {{"value": "...", "yoy_change": "..."}},
        "ebitda":     {{"value": "...", "yoy_change": "..."}},
        "net_worth":  {{"value": "...", "yoy_change": "..."}}
    }},
    "key_ratios": {{"roe": "...", "roce": "...", "debt_equity": "...", "current_ratio": "...", "ebitda_margin": "...", "eps": "..."}},
    "year_wise_data": [
        {{"year": "FY22", "revenue": "...", "net_profit": "...", "total_assets": "..."}}
    ],
    "creditworthiness_summary": "2-3 sentence assessment of whether this company appears creditworthy",
    "highlights": ["notable positive point 1", "notable positive point 2"],
    "risk_factors": ["notable risk or red flag 1", "notable risk or red flag 2"]
}}

Use only information present in the excerpts. If a field is genuinely not found, use "Not found" (or [] for arrays)."""

def analyze_pdf_financials(pdf_text, company_name=""):
    if not groq_client:
        return {"success": False, "error": "Groq API not configured"}
    if not pdf_text.strip():
        return {"success": False, "error": "No text could be extracted from this PDF"}

    prompt = FIN_ANALYSIS_PROMPT.format(context=pdf_text[:28000])
    try:
        resp = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.05, max_tokens=3000,
        )
        raw_text = resp.choices[0].message.content or ""
        clean = re.sub(r"```json\s*", "", raw_text.strip())
        clean = re.sub(r"```\s*", "", clean)
        start, end = clean.find("{"), clean.rfind("}")
        if start == -1 or end == -1:
            return {"success": False, "error": "LLM did not return valid JSON"}
        data = json.loads(clean[start:end+1])
        data["success"] = True
        data["company_name"] = company_name
        return data
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Groq error: {e}"}

def analyze_financial_pdf(file_path, company_name=""):
    try:
        text, pages_analyzed = extract_pdf_text(file_path)
    except Exception as e:
        return {"success": False, "error": f"PDF extraction failed: {e}"}

    result = analyze_pdf_financials(text, company_name)
    if result.get("success"):
        result["pages_analyzed"] = pages_analyzed
    return result


# ── (d) Website crawler for annual-report / balance-sheet links ─────────────

ANNUAL_KW = ["annual report", "annual-report", "annual_report", "ar 20",
             "integrated report", "annual results", "annual accounts",
             "audited results", "q4 annual", "full year"]
BALANCE_KW = ["balance sheet", "balance-sheet", "balance_sheet",
              "financial statement", "standalone financial",
              "financial result", "quarterly result"]
IR_NAV_KW = ["investor", "financials", "annual report", "annual-report",
             "financial result", "investor relation", "reports"]
COMMON_IR_PATHS = ["/investors", "/investor-relations", "/investor_relations",
                    "/ir", "/annual-reports", "/financials",
                    "/investors/financials", "/investors/annual-reports",
                    "/investor/financials", "/investor/reports"]
YEAR_RE = re.compile(r"20\d{2}")

GENERIC_LINK_TEXTS = {
    "download pdf", "download", "pdf", "view", "view pdf", "click here",
    "read more", "more", "link", "report", "download report",
    "download annual report", "annual report pdf", "here",
}

def _classify_link(text, href):
    blob = f"{text} {href}".lower()
    if any(k in blob for k in ANNUAL_KW):
        return "Annual Report"
    if any(k in blob for k in BALANCE_KW):
        return "Financial Statement"
    return "Financial Document"

def _derive_link_label(text, href):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    year_m = YEAR_RE.search(f"{text} {href}")
    year = year_m.group() if year_m else None
    is_generic = (not text) or (text.lower() in GENERIC_LINK_TEXTS) or (len(text) <= 4)

    if not is_generic:
        label = text
        if year and year not in label:
            label = f"{label} ({year})"
        return label[:90]

    category = _classify_link(text, href)
    if year:
        return f"{category} {year}"

    fname = os.path.splitext(os.path.basename(urlparse(href).path))[0]
    fname = re.sub(r"[_\-]+", " ", fname).strip()
    if fname and fname.lower() not in GENERIC_LINK_TEXTS and len(fname) > 3:
        return fname.title()[:90]

    return category

def _report_link_score(text, href, keywords):
    blob = f"{text} {href}".lower()
    s = sum(2 for k in keywords if k in blob)
    if not s:
        return 0
    m = YEAR_RE.search(blob)
    if m:
        y = int(m.group())
        if y >= 2023: s += 3
        elif y >= 2021: s += 1
    if href.lower().endswith(".pdf"):
        s += 2
    return s

def _crawler_fetch_html(url, timeout=12):
    if not REQUESTS_AVAILABLE:
        return None
    try:
        r = req_lib.get(url, headers=SCREENER_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None

def _crawler_extract_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        text = (tag.get_text() or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        full = urljoin(base_url, href)
        blob = f"{text} {href}".lower()
        if href.lower().endswith(".pdf") or any(k in blob for k in ANNUAL_KW + BALANCE_KW):
            links.append({"text": _derive_link_label(text, full), "href": full})
    return links

def _crawler_ir_subpages(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    domain = urlparse(base_url).netloc
    seen, result = set(), []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).netloc != domain:
            continue
        blob = f"{tag.get_text() or ''} {href}".lower()
        if any(k in blob for k in IR_NAV_KW) and full not in seen:
            seen.add(full)
            result.append(full)
        if len(result) >= 10:
            break
    return result

def _crawler_stage1_requests(start_url):
    parsed = urlparse(start_url)
    domain_root = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [start_url]
    if parsed.path.rstrip("/") in ("", "/"):
        candidates += [urljoin(domain_root, p) for p in COMMON_IR_PATHS]

    visited = set()
    for url in candidates:
        if url in visited:
            continue
        visited.add(url)
        html = _crawler_fetch_html(url)
        if not html:
            continue
        links = _crawler_extract_links(html, url)
        if links:
            return links, url
        for sub in _crawler_ir_subpages(html, url):
            if sub in visited:
                continue
            visited.add(sub)
            sub_html = _crawler_fetch_html(sub)
            if not sub_html:
                continue
            sub_links = _crawler_extract_links(sub_html, sub)
            if sub_links:
                return sub_links, sub
    return [], start_url

def _crawler_stage2_playwright(start_url):
    if not PLAYWRIGHT_AVAILABLE:
        return [], start_url
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(20000)
            page.goto(start_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            html = page.content()
            browser.close()
        return _crawler_extract_links(html, start_url), start_url
    except Exception as e:
        logger.warning(f"Playwright fallback crawl failed: {e}")
        return [], start_url

def find_report_links(start_url):
    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url

    links, page_used = _crawler_stage1_requests(start_url)
    if not links:
        links, page_used = _crawler_stage2_playwright(start_url)

    if not links:
        return {"success": True, "report_links": [], "page_used": page_used,
                "note": "No financial document links found on the site. Try manual PDF upload instead."}

    ar = [l for l in links if _report_link_score(l["text"], l["href"], ANNUAL_KW) > 0]
    bs = [l for l in links if _report_link_score(l["text"], l["href"], BALANCE_KW) > 0]
    seen_urls, deduped = set(), []
    for l in (ar + bs):
        if l["href"] not in seen_urls:
            seen_urls.add(l["href"])
            deduped.append(l)
    deduped = deduped[:10]

    label_counts = {}
    for l in deduped:
        label_counts[l["text"]] = label_counts.get(l["text"], 0) + 1
    seen_so_far = {}
    for l in deduped:
        if label_counts[l["text"]] > 1:
            seen_so_far[l["text"]] = seen_so_far.get(l["text"], 0) + 1
            l["text"] = f'{l["text"]} #{seen_so_far[l["text"]]}'

    return {"success": True, "report_links": deduped, "page_used": page_used}

def download_pdf(url, dest_path):
    r = req_lib.get(url, headers=SCREENER_HEADERS, timeout=30, stream=True)
    r.raise_for_status()
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return dest_path

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — DEEP PLAYWRIGHT CRAWLER
# ═══════════════════════════════════════════════════════════════════════════════

SKIP_EXTENSIONS = {".pdf",".jpg",".jpeg",".png",".gif",".svg",".ico",".css",".js",
                   ".zip",".mp4",".mp3",".woff",".woff2",".ttf",".eot"}
SKIP_PATTERNS   = ["login","logout","signup","register","cart","checkout",
                   "privacy","terms","cookie","sitemap.xml","feed","rss"]

def _should_skip_url(url, base_domain):
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != base_domain:
        return True
    path = parsed.path.lower()
    ext  = os.path.splitext(path)[1]
    if ext in SKIP_EXTENSIONS:
        return True
    if any(pat in path for pat in SKIP_PATTERNS):
        return True
    return False

def _normalise_url(url, base):
    url = url.split("#")[0].rstrip("/")
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        p = urlparse(base)
        url = f"{p.scheme}://{p.netloc}{url}"
    elif not url.startswith("http"):
        url = urljoin(base, url)
    return url

def crawl_website(start_url, max_pages=MAX_PAGES):
    if not PLAYWRIGHT_AVAILABLE or not BS4_AVAILABLE:
        return {"success": False, "error": "Playwright or BeautifulSoup not installed"}

    if not start_url.startswith(("http://","https://")):
        start_url = "https://" + start_url

    base_domain  = urlparse(start_url).netloc
    visited      = set()
    queue        = deque([start_url])
    pages        = []

    print(f"\n[CRAWLER] Starting BFS crawl: {start_url}")
    print(f"[CRAWLER] Domain: {base_domain} | Max pages: {max_pages}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.set_default_timeout(20000)

            while queue and len(pages) < max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)

                try:
                    print(f"[CRAWLER] Fetching ({len(pages)+1}/{max_pages}): {url}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1200)

                    html  = page.content()
                    title = page.title()

                    links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    for link in links:
                        norm = _normalise_url(link, url)
                        if norm and norm not in visited and not _should_skip_url(norm, base_domain):
                            queue.append(norm)

                    pages.append({"url": url, "title": title, "html": html})
                    print(f"[CRAWLER]   ✓ Got {len(html)} chars, found {len(links)} links")

                except Exception as e:
                    print(f"[CRAWLER]   ✗ Skipped {url}: {e}")
                    continue

            browser.close()

    except Exception as e:
        return {"success": False, "error": f"Playwright error: {str(e)}"}

    print(f"[CRAWLER] Done. Crawled {len(pages)} pages.")
    return {"success": True, "pages": pages, "domain": base_domain}

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — SECTION-BASED HTML CHUNKER
# ═══════════════════════════════════════════════════════════════════════════════

HEADING_TAGS = ["h1","h2","h3","h4"]
NOISE_TAGS   = ["script","style","nav","footer","noscript","iframe",
                 "svg","meta","link","header","aside","form","button","input"]

def _clean_text(raw):
    clean = ""
    for ch in raw:
        if ch.isprintable():
            clean += ch
        else:
            clean += " "
    return " ".join(clean.split())

def chunk_page_by_sections(html, page_url, page_title):
    if not BS4_AVAILABLE:
        return []

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(NOISE_TAGS):
        tag.decompose()

    body = soup.find("body") or soup
    nodes = []
    for el in body.descendants:
        if not hasattr(el, "name"):
            continue
        if el.name in HEADING_TAGS:
            text = _clean_text(el.get_text())
            if text:
                nodes.append(("heading", el.name, text))
        elif el.name in ("p","li","td","th","blockquote","dd","dt","span","div"):
            if not el.find(["p","div","ul","ol","table"]):
                text = _clean_text(el.get_text())
                if len(text) > 30:
                    nodes.append(("text", el.name, text))

    chunks = []
    current_heading  = page_title or "Introduction"
    current_path     = [current_heading]
    current_texts    = []

    def flush_chunk():
        combined = " ".join(current_texts).strip()
        if len(combined) >= MIN_CHUNK_LEN:
            if len(combined) <= MAX_CHUNK_LEN:
                chunks.append({
                    "heading":      current_heading,
                    "section_path": " > ".join(current_path),
                    "text":         combined,
                    "url":          page_url,
                    "page_title":   page_title or "",
                })
            else:
                sentences = re.split(r'(?<=[.!?])\s+', combined)
                buf = ""
                for s in sentences:
                    if len(buf) + len(s) > MAX_CHUNK_LEN and len(buf) >= MIN_CHUNK_LEN:
                        chunks.append({
                            "heading":      current_heading,
                            "section_path": " > ".join(current_path),
                            "text":         buf.strip(),
                            "url":          page_url,
                            "page_title":   page_title or "",
                        })
                        buf = s + " "
                    else:
                        buf += s + " "
                if len(buf.strip()) >= MIN_CHUNK_LEN:
                    chunks.append({
                        "heading":      current_heading,
                        "section_path": " > ".join(current_path),
                        "text":         buf.strip(),
                        "url":          page_url,
                        "page_title":   page_title or "",
                    })

    for kind, tag, text in nodes:
        if kind == "heading":
            flush_chunk()
            current_texts   = []
            current_heading = text
            level = int(tag[1])
            current_path = current_path[:level-1] + [text]
        else:
            current_texts.append(text)

    flush_chunk()
    return chunks

def chunk_all_pages(pages):
    all_chunks = []
    for page in pages:
        page_chunks = chunk_page_by_sections(page["html"], page["url"], page["title"])
        all_chunks.extend(page_chunks)
        print(f"[CHUNKER] {page['url']} → {len(page_chunks)} chunks")
    print(f"[CHUNKER] Total: {len(all_chunks)} chunks across {len(pages)} pages")
    return all_chunks

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — EMBEDDINGS (sentence-transformers)
# ═══════════════════════════════════════════════════════════════════════════════

def get_embed_model():
    global embed_model
    if embed_model is None:
        if not ST_AVAILABLE:
            raise RuntimeError("sentence-transformers not installed")
        print(f"[EMBED] Loading model: {EMBED_MODEL} ...")
        embed_model = SentenceTransformer(EMBED_MODEL)
        print("[EMBED] Model ready.")
    return embed_model

def embed_texts(texts):
    model = get_embed_model()
    vecs  = model.encode(texts, show_progress_bar=False, batch_size=32)
    return vecs.tolist()

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — CHROMADB VECTOR STORE
# ═══════════════════════════════════════════════════════════════════════════════

def _domain_to_collection(domain):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", domain)
    return safe[:60] or "default"

def get_chroma_client():
    if not CHROMA_AVAILABLE:
        raise RuntimeError("chromadb not installed")
    return chromadb.PersistentClient(path=CHROMA_DIR)

def is_domain_indexed(domain):
    try:
        client     = get_chroma_client()
        coll_name  = _domain_to_collection(domain)
        existing   = [c.name for c in client.list_collections()]
        if coll_name not in existing:
            return False, 0
        coll  = client.get_collection(coll_name)
        count = coll.count()
        return count > 0, count
    except Exception:
        return False, 0

def store_chunks(domain, chunks, embeddings):
    client    = get_chroma_client()
    coll_name = _domain_to_collection(domain)

    try:
        client.delete_collection(coll_name)
    except Exception:
        pass

    coll = client.create_collection(
        name=coll_name,
        metadata={"hnsw:space": "cosine"},
    )

    ids       = [hashlib.md5(f"{domain}_{i}".encode()).hexdigest() for i in range(len(chunks))]
    documents = [c["text"] for c in chunks]
    metadatas = [{
        "heading":      c.get("heading",""),
        "section_path": c.get("section_path",""),
        "url":          c.get("url",""),
        "page_title":   c.get("page_title",""),
    } for c in chunks]

    BATCH = 500
    for i in range(0, len(ids), BATCH):
        coll.add(
            ids=ids[i:i+BATCH],
            embeddings=embeddings[i:i+BATCH],
            documents=documents[i:i+BATCH],
            metadatas=metadatas[i:i+BATCH],
        )

    print(f"[CHROMA] Stored {len(ids)} chunks in collection '{coll_name}'")
    return len(ids)

def retrieve_chunks(domain, query, n_results=TOP_K):
    client    = get_chroma_client()
    coll_name = _domain_to_collection(domain)
    coll      = client.get_collection(coll_name)

    query_vec = embed_texts([query])[0]
    results   = coll.query(
        query_embeddings=[query_vec],
        n_results=min(n_results, coll.count()),
    )

    chunks = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        chunks.append({
            "text":         doc,
            "heading":      meta.get("heading",""),
            "section_path": meta.get("section_path",""),
            "url":          meta.get("url",""),
        })
    return chunks

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — RAG EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

RAG_QUERIES = [
    ("overview",     "company overview description what does this company do industry sector"),
    ("founding",     "founded year established history milestones headquarters location"),
    ("leadership",   "founders directors CEO managing director leadership management team"),
    ("products",     "products services offerings solutions what they provide sell"),
    ("contact",      "contact address phone email office location registered"),
    ("highlights",   "achievements awards recognition certifications clients customers partners turnover revenue"),
]

def _build_context_from_retrieval(domain):
    seen_texts = set()
    context_blocks = []

    for label, query in RAG_QUERIES:
        try:
            chunks = retrieve_chunks(domain, query, n_results=TOP_K)
        except Exception as e:
            print(f"[RAG] Query '{label}' failed: {e}")
            continue

        section_chunks = []
        for c in chunks:
            key = c["text"][:80]
            if key not in seen_texts:
                seen_texts.add(key)
                section_chunks.append(c)

        if section_chunks:
            block = f"### {label.upper()}\n"
            for c in section_chunks:
                block += f"[{c['section_path']} | {c['url']}]\n{c['text']}\n\n"
            context_blocks.append(block)

    return "\n".join(context_blocks)

EXTRACTION_PROMPT = """You are a business analyst performing due diligence on a company.
Below are relevant excerpts retrieved from the company's website, organized by topic.
Extract structured information and return ONLY a JSON object — no markdown, no explanation.

{context}

Return this exact JSON structure:
{{
    "company_overview": {{
        "name": "official company name",
        "description": "2-3 sentence description of what the company does",
        "industry": "primary industry/sector",
        "founded_year": "year or 'Not found'",
        "headquarters": "city, state/country or 'Not found'",
        "tagline": "slogan or tagline if present, else empty string",
        "employee_count": "number or range if mentioned, else empty string"
    }},
    "products_services": {{
        "main_offerings": ["product or service 1", "product or service 2"],
        "usp": "what makes them unique, their key differentiator",
        "target_markets": ["market segment 1", "market segment 2"]
    }},
    "leadership_team": [
        {{"name": "full name", "designation": "title/role"}}
    ],
    "contact_information": {{
        "registered_office": "full address if found",
        "phone": ["phone numbers"],
        "email": ["email addresses"],
        "website": "main website url"
    }},
    "key_highlights": [
        "notable achievement, award, or fact 1",
        "notable achievement, award, or fact 2",
        "notable achievement, award, or fact 3"
    ],
    "clients_partners": {{
        "notable_clients": ["client 1", "client 2"],
        "partners": ["partner 1", "partner 2"]
    }},
    "awards_recognition": ["award 1", "award 2"],
    "extraction_confidence": "High"
}}

Use only information present in the excerpts above. If a field is genuinely not found, use empty string or empty array."""

def extract_from_rag(domain, company_name=""):
    if not groq_client:
        return {"success": False, "error": "Groq API not configured"}

    print(f"\n[RAG] Building context for domain: {domain}")
    context = _build_context_from_retrieval(domain)

    if not context.strip():
        return {"success": False, "error": "No content retrieved from vector store"}

    print(f"[RAG] Context built: {len(context)} chars across {len(RAG_QUERIES)} query types")

    prompt = EXTRACTION_PROMPT.format(context=context[:28000])

    try:
        print("[RAG] Calling Groq for synthesis...")
        resp = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.05,
            max_tokens=3000,
        )
        raw_text = resp.choices[0].message.content or ""

        clean = raw_text.strip()
        clean = re.sub(r"```json\s*", "", clean)
        clean = re.sub(r"```\s*", "", clean)
        start = clean.find("{")
        end   = clean.rfind("}")
        if start == -1 or end == -1:
            return {"success": False, "error": "LLM did not return valid JSON"}
        clean = clean[start:end+1]

        data = json.loads(clean)
        data["success"]              = True
        data["domain"]               = domain
        data["extraction_timestamp"] = datetime.now().isoformat()
        data["rag_context_chars"]    = len(context)
        print("[RAG] Extraction complete.")
        return data

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Groq error: {e}"}

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6B — CONVERSATIONAL Q&A
# ═══════════════════════════════════════════════════════════════════════════════

QA_SYSTEM_PROMPT = """You are a business due-diligence assistant answering questions about a specific company.
You are given excerpts retrieved from the company's official website. Answer the user's question
using ONLY the information in these excerpts.

Rules:
- Be concise, factual, and professional.
- If the answer is not present in the excerpts, clearly say: "I couldn't find that information on the company's website."
- Do not invent facts, numbers, names, or dates.
- When helpful, mention the section/page the information came from.
- Format lists with bullet points when appropriate.
"""

def answer_question(domain, question, n_results=8):
    if not groq_client:
        return {"success": False, "error": "Groq API not configured"}

    if not question or not question.strip():
        return {"success": False, "error": "Question is required"}

    try:
        chunks = retrieve_chunks(domain, question, n_results=n_results)
    except Exception as e:
        return {"success": False, "error": f"Retrieval error: {e}"}

    if not chunks:
        return {"success": False, "error": "No relevant content found in the vector store"}

    context_blocks = []
    sources = []
    for c in chunks:
        context_blocks.append(
            f"[Source: {c.get('section_path','')} | {c.get('url','')}]\n{c['text']}"
        )
        src = {"section_path": c.get("section_path", ""), "url": c.get("url", "")}
        if src not in sources:
            sources.append(src)

    context = "\n\n".join(context_blocks)[:24000]

    user_prompt = f"""Company website excerpts:

{context}

---
User question: {question}

Answer the question using only the excerpts above."""

    try:
        resp = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": QA_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=900,
        )
        answer = (resp.choices[0].message.content or "").strip()
        return {
            "success": True,
            "question": question,
            "answer": answer,
            "sources": sources[:5],
            "chunks_used": len(chunks),
            "domain": domain,
        }
    except Exception as e:
        return {"success": False, "error": f"Groq error: {e}"}


def ask_company_question(url, question):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    domain = urlparse(url).netloc

    indexed, count = is_domain_indexed(domain)
    if not indexed:
        return {"success": False, "error": f"Domain '{domain}' not indexed yet. Run indexing first."}

    return answer_question(domain, question)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — MAIN PIPELINE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

def index_company_website(url, force_reindex=False):
    if not url.startswith(("http://","https://")):
        url = "https://" + url

    domain = urlparse(url).netloc

    if not force_reindex:
        indexed, count = is_domain_indexed(domain)
        if indexed:
            print(f"[PIPELINE] Domain already indexed: {domain} ({count} chunks)")
            return {"success": True, "domain": domain, "pages_crawled": 0,
                    "chunks_stored": count, "already_cached": True}

    crawl_result = crawl_website(url)
    if not crawl_result.get("success"):
        return {"success": False, "error": crawl_result.get("error")}

    pages = crawl_result["pages"]
    if not pages:
        return {"success": False, "error": "No pages crawled"}

    chunks = chunk_all_pages(pages)
    if not chunks:
        return {"success": False, "error": "No content chunks extracted"}

    print(f"[PIPELINE] Embedding {len(chunks)} chunks...")
    texts      = [c["text"] for c in chunks]
    embeddings = embed_texts(texts)
    print(f"[PIPELINE] Embeddings done: {len(embeddings)} vectors")

    stored = store_chunks(domain, chunks, embeddings)

    return {
        "success":       True,
        "domain":        domain,
        "pages_crawled": len(pages),
        "chunks_stored": stored,
        "already_cached": False,
        "page_urls":     [p["url"] for p in pages],
    }

def extract_company_info_rag(url, company_name=""):
    if not url.startswith(("http://","https://")):
        url = "https://" + url
    domain = urlparse(url).netloc

    indexed, count = is_domain_indexed(domain)
    if not indexed:
        return {"success": False, "error": f"Domain '{domain}' not indexed yet. Run indexing first."}

    result = extract_from_rag(domain, company_name)
    if result.get("success"):
        result["website_url"]   = url
        result["chunks_in_db"]  = count
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Due Diligence Assistant</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --navy-dark: #081C4B;
            --blue-primary: #2563EB;
            --purple-primary: #7C3AED;
            --green-primary: #16a34a;
            --orange-primary: #ea580c;
            --gradient-primary: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%);
            --gradient-green: linear-gradient(135deg, #16a34a 0%, #15803d 100%);
            --gradient-orange: linear-gradient(135deg, #ea580c 0%, #c2410c 100%);
            --glass-bg: rgba(255,255,255,0.95);
            --shadow-soft: 0 8px 32px rgba(8,28,75,0.10);
            --shadow-hover: 0 20px 60px rgba(8,28,75,0.18);
            --radius-lg: 20px;
            --radius-sm: 12px;
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:'Poppins',sans-serif; background:#f1f5fb; color:#1a1a2e; line-height:1.6; }
        .navbar-enterprise { background:var(--navy-dark); padding:.75rem 0; box-shadow:0 4px 20px rgba(8,28,75,.3); position:sticky; top:0; z-index:1000; }
        .navbar-brand-custom { display:flex; align-items:center; gap:12px; color:#fff !important; font-weight:700; font-size:1.2rem; text-decoration:none; }
        .brand-logo { width:40px; height:40px; background:var(--gradient-primary); border-radius:var(--radius-sm); display:flex; align-items:center; justify-content:center; font-size:1.1rem; color:white; }
        .nav-link-custom { color:rgba(255,255,255,.8) !important; font-weight:500; font-size:.85rem; padding:.5rem 1rem !important; border-radius:var(--radius-sm); transition:all .3s; cursor:pointer; border:none; background:none; }
        .nav-link-custom:hover,.nav-link-custom.active { color:#fff !important; background:rgba(255,255,255,.1); }
        .nav-link-custom:disabled { opacity:.4; cursor:not-allowed; }
        .page-section { display:none; min-height:calc(100vh - 60px); padding:2rem 0; }
        .page-section.active { display:block; }
        .hero-mini { text-align:center; padding:2rem 0 1rem; }
        .hero-mini h1 { font-size:2.2rem; font-weight:800; color:var(--navy-dark); margin-bottom:.5rem; }
        .hero-mini h1 span { background:var(--gradient-primary); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .hero-mini p { color:#64748b; font-size:1rem; }
        .cards-container { display:grid; grid-template-columns:repeat(auto-fit,minmax(380px,1fr)); gap:2rem; max-width:920px; margin:0 auto; padding:1rem; }
        .module-card { background:var(--glass-bg); border-radius:var(--radius-lg); padding:2rem; box-shadow:var(--shadow-soft); position:relative; overflow:hidden; transition:all .3s; }
        .module-card:hover { box-shadow:var(--shadow-hover); transform:translateY(-4px); }
        .module-card::before { content:''; position:absolute; top:0; left:0; right:0; height:4px; }
        .module-card.gst::before    { background:var(--gradient-green); }
        .module-card.website::before { background:var(--gradient-primary); }
        .card-header-custom { display:flex; align-items:center; gap:12px; margin-bottom:1.5rem; }
        .card-icon { width:50px; height:50px; border-radius:var(--radius-sm); display:flex; align-items:center; justify-content:center; font-size:1.3rem; color:white; }
        .card-icon.green  { background:var(--gradient-green); }
        .card-icon.blue   { background:var(--gradient-primary); }
        .card-header-custom h3 { font-size:1.2rem; font-weight:700; color:var(--navy-dark); margin:0; }
        .card-header-custom p  { font-size:.8rem; color:#64748b; margin:0; }
        .form-group-custom { margin-bottom:1rem; }
        .form-group-custom label { display:block; font-weight:600; font-size:.8rem; color:#334155; margin-bottom:.4rem; }
        .form-control-custom { width:100%; padding:.75rem 1rem; border:2px solid #e2e8f0; border-radius:var(--radius-sm); font-family:'Poppins',sans-serif; font-size:.9rem; transition:all .3s; background:#f8fafc; }
        .form-control-custom:focus { outline:none; border-color:var(--blue-primary); background:#fff; box-shadow:0 0 0 4px rgba(37,99,235,.1); }
        .btn-module { width:100%; padding:.8rem 1.5rem; border:none; border-radius:var(--radius-sm); color:white; font-family:'Poppins',sans-serif; font-weight:600; font-size:.9rem; cursor:pointer; transition:all .3s; display:flex; align-items:center; justify-content:center; gap:8px; }
        .btn-module.green  { background:var(--gradient-green);   box-shadow:0 4px 15px rgba(22,163,74,.35); }
        .btn-module.blue   { background:var(--gradient-primary);  box-shadow:0 4px 15px rgba(37,99,235,.35); }
        .btn-module.orange { background:var(--gradient-orange);   box-shadow:0 4px 15px rgba(234,88,12,.35); }
        .btn-module:hover:not(:disabled) { transform:translateY(-2px); }
        .btn-module:disabled { opacity:.65; cursor:not-allowed; transform:none; }
        .btn-sm-outline { padding:.4rem 1rem; background:transparent; border:1.5px solid var(--blue-primary); color:var(--blue-primary); border-radius:8px; font-size:.8rem; font-weight:600; cursor:pointer; transition:all .3s; }
        .btn-sm-outline:hover { background:var(--blue-primary); color:white; }
        .alert-custom { padding:.7rem 1rem; border-radius:var(--radius-sm); margin-bottom:1rem; display:none; font-size:.85rem; }
        .alert-custom.show { display:block; }
        .alert-success { background:rgba(34,197,94,.1); color:#16a34a; border:1px solid rgba(34,197,94,.3); }
        .alert-error   { background:rgba(239,68,68,.1);  color:#dc2626; border:1px solid rgba(239,68,68,.3); }
        .alert-info    { background:rgba(37,99,235,.1);  color:#1d4ed8; border:1px solid rgba(37,99,235,.3); }
        .pipeline-steps { display:flex; flex-direction:column; gap:.5rem; margin:1rem 0; }
        .pipeline-step { display:flex; align-items:center; gap:10px; padding:.5rem .75rem; border-radius:8px; font-size:.82rem; background:#f8fafc; border:1px solid #e2e8f0; transition:all .3s; }
        .pipeline-step .step-icon { width:24px; height:24px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:.7rem; flex-shrink:0; background:#e2e8f0; color:#94a3b8; }
        .pipeline-step.active  .step-icon { background:#2563EB; color:white; animation:pulse 1.2s infinite; }
        .pipeline-step.done    .step-icon { background:#16a34a; color:white; }
        .pipeline-step.active  { border-color:#2563EB; background:rgba(37,99,235,.06); }
        .pipeline-step.done    { border-color:#16a34a; background:rgba(22,163,74,.06); }
        .step-label { font-weight:500; color:#475569; }
        .pipeline-step.active .step-label { color:#1d4ed8; }
        .pipeline-step.done   .step-label { color:#15803d; }
        .step-detail { font-size:.75rem; color:#94a3b8; margin-left:auto; }
        .cache-badge { display:inline-flex; align-items:center; gap:5px; padding:.25rem .65rem; border-radius:12px; font-size:.72rem; font-weight:600; background:rgba(22,163,74,.12); color:#16a34a; }
        .results-section { margin-top:1.5rem; display:none; }
        .results-section.show { display:block; animation:fadeInUp .4s ease; }
        .results-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem; padding-bottom:.75rem; border-bottom:2px solid #e2e8f0; }
        .results-header h4 { font-size:1rem; font-weight:700; color:var(--navy-dark); display:flex; align-items:center; gap:8px; }
        .btn-clear { padding:.4rem .8rem; background:#f1f5f9; border:1px solid #e2e8f0; border-radius:6px; color:#64748b; font-size:.75rem; cursor:pointer; }
        .btn-clear:hover { background:#e2e8f0; }
        .company-banner { border-radius:var(--radius-sm); padding:1.25rem; color:white; margin-bottom:1rem; }
        .company-banner.green { background:var(--gradient-green); }
        .company-banner.blue  { background:var(--gradient-primary); }
        .company-banner h5 { font-size:1.1rem; font-weight:700; margin-bottom:.25rem; }
        .company-banner .trade { font-size:.85rem; opacity:.9; }
        .company-banner .meta { display:flex; flex-wrap:wrap; gap:1rem; margin-top:.75rem; font-size:.8rem; }
        .info-grid-mini { display:grid; grid-template-columns:repeat(2,1fr); gap:.75rem; }
        .info-item-mini { background:#f8fafc; border-radius:8px; padding:.75rem; border-left:3px solid var(--blue-primary); }
        .info-item-mini label { font-size:.65rem; color:#64748b; text-transform:uppercase; display:block; margin-bottom:.2rem; }
        .info-item-mini .value { font-size:.85rem; font-weight:600; color:var(--navy-dark); }
        .info-item-mini.success { border-left-color:#22c55e; }
        .status-badge { display:inline-flex; align-items:center; gap:4px; padding:.2rem .6rem; border-radius:12px; font-size:.7rem; font-weight:600; }
        .status-badge.active   { background:rgba(34,197,94,.15); color:#16a34a; }
        .status-badge.inactive { background:rgba(239,68,68,.15); color:#dc2626; }
        .fin-tabs { display:flex; gap:.5rem; margin-bottom:1.25rem; flex-wrap:wrap; border-bottom:2px solid #e2e8f0; padding-bottom:.5rem; }
        .fin-tab { padding:.6rem 1.1rem; background:transparent; border:none; border-radius:var(--radius-sm); color:#64748b; font-family:'Poppins',sans-serif; font-weight:600; font-size:.85rem; cursor:pointer; display:flex; align-items:center; gap:6px; transition:all .2s; }
        .fin-tab:hover { background:#f1f5f9; color:var(--navy-dark); }
        .fin-tab.active { background:var(--gradient-primary); color:white; }
        .fin-panel { display:none; }
        .fin-panel.active { display:block; animation:fadeInUp .3s ease; }
        .fin-table-wrap { overflow-x:auto; margin-bottom:1rem; }
        table.fin-table { width:100%; border-collapse:collapse; font-size:.82rem; }
        table.fin-table th, table.fin-table td { padding:.55rem .75rem; text-align:right; border-bottom:1px solid #f1f5f9; white-space:nowrap; }
        table.fin-table th:first-child, table.fin-table td:first-child { text-align:left; font-weight:600; color:#334155; white-space:normal; }
        table.fin-table thead th { background:#f8fafc; color:#64748b; font-size:.72rem; text-transform:uppercase; font-weight:700; }
        table.fin-table tbody tr:hover { background:#f8fafc; }
        .fin-section-title { font-size:.95rem; font-weight:700; color:var(--navy-dark); margin:1.25rem 0 .5rem; display:flex; align-items:center; gap:8px; }
        .report-link-row { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:.6rem .9rem; background:#f8fafc; border-radius:8px; margin-bottom:.5rem; font-size:.85rem; }
        .report-link-row a { color:var(--blue-primary); text-decoration:none; font-weight:600; word-break:break-all; }
        .metric-mini-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.75rem; margin-bottom:1rem; }
        .risk-pill { display:inline-flex; align-items:center; gap:6px; padding:.4rem .7rem; border-radius:8px; font-size:.8rem; margin-bottom:.4rem; width:100%; }
        .risk-pill.good { background:rgba(34,197,94,.1); color:#166534; }
        .risk-pill.bad  { background:rgba(239,68,68,.1); color:#991b1b; }
        .fin2-searchbar { background:white; border-radius:var(--radius-lg); padding:1rem 1.25rem; box-shadow:var(--shadow-soft); margin-bottom:1rem; display:flex; gap:.75rem; flex-wrap:wrap; align-items:flex-end; }
        .fin2-searchbar .fin2-field { flex:1; min-width:180px; }
        .fin2-searchbar label { display:block; font-size:.72rem; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:.03em; margin-bottom:.3rem; }
        .fin2-searchbar input { width:100%; padding:.6rem .8rem; border:1.5px solid #e2e8f0; border-radius:10px; font-family:'Poppins',sans-serif; font-size:.85rem; background:#f8fafc; }
        .fin2-searchbar input:focus { outline:none; border-color:var(--blue-primary); background:#fff; }
        .fin2-analyze-btn { padding:.65rem 1.5rem; border:none; border-radius:10px; background:var(--gradient-primary); color:white; font-weight:700; font-size:.85rem; cursor:pointer; white-space:nowrap; box-shadow:0 4px 15px rgba(37,99,235,.3); }
        .fin2-analyze-btn:disabled { opacity:.6; cursor:not-allowed; }
        .fin2-hero { display:none; background:linear-gradient(120deg,#182b73 0%,#2d4fc4 55%,#3a5be0 100%); border-radius:18px; padding:1.5rem 1.75rem; color:white; box-shadow:var(--shadow-soft); position:relative; overflow:hidden; margin-bottom:1rem; }
        .fin2-hero.show { display:flex; flex-wrap:wrap; gap:1.25rem; justify-content:space-between; align-items:center; animation:fadeInUp .4s ease; }
        .fin2-hero::after { content:''; position:absolute; right:-60px; bottom:-60px; width:220px; height:220px; background:rgba(255,255,255,.08); border-radius:50%; filter:blur(2px); }
        .fin2-hero-left { display:flex; align-items:center; gap:1.1rem; z-index:1; }
        .fin2-hero-icon { width:56px; height:56px; background:white; border-radius:16px; display:flex; align-items:center; justify-content:center; font-size:1.4rem; color:#2d4fc4; flex-shrink:0; }
        .fin2-hero-name { font-size:1.35rem; font-weight:800; margin-bottom:.4rem; }
        .fin2-hero-meta { display:flex; flex-wrap:wrap; gap:1rem; font-size:.78rem; color:#dbe4ff; }
        .fin2-hero-meta span { display:flex; align-items:center; gap:.4rem; }
        .fin2-hero-right { display:flex; flex-direction:column; align-items:flex-end; gap:.5rem; z-index:1; }
        .fin2-confidence { display:inline-flex; align-items:center; gap:6px; padding:.3rem .8rem; border-radius:20px; font-size:.72rem; font-weight:700; background:rgba(16,185,129,.18); border:1px solid rgba(52,211,153,.4); color:#a7f3d0; }
        .fin2-confidence.mock { background:rgba(251,191,36,.18); border-color:rgba(251,191,36,.4); color:#fde68a; }
        .fin2-fy-pill { background:rgba(30,41,59,.55); border:1px solid rgba(255,255,255,.2); color:white; font-size:.75rem; font-weight:600; padding:.4rem .9rem; border-radius:8px; }
        .fin2-kpi-grid { display:none; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.7rem; margin-bottom:1rem; }
        .fin2-kpi-grid.show { display:grid; animation:fadeInUp .4s ease; }
        .fin2-kpi-card { background:white; border-radius:14px; padding:.85rem 1rem; border:1px solid #eef1f6; box-shadow:0 1px 3px rgba(15,23,42,.04); display:flex; align-items:center; gap:.75rem; }
        .fin2-kpi-icon { width:38px; height:38px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:.85rem; flex-shrink:0; }
        .fin2-kpi-label { font-size:.68rem; color:#64748b; font-weight:600; }
        .fin2-kpi-value { font-size:.95rem; font-weight:800; color:#0f172a; line-height:1.25; }
        .fin2-kpi-delta { font-size:.68rem; font-weight:700; display:flex; align-items:center; gap:3px; margin-top:1px; }
        .fin2-kpi-delta.up { color:#059669; }
        .fin2-kpi-delta.down { color:#dc2626; }
        .fin2-kpi-delta.flat { color:#94a3b8; }
        .fin2-tabs-row { display:none; align-items:center; justify-content:space-between; border-bottom:1px solid #e2e8f0; margin-bottom:1rem; }
        .fin2-tabs-row.show { display:flex; animation:fadeInUp .4s ease; }
        .fin2-tabs { display:flex; gap:1.6rem; overflow-x:auto; }
        .fin2-tab { background:none; border:none; padding:.15rem .1rem .75rem; font-size:.8rem; font-weight:700; color:#64748b; cursor:pointer; border-bottom:2px solid transparent; white-space:nowrap; }
        .fin2-tab:hover { color:#1e293b; }
        .fin2-tab.active { color:var(--blue-primary); border-bottom-color:var(--blue-primary); }
        .fin2-nse-link { border:1px solid #93c5fd; color:var(--blue-primary); background:none; padding:.35rem .8rem; border-radius:8px; font-size:.75rem; font-weight:600; cursor:pointer; margin-bottom:.5rem; text-decoration:none; display:inline-flex; align-items:center; gap:6px; }
        .fin2-nse-link:hover { background:#eff6ff; }
        .fin2-layout { display:none; grid-template-columns:1fr; gap:1rem; }
        .fin2-layout.show { display:grid; animation:fadeInUp .45s ease; }
        @media(min-width:1200px){ .fin2-layout { grid-template-columns:2.5fr 1fr; align-items:start; } }
        .fin2-panel { display:none; }
        .fin2-panel.active { display:block; }
        .fin2-card { background:white; border-radius:14px; padding:1.1rem; border:1px solid #e6eaf1; box-shadow:0 1px 3px rgba(15,23,42,.04); margin-bottom:1rem; }
        .fin2-card-title { display:flex; align-items:center; gap:.4rem; font-size:.8rem; font-weight:800; color:#1e293b; margin-bottom:.75rem; }
        .fin2-card-title i.fa-circle-question { color:#cbd5e1; font-size:.7rem; }
        .fin2-upper-grid { display:grid; grid-template-columns:1fr; gap:1rem; margin-bottom:1rem; }
        @media(min-width:900px){ .fin2-upper-grid { grid-template-columns:5fr 7fr; } }
        table.fin2-table { width:100%; border-collapse:collapse; font-size:.74rem; }
        table.fin2-table th, table.fin2-table td { padding:.4rem .5rem; text-align:center; white-space:nowrap; }
        table.fin2-table th:first-child, table.fin2-table td:first-child { text-align:left; white-space:normal; }
        table.fin2-table thead th { background:#f8fafc; color:#64748b; font-weight:700; border-top:1px solid #f1f5f9; border-bottom:1px solid #f1f5f9; }
        table.fin2-table tbody td { border-bottom:1px solid #f8fafc; color:#334155; }
        table.fin2-table tbody tr td:first-child { font-weight:600; }
        table.fin2-table tbody tr.bold td { font-weight:800; color:#0f172a; }
        .fin2-view-more { border:1px solid #93c5fd; color:var(--blue-primary); background:none; padding:.4rem .9rem; border-radius:8px; font-size:.72rem; font-weight:700; cursor:pointer; margin-top:.75rem; }
        .fin2-view-more:hover { background:#eff6ff; }
        .fin2-chart-toolbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:.4rem; }
        .fin2-period-group { display:flex; background:#f1f5f9; border-radius:8px; padding:2px; font-size:.7rem; font-weight:700; color:#64748b; }
        .fin2-period-btn { padding:.25rem .55rem; border-radius:6px; border:none; background:none; cursor:pointer; color:inherit; }
        .fin2-period-btn.active { background:var(--blue-primary); color:white; }
        .fin2-chart-price-row { display:flex; align-items:center; gap:.6rem; font-size:.78rem; margin-bottom:.3rem; }
        .fin2-chart-footer { display:grid; grid-template-columns:repeat(3,1fr); gap:.5rem; border-top:1px solid #f1f5f9; padding-top:.6rem; margin-top:.5rem; }
        @media(min-width:600px){ .fin2-chart-footer { grid-template-columns:repeat(6,1fr); } }
        .fin2-chart-footer .lbl { display:block; font-size:.65rem; color:#94a3b8; font-weight:600; }
        .fin2-chart-footer .val { display:block; font-size:.78rem; font-weight:800; color:#0f172a; }
        .fin2-bs-section-label { display:block; font-size:.65rem; font-weight:800; color:var(--blue-primary); text-transform:uppercase; letter-spacing:.04em; margin:.6rem 0 .3rem; }
        .fin2-bs-row { display:flex; justify-content:space-between; padding:.35rem 0; border-bottom:1px solid #f8fafc; font-size:.78rem; }
        .fin2-bs-row.total { font-weight:800; color:#0f172a; border-top:1px solid #e2e8f0; border-bottom:none; padding-top:.5rem; }
        .fin2-side-upload { border:2px dashed #bfdbfe; border-radius:14px; background:rgba(219,234,254,.15); padding:1.4rem 1rem; text-align:center; }
        .fin2-side-upload i.cloud { width:42px; height:42px; border-radius:50%; background:var(--blue-primary); color:white; display:flex; align-items:center; justify-content:center; font-size:1.1rem; margin:0 auto .5rem; }
        .fin2-choose-btn { background:var(--blue-primary); color:white; border:none; padding:.45rem 1.3rem; border-radius:8px; font-size:.75rem; font-weight:700; cursor:pointer; }
        .fin2-choose-btn:hover { background:#1d4ed8; }
        .fin2-upload-status { margin-top:.75rem; padding:.4rem .7rem; border-radius:8px; font-size:.72rem; font-weight:700; text-align:center; background:#f1f5f9; color:#64748b; }
        .fin2-upload-status.ready { background:rgba(34,197,94,.12); color:#15803d; border:1px solid rgba(34,197,94,.3); }
        .fin2-upload-status.busy { background:rgba(37,99,235,.1); color:#1d4ed8; border:1px solid rgba(37,99,235,.3); }
        .fin2-side-row { display:flex; justify-content:space-between; padding:.4rem 0; border-bottom:1px solid #f1f5f9; font-size:.78rem; }
        .fin2-side-row:last-child { border-bottom:none; }
        .fin2-doc-item { display:flex; align-items:center; justify-content:space-between; gap:.5rem; padding:.4rem 0; }
        .fin2-doc-icon { width:28px; height:28px; border-radius:6px; background:#fff1f2; color:#e11d48; border:1px solid #fecdd3; display:flex; align-items:center; justify-content:center; font-size:.8rem; flex-shrink:0; }
        .fin2-doc-name { font-size:.75rem; font-weight:700; color:#1e293b; }
        .fin2-doc-open { color:var(--blue-primary); border:1px solid #93c5fd; padding:.25rem .6rem; border-radius:6px; font-size:.68rem; font-weight:700; text-decoration:none; }
        .fin2-doc-open:hover { background:#eff6ff; }
        .fin2-ai-box { display:none; }
        .fin2-ai-box.show { display:block; animation:fadeInUp .4s ease; }
        .fin2-empty-state { background:white; border-radius:16px; border:2px dashed #e2e8f0; padding:3.5rem 2rem; text-align:center; color:#94a3b8; }
        .fin2-empty-state i { font-size:2.4rem; margin-bottom:.75rem; color:#cbd5e1; }
        .fin2-3col-grid { display:grid; grid-template-columns:1fr; gap:1rem; }
        @media(min-width:900px){ .fin2-3col-grid { grid-template-columns:repeat(3,1fr) !important; } }
        .company-page-header { background:var(--gradient-primary); border-radius:var(--radius-lg); padding:2rem; color:white; margin-bottom:1.5rem; }
        .company-page-header h1 { font-size:1.75rem; font-weight:800; margin-bottom:.3rem; }
        .company-page-header .tagline { font-size:1rem; opacity:.9; font-style:italic; }
        .company-page-header .meta { display:flex; flex-wrap:wrap; gap:1.25rem; margin-top:1rem; font-size:.85rem; }
        .company-page-header .meta-item { display:flex; align-items:center; gap:6px; }
        .info-card { background:white; border-radius:var(--radius-lg); padding:1.25rem; box-shadow:var(--shadow-soft); margin-bottom:1.25rem; }
        .info-card h3 { font-size:1.05rem; font-weight:700; color:var(--navy-dark); margin-bottom:.75rem; display:flex; align-items:center; gap:8px; }
        .info-card h3 i { color:var(--blue-primary); width:30px; height:30px; background:rgba(37,99,235,.1); border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:.9rem; }
        .info-card p { color:#475569; line-height:1.6; font-size:.9rem; }
        .info-card ul { list-style:none; padding:0; margin:0; }
        .info-card ul li { padding:.4rem 0; border-bottom:1px solid #f1f5f9; display:flex; align-items:flex-start; gap:8px; font-size:.85rem; }
        .info-card ul li:last-child { border-bottom:none; }
        .info-card ul li i { color:var(--blue-primary); margin-top:3px; min-width:14px; font-size:.8rem; }
        .highlight-box { background:linear-gradient(135deg,rgba(37,99,235,.05),rgba(124,58,237,.05)); border-radius:8px; padding:.75rem; margin-bottom:.75rem; border-left:3px solid var(--blue-primary); border-radius:0 8px 8px 0; }
        .highlight-box h5 { font-size:.75rem; color:#64748b; text-transform:uppercase; margin-bottom:.3rem; }
        .highlight-box p { font-size:.9rem; font-weight:500; color:var(--navy-dark); margin:0; }
        .team-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:.75rem; }
        .team-card { background:#f8fafc; border-radius:8px; padding:.75rem; display:flex; align-items:center; gap:.75rem; }
        .team-card .avatar { width:40px; height:40px; background:var(--gradient-primary); border-radius:50%; display:flex; align-items:center; justify-content:center; color:white; font-weight:700; font-size:1rem; flex-shrink:0; }
        .team-card .info h5 { font-size:.9rem; font-weight:600; color:var(--navy-dark); margin-bottom:.1rem; }
        .team-card .info p { font-size:.75rem; color:#64748b; margin:0; }
        .tag { display:inline-block; background:rgba(37,99,235,.1); color:var(--blue-primary); padding:.25rem .6rem; border-radius:15px; font-size:.75rem; font-weight:500; margin:.2rem; }
        .contact-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:.75rem; }
        .contact-item { display:flex; align-items:flex-start; gap:10px; }
        .contact-item i { color:var(--blue-primary); font-size:1rem; margin-top:2px; }
        .contact-item .label { font-size:.65rem; color:#64748b; text-transform:uppercase; }
        .contact-item .value { font-size:.85rem; color:var(--navy-dark); font-weight:500; }
        .contact-item .value a { color:var(--blue-primary); text-decoration:none; }
        .confidence-badge { display:inline-flex; align-items:center; gap:5px; padding:.3rem .8rem; border-radius:15px; font-size:.75rem; font-weight:600; }
        .confidence-badge.high   { background:rgba(34,197,94,.15); color:#16a34a; }
        .confidence-badge.medium { background:rgba(249,115,22,.15); color:#ea580c; }
        .rag-meta { background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px; padding:.75rem 1rem; font-size:.78rem; color:#64748b; margin-top:.5rem; }
        .rag-meta strong { color:#475569; }
        .back-btn { display:inline-flex; align-items:center; gap:6px; padding:.5rem 1rem; background:#f1f5f9; border:1px solid #e2e8f0; border-radius:8px; color:#475569; font-weight:600; font-size:.85rem; cursor:pointer; transition:all .3s; text-decoration:none; }
        .back-btn:hover { background:#e2e8f0; color:var(--navy-dark); }
        .qa-card { background:white; border-radius:var(--radius-lg); padding:1.25rem; box-shadow:var(--shadow-soft); margin-bottom:1.25rem; }
        .qa-card h3 { font-size:1.05rem; font-weight:700; color:var(--navy-dark); margin-bottom:.75rem; display:flex; align-items:center; gap:8px; }
        .qa-card h3 i { color:var(--purple-primary); width:30px; height:30px; background:rgba(124,58,237,.1); border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:.9rem; }
        .qa-chat { display:flex; flex-direction:column; gap:.85rem; max-height:480px; overflow-y:auto; padding:.5rem; margin-bottom:1rem; }
        .qa-chat:empty { display:none; }
        .qa-msg { display:flex; gap:.6rem; align-items:flex-start; animation:fadeInUp .3s ease; }
        .qa-msg .bubble { padding:.7rem 1rem; border-radius:14px; font-size:.88rem; line-height:1.55; max-width:88%; white-space:pre-wrap; }
        .qa-msg.user { flex-direction:row-reverse; }
        .qa-msg.user .bubble { background:var(--gradient-primary); color:white; border-bottom-right-radius:4px; }
        .qa-msg.bot .bubble { background:#f1f5f9; color:#1e293b; border-bottom-left-radius:4px; }
        .qa-avatar { width:32px; height:32px; border-radius:50%; display:flex; align-items:center; justify-content:center; color:white; font-size:.8rem; flex-shrink:0; }
        .qa-avatar.user { background:var(--navy-dark); }
        .qa-avatar.bot  { background:var(--gradient-primary); }
        .qa-sources { margin-top:.5rem; padding-top:.5rem; border-top:1px dashed #cbd5e1; font-size:.72rem; color:#64748b; }
        .qa-sources a { color:var(--blue-primary); text-decoration:none; display:block; margin-top:.2rem; word-break:break-all; }
        .qa-typing .bubble { background:#f1f5f9; color:#94a3b8; font-style:italic; }
        .qa-input-row { display:flex; gap:.5rem; }
        .qa-input-row input { flex:1; padding:.7rem 1rem; border:2px solid #e2e8f0; border-radius:var(--radius-sm); font-family:'Poppins',sans-serif; font-size:.88rem; background:#f8fafc; }
        .qa-input-row input:focus { outline:none; border-color:var(--purple-primary); background:#fff; box-shadow:0 0 0 4px rgba(124,58,237,.1); }
        .qa-send-btn { padding:.7rem 1.2rem; border:none; border-radius:var(--radius-sm); background:var(--gradient-primary); color:white; font-weight:600; cursor:pointer; transition:all .3s; }
        .qa-send-btn:hover:not(:disabled) { transform:translateY(-2px); }
        .qa-send-btn:disabled { opacity:.6; cursor:not-allowed; }
        .qa-suggestions { display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:.75rem; }
        .qa-chip { padding:.35rem .8rem; background:rgba(124,58,237,.08); color:var(--purple-primary); border:1px solid rgba(124,58,237,.2); border-radius:15px; font-size:.78rem; cursor:pointer; transition:all .2s; }
        .qa-chip:hover { background:var(--purple-primary); color:white; }
        .loading-overlay { position:fixed; inset:0; background:rgba(255,255,255,.96); display:none; align-items:center; justify-content:center; z-index:9999; flex-direction:column; gap:1rem; }
        .loading-overlay.show { display:flex; }
        .spinner { width:50px; height:50px; border:4px solid #e2e8f0; border-top-color:var(--blue-primary); border-radius:50%; animation:spin 1s linear infinite; }
        .loading-text    { font-size:1rem; color:#475569; text-align:center; font-weight:600; }
        .loading-subtext { font-size:.85rem; color:#94a3b8; text-align:center; }
        .footer { background:var(--navy-dark); padding:1.5rem 0; color:rgba(255,255,255,.7); margin-top:2rem; }
        .footer-content { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:1rem; }
        .footer-brand { display:flex; align-items:center; gap:8px; color:white; font-weight:600; font-size:.9rem; }
        @keyframes fadeInUp { from{opacity:0;transform:translateY(15px)} to{opacity:1;transform:translateY(0)} }
        @keyframes spin { to{transform:rotate(360deg)} }
        @keyframes pulse { 0%,100%{box-shadow:0 0 0 0 rgba(37,99,235,.4)} 50%{box-shadow:0 0 0 6px rgba(37,99,235,0)} }
        .legal-hero {
            background: linear-gradient(135deg, #2952E8 0%, #484FEA 50%, #7B3FE4 100%);
            border-radius: var(--radius-lg);
            padding: 1.75rem 2rem;
            color: white;
            position: relative;
            overflow: hidden;
            margin-bottom: 1.25rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 1rem;
        }
        .legal-hero::after {
            content: '';
            position: absolute;
            right: -40px;
            bottom: -40px;
            width: 220px;
            height: 220px;
            background: rgba(123,63,228,.25);
            border-radius: 50%;
            filter: blur(40px);
            pointer-events: none;
        }
        .legal-hero-left { display: flex; align-items: center; gap: 1.25rem; z-index: 1; }
        .legal-hero-icon {
            width: 60px; height: 60px;
            background: rgba(255,255,255,.12);
            border: 1px solid rgba(255,255,255,.2);
            border-radius: 16px;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.6rem; flex-shrink: 0;
        }
        .legal-hero h1 { font-size: 1.65rem; font-weight: 800; margin-bottom: .2rem; }
        .legal-hero p  { font-size: .85rem; opacity: .8; margin: 0; }
        .legal-live-badge {
            display: flex; align-items: center; gap: .5rem;
            padding: .4rem 1rem;
            background: rgba(255,255,255,.12);
            border: 1px solid rgba(255,255,255,.2);
            border-radius: 20px;
            font-size: .72rem; font-weight: 700; z-index: 1;
        }
        .legal-live-dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: #4ade80;
            animation: pulse 1.5s infinite;
        }
        .legal-kpi-strip {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: .75rem;
            margin-bottom: 1.25rem;
        }
        .legal-kpi-card {
            background: white;
            border-radius: 14px;
            padding: .85rem 1rem;
            border: 1px solid #e6eaf1;
            box-shadow: 0 1px 3px rgba(15,23,42,.04);
            display: flex; align-items: center; gap: .7rem;
        }
        .legal-kpi-icon {
            width: 38px; height: 38px;
            border-radius: 50%;
            display: flex; align-items: center; justify-content:center;
            font-size: .9rem; flex-shrink: 0;
        }
        .legal-kpi-label { font-size: .66rem; color: #64748b; font-weight: 600; }
        .legal-kpi-value { font-size: 1.1rem; font-weight: 800; color: #0f172a; line-height: 1.2; }
        .legal-findings { display: flex; flex-direction: column; gap: .6rem; margin-bottom: 1.25rem; }
        .legal-finding {
            display: flex; align-items: flex-start; gap: .75rem;
            padding: .85rem 1rem;
            border-radius: 12px;
            border: 1px solid transparent;
            font-size: .82rem;
        }
        .legal-finding.success { background: rgba(34,197,94,.06); border-color: rgba(34,197,94,.2); }
        .legal-finding.warning { background: rgba(245,158,11,.06); border-color: rgba(245,158,11,.2); }
        .legal-finding.critical { background: rgba(239,68,68,.06); border-color: rgba(239,68,68,.2); }
        .legal-finding.info    { background: rgba(37,99,235,.06); border-color: rgba(37,99,235,.2); }
        .legal-finding-icon { font-size: 1.1rem; flex-shrink: 0; margin-top: -.1rem; }
        .legal-finding-title { font-weight: 700; color: #0f172a; margin-bottom: .15rem; }
        .legal-finding-body  { color: #475569; line-height: 1.5; }
        .legal-columns {
            display: grid;
            grid-template-columns: 1fr;
            gap: 1.25rem;
        }
        @media(min-width:900px) { .legal-columns { grid-template-columns: 1fr 1fr; } }
        .legal-panel {
            background: white;
            border-radius: 18px;
            padding: 1.25rem;
            border: 1px solid #e6eaf1;
            box-shadow: 0 1px 3px rgba(15,23,42,.04);
            display: flex; flex-direction: column; gap: 1rem;
        }
        .legal-panel-header {
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .5rem;
        }
        .legal-panel-title {
            display: flex; align-items: center; gap: .75rem;
        }
        .legal-panel-icon {
            width: 40px; height: 40px; border-radius: 12px;
            display: flex; align-items: center; justify-content: center;
            color: white; font-size: 1rem;
        }
        .legal-panel-icon.news  { background: #2563EB; box-shadow: 0 4px 12px rgba(37,99,235,.3); }
        .legal-panel-icon.cases { background: #7C3AED; box-shadow: 0 4px 12px rgba(124,58,237,.3); }
        .legal-panel-icon h2 { font-size: 1rem; font-weight: 700; color: #0f172a; margin: 0; }
        .legal-panel-icon p  { font-size: .72rem; color: #64748b; margin: 0; }
        .legal-filter-bar {
            display: grid;
            grid-template-columns: 1fr auto;
            gap: .5rem;
        }
        .legal-search-wrap { position: relative; }
        .legal-search-wrap i { position: absolute; left: .75rem; top: 50%; transform: translateY(-50%); color: #94a3b8; font-size: .85rem; }
        .legal-search-input {
            width: 100%; padding: .55rem .75rem .55rem 2.25rem;
            border: 1.5px solid #e2e8f0; border-radius: 10px;
            font-family: 'Poppins', sans-serif; font-size: .8rem;
            background: #f8fafc; color: #1e293b;
        }
        .legal-search-input:focus { outline: none; border-color: var(--blue-primary); background: #fff; }
        .legal-date-select {
            padding: .55rem .75rem;
            border: 1.5px solid #e2e8f0; border-radius: 10px;
            font-family: 'Poppins', sans-serif; font-size: .8rem;
            background: #f8fafc; color: #475569; cursor: pointer;
        }
        .legal-date-select:focus { outline: none; border-color: var(--blue-primary); }
        .legal-chips { display: flex; gap: .4rem; flex-wrap: wrap; }
        .legal-chip {
            padding: .3rem .75rem; border-radius: 15px;
            font-size: .72rem; font-weight: 600; cursor: pointer;
            border: 1px solid transparent; transition: all .2s;
        }
        .legal-chip.active  { background: var(--blue-primary); color: white; border-color: var(--blue-primary); }
        .legal-chip.inactive { background: #f1f5f9; color: #64748b; border-color: #e2e8f0; }
        .legal-chip.inactive:hover { background: #e2e8f0; }
        .legal-news-card {
            display: flex; gap: .85rem; align-items: flex-start;
            padding: .9rem;
            border-radius: 12px;
            border: 1px solid #f1f5f9;
            background: white;
            transition: all .2s;
            cursor: pointer;
            text-decoration: none;
        }
        .legal-news-card:hover { border-color: #cbd5e1; box-shadow: 0 4px 16px rgba(15,23,42,.07); }
        .legal-news-avatar {
            width: 38px; height: 38px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: .65rem; font-weight: 900; color: white;
            flex-shrink: 0; letter-spacing: .03em;
        }
        .legal-news-title {
            font-size: .82rem; font-weight: 700; color: #0f172a;
            line-height: 1.4; margin-bottom: .25rem;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
        }
        .legal-news-desc {
            font-size: .75rem; color: #64748b; line-height: 1.5;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
            margin-bottom: .3rem;
        }
        .legal-news-meta { display: flex; align-items: center; gap: .5rem; font-size: .68rem; color: #94a3b8; font-weight: 500; }
        .legal-news-badge {
            padding: .15rem .5rem; border-radius: 10px;
            font-size: .65rem; font-weight: 700; flex-shrink: 0;
        }
        .badge-high     { background: rgba(239,68,68,.1);   color: #dc2626; }
        .badge-medium   { background: rgba(245,158,11,.1);  color: #d97706; }
        .badge-low      { background: rgba(34,197,94,.1);   color: #16a34a; }
        .badge-neutral  { background: rgba(100,116,139,.1); color: #475569; }
        .badge-relevant { background: rgba(37,99,235,.1);   color: #1d4ed8; }
        .badge-lowconf  { background: rgba(148,163,184,.1); color: #64748b; }
        .badge-biz      { background: rgba(34,197,94,.08);  color: #15803d; }
        .badge-markets  { background: rgba(124,58,237,.08); color: #7c3aed; }
        .badge-legal2   { background: rgba(239,68,68,.08);  color: #dc2626; }
        .badge-reg      { background: rgba(37,99,235,.08);  color: #1d4ed8; }
        .badge-esg      { background: rgba(6,182,212,.08);  color: #0891b2; }
        .legal-case-card {
            padding: .9rem;
            border-radius: 12px;
            border: 1px solid #f1f5f9;
            background: white;
            transition: all .2s;
        }
        .legal-case-card:hover { border-color: #cbd5e1; box-shadow: 0 4px 16px rgba(15,23,42,.07); }
        .legal-case-title {
            font-size: .82rem; font-weight: 700; color: #0f172a;
            line-height: 1.4; margin-bottom: .3rem;
        }
        .legal-case-meta { display: flex; align-items: center; gap: .5rem; font-size: .68rem; color: #94a3b8; font-weight: 500; flex-wrap: wrap; }
        .legal-case-snippet { font-size: .75rem; color: #64748b; line-height: 1.5; margin: .4rem 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .legal-case-footer { display: flex; justify-content: flex-end; margin-top: .5rem; }
        .legal-case-btn {
            display: inline-flex; align-items: center; gap: .3rem;
            font-size: .72rem; font-weight: 700; color: var(--blue-primary);
            border: 1px solid #bfdbfe; border-radius: 7px;
            padding: .3rem .65rem; text-decoration: none; background: none; cursor: pointer;
            transition: all .2s;
        }
        .legal-case-btn:hover { background: #eff6ff; }
        .court-badge {
            padding: .15rem .55rem; border-radius: 10px; font-size: .65rem; font-weight: 700;
            background: rgba(37,99,235,.08); color: #1d4ed8; border: 1px solid rgba(37,99,235,.15); flex-shrink: 0;
        }
        .legal-empty {
            text-align: center; padding: 2.5rem 1rem; color: #94a3b8;
        }
        .legal-empty i { font-size: 2rem; margin-bottom: .5rem; display: block; }
        .legal-empty p { font-size: .8rem; }
        .legal-searchbar {
            background: white;
            border-radius: var(--radius-lg);
            padding: 1rem 1.25rem;
            box-shadow: var(--shadow-soft);
            margin-bottom: 1rem;
            display: flex; gap: .75rem; flex-wrap: wrap; align-items: flex-end;
        }
        .legal-searchbar .lsb-field { flex: 1; min-width: 220px; }
        .legal-searchbar label { display: block; font-size: .72rem; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: .03em; margin-bottom: .3rem; }
        .legal-searchbar input, .legal-searchbar select {
            width: 100%; padding: .6rem .8rem;
            border: 1.5px solid #e2e8f0; border-radius: 10px;
            font-family: 'Poppins', sans-serif; font-size: .85rem;
            background: #f8fafc;
        }
        .legal-searchbar input:focus, .legal-searchbar select:focus { outline: none; border-color: var(--blue-primary); background: #fff; }
        .legal-analyze-btn {
            padding: .65rem 1.5rem; border: none; border-radius: 10px;
            background: linear-gradient(135deg, #2952E8 0%, #7B3FE4 100%);
            color: white; font-weight: 700; font-size: .85rem; cursor: pointer;
            white-space: nowrap; box-shadow: 0 4px 15px rgba(37,99,235,.3);
        }
        .legal-analyze-btn:disabled { opacity: .6; cursor: not-allowed; }
        .legal-risk-meter {
            display: flex; align-items: center; gap: .75rem;
            padding: .85rem 1.1rem;
            background: white; border-radius: 12px;
            border: 1px solid #e6eaf1; box-shadow: 0 1px 3px rgba(15,23,42,.04);
            margin-bottom: 1rem;
        }
        .legal-risk-score-circle {
            width: 52px; height: 52px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.1rem; font-weight: 900; color: white; flex-shrink: 0;
        }
        .legal-risk-score-circle.low  { background: linear-gradient(135deg,#22c55e,#16a34a); }
        .legal-risk-score-circle.moderate { background: linear-gradient(135deg,#f59e0b,#d97706); }
        .legal-risk-score-circle.high { background: linear-gradient(135deg,#ef4444,#dc2626); }
        .risk-bar-wrap { flex: 1; }
        .risk-bar-track { background: #f1f5f9; border-radius: 6px; height: 8px; overflow: hidden; margin-top: .35rem; }
        .risk-bar-fill { height: 100%; border-radius: 6px; transition: width .6s ease; }
        .risk-bar-fill.low      { background: linear-gradient(90deg,#22c55e,#16a34a); }
        .risk-bar-fill.moderate { background: linear-gradient(90deg,#f59e0b,#d97706); }
        .risk-bar-fill.high     { background: linear-gradient(90deg,#ef4444,#dc2626); }
        .an-searchbar { background:white; border-radius:var(--radius-lg); padding:1rem 1.25rem; box-shadow:var(--shadow-soft); margin-bottom:1rem; display:flex; gap:.75rem; flex-wrap:wrap; align-items:flex-end; }
        .an-searchbar .an-field { flex:1; min-width:180px; }
        .an-searchbar label { display:block; font-size:.72rem; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:.03em; margin-bottom:.3rem; }
        .an-searchbar input { width:100%; padding:.6rem .8rem; border:1.5px solid #e2e8f0; border-radius:10px; font-family:'Poppins',sans-serif; font-size:.85rem; background:#f8fafc; }
        .an-searchbar input:focus { outline:none; border-color:var(--blue-primary); background:#fff; }
        .an-run-btn { padding:.65rem 1.5rem; border:none; border-radius:10px; background:linear-gradient(135deg,#4338ca,#6d28d9); color:white; font-weight:700; font-size:.85rem; cursor:pointer; white-space:nowrap; box-shadow:0 4px 15px rgba(67,56,202,.3); display:flex; align-items:center; gap:8px; }
        .an-run-btn:disabled { opacity:.6; cursor:not-allowed; }
        .an-empty { background:white; border-radius:16px; border:2px dashed #e2e8f0; padding:3.5rem 2rem; text-align:center; color:#94a3b8; }
        .an-empty i { font-size:2.4rem; margin-bottom:.75rem; color:#cbd5e1; }
        .an-banner { background:white; border:1px solid #e6eaf1; border-radius:16px; padding:1.1rem 1.3rem; box-shadow:0 1px 3px rgba(15,23,42,.04); display:flex; flex-wrap:wrap; gap:1rem; align-items:center; justify-content:space-between; margin-bottom:1rem; }
        .an-banner-left { display:flex; align-items:center; gap:.9rem; }
        .an-banner-icon { width:44px; height:44px; border-radius:12px; background:linear-gradient(135deg,#4338ca,#6d28d9); color:white; display:flex; align-items:center; justify-content:center; font-size:1.1rem; flex-shrink:0; }
        .an-banner-name { font-size:.95rem; font-weight:800; color:#0f172a; }
        .an-banner-meta { font-size:.72rem; color:#64748b; margin-top:.15rem; }
        .an-top-grid { display:grid; grid-template-columns:1fr; gap:1rem; margin-bottom:1rem; }
        @media(min-width:700px){ .an-top-grid { grid-template-columns:repeat(2,1fr); } }
        @media(min-width:1100px){ .an-top-grid { grid-template-columns:repeat(4,1fr); } }
        .an-card { background:white; border:1px solid #e6eaf1; border-radius:16px; padding:1.25rem; box-shadow:0 1px 3px rgba(15,23,42,.04); }
        .an-card-title { font-size:.75rem; font-weight:700; color:#334155; display:flex; align-items:center; gap:6px; margin-bottom:.5rem; }
        .an-gauge-wrap { display:flex; flex-direction:column; align-items:center; justify-content:center; padding:.5rem 0; }
        .an-gauge-score { font-size:2.1rem; font-weight:800; color:#0f172a; line-height:1; }
        .an-gauge-sub { font-size:.7rem; color:#94a3b8; font-weight:600; }
        .an-band-label { text-align:center; font-weight:800; font-size:.72rem; letter-spacing:.04em; margin-top:.4rem; text-transform:uppercase; }
        .an-reco-box { border-radius:12px; padding:.8rem 1rem; display:flex; align-items:center; gap:8px; font-weight:800; font-size:.85rem; margin-bottom:.6rem; }
        .an-reco-box.green  { background:rgba(16,185,129,.08); color:#059669; border:1px solid rgba(16,185,129,.25); }
        .an-reco-box.blue   { background:rgba(37,99,235,.08); color:#1d4ed8; border:1px solid rgba(37,99,235,.25); }
        .an-reco-box.amber  { background:rgba(245,158,11,.08); color:#b45309; border:1px solid rgba(245,158,11,.25); }
        .an-reco-box.orange { background:rgba(234,88,12,.08); color:#c2410c; border:1px solid rgba(234,88,12,.25); }
        .an-reco-box.red    { background:rgba(239,68,68,.08); color:#dc2626; border:1px solid rgba(239,68,68,.25); }
        .an-legend-row { display:flex; align-items:center; justify-content:space-between; font-size:.78rem; padding:.35rem 0; }
        .an-legend-dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; }
        .an-pillar-grid { display:grid; grid-template-columns:1fr; gap:1rem; margin-bottom:1rem; }
        @media(min-width:600px){ .an-pillar-grid { grid-template-columns:repeat(2,1fr); } }
        @media(min-width:900px){ .an-pillar-grid { grid-template-columns:repeat(4,1fr); } }
        @media(min-width:1200px){ .an-pillar-grid { grid-template-columns:repeat(5,1fr); } }
        .an-pillar-card { background:white; border:1px solid #e6eaf1; border-radius:16px; padding:1.1rem; box-shadow:0 1px 3px rgba(15,23,42,.04); }
        .an-pillar-icon { width:32px; height:32px; border-radius:9px; display:flex; align-items:center; justify-content:center; font-size:.85rem; margin-bottom:.6rem; }
        .an-pillar-name { font-size:.72rem; font-weight:700; color:#334155; margin-bottom:.4rem; }
        .an-pillar-score { font-size:1.5rem; font-weight:800; }
        .an-pillar-bar { width:100%; background:#f1f5f9; border-radius:99px; height:6px; overflow:hidden; margin-top:.6rem; }
        .an-pillar-bar-fill { height:100%; border-radius:99px; }
        .an-pillar-note { font-size:.68rem; color:#94a3b8; margin-top:.4rem; line-height:1.4; min-height:2.2em; }
        .an-segment-grid { display:grid; grid-template-columns:1fr; gap:1rem; margin-bottom:1rem; }
        @media(min-width:900px){ .an-segment-grid { grid-template-columns:repeat(3,1fr); } }
        .an-segment-card { background:white; border:1px solid #e6eaf1; border-radius:16px; padding:1.1rem; box-shadow:0 1px 3px rgba(15,23,42,.04); }
        .an-segment-title { font-size:.72rem; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:.03em; margin-bottom:.3rem; }
        .an-segment-score { font-size:1.7rem; font-weight:800; }
        .an-segment-verdict { font-size:.78rem; color:#334155; margin-top:.4rem; line-height:1.45; }
        .an-bottom-grid { display:grid; grid-template-columns:1fr; gap:1rem; }
        @media(min-width:1100px){ .an-bottom-grid { grid-template-columns:7fr 5fr; align-items:start; } }
        .an-narrative { font-size:.83rem; color:#475569; line-height:1.65; white-space:pre-wrap; }
        .an-subbox { background:#f8fafc; border:1px solid #f1f5f9; border-radius:12px; padding:.9rem; }
        .an-subbox-title { font-weight:700; font-size:.78rem; display:flex; align-items:center; gap:6px; margin-bottom:.5rem; }
        .an-subbox ul { list-style:none; margin:0; padding:0; }
        .an-subbox li { font-size:.75rem; color:#475569; padding:.25rem 0; display:flex; gap:6px; align-items:flex-start; line-height:1.5; }
        .an-gst-manual { border:1.5px dashed #c7d2fe; background:rgba(99,102,241,.04); border-radius:12px; padding:.9rem 1rem; margin-bottom:1rem; }
        .an-gst-manual .row { display:flex; gap:.6rem; flex-wrap:wrap; align-items:flex-end; }
        .an-gst-manual input { flex:1; min-width:180px; padding:.55rem .75rem; border:1.5px solid #e2e8f0; border-radius:8px; font-family:'Poppins',sans-serif; font-size:.82rem; background:white; }
        .an-gst-status { font-size:.72rem; margin-top:.5rem; font-weight:600; }
        .an-gst-status.ok  { color:#16a34a; }
        .an-gst-status.bad { color:#dc2626; }
        .an-weight-card { background:white; border:1px solid #e6eaf1; border-radius:16px; padding:1.1rem 1.25rem; box-shadow:0 1px 3px rgba(15,23,42,.04); margin-bottom:1rem; }
        .an-weight-header { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:.6rem; border-bottom:1px solid #f1f5f9; padding-bottom:.7rem; margin-bottom:.7rem; }
        .an-weight-title { font-size:.85rem; font-weight:800; color:#1e293b; display:flex; align-items:center; gap:8px; }
        .an-weight-toggle { display:flex; background:#f1f5f9; border-radius:8px; padding:2px; }
        .an-weight-toggle-btn { border:none; background:none; padding:.35rem .9rem; font-size:.75rem; font-weight:700; color:#64748b; border-radius:7px; cursor:pointer; font-family:'Poppins',sans-serif; }
        .an-weight-toggle-btn.active { background:white; color:#4338ca; box-shadow:0 1px 2px rgba(15,23,42,.08); }
        .an-weight-desc { font-size:.72rem; color:#64748b; line-height:1.5; margin-bottom:.9rem; }
        .an-weight-row { display:grid; grid-template-columns:1fr; gap:.4rem; align-items:center; padding:.45rem 0; }
        @media(min-width:640px){ .an-weight-row { grid-template-columns:9rem 1fr 5.5rem; gap:.75rem; } }
        .an-weight-label { display:flex; align-items:center; gap:.5rem; font-size:.75rem; font-weight:700; color:#334155; }
        .an-weight-icon { width:22px; height:22px; border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:.65rem; flex-shrink:0; }
        .an-weight-slider { width:100%; accent-color:#4f46e5; cursor:pointer; }
        .an-weight-slider:disabled { cursor:not-allowed; opacity:.55; }
        .an-weight-input-wrap { display:flex; align-items:center; justify-content:flex-end; gap:.25rem; }
        .an-weight-input { width:3rem; text-align:center; padding:.3rem .2rem; border:1.5px solid #e2e8f0; border-radius:6px; font-size:.75rem; font-weight:700; color:#334155; background:#f8fafc; font-family:'Poppins',sans-serif; }
        .an-weight-input:disabled { background:#f1f5f9; color:#94a3b8; }
        .an-weight-total-row { display:flex; justify-content:space-between; align-items:center; padding-top:.7rem; margin-top:.4rem; border-top:1px solid #f1f5f9; font-size:.8rem; font-weight:800; color:#1e293b; }
        .an-weight-total-row span:last-child.bad { color:#dc2626; }
        .an-weight-warning { font-size:.72rem; color:#dc2626; font-weight:600; margin-top:.4rem; display:flex; align-items:center; gap:6px; }
        .an-weight-mode-badge { display:inline-flex; align-items:center; gap:5px; padding:.2rem .65rem; border-radius:12px; font-size:.68rem; font-weight:700; background:rgba(79,70,229,.1); color:#4338ca; border:1px solid rgba(79,70,229,.25); margin-left:.5rem; }
        .an-decision-radio { border:1px solid #e2e8f0; border-radius:12px; padding:.8rem; }
        .an-decision-radio label { display:flex; align-items:center; gap:.6rem; padding:.35rem 0; font-size:.82rem; cursor:pointer; }
        .an-textarea { width:100%; font-size:.8rem; padding:.7rem; border:1.5px solid #e2e8f0; border-radius:10px; resize:vertical; font-family:'Poppins',sans-serif; background:#f8fafc; }
        .an-textarea:focus { outline:none; border-color:var(--blue-primary); background:#fff; }
        .an-save-btn { width:100%; background:linear-gradient(135deg,#16a34a,#15803d); color:white; border:none; padding:.75rem; border-radius:12px; font-weight:700; font-size:.82rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; margin-top:.9rem; }
        .an-save-btn:hover { opacity:.92; }
        @media(max-width:768px){
            .cards-container{grid-template-columns:1fr;padding:.5rem}
            .info-grid-mini{grid-template-columns:1fr}
            .hero-mini h1{font-size:1.8rem}
        }
    </style>
</head>
<body>

<div class="loading-overlay" id="loadingOverlay">
    <div class="spinner"></div>
    <p class="loading-text"    id="loadingText">Processing...</p>
    <p class="loading-subtext" id="loadingSubtext"></p>
</div>

<nav class="navbar-enterprise">
    <div class="container">
        <div class="d-flex justify-content-between align-items-center w-100">
            <a href="#" class="navbar-brand-custom" onclick="showPage('home')">
                <div class="brand-logo"><i class="fas fa-brain"></i></div>
                <span>AI Due Diligence</span>
            </a>
            <div class="d-flex gap-1">
                <button class="nav-link-custom active" data-page="home"    onclick="showPage('home')">Home</button>
                <button class="nav-link-custom" data-page="company"        onclick="showPage('company')" id="navCompany" disabled>About Company</button>
                <button class="nav-link-custom" data-page="financials"     onclick="showPage('financials')" id="navFinancials">Financials</button>
                <button class="nav-link-custom" data-page="legal" onclick="showPage('legal')" id="navLegal">Legal</button>
                <button class="nav-link-custom" data-page="analysis" onclick="showPage('analysis')" id="navAnalysis">Analysis</button>
            </div>
        </div>
    </div>
</nav>

<section id="page-home" class="page-section active">
<div class="container">
    <div class="hero-mini">
        <h1>AI-Powered <span>Due Diligence</span></h1>
        <p>Deep website RAG pipeline + GST verification for comprehensive business intelligence</p>
    </div>

    <div class="cards-container">

        <div class="module-card gst">
            <div class="card-header-custom">
                <div class="card-icon green"><i class="fas fa-file-invoice"></i></div>
                <div><h3>GST Verification</h3><p>Verify company GST registration</p></div>
            </div>
            <div id="gstAlert" class="alert-custom"></div>
            <form id="gstForm">
                <div class="form-group-custom">
                    <label><i class="fas fa-hashtag me-1"></i>GST Number *</label>
                    <input type="text" id="gstNumber" class="form-control-custom" placeholder="e.g., 29AAACI4798L1ZU" maxlength="15" required>
                </div>
                <button type="submit" class="btn-module green" id="gstBtn">
                    <i class="fas fa-search"></i> Verify GST
                </button>
            </form>
            <div class="results-section" id="gstResults">
                <div class="results-header">
                    <h4><i class="fas fa-check-circle text-success"></i> Verified</h4>
                    <button class="btn-clear" onclick="clearGstResults()"><i class="fas fa-times"></i> Clear</button>
                </div>
                <div id="gstResultsContent"></div>
            </div>
        </div>

        <div class="module-card website">
            <div class="card-header-custom">
                <div class="card-icon blue"><i class="fas fa-spider"></i></div>
                <div><h3>Deep Website RAG</h3><p>Playwright crawl → embeddings → AI extraction</p></div>
            </div>
            <div id="websiteAlert" class="alert-custom"></div>

            <div id="ragInputSection">
                <div class="form-group-custom">
                    <label><i class="fas fa-building me-1"></i>Company Name <span class="text-muted">(optional)</span></label>
                    <input type="text" id="companyName" class="form-control-custom" placeholder="e.g., Techno Electric">
                </div>
                <div class="form-group-custom">
                    <label><i class="fas fa-link me-1"></i>Website URL *</label>
                    <input type="text" id="websiteUrl" class="form-control-custom" placeholder="e.g., www.techno.co.in">
                </div>
                <div class="form-group-custom">
                    <label><i class="fas fa-hashtag me-1"></i>NSE Symbol <span class="text-muted">(optional)</span></label>
                    <input type="text" id="homeNseSymbol" class="form-control-custom" placeholder="e.g., TECHNOE" maxlength="20">
                </div>
                <div style="font-size:.72rem;color:#64748b;margin:-.4rem 0 .75rem">
                    <i class="fas fa-circle-info me-1"></i>Company name, website &amp; NSE symbol are also used to auto-run the Financials and Legal pages — no need to re-enter them there.
                </div>

                <div id="cacheStatus" style="display:none; margin-bottom:.75rem; font-size:.82rem; color:#475569;">
                    <span id="cacheStatusText"></span>
                    <button class="btn-sm-outline ms-2" onclick="reindexSite()" id="reindexBtn" style="display:none">
                        <i class="fas fa-rotate-right"></i> Re-index
                    </button>
                </div>

                <div class="d-grid gap-2">
                    <button class="btn-module blue" id="indexBtn" onclick="runFullCompanyAnalysis()">
                        <i class="fas fa-database"></i> Step 1 — Index &amp; Analyze
                    </button>
                    <button class="btn-module orange" id="extractBtn" onclick="startExtraction()" disabled>
                        <i class="fas fa-magic"></i> Step 2 — Extract Intelligence
                    </button>
                </div>
            </div>

            <div id="pipelineProgress" style="display:none; margin-top:1rem;">
                <div class="pipeline-steps">
                    <div class="pipeline-step" id="step-crawl">
                        <div class="step-icon"><i class="fas fa-spider"></i></div>
                        <span class="step-label">Crawling website pages</span>
                        <span class="step-detail" id="step-crawl-detail"></span>
                    </div>
                    <div class="pipeline-step" id="step-chunk">
                        <div class="step-icon"><i class="fas fa-scissors"></i></div>
                        <span class="step-label">Chunking by sections</span>
                        <span class="step-detail" id="step-chunk-detail"></span>
                    </div>
                    <div class="pipeline-step" id="step-embed">
                        <div class="step-icon"><i class="fas fa-microchip"></i></div>
                        <span class="step-label">Generating embeddings</span>
                        <span class="step-detail" id="step-embed-detail"></span>
                    </div>
                    <div class="pipeline-step" id="step-store">
                        <div class="step-icon"><i class="fas fa-database"></i></div>
                        <span class="step-label">Storing in ChromaDB</span>
                        <span class="step-detail" id="step-store-detail"></span>
                    </div>
                </div>
            </div>

            <div class="results-section" id="indexResults">
                <div class="results-header">
                    <h4><i class="fas fa-check-circle text-success"></i> Site Indexed</h4>
                </div>
                <div id="indexResultsContent"></div>
            </div>

            <div class="results-section" id="websiteResults">
                <div class="results-header">
                    <h4><i class="fas fa-check-circle text-primary"></i> Extraction Complete</h4>
                    <button class="btn-clear" onclick="clearWebsiteResults()"><i class="fas fa-times"></i> Clear</button>
                </div>
                <div id="websiteResultsPreview"></div>
                <button class="btn-module blue mt-3" onclick="showPage('company')" style="font-size:.85rem;padding:.6rem;">
                    <i class="fas fa-external-link-alt"></i> View Full Intelligence Report
                </button>
            </div>
        </div>

    </div>
</div>
</section>

<section id="page-company" class="page-section">
<div class="container">
    <div class="mb-3">
        <button class="back-btn" onclick="showPage('home')"><i class="fas fa-arrow-left"></i> Back</button>
    </div>
    <div id="companyPageContent">
        <div class="info-card text-center py-4">
            <i class="fas fa-building fa-3x mb-3" style="color:#e2e8f0"></i>
            <h4 class="text-muted">No Data Yet</h4>
            <p class="text-muted">Use the Deep Website RAG module on the home page first.</p>
        </div>
    </div>
</div>
</section>

<section id="page-financials" class="page-section">
<div class="container-fluid" style="max-width:1400px">
    <div class="mb-3">
        <button class="back-btn" onclick="showPage('home')"><i class="fas fa-arrow-left"></i> Back</button>
    </div>

    <div class="fin2-searchbar">
        <div class="fin2-field" style="flex:1.4;min-width:220px">
            <label><i class="fas fa-building me-1"></i>Company Name (Screener.in)</label>
            <input type="text" id="finCompanyName" placeholder="e.g., Techno Electric & Engineering">
        </div>
        <div class="fin2-field">
            <label><i class="fas fa-hashtag me-1"></i>NSE Symbol (optional)</label>
            <input type="text" id="finNseSymbol" placeholder="e.g., TECHNOE" maxlength="20">
        </div>
        <div class="fin2-field">
            <label><i class="fas fa-link me-1"></i>Company Website (optional)</label>
            <input type="text" id="finWebsiteUrl" placeholder="e.g., www.company.com">
        </div>
        <button class="fin2-analyze-btn" id="finAnalyzeBtn" onclick="runFinancialsAnalysis()">
            <i class="fas fa-magnifying-glass-chart"></i> Analyze
        </button>
    </div>
    <div id="fin2Alert" class="alert-custom" style="margin-top:-.25rem"></div>

    <div class="fin2-empty-state" id="fin2Empty">
        <i class="fa-solid fa-chart-pie"></i>
        <h4 style="color:#475569;margin-bottom:.4rem">No company analyzed yet</h4>
        <p style="font-size:.85rem">Enter a company name above and click <strong>Analyze</strong> to pull Screener.in fundamentals and live NSE data.</p>
    </div>

    <div class="fin2-hero" id="fin2Hero">
        <div class="fin2-hero-left">
            <div class="fin2-hero-icon"><i class="fa-solid fa-building"></i></div>
            <div>
                <div class="fin2-hero-name" id="fin2HeroName">—</div>
                <div class="fin2-hero-meta" id="fin2HeroMeta"></div>
            </div>
        </div>
        <div class="fin2-hero-right">
            <span class="fin2-confidence" id="fin2Confidence"><i class="fa-solid fa-shield-halved"></i><span>Live data</span></span>
            <span class="fin2-fy-pill" id="fin2FyPill">—</span>
        </div>
    </div>

    <div class="fin2-kpi-grid" id="fin2KpiGrid"></div>

    <div class="fin2-tabs-row" id="fin2TabsRow">
        <div class="fin2-tabs" id="fin2Tabs">
            <button class="fin2-tab active" data-tab="summary" onclick="switchFin2Tab('summary')">NSE Financials</button>
            <button class="fin2-tab" data-tab="balance_sheet" onclick="switchFin2Tab('balance_sheet')">Balance Sheet</button>
            <button class="fin2-tab" data-tab="profit_loss" onclick="switchFin2Tab('profit_loss')">Profit &amp; Loss</button>
            <button class="fin2-tab" data-tab="cash_flow" onclick="switchFin2Tab('cash_flow')">Cash Flow</button>
            <button class="fin2-tab" data-tab="ratios" onclick="switchFin2Tab('ratios')">Ratios</button>
        </div>
        <a href="#" target="_blank" id="fin2NseLink" class="fin2-nse-link"><span>View on Screener.in</span><i class="fa-solid fa-arrow-up-right-from-square"></i></a>
    </div>

    <div class="fin2-layout" id="fin2Layout">

        <div>
            <div class="fin2-panel active" id="fin2Panel-summary">
                <div class="fin2-upper-grid">
                    <div class="fin2-card" style="margin-bottom:0">
                        <div class="fin2-card-title"><span>Financials Summary (₹ Cr)</span><i class="fa-regular fa-circle-question"></i></div>
                        <div id="fin2SummaryTable"><p class="text-muted" style="font-size:.8rem">No data yet.</p></div>
                    </div>
                    <div class="fin2-card" style="margin-bottom:0">
                        <div class="fin2-chart-toolbar">
                            <div class="fin2-card-title" style="margin-bottom:0"><span>NSE Price Chart</span><i class="fa-regular fa-circle-question"></i></div>
                            <div class="fin2-period-group" id="fin2PeriodGroup">
                                <button class="fin2-period-btn" data-p="5d">1D</button>
                                <button class="fin2-period-btn" data-p="1mo">1M</button>
                                <button class="fin2-period-btn" data-p="3mo">3M</button>
                                <button class="fin2-period-btn active" data-p="6mo">6M</button>
                                <button class="fin2-period-btn" data-p="1y">1Y</button>
                                <button class="fin2-period-btn" data-p="5y">5Y</button>
                                <button class="fin2-period-btn" data-p="max">Max</button>
                            </div>
                        </div>
                        <div class="fin2-chart-price-row" id="fin2ChartPriceRow">
                            <span style="color:#94a3b8">Enter an NSE symbol and analyze to load the chart</span>
                        </div>
                        <div id="fin2ChartSvgWrap" style="min-height:150px"></div>
                        <div class="fin2-chart-footer" id="fin2ChartFooter"></div>
                    </div>
                </div>

                <div class="d-flex gap-3 flex-wrap" id="fin2ThreeCol"></div>
            </div>

            <div class="fin2-panel" id="fin2Panel-balance_sheet"><div class="fin2-card"><div class="fin2-card-title">Balance Sheet — Full History</div><div id="fin2Full-balance_sheet"></div></div></div>
            <div class="fin2-panel" id="fin2Panel-profit_loss"><div class="fin2-card"><div class="fin2-card-title">Profit &amp; Loss — Full History</div><div id="fin2Full-profit_loss"></div></div></div>
            <div class="fin2-panel" id="fin2Panel-cash_flow"><div class="fin2-card"><div class="fin2-card-title">Cash Flow — Full History</div><div id="fin2Full-cash_flow"></div></div></div>
            <div class="fin2-panel" id="fin2Panel-ratios"><div class="fin2-card"><div class="fin2-card-title">Key Ratios — Full History</div><div id="fin2Full-ratios"></div></div></div>

            <div class="fin2-ai-box" id="fin2AiBox">
                <div class="fin2-card">
                    <div class="fin2-card-title"><i class="fas fa-wand-magic-sparkles" style="color:#7c3aed"></i><span>AI Report Analysis</span></div>
                    <div id="fin2AiContent"></div>
                </div>
            </div>
        </div>

        <div>
            <div class="fin2-card">
                <div class="fin2-card-title"><span>Upload Annual Report / Financials</span><i class="fa-regular fa-circle-question"></i></div>
                <p style="font-size:.72rem;color:#64748b;margin-bottom:.75rem">Upload a PDF of the Annual Report or Financial Statements — analyzed with AI</p>
                <div class="fin2-side-upload">
                    <div class="cloud"><i class="fa-solid fa-cloud-arrow-up"></i></div>
                    <p style="font-size:.75rem;font-weight:700;color:#334155;margin-bottom:.1rem">Drag &amp; drop your file here</p>
                    <p style="font-size:.68rem;color:#94a3b8;margin-bottom:.6rem">or</p>
                    <input type="file" id="finPdfFile" accept="application/pdf" style="display:none" onchange="uploadFinancialPdf()">
                    <button class="fin2-choose-btn" onclick="document.getElementById('finPdfFile').click()">Choose File</button>
                </div>
                <div style="font-size:.68rem;color:#94a3b8;margin-top:.6rem"><i class="fa-regular fa-clock"></i> Supports PDF (Max. 25MB)</div>
                <div class="fin2-upload-status" id="fin2UploadStatus">No file uploaded</div>
            </div>

            <div class="fin2-card">
                <div class="fin2-card-title"><span>Market Data (NSE)</span><i class="fa-regular fa-circle-question"></i></div>
                <div id="fin2MarketData"><p style="font-size:.75rem;color:#94a3b8">Enter an NSE symbol and analyze to load live data.</p></div>
            </div>

            <div class="fin2-card">
                <div class="fin2-card-title" style="justify-content:space-between;display:flex">
                    <span><span>Financial Documents</span> <i class="fa-regular fa-circle-question"></i></span>
                </div>
                <div id="fin2Docs"><p style="font-size:.75rem;color:#94a3b8">Add a company website above to auto-find annual reports.</p></div>
            </div>
        </div>

    </div>

    <div style="text-align:center;font-size:.72rem;color:#94a3b8;margin-top:2rem">
        All financial data is sourced from Screener.in, NSE (via Yahoo Finance), and uploaded filings. AI insights are for informational purposes only.
    </div>
</div>
</section>


<section id="page-legal" class="page-section">
<div class="container-fluid" style="max-width:1400px">
    <div class="mb-3">
        <button class="back-btn" onclick="showPage('home')"><i class="fas fa-arrow-left"></i> Back</button>
    </div>

    <div class="legal-searchbar">
        <div class="lsb-field" style="flex:1.6;min-width:260px">
            <label><i class="fas fa-building me-1"></i>Company Name</label>
            <input type="text" id="legalCompanyInput" placeholder="e.g., Techno Electric &amp; Engineering Company Ltd.">
        </div>
        <div class="lsb-field">
            <label><i class="fas fa-calendar me-1"></i>Date Range</label>
            <select id="legalDaysBack">
                <option value="30">Last 30 days</option>
                <option value="90">Last 3 months</option>
                <option value="180">Last 6 months</option>
                <option value="365" selected>Last 1 year</option>
                <option value="730">Last 2 years</option>
            </select>
        </div>
        <div class="lsb-field">
            <label><i class="fas fa-file-lines me-1"></i>Court Pages</label>
            <select id="legalCourtPages">
                <option value="1">1 page (~10 cases)</option>
                <option value="2" selected>2 pages (~20 cases)</option>
                <option value="3">3 pages (~30 cases)</option>
            </select>
        </div>
        <button class="legal-analyze-btn" id="legalAnalyzeBtn" onclick="runLegalAnalysis()">
            <i class="fas fa-scale-balanced me-1"></i> Analyze
        </button>
    </div>
    <div id="legalAlert" class="alert-custom"></div>

    <div class="legal-empty" id="legalEmpty" style="padding:4rem 2rem;background:white;border-radius:18px;border:2px dashed #e2e8f0;">
        <i class="fas fa-scale-balanced" style="font-size:2.5rem;color:#cbd5e1;display:block;margin-bottom:.75rem"></i>
        <h4 style="color:#475569;margin-bottom:.4rem">No company analyzed yet</h4>
        <p style="font-size:.85rem;color:#94a3b8">Enter a company name above and click <strong>Analyze</strong> to pull news &amp; court records.</p>
    </div>

    <div id="legalResultsArea" style="display:none">

        <div class="legal-hero" id="legalHero">
            <div class="legal-hero-left">
                <div class="legal-hero-icon"><i class="fas fa-scale-balanced"></i></div>
                <div>
                    <h1 id="legalHeroName">Legal Intelligence</h1>
                    <p>Real-time news from trusted sources &amp; court records from Indian Kanoon</p>
                </div>
            </div>
            <div class="legal-live-badge">
                <div class="legal-live-dot"></div>
                <span>Live Data</span>
            </div>
        </div>


        <div class="legal-kpi-strip" id="legalKpiStrip"></div>

        <div class="legal-findings" id="legalFindings"></div>

        <div class="legal-columns">

            <div class="legal-panel">
                <div class="legal-panel-header">
                    <div class="legal-panel-title">
                        <div class="legal-panel-icon news"><i class="fas fa-newspaper"></i></div>
                        <div>
                            <h2 style="font-size:1rem;font-weight:700;color:#0f172a;margin:0">Latest News</h2>
                            <p style="font-size:.72rem;color:#64748b;margin:0">Google News RSS + GNews API</p>
                        </div>
                    </div>
                    <button class="btn-sm-outline" onclick="refreshLegalNews()" style="font-size:.72rem">
                        <i class="fas fa-rotate-right"></i> Refresh
                    </button>
                </div>

                <div class="legal-filter-bar">
                    <div class="legal-search-wrap">
                        <i class="fas fa-search"></i>
                        <input type="text" class="legal-search-input" id="newsSearchInput"
                               placeholder="Search news headlines..."
                               oninput="filterNews()">
                    </div>
                    <select class="legal-date-select" id="newsDateFilter" onchange="filterNews()">
                        <option value="all">All time</option>
                        <option value="7">Last 7d</option>
                        <option value="30">Last 30d</option>
                        <option value="90">Last 90d</option>
                    </select>
                </div>

                <div class="legal-chips" id="newsChips">
                    <span class="legal-chip active" data-risk="all" onclick="setNewsChip(this,'all')">All</span>
                    <span class="legal-chip inactive" data-risk="high" onclick="setNewsChip(this,'high')">High Risk</span>
                    <span class="legal-chip inactive" data-risk="medium" onclick="setNewsChip(this,'medium')">Medium</span>
                    <span class="legal-chip inactive" data-risk="low" onclick="setNewsChip(this,'low')">Low Risk</span>
                    <span class="legal-chip inactive" data-risk="relevant" onclick="setNewsChip(this,'relevant')">Relevant</span>
                    <span class="legal-chip inactive" data-risk="low_confidence" onclick="setNewsChip(this,'low_conf')">Low Confidence</span>
                </div>

                <div id="newsCards" style="display:flex;flex-direction:column;gap:.6rem"></div>
                <div id="newsLoadMore" style="text-align:center;display:none">
                    <button class="fin2-view-more" onclick="loadMoreNews()">Load more</button>
                </div>
            </div>

            <div class="legal-panel">
                <div class="legal-panel-header">
                    <div class="legal-panel-title">
                        <div class="legal-panel-icon cases"><i class="fas fa-gavel"></i></div>
                        <div>
                            <h2 style="font-size:1rem;font-weight:700;color:#0f172a;margin:0">Legal Cases</h2>
                            <p style="font-size:.72rem;color:#64748b;margin:0">Indian Kanoon — Scraped</p>
                        </div>
                    </div>
                    <button class="btn-sm-outline" onclick="refreshLegalCases()" style="font-size:.72rem">
                        <i class="fas fa-rotate-right"></i> Refresh
                    </button>
                </div>

                <div style="display:flex;gap:.5rem">
                    <div class="legal-search-wrap" style="flex:1;position:relative">
                        <i class="fas fa-search" style="position:absolute;left:.75rem;top:50%;transform:translateY(-50%);color:#94a3b8;font-size:.85rem"></i>
                        <input type="text" class="legal-search-input" id="casesSearchInput"
                               placeholder="Search cases..."
                               oninput="filterCases()" style="padding-left:2.25rem">
                    </div>
                    <button class="btn-module blue" style="width:auto;padding:.55rem 1rem;font-size:.78rem"
                            onclick="refreshLegalCases()">
                        <i class="fas fa-search"></i> Search
                    </button>
                </div>

                <div class="legal-chips" id="casesChips">
                    <span class="legal-chip active" data-court="all" onclick="setCasesChip(this,'all')">All Cases</span>
                    <span class="legal-chip inactive" data-court="supreme" onclick="setCasesChip(this,'supreme')">Supreme Court</span>
                    <span class="legal-chip inactive" data-court="high" onclick="setCasesChip(this,'high')">High Court</span>
                    <span class="legal-chip inactive" data-court="nclt" onclick="setCasesChip(this,'nclt')">NCLT/NCLAT</span>
                    <span class="legal-chip inactive" data-court="tribunal" onclick="setCasesChip(this,'tribunal')">Tribunal</span>
                </div>

                <div id="casesCards" style="display:flex;flex-direction:column;gap:.6rem"></div>
                <div id="casesLoadMore" style="text-align:center;display:none">
                    <button class="fin2-view-more" onclick="loadMoreCases()">Load more</button>
                </div>
            </div>

        </div>
    </div>
</div>
</section>

<section id="page-analysis" class="page-section">
<div class="container-fluid" style="max-width:1400px">
    <div class="mb-3">
        <button class="back-btn" onclick="showPage('home')"><i class="fas fa-arrow-left"></i> Back</button>
    </div>

    <div style="margin-bottom:1.1rem">
        <h1 style="font-size:1.55rem;font-weight:800;color:#0f172a;margin:0">Due Diligence <span style="color:#4f46e5">Analysis</span></h1>
        <p style="font-size:.75rem;color:#64748b;margin:.25rem 0 0">Comprehensive risk assessment &amp; recommendation</p>
    </div>
    <div id="anAlert" class="alert-custom"></div>

    <div style="background:white;border-radius:var(--radius-lg);padding:1rem 1.25rem;box-shadow:var(--shadow-soft);margin-bottom:1rem;display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end">
        <div style="flex:1.4;min-width:220px">
            <label style="display:block;font-size:.7rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.3rem"><i class="fas fa-building me-1"></i>Company Name</label>
            <input type="text" id="anCompanyName" placeholder="e.g., Techno Electric &amp; Engineering Company Ltd."
                   style="width:100%;padding:.6rem .8rem;border:1.5px solid #e2e8f0;border-radius:10px;font-family:'Poppins',sans-serif;font-size:.85rem;background:#f8fafc">
        </div>
        <div style="font-size:.72rem;color:#64748b;max-width:300px;padding-bottom:.3rem;line-height:1.5">
            <i class="fas fa-circle-info me-1 text-indigo-400"></i>Reuses GST / Financials / Legal / Website data already fetched on other tabs.
        </div>
        <button class="an-run-btn" id="anRunBtn" onclick="runFullAnalysis()">
            <i class="fas fa-wand-magic-sparkles"></i> Generate Analysis
        </button>
    </div>

    <div id="anCompanyHeader" style="display:none;background:white;border-radius:14px;border:1px solid #e2e8f0;padding:1rem 1.25rem;display:none;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:1rem;box-shadow:0 1px 4px rgba(15,23,42,.05);margin-bottom:1rem">
        <div style="display:flex;align-items:center;gap:.85rem">
            <div style="width:44px;height:44px;border-radius:12px;background:#2563eb;display:flex;align-items:center;justify-content:center;color:white;font-size:1.1rem;flex-shrink:0">
                <i class="fas fa-building"></i>
            </div>
            <div>
                <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
                    <span style="font-size:1rem;font-weight:800;color:#0f172a" id="anBannerName">—</span>
                    <span id="anGstStatusBadge" style="display:none;font-size:.65rem;font-weight:700;padding:.2rem .6rem;border-radius:20px;background:#ecfdf5;color:#16a34a;border:1px solid #bbf7d0">Active</span>
                </div>
                <div style="font-size:.72rem;color:#64748b;margin-top:.2rem" id="anBannerMeta"></div>
            </div>
        </div>
        <div style="display:flex;align-items:center;gap:.75rem">
            <div style="text-align:right">
                <div style="font-size:.65rem;color:#94a3b8;font-weight:600">Analysis Date</div>
                <div style="font-size:.75rem;font-weight:700;color:#334155" id="anBannerTimestamp"></div>
            </div>
            <button onclick="runFullAnalysis()" style="display:flex;align-items:center;gap:6px;padding:.4rem 1rem;border:1.5px solid #2563eb;color:#2563eb;border-radius:9px;font-size:.75rem;font-weight:700;background:none;cursor:pointer">
                <i class="fas fa-rotate-right"></i> Re-analyze
            </button>
        </div>
    </div>

    <details style="margin-bottom:.75rem">
        <summary style="font-size:.75rem;font-weight:700;color:#4338ca;cursor:pointer;padding:.5rem .75rem;background:rgba(99,102,241,.06);border:1.5px dashed #c7d2fe;border-radius:10px;list-style:none;display:flex;align-items:center;gap:.5rem">
            <i class="fas fa-pen-to-square"></i> Manual GST Verification Override
        </summary>
        <div style="margin-top:.5rem;padding:.85rem 1rem;background:rgba(99,102,241,.04);border:1.5px dashed #c7d2fe;border-radius:10px;border-top:none">
            <div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:flex-end">
                <input type="text" id="anGstManualInput" placeholder="Enter GSTIN, e.g. 29AAACI4798L1ZU" maxlength="15"
                       style="flex:1;min-width:200px;padding:.55rem .75rem;border:1.5px solid #e2e8f0;border-radius:8px;font-family:'Poppins',sans-serif;font-size:.82rem;background:white">
                <button class="btn-sm-outline" onclick="verifyGstManual()" id="anGstManualBtn">Verify &amp; Use</button>
                <button class="btn-clear" onclick="clearGstManualOverride()">Clear</button>
            </div>
            <div class="an-gst-status" id="anGstManualStatus" style="margin-top:.5rem;font-size:.72rem;font-weight:600;color:#64748b">No override set — using Home page GST data.</div>
        </div>
    </details>

    <div class="an-weight-card" style="margin-bottom:1rem">
        <div class="an-weight-header">
            <div class="an-weight-title"><i class="fas fa-sliders"></i> Weight Configuration<span class="an-weight-mode-badge" id="anWeightModeBadge">Standard</span></div>
            <div class="an-weight-toggle">
                <button class="an-weight-toggle-btn active" data-mode="standard" onclick="setWeightMode('standard')" type="button">Standard</button>
                <button class="an-weight-toggle-btn" data-mode="custom" onclick="setWeightMode('custom')" type="button">Custom</button>
            </div>
        </div>
        <p class="an-weight-desc" id="anWeightDesc">
            Standard mode uses built-in weight profiles that differ by segment. Switch to <strong>Custom</strong> to set a uniform weight across all 5 pillars.
        </p>

        <div style="display:grid;grid-template-columns:9rem 1fr 5.5rem;gap:.75rem;font-size:.65rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em;padding:.2rem 0;border-bottom:1px solid #f1f5f9;margin-bottom:.4rem">
            <span>Factor</span>
            <span style="text-align:center">Weight (%)</span>
            <span style="text-align:right">Value</span>
        </div>

        <div class="an-weight-rows" id="anWeightRows"></div>
        <div class="an-weight-total-row">
            <span>Total</span>
            <span id="anWeightTotal">100%</span>
        </div>
        <div class="an-weight-warning" id="anWeightWarning" style="display:none">
            <i class="fas fa-triangle-exclamation"></i><span>Weights must add up to 100% before generating the analysis.</span>
        </div>
        <button class="an-run-btn" id="anRunBtn2" onclick="runFullAnalysis()" style="width:100%;justify-content:center;margin-top:.85rem;background:linear-gradient(135deg,#4f46e5,#6d28d9)">
            <i class="fas fa-wand-magic-sparkles"></i> Generate Analysis
        </button>
    </div>

    <div class="an-empty" id="anEmpty">
        <i class="fa-solid fa-scale-balanced"></i>
        <h4 style="color:#475569;margin-bottom:.4rem">No analysis run yet</h4>
        <p style="font-size:.85rem">Enter a company name above and click <strong>Generate Analysis</strong>.</p>
    </div>

    <div id="anResults" style="display:none">

        <div style="display:grid;grid-template-columns:1fr;gap:1rem;margin-bottom:1rem" class="an-top-grid">

            <div style="background:white;border-radius:14px;border:1px solid #e2e8f0;padding:1.1rem;box-shadow:0 1px 4px rgba(15,23,42,.05);display:flex;flex-direction:column;justify-content:space-between">
                <div style="display:flex;align-items:center;justify-content:space-between">
                    <span style="font-size:.72rem;font-weight:800;color:#1e293b">Overall Due Diligence Score</span>
                    <i class="fas fa-circle-info" style="color:#cbd5e1;font-size:.75rem"></i>
                </div>
                <div style="display:flex;flex-direction:column;align-items:center;padding:.5rem 0;position:relative">
                    <svg width="160" height="95" viewBox="0 0 160 90" style="overflow:visible">
                        <path d="M 15 80 A 65 65 0 0 1 145 80" fill="none" stroke="#f1f5f9" stroke-width="12" stroke-linecap="round"/>
                        <defs>
                            <linearGradient id="anScoreGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                                <stop offset="0%" stop-color="#10b981"/>
                                <stop offset="60%" stop-color="#eab308"/>
                                <stop offset="100%" stop-color="#22c55e"/>
                            </linearGradient>
                        </defs>
                        <path id="anGaugeArc" d="M 15 80 A 65 65 0 0 1 145 80" fill="none" stroke="url(#anScoreGrad)" stroke-width="12"
                              stroke-linecap="round" style="stroke-dasharray:204.2;stroke-dashoffset:204.2;transition:stroke-dashoffset 1s ease"/>
                    </svg>
                    <div style="position:absolute;bottom:4px;display:flex;flex-direction:column;align-items:center">
                        <span class="an-gauge-score" id="anOverallScore" style="font-size:2rem;font-weight:900;color:#0f172a;line-height:1">—</span>
                        <span style="font-size:.7rem;color:#94a3b8;font-weight:600">/ 100</span>
                    </div>
                </div>
                <div style="text-align:center">
                    <span id="anOverallBand" style="font-size:.68rem;font-weight:800;letter-spacing:.05em;text-transform:uppercase;padding:.3rem .9rem;border-radius:20px;background:#ecfdf5;color:#16a34a;border:1px solid #bbf7d0;display:inline-block">—</span>
                </div>
            </div>

            <div style="background:white;border-radius:14px;border:1px solid #e2e8f0;padding:1.1rem;box-shadow:0 1px 4px rgba(15,23,42,.05);display:flex;flex-direction:column;justify-content:space-between">
                <span style="font-size:.72rem;font-weight:800;color:#1e293b">Recommendation</span>
                <div class="an-reco-box" id="anRecoBox" style="margin:.6rem 0">
                    <i class="fa-solid fa-check-circle"></i><span id="anRecoText">—</span>
                </div>
                <p style="font-size:.72rem;color:#64748b;line-height:1.55;margin:0" id="anRecoBlurb"></p>
                <div style="display:flex;gap:.75rem;padding-top:.75rem;border-top:1px solid #f1f5f9;margin-top:.5rem">
                    <div style="flex:1;text-align:center">
                        <div style="font-size:.6rem;color:#94a3b8;font-weight:700;text-transform:uppercase">Confidence</div>
                        <div style="font-size:.95rem;font-weight:800;color:#0f172a" id="anSegAvg">—</div>
                    </div>
                    <div style="flex:1;text-align:center">
                        <div style="font-size:.6rem;color:#94a3b8;font-weight:700;text-transform:uppercase">Data Coverage</div>
                        <div style="font-size:.95rem;font-weight:800;color:#0f172a" id="anDataCoverage">—</div>
                    </div>
                </div>
            </div>

            <div style="background:white;border-radius:14px;border:1px solid #e2e8f0;padding:1.1rem;box-shadow:0 1px 4px rgba(15,23,42,.05)">
                <span style="font-size:.72rem;font-weight:800;color:#1e293b;display:block;margin-bottom:.75rem">Decision Guide</span>
                <div style="display:flex;flex-direction:column;gap:.5rem;font-size:.72rem">
                    <div style="display:flex;justify-content:space-between;align-items:center"><div style="display:flex;align-items:center;gap:.5rem"><span style="width:10px;height:10px;border-radius:50%;background:#10b981;display:inline-block"></span><strong>80–100</strong></div><span style="color:#64748b">Low Risk – Provide</span></div>
                    <div style="display:flex;justify-content:space-between;align-items:center"><div style="display:flex;align-items:center;gap:.5rem"><span style="width:10px;height:10px;border-radius:50%;background:#eab308;display:inline-block"></span><strong>60–79</strong></div><span style="color:#64748b">Moderate – Conditions</span></div>
                    <div style="display:flex;justify-content:space-between;align-items:center"><div style="display:flex;align-items:center;gap:.5rem"><span style="width:10px;height:10px;border-radius:50%;background:#f97316;display:inline-block"></span><strong>40–59</strong></div><span style="color:#64748b">High Risk – Review</span></div>
                    <div style="display:flex;justify-content:space-between;align-items:center"><div style="display:flex;align-items:center;gap:.5rem"><span style="width:10px;height:10px;border-radius:50%;background:#ef4444;display:inline-block"></span><strong>0–39</strong></div><span style="color:#64748b">Very High – Do Not Provide</span></div>
                </div>
            </div>

            <div style="background:white;border-radius:14px;border:2px solid #10b981;padding:1rem;box-shadow:0 1px 4px rgba(15,23,42,.05);display:flex;flex-direction:column;justify-content:space-between" id="anGstCallout">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <span style="font-size:.72rem;font-weight:800;color:#1e293b">GST Status</span>
                    <i class="fas fa-circle-info" style="color:#cbd5e1;font-size:.75rem"></i>
                </div>
                <div style="text-align:center;padding:.5rem 0">
                    <div id="anGstCalloutContent" style="font-size:.78rem;color:#64748b">Run analysis to check GST data.</div>
                </div>
            </div>
        </div>

        <div class="an-pillar-grid" id="anPillarGrid" style="margin-bottom:1rem"></div>

        <div style="display:grid;grid-template-columns:1fr;gap:1rem;margin-bottom:1rem" id="anSegmentRow">
            <div class="an-segment-grid" id="anSegmentGrid"></div>
        </div>

        <div class="an-bottom-grid">
            <div style="background:white;border-radius:14px;border:1px solid #e2e8f0;padding:1.25rem;box-shadow:0 1px 4px rgba(15,23,42,.05)">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem">
                    <div style="display:flex;align-items:center;gap:.5rem">
                        <i class="fas fa-wand-magic-sparkles" style="color:#2563eb;font-size:.85rem"></i>
                        <span style="font-size:.75rem;font-weight:800;color:#1e293b">AI Analysis &amp; Explanation</span>
                    </div>
                    <i class="fas fa-circle-info" style="color:#cbd5e1;font-size:.75rem"></i>
                </div>

                <p class="an-narrative" id="anNarrative" style="font-size:.8rem;color:#475569;line-height:1.7;margin-bottom:1rem">Run analysis to generate an AI narrative.</p>

                <div style="display:grid;grid-template-columns:1fr 1fr;gap:.85rem">
                    <div>
                        <div style="display:flex;align-items:center;gap:.4rem;font-size:.72rem;font-weight:800;color:#16a34a;margin-bottom:.5rem">
                            <i class="fas fa-shield-halved"></i><span>Key Positives</span>
                        </div>
                        <ul id="anHighlightsList" style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.35rem"></ul>
                    </div>
                    <div style="background:#fffbeb;border:1px solid #fef08a;border-radius:10px;padding:.75rem">
                        <div style="display:flex;align-items:center;gap:.4rem;font-size:.72rem;font-weight:800;color:#b45309;margin-bottom:.5rem">
                            <i class="fas fa-triangle-exclamation"></i><span>Watch Points</span>
                        </div>
                        <ul id="anFlagsList" style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.35rem"></ul>
                    </div>
                </div>

                <div style="margin-top:1rem">
                    <div style="display:flex;align-items:center;gap:.4rem;font-size:.72rem;font-weight:800;color:#2563eb;margin-bottom:.5rem">
                        <i class="fas fa-layer-group"></i><span>Key Evidence</span>
                    </div>
                    <div id="anEvidenceTable" style="font-size:.72rem;display:flex;flex-direction:column;gap:.1rem"></div>
                </div>

                <div style="margin-top:1rem;font-size:.65rem;color:#94a3b8">
                    AI analysis is based on available data. Validate critical decisions manually.
                </div>
            </div>

            <div style="background:white;border-radius:14px;border:1px solid #e2e8f0;padding:1.25rem;box-shadow:0 1px 4px rgba(15,23,42,.05);display:flex;flex-direction:column;justify-content:space-between">
                <div>
                    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.85rem">
                        <span style="font-size:.75rem;font-weight:800;color:#1e293b">Final Decision (Analyst Override)</span>
                        <i class="fas fa-circle-info" style="color:#cbd5e1;font-size:.75rem"></i>
                    </div>

                    <span style="font-size:.7rem;font-weight:700;color:#334155;display:block;margin-bottom:.4rem">Override Decision</span>
                    <div style="border:1px solid #e2e8f0;border-radius:10px;padding:.75rem;display:flex;flex-direction:column;gap:.4rem">
                        <label style="display:flex;align-items:center;gap:.6rem;cursor:pointer;font-size:.72rem;color:#334155;font-weight:500">
                            <input type="radio" name="anDecision" value="provide" checked style="accent-color:#16a34a;width:14px;height:14px"> Provide Service
                        </label>
                        <label style="display:flex;align-items:center;gap:.6rem;cursor:pointer;font-size:.72rem;color:#334155;font-weight:500">
                            <input type="radio" name="anDecision" value="conditions" style="accent-color:#f59e0b;width:14px;height:14px"> Provide with Conditions
                        </label>
                        <label style="display:flex;align-items:center;gap:.6rem;cursor:pointer;font-size:.72rem;color:#334155;font-weight:500">
                            <input type="radio" name="anDecision" value="review" style="accent-color:#f97316;width:14px;height:14px"> Manual Review Required
                        </label>
                        <label style="display:flex;align-items:center;gap:.6rem;cursor:pointer;font-size:.72rem;color:#334155;font-weight:500">
                            <input type="radio" name="anDecision" value="deny" style="accent-color:#ef4444;width:14px;height:14px"> Do Not Provide Service
                        </label>
                    </div>

                    <div style="margin-top:.85rem">
                        <span style="font-size:.7rem;font-weight:700;color:#334155;display:block;margin-bottom:.35rem">Analyst Comments (Optional)</span>
                        <textarea id="anComments" class="an-textarea" rows="3" placeholder="Enter your comments or specific observations..." style="width:100%;border:1.5px solid #e2e8f0;border-radius:10px;padding:.6rem .75rem;font-size:.75rem;font-family:'Poppins',sans-serif;background:#f8fafc;resize:vertical"></textarea>
                    </div>

                    <div style="margin-top:.75rem">
                        <span style="font-size:.7rem;font-weight:700;color:#334155;display:block;margin-bottom:.3rem">Decision Confidence</span>
                        <div style="display:flex;align-items:center;gap:.75rem">
                            <input type="range" id="anConfidence" min="0" max="100" value="80" style="flex:1;accent-color:#4f46e5;height:4px;cursor:pointer">
                            <span style="background:#f1f5f9;color:#334155;font-size:.72rem;font-weight:700;padding:.3rem .7rem;border-radius:8px;border:1px solid #e2e8f0;white-space:nowrap" id="anConfidenceLabel">High</span>
                        </div>
                    </div>
                </div>

                <button class="an-save-btn" onclick="saveAnalystDecision()" style="margin-top:.9rem;width:100%;padding:.7rem;background:#16a34a;color:white;border:none;border-radius:11px;font-weight:700;font-size:.8rem;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px">
                    <i class="fas fa-download"></i> Save &amp; Download Report (JSON)
                </button>

                <!-- ═══ NEW — PDF report download button ═══ -->
                <button class="an-save-btn" onclick="downloadPdfReport()" style="margin-top:.5rem;width:100%;padding:.7rem;background:#2563eb;color:white;border:none;border-radius:11px;font-weight:700;font-size:.8rem;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px">
                    <i class="fas fa-file-pdf"></i> Download PDF Report
                </button>
            </div>
        </div>

        <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:.7rem 1rem;display:flex;align-items:center;gap:.6rem;font-size:.72rem;color:#1d4ed8;margin-top:1rem">
            <i class="fas fa-circle-info" style="flex-shrink:0"></i>
            <span>This analysis is for informational purposes only and should not be considered as financial or legal advice.</span>
        </div>

    </div>
</div>
</section>

<footer class="footer">
    <div class="container">
        <div class="footer-content">
            <div class="footer-brand">
                <div class="brand-logo" style="width:28px;height:28px;font-size:.9rem"><i class="fas fa-brain"></i></div>
                AI Due Diligence Assistant — RAG Edition
            </div>
            <span style="font-size:.8rem;opacity:.6">Playwright · sentence-transformers · ChromaDB · Groq</span>
        </div>
    </div>
</footer>

<script>
const AppState = { gstData: null, companyInfo: null, indexedDomain: null, nseSymbol: '' };

function showLoading(show, text='Processing...', sub='') {
    document.getElementById('loadingOverlay').classList.toggle('show', show);
    document.getElementById('loadingText').textContent = text;
    document.getElementById('loadingSubtext').textContent = sub;
}
function showAlert(id, message, type='error') {
    const el = document.getElementById(id);
    el.className = `alert-custom show alert-${type}`;
    el.innerHTML = `<i class="fas fa-${type==='error'?'exclamation-circle':type==='info'?'info-circle':'check-circle'} me-1"></i>${message}`;
    if (type === 'error') setTimeout(() => el.classList.remove('show'), 5000);
}
function showPage(page) {
    document.querySelectorAll('.page-section').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-link-custom').forEach(n => n.classList.remove('active'));
    document.getElementById(`page-${page}`).classList.add('active');
    document.querySelector(`[data-page="${page}"]`)?.classList.add('active');
    window.scrollTo({top:0,behavior:'smooth'});
}
function setPipelineStep(stepId, state, detail='') {
    const el = document.getElementById(stepId);
    if (!el) return;
    el.className = 'pipeline-step ' + state;
    const icon = el.querySelector('.step-icon i');
    if (state === 'active') icon.className = 'fas fa-spinner fa-spin';
    else if (state === 'done') icon.className = 'fas fa-check';
    const detailEl = el.querySelector('.step-detail');
    if (detailEl) detailEl.textContent = detail;
}

document.getElementById('websiteUrl').addEventListener('blur', async function() {
    const url = this.value.trim();
    if (!url || !url.includes('.')) return;
    try {
        const domain = url.replace('https://','').replace('http://','').split('/')[0];
        const res  = await fetch(`/api/check-index?domain=${encodeURIComponent(domain)}`);
        const data = await res.json();
        const cs   = document.getElementById('cacheStatus');
        const cst  = document.getElementById('cacheStatusText');
        const rb   = document.getElementById('reindexBtn');
        cs.style.display = 'block';
        if (data.indexed) {
            cst.innerHTML = `<span class="cache-badge"><i class="fas fa-check-circle me-1"></i>Already indexed — ${data.chunk_count} chunks</span>`;
            rb.style.display = 'inline-block';
            document.getElementById('extractBtn').disabled = false;
            document.getElementById('indexBtn').innerHTML = '<i class="fas fa-check"></i> Already Indexed';
            AppState.indexedDomain = domain;
        } else {
            cst.innerHTML = `<span style="color:#94a3b8"><i class="fas fa-circle-dot me-1"></i>Not indexed yet</span>`;
            rb.style.display = 'none';
        }
    } catch(e) {}
});

document.getElementById('gstForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const gstin = document.getElementById('gstNumber').value.trim().toUpperCase();
    if (!/^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$/.test(gstin))
        return showAlert('gstAlert','Invalid GSTIN format');
    showLoading(true,'Verifying GST...','Connecting to GST portal');
    try {
        const res  = await fetch('/api/verify-gst',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gstin})});
        const data = await res.json();
        if (data.success) { AppState.gstData = data; renderGstResults(data); showAlert('gstAlert','GST verified!','success'); }
        else showAlert('gstAlert', data.error || 'Verification failed');
    } catch(e) { showAlert('gstAlert','Network error'); }
    showLoading(false);
});
document.getElementById('gstNumber').addEventListener('input', e => e.target.value = e.target.value.toUpperCase());

function renderGstResults(data) {
    const sc = data.status?.toLowerCase() === 'active' ? 'active' : 'inactive';
    document.getElementById('gstResultsContent').innerHTML = `
        <div class="company-banner green">
            <h5>${data.legal_name||'N/A'}</h5>
            ${data.trade_name?`<p class="trade">${data.trade_name}</p>`:''}
            <div class="meta"><span><i class="fas fa-fingerprint me-1"></i>${data.gstin}</span><span><i class="fas fa-map-marker-alt me-1"></i>${data.state||'N/A'}</span></div>
        </div>
        <div class="info-grid-mini">
            <div class="info-item-mini ${sc==='active'?'success':''}"><label>Status</label><div class="value"><span class="status-badge ${sc}"><i class="fas fa-circle" style="font-size:6px"></i> ${data.status||'Unknown'}</span></div></div>
            <div class="info-item-mini"><label>Business Type</label><div class="value">${data.business_type||'N/A'}</div></div>
            <div class="info-item-mini"><label>PAN</label><div class="value">${data.pan||'N/A'}</div></div>
            <div class="info-item-mini"><label>Registered</label><div class="value">${data.reg_date||'N/A'}</div></div>
        </div>`;
    document.getElementById('gstResults').classList.add('show');
}
function clearGstResults() {
    AppState.gstData = null;
    document.getElementById('gstResults').classList.remove('show');
    document.getElementById('gstForm').reset();
    document.getElementById('gstBtn').innerHTML = '<i class="fas fa-search"></i> Verify GST';
    document.getElementById('gstAlert').classList.remove('show');
}

async function startIndexing(forceReindex=false) {
    const url  = document.getElementById('websiteUrl').value.trim();
    if (!url || !url.includes('.')) return showAlert('websiteAlert','Enter a valid website URL');

    document.getElementById('pipelineProgress').style.display = 'block';
    document.getElementById('indexBtn').disabled  = true;
    document.getElementById('extractBtn').disabled = true;

    ['step-crawl','step-chunk','step-embed','step-store'].forEach(s => setPipelineStep(s,'pending'));
    setPipelineStep('step-crawl','active','starting...');

    const stepTimings = [
        { id:'step-crawl',  delay:0,    detail:'fetching pages with Playwright...' },
        { id:'step-chunk',  delay:8000, detail:'splitting by h1/h2/h3 sections...' },
        { id:'step-embed',  delay:14000,detail:'generating 384-dim vectors...' },
        { id:'step-store',  delay:19000,detail:'writing to ChromaDB...' },
    ];
    const timers = [];
    stepTimings.forEach((s,i) => {
        const t = setTimeout(() => {
            if (i > 0) setPipelineStep(stepTimings[i-1].id,'done');
            setPipelineStep(s.id,'active',s.detail);
        }, s.delay);
        timers.push(t);
    });

    showAlert('websiteAlert','Deep crawl started — this takes 2–5 minutes for a full site','info');

    try {
        const res  = await fetch('/api/index-company',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({website_url:url, force_reindex:forceReindex})
        });
        const data = await res.json();
        timers.forEach(t => clearTimeout(t));

        if (data.success) {
            ['step-crawl','step-chunk','step-embed','step-store'].forEach(s => setPipelineStep(s,'done'));
            AppState.indexedDomain = data.domain;
            renderIndexResults(data);
            document.getElementById('extractBtn').disabled = false;
            document.getElementById('indexBtn').innerHTML = '<i class="fas fa-check"></i> Indexed';
            const msg = data.already_cached
                ? `Using cached index — ${data.chunks_stored} chunks`
                : `Indexed ${data.pages_crawled} pages → ${data.chunks_stored} chunks stored`;
            showAlert('websiteAlert', msg, 'success');
        } else {
            ['step-crawl','step-chunk','step-embed','step-store'].forEach(s => document.getElementById(s).classList.remove('active'));
            showAlert('websiteAlert', data.error || 'Indexing failed');
            document.getElementById('indexBtn').disabled = false;
        }
    } catch(e) {
        timers.forEach(t => clearTimeout(t));
        showAlert('websiteAlert','Network error during indexing');
        document.getElementById('indexBtn').disabled = false;
    }
}

function reindexSite() { startIndexing(true); }

function renderIndexResults(data) {
    const cached = data.already_cached;
    document.getElementById('indexResultsContent').innerHTML = `
        <div style="display:flex;flex-wrap:wrap;gap:.75rem;margin-top:.5rem">
            <div class="info-item-mini" style="flex:1;min-width:120px">
                <label>Pages crawled</label>
                <div class="value">${cached ? '— (cached)' : data.pages_crawled}</div>
            </div>
            <div class="info-item-mini success" style="flex:1;min-width:120px">
                <label>Chunks in DB</label>
                <div class="value">${data.chunks_stored}</div>
            </div>
            <div class="info-item-mini" style="flex:1;min-width:120px">
                <label>Domain</label>
                <div class="value" style="font-size:.75rem;word-break:break-all">${data.domain}</div>
            </div>
        </div>
        ${cached ? '<p style="font-size:.78rem;color:#64748b;margin-top:.5rem"><i class="fas fa-bolt me-1 text-warning"></i>Loaded from cache — click Re-index to refresh</p>' : ''}
    `;
    document.getElementById('indexResults').classList.add('show');
}

async function startExtraction() {
    const url         = document.getElementById('websiteUrl').value.trim();
    const companyName = document.getElementById('companyName').value.trim();
    if (!url) return showAlert('websiteAlert','Enter website URL');

    showLoading(true,'Running RAG extraction...','Querying vector store → synthesising with Groq');
    try {
        const res  = await fetch('/api/extract-company',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({website_url:url, company_name:companyName})
        });
        const data = await res.json();
        if (data.success) {
            AppState.companyInfo = data;
            renderWebsitePreview(data);
            renderCompanyPage(data);
            document.getElementById('navCompany').disabled = false;
            showAlert('websiteAlert','Intelligence extraction complete!','success');
            document.getElementById('extractBtn').innerHTML = '<i class="fas fa-check"></i> Extracted';
        } else {
            showAlert('websiteAlert', data.error || 'Extraction failed');
        }
    } catch(e) { showAlert('websiteAlert','Network error during extraction'); }
    showLoading(false);
}

function renderWebsitePreview(data) {
    const ov = data.company_overview || {};
    document.getElementById('websiteResultsPreview').innerHTML = `
        <div class="company-banner blue">
            <h5>${ov.name||'Company'}</h5>
            ${ov.tagline?`<p class="trade">"${ov.tagline}"</p>`:''}
            <div class="meta">
                ${ov.industry?`<span><i class="fas fa-industry me-1"></i>${ov.industry}</span>`:''}
                ${ov.headquarters?`<span><i class="fas fa-map-marker-alt me-1"></i>${ov.headquarters}</span>`:''}
            </div>
        </div>
        <p style="font-size:.85rem;color:#475569">${(ov.description||'').substring(0,220)}...</p>
        <div class="rag-meta">
            <strong><i class="fas fa-database me-1"></i>RAG stats:</strong>
            ${data.chunks_in_db||0} chunks indexed · ${data.rag_context_chars||0} chars of context synthesised
        </div>`;
    document.getElementById('websiteResults').classList.add('show');
}

function clearWebsiteResults() {
    AppState.companyInfo = null;
    ['websiteResults','indexResults'].forEach(id => document.getElementById(id).classList.remove('show'));
    document.getElementById('websiteForm')?.reset?.();
    document.getElementById('companyName').value = '';
    document.getElementById('websiteUrl').value  = '';
    document.getElementById('homeNseSymbol').value = '';
    document.getElementById('extractBtn').innerHTML = '<i class="fas fa-magic"></i> Step 2 — Extract Intelligence';
    document.getElementById('indexBtn').innerHTML   = '<i class="fas fa-database"></i> Step 1 — Index &amp; Analyze';
    document.getElementById('indexBtn').disabled    = false;
    document.getElementById('extractBtn').disabled  = true;
    document.getElementById('pipelineProgress').style.display = 'none';
    document.getElementById('cacheStatus').style.display = 'none';
    document.getElementById('websiteAlert').classList.remove('show');
    document.getElementById('navCompany').disabled = true;
    AppState.indexedDomain = null;
    AppState.nseSymbol = '';
    clearNavReady('navFinancials');
    clearNavReady('navLegal');
}

function markNavReady(navId) {
    const el = document.getElementById(navId);
    if (el && !el.querySelector('.nav-ready-dot')) {
        el.insertAdjacentHTML('beforeend', ' <span class="nav-ready-dot" title="Analysis ready" style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#4ade80;margin-left:4px;vertical-align:middle;"></span>');
    }
}
function clearNavReady(navId) {
    const el = document.getElementById(navId);
    const dot = el?.querySelector('.nav-ready-dot');
    if (dot) dot.remove();
}

async function runFullCompanyAnalysis() {
    const companyName = document.getElementById('companyName').value.trim();
    const websiteUrl   = document.getElementById('websiteUrl').value.trim();
    const nseSymbol     = document.getElementById('homeNseSymbol').value.trim();

    if (!websiteUrl || !websiteUrl.includes('.')) return showAlert('websiteAlert','Enter a valid website URL');

    AppState.nseSymbol = nseSymbol;
    clearNavReady('navFinancials');
    clearNavReady('navLegal');

    document.getElementById('finCompanyName').value = companyName;
    document.getElementById('finNseSymbol').value    = nseSymbol;
    document.getElementById('finWebsiteUrl').value   = websiteUrl;
    document.getElementById('legalCompanyInput').value = companyName;

    startIndexing();

    if (companyName) {
        runFinancialsAnalysis(true).then(() => markNavReady('navFinancials')).catch(() => {});
        runLegalAnalysis(true).then(() => markNavReady('navLegal')).catch(() => {});
    } else {
        showAlert('websiteAlert', 'Tip: add a Company Name too, so Financials & Legal can auto-run in the background', 'info');
    }
}

function renderCompanyPage(data) {
    const ov   = data.company_overview     || {};
    const ps   = data.products_services    || {};
    const lead = data.leadership_team      || [];
    const ct   = data.contact_information  || {};
    const hi   = data.key_highlights       || [];
    const aw   = data.awards_recognition   || [];
    const cp   = data.clients_partners     || {};
    const conf = data.extraction_confidence|| 'Medium';

    const ok = v => v && v !== 'Not found' && v !== 'Not Available' && (Array.isArray(v)?v.length>0:true);
    const init = n => n?n.charAt(0).toUpperCase():'?';

    const html = `
    <div class="company-page-header fade-in">
        <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
            <div>
                <h1>${ov.name||'Company Profile'}</h1>
                ${ok(ov.tagline)?`<p class="tagline">"${ov.tagline}"</p>`:''}
            </div>
            <span class="confidence-badge ${conf.toLowerCase()}"><i class="fas fa-chart-simple"></i> ${conf} confidence</span>
        </div>
        <div class="meta">
            ${ok(ov.industry)?`<div class="meta-item"><i class="fas fa-industry"></i>${ov.industry}</div>`:''}
            ${ok(ov.founded_year)?`<div class="meta-item"><i class="fas fa-calendar"></i>Est. ${ov.founded_year}</div>`:''}
            ${ok(ov.headquarters)?`<div class="meta-item"><i class="fas fa-map-marker-alt"></i>${ov.headquarters}</div>`:''}
            ${ok(ov.employee_count)?`<div class="meta-item"><i class="fas fa-users"></i>${ov.employee_count}</div>`:''}
        </div>
    </div>

    <div class="row">
    <div class="col-lg-8">

        ${ok(ov.description)?`
        <div class="info-card fade-in">
            <h3><i class="fas fa-info-circle"></i> Overview</h3>
            <p>${ov.description}</p>
        </div>`:''}

        ${(ok(ps.main_offerings)||ok(ps.usp))?`
        <div class="info-card fade-in">
            <h3><i class="fas fa-boxes-stacked"></i> Products &amp; Services</h3>
            ${ok(ps.usp)?`<div class="highlight-box"><h5>Key differentiator</h5><p>${ps.usp}</p></div>`:''}
            ${ok(ps.main_offerings)?`<ul>${ps.main_offerings.filter(x=>x&&x!=='Not Available').map(x=>`<li><i class="fas fa-check"></i>${x}</li>`).join('')}</ul>`:''}
            ${ok(ps.target_markets)?`<div class="mt-2">${ps.target_markets.filter(x=>x&&x!=='Not Available').map(x=>`<span class="tag">${x}</span>`).join('')}</div>`:''}
        </div>`:''}

        ${(lead.length>0&&lead[0]?.name&&lead[0].name!=='Not found')?`
        <div class="info-card fade-in">
            <h3><i class="fas fa-user-tie"></i> Leadership Team</h3>
            <div class="team-grid">
                ${lead.filter(l=>l.name&&l.name!=='Not found').map(l=>`
                <div class="team-card">
                    <div class="avatar">${init(l.name)}</div>
                    <div class="info"><h5>${l.name}</h5><p>${l.designation||''}</p></div>
                </div>`).join('')}
            </div>
        </div>`:''}

        ${(ok(cp.notable_clients)||ok(cp.partners))?`
        <div class="info-card fade-in">
            <h3><i class="fas fa-handshake"></i> Clients &amp; Partners</h3>
            ${ok(cp.notable_clients)?`<p class="mb-1" style="font-size:.8rem;color:#64748b;font-weight:600">CLIENTS</p><div>${cp.notable_clients.filter(x=>x&&x!=='Not Available').map(x=>`<span class="tag">${x}</span>`).join('')}</div>`:''}
            ${ok(cp.partners)?`<p class="mb-1 mt-2" style="font-size:.8rem;color:#64748b;font-weight:600">PARTNERS</p><div>${cp.partners.filter(x=>x&&x!=='Not Available').map(x=>`<span class="tag">${x}</span>`).join('')}</div>`:''}
        </div>`:''}

        <div class="qa-card fade-in">
            <h3><i class="fas fa-comments"></i> Ask About This Company</h3>
            <div class="qa-suggestions" id="qaSuggestions">
                <span class="qa-chip" onclick="askSuggested(this)">What does the company do?</span>
                <span class="qa-chip" onclick="askSuggested(this)">Who are the founders?</span>
                <span class="qa-chip" onclick="askSuggested(this)">What products do they offer?</span>
                <span class="qa-chip" onclick="askSuggested(this)">Where are they located?</span>
                <span class="qa-chip" onclick="askSuggested(this)">Who are their clients?</span>
            </div>
            <div class="qa-chat" id="qaChat"></div>
            <div class="qa-input-row">
                <input type="text" id="qaInput" placeholder="Ask anything about the company..."
                       onkeydown="if(event.key==='Enter'){event.preventDefault();sendQuestion();}">
                <button class="qa-send-btn" id="qaSendBtn" onclick="sendQuestion()">
                    <i class="fas fa-paper-plane"></i>
                </button>
            </div>
        </div>

    </div>
    <div class="col-lg-4">

        ${ok(hi)?`
        <div class="info-card fade-in">
            <h3><i class="fas fa-star"></i> Key Highlights</h3>
            <ul>${hi.filter(h=>h&&h!=='Not Available').map(h=>`<li><i class="fas fa-star" style="color:#f59e0b"></i>${h}</li>`).join('')}</ul>
        </div>`:''}

        <div class="info-card fade-in">
            <h3><i class="fas fa-address-book"></i> Contact</h3>
            <div class="contact-grid">
                ${ok(ct.registered_office)?`<div class="contact-item" style="grid-column:1/-1"><i class="fas fa-building"></i><div><div class="label">Address</div><div class="value">${ct.registered_office}</div></div></div>`:''}
                ${ok(ct.phone)?`<div class="contact-item"><i class="fas fa-phone"></i><div><div class="label">Phone</div><div class="value">${Array.isArray(ct.phone)?ct.phone.join(', '):ct.phone}</div></div></div>`:''}
                ${ok(ct.email)?`<div class="contact-item"><i class="fas fa-envelope"></i><div><div class="label">Email</div><div class="value">${Array.isArray(ct.email)?ct.email.join(', '):ct.email}</div></div></div>`:''}
            </div>
        </div>

        ${ok(aw)?`
        <div class="info-card fade-in">
            <h3><i class="fas fa-trophy"></i> Awards &amp; Recognition</h3>
            <ul>${aw.filter(a=>a&&a!=='Not Available').map(a=>`<li><i class="fas fa-award" style="color:#f59e0b"></i>${a}</li>`).join('')}</ul>
        </div>`:''}

        <div class="info-card fade-in" style="background:#f8fafc;font-size:.78rem;">
            <p class="text-muted mb-2"><i class="fas fa-database me-1"></i><strong>RAG Pipeline Info</strong></p>
            <p style="margin:.2rem 0"><strong>Domain:</strong> ${data.domain||''}</p>
            <p style="margin:.2rem 0"><strong>Chunks indexed:</strong> ${data.chunks_in_db||0}</p>
            <p style="margin:.2rem 0"><strong>Context used:</strong> ${data.rag_context_chars||0} chars</p>
            <p style="margin:.2rem 0"><strong>Extracted:</strong> ${new Date(data.extraction_timestamp).toLocaleString()}</p>
        </div>

    </div>
    </div>`;

    document.getElementById('companyPageContent').innerHTML = html;
}

function askSuggested(el) {
    document.getElementById('qaInput').value = el.textContent;
    sendQuestion();
}

function appendQaMessage(role, text, sources) {
    const chat = document.getElementById('qaChat');
    if (!chat) return null;
    const wrap = document.createElement('div');
    wrap.className = 'qa-msg ' + role;

    const avatar = document.createElement('div');
    avatar.className = 'qa-avatar ' + role;
    avatar.innerHTML = role === 'user'
        ? '<i class="fas fa-user"></i>'
        : '<i class="fas fa-robot"></i>';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;

    if (sources && sources.length) {
        const src = document.createElement('div');
        src.className = 'qa-sources';
        src.innerHTML = '<strong><i class="fas fa-link me-1"></i>Sources:</strong>' +
            sources.map(s => `<a href="${s.url}" target="_blank">${s.section_path || s.url}</a>`).join('');
        bubble.appendChild(src);
    }

    wrap.appendChild(avatar);
    wrap.appendChild(bubble);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
    return wrap;
}

async function sendQuestion() {
    const input = document.getElementById('qaInput');
    const btn   = document.getElementById('qaSendBtn');
    if (!input) return;
    const question = input.value.trim();
    if (!question) return;

    const url = document.getElementById('websiteUrl').value.trim()
                || (AppState.companyInfo && AppState.companyInfo.website_url)
                || AppState.indexedDomain;
    if (!url) {
        appendQaMessage('bot', "Please index a company website first (Home page → Deep Website RAG).");
        return;
    }

    appendQaMessage('user', question);
    input.value = '';
    btn.disabled = true;

    const typing = appendQaMessage('bot', 'Thinking...');
    if (typing) typing.classList.add('qa-typing');

    try {
        const res = await fetch('/api/ask-question', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ website_url: url, question })
        });
        const data = await res.json();
        if (typing) typing.remove();

        if (data.success) {
            appendQaMessage('bot', data.answer, data.sources);
        } else {
            appendQaMessage('bot', '⚠ ' + (data.error || 'Could not answer the question.'));
        }
    } catch (e) {
        if (typing) typing.remove();
        appendQaMessage('bot', '⚠ Network error. Please try again.');
    } finally {
        btn.disabled = false;
        input.focus();
    }
}

function fmtNum(v) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    if (Number.isNaN(n)) return v;
    return n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

const Fin2 = {
    companyName: '', nseSymbol: '', websiteUrl: '',
    screener: null,
    nse: null,
    period: '6mo',
    autoDocs: [],
    uploadedDocs: [],
};

function fmtCr(v) {
    if (v === null || v === undefined || v === '') return '—';
    return v;
}

function fin2FindRow(section, patterns) {
    if (!section || !section.rows) return null;
    const rows = section.rows;
    for (const p of patterns) {
        const hit = rows.find(r => (r.particular || '').toLowerCase().includes(p));
        if (hit) return hit;
    }
    return null;
}

function fin2SumRows(label, rowsArr, years) {
    const rows = rowsArr.filter(Boolean);
    if (!rows.length) return null;
    const values = {};
    (years || []).forEach(y => {
        let total = null, any = false;
        rows.forEach(r => {
            const n = fin2ParseNum(r.values ? r.values[y] : null);
            if (n !== null) { total = (total || 0) + n; any = true; }
        });
        values[y] = any ? total.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : null;
    });
    return { particular: label, values };
}

function fin2GetTotalAssetsRow(block) {
    if (!block || !block.balance_sheet) return null;
    const direct = fin2FindRow(block.balance_sheet,
        ['total assets', 'total asset', 'assets total']);
    if (direct) return direct;
    const liab = fin2FindRow(block.balance_sheet,
        ['total liabilities', 'total liability', 'liabilities total']);
    if (liab) return { ...liab, particular: 'Total Assets' };
    const bsYears = block.balance_sheet.years || [];
    const fixedAssets  = fin2FindRow(block.balance_sheet, ['fixed assets', 'tangible assets', 'ppe']);
    const cwip         = fin2FindRow(block.balance_sheet, ['cwip', 'capital work']);
    const investments  = fin2FindRow(block.balance_sheet, ['investments']);
    const otherAssets  = fin2FindRow(block.balance_sheet, ['other assets']);
    return fin2SumRows('Total Assets', [fixedAssets, cwip, investments, otherAssets], bsYears);
}

function fin2GetTotalEquityRow(block) {
    if (!block || !block.balance_sheet) return null;
    const direct = fin2FindRow(block.balance_sheet,
        ['total equity', 'net worth', "shareholder's fund", 'shareholders fund',
         'shareholders funds', 'equity funds', 'total shareholders']);
    if (direct) return direct;
    const bsYears = block.balance_sheet.years || [];
    const equityCapital = fin2FindRow(block.balance_sheet,
        ['equity capital', 'share capital', 'equity share']);
    const reserves = fin2FindRow(block.balance_sheet,
        ['reserves', 'surplus', 'retained earnings']);
    return fin2SumRows('Total Equity', [equityCapital, reserves], bsYears);
}

function fin2LatestVal(row, sectionYears) {
    if (!row || !sectionYears || !sectionYears.length) return null;
    for (let i = sectionYears.length - 1; i >= 0; i--) {
        const v = row.values?.[sectionYears[i]];
        if (v !== undefined && v !== null && v !== '' && v !== '0') return { val: v, yr: sectionYears[i] };
    }
    return null;
}
function fin2PrevVal(row, sectionYears, latestYr) {
    if (!row || !sectionYears || !sectionYears.length || !latestYr) return null;
    const idx = sectionYears.indexOf(latestYr);
    if (idx <= 0) return null;
    const v = row.values?.[sectionYears[idx - 1]];
    return (v !== undefined && v !== null && v !== '') ? { val: v, yr: sectionYears[idx - 1] } : null;
}

function fin2RowValues(row, years) {
    if (!row) return years.map(() => null);
    return years.map(y => row.values ? row.values[y] : null);
}
function fin2ParseNum(v) {
    if (v === null || v === undefined) return null;
    const n = parseFloat(String(v).replace(/,/g, ''));
    return Number.isNaN(n) ? null : n;
}
function fin2Pct(curr, prev) {
    const c = fin2ParseNum(curr), p = fin2ParseNum(prev);
    if (c === null || p === null || p === 0) return null;
    return ((c - p) / Math.abs(p)) * 100;
}
function fin2DeltaHtml(pct) {
    if (pct === null) return `<span class="fin2-kpi-delta flat">—</span>`;
    const dir = pct >= 0 ? 'up' : 'down';
    const icon = pct >= 0 ? 'fa-arrow-up' : 'fa-arrow-down';
    return `<span class="fin2-kpi-delta ${dir}"><i class="fa-solid ${icon}" style="font-size:.6rem"></i>${Math.abs(pct).toFixed(1)}%</span>`;
}

async function runFinancialsAnalysis(silent=false) {
    const companyName = document.getElementById('finCompanyName').value.trim();
    const nseSymbol    = document.getElementById('finNseSymbol').value.trim();
    const websiteUrl   = document.getElementById('finWebsiteUrl').value.trim();

    if (!companyName) { if (!silent) showAlert('fin2Alert', 'Enter a company name to analyze'); return; }

    Fin2.companyName = companyName;
    Fin2.nseSymbol   = nseSymbol;
    Fin2.websiteUrl  = websiteUrl;
    Fin2.autoDocs    = [];
    Fin2.uploadedDocs = [];
    renderFin2Docs();

    const btn = document.getElementById('finAnalyzeBtn');
    btn.disabled = true;
    if (!silent) showLoading(true, 'Analyzing company...', 'Fetching Screener.in fundamentals & NSE data');

    const tasks = [ fetchScreenerFinancials() ];
    if (nseSymbol)  tasks.push(fetchNseData(Fin2.period));
    if (websiteUrl) tasks.push(findAnnualReports());
    await Promise.allSettled(tasks);

    document.getElementById('fin2Empty').style.display = 'none';
    document.getElementById('fin2Hero').classList.add('show');
    document.getElementById('fin2KpiGrid').classList.add('show');
    document.getElementById('fin2TabsRow').classList.add('show');
    document.getElementById('fin2Layout').classList.add('show');
    renderFin2Hero();
    renderFin2Kpis();

    btn.disabled = false;
    if (!silent) showLoading(false);
}

function renderFin2Hero() {
    document.getElementById('fin2HeroName').textContent = Fin2.screener?.matched_name || Fin2.companyName;
    const metaBits = [];
    if (Fin2.nseSymbol) metaBits.push(`<span><i class="fa-solid fa-bolt"></i>NSE: ${Fin2.nseSymbol.toUpperCase()}</span>`);
    if (Fin2.nse?.sector || Fin2.nse?.industry) metaBits.push(`<span><i class="fa-regular fa-building"></i>${Fin2.nse.industry || Fin2.nse.sector}</span>`);
    document.getElementById('fin2HeroMeta').innerHTML = metaBits.join('') || `<span>No sector data available</span>`;

    const block = Fin2.screener?.consolidated || Fin2.screener?.standalone;
    const years = block?.profit_loss?.years || [];
    document.getElementById('fin2FyPill').textContent = years.length ? years[years.length - 1] : '—';

    const mockReasons = [];
    if (Fin2.screener?._mock) mockReasons.push(`Screener.in: ${Fin2.screener._screener_error || 'live fetch failed'}`);
    if (Fin2.nse?._mock)      mockReasons.push('NSE/Yahoo Finance: live fetch failed');
    const isMock = mockReasons.length > 0;

    const confEl = document.getElementById('fin2Confidence');
    confEl.className = 'fin2-confidence' + (isMock ? ' mock' : '');
    confEl.title = mockReasons.join(' | ');
    confEl.innerHTML = isMock
        ? `<i class="fa-solid fa-triangle-exclamation"></i><span>Mock data (live fetch unavailable)</span>`
        : `<i class="fa-solid fa-shield-halved"></i><span>Live data</span>`;

    const nseLink = document.getElementById('fin2NseLink');
    if (Fin2.screener?.screener_url) {
        nseLink.href = Fin2.screener.screener_url;
        nseLink.style.display = 'inline-flex';
        nseLink.querySelector('span').textContent = 'View on Screener.in';
    } else {
        nseLink.style.display = 'none';
    }
}

function renderFin2Kpis() {
    const block = Fin2.screener?.consolidated || Fin2.screener?.standalone;
    const grid = document.getElementById('fin2KpiGrid');
    if (!block) { grid.innerHTML = ''; return; }

    const plYears  = block.profit_loss?.years  || [];
    const bsYears  = block.balance_sheet?.years || plYears;
    const ratYears = block.ratios?.years        || plYears;

    const revenue   = fin2FindRow(block.profit_loss, ['sales', 'revenue']);
    const netProfit = fin2FindRow(block.profit_loss, ['net profit']);
    const eps       = fin2FindRow(block.profit_loss, ['eps']) || fin2FindRow(block.ratios, ['eps']);
    const assets    = fin2GetTotalAssetsRow(block);
    const equity    = fin2GetTotalEquityRow(block);
    const marketCap = Fin2.nse?.market_cap_cr;

    function kpiVal(row, sectionYears, prefix, suffix) {
        if (!row) return { txt: '—', delta: fin2DeltaHtml(null), prevYr: sectionYears[sectionYears.length-2] || '' };
        const latest = fin2LatestVal(row, sectionYears);
        if (!latest) return { txt: '—', delta: fin2DeltaHtml(null), prevYr: '' };
        const prev = fin2PrevVal(row, sectionYears, latest.yr);
        const pct  = prev ? fin2Pct(latest.val, prev.val) : null;
        return {
            txt:    `${prefix}${latest.val}${suffix}`,
            delta:  fin2DeltaHtml(pct),
            prevYr: prev ? prev.yr : '',
        };
    }

    const rev  = kpiVal(revenue,   plYears,  '₹', ' Cr');
    const np   = kpiVal(netProfit, plYears,  '₹', ' Cr');
    const ast  = kpiVal(assets,    bsYears,  '₹', ' Cr');
    const eq   = kpiVal(equity,    bsYears,  '₹', ' Cr');
    const epsV = kpiVal(eps,       eps ? (block.profit_loss?.rows?.find(r=>r===eps) ? plYears : ratYears) : plYears, '₹', '');

    const mcapTxt = marketCap ? `₹${Number(marketCap).toLocaleString('en-IN')} Cr` : '—';

    const colorMap = { blue:['#eff6ff','#2563eb'], emerald:['#ecfdf5','#059669'], purple:['#f5f3ff','#7c3aed'], amber:['#fffbeb','#d97706'], violet:['#f5f3ff','#8b5cf6'], cyan:['#ecfeff','#0891b2'] };

    const cards = [
        { icon:'₹',                                                                          color:'blue',    label:'Total Revenue', ...rev  },
        { icon:'<i class="fa-solid fa-chart-line"></i>',                                    color:'emerald', label:'Net Profit',    ...np   },
        { icon:'<i class="fa-solid fa-briefcase"></i>',                                     color:'purple',  label:'Total Assets',  ...ast  },
        { icon:'<i class="fa-regular fa-credit-card"></i>',                                 color:'amber',   label:'Total Equity',  ...eq   },
        { icon:'<i class="fa-solid fa-layer-group"></i>',                                   color:'violet',  label:'EPS',           ...epsV },
        { icon:'<i class="fa-solid fa-scale-balanced"></i>', color:'cyan', label:'Market Cap',
          txt: mcapTxt, delta: `<span class="fin2-kpi-delta flat">NSE</span>`, prevYr: '' },
    ];

    grid.innerHTML = cards.map(c => {
        const [bg, fg] = colorMap[c.color];
        return `<div class="fin2-kpi-card">
            <div class="fin2-kpi-icon" style="background:${bg};color:${fg}">${c.icon}</div>
            <div class="min-w-0">
                <div class="fin2-kpi-label">${c.label}</div>
                <div class="fin2-kpi-value">${c.txt}</div>
                ${c.delta} ${c.prevYr ? `<span style="font-size:.62rem;color:#94a3b8">vs ${c.prevYr}</span>` : ''}
            </div>
        </div>`;
    }).join('');
}

function switchFin2Tab(tab) {
    document.querySelectorAll('.fin2-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.fin2-panel').forEach(p => p.classList.toggle('active', p.id === `fin2Panel-${tab}`));
    if (tab !== 'summary') renderFin2FullTable(tab);
}

function fin2FullTableHtml(section) {
    if (!section || !section.rows || !section.rows.length) return '<p style="font-size:.8rem;color:#94a3b8">No data available for this statement.</p>';
    const years = section.years || [];
    let html = '<div class="fin-table-wrap"><table class="fin2-table"><thead><tr><th>Particulars</th>';
    years.forEach(y => html += `<th>${y}</th>`);
    html += '</tr></thead><tbody>';
    section.rows.forEach(r => {
        const bold = /total|net profit|net worth/i.test(r.particular);
        html += `<tr class="${bold ? 'bold' : ''}"><td>${r.particular}</td>`;
        years.forEach(y => html += `<td>${r.values?.[y] ?? '—'}</td>`);
        html += '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
}

function renderFin2FullTable(tab) {
    const block = Fin2.screener?.consolidated || Fin2.screener?.standalone;
    const map = { balance_sheet: 'balance_sheet', profit_loss: 'profit_loss', cash_flow: 'cash_flow', ratios: 'ratios' };
    const el = document.getElementById(`fin2Full-${tab}`);
    if (!block) { el.innerHTML = '<p style="font-size:.8rem;color:#94a3b8">Analyze a company first.</p>'; return; }
    el.innerHTML = fin2FullTableHtml(block[map[tab]]);
}

function renderFin2SummaryAndMiniTables() {
    const block = Fin2.screener?.consolidated || Fin2.screener?.standalone;
    const summaryEl = document.getElementById('fin2SummaryTable');
    const threeColEl = document.getElementById('fin2ThreeCol');
    if (!block) {
        summaryEl.innerHTML = '<p style="font-size:.8rem;color:#94a3b8">No financial tables available.</p>';
        threeColEl.innerHTML = '';
        return;
    }

    const plYears  = block.profit_loss?.years  || [];
    const bsYears  = block.balance_sheet?.years || [];
    const ratYears = block.ratios?.years        || [];
    const allYears = [...new Set([...plYears, ...bsYears])].slice(-5);
    const summaryYears = allYears;

    const assetsRow = fin2GetTotalAssetsRow(block);
    const rows = [
        ['Total Revenue',  fin2FindRow(block.profit_loss, ['sales','revenue']),         plYears],
        ['EBITDA',         fin2FindRow(block.profit_loss, ['ebitda','operating profit']),plYears],
        ['Net Profit',     fin2FindRow(block.profit_loss, ['net profit']),               plYears],
        ['Total Assets',   assetsRow,                                                    bsYears.length ? bsYears : plYears],
        ['ROE (%)',        fin2FindRow(block.ratios, ['roe']),                           ratYears.length ? ratYears : plYears],
        ['Debt to Equity', fin2FindRow(block.ratios, ['debt to equity','debt/equity']), ratYears.length ? ratYears : plYears],
    ].filter(([, row]) => row);

    if (!rows.length) {
        summaryEl.innerHTML = '<p style="font-size:.8rem;color:#94a3b8">No financial tables available.</p>';
    } else {
        let html = '<div class="fin-table-wrap"><table class="fin2-table"><thead><tr><th>Particulars</th>';
        summaryYears.forEach(y => html += `<th>${y}</th>`);
        html += '</tr></thead><tbody>';
        rows.forEach(([label, row, rowYears]) => {
            html += `<tr><td>${label}</td>`;
            summaryYears.forEach(y => {
                let v = row.values?.[y];
                if (v === undefined) {
                    const yr4 = (y.match(/\d{4}/) || [])[0];
                    if (yr4) {
                        const match = (rowYears || []).find(ry => ry.includes(yr4));
                        if (match) v = row.values?.[match];
                    }
                }
                html += `<td>${v ?? '—'}</td>`;
            });
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        summaryEl.innerHTML = html;
    }

    const miniCard = (title, section) => `
        <div class="fin2-card" style="margin-bottom:0">
            <div class="fin2-card-title"><span>${title}</span><i class="fa-regular fa-circle-question"></i></div>
            ${fin2FullTableHtml(section ? { years: (section.years||[]).slice(-2), rows: (section.rows||[]).map(r => ({particular:r.particular, values:r.values})) } : null)}
        </div>`;
    threeColEl.style.gridTemplateColumns = '1fr';
    threeColEl.innerHTML = `<div class="fin2-3col-grid">
        ${miniCard('Profit &amp; Loss (₹ Cr)', block.profit_loss)}
        ${miniCard('Balance Sheet (₹ Cr)', block.balance_sheet)}
        ${miniCard('Cash Flow (₹ Cr)', block.cash_flow)}
    </div>`;
}

async function fetchScreenerFinancials() {
    try {
        const res  = await fetch('/api/financials/screener', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company_name: Fin2.companyName })
        });
        const data = await res.json();
        Fin2.screener = data;
        if (data._screener_error) {
            showAlert('fin2Alert', `Screener.in unavailable, showing mock data — ${data._screener_error}`, 'error');
        } else if (!data.success) {
            showAlert('fin2Alert', data.error || 'Could not fetch financials', 'error');
        }
        renderFin2SummaryAndMiniTables();
    } catch (e) {
        showAlert('fin2Alert', 'Network error fetching Screener.in data', 'error');
    }
}

async function fetchNseData(period) {
    try {
        const res  = await fetch('/api/financials/nse', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol: Fin2.nseSymbol, period: period || Fin2.period })
        });
        const data = await res.json();
        if (data.success) {
            Fin2.nse = data;
            renderFin2MarketData(data);
            renderFin2Chart(data);
        } else {
            showAlert('fin2Alert', data.error || 'NSE symbol not found', 'error');
        }
    } catch (e) {
        showAlert('fin2Alert', 'Network error fetching NSE data', 'error');
    }
}

document.addEventListener('click', (e) => {
    const btn = e.target.closest('.fin2-period-btn');
    if (!btn) return;
    document.querySelectorAll('.fin2-period-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    Fin2.period = btn.dataset.p;
    if (Fin2.nseSymbol) fetchNseData(Fin2.period);
});

function renderFin2MarketData(data) {
    const rows = [
        ['Current Price (₹)', fmtNum(data.price)],
        ['Change', `${data.change_pct >= 0 ? '+' : ''}${fmtNum(data.change_pct)}%`],
        ['Market Cap (₹ Cr)', fmtNum(data.market_cap_cr)],
        ['52 Week High / Low (₹)', `${fmtNum(data.week52_high)} / ${fmtNum(data.week52_low)}`],
        ['P/E Ratio (x)', fmtNum(data.pe_ratio)],
        ['P/B Ratio (x)', fmtNum(data.pb_ratio)],
        ['EPS (₹)', fmtNum(data.eps)],
        ['Volume', fmtNum(data.volume)],
    ];
    document.getElementById('fin2MarketData').innerHTML = rows.map(([l, v]) =>
        `<div class="fin2-side-row"><span style="color:#64748b">${l}</span><span style="font-weight:700;color:#0f172a">${v}</span></div>`
    ).join('') + (data._mock ? '<div style="margin-top:.5rem"><span class="pill pill-yellow" style="font-size:.65rem">⚠️ Mock data</span></div>' : '');
}

function renderFin2Chart(data) {
    const hist = data.price_history || [];
    const priceRow = document.getElementById('fin2ChartPriceRow');
    const chg = Number(data.change_pct || 0);
    priceRow.innerHTML = `<span style="font-weight:800;color:#1e293b">${(data.symbol||'').toUpperCase()} • ${document.querySelector('.fin2-period-btn.active')?.textContent || ''} • NSE</span>
        <span style="font-weight:800;color:${chg>=0?'#059669':'#dc2626'}">₹${fmtNum(data.price)} ${chg>=0?'+':''}${fmtNum(data.change)} (${chg>=0?'+':''}${fmtNum(chg)}%)</span>`;

    const wrap = document.getElementById('fin2ChartSvgWrap');
    if (!hist.length) {
        wrap.innerHTML = '<p style="font-size:.78rem;color:#94a3b8;padding:2rem 0;text-align:center">No price history available for this period.</p>';
        document.getElementById('fin2ChartFooter').innerHTML = '';
        return;
    }
    const closes = hist.map(h => h.close);
    const max = Math.max(...closes), min = Math.min(...closes);
    const range = (max - min) || 1;
    const W = 500, H = 140;
    const pts = closes.map((c, i) => {
        const x = (i / Math.max(1, closes.length - 1)) * W;
        const y = H - ((c - min) / range) * H;
        return [x, y];
    });
    const linePath = 'M ' + pts.map(p => p.join(',')).join(' L ');
    const areaPath = linePath + ` L ${W},${H} L 0,${H} Z`;
    const up = closes[closes.length-1] >= closes[0];
    const strokeColor = up ? '#2563eb' : '#dc2626';

    wrap.innerHTML = `
        <div style="display:flex">
            <div style="display:flex;flex-direction:column;justify-content:space-between;font-size:9px;color:#94a3b8;font-weight:600;padding-right:6px;height:140px">
                <span>${max.toFixed(0)}</span><span>${((max+min)/2).toFixed(0)}</span><span>${min.toFixed(0)}</span>
            </div>
            <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:140px;overflow:visible">
                <defs><linearGradient id="fin2grad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="${strokeColor}" stop-opacity="0.3" />
                    <stop offset="100%" stop-color="${strokeColor}" stop-opacity="0" />
                </linearGradient></defs>
                <line x1="0" y1="0" x2="${W}" y2="0" stroke="#f1f5f9"/>
                <line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}" stroke="#f1f5f9"/>
                <line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="#f1f5f9"/>
                <path d="${areaPath}" fill="url(#fin2grad)"/>
                <path d="${linePath}" fill="none" stroke="${strokeColor}" stroke-width="2"/>
            </svg>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:9px;color:#94a3b8;font-weight:600;padding-left:26px;margin-top:2px">
            <span>${hist[0]?.date || ''}</span><span>${hist[Math.floor(hist.length/2)]?.date || ''}</span><span>${hist[hist.length-1]?.date || ''}</span>
        </div>`;

    const last = hist[hist.length - 1] || {};
    const totalVol = hist.reduce((s,h) => s + (h.volume||0), 0);
    document.getElementById('fin2ChartFooter').innerHTML = [
        ['Open', fmtNum(last.open)], ['High', fmtNum(Math.max(...hist.map(h=>h.high||0)))],
        ['Low', fmtNum(Math.min(...hist.map(h=>h.low||Infinity)))], ['Prev. Close', fmtNum(data.prev_close)],
        ['Volume', fmtNum(last.volume)], ['Avg Volume', fmtNum(Math.round(totalVol/hist.length))]
    ].map(([l,v]) => `<div><span class="lbl">${l}</span><span class="val">${v}</span></div>`).join('');
}

function renderFin2Docs() {
    const docsEl = document.getElementById('fin2Docs');
    const all = [
        ...Fin2.uploadedDocs.map(d => ({ ...d, source: 'upload' })),
        ...Fin2.autoDocs.map(d => ({ ...d, source: 'auto' })),
    ];
    if (!all.length) {
        docsEl.innerHTML = `<p style="font-size:.75rem;color:#94a3b8">${Fin2.docsNote || 'Add a company website above to auto-find annual reports, or upload a PDF.'}</p>`;
        return;
    }
    docsEl.innerHTML = all.map(l => {
        const safeHref = (l.href || '').replace(/'/g, "\\'");
        const actionHtml = l.analyzed
            ? `<span style="font-size:.62rem;font-weight:700;color:#16a34a;padding:.15rem .5rem;border:1px solid #bbf7d0;border-radius:6px;white-space:nowrap">Analyzed ✓</span>`
            : `<button class="fin2-doc-open" style="background:none;cursor:pointer" onclick="analyzePdfUrl('${safeHref}')">Analyze</button>`;
        const sourceTag = l.source === 'upload'
            ? `<div style="font-size:.6rem;color:#7c3aed;font-weight:700;letter-spacing:.03em">UPLOADED</div>` : '';
        return `
            <div class="fin2-doc-item">
                <div style="display:flex;align-items:center;gap:.6rem;min-width:0">
                    <div class="fin2-doc-icon"><i class="fa-regular fa-file-pdf"></i></div>
                    <div style="min-width:0">
                        <div class="fin2-doc-name" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px">${l.text || 'Financial Document'}</div>
                        ${sourceTag}
                    </div>
                </div>
                <div style="display:flex;gap:.3rem;align-items:center">
                    ${l.href ? `<a class="fin2-doc-open" href="${l.href}" target="_blank">Open</a>` : ''}
                    ${actionHtml}
                </div>
            </div>`;
    }).join('');
}

async function findAnnualReports() {
    try {
        const res  = await fetch('/api/financials/find-reports', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ website_url: Fin2.websiteUrl })
        });
        const data = await res.json();
        if (data.success && data.report_links && data.report_links.length) {
            Fin2.autoDocs = data.report_links.map(l => ({ text: l.text, href: l.href, analyzed: false }));
            Fin2.docsNote = '';
        } else {
            Fin2.autoDocs = [];
            Fin2.docsNote = data.note || data.error || 'No report links found — try uploading a PDF manually.';
        }
    } catch (e) {
        Fin2.autoDocs = [];
        Fin2.docsNote = 'Network error while scanning website.';
    }
    renderFin2Docs();
}

async function analyzePdfUrl(pdfUrl) {
    const statusEl = document.getElementById('fin2UploadStatus');
    statusEl.className = 'fin2-upload-status busy'; statusEl.textContent = 'Downloading & analyzing PDF...';
    showLoading(true, 'Downloading & analyzing PDF...', 'This can take up to a minute for large reports');
    try {
        const res  = await fetch('/api/financials/analyze-pdf-url', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pdf_url: pdfUrl, company_name: Fin2.companyName })
        });
        const data = await res.json();
        if (data.success) {
            renderFin2AiAnalysis(data);
            const doc = Fin2.autoDocs.find(d => d.href === pdfUrl);
            if (doc) doc.analyzed = true;
            renderFin2Docs();
            statusEl.className = 'fin2-upload-status ready'; statusEl.textContent = 'Analysis complete';
        } else {
            statusEl.className = 'fin2-upload-status'; statusEl.textContent = data.error || 'Analysis failed';
        }
    } catch (e) {
        statusEl.className = 'fin2-upload-status'; statusEl.textContent = 'Network error';
    }
    showLoading(false);
}

async function uploadFinancialPdf() {
    const fileInput = document.getElementById('finPdfFile');
    if (!fileInput.files.length) return;
    const fileName = fileInput.files[0].name;
    const statusEl = document.getElementById('fin2UploadStatus');
    statusEl.className = 'fin2-upload-status busy'; statusEl.textContent = `Analyzing ${fileName}...`;
    showLoading(true, 'Extracting & analyzing...', 'Reading PDF → summarising with AI');
    try {
        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        formData.append('company_name', Fin2.companyName || document.getElementById('finCompanyName').value.trim());
        const res  = await fetch('/api/financials/upload-pdf', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.success) {
            renderFin2AiAnalysis(data);
            Fin2.uploadedDocs.unshift({
                text: data.file_name || fileName,
                href: data.file_url || null,
                analyzed: true,
            });
            renderFin2Docs();
            statusEl.className = 'fin2-upload-status ready'; statusEl.textContent = `✓ ${fileName} analyzed`;
        } else {
            statusEl.className = 'fin2-upload-status'; statusEl.textContent = data.error || 'Analysis failed';
        }
    } catch (e) {
        statusEl.className = 'fin2-upload-status'; statusEl.textContent = 'Network error';
    }
    showLoading(false);
}

function renderFin2AiAnalysis(data) {
    const kf = data.key_financials || {};
    const kr = data.key_ratios || {};
    const yw = data.year_wise_data || [];
    const ok = v => v && v !== 'Not found';

    let html = `<div class="company-banner blue" style="margin-bottom:1rem">
        <h5>${data.report_type || 'Financial Report'}</h5>
        <p class="trade">${data.report_period || ''}${data.pages_analyzed ? ` · ${data.pages_analyzed} pages analyzed` : ''}</p>
    </div>`;

    const metricRows = [['Revenue', kf.revenue], ['Net Profit', kf.net_profit], ['EBITDA', kf.ebitda], ['Net Worth', kf.net_worth]]
        .filter(([, v]) => v && ok(v.value));
    if (metricRows.length) {
        html += '<div class="fin-section-title"><i class="fas fa-sack-dollar"></i>Key Financials</div><div class="metric-mini-grid">';
        metricRows.forEach(([label, v]) => {
            html += `<div class="info-item-mini success"><label>${label}</label><div class="value">${v.value}</div>${v.yoy_change && ok(v.yoy_change) ? `<div style="font-size:.7rem;color:#16a34a;margin-top:2px">${v.yoy_change} YoY</div>` : ''}</div>`;
        });
        html += '</div>';
    }

    const ratioEntries = Object.entries(kr).filter(([, v]) => ok(v));
    if (ratioEntries.length) {
        html += '<div class="fin-section-title"><i class="fas fa-percent"></i>Key Ratios</div><div class="metric-mini-grid">';
        ratioEntries.forEach(([k, v]) => {
            html += `<div class="info-item-mini"><label>${k.replace(/_/g,' ')}</label><div class="value">${v}</div></div>`;
        });
        html += '</div>';
    }

    if (yw.length) {
        html += '<div class="fin-section-title"><i class="fas fa-calendar-days"></i>Year-wise Data</div>';
        const cols = Object.keys(yw[0]);
        html += '<div class="fin-table-wrap"><table class="fin-table"><thead><tr>' + cols.map(c => `<th>${c.replace(/_/g,' ')}</th>`).join('') + '</tr></thead><tbody>';
        yw.forEach(row => { html += '<tr>' + cols.map(c => `<td>${row[c] ?? '—'}</td>`).join('') + '</tr>'; });
        html += '</tbody></table></div>';
    }

    if (ok(data.creditworthiness_summary)) {
        html += `<div class="highlight-box"><h5>Creditworthiness Assessment</h5><p>${data.creditworthiness_summary}</p></div>`;
    }

    const highlights = (data.highlights || []).filter(ok);
    const risks = (data.risk_factors || []).filter(ok);
    if (highlights.length || risks.length) {
        html += '<div class="row mt-2">';
        if (highlights.length) html += '<div class="col-md-6">' + highlights.map(h => `<div class="risk-pill good"><i class="fas fa-check-circle"></i>${h}</div>`).join('') + '</div>';
        if (risks.length) html += '<div class="col-md-6">' + risks.map(r => `<div class="risk-pill bad"><i class="fas fa-triangle-exclamation"></i>${r}</div>`).join('') + '</div>';
        html += '</div>';
    }

    document.getElementById('fin2AiContent').innerHTML = html;
    document.getElementById('fin2AiBox').classList.add('show');
    document.getElementById('fin2AiBox').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

const Legal = {
    company: '',
    allNews: [],
    allCases: [],
    newsOffset: 0,
    casesOffset: 0,
    NEWS_PAGE: 8,
    CASES_PAGE: 6,
    newsRiskFilter: 'all',
    casesCourtFilter: 'all',
};

async function runLegalAnalysis(silent=false) {
    const company = document.getElementById('legalCompanyInput').value.trim();
    if (!company) { if (!silent) showAlert('legalAlert', 'Enter a company name'); return; }
    const daysBack = document.getElementById('legalDaysBack').value;
    const courtPages = document.getElementById('legalCourtPages').value;
    Legal.company = company;
    Legal.newsRiskFilter = 'all';
    Legal.casesCourtFilter = 'all';

    const btn = document.getElementById('legalAnalyzeBtn');
    btn.disabled = true;
    if (!silent) showLoading(true, 'Fetching legal intelligence...', 'Scanning news sources & Indian Kanoon — may take 20–40 s');

    try {
        const res = await fetch('/api/legal/summary', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({company_name: company, days_back: parseInt(daysBack), max_court_pages: parseInt(courtPages)})
        });
        const data = await res.json();
        if (!data.success) {
            if (!silent) showAlert('legalAlert', data.error || 'Analysis failed');
        } else {
            Legal.allNews = data.articles || [];
            Legal.allCases = data.cases || [];
            document.getElementById('legalEmpty').style.display = 'none';
            document.getElementById('legalResultsArea').style.display = 'block';
            renderLegalHero(data);
            renderLegalKpis(data);
            renderLegalFindings(data);
            renderLegalRiskMeter(data);
            resetChips('newsChips');
            resetChips('casesChips');
            Legal.newsRiskFilter = 'all';
            Legal.casesCourtFilter = 'all';
            renderNewsCards();
            renderCaseCards();
            if (!silent) showAlert('legalAlert', 'Legal intelligence loaded successfully', 'success');
        }
    } catch(e) {
        if (!silent) showAlert('legalAlert', 'Network error during legal analysis');
    }
    btn.disabled = false;
    if (!silent) showLoading(false);
}

function renderLegalHero(data) {
    document.getElementById('legalHeroName').textContent = data.company || Legal.company;
}


function renderLegalKpis(data) {
    const kpis = data.kpis || {};
    const cards = [
        {icon: 'fas fa-newspaper', color: ['#eff6ff','#2563eb'], label: 'News Articles', value: kpis.adverse_news_total ?? '—'},
        {icon: 'fas fa-triangle-exclamation', color: ['#fef2f2','#dc2626'], label: 'High Risk Signals', value: kpis.critical_flags ?? '—'},
        {icon: 'fas fa-circle-check', color: ['#f0fdf4','#16a34a'], label: 'Relevant Articles', value: kpis.relevant_signals ?? '—'},
        {icon: 'fas fa-gavel', color: ['#f5f3ff','#7c3aed'], label: 'Court Records', value: kpis.court_records ?? '—'},
    ];
    document.getElementById('legalKpiStrip').innerHTML = cards.map(c => `
        <div class="legal-kpi-card">
            <div class="legal-kpi-icon" style="background:${c.color[0]};color:${c.color[1]}">
                <i class="${c.icon}"></i>
            </div>
            <div>
                <div class="legal-kpi-label">${c.label}</div>
                <div class="legal-kpi-value">${c.value}</div>
            </div>
        </div>`).join('');
}

function renderLegalRiskMeter(data) {
    return;
}

function renderLegalFindings(data) {
    const findings = data.key_findings || [];
    if (!findings.length) { document.getElementById('legalFindings').innerHTML = ''; return; }
    document.getElementById('legalFindings').innerHTML = findings.map(f => `
        <div class="legal-finding ${f.type}">
            <span class="legal-finding-icon">${f.icon || ''}</span>
            <div>
                <div class="legal-finding-title">${f.title}</div>
                <div class="legal-finding-body">${f.body}</div>
            </div>
        </div>`).join('');
}

function _newsMatchesFilters(article) {
    const riskF = Legal.newsRiskFilter;
    const searchQ = (document.getElementById('newsSearchInput')?.value || '').toLowerCase();
    const dateF = document.getElementById('newsDateFilter')?.value || 'all';

    if (riskF === 'low_conf' && article.relevance !== 'low_confidence') return false;
    if (riskF === 'relevant' && article.relevance !== 'relevant') return false;
    if (['high','medium','low'].includes(riskF) && article.risk_level !== riskF) return false;

    if (searchQ && !((article.title||'').toLowerCase().includes(searchQ) || (article.description||'').toLowerCase().includes(searchQ))) return false;

    if (dateF !== 'all' && article.published_at) {
        const pub = new Date(article.published_at);
        const cutoff = new Date();
        cutoff.setDate(cutoff.getDate() - parseInt(dateF));
        if (pub < cutoff) return false;
    }
    return true;
}

function _newsCategory(article) {
    const kws = (article.matched_keywords || []).map(k => k.toLowerCase());
    if (kws.some(k => ['sebi','regulatory','compliance','norms'].includes(k))) return {label:'Regulatory', cls:'badge-reg'};
    if (kws.some(k => ['court','lawsuit','litigation','fir','ed','cbi','fraud','penalty','fine','insolvency','nclt','nclat','tribunal'].includes(k))) return {label:'Legal', cls:'badge-legal2'};
    const t = (article.title||'').toLowerCase();
    if (/esg|green|sustainability|environment/.test(t)) return {label:'ESG', cls:'badge-esg'};
    if (/share|stock|nse|bse|market|price|ipo/.test(t)) return {label:'Markets', cls:'badge-markets'};
    return {label:'Business', cls:'badge-biz'};
}

function _sourceInitials(source) {
    if (!source) return '??';
    const words = source.replace(/[^a-zA-Z ]/g,'').trim().split(/\s+/);
    if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
    return source.substring(0,2).toUpperCase();
}
const SOURCE_COLORS = ['#E62E2D','#005FA8','#1A334E','#901224','#0A3D2E','#4A1942','#1B4F72','#145A32','#6E2F00','#1A237E'];
function _sourceColor(source) {
    if (!source) return '#64748b';
    let h = 0; for (let i=0;i<source.length;i++) h = source.charCodeAt(i) + ((h << 5) - h);
    return SOURCE_COLORS[Math.abs(h) % SOURCE_COLORS.length];
}

function _newsCardHtml(article) {
    const cat = _newsCategory(article);
    const riskCls = {high:'badge-high', medium:'badge-medium', low:'badge-low'}[article.risk_level] || 'badge-neutral';
    const kws = (article.matched_keywords || []).slice(0,3).join(', ');
    const pub = article.published_at ? new Date(article.published_at).toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'numeric'}) : '';
    const initials = _sourceInitials(article.source);
    const bgColor  = _sourceColor(article.source);
    const href = article.url ? `href="${article.url}" target="_blank"` : '';
    const confBadge = article.relevance === 'low_confidence' ? `<span class="legal-news-badge badge-lowconf" style="margin-left:.3rem">Low Conf.</span>` : '';
    return `
    <a class="legal-news-card" ${href} style="text-decoration:none">
        <div class="legal-news-avatar" style="background:${bgColor}">${initials}</div>
        <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:.4rem;margin-bottom:.2rem">
                <div class="legal-news-title">${article.title || '(No title)'}</div>
                <div style="display:flex;gap:.25rem;flex-shrink:0">
                    <span class="legal-news-badge ${cat.cls}">${cat.label}</span>
                    ${confBadge}
                </div>
            </div>
            ${article.description ? `<div class="legal-news-desc">${article.description}</div>` : ''}
            ${kws ? `<div style="font-size:.65rem;color:#94a3b8;margin-bottom:.2rem">Keywords: ${kws}</div>` : ''}
            <div class="legal-news-meta">
                <span><i class="fas fa-calendar" style="font-size:.6rem"></i> ${pub}</span>
                ${pub ? '<span>|</span>' : ''}
                <span>${article.source || 'News source'}</span>
                <span class="legal-news-badge ${riskCls}" style="margin-left:auto">${(article.risk_level||'').charAt(0).toUpperCase()+(article.risk_level||'').slice(1)} Risk</span>
            </div>
        </div>
    </a>`;
}

function renderNewsCards(append) {
    const filtered = Legal.allNews.filter(_newsMatchesFilters);
    const el = document.getElementById('newsCards');
    if (!filtered.length) {
        el.innerHTML = `<div class="legal-empty"><i class="fas fa-newspaper"></i><p>No news matches the current filters.</p></div>`;
        document.getElementById('newsLoadMore').style.display = 'none';
        return;
    }
    const slice = filtered.slice(0, Legal.newsOffset + Legal.NEWS_PAGE);
    el.innerHTML = slice.map(_newsCardHtml).join('');
    document.getElementById('newsLoadMore').style.display = slice.length < filtered.length ? 'block' : 'none';
}

function loadMoreNews() {
    Legal.newsOffset += Legal.NEWS_PAGE;
    renderNewsCards(true);
}
function filterNews() { Legal.newsOffset = 0; renderNewsCards(); }

function setNewsChip(el, risk) {
    document.querySelectorAll('#newsChips .legal-chip').forEach(c => { c.className = 'legal-chip inactive'; });
    el.className = 'legal-chip active';
    Legal.newsRiskFilter = risk;
    Legal.newsOffset = 0;
    renderNewsCards();
}

function _courtLabel(caseItem) {
    const c = ((caseItem.court || '') + ' ' + (caseItem.title || '')).toLowerCase();
    if (c.includes('supreme')) return 'Supreme Court';
    if (c.includes('high court') || /bombay|delhi|calcutta|madras|allahabad|karnataka/.test(c)) return 'High Court';
    if (c.includes('nclt') || c.includes('nclat')) return 'NCLT/NCLAT';
    if (c.includes('ngt') || c.includes('green tribunal')) return 'NGT';
    if (c.includes('tribunal') || c.includes('itat') || c.includes('cestat')) return 'Tribunal';
    return caseItem.court || 'Court';
}

function _caseMatchesFilters(caseItem) {
    const courtF = Legal.casesCourtFilter;
    const searchQ = (document.getElementById('casesSearchInput')?.value || '').toLowerCase();
    if (searchQ && !((caseItem.title||'').toLowerCase().includes(searchQ) || (caseItem.snippet||'').toLowerCase().includes(searchQ))) return false;
    if (courtF === 'all') return true;
    const label = _courtLabel(caseItem).toLowerCase();
    if (courtF === 'supreme' && !label.includes('supreme')) return false;
    if (courtF === 'high' && !label.includes('high')) return false;
    if (courtF === 'nclt' && !label.includes('nclt')) return false;
    if (courtF === 'tribunal' && !label.includes('tribunal') && !label.includes('ngt')) return false;
    return true;
}

function _caseCardHtml(c) {
    const label = _courtLabel(c);
    const pub = c.date ? c.date : '';
    return `
    <div class="legal-case-card">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:.5rem;margin-bottom:.3rem">
            <div class="legal-case-title">${c.title || '(Untitled)'}</div>
            <span class="court-badge">${label}</span>
        </div>
        <div class="legal-case-meta">
            ${pub ? `<span><i class="fas fa-calendar" style="font-size:.6rem"></i> ${pub}</span><span>|</span>` : ''}
            <span><i class="fas fa-gavel" style="font-size:.6rem"></i> ${c.court || label}</span>
        </div>
        ${c.snippet ? `<div class="legal-case-snippet">${c.snippet}</div>` : ''}
        <div class="legal-case-footer">
            ${c.url ? `<a class="legal-case-btn" href="${c.url}" target="_blank"><span>View Details</span><i class="fas fa-arrow-right" style="font-size:.65rem"></i></a>` : ''}
        </div>
    </div>`;
}

function renderCaseCards() {
    const filtered = Legal.allCases.filter(_caseMatchesFilters);
    const el = document.getElementById('casesCards');
    if (!filtered.length) {
        el.innerHTML = `<div class="legal-empty"><i class="fas fa-gavel"></i><p>No court cases match the current filters.</p></div>`;
        document.getElementById('casesLoadMore').style.display = 'none';
        return;
    }
    const slice = filtered.slice(0, Legal.casesOffset + Legal.CASES_PAGE);
    el.innerHTML = slice.map(_caseCardHtml).join('');
    document.getElementById('casesLoadMore').style.display = slice.length < filtered.length ? 'block' : 'none';
}

function loadMoreCases() { Legal.casesOffset += Legal.CASES_PAGE; renderCaseCards(); }
function filterCases() { Legal.casesOffset = 0; renderCaseCards(); }

function setCasesChip(el, court) {
    document.querySelectorAll('#casesChips .legal-chip').forEach(c => { c.className = 'legal-chip inactive'; });
    el.className = 'legal-chip active';
    Legal.casesCourtFilter = court;
    Legal.casesOffset = 0;
    renderCaseCards();
}

function resetChips(groupId) {
    document.querySelectorAll(`#${groupId} .legal-chip`).forEach((c,i) => {
        c.className = i === 0 ? 'legal-chip active' : 'legal-chip inactive';
    });
}

async function refreshLegalNews() {
    if (!Legal.company) return;
    showLoading(true, 'Refreshing news...', '');
    try {
        const res = await fetch('/api/legal/news', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({company_name: Legal.company, days_back: parseInt(document.getElementById('legalDaysBack').value)})
        });
        const data = await res.json();
        if (data.success) { Legal.allNews = data.articles || []; Legal.newsOffset = 0; renderNewsCards(); }
        else showAlert('legalAlert', data.error || 'Refresh failed');
    } catch(e) { showAlert('legalAlert', 'Network error'); }
    showLoading(false);
}

async function refreshLegalCases() {
    if (!Legal.company) return;
    showLoading(true, 'Refreshing court records...', '');
    try {
        const res = await fetch('/api/legal/cases', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({company_name: Legal.company, max_pages: parseInt(document.getElementById('legalCourtPages').value)})
        });
        const data = await res.json();
        if (data.success) { Legal.allCases = data.cases || []; Legal.casesOffset = 0; renderCaseCards(); }
        else showAlert('legalAlert', data.error || 'Refresh failed');
    } catch(e) { showAlert('legalAlert', 'Network error'); }
    showLoading(false);
}

const Analysis = {
    gstOverride: null,
    lastResult:  null,
    weightMode:  'standard',
    weights:     { gst: 20, financial: 35, market: 15, legal: 20, web: 10 },
};

const AN_WEIGHT_PILLARS = [
    { key: 'gst',       label: 'GST Verification',    icon: 'fa-file-invoice',   fg: '#059669', bg: '#ecfdf5' },
    { key: 'financial', label: 'Financial Health',     icon: 'fa-chart-line',     fg: '#2563eb', bg: '#eff6ff' },
    { key: 'market',    label: 'Market Signals',       icon: 'fa-arrow-trend-up', fg: '#0891b2', bg: '#ecfeff' },
    { key: 'legal',     label: 'Legal Risk',           icon: 'fa-scale-balanced', fg: '#7c3aed', bg: '#f5f3ff' },
    { key: 'web',       label: 'Web Presence',         icon: 'fa-globe',          fg: '#d97706', bg: '#fffbeb' },
];

function renderWeightRows() {
    const disabled = Analysis.weightMode !== 'custom';
    document.getElementById('anWeightRows').innerHTML = AN_WEIGHT_PILLARS.map(p => `
        <div class="an-weight-row">
            <div class="an-weight-label">
                <div class="an-weight-icon" style="background:${p.bg};color:${p.fg}"><i class="fa-solid ${p.icon}"></i></div>
                <span>${p.label}</span>
            </div>
            <input type="range" class="an-weight-slider" min="0" max="100" step="1"
                   value="${Analysis.weights[p.key]}" ${disabled ? 'disabled' : ''}
                   oninput="onWeightChange('${p.key}', this.value)">
            <div class="an-weight-input-wrap">
                <input type="text" class="an-weight-input" value="${Analysis.weights[p.key]}"
                       ${disabled ? 'disabled' : ''} onchange="onWeightChange('${p.key}', this.value)">
                <span style="font-size:.7rem;color:#94a3b8">%</span>
            </div>
        </div>`).join('');
    updateWeightTotal();
}

function onWeightChange(key, value) {
    let n = parseInt(value, 10);
    if (Number.isNaN(n)) n = 0;
    n = Math.max(0, Math.min(100, n));
    Analysis.weights[key] = n;
    renderWeightRows();
}

function updateWeightTotal() {
    const total = AN_WEIGHT_PILLARS.reduce((s, p) => s + (Analysis.weights[p.key] || 0), 0);
    const totalEl = document.getElementById('anWeightTotal');
    const warnEl  = document.getElementById('anWeightWarning');
    const ok = Analysis.weightMode !== 'custom' || total === 100;
    totalEl.textContent = `${total}%`;
    totalEl.style.color = (Analysis.weightMode === 'custom' && total !== 100) ? '#dc2626' : '#1e293b';
    warnEl.style.display = (Analysis.weightMode === 'custom' && total !== 100) ? 'flex' : 'none';
    return ok;
}

function setWeightMode(mode) {
    Analysis.weightMode = mode;
    document.querySelectorAll('.an-weight-toggle-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    const badge = document.getElementById('anWeightModeBadge');
    badge.textContent = mode === 'custom' ? 'Custom' : 'Standard';
    document.getElementById('anWeightDesc').innerHTML = mode === 'custom'
        ? 'Adjust the sliders so the 5 weights add up to <strong>100%</strong>, then click <strong>Generate Analysis</strong>. This single profile is applied uniformly to every segment (Credit / Vendor / Investment).'
        : "Standard mode uses built-in weight profiles that already differ by segment (e.g. Financial Health matters more for Investment, Legal Risk matters more for Vendor Trust). Switch to <strong>Custom</strong> to set your own single weight profile across the 5 pillars — it's applied uniformly to every segment, and the overall score, KPIs and recommendation below recalculate accordingly.";
    renderWeightRows();
}

renderWeightRows();

const _origRunLegalAnalysis = runLegalAnalysis;
runLegalAnalysis = async function(silent=false) {
    const company = document.getElementById('legalCompanyInput').value.trim();
    if (!company) { if (!silent) showAlert('legalAlert', 'Enter a company name'); return; }
    const daysBack = document.getElementById('legalDaysBack').value;
    const courtPages = document.getElementById('legalCourtPages').value;
    Legal.company = company;
    Legal.newsRiskFilter = 'all';
    Legal.casesCourtFilter = 'all';

    const btn = document.getElementById('legalAnalyzeBtn');
    btn.disabled = true;
    if (!silent) showLoading(true, 'Fetching legal intelligence...', 'Scanning news sources & Indian Kanoon — may take 20–40 s');

    try {
        const res = await fetch('/api/legal/summary', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({company_name: company, days_back: parseInt(daysBack), max_court_pages: parseInt(courtPages)})
        });
        const data = await res.json();
        if (!data.success) {
            if (!silent) showAlert('legalAlert', data.error || 'Analysis failed');
        } else {
            Legal.summary = data;
            Legal.allNews = data.articles || [];
            Legal.allCases = data.cases || [];
            document.getElementById('legalEmpty').style.display = 'none';
            document.getElementById('legalResultsArea').style.display = 'block';
            renderLegalHero(data);
            renderLegalKpis(data);
            renderLegalFindings(data);
            renderLegalRiskMeter(data);
            resetChips('newsChips');
            resetChips('casesChips');
            Legal.newsRiskFilter = 'all';
            Legal.casesCourtFilter = 'all';
            renderNewsCards();
            renderCaseCards();
            if (!silent) showAlert('legalAlert', 'Legal intelligence loaded successfully', 'success');
        }
    } catch(e) {
        if (!silent) showAlert('legalAlert', 'Network error during legal analysis');
    }
    btn.disabled = false;
    if (!silent) showLoading(false);
};

function _anResolveCompanyName() {
    const typed = document.getElementById('anCompanyName').value.trim();
    if (typed) return typed;
    return (AppState.companyInfo && AppState.companyInfo.company_overview && AppState.companyInfo.company_overview.name)
        || Fin2.companyName || Legal.company || '';
}

async function verifyGstManual() {
    const gstin = document.getElementById('anGstManualInput').value.trim().toUpperCase();
    const statusEl = document.getElementById('anGstManualStatus');
    if (!/^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$/.test(gstin)) {
        statusEl.className = 'an-gst-status bad';
        statusEl.textContent = 'Invalid GSTIN format.';
        return;
    }
    statusEl.className = 'an-gst-status';
    statusEl.textContent = 'Verifying...';
    try {
        const res = await fetch('/api/analysis/verify-gst-manual', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ gstin })
        });
        const data = await res.json();
        if (data.success) {
            Analysis.gstOverride = data;
            statusEl.className = 'an-gst-status ok';
            statusEl.textContent = `✓ Override set: ${data.legal_name || gstin} — ${data.status || 'Unknown'} (will be used instead of Home page GST data)`;
        } else {
            statusEl.className = 'an-gst-status bad';
            statusEl.textContent = data.error || 'Verification failed';
        }
    } catch (e) {
        statusEl.className = 'an-gst-status bad';
        statusEl.textContent = 'Network error';
    }
}

function clearGstManualOverride() {
    Analysis.gstOverride = null;
    document.getElementById('anGstManualInput').value = '';
    const statusEl = document.getElementById('anGstManualStatus');
    statusEl.className = 'an-gst-status';
    statusEl.textContent = 'No manual override set — using GST data from the Home page (if any).';
}

async function runFullAnalysis() {
    const companyName = _anResolveCompanyName();
    if (!companyName) { showAlert('anAlert', 'Enter a company name (or run Financials/Legal first so one is already set)'); return; }

    if (Analysis.weightMode === 'custom' && !updateWeightTotal()) {
        showAlert('anAlert', 'Custom weights must add up to 100% before generating analysis');
        return;
    }

    const btn = document.getElementById('anRunBtn');
    btn.disabled = true;
    showLoading(true, 'Running due-diligence analysis...', 'Scoring GST, financial, market, legal & web pillars + AI narrative');

    const payload = {
        company_name:  companyName,
        gst_data:      AppState.gstData || null,
        gst_override:  Analysis.gstOverride || null,
        screener_data: Fin2.screener || null,
        nse_data:      Fin2.nse || null,
        legal_data:    Legal.summary || null,
        rag_data:      AppState.companyInfo || null,
        custom_weights: Analysis.weightMode === 'custom' ? { ...Analysis.weights } : null,
    };

    try {
        const res  = await fetch('/api/analysis/run', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) {
            Analysis.lastResult = data;
            document.getElementById('anEmpty').style.display = 'none';
            document.getElementById('anResults').style.display = 'block';
            renderAnalysisResults(data);
            showAlert('anAlert', 'Analysis complete', 'success');
        } else {
            showAlert('anAlert', data.error || 'Analysis failed');
        }
    } catch (e) {
        showAlert('anAlert', 'Network error while running analysis');
    }
    btn.disabled = false;
    showLoading(false);
}

const AN_BAND_COLORS = { green: '#10b981', blue: '#2563eb', amber: '#f59e0b', orange: '#ea580c', red: '#ef4444' };
const AN_PILLAR_META = {
    gst:       { icon: 'fa-file-invoice',      color: 'emerald', hex: ['#ecfdf5', '#059669'] },
    financial: { icon: 'fa-chart-line',        color: 'blue',    hex: ['#eff6ff', '#2563eb'] },
    market:    { icon: 'fa-arrow-trend-up',    color: 'cyan',    hex: ['#ecfeff', '#0891b2'] },
    legal:     { icon: 'fa-scale-balanced',    color: 'purple',  hex: ['#f5f3ff', '#7c3aed'] },
    web:       { icon: 'fa-globe',             color: 'amber',   hex: ['#fffbeb', '#d97706'] },
};

function renderAnalysisResults(data) {
    const overall  = data.overall || {};
    const segments = data.segments || {};
    const pillars  = data.pillars || {};
    const coverage = data.data_coverage || {};

    const hdr = document.getElementById('anCompanyHeader');
    if (hdr) hdr.style.display = 'flex';
    document.getElementById('anBannerName').textContent = data.company_name || 'Company';
    const covCount = Object.values(coverage).filter(Boolean).length;
    const modeLabel = data.weight_mode === 'custom' ? 'Custom weights' : 'Standard weights';
    document.getElementById('anBannerMeta').textContent =
        `${covCount}/5 pillars backed by live data · ${modeLabel}`;
    const ts = data.timestamp ? new Date(data.timestamp) : null;
    document.getElementById('anBannerTimestamp').textContent = ts
        ? ts.toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'}) : '';

    const gstBadge = document.getElementById('anGstStatusBadge');
    if (gstBadge && pillars.gst && !pillars.gst._mock) {
        gstBadge.style.display = 'inline-block';
        const gstOk = pillars.gst.score >= 70;
        gstBadge.textContent = gstOk ? 'GST Active' : 'GST Issue';
        gstBadge.style.background = gstOk ? '#ecfdf5' : '#fef2f2';
        gstBadge.style.color = gstOk ? '#16a34a' : '#dc2626';
        gstBadge.style.borderColor = gstOk ? '#bbf7d0' : '#fecaca';
    }

    if (data.weight_mode) {
        document.getElementById('anWeightModeBadge').textContent =
            data.weight_mode === 'custom' ? 'Custom' : 'Standard';
    }

    const score = overall.score || 0;
    const arcLen = 204.2;
    const offset = arcLen - (Math.min(100, Math.max(0, score)) / 100) * arcLen;
    document.getElementById('anGaugeArc').style.strokeDashoffset = offset;
    document.getElementById('anOverallScore').textContent = score;

    const bandColorMap = { green:'#10b981', blue:'#2563eb', amber:'#eab308', orange:'#f97316', red:'#ef4444' };
    const bandBgMap  = { green:'#ecfdf5', blue:'#eff6ff', amber:'#fefce8', orange:'#fff7ed', red:'#fef2f2' };
    const bandBdrMap = { green:'#bbf7d0', blue:'#bfdbfe', amber:'#fde68a', orange:'#fed7aa', red:'#fecaca' };
    const bandEl = document.getElementById('anOverallBand');
    const bc = overall.color || 'amber';
    bandEl.textContent = (overall.band || '—').toUpperCase().replace('LOW-MODERATE RISK','Low-Moderate Risk');
    bandEl.style.background = bandBgMap[bc] || '#fefce8';
    bandEl.style.color = bandColorMap[bc] || '#b45309';
    bandEl.style.borderColor = bandBdrMap[bc] || '#fde68a';

    const recoMap = {
        green:  { icon:'fa-circle-check', text:'PROVIDE SERVICE', blurb:'Strong signals across pillars support proceeding.' },
        blue:   { icon:'fa-circle-check', text:'PROVIDE SERVICE', blurb:'Overall healthy profile; standard due diligence sufficient.' },
        amber:  { icon:'fa-circle-exclamation', text:'PROVIDE WITH CONDITIONS', blurb:'Some pillars show moderate risk — review flagged items below.' },
        orange: { icon:'fa-triangle-exclamation', text:'MANUAL REVIEW REQUIRED', blurb:'Multiple risk signals — manual review recommended before proceeding.' },
        red:    { icon:'fa-circle-xmark', text:'DO NOT PROVIDE SERVICE', blurb:'Significant red flags — proceed only with strong justification.' },
    };
    const reco = recoMap[bc] || recoMap.amber;
    const recoBox = document.getElementById('anRecoBox');
    recoBox.className = 'an-reco-box ' + bc;
    document.getElementById('anRecoText').textContent = reco.text;
    recoBox.querySelector('i').className = 'fa-solid ' + reco.icon;
    document.getElementById('anRecoBlurb').textContent = reco.blurb;

    const segScores = Object.values(segments).map(s => s.score || 0);
    const segAvg = segScores.length ? Math.round(segScores.reduce((a,b)=>a+b,0)/segScores.length) : 0;
    const confLabel = segAvg >= 75 ? 'High' : segAvg >= 50 ? 'Medium' : 'Low';
    document.getElementById('anSegAvg').textContent = confLabel;
    document.getElementById('anDataCoverage').textContent = `${Math.round(covCount/5*100)}%`;

    const gstCallout = document.getElementById('anGstCalloutContent');
    const gstPillar  = pillars.gst || {};
    if (gstCallout) {
        const gstOk = gstPillar.score >= 70 && !gstPillar._mock;
        const borderEl = document.getElementById('anGstCallout');
        if (borderEl) borderEl.style.borderColor = gstOk ? '#10b981' : '#f97316';
        const icon = gstOk ? 'fa-circle-check' : (gstPillar._mock ? 'fa-circle-question' : 'fa-circle-exclamation');
        const clr  = gstOk ? '#16a34a' : (gstPillar._mock ? '#94a3b8' : '#f97316');
        const msg  = gstPillar._mock
            ? 'GST data not verified on this run.'
            : (gstOk ? 'Valid & active GST registration.' : 'GST issue detected — review required.');
        gstCallout.innerHTML = `<i class="fas ${icon}" style="font-size:1.6rem;color:${clr};display:block;margin-bottom:.35rem"></i>
            <span style="font-size:.75rem;font-weight:700;color:#334155">${gstPillar.score || 0}/100</span>
            <div style="font-size:.65rem;color:#64748b;margin-top:.2rem">${msg}</div>`;
    }

    const pillarGrid = document.getElementById('anPillarGrid');
    const pillarDisplayLabels = {
        gst: 'GST Verification', financial: 'Financial Health',
        market: 'News & Market Risk', legal: 'Legal Risk', web: 'Web Presence'
    };
    const pillarBandLabel = s => s >= 80 ? 'Strong' : s >= 65 ? 'Good' : s >= 50 ? 'Moderate' : s >= 35 ? 'Weak' : 'Poor';
    pillarGrid.innerHTML = Object.entries(pillars).map(([key, p]) => {
        const meta = AN_PILLAR_META[key] || AN_PILLAR_META.web;
        const [bg, fg] = meta.hex;
        const s = p.score || 0;
        const barColor = s >= 70 ? '#10b981' : s >= 50 ? '#3b82f6' : s >= 35 ? '#f59e0b' : '#ef4444';
        const bandLbl = pillarBandLabel(s);
        const bandBg  = s >= 70 ? '#ecfdf5' : s >= 50 ? '#eff6ff' : s >= 35 ? '#fffbeb' : '#fef2f2';
        const bandClr = s >= 70 ? '#16a34a' : s >= 50 ? '#2563eb' : s >= 35 ? '#d97706' : '#dc2626';
        const note    = (p.flags && p.flags[0]) || (p.highlights && p.highlights[0]) || (p.findings && p.findings[0]) || '';
        const mockTag = p._mock ? ' <span style="font-size:.6rem;color:#d97706;font-weight:700">⚠ est.</span>' : '';
        return `<div class="an-pillar-card" style="display:flex;flex-direction:column;justify-content:space-between">
            <div>
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem">
                    <div style="display:flex;align-items:center;gap:.5rem">
                        <div class="an-pillar-icon" style="background:${bg};color:${fg};margin-bottom:0"><i class="fa-solid ${meta.icon}"></i></div>
                        <span style="font-size:.72rem;font-weight:700;color:#1e293b">${pillarDisplayLabels[key] || (p.label || key)}</span>
                    </div>
                    <i class="fas fa-circle-info" style="color:#e2e8f0;font-size:.7rem"></i>
                </div>
                <div style="display:flex;align-items:baseline;gap:.4rem;margin-bottom:.5rem">
                    <span style="font-size:1.5rem;font-weight:800;color:${fg}">${s}</span>
                    <span style="font-size:.72rem;color:#94a3b8;font-weight:600">/ 100</span>
                    <span style="margin-left:auto;font-size:.62rem;font-weight:700;padding:.2rem .5rem;border-radius:20px;background:${bandBg};color:${bandClr};border:1px solid ${bandBg}">${bandLbl}${mockTag}</span>
                </div>
                <p style="font-size:.7rem;color:#64748b;line-height:1.5;margin:.3rem 0 0;min-height:2.4em">${note || '&nbsp;'}</p>
            </div>
            <div class="an-pillar-bar" style="margin-top:.85rem"><div class="an-pillar-bar-fill" style="width:${s}%;background:${barColor}"></div></div>
        </div>`;
    }).join('');

    const segLabels = { credit:'B2B Credit', vendor:'Vendor Trust', investment:'Investment' };
    const segIcons  = { credit:'fa-file-invoice-dollar', vendor:'fa-handshake', investment:'fa-chart-pie' };
    const segGrid   = document.getElementById('anSegmentGrid');
    segGrid.innerHTML = Object.entries(segments).map(([key, s]) => {
        const color  = bandColorMap[s.color] || '#64748b';
        const sBg    = bandBgMap[s.color]  || '#f8fafc';
        const sBdr   = bandBdrMap[s.color] || '#e2e8f0';
        const wLabels = { gst:'GST', financial:'Fin', market:'Mkt', legal:'Legal', web:'Web' };
        const wLine   = s.weights ? Object.entries(s.weights).map(([wk,wv]) => `${wLabels[wk]||wk} ${wv}`).join(' · ') : '';
        return `<div class="an-segment-card">
            <div style="font-size:.68rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.25rem;display:flex;align-items:center;gap:.35rem">
                <i class="fa-solid ${segIcons[key]||'fa-chart-simple'}"></i>${segLabels[key] || key}
            </div>
            <div style="display:flex;align-items:baseline;gap:.35rem;margin-bottom:.35rem">
                <span style="font-size:1.6rem;font-weight:800;color:${color}">${s.score}</span>
                <span style="font-size:.8rem;color:#94a3b8;font-weight:600">/ 100</span>
                <span style="font-size:.62rem;font-weight:800;text-transform:uppercase;color:${color};margin-left:.3rem;padding:.15rem .5rem;border-radius:9px;background:${sBg};border:1px solid ${sBdr}">${s.band}</span>
            </div>
            <div style="font-size:.75rem;color:#334155;line-height:1.5">${s.verdict}</div>
            ${wLine ? `<div style="font-size:.6rem;color:#94a3b8;margin-top:.5rem;padding-top:.4rem;border-top:1px dashed #f1f5f9">Weights: ${wLine}</div>` : ''}
        </div>`;
    }).join('');

    document.getElementById('anNarrative').textContent = data.ai_narrative && data.ai_narrative.trim()
        ? data.ai_narrative
        : 'AI narrative unavailable (Groq not configured or generation failed) — pillar & segment scores above are still fully computed.';

    const hi = data.highlights || [];
    const fl = data.flags || [];
    document.getElementById('anHighlightsList').innerHTML = hi.length
        ? hi.slice(0,6).map(h => `<li style="display:flex;align-items:flex-start;gap:.4rem;font-size:.72rem;color:#475569"><i class="fas fa-check-circle" style="color:#16a34a;margin-top:.15rem;flex-shrink:0"></i><span>${h}</span></li>`).join('')
        : '<li style="font-size:.72rem;color:#94a3b8">No positive signals recorded.</li>';
    document.getElementById('anFlagsList').innerHTML = fl.length
        ? fl.slice(0,6).map(f => `<li style="display:flex;align-items:flex-start;gap:.4rem;font-size:.72rem;color:#475569"><i class="fas fa-check" style="color:#d97706;margin-top:.15rem;flex-shrink:0"></i><span>${f}</span></li>`).join('')
        : '<li style="font-size:.72rem;color:#94a3b8">No risk flags recorded.</li>';

    const evEl = document.getElementById('anEvidenceTable');
    if (evEl) {
        const gstStatus = AppState.gstData?.status || (pillars.gst?._mock ? '—' : 'Checked');
        const rev  = _anEvidenceVal(pillars, 'financial', Fin2);
        const np   = _anEvidenceNp(pillars, Fin2);
        const cases = Legal?.allCases?.length ?? '—';
        const highRisk = Legal?.allNews?.filter(a => a.risk_level==='high' && a.relevance==='relevant')?.length ?? '—';
        const newsCount = Legal?.allNews?.filter(a => a.relevance==='relevant')?.length ?? '—';
        const rows = [
            ['GST Status',      gstStatus, gstStatus==='Active'||gstStatus==='active' ? 'ok' : 'neutral'],
            ['Total Revenue',   rev,  'neutral'],
            ['Net Profit',      np,   'neutral'],
            ['Total Legal Cases', cases, cases > 5 ? 'warn' : 'neutral'],
            ['High Risk Cases', highRisk, highRisk > 0 ? 'warn' : 'ok'],
            ['Relevant News',   newsCount, typeof newsCount==='number'&&newsCount>3 ? 'warn' : 'neutral'],
        ];
        evEl.innerHTML = rows.map(([label, val, state], i) => {
            const isLast = i === rows.length - 1;
            const valStyle = state==='ok' ? 'background:#ecfdf5;color:#16a34a;font-weight:700;padding:.15rem .5rem;border-radius:5px'
                           : state==='warn' ? 'background:#fffbeb;color:#d97706;font-weight:700;padding:.15rem .5rem;border-radius:5px'
                           : 'font-weight:700;color:#334155';
            return `<div style="display:flex;align-items:center;justify-content:space-between;padding:.45rem 0;${isLast?'':'border-bottom:1px solid #f1f5f9'}">
                <span style="color:#64748b">${label}</span>
                <span style="${valStyle}">${val ?? '—'}</span>
            </div>`;
        }).join('');
    }
}

function _anEvidenceVal(pillars, key, Fin2) {
    try {
        const block = Fin2?.screener?.consolidated || Fin2?.screener?.standalone;
        const pl = block?.profit_loss;
        if (!pl) return '—';
        const row = (pl.rows||[]).find(r => /sales|revenue/i.test(r.particular||''));
        if (!row) return '—';
        const yrs = pl.years||[];
        const latest = yrs.length ? row.values?.[yrs[yrs.length-1]] : null;
        return latest ? `₹${latest} Cr` : '—';
    } catch(e) { return '—'; }
}
function _anEvidenceNp(pillars, Fin2) {
    try {
        const block = Fin2?.screener?.consolidated || Fin2?.screener?.standalone;
        const pl = block?.profit_loss;
        if (!pl) return '—';
        const row = (pl.rows||[]).find(r => /net profit/i.test(r.particular||''));
        if (!row) return '—';
        const yrs = pl.years||[];
        const latest = yrs.length ? row.values?.[yrs[yrs.length-1]] : null;
        return latest ? `₹${latest} Cr` : '—';
    } catch(e) { return '—'; }
}

document.getElementById('anConfidence')?.addEventListener('input', function() {
    const v = parseInt(this.value);
    const label = v >= 75 ? 'High' : v >= 45 ? 'Medium' : 'Low';
    document.getElementById('anConfidenceLabel').textContent = label;
});

async function saveAnalystDecision() {
    if (!Analysis.lastResult) { showAlert('anAlert', 'Run analysis first'); return; }
    const decision   = document.querySelector('input[name="anDecision"]:checked')?.value || '';
    const comments   = document.getElementById('anComments').value.trim();
    const confidence = document.getElementById('anConfidence').value;
    const payload = {
        company_name: Analysis.lastResult.company_name,
        decision, comments, confidence,
        overall_score: Analysis.lastResult.overall?.score,
    };
    try {
        await fetch('/api/analysis/save-decision', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
    } catch(e) { /* non-fatal — still download the report client-side */ }

    const blob = new Blob([JSON.stringify({ ...Analysis.lastResult, analyst_decision: payload }, null, 2)],
                           { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = `due_diligence_${(Analysis.lastResult.company_name||'report').replace(/\s+/g,'_')}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    showAlert('anAlert', 'Decision saved & report downloaded', 'success');
}

/* ═══════════════════════════════════════════════════════════
   NEW — PDF report generation (reportlab, server-rendered)
═══════════════════════════════════════════════════════════ */
async function downloadPdfReport() {
    const companyName = _anResolveCompanyName();
    if (!companyName) { showAlert('anAlert', 'Run analysis first (or enter a company name)'); return; }

    showLoading(true, 'Generating PDF report...', 'Rendering pillars, financials & legal tables');
    try {
        const res = await fetch('/api/report/generate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                company_name:  companyName,
                gst_data:      AppState.gstData || null,
                gst_override:  Analysis.gstOverride || null,
                screener_data: Fin2.screener || null,
                nse_data:      Fin2.nse || null,
                legal_data:    Legal.summary || null,
                rag_data:      AppState.companyInfo || null,
                custom_weights: Analysis.weightMode === 'custom' ? { ...Analysis.weights } : null,
            })
        });
        const data = await res.json();
        if (data.success) {
            window.open(data.file_url, '_blank');
            showAlert('anAlert', 'PDF report generated', 'success');
        } else {
            showAlert('anAlert', data.error || 'Report generation failed');
        }
    } catch (e) {
        showAlert('anAlert', 'Network error generating report');
    }
    showLoading(false);
}

</script>
</body>
</html>
'''

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/verify-gst", methods=["POST"])
def api_verify_gst():
    data  = request.get_json()
    gstin = data.get("gstin","").strip()
    return jsonify(verify_gst(gstin))

@app.route("/api/check-index", methods=["GET"])
def api_check_index():
    domain  = request.args.get("domain","").strip()
    if not domain:
        return jsonify({"indexed": False, "chunk_count": 0})
    indexed, count = is_domain_indexed(domain)
    return jsonify({"indexed": indexed, "chunk_count": count, "domain": domain})

@app.route("/api/index-company", methods=["POST"])
def api_index_company():
    data          = request.get_json()
    website_url   = data.get("website_url","").strip()
    force_reindex = data.get("force_reindex", False)

    if not website_url:
        return jsonify({"success": False, "error": "Website URL required"}), 400

    missing = []
    if not PLAYWRIGHT_AVAILABLE: missing.append("playwright")
    if not ST_AVAILABLE:         missing.append("sentence-transformers")
    if not CHROMA_AVAILABLE:     missing.append("chromadb")
    if not BS4_AVAILABLE:        missing.append("beautifulsoup4")
    if missing:
        return jsonify({"success": False, "error": f"Missing libraries: {', '.join(missing)}. Run: pip install {' '.join(missing)}"}), 400

    result = index_company_website(website_url, force_reindex=force_reindex)
    return jsonify(result)

@app.route("/api/extract-company", methods=["POST"])
def api_extract_company():
    data         = request.get_json()
    website_url  = data.get("website_url","").strip()
    company_name = data.get("company_name","").strip()

    if not website_url:
        return jsonify({"success": False, "error": "Website URL required"}), 400
    if not groq_client:
        return jsonify({"success": False, "error": "Groq API key not configured"}), 400

    result = extract_company_info_rag(website_url, company_name)
    return jsonify(result)

@app.route("/api/financials/screener", methods=["POST"])
def api_financials_screener():
    data = request.get_json()
    company_name = (data.get("company_name") or "").strip()
    if not company_name:
        return jsonify({"success": False, "error": "Company name required"}), 400
    try:
        result = get_company_financials(company_name)
        return jsonify(result)
    except ScreenerError as e:
        print(f"[SCREENER] Live fetch failed for '{company_name}': {e}", flush=True)
        result = _mock_screener_financials(company_name)
        result["_screener_error"] = str(e)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": f"Unexpected error: {e}"}), 500

@app.route("/api/financials/nse", methods=["POST"])
def api_financials_nse():
    data = request.get_json()
    symbol = (data.get("symbol") or "").strip()
    period = (data.get("period") or "6mo").strip()
    if not symbol:
        return jsonify({"success": False, "error": "NSE symbol required"}), 400
    result = get_nse_quote(symbol, period)
    return jsonify(result)

@app.route("/api/financials/find-reports", methods=["POST"])
def api_financials_find_reports():
    data = request.get_json()
    website_url = (data.get("website_url") or "").strip()
    if not website_url:
        return jsonify({"success": False, "error": "Website URL required"}), 400
    try:
        result = find_report_links(website_url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": f"Crawl failed: {e}"}), 500

@app.route("/api/financials/upload-pdf", methods=["POST"])
def api_financials_upload_pdf():
    if not FITZ_AVAILABLE:
        return jsonify({"success": False, "error": "PyMuPDF not installed. Run: pip install pymupdf"}), 400
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files["file"]
    company_name = (request.form.get("company_name") or "").strip()
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Please upload a PDF file"}), 400

    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", file.filename)
    stored_name = f"{hashlib.md5(safe_name.encode()).hexdigest()[:8]}_{safe_name}"
    dest_path = os.path.join(PDF_UPLOAD_DIR, stored_name)
    file.save(dest_path)

    result = analyze_financial_pdf(dest_path, company_name)
    if result.get("success"):
        result["file_name"] = file.filename
        result["file_url"] = f"/api/financials/uploaded-file/{stored_name}"
    return jsonify(result)

@app.route("/api/financials/uploaded-file/<path:stored_name>", methods=["GET"])
def api_financials_uploaded_file(stored_name):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", stored_name)
    return send_from_directory(PDF_UPLOAD_DIR, safe, as_attachment=False)

@app.route("/api/financials/analyze-pdf-url", methods=["POST"])
def api_financials_analyze_pdf_url():
    if not FITZ_AVAILABLE:
        return jsonify({"success": False, "error": "PyMuPDF not installed. Run: pip install pymupdf"}), 400
    data = request.get_json()
    pdf_url = (data.get("pdf_url") or "").strip()
    company_name = (data.get("company_name") or "").strip()
    if not pdf_url:
        return jsonify({"success": False, "error": "PDF URL required"}), 400

    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", pdf_url.split("/")[-1] or "report.pdf")
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"
    dest_path = os.path.join(PDF_UPLOAD_DIR, f"{hashlib.md5(pdf_url.encode()).hexdigest()[:8]}_{safe_name}")

    try:
        download_pdf(pdf_url, dest_path)
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not download PDF: {e}"}), 400

    result = analyze_financial_pdf(dest_path, company_name)
    return jsonify(result)

@app.route("/api/ask-question", methods=["POST"])
def api_ask_question():
    data        = request.get_json()
    website_url = data.get("website_url", "").strip()
    question    = data.get("question", "").strip()

    if not website_url:
        return jsonify({"success": False, "error": "Website URL required"}), 400
    if not question:
        return jsonify({"success": False, "error": "Question required"}), 400
    if not groq_client:
        return jsonify({"success": False, "error": "Groq API key not configured"}), 400

    result = ask_company_question(website_url, question)
    return jsonify(result)


@app.route("/api/legal/summary", methods=["POST"])
def api_legal_summary():
    data = request.get_json()
    company_name = (data.get("company_name") or "").strip()
    days_back = int(data.get("days_back", 365))
    max_pages = int(data.get("max_court_pages", 2))
    if not company_name:
        return jsonify({"success": False, "error": "Company name required"}), 400
    if not LEGAL_SERVICE_AVAILABLE:
        return jsonify({"success": False, "error": "legal_service.py not installed"}), 500
    try:
        result = get_legal_summary(company_name, days_back=days_back, max_court_pages=max_pages)
        result["success"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/legal/news", methods=["POST"])
def api_legal_news():
    data = request.get_json()
    company_name = (data.get("company_name") or "").strip()
    days_back = int(data.get("days_back", 365))
    if not company_name:
        return jsonify({"success": False, "error": "Company name required"}), 400
    if not LEGAL_SERVICE_AVAILABLE:
        return jsonify({"success": False, "error": "legal_service.py not installed"}), 500
    try:
        result = get_legal_news(company_name, days_back=days_back)
        result["success"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/legal/cases", methods=["POST"])
def api_legal_cases():
    data = request.get_json()
    company_name = (data.get("company_name") or "").strip()
    max_pages = int(data.get("max_pages", 2))
    if not company_name:
        return jsonify({"success": False, "error": "Company name required"}), 400
    if not LEGAL_SERVICE_AVAILABLE:
        return jsonify({"success": False, "error": "legal_service.py not installed"}), 500
    try:
        result = get_court_cases(company_name, max_pages=max_pages)
        result["success"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9B — ANALYSIS (5-pillar / 3-segment due-diligence scoring)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/analysis/run", methods=["POST"])
def api_analysis_run():
    if not ANALYSIS_SERVICE_AVAILABLE:
        return jsonify({"success": False, "error": "services/analysis_service.py not installed"}), 500

    data = request.get_json() or {}
    company_name = (data.get("company_name") or "").strip()

    gst_data = data.get("gst_override") or data.get("gst_data")
    custom_weights = data.get("custom_weights")

    try:
        result = analysis_service.run_analysis(
            company_name  = company_name,
            gst_data      = gst_data,
            screener_data = data.get("screener_data"),
            nse_data      = data.get("nse_data"),
            legal_data    = data.get("legal_data"),
            rag_data      = data.get("rag_data"),
            groq_client   = groq_client,
            custom_weights= custom_weights,
        )
        result["success"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": f"Analysis failed: {e}"}), 500


@app.route("/api/analysis/verify-gst-manual", methods=["POST"])
def api_analysis_verify_gst_manual():
    data  = request.get_json() or {}
    gstin = (data.get("gstin") or "").strip()
    return jsonify(verify_gst(gstin))


_SAVED_DECISIONS = []

@app.route("/api/analysis/save-decision", methods=["POST"])
def api_analysis_save_decision():
    data = request.get_json() or {}
    record = {
        "company_name": (data.get("company_name") or "").strip(),
        "decision":     data.get("decision", ""),
        "comments":     data.get("comments", ""),
        "confidence":   data.get("confidence", ""),
        "overall_score": data.get("overall_score"),
        "timestamp":    datetime.now().isoformat(),
    }
    _SAVED_DECISIONS.append(record)
    return jsonify({"success": True, "saved": record, "total_saved": len(_SAVED_DECISIONS)})


# ═══════════════════════════════════════════════════════════════════════════════
# NEW — SECTION 9C — PDF REPORT GENERATION (reportlab-based due diligence PDF)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/report/generate", methods=["POST"])
def api_report_generate():
    """
    Re-runs the 5-pillar analysis (so the PDF always reflects the current
    inputs / weight configuration) and renders a CareEdge-style PDF via
    services/report_generator.py. Returns a download URL.
    """
    if not REPORT_SERVICE_AVAILABLE:
        return jsonify({"success": False, "error": "report_generator.py not installed"}), 500
    if not ANALYSIS_SERVICE_AVAILABLE:
        return jsonify({"success": False, "error": "analysis_service.py not installed"}), 500

    payload = request.get_json() or {}
    company_name  = (payload.get("company_name") or "Company").strip()
    gst_data      = payload.get("gst_override") or payload.get("gst_data")
    screener_data = payload.get("screener_data")
    nse_data      = payload.get("nse_data")
    legal_data    = payload.get("legal_data")
    rag_data      = payload.get("rag_data")
    custom_weights = payload.get("custom_weights")

    try:
        analysis = analysis_service.run_analysis(
            company_name=company_name,
            gst_data=gst_data,
            screener_data=screener_data,
            nse_data=nse_data,
            legal_data=legal_data,
            rag_data=rag_data,
            groq_client=groq_client,
            custom_weights=custom_weights,
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"Analysis failed: {e}"}), 500

    ov = (rag_data or {}).get("company_overview", {}) if rag_data else {}

    report_data = {
        "company_name":   company_name,
        "report_type":    "Vendor / Credit Assessment",
        "report_date":    datetime.now().strftime("%B %Y"),
        "gstin":          (gst_data or {}).get("gstin", ""),
        "sector":         ov.get("industry", ""),
        "overall_rating": f"{analysis['overall']['band']} / {analysis['overall']['score']}",
        "gst_data":       gst_data,
        "screener_data":  screener_data,
        "nse_data":       nse_data,
        "legal_data":     legal_data,
        "rag_data":       rag_data,
        "analysis":       analysis,
        "credit_drivers": [],
    }

    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", company_name)[:60]
    filename  = f"{safe_name}_{int(time.time())}.pdf"
    out_path  = os.path.join(REPORTS_DIR, filename)

    try:
        generate_due_diligence_report(report_data, out_path)
    except Exception as e:
        return jsonify({"success": False, "error": f"PDF generation failed: {e}"}), 500

    return jsonify({
        "success": True,
        "file_name": filename,
        "file_url": f"/api/report/download/{filename}",
        "overall_score": analysis["overall"]["score"],
    })


@app.route("/api/report/download/<path:filename>", methods=["GET"])
def api_report_download(filename):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)
    return send_from_directory(REPORTS_DIR, safe, as_attachment=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — SERVER STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("=" * 60)
    print("🚀  AI Due Diligence Assistant — RAG Edition")
    print("=" * 60)
    print(f"   Server  : http://localhost:{port}")
    print(f"   ChromaDB: {CHROMA_DIR}")
    print(f"   GST API : {'Configured ✓' if GST_API_KEY else 'Mock mode'}")
    print(f"   Groq    : {'Configured ✓' if GROQ_API_KEY else '✗ missing'}")
    print(f"   Financials svc: {'✓ services/ loaded' if SCREENER_SERVICE_AVAILABLE else '✗ services/ package missing — financials will use mock data'}")
    print(f"   Legal svc   : {'✓ legal_service loaded' if LEGAL_SERVICE_AVAILABLE else '✗ legal_service.py missing'}")
    print(f"   Analysis svc: {'✓ analysis_service loaded' if ANALYSIS_SERVICE_AVAILABLE else '✗ analysis_service.py missing'}")
    print(f"   Report svc  : {'✓ report_generator loaded' if REPORT_SERVICE_AVAILABLE else '✗ report_generator.py missing — PDF export disabled'}")
    print(f"   Playwright  : {'✓' if PLAYWRIGHT_AVAILABLE else '✗ pip install playwright && playwright install chromium'}")
    print(f"   sentence-t  : {'✓' if ST_AVAILABLE else '✗ pip install sentence-transformers'}")
    print(f"   ChromaDB    : {'✓' if CHROMA_AVAILABLE else '✗ pip install chromadb'}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
