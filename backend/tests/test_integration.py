"""End-to-end tests against a real PostgreSQL (schema constraints included)."""
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.billing import add_months, month_start
from app.models import (BankAccount, BankTransaction, Charge, Lease, Payment, TenantPayerAlias, Unit)
from app.seed import demo_statement
from app.services import billing_service, lease_service, reconciliation_service as recon

TODAY = date.today()


def _import(db):
    acct = db.scalar(select(BankAccount))
    return recon.import_statement(db, acct, demo_statement(), "extracto.csv", None)


def _txn(db, needle):
    return db.scalar(select(BankTransaction).where(BankTransaction.description.contains(needle)))


def _balance(db, lease_id):
    return sum(c.outstanding_minor for c in db.scalars(
        select(Charge).where(Charge.lease_id == lease_id, Charge.voided_at.is_(None))))


def test_import_dedup_and_suggestions(db):
    imp = _import(db)
    assert imp.rows_imported == 10 and imp.rows_error == 0
    with pytest.raises(recon.DuplicateImport):
        _import(db)
    t = _txn(db, "CM-101")
    assert t.status == "suggested" and t.suggestions[0].confidence == "high"
    assert _txn(db, "EFECTIVO").status == "unmatched"          # ambiguous: left for a human


def test_confirm_allocates_fifo_and_learns_alias(db):
    _import(db)
    t = _txn(db, "INVERSIONES COHEN")
    lease_id = t.suggestions[0].lease_id
    before = _balance(db, lease_id)
    recon.confirm(db, t.id, lease_id, None)
    db.expire_all()
    assert _txn(db, "INVERSIONES COHEN").status == "reconciled"
    assert _balance(db, lease_id) == before - 224000
    aliases = db.scalars(select(TenantPayerAlias.alias_normalized)).all()
    assert "inversiones cohen" in aliases


def test_bulk_confirm_high_then_undo(db):
    _import(db)
    n = recon.confirm_all_high(db, None)
    assert n == 5
    t = _txn(db, "CM-101")
    recon.unreconcile(db, t.id, None)
    db.expire_all()
    t = _txn(db, "CM-101")
    assert t.status == "suggested" and db.scalar(select(Payment).where(Payment.bank_transaction_id == t.id)) is None


def test_overpayment_becomes_credit_and_is_applied_next_month(db):
    lease = db.scalar(select(Lease).join(Unit).where(Unit.unit_number == "101"))
    owed = _balance(db, lease.id)
    recon.record_manual_payment(db, lease.id, owed + lease.monthly_total_minor, TODAY, "cash", None, None)
    assert _balance(db, lease.id) == 0
    created = billing_service.generate_charges(db, add_months(month_start(TODAY), 1))
    assert created > 0
    db.expire_all()
    assert _balance(db, lease.id) == 0                          # next month prepaid by the credit


def test_billing_is_idempotent(db):
    assert billing_service.generate_charges(db, TODAY) == 0     # seed already billed this month
    n1 = billing_service.generate_charges(db, add_months(TODAY, 1))
    assert n1 > 0 and billing_service.generate_charges(db, add_months(TODAY, 1)) == 0


def test_overlapping_lease_rejected_by_app_and_db(db):
    lease = db.scalar(select(Lease).join(Unit).where(Unit.unit_number == "101"))
    with pytest.raises(lease_service.LeaseError):
        lease_service.create_lease(db, unit_id=lease.unit_id, primary_tenant_id=lease.parties[0].tenant_id,
                                   start_date=lease.start_date, end_date=lease.end_date,
                                   monthly_rent_minor=1, monthly_condo_fee_minor=0)
    with pytest.raises(IntegrityError):
        db.execute(text("INSERT INTO leases (unit_id, payment_reference, start_date, end_date, monthly_rent_minor) "
                        "VALUES (:u, 'X-1', :s, :e, 1)"), {"u": lease.unit_id, "s": lease.start_date, "e": lease.end_date})
        db.flush()
    db.rollback()


def test_db_trigger_blocks_over_allocation(db):
    charge = db.scalar(select(Charge).order_by(Charge.id))
    lease_id = charge.lease_id
    with pytest.raises(DBAPIError):
        pid = db.execute(text("INSERT INTO payments (lease_id, received_date, amount_minor) VALUES (:l, now(), 100) "
                              "RETURNING id"), {"l": lease_id}).scalar()
        db.execute(text("INSERT INTO payment_allocations (payment_id, charge_id, amount_minor) VALUES (:p, :c, 500)"),
                   {"p": pid, "c": charge.id})
        db.commit()
    db.rollback()


def test_extend_and_terminate(db):
    lease = db.scalar(select(Lease).join(Unit).where(Unit.unit_number == "201"))
    old_end = lease.end_date
    lease_service.extend_lease(db, lease, new_end_date=add_months(old_end, 12), new_rent_minor=160000)
    assert lease.end_date == add_months(old_end, 12) and lease.monthly_rent_minor == 160000
    billing_service.generate_charges(db, add_months(TODAY, 1))
    voided = lease_service.terminate_lease(db, lease, termination_date=TODAY, reason="mutuo acuerdo")
    assert voided == 2   # next month's rent + condo voided


def test_expense_from_debit(db):
    _import(db)
    t = _txn(db, "ELECTRICIDAD")
    cat = db.execute(text("SELECT id FROM expense_categories WHERE code='utilities'")).scalar()
    prop = db.execute(text("SELECT id FROM properties")).scalar()
    exp = recon.record_expense_from_txn(db, t.id, prop, cat, "", None, None, None)
    assert exp.amount_minor == 32055 and _txn(db, "ELECTRICIDAD").status == "expense"


def test_confirmation_audit_records_matcher_prediction(db):
    _import(db)
    recon.confirm_all_high(db, None)
    rows = db.execute(text("SELECT details FROM audit_log WHERE action = 'reconcile.confirm'")).scalars().all()
    assert len(rows) == 5
    assert all(r["suggested_confidence"] == "high" and r["suggested_lease_id"] == r["lease_id"] for r in rows)
