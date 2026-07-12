"""Ingest historical balance exports dropped into the import/ folder.

Real brokerage exports are messy: title/preamble rows before the header,
multi-section layouts, a UTF-8 BOM, money in columns like "Ending balance" or
"Account: all", and dates like "Jul 2025(As of Jul-16-2025)". So instead of
assuming a fixed shape we *detect* the best header row: for each candidate row we
try to parse the table beneath it and keep whichever interpretation yields the
most valid (date, balance) rows. That naturally skips preambles and, in a
multi-section file, locks onto the value-history section.

Two shapes are supported:
  * Per-account file  — has an account/name column; each row is its own account
    (mapped through the optional import/account_map.json, id "import:<slug>").
  * Whole-institution file — no account column; the file is one historical series
    for an institution (id "hist:<institution>"), inferred from the filename.

Everything flows through the idempotent store, so re-dropping files is harmless.
"""
import csv
import io
import json
import re
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import config

_DATE_KEYS = ["as_of", "date", "as of date", "as of", "period", "month", "date value"]
_ACCOUNT_KEYS = ["account_id", "account", "account name", "name", "fund"]
# Balance column preference, most-specific first. Exact (lowercased) header match
# so "Ending balance" wins over "Beginning balance" and "Market change".
_BAL_PRIORITY = [
    "ending balance", "account value", "account: all", "market value",
    "current value", "value", "total assets", "balance", "amount", "total",
]

_DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y",
    "%b %Y", "%B %Y", "%b-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m", "%m/%Y",
]

_INSTITUTIONS = [
    ("etrade", "E*TRADE"), ("e-trade", "E*TRADE"), ("e*trade", "E*TRADE"),
    ("fidelity", "Fidelity"), ("vanguard", "Vanguard"),
    ("schwab", "Charles Schwab"), ("edward", "Edward Jones"),
    ("merrill", "Merrill"), ("robinhood", "Robinhood"), ("morgan", "Morgan Stanley"),
]


# ---------------------------------------------------------------------------
# Parsers for messy cells
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = re.sub(r"\(.*?\)", "", str(raw)).strip().strip('"')
    s = re.sub(r"\s+", " ", s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower() in ("", "-", "--", "n/a", "na"):
        return None
    neg = ("(" in s and ")" in s) or s.lstrip().startswith("-")
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -val if neg else val


# ---------------------------------------------------------------------------
# Header / column detection
# ---------------------------------------------------------------------------

def _find_col(header: List[str], keys: List[str]) -> Optional[int]:
    low = [c.strip().lower() for c in header]
    for key in keys:
        for i, cell in enumerate(low):
            if cell == key:
                return i
    for key in keys:  # looser contains-match fallback
        for i, cell in enumerate(low):
            if key in cell:
                return i
    return None


def _find_balance_col(header: List[str]) -> Optional[int]:
    low = [c.strip().lower() for c in header]
    for key in _BAL_PRIORITY:
        for i, cell in enumerate(low):
            if cell == key:
                return i
    return None


def _detect_date_col(body: List[List[str]], exclude: int) -> Optional[int]:
    ncols = max((len(r) for r in body[:40]), default=0)
    best_idx, best_hits = None, 0
    for i in range(ncols):
        if i == exclude:
            continue
        hits = sum(1 for r in body[:40] if i < len(r) and _parse_date(r[i]))
        if hits > best_hits:
            best_idx, best_hits = i, hits
    return best_idx if best_hits >= 2 else None


def _detect_table(rows: List[List[str]]):
    """Return (header, date_idx, bal_idx, acct_idx, data_rows) for the best table."""
    best = None
    for h in range(len(rows)):
        header = [c.strip() for c in rows[h]]
        bal_idx = _find_balance_col(header)
        if bal_idx is None:
            continue
        # Body ends at the first fully-blank line (section boundary).
        body: List[List[str]] = []
        for r in rows[h + 1:]:
            if not any(c.strip() for c in r):
                break
            body.append(r)
        if not body:
            continue
        acct_idx = _find_col(header, _ACCOUNT_KEYS)
        if acct_idx == bal_idx:
            acct_idx = None
        date_idx = _find_col(header, _DATE_KEYS)
        if date_idx is None or date_idx == bal_idx:
            date_idx = _detect_date_col(body, exclude=bal_idx)
        if date_idx is None:
            continue
        parsed = [(r, _parse_date(r[date_idx]) if date_idx < len(r) else None,
                   _parse_amount(r[bal_idx]) if bal_idx < len(r) else None)
                  for r in body]
        valid = [p for p in parsed if p[1] and p[2] is not None]
        if valid and (best is None or len(valid) > best[0]):
            best = (len(valid), header, date_idx, bal_idx, acct_idx, valid)
    return best


# ---------------------------------------------------------------------------
# File -> rows
# ---------------------------------------------------------------------------

def _sniff_delimiter(lines: List[str]) -> str:
    """Pick the delimiter (tab/comma/semicolon/pipe) that parses the sample most
    consistently. Vanguard exports as tab-separated with commas inside values, so
    naive comma parsing shreds them; this recovers the right delimiter per file.
    """
    rows = [ln for ln in lines if ln.strip()]
    best, best_key = ",", (0.0, 0)
    for delim in ("\t", ",", ";", "|"):
        counts = [len(next(csv.reader([ln], delimiter=delim))) for ln in rows]
        if not counts:
            continue
        modal, modal_n = Counter(counts).most_common(1)[0]
        if modal <= 1:
            continue
        # Consistency first (fraction of rows at the modal width), then width.
        key = (round(modal_n / len(counts), 2), modal)
        if key > best_key:
            best_key, best = key, delim
    return best


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-") or "unknown"


def _institution_from_name(stem: str) -> Optional[str]:
    low = stem.lower()
    for key, label in _INSTITUTIONS:
        if key in low:
            return label
    return None


def _load_account_map() -> Dict[str, str]:
    if not config.ACCOUNT_MAP_JSON.exists():
        return {}
    try:
        with config.ACCOUNT_MAP_JSON.open() as fh:
            return {str(k).strip().lower(): str(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        return {}


def ingest_files() -> Tuple[List[dict], List[dict], List[str]]:
    """Parse every import/*.csv into balance rows. Returns (balances, accounts, warnings)."""
    account_map = _load_account_map()
    balance_rows: List[dict] = []
    accounts: Dict[str, dict] = {}
    warnings: List[str] = []

    if not config.IMPORT_DIR.exists():
        return [], [], []

    for path in sorted(config.IMPORT_DIR.glob("*.csv")):
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                text = fh.read()
            delim = _sniff_delimiter(text.splitlines()[:40])
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        except OSError as exc:
            warnings.append(f"{path.name}: {exc}")
            continue

        table = _detect_table(rows)
        if not table:
            warnings.append(f"{path.name}: no date+balance table found, skipped")
            continue
        _, header, date_idx, bal_idx, acct_idx, valid = table

        # Whole-institution file: one series id'd from the filename.
        file_inst = _institution_from_name(path.stem)
        file_label = file_inst or path.stem
        file_id = account_map.get(file_label.lower()) or f"hist:{_slug(file_label)}"

        n = 0
        for raw_row, as_of, amount in valid:
            if acct_idx is not None and acct_idx < len(raw_row) and raw_row[acct_idx].strip():
                label = raw_row[acct_idx].strip()
                account_id = account_map.get(label.lower()) or f"import:{_slug(label)}"
                org = _institution_from_name(label) or ""
            else:
                label, account_id, org = f"{file_label} (history)", file_id, file_inst or ""
            balance_rows.append({"as_of": as_of, "account_id": account_id,
                                 "balance": amount, "source": "import"})
            acct = accounts.setdefault(account_id, {
                "account_id": account_id, "name": label, "org": org,
                "currency": "USD", "source": "import", "first_seen": as_of, "active": "1",
            })
            if as_of < acct["first_seen"]:
                acct["first_seen"] = as_of
            n += 1
        warnings.append(f"{path.name}: parsed {n} rows "
                        f"(date='{header[date_idx]}', balance='{header[bal_idx]}')")

    return balance_rows, list(accounts.values()), warnings
