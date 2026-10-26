from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.leases import lease_status
from ..models import Lease, LeaseParty, Property, Unit, User
from ..security import current_user, require_role, verify_csrf
from ..services.audit import audit
from .common import flash, int_in, money_in, redirect, render

router = APIRouter(prefix="/units", dependencies=[Depends(verify_csrf)])
UNIT_TYPES = {"apartment": "Departamento", "commercial": "Local comercial", "office": "Oficina",
              "storage": "Depósito", "parking": "Cochera", "other": "Otro"}


@router.get("")
def list_units(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from datetime import date
    today = date.today()
    units = db.scalars(select(Unit).options(
        selectinload(Unit.leases).selectinload(Lease.parties).selectinload(LeaseParty.tenant))
        .order_by(Unit.floor, Unit.unit_number)).all()
    rows = []
    for u in units:
        current = next((l for l in u.leases
                        if lease_status(l.start_date, l.end_date, today, terminated_at=l.terminated_at)
                        in ("active", "expiring_soon", "expired") and not l.terminated_at), None)
        rows.append((u, current, lease_status(current.start_date, current.end_date, today) if current else None))
    return render(request, "units/list.html", rows=rows, types=UNIT_TYPES)


@router.get("/new")
def new_unit(request: Request, user: User = Depends(require_role("manager"))):
    return render(request, "units/form.html", unit=None, types=UNIT_TYPES)


@router.get("/{unit_id}/edit")
def edit_unit(unit_id: int, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_role("manager"))):
    return render(request, "units/form.html", unit=db.get(Unit, unit_id), types=UNIT_TYPES)


@router.post("")
@router.post("/{unit_id}")
async def save_unit(request: Request, unit_id: int | None = None, db: Session = Depends(get_db),
                    user: User = Depends(require_role("manager"))):
    f = await request.form()
    unit = db.get(Unit, unit_id) if unit_id else Unit(property_id=db.scalar(select(Property.id)))
    try:
        unit.unit_number = str(f["unit_number"]).strip()
        unit.floor = int_in(f.get("floor"))
        unit.size_sqm = f.get("size_sqm") or None
        unit.unit_type = f.get("unit_type", "apartment")
        unit.baseline_rent_minor = money_in(f.get("baseline_rent"))
        unit.condo_fee_minor = money_in(f.get("condo_fee"))
        unit.is_active = f.get("is_active") == "on"
        unit.notes = f.get("notes") or None
        db.add(unit)
        db.flush()
        audit(db, user.id, "unit.save", "unit", unit.id, unit_number=unit.unit_number)
        db.commit()
    except (ValueError, KeyError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return render(request, "units/form.html", unit=unit, types=UNIT_TYPES, status_code=422)
    except IntegrityError:
        db.rollback()
        flash(request, f"Ya existe la unidad {f.get('unit_number')}", "error")
        return render(request, "units/form.html", unit=None, types=UNIT_TYPES, status_code=422)
    flash(request, f"Unidad {unit.unit_number} guardada")
    return redirect("/units")
