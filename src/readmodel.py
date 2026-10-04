"""Read-only views of the database, shared by the live API and the static demo snapshot.

Keeping the queries in one place means the Vercel demo can never drift from what the
local workbench shows."""

from __future__ import annotations

import json

from .common import config
from .db import all_rows, connect, one

STATUSES = {"DISCOVERED", "PASSED", "FAILED", "REVIEW_REQUIRED"}
JSON_FIELDS = ("themes", "filter_reasons", "filter_reason_codes", "discovered_by", "classification_evidence_ids")


def profile_dict(row) -> dict:
    item = dict(row)
    for field in JSON_FIELDS:
        if field not in item:
            continue
        try:
            item[field] = json.loads(item[field] or "[]")
        except json.JSONDecodeError:
            item[field] = []
    item["platform"] = "YouTube"
    item["audience_age"] = "Not Available"
    item["audience_gender"] = "Not Available"
    item["audience_geography"] = "Not Available"
    return item


def summary() -> dict:
    with connect() as db:
        statuses = {row["filter_status"]: row["n"] for row in all_rows(
            db, "SELECT filter_status,COUNT(*) n FROM profiles GROUP BY filter_status")}
        count = one(db, "SELECT COUNT(*) n FROM profiles")["n"]
        emails = one(db, "SELECT COUNT(*) n FROM profiles WHERE filter_status='PASSED' AND email!='Not Found'")["n"]
        # Only creators who still qualify: a draft for a creator who no longer passes is stale.
        drafts = one(db, "SELECT COUNT(*) n FROM messages m JOIN profiles p ON p.channel_id=m.channel_id "
                         "WHERE p.filter_status='PASSED' AND m.validation_status='VALID'")["n"]
        approved = one(db, "SELECT COUNT(*) n FROM messages m JOIN profiles p ON p.channel_id=m.channel_id "
                           "WHERE p.filter_status='PASSED' AND m.review_status='APPROVED' AND m.validation_status='VALID'")["n"]
        sent = one(db, "SELECT COUNT(*) n FROM outreach_log WHERE status='SIMULATED_SENT'")["n"]
    return {"discovered": count, "passed": statuses.get("PASSED", 0), "failed": statuses.get("FAILED", 0),
            "review_required": statuses.get("REVIEW_REQUIRED", 0), "contactable": emails,
            "drafts": drafts, "approved": approved, "simulated_sent": sent}


def list_creators(status: str | None = None, search: str | None = None, limit: int = 5000) -> list[dict]:
    conditions: list[str] = []
    args: list = []
    if status and status != "ALL":
        if status not in STATUSES:
            raise ValueError("Unknown status filter")
        conditions.append("filter_status=?")
        args.append(status)
    if search:
        conditions.append("(name LIKE ? OR description LIKE ? OR channel_id LIKE ?)")
        args.extend([f"%{search}%"] * 3)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with connect() as db:
        rows = all_rows(
            db,
            "SELECT channel_id,name,profile_url,subscribers,engagement_rate,metric_samples,"
            "category,themes,fit_score,brand_tier,median_views,reach_ratio,filter_status,filter_reasons,email,"
            "channel_country,discovered_at,assessed_at FROM profiles" + where +
            " ORDER BY CASE filter_status WHEN 'PASSED' THEN 0 WHEN 'REVIEW_REQUIRED' THEN 1 "
            "WHEN 'FAILED' THEN 2 ELSE 3 END,fit_score DESC,name LIMIT ?",
            tuple(args + [limit]),
        )
    return [profile_dict(row) for row in rows]


def creator_detail(channel_id: str, *, description_chars: int | None = None) -> dict | None:
    campaign_id = config()["campaign_id"]
    with connect() as db:
        profile = one(db, "SELECT * FROM profiles WHERE channel_id=?", (channel_id,))
        if not profile:
            return None
        videos = all_rows(
            db, "SELECT video_id,title,description,published_at,url,views,likes,comments,duration_seconds "
                "FROM videos WHERE channel_id=? ORDER BY published_at DESC LIMIT 12", (channel_id,))
        message = one(db, "SELECT * FROM messages WHERE campaign_id=? AND channel_id=?", (campaign_id, channel_id))
        events = all_rows(db, "SELECT * FROM outreach_log WHERE campaign_id=? AND channel_id=? ORDER BY id DESC",
                          (campaign_id, channel_id))
    video_rows = [dict(row) for row in videos]
    detail = profile_dict(profile)
    if description_chars is not None:  # the static demo does not need whole channel/video descriptions
        for video in video_rows:
            video["description"] = (video["description"] or "")[:description_chars]
        detail["description"] = (detail.get("description") or "")[:description_chars]
    return {"profile": detail, "videos": video_rows,
            "message": dict(message) if message else None, "outreach": [dict(row) for row in events]}


def outreach_events(limit: int = 200) -> list[dict]:
    with connect() as db:
        rows = all_rows(
            db, "SELECT o.id,o.created_at,o.channel,o.method,o.status,o.recipient,o.error,"
                "o.channel_id,p.name,p.profile_url FROM outreach_log o "
                "JOIN profiles p ON p.channel_id=o.channel_id ORDER BY o.id DESC LIMIT ?", (limit,))
    return [dict(row) for row in rows]
