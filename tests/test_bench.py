import argparse
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest

from victorique.bench import add_parser, bench
from victorique.grading import FakeGrader, RUBRIC, answer_match, extract_answer


def problem(root, name, **metadata):
    root.mkdir(exist_ok=True)
    path = root / (name + ".md")
    path.write_text("Prove that n + 0 = n for every integer n.", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(metadata), encoding="utf-8")
    return path


def delivered(path, *, out, **kwargs):
    out.mkdir()
    (out / "proof.md").write_text("A candidate proof.\nFinal answer: 42\n", encoding="utf-8")
    return {"outcome": "complete", "calls_used": 3, "tokens": {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 8}}


@pytest.mark.parametrize("status", ["completed", "failed", "unknown", "timeout"])
def test_codex_grader_keeps_answer_metric_and_separate_costs(monkeypatch, tmp_path, status):
    problem(tmp_path / "problems", "a", reference_solution="reference", reference_answer="42")
    seen = []
    def codex_grade(request, **kwargs):
        seen.append((request, kwargs))
        return {"status": status, "score": 2 if status == "completed" else None,
                "reason": "fixture", "tokens": {"input_tokens": 5, "output_tokens": 3},
                "model": "fixture-model", "calls_used": 1}
    monkeypatch.setattr("victorique.harness.grade.grade_codex", codex_grade)
    result = bench(tmp_path / "problems", out=tmp_path / "out", grader="codex", transport="codex",
                   solve_fn=delivered, model="fixture-model", grading_seconds=30)
    row = result["rows"][0]
    assert len(seen) == 1 and seen[0][1]["seconds"] == 30
    assert seen[0][1]["directory"] == tmp_path / "out" / "a" / "grading-primary"
    assert seen[0][1]["model"] is None  # Solver model is not inherited by the grader.
    assert row["grading_status"] == status and row["grading_model"] == "fixture-model"
    assert row["proof_score"] == (2 if status == "completed" else None)
    assert row["classification"] == "correct_delivery" and row["answer_match"] == "match"
    assert result["totals"]["calls_used"]["total"] == 3
    assert result["totals"]["grading_calls"]["total"] == 1
    assert result["totals"]["grading_tokens"]["total"] == 8
    assert result["totals"]["tokens"]["total"] == 12


def test_codex_grader_cli_and_concurrency_limit(tmp_path):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["bench", "problems", "--grader", "codex", "--grading-seconds", "45"])
    assert args.grader == "codex" and args.grading_seconds == 45
    with pytest.raises(ValueError, match="at most 2 jobs"):
        bench(tmp_path, grader="codex", policy="ladder", jobs=3)


def test_fake_cli_and_registration(tmp_path):
    problem(tmp_path / "problems", "toy", reference_solution="By the additive identity.")
    output = tmp_path / "output"
    result = subprocess.run([sys.executable, "-m", "victorique.bench", str(tmp_path / "problems"),
                             "--policy", "single", "--calls", "3", "--jobs", "2", "--transport", "fake",
                             "--grader", "fake", "--out", str(output)], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = (output / "BENCH.md").read_text(encoding="utf-8")
    data = json.loads((output / "bench.json").read_text())
    assert data["synthetic"] is True
    assert data["rows"][0]["grading_calls"] == 1
    assert data["rows"][0]["calls_used"] <= 3
    assert data["counts"]["correct_delivery"] == 1
    assert data["totals"]["tokens"]["total"] is None
    assert "synthetic: true" in report and "missing rows: 1" in report
    assert (output / "toy" / "proof.md").exists()
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["bench", "problems", "--policy", "ladder"]).policy == "ladder"
    assert parser.parse_args(["bench", "problems", "--policy", "full"]).policy == "full"


@pytest.mark.parametrize("entry", ["python", "module", "cli"])
def test_bench_defaults_to_single_without_investigation_or_escalation(tmp_path, monkeypatch, entry):
    from victorique import cli
    from victorique.bench import main
    problem(tmp_path / "problems", "toy")
    output = tmp_path / "output"
    if entry == "python":
        result = bench(tmp_path / "problems", transport="fake", out=output)
    else:
        options = [str(tmp_path / "problems"), "--transport", "fake", "--out", str(output)]
        monkeypatch.setattr(sys, "argv", ["victorique.bench", *options] if entry == "module" else ["victorique", "bench", *options])
        (main if entry == "module" else cli.main)()
        result = json.loads((output / "bench.json").read_text())
    assert result["policy"] == "single"
    assert result["rows"][0]["calls_used"] == 3
    options = json.loads((output / "toy/invocation.json").read_text())["parameters"]
    assert options["policy"] == "single" and options["repair_rounds"] == 2
    assert options["investigate"] is options["escalate"] is False


def test_parallel_jobs_isolated_budgets_and_references(tmp_path):
    root = tmp_path / "problems"
    for name in ("a", "b"):
        problem(root, name, reference_solution="secret reference", reference_answer="42")
    barrier = Barrier(2)
    seen = []
    def solve_fn(path, *, out, **kwargs):
        assert kwargs["calls"] == 7 and kwargs["hours"] == .25
        assert "secret reference" not in path.read_text()
        assert "reference_solution" not in kwargs
        barrier.wait(timeout=5)
        return delivered(path, out=out)
    def grader(request):
        assert request["reference_solution"] == "secret reference"
        assert request["rubric"] == RUBRIC
        assert set(request) == {"problem", "candidate", "reference_solution", "guidelines", "rubric", "instructions"}
        seen.append(request)
        return {"score": 2, "reason": "Substantive gap", "tokens": {
            "input_tokens": 5, "input_tokens_including_cache": 5, "output_tokens": 1}}
    result = bench(root, out=tmp_path / "out", calls=7, hours=.25, jobs=2, solve_fn=solve_fn, grader=grader)
    assert len(seen) == 2
    assert result["counts"]["correct_delivery"] == 2  # answer metric, separate proof quality
    assert [row["proof_score"] for row in result["rows"]] == [2, 2]
    assert result["totals"]["calls_used"]["total"] == 6
    assert result["totals"]["grading_calls"]["total"] == 2
    assert result["totals"]["tokens"]["total"] == 24  # cached tokens are a subset
    assert result["totals"]["grading_tokens"]["total"] == 12
    assert all((tmp_path / "out" / name / "proof.md").exists() for name in ("a", "b"))


def test_failures_timeouts_unknowns_and_abstentions_remain(tmp_path):
    root = tmp_path / "problems"
    for name in ("good", "wrong", "abstain", "failed", "timeout", "pending", "partial", "ungraded"):
        problem(root, name, **({"reference_answer": "42" if name == "good" else "41"} if name in ("good", "wrong") else {}))
    def solve_fn(path, *, out, **kwargs):
        if path.stem == "failed":
            out.mkdir()
            (out / "summary.json").write_text(json.dumps({"calls_used": 1, "tokens": None}))
            raise RuntimeError("worker error")
        if path.stem == "timeout":
            raise TimeoutError("deadline")
        result = delivered(path, out=out)
        if path.stem in ("abstain", "pending", "partial"):
            result["outcome"] = path.stem
        return result
    result = bench(root, out=tmp_path / "out", jobs=2, solve_fn=solve_fn)
    assert result["counts"] == {"correct_delivery": 1, "incorrect_delivery": 1, "ungraded_delivery": 1,
                                "abstain": 1, "failed": 1, "timeout": 1, "unknown": 2}
    assert len(result["rows"]) == 8
    assert result["totals"]["calls_used"] == {"total": None, "known_subtotal": 19, "missing": 1}
    assert result["totals"]["grading_tokens"]["total"] == 0
    assert "worker error" in (tmp_path / "out" / "BENCH.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("score, classification", [(7, "correct_delivery"), (6, "incorrect_delivery"), (0, "incorrect_delivery")])
def test_proof_only_threshold(tmp_path, score, classification):
    problem(tmp_path / "problems", "a", reference_solution="reference")
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=delivered,
                   grader=lambda request: {"score": score, "reason": "fixture"})
    assert result["rows"][0]["classification"] == classification
    assert result["rows"][0]["answer_match"] == "not_applicable"


@pytest.mark.parametrize("bad", [{"score": True, "reason": "bad"}, {"score": 8, "reason": "bad"}, {"score": 7}])
def test_bad_grading_keeps_delivered_proof_unknown(tmp_path, bad):
    problem(tmp_path / "problems", "a", reference_solution="reference")
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=delivered, grader=lambda request: bad)
    row = result["rows"][0]
    assert row["outcome"] == "complete" and row["classification"] == "ungraded_delivery"
    assert row["grading_status"] == "failed" and row["grading_calls"] == 1
    assert row["proof_score"] is None


def test_missing_reference_and_grading_timeout(tmp_path):
    root = tmp_path / "problems"
    problem(root, "a")
    problem(root, "b", reference_solution="reference")
    requests = []
    def grader(request):
        requests.append(request)
        raise TimeoutError("grader timeout")
    result = bench(root, out=tmp_path / "out", solve_fn=delivered, grader=grader)
    assert len(requests) == 1
    assert [r["grading_status"] for r in result["rows"]] == ["missing_reference", "timeout"]
    assert result["counts"]["ungraded_delivery"] == 2
    assert result["counts"]["timeout"] == 0  # solving completed; grading is separate


def test_answer_extraction_does_not_match_incidental_reference():
    assert answer_match("We tried 42 and failed.", "42") == "unknown"
    assert answer_match("Final answer: $42$", "42") == "match"
    assert answer_match("Final answer: 43", "42") == "mismatch"
    assert answer_match("Final answer: 2/4", "1/2", ["2/4"]) == "match"
    assert extract_answer(r"\boxed{\frac{1}{2}}") == r"\frac{1}{2}"


@pytest.mark.parametrize("kwargs", [{"jobs": 0}, {"jobs": 5}, {"calls": 1}, {"hours": float('nan')},
                                    {"transport": "wrong"}, {"grader": FakeGrader()}])
def test_invalid_options_start_no_solve(tmp_path, kwargs):
    problem(tmp_path / "problems", "a")
    def never(*args, **kwargs):
        pytest.fail("solve must not be called")
    with pytest.raises(ValueError):
        bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=never, **kwargs)
    assert not (tmp_path / "out").exists()


def test_output_collision_refused(tmp_path):
    problem(tmp_path / "problems", "a")
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        bench(tmp_path / "problems", out=output)


@pytest.mark.parametrize("enabled", [False, True])
def test_abstained_native_draft_grading_is_opt_in_and_stays_abstained(tmp_path, enabled):
    problem(tmp_path / "problems", "a", reference_solution="Synthetic reference.")
    result = bench(tmp_path / "problems", out=tmp_path / "out", transport="fake",
                   policy="single", calls=2, grader="fake", grade_abstentions=enabled)
    row = result["rows"][0]
    assert row["outcome"] == "abstain" and row["classification"] == "abstain"
    assert result["counts"]["correct_delivery"] == 0 and result["counts"]["abstain"] == 1
    assert row["grading_calls"] == int(enabled)
    assert row["proof_score"] == (7 if enabled else None)
    assert result["grade_abstentions"] is enabled
    if enabled:
        request = json.loads((tmp_path / "out/a/grading-primary/request.json").read_text())
        assert request["candidate_ref"] == row["grades"]["primary"]["candidate_ref"]
        assert request["candidate_ref"]["revision"] == 1
        assert row["fully_correct"] is True


@pytest.mark.parametrize("outcome", ["abstain", "pending", "failed", "timeout"])
def test_no_frozen_draft_or_live_attempt_is_not_graded(tmp_path, outcome):
    problem(tmp_path / "problems", "a", reference_solution="reference")
    def solve_fn(path, *, out, **kwargs):
        summary = delivered(path, out=out)
        summary["outcome"] = outcome
        return summary  # No native endpoint candidate, even though a text file exists.
    def never(request):
        pytest.fail("an unbound or live draft must not be graded")
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn,
                   grader=never, grade_abstentions=True)
    row = result["rows"][0]
    assert row["outcome"] == outcome and row["grading_calls"] == 0
    assert row["grading_status"] == ("missing_candidate" if outcome == "abstain" else "not_requested")


def test_abstained_score_and_answer_do_not_become_delivery(monkeypatch, tmp_path):
    problem(tmp_path / "problems", "a", reference_solution="reference", reference_answer="42")
    binding = {"artifact_id": "draft", "revision": 2, "plan_id": "plan", "plan_revision": 1,
               "route_id": "route", "obligation_id": "whole"}
    monkeypatch.setattr("victorique.bench._candidate_ref", lambda directory: binding)
    def solve_fn(path, *, out, **kwargs):
        return delivered(path, out=out) | {"outcome": "abstain"}
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn,
                   transport="fake", grader="fake", same_family_secondary=True, grade_abstentions=True)
    row = result["rows"][0]
    assert row["classification"] == "abstain" and row["answer_match"] == "match"
    assert row["proof_score"] == 7 and row["grading_calls"] == 2
    assert result["grading_calls_limit_per_candidate"] == 2
    for role in ("primary", "secondary"):
        assert row["grades"][role]["candidate_ref"] == binding


def test_abstained_grading_failure_stays_separate(monkeypatch, tmp_path):
    problem(tmp_path / "problems", "a", reference_solution="reference")
    monkeypatch.setattr("victorique.bench._candidate_ref", lambda directory: {"revision": 1})
    def solve_fn(path, *, out, **kwargs):
        return delivered(path, out=out) | {"outcome": "abstain"}
    def grader(request):
        raise TimeoutError("fixture grader timeout")
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn,
                   grader=grader, grade_abstentions=True)
    row = result["rows"][0]
    assert row["classification"] == "abstain" and row["grading_status"] == "timeout"
    assert row["grading_calls"] == 1 and row["proof_score"] is None


def test_grade_abstentions_cli_and_validation(tmp_path):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["bench", "problems", "--grade-abstentions"]).grade_abstentions
    with pytest.raises(ValueError, match="primary grader"):
        bench(tmp_path, grade_abstentions=True)
    with pytest.raises(ValueError, match="boolean"):
        bench(tmp_path, grader="fake", grade_abstentions=1)


@pytest.mark.parametrize("grade_abstentions", [False, True])
def test_main_cli_bench(tmp_path, grade_abstentions):
    import subprocess
    import sys
    problem(tmp_path / "problems", "a", reference_solution="Synthetic reference.")
    output = tmp_path / "out"
    command = [sys.executable, "-m", "victorique", "bench", str(tmp_path / "problems"),
               "--transport", "fake", "--policy", "single", "--calls", "2" if grade_abstentions else "3",
               "--out", str(output)]
    if grade_abstentions:
        command.extend(["--grader", "fake", "--grade-abstentions"])
    result = subprocess.run(command, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "bench.json").read_text())
    assert summary["totals"]["calls_used"]["total"] == (2 if grade_abstentions else 3)
    assert summary["counts"]["abstain" if grade_abstentions else "ungraded_delivery"] == 1
    assert summary["totals"]["grading_calls"]["total"] == int(grade_abstentions)
    assert (output / "BENCH.md").is_file()


@pytest.mark.parametrize("raises", [False, True])
def test_call_usage_keeps_planner_cost_when_solver_times_out(tmp_path, raises):
    problem(tmp_path / "problems", "a")
    def solve_fn(path, *, out, **kwargs):
        out.mkdir()
        calls = [
            {"role": "planner", "transport": "codex_cli", "status": "completed",
             "tokens": {"input_tokens": 32000, "input_tokens_including_cache": 32000,
                        "cached_input_tokens": 30000, "output_tokens": 1979, "reasoning_output_tokens": 1000}},
            {"role": "solver", "transport": "codex_cli", "status": "timed_out", "tokens": None},
        ]
        (out / "calls.jsonl").write_text("".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8")
        # Unrelated nested files must never enter this run's accounting.
        (out / "history").mkdir()
        (out / "history/calls.jsonl").write_text(json.dumps({"tokens": 999999}) + "\n")
        summary = {"outcome": "abstain", "calls_used": 2,
                   "tokens": {"input_tokens": None, "output_tokens": None, "input_tokens_including_cache": None}}
        (out / "summary.json").write_text(json.dumps(summary))
        if raises:
            raise TimeoutError("solver timeout")
        return summary
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn)
    assert result["totals"]["tokens"] == {
        "total": None, "known_subtotal": 33979, "missing": 1, "missing_calls": 1}
    assert result["rows"][0]["usage"] == {
        "total": None, "known_subtotal": 33979, "calls": 2, "missing_calls": 1, "source": "calls.jsonl"}
    assert result["rows"][0]["tokens"]["input_tokens"] is None
    report = (tmp_path / "out/BENCH.md").read_text(encoding="utf-8")
    assert "known subtotal: 33979; missing rows: 1; missing calls: 1" in report


@pytest.mark.parametrize("transport, tokens, total, subtotal, missing", [
    ("codex", {"input_tokens": 0, "output_tokens": 0}, 0, 0, 0),
    ("codex", None, None, 0, 1),
    ("codex", {"input_tokens": 10, "output_tokens": None, "cached_input_tokens": 8}, None, 10, 1),
    ("codex", {"input_tokens": None, "output_tokens": 3, "reasoning_output_tokens": 2}, None, 3, 1),
    ("codex", {"input_tokens": 10, "output_tokens": 3, "cached_input_tokens": 8,
               "reasoning_output_tokens": 2}, 13, 13, 0),
    ("claude", {"input_tokens": 10, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5, "output_tokens": 3}, 38, 38, 0),
    ("claude", {"input_tokens": 10, "cached_input_tokens": 20, "cache_creation_input_tokens": None,
                "input_tokens_including_cache": None, "output_tokens": 3}, None, 33, 1),
    ("claude", {"input_tokens": 10, "cached_input_tokens": 20, "cache_creation_input_tokens": 5,
                "input_tokens_including_cache": 35, "output_tokens": 3, "reasoning_output_tokens": 2}, 38, 38, 0),
    ("codex", {"input_tokens": True, "output_tokens": -1}, None, 0, 1),
])
def test_call_usage_partial_counters_and_cache_semantics(tmp_path, transport, tokens, total, subtotal, missing):
    problem(tmp_path / "problems", "a")
    def solve_fn(path, *, out, **kwargs):
        out.mkdir()
        (out / "calls.jsonl").write_text(json.dumps({"tokens": tokens, "transport": transport + "_cli"}) + "\n")
        return {"outcome": "abstain", "calls_used": 1, "tokens": tokens}
    result = bench(tmp_path / "problems", out=tmp_path / "out", transport=transport, solve_fn=solve_fn)
    assert result["totals"]["tokens"] == {
        "total": total, "known_subtotal": subtotal, "missing": int(total is None), "missing_calls": missing}


@pytest.mark.parametrize("unknown_role", ["primary", "secondary"])
def test_grading_known_cost_survives_other_grader_missing_usage(monkeypatch, tmp_path, unknown_role):
    problem(tmp_path / "problems", "a", reference_solution="reference")
    def grade(request, **kwargs):
        missing = kwargs["directory"].name == "grading-" + unknown_role
        return {"status": "unknown" if missing else "completed", "score": None if missing else 7,
                "reason": "fixture", "tokens": None if missing else {"input_tokens": 10, "output_tokens": 3,
                "cached_input_tokens": 8, "reasoning_output_tokens": 2}, "calls_used": 1}
    monkeypatch.setattr("victorique.harness.grade.grade_codex", grade)
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=delivered,
                   grader="codex", same_family_secondary=True)
    assert result["rows"][0]["grading_tokens"] is None
    assert result["totals"]["grading_tokens"] == {
        "total": None, "known_subtotal": 13, "missing": 1, "missing_calls": 1}


@pytest.mark.parametrize("tokens,total,subtotal,missing_calls", [
    (12, 12, 12, 0),
    ({"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 8}, 12, 12, 0),
    ({"input_tokens": 10, "output_tokens": None}, None, 10, None),
    (None, None, 0, None),
])
def test_custom_solve_without_call_file_retains_summary_compatibility(tmp_path, tokens, total, subtotal, missing_calls):
    problem(tmp_path / "problems", "a")
    def solve_fn(path, *, out, **kwargs):
        return delivered(path, out=out) | {"tokens": tokens}
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn)
    assert result["rows"][0]["tokens"] == tokens
    assert result["rows"][0]["usage"]["source"] == "summary"
    assert result["totals"]["tokens"] == {
        "total": total, "known_subtotal": subtotal, "missing": int(total is None), "missing_calls": missing_calls}


def test_incomplete_call_projection_does_not_imply_complete_usage(tmp_path):
    problem(tmp_path / "problems", "a")
    def solve_fn(path, *, out, **kwargs):
        out.mkdir()
        (out / "calls.jsonl").write_text('{"tokens": 5}\ninvalid-json\n')
        return {"outcome": "abstain", "calls_used": 3, "tokens": None}
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn)
    assert result["totals"]["tokens"] == {
        "total": None, "known_subtotal": 5, "missing": 1, "missing_calls": 2}


@pytest.mark.parametrize("count", [None, 0])
def test_known_call_prefix_without_matching_summary_count_is_incomplete(tmp_path, count):
    problem(tmp_path / "problems", "a")
    def solve_fn(path, *, out, **kwargs):
        out.mkdir()
        (out / "calls.jsonl").write_text('{"tokens": 33979}\n')
        if count is None:
            # Interruption before the public summary could be exported.
            raise TimeoutError("summary not written")
        return {"outcome": "abstain", "calls_used": count, "tokens": None}
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=solve_fn)
    assert result["totals"]["tokens"] == {
        "total": None, "known_subtotal": 33979, "missing": 1, "missing_calls": None}
    assert result["rows"][0]["usage"]["coverage"] == "unconfirmed"


def test_callable_grade_with_unknown_cache_semantics_keeps_lower_bound(tmp_path):
    problem(tmp_path / "problems", "a", reference_solution="reference")
    result = bench(tmp_path / "problems", out=tmp_path / "out", solve_fn=delivered,
                   grader=lambda request: {"score": 7, "reason": "fixture", "tokens": {
                       "input_tokens": 10, "output_tokens": 3, "cached_input_tokens": 8}})
    assert result["totals"]["grading_tokens"] == {
        "total": None, "known_subtotal": 13, "missing": 1, "missing_calls": 1}
