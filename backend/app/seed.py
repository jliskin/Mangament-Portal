"""Demo dataset: an 8-unit building with realistic edge cases (arrears,
partial payments, an expiring lease, a vacant unit, a renewal option)."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from . import models as m
from .domain.billing import add_months, month_start
from .services import billing_service, lease_service
from .services.reconciliation_service import record_manual_payment

UNITS = [  # number, floor, sqm, rent, condo
    ("101", 1, 62, 145000, 18000), ("102", 1, 58, 140000, 17000), ("201", 2, 62, 150000, 18000),
    ("202", 2, 58, 145000, 17000), ("301", 3, 85, 200000, 24000), ("302", 3, 85, 200000, 24000),
    ("L1", 0, 120, 320000, 30000), ("401", 4, 70, 165000, 20000),
]
TENANTS = ["José Pérez Gómez", "María López Sánchez", "Pedro Ruiz", "Yael Cohen", "Ana Souza",
           "Comercial Andina SRL", "Daniel Levi"]


def seed_demo(db: Session, today: date | None = None) -> None:
    today = today or date.today()
    prop = m.Property(name="Edificio C.M — Yosef Golan", address="(dirección)", currency="USD",
                      invoice_day=1, grace_days=5, expiring_soon_days=90)
    db.add(prop)
    db.flush()
    units = []
    for num, floor, sqm, rent, condo in UNITS:
        u = m.Unit(property_id=prop.id, unit_number=num, floor=floor, size_sqm=sqm,
                   unit_type="commercial" if num.startswith("L") else "apartment",
                   baseline_rent_minor=rent, condo_fee_minor=condo)
        db.add(u)
        units.append(u)
    tenants = [m.Tenant(full_name=n, phone="+00 000 0000", email=f"t{i}@example.com") for i, n in enumerate(TENANTS)]
    db.add_all(tenants)
    db.add(m.BankAccount(property_id=prop.id, name="Cuenta alquileres", bank_name="Banco Demo",
                         account_mask="4821", currency="USD"))
    db.commit()

    start = add_months(month_start(today), -5)
    plans = [  # unit idx, tenant idx, start, end
        (0, 0, start, add_months(start, 24)),
        (1, 1, start, add_months(start, 12)),
        (2, 2, start, add_months(today, 2)),            # expiring soon
        (4, 3, add_months(start, -12), add_months(start, 24)),
        (5, 4, add_months(start, 1), add_months(start, 25)),
        (6, 5, add_months(start, -24), add_months(today, 4)),
        (7, 6, start, add_months(start, 12)),
    ]                                                    # unit 202 stays vacant
    leases = []
    for ui, ti, s, e in plans:
        u = units[ui]
        leases.append(lease_service.create_lease(
            db, unit_id=u.id, primary_tenant_id=tenants[ti].id, start_date=s, end_date=e,
            monthly_rent_minor=u.baseline_rent_minor, monthly_condo_fee_minor=u.condo_fee_minor, due_day=5,
            security_deposit_minor=u.baseline_rent_minor, signed_date=s, entry_date=s,
            has_renewal_option=ui in (2, 6), renewal_option_months=12 if ui in (2, 6) else None,
            renewal_notice_days=60 if ui in (2, 6) else None))
    billing_service.backfill(db, start, today)

    # history: most tenants paid every month except the current one; some arrears
    for i, lease in enumerate(leases):
        for k in range(5):
            period = add_months(start, k)
            if period < lease.start_date.replace(day=1):
                continue
            if i == 2 and k >= 3:          # Pedro: stopped paying 2 months ago
                continue
            amount = lease.monthly_total_minor // 2 if (i == 5 and k == 4) else lease.monthly_total_minor
            record_manual_payment(db, lease.id, amount, period.replace(day=4), "bank_transfer",
                                  "Histórico migrado de Excel", None)

    cats = {c.code: c.id for c in db.query(m.ExpenseCategory)}
    for k in range(6):
        d = add_months(month_start(today), -5 + k).replace(day=10)
        if d > today:
            continue
        db.add_all([
            m.Expense(property_id=prop.id, category_id=cats["utilities"], expense_date=d, amount_minor=32055,
                      description="Electricidad áreas comunes"),
            m.Expense(property_id=prop.id, category_id=cats["cleaning"], expense_date=d, amount_minor=45000,
                      description="Limpieza mensual"),
            m.Expense(property_id=prop.id, category_id=cats["management"], expense_date=d, amount_minor=60000,
                      description="Honorarios administración"),
        ])
    db.add(m.Expense(property_id=prop.id, unit_id=units[2].id, category_id=cats["repairs"],
                     expense_date=add_months(today, -1).replace(day=15), amount_minor=38000,
                     description="Reparación calefón unidad 201"))
    db.add(m.Expense(property_id=prop.id, category_id=cats["capex"], expense_date=add_months(today, -2).replace(day=20),
                     amount_minor=1200000, description="Impermeabilización de terraza"))
    db.commit()


def demo_statement(today: date | None = None) -> bytes:
    """A Spanish-format bank CSV for the current month that exercises every
    matcher path: payment code, reordered name, unit mention, partial payment,
    weak name, amount-only, and debits to classify as expenses."""
    today = today or date.today()
    d = lambda day: today.replace(day=min(day, today.day)).strftime("%d/%m/%Y")  # noqa: E731
    rows = [
        (d(2), "TRANSF. RECIBIDA DE JOSE PEREZ GOMEZ REF CM-101", "1.630,00"),
        (d(3), "TRANSFERENCIA DE LOPEZ SANCHEZ MARIA", "1.570,00"),
        (d(4), "DEPOSITO DE INVERSIONES COHEN LTDA APTO 301", "2.240,00"),
        (d(4), "PIX RECEBIDO DE ANA SOUZA", "2.240,00"),
        (d(5), "TRANSF. RECIBIDA DE PEDRO RUIZ", "1.000,00"),
        (d(6), "TRANSF. RECIBIDA DE D. LEVI", "1.850,00"),
        (d(7), "DEPOSITO EN EFECTIVO SUCURSAL 12", "1.630,00"),
        (d(8), "PAGO SERVICIO ELECTRICIDAD AREAS COMUNES", "-320,55"),
        (d(9), "COMISION MANTENIMIENTO CUENTA", "-12,00"),
        (d(10), "TRANSFERENCIA DE COMERCIAL ANDINA SRL", "1.750,00"),
    ]
    lines = ["Banco Demo S.A. - Extracto de cuenta", "Cuenta: ****4821", "",
             "Fecha;Concepto;Importe;Saldo"]
    bal = 1000000
    for day, desc, amt in rows:
        bal += int(amt.replace(".", "").replace(",", ""))
        balance = f"{bal // 100:,}".replace(",", ".") + f",{bal % 100:02d}"
        lines.append(f"{day};{desc};{amt};{balance}")
    return ("\n".join(lines) + "\n").encode("utf-8")
