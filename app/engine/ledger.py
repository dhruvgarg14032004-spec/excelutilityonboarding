"""Ledger conversion pipeline: source ledger file (+ optional trial balance) -> Accobiz ledgers.

Stages: read -> map columns -> attach TB balances -> prebuilt match -> classify -> validate.
User overrides (category / prebuilt decision / include) re-run validation only.
"""
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from . import ai_review, classifier, prebuilt
from .config import ledger_template, profile
from .normalize import GSTIN_RE, PAN_RE, PIN_RE, is_blank, money_text, to_decimal, to_text
from .reader import FileStructureError, cell, check_headers, data_rows, read_sheet
from .transforms import TransformWarning, apply

ZERO = Decimal(0)


@dataclass
class Issue:
    level: str               # error | warning
    message: str
    field: str | None = None


@dataclass
class LedgerRecord:
    src_row: int
    values: dict[str, str]
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    category: str = "other"
    reason: str = ""
    prebuilt: prebuilt.PrebuiltMatch | None = None
    prebuilt_decision: str = "new"     # merge | new
    include: bool = True
    static_issues: list[Issue] = field(default_factory=list)   # from transforms
    ai: dict | None = None             # {category, confidence, reason, status: pending|accepted|dismissed}
    issues: list[Issue] = field(default_factory=list)          # recomputed

    @property
    def name(self) -> str:
        return self.values.get("name", "")

    @property
    def merged(self) -> bool:
        return self.prebuilt is not None and self.prebuilt_decision == "merge"


@dataclass
class Controls:
    tb_loaded: bool = False
    tb_debit: Decimal = ZERO
    tb_credit: Decimal = ZERO
    tb_total_row: tuple[Decimal, Decimal] | None = None
    out_debit: Decimal = ZERO
    out_credit: Decimal = ZERO


class LedgerJob:
    def __init__(self, software: str):
        self.id = uuid.uuid4().hex[:12]
        self.software = software
        self.prof = profile(software, "ledger")
        self.records: list[LedgerRecord] = []
        self.dataset_issues: list[Issue] = []
        self.controls = Controls()
        self.unmapped_columns: list[str] = []
        self.ai_reviewed = 0

    # ---------- build ----------
    def load(self, ledger_bytes: bytes, tb_bytes: bytes | None):
        self._read_ledgers(ledger_bytes)
        if tb_bytes:
            self._read_tb(tb_bytes)
        for r in self.records:
            r.prebuilt = prebuilt.match(r.name)
            r.prebuilt_decision = "merge" if r.prebuilt and r.prebuilt.method != "fuzzy" else "new"
            r.category, r.reason = classifier.classify(r.name, r.values.get("gstin", ""))
        self.refresh()
        return self

    def _read_ledgers(self, data: bytes):
        p = self.prof
        lay = p["layout"]
        rows = read_sheet(data, lay["sheet"])
        check_headers(rows, lay["header_row"], p["fingerprint"]["headers"], "Ledger file")
        headers = ledger_template().headers
        cols = p["columns"]
        joins = defaultdict(list)
        for row_no, row in data_rows(rows, lay["data_start_row"], lay["stop_at_blank_rows"]):
            vals = {h: "" for h in headers}
            vals.update(p.get("defaults", {}))
            rec = LedgerRecord(row_no, vals)
            joins.clear()
            for letter, spec in cols.items():
                raw = cell(row, letter)
                try:
                    v = apply(raw, spec.get("transform"))
                except TransformWarning as w:
                    v = w.value
                    rec.static_issues.append(Issue("warning", str(w), spec["to"]))
                if "join_order" in spec:
                    joins[spec["to"]].append((spec["join_order"], v))
                else:
                    vals[spec["to"]] = v
            for target, parts in joins.items():
                sep = p["join"][target]["separator"]
                vals[target] = sep.join(v for _, v in sorted(parts) if v)
            self.records.append(rec)
        if not self.records:
            raise FileStructureError("Ledger file: no ledger rows found below the header.")
        self.unmapped_columns = [f"{c} {h!r}" for c, h in p.get("unmapped", {}).items()]

    def _read_tb(self, data: bytes):
        tp = profile(self.software, "trial_balance")
        lay = tp["layout"]
        rows = read_sheet(data, lay["sheet"])
        check_headers(rows, lay["header_row"], tp["fingerprint"]["headers"], "Trial balance")
        role = {spec["role"]: letter for letter, spec in tp["columns"].items()}
        c = self.controls
        c.tb_loaded = True
        by_exact = defaultdict(list)
        by_loose = defaultdict(list)
        for r in self.records:
            by_exact[r.name].append(r)
            by_loose[" ".join(r.name.lower().split())].append(r)
        for row_no, row in data_rows(rows, lay["data_start_row"], 10**9):
            name = to_text(cell(row, role["ledger_name"]))
            dr_raw, cr_raw = cell(row, role["debit"]), cell(row, role["credit"])
            if is_blank(dr_raw) and is_blank(cr_raw):
                continue                                   # group header
            if not name:
                continue                                   # subtotal
            try:
                dr, cr = to_decimal(dr_raw), to_decimal(cr_raw)
            except ValueError as e:
                self.dataset_issues.append(Issue("error", f"Trial balance row {row_no}: {e}"))
                continue
            if name.upper() == "TOTAL":
                c.tb_total_row = (dr, cr)
                continue
            if (dr and cr) or dr < 0 or cr < 0:
                self.dataset_issues.append(Issue(
                    "error", f'Trial balance row {row_no} "{name}": needs exactly one positive Dr or Cr amount'))
                continue
            for d in (dr, cr):
                if d != d.quantize(Decimal("0.01")):
                    self.dataset_issues.append(Issue(
                        "warning", f'Trial balance "{name}": amount {d} has more than 2 decimals'))
            c.tb_debit += dr
            c.tb_credit += cr
            targets = by_exact.get(name) or by_loose.get(" ".join(name.lower().split()))
            if not targets:
                self.dataset_issues.append(Issue(
                    "error", f'Trial balance ledger "{name}" (row {row_no}) not found in the ledger file'))
                continue
            t = targets[0]
            t.debit += dr
            t.credit += cr
        if c.tb_total_row and (c.tb_total_row[0].quantize(Decimal("0.01")) != c.tb_debit.quantize(Decimal("0.01"))
                               or c.tb_total_row[1].quantize(Decimal("0.01")) != c.tb_credit.quantize(Decimal("0.01"))):
            self.dataset_issues.append(Issue("warning", "Trial balance: sum of ledgers does not equal the TOTAL row"))
        if c.tb_debit != c.tb_credit:
            self.dataset_issues.append(Issue(
                "warning", f"Trial balance does not balance: Dr {money_text(c.tb_debit)} vs Cr {money_text(c.tb_credit)}"))

    # ---------- AI review ----------
    def ai_candidates(self) -> list[int]:
        return [i for i, r in enumerate(self.records)
                if r.include and not r.merged and r.category == "other" and r.reason == "default"]

    def run_ai_review(self, client=None) -> int:
        idx = self.ai_candidates()
        items = [{"index": i, "name": self.records[i].name,
                  "has_gstin": bool(self.records[i].values.get("gstin")),
                  "side": "Dr" if self.records[i].debit > self.records[i].credit
                          else "Cr" if self.records[i].credit > self.records[i].debit else ""}
                 for i in idx]
        result = ai_review.review(items, client)
        for i, s in result.items():
            r = self.records[i]
            r.ai = {"category": s.category, "confidence": s.confidence, "reason": s.reason,
                    "status": "pending" if s.category != r.category else "confirmed"}
        self.ai_reviewed = len(result)
        return len(result)

    def accept_ai(self, indexes: list[int] | None = None, min_confidence: str | None = None) -> int:
        rank = {"low": 0, "medium": 1, "high": 2}
        n = 0
        for i, r in enumerate(self.records):
            a = r.ai
            if not a or a["status"] != "pending":
                continue
            if indexes is not None and i not in indexes:
                continue
            if min_confidence and rank[a["confidence"]] < rank[min_confidence]:
                continue
            r.category, r.reason = a["category"], f'AI ({a["confidence"]}): {a["reason"]}'
            a["status"] = "accepted"
            n += 1
        self.refresh()
        return n

    def dismiss_ai(self, idx: int):
        if self.records[idx].ai:
            self.records[idx].ai["status"] = "dismissed"

    def ai_summary(self) -> dict:
        st = [r.ai["status"] for r in self.records if r.ai]
        conf = [r.ai["confidence"] for r in self.records if r.ai and r.ai["status"] == "pending"]
        return {"available": ai_review.available(), "candidates": len(self.ai_candidates()),
                "reviewed": self.ai_reviewed, "pending": st.count("pending"),
                "pending_high": conf.count("high"),
                "accepted": st.count("accepted"), "confirmed_other": st.count("confirmed")}

    # ---------- user overrides ----------
    def set_override(self, idx: int, *, category=None, prebuilt_decision=None, include=None):
        r = self.records[idx]
        if category is not None:
            classifier.category(category)  # validates id
            r.category, r.reason = category, "set by user"
            if r.ai and r.ai["status"] == "pending":
                r.ai["status"] = "dismissed"
        if prebuilt_decision is not None:
            if prebuilt_decision not in ("merge", "new") or not r.prebuilt:
                raise ValueError("invalid prebuilt decision")
            r.prebuilt_decision = prebuilt_decision
        if include is not None:
            r.include = bool(include)
        self.refresh()

    # ---------- validation + output ----------
    def _balance_fields(self, net: Decimal) -> dict:
        if net > 0:
            return {"openingbalance": money_text(net), "balancetype": "DEBIT"}
        if net < 0:
            return {"openingbalance": money_text(-net), "balancetype": "CREDIT"}
        return {"openingbalance": "0.00", "balancetype": ""}

    def output_row(self, r: LedgerRecord) -> dict:
        row = dict(r.values)
        row.update(classifier.category_values(r.category))
        row.update(self._balance_fields(r.debit - r.credit))
        return row

    def prebuilt_rows(self) -> list[dict]:
        groups = defaultdict(list)
        for r in self.records:
            if r.include and r.merged:
                groups[r.prebuilt.name].append(r)
        out = []
        for name, members in groups.items():
            net = sum((m.debit - m.credit for m in members), ZERO)
            out.append({"name": name, "net": net, "members": [m.name for m in members],
                        "written": net != 0})
        return out

    def output_rows(self) -> list[dict]:
        headers = ledger_template().headers
        rows = [self.output_row(r) for r in self.records if r.include and not r.merged]
        for p in self.prebuilt_rows():
            if p["written"]:
                row = {h: "" for h in headers}
                row["name"] = p["name"]
                row.update(self._balance_fields(p["net"]))
                rows.append(row)
        return rows

    def refresh(self):
        opts = ledger_template().options
        new = [r for r in self.records if r.include and not r.merged]
        counts = Counter(r.name.strip().lower() for r in new)
        prebuilt_names = {p["name"].lower() for p in self.prebuilt_rows() if p["written"]}
        for r in self.records:
            iss = list(r.static_issues)
            v = r.values
            if r.include and not r.merged:
                if not r.name.strip():
                    iss.append(Issue("error", "Ledger name is blank", "name"))
                elif counts[r.name.strip().lower()] > 1:
                    iss.append(Issue("error", f'Duplicate ledger name "{r.name}"', "name"))
                elif r.name.strip().lower() in prebuilt_names:
                    iss.append(Issue("error", "Same name as an Accobiz prebuilt ledger row", "name"))
                if v.get("gstin") and not GSTIN_RE.match(v["gstin"]):
                    iss.append(Issue("warning", "GSTIN format looks invalid", "gstin"))
                if v.get("pan") and not PAN_RE.match(v["pan"]):
                    iss.append(Issue("warning", "PAN format looks invalid", "pan"))
                elif v.get("pan") and GSTIN_RE.match(v.get("gstin", "")) and v["gstin"][2:12] != v["pan"]:
                    iss.append(Issue("warning", "PAN does not match GSTIN", "pan"))
                if v.get("pin") and not PIN_RE.match(v["pin"]):
                    iss.append(Issue("warning", "PIN code should be 6 digits", "pin"))
                if v.get("state") and v["state"] not in opts["state"]:
                    iss.append(Issue("error", f'state "{v["state"]}" not in Accobiz dropdown', "state"))
            if r.prebuilt and r.prebuilt.method == "fuzzy" and r.include:
                iss.append(Issue("warning", f'Possibly the Accobiz prebuilt ledger "{r.prebuilt.name}" '
                                            f"({r.prebuilt.score}% similar) - confirm", "name"))
            if not r.include and (r.debit or r.credit):
                iss.append(Issue("warning", "Excluded but has an opening balance - totals will not match"))
            r.issues = iss
        c = self.controls
        c.out_debit = c.out_credit = ZERO
        for row in self.output_rows():
            amt = Decimal(row["openingbalance"] or "0")
            if row["balancetype"] == "DEBIT":
                c.out_debit += amt
            elif row["balancetype"] == "CREDIT":
                c.out_credit += amt

    # ---------- reporting ----------
    @property
    def control_issues(self) -> list[Issue]:
        c = self.controls
        if not c.tb_loaded:
            return []
        if (c.out_debit - c.out_credit) != (c.tb_debit - c.tb_credit):
            return [Issue("error", "Output net balance (Dr - Cr) does not equal trial balance net")]
        return []

    def can_download(self) -> bool:
        if any(i.level == "error" for i in self.dataset_issues + self.control_issues):
            return False
        return not any(i.level == "error" for r in self.records if r.include for i in r.issues)

    def summary(self) -> dict:
        recs = self.records
        inc = [r for r in recs if r.include]
        c = self.controls
        prows = self.prebuilt_rows()
        return {
            "job_id": self.id,
            "software": self.prof["software"],
            "source_ledgers": len(recs),
            "excluded": len(recs) - len(inc),
            "new_ledgers": sum(1 for r in inc if not r.merged),
            "merged_into_prebuilt": sum(1 for r in inc if r.merged),
            "prebuilt_rows_written": sum(1 for p in prows if p["written"]),
            "output_rows": len(self.output_rows()),
            "by_category": dict(Counter(r.category for r in inc if not r.merged)),
            "errors": sum(1 for r in inc for i in r.issues if i.level == "error")
                      + sum(1 for i in self.dataset_issues + self.control_issues if i.level == "error"),
            "warnings": sum(1 for r in inc for i in r.issues if i.level == "warning")
                        + sum(1 for i in self.dataset_issues if i.level == "warning"),
            "dataset_issues": [i.__dict__ for i in self.dataset_issues + self.control_issues],
            "unmapped_columns": self.unmapped_columns,
            "controls": {
                "tb_loaded": c.tb_loaded,
                "tb_debit": money_text(c.tb_debit), "tb_credit": money_text(c.tb_credit),
                "out_debit": money_text(c.out_debit), "out_credit": money_text(c.out_credit),
                "tb_net": money_text(c.tb_debit - c.tb_credit),
                "out_net": money_text(c.out_debit - c.out_credit),
            },
            "prebuilt": [{**p, "net": money_text(p["net"])} for p in prows],
            "can_download": self.can_download(),
            "ai": self.ai_summary(),
        }

    def record_view(self, idx: int) -> dict:
        r = self.records[idx]
        return {
            "idx": idx, "src_row": r.src_row, "include": r.include,
            "values": self.output_row(r),
            "category": r.category, "reason": r.reason,
            "prebuilt": r.prebuilt.__dict__ if r.prebuilt else None,
            "prebuilt_decision": r.prebuilt_decision,
            "ai": r.ai,
            "merged": r.merged,
            "issues": [i.__dict__ for i in r.issues],
        }
