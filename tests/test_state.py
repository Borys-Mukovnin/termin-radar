from __future__ import annotations

from datetime import date, timedelta

from termin_alarm.etermin import Slot
from termin_alarm.state import SeenSlots

# Relative to today, because saving prunes anything already in the past.
SOON = date.today() + timedelta(days=7)
LATER = SOON + timedelta(days=1)
PAST = date.today() - timedelta(days=3)


def test_everything_is_new_on_a_fresh_store(tmp_path):
    seen = SeenSlots(tmp_path / "state.json")
    slots = [Slot(SOON, "09:00"), Slot(SOON, "09:20")]
    assert seen.new_slots("demo", slots) == slots


def test_remembered_slots_are_not_reported_twice(tmp_path):
    seen = SeenSlots(tmp_path / "state.json")
    seen.remember("demo", [Slot(SOON, "09:00")])

    fresh = seen.new_slots("demo", [Slot(SOON, "09:00"), Slot(SOON, "09:20")])
    assert fresh == [Slot(SOON, "09:20")]


def test_state_is_kept_per_target(tmp_path):
    seen = SeenSlots(tmp_path / "state.json")
    seen.remember("a", [Slot(SOON, "09:00")])
    assert seen.new_slots("b", [Slot(SOON, "09:00")]) == [Slot(SOON, "09:00")]


def test_state_survives_a_round_trip(tmp_path):
    path = tmp_path / "state.json"
    first = SeenSlots(path)
    first.remember("demo", [Slot(LATER, "11:00")])
    first.save()

    second = SeenSlots(path)
    assert second.new_slots("demo", [Slot(LATER, "11:00")]) == []


def test_prune_forgets_slots_that_are_in_the_past(tmp_path):
    seen = SeenSlots(tmp_path / "state.json")
    seen.remember("demo", [Slot(PAST, "09:00"), Slot(SOON, "09:00")])

    seen.prune()

    assert seen.new_slots("demo", [Slot(PAST, "09:00")]) == [Slot(PAST, "09:00")]
    assert seen.new_slots("demo", [Slot(SOON, "09:00")]) == []


def test_saving_drops_stale_entries_so_the_file_cannot_grow_forever(tmp_path):
    path = tmp_path / "state.json"
    seen = SeenSlots(path)
    seen.remember("demo", [Slot(PAST, "09:00"), Slot(SOON, "09:00")])
    seen.save()

    assert str(PAST) not in path.read_text(encoding="utf-8")
    assert str(SOON) in path.read_text(encoding="utf-8")


def test_a_corrupt_state_file_is_ignored_rather_than_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    seen = SeenSlots(path)
    assert seen.new_slots("demo", [Slot(SOON, "09:00")]) == [Slot(SOON, "09:00")]

    seen.remember("demo", [Slot(SOON, "09:00")])
    seen.save()
    assert SeenSlots(path).new_slots("demo", [Slot(SOON, "09:00")]) == []
