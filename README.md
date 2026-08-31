# AI Due Diligence Assistant

An AI-powered business due-diligence platform built for Indian companies. It aggregates GST compliance data, financial statements, live NSE market data, legal & news risk signals, and a company's own website into a single automated risk assessment — producing a 5-pillar scorecard, 3 segment-specific recommendations (Credit / Vendor / Investment), an AI-written narrative, and a downloadable PDF report.

Built to answer one practical question: **"Should we do business with this company?"** — whether that means extending credit, onboarding them as a vendor, or considering them as an investment.

---

## What it actually does

| Module | What it pulls | Source |
|---|---|---|
| **GST Verification** | Legal name, registration status, directors, HSN codes, filing history | GST Portal API |
| **Financials** | Standalone + consolidated P&L, balance sheet, cash flow, ratios | Screener.in (scraped) |
| **Market Data** | Live price, 52-week range, P/E, P/B, EPS, beta, volume, price history | NSE via Yahoo Finance |
| **Legal & News Risk** | Adverse news coverage, court cases | Google News RSS / GNews API, Indian Kanoon |
| **Web Presence** | Company overview, leadership, products, contact info, awards | Company's own website (Playwright crawl + RAG) |
| **Annual Report Analysis** | Extracted financial highlights, credit assessment | Uploaded/downloaded PDF + Groq LLM |

All five feed into a single **due-diligence analysis engine** that produces pillar scores, segment scores, and a plain-English recommendation.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Flask app (app.py)                   │
│         HTML/JS frontend served directly, no build step      │
└───────┬─────────┬──────────┬──────────┬───────────┬─────────┘
        │         │          │          │           │
   ┌────▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼─────┐ ┌───▼────────┐
   │  GST   │ │Screener│ │  NSE   │ │  Legal  │ │  Website   │
   │verify()│ │scraper │ │(yfinance)│ │service │ │ RAG crawl  │
   └────────┘ └────────┘ └────────┘ └─────────┘ └─────┬──────┘
                                                        │
                                          Playwright → BeautifulSoup
                                          section chunker → sentence-
                                          transformers → ChromaDB
                                                        │
                                                  Groq LLM synthesis
        │         │          │          │           │
        └────┬────┴────┬─────┴────┬─────┴─────┬─────┘
             │          │          │           │
        ┌────▼──────────▼──────────▼───────────▼────┐
        │      analysis_service.run_analysis()        │
        │   5-pillar scoring → 3-segment weighting     │
        │        → overall score → AI narrative        │
        └───────────────────┬──────────────────────────┘
                             │
                   ┌─────────▼──────────┐
                   │  report_generator   │
                   │  (ReportLab PDF)     │
                   └─────────────────────┘
```

---

## The scoring engine

This is the core of the platform (`services/analysis_service.py`).

### 5 Pillars (each scored 0–100)

| Pillar | What's evaluated |
|---|---|
| **GST & Identity** | Active status, filing compliance %, business type, registration age |
| **Financial Health** | Revenue growth, net profit margin, debt-to-equity, ROE, operating cash flow |
| **NSE / Market Signals** | 52-week price position, P/E ratio, day change, market cap, beta |
| **Legal & News Risk** | Court case count (Indian Kanoon), high/medium-risk news article density |
| **Web Presence / RAG** | Description quality, leadership disclosure, contact completeness, clients/awards |

Each pillar degrades gracefully to a neutral/partial score when data is missing (e.g. an unlisted company gets 50/100 on Market Signals instead of failing outright), so a partial dataset still produces a usable analysis.

### 3 Segments (weighted combinations of the 5 pillars)

| Segment | Question it answers | What it weights heaviest |
|---|---|---|
| **B2B Credit** | Should we extend Net-30 / credit terms? | Financial Health (38%), Legal Risk (22%) |
| **Vendor Trust** | Should we onboard them as a supplier? | Legal Risk (30%), Financial Health (28%) |
| **Investment** | Is this worth investing in? | Financial Health (42%), Market Signals (28%) |

Analysts can override the default per-segment weights with a single **custom weight profile** (applied uniformly across all 3 segments) via the Analysis tab's slider panel.

### Output
- Overall score (0–100) with a 5-band risk rating: Excellent → Good → Moderate → Caution → High Risk
- Per-segment score, band, and a plain-language verdict/recommendation
- Flagged risks and positive highlights, aggregated across pillars
- Optional **Groq-generated 3-paragraph narrative** summarizing the findings
- Data coverage indicator (which pillars had real vs. estimated data)

---

## Website RAG Pipeline

A separate deep-crawl pipeline builds a structured company profile straight from the target's own website:

1. **Crawl** — Playwright does a BFS crawl (up to 25 pages) of the domain
2. **Chunk** — HTML is split by heading structure (`h1`–`h4`) into section-aware text chunks
3. **Embed** — Chunks are embedded with `sentence-transformers` (`all-MiniLM-L6-v2`)
4. **Store** — Vectors persist in a local ChromaDB collection, keyed by domain (cached — re-indexing is optional)
5. **Extract** — Targeted retrieval queries (overview, leadership, products, contact, highlights) build context for a Groq LLM call that returns a structured JSON company profile
6. **Q&A** — A retrieval-augmented chat interface lets you ask free-form questions about the indexed company, answered strictly from retrieved website content

There's also a separate crawler that specifically hunts for annual report / balance sheet PDF links on a company's investor-relations pages.

---

## Tech Stack

- **Backend:** Flask, Flask-CORS
- **Scraping / crawling:** Playwright, BeautifulSoup4, requests
- **AI/ML:** Groq (LLM synthesis + narrative), sentence-transformers, ChromaDB
- **Financial data:** Screener.in (scraped), yfinance (NSE quotes)
- **Legal/news:** Google News RSS, GNews API (optional), Indian Kanoon (scraped)
- **PDF:** PyMuPDF (fitz) + pdfplumber for extraction, ReportLab for report generation
- **Frontend:** Server-rendered HTML/CSS/JS (Bootstrap 5), no build step required

---

## Getting Started

### Prerequisites
- Python 3.10+
- pip

### Installation
```bash
git clone https://github.com/Kshitij250/company_intel.git
cd company_intel
pip install -r requirements.txt
playwright install chromium
```

### Environment Variables
Copy `.env.example` → `.env` and fill in your own values. **Never commit `.env`.**

```
GROQ_API_KEY=your_groq_api_key
GST_API_KEY=your_gst_api_key
GST_API_BASE_URL=https://sheet.gstincheck.co.in/check
GNEWS_API_KEY=your_gnews_api_key      # optional — supplements Google News RSS
FLASK_SECRET_KEY=your_flask_secret
PORT=5000
```

### Run
```bash
python app.py
```
Open [http://localhost:5000](http://localhost:5000).

---

## Typical workflow

1. **Home** — Verify GST, and/or point the crawler at the company's website (also accepts a company name + NSE symbol, which auto-populates the Financials/Legal tabs)
2. **Financials** — Pull Screener.in fundamentals + live NSE data; optionally upload/auto-find an annual report PDF for AI analysis
3. **Legal** — Pull adverse news + Indian Kanoon court records, filterable by risk level / court type
4. **Analysis** — Run the 5-pillar/3-segment scoring engine (standard or custom weights), review the AI narrative, record an analyst decision, and export a PDF report

---

## Project Structure

```
company_intel/
├── app.py                       # Flask app: routes, RAG pipeline, GST/PDF/crawler logic, HTML frontend
├── requirements.txt
├── .env.example
├── services/
│   ├── analysis_service.py      # 5-pillar / 3-segment scoring engine + AI narrative
│   ├── screener_service.py      # Screener.in search + financial statement scraping
│   ├── nse_service.py           # Yahoo Finance / NSE live quote + price history
│   ├── legal_service.py         # News (RSS/GNews) + Indian Kanoon court record scraping
│   ├── pdf_extractor.py         # PDF text/table extraction (PyMuPDF + pdfplumber)
│   ├── report_generator.py      # ReportLab-based due-diligence PDF report builder
│   └── __init__.py
└── frontend/
    └── pages/                   # Modular page renderers (GST, Financials, Legal/News, Stock, Risk, Overview)
```

---
