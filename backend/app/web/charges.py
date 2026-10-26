from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user, require_role, verify_csrf
from ..services.billing_service import generate_charges
from ..services.reports_service import dashboard
from .common import date_in, flash, redirect, render

router = APIRouter(prefix="/charges", dependencies=[Depends(verify_csrf)])


@router.get("")
def rent_roll(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = dashboard(db, date.today())
    return render(request, "charges/roll.html", **data)


@router.post("/generate")
async def generate(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("manager"))):
    month = (await request.form()).get("period")          # 'YYYY-MM' from <input type=month>
    period = date_in(f"{month}-01") if month else date.today()
    n = generate_charges(db, period, user.id)
    flash(request, f"{n} cargo(s) generados para {period:%m/%Y}" if n else
          f"No había cargos nuevos para {period:%m/%Y} (ya estaban emitidos)")
    return redirect("/charges")
