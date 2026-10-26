"""Unit tests for the pure domain layer (no database)."""
from datetime import date
from pathlib import Path

import pytest

from app.domain import allocation, billing, financials, leases, matching
from app.domain.bank_parser import extract_counterparty, parse_statement
from app.domain.money import detect_decimal_separator, fmt, parse_amount

FIX = Path(__file__).parent / "fixtures"


# ----------------------------------------------------------------- money
@pytest.mark.parametrize("raw,sep,expected", [
    ("1.450,00", ",", 145000), ("1,450.00", ".", 145000), ("-320,55", ",", -32055),
    ("(1,234.50)", ".", -123450), ("500-", ".", -50000), ("$ 1,000", ".", 100000),
    ("R$ 2.500,10", ",", 250010), ("₪3,200.00", ".", 320000), (1450.5, ".", 145050),
])
def test_parse_amount(raw, sep, expected):
    assert parse_amount(raw, sep) == expected


def test_detect_decimal_separator():
    assert detect_decimal_separator(["1.450,00", "-320,55"]) == ","
    assert detect_decimal_separator(["1,450.00", "12.5"]) == "."
    assert detect_decimal_separator(["1.450", "2.000", "12,50"]) == ","


def test_fmt():
    assert fmt(123456789) == "1,234,567.89"
    assert fmt(-5) == "-0.05"


# ----------------------------------------------------------- bank parser
def test_parse_spanish_statement_with_preamble():
    res = parse_statement((FIX / "banco_es.csv").read_bytes(), "banco_es.csv", "acct1")
    r = res.report
    assert r["header_row"] == 5 and r["delimiter"] == ";" and r["decimal_separator"] == ","
    assert r["columns"]["description"] == "Concepto"
    assert len(res.transactions) == 7 and not res.errors      # 'Saldo final' footer skipped
    first = res.transactions[0]
    assert first.txn_date == date(2026, 9, 1) and first.amount_minor == 145000
    assert first.balance_minor == 1045000
    assert first.counterparty_name == "Jose Perez Gomez"
    assert len(res.credits) == 5
    assert len({t.fingerprint for t in res.transactions}) == 7


def test_parse_us_split_columns_monthfirst():
    res = parse_statement((FIX / "bank_us_split.csv").read_bytes(), "x.csv", "a")
    assert res.report["date_order"] == "monthfirst"
    amounts = [t.amount_minor for t in res.transactions]
    assert amounts == [145000, -500000, 145000]
    assert res.transactions[1].txn_date == date(2026, 9, 15)


def test_parse_xlsx(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Data", "Histórico", "Valor"])
    ws.append([date(2026, 9, 2), "PIX RECEBIDO DE ANA SOUZA", 1450.0])
    ws.append([date(2026, 9, 3), "TARIFA", -15.9])
    p = tmp_path / "s.xlsx"
    wb.save(p)
    res = parse_statement(p.read_bytes(), "s.xlsx", "a")
    assert [t.amount_minor for t in res.transactions] == [145000, -1590]
    assert res.transactions[0].counterparty_name == "Ana Souza"


def test_reimport_produces_identical_fingerprints_and_keeps_true_duplicates():
    csv = b"Fecha;Concepto;Importe\n01/09/2026;TRANSF DE X;100,00\n01/09/2026;TRANSF DE X;100,00\n"
    a = parse_statement(csv, "a.csv", "acct")
    b = parse_statement(csv, "a.csv", "acct")
    assert [t.fingerprint for t in a.transactions] == [t.fingerprint for t in b.transactions]
    assert a.transactions[0].fingerprint != a.transactions[1].fingerprint


def test_bad_rows_are_reported_not_fatal():
    csv = b"Fecha;Concepto;Importe\n01/09/2026;OK;100,00\n99/99/2026;MAL;100,00\n"
    res = parse_statement(csv, "a.csv")
    assert len(res.transactions) == 1 and len(res.errors) == 1 and res.errors[0].row_number == 3


def test_extract_counterparty():
    assert extract_counterparty("TRANSF. RECIBIDA DE JOSE PEREZ REF 123") == "Jose Perez"
    assert extract_counterparty("COMISION MANTENIMIENTO") is None


# -------------------------------------------------------------- matching
def _lease(lid, unit, names, rent=145000, open_amounts=(145000,), aliases=()):
    return matching.LeaseCandidate(
        lease_id=lid, unit_number=unit, payment_reference=f"CM-{unit}", party_names=list(names),
        aliases=list(aliases), monthly_amount_minor=rent,
        open_charges=[matching.OpenCharge(lid * 100 + i, date(2026, 9 - len(open_amounts) + 1 + i, 1), a, "rent")
                      for i, a in enumerate(open_amounts)])


LEASES = [
    _lease(1, "101", ["José Pérez Gómez"]),
    _lease(2, "102", ["María López Sánchez"]),
    _lease(3, "301", ["Yael Cohen"], rent=200000, open_amounts=(200000,), aliases=["inversiones golan"]),
    _lease(4, "201", ["Pedro Ruiz"], open_amounts=(145000,)),
]


def _txn(i, amount, desc, who=None):
    return matching.TxnInput(i, date(2026, 9, 3), amount, desc, who)


def test_high_confidence_with_reference_code():
    res = matching.match_transactions([_txn(1, 145000, "TRANSF REF CM-101", "JOSE PEREZ GOMEZ")], LEASES)
    s = res[1][0]
    assert s.lease_id == 1 and s.confidence == "high"


def test_same_amount_without_identity_is_not_high():
    # 145000 fits leases 1, 2 and 4 equally: amount alone must not auto-match
    res = matching.match_transactions([_txn(1, 145000, "DEPOSITO EFECTIVO")], LEASES)
    assert not res[1] or res[1][0].confidence != "high"


def test_name_match_with_reordered_tokens_and_no_accents():
    res = matching.match_transactions([_txn(1, 145000, "TRANSFERENCIA", "LOPEZ SANCHEZ MARIA")], LEASES)
    assert res[1][0].lease_id == 2 and res[1][0].confidence == "high"


def test_learned_alias_and_unit_mention():
    res = matching.match_transactions([_txn(1, 200000, "DEPOSITO APTO 301", "INVERSIONES GOLAN SL")], LEASES)
    assert res[1][0].lease_id == 3 and res[1][0].confidence == "high"


def test_partial_payment_still_matched_by_name():
    res = matching.match_transactions([_txn(1, 70000, "TRANSF", "PEDRO RUIZ")], LEASES)
    assert res[1][0].lease_id == 4 and res[1][0].confidence in ("high", "medium")


def test_virtual_ledger_prevents_double_claim():
    leases = [_lease(1, "101", ["José Pérez"], open_amounts=(145000,))]
    txns = [_txn(1, 145000, "TRANSF", "JOSE PEREZ"), _txn(2, 145000, "TRANSF", "JOSE PEREZ")]
    res = matching.match_transactions(txns, leases)
    reasons = res[1][0].reasons + res[2][0].reasons
    # only one of the two can be "Importe = deuda total pendiente"
    assert sum("deuda total" in r for r in reasons) == 1


def test_debits_are_ignored():
    res = matching.match_transactions([_txn(1, -145000, "PAGO", "JOSE PEREZ")], LEASES)
    assert res[1] == []


# ------------------------------------------------------------ allocation
def test_allocate_fifo_oldest_first_with_credit():
    items = [allocation.OpenItem(2, date(2026, 9, 1), "rent", 100000),
             allocation.OpenItem(1, date(2026, 8, 1), "rent", 100000),
             allocation.OpenItem(3, date(2026, 8, 1), "condo_fee", 20000)]
    allocs, credit = allocation.allocate_fifo(250000, items)
    assert allocs == [(3, 20000), (1, 100000), (2, 100000)] and credit == 30000


# --------------------------------------------------------------- billing
TERMS = billing.LeaseTerms(1, 10, date(2026, 3, 15), date(2027, 3, 14), 150000, 20000, 5)


def test_full_month_charges():
    drafts = billing.charges_for_period(TERMS, date(2026, 4, 1))
    assert [(d.charge_type, d.amount_minor, d.due_date) for d in drafts] == [
        ("rent", 150000, date(2026, 4, 5)), ("condo_fee", 20000, date(2026, 4, 5))]


def test_prorated_first_month():
    rent = billing.charges_for_period(TERMS, date(2026, 3, 1))[0]
    assert rent.amount_minor == round(150000 * 17 / 31) and "17/31" in rent.description


def test_no_charges_outside_term_or_after_termination():
    assert billing.charges_for_period(TERMS, date(2026, 2, 1)) == []
    terminated = billing.LeaseTerms(1, 10, date(2026, 1, 1), date(2026, 12, 31), 100000, 0, 1,
                                    terminated_at=date(2026, 6, 10))
    assert billing.charges_for_period(terminated, date(2026, 7, 1)) == []
    assert billing.charges_for_period(terminated, date(2026, 6, 1))[0].amount_minor == round(100000 * 10 / 30)


# ---------------------------------------------------------------- leases
def test_lease_status():
    t = date(2026, 10, 26)
    assert leases.lease_status(date(2026, 1, 1), date(2027, 12, 31), t) == "active"
    assert leases.lease_status(date(2026, 1, 1), date(2026, 12, 31), t) == "expiring_soon"
    assert leases.lease_status(date(2025, 1, 1), date(2026, 9, 30), t) == "expired"
    assert leases.lease_status(date(2026, 11, 1), date(2027, 10, 31), t) == "upcoming"
    assert leases.lease_status(date(2026, 1, 1), date(2027, 1, 1), t, terminated_at=date(2026, 10, 1)) == "terminated"


def test_renewal_notice_alert_is_critical():
    alerts = leases.expiry_alerts(1, "101", date(2027, 1, 24), date(2026, 10, 26), True, 60)
    assert any(a.level == "critical" and "renovación" in a.message for a in alerts)


# ------------------------------------------------------------ financials
CH = [
    financials.ChargeRow(1, 1, 1, "rent", date(2026, 9, 1), date(2026, 9, 5), 100000, 100000),
    financials.ChargeRow(2, 1, 1, "condo_fee", date(2026, 9, 1), date(2026, 9, 5), 20000, 0),
    financials.ChargeRow(3, 2, 2, "rent", date(2026, 9, 1), date(2026, 9, 5), 100000, 40000),
    financials.ChargeRow(4, 2, 2, "rent", date(2026, 6, 1), date(2026, 6, 5), 100000, 0),
]
EX = [
    financials.ExpenseRow(None, date(2026, 9, 10), 30001, "maintenance", False),
    financials.ExpenseRow(1, date(2026, 9, 11), 5000, "repairs", False),
    financials.ExpenseRow(None, date(2026, 9, 12), 900000, "capex", True),
]
UNITS = [financials.UnitRow(1, "101", 60.0, 100000, True), financials.UnitRow(2, "102", 40.0, 100000, True)]
S, E = date(2026, 9, 1), date(2026, 9, 30)


def test_noi_excludes_capex():
    r = financials.compute_noi(S, E, CH, [], EX)
    assert r.revenue_minor == 220000 and r.opex_minor == 35001 and r.capex_minor == 900000
    assert r.noi_minor == 184999 and r.cash_flow_before_debt_minor == 184999 - 900000


def test_per_unit_allocation_by_sqm_sums_exactly():
    rows = {r.unit_number: r for r in financials.per_unit_pnl(S, E, UNITS, CH, EX)}
    assert rows["101"].allocated_opex_minor + rows["102"].allocated_opex_minor == 30001
    assert rows["101"].allocated_opex_minor == 18001  # 60% (+1 cent remainder)
    assert rows["101"].noi_minor == 120000 - 5000 - 18001


def test_aging_buckets():
    rows = {r.lease_id: r for r in financials.aging_report(CH, date(2026, 10, 26), grace_days=5)}
    assert rows[1].buckets["d31_60"] == 20000          # 51 days late - 5 grace = 46
    assert rows[2].buckets["d31_60"] == 60000 and rows[2].buckets["d90_plus"] == 100000
    assert rows[2].oldest_due == date(2026, 6, 5)


def test_kpis():
    k = financials.kpis(S, E, UNITS, CH, date(2026, 10, 26))
    assert k.billed_minor == 220000 and k.collected_on_billed_minor == 140000
    assert k.arrears_minor == 20000 + 60000 + 100000 and k.economic_occupancy == 1.0


def test_hebrew_statement_cp1255_with_split_description():
    # Israeli bank layout (e.g. Bank Hapoalim): Hebrew headers, Windows-1255,
    # separate debit/credit, payer name + reference in a second text column.
    csv = ('תנועות בחשבון\n\n'
           'תאריך,תיאור הפעולה,פרטים,אסמכתא,חובה,זכות,יתרה בש"ח,תאריך ערך\n'
           '02/10/2026,העברה מאת,יוסי כהן CM-101,12345,,"4,500.00","14,500.00",02/10/2026\n'
           '05/10/2026,חשמל,חברת החשמל,99887,"420.50",,"17,879.50",05/10/2026\n').encode("cp1255")
    res = parse_statement(csv, "poalim.csv", "a")
    assert res.report["encoding"] == "cp1255" and not res.errors
    assert [t.amount_minor for t in res.transactions] == [450000, -42050]
    assert "CM-101" in res.transactions[0].description and "יוסי כהן" in res.transactions[0].description
    lease = _lease(9, "101", ["Yossi Cohen"], rent=450000, open_amounts=(450000,), aliases=["יוסי כהן"])
    t = res.transactions[0]
    s = matching.match_transactions([matching.TxnInput(1, t.txn_date, t.amount_minor, t.description)], [lease])[1][0]
    assert s.confidence == "high"
