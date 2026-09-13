"""Repeatable end-to-end test against a throwaway scratch directory.

Runs the real entrypoints as subprocesses (as launchd would), so it exercises the
same code path. Asserts: 7-year backfill on first run, incremental window on the
second, idempotency, correct net-worth total, and that the dashboard renders
offline (uPlot inlined, valid JSON).

Run:  PYTHONPATH=. python3 -m engine.tests.test_e2e
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "engine" / "tests" / "fixture_simplefin.json"
EXPECTED_TOTAL = 158200.42 + 91340.10 + 47990.00  # sum of fixture balances


def _run(args, env):
    r = subprocess.run([sys.executable, "-m"] + args, cwd=str(ROOT), env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"{args} failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="accounts-e2e-"))
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "ACCOUNTS_DATA_DIR": str(scratch / "data"),
        "ACCOUNTS_IMPORT_DIR": str(scratch / "import"),
        "ACCOUNTS_DASHBOARD": str(scratch / "dashboard.html"),
    })
    (scratch / "import").mkdir(parents=True)

    _run(["engine.tests.make_sample_history"], env)
    out1 = _run(["engine.sync", "--mock", str(FIXTURE), "--commit-watermark"], env)
    balances = list(csv.DictReader((scratch / "data" / "balances.csv").open()))
    n1 = len(balances)
    out2 = _run(["engine.sync", "--mock", str(FIXTURE), "--commit-watermark"], env)
    n2 = len(list(csv.DictReader((scratch / "data" / "balances.csv").open())))

    checks = []

    def check(name, cond):
        checks.append((name, cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    today = date.today().isoformat()
    seven_years = date.today().replace(year=date.today().year - 7).isoformat()

    check("first run uses 7-year window", f"window {seven_years}" in out1)
    check("second run uses watermark window (not 7y)", f"window {today} -> {today}" in out2)
    check("both sources present",
          {r["source"] for r in balances} == {"import", "aggregator"})
    check("idempotent (row count unchanged on rerun)", n1 == n2)

    accounts = {r["account_id"]: r for r in
                csv.DictReader((scratch / "data" / "accounts.csv").open())}
    check("tax_type guessed from account name (401(k) -> traditional)",
          accounts["ACT-fid-401k"]["tax_type"] == "traditional")
    check("tax_type guessed from account name (Roth IRA -> roth)",
          accounts["ACT-van-roth"]["tax_type"] == "roth")
    check("tax_type guessed from account name (Brokerage -> taxable)",
          accounts["ACT-schwab-brk"]["tax_type"] == "taxable")

    # Net worth = latest LOCF total; recompute from dashboard payload.
    html = (scratch / "dashboard.html").read_text()
    check("uPlot inlined (offline)", "uPlot=function" in html)
    m = re.search(r"const DATA = (\{.*?\});\nconst usd", html, re.S)
    data = json.loads(m.group(1))
    check("dashboard JSON valid & has series", len(data["comp"]["labels"]) == 3)
    check("net worth matches aggregator sum (no double-count)",
          abs(data["grand_total"] - EXPECTED_TOTAL) < 1.0)
    check("history spans ~7 years", len(data["x"]) >= 80)

    # Secret must never appear; redaction sanity.
    from engine.secrets import redact
    check("redaction scrubs credentials",
          "u:p@" not in redact("https://u:p@bridge.simplefin.org/x"))

    passed = all(c for _, c in checks)
    print(f"\n{'ALL PASSED' if passed else 'FAILURES PRESENT'} "
          f"({sum(c for _, c in checks)}/{len(checks)})  scratch={scratch}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
