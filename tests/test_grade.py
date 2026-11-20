import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from victorique.grading import GRADE_SCHEMA, GradingIncomplete, RUBRIC, grade_proof
from victorique.harness import grade
from victorique.harness.codex_contract import conservative_codex_argv
from victorique.harness.codex_transport import CodexFixtureTransport, SCHEMA, VERSION
from victorique.harness.safety import json_bytes, write_new
from tests.test_victorique_harness_codex_contract import public_profile


def request():
    captured = []
    grade_proof(lambda value: captured.append(value) or {"score": 7, "reason": "fixture"},
                problem="Problem text", candidate="Candidate text", reference_solution="Reference text")
    return captured[0]


@pytest.mark.parametrize("deadline", ["2001-01-01T00:00:00Z", "2028-01-01T00:00:00", "2028-01-01 00:00:00Z"])
def test_grade_absolute_deadline_rejects_before_probe_and_directory(tmp_path, monkeypatch, deadline):
    monkeypatch.setattr(grade, "inspect_host", lambda *a: pytest.fail("host probe"))
    with pytest.raises(ValueError):
        grade.grade_reference(request(), directory=tmp_path / "grade", deadline_utc=deadline)
    assert not (tmp_path / "grade").exists()


def test_grade_absolute_deadline_is_frozen_and_resume_does_not_extend(tmp_path):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    directory = tmp_path / "grade"
    result = grade.grade_reference(request(), directory=directory, transport="fake", seconds=60, deadline_utc=deadline)
    assert result["deadline_utc"] == deadline
    assert grade.resume_grade(directory)["deadline_utc"] == deadline


def test_grade_codex_forwards_deadline(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(grade, "grade_reference", lambda value, **kwargs: seen.append(kwargs) or {})
    grade.grade_codex(request(), directory=tmp_path / "grade", deadline_utc="2028-01-01T00:00:00Z")
    assert seen[0]["deadline_utc"] == "2028-01-01T00:00:00Z"


def fixture(monkeypatch, tmp_path, *, score=7, scenario="success", seconds=3):
    monkeypatch.delenv("VICTORIQUE_CODEX_BINARY", raising=False)
    monkeypatch.setattr(grade, "inspect_host", lambda host: public_profile())
    monkeypatch.setattr(grade, "_cli_defaults", lambda binary, cwd: {
        "model": "fixture-default", "effort": "high", "source": "cli_default_config",
        "status": "observed", "observed_at_utc": "2026-11-13T01:00:00+00:00"})
    monkeypatch.setattr("victorique.api._mcp_names", lambda binary, directory: ["inert-server"])
    staging = tmp_path / "outside"
    staging.mkdir()
    monkeypatch.setattr(grade.tempfile, "mkdtemp", lambda **kwargs: str(staging))
    observed = []
    def create(root, profile, authorization, packet, timeout, workspace_root):
        observed.append((profile, authorization, packet))
        adapter = CodexFixtureTransport._create(root, {
            "schema": SCHEMA, "version": VERSION, "mode": "synthetic", "profile": None,
            "authorization": None, "output_schemas": {"reference_grade": GRADE_SCHEMA},
            "timeout_seconds": seconds, "max_capture_bytes": 131072, "scenario": scenario,
            "delay_seconds": 0, "workspace_root": str(workspace_root)})
        write_new(root / "inert-response.bin", json_bytes({
            "schema": packet["response_contract"]["schema"], "binding": packet["binding"],
            "output": {"score": score, "reason": "Fixture score"}}))
        return adapter
    monkeypatch.setattr(grade, "_new_adapter", create)
    return observed


def finish(directory):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        record = grade._read(directory / "request.json")
        adapter, dispatch = grade._open(record, directory)
        capture = adapter.runner.observe_existing(adapter._binding(dispatch))
        if capture and capture["worker_finished"]:
            return grade.resume_grade(directory, wait=False)
        time.sleep(.02)
    pytest.fail("fixture worker did not finish")


def test_one_durable_grade_isolated_context_budget_and_read_only_resume(monkeypatch, tmp_path):
    seen = fixture(monkeypatch, tmp_path)
    directory = tmp_path / "grade"
    result = grade.grade_codex(request(), directory=directory, seconds=10)
    assert result["status"] == "completed" and result["score"] == 7 and result["calls_used"] == 1
    assert result["model"] == "fixture-default" and result["model_source"] == "cli_default_config"
    assert result["effort"] == "high" and result["effort_source"] == "cli_default_config"
    profile, authorization, packet = seen[0]
    assert set(packet["context"]) == {"problem", "candidate", "reference_solution", "guidelines", "rubric", "instructions"}
    assert packet["context"]["rubric"] == RUBRIC
    assert authorization["max_process_starts"] == 1
    assert result["deadline_utc"] == authorization["expires_at_utc"]
    record = grade._read(directory / "request.json")
    adapter, dispatch = grade._open(record, directory)
    argv = conservative_codex_argv(profile, adapter.runner.layout(adapter._binding(dispatch)), None)
    assert "project_doc_max_bytes=0" in argv and '"inert-server"={enabled=false}' in " ".join(argv)
    assert "--ephemeral" in argv and "--sandbox" in argv and "read-only" in argv
    assert "--model" not in argv and "resume" not in argv
    monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("restarted"))
    assert grade.resume_grade(directory)["score"] == 7
    with pytest.raises(FileExistsError):
        grade.grade_codex(request(), directory=directory)
    with adapter.runner._db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize("scenario,score,status", [("success", 8, "failed"), ("no-final", 7, "unknown"),
                                                 ("nonzero", 7, "failed"), ("timeout", 7, "timeout")])
def test_invalid_unknown_failure_timeout_keep_original_attempt(monkeypatch, tmp_path, scenario, score, status):
    fixture(monkeypatch, tmp_path, score=score, scenario=scenario, seconds=.15 if scenario == "timeout" else 3)
    directory = tmp_path / "grade"
    grade.grade_codex(request(), directory=directory, seconds=3)
    result = finish(directory)
    assert result["status"] == status and result["calls_used"] == 1
    assert result["score"] is None
    monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("restarted"))
    assert grade.resume_grade(directory, wait=False)["status"] == status


def test_unknown_preserves_reported_usage_and_model(monkeypatch, tmp_path):
    fixture(monkeypatch, tmp_path, scenario="no-final")
    directory = tmp_path / "grade"
    grade.grade_codex(request(), directory=directory, seconds=3)
    finish(directory)
    record = grade._read(directory / "request.json")
    adapter, dispatch = grade._open(record, directory)
    observation = adapter.observe_existing(dispatch)
    carrier = json.loads(observation.raw_transport)
    carrier["parser"].update(usage={"input_tokens": 10, "output_tokens": 2}, actual_model="fixture-model")
    from victorique.harness_bridge import TransportObservation
    monkeypatch.setattr(adapter, "observe_existing", lambda request: TransportObservation(
        observation.metadata, observation.raw_response, json_bytes(carrier)))
    result, finished = grade._observe(adapter, dispatch)
    assert finished and result["tokens"] == {"input_tokens": 10, "output_tokens": 2}
    assert result["model"] == "fixture-model" and result["status"] == "unknown"
    monkeypatch.setattr(grade, "_open", lambda record, directory=None: (adapter, dispatch))
    final = grade.resume_grade(directory, wait=False)
    assert final["model"] == "fixture-model" and final["model_source"] == "cli_reported"
    with pytest.raises(GradingIncomplete) as error:
        grade_proof(lambda request: result, problem="p", candidate="c", reference_solution="r")
    assert error.value.result["tokens"]["input_tokens"] == 10


def test_request_rejects_extra_history_and_invalid_deadline(tmp_path):
    with pytest.raises(ValueError, match="fixed grading request"):
        grade.grade_codex(dict(request(), solver_reviews="secret"), directory=tmp_path / "grade")
    assert not (tmp_path / "grade").exists()
    for seconds in (0, True, float("nan"), 86401):
        with pytest.raises(ValueError, match="grading seconds"):
            grade.grade_codex(request(), directory=tmp_path / "grade", seconds=seconds)


def test_probe_failure_has_no_model_charge(monkeypatch, tmp_path):
    fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(grade, "inspect_host", lambda host: (_ for _ in ()).throw(ValueError("no CLI")))
    result = grade.grade_codex(request(), directory=tmp_path / "grade")
    assert result["status"] == "failed" and result["calls_used"] == 0
    assert (tmp_path / "grade" / "request.json").exists()
    assert grade.resume_grade(tmp_path / "grade") == result


def test_explicit_native_codex_binary_matches_solve_and_verify(monkeypatch, tmp_path):
    observed = fixture(monkeypatch, tmp_path)
    selected = str(tmp_path / "selected-native-codex.exe")
    monkeypatch.setenv("VICTORIQUE_CODEX_BINARY", selected)
    probes = []
    def inspect(host, binary):
        probes.append((host, binary))
        profile = public_profile()
        profile["binary"] = binary
        return profile
    monkeypatch.setattr(grade, "inspect_host", inspect)
    result = grade.grade_codex(request(), directory=tmp_path / "grade", seconds=3)
    assert result["status"] == "completed"
    assert probes == [("codex", selected)]
    assert observed[0][0]["binary"] == selected and observed[0][1]["binary"] == selected


def test_unavailable_default_is_not_an_observed_model(monkeypatch, tmp_path):
    fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(grade, "_cli_defaults", lambda *args: {"status": "unavailable", "model": None,
                                                            "effort": None, "source": "cli_default_config"})
    result = grade.grade_codex(request(), directory=tmp_path / "grade", seconds=3)
    assert result["status"] == "completed" and result["model"] is None and result["model_source"] is None


def test_crash_after_fence_recovers_without_restart(monkeypatch, tmp_path):
    fixture(monkeypatch, tmp_path)
    original = CodexFixtureTransport.start_once
    def crash(adapter, request):
        original(adapter, request)
        raise RuntimeError("coordinator lost acknowledgement")
    monkeypatch.setattr(CodexFixtureTransport, "start_once", crash)
    directory = tmp_path / "grade"
    result = grade.grade_codex(request(), directory=directory, seconds=3)
    assert result["calls_used"] == 1
    monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("restarted"))
    assert finish(directory)["status"] == "completed"


def test_completed_grade_survives_removal_of_external_cwd(monkeypatch, tmp_path):
    fixture(monkeypatch, tmp_path)
    directory = tmp_path / "grade"
    grade.grade_codex(request(), directory=directory, seconds=3)
    finish(directory)
    record = grade._read(directory / "request.json")
    assert record["transport_directory"] == "transport"
    adapter, dispatch = grade._open(record, directory)
    capture = adapter.runner.observe_existing(adapter._binding(dispatch))
    layout = adapter.runner.layout(adapter._binding(dispatch))
    from pathlib import Path
    assert Path(layout["workspace"]).is_relative_to(tmp_path / "outside")
    for key in ("final_file", "schema_file", "stdout_file", "stderr_file"):
        assert Path(layout[key]).is_relative_to(directory / "transport")
    assert capture["worker_finished"] and adapter.path.is_relative_to(directory / "transport")
    # Empty execution directories may already have been removed by the worker.
    cwd = Path(layout["workspace"])
    if cwd.exists():
        # The INERT executable alone writes this input-observation fixture.
        assert {path.name for path in cwd.iterdir()} == {"stdin-observed.bin"}
        (cwd / "stdin-observed.bin").unlink()
        cwd.rmdir()
    if (tmp_path / "outside").exists():
        (tmp_path / "outside").rmdir()
    monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("restarted"))
    assert grade.resume_grade(directory)["score"] == 7
    # Existing requests with an absolute transport path remain readable.
    record["transport_directory"] = str(directory / "transport")
    (directory / "request.json").write_bytes(json_bytes(record))
    assert grade.resume_grade(directory)["score"] == 7


def test_grade_projection_keeps_original_reason_and_unknown_issues_without_redispatch(monkeypatch, tmp_path):
    fixture(monkeypatch, tmp_path)
    directory = tmp_path / "grade"
    result = grade.grade_codex(request(), directory=directory, seconds=3)
    assert result["review_output"] == {"score": 7, "reason": "Fixture score"}
    assert result["reason"] == "Fixture score" and result["issue_lists"] is None
    assert result["capture_settings"]["max_capture_bytes"] == 131072
    assert result["effective_output_token_limit"] is None
    assert result["output_limit_reached"] is None
    assert result["capture_truncated"] is False
    assert result["capture_streams"]["stdout"]["captured_bytes"] > 0
    before = (directory / "request.json").read_bytes()
    # Emulate an old public projection; original durable response stays intact.
    old = {key: value for key, value in result.items() if key not in grade.evidence()}
    (directory / "result.json").write_bytes(json_bytes(old))
    monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("restarted"))
    restored = grade.resume_grade(directory, wait=False)
    assert restored["review_output"] == result["review_output"]
    assert restored["issue_lists"] is None
    assert (directory / "request.json").read_bytes() == before
    rows = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
    record = json.loads(before)
    assert len(rows) == 1 and rows[0]["role"] == "reference_grader"
    assert rows[0]["run_id"] == record["packet"]["binding"]["session_id"]
    assert rows[0]["request_id"] == record["packet"]["binding"]["request_id"]
    assert rows[0]["job_id"] == record["job_id"]
    assert rows[0]["review_output"] == result["review_output"]
    grade.resume_grade(directory, wait=False)
    assert len((directory / "calls.jsonl").read_text().splitlines()) == 1


def test_invalid_grade_preserves_all_raw_issue_severities_without_changing_schema(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    output = {"score": 7, "reason": "Original raw reason", "findings": [
        {"severity": severity, "claim": severity + " original concern"} for severity in ("minor", "material", "fatal")]}
    carrier = {"parser": {}, "process_capture": {"worker_finished": True}}
    observation = SimpleNamespace(metadata={"status": "completed"}, raw_response=json_bytes({"output": output}),
                                  raw_transport=json_bytes(carrier))
    adapter = SimpleNamespace(config={"max_capture_bytes": 4096, "timeout_seconds": 2},
                              observe_existing=lambda request: observation,
                              runner=SimpleNamespace(observe_existing=lambda binding: {"worker_finished": True}),
                              _binding=lambda request: {})
    spy = Mock(wraps=grade.evidence)
    monkeypatch.setattr(grade, "evidence", spy)
    result, finished = grade._observe(adapter, object())
    assert finished and result["status"] == "failed" and result["score"] is None
    assert result["review_output"] == output
    assert result["issue_lists"]["findings"] == output["findings"]
    assert set(GRADE_SCHEMA["properties"]) == {"score", "reason"}
    assert spy.call_args.args == (carrier, adapter.config, output)


def test_grade_without_registered_observation_keeps_unknown_evidence():
    from types import SimpleNamespace
    adapter = SimpleNamespace(config={}, observe_existing=lambda request: None)
    result, finished = grade._observe(adapter, object())
    assert finished and result["calls_used"] == 0
    assert result["review_output"] is result["issue_lists"] is None
    assert result["capture_truncated"] is result["effective_output_token_limit"] is None


@pytest.mark.parametrize("kind", ["setup_error", "callable"])
def test_old_grade_without_raw_transport_exports_null_metadata(tmp_path, kind):
    directory = tmp_path / "grade"
    directory.mkdir()
    record = {"setup_error": "no host"} if kind == "setup_error" else {"transport": "callable"}
    (directory / "request.json").write_bytes(json_bytes(record))
    (directory / "result.json").write_bytes(json_bytes({"status": "failed", "reason": "Original reason"}))
    result = grade.resume_grade(directory, wait=False)
    assert result["reason"] == "Original reason"
    assert result["review_output"] is result["issue_lists"] is None
    assert result["finish_reason"] is result["capture_truncated"] is None
    assert (directory / "calls.jsonl").read_text() == ""


def test_unknown_grade_projection_rebuilds_original_single_attempt_and_normalizes_usage(tmp_path):
    record = {"packet": {"binding": {"session_id": "original-session", "request_id": "original-request"}},
              "job_id": "original-job", "transport": "codex"}
    result = {"status": "unknown", "calls_used": 1, "tokens": {"input_tokens": 17, "output_tokens": 4},
              **grade.evidence()}
    grade._export_result(tmp_path, record, result)
    grade._export_result(tmp_path, record, result)
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    assert rows[0]["request_id"] == "original-request" and rows[0]["job_id"] == "original-job"
    assert rows[0]["tokens"]["input_tokens_including_cache"] == 17
    assert rows[0]["capture_truncated"] is None
    assert result["tokens"] == {"input_tokens": 17, "output_tokens": 4}
