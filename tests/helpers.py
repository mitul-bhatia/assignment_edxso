"""Shared fixtures: a throwaway database with one qualified-looking creator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import db
from src.common import now_utc

CAMPAIGN = "edxso-technology-demo-01"


class TempDatabase(unittest.TestCase):
    channel_id = "UCtest123"

    def setUp(self):
        self.env_patch = patch.dict("os.environ", {"GEMINI_CLASSIFIER_PAUSE_SECONDS": "0", "CLASSIFIER_PROVIDER": "gemini"})
        self.env_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()
        self.env_patch.stop()

    def add_profile(self, channel_id=None, *, subscribers=15000, status="DISCOVERED", description="AI teaching tools",
                    email="Not Found", source=None, tier=None, score=None):
        channel_id = channel_id or self.channel_id
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO profiles
                (channel_id,name,profile_url,description,subscribers,hidden_subscribers,discovered_by,
                 discovered_at,updated_at,filter_status,email,email_source,brand_tier,fit_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (channel_id, f"Creator {channel_id}", f"https://www.youtube.com/channel/{channel_id}", description,
                 subscribers, 0, json.dumps(["test query"]), now_utc(), now_utc(), status, email, source, tier, score))
        return channel_id

    def add_videos(self, channel_id=None, *, count=3, views=1000, likes=50, comments=10, duration=600,
                   title="AI tools for educators", description="", prefix="v"):
        channel_id = channel_id or self.channel_id
        with db.connect() as connection:
            for index in range(count):
                connection.execute(
                    """INSERT INTO videos
                    (video_id,channel_id,title,description,published_at,url,views,likes,comments,duration_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (f"{prefix}-{channel_id}-{index}", channel_id, title, description, now_utc(),
                     f"https://www.youtube.com/watch?v={prefix}-{channel_id}-{index}", views, likes, comments, duration))

    def add_message(self, channel_id=None, *, review="APPROVED", subject="Subject", body="Body text"):
        channel_id = channel_id or self.channel_id
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO messages
                (campaign_id,channel_id,subject,email_body,dm,referenced_video_id,prompt_version,
                 validation_status,review_status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (CAMPAIGN, channel_id, subject, body, "DM", "video-test", "test", "VALID", review, now_utc(), now_utc()))
