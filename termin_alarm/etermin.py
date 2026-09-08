"""A small client for the (undocumented) eTermin booking API.

Most German municipalities that use eTermin expose the same JSON endpoint that
their own booking widget talks to::

    GET /api/timeslots?serviceid=...&date=...&rangesearch=1   -> days with capacity
    GET /api/timeslots?serviceid=...&date=...                 -> slots on one day

Querying it directly is far cheaper and far more reliable than driving a
headless browser: the whole scan costs one request per month in range plus one
per candidate day. The endpoint ignores clients that have not visited the
portal first, hence :meth:`ETerminClient.prime`.
"""

from __future__ import annotations

import logging
import random
import string
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import requests

from termin_alarm.config import Target

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
REQUEST_TIMEOUT = 15
# Deliberate pacing: the portal is a public service, not a load-test victim.
POLITE_DELAY = 0.4


@dataclass(frozen=True)
class Slot:
    """One bookable appointment."""

    day: date
    start: str  # "HH:MM"

    def __str__(self) -> str:
        return f"{self.day.isoformat()} {self.start}"


def working_days_ahead(days: int, start: date | None = None) -> date:
    """Return the date ``days`` working days after ``start`` (Mon-Fri only)."""
    current = start or date.today()
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def month_query_dates(end_date: date, start: date | None = None) -> list[date]:
    """First-of-month probe dates covering ``start``..``end_date`` inclusive."""
    today = start or date.today()
    dates = [today]
    cursor = today.replace(day=1)
    while True:
        # Jump to the next month without needing calendar arithmetic.
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        if cursor > end_date:
            break
        dates.append(cursor)
    return dates


def parse_available_days(payload: Iterable[dict[str, Any]]) -> list[date]:
    """Extract days with remaining capacity from a ``rangesearch`` response."""
    days: list[date] = []
    for entry in payload or []:
        if not isinstance(entry, dict) or entry.get("available", 0) <= 0:
            continue
        raw = str(entry.get("start", ""))[:10]
        try:
            days.append(date.fromisoformat(raw))
        except ValueError:
            log.debug("skipping unparsable day %r", raw)
    return sorted(set(days))


def parse_time_slots(payload: Iterable[dict[str, Any]]) -> list[str]:
    """Extract bookable ``HH:MM`` start times from a single-day response.

    ``f`` (free) and ``hv`` (has vacancy) are the flags the portal sets on a
    slot that can actually be booked; a ``00:00`` start is the placeholder it
    returns for a fully booked day.
    """
    times: list[str] = []
    for slot in payload or []:
        if not isinstance(slot, dict):
            continue
        if not (slot.get("f") and slot.get("hv")):
            continue
        start = str(slot.get("start", ""))
        if "T" not in start or len(start) < 16:
            continue
        hhmm = start[11:16]
        if hhmm != "00:00":
            times.append(hhmm)
    return sorted(set(times))


class ETerminClient:
    """Queries one eTermin portal. Inject ``session`` to test without network."""

    def __init__(
        self,
        target: Target,
        session: requests.Session | None = None,
        delay: float = POLITE_DELAY,
    ) -> None:
        self.target = target
        self.delay = delay
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "Referer": target.booking_url,
                "webid": target.portal_id,
                "Cache-Control": "no-cache",
            }
        )

    # -- session priming ---------------------------------------------------

    @staticmethod
    def _make_sid() -> str:
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        return f"sid_{rnd}_{int(time.time() * 1000)}"

    def prime(self) -> None:
        """Pick up the cookies the API expects, the way a real visitor would."""
        target = self.target
        try:
            if target.account_id:
                sid = self._make_sid()
                for pageidx in (1, 2):
                    self.session.post(
                        f"{target.base_url}/site",
                        params={
                            "pageidx": pageidx,
                            "z": target.account_id,
                            "storeip": "true",
                            "sid": sid,
                            "utm_source": "",
                            "utm_medium": "",
                            "utm_campaign": "",
                            "ref_url": "",
                        },
                        headers={"Origin": target.base_url},
                        data=b"",
                        timeout=REQUEST_TIMEOUT,
                    )
                    self._sleep(0.3)
            else:
                self.session.get(target.booking_url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            # Priming is best effort; the API call afterwards may still work.
            log.warning("could not prime session (%s), continuing", exc)

    def _sleep(self, seconds: float) -> None:
        if self.delay:
            time.sleep(seconds)

    # -- raw API -----------------------------------------------------------

    def _timeslots(self, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        """GET /api/timeslots. ``None`` means the portal cut the session off."""
        try:
            response = self.session.get(
                f"{self.target.base_url}/api/timeslots",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("request failed: %s", exc)
            return []

        # The portal answers 400 once a session has asked for too many months.
        # That is a stop signal, not a bug.
        if response.status_code == 400:
            return None
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("bad response from portal: %s", exc)
            return []
        return payload if isinstance(payload, list) else []

    def _common_params(self) -> dict[str, Any]:
        target = self.target
        return {
            "serviceid": target.service_id,
            "capacity": 1,
            "caching": "false",
            "duration": target.duration,
            "cluster": "false",
            "slottype": 0,
            "fillcalendarstrategy": 0,
            "showavcap": "false",
            "appfuture": target.app_future,
            "appdeadline": target.app_deadline,
            "appdeadlinewm": 0,
            "oneoff": "null",
            "msdcm": 0,
            "calendarid": target.calendar_id,
        }

    def available_days(self, query_date: date, counter: int = 9) -> list[date] | None:
        params = self._common_params()
        params.update(
            date=query_date.strftime("%Y-%m-%d"),
            rangesearch=1,
            searchcounter=counter,
        )
        payload = self._timeslots(params)
        if payload is None:
            return None
        return parse_available_days(payload)

    def time_slots(self, day: date) -> list[str]:
        params = self._common_params()
        params.update(
            date=day.strftime("%Y-%m-%d"),
            tz="W. Europe Standard Time",
            tzaccount="W. Europe Standard Time",
        )
        return parse_time_slots(self._timeslots(params) or [])

    # -- the actual check --------------------------------------------------

    def find_slots(self, today: date | None = None) -> list[Slot]:
        """Two-phase search: which days have capacity, then at what times."""
        target = self.target
        today = today or date.today()
        end_date = working_days_ahead(target.app_future, today)
        probes = month_query_dates(end_date, today)

        log.info(
            "%s: scanning %s to %s (%d month request(s))",
            target.key, today, end_date, len(probes),
        )
        self.prime()
        self._sleep(0.5)

        candidates: list[date] = []
        for index, probe in enumerate(probes):
            days = self.available_days(probe, counter=9 + index)
            if days is None:
                log.info("%s: portal stopped answering range queries", target.key)
                break
            in_range = [d for d in days if today <= d <= end_date]
            kept = [d for d in in_range if target.filters.accepts_day(d)]
            log.info(
                "%s: %s - %d day(s) with capacity%s",
                target.key,
                probe.strftime("%B %Y"),
                len(in_range),
                "" if len(kept) == len(in_range) else f" ({len(kept)} after filters)",
            )
            candidates.extend(kept)
            self._sleep(self.delay)

        slots: list[Slot] = []
        for day in sorted(set(candidates)):
            times = [t for t in self.time_slots(day) if target.filters.accepts_time(t)]
            log.info(
                "%s: %s - %s",
                target.key,
                day.strftime("%a %d.%m.%Y"),
                " | ".join(times) if times else "no bookable slots",
            )
            slots.extend(Slot(day, t) for t in times)
            self._sleep(0.3)

        return slots


def group_by_day(slots: Sequence[Slot]) -> dict[date, list[str]]:
    """``[Slot, ...]`` -> ``{day: ["09:00", "09:20"], ...}``, sorted."""
    grouped: dict[date, list[str]] = {}
    for slot in slots:
        grouped.setdefault(slot.day, []).append(slot.start)
    return {day: sorted(times) for day, times in sorted(grouped.items())}
