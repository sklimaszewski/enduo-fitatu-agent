"""One pass: read the recent days of the Fitatu diary (and, once a day, the older ones; on the first run a batch
of the backfill) and send every day that changed to Enduo. A day is sent whole: nutrition_diary_set replaces the
meals of the day, so a meal deleted in Fitatu disappears in Enduo too.

A day that cannot be read, or that Enduo refuses as invalid, is given up on and the pass goes on: it is remembered
with its fingerprint and tried again when it changes in Fitatu. A failure that is not about one day (the network, a
refused token, 429, 5xx) ends the pass, which is then repeated.

The state belongs to the accounts it was built from (store.sync_for): a day is cleared in Enduo only when this
agent sent it there, for the Enduo account and the Fitatu account signed in now.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .config import Config, server_origin
from .enduo import EnduoToolError
from .mapping import MalformedDay, add_days, day_kcal, days_back, diary_hash, raw_hash, to_diary, today
from .store import Identity, SentDay, SkippedDay, Store, SyncState
from .text import line

Outcome = Literal["sent", "unchanged", "empty", "skipped"]


class DiarySource(Protocol):
    def account(self) -> str:
        """The Fitatu account of the session, to tie the sync state to it."""
        ...

    def day(self, day: str) -> Any: ...


class ToolCaller(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


class EnduoTools(ToolCaller, Protocol):
    def account(self) -> str:
        """The Enduo account of the token, to tie the sync state to it."""
        ...


@dataclass
class PassResult:
    sent: int = 0
    unchanged: int = 0
    #: days with nothing logged that were never sent: left alone, so meals typed into Enduo stay
    empty: int = 0
    #: days given up on: they could not be read, or Enduo refused them as invalid
    skipped: int = 0
    #: of the skipped days, those given up on in this pass (the others were said before)
    newly_skipped: int = 0

    def summary(self, dry_run: bool = False) -> str:
        return (
            f"{self.sent} {'to send' if dry_run else 'sent'}, {self.unchanged} unchanged, {self.empty} empty, "
            f"{self.skipped} skipped (unreadable or refused by Enduo)"
        )


@dataclass
class PassOptions:
    #: Time zone of the athlete's days.
    timezone: str
    log: Callable[[str], None]
    #: Print what would be sent instead of sending it; the state is not changed.
    dry_run: bool = False
    #: Send every day again, even when it did not change since it was sent.
    force: bool = False
    #: Pause between two days read from Fitatu, in seconds.
    pause: float = 0.0
    now: datetime | None = None


def state_for(config: Config, store: Store, fitatu: DiarySource, enduo: EnduoTools | None, options: PassOptions) -> SyncState:
    """The sync state of the accounts signed in now; a state of other accounts is not used (store.sync_for)."""
    if enduo is None:
        # A dry run without an Enduo sign-in: whose state the stored one is cannot be told, so it is not used.
        options.log("Not signed in to Enduo: showing what a first run would send.")
        return SyncState()
    identity = Identity(enduo=server_origin(config.mcp_url), enduo_account=enduo.account(), fitatu_account=fitatu.account())
    return store.sync_for(identity, options.log, dry_run=options.dry_run)


class DaySync:
    def __init__(self, config: Config, store: Store, state: SyncState, fitatu: DiarySource, enduo: ToolCaller | None, options: PassOptions):
        self.config = config
        self.store = store
        self.state = state
        self.fitatu = fitatu
        self.enduo = enduo
        self.options = options
        self.dry_run = options.dry_run or enduo is None
        self.result = PassResult()
        self.reads = 0

    def save(self) -> None:
        if not self.dry_run:
            self.store.save_sync(self.state)

    def skip(self, day: str, fingerprint: str, reason: str, why: str) -> Outcome:
        """Gives up on a day as it is now: it is said once and not tried again until the day changes in Fitatu.
        What was sent for the day before stays as it is, in the state and in Enduo."""
        known = self.state.skipped.get(day)
        if known is None or known.hash != fingerprint or self.options.force:
            self.options.log(f"Skipped {day}: {why}. It is tried again when the day changes in Fitatu.")
            self.result.newly_skipped += 1
            self.state.skipped[day] = SkippedDay(hash=fingerprint, reason=reason, at=datetime.now(UTC).isoformat(timespec="seconds"))
            self.save()
        return "skipped"

    def day(self, day: str) -> Outcome:
        outcome = self._day(day)
        setattr(self.result, outcome, getattr(self.result, outcome) + 1)
        return outcome

    def _day(self, day: str) -> Outcome:
        if self.reads > 0 and self.options.pause > 0:
            time.sleep(self.options.pause)
        self.reads += 1
        answer = self.fitatu.day(day)
        try:
            diary = to_diary(day, answer, self.config.diary_items)
            fingerprint = diary_hash(diary)
        except MalformedDay as error:
            return self.skip(day, raw_hash(answer), "unreadable", f"what Fitatu sent cannot be read ({error})")
        except (ArithmeticError, AttributeError, LookupError, TypeError, ValueError) as error:
            # The mapping only computes: whatever it trips over is in this day, not in the connection.
            return self.skip(day, raw_hash(answer), "unreadable", f"what Fitatu sent cannot be mapped ({type(error).__name__})")

        previous = self.state.days.get(day)
        given_up = self.state.skipped.get(day)
        log = self.options.log

        if previous is not None and previous.hash == fingerprint and not self.options.force:
            # What is in Enduo is what Fitatu has (again): nothing is left to give up on.
            self.state.skipped.pop(day, None)
            return "unchanged"
        if given_up is not None and given_up.hash == fingerprint and not self.options.force:
            return "skipped"
        if not diary["meals"] and (previous is None or not previous.bound):
            # An empty list would clear the day in Enduo, meals typed in there included: only a day this agent
            # filled before, for the accounts signed in now, is cleared.
            return "empty"
        if self.dry_run or self.enduo is None:
            log(f"Would send {day}:\n{json.dumps(diary, indent=2, ensure_ascii=False)}")
            return "sent"

        try:
            self.enduo.call_tool("nutrition_diary_set", diary)
        except EnduoToolError as error:
            # Enduo refused this day as invalid: say why and move on, or one bad day would block every later one.
            return self.skip(day, fingerprint, "refused", f"Enduo refused it ({line(str(error), 300) or 'no reason given'})")
        meals = len(diary["meals"])
        log(f"Sent {day}: cleared (nothing left in Fitatu)." if meals == 0 else f"Sent {day}: {meals} meals, {day_kcal(diary)} kcal.")
        self.state.days[day] = SentDay(hash=fingerprint, meals=meals, sent_at=datetime.now(UTC).isoformat(timespec="seconds"))
        self.state.skipped.pop(day, None)
        self.save()
        return "sent"


def run_pass(config: Config, store: Store, fitatu: DiarySource, enduo: EnduoTools | None, options: PassOptions) -> PassResult:
    """The pass of the service: recent days, the older ones once a day, and a batch of the backfill."""
    state = state_for(config, store, fitatu, enduo, options)
    sync = DaySync(config, store, state, fitatu, enduo, options)
    now = today(options.timezone, options.now)

    if state.backfill_from is None:
        state.backfill_from = add_days(now, -config.backfill_days)
        state.backfill_next = add_days(now, -config.recent_days) if config.backfill_days >= config.recent_days else None
        options.log(
            f"First run: sending the diary from {state.backfill_from} on (BACKFILL_DAYS={config.backfill_days}), "
            f"{config.backfill_batch} older days per pass."
            if config.backfill_days > 0
            else "First run: sending only the recent days (BACKFILL_DAYS=0)."
        )

    for day in days_back(now, add_days(now, -(config.recent_days - 1))):
        sync.day(day)

    if config.recheck_days > config.recent_days and state.last_recheck != now:
        for day in days_back(add_days(now, -config.recent_days), add_days(now, -(config.recheck_days - 1))):
            if state.backfill_next is None or day > state.backfill_next:
                sync.day(day)
        state.last_recheck = now

    if state.backfill_next is not None and state.backfill_from is not None:
        for day in days_back(state.backfill_next, state.backfill_from)[: config.backfill_batch]:
            sync.day(day)
            following = add_days(day, -1)
            state.backfill_next = None if following < state.backfill_from else following
            sync.save()
        if state.backfill_next is None:
            options.log(f"Backfill done: the diary from {state.backfill_from} on is in Enduo.")

    if not options.dry_run:
        state.last_sync_at = datetime.now(UTC).isoformat(timespec="seconds")
    sync.save()
    return sync.result


def run_range(
    config: Config, store: Store, fitatu: DiarySource, enduo: EnduoTools | None, oldest: str, newest: str, options: PassOptions
) -> PassResult:
    """The days from `oldest` to `newest` (both included, newest first), now: the `backfill` command."""
    sync = DaySync(config, store, state_for(config, store, fitatu, enduo, options), fitatu, enduo, options)
    for day in days_back(newest, oldest):
        sync.day(day)
    sync.save()
    return sync.result
