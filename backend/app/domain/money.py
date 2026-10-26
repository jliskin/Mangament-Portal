"""Money helpers. All amounts are integers in minor units (cents/agorot)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_CURRENCY_NOISE = re.compile(r"[^\d,.\-()+]")


class AmountParseError(ValueError):
    pass


def detect_decimal_separator(samples: list[str]) -> str:
    """Infer the decimal separator of a column from its values.

    Bank exports are inconsistent ("1.234,56" in ES/PT/BR, "1,234.56" in US/IL).
    A single value like "1.234" is ambiguous, so we vote across the column:
      * a value containing both separators -> the LAST one is decimal;
      * a separator followed by exactly 1-2 trailing digits -> decimal;
      * a separator followed by exactly 3 digits -> weak evidence of thousands.
    """
    votes = {".": 0.0, ",": 0.0}
    for raw in samples:
        s = _CURRENCY_NOISE.sub("", str(raw or ""))
        if not s:
            continue
        has_dot, has_comma = "." in s, "," in s
        if has_dot and has_comma:
            votes["." if s.rfind(".") > s.rfind(",") else ","] += 3
            continue
        for sep in (".", ","):
            if sep in s:
                tail = s.rsplit(sep, 1)[1].rstrip(")-+")
                if len(tail) in (1, 2):
                    votes[sep] += 2
                elif len(tail) == 3 and s.count(sep) >= 1:
                    other = "," if sep == "." else "."
                    votes[other] += 0.5
    return "," if votes[","] > votes["."] else "."


def parse_amount(raw, decimal_sep: str = ".") -> int:
    """Parse a bank amount string into signed minor units.

    Handles currency symbols, thousands separators, "(123.45)" and "123.45-"
    negative notations. Numeric inputs (from Excel) are taken as-is.
    """
    if raw is None:
        raise AmountParseError("empty amount")
    if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
        return to_minor(Decimal(str(raw)))
    s = str(raw).strip()
    if not s:
        raise AmountParseError("empty amount")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _CURRENCY_NOISE.sub("", s)
    if s.endswith("-"):
        negative, s = True, s[:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    s = s.lstrip("+")
    thousands = "," if decimal_sep == "." else "."
    s = s.replace(thousands, "").replace(decimal_sep, ".")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise AmountParseError(f"invalid amount: {raw!r}") from exc
    minor = to_minor(value)
    return -minor if negative else minor


def to_minor(value: Decimal | str | int | float) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


def fmt(minor: int | None, currency: str = "") -> str:
    """1234567 -> '12,345.67' (UI display)."""
    if minor is None:
        return "—"
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(int(minor)), 100)
    text = f"{sign}{whole:,}.{cents:02d}"
    return f"{text} {currency}".strip()
