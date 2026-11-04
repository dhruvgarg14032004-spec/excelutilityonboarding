from pathlib import Path

import openpyxl
import io

from app.engine.config import ledger_template
from app.engine.ledger import LedgerJob
from app.engine.writer import write_accobiz

FIX = Path(__file__).parent / "fixtures" / "shakun"


def job():
    return LedgerJob("shakun").load((FIX / "ledgers.xls").read_bytes(), (FIX / "trial.xls").read_bytes())


def test_counts_and_controls():
    s = job().summary()
    assert s["source_ledgers"] == 952
    assert s["merged_into_prebuilt"] == 45
    assert s["new_ledgers"] + s["merged_into_prebuilt"] == 952
    c = s["controls"]
    assert c["tb_debit"] == c["out_debit"] == "178802050.88"
    assert c["tb_credit"] == c["out_credit"] == "178802050.88"


def test_duplicate_blocks_download_until_excluded():
    j = job()
    dups = [i for i, r in enumerate(j.records) if r.name == "Cash Receipt No."]
    assert len(dups) == 2 and not j.can_download()
    j.set_override(dups[1], include=False)
    assert j.can_download()


def test_rules_and_prebuilt():
    j = job()
    by = {r.name: r for r in j.records}
    assert by["Union Bank Of India"].category == "bank"
    assert by["TDS 194-C Payable"].category == "tax"
    assert by["GST (CGST) A/c"].prebuilt.name == "GST CGST"
    assert by["TCS RECOVERABLE"].prebuilt is None          # fuzzy guard
    pre = {p["name"]: p for p in j.prebuilt_rows()}
    assert set(pre["SALE RAW MATERIAL"]["members"]) == {"Sale Raw Material", "Sale (Raw Material)"}


def test_output_file():
    j = job()
    data = write_accobiz(ledger_template(), j.output_rows())
    ws = openpyxl.load_workbook(io.BytesIO(data))["Ledgers"]
    hdr = [c.value for c in ws[1]]
    rows = [dict(zip(hdr, (c.value for c in r))) for r in ws.iter_rows(min_row=2)]
    assert len(rows) == len(j.output_rows())
    a = next(r for r in rows if r["name"] == "A. M. Jindal Tubes")
    assert (a["code"], a["pin"], a["state"], a["country"]) == ("100976", "147301", "Punjab", "India")
    assert all(str(dv.sqref).endswith(str(ws.max_row)) for dv in ws.data_validations.dataValidation)
