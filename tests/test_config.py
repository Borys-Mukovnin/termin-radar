from __future__ import annotations

import tomllib
from datetime import date, time

import pytest

from termin_alarm.config import (
    ConfigError,
    SlotFilter,
    load_targets,
    parse_targets,
    select_targets,
)

MINIMAL = """
[targets.a]
name = "Office A"
portal_id = "qtermin-a"
service_id = 1
"""


def parse(text: str):
    return parse_targets(tomllib.loads(text))


def test_minimal_target_gets_sensible_defaults():
    target = parse(MINIMAL)["a"]
    assert target.key == "a"
    assert target.enabled is True
    assert target.account_id is None
    assert target.booking_url == "https://www.etermin.net/qtermin-a"
    assert target.filters.is_empty


def test_defaults_table_is_applied_and_overridable():
    targets = parse(
        """
        [defaults]
        app_future = 42
        base_url = "https://example.test"

        [targets.a]
        name = "A"
        portal_id = "p-a"
        service_id = 1

        [targets.b]
        name = "B"
        portal_id = "p-b"
        service_id = 2
        app_future = 7
        """
    )
    assert targets["a"].app_future == 42
    assert targets["b"].app_future == 7
    assert targets["a"].base_url == "https://example.test"


@pytest.mark.parametrize(
    "body, message",
    [
        ('[targets.a]\nportal_id = "p"\nservice_id = 1\n', "missing required key: name"),
        ('[targets.a]\nname = "A"\nservice_id = 1\n', "missing required key: portal_id"),
        ('[targets.a]\nname = "A"\nportal_id = "p"\nservice_id = "x"\n', "must be an integer"),
        ("[defaults]\nx = 1\n", "no [targets.*] sections"),
    ],
)
def test_invalid_config_is_rejected_with_a_useful_message(body, message):
    with pytest.raises(ConfigError, match=message.replace("[", r"\[").replace("*", r"\*")):
        parse(body)


def test_unknown_filter_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown filter"):
        parse(MINIMAL + '\n[targets.a.filters]\nweekdayz = ["mon"]\n')


def test_weekday_names_and_numbers_are_both_accepted():
    filters = parse(MINIMAL + '\n[targets.a.filters]\nweekdays = ["mon", "Fr", 2]\n')["a"].filters
    assert filters.weekdays == frozenset({0, 2, 4})


def test_time_filter_parsing():
    filters = parse(
        MINIMAL + '\n[targets.a.filters]\nearliest_time = "08:30"\nlatest_time = "15:00"\n'
    )["a"].filters
    assert filters.earliest_time == time(8, 30)
    assert filters.latest_time == time(15, 0)


def test_bad_time_filter_is_rejected():
    with pytest.raises(ConfigError, match="expected HH:MM"):
        parse(MINIMAL + '\n[targets.a.filters]\nearliest_time = "half past eight"\n')


class TestSlotFilter:
    def test_empty_filter_accepts_everything(self):
        empty = SlotFilter()
        assert empty.accepts_day(date(2025, 1, 4))  # a Saturday
        assert empty.accepts_time("23:59")

    def test_weekday_filter(self):
        weekdays_only = SlotFilter(weekdays=frozenset({0, 1}))
        assert weekdays_only.accepts_day(date(2025, 1, 6))  # Monday
        assert not weekdays_only.accepts_day(date(2025, 1, 8))  # Wednesday

    def test_time_window_is_inclusive(self):
        window = SlotFilter(earliest_time=time(9, 0), latest_time=time(12, 0))
        assert window.accepts_time("09:00")
        assert window.accepts_time("12:00")
        assert not window.accepts_time("08:59")
        assert not window.accepts_time("12:01")

    def test_unparsable_time_is_kept_rather_than_silently_dropped(self):
        window = SlotFilter(earliest_time=time(9, 0))
        assert window.accepts_time("not-a-time")


def test_select_targets_defaults_to_enabled_only():
    targets = parse(
        """
        [targets.a]
        name = "A"
        portal_id = "p-a"
        service_id = 1

        [targets.b]
        name = "B"
        portal_id = "p-b"
        service_id = 2
        enabled = false
        """
    )
    assert [t.key for t in select_targets(targets, None)] == ["a"]
    assert [t.key for t in select_targets(targets, ["b"])] == ["b"]


def test_select_targets_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown target"):
        select_targets(parse(MINIMAL), ["nope"])


def test_load_targets_reports_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_targets(tmp_path / "absent.toml")


def test_repo_config_is_valid():
    """The config shipped in the repo must always load."""
    targets = load_targets("targets.toml")
    assert targets, "targets.toml should define at least one target"
    for target in targets.values():
        assert target.service_id > 0
        assert target.booking_url.startswith("https://")
