from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import BankAccount, Property, User
from ..security import hash_password, require_role, verify_csrf
from ..services.audit import audit
from .common import flash, redirect, render

router = APIRouter(prefix="/settings", dependencies=[Depends(verify_csrf)])


@router.get("")
def settings(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    return render(request, "settings.html", prop=db.scalar(select(Property)),
                  accounts=db.scalars(select(BankAccount)).all(),
                  users=db.scalars(select(User).order_by(User.email)).all())


@router.post("/property")
async def save_property(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    f = await request.form()
    prop = db.scalar(select(Property)) or Property()
    try:
        prop.name = str(f["name"]).strip()
        prop.address = f.get("address") or None
        prop.currency = str(f.get("currency") or "USD").upper()[:3]
        prop.invoice_day = int(f.get("invoice_day") or 1)
        prop.grace_days = int(f.get("grace_days") or 5)
        prop.expiring_soon_days = int(f.get("expiring_soon_days") or 90)
        db.add(prop)
        db.flush()
        audit(db, user.id, "settings.property", "property", prop.id)
        db.commit()
        flash(request, "Configuración guardada")
    except (ValueError, KeyError, IntegrityError) as exc:
        db.rollback()
        flash(request, f"Error: {exc}", "error")
    return redirect("/settings")


@router.post("/bank-accounts")
async def add_account(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    f = await request.form()
    prop = db.scalar(select(Property))
    mask = "".join(ch for ch in str(f.get("account_mask") or "") if ch.isdigit())[-4:]
    db.add(BankAccount(property_id=prop.id, name=str(f["name"]), bank_name=f.get("bank_name") or None,
                       account_mask=mask or None, currency=prop.currency))
    db.commit()
    flash(request, "Cuenta bancaria agregada")
    return redirect("/settings")


@router.post("/users")
async def add_user(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    f = await request.form()
    try:
        db.add(User(email=str(f["email"]).strip().lower(), full_name=str(f["full_name"]).strip(),
                    password_hash=hash_password(str(f["password"])), role=str(f.get("role") or "viewer")))
        db.commit()
        flash(request, "Usuario creado")
    except (ValueError, KeyError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except IntegrityError:
        db.rollback()
        flash(request, "Ese email ya existe", "error")
    return redirect("/settings")
