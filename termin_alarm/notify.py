"""Where alerts go: e-mail, Telegram, or nowhere at all.

Every channel is optional and configured purely through environment variables,
so a fork can enable Telegram without touching the code and CI can run with no
credentials at all.
"""

from __future__ import annotations

import logging
import os
import smtplib
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from html import escape
from typing import Protocol

from termin_alarm.config import Target

log = logging.getLogger(__name__)

WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
               "Samstag", "Sonntag")


@dataclass(frozen=True)
class Alert:
    """A rendered message, ready for any channel."""

    subject: str
    text: str
    html: str


def _format_day(day: date) -> str:
    return f"{WEEKDAYS_DE[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


def render_alert(target: Target, slots_by_day: Mapping[date, Sequence[str]]) -> Alert:
    """Build the alert message for one target."""
    day_count = len(slots_by_day)
    slot_count = sum(len(times) for times in slots_by_day.values())
    subject = (
        f"Termin frei: {target.name} "
        f"({slot_count} Slot(s) an {day_count} Tag(en))"
    )

    text_lines = [f"Freie Termine: {target.name}", ""]
    html_rows = []
    for day, times in sorted(slots_by_day.items()):
        joined = "   ".join(f"{t} Uhr" for t in times)
        text_lines.append(f"  {_format_day(day)}")
        text_lines.append(f"    {joined}")
        html_rows.append(
            "<tr>"
            f'<td style="padding:6px 16px 6px 0;white-space:nowrap;">'
            f"<strong>{escape(_format_day(day))}</strong></td>"
            f'<td style="padding:6px 0;">{escape(joined)}</td>'
            "</tr>"
        )
    text_lines += ["", f"Jetzt buchen: {target.booking_url}"]
    if target.info_url:
        text_lines.append(f"Infoseite:    {target.info_url}")

    info_link = (
        f'<p style="margin:8px 0 0;font-size:13px;">'
        f'<a href="{escape(target.info_url)}">Infoseite der Behörde</a></p>'
        if target.info_url
        else ""
    )
    html = (
        '<div style="font-family:system-ui,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;color:#111;">'
        f"<h2 style=\"margin:0 0 4px;\">Freie Termine gefunden</h2>"
        f'<p style="margin:0 0 16px;color:#555;">{escape(target.name)}</p>'
        f'<table style="border-collapse:collapse;">{"".join(html_rows)}</table>'
        f'<p style="margin:20px 0 0;">'
        f'<a href="{escape(target.booking_url)}" '
        'style="background:#0b5fff;color:#fff;padding:10px 18px;border-radius:6px;'
        'text-decoration:none;display:inline-block;">Termin buchen</a></p>'
        f"{info_link}"
        "</div>"
    )
    return Alert(subject=subject, text="\n".join(text_lines), html=html)


class Notifier(Protocol):
    """Anything that can deliver an :class:`Alert`."""

    name: str

    def send(self, alert: Alert) -> None: ...


class EmailNotifier:
    """Sends alerts over SMTP (tested with Gmail app passwords)."""

    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        recipients: Sequence[str],
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.recipients = list(recipients)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> EmailNotifier | None:
        user = env.get("SMTP_USER", "").strip()
        password = env.get("SMTP_PASSWORD", "").strip()
        recipients = [a.strip() for a in env.get("ALERT_TO", "").split(",") if a.strip()]
        if not (user and password and recipients):
            return None
        return cls(
            host=env.get("SMTP_HOST", "smtp.gmail.com").strip(),
            port=int(env.get("SMTP_PORT", "587")),
            user=user,
            password=password,
            recipients=recipients,
        )

    def send(self, alert: Alert) -> None:
        message = EmailMessage()
        message["From"] = self.user
        message["To"] = ", ".join(self.recipients)
        message["Subject"] = alert.subject
        message.set_content(alert.text)
        message.add_alternative(alert.html, subtype="html")

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(self.user, self.password)
            smtp.send_message(message)
        log.info("e-mail sent to %s", ", ".join(self.recipients))


class TelegramNotifier:
    """Pushes alerts to a Telegram chat via a bot token.

    Handy because a phone notification arrives in seconds, while mail apps
    often sync on a schedule and these slots disappear within minutes.
    """

    name = "telegram"
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TelegramNotifier | None:
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
        if not (token and chat_id):
            return None
        return cls(token, chat_id)

    def send(self, alert: Alert) -> None:
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": f"*{alert.subject}*\n\n{alert.text}",
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.API}/bot{self.token}/sendMessage", data=payload
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        log.info("telegram message sent to chat %s", self.chat_id)


class ConsoleNotifier:
    """Fallback channel: print the alert. Used by --dry-run and by CI."""

    name = "console"

    def send(self, alert: Alert) -> None:
        print("\n" + "=" * 64)
        print(alert.subject)
        print("=" * 64)
        print(alert.text + "\n")


def build_notifiers(env: Mapping[str, str] | None = None) -> list[Notifier]:
    """Return every channel that has complete credentials in the environment."""
    env = os.environ if env is None else env
    notifiers: list[Notifier] = []
    for factory in (EmailNotifier.from_env, TelegramNotifier.from_env):
        notifier = factory(env)
        if notifier is not None:
            notifiers.append(notifier)
    return notifiers


def deliver(notifiers: Sequence[Notifier], alert: Alert) -> int:
    """Send one alert through every channel. Returns the number that worked."""
    delivered = 0
    for notifier in notifiers:
        try:
            notifier.send(alert)
            delivered += 1
        except Exception as exc:  # one broken channel must not silence the rest
            log.error("%s notifier failed: %s", notifier.name, exc)
    return delivered
