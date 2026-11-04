"""Reusable value transforms referenced by name from source profiles."""
from rapidfuzz import fuzz, process

from .config import ledger_template
from .normalize import money_text, to_decimal, to_text

STATE_ALIASES = {
    "orissa": "Odisha", "pondicherry": "Puducherry", "uttaranchal": "Uttarakhand",
    "new delhi": "Delhi", "nct of delhi": "Delhi", "j and k": "Jammu and Kashmir",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "chattisgarh": "Chhattisgarh", "andaman": "Andaman and Nicobar Islands",
}


class TransformWarning(Exception):
    """Value could not be normalized; carries the value to write instead."""

    def __init__(self, message: str, value: str):
        super().__init__(message)
        self.value = value


def state_normalize(v: str) -> str:
    if not v:
        return ""
    states = ledger_template().options["state"]
    key = " ".join(v.lower().replace("&", " and ").replace("_", " ").split())
    for s in states:
        if s.lower() == key:
            return s
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    best = process.extractOne(key, [s.lower() for s in states], scorer=fuzz.ratio)
    if best and best[1] >= 85:
        return states[best[2]]
    raise TransformWarning(f'state "{v}" not in Accobiz list - left blank', "")


TRANSFORMS = {
    "int_to_text": to_text,
    "trim": lambda v: " ".join(v.split()),
    "upper": str.upper,
    "underscore_to_space": lambda v: " ".join(v.replace("_", " ").split()),
    "state_normalize": state_normalize,
    "decimal_2": lambda v: money_text(to_decimal(v)) if v else "",
}


def apply(value, names: list[str]) -> str:
    v = to_text(value)
    for n in names or []:
        v = TRANSFORMS[n](v)
    return v
