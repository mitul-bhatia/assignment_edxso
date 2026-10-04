"""Human audit of the classifier: sample, label by hand, measure.

Filtering is only meaningful if it agrees with a person looking at the same evidence.
`audit-sample` draws a reproducible, stratified sample (passed / borderline / clear
failures, because errors concentrate near the boundary). A reviewer fills in the
`human_decision` column; `audit-score` then computes precision and recall.
"""

from __future__ import annotations

import csv
import json
import random

from .common import DATA
from .db import all_rows, connect, init_db

AUDIT_FILE = DATA / "exports" / "audit_sample.csv"
FIELDS = ["channel_id", "name", "profile_url", "stratum", "predicted_status", "fit_score", "subscribers",
          "median_views", "engagement_rate", "creator_type", "themes", "recent_titles", "reasons",
          "human_decision", "human_note"]


def _titles(db, channel_id: str) -> str:
    rows = all_rows(db, "SELECT title FROM videos WHERE channel_id=? ORDER BY published_at DESC LIMIT 5", (channel_id,))
    return " | ".join(row["title"] for row in rows)


def draw_sample(size: int = 30, seed: int = 42) -> dict:
    init_db()
    rng = random.Random(seed)
    with connect() as db:
        rows = all_rows(db, "SELECT * FROM profiles WHERE subscribers BETWEEN 5000 AND 100000 AND filter_status IN ('PASSED','FAILED')")
        passed = [r for r in rows if r["filter_status"] == "PASSED"]
        # Borderline = a model-labelled channel that failed on a soft rule, or one that passed with a thin margin.
        soft = {"NOT_TECH", "NOT_CREATOR_LED", "LOW_FIT_SCORE", "SCHOOL_BELOW_MIN"}
        borderline = [r for r in rows if r["filter_status"] == "FAILED" and soft & set(json.loads(r["filter_reason_codes"] or "[]"))]
        clear = [r for r in rows if r["filter_status"] == "FAILED" and r not in borderline]
        share = size // 3
        chosen = ([("passed", r) for r in rng.sample(passed, min(share, len(passed)))]
                  + [("borderline", r) for r in rng.sample(borderline, min(share, len(borderline)))]
                  + [("clear_failure", r) for r in rng.sample(clear, min(size - 2 * share, len(clear)))])
        records = [{
            "channel_id": r["channel_id"], "name": r["name"], "profile_url": r["profile_url"], "stratum": stratum,
            "predicted_status": r["filter_status"], "fit_score": r["fit_score"], "subscribers": r["subscribers"],
            "median_views": r["median_views"], "engagement_rate": r["engagement_rate"],
            "creator_type": r["creator_type"], "themes": "; ".join(json.loads(r["themes"] or "[]")),
            "recent_titles": _titles(db, r["channel_id"]), "reasons": " / ".join(json.loads(r["filter_reasons"] or "[]")),
            "human_decision": "", "human_note": ""} for stratum, r in chosen]
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    return {"file": str(AUDIT_FILE), "rows": len(records), "instruction":
            "Open each profile_url, then type PASS or FAIL in human_decision (would you pitch this creator for a "
            "Technology micro-influencer campaign?). Then run: python main.py audit-score"}


def score_audit() -> dict:
    with AUDIT_FILE.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    labelled = [r for r in rows if r["human_decision"].strip().upper() in ("PASS", "FAIL")]
    if not labelled:
        return {"error": f"No labels yet in {AUDIT_FILE}; fill human_decision with PASS or FAIL."}
    tp = sum(1 for r in labelled if r["predicted_status"] == "PASSED" and r["human_decision"].strip().upper() == "PASS")
    fp = sum(1 for r in labelled if r["predicted_status"] == "PASSED" and r["human_decision"].strip().upper() == "FAIL")
    fn = sum(1 for r in labelled if r["predicted_status"] == "FAILED" and r["human_decision"].strip().upper() == "PASS")
    tn = sum(1 for r in labelled if r["predicted_status"] == "FAILED" and r["human_decision"].strip().upper() == "FAIL")
    ratio = lambda a, b: round(a / b, 3) if b else None
    return {"labelled": len(labelled), "unlabelled": len(rows) - len(labelled),
            "true_pass": tp, "false_pass": fp, "false_fail": fn, "true_fail": tn,
            "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "note": "The sample is stratified toward the boundary, so recall here is a stress test, not a population estimate."}
