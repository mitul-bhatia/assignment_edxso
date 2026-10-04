"""Funnel accounting and audit scoring."""

import csv
import json
from unittest.mock import patch

from helpers import TempDatabase

from src import audit, db
from src.funnel import funnel


class FunnelTests(TempDatabase):
    def set_codes(self, channel_id, status, codes, email="Not Found"):
        self.add_profile(channel_id, status=status, email=email)
        with db.connect() as c:
            c.execute("UPDATE profiles SET filter_reason_codes=? WHERE channel_id=?", (json.dumps(codes), channel_id))

    def test_each_stage_only_counts_survivors_of_earlier_stages(self):
        self.set_codes("a", "FAILED", ["SUBS_BELOW_MIN"])
        self.set_codes("b", "FAILED", ["TOO_FEW_VIDEOS"])
        self.set_codes("c", "FAILED", ["LOW_REACH"])
        self.set_codes("d", "FAILED", ["NOT_TECH"])
        self.set_codes("e", "PASSED", ["PASSED"], email="e@x.dev")
        report = funnel()
        counts = {s["stage"]: s["count"] for s in report["stages"]}
        self.assertEqual(counts["discovered"], 5)
        self.assertEqual(counts["in follower range"], 4)
        self.assertEqual(counts["recent, measurable videos"], 3)
        self.assertEqual(counts["real audience (reach + engagement)"], 2)
        self.assertEqual(counts["technology, creator-led, fit score (PASSED)"], 1)
        self.assertEqual(counts["with a published email"], 1)
        self.assertEqual(report["failure_reasons"]["NOT_TECH"], 1)


class AuditTests(TempDatabase):
    def test_precision_and_recall_from_human_labels(self):
        rows = [("PASSED", "PASS"), ("PASSED", "PASS"), ("PASSED", "FAIL"), ("FAILED", "PASS"), ("FAILED", "FAIL"), ("FAILED", "")]
        path = audit.DATA / "exports" / "audit_sample.csv"
        with patch.object(audit, "AUDIT_FILE", self.tmp_path()):
            with audit.AUDIT_FILE.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=audit.FIELDS)
                writer.writeheader()
                for predicted, human in rows:
                    writer.writerow({"channel_id": "x", "predicted_status": predicted, "human_decision": human})
            result = audit.score_audit()
        self.assertEqual((result["true_pass"], result["false_pass"], result["false_fail"], result["true_fail"]), (2, 1, 1, 1))
        self.assertEqual(result["precision"], round(2 / 3, 3))
        self.assertEqual(result["recall"], round(2 / 3, 3))
        self.assertEqual(result["unlabelled"], 1)

    def tmp_path(self):
        from pathlib import Path
        return Path(self.temp.name) / "audit.csv"

    def test_sample_is_reproducible_and_stratified(self):
        for i in range(12):
            self.add_profile(f"p{i}", status="PASSED", subscribers=20000)
        for i in range(12):
            self.add_profile(f"f{i}", status="FAILED", subscribers=20000)
            with db.connect() as c:
                c.execute("UPDATE profiles SET filter_reason_codes=? WHERE channel_id=?", (json.dumps(["NOT_TECH" if i % 2 else "LOW_REACH"]), f"f{i}"))
        with patch.object(audit, "AUDIT_FILE", self.tmp_path()):
            audit.draw_sample(9, seed=7)
            first = audit.AUDIT_FILE.read_text(encoding="utf-8-sig")
            audit.draw_sample(9, seed=7)
            self.assertEqual(first, audit.AUDIT_FILE.read_text(encoding="utf-8-sig"))
            strata = [r["stratum"] for r in csv.DictReader(first.splitlines())]
        self.assertEqual(sorted(set(strata)), ["borderline", "clear_failure", "passed"])
