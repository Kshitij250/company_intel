"""
analysis_service.py
────────────────────
Three-segment due-diligence scoring engine.

Segments
────────
  1. B2B Credit      — Should we extend services on credit / Net-30?
  2. Vendor Trust    — Is this vendor reliable & trustworthy?
  3. Investment      — Is this company worth investing in?

Each segment has its own weight profile over the same 5 data pillars:

  Pillar                  Max points
  ──────────────────────────────────
  A. GST & Identity           20
  B. Financial Health         35
  C. NSE / Market Signals     15
  D. Legal & News Risk        20
  E. Web Presence / RAG       10
                            ─────
                              100

The weight of each pillar varies by segment (see SEGMENT_WEIGHTS below).
Raw pillar scores are always out of their own max, then scaled to the
segment weight.

Inputs (all optional — missing data gracefully degrades the score):
  gst_data       — output of verify_gst()
  screener_data  — output of get_company_financials()
  nse_data       — output of get_nse_quote()
  legal_data     — output of get_legal_summary()
  rag_data       — output of extract_company_info_rag()

Output
──────
{
  "overall": {
      "score": int 0–100,
      "band":  "Excellent" | "Good" | "Moderate" | "Caution" | "High Risk",
      "color": "green" | "blue" | "amber" | "orange" | "red",
  },
  "segments": {
      "credit":     { score, band, color, verdict, pillars: {...}, flags: [...] },
      "vendor":     { ... },
      "investment": { ... },
  },
  "pillars": {          # raw pillar scores (0–100 each)
      "gst":        { score, max, label, findings: [...] },
      "financial":  { score, max, label, findings: [...] },
      "market":     { score, max, label, findings: [...] },
      "legal":      { score, max, label, findings: [...] },
      "web":        { score, max, label, findings: [...] },
  },
  "flags":     [...],   # flat list of all critical flags
  "highlights": [...],  # flat list of positive signals
  "ai_narrative": str,  # Groq-synthesised 3-paragraph analysis (optional)
  "data_coverage": {    # which pillars had real vs mock/missing data
      "gst": bool, "financial": bool, "market": bool,
      "legal": bool, "web": bool,
  },
  "timestamp": str,
}
"""

from __future__ import annotations

import re
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ── Segment weight profiles ────────────────────────────────────────────────────
# Each key maps to the % share of 100 that pillar contributes for that segment.
SEGMENT_WEIGHTS = {
    "credit": {
        "gst": 0.22,       # compliance = payment behaviour proxy
        "financial": 0.38, # can they actually pay?
        "market": 0.12,
        "legal": 0.22,     # defaults / insolvency risk
        "web": 0.06,
    },
    "vendor": {
        "gst": 0.20,
        "financial": 0.28,
        "market": 0.10,
        "legal": 0.30,     # court cases / fraud = deal-breaker
        "web": 0.12,
    },
    "investment": {
        "gst": 0.10,
        "financial": 0.42, # growth / profitability = primary driver
        "market": 0.28,    # listed companies: valuation matters
        "legal": 0.12,
        "web": 0.08,
    },
}

# ── Custom / manual weight support ─────────────────────────────────────────
# By default each of the 3 segments (credit/vendor/investment) uses its own
# fixed weight profile (SEGMENT_WEIGHTS above). If the analyst supplies a
# custom weight profile from the Analysis page's "Weight Configuration"
# panel, that single profile is applied uniformly across all 3 segments
# instead — so the analyst is directly controlling how much each pillar
# counts, and every downstream number (segment scores, overall score, band,
# recommendation, flags surfaced) shifts accordingly.
PILLAR_KEYS = ("gst", "financial", "market", "legal", "web")

# Sensible starting point shown in the UI slider defaults — mirrors the
# pillar point-allocation documented at the top of this file (20/35/15/20/10).
DEFAULT_CUSTOM_WEIGHTS = {"gst": 0.20, "financial": 0.35, "market": 0.15, "legal": 0.20, "web": 0.10}


def normalize_custom_weights(raw: dict | None) -> dict | None:
    """Validate + normalize a user-supplied weight profile.

    Accepts values either as fractions (0-1) or percentages (0-100) — the
    UI sends percentages (e.g. {"gst": 20, "financial": 35, ...}). Returns
    a dict of 5 floats that sum to exactly 1.0, or None if the input is
    missing/unusable (falls back to the standard per-segment profiles).
    """
    if not raw or not isinstance(raw, dict):
        return None
    try:
        vals = {k: max(0.0, _safe_float(raw.get(k), 0.0)) for k in PILLAR_KEYS}
    except Exception:
        return None
    total = sum(vals.values())
    if total <= 0:
        return None
    # If it looks like percentages (sum well above 1), normalize to fractions.
    return {k: v / total for k, v in vals.items()}


BAND_THRESHOLDS = [
    (85, "Excellent", "green"),
    (70, "Good",      "blue"),
    (55, "Moderate",  "amber"),
    (40, "Caution",   "orange"),
    (0,  "High Risk", "red"),
]

SEGMENT_VERDICTS = {
    "credit": {
        "green":  "Recommend extending credit / Net-30 terms.",
        "blue":   "Credit extension advisable with standard limits.",
        "amber":  "Proceed with caution — request advance or smaller credit limit.",
        "orange": "High risk — consider upfront payment or collateral.",
        "red":    "Do not extend credit. Significant default risk identified.",
    },
    "vendor": {
        "green":  "Trusted vendor — proceed with onboarding.",
        "blue":   "Reliable vendor — standard due diligence sufficient.",
        "amber":  "Onboard with enhanced monitoring & shorter contract terms.",
        "orange": "Elevated risk — seek additional references before proceeding.",
        "red":    "Do not onboard — critical risk signals detected.",
    },
    "investment": {
        "green":  "Strong investment candidate — fundamentals support a position.",
        "blue":   "Good opportunity — conduct final valuation checks.",
        "amber":  "Moderate opportunity — watch growth consistency before committing.",
        "orange": "Weak fundamentals — speculative at best.",
        "red":    "Avoid — significant financial or legal headwinds.",
    },
}


def _band(score: float) -> tuple[str, str]:
    for threshold, label, color in BAND_THRESHOLDS:
        if score >= threshold:
            return label, color
    return "High Risk", "red"


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        n = float(str(v).replace(",", ""))
        import math
        return default if (math.isnan(n) or math.isinf(n)) else n
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    return int(_safe_float(v, default))


# ══════════════════════════════════════════════════════════════════════════════
# PILLAR A — GST & Identity  (raw max = 100)
# ══════════════════════════════════════════════════════════════════════════════

def _score_gst(gst_data: dict | None) -> dict:
    findings, flags, highlights = [], [], []
    score = 0

    if not gst_data or not gst_data.get("success"):
        findings.append("GST data unavailable — pillar score defaulted to 30/100.")
        return {"score": 30, "max": 100, "label": "GST & Identity",
                "findings": findings, "flags": flags, "highlights": highlights,
                "_mock": True}

    is_mock = gst_data.get("_mock", False)

    # Active status (40 pts)
    status = (gst_data.get("status") or "").lower()
    if status == "active":
        score += 40
        highlights.append("GST registration is Active.")
    elif status in ("suspended", "cancelled"):
        score += 0
        flags.append(f"GST registration is {status.title()} — critical red flag.")
    else:
        score += 15
        findings.append(f"GST status: {status or 'Unknown'}.")

    # Filing compliance (35 pts)
    filings = gst_data.get("filings") or []
    if filings:
        filed = sum(1 for f in filings if (f.get("status") or "").lower() == "filed")
        total = len(filings)
        pct   = filed / total if total else 0
        filing_score = int(pct * 35)
        score += filing_score
        if pct == 1.0:
            highlights.append(f"100% filing compliance ({filed}/{total} returns filed).")
        elif pct >= 0.8:
            findings.append(f"Good filing compliance: {filed}/{total} returns filed ({pct*100:.0f}%).")
        else:
            flags.append(f"Poor filing compliance: only {filed}/{total} returns filed ({pct*100:.0f}%).")
    else:
        score += 15
        findings.append("No filing history available — partial credit applied.")

    # Business type (10 pts)
    btype = (gst_data.get("business_type") or "").lower()
    if any(k in btype for k in ["private limited", "public limited", "llp"]):
        score += 10
        highlights.append(f"Registered as: {gst_data.get('business_type')}.")
    elif btype:
        score += 5
        findings.append(f"Business type: {gst_data.get('business_type')}.")

    # Registration age (15 pts)
    reg_date_str = gst_data.get("reg_date") or ""
    try:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                reg_date = datetime.strptime(reg_date_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError("no parse")
        years = (datetime.now() - reg_date).days / 365.25
        if years >= 5:
            score += 15
            highlights.append(f"Established business — registered {years:.1f} years ago.")
        elif years >= 2:
            score += 8
            findings.append(f"Relatively young business — registered {years:.1f} years ago.")
        else:
            score += 2
            flags.append(f"Very new business — registered only {years:.1f} years ago.")
    except Exception:
        score += 5
        findings.append("Registration date could not be parsed.")

    if is_mock:
        findings.append("⚠ Mock GST data — results are illustrative only.")

    return {"score": min(score, 100), "max": 100, "label": "GST & Identity",
            "findings": findings, "flags": flags, "highlights": highlights,
            "_mock": is_mock}


# ══════════════════════════════════════════════════════════════════════════════
# PILLAR B — Financial Health  (raw max = 100)
# ══════════════════════════════════════════════════════════════════════════════

def _latest_val(section: dict | None, patterns: list[str]) -> float | None:
    """Find the most recent non-zero value for a row matching any pattern."""
    if not section or not section.get("rows"):
        return None
    rows = section["rows"]
    years = section.get("years", [])
    for pat in patterns:
        for row in rows:
            if pat.lower() in (row.get("particular") or "").lower():
                for yr in reversed(years):
                    v = _safe_float(row.get("values", {}).get(yr), None)
                    if v is not None and v != 0:
                        return v
    return None


def _yoy_growth(section: dict | None, patterns: list[str]) -> float | None:
    """Year-over-year growth rate for a row, as a fraction (0.12 = 12%)."""
    if not section or not section.get("rows"):
        return None
    rows  = section["rows"]
    years = section.get("years", [])
    if len(years) < 2:
        return None
    for pat in patterns:
        for row in rows:
            if pat.lower() in (row.get("particular") or "").lower():
                curr = _safe_float(row.get("values", {}).get(years[-1]), None)
                prev = _safe_float(row.get("values", {}).get(years[-2]), None)
                if curr is not None and prev and prev != 0:
                    return (curr - prev) / abs(prev)
    return None


def _score_financial(screener_data: dict | None) -> dict:
    findings, flags, highlights = [], [], []
    score = 0

    if not screener_data or not screener_data.get("success"):
        findings.append("Financial data unavailable — pillar score defaulted to 30/100.")
        return {"score": 30, "max": 100, "label": "Financial Health",
                "findings": findings, "flags": flags, "highlights": highlights,
                "_mock": True}

    is_mock = screener_data.get("_mock", False)
    block   = screener_data.get("consolidated") or screener_data.get("standalone") or {}
    pl      = block.get("profit_loss")
    bs      = block.get("balance_sheet")
    rat     = block.get("ratios")

    # Revenue growth (20 pts)
    rev_growth = _yoy_growth(pl, ["sales", "revenue"])
    if rev_growth is not None:
        if rev_growth >= 0.20:
            score += 20
            highlights.append(f"Strong revenue growth: {rev_growth*100:.1f}% YoY.")
        elif rev_growth >= 0.10:
            score += 14
            findings.append(f"Moderate revenue growth: {rev_growth*100:.1f}% YoY.")
        elif rev_growth >= 0:
            score += 8
            findings.append(f"Flat revenue growth: {rev_growth*100:.1f}% YoY.")
        else:
            score += 0
            flags.append(f"Revenue DECLINED {rev_growth*100:.1f}% YoY — significant concern.")
    else:
        score += 10
        findings.append("Revenue trend data unavailable.")

    # Net profit & margin (20 pts)
    np_val  = _latest_val(pl, ["net profit"])
    rev_val = _latest_val(pl, ["sales", "revenue"])
    if np_val is not None:
        if np_val > 0:
            if rev_val and rev_val > 0:
                margin = np_val / rev_val
                if margin >= 0.15:
                    score += 20
                    highlights.append(f"Excellent net profit margin: {margin*100:.1f}%.")
                elif margin >= 0.08:
                    score += 14
                    findings.append(f"Decent net profit margin: {margin*100:.1f}%.")
                elif margin >= 0.03:
                    score += 8
                    findings.append(f"Thin net profit margin: {margin*100:.1f}%.")
                else:
                    score += 4
                    flags.append(f"Very thin margin: {margin*100:.2f}%.")
            else:
                score += 10
                highlights.append("Company is profitable.")
        else:
            score += 0
            flags.append(f"Net loss reported: ₹{abs(np_val):,.0f} Cr — loss-making entity.")
    else:
        score += 8
        findings.append("Profitability data unavailable.")

    # Debt-to-Equity (20 pts)
    de = _latest_val(rat, ["debt to equity", "debt/equity"])
    if de is not None:
        if de <= 0.5:
            score += 20
            highlights.append(f"Very low D/E ratio: {de:.2f} — conservative leverage.")
        elif de <= 1.0:
            score += 14
            findings.append(f"Moderate D/E ratio: {de:.2f}.")
        elif de <= 2.0:
            score += 7
            flags.append(f"High D/E ratio: {de:.2f} — elevated leverage.")
        else:
            score += 0
            flags.append(f"Very high D/E ratio: {de:.2f} — over-leveraged.")
    else:
        score += 8
        findings.append("Debt-to-equity data unavailable.")

    # ROE (20 pts)
    roe = _latest_val(rat, ["roe", "return on equity"])
    if roe is not None:
        if roe >= 20:
            score += 20
            highlights.append(f"Excellent ROE: {roe:.1f}%.")
        elif roe >= 12:
            score += 14
            findings.append(f"Good ROE: {roe:.1f}%.")
        elif roe >= 6:
            score += 7
            findings.append(f"Weak ROE: {roe:.1f}%.")
        else:
            score += 0
            flags.append(f"Very low/negative ROE: {roe:.1f}%.")
    else:
        score += 8
        findings.append("ROE data unavailable.")

    # Cash from operations (20 pts)
    cf     = block.get("cash_flow")
    cfo    = _latest_val(cf, ["cash from operations", "operating activities"])
    if cfo is not None:
        if cfo > 0:
            score += 20
            highlights.append(f"Positive operating cash flow: ₹{cfo:,.0f} Cr.")
        else:
            score += 0
            flags.append(f"Negative operating cash flow: ₹{cfo:,.0f} Cr.")
    else:
        score += 8
        findings.append("Cash flow data unavailable.")

    if is_mock:
        findings.append("⚠ Mock financial data — results are illustrative only.")

    return {"score": min(score, 100), "max": 100, "label": "Financial Health",
            "findings": findings, "flags": flags, "highlights": highlights,
            "_mock": is_mock}


# ══════════════════════════════════════════════════════════════════════════════
# PILLAR C — NSE / Market Signals  (raw max = 100)
# ══════════════════════════════════════════════════════════════════════════════

def _score_market(nse_data: dict | None) -> dict:
    findings, flags, highlights = [], [], []
    score = 0

    if not nse_data or not nse_data.get("success"):
        findings.append("NSE/Market data unavailable (unlisted or symbol not provided). Neutral score applied.")
        return {"score": 50, "max": 100, "label": "NSE / Market Signals",
                "findings": findings, "flags": flags, "highlights": highlights,
                "_mock": True}

    is_mock = nse_data.get("_mock", False)

    # 52-week price position (30 pts) — where is the price vs range?
    price   = _safe_float(nse_data.get("price"), None)
    hi52    = _safe_float(nse_data.get("week52_high"), None)
    lo52    = _safe_float(nse_data.get("week52_low"), None)
    if price and hi52 and lo52 and hi52 > lo52:
        pos = (price - lo52) / (hi52 - lo52)
        pts = int(pos * 30)
        score += pts
        if pos >= 0.75:
            highlights.append(f"Price near 52-week high ({pos*100:.0f}% of range) — strong momentum.")
        elif pos >= 0.40:
            findings.append(f"Price in mid-range ({pos*100:.0f}% of 52-week range).")
        else:
            flags.append(f"Price near 52-week low ({pos*100:.0f}% of range) — weak momentum.")
    else:
        score += 15
        findings.append("52-week range data unavailable.")

    # P/E ratio (25 pts) — reasonable valuation
    pe = _safe_float(nse_data.get("pe_ratio"), None)
    if pe is not None and pe > 0:
        if 8 <= pe <= 25:
            score += 25
            highlights.append(f"P/E ratio {pe:.1f}x — reasonable valuation.")
        elif pe <= 40:
            score += 15
            findings.append(f"P/E ratio {pe:.1f}x — somewhat elevated.")
        elif pe < 8:
            score += 10
            flags.append(f"Very low P/E ({pe:.1f}x) — may signal distress or value trap.")
        else:
            score += 5
            flags.append(f"Very high P/E ({pe:.1f}x) — expensive or speculative.")
    else:
        score += 12
        findings.append("P/E ratio unavailable.")

    # Day change (15 pts)
    chg_pct = _safe_float(nse_data.get("change_pct"), None)
    if chg_pct is not None:
        if chg_pct >= 2:
            score += 15
            highlights.append(f"Strong positive day-change: +{chg_pct:.2f}%.")
        elif chg_pct >= 0:
            score += 10
            findings.append(f"Flat/slightly positive day: {chg_pct:+.2f}%.")
        elif chg_pct >= -2:
            score += 5
            findings.append(f"Slightly negative day: {chg_pct:.2f}%.")
        else:
            score += 0
            flags.append(f"Significant price decline today: {chg_pct:.2f}%.")
    else:
        score += 7
        findings.append("Day-change data unavailable.")

    # Market cap (15 pts) — size = stability proxy
    mcap = _safe_float(nse_data.get("market_cap_cr"), None)
    if mcap:
        if mcap >= 20000:
            score += 15
            highlights.append(f"Large-cap: ₹{mcap:,.0f} Cr market cap.")
        elif mcap >= 5000:
            score += 10
            findings.append(f"Mid-cap: ₹{mcap:,.0f} Cr market cap.")
        elif mcap >= 500:
            score += 5
            findings.append(f"Small-cap: ₹{mcap:,.0f} Cr market cap.")
        else:
            score += 2
            flags.append(f"Micro-cap: ₹{mcap:,.0f} Cr — high volatility risk.")
    else:
        score += 7
        findings.append("Market cap unavailable.")

    # Beta (15 pts) — volatility
    beta = _safe_float(nse_data.get("beta"), None)
    if beta is not None:
        if 0.5 <= beta <= 1.2:
            score += 15
            highlights.append(f"Low-to-moderate volatility: beta {beta:.2f}.")
        elif beta <= 1.5:
            score += 8
            findings.append(f"Moderate-high volatility: beta {beta:.2f}.")
        else:
            score += 2
            flags.append(f"High volatility: beta {beta:.2f} — aggressive stock.")
    else:
        score += 7
        findings.append("Beta/volatility data unavailable.")

    if is_mock:
        findings.append("⚠ Mock market data — results are illustrative only.")

    return {"score": min(score, 100), "max": 100, "label": "NSE / Market Signals",
            "findings": findings, "flags": flags, "highlights": highlights,
            "_mock": is_mock}


# ══════════════════════════════════════════════════════════════════════════════
# PILLAR D — Legal & News Risk  (raw max = 100, INVERTED — lower risk = higher score)
# ══════════════════════════════════════════════════════════════════════════════

def _score_legal(legal_data: dict | None) -> dict:
    findings, flags, highlights = [], [], []
    score = 100  # start perfect, deduct for risk signals

    if not legal_data or not legal_data.get("success"):
        findings.append("Legal/news data unavailable — neutral score applied.")
        return {"score": 60, "max": 100, "label": "Legal & News Risk",
                "findings": findings, "flags": flags, "highlights": highlights,
                "_mock": True}

    kpis         = legal_data.get("kpis") or {}
    articles     = legal_data.get("articles") or []
    cases        = legal_data.get("cases") or []
    risk_band    = (legal_data.get("risk_band") or "low").lower()
    legal_score  = _safe_int(legal_data.get("legal_risk_score"), 0)

    # Court cases penalty (up to -40)
    n_cases = len(cases)
    if n_cases == 0:
        highlights.append("No court records found on Indian Kanoon.")
    elif n_cases <= 2:
        score -= 15
        findings.append(f"{n_cases} court case(s) found — minor litigation history.")
    elif n_cases <= 5:
        score -= 28
        flags.append(f"{n_cases} court cases found — moderate litigation exposure.")
    else:
        score -= 40
        flags.append(f"{n_cases} court cases found — significant litigation history.")

    # High-risk news (up to -30)
    high_risk   = [a for a in articles if a.get("risk_level") == "high"   and a.get("relevance") == "relevant"]
    medium_risk = [a for a in articles if a.get("risk_level") == "medium" and a.get("relevance") == "relevant"]

    if high_risk:
        deduct = min(30, len(high_risk) * 12)
        score -= deduct
        kws = ", ".join(high_risk[0].get("matched_keywords", [])[:3])
        flags.append(f"{len(high_risk)} high-risk article(s) — keywords: {kws or 'legal/regulatory'}.")
    if medium_risk:
        deduct = min(15, len(medium_risk) * 5)
        score -= deduct
        findings.append(f"{len(medium_risk)} medium-risk article(s) found.")

    # Positive: no adverse signals at all (bonus +5)
    if not high_risk and not medium_risk and n_cases == 0:
        score = min(100, score + 5)
        highlights.append("Clean record — no adverse news or legal signals detected.")

    # Low confidence mentions (minor deduct)
    low_conf = [a for a in articles if a.get("relevance") == "low_confidence"]
    if low_conf:
        findings.append(f"{len(low_conf)} low-confidence mention(s) — possibly a different company.")

    return {"score": max(0, min(score, 100)), "max": 100, "label": "Legal & News Risk",
            "findings": findings, "flags": flags, "highlights": highlights,
            "_mock": False}


# ══════════════════════════════════════════════════════════════════════════════
# PILLAR E — Web Presence / RAG  (raw max = 100)
# ══════════════════════════════════════════════════════════════════════════════

def _score_web(rag_data: dict | None) -> dict:
    findings, flags, highlights = [], [], []
    score = 0

    if not rag_data or not rag_data.get("success"):
        findings.append("Website/RAG data unavailable — partial score applied.")
        return {"score": 40, "max": 100, "label": "Web Presence / RAG",
                "findings": findings, "flags": flags, "highlights": highlights,
                "_mock": True}

    ov   = rag_data.get("company_overview") or {}
    ps   = rag_data.get("products_services") or {}
    lead = rag_data.get("leadership_team") or []
    ct   = rag_data.get("contact_information") or {}
    hi   = rag_data.get("key_highlights") or []
    aw   = rag_data.get("awards_recognition") or []
    cp   = rag_data.get("clients_partners") or {}

    ok = lambda v: v and v not in ("Not found", "Not Available", "", [])

    # Description / overview (20 pts)
    if ok(ov.get("description")):
        score += 20
        highlights.append("Clear company description found on website.")
    else:
        findings.append("Company description not found on website.")

    # Products/services (20 pts)
    if ok(ps.get("main_offerings")):
        score += 20
        highlights.append(f"{len(ps['main_offerings'])} product/service line(s) identified.")
    elif ok(ps.get("usp")):
        score += 10
        findings.append("Partial product information found.")

    # Leadership (20 pts)
    valid_leaders = [l for l in lead if l.get("name") and l["name"] not in ("Not found",)]
    if len(valid_leaders) >= 2:
        score += 20
        highlights.append(f"{len(valid_leaders)} leadership team members identified.")
    elif valid_leaders:
        score += 10
        findings.append("Partial leadership information found.")
    else:
        flags.append("No leadership team information found on website.")

    # Contact info (20 pts)
    contact_pts = 0
    if ok(ct.get("registered_office")): contact_pts += 7
    if ok(ct.get("phone")):              contact_pts += 7
    if ok(ct.get("email")):              contact_pts += 6
    score += contact_pts
    if contact_pts >= 14:
        highlights.append("Complete contact information available.")
    elif contact_pts > 0:
        findings.append("Partial contact information found.")
    else:
        flags.append("No contact information found on website.")

    # Clients / Awards (20 pts)
    if ok(cp.get("notable_clients")):
        score += 10
        highlights.append(f"Notable clients: {', '.join(cp['notable_clients'][:3])}.")
    if aw:
        score += 10
        highlights.append(f"{len(aw)} award(s)/recognition(s) found.")

    chunks = _safe_int(rag_data.get("chunks_in_db"), 0)
    findings.append(f"Website indexed: {chunks} content chunks in vector store.")

    return {"score": min(score, 100), "max": 100, "label": "Web Presence / RAG",
            "findings": findings, "flags": flags, "highlights": highlights,
            "_mock": False}


# ══════════════════════════════════════════════════════════════════════════════
# SEGMENT SCORER
# ══════════════════════════════════════════════════════════════════════════════

def _score_segment(segment: str, pillars: dict, weight_override: dict | None = None) -> dict:
    weights = weight_override or SEGMENT_WEIGHTS[segment]
    raw_score = (
        pillars["gst"]["score"]       * weights["gst"]
        + pillars["financial"]["score"] * weights["financial"]
        + pillars["market"]["score"]    * weights["market"]
        + pillars["legal"]["score"]     * weights["legal"]
        + pillars["web"]["score"]       * weights["web"]
    )
    score = round(raw_score)
    band_label, color = _band(score)
    verdict = SEGMENT_VERDICTS[segment][color]

    # Segment-specific flags: pull from pillars weighted most heavily for this segment
    dominant = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:2]
    seg_flags = []
    seg_highlights = []
    for k, _ in dominant:
        seg_flags.extend(pillars[k]["flags"])
        seg_highlights.extend(pillars[k]["highlights"])

    return {
        "score":      score,
        "band":       band_label,
        "color":      color,
        "verdict":    verdict,
        "weights":    {k: f"{v*100:.0f}%" for k, v in weights.items()},
        "pillar_contributions": {
            k: round(pillars[k]["score"] * weights[k])
            for k in weights
        },
        "flags":      seg_flags[:5],
        "highlights": seg_highlights[:5],
    }


# ══════════════════════════════════════════════════════════════════════════════
# AI NARRATIVE  (Groq — optional, gracefully skipped if unavailable)
# ══════════════════════════════════════════════════════════════════════════════

NARRATIVE_PROMPT = """You are a senior business analyst writing a concise due-diligence narrative.

Company: {company_name}
Overall Score: {overall_score}/100 ({overall_band})

Pillar Scores:
  - GST & Identity:        {gst_score}/100
  - Financial Health:      {financial_score}/100
  - NSE / Market Signals:  {market_score}/100
  - Legal & News Risk:     {legal_score}/100
  - Web Presence:          {web_score}/100

Segment Verdicts:
  - B2B Credit:   {credit_verdict} (score: {credit_score}/100)
  - Vendor Trust: {vendor_verdict} (score: {vendor_score}/100)
  - Investment:   {investment_verdict} (score: {investment_score}/100)

Key Flags:
{flags_text}

Key Highlights:
{highlights_text}

Write exactly 3 short paragraphs (no headings, no bullet points, no markdown):
1. Executive summary of the company's overall risk profile.
2. Key strengths and the most positive signals from the analysis.
3. Key risks and what a decision-maker should watch for before proceeding.

Be direct, factual, and professional. Maximum 180 words total."""


def generate_ai_narrative(result: dict, groq_client, company_name: str) -> str:
    pillars = result["pillars"]
    segs    = result["segments"]
    flags   = result["flags"][:6]
    highs   = result["highlights"][:6]

    prompt = NARRATIVE_PROMPT.format(
        company_name      = company_name or "the company",
        overall_score     = result["overall"]["score"],
        overall_band      = result["overall"]["band"],
        gst_score         = pillars["gst"]["score"],
        financial_score   = pillars["financial"]["score"],
        market_score      = pillars["market"]["score"],
        legal_score       = pillars["legal"]["score"],
        web_score         = pillars["web"]["score"],
        credit_verdict    = segs["credit"]["verdict"],
        credit_score      = segs["credit"]["score"],
        vendor_verdict    = segs["vendor"]["verdict"],
        vendor_score      = segs["vendor"]["score"],
        investment_verdict= segs["investment"]["verdict"],
        investment_score  = segs["investment"]["score"],
        flags_text        = "\n".join(f"  - {f}" for f in flags) or "  - None",
        highlights_text   = "\n".join(f"  - {h}" for h in highs) or "  - None",
    )
    try:
        resp = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.25,
            max_tokens=350,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"[analysis_service] Groq narrative failed: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_analysis(
    company_name: str = "",
    gst_data:      dict | None = None,
    screener_data: dict | None = None,
    nse_data:      dict | None = None,
    legal_data:    dict | None = None,
    rag_data:      dict | None = None,
    groq_client    = None,
    custom_weights: dict | None = None,
) -> dict:
    """
    Run the full 5-pillar, 3-segment due-diligence analysis.
    All inputs are optional; missing data degrades gracefully with neutral scores.

    custom_weights: optional analyst-supplied pillar weight profile (from the
    Analysis page's "Weight Configuration" panel), e.g.
    {"gst": 20, "financial": 35, "market": 15, "legal": 20, "web": 10}
    (percentages or fractions — either is accepted). When supplied and valid,
    this ONE profile is applied uniformly to all 3 segments in place of their
    individual fixed weight profiles, so the analyst directly controls how
    much each pillar counts — every score, band and recommendation below is
    computed against it. When omitted/invalid, the standard per-segment
    weight profiles (SEGMENT_WEIGHTS) are used, unchanged from before.
    """

    # ── Score each pillar ──────────────────────────────────────────────────
    pillars = {
        "gst":       _score_gst(gst_data),
        "financial": _score_financial(screener_data),
        "market":    _score_market(nse_data),
        "legal":     _score_legal(legal_data),
        "web":       _score_web(rag_data),
    }

    active_weights = normalize_custom_weights(custom_weights)
    weight_mode = "custom" if active_weights else "standard"

    # ── Score each segment ─────────────────────────────────────────────────
    segments = {
        "credit":     _score_segment("credit",     pillars, active_weights),
        "vendor":     _score_segment("vendor",     pillars, active_weights),
        "investment": _score_segment("investment", pillars, active_weights),
    }

    # ── Overall score (equal average of 3 segments) ────────────────────────
    overall_score = round(sum(s["score"] for s in segments.values()) / 3)
    overall_band, overall_color = _band(overall_score)

    # ── Aggregate flags & highlights across all pillars ────────────────────
    all_flags      = []
    all_highlights = []
    for p in pillars.values():
        all_flags.extend(p["flags"])
        all_highlights.extend(p["highlights"])

    result = {
        "overall": {
            "score": overall_score,
            "band":  overall_band,
            "color": overall_color,
        },
        "segments":   segments,
        "pillars":    pillars,
        "flags":      all_flags,
        "highlights": all_highlights,
        "ai_narrative": "",
        "data_coverage": {
            "gst":       not pillars["gst"].get("_mock",       True),
            "financial": not pillars["financial"].get("_mock", True),
            "market":    not pillars["market"].get("_mock",    True),
            "legal":     not pillars["legal"].get("_mock",     True),
            "web":       not pillars["web"].get("_mock",       True),
        },
        "company_name": company_name,
        "timestamp":    datetime.now().isoformat(),
        "weight_mode":  weight_mode,
        "active_weights": (
            {k: round(v * 100, 1) for k, v in active_weights.items()}
            if active_weights else
            {k: round(v * 100, 1) for k, v in DEFAULT_CUSTOM_WEIGHTS.items()}
        ),
    }

    # ── Groq AI narrative (non-blocking) ──────────────────────────────────
    if groq_client:
        result["ai_narrative"] = generate_ai_narrative(result, groq_client, company_name)

    return result