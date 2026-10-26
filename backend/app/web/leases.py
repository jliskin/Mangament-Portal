from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Charge, Document, Lease, LeaseParty, Payment, Property, Tenant, Unit, User
from ..security import current_user, require_role, verify_csrf
from ..services import lease_service, reconciliation_service as recon
from ..services.billing_service import generate_charges
from ..services.reports_service import leases_with_status
from ..services.storage import UploadRejected, document_path, save_upload
from .common import date_in, flash, int_in, money_in, redirect, render

router = APIRouter(dependencies=[Depends(verify_csrf)])
PDF_IMG = {"application/pdf", "image/png", "image/jpeg"}


@router.get("/leases")
def list_leases(request: Request, status: str = "", db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    rows = leases_with_status(db, date.today())
    if status:
        rows = [r for r in rows if r[1] == status]
    return render(request, "leases/list.html", rows=rows, status=status)


def _form_ctx(db: Session):
    return {"units": db.scalars(select(Unit).where(Unit.is_active.is_(True)).order_by(Unit.unit_number)).all(),
            "tenants": db.scalars(select(Tenant).order_by(Tenant.full_name)).all()}


@router.get("/leases/new")
def new_lease(request: Request, unit_id: int | None = None, db: Session = Depends(get_db),
              user: User = Depends(require_role("manager"))):
    return render(request, "leases/form.html", unit_id=unit_id, **_form_ctx(db))


async def _upload(request: Request, db: Session, field: str, kind: str, user: User) -> int | None:
    f = await request.form()
    up = f.get(field)
    if up is None or not getattr(up, "filename", ""):
        return None
    content = await up.read()
    if not content:
        return None
    return save_upload(db, content, up.filename, kind, user.id, allowed=PDF_IMG).id


@router.post("/leases")
async def create_lease(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    f = await request.form()
    try:
        doc_id = await _upload(request, db, "contract_file", "lease_contract", user)
        unit = db.get(Unit, int(f["unit_id"]))
        rent = money_in(f.get("monthly_rent")) if f.get("monthly_rent") else unit.baseline_rent_minor
        condo = money_in(f.get("monthly_condo_fee")) if f.get("monthly_condo_fee") else unit.condo_fee_minor
        lease = lease_service.create_lease(
            db, unit_id=unit.id, primary_tenant_id=int(f["tenant_id"]),
            co_tenant_ids=[int(x) for x in f.getlist("co_tenant_ids") if x],
            start_date=date_in(f["start_date"]), end_date=date_in(f["end_date"]),
            entry_date=date_in(f.get("entry_date")), signed_date=date_in(f.get("signed_date")),
            monthly_rent_minor=rent, monthly_condo_fee_minor=condo, due_day=int(f.get("due_day") or 5),
            security_deposit_minor=money_in(f.get("security_deposit")),
            has_renewal_option=f.get("has_renewal_option") == "on",
            renewal_option_months=int_in(f.get("renewal_option_months")),
            renewal_notice_days=int_in(f.get("renewal_notice_days")),
            contract_document_id=doc_id, notes=f.get("notes") or None, user_id=user.id)
    except (ValueError, KeyError, UploadRejected, lease_service.LeaseError) as exc:
        db.rollback()
        flash(request, str(exc) or "Datos incompletos", "error")
        return render(request, "leases/form.html", unit_id=int_in(f.get("unit_id")), status_code=422, **_form_ctx(db))
    # Bill the current month right away if the lease is already running.
    today = date.today()
    if lease.start_date <= today:
        generate_charges(db, today, user.id, lease_ids=[lease.id])
    flash(request, f"Contrato creado. Código de pago para transferencias: {lease.payment_reference}")
    return redirect(f"/leases/{lease.id}")


@router.get("/leases/{lease_id}")
def lease_detail(lease_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lease = db.scalar(select(Lease).options(
        selectinload(Lease.unit), selectinload(Lease.parties).selectinload(LeaseParty.tenant),
        selectinload(Lease.extensions), selectinload(Lease.contract_document)).where(Lease.id == lease_id))
    if not lease:
        raise HTTPException(404)
    charges = db.scalars(select(Charge).options(selectinload(Charge.allocations))
                         .where(Charge.lease_id == lease_id).order_by(Charge.period.desc(), Charge.charge_type)).all()
    payments = db.scalars(select(Payment).options(selectinload(Payment.allocations))
                          .where(Payment.lease_id == lease_id).order_by(Payment.received_date.desc())).all()
    prop = db.scalar(select(Property))
    status = next((st for l, st in leases_with_status(db, date.today()) if l.id == lease_id), "active")
    live = [c for c in charges if c.voided_at is None]
    return render(request, "leases/detail.html", lease=lease, charges=charges, payments=payments, status=status,
                  balance=sum(c.outstanding_minor for c in live),
                  credit=sum(p.unapplied_minor for p in payments), today=date.today(), prop=prop)


@router.post("/leases/{lease_id}/extend")
async def extend(lease_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_role("manager"))):
    f = await request.form()
    lease = db.get(Lease, lease_id)
    try:
        doc_id = await _upload(request, db, "extension_file", "lease_extension", user)
        lease_service.extend_lease(
            db, lease, new_end_date=date_in(f["new_end_date"]),
            new_rent_minor=money_in(f["new_rent"]) if f.get("new_rent") else None,
            new_condo_fee_minor=money_in(f["new_condo_fee"]) if f.get("new_condo_fee") else None,
            signed_date=date_in(f.get("signed_date")), document_id=doc_id, notes=f.get("notes") or None,
            user_id=user.id)
        flash(request, "Contrato extendido")
    except (ValueError, KeyError, UploadRejected, lease_service.LeaseError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/leases/{lease_id}")


@router.post("/leases/{lease_id}/terminate")
async def terminate(lease_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_role("manager"))):
    f = await request.form()
    try:
        voided = lease_service.terminate_lease(
            db, db.get(Lease, lease_id), termination_date=date_in(f["termination_date"]),
            reason=str(f.get("reason") or "").strip() or "sin especificar", exit_date=date_in(f.get("exit_date")),
            deposit_returned_minor=money_in(f.get("deposit_returned")), user_id=user.id)
        flash(request, f"Contrato rescindido. {voided} cargo(s) futuro(s) anulados.")
    except (ValueError, KeyError, lease_service.LeaseError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/leases/{lease_id}")


@router.post("/leases/{lease_id}/payments")
async def manual_payment(lease_id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_role("manager"))):
    f = await request.form()
    try:
        recon.record_manual_payment(db, lease_id, money_in(f.get("amount")), date_in(f.get("received_date")),
                                    str(f.get("method") or "cash"), f.get("notes") or None, user.id)
        flash(request, "Pago registrado y aplicado a la deuda más antigua")
    except (ValueError, recon.ReconciliationError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect(f"/leases/{lease_id}")


@router.get("/documents/{doc_id}")
def download(doc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doc = db.get(Document, doc_id)
    if not doc or not document_path(doc).exists():
        raise HTTPException(404)
    return FileResponse(document_path(doc), media_type=doc.mime_type, filename=doc.original_filename)
