"""Cash application: how a payment is spread across open charges."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Within the same due date, which charge type is settled first.
DEFAULT_PRIORITY = ("late_fee", "condo_fee", "rent", "utility", "other")


@dataclass(frozen=True)
class OpenItem:
    charge_id: int
    due_date: date
    charge_type: str
    outstanding_minor: int


def allocate_fifo(amount_minor: int, open_items: list[OpenItem],
                  priority: tuple[str, ...] = DEFAULT_PRIORITY) -> tuple[list[tuple[int, int]], int]:
    """Apply `amount_minor` oldest-first. Returns ([(charge_id, amount)], unapplied_credit).

    Oldest-first is the standard arrears convention and minimizes the age of
    the remaining debt (which is what the aging report and any late-fee
    policy key on). Unapplied money stays as a credit on the payment and is
    consumed when the next month's charges are generated.
    """
    rank = {t: i for i, t in enumerate(priority)}
    ordered = sorted(open_items, key=lambda c: (c.due_date, rank.get(c.charge_type, 99), c.charge_id))
    remaining, out = amount_minor, []
    for item in ordered:
        if remaining <= 0:
            break
        take = min(remaining, item.outstanding_minor)
        if take > 0:
            out.append((item.charge_id, take))
            remaining -= take
    return out, remaining
