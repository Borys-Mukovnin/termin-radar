from __future__ import annotations

from datetime import date, timedelta

import pytest

from termin_radar import cli
from termin_radar.etermin import Slot

CONFIG = """
[targets.demo]
name = "Demo Service"
portal_id = "qtermin-demo"
service_id = 12345

[targets.off]
name = "Disabled Service"
portal_id = "qtermin-off"
service_id = 999
enabled = false
"""

SOON = date.today() + timedelta(days=7)


@pytest.fixture(autouse=True)
def no_config_from_environment(monkeypatch):
    """Keep a developer's own TERMIN_* variables out of the tests."""
    monkeypatch.delenv(cli.CONFIG_ENV, raising=False)
    monkeypatch.delenv(cli.TARGETS_ENV, raising=False)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "targets.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def found_slots(monkeypatch):
    """Make the scraper return a fixed answer instead of hitting the network."""
    calls: list[str] = []
    slots = [Slot(SOON, "09:00"), Slot(SOON, "09:20")]

    def fake_find_slots(self, today=None):
        calls.append(self.target.key)
        return list(slots)

    monkeypatch.setattr(cli.ETerminClient, "find_slots", fake_find_slots)
    return calls


def test_list_shows_targets_and_their_status(config_file, capsys):
    assert cli.main(["--config", str(config_file), "--list"]) == cli.EXIT_OK

    printed = capsys.readouterr().out
    assert "demo" in printed and "enabled" in printed
    assert "off" in printed and "disabled" in printed
    assert "https://www.etermin.net/qtermin-demo" in printed


def test_dry_run_prints_the_alert_and_skips_disabled_targets(
    config_file, found_slots, tmp_path, capsys
):
    exit_code = cli.main(
        ["--config", str(config_file), "--dry-run", "--no-delay",
         "--state", str(tmp_path / "state.json")]
    )

    assert exit_code == cli.EXIT_OK
    assert found_slots == ["demo"], "only enabled targets should be checked"
    assert "Demo Service" in capsys.readouterr().out
    assert not (tmp_path / "state.json").exists(), "--dry-run must not write state"


def test_explicit_target_selection_includes_disabled_ones(config_file, found_slots, capsys):
    cli.main(["--config", str(config_file), "--dry-run", "--no-delay", "--target", "off"])
    assert found_slots == ["off"]


def test_unknown_target_is_a_clean_error(config_file, caplog):
    assert cli.main(["--config", str(config_file), "--target", "nope"]) == cli.EXIT_ERROR
    assert "unknown target" in caplog.text


def test_missing_config_is_a_clean_error(tmp_path, caplog):
    assert cli.main(["--config", str(tmp_path / "nope.toml")]) == cli.EXIT_ERROR
    assert "not found" in caplog.text


def test_run_without_any_notification_channel_fails_fast(config_file, monkeypatch, caplog):
    monkeypatch.setattr(cli, "build_notifiers", lambda: [])
    assert cli.main(["--config", str(config_file), "--no-delay"]) == cli.EXIT_ERROR
    assert "no notification channel configured" in caplog.text


def test_repeat_runs_do_not_re_alert_on_the_same_slots(
    config_file, found_slots, tmp_path, monkeypatch, capsys
):
    sent = []

    class Recorder:
        name = "recorder"

        def send(self, alert):
            sent.append(alert)

    monkeypatch.setattr(cli, "build_notifiers", lambda: [Recorder()])
    args = ["--config", str(config_file), "--no-delay", "--state", str(tmp_path / "state.json")]

    cli.main(args)
    cli.main(args)

    assert len(sent) == 1, "the second run found the same slots and must stay quiet"
    assert found_slots == ["demo", "demo"], "but it must still have checked the portal"


def test_force_re_alerts_on_known_slots(config_file, found_slots, tmp_path, monkeypatch):
    sent = []

    class Recorder:
        name = "recorder"

        def send(self, alert):
            sent.append(alert)

    monkeypatch.setattr(cli, "build_notifiers", lambda: [Recorder()])
    base = ["--config", str(config_file), "--no-delay", "--state", str(tmp_path / "state.json")]

    cli.main(base)
    cli.main([*base, "--force"])

    assert len(sent) == 2


def test_slots_announced_by_force_are_not_announced_again(
    config_file, found_slots, tmp_path, monkeypatch
):
    sent = []

    class Recorder:
        name = "recorder"

        def send(self, alert):
            sent.append(alert)

    monkeypatch.setattr(cli, "build_notifiers", lambda: [Recorder()])
    base = ["--config", str(config_file), "--no-delay", "--state", str(tmp_path / "state.json")]

    cli.main([*base, "--force"])
    cli.main(base)

    assert len(sent) == 1, "the forced run must still record what it announced"


def test_state_is_not_updated_when_every_channel_fails(
    config_file, found_slots, tmp_path, monkeypatch
):
    class Broken:
        name = "broken"

        def send(self, alert):
            raise RuntimeError("nope")

    monkeypatch.setattr(cli, "build_notifiers", lambda: [Broken()])
    args = ["--config", str(config_file), "--no-delay", "--state", str(tmp_path / "state.json")]

    cli.main(args)
    state = cli.SeenSlots(tmp_path / "state.json")
    assert state.new_slots("demo", [Slot(SOON, "09:00")]), (
        "an undelivered alert must be retried on the next run"
    )


def test_a_failing_target_does_not_abort_the_rest(config_file, tmp_path, monkeypatch, caplog):
    def explode(self, today=None):
        raise RuntimeError("portal on fire")

    monkeypatch.setattr(cli.ETerminClient, "find_slots", explode)
    monkeypatch.setattr(cli, "build_notifiers", lambda: [cli.ConsoleNotifier()])

    exit_code = cli.main(
        ["--config", str(config_file), "--no-delay", "--state", str(tmp_path / "state.json")]
    )
    assert exit_code == cli.EXIT_ERROR
    assert "portal on fire" in caplog.text


def test_one_failing_target_fails_the_run_but_the_others_still_alert(
    config_file, tmp_path, monkeypatch, capsys
):
    def find_slots(self, today=None):
        if self.target.key == "demo":
            raise RuntimeError("portal on fire")
        return [Slot(SOON, "09:00")]

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cli.ETerminClient, "find_slots", find_slots)

    exit_code = cli.main(
        ["--config", str(config_file), "--dry-run", "--no-delay",
         "--target", "demo", "--target", "off"]
    )

    assert exit_code == cli.EXIT_ERROR, "a target that was not checked must not pass silently"
    assert "Disabled Service" in capsys.readouterr().out, "the healthy target still alerts"
    written = summary.read_text(encoding="utf-8")
    assert "| Demo Service | **check failed** |" in written
    assert "| Disabled Service | 1 | 1 |" in written


def test_job_summary_is_written_when_running_in_github_actions(
    config_file, found_slots, tmp_path, monkeypatch
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    cli.main(["--config", str(config_file), "--dry-run", "--no-delay"])

    written = summary.read_text(encoding="utf-8")
    assert "## Termin Radar" in written
    assert "Demo Service" in written


# -- configuration from the environment (repository variables on Actions) ----


def test_target_keys_can_come_from_the_environment(config_file, found_slots, monkeypatch):
    monkeypatch.setenv(cli.TARGETS_ENV, " off , demo ")

    cli.main(["--config", str(config_file), "--dry-run", "--no-delay"])

    assert found_slots == ["off", "demo"], "like --target, it may name disabled targets"


def test_command_line_targets_win_over_the_environment(config_file, found_slots, monkeypatch):
    monkeypatch.setenv(cli.TARGETS_ENV, "off")

    cli.main(["--config", str(config_file), "--dry-run", "--no-delay", "--target", "demo"])

    assert found_slots == ["demo"]


def test_unknown_target_key_in_the_environment_is_a_clean_error(
    config_file, monkeypatch, caplog
):
    monkeypatch.setenv(cli.TARGETS_ENV, "nope")
    assert cli.main(["--config", str(config_file), "--dry-run"]) == cli.EXIT_ERROR
    assert "unknown target(s): nope" in caplog.text


def test_whole_config_can_come_from_the_environment(found_slots, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no targets.toml here, so the variable is all there is
    monkeypatch.setenv(
        cli.CONFIG_ENV,
        '[targets.env_only]\nname = "From Variable"\nportal_id = "p"\nservice_id = 7\n',
    )

    assert cli.main(["--dry-run", "--no-delay"]) == cli.EXIT_OK
    assert found_slots == ["env_only"]
    assert "From Variable" in capsys.readouterr().out


def test_config_file_flag_wins_over_the_environment(config_file, found_slots, monkeypatch):
    monkeypatch.setenv(
        cli.CONFIG_ENV, '[targets.env_only]\nname = "X"\nportal_id = "p"\nservice_id = 7\n'
    )

    cli.main(["--config", str(config_file), "--dry-run", "--no-delay"])

    assert found_slots == ["demo"]


def test_broken_config_in_the_environment_names_the_variable(monkeypatch, caplog):
    monkeypatch.setenv(cli.CONFIG_ENV, "[targets.a\nname = ")
    assert cli.main(["--dry-run"]) == cli.EXIT_ERROR
    assert "$TERMIN_CONFIG: invalid TOML" in caplog.text


def test_pointing_a_target_at_another_service_alerts_again(found_slots, tmp_path, monkeypatch):
    """Same key, different service: old reports must not hide the new service's slots."""
    sent = []

    class Recorder:
        name = "recorder"

        def send(self, alert):
            sent.append(alert)

    monkeypatch.setattr(cli, "build_notifiers", lambda: [Recorder()])
    state = ["--no-delay", "--state", str(tmp_path / "state.json")]
    config = '[targets.fs]\nname = "FS"\nportal_id = "p"\nservice_id = {}\n'

    monkeypatch.setenv(cli.CONFIG_ENV, config.format(1))
    cli.main(state)
    cli.main(state)
    monkeypatch.setenv(cli.CONFIG_ENV, config.format(2))
    cli.main(state)

    assert len(sent) == 2, "one alert per service, none for the repeat run"
