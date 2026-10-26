"""ORM mapping of db/migrations/*.sql. The SQL files are the source of truth;
these classes never create tables (no metadata.create_all)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def pg_enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, create_type=False)


UnitType = pg_enum("unit_type", "apartment", "commercial", "office", "storage", "parking", "other")
PartyRole = pg_enum("lease_party_role", "primary", "co_tenant", "guarantor")
ChargeType = pg_enum("charge_type", "rent", "condo_fee", "late_fee", "utility", "other")
PaymentMethod = pg_enum("payment_method", "bank_transfer", "cash", "check", "card", "other")
BankTxnStatus = pg_enum("bank_txn_status", "unmatched", "suggested", "reconciled", "ignored", "expense")
DocumentKind = pg_enum("document_kind", "lease_contract", "lease_extension", "receipt", "bank_statement", "other")
UserRole = pg_enum("user_role", "admin", "manager", "viewer")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(UserRole, default="manager")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(DocumentKind)
    original_filename: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, default="USD")
    invoice_day: Mapped[int] = mapped_column(SmallInteger, default=1)
    grace_days: Mapped[int] = mapped_column(SmallInteger, default=5)
    expiring_soon_days: Mapped[int] = mapped_column(SmallInteger, default=90)


class Unit(Base):
    __tablename__ = "units"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    unit_number: Mapped[str] = mapped_column(Text)
    floor: Mapped[int | None] = mapped_column(SmallInteger)
    size_sqm: Mapped[float | None] = mapped_column(Numeric(10, 2))
    unit_type: Mapped[str] = mapped_column(UnitType, default="apartment")
    baseline_rent_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    condo_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    property: Mapped[Property] = relationship()
    leases: Mapped[list[Lease]] = relationship(back_populates="unit", order_by="Lease.start_date.desc()")


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(Text)
    national_id: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    emergency_contact_name: Mapped[str | None] = mapped_column(Text)
    emergency_contact_phone: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    aliases: Mapped[list[TenantPayerAlias]] = relationship(cascade="all, delete-orphan")
    lease_links: Mapped[list[LeaseParty]] = relationship(back_populates="tenant")


class TenantPayerAlias(Base):
    __tablename__ = "tenant_payer_aliases"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    alias_normalized: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="manual")


class Lease(Base):
    __tablename__ = "leases"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    payment_reference: Mapped[str] = mapped_column(Text, unique=True)
    signed_date: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date] = mapped_column(Date)
    entry_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    exit_date: Mapped[date | None] = mapped_column(Date)
    monthly_rent_minor: Mapped[int] = mapped_column(BigInteger)
    monthly_condo_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    due_day: Mapped[int] = mapped_column(SmallInteger, default=5)
    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    deposit_returned_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    has_renewal_option: Mapped[bool] = mapped_column(Boolean, default=False)
    renewal_option_months: Mapped[int | None] = mapped_column(SmallInteger)
    renewal_notice_days: Mapped[int | None] = mapped_column(SmallInteger)
    terminated_at: Mapped[date | None] = mapped_column(Date)
    termination_reason: Mapped[str | None] = mapped_column(Text)
    contract_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    notes: Mapped[str | None] = mapped_column(Text)

    unit: Mapped[Unit] = relationship(back_populates="leases")
    parties: Mapped[list[LeaseParty]] = relationship(back_populates="lease", cascade="all, delete-orphan")
    extensions: Mapped[list[LeaseExtension]] = relationship(order_by="LeaseExtension.created_at")
    contract_document: Mapped[Document | None] = relationship()

    @property
    def primary_tenant(self) -> Tenant | None:
        return next((p.tenant for p in self.parties if p.role == "primary"), None)

    @property
    def monthly_total_minor(self) -> int:
        return self.monthly_rent_minor + self.monthly_condo_fee_minor


class LeaseParty(Base):
    __tablename__ = "lease_parties"
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    role: Mapped[str] = mapped_column(PartyRole, default="primary")
    lease: Mapped[Lease] = relationship(back_populates="parties")
    tenant: Mapped[Tenant] = relationship(back_populates="lease_links")


class LeaseExtension(Base):
    __tablename__ = "lease_extensions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id", ondelete="CASCADE"))
    previous_end_date: Mapped[date] = mapped_column(Date)
    new_end_date: Mapped[date] = mapped_column(Date)
    previous_rent_minor: Mapped[int] = mapped_column(BigInteger)
    new_rent_minor: Mapped[int] = mapped_column(BigInteger)
    previous_condo_fee_minor: Mapped[int] = mapped_column(BigInteger)
    new_condo_fee_minor: Mapped[int] = mapped_column(BigInteger)
    signed_date: Mapped[date | None] = mapped_column(Date)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    charge_type: Mapped[str] = mapped_column(ChargeType)
    period: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str | None] = mapped_column(Text)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(Text)

    allocations: Mapped[list[PaymentAllocation]] = relationship(back_populates="charge")

    @property
    def allocated_minor(self) -> int:
        return sum(a.amount_minor for a in self.allocations)

    @property
    def outstanding_minor(self) -> int:
        return self.amount_minor - self.allocated_minor

    @property
    def payment_status(self) -> str:
        a = self.allocated_minor
        return "paid" if a >= self.amount_minor else "partial" if a > 0 else "unpaid"


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    name: Mapped[str] = mapped_column(Text)
    bank_name: Mapped[str | None] = mapped_column(Text)
    account_mask: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text)


class BankImport(Base):
    __tablename__ = "bank_imports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    source: Mapped[str] = mapped_column(Text)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    file_sha256: Mapped[str | None] = mapped_column(Text, unique=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_error: Mapped[int] = mapped_column(Integer, default=0)
    parse_report: Mapped[dict] = mapped_column(JSONB, default=dict)
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    import_id: Mapped[int | None] = mapped_column(ForeignKey("bank_imports.id"))
    txn_date: Mapped[date] = mapped_column(Date)
    value_date: Mapped[date | None] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    balance_minor: Mapped[int | None] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(Text, default="")
    counterparty_name: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(BankTxnStatus, default="unmatched")
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    suggestions: Mapped[list[ReconciliationSuggestion]] = relationship(
        order_by="ReconciliationSuggestion.rank", cascade="all, delete-orphan")
    payment: Mapped[Payment | None] = relationship(back_populates="bank_transaction", uselist=False)


class ReconciliationSuggestion(Base):
    __tablename__ = "reconciliation_suggestions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bank_transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id", ondelete="CASCADE"))
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    score: Mapped[float] = mapped_column(Numeric(5, 4))
    confidence: Mapped[str] = mapped_column(Text)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    rank: Mapped[int] = mapped_column(SmallInteger)
    lease: Mapped[Lease] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"))
    bank_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transactions.id"), unique=True)
    received_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(PaymentMethod, default="bank_transfer")
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    lease: Mapped[Lease] = relationship()
    bank_transaction: Mapped[BankTransaction | None] = relationship(back_populates="payment")
    allocations: Mapped[list[PaymentAllocation]] = relationship(back_populates="payment", cascade="all, delete-orphan")

    @property
    def unapplied_minor(self) -> int:
        return self.amount_minor - sum(a.amount_minor for a in self.allocations)


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))
    charge_id: Mapped[int] = mapped_column(ForeignKey("charges.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    payment: Mapped[Payment] = relationship(back_populates="allocations")
    charge: Mapped[Charge] = relationship(back_populates="allocations")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    is_capex: Mapped[bool] = mapped_column(Boolean, default=False)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    tax_id: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"))
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    expense_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(Text)
    receipt_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    bank_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transactions.id"), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    category: Mapped[ExpenseCategory] = relationship()
    vendor: Mapped[Vendor | None] = relationship()
    unit: Mapped[Unit | None] = relationship()
    receipt: Mapped[Document | None] = relationship()
