Drop brokerage value-history / statement exports here as CSV to backfill
multi-year history. Aggregators only return the CURRENT balance, so this folder
is the only source of years-deep history. Files are re-read every sync and merged
idempotently, so re-dropping the same file is harmless.

The parser is tolerant of real brokerage exports: it skips title/preamble rows,
handles a UTF-8 BOM, finds the value-history section in multi-section files, and
understands columns like "Ending balance" or "Account: all" and dates like
"Jul 2025(As of Jul-16-2025)". Confirmed working with E*TRADE "Account Value
Summary" and Fidelity "Investment income" exports.

Two shapes are auto-detected:

  * Per-account file — has an account/name column (account | name | fund); each
    row becomes its own account (id "import:<slug>").
  * Whole-institution file — no account column; the whole file is one historical
    series for the institution inferred from the filename (id "hist:<institution>"),
    e.g. name the file "Fidelity ...csv" or "ETrade ...csv".

History hands off to live data automatically: once an institution starts coming
through the aggregator, its "(history)" series stops contributing from that date
so it never double-counts with the live per-account balances. An institution the
aggregator does NOT cover (e.g. Fidelity while its connection is down) keeps
contributing its last known value until live data arrives.

Minimum needs: some column the parser reads as a date, and some column it reads
as a balance. Simple hand-made files also work:

  Date,Account,Value
  2020-01-31,Fidelity 401k,60250.11
  2020-02-29,Fidelity 401k,58990.02

Optional account_map.json maps an account label OR an institution filename-label
to a specific aggregator account id, to stitch history onto a live account (one
continuous series). Find aggregator account ids in data/accounts.csv after a sync:

  { "Fidelity 401k": "ACT-abc123", "E*TRADE": "ACT-def456" }
