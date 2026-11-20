"""Offline public CLI-shape fixtures and read-only report regressions."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

from victorique.harness.claude_contract import parse_claude_capture
from victorique.harness.codex_contract import parse_codex_capture
from victorique.telemetry import call_rows, token_counts, totals, write_calls
from victorique.telemetry_report import main, report
from tests.test_claude_contract import stream
from tests.test_victorique_harness_codex_contract import events, packet, process, stdout


def test_codex_native_terminal_usage_and_failed_call_are_preserved():
    rows = events(failed=True)
    rows[-1]["usage"] = {"input_tokens": 1200, "cached_input_tokens": 900,
                         "output_tokens": 60, "reasoning_output_tokens": 40}
    parsed = parse_codex_capture(stdout(rows), b"model: gpt-5.3-codex\n", None,
                                 process(exit_code=1), packet())
    assert parsed["status"] == "failed"
    assert parsed["actual_model"] == "gpt-5.3-codex"
    assert parsed["actual_model_source"] == "codex_stderr.model"
    assert parsed["usage_source"] == "codex_terminal.usage"
    tokens = token_counts(parsed["usage"], "codex_cli")
    assert tokens["input_tokens_including_cache"] == 1200
    assert tokens["reasoning_output_tokens"] == 40
    assert tokens["cache_creation_input_tokens"] is None


def test_claude_native_cache_and_thinking_usage_without_double_counting():
    def change(rows):
        rows[-1]["usage"] = {"input_tokens": 10, "output_tokens": 80,
                             "cache_creation_input_tokens": 300, "cache_read_input_tokens": 600,
                             "output_tokens_details": {"thinking_tokens": 50}}
        # This per-model duplicate must never be added to aggregate usage.
        rows[-1]["modelUsage"] = {"claude-opus-5-5": {"inputTokens": 910, "outputTokens": 80}}
    parsed = parse_claude_capture(stream(change=change), b"", process(), packet())
    tokens = token_counts(parsed["usage"], "claude_cli")
    assert tokens == {"input_tokens": 10, "output_tokens": 80, "cached_input_tokens": 600,
                      "reasoning_output_tokens": 50, "cache_creation_input_tokens": 300,
                      "input_tokens_including_cache": 910}
    assert parsed["actual_model_source"] == "claude_system_init.model"
    assert token_counts({"input_tokens": 10}, "claude_cli")["input_tokens_including_cache"] is None
    assert token_counts({"input_tokens": True, "output_tokens": -1}, "codex_cli")["input_tokens"] is None
    assert token_counts(None, "codex_cli") is None


def test_projection_records_observed_model_version_date_and_config_separately(tmp_path):
    root = tmp_path / "loop"
    root.mkdir()
    database = root / "core.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE events (sequence INTEGER, payload TEXT)")
        db.execute("CREATE TABLE jobs (job_id TEXT, claim_id TEXT)")
        db.execute("CREATE TABLE observations (job_id TEXT, phase TEXT, carrier TEXT)")
        db.execute("INSERT INTO jobs VALUES ('j', 'c')")
        carrier = {"parser": {"actual_model": "observed-model-20261101", "actual_model_source": "codex_stderr.model",
                              "usage_source": "codex_terminal.usage", "usage": {"input_tokens": 10}},
                   "process_capture": {"worker_started_at": "2026-11-13T01:00:00Z",
                                       "finished_at_utc": "2026-11-13T01:01:00Z"}}
        db.execute("INSERT INTO observations VALUES ('j', 'worker_finished', ?)", (json.dumps(carrier),))
    (tmp_path / "invocation.json").write_text(json.dumps({"cli_defaults": {"model": "configured-alias", "source": "config/read"}}))
    @contextmanager
    def connect():
        connection = sqlite3.connect(database)
        try:
            yield connection
        finally:
            connection.close()
    loop = SimpleNamespace(root=root, core=SimpleNamespace(path=database, _guard=lambda: None),
                           config={"transport": {"kind": "codex_cli", "profile": {"version": "0.167.0"}}},
                           adapter=SimpleNamespace(_db=connect))
    base = {"manifest": {"session_id": "r"}, "observations": [],
            "requests": {"q": {"claim_id": "c", "first_result_observation_id": None,
                                "phase": "failed", "record": {"role": "solve"}}}}
    row = call_rows(loop, "single", base)[0]
    assert row["status"] == "failed" and row["tokens"]["input_tokens"] == 10
    assert row["model"] == "observed-model-20261101" and row["configured_model"] == "configured-alias"
    assert row["model_observed_at_utc"] == "2026-11-13T01:01:00Z"
    assert row["cli_version"] == "0.167.0" and row["model_source"] == "codex_stderr.model"
    assert row["input_token_semantics"] == "includes_cached_input"
    assert totals([row])[0]["input_tokens_including_cache"] == 10


def call(rid, start, end, status="completed", run="run", role="solve"):
    return {"run_id": run, "request_id": rid, "role": role, "status": status,
            "started_at_utc": start, "ended_at_utc": end, "transport": "codex_cli",
            "model": "reported-model", "tokens": token_counts({"input_tokens": 10, "output_tokens": 2}, "codex_cli")}


def write(directory, rows):
    directory.mkdir()
    write_calls(directory, rows)
    return directory


def test_window_deduplication_percentiles_concurrency_and_read_only(tmp_path):
    a = call("a", "2026-11-13T00:30:00Z", "2026-11-13T01:00:00Z")
    b = call("b", "2026-11-13T01:00:00Z", "2026-11-13T01:10:00Z", "failed")
    c = call("c", "2026-11-13T01:05:00Z", "2026-11-13T02:00:00Z")
    d = call("d", "2026-11-13T01:06:00Z", "2026-11-13T01:07:00Z", "unknown")
    e = call("a", "2026-11-13T01:08:00Z", "2026-11-13T01:09:00Z", run="other", role="review")
    x = call("missing", None, None)
    x["tokens"] = x["model"] = None
    first = write(tmp_path / "first", [a, b, c, d, x])
    second = write(tmp_path / "second", [a, e])
    before = {p: p.read_bytes() for p in tmp_path.rglob("calls.jsonl")}
    data = report([first, second, first], start="2026-11-13T01:00:00Z", end="2026-11-13T02:00:00Z")
    assert data["input_calls"] == 6 and data["duplicate_rows"] == 1
    assert data["overall"]["calls"] == 4 and data["overall"]["completed_calls_per_hour"] == 2
    assert data["overall"]["failed_calls"] == 1 and data["overall"]["unknown_calls"] == 1
    assert data["peak_concurrency"] == 3 and data["input_missing_interval_calls"] == 1
    assert data["input_missing_end_calls"] == 1
    assert data["by_role"]["solve"]["elapsed_seconds"] == {"n": 3, "median": 600, "p90": 1800}
    assert {p: p.read_bytes() for p in tmp_path.rglob("calls.jsonl")} == before
    assert sorted(p.name for p in first.iterdir()) == ["calls.jsonl"]


def test_no_window_does_not_invent_throughput_and_legacy_missing_counts(tmp_path):
    row = call("a", None, None, "timed_out")
    row["tokens"] = {"input_tokens": 7}
    row["model"] = None
    folder = write(tmp_path / "run", [row])
    data = report([folder])
    assert data["window"] is None and data["overall"]["completed_calls_per_hour"] is None
    assert data["overall"]["failed_calls"] == 1 and data["overall"]["timed_out_calls"] == 1
    assert data["overall"]["missing_model_calls"] == 1
    assert data["overall"]["missing_tokens"]["output_tokens"] == 1
    assert data["overall"]["token_totals"]["input_tokens"] == 7
    assert data["overall"]["token_totals"]["input_tokens_including_cache"] is None
    assert totals([row])[0]["input_tokens_including_cache"] is None


def test_conflicting_copy_is_rejected_and_cross_run_ids_are_distinct(tmp_path):
    a = call("same", None, None)
    first = write(tmp_path / "a", [a])
    second = write(tmp_path / "b", [a | {"status": "failed"}])
    with pytest.raises(ValueError, match="conflicting duplicate"):
        report([first, second])
    write_calls(second, [a | {"run_id": "other"}])
    assert report([first, second])["overall"]["calls"] == 2


@pytest.mark.parametrize("start,end", [("2026-11-13T01:00:00Z", None),
                                      ("2026-11-13T01:00:00", "2026-11-13T02:00:00Z"),
                                      ("2026-11-13T01:00:00Z", "2026-11-13T01:00:00Z")])
def test_invalid_windows_are_rejected(start, end):
    with pytest.raises(ValueError):
        report([], start=start, end=end)


def test_cli_reports_json_without_files_or_model_dispatch(tmp_path, capsys):
    folder = write(tmp_path / "run", [call("q", None, None, "unknown")])
    main([str(folder)])
    assert json.loads(capsys.readouterr().out)["overall"]["unknown_calls"] == 1
    assert list(folder.iterdir()) == [folder / "calls.jsonl"]


def test_missing_files_and_malformed_rows_fail_explicitly(tmp_path):
    with pytest.raises(OSError):
        report([tmp_path])
    folder = write(tmp_path / "run", [{"tokens": []}])
    with pytest.raises(ValueError, match="invalid call JSON"):
        report([folder])
