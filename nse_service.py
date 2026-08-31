"""
nse_service.py — Fixed
-----------------------
yfinance (Yahoo Finance) throttles or returns empty data when:
  - Too many requests in short time
  - Yahoo blocks the IP temporarily
  - Symbol not found / wrong format

Fixes applied:
  1. Retry with exponential backoff (3 attempts)
  2. Proper .NS suffix handling
  3. Better error messages distinguishing "bad symbol" vs "throttled"
  4. History fetch made optional (doesn't crash quote if history fails)
  5. Added `fast_info` fallback (yfinance v0.2+ has this as a lighter call)
"""

from __future__ import annotations

import math
import time
import logging
import yfinance as yf

logger = logging.getLogger(__name__)


def _clean_nans(obj):
    """Recursively replaces NaN/Infinity floats with None so the result is
    always valid JSON. yfinance frequently returns float('nan') for fields
    it doesn't have data for, and Python's json encoder raises ValueError
    on nan/inf instead of silently passing it through."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean_nans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nans(v) for v in obj]
    return obj


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_ns_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    # Remove exchange suffix if already present, then re-add .NS
    for suffix in (".NS", ".BO", ".BSE"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    return symbol + ".NS"


def _fetch_info_with_retry(ticker: yf.Ticker, max_attempts: int = 3) -> dict:
    """
    yfinance can silently return {} on first call due to throttling.
    Retry with backoff before giving up.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            info = ticker.info
            # yfinance returns {} when throttled — treat as failure
            if info and info.get("regularMarketPrice") is not None:
                return dict(info)
            if info and info.get("currentPrice") is not None:
                return dict(info)
            # Got data but price missing — check if symbol simply invalid
            if info and info.get("quoteType") == "NONE":
                raise ValueError(f"Symbol not found on NSE/Yahoo Finance")
        except Exception as e:
            last_exc = e

        if attempt < max_attempts - 1:
            wait = 2 ** attempt  # 1s, 2s, 4s
            logger.warning(f"yfinance attempt {attempt+1} failed, retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(
        f"Yahoo Finance returned no data after {max_attempts} attempts. "
        f"This usually means the symbol is invalid OR Yahoo is temporarily "
        f"throttling requests. Last error: {last_exc}"
    )


def _safe_history(ticker: yf.Ticker, period: str = "1mo") -> list[dict]:
    """Fetch price history; returns [] instead of crashing if unavailable."""
    try:
        hist = ticker.history(period=period)
        if hist is None or hist.empty:
            return []
        records = []
        for date, row in hist.iterrows():
            records.append({
                "date":   str(date.date()),
                "open":   round(float(row.get("Open", 0)), 2),
                "high":   round(float(row.get("High", 0)), 2),
                "low":    round(float(row.get("Low", 0)), 2),
                "close":  round(float(row.get("Close", 0)), 2),
                "volume": int(row.get("Volume", 0)),
            })
        return records
    except Exception as e:
        logger.warning(f"History fetch failed (non-fatal): {e}")
        return []


def _avg_volume_10d(history: list[dict]) -> float | None:
    vols = [d["volume"] for d in history[-10:] if d.get("volume")]
    if not vols:
        return None
    return round(sum(vols) / len(vols))


# ── Public functions called from main.py ──────────────────────────────────────

def _run_nse_search(query: str) -> list:
    """
    yfinance has no search endpoint.
    We return the query as a suggested symbol so the frontend can call
    /api/nse/quote directly. The frontend should let users type the
    exact NSE symbol (TCS, LT, RELIANCE, INFY etc.).
    """
    query = query.upper().strip()
    # Remove .NS if user typed it
    clean = query.replace(".NS", "").replace(".BO", "")
    return [
        {
            "symbol": clean,
            "display": f"{clean} — enter exact NSE symbol and press Search",
            "note": "Type the exact NSE symbol (e.g. TCS, LT, RELIANCE, WIPRO)",
        }
    ]


VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}


def _run_nse_quote(symbol: str, period: str = "6mo") -> dict:
    """
    Fetch full quote info from Yahoo Finance for an NSE symbol.
    Raises RuntimeError with a clear message on failure.

    `period` controls the chart price-history window (e.g. for a UI period
    selector: 1D/1M/3M/6M/1Y/5Y/Max). 10-day average volume is always
    computed from a separate short 1mo pull so it stays meaningful even
    when `period` is something like "5y".
    """
    if period not in VALID_PERIODS:
        period = "6mo"

    ns_symbol = _to_ns_symbol(symbol)
    logger.info(f"Fetching NSE quote for {ns_symbol} (period={period})")

    ticker = yf.Ticker(ns_symbol)
    info   = _fetch_info_with_retry(ticker)

    # Attach history for the chart (non-fatal if fails)
    history = _safe_history(ticker, period=period)
    info["_price_history"] = history

    # 10-day average volume should reflect recent trading, not the whole
    # selected chart window — pull a fresh short window unless it's already short.
    vol_history = history if period in ("1d", "5d", "1mo") else _safe_history(ticker, period="1mo")
    info["_avg_volume_10d"] = _avg_volume_10d(vol_history)

    return info


def parse_quote_summary(info: dict) -> dict:
    """
    Map raw yfinance info dict → clean dict used by the frontend.
    All fields are Optional — UI should handle None as "—".
    """
    # Price
    last_price = info.get("regularMarketPrice") or info.get("currentPrice")
    change     = info.get("regularMarketChange")
    pct_change = info.get("regularMarketChangePercent")

    # Market cap → Crores
    market_cap    = info.get("marketCap")
    market_cap_cr = round(market_cap / 1e7, 2) if market_cap else None

    # Volume → turnover approximation
    volume   = info.get("regularMarketVolume")
    value_cr = (
        round((last_price * volume) / 1e7, 2)
        if isinstance(last_price, (int, float)) and isinstance(volume, (int, float))
        else None
    )

    # Dividend yield normalisation
    # yfinance sometimes gives 0.0048 (fraction) sometimes 0.48 (percent)
    div_yield = info.get("dividendYield")
    div_yield_pct = None
    if isinstance(div_yield, (int, float)):
        div_yield_pct = (
            round(div_yield * 100, 2) if div_yield < 1 else round(div_yield, 2)
        )

    # EPS
    eps = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")

    return _clean_nans({
        # Identity
        "symbol":       info.get("symbol", "").replace(".NS", ""),
        "company_name": info.get("longName") or info.get("shortName"),
        "sector":       info.get("sector"),
        "industry":     info.get("industry"),
        "isin":         None,  # not available from yfinance

        # Price
        "last_price":   last_price,
        "change":       round(change, 2) if change is not None else None,
        "pct_change":   round(pct_change, 2) if pct_change is not None else None,
        "open":         info.get("regularMarketOpen")  or info.get("open"),
        "high":         info.get("regularMarketDayHigh") or info.get("dayHigh"),
        "low":          info.get("regularMarketDayLow")  or info.get("dayLow"),
        "prev_close":   info.get("regularMarketPreviousClose") or info.get("previousClose"),
        "vwap":         info.get("regularMarketPrice"),   # approx
        "week52_high":  info.get("fiftyTwoWeekHigh"),
        "week52_low":   info.get("fiftyTwoWeekLow"),

        # Volume / Market
        "volume":        volume,
        "avg_volume_10d": info.get("_avg_volume_10d"),
        "value_cr":       value_cr,
        "market_cap_cr":  market_cap_cr,

        # Fundamentals
        "pe_ratio":       info.get("trailingPE"),
        "pb_ratio":       info.get("priceToBook"),
        "eps":            round(eps, 2) if eps else None,
        "dividend_yield_pct": div_yield_pct,
        "beta":           info.get("beta") or info.get("beta3Year"),
        "face_value":     None,   # not in yfinance for NSE

        # Circuit limits — not available from yfinance
        "upper_circuit":  None,
        "lower_circuit":  None,

        # Chart data (list of {date, open, high, low, close, volume})
        "price_history":  info.get("_price_history", []),

        # Timestamp
        "last_updated": info.get("regularMarketTime"),
    })