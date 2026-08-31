"""frontend/pages/page_legal_news.py — News + Legal + NSE combined intelligence page"""
import streamlit as st
import httpx

API = "http://localhost:8000"

def render():
    company_name = st.session_state.get("company_name","")
    nse_symbol   = st.session_state.get("nse_symbol","")

    if not company_name:
        st.markdown("""
        <div class="card" style="text-align:center;padding:60px;border-style:dashed">
            <div style="font-size:48px;margin-bottom:12px">📰</div>
            <div style="font-size:18px;font-weight:600;color:#64748b">Enter a company name in the search bar above</div>
        </div>
        """, unsafe_allow_html=True)
        return

    # ── Fetch all 3 sources ────────────────────────────────────────────────
    if "legal_result" not in st.session_state:
        with st.spinner("Scanning news, legal databases and NSE…"):
            try:
                resp = httpx.post(f"{API}/api/legal-news/full",
                                  json={"company_name": company_name}, timeout=30)
                st.session_state["legal_result"] = resp.json()
            except Exception as e:
                st.error(f"Backend error: {e}")
                return

    if "stock_result" not in st.session_state:
        with st.spinner("Fetching NSE data…"):
            try:
                resp = httpx.post(f"{API}/api/stock/fetch",
                                  json={"company_name": company_name, "symbol": nse_symbol}, timeout=20)
                st.session_state["stock_result"] = resp.json()
            except Exception:
                st.session_state["stock_result"] = {}

    data   = st.session_state["legal_result"]
    news   = data.get("news",{})
    legal  = data.get("legal",{})
    stock  = st.session_state.get("stock_result",{})

    # ── Summary strip ──────────────────────────────────────────────────────
    sent       = news.get("sentiment_summary",{})
    sent_label = sent.get("label","neutral")
    sent_color = "#22c55e" if sent_label=="positive" else "#ef4444" if sent_label=="negative" else "#94a3b8"
    risk_flags = news.get("risk_flags",[])
    high_cases = legal.get("high_risk_cases",[])

    st.markdown(f"""
    <div class="stat-strip">
        <div class="stat-item">
            <div class="stat-val">{news.get('total_results',0)}</div>
            <div class="stat-lbl">News Articles</div>
        </div>
        <div class="stat-item">
            <div class="stat-val" style="color:{'#ef4444' if risk_flags else '#4ade80'}">{len(risk_flags)}</div>
            <div class="stat-lbl">Risk Flags</div>
        </div>
        <div class="stat-item">
            <div class="stat-val" style="color:{sent_color}">{sent_label.title()}</div>
            <div class="stat-lbl">News Sentiment</div>
        </div>
        <div class="stat-item">
            <div class="stat-val">{legal.get('total_found',0)}</div>
            <div class="stat-lbl">Court Cases</div>
        </div>
        <div class="stat-item">
            <div class="stat-val" style="color:{'#ef4444' if high_cases else '#4ade80'}">{len(high_cases)}</div>
            <div class="stat-lbl">High Risk Cases</div>
        </div>
        <div class="stat-item">
            <div class="stat-val" style="color:{'#4ade80' if stock.get('success') else '#94a3b8'}">{'₹'+str(stock.get('price','—')) if stock.get('success') else 'Unlisted'}</div>
            <div class="stat-lbl">NSE Price</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Three columns layout ───────────────────────────────────────────────
    col_news, col_legal, col_nse = st.columns([2, 2, 1.5])

    # NEWS ─────────────────────────────────────────────────────────────────
    with col_news:
        st.markdown('<div class="sec-title">📰 Recent News</div>', unsafe_allow_html=True)

        if news.get("_mock"):
            st.markdown('<span class="pill pill-yellow">⚠️ Mock — Add NEWS_API_KEY in .env</span><br><br>', unsafe_allow_html=True)

        if risk_flags:
            for rf in risk_flags:
                st.markdown(f"""
                <div class="news-card negative">
                    <div style="font-size:11px;font-weight:700;color:#ef4444;margin-bottom:4px">🚨 RISK FLAG</div>
                    <div style="font-size:14px;font-weight:600;color:#0f172a">{rf.get('title','')[:90]}</div>
                    <div style="font-size:11px;color:#94a3b8;margin-top:4px">Keywords: {', '.join(rf.get('keywords',[]))}</div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.markdown('<div class="news-card positive"><div style="font-size:13px;font-weight:600;color:#166534">✅ No risk keywords detected in recent news</div></div>', unsafe_allow_html=True)

        for article in news.get("articles",[])[:6]:
            s    = article.get("sentiment",0)
            cls  = "positive" if s>0.1 else "negative" if s<-0.1 else "neutral"
            icon = "🟢" if s>0.1 else "🔴" if s<-0.1 else "⚪"
            st.markdown(f"""
            <div class="news-card {cls}">
                <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:4px">{icon} {article.get('title','')[:85]}</div>
                <div style="font-size:11px;color:#64748b">{article.get('source','')} · {article.get('published_at','')[:10]}</div>
                <a href="{article.get('url','#')}" target="_blank" style="font-size:11px;color:#3b82f6;text-decoration:none">Read ↗</a>
            </div>
            """, unsafe_allow_html=True)

    # LEGAL ────────────────────────────────────────────────────────────────
    with col_legal:
        st.markdown('<div class="sec-title">⚖️ Indian Kanoon — Court Cases</div>', unsafe_allow_html=True)

        if legal.get("_mock"):
            st.markdown('<span class="pill pill-yellow">⚠️ Mock data</span><br><br>', unsafe_allow_html=True)

        cases = legal.get("cases",[])
        if not cases:
            st.markdown('<div class="news-card positive"><div style="font-size:13px;font-weight:600;color:#166534">✅ No court cases found</div></div>', unsafe_allow_html=True)
        else:
            for c in cases:
                rk   = c.get("risk_keywords",[])
                cls  = "negative" if rk else "neutral"
                icon = "🔴" if rk else "🟡"
                st.markdown(f"""
                <div class="news-card {cls}">
                    <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:4px">{icon} {c.get('title','')[:80]}</div>
                    <div style="font-size:11px;color:#64748b;margin-bottom:4px">{c.get('court','')}</div>
                    {f'<div style="font-size:11px;color:#ef4444">⚠️ {", ".join(rk)}</div>' if rk else ''}
                    <div style="font-size:11px;color:#94a3b8;margin-top:4px">{c.get("snippet","")[:120]}</div>
                    <a href="{c.get('url','#')}" target="_blank" style="font-size:11px;color:#3b82f6;text-decoration:none">View Case ↗</a>
                </div>
                """, unsafe_allow_html=True)

    # NSE ──────────────────────────────────────────────────────────────────
    with col_nse:
        st.markdown('<div class="sec-title">📈 NSE Stock Data</div>', unsafe_allow_html=True)

        if not stock.get("success"):
            st.markdown("""
            <div class="card" style="text-align:center;padding:32px">
                <div style="font-size:32px;margin-bottom:8px">📊</div>
                <div style="font-size:13px;font-weight:600;color:#64748b">Not listed on NSE</div>
                <div style="font-size:11px;color:#94a3b8;margin-top:4px">Add NSE symbol in search bar for stock data</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            if stock.get("_mock"):
                st.markdown('<span class="pill pill-yellow">⚠️ Mock stock data</span><br><br>', unsafe_allow_html=True)

            chg     = float(stock.get("change_pct",0))
            chg_clr = "#22c55e" if chg>=0 else "#ef4444"
            price   = stock.get("price",0)
            high52  = float(stock.get("week52_high",1))
            low52   = float(stock.get("week52_low",0))
            pos     = int((float(price)-low52)/max(1,high52-low52)*100)

            st.markdown(f"""
            <div class="card" style="text-align:center;padding:20px;margin-bottom:12px">
                <div style="font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase">{stock.get('symbol','')} · NSE</div>
                <div style="font-size:36px;font-weight:900;color:#0f172a;margin:8px 0">₹{price:,.2f}</div>
                <div style="font-size:14px;font-weight:600;color:{chg_clr}">{chg:+.2f}% today</div>
            </div>
            """, unsafe_allow_html=True)

            metrics = [
                ("52W High", f"₹{stock.get('week52_high',0):,.0f}"),
                ("52W Low",  f"₹{stock.get('week52_low',0):,.0f}"),
                ("P/E Ratio",str(stock.get("pe_ratio","—"))),
                ("Prev Close",f"₹{stock.get('prev_close',0):,.2f}"),
            ]
            for label, val in metrics:
                st.markdown(f"""
                <div style="display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f1f5f9">
                    <span style="font-size:12px;color:#64748b">{label}</span>
                    <span style="font-size:13px;font-weight:600;color:#0f172a">{val}</span>
                </div>
                """, unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div style="font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;margin-bottom:6px">52-Week Position</div>', unsafe_allow_html=True)
            st.progress(pos)
            st.caption(f"At {pos}% of 52-week range")
