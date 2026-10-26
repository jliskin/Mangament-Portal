import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import authenticate, verify_csrf
from .common import flash, redirect, render

router = APIRouter()

# Brute-force throttle: 10 failed attempts per IP per 15 minutes (in-process;
# a single-instance deployment is the target, see docs/ARCHITECTURE.md).
_FAILS: dict[str, deque] = defaultdict(deque)
WINDOW_S, MAX_FAILS = 900, 10


def _throttled(ip: str) -> bool:
    q = _FAILS[ip]
    while q and q[0] < time.time() - WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILS


@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html", currency="")


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _throttled(ip):
        flash(request, "Demasiados intentos. Espere 15 minutos.", "error")
        return render(request, "login.html", status_code=429, currency="")
    user = authenticate(db, email, password)
    if not user:
        _FAILS[ip].append(time.time())
        flash(request, "Credenciales inválidas", "error")
        return render(request, "login.html", status_code=401, currency="")
    request.session.clear()  # prevent session fixation
    request.session["uid"] = user.id
    return redirect("/")


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return redirect("/login")
