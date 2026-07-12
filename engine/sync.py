"""The one data-pull job. Run as:  python3 -m engine.sync

Every run, in order:
  1. Ingest any historical export files in import/ (idempotent).
  2. Pull the aggregator for the incremental window [since, today], where
     since = last successful sync, or today - 7y on a first run with no history.
  3. Upsert results and advance the watermark to today.
  4. Regenerate the dashboard.

Because the window is derived from the watermark, this is safe on ANY cadence:
skip a week and the next run simply covers the gap. Idempotent throughout.
"""
import argparse
import json
import sys
from datetime import date

from . import config, secrets, store
from .providers import base, simplefin


def _years_ago(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year - years, day=28)


def _window(args) -> date:
    """Compute the start of the pull window."""
    if args.since:
        return date.fromisoformat(args.since)
    watermark = store.get_watermark()
    if watermark:
        return date.fromisoformat(watermark)
    return _years_ago(date.today(), config.BACKFILL_YEARS)


def _load_mock(path: str, until: date):
    with open(path) as fh:
        data = json.load(fh)
    return simplefin.parse_accounts(data, fallback_as_of=until.strftime("%Y-%m-%d"))


def run(args) -> int:
    today = date.today()
    since = _window(args)
    print(f"sync: window {since.isoformat()} -> {today.isoformat()}")

    # --- 1. Ingest historical export files ------------------------------------
    imp_balances, imp_accounts, warnings = ingest_step()
    if imp_balances:
        store.upsert_accounts(imp_accounts)
        added = store.upsert_balances(imp_balances)
        print(f"  import: {len(imp_balances)} rows from files ({added} new/changed)")
    for w in warnings:
        print(f"  import warning: {w}")

    # --- 2. Aggregator pull ---------------------------------------------------
    balances = []
    if args.mock:
        balances = _load_mock(args.mock, today)
        print(f"  aggregator (mock): {len(balances)} accounts")
    elif args.no_aggregator:
        print("  aggregator: skipped (--no-aggregator)")
    else:
        access_url = secrets.get_secret()
        if not access_url:
            print("  aggregator: no Access URL in Keychain -- run accounts-setup. "
                  "Continuing with imported data only.")
        else:
            try:
                provider = simplefin.SimpleFinProvider(access_url)
                balances = provider.fetch(since, today)
                print(f"  aggregator: {len(balances)} accounts")
            except Exception as exc:  # noqa: BLE001 - surface but never leak the URL
                print(f"  aggregator error: {secrets.redact(str(exc))}", file=sys.stderr)
                return 1

    if balances:
        store.upsert_accounts([b.account_row("aggregator") for b in balances])
        added = store.upsert_balances([b.balance_row("aggregator") for b in balances])
        print(f"  stored aggregator balances ({added} new/changed)")

    # --- 3. Advance watermark -------------------------------------------------
    if not args.mock or args.commit_watermark:
        store.set_watermark(today.isoformat(), provider=config.DEFAULT_PROVIDER)

    # --- 4. Dashboard ---------------------------------------------------------
    if not args.no_dashboard:
        from . import dashboard
        out = dashboard.generate()
        print(f"  dashboard: {out}")

    return 0


def ingest_step():
    from . import ingest
    return ingest.ingest_files()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Sync investment balances (the one job).")
    p.add_argument("--mock", metavar="FIXTURE.json",
                   help="read aggregator data from a SimpleFIN JSON file instead of the network")
    p.add_argument("--no-aggregator", action="store_true",
                   help="ingest import files only; skip the aggregator")
    p.add_argument("--no-dashboard", action="store_true", help="skip dashboard regeneration")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="override the window start")
    p.add_argument("--commit-watermark", action="store_true",
                   help="advance the watermark even in --mock mode")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
