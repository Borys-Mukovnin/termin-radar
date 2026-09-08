from __future__ import annotations

from dataclasses import replace
from datetime import date, time

import pytest

from termin_alarm.config import SlotFilter
from termin_alarm.etermin import (
    ETerminClient,
    Slot,
    group_by_day,
    month_query_dates,
    parse_available_days,
    parse_time_slots,
    working_days_ahead,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("raise_for_status should not be reached in these tests")

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for requests.Session: records calls, replays canned answers."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "/api/timeslots" not in url:
            return FakeResponse([])
        return self.responses.pop(0) if self.responses else FakeResponse([])

    def post(self, url, params=None, headers=None, data=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "params": params})
        return FakeResponse([])


def day_payload(*days_with_capacity):
    return FakeResponse([{"start": f"{d}T00:00:00", "available": 1} for d in days_with_capacity])


def slot_payload(day, *times):
    return FakeResponse(
        [{"start": f"{day}T{t}:00", "f": True, "hv": True} for t in times]
    )


# -- pure helpers ----------------------------------------------------------


def test_working_days_ahead_skips_weekends():
    # Friday 2025-01-03 + 1 working day is Monday the 6th.
    assert working_days_ahead(1, date(2025, 1, 3)) == date(2025, 1, 6)
    assert working_days_ahead(5, date(2025, 1, 6)) == date(2025, 1, 13)


def test_working_days_ahead_of_zero_is_today():
    assert working_days_ahead(0, date(2025, 1, 3)) == date(2025, 1, 3)


def test_month_query_dates_covers_every_month_in_range():
    probes = month_query_dates(date(2025, 3, 10), date(2025, 1, 20))
    assert probes == [date(2025, 1, 20), date(2025, 2, 1), date(2025, 3, 1)]


def test_month_query_dates_within_one_month_probes_once():
    assert month_query_dates(date(2025, 1, 25), date(2025, 1, 20)) == [date(2025, 1, 20)]


def test_parse_available_days_keeps_only_days_with_capacity():
    payload = [
        {"start": "2025-05-05T00:00:00", "available": 2},
        {"start": "2025-05-06T00:00:00", "available": 0},
        {"start": "2025-05-07T00:00:00"},
        {"start": "garbage", "available": 1},
        "not-a-dict",
    ]
    assert parse_available_days(payload) == [date(2025, 5, 5)]


def test_parse_time_slots_requires_both_portal_flags():
    payload = [
        {"start": "2025-05-05T09:00:00", "f": True, "hv": True},
        {"start": "2025-05-05T09:20:00", "f": True, "hv": False},
        {"start": "2025-05-05T09:40:00", "f": False, "hv": True},
    ]
    assert parse_time_slots(payload) == ["09:00"]


def test_parse_time_slots_drops_the_fully_booked_placeholder():
    payload = [
        {"start": "2025-05-05T00:00:00", "f": True, "hv": True},
        {"start": "2025-05-05T11:30:00", "f": True, "hv": True},
    ]
    assert parse_time_slots(payload) == ["11:30"]


def test_parse_time_slots_deduplicates_and_sorts():
    payload = [
        {"start": "2025-05-05T14:00:00", "f": 1, "hv": 1},
        {"start": "2025-05-05T08:00:00", "f": 1, "hv": 1},
        {"start": "2025-05-05T14:00:00", "f": 1, "hv": 1},
    ]
    assert parse_time_slots(payload) == ["08:00", "14:00"]


@pytest.mark.parametrize("payload", [[], None, [{}], [{"f": True, "hv": True, "start": "x"}]])
def test_parsers_survive_junk(payload):
    assert parse_available_days(payload) == []
    assert parse_time_slots(payload) == []


def test_group_by_day():
    slots = [Slot(date(2025, 5, 6), "10:00"), Slot(date(2025, 5, 5), "09:20"),
             Slot(date(2025, 5, 5), "09:00")]
    assert group_by_day(slots) == {
        date(2025, 5, 5): ["09:00", "09:20"],
        date(2025, 5, 6): ["10:00"],
    }


# -- client ----------------------------------------------------------------


def test_find_slots_runs_both_phases(target):
    session = FakeSession([day_payload("2025-05-05"), slot_payload("2025-05-05", "09:00", "09:20")])
    client = ETerminClient(target, session=session, delay=0)

    slots = client.find_slots(today=date(2025, 5, 1))

    assert slots == [Slot(date(2025, 5, 5), "09:00"), Slot(date(2025, 5, 5), "09:20")]
    api_calls = [c for c in session.calls if "/api/timeslots" in c["url"]]
    assert len(api_calls) == 2
    assert api_calls[0]["params"]["rangesearch"] == 1
    assert api_calls[0]["params"]["serviceid"] == target.service_id
    assert "rangesearch" not in api_calls[1]["params"]


def test_find_slots_ignores_days_outside_the_booking_horizon(target):
    session = FakeSession([day_payload("2024-01-01", "2099-01-01")])
    client = ETerminClient(target, session=session, delay=0)

    assert client.find_slots(today=date(2025, 5, 1)) == []
    assert len([c for c in session.calls if "/api/timeslots" in c["url"]]) == 1


def test_find_slots_stops_when_the_portal_returns_400(target):
    # 42 working days past 1 May 2025 reaches into July, so the client would
    # normally probe three months; the 400 must cut the scan short instead.
    long_horizon = replace(target, app_future=42)
    session = FakeSession([FakeResponse([], status_code=400), day_payload("2025-06-05")])
    client = ETerminClient(long_horizon, session=session, delay=0)

    assert client.find_slots(today=date(2025, 5, 1)) == []
    assert len([c for c in session.calls if "/api/timeslots" in c["url"]]) == 1


def test_find_slots_applies_target_filters(target):
    filtered = replace(target, filters=SlotFilter(earliest_time=time(9, 30)))
    session = FakeSession([day_payload("2025-05-05"), slot_payload("2025-05-05", "09:00", "10:00")])

    slots = ETerminClient(filtered, session=session, delay=0).find_slots(today=date(2025, 5, 1))
    assert slots == [Slot(date(2025, 5, 5), "10:00")]


def test_prime_uses_the_site_endpoint_when_an_account_id_is_known(target):
    session = FakeSession([])
    ETerminClient(target, session=session, delay=0).prime()

    posts = [c for c in session.calls if c["method"] == "POST"]
    assert len(posts) == 2
    assert posts[0]["params"]["z"] == target.account_id


def test_prime_falls_back_to_the_booking_page(target):
    session = FakeSession([])
    ETerminClient(replace(target, account_id=None), session=session, delay=0).prime()

    assert session.calls == [
        {"method": "GET", "url": target.booking_url, "params": None}
    ]
