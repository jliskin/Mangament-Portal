from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.text import normalize
from ..models import Lease, LeaseParty, Tenant, TenantPayerAlias, User
from ..security import current_user, require_role, verify_csrf
from ..services.audit import audit
from .common import flash, redirect, render

router = APIRouter(prefix="/tenants", dependencies=[Depends(verify_csrf)])
FIELDS = ("full_name", "national_id", "phone", "email", "emergency_contact_name", "emergency_contact_phone", "notes")


@router.get("")
def list_tenants(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(Tenant).options(selectinload(Tenant.lease_links).selectinload(LeaseParty.lease)
                                  .selectinload(Lease.unit)).order_by(Tenant.full_name)
    if q:
        stmt = stmt.where(Tenant.full_name.ilike(f"%{q}%"))
    return render(request, "tenants/list.html", tenants=db.scalars(stmt).all(), q=q)


@router.get("/new")
def new_tenant(request: Request, user: User = Depends(require_role("manager"))):
    return render(request, "tenants/form.html", tenant=None)


@router.get("/{tenant_id}")
def tenant_detail(tenant_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    t = db.scalar(select(Tenant).options(selectinload(Tenant.aliases), selectinload(Tenant.lease_links)
                                         .selectinload(LeaseParty.lease).selectinload(Lease.unit))
                  .where(Tenant.id == tenant_id))
    return render(request, "tenants/form.html", tenant=t)


@router.post("")
@router.post("/{tenant_id}")
async def save_tenant(request: Request, tenant_id: int | None = None, db: Session = Depends(get_db),
                      user: User = Depends(require_role("manager"))):
    f = await request.form()
    t = db.get(Tenant, tenant_id) if tenant_id else Tenant()
    for k in FIELDS:
        setattr(t, k, (str(f.get(k) or "").strip() or None))
    if not t.full_name:
        flash(request, "El nombre es obligatorio", "error")
        return render(request, "tenants/form.html", tenant=t, status_code=422)
    db.add(t)
    db.flush()
    audit(db, user.id, "tenant.save", "tenant", t.id)
    db.commit()
    flash(request, f"Inquilino {t.full_name} guardado")
    return redirect(f"/tenants/{t.id}")


@router.post("/{tenant_id}/aliases")
async def add_alias(tenant_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_role("manager"))):
    alias = normalize(str((await request.form()).get("alias", "")))
    if alias:
        db.execute(insert(TenantPayerAlias).values(tenant_id=tenant_id, alias_normalized=alias, source="manual")
                   .on_conflict_do_nothing())
        db.commit()
        flash(request, f"Alias '{alias}' agregado: los pagos con ese nombre se asociarán a este inquilino")
    return redirect(f"/tenants/{tenant_id}")


@router.post("/{tenant_id}/aliases/{alias_id}/delete")
def delete_alias(tenant_id: int, alias_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_role("manager"))):
    a = db.get(TenantPayerAlias, alias_id)
    if a and a.tenant_id == tenant_id:
        db.delete(a)
        db.commit()
    return redirect(f"/tenants/{tenant_id}")
