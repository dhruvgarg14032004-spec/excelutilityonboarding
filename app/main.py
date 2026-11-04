"""Accobiz Excel Conversion Utility - API + static UI."""
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")   # ANTHROPIC_API_KEY lives here, never in code

from .engine import ai_review, classifier  # noqa: E402
from .engine.config import (ledger_template, list_softwares, save_stock_settings, stock_settings,
                            stock_template)
from .engine.ledger import LedgerJob
from .engine.reader import FileStructureError
from .engine.stock import StockJob, validate_settings
from .engine.writer import write_accobiz, write_report, write_stock_report

MAX_UPLOAD = 25 * 1024 * 1024
JOB_TTL = 4 * 3600
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="Accobiz Excel Conversion Utility")
JOBS: dict[str, tuple[float, LedgerJob | StockJob]] = {}


def _job(job_id: str, kind=LedgerJob):
    now = time.time()
    for k in [k for k, (t, _) in JOBS.items() if now - t > JOB_TTL]:
        del JOBS[k]
    if job_id not in JOBS:
        raise HTTPException(404, "Conversion session expired - please upload again.")
    JOBS[job_id] = (now, JOBS[job_id][1])
    job = JOBS[job_id][1]
    if not isinstance(job, kind):
        raise HTTPException(404, "No such conversion.")
    return job


async def _read(f: UploadFile | None) -> bytes | None:
    if f is None or not f.filename:
        return None
    if not f.filename.lower().endswith((".xls", ".xlsx", ".xlsb")):
        raise HTTPException(400, f"{f.filename}: only .xls / .xlsx / .xlsb files are supported.")
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, f"{f.filename}: file is larger than 25 MB.")
    return data


@app.get("/api/meta/softwares")
def softwares(data_type: str = "ledger"):
    return list_softwares(data_type)


@app.get("/api/meta/categories")
def categories():
    return [{"id": c["id"], "label": c["label"], "annexuredr": c["annexuredr"],
             "annexurecr": c["annexurecr"]} for c in classifier.categories()]


@app.post("/api/ledger/jobs")
async def create_ledger_job(software: str = Form(...), ledger_file: UploadFile = File(...),
                            tb_file: UploadFile | None = File(None)):
    if software not in {s["id"] for s in list_softwares("ledger")}:
        raise HTTPException(400, "Unknown software.")
    ledger, tb = await _read(ledger_file), await _read(tb_file)
    try:
        job = LedgerJob(software).load(ledger, tb)
    except FileStructureError as e:
        raise HTTPException(422, str(e))
    JOBS[job.id] = (time.time(), job)
    return job.summary()


@app.get("/api/ledger/jobs/{job_id}")
def job_summary(job_id: str):
    return _job(job_id).summary()


@app.get("/api/ledger/jobs/{job_id}/mapping")
def job_mapping(job_id: str):
    job = _job(job_id)
    p = job.prof
    cols = [{"source_col": c, "source_header": s.get("header", ""), "target": s["to"]}
            for c, s in p["columns"].items()]
    mapped = {s["to"] for s in p["columns"].values()}
    derived = {"openingbalance": "Trial balance (Debit / Credit)",
               "balancetype": "Trial balance (Debit / Credit)",
               "annexuredr": "Classification rules", "annexurecr": "Classification rules",
               "cashbank": "Classification rules"}
    derived.update({k: f'Default "{v}"' for k, v in p.get("defaults", {}).items()})
    targets = [{"target": h,
                "source": ", ".join(f'{c["source_col"]} ({c["source_header"] or "no header"})'
                                    for c in cols if c["target"] == h)
                          or derived.get(h, ""),
                "kind": "column" if h in mapped else ("derived" if h in derived else "blank")}
               for h in ledger_template().headers]
    return {"targets": targets, "dropped": job.unmapped_columns}


@app.get("/api/ledger/jobs/{job_id}/records")
def job_records(job_id: str, filter: str = "all", q: str = "", page: int = 1, size: int = 50):
    job = _job(job_id)
    idx = range(len(job.records))
    rs = job.records

    def keep(i):
        r = rs[i]
        if q and q.lower() not in r.name.lower():
            return False
        if filter == "errors":
            return any(x.level == "error" for x in r.issues)
        if filter == "issues":
            return bool(r.issues)
        if filter == "prebuilt":
            return r.prebuilt is not None
        if filter == "excluded":
            return not r.include
        if filter == "ai":
            return bool(r.ai) and r.ai["status"] == "pending"
        if filter.startswith("category:"):
            return not r.merged and r.category == filter.split(":", 1)[1]
        return True

    sel = [i for i in idx if keep(i)]
    size = max(10, min(size, 500))
    start = (max(page, 1) - 1) * size
    return {"total": len(sel), "page": page, "size": size,
            "rows": [job.record_view(i) for i in sel[start:start + size]]}


class Override(BaseModel):
    category: str | None = None
    prebuilt_decision: str | None = None
    include: bool | None = None


@app.patch("/api/ledger/jobs/{job_id}/records/{idx}")
def override(job_id: str, idx: int, body: Override):
    job = _job(job_id)
    if not 0 <= idx < len(job.records):
        raise HTTPException(404, "No such row.")
    try:
        job.set_override(idx, **body.model_dump(exclude_none=True))
    except (ValueError, StopIteration) as e:
        raise HTTPException(400, str(e) or "Invalid value.")
    return {"record": job.record_view(idx), "summary": job.summary()}


@app.get("/api/ledger/jobs/{job_id}/download")
def download(job_id: str):
    job = _job(job_id)
    if not job.can_download():
        raise HTTPException(409, "Fix or exclude all errors before downloading.")
    data = write_accobiz(ledger_template(), job.output_rows())
    return Response(data, media_type=XLSX, headers={
        "Content-Disposition": f'attachment; filename="accobiz-ledger-accounts-{job.software}.xlsx"'})


@app.get("/api/ledger/jobs/{job_id}/report")
def report(job_id: str):
    job = _job(job_id)
    return Response(write_report(job), media_type=XLSX, headers={
        "Content-Disposition": f'attachment; filename="conversion-report-{job.software}.xlsx"'})


# ---------------- AI review (ledger "other") ----------------
@app.post("/api/ledger/jobs/{job_id}/ai-review")
def ai_review_run(job_id: str):
    job = _job(job_id)
    if not ai_review.available():
        raise HTTPException(400, "AI review is not set up: add ANTHROPIC_API_KEY to the .env file and restart.")
    try:
        n = job.run_ai_review()
    except anthropic.AuthenticationError:
        raise HTTPException(400, "The Anthropic API key was rejected - check ANTHROPIC_API_KEY in .env.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "AI service is busy (rate limited) - try again in a minute.")
    except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
        raise HTTPException(502, f"AI service error - try again later ({type(e).__name__}).")
    return {"reviewed": n, "summary": job.summary()}


class AiAccept(BaseModel):
    indexes: list[int] | None = None
    min_confidence: str | None = None


@app.post("/api/ledger/jobs/{job_id}/ai-accept")
def ai_accept(job_id: str, body: AiAccept):
    job = _job(job_id)
    if body.min_confidence not in (None, "low", "medium", "high"):
        raise HTTPException(400, "Invalid confidence.")
    n = job.accept_ai(body.indexes, body.min_confidence)
    return {"accepted": n, "summary": job.summary()}


@app.post("/api/ledger/jobs/{job_id}/ai-dismiss/{idx}")
def ai_dismiss(job_id: str, idx: int):
    job = _job(job_id)
    if not 0 <= idx < len(job.records):
        raise HTTPException(404, "No such row.")
    job.dismiss_ai(idx)
    return {"record": job.record_view(idx), "summary": job.summary()}


# ---------------- stock ----------------
@app.post("/api/stock/jobs")
async def create_stock_job(software: str = Form(...), stock_file: UploadFile = File(...)):
    if software not in {s["id"] for s in list_softwares("stock")}:
        raise HTTPException(400, "Unknown software.")
    data = await _read(stock_file)
    try:
        job = StockJob(software).load(data)
    except FileStructureError as e:
        raise HTTPException(422, str(e))
    JOBS[job.id] = (time.time(), job)
    return job.summary()


@app.get("/api/stock/jobs/{job_id}")
def stock_summary(job_id: str):
    return _job(job_id, StockJob).summary()


@app.get("/api/stock/jobs/{job_id}/mapping")
def stock_mapping(job_id: str):
    job = _job(job_id, StockJob)
    by_target = {s["to"]: f'{c} ({s.get("header") or "no header"})' for c, s in job.prof["columns"].items()}
    st = job.settings
    words = ", ".join(f'"{k}"' for k in st["tcs"]["name_keywords"])
    rules = {
        "type": "Rule: SAC 99xx = Service, HSN present = Goods, service words = Service, else Goods",
        "gsttype": "Rule: GST rate 0 = NIL, otherwise REGULAR",
        "unit": f'Setting: {st["unit"]}',
        "purchaseaccount": f'Setting: {st["purchaseaccount"]}',
        "salesaccount": f'Setting: {st["salesaccount"]}',
        "gsttdsapplicable": "Rule: HSN in GST TDS list = Yes",
        "gsttdsrate": f'Setting: {st["gst_tds"]["rate"]}% when applicable',
        "tcsapplicable": f"Rule: description contains {words} = Yes",
        "tcsrate": f'Setting: {st["tcs"]["rate"]}% when applicable',
    }
    targets = [{"target": h, "source": by_target.get(h) or rules.get(h, ""),
                "kind": "column" if h in by_target else ("derived" if h in rules else "blank")}
               for h in stock_template().headers]
    return {"targets": targets, "dropped": job.unmapped_columns}


@app.get("/api/stock/jobs/{job_id}/records")
def stock_records(job_id: str, filter: str = "all", q: str = "", page: int = 1, size: int = 50):
    job = _job(job_id, StockJob)

    def keep(r):
        if q and q.lower() not in r.name.lower():
            return False
        v = r.values
        return {"errors": any(i.level == "error" for i in r.issues), "issues": bool(r.issues),
                "excluded": not r.include, "service": v["type"] == "Service",
                "tds": v["gsttdsapplicable"] == "Yes" or v["tcsapplicable"] == "Yes"}.get(filter, True)

    sel = [i for i, r in enumerate(job.records) if keep(r)]
    size = max(10, min(size, 500))
    start = (max(page, 1) - 1) * size
    return {"total": len(sel), "page": page, "size": size,
            "rows": [job.record_view(i) for i in sel[start:start + size]]}


class StockOverride(BaseModel):
    item_type: str | None = None
    include: bool | None = None


@app.patch("/api/stock/jobs/{job_id}/records/{idx}")
def stock_override(job_id: str, idx: int, body: StockOverride):
    job = _job(job_id, StockJob)
    if not 0 <= idx < len(job.records):
        raise HTTPException(404, "No such row.")
    try:
        job.set_override(idx, **body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"record": job.record_view(idx), "summary": job.summary()}


@app.post("/api/stock/jobs/{job_id}/reapply")
def stock_reapply(job_id: str):
    """Re-applies the saved settings (rates, HSN list, unit...) to an open conversion."""
    job = _job(job_id, StockJob)
    job.settings = validate_settings(stock_settings())
    job.apply_rules()
    return job.summary()


@app.get("/api/stock/jobs/{job_id}/download")
def stock_download(job_id: str):
    job = _job(job_id, StockJob)
    if not job.can_download():
        raise HTTPException(409, "Fix or exclude all errors before downloading.")
    return Response(write_accobiz(stock_template(), job.output_rows()), media_type=XLSX, headers={
        "Content-Disposition": f'attachment; filename="accobiz-stock-accounts-{job.software}.xlsx"'})


@app.get("/api/stock/jobs/{job_id}/report")
def stock_report(job_id: str):
    job = _job(job_id, StockJob)
    return Response(write_stock_report(job), media_type=XLSX, headers={
        "Content-Disposition": f'attachment; filename="stock-conversion-report-{job.software}.xlsx"'})


# ---------------- settings ----------------
@app.get("/api/settings/stock")
def get_stock_settings():
    o = stock_template().options
    return {"settings": validate_settings(stock_settings()),
            "choices": {"unit": o["unit"], "purchaseaccount": o["purchaseaccount"],
                        "salesaccount": o["salesaccount"]}}


@app.put("/api/settings/stock")
def put_stock_settings(body: dict):
    try:
        clean = validate_settings(body)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Invalid settings: {e}")
    try:
        save_stock_settings(clean)
    except OSError:
        raise HTTPException(503, "Settings cannot be saved on this server (read-only disk). "
                                 "Edit rules/accobiz/stock_settings.yaml in the repository and redeploy.")
    return {"settings": clean}


STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
