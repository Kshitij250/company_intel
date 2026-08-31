"""frontend/pages/page_risk.py — ML Prediction & Credit Risk"""
import streamlit as st
import httpx
import json

API = "http://localhost:8000"

def render():
    gstin        = st.session_state.get("gstin","")
    company_name = st.session_state.get("company_name","")
    nse_symbol   = st.session_state.get("nse_symbol","")

    if not gstin or not company_name:
        st.markdown("""
        <div class="card" style="text-align:center;padding:60px;border-style:dashed">
            <div style="font-size:48px;margin-bottom:12px">🎯</div>
            <div style="font-size:18px;font-weight:600;color:#64748b">Enter both GSTIN and Company Name above to run the ML prediction</div>
        </div>
        """, unsafe_allow_html=True)
        return

    if "risk_result" not in st.session_state:
        with st.spinner("🤖 Running ML analysis across all data sources…"):
            try:
                resp = httpx.post(f"{API}/api/risk/score", json={
                    "gstin": gstin, "company_name": company_name, "nse_symbol": nse_symbol
                }, timeout=60)
                result = resp.json()
                st.session_state["risk_result"] = result
                sources = result.get("sources",{})
                st.session_state["gst_result"]  = sources.get("gst",{})
                st.session_state["fin_result"]  = sources.get("financials",{})
                st.session_state["stock_result"]= sources.get("stock") or {}
                st.session_state["legal_result"]= {"news": sources.get("news",{}), "legal": sources.get("legal",{})}
            except Exception as e:
                st.error(f"Backend error: {e}")
                return

    result = st.session_state["risk_result"]
    score  = result.get("score",{})
    overall= score.get("overall_score",0)
    band   = score.get("band","")
    reco   = score.get("recommendation","")
    color  = score.get("color","gray")

    score_cls = "score-green" if color=="green" else "score-red" if color=="red" else "score-yellow"
    border_clr= "#4ade80" if color=="green" else "#f87171" if color=="red" else "#fbbf24"

    # ── Score hero ─────────────────────────────────────────────────────────
    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown(f"""
        <div class="score-hero" style="border-color:{border_clr}40">
            <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:8px">Credit Risk Score</div>
            <div class="score-number {score_cls}">{overall}</div>
            <div style="font-size:13px;color:#64748b;margin:8px 0 16px">out of 100</div>
            <div style="display:inline-block;background:{border_clr}20;border:1px solid {border_clr}40;
                        border-radius:20px;padding:6px 20px;font-size:14px;font-weight:700;color:{border_clr}">
                {band}
            </div>
            <div style="margin-top:16px;font-size:14px;color:#94a3b8;line-height:1.5">{reco}</div>
        </div>
        """, unsafe_allow_html=True)

    with c2:
        st.markdown('<div class="sec-title">Score Breakdown</div>', unsafe_allow_html=True)
        breakdown = score.get("breakdown",{})
        for key, val in breakdown.items():
            s   = val.get("score",0)
            mx  = val.get("max",1)
            lb  = val.get("label",key)
            pct = int(s/max(1,mx)*100)
            bar_color = "#22c55e" if pct>=70 else "#f59e0b" if pct>=40 else "#ef4444"
            st.markdown(f"""
            <div style="margin-bottom:18px">
                <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span style="font-size:14px;font-weight:600;color:#0f172a">{lb}</span>
                    <span style="font-size:14px;font-weight:700;color:{bar_color}">{s:.0f} / {mx}</span>
                </div>
                <div style="background:#f1f5f9;border-radius:6px;height:10px;overflow:hidden">
                    <div style="background:{bar_color};height:100%;width:{pct}%;border-radius:6px;transition:width 0.5s"></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="sec-title">Key Risk Flags</div>', unsafe_allow_html=True)
        for flag in score.get("key_flags",[]):
            if flag["type"]=="danger":
                st.markdown(f'<div class="news-card negative" style="padding:12px 16px"><span style="font-size:13px;font-weight:600">🔴 {flag["msg"]}</span></div>', unsafe_allow_html=True)
            elif flag["type"]=="warning":
                st.markdown(f'<div class="news-card neutral" style="padding:12px 16px"><span style="font-size:13px;font-weight:600">🟡 {flag["msg"]}</span></div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="news-card positive" style="padding:12px 16px"><span style="font-size:13px;font-weight:600">🟢 {flag["msg"]}</span></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Methodology ────────────────────────────────────────────────────────
    st.markdown('<div class="sec-title">📐 How the ML Score is Computed</div>', unsafe_allow_html=True)
    mc1, mc2, mc3, mc4 = st.columns(4)
    factors = [
        ("📋", "GST Compliance", "20 pts", "Active status + filing regularity", "#3b82f6"),
        ("💰", "Financial Health", "40 pts", "Revenue growth, profit margin, D/E ratio, ROCE", "#8b5cf6"),
        ("📈", "Market Signals", "15 pts", "52W position, daily trend, P/E ratio", "#06b6d4"),
        ("⚖️", "Legal & News Risk", "25 pts", "Court cases × severity + news sentiment", "#f59e0b"),
    ]
    for col, (icon, title, pts, desc, clr) in zip([mc1,mc2,mc3,mc4], factors):
        with col:
            st.markdown(f"""
            <div class="card" style="text-align:center;border-top:3px solid {clr}">
                <div style="font-size:28px;margin-bottom:8px">{icon}</div>
                <div style="font-weight:700;font-size:14px;color:#0f172a">{title}</div>
                <div style="font-size:20px;font-weight:800;color:{clr};margin:6px 0">{pts}</div>
                <div style="font-size:11px;color:#64748b;line-height:1.5">{desc}</div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Final decision banner ──────────────────────────────────────────────
    bg_clr = "#052e16" if color=="green" else "#450a0a" if color=="red" else "#422006"
    bd_clr = "#166534" if color=="green" else "#991b1b" if color=="red" else "#92400e"
    decision_icon = "✅" if color=="green" else "❌" if color=="red" else "⚠️"

    st.markdown(f"""
    <div style="background:{bg_clr};border:2px solid {bd_clr};border-radius:16px;padding:32px;text-align:center;margin-bottom:24px">
        <div style="font-size:48px;margin-bottom:12px">{decision_icon}</div>
        <div style="font-size:24px;font-weight:800;color:#f1f5f9;margin-bottom:8px">{band}</div>
        <div style="font-size:16px;color:#94a3b8">{reco}</div>
    </div>
    """, unsafe_allow_html=True)

    # ── Download ───────────────────────────────────────────────────────────
    st.download_button(
        "⬇️ Download Full Report (JSON)",
        data=json.dumps(result, indent=2, default=str),
        file_name=f"risk_{company_name.replace(' ','_')}.json",
        mime="application/json",
    )

    with st.expander("View raw score data"):
        st.json(score)
