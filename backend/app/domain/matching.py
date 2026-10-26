"""Auto-reconciliation: match incoming bank credits to leases.

Model
-----
For every (credit transaction, lease) pair we compute independent pieces of
evidence in [0, 1]:

    reference   payment code ('CM-301') or unit number in the text
    name        fuzzy similarity of payer vs. tenant names / learned aliases
    amount      fit of the amount to what the lease owes (total arrears, an
                exact FIFO prefix of open charges, the monthly amount, a
                single charge, or a partial payment)
    timing      distance from the nearest due date

Reference and name are *substitutes* (either one identifies the payer), so
they are combined into one identity signal, with a corroboration bonus when
both agree:

    identity = min(1, max(ref, name) + 0.25 * min(ref, name))
    score    = 0.60 * identity + 0.35 * amount + 0.05 * timing

Why amount is NOT the dominant signal: in a building where several units pay
the same rent, an exact amount match is non-discriminating (it fits N leases
equally). So a high-confidence match additionally requires an identity signal
(reference or name) and a clear margin over the runner-up. This keeps the
false-positive rate low; anything ambiguous goes to human review.

Assignment is greedy over a "virtual ledger": once a transaction is assigned
to a lease, that lease's open balance is reduced before scoring the remaining
transactions, so two identical transfers don't both claim the same month.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

from rapidfuzz import fuzz

from .text import normalize, strip_accents, tokens

WEIGHTS = {"identity": 0.60, "amount": 0.35, "timing": 0.05}
CORROBORATION_BONUS = 0.25
HIGH_THRESHOLD = 0.75     # score needed for 'high' (1-click / bulk confirm)
MEDIUM_THRESHOLD = 0.40   # below this the row stays "unmatched" (exact amount alone = 0.40)
MIN_MARGIN = 0.15         # high confidence also needs a clear gap to the runner-up
AMOUNT_TOLERANCE_MINOR = 200  # bank fees / rounding: up to 2.00 currency units
UNIT_PREFIXES = ("apto", "apt", "apartamento", "depto", "dpto", "departamento", "unidad",
                 "unit", "u", "piso", "local", "oficina", "of", "casa", "dira", "דירה")


@dataclass(frozen=True)
class OpenCharge:
    charge_id: int
    due_date: date
    outstanding_minor: int
    charge_type: str


@dataclass
class LeaseCandidate:
    lease_id: int
    unit_number: str
    payment_reference: str
    party_names: list[str]
    aliases: list[str] = field(default_factory=list)          # already normalized
    monthly_amount_minor: int = 0                              # rent + condo
    open_charges: list[OpenCharge] = field(default_factory=list)

    @property
    def total_outstanding(self) -> int:
        return sum(c.outstanding_minor for c in self.open_charges)


@dataclass(frozen=True)
class TxnInput:
    txn_id: int
    txn_date: date
    amount_minor: int
    description: str
    counterparty_name: str | None = None
    reference: str | None = None

    @property
    def text(self) -> str:
        return " ".join(filter(None, [self.counterparty_name, self.description, self.reference]))


@dataclass
class Evidence:
    reference: float = 0.0
    name: float = 0.0
    amount: float = 0.0
    timing: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def identity(self) -> float:
        hi, lo = max(self.reference, self.name), min(self.reference, self.name)
        return min(1.0, hi + CORROBORATION_BONUS * lo)

    @property
    def score(self) -> float:
        return round(WEIGHTS["identity"] * self.identity + WEIGHTS["amount"] * self.amount
                     + WEIGHTS["timing"] * self.timing, 4)


@dataclass
class Suggestion:
    txn_id: int
    lease_id: int
    score: float
    confidence: str          # 'high' | 'medium' | 'low'
    reasons: list[str]
    rank: int = 1


# --------------------------------------------------------------------------
# Individual signals
# --------------------------------------------------------------------------
def reference_score(txn: TxnInput, lease: LeaseCandidate) -> tuple[float, str | None]:
    raw = strip_accents(txn.text).lower()
    parts = re.findall(r"[a-z0-9]+", lease.payment_reference.lower())
    if parts:
        # 'CM-301' also matches 'cm 301', 'CM301', 'cm_301'
        code_re = r"(?<![a-z0-9])" + r"[\s\-_./#]*".join(map(re.escape, parts)) + r"(?![a-z0-9])"
        if re.search(code_re, raw):
            return 1.0, f"Código de pago {lease.payment_reference} en la descripción"
    toks = tokens(txn.text)
    unit = lease.unit_number.lower()
    for i, tok in enumerate(toks):
        # 'apto 3b', 'unit 301', 'depto-301'
        if tok in UNIT_PREFIXES and i + 1 < len(toks) and toks[i + 1] == unit:
            return 0.9, f"Unidad {lease.unit_number} mencionada"
        # glued forms: 'apto301', 'u301'
        if any(tok == p + unit for p in UNIT_PREFIXES):
            return 0.9, f"Unidad {lease.unit_number} mencionada"
    return 0.0, None


def name_score(txn: TxnInput, lease: LeaseCandidate) -> tuple[float, str | None]:
    payer = normalize(txn.counterparty_name) or ""
    text = normalize(txn.text)
    if payer and payer in lease.aliases:
        return 1.0, f"Pagador conocido ({txn.counterparty_name})"
    best, who = 0.0, None
    for name in lease.party_names:
        n = normalize(name)
        if not n:
            continue
        # token_set_ratio handles word order and extra words ('PEREZ JOSE MARIA' vs 'José Pérez')
        s = max(fuzz.token_set_ratio(n, payer) if payer else 0, fuzz.token_set_ratio(n, text) * 0.95)
        # a single-token overlap on a common surname shouldn't look like a full match
        if len(set(n.split()) & set(text.split())) < min(2, len(n.split())):
            s *= 0.8
        if s > best:
            best, who = s, name
    for alias in lease.aliases:
        s = fuzz.token_set_ratio(alias, text)
        if s > best:
            best, who = s, f"alias '{alias}'"
    best /= 100
    if best < 0.6:
        return 0.0, None
    return best, f"Nombre similar a {who} ({best:.0%})"


def amount_score(amount: int, lease: LeaseCandidate) -> tuple[float, str | None]:
    tol = AMOUNT_TOLERANCE_MINOR
    total = lease.total_outstanding
    if total and abs(amount - total) <= tol:
        return 1.0, "Importe = deuda total pendiente"
    # exact FIFO prefix: pays the oldest k open charges in full
    running = 0
    for k, ch in enumerate(sorted(lease.open_charges, key=lambda c: c.due_date), start=1):
        running += ch.outstanding_minor
        if abs(amount - running) <= tol:
            return 0.95, f"Importe cubre los {k} cargos más antiguos"
        if running > amount + tol:
            break
    if lease.monthly_amount_minor and abs(amount - lease.monthly_amount_minor) <= tol:
        return 0.85, "Importe = cuota mensual (alquiler + expensas)"
    if any(abs(amount - c.outstanding_minor) <= tol for c in lease.open_charges):
        return 0.8, "Importe = un cargo pendiente"
    if total and amount < total:
        return 0.4 * amount / total, "Pago parcial de la deuda"
    if lease.monthly_amount_minor and amount % lease.monthly_amount_minor == 0:
        return 0.5, "Múltiplo de la cuota mensual (pago adelantado)"
    return 0.0, None


def timing_score(txn_date: date, lease: LeaseCandidate) -> float:
    if not lease.open_charges:
        return 0.3
    gap = min(abs((txn_date - c.due_date).days) for c in lease.open_charges)
    if gap <= 10:
        return 1.0
    return max(0.0, 1 - (gap - 10) / 50)


def classify(best: float, runner_up: float, identity: float) -> str:
    if best >= HIGH_THRESHOLD and best - runner_up >= MIN_MARGIN and identity >= 0.8:
        return "high"
    if best >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


# --------------------------------------------------------------------------
# Batch matching
# --------------------------------------------------------------------------
def _consume(lease: LeaseCandidate, amount: int) -> LeaseCandidate:
    """Virtually apply `amount` FIFO to a lease's open charges."""
    remaining, new_open = amount, []
    for ch in sorted(lease.open_charges, key=lambda c: c.due_date):
        take = min(remaining, ch.outstanding_minor)
        remaining -= take
        if ch.outstanding_minor - take > 0:
            new_open.append(replace(ch, outstanding_minor=ch.outstanding_minor - take))
    return replace(lease, open_charges=new_open)


def match_transactions(txns: list[TxnInput], leases: list[LeaseCandidate],
                       top_n: int = 3) -> dict[int, list[Suggestion]]:
    """Return ranked suggestions per transaction id (only credits are scored)."""
    state = {l.lease_id: l for l in leases}
    pending = [t for t in txns if t.amount_minor > 0]
    result: dict[int, list[Suggestion]] = {t.txn_id: [] for t in txns}

    # Identity signals don't depend on balances: compute once per pair.
    identity: dict[tuple[int, int], tuple[float, float, list[str]]] = {}
    for t in pending:
        for l in leases:
            ref, r1 = reference_score(t, l)
            nam, r2 = name_score(t, l)
            identity[(t.txn_id, l.lease_id)] = (ref, nam, [r for r in (r1, r2) if r])

    def evaluate(t: TxnInput, l: LeaseCandidate) -> Evidence:
        ref, nam, reasons = identity[(t.txn_id, l.lease_id)]
        amt, r3 = amount_score(t.amount_minor, l)
        ev = Evidence(reference=ref, name=nam, amount=amt, timing=timing_score(t.txn_date, l))
        ev.reasons = reasons + ([r3] if r3 else [])
        return ev

    while pending:
        # Score every pending transaction against the current virtual ledger,
        # commit the single most confident pair, update the ledger, repeat.
        best_txn, best_ranked = None, None
        for t in pending:
            ranked = sorted(((ev.score, lid, ev) for lid, ev in
                             ((lid, evaluate(t, l)) for lid, l in state.items())),
                            key=lambda x: x[0], reverse=True)
            if ranked and (best_ranked is None or ranked[0][0] > best_ranked[0][0]):
                best_txn, best_ranked = t, ranked
        if best_txn is None:
            break
        top_score, top_lease, top_ev = best_ranked[0]
        runner_up = best_ranked[1][0] if len(best_ranked) > 1 else 0.0
        suggestions = []
        for rank, (score, lease_id, ev) in enumerate(best_ranked[:top_n], start=1):
            if rank == 1:
                conf = classify(score, runner_up, ev.identity)
            elif score >= MEDIUM_THRESHOLD:
                conf = "medium"
            else:
                break
            suggestions.append(Suggestion(best_txn.txn_id, lease_id, score, conf, ev.reasons, rank))
        result[best_txn.txn_id] = suggestions
        if top_score >= MEDIUM_THRESHOLD:
            state[top_lease] = _consume(state[top_lease], best_txn.amount_minor)
        pending.remove(best_txn)
    return result
