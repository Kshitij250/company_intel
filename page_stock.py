"""frontend/pages/page_stock.py — Page 3: Stock Market Analysis"""
import streamlit as st
import httpx
import pandas as pd

API = "http://localhost:8000"


def render():
    st.markdown("""
    <div class="page-header">
        <h1>📈 Stock Market Analysis</h1>
        <p>NSE live quote, 52-week range, volume, and price history</p>
    </div>
    """, unsafe_allow_html=True)

    company_name = st.session_state.get("company_name", "")
    nse_symbol   = st.session_state.get("nse_symbol", "")
    if not company_name:
        st.warning("Enter a company name in the sidebar to continue.")
        return

    if "stock_result" not in st.session_state:
        with st.spinner("Fetching NSE data…"):
            try:
                resp = httpx.post(f"{API}/api/stock/fetch",
                                  json={"company_name": company_name, "symbol": nse_symbol},
                                  timeout=20)
                st.session_state["stock_result"] = resp.json()
            except Exception as e:
                st.error(f"API error: {e}")
                return

    data = st.session_state["stock_result"]

    if not data.get("success"):
        st.warning("⚠️ This company does not appear to be listed on NSE.")
        st.info("The risk score will rely entirely on financial and legal data for unlisted companies.")
        return

    if data.get("_mock"):
        st.info("ℹ️ Showing mock data — provide an NSE symbol in the sidebar for live data.")

    symbol = data.get("symbol", "—")
    name   = data.get("company_name", company_name)

    # ── Header ─────────────────────────────────────────────────────────────
    st.markdown(f"## {name}  `{symbol}`")
    st.caption(data.get("industry", ""))

    # ── Price Metrics ──────────────────────────────────────────────────────
    change_pct = data.get("change_pct", 0)
    delta_str  = f"{change_pct:+.2f}%"

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Current Price",  f"₹{data.get('price', 0):,.2f}",      delta=delta_str)
    col2.metric("Prev Close",     f"₹{data.get('prev_close', 0):,.2f}")
    col3.metric("52W High",       f"₹{data.get('week52_high', 0):,.2f}")
    col4.metric("52W Low",        f"₹{data.get('week52_low', 0):,.2f}")
    col5.metric("P/E Ratio",      f"{data.get('pe_ratio', 0):.1f}")

    st.markdown("---")

    # ── 52-week range bar ──────────────────────────────────────────────────
    price    = float(data.get("price", 0))
    high52   = float(data.get("week52_high", 1))
    low52    = float(data.get("week52_low", 0))
    position = (price - low52) / max(1, high52 - low52) * 100

    st.markdown("#### 📏 52-Week Price Range")
    st.markdown(f"**₹{low52:,.0f}** ←  Current: **₹{price:,.2f}** ({position:.0f}% of range)  → **₹{high52:,.0f}**")
    st.progress(int(position))

    st.markdown("---")

    # ── Volume & Market Cap ────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 📦 Volume")
        vol = data.get("volume", 0)
        st.metric("Today's Volume", f"{vol:,}" if vol else "N/A")

    with col2:
        st.markdown("#### 🏦 Market Cap")
        mcap = data.get("market_cap", 0)
        if mcap:
            if mcap >= 1e12:
                label = f"₹{mcap/1e12:.2f}L Cr"
            elif mcap >= 1e9:
                label = f"₹{mcap/1e9:.2f} Cr"
            else:
                label = f"₹{mcap:,.0f}"
            st.metric("Market Capitalisation", label)

    # ── Price History chart (if available) ────────────────────────────────
    st.markdown("---")
    st.markdown("#### 📉 Price History")

    if symbol and symbol != "—":
        with st.spinner("Loading price history…"):
            try:
                hist_resp = httpx.get(f"{API}/api/stock/history/{symbol}", timeout=20)
                hist_data = hist_resp.json()
                if hist_data.get("success") and hist_data.get("ohlc"):
                    ohlc = hist_data["ohlc"]
                    df   = pd.DataFrame(ohlc).set_index("date")
                    st.line_chart(df[["close"]])
                else:
                    st.info("Price history not available for this symbol.")
            except Exception:
                st.info("Could not load price history.")
    else:
        st.info("Provide a valid NSE symbol to view price history.")
