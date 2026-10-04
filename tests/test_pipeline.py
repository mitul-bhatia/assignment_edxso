"""Offline checks for calculations, grounding, persistence and duplicate protection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import db
from src.assessment import assess, classify, engagement_proxy
from src.common import ExternalServiceError, now_utc
from src.discovery import discover, duration_seconds
from src.enrichment import enrich, explicit_emails, find_contact, network_safe_url, record_verified_contact, safe_public_url
from src.outreach import approve, simulate_send
from src.personalization import validate_draft


class UnitTests(unittest.TestCase):
    def test_duration_and_engagement_ignore_missing_statistics(self):
        self.assertEqual(duration_seconds("PT12M4S"), 724)
        videos = [
            {"duration_seconds": 400, "views": 1000, "likes": 30, "comments": 10},
            {"duration_seconds": 600, "views": 2000, "likes": 60, "comments": 20},
            {"duration_seconds": 50, "views": 100, "likes": 50, "comments": 5},
            {"duration_seconds": 500, "views": 1000, "likes": None, "comments": 4},
        ]
        self.assertEqual(engagement_proxy(videos), (4.0, 2))

    def test_emails_are_explicit_and_not_predicted(self):
        self.assertEqual(explicit_emails("Business: creator [at] example [dot] com"), ["creator@example.com"])
        self.assertEqual(find_contact("No public email or website", crawl=False)[0], "Not Found")

    def test_contact_crawler_rejects_private_targets(self):
        self.assertFalse(safe_public_url("http://127.0.0.1/contact"))
        self.assertFalse(safe_public_url("https://creator.example:8080/contact"))
        with patch("src.enrichment.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.1.10", 0))]):
            self.assertFalse(network_safe_url("https://creator.example/contact"))

    def test_message_length_and_reference_validation(self):
        videos = [{"video_id": "real-video"}]
        draft = {"subject": "Hi", "email_body": "brief note", "dm": "hello",
                 "referenced_video_id": "invented-video"}
        errors = validate_draft(draft, videos)
        self.assertTrue(any("60–90" in error for error in errors))
        self.assertTrue(any("15–30" in error for error in errors))
        self.assertTrue(any("does not belong" in error for error in errors))

    def test_classifier_falls_back_and_labels_provider(self):
        classification = {"category": "Technology", "technology_match": True}
        with patch.dict("os.environ", {"CLASSIFIER_PROVIDER": "auto"}), \
                patch("src.assessment.groq_classify", side_effect=ExternalServiceError("HTTP 403")), \
                patch("src.assessment.gemini_classify", return_value=classification):
            result, provider = classify({}, [])
        self.assertEqual(result, classification)
        self.assertTrue(provider.startswith("Gemini fallback ("))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict("os.environ", {"GEMINI_CLASSIFIER_PAUSE_SECONDS": "0"})
        self.env_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        db.init_db()
        self.channel_id = "UCtest123"
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO profiles
                (channel_id,name,profile_url,description,subscribers,hidden_subscribers,
                 discovered_by,discovered_at,updated_at,filter_status,email,email_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.channel_id, "Synthetic test creator", "https://www.youtube.com/channel/UCtest123",
                 "AI teaching tools", 15000, 0, json.dumps(["test query"]), now_utc(), now_utc(),
                 "PASSED", "creator@example.com", "synthetic test fixture"),
            )
            connection.execute(
                """INSERT INTO messages
                (campaign_id,channel_id,subject,email_body,dm,referenced_video_id,
                 prompt_version,validation_status,review_status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("edxso-technology-demo-01", self.channel_id, "Demo", "Test body", "Test DM",
                 "video-test", "test", "VALID", "PENDING_REVIEW", now_utc(), now_utc()),
            )

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()
        self.env_patch.stop()

    def test_simulated_send_requires_review_and_blocks_duplicate(self):
        self.assertEqual(simulate_send(self.channel_id), "NOT_APPROVED")
        self.assertEqual(approve(self.channel_id), "APPROVED")
        self.assertEqual(simulate_send(self.channel_id), "SIMULATED_SENT")
        self.assertEqual(simulate_send(self.channel_id), "DUPLICATE_BLOCKED")
        with db.connect() as connection:
            sent = connection.execute("SELECT COUNT(*) FROM outreach_log WHERE status='SIMULATED_SENT'").fetchone()[0]
        self.assertEqual(sent, 1)

    def test_manual_contact_requires_published_source(self):
        with patch("src.enrichment.fetch_page", return_value="<p>public@example.com</p>"):
            with self.assertRaisesRegex(ValueError, "not explicitly published"):
                record_verified_contact(self.channel_id, "guessed@example.com", "https://example.com/contact")
            result = record_verified_contact(self.channel_id, "public@example.com", "https://example.com/contact")
        self.assertEqual(result, "VERIFIED_PUBLIC_EMAIL_RECORDED")
        with db.connect() as connection:
            row = connection.execute("SELECT email,email_source FROM profiles WHERE channel_id=?", (self.channel_id,)).fetchone()
        self.assertEqual(row["email"], "public@example.com")
        self.assertEqual(row["email_source"], "https://example.com/contact")
        self.assertEqual(enrich(refresh=True, crawl=False)["skipped"], 1)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT email FROM profiles WHERE channel_id=?", (self.channel_id,)).fetchone()[0],
                             "public@example.com")

    def test_assessment_keeps_model_evidence_without_failing_a_pass(self):
        with db.connect() as connection:
            connection.execute("UPDATE profiles SET filter_status='DISCOVERED',email='Not Found' WHERE channel_id=?", (self.channel_id,))
            for index in range(3):
                connection.execute(
                    """INSERT INTO videos
                    (video_id,channel_id,title,published_at,url,views,likes,comments,duration_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f"video-{index}", self.channel_id, "AI tools for educators", now_utc(),
                     f"https://www.youtube.com/watch?v=video-{index}", 1000, 50, 10, 600),
                )
        classification = {"category": "Technology", "technology_match": True,
                          "school_relevance": 3, "recent_relevance": 3,
                          "creator_type": "individual",
                          "themes": ["AI tools for teachers"], "tone": "explanatory",
                          "evidence_video_ids": ["video-0"], "reason": "Recent titles concern teacher tools."}
        with patch("src.assessment.classify", return_value=(classification, "Groq")):
            result = assess()
        self.assertEqual(result["PASSED"], 1)
        with db.connect() as connection:
            row = connection.execute("SELECT filter_status,filter_reasons,classification_evidence_ids FROM profiles WHERE channel_id=?", (self.channel_id,)).fetchone()
        self.assertEqual(row["filter_status"], "PASSED")
        self.assertIn("Model evidence", row["filter_reasons"])
        self.assertEqual(json.loads(row["classification_evidence_ids"]), ["video-0"])

    def test_general_technology_passes_as_standard_tier_not_priority(self):
        """Policy v2: K-12 fit ranks creators (PRIORITY) but no longer gates them."""
        with db.connect() as connection:
            for index in range(3):
                connection.execute(
                    """INSERT INTO videos
                    (video_id,channel_id,title,published_at,url,views,likes,comments,duration_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f"general-{index}", self.channel_id, "General software tutorial", now_utc(),
                     f"https://www.youtube.com/watch?v=general-{index}", 1000, 50, 10, 600),
                )
        classification = {"category": "Technology", "technology_match": True,
                          "school_relevance": 2, "recent_relevance": 3,
                          "creator_type": "individual",
                          "themes": ["Software tutorials"], "tone": "instructional",
                          "evidence_video_ids": ["general-0"],
                          "reason": "Useful software, but no recurring K-12 school content."}
        with patch("src.assessment.classify", return_value=(classification, "Gemini")):
            result = assess()
        self.assertEqual(result["PASSED"], 1)
        with db.connect() as connection:
            row = connection.execute("SELECT brand_tier FROM profiles WHERE channel_id=?", (self.channel_id,)).fetchone()
        self.assertEqual(row["brand_tier"], "STANDARD")

    def test_organization_does_not_pass_creator_led_gate(self):
        with db.connect() as connection:
            for index in range(3):
                connection.execute(
                    """INSERT INTO videos
                    (video_id,channel_id,title,published_at,url,views,likes,comments,duration_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f"org-{index}", self.channel_id, "AI tools for K-12 teachers", now_utc(),
                     f"https://www.youtube.com/watch?v=org-{index}", 1000, 50, 10, 600),
                )
        classification = {"category": "Technology", "technology_match": True,
                          "school_relevance": 3, "recent_relevance": 3,
                          "creator_type": "organization", "themes": ["Classroom AI"],
                          "tone": "instructional", "evidence_video_ids": ["org-0"],
                          "reason": "An institutional channel posting school technology videos."}
        with patch("src.assessment.classify", return_value=(classification, "Gemini")):
            result = assess()
        self.assertEqual(result["FAILED"], 1)
        with db.connect() as connection:
            row = connection.execute("SELECT filter_reasons,creator_type FROM profiles WHERE channel_id=?", (self.channel_id,)).fetchone()
        self.assertEqual(row["creator_type"], "organization")
        self.assertIn("not a verified creator-led", row["filter_reasons"])

    def test_discovery_collects_over_fifty_unique_channels_and_resumes(self):
        settings = {"minimum_discovered": 50, "max_candidates": 60,
                    "videos_per_channel": 3, "search_lookback_days": 365,
                    "queries": ["technology query one", "technology query two"]}

        def fake_youtube_get(path, params):
            if path == "search":
                offset = 0 if params["q"].endswith("one") else 30
                return {"items": [{"snippet": {"channelId": f"UCsample{i}"}}
                                  for i in range(offset, offset + 30)]}
            if path == "channels":
                return {"items": [{"id": channel_id,
                                   "snippet": {"title": f"Synthetic {channel_id}",
                                               "description": "Tech for teachers"},
                                   "statistics": {"subscriberCount": "15000"},
                                   "contentDetails": {"relatedPlaylists": {"uploads": f"PL{channel_id}"}}}
                                  for channel_id in params["id"].split(",")]}
            if path == "playlistItems":
                channel_id = params["playlistId"][2:]
                return {"items": [{"contentDetails": {"videoId": f"v-{channel_id}-{index}",
                                                     "videoPublishedAt": now_utc()},
                                   "snippet": {"title": "Teacher technology demonstration",
                                               "description": "Public test text"}} for index in range(3)]}
            if path == "videos":
                return {"items": [{"id": video_id,
                                   "statistics": {"viewCount": "1000", "likeCount": "50",
                                                  "commentCount": "10"},
                                   "contentDetails": {"duration": "PT5M"}}
                                  for video_id in params["id"].split(",")]}
            raise AssertionError(path)

        with patch("src.discovery.config", return_value=settings), patch("src.discovery.youtube_get", side_effect=fake_youtube_get) as api:
            result = discover()
            self.assertGreaterEqual(result["stored"], 50)
            self.assertEqual(result["videos"], 180)
            calls = api.call_count
            resumed = discover()
            self.assertEqual(resumed["new"], 0)
            self.assertEqual(api.call_count, calls)

    def test_unrelated_technology_creator_fails_with_reasons(self):
        with db.connect() as connection:
            for index in range(3):
                connection.execute(
                    """INSERT INTO videos
                    (video_id,channel_id,title,published_at,url,views,likes,comments,duration_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (f"phone-{index}", self.channel_id, "Phone accessories", now_utc(),
                     f"https://www.youtube.com/watch?v=phone-{index}", 1000, 30, 5, 600),
                )
        classification = {"category": "Lifestyle", "technology_match": False,
                          "school_relevance": 0, "recent_relevance": 0, "themes": [],
                          "creator_type": "individual",
                          "tone": "casual", "evidence_video_ids": [],
                          "reason": "Recent titles discuss phone cases, not school workflows."}
        with patch("src.assessment.classify", return_value=(classification, "Groq")):
            result = assess()
        self.assertEqual(result["FAILED"], 1)
        with db.connect() as connection:
            row = connection.execute("SELECT filter_reasons,filter_reason_codes FROM profiles WHERE channel_id=?", (self.channel_id,)).fetchone()
        self.assertIn("NOT_TECH", json.loads(row["filter_reason_codes"]))
        self.assertIn("Technology focus", row["filter_reasons"])


if __name__ == "__main__":
    unittest.main()
