"""
Sandbox-to-Vault app — FastAPI backend.
localhost only. Phase 1 = Tab 1, Tab 2, Tab 3, & Tab 4 wired live.
"""
from pathlib import Path
import sys

# Ensure backend root is on sys.path
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from routers import ingestion, planning, execution, reporting

app = FastAPI(title="research-tool", version="0.1.0-app-phase1")

# localhost SPA talking to localhost API — CORS wide open is fine, never exposed beyond loopback.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingestion.router, prefix="/api/ingest", tags=["tab1-ingestion"])
app.include_router(planning.router, prefix="/api/plan", tags=["tab2-planning"])
app.include_router(execution.router, prefix="/api/execute", tags=["tab3-execution"])
app.include_router(reporting.router, prefix="/api/report", tags=["tab4-reporting"])

# Serve exports directory for HTML/SVG asset viewing
exports_dir = backend_dir / "exports"
exports_dir.mkdir(exist_ok=True)
app.mount("/exports", StaticFiles(directory=str(exports_dir)), name="exports")

# Serve the vanilla JS SPA
frontend_dir = backend_dir.parent / "frontend"
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
