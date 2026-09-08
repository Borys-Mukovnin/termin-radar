# Adding a new office

Everything the watcher needs about an office is visible in the browser dev
tools while you use its booking page normally. Budget five minutes.

## 1. Find the booking page

Most German authorities link to theirs from a page called *Terminvereinbarung*.
If the URL looks like this, the office uses eTermin and this tool can watch it:

```
https://www.etermin.net/qterminstadtduisburgstva
                        ^^^^^^^^^^^^^^^^^^^^^^^^
                        this is your portal_id
```

Some municipalities embed the widget in their own site instead. Open the page,
look for an `<iframe>` in the dev tools *Elements* panel, and take the
`etermin.net/...` URL from its `src`.

## 2. Watch the network while you click

1. Open the booking page and press **F12** → **Network** tab.
2. Tick **Fetch/XHR** so the list stays readable, then clear it.
3. Pick your service (*Dienstleistung*) and continue to the calendar step.

You will see one or more requests to `/api/timeslots`. Click the first one and
open **Headers → Query String Parameters**:

```
date                 2025-05-01
serviceid            110023        <- service_id
rangesearch          1
duration             0             <- duration
appfuture            14            <- app_future
appdeadline          15            <- app_deadline
calendarid                         <- calendar_id (usually empty)
```

## 3. Find the account id (optional)

Some portals ignore an API client that has not been "seen" arriving on the
site. If your target returns nothing while the browser clearly shows slots,
look in the same Network tab for a `POST` to `/site`:

```
/site?pageidx=1&z=71400&storeip=true&sid=sid_...
                ^^^^^
                account_id
```

Add it as `account_id` and the watcher will prime its session the same way the
widget does. Leave it out and the watcher simply fetches the booking page
first, which is enough for most offices.

## 4. Write the block

```toml
[targets.buergeramt_essen]
name         = "Buergeramt Essen - Anmeldung"
portal_id    = "qtermin-essen-buergeramt"
service_id   = 123456
duration     = 10
app_future   = 30
info_url     = "https://www.essen.de/..."
```

## 5. Check it

```bash
python -m termin_alarm --target buergeramt_essen --dry-run --verbose
```

`--verbose` prints every phase, so you can tell the difference between *the
office has nothing free* and *the parameters are wrong*:

- **`0 day(s) with capacity` on every month** — usually correct, but compare
  against what the real calendar shows in your browser right now.
- **`portal stopped answering range queries` immediately** — the session was
  not accepted; try adding `account_id`.
- **days found, but `no bookable slots` on each of them** — the day query
  works and the time query does not, which normally means `duration` or
  `calendar_id` does not match the service.

Once it looks right, drop `--dry-run` and commit the config.

## A different booking system?

Plenty of authorities use something other than eTermin (`termine-reservieren.de`
and various in-house systems are common). The layering here anticipates that:
`config.py`, `state.py` and `notify.py` are portal-agnostic, so a second
backend means writing a client next to `etermin.py` that returns the same
`list[Slot]`, and giving targets a `kind` to select it.
