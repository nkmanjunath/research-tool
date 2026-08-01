"""
Sandbox-to-Vault app — FastAPI backend.
localhost only. Phase 1 = Tab 1, Tab 2, Tab 3, & Tab 4 wired live.
"""
from pathlib import Path
import sys

# Ensure repo root and backend root are on sys.path
backend_dir = Path(__file__).resolve().parent
repo_root = backend_dir.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from routers import ingestion, planning, execution, reporting

tags_metadata = [
    {
        "name": "Ingestion (Tab 1)",
        "description": "Blinded dataset upload, schema mapping, sentinel configuration, and Stage 1 (H0) protocol vaulting.",
    },
    {
        "name": "Planning (Tab 2)",
        "description": "Socratic Wizard for pre-registering exposure, confounders, missing-data strategy, live EPV calculations, and Stage 2 (H1) protocol lock.",
    },
    {
        "name": "Execution (Tab 3)",
        "description": "Unattended statistical execution (Logistic / Cox PH), 4 diagnostic gates (Separation, VIF, Proportional Hazards, Linearity), and Autopsy Canvas remediation routing.",
    },
    {
        "name": "Reporting (Tab 4)",
        "description": "Journal-ready manuscript assets (Table 1 Baseline Balance with SMD, Table 2 Primary Effect Estimates, Interactive SVG Forest Plots, STROBE Checklists, Cryptographic Audit Binder).",
    },
]

app = FastAPI(
    title="Clinical Research Tool — Sandbox-to-Vault Engine",
    description="Publication-grade, audit-sealed epidemiological and clinical trial analytical framework.",
    version="1.2.0-strobe-default",
    openapi_tags=tags_metadata,
)

# CORS middleware for local SPA
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingestion.router, prefix="/api/ingest", tags=["Ingestion (Tab 1)"])
app.include_router(planning.router, prefix="/api/plan", tags=["Planning (Tab 2)"])
app.include_router(execution.router, prefix="/api/execute", tags=["Execution (Tab 3)"])
app.include_router(reporting.router, prefix="/api/report", tags=["Reporting (Tab 4)"])

# Serve exports directory for HTML/SVG asset viewing
exports_dir = backend_dir / "exports"
exports_dir.mkdir(exist_ok=True)
app.mount("/exports", StaticFiles(directory=str(exports_dir)), name="exports")

# Serve the vanilla JS SPA
frontend_dir = backend_dir.parent / "frontend"
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
