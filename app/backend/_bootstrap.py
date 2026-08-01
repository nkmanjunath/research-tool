"""
Backend bootstrap module.
Ensures repository root and backend directory are registered in sys.path.
Exposes BACKEND_DIR and REPO_ROOT paths.
"""
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
