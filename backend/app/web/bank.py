"""Bank import + reconciliation workbench (+ machine-to-machine bank feed webhook)."""
import json
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_db
from ..domain.bank_parser import StatementParseError
from ..models import BankAccount, BankImport, BankTransaction, ExpenseCategory, Lease, LeaseParty, Property, Unit, User
from ..security import current_user, require_role, verify_csrf
from ..services import bank_feed, reconciliation_service as recon
from ..services.storage import UploadRejected
from .common import flash, int_in, redirect, render

router = APIRouter(prefix="/bank", dependencies=[Depends(verify_csrf)])
api = APIRouter(prefix="/api/bank-feed")


def _all_leases(db: Session):
    return db.scalars(select(Lease).options(selectinload(Lease.unit), selectinload(Lease.parties)
                                            .selectinload(LeaseParty.tenant))
                      .where(Lease.terminated_at.is_(None)).join(Unit).order_by(Unit.unit_number)).all()


@router.get("")
def workbench(request: Request, status: str = "open", db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    rows = recon.workbench_rows(db, status)
    imports = db.scalars(select(BankImport).order_by(BankImport.imported_at.desc()).limit(10)).all()
    high = sum(1 for t in rows if t.status == "suggested" and t.suggestions and t.suggestions[0].confidence == "high")
    return render(request, "bank/workbench.html", rows=rows, status=status, imports=imports, high=high,
                  accounts=db.scalars(select(BankAccount)).all(), leases=_all_leases(db),
                  counts=recon.queue_counts(db), debits=len(recon.pending_debits(db)))


@router.post("/import")
async def import_file(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    form = await request.form()
    up = form.get("file")
    account = db.get(BankAccount, int_in(form.get("bank_account_id")) or 0)
    if not account or up is None or not getattr(up, "filename", ""):
        flash(request, "Seleccione cuenta y archivo", "error")
        return redirect("/bank")
    try:
        imp = recon.import_statement(db, account, await up.read(), up.filename, user.id)
    except recon.DuplicateImport as exc:
        flash(request, str(exc), "error")
        return redirect("/bank")
    except (StatementParseError, UploadRejected) as exc:
        db.rollback()
        flash(request, f"No se pudo leer el extracto: {exc}", "error")
        return redirect("/bank")
    r = imp.parse_report
    flash(request, f"Importadas {imp.rows_imported} filas nuevas ({imp.rows_duplicate} duplicadas, "
                   f"{imp.rows_error} con error). Columnas detectadas: {', '.join(r['columns'].values())}; "
                   f"decimal '{r['decimal_separator']}', fechas {r['date_order']}.",
          "error" if imp.rows_error else "ok")
    return redirect("/bank")


def _row(request: Request, db: Session, txn_id: int):
    txn = db.scalar(select(BankTransaction).options(
        selectinload(BankTransaction.suggestions), selectinload(BankTransaction.payment))
        .where(BankTransaction.id == txn_id))
    return render(request, "partials/txn_row.html", t=txn, leases=_all_leases(db))


@router.post("/txn/{txn_id}/confirm")
async def confirm(txn_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_role("manager"))):
    lease_id = int_in((await request.form()).get("lease_id"))
    try:
        if not lease_id:
            raise recon.ReconciliationError("Seleccione un contrato")
        recon.confirm(db, txn_id, lease_id, user.id)
    except recon.ReconciliationError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    if request.headers.get("hx-request"):
        resp = _row(request, db, txn_id)
        resp.headers["HX-Trigger"] = "reconciled"
        return resp
    return redirect("/bank")


@router.post("/confirm-high")
def confirm_high(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    n = recon.confirm_all_high(db, user.id)
    flash(request, f"{n} movimiento(s) de alta confianza conciliados")
    return redirect("/bank")


@router.post("/txn/{txn_id}/ignore")
def ignore(txn_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    try:
        recon.ignore(db, txn_id, user.id, "manual")
    except recon.ReconciliationError as exc:
        flash(request, str(exc), "error")
    return _row(request, db, txn_id) if request.headers.get("hx-request") else redirect("/bank")


@router.post("/txn/{txn_id}/undo")
def undo(txn_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    recon.unreconcile(db, txn_id, user.id)
    return _row(request, db, txn_id) if request.headers.get("hx-request") else redirect(request.headers.get("referer", "/bank"))


@router.post("/rematch")
def rematch(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    n = recon.run_matching(db)
    db.commit()
    flash(request, f"Sugerencias recalculadas ({n} movimientos con candidato)")
    return redirect("/bank")


@router.get("/debits")
def debits(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "bank/debits.html", rows=recon.pending_debits(db),
                  categories=db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.name)).all(),
                  units=db.scalars(select(Unit).order_by(Unit.unit_number)).all())


@router.post("/txn/{txn_id}/expense")
async def to_expense(txn_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_role("manager"))):
    f = await request.form()
    try:
        recon.record_expense_from_txn(db, txn_id, db.scalar(select(Property.id)), int(f["category_id"]),
                                      str(f.get("description") or ""), int_in(f.get("unit_id")), None, user.id)
        flash(request, "Gasto registrado desde el extracto")
    except (ValueError, KeyError, recon.ReconciliationError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect("/bank/debits")


@router.post("/txn/{txn_id}/ignore-debit")
def ignore_debit(txn_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_role("manager"))):
    recon.ignore(db, txn_id, user.id, "no operativo")
    return redirect("/bank/debits")


# ---------------------------------------------------------------- webhook
@api.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Push endpoint for an Open-Banking aggregator. Body:
    {"bank_account_id": 1, "transactions": [{"external_id": "...", "date": "2026-09-01",
      "amount": "1450.00", "description": "...", "counterparty": "...", "reference": "..."}]}"""
    body = await request.body()
    if not bank_feed.verify_signature(get_settings().bank_webhook_secret, request.headers.get("x-signature"), body):
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(body)
        account = db.get(BankAccount, int(payload["bank_account_id"]))
        items = [bank_feed.FeedTransaction(str(t["external_id"]), date.fromisoformat(t["date"]),
                                           Decimal(str(t["amount"])), t.get("description", ""),
                                           t.get("counterparty"), t.get("reference"))
                 for t in payload["transactions"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, f"invalid payload: {exc}") from exc
    if not account:
        raise HTTPException(404, "unknown account")
    parsed, ext = bank_feed.to_parsed(account.id, items)
    imp = BankImport(bank_account_id=account.id, source="api", rows_total=len(parsed))
    db.add(imp)
    db.flush()
    imp.rows_imported = recon.ingest(db, account, parsed, imp.id, ext)
    imp.rows_duplicate = len(parsed) - imp.rows_imported
    recon.run_matching(db)
    db.commit()
    return {"received": len(parsed), "inserted": imp.rows_imported}

