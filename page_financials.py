"""frontend/pages/page_financials.py — Financial Intelligence with PDF Upload & NSE Data"""
import streamlit as st
import httpx
import pandas as pd
from io import BytesIO

API = "http://localhost:8000"


def render():
    st.markdown("""
    <div class="page-header">
        <h1>💰 Financial Intelligence</h1>
        <p>Comprehensive financial analysis from multiple sources</p>
    </div>
    """, unsafe_allow_html=True)

    company_name = st.session_state.get("company_name", "")
    nse_symbol = st.session_state.get("nse_symbol", "")

    # ══════════════════════════════════════════════════════════════════════════
    # TABS: Upload PDF | Auto-Fetch | NSE Data
    # ══════════════════════════════════════════════════════════════════════════
    tab1, tab2, tab3 = st.tabs(["📤 Upload PDF", "🔍 Auto-Fetch", "📊 NSE Corporate Data"])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1: Manual PDF Upload
    # ──────────────────────────────────────────────────────────────────────────
    with tab1:
        st.markdown("### Upload Annual Report / Balance Sheet")
        st.caption("Upload a PDF to extract and analyze financial data using AI")

        col1, col2 = st.columns([2, 1])
        with col1:
            uploaded_file = st.file_uploader(
                "Choose a PDF file",
                type=["pdf"],
                help="Upload annual report, balance sheet, or financial statement"
            )
        with col2:
            pdf_company_name = st.text_input(
                "Company Name (optional)",
                value=company_name,
                key="pdf_company_name"
            )

        if uploaded_file:
            st.info(f"📄 **{uploaded_file.name}** ({uploaded_file.size / 1024:.1f} KB)")

            if st.button("🔍 Analyze PDF", type="primary", key="analyze_pdf_btn"):
                with st.spinner("Extracting and analyzing financial data..."):
                    try:
                        files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
                        data = {"company_name": pdf_company_name}
                        resp = httpx.post(
                            f"{API}/api/financials/upload-pdf",
                            files=files,
                            data=data,
                            timeout=120
                        )
                        result = resp.json()

                        if result.get("success"):
                            st.session_state["pdf_analysis"] = result
                            st.success("✅ Analysis complete!")
                        else:
                            st.error(f"❌ {result.get('error', 'Analysis failed')}")
                    except Exception as e:
                        st.error(f"API error: {e}")

        # Display PDF analysis results
        if "pdf_analysis" in st.session_state:
            _render_pdf_analysis(st.session_state["pdf_analysis"])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2: Auto-Fetch from Screener + Website
    # ──────────────────────────────────────────────────────────────────────────
    with tab2:
        st.markdown("### Auto-Fetch Financial Data")
        st.caption("Automatically fetch from Screener.in and company website")

        if not company_name:
            st.warning("⚠️ Enter a company name in the sidebar to continue.")
        else:
            col1, col2 = st.columns(2)
            with col1:
                if st.button("📊 Fetch from Screener.in", type="primary"):
                    _fetch_screener_data(company_name)

            with col2:
                website_url = st.text_input(
                    "Company Website (optional)",
                    placeholder="https://www.company.com",
                    key="fin_website_url"
                )
                if website_url and st.button("🔗 Find Annual Reports"):
                    _find_annual_reports(website_url)

        # Display Screener data
        if "fin_result" in st.session_state:
            _render_screener_data(st.session_state["fin_result"])

        # Display found report links
        if "report_links" in st.session_state and st.session_state["report_links"]:
            st.markdown("---")
            st.markdown("#### 📄 Found Annual Reports")
            for link in st.session_state["report_links"][:10]:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown(f"- [{link.get('text', 'Report')}]({link.get('url', '#')})")
                with col2:
                    if st.button("Analyze", key=f"analyze_{hash(link.get('url', ''))}"):
                        _analyze_pdf_url(link.get("url", ""), company_name)

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3: NSE Corporate Data
    # ──────────────────────────────────────────────────────────────────────────
    with tab3:
        st.markdown("### NSE Corporate Filings & Announcements")
        st.caption("Real-time data from National Stock Exchange")

        col1, col2 = st.columns([2, 1])
        with col1:
            nse_input = st.text_input(
                "NSE Symbol",
                value=nse_symbol,
                placeholder="e.g., TCS, RELIANCE, INFY",
                key="nse_symbol_input"
            )
        with col2:
            st.write("")  # Spacer
            st.write("")
            if st.button("🔍 Fetch NSE Data", type="primary"):
                if nse_input:
                    _fetch_nse_data(nse_input)
                elif company_name:
                    _fetch_nse_data_by_name(company_name)
                else:
                    st.warning("Enter an NSE symbol or company name")

        # Display NSE data
        if "nse_corporate_data" in st.session_state:
            _render_nse_data(st.session_state["nse_corporate_data"])


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_screener_data(company_name: str):
    """Fetch data from Screener.in"""
    with st.spinner("Fetching financials from Screener.in…"):
        try:
            resp = httpx.post(
                f"{API}/api/financials/fetch",
                json={"company_name": company_name},
                timeout=25
            )
            st.session_state["fin_result"] = resp.json()
        except Exception as e:
            st.error(f"API error: {e}")


def _find_annual_reports(website_url: str):
    """Find annual report links on company website"""
    with st.spinner("Scanning website for annual reports…"):
        try:
            resp = httpx.post(
                f"{API}/api/financials/find-reports",
                json={"website_url": website_url},
                timeout=30
            )
            result = resp.json()
            if result.get("success"):
                st.session_state["report_links"] = result.get("report_links", [])
                if not result.get("report_links"):
                    st.info("No annual report links found on this website.")
            else:
                st.error(result.get("error", "Failed to scan website"))
        except Exception as e:
            st.error(f"API error: {e}")


def _analyze_pdf_url(pdf_url: str, company_name: str):
    """Analyze a PDF from URL"""
    with st.spinner("Downloading and analyzing PDF…"):
        try:
            resp = httpx.post(
                f"{API}/api/financials/analyze-pdf-url",
                json={"pdf_url": pdf_url, "company_name": company_name},
                timeout=120
            )
            result = resp.json()
            if result.get("success"):
                st.session_state["pdf_analysis"] = result
                st.success("✅ Analysis complete!")
                st.rerun()
            else:
                st.error(result.get("error", "Analysis failed"))
        except Exception as e:
            st.error(f"API error: {e}")


def _fetch_nse_data(symbol: str):
    """Fetch NSE corporate data by symbol"""
    with st.spinner(f"Fetching NSE data for {symbol}…"):
        try:
            resp = httpx.get(f"{API}/api/financials/nse-data/{symbol}", timeout=30)
            result = resp.json()
            if result.get("success"):
                st.session_state["nse_corporate_data"] = result
            else:
                st.error(result.get("error", "Failed to fetch NSE data"))
        except Exception as e:
            st.error(f"API error: {e}")


def _fetch_nse_data_by_name(company_name: str):
    """Fetch NSE data by company name"""
    with st.spinner(f"Searching NSE for {company_name}…"):
        try:
            resp = httpx.post(
                f"{API}/api/financials/nse-data",
                json={"company_name": company_name},
                timeout=30
            )
            result = resp.json()
            if result.get("success"):
                st.session_state["nse_corporate_data"] = result
            else:
                st.warning(result.get("error", "Company not found on NSE"))
                if result.get("suggestion"):
                    st.info(result["suggestion"])
        except Exception as e:
            st.error(f"API error: {e}")


def _render_pdf_analysis(data: dict):
    """Render PDF analysis results"""
    st.markdown("---")
    st.markdown("### 📊 PDF Analysis Results")

    # Header info
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Report Type", data.get("report_type", "N/A"))
    with col2:
        st.metric("Period", data.get("report_period", "N/A"))
    with col3:
        st.metric("Pages Analyzed", data.get("pages_analyzed", 0))

    # Key Financials
    key_fin = data.get("key_financials", {})
    if key_fin:
        st.markdown("#### 💰 Key Financials")
        cols = st.columns(4)
        metrics = [
            ("Revenue", key_fin.get("revenue", {})),
            ("Net Profit", key_fin.get("net_profit", {})),
            ("EBITDA", key_fin.get("ebitda", {})),
            ("Net Worth", key_fin.get("net_worth", {})),
        ]
        for col, (label, val) in zip(cols, metrics):
            if isinstance(val, dict):
                col.metric(label, val.get("value", "N/A"), val.get("yoy_change", ""))
            else:
                col.metric(label, val or "N/A")

    # Key Ratios
    ratios = data.get("key_ratios", {})
    if ratios:
        st.markdown("#### 📈 Key Ratios")
        ratio_cols = st.columns(6)
        ratio_items = list(ratios.items())[:6]
        for col, (k, v) in zip(ratio_cols, ratio_items):
            col.metric(k.replace("_", " ").title(), v or "N/A")

    # Year-wise data
    year_data = data.get("year_wise_data", [])
    if year_data:
        st.markdown("#### 📅 Year-wise Comparison")
        df = pd.DataFrame(year_data)
        st.dataframe(df, use_container_width=True)

    # Creditworthiness Summary
    summary = data.get("creditworthiness_summary", "")
    if summary:
        st.markdown("#### 🎯 Creditworthiness Assessment")
        st.info(summary)

    # Highlights & Risks
    col1, col2 = st.columns(2)
    with col1:
        highlights = data.get("highlights", [])
        if highlights:
            st.markdown("#### ✅ Key Highlights")
            for h in highlights:
                st.markdown(f"- {h}")
    with col2:
        risks = data.get("risk_factors", [])
        if risks:
            st.markdown("#### ⚠️ Risk Factors")
            for r in risks:
                st.markdown(f"- {r}")


def _render_screener_data(data: dict):
    """Render Screener.in financial data"""
    if not data.get("success"):
        st.error("Could not fetch financial data.")
        return

    if data.get("_mock"):
        st.info("ℹ️ Showing mock data — live data requires a public company listed on Screener.in.")

    # Key Ratios
    st.markdown("### 📊 Key Financial Ratios")
    ratios = data.get("key_ratios", {})
    if ratios:
        cols = st.columns(len(ratios))
        for col, (k, v) in zip(cols, ratios.items()):
            col.metric(k, v)

    st.markdown("---")

    # P&L and Balance Sheet
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 📈 Profit & Loss (₹ Cr)")
        _render_fin_table(data.get("profit_loss", {}))

    with col2:
        st.markdown("#### 🏦 Balance Sheet (₹ Cr)")
        _render_fin_table(data.get("balance_sheet", {}))

    # Cash Flow
    st.markdown("---")
    st.markdown("#### 💧 Cash Flow (₹ Cr)")
    _render_fin_table(data.get("cash_flow", {}))

    # Revenue Trend Chart
    pl = data.get("profit_loss", {})
    pl_rows = pl.get("rows", {})
    headers = pl.get("headers", [])[1:]
    rev_row = pl_rows.get("Revenue") or pl_rows.get("Net Sales") or []
    if rev_row and headers:
        st.markdown("---")
        st.markdown("#### 📉 Revenue Trend")
        try:
            chart_data = pd.DataFrame({
                "Year": headers[:len(rev_row)],
                "Revenue": [float(str(v).replace(",", "")) for v in rev_row],
            }).set_index("Year")
            st.bar_chart(chart_data)
        except Exception:
            pass

    # Annual Report Links
    ar_links = data.get("annual_reports", [])
    if ar_links:
        st.markdown("---")
        st.markdown("#### 📄 Annual Reports")
        for ar in ar_links:
            st.markdown(f"- [{ar.get('text', 'Annual Report')}]({ar.get('url', '#')})")


def _render_nse_data(data: dict):
    """Render NSE corporate data"""
    st.markdown("---")

    # Quote summary
    quote = data.get("quote")
    if quote:
        st.markdown(f"### {quote.get('company_name', data.get('symbol', ''))} `{data.get('symbol', '')}`")
        st.caption(quote.get("industry", ""))

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Price", f"₹{quote.get('price', 0):,.2f}",
                    f"{quote.get('change_pct', 0):+.2f}%")
        col2.metric("52W High", f"₹{quote.get('week52_high', 0):,.2f}")
        col3.metric("52W Low", f"₹{quote.get('week52_low', 0):,.2f}")
        col4.metric("P/E Ratio", f"{quote.get('pe_ratio', 0):.1f}")

    # Corporate Announcements
    announcements = data.get("announcements", [])
    if announcements:
        st.markdown("---")
        st.markdown("#### 📢 Recent Announcements")
        for ann in announcements[:5]:
            with st.expander(f"📌 {ann.get('date', '')} - {ann.get('subject', '')[:80]}..."):
                st.write(ann.get("subject", ""))
                if ann.get("attachment_url"):
                    st.markdown(f"[📎 Attachment]({ann['attachment_url']})")

    # Financial Results
    results = data.get("financial_results", [])
    if results:
        st.markdown("---")
        st.markdown("#### 📊 Financial Results (Quarterly)")
        df = pd.DataFrame(results[:8])
        if not df.empty:
            display_cols = ["period", "from_date", "to_date", "audited", "consolidated"]
            available_cols = [c for c in display_cols if c in df.columns]
            st.dataframe(df[available_cols], use_container_width=True)

    # Board Meetings
    meetings = data.get("board_meetings", [])
    if meetings:
        st.markdown("---")
        st.markdown("#### 🗓️ Board Meetings")
        for m in meetings[:5]:
            st.markdown(f"- **{m.get('date', '')}**: {m.get('purpose', '')}")


def _render_fin_table(section: dict):
    """Render a financial table"""
    rows = section.get("rows", {})
    headers = section.get("headers", [])
    if not rows:
        st.write("No data available.")
        return
    df = pd.DataFrame.from_dict(rows, orient="index", columns=headers[1:] if headers else None)
    st.dataframe(df, use_container_width=True)
