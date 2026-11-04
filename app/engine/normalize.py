"""Text / number normalization shared by every stage of the engine."""
import re
from decimal import Decimal, InvalidOperation

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
PIN_RE = re.compile(r"^[1-9]\d{5}$")


def is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def words(s: str) -> str:
    """Lowercase, '&' -> 'and', collapse dotted/spaced acronyms (T.D.S / T D S -> tds),
    punctuation -> space. Returned padded with spaces for whole-word `in` checks."""
    s = str(s).lower().replace("&", " and ")
    s = re.sub(r"\b([a-z])[.\s]+(?=[a-z]\b)", r"\1", s)
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", s).split()) + " "


def has_keyword(padded: str, keywords) -> str | None:
    return next((k for k in keywords if f" {k} " in padded), None)


def ledger_key(s: str) -> str:
    """Key used to compare ledger names against Accobiz prebuilt ledgers."""
    s = words(s)
    for w in (" a c ", " ac ", " account ", " acct "):
        s = s.replace(w, " ")
    return " ".join(s.split())


def to_text(v) -> str:
    """Cell value -> text without float artefacts (100803.0 -> '100803')."""
    if is_blank(v):
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, int):
        return str(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s


def to_decimal(v) -> Decimal:
    """Cell value -> Decimal. Floats go through repr() to keep the shortest exact form."""
    if is_blank(v):
        return Decimal(0)
    if isinstance(v, float):
        return Decimal(repr(v))
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError(f"not a number: {v!r}") from e


def money_text(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):f}"
