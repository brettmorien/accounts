"""One-time onboarding.  Run:  python3 -m engine.setup

Steps (each is skippable / idempotent):
  1. Claim a SimpleFIN setup token -> Access URL, stored in the macOS Keychain.
  2. Vendor the uPlot chart library into assets/ (only if missing).
  3. Install a launchd user agent that runs `engine.sync` on a schedule.
  4. Run a first sync to verify accounts appear.

Nothing here prints the Access URL. Uninstall the schedule with --uninstall.
"""
import argparse
import plistlib
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import config, secrets
from .providers import simplefin

_UPLOT_BASE = "https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/"


# ---------------------------------------------------------------------------
# 1. Secret
# ---------------------------------------------------------------------------

def cmd_secret(token: str) -> None:
    token = token.strip()
    if not token:
        token = input("Paste your SimpleFIN setup token: ").strip()
    print("Claiming setup token (one-time)...")
    access_url = simplefin.claim_setup_token(token)
    secrets.set_secret(access_url)
    print("  Stored read-only Access URL in Keychain "
          f"(service={config.KEYCHAIN_SERVICE}, account={config.KEYCHAIN_ACCOUNT}).")


# ---------------------------------------------------------------------------
# 2. Vendor uPlot
# ---------------------------------------------------------------------------

def cmd_vendor(force: bool = False) -> None:
    config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for path, url in ((config.UPLOT_JS, _UPLOT_BASE + "uPlot.iife.min.js"),
                      (config.UPLOT_CSS, _UPLOT_BASE + "uPlot.min.css")):
        if path.exists() and not force:
            print(f"  {path.name}: already present")
            continue
        print(f"  downloading {path.name}...")
        with urllib.request.urlopen(url, timeout=30) as resp:
            path.write_bytes(resp.read())


# ---------------------------------------------------------------------------
# 3. launchd schedule
# ---------------------------------------------------------------------------

def _calendar_interval(schedule: str, hour: int, minute: int, weekday: int, day: int) -> dict:
    ci = {"Hour": hour, "Minute": minute}
    if schedule == "weekly":
        ci["Weekday"] = weekday          # 0 or 7 = Sunday ... 6 = Saturday
    elif schedule == "monthly":
        ci["Day"] = day
    return ci  # "daily" => hour+minute only


def cmd_schedule(schedule: str, hour: int, minute: int, weekday: int, day: int) -> None:
    config.SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": config.LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "engine.sync"],
        "WorkingDirectory": str(config.ROOT),
        "EnvironmentVariables": {"PYTHONPATH": str(config.ROOT)},
        "StartCalendarInterval": _calendar_interval(schedule, hour, minute, weekday, day),
        "StandardOutPath": str(config.SYNC_LOG),
        "StandardErrorPath": str(config.SYNC_LOG),
        "RunAtLoad": False,
    }
    config.LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with config.LAUNCHD_PLIST.open("wb") as fh:
        plistlib.dump(plist, fh)
    # Reload cleanly (ignore "not loaded" on first install).
    subprocess.run(["launchctl", "unload", str(config.LAUNCHD_PLIST)],
                   capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(config.LAUNCHD_PLIST)], check=True)
    print(f"  Installed launchd agent '{config.LAUNCHD_LABEL}' ({schedule} "
          f"at {hour:02d}:{minute:02d}). Logs -> {config.SYNC_LOG}")


def cmd_uninstall() -> None:
    subprocess.run(["launchctl", "unload", str(config.LAUNCHD_PLIST)], capture_output=True)
    if config.LAUNCHD_PLIST.exists():
        config.LAUNCHD_PLIST.unlink()
    print(f"  Removed launchd agent '{config.LAUNCHD_LABEL}'. "
          "(Secret and data are left intact.)")


# ---------------------------------------------------------------------------
# 4. First sync
# ---------------------------------------------------------------------------

def cmd_first_sync() -> int:
    from . import sync
    print("Running first sync...")
    return sync.main([])


def _print_export_hint() -> None:
    print(f"""
Next, backfill history (once per account) for a real multi-year trend:
  1. In each brokerage site, download a *value/balance history* CSV
     (or a positions/statement export with period-end balances).
  2. Drop the file(s) into: {config.IMPORT_DIR}
  3. (Optional) create {config.ACCOUNT_MAP_JSON} mapping each export's account
     label to its aggregator account id so history stitches onto the live account.
  4. Run:  python3 -m engine.sync   (or the accounts-sync skill)
""")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Set up the accounts trend tracker.")
    p.add_argument("--token", default="", help="SimpleFIN setup token (else prompt)")
    p.add_argument("--schedule", choices=["daily", "weekly", "monthly"], default="weekly")
    p.add_argument("--hour", type=int, default=6)
    p.add_argument("--minute", type=int, default=0)
    p.add_argument("--weekday", type=int, default=1, help="0/7=Sun..6=Sat (weekly)")
    p.add_argument("--day", type=int, default=1, help="day of month (monthly)")
    p.add_argument("--no-secret", action="store_true", help="skip Keychain step")
    p.add_argument("--no-launchd", action="store_true", help="skip schedule install")
    p.add_argument("--no-first-sync", action="store_true")
    p.add_argument("--force-vendor", action="store_true", help="re-download uPlot")
    p.add_argument("--uninstall", action="store_true", help="remove the launchd agent and exit")
    args = p.parse_args(argv)

    if args.uninstall:
        cmd_uninstall()
        return 0

    print("== 1/4 Secret ==")
    if args.no_secret:
        print("  skipped")
    else:
        cmd_secret(args.token)

    print("== 2/4 Chart assets ==")
    cmd_vendor(force=args.force_vendor)

    print("== 3/4 Schedule ==")
    if args.no_launchd:
        print("  skipped")
    else:
        cmd_schedule(args.schedule, args.hour, args.minute, args.weekday, args.day)

    print("== 4/4 First sync ==")
    rc = 0
    if args.no_first_sync:
        print("  skipped")
    else:
        rc = cmd_first_sync()

    _print_export_hint()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
