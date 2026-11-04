"""Matches source ledgers against Accobiz prebuilt ledgers (exact / synonym / fuzzy)."""
import re
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz, process

from .config import RULES, load_yaml
from .normalize import ledger_key

FUZZY_MIN = 88


@dataclass
class PrebuiltMatch:
    name: str        # exact Accobiz prebuilt name
    method: str      # exact | synonym | fuzzy
    score: int


@lru_cache
def _data():
    pre = load_yaml(RULES / "accobiz" / "prebuilt_ledgers.yaml")["prebuilt"]
    syn = load_yaml(RULES / "accobiz" / "prebuilt_synonyms.yaml")
    by_key = {ledger_key(p["name"]): p["name"] for p in pre}
    synonyms = {ledger_key(a): target for target, aliases in syn["synonyms"].items() for a in aliases}
    guard = [re.compile(rf"^{t}$") for t in syn["fuzzy_guard_tokens"]]
    return by_key, synonyms, guard


def _identity(key: str, guard) -> set[str]:
    return {t for t in key.split() if any(g.match(t) for g in guard)}


def match(name: str) -> PrebuiltMatch | None:
    by_key, synonyms, guard = _data()
    k = ledger_key(name)
    if k in by_key:
        return PrebuiltMatch(by_key[k], "exact", 100)
    if k in synonyms:
        return PrebuiltMatch(synonyms[k], "synonym", 100)
    best = process.extractOne(k, list(by_key), scorer=fuzz.token_sort_ratio)
    if best and best[1] >= FUZZY_MIN and _identity(k, guard) == _identity(best[0], guard):
        return PrebuiltMatch(by_key[best[0]], "fuzzy", round(best[1]))
    return None
