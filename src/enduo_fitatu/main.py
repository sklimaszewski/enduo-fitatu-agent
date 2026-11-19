"""Enduo Fitatu Agent (unofficial): sends the food diary of Fitatu to Enduo.

enduo-fitatu run                     the service: one pass every POLL_SECONDS (default command)
enduo-fitatu setup                   sign in to Fitatu and Enduo; skips what is already done
enduo-fitatu setup --fitatu          sign in to Fitatu again
enduo-fitatu setup --enduo           sign in to Enduo again
enduo-fitatu once [--dry-run]        one pass now; --dry-run prints what would be sent
enduo-fitatu backfill --days N       send the last N days now (or --from YYYY-MM-DD [--to YYYY-MM-DD])
enduo-fitatu targets [--dry-run]     set today's targets of Enduo as the goal in Fitatu now (TARGETS=on does it every pass)
enduo-fitatu targets --restore       put back the goal the Fitatu account had before
enduo-fitatu status                  both sign-ins and the sync state
enduo-fitatu health                  exit 0 while the service loop is alive (Docker healthcheck)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from types import FrameType

from fitatu_api import FitatuApiError

from .config import Config, load_config, valid_zone
from .enduo import Enduo, EnduoAuthError, EnduoError, EnduoToolError, device_login, enduo_token
from .fitatu import Fitatu, FitatuSignedOut, sign_in
from .mapping import add_days, is_day, today
from .store import Store
from .sync import PassOptions, run_pass, run_range
from .targets import TargetOptions, describe, restore, sync_targets
from .text import line as one_line

#: Pause between two days read from Fitatu, in seconds: its rate limit is not known.
PAUSE = 1.0

stopping = threading.Event()


def log(line: str) -> None:
    print(f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z {line}", flush=True)


class SetupError(Exception):
    """Setup could not finish; the message says what to do."""


#: Time zones of the Fitatu account that were not known and already said so: one warning each, not one per pass.
_unknown_zones: set[str] = set()


def timezone_of(config: Config, fitatu: Fitatu, enduo: Enduo | None = None) -> str:
    """The time zone of the athlete's days: TIMEZONE, else the one of the Fitatu account. When the account has none,
    or one this system does not know, the time zone of the Enduo account (when signed in), else TZ, else UTC: an
    unknown zone is said once and never stops a pass."""
    if config.timezone:
        return config.timezone
    try:
        named = fitatu.timezone
    except (FitatuApiError, FitatuSignedOut):
        named = None
    zone = valid_zone(named)
    if zone is not None:
        return zone

    fallback, source = "UTC", "UTC"
    try:
        user = enduo.whoami().get("user") if enduo is not None else None
    except (EnduoAuthError, EnduoError, EnduoToolError):
        user = None  # the pass says what is wrong with Enduo
    of_enduo = valid_zone(user.get("timezone")) if isinstance(user, dict) else None
    if of_enduo is not None:
        fallback, source = of_enduo, "the time zone of your Enduo account"
    elif (of_system := valid_zone(os.environ.get("TZ"))) is not None:
        fallback, source = of_system, "TZ"
    if named and named not in _unknown_zones:
        _unknown_zones.add(named)
        log(
            f'The time zone of the Fitatu account ("{one_line(named, 60) or "?"}") is not one this system knows: '
            f"using {fallback} ({source}) for the days. Set TIMEZONE to choose another."
        )
    return fallback


def setup_fitatu(config: Config, store: Store, force: bool) -> None:
    fitatu = Fitatu(store.fitatu_session)
    if fitatu.signed_in() and not force:
        try:
            log(f"Fitatu: signed in ({fitatu.check()}, days in {timezone_of(config, fitatu)}).")
            return
        except (FitatuApiError, FitatuSignedOut) as error:
            log(f"Fitatu: the stored session does not work ({error}); signing in again.")
        finally:
            fitatu.close()

    store.ensure()
    email = (os.environ.get("FITATU_EMAIL") or "").strip()
    if not email or not os.environ.get("FITATU_PASSWORD"):
        print(
            "\n".join(
                [
                    "",
                    "Sign in to Fitatu (through fitatu-api). The password is used once and not stored: the container keeps",
                    "only the session. An account made with Apple, Google or Facebook has no password: leave the e-mail empty",
                    'and paste the session of the web app instead (README, "Accounts without a password").',
                    "",
                ]
            )
        )
        email = email or input("Fitatu e-mail: ").strip()
    code = sign_in(store.fitatu_session, email=email or None)
    if code != 0:
        raise SetupError("Fitatu sign-in failed; run `enduo-fitatu setup --fitatu` again.")

    fitatu = Fitatu(store.fitatu_session)
    try:
        log(f"Fitatu: signed in ({fitatu.check()}, days in {timezone_of(config, fitatu)}).")
    finally:
        fitatu.close()


def whoami(config: Config, token: str) -> str:
    me = Enduo(config, token).call_tool("whoami")
    me = me if isinstance(me, dict) else {}
    email = (me.get("user") or {}).get("email") or "signed in"
    return f"Enduo ({config.enduo_url}): {email}, scopes: {' '.join(me.get('scopes') or [])}."


def setup_enduo(config: Config, store: Store, force: bool) -> None:
    token = enduo_token(store)
    if token is not None and not force:
        try:
            log(whoami(config, token))
            return
        except (EnduoAuthError, EnduoError) as error:
            if os.environ.get("ENDUO_TOKEN"):
                raise SetupError(f"ENDUO_TOKEN does not work: {error}") from error
            log(f"Enduo: the stored token does not work ({error}); signing in again.")

    result = device_login(config, store)
    if result != "signed_in":
        raise SetupError(
            "The sign-in was denied in Enduo." if result == "denied" else "The code expired before it was approved; run setup again."
        )
    log(f"Enduo: signed in ({config.enduo_url}).")


def targets_pass(store: Store, fitatu: Fitatu, enduo: Enduo, options: TargetOptions) -> None:
    """The goal of the day after the diary: a failure is logged and leaves the diary alone."""
    try:
        sync_targets(store, fitatu, enduo, options)
    except (EnduoToolError, FitatuApiError) as error:
        options.log(f"Goal in Fitatu not set, will retry: {error}")


def heartbeat(store: Store) -> None:
    try:
        store.ensure()
        (store.dir / "heartbeat").write_text(datetime.now(UTC).isoformat())
    except OSError:
        pass  # health reports it


def serve(config: Config, store: Store) -> None:
    log(f"Enduo Fitatu Agent {config.version}: Enduo {config.enduo_url}, a pass every {config.poll_seconds} s.")
    waiting_said = False
    while not stopping.is_set():
        heartbeat(store)
        token = enduo_token(store)
        fitatu = Fitatu(store.fitatu_session)
        if not fitatu.signed_in() or token is None:
            if not waiting_said:
                log("Waiting for setup: run `enduo-fitatu setup` in this container.")
                waiting_said = True
            stopping.wait(15)
            continue
        waiting_said = False

        try:
            enduo = Enduo(config, token)
            zone = timezone_of(config, fitatu, enduo)
            result = run_pass(config, store, fitatu, enduo, PassOptions(timezone=zone, log=log, pause=PAUSE))
            if result.sent + result.newly_skipped > 0:
                log(f"Pass done: {result.summary()}.")
            if config.targets:
                targets_pass(store, fitatu, enduo, TargetOptions(timezone=zone, log=log))
        except (FitatuSignedOut, EnduoAuthError) as error:
            log(f"Signed out: {error}")
        except (FitatuApiError, EnduoError, OSError, ValueError) as error:
            log(f"Pass failed, will retry: {error}")
        finally:
            fitatu.close()
        stopping.wait(config.poll_seconds)
    log("Stopped.")


def status(config: Config, store: Store) -> None:
    fitatu = Fitatu(store.fitatu_session)
    if not fitatu.signed_in():
        print("Fitatu: not signed in.")
    else:
        try:
            print(f"Fitatu: signed in ({fitatu.check()}), days in {timezone_of(config, fitatu)}.")
        except (FitatuApiError, FitatuSignedOut) as error:
            print(f"Fitatu: {error}")
        finally:
            fitatu.close()

    token = enduo_token(store)
    if token is None:
        print("Enduo: not signed in.")
    else:
        try:
            print(whoami(config, token))
        except (EnduoAuthError, EnduoError) as error:
            print(f"Enduo: {error}")

    state = store.sync()
    if state.backfill_from is None:
        print("Sync: not run yet.")
        return
    filled = sorted(day for day, sent in state.days.items() if sent.meals > 0)
    newest = f" (newest {filled[-1]})" if filled else ""
    print(f"Sync: last pass {state.last_sync_at or 'never'}, {len(filled)} days with meals in Enduo{newest}.")
    if state.skipped:
        print(
            f"Skipped (unreadable or refused by Enduo; tried again when they change in Fitatu): "
            f"{', '.join(sorted(day for day in state.skipped if isinstance(day, str))[-10:])}."
        )
    print(f"Backfill from {state.backfill_from}: " + ("done." if state.backfill_next is None else f"next {state.backfill_next}."))
    goal = store.targets()
    switch = "on" if config.targets else "off"
    if goal.applied is not None:
        print(f"Goal in Fitatu (TARGETS={switch}): {describe(goal.applied)}, set from {goal.day} on.")
    else:
        print(f"Goal in Fitatu (TARGETS={switch}): the account's own.")


def health(config: Config, store: Store) -> int:
    try:
        age = time.time() - (store.dir / "heartbeat").stat().st_mtime
    except OSError:
        return 1
    return 0 if age < config.poll_seconds + 600 else 1


def backfill_range(days: int | None, oldest: str | None, newest: str | None, now: str) -> tuple[str, str]:
    """The range of the backfill command: --days N, or --from YYYY-MM-DD [--to YYYY-MM-DD]; never after today."""
    last = newest or now
    if not is_day(last):
        raise SetupError(f'--to takes a day as YYYY-MM-DD, not "{last}".')
    last = min(last, now)
    if days is not None:
        if not 1 <= days <= 3650:
            raise SetupError("--days takes a number of days from 1 to 3650.")
        return add_days(last, -(days - 1)), last
    if oldest is None or not is_day(oldest):
        raise SetupError("Give the days to send: --days N, or --from YYYY-MM-DD [--to YYYY-MM-DD].")
    if oldest > last:
        raise SetupError(f"--from {oldest} is after {last}.")
    return oldest, last


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="enduo-fitatu", description="Enduo Fitatu Agent: sends the Fitatu food diary to Enduo.")
    commands = root.add_subparsers(dest="command")
    commands.add_parser("run", help="the service: one pass every POLL_SECONDS")
    setup = commands.add_parser("setup", help="sign in to Fitatu and Enduo")
    only = setup.add_mutually_exclusive_group()
    only.add_argument("--fitatu", action="store_true", help="sign in to Fitatu again")
    only.add_argument("--enduo", action="store_true", help="sign in to Enduo again")
    for name, text in (("once", "one pass now"), ("backfill", "send a range of days now")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--dry-run", action="store_true", help="print what would be sent and change nothing")
        command.add_argument("--force", action="store_true", help="send days again even when they did not change")
        if name == "backfill":
            command.add_argument("--days", type=int, help="the last N days, today included")
            command.add_argument("--from", dest="oldest", metavar="YYYY-MM-DD", help="first day")
            command.add_argument("--to", dest="newest", metavar="YYYY-MM-DD", help="last day (default: today)")
    targets = commands.add_parser("targets", help="set today's targets of Enduo as the goal in Fitatu now")
    targets.add_argument("--dry-run", action="store_true", help="print the goal that would be set and change nothing")
    targets.add_argument("--restore", action="store_true", help="put back the goal the account had before this agent set one")
    commands.add_parser("status", help="both sign-ins and the sync state")
    commands.add_parser("health", help="exit 0 while the service runs (Docker healthcheck)")
    return root


def sync_now(config: Config, store: Store, args: argparse.Namespace) -> int:
    fitatu = Fitatu(store.fitatu_session)
    if not fitatu.signed_in():
        raise SetupError("Not signed in to Fitatu: run `enduo-fitatu setup`.")
    token = enduo_token(store)
    if not args.dry_run and token is None:
        raise SetupError("Not signed in to Enduo: run `enduo-fitatu setup`.")
    # A dry run asks Enduo too, but only whose token it is: the state is used only for the accounts it was built from.
    enduo = None if token is None else Enduo(config, token)
    try:
        options = PassOptions(timezone=timezone_of(config, fitatu, enduo), log=log, dry_run=args.dry_run, force=args.force, pause=PAUSE)
        if args.command == "once":
            result = run_pass(config, store, fitatu, enduo, options)
            if config.targets and enduo is not None and not args.dry_run:
                targets_pass(store, fitatu, enduo, TargetOptions(timezone=options.timezone, log=log))
        else:
            oldest, newest = backfill_range(args.days, args.oldest, args.newest, today(options.timezone))
            log(f"{'Dry run of' if args.dry_run else 'Sending'} the diary from {oldest} to {newest}.")
            result = run_range(config, store, fitatu, enduo, oldest, newest, options)
    finally:
        fitatu.close()
    log(f"{'Dry run' if args.dry_run else 'Done'}: {result.summary(args.dry_run)}.")
    return 0


def targets_now(config: Config, store: Store, args: argparse.Namespace) -> int:
    fitatu = Fitatu(store.fitatu_session)
    if not fitatu.signed_in():
        raise SetupError("Not signed in to Fitatu: run `enduo-fitatu setup`.")
    try:
        if args.restore:
            restore(store, fitatu, log, dry_run=args.dry_run)
            return 0
        token = enduo_token(store)
        if token is None:
            raise SetupError("Not signed in to Enduo: run `enduo-fitatu setup`.")
        enduo = Enduo(config, token)
        options = TargetOptions(timezone=timezone_of(config, fitatu, enduo), log=log, dry_run=args.dry_run)
        outcome = sync_targets(store, fitatu, enduo, options)
        if outcome == "unchanged":
            log("The goal in Fitatu is already Enduo's target of today.")
        elif outcome == "none":
            log("Enduo has no energy target for today: the goal in Fitatu is left as it is.")
        if not config.targets and outcome == "set" and not args.dry_run:
            log("TARGETS is off: the service does not follow later changes. Set TARGETS=on to have it do this every pass.")
    finally:
        fitatu.close()
    return 0


def _stop(signum: int, frame: FrameType | None) -> None:
    stopping.set()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("FITATU_LOG_LEVEL", "WARNING").upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _stop)
    args = parser().parse_args(argv)
    config = load_config()
    store = Store(config)
    try:
        match args.command:
            case None | "run":
                serve(config, store)
                return 0
            case "setup":
                if not args.enduo:
                    setup_fitatu(config, store, args.fitatu)
                if not args.fitatu:
                    setup_enduo(config, store, args.enduo)
                missing = [
                    name for name, ok in (("Fitatu", store.fitatu_session.is_file()), ("Enduo", enduo_token(store) is not None)) if not ok
                ]
                print(
                    f"\nStill missing: {' and '.join(missing)}. Run `enduo-fitatu setup` again."
                    if missing
                    else "\nReady. The service picks it up within 15 seconds; `enduo-fitatu status` shows the state."
                )
                return 0
            case "once" | "backfill":
                return sync_now(config, store, args)
            case "targets":
                return targets_now(config, store, args)
            case "status":
                status(config, store)
                return 0
            case "health":
                return health(config, store)
    except (SetupError, FitatuSignedOut, FitatuApiError, EnduoAuthError, EnduoError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
