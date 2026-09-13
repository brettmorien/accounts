"""Plain-text CSV store with idempotent upserts and crash-safe atomic writes.

The dataset is tiny (a handful of accounts, weekly, for a few years), so we load
each file fully, merge in memory keyed on a natural primary key, and rewrite it.
Writes go to a temp file in the same directory and are swapped in with
os.replace() -- atomic on POSIX -- so an interrupted sync can never corrupt data.
"""
import csv
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import config

# A first pass at how a withdrawal from an account gets taxed, guessed from its
# name (aggregators don't report this) so a future tax-liability model has
# something to key off of. "roth" is checked first since "Roth 401(k)" and
# "Roth IRA" must not fall through to "traditional". Never authoritative --
# accounts.csv is hand-editable, so a guess here is only ever a starting point;
# upsert_accounts() below preserves whatever value (guessed or corrected)
# already exists rather than re-guessing on every sync.
_TAX_TYPE_TRADITIONAL_COMPACT = ("401k", "403b", "457b", "457")
_TAX_TYPE_TRADITIONAL_WORDS = {"ira", "pension", "annuity", "rollover", "sep", "simple"}
_TAX_TYPE_TAXABLE_COMPACT = ("stockplan", "espp")   # equity-comp accounts: already-taxed shares
_TAX_TYPE_TAXABLE_WORDS = {"brokerage", "individual", "joint", "taxable", "trust", "llc"}


def _guess_tax_type(name: str) -> str:
    low = (name or "").lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    compact = re.sub(r"[^a-z0-9]", "", low)
    if "roth" in words:
        return "roth"
    if any(p in compact for p in _TAX_TYPE_TRADITIONAL_COMPACT) or words & _TAX_TYPE_TRADITIONAL_WORDS:
        return "traditional"
    if any(p in compact for p in _TAX_TYPE_TAXABLE_COMPACT) or words & _TAX_TYPE_TAXABLE_WORDS:
        return "taxable"
    return ""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Directory readable/traversable by owner only.
    try:
        os.chmod(config.DATA_DIR, 0o700)
    except OSError:
        pass


def _atomic_write_csv(path: Path, fieldnames: List[str], rows: Iterable[dict]) -> None:
    _ensure_data_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # atomic swap
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _atomic_write_json(path: Path, data: dict) -> None:
    _ensure_data_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Balances: keyed on (account_id, as_of)
# ---------------------------------------------------------------------------

def load_balances() -> List[dict]:
    return _read_csv(config.BALANCES_CSV)


def upsert_balances(new_rows: Iterable[dict]) -> int:
    """Insert-or-replace balance rows keyed on (account_id, as_of).

    Returns the number of rows that were newly added or changed.
    """
    existing: Dict[Tuple[str, str], dict] = {}
    for row in load_balances():
        existing[(row["account_id"], row["as_of"])] = row

    changed = 0
    for row in new_rows:
        key = (str(row["account_id"]), str(row["as_of"]))
        normalized = {
            "as_of": key[1],
            "account_id": key[0],
            "balance": _fmt_amount(row["balance"]),
            "source": row.get("source", ""),
        }
        if existing.get(key) != normalized:
            changed += 1
        existing[key] = normalized

    ordered = sorted(existing.values(), key=lambda r: (r["as_of"], r["account_id"]))
    _atomic_write_csv(config.BALANCES_CSV, config.BALANCE_FIELDS, ordered)
    return changed


def _fmt_amount(value) -> str:
    """Store amounts as a plain decimal string with 2 places."""
    return f"{float(value):.2f}"


# ---------------------------------------------------------------------------
# Accounts metadata: keyed on account_id
# ---------------------------------------------------------------------------

def load_accounts() -> List[dict]:
    return _read_csv(config.ACCOUNTS_CSV)


def upsert_accounts(new_rows: Iterable[dict]) -> None:
    existing: Dict[str, dict] = {r["account_id"]: r for r in load_accounts()}
    for row in new_rows:
        aid = str(row["account_id"])
        prior = existing.get(aid, {})
        name = row.get("name") or prior.get("name", "")
        merged = {
            "account_id": aid,
            "name": name,
            "org": row.get("org") or prior.get("org", ""),
            "currency": row.get("currency") or prior.get("currency", "USD"),
            # A hand-editable guess: preserve whatever's already on record (a
            # correction included) rather than re-guessing on every sync.
            "tax_type": prior.get("tax_type") or row.get("tax_type") or _guess_tax_type(name),
            # Preserve the original source once set (first writer wins).
            "source": prior.get("source") or row.get("source", ""),
            "first_seen": prior.get("first_seen") or row.get("first_seen", ""),
            "active": row.get("active", prior.get("active", "1")),
        }
        existing[aid] = merged
    ordered = sorted(existing.values(), key=lambda r: (r.get("org", ""), r["account_id"]))
    _atomic_write_csv(config.ACCOUNTS_CSV, config.ACCOUNT_FIELDS, ordered)


# ---------------------------------------------------------------------------
# Sync watermark
# ---------------------------------------------------------------------------

def get_watermark() -> Optional[str]:
    """Return the last successful sync date (YYYY-MM-DD), or None."""
    if not config.SYNC_STATE_JSON.exists():
        return None
    try:
        with config.SYNC_STATE_JSON.open() as fh:
            return json.load(fh).get("last_sync")
    except (OSError, ValueError):
        return None


def set_watermark(date_str: str, provider: str = config.DEFAULT_PROVIDER) -> None:
    _atomic_write_json(config.SYNC_STATE_JSON, {"last_sync": date_str, "provider": provider})
