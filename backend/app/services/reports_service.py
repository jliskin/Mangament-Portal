"""Read models: load ORM data into the pure financial functions."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domain import financials as fin
from ..domain.billing import add_months, month_end, month_start
from ..domain.leases import expiry_alerts, lease_status
from ..models import Charge, Expense, Lease, LeaseParty, Payment, Property, Unit


def load_charges(db: Session) -> list[fin.ChargeRow]:
    rows = db.scalars(select(Charge).options(selectinload(Charge.allocations)).where(Charge.voided_at.is_(None)))
    return [fin.ChargeRow(c.id, c.lease_id, c.unit_id, c.charge_type, c.period, c.due_date, c.amount_minor,
                          c.allocated_minor) for c in rows]


def load_payments(db: Session) -> list[fin.PaymentRow]:
    rows = db.execute(select(Payment.lease_id, Lease.unit_id, Payment.received_date, Payment.amount_minor)
                      .join(Lease)).all()
    return [fin.PaymentRow(*r) for r in rows]


def load_expenses(db: Session) -> list[fin.ExpenseRow]:
    rows = db.scalars(select(Expense).options(selectinload(Expense.category)))
    return [fin.ExpenseRow(e.unit_id, e.expense_date, e.amount_minor, e.category.code, e.category.is_capex)
            for e in rows]


def load_units(db: Session, today: date) -> list[fin.UnitRow]:
    units = db.scalars(select(Unit).options(selectinload(Unit.leases)).where(Unit.is_active.is_(True))).all()
    out = []
    for u in units:
        occupied = any(lease_status(l.start_date, l.end_date, today, terminated_at=l.terminated_at)
                       in ("active", "expiring_soon") for l in u.leases)
        out.append(fin.UnitRow(u.id, u.unit_number, float(u.size_sqm) if u.size_sqm else None,
                               u.baseline_rent_minor, occupied))
    return out


def leases_with_status(db: Session, today: date):
    prop = db.scalar(select(Property))
    soon = prop.expiring_soon_days if prop else 90
    leases = db.scalars(select(Lease).options(
        selectinload(Lease.unit), selectinload(Lease.parties).selectinload(LeaseParty.tenant))
        .order_by(Lease.end_date)).all()
    return [(l, lease_status(l.start_date, l.end_date, today, soon, l.terminated_at)) for l in leases]


def dashboard(db: Session, today: date) -> dict:
    prop = db.scalar(select(Property))
    grace = prop.grace_days if prop else 0
    charges, payments, expenses = load_charges(db), load_payments(db), load_expenses(db)
    units = load_units(db, today)
    m_start, m_end = month_start(today), month_end(today)
    y_start = date(today.year, 1, 1)

    noi_mtd = fin.compute_noi(m_start, m_end, charges, payments, expenses)
    noi_ytd = fin.compute_noi(y_start, m_end, charges, payments, expenses)
    cash_mtd = fin.compute_noi(m_start, m_end, charges, payments, expenses, basis="cash")
    series = fin.monthly_noi_series(12, today, charges, payments, expenses)
    k = fin.kpis(m_start, m_end, units, charges, today)

    leases = {l.id: (l, st) for l, st in leases_with_status(db, today)}
    aging = fin.aging_report(charges, today, grace)
    aging_rows = [(leases[a.lease_id][0], a) for a in aging if a.lease_id in leases and a.total_minor > 0]
    aging_totals = {k_: sum(a.buckets[k_] for _, a in aging_rows) for k_, _ in fin.AGING_BUCKETS}

    alerts = []
    for l, st in leases.values():
        if st in ("active", "expiring_soon", "expired") and not l.terminated_at:
            alerts.extend((l, a) for a in expiry_alerts(l.id, l.unit.unit_number, l.end_date, today,
                                                         l.has_renewal_option, l.renewal_notice_days))
    expiring = sorted([(l, st) for l, st in leases.values() if st in ("expiring_soon", "expired")
                       and not l.terminated_at], key=lambda x: x[0].end_date)

    # rent-roll matrix: last 6 months x units, cell = payment status
    months = [add_months(m_start, -i) for i in range(5, -1, -1)]
    unit_numbers = {u.unit_id: u.unit_number for u in units}
    matrix: dict[int, dict[date, dict]] = {}
    for c in charges:
        if c.period in months and c.unit_id in unit_numbers:
            cell = matrix.setdefault(c.unit_id, {}).setdefault(c.period, {"amount": 0, "allocated": 0, "late": False})
            cell["amount"] += c.amount_minor
            cell["allocated"] += c.allocated_minor
            cell["late"] |= c.outstanding_minor > 0 and (today - c.due_date).days > grace
    for row in matrix.values():
        for cell in row.values():
            cell["status"] = ("paid" if cell["allocated"] >= cell["amount"]
                              else "partial" if cell["allocated"] > 0 else "unpaid")

    return {
        "property": prop, "today": today, "noi_mtd": noi_mtd, "noi_ytd": noi_ytd, "cash_mtd": cash_mtd,
        "series": series, "kpis": k, "aging_rows": aging_rows, "aging_totals": aging_totals,
        "aging_buckets": fin.AGING_BUCKETS, "alerts": sorted(alerts, key=lambda x: x[1].days_left),
        "expiring": expiring, "months": months, "matrix": matrix,
        "units": sorted(units, key=lambda u: u.unit_number),
    }


def unit_pnl(db: Session, start: date, end: date, today: date) -> list[fin.UnitPnL]:
    return fin.per_unit_pnl(start, end, load_units(db, today), load_charges(db), load_expenses(db))


def noi(db: Session, start: date, end: date, basis: str) -> fin.NOIResult:
    return fin.compute_noi(start, end, load_charges(db), load_payments(db), load_expenses(db), basis)


def aging(db: Session, today: date):
    prop = db.scalar(select(Property))
    leases = {l.id: l for l, _ in leases_with_status(db, today)}
    rows = fin.aging_report(load_charges(db), today, prop.grace_days if prop else 0)
    return [(leases[a.lease_id], a) for a in rows if a.total_minor > 0]
