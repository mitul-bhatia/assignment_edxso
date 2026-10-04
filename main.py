"""Command-line entry point for the complete outreach workflow."""

from __future__ import annotations

import argparse
import json
import sys

from src.assessment import assess, rescore
from src.audit import draw_sample, score_audit
from src.common import config
from src.db import all_rows, connect, init_db, one
from src.discovery import discover
from src.enrichment import enrich, record_verified_contact
from src.exports import export_all
from src.funnel import funnel, render
from src.outreach import approve, mark_manual_dm, reject, send_batch, send_email, simulate_send, suppress
from src.quota import usage_report
from src.snapshot import build_snapshot
from src.personalization import personalize, revalidate_messages


def status() -> dict:
    init_db()
    with connect() as db:
        rows = all_rows(db, "SELECT filter_status,COUNT(*) AS n FROM profiles GROUP BY filter_status")
        messages = one(db, "SELECT COUNT(*) AS n FROM messages")
        logs = one(db, "SELECT COUNT(*) AS n FROM outreach_log")
        return {"profiles": {row["filter_status"]: row["n"] for row in rows},
                "messages": messages["n"], "outreach_events": logs["n"]}


def show(channel_id: str) -> dict | None:
    init_db()
    with connect() as db:
        profile = one(db, "SELECT * FROM profiles WHERE channel_id=?", (channel_id,))
        if not profile:
            return None
        videos = all_rows(db, "SELECT video_id,title,url,published_at,views,likes,comments FROM videos WHERE channel_id=? ORDER BY published_at DESC LIMIT 5", (channel_id,))
        message = one(db, "SELECT * FROM messages WHERE campaign_id=? AND channel_id=?", (config()["campaign_id"], channel_id))
    item = dict(profile)
    item["recent_videos"] = [dict(video) for video in videos]
    item["message"] = dict(message) if message else None
    return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Technology micro-influencer outreach prototype")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "discover", "assess", "enrich", "personalize", "export", "status", "run-all",
                 "rescore", "funnel", "quota", "revalidate", "snapshot"):
        command = sub.add_parser(name)
        if name in ("discover", "assess", "enrich", "personalize"):
            command.add_argument("--refresh", action="store_true")
        if name == "assess":
            command.add_argument("--only-status", choices=["DISCOVERED", "PASSED", "FAILED", "REVIEW_REQUIRED"])
        if name == "enrich":
            command.add_argument("--no-crawl", action="store_true")
    sending = sub.add_parser("send", help="Send approved emails. Dry run unless --live is given")
    sending.add_argument("--live", action="store_true", help="really send through SMTP")
    sending.add_argument("--limit", type=int, default=10)
    sending.add_argument("--to", dest="override_to", help="deliver every message to this address instead (test mode)")
    sending.add_argument("--delay", type=float, default=20.0, help="seconds between live sends")
    sending.add_argument("--channel", help="send for one channel ID only")
    audit = sub.add_parser("audit-sample", help="Write a stratified sample for manual labelling")
    audit.add_argument("--n", type=int, default=30)
    sub.add_parser("audit-score", help="Precision/recall from the labelled audit sample")
    optout = sub.add_parser("suppress", help="Never contact this email address again")
    optout.add_argument("email")
    optout.add_argument("--reason", default="opt-out")
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=["DISCOVERED", "PASSED", "FAILED", "REVIEW_REQUIRED"])
    for name in ("show", "approve", "reject", "simulate-send", "mark-dm"):
        command = sub.add_parser(name)
        command.add_argument("channel_id")
    contact = sub.add_parser("add-contact", help="Verify an explicit email on a public source page")
    contact.add_argument("channel_id")
    contact.add_argument("email")
    contact.add_argument("source_url")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_db()
            result = {"database": "ready"}
        elif args.command == "discover":
            result = discover(refresh=args.refresh)
        elif args.command == "assess":
            result = assess(refresh=args.refresh, only_status=args.only_status)
        elif args.command == "enrich":
            result = enrich(refresh=args.refresh, crawl=not args.no_crawl)
        elif args.command == "personalize":
            result = personalize(refresh=args.refresh)
        elif args.command == "export":
            result = export_all()
        elif args.command == "status":
            result = status()
        elif args.command == "snapshot":
            result = build_snapshot()
        elif args.command == "revalidate":
            result = revalidate_messages()
        elif args.command == "rescore":
            result = rescore()
        elif args.command == "quota":
            result = usage_report()
        elif args.command == "funnel":
            print(render(funnel()))
            return 0
        elif args.command == "send":
            if args.channel:
                result = send_email(args.channel, live=args.live, override_to=args.override_to)
            else:
                result = send_batch(live=args.live, limit=args.limit, override_to=args.override_to,
                                    delay_seconds=args.delay)
        elif args.command == "audit-sample":
            result = draw_sample(args.n)
        elif args.command == "audit-score":
            result = score_audit()
        elif args.command == "suppress":
            result = suppress(args.email, args.reason)
        elif args.command == "run-all":
            discovery = discover()
            if discovery["stored"] < config()["minimum_discovered"]:
                raise RuntimeError(f"Only {discovery['stored']} real profiles found; need {config()['minimum_discovered']}. Adjust queries and rerun.")
            result = {"discovery": discovery, "assessment": assess(),
                      "enrichment": enrich(), "personalization": personalize(),
                      "exports": export_all(), "status": status()}
        elif args.command == "list":
            init_db()
            with connect() as db:
                if args.status:
                    rows = all_rows(db, "SELECT channel_id,name,subscribers,fit_score,filter_status,email FROM profiles WHERE filter_status=? ORDER BY fit_score DESC,name", (args.status,))
                else:
                    rows = all_rows(db, "SELECT channel_id,name,subscribers,fit_score,filter_status,email FROM profiles ORDER BY filter_status,name")
            result = [dict(row) for row in rows]
        elif args.command == "show":
            result = show(args.channel_id)
            if result is None:
                raise ValueError("Unknown channel ID")
        elif args.command == "add-contact":
            result = record_verified_contact(args.channel_id, args.email, args.source_url)
        elif args.command == "approve":
            result = approve(args.channel_id)
        elif args.command == "reject":
            result = reject(args.channel_id)
        elif args.command == "simulate-send":
            result = simulate_send(args.channel_id)
        else:
            result = mark_manual_dm(args.channel_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
