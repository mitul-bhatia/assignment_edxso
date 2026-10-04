"""Find explicit public business contact details for qualified creators."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from .common import load_env, now_utc
from .db import all_rows, connect, init_db, one, record_run

EMAIL = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
SOCIAL_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "facebook.com", "tiktok.com", "x.com", "twitter.com")


def valid_email(value: str) -> bool:
    if not EMAIL.fullmatch(value.strip()) or len(value) > 254:
        return False
    local, domain = value.rsplit("@", 1)
    return len(local) <= 64 and ".." not in value and not domain.startswith("-")


CONTEXT_WORDS = re.compile(
    r"business|collab|sponsor|inquir|enquir|contact|e-?mail|reach|booking|partnership|press|work with", re.I)
IGNORED_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}
IGNORED_DOMAINS = ("example.com", "example.org", "youtube.com", "google.com", "sentry.io", "wixpress.com")
# Hosting/community platforms: an address on these belongs to the platform, not the creator.
PLATFORM_DOMAINS = ("skool.com", "linktr.ee", "beacons.ai", "substack.com", "patreon.com", "gumroad.com",
                    "kajabi.com", "teachable.com", "squarespace.com", "wix.com", "notion.so", "carrd.co",
                    "hotmart.com", "podia.com", "shopify.com", "mailchimp.com", "convertkit.com")
# Role mailboxes that, on someone else's domain, are that organisation's support desk.
GENERIC_ROLES = {"help", "support", "privacy", "legal", "abuse", "billing", "security", "admin", "sales"}
# Links that can never hold a creator's email; skipping them saves crawl budget.
SKIPPED_LINK_HOSTS = SOCIAL_HOSTS + (
    "linkedin.com", "discord.gg", "discord.com", "t.me", "twitch.tv", "patreon.com", "ko-fi.com",
    "buymeacoffee.com", "amzn.to", "amazon.com", "bit.ly", "tinyurl.com", "goo.gl", "t.co", "geni.us",
    "spotify.com", "apple.com", "github.com", "docs.google.com", "forms.gle")


def _decode(text: str) -> str:
    # Pages often embed JSON: "\\u003e" is a literal ">" and must not leak into an address.
    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    decoded = html.unescape(decoded)
    # Publicly written obfuscation is decoded, but no address is predicted.
    decoded = re.sub(r"\s*(?:\[at\]|\(at\))\s*", "@", decoded, flags=re.I)
    return re.sub(r"\s*(?:\[dot\]|\(dot\))\s*", ".", decoded, flags=re.I)


def explicit_emails(text: str) -> list[str]:
    decoded = _decode(text)
    return list(dict.fromkeys(match.group(1) for match in EMAIL.finditer(decoded) if valid_email(match.group(1))))


def acceptable_email(value: str, site_host: str | None = None) -> bool:
    """Reject addresses that provably are not the creator's: no-reply boxes, platform
    domains, and (when found on someone's site) a different company's support desk."""
    local, domain = value.lower().rsplit("@", 1)
    if local in IGNORED_LOCAL or domain.endswith(IGNORED_DOMAINS + PLATFORM_DOMAINS):
        return False
    if site_host and local in GENERIC_ROLES and not (domain == site_host or site_host.endswith("." + domain)
                                                      or domain.endswith("." + site_host)):
        return False
    return True


def contextual_emails(text: str) -> list[tuple[str, bool]]:
    """Explicit addresses with a flag: did a contact-style word precede it (within 100 chars)?"""
    decoded = _decode(text)
    found: dict[str, bool] = {}
    for match in EMAIL.finditer(decoded):
        email = match.group(1)
        if valid_email(email) and acceptable_email(email):
            near = bool(CONTEXT_WORDS.search(decoded[max(0, match.start() - 100):match.start()]))
            found[email] = found.get(email, False) or near
    return list(found.items())


def mine_stored_text(description: str, videos: list, profile_url: str) -> tuple[str, str, str] | None:
    """Find a published email in text we already hold. Returns (email, source_url, kind).

    Channel description is first-party. A video description is looser: the address may
    belong to a guest or sponsor, so it is accepted only when it repeats across at least
    two videos or sits right after a contact word, and it is labelled lower-confidence.
    """
    pairs = contextual_emails(description)
    if pairs:
        pairs.sort(key=lambda pair: not pair[1])  # contact-worded first
        return pairs[0][0], profile_url, "channel_description"
    seen: dict[str, dict] = {}
    for video in videos:
        for email, near in contextual_emails(video["description"]):
            info = seen.setdefault(email, {"urls": [], "near": False})
            info["urls"].append(video["url"])
            info["near"] = info["near"] or near
    ranked = sorted(((len(i["urls"]), i["near"], e) for e, i in seen.items()
                     if len(i["urls"]) >= 2 or i["near"]), reverse=True)
    if not ranked:
        return None
    email = ranked[0][2]
    return email, seen[email]["urls"][0], "video_description"


def safe_public_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password
            or port not in (None, 80, 443) or host == "localhost"
            or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid"))):
        return False
    try:
        address = ipaddress.ip_address(host)
        return address.is_global
    except ValueError:
        return "." in host


def network_safe_url(value: str) -> bool:
    """Fail closed if a public-looking hostname resolves to a private address."""
    if not safe_public_url(value):
        return False
    host = urlsplit(value).hostname
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)}
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def host_matches(value: str, original: str) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    base = (urlsplit(original).hostname or "").lower()
    return host == base or host.endswith("." + base)


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.mailto: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if href.startswith("mailto:"):
            self.mailto.append(href.removeprefix("mailto:").split("?", 1)[0])
        elif href:
            self.links.append(href)

    def handle_data(self, data):
        self.text.append(data)


def allowed_by_robots(url: str) -> bool:
    if not network_safe_url(url):
        return False
    parsed = urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        with OPENER.open(Request(robots_url, headers={"User-Agent": "OutreachAssignmentPrototype/1.0"}), timeout=8) as response:
            robots_text = response.read(200_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            return True
        return False
    except Exception:
        return False  # fail closed when crawl permission cannot be checked
    parser = RobotFileParser()
    parser.parse(robots_text.splitlines())
    return parser.can_fetch("OutreachAssignmentPrototype/1.0", url)


def fetch_page(url: str) -> str | None:
    if not network_safe_url(url) or not allowed_by_robots(url):
        return None
    try:
        with OPENER.open(Request(url, headers={"User-Agent": "OutreachAssignmentPrototype/1.0"}), timeout=10) as response:
            if "text/html" not in response.headers.get("Content-Type", ""):
                return None
            return response.read(500_000).decode("utf-8", errors="replace")
    except Exception:
        return None


def pick_websites(description: str, limit: int = 3) -> tuple[list[str], str | None]:
    """Up to `limit` external sites worth crawling, plus a public Instagram link if present."""
    websites: list[str] = []
    instagram = None
    for match in URL.finditer(description):
        value = match.group(0).rstrip(".,);]")
        if not safe_public_url(value):
            continue
        host = (urlsplit(value).hostname or "").lower().removeprefix("www.")
        if host.endswith("instagram.com") and not instagram:
            instagram = value
        skipped = any(host == blocked or host.endswith("." + blocked) for blocked in SKIPPED_LINK_HOSTS)
        if not skipped and value not in websites and len(websites) < limit:
            websites.append(value)
    return websites, instagram


def pick_website(description: str) -> tuple[str | None, str | None]:
    websites, instagram = pick_websites(description, 1)
    return (websites[0] if websites else None), instagram


def crawl_site(website: str) -> tuple[str, str] | None:
    """Look for an explicit email on a site's landing page and up to two contact/about pages."""
    queue, visited = [website], set()
    while queue and len(visited) < 3:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = fetch_page(url)
        if not page:
            continue
        parser = PageLinks()
        parser.feed(page)
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        matches = [e for e in explicit_emails(" ".join(parser.mailto + parser.text)) if acceptable_email(e, host)]
        if matches:
            return matches[0], url
        for link in parser.links:
            next_url = urljoin(url, link)
            if (safe_public_url(next_url) and host_matches(next_url, website)
                    and re.search(r"contact|about", urlsplit(next_url).path, re.I)
                    and next_url not in visited and next_url not in queue):
                queue.append(next_url)
    return None


def find_contact(description: str, *, profile_url: str | None = None,
                 crawl: bool = True) -> tuple[str, str | None, str | None, str | None]:
    websites, instagram = pick_websites(description)
    website = websites[0] if websites else None
    direct = [e for e in explicit_emails(description) if acceptable_email(e)]
    if direct:
        return direct[0], profile_url or "YouTube channel description", website, instagram
    if crawl:
        for site in websites:
            found = crawl_site(site)
            if found:
                return found[0], found[1], website, instagram
    return "Not Found", None, website, instagram


def enrich(*, refresh: bool = False, crawl: bool = True) -> dict:
    """Two passes, cheapest first.

    1. Mine text already stored (channel + video descriptions) for EVERY profile: no
       network, so contactability is known across the whole candidate pool.
    2. Crawl linked public sites only for PASSED creators still without an email.
    A sourced email is never replaced by a later "Not Found"; a reviewer can correct it
    through add-contact.
    """
    init_db()
    load_env()
    started = now_utc()
    counts = {"profiles": 0, "mined_found": 0, "crawl_attempted": 0, "crawl_found": 0, "skipped": 0}
    with connect() as db:
        profiles = all_rows(db, "SELECT * FROM profiles ORDER BY channel_id")
    for profile in profiles:
        counts["profiles"] += 1
        if profile["email"] != "Not Found" and profile["email_source"]:
            if _still_valid(profile):
                counts["skipped"] += 1
                _backfill_kind(profile)
                continue
            counts["revalidated_rejected"] = counts.get("revalidated_rejected", 0) + 1
        with connect() as db:
            videos = all_rows(db, "SELECT url,description FROM videos WHERE channel_id=?", (profile["channel_id"],))
        websites, instagram = pick_websites(profile["description"])
        website = profile["website"] or (websites[0] if websites else None)
        instagram = profile["instagram_url"] or instagram
        email, source, kind = "Not Found", None, None
        mined = mine_stored_text(profile["description"], videos, profile["profile_url"])
        if mined:
            email, source, kind = mined
            counts["mined_found"] += 1
        elif (crawl and profile["filter_status"] == "PASSED"
              and (refresh or not profile["enriched_at"])):
            counts["crawl_attempted"] += 1
            found = next(filter(None, (crawl_site(site) for site in websites)), None)
            if found:
                (email, source), kind = found, "website"
                counts["crawl_found"] += 1
        crawled = (profile["filter_status"] == "PASSED" and crawl) or bool(profile["enriched_at"])
        with connect() as db:
            db.execute(
                "UPDATE profiles SET email=?,email_source=?,email_source_kind=?,website=?,instagram_url=?,enriched_at=? "
                "WHERE channel_id=?",
                (email, source, kind, website, instagram,
                 now_utc() if crawled else None, profile["channel_id"]),
            )
    counts["shared_addresses_flagged"] = flag_shared_addresses()
    record_run("enrich", started, now_utc(), None, counts)
    return counts


def infer_source_kind(source: str | None) -> str | None:
    if not source:
        return None
    if "youtube.com/watch" in source:
        return "video_description"
    return "channel_description" if "youtube.com/channel" in source else "website"


def _still_valid(profile) -> bool:
    """Re-check an automatically found email against today's rules. A manual verification stands."""
    if profile["email_source_kind"] == "manual_verified":
        return True
    kind = profile["email_source_kind"] or infer_source_kind(profile["email_source"])
    host = None
    if kind == "website":
        host = (urlsplit(profile["email_source"]).hostname or "").lower().removeprefix("www.")
    return valid_email(profile["email"]) and acceptable_email(profile["email"], host)


def _backfill_kind(profile) -> None:
    if not profile["email_source_kind"]:
        with connect() as db:
            db.execute("UPDATE profiles SET email_source_kind=? WHERE channel_id=?",
                       (infer_source_kind(profile["email_source"]), profile["channel_id"]))


def flag_shared_addresses() -> int:
    """One address attached to several unrelated channels is an agency or broker inbox.

    The creator did publish it, so it is kept (never discarded or guessed around), but it is
    labelled `shared_address`: it is not a mailbox unique to this creator, and the sender's
    idempotency rule means only one of those creators could ever be emailed at it."""
    with connect() as db:
        rows = all_rows(db, "SELECT channel_id,email,email_source_kind FROM profiles "
                            "WHERE email!='Not Found' AND COALESCE(email_source_kind,'')!='manual_verified'")
        owners: dict[str, list[str]] = {}
        for row in rows:
            owners.setdefault(row["email"].lower(), []).append(row["channel_id"])
        flagged = 0
        for row in rows:
            shared = len(owners[row["email"].lower()]) > 1
            if shared and row["email_source_kind"] != "shared_address":
                db.execute("UPDATE profiles SET email_source_kind='shared_address' WHERE channel_id=?", (row["channel_id"],))
                flagged += 1
            elif not shared and row["email_source_kind"] == "shared_address":
                # no longer shared: restore the original provenance
                src = db.execute("SELECT email_source FROM profiles WHERE channel_id=?", (row["channel_id"],)).fetchone()[0]
                db.execute("UPDATE profiles SET email_source_kind=? WHERE channel_id=?", (infer_source_kind(src), row["channel_id"]))
    return flagged


def record_verified_contact(channel_id: str, email: str, source_url: str) -> str:
    """Record a manually located email only when the cited public page contains it."""
    init_db()
    load_env()
    email = email.strip()
    if not valid_email(email):
        raise ValueError("Contact email is not a valid explicit address")
    if not safe_public_url(source_url):
        raise ValueError("Contact source must be a public HTTP(S) URL")
    page = fetch_page(source_url)
    if page is None:
        raise ValueError("Contact source could not be fetched under robots and network safety rules")
    parser = PageLinks()
    parser.feed(page)
    published = explicit_emails(" ".join(parser.mailto + parser.text))
    if email.lower() not in {value.lower() for value in published}:
        raise ValueError("The supplied email is not explicitly published on the cited page")
    with connect() as db:
        profile = one(db, "SELECT filter_status,website FROM profiles WHERE channel_id=?", (channel_id,))
        if not profile:
            raise ValueError("Unknown creator channel ID")
        if profile["filter_status"] != "PASSED":
            raise ValueError("Only qualified creators can receive contact enrichment")
        website = profile["website"] or f"{urlsplit(source_url).scheme}://{urlsplit(source_url).netloc}/"
        db.execute(
            "UPDATE profiles SET email=?,email_source=?,email_source_kind='manual_verified',website=?,enriched_at=? WHERE channel_id=?",
            (email, source_url, website, now_utc(), channel_id),
        )
    return "VERIFIED_PUBLIC_EMAIL_RECORDED"
