"""A Fitatu day → the arguments of Enduo's MCP tool `nutrition_diary_set`, and the arithmetic of days.

Pure functions only: everything they need comes in as arguments. The day is what fitatu-api's
``planner.get_day()`` returns, Fitatu's own answer; its fields are described in docs/fitatu-api.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import DiaryItems
from .text import line

# Limits of nutrition_diary_set (FoodDiaryInput, FoodDiaryMealInput, FoodDiaryItemInput in enduo-api).
MAX_MEALS = 10
MAX_ITEMS = 100
MAX_KCAL = 10_000
MAX_PROTEIN_G = 500
MAX_CARBS_G = 1500
MAX_FAT_G = 500
MAX_FIBER_G = 300
MAX_SUGAR_G = 1500
MAX_SODIUM_MG = 50_000
MAX_AMOUNT = 100_000
MAX_FLUID_L = 20

# Text of Fitatu is cut to what a name plausibly needs, inside what nutrition_diary_set takes (name of a food 300,
# brand 200, name of a meal 200, unit 20 characters): the coach, a language model, reads it later.
MAX_ITEM_NAME = 120
MAX_BRAND = 80
MAX_MEAL_NAME = 60
MAX_UNIT = 20

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Row = Mapping[str, Any]


class MalformedDay(ValueError):
    """What Fitatu sent for a day is not a day of the diary as docs/fitatu-api.md describes it. Such a day is never
    sent, in part or as an empty one: an answer that cannot be read does not say that the day was emptied. The
    message names the part that is wrong, never what is in it."""


def num(value: Any) -> float | None:
    """A number Fitatu sent, as a number or a string ("12.50"); None when there is none."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 and parsed == parsed and parsed != float("inf") else None


def _round1(value: float) -> float:
    rounded = round(value + 0.0, 1)
    return int(rounded) if rounded.is_integer() else rounded


def _present(row: Row) -> bool:
    """A row that is in the diary: not hidden and not deleted."""
    return row.get("visible") is not False and not row.get("deletedAt")


def _meals(day: Any) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """The meals of a day with their keys, in the order of the day; MalformedDay when it is not a day."""
    if not isinstance(day, Mapping):
        raise MalformedDay("it is not an object")
    plan = day.get("dietPlan")
    if not isinstance(plan, Mapping):
        raise MalformedDay("it has no diet plan")
    meals = []
    for key, meal in plan.items():
        if not isinstance(key, str) or not isinstance(meal, Mapping):
            raise MalformedDay("one of its meals is not an object")
        meals.append((key, meal))
    return meals


def _rows(meal: Mapping[str, Any]) -> list[Row]:
    """The foods of a meal. A meal always has the list, empty when nothing is logged: a meal without one is not
    read as an empty meal."""
    items = meal.get("items")
    if not isinstance(items, list) or not all(isinstance(row, Mapping) for row in items):
        raise MalformedDay("the foods of one of its meals are not a list of objects")
    return list(items)


def eaten_filter(day: Mapping[str, Any], mode: DiaryItems) -> Callable[[Row], bool]:
    """Whether a row counts as eaten. Fitatu lets the athlete tick rows as eaten, but many log after eating and
    never tick: "auto" takes the ticked rows of a day when it has any, and every row when it has none."""
    if mode == "all":
        return lambda row: True
    if mode == "eaten":
        return lambda row: row.get("eaten") is True
    any_ticked = any(_present(row) and row.get("eaten") is True for _, meal in _meals(day) for row in _rows(meal))
    return (lambda row: row.get("eaten") is True) if any_ticked else (lambda row: True)


def _item(row: Row) -> dict[str, Any]:
    out: dict[str, Any] = {"name": line(row.get("name"), MAX_ITEM_NAME) or "Fitatu item"}
    brand = line(row.get("brand"), MAX_BRAND)
    if brand:
        out["brand"] = brand

    capacity, weight, quantity = num(row.get("capacity")), num(row.get("weight")), num(row.get("measureQuantity"))
    if row.get("liquid") is True and capacity:
        out["amount"], out["unit"] = min(_round1(capacity), MAX_AMOUNT), "ml"
    elif weight:
        out["amount"], out["unit"] = min(_round1(weight), MAX_AMOUNT), "g"
    elif quantity:
        out["amount"] = min(_round1(quantity), MAX_AMOUNT)
        unit = line(row.get("measureName"), MAX_UNIT)
        if unit:
            out["unit"] = unit

    kcal = num(row.get("energy"))
    if kcal is not None:
        out["kcal"] = min(round(kcal), MAX_KCAL)
    for key, field, limit in (
        ("protein_g", "protein", MAX_PROTEIN_G),
        ("carbs_g", "carbohydrate", MAX_CARBS_G),
        ("fat_g", "fat", MAX_FAT_G),
        ("sugar_g", "sugars", MAX_SUGAR_G),
        ("fiber_g", "fiber", MAX_FIBER_G),
    ):
        grams = num(row.get(field))
        if grams is not None:
            out[key] = min(_round1(grams), limit)
    return out


def _total(rows: list[Row], pick: Callable[[Row], float | None]) -> float | None:
    """Sum of a nutrient over the rows; None when no row has it (unknown, not zero)."""
    values = [value for value in map(pick, rows) if value is not None]
    return sum(values) if values else None


def _sodium(row: Row) -> float | None:
    """Sodium of a row in mg: Fitatu's sodium, else its salt (40 % sodium)."""
    sodium = num(row.get("sodium"))
    if sodium is not None:
        return sodium
    salt = num(row.get("salt"))
    return None if salt is None else salt * 400


def _meal(key: str, meal: Mapping[str, Any], rows: list[Row]) -> dict[str, Any]:
    out: dict[str, Any] = {"name": line(meal.get("mealName"), MAX_MEAL_NAME) or line(key.replace("_", " "), MAX_MEAL_NAME) or "meal"}
    time = str(meal.get("mealTime") or "").strip()[:5]
    if _TIME.match(time):
        out["time_of_day"] = time

    kcal = _total(rows, lambda row: num(row.get("energy")))
    if kcal is not None:
        out["kcal"] = min(round(kcal), MAX_KCAL)
    for key_out, pick, limit in (
        ("protein_g", lambda row: num(row.get("protein")), MAX_PROTEIN_G),
        ("carbs_g", lambda row: num(row.get("carbohydrate")), MAX_CARBS_G),
        ("fat_g", lambda row: num(row.get("fat")), MAX_FAT_G),
        ("fiber_g", lambda row: num(row.get("fiber")), MAX_FIBER_G),
        ("sugar_g", lambda row: num(row.get("sugars")), MAX_SUGAR_G),
        ("saturated_fat_g", lambda row: num(row.get("saturatedFat")), MAX_FAT_G),
        ("sodium_mg", _sodium, MAX_SODIUM_MG),
    ):
        value = _total(rows, pick)
        if value is not None:
            out[key_out] = min(_round1(value), limit)
    out["items"] = [_item(row) for row in rows[:MAX_ITEMS]]
    return out


def to_diary(day: str, fitatu: Any, mode: DiaryItems) -> dict[str, Any]:
    """The call for one day. Meals keep Fitatu's order; a meal without an eaten row is left out. A day without
    any meal gives an empty list, which clears the day in Enduo: the caller sends that only for a day it sent before.
    Raises MalformedDay when what Fitatu sent is not a day; odd values inside a food are left out instead."""
    eaten = eaten_filter(fitatu, mode)
    meals = []
    for key, meal in _meals(fitatu):
        rows = [row for row in _rows(meal) if _present(row) and eaten(row)]
        if rows:
            meals.append(_meal(key, meal, rows))

    diary: dict[str, Any] = {"source": "fitatu", "day": day, "meals": meals[:MAX_MEALS]}
    water = fitatu.get("water")
    millilitres = num(water.get("waterConsumption")) if isinstance(water, Mapping) else None
    if millilitres:
        diary["fluid_l"] = min(round(millilitres) / 1000, MAX_FLUID_L)
    return diary


def diary_hash(diary: Mapping[str, Any]) -> str:
    """Fingerprint of what would be sent: a day is sent again only when it changes."""
    return hashlib.sha256(json.dumps(diary, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def raw_hash(fitatu: Any) -> str:
    """Fingerprint of what Fitatu sent for a day, whatever it is: a day that could not be read is looked at again
    only when Fitatu sends something else for it."""
    try:
        text = json.dumps(fitatu, sort_keys=True, default=repr)
    except (TypeError, ValueError, RecursionError):
        text = repr(fitatu)
    return hashlib.sha256(text.encode("utf-8", "backslashreplace")).hexdigest()


def day_kcal(diary: Mapping[str, Any]) -> int:
    return int(sum(meal.get("kcal", 0) for meal in diary["meals"]))


def today(timezone: str, now: datetime | None = None) -> str:
    """Today (YYYY-MM-DD) in a time zone."""
    zone = ZoneInfo(timezone)
    return (now.astimezone(zone) if now is not None else datetime.now(zone)).date().isoformat()


def add_days(day: str, days: int) -> str:
    """The day `days` after `day` (negative: before)."""
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def days_back(newest: str, oldest: str) -> list[str]:
    """Days from `oldest` to `newest`, both included, newest first."""
    first, last = date.fromisoformat(oldest), date.fromisoformat(newest)
    return [(last - timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def is_day(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
