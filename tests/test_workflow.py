"""Behavioural tests for assessment flow, discovery resilience, contact mining, copy checks and sending."""

import json
from unittest.mock import patch

from helpers import CAMPAIGN, TempDatabase

from src import db
from src.assessment import assess, rescore
from src.common import ExternalServiceError, QuotaBudgetExceeded, now_utc
from src.discovery import discover
from src.enrichment import acceptable_email, enrich, explicit_emails, mine_stored_text
from src.mailer import DefiniteFailure
from src.outreach import approve, send_batch, send_email, simulate_send, suppress
from src.personalization import choose_angle, clean_title, opening, validate_draft

GOOD = {"category": "Technology", "technology_match": True, "school_relevance": 2, "recent_relevance": 3,
        "creator_type": "individual", "themes": ["AI tools"], "tone": "clear", "evidence_video_ids": ["x"],
        "reason": "Recurring AI tool tutorials."}


class AssessmentFlowTests(TempDatabase):
    def test_model_is_never_called_when_objective_rules_fail(self):
        self.add_profile()
        self.add_videos(views=14, likes=1, comments=0)  # below the noise floor
        with patch("src.assessment.classify") as classify:
            result = assess()
        classify.assert_not_called()
        self.assertEqual((result["FAILED"], result["llm_calls"]), (1, 0))

    def test_paid_for_label_survives_a_later_failure_and_is_reused(self):
        self.add_profile()
        self.add_videos()
        with patch("src.assessment.classify", return_value=(GOOD, "Gemini")) as classify:
            self.assertEqual(assess()["PASSED"], 1)
            self.assertEqual(classify.call_count, 1)
            with db.connect() as c:  # the audience collapses: views drop, data refreshed
                c.execute("UPDATE videos SET views=5")
                c.execute("UPDATE profiles SET updated_at=?", (now_utc(),))
            self.assertEqual(assess()["FAILED"], 1)
            self.assertEqual(classify.call_count, 1)  # reused, not re-bought
            # A forced refresh of a channel that now fails an objective rule makes no new
            # call (nothing to classify) and must not erase the label it already paid for.
            self.assertEqual(assess(refresh=True)["FAILED"], 1)
            self.assertEqual(classify.call_count, 1)
        with db.connect() as c:
            row = c.execute("SELECT tech_match,creator_type FROM profiles").fetchone()
        self.assertEqual((row["tech_match"], row["creator_type"]), (1, "individual"))

    def test_rescore_parks_unlabelled_profiles_and_assess_later_classifies_them(self):
        """Regression: 'attempted' must not be recorded as 'finished'."""
        self.add_profile()
        self.add_videos()
        self.assertEqual(rescore()["pending_classification"], 1)
        with patch("src.assessment.classify", return_value=(GOOD, "Gemini")) as classify:
            self.assertEqual(assess()["PASSED"], 1)
        self.assertEqual(classify.call_count, 1)

    def test_policy_change_is_reapplied_without_any_model_call(self):
        self.add_profile()
        self.add_videos()
        with patch("src.assessment.classify", return_value=(GOOD, "Gemini")):
            assess()
        strict = {"filters": {**__import__("src.common", fromlist=["config"]).config()["filters"], "minimum_school_relevance": 3},
                  "videos_per_channel": 30}
        with patch("src.assessment.config", return_value=strict), patch("src.assessment.classify") as classify:
            with db.connect() as c:
                c.execute("UPDATE profiles SET policy_version='older'")
            self.assertEqual(rescore()["FAILED"], 1)
        classify.assert_not_called()


def make_api(*, channels, subscribers=15000, fail_playlist=(), pages=None):
    calls = []

    def fake(path, params):
        calls.append((path, dict(params)))
        if path == "search":
            token = params.get("pageToken")
            ids = channels[:2] if not token else channels[2:]
            body = {"items": [{"snippet": {"channelId": i}} for i in ids]}
            if not token and pages:
                body["nextPageToken"] = "PAGE2"
            return body
        if path == "channels":
            return {"items": [{"id": i, "snippet": {"title": i, "description": ""},
                               "statistics": {"subscriberCount": str(subscribers.get(i, 15000) if isinstance(subscribers, dict) else subscribers)},
                               "contentDetails": {"relatedPlaylists": {"uploads": f"PL{i}"}}} for i in params["id"].split(",")]}
        if path == "playlistItems":
            channel = params["playlistId"][2:]
            if channel in fail_playlist:
                raise ExternalServiceError("HTTP 500: boom")
            return {"items": [{"contentDetails": {"videoId": f"{channel}-v", "videoPublishedAt": now_utc()},
                               "snippet": {"title": "t", "description": ""}}]}
        if path == "videos":
            return {"items": [{"id": i, "statistics": {"viewCount": "500", "likeCount": "5", "commentCount": "1"},
                               "contentDetails": {"duration": "PT5M"}} for i in params["id"].split(",")]}
        raise AssertionError(path)
    return fake, calls


SETTINGS = {"max_candidates": 100, "videos_per_channel": 5, "search_lookback_days": 30, "queries": ["q"],
            "filters": {"minimum_subscribers": 5000, "maximum_subscribers": 100000}}


class DiscoveryResilienceTests(TempDatabase):
    def run_discover(self, settings, fake):
        with patch("src.discovery.config", return_value=settings), patch("src.discovery.youtube_get", side_effect=fake):
            return discover()

    def test_out_of_range_channels_never_cost_a_video_fetch(self):
        fake, calls = make_api(channels=["UCsmall", "UCok"], subscribers={"UCsmall": 40, "UCok": 15000})
        result = self.run_discover(SETTINGS, fake)
        fetched = [p["playlistId"] for path, p in calls if path == "playlistItems"]
        self.assertEqual(fetched, ["PLUCok"])
        self.assertEqual((result["stored"], result["in_range"]), (2, 1))

    def test_one_failing_channel_does_not_abort_the_run_and_is_retried_later(self):
        fake, _ = make_api(channels=["UCa", "UCb"], fail_playlist={"UCa"})
        result = self.run_discover(SETTINGS, fake)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["videos"], 1)  # UCb still got its videos
        with db.connect() as c:
            depth = {r["channel_id"]: r["videos_depth"] for r in c.execute("SELECT channel_id,videos_depth FROM profiles")}
        self.assertEqual(depth, {"UCa": 0, "UCb": 5})  # UCa will be retried next run

    def test_pagination_uses_the_stored_token_and_never_repeats_a_search(self):
        fake, calls = make_api(channels=["UCa", "UCb", "UCc", "UCd"], pages=True)
        settings = {**SETTINGS, "search_pages_per_query": 2}
        self.run_discover(settings, fake)
        searches = [p for path, p in calls if path == "search"]
        self.assertEqual([p.get("pageToken") for p in searches], [None, "PAGE2"])
        before = len(calls)
        self.run_discover(settings, fake)
        self.assertEqual(len([c for c in calls[before:] if c[0] == "search"]), 0)

    def test_quota_exhaustion_stops_cleanly_and_keeps_progress(self):
        fake, _ = make_api(channels=["UCa", "UCb"])

        def flaky(path, params):
            if path == "playlistItems":
                raise QuotaBudgetExceeded("budget reached")
            return fake(path, params)
        result = self.run_discover(SETTINGS, flaky)
        self.assertIn("budget reached", result["stopped_reason"])
        self.assertEqual(result["stored"], 2)  # searched profiles were kept


DESC_VIDEO = lambda text, n: {"url": f"https://www.youtube.com/watch?v={n}", "description": text}


class ContactMiningTests(TempDatabase):
    def test_channel_description_is_first_party(self):
        found = mine_stored_text("Business: hello@creator.com", [], "https://yt/c")
        self.assertEqual(found, ("hello@creator.com", "https://yt/c", "channel_description"))

    def test_video_email_needs_repetition_or_contact_wording(self):
        lone = [DESC_VIDEO("Thanks to guest jane@guest.org for joining", 1)]
        self.assertIsNone(mine_stored_text("", lone, "u"))
        repeated = [DESC_VIDEO("music by x@label.com", 1), DESC_VIDEO("music by x@label.com", 2)]
        self.assertEqual(mine_stored_text("", repeated, "u")[2], "video_description")
        contact = [DESC_VIDEO("For business inquiries email: biz@creator.com", 3)]
        self.assertEqual(mine_stored_text("", contact, "u")[0], "biz@creator.com")

    def test_noreply_and_platform_addresses_are_ignored(self):
        self.assertIsNone(mine_stored_text("noreply@brand.com and x@youtube.com", [], "u"))

    def test_json_escaped_characters_do_not_leak_into_addresses(self):
        self.assertEqual(explicit_emails('"label":"mail\\u003ehelp@creator.dev"'), ["help@creator.dev"])

    def test_platform_and_foreign_support_addresses_are_rejected(self):
        self.assertFalse(acceptable_email("help@skool.com"))
        self.assertFalse(acceptable_email("support@somesaas.com", site_host="creator.dev"))  # another company's desk
        self.assertTrue(acceptable_email("support@creator.dev", site_host="creator.dev"))   # their own domain
        self.assertTrue(acceptable_email("hello@creator.dev", site_host="creator.dev"))

    def test_one_address_on_several_channels_is_flagged_not_trusted(self):
        for name in ("a", "b"):
            self.add_profile(name, status="FAILED", description="collab: broker@agency.net")
        self.add_profile("c", status="FAILED", description="mail: solo@creator.dev")
        result = enrich(crawl=False)
        self.assertEqual(result["shared_addresses_flagged"], 2)
        with db.connect() as c:
            kinds = {r["channel_id"]: r["email_source_kind"] for r in c.execute("SELECT channel_id,email_source_kind FROM profiles")}
        self.assertEqual(kinds, {"a": "shared_address", "b": "shared_address", "c": "channel_description"})
        self.assertEqual(enrich(crawl=False)["shared_addresses_flagged"], 0)  # idempotent

    def test_stored_emails_are_revalidated_when_rules_tighten(self):
        self.add_profile(status="FAILED", email="help@skool.com", source="https://www.skool.com/x")
        enrich(crawl=False)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT email FROM profiles").fetchone()[0], "Not Found")

    def test_mining_covers_unshortlisted_profiles_without_any_network(self):
        self.add_profile(status="FAILED", description="collabs: work@creator.dev")
        with patch("src.enrichment.fetch_page", side_effect=AssertionError("no network for mining")):
            result = enrich()
        self.assertEqual(result["mined_found"], 1)
        with db.connect() as c:
            row = c.execute("SELECT email,email_source_kind FROM profiles").fetchone()
        self.assertEqual((row["email"], row["email_source_kind"]), ("work@creator.dev", "channel_description"))


class CopyValidationTests(TempDatabase):
    videos = [{"video_id": "v1", "title": "Google Slides tips for teachers #shorts"}]

    def draft(self, **over):
        email = ("Your Google Slides walkthrough makes a hard tool feel easy for busy classrooms, and that clarity is "
                 "rare. At EDXSO we work with schools on practical technology use. We would like to explore a UGC-style "
                 "tutorial together, with format and terms open for discussion, so more educators benefit from your "
                 "approach. Would you be open to a brief conversation about it this month?")
        return {"subject": "Hello", "email_body": email, "dm": "Your Google Slides walkthrough is so clear! EDXSO would love to "
                "explore a tutorial together. Open to a short chat?", "referenced_video_id": "v1", **over}

    def test_clean_draft_passes(self):
        self.assertEqual(validate_draft(self.draft(), self.videos, openings=set(), brand="EDXSO"), [])

    def test_hashtags_cliches_missing_brand_and_ungrounded_text_are_rejected(self):
        self.assertTrue(any("hashtag" in e for e in validate_draft(self.draft(dm="Love #shorts Google Slides content so much, "
                                                                              "EDXSO would love to explore a tutorial. Open to chat?"), self.videos)))
        generic = self.draft(email_body=self.draft()["email_body"].replace("Your Google Slides walkthrough", "I hope this email finds you well; your walkthrough"))
        self.assertTrue(any("generic phrasing" in e for e in validate_draft(generic, self.videos)))
        self.assertTrue(any("sender" in e for e in validate_draft(self.draft(), self.videos, brand="Acme")))
        off_topic = self.draft(email_body=self.draft()["email_body"].replace("Google Slides", "photography"),
                               dm="Your photography clips are so clear! EDXSO would love to explore a tutorial together. Open to chat?")
        self.assertTrue(any("cited video" in e for e in validate_draft(off_topic, self.videos)))

    def test_claims_of_enjoying_or_learning_from_content_are_rejected(self):
        for claim in ("Loved your video on Google Slides, truly.", "I really enjoyed your Google Slides tutorial.",
                      "We learned a lot from your Google Slides walkthrough."):
            draft = self.draft(dm=claim + " EDXSO would like to explore a tutorial together. Open to a chat soon?")
            self.assertTrue(any("watched or enjoyed" in e for e in validate_draft(draft, self.videos)), claim)
        honest = self.draft(dm="Your Google Slides walkthrough stood out to us. EDXSO would like to explore a tutorial together. Open to a chat?")
        self.assertFalse(any("watched or enjoyed" in e for e in validate_draft(honest, self.videos)))

    def test_dm_must_not_repeat_the_email_opening_or_say_video_title(self):
        email = self.draft()["email_body"]
        first = " ".join(email.split()[:8])
        dm = first + " so we would love a short chat about a tutorial together soon"
        self.assertTrue(any("dm repeats" in e for e in validate_draft(self.draft(dm=dm), self.videos)))
        titled = self.draft(dm="Your video title Google Slides tips stood out. EDXSO would love to explore a tutorial. Open to chat?")
        self.assertTrue(any("video title" in e for e in validate_draft(titled, self.videos)))

    def test_variety_choices_are_stable_per_creator_but_spread_across_a_batch(self):
        from src.personalization import OPENING_STYLES, pick
        self.assertEqual(pick(OPENING_STYLES, "UCabc"), pick(OPENING_STYLES, "UCabc"))
        spread = {pick(OPENING_STYLES, f"UC{i}") for i in range(40)}
        self.assertEqual(len(spread), len(OPENING_STYLES))

    def test_duplicate_openings_across_creators_are_rejected(self):
        taken = {opening(self.draft()["email_body"])}
        self.assertTrue(any("duplicates" in e for e in validate_draft(self.draft(), self.videos, openings=taken)))

    def test_titles_are_cleaned_before_quoting(self):
        self.assertEqual(clean_title("Top STEM Tools #shorts #viral pt 337 \U0001F680"), "Top STEM Tools")

    def test_angle_choice_is_deterministic_and_rule_based(self):
        campaign = {"collaboration_angles": [
            {"id": "amb", "label": "a", "pitch": "p", "when": {"min_engagement": 4, "min_subscribers": 10000}},
            {"id": "k12", "label": "b", "pitch": "p", "when": {"brand_tier": "PRIORITY"}},
            {"id": "default", "label": "c", "pitch": "p"}]}
        row = lambda **kw: {"engagement_rate": 1.0, "subscribers": 6000, "brand_tier": "STANDARD", **kw}
        self.assertEqual(choose_angle(row(engagement_rate=5, subscribers=20000), campaign)["id"], "amb")
        self.assertEqual(choose_angle(row(brand_tier="PRIORITY"), campaign)["id"], "k12")
        self.assertEqual(choose_angle(row(), campaign)["id"], "default")


class RevalidationTests(TempDatabase):
    def test_drafts_that_fail_todays_rules_are_blocked_without_a_model_call(self):
        from src.personalization import revalidate_messages
        from src.outreach import approve
        self.add_profile(status="PASSED", email="a@b.dev", source="s")
        self.add_videos(title="Google Slides tips")
        self.add_message(review="APPROVED", body="We loved your video and highlights of Google Slides. " * 6)
        with db.connect() as c:
            c.execute("UPDATE messages SET referenced_video_id=?", (f"v-{self.channel_id}-0",))
        counts = revalidate_messages()
        self.assertEqual(counts["invalidated"], 1)
        with db.connect() as c:
            row = c.execute("SELECT validation_status,review_status FROM messages").fetchone()
        self.assertEqual((row["validation_status"], row["review_status"]), ("INVALID", "PENDING_REVIEW"))
        self.assertEqual(approve(self.channel_id), "INVALID_MESSAGE")  # cannot be approved, so cannot be sent

    def test_already_sent_history_is_never_rewritten(self):
        from src.personalization import revalidate_messages
        self.add_profile(status="PASSED", email="a@b.dev", source="s")
        self.add_message(body="short")
        with db.connect() as c:
            c.execute("INSERT INTO outreach_log (campaign_id,channel_id,recipient,channel,method,status,created_at) VALUES (?,?,?,?,?,?,?)",
                      (CAMPAIGN, self.channel_id, "a@b.dev", "email", "dry_run", "SIMULATED_SENT", now_utc()))
        self.assertEqual(revalidate_messages()["invalidated"], 0)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT validation_status FROM messages").fetchone()[0], "VALID")


class RateLimitTests(TempDatabase):
    def test_retry_after_header_is_honoured_and_capped(self):
        from src.common import retry_delay
        self.assertGreaterEqual(retry_delay("7", 0, 1.0), 7.0)
        self.assertLessEqual(retry_delay("9999", 0, 1.0), 61.0)
        self.assertGreater(retry_delay(None, 3, 2.0), retry_delay(None, 0, 2.0) - 2.0)  # exponential growth

    def test_throttle_enforces_a_minimum_gap(self):
        import time
        from src.common import throttle
        throttle("t-test", 0.2)
        start = time.monotonic()
        throttle("t-test", 0.2)
        self.assertGreaterEqual(time.monotonic() - start, 0.15)

    def _http_429(self, body):
        import io
        from email.message import Message
        from urllib.error import HTTPError
        return HTTPError("http://x", 429, "Too Many Requests", Message(), io.BytesIO(body.encode()))

    def test_daily_quota_429_fails_fast_instead_of_retrying(self):
        from src.common import RateLimited, request_json, retry_hint_seconds
        body = '{"error": {"message": "Quota exceeded for metric. Please retry in 12h53m4.5s"}}'
        self.assertAlmostEqual(retry_hint_seconds(None, body), 12 * 3600 + 53 * 60 + 4.5)
        with patch("src.common.urlopen", side_effect=self._http_429(body)) as opened, patch("src.common.time.sleep") as nap:
            with self.assertRaises(RateLimited) as caught:
                request_json("http://x", attempts=5)
        self.assertEqual(opened.call_count, 1)   # no pointless retries
        nap.assert_not_called()
        self.assertGreater(caught.exception.retry_after, 3600)

    def test_short_429_is_retried_with_the_servers_hint(self):
        from src.common import request_json
        import io
        ok = io.BytesIO(b'{"ok": true}')
        response = type("R", (), {"__enter__": lambda s: s, "__exit__": lambda s, *a: False, "read": lambda s: ok.read()})()
        with patch("src.common.urlopen", side_effect=[self._http_429('{"error":{"message":"slow down"}}'), response]), \
                patch("src.common.time.sleep") as nap:
            self.assertEqual(request_json("http://x", attempts=3), {"ok": True})
        nap.assert_called_once()

    def test_exhausted_model_falls_back_to_the_next_and_is_remembered(self):
        from src import personalization as P
        from src.common import RateLimited
        P._EXHAUSTED_MODELS.clear()
        seen = []

        def fake(model, *args):
            seen.append(model)
            if model == "primary":
                raise RateLimited("HTTP 429: daily", 46000)
            return {"subject": "s"}
        env = {"GEMINI_PERSONALIZATION_MODEL": "primary", "GEMINI_PERSONALIZATION_FALLBACK_MODELS": "backup"}
        with patch.dict("os.environ", env), patch.object(P, "_generate_with", side_effect=fake):
            first = P.gemini_generate({}, [])
            P.gemini_generate({}, [])
        self.assertEqual(first["_model"], "backup")
        self.assertEqual(seen, ["primary", "backup", "backup"])  # primary is not tried again
        P._EXHAUSTED_MODELS.clear()

    def test_repeated_429s_stop_the_batch_and_keep_it_resumable(self):
        from src import personalization
        for name in ("a", "b", "c", "d"):
            self.add_profile(name, status="PASSED", tier="STANDARD", score=70)
            self.add_videos(name)
        with patch("src.personalization.gemini_generate", side_effect=ExternalServiceError("HTTP 429: quota")) as gen:
            result = personalization.personalize()
        self.assertEqual(gen.call_count, 3)  # stopped after 3 rate-limited profiles, not all 4
        self.assertIn("rate limited", result["stopped"])
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)  # nothing half-written


class FakeTransport:
    def __init__(self, *outcomes):
        self.outcomes, self.sent = list(outcomes), []

    def send(self, to, subject, body, *, reply_to=None):
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append((to, subject, body))
        return "<id@test>"


LIVE_CONFIG = {"campaign_id": CAMPAIGN, "campaign": {"brand_name": "EDXSO", "live_outreach_approved": True},
               "sending": {"daily_cap": 2}}


class SendingTests(TempDatabase):
    def setUp(self):
        super().setUp()
        self.add_profile(status="PASSED", email="creator@example.com", source="src", tier="STANDARD", score=70)
        self.add_message()
        patcher = patch("src.outreach.config", return_value=LIVE_CONFIG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def statuses(self):
        with db.connect() as c:
            return [r["status"] for r in c.execute("SELECT status FROM outreach_log ORDER BY id")]

    def test_dry_run_never_blocks_a_later_live_send(self):
        self.assertEqual(simulate_send(self.channel_id), "SIMULATED_SENT")
        transport = FakeTransport()
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "SENT")
        self.assertEqual(len(transport.sent), 1)
        self.assertIn("unsubscribe", transport.sent[0][2])

    def test_live_send_is_idempotent(self):
        transport = FakeTransport()
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "SENT")
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "DUPLICATE_BLOCKED")
        self.assertEqual(len(transport.sent), 1)

    def test_definite_failure_releases_the_key_so_retry_works(self):
        transport = FakeTransport(DefiniteFailure("auth rejected"))
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "SEND_FAILED")
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "SENT")
        self.assertEqual(len(transport.sent), 1)

    def test_uncertain_failure_is_never_auto_retried(self):
        transport = FakeTransport(TimeoutError("after DATA"))
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "SEND_UNCERTAIN")
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "DUPLICATE_BLOCKED")
        self.assertEqual(transport.sent, [])

    def test_unapproved_campaign_cannot_email_real_creators_but_test_mode_works(self):
        unapproved = {**LIVE_CONFIG, "campaign": {"brand_name": "EDXSO"}}
        with patch("src.outreach.config", return_value=unapproved):
            transport = FakeTransport()
            self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "CAMPAIGN_NOT_APPROVED")
            self.assertEqual(send_email(self.channel_id, live=True, override_to="me@mine.dev", transport=transport), "SENT")
        self.assertEqual(transport.sent[0][0], "me@mine.dev")  # went to the operator, not the creator

    def test_suppressed_addresses_are_never_contacted(self):
        suppress("Creator@Example.com")
        transport = FakeTransport()
        self.assertEqual(send_email(self.channel_id, live=True, transport=transport), "SUPPRESSED")
        self.assertEqual(transport.sent, [])

    def test_unapproved_message_is_not_sent(self):
        with db.connect() as c:
            c.execute("UPDATE messages SET review_status='PENDING_REVIEW'")
        self.assertEqual(send_email(self.channel_id, live=True, transport=FakeTransport()), "NOT_APPROVED")

    def test_daily_cap_stops_a_batch(self):
        for name in ("UCb", "UCc"):
            self.add_profile(name, status="PASSED", email=f"{name}@example.com", source="s", tier="STANDARD", score=60)
            self.add_message(name)
        transport = FakeTransport()
        result = send_batch(live=True, limit=10, delay_seconds=0, transport=transport)
        self.assertEqual(result.get("SENT"), 2)
        self.assertEqual(result.get("DAILY_CAP_REACHED"), 1)

    def test_stale_sending_rows_are_parked_for_a_human(self):
        with db.connect() as c:
            c.execute("INSERT INTO outreach_log (idempotency_key,campaign_id,channel_id,recipient,channel,method,status,created_at) "
                      "VALUES ('k',?,?,?,?,?,?,?)", (CAMPAIGN, self.channel_id, "x@y.z", "email", "smtp", "SENDING", "2020-01-01T00:00:00+00:00"))
        send_email(self.channel_id, live=True, transport=FakeTransport())
        self.assertIn("SEND_UNCERTAIN", self.statuses())


if __name__ == "__main__":
    import unittest
    unittest.main()
