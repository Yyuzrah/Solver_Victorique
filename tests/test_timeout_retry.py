"""A call that stops at the per-call time limit is sent again, at most timeout_retries times.

The synthetic transport below answers like the fake transport, except that the
first call of each listed kind stops at its time limit. The offline CLIs run the
real Codex and Claude transports with fixed local Python prints; a stalled call
is stopped by the transport's own per-call limit.
"""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import time
from unittest import mock

import pytest

from victorique import api, bench, cli, retries, settings, solve, verify
from victorique.fake import FakeTransport
from victorique.harness.claude_transport import ClaudeCliTransport
from victorique.harness.codex_transport import CodexCliTransport
from victorique.harness_bridge import OBSERVATION, TransportObservation

PROBLEM = "Prove that n + 0 = n for every integer n."


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch):
    monkeypatch.delenv(settings.CONFIG_ENVIRONMENT, raising=False)


class StopsAtLimit(FakeTransport):
    """Fixed answers; the first ``times`` calls of each listed kind stop at their time limit."""

    def __init__(self, *kinds, times=1, delay=0):
        self.kinds, self.times, self.delay = kinds, times, delay
        self.counts, self.stopped, self.settlements, self.calls = {}, {}, {}, []

    def _metadata(self, request, status, observation_id, observed_at):
        return {"schema": OBSERVATION, "observation_id": observation_id, "job_id": request.job_id,
                "core_claim_id": request.core_claim_id, "packet_id": request.packet["binding"]["packet_id"],
                "adapter_id": "fake", "adapter_version": "v1", "mode": "synthetic", "status": status,
                "terminal_observed": status != "unknown", "host_reference": "/fake/" + request.job_id,
                "observed_at_utc": observed_at, "source_event_id": None, "source_line": None,
                "model_observation": None, "charge_observation": None}

    def start_once(self, request):
        kind = request.packet["binding"]["action_kind"]
        self.calls.append((request.packet["binding"]["request_id"], kind))
        if kind in self.kinds and self.counts.get(kind, 0) < self.times:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if self.delay:
                time.sleep(self.delay)
            now = datetime.now(timezone.utc).isoformat()
            self.stopped[request.job_id] = TransportObservation(
                self._metadata(request, "unknown", "fake." + request.job_id, now), None,
                b"Synthetic call stopped at its time limit")
        return self.stopped.get(request.job_id) or super().start_once(request)

    def observe_existing(self, request):
        return self.stopped.get(request.job_id) or super().observe_existing(request)

    def timeout_observation(self, request):
        stopped = self.stopped.get(request.job_id)
        if stopped is None:
            return None
        if request.job_id not in self.settlements:
            self.settlements[request.job_id] = TransportObservation(self._metadata(
                request, "failed", "timeout_" + request.job_id, stopped.metadata["observed_at_utc"]), None,
                b"Synthetic settlement")
        return self.settlements[request.job_id]


def problem_file(tmp_path):
    path = tmp_path / "problem.md"
    path.write_text(PROBLEM, encoding="utf-8")
    return path


def run(tmp_path, adapter, name="run", **options):
    out = tmp_path / name
    with redirect_stdout(io.StringIO()):
        directory, loop = api._create_run(problem_file(tmp_path), out=out, transport="fake", _adapter=adapter, **options)
        summary = api._run(directory, loop)
    return out, summary


def rows(out):
    return [json.loads(line) for line in (out / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def resume_with(adapter, out, monkeypatch):
    monkeypatch.setattr(api, "FakeTransport", lambda: adapter)
    with redirect_stdout(io.StringIO()):
        return api.resume(out)


# ---- the setting ------------------------------------------------------------------


def test_setting_default_bounds_and_settings_file(tmp_path):
    assert settings.TIMEOUT_RETRIES == 1 and settings.timeout_retries() == 1
    assert [settings.timeout_retries(value) for value in (0, 1, 2, 3)] == [0, 1, 2, 3]
    for value in (-1, 4, True, "1", 1.0):
        with pytest.raises(ValueError, match="timeout_retries"):
            settings.timeout_retries(value)
    path = tmp_path / "victorique.toml"
    path.write_text("timeout_retries = 2\n", encoding="utf-8")
    assert settings.load_config(path) == {"timeout_retries": 2}
    assert settings.command_settings(path, timeout_retries=None) == {"timeout_retries": 2}
    assert settings.command_settings(path, timeout_retries=0) == {"timeout_retries": 0}
    path.write_text("timeout_retries = 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timeout_retries"):
        settings.load_config(path)


def test_command_line_option_over_settings_file_over_default(tmp_path, monkeypatch):
    config = tmp_path / "settings.toml"
    config.write_text("timeout_retries = 2\n", encoding="utf-8")
    for extra, expected in (([], None), (["--timeout-retries", "0"], 0), (["--config", str(config)], 2),
                            (["--config", str(config), "--timeout-retries", "3"], 3)):
        for command, target, arguments in (("solve", (api, "solve"), ["problem.md"]),
                                           ("bench", (bench, "bench"), ["problems"])):
            with mock.patch("sys.argv", ["victorique", command, *arguments, *extra]), \
                 mock.patch.object(*target, return_value={}) as called, redirect_stdout(io.StringIO()):
                cli.main()
            assert called.call_args.kwargs["timeout_retries"] == expected
    # verify makes no solving calls and has no such option.
    with mock.patch("sys.argv", ["victorique", "verify", "proof.md", "--problem", "p.md", "--timeout-retries", "1"]), \
         pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2


def test_invalid_values_stop_before_a_run_starts(tmp_path):
    out = tmp_path / "run"
    with mock.patch.object(api, "inspect_host", side_effect=AssertionError("no probe")):
        for value in (-1, 4, "1"):
            with pytest.raises(ValueError, match="timeout_retries"):
                solve(problem_file(tmp_path), out=out, timeout_retries=value)
        with mock.patch("sys.argv", ["victorique", "solve", str(problem_file(tmp_path)), "--out", str(out),
                                     "--timeout-retries", "9"]), pytest.raises(SystemExit) as stopped:
            cli.main()
    assert stopped.value.code == 2 and not out.exists()


# ---- single ------------------------------------------------------------------------


def test_single_sends_a_timed_out_solve_again_and_delivers(tmp_path, monkeypatch):
    adapter = StopsAtLimit("solve_obligation")
    out, summary = run(tmp_path, adapter)
    assert summary["policy"] == "single" and summary["outcome"] == "complete"
    assert summary["endpoint_reason"] == "single_complete"
    assert summary["calls_used"] == 4 and summary["calls_limit"] == 12
    assert summary["timeout_retries_per_call"] == 1 and summary["timeout_retries_used"] == 1
    calls = rows(out)
    assert [(row["request_id"], row["retry_of"], row["status"]) for row in calls] == [
        ("single.solve", None, "timed_out"),
        ("single.solve.timeout-retry-1", "single.solve", "completed"),
        ("single.alignment", None, "completed"), ("single.review", None, "completed")]
    assert json.loads((out / "invocation.json").read_text())["parameters"]["timeout_retries"] == 1
    assert "n + 0 = n" in (out / "proof.md").read_text(encoding="utf-8")
    # Resume observes the finished run and sends nothing.
    assert resume_with(adapter, out, monkeypatch) == summary and len(adapter.calls) == 4


def test_without_retries_the_run_ends_at_the_timeout_as_before(tmp_path, monkeypatch):
    adapter = StopsAtLimit("solve_obligation")
    out, summary = run(tmp_path, adapter, timeout_retries=0)
    assert summary["outcome"] == "pending" and summary["endpoint_reason"] is None
    assert summary["calls_used"] == 1 and summary["timeout_retries_used"] == 0
    assert summary["timeout_retries_per_call"] == 0
    assert [(row["request_id"], row["retry_of"]) for row in rows(out)] == [("single.solve", None)]
    again = resume_with(adapter, out, monkeypatch)
    assert again["outcome"] == "pending" and again["calls_used"] == 1 and len(adapter.calls) == 1


def test_a_second_timeout_ends_the_run_once_the_retries_are_used(tmp_path):
    adapter = StopsAtLimit("solve_obligation", times=2)
    out, summary = run(tmp_path, adapter)
    assert summary["outcome"] == "pending" and summary["calls_used"] == 2
    assert [row["request_id"] for row in rows(out)] == ["single.solve", "single.solve.timeout-retry-1"]
    adapter = StopsAtLimit("solve_obligation", times=2)
    out, summary = run(tmp_path, adapter, name="two", timeout_retries=2)
    assert summary["outcome"] == "complete" and summary["calls_used"] == 5
    assert summary["timeout_retries_used"] == 2
    assert [(row["request_id"], row["retry_of"]) for row in rows(out)][:3] == [
        ("single.solve", None), ("single.solve.timeout-retry-1", "single.solve"),
        ("single.solve.timeout-retry-2", "single.solve.timeout-retry-1")]


def test_a_retry_counts_toward_the_call_budget(tmp_path):
    # Three calls: the repeated solve leaves room for one check only.
    adapter = StopsAtLimit("solve_obligation")
    _, summary = run(tmp_path, adapter, calls=3)
    assert summary["calls_used"] == 3 and summary["timeout_retries_used"] == 1
    assert summary["outcome"] == "abstain" and summary["endpoint_reason"] == "single_budget"
    # The last allowed call times out: no call is left to repeat it, so the run ends there.
    adapter = StopsAtLimit("whole_proof_review")
    out, summary = run(tmp_path, adapter, name="last", calls=3)
    assert summary["outcome"] == "pending" and summary["calls_used"] == 3
    assert summary["timeout_retries_used"] == 0
    assert [row["request_id"] for row in rows(out)] == ["single.solve", "single.alignment", "single.review"]


def test_no_retry_after_the_run_deadline(tmp_path):
    # The call stops after the run's own deadline: nothing is repeated.
    adapter = StopsAtLimit("solve_obligation", delay=2.5)
    _, summary = run(tmp_path, adapter, hours=0.0005)
    assert summary["calls_used"] == 1 and summary["timeout_retries_used"] == 0
    assert summary["outcome"] == "abstain" and summary["endpoint_reason"] == "single_deadline"


def test_both_checks_time_out_and_both_are_sent_again(tmp_path):
    adapter = StopsAtLimit("alignment_review", "whole_proof_review")
    out, summary = run(tmp_path, adapter)
    assert summary["outcome"] == "complete" and summary["calls_used"] == 5
    assert summary["timeout_retries_used"] == 2
    assert {row["request_id"]: row["retry_of"] for row in rows(out)} == {
        "single.solve": None, "single.alignment": None, "single.review": None,
        "single.alignment.timeout-retry-1": "single.alignment", "single.review.timeout-retry-1": "single.review"}


def test_a_run_recorded_without_the_setting_resumes_without_retries(tmp_path, monkeypatch):
    adapter = StopsAtLimit("solve_obligation")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        api._create_run(problem_file(tmp_path), out=out, transport="fake", _adapter=adapter)
    invocation = json.loads((out / "invocation.json").read_text())
    assert invocation["parameters"]["timeout_retries"] == 1
    del invocation["parameters"]["timeout_retries"]
    (out / "invocation.json").write_text(json.dumps(invocation), encoding="utf-8")
    assert api._open(out)[1].timeout_retry_limit == 0
    summary = resume_with(adapter, out, monkeypatch)
    assert summary["outcome"] == "pending" and summary["calls_used"] == 1
    assert summary["timeout_retries_per_call"] == 0 and summary["timeout_retries_used"] == 0


def test_resume_uses_the_recorded_setting(tmp_path, monkeypatch):
    adapter = StopsAtLimit("solve_obligation")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        directory, loop = api._create_run(problem_file(tmp_path), out=out, transport="fake", _adapter=adapter,
                                          timeout_retries=2)
        loop.step()  # Prepare the solve.
        loop.step()  # Send it; it stops at its time limit.
    assert api.status(out)["calls_used"] == 1
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(tmp_path / "absent.toml"))
    summary = resume_with(adapter, out, monkeypatch)
    assert summary["outcome"] == "complete" and summary["calls_used"] == 4
    assert summary["timeout_retries_per_call"] == 2 and summary["timeout_retries_used"] == 1


# ---- the other policies ------------------------------------------------------------


def test_ladder_sends_a_timed_out_check_again(tmp_path):
    adapter = StopsAtLimit("alignment_review")
    out, summary = run(tmp_path, adapter, policy="ladder")
    assert summary["outcome"] == "complete" and summary["calls_used"] == 4
    assert ("ladder.L0.alignment.timeout-retry-1", "ladder.L0.alignment") in [
        (row["request_id"], row["retry_of"]) for row in rows(out)]
    assert summary["incomplete_checks"] == [] and summary["timeout_retries_used"] == 1


def test_ladder_without_retries_ends_at_a_timed_out_check(tmp_path):
    adapter = StopsAtLimit("alignment_review")
    _, summary = run(tmp_path, adapter, policy="ladder", timeout_retries=0)
    assert summary["outcome"] == "pending" and summary["calls_used"] == 3


def test_resample_sends_a_timed_out_solve_again(tmp_path):
    adapter = StopsAtLimit("solve_obligation")
    out, summary = run(tmp_path, adapter, policy="resample")
    assert summary["outcome"] == "complete" and summary["calls_used"] == 4
    assert rows(out)[1]["request_id"] == "resample.L0.solve.timeout-retry-1"


def test_bestofn_repeats_a_generation_within_its_budget(tmp_path):
    from tests.test_bestofn import reference
    adapter = StopsAtLimit("solve_obligation")
    out, summary = run(tmp_path, adapter, policy="bestofn", full_calls=reference(tmp_path, problem=PROBLEM), calls=5)
    # The repetition uses the call a second manuscript would have used; one manuscript goes to the checks.
    assert summary["outcome"] == "complete" and summary["calls_used"] == 4
    assert summary["bestofn"]["n"] == 1 and summary["bestofn"]["selected_ref"] is not None
    assert [row["request_id"] for row in rows(out)] == [
        "bestofn.solve.0", "bestofn.solve.0.timeout-retry-1", "bestofn.alignment", "bestofn.review"]


# The roles of the fake full run's nine calls without a timeout.
FULL_ROLES = ["planner", "solver", "critic", "investigator", "solver", "critic", "investigator",
              "alignment_reviewer", "proof_reviewer"]


@pytest.mark.parametrize("kind", ["solve_obligation", "whole_proof_review"])
def test_full_sends_the_same_work_again(tmp_path, kind):
    adapter = StopsAtLimit(kind)
    out, summary = run(tmp_path, adapter, policy="full")
    assert summary["outcome"] == "complete" and summary["calls_used"] == len(FULL_ROLES) + 1
    calls = rows(out)
    stopped = next(row for row in calls if row["status"] == "timed_out")
    repeated = next(row for row in calls if row["retry_of"] is not None)
    assert repeated["retry_of"] == stopped["request_id"] and repeated["request_id"] == stopped["request_id"] + ".timeout-retry-1"
    assert repeated["role"] == stopped["role"] and repeated["status"] == "completed"
    assert [row["role"] for row in calls if row["retry_of"] is None] == FULL_ROLES


@pytest.mark.parametrize("policy", ["single", "full"])
def test_incremental_checks_match_the_full_replay_after_a_retried_timeout(tmp_path, monkeypatch, policy):
    # A session object replays every event in its first transaction and then applies only the
    # rows each later transaction added. The local_timeout settlement and the repetition must
    # pass that incremental check as they pass the full replay of open, status and resume.
    from victorique.adaptive_exchange import AdaptiveExchange
    full, incremental, refused = [], [], []
    replay_full, replay_since = AdaptiveExchange._replay_full, AdaptiveExchange._replay_since

    def counted_full(self, db):
        full.append(self)
        return replay_full(self, db)

    def counted_since(self, db, verified):
        try:
            work = replay_since(self, db, verified)
        except Exception as error:
            refused.append(repr(error))
            raise
        incremental.append(self)
        return work

    monkeypatch.setattr(AdaptiveExchange, "_replay_full", counted_full)
    monkeypatch.setattr(AdaptiveExchange, "_replay_since", counted_since)
    adapter = StopsAtLimit("solve_obligation")
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        directory, loop = api._create_run(problem_file(tmp_path), out=out, policy=policy, transport="fake",
                                          _adapter=adapter)
        live = api._run(directory, loop)
    assert live["outcome"] == "complete" and live["timeout_retries_used"] == 1
    # Each object replayed every event once, in its first transaction; every later
    # transaction, including those with the settlement and the repetition, was incremental.
    objects = {id(item) for item in full}
    assert len(full) == len(objects) and incremental and refused == []
    state = loop.core.snapshot()
    assert len(full) == len(objects) and refused == []
    base = state["exchange"]
    settled = [rid for rid in base["requests"] if retries.settled(base, rid)]
    assert len(settled) == 1 and retries.next_id(settled[0]) in base["requests"]
    assert base["requests"][settled[0]]["phase"] == "failed"
    if policy == "full":
        action = base["requests"][settled[0]]["record"]["action_id"]
        assert next(a for a in state["state"]["actions"] if a["action_id"] == action)["status"] == "timed_out"
    # New objects replay the whole record and reach the same state and summary.
    assert api._open(out)[1].core.snapshot() == state
    assert api.status(out) == live
    assert resume_with(adapter, out, monkeypatch) == live and len(adapter.calls) == live["calls_used"]
    # A changed settlement reason is found by the full check of a new object.
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(loop.core.path)) as db, db:
        rows = db.execute("SELECT sequence,payload FROM events ORDER BY sequence").fetchall()
        sequence, payload = next((sequence, json.loads(payload)) for sequence, payload in rows
                                 if json.loads(payload)["data"]["record"].get("reason") == "local_timeout")
        payload["data"]["record"]["reason"] = None
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='events'").fetchall()
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name + '"')
        db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(payload), sequence))
        for _, sql in triggers:
            db.execute(sql)
    with pytest.raises(ValueError, match="observation classification differs from replay"):
        api.status(out)


def test_paired_runner_and_error_probe_send_no_timed_out_call_again():
    # The paired runner and the error probe never send a timed-out call again.
    import ast
    root = Path(__file__).resolve().parents[1]
    sources = {"src/victorique/error_probe.py": "_create_run", "src/victorique/shared_prefix.py": "_create_run"}
    for name, function in sources.items():
        calls = [node for node in ast.walk(ast.parse((root / name).read_text(encoding="utf-8")))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == function
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "api"]
        assert calls, name
        for call in calls:
            given = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
            assert isinstance(given.get("timeout_retries"), ast.Constant) and given["timeout_retries"].value == 0, (
                name, call.lineno)


def test_retry_identities():
    assert retries.next_id("single.solve") == "single.solve.timeout-retry-1"
    assert retries.next_id("single.solve.timeout-retry-1") == "single.solve.timeout-retry-2"
    assert retries.retry_of("single.solve") is None
    assert retries.retry_of("single.solve.timeout-retry-1") == "single.solve"
    assert retries.retry_of("L3.loop_request_4.timeout-retry-2") == "L3.loop_request_4.timeout-retry-1"
    assert retries.logical("ladder.L0.review.retry.timeout-retry-1") == "ladder.L0.review.retry"
    assert retries.used(["a", "a.timeout-retry-1", "b"]) == 1


# ---- offline CLIs: the real transports stop a stalled call --------------------------


class OfflineCli:
    """Installs offline Codex and Claude executables; records each call's transport and limit."""

    def __init__(self, monkeypatch):
        from tests.test_victorique_harness_codex_contract import public_profile as codex_profile
        from tests.test_claude_contract import public_profile as claude_profile
        self.calls, self.stall = [], set()
        profiles = {"codex": codex_profile(), "claude-code": claude_profile()}
        inspect = lambda host, *args, **kwargs: profiles[host]
        for module in (api, verify):
            monkeypatch.setattr(module, "inspect_host", inspect)
            monkeypatch.setattr(module, "_mcp_names", lambda *args: [])
        monkeypatch.setattr("victorique.harness.codex_defaults.read_cli_defaults",
                            lambda *args, **kwargs: {"status": "unavailable"})
        for adapter_type, transport in ((CodexCliTransport, "codex"), (ClaudeCliTransport, "claude")):
            monkeypatch.setattr(adapter_type, "_invocation", self._invocation(adapter_type._invocation, transport))

    def _invocation(self, original, transport):
        cli = self

        def invocation(adapter, request, binding):
            computed = original(adapter, request, binding)
            kind = request.packet["binding"]["action_kind"]
            stalls = (transport, kind) in cli.stall
            if stalls:
                cli.stall.discard((transport, kind))
            cli.calls.append({"transport": transport, "kind": kind, "timeout_seconds": computed["timeout_seconds"],
                              "model": request.packet["binding"]["model_request"]["model_id"],
                              "argv": computed["argv"], "stalled": stalls})
            envelope = json.loads(FakeTransport()._response(request))
            model = "offline-" + transport
            if transport == "codex":
                rows = [{"type": "thread.started", "thread_id": request.job_id, "model": model},
                        {"type": "turn.started"},
                        {"type": "item.completed", "item": {"id": "final", "type": "agent_message",
                                                            "text": json.dumps(envelope)}},
                        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}]
            else:
                rows = [{"type": "system", "subtype": "init", "session_id": request.job_id, "model": model,
                         "tools": [], "mcp_servers": []},
                        {"type": "result", "subtype": "success", "is_error": False, "session_id": request.job_id,
                         "structured_output": envelope, "usage": {"input_tokens": 7, "output_tokens": 3}}]
            script = "import sys,pathlib,time; sys.stdin.buffer.read(); "
            if stalls:
                # The host starts its session, then never answers: the transport stops it at the limit.
                script += "print(" + repr(json.dumps(rows[0])) + ", flush=True); time.sleep(60)"
                return {**computed, "argv": [sys.executable, "-I", "-c", script], "timeout_seconds": 1}
            if transport == "codex":
                script += ("pathlib.Path(" + repr(computed["final_file"]) + ").write_text("
                           + repr(json.dumps(envelope)) + ",encoding='utf-8'); ")
            script += "print(" + repr("\n".join(json.dumps(row) for row in rows)) + ")"
            return {**computed, "argv": [sys.executable, "-I", "-c", script], "timeout_seconds": 10}
        return invocation


@pytest.fixture
def offline(monkeypatch):
    return OfflineCli(monkeypatch)


@pytest.mark.parametrize("transport", ["codex", "claude"])
def test_a_stalled_cli_call_is_stopped_and_sent_again(tmp_path, offline, transport):
    offline.stall.add((transport, "solve_obligation"))
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        summary = api.solve(problem_file(tmp_path), transport=transport, out=out, hours=0.05)
    assert summary["outcome"] == "complete" and summary["calls_used"] == 4
    assert summary["timeout_retries_used"] == 1
    calls = rows(out)
    assert [(row["request_id"], row["status"], row["retry_of"]) for row in calls] == [
        ("single.solve", "timed_out", None), ("single.solve.timeout-retry-1", "completed", "single.solve"),
        ("single.alignment", "completed", None), ("single.review", "completed", None)]
    assert [call["stalled"] for call in offline.calls] == [True, False, False, False]
    # Each call's limit is the setting, at most the run's remaining time (0.05 hours here).
    first, retry = offline.calls[0]["timeout_seconds"], offline.calls[1]["timeout_seconds"]
    assert 0 < retry <= first <= 180
    with redirect_stdout(io.StringIO()):
        assert api.resume(out) == summary
    assert len(offline.calls) == 4


def test_a_stalled_cli_call_without_retries_stays_unknown(tmp_path, offline):
    offline.stall.add(("codex", "solve_obligation"))
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        summary = api.solve(problem_file(tmp_path), transport="codex", out=out, timeout_retries=0)
        again = api.resume(out)
    assert summary["outcome"] == again["outcome"] == "pending"
    assert summary["calls_used"] == again["calls_used"] == 1
    assert rows(out)[0]["status"] == "timed_out" and len(offline.calls) == 1
