# The Fitatu API this agent reads

Fitatu has no public API. The agent reads it through [fitatu-api](https://github.com/Jezue/fitatu_library)
(pinned in `pyproject.toml`), an unofficial Python client of the API of Fitatu's own web app
(<https://www.fitatu.com/app/>). fitatu-api's [docs](https://github.com/Jezue/fitatu_library/tree/main/docs) describe
that API in full (decompiled from the Android app 4.17.1 and recorded live in October 2026); this page keeps only
what the agent depends on, so that a change of Fitatu or of fitatu-api can be checked against it. Anything here may
change with an update of Fitatu.

The agent reads the diary and never changes it (`POST /diet-plan/{userId}/days` and the like are not called). The one
write is the daily goal, with `TARGETS=on` only (section 5).

## 1. What fitatu-api does for the agent

| Agent | fitatu-api |
|---|---|
| `enduo-fitatu setup` | `fitatu-api login --email … -o /data/fitatu-session.json` (password from the prompt or `FITATU_PASSWORD`), or `fitatu-api import-session -o …` for an account without a password |
| every read | `FitatuApiClient.from_session("/data/fitatu-session.json")`: the session file, its tokens refreshed a minute before they end and once after a 401, and written back to the file |
| a day | `client.planner.get_day(user_id, day)`: Fitatu's answer as it is (section 4) |
| the account's days | `client.auth.app_timezone`, from the profile read at sign-in |
| the goal of today (`TARGETS=on`) | `FitatuTools(client).set_targets(energy_kcal=…, protein_pct=…, fat_pct=…, carbohydrate_pct=…)`, and `set_targets(**previous)` to put the account's goal back (section 5) |
| errors | `FitatuAuthError` (the session is gone), `FitatuRateLimitError`, `FitatuConnectionError`, … ; retries of 429 and 5xx inside fitatu-api |

The agent passes the path of the session file every time, so fitatu-api's `$FITATU_SESSION` and
`$FITATU_SESSION_JSON` are not read by the service.

## 2. Host, headers and session (inside fitatu-api)

- The API of an account is `https://<storage locale>.fitatu.com/api`, e.g. `https://pl-pl.fitatu.com/api`.
- Every call carries the public identity of the web client (`API-Key`, `API-Secret`, `APP-UUID`, `APP-OS`,
  `APP-Version`); without `API-Key` and `API-Secret` the API answers 403 (checked 2026-11-14).
  `FITATU_CLIENT_PROFILE` overrides them should Fitatu change them before fitatu-api does.
- `POST /login` (`{"_username", "_password"}`) and `POST /token/refresh` (`{"refresh_token"}`) answer
  `{token, refresh_token}`. The token is a JWT that lives an hour; its `id` claim is the user id. The refresh token is
  not rotated, and how long it lives is not known. A wrong password is a 422.
- When the refresh token is refused, fitatu-api raises `FitatuAuthError`. The agent then signs in again with
  `FITATU_EMAIL` and `FITATU_PASSWORD` when both are set, and otherwise logs "Signed out".

## 3. Profile

`GET /users/{userId}` →

```json
{ "id": "1234567", "locale": "en_GB", "storageLocale": "pl_PL", "searchLocale": "pl_PL", "timezone": "Europe/Warsaw", "weightUnit": "KG", "sizeUnit": "CM" }
```

fitatu-api reads it at sign-in and keeps `storageLocale` (the host), the locales and `timezone` in the session file; the agent takes the time zone from there for the days of the account. `enduo-fitatu status` reads it once to show that the session works.

## 4. A day of the diary

`GET /diet-and-activity-plan/{userId}/day/{YYYY-MM-DD}` →

```json
{
  "planDayRevisions": ["…"],
  "dietPlan": {
    "breakfast": {
      "mealName": "Breakfast", "mealTime": "08:00", "recommendedPercent": 20,
      "items": [{
        "planDayDietItemId": "2425e4b1-…", "foodType": "PRODUCT", "productId": 15956231, "name": "Białko jaja kurzego", "brand": null,
        "measureName": "portion", "measureQuantity": 1, "weight": 35, "capacity": null, "liquid": false,
        "energy": 16.8, "protein": 3.815, "fat": 0.07, "carbohydrate": 0.245, "fiber": null, "sugars": null,
        "saturatedFat": 0, "salt": 0.175, "sodium": 70, "eaten": false, "visible": true, "updatedAt": "2026-10-05 12:32:23"
      }]
    },
    "second_breakfast": { … }, "lunch": { … }, "dinner": { … }, "snack": { … }, "supper": { … }
  },
  "activityPlan": [],
  "water": { "waterConsumption": 0 },
  "note": null, "tagsIds": []
}
```

- The meals are the slots of the account's settings, in the order of the day; `mealName` is in the account's
  language, `mealTime` the time of the slot in the settings.
- `foodType` is `PRODUCT`, `RECIPE` or `CUSTOM_ITEM`. The nutrients of a row are those of the amount eaten (a recipe
  row's cover the portions eaten), in kcal and grams; `sodium` is in milligrams, `salt` in grams.
- `weight` is the grams of the row (the weight of the measure times `measureQuantity`), `capacity` the millilitres of
  a drink (`liquid`). A custom row may have neither.
- Numbers may come as strings (`"12.50"`).
- `eaten` is the tick of a row. A row written without it is stored as not eaten.
- A deleted row is hidden (`visible: false` or `deletedAt`).
- `water.waterConsumption` is the water of the day in millilitres.

## 5. The daily goal (`TARGETS=on`)

`GET /users/{userId}/settings-new/{YYYY-MM-DD}` gives the settings in effect on a day; `userDietSettings` holds the
goal:

```json
{ "manualEnergyTarget": true, "energy": 2400, "proteinPercentage": 28, "proteinWeight": 168, "fatPercentage": 26, "fatWeight": 69, "carbohydratePercentage": 46, "carbohydrateWeight": 276, … }
```

`PATCH /users/{userId}/settings-new` (`{"userId", "updatedAt": null, "userDietSettings": {…}}`) changes it **from today
on**: earlier days keep theirs, and the body carries no day, so a coming day cannot be set ahead. Fitatu refuses a part
of `userDietSettings` (the whole object goes back with the changes), and checks the grams against the percentages
within 1 g. `manualEnergyTarget: false` goes back to the goal Fitatu works out itself. A free account takes a manual
goal and split too (fitatu-api, checked live 2026-10-15).

The agent does not build that body: fitatu-api's `set_targets` tool does (energy, whole percent summing to 100, the
grams from them), reads the settings back and answers `previous`, the arguments that put back what it changed
(`{"automatic": true}` for Fitatu's own goal). The agent keeps the first `previous` in `targets.json`.

## 6. Not used

- `GET /diet-plan/{userId}/days/{date}/revisions`: the revisions of a day, a cheaper check for a change than reading
  the day. The agent compares what it would send instead, which also notices a change of its own mapping.
- `GET /diet-plan/{userId}/export` (CSV of a range): needs Fitatu Premium.
- `GET /v2/diet-plan/{userId}/summary/custom`: totals of a range, without the meals.
