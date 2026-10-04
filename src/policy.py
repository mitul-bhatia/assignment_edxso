"""Filtering policy: measurements in, an explainable decision out.

Everything here is a pure function (no database, network or LLM), which gives
three properties the rest of the system relies on:

* Testable: a decision is reproducible from its inputs, so unit tests need no fixtures.
* Cheap to change: the stored *signals* (subscribers, engagement, model labels)
  are kept separate from the *decision*, so a threshold change is re-applied with
  `main.py rescore` without paying for another LLM call.
* Auditable: every outcome carries machine-readable reason codes, so failures can
  be counted by cause rather than by reading free text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

# Bump when the rules or scoring change. Stored on every profile, so a stale
# decision is detectable and rescore knows what to recompute.
POLICY_VERSION = "tier-v2"

CREATOR_LED = {"individual", "creator_team"}


@dataclass(frozen=True)
class Reason:
    code: str
    text: str


@dataclass
class Decision:
    status: str  # PASSED | FAILED | NEEDS_CLASSIFICATION
    reasons: list[Reason] = field(default_factory=list)
    score: int | None = None
    brand_tier: str | None = None  # PRIORITY (direct K-12 fit) | STANDARD


# ---------------------------------------------------------------- measurement

def usable_video(video, minimum_duration: int, minimum_views: int) -> bool:
    """A video that can contribute to a rate.

    Views must clear a noise floor: one like on seven views is a "14%" rate that
    says nothing about an audience. Missing (hidden) counts are never treated as zero.
    """
    duration, views = video["duration_seconds"], video["views"]
    likes, comments = video["likes"], video["comments"]
    if minimum_duration > 0 and (duration is None or duration < minimum_duration):
        return False
    if views is None or views < max(1, minimum_views):
        return False
    return likes is not None and comments is not None


def engagement_proxy(videos: list, minimum_duration: int = 181, minimum_views: int = 0) -> tuple[float | None, int]:
    """Median (likes + comments) / views * 100 over usable videos."""
    rates = [(v["likes"] + v["comments"]) / v["views"] * 100
             for v in videos if usable_video(v, minimum_duration, minimum_views)]
    return (round(median(rates), 4), len(rates)) if rates else (None, 0)


def metric_videos(videos: list, rules: dict, basis: str | None) -> list:
    """The exact videos behind a stored engagement rate (used for exports and audits)."""
    if basis not in ("long_form", "all_formats"):
        return []
    duration = rules["minimum_video_duration_seconds"] if basis == "long_form" else 0
    return [v for v in videos if usable_video(v, duration, rules.get("minimum_views_per_video", 0))]


def channel_metrics(videos: list, subscribers: int | None, rules: dict) -> dict:
    """Engagement, reach and the basis they were computed on.

    Long-form videos are preferred because Shorts and long videos have different
    engagement distributions. Channels that publish mostly Shorts fall back to
    all formats, and the basis is recorded so the two are never silently compared.
    """
    floor = rules.get("minimum_views_per_video", 0)
    needed = rules["minimum_usable_videos"]
    rate, samples, basis = None, 0, "insufficient"
    for candidate, duration in (("long_form", rules["minimum_video_duration_seconds"]), ("all_formats", 0)):
        rate, samples = engagement_proxy(videos, duration, floor)
        if samples >= needed:
            basis = candidate
            break
    sample = metric_videos(videos, rules, basis)
    median_views = int(median(v["views"] for v in sample)) if sample else None
    reach = round(median_views / subscribers, 4) if median_views is not None and subscribers else None
    return {"engagement_rate": rate, "metric_samples": samples, "engagement_basis": basis,
            "median_views": median_views, "reach_ratio": reach}


# ------------------------------------------------------------------- decision

def hard_failures(signals: dict, rules: dict) -> list[Reason]:
    """Objective rules, ordered cheapest and most selective first."""
    subscribers = signals["subscribers"]
    if subscribers is None or signals.get("hidden_subscribers"):
        return [Reason("SUBS_UNAVAILABLE", "Public subscriber count is unavailable")]
    if subscribers < rules["minimum_subscribers"]:
        return [Reason("SUBS_BELOW_MIN", f"{subscribers:,} subscribers is below {rules['minimum_subscribers']:,}")]
    if subscribers > rules["maximum_subscribers"]:
        return [Reason("SUBS_ABOVE_MAX", f"{subscribers:,} subscribers exceeds {rules['maximum_subscribers']:,}")]

    reasons: list[Reason] = []
    age = signals["age_days"]
    if age is None:
        reasons.append(Reason("NO_UPLOAD_DATA", "No dated public upload was available"))
    elif age > rules["maximum_days_since_upload"]:
        reasons.append(Reason("STALE", f"Latest upload was {age} days ago; maximum is {rules['maximum_days_since_upload']}"))

    samples, rate = signals["metric_samples"], signals["engagement_rate"]
    if samples < rules["minimum_usable_videos"] or rate is None:
        reasons.append(Reason(
            "TOO_FEW_VIDEOS",
            f"Only {samples} videos with usable public metrics; need {rules['minimum_usable_videos']}"))
        return reasons  # reach/engagement are meaningless without a sample

    views = signals["median_views"] or 0
    if views < rules.get("minimum_median_views", 0):
        reasons.append(Reason("LOW_REACH", f"Median {views:,} views per video is below {rules['minimum_median_views']:,}"))
    reach = signals["reach_ratio"]
    if reach is not None and reach < rules.get("minimum_reach_ratio", 0):
        reasons.append(Reason(
            "LOW_REACH_RATIO",
            f"Videos reach {reach:.1%} of subscribers; minimum is {rules['minimum_reach_ratio']:.1%}"))
    if rate < rules.get("minimum_engagement_rate", 0):
        reasons.append(Reason(
            "LOW_ENGAGEMENT",
            f"Engagement {rate:.2f}% is below {rules['minimum_engagement_rate']}% "
            "(zero can also mean likes are hidden)"))
    return reasons


def fit_score(classification: dict, signals: dict, country: str | None) -> int:
    """0-100 ranking score. It orders candidates; it is not what decides the gate."""
    rate = signals["engagement_rate"] or 0.0
    reach = signals["reach_ratio"] or 0.0
    age = signals["age_days"] if signals["age_days"] is not None else 10_000
    technology = 20 if classification["technology_match"] else 0
    school = round(classification["school_relevance"] * 20 / 3)
    recent = round(classification["recent_relevance"] * 20 / 3)
    engagement = min(15, round(rate * 15 / 4))
    reach_points = min(10, round(reach / 0.10 * 10))
    activity = 10 if age <= 30 else 7 if age <= 90 else 4
    geography = 5 if country == "IN" else 0
    return technology + school + recent + engagement + reach_points + activity + geography


def evaluate(signals: dict, classification: dict | None, rules: dict, country: str | None = None) -> Decision:
    """Combine measured signals and the model's labels into PASSED / FAILED.

    Returns NEEDS_CLASSIFICATION when objective rules pass but no label exists yet,
    so the caller only spends an LLM call on candidates that can still qualify.
    """
    failures = hard_failures(signals, rules)
    if failures:
        return Decision("FAILED", failures)
    if classification is None:
        return Decision("NEEDS_CLASSIFICATION")

    score = fit_score(classification, signals, country)
    if not classification["technology_match"]:
        failures.append(Reason("NOT_TECH", "Recent text does not show a recurring Technology focus"))
    if classification["school_relevance"] < rules.get("minimum_school_relevance", 0):
        failures.append(Reason("SCHOOL_BELOW_MIN", "Insufficient direct K-12 teacher or school relevance in the cited content"))
    if classification["creator_type"] not in CREATOR_LED:
        failures.append(Reason(
            "NOT_CREATOR_LED",
            f"Account type {classification['creator_type']} is not a verified creator-led micro-influencer"))
    if score < rules["minimum_fit_score"]:
        failures.append(Reason("LOW_FIT_SCORE", f"Fit score {score}/100 is below {rules['minimum_fit_score']}/100"))
    if failures:
        return Decision("FAILED", failures, score)

    priority = classification["school_relevance"] >= rules.get("priority_school_relevance", 3)
    tier = "PRIORITY" if priority else "STANDARD"
    basis = signals["engagement_basis"].replace("_", " ")
    note = "direct K-12 fit" if priority else "general technology fit"
    return Decision("PASSED", [Reason(
        "PASSED", f"{tier.title()} tier ({note}); fit score {score}/100; "
                  f"{signals['metric_samples']} metric videos ({basis}); median {signals['median_views']:,} views")],
        score, tier)
