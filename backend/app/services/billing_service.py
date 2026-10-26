"""Billing engine persistence: monthly charge generation and credit application."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.allocation import OpenItem, allocate_fifo
from ..domain.billing import LeaseTerms, charges_for_period, month_end, month_start, periods_between
from ..models import Charge, Lease, Payment, PaymentAllocation, Property, Unit
from .audit import audit

log = logging.getLogger(__name__)


def lease_terms(lease: Lease) -> LeaseTerms:
    return LeaseTerms(lease.id, lease.unit_id, lease.start_date, lease.end_date, lease.monthly_rent_minor,
                      lease.monthly_condo_fee_minor, lease.due_day, lease.terminated_at, lease.exit_date)


def generate_charges(db: Session, period: date, user_id: int | None = None,
                     lease_ids: list[int] | None = None) -> int:
    """Create rent + condo charges for every lease billable in `period`.
    Idempotent: re-running for the same month inserts nothing new."""
    period = month_start(period)
    q = select(Lease).where(Lease.start_date <= month_end(period), Lease.end_date >= period,
                            or_(Lease.terminated_at.is_(None), Lease.terminated_at >= period))
    if lease_ids:
        q = q.where(Lease.id.in_(lease_ids))
    created = 0
    touched: set[int] = set()
    for lease in db.scalars(q):
        for d in charges_for_period(lease_terms(lease), period, get_settings().prorate_partial_months):
            stmt = insert(Charge).values(
                lease_id=d.lease_id, unit_id=d.unit_id, charge_type=d.charge_type, period=d.period,
                due_date=d.due_date, amount_minor=d.amount_minor, description=d.description,
            ).on_conflict_do_nothing(
                index_elements=["lease_id", "charge_type", "period"],
                index_where=text("charge_type IN ('rent', 'condo_fee') AND voided_at IS NULL"),
            ).returning(Charge.id)
            if db.execute(stmt).scalar() is not None:
                created += 1
                touched.add(lease.id)
    for lid in touched:
        apply_credits(db, lid)
    if created:
        audit(db, user_id, "billing.generate", "charges", None, period=period.isoformat(), created=created)
    db.commit()
    return created


def backfill(db: Session, first: date, last: date, user_id: int | None = None) -> int:
    return sum(generate_charges(db, p, user_id) for p in periods_between(first, last))


def open_items(db: Session, lease_id: int, lock: bool = False) -> list[OpenItem]:
    q = select(Charge).where(Charge.lease_id == lease_id, Charge.voided_at.is_(None)).order_by(Charge.due_date)
    if lock:
        q = q.with_for_update()
    return [OpenItem(c.id, c.due_date, c.charge_type, c.outstanding_minor)
            for c in db.scalars(q) if c.outstanding_minor > 0]


def apply_payment(db: Session, payment: Payment) -> int:
    """Allocate a payment's unapplied amount FIFO. Returns what remains unapplied (credit)."""
    allocs, remaining = allocate_fifo(payment.unapplied_minor, open_items(db, payment.lease_id, lock=True))
    for charge_id, amount in allocs:
        existing = db.scalar(select(PaymentAllocation).where(
            PaymentAllocation.payment_id == payment.id, PaymentAllocation.charge_id == charge_id))
        if existing:
            existing.amount_minor += amount
        else:
            payment.allocations.append(PaymentAllocation(charge_id=charge_id, amount_minor=amount))
    db.flush()
    return remaining


def apply_credits(db: Session, lease_id: int) -> None:
    """Consume prepayments / overpayments against newly generated charges."""
    for p in db.scalars(select(Payment).where(Payment.lease_id == lease_id).order_by(Payment.received_date)):
        if p.unapplied_minor > 0:
            apply_payment(db, p)


def void_charges_after(db: Session, lease: Lease, last_day: date, reason: str) -> int:
    """On termination: void unpaid recurring charges for months after the lease ends."""
    n = 0
    for c in db.scalars(select(Charge).where(Charge.lease_id == lease.id, Charge.voided_at.is_(None),
                                             Charge.period > last_day)):
        if c.allocated_minor == 0:
            c.voided_at, c.void_reason = datetime.now(timezone.utc), reason
            n += 1
    return n


def run_daily(db: Session, today: date) -> dict:
    """Scheduler entry point. Issues the current month's charges once the
    property's invoice day is reached (idempotent, safe to run many times)."""
    created = 0
    for prop in db.scalars(select(Property)):
        if today.day >= prop.invoice_day:
            ids = list(db.scalars(select(Lease.id).join(Unit).where(Unit.property_id == prop.id)))
            if ids:
                created += generate_charges(db, today, lease_ids=ids)
    log.info("daily billing run %s: %d charges created", today, created)
    return {"charges_created": created}
