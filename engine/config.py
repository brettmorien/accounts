"""Shared paths and constants for the accounts trend tracker.

Everything is resolved relative to the project root (the parent of the `engine/`
package) so the scripts work no matter the current working directory -- important
because the unattended launchd job runs with an arbitrary cwd.
"""
import os
from pathlib import Path

# Project root = parent of the engine/ package directory.
ROOT = Path(__file__).resolve().parent.parent

# Data / import / output locations honor env overrides (used by tests to run
# against a scratch directory without touching the real store). Assets stay in
# the real project since they are the vendored chart library.
DATA_DIR = Path(os.environ.get("ACCOUNTS_DATA_DIR", ROOT / "data"))
BALANCES_CSV = DATA_DIR / "balances.csv"
ACCOUNTS_CSV = DATA_DIR / "accounts.csv"
SYNC_STATE_JSON = DATA_DIR / "sync_state.json"
SYNC_LOG = DATA_DIR / "sync.log"

# Historical export drop-folder and optional name->id mapping.
IMPORT_DIR = Path(os.environ.get("ACCOUNTS_IMPORT_DIR", ROOT / "import"))
ACCOUNT_MAP_JSON = IMPORT_DIR / "account_map.json"

# Vendored chart assets and generated dashboard.
ASSETS_DIR = ROOT / "assets"
UPLOT_JS = ASSETS_DIR / "uPlot.iife.min.js"
UPLOT_CSS = ASSETS_DIR / "uPlot.min.css"
DASHBOARD_HTML = Path(os.environ.get("ACCOUNTS_DASHBOARD", ROOT / "dashboard.html"))

# launchd agent.
LAUNCHD_LABEL = "com.brett.accounts.sync"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

# macOS Keychain coordinates for the single read-only secret.
KEYCHAIN_SERVICE = "financial-accounts"
KEYCHAIN_ACCOUNT = "simplefin-access-url"

# Behaviour.
DEFAULT_PROVIDER = "simplefin"
BACKFILL_YEARS = 7  # first-run window when there is no history yet

# CSV headers (kept explicit so files are self-describing and hand-editable).
BALANCE_FIELDS = ["as_of", "account_id", "balance", "source"]
ACCOUNT_FIELDS = ["account_id", "name", "org", "currency", "tax_type", "source", "first_seen", "active"]
