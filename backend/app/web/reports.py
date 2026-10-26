import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.financials import AGING_BUCKETS
from ..domain.money import from_minor
from ..models import ExpenseCategory, User
from ..security import current_user
from ..services import reports_service as rs
from .common import date_in, render

router = APIRouter(prefix="/reports")


def _range(start: str, end: str) -> tuple[date, date]:
    today = date.today()
    return date_in(start) or date(today.year, 1, 1), date_in(end) or today


@router.get("")
def reports(request: Request, start: str = "", end: str = "", db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    s, e = _range(start, end)
    today = date.today()
    pnl = rs.unit_pnl(db, s, e, today)
    cat_names = {c.code: c.name for c in db.scalars(select(ExpenseCategory))}
    return render(request, "reports/index.html", start=s, end=e, cat_names=cat_names, accrual=rs.noi(db, s, e, "accrual"),
                  cash=rs.noi(db, s, e, "cash"), pnl=pnl, aging=rs.aging(db, today), buckets=AGING_BUCKETS,
                  pnl_total={k: sum(getattr(r, k) for r in pnl) for k in
                             ("revenue_minor", "collected_minor", "direct_opex_minor", "allocated_opex_minor",
                              "noi_minor", "outstanding_minor")})


def _csv(name: str, header: list[str], rows: list[list]) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/pnl.csv")
def pnl_csv(start: str = "", end: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    s, e = _range(start, end)
    rows = [[r.unit_number, from_minor(r.revenue_minor), from_minor(r.collected_minor),
             from_minor(r.direct_opex_minor), from_minor(r.allocated_opex_minor), from_minor(r.noi_minor),
             from_minor(r.outstanding_minor)] for r in rs.unit_pnl(db, s, e, date.today())]
    return _csv(f"pnl_unidades_{s}_{e}.csv", ["Unidad", "Facturado", "Cobrado", "Gasto directo",
                                              "Gasto común asignado", "NOI", "Pendiente"], rows)


@router.get("/aging.csv")
def aging_csv(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = []
    for lease, a in rs.aging(db, date.today()):
        t = lease.primary_tenant
        rows.append([lease.unit.unit_number, t.full_name if t else "", lease.payment_reference]
                    + [from_minor(a.buckets[k]) for k, _ in AGING_BUCKETS] + [from_minor(a.total_minor)])
    return _csv(f"morosidad_{date.today()}.csv", ["Unidad", "Inquilino", "Código"]
                + [label for _, label in AGING_BUCKETS] + ["Total"], rows)
