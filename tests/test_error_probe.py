from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from victorique import adaptive_contracts as c, api, error_probe as probe
from victorique.fake import FakeTransport
from victorique.harness.codex_transport import CodexFixtureTransport
from victorique.harness.safety import json_bytes


LIVE_DEADLINE = probe.LATEST_DEADLINE


@pytest.fixture(autouse=True)
def synthetic_deadline_window(monkeypatch):
    # Exercise real fake-transport clocks after the latest allowed deadline has passed.
    # The live module constant and real-plan acceptance rule stay unchanged.
    monkeypatch.setattr(probe, "LATEST_DEADLINE", datetime.now(timezone.utc) + timedelta(days=2))


def test_live_deadline_remains_fixed(tmp_path, monkeypatch):
    assert LIVE_DEADLINE == datetime(2026, 11, 14, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(probe, "LATEST_DEADLINE", LIVE_DEADLINE)
    p = plan(tmp_path)
    p.update(transport="codex", protocol_ref="abcdef1", deadline_utc=(LIVE_DEADLINE + timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError, match="global deadline"):
        probe.validate_plan(p)


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def plan(tmp_path, prior=0):
    original = "For every integer n, adding the additive identity 0 leaves n unchanged, so n + 0 = n."
    candidate = {"schema": "victorique.adaptive_candidate.v1", "artifact_id": "original-L0", "revision": 1,
        "parent_revision": None, "plan_id": "single-plan", "plan_revision": 1, "route_id": "single-route",
        "obligation_id": "root", "content": original, "assumptions": [], "dependency_refs": [], "addressed_issue_ids": []}
    (tmp_path / "problem.md").write_text("Prove n + 0 = n for every integer n.", encoding="utf-8")
    (tmp_path / "original.md").write_text(original, encoding="utf-8")
    dump(tmp_path / "candidate.json", candidate)
    sample = {"id": "probe-1", "task_id": "r001-NEM-synthetic-shared", "repetition": 1,
        "problem": str(tmp_path / "problem.md"), "proof": str(tmp_path / "original.md"),
        "candidate": str(tmp_path / "candidate.json"), "candidate_ref": c.candidate_ref(candidate),
        "original_gates": {kind: str(tmp_path / (kind + "-not-read.json")) for kind in ("alignment_review", "whole_proof_review")}}
    return {"schema": "victorique.error_probe.v1", "transport": "fake", "model": None,
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "call_cap": 200,
        "prior_calls": prior, "protocol_ref": "synthetic", "selection_ref": "separately frozen synthetic IDs",
        "selection_ids": [sample["id"]], "samples": [sample]}


class Factory:
    def __init__(self, outputs=None, scenario="success"):
        self.outputs = outputs or {}
        self.scenario = scenario
        self.packets = []

    def __call__(self, directory, packet):
        self.packets.append(deepcopy(packet))
        kind = packet["binding"]["action_kind"]
        index = sum(p["binding"]["action_kind"] == kind for p in self.packets) - 1
        sequence = self.outputs.get(kind, [])
        output = deepcopy(sequence[min(index, len(sequence) - 1)]) if sequence else probe._fake_output(kind, packet["context"])
        return CodexFixtureTransport.create(directory, response_bytes=json_bytes({
            "schema": packet["response_contract"]["schema"], "binding": packet["binding"], "output": output}),
            output_schemas=probe.SCHEMAS, scenario=self.scenario.get(kind, "success") if isinstance(self.scenario, dict) else self.scenario,
            timeout_seconds=2, max_capture_bytes=1024 * 1024)


class Gates(FakeTransport):
    def __init__(self, severity=None):
        self.packets = []
        self.severity = severity

    def _response(self, request):
        self.packets.append(deepcopy(request.packet))
        response = json.loads(super()._response(request))
        if self.severity and request.packet["binding"]["action_kind"] == "whole_proof_review":
            output = response["output"]
            content = request.packet["context"]["target_candidate"]["content"]
            output["findings"] = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "synthetic-finding",
                "target_ref": output["target_ref"], "kind": "invalid_step", "severity": self.severity,
                "location": {"start_byte": 0, "end_byte": len(content.encode()), "quote": content},
                "claim": "Synthetic full finding retained."}]
            output["outcome"] = "fail"
        return json_bytes(response)


def row(result):
    return result["samples"]["probe-1"]


def test_dry_plan_only_and_frozen_selection_budget_deadline(tmp_path):
    p = plan(tmp_path)
    assert probe.run(p, tmp_path / "dry")["per_probe_reservation"] == 5
    assert not (tmp_path / "dry").exists()
    for key, value in (("selection_ids", []), ("call_cap", 201), ("model", "override"),
                       ("deadline_utc", (probe.LATEST_DEADLINE + timedelta(seconds=1)).isoformat())):
        bad = deepcopy(p)
        bad[key] = value
        with pytest.raises(ValueError):
            probe.validate_plan(bad)


@pytest.mark.parametrize("severity,detected", [(None, False), ("minor", False), ("material", True)])
def test_four_calls_version_binding_full_findings_and_evidence(tmp_path, severity, detected):
    p = plan(tmp_path)
    factory, gates = Factory(), Gates(severity)
    out = tmp_path / "out"
    result = probe.run(p, out, execute=True, _adapter_factory=factory, _gate_adapter=gates)
    assert result["charged_calls"] == 4, result
    assert row(result)["denominator_eligible"] is True and row(result)["detected"] is detected
    assert row(result)["gate_complete"] is True and row(result)["gate_accepts"] is (severity is None)
    assert result["label"] == "model-validated injected errors"
    assert [packet["binding"]["action_kind"] for packet in factory.packets] == ["inject", "validate"]
    assert len({packet["binding"]["session_id"] for packet in factory.packets}) == 2
    assert len(gates.packets) == 2
    modified = json.loads((out / "probe-1/modified-candidate.json").read_text())
    assert modified["revision"] == 2 and modified["parent_revision"] == 1
    assert row(result)["attempts"]["gates"]["result"]["candidate_ref"] == c.candidate_ref(modified)
    calls = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 4 and all("finish_reason" in call and "issue_lists" in call for call in calls)
    assert all(call["source_binding"]["candidate_ref"] == p["samples"][0]["candidate_ref"] for call in calls)
    if severity:
        review = next(call for call in calls if call["role"] == "proof_reviewer")
        assert review["review_output"]["findings"][0]["severity"] == severity
    for attempt in ("inject-0", "validate-0"):
        assert (out / "probe-1" / attempt / "carrier.json").is_file()
        assert (out / "probe-1" / attempt / "response.bin").is_file()
    assert probe.resume(out, _gate_adapter=gates) == result
    assert len(gates.packets) == 2


def test_gate_prompts_and_schemas_are_original_l0_and_inputs_are_blind(tmp_path):
    p = plan(tmp_path)
    gates, original = Gates(), Gates()
    result = probe.run(p, tmp_path / "out", execute=True, _gate_adapter=gates)
    assert row(result)["denominator_eligible"] is True
    _, loop = api._create_run(p["samples"][0]["problem"], policy="single", calls=3, hours=1,
        out=tmp_path / "ordinary", transport="fake", _adapter=original)
    loop.run()
    expected = {packet["binding"]["action_kind"]: packet for packet in original.packets
                if packet["binding"]["action_kind"] in ("alignment_review", "whole_proof_review")}
    assert set(expected) == {"alignment_review", "whole_proof_review"}
    for packet in gates.packets:
        kind = packet["binding"]["action_kind"]
        native = expected[kind]
        assert packet["template"] == native["template"]
        assert packet["response_contract"]["schema"] == native["response_contract"]["schema"]
        assert packet["response_contract"]["fields"] == native["response_contract"]["fields"]
        assert set(packet["context"]) == set(native["context"])
        context = packet["context"]
        assert context["selected_issue"] is None and context["released_feedback"] == []
        assert context["planning_evidence"] == [] and context["dependency_closure"] == []
        assert context["problem"]["content"] == native["context"]["problem"]["content"]
        assert "Thus 0 = 1." in context["target_candidate"]["content"]
        raw = json.dumps(packet)
        assert all(secret not in raw for secret in ("Synthetic invalid equality.", "old_span", "new_span", "no_meta_comments", "reference_score"))
    gate_schemas = json.loads((tmp_path / "out/probe-1/gates/state/role-output-schemas.json").read_text())
    assert gate_schemas == loop.schemas


def test_exact_unique_single_span_and_no_markers():
    valid = {"old_span": "A", "new_span": "C", "mutated": "C B", "explanation": "Separate."}
    assert probe.validate_injection(valid, "A B") == valid
    for original, patch in (("A A", {}), ("A B", {"mutated": "C D"}), ("A B", {"old_span": ""}),
                            ("A B", {"new_span": "[INJECTED]", "mutated": "[INJECTED] B"})):
        with pytest.raises(ValueError):
            probe.validate_injection({**valid, **patch}, original)


def test_only_one_extra_injection_attempt_then_four_remaining_stages(tmp_path):
    p = plan(tmp_path)
    original = Path(p["samples"][0]["proof"]).read_text()
    good = probe._fake_output("inject", {"original": original})
    bad = {**good, "mutated": "unreported change"}
    factory = Factory({"inject": [bad, good]})
    result = probe.run(p, tmp_path / "out", execute=True, _adapter_factory=factory)
    assert result["charged_calls"] == 5 and row(result)["retry_stage"] == "inject"
    assert row(result)["denominator_eligible"]


@pytest.mark.parametrize("decision", ["refuted", "unresolved"])
def test_known_validation_failure_retries_once(tmp_path, decision):
    p = plan(tmp_path)
    good = probe._fake_output("validate", {})
    factory = Factory({"validate": [{**good, "decision": decision}, good]})
    result = probe.run(p, tmp_path / "out", execute=True, _adapter_factory=factory)
    assert result["charged_calls"] == 5 and row(result)["retry_stage"] == "validate"
    assert row(result)["denominator_eligible"]


def test_failed_confirmation_excluded_and_injection_retry_cannot_retry_validation(tmp_path):
    p = plan(tmp_path)
    good = probe._fake_output("inject", {"original": Path(p["samples"][0]["proof"]).read_text()})
    bad = {**good, "mutated": "unreported change"}
    unconfirmed = {**probe._fake_output("validate", {}), "decision": "refuted"}
    factory = Factory({"inject": [bad, good], "validate": [unconfirmed]})
    gates = Gates()
    result = probe.run(p, tmp_path / "out", execute=True, _adapter_factory=factory, _gate_adapter=gates)
    assert result["charged_calls"] == 3 and not row(result)["denominator_eligible"]
    assert len(factory.packets) == 3 and gates.packets == []


def test_unknown_attempt_never_reissued_and_keeps_reservation(tmp_path):
    p = plan(tmp_path, prior=195)
    factory = Factory(scenario="mismatch")
    out = tmp_path / "out"
    result = probe.run(p, out, execute=True, _adapter_factory=factory)
    assert row(result)["status"] == "unknown" and result["charged_calls"] == 200
    assert row(result)["retry_stage"] is None
    again = probe.resume(out, _adapter_factory=factory)
    assert row(again)["status"] == "unknown" and len(factory.packets) == 1


def test_unknown_validation_does_not_retry_or_enter_denominator(tmp_path):
    p = plan(tmp_path)
    factory, gates = Factory(scenario={"validate": "mismatch"}), Gates()
    out = tmp_path / "out"
    result = probe.run(p, out, execute=True, _adapter_factory=factory, _gate_adapter=gates)
    assert row(result)["status"] == "unknown" and result["charged_calls"] == 5
    assert result["coverage"]["validated_injection_count"] == 0
    assert row(result)["retry_stage"] is None and gates.packets == []
    probe.resume(out, _adapter_factory=factory, _gate_adapter=gates)
    assert len(factory.packets) == 2 and gates.packets == []
    path = out / "probe-1/validate-0/request.json"
    record = json.loads(path.read_text())
    record["packet"]["context"]["modified"] = "Another candidate"
    dump(path, record)
    with pytest.raises(ValueError, match="frozen source/candidate"):
        probe.resume(out, _adapter_factory=factory, _gate_adapter=gates)
    assert len(factory.packets) == 2


@pytest.mark.parametrize("severity,detected", [(None, None), ("material", True)])
def test_missing_gate_is_not_a_false_negative(tmp_path, severity, detected):
    class Incomplete(Gates):
        def _response(self, request):
            raw = super()._response(request)
            if request.packet["binding"]["action_kind"] == "alignment_review":
                return b"invalid gate result"
            return raw
    p = plan(tmp_path)
    result = probe.run(p, tmp_path / "out", execute=True, _gate_adapter=Incomplete(severity))
    assert result["charged_calls"] == 4 and row(result)["denominator_eligible"] is True
    assert row(result)["gate_complete"] is False and row(result)["gate_accepts"] is None
    assert row(result)["detected"] is detected
    assert result["coverage"]["gate_complete_count"] == 0
    assert result["coverage"]["gate_incomplete_count"] == 1


def test_validation_with_meta_comments_is_excluded_after_only_one_retry(tmp_path):
    p = plan(tmp_path)
    factory = Factory({"validate": [{**probe._fake_output("validate", {}), "no_meta_comments": False}]})
    gates = Gates()
    result = probe.run(p, tmp_path / "out", execute=True, _adapter_factory=factory, _gate_adapter=gates)
    assert result["charged_calls"] == 3 and row(result)["denominator_eligible"] is False
    assert gates.packets == [] and len(factory.packets) == 3


def test_full_reservation_required_and_deadline_prevents_new_dispatch(tmp_path):
    p = plan(tmp_path, prior=196)
    result = probe.run(p, tmp_path / "budget", execute=True)
    assert result["samples"] == {} and result["unexecuted"] == ["probe-1"]
    p["prior_calls"] = 0
    p["deadline_utc"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert probe.run(p, tmp_path / "expired", execute=True)["samples"] == {}
    p["deadline_utc"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="frozen"):
        probe.run(p, tmp_path / "expired", execute=True)


def test_windows_crlf_proof_and_problem_files_match_lf_candidate(tmp_path):
    # proof.md and problem files are written in text mode, so on Windows they hold CRLF.
    p = plan(tmp_path)
    text = "First line of the proof.\nSecond line: adding 0 leaves n unchanged, so n + 0 = n."
    candidate = json.loads((tmp_path / "candidate.json").read_text(encoding="utf-8"))
    candidate["content"] = text
    dump(tmp_path / "candidate.json", candidate)
    (tmp_path / "original.md").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    (tmp_path / "problem.md").write_bytes(b"Prove n + 0 = n\r\nfor every integer n.")
    p["samples"][0]["candidate_ref"] = c.candidate_ref(candidate)
    out = tmp_path / "out"
    result = probe.run(p, out, execute=True, _adapter_factory=Factory(), _gate_adapter=Gates())
    assert result["charged_calls"] == 4, result
    frozen = json.loads((out / "probe-1" / "inputs.json").read_text(encoding="utf-8"))
    assert frozen["original"] == text and "\r" not in frozen["problem"]


def test_source_candidate_mismatch_rejected_before_any_call(tmp_path):
    p = plan(tmp_path)
    Path(p["samples"][0]["proof"]).write_text("Different proof", encoding="utf-8")
    factory = Factory()
    with pytest.raises(ValueError, match="binding"):
        probe.run(p, tmp_path / "out", execute=True, _adapter_factory=factory)
    assert factory.packets == []
