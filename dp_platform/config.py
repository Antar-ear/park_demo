"""Deployment settings for the drawing-parser platform. Edit here, or override by env var."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Branding (shown in the page header and login screen) -----------------------
APP_NAME = os.environ.get("DP_APP_NAME", "Drawing Parser")
APP_TAGLINE = os.environ.get("DP_TAGLINE", "Title-block extraction & classification")
BRAND_COLOR = os.environ.get("DP_BRAND_COLOR", "#1f3864")

# --- Where things live -----------------------------------------------------------
DATA_DIR = Path(os.environ.get("DP_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "app.db"
SECRET_FILE = DATA_DIR / "secret.key"       # generated on first run, never committed

# --- Demo login (created on first run if it does not exist) ----------------------
DEMO_USER = os.environ.get("DP_USER", "demo")
DEMO_PASSWORD = os.environ.get("DP_PASSWORD", "demo123")

# --- Limits ------------------------------------------------------------------------
MAX_UPLOAD_MB = int(os.environ.get("DP_MAX_UPLOAD_MB", "50"))    # per file
MAX_FILES_PER_BATCH = int(os.environ.get("DP_MAX_FILES", "200"))
SESSION_HOURS = 12

# --- Server ------------------------------------------------------------------------
HOST = os.environ.get("DP_HOST", "127.0.0.1")   # localhost only; 0.0.0.0 exposes to the LAN
PORT = int(os.environ.get("DP_PORT", "8000"))
