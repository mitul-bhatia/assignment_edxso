"""Create evaluator-friendly CSVs from the stored run."""

from __future__ import annotations

import csv
import json

from .common import DATA
from .common import config
from .db import all_rows, connect, init_db
from .policy import metric_videos

EXPORTS = DATA / "exports"


def write_csv(path, fields: list[str], records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def export_all() -> dict:
    init_db()
    with connect() as db:
        profiles = all_rows(db, "SELECT * FROM profiles ORDER BY filter_status,name")
        videos = all_rows(db, "SELECT * FROM videos ORDER BY channel_id,published_at DESC")
        messages = all_rows(db, "SELECT m.*,p.name,p.profile_url FROM messages m JOIN profiles p ON p.channel_id=m.channel_id WHERE p.filter_status='PASSED' ORDER BY p.name")
        log = all_rows(db, "SELECT o.*,p.name,p.email FROM outreach_log o JOIN profiles p ON p.channel_id=o.channel_id ORDER BY o.id")
    video_lookup: dict[str, list] = {}
    for video in videos:
        video_lookup.setdefault(video["channel_id"], []).append(video)
    settings = config()
    rules = settings["filters"]
    profile_fields = [
        "channel_id", "name", "platform", "profile_url", "subscribers", "engagement_rate",
        "engagement_basis", "median_views", "reach_ratio", "metric_samples", "metric_video_ids", "recent_video_urls", "recent_video_titles",
        "category", "themes", "email", "email_source", "email_source_kind", "website",
        "instagram_url", "channel_country", "audience_age", "audience_gender",
        "audience_geography", "filter_status", "brand_tier", "fit_score", "classification_provider",
        "classification_evidence_ids", "creator_type", "filter_reason_codes", "filter_reasons",
        "discovered_by", "discovered_at", "assessed_at", "enriched_at",
    ]
    profile_records = []
    for row in profiles:
        item = dict(row)
        item.update({"platform": "YouTube", "audience_age": "Not Available",
                     "audience_gender": "Not Available", "audience_geography": "Not Available"})
        own_videos = video_lookup.get(row["channel_id"], [])[: settings["videos_per_channel"]]
        item["recent_video_urls"] = "; ".join(video["url"] for video in own_videos[:5])
        item["recent_video_titles"] = "; ".join(video["title"] for video in own_videos[:5])
        item["metric_video_ids"] = "; ".join(
            video["video_id"] for video in metric_videos(own_videos, rules, row["engagement_basis"]))
        for field in ("themes", "filter_reasons", "filter_reason_codes", "discovered_by", "classification_evidence_ids"):
            item[field] = "; ".join(map(str, json.loads(item[field] or "[]")))
        profile_records.append(item)
    write_csv(EXPORTS / "influencers.csv", profile_fields, profile_records)
    message_fields = ["campaign_id", "channel_id", "name", "profile_url", "subject", "email_body",
                      "dm", "referenced_video_id", "collaboration_type", "prompt_version", "generation_model", "validation_status",
                      "review_status", "created_at"]
    write_csv(EXPORTS / "messages.csv", message_fields, [dict(row) for row in messages])
    log_fields = ["id", "campaign_id", "channel_id", "name", "email", "recipient",
                  "message_id", "message_generated", "sent", "simulated_sent", "channel",
                  "method", "status", "provider_message_id", "date", "error"]
    log_records = []
    for row in log:
        item = dict(row)
        item["message_generated"] = bool(item["message_id"])
        item["sent"] = item["status"] in ("SENT", "MANUAL_DM_RECORDED")
        item["simulated_sent"] = item["status"] == "SIMULATED_SENT"
        item["date"] = item["created_at"]
        log_records.append(item)
    write_csv(EXPORTS / "outreach_log.csv", log_fields, log_records)
    return {"influencers": len(profiles), "messages": len(messages), "log_events": len(log),
            "folder": str(EXPORTS)}
