"""Cross-family grades run through both durable offline CLI executables."""
import argparse
import json

import pytest

from victorique import solve
from victorique.bench import add_parser, bench
from victorique.grading import GRADE_SCHEMA
from victorique.harness import grade
from victorique.harness.claude_transport import ClaudeFixtureTransport
from victorique.harness.codex_contract import DEFAULT_MODEL
from victorique.harness.codex_transport import CodexFixtureTransport
from tests.test_bench import problem
from tests.test_grade import finish, request
from tests.test_claude_contract import public_profile as claude_profile
from tests.test_victorique_harness_codex_contract import public_profile as codex_profile


def offline(monkeypatch, *, scenarios=None, scores=None):
    scenarios, scores = scenarios or {}, scores or {}
    seen = []
    monkeypatch.delenv("VICTORIQUE_CODEX_BINARY", raising=False)
    monkeypatch.delenv("VICTORIQUE_CLAUDE_BINARY", raising=False)
    monkeypatch.setattr(grade, "inspect_host", lambda host: claude_profile() if host == "claude-code" else codex_profile())
    monkeypatch.setattr("victorique.harness.claude_contract.complete_claude_profile", lambda profile: profile)
    monkeypatch.setattr("victorique.api._mcp_names", lambda *args: [])
    monkeypatch.setattr(grade, "_cli_defaults", lambda *args: {"status": "observed", "model": "codex-default", "source": "cli_default_config"})

    def create(backend, root, profile, authorization, packet, seconds, workspace_root):
        seen.append((backend, packet, authorization))
        adapter = ClaudeFixtureTransport if backend == "claude" else CodexFixtureTransport
        return adapter.create(root, response_bytes=json.dumps({
            "schema": packet["response_contract"]["schema"], "binding": packet["binding"],
            "output": {"score": scores.get(backend, 7), "reason": "private-" + backend + "-assessment"}}).encode(),
            output_schemas={"reference_grade": GRADE_SCHEMA}, scenario=scenarios.get(backend, "success"),
            timeout_seconds=seconds)

    monkeypatch.setattr(grade, "_new_adapter", lambda *args: create("codex", *args))
    monkeypatch.setattr(grade, "_new_claude_adapter", lambda *args: create("claude", *args))
    return seen


def fake_solve(path, **kwargs):
    kwargs["transport"] = "fake"
    return solve(path, **kwargs)


@pytest.mark.parametrize("solver,primary", [("codex", "claude"), ("claude", "codex")])
def test_cross_family_two_durable_grades_bound_to_same_candidate(monkeypatch, tmp_path, solver, primary):
    seen = offline(monkeypatch, scores={"codex": 6, "claude": 7})
    problem(tmp_path / "problems", "toy", reference_solution="secret reference", grading_guidelines="private rubric")
    report = bench(tmp_path / "problems", out=tmp_path / "out", transport=solver, grader=primary,
                   policy="single", calls=3, model="solver-only-model", solve_fn=fake_solve,
                   same_family_secondary=True, grading_seconds=10)
    row = report["rows"][0]
    assert [s[0] for s in seen] == [primary, solver]
    assert report["grading_calls_limit_per_delivery"] == row["grading_calls"] == 2
    assert all(s[1]["binding"]["model_request"]["model_id"] == DEFAULT_MODEL for s in seen)
    assert seen[0][1]["binding"] != seen[1][1]["binding"]
    assert seen[0][1]["context"] == seen[1][1]["context"]
    assert set(seen[0][1]["context"]) == {"problem", "candidate", "reference_solution", "guidelines", "rubric", "instructions"}
    assert "private-" not in json.dumps(seen[1][1]["context"])
    assert row["grades"]["primary"]["candidate_ref"] == row["grades"]["secondary"]["candidate_ref"]
    assert row["grades"]["primary"]["candidate_ref"] is not None
    assert row["proof_score"] == (7 if primary == "claude" else 6)
    assert row["fully_correct"] == (primary == "claude")
    assert row["grades"]["secondary"]["fully_correct"] == (solver == "claude")
    # This Claude executable omits cache counts. Its total must remain unknown.
    assert report["totals"]["grading_tokens"]["total"] is None
    for role in ("primary", "secondary"):
        directory = tmp_path / "out" / "toy" / ("grading-" + role)
        saved = grade._read(directory / "request.json")
        assert saved["calls_limit"] == 1
        assert saved["packet"]["context"]["candidate"] == (directory.parent / "proof.md").read_text(encoding="utf-8")
        before = grade._read(directory / "result.json")
        monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("relaunch"))
        monkeypatch.setattr(ClaudeFixtureTransport, "start_once", lambda *args: pytest.fail("relaunch"))
        resumed = grade.resume_grade(directory)
        assert resumed["score"] == before["score"] and resumed["deadline_utc"] == before["deadline_utc"]
    # Solver packets do not receive reference material or grading instructions.
    from victorique.api import _open, _snapshot
    _, loop = _open(tmp_path / "out" / "toy")
    for value in _snapshot(loop)["exchange"]["requests"].values():
        assert "secret reference" not in json.dumps(value["packet"])
        assert "private rubric" not in json.dumps(value["packet"])


def test_explicit_models_route_independently_and_event_model_wins(monkeypatch, tmp_path):
    seen = offline(monkeypatch)
    problem(tmp_path / "problems", "toy", reference_solution="reference")
    result = bench(tmp_path / "problems", out=tmp_path / "out", policy="single", calls=3,
                   transport="codex", model="solver-model", grader="claude", grader_model="claude-requested",
                   same_family_secondary=True, secondary_grader_model="codex-requested", solve_fn=fake_solve)
    assert [s[1]["binding"]["model_request"]["model_id"] for s in seen] == ["claude-requested", "codex-requested"]
    primary, secondary = (result["rows"][0]["grades"][k] for k in ("primary", "secondary"))
    assert primary["model"] == "fixture-model" and primary["model_source"] == "cli_reported", json.dumps(primary)
    assert primary["tokens"]["input_tokens"] == 9 and primary["tokens"]["output_tokens"] == 4
    assert secondary["model"] == "codex-requested" and secondary["model_source"] == "request_config"


@pytest.mark.parametrize("backend,scenario,expected", [("claude", "failure", "failed"),
    ("claude", "malformed", "unknown"), ("codex", "no-final", "unknown"), ("codex", "nonzero", "failed")])
def test_both_backend_failures_keep_single_attempt(monkeypatch, tmp_path, backend, scenario, expected):
    offline(monkeypatch, scenarios={backend: scenario})
    directory = tmp_path / "grading"
    grade.grade_reference(request(), directory=directory, transport=backend, seconds=10,
                          candidate_ref={"candidate_id": "candidate", "version": 2})
    result = finish(directory)
    assert result["status"] == expected
    assert result["score"] is None and result["fully_correct"] is None and result["calls_used"] == 1
    monkeypatch.setattr(CodexFixtureTransport, "start_once", lambda *args: pytest.fail("relaunch"))
    monkeypatch.setattr(ClaudeFixtureTransport, "start_once", lambda *args: pytest.fail("relaunch"))
    assert grade.resume_grade(directory)["status"] == expected


def test_primary_unknown_does_not_hide_secondary_or_change_classification(monkeypatch, tmp_path):
    offline(monkeypatch, scenarios={"codex": "no-final"})
    problem(tmp_path / "problems", "toy", reference_solution="reference")
    result = bench(tmp_path / "problems", out=tmp_path / "out", policy="single", calls=3,
                   transport="claude", grader="codex", same_family_secondary=True, solve_fn=fake_solve)
    row = result["rows"][0]
    assert row["grades"]["primary"]["status"] == "unknown"
    assert row["grades"]["secondary"]["score"] == 7
    assert row["classification"] == "ungraded_delivery" and row["grading_calls"] == 2


def test_fake_primary_secondary_and_cli_defaults(tmp_path):
    problem(tmp_path / "problems", "toy", reference_solution="reference")
    result = bench(tmp_path / "problems", out=tmp_path / "out", policy="single", calls=3,
                   transport="fake", grader="fake", same_family_secondary=True)
    assert result["rows"][0]["grading_calls"] == 2
    assert all(g["synthetic"] for g in result["rows"][0]["grades"].values())
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    args = parser.parse_args(["bench", "problems", "--grader", "codex"])
    assert args.same_family_secondary is False and args.grader_model is None
    for kwargs in ({"transport": "claude", "jobs": 2}, {"grader": "claude", "jobs": 2},
                   {"same_family_secondary": True}, {"secondary_grader_model": "orphan"},
                   {"grader": "codex", "grader_model": "--bad"}):
        with pytest.raises(ValueError):
            bench(tmp_path / "problems", out=tmp_path / "invalid", **kwargs)
    assert not (tmp_path / "invalid").exists()


def test_resample_bench_cli_and_budget(tmp_path):
    from victorique.bench import run_args
    problem(tmp_path / "problems", "toy", reference_solution="reference")
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    args = parser.parse_args(["bench", str(tmp_path / "problems"), "--policy", "resample",
                              "--transport", "fake", "--grader", "fake", "--calls", "3",
                              "--out", str(tmp_path / "out")])
    result = run_args(args)
    assert result["policy"] == "resample" and result["rows"][0]["calls_used"] == 3
    assert result["rows"][0]["grading_calls"] == 1
    with pytest.raises(ValueError, match="at most 2 jobs"):
        bench(tmp_path / "problems", policy="resample", jobs=3)


def test_setup_interruption_recovers_without_dispatch(monkeypatch, tmp_path):
    offline(monkeypatch)
    def interrupted(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(grade, "_new_claude_adapter", interrupted)
    directory = tmp_path / "grade"
    with pytest.raises(KeyboardInterrupt):
        grade.grade_reference(request(), directory=directory, transport="claude", seconds=10)
    record = grade._read(directory / "request.json")
    result = grade.resume_grade(directory)
    assert result["status"] == "unknown" and result["calls_used"] == 0
    assert result["deadline_utc"] == record["deadline_utc"]


@pytest.mark.parametrize("usage,transport,expected", [
    ({"input_tokens": 10, "output_tokens": 3, "cached_input_tokens": 8}, "codex", 13),
    ({"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 20,
      "cache_creation_input_tokens": 5}, "claude", 38),
    ({"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 0,
      "cache_creation_input_tokens": 0}, "claude", 13),
    ({"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 20}, "claude", None),
    ({"input_tokens": 10, "output_tokens": 3, "cache_creation_input_tokens": 5}, "claude", None),
    ({"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": None,
      "cache_creation_input_tokens": 5}, "claude", None),
    ({"input_tokens": 10, "output_tokens": 3, "input_tokens_including_cache": 35}, "claude", 38),
    ({"input_tokens": 10, "output_tokens": 3, "input_tokens_including_cache": 35}, "callable", 38),
    ({"input_tokens": 10, "output_tokens": 3, "input_tokens_including_cache": None,
      "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}, "claude", None),
    ({"input_tokens": 10, "output_tokens": 3}, None, None),
    ({"input_tokens": 10, "output_tokens": 3}, "unrecognized", None),
    ({"input_tokens": 10, "output_tokens": True}, "codex", None),
    ({"input_tokens_including_cache": -1, "output_tokens": 3}, "claude", None),
])
def test_provider_cache_totals_preserve_unknowns(usage, transport, expected):
    from victorique.bench import _token_total
    assert _token_total(usage, transport) == expected


@pytest.mark.parametrize("solver,normalized,total", [("claude", 35, 38), ("claude", None, None),
                                                     ("codex", 10, 13)])
def test_bench_report_and_totals_use_normalized_input(tmp_path, solver, normalized, total):
    from tests.test_bench import delivered
    problem(tmp_path / "problems", "toy", reference_solution="reference", reference_answer="42")
    usage = {"input_tokens": 10, "output_tokens": 3, "input_tokens_including_cache": normalized}
    def solve_fixture(path, **kwargs):
        summary = delivered(path, **kwargs)
        summary["tokens"] = dict(usage)
        return summary
    def grader_fixture(request):
        return {"score": 6, "reason": "Fixture partial proof", "tokens": dict(usage)}
    report = bench(tmp_path / "problems", out=tmp_path / "out", transport=solver,
                   solve_fn=solve_fixture, grader=grader_fixture)
    assert report["totals"]["tokens"]["total"] == total
    assert report["totals"]["grading_tokens"]["total"] == total
    assert report["rows"][0]["classification"] == "correct_delivery"
    markdown = (tmp_path / "out" / "BENCH.md").read_text(encoding="utf-8")
    assert "Answer problems use answer matching" in markdown
    assert "classification uses the primary grade" not in markdown
    rendered = "unknown" if total is None else str(total)
    assert f"| 3 | 1 | {rendered} | {rendered} |" in markdown
