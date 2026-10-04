"""The filtering policy is pure, so these tests need no database or network."""

import unittest

from src.policy import (channel_metrics, engagement_proxy, evaluate, fit_score, hard_failures, usable_video)

RULES = {"minimum_subscribers": 5000, "maximum_subscribers": 100000, "minimum_usable_videos": 3,
         "minimum_video_duration_seconds": 181, "minimum_views_per_video": 50, "maximum_days_since_upload": 180,
         "minimum_median_views": 200, "minimum_reach_ratio": 0.01, "minimum_engagement_rate": 0.5,
         "minimum_school_relevance": 0, "priority_school_relevance": 3, "minimum_fit_score": 50}


def video(views=1000, likes=50, comments=10, duration=600):
    return {"duration_seconds": duration, "views": views, "likes": likes, "comments": comments}


def signals(**overrides):
    base = {"subscribers": 15000, "hidden_subscribers": 0, "age_days": 5, "engagement_rate": 4.0,
            "metric_samples": 5, "engagement_basis": "long_form", "median_views": 1500, "reach_ratio": 0.1}
    return {**base, **overrides}


def labels(**overrides):
    base = {"technology_match": True, "school_relevance": 1, "recent_relevance": 3, "creator_type": "individual"}
    return {**base, **overrides}


class MeasurementTests(unittest.TestCase):
    def test_tiny_view_counts_are_noise_not_engagement(self):
        """1 like on 7 views is a 14% 'rate' that says nothing about an audience."""
        self.assertFalse(usable_video(video(views=7, likes=1, comments=0), 181, 50))
        self.assertEqual(engagement_proxy([video(views=7, likes=1, comments=0)] * 5, 181, 50), (None, 0))

    def test_hidden_counts_are_not_zero(self):
        self.assertFalse(usable_video(video(likes=None), 181, 0))

    def test_shorts_heavy_channel_falls_back_and_says_so(self):
        shorts = [video(duration=30) for _ in range(4)]
        metrics = channel_metrics(shorts, 10000, RULES)
        self.assertEqual(metrics["engagement_basis"], "all_formats")
        self.assertEqual(metrics["metric_samples"], 4)

    def test_long_form_is_preferred_when_available(self):
        videos = [video(duration=600) for _ in range(3)] + [video(duration=30) for _ in range(10)]
        self.assertEqual(channel_metrics(videos, 10000, RULES)["engagement_basis"], "long_form")

    def test_no_usable_videos_is_insufficient(self):
        self.assertEqual(channel_metrics([], 10000, RULES)["engagement_basis"], "insufficient")


class RuleTests(unittest.TestCase):
    def codes(self, **overrides):
        return [reason.code for reason in hard_failures(signals(**overrides), RULES)]

    def test_follower_range_short_circuits(self):
        self.assertEqual(self.codes(subscribers=300), ["SUBS_BELOW_MIN"])
        self.assertEqual(self.codes(subscribers=300000), ["SUBS_ABOVE_MAX"])
        self.assertEqual(self.codes(subscribers=None), ["SUBS_UNAVAILABLE"])

    def test_low_reach_and_zero_engagement_fail(self):
        self.assertIn("LOW_REACH", self.codes(median_views=14, reach_ratio=0.002))
        self.assertIn("LOW_REACH_RATIO", self.codes(median_views=300, reach_ratio=0.002))
        self.assertIn("LOW_ENGAGEMENT", self.codes(engagement_rate=0.0))

    def test_too_few_videos_stops_before_reach_checks(self):
        self.assertEqual(self.codes(metric_samples=1, engagement_rate=None), ["TOO_FEW_VIDEOS"])

    def test_stale_channel_fails(self):
        self.assertIn("STALE", self.codes(age_days=400))

    def test_clean_signals_pass_hard_rules(self):
        self.assertEqual(self.codes(), [])


class DecisionTests(unittest.TestCase):
    def test_model_is_not_called_for_channels_that_fail_objective_rules(self):
        decision = evaluate(signals(subscribers=300), None, RULES)
        self.assertEqual(decision.status, "FAILED")

    def test_eligible_channel_without_label_needs_classification(self):
        self.assertEqual(evaluate(signals(), None, RULES).status, "NEEDS_CLASSIFICATION")

    def test_k12_fit_ranks_but_does_not_gate(self):
        priority = evaluate(signals(), labels(school_relevance=3), RULES)
        standard = evaluate(signals(), labels(school_relevance=1), RULES)
        self.assertEqual((priority.status, priority.brand_tier), ("PASSED", "PRIORITY"))
        self.assertEqual((standard.status, standard.brand_tier), ("PASSED", "STANDARD"))
        self.assertGreater(priority.score, standard.score)

    def test_strict_k12_mode_is_one_config_change(self):
        strict = {**RULES, "minimum_school_relevance": 3}
        decision = evaluate(signals(), labels(school_relevance=1), strict)
        self.assertIn("SCHOOL_BELOW_MIN", [r.code for r in decision.reasons])

    def test_organizations_and_non_tech_fail_with_codes(self):
        org = evaluate(signals(), labels(creator_type="organization"), RULES)
        off_topic = evaluate(signals(), labels(technology_match=False), RULES)
        self.assertIn("NOT_CREATOR_LED", [r.code for r in org.reasons])
        self.assertIn("NOT_TECH", [r.code for r in off_topic.reasons])

    def test_fit_score_is_bounded(self):
        best = fit_score(labels(school_relevance=3), signals(engagement_rate=50, reach_ratio=5, age_days=0), "IN")
        self.assertLessEqual(best, 100)
        self.assertEqual(best, 100)


if __name__ == "__main__":
    unittest.main()
