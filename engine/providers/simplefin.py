"""SimpleFIN Bridge adapter.

Setup (one-time, human): link institutions in the SimpleFIN Bridge UI and create
a *setup token* (a base64-encoded claim URL). `claim_setup_token` POSTs to that
claim URL exactly once and receives the long-lived *Access URL*
(https://user:pass@bridge.simplefin.org/simplefin) which is the only stored secret.

Fetch: GET {access_url}/accounts?balances-only=1 with the URL's embedded Basic
Auth. Returns the current balance per account (aggregators do not provide history).
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import List, Optional

from .base import AccountBalance, Provider

_TIMEOUT = 30

# SimpleFIN Bridge sits behind Cloudflare, which blocks the default
# "Python-urllib/x.y" User-Agent with a 403 ("error code: 1010"). Present a
# normal browser UA so requests reach the application.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_403_HINT = ("Cloudflare blocked the request (error 1010) — usually the User-Agent; "
             "otherwise the token was already claimed or the Bridge subscription is inactive")
_HTTP_HINTS = {403: _403_HINT, 404: "the claim URL was not found — the token may be malformed"}


def _decode_claim_url(setup_token: str) -> str:
    raw = setup_token.strip()
    try:
        # Tolerate missing base64 padding, which pasted tokens sometimes lack.
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Setup token is not valid base64 ({exc}). "
                           "Paste the full token from Bridge → Apps.") from None
    if not decoded.startswith("http"):
        raise RuntimeError(f"Setup token did not decode to a URL (got {decoded[:40]!r}). "
                           "Make sure you copied the whole token.")
    return decoded


def claim_setup_token(setup_token: str, verbose: bool = True) -> str:
    """Exchange a one-time setup token for a durable Access URL."""
    claim_url = _decode_claim_url(setup_token)
    parts = urllib.parse.urlsplit(claim_url)
    if verbose:
        # Confirm the target without leaking the random claim id.
        redacted = re.sub(r"(/claim/).*$", r"\1****", parts.path)
        print(f"  claiming at {parts.scheme}://{parts.netloc}{redacted}")
    req = urllib.request.Request(claim_url, method="POST", data=b"",
                                 headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            access_url = resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            pass
        hint = _HTTP_HINTS.get(exc.code, "unexpected error")
        detail = f" Server said: {body[:300]}" if body else ""
        raise RuntimeError(f"SimpleFIN claim failed: HTTP {exc.code} ({hint}).{detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach SimpleFIN ({exc.reason}). "
                           "Check your network connection.") from None
    if not access_url.startswith("http"):
        raise RuntimeError(f"Claim did not return an Access URL (got {access_url[:60]!r}).")
    return access_url


def _split_auth(access_url: str):
    """Return (clean_url_without_creds, authorization_header_or_None)."""
    parts = urllib.parse.urlsplit(access_url)
    if parts.username is None:
        return access_url, None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    raw = f"{parts.username}:{parts.password or ''}".encode("utf-8")
    header = "Basic " + base64.b64encode(raw).decode("ascii")
    return clean, header


def parse_accounts(data: dict, fallback_as_of: str) -> List[AccountBalance]:
    """Turn a SimpleFIN /accounts JSON payload into AccountBalance rows."""
    out: List[AccountBalance] = []
    for acct in data.get("accounts", []):
        org = acct.get("org") or {}
        org_name = org.get("name") or org.get("domain") or "" if isinstance(org, dict) else str(org)
        as_of = _epoch_to_date(acct.get("balance-date")) or fallback_as_of
        try:
            balance = float(acct.get("balance"))
        except (TypeError, ValueError):
            continue
        out.append(AccountBalance(
            account_id=str(acct.get("id")),
            name=acct.get("name") or str(acct.get("id")),
            org=org_name,
            currency=acct.get("currency") or "USD",
            balance=balance,
            as_of=as_of,
        ))
    return out


def _epoch_to_date(epoch) -> Optional[str]:
    if epoch in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


class SimpleFinProvider(Provider):
    name = "simplefin"

    def __init__(self, access_url: str):
        self._clean_url, self._auth = _split_auth(access_url)

    def fetch(self, since: date, until: date) -> List[AccountBalance]:
        query = {"balances-only": "1"}
        if since:
            query["start-date"] = str(int(datetime(since.year, since.month, since.day,
                                                    tzinfo=timezone.utc).timestamp()))
        url = self._clean_url.rstrip("/") + "/accounts?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": _UA})
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"SimpleFIN fetch failed with HTTP {exc.code}") from None
        for err in data.get("errors", []):
            # SimpleFIN reports per-institution errors here (e.g. re-auth needed).
            print(f"  [simplefin] warning: {err}")
        return parse_accounts(data, fallback_as_of=until.strftime("%Y-%m-%d"))
