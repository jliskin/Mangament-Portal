"""Asset financials: NOI, per-unit P&L, arrears aging and operating KPIs.

Definitions (standard real-estate practice):
  * Effective Gross Income (EGI) = rent + condo fees + other income billed
    (accrual basis) or collected (cash basis).
  * Operating Expenses (OpEx) exclude CapEx and debt service.
  * NOI = EGI - OpEx.
  * Cash flow before debt service = NOI - CapEx.
Common-area expenses (unit_id NULL) are allocated to units pro-rata by
surface (sqm), falling back to equal shares if any surface is missing.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .billing import add_months, month_start


@dataclass(frozen=True)
class ChargeRow:
    charge_id: int
    lease_id: int
    unit_id: int
    charge_type: str
    period: date
    due_date: date
    amount_minor: int
    allocated_minor: int

    @property
    def outstanding_minor(self) -> int:
        return self.amount_minor - self.allocated_minor


@dataclass(frozen=True)
class PaymentRow:
    lease_id: int
    unit_id: int
    received_date: date
    amount_minor: int


@dataclass(frozen=True)
class ExpenseRow:
    unit_id: int | None
    expense_date: date
    amount_minor: int
    category: str
    is_capex: bool


@dataclass(frozen=True)
class UnitRow:
    unit_id: int
    unit_number: str
    size_sqm: float | None
    baseline_rent_minor: int
    occupied: bool


@dataclass
class NOIResult:
    start: date
    end: date
    basis: str
    revenue_minor: int
    revenue_by_type: dict[str, int]
    opex_minor: int
    opex_by_category: dict[str, int]
    capex_minor: int

    @property
    def noi_minor(self) -> int:
        return self.revenue_minor - self.opex_minor

    @property
    def noi_margin(self) -> float | None:
        return self.noi_minor / self.revenue_minor if self.revenue_minor else None

    @property
    def cash_flow_before_debt_minor(self) -> int:
        return self.noi_minor - self.capex_minor


def compute_noi(start: date, end: date, charges: list[ChargeRow], payments: list[PaymentRow],
                expenses: list[ExpenseRow], basis: str = "accrual") -> NOIResult:
    rev_by_type: dict[str, int] = defaultdict(int)
    if basis == "accrual":
        for c in charges:
            if start <= c.period <= end:
                rev_by_type[c.charge_type] += c.amount_minor
    elif basis == "cash":
        for p in payments:
            if start <= p.received_date <= end:
                rev_by_type["collected"] += p.amount_minor
    else:
        raise ValueError("basis must be 'accrual' or 'cash'")
    opex_by_cat: dict[str, int] = defaultdict(int)
    capex = 0
    for e in expenses:
        if not (start <= e.expense_date <= end):
            continue
        if e.is_capex:
            capex += e.amount_minor
        else:
            opex_by_cat[e.category] += e.amount_minor
    return NOIResult(start, end, basis, sum(rev_by_type.values()), dict(rev_by_type),
                     sum(opex_by_cat.values()), dict(opex_by_cat), capex)


def monthly_noi_series(months: int, as_of: date, charges, payments, expenses,
                       basis: str = "accrual") -> list[NOIResult]:
    first = add_months(month_start(as_of), -(months - 1))
    out = []
    for i in range(months):
        s = add_months(first, i)
        e = add_months(s, 1).replace(day=1)
        e = date.fromordinal(e.toordinal() - 1)
        out.append(compute_noi(s, e, charges, payments, expenses, basis))
    return out


@dataclass
class UnitPnL:
    unit_id: int
    unit_number: str
    revenue_minor: int = 0
    collected_minor: int = 0
    direct_opex_minor: int = 0
    allocated_opex_minor: int = 0
    outstanding_minor: int = 0

    @property
    def noi_minor(self) -> int:
        return self.revenue_minor - self.direct_opex_minor - self.allocated_opex_minor

    @property
    def collection_rate(self) -> float | None:
        return self.collected_minor / self.revenue_minor if self.revenue_minor else None


def per_unit_pnl(start: date, end: date, units: list[UnitRow], charges: list[ChargeRow],
                 expenses: list[ExpenseRow]) -> list[UnitPnL]:
    rows = {u.unit_id: UnitPnL(u.unit_id, u.unit_number) for u in units}
    for c in charges:
        if start <= c.period <= end and c.unit_id in rows:
            r = rows[c.unit_id]
            r.revenue_minor += c.amount_minor
            r.collected_minor += c.allocated_minor
            r.outstanding_minor += c.outstanding_minor
    common = 0
    for e in expenses:
        if not (start <= e.expense_date <= end) or e.is_capex:
            continue
        if e.unit_id is None:
            common += e.amount_minor
        elif e.unit_id in rows:
            rows[e.unit_id].direct_opex_minor += e.amount_minor
    shares = allocation_shares(units)
    # largest-remainder rounding so allocated cents sum exactly to `common`
    raw = {uid: common * s for uid, s in shares.items()}
    floors = {uid: int(v) for uid, v in raw.items()}
    leftover = common - sum(floors.values())
    for uid in sorted(raw, key=lambda u: raw[u] - floors[u], reverse=True)[:leftover]:
        floors[uid] += 1
    for uid, amt in floors.items():
        rows[uid].allocated_opex_minor = amt
    return sorted(rows.values(), key=lambda r: r.unit_number)


def allocation_shares(units: list[UnitRow]) -> dict[int, float]:
    if not units:
        return {}
    if all(u.size_sqm and u.size_sqm > 0 for u in units):
        total = sum(float(u.size_sqm) for u in units)
        return {u.unit_id: float(u.size_sqm) / total for u in units}
    return {u.unit_id: 1 / len(units) for u in units}


AGING_BUCKETS = (("current", "Al día (no vencido)"), ("d1_30", "1–30 días"), ("d31_60", "31–60 días"),
                 ("d61_90", "61–90 días"), ("d90_plus", "+90 días"))


def aging_bucket(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "d1_30"
    if days_past_due <= 60:
        return "d31_60"
    if days_past_due <= 90:
        return "d61_90"
    return "d90_plus"


@dataclass
class AgingRow:
    lease_id: int
    buckets: dict[str, int] = field(default_factory=lambda: {k: 0 for k, _ in AGING_BUCKETS})
    oldest_due: date | None = None

    @property
    def total_minor(self) -> int:
        return sum(self.buckets.values())

    @property
    def past_due_minor(self) -> int:
        return self.total_minor - self.buckets["current"]


def aging_report(charges: list[ChargeRow], as_of: date, grace_days: int = 0) -> list[AgingRow]:
    """Outstanding balances by days past due. Charges inside the grace period
    count as 'current' — they are not delinquent yet."""
    rows: dict[int, AgingRow] = {}
    for c in charges:
        if c.outstanding_minor <= 0 or c.period > as_of:
            continue
        r = rows.setdefault(c.lease_id, AgingRow(c.lease_id))
        days = (as_of - c.due_date).days - grace_days
        r.buckets[aging_bucket(days)] += c.outstanding_minor
        if days > 0 and (r.oldest_due is None or c.due_date < r.oldest_due):
            r.oldest_due = c.due_date
    return sorted(rows.values(), key=lambda r: r.past_due_minor, reverse=True)


@dataclass
class KPIs:
    billed_minor: int
    collected_on_billed_minor: int
    physical_occupancy: float | None
    economic_occupancy: float | None
    arrears_minor: int

    @property
    def collection_rate(self) -> float | None:
        return self.collected_on_billed_minor / self.billed_minor if self.billed_minor else None


def kpis(start: date, end: date, units: list[UnitRow], charges: list[ChargeRow], as_of: date) -> KPIs:
    in_period = [c for c in charges if start <= c.period <= end]
    billed = sum(c.amount_minor for c in in_period)
    collected = sum(c.allocated_minor for c in in_period)
    occupied = sum(1 for u in units if u.occupied)
    months = len({c.period for c in in_period}) or 1
    potential_rent = sum(u.baseline_rent_minor for u in units) * months
    actual_rent = sum(c.amount_minor for c in in_period if c.charge_type == "rent")
    arrears = sum(c.outstanding_minor for c in charges if c.due_date < as_of)
    return KPIs(
        billed_minor=billed,
        collected_on_billed_minor=collected,
        physical_occupancy=occupied / len(units) if units else None,
        economic_occupancy=actual_rent / potential_rent if potential_rent else None,
        arrears_minor=arrears,
    )
