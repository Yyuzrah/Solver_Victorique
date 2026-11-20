import json

import pytest

from victorique import adaptive_contracts as c, shared_prefix as paired
from victorique.grading import grade_proof
from victorique.harness.grade import _packet
from tests.test_shared_prefix import frozen_e2_prefix, read


def oracle_fixture(tmp_path, monkeypatch, mode="pass", score=6):
    source, prefix, factory = frozen_e2_prefix(tmp_path, monkeypatch, mode)
    candidate = paired._classification(prefix)[1]
    ref = c.candidate_ref(candidate)
    grade_dir = tmp_path / "shared-task-1-grade-single"
    grade_dir.mkdir()
    captured = []
    grade_proof(lambda request: captured.append(request) or {"score": score, "reason": "ORACLE SPECIFIC GAP"},
                problem=prefix.config["manifest"]["problem"]["content"], candidate=candidate["content"],
                reference_solution="REFERENCE SECRET ONLY V5", guidelines="ORACLE GUIDELINES")
    request = {"candidate_ref": ref, "transport": "fake", "packet": _packet(captured[0], "fake", "grade-fixture")}
    result = {"status": "completed", "score": score, "reason": "ORACLE SPECIFIC GAP",
              "candidate_ref": ref, "calls_used": 1, "transport": "fake", "model": "fake"}
    (grade_dir / "request.json").write_text(json.dumps(request))
    (grade_dir / "result.json").write_text(json.dumps(result))
    return source, prefix, factory, grade_dir


def branch(source, out, factory, grade_dir):
    return paired.branch_e2(source, out=out, task_id="shared-task-1", repetition=1,
                            adapter_factory=factory, oracle_grade=grade_dir)


@pytest.mark.parametrize("variant,mode,score,calls", [
    ("V6", "pass", 6, 0), ("V6", "fix", 7, 4),
    ("V7", "fix", 6, 4), ("V7", "pass", 6, 5),
    ("V7", "pass", 7, 0), ("V7", "fix", 7, 0)])
def test_factorial_trigger_feedback_isolation_and_version_bound_final_grade(tmp_path, monkeypatch, variant, mode, score, calls):
    from victorique import oracle_branch as oracle
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch, mode=mode, score=score)
    candidate = paired._classification(prefix)[1]
    feedback = oracle.load_l0_grade(source, prefix, candidate, "shared-task-1", 1, grade_dir)
    plan = read(source / "pair.json")
    out = tmp_path / variant
    arm, physical = oracle.run_arm(prefix, out, plan, paired.call_rows(prefix, "shared_L0"), candidate,
        paired._classification(prefix)[0], feedback, "factorial." + variant, factory(0, variant), variant=variant)
    assert arm["additional_physical_calls"] == len(physical) == calls
    assert arm["logical_calls"] <= 12
    packets = factory(0, variant).packets
    repair = next((p for p in packets if p["binding"]["action_kind"] == "repair_obligation"), None)
    critique = [p for p in packets if p["binding"]["action_kind"] == "critique_candidate"]
    assert len(critique) == int(variant == "V7" and mode == "pass" and score < 7)
    if calls:
        assert arm["candidate_ref"]["revision"] == 2
        assert arm["final_grade"]["candidate_ref"] == arm["candidate_ref"] and arm["grading_calls"] == 1
        final = read(out / "grading-final/request.json")["packet"]["context"]
        assert final["reference_solution"] == "REFERENCE SECRET ONLY V5"
        gates = [p for p in packets if p["binding"]["action_kind"] in c.GATES]
        assert len(gates) == 2 and all(p["binding"]["target_ref"] == arm["candidate_ref"] for p in gates)
        assert repair is not None
        if variant == "V6":
            assert "ORACLE SPECIFIC GAP" in json.dumps(repair)
            assert "PRIVATE previous criticism" not in json.dumps(repair)
            assert "REFERENCE SECRET ONLY V5" not in json.dumps(packets)
            assert "ORACLE GUIDELINES" not in json.dumps(packets)
            assert "ORACLE SPECIFIC GAP" not in json.dumps(gates)
        else:
            text = json.dumps(packets)
            assert all(secret not in text for secret in ("ORACLE SPECIFIC GAP", "REFERENCE SECRET ONLY V5", "ORACLE GUIDELINES", "oracle_reference"))
            control = read(out / "invocation.json")["oracle_feedback"]
            assert not {"score", "reason", "reference_solution", "guidelines"} & set(control)
            if mode == "fix":
                assert "PRIVATE previous criticism" in json.dumps(repair)
            else:
                assert critique[0]["context"]["selected_issue"] is None
                assert critique[0]["context"]["released_feedback"] == []
                instructions = json.loads(critique[0]["template"]["instructions"])["policy_instructions"]
                assert set(instructions["check_instructions"]) == {"alignment_review", "whole_proof_review"}
    else:
        assert not packets and arm["candidate_ref"] == c.candidate_ref(candidate)


def test_v7_critic_without_blocking_finding_abstains_without_inventing_oracle_issue(tmp_path, monkeypatch):
    from victorique import oracle_branch as oracle
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    adapter = factory(0, "V7")
    original = adapter._response
    def response(request):
        value = json.loads(original(request))
        if value["binding"]["action_kind"] == "critique_candidate":
            value["output"].update(findings=[], no_gap_identified=True)
        return json.dumps(value).encode()
    monkeypatch.setattr(adapter, "_response", response)
    candidate = paired._classification(prefix)[1]
    feedback = oracle.load_l0_grade(source, prefix, candidate, "shared-task-1", 1, grade_dir)
    arm, physical = oracle.run_arm(prefix, tmp_path / "V7", read(source / "pair.json"), paired.call_rows(prefix, "shared_L0"),
        candidate, "passed", feedback, "no-issue.V7", adapter, variant="V7")
    assert arm["outcome"] == "abstain" and len(physical) == 1
    assert [p["binding"]["action_kind"] for p in adapter.packets] == ["critique_candidate"]
    assert arm["grading_calls"] == 0 and arm["final_grade"]["reused_from"] == str(grade_dir)


def test_v7_extra_critic_consumes_original_twelve_call_ceiling(tmp_path, monkeypatch):
    from victorique import oracle_branch as oracle
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    adapter = factory(0, "V7")
    adapter.mode = "always"
    candidate = paired._classification(prefix)[1]
    feedback = oracle.load_l0_grade(source, prefix, candidate, "shared-task-1", 1, grade_dir)
    arm, physical = oracle.run_arm(prefix, tmp_path / "V7", read(source / "pair.json"), paired.call_rows(prefix, "shared_L0"),
        candidate, "passed", feedback, "budget.V7", adapter, variant="V7")
    assert arm["logical_calls"] == arm["calls_limit"] == 12
    assert len([p for p in adapter.packets if p["binding"]["action_kind"] == "critique_candidate"]) == 1
    assert len(adapter.packets) == 9 and len(physical) == 10 and arm["grading_calls"] == 1


def test_v7_unknown_critic_is_observed_only_and_blocks_repair(tmp_path, monkeypatch):
    from victorique import oracle_branch as oracle
    from victorique.harness_bridge import TransportObservation
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    adapter = factory(0, "V7")
    original = adapter.start_once
    starts = []
    def pending(request):
        starts.append(request.packet["binding"]["action_kind"])
        observation = original(request)
        return TransportObservation({**observation.metadata, "status": "unknown", "terminal_observed": False}, None, b"pending")
    monkeypatch.setattr(adapter, "start_once", pending)
    monkeypatch.setattr(adapter, "observe_existing", lambda request: None)
    run = oracle.OracleLadderLoop.run
    monkeypatch.setattr(oracle.OracleLadderLoop, "run", lambda self: run(self, max_steps=5))
    candidate = paired._classification(prefix)[1]
    feedback = oracle.load_l0_grade(source, prefix, candidate, "shared-task-1", 1, grade_dir)
    arguments = (prefix, tmp_path / "V7", read(source / "pair.json"), paired.call_rows(prefix, "shared_L0"),
                 candidate, "passed", feedback, "unknown.V7", adapter)
    first, _ = oracle.run_arm(*arguments, variant="V7")
    again, _ = oracle.run_arm(*arguments, variant="V7")
    assert first["outcome"] == again["outcome"] == "pending"
    assert first["logical_calls"] == again["logical_calls"] == 4 and starts == ["critique_candidate"]
    assert first["grading_calls"] == again["grading_calls"] == 0


def test_oracle_repairs_reference_rejected_l0_even_when_both_original_gates_pass(tmp_path, monkeypatch):
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    before = prefix.core.snapshot()
    reservation = paired.e2_reservation(source, task_id="shared-task-1", repetition=1, oracle_grade=grade_dir)
    assert reservation["new_physical_call_limit"] == 10 and reservation["slots"] == 2
    result = branch(source, tmp_path / "e2", factory, grade_dir)
    assert result["schema"] == "victorique.e2.v2" and set(result["arms"]) == {"V1", "V2", "V4", "V5"}
    arm = result["arms"]["V5"]
    assert arm["outcome"] == "complete" and arm["diagnostic_only"]
    assert arm["candidate_ref"]["revision"] == 2  # Never select the passing old L0 again.
    assert arm["logical_calls"] == 6 and arm["grading_calls"] == 1
    assert arm["final_grade"]["score"] == 7 and arm["final_grade"]["candidate_ref"] == arm["candidate_ref"]
    assert result["new_physical_calls"] == 4  # Repair, two cold gates, one new final grade.
    for variant in ("V1", "V2", "V4"):
        assert result["arms"][variant]["additional_physical_calls"] == 0
    packets = factory(0, "V5").packets
    repair = next(p for p in packets if p["binding"]["action_kind"] == "repair_obligation")
    repair_text = json.dumps(repair)
    assert "REFERENCE SECRET ONLY V5" not in repair_text
    assert "ORACLE GUIDELINES" not in repair_text
    assert "ORACLE SPECIFIC GAP" in json.dumps(repair)
    feedback = json.loads(repair["template"]["instructions"])["policy_instructions"]["oracle_reference"]
    assert feedback == {"score": 6, "reason": "ORACLE SPECIFIC GAP"}
    final_request = read(tmp_path / "e2/V5/grading-final/request.json")["packet"]["context"]
    assert final_request["reference_solution"] == "REFERENCE SECRET ONLY V5"
    assert final_request["guidelines"] == "ORACLE GUIDELINES"
    gates = [p for p in packets if p["binding"]["action_kind"] in c.GATES]
    assert len(gates) == 2
    assert all("REFERENCE SECRET" not in json.dumps(p) and "ORACLE SPECIFIC GAP" not in json.dumps(p) for p in gates)
    assert all(p["binding"]["target_ref"] == arm["candidate_ref"] for p in gates)
    assert prefix.core.snapshot() == before
    assert paired.resume_e2(tmp_path / "e2", adapter_factory=factory) == result
    assert len(factory.order) == 6


@pytest.mark.parametrize("mode", ["pass", "fix"])
def test_oracle_score_seven_adds_no_v5_calls_or_grade_and_does_not_feed_other_branches(tmp_path, monkeypatch, mode):
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch, mode, score=7)
    result = branch(source, tmp_path / "e2", factory, grade_dir)
    arm = result["arms"]["V5"]
    assert arm["additional_physical_calls"] == arm["grading_calls"] == 0
    assert arm["final_grade"]["reused_from"] == str(grade_dir)
    assert not (tmp_path / "e2/V5/state").exists()
    assert not factory(0, "V5").packets
    for variant in ("V1", "V2", "V4"):
        assert all("REFERENCE SECRET" not in json.dumps(p) and "ORACLE SPECIFIC GAP" not in json.dumps(p)
                   for p in factory(0, variant).packets)


def test_oracle_repair_limit_and_original_deadline_include_only_generation_calls(tmp_path, monkeypatch):
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    factory(0, "V5").mode = "always"
    result = branch(source, tmp_path / "e2", factory, grade_dir)
    arm = result["arms"]["V5"]
    assert arm["logical_calls"] == arm["calls_limit"] == 12
    assert arm["outcome"] == "abstain" and arm["grading_calls"] == 1
    assert arm["additional_physical_calls"] == 10
    assert arm["deadline_utc"] == read(source / "pair.json")["deadline_utc"]
    assert arm["candidate_ref"]["revision"] > 1
    config = read(tmp_path / "e2/V5/state/run-config.json")
    assert config["manifest"]["limits"]["global_call_limit"] == 12
    assert config["manifest"]["limits"]["stop_condition"]["deadline_utc"] == arm["deadline_utc"]


@pytest.mark.parametrize("change", ["unknown", "proof", "candidate_ref", "problem", "task"])
def test_oracle_requires_completed_exact_source_grade_before_any_new_calls(tmp_path, monkeypatch, change):
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    request, result = read(grade_dir / "request.json"), read(grade_dir / "result.json")
    if change == "unknown":
        result.update(status="unknown", score=None)
    elif change == "proof":
        request["packet"]["context"]["candidate"] = "different manuscript"
    elif change == "candidate_ref":
        result["candidate_ref"]["revision"] += 1
    elif change == "problem":
        request["packet"]["context"]["problem"] = "different problem"
    else:
        grade_dir = tmp_path / "another-task-grade-single"
        grade_dir.mkdir()
    (grade_dir / "request.json").write_text(json.dumps(request))
    (grade_dir / "result.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="oracle"):
        branch(source, tmp_path / "e2", factory, grade_dir)
    assert len(factory.order) == 3 and not (tmp_path / "e2").exists()


def test_oracle_unknown_final_grade_is_only_observed_on_resume(tmp_path, monkeypatch):
    from victorique import api
    from victorique.harness import grade
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch)
    attempts = []
    def pending(request, *, directory, candidate_ref, **kwargs):
        attempts.append(directory)
        directory.mkdir()
        api._write(directory / "request.json", {"transport": "callable", "candidate_ref": candidate_ref})
        from victorique.call_evidence import evidence
        value = {"status": "unknown", "calls_used": 1, "candidate_ref": candidate_ref, "score": None,
                 **evidence()}
        api._write(directory / "result.json", value)
        return value
    monkeypatch.setattr(grade, "grade_reference", pending)
    result = branch(source, tmp_path / "e2", factory, grade_dir)
    assert result["arms"]["V5"]["grading_status"] == "unknown"
    assert paired.resume_e2(tmp_path / "e2", adapter_factory=factory) == result
    assert len(attempts) == 1 and len(factory.order) == 6
    assert result["physical_calls"] == 4
    rows = [json.loads(line) for line in (tmp_path / "e2/calls.jsonl").read_text().splitlines()]
    assert rows[-1]["status"] == "unknown" and rows[-1]["role"] == "reference_grader"


def test_oracle_grade_binding_ignores_windows_line_endings():
    # The solver manifest keeps CRLF from Windows problem files; grader contexts hold LF.
    from victorique.oracle_branch import _lf
    assert _lf("Line one,\r\nline two.\r\n") == "Line one,\nline two.\n" == _lf("Line one,\nline two.\n")
    assert _lf(None) is None
