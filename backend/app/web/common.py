"""Shared helpers for the server-rendered UI."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..config import get_settings
from ..domain.billing import MONTHS_ES
from ..domain.leases import STATUS_LABELS
from ..domain.money import fmt, to_minor
from ..models import Property
from ..security import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _date(d: date | None, f: str = "%d/%m/%Y") -> str:
    return d.strftime(f) if d else "—"


def _pct(x: float | None, digits: int = 0) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


templates.env.filters.update(
    money=lambda m: fmt(m),
    date=_date,
    pct=_pct,
    month=lambda d: f"{MONTHS_ES[d.month]} {d:%y}",
)
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    CHARGE_LABELS={"rent": "Alquiler", "condo_fee": "Expensas", "late_fee": "Recargo", "utility": "Servicio",
                   "other": "Otro"},
    PAY_LABELS={"paid": "Pagado", "partial": "Parcial", "unpaid": "Impago"},
    METHOD_LABELS={"bank_transfer": "Transferencia", "cash": "Efectivo", "check": "Cheque", "card": "Tarjeta",
                   "other": "Otro"},
    ROLE_LABELS={"primary": "titular", "co_tenant": "co-inquilino", "guarantor": "garante"},
    app_name=get_settings().app_name,
)


def render(request: Request, name: str, status_code: int = 200, **ctx):
    from ..db import session_factory  # local: avoid import cycle at startup

    if "currency" not in ctx:
        with session_factory()() as db:
            prop = db.scalar(select(Property))
            ctx["currency"] = prop.currency if prop else ""
    ctx.update(request=request, user=getattr(request.state, "user", None), csrf=csrf_token(request),
               flashes=request.session.pop("flashes", []))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, level: str = "ok") -> None:
    request.session.setdefault("flashes", []).append({"level": level, "message": message})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def money_in(value: str | None) -> int:
    """Form input -> minor units. Accepts '1450', '1450.50', '1,450.50' and '1.450,50'."""
    s = (value or "").strip().replace(" ", "")
    if not s:
        return 0
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.rsplit(",", 1)[1]) <= 2 else s.replace(",", "")
    try:
        return to_minor(Decimal(s))
    except InvalidOperation as exc:
        raise ValueError(f"Importe inválido: {value}") from exc


def date_in(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def int_in(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None
