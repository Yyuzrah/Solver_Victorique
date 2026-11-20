from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from victorique import api, adaptive_contracts as c, adaptive_exchange as exchange, bestofn
from victorique.fake import FakeTransport
from victorique.harness_bridge import TransportObservation


PROBLEM = "Prove n + 0 = n for every integer n."


def reference(tmp_path, costs=None, *, run_id="paired-full", problem=PROBLEM, missing=()):
    directory = tmp_path / run_id
    (directory / "state").mkdir(parents=True)
    (directory / "invocation.json").write_text(json.dumps({"parameters": {"policy": "full"}}))
    (directory / "state/run-config.json").write_text(json.dumps({"manifest": {
        "session_id": run_id, "problem": {"content": problem}}}))
    if costs is None:
        costs = [("solver", 100), ("critic", 50), ("alignment_reviewer", 50),
                 ("proof_reviewer", 50), ("planner", 250)]
    (directory / "summary.json").write_text(json.dumps({"run_id": run_id, "policy": "full", "outcome": "complete",
                                                       "calls_used": len(costs)}))
    rows = [{"run_id": run_id, "request_id": f"full.{i}", "policy": "full", "role": role,
             "status": "completed", "tokens": {"input_tokens_including_cache": cost - 10, "output_tokens": 10}}
            for i, (role, cost) in enumerate(costs)]
    for index in missing:
        rows[index]["tokens"] = None
    path = directory / "calls.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # Any accidental proof/grade read would see data with no mathematical use.
    (directory / "proof.md").write_text("PRIVATE FULL PROOF")
    (directory / "grade.json").write_text("PRIVATE FULL GRADE")
    return path


class Scripted(FakeTransport):
    def __init__(self, mode="pass"):
        self.mode = mode
        self.packets = []

    def _response(self, request):
        value = json.loads(super()._response(request))
        kind = value["binding"]["action_kind"]
        output = value["output"]
        if self.mode.startswith("solve_invalid_") and value["binding"]["request_id"] == "bestofn.solve." + self.mode[-1]:
            return b"{}"  # e.g. a timed-out generation: no valid manuscript
        if kind == "solve_obligation":
            output["content"] = "Exact original manuscript " + value["binding"]["request_id"] + "."
        if kind == "compare_candidates":
            output["winner_ref"] = deepcopy(value["binding"]["opponent_ref"])
            output["reason"] = "PRIVATE PREVIOUS COMPARISON"
            if self.mode == "selection_unknown":
                output.update(outcome="unknown", winner_ref=None)
            if self.mode == "wrong_winner":
                output["winner_ref"]["revision"] = 2
            if self.mode == "rewrite":
                output["content"] = "Silently rewritten proof"
        if kind in c.GATES:
            if self.mode == "gate_unknown":
                output.update(outcome="unknown", uncertainties=["Unresolved gap"])
            if self.mode == "gate_wrong_version":
                output["target_ref"]["revision"] = 2
            if self.mode == "gate_minor":
                output["findings"] = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "note",
                                       "target_ref": value["binding"]["target_ref"], "kind": "ambiguity",
                                       "severity": "minor", "location": None, "claim": "Wording."}]
        return json.dumps(value).encode()

    def start_once(self, request):
        self.packets.append(deepcopy(request.packet))
        observation = super().start_once(request)
        if self.mode == "transport_unknown" and request.packet["binding"]["action_kind"] == "compare_candidates":
            return TransportObservation({**observation.metadata, "status": "unknown", "terminal_observed": False}, None, b"pending")
        return observation

    def observe_existing(self, request):
        if self.mode == "transport_unknown":
            return None
        return super().observe_existing(request)


def usage(loop, policy, _rows=api.call_rows):
    rows = _rows(loop, policy)
    for row in rows:
        amount = {"solver": 100, "critic": 50, "alignment_reviewer": 50, "proof_reviewer": 50}[row["role"]]
        row["tokens"] = {"input_tokens_including_cache": amount - 10, "output_tokens": 10}
    return rows


def run(tmp_path, mode="pass", **options):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    adapter = Scripted(mode)
    full = reference(tmp_path)
    out = tmp_path / "run"
    with patch.object(api, "FakeTransport", return_value=adapter), patch.object(bestofn, "call_rows", side_effect=usage), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=full, transport="fake", out=out, **options)
    return result, out, adapter


def test_tournament_original_manuscripts_and_same_version_gates(tmp_path):
    result, out, adapter = run(tmp_path)
    assert result["outcome"] == "complete"
    assert result["calls_used"] == 7  # 3 generations, 2 selections, 2 gates
    assert result["bestofn"]["n"] == 3 and result["bestofn"]["n_frozen"]
    assert result["bestofn"]["estimate"]["target_tokens"] == 500
    solvers = [p for p in adapter.packets if p["binding"]["action_kind"] == "solve_obligation"]
    for packet in solvers:
        ctx = packet["context"]
        assert ctx["problem"]["content"] == PROBLEM
        assert ctx["target_candidate"] is None
        assert ctx["dependency_closure"] == ctx["released_feedback"] == ctx["planning_evidence"] == []
        assert "manuscript" not in json.dumps(ctx) and "PRIVATE" not in json.dumps(packet)
    comparisons = [p for p in adapter.packets if p["binding"]["action_kind"] == "compare_candidates"]
    for packet in comparisons:
        assert "PRIVATE" not in json.dumps(packet)
        instructions = json.loads(packet["template"]["instructions"])
        assert instructions["alignment_standard"] == bestofn.adaptive_roles.INSTRUCTIONS["alignment_review"]
        assert instructions["proof_standard"] == bestofn.adaptive_roles.INSTRUCTIONS["whole_proof_review"]
    _, loop = api._open(out)
    winner = loop.core.snapshot()["endpoint"]["root_candidate"]
    assert winner["revision"] == 1 and winner["parent_revision"] is None
    assert (out / "proof.md").read_text() == "Exact original manuscript bestofn.solve.2."
    gates = [p for p in adapter.packets if p["binding"]["action_kind"] in c.GATES]
    assert len(gates) == 2
    assert all(p["binding"]["target_ref"] == c.candidate_ref(winner) for p in gates)
    assert all("PRIVATE" not in json.dumps(p) and "opponent" not in p["context"] for p in gates)


@pytest.mark.parametrize("mode", ["selection_unknown", "wrong_winner", "rewrite", "gate_unknown", "gate_wrong_version"])
def test_selection_or_gates_never_certify_wrong_version(tmp_path, mode):
    result, out, adapter = run(tmp_path, mode)
    assert result["outcome"] == "abstain"
    if mode in ("selection_unknown", "wrong_winner", "rewrite"):
        assert not any(p["binding"]["action_kind"] in c.GATES for p in adapter.packets)
    assert "Silently rewritten" not in (out / "proof.md").read_text()


def test_failed_generation_keeps_the_manuscripts_already_returned(tmp_path):
    result, out, adapter = run(tmp_path, "solve_invalid_1")
    assert result["outcome"] == "complete"
    assert result["calls_used"] == 4  # one valid and one failed generation, then two gates
    assert result["bestofn"]["n"] == 1 and result["bestofn"]["generation_stop_reason"] == "call_failed"
    assert result["bestofn"]["selected_ref"] is not None
    assert not any(p["binding"]["action_kind"] == "compare_candidates" for p in adapter.packets)
    assert (out / "proof.md").read_text() == "Exact original manuscript bestofn.solve.0."


def test_failed_first_generation_abstains(tmp_path):
    result, _, adapter = run(tmp_path, "solve_invalid_0")
    assert result["outcome"] == "abstain" and result["calls_used"] == 1
    assert result["bestofn"]["stop_reason"] == "bestofn_call_failed"
    assert not any(p["binding"]["action_kind"] in c.GATES for p in adapter.packets)


def test_gate_pass_with_a_minor_note_certifies_the_winner(tmp_path):
    result, _, _ = run(tmp_path, "gate_minor")
    assert result["outcome"] == "complete"


def test_unknown_request_resume_keeps_budget_deadline_and_does_not_resend(tmp_path):
    result, out, adapter = run(tmp_path, "transport_unknown", calls=5)
    assert result["outcome"] == "pending" and result["calls_used"] == 3
    config = (out / "state/run-config.json").read_bytes()
    (tmp_path / "paired-full/calls.jsonl").write_text("corrupted after reference was frozen")
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        resumed = api.resume(out)
    assert resumed["outcome"] == "pending"
    assert resumed["calls_limit"] == 5 and resumed["deadline_utc"] == result["deadline_utc"]
    assert resumed["bestofn"]["estimate"] == result["bestofn"]["estimate"]
    assert len(adapter.packets) == 3 and (out / "state/run-config.json").read_bytes() == config


def test_missing_generation_usage_stops_expansion_and_call_budget_reserves_gates(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    full = reference(tmp_path)
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=full, transport="fake", out=tmp_path / "run", calls=12)
    assert result["outcome"] == "complete" and result["calls_used"] == 5
    assert result["bestofn"]["n"] == 2 and result["bestofn"]["generation_stop_reason"] == "missing_usage"
    assert result["bestofn"]["actual_tokens"] is None and result["bestofn"]["missing_usage_calls"] == 5
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=full, transport="fake", out=tmp_path / "bounded", calls=5)
    assert result["outcome"] == "complete" and result["calls_used"] == 5
    assert result["bestofn"]["n"] == 2


@pytest.mark.parametrize("mutation,message", [
    ("problem", "different original"),
    ("policy", "must be a full"), ("duplicate", "duplicate full"),
])
def test_invalid_full_reference_refused_before_dispatch(tmp_path, mutation, message):
    path = reference(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "duplicate":
        rows.append(rows[0])
    if mutation == "problem":
        (path.parent / "state/run-config.json").write_text(json.dumps({"manifest": {"session_id": "paired-full", "problem": {"content": "other"}}}))
    if mutation == "policy":
        (path.parent / "invocation.json").write_text(json.dumps({"parameters": {"policy": "single"}}))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    with pytest.raises(ValueError, match=message):
        api.solve(problem, policy="bestofn", full_calls=path, transport="fake", out=tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_estimate_and_report_use_costs_without_double_counting_reasoning(tmp_path):
    path = reference(tmp_path)
    estimate = bestofn.estimate_from_full(path, PROBLEM, 12)
    assert estimate["role_mean_tokens"] == {"solver": 100, "critic": 50, "alignment_reviewer": 50, "proof_reviewer": 50}
    assert estimate["target_fallback"] == "paired_full"
    assert estimate["target_source_run_ids"] == ["paired-full"]
    assert bestofn.measured_tokens({"tokens": {"input_tokens_including_cache": 90,
        "output_tokens": 10, "cached_input_tokens": 40, "reasoning_output_tokens": 8}}) == 100
    with pytest.raises(ValueError, match="at least 5 calls"):
        bestofn.estimate_from_full(path, PROBLEM, 4)


def reference_list(tmp_path, entries, difficulty="Advanced"):
    path = tmp_path / "references.json"
    path.write_text(json.dumps({"difficulty": difficulty, "runs": [
        {"calls": str(source.relative_to(tmp_path)), "difficulty": band} for source, band in entries]}))
    return path


def test_same_problem_median_precedes_difficulty_and_reads_only_public_files(tmp_path):
    paired = reference(tmp_path, missing=(4,))
    a = reference(tmp_path, run_id="same-a")
    b = reference(tmp_path, [("solver", 501)], run_id="same-b")
    unknown = reference(tmp_path, run_id="unknown", missing=(0,))
    other = reference(tmp_path, [("solver", 900)], run_id="other", problem="Other problem")
    pool = reference_list(tmp_path, [(a, "Different"), (b, "Advanced"), (unknown, "Advanced"), (other, "Advanced")])
    allowed = {pool.resolve()}
    for source in (paired, a, b, unknown, other):
        allowed.update((source.resolve(), (source.parent / "invocation.json").resolve(),
                        (source.parent / "state/run-config.json").resolve(), (source.parent / "summary.json").resolve()))
    original_open = Path.open
    def public_only(path, *args, **kwargs):
        assert path.resolve() in allowed, str(path)
        return original_open(path, *args, **kwargs)
    with patch.object(Path, "open", public_only), patch.object(Path, "glob", side_effect=AssertionError("no directory scan")):
        estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
    assert estimate["target_tokens"] == 500.5
    assert estimate["target_fallback"] == "same_problem_median"
    assert estimate["target_source_run_ids"] == ["same-a", "same-b"]
    assert estimate["unexecuted_reason"] is None
    assert estimate["role_mean_tokens"]["solver"] == 100


def test_same_difficulty_median_and_paired_precedence(tmp_path):
    paired = reference(tmp_path, missing=(4,))
    a = reference(tmp_path, [("solver", 700)], run_id="other-a", problem="Other A")
    b = reference(tmp_path, [("solver", 701)], run_id="other-b", problem="Other B")
    excluded = reference(tmp_path, run_id="basic", problem="Other C")
    pool = reference_list(tmp_path, [(a, "Advanced"), (b, "Advanced"), (excluded, "Basic")])
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
    assert estimate["target_tokens"] == 700.5
    assert estimate["target_fallback"] == "same_difficulty_median"
    assert estimate["target_source_run_ids"] == ["other-a", "other-b"]
    complete = reference(tmp_path, run_id="complete")
    estimate = bestofn.estimate_from_full(complete, PROBLEM, 12, pool)
    assert estimate["target_tokens"] == 500
    assert estimate["target_source_run_ids"] == ["complete"]


def test_role_fallback_precedence_uses_individual_call_medians_and_sources(tmp_path):
    paired = reference(tmp_path, [("solver", 100), ("proof_reviewer", 20)])
    same_a = reference(tmp_path, [("critic", 10), ("critic", 30), ("critic", 90), ("planner", 40)],
                       run_id="same-a", missing=(3,))
    same_b = reference(tmp_path, [("critic", 50), ("solver", 900)], run_id="same-b")
    other_a = reference(tmp_path, [("alignment_reviewer", 60), ("critic", 900)], run_id="other-a")
    other_b = reference(tmp_path, [("alignment_reviewer", 80)], run_id="other-b")
    pool = reference_list(tmp_path, [(same_a, "Advanced"), (same_b, "Advanced"),
                                     (other_a, "Basic"), (other_b, "Basic")])
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
    assert estimate["role_mean_tokens"] == {"solver": 100, "critic": 40, "alignment_reviewer": 70, "proof_reviewer": 20}
    assert estimate["role_sample_counts"] == {"solver": 1, "critic": 4, "alignment_reviewer": 2, "proof_reviewer": 1}
    assert estimate["role_sources"]["solver"] == {"kind": "paired_full_mean", "run_ids": ["paired-full"], "unit": "measured_role_calls"}
    assert estimate["role_sources"]["critic"] == {"kind": "same_difficulty_full_median", "run_ids": ["same-a", "same-b"], "unit": "measured_role_calls"}
    assert estimate["role_sources"]["alignment_reviewer"]["kind"] == "all_full_median"
    assert estimate["role_sources"]["alignment_reviewer"]["run_ids"] == ["other-a", "other-b"]
    assert estimate["target_fallback"] == "paired_full" and estimate["target_tokens"] == 120
    assert estimate["unexecuted_reason"] is None


@pytest.mark.parametrize("unavailable", ["pending", "mismatched_count"])
def test_other_role_pool_requires_terminal_matching_projection(tmp_path, unavailable):
    paired = reference(tmp_path, [("planner", 1000)])
    other = reference(tmp_path, run_id="other")
    summary = json.loads((other.parent / "summary.json").read_text())
    summary["outcome" if unavailable == "pending" else "calls_used"] = "pending" if unavailable == "pending" else 9
    (other.parent / "summary.json").write_text(json.dumps(summary))
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, reference_list(tmp_path, [(other, "Advanced")]))
    assert estimate["unexecuted_reason"] is None
    assert all(amount is None for amount in estimate["role_mean_tokens"].values())
    assert all(source["kind"] == "current_generation_mean" for source in estimate["role_sources"].values())


def test_null_difficulty_skips_to_global_role_pool(tmp_path):
    paired = reference(tmp_path, [("planner", 1000)])
    other = reference(tmp_path, run_id="other")
    pool = reference_list(tmp_path, [(other, None)], difficulty=None)
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
    assert all(source["kind"] == "all_full_median" for source in estimate["role_sources"].values())
    assert estimate["role_mean_tokens"]["solver"] == 100


def test_all_roles_missing_bootstraps_current_generation_mean_then_stops(tmp_path):
    paired = reference(tmp_path, [("planner", 1000)])
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    adapter = Scripted()
    out = tmp_path / "run"
    with patch.object(api, "FakeTransport", return_value=adapter), patch.object(bestofn, "call_rows", side_effect=usage), \
         patch.object(api, "call_rows", side_effect=usage), redirect_stdout(io.StringIO()):
        _, loop = api._create_run(problem, policy="bestofn", full_calls=paired, transport="fake", out=out)
        frozen = (out / "state/run-config.json").read_bytes()
        before = api.status(out)
        assert before["outcome"] == "pending" and before["calls_used"] == 0
        assert before["bestofn"]["minimum_workflow_estimated_tokens"] is None
        result = api._run(out, loop)
    assert result["outcome"] == "complete" and result["calls_used"] == 9
    assert result["bestofn"]["n"] == 4 and result["bestofn"]["generation_stop_reason"] == "token_target"
    assert len(adapter.packets) == 9
    assert all(amount is None for amount in result["bestofn"]["estimate"]["role_mean_tokens"].values())
    for role in result["bestofn"]["effective_role_estimates"].values():
        assert role == {"tokens": 100, "sample_count": 4, "kind": "current_generation_mean",
                        "run_ids": [result["run_id"]], "unit": "generated_manuscripts"}
    assert (out / "state/run-config.json").read_bytes() == frozen


def test_missing_current_tokens_finishes_selection_with_unknown_costs(tmp_path):
    paired = reference(tmp_path, [("planner", 1000)])
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=paired, transport="fake", calls=12, out=tmp_path / "run")
    assert result["outcome"] == "complete" and result["calls_used"] == 5
    assert result["bestofn"]["n"] == 2 and result["bestofn"]["n_frozen"]
    assert result["bestofn"]["generation_stop_reason"] == "missing_usage"
    assert result["bestofn"]["actual_tokens"] is None
    assert result["bestofn"]["minimum_workflow_estimated_tokens"] is None
    assert all(role["tokens"] is None and role["sample_count"] == 0 for role in result["bestofn"]["effective_role_estimates"].values())


def test_partial_generation_usage_does_not_become_a_complete_role_mean(tmp_path):
    loop = bestofn.BestofnLoop()
    loop.config = {"manifest": {"session_id": "current-run"}, "bestofn": bestofn.estimate_from_full(
        reference(tmp_path, [("planner", 1000)]), PROBLEM, 12)}
    rows = [{"request_id": "bestofn.solve.0", "tokens": {"input_tokens_including_cache": 90, "output_tokens": 10}},
            {"request_id": "bestofn.solve.1", "tokens": None}]
    assert all(role["tokens"] is None for role in loop._role_estimates(rows).values())
    assert loop._generation_stop({"manifest": {"limits": {"global_call_limit": 12}}}, 2, rows) == "missing_usage"


def test_explicit_zero_role_cost_remains_measured(tmp_path):
    paired = reference(tmp_path)
    rows = [json.loads(line) for line in paired.read_text().splitlines()]
    rows[0]["tokens"] = {"input_tokens_including_cache": 0, "output_tokens": 0}
    paired.write_text("".join(json.dumps(row) + "\n" for row in rows))
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12)
    assert estimate["role_mean_tokens"]["solver"] == 0 and estimate["role_sample_counts"]["solver"] == 1
    assert estimate["role_sources"]["solver"]["kind"] == "paired_full_mean"


def test_missing_roles_resume_keeps_budget_deadline_and_original_request(tmp_path):
    paired = reference(tmp_path, [("planner", 1000)])
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    adapter = Scripted("transport_unknown")
    out = tmp_path / "run"
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=paired, transport="fake", calls=5, out=out)
        frozen = (out / "state/run-config.json").read_bytes()
        paired.write_text("Changed after creation")
        resumed = api.resume(out)
    assert result["outcome"] == resumed["outcome"] == "pending"
    assert result["calls_used"] == resumed["calls_used"] == 3 and len(adapter.packets) == 3
    assert resumed["calls_limit"] == 5 and resumed["deadline_utc"] == result["deadline_utc"]
    assert resumed["bestofn"]["estimate"] == result["bestofn"]["estimate"]
    assert (out / "state/run-config.json").read_bytes() == frozen


def test_missing_roles_still_obeys_deadline_and_minimum_call_budget(tmp_path):
    paired = reference(tmp_path, [("planner", 1000)])
    with pytest.raises(ValueError, match="at least 5 calls"):
        bestofn.estimate_from_full(paired, PROBLEM, 4)
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    adapter = Scripted()
    with redirect_stdout(io.StringIO()):
        out, loop = api._create_run(problem, policy="bestofn", full_calls=paired, transport="fake", calls=5,
                                   out=tmp_path / "run", _adapter=adapter,
                                   _started_at=datetime.now(timezone.utc) - timedelta(hours=3))
        result = api._run(out, loop)
    assert result["outcome"] == "abstain" and result["calls_used"] == 0 and not adapter.packets


@pytest.mark.parametrize("mutation", ["policy", "run_id", "duplicate", "summary_id", "summary_policy"])
def test_invalid_pool_run_is_refused(tmp_path, mutation):
    paired = reference(tmp_path, missing=(4,))
    other = reference(tmp_path, run_id="other")
    entries = [(other, "Advanced")]
    if mutation == "policy":
        (other.parent / "invocation.json").write_text(json.dumps({"parameters": {"policy": "single"}}))
    elif mutation == "duplicate":
        entries *= 2
    elif mutation.startswith("summary_"):
        summary = {"run_id": "other", "policy": "full", "outcome": "complete"}
        summary["run_id" if mutation == "summary_id" else "policy"] = "wrong"
        (other.parent / "summary.json").write_text(json.dumps(summary))
    else:
        rows = [json.loads(line) for line in other.read_text().splitlines()]
        rows[0]["run_id"] = "wrong"
        other.write_text("".join(json.dumps(row) + "\n" for row in rows))
    pool = reference_list(tmp_path, entries)
    with pytest.raises(ValueError):
        bestofn.estimate_from_full(paired, PROBLEM, 12, pool)


@pytest.mark.parametrize("unavailable", ["pending", "missing_summary", "unknown_call"])
def test_full_prefix_is_not_a_total_even_when_other_calls_are_complete(tmp_path, unavailable):
    paired = reference(tmp_path, run_id="paired-full")
    if unavailable == "missing_summary":
        # Simulate an absent summary without deleting a test output manually.
        original_read = Path.read_text
        def missing_summary(path, *args, **kwargs):
            if path == paired.parent / "summary.json":
                raise FileNotFoundError(path)
            return original_read(path, *args, **kwargs)
        context = patch.object(Path, "read_text", missing_summary)
    else:
        if unavailable == "pending":
            (paired.parent / "summary.json").write_text(json.dumps({"run_id": "paired-full", "policy": "full", "outcome": "pending"}))
        else:
            rows = [json.loads(line) for line in paired.read_text().splitlines()]
            rows[-1]["status"] = "unknown"
            paired.write_text("".join(json.dumps(row) + "\n" for row in rows))
        from contextlib import nullcontext
        context = nullcontext()
    other = reference(tmp_path, run_id="other", problem="Another problem")
    pool = reference_list(tmp_path, [(other, "Advanced")])
    with context:
        estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
        assert estimate["target_fallback"] == "same_difficulty_median"
        assert estimate["target_source_run_ids"] == ["other"]
        assert estimate["role_mean_tokens"]["solver"] == 100
        # An unfinished reference must also be excluded when offered in a pool.
        pool = reference_list(tmp_path, [(paired, "Advanced")])
        missing = reference(tmp_path, run_id="missing", missing=(4,))
        estimate = bestofn.estimate_from_full(missing, PROBLEM, 12, pool)
        assert estimate["target_tokens"] is None
        assert estimate["unexecuted_reason"] == "missing_target_tokens"


def test_unexecuted_retained_without_cli_and_resume_frozen(tmp_path):
    reason = "missing_target_tokens"
    paired = reference(tmp_path, missing=(4,))
    other = reference(tmp_path, run_id="other", missing=(4,))
    pool = reference_list(tmp_path, [(other, "Advanced")])
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    out = tmp_path / "run"
    with patch.object(api, "inspect_host", side_effect=AssertionError("must not inspect CLI")), \
         patch.object(FakeTransport, "start_once", side_effect=AssertionError("must not dispatch")), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=paired, full_references=pool, out=out)
        assert result["outcome"] == "unexecuted" and result["reason"] == reason
        assert result["calls_used"] == 0 and not result["proof_available"]
        assert result["bestofn"]["actual_tokens"] is None
        assert (out / "calls.jsonl").read_text() == ""
        frozen = (out / "state/run-config.json").read_bytes()
        paired.write_text("Changed after creation")
        pool.write_text("Changed after creation")
        other.write_text("Changed after creation")
        assert api.status(out) == result
        assert api.resume(out) == result
        assert api.export(out) == result
    assert (out / "state/run-config.json").read_bytes() == frozen
    assert result["bestofn"]["estimate"]["target_tokens"] is None


def test_no_pool_missing_target_and_empty_reference_keep_planned_row(tmp_path):
    paired = reference(tmp_path, missing=(4,))
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12)
    assert estimate["target_tokens"] is None
    assert estimate["target_fallback"] == "unavailable"
    assert estimate["target_source_run_ids"] == []
    assert estimate["unexecuted_reason"] == "missing_target_tokens"
    paired.write_text("")
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12)
    assert estimate["reference_calls"] == 0
    assert all(amount is None for amount in estimate["role_mean_tokens"].values())


def test_measured_zero_target_is_not_missing(tmp_path):
    paired = reference(tmp_path, missing=(4,))
    other = reference(tmp_path, [("solver", 10)], run_id="zero")
    row = json.loads(other.read_text())
    row["tokens"] = {"input_tokens_including_cache": 0, "output_tokens": 0}
    other.write_text(json.dumps(row) + "\n")
    pool = reference_list(tmp_path, [(other, "Advanced")])
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
    assert estimate["target_tokens"] == 0
    assert estimate["target_fallback"] == "same_problem_median"
    assert estimate["unexecuted_reason"] is None


@pytest.mark.parametrize("declared", [9, "missing"])
def test_summary_count_missing_or_larger_than_projection_cannot_supply_total(tmp_path, declared):
    costs = [("solver", 100), ("critic", 50), ("alignment_reviewer", 50), ("proof_reviewer", 50)] + [("planner", 50)] * 4
    paired = reference(tmp_path, costs)
    summary = json.loads((paired.parent / "summary.json").read_text())
    if declared == "missing":
        del summary["calls_used"]
    else:
        summary["calls_used"] = declared
    (paired.parent / "summary.json").write_text(json.dumps(summary))
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12)
    assert estimate["reference_calls"] == 8
    assert estimate["target_tokens"] is None
    assert estimate["role_mean_tokens"]["solver"] == 100
    assert estimate["unexecuted_reason"] == "missing_target_tokens"
    other = reference(tmp_path, run_id="other", problem="Another problem")
    pool = reference_list(tmp_path, [(other, "Advanced")])
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12, pool)
    assert estimate["target_source_run_ids"] == ["other"]
    # The same incomplete projection must not enter a fallback median either.
    missing = reference(tmp_path, run_id="missing", missing=(4,))
    pool = reference_list(tmp_path, [(paired, "Advanced")])
    assert bestofn.estimate_from_full(missing, PROBLEM, 12, pool)["target_tokens"] is None


@pytest.mark.parametrize("count", [0, 5])
def test_matching_summary_count_allows_measured_zero_or_nonzero(tmp_path, count):
    paired = reference(tmp_path, [] if count == 0 else None)
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12)
    assert estimate["reference_calls"] == count
    assert estimate["target_tokens"] == (0 if count == 0 else 500)
    assert estimate["target_fallback"] == "paired_full"
    assert estimate["unexecuted_reason"] is None


@pytest.mark.parametrize("count", [-1, True, False, 5.0, "5", None])
def test_invalid_summary_count_is_rejected(tmp_path, count):
    paired = reference(tmp_path)
    summary = json.loads((paired.parent / "summary.json").read_text())
    summary["calls_used"] = count
    (paired.parent / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="full summary calls_used"):
        bestofn.estimate_from_full(paired, PROBLEM, 12)


def test_fallback_cli_dispatch_and_resume_keep_frozen_target(tmp_path):
    from victorique import cli
    paired = reference(tmp_path, missing=(4,))
    other = reference(tmp_path, run_id="other")
    pool = reference_list(tmp_path, [(other, "Advanced")])
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    out = tmp_path / "run"
    adapter = Scripted("transport_unknown")
    argv = ["victorique", "solve", str(problem), "--policy", "bestofn", "--full-calls", str(paired),
            "--full-references", str(pool), "--calls", "5", "--transport", "fake", "--out", str(out)]
    with patch("sys.argv", argv), patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        cli.main()
        result = api.status(out)
        assert result["outcome"] == "pending" and result["calls_used"] == 3
        assert result["bestofn"]["estimate"]["target_fallback"] == "same_problem_median"
        frozen = (out / "state/run-config.json").read_bytes()
        pool.write_text("Changed after creation")
        other.write_text("Changed after creation")
        resumed = api.resume(out)
    assert resumed["bestofn"]["estimate"] == result["bestofn"]["estimate"]
    assert resumed["calls_limit"] == 5 and resumed["deadline_utc"] == result["deadline_utc"]
    assert len(adapter.packets) == 3 and (out / "state/run-config.json").read_bytes() == frozen


def test_old_frozen_estimate_remains_valid(tmp_path):
    estimate = bestofn.estimate_from_full(reference(tmp_path), PROBLEM, 12)
    for key in ("target_fallback", "target_source_run_ids", "difficulty", "unexecuted_reason", "role_sources"):
        del estimate[key]
    assert bestofn.validate_estimate(estimate, 12) == estimate


@pytest.mark.parametrize("missing_roles", [False, True])
def test_legacy_frozen_estimate_keeps_missing_cost_behavior_on_resume(tmp_path, missing_roles):
    paired = reference(tmp_path, [("planner", 1000)] if missing_roles else None)
    estimate = bestofn.estimate_from_full(paired, PROBLEM, 12)
    del estimate["role_sources"]
    if missing_roles:
        estimate["unexecuted_reason"] = "missing_role_estimates"
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    out = tmp_path / "run"
    with patch.object(api, "estimate_from_full", return_value=estimate), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=paired, calls=12, transport="fake", out=out)
        before = (out / "state/run-config.json").read_bytes()
        with patch.object(FakeTransport, "start_once", side_effect=AssertionError("old request resent")):
            resumed = api.resume(out)
    assert result["outcome"] == resumed["outcome"] == ("unexecuted" if missing_roles else "pending")
    assert result["calls_used"] == resumed["calls_used"] == (0 if missing_roles else 2)
    assert result["bestofn"]["estimate"] == resumed["bestofn"]["estimate"] == estimate
    assert (out / "state/run-config.json").read_bytes() == before


def test_deadline_stops_before_new_dispatch(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    adapter = Scripted()
    with redirect_stdout(io.StringIO()):
        _, loop = api._create_run(problem, policy="bestofn", full_calls=reference(tmp_path), transport="fake",
            out=tmp_path / "run", _adapter=adapter, _started_at=datetime.now(timezone.utc) - timedelta(hours=3))
        result = api._run(tmp_path / "run", loop)
    assert result["calls_used"] == 0 and result["outcome"] == "abstain" and not adapter.packets


def test_ratio_overrun_and_missing_are_explicit(tmp_path):
    _, out, _ = run(tmp_path, calls=5)
    _, loop = api._open(out)
    snapshot = loop.core.snapshot()
    report = loop.diagnostics(snapshot, [{"request_id": "bestofn.solve.0", "tokens": {"input_tokens_including_cache": 600, "output_tokens": 100}}])["bestofn"]
    assert report["actual_tokens"] == 700 and report["token_overrun"] == 200
    assert report["actual_to_full_ratio"] == 700 / 500 and report["token_alignment_tolerance"] is None
    report = loop.diagnostics(snapshot, [{"request_id": "bestofn.solve.0", "tokens": None}])["bestofn"]
    assert report["actual_tokens"] is report["token_overrun"] is report["actual_to_full_ratio"] is None


def test_cli_matches_api_and_default_is_single(tmp_path):
    from victorique import cli
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    path = reference(tmp_path)
    out = tmp_path / "cli"
    argv = ["victorique", "solve", str(problem), "--policy", "bestofn", "--full-calls", str(path),
            "--calls", "5", "--transport", "fake", "--out", str(out)]
    with patch("sys.argv", argv), redirect_stdout(io.StringIO()):
        cli.main()
    assert api.status(out)["policy"] == "bestofn"
    with patch("sys.argv", ["victorique", "solve", str(problem)]), patch.object(api, "solve", return_value={}) as solve, redirect_stdout(io.StringIO()):
        cli.main()
    assert solve.call_args.kwargs["policy"] == "single"
    assert solve.call_args.kwargs["repair_rounds"] == 2


def test_next_generation_uses_observed_mean_and_includes_selection_and_gates():
    loop = bestofn.BestofnLoop()
    loop.config = {"bestofn": {"target_tokens": 500, "role_mean_tokens": {
        "solver": 100, "critic": 50, "alignment_reviewer": 50, "proof_reviewer": 50}}}
    base = {"manifest": {"limits": {"global_call_limit": 20}}}
    rows = [{"request_id": f"bestofn.solve.{i}", "tokens": {
        "input_tokens_including_cache": 30, "output_tokens": 10}} for i in range(4)]
    # 160 observed + 40 predicted + 4*50 selections + 100 gates = 500.
    # Using the full solver mean (100) instead would stop prematurely.
    assert loop._generation_stop(base, 4, rows) is None
    rows.append({"request_id": "bestofn.solve.4", "tokens": {"input_tokens_including_cache": 30, "output_tokens": 10}})
    assert loop._generation_stop(base, 5, rows) == "token_target"
    rows[0]["tokens"] = None
    assert loop._generation_stop(base, 5, rows) == "missing_usage"
    base["manifest"]["limits"]["global_call_limit"] = 11
    assert loop._generation_stop(base, 5, rows) == "call_budget"


def test_minimum_two_is_retained_when_estimated_minimum_exceeds_target(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    path = reference(tmp_path, [("solver", 100), ("critic", 20), ("alignment_reviewer", 20), ("proof_reviewer", 20)])
    with redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy="bestofn", full_calls=path, transport="fake", calls=5, out=tmp_path / "run")
    assert result["bestofn"]["n"] == 2 and result["calls_used"] == 5
    assert result["bestofn"]["minimum_workflow_estimate_over_target"]
    assert result["bestofn"]["minimum_workflow_estimated_tokens"] == 260


def test_optional_native_action_is_disabled_by_default(tmp_path):
    _, out, _ = run(tmp_path, calls=5)
    _, loop = api._open(out)
    base = loop.core.snapshot()["exchange"]
    record = next(row["record"] for row in base["requests"].values() if row["record"]["kind"] == "compare_candidates")
    manifest = deepcopy(base["manifest"])
    del manifest["limits"]["action_caps"]["compare_candidates"]
    del manifest["limits"]["action_roles"]["compare_candidates"]
    c.validate_limits(manifest["limits"])
    with pytest.raises(ValueError, match="action role differs"):
        exchange.validate_request(record, manifest)


@pytest.mark.parametrize("policy", ["ladder", "resample"])
@pytest.mark.parametrize("rounds", [3, 12, 100])
def test_public_api_and_cli_accept_explicit_round_limit(tmp_path, policy, rounds):
    from victorique import cli
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    out = tmp_path / "run"
    with redirect_stdout(io.StringIO()):
        _, loop = api._create_run(problem, policy=policy, repair_rounds=rounds, calls=12,
                                  transport="fake", out=out)
    assert loop.options["repair_rounds"] == rounds
    _, reopened = api._open(out)
    assert reopened.options["repair_rounds"] == rounds
    assert reopened.config["manifest"]["limits"]["global_call_limit"] == 12
    assert reopened.config["manifest"]["limits"]["stop_condition"] == loop.config["manifest"]["limits"]["stop_condition"]
    argv = ["victorique", "solve", str(problem), "--policy", policy, "--repair-rounds", str(rounds), "--calls", "12"]
    with patch("sys.argv", argv), patch.object(api, "solve", return_value={}) as solve, redirect_stdout(io.StringIO()):
        cli.main()
    assert solve.call_args.kwargs["repair_rounds"] == rounds
    assert solve.call_args.kwargs["calls"] == 12
    with pytest.raises(ValueError, match="repair_rounds"):
        api.solve(problem, policy=policy, repair_rounds=101, transport="fake", out=tmp_path / "invalid")
