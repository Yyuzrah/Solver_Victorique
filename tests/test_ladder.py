from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import threading
import time
from unittest.mock import patch

import pytest

from victorique import api
from victorique.fake import FakeTransport
from victorique.harness_bridge import TransportObservation
from victorique.precheck import precheck


class Scripted(FakeTransport):
    def __init__(self, mode="fix"):
        self.mode = mode
        self.kinds = []
        self.gates = 0
        self.lock = threading.Lock()
        self.active = self.maximum = 0
        self.review_seconds = 0
        self.intervals = []
        self.unknown = None

    def _response(self, request):
        value = json.loads(super()._response(request))
        output = value["output"]
        kind = value["binding"]["action_kind"]
        target = request.packet["context"]["target_candidate"]
        if kind == "solve_obligation" and self.mode == "empty":
            output["content"] = ""
        if kind == "solve_obligation" and self.mode in ("placeholder", "placeholder_refuted"):
            output["content"] = "TODO omitted proof"
        if kind == "repair_obligation":
            if self.mode in ("refuted", "unresolved", "investigate", "contradiction", "placeholder_refuted"):
                for item in output["assessments"]:
                    item["decision"] = "unresolved" if self.mode == "unresolved" else "refuted"
                output["content"] = "This unauthorized changed text must never be used."
            elif self.mode == "regression":
                output["content"] = "A new draft that regresses."
        if kind == "whole_proof_review":
            with self.lock:
                self.gates += 1
                count = self.gates
            failing = self.mode in ("fix", "regression", "refuted", "unresolved", "investigate", "minor", "contradiction") and (count == 1 or self.mode in ("regression", "unresolved", "investigate"))
            if failing or (self.mode == "minor_pass" and count == 1):
                # minor_pass keeps outcome pass with one minor note; minor says fail with it.
                output["outcome"] = "pass" if self.mode == "minor_pass" else "fail"
                output["findings"] = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                    "target_ref": value["binding"]["target_ref"], "kind": "invalid_step",
                    "severity": "minor" if self.mode in ("minor", "minor_pass") else "material",
                    "location": None, "claim": "A repeated concrete gap."}]
            if self.mode == "invalid" and count == 1:
                return b"invalid JSON"
        if kind == "alignment_review" and self.mode == "contradiction" and ".L1." in value["binding"]["request_id"]:
            output["outcome"] = "unknown"
            output["uncertainties"] = ["A new doubt on the same unchanged version."]
        if kind == "alignment_review" and self.mode == "regression" and target["revision"] > 1:
            output["outcome"] = "unknown"
            output["uncertainties"] = ["Regression"]
        return json.dumps(value).encode()

    def start_once(self, request):
        kind = request.packet["binding"]["action_kind"]
        self.kinds.append(kind)
        if kind.endswith("review"):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            started = time.perf_counter()
            time.sleep(.25)
            with self.lock:
                ended = time.perf_counter()
                self.review_seconds += ended - started
                self.intervals.append((started, ended))
                self.active -= 1
        result = super().start_once(request)
        if self.mode == "cli_failed" and kind == "whole_proof_review" and self.gates == 1:
            return TransportObservation({**result.metadata, "status": "failed", "terminal_observed": True}, None, b"CLI failed")
        if self.mode == "unknown" and kind.endswith("review"):
            metadata = {**result.metadata, "status": "unknown", "terminal_observed": False}
            self.unknown = TransportObservation(metadata, None, b"pending")
            return self.unknown
        return result

    def observe_existing(self, request):
        if self.mode == "unknown":
            return None
        return super().observe_existing(request)


def run(tmp_path, adapter, **options):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n + 0 = n for every integer n.")
    out = tmp_path / "run"
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="ladder", out=out, transport="fake", **options)
    return result, out


def test_simple_ladder_three_calls(tmp_path):
    result, out = run(tmp_path, Scripted("pass"), calls=3)
    assert result["outcome"] == "complete" and result["calls_used"] == 3
    assert {json.loads(line)["rung"] for line in (out / "calls.jsonl").read_text().splitlines()} == {"L0"}


@pytest.mark.parametrize("policy", ["single", "ladder"])
def test_two_gates_inflight_and_budget(tmp_path, policy):
    adapter = Scripted("pass")
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n+0=n.")
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy=policy, transport="fake", calls=3, out=tmp_path / "run")
    assert adapter.maximum == 2
    assert result["calls_used"] == 3
    # Overlap saves one complete fake review latency, not merely interleaved bookkeeping.
    elapsed = max(end for _, end in adapter.intervals) - min(start for start, _ in adapter.intervals)
    assert elapsed < adapter.review_seconds * .8


def test_refuted_issue_preserves_original_body(tmp_path):
    result, out = run(tmp_path, Scripted("refuted"))
    assert result["outcome"] == "complete"
    assert result["calls_used"] == 6
    assert "unauthorized" not in (out / "proof.md").read_text()
    _, loop = api._open(out)
    candidate = loop.core.snapshot()["endpoint"]["root_candidate"]
    assert candidate["revision"] == 1


@pytest.mark.parametrize("mode", ["placeholder", "empty"])
def test_placeholder_bypasses_first_reviews(tmp_path, mode):
    adapter = Scripted(mode)
    result, _ = run(tmp_path, adapter)
    assert adapter.kinds[:2] == ["solve_obligation", "repair_obligation"]
    assert result["outcome"] == "complete"
    assert result["calls_used"] == 4


@pytest.mark.parametrize("mode", ["invalid", "cli_failed"])
def test_failed_check_retries_only_that_check(tmp_path, mode):
    adapter = Scripted(mode)
    result, _ = run(tmp_path, adapter)
    assert result["outcome"] == "complete" and result["calls_used"] == 4
    assert not result["incomplete_checks"]
    assert adapter.kinds.count("whole_proof_review") == 2
    assert adapter.kinds.count("alignment_review") == 1
    assert "repair_obligation" not in adapter.kinds


@pytest.mark.parametrize("mode,outcome,reason", [("minor_pass", "complete", "ladder_complete"),
                                                 ("minor", "abstain", "ladder_check_inconclusive")])
def test_minor_does_not_repair(tmp_path, mode, outcome, reason):
    # One pass rule (gate_passes): a check that says fail is not a pass, even when
    # its findings are all minor. With no blocking finding it is inconclusive.
    adapter = Scripted(mode)
    result, _ = run(tmp_path, adapter)
    assert result["outcome"] == outcome and result["calls_used"] == 3
    assert result["endpoint_reason"] == reason
    assert len(result["non_blocking"]) == 1 and not result["unresolved_issues"]
    assert "repair_obligation" not in adapter.kinds
    assert [row["outcome"] for row in result["inconclusive_checks"]] == ([] if mode == "minor_pass" else ["fail"])


def test_unresolved_is_not_refuted_and_best_preserved(tmp_path):
    adapter = Scripted("unresolved")
    result, out = run(tmp_path, adapter, investigate=True)
    assert result["outcome"] == "abstain"
    assert "investigate" not in adapter.kinds
    assert "unauthorized" not in (out / "proof.md").read_text()


def test_investigation_only_after_repeated_refuted_claim(tmp_path):
    adapter = Scripted("investigate")
    result, out = run(tmp_path, adapter, investigate=True)
    assert adapter.kinds.count("investigate") == 1
    assert result["outcome"] == "abstain"
    _, loop = api._open(out)
    requests = loop.core.snapshot()["exchange"]["requests"]
    repair = requests["ladder.L1.repair.2"]["record"]
    assert "released_investigation" in repair["template"]["instructions"]


def test_regressed_draft_not_exported(tmp_path):
    result, out = run(tmp_path, Scripted("regression"))
    assert result["outcome"] == "abstain"
    assert "regresses" not in (out / "proof.md").read_text()
    assert result["unresolved_issues"]


def test_full_escalation_shares_remaining_budget_and_deadline(tmp_path):
    result, out = run(tmp_path, Scripted("fix"), repair_rounds=0, escalate=True, calls=12)
    assert result["calls_used"] <= 12
    assert result["outcome"] == "complete"
    parent = json.loads((out / "state" / "run-config.json").read_text())
    child = json.loads((out / "state" / "escalated" / "run-config.json").read_text())
    assert child["manifest"]["limits"]["global_call_limit"] == 9
    assert child["manifest"]["limits"]["max_inflight"] == 1
    assert child["manifest"]["limits"]["stop_condition"] == parent["manifest"]["limits"]["stop_condition"]
    assert len((out / "calls.jsonl").read_text().splitlines()) == result["calls_used"]
    with redirect_stdout(io.StringIO()):
        assert api.resume(out) == result
    assert api.status(out) == result


def test_unknown_does_not_redispatch(tmp_path):
    adapter = Scripted("unknown")
    with patch.object(api, "_run", return_value=None):
        _, out = run(tmp_path, adapter)
    with patch.object(api, "FakeTransport", return_value=adapter):
        _, loop = api._open(out)
        loop.run(max_steps=8)
        original = list(adapter.kinds)
        _, resumed = api._open(out)
        resumed.run(max_steps=3)
    assert adapter.kinds == original
    assert len(original) == 3
    assert adapter.maximum == 2


def test_refuted_precheck_marker_stops_after_one_repair(tmp_path):
    # The precheck is deterministic, so repeating it on the unchanged draft only spends calls.
    adapter = Scripted("placeholder_refuted")
    result, _ = run(tmp_path, adapter)
    assert adapter.kinds == ["solve_obligation", "repair_obligation"]
    assert result["outcome"] == "abstain" and result["calls_used"] == 2
    assert result["endpoint_reason"] == "ladder_precheck_unresolved"


def test_precheck_empty_and_missing_answer():
    candidate = {"content": "", "artifact_id": "a", "revision": 1, "plan_id": "p", "plan_revision": 1, "route_id": "r", "obligation_id": "o"}
    assert precheck(candidate, "Prove it")
    candidate["content"] = "A discussion with no conclusion."
    assert precheck(candidate, "Compute the integer value.")


def test_repair_judgments_do_not_leak_into_cold_review(tmp_path):
    result, out = run(tmp_path, Scripted("fix"))
    assert result["outcome"] == "complete"
    _, loop = api._open(out)
    rows = loop.core.snapshot()["exchange"]["requests"].values()
    assert all("assessments" not in row["record"]["context"]["target_candidate"]
               for row in rows if row["record"]["kind"].endswith("review"))


def test_completed_gates_deliver_even_at_deadline(tmp_path):
    adapter = Scripted("pass")
    with patch.object(api, "_run", return_value=None):
        _, out = run(tmp_path, adapter, calls=3)
    with patch.object(api, "FakeTransport", return_value=adapter):
        _, loop = api._open(out)
        for _ in range(4):
            loop.step()
        base = loop.core.snapshot()["exchange"]
        loop._finish(base, "ladder_deadline")
        assert api.export(out)["outcome"] == "complete"


def test_retry_stops_at_shared_budget(tmp_path):
    adapter = Scripted("invalid")
    result, _ = run(tmp_path, adapter, calls=3)
    assert result["outcome"] == "abstain" and result["calls_used"] == 3
    assert adapter.kinds.count("whole_proof_review") == 1


def test_later_doubt_invalidates_earlier_pass_on_same_version(tmp_path):
    result, _ = run(tmp_path, Scripted("contradiction"), repair_rounds=1)
    assert result["outcome"] == "abstain"
    assert result["unresolved_questions"]


@pytest.mark.parametrize("current_root", [True, False])
def test_child_export_retains_old_checked_draft_and_its_closure(current_root):
    from victorique import adaptive_contracts as c
    from victorique.ladder import _child_export
    old = {"artifact_id": "root", "revision": 1, "plan_id": "old-plan", "plan_revision": 1,
           "route_id": "route", "obligation_id": "root", "content": "The earlier checked draft."}
    new = {**old, "plan_id": "new-plan", "content": "An unchecked replacement."}
    dependency = {**old, "artifact_id": "dependency", "obligation_id": "lemma", "content": "The original lemma."}
    gate = {"target_ref": c.candidate_ref(old), "outcome": "pass", "findings": [], "uncertainties": []}
    child = {"exchange": {"manifest": {"limits": {}}, "requests": {"old.alignment": {
        "phase": "returned_valid", "first_result_observation_id": "observed",
        "record": {"kind": "alignment_review", "context": {"target_candidate": old,
                   "dependency_closure": [dependency]}}}}, "observations": [{
        "observation_id": "observed", "parsed_response": {"output": gate}}]},
        "endpoint": {"root_candidate": new if current_root else None,
                     "root_dependency_closure": [new] if current_root else [],
                     "reducer_endpoint": {"decision": "stop"}}}
    result = _child_export(child)
    assert result["root_candidate"] == old
    assert result["root_dependency_closure"] == [old, dependency]
    assert result["reducer_endpoint"]["decision"] == "abstain"
