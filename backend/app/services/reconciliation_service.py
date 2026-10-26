"""Bank ingestion + reconciliation workflow (persistence side of domain.matching)."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from ..domain import matching
from ..domain.bank_parser import ParsedTransaction, parse_statement
from ..domain.text import normalize
from ..models import (BankAccount, BankImport, BankTransaction, Charge, Expense, Lease, LeaseParty, Payment,
                      ReconciliationSuggestion, TenantPayerAlias)
from .audit import audit
from .billing_service import apply_payment
from .storage import save_upload


class DuplicateImport(ValueError):
    def __init__(self, existing: BankImport):
        super().__init__(f"Este archivo ya fue importado (importación #{existing.id})")
        self.existing = existing


class ReconciliationError(ValueError):
    pass


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def import_statement(db: Session, account: BankAccount, content: bytes, filename: str,
                     user_id: int | None) -> BankImport:
    sha = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(BankImport).where(BankImport.file_sha256 == sha))
    if existing:
        raise DuplicateImport(existing)
    result = parse_statement(content, filename, account_key=f"acct{account.id}")
    doc = save_upload(db, content, filename, "bank_statement", user_id,
                      allowed={"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
    imp = BankImport(bank_account_id=account.id, source="file", document_id=doc.id, file_sha256=sha,
                     imported_by=user_id)
    db.add(imp)
    db.flush()
    inserted = ingest(db, account, result.transactions, imp.id)
    imp.rows_total = len(result.transactions) + len(result.errors)
    imp.rows_imported = inserted
    imp.rows_duplicate = len(result.transactions) - inserted
    imp.rows_error = len(result.errors)
    imp.parse_report = {**result.report, "errors": [
        {"row": e.row_number, "message": e.message, "raw": e.raw} for e in result.errors[:100]]}
    audit(db, user_id, "bank.import", "bank_import", imp.id, filename=filename, inserted=inserted)
    db.flush()
    run_matching(db)
    db.commit()
    return imp


def ingest(db: Session, account: BankAccount, txns: list[ParsedTransaction], import_id: int | None,
           external_ids: dict[str, str] | None = None) -> int:
    """Insert parsed rows; duplicates (same fingerprint) are skipped silently."""
    inserted = 0
    for t in txns:
        stmt = insert(BankTransaction).values(
            bank_account_id=account.id, import_id=import_id, txn_date=t.txn_date, value_date=t.value_date,
            amount_minor=t.amount_minor, balance_minor=t.balance_minor, description=t.description or "",
            counterparty_name=t.counterparty_name, reference=t.reference, fingerprint=t.fingerprint,
            external_id=(external_ids or {}).get(t.fingerprint), raw=t.raw,
            status="unmatched",
        ).on_conflict_do_nothing(index_elements=["bank_account_id", "fingerprint"]).returning(BankTransaction.id)
        if db.execute(stmt).scalar() is not None:
            inserted += 1
    return inserted


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def load_lease_candidates(db: Session, today: date | None = None) -> list[matching.LeaseCandidate]:
    today = today or date.today()
    leases = db.scalars(select(Lease).options(
        selectinload(Lease.unit),
        selectinload(Lease.parties).selectinload(LeaseParty.tenant),
    )).all()
    charges = db.scalars(select(Charge).options(selectinload(Charge.allocations))
                         .where(Charge.voided_at.is_(None))).all()
    open_by_lease: dict[int, list[matching.OpenCharge]] = defaultdict(list)
    for c in charges:
        if c.outstanding_minor > 0:
            open_by_lease[c.lease_id].append(
                matching.OpenCharge(c.id, c.due_date, c.outstanding_minor, c.charge_type))
    tenant_ids = {p.tenant_id for l in leases for p in l.parties}
    aliases: dict[int, list[str]] = defaultdict(list)
    for a in db.scalars(select(TenantPayerAlias).where(TenantPayerAlias.tenant_id.in_(tenant_ids))):
        aliases[a.tenant_id].append(a.alias_normalized)
    out = []
    for l in leases:
        recent = l.end_date >= today - timedelta(days=180) and (l.terminated_at is None or
                                                                 l.terminated_at >= today - timedelta(days=60))
        if not recent and not open_by_lease.get(l.id):
            continue
        out.append(matching.LeaseCandidate(
            lease_id=l.id, unit_number=l.unit.unit_number, payment_reference=l.payment_reference,
            party_names=[p.tenant.full_name for p in l.parties],
            aliases=[a for p in l.parties for a in aliases.get(p.tenant_id, [])],
            monthly_amount_minor=l.monthly_total_minor, open_charges=open_by_lease.get(l.id, []),
        ))
    return out


def run_matching(db: Session) -> int:
    """(Re)compute suggestions for every open credit. Returns # of suggested txns."""
    txns = db.scalars(select(BankTransaction).where(
        BankTransaction.status.in_(("unmatched", "suggested")), BankTransaction.amount_minor > 0)
        .order_by(BankTransaction.txn_date)).all()
    if not txns:
        return 0
    db.execute(delete(ReconciliationSuggestion).where(
        ReconciliationSuggestion.bank_transaction_id.in_([t.id for t in txns])))
    inputs = [matching.TxnInput(t.id, t.txn_date, t.amount_minor, t.description, t.counterparty_name, t.reference)
              for t in txns]
    results = matching.match_transactions(inputs, load_lease_candidates(db))
    suggested = 0
    by_id = {t.id: t for t in txns}
    for txn_id, sugs in results.items():
        txn = by_id[txn_id]
        useful = [s for s in sugs if s.score >= matching.MEDIUM_THRESHOLD]
        for s in useful:
            db.add(ReconciliationSuggestion(bank_transaction_id=txn_id, lease_id=s.lease_id, score=s.score,
                                            confidence=s.confidence, reasons=s.reasons, rank=s.rank))
        txn.status = "suggested" if useful else "unmatched"
        suggested += bool(useful)
    db.flush()
    return suggested


# --------------------------------------------------------------------------
# User actions
# --------------------------------------------------------------------------
def _lock_txn(db: Session, txn_id: int) -> BankTransaction:
    txn = db.scalar(select(BankTransaction).where(BankTransaction.id == txn_id).with_for_update())
    if not txn:
        raise ReconciliationError("Movimiento no encontrado")
    return txn


def confirm(db: Session, txn_id: int, lease_id: int, user_id: int | None, learn_alias: bool = True,
            commit: bool = True) -> Payment:
    txn = _lock_txn(db, txn_id)
    if txn.status in ("reconciled", "expense"):
        raise ReconciliationError("Este movimiento ya fue conciliado")
    if txn.amount_minor <= 0:
        raise ReconciliationError("Solo los créditos (ingresos) se concilian contra contratos")
    lease = db.get(Lease, lease_id)
    if not lease:
        raise ReconciliationError("Contrato no encontrado")
    top = db.scalar(select(ReconciliationSuggestion).where(
        ReconciliationSuggestion.bank_transaction_id == txn.id, ReconciliationSuggestion.rank == 1))
    payment = Payment(lease_id=lease.id, bank_transaction_id=txn.id, received_date=txn.txn_date,
                      amount_minor=txn.amount_minor, method="bank_transfer", created_by=user_id)
    db.add(payment)
    db.flush()
    credit = apply_payment(db, payment)
    txn.status = "reconciled"
    db.execute(delete(ReconciliationSuggestion).where(ReconciliationSuggestion.bank_transaction_id == txn.id))
    if learn_alias:
        _learn_alias(db, lease, txn.counterparty_name)
    # suggested_* lets us measure the matcher's real precision per confidence band (docs/ARCHITECTURE.md §3.3)
    audit(db, user_id, "reconcile.confirm", "bank_transaction", txn.id, lease_id=lease.id,
          payment_id=payment.id, amount=txn.amount_minor, unapplied_credit=credit,
          suggested_lease_id=top.lease_id if top else None,
          suggested_confidence=top.confidence if top else None,
          suggested_score=float(top.score) if top else None)
    if commit:
        run_matching(db)   # balances changed: refresh remaining suggestions
        db.commit()
    return payment


def _learn_alias(db: Session, lease: Lease, payer: str | None) -> None:
    alias = normalize(payer)
    primary = lease.primary_tenant
    if not alias or len(alias) < 4 or not primary:
        return
    if any(normalize(p.tenant.full_name) == alias for p in lease.parties):
        return  # already identical to a party name; nothing to learn
    db.execute(insert(TenantPayerAlias).values(tenant_id=primary.id, alias_normalized=alias, source="learned")
               .on_conflict_do_nothing(index_elements=["tenant_id", "alias_normalized"]))


def confirm_all_high(db: Session, user_id: int | None) -> int:
    """Bulk 1-click: accept every top-ranked 'high' confidence suggestion."""
    rows = db.execute(select(ReconciliationSuggestion.bank_transaction_id, ReconciliationSuggestion.lease_id)
                      .join(BankTransaction)
                      .where(ReconciliationSuggestion.rank == 1, ReconciliationSuggestion.confidence == "high",
                             BankTransaction.status == "suggested")
                      .order_by(BankTransaction.txn_date)).all()
    for txn_id, lease_id in rows:
        confirm(db, txn_id, lease_id, user_id, commit=False)
    run_matching(db)
    db.commit()
    return len(rows)


def unreconcile(db: Session, txn_id: int, user_id: int | None) -> None:
    txn = _lock_txn(db, txn_id)
    if txn.status == "reconciled":
        payment = db.scalar(select(Payment).where(Payment.bank_transaction_id == txn.id))
        if payment:
            audit(db, user_id, "reconcile.undo", "bank_transaction", txn.id, payment_id=payment.id,
                  lease_id=payment.lease_id)
            db.delete(payment)
    elif txn.status == "expense":
        exp = db.scalar(select(Expense).where(Expense.bank_transaction_id == txn.id))
        if exp:
            audit(db, user_id, "reconcile.undo_expense", "bank_transaction", txn.id, expense_id=exp.id)
            db.delete(exp)
    txn.status = "unmatched"
    db.flush()
    run_matching(db)
    db.commit()


def ignore(db: Session, txn_id: int, user_id: int | None, reason: str = "") -> None:
    txn = _lock_txn(db, txn_id)
    if txn.status in ("reconciled", "expense"):
        raise ReconciliationError("Deshaga la conciliación antes de ignorar el movimiento")
    txn.status = "ignored"
    db.execute(delete(ReconciliationSuggestion).where(ReconciliationSuggestion.bank_transaction_id == txn.id))
    audit(db, user_id, "reconcile.ignore", "bank_transaction", txn.id, reason=reason)
    db.commit()


def record_expense_from_txn(db: Session, txn_id: int, property_id: int, category_id: int, description: str,
                            unit_id: int | None, vendor_id: int | None, user_id: int | None) -> Expense:
    txn = _lock_txn(db, txn_id)
    if txn.amount_minor >= 0 or txn.status in ("reconciled", "expense"):
        raise ReconciliationError("Solo débitos no conciliados pueden registrarse como gasto")
    exp = Expense(property_id=property_id, unit_id=unit_id, category_id=category_id, vendor_id=vendor_id,
                  expense_date=txn.txn_date, amount_minor=-txn.amount_minor,
                  description=description or txn.description, bank_transaction_id=txn.id, created_by=user_id)
    db.add(exp)
    txn.status = "expense"
    db.flush()
    audit(db, user_id, "reconcile.expense", "bank_transaction", txn.id, expense_id=exp.id)
    db.commit()
    return exp


def record_manual_payment(db: Session, lease_id: int, amount_minor: int, received: date, method: str,
                          notes: str | None, user_id: int | None) -> Payment:
    """Cash / cheque payments that never touch the bank feed."""
    if amount_minor <= 0:
        raise ReconciliationError("El importe debe ser positivo")
    p = Payment(lease_id=lease_id, amount_minor=amount_minor, received_date=received, method=method,
                notes=notes, created_by=user_id)
    db.add(p)
    db.flush()
    apply_payment(db, p)
    audit(db, user_id, "payment.manual", "payment", p.id, lease_id=lease_id, amount=amount_minor)
    run_matching(db)
    db.commit()
    return p


def queue_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(BankTransaction.status, func.count()).where(BankTransaction.amount_minor > 0)
                      .group_by(BankTransaction.status)).all()
    return {status: n for status, n in rows}


def pending_debits(db: Session):
    return db.scalars(select(BankTransaction).where(
        BankTransaction.amount_minor < 0, BankTransaction.status == "unmatched")
        .order_by(BankTransaction.txn_date.desc())).all()


def workbench_rows(db: Session, status: str = "open"):
    q = select(BankTransaction).options(
        selectinload(BankTransaction.suggestions).selectinload(ReconciliationSuggestion.lease)
        .selectinload(Lease.unit),
        selectinload(BankTransaction.suggestions).selectinload(ReconciliationSuggestion.lease)
        .selectinload(Lease.parties).selectinload(LeaseParty.tenant),
        selectinload(BankTransaction.payment).selectinload(Payment.lease).selectinload(Lease.unit),
    ).where(BankTransaction.amount_minor > 0)
    if status == "open":
        q = q.where(BankTransaction.status.in_(("suggested", "unmatched")))
    elif status != "all":
        q = q.where(BankTransaction.status == status)
    return db.scalars(q.order_by(BankTransaction.txn_date.desc(), BankTransaction.id.desc())).all()

