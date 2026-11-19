"""Fitatu, read through fitatu-api (https://github.com/Jezue/fitatu_library).

The library signs in, keeps the session file on the volume (it writes the refreshed tokens back itself), retries
what can be retried and maps Fitatu's answers to its exceptions. This module only adds what the agent needs on top:
the day of the diary as Fitatu sends it, the account's time zone, the daily goals (through the library's
``get_targets`` and ``set_targets`` tools), and signing in again with FITATU_EMAIL and FITATU_PASSWORD when Fitatu no
longer takes the refresh token.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from fitatu_api import FitatuApiClient, FitatuAuthError
from fitatu_api.__main__ import main as fitatu_cli
from fitatu_api.agent import FitatuTools

T = TypeVar("T")


class FitatuSignedOut(Exception):
    """No session, or Fitatu ended it: only `enduo-fitatu setup --fitatu` helps."""


class Fitatu:
    def __init__(self, session_path: Path, env: Mapping[str, str] | None = None) -> None:
        self.session_path = session_path
        self.env = os.environ if env is None else env
        self._client: FitatuApiClient | None = None

    def signed_in(self) -> bool:
        return self.session_path.is_file()

    @property
    def client(self) -> FitatuApiClient:
        if self._client is None:
            if not self.signed_in():
                raise FitatuSignedOut("Not signed in to Fitatu: run `enduo-fitatu setup`.")
            # The path is always given, so $FITATU_SESSION and $FITATU_SESSION_JSON of the library are not read.
            self._client = FitatuApiClient.from_session(self.session_path)
        return self._client

    @property
    def user_id(self) -> str:
        user_id = self.client.auth.fitatu_user_id
        if not user_id:
            raise FitatuSignedOut("The Fitatu session has no user id: run `enduo-fitatu setup --fitatu`.")
        return str(user_id)

    def account(self) -> str:
        """The Fitatu account of the session, by its user id: what the sync state and the goal are tied to."""
        return self.user_id

    @property
    def timezone(self) -> str | None:
        zone = self.client.auth.app_timezone
        return zone if isinstance(zone, str) and zone else None

    def _sign_in_again(self) -> bool:
        """With FITATU_EMAIL and FITATU_PASSWORD set, a new session replaces the one Fitatu ended."""
        email = (self.env.get("FITATU_EMAIL") or "").strip()
        password = self.env.get("FITATU_PASSWORD") or ""
        if not email or not password:
            return False
        self.close()
        FitatuApiClient.login(email, password, token_store_path=self.session_path).close()
        return True

    def _signed_in(self, call: Callable[[], T]) -> T:
        """Runs ``call``; when Fitatu ended the session, signs in again (if it can) and runs it once more."""
        try:
            return call()
        except FitatuAuthError as error:
            if not self._sign_in_again():
                raise FitatuSignedOut(f"Fitatu ended the session ({error}): run `enduo-fitatu setup --fitatu`.") from error
            return call()

    def day(self, day: str) -> Any:
        """The diary of one day (YYYY-MM-DD) as Fitatu sends it: meals with their rows, water, activities. It is not
        checked here: the mapping does that (mapping.MalformedDay)."""
        return self._signed_in(lambda: self.client.planner.get_day(self.user_id, day))

    def targets(self, day: str) -> dict[str, Any]:
        """The goals in effect on a day: ``energy`` and ``protein``/``fat``/``carbohydrate`` as ``{grams, percent}``."""
        return dict(self._signed_in(lambda: FitatuTools(self.client).get_targets(day)))

    def set_targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """fitatu-api's ``set_targets``: the goals from today on (earlier days keep theirs). Takes ``energy_kcal`` with
        ``protein_pct``, ``fat_pct`` and ``carbohydrate_pct`` (whole percent summing to 100), or ``automatic: True``;
        answers the goals now in effect with ``previous`` (the arguments that put back what it changed) and ``ok``."""
        return dict(self._signed_in(lambda: FitatuTools(self.client).set_targets(**arguments)))

    def check(self) -> str:
        """Reads the account once, to show that the session works."""
        self.client.user_settings.get_profile(self.user_id)
        return f"account {self.user_id}, {self.client.base_url}"

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def sign_in(session_path: Path, *, email: str | None) -> int:
    """`fitatu-api login` of the library, writing the session file onto the volume: it asks for the password, or
    reads FITATU_PASSWORD. Without an e-mail, `fitatu-api import-session`: it reads the session the browser snippet
    copied from the web app (README) on stdin."""
    if email:
        return fitatu_cli(["login", "--email", email, "-o", str(session_path)])
    return fitatu_cli(["import-session", "-o", str(session_path)])
