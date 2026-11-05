"""Loads the YAML rule files and Accobiz templates (cached)."""
from functools import lru_cache
from pathlib import Path

import openpyxl
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "profiles"
RULES = ROOT / "rules"
TEMPLATES = ROOT / "templates"


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def profile(software: str, name: str) -> dict:
    return load_yaml(PROFILES / software / f"{name}.yaml")


def list_softwares(data_type: str) -> list[dict]:
    out = []
    for d in sorted(p for p in PROFILES.iterdir() if p.is_dir()):
        f = d / f"{data_type}.yaml"
        if f.exists():
            out.append({"id": d.name, "name": load_yaml(f)["software"]})
    return out


class AccobizTemplate:
    """The fixed output format: headers in order + dropdown values from the Options sheet."""

    def __init__(self, filename: str, sheet: str):
        self.path = TEMPLATES / "accobiz" / filename
        self.sheet = sheet
        wb = openpyxl.load_workbook(self.path)
        self.headers = [c.value for c in wb[sheet][1] if c.value]
        self.options: dict[str, list[str]] = {}
        for col in wb["Options"].iter_cols(values_only=True):
            if col[0]:
                self.options[col[0]] = ["" if v is None else str(v) for v in col[1:] if v is not None]


@lru_cache
def ledger_template() -> AccobizTemplate:
    return AccobizTemplate("ledger-accounts.xlsx", "Ledgers")


@lru_cache
def stock_template() -> AccobizTemplate:
    return AccobizTemplate("stock-accounts.xlsx", "StockAccounts")


STOCK_SETTINGS = RULES / "accobiz" / "stock_settings.yaml"


def stock_settings() -> dict:
    """Saved settings from the database (if configured), else the YAML defaults in the repo."""
    from .store import store
    return store().get_settings("stock") or load_yaml(STOCK_SETTINGS)


def save_stock_settings(data: dict):
    from .store import store
    if store().put_settings("stock", data):
        return
    with open(STOCK_SETTINGS, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
