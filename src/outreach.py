"""Human review and idempotent dry-run outreach."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

from .common import config, normalize_email, now_utc
from .db import connect, init_db, one
from .enrichment import valid_email
from .mailer import DefiniteFailure, DryRunTransport, SMTPTransport
from .personalization import validate_draft
from .db import all_rows


def approve(channel_id: str) -> str:
    init_db()
    campaign = config()["campaign_id"]
    with connect() as db:
        message = one(db, "SELECT * FROM messages WHERE campaign_id=? AND channel_id=?", (campaign, channel_id))
        if not message:
            return "NO_MESSAGE"
        if message["validation_status"] != "VALID":
            return "INVALID_MESSAGE"
        db.execute("UPDATE messages SET review_status='APPROVED',updated_at=? WHERE id=?", (now_utc(), message["id"]))
    return "APPROVED"


def reject(channel_id: str) -> str:
    init_db()
    campaign = config()["campaign_id"]
    with connect() as db:
        message = one(db, "SELECT id FROM messages WHERE campaign_id=? AND channel_id=?", (campaign, channel_id))
        if not message:
            return "NO_MESSAGE"
        db.execute("UPDATE messages SET review_status='REJECTED',updated_at=? WHERE id=?", (now_utc(), message["id"]))
    return "REJECTED"


def save_message(channel_id: str, subject: str, email_body: str, dm: str,
                 referenced_video_id: str) -> list[str]:
    """Save a reviewer edit, resetting approval until the edited copy is reviewed."""
    init_db()
    campaign = config()["campaign_id"]
    with connect() as db:
        message = one(db, "SELECT id FROM messages WHERE campaign_id=? AND channel_id=?", (campaign, channel_id))
        videos = all_rows(db, "SELECT video_id,title FROM videos WHERE channel_id=?", (channel_id,))
        if not message:
            return ["No generated message exists for this creator"]
        if one(db, "SELECT id FROM outreach_log WHERE campaign_id=? AND channel_id=? AND status='SIMULATED_SENT' LIMIT 1", (campaign, channel_id)):
            return ["This message already has a simulated send; its history cannot be edited"]
        draft = {"subject": subject, "email_body": email_body, "dm": dm,
                 "referenced_video_id": referenced_video_id}
        errors = validate_draft(draft, videos)
        if errors:
            return errors
        db.execute(
            """UPDATE messages SET subject=?,email_body=?,dm=?,referenced_video_id=?,
            validation_status='VALID',review_status='PENDING_REVIEW',updated_at=? WHERE id=?""",
            (subject.strip(), email_body.strip(), dm.strip(), referenced_video_id, now_utc(), message["id"]),
        )
    return []


def _log(db, campaign: str, channel_id: str, message_id: int | None, recipient: str | None,
         channel: str, method: str, status: str, key: str | None = None, error: str | None = None) -> None:
    db.execute(
        """INSERT INTO outreach_log
        (idempotency_key,campaign_id,channel_id,message_id,recipient,channel,method,status,created_at,error)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (key, campaign, channel_id, message_id, recipient, channel, method, status, now_utc(), error),
    )


SENDING_TIMEOUT_MINUTES = 10


def _recover_stale_sends(db) -> None:
    """A SENDING row older than the timeout means the process died mid-send. The outcome
    is unknown, so it is parked for a human rather than retried."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=SENDING_TIMEOUT_MINUTES)).isoformat(timespec="microseconds")
    db.execute("UPDATE outreach_log SET status='SEND_UNCERTAIN',error='process stopped mid-send; verify in the mailbox' "
               "WHERE status='SENDING' AND created_at<?", (cutoff,))


def _footer(brand: str) -> str:
    return (f"\n\n--\nSent on behalf of {brand}. If you would rather not hear from us, "
            "reply with 'unsubscribe' and we will not contact you again.")


def _reserve(channel_id: str, *, live: bool, override_to: str | None) -> tuple[str, dict | None]:
    """Atomically decide whether to send and claim the idempotency key.

    Returns (code, context). Only code 'RESERVED' allows a send to proceed."""
    init_db()
    campaign = config()["campaign_id"]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _recover_stale_sends(db)
        profile = one(db, "SELECT * FROM profiles WHERE channel_id=?", (channel_id,))
        message = one(db, "SELECT * FROM messages WHERE campaign_id=? AND channel_id=?", (campaign, channel_id))
        if not profile or profile["filter_status"] != "PASSED" or not message:
            return "NOT_ELIGIBLE", None
        email = profile["email"]
        if email == "Not Found" or not valid_email(email):
            _log(db, campaign, channel_id, message["id"], email, "email", "dry_run" if not live else "smtp", "SKIPPED_NO_EMAIL")
            return "SKIPPED_NO_EMAIL", None
        if message["review_status"] != "APPROVED" or message["validation_status"] != "VALID":
            return "NOT_APPROVED", None
        normalized = normalize_email(email)
        if one(db, "SELECT 1 FROM suppressions WHERE email=?", (normalized,)):
            _log(db, campaign, channel_id, message["id"], normalized, "email", "smtp" if live else "dry_run", "SUPPRESSED")
            return "SUPPRESSED", None
        if live and not override_to:
            if not config()["campaign"].get("live_outreach_approved"):
                return "CAMPAIGN_NOT_APPROVED", None
            cap = int(config().get("sending", {}).get("daily_cap", 20))
            today = datetime.now(timezone.utc).date().isoformat()
            sent = one(db, "SELECT COUNT(*) n FROM outreach_log WHERE status IN ('SENT','SENDING','SEND_UNCERTAIN') AND created_at>=?", (today,))["n"]
            if sent >= cap:
                return "DAILY_CAP_REACHED", None
        # Separate namespaces: a dry run must never block a later real send, and a
        # test send to the operator must never count as outreach to the creator.
        if override_to:
            name, recipient, method = "email_test", normalize_email(override_to), "smtp_test"
            raw = f"{campaign}|{name}|{recipient}|{channel_id}"
        elif live:
            name, recipient, method = "email_live", normalized, "smtp"
            raw = f"{campaign}|{name}|{normalized}"
        else:
            name, recipient, method = "email", normalized, "dry_run"
            raw = f"{campaign}|{name}|{normalized}"
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if one(db, "SELECT id FROM outreach_log WHERE idempotency_key=?", (key,)):
            _log(db, campaign, channel_id, message["id"], recipient, "email", method, "DUPLICATE_BLOCKED")
            return "DUPLICATE_BLOCKED", None
        _log(db, campaign, channel_id, message["id"], recipient, "email", method, "SENDING", key)
        row_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return "RESERVED", {"row_id": row_id, "recipient": recipient, "message": dict(message), "method": method}


def _settle(row_id: int, status: str, *, error: str | None = None, release_key: bool = False,
            provider_id: str | None = None) -> None:
    with connect() as db:
        db.execute("UPDATE outreach_log SET status=?,error=?,provider_message_id=?,"
                   "idempotency_key=CASE WHEN ? THEN NULL ELSE idempotency_key END WHERE id=?",
                   (status, error, provider_id, int(release_key), row_id))


def send_email(channel_id: str, *, live: bool = False, override_to: str | None = None,
               transport=None) -> str:
    """Send (or simulate) the approved email for one creator. Returns a status code."""
    code, context = _reserve(channel_id, live=live, override_to=override_to)
    if code != "RESERVED":
        return code
    brand = config()["campaign"].get("brand_name", "our team")
    try:
        transport = transport or (SMTPTransport() if live else DryRunTransport())
        provider_id = transport.send(context["recipient"], context["message"]["subject"],
                                     context["message"]["email_body"] + _footer(brand))
    except DefiniteFailure as exc:
        _settle(context["row_id"], "SEND_FAILED", error=str(exc), release_key=True)
        return "SEND_FAILED"
    except Exception as exc:  # noqa: BLE001  anything else is an unknown delivery state
        _settle(context["row_id"], "SEND_UNCERTAIN", error=f"{type(exc).__name__}: {exc}"[:300])
        return "SEND_UNCERTAIN"
    status = "SENT" if live else "SIMULATED_SENT"
    _settle(context["row_id"], status, provider_id=provider_id)
    return status


def simulate_send(channel_id: str) -> str:
    return send_email(channel_id, live=False)


def send_batch(*, live: bool = False, limit: int = 10, override_to: str | None = None,
               delay_seconds: float = 20.0, transport=None) -> dict:
    """Send approved, contactable, not-yet-sent creators best-first, paced to look human."""
    init_db()
    campaign = config()["campaign_id"]
    with connect() as db:
        rows = all_rows(
            db, """SELECT p.channel_id FROM profiles p JOIN messages m
                   ON m.channel_id=p.channel_id AND m.campaign_id=?
                   WHERE p.filter_status='PASSED' AND p.email!='Not Found' AND m.review_status='APPROVED'
                   ORDER BY (p.brand_tier='PRIORITY') DESC, p.fit_score DESC""", (campaign,))
    results: dict[str, int] = {}
    attempted = 0
    for row in rows:
        if attempted >= limit:
            break
        status = send_email(row["channel_id"], live=live, override_to=override_to, transport=transport)
        results[status] = results.get(status, 0) + 1
        if status in ("SENT", "SIMULATED_SENT"):
            attempted += 1
            if live and attempted < limit:
                time.sleep(delay_seconds)
        elif status in ("DAILY_CAP_REACHED", "CAMPAIGN_NOT_APPROVED"):
            break
    return results


def suppress(email: str, reason: str = "opt-out") -> str:
    init_db()
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO suppressions (email,reason,created_at) VALUES (?,?,?)",
                   (normalize_email(email), reason, now_utc()))
    return "SUPPRESSED"


def mark_manual_dm(channel_id: str) -> str:
    init_db()
    campaign = config()["campaign_id"]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        profile = one(db, "SELECT * FROM profiles WHERE channel_id=?", (channel_id,))
        message = one(db, "SELECT * FROM messages WHERE campaign_id=? AND channel_id=?", (campaign, channel_id))
        if not profile or not message or profile["filter_status"] != "PASSED":
            return "NOT_ELIGIBLE"
        if not profile["instagram_url"]:
            return "NO_PUBLIC_INSTAGRAM_URL"
        if message["review_status"] != "APPROVED":
            return "NOT_APPROVED"
        key = hashlib.sha256(f"{campaign}|instagram_dm|{profile['instagram_url'].lower()}".encode("utf-8")).hexdigest()
        if one(db, "SELECT id FROM outreach_log WHERE idempotency_key=?", (key,)):
            _log(db, campaign, channel_id, message["id"], profile["instagram_url"],
                 "instagram_dm", "manual", "DUPLICATE_BLOCKED")
            return "DUPLICATE_BLOCKED"
        _log(db, campaign, channel_id, message["id"], profile["instagram_url"],
             "instagram_dm", "manual", "MANUAL_DM_RECORDED", key)
        return "MANUAL_DM_RECORDED"
