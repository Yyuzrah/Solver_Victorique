import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from unittest import mock

import pytest

from victorique import verify as v
from victorique.fake import FakeTransport


class ScriptedTransport(FakeTransport):
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.packets = []

    def _response(self, request):
        self.packets.append(deepcopy(request.packet))
        result = self.outcomes[len(self.packets) - 1]
        if result == "exception":
            raise TimeoutError("synthetic timeout")
        if result == "invalid":
            return b"invalid JSON"
        payload = json.loads(super()._response(request))
        output = payload["output"]
        if result == "unknown":
            output.update(outcome="unknown", uncertainties=["Cannot resolve a premise."])
        if result in ("material", "fatal", "minor", "minor_unknown"):
            content = request.packet["context"]["target_candidate"]["content"]
            output.update(outcome="unknown" if result == "minor_unknown" else "fail", findings=[{
                "schema": "victorique.adaptive_defect.v1", "issue_id": "fixture-issue",
                "target_ref": output["target_ref"], "kind": "invalid_step",
                "severity": "minor" if result == "minor_unknown" else result,
                "location": {"start_byte": 0, "end_byte": len(content.encode()), "quote": content},
                "claim": "Synthetic fixture concern."}])
        if result == "wrong_version":
            output["target_ref"]["revision"] = 2
        return json.dumps(payload).encode()


@pytest.mark.parametrize("kind", ["codex_cli", "claude_cli"])
def test_finished_capture_race_recovers_original_once_more(tmp_path, inputs, kind):
    problem, proof = inputs
    with mock.patch.object(v, "_run", side_effect=lambda directory, loop: loop):
        loop = v.verify(proof, problem=problem, transport="fake", out=tmp_path / "race")
    assert loop.step()["phase"] == "native_prepared"
    with mock.patch.object(loop.adapter, "start_once", side_effect=RuntimeError("pending capture")):
        loop.step()
    loop.config["transport"]["kind"] = kind
    loop.adapter.runner = mock.Mock()
    loop.adapter.runner.observe_existing.return_value = {"worker_finished": True}
    loop.adapter._binding = lambda request: {}
    original = loop.bridge.recover
    count = 0
    def racing_recover(rid, adapter):
        nonlocal count
        count += 1
        return {"phase": "pending"} if count == 1 else original(rid, adapter)
    with mock.patch.object(loop.bridge, "recover", side_effect=racing_recover), \
         mock.patch.object(loop.bridge, "dispatch", side_effect=AssertionError("redispatch")):
        assert loop.step()["phase"] == "original_response_observed"
    assert count == 2
    rows = list(loop.core.snapshot()["exchange"]["requests"].values())
    assert len(rows) == 1 and rows[0]["phase"] == "returned_valid"


@pytest.fixture
def inputs(tmp_path):
    problem, proof = tmp_path / "problem.md", tmp_path / "proof.md"
    problem.write_text("Prove n + 0 = n for every integer n.", encoding="utf-8")
    proof.write_bytes("The additive identity gives n + 0 = n.\r\nUnicode: α.\r\n".encode())
    return problem, proof


def run(tmp_path, inputs, outcomes, **kwargs):
    problem, proof = inputs
    adapter = ScriptedTransport(outcomes)
    result = v.verify(proof, problem=problem, transport="fake", out=tmp_path / "run",
                      _adapter=adapter, **kwargs)
    return result, adapter


@pytest.mark.parametrize("mode,passes,count", [("single", 1, 1), ("stepwise", 2, 2), ("multi", 2, 6)])
def test_modes_passes_fresh_contexts_and_version_binding(tmp_path, inputs, mode, passes, count):
    result, adapter = run(tmp_path, inputs, ["pass"] * count, mode=mode, passes=passes)
    assert result["outcome"] == "pass"
    assert result["calls_used"] == result["reviews_passed"] == count
    assert result["reviews_required"] == count
    assert len({p["binding"]["packet_id"] for p in adapter.packets}) == count
    assert len({p["binding"]["request_id"] for p in adapter.packets}) == count
    expected = inputs[1].read_bytes().decode()
    for packet in adapter.packets:
        assert packet["binding"]["target_ref"] == result["target_ref"]
        assert packet["binding"]["problem_ref"] == result["problem_ref"]
        assert packet["context"]["problem"]["content"] == inputs[0].read_text()
        assert packet["context"]["target_candidate"]["content"] == expected
        assert packet["context"]["released_feedback"] == []
        assert packet["context"]["planning_evidence"] == []
        assert packet["context"]["plan"] is None
    assert (tmp_path / "run" / "proof.md").read_bytes() == inputs[1].read_bytes()
    if mode == "stepwise":
        assert "enumerate the proof's reasoning steps" in adapter.packets[0]["template"]["instructions"]
    manifest = v._read(tmp_path / "run" / "state" / "run-config.json")["manifest"]
    assert manifest["role_bindings"]["proof_reviewer"]["fresh_context"] is True


@pytest.mark.parametrize("outcome,expected", [("material", "fail"), ("fatal", "fail"), ("minor", "pass"),
    ("unknown", "incomplete"), ("minor_unknown", "incomplete"), ("invalid", "incomplete"),
    ("wrong_version", "incomplete"), ("exception", "incomplete")])
def test_failure_classes_and_nonblocking(tmp_path, inputs, outcome, expected):
    result, adapter = run(tmp_path, inputs, [outcome])
    assert result["outcome"] == expected
    assert result["calls_used"] == len(adapter.packets) == 1
    if outcome == "minor":
        assert result["blocking"] == []
        assert len(result["non_blocking"]) == 1
        assert result["reviews"][0]["output"]["outcome"] == "fail"
    if outcome == "exception":
        result = v.resume_verify(tmp_path / "run", _adapter=adapter)
        assert result["outcome"] == "incomplete"
        assert len(adapter.packets) == 1


def test_multi_any_blocking_stops_without_majority_vote(tmp_path, inputs):
    result, adapter = run(tmp_path, inputs, ["pass", "pass", "material"], mode="multi")
    assert result["outcome"] == "fail"
    assert result["reviews_passed"] == 2
    assert len(adapter.packets) == 3


def test_verify_export_keeps_every_severity_and_per_call_evidence(tmp_path, inputs):
    class MixedFindings(ScriptedTransport):
        def _response(self, request):
            payload = json.loads(super()._response(request))
            original = payload["output"]["findings"][0]
            payload["output"]["findings"] = [{**original, "issue_id": severity + "-issue", "severity": severity}
                                             for severity in ("fatal", "material", "minor")]
            return json.dumps(payload).encode()
    problem, proof = inputs
    adapter = MixedFindings(["material"])
    out = tmp_path / "mixed"
    result = v.verify(proof, problem=problem, transport="fake", out=out, _adapter=adapter)
    report = result["reviews"][0]
    assert report["review_output"] == report["output"]
    assert report["issue_lists"]["findings"] == report["output"]["findings"]
    assert [x["severity"] for x in report["issue_lists"]["findings"]] == ["fatal", "material", "minor"]
    assert [x["severity"] for x in result["blocking"]] == ["fatal", "material"]
    assert [x["severity"] for x in result["non_blocking"]] == ["minor"]
    assert report["capture_truncated"] is report["effective_output_token_limit"] is None
    frozen = (out / "state/run-config.json").read_bytes()
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    metadata = {**v.evidence(output=report["output"]), "finish_reason": "length",
                "capture_truncated": True, "effective_output_token_limit": 512, "output_limit_reached": True}
    rows[0].update(metadata)
    with mock.patch.object(v, "call_rows", return_value=rows), \
         mock.patch.object(adapter, "start_once", side_effect=AssertionError("redispatch")):
        resumed = v.resume_verify(out, _adapter=adapter)
    assert {key: resumed["reviews"][0][key] for key in metadata} == metadata
    assert resumed["outcome"] == result["outcome"] == "fail"
    assert (out / "state/run-config.json").read_bytes() == frozen
    assert len(adapter.packets) == 1


def test_passes_fail_does_not_retry_or_reset_streak(tmp_path, inputs):
    result, adapter = run(tmp_path, inputs, ["pass", "material"], passes=3)
    assert result["outcome"] == "fail"
    assert len(adapter.packets) == 2


def test_calls_cap_is_hard_and_incomplete(tmp_path, inputs):
    result, adapter = run(tmp_path, inputs, ["pass"] * 2, mode="multi", calls=2)
    assert result["outcome"] == "incomplete"
    assert result["reason"] == "call_limit"
    assert result["calls_used"] == 2
    assert v.resume_verify(tmp_path / "run", _adapter=adapter) == result
    assert len(adapter.packets) == 2


def test_expired_deadline_never_dispatches(tmp_path, inputs):
    result, adapter = run(tmp_path, inputs, [], _deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert result["outcome"] == "incomplete"
    assert result["reason"] == "deadline"
    assert result["calls_used"] == 0
    assert adapter.packets == []


def test_resume_uses_frozen_proof_and_problem(tmp_path, inputs):
    problem, proof = inputs
    with mock.patch.object(v, "_run", return_value=None):
        v.verify(proof, problem=problem, transport="fake", out=tmp_path / "run")
    before = proof.read_bytes().decode()
    proof.write_text("Changed draft")
    problem.write_text("Changed problem")
    adapter = ScriptedTransport(["pass"])
    assert v.resume_verify(tmp_path / "run", _adapter=adapter)["outcome"] == "pass"
    assert adapter.packets[0]["context"]["target_candidate"]["content"] == before
    assert adapter.packets[0]["context"]["problem"]["content"] != "Changed problem"


def test_observation_failure_is_incomplete_without_new_dispatch(tmp_path, inputs):
    adapter = ScriptedTransport(["exception"])
    with mock.patch.object(v, "_run", return_value=None):
        v.verify(inputs[1], problem=inputs[0], transport="fake", out=tmp_path / "run", _adapter=adapter)
    loop = v.VerifyLoop.open(tmp_path / "run" / "state", adapter=adapter)
    loop.step()  # Prepare one review.
    loop.step()  # Claim it; simulate loss of the transport result.
    # Exercise the live observation branch with an offline exception fixture.
    loop.config["transport"]["kind"] = "codex_cli"
    with mock.patch.object(loop.bridge, "recover", side_effect=OSError("fixture observation unavailable")):
        stopped = loop.step()
    loop.config["transport"]["kind"] = "injected_inert"
    result = v._summary(tmp_path / "run", loop, stopped)
    assert result["outcome"] == "incomplete"
    assert result["reason"] == "blocked_observation_error"
    assert result["calls_used"] == len(adapter.packets) == 1


@pytest.mark.parametrize("kwargs", [{"passes": 0}, {"passes": True}, {"hours": float("nan")},
    {"calls": 0}, {"mode": "invalid"}, {"transport": "invalid"}, {"model": "a b"}])
def test_invalid_options_do_not_probe_or_create(tmp_path, inputs, kwargs):
    with mock.patch.object(v, "inspect_host", side_effect=AssertionError("no probe")):
        with pytest.raises(ValueError):
            v.verify(inputs[1], problem=inputs[0], out=tmp_path / "run", **kwargs)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("entry", [["-m", "victorique.verify"], ["-m", "victorique", "verify"]])
def test_module_cli_and_parser_hook(tmp_path, inputs, entry):
    parser = argparse.ArgumentParser()
    v.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["verify", str(inputs[1]), "--problem", str(inputs[0])])
    assert args.handler == v.run_args
    command = [sys.executable, *entry, str(inputs[1]), "--problem", str(inputs[0]),
               "--mode", "multi", "--transport", "fake", "--out", str(tmp_path / "run")]
    result = subprocess.run(command, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert summary["outcome"] == "pass"
    assert summary["calls_used"] == 3
    assert summary["tokens"] is None
    rows = [json.loads(line) for line in (tmp_path / "run" / "calls.jsonl").read_text().splitlines()]
    assert len(rows) == summary["calls_used"]
    assert all(row["policy"] == "verify" and row["role"] == "proof_reviewer" for row in rows)


def test_cli_incomplete_exit_code(tmp_path, inputs):
    result = v.main([str(inputs[1]), "--problem", str(inputs[0]), "--transport", "fake",
                     "--out", str(tmp_path / "run"), "--mode", "multi", "--calls", "2"])
    assert result == 2


@pytest.mark.parametrize("mode,expected", [("single", "pass"), ("stepwise", "pass"), ("multi", "incomplete")])
def test_one_call_review_has_exact_global_cap(tmp_path, inputs, mode, expected):
    result, adapter = run(tmp_path, inputs, ["pass"], mode=mode, calls=1)
    assert result["outcome"] == expected
    assert result["calls_limit"] == result["calls_used"] == len(adapter.packets) == 1
    resumed = v.resume_verify(tmp_path / "run", _adapter=adapter)
    assert resumed == result and len(adapter.packets) == 1
