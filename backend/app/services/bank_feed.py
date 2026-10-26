"""Bank API / Open Banking ingestion layer.

Direct bank APIs are reached through a licensed aggregator (e.g. Plaid in the
US, GoCardless Bank Account Data / Nordigen in the EU/UK, Belvo or Pluggy in
LatAm, a licensed AISP under Israel's open-banking regime). The platform
never stores bank credentials; it stores only the aggregator's opaque token.

Two ingestion modes share the same pipeline as the CSV import
(fingerprint dedup → matching → 1-click confirmation):

1. Pull: a scheduled job calls ``BankFeedProvider.fetch_transactions``.
2. Push: the aggregator POSTs to ``/api/bank-feed/webhook``. Requests are
   authenticated with HMAC-SHA256 over ``"{timestamp}.{body}"`` and rejected
   if older than 5 minutes (replay protection).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from ..domain.bank_parser import ParsedTransaction
from ..domain.money import to_minor

MAX_SKEW_S = 300


@dataclass(frozen=True)
class FeedTransaction:
    external_id: str
    booking_date: date
    amount: Decimal              # signed, major units, as aggregators usually send it
    description: str
    counterparty: str | None = None
    reference: str | None = None


class BankFeedProvider(Protocol):
    def fetch_transactions(self, account_token: str, since: date) -> list[FeedTransaction]: ...


def verify_signature(secret: str, header: str | None, body: bytes, now: float | None = None) -> bool:
    """Header format: ``t=<unix>,v1=<hex hmac>``."""
    if not secret or not header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - ts) > MAX_SKEW_S:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def to_parsed(account_id: int, items: list[FeedTransaction]) -> tuple[list[ParsedTransaction], dict[str, str]]:
    """Normalize feed rows. Identity = provider's external id (stable across pulls)."""
    out, ext = [], {}
    for i, it in enumerate(items):
        fp = hashlib.sha256(f"api|acct{account_id}|{it.external_id}".encode()).hexdigest()
        out.append(ParsedTransaction(row_number=i + 1, txn_date=it.booking_date, amount_minor=to_minor(it.amount),
                                     description=it.description, counterparty_name=it.counterparty,
                                     reference=it.reference, raw={"external_id": it.external_id}, fingerprint=fp))
        ext[fp] = it.external_id
    return out, ext
