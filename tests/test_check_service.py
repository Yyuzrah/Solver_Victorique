"""The statement and proof checks can use another model service than the solver.

The offline CLIs (tests.test_timeout_retry.OfflineCli) run the real Codex and
Claude transports with fixed local Python prints and record which transport
and model each call went to.
"""
from contextlib import redirect_stdout
import io
import json
from unittest import mock

import pytest

from victorique import api, cli, settings, solve
from victorique.harness.codex_contract import DEFAULT_MODEL
from victorique.harness.routed_transport import ADAPTER_ID, RoutedTransport, service_of
from tests.test_timeout_retry import OfflineCli, problem_file, rows

CHECKS = {"alignment_review", "whole_proof_review"}


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch):
    monkeypatch.delenv(settings.CONFIG_ENVIRONMENT, raising=False)


@pytest.fixture
def offline(monkeypatch):
    return OfflineCli(monkeypatch)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(tmp_path, name="run", **options):
    out = tmp_path / name
    with redirect_stdout(io.StringIO()):
        summary = api.solve(problem_file(tmp_path), out=out, **options)
    return out, summary


def test_omitted_check_options_follow_the_solver():
    assert api.check_service("codex", None, None) == ("codex", None, None)
    assert api.check_service("codex", "gpt-x", "high") == ("codex", "gpt-x", "high")
    assert api.check_service("codex", "gpt-x", "high", check_model="gpt-y") == ("codex", "gpt-y", "high")
    assert api.check_service("codex", "gpt-x", "high", check_reasoning_effort="low") == ("codex", "gpt-x", "low")
    # Another CLI starts from its own defaults, not the solver's model or effort.
    assert api.check_service("codex", "gpt-x", "high", "claude") == ("claude", None, None)
    assert api.check_service("claude", None, None, "codex", None, "max") == ("codex", None, "max")
    assert api.check_service("fake", None, None) == ("fake", None, None)
    assert api.check_service("fake", None, None, "fake") == ("fake", None, None)


@pytest.mark.parametrize("arguments,message", [
    (("codex", None, None, "fake"), "practice run"),
    (("fake", None, None, "codex"), "practice run"),
    (("codex", None, None, "gemini"), "check_transport must be"),
    (("codex", None, None, "claude", None, "high"), "check_reasoning_effort is only supported by the codex"),
    (("codex", None, None, None, None, "extreme"), "check_reasoning_effort must be one of"),
    (("codex", None, None, None, "two words"), "check_model must be an identifier"),
    (("codex", None, None, None, "-x"), "check_model must be an identifier"),
])
def test_invalid_check_options(arguments, message):
    with pytest.raises(ValueError, match=message):
        api.check_service(*arguments)


def test_invalid_check_options_stop_before_a_run_starts(tmp_path):
    out = tmp_path / "run"
    with mock.patch.object(api, "inspect_host", side_effect=AssertionError("no probe")):
        with pytest.raises(ValueError, match="practice run"):
            solve(problem_file(tmp_path), out=out, transport="fake", check_transport="claude")
        with pytest.raises(ValueError, match="only supported by the codex"):
            solve(problem_file(tmp_path), out=out, check_transport="claude", check_reasoning_effort="low")
    assert not out.exists()


def test_command_line_options_reach_the_api():
    argv = ["victorique", "solve", "problem.md", "--check-transport", "claude", "--check-model", "claude-x",
            "--check-reasoning-effort", "high"]
    with mock.patch("sys.argv", argv), mock.patch.object(api, "solve", return_value={}) as called, \
         redirect_stdout(io.StringIO()):
        cli.main()
    assert {key: called.call_args.kwargs[key] for key in ("check_transport", "check_model", "check_reasoning_effort")} == {
        "check_transport": "claude", "check_model": "claude-x", "check_reasoning_effort": "high"}
    with mock.patch("sys.argv", ["victorique", "solve", "problem.md"]), \
         mock.patch.object(api, "solve", return_value={}) as called, redirect_stdout(io.StringIO()):
        cli.main()
    assert [called.call_args.kwargs[key] for key in ("check_transport", "check_model", "check_reasoning_effort")] == [
        None, None, None]


def test_checks_use_the_solver_service_by_default(tmp_path, offline):
    out, summary = run(tmp_path, transport="codex")
    assert summary["outcome"] == "complete" and summary["check_service"] is None
    assert {call["transport"] for call in offline.calls} == {"codex"}
    config = read(out / "state/run-config.json")
    assert "check_transport" not in config and config["execution_grant"]["adapter_id"] == "codex_cli"
    parameters = read(out / "invocation.json")["parameters"]
    assert (parameters["check_transport"], parameters["check_model"], parameters["check_reasoning_effort"]) == (
        "codex", None, None)
    assert {(row["service"], row["transport"]) for row in rows(out)} == {("solver", "codex_cli")}
    assert not (out / "check-profile.json").exists()


def test_fake_runs_keep_one_transport(tmp_path):
    out, summary = run(tmp_path, transport="fake", check_transport="fake")
    assert summary["outcome"] == "complete" and summary["check_service"] is None
    assert {row["service"] for row in rows(out)} == {"solver"}


@pytest.mark.parametrize("policy,expected", [
    ("single", ["solve_obligation", "alignment_review", "whole_proof_review"]),
    ("ladder", ["solve_obligation", "alignment_review", "whole_proof_review"]),
    ("full", ["propose_plan", "solve_obligation", "critique_candidate", "investigate", "repair_obligation",
              "critique_candidate", "investigate", "alignment_review", "whole_proof_review"]),
])
def test_codex_solves_and_claude_checks(tmp_path, offline, policy, expected):
    out, summary = run(tmp_path, policy=policy, transport="codex", check_transport="claude")
    assert summary["outcome"] == "complete" and summary["calls_used"] == len(expected)
    assert [call["kind"] for call in offline.calls] == expected
    # The two check roles go to Claude Code; every other role stays with Codex.
    assert [call["transport"] for call in offline.calls] == [
        "claude" if kind in CHECKS else "codex" for kind in expected]
    calls = rows(out)
    assert [(row["service"], row["transport"]) for row in calls] == [
        ("checks", "claude_cli") if row["role"] in ("alignment_reviewer", "proof_reviewer") else ("solver", "codex_cli")
        for row in calls]
    assert {row["model"] for row in calls if row["service"] == "checks"} == {"offline-claude"}
    assert {row["model"] for row in calls if row["service"] == "solver"} == {"offline-codex"}
    assert summary["transport"] == "codex" and summary["reported_model"] == "offline-codex"
    assert summary["check_service"] == {"transport": "claude", "requested_model": None,
                                        "requested_reasoning_effort": None, "cli_version": "2.1.999",
                                        "reported_model": "offline-claude", "calls": 2}
    config = read(out / "state/run-config.json")
    assert config["check_transport"]["kind"] == "claude_cli"
    assert config["check_transport"]["authorization"]["requested_model"] == DEFAULT_MODEL
    assert config["execution_grant"]["adapter_id"] == ADAPTER_ID
    bindings = config["manifest"]["role_bindings"]
    assert {role: bindings[role]["model_request"]["provider_id"] for role in bindings} == {
        role: "claude-cli" if role in ("alignment_reviewer", "proof_reviewer") else "codex-cli" for role in bindings}
    if policy != "full":
        # Claude Code roles run one at a time, also when only the checks use it.
        assert config["manifest"]["limits"]["max_inflight"] == 1
    assert read(out / "check-authorization.json")["schema"] == "victorique.claude_execution_authorization.v1"
    with redirect_stdout(io.StringIO()):
        assert api.resume(out) == summary
        assert api.status(out) == summary
    _, loop = api._open(out)
    assert isinstance(loop.adapter, RoutedTransport)
    assert len(offline.calls) == len(expected)


def test_claude_solves_and_codex_checks_with_its_own_model_and_effort(tmp_path, offline):
    out, summary = run(tmp_path, transport="claude", check_transport="codex", check_model="gpt-check",
                       check_reasoning_effort="high")
    assert summary["outcome"] == "complete"
    assert [(call["transport"], call["model"]) for call in offline.calls] == [
        ("claude", DEFAULT_MODEL), ("codex", "gpt-check"), ("codex", "gpt-check")]
    checks = [call for call in offline.calls if call["transport"] == "codex"]
    assert all(call["argv"][call["argv"].index("--model") + 1] == "gpt-check" for call in checks)
    assert all('model_reasoning_effort="high"' in call["argv"] for call in checks)
    assert [(row["requested_model"], row["requested_reasoning_effort"]) for row in rows(out)] == [
        (None, None), ("gpt-check", "high"), ("gpt-check", "high")]
    assert summary["check_service"]["requested_model"] == "gpt-check"
    assert summary["check_service"]["requested_reasoning_effort"] == "high"
    parameters = read(out / "invocation.json")["parameters"]
    assert (parameters["check_transport"], parameters["check_model"], parameters["check_reasoning_effort"]) == (
        "codex", "gpt-check", "high")


def test_same_cli_with_another_check_model_gets_its_own_transport(tmp_path, offline):
    out, summary = run(tmp_path, transport="codex", model="gpt-solve", check_model="gpt-check")
    assert summary["outcome"] == "complete"
    assert [call["model"] for call in offline.calls] == ["gpt-solve", "gpt-check", "gpt-check"]
    assert summary["requested_model"] == "gpt-solve" and summary["check_service"]["transport"] == "codex"
    assert (out / "state/check-transport").is_dir() and (out / "state/transport").is_dir()


def test_a_timed_out_check_is_sent_again_on_the_check_service(tmp_path, offline):
    offline.stall.add(("claude", "whole_proof_review"))
    out, summary = run(tmp_path, transport="codex", check_transport="claude", hours=0.05)
    assert summary["outcome"] == "complete" and summary["calls_used"] == 4
    assert summary["timeout_retries_used"] == 1 and summary["check_service"]["calls"] == 3
    assert [(call["transport"], call["kind"], call["stalled"]) for call in offline.calls] == [
        ("codex", "solve_obligation", False), ("claude", "alignment_review", False),
        ("claude", "whole_proof_review", True), ("claude", "whole_proof_review", False)]
    repeated = rows(out)[-1]
    assert repeated["retry_of"] == "single.review" and repeated["service"] == "checks"


def test_routing_by_action_kind():
    assert [service_of(kind) for kind in ("alignment_review", "whole_proof_review")] == ["checks", "checks"]
    assert {service_of(kind) for kind in ("propose_plan", "replan", "solve_obligation", "repair_obligation",
                                          "integrate", "critique_candidate", "investigate",
                                          "compare_candidates")} == {"solver"}
