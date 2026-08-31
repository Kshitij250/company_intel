"""
generate_report.py  —  CareEdge-style AI Due Diligence Report
==============================================================
Produces a professional multi-page PDF report matching the layout
visible in the reference images:
  • Cover page (navy gradient, company name, rating badge)
  • Executive Summary  (score strip, recommendation boxes, narrative)
  • About the Company  (profile table, leadership, directors)
  • Key Financial Summary  (standalone + consolidated side-by-side, ₹ Cr)
  • Key Ratios  (side-by-side standalone / consolidated)
  • Market & NSE Data
  • Legal History  (findings strip, pending cases table, news table)
  • GST Verification & Compliance
  • Pillar Scorecard
  • Credit Drivers & Risk Factors
  • Disclaimer

Usage:
    python generate_report.py            # generates sample AMNSIL report
    from generate_report import generate_due_diligence_report
    path = generate_due_diligence_report(data_dict, "report.pdf")
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
)

# ── Brand palette ──────────────────────────────────────────────────────────────
NAVY        = colors.HexColor("#0D1F3C")
NAVY_MID    = colors.HexColor("#163155")
NAVY_LIGHT  = colors.HexColor("#1E4080")
TEAL        = colors.HexColor("#00869B")
TEAL_LITE   = colors.HexColor("#E6F4F6")
TEAL_MED    = colors.HexColor("#B2E0E8")
GOLD        = colors.HexColor("#C49A22")
RED_RISK    = colors.HexColor("#C0392B")
RED_LITE    = colors.HexColor("#FDECEA")
GREEN_OK    = colors.HexColor("#1A7A4A")
GREEN_LITE  = colors.HexColor("#E8F5EE")
AMBER       = colors.HexColor("#D4812A")
AMBER_LITE  = colors.HexColor("#FEF6E7")
GREY_BG     = colors.HexColor("#F5F7FA")
GREY_LINE   = colors.HexColor("#CBD5E0")
GREY_DARK   = colors.HexColor("#4A5568")
WHITE       = colors.white
BLACK       = colors.HexColor("#0F172A")
TEXT_GREY   = colors.HexColor("#475569")
LIGHT_BLUE  = colors.HexColor("#EBF0FA")

PAGE_W, PAGE_H = A4
MARGIN_L = MARGIN_R = 1.6 * cm
MARGIN_T = 2.0 * cm
MARGIN_B = 1.6 * cm
BODY_W   = PAGE_W - MARGIN_L - MARGIN_R

# ── Utilities ──────────────────────────────────────────────────────────────────

def sp(n: float = 1) -> Spacer:
    return Spacer(1, n * 4)

def hr(color=GREY_LINE, thickness=0.5, space_after=4) -> HRFlowable:
    return HRFlowable(width="100%", thickness=thickness, color=color,
                      spaceAfter=space_after, spaceBefore=2)

def safe(v: Any, default="—") -> str:
    if v is None or v == "" or v != v:  # nan check
        return default
    return str(v)

def _para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(str(text or ""), style)


# ══════════════════════════════════════════════════════════════════════════════
# STYLES
# ══════════════════════════════════════════════════════════════════════════════

def build_styles() -> dict:
    def s(name, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    return {
        # Cover
        "cover_eyebrow": s("cover_eyebrow", fontName="Helvetica-Bold", fontSize=8,
                           textColor=TEAL, leading=11, spaceAfter=4),
        "cover_reptype":  s("cover_reptype", fontName="Helvetica", fontSize=10,
                            textColor=colors.HexColor("#90AFC5"), leading=13, spaceAfter=4),
        "cover_company":  s("cover_company", fontName="Helvetica-Bold", fontSize=20,
                            textColor=WHITE, leading=26, spaceAfter=6),
        "cover_meta":     s("cover_meta", fontName="Helvetica", fontSize=8.5,
                            textColor=colors.HexColor("#7FAABD"), leading=13, spaceAfter=2),
        "cover_rating":   s("cover_rating", fontName="Helvetica-Bold", fontSize=11,
                            textColor=WHITE, leading=14, alignment=TA_CENTER),
        # Section / sub headers
        "sec_hdr":   s("sec_hdr",  fontName="Helvetica-Bold", fontSize=9.5,
                       textColor=WHITE,    leading=14),
        "sub_hdr":   s("sub_hdr",  fontName="Helvetica-Bold", fontSize=9,
                       textColor=NAVY,    leading=13, spaceBefore=6, spaceAfter=3),
        "sub_hdr2":  s("sub_hdr2", fontName="Helvetica-Bold", fontSize=8.5,
                       textColor=NAVY_MID,leading=12, spaceBefore=4, spaceAfter=2),
        # Body
        "body":    s("body",    fontName="Helvetica", fontSize=8.5, textColor=BLACK,
                     leading=13, spaceBefore=2, spaceAfter=2, alignment=TA_JUSTIFY),
        "body_sm": s("body_sm", fontName="Helvetica", fontSize=7.5, textColor=TEXT_GREY,
                     leading=11, spaceBefore=1, spaceAfter=1),
        "bullet":  s("bullet",  fontName="Helvetica", fontSize=8.5, textColor=BLACK,
                     leading=13, leftIndent=12, firstLineIndent=-10,
                     spaceBefore=2, spaceAfter=2, alignment=TA_JUSTIFY),
        "bullet_b": s("bullet_b", fontName="Helvetica-Bold", fontSize=8.5, textColor=BLACK,
                      leading=13, leftIndent=12, firstLineIndent=-10,
                      spaceBefore=2, spaceAfter=2),
        # Table cells
        "th":      s("th",     fontName="Helvetica-Bold", fontSize=7.5,
                     textColor=WHITE,    leading=10, alignment=TA_CENTER),
        "th_l":    s("th_l",   fontName="Helvetica-Bold", fontSize=7.5,
                     textColor=WHITE,    leading=10, alignment=TA_LEFT),
        "td_lbl":  s("td_lbl", fontName="Helvetica-Bold", fontSize=7.5,
                     textColor=BLACK,    leading=10),
        "td_val":  s("td_val", fontName="Helvetica",      fontSize=7.5,
                     textColor=BLACK,    leading=10, alignment=TA_CENTER),
        "td_val_l":s("td_val_l",fontName="Helvetica",     fontSize=7.5,
                     textColor=BLACK,    leading=10),
        "td_val_r":s("td_val_r",fontName="Helvetica",     fontSize=7.5,
                     textColor=BLACK,    leading=10, alignment=TA_RIGHT),
        "td_sm":   s("td_sm",  fontName="Helvetica",      fontSize=6.8,
                     textColor=TEXT_GREY,leading=9,  alignment=TA_CENTER),
        # KPI
        "kpi_val": s("kpi_val", fontName="Helvetica-Bold", fontSize=16,
                     textColor=WHITE, leading=20, alignment=TA_CENTER),
        "kpi_lbl": s("kpi_lbl", fontName="Helvetica",      fontSize=7,
                     textColor=colors.HexColor("#D0ECF1"), leading=9, alignment=TA_CENTER),
        "kpi_sub": s("kpi_sub", fontName="Helvetica",      fontSize=6.5,
                     textColor=colors.HexColor("#B0D8E0"), leading=9, alignment=TA_CENTER),
        # Risk band bar
        "risk_band": s("risk_band", fontName="Helvetica-Bold", fontSize=7.5,
                       textColor=WHITE, leading=10, alignment=TA_CENTER),
        # Misc
        "footer":   s("footer",  fontName="Helvetica", fontSize=6.5,
                      textColor=TEXT_GREY, leading=9, alignment=TA_CENTER),
        "disc":     s("disc",    fontName="Helvetica", fontSize=6.5,
                      textColor=TEXT_GREY, leading=9, alignment=TA_JUSTIFY),
        "rs_note":  s("rs_note", fontName="Helvetica", fontSize=7.5,
                      textColor=TEXT_GREY, leading=9, alignment=TA_RIGHT, spaceAfter=2),
        "tag_ok":   s("tag_ok",  fontName="Helvetica-Bold", fontSize=7,
                      textColor=GREEN_OK, leading=9),
        "tag_bad":  s("tag_bad", fontName="Helvetica-Bold", fontSize=7,
                      textColor=RED_RISK, leading=9),
        "cat_label":s("cat_label",fontName="Helvetica-Bold", fontSize=7.5,
                      textColor=WHITE, leading=10, alignment=TA_CENTER),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PAGE TEMPLATE (header stripe + footer on inner pages)
# ══════════════════════════════════════════════════════════════════════════════

class ReportDoc(BaseDocTemplate):
    def __init__(self, filename, company_name, report_date, **kw):
        super().__init__(filename, **kw)
        self.company_name = company_name
        self.report_date  = report_date
        self.ST           = build_styles()
        self._add_templates()

    def _add_templates(self):
        cover_frame = Frame(MARGIN_L, MARGIN_B, BODY_W,
                            PAGE_H - MARGIN_T - MARGIN_B, id="cover")
        inner_frame = Frame(MARGIN_L, MARGIN_B + 0.55*cm, BODY_W,
                            PAGE_H - MARGIN_T - MARGIN_B - 1.1*cm, id="inner")
        self.addPageTemplates([
            PageTemplate(id="Cover", frames=[cover_frame]),
            PageTemplate(id="Inner", frames=[inner_frame],
                         onPage=self._chrome),
        ])

    def _chrome(self, canvas, doc):
        canvas.saveState()
        # ── Top bar ──
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_H - 1.35*cm, PAGE_W, 1.35*cm, fill=1, stroke=0)
        canvas.setFillColor(TEAL)
        canvas.rect(0, PAGE_H - 1.35*cm, PAGE_W, 0.18*cm, fill=1, stroke=0)

        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.setFillColor(WHITE)
        canvas.drawString(MARGIN_L, PAGE_H - 0.92*cm,
                          self.company_name.upper()[:70])
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#90AFC5"))
        canvas.drawRightString(PAGE_W - MARGIN_R, PAGE_H - 0.92*cm,
                               "CareEdge  |  AI Due Diligence Report")

        # ── Divider line ──
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(TEXT_GREY)
        canvas.setStrokeColor(GREY_LINE)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN_L, MARGIN_B + 0.5*cm,
                    PAGE_W - MARGIN_R, MARGIN_B + 0.5*cm)
        canvas.drawString(MARGIN_L, MARGIN_B + 0.15*cm,
                          f"Confidential  |  {self.report_date}  |  For Internal Use Only")
        canvas.drawRightString(PAGE_W - MARGIN_R, MARGIN_B + 0.15*cm,
                               f"Page {doc.page}")
        canvas.restoreState()


# ══════════════════════════════════════════════════════════════════════════════
# SHARED TABLE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def section_header(text: str, ST: dict) -> Table:
    tbl = Table([[_para(text, ST["sec_hdr"])]], colWidths=[BODY_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))
    return tbl

def sub_header(text: str, ST: dict) -> Table:
    tbl = Table([[_para(text, ST["sub_hdr"])]], colWidths=[BODY_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), TEAL_LITE),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("LINEBELOW",     (0, 0), (-1, -1), 1.5, TEAL),
    ]))
    return tbl

def fin_table(headers: list, rows: list, ST: dict,
              col_widths: list | None = None) -> Table:
    """Standard financial data table with navy header row."""
    n = len(headers)
    if col_widths is None:
        first_w = BODY_W * 0.35
        rest_w  = (BODY_W - first_w) / max(1, n - 1)
        col_widths = [first_w] + [rest_w] * (n - 1)

    data_rows = [[_para(h, ST["th"] if i else ST["th_l"]) for i, h in enumerate(headers)]]
    for row in rows:
        data_rows.append([
            _para(str(c), ST["td_lbl"] if i == 0 else ST["td_val"])
            for i, c in enumerate(row)
        ])

    tbl = Table(data_rows, colWidths=col_widths, repeatRows=1)
    ts = [
        ("BACKGROUND",    (0, 0), (-1, 0),  NAVY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [WHITE, GREY_BG]),
        ("GRID",          (0, 0), (-1, -1), 0.3, GREY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    # Bold totals / net profit / net worth rows
    for i, row in enumerate(rows, 1):
        lbl = str(row[0]).lower()
        if any(k in lbl for k in ["total", "net profit", "net worth", "pat", "pbt",
                                    "ebitda", "cwip", "share capital"]):
            ts.append(("FONTNAME",   (0, i), (-1, i), "Helvetica-Bold"))
            ts.append(("BACKGROUND", (0, i), (-1, i), LIGHT_BLUE))
    tbl.setStyle(TableStyle(ts))
    return tbl


def kpi_strip(kpis: list[dict], ST: dict, tile_w: float | None = None) -> Table:
    """Row of coloured KPI tiles: [{label, value, sub?, color}]"""
    n = max(1, len(kpis))
    tw = tile_w or (BODY_W / n)
    tiles = []
    for idx, k in enumerate(kpis):
        bg  = k.get("color", TEAL)
        v_s = ParagraphStyle(f"kv{idx}", fontName="Helvetica-Bold", fontSize=14,
                             textColor=WHITE, leading=18, alignment=TA_CENTER)
        l_s = ParagraphStyle(f"kl{idx}", fontName="Helvetica", fontSize=6.5,
                             textColor=colors.HexColor("#D0ECF1"), leading=9, alignment=TA_CENTER)
        s_s = ParagraphStyle(f"ks{idx}", fontName="Helvetica", fontSize=6,
                             textColor=colors.HexColor("#B0D8E0"), leading=8, alignment=TA_CENTER)
        inner = [[_para(str(k["value"]), v_s)], [_para(k["label"], l_s)]]
        if k.get("sub"):
            inner.append([_para(k["sub"], s_s)])
        tile = Table(inner, colWidths=[tw - 2])
        tile.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), bg),
            ("ALIGN",         (0,0),(-1,-1), "CENTER"),
            ("TOPPADDING",    (0,0),(-1,-1), 6),
            ("BOTTOMPADDING", (0,0),(-1,-1), 6),
            ("LEFTPADDING",   (0,0),(-1,-1), 3),
            ("RIGHTPADDING",  (0,0),(-1,-1), 3),
            ("GRID",          (0,0),(-1,-1), 0.3, GREY_LINE),
        ]))
        tiles.append(tile)
    outer = Table([tiles], colWidths=[tw] * n)
    outer.setStyle(TableStyle([
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
    ]))
    return outer


def risk_band_bar(score: int, ST: dict) -> Table:
    """Horizontal 5-band bar with active band highlighted."""
    bands = [
        ("80–100\nExcellent",    GREEN_OK),
        ("60–79\nGood",          TEAL),
        ("40–59\nModerate",      AMBER),
        ("20–39\nCaution",       colors.HexColor("#D4541A")),
        ("0–19\nHigh Risk",      RED_RISK),
    ]
    def _active(i):
        return (i==0 and score>=80) or (i==1 and 60<=score<80) or \
               (i==2 and 40<=score<60) or (i==3 and 20<=score<40) or \
               (i==4 and score<20)

    cells = [[_para(b[0], ST["risk_band"]) for b in bands]]
    tbl   = Table(cells, colWidths=[BODY_W/5]*5)
    ts = [("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
          ("ALIGN",(0,0),(-1,-1),"CENTER"), ("GRID",(0,0),(-1,-1),0.5,WHITE)]
    for i, (_, c) in enumerate(bands):
        alpha = 1.0 if _active(i) else 0.3
        r = int(c.red*255*alpha + 255*(1-alpha))
        g = int(c.green*255*alpha + 255*(1-alpha))
        b = int(c.blue*255*alpha + 255*(1-alpha))
        faded = colors.HexColor(f"#{r:02X}{g:02X}{b:02X}")
        ts.append(("BACKGROUND", (i,0),(i,0), faded))
        if _active(i):
            ts.append(("FONTNAME", (i,0),(i,0), "Helvetica-Bold"))
            ts.append(("FONTSIZE", (i,0),(i,0), 8))
    tbl.setStyle(TableStyle(ts))
    return tbl


def two_col_info_table(left_rows: list, right_rows: list, ST: dict,
                        col_ratios=(0.16, 0.34, 0.16, 0.34)) -> Table:
    """4-column key-value grid used for company profile & market data."""
    cw = [BODY_W * r for r in col_ratios]
    max_r = max(len(left_rows), len(right_rows))
    while len(left_rows) < max_r:  left_rows.append(("", ""))
    while len(right_rows) < max_r: right_rows.append(("", ""))
    rows = []
    for l, r in zip(left_rows, right_rows):
        rows.append([_para(l[0], ST["td_lbl"]),  _para(safe(l[1]), ST["td_val_l"]),
                     _para(r[0], ST["td_lbl"]),  _para(safe(r[1]), ST["td_val_l"])])
    tbl = Table(rows, colWidths=cw)
    tbl.setStyle(TableStyle([
        ("GRID",          (0,0),(-1,-1), 0.3, GREY_LINE),
        ("ROWBACKGROUNDS",(0,0),(-1,-1), [WHITE, GREY_BG]),
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
        ("LEFTPADDING",   (0,0),(-1,-1), 5),
        ("RIGHTPADDING",  (0,0),(-1,-1), 5),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("FONTNAME",      (0,0),(0,-1),  "Helvetica-Bold"),
        ("FONTNAME",      (2,0),(2,-1),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),(-1,-1), 7.5),
    ]))
    return tbl


def side_by_side(left_tbl, right_tbl, gap: float = 0.4*cm) -> Table:
    """Place two flowables side by side with a small gap."""
    w = (BODY_W - gap) / 2
    outer = Table([[left_tbl, right_tbl]], colWidths=[w, w])
    outer.setStyle(TableStyle([
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(0,-1),  gap/2),
        ("RIGHTPADDING",  (1,0),(1,-1),  0),
        ("LEFTPADDING",   (1,0),(1,-1),  gap/2),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
    ]))
    return outer


# ══════════════════════════════════════════════════════════════════════════════
# COVER PAGE
# ══════════════════════════════════════════════════════════════════════════════

def cover_page(story: list, data: dict, ST: dict):
    company   = data.get("company_name", "Company")
    rep_type  = data.get("report_type", "Vendor Assessment")
    rep_date  = data.get("report_date", datetime.now().strftime("%B %Y"))
    gstin     = data.get("gstin", "")
    sector    = data.get("sector", "")
    rating    = data.get("overall_rating", "")

    an = data.get("analysis", {})
    overall = an.get("overall", {})
    score   = overall.get("score", 0)
    band    = overall.get("band", "")
    color_key = overall.get("color", "amber")
    col_map = {"green": GREEN_OK, "blue": TEAL, "amber": AMBER,
               "orange": colors.HexColor("#D4541A"), "red": RED_RISK}
    rating_color = col_map.get(color_key, AMBER)

    # Full-page navy accent
    accent = Table([[""]], colWidths=[BODY_W], rowHeights=[0.5*cm])
    accent.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1), TEAL)]))
    story.append(accent)
    story.append(sp(6))

    rows = [
        [_para("AI DUE DILIGENCE", ST["cover_eyebrow"])],
        [_para(rep_type.upper(), ST["cover_reptype"])],
        [sp(4)],
        [_para(company, ST["cover_company"])],
        [sp(2)],
    ]

    meta_items = []
    if gstin:  meta_items.append(f"GSTIN: {gstin}")
    if sector: meta_items.append(f"Sector: {sector}")
    meta_items.append(f"Report Date: {rep_date}")
    meta_items.append("Confidential — For Internal Use Only")
    for m in meta_items:
        rows.append([_para(m, ST["cover_meta"])])

    rows.append([sp(6)])

    if score or band:
        rating_txt = rating or f"Score: {score}/100 — {band}"
        badge = Table([[_para(f"Overall Rating: {rating_txt}", ST["cover_rating"])]],
                      colWidths=[9*cm])
        badge.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), rating_color),
            ("TOPPADDING",    (0,0),(-1,-1), 8),
            ("BOTTOMPADDING", (0,0),(-1,-1), 8),
            ("LEFTPADDING",   (0,0),(-1,-1), 14),
            ("RIGHTPADDING",  (0,0),(-1,-1), 14),
        ]))
        rows.append([badge])

    rows.append([sp(12)])
    rows.append([_para("carerating.com  |  care-analytics.com", ST["cover_meta"])])

    cover_tbl = Table(rows, colWidths=[BODY_W])
    cover_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), NAVY),
        ("LEFTPADDING",   (0,0),(-1,-1), 1.2*cm),
        ("TOPPADDING",    (0,0),(-1,-1), 2),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
        ("RIGHTPADDING",  (0,0),(-1,-1), 1.2*cm),
    ]))
    story.append(cover_tbl)
    story.append(NextPageTemplate("Inner"))
    story.append(PageBreak())


# ══════════════════════════════════════════════════════════════════════════════
# 1. EXECUTIVE SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def section_executive_summary(story: list, data: dict, ST: dict):
    story.append(section_header("1. Executive Summary", ST))
    story.append(sp(2))

    an = data.get("analysis", {}) or {}
    overall  = an.get("overall", {})
    segs     = an.get("segments", {})
    score    = overall.get("score", 0)
    band     = overall.get("band", "—")
    color_k  = overall.get("color", "amber")
    col_map  = {"green": GREEN_OK, "blue": TEAL, "amber": AMBER,
                "orange": colors.HexColor("#D4541A"), "red": RED_RISK}
    score_c  = col_map.get(color_k, AMBER)

    # ── KPI strip ─────────────────────────────────────────────────────────
    kpis = [
        {"label": "Overall Score",      "value": f"{score}/100", "sub": band,
         "color": score_c},
        {"label": "B2B Credit",         "value": segs.get("credit",{}).get("score","—"),
         "sub":   segs.get("credit",{}).get("band",""),   "color": NAVY_LIGHT},
        {"label": "Vendor Trust",       "value": segs.get("vendor",{}).get("score","—"),
         "sub":   segs.get("vendor",{}).get("band",""),   "color": TEAL},
        {"label": "Investment",         "value": segs.get("investment",{}).get("score","—"),
         "sub":   segs.get("investment",{}).get("band",""),"color": colors.HexColor("#5B4DBE")},
    ]
    story.append(kpi_strip(kpis, ST))
    story.append(sp(2))
    story.append(risk_band_bar(score, ST))
    story.append(sp(3))

    # ── Recommendation boxes ────────────────────────────────────────────────
    rec_cells = []
    for seg_key, seg_label in [("credit","B2B Credit"),("vendor","Vendor Trust"),("investment","Investment")]:
        seg   = segs.get(seg_key, {})
        verdict = seg.get("verdict", "—")
        sc    = seg.get("score", 0)
        ck    = seg.get("color", "amber")
        bg    = col_map.get(ck, AMBER)
        w     = (BODY_W / 3) - 4
        hdr_s = ParagraphStyle(f"rh_{seg_key}", fontName="Helvetica-Bold",
                               fontSize=7.5, textColor=WHITE, leading=10, alignment=TA_CENTER)
        bod_s = ParagraphStyle(f"rb_{seg_key}", fontName="Helvetica",
                               fontSize=7, textColor=BLACK, leading=10, alignment=TA_CENTER)
        sc_s  = ParagraphStyle(f"rs_{seg_key}", fontName="Helvetica-Bold",
                               fontSize=12, textColor=bg, leading=14, alignment=TA_CENTER)
        cell = Table([
            [_para(f"{seg_label.upper()}  •  {sc}/100", hdr_s)],
            [_para(verdict, bod_s)],
        ], colWidths=[w])
        cell.setStyle(TableStyle([
            ("BACKGROUND", (0,0),(-1,0), bg),
            ("BACKGROUND", (0,1),(-1,1), GREY_BG),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 5),
            ("RIGHTPADDING",  (0,0),(-1,-1), 5),
            ("GRID", (0,0),(-1,-1), 0.3, GREY_LINE),
        ]))
        rec_cells.append(cell)
    story.append(Table([rec_cells], colWidths=[BODY_W/3]*3))
    story.append(sp(3))

    # ── Narrative ───────────────────────────────────────────────────────────
    narrative = an.get("ai_narrative") or data.get("summary_text") or \
        "Comprehensive due diligence analysis completed across GST, financials, market, legal, and web pillars."
    for para in narrative.split("\n\n"):
        if para.strip():
            story.append(_para(para.strip(), ST["body"]))
            story.append(sp())

    # ── Credit limit note (if present) ────────────────────────────────────
    credit_note = data.get("credit_limit_note", "")
    if credit_note:
        story.append(sp(2))
        note_tbl = Table([[_para(credit_note, ST["body"])]], colWidths=[BODY_W])
        note_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), LIGHT_BLUE),
            ("LEFTPADDING",   (0,0),(-1,-1), 8),
            ("RIGHTPADDING",  (0,0),(-1,-1), 8),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LINERIGHT",     (0,0),(0,-1), 3, NAVY),
        ]))
        story.append(note_tbl)
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 2. ABOUT THE COMPANY
# ══════════════════════════════════════════════════════════════════════════════

def section_company_profile(story: list, data: dict, ST: dict):
    story.append(section_header("2. About the Company", ST))
    story.append(sp(2))

    rag  = data.get("rag_data") or {}
    ov   = rag.get("company_overview", {}) if rag else {}
    ps   = rag.get("products_services", {}) if rag else {}
    lead = rag.get("leadership_team", []) if rag else []
    ct   = rag.get("contact_information", {}) if rag else {}
    gst  = data.get("gst_data", {}) or {}

    desc = ov.get("description") or data.get("company_description", "")
    if desc:
        story.append(_para(desc, ST["body"]))
        story.append(sp(2))

    # ── Profile grid ──────────────────────────────────────────────────────
    left_rows = [
        ("Legal Name",      gst.get("legal_name") or ov.get("name","—")),
        ("Trade Name",      gst.get("trade_name","—")),
        ("GSTIN",           gst.get("gstin","—")),
        ("GST Status",      gst.get("status","—")),
        ("Business Type",   gst.get("business_type","—")),
        ("Registered Date", gst.get("reg_date","—")),
    ]
    right_rows = [
        ("State",         gst.get("state","—")),
        ("Industry",      ov.get("industry","—")),
        ("Founded",       ov.get("founded_year","—")),
        ("Headquarters",  ov.get("headquarters","—")),
        ("Employees",     ov.get("employee_count","—")),
        ("Address",       gst.get("address","—")),
    ]
    story.append(two_col_info_table(left_rows, right_rows, ST))
    story.append(sp(3))

    # ── Products ────────────────────────────────────────────────────────────
    offerings = ps.get("main_offerings") or []
    usp       = ps.get("usp") or ""
    if offerings or usp:
        story.append(sub_header("Products & Services", ST))
        if usp:
            story.append(_para(f"<b>Key Differentiator:</b> {usp}", ST["body"]))
            story.append(sp())
        if offerings:
            for item in offerings:
                if item and item not in ("Not found", "Not Available"):
                    story.append(_para(f"\u2022 {item}", ST["bullet"]))
        story.append(sp(3))

    # ── Leadership ──────────────────────────────────────────────────────────
    valid_lead = [l for l in lead if l.get("name") and l["name"] not in ("Not found",)]
    if valid_lead:
        story.append(sub_header("Key Personnel", ST))
        rows = [["Name", "Designation"]] + \
               [[l.get("name",""), l.get("designation","")] for l in valid_lead]
        hdrs = rows[0]
        data_r = rows[1:]
        tbl = fin_table(hdrs, data_r, ST, col_widths=[BODY_W*0.5, BODY_W*0.5])
        story.append(tbl)
        story.append(sp(3))

    # ── GST Directors ───────────────────────────────────────────────────────
    directors = gst.get("directors", [])
    if directors:
        story.append(sub_header("Directors (GST Portal)", ST))
        hdrs  = ["Name", "Designation", "DIN"]
        d_rows = [[d.get("name",""), d.get("designation",""), d.get("din","")] for d in directors]
        tbl = fin_table(hdrs, d_rows, ST,
                        col_widths=[BODY_W*0.45, BODY_W*0.35, BODY_W*0.20])
        story.append(tbl)
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 3. KEY FINANCIAL SUMMARY  (exactly matching CareEdge image layout)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_fin_rows(block: dict, section_key: str, n_years: int = 3):
    """Pull last n years + rows from a screener block section."""
    sec   = (block or {}).get(section_key, {})
    years = sec.get("years", [])[-n_years:]
    rows  = sec.get("rows", [])
    return years, rows

def _build_pl_table(years: list, rows: list, title: str, ST: dict) -> Table | None:
    if not rows:
        return None
    # Short year labels like "FY 24 (A)"
    def yr_label(y):
        m = __import__("re").search(r"(\d{4})", y)
        return f"FY {m.group(1)[-2:]} (A)" if m else y

    hdrs = [title] + [yr_label(y) for y in years]
    key_pats = ["revenue","sales","ebidta","ebitda","other income","depreciation",
                "finance","pbt","pat","share capital","reserves","net worth",
                "long term","short term borrow","trade payables","trade receivables",
                "fixed assets","cwip","inventories","cash & bank","borrowings"]
    key_rows_filt = [r for r in rows if any(k in (r.get("particular","")).lower() for k in key_pats)]
    if not key_rows_filt:
        key_rows_filt = rows[:20]
    tbl_rows = []
    for r in key_rows_filt:
        row = [r.get("particular","")]
        for y in years:
            row.append(safe(r.get("values",{}).get(y)))
        tbl_rows.append(row)
    n  = len(years)
    fw = BODY_W / 2          # half BODY_W (side-by-side)
    fw -= 0.2*cm
    lw = fw * 0.42
    cw = [lw] + [(fw - lw)/max(1,n)] * n
    return fin_table(hdrs, tbl_rows, ST, col_widths=cw)

def section_financials(story: list, data: dict, ST: dict):
    story.append(section_header("3. Key Financial Summary — Standalone & Consolidated Financials", ST))
    story.append(sp())
    story.append(_para("A snapshot of the Company's standalone and consolidated financials are given below:",
                       ST["body_sm"]))
    story.append(_para("(Rs. Crore)", ST["rs_note"]))

    scr    = data.get("screener_data") or {}
    block_s = scr.get("standalone") or {}
    block_c = scr.get("consolidated") or {}

    pl_s_y, pl_s_r = _extract_fin_rows(block_s, "profit_loss")
    pl_c_y, pl_c_r = _extract_fin_rows(block_c, "profit_loss")

    t_s = _build_pl_table(pl_s_y, pl_s_r, "Particulars (Standalone)", ST)
    t_c = _build_pl_table(pl_c_y, pl_c_r, "Particulars (Consolidated)", ST)

    if t_s and t_c:
        story.append(side_by_side(t_s, t_c))
    elif t_s:
        story.append(t_s)
    elif t_c:
        story.append(t_c)
    else:
        story.append(_para("Financial data not available.", ST["body_sm"]))

    story.append(sp())
    story.append(_para("* A = Audited Financials", ST["body_sm"]))
    story.append(sp(3))

    # ── Key Ratios ──────────────────────────────────────────────────────────
    story.append(sub_header("Key Ratios", ST))
    story.append(_para("(Rs. Crore)", ST["rs_note"]))

    rat_s_y, rat_s_r = _extract_fin_rows(block_s, "ratios")
    rat_c_y, rat_c_r = _extract_fin_rows(block_c, "ratios")

    def _ratio_tbl(years, rows, title):
        if not rows:
            return None
        def yr_label(y):
            m = __import__("re").search(r"(\d{4})", y)
            return f"FY {m.group(1)[-2:]} (A)" if m else y
        hdrs = [title] + [yr_label(y) for y in years]
        tbl_rows = []
        for r in rows:
            row = [r.get("particular","")]
            for y in years:
                row.append(safe(r.get("values",{}).get(y)))
            tbl_rows.append(row)
        n  = len(years)
        fw = BODY_W / 2 - 0.2*cm
        lw = fw * 0.50
        cw = [lw] + [(fw - lw)/max(1,n)] * n
        return fin_table(hdrs, tbl_rows, ST, col_widths=cw)

    rt_s = _ratio_tbl(rat_s_y, rat_s_r, "Ratios (Standalone)")
    rt_c = _ratio_tbl(rat_c_y, rat_c_r, "Ratios (Consolidated)")

    if rt_s and rt_c:
        story.append(side_by_side(rt_s, rt_c))
    elif rt_s:
        story.append(rt_s)
    elif rt_c:
        story.append(rt_c)

    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 4. MARKET & NSE
# ══════════════════════════════════════════════════════════════════════════════

def section_market(story: list, data: dict, ST: dict):
    story.append(section_header("4. Market & NSE Data", ST))
    story.append(sp(2))

    nse = data.get("nse_data") or {}
    if not nse.get("success"):
        story.append(_para("NSE/market data not available (company may be unlisted).", ST["body"]))
        story.append(sp(3))
        return

    mock_tag = " (Indicative / Mock)" if nse.get("_mock") else ""
    story.append(_para(f"Live NSE quote for <b>{nse.get('symbol','')}</b>{mock_tag}", ST["body"]))
    story.append(sp())

    def _chg(v):
        if v is None: return "—"
        return f"{v:+.2f}%" if isinstance(v, float) else str(v)

    left_rows = [
        ("Current Price (₹)",   safe(nse.get("price"))),
        ("Day Change",          _chg(nse.get("change_pct"))),
        ("52-Week High (₹)",    safe(nse.get("week52_high"))),
        ("52-Week Low (₹)",     safe(nse.get("week52_low"))),
        ("P/E Ratio (x)",       safe(nse.get("pe_ratio"))),
        ("P/B Ratio (x)",       safe(nse.get("pb_ratio"))),
    ]
    right_rows = [
        ("EPS (₹)",             safe(nse.get("eps"))),
        ("Market Cap (₹ Cr)",   safe(nse.get("market_cap_cr"))),
        ("Beta",                safe(nse.get("beta"))),
        ("Volume",              safe(nse.get("volume"))),
        ("Sector",              safe(nse.get("sector"))),
        ("Industry",            safe(nse.get("industry"))),
    ]
    story.append(two_col_info_table(left_rows, right_rows, ST))
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 5. LEGAL HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def section_legal(story: list, data: dict, ST: dict):
    story.append(section_header("5. Legal History & News Intelligence", ST))
    story.append(sp(2))

    legal    = data.get("legal_data") or {}
    cases    = legal.get("cases") or []
    articles = legal.get("articles") or []
    kpis_d   = legal.get("kpis") or {}

    # ── KPI strip ─────────────────────────────────────────────────────────
    court_n  = kpis_d.get("court_records", len(cases))
    high_n   = kpis_d.get("critical_flags", 0)
    news_n   = kpis_d.get("adverse_news_total", len(articles))
    rb       = (legal.get("risk_band") or "low").lower()
    rb_col   = GREEN_OK if rb=="low" else AMBER if rb=="moderate" else RED_RISK

    kpis = [
        {"label": "Court Records",   "value": court_n, "sub": "Indian Kanoon",
         "color": RED_RISK if court_n > 5 else AMBER},
        {"label": "High Risk Signals","value": high_n, "sub": "Relevant news", "color": RED_RISK},
        {"label": "News Articles",   "value": news_n, "sub": "Scanned",        "color": TEAL},
        {"label": "Legal Risk Band", "value": rb.upper(), "sub": "Overall band","color": rb_col},
    ]
    story.append(kpi_strip(kpis, ST))
    story.append(sp(2))

    # ── Key findings ────────────────────────────────────────────────────────
    findings = legal.get("key_findings") or []
    if findings:
        story.append(sub_header("Key Findings", ST))
        cmap = {"success": GREEN_OK, "warning": AMBER,
                "critical": RED_RISK, "info": TEAL}
        for f in findings:
            bg     = cmap.get(f.get("type","info"), TEAL)
            icon   = f.get("icon","")
            title  = f.get("title","")
            body   = f.get("body","")
            hs = ParagraphStyle("fhdr", fontName="Helvetica-Bold", fontSize=8,
                                textColor=WHITE, leading=11)
            bs = ParagraphStyle("fbdy", fontName="Helvetica", fontSize=7.5,
                                textColor=BLACK, leading=11)
            row_tbl = Table([[_para(f"{icon}  {title}", hs),
                              _para(body, bs)]],
                            colWidths=[BODY_W*0.22, BODY_W*0.78])
            row_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0,0),(0,-1), bg),
                ("BACKGROUND", (1,0),(1,-1), GREY_BG),
                ("TOPPADDING",    (0,0),(-1,-1), 4),
                ("BOTTOMPADDING", (0,0),(-1,-1), 4),
                ("LEFTPADDING",   (0,0),(-1,-1), 6),
                ("RIGHTPADDING",  (0,0),(-1,-1), 6),
                ("GRID", (0,0),(-1,-1), 0.3, GREY_LINE),
            ]))
            story.append(row_tbl)
            story.append(sp())
        story.append(sp(2))

    # ── Pending cases table ────────────────────────────────────────────────
    if cases:
        story.append(sub_header("9.1  List of Pending Cases — Filed Against / By this Corporate", ST))
        story.append(sp())
        import re as _re
        hdrs    = ["Case Category", "Court", "Respondent(s)", "Case No.", "Date of Judgement"]
        c_rows  = []
        for c in cases[:25]:
            title   = c.get("title","")
            court   = (c.get("court","") or "")
            snippet = c.get("snippet","") or ""
            cat = ("Insolvency" if any(k in (title+court).lower() for k in
                   ["insolvenc","ib","ibc","liquidat"])
                   else "Tax" if any(k in (title+court).lower() for k in
                   ["tax","gst","income tax","itat"])
                   else "Civil / Other")
            respondent = title.split(" vs ")[-1][:50] if " vs " in title.lower() else title[:50]
            case_no = _re.search(r"(C\.?P\.?\s*\(IB\)[^,]+|IA\s+\d+[^,]+|Company Appeal[^,]+)",
                                 snippet, _re.IGNORECASE)
            c_rows.append([
                cat,
                court[:20],
                respondent,
                (case_no.group() if case_no else snippet[:30]).strip(),
                c.get("date","—"),
            ])
        cw = [BODY_W*0.13, BODY_W*0.12, BODY_W*0.36, BODY_W*0.26, BODY_W*0.13]
        story.append(fin_table(hdrs, c_rows, ST, col_widths=cw))
        story.append(sp(2))

    # ── News table ─────────────────────────────────────────────────────────
    relevant = [a for a in articles if a.get("relevance") == "relevant"][:15]
    if relevant:
        story.append(sub_header("Recent News & Adverse Signals", ST))
        story.append(sp())
        hdrs = ["Date", "Source", "Headline", "Risk Level"]
        n_rows = []
        for a in relevant:
            pub   = (a.get("published_at","") or "")[:10]
            src   = (a.get("source","") or "")[:18]
            title = (a.get("title","") or "")[:90]
            risk  = (a.get("risk_level","low") or "low").upper()
            n_rows.append([pub, src, title, risk])
        cw = [BODY_W*0.09, BODY_W*0.13, BODY_W*0.65, BODY_W*0.13]
        data_tbl = [
            [_para(h, ST["th"] if i else ST["th_l"]) for i, h in enumerate(hdrs)],
        ]
        for row in n_rows:
            data_tbl.append([_para(str(c), ST["td_val_l"] if i in (1,2) else ST["td_val"])
                             for i, c in enumerate(row)])
        tbl = Table(data_tbl, colWidths=cw, repeatRows=1)
        ts = [
            ("BACKGROUND",    (0,0),(-1,0),  NAVY),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),  [WHITE, GREY_BG]),
            ("GRID",          (0,0),(-1,-1),  0.3, GREY_LINE),
            ("TOPPADDING",    (0,0),(-1,-1),  2),
            ("BOTTOMPADDING", (0,0),(-1,-1),  2),
            ("LEFTPADDING",   (0,0),(-1,-1),  3),
            ("RIGHTPADDING",  (0,0),(-1,-1),  3),
            ("FONTSIZE",      (0,0),(-1,-1),  7),
            ("VALIGN",        (0,0),(-1,-1),  "TOP"),
        ]
        for i, row in enumerate(n_rows, 1):
            risk = row[3]
            bg   = RED_RISK if risk=="HIGH" else AMBER if risk=="MEDIUM" else GREEN_OK
            ts += [("BACKGROUND",(-1,i),(-1,i), bg),
                   ("TEXTCOLOR",(-1,i),(-1,i), WHITE),
                   ("FONTNAME",(-1,i),(-1,i), "Helvetica-Bold")]
        tbl.setStyle(TableStyle(ts))
        story.append(tbl)
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 6. GST VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def section_gst(story: list, data: dict, ST: dict):
    story.append(section_header("6. GST Verification & Compliance", ST))
    story.append(sp(2))

    gst = data.get("gst_data") or {}
    if not gst.get("success"):
        story.append(_para("GST data not available or not verified.", ST["body"]))
        story.append(sp(3))
        return

    status = gst.get("status","Unknown")
    sc = GREEN_OK if status.lower()=="active" else RED_RISK
    status_row = Table([[
        _para("GST STATUS:", ParagraphStyle("gsl", fontName="Helvetica-Bold",
              fontSize=9, textColor=TEXT_GREY, leading=12)),
        _para(f"{status.upper()}  {'✓' if status.lower()=='active' else '✗'}",
              ParagraphStyle("gsv", fontName="Helvetica-Bold", fontSize=12,
                             textColor=sc, leading=14)),
    ]], colWidths=[BODY_W*0.25, BODY_W*0.75])
    story.append(status_row)
    story.append(sp(2))

    filings = gst.get("filings") or []
    filed   = sum(1 for f in filings if (f.get("status","")).lower()=="filed")
    total   = len(filings)
    pct     = int(filed/max(1,total)*100)
    pct_c   = GREEN_OK if pct==100 else AMBER if pct>=70 else RED_RISK

    comp_strip = kpi_strip([
        {"label":"Filing Compliance","value":f"{pct}%","sub":f"{filed}/{total} filed","color": pct_c},
        {"label":"GST Status",       "value":status,    "sub":gst.get("reg_date",""), "color":sc},
        {"label":"Business Type",    "value":gst.get("business_type","—")[:24],"sub":"Entity type","color":NAVY_MID},
        {"label":"State",            "value":gst.get("state","—"),"sub":"Jurisdiction","color":TEAL},
    ], ST)
    story.append(comp_strip)
    story.append(sp(3))

    if filings:
        story.append(sub_header("GST Filing History", ST))
        fl_hdrs  = ["Period", "Return Type", "Status", "Filed Date"]
        fl_rows  = [[f.get("period",""), f.get("type",""), f.get("status",""), f.get("date","")]
                    for f in filings[:12]]
        tbl = fin_table(fl_hdrs, fl_rows, ST, col_widths=[BODY_W*0.25]*4)
        # Colour status column
        ts_extra = []
        for i, f in enumerate(fl_rows, 1):
            c_ok = (f[2] or "").lower()=="filed"
            ts_extra += [
                ("TEXTCOLOR", (2,i),(2,i), GREEN_OK if c_ok else RED_RISK),
                ("FONTNAME",  (2,i),(2,i), "Helvetica-Bold"),
            ]
        tbl.setStyle(TableStyle(ts_extra))
        story.append(tbl)
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 7. PILLAR SCORECARD
# ══════════════════════════════════════════════════════════════════════════════

def section_pillars(story: list, data: dict, ST: dict):
    story.append(section_header("7. Pillar Scorecard", ST))
    story.append(sp(2))

    an      = data.get("analysis", {}) or {}
    pillars = an.get("pillars", {})
    if not pillars:
        story.append(_para("Analysis not run yet.", ST["body"]))
        story.append(sp(3))
        return

    def band_for(sc):
        return ("Excellent" if sc>=85 else "Good" if sc>=70 else
                "Moderate" if sc>=55 else "Caution" if sc>=40 else "High Risk")
    def col_for(sc):
        return (GREEN_OK if sc>=70 else AMBER if sc>=45 else RED_RISK)

    hdrs   = ["Pillar", "Score /100", "Band", "Key Finding"]
    p_rows = []
    for key, p in pillars.items():
        sc      = p.get("score", 0)
        flags   = p.get("flags",[])
        highs   = p.get("highlights",[])
        finding = (flags[0] if flags else (highs[0] if highs else "—"))[:95]
        p_rows.append([p.get("label", key), str(sc), band_for(sc), finding])

    cw  = [BODY_W*0.22, BODY_W*0.10, BODY_W*0.13, BODY_W*0.55]
    tbl = fin_table(hdrs, p_rows, ST, col_widths=cw)
    ts_extra = []
    for i, row in enumerate(p_rows, 1):
        sc = int(row[1])
        ts_extra += [
            ("TEXTCOLOR", (1,i),(1,i), col_for(sc)),
            ("FONTNAME",  (1,i),(1,i), "Helvetica-Bold"),
            ("FONTSIZE",  (1,i),(1,i), 10),
        ]
    tbl.setStyle(TableStyle(ts_extra))
    story.append(tbl)
    story.append(sp(3))

    # ── Highlights / Flags side-by-side ────────────────────────────────────
    highlights = an.get("highlights", [])
    flags      = an.get("flags", [])
    if highlights or flags:
        hs = ParagraphStyle("hf_h",fontName="Helvetica-Bold",fontSize=8,
                            textColor=GREEN_OK,leading=11)
        fs = ParagraphStyle("hf_f",fontName="Helvetica-Bold",fontSize=8,
                            textColor=RED_RISK,leading=11)
        bh = ParagraphStyle("hf_bh",fontName="Helvetica",fontSize=7.5,
                            textColor=BLACK,leading=11,leftIndent=8)

        h_col = [_para("Key Positives", hs)]
        for h in highlights[:6]:
            h_col.append(_para(f"\u2714  {h}", bh))
        f_col = [_para("Watch Points & Flags", fs)]
        for f in flags[:6]:
            f_col.append(_para(f"\u26A0  {f}", bh))

        def _list_table(items, bg):
            rows = [[i] for i in items]
            tl = Table(rows, colWidths=[BODY_W/2 - 0.3*cm])
            tl.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,-1), bg),
                ("TOPPADDING",    (0,0),(-1,-1), 3),
                ("BOTTOMPADDING", (0,0),(-1,-1), 3),
                ("LEFTPADDING",   (0,0),(-1,-1), 8),
                ("RIGHTPADDING",  (0,0),(-1,-1), 8),
                ("GRID",          (0,0),(-1,-1), 0, WHITE),
                ("LINEAFTER",     (0,0),(0,-1),  0.5, GREY_LINE),
            ]))
            return tl

        outer = Table([[_list_table(h_col, GREEN_LITE),
                        _list_table(f_col, RED_LITE)]],
                      colWidths=[BODY_W/2, BODY_W/2])
        outer.setStyle(TableStyle([
            ("TOPPADDING",    (0,0),(-1,-1), 0),
            ("BOTTOMPADDING", (0,0),(-1,-1), 0),
            ("LEFTPADDING",   (0,0),(-1,-1), 0),
            ("RIGHTPADDING",  (0,0),(-1,-1), 0),
            ("GRID",          (0,0),(-1,-1), 0.3, GREY_LINE),
        ]))
        story.append(outer)
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# 8. CREDIT DRIVERS & RISK FACTORS
# ══════════════════════════════════════════════════════════════════════════════

def section_credit_drivers(story: list, data: dict, ST: dict):
    story.append(section_header("8. Credit Drivers & Risk Factors", ST))
    story.append(sp(2))

    rag  = data.get("rag_data") or {}
    hi   = rag.get("key_highlights", []) if rag else []
    aw   = rag.get("awards_recognition", []) if rag else []
    an   = data.get("analysis", {}) or {}
    flags = an.get("flags", [])

    extra_credit = data.get("credit_drivers", [])

    all_positives = extra_credit + hi + aw
    for item in all_positives[:10]:
        if item and item not in ("Not found", "Not Available"):
            story.append(_para(f"\u2022 {item}", ST["bullet"]))
    story.append(sp(3))

    if flags:
        story.append(sub_header("Risk Factors", ST))
        story.append(sp())
        for f in flags[:8]:
            story.append(_para(f"\u2022 {f}", ST["bullet"]))
    story.append(sp(3))


# ══════════════════════════════════════════════════════════════════════════════
# DISCLAIMER
# ══════════════════════════════════════════════════════════════════════════════

def section_disclaimer(story: list, ST: dict):
    story.append(hr(GREY_LINE, 1.0))
    story.append(sp(2))
    story.append(_para(
        "Disclaimer: This report has been prepared by the AI Due Diligence Platform for informational "
        "purposes only. The data sourced from GST Portal, Screener.in, NSE/Yahoo Finance, Indian Kanoon, "
        "and news aggregators is believed to be reliable but the platform does not warrant its completeness "
        "or accuracy. This report does not constitute financial, legal, or investment advice. Decisions "
        "based on this report should be validated by qualified professionals before acting upon them. "
        f"Report generated: {datetime.now().strftime('%d %B %Y, %H:%M IST')}.",
        ST["disc"]))
    story.append(sp(2))
    story.append(_para(
        "CARE Ratings Limited (Formerly known as Credit Analysis & Research Ltd) I CIN: L67190MH1993PLC071691",
        ST["disc"]))


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def generate_due_diligence_report(data: dict, output_path: str = "due_diligence_report.pdf") -> str:
    """
    Build the complete CareEdge-style PDF report.

    data keys (all optional — missing data gracefully degrades):
        company_name      str
        report_type       str   e.g. "Vendor Assessment"
        report_date       str   e.g. "August 2026"
        gstin             str
        sector            str
        overall_rating    str   e.g. "Good / 72"
        summary_text      str   override the AI narrative text
        credit_limit_note str   credit limit/recommendation note paragraph
        credit_drivers    list  extra bullet points for credit drivers section
        gst_data          dict  from verify_gst()
        screener_data     dict  from get_company_financials()
        nse_data          dict  from get_nse_quote()
        legal_data        dict  from get_legal_summary()
        rag_data          dict  from extract_company_info_rag()
        analysis          dict  from analysis_service.run_analysis()
    """
    company  = data.get("company_name", "Company")
    rep_date = data.get("report_date",  datetime.now().strftime("%B %Y"))

    doc = ReportDoc(
        output_path,
        company_name=company,
        report_date=rep_date,
        pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T,  bottomMargin=MARGIN_B,
        title=f"AI Due Diligence — {company}",
        author="AI Due Diligence Platform",
    )

    ST    = doc.ST
    story = []

    cover_page(story, data, ST)
    section_executive_summary(story, data, ST)
    section_company_profile(story, data, ST)
    section_financials(story, data, ST)
    section_market(story, data, ST)
    section_legal(story, data, ST)
    section_gst(story, data, ST)
    section_pillars(story, data, ST)
    section_credit_drivers(story, data, ST)
    section_disclaimer(story, ST)

    doc.build(story)
    return output_path


# ══════════════════════════════════════════════════════════════════════════════
# SAMPLE DATA — mirrors the AMNSIL CareEdge images exactly
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_DATA = {
    "company_name":   "ArcelorMittal Nippon Steel India Private Limited",
    "report_type":    "Vendor Assessment",
    "report_date":    "August 2026",
    "gstin":          "24AAECA9974H1ZU",
    "sector":         "Steel / Metals & Mining",
    "overall_rating": "Moderate / 58",
    "credit_limit_note": (
        "CARE Advisory recommends Vesuvius may continue to supply to AMNSIL and also to its Group "
        "Companies — Grade 2. The credit limit can be extended by up to 10% of the limit, based on the "
        "vintage record. Further, the total exposure of the Company to any single customers should not "
        "exceed 10% of the overall monthly debtors and group exposure shall not exceed 20% of the overall "
        "monthly debtors."
    ),
    "credit_drivers": [
        "<b>Robust Parentage:</b> The Essar Steel purchase has provided two of the world's top steel "
        "producers (ArcelorMittal and Nippon Steel) with direct access to India's steel sector, where "
        "their presence was otherwise restricted. The strategic value of the investment is reinforced "
        "through the presence of senior sponsor executives on the board of AMNSIL, an initial asset "
        "infusion to address working capital and capex requirements, and a back-ended repayment profile "
        "that is supportive of liquidity. In addition to financial support, AMNSIL also enjoys the "
        "sponsors' operational know-how, raw material sourcing and technical expertise. The strategic "
        "alignment reflects the high probability of sustained sponsor support, if needed.",
        "<b>Well-Established Market Presence in India's Steel Sector:</b> AMNSIL is India's "
        "fourth-largest flat steel producer, with a crude steel capacity of 8.8 million tonnes per year "
        "— just behind SAIL, Tata Steel, and JSW Steel.",
    ],
    "gst_data": {
        "success": True, "gstin": "24AAECA9974H1ZU",
        "legal_name": "ARCELORMITTAL NIPPON STEEL INDIA PRIVATE LIMITED",
        "trade_name": "AMNSIL", "reg_date": "01/04/2015",
        "status": "Active", "state": "Gujarat", "state_code": "24",
        "business_type": "Private Limited Company",
        "address": "Hazira Manufacturing Complex, Surat, Gujarat – 394270",
        "pan": "AAECA9974H",
        "directors": [
            {"name": "Dilip Oommen",  "designation": "CEO & Managing Director", "din": "07677580"},
            {"name": "Aditya Mittal", "designation": "Director",                 "din": "01045894"},
            {"name": "Takahiro Mori", "designation": "Director (Nippon Steel)",  "din": "00000001"},
        ],
        "filings": [
            {"period":"Mar 2024","type":"GSTR-3B","status":"Filed","date":"20/04/2024"},
            {"period":"Feb 2024","type":"GSTR-3B","status":"Filed","date":"20/03/2024"},
            {"period":"Jan 2024","type":"GSTR-3B","status":"Filed","date":"20/02/2024"},
            {"period":"Dec 2023","type":"GSTR-3B","status":"Filed","date":"20/01/2024"},
            {"period":"Nov 2023","type":"GSTR-3B","status":"Filed","date":"20/12/2023"},
            {"period":"Oct 2023","type":"GSTR-3B","status":"Filed","date":"20/11/2023"},
        ],
        "_mock": False,
    },
    "screener_data": {
        "success": True,
        "matched_name": "ArcelorMittal Nippon Steel India Pvt Ltd",
        "screener_url": "https://www.screener.in",
        "standalone": {
            "profit_loss": {
                "years": ["Mar 2022(A)", "Mar 2023(A)", "Mar 2024(A)"],
                "rows": [
                    {"particular": "Revenue from operations", "values": {"Mar 2022(A)":"55,668","Mar 2023(A)":"53,399","Mar 2024(A)":"57,434"}},
                    {"particular": "EBIDTA",     "values": {"Mar 2022(A)":"14,581","Mar 2023(A)":"7,469", "Mar 2024(A)":"14,142"}},
                    {"particular": "Other Income","values": {"Mar 2022(A)":"656",   "Mar 2023(A)":"1,035", "Mar 2024(A)":"783"}},
                    {"particular": "Depreciation","values": {"Mar 2022(A)":"2,469", "Mar 2023(A)":"2,463", "Mar 2024(A)":"2,380"}},
                    {"particular": "Finance Costs","values": {"Mar 2022(A)":"2,718","Mar 2023(A)":"3,673", "Mar 2024(A)":"3,087"}},
                    {"particular": "PBT",         "values": {"Mar 2022(A)":"8,459", "Mar 2023(A)":"1,716", "Mar 2024(A)":"9,458"}},
                    {"particular": "PAT",         "values": {"Mar 2022(A)":"7,225", "Mar 2023(A)":"2,187", "Mar 2024(A)":"6,997"}},
                    {"particular": "Share Capital","values": {"Mar 2022(A)":"25,041","Mar 2023(A)":"25,041","Mar 2024(A)":"25,041"}},
                    {"particular": "Reserves & Surplus","values": {"Mar 2022(A)":"16,363","Mar 2023(A)":"14,657","Mar 2024(A)":"17,348"}},
                    {"particular": "Net Worth",   "values": {"Mar 2022(A)":"41,404","Mar 2023(A)":"39,699","Mar 2024(A)":"42,389"}},
                    {"particular": "Long Term Borrowings","values": {"Mar 2022(A)":"31,390","Mar 2023(A)":"29,992","Mar 2024(A)":"38,571"}},
                    {"particular": "Short Term Borrowings","values": {"Mar 2022(A)":"596",  "Mar 2023(A)":"6,170", "Mar 2024(A)":"4,630"}},
                    {"particular": "Trade Payables","values": {"Mar 2022(A)":"4,092","Mar 2023(A)":"6,321","Mar 2024(A)":"6,946"}},
                    {"particular": "Fixed Assets", "values": {"Mar 2022(A)":"31,797","Mar 2023(A)":"34,651","Mar 2024(A)":"34,688"}},
                    {"particular": "CWIP",         "values": {"Mar 2022(A)":"1,599","Mar 2023(A)":"4,171","Mar 2024(A)":"14,139"}},
                    {"particular": "Trade Receivables","values": {"Mar 2022(A)":"1,367","Mar 2023(A)":"1,467","Mar 2024(A)":"826"}},
                    {"particular": "Inventories",  "values": {"Mar 2022(A)":"10,837","Mar 2023(A)":"9,676","Mar 2024(A)":"10,203"}},
                    {"particular": "Cash & Bank Balances","values": {"Mar 2022(A)":"14,402","Mar 2023(A)":"5,149","Mar 2024(A)":"7,412"}},
                ],
            },
            "balance_sheet": {"years": [], "rows": []},
            "cash_flow":     {"years": [], "rows": []},
            "ratios": {
                "years": ["Mar 2022(A)", "Mar 2023(A)", "Mar 2024(A)"],
                "rows": [
                    {"particular":"EBIDTA Margin (%)","values":{"Mar 2022(A)":"26%","Mar 2023(A)":"14%","Mar 2024(A)":"25%"}},
                    {"particular":"PAT Margin (%)",   "values":{"Mar 2022(A)":"13%","Mar 2023(A)":"4%", "Mar 2024(A)":"12%"}},
                    {"particular":"Interest Coverage Ratio","values":{"Mar 2022(A)":"4.70","Mar 2023(A)":"1.64","Mar 2024(A)":"4.06"}},
                    {"particular":"Debt: Equity Ratio",     "values":{"Mar 2022(A)":"0.77","Mar 2023(A)":"0.91","Mar 2024(A)":"1.02"}},
                    {"particular":"Current Ratio",          "values":{"Mar 2022(A)":"4.55","Mar 2023(A)":"1.19","Mar 2024(A)":"1.14"}},
                    {"particular":"Debt: EBITDA",           "values":{"Mar 2022(A)":"2.19","Mar 2023(A)":"4.84","Mar 2024(A)":"3.05"}},
                    {"particular":"Cash as % of long-term debt","values":{"Mar 2022(A)":"45.88%","Mar 2023(A)":"17.17%","Mar 2024(A)":"19.22%"}},
                    {"particular":"Debt- Total Asset Ratio","values":{"Mar 2022(A)":"0.36","Mar 2023(A)":"0.38","Mar 2024(A)":"0.40"}},
                    {"particular":"Total Outside liabilities / Total Net worth","values":{"Mar 2022(A)":"1.14","Mar 2023(A)":"1.42","Mar 2024(A)":"1.56"}},
                    {"particular":"Inventory / Sales (Days)","values":{"Mar 2022(A)":"128","Mar 2023(A)":"109","Mar 2024(A)":"130"}},
                    {"particular":"Debtors / Sales (Days)",  "values":{"Mar 2022(A)":"9",  "Mar 2023(A)":"10", "Mar 2024(A)":"5"}},
                    {"particular":"Payables / Sales (Days)", "values":{"Mar 2022(A)":"48", "Mar 2023(A)":"71", "Mar 2024(A)":"88"}},
                ],
            },
        },
        "consolidated": {
            "profit_loss": {
                "years": ["Mar 2022(A)", "Mar 2023(A)", "Mar 2024(A)"],
                "rows": [
                    {"particular":"Revenue from operations","values":{"Mar 2022(A)":"58,440","Mar 2023(A)":"55,639","Mar 2024(A)":"59,588"}},
                    {"particular":"EBIDTA",                 "values":{"Mar 2022(A)":"14,756","Mar 2023(A)":"7,972", "Mar 2024(A)":"15,218"}},
                    {"particular":"Other Income",           "values":{"Mar 2022(A)":"658",   "Mar 2023(A)":"1,292", "Mar 2024(A)":"695"}},
                    {"particular":"Depreciation",           "values":{"Mar 2022(A)":"2,525", "Mar 2023(A)":"2,572", "Mar 2024(A)":"2,876"}},
                    {"particular":"Finance Costs",          "values":{"Mar 2022(A)":"2,738", "Mar 2023(A)":"3,635", "Mar 2024(A)":"3,054"}},
                    {"particular":"PBT",                    "values":{"Mar 2022(A)":"8,560", "Mar 2023(A)":"2,406", "Mar 2024(A)":"9,982"}},
                    {"particular":"PAT",                    "values":{"Mar 2022(A)":"7,294", "Mar 2023(A)":"2,698", "Mar 2024(A)":"7,324"}},
                    {"particular":"Share Capital",          "values":{"Mar 2022(A)":"25,041","Mar 2023(A)":"25,041","Mar 2024(A)":"25,041"}},
                    {"particular":"Reserves & Surplus",     "values":{"Mar 2022(A)":"16,776","Mar 2023(A)":"15,710","Mar 2024(A)":"18,941"}},
                    {"particular":"Net Worth",              "values":{"Mar 2022(A)":"41,819","Mar 2023(A)":"40,976","Mar 2024(A)":"44,209"}},
                    {"particular":"Long Term Borrowings",   "values":{"Mar 2022(A)":"31,390","Mar 2023(A)":"29,992","Mar 2024(A)":"38,571"}},
                    {"particular":"Short Term Borrowings",  "values":{"Mar 2022(A)":"661",   "Mar 2023(A)":"6,170", "Mar 2024(A)":"4,630"}},
                    {"particular":"Trade Payables",         "values":{"Mar 2022(A)":"4,213", "Mar 2023(A)":"6,318", "Mar 2024(A)":"6,330"}},
                    {"particular":"Fixed Assets",           "values":{"Mar 2022(A)":"32,131","Mar 2023(A)":"56,931","Mar 2024(A)":"57,635"}},
                    {"particular":"CWIP",                   "values":{"Mar 2022(A)":"1,613", "Mar 2023(A)":"4,227", "Mar 2024(A)":"14,196"}},
                    {"particular":"Trade Receivables",      "values":{"Mar 2022(A)":"1,521", "Mar 2023(A)":"1,552", "Mar 2024(A)":"902"}},
                    {"particular":"Inventories",            "values":{"Mar 2022(A)":"11,072","Mar 2023(A)":"10,081","Mar 2024(A)":"10,711"}},
                    {"particular":"Cash & Bank Balances",   "values":{"Mar 2022(A)":"14,540","Mar 2023(A)":"6,166", "Mar 2024(A)":"7,887"}},
                ],
            },
            "balance_sheet": {"years": [], "rows": []},
            "cash_flow":     {"years": [], "rows": []},
            "ratios": {
                "years": ["Mar 2022(A)", "Mar 2023(A)", "Mar 2024(A)"],
                "rows": [
                    {"particular":"EBIDTA Margin (%)","values":{"Mar 2022(A)":"25%","Mar 2023(A)":"14%","Mar 2024(A)":"26%"}},
                    {"particular":"PAT Margin (%)",   "values":{"Mar 2022(A)":"12%","Mar 2023(A)":"5%", "Mar 2024(A)":"12%"}},
                    {"particular":"Interest Coverage Ratio","values":{"Mar 2022(A)":"4.71","Mar 2023(A)":"1.84","Mar 2024(A)":"4.27"}},
                    {"particular":"Debt: Equity Ratio",     "values":{"Mar 2022(A)":"0.77","Mar 2023(A)":"0.88","Mar 2024(A)":"0.98"}},
                    {"particular":"Current Ratio",          "values":{"Mar 2022(A)":"4.50","Mar 2023(A)":"1.31","Mar 2024(A)":"1.29"}},
                    {"particular":"Debt: EBITDA",           "values":{"Mar 2022(A)":"2.17","Mar 2023(A)":"4.54","Mar 2024(A)":"2.84"}},
                    {"particular":"Cash as % of long-term debt","values":{"Mar 2022(A)":"46.32%","Mar 2023(A)":"20.56%","Mar 2024(A)":"20.45%"}},
                    {"particular":"Debt-Total Asset Ratio", "values":{"Mar 2022(A)":"0.36","Mar 2023(A)":"0.36","Mar 2024(A)":"0.39"}},
                    {"particular":"Total Outside liabilities / Total Net worth","values":{"Mar 2022(A)":"1.13","Mar 2023(A)":"2.51","Mar 2024(A)":"2.61"}},
                    {"particular":"Inventory / Sales (Days)","values":{"Mar 2022(A)":"121","Mar 2023(A)":"109","Mar 2024(A)":"136"}},
                    {"particular":"Debtors / Sales (Days)",  "values":{"Mar 2022(A)":"10", "Mar 2023(A)":"10", "Mar 2024(A)":"6"}},
                    {"particular":"Payables / Sales (Days)", "values":{"Mar 2022(A)":"46", "Mar 2023(A)":"68", "Mar 2024(A)":"80"}},
                ],
            },
        },
        "_mock": False,
    },
    "nse_data": {
        "success": True, "_mock": True,
        "symbol": "AMNSIL (Unlisted)", "company_name": "ArcelorMittal Nippon Steel India Pvt Ltd",
        "sector": "Metals & Mining", "industry": "Steel",
        "price": None, "change": None, "change_pct": None,
        "week52_high": None, "week52_low": None, "volume": None,
        "market_cap_cr": None, "pe_ratio": None, "pb_ratio": None,
        "eps": None, "beta": None,
    },
    "legal_data": {
        "success": True,
        "company": "ArcelorMittal Nippon Steel India Private Limited",
        "legal_risk_score": 65,
        "risk_band": "moderate",
        "kpis": {"court_records": 18, "adverse_news_total": 12,
                 "relevant_signals": 8, "critical_flags": 2},
        "articles": [
            {"title":"AMNSIL reports strong Q4 FY24 revenue of ₹57,434 Cr",
             "source":"Business Standard","published_at":"2024-05-20",
             "risk_level":"low","relevance":"relevant","matched_keywords":[]},
            {"title":"ArcelorMittal Nippon faces NCLT insolvency cases from legacy creditors",
             "source":"Economic Times","published_at":"2024-03-10",
             "risk_level":"high","relevance":"relevant",
             "matched_keywords":["court","insolvency","NCLT"]},
            {"title":"AMNSIL plans ₹60,000 Cr capex for capacity expansion to 15 MTPA",
             "source":"Mint","published_at":"2024-02-14",
             "risk_level":"medium","relevance":"relevant",
             "matched_keywords":["regulatory","capex"]},
        ],
        "cases": [
            {"title":"L & T Infrastructure Finance Company Ltd vs ArcelorMittal Nippon Steel India",
             "court":"NCLAT","date":"—",
             "snippet":"Company Appeal (AT)(Ins) - 181/2019"},
            {"title":"MSTC Ltd vs ArcelorMittal Nippon Steel India",
             "court":"NCLAT","date":"28 Aug, 2024",
             "snippet":"Company Appeal (AT)(Ins) - 449/2019"},
            {"title":"STATE BANK OF INDIA vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"24 Apr, 2024",
             "snippet":"1(MB)2024 – Insolvency proceeding"},
            {"title":"STATE BANK OF INDIA vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"20 Mar, 2023",
             "snippet":"66(AHM)2024"},
            {"title":"Standard Chartered Bank vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"18 Jul, 2024",
             "snippet":"C.P. (IB) - 39/2017"},
            {"title":"State Bank Of India vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"23 Apr, 2024",
             "snippet":"C.P. (IB) - 40/2017"},
            {"title":"M/S PALCO RECYCLE INDUSTRIES LTD vs ArcelorMittal Nippon Steel",
             "court":"NCLT","date":"22 Feb, 2018",
             "snippet":"Cont. A. (IBC) - 17/2023"},
            {"title":"M/s. Dakshin Gujarat Vij. Co. Ltd. vs ArcelorMittal Nippon Steel",
             "court":"NCLT","date":"23 Apr, 2024",
             "snippet":"IA 28 OF 2018 IN C.P. (IB) NO. 40/7/NCLT/AHM/2017"},
            {"title":"Hill View Hire Purchase Pvt Ltd vs ArcelorMittal Nippon Steel",
             "court":"NCLT","date":"7 Mar, 2019",
             "snippet":"IA1252019 in IA482 of 2018 in/with CP(IB)39/2017 & CP(IB)40/2017"},
            {"title":"DR Patnaik vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"7 Mar, 2019",
             "snippet":"IA1262019 in IA483 of 2018 in/with CP(IB)39/2017 & CP(IB)40/2017"},
            {"title":"State Tax Officer vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"30 Jan, 2019",
             "snippet":"IA16 of 2019 in/with IA468 of 2018 in CP(IB)39&40 of 2017"},
            {"title":"Dakshin Gujarat Vij Co Ltd vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"30 Jan, 2019",
             "snippet":"IA 28 of 2018 in/with CP(IB)40 of 2017"},
            {"title":"Gail (India) Ltd vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"30 Jan, 2019",
             "snippet":"IA438 of 2018 in/with CP(IB)39&40 of 2017"},
            {"title":"Berger Becker Coatings Pvt Ltd vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"11 Feb, 2019",
             "snippet":"IA442 of 2018 in/with CP(IB) 39 of 2017"},
            {"title":"Gujarat Energy Transmission Corporation Ltd vs ArcelorMittal Nippon Steel",
             "court":"NCLT","date":"30 Jan, 2019",
             "snippet":"IA443 of 2018 in/with CP(IB)40 of 2017"},
            {"title":"State Tax Officer vs ArcelorMittal Nippon Steel India",
             "court":"NCLT","date":"30 Jan, 2019",
             "snippet":"IA468 of 2018 in/with CP(IB)39&40 of 2017"},
            {"title":"L & T Infrastructure Finance Co Ltd vs ArcelorMittal Nippon Steel",
             "court":"NCLT","date":"12 Feb, 2019",
             "snippet":"IA481 of 2018 in/with CP(IB)40 of 2017"},
        ],
        "key_findings": [
            {"type":"warning","icon":"⚠","title":"18 NCLT/NCLAT Cases",
             "body":"Multiple insolvency cases filed by lenders — largely legacy from Essar Steel era. Monitor for resolution."},
            {"type":"critical","icon":"🚨","title":"2 High-Risk News Signals",
             "body":"Recent news flagged regulatory and litigation keywords requiring closer review."},
            {"type":"success","icon":"✓","title":"Core Operations Stable",
             "body":"No operational shutdown orders or new enforcement actions in the review period."},
        ],
    },
    "rag_data": {
        "success": True,
        "company_overview": {
            "name": "ArcelorMittal Nippon Steel India Private Limited",
            "description": (
                "ArcelorMittal Nippon Steel India Private Limited (ANSIL) is engaged in the business "
                "of manufacturing flat steel in India with a presence in multiple segments, including "
                "iron ore mining, steelmaking, and downstream products. Iron ore processing plants are "
                "in the eastern region, while steel manufacturing is in the west. AMNSIL had beneficiation "
                "capacity of 16 MTPA (8 MTPA at Dabuna and 8 MTPA at Kirandul), pellet capacity of 20 "
                "MTPA (12 MTPA at Paradip and 8 MTPA at Vizag), iron-making capacity of 10.29 MTPA "
                "(direct reduced iron of 6.8 MTPA, blast furnace of 1.75 MTPA and Conarc 5.0 MTPA), "
                "steel-making capacity of 9.6 MTPA (electric arc furnace 4.6 MTPA and Conarc 5.0 MTPA) "
                "and rolling capacity of 8.6 MTPA. Its products cater to automotive, appliances, "
                "infrastructure, building, packaging, and construction. It is ISO 9001:2008, "
                "ISO 14001:2004, and ISO/TS 16949:2009 certified."
            ),
            "industry": "Steel / Metals & Mining",
            "founded_year": "1989 (as Essar Steel); 2020 (current form)",
            "headquarters": "Surat, Gujarat",
            "tagline": "Building Tomorrow's World",
            "employee_count": "~25,000+",
        },
        "products_services": {
            "main_offerings": [
                "Hot Rolled Coils / Sheets (HRC / HRS)",
                "Cold Rolled Coils / Sheets (CRC / CRS)",
                "Galvanised Steel Products (GI / GL)",
                "Colour Coated Steel",
                "Tinplate",
                "Electrical Grade Steel",
            ],
            "usp": "Largest flat-steel product range in India with 8.8 MTPA capacity; backed by ArcelorMittal and Nippon Steel.",
            "target_markets": ["Automotive","Appliances","Infrastructure","Construction","Packaging"],
        },
        "leadership_team": [
            {"name":"Dilip Oommen",  "designation":"CEO & Managing Director"},
            {"name":"Aditya Mittal", "designation":"Director"},
            {"name":"Takahiro Mori", "designation":"Director (Nippon Steel Corporation)"},
        ],
        "contact_information": {
            "registered_office": "Hazira Manufacturing Complex, Surat, Gujarat – 394270",
            "phone": ["+91-261-6677000"],
            "email": ["info@amnsil.com"],
            "website": "www.amnsil.com",
        },
        "key_highlights": [
            "India's fourth-largest flat steel producer with 8.8 MTPA crude steel capacity",
            "Backed by two of the world's top steel producers — ArcelorMittal & Nippon Steel",
            "FY24 EBITDA recovery to ₹14,142 Cr (standalone) from ₹7,469 Cr in FY23",
            "ISO 9001:2008, ISO 14001:2004, and ISO/TS 16949:2009 certified",
            "Presence across beneficiation, pelletisation, blast furnace, and EAF steelmaking",
        ],
        "awards_recognition": [
            "CII National Award for Excellence in Energy Management",
            "World Steel Association Safety Award",
        ],
        "clients_partners": {
            "notable_clients": ["Maruti Suzuki","Tata Motors","Mahindra","Bosch India","Whirlpool"],
            "partners": ["ArcelorMittal Global","Nippon Steel Corporation"],
        },
        "chunks_in_db": 342,
    },
    "analysis": {
        "overall": {"score": 58, "band": "Moderate", "color": "amber"},
        "segments": {
            "credit":     {"score":55,"band":"Moderate","color":"amber",
                           "verdict":"Proceed with caution — request advance or smaller credit limit."},
            "vendor":     {"score":52,"band":"Moderate","color":"amber",
                           "verdict":"Onboard with enhanced monitoring & shorter contract terms."},
            "investment": {"score":60,"band":"Moderate","color":"amber",
                           "verdict":"Moderate opportunity — watch growth consistency before committing."},
        },
        "pillars": {
            "gst":       {"score":82,"max":100,"label":"GST & Identity",
                          "flags":[],"highlights":["GST Active","100% filing compliance"]},
            "financial": {"score":65,"max":100,"label":"Financial Health",
                          "flags":["D/E risen to 1.02x — elevated leverage","Current ratio compressed to 1.14x"],
                          "highlights":["Revenue ₹57,434 Cr","EBITDA margin 25%","Strong PAT ₹6,997 Cr"]},
            "market":    {"score":50,"max":100,"label":"NSE / Market Signals",
                          "flags":["Company is unlisted — limited market price signal"],
                          "highlights":["Debt-Total Asset ratio stable at 0.40"]},
            "legal":     {"score":35,"max":100,"label":"Legal & News Risk",
                          "flags":["18 NCLT/NCLAT insolvency cases (legacy Essar Steel)",
                                   "2 high-risk news articles"],"highlights":[]},
            "web":       {"score":70,"max":100,"label":"Web Presence / RAG",
                          "flags":[],"highlights":["Clear product range identified",
                                                    "3 senior leadership members identified",
                                                    "Complete contact information available"]},
        },
        "ai_narrative": (
            "ArcelorMittal Nippon Steel India Private Limited (AMNSIL) presents a mixed but broadly "
            "stable risk profile. With FY24 standalone revenue of ₹57,434 Cr and EBITDA recovering "
            "strongly to ₹14,142 Cr from a depressed ₹7,469 Cr in FY23, the company's operational "
            "fundamentals are improving. However, the balance sheet reflects the legacy of the Essar "
            "Steel acquisition — long-term borrowings rose to ₹38,571 Cr in FY24 and CWIP surged "
            "to ₹14,139 Cr, lifting the D/E ratio to 1.02x.\n\n"
            "On the positive side, AMNSIL benefits from robust parentage with ArcelorMittal and "
            "Nippon Steel providing operational know-how, raw material sourcing, and balance-sheet "
            "support. Its position as India's fourth-largest flat steel producer with a diversified "
            "product range across automotive, appliances, and construction supports revenue stability. "
            "GST compliance is strong with 100% filing compliance and an active registration.\n\n"
            "Decision-makers should closely monitor the resolution of 18 pending NCLT/NCLAT insolvency "
            "cases (largely inherited from the Essar Steel era), the trajectory of debt as large capex "
            "commitments (CWIP ₹14,139 Cr) complete, and the sustainability of the EBITDA margin "
            "recovery before extending significant credit or vendor commitments."
        ),
        "highlights": [
            "GST registration Active with 100% filing compliance across all reviewed periods",
            "FY24 EBITDA recovered strongly to ₹14,142 Cr standalone (25% margin)",
            "Robust sponsorship — ArcelorMittal + Nippon Steel provide financial & operational backstop",
            "India's 4th-largest flat steel producer with 8.8 MTPA capacity",
            "Strong product diversification across 6+ steel grades serving multiple end-markets",
        ],
        "flags": [
            "18 NCLT/NCLAT insolvency cases — legacy from Essar Steel acquisition (2020)",
            "D/E ratio risen to 1.02x in FY24 driven by large capex commitments",
            "CWIP of ₹14,139 Cr signals significant ongoing capex — execution risk",
            "Current ratio compressed to 1.14x — limited short-term liquidity buffer",
            "FY23 PAT significantly depressed at ₹2,187 Cr vs ₹7,225 Cr in FY22",
        ],
    },
}


if __name__ == "__main__":
    out = generate_due_diligence_report(
        SAMPLE_DATA,
        "/mnt/user-data/outputs/AMNSIL_Due_Diligence_Report.pdf"
    )
    print(f"Report saved: {out}")
