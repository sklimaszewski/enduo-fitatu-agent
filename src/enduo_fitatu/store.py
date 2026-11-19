"""Files on the state volume: the Enduo sign-in, the sync state and the goal set in Fitatu. The Fitatu session is the session file of
fitatu-api (``fitatu-session.json``), which the library writes itself. Every file here is readable by its owner
only and replaced in one step, so a crash never leaves half a file.

The sync state and the goal belong to the accounts they were built from. A file of other accounts is never applied:
it is renamed, kept, and the agent starts from an empty one (``sync_for``, ``targets_for``)."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config


@dataclass(frozen=True)
class Identity:
    """Whose data a sync state describes."""

    #: Origin of the Enduo server the diary is sent to, e.g. "https://api.enduo.app".
    enduo: str
    #: Id of the Enduo account the token belongs to (``whoami``).
    enduo_account: str
    #: User id of the Fitatu account of the session.
    fitatu_account: str

    def difference(self, other: Identity) -> str | None:
        """What makes a state of ``self`` another one's than ``other``'s; None when they are the same."""
        if self.enduo != other.enduo:
            return "Enduo server"
        if self.enduo_account != other.enduo_account:
            return "Enduo account"
        return None if self.fitatu_account == other.fitatu_account else "Fitatu account"


@dataclass
class SentDay:
    #: sha256 of the arguments of nutrition_diary_set that were sent
    hash: str
    #: meals sent; 0 after the day was cleared
    meals: int
    sent_at: str
    #: Sent for the accounts of this state. False for a day taken over from a file that named no accounts: it still
    #: tells an unchanged day, but a day is never cleared in Enduo on its word.
    bound: bool = True


@dataclass
class SkippedDay:
    """A day that was given up on: it could not be read, or Enduo refused it."""

    #: Fingerprint of what was skipped (of what Fitatu sent, or of what would have been sent): the day is tried
    #: again when it differs.
    hash: str
    #: "unreadable" or "refused"
    reason: str
    at: str


@dataclass
class SyncState:
    #: The accounts this state was built from; None only for a file of a version that did not record them.
    identity: Identity | None = None
    #: What was last sent, by day YYYY-MM-DD.
    days: dict[str, SentDay] = field(default_factory=dict)
    #: Days given up on, by day; an entry goes when the day is sent.
    skipped: dict[str, SkippedDay] = field(default_factory=dict)
    #: Oldest day of the backfill; None before the first run.
    backfill_from: str | None = None
    #: Newest day of the backfill still to read; None when the backfill is done.
    backfill_next: str | None = None
    #: Day (YYYY-MM-DD) the older days were last read again.
    last_recheck: str | None = None
    last_sync_at: str | None = None


@dataclass
class TargetsState:
    """The goal this agent set in Fitatu (TARGETS=on)."""

    #: User id of the Fitatu account the goals are of; None only for a file of a version that did not record it.
    fitatu_account: str | None = None
    #: The goal of the account before the first change, as set_targets' arguments; put back by `targets --restore`.
    original: dict[str, Any] | None = None
    #: The arguments of the last set_targets; None while the account has its own goal.
    applied: dict[str, Any] | None = None
    #: The day it was set from.
    day: str | None = None
    applied_at: str | None = None


class Store:
    def __init__(self, config: Config) -> None:
        self.dir = Path(config.state_dir)

    @property
    def fitatu_session(self) -> Path:
        """The session file of fitatu-api: tokens, user id, region and time zone of the account."""
        return self.dir / "fitatu-session.json"

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(self, name: str) -> Any:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write(self, name: str, value: Any) -> None:
        self.ensure()
        descriptor, temporary = tempfile.mkstemp(dir=self.dir, prefix=f".{name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(value, file, indent=2, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.dir / name)
        except BaseException:
            os.unlink(temporary)
            raise

    def enduo(self) -> dict[str, Any] | None:
        credentials = self.read("enduo.json")
        return credentials if isinstance(credentials, dict) and isinstance(credentials.get("access_token"), str) else None

    def save_enduo(self, credentials: dict[str, Any]) -> None:
        self.write("enduo.json", credentials)

    def set_aside(self, name: str) -> str:
        """Renames a file of the volume instead of deleting it and returns its new name. It stays readable by its
        owner only."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        kept = f"{name.removesuffix('.json')}.{stamp}.other-account.json"
        os.replace(self.dir / name, self.dir / kept)
        os.chmod(self.dir / kept, 0o600)
        return kept

    @staticmethod
    def _identity(raw: Any) -> Identity | None:
        if not isinstance(raw, dict):
            return None
        enduo, enduo_account, fitatu_account = raw.get("enduo"), raw.get("enduo_account"), raw.get("fitatu_account")
        if isinstance(enduo, str) and isinstance(enduo_account, str) and isinstance(fitatu_account, str):
            return Identity(enduo=enduo, enduo_account=enduo_account, fitatu_account=fitatu_account)
        return None

    @staticmethod
    def _sync(raw: dict[str, Any], identity: Identity | None, bound: bool) -> SyncState:
        days = {
            day: SentDay(
                hash=str(sent.get("hash", "")),
                meals=int(sent.get("meals", 0)),
                sent_at=str(sent.get("sent_at", "")),
                bound=bound and sent.get("bound") is not False,
            )
            for day, sent in (raw.get("days") or {}).items()
            if isinstance(sent, dict)
        }
        skipped = {
            day: SkippedDay(hash=str(entry.get("hash", "")), reason=str(entry.get("reason", "")), at=str(entry.get("at", "")))
            for day, entry in (raw.get("skipped") or {}).items()
            if isinstance(entry, dict)
        }
        return SyncState(
            identity=identity,
            days=days,
            skipped=skipped,
            backfill_from=raw.get("backfill_from"),
            backfill_next=raw.get("backfill_next"),
            last_recheck=raw.get("last_recheck"),
            last_sync_at=raw.get("last_sync_at"),
        )

    def sync(self) -> SyncState:
        """The state as it is on the volume, whoever it belongs to: for `status` only. A pass takes sync_for()."""
        raw = self.read("sync.json")
        if not isinstance(raw, dict):
            return SyncState()
        identity = self._identity(raw.get("identity"))
        return self._sync(raw, identity, bound=identity is not None)

    def sync_for(self, identity: Identity, log: Callable[[str], None], dry_run: bool = False) -> SyncState:
        """The state of a pass for the accounts signed in now.

        A state built from another Enduo server, Enduo account or Fitatu account is not applied: the pass starts from
        an empty one, and the old file is kept aside.

        A file of a version that recorded no accounts cannot say whose it is. It is taken over for what cannot do
        harm when it is another account's: a day it knows unchanged is not sent again, and the backfill goes on
        where it was. Its days are not ``bound``, so none of them is ever cleared in Enduo on its word.
        """
        raw = self.read("sync.json")
        if not isinstance(raw, dict):
            return SyncState(identity=identity)
        if raw.get("identity") is None:
            log(
                "The sync state names no accounts (an older version wrote it): taking it for the accounts signed in now. "
                "A day it sent and that is empty in Fitatu now is not cleared in Enduo; days sent from now on are."
            )
            state = self._sync(raw, identity, bound=False)
            if not dry_run:
                self.save_sync(state)
            return state

        owner = self._identity(raw.get("identity"))
        other = "account (its owner cannot be read)" if owner is None else owner.difference(identity)
        if other is None:
            return self._sync(raw, identity, bound=True)
        if dry_run:
            log(f"The sync state belongs to another {other}: a pass would start from an empty state and keep the old file aside.")
        else:
            log(
                f"The sync state belongs to another {other}: starting from an empty state; "
                f"the old one is kept as {self.set_aside('sync.json')}."
            )
        return SyncState(identity=identity)

    def save_sync(self, state: SyncState) -> None:
        self.write("sync.json", asdict(state))

    @staticmethod
    def _targets(raw: dict[str, Any], fitatu_account: str | None) -> TargetsState:
        original, applied = raw.get("original"), raw.get("applied")
        return TargetsState(
            fitatu_account=fitatu_account,
            original=original if isinstance(original, dict) else None,
            applied=applied if isinstance(applied, dict) else None,
            day=raw.get("day"),
            applied_at=raw.get("applied_at"),
        )

    def targets(self) -> TargetsState:
        """The goal as it is on the volume, whoever it belongs to: for `status` only."""
        raw = self.read("targets.json")
        if not isinstance(raw, dict):
            return TargetsState()
        account = raw.get("fitatu_account")
        return self._targets(raw, account if isinstance(account, str) else None)

    def targets_for(self, fitatu_account: str, log: Callable[[str], None], dry_run: bool = False) -> TargetsState:
        """The goal set in the Fitatu account signed in now.

        The goal of another Fitatu account is not applied (its ``original`` would be put back into this one): the
        agent starts from an empty one and the old file is kept aside. A file of a version that recorded no account
        is taken for the current one: it holds the only copy of the goal the account had before the agent changed
        it, and without it `targets --restore` could not put that goal back.
        """
        raw = self.read("targets.json")
        if not isinstance(raw, dict):
            return TargetsState(fitatu_account=fitatu_account)
        owner = raw.get("fitatu_account")
        if owner is None or owner == fitatu_account:
            state = self._targets(raw, fitatu_account)
            if owner is None and not dry_run:
                self.save_targets(state)
            return state
        if dry_run:
            log("The goal on the volume was set in another Fitatu account: it would be set aside and not used.")
        else:
            log(
                "The goal on the volume was set in another Fitatu account: not using it; "
                f"the old file is kept as {self.set_aside('targets.json')}."
            )
        return TargetsState(fitatu_account=fitatu_account)

    def save_targets(self, state: TargetsState) -> None:
        self.write("targets.json", asdict(state))
