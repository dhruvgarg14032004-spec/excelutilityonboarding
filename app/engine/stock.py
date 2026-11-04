"""Stock conversion pipeline: source stock list -> Accobiz stock accounts.

Stages: read -> map columns -> apply Accobiz stock rules (type, gsttype, unit, ledgers,
GST TDS, TCS) using editable settings -> validate. User can override type / exclude rows.
"""
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from .config import RULES, load_yaml, profile, stock_settings, stock_template
from .ledger import Issue
from .normalize import has_keyword, words
from .reader import FileStructureError, cell, check_headers, data_rows, read_sheet
from .transforms import TransformWarning, apply

HSN_RE = re.compile(r"^\d{4}(\d{2}){0,2}$")


@lru_cache
def stock_rules() -> dict:
    return load_yaml(RULES / "accobiz" / "stock_rules.yaml")


def validate_settings(s: dict) -> dict:
    """Checks user-edited settings against the Accobiz dropdowns; returns cleaned dict."""
    opts = stock_template().options
    out = {
        "unit": str(s["unit"]).strip().upper(),
        "purchaseaccount": str(s["purchaseaccount"]).strip(),
        "salesaccount": str(s["salesaccount"]).strip(),
        "gst_tds": {"hsn_prefixes": [], "rate": str(s["gst_tds"]["rate"]).strip()},
        "tcs": {"name_keywords": [], "rate": str(s["tcs"]["rate"]).strip()},
    }
    if out["unit"] not in opts["unit"]:
        raise ValueError(f'Unit "{out["unit"]}" is not in the Accobiz unit list')
    for k in ("purchaseaccount", "salesaccount"):
        if out[k] not in opts[k]:
            raise ValueError(f'"{out[k]}" is not in the Accobiz {k} list')
    for p in s["gst_tds"]["hsn_prefixes"]:
        p = str(p).strip()
        if p:
            if not p.isdigit() or not 2 <= len(p) <= 8:
                raise ValueError(f'HSN prefix "{p}" must be 2-8 digits')
            out["gst_tds"]["hsn_prefixes"].append(p)
    out["tcs"]["name_keywords"] = [str(k).strip().lower() for k in s["tcs"]["name_keywords"] if str(k).strip()]
    for k in ("gst_tds", "tcs"):
        r = out[k]["rate"]
        try:
            if not 0 <= float(r) <= 100:
                raise ValueError
        except ValueError:
            raise ValueError(f"{k} rate must be a number between 0 and 100") from None
    return out


@dataclass
class StockRecord:
    src_row: int
    values: dict[str, str]
    type_reason: str = ""
    include: bool = True
    static_issues: list[Issue] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.values.get("description", "")


class StockJob:
    data_type = "stock"

    def __init__(self, software: str):
        self.id = uuid.uuid4().hex[:12]
        self.software = software
        self.prof = profile(software, "stock")
        self.records: list[StockRecord] = []
        self.dataset_issues: list[Issue] = []
        self.unmapped_columns: list[str] = []
        self.settings = validate_settings(stock_settings())

    def load(self, data: bytes):
        p = self.prof
        lay = p["layout"]
        rows = read_sheet(data, lay["sheet"])
        check_headers(rows, lay["header_row"], p["fingerprint"]["headers"], "Stock file")
        headers = stock_template().headers
        for row_no, row in data_rows(rows, lay["data_start_row"], lay["stop_at_blank_rows"]):
            vals = {h: "" for h in headers}
            rec = StockRecord(row_no, vals)
            for letter, spec in p["columns"].items():
                try:
                    vals[spec["to"]] = apply(cell(row, letter), spec.get("transform"))
                except (TransformWarning, ValueError) as w:
                    vals[spec["to"]] = ""
                    rec.static_issues.append(Issue("error", f'{spec["to"]}: {w}', spec["to"]))
            self.records.append(rec)
        if not self.records:
            raise FileStructureError("Stock file: no item rows found below the header.")
        self.unmapped_columns = [f"{c} {h!r}" for c, h in p.get("unmapped", {}).items()]
        self.apply_rules()
        return self

    # ---------- rules ----------
    def _item_type(self, name: str, hsn: str) -> tuple[str, str]:
        n = words(name)
        for rule in stock_rules()["item_type"]:
            m = rule["match"]
            if m == "default":
                return rule["result"], rule["when"]
            if m.get("hsn_prefix") and hsn.startswith(tuple(m["hsn_prefix"])):
                return rule["result"], rule["when"]
            if m.get("hsn_present") and hsn:
                return rule["result"], rule["when"]
            if m.get("name_keywords"):
                k = has_keyword(n, m["name_keywords"])
                if k:
                    return rule["result"], f'description contains "{k}"'
        raise AssertionError("no default item type")

    def apply_rules(self, keep_user_types: bool = True):
        s = self.settings
        gt = stock_rules()["gst_type"]
        for r in self.records:
            v = r.values
            if not (keep_user_types and r.type_reason == "set by user"):
                v["type"], r.type_reason = self._item_type(r.name, v.get("hsn", ""))
            rate = v.get("gstrate", "")
            v["gsttype"] = gt["zero_rate"] if rate != "" and float(rate) == 0 else gt["otherwise"]
            v["unit"] = s["unit"]
            v["purchaseaccount"] = s["purchaseaccount"]
            v["salesaccount"] = s["salesaccount"]
            hsn = v.get("hsn", "")
            tds = bool(hsn) and hsn.startswith(tuple(s["gst_tds"]["hsn_prefixes"]))
            v["gsttdsapplicable"], v["gsttdsrate"] = ("Yes", s["gst_tds"]["rate"]) if tds else ("No", "")
            tcs = has_keyword(words(r.name), s["tcs"]["name_keywords"])
            v["tcsapplicable"], v["tcsrate"] = ("Yes", s["tcs"]["rate"]) if tcs else ("No", "")
        self.refresh()

    def set_override(self, idx: int, *, item_type=None, include=None):
        r = self.records[idx]
        if item_type is not None:
            if item_type not in stock_template().options["type"]:
                raise ValueError("invalid type")
            r.values["type"], r.type_reason = item_type, "set by user"
        if include is not None:
            r.include = bool(include)
        self.refresh()

    # ---------- validation ----------
    def refresh(self):
        opts = stock_template().options
        counts = Counter(r.name.strip().lower() for r in self.records if r.include)
        codes = Counter(r.values["code"] for r in self.records if r.include and r.values["code"])
        for r in self.records:
            v = r.values
            iss = list(r.static_issues)
            if r.include:
                if not r.name.strip():
                    iss.append(Issue("error", "Item description is blank", "description"))
                elif counts[r.name.strip().lower()] > 1:
                    iss.append(Issue("error", f'Duplicate item "{r.name}"', "description"))
                if v["code"] and codes[v["code"]] > 1:
                    iss.append(Issue("error", f'Duplicate code "{v["code"]}"', "code"))
                if v["gstrate"] != "" and v["gstrate"] not in opts["gstrate"]:
                    iss.append(Issue("error", f'GST rate {v["gstrate"]}% is not in Accobiz list '
                                              f'({", ".join(opts["gstrate"])})', "gstrate"))
                if v["hsn"] and not HSN_RE.match(v["hsn"]):
                    iss.append(Issue("warning", "HSN should be 4, 6 or 8 digits", "hsn"))
            r.issues = iss

    def output_rows(self) -> list[dict]:
        return [dict(r.values) for r in self.records if r.include]

    def can_download(self) -> bool:
        return not any(i.level == "error" for r in self.records if r.include for i in r.issues)

    def summary(self) -> dict:
        inc = [r for r in self.records if r.include]
        v = lambda k, val: sum(1 for r in inc if r.values.get(k) == val)
        return {
            "job_id": self.id, "data_type": "stock", "software": self.prof["software"],
            "source_items": len(self.records), "excluded": len(self.records) - len(inc),
            "output_rows": len(inc),
            "goods": v("type", "Goods"), "services": v("type", "Service"),
            "nil_rated": v("gsttype", "NIL"),
            "gst_tds_yes": v("gsttdsapplicable", "Yes"), "tcs_yes": v("tcsapplicable", "Yes"),
            "errors": sum(1 for r in inc for i in r.issues if i.level == "error"),
            "warnings": sum(1 for r in inc for i in r.issues if i.level == "warning"),
            "dataset_issues": [i.__dict__ for i in self.dataset_issues],
            "unmapped_columns": self.unmapped_columns,
            "settings": self.settings,
            "can_download": self.can_download(),
        }

    def record_view(self, idx: int) -> dict:
        r = self.records[idx]
        return {"idx": idx, "src_row": r.src_row, "include": r.include, "values": r.values,
                "type_reason": r.type_reason, "issues": [i.__dict__ for i in r.issues]}
