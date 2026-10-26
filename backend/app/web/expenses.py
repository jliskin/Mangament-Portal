from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.billing import month_end, month_start
from ..models import Expense, ExpenseCategory, Property, Unit, User, Vendor
from ..security import current_user, require_role, verify_csrf
from ..services.audit import audit
from ..services.storage import UploadRejected, save_upload
from .common import date_in, flash, int_in, money_in, redirect, render

router = APIRouter(prefix="/expenses", dependencies=[Depends(verify_csrf)])


def _ctx(db: Session):
    return {"categories": db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.name)).all(),
            "units": db.scalars(select(Unit).order_by(Unit.unit_number)).all(),
            "vendors": db.scalars(select(Vendor).order_by(Vendor.name)).all()}


@router.get("")
def list_expenses(request: Request, month: str = "", category_id: int | None = None,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    start = date_in(f"{month}-01") if month else month_start(date.today())
    end = month_end(start)
    q = (select(Expense).options(selectinload(Expense.category), selectinload(Expense.unit),
                                 selectinload(Expense.vendor), selectinload(Expense.receipt))
         .where(Expense.expense_date.between(start, end)).order_by(Expense.expense_date.desc()))
    if category_id:
        q = q.where(Expense.category_id == category_id)
    rows = db.scalars(q).all()
    by_cat: dict[str, int] = {}
    for e in rows:
        by_cat[e.category.name] = by_cat.get(e.category.name, 0) + e.amount_minor
    return render(request, "expenses/list.html", rows=rows, month=f"{start:%Y-%m}", category_id=category_id,
                  total=sum(e.amount_minor for e in rows), by_cat=sorted(by_cat.items(), key=lambda x: -x[1]),
                  **_ctx(db))


@router.get("/new")
def new_expense(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    return render(request, "expenses/form.html", today=date.today(), **_ctx(db))


@router.post("")
async def create_expense(request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_role("manager"))):
    f = await request.form()
    try:
        receipt_id = None
        up = f.get("receipt")
        if up is not None and getattr(up, "filename", ""):
            content = await up.read()
            if content:
                receipt_id = save_upload(db, content, up.filename, "receipt", user.id,
                                         allowed={"application/pdf", "image/png", "image/jpeg"}).id
        vendor_id = int_in(f.get("vendor_id"))
        new_vendor = str(f.get("new_vendor") or "").strip()
        if new_vendor:
            vendor_id = db.execute(insert(Vendor).values(name=new_vendor).on_conflict_do_update(
                index_elements=["name"], set_={"name": new_vendor}).returning(Vendor.id)).scalar()
        exp = Expense(property_id=db.scalar(select(Property.id)), unit_id=int_in(f.get("unit_id")),
                      category_id=int(f["category_id"]), vendor_id=vendor_id,
                      expense_date=date_in(f["expense_date"]), amount_minor=money_in(f["amount"]),
                      description=str(f["description"]).strip(), receipt_document_id=receipt_id, created_by=user.id)
        if exp.amount_minor <= 0 or not exp.description:
            raise ValueError("Importe y descripción son obligatorios")
        db.add(exp)
        db.flush()
        audit(db, user.id, "expense.create", "expense", exp.id, amount=exp.amount_minor)
        db.commit()
    except (ValueError, KeyError, UploadRejected) as exc:
        db.rollback()
        flash(request, str(exc) or "Datos incompletos", "error")
        return render(request, "expenses/form.html", today=date.today(), status_code=422, **_ctx(db))
    flash(request, "Gasto registrado")
    return redirect(f"/expenses?month={exp.expense_date:%Y-%m}")


@router.post("/{expense_id}/delete")
def delete_expense(expense_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_role("manager"))):
    exp = db.get(Expense, expense_id)
    if exp and exp.bank_transaction_id:
        flash(request, "Este gasto proviene del extracto: deshaga la conciliación desde Banco", "error")
    elif exp:
        audit(db, user.id, "expense.delete", "expense", exp.id, amount=exp.amount_minor, desc=exp.description)
        db.delete(exp)
        db.commit()
        flash(request, "Gasto eliminado")
    return redirect("/expenses")
