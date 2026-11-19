# Enduo Fitatu Agent

An **unofficial** agent for [Enduo](https://enduo.app) that connects [Fitatu](https://www.fitatu.com) with Enduo:
the meals you log in Fitatu, with their energy, macros and foods, land in your food diary in Enduo through Enduo's
MCP tool `nutrition_diary_set`, where the coach reads them against your targets. With `TARGETS=on` it also works the
other way: the energy and macro targets your coach set for the day in Enduo become the daily goal in Fitatu. Fitatu is
read through [fitatu-api](https://github.com/Jezue/fitatu_library), an unofficial Python client of Fitatu.

```
 Fitatu (app) ──► <region>.fitatu.com ◄── fitatu-api ── this container ──► Enduo MCP: nutrition_diary_set
                                       (goal of the day,                ◄── Enduo MCP: nutrition_targets_get
                                        with TARGETS=on)                    every POLL_SECONDS
```

- The container only connects **out**. No port is opened.
- It reads the diary of Fitatu and never changes it. The one thing it writes there, and only with `TARGETS=on`, is the
  daily goal (energy and macros) in your Fitatu settings.
- It asks Enduo only for the `write:food_log` scope: it can set the food diary and read the targets of the day, and
  nothing else (no change of the diet, no supplements, no other data), and it does not take the place of the coach
  container ([enduo-app/enduo-agent](https://github.com/enduo-app/enduo-agent)).

> [!IMPORTANT]
> **Unofficial.** This agent is not part of the Enduo service and comes without support or guarantees. It is not
> affiliated with, endorsed by or supported by Fitatu, nor by the authors of fitatu-api. Fitatu has no public API:
> fitatu-api uses the API of Fitatu's own web app ([docs/fitatu-api.md](docs/fitatu-api.md)), which may change with
> any update and may not be allowed by Fitatu's terms of use. Use it with your own account only, and at your own
> risk. Fitatu is a trademark of its owner.

## Quick start

With [compose.yaml](compose.yaml) from this repository:

```bash
docker compose up -d
docker compose exec -it fitatu enduo-fitatu setup
```

The image is `sklimaszewski/enduo-fitatu-agent` on Docker Hub (`linux/amd64` and `linux/arm64`); `docker compose build` builds
it from the checkout instead. The container starts and waits until `setup` is done; it picks up the sign-ins within
15 seconds.

### `enduo-fitatu setup`

1. **Fitatu.** It asks for the e-mail of your Fitatu account and runs fitatu-api's `login`, which asks for the
   password (or reads `FITATU_EMAIL` and `FITATU_PASSWORD`). The password is used once; fitatu-api writes only the
   session to the volume (`fitatu-session.json`: a token for an hour, the refresh token it renews it with, the
   account's region and time zone) and keeps it up to date by itself. Fitatu does not say how long the refresh token
   lives: when it ends, the log says "Signed out" and `enduo-fitatu setup --fitatu` signs in again. With
   `FITATU_EMAIL` and `FITATU_PASSWORD` in `.env` the container signs in again by itself instead, but the password
   then sits in `.env`. An account made with Apple, Google or Facebook has no password: see
   [Accounts without a password](#accounts-without-a-password).
2. **Enduo.** It shows a link, a code and a QR code. Open the link, sign in to Enduo, choose how long the token stays valid
   and approve with your Enduo password; Enduo confirms the approval by e-mail. The container gets
   a personal access token with the `write:food_log` scope (the diary, and reading the targets of the day), which you can revoke in Enduo at any time.
   Resetting your Enduo password revokes it too: sign the container in again afterwards. Instead,
   `ENDUO_TOKEN` can hold a personal access token with that scope.

Run `setup` again whenever something is off: it skips what already works.

### Accounts without a password

Sign in to <https://www.fitatu.com/app/> in a browser, open the developer console on that page and run the line
fitatu-api gives for this:

```js
copy(JSON.stringify((u => ({token: u.token, refresh_token: u.refresh_token, id: u.id}))(JSON.parse(localStorage.user))))
```

It copies only the tokens and your user id. In `enduo-fitatu setup`, leave the e-mail empty: fitatu-api's
`import-session` then asks for that JSON; paste it, press Enter and Ctrl-D. Treat it like a password: it gives full
access to your Fitatu account.

## What it sends

- **When:** every `POLL_SECONDS` (default 300) it reads the last `RECENT_DAYS` days (default 3: today and the two
  days before) and, once a day, the last `RECHECK_DAYS` (default 14), for a day you edit later. A day is sent when
  it differs from what was sent before; an unchanged day costs one read of Fitatu and nothing in Enduo.
- **Whole days:** `nutrition_diary_set` replaces every meal of the day in Enduo, so a meal or a food deleted in
  Fitatu disappears in Enduo too, and a day you emptied in Fitatu is emptied in Enduo. A day with nothing logged that
  the agent never sent is left alone, so meals you typed into Enduo yourself on such a day stay. A day is emptied in
  Enduo only when this agent filled it there, for the Enduo account and the Fitatu account it is signed in to now
  ([State](#state)), and only when Fitatu answers a day of the diary that has no food: an answer that cannot be read
  empties nothing.
- **First run:** the diary of the last `BACKFILL_DAYS` days (default 90) is sent, `BACKFILL_BATCH` days (default 14)
  per pass, newest first, with a second between two reads of Fitatu. With the defaults the history is in Enduo after
  about seven passes, half an hour. Filling in the history does not start an analysis of the coach.
- **Older days, any time:** `enduo-fitatu backfill --days 365`, or `--from 2026-01-01 [--to 2026-03-31]`, reads and
  sends those days now. Days already sent and unchanged are skipped; `--force` sends them again.
- **Eaten or planned:** Fitatu lets you tick a food as eaten, and many people log after eating and never tick.
  `DIARY_ITEMS=auto` (default) counts the ticked foods of a day when it has any, and every food when it has none;
  `eaten` counts only ticked foods, `all` every food. Days after today are never read, so meals planned ahead reach
  Enduo on their day.
- **Days** are the days of the time zone of your Fitatu account (`TIMEZONE` overrides it). When the account names a
  time zone the container does not know, the days are those of the time zone of your Enduo account, else of `TZ`,
  else of UTC, and the log says so once.

The agent reads the day as Fitatu sends it (fitatu-api's `planner.get_day()`), not the summary of fitatu-api's
`get_day` tool, which leaves out sodium, saturated fat, the millilitres of a drink and hidden rows.

| Fitatu | `nutrition_diary_set` |
|---|---|
| the day | `day`, `source` = `fitatu` |
| meal (breakfast, lunch, …) with its name in your language and its time from the settings | `meals[].name`, `meals[].time_of_day` |
| sum of the foods of a meal: energy, protein, carbohydrate, fat, fibre, sugars, saturated fat, sodium (or salt × 400) | `meals[].kcal`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `sugar_g`, `saturated_fat_g`, `sodium_mg` |
| food: name, brand, grams (millilitres for a drink, else the measure) | `meals[].items[]`: `name`, `brand`, `amount`, `unit` |
| food: energy, protein, carbohydrate, fat, sugars, fibre | `items[].kcal`, `protein_g`, `carbs_g`, `fat_g`, `sugar_g`, `fiber_g` |
| water of the day | `fluid_l` |

A meal without a food is left out. A nutrient no food of a meal has is left out of the meal, not sent as 0. Meal
times are the times of the meals in Fitatu's settings, not when you ate.

Names are text from Fitatu's food database, which anyone can add to, and the coach in Enduo, a language model, reads
them later. Before they are sent, control and invisible characters are removed, whitespace is collapsed and the text
is cut to what a name needs, with `…` at the cut: 120 characters for a food, 80 for a brand, 60 for a meal, 20 for a
unit.

### When something goes wrong

- **A day that cannot be read, or that Enduo refuses as invalid,** is skipped and the pass goes on with the next
  day; the backfill goes on past it too. A day counts as unreadable when Fitatu's answer is not a day of the diary
  as [docs/fitatu-api.md](docs/fitatu-api.md) describes it (no diet plan, a meal without its list of foods, a food
  that is not an object); nothing of such a day is sent, and what Enduo has for it stays. An odd value inside a
  food (a name that is no text, a number that is none) is only left out. The log names the day and says why, never
  what is in it; the pass ends with
  `Pass done: 2 sent, 0 unchanged, 1 empty, 1 skipped (unreadable or refused by Enduo)`, and `enduo-fitatu status`
  lists the skipped days. A skipped day is read on every pass like any other, but sent and reported again only when
  it changes in Fitatu (or with `--force`).
- **Everything else** (the network, Fitatu or Enduo answering 429 or 5xx, a session Fitatu ended, a token Enduo no
  longer takes or that lacks the scope) ends the pass without giving up on any day. The next pass starts where this
  one did; after `Signed out` run `enduo-fitatu setup`.

## Goal in Fitatu

Off by default. With `TARGETS=on` every pass reads the targets of today from Enduo (`nutrition_targets_get`: the
energy, protein, carbohydrate and fat your coach set for the day, its kind of day or the calorie window) and, when
they differ from what the agent set last, sets them as the goal in Fitatu.

- **From today on.** Fitatu keeps a goal per day, but a change applies from today on and the goal of a coming day
  cannot be set ahead. So the goal changes on the first pass after midnight (in the time zone of your days), and later
  in the day when your coach changes the target. Past days keep the goal they had, so Fitatu's history shows the
  target of each day. Tomorrow's page in Fitatu shows today's goal until tomorrow comes.
- **Whole percent.** Fitatu takes the energy and a split of it in whole percent, and works the grams out of them.
  Protein and fat keep Enduo's grams as closely as whole percent allow (within half a percent of the energy: 3 g of
  protein at 2400 kcal); carbohydrate takes the rest of the energy, so when Enduo's macros do not add up to its energy
  target, the difference lands there. Without all three macro targets only the energy is set and Fitatu keeps its
  split.
- **Your own goal is kept.** Before the first change the agent writes down the goal your account had (Fitatu's
  automatic one, or the energy and split you set). It is put back from today on when Enduo has no target for the day
  any more (the calorie window ended), and by `enduo-fitatu targets --restore`. Turning `TARGETS` off leaves the last
  goal in place: run `targets --restore` for yours.
- A goal you change by hand in Fitatu stays until Enduo's target changes.
- `enduo-fitatu targets --dry-run` shows the goal it would set, without changing anything; `enduo-fitatu targets`
  sets it now.

A Fitatu account without Premium takes a manual goal and split too.

## Commands

| Command | What it does |
|---|---|
| `enduo-fitatu run` | the service (default command of the image) |
| `enduo-fitatu setup [--fitatu\|--enduo]` | sign in; with a flag, sign in to that one again |
| `enduo-fitatu once [--dry-run] [--force]` | one pass now; `--dry-run` prints what would be sent and changes nothing |
| `enduo-fitatu backfill (--days N \| --from YYYY-MM-DD [--to YYYY-MM-DD]) [--dry-run] [--force]` | send those days now |
| `enduo-fitatu targets [--dry-run]` | set today's targets of Enduo as the goal in Fitatu now ([Goal in Fitatu](#goal-in-fitatu)) |
| `enduo-fitatu targets --restore [--dry-run]` | put back the goal the Fitatu account had before the agent set one |
| `enduo-fitatu status` | both sign-ins and the sync state |
| `enduo-fitatu health` | exit 0 while the service runs (Docker healthcheck) |

Run them with `docker compose exec -it fitatu enduo-fitatu <command>`. fitatu-api's own CLI is in the image too:
`fitatu-api --help`; give it the session with `FITATU_SESSION=/data/fitatu-session.json`.

## Configuration

All optional; see [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `ENDUO_URL` | `https://api.enduo.app` | Enduo API |
| `ENDUO_TOKEN` | — | personal access token (`write:food_log`) instead of the sign-in with a code |
| `ENDUO_CLIENT_NAME` | `Fitatu Agent (<hostname>)` | name of the token in Enduo |
| `POLL_SECONDS` | `300` | how often Fitatu is read (at least 60) |
| `RECENT_DAYS` | `3` | days read on every pass, today included |
| `RECHECK_DAYS` | `14` | days read again once a day; `0` turns it off |
| `BACKFILL_DAYS` | `90` | how far back the first run goes; `0` sends only the recent days |
| `BACKFILL_BATCH` | `14` | days of the first run read per pass |
| `DIARY_ITEMS` | `auto` | which foods count: `auto`, `eaten` or `all` (see [What it sends](#what-it-sends)) |
| `TARGETS` | `off` | `on` sets the day's targets of Enduo as the goal in Fitatu ([Goal in Fitatu](#goal-in-fitatu)) |
| `TIMEZONE` | the Fitatu account's | IANA time zone of your days, e.g. `Europe/Warsaw` |
| `FITATU_EMAIL`, `FITATU_PASSWORD` | — | the Fitatu sign-in, read by `setup`; with both set, also used to sign in again |
| `FITATU_LOG_LEVEL` | `WARNING` | log level of fitatu-api and the agent on stderr |
| `FITATU_CLIENT_PROFILE` | — | fitatu-api: overrides its web-client identity, e.g. `{"app_version": "4.14.0"}`, when Fitatu moves on before a release of fitatu-api |
| `ENDUO_USER_AGENT` | `enduo-fitatu-agent/<version> (+<this repository>)` | User-Agent of the calls to Enduo |
| `STATE_DIR` | `/data` | the volume |

## State

Everything lives on the `/data` volume, readable only by the container's user. Keep it and back it up like a password
file:

| File | Content |
|---|---|
| `fitatu-session.json` | fitatu-api's session file: the Fitatu tokens, user id, region and time zone |
| `enduo.json` | the Enduo token |
| `sync.json` | the accounts it belongs to (Enduo server, Enduo account id, Fitatu user id), a fingerprint of every day sent, the days skipped, and how far the backfill got |
| `targets.json` | the Fitatu account it belongs to, the goal the agent last set there, and the account's own goal from before (for `targets --restore`) |

Deleting `sync.json` makes the next pass start over as a first run. Enduo replaces each day it gets, so nothing is
duplicated. The agent then no longer knows which days it filled: a day that is empty in Fitatu by then is left as it
is in Enduo.

`sync.json` belongs to the accounts it was built from. When you sign the container in to another Enduo account,
another Enduo server or another Fitatu account, the next pass does not use it: it starts over as a first run for the
new accounts, renames the old file to `sync.<time>.other-account.json` (kept, never read again; delete it when you do
not need it) and says so once in the log. Nothing is emptied in the new Enduo account because of days the old state
remembers. `targets.json` belongs to the Fitatu account in the same way: the goal another account had before is never
put into this one.

A `sync.json` written before the agent recorded the accounts cannot say whose it is. It is kept for what does no
harm: a day it knows unchanged is not sent again and the backfill goes on where it was. But a day it remembers as
sent and that is empty in Fitatu now is **not** emptied in Enduo (delete its meals in Enduo yourself if you emptied
it); days the agent sends from then on are emptied as before. A `targets.json` from before is taken for the Fitatu
account signed in now, because it holds the only copy of the goal the account had.

## Verify the image

The release workflow of this repository signs every image it pushes with [cosign](https://docs.sigstore.dev/cosign/)
(keyless: the signature names the workflow that built the image, there is no key to trust). To check an image before
you run it, with cosign 3 or newer:

```bash
cosign verify sklimaszewski/enduo-fitatu-agent:1.2.3 \
  --certificate-identity-regexp '^https://github\.com/sklimaszewski/enduo-fitatu-agent/\.github/workflows/release\.yml@refs/(tags/v[0-9].*|heads/main)$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

It succeeds only for an image built by [.github/workflows/release.yml](.github/workflows/release.yml) of this
repository; images released before signing was introduced have no signature. Run a version you verified instead of
`latest`: set `FITATU_AGENT_TAG=1.2.3` in `.env`, which `compose.yaml` reads, so the image changes only when you
change the version.

## Development

Python 3.11+ (fitatu-api's minimum).

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy src/enduo_fitatu tests
STATE_DIR=./data .venv/bin/enduo-fitatu setup --fitatu
STATE_DIR=./data .venv/bin/enduo-fitatu backfill --days 3 --dry-run
```

fitatu-api is pinned (`fitatu-api==0.6.0` in [pyproject.toml](pyproject.toml)): it follows a private API, so a new
release is tried with `--dry-run` before it is taken.

Releases: pushing a tag `vX.Y.Z` builds and pushes `sklimaszewski/enduo-fitatu-agent:X.Y.Z` (and `X.Y`, `X`, `latest`) with
[.github/workflows/release.yml](.github/workflows/release.yml).

## Contributing

Issues and pull requests are welcome. A change of Fitatu's API belongs to
[fitatu-api](https://github.com/Jezue/fitatu_library) first; update [docs/fitatu-api.md](docs/fitatu-api.md) when the
fields this agent reads change. Never paste tokens, passwords, captured traffic or diary data of a real account into
an issue.

## License

[MIT](LICENSE). fitatu-api is MIT-licensed by its authors. Enduo and Fitatu are trademarks of their owners; this
project uses the names only to say what it connects.
