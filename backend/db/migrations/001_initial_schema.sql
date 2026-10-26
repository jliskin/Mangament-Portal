-- =============================================================================
-- Control Platform C.M / Yosef Golan — Initial schema (PostgreSQL 15+)
--
-- Conventions
--   * Money is stored as BIGINT in minor units (cents/agorot). Never FLOAT.
--     Currency lives on the property; all amounts of a property share it.
--   * A charge's payment status (paid / partial / unpaid) is DERIVED from
--     payment_allocations (view v_charge_balances), never stored, so it can
--     never drift from the ledger.
--   * A lease's lifecycle status (upcoming / active / expiring_soon / expired /
--     terminated) is DERIVED from dates (view v_lease_status).
--   * Bank rows are immutable facts; reconciliation creates payments that
--     reference them. Re-importing the same statement is idempotent
--     (bank_imports.file_sha256 + bank_transactions.fingerprint).
--   * Applied by `python -m app.cli migrate` inside one transaction
--     (or manually: psql -1 -f 001_initial_schema.sql).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS btree_gist;   -- exclusion constraint on leases

-- ---------------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------------
CREATE TYPE unit_type        AS ENUM ('apartment', 'commercial', 'office', 'storage', 'parking', 'other');
CREATE TYPE lease_party_role AS ENUM ('primary', 'co_tenant', 'guarantor');
CREATE TYPE charge_type      AS ENUM ('rent', 'condo_fee', 'late_fee', 'utility', 'other');
CREATE TYPE payment_method   AS ENUM ('bank_transfer', 'cash', 'check', 'card', 'other');
CREATE TYPE bank_txn_status  AS ENUM ('unmatched', 'suggested', 'reconciled', 'ignored', 'expense');
CREATE TYPE document_kind    AS ENUM ('lease_contract', 'lease_extension', 'receipt', 'bank_statement', 'other');
CREATE TYPE user_role        AS ENUM ('admin', 'manager', 'viewer');

-- ---------------------------------------------------------------------------
-- Users & audit
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id             BIGSERIAL PRIMARY KEY,
    email          TEXT        NOT NULL UNIQUE,
    full_name      TEXT        NOT NULL,
    password_hash  TEXT        NOT NULL,              -- argon2id
    role           user_role   NOT NULL DEFAULT 'manager',
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),
    action      TEXT        NOT NULL,                 -- e.g. 'reconcile.confirm'
    entity      TEXT        NOT NULL,                 -- e.g. 'bank_transaction'
    entity_id   BIGINT,
    details     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_entity ON audit_log (entity, entity_id);

-- ---------------------------------------------------------------------------
-- Documents (contracts, receipts, statements). Files live on disk / object
-- storage under an opaque storage_key; the DB holds metadata + integrity hash.
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    id                 BIGSERIAL PRIMARY KEY,
    kind               document_kind NOT NULL,
    original_filename  TEXT          NOT NULL,
    storage_key        TEXT          NOT NULL UNIQUE,
    mime_type          TEXT          NOT NULL,
    size_bytes         BIGINT        NOT NULL CHECK (size_bytes >= 0),
    sha256             CHAR(64)      NOT NULL,
    uploaded_by        BIGINT REFERENCES users(id),
    uploaded_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Property & units
-- ---------------------------------------------------------------------------
CREATE TABLE properties (
    id                       BIGSERIAL PRIMARY KEY,
    name                     TEXT     NOT NULL,
    address                  TEXT,
    currency                 CHAR(3)  NOT NULL DEFAULT 'USD',
    invoice_day              SMALLINT NOT NULL DEFAULT 1  CHECK (invoice_day BETWEEN 1 AND 28),  -- day the billing engine issues the month's charges
    grace_days               SMALLINT NOT NULL DEFAULT 5  CHECK (grace_days >= 0),
    expiring_soon_days       SMALLINT NOT NULL DEFAULT 90 CHECK (expiring_soon_days > 0),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE units (
    id                    BIGSERIAL PRIMARY KEY,
    property_id           BIGINT    NOT NULL REFERENCES properties(id),
    unit_number           TEXT      NOT NULL,
    floor                 SMALLINT,
    size_sqm              NUMERIC(10,2) CHECK (size_sqm > 0),
    unit_type             unit_type NOT NULL DEFAULT 'apartment',
    baseline_rent_minor   BIGINT    NOT NULL DEFAULT 0 CHECK (baseline_rent_minor >= 0),  -- market/asking rent
    condo_fee_minor       BIGINT    NOT NULL DEFAULT 0 CHECK (condo_fee_minor >= 0),      -- standard fee
    is_active             BOOLEAN   NOT NULL DEFAULT TRUE,
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (property_id, unit_number)
);

-- ---------------------------------------------------------------------------
-- Tenants
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id                        BIGSERIAL PRIMARY KEY,
    full_name                 TEXT NOT NULL,
    national_id               TEXT,                    -- optional; PII
    phone                     TEXT,
    email                     TEXT,
    emergency_contact_name    TEXT,
    emergency_contact_phone   TEXT,
    notes                     TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_tenants_name ON tenants (lower(full_name));

-- Names under which a tenant's money arrives at the bank (spouse, company,
-- parent...). 'learned' rows are created automatically when a user confirms
-- a reconciliation, so the matcher improves with every month of use.
CREATE TABLE tenant_payer_aliases (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    alias_normalized  TEXT    NOT NULL,
    source            TEXT    NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'learned')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, alias_normalized)
);
CREATE INDEX ix_alias_lookup ON tenant_payer_aliases (alias_normalized);

-- ---------------------------------------------------------------------------
-- Leases (digital contracts)
-- ---------------------------------------------------------------------------
CREATE TABLE leases (
    id                        BIGSERIAL PRIMARY KEY,
    unit_id                   BIGINT   NOT NULL REFERENCES units(id),
    payment_reference         TEXT     NOT NULL UNIQUE,      -- code tenants put in the transfer, e.g. 'CM-301'
    signed_date               DATE,
    start_date                DATE     NOT NULL,             -- contractual start (billing starts here)
    entry_date                DATE,                          -- physical move-in / key handover
    end_date                  DATE     NOT NULL,             -- contractual expiration (current, after extensions)
    exit_date                 DATE,                          -- actual move-out, if any
    monthly_rent_minor        BIGINT   NOT NULL CHECK (monthly_rent_minor >= 0),
    monthly_condo_fee_minor   BIGINT   NOT NULL DEFAULT 0 CHECK (monthly_condo_fee_minor >= 0),
    due_day                   SMALLINT NOT NULL DEFAULT 5 CHECK (due_day BETWEEN 1 AND 28),     -- contractual payment due day
    security_deposit_minor    BIGINT   NOT NULL DEFAULT 0 CHECK (security_deposit_minor >= 0),
    deposit_returned_minor    BIGINT   NOT NULL DEFAULT 0 CHECK (deposit_returned_minor >= 0),
    has_renewal_option        BOOLEAN  NOT NULL DEFAULT FALSE,
    renewal_option_months     SMALLINT CHECK (renewal_option_months > 0),
    renewal_notice_days       SMALLINT CHECK (renewal_notice_days >= 0),
    terminated_at             DATE,                          -- early termination
    termination_reason        TEXT,
    contract_document_id      BIGINT REFERENCES documents(id),
    notes                     TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_date >= start_date),
    CHECK (exit_date IS NULL OR exit_date >= start_date),
    CHECK (deposit_returned_minor <= security_deposit_minor),
    -- A unit can never carry two overlapping leases.
    CONSTRAINT no_overlapping_leases EXCLUDE USING gist (
        unit_id WITH =,
        daterange(start_date, COALESCE(terminated_at, exit_date, end_date), '[]') WITH &&
    )
);
CREATE INDEX ix_leases_unit ON leases (unit_id);
CREATE INDEX ix_leases_end ON leases (end_date);

CREATE TABLE lease_parties (
    lease_id   BIGINT NOT NULL REFERENCES leases(id) ON DELETE CASCADE,
    tenant_id  BIGINT NOT NULL REFERENCES tenants(id),
    role       lease_party_role NOT NULL DEFAULT 'primary',
    PRIMARY KEY (lease_id, tenant_id)
);
-- exactly one primary per lease
CREATE UNIQUE INDEX ux_lease_primary ON lease_parties (lease_id) WHERE role = 'primary';

-- History of renewals/extensions: the lease row always holds the CURRENT
-- terms; this table keeps the audit trail of how they changed.
CREATE TABLE lease_extensions (
    id                          BIGSERIAL PRIMARY KEY,
    lease_id                    BIGINT NOT NULL REFERENCES leases(id) ON DELETE CASCADE,
    previous_end_date           DATE   NOT NULL,
    new_end_date                DATE   NOT NULL,
    previous_rent_minor         BIGINT NOT NULL,
    new_rent_minor              BIGINT NOT NULL,
    previous_condo_fee_minor    BIGINT NOT NULL,
    new_condo_fee_minor         BIGINT NOT NULL,
    signed_date                 DATE,
    document_id                 BIGINT REFERENCES documents(id),
    notes                       TEXT,
    created_by                  BIGINT REFERENCES users(id),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (new_end_date > previous_end_date)
);

-- ---------------------------------------------------------------------------
-- Receivables: charges (what is owed) — generated monthly by the billing job
-- ---------------------------------------------------------------------------
CREATE TABLE charges (
    id            BIGSERIAL PRIMARY KEY,
    lease_id      BIGINT      NOT NULL REFERENCES leases(id),
    unit_id       BIGINT      NOT NULL REFERENCES units(id),
    charge_type   charge_type NOT NULL,
    period        DATE        NOT NULL CHECK (EXTRACT(DAY FROM period) = 1),  -- first day of billed month
    due_date      DATE        NOT NULL,
    amount_minor  BIGINT      NOT NULL CHECK (amount_minor > 0),
    description   TEXT,
    voided_at     TIMESTAMPTZ,
    void_reason   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Idempotency of the billing engine: one rent + one condo charge per lease per month.
CREATE UNIQUE INDEX ux_charges_recurring
    ON charges (lease_id, charge_type, period)
    WHERE charge_type IN ('rent', 'condo_fee') AND voided_at IS NULL;
CREATE INDEX ix_charges_lease_due ON charges (lease_id, due_date) WHERE voided_at IS NULL;

-- ---------------------------------------------------------------------------
-- Bank ingestion
-- ---------------------------------------------------------------------------
CREATE TABLE bank_accounts (
    id            BIGSERIAL PRIMARY KEY,
    property_id   BIGINT  NOT NULL REFERENCES properties(id),
    name          TEXT    NOT NULL,
    bank_name     TEXT,
    account_mask  TEXT,                 -- last 4 digits only; never the full number
    currency      CHAR(3) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bank_imports (
    id               BIGSERIAL PRIMARY KEY,
    bank_account_id  BIGINT  NOT NULL REFERENCES bank_accounts(id),
    source           TEXT    NOT NULL CHECK (source IN ('file', 'api')),
    document_id      BIGINT REFERENCES documents(id),
    file_sha256      CHAR(64) UNIQUE,                  -- same file can't be imported twice
    rows_total       INT NOT NULL DEFAULT 0,
    rows_imported    INT NOT NULL DEFAULT 0,
    rows_duplicate   INT NOT NULL DEFAULT 0,
    rows_error       INT NOT NULL DEFAULT 0,
    parse_report     JSONB NOT NULL DEFAULT '{}'::jsonb,  -- detected columns, formats, row errors
    imported_by      BIGINT REFERENCES users(id),
    imported_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bank_transactions (
    id                 BIGSERIAL PRIMARY KEY,
    bank_account_id    BIGINT  NOT NULL REFERENCES bank_accounts(id),
    import_id          BIGINT REFERENCES bank_imports(id),
    txn_date           DATE    NOT NULL,
    value_date         DATE,
    amount_minor       BIGINT  NOT NULL CHECK (amount_minor <> 0),  -- + credit (money in) / - debit
    balance_minor      BIGINT,
    description        TEXT    NOT NULL DEFAULT '',
    counterparty_name  TEXT,
    reference          TEXT,
    external_id        TEXT,                           -- provider id when coming from an API
    fingerprint        CHAR(64) NOT NULL,
    status             bank_txn_status NOT NULL DEFAULT 'unmatched',
    raw                JSONB   NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bank_account_id, fingerprint)
);
CREATE INDEX ix_bank_txn_status ON bank_transactions (status, txn_date);

-- Matcher output. Recomputed on demand; the highest-ranked row per
-- transaction is what the reconciliation screen proposes.
CREATE TABLE reconciliation_suggestions (
    id                   BIGSERIAL PRIMARY KEY,
    bank_transaction_id  BIGINT NOT NULL REFERENCES bank_transactions(id) ON DELETE CASCADE,
    lease_id             BIGINT NOT NULL REFERENCES leases(id),
    score                NUMERIC(5,4) NOT NULL CHECK (score BETWEEN 0 AND 1),
    confidence           TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    reasons              JSONB NOT NULL DEFAULT '[]'::jsonb,
    rank                 SMALLINT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bank_transaction_id, lease_id)
);

-- ---------------------------------------------------------------------------
-- Payments & allocations (cash applied against charges, FIFO by default)
-- ---------------------------------------------------------------------------
CREATE TABLE payments (
    id                   BIGSERIAL PRIMARY KEY,
    lease_id             BIGINT NOT NULL REFERENCES leases(id),
    bank_transaction_id  BIGINT UNIQUE REFERENCES bank_transactions(id),  -- 1 bank row → at most 1 payment
    received_date        DATE   NOT NULL,
    amount_minor         BIGINT NOT NULL CHECK (amount_minor > 0),
    method               payment_method NOT NULL DEFAULT 'bank_transfer',
    notes                TEXT,
    created_by           BIGINT REFERENCES users(id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_payments_lease ON payments (lease_id, received_date);

CREATE TABLE payment_allocations (
    id            BIGSERIAL PRIMARY KEY,
    payment_id    BIGINT NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    charge_id     BIGINT NOT NULL REFERENCES charges(id),
    amount_minor  BIGINT NOT NULL CHECK (amount_minor > 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (payment_id, charge_id)
);
CREATE INDEX ix_alloc_charge ON payment_allocations (charge_id);

-- Guard: allocations may never exceed the payment or the charge.
CREATE OR REPLACE FUNCTION trg_check_allocation() RETURNS trigger AS $$
DECLARE
    pay_total    BIGINT;
    pay_alloc    BIGINT;
    chg_total    BIGINT;
    chg_alloc    BIGINT;
BEGIN
    SELECT amount_minor INTO pay_total FROM payments WHERE id = NEW.payment_id FOR UPDATE;
    SELECT COALESCE(SUM(amount_minor), 0) INTO pay_alloc FROM payment_allocations WHERE payment_id = NEW.payment_id;
    SELECT amount_minor INTO chg_total FROM charges  WHERE id = NEW.charge_id FOR UPDATE;
    SELECT COALESCE(SUM(amount_minor), 0) INTO chg_alloc FROM payment_allocations WHERE charge_id = NEW.charge_id;
    IF pay_alloc > pay_total THEN
        RAISE EXCEPTION 'over-allocation of payment % (% > %)', NEW.payment_id, pay_alloc, pay_total;
    END IF;
    IF chg_alloc > chg_total THEN
        RAISE EXCEPTION 'over-allocation of charge % (% > %)', NEW.charge_id, chg_alloc, chg_total;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER payment_allocations_guard
    AFTER INSERT OR UPDATE ON payment_allocations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_check_allocation();

-- ---------------------------------------------------------------------------
-- Expenses
-- ---------------------------------------------------------------------------
CREATE TABLE expense_categories (
    id        BIGSERIAL PRIMARY KEY,
    code      TEXT    NOT NULL UNIQUE,
    name      TEXT    NOT NULL,
    is_capex  BOOLEAN NOT NULL DEFAULT FALSE      -- CapEx is excluded from NOI by definition
);

CREATE TABLE vendors (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    tax_id      TEXT,
    phone       TEXT,
    email       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE expenses (
    id                    BIGSERIAL PRIMARY KEY,
    property_id           BIGINT NOT NULL REFERENCES properties(id),
    unit_id               BIGINT REFERENCES units(id),     -- NULL = common-area / building-wide
    category_id           BIGINT NOT NULL REFERENCES expense_categories(id),
    vendor_id             BIGINT REFERENCES vendors(id),
    expense_date          DATE   NOT NULL,
    amount_minor          BIGINT NOT NULL CHECK (amount_minor > 0),
    description           TEXT   NOT NULL,
    receipt_document_id   BIGINT REFERENCES documents(id),
    bank_transaction_id   BIGINT UNIQUE REFERENCES bank_transactions(id),
    created_by            BIGINT REFERENCES users(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_expenses_date ON expenses (property_id, expense_date);

-- ---------------------------------------------------------------------------
-- Derived views
-- ---------------------------------------------------------------------------
CREATE VIEW v_charge_balances AS
SELECT c.*,
       COALESCE(a.allocated, 0)                    AS allocated_minor,
       c.amount_minor - COALESCE(a.allocated, 0)   AS outstanding_minor,
       CASE
         WHEN COALESCE(a.allocated, 0) >= c.amount_minor THEN 'paid'
         WHEN COALESCE(a.allocated, 0) > 0              THEN 'partial'
         ELSE 'unpaid'
       END                                         AS payment_status
FROM charges c
LEFT JOIN (SELECT charge_id, SUM(amount_minor) AS allocated
           FROM payment_allocations GROUP BY charge_id) a ON a.charge_id = c.id
WHERE c.voided_at IS NULL;

CREATE VIEW v_lease_status AS
SELECT l.id AS lease_id,
       CASE
         WHEN l.terminated_at IS NOT NULL AND l.terminated_at <= CURRENT_DATE THEN 'terminated'
         WHEN l.start_date > CURRENT_DATE                                       THEN 'upcoming'
         WHEN l.end_date < CURRENT_DATE                                         THEN 'expired'
         WHEN l.end_date <= CURRENT_DATE + p.expiring_soon_days                 THEN 'expiring_soon'
         ELSE 'active'
       END AS status,
       (l.end_date - CURRENT_DATE) AS days_to_expiry
FROM leases l
JOIN units u      ON u.id = l.unit_id
JOIN properties p ON p.id = u.property_id;

-- ---------------------------------------------------------------------------
-- Seed: expense categories
-- ---------------------------------------------------------------------------
INSERT INTO expense_categories (code, name, is_capex) VALUES
    ('maintenance', 'Mantenimiento',             FALSE),
    ('repairs',     'Reparaciones',              FALSE),
    ('utilities',   'Servicios (agua/luz/gas)',  FALSE),
    ('taxes',       'Impuestos y tasas',         FALSE),
    ('insurance',   'Seguros',                   FALSE),
    ('cleaning',    'Limpieza',                  FALSE),
    ('management',  'Administración / honorarios', FALSE),
    ('bank_fees',   'Comisiones bancarias',      FALSE),
    ('capex',       'Mejoras de capital (CapEx)', TRUE);

