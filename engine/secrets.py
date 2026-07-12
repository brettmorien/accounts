"""Read/write the single read-only secret (the SimpleFIN Access URL) via the
macOS Keychain, and redact it from any log/error output.

The Access URL embeds HTTP Basic Auth credentials
(https://user:pass@bridge.simplefin.org/simplefin) and is read-only by protocol,
but we still never write it to disk or print it.
"""
import re
import subprocess
from typing import Optional

from . import config


def get_secret(
    account: str = config.KEYCHAIN_ACCOUNT,
    service: str = config.KEYCHAIN_SERVICE,
) -> Optional[str]:
    """Return the stored secret, or None if not present in the Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def set_secret(
    value: str,
    account: str = config.KEYCHAIN_ACCOUNT,
    service: str = config.KEYCHAIN_SERVICE,
) -> None:
    """Store (or update) the secret in the Keychain.

    Note: the value is passed on the argv of `security`, so it is briefly visible
    to `ps` for other processes owned by this user. Acceptable for a one-time
    setup on the user's own machine; the alternative (interactive prompt) is used
    by `setup.py` when possible.
    """
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
        check=True,
        capture_output=True,
        text=True,
    )


# Matches the "user:pass@" portion of any http(s) URL so we can scrub it.
_CREDS_RE = re.compile(r"(https?://)[^/@\s]+@")


def redact(text: str) -> str:
    """Strip embedded Basic-Auth credentials from any URL in `text`."""
    if not text:
        return text
    return _CREDS_RE.sub(r"\1***:***@", text)
