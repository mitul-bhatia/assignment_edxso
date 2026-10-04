"""Freeze the current database into static JSON for a read-only hosted demo (Vercel).

A public deployment must not expose the live API: it has no authentication, and its
endpoints can spend API quota or send email. The snapshot contains only what the
workbench already displays, and is served as plain files."""

from __future__ import annotations

import json
import shutil

from . import readmodel
from .common import ROOT, config
from .db import init_db
from .exports import EXPORTS
from .funnel import funnel

DEMO_DIR = ROOT / "web" / "public" / "demo"
DESCRIPTION_CHARS = 280


def _write(path, payload) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def build_snapshot(out=DEMO_DIR) -> dict:
    init_db()
    if out.exists():
        shutil.rmtree(out)
    settings = config()
    rules = settings.get("filters", {})
    lo, hi = rules.get("minimum_subscribers", 5000), rules.get("maximum_subscribers", 100000)
    size = _write(out / "summary.json", readmodel.summary())
    size += _write(out / "config.json", {
        "campaign_id": settings["campaign_id"], "category": settings["category"],
        "campaign_status": settings["campaign"]["status"],
        "minimum_discovered": settings["minimum_discovered"], "classifier_provider": "gemini"})
    size += _write(out / "funnel.json", funnel())
    size += _write(out / "outreach.json", readmodel.outreach_events(500))
    creators = readmodel.list_creators()
    size += _write(out / "creators.json", creators)
    details = 0
    for creator in creators:
        in_range = creator["subscribers"] is not None and lo <= creator["subscribers"] <= hi
        if creator["filter_status"] == "PASSED" or in_range:  # out-of-range rows have no videos worth showing
            detail = readmodel.creator_detail(creator["channel_id"], description_chars=DESCRIPTION_CHARS)
            size += _write(out / "creators" / f"{creator['channel_id']}.json", detail)
            details += 1
    for name in ("influencers.csv", "messages.csv", "outreach_log.csv"):
        if (EXPORTS / name).exists():
            (out / "exports").mkdir(parents=True, exist_ok=True)
            shutil.copy(EXPORTS / name, out / "exports" / name)
            size += (out / "exports" / name).stat().st_size
    return {"creators": len(creators), "detail_files": details, "total_mb": round(size / 1e6, 2), "folder": str(out)}
