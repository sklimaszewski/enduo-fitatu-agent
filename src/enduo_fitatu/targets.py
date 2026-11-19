"""The day's energy and macro targets of Enduo, set as the daily goal in Fitatu (TARGETS=on).

Fitatu keeps its goals per day, but a change applies from today on: there is no way to set the goal of a coming day.
So every pass sets today's goal when Enduo's target of today differs from what this agent set last; the days that
pass keep the goal they had, and the history in Fitatu ends up with the target of each day.

Before the first change the goal of the account is kept (fitatu-api's ``previous``). It is put back when Enduo has no
target for the day any more (the calorie window ended) and by ``enduo-fitatu targets --restore``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .mapping import num, today
from .store import Store, TargetsState
from .sync import ToolCaller

#: kcal per gram of protein, fat and carbohydrate, as Fitatu counts them.
KCAL_PER_G = {"protein": 4, "fat": 9, "carbohydrate": 4}
#: The macros of Fitatu and the target of each in Enduo's nutrition_targets_get.
MACROS = {"protein": "protein_g_target", "fat": "fat_g_target", "carbohydrate": "carbs_g_target"}


class GoalSetter(Protocol):
    def account(self) -> str:
        """The Fitatu account of the session: the goal on the volume is used only for the account it was set in."""
        ...

    def set_targets(self, arguments: dict[str, Any]) -> dict[str, Any]: ...


def split(kcal: float, grams: dict[str, float]) -> dict[str, int]:
    """Whole percent of the energy for each macro, summing to 100: Fitatu takes no fractions.

    Protein and fat keep their grams as closely as whole percent of ``kcal`` allow and carbohydrate takes the rest, so
    when Enduo's macros do not add up to its energy target, the difference lands on the carbohydrate. When protein and
    fat alone are more than the energy, the three share it in proportion to their energy.
    """
    pct = {macro: round(grams[macro] * KCAL_PER_G[macro] * 100 / kcal) for macro in ("protein", "fat")}
    if sum(pct.values()) <= 100:
        return {"protein": pct["protein"], "fat": pct["fat"], "carbohydrate": 100 - sum(pct.values())}
    energy = {macro: grams[macro] * KCAL_PER_G[macro] for macro in KCAL_PER_G}
    exact = {macro: energy[macro] * 100 / sum(energy.values()) for macro in energy}
    shares = {macro: int(value) for macro, value in exact.items()}
    for macro in sorted(exact, key=lambda m: (shares[m] - exact[m], m))[: 100 - sum(shares.values())]:
        shares[macro] += 1
    return shares


def to_goal(target: dict[str, Any] | None) -> dict[str, Any] | None:
    """The arguments of fitatu-api's set_targets for a day of nutrition_targets_get; None without an energy target.

    Fitatu works the grams out of the energy and whole percent (``split``), so protein and fat may differ from Enduo's
    by up to half a percent of the energy (3 g of protein at 2400 kcal). Without all three macro targets only the energy
    is set and Fitatu keeps its split. Without an energy target it is the energy of the three macros.
    """
    if not target:
        return None
    grams = {macro: num(target.get(field)) for macro, field in MACROS.items()}
    complete = {macro: value for macro, value in grams.items() if value is not None}
    kcal = num(target.get("kcal_target"))
    if kcal is None and len(complete) == len(MACROS):
        kcal = sum(complete[macro] * KCAL_PER_G[macro] for macro in complete)
    if kcal is None or kcal < 1:
        return None
    goal: dict[str, Any] = {"energy_kcal": round(kcal)}
    if len(complete) == len(MACROS) and sum(complete.values()) > 0:
        goal |= {f"{macro}_pct": pct for macro, pct in split(goal["energy_kcal"], complete).items()}
    return goal


def describe(goal: dict[str, Any], result: dict[str, Any] | None = None) -> str:
    """'2400 kcal, protein 28% (168 g), fat 25% (67 g), carbohydrate 47% (282 g)'; the grams as Fitatu answered them."""
    parts = [f"{goal['energy_kcal']} kcal"]
    for macro in KCAL_PER_G:
        pct = goal.get(f"{macro}_pct")
        if pct is None:
            continue
        grams = ((result or {}).get(macro) or {}).get("grams")
        parts.append(f"{macro} {pct}%" + (f" ({grams:g} g)" if isinstance(grams, int | float) else ""))
    return ", ".join(parts)


@dataclass
class TargetOptions:
    timezone: str
    log: Callable[[str], None]
    dry_run: bool = False
    now: datetime | None = None


def _previous(result: dict[str, Any]) -> dict[str, Any]:
    """The goal the account had, as set_targets' arguments; the glass size is never changed, so it is left out."""
    previous = {key: value for key, value in (result.get("previous") or {}).items() if key != "water_glass_ml"}
    return previous or {"automatic": True}


def sync_targets(store: Store, fitatu: GoalSetter, enduo: ToolCaller, options: TargetOptions) -> str:
    """Sets today's goal in Fitatu from Enduo when it changed. Returns what happened: set, unchanged, restored, none."""
    day = today(options.timezone, options.now)
    answer = enduo.call_tool("nutrition_targets_get", {"date_from": day, "date_to": day})
    days = answer.get("days") if isinstance(answer, dict) else None
    target = next((row for row in days or [] if isinstance(row, dict) and row.get("day") == day), None)
    goal = to_goal(target)
    account = fitatu.account()
    state = store.targets_for(account, options.log, dry_run=options.dry_run)

    if goal is None:
        if state.applied is None:
            return "none"
        original = state.original or {"automatic": True}
        if options.dry_run:
            options.log(f"Would put Fitatu's own goal back ({json.dumps(original)}): Enduo has no target for {day}.")
            return "restored"
        fitatu.set_targets(original)
        store.save_targets(TargetsState(fitatu_account=account))
        options.log(f"No target in Enduo for {day}: Fitatu's own goal is back.")
        return "restored"

    if goal == state.applied:
        return "unchanged"
    source = f" ({target.get('day_type')}, {target.get('target_source')})" if target and target.get("day_type") else ""
    if options.dry_run:
        options.log(f"Would set the goal in Fitatu from {day} on{source}: {describe(goal)}.")
        return "set"

    result = fitatu.set_targets(goal)
    store.save_targets(
        TargetsState(
            fitatu_account=account,
            original=state.original if state.original is not None else _previous(result),
            applied=goal,
            day=day,
            applied_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
    options.log(
        f"Set the goal in Fitatu from {day} on{source}: {describe(goal, result)}."
        + ("" if result.get("ok", True) else " Fitatu's answer does not show it; check the goal in the app.")
    )
    return "set"


def restore(store: Store, fitatu: GoalSetter, log: Callable[[str], None], dry_run: bool = False) -> bool:
    """Puts back the goal the account had before this agent first set one (`enduo-fitatu targets --restore`)."""
    account = fitatu.account()
    state = store.targets_for(account, log, dry_run=dry_run)
    if state.applied is None and state.original is None:
        log("Nothing to put back: this agent has not set a goal in Fitatu.")
        return False
    original = state.original or {"automatic": True}
    if dry_run:
        log(f"Would put Fitatu's own goal back: {json.dumps(original)}.")
        return True
    fitatu.set_targets(original)
    store.save_targets(TargetsState(fitatu_account=account))
    log(f"Fitatu's own goal is back from today on: {json.dumps(original)}.")
    return True
