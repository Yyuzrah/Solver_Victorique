"""Stopping a run: victorique stop DIR, victorique.stop and the stop request file.

The runs in a separate process use the real Codex and Claude transports with
fixed local Python prints, as in test_timeout_retry; a stalled call prints its
first event and then sleeps, so only the stop request can end it before the
per-call limit. The in-process runs use the fake transport.
"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest

import victorique
from victorique import api, cli, settings, stop_request
from victorique.fake import FakeTransport
from victorique.harness.codex_process import STOP_KILL_REASON
from victorique.harness.codex_transport import CodexFixtureTransport
from victorique.harness_bridge import OBSERVATION, TransportObservation

ROOT = Path(__file__).resolve().parents[1]
PROBLEM = "Prove that n + 0 = n for every integer n."
POLICIES = ("single", "ladder", "resample", "bestofn", "full")
# The longest time these tests allow from the stop request to the end of the run's
# process. The run sees the request within one poll (0.5 s for a CLI transport),
# the worker within 0.1 s; the rest is ending the CLI process and writing the files.
STOP_BOUND_SECONDS = 20


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch):
    monkeypatch.delenv(settings.CONFIG_ENVIRONMENT, raising=False)


def problem_file(tmp_path):
    path = tmp_path / "problem.md"
    path.write_text(PROBLEM, encoding="utf-8")
    return path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(out):
    return [json.loads(line) for line in (out / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def options(policy, tmp_path):
    if policy != "bestofn":
        return {}
    from tests.test_bestofn import reference
    return {"full_calls": reference(tmp_path, problem=PROBLEM), "calls": 5}


# ---- a run in another process, stopped while a model call is in flight ---------------


def offline_run(out, problem, policy, transport, stall, marks):
    """Run victorique solve with an offline CLI; the first call of each kind in ``stall`` never answers.

    Every call writes ``marks/sent-REQUEST.json``; a stalled call's process writes
    ``marks/started-REQUEST`` (its process number) when it starts and
    ``marks/answered-REQUEST`` if it ever finishes its sleep.
    """
    from victorique.harness.claude_transport import ClaudeCliTransport
    from victorique.harness.codex_transport import CodexCliTransport
    from tests.test_victorique_harness_codex_contract import public_profile as codex_profile
    from tests.test_claude_contract import public_profile as claude_profile
    marks, stalled = Path(marks), set(filter(None, stall.split(",")))
    profile = codex_profile() if transport == "codex" else claude_profile()
    adapter_type = CodexCliTransport if transport == "codex" else ClaudeCliTransport
    original = adapter_type._invocation

    def invocation(adapter, request, binding):
        computed = original(adapter, request, binding)
        kind = request.packet["binding"]["action_kind"]
        rid = request.packet["binding"]["request_id"]
        stalls = kind in stalled
        stalled.discard(kind)
        (marks / ("sent-" + rid + ".json")).write_text(json.dumps({"kind": kind, "stalls": stalls, "at": time.time()}))
        envelope = json.loads(FakeTransport()._response(request))
        if transport == "codex":
            answer = [{"type": "thread.started", "thread_id": request.job_id, "model": "offline-codex"},
                      {"type": "turn.started"},
                      {"type": "item.completed", "item": {"id": "final", "type": "agent_message",
                                                          "text": json.dumps(envelope)}},
                      {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}]
        else:
            answer = [{"type": "system", "subtype": "init", "session_id": request.job_id, "model": "offline-claude",
                       "tools": [], "mcp_servers": []},
                      {"type": "result", "subtype": "success", "is_error": False, "session_id": request.job_id,
                       "structured_output": envelope, "usage": {"input_tokens": 7, "output_tokens": 3}}]
        script = "import os,sys,pathlib,time; sys.stdin.buffer.read(); "
        if stalls:
            # The host starts its session and then never answers.
            script += ("pathlib.Path(" + repr(str(marks / ("started-" + rid))) + ").write_text(str(os.getpid())); "
                       "print(" + repr(json.dumps(answer[0])) + ", flush=True); time.sleep(60); "
                       "pathlib.Path(" + repr(str(marks / ("answered-" + rid))) + ").write_text('late')")
        else:
            if transport == "codex":
                script += ("pathlib.Path(" + repr(computed["final_file"]) + ").write_text("
                           + repr(json.dumps(envelope)) + ",encoding='utf-8'); ")
            script += "print(" + repr("\n".join(json.dumps(row) for row in answer)) + ")"
        return {**computed, "argv": [sys.executable, "-I", "-c", script]}

    with mock.patch.object(api, "inspect_host", lambda *args, **kwargs: profile), \
            mock.patch.object(api, "_mcp_names", lambda *args: []), \
            mock.patch("victorique.harness.codex_defaults.read_cli_defaults",
                       lambda *args, **kwargs: {"status": "unavailable"}), \
            mock.patch.object(adapter_type, "_invocation", invocation):
        api.solve(problem, policy=policy, transport=transport, out=out, hours=0.5)


def start_offline_run(tmp_path, policy, transport, stall):
    out, marks, log = tmp_path / "run", tmp_path / "marks", tmp_path / "run.log"
    marks.mkdir()
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(ROOT / "src"), str(ROOT), os.environ.get("PYTHONPATH", "")]), "PYTHONIOENCODING": "utf-8"}
    environment.pop(settings.CONFIG_ENVIRONMENT, None)
    with open(log, "wb") as output:
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; from tests.test_stop import offline_run; offline_run(*sys.argv[1:])",
             str(out), str(problem_file(tmp_path)), policy, transport, ",".join(stall), str(marks)],
            cwd=str(ROOT), env=environment, stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out, marks, child, log


def wait_for(condition, child, log, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        if child.poll() is not None:
            raise AssertionError(what + "; the run ended first:\n" + log.read_text(encoding="utf-8", errors="replace"))
        time.sleep(0.05)
    raise AssertionError(what + " within " + str(seconds) + " seconds")


@pytest.mark.parametrize("policy,transport,stall,proof", [
    # Both checks are in flight together; the solved draft is kept.
    ("single", "codex", ("alignment_review", "whole_proof_review"), "n + 0 = n"),
    # The statement check stalls while the proof check returns.
    ("ladder", "codex", ("alignment_review",), "n + 0 = n"),
    # The critique of the first draft stalls; the full workflow keeps that draft.
    ("full", "codex", ("critique_candidate",), "Synthetic toy proof"),
    # Claude Code roles run one at a time: the solve call stalls before any draft.
    ("single", "claude", ("solve_obligation",), None),
])
def test_a_stop_ends_the_call_in_flight_and_the_run(tmp_path, capsys, policy, transport, stall, proof):
    from victorique.gui.launch import process_running
    test_started = time.time()
    out, marks, child, log = start_offline_run(tmp_path, policy, transport, stall)
    try:
        wait_for(lambda: len(list(marks.glob("started-*"))) == len(stall), child, log, 120,
                 "every stalled call should start")
        sent = sorted(path.name for path in marks.glob("sent-*"))
        requested_at = time.time()
        clock = time.monotonic()
        recorded = victorique.stop(out)
        child.wait(timeout=120)
        elapsed = time.monotonic() - clock
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(30)
    output = log.read_text(encoding="utf-8", errors="replace")
    assert child.returncode == 0, output
    assert recorded["status"] == "requested" and (out / stop_request.FILE).is_file()
    assert read(out / stop_request.FILE)["requested_at"] == recorded["requested_at"]
    # The run ended within the bound; summary.json was written after the request.
    summary_written = (out / "summary.json").stat().st_mtime - requested_at
    assert elapsed < STOP_BOUND_SECONDS, (elapsed, output)
    with capsys.disabled():  # Shown with pytest -s.
        print(f"\nstop request to end of run ({policy}, {transport}): summary.json written after "
              f"{summary_written:.2f} s, process ended after {elapsed:.2f} s")
    # No call was sent after the request, and the stalled calls never answered.
    assert sorted(path.name for path in marks.glob("sent-*")) == sent
    assert not list(marks.glob("answered-*"))
    for started in marks.glob("started-*"):
        assert process_running(int(started.read_text()), test_started - 5, time.time() + 5) is False
    summary = read(out / "summary.json")
    assert summary["outcome"] == "partial" and summary["endpoint_reason"] == stop_request.REASON
    assert summary["calls_used"] == len(sent)
    assert summary["timeout_retries_per_call"] == 1 and summary["timeout_retries_used"] == 0
    calls = rows(out)
    assert len(calls) == len(sent) and all(row["retry_of"] is None for row in calls)
    stopped = [row for row in calls if row["status"] == "stopped"]
    assert len(stopped) == len(stall)
    assert all(row["status"] == "completed" for row in calls if row not in stopped)
    assert "stopped_by_user | controller" in output
    # The files of an ending without a proof: the best draft, or the note that there is none.
    text = (out / "proof.md").read_text(encoding="utf-8")
    if proof is None:
        assert not summary["proof_available"] and text.startswith("# No proof delivered")
    else:
        assert summary["proof_available"] and proof in text
    # A full replay by new objects agrees with the live state; resume reports the run as finished.
    assert api.status(out) == summary
    capsys.readouterr()
    with redirect_stdout(io.StringIO()):
        assert api.resume(out) == summary
    assert "already finished" in capsys.readouterr().err
    assert sorted(path.name for path in marks.glob("sent-*")) == sent
    assert victorique.stop(out)["status"] == "finished"


# ---- runs in this process with the fake transport ------------------------------------


class Recording(FakeTransport):
    """Fixed answers; records every call it receives."""

    def __init__(self):
        self.calls = []

    def start_once(self, request):
        self.calls.append(request.packet["binding"]["request_id"])
        return super().start_once(request)


class StopsInFlight(Recording):
    """Fixed answers, except that the first call of ``kind`` asks the run to stop and never answers.

    With ``timed_out``, the transport reports that call as stopped at its per-call
    limit, so without the stop the run would send it again.
    """

    def __init__(self, out, kind, timed_out=False):
        super().__init__()
        self.out, self.kind, self.timed_out = out, kind, timed_out
        self.unanswered = {}
        # The request in flight, and how many calls had been received when the stop was requested.
        self.in_flight = self.sent_at_stop = None

    def _metadata(self, request, status, observation_id):
        return {"schema": OBSERVATION, "observation_id": observation_id, "job_id": request.job_id,
                "core_claim_id": request.core_claim_id, "packet_id": request.packet["binding"]["packet_id"],
                "adapter_id": "fake", "adapter_version": "v1", "mode": "synthetic", "status": status,
                "terminal_observed": status != "unknown", "host_reference": "/fake/" + request.job_id,
                "observed_at_utc": datetime.now(timezone.utc).isoformat(), "source_event_id": None,
                "source_line": None, "model_observation": None, "charge_observation": None}

    def start_once(self, request):
        if request.packet["binding"]["action_kind"] == self.kind and not self.unanswered:
            self.in_flight = request.packet["binding"]["request_id"]
            self.calls.append(self.in_flight)
            victorique.stop(self.out)  # The request arrives while this call is in flight.
            self.sent_at_stop = len(self.calls)
            self.unanswered[request.job_id] = TransportObservation(
                self._metadata(request, "unknown", "fake." + request.job_id), None, b"Synthetic call in flight")
            return self.unanswered[request.job_id]
        return super().start_once(request)

    def observe_existing(self, request):
        return self.unanswered.get(request.job_id) or super().observe_existing(request)

    def timeout_observation(self, request):
        if not self.timed_out or request.job_id not in self.unanswered:
            return None
        return TransportObservation(self._metadata(request, "failed", "timeout_" + request.job_id), None,
                                    b"Synthetic settlement")


def create(tmp_path, policy, adapter, name="run", **extra):
    with redirect_stdout(io.StringIO()):
        return api._create_run(problem_file(tmp_path), policy=policy, out=tmp_path / name, transport="fake",
                               _adapter=adapter, **options(policy, tmp_path), **extra)


@pytest.mark.parametrize("policy", POLICIES)
def test_a_stop_before_the_first_call(tmp_path, policy):
    adapter = Recording()
    directory, loop = create(tmp_path, policy, adapter)
    recorded = victorique.stop(directory)
    assert recorded["status"] == "requested" and recorded["output_directory"] == str(directory)
    again = victorique.stop(directory)
    assert again["status"] == "already_requested" and again["requested_at"] == recorded["requested_at"]
    with redirect_stdout(io.StringIO()) as printed:
        summary = api._run(directory, loop)
    assert adapter.calls == []
    assert summary["outcome"] == "partial" and summary["endpoint_reason"] == "stopped_by_user"
    assert summary["calls_used"] == 0 and not summary["proof_available"]
    assert printed.getvalue().splitlines()[-1].startswith("stopped_by_user | controller | 0/")
    assert (directory / "proof.md").read_text(encoding="utf-8").startswith("# No proof delivered")
    assert read(directory / "summary.json") == summary and (directory / "calls.jsonl").read_text() == ""
    assert api.status(directory) == summary
    with redirect_stderr(io.StringIO()) as note:
        assert api.resume(directory) == summary
    assert "already finished (outcome partial, end reason stopped_by_user)" in note.getvalue()
    assert victorique.stop(directory)["status"] == "finished"


# The kind whose call is in flight when the stop request arrives; a draft exists by then. In
# single, ladder and resample the two checks are sent together, so one check of the pair is in flight.
IN_FLIGHT = {"single": "whole_proof_review", "ladder": "alignment_review", "resample": "alignment_review",
             "bestofn": "alignment_review", "full": "critique_candidate"}
PAIRED = ("single", "ladder", "resample")


@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize("policy", POLICIES)
def test_a_stop_while_a_call_is_in_flight_sends_nothing_more(tmp_path, policy, timed_out):
    # With timed_out the transport also reports the call in flight as stopped at its per-call
    # limit, so that without the stop the run would send it again (timeout_retries is 1).
    adapter = StopsInFlight(tmp_path / "run", IN_FLIGHT[policy], timed_out=timed_out)
    directory, loop = create(tmp_path, policy, adapter)
    with redirect_stdout(io.StringIO()):
        summary = api._run(directory, loop)
    # Nothing was sent after the request, except the other check of a pair that started with it.
    assert len(adapter.calls) - adapter.sent_at_stop <= (1 if policy in PAIRED else 0)
    assert len(adapter.calls) == summary["calls_used"] and len(set(adapter.calls)) == len(adapter.calls)
    assert summary["outcome"] == "partial" and summary["endpoint_reason"] == "stopped_by_user"
    assert summary["timeout_retries_per_call"] == 1 and summary["timeout_retries_used"] == 0
    assert summary["proof_available"]
    calls = {row["request_id"]: row for row in rows(directory)}
    assert set(calls) == set(adapter.calls) and all(row["retry_of"] is None for row in calls.values())
    assert calls[adapter.in_flight]["status"] == ("timed_out" if timed_out else "unknown")
    assert all(row["status"] == "completed" for rid, row in calls.items() if rid != adapter.in_flight)
    assert api.status(directory) == summary
    with redirect_stderr(io.StringIO()):
        assert api.resume(directory) == summary
    assert len(adapter.calls) == summary["calls_used"]


@pytest.mark.parametrize("policy", ["single", "full"])
def test_incremental_checks_match_the_full_replay_after_a_stop(tmp_path, monkeypatch, policy):
    # As for a retried timeout (test_timeout_retry): each session object replays every event in its
    # first transaction and later applies only the rows each transaction added. The observation of
    # the call in flight and the close at the stop request pass that check like the full replay.
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
    adapter = StopsInFlight(tmp_path / "run", IN_FLIGHT[policy])
    directory, loop = create(tmp_path, policy, adapter)
    with redirect_stdout(io.StringIO()):
        live = api._run(directory, loop)
    assert live["endpoint_reason"] == "stopped_by_user"
    objects = {id(item) for item in full}
    assert len(full) == len(objects) and incremental and refused == []
    state = loop.core.snapshot()
    assert state["exchange"]["requests"][adapter.in_flight]["phase"] == "unknown"
    if policy == "full":
        assert state["endpoint"]["reducer_endpoint"]["decision"] == "stop"
        assert state["endpoint"]["reducer_endpoint"]["reason"] == "stopped_by_user"
    else:
        assert state["endpoint"]["reason"] == "stopped_by_user"
    # New objects replay the whole record and reach the same state and summary.
    assert api._open(directory)[1].core.snapshot() == state
    assert api.status(directory) == live
    with redirect_stderr(io.StringIO()):
        assert api.resume(directory) == live
    assert len(adapter.calls) == live["calls_used"]


def test_a_draft_that_passed_both_checks_is_still_delivered(tmp_path):
    # The stop request appears after the second check returned: ending the run needs no call.
    class StopsAfterTheChecks(Recording):
        def start_once(self, request):
            observed = super().start_once(request)
            if request.packet["binding"]["action_kind"] == "whole_proof_review":
                victorique.stop(tmp_path / "run")
            return observed
    adapter = StopsAfterTheChecks()
    directory, loop = create(tmp_path, "ladder", adapter)
    with redirect_stdout(io.StringIO()):
        summary = api._run(directory, loop)
    assert summary["outcome"] == "complete" and summary["endpoint_reason"] == "ladder_complete"
    assert len(adapter.calls) == 3 and (directory / stop_request.FILE).is_file()


def test_an_escalated_full_workflow_follows_the_run_stop_request(tmp_path):
    # ladder --escalate continues in a full workflow under state/escalated; its calls use the
    # stop request of the run folder.
    class FailsChecks(StopsInFlight):
        def _response(self, request):
            binding = request.packet["binding"]
            if binding["action_kind"] in ("alignment_review", "whole_proof_review") and \
                    binding["request_id"].startswith("ladder."):
                target = binding["target_ref"]
                content = request.packet["context"]["target_candidate"]["content"]
                output = {"schema": "victorique.adaptive_gate.v1", "gate_id": binding["action_kind"],
                          "problem_ref": binding["problem_ref"], "target_ref": target,
                          "coverage": "whole_problem_and_proof", "outcome": "fail", "uncertainties": [],
                          "findings": [{"schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                                        "target_ref": target, "kind": "invalid_step", "severity": "material",
                                        "location": {"start_byte": 0, "end_byte": len(content.encode()),
                                                     "quote": content},
                                        "claim": "Synthetic fixture rejects this draft."}]}
                return json.dumps({"schema": "victorique.adaptive_actor_response.v2", "binding": binding,
                                   "output": output}).encode("utf-8")
            return super()._response(request)
    adapter = FailsChecks(tmp_path / "run", "critique_candidate")
    directory, loop = create(tmp_path, "ladder", adapter, escalate=True, repair_rounds=0, calls=8)
    with redirect_stdout(io.StringIO()):
        summary = api._run(directory, loop)
    assert (directory / "state" / "escalated").is_dir()
    assert loop._child().stop_path == directory / stop_request.FILE
    assert summary["outcome"] == "partial" and summary["endpoint_reason"] == "stopped_by_user"
    assert adapter.calls[-1].startswith("loop_request_") and len(adapter.calls) == summary["calls_used"]
    assert loop._child().core.snapshot()["endpoint"] is None
    assert api.status(directory) == summary


def test_a_stopped_full_run_is_accepted_as_a_bestofn_reference(tmp_path):
    # A stopped call has no measured cost: that run gives no token total, and the target comes
    # from the runs in --full-references, as for a run with an unknown call.
    from victorique import bestofn
    from tests.test_bestofn import PROBLEM as BESTOFN_PROBLEM, reference, reference_list
    paired = reference(tmp_path, problem=BESTOFN_PROBLEM)
    calls = [json.loads(line) for line in paired.read_text(encoding="utf-8").splitlines()]
    calls[-1].update(status="stopped", tokens=None)
    paired.write_text("".join(json.dumps(row) + "\n" for row in calls), encoding="utf-8")
    other = reference(tmp_path, [("solver", 501)], run_id="same-problem", problem=BESTOFN_PROBLEM)
    estimate = bestofn.estimate_from_full(paired, BESTOFN_PROBLEM, 12, reference_list(tmp_path, [(other, None)]))
    assert estimate["target_fallback"] == "same_problem_median" and estimate["target_tokens"] == 501
    assert estimate["role_mean_tokens"]["solver"] == 100


def test_both_services_of_a_routed_run_watch_the_stop_request(tmp_path, monkeypatch):
    from tests.test_victorique_harness_codex_contract import public_profile as codex_profile
    from tests.test_claude_contract import public_profile as claude_profile
    profiles = {"codex": codex_profile(), "claude-code": claude_profile()}
    monkeypatch.setattr(api, "inspect_host", lambda host, *args, **kwargs: profiles[host])
    monkeypatch.setattr(api, "_mcp_names", lambda *args: [])
    monkeypatch.setattr("victorique.harness.codex_defaults.read_cli_defaults",
                        lambda *args, **kwargs: {"status": "unavailable"})
    with redirect_stdout(io.StringIO()):
        directory, loop = api._create_run(problem_file(tmp_path), transport="codex", check_transport="claude",
                                          out=tmp_path / "run")
    expected = directory / stop_request.FILE
    assert loop.bridge.stop_file == expected
    assert [member.stop_file for _, member in loop.adapter.members] == [expected, expected]
    _, reopened = api._open(directory)
    assert [member.stop_file for _, member in reopened.adapter.members] == [expected, expected]


# ---- the worker process ends the call: the path of the per-call time limit -----------


def await_finished(adapter, request, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        capture = adapter.local_capture(request)
        if capture and capture["worker_finished"]:
            return capture
        time.sleep(0.02)
    raise AssertionError("the worker did not finish within the test bound")


def core_packet(tmp_path):
    """The packet of one prepared request of a new synthetic session (as in the transport tests)."""
    from tests.test_victorique_adaptive_exchange import manifest
    from victorique.adaptive_session import (AdaptiveSession, CONTRACTS_VERSION, CONTROLLER_VERSION,
                                             SESSION_VERSION, TEMPLATES_VERSION)
    value = manifest()
    value["implementation_versions"].update(contracts=CONTRACTS_VERSION, controller=CONTROLLER_VERSION,
                                            session=SESSION_VERSION, templates=TEMPLATES_VERSION)
    core = AdaptiveSession.create(tmp_path / "core", value)
    state = core.status()
    action = core.propose()["legal_action_listing"]["actions"][0]
    core.prepare({"session_id": value["session_id"], "operation_id": "op-1",
                  "expected_session_sequence": state["session_sequence"],
                  "expected_state_revision": state["state_revision"], "request_id": "request-1",
                  "logical_action_id": "logical-1", "action_id": "action-1", "selected_proposal": action})
    return core.snapshot()["exchange"]["requests"]["request-1"]["packet"]


def fixture_request(packet, name):
    from victorique.harness_bridge import DispatchRequest
    return DispatchRequest("job-" + name, "claim-" + name, packet,
                           (json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"), "synthetic")


def test_the_worker_ends_a_call_when_the_stop_request_appears(tmp_path):
    # codex_fake_host's delay-after-thread-start prints the session, then waits 10 seconds.
    packet = core_packet(tmp_path)
    adapter = CodexFixtureTransport.create(tmp_path / "transport", response_bytes=b"{}",
                                           scenario="delay-after-thread-start", delay_seconds=10, timeout_seconds=60)
    adapter.stop_file = tmp_path / stop_request.FILE
    request = fixture_request(packet, "inflight")
    adapter.start_once(request)
    stdout = Path(adapter.runner.layout(adapter._binding(request))["stdout_file"])
    deadline = time.monotonic() + 30
    while not (stdout.exists() and b"thread.started" in stdout.read_bytes()):
        assert time.monotonic() < deadline, "the fixture host did not start"
        time.sleep(0.02)
    clock = time.monotonic()
    adapter.stop_file.write_text("{}")
    capture = await_finished(adapter, request)
    elapsed = time.monotonic() - clock
    # Ended by the worker like a timeout, but not counted as one: no repetition is allowed.
    assert capture["kill_requested"] and capture["kill_reason"] == STOP_KILL_REASON == "stopped_by_user"
    assert capture["timed_out"] is False and capture["local_terminal_observed"] and capture["exit_code"] != 0
    assert capture["invocation"]["stop_file"] == str(adapter.stop_file)
    assert elapsed < 5, elapsed
    assert adapter.timeout_observation(request) is None
    assert adapter.observe_existing(request).metadata["status"] == "unknown"
    # A call whose worker starts after the request never launches its CLI.
    late = fixture_request(packet, "late")
    adapter.start_once(late)
    capture = await_finished(adapter, late)
    assert capture["kill_reason"] == "stopped_by_user" and capture["launch_error"] == "HarnessError"
    assert capture["child_pid"] is None and capture["local_terminal_observed"] is False
    workspace = Path(adapter.runner.layout(adapter._binding(late))["workspace"])
    assert not (workspace / "stdin-observed.bin").exists()


def test_the_stop_file_must_be_an_absolute_path(tmp_path):
    from victorique.harness.codex_process import DurableCodexProcess, fake_codex_argv
    from victorique.harness.safety import HarnessError
    runner = DurableCodexProcess.create(tmp_path / "runner")
    binding = {"job_id": "job-1", "core_claim_id": "claim-1", "packet_id": "packet-1", "mode": "synthetic"}
    layout = runner.layout(binding)
    invocation = {"argv": fake_codex_argv(layout["final_file"]), "cwd": layout["workspace"],
                  "final_file": layout["final_file"]}
    for value in ("relative/stop-request.json", 7):
        with pytest.raises(HarnessError, match="absolute path"):
            runner.start_once(binding, {**invocation, "stop_file": value}, b"{}")


# ---- the command ------------------------------------------------------------------------


def run_cli(*arguments):
    with mock.patch("sys.argv", ["victorique", *arguments]), redirect_stdout(io.StringIO()) as printed:
        cli.main()
    return json.loads(printed.getvalue())


def test_the_stop_command_on_a_finished_run_and_on_other_folders(tmp_path, capsys):
    with redirect_stdout(io.StringIO()):
        finished = api.solve(problem_file(tmp_path), transport="fake", out=tmp_path / "done")
    result = run_cli("stop", str(tmp_path / "done"))
    assert result["status"] == "finished" and result["outcome"] == finished["outcome"] == "complete"
    assert result["endpoint_reason"] == "single_complete" and "already finished" in result["message"]
    assert not (tmp_path / "done" / stop_request.FILE).exists()
    for folder in (tmp_path / "empty", tmp_path / "missing", tmp_path / "problem.md"):
        if folder.name == "empty":
            folder.mkdir()
        with mock.patch("sys.argv", ["victorique", "stop", str(folder)]), pytest.raises(SystemExit) as stopped:
            cli.main()
        assert stopped.value.code == 2
        assert "is not a Victorique run folder" in capsys.readouterr().err
        assert not (folder / stop_request.FILE).exists()
    # A run folder whose invocation cannot be read is refused as well.
    (tmp_path / "empty" / "invocation.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not a Victorique run folder"):
        victorique.stop(tmp_path / "empty")
    # A run without a final summary takes the request: an interrupted run ends with it when resumed.
    directory, _ = create(tmp_path, "single", Recording(), name="pending")
    (directory / "summary.json").write_text(json.dumps({"outcome": "pending"}), encoding="utf-8")
    result = run_cli("stop", str(directory))
    assert result["status"] == "requested" and read(directory / stop_request.FILE)["schema"] == stop_request.SCHEMA
    assert run_cli("stop", str(directory))["status"] == "already_requested"
    with redirect_stdout(io.StringIO()):
        assert api.resume(directory)["endpoint_reason"] == "stopped_by_user"


def test_the_command_help_lists_stop():
    result = subprocess.run([sys.executable, "-m", "victorique", "--help"], capture_output=True, text=True,
                            timeout=60, cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0 and "stop" in result.stdout
    assert victorique.stop.__doc__ and "stop" in api.stop.__doc__
