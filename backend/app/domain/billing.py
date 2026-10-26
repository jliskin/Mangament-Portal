"""Invoicing engine: which recurring charges a lease owes for a given month."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


MONTHS_ES = ("", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")


@dataclass(frozen=True)
class LeaseTerms:
    lease_id: int
    unit_id: int
    start_date: date
    end_date: date
    monthly_rent_minor: int
    monthly_condo_fee_minor: int
    due_day: int
    terminated_at: date | None = None
    exit_date: date | None = None

    @property
    def last_day(self) -> date:
        """Last day the lease is billable (termination/move-out cut it short)."""
        return min(d for d in (self.terminated_at, self.exit_date, self.end_date) if d)


@dataclass(frozen=True)
class ChargeDraft:
    lease_id: int
    unit_id: int
    charge_type: str
    period: date
    due_date: date
    amount_minor: int
    description: str


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def prorate(amount_minor: int, period: date, first: date, last: date) -> tuple[int, int, int]:
    """Actual-days proration. Returns (amount, billable_days, days_in_month)."""
    p_start, p_end = month_start(period), month_end(period)
    start, end = max(p_start, first), min(p_end, last)
    if end < start:
        return 0, 0, p_end.day
    days = (end - start).days + 1
    if days == p_end.day:
        return amount_minor, days, days
    return round(amount_minor * days / p_end.day), days, p_end.day


def charges_for_period(lease: LeaseTerms, period: date, prorate_partial: bool = True) -> list[ChargeDraft]:
    """Recurring charges (rent + condo fee) for `lease` in month `period`.

    Pure function; persistence is idempotent via the unique index
    ux_charges_recurring, so running the job twice never double-bills.
    """
    period = month_start(period)
    first, last = lease.start_date, lease.last_day
    if last < period or first > month_end(period):
        return []
    due = period.replace(day=min(lease.due_day, 28))
    label = f"{MONTHS_ES[period.month]} {period.year}"
    drafts = []
    for ctype, base, name in (("rent", lease.monthly_rent_minor, "Alquiler"),
                              ("condo_fee", lease.monthly_condo_fee_minor, "Expensas / condominio")):
        if base <= 0:
            continue
        amount, days, dim = prorate(base, period, first, last) if prorate_partial else (base, 0, 0)
        if amount <= 0:
            continue
        desc = f"{name} {label}" + (f" (prorrateo {days}/{dim} días)" if prorate_partial and days != dim else "")
        drafts.append(ChargeDraft(lease.lease_id, lease.unit_id, ctype, period, due, amount, desc))
    return drafts


def periods_between(first: date, last: date) -> list[date]:
    out, p = [], month_start(first)
    while p <= last:
        out.append(p)
        p = add_months(p, 1)
    return out
