import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import sqlite3
from unittest import mock

import pytest

from victorique import api
from victorique.fake import FakeTransport
from victorique.telemetry import TOKEN_KEYS, call_rows, totals
from tests.test_single import problem_file


CALL_FIELDS = {"run_id", "request_id", "retry_of", "policy", "rung", "role", "model", "effort",
               "started_at_utc", "ended_at_utc", "status", "tokens", "configured_model", "configured_effort",
               "transport", "model_source", "model_observed_at_utc", "cli_version", "configured_model_source",
               "token_source", "input_token_semantics", "service", "requested_model", "requested_reasoning_effort"}
from victorique.call_evidence import evidence
CALL_FIELDS |= set(evidence())


def test_summary_and_call_output_contract(tmp_path):
    out = tmp_path / "run"
    result = api.solve(problem_file(tmp_path), policy="single", transport="fake", out=out)
    assert set(result) == {"run_id", "policy", "outcome", "endpoint_reason", "calls_used", "calls_limit", "wall_seconds",
                           "deadline_utc", "transport", "synthetic", "requested_model", "reported_model",
                           "cli_version", "cli_defaults", "tokens", "proof_available", "output_directory",
                           "timeout_retries_per_call", "timeout_retries_used", "check_service"}
    assert result["endpoint_reason"] == "single_complete"
    assert result["timeout_retries_per_call"] == 1 and result["timeout_retries_used"] == 0
    assert result["check_service"] is None
    assert json.loads((out / "summary.json").read_text()) == result
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert len(rows) == result["calls_used"] == 3
    assert [row["role"] for row in rows] == ["solver", "alignment_reviewer", "proof_reviewer"]
    assert len({row["request_id"] for row in rows}) == 3
    assert rows[0]["review_output"] is None
    assert rows[1]["review_output"]["findings"] == []
    assert rows[1]["issue_lists"] == {"findings": [], "uncertainties": []}
    for row in rows:
        assert set(row) == CALL_FIELDS
        assert row["run_id"] == result["run_id"]
        assert row["policy"] == "single" and row["rung"] is None and row["retry_of"] is None
        assert row["service"] == "solver" and row["requested_model"] is row["requested_reasoning_effort"] is None
        assert row["model"] is row["effort"] is row["tokens"] is None
        assert row["model_source"] is row["model_observed_at_utc"] is row["cli_version"] is None
        assert row["token_source"] is row["input_token_semantics"] is None
        assert row["status"] == "completed"
        assert datetime.fromisoformat(row["started_at_utc"]) <= datetime.fromisoformat(row["ended_at_utc"])


def test_unknown_call_is_retained_without_duplicate_or_invented_usage(tmp_path):
    out = tmp_path / "run"
    with mock.patch.object(FakeTransport, "start_once", side_effect=RuntimeError("lost connection")), \
         mock.patch.object(FakeTransport, "observe_existing", return_value=None):
        result = api.solve(problem_file(tmp_path), policy="single", transport="fake", out=out)
        before = (out / "calls.jsonl").read_text()
        api.resume(out)
        api.export(out)
    assert (out / "calls.jsonl").read_text() == before
    rows = [json.loads(line) for line in before.splitlines()]
    assert len(rows) == result["calls_used"] == 1
    assert rows[0]["status"] == "unknown"
    assert rows[0]["ended_at_utc"] is None
    assert rows[0]["model"] is rows[0]["effort"] is rows[0]["tokens"] is None
    assert result["tokens"] is None


@pytest.mark.parametrize("entry", ["solve", "verify"])
def test_codex_defaults_are_saved_before_dispatch(tmp_path, entry):
    from victorique import verify
    from victorique.harness import codex_defaults
    from tests.test_victorique_harness_codex_contract import public_profile
    out = tmp_path / "defaults"
    problem = problem_file(tmp_path)
    proof = tmp_path / "proof.md"
    proof.write_text("The additive identity proves the claim.")
    defaults = {"model": "configured-model", "effort": "high", "source": "cli_default_config",
                "observed_at_utc": "2026-11-13T00:00:00+00:00", "status": "observed"}
    module = api if entry == "solve" else verify
    loop_type = api.OriginalProblemLoop if entry == "solve" else verify.VerifyLoop
    def check_saved(directory, loop):
        assert json.loads((directory / "invocation.json").read_text())["cli_defaults"] == defaults
        return "prepared"
    with mock.patch.object(module, "inspect_host", return_value=public_profile()), \
         mock.patch.object(module, "_mcp_names", return_value=[]), \
         mock.patch.object(codex_defaults, "read_cli_defaults", return_value=defaults) as read, \
         mock.patch.object(loop_type, "create"), mock.patch.object(module, "_run", side_effect=check_saved):
        result = api.solve(problem, policy="full", out=out) if entry == "solve" else verify.verify(proof, problem=problem, out=out)
    assert result == "prepared"
    read.assert_called_once_with(public_profile()["binary"], out)


def test_calls_written_after_each_dispatch(tmp_path):
    out = tmp_path / "run"
    start = FakeTransport.start_once
    seen = []

    def observe_output(self, request):
        rows = (out / "calls.jsonl").read_text().splitlines()
        seen.append(len(rows))
        return start(self, request)

    with mock.patch.object(FakeTransport, "start_once", observe_output):
        api.solve(problem_file(tmp_path), policy="single", transport="fake", out=out)
    # Both independent gates start against the previous completed batch.
    assert seen == [0, 1, 1]
    assert len((out / "calls.jsonl").read_text().splitlines()) == 3


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("corrected", [False, True])
def test_cli_records_project_per_call_and_totals_without_double_counting(tmp_path, missing, corrected):
    out = tmp_path / "run"
    api.solve(problem_file(tmp_path), policy="single", transport="fake", out=out)
    _, loop = api._open(out)
    base = loop.core.snapshot()["exchange"]
    loop.config = deepcopy(loop.config)
    loop.config["transport"]["kind"] = "codex_cli"
    invocation = json.loads((out / "invocation.json").read_text())
    invocation["cli_defaults"] = {"model": "configured-only", "effort": "medium", "source": "cli_default_config"}
    (out / "invocation.json").write_text(json.dumps(invocation))
    # In-memory copies of the transport's public capture columns; no CLI launch.
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("CREATE TABLE jobs(job_id TEXT,claim_id TEXT);"
                        "CREATE TABLE observations(job_id TEXT,phase TEXT,carrier TEXT);")
        for index, row in enumerate(base["requests"].values()):
            db.execute("INSERT INTO jobs VALUES (?,?)", (str(index), row["claim_id"]))
            db.execute("INSERT INTO observations VALUES (?,?,?)", (str(index), "pending", "{}"))
            if missing and index == 2:
                continue
            carrier = {"parser": {"actual_model": "cli-reported-model", "usage": {
                "input_tokens": 10 + index, "output_tokens": 3, "cached_input_tokens": 0,
                "reasoning_output_tokens": None}},
                "process_capture": {"worker_started_at": "2026-11-13T00:01:00+00:00",
                                    "finished_at_utc": "2026-11-13T00:02:00+00:00", "timed_out": index == 1},
                "streams_base64": {"stderr": base64.b64encode(b"reasoning effort: high\n").decode()}}
            if corrected:
                from victorique.harness.codex_transport import REDECODE_PHASE
                db.execute("INSERT INTO observations VALUES (?,?,?)", (str(index), REDECODE_PHASE, json.dumps(carrier)))
                # The original row remains present even when inserted later;
                # it cannot hide the explicitly recovered carrier projection.
                db.execute("INSERT INTO observations VALUES (?,?,?)", (str(index), "worker_finished", "{}"))
            else:
                db.execute("INSERT INTO observations VALUES (?,?,?)", (str(index), "worker_finished", json.dumps(carrier)))

        @contextmanager
        def transport_db():
            yield db

        loop.adapter = mock.Mock(_db=transport_db)
        rows = call_rows(loop, "single", base)
        assert len(rows) == 3
        assert rows[0]["model"] == "cli-reported-model"
        assert rows[0]["effort"] == "high"
        assert rows[0]["configured_model"] == "configured-only"
        assert rows[0]["configured_effort"] == "medium"
        assert rows[0]["tokens"]["input_tokens"] == 10
        assert rows[0]["started_at_utc"] == "2026-11-13T00:01:00+00:00"
        assert rows[0]["ended_at_utc"] == "2026-11-13T00:02:00+00:00"
        assert rows[1]["status"] == "timed_out"
        usage, model = totals(rows)
        assert set(usage) == set(TOKEN_KEYS)
        if missing:
            assert rows[2]["tokens"] is rows[2]["model"] is rows[2]["effort"] is None
            assert all(value is None for value in usage.values())
            assert model is None
        else:
            assert usage == {"input_tokens": 33, "output_tokens": 9,
                             "cached_input_tokens": 0, "reasoning_output_tokens": None,
                             "cache_creation_input_tokens": None, "input_tokens_including_cache": 33}
            assert model == "cli-reported-model"
        summary = api._summary(out, loop, loop.core.snapshot(), rows)
        assert summary["tokens"] == usage
        assert summary["reported_model"] == model
        assert summary["cli_defaults"] == invocation["cli_defaults"]
    finally:
        db.close()


@pytest.mark.parametrize("invalid", [False, True])
def test_gate_projection_preserves_all_findings_and_recovers_archived_invalid_output(tmp_path, invalid):
    original = FakeTransport._response
    expected = {}
    def response(self, request):
        envelope = json.loads(original(self, request))
        output = envelope["output"]
        if "findings" in output:
            output.update(outcome="fail", findings=[{
                "schema": "victorique.adaptive_defect.v1", "issue_id": "style-issue",
                "target_ref": output["target_ref"], "kind": "invalid_step", "severity": "minor",
                "location": None, "claim": "A convention or style concern."}])
            if invalid:
                output["extra_unaccepted_field"] = "original output is still evidence"
            expected[request.packet["binding"]["action_kind"]] = deepcopy(output)
        return json.dumps(envelope).encode()
    out = tmp_path / "run"
    with mock.patch.object(FakeTransport, "_response", response):
        api.solve(problem_file(tmp_path), policy="single", transport="fake", out=out)
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    gates = [row for row in rows if row["review_output"] is not None]
    assert gates
    for row in gates:
        output = row["review_output"]
        assert output == expected[output["gate_id"]]
        assert row["issue_lists"]["findings"] == output["findings"]
        assert row["issue_lists"]["findings"][0]["severity"] == "minor"
        assert row["status"] == ("returned_invalid" if invalid else "completed")
    before = (out / "calls.jsonl").read_bytes()
    api.export(out)
    assert (out / "calls.jsonl").read_bytes() == before
