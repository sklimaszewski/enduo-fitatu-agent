from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fitatu_api.agent import FitatuTools

from enduo_fitatu.config import load_config
from enduo_fitatu.fitatu import Fitatu
from enduo_fitatu.store import Store
from enduo_fitatu.targets import TargetOptions, restore, split, sync_targets, to_goal

NOW = datetime(2026, 11, 15, 6, tzinfo=UTC)


def target(day: str = "2026-11-15", **fields: Any) -> dict[str, Any]:
    """A day of nutrition_targets_get."""
    return {
        "day": day,
        "day_type": "quality",
        "kcal_target": 2400,
        "protein_g_target": 170,
        "carbs_g_target": 250,
        "fat_g_target": 70,
        "target_source": "day_type",
        **fields,
    }


class FakeEnduo:
    def __init__(self) -> None:
        self.days: list[dict[str, Any]] = []
        self.asked: list[dict[str, Any]] = []

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        assert name == "nutrition_targets_get"
        assert arguments is not None
        self.asked.append(arguments)
        return {"days": self.days}


class FakeFitatu:
    """set_targets as fitatu-api answers it: the goals now in effect and the arguments that put back the old ones."""

    def __init__(self) -> None:
        self.user = "1234567"
        self.goal: dict[str, Any] = {"automatic": True}
        self.calls: list[dict[str, Any]] = []

    def account(self) -> str:
        return self.user

    def set_targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(arguments)
        previous, self.goal = self.goal, dict(arguments)
        kcal = arguments.get("energy_kcal", 2000)
        grams = {
            macro: round(kcal * arguments.get(f"{macro}_pct", 0) / 100 / per_g)
            for macro, per_g in (("protein", 4), ("fat", 9), ("carbohydrate", 4))
        }
        return {"energy": kcal, **{macro: {"grams": g} for macro, g in grams.items()}, "previous": previous, "ok": True}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(load_config({"STATE_DIR": str(tmp_path)}))


@pytest.fixture
def lines() -> list[str]:
    return []


@pytest.fixture
def options(lines: list[str]) -> TargetOptions:
    return TargetOptions(timezone="Europe/Warsaw", log=lines.append, now=NOW)


def test_protein_and_fat_keep_their_grams_and_carbohydrate_takes_the_rest_of_the_energy() -> None:
    assert split(2400, {"protein": 170, "fat": 70, "carbohydrate": 250}) == {"protein": 28, "fat": 26, "carbohydrate": 46}
    assert split(2310, {"protein": 170, "fat": 70, "carbohydrate": 250}) == {"protein": 29, "fat": 27, "carbohydrate": 44}


def test_when_protein_and_fat_alone_pass_the_energy_the_macros_share_it() -> None:
    shares = split(1800, {"protein": 300, "fat": 100, "carbohydrate": 100})

    assert shares == {"protein": 48, "fat": 36, "carbohydrate": 16}
    assert sum(shares.values()) == 100


def test_the_goal_of_a_day() -> None:
    assert to_goal(target()) == {"energy_kcal": 2400, "protein_pct": 28, "fat_pct": 26, "carbohydrate_pct": 46}
    assert to_goal(target(kcal_target=None)) == {"energy_kcal": 2310, "protein_pct": 29, "fat_pct": 27, "carbohydrate_pct": 44}
    assert to_goal(target(fat_g_target=None)) == {"energy_kcal": 2400}, "without every macro Fitatu keeps its split"
    assert to_goal(target(kcal_target="2450.4", fat_g_target=None)) == {"energy_kcal": 2450}
    assert to_goal(target(kcal_target=None, fat_g_target=None)) is None
    assert to_goal(None) is None


def test_sets_the_goal_of_today_once_and_again_only_when_enduo_changes_it(store: Store, options: TargetOptions, lines: list[str]) -> None:
    enduo, fitatu = FakeEnduo(), FakeFitatu()
    enduo.days = [target()]

    assert sync_targets(store, fitatu, enduo, options) == "set"
    assert enduo.asked == [{"date_from": "2026-11-15", "date_to": "2026-11-15"}]
    assert fitatu.calls == [{"energy_kcal": 2400, "protein_pct": 28, "fat_pct": 26, "carbohydrate_pct": 46}]
    assert lines[-1] == (
        "Set the goal in Fitatu from 2026-11-15 on (quality, day_type): "
        "2400 kcal, protein 28% (168 g), fat 26% (69 g), carbohydrate 46% (276 g)."
    )
    state = store.targets()
    assert (state.original, state.day) == ({"automatic": True}, "2026-11-15")

    assert sync_targets(store, fitatu, enduo, options) == "unchanged"
    assert len(fitatu.calls) == 1, "an unchanged target costs nothing in Fitatu"

    enduo.days = [target(kcal_target=3000, carbs_g_target=420)]
    assert sync_targets(store, fitatu, enduo, options) == "set"
    assert fitatu.calls[-1]["energy_kcal"] == 3000
    assert store.targets().original == {"automatic": True}, "the account's own goal is kept from before the first change"


def test_puts_the_accounts_own_goal_back_when_enduo_has_no_target_any_more(store: Store, options: TargetOptions, lines: list[str]) -> None:
    enduo, fitatu = FakeEnduo(), FakeFitatu()
    fitatu.goal = {"energy_kcal": 2200, "protein_pct": 25, "fat_pct": 30, "carbohydrate_pct": 45}
    enduo.days = [target()]
    sync_targets(store, fitatu, enduo, options)

    enduo.days = []
    assert sync_targets(store, fitatu, enduo, options) == "restored"
    assert fitatu.calls[-1] == {"energy_kcal": 2200, "protein_pct": 25, "fat_pct": 30, "carbohydrate_pct": 45}
    assert store.targets().applied is None
    assert lines[-1] == "No target in Enduo for 2026-11-15: Fitatu's own goal is back."

    assert sync_targets(store, fitatu, enduo, options) == "none"
    assert len(fitatu.calls) == 2


def test_leaves_fitatu_alone_without_a_target_it_never_set(store: Store, options: TargetOptions) -> None:
    enduo, fitatu = FakeEnduo(), FakeFitatu()
    enduo.days = [target(day="2026-11-16")]

    assert sync_targets(store, fitatu, enduo, options) == "none", "only the target of today counts"
    assert fitatu.calls == []


def test_the_day_is_the_athletes(store: Store, lines: list[str]) -> None:
    enduo, fitatu = FakeEnduo(), FakeFitatu()
    enduo.days = [target(day="2026-11-16")]

    late = TargetOptions(timezone="Europe/Warsaw", log=lines.append, now=datetime(2026, 11, 15, 23, 30, tzinfo=UTC))
    assert sync_targets(store, fitatu, enduo, late) == "set"
    assert enduo.asked == [{"date_from": "2026-11-16", "date_to": "2026-11-16"}]


def test_changes_nothing_in_a_dry_run(store: Store, options: TargetOptions, lines: list[str]) -> None:
    enduo, fitatu = FakeEnduo(), FakeFitatu()
    enduo.days = [target()]

    assert sync_targets(store, fitatu, enduo, TargetOptions(timezone="Europe/Warsaw", log=lines.append, dry_run=True, now=NOW)) == "set"
    assert fitatu.calls == []
    assert store.read("targets.json") is None
    assert lines[-1].startswith("Would set the goal in Fitatu from 2026-11-15 on")


def test_restore_puts_back_the_goal_from_before(store: Store, options: TargetOptions, lines: list[str]) -> None:
    enduo, fitatu = FakeEnduo(), FakeFitatu()
    assert restore(store, fitatu, lines.append) is False

    enduo.days = [target()]
    sync_targets(store, fitatu, enduo, options)
    assert restore(store, fitatu, lines.append) is True
    assert fitatu.calls[-1] == {"automatic": True}
    assert store.targets().applied is None


def test_the_goal_of_another_fitatu_account_is_neither_used_nor_put_back(store: Store, options: TargetOptions, lines: list[str]) -> None:
    enduo, first = FakeEnduo(), FakeFitatu()
    first.goal = {"energy_kcal": 2200, "protein_pct": 25, "fat_pct": 30, "carbohydrate_pct": 45}
    enduo.days = [target()]
    sync_targets(store, first, enduo, options)
    assert store.targets().fitatu_account == "1234567"

    # The container is signed in to another Fitatu account, which has its own goal.
    second = FakeFitatu()
    second.user = "7654321"
    second.goal = {"energy_kcal": 1800, "protein_pct": 30, "fat_pct": 30, "carbohydrate_pct": 40}
    lines.clear()

    assert sync_targets(store, second, enduo, options) == "set", "the goal set in the first account does not count as set here"
    assert store.targets().original == second.calls[0] | {"energy_kcal": 1800, "protein_pct": 30, "fat_pct": 30, "carbohydrate_pct": 40}
    kept = [path for path in store.dir.iterdir() if path.name.endswith(".other-account.json")]
    assert len(kept) == 1 and kept[0].stat().st_mode & 0o777 == 0o600
    assert sum("another Fitatu account" in text for text in lines) == 1

    assert restore(store, second, lines.append) is True
    assert second.calls[-1] == {"energy_kcal": 1800, "protein_pct": 30, "fat_pct": 30, "carbohydrate_pct": 40}, (
        "its own goal, not the first account's"
    )


def test_a_goal_file_that_names_no_account_is_taken_for_the_current_one(store: Store, options: TargetOptions) -> None:
    original = {"energy_kcal": 2200, "protein_pct": 25, "fat_pct": 30, "carbohydrate_pct": 45}
    store.write("targets.json", {"original": original, "applied": {"energy_kcal": 2400}, "day": "2026-11-14", "applied_at": "x"})
    fitatu = FakeFitatu()

    assert restore(store, fitatu, lambda text: None) is True
    assert fitatu.calls == [original], "the only copy of the goal the account had is not thrown away"
    assert store.targets().fitatu_account == "1234567"


def test_targets_are_off_unless_switched_on() -> None:
    assert load_config({}).targets is False
    assert load_config({"TARGETS": " On "}).targets is True
    assert load_config({"TARGETS": "maybe"}).targets is False


def test_sets_the_goal_through_fitatu_api_and_puts_it_back() -> None:
    """Against fitatu-api's own set_targets (its in-memory demo account): the split it takes and the previous goal."""
    fitatu = Fitatu(Path("unused.json"), env={})
    fitatu._client = FitatuTools.demo().client
    goal = to_goal(target())
    assert goal is not None

    result = fitatu.set_targets(goal)
    assert (result["energy"], result["protein"]["grams"], result["fat"]["grams"], result["carbohydrate"]["grams"]) == (2400, 168, 69, 276)
    assert (result["ok"], result["previous"]) == (True, {"automatic": True})
    assert fitatu.targets("2026-11-15")["energy"] == 2400

    assert fitatu.set_targets(result["previous"])["automatic"] is True
