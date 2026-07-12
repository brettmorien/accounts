"""Provider-agnostic types. Adding Plaid later means implementing `fetch` to
return the same AccountBalance list -- nothing downstream changes.
"""
from dataclasses import dataclass
from datetime import date
from typing import List


@dataclass
class AccountBalance:
    account_id: str
    name: str
    org: str
    currency: str
    balance: float
    as_of: str  # YYYY-MM-DD

    def balance_row(self, source: str) -> dict:
        return {
            "as_of": self.as_of,
            "account_id": self.account_id,
            "balance": self.balance,
            "source": source,
        }

    def account_row(self, source: str) -> dict:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "org": self.org,
            "currency": self.currency,
            "source": source,
            "first_seen": self.as_of,
            "active": "1",
        }


class Provider:
    """Interface implemented by each aggregator adapter."""

    name = "base"

    def fetch(self, since: date, until: date) -> List[AccountBalance]:
        raise NotImplementedError
