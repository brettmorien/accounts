"""Dev helper: synthesize ~7 years of monthly history + an account_map.json into
the configured IMPORT_DIR, so the pipeline can be exercised end-to-end.

Run with ACCOUNTS_IMPORT_DIR pointed at a scratch folder:
    ACCOUNTS_IMPORT_DIR=/tmp/scratch/import python3 -m engine.tests.make_sample_history
"""
import csv
import json
import math
from datetime import date

from .. import config

# Import label -> aggregator account id (stitches history onto the live account).
ACCOUNTS = [
    ("Fidelity 401k", "ACT-fid-401k", 60000, 0.011),
    ("Vanguard Roth", "ACT-van-roth", 40000, 0.010),
    ("Schwab Brokerage", "ACT-schwab-brk", 20000, 0.009),
]


def month_iter(start_y, start_m, count):
    y, m = start_y, start_m
    for _ in range(count):
        yield date(y, m, 1)
        m += 1
        if m > 12:
            m, y = 1, y + 1


def main():
    config.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    # 84 months (7y) ending ~2 months ago so the aggregator point extends the series.
    end = today.year * 12 + today.month - 3
    start = end - 83
    months = list(month_iter(start // 12, start % 12 + 1, 84))

    rows = []
    for i, d in enumerate(months):
        for label, _id, base, rate in ACCOUNTS:
            drift = base * math.exp(rate * i)
            wobble = 1 + 0.06 * math.sin(i / 5.0)  # gentle market waves
            rows.append({"Date": d.strftime("%Y-%m-%d"),
                         "Account": label,
                         "Value": f"{drift * wobble:.2f}"})

    hist = config.IMPORT_DIR / "history.csv"
    with hist.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Date", "Account", "Value"])
        w.writeheader()
        w.writerows(rows)

    amap = {label: _id for label, _id, _, _ in ACCOUNTS}
    (config.IMPORT_DIR / "account_map.json").write_text(json.dumps(amap, indent=2))
    print(f"wrote {len(rows)} rows to {hist} and account_map.json")


if __name__ == "__main__":
    main()
