# Termin Radar

**Watches German public-authority booking portals and alerts you the moment an appointment slot opens up.**

[![CI](https://github.com/Borys-Mukovnin/termin-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/Borys-Mukovnin/termin-radar/actions/workflows/ci.yml)
[![Watcher](https://github.com/Borys-Mukovnin/termin-radar/actions/workflows/watch.yml/badge.svg)](https://github.com/Borys-Mukovnin/termin-radar/actions/workflows/watch.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## The problem

Getting an appointment at a German *Ausländerbehörde*, *Fahrerlaubnisbehörde* or
*Bürgeramt* means opening a booking portal that says **"Keine Termine verfügbar"**
almost every time you look. Cancellations and newly released batches do appear —
but they are taken within minutes, so catching one means refreshing the page
dozens of times a day for weeks.

This project does the refreshing. It polls the portals every 15 minutes on
GitHub Actions and pushes an e-mail or Telegram message the moment something
frees up, with the exact dates, the exact times, and a link straight to the
booking page.

It costs nothing to run: no server, no browser automation, no third-party
service — just a scheduled Actions workflow and a few hundred lines of Python.

## How it works

Most German municipalities outsource booking to [eTermin](https://www.etermin.net),
whose booking widget talks to a plain JSON endpoint. Talking to that endpoint
directly — instead of driving a headless browser — makes each check take about
a second and a couple of HTTP requests.

```
                  ┌──────────────────────────────────────────┐
  cron (*/15)  ─► │  GitHub Actions                          │
                  │                                          │
                  │  1. restore alert history from cache     │
                  │  2. for each target in targets.toml:     │
                  │       ├─ prime session (cookies)         │
                  │       ├─ phase 1: which DAYS have room?  │  ── GET /api/timeslots
                  │       └─ phase 2: which TIMES on those?  │  ── GET /api/timeslots
                  │  3. drop slots already reported          │
                  │  4. notify + write run summary           │  ──► e-mail / Telegram
                  │  5. save alert history back to cache     │
                  └──────────────────────────────────────────┘
```

Three details make it usable rather than merely working:

- **Two-phase search.** A cheap month-wide range query finds the days that have
  any capacity at all; only those days are then queried for exact times. A
  three-month horizon costs ~3 requests instead of ~90.
- **Alert deduplication.** The set of already-announced slots is carried between
  workflow runs in the Actions cache, so one free appointment produces one
  notification — not one every 15 minutes until somebody books it.
- **Filters.** A slot at 07:40 on a Saturday three cities away may be useless to
  you. Targets can restrict alerts to certain weekdays and a time window.

The portal is a public service, so requests are paced deliberately and the
scanner stops as soon as the API signals it has had enough.

## Quick start

```bash
git clone https://github.com/Borys-Mukovnin/termin-radar.git
cd termin-radar
pip install -r requirements.txt

# What is being watched?
python -m termin_radar --list

# Check now, print any hits instead of mailing them
python -m termin_radar --dry-run
```

Nothing is sent until you configure a channel, so `--dry-run` is safe to try
straight away.

## Configuring what to watch

Everything lives in [`targets.toml`](targets.toml). One block per service:

```toml
[targets.fahrerlaubnis]
name         = "Fahrerlaubnis - Stadt Duisburg"
portal_id    = "qterminstadtduisburgstva"   # the path after etermin.net/
service_id   = 110023                       # the service you picked
duration     = 0
app_future   = 14                           # booking horizon, in working days

[targets.fahrerlaubnis.filters]             # optional
weekdays      = ["mon", "tue", "wed", "thu", "fri"]
earliest_time = "08:00"
latest_time   = "15:30"
```

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Human-readable label used in alerts and logs |
| `portal_id` | yes | Path segment after `etermin.net/` |
| `service_id` | yes | Numeric id of the service on that portal |
| `account_id` | no | Numeric `z` id; when present the session is primed the way the widget does it |
| `duration` | no | Appointment length the portal expects (`0` for many offices) |
| `app_future` | no | How many working days ahead the portal lets you book |
| `app_deadline` | no | Lead time the portal enforces |
| `calendar_id` | no | Specific calendar, when an office splits its service across several |
| `info_url` | no | Link to the authority's own info page, added to alerts |
| `enabled` | no | Set `false` to keep a target configured but skipped |

A `[defaults]` table at the top applies to every target; a target can override
any of it (a target's own `filters` table replaces the default one as a whole).
Unknown keys are rejected rather than ignored, so a typo such as `app_futur`
stops the run instead of quietly falling back to a default. **Adding a new office is a config change, not a code change** —
[`docs/adding-a-target.md`](docs/adding-a-target.md) walks through finding the
four values you need with the browser dev tools.

## Getting notified

Channels are configured entirely through environment variables. Configure one
or both; if none are set, the run stops with an explanation instead of
pretending to work.

### E-mail

| Variable | Required | Default |
| --- | --- | --- |
| `SMTP_USER` | yes | — |
| `SMTP_PASSWORD` | yes | — |
| `ALERT_TO` | yes | — (comma-separated list) |
| `SMTP_HOST` | no | `smtp.gmail.com` |
| `SMTP_PORT` | no | `587` |

With Gmail, `SMTP_PASSWORD` must be an
[App Password](https://support.google.com/accounts/answer/185833), not the
account password.

### Telegram

| Variable | Required |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes |
| `TELEGRAM_CHAT_ID` | yes |

Create a bot with [@BotFather](https://t.me/botfather), send it a message, then
read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
Worth the five minutes: a push notification arrives in seconds, while mail
clients often sync on a schedule — and these slots do not wait.

See [`.env.example`](.env.example) for a copy-paste starting point.

## Running it on GitHub Actions

[`.github/workflows/watch.yml`](.github/workflows/watch.yml) runs the check
every 15 minutes, around the clock and at weekends — slots turn up at night
too. To use it on your own fork:

1. Add the variables above as repository secrets
   (*Settings → Secrets and variables → Actions*). Only the channels you
   actually want need secrets.
2. Choose what to watch: edit `targets.toml` and commit, or set the repository
   variables below to change it without a commit.
3. Enable Actions on the fork. Trigger a first run by hand from the **Actions**
   tab — the dispatch form has *target*, *dry run* and *force* switches.

### Changing targets without a commit

Two optional repository **variables** (*Settings → Secrets and variables →
Actions → Variables*) override the committed config on the next run:

| Variable | Effect |
| --- | --- |
| `TERMIN_TARGETS` | Comma-separated target keys to check, e.g. `fahrerlaubnis`. Like `--target`, it also runs targets marked `enabled = false`, so `targets.toml` can hold a catalogue of offices and the variable picks from it. |
| `TERMIN_CONFIG` | A complete targets file, pasted in as TOML. Used instead of `targets.toml`, for watching an office the repository does not know about. |

A dispatch form's *target* field still wins over `TERMIN_TARGETS`. Secrets of
the same names work too, but GitHub then masks their values in the logs, so
variables are the more readable choice. Reported slots are remembered per
office and service, so repointing a target at a different service alerts on
that service's slots from scratch.

At 96 runs a day, the schedule fits comfortably in a public repository, where
Actions minutes are free. In a private one it uses around 2,900 minutes a
month, more than the 2,000 included in the free plan; widen the cron interval
in `watch.yml` if that matters.

Every run writes a summary table to the run page, so the Actions tab doubles as
a log of what was free and when:

> **Termin Radar** — *Checked 2025-05-05 09:15 CEST (2 targets)*
>
> | Target | Free slots | New since last run | Portal |
> | --- | ---: | ---: | --- |
> | Niederlassungserlaubnis - Duisburg, ABH Sued | 0 | 0 | booking page |
> | Fahrerlaubnis - Stadt Duisburg | 4 | 2 | booking page |

If a portal cannot be reached or answers with an error, that target's row
reads **check failed** and the run is marked as failed, so a broken target
shows up as a red run instead of passing as an empty calendar.

Two things to know about scheduled workflows: GitHub queues `schedule` runs on a
best-effort basis, so an occasional run drifts by a few minutes, and it disables
schedules in public repositories after 60 days without commits — a manual
dispatch re-enables them.

## Command line

```
python -m termin_radar [options]

  -c, --config PATH     targets file (default: $TERMIN_CONFIG if set, else
                        targets.toml)
  -t, --target KEY      only check this target; repeatable (default: the keys
                        in $TERMIN_TARGETS if set, else all enabled targets)
  -l, --list            show configured targets and exit
  -n, --dry-run         print alerts instead of sending them
  -f, --force           re-alert on slots that were already reported (and
                        remember them, so the next normal run stays quiet)
      --state PATH      where to keep the seen-slots file
      --no-delay        skip the polite pauses between requests
  -v, --verbose         debug logging
  -q, --quiet           warnings only
```

Installing the package (`pip install -e .`) also puts a `termin-radar` command
on the path.

## Project layout

```
termin_radar/
  cli.py        argument parsing, run loop, GitHub job summary
  config.py     targets.toml -> validated Target objects
  etermin.py    the eTermin API client and its response parsers
  notify.py     alert rendering, e-mail / Telegram / console channels
  state.py      which slots have already been announced
tests/          pytest suite, no network access required
docs/           how to add a new office
targets.toml    what is being watched
```

The layering is deliberate: `etermin.py` knows nothing about notifications,
`notify.py` knows nothing about scraping, and both take their inputs as plain
data — which is what makes the whole suite runnable without a network
connection or a single credential.

## Development

```bash
pip install -r requirements-dev.txt
pytest          # unit tests, fully offline
ruff check .    # lint
```

Network calls are isolated behind an injected session object, so the tests
drive the client with recorded API payloads. CI runs the suite on Python 3.11,
3.12 and 3.13.

## Notes

This is a personal tool, built because the alternative was refreshing a booking
page by hand for six weeks. It reads the same public endpoint the official
booking widget reads, at a far lower rate than a human clicking through the
calendar, and it never books anything — it only tells you to go look.

Portals change. If a target stops returning results, re-check its `service_id`
and `app_future` in the dev tools; the parameters are the first thing an office
changes when it reorganises its services.

## License

MIT — see [LICENSE](LICENSE).
