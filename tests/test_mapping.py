from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from enduo_fitatu.mapping import (
    MAX_BRAND,
    MAX_ITEM_NAME,
    MAX_MEAL_NAME,
    MAX_UNIT,
    MalformedDay,
    add_days,
    days_back,
    diary_hash,
    eaten_filter,
    is_day,
    num,
    raw_hash,
    to_diary,
    today,
)


def row(**overrides: Any) -> dict[str, Any]:
    """A row as Fitatu sends it (recorded by fitatu_library, docs/fitatu-api.md)."""
    return {
        "planDayDietItemId": "row-1",
        "source": "API",
        "name": "Białko jaja kurzego",
        "foodType": "PRODUCT",
        "measureId": 39,
        "measureName": "portion",
        "measureQuantity": 1,
        "energy": 16.799999999999997,
        "protein": 3.815,
        "fat": 0.06999999999999999,
        "carbohydrate": 0.24499999999999997,
        "weight": 35,
        "capacity": None,
        "fiber": None,
        "saturatedFat": 0,
        "sugars": None,
        "salt": 0.175,
        "sodium": 70,
        "liquid": False,
        "eaten": False,
        "visible": True,
        "brand": None,
        **overrides,
    }


def day(water: float = 0, **meals: list[dict[str, Any]]) -> dict[str, Any]:
    slots = [
        ("breakfast", "Śniadanie", "08:00"),
        ("second_breakfast", "II śniadanie", "11:00"),
        ("lunch", "Obiad", "14:00"),
        ("snack", "Przekąska", "17:00"),
        ("supper", "Kolacja", "21:00"),
    ]
    return {
        "dietPlan": {key: {"mealName": name, "mealTime": time, "items": meals.get(key, [])} for key, name, time in slots},
        "water": {"waterConsumption": water},
    }


def names(diary: dict[str, Any]) -> list[str]:
    return [item["name"] for meal in diary["meals"] for item in meal["items"]]


def test_maps_the_meals_of_a_day_with_their_totals_and_foods_in_the_order_of_the_day() -> None:
    diary = to_diary(
        "2026-11-14",
        day(
            water=1750,
            supper=[
                row(
                    name=" Twaróg  chudy ",
                    brand="Piątnica",
                    energy=99,
                    protein=18,
                    fat=0.5,
                    carbohydrate=4,
                    sugars=4,
                    fiber=0,
                    weight=100,
                    sodium=None,
                    salt=0.1,
                )
            ],
            breakfast=[
                row(),
                row(
                    name="Mleko 2%",
                    liquid=True,
                    capacity=250,
                    weight=257,
                    energy=127.5,
                    protein=8.5,
                    fat=5,
                    carbohydrate=12,
                    sugars=12,
                    sodium=110,
                    saturatedFat=3.2,
                ),
            ],
        ),
        "all",
    )

    assert diary == {
        "source": "fitatu",
        "day": "2026-11-14",
        "fluid_l": 1.75,
        "meals": [
            {
                "name": "Śniadanie",
                "time_of_day": "08:00",
                "kcal": 144,
                "protein_g": 12.3,
                "carbs_g": 12.2,
                "fat_g": 5.1,
                "sugar_g": 12,
                "saturated_fat_g": 3.2,
                "sodium_mg": 180,
                "items": [
                    {"name": "Białko jaja kurzego", "amount": 35, "unit": "g", "kcal": 17, "protein_g": 3.8, "carbs_g": 0.2, "fat_g": 0.1},
                    {
                        "name": "Mleko 2%",
                        "amount": 250,
                        "unit": "ml",
                        "kcal": 128,
                        "protein_g": 8.5,
                        "carbs_g": 12,
                        "fat_g": 5,
                        "sugar_g": 12,
                    },
                ],
            },
            {
                "name": "Kolacja",
                "time_of_day": "21:00",
                "kcal": 99,
                "protein_g": 18,
                "carbs_g": 4,
                "fat_g": 0.5,
                "fiber_g": 0,
                "sugar_g": 4,
                "saturated_fat_g": 0,
                "sodium_mg": 40,
                "items": [
                    {
                        "name": "Twaróg chudy",
                        "brand": "Piątnica",
                        "amount": 100,
                        "unit": "g",
                        "kcal": 99,
                        "protein_g": 18,
                        "carbs_g": 4,
                        "fat_g": 0.5,
                        "sugar_g": 4,
                        "fiber_g": 0,
                    }
                ],
            },
        ],
    }


def test_reads_numbers_fitatu_sends_as_strings() -> None:
    diary = to_diary("2026-11-14", day(lunch=[row(energy="250.4", protein="10", weight="120.25", fat=None, carbohydrate=None)]), "all")

    assert diary["meals"][0]["items"] == [{"name": "Białko jaja kurzego", "amount": 120.2, "unit": "g", "kcal": 250, "protein_g": 10}]
    assert "fat_g" not in diary["meals"][0]


def test_uses_the_measure_for_a_row_without_a_weight() -> None:
    diary = to_diary(
        "2026-11-14", day(snack=[row(foodType="CUSTOM_ITEM", name="Batonik", weight=None, measureQuantity=2, measureName="sztuka")]), "all"
    )

    assert diary["meals"][0]["items"][0] == {
        "name": "Batonik",
        "amount": 2,
        "unit": "sztuka",
        "kcal": 17,
        "protein_g": 3.8,
        "carbs_g": 0.2,
        "fat_g": 0.1,
    }


def test_leaves_out_hidden_and_deleted_rows_and_the_meals_left_without_rows() -> None:
    diary = to_diary(
        "2026-11-14", day(breakfast=[row(visible=False)], lunch=[row(deletedAt="2026-11-14 10:00:00"), row(name="Ryż")]), "all"
    )

    assert [(meal["name"], [item["name"] for item in meal["items"]]) for meal in diary["meals"]] == [("Obiad", ["Ryż"])]


def test_gives_an_empty_list_for_a_day_with_nothing_logged_and_no_fluid_without_water() -> None:
    assert to_diary("2026-11-14", day(), "all") == {"source": "fitatu", "day": "2026-11-14", "meals": []}
    assert to_diary("2026-11-14", {"dietPlan": {}, "water": None}, "all")["meals"] == []


@pytest.mark.parametrize(
    ("answer", "problem"),
    [
        (None, "not an object"),
        (["SECRET"], "not an object"),
        ({}, "no diet plan"),
        ({"dietPlan": None, "water": None}, "no diet plan"),
        ({"dietPlan": []}, "no diet plan"),
        ({"error": "SECRET"}, "no diet plan"),
        ({"dietPlan": {"lunch": None}}, "meals is not an object"),
        ({"dietPlan": {"lunch": "SECRET"}}, "meals is not an object"),
        ({"dietPlan": {"lunch": {"mealName": "Obiad"}}}, "not a list of objects"),
        ({"dietPlan": {"lunch": {"items": {"0": {"name": "SECRET"}}}}}, "not a list of objects"),
        ({"dietPlan": {"breakfast": {"items": []}, "lunch": {"items": [{"name": "Ryż"}, "SECRET"]}}}, "not a list of objects"),
    ],
)
def test_an_answer_that_is_no_day_is_never_read_as_an_empty_day(answer: Any, problem: str) -> None:
    for mode in ("auto", "eaten", "all"):
        with pytest.raises(MalformedDay, match=problem) as raised:
            to_diary("2026-11-14", answer, mode)
        assert "SECRET" not in str(raised.value)


def test_odd_values_inside_a_food_are_left_out_and_never_make_a_day_unreadable() -> None:
    odd = row(
        name={"pl": "Ryż"},
        brand=7,
        energy=[1],
        protein="dużo",
        fat=10**400,
        carbohydrate=float("nan"),
        weight="1e999",
        measureQuantity={},
        measureName=3,
        eaten="yes",
        fiber="  ",
        sodium=None,
        salt=None,
    )
    answer = {"dietPlan": {"lunch": {"mealName": None, "mealTime": 1400, "items": [odd]}}, "water": ["much"], "note": 5}

    diary = to_diary("2026-11-14", answer, "all")

    assert diary == {
        "source": "fitatu",
        "day": "2026-11-14",
        "meals": [{"name": "lunch", "saturated_fat_g": 0, "items": [{"name": "Fitatu item"}]}],
    }
    assert len(diary_hash(diary)) == 64


def test_names_are_cleaned_of_control_and_invisible_characters_before_they_are_sent() -> None:
    answer = {
        "dietPlan": {
            "lu\x00nch\n": {
                "mealName": " Obiad\t \x1b[2J\u200b ",
                "items": [
                    row(
                        name="Ry\u017c\r\n bia\u0142y\x00\u202e\ud83d",
                        brand="\x07Bio\u00a0 Farm\ufeff",
                        weight=None,
                        measureName="por\ntion\x00",
                    )
                ],
            }
        }
    }

    diary = to_diary("2026-11-14", answer, "all")

    assert diary["meals"][0]["name"] == "Obiad [2J"
    assert diary["meals"][0]["items"][0]["name"] == "Ry\u017c bia\u0142y"
    assert diary["meals"][0]["items"][0]["brand"] == "Bio Farm"
    assert diary["meals"][0]["items"][0]["unit"] == "por tion"
    assert len(diary_hash(diary)) == 64, "half a surrogate pair in a name would not even encode"


def test_names_are_cut_to_what_a_name_needs_inside_the_limits_of_enduo() -> None:
    long = "lorem ipsum " * 10_000
    answer = {"dietPlan": {long: {"mealName": long, "items": [row(name=long, brand=long, weight=None, measureName=long)]}}}

    meal = to_diary("2026-11-14", answer, "all")["meals"][0]
    item = meal["items"][0]

    for text, limit in ((meal["name"], MAX_MEAL_NAME), (item["name"], MAX_ITEM_NAME), (item["brand"], MAX_BRAND), (item["unit"], MAX_UNIT)):
        assert limit - 12 < len(text) <= limit
        assert text.startswith("lorem") and text.endswith("…")
    assert MAX_MEAL_NAME <= 200 and MAX_ITEM_NAME <= 300 and MAX_BRAND <= 200 and MAX_UNIT <= 20


def test_the_fingerprint_of_an_answer_that_cannot_be_read_changes_with_the_answer() -> None:
    assert raw_hash({"error": "a"}) == raw_hash({"error": "a"})
    assert raw_hash({"error": "a"}) != raw_hash({"error": "b"})
    assert len(raw_hash(None)) == len(raw_hash({1: object(), "x": {2, 3}})) == len(raw_hash("\ud83d")) == 64


def test_drops_a_meal_time_that_is_no_hh_mm_and_names_a_meal_without_a_name_by_its_key() -> None:
    diary = to_diary("2026-11-14", {"dietPlan": {"second_breakfast": {"mealName": " ", "mealTime": "8 rano", "items": [row()]}}}, "all")

    assert diary["meals"][0]["name"] == "second breakfast"
    assert "time_of_day" not in diary["meals"][0]


def test_keeps_every_value_within_the_limits_of_nutrition_diary_set() -> None:
    diary = to_diary("2026-11-14", day(water=30_000, lunch=[row(energy=12_000, protein=800, weight=200_000)]), "all")

    assert diary["meals"][0]["kcal"] == 10_000
    assert diary["meals"][0]["protein_g"] == 500
    assert diary["meals"][0]["items"][0]["amount"] == 100_000
    assert diary["fluid_l"] == 20


def test_auto_takes_the_ticked_rows_when_the_day_has_any_else_every_row() -> None:
    ticked = day(breakfast=[row(eaten=True, name="A")], supper=[row(eaten=False, name="B")])
    unticked = day(breakfast=[row(name="A")], supper=[row(name="B")])

    assert names(to_diary("2026-11-14", ticked, "auto")) == ["A"]
    assert names(to_diary("2026-11-14", unticked, "auto")) == ["A", "B"]
    assert names(to_diary("2026-11-14", unticked, "eaten")) == []
    assert names(to_diary("2026-11-14", ticked, "all")) == ["A", "B"]


def test_a_hidden_ticked_row_does_not_count() -> None:
    assert eaten_filter(day(breakfast=[row(eaten=True, visible=False)], lunch=[row()]), "auto")(row()) is True


def test_the_fingerprint_changes_with_what_would_be_sent_and_only_then() -> None:
    first = to_diary("2026-11-14", day(lunch=[row()]), "all")

    assert diary_hash(first) == diary_hash(to_diary("2026-11-14", day(lunch=[row(updatedAt="later")]), "all"))
    assert diary_hash(first) != diary_hash(to_diary("2026-11-14", day(lunch=[row(measureQuantity=2, weight=70)]), "all"))


def test_today_is_the_day_of_the_athletes_time_zone() -> None:
    late_evening = datetime(2026, 11, 14, 23, 30, tzinfo=UTC)

    assert today("UTC", late_evening) == "2026-11-14"
    assert today("Europe/Warsaw", late_evening) == "2026-11-15"
    assert today("America/New_York", late_evening) == "2026-11-14"


def test_days_across_months_and_the_change_of_time() -> None:
    assert add_days("2026-03-01", -1) == "2026-02-28"
    assert add_days("2026-10-25", 1) == "2026-10-26"
    assert days_back("2026-11-02", "2026-10-30") == ["2026-11-02", "2026-11-01", "2026-10-31", "2026-10-30"]
    assert days_back("2026-11-02", "2026-11-03") == []
    assert is_day("2026-02-28")
    assert not is_day("2026-02-30")
    assert not is_day("14.11.2026")


def test_numbers() -> None:
    assert num("12.5") == 12.5
    assert num(" ") is None
    assert num(-1) is None
    assert num(None) is None
    assert num(True) is None
    assert num(10**400) is None
    assert num("1e999") is None
