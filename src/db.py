"""SQLite persistence. Rows are stored after each stage for safe reruns."""

from __future__ import annotations

import sqlite3

from .common import DATA, DB_PATH


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                channel_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                profile_url TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                subscribers INTEGER,
                hidden_subscribers INTEGER NOT NULL DEFAULT 0,
                channel_country TEXT,
                uploads_playlist TEXT,
                discovered_by TEXT NOT NULL DEFAULT '[]',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                engagement_rate REAL,
                metric_samples INTEGER NOT NULL DEFAULT 0,
                last_upload_at TEXT,
                category TEXT,
                themes TEXT NOT NULL DEFAULT '[]',
                tone TEXT,
                tech_match INTEGER,
                school_relevance INTEGER,
                recent_relevance INTEGER,
                fit_score INTEGER,
                classification_provider TEXT,
                classification_evidence_ids TEXT NOT NULL DEFAULT '[]',
                creator_type TEXT,
                filter_status TEXT NOT NULL DEFAULT 'DISCOVERED',
                filter_reasons TEXT NOT NULL DEFAULT '[]',
                email TEXT NOT NULL DEFAULT 'Not Found',
                email_source TEXT,
                website TEXT,
                instagram_url TEXT,
                assessed_at TEXT,
                enriched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL REFERENCES profiles(channel_id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                url TEXT NOT NULL,
                views INTEGER,
                likes INTEGER,
                comments INTEGER,
                duration_seconds INTEGER
            );
            CREATE INDEX IF NOT EXISTS videos_by_channel ON videos(channel_id, published_at DESC);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL,
                channel_id TEXT NOT NULL REFERENCES profiles(channel_id),
                subject TEXT NOT NULL,
                email_body TEXT NOT NULL,
                dm TEXT NOT NULL,
                referenced_video_id TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                generation_model TEXT,
                validation_status TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(campaign_id, channel_id)
            );
            -- One row per (query, strategy, page): makes discovery resumable and
            -- stops a rerun from paying 100 quota units for a search already done.
            CREATE TABLE IF NOT EXISTS search_runs (
                query TEXT NOT NULL,
                strategy TEXT NOT NULL,
                page_index INTEGER NOT NULL,
                next_page_token TEXT,
                results INTEGER NOT NULL DEFAULT 0,
                new_channels INTEGER NOT NULL DEFAULT 0,
                executed_at TEXT NOT NULL,
                PRIMARY KEY (query, strategy, page_index)
            );
            -- Local ledger of YouTube quota units, so a budget can be enforced
            -- before the API starts refusing requests.
            CREATE TABLE IF NOT EXISTS api_usage (
                day TEXT NOT NULL,
                api TEXT NOT NULL,
                units INTEGER NOT NULL DEFAULT 0,
                calls INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, api)
            );
            -- Provenance of every stage execution (settings + policy in force).
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                config_hash TEXT,
                policy_version TEXT,
                result TEXT
            );
            -- Addresses that must never be contacted again (opt-outs, bounces).
            CREATE TABLE IF NOT EXISTS suppressions (
                email TEXT PRIMARY KEY,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outreach_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT UNIQUE,
                campaign_id TEXT NOT NULL,
                channel_id TEXT NOT NULL REFERENCES profiles(channel_id),
                message_id INTEGER REFERENCES messages(id),
                recipient TEXT,
                channel TEXT NOT NULL,
                method TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                error TEXT
            );
            """
        )
        for table, columns in MIGRATIONS.items():
            ensure_columns(db, table, columns)


# Columns added after the first release. Declared as data so a new column is a
# one-line change and an old database upgrades in place without losing rows.
MIGRATIONS: dict[str, dict[str, str]] = {
    "profiles": {
        "classification_provider": "TEXT",
        "classification_evidence_ids": "TEXT NOT NULL DEFAULT '[]'",
        "creator_type": "TEXT",
        "classification_reason": "TEXT",
        "median_views": "INTEGER",
        "reach_ratio": "REAL",
        "engagement_basis": "TEXT",
        "brand_tier": "TEXT",
        "filter_reason_codes": "TEXT NOT NULL DEFAULT '[]'",
        "policy_version": "TEXT",
        "videos_depth": "INTEGER NOT NULL DEFAULT 0",
        "email_source_kind": "TEXT",
    },
    "messages": {
        "generation_model": "TEXT",
        "collaboration_type": "TEXT",
    },
    "outreach_log": {
        "provider_message_id": "TEXT",
    },
}


def ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def one(db: sqlite3.Connection, sql: str, args: tuple = ()) -> sqlite3.Row | None:
    return db.execute(sql, args).fetchone()


def all_rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return db.execute(sql, args).fetchall()


def record_run(stage: str, started_at: str, finished_at: str, policy_version: str | None,
               result: dict) -> None:
    """Append a provenance row: which stage ran, under which settings, with what outcome."""
    import json
    from .common import config_hash
    with connect() as db:
        db.execute(
            "INSERT INTO runs (stage,started_at,finished_at,config_hash,policy_version,result) VALUES (?,?,?,?,?,?)",
            (stage, started_at, finished_at, config_hash(), policy_version,
             json.dumps(result, default=str)),
        )
