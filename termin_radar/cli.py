"""Command line entry point: ``python -m termin_radar``."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from termin_radar import __version__
from termin_radar.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    Target,
    load_targets,
    load_targets_text,
    select_targets,
)
from termin_radar.etermin import POLITE_DELAY, ETerminClient, Slot, group_by_day
from termin_radar.notify import (
    ConsoleNotifier,
    Notifier,
    build_notifiers,
    deliver,
    render_alert,
)
from termin_radar.state import DEFAULT_STATE_PATH, SeenSlots

log = logging.getLogger("termin_radar")

EXIT_OK = 0
EXIT_ERROR = 1

# The portals and the people booking them run on German time; Actions runs on UTC.
SUMMARY_TIMEZONE = "Europe/Berlin"

# Let a deployment change what is watched without a commit (on GitHub Actions:
# repository variables). Command-line flags still win over both.
CONFIG_ENV = "TERMIN_CONFIG"    # full targets.toml contents, used instead of the file
TARGETS_ENV = "TERMIN_TARGETS"  # comma-separated target keys, like repeated --target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="termin-radar",
        description=(
            "Watch German public-authority booking portals and shout when an "
            "appointment slot opens up."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m termin_radar --list\n"
            "  python -m termin_radar --target fahrerlaubnis --dry-run\n"
            "  python -m termin_radar --config targets.toml\n"
        ),
    )
    parser.add_argument(
        "-c", "--config", type=Path,
        help=f"path to the targets file (default: ${CONFIG_ENV} if set, else targets.toml)",
    )
    parser.add_argument(
        "-t", "--target", action="append", dest="targets", metavar="KEY",
        help=(
            f"only check this target; repeatable "
            f"(default: the keys in ${TARGETS_ENV} if set, else all enabled ones)"
        ),
    )
    parser.add_argument(
        "-l", "--list", action="store_true", dest="list_targets",
        help="list the configured targets and exit",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="print alerts instead of sending them, and do not touch the state file",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="alert on every free slot, including ones already reported",
    )
    parser.add_argument(
        "--state", default=DEFAULT_STATE_PATH, type=Path,
        help=f"path to the seen-slots file (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--no-delay", action="store_true",
        help="skip the polite pauses between requests (for local debugging)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def list_targets(targets: dict[str, Target]) -> int:
    width = max((len(k) for k in targets), default=3)
    print(f"{'KEY'.ljust(width)}  STATUS    NAME")
    for key, target in targets.items():
        status = "enabled" if target.enabled else "disabled"
        print(f"{key.ljust(width)}  {status:<9} {target.name}")
        print(f"{' ' * width}  {'':<9} {target.booking_url}")
        if not target.filters.is_empty:
            filters = target.filters
            bits = []
            if filters.weekdays:
                names = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
                bits.append("days: " + ",".join(names[d] for d in sorted(filters.weekdays)))
            if filters.earliest_time:
                bits.append(f"from {filters.earliest_time.strftime('%H:%M')}")
            if filters.latest_time:
                bits.append(f"until {filters.latest_time.strftime('%H:%M')}")
            print(f"{' ' * width}  {'':<9} filters: {'; '.join(bits)}")
    return EXIT_OK


def _summary_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(SUMMARY_TIMEZONE))
    except ZoneInfoNotFoundError:  # e.g. Windows without the tzdata package
        return datetime.now().astimezone()


def write_job_summary(rows: Sequence[tuple[Target, list[Slot] | None, int]]) -> None:
    """Render a table into the GitHub Actions run summary, when running there.

    ``slots`` is ``None`` for a target whose check failed.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Termin Radar",
        "",
        f"_Checked {_summary_now().strftime('%Y-%m-%d %H:%M %Z')} "
        f"({len(rows)} target(s))_",
        "",
        "| Target | Free slots | New since last run | Portal |",
        "| --- | ---: | ---: | --- |",
    ]
    for target, slots, new_count in rows:
        free, new = ("**check failed**", "-") if slots is None else (len(slots), new_count)
        lines.append(
            f"| {target.name} | {free} | {new} | "
            f"[booking page]({target.booking_url}) |"
        )
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.debug("could not write job summary: %s", exc)


def load_config(path: Path | None) -> tuple[dict[str, Target], str]:
    """Targets from ``--config``, else ``$TERMIN_CONFIG``, else targets.toml.

    Returns the targets and a description of where they came from.
    """
    if path is None:
        text = os.environ.get(CONFIG_ENV, "").strip()
        if text:
            source = f"${CONFIG_ENV}"
            return load_targets_text(text, source), source
        path = DEFAULT_CONFIG_PATH
    return load_targets(path), str(path)


def requested_keys(cli_keys: list[str] | None) -> list[str] | None:
    """Target keys from ``--target``, else ``$TERMIN_TARGETS``, else ``None`` (all)."""
    if cli_keys:
        return cli_keys
    keys = [k for k in re.split(r"[\s,]+", os.environ.get(TARGETS_ENV, "")) if k]
    return keys or None


def state_key(target: Target) -> str:
    """Key reported slots by what is watched, not just by the target's name.

    Pointing an existing key at another office or service must not let the old
    service's reported slots suppress alerts for the new one.
    """
    return f"{target.key}:{target.portal_id}:{target.service_id}"


def check_target(
    target: Target,
    notifiers: Sequence[Notifier],
    seen: SeenSlots | None,
    delay: float,
    today: date | None = None,
    force: bool = False,
) -> tuple[list[Slot], int]:
    """Scan one target and alert on whatever is new. Returns (all, new-count).

    With ``force`` every free slot counts as new, but is still remembered, so
    the next ordinary run does not announce it yet again.
    """
    slots = ETerminClient(target, delay=delay).find_slots(today=today)
    if not slots:
        log.info("%s: nothing free right now", target.key)
        return [], 0

    fresh = list(slots) if force or seen is None else seen.new_slots(state_key(target), slots)
    if not fresh:
        log.info(
            "%s: %d free slot(s), all previously reported", target.key, len(slots)
        )
        return slots, 0

    log.info("%s: %d new slot(s) -> alerting", target.key, len(fresh))
    alert = render_alert(target, group_by_day(fresh))
    if deliver(notifiers, alert) and seen is not None:
        seen.remember(state_key(target), fresh)
    return slots, len(fresh)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    try:
        configured, source = load_config(args.config)
        if args.list_targets:
            return list_targets(configured)
        selected = select_targets(configured, requested_keys(args.targets))
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_ERROR

    if not selected:
        log.warning("no enabled targets in %s - nothing to do", source)
        return EXIT_OK
    log.info("checking %s (from %s)", ", ".join(t.key for t in selected), source)

    notifiers: list[Notifier]
    if args.dry_run:
        notifiers = [ConsoleNotifier()]
    else:
        notifiers = build_notifiers()
        if not notifiers:
            log.error(
                "no notification channel configured. Set SMTP_USER, SMTP_PASSWORD "
                "and ALERT_TO for e-mail, and/or TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID for Telegram (or use --dry-run)."
            )
            return EXIT_ERROR
    log.info("notifying via: %s", ", ".join(n.name for n in notifiers))

    seen = None if args.dry_run else SeenSlots(args.state)
    delay = 0.0 if args.no_delay else POLITE_DELAY

    results: list[tuple[Target, list[Slot] | None, int]] = []
    failures = 0
    for target in selected:
        try:
            slots, new_count = check_target(target, notifiers, seen, delay, force=args.force)
        except Exception as exc:  # keep checking the other targets
            failures += 1
            log.error("%s: check failed: %s", target.key, exc, exc_info=args.verbose)
            results.append((target, None, 0))
            continue
        results.append((target, slots, new_count))

    if seen is not None:
        seen.save()
    write_job_summary(results)

    total_new = sum(new for _, _, new in results)
    total_free = sum(len(slots) for _, slots, _ in results if slots is not None)
    log.info(
        "done: %d free slot(s) across %d target(s), %d newly reported, %d failed",
        total_free, len(results) - failures, total_new, failures,
    )
    # A target that could not be checked is not being watched; fail the run so
    # that shows up instead of passing for an empty calendar.
    return EXIT_ERROR if failures else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
