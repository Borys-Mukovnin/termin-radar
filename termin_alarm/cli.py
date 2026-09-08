"""Command line entry point: ``python -m termin_alarm``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from termin_alarm import __version__
from termin_alarm.config import ConfigError, Target, load_targets, select_targets
from termin_alarm.etermin import POLITE_DELAY, ETerminClient, Slot, group_by_day
from termin_alarm.notify import (
    ConsoleNotifier,
    Notifier,
    build_notifiers,
    deliver,
    render_alert,
)
from termin_alarm.state import DEFAULT_STATE_PATH, SeenSlots

log = logging.getLogger("termin_alarm")

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="termin-alarm",
        description=(
            "Watch German public-authority booking portals and shout when an "
            "appointment slot opens up."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m termin_alarm --list\n"
            "  python -m termin_alarm --target fahrerlaubnis --dry-run\n"
            "  python -m termin_alarm --config targets.toml\n"
        ),
    )
    parser.add_argument(
        "-c", "--config", default="targets.toml", type=Path,
        help="path to the targets file (default: targets.toml)",
    )
    parser.add_argument(
        "-t", "--target", action="append", dest="targets", metavar="KEY",
        help="only check this target; repeatable (default: all enabled ones)",
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


def write_job_summary(rows: Sequence[tuple[Target, list[Slot], int]]) -> None:
    """Render a table into the GitHub Actions run summary, when running there."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Termin Alarm",
        "",
        f"_Checked {datetime.now().strftime('%Y-%m-%d %H:%M')} "
        f"({len(rows)} target(s))_",
        "",
        "| Target | Free slots | New since last run | Portal |",
        "| --- | ---: | ---: | --- |",
    ]
    for target, slots, new_count in rows:
        lines.append(
            f"| {target.name} | {len(slots)} | {new_count} | "
            f"[booking page]({target.booking_url}) |"
        )
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.debug("could not write job summary: %s", exc)


def check_target(
    target: Target,
    notifiers: Sequence[Notifier],
    seen: SeenSlots | None,
    delay: float,
    today: date | None = None,
) -> tuple[list[Slot], int]:
    """Scan one target and alert on whatever is new. Returns (all, new-count)."""
    slots = ETerminClient(target, delay=delay).find_slots(today=today)
    if not slots:
        log.info("%s: nothing free right now", target.key)
        return [], 0

    fresh = seen.new_slots(target.key, slots) if seen else list(slots)
    if not fresh:
        log.info(
            "%s: %d free slot(s), all previously reported", target.key, len(slots)
        )
        return slots, 0

    log.info("%s: %d new slot(s) -> alerting", target.key, len(fresh))
    alert = render_alert(target, group_by_day(fresh))
    if deliver(notifiers, alert) and seen is not None:
        seen.remember(target.key, fresh)
    return slots, len(fresh)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    try:
        configured = load_targets(args.config)
        if args.list_targets:
            return list_targets(configured)
        selected = select_targets(configured, args.targets)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_ERROR

    if not selected:
        log.warning("no enabled targets in %s - nothing to do", args.config)
        return EXIT_OK

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

    seen = None if (args.dry_run or args.force) else SeenSlots(args.state)
    delay = 0.0 if args.no_delay else POLITE_DELAY

    results: list[tuple[Target, list[Slot], int]] = []
    failures = 0
    for target in selected:
        try:
            slots, new_count = check_target(target, notifiers, seen, delay)
            results.append((target, slots, new_count))
        except Exception as exc:  # keep checking the other targets
            failures += 1
            log.error("%s: check failed: %s", target.key, exc, exc_info=args.verbose)

    if seen is not None:
        seen.save()
    write_job_summary(results)

    total_new = sum(new for _, _, new in results)
    total_free = sum(len(slots) for _, slots, _ in results)
    log.info(
        "done: %d free slot(s) across %d target(s), %d newly reported",
        total_free, len(results), total_new,
    )
    return EXIT_ERROR if failures and not results else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
