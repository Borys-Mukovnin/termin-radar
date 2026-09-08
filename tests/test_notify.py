from __future__ import annotations

from datetime import date

import pytest

from termin_alarm.notify import (
    Alert,
    ConsoleNotifier,
    EmailNotifier,
    TelegramNotifier,
    build_notifiers,
    deliver,
    render_alert,
)

MONDAY = date(2025, 5, 5)


def test_render_alert_lists_every_day_and_time(target):
    alert = render_alert(target, {MONDAY: ["09:00", "09:20"], date(2025, 5, 6): ["11:00"]})

    assert "2 Tag(en)" in alert.subject
    assert "3 Slot(s)" in alert.subject
    assert target.name in alert.subject
    for expected in ("Montag, 05.05.2025", "09:00 Uhr", "09:20 Uhr", "Dienstag, 06.05.2025"):
        assert expected in alert.text
    assert target.booking_url in alert.text


def test_render_alert_html_links_to_the_booking_page(target):
    alert = render_alert(target, {MONDAY: ["09:00"]})
    assert f'href="{target.booking_url}"' in alert.html
    assert "<table" in alert.html


def test_render_alert_escapes_html_in_the_target_name(target):
    from dataclasses import replace

    alert = render_alert(replace(target, name="<script>x</script>"), {MONDAY: ["09:00"]})
    assert "<script>" not in alert.html
    assert "&lt;script&gt;" in alert.html


# -- channel construction --------------------------------------------------


def test_email_notifier_needs_a_complete_credential_set():
    assert EmailNotifier.from_env({}) is None
    assert EmailNotifier.from_env({"SMTP_USER": "a@b.c", "SMTP_PASSWORD": "x"}) is None
    assert (
        EmailNotifier.from_env({"SMTP_USER": "a@b.c", "ALERT_TO": "d@e.f"}) is None
    )


def test_email_notifier_splits_recipients_and_applies_defaults():
    notifier = EmailNotifier.from_env(
        {"SMTP_USER": "a@b.c", "SMTP_PASSWORD": "x", "ALERT_TO": " d@e.f , g@h.i "}
    )
    assert notifier.recipients == ["d@e.f", "g@h.i"]
    assert (notifier.host, notifier.port) == ("smtp.gmail.com", 587)


def test_email_notifier_honours_a_custom_smtp_server():
    notifier = EmailNotifier.from_env(
        {
            "SMTP_USER": "a@b.c",
            "SMTP_PASSWORD": "x",
            "ALERT_TO": "d@e.f",
            "SMTP_HOST": "mail.example.org",
            "SMTP_PORT": "2525",
        }
    )
    assert (notifier.host, notifier.port) == ("mail.example.org", 2525)


def test_telegram_notifier_needs_token_and_chat():
    assert TelegramNotifier.from_env({"TELEGRAM_BOT_TOKEN": "t"}) is None
    assert TelegramNotifier.from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})


def test_build_notifiers_enables_only_configured_channels():
    assert build_notifiers({}) == []

    names = [n.name for n in build_notifiers(
        {
            "SMTP_USER": "a@b.c",
            "SMTP_PASSWORD": "x",
            "ALERT_TO": "d@e.f",
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
        }
    )]
    assert names == ["email", "telegram"]


# -- delivery --------------------------------------------------------------


class RecordingNotifier:
    name = "recording"

    def __init__(self):
        self.sent: list[Alert] = []

    def send(self, alert):
        self.sent.append(alert)


class BrokenNotifier:
    name = "broken"

    def send(self, alert):
        raise RuntimeError("SMTP is down")


def test_deliver_counts_successful_channels():
    good = RecordingNotifier()
    alert = Alert("s", "t", "<p>h</p>")
    assert deliver([good, good], alert) == 2
    assert len(good.sent) == 2


def test_one_broken_channel_does_not_silence_the_others(caplog):
    good = RecordingNotifier()
    assert deliver([BrokenNotifier(), good], Alert("s", "t", "h")) == 1
    assert good.sent, "the working channel must still receive the alert"
    assert "broken notifier failed" in caplog.text


def test_console_notifier_prints_the_alert(capsys):
    ConsoleNotifier().send(Alert("Subject line", "Body line", "<p>ignored</p>"))
    printed = capsys.readouterr().out
    assert "Subject line" in printed
    assert "Body line" in printed


@pytest.mark.parametrize("channel", [EmailNotifier, TelegramNotifier])
def test_channels_satisfy_the_notifier_protocol(channel):
    assert callable(channel.send)
    assert isinstance(channel.name, str)
