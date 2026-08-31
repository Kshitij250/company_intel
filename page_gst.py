"""frontend/pages/page_gst.py — GST & Identity (no sidebar, inline)"""
import streamlit as st
import httpx
import pandas as pd

API = "http://localhost:8000"

def render():
    gstin        = st.session_state.get("gstin", "")
    company_name = st.session_state.get("company_name", "")

    if not gstin:
        st.markdown("""
        <div class="card" style="text-align:center;padding:60px;border-style:dashed">
            <div style="font-size:48px;margin-bottom:12px">📋</div>
            <div style="font-size:18px;font-weight:600;color:#64748b">Enter a GSTIN in the search bar above and click Analyse</div>
        </div>
        """, unsafe_allow_html=True)
        return

    # ── Fetch ──────────────────────────────────────────────────────────────
    if "gst_result" not in st.session_state:
        with st.spinner("Verifying GSTIN with GST portal…"):
            try:
                resp = httpx.post(f"{API}/api/gst/verify", json={"gstin": gstin}, timeout=15)
                st.session_state["gst_result"] = resp.json()
            except Exception as e:
                st.error(f"Backend error: {e}")
                return

    data = st.session_state["gst_result"]
    if not data.get("success"):
        st.error(f"❌ {data.get('error', 'GSTIN not found')}")
        return

    if data.get("_mock"):
        st.warning("⚠️ Mock data — add GST_API_KEY in .env for live results")
    else:
        st.markdown('<span class="pill pill-green">✅ Live Data — GST Portal Verified</span>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Company Hero ───────────────────────────────────────────────────────
    status     = data.get("status","Unknown")
    status_cls = "pill-green" if status.lower()=="active" else "pill-red"
    website    = st.session_state.get("website","") or ""

    st.markdown(f"""
    <div class="card" style="margin-bottom:24px">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div>
                <div style="font-size:28px;font-weight:800;color:#0f172a;margin-bottom:6px">{data.get('legal_name','')}</div>
                <div style="font-size:14px;color:#64748b;margin-bottom:12px">Trade Name: {data.get('trade_name','—')}</div>
                <span class="pill {status_cls}">{'✅' if status.lower()=='active' else '❌'} {status}</span>
                <span class="pill pill-gray">📅 {data.get('reg_date','—')}</span>
                <span class="pill pill-blue">🏢 {data.get('business_type','—')}</span>
                <span class="pill pill-gray">📍 {data.get('state','—')}</span>
                {f'<a href="https://{website}" target="_blank" class="website-chip">🌐 {website} ↗</a>' if website else ''}
            </div>
            <div style="font-size:56px">🏛️</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── 3 detail boxes ─────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown(f"""
        <div class="card">
            <div class="sec-title">GSTIN & Address</div>
            <div style="font-family:monospace;font-size:17px;font-weight:700;color:#0f172a;margin-bottom:12px">{data.get('gstin','')}</div>
            <div style="font-size:13px;color:#475569;line-height:1.7">{data.get('address','Not available')}</div>
        </div>
        """, unsafe_allow_html=True)

    with c2:
        hsn = data.get("hsn_codes",[])
        hsn_html = "".join(f'<span class="pill pill-blue">{h}</span>' for h in hsn) if hsn else "<span style='color:#94a3b8;font-size:13px'>None listed</span>"
        filings  = data.get("filing_status",[])
        filed    = sum(1 for f in filings if f.get("status","").lower()=="filed")
        total    = len(filings)
        pct      = int(filed/max(1,total)*100)
        pct_color= "#22c55e" if pct==100 else "#f59e0b" if pct>=60 else "#ef4444"
        st.markdown(f"""
        <div class="card">
            <div class="sec-title">Business Info</div>
            <div style="margin-bottom:12px">
                <div style="font-size:11px;color:#94a3b8;font-weight:600;text-transform:uppercase">Type</div>
                <div style="font-weight:600;color:#0f172a">{data.get('business_type','—')}</div>
            </div>
            <div style="margin-bottom:12px">
                <div style="font-size:11px;color:#94a3b8;font-weight:600;text-transform:uppercase">State</div>
                <div style="font-weight:600;color:#0f172a">{data.get('state','—')}</div>
            </div>
            <div>
                <div style="font-size:11px;color:#94a3b8;font-weight:600;text-transform:uppercase;margin-bottom:6px">HSN / SAC Codes</div>
                {hsn_html}
            </div>
        </div>
        """, unsafe_allow_html=True)

    with c3:
        st.markdown(f"""
        <div class="card" style="text-align:center">
            <div class="sec-title">Filing Compliance</div>
            <div style="font-size:52px;font-weight:900;color:{pct_color};line-height:1">{pct}%</div>
            <div style="font-size:13px;color:#64748b;margin-top:6px">{filed} of {total} returns filed</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Directors ──────────────────────────────────────────────────────────
    directors = data.get("directors",[])
    if directors:
        st.markdown('<div class="sec-title">👥 Directors & Key Personnel</div>', unsafe_allow_html=True)
        dcols = st.columns(min(len(directors), 4))
        for col, d in zip(dcols, directors):
            initials = "".join(w[0] for w in d.get("name","?").split()[:2])
            with col:
                st.markdown(f"""
                <div class="dir-card">
                    <div class="dir-avatar">{initials}</div>
                    <div>
                        <div style="font-weight:700;font-size:14px;color:#0f172a">{d.get('name','—')}</div>
                        <div style="font-size:12px;color:#64748b">{d.get('designation','—')}</div>
                        <div style="font-size:11px;color:#94a3b8">DIN: {d.get('din','—')}</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Filing History ─────────────────────────────────────────────────────
    filings = data.get("filing_status",[])
    if filings:
        st.markdown('<div class="sec-title">📄 GST Filing History</div>', unsafe_allow_html=True)
        fcols = st.columns(min(len(filings), 3))
        for col, f in zip(fcols * 10, filings):
            s    = f.get("status","").lower()
            icon = "✅" if s=="filed" else "❌"
            cls  = "filing-ok" if s=="filed" else "filing-bad"
            clr  = "#166534" if s=="filed" else "#991b1b"
            with col:
                st.markdown(f"""
                <div class="filing-row {cls}">
                    <div>
                        <div style="font-weight:700;font-size:14px">{f.get('ret_typ','')}</div>
                        <div style="font-size:12px;color:#64748b">{f.get('period','')}</div>
                    </div>
                    <div style="font-weight:600;color:{clr}">{icon} {f.get('status','')}</div>
                </div>
                """, unsafe_allow_html=True)
