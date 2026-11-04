"""Assigns each ledger an Accobiz category (annexuredr / annexurecr / cashbank)
using rules/accobiz/ledger_annexure.yaml. Name-based rules only."""
from functools import lru_cache

from .config import RULES, load_yaml, ledger_template
from .normalize import GSTIN_RE, has_keyword, words


@lru_cache
def categories() -> list[dict]:
    cats = load_yaml(RULES / "accobiz" / "ledger_annexure.yaml")["categories"]
    opts = ledger_template().options
    for c in cats:  # rule values must be exact dropdown strings
        for col in ("annexuredr", "annexurecr"):
            if c[col] not in opts[col]:
                raise ValueError(f"{c['id']}: {c[col]!r} is not in Accobiz {col} dropdown")
    return cats


def category(cat_id: str) -> dict:
    return next(c for c in categories() if c["id"] == cat_id)


def classify(name: str, gstin: str) -> tuple[str, str]:
    """Returns (category_id, reason)."""
    n = words(name)
    for c in categories():
        m = c["match"]
        if m == "default":
            return c["id"], "default"
        if m.get("has_valid_gstin"):
            if GSTIN_RE.match(gstin or ""):
                return c["id"], "has GSTIN"
            continue
        if has_keyword(n, m.get("exclude_keywords", [])):
            continue
        k = has_keyword(n, m["name_keywords"])
        if k:
            return c["id"], f'name contains "{k}"'
    raise AssertionError("no default category")


def category_values(cat_id: str) -> dict:
    c = category(cat_id)
    return {"annexuredr": c["annexuredr"], "annexurecr": c["annexurecr"],
            "cashbank": c.get("set", {}).get("cashbank", "NO")}
