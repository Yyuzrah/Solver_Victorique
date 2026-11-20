from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from unittest.mock import patch

import pytest

from victorique import api, adaptive_contracts as c
from victorique.fake import FakeTransport
from victorique.harness_bridge import TransportObservation


class Scripted(FakeTransport):
    def __init__(self, mode="fix"):
        self.mode = mode
        self.packets = []

    def _response(self, request):
        value = json.loads(super()._response(request))
        kind = value["binding"]["action_kind"]
        rid = value["binding"]["request_id"]
        output = value["output"]
        initial = ".L0." in rid
        if kind == "solve_obligation":
            output["content"] = "The original manuscript." if initial else "A fresh independent manuscript."
            if initial and self.mode in ("placeholder", "empty"):
                output["content"] = "TODO finish this proof" if self.mode == "placeholder" else ""
        if kind in c.GATES:
            if self.mode == "invalid" and kind == "whole_proof_review" and not rid.endswith(".retry"):
                return b"invalid JSON"
            if self.mode == "wrong_binding" and not initial:
                output["target_ref"]["plan_id"] = "single-plan"
            if (self.mode == "uncertain" or (self.mode == "regression" and not initial)
                    or (self.mode == "split" and not initial and kind == "alignment_review")):
                output.update(outcome="unknown", uncertainties=["Unresolved question."])
            elif (kind == "whole_proof_review" and self.mode in ("fix", "minor", "minor_pass", "always", "regression", "split", "wrong_binding")
                  and (initial or self.mode == "always")):
                # minor_pass keeps outcome pass with one minor note; minor says fail with it.
                output.update(outcome="pass" if self.mode == "minor_pass" else "fail", findings=[{
                    "schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                    "target_ref": value["binding"]["target_ref"], "kind": "invalid_step",
                    "severity": "minor" if self.mode in ("minor", "minor_pass") else "material",
                    "location": None, "claim": "PRIVATE previous criticism."}])
        return json.dumps(value).encode()

    def start_once(self, request):
        self.packets.append(deepcopy(request.packet))
        observation = super().start_once(request)
        rid = request.packet["binding"]["request_id"]
        kind = request.packet["binding"]["action_kind"]
        if self.mode in ("unknown", "cli_failed", "retry_exhausted") and kind == "whole_proof_review":
            if self.mode == "unknown":
                return TransportObservation({**observation.metadata, "status": "unknown", "terminal_observed": False}, None, b"pending")
            if not rid.endswith(".retry") or self.mode == "retry_exhausted":
                return TransportObservation({**observation.metadata, "status": "failed", "terminal_observed": True}, None, b"CLI failed")
        return observation

    def observe_existing(self, request):
        if self.mode == "unknown":
            return None
        return super().observe_existing(request)


def run(tmp_path, adapter, **options):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n + 0 = n for all integers n.")
    out = tmp_path / "run"
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="resample", out=out, transport="fake", **options)
    return result, out


def test_l0_three_calls_and_same_ladder_inputs(tmp_path):
    adapter = Scripted("pass")
    result, out = run(tmp_path, adapter, calls=3)
    assert result["outcome"] == "complete" and result["calls_used"] == 3
    other = Scripted("pass")
    with patch.object(api, "FakeTransport", return_value=other), redirect_stdout(io.StringIO()):
        api.solve(tmp_path / "problem.md", policy="ladder", out=tmp_path / "ladder", transport="fake", calls=3)
    def by_kind(packets):
        return {p["binding"]["action_kind"]: json.dumps((p["context"], p["template"]), sort_keys=True)
                .replace(p["binding"]["problem_ref"]["artifact_id"], "problem") for p in packets}
    assert by_kind(adapter.packets) == by_kind(other.packets)
    assert api.status(out) == result


def test_fresh_solver_has_no_old_proof_or_feedback_and_gates_bind_new_version(tmp_path):
    adapter = Scripted()
    result, out = run(tmp_path, adapter)
    assert result["outcome"] == "complete" and result["calls_used"] == 6
    solvers = [p for p in adapter.packets if p["binding"]["action_kind"] == "solve_obligation"]
    assert len(solvers) == 2
    assert solvers[0]["template"] == solvers[1]["template"]
    for packet in solvers:
        assert packet["binding"]["target_ref"] is None
        assert packet["context"]["target_candidate"] is None
        assert packet["context"]["selected_issue"] is None
        assert packet["context"]["released_feedback"] == []
        assert packet["context"]["planning_evidence"] == []
        assert "PRIVATE" not in json.dumps(packet)
        assert "original manuscript" not in json.dumps(packet)
    _, loop = api._open(out)
    candidate = loop.core.snapshot()["endpoint"]["root_candidate"]
    assert candidate["revision"] == 1 and candidate["parent_revision"] is None
    assert candidate["plan_id"] == "resample-plan-1"
    reviews = [p for p in adapter.packets if ".L1." in p["binding"]["request_id"] and p["binding"]["action_kind"] in c.GATES]
    assert len(reviews) == 2
    assert all(p["binding"]["target_ref"] == c.candidate_ref(candidate) for p in reviews)
    assert "fresh independent" in (out / "proof.md").read_text()


@pytest.mark.parametrize("mode", ["placeholder", "empty"])
def test_precheck_resamples_without_review_calls(tmp_path, mode):
    adapter = Scripted(mode)
    result, _ = run(tmp_path, adapter)
    assert [p["binding"]["action_kind"] for p in adapter.packets[:2]] == ["solve_obligation"] * 2
    assert result["outcome"] == "complete" and result["calls_used"] == 4


@pytest.mark.parametrize("mode", ["invalid", "cli_failed"])
def test_check_execution_failure_only_retries_check(tmp_path, mode):
    adapter = Scripted(mode)
    result, _ = run(tmp_path, adapter)
    kinds = [p["binding"]["action_kind"] for p in adapter.packets]
    assert kinds.count("solve_obligation") == 1
    assert kinds.count("whole_proof_review") == 2
    assert result["calls_used"] == 4 and result["outcome"] == "complete"


def test_exhausted_check_retry_does_not_trigger_resampling(tmp_path):
    adapter = Scripted("retry_exhausted")
    result, _ = run(tmp_path, adapter)
    assert result["outcome"] == "abstain" and result["calls_used"] == 4
    assert len(result["incomplete_checks"]) == 1
    assert sum(p["binding"]["action_kind"] == "solve_obligation" for p in adapter.packets) == 1


@pytest.mark.parametrize("mode,outcome", [("minor_pass", "complete"), ("minor", "abstain"), ("uncertain", "abstain")])
def test_nonblocking_or_unknown_gate_does_not_resample(tmp_path, mode, outcome):
    # A fail that lists only minor findings is inconclusive under the shared pass rule.
    result, _ = run(tmp_path, Scripted(mode))
    assert result["outcome"] == outcome and result["calls_used"] == 3
    if outcome == "abstain":
        assert result["endpoint_reason"] == "resample_check_inconclusive"


@pytest.mark.parametrize("mode", ["regression", "split", "wrong_binding"])
def test_best_version_preserved_and_old_gates_never_complete_new_candidate(tmp_path, mode):
    result, out = run(tmp_path, Scripted(mode))
    assert result["outcome"] == "abstain"
    assert (out / "proof.md").read_text() == "The original manuscript."
    assert result["unresolved_issues"]


@pytest.mark.parametrize("rounds,expected", [(0, 3), (1, 5), (2, 7)])
def test_round_limit_shared_with_ladder(tmp_path, rounds, expected):
    adapter = Scripted("always")
    result, _ = run(tmp_path, adapter, repair_rounds=rounds)
    assert result["outcome"] == "abstain" and result["calls_used"] == expected
    assert sum(p["binding"]["action_kind"] == "solve_obligation" for p in adapter.packets) == rounds + 1


def test_budget_resume_and_export_preserve_best_candidate(tmp_path):
    adapter = Scripted()
    result, out = run(tmp_path, adapter, calls=4)
    assert result["outcome"] == "abstain" and result["calls_used"] == 4
    assert "original manuscript" in (out / "proof.md").read_text()
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        resumed = api.resume(out)
    assert resumed == result == api.export(out)
    assert len(adapter.packets) == 4


def test_unknown_original_never_redispatched_on_resume(tmp_path):
    adapter = Scripted("unknown")
    with patch.object(api, "_run", return_value=None):
        _, out = run(tmp_path, adapter)
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        first = api.resume(out)
        before = deepcopy(adapter.packets)
        resumed = api.resume(out)
    assert adapter.packets == before and len(before) == 3
    assert first["outcome"] == resumed["outcome"] == "pending"
    assert first["calls_limit"] == resumed["calls_limit"] == 12
    assert first["deadline_utc"] == resumed["deadline_utc"]


def test_deadline_resume_cannot_extend_time_or_spend_calls(tmp_path):
    adapter = Scripted()
    with patch.object(api, "_run", return_value=None):
        _, out = run(tmp_path, adapter, calls=6)
    _, loop = api._open(out)
    deadline = loop.config["manifest"]["limits"]["stop_condition"]["deadline_utc"]
    with patch("victorique.resample.datetime") as clock, redirect_stdout(io.StringIO()):
        clock.now.return_value = c.utc(deadline) + timedelta(seconds=1)
        result = api.resume(out)
    assert result["deadline_utc"] == deadline and result["calls_limit"] == 6
    assert result["outcome"] == "abstain" and result["calls_used"] == 0


def test_completed_gates_retained_at_deadline(tmp_path):
    adapter = Scripted("pass")
    with patch.object(api, "_run", return_value=None):
        _, out = run(tmp_path, adapter, calls=3)
    with patch.object(api, "FakeTransport", return_value=adapter):
        _, loop = api._open(out)
        for _ in range(4):
            loop.step()
        with patch("victorique.resample.datetime") as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=1)
            loop.step()
    assert api.export(out)["outcome"] == "complete"


@pytest.mark.parametrize("option", ["investigate", "escalate"])
def test_forbidden_strategy_options_rejected_before_probe(tmp_path, option):
    with patch.object(api, "inspect_host", side_effect=AssertionError("no real probe")):
        with pytest.raises(ValueError, match="resample does not support"):
            api.solve(tmp_path / "absent", policy="resample", **{option: True})


def test_cli_forwards_resample_round_limit():
    from victorique import cli
    with patch("sys.argv", ["victorique", "solve", "problem.md", "--policy", "resample", "--repair-rounds", "1"]), \
            patch.object(api, "solve", return_value={}) as solve, redirect_stdout(io.StringIO()):
        cli.main()
    assert solve.call_args.kwargs["policy"] == "resample"
    assert solve.call_args.kwargs["repair_rounds"] == 1
