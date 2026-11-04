import io
from pathlib import Path

import openpyxl
import pytest

from app.engine.config import stock_template
from app.engine.stock import StockJob, validate_settings
from app.engine.writer import write_accobiz

FIX = Path(__file__).parent / "fixtures" / "shakun"


def job():
    return StockJob("shakun").load((FIX / "stock.xls").read_bytes())


def test_summary():
    s = job().summary()
    assert (s["source_items"], s["output_rows"], s["errors"]) == (26, 26, 0)
    assert (s["goods"], s["services"], s["nil_rated"]) == (26, 0, 3)


def test_rules():
    j = job()
    by = {r.name: r.values for r in j.records}
    scrap = by["Scrap"]
    assert (scrap["gsttdsapplicable"], scrap["gsttdsrate"]) == ("Yes", j.settings["gst_tds"]["rate"])
    assert (scrap["tcsapplicable"], scrap["tcsrate"]) == ("Yes", j.settings["tcs"]["rate"])
    assert by["Wastage"]["gsttype"] == "NIL" and by["Wastage"]["hsn"] == ""
    assert by["ALUMINIUM BAR/ INGOT"]["hsn"] == "7601"
    assert by["ALUMINIUM BAR/ INGOT"]["purchaserate"] == "112.00"
    for v in by.values():
        assert v["unit"] == "MTS"
        assert (v["purchaseaccount"], v["salesaccount"]) == ("PURCHASE TRADED GOODS", "SALE TRADED GOODS")
        assert v["cessrate"] == v["openingqty"] == v["openingvalue"] == v["batchno"] == v["expirydate"] == ""


def test_changed_rate_applies():
    j = job()
    j.settings = {**j.settings, "tcs": {"name_keywords": ["scrap"], "rate": "1"}}
    j.apply_rules()
    assert next(r for r in j.records if r.name == "Scrap").values["tcsrate"] == "1"


def test_service_and_override():
    j = job()
    assert j._item_type("Job Work Charges", "")[0] == "Service"
    assert j._item_type("Anything", "998898")[0] == "Service"
    j.set_override(0, item_type="Service")
    j.apply_rules()
    assert j.records[0].values["type"] == "Service"


def test_invalid_settings_rejected():
    s = validate_settings({"unit": "MTS", "purchaseaccount": "PURCHASE TRADED GOODS",
                           "salesaccount": "SALE TRADED GOODS",
                           "gst_tds": {"hsn_prefixes": ["7204"], "rate": "2"},
                           "tcs": {"name_keywords": ["scrap"], "rate": "2"}})
    with pytest.raises(ValueError):
        validate_settings({**s, "unit": "XYZ"})
    with pytest.raises(ValueError):
        validate_settings({**s, "tcs": {"name_keywords": [], "rate": "abc"}})


def test_output_file():
    data = write_accobiz(stock_template(), job().output_rows())
    ws = openpyxl.load_workbook(io.BytesIO(data))["StockAccounts"]
    assert ws.max_row == 27
    hdr = [c.value for c in ws[1]]
    row = dict(zip(hdr, (c.value for c in ws[2])))
    assert row["gstrate"] == 18 and row["code"] == "10275"
