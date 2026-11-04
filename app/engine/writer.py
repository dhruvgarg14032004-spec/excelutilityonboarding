"""Writes the Accobiz import file (fills the official template) and the conversion report."""
import io
import re

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import AccobizTemplate

NUMERIC_COLUMNS = {"gstrate", "cessrate"}   # Accobiz template stores these as numbers


def write_accobiz(tpl: AccobizTemplate, rows: list[dict]) -> bytes:
    wb = openpyxl.load_workbook(tpl.path)
    ws = wb[tpl.sheet]
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)               # remove sample row(s)
    for r, row in enumerate(rows, start=2):
        for c, h in enumerate(tpl.headers, start=1):
            v = row.get(h, "")
            if h in NUMERIC_COLUMNS and v != "":
                v = float(v) if "." in str(v) else int(v)
            cell = ws.cell(r, c, v)
            if not isinstance(v, (int, float)):
                cell.number_format = "@"
    last = max(500, len(rows) + 1)
    seen = set()
    for dv in list(ws.data_validations.dataValidation):
        letter = re.match(r"[A-Z]+", str(dv.sqref)).group(0)
        if letter in seen:                              # template has duplicate DVs per column
            ws.data_validations.dataValidation.remove(dv)
            continue
        seen.add(letter)
        dv.sqref = openpyxl.worksheet.cell_range.MultiCellRange(f"{letter}2:{letter}{last}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet(wb, title, headers, rows):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, start=1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:500]]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 70)
    ws.freeze_panes = "A2"
    return ws


def write_report(job) -> bytes:
    s = job.summary()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    c = s["controls"]
    _sheet(wb, "Summary", ["Item", "Value"], [
        ["Source software", s["software"]],
        ["Ledgers in source file", s["source_ledgers"]],
        ["Excluded by user", s["excluded"]],
        ["New ledgers written", s["new_ledgers"]],
        ["Merged into Accobiz prebuilt ledgers", s["merged_into_prebuilt"]],
        ["Prebuilt balance rows written", s["prebuilt_rows_written"]],
        ["Total output rows", s["output_rows"]],
        ["Trial balance Dr / Cr", f'{c["tb_debit"]} / {c["tb_credit"]}' if c["tb_loaded"] else "not uploaded"],
        ["Output Dr / Cr", f'{c["out_debit"]} / {c["out_credit"]}'],
        ["Trial balance net / Output net", f'{c["tb_net"]} / {c["out_net"]}'],
        ["Errors", s["errors"]], ["Warnings", s["warnings"]],
        ["Source columns dropped", ", ".join(s["unmapped_columns"])],
    ])
    issues = [["(file)", "", i["level"], i.get("field") or "", i["message"]] for i in s["dataset_issues"]]
    for r in job.records:
        for i in r.issues:
            issues.append([r.src_row, r.name, i.level, i.field or "", i.message])
    _sheet(wb, "Issues", ["Source row", "Ledger", "Level", "Field", "Message"], issues)
    _sheet(wb, "Classification", ["Source row", "Ledger", "Category", "Reason", "annexuredr", "annexurecr", "cashbank"],
           [[r.src_row, r.name, r.category, r.reason,
             *(job.output_row(r)[k] for k in ("annexuredr", "annexurecr", "cashbank"))]
            for r in job.records if r.include and not r.merged])
    pre = []
    for r in job.records:
        if r.prebuilt:
            pre.append([r.src_row, r.name, r.prebuilt.name, r.prebuilt.method, r.prebuilt.score,
                        r.prebuilt_decision if r.include else "excluded",
                        str(r.debit), str(r.credit)])
    _sheet(wb, "Prebuilt matches", ["Source row", "Source ledger", "Accobiz prebuilt", "Match", "Score",
                                    "Decision", "Debit", "Credit"], pre)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_stock_report(job) -> bytes:
    s = job.summary()
    st = s["settings"]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Summary", ["Item", "Value"], [
        ["Source software", s["software"]], ["Items in source file", s["source_items"]],
        ["Excluded by user", s["excluded"]], ["Items written", s["output_rows"]],
        ["Goods / Services", f'{s["goods"]} / {s["services"]}'], ["NIL rated", s["nil_rated"]],
        ["GST TDS applicable", s["gst_tds_yes"]], ["TCS applicable", s["tcs_yes"]],
        ["Errors", s["errors"]], ["Warnings", s["warnings"]],
        ["Unit used", st["unit"]],
        ["Purchase / Sales account", f'{st["purchaseaccount"]} / {st["salesaccount"]}'],
        ["GST TDS HSNs / rate", f'{", ".join(st["gst_tds"]["hsn_prefixes"])} / {st["gst_tds"]["rate"]}%'],
        ["TCS words / rate", f'{", ".join(st["tcs"]["name_keywords"])} / {st["tcs"]["rate"]}%'],
        ["Source columns dropped", ", ".join(s["unmapped_columns"])],
    ])
    _sheet(wb, "Issues", ["Source row", "Item", "Level", "Field", "Message"],
           [[r.src_row, r.name, i.level, i.field or "", i.message] for r in job.records for i in r.issues])
    _sheet(wb, "Rules applied", ["Source row", "Item", "HSN", "Type", "Type reason", "GST type",
                                 "GST TDS", "GST TDS rate", "TCS", "TCS rate", "Included"],
           [[r.src_row, r.name, r.values["hsn"], r.values["type"], r.type_reason, r.values["gsttype"],
             r.values["gsttdsapplicable"], r.values["gsttdsrate"], r.values["tcsapplicable"],
             r.values["tcsrate"], "Yes" if r.include else "No"] for r in job.records])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
