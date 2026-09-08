"""Loading and validation of ``targets.toml``.

A *target* is one bookable service on one eTermin portal — e.g. "residence
permit, Duisburg immigration office south". Everything the scraper needs to
query that service lives in the config file, so adding a new office means
adding six lines of TOML instead of copying a script.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time as _time
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("targets.toml")

# Query parameters the eTermin API expects but that virtually never change.
DEFAULT_DURATION = 0
DEFAULT_APP_FUTURE = 30
DEFAULT_APP_DEADLINE = 20

_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "mo": 0,
    "tue": 1, "tuesday": 1, "di": 1,
    "wed": 2, "wednesday": 2, "mi": 2,
    "thu": 3, "thursday": 3, "do": 3,
    "fri": 4, "friday": 4, "fr": 4,
    "sat": 5, "saturday": 5, "sa": 5,
    "sun": 6, "sunday": 6, "so": 6,
}


class ConfigError(ValueError):
    """Raised when ``targets.toml`` is missing, malformed or incomplete."""


@dataclass(frozen=True)
class SlotFilter:
    """Optional narrowing of which free slots are worth an alert.

    An empty filter (the default) accepts everything the portal offers.
    """

    weekdays: frozenset[int] = frozenset()
    earliest_time: _time | None = None
    latest_time: _time | None = None

    def accepts_day(self, day) -> bool:
        return not self.weekdays or day.weekday() in self.weekdays

    def accepts_time(self, hhmm: str) -> bool:
        if self.earliest_time is None and self.latest_time is None:
            return True
        try:
            moment = _time.fromisoformat(hhmm)
        except ValueError:
            return True  # never drop a slot just because we cannot parse it
        if self.earliest_time and moment < self.earliest_time:
            return False
        return not (self.latest_time and moment > self.latest_time)

    @property
    def is_empty(self) -> bool:
        return not self.weekdays and not self.earliest_time and not self.latest_time


@dataclass(frozen=True)
class Target:
    """One watched service on one eTermin portal."""

    key: str
    name: str
    portal_id: str
    service_id: int
    enabled: bool = True
    account_id: str | None = None
    calendar_id: str = ""
    duration: int = DEFAULT_DURATION
    app_future: int = DEFAULT_APP_FUTURE
    app_deadline: int = DEFAULT_APP_DEADLINE
    base_url: str = "https://www.etermin.net"
    info_url: str | None = None
    filters: SlotFilter = field(default_factory=SlotFilter)

    @property
    def booking_url(self) -> str:
        return f"{self.base_url}/{self.portal_id}"


def _parse_weekdays(raw: Any, key: str) -> frozenset[int]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ConfigError(f"[{key}] filters.weekdays must be a list, got {type(raw).__name__}")
    days: set[int] = set()
    for item in raw:
        if isinstance(item, int):
            if not 0 <= item <= 6:
                raise ConfigError(f"[{key}] weekday {item} out of range 0-6")
            days.add(item)
        elif isinstance(item, str) and item.strip().lower() in _WEEKDAY_NAMES:
            days.add(_WEEKDAY_NAMES[item.strip().lower()])
        else:
            raise ConfigError(f"[{key}] unknown weekday {item!r}")
    return frozenset(days)


def _parse_time(raw: Any, key: str, field_name: str) -> _time | None:
    if raw is None:
        return None
    if isinstance(raw, _time):
        return raw
    try:
        return _time.fromisoformat(str(raw))
    except ValueError as exc:
        raise ConfigError(f"[{key}] filters.{field_name}: expected HH:MM, got {raw!r}") from exc


def _parse_filters(raw: Any, key: str) -> SlotFilter:
    if raw is None:
        return SlotFilter()
    if not isinstance(raw, dict):
        raise ConfigError(f"[{key}] filters must be a table")
    unknown = set(raw) - {"weekdays", "earliest_time", "latest_time"}
    if unknown:
        raise ConfigError(f"[{key}] unknown filter key(s): {', '.join(sorted(unknown))}")
    return SlotFilter(
        weekdays=_parse_weekdays(raw.get("weekdays"), key),
        earliest_time=_parse_time(raw.get("earliest_time"), key, "earliest_time"),
        latest_time=_parse_time(raw.get("latest_time"), key, "latest_time"),
    )


def _build_target(key: str, raw: dict[str, Any], defaults: dict[str, Any]) -> Target:
    merged: dict[str, Any] = {**defaults, **raw}

    for required in ("name", "portal_id", "service_id"):
        if not merged.get(required):
            raise ConfigError(f"[{key}] missing required key: {required}")

    try:
        service_id = int(merged["service_id"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{key}] service_id must be an integer") from exc

    account_id = merged.get("account_id")
    return Target(
        key=key,
        name=str(merged["name"]),
        portal_id=str(merged["portal_id"]),
        service_id=service_id,
        enabled=bool(merged.get("enabled", True)),
        account_id=str(account_id) if account_id not in (None, "") else None,
        calendar_id=str(merged.get("calendar_id", "")),
        duration=int(merged.get("duration", DEFAULT_DURATION)),
        app_future=int(merged.get("app_future", DEFAULT_APP_FUTURE)),
        app_deadline=int(merged.get("app_deadline", DEFAULT_APP_DEADLINE)),
        base_url=str(merged.get("base_url", "https://www.etermin.net")).rstrip("/"),
        info_url=merged.get("info_url") or None,
        filters=_parse_filters(raw.get("filters"), key),
    )


def parse_targets(document: dict[str, Any]) -> dict[str, Target]:
    """Turn a parsed TOML document into ``{key: Target}``."""
    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("[defaults] must be a table")

    raw_targets = document.get("targets")
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise ConfigError("config contains no [targets.*] sections")

    targets: dict[str, Target] = {}
    for key, raw in raw_targets.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"[targets.{key}] must be a table")
        targets[key] = _build_target(key, raw, defaults)
    return targets


def load_targets(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Target]:
    """Read and validate a targets file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path}\n"
            "Point --config at a targets file; see targets.toml in this repo."
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML — {exc}") from exc
    return parse_targets(document)


def select_targets(targets: dict[str, Target], keys: list[str] | None) -> list[Target]:
    """Pick the targets to run: the named ones, or every enabled one."""
    if keys:
        missing = [k for k in keys if k not in targets]
        if missing:
            known = ", ".join(sorted(targets)) or "none"
            raise ConfigError(f"unknown target(s): {', '.join(missing)}. Known: {known}")
        return [targets[k] for k in keys]
    return [t for t in targets.values() if t.enabled]
