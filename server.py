"""Local API and built-web host for the reviewer interface.

Run: python -m uvicorn server:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import os
import threading

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.assessment import assess
from src.common import ROOT, config, load_env, required_env
from src.db import init_db
from src.discovery import discover
from src.enrichment import enrich
from src.exports import EXPORTS, export_all
from src.outreach import approve, mark_manual_dm, reject, save_message, simulate_send
from src import readmodel
from src.personalization import personalize

app = FastAPI(title="Micro-Influencer Outreach Review API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)
init_db()

_run_lock = threading.Lock()
_run_state: dict = {"status": "idle", "stage": None, "detail": None, "results": {}}


class MessageEdit(BaseModel):
    subject: str = Field(min_length=1)
    email_body: str = Field(min_length=1)
    dm: str = Field(min_length=1)
    referenced_video_id: str = Field(min_length=1)


@app.get("/api/health")
def health():
    return {"status": "ok", "database": "ready"}


@app.get("/api/config")
def public_config():
    load_env()
    settings = config()
    return {"campaign_id": settings["campaign_id"], "category": settings["category"],
            "campaign_status": settings["campaign"]["status"],
            "minimum_discovered": settings["minimum_discovered"],
            "classifier_provider": os.environ.get("CLASSIFIER_PROVIDER", "auto")}


@app.get("/api/funnel")
def pipeline_funnel():
    from src.funnel import funnel
    return funnel()


@app.get("/api/credentials")
def credential_status():
    load_env()
    return {name: bool(os.environ.get(name)) for name in (
        "YOUTUBE_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY")}


@app.get("/api/summary")
def summary():
    return readmodel.summary()


def _quality_issues(totals: dict) -> list[str]:
    issues = []
    if totals["review_required"]:
        issues.append(f"{totals['review_required']} classification decisions need review")
    if totals["passed"] == 0:
        issues.append("no creators passed filtering")
    if totals["drafts"] < totals["passed"]:
        issues.append(f"{totals['passed'] - totals['drafts']} qualified creators lack drafts")
    return issues


@app.get("/api/creators")
def creators(
    status: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=5000, ge=1, le=5000),
):
    try:
        return readmodel.list_creators(status, search, limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/creators/{channel_id}")
def creator_detail(channel_id: str):
    detail = readmodel.creator_detail(channel_id)
    if detail is None:
        raise HTTPException(404, "Creator not found")
    return detail


@app.put("/api/creators/{channel_id}/message")
def edit_message(channel_id: str, payload: MessageEdit):
    errors = save_message(channel_id, payload.subject, payload.email_body, payload.dm,
                          payload.referenced_video_id)
    if errors:
        raise HTTPException(422, errors)
    return {"status": "PENDING_REVIEW"}


@app.post("/api/creators/{channel_id}/approve")
def approve_message(channel_id: str):
    result = approve(channel_id)
    if result != "APPROVED":
        raise HTTPException(409, result)
    return {"status": result}


@app.post("/api/creators/{channel_id}/reject")
def reject_message(channel_id: str):
    result = reject(channel_id)
    if result != "REJECTED":
        raise HTTPException(409, result)
    return {"status": result}


@app.post("/api/creators/{channel_id}/simulate-send")
def simulate_email(channel_id: str):
    return {"status": simulate_send(channel_id)}


@app.post("/api/creators/{channel_id}/record-dm")
def record_dm(channel_id: str):
    return {"status": mark_manual_dm(channel_id)}


@app.get("/api/outreach")
def outreach_events(limit: int = Query(default=200, ge=1, le=500)):
    return readmodel.outreach_events(limit)


def _run_pipeline():
    stages = [
        ("Discovery", discover),
        ("Assessment", assess),
        ("Enrichment", enrich),
        ("Personalization", personalize),
        ("Export", export_all),
    ]
    try:
        for label, operation in stages:
            with _run_lock:
                _run_state.update({"status": "running", "stage": label,
                                   "detail": f"{label} in progress"})
            result = operation()
            with _run_lock:
                _run_state["results"][label] = result
            if label == "Discovery" and result["stored"] < config()["minimum_discovered"]:
                raise RuntimeError(f"Only {result['stored']} unique creators found; minimum is {config()['minimum_discovered']}")
        totals = summary()
        issues = _quality_issues(totals)
        with _run_lock:
            _run_state.update({"status": "partial" if issues else "complete", "stage": None,
                               "detail": "; ".join(issues) if issues else
                               "Pipeline finished; review drafts and simulate outreach before submission"})
    except Exception as exc:
        with _run_lock:
            _run_state.update({"status": "failed", "stage": _run_state.get("stage"),
                               "detail": f"{type(exc).__name__}: {str(exc)[:400]}"})


@app.get("/api/run")
def run_status():
    totals = summary()
    with _run_lock:
        if _run_state["status"] == "idle" and totals["discovered"]:
            issues = _quality_issues(totals)
            _run_state.update({"status": "partial" if issues else "complete", "stage": None,
                               "detail": "; ".join(issues) if issues else
                               "Existing local dataset loaded; review drafts and outreach log"})
        return dict(_run_state)


@app.post("/api/run")
def start_run():
    load_env()
    provider = os.environ.get("CLASSIFIER_PROVIDER", "auto").lower()
    missing = []
    required = ["YOUTUBE_API_KEY", "GEMINI_API_KEY"]
    if provider == "groq":
        required.append("GROQ_API_KEY")
    for name in required:
        try:
            required_env(name)
        except ValueError:
            missing.append(name)
    if missing:
        raise HTTPException(422, {"missing_keys": missing,
                                  "message": "Add the keys to the project .env file, then retry."})
    with _run_lock:
        if _run_state["status"] == "running":
            raise HTTPException(409, "A pipeline run is already in progress")
        _run_state.update({"status": "running", "stage": "Starting",
                           "detail": "Preparing pipeline", "results": {}})
    thread = threading.Thread(target=_run_pipeline, daemon=True)
    thread.start()
    return {"status": "running"}


@app.post("/api/exports")
def create_exports():
    return export_all()


@app.get("/api/exports/{filename}")
def download_export(filename: str):
    if filename not in {"influencers.csv", "messages.csv", "outreach_log.csv"}:
        raise HTTPException(404, "Unknown export")
    file = EXPORTS / filename
    if not file.exists():
        raise HTTPException(404, "Run Export first")
    return FileResponse(file, filename=filename, media_type="text/csv")


DIST = ROOT / "web" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
