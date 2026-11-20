"""One per-call limit: a value other than 600 reaches every model call path.

Live Codex and Claude roles run here as fixed local Python prints, so each test
records the limit that the real transport computed for every call.
"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
from unittest import mock

import pytest

from victorique import api, bench, calibrate, cli, error_probe, settings, shared_prefix, verify
from victorique.fake import FakeTransport
from victorique.harness import grade
from victorique.harness.claude_transport import ClaudeCliTransport
from victorique.harness.codex_transport import CodexCliTransport, CodexFixtureTransport
from tests.test_bestofn import PROBLEM, reference
from tests.test_resample import Scripted


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch):
    monkeypatch.delenv(settings.CONFIG_ENVIRONMENT, raising=False)


@pytest.fixture
def offline(monkeypatch):
    """Install an offline CLI for one transport; returns each call's computed limit."""
    from tests.test_victorique_harness_codex_contract import public_profile as codex_profile
    from tests.test_claude_contract import public_profile as claude_profile
    limits = []

    def install(transport, responder=None):
        responder = responder or FakeTransport()
        profile = codex_profile() if transport == "codex" else claude_profile()
        for module in (api, verify):
            monkeypatch.setattr(module, "inspect_host", lambda *args, **kwargs: profile)
            monkeypatch.setattr(module, "_mcp_names", lambda *args: [])
        monkeypatch.setattr("victorique.harness.codex_defaults.read_cli_defaults",
                            lambda *args, **kwargs: {"status": "unavailable"})
        adapter_type = CodexCliTransport if transport == "codex" else ClaudeCliTransport
        original = adapter_type._invocation

        def invocation(self, request, binding):
            computed = original(self, request, binding)
            limits.append(computed["timeout_seconds"])
            envelope = json.loads(responder._response(request))
            if transport == "codex":
                rows = [{"type": "thread.started", "thread_id": request.job_id, "model": "offline-model"},
                        {"type": "turn.started"},
                        {"type": "item.completed", "item": {"id": "final", "type": "agent_message",
                                                            "text": json.dumps(envelope)}},
                        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}]
            else:
                rows = [{"type": "system", "subtype": "init", "session_id": request.job_id, "model": "offline-model",
                         "tools": [], "mcp_servers": []},
                        {"type": "result", "subtype": "success", "is_error": False, "session_id": request.job_id,
                         "structured_output": envelope, "usage": {"input_tokens": 7, "output_tokens": 3}}]
            script = "import sys,pathlib; sys.stdin.buffer.read(); "
            if transport == "codex":
                script += ("pathlib.Path(" + repr(computed["final_file"]) + ").write_text("
                           + repr(json.dumps(envelope)) + ",encoding='utf-8'); ")
            script += "print(" + repr("\n".join(json.dumps(row) for row in rows)) + ")"
            return {**computed, "argv": [sys.executable, "-I", "-c", script], "timeout_seconds": 5}

        monkeypatch.setattr(adapter_type, "_invocation", invocation)
        return limits
    return install


def problem_file(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    return problem


def test_setting_default_bounds_and_run_limit():
    assert settings.CALL_TIMEOUT_SECONDS == 600 and settings.call_timeout_seconds() == 600
    assert settings.call_timeout_seconds(900) == 900 and settings.call_timeout_seconds(0.05) == 0.05
    assert settings.call_timeout_seconds(86400) == 86400
    for value in (0.04, 86401, float("nan"), float("inf"), True, "600"):
        with pytest.raises(ValueError, match="call_timeout_seconds"):
            settings.call_timeout_seconds(value)
    with pytest.raises(ValueError):
        settings.check_seconds(None)
    # Each call is also bounded by the run itself.
    assert settings.run_call_timeout(900, 7200) == 900
    assert settings.run_call_timeout(900, 60) == 60
    assert settings.run_call_timeout(None, 0) == 0.05
    assert settings.seconds("900") == 900 and type(settings.seconds("900")) is int
    assert settings.seconds("1.5") == 1.5


def test_config_file_environment_and_errors(tmp_path, monkeypatch):
    path = tmp_path / "victorique.toml"
    path.write_text('call_timeout_seconds = 1200\nundecided_checks = "repair"\n')
    assert settings.load_config(path) == {"call_timeout_seconds": 1200, "undecided_checks": "repair"}
    assert settings.load_config() == {}
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(path))
    assert settings.load_config() == {"call_timeout_seconds": 1200, "undecided_checks": "repair"}
    # An explicit option wins over the file; the file fills in what is omitted.
    assert settings.command_settings(None, call_timeout_seconds=None) == {"call_timeout_seconds": 1200}
    assert settings.command_settings(None, call_timeout_seconds=30) == {"call_timeout_seconds": 30}
    for text, message in (("timeout = 5\n", "unknown setting timeout"),
                          ("call_timeout_seconds = 0\n", "call_timeout_seconds"),
                          ('call_timeout_seconds = "600"\n', "call_timeout_seconds"),
                          ('undecided_checks = "maybe"\n', "undecided_checks"),
                          ("call_timeout_seconds = \n", "invalid TOML")):
        path.write_text(text)
        with pytest.raises(ValueError, match=message):
            settings.load_config(path)
    with pytest.raises(OSError):
        settings.load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize("command", ["solve", "verify", "bench"])
def test_cli_flag_over_config_file_over_default(tmp_path, monkeypatch, command):
    config = tmp_path / "settings.toml"
    config.write_text("call_timeout_seconds = 1200\n")
    target = {"solve": (api, "solve"), "verify": (verify, "verify"), "bench": (bench, "bench")}[command]
    arguments = {"solve": ["problem.md"], "verify": ["proof.md", "--problem", "problem.md"],
                 "bench": ["problems"]}[command]
    for extra, expected in (([], None), (["--call-timeout-seconds", "900"], 900),
                            (["--config", str(config)], 1200),
                            (["--config", str(config), "--call-timeout-seconds", "45.5"], 45.5)):
        with mock.patch("sys.argv", ["victorique", command, *arguments, *extra]), \
             mock.patch.object(*target, return_value={"outcome": "pass"}) as called, \
             redirect_stdout(io.StringIO()), pytest.raises(SystemExit) if command == "verify" else _nothing():
            cli.main()
        assert called.call_args.kwargs["call_timeout_seconds"] == expected
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(config))
    with mock.patch("sys.argv", ["victorique", command, *arguments]), \
         mock.patch.object(*target, return_value={"outcome": "pass"}) as called, \
         redirect_stdout(io.StringIO()), pytest.raises(SystemExit) if command == "verify" else _nothing():
        cli.main()
    assert called.call_args.kwargs["call_timeout_seconds"] == 1200


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_invalid_cli_value_or_file_exits_before_any_run(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("call_timeout_seconds = 90000\n")
    out = tmp_path / "run"
    for extra in (["--call-timeout-seconds", "0"], ["--call-timeout-seconds", "ten"], ["--config", str(bad)]):
        with mock.patch("sys.argv", ["victorique", "solve", str(tmp_path / "absent.md"), "--out", str(out), *extra]), \
             mock.patch.object(api, "inspect_host", side_effect=AssertionError("no probe")), \
             pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize("policy", ["single", "ladder", "resample", "full", "bestofn"])
def test_codex_role_calls_use_the_setting_in_every_policy(tmp_path, offline, policy):
    limits = offline("codex")
    out = tmp_path / "run"
    # A four-call full run plans, solves and checks once; it takes the same transport path.
    options = ({"full_calls": reference(tmp_path), "calls": 5} if policy == "bestofn"
               else {"calls": 4} if policy == "full" else {})
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem_file(tmp_path), policy=policy, transport="codex", out=out,
                           call_timeout_seconds=900, **options)
    assert result["outcome"] == "complete"
    assert len(limits) == result["calls_used"] == {"full": 4, "bestofn": 5}.get(policy, 3)
    assert set(limits) == {900}
    assert read(out / "state/run-config.json")["transport"]["timeout_seconds"] == 900
    assert read(out / "invocation.json")["parameters"]["call_timeout_seconds"] == 900


def test_codex_default_stays_600_and_resume_keeps_the_frozen_limit(tmp_path, offline):
    limits = offline("codex")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem_file(tmp_path), policy="single", transport="codex", out=out)
    assert set(limits) == {600} and len(limits) == 3
    assert read(out / "invocation.json")["parameters"]["call_timeout_seconds"] == 600
    _, loop = api._open(out)
    assert loop.adapter.config["timeout_seconds"] == 600
    with redirect_stdout(io.StringIO()):
        assert api.resume(out) == result


def test_short_run_caps_each_call_at_its_own_duration(tmp_path, offline):
    offline("codex")
    with mock.patch.object(api, "_run", return_value=None), redirect_stdout(io.StringIO()):
        api.solve(problem_file(tmp_path), policy="single", transport="codex", out=tmp_path / "run",
                  hours=.05, call_timeout_seconds=900)
    assert read(tmp_path / "run/state/run-config.json")["transport"]["timeout_seconds"] == 180


def test_claude_role_calls_and_verify_use_the_setting(tmp_path, offline):
    limits = offline("claude")
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem_file(tmp_path), policy="single", transport="claude", out=tmp_path / "solve",
                           calls=3, call_timeout_seconds=900)
    assert result["outcome"] == "complete" and limits == [900] * 3
    proof = tmp_path / "proof.md"
    proof.write_text("Adding 0 leaves every integer unchanged.")
    with redirect_stdout(io.StringIO()):
        result = verify.verify(proof, problem=tmp_path / "problem.md", transport="claude", calls=1,
                               out=tmp_path / "verify", call_timeout_seconds=1200)
    assert result["outcome"] == "pass" and limits[3:] == [1200]


def test_codex_verify_uses_the_setting(tmp_path, offline):
    limits = offline("codex")
    proof = tmp_path / "proof.md"
    proof.write_text("Adding 0 leaves every integer unchanged.")
    with redirect_stdout(io.StringIO()):
        result = verify.verify(proof, problem=problem_file(tmp_path), transport="codex", mode="multi",
                               calls=3, out=tmp_path / "verify", call_timeout_seconds=900)
    assert result["outcome"] == "pass" and limits == [900] * 3
    assert read(tmp_path / "verify/invocation.json")["parameters"]["call_timeout_seconds"] == 900
    with pytest.raises(ValueError, match="call_timeout_seconds"):
        verify.verify(proof, problem=tmp_path / "problem.md", transport="codex", out=tmp_path / "bad",
                      call_timeout_seconds=0)
    assert not (tmp_path / "bad").exists()


def test_escalated_full_child_keeps_the_parent_limit(tmp_path, offline):
    offline("codex")
    with redirect_stdout(io.StringIO()):
        _, loop = api._create_run(problem_file(tmp_path), policy="ladder", transport="codex", out=tmp_path / "run",
                                  repair_rounds=0, escalate=True, call_timeout_seconds=900)
        loop._escalate(loop.core.snapshot()["exchange"])
    assert loop._child().adapter.config["timeout_seconds"] == 900
    assert read(tmp_path / "run/state/escalated/run-config.json")["transport"]["timeout_seconds"] == 900


def test_paired_runner_prefix_and_branches_use_the_default(tmp_path, offline, monkeypatch):
    # The paired runner has no per-run option; it follows the one built-in default.
    monkeypatch.setattr(settings, "CALL_TIMEOUT_SECONDS", 900)
    limits = offline("codex", Scripted())
    result = shared_prefix.solve(problem_file(tmp_path), out=tmp_path / "paired", calls=6, transport="codex")
    assert result["physical_calls"] == 9 == len(limits) and set(limits) == {900}
    for policy in ("prefix", "ladder", "resample"):
        config = read(tmp_path / "paired/0000" / policy / "state/run-config.json")
        assert config["transport"]["timeout_seconds"] == 900


def test_error_probe_attempts_use_the_default(tmp_path, monkeypatch):
    from tests.test_victorique_harness_codex_contract import public_profile
    monkeypatch.setattr(settings, "CALL_TIMEOUT_SECONDS", 900)
    monkeypatch.setattr(error_probe, "inspect_host", lambda *args: public_profile())
    monkeypatch.setattr(error_probe.api, "_mcp_names", lambda *args: [])
    seen = []
    def create(*args, **kwargs):
        seen.append(kwargs["timeout_seconds"])
        raise ValueError("no live transport in this test")
    monkeypatch.setattr(error_probe.CodexCliTransport, "create", create)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    result = error_probe._new_attempt(tmp_path / "attempt", "inject", {"original": "A proof."}, "codex", deadline)
    assert result["status"] == "failed" and result["calls_used"] == 0
    assert seen == [900]


@pytest.mark.parametrize("seconds,expected", [(None, 600), (900, 900), (86400, 86400)])
def test_reference_grade_uses_the_setting(tmp_path, seconds, expected):
    from tests.test_grade import request
    result = grade.grade_reference(request(), directory=tmp_path / "grade", transport="fake",
                                   **({} if seconds is None else {"seconds": seconds}))
    assert result["status"] == "completed"
    assert CodexFixtureTransport(tmp_path / "grade/transport").config["timeout_seconds"] == expected


def test_bench_passes_the_setting_to_each_solve_and_grade(tmp_path):
    from tests.test_bench import delivered, problem
    problem(tmp_path / "problems", "a", reference_solution="reference")
    seen = []
    def solve_fn(path, *, out, **kwargs):
        seen.append(kwargs)
        return delivered(path, out=out)
    for grading_seconds, expected in ((None, 900), (30, 30)):
        out = tmp_path / ("out-" + str(grading_seconds))
        result = bench.bench(tmp_path / "problems", out=out, transport="fake", grader="fake", solve_fn=solve_fn,
                             call_timeout_seconds=900, undecided_checks="repair", grading_seconds=grading_seconds)
        assert seen[-1]["call_timeout_seconds"] == 900 and seen[-1]["undecided_checks"] == "repair"
        assert result["call_timeout_seconds"] == 900 and result["grading_seconds_per_candidate"] == expected
        assert CodexFixtureTransport(out / "a/grading-primary/transport").config["timeout_seconds"] == expected
    with pytest.raises(ValueError, match="grading seconds"):
        bench.bench(tmp_path / "problems", out=tmp_path / "bad", transport="fake", grading_seconds=86401)


def test_calibration_passes_the_setting_to_each_review(tmp_path):
    (tmp_path / "proof.md").write_text("A proof.")
    (tmp_path / "problem.md").write_text(PROBLEM)
    dataset = tmp_path / "labels.json"
    dataset.write_text(json.dumps([{"id": "one", "label": "correct", "proof": "proof.md", "problem": "problem.md"}]))
    with redirect_stdout(io.StringIO()):
        report = calibrate.calibrate(dataset, modes=["single"], out=tmp_path / "out", call_timeout_seconds=900)
    assert report["samples"][0]["outcome"] == "pass"
    assert read(tmp_path / "out/1-single/invocation.json")["parameters"]["call_timeout_seconds"] == 900
    with mock.patch.object(calibrate, "calibrate", return_value={}) as called, redirect_stdout(io.StringIO()):
        calibrate.main([str(dataset), "--call-timeout-seconds", "1200"])
    assert called.call_args.kwargs["call_timeout_seconds"] == 1200
