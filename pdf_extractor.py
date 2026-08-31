"""Extracts text (and, where genuinely present, structured tables) from
uploaded/downloaded PDF financial documents."""

from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber

# Pages matching any of these are kept (in addition to the first N pages),
# since this is where multi-year financial tables / ratios / operational
# metrics typically live in an Indian annual report or balance sheet PDF.
KEYWORDS = (
    "balance sheet", "profit and loss", "statement of profit",
    "statement of assets and liabilities", "total assets", "total liabilities",
    "net worth", "revenue from operations", "cash flow", "shareholders' funds",
    "key financial", "financial highlights", "key ratios", "operational metrics",
    "ebitda margin", "return on equity", "return on assets", "debt equity",
    "debt-equity", "current ratio", "interest coverage", "5 year", "10 year",
    "five year", "ten year", "inventory", "debtors", "creditors", "payables",
    "days sales outstanding", "standalone", "consolidated", "earning per share",
    "earnings per share",
)


def _format_table_markdown(table):
    """Convert a pdfplumber-extracted table (list of rows, each a list of
    cell strings/None) into a clean Markdown table string. Returns None if
    the table is too small, or looks like a mis-detected garbage table
    (e.g. words split across many spurious columns) rather than a real one.
    """
    if not table or len(table) < 2:
        return None

    clean_rows = []
    for row in table:
        clean_row = [(c or "").replace("\n", " ").strip() for c in row]
        if any(clean_row):
            clean_rows.append(clean_row)
    if len(clean_rows) < 2:
        return None

    n_cols = max(len(r) for r in clean_rows)
    if n_cols < 2:
        return None

    # Sanity/quality guard: real financial tables have a handful of columns
    # (particulars + a few years/periods) and reasonably substantial cells.
    # A mis-detected table (whitespace-based column guessing gone wrong)
    # tends to have MANY columns, mostly made of 1-3 character fragments --
    # including that would feed the LLM garbage instead of helping it, so
    # skip it and just let the plain text carry that page instead.
    if n_cols > 8:
        return None
    total_cells = sum(len(r) for r in clean_rows)
    short_cells = sum(1 for r in clean_rows for c in r if 0 < len(c) <= 2)
    if total_cells and (short_cells / total_cells) > 0.5:
        return None

    clean_rows = [r + [""] * (n_cols - len(r)) for r in clean_rows]
    header = clean_rows[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * n_cols) + " |",
    ]
    for r in clean_rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _extract_ruling_line_tables(pdf_path: str, page_index: int):
    """Looks for genuine bordered/ruled tables on one page via pdfplumber's
    default (line-based) strategy only. Many Indian financial-statement
    PDFs use plain whitespace-aligned tables with NO visible ruling lines
    at all -- for those, this correctly finds nothing, and the page's
    plain text (already well-ordered by PyMuPDF) carries the data instead.
    We deliberately do NOT use pdfplumber's "text"-based table-guessing
    strategy here: on documents with unusual/corrupted font glyph mappings
    it tends to mis-segment single words into many spurious columns,
    producing a garbled table that actively confuses the LLM rather than
    helping it.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_index]
            tables = page.extract_tables()
    except Exception:
        return []
    return [t for t in tables if t and len(t) >= 2 and len(t[0]) >= 2]


def extract_pdf_text(file_path: str, max_pages: int = 60) -> str:
    """
    Extract text (and, where genuinely present, ruled tables) from a PDF,
    page by page, capped at max_pages to keep token usage sane for the LLM
    call. Annual reports can be 200+ pages; we mostly need the financial
    statements + MD&A sections, so instead of reading linearly we sample:
    first 15 pages (overview/MD&A) + pages containing balance-sheet/P&L/
    ratio keywords.

    Text extraction uses PyMuPDF (fitz) rather than pdfplumber's own
    extract_text(). On several real-world Indian financial-statement PDFs
    (e.g. NSE/BSE quarterly result filings) pdfplumber's text extraction
    badly mis-orders and corrupts characters when the PDF's embedded font
    has an unusual glyph/CID mapping -- e.g. "31 March 2026" coming out as
    "34 March 2026", numbers running together, words splitting mid-token.
    PyMuPDF handles the same files far more reliably and naturally emits
    each label/value on its own line in table order, which is already
    fairly easy for an LLM to parse correctly on its own even without an
    explicit table structure.

    pdfplumber is still used, but ONLY to detect genuine ruled/bordered
    tables (its default line-based strategy) as a bonus -- those get
    reformatted as clean Markdown and appended after the page's text. Pages
    with no ruled tables (very common for these whitespace-aligned
    financial statements) simply rely on the clean PyMuPDF text alone.
    """
    text_chunks = []
    keyword_hits = []

    doc = fitz.open(file_path)
    try:
        total_pages = len(doc)
        scan_limit = min(total_pages, max_pages * 3)  # scan more than we keep

        for i in range(scan_limit):
            page_text = doc[i].get_text() or ""
            if not page_text.strip():
                continue

            tables = _extract_ruling_line_tables(file_path, i)
            table_md_blocks = []
            for t in tables:
                md = _format_table_markdown(t)
                if md:
                    table_md_blocks.append(md)

            page_content = page_text
            if table_md_blocks:
                page_content += "\n\n[Extracted table(s) on this page]\n" + "\n\n".join(table_md_blocks)

            lowered = page_text.lower()
            if i < 15:
                text_chunks.append((i, page_content))
            elif table_md_blocks or any(k in lowered for k in KEYWORDS):
                keyword_hits.append((i, page_content))
    finally:
        doc.close()

    combined = text_chunks + keyword_hits
    combined = sorted(combined, key=lambda x: x[0])[:max_pages]

    return "\n\n".join(f"[Page {i+1}]\n{t}" for i, t in combined)


def save_upload(file_bytes: bytes, filename: str, dest_dir: str = "uploads") -> str:
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(dest_dir) / filename
    out_path.write_bytes(file_bytes)
    return str(out_path)