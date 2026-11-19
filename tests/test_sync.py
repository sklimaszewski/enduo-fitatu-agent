from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fitatu_api import FitatuApiError

from enduo_fitatu.config import Config, load_config
from enduo_fitatu.enduo import EnduoAuthError, EnduoError, EnduoToolError
from enduo_fitatu.store import Store
from enduo_fitatu.sync import PassOptions, run_pass, run_range

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


class FakeFitatu:
    """A Fitatu diary in memory: kcal of one row per day; a day missing is a day with nothing logged."""

    def __init__(self) -> None:
        self.user = "1234567"
        self.diary: dict[str, float] = {}
        #: What Fitatu answers for a day instead of a day of the diary.
        self.broken: dict[str, Any] = {}
        self.failure: Exception | None = None
        self.reads: list[str] = []

    def account(self) -> str:
        return self.user

    def day(self, day: str) -> Any:
        self.reads.append(day)
        if self.failure is not None:
            raise self.failure
        if day in self.broken:
            return self.broken[day]
        kcal = self.diary.get(day)
        items = [] if kcal is None else [{"name": "Ryż", "energy": kcal, "weight": 100}]
        return {"dietPlan": {"lunch": {"mealName": "Obiad", "mealTime": "14:00", "items": items}}, "water": {"waterConsumption": 0}}


class FakeEnduo:
    def __init__(self) -> None:
        self.user = "enduo-account-a"
        self.calls: list[dict[str, Any]] = []
        self.refuse: set[str] = set()
        self.failure: Exception | None = None

    def account(self) -> str:
        return self.user

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        assert name == "nutrition_diary_set"
        assert arguments is not None
        if self.failure is not None:
            raise self.failure
        if arguments["day"] in self.refuse:
            raise EnduoToolError("meals[0].kcal: This value should be between 0 and 10000.")
        self.calls.append(arguments)
        return {"day": {"day": arguments["day"]}, "meals": []}

    def days(self) -> list[str]:
        return [call["day"] for call in self.calls]

    def cleared(self) -> list[str]:
        """Days whose meals were removed in this account."""
        return [call["day"] for call in self.calls if not call["meals"]]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return load_config(
        {
            "STATE_DIR": str(tmp_path),
            "ENDUO_URL": "https://api.enduo.test",
            "BACKFILL_DAYS": "10",
            "RECENT_DAYS": "3",
            "RECHECK_DAYS": "5",
            "BACKFILL_BATCH": "4",
        }
    )


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config)


@pytest.fixture
def fitatu() -> FakeFitatu:
    return FakeFitatu()


@pytest.fixture
def enduo() -> FakeEnduo:
    return FakeEnduo()


@pytest.fixture
def lines() -> list[str]:
    return []


@pytest.fixture
def options(lines: list[str]) -> PassOptions:
    return PassOptions(timezone="Europe/Warsaw", now=NOW, log=lines.append)


def test_sends_the_recent_days_with_meals_and_leaves_empty_days_alone(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    fitatu.diary = {"2026-11-14": 500, "2026-11-12": 2100}

    result = run_pass(config, store, fitatu, enduo, options)

    assert enduo.days()[:2] == ["2026-11-14", "2026-11-12"]
    assert enduo.calls[0] == {
        "source": "fitatu",
        "day": "2026-11-14",
        "meals": [
            {"name": "Obiad", "time_of_day": "14:00", "kcal": 500, "items": [{"name": "Ryż", "amount": 100, "unit": "g", "kcal": 500}]}
        ],
    }
    assert "2026-11-13" not in store.sync().days
    assert result.sent == 2


def test_sends_a_day_again_only_when_it_changed(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    fitatu.diary = {"2026-11-14": 500}
    run_pass(config, store, fitatu, enduo, options)
    enduo.calls.clear()

    result = run_pass(config, store, fitatu, enduo, options)
    assert enduo.days() == []
    assert result.unchanged == 1

    fitatu.diary["2026-11-14"] = 900
    result = run_pass(config, store, fitatu, enduo, options)
    assert enduo.days() == ["2026-11-14"]
    assert result.sent == 1


def test_clears_a_day_in_enduo_when_everything_it_sent_was_deleted_in_fitatu(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    fitatu.diary = {"2026-11-13": 700}
    run_pass(config, store, fitatu, enduo, options)
    enduo.calls.clear()

    del fitatu.diary["2026-11-13"]
    run_pass(config, store, fitatu, enduo, options)

    assert enduo.calls == [{"source": "fitatu", "day": "2026-11-13", "meals": []}]
    assert store.sync().days["2026-11-13"].meals == 0


def test_fills_the_history_a_batch_per_pass_newest_first(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, lines: list[str]
) -> None:
    for logged in ("2026-11-11", "2026-11-08", "2026-11-04", "2026-11-03"):
        fitatu.diary[logged] = 1800

    run_pass(config, store, fitatu, enduo, options)
    assert fitatu.reads == ["2026-11-14", "2026-11-13", "2026-11-12", "2026-11-11", "2026-11-10", "2026-11-09", "2026-11-08"]
    assert store.sync().backfill_next == "2026-11-07"

    fitatu.reads.clear()
    run_pass(config, store, fitatu, enduo, options)
    assert fitatu.reads == ["2026-11-14", "2026-11-13", "2026-11-12", "2026-11-07", "2026-11-06", "2026-11-05", "2026-11-04"]

    run_pass(config, store, fitatu, enduo, options)
    state = store.sync()
    assert (state.backfill_from, state.backfill_next) == ("2026-11-04", None)
    assert enduo.days() == ["2026-11-11", "2026-11-08", "2026-11-04"]
    assert any(line.startswith("Backfill done") for line in lines)


def test_reads_the_older_days_again_once_a_day(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    config = replace(config, backfill_days=0)

    run_pass(config, store, fitatu, enduo, options)
    assert fitatu.reads == ["2026-11-14", "2026-11-13", "2026-11-12", "2026-11-11", "2026-11-10"]

    fitatu.reads.clear()
    run_pass(config, store, fitatu, enduo, options)
    assert fitatu.reads == ["2026-11-14", "2026-11-13", "2026-11-12"]

    fitatu.reads.clear()
    run_pass(config, store, fitatu, enduo, replace(options, now=datetime(2026, 11, 15, 12, tzinfo=UTC)))
    assert fitatu.reads == ["2026-11-15", "2026-11-14", "2026-11-13", "2026-11-12", "2026-11-11"]


def test_moves_on_when_enduo_refuses_a_day_and_tries_it_again_when_it_changes(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, lines: list[str]
) -> None:
    fitatu.diary = {"2026-11-14": 500, "2026-11-13": 600}
    enduo.refuse.add("2026-11-14")

    result = run_pass(config, store, fitatu, enduo, options)
    assert (result.sent, result.skipped, result.newly_skipped) == (1, 1, 1)
    assert "1 skipped (unreadable or refused by Enduo)" in result.summary()
    assert enduo.days() == ["2026-11-13"]
    assert [line for line in lines if line.startswith("Skipped")] == [
        "Skipped 2026-11-14: Enduo refused it (meals[0].kcal: This value should be between 0 and 10000.). "
        "It is tried again when the day changes in Fitatu."
    ]
    assert store.sync().skipped["2026-11-14"].reason == "refused"
    assert "2026-11-14" not in store.sync().days, "a day Enduo refused is not a day this agent filled"

    # The same day is not sent, and not said, again on every pass.
    enduo.refuse.clear()
    enduo.calls.clear()
    lines.clear()
    result = run_pass(config, store, fitatu, enduo, options)
    assert enduo.days() == []
    assert (result.skipped, result.newly_skipped) == (1, 0)
    assert not any("2026-11-14" in line for line in lines)

    fitatu.diary["2026-11-14"] = 550
    run_pass(config, store, fitatu, enduo, options)
    assert enduo.days() == ["2026-11-14"]
    assert store.sync().skipped == {}


def test_a_day_enduo_refused_is_not_cleared_when_it_is_emptied_in_fitatu(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    fitatu.diary = {"2026-11-14": 500}
    enduo.refuse.add("2026-11-14")
    run_pass(config, store, fitatu, enduo, options)

    enduo.refuse.clear()
    del fitatu.diary["2026-11-14"]
    result = run_pass(config, store, fitatu, enduo, options)

    assert (result.sent, result.skipped) == (0, 0)

    assert enduo.calls == [], "what is in Enduo for that day is not this agent's: it stays"


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {},
        {"error": "PRIVATE"},
        {"dietPlan": None},
        {"dietPlan": {"lunch": {"mealName": "PRIVATE", "items": {"0": {"name": "PRIVATE"}}}}},
        {"dietPlan": {"lunch": {"items": [{"name": "Ry\u017c", "energy": 100}, "PRIVATE"]}}},
    ],
)
def test_skips_a_day_it_cannot_read_sends_the_next_one_and_clears_nothing(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, lines: list[str], answer: Any
) -> None:
    fitatu.diary = {"2026-11-14": 500, "2026-11-13": 600, "2026-11-12": 700}
    run_pass(config, store, fitatu, enduo, options)
    enduo.calls.clear()
    lines.clear()

    # Fitatu answers something that is no day for a day that is in Enduo, and the day before it changed.
    fitatu.broken["2026-11-14"] = answer
    fitatu.diary["2026-11-13"] = 650
    result = run_pass(config, store, fitatu, enduo, options)

    assert enduo.days() == ["2026-11-13"], "the next day is sent"
    assert enduo.cleared() == [], "an answer that cannot be read does not say the day was emptied"
    assert (result.sent, result.skipped, result.newly_skipped) == (1, 1, 1)
    assert store.sync().skipped["2026-11-14"].reason == "unreadable"
    assert store.sync().days["2026-11-14"].meals == 1, "what was sent before stays as it was"
    said = [line for line in lines if line.startswith("Skipped 2026-11-14: what Fitatu sent cannot be read")]
    assert len(said) == 1
    assert not any("PRIVATE" in line for line in lines)

    # Not said again while Fitatu answers the same; read again and sent once the day can be read.
    lines.clear()
    result = run_pass(config, store, fitatu, enduo, options)
    assert (result.skipped, result.newly_skipped) == (1, 0)
    assert not any("2026-11-14" in line for line in lines)

    del fitatu.broken["2026-11-14"]
    fitatu.diary["2026-11-14"] = 520
    enduo.calls.clear()
    run_pass(config, store, fitatu, enduo, options)
    assert enduo.days() == ["2026-11-14"]
    assert store.sync().skipped == {}


def test_the_backfill_goes_on_past_a_day_it_cannot_read(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    fitatu.diary = {"2026-11-10": 1800}
    fitatu.broken["2026-11-11"] = {"dietPlan": "soon"}

    result = run_pass(config, store, fitatu, enduo, options)

    assert enduo.days() == ["2026-11-10"]
    assert result.skipped == 1
    assert store.sync().backfill_next == "2026-11-07"


@pytest.mark.parametrize(
    "failure",
    [
        EnduoError("Enduo MCP answered 503: "),
        EnduoError("Enduo MCP answered 429: "),
        EnduoAuthError("Enduo refused the token (401)"),
    ],
)
def test_a_failure_of_enduo_that_is_not_about_a_day_stops_the_pass_and_gives_up_on_nothing(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, failure: Exception
) -> None:
    fitatu.diary = {"2026-11-14": 500, "2026-11-13": 600}
    enduo.failure = failure

    with pytest.raises(type(failure)):
        run_pass(config, store, fitatu, enduo, options)
    assert fitatu.reads == ["2026-11-14"], "the pass stops at the first day"
    assert (store.sync().days, store.sync().skipped) == ({}, {})

    enduo.failure = None
    assert run_pass(config, store, fitatu, enduo, options).sent == 2


def test_a_failure_of_fitatu_stops_the_pass_and_the_backfill_stays_where_it_was(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    run_pass(config, store, fitatu, enduo, options)
    before = store.sync().backfill_next

    fitatu.failure = FitatuApiError("GET /diet-and-activity-plan -> 502", status_code=502)
    with pytest.raises(FitatuApiError):
        run_pass(config, store, fitatu, enduo, options)

    assert store.sync().backfill_next == before
    assert store.sync().skipped == {}


def other_enduo_account(config: Config, fitatu: FakeFitatu, enduo: FakeEnduo) -> Config:
    enduo.user = "enduo-account-b"
    return config


def other_fitatu_account(config: Config, fitatu: FakeFitatu, enduo: FakeEnduo) -> Config:
    fitatu.user = "7654321"
    return config


def other_enduo_server(config: Config, fitatu: FakeFitatu, enduo: FakeEnduo) -> Config:
    return replace(config, mcp_url="https://other.enduo.test/mcp")


@pytest.mark.parametrize(
    ("switch", "what"),
    [(other_enduo_account, "Enduo account"), (other_fitatu_account, "Fitatu account"), (other_enduo_server, "Enduo server")],
)
def test_a_state_built_for_other_accounts_is_not_applied_and_clears_nothing(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, lines: list[str], switch: Any, what: str
) -> None:
    fitatu.diary = {"2026-11-14": 500, "2026-11-13": 700}
    run_pass(config, store, fitatu, enduo, options)
    before = json.loads((store.dir / "sync.json").read_text())
    assert before["identity"] == {"enduo": "https://api.enduo.test", "enduo_account": "enduo-account-a", "fitatu_account": "1234567"}

    # The container is signed in to other accounts; there the 13th has nothing in Fitatu.
    config = switch(config, fitatu, enduo)
    del fitatu.diary["2026-11-13"]
    enduo.calls.clear()
    lines.clear()
    result = run_pass(config, store, fitatu, enduo, options)

    assert enduo.cleared() == [], "the old state remembers the 13th as sent: that is no reason to clear it here"
    assert enduo.days() == ["2026-11-14"], "and what the old state calls unchanged is sent, as on a first run"
    assert result.empty >= 1
    assert sum(f"belongs to another {what}" in line for line in lines) == 1
    assert any(line.startswith("First run") for line in lines)

    kept = [path for path in store.dir.iterdir() if path.name.endswith(".other-account.json")]
    assert len(kept) == 1
    assert json.loads(kept[0].read_text()) == before
    assert kept[0].stat().st_mode & 0o777 == 0o600
    assert json.loads((store.dir / "sync.json").read_text())["identity"] != before["identity"]

    # The new state is this pair's own: a day it filled is cleared when it is emptied, and no file is set aside again.
    del fitatu.diary["2026-11-14"]
    enduo.calls.clear()
    run_pass(config, store, fitatu, enduo, options)
    assert enduo.cleared() == ["2026-11-14"]
    assert len([path for path in store.dir.iterdir() if path.name.endswith(".other-account.json")]) == 1


def test_a_state_that_names_no_accounts_spares_unchanged_days_but_clears_nothing(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, lines: list[str]
) -> None:
    fitatu.diary = {"2026-11-14": 500, "2026-11-13": 700, "2026-11-12": 900}
    run_pass(config, store, fitatu, enduo, options)
    # The file as a version before the accounts were recorded wrote it.
    legacy = json.loads((store.dir / "sync.json").read_text())
    del legacy["identity"], legacy["skipped"]
    for sent in legacy["days"].values():
        del sent["bound"]
    (store.dir / "sync.json").write_text(json.dumps(legacy))

    del fitatu.diary["2026-11-13"]
    fitatu.diary["2026-11-12"] = 950
    enduo.calls.clear()
    lines.clear()
    run_pass(config, store, fitatu, enduo, options)
    run_pass(config, store, fitatu, enduo, options)

    assert enduo.days() == ["2026-11-12"], "an unchanged day is not sent again, a changed one is"
    assert enduo.cleared() == [], "whose state it was is not known: the 13th is not cleared"
    assert sum("names no accounts" in line for line in lines) == 1
    state = store.sync()
    assert state.identity is not None and state.identity.enduo_account == "enduo-account-a"
    assert (state.days["2026-11-14"].bound, state.days["2026-11-13"].bound, state.days["2026-11-12"].bound) == (False, False, True)
    assert not [path for path in store.dir.iterdir() if path.name.endswith(".other-account.json")]

    # The 12th was sent for these accounts: emptied in Fitatu, it is cleared. Not even --force clears the 13th.
    del fitatu.diary["2026-11-12"]
    enduo.calls.clear()
    run_pass(config, store, fitatu, enduo, replace(options, force=True))
    assert enduo.cleared() == ["2026-11-12"]


def test_changes_nothing_in_a_dry_run(config: Config, store: Store, fitatu: FakeFitatu, options: PassOptions, lines: list[str]) -> None:
    fitatu.diary = {"2026-11-14": 500}

    assert run_pass(config, store, fitatu, None, replace(options, dry_run=True)).sent == 1
    assert any(line.startswith("Would send 2026-11-14") for line in lines)
    assert store.read("sync.json") is None


def test_a_dry_run_leaves_a_state_of_other_accounts_where_it_is(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions, lines: list[str]
) -> None:
    fitatu.diary = {"2026-11-14": 500}
    run_pass(config, store, fitatu, enduo, options)
    before = (store.dir / "sync.json").read_text()

    enduo.user = "enduo-account-b"
    enduo.calls.clear()
    assert run_pass(config, store, fitatu, enduo, replace(options, dry_run=True)).sent == 1

    assert enduo.calls == []
    assert (store.dir / "sync.json").read_text() == before
    assert [path.name for path in store.dir.iterdir()] == ["sync.json"]
    assert any("would start from an empty state" in line for line in lines)


def test_sends_the_days_of_a_range_and_with_force_also_the_unchanged_ones(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo
) -> None:
    fitatu.diary = {"2026-01-02": 2000, "2026-01-04": 2200}
    options = PassOptions(timezone="UTC", log=lambda line: None)

    run_range(config, store, fitatu, enduo, "2026-01-01", "2026-01-04", options)
    assert enduo.days() == ["2026-01-04", "2026-01-02"]

    enduo.calls.clear()
    run_range(config, store, fitatu, enduo, "2026-01-01", "2026-01-04", options)
    assert enduo.days() == []

    run_range(config, store, fitatu, enduo, "2026-01-01", "2026-01-04", replace(options, force=True))
    assert enduo.days() == ["2026-01-04", "2026-01-02"]


def test_state_files_are_readable_by_their_owner_only(
    config: Config, store: Store, fitatu: FakeFitatu, enduo: FakeEnduo, options: PassOptions
) -> None:
    fitatu.diary = {"2026-11-14": 500}
    run_pass(config, store, fitatu, enduo, options)

    assert (store.dir / "sync.json").stat().st_mode & 0o777 == 0o600
