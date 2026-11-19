from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from enduo_fitatu import main
from enduo_fitatu.config import load_config, server_origin
from enduo_fitatu.enduo import Enduo, EnduoAuthError, EnduoError
from enduo_fitatu.fitatu import Fitatu, FitatuSignedOut
from enduo_fitatu.main import SetupError, backfill_range, timezone_of
from enduo_fitatu.mapping import today


def test_names_itself_in_the_user_agent_by_default() -> None:
    assert (
        load_config({"APP_VERSION": "1.2.3"}).user_agent
        == "enduo-fitatu-agent/1.2.3 (+https://github.com/sklimaszewski/enduo-fitatu-agent)"
    )


def test_has_defaults_for_the_sync_and_ignores_values_out_of_range() -> None:
    config = load_config({"POLL_SECONDS": "5", "BACKFILL_DAYS": "x", "RECENT_DAYS": "0", "DIARY_ITEMS": "nope", "TIMEZONE": "Mars/Olympus"})

    assert (config.poll_seconds, config.backfill_days, config.recent_days, config.recheck_days) == (300, 90, 3, 14)
    assert config.diary_items == "auto"
    assert config.timezone is None


def test_takes_the_settings_from_the_environment() -> None:
    config = load_config(
        {"BACKFILL_DAYS": "0", "DIARY_ITEMS": " Eaten ", "TIMEZONE": "Europe/Warsaw", "ENDUO_URL": "https://api.enduo.ddev.site/"}
    )

    assert config.backfill_days == 0
    assert config.diary_items == "eaten"
    assert config.timezone == "Europe/Warsaw"
    assert config.mcp_url == "https://api.enduo.ddev.site/mcp"


def test_the_enduo_server_of_a_state_is_the_origin_of_its_address() -> None:
    assert server_origin("https://API.enduo.app/mcp") == "https://api.enduo.app"
    assert server_origin("https://api.enduo.ddev.site:8443/mcp") == "https://api.enduo.ddev.site:8443"


def test_the_range_of_the_backfill_command_never_goes_past_today() -> None:
    assert backfill_range(7, None, None, "2026-11-14") == ("2026-11-08", "2026-11-14")
    assert backfill_range(None, "2026-01-01", "2026-12-31", "2026-11-14") == ("2026-01-01", "2026-11-14")
    with pytest.raises(SetupError, match="--days N"):
        backfill_range(None, None, None, "2026-11-14")
    with pytest.raises(SetupError, match="after"):
        backfill_range(None, "2026-11-20", None, "2026-11-14")
    with pytest.raises(SetupError, match="1 to 3650"):
        backfill_range(0, None, None, "2026-11-14")


def session_file(path: Path, timezone: str = "Europe/Warsaw") -> Path:
    """A session file as `fitatu-api login` writes it."""
    path.write_text(
        json.dumps(
            {
                "bearer_token": "header.eyJpZCI6IjEyMzQ1NjciLCJleHAiOjQxMDI0NDQ4MDB9.signature",
                "refresh_token": "refresh",
                "fitatu_user_id": "1234567",
                "base_url": "https://pl-pl.fitatu.com/api",
                "app_timezone": timezone,
            }
        )
    )
    return path


class EnduoIn(Enduo):
    """An Enduo account with a time zone (or one that cannot be reached), without the network."""

    def __init__(self, timezone: str | None, failure: Exception | None = None) -> None:
        super().__init__(load_config({}), "token")
        self.timezone = timezone
        self.failure = failure
        self.asked = 0

    def whoami(self) -> dict[str, Any]:
        self.asked += 1
        if self.failure is not None:
            raise self.failure
        return {"user": {"id": "account-a", "timezone": self.timezone}}


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(main, "log", lines.append)
    monkeypatch.setattr(main, "_unknown_zones", set())
    monkeypatch.delenv("TZ", raising=False)
    return lines


def test_the_days_are_those_of_the_fitatu_account_unless_timezone_is_set(tmp_path: Path, said: list[str]) -> None:
    fitatu = Fitatu(session_file(tmp_path / "fitatu-session.json", "America/New_York"), env={})
    enduo = EnduoIn("Europe/Warsaw")

    assert timezone_of(load_config({}), fitatu, enduo) == "America/New_York"
    assert timezone_of(load_config({"TIMEZONE": "Asia/Tokyo"}), fitatu, enduo) == "Asia/Tokyo"
    assert (enduo.asked, said) == (0, [])


@pytest.mark.parametrize("unknown", ["Mars/Olympus", "../../etc/passwd", "Europe/Warsaw\x00", "CET-1CEST,M3.5.0", "Europe"])
def test_an_unknown_time_zone_of_the_account_falls_back_to_enduos_and_is_said_once(tmp_path: Path, said: list[str], unknown: str) -> None:
    fitatu = Fitatu(session_file(tmp_path / "fitatu-session.json", unknown), env={})
    enduo = EnduoIn("Europe/Warsaw")

    assert timezone_of(load_config({}), fitatu, enduo) == "Europe/Warsaw"
    assert timezone_of(load_config({}), fitatu, enduo) == "Europe/Warsaw"
    assert today(timezone_of(load_config({}), fitatu, enduo), datetime(2026, 11, 14, 23, 30, tzinfo=UTC)) == "2026-11-15"

    assert len(said) == 1
    assert "is not one this system knows: using Europe/Warsaw (the time zone of your Enduo account)" in said[0]
    assert "\x00" not in said[0]


@pytest.mark.parametrize(
    "enduo",
    [
        None,
        EnduoIn(None),
        EnduoIn("Mars/Olympus"),
        EnduoIn("Europe/Warsaw", EnduoError("Enduo is not reachable")),
        EnduoIn(None, EnduoAuthError("401")),
    ],
)
def test_without_a_time_zone_from_enduo_the_days_are_those_of_utc(tmp_path: Path, said: list[str], enduo: Enduo | None) -> None:
    fitatu = Fitatu(session_file(tmp_path / "fitatu-session.json", "Mars/Olympus"), env={})

    assert timezone_of(load_config({}), fitatu, enduo) == "UTC"
    assert timezone_of(load_config({}), fitatu, enduo) == "UTC"
    assert len(said) == 1 and "using UTC (UTC)" in said[0]


def test_tz_of_the_container_counts_only_when_it_is_a_time_zone(tmp_path: Path, said: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    fitatu = Fitatu(session_file(tmp_path / "fitatu-session.json", "Mars/Olympus"), env={})

    monkeypatch.setenv("TZ", ":/etc/localtime")
    assert timezone_of(load_config({}), fitatu) == "UTC"
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert timezone_of(load_config({}), fitatu) == "Europe/Berlin"
    assert timezone_of(load_config({}), fitatu, EnduoIn("Europe/Warsaw")) == "Europe/Warsaw"


def test_the_fitatu_account_of_a_session_is_its_user_id(tmp_path: Path) -> None:
    assert Fitatu(session_file(tmp_path / "fitatu-session.json"), env={}).account() == "1234567"


def test_reads_the_session_file_of_fitatu_api(tmp_path: Path) -> None:
    fitatu = Fitatu(session_file(tmp_path / "fitatu-session.json"), env={})

    assert fitatu.user_id == "1234567"
    assert fitatu.timezone == "Europe/Warsaw"
    assert fitatu.client.base_url == "https://pl-pl.fitatu.com/api"


def test_without_a_session_it_asks_for_setup(tmp_path: Path) -> None:
    with pytest.raises(FitatuSignedOut, match="enduo-fitatu setup"):
        Fitatu(tmp_path / "missing.json", env={}).day("2026-11-14")


def test_reads_a_day_through_the_planner_of_fitatu_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fitatu = Fitatu(session_file(tmp_path / "fitatu-session.json"), env={})
    asked: list[tuple[str, str]] = []

    def get_day(user_id: str, day: str) -> dict[str, Any]:
        asked.append((user_id, day))
        return {"dietPlan": {}}

    monkeypatch.setattr(fitatu.client.planner, "get_day", get_day)

    assert fitatu.day("2026-11-14") == {"dietPlan": {}}
    assert asked == [("1234567", "2026-11-14")]


def test_signs_in_again_with_the_password_when_fitatu_ends_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fitatu_api import FitatuApiClient, FitatuAuthError

    path = session_file(tmp_path / "fitatu-session.json")
    logins: list[tuple[str, str, Path]] = []
    calls = {"n": 0}

    def get_day(self: Any, user_id: str, day: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FitatuAuthError("POST /token/refresh -> 401")
        return {"dietPlan": {}}

    class Session:
        def close(self) -> None:
            pass

    def login(email: str, password: str, token_store_path: Path, **kwargs: Any) -> Session:
        logins.append((email, password, token_store_path))
        return Session()

    monkeypatch.setattr("fitatu_api.planner.PlannerModule.get_day", get_day)
    monkeypatch.setattr(FitatuApiClient, "login", staticmethod(login))

    assert Fitatu(path, env={"FITATU_EMAIL": "me@example.com", "FITATU_PASSWORD": "secret"}).day("2026-11-14") == {"dietPlan": {}}
    assert logins == [("me@example.com", "secret", path)]

    calls["n"] = 0
    with pytest.raises(FitatuSignedOut, match="setup --fitatu"):
        Fitatu(path, env={}).day("2026-11-14")
