"""Discover Technology creators using YouTube Data API v3.

Design notes (each one answers a measured problem, not a guess):

* Retrieval diversity beats parameter tuning. A live experiment showed that
  `videoDuration` filters did not reliably raise precision, but different search
  strategies returned largely *different* in-range channels. So the plan is a matrix
  of queries x strategies x pages, not one clever query.
* Quota asymmetry. search.list costs 100 units; channels/playlistItems/videos cost 1.
  Searching is therefore the scarce resource: every search is recorded (resumable,
  never repeated) and charged to a local ledger with a daily budget.
* Spend cheap calls to avoid expensive ones. Channel statistics (1 unit / 50
  channels) are fetched for every hit, and only channels inside the subscriber range
  get their videos downloaded and, later, an LLM call.
* One bad channel must not abort the run. Failures are isolated and counted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import quota
from .common import (ExternalServiceError, QuotaBudgetExceeded, chunks, config, now_utc,
                     request_json, required_env)
from .db import all_rows, connect, init_db, one, record_run

YOUTUBE = "https://www.googleapis.com/youtube/v3"

# Strategy name -> extra search.list parameters. "video" is the original behaviour
# and keeps its plain labels so existing discovered_by data stays valid.
STRATEGIES = {
    "video": lambda after: {"type": "video", "order": "relevance", "publishedAfter": after},
    "video_medium": lambda after: {"type": "video", "order": "relevance", "publishedAfter": after,
                                   "videoDuration": "medium"},
    "channel": lambda after: {"type": "channel", "order": "relevance"},
}


def youtube_get(path: str, params: dict) -> dict:
    quota.charge(path)
    try:
        return request_json(f"{YOUTUBE}/{path}", params={**params, "key": required_env("YOUTUBE_API_KEY")})
    except ExternalServiceError as exc:
        if "quota" in str(exc).lower():
            quota.mark_exhausted()
            raise QuotaBudgetExceeded("YouTube reports the daily quota is exhausted") from None
        raise


def duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def label(query: str, strategy: str) -> str:
    return query if strategy == "video" else f"{query} [{strategy}]"


def search_plan(settings: dict) -> list[tuple[int, str, str]]:
    """(page, strategy, query) ordered so a budget-truncated run still has breadth."""
    strategies = settings.get("search_strategies", ["video"])
    unknown = [name for name in strategies if name not in STRATEGIES]
    if unknown:
        raise ValueError(f"Unknown search strategies: {', '.join(unknown)}")
    pages = int(settings.get("search_pages_per_query", 1))
    return [(page, strategy, query) for page in range(pages)
            for strategy in strategies for query in settings["queries"]]


def _seed_legacy_searches() -> None:
    """Mark pre-existing baseline searches as done so they are never paid for twice."""
    with connect() as db:
        done = {(r["query"], r["strategy"]) for r in all_rows(db, "SELECT query,strategy FROM search_runs WHERE page_index=0")}
        seen: set[str] = set()
        for row in all_rows(db, "SELECT discovered_by FROM profiles"):
            seen.update(item for item in json.loads(row["discovered_by"]) if not item.endswith("]"))
        for query in seen:
            if (query, "video") not in done:
                db.execute("INSERT OR IGNORE INTO search_runs (query,strategy,page_index,next_page_token,results,new_channels,executed_at) "
                           "VALUES (?,?,0,NULL,0,0,?)", (query, "video", now_utc()))


def _upsert_channels(batch: list[str], labels: dict[str, set[str]]) -> int:
    response = youtube_get(
        "channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch), "maxResults": 50})
    added = 0
    with connect() as db:
        for channel in response.get("items", []):
            channel_id = channel["id"]
            snippet = channel.get("snippet", {})
            stats = channel.get("statistics", {})
            playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            existing = one(db, "SELECT discovered_by FROM profiles WHERE channel_id=?", (channel_id,))
            all_labels = sorted(set(json.loads(existing["discovered_by"]) if existing else []) | labels.get(channel_id, set()))
            timestamp = now_utc()
            db.execute(
                """INSERT INTO profiles
                (channel_id,name,profile_url,description,subscribers,hidden_subscribers,
                 channel_country,uploads_playlist,discovered_by,discovered_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                name=excluded.name,description=excluded.description,
                subscribers=excluded.subscribers,hidden_subscribers=excluded.hidden_subscribers,
                channel_country=excluded.channel_country,uploads_playlist=excluded.uploads_playlist,
                discovered_by=excluded.discovered_by,updated_at=excluded.updated_at""",
                (channel_id, snippet.get("title", "Unknown"),
                 f"https://www.youtube.com/channel/{channel_id}", snippet.get("description", ""),
                 int(stats["subscriberCount"]) if stats.get("subscriberCount") is not None else None,
                 int(bool(stats.get("hiddenSubscriberCount", False))), snippet.get("country"),
                 playlist, json.dumps(all_labels), timestamp, timestamp),
            )
            if existing is None:
                added += 1
    return added


def _label_known(channel_ids: list[str], labels: dict[str, set[str]]) -> None:
    with connect() as db:
        for channel_id in channel_ids:
            row = one(db, "SELECT discovered_by FROM profiles WHERE channel_id=?", (channel_id,))
            merged = sorted(set(json.loads(row["discovered_by"])) | labels[channel_id])
            db.execute("UPDATE profiles SET discovered_by=? WHERE channel_id=?", (json.dumps(merged), channel_id))


def _in_range_count(lo: int, hi: int) -> int:
    with connect() as db:
        return one(db, "SELECT COUNT(*) n FROM profiles WHERE hidden_subscribers=0 AND subscribers BETWEEN ? AND ?", (lo, hi))["n"]


def _stop_reason(settings: dict, lo: int, hi: int) -> str | None:
    """Global limits checked before every search."""
    cap = int(settings.get("max_candidates", 1500))
    target = settings.get("target_in_range_candidates")
    with connect() as db:
        total = one(db, "SELECT COUNT(*) n FROM profiles")["n"]
    if total >= cap:
        return f"candidate cap reached ({cap})"
    if target and _in_range_count(lo, hi) >= target:
        return f"target of {target} in-range candidates reached"
    return None


def _page_token(query: str, strategy: str, page: int, refresh: bool) -> tuple[bool, str | None]:
    """Decide whether this (query, strategy, page) still needs a call: (run?, token)."""
    with connect() as db:
        done = one(db, "SELECT 1 FROM search_runs WHERE query=? AND strategy=? AND page_index=?", (query, strategy, page))
        previous = one(db, "SELECT next_page_token FROM search_runs WHERE query=? AND strategy=? AND page_index=?",
                       (query, strategy, page - 1)) if page else None
    if done and not refresh:
        return False, None
    if page:
        if previous is None or not previous["next_page_token"]:
            return False, None  # earlier page missing, or it was the last
        return True, previous["next_page_token"]
    return True, None


def _store_search_page(query: str, strategy: str, page: int, response: dict) -> int:
    labels: dict[str, set[str]] = {}
    for item in response.get("items", []):
        channel_id = item.get("snippet", {}).get("channelId") or item.get("id", {}).get("channelId")
        if channel_id:
            labels.setdefault(channel_id, set()).add(label(query, strategy))
    with connect() as db:
        placeholders = ",".join("?" * len(labels))
        known = {r["channel_id"] for r in all_rows(
            db, f"SELECT channel_id FROM profiles WHERE channel_id IN ({placeholders})", tuple(labels))} if labels else set()
    added = sum(_upsert_channels(batch, labels) for batch in chunks([c for c in labels if c not in known]))
    _label_known(sorted(known), labels)
    with connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO search_runs
            (query,strategy,page_index,next_page_token,results,new_channels,executed_at) VALUES (?,?,?,?,?,?,?)""",
            (query, strategy, page, response.get("nextPageToken"), len(labels), added, now_utc()))
    return added


def _search_phase(settings: dict, lo: int, hi: int, refresh: bool, stats: dict) -> None:
    after = (datetime.now(timezone.utc) - timedelta(days=settings["search_lookback_days"])).isoformat().replace("+00:00", "Z")
    budget = int(settings.get("max_search_calls_per_run", 40))
    _seed_legacy_searches()
    for page, strategy, query in search_plan(settings):
        reason = _stop_reason(settings, lo, hi)
        if reason:
            stats["stopped_reason"] = reason
            return
        needed, token = _page_token(query, strategy, page, refresh)
        if not needed:
            stats["searches_skipped"] += 1
            continue
        if stats["searches_run"] >= budget:
            stats["stopped_reason"] = f"per-run search limit reached ({budget})"
            return
        params = {"part": "snippet", "q": query, "maxResults": 50, **STRATEGIES[strategy](after)}
        if token:
            params["pageToken"] = token
        try:
            response = youtube_get("search", params)
        except QuotaBudgetExceeded:
            raise
        except ExternalServiceError as exc:
            _note_error(stats, f"search '{query}' [{strategy}]: {exc}")
            continue
        stats["searches_run"] += 1
        stats["new"] += _store_search_page(query, strategy, page, response)


def _note_error(stats: dict, message: str) -> None:
    stats["errors"] += 1
    if len(stats["error_samples"]) < 5:
        stats["error_samples"].append(message)


def _refresh_stats() -> None:
    with connect() as db:
        ids = [r["channel_id"] for r in all_rows(db, "SELECT channel_id FROM profiles")]
    for batch in chunks(ids):
        _upsert_channels(batch, {})


def _fetch_playlist(channel, depth: int, stats: dict) -> tuple[dict, bool]:
    """Recent uploads for one channel -> ({video_id: fields}, finished?). Errors stay local."""
    try:
        response = youtube_get(
            "playlistItems", {"part": "snippet,contentDetails", "playlistId": channel["uploads_playlist"],
                              "maxResults": min(50, depth)})
    except QuotaBudgetExceeded:
        raise
    except ExternalServiceError as exc:
        _note_error(stats, f"videos for {channel['channel_id']}: {exc}")
        return {}, str(exc).startswith("HTTP 404")  # 404 is permanent; anything else is retried next run
    videos = {}
    for item in response.get("items", []):
        video_id = item.get("contentDetails", {}).get("videoId")
        snippet = item.get("snippet", {})
        published_at = item.get("contentDetails", {}).get("videoPublishedAt") or snippet.get("publishedAt")
        if video_id and published_at:
            videos[video_id] = (channel["channel_id"], snippet.get("title", ""),
                                snippet.get("description", ""), published_at)
    return videos, True


def _store_video_stats(videos: dict[str, tuple]) -> None:
    for batch in chunks(list(videos)):
        response = youtube_get("videos", {"part": "statistics,contentDetails", "id": ",".join(batch), "maxResults": 50})
        with connect() as db:
            for video in response.get("items", []):
                video_id = video["id"]
                channel_id, title, description, published_at = videos[video_id]
                counts = video.get("statistics", {})
                db.execute(
                    """INSERT INTO videos
                    (video_id,channel_id,title,description,published_at,url,views,likes,comments,duration_seconds)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(video_id) DO UPDATE SET
                    title=excluded.title,description=excluded.description,published_at=excluded.published_at,
                    views=excluded.views,likes=excluded.likes,comments=excluded.comments,
                    duration_seconds=excluded.duration_seconds""",
                    (video_id, channel_id, title, description, published_at,
                     f"https://www.youtube.com/watch?v={video_id}",
                     int(counts["viewCount"]) if counts.get("viewCount") is not None else None,
                     int(counts["likeCount"]) if counts.get("likeCount") is not None else None,
                     int(counts["commentCount"]) if counts.get("commentCount") is not None else None,
                     duration_seconds(video.get("contentDetails", {}).get("duration"))),
                )


def _video_phase(settings: dict, lo: int, hi: int, stats: dict) -> None:
    """Download recent videos, but only for in-range channels not yet fetched this deep."""
    depth = int(settings["videos_per_channel"])
    with connect() as db:
        targets = all_rows(
            db, """SELECT channel_id,uploads_playlist FROM profiles
                   WHERE uploads_playlist IS NOT NULL AND hidden_subscribers=0
                   AND subscribers BETWEEN ? AND ? AND videos_depth < ?""", (lo, hi, depth))
    for group in chunks(targets, 25):
        videos: dict[str, tuple] = {}
        completed: list[str] = []
        for channel in group:
            found, finished = _fetch_playlist(channel, depth, stats)
            videos.update(found)
            if finished:
                completed.append(channel["channel_id"])
        _store_video_stats(videos)
        # Progress is committed per group, so an interrupted run resumes where it stopped.
        with connect() as db:
            db.executemany("UPDATE profiles SET videos_depth=?,updated_at=? WHERE channel_id=?",
                           [(depth, now_utc(), channel_id) for channel_id in completed])


def discover(*, refresh: bool = False) -> dict:
    init_db()
    started = now_utc()
    settings = config()
    rules = settings.get("filters", {})
    lo, hi = rules.get("minimum_subscribers", 5000), rules.get("maximum_subscribers", 100000)
    stats: dict = {"searches_run": 0, "searches_skipped": 0, "new": 0, "errors": 0,
                   "error_samples": [], "stopped_reason": None}
    try:
        _search_phase(settings, lo, hi, refresh, stats)
        if refresh:
            _refresh_stats()
        _video_phase(settings, lo, hi, stats)
    except QuotaBudgetExceeded as exc:
        stats["stopped_reason"] = str(exc)
    with connect() as db:
        stats["stored"] = db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        stats["videos"] = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        stats["in_range"] = _in_range_count(lo, hi)
    stats["error_samples"] = stats["error_samples"][:5]
    stats["quota_used_today"] = quota.units_today()
    record_run("discover", started, now_utc(), None, stats)
    return stats
