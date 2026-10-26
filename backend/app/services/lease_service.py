"""Lease administration: create, extend/renew, terminate."""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Lease, LeaseExtension, LeaseParty, Unit
from .audit import audit
from .billing_service import void_charges_after


class LeaseError(ValueError):
    pass


def next_payment_reference(db: Session, unit: Unit) -> str:
    base = "CM-" + re.sub(r"[^A-Za-z0-9]", "", unit.unit_number).upper()
    ref, n = base, 2
    while db.scalar(select(Lease.id).where(Lease.payment_reference == ref)):
        ref, n = f"{base}-{n}", n + 1
    return ref


def _overlaps(db: Session, unit_id: int, start: date, end: date, exclude_id: int | None = None) -> Lease | None:
    for l in db.scalars(select(Lease).where(Lease.unit_id == unit_id)):
        if l.id == exclude_id:
            continue
        l_end = l.terminated_at or l.exit_date or l.end_date
        if l.start_date <= end and start <= l_end:
            return l
    return None


def create_lease(db: Session, *, unit_id: int, primary_tenant_id: int, start_date: date, end_date: date,
                 monthly_rent_minor: int, monthly_condo_fee_minor: int, due_day: int = 5,
                 security_deposit_minor: int = 0, entry_date: date | None = None, signed_date: date | None = None,
                 has_renewal_option: bool = False, renewal_option_months: int | None = None,
                 renewal_notice_days: int | None = None, co_tenant_ids: list[int] | None = None,
                 contract_document_id: int | None = None, notes: str | None = None,
                 user_id: int | None = None) -> Lease:
    if end_date < start_date:
        raise LeaseError("La fecha de vencimiento es anterior al inicio")
    unit = db.get(Unit, unit_id)
    if not unit:
        raise LeaseError("Unidad inexistente")
    clash = _overlaps(db, unit_id, start_date, end_date)
    if clash:
        raise LeaseError(f"La unidad {unit.unit_number} ya tiene un contrato en esas fechas "
                         f"({clash.start_date:%d/%m/%Y} – {clash.end_date:%d/%m/%Y})")
    lease = Lease(unit_id=unit_id, payment_reference=next_payment_reference(db, unit), signed_date=signed_date,
                  start_date=start_date, entry_date=entry_date, end_date=end_date,
                  monthly_rent_minor=monthly_rent_minor, monthly_condo_fee_minor=monthly_condo_fee_minor,
                  due_day=due_day, security_deposit_minor=security_deposit_minor,
                  has_renewal_option=has_renewal_option, renewal_option_months=renewal_option_months,
                  renewal_notice_days=renewal_notice_days, contract_document_id=contract_document_id, notes=notes)
    lease.parties.append(LeaseParty(tenant_id=primary_tenant_id, role="primary"))
    for tid in co_tenant_ids or []:
        if tid != primary_tenant_id:
            lease.parties.append(LeaseParty(tenant_id=tid, role="co_tenant"))
    db.add(lease)
    try:
        db.flush()
    except IntegrityError as exc:  # DB exclusion constraint is the last line of defence
        db.rollback()
        raise LeaseError("Conflicto de fechas con otro contrato de la unidad") from exc
    audit(db, user_id, "lease.create", "lease", lease.id, unit=unit.unit_number)
    db.commit()
    return lease


def extend_lease(db: Session, lease: Lease, *, new_end_date: date, new_rent_minor: int | None = None,
                 new_condo_fee_minor: int | None = None, signed_date: date | None = None,
                 document_id: int | None = None, notes: str | None = None, user_id: int | None = None) -> LeaseExtension:
    if lease.terminated_at:
        raise LeaseError("No se puede extender un contrato rescindido")
    if new_end_date <= lease.end_date:
        raise LeaseError("La nueva fecha debe ser posterior al vencimiento actual")
    clash = _overlaps(db, lease.unit_id, lease.start_date, new_end_date, exclude_id=lease.id)
    if clash:
        raise LeaseError("La extensión se superpone con otro contrato de la unidad")
    ext = LeaseExtension(
        lease_id=lease.id, previous_end_date=lease.end_date, new_end_date=new_end_date,
        previous_rent_minor=lease.monthly_rent_minor,
        new_rent_minor=new_rent_minor if new_rent_minor is not None else lease.monthly_rent_minor,
        previous_condo_fee_minor=lease.monthly_condo_fee_minor,
        new_condo_fee_minor=new_condo_fee_minor if new_condo_fee_minor is not None else lease.monthly_condo_fee_minor,
        signed_date=signed_date, document_id=document_id, notes=notes, created_by=user_id)
    db.add(ext)
    # New terms apply to charges generated from now on; already-issued charges are untouched.
    lease.end_date, lease.monthly_rent_minor, lease.monthly_condo_fee_minor = (
        new_end_date, ext.new_rent_minor, ext.new_condo_fee_minor)
    db.flush()
    audit(db, user_id, "lease.extend", "lease", lease.id, new_end=new_end_date.isoformat(),
          rent=ext.new_rent_minor, condo=ext.new_condo_fee_minor)
    db.commit()
    return ext


def terminate_lease(db: Session, lease: Lease, *, termination_date: date, reason: str,
                    exit_date: date | None = None, deposit_returned_minor: int = 0,
                    user_id: int | None = None) -> int:
    if termination_date < lease.start_date:
        raise LeaseError("La rescisión no puede ser anterior al inicio del contrato")
    if deposit_returned_minor > lease.security_deposit_minor:
        raise LeaseError("La devolución supera el depósito en garantía")
    lease.terminated_at = termination_date
    lease.termination_reason = reason
    lease.exit_date = exit_date or termination_date
    lease.deposit_returned_minor = deposit_returned_minor
    voided = void_charges_after(db, lease, termination_date, f"Rescisión: {reason}")
    audit(db, user_id, "lease.terminate", "lease", lease.id, date=termination_date.isoformat(), voided=voided)
    db.commit()
    return voided
