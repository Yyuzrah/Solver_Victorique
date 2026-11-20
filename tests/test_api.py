from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import inspect
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from victorique import solve
from victorique import api
from victorique.harness.codex_contract import conservative_codex_argv, DEFAULT_MODEL, parse_codex_capture
from tests.test_victorique_harness_codex_contract import public_profile, LAYOUT, events, packet, process, response_bytes, stdout


@pytest.mark.parametrize("policy, calls", [("full", 9), ("single", 3), ("ladder", 3), ("resample", 3)])
def test_cli_fake_solve_and_resume(tmp_path, policy, calls):
    problem = tmp_path / "toy.md"
    problem.write_text("Prove that n + 0 = n for every integer n.", encoding="utf8")
    out = tmp_path / "run"
    cmd = [str(Path(sys.executable).with_name("victorique.exe" if sys.platform == "win32" else "victorique"))]
    # The default policy is single: its command line names no policy.
    policy_args = [] if policy == "single" else ["--policy", policy]
    result = subprocess.run([*cmd, "solve", str(problem), "--transport", "fake", "--out", str(out), *policy_args],
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    summary = json.loads((out / "summary.json").read_text())
    assert summary["outcome"] == "complete"
    assert summary["policy"] == policy
    assert summary["run_id"].startswith("run-")
    assert summary["calls_used"] == calls
    assert summary["tokens"] is None
    assert summary["reported_model"] is None
    assert summary["wall_seconds"] < 60
    assert "n + 0 = n" in (out / "proof.md").read_text()
    records = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert len(records) == calls
    assert all(row["run_id"] == summary["run_id"] and row["policy"] == policy for row in records)
    assert all(row["tokens"] is None for row in records)
    if policy == "full":
        assert "investigate | investigator" in result.stdout
        assert "repair_obligation | solver" in result.stdout
    else:
        assert "solve_obligation | solver" in result.stdout
        assert "alignment_review | alignment_reviewer" in result.stdout
        assert "whole_proof_review | proof_reviewer" in result.stdout
    assert json.loads((out / "invocation.json").read_text())["argv"][1:] == ["solve", str(problem), "--transport", "fake", "--out", str(out), *policy_args]
    resumed = subprocess.run([*cmd, "resume", str(out)], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=30)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads((out / "summary.json").read_text()) == summary
    refused = subprocess.run([*cmd, "resume", str(out), "--calls", "20"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=5)
    assert refused.returncode == 2
    assert api.status(out) == summary
    assert api.export(out) == summary
    assert [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()] == records


@pytest.mark.parametrize("policy, calls", [("full", 9), ("single", 3), ("ladder", 3), ("resample", 3)])
def test_python_api_fake_solve(tmp_path, policy, calls):
    problem = tmp_path / "toy.md"
    problem.write_text("Prove that n + 0 = n for every integer n.")
    out = tmp_path / "python-run"
    with redirect_stdout(io.StringIO()):
        result = solve(problem, policy=policy, out=out, transport="fake")
    assert result["outcome"] == "complete"
    assert result["policy"] == policy
    assert result["calls_used"] == calls
    assert result["calls_limit"] == 12
    assert (out / "proof.md").is_file()
    assert len((out / "calls.jsonl").read_text().splitlines()) == calls
    assert json.loads((out / "summary.json").read_text()) == result
    with pytest.raises(ValueError, match="already exists"):
        solve(problem, out=out, transport="fake")


def test_resume_after_an_outside_change_runs_the_full_check(tmp_path):
    import sqlite3
    from contextlib import closing
    problem = tmp_path / "toy.md"
    problem.write_text("Prove that n + 0 = n for every integer n.")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        result = solve(problem, policy="full", out=out, transport="fake")
        assert api.resume(out) == result
    # Backdate the first event; the schema, row counts and last event stay as they were.
    with closing(sqlite3.connect(out / "state" / "native" / "adaptive_session.sqlite3")) as db, db:
        value = json.loads(db.execute("SELECT payload FROM events WHERE sequence=1").fetchone()[0])
        value["recorded_at_utc"] = "2000-01-01T00:00:00+00:00"
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='events'").fetchall()
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name + '"')
        db.execute("UPDATE events SET payload=? WHERE sequence=1", (json.dumps(value),))
        for _, sql in triggers:
            db.execute(sql)
    with redirect_stdout(io.StringIO()), pytest.raises(ValueError):
        api.resume(out)
    with pytest.raises(ValueError):
        api.status(out)


def test_package_solve_exposes_all_public_api_parameters_and_defaults():
    public = {name: parameter for name, parameter in inspect.signature(api.solve).parameters.items()
              if not name.startswith("_")}
    assert dict(inspect.signature(solve).parameters) == public


@pytest.mark.parametrize("entry", [solve, api.solve])
def test_new_run_defaults_to_single_and_resume_preserves_options(tmp_path, entry):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove that n + 0 = n for every integer n.")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        result = entry(problem, calls=7, hours=.25, transport="fake", out=out)
        frozen = (out / "state/run-config.json").read_bytes()
        resumed = api.resume(out)
    assert result["policy"] == "single" and result["calls_used"] == 3
    assert result["outcome"] == "complete" and resumed == result
    assert result["endpoint_reason"] == "single_complete"
    options = json.loads((out / "invocation.json").read_text())["parameters"]
    assert options["policy"] == "single" and options["repair_rounds"] == 2
    assert options["investigate"] is options["escalate"] is False
    assert result["calls_limit"] == 7 and options["hours"] == .25
    assert (out / "state/run-config.json").read_bytes() == frozen


def test_legacy_invocation_without_policy_keeps_full_budget_and_deadline(tmp_path):
    from victorique.fake import FakeTransport
    problem = tmp_path / "problem.md"
    problem.write_text("Synthetic legacy problem")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        api._create_run(problem, policy="full", calls=7, hours=.25, transport="fake", out=out,
                        _started_at=datetime.now(timezone.utc) - timedelta(hours=1))
    invocation = json.loads((out / "invocation.json").read_text())
    del invocation["parameters"]["policy"]
    (out / "invocation.json").write_text(json.dumps(invocation))
    frozen = (out / "state/run-config.json").read_bytes()
    deadline = json.loads(frozen)["manifest"]["limits"]["stop_condition"]["deadline_utc"]
    with mock.patch.object(FakeTransport, "start_once", side_effect=AssertionError("expired legacy run dispatched")), \
         redirect_stdout(io.StringIO()):
        assert type(api._open(out)[1]) is api.OriginalProblemLoop
        assert api.status(out)["policy"] == "full"
        result = api.resume(out)
        assert api.export(out) == result
    assert result["policy"] == "full" and result["outcome"] == "partial"
    assert result["calls_used"] == 0 and result["calls_limit"] == 7
    assert result["deadline_utc"] == deadline
    assert (out / "state/run-config.json").read_bytes() == frozen


@pytest.mark.parametrize("fallback", [False, True])
def test_package_solve_accepts_bestofn_references(tmp_path, fallback):
    from tests.test_bestofn import PROBLEM, reference, reference_list
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    paired = reference(tmp_path, missing=(4,) if fallback else ())
    pool = None
    if fallback:
        other = reference(tmp_path, run_id="other")
        pool = reference_list(tmp_path, [(other, "Advanced")])
    with redirect_stdout(io.StringIO()):
        result = solve(problem, policy="bestofn", full_calls=paired, full_references=pool,
                       calls=5, transport="fake", out=tmp_path / "run")
    assert result["outcome"] == "complete" and result["calls_used"] == 5
    estimate = result["bestofn"]["estimate"]
    assert estimate["target_tokens"] == 500
    assert estimate["target_fallback"] == ("same_problem_median" if fallback else "paired_full")
    assert estimate["target_source_run_ids"] == (["other"] if fallback else ["paired-full"])


def test_expired_deadline_resumes_without_calls(tmp_path):
    problem = tmp_path / "toy.md"
    problem.write_text("toy")
    with mock.patch.object(api, "_run", return_value=None):
        solve(problem, policy="full", out=tmp_path / "expired", transport="fake", hours=.00001)
    import time
    time.sleep(.05)
    with redirect_stdout(io.StringIO()):
        summary = api.resume(tmp_path / "expired")
    assert summary["calls_used"] == 0
    assert summary["outcome"] == "partial"


def test_call_limit_retained(tmp_path):
    problem = tmp_path / "toy.md"
    problem.write_text("toy")
    with redirect_stdout(io.StringIO()):
        result = solve(problem, calls=2, out=tmp_path / "limited", transport="fake")
        resumed = api.resume(tmp_path / "limited")
    assert result["calls_used"] <= 2
    assert result["outcome"] == "abstain"
    assert resumed == result


@pytest.mark.parametrize("reason,error", [
    ("capture_failed", None),
    ("domain_projection_rejected", "Synthetic projection failure"),
    ("projection_resource_exhausted", "JSON exceeds byte limit"),
])
def test_sealed_failure_reports_partial_and_never_dispatches_on_resume(tmp_path, reason, error):
    from victorique import adaptive_session
    from victorique.fake import FakeTransport
    problem = tmp_path / "problem.md"
    problem.write_text("Synthetic problem")
    out = tmp_path / "run"
    original_reduce = adaptive_session.controller.reduce
    def reduce(state, event):
        if error is not None and event["kind"] == "return_action":
            raise ValueError(error)
        return original_reduce(state, event)
    adapter = FakeTransport()
    if error is None:
        adapter._response = lambda request: None
    # Keep the injected reducer fault active while reopening the synthetic run.
    with mock.patch.object(adaptive_session.controller, "reduce", side_effect=reduce), \
         redirect_stdout(io.StringIO()):
        _, loop = api._create_run(problem, policy="full", out=out, transport="fake", _adapter=adapter)
        assert api.status(out)["outcome"] == "pending"
        assert api.status(out)["endpoint_reason"] is None
        loop.step()  # Prepare the first plan.
        loop.step()  # Dispatch the fake response and freeze the endpoint.
        result = api.export(out)
        assert result["outcome"] == "partial"
        assert result["endpoint_reason"] == reason
        assert result["calls_used"] == 1
        assert loop.core.snapshot()["endpoint"]["reducer_endpoint"] is None
        with mock.patch.object(FakeTransport, "start_once", side_effect=AssertionError("sealed run dispatched")):
            assert api.status(out) == result
            assert api.export(out) == result
            if reason != "capture_failed":
                assert loop.status()["pending_original_response_responsibilities"] == []
                assert api.resume(out) == result
        assert len((out / "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("kwargs", [{"calls": 0}, {"hours": float("nan")}, {"hours": -1}, {"model": "two words"}, {"policy": "invalid"}])
def test_invalid_parameters_before_probe(tmp_path, kwargs):
    with mock.patch.object(api, "inspect_host", side_effect=AssertionError("no probe")):
        with pytest.raises(ValueError):
            solve(tmp_path / "missing", out=tmp_path / "new", **kwargs)
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("deadline", ["2001-01-01T00:00:00Z", "2028-01-01", "2028-01-01T00:00:00",
                                     "2028-01-01T00:00:00+01:00", "2028-01-01 00:00:00Z", 1])
def test_explicit_deadline_rejects_expired_or_malformed_before_probe(tmp_path, deadline):
    problem = tmp_path / "problem.md"
    problem.write_text("INERT problem")
    with mock.patch.object(api, "inspect_host", side_effect=AssertionError("no probe")), pytest.raises(ValueError):
        solve(problem, out=tmp_path / "run", deadline_utc=deadline)
    assert not (tmp_path / "run").exists()


def test_absolute_deadline_only_shortens_and_resume_retains_it(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n + 0 = n for integers n.")
    started = datetime.now(timezone.utc)
    later = (started + timedelta(hours=4)).isoformat()
    assert api._bounded_deadline(started, 1, later) == (started + timedelta(hours=1)).isoformat()
    deadline = (started + timedelta(minutes=2)).isoformat()
    out = tmp_path / "run"
    result = solve(problem, transport="fake", calls=3, out=out, deadline_utc=deadline)
    assert result["deadline_utc"] == deadline
    assert api.resume(out)["deadline_utc"] == deadline
    assert result["calls_used"] == 3


def test_codex_scope_default_and_capability_probe(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text("toy")
    profile = public_profile()
    with mock.patch.object(api, "inspect_host", return_value=profile), \
         mock.patch.object(api, "_mcp_names", return_value=["local_tool"]), \
         mock.patch.object(api.SingleLoop, "create") as create, \
         mock.patch.object(api, "_run", return_value={"prepared": True}):
        result = solve(problem, out=tmp_path / "run")
    assert result == {"prepared": True}
    create.assert_called_once()
    auth = json.loads((tmp_path / "run" / "authorization.json").read_text())
    assert auth["max_process_starts"] == 12
    assert auth["requested_model"] == DEFAULT_MODEL
    assert json.loads((tmp_path / "run" / "profile.json").read_text())["disabled_mcp_servers"] == ["local_tool"]
    config = json.loads((tmp_path / "run" / "prepared" / "config.json").read_text())
    assert auth["expires_at_utc"] == config["manifest"]["limits"]["stop_condition"]["deadline_utc"]


def test_default_model_and_other_capable_cli_version():
    profile = public_profile()
    profile["version"] = "9.0.0"
    profile["version_banner"] = "codex-cli 9.0.0"
    profile["probes"][0]["stdout"] = profile["version_banner"]
    for model in (None, DEFAULT_MODEL):
        args = conservative_codex_argv(profile, LAYOUT, model)
        assert "--model" not in args
    assert "specific-model" in conservative_codex_argv(profile, LAYOUT, "specific-model")


@pytest.mark.parametrize("effort", [None, "low", "medium"])
@pytest.mark.parametrize("entry", [solve, api.solve])
def test_codex_reasoning_effort_is_frozen_and_resume_never_rereads_defaults(tmp_path, effort, entry):
    from victorique.harness import codex_defaults
    from victorique.harness.codex_transport import CodexCliTransport
    problem = tmp_path / "problem.md"
    problem.write_text("INERT problem")
    out = tmp_path / "run"
    profile = public_profile()
    defaults = {"model": "configured-model", "effort": "high", "source": "cli_default_config", "status": "observed"}
    with mock.patch.object(api, "inspect_host", return_value=profile), \
         mock.patch.object(api, "_mcp_names", return_value=[]), \
         mock.patch.object(codex_defaults, "read_cli_defaults", return_value=defaults), \
         mock.patch.object(api.SingleLoop, "run"), \
         mock.patch.object(CodexCliTransport, "start_once", side_effect=AssertionError("no dispatch")), \
         redirect_stdout(io.StringIO()):
        result = entry(problem, out=out, calls=7, hours=.25, reasoning_effort=effort)
    invocation = json.loads((out / "invocation.json").read_text())
    frozen = (out / "state/run-config.json").read_bytes()
    config = json.loads(frozen)
    stored_profile = config["transport"]["profile"]
    assert invocation["cli_defaults"] == defaults
    assert invocation["parameters"]["model"] is None
    assert config["transport"]["authorization"]["requested_model"] == DEFAULT_MODEL
    assert "reasoning_effort" not in profile  # Do not mutate the observed public probe.
    assert ("reasoning_effort" in invocation["parameters"]) == (effort is not None)
    assert ("reasoning_effort" in stored_profile) == (effort is not None)
    assert stored_profile.get("reasoning_effort") == effort
    assert ("requested_reasoning_effort" in result) == (effort is not None)
    assert result.get("requested_reasoning_effort") == effort
    before = conservative_codex_argv(stored_profile, LAYOUT, None)
    with mock.patch.object(api, "inspect_host", side_effect=AssertionError("no new probe")), \
         mock.patch.object(codex_defaults, "read_cli_defaults", side_effect=AssertionError("no new defaults")), \
         mock.patch.object(api.SingleLoop, "run"), \
         mock.patch.object(CodexCliTransport, "start_once", side_effect=AssertionError("no dispatch")), \
         redirect_stdout(io.StringIO()):
        _, reopened = api._open(out)
        assert reopened.adapter.config["profile"] == stored_profile
        assert conservative_codex_argv(reopened.adapter.config["profile"], LAYOUT, None) == before
        resumed = api.resume(out)
    assert resumed.get("requested_reasoning_effort") == effort
    assert resumed["calls_used"] == 0
    assert resumed["calls_limit"] == 7
    assert resumed["deadline_utc"] == result["deadline_utc"]
    assert (out / "state/run-config.json").read_bytes() == frozen


@pytest.mark.parametrize("value,transport", [("invalid", "codex"), (False, "codex"),
                                            ([], "codex"), ("low", "claude"), ("low", "fake")])
def test_reasoning_effort_rejected_before_files_or_public_probe(tmp_path, value, transport):
    with mock.patch.object(api, "inspect_host", side_effect=AssertionError("no probe")), \
         pytest.raises(ValueError, match="reasoning_effort"):
        solve(tmp_path / "absent.md", out=tmp_path / "run", transport=transport, reasoning_effort=value)
    assert not (tmp_path / "run").exists()


def test_reported_model_and_tokens_from_cli_capture():
    rows = events()
    rows[0]["model"] = "reported-model"
    rows[-1]["usage"] = {"input_tokens": 10, "output_tokens": 3}
    result = parse_codex_capture(stdout(rows), b"", response_bytes(), process(), packet())
    assert result["actual_model"] == "reported-model"
    assert result["usage"]["input_tokens"] == 10
    assert result["status"] == "completed"


def test_public_mcp_inventory_discards_transport_details(tmp_path):
    payload = [{"name": "local_tool", "transport": {"env": {"SECRET": "do-not-retain"}}}]
    with mock.patch.object(api.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(payload).encode())) as probe:
        assert api._mcp_names("codex", tmp_path) == ["local_tool"]
    assert probe.call_args.args[0] == ["codex", "--disable", "plugins", "--disable", "apps", "mcp", "list", "--json"]
    assert list(tmp_path.iterdir()) == []
