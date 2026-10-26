from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import reconciliation_service as recon
from ..services.reports_service import dashboard as build_dashboard
from .common import render

router = APIRouter()


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    today = date.today()
    data = build_dashboard(db, today)
    if data["property"] is None:
        return render(request, "dashboard.html", empty=True)
    max_abs = max([abs(r.noi_minor) for r in data["series"]] + [abs(r.revenue_minor) for r in data["series"]] + [1])
    return render(request, "dashboard.html", empty=False, queue=recon.queue_counts(db), chart_max=max_abs, **data)
