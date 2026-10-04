"""Pipeline funnel: where candidates are lost, and why.

The yield of a multi-stage filter is the product of each stage's pass rate, so the
only way to improve it rationally is to see which stage loses the most and whether
that loss is deserved. Every number here derives from stored reason codes.
"""

from __future__ import annotations

import json

from .db import all_rows, connect, init_db, one
from .quota import usage_report

STAGES = [
    ("discovered", set()),
    ("in follower range", {"SUBS_UNAVAILABLE", "SUBS_BELOW_MIN", "SUBS_ABOVE_MAX"}),
    ("recent, measurable videos", {"NO_UPLOAD_DATA", "STALE", "TOO_FEW_VIDEOS"}),
    ("real audience (reach + engagement)", {"LOW_REACH", "LOW_REACH_RATIO", "LOW_ENGAGEMENT"}),
]
CLASSIFIER_CODES = {"NOT_TECH", "SCHOOL_BELOW_MIN", "NOT_CREATOR_LED", "LOW_FIT_SCORE"}


def funnel() -> dict:
    init_db()
    with connect() as db:
        profiles = all_rows(db, "SELECT filter_status,filter_reason_codes,email,brand_tier,email_source_kind FROM profiles")
        messages = one(db, "SELECT COUNT(*) n FROM messages m JOIN profiles p ON p.channel_id=m.channel_id "
                              "WHERE p.filter_status='PASSED' AND m.validation_status='VALID'")["n"]
        approved = one(db, "SELECT COUNT(*) n FROM messages m JOIN profiles p ON p.channel_id=m.channel_id "
                           "WHERE p.filter_status='PASSED' AND m.validation_status='VALID' AND m.review_status='APPROVED'")["n"]
        sent = one(db, "SELECT COUNT(DISTINCT channel_id) n FROM outreach_log WHERE status IN ('SENT','SIMULATED_SENT')")["n"]
    surviving = [(row, set(json.loads(row["filter_reason_codes"] or "[]"))) for row in profiles]
    stages, removed = [], set()
    for name, codes in STAGES:
        removed |= codes
        count = sum(1 for _, found in surviving if not (found & removed))
        stages.append({"stage": name, "count": count})
    passed = [row for row, _ in surviving if row["filter_status"] == "PASSED"]
    pending = sum(1 for row, _ in surviving if row["filter_status"] == "DISCOVERED")
    stages.append({"stage": "technology, creator-led, fit score (PASSED)", "count": len(passed)})
    stages.append({"stage": "with a published email", "count": sum(1 for r in passed if r["email"] != "Not Found")})
    stages.append({"stage": "valid message drafted", "count": messages})
    stages.append({"stage": "message approved", "count": approved})
    stages.append({"stage": "emailed (simulated or live)", "count": sent})
    # Each stage is compared with its logical parent, not simply the row above: emails and drafts both
    # branch from the qualified set.
    parent = {"with a published email": "technology, creator-led, fit score (PASSED)",
              "valid message drafted": "technology, creator-led, fit score (PASSED)",
              "message approved": "valid message drafted", "emailed (simulated or live)": "message approved"}
    counts = {entry["stage"]: entry["count"] for entry in stages}
    for index, entry in enumerate(stages):
        base = counts.get(parent.get(entry["stage"])) if entry["stage"] in parent else (stages[index - 1]["count"] if index else None)
        entry["of_previous"] = f"{entry['count'] / base:.0%}" if base else "-"
    reasons: dict[str, int] = {}
    for row, found in surviving:
        if row["filter_status"] == "FAILED":
            for code in found:
                reasons[code] = reasons.get(code, 0) + 1
    contact_kinds: dict[str, int] = {}
    for row in passed:
        key = row["email_source_kind"] if row["email"] != "Not Found" else "not_found"
        contact_kinds[key or "unknown"] = contact_kinds.get(key or "unknown", 0) + 1
    tiers: dict[str, int] = {}
    for row in passed:
        tiers[row["brand_tier"] or "unassigned"] = tiers.get(row["brand_tier"] or "unassigned", 0) + 1
    return {"stages": stages, "pending_classification": pending,
            "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "shortlist_tiers": tiers, "shortlist_contacts": contact_kinds, "quota": usage_report()}


def render(report: dict) -> str:
    lines = ["PIPELINE FUNNEL", "-" * 64]
    for entry in report["stages"]:
        lines.append(f"{entry['count']:6d}  {entry['of_previous']:>5}  {entry['stage']}")
    if report["pending_classification"]:
        lines.append(f"\n{report['pending_classification']} eligible profiles still await classification (run: assess)")
    lines += ["", "WHY CANDIDATES FAILED (a profile can have several reasons)"]
    lines += [f"{count:6d}  {code}" for code, count in report["failure_reasons"].items()]
    lines += ["", f"Shortlist tiers:   {report['shortlist_tiers']}", f"Shortlist contact: {report['shortlist_contacts']}"]
    quota = report["quota"]
    lines.append(f"YouTube quota today (Pacific {quota['day_pacific']}): {quota['used']}/{quota['budget']} units")
    return "\n".join(lines)
