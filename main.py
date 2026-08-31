"""
backend/main.py
FastAPI application — all 5 feature routers mounted here.
Run with: uvicorn backend.main:app --reload --port 8000
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from backend.routers import gst, financials, stock, legal_news, risk


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Startup] Company Intel API ready.")
    yield
    print("[Shutdown] Cleaning up.")


app = FastAPI(
    title="Company Intelligence API",
    description="Due diligence & credit risk platform for Indian companies",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(gst.router,         prefix="/api/gst",         tags=["GST Verification"])
app.include_router(financials.router,  prefix="/api/financials",  tags=["Financials"])
app.include_router(stock.router,       prefix="/api/stock",       tags=["Stock Market"])
app.include_router(legal_news.router,  prefix="/api/legal-news",  tags=["Legal & News"])
app.include_router(risk.router,        prefix="/api/risk",        tags=["Risk Score"])


@app.get("/")
async def root():
    return {
        "service": "Company Intelligence API",
        "version": "1.0.0",
        "docs":    "/docs",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
