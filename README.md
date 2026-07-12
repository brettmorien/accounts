# Investment Balance Trend Tracker

Gathers balances from several investment accounts, stores them over time as plain
CSV, and renders a self-contained local dashboard of the trend. Refreshes run
unattended via `launchd`; the only secret is a **read-only** aggregator token in
the macOS Keychain.

## How it works

- **Access:** a read-only aggregator (SimpleFIN Bridge by default) holds the
  brokerage logins in its vault. You link each institution once (approving MFA
  once); after that, refreshes need no login and no MFA. The provider is behind an
  adapter (`engine/providers/`) so Plaid can be swapped in later.
- **One job, `engine/sync.py`:** every run it (1) ingests any history CSVs in
  `import/`, (2) pulls the aggregator for the window since the last run — or up to
  7 years back on a first run, (3) advances a watermark, (4) regenerates the
  dashboard. Safe on any cadence and idempotent (keyed on account + date).
- **History:** aggregators only return the *current* balance, so multi-year
  history comes from brokerage value-history exports dropped in `import/`
  (see `import/README.txt`).
- **Storage:** plain text — `data/balances.csv`, `data/accounts.csv`,
  `data/sync_state.json` (mode 600, gitignored).
- **Dashboard:** `dashboard.html`, chart library inlined → renders fully offline.

## Usage (via Claude Code skills)

- **`accounts-setup`** — one-time: claim a SimpleFIN setup token into the Keychain,
  install the weekly schedule, first sync.
- **`accounts-sync`** — on-demand refresh (same job `launchd` runs).
- **`accounts-dashboard`** — regenerate and open the dashboard from stored data.

Or directly:

```bash
PYTHONPATH=. python3 -m engine.setup      # onboarding
PYTHONPATH=. python3 -m engine.sync       # refresh
PYTHONPATH=. python3 -m engine.dashboard  # rebuild dashboard.html
PYTHONPATH=. python3 -m engine.tests.test_e2e   # run the test suite
```

## Security

- The Access URL (embeds Basic Auth, read-only by protocol — cannot move money)
  lives only in the macOS Keychain (`security`, service `financial-accounts`).
  It is never written to disk, committed, or printed; it is redacted from all logs.
- Balance data stays local (`data/`, `import/`, `dashboard.html` are gitignored).

## Requirements

Python 3 (standard library only — no pip install). macOS (`security`, `launchctl`).
A SimpleFIN Bridge subscription (~$15/yr) or a Plaid account.
