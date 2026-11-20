"""The run settings together: call_timeout_seconds, undecided_checks, minor_findings,
timeout_retries and the check service options.

One settings file holds all four settings. The command line, /api/options and the web
form read them together, and a run records every value and uses it on resume.
"""
from contextlib import redirect_stdout
import io
import json
from unittest import mock

import pytest

from victorique import api, bench, cli, settings
from victorique.gui import launch
from victorique.gui.server import GuiServer, parse_form
from tests.test_gui import STATEMENT, _parameters, _request, call, gui, wait_finished  # noqa: F401 (gui is a fixture)
from tests.test_minor_findings import MinorNote
from tests.test_timeout_retry import StopsAtLimit, problem_file, resume_with, rows

SETTINGS = ('call_timeout_seconds = 90\nundecided_checks = "repair"\nminor_findings = "blocking"\n'
            'timeout_retries = 2\n')
RECORDED = {"call_timeout_seconds": 90, "undecided_checks": "repair", "minor_findings": "blocking",
            "timeout_retries": 2}


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch):
    monkeypatch.delenv(settings.CONFIG_ENVIRONMENT, raising=False)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "victorique.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    return path


class StopsWithNote(StopsAtLimit):
    """The first call of each listed kind stops at its time limit, and the checks of the
    first version checked say pass with one minor finding (MinorNote)."""

    def __init__(self, *kinds):
        super().__init__(*kinds)
        self.note = MinorNote()

    def _response(self, request):
        return self.note._response(request)


def test_the_command_line_reads_all_settings_from_one_file(config):
    assert settings.load_config(config) == RECORDED
    for command, target, arguments in (
            ("solve", (api, "solve"), ["problem.md", "--check-transport", "claude", "--check-model", "m"]),
            ("bench", (bench, "bench"), ["problems"])):
        # An option on the command line still overrides its key in the file.
        with mock.patch("sys.argv", ["victorique", command, *arguments, "--config", str(config),
                                     "--minor-findings", "nonblocking"]), \
             mock.patch.object(*target, return_value={}) as called, redirect_stdout(io.StringIO()):
            cli.main()
        given = called.call_args.kwargs
        assert {key: given[key] for key in RECORDED} == {**RECORDED, "minor_findings": "nonblocking"}
        if command == "solve":
            assert (given["check_transport"], given["check_model"], given["check_reasoning_effort"]) == (
                "claude", "m", None)


def test_options_and_the_form_preselect_from_one_settings_file(tmp_path, config, monkeypatch):
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(config))
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False)
    try:
        app = server.app
        app.timeout_option, app.undecided_option = "--call-timeout-seconds", "--undecided-checks"
        app.minor_option, app.retries_option = "--minor-findings", "--timeout-retries"
        app.check_options = dict(launch.CHECK_OPTIONS)
        options = app.options()
        assert options["defaults"] == {"policy": "single", "backend": "codex", "calls": 12, "hours": 2,
                                       "undecided_checks": "repair", "minor_findings": "blocking",
                                       "check_backend": "", "timeout_retries": 2}
        for key in ("undecided_checks", "minor_findings", "timeout_retries"):
            assert (options[key]["source"], options[key]["settings_file"]) == ("settings file", str(config))
        assert [item["value"] for item in options["policies"]][0] == "single"
        # A ladder form sent without these choices passes the preselected values.
        _, _, values = parse_form({"statement": STATEMENT, "policy": "ladder", "backend": "fake",
                                   "check_backend": "fake"}, app.timeout_option, app.undecided_option,
                                  app.undecided_setting()["default"], app.minor_option,
                                  app.minor_findings_setting()["default"], retries_option=app.retries_option,
                                  retries_default=app.retries_setting()["default"], check_options=app.check_options)
        arguments = launch.solve_arguments("p", "o", values, app.timeout_option, app.undecided_option,
                                           app.minor_option, retries_option=app.retries_option,
                                           check_options=app.check_options)
        chosen = {option: arguments[arguments.index(option) + 1] for option in (
            "--policy", "--undecided-checks", "--minor-findings", "--timeout-retries", "--check-transport")}
        assert chosen == {"--policy": "ladder", "--undecided-checks": "repair", "--minor-findings": "blocking",
                          "--timeout-retries": "2", "--check-transport": "fake"}
        # An empty per-call field passes nothing, so the command takes call_timeout_seconds from the file.
        assert "--call-timeout-seconds" not in arguments
    finally:
        server.httpd.server_close()


def test_a_form_run_passes_and_records_every_field(gui):
    status, _, body = call(gui, "/api/runs", body={
        "statement": STATEMENT, "policy": "ladder", "backend": "fake", "check_backend": "fake",
        "call_timeout": "90", "undecided_checks": "repair", "minor_findings": "blocking", "timeout_retries": "2",
        "calls": 12, "hours": 1})
    assert status == 201, body
    name = json.loads(body)["name"]
    detail, _ = wait_finished(gui, name)
    # The fake checks list no finding, so the proof is delivered.
    assert detail["status"] == "delivered", detail
    command = _request(gui, name)["command"]
    assert {option: command[command.index(option) + 1] for option in (
        "--policy", "--call-timeout-seconds", "--undecided-checks", "--minor-findings", "--timeout-retries",
        "--check-transport")} == {"--policy": "ladder", "--call-timeout-seconds": "90.0",
                                  "--undecided-checks": "repair", "--minor-findings": "blocking",
                                  "--timeout-retries": "2", "--check-transport": "fake"}
    parameters = _parameters(gui, name)
    assert {key: parameters[key] for key in RECORDED} == RECORDED
    assert (parameters["check_transport"], parameters["check_model"]) == ("fake", None)
    assert (detail["call_timeout_seconds"], detail["undecided_checks_label"], detail["minor_findings_label"],
            detail["timeout_retries"], detail["timeout_retries_used"]) == (
        90, "Repair once", "Count it as not passed", 2, 0)


def test_resume_uses_every_recorded_setting(tmp_path, monkeypatch):
    adapter = StopsWithNote("alignment_review")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        api._create_run(problem_file(tmp_path), out=out, policy="ladder", transport="fake", _adapter=adapter,
                        check_transport="fake", **RECORDED)
    invocation = json.loads((out / "invocation.json").read_text(encoding="utf-8"))["parameters"]
    assert {key: invocation[key] for key in RECORDED} == RECORDED
    assert (invocation["check_transport"], invocation["check_model"], invocation["check_reasoning_effort"]) == (
        "fake", None, None)
    config = json.loads((out / "state" / "run-config.json").read_text(encoding="utf-8"))
    assert config["manifest"]["limits"]["minor_findings"] == "blocking" and "check_transport" not in config
    # A settings file with other values does not reach resume, which runs the whole run here.
    other = tmp_path / "other.toml"
    other.write_text('call_timeout_seconds = 5\nundecided_checks = "abstain"\nminor_findings = "nonblocking"\n'
                     'timeout_retries = 0\n', encoding="utf-8")
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(other))
    loop = api._open(out)[1]
    assert loop.timeout_retry_limit == 2 and loop._undecided_mode() == "repair"
    summary = resume_with(adapter, out, monkeypatch)
    calls = rows(out)
    ids = [row["request_id"] for row in calls]
    # timeout_retries: the timed-out check was sent again.
    assert ("ladder.L0.alignment.timeout-retry-1", "ladder.L0.alignment") in [
        (row["request_id"], row["retry_of"]) for row in calls]
    assert summary["timeout_retries_per_call"] == 2 and summary["timeout_retries_used"] == 1
    # minor_findings: the checks that passed with a minor finding sent the draft to repair.
    assert "ladder.L1.repair.1" in ids
    assert summary["outcome"] == "complete" and summary["check_service"] is None
