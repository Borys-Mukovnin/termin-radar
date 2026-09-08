"""Remembers which slots have already been reported.

Without this, a watcher that runs every 15 minutes mails you the same three
appointments 96 times a day and you stop reading the mails, which defeats the
whole point. The store keeps one JSON file mapping each target to the slots it
has already alerted on, and forgets entries once their date has passed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from termin_alarm.etermin import Slot

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(".termin-alarm-state.json")


def _slot_id(slot: Slot) -> str:
    return f"{slot.day.isoformat()}T{slot.start}"


class SeenSlots:
    """A tiny JSON-backed set of already-announced slots."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return
        if isinstance(raw, dict):
            self._data = {
                str(key): [str(v) for v in value]
                for key, value in raw.items()
                if isinstance(value, list)
            }

    def new_slots(self, target_key: str, slots: Iterable[Slot]) -> list[Slot]:
        """Return only the slots not yet announced for this target."""
        known = set(self._data.get(target_key, ()))
        return [slot for slot in slots if _slot_id(slot) not in known]

    def remember(self, target_key: str, slots: Sequence[Slot]) -> None:
        """Mark slots as announced (call this only after a successful send)."""
        known = set(self._data.get(target_key, ()))
        known.update(_slot_id(slot) for slot in slots)
        self._data[target_key] = sorted(known)

    def prune(self, today: date | None = None) -> None:
        """Drop remembered slots whose date has already passed."""
        cutoff = (today or date.today()).isoformat()
        for key, ids in list(self._data.items()):
            kept = [i for i in ids if i[:10] >= cutoff]
            if kept:
                self._data[key] = kept
            else:
                del self._data[key]

    def save(self) -> None:
        self.prune()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write state file %s: %s", self.path, exc)
