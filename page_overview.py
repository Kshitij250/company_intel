"""frontend/pages/page_overview.py — Overview / Landing"""
import streamlit as st

def render():
    if not st.session_state.get("analysed"):
        # ── Stat strip ────────────────────────────────────────────────────
        st.markdown("""
        <div class="stat-strip">
            <div class="stat-item"><div class="stat-val">GST</div><div class="stat-lbl">Live Verification</div></div>
            <div class="stat-item"><div class="stat-val">MCA</div><div class="stat-lbl">Director Data</div></div>
            <div class="stat-item"><div class="stat-val">NSE</div><div class="stat-lbl">Stock Market</div></div>
            <div class="stat-item"><div class="stat-val">News</div><div class="stat-lbl">Press & Reputation</div></div>
            <div class="stat-item"><div class="stat-val">ML</div><div class="stat-lbl">Risk Prediction</div></div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown('<div class="sec-title">What this platform does</div>', unsafe_allow_html=True)
        c1, c2, c3, c4, c5 = st.columns(5)
        features = [
            ("📋", "GST & Identity", "Verify GSTIN, legal name, registered address, directors, filing compliance — live from the GST portal."),
            ("💰", "Financials", "Revenue, profit, balance sheet, D/E ratio, ROCE from Screener.in + annual report PDF extraction."),
            ("📰", "News & Legal", "NewsAPI press coverage, Indian Kanoon court cases, risk keyword detection, sentiment scoring."),
            ("📈", "NSE Market", "Live stock price, 52-week range, P/E ratio, volume, and 6-month price history chart."),
            ("🎯", "ML Prediction", "Weighted 0–100 credit risk score. Should you extend services before payment? Get a clear answer."),
        ]
        for col, (icon, title, desc) in zip([c1,c2,c3,c4,c5], features):
            with col:
                st.markdown(f"""
                <div class="feature-card">
                    <div class="feature-icon">{icon}</div>
                    <h3>{title}</h3>
                    <p>{desc}</p>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="sec-title">How to use</div>', unsafe_allow_html=True)
        st.markdown("""
        <div class="card" style="max-width:720px">
            <div style="display:flex;gap:32px">
                <div style="text-align:center;flex:1">
                    <div style="width:36px;height:36px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;color:white;margin:0 auto 10px">1</div>
                    <div style="font-weight:600;font-size:14px;color:#0f172a">Enter Details</div>
                    <div style="font-size:12px;color:#64748b;margin-top:4px">Type company name + GSTIN in the search bar above</div>
                </div>
                <div style="text-align:center;flex:1">
                    <div style="width:36px;height:36px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;color:white;margin:0 auto 10px">2</div>
                    <div style="font-weight:600;font-size:14px;color:#0f172a">Click Analyse</div>
                    <div style="font-size:12px;color:#64748b;margin-top:4px">We pull data from 5 sources simultaneously</div>
                </div>
                <div style="text-align:center;flex:1">
                    <div style="width:36px;height:36px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;color:white;margin:0 auto 10px">3</div>
                    <div style="font-weight:600;font-size:14px;color:#0f172a">Navigate Tabs</div>
                    <div style="font-size:12px;color:#64748b;margin-top:4px">Browse GST, Financials, News & NSE, then get ML prediction</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        return

    # ── Post analysis summary ──────────────────────────────────────────────
    company = st.session_state.get("company_name", "")
    gstin   = st.session_state.get("gstin", "")

    st.markdown(f"""
    <div class="card" style="background:linear-gradient(135deg,#0f172a,#1e3a5f);border-color:#1e3a5f;margin-bottom:28px">
        <div style="display:flex;align-items:center;justify-content:space-between">
            <div>
                <div style="font-size:11px;font-weight:600;color:#3b82f6;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px">✅ Analysis Ready</div>
                <div style="font-size:26px;font-weight:800;color:#f1f5f9">{company or "Company"}</div>
                <div style="font-size:13px;color:#64748b;margin-top:4px">GSTIN: {gstin or "—"} &nbsp;·&nbsp; NSE: {st.session_state.get("nse_symbol","Not provided")}</div>
            </div>
            <div style="font-size:48px">🏢</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="sec-title">Navigate to a tab above to see detailed results</div>', unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    tabs_info = [
        ("📋", "GST & Identity", "gst", "Legal verification, directors, filing status"),
        ("💰", "Financials", "financials", "Revenue, profit, balance sheet"),
        ("📰", "News & Legal & NSE", "intelligence", "Press, court cases, stock market"),
        ("🎯", "ML Prediction", "prediction", "Credit risk score & recommendation"),
    ]
    for col, (icon, title, key, desc) in zip([c1,c2,c3,c4], tabs_info):
        with col:
            st.markdown(f"""
            <div class="feature-card" style="text-align:center;padding:20px">
                <div style="font-size:28px;margin-bottom:8px">{icon}</div>
                <div style="font-weight:700;font-size:14px;color:#0f172a;margin-bottom:4px">{title}</div>
                <div style="font-size:12px;color:#64748b">{desc}</div>
            </div>
            """, unsafe_allow_html=True)
