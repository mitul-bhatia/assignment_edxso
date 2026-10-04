"""Configuration, HTTP, and small shared helpers."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "outreach.sqlite3"


class ExternalServiceError(RuntimeError):
    """An API call failed without exposing its credential-bearing URL."""


class RateLimited(ExternalServiceError):
    """HTTP 429. `retry_after` (seconds) is the server's hint when it gave one; a long hint
    means a daily quota, where retrying immediately cannot help."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaBudgetExceeded(ExternalServiceError):
    """The daily API budget is spent. Callers stop cleanly and resume later."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def required_env(name: str) -> str:
    load_env()
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is missing. Copy .env.example to .env and add the key.")
    return value


def config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def config_hash() -> str:
    """Stable fingerprint of config.json so a run can be tied to its settings."""
    canonical = json.dumps(config(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def chunks(values: list, size: int = 50):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def request_json(
    url: str,
    *,
    params: dict | None = None,
    method: str = "GET",
    headers: dict | None = None,
    body: dict | None = None,
    attempts: int = 3,
    backoff: float = 1.0,
) -> dict:
    """JSON over HTTP with retry. Rate limits (429) honour the server's Retry-After header;
    other transient errors use exponential backoff with jitter so parallel callers spread out."""
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, method=method, headers=headers or {})
    for index in range(attempts):
        try:
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read(4000).decode("utf-8", errors="replace")
            # The response body normally has no API key; retain only an error summary.
            try:
                detail = str(json.loads(raw).get("error", {}).get("message", "API error"))
            except (ValueError, AttributeError):
                found = re.search(r'"message":\s*"([^"]{0,300})', raw)
                detail = found.group(1) if found else "API error"
            hint = retry_hint_seconds(exc.headers.get("Retry-After") if exc.headers else None, raw)
            if exc.code == 429 and hint is not None and hint > MAX_USEFUL_WAIT:
                # A daily quota: waiting inside this process cannot help, so fail fast.
                raise RateLimited(f"HTTP 429: {detail[:160]} (retry in about {hint / 60:.0f} min)", hint) from None
            if exc.code in (429, 500, 502, 503, 504) and index + 1 < attempts:
                time.sleep(retry_delay(exc.headers.get("Retry-After") if exc.headers else None, index, backoff))
                continue
            if exc.code == 429:
                raise RateLimited(f"HTTP 429: {detail[:160]}", hint) from None
            raise ExternalServiceError(f"HTTP {exc.code}: {detail}") from None
        except (URLError, TimeoutError) as exc:
            if index + 1 < attempts:
                time.sleep(2**index)
                continue
            raise ExternalServiceError(f"Network request failed: {type(exc).__name__}") from None
    raise ExternalServiceError("Request failed after retries")


MAX_USEFUL_WAIT = 60.0  # seconds; longer than this and retrying within the run is pointless


def retry_hint_seconds(header: str | None, body: str = "") -> float | None:
    """How long the server says to wait: the Retry-After header, else a 'retry in 1h2m3.4s' phrase."""
    try:
        if header is not None:
            return float(header)
    except ValueError:
        pass
    found = re.search(r"retry in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", body, re.I)
    if found and any(found.groups()):
        hours, minutes, seconds = (float(part or 0) for part in found.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


def retry_delay(retry_after: str | None, attempt: int, base: float) -> float:
    """Seconds to wait before retry `attempt`: the server's hint if given, else base * 2^attempt, jittered."""
    jitter = random.uniform(0, base)
    try:
        if retry_after is not None:
            return min(60.0, max(0.0, float(retry_after))) + jitter
    except ValueError:
        pass
    return min(60.0, base * 2**attempt) + jitter


_last_call: dict[str, float] = {}


def throttle(name: str, min_interval: float) -> None:
    """Client-side pacing: sleep so calls under `name` are at least `min_interval` apart.
    Cheaper than discovering the quota by being rejected, and keeps a long batch steady."""
    wait = _last_call.get(name, 0.0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[name] = time.monotonic()


def word_count(value: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", value, flags=re.UNICODE))


def normalize_email(value: str) -> str:
    return value.strip().lower()
