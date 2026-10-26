"""Text normalisation for name / reference matching."""
from __future__ import annotations

import re
import unicodedata

# Tokens that carry no identity information in bank descriptions.
STOPWORDS = {
    # banking verbs / channels (es, pt, en)
    "transferencia", "transf", "trf", "tef", "ted", "doc", "pix", "recibida", "recebido",
    "recibido", "deposito", "dep", "abono", "credito", "cred", "pago", "pagamento", "payment",
    "transfer", "from", "de", "del", "da", "do", "la", "el", "los", "las", "y", "e",
    "inmediata", "spei", "sepa", "ach", "wire", "online", "banca", "movil", "app",
    "alquiler", "renta", "aluguel", "rent", "condominio", "expensas", "cuota", "mes",
    "apto", "apt", "depto", "dpto", "unidad", "unit", "piso", "local",
    # Hebrew banking terms (Israeli statements)
    "העברה", "העברת", "מאת", "זיכוי", "הפקדה", "ביט", "פייבוקס", "בנקאית", "מזומן", "שכירות", "שכר", "דירה",
    "ועד", "בית", "בעמ", 'בע"מ',
    # honorifics / legal forms
    "sr", "sra", "srta", "mr", "mrs", "ms", "dr", "dra", "sa", "srl", "sl", "ltda", "ltd",
    "inc", "llc", "cia", "me", "eireli",
}

_NON_ALNUM = re.compile(r"[^a-z0-9א-ת]+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str | None) -> str:
    """'TRANSF. RECIBIDA - José  Pérez S.A.' -> 'jose perez'"""
    if not text:
        return ""
    t = _NON_ALNUM.sub(" ", strip_accents(text).lower())
    return " ".join(tok for tok in t.split() if tok not in STOPWORDS and not tok.isdigit())


def tokens(text: str | None) -> list[str]:
    if not text:
        return []
    return _NON_ALNUM.sub(" ", strip_accents(text).lower()).split()
