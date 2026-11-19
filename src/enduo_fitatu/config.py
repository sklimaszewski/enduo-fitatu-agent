"""Everything the container reads from the environment, in one place."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__

DiaryItems = Literal["auto", "eaten", "all"]

PROJECT_URL = "https://github.com/sklimaszewski/enduo-fitatu-agent"

#: The only scope the container needs: nutrition_diary_set, and nutrition_targets_get for TARGETS=on.
ENDUO_SCOPES = "write:food_log"


@dataclass(frozen=True)
class Config:
    #: Address of the Enduo API, without a trailing slash.
    enduo_url: str
    mcp_url: str
    #: The volume with both sign-ins and the sync state.
    state_dir: str
    #: How often Fitatu is read, in seconds.
    poll_seconds: int
    #: On the first run, the diary of this many days back is sent, a batch per pass.
    backfill_days: int
    #: Days read on every pass: today and the days before it.
    recent_days: int
    #: Days read again once a day, for an older day edited later.
    recheck_days: int
    #: Days of the backfill read in one pass, so the first run does not ask Fitatu for months at once.
    backfill_batch: int
    diary_items: DiaryItems
    #: Set the day's energy and macro targets of Enduo as the goal in Fitatu (TARGETS=on); off by default.
    targets: bool
    #: IANA time zone of the athlete's days (TIMEZONE); None takes the one of the Fitatu account.
    timezone: str | None
    #: Name of the container in Enduo (its personal access token).
    name: str
    version: str
    #: User-Agent of the calls to Enduo (ENDUO_USER_AGENT).
    user_agent: str


def _text(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


def _number(value: str | None, fallback: int, low: int, high: int = 1_000_000) -> int:
    try:
        parsed = int((value or "").strip())
    except ValueError:
        return fallback
    return parsed if low <= parsed <= high else fallback


def _diary_items(value: str | None) -> DiaryItems:
    wanted = (value or "").strip().lower()
    if wanted == "eaten":
        return "eaten"
    if wanted == "all":
        return "all"
    return "auto"


def _switch(value: str | None) -> bool:
    return (value or "").strip().lower() in {"on", "1", "true", "yes"}


def valid_zone(value: object) -> str | None:
    """The name of an IANA time zone this system knows, or None: for anything else, ZoneInfo raises."""
    zone = value.strip() if isinstance(value, str) else ""
    if not zone:
        return None
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return zone


def server_origin(url: str) -> str:
    """Scheme, host and port of an address, lower case: the Enduo server a sync state belongs to."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else url.strip().lower()


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    enduo_url = (_text(env.get("ENDUO_URL")) or "https://api.enduo.app").rstrip("/")
    version = _text(env.get("APP_VERSION")) or __version__

    return Config(
        enduo_url=enduo_url,
        mcp_url=_text(env.get("ENDUO_MCP_URL")) or f"{enduo_url}/mcp",
        state_dir=_text(env.get("STATE_DIR")) or "/data",
        poll_seconds=_number(env.get("POLL_SECONDS"), 300, 60),
        backfill_days=_number(env.get("BACKFILL_DAYS"), 90, 0, 3650),
        recent_days=_number(env.get("RECENT_DAYS"), 3, 1, 31),
        recheck_days=_number(env.get("RECHECK_DAYS"), 14, 0, 92),
        backfill_batch=_number(env.get("BACKFILL_BATCH"), 14, 1, 92),
        diary_items=_diary_items(env.get("DIARY_ITEMS")),
        targets=_switch(env.get("TARGETS")),
        timezone=valid_zone(env.get("TIMEZONE")),
        name=_text(env.get("ENDUO_CLIENT_NAME")) or f"Fitatu Agent ({socket.gethostname()})",
        version=version,
        user_agent=_text(env.get("ENDUO_USER_AGENT")) or f"enduo-fitatu-agent/{version} (+{PROJECT_URL})",
    )
