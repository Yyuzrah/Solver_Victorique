from datetime import datetime, timedelta, timezone
import json
from unittest import mock

import pytest

from victorique import api
from victorique.fake import FakeTransport
from victorique.harness_bridge import AdaptiveHarnessBridge
from victorique.single import SingleLoop


def problem_file(tmp_path):
    path = tmp_path / "problem.md"
    path.write_text("Prove that n + 0 = n for every integer n.", encoding="utf-8")
    return path


@pytest.mark.parametrize("verdict", ["fail", "unknown", "invalid"])
@pytest.mark.parametrize("gate, rid", [("alignment_review", "single.alignment"), ("whole_proof_review", "single.review")])
def test_review_must_pass(tmp_path, verdict, gate, rid):
    original = FakeTransport._response

    def response(self, request):
        value = json.loads(original(self, request))
        if request.packet["binding"]["action_kind"] == gate:
            output = value["output"]
            if verdict == "invalid":
                output["target_ref"]["revision"] += 1
            else:
                output["outcome"] = verdict
                if verdict == "unknown":
                    output["uncertainties"] = ["Unresolved argument"]
                else:
                    output["findings"] = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                                           "target_ref": output["target_ref"], "kind": "invalid_step",
                                           "severity": "material", "location": None, "claim": "Unproved step"}]
        return json.dumps(value).encode()

    with mock.patch.object(FakeTransport, "_response", response):
        summary = api.solve(problem_file(tmp_path), policy="single", calls=3, transport="fake", out=tmp_path / "run")
    assert summary["outcome"] == "abstain"
    assert summary["calls_used"] == 3
    assert api.resume(tmp_path / "run") == summary
    _, loop = api._open(tmp_path / "run")
    base = loop.core.snapshot()["exchange"]
    assert base["requests"][rid]["phase"] == ("returned_invalid" if verdict == "invalid" else "returned_valid")


def test_three_calls_share_templates_and_candidate_binding(tmp_path):
    api.solve(problem_file(tmp_path), policy="single", calls=3, transport="fake", out=tmp_path / "run")
    _, loop = api._open(tmp_path / "run")
    requests = loop.core.snapshot()["exchange"]["requests"]
    assert list(requests) == ["single.solve", "single.alignment", "single.review"]
    solver, alignment, review = (row["packet"] for row in requests.values())
    assert "entire original problem" in solver["context"]["plan"]["routes"][0]["obligations"][0]["statement"]
    assert solver["context"]["problem"]["content"] == problem_file(tmp_path).read_text()
    assert solver["template"]["template_id"] == "victorique.adaptive_role.solve_obligation"
    assert review["template"]["template_id"] == "victorique.adaptive_role.whole_proof_review"
    assert alignment["template"]["template_id"] == "victorique.adaptive_role.alignment_review"
    assert alignment["binding"]["target_ref"] == review["binding"]["target_ref"]
    assert alignment["context"] == review["context"]
    assert review["context"]["plan"] is None
    assert review["context"]["released_feedback"] == []
    assert review["binding"]["target_ref"]["revision"] == 1


@pytest.mark.parametrize("checkpoint", ["transport_fenced", "transport_returned_before_archive"])
def test_unknown_resume_never_starts_again_or_adds_budget(tmp_path, checkpoint):
    def interrupt(self, stage, request_id):
        if stage == checkpoint:
            raise KeyboardInterrupt

    with mock.patch.object(AdaptiveHarnessBridge, "_checkpoint", interrupt):
        summary = api.solve(problem_file(tmp_path), policy="single", calls=3, hours=1,
                            transport="fake", out=tmp_path / "run")
    assert summary["calls_used"] == 1
    assert summary["outcome"] == "pending"
    with mock.patch.object(FakeTransport, "start_once", side_effect=AssertionError("must not restart")), \
         mock.patch.object(FakeTransport, "observe_existing", return_value=None) as observe:
        for _ in range(2):
            resumed = api.resume(tmp_path / "run")
            assert resumed["calls_used"] == 1
            assert resumed["calls_limit"] == 3
            assert resumed["deadline_utc"] == summary["deadline_utc"]
            assert resumed["outcome"] == "pending"
        assert observe.call_count == 2
    assert api.status(tmp_path / "run")["calls_used"] == 1


def test_resume_recovers_original_then_runs_only_reviews(tmp_path):
    original = SingleLoop.step

    def pause_after_solver(self):
        base = self.core.snapshot()["exchange"]
        if base["requests"].get("single.solve", {}).get("phase") == "returned_valid":
            raise KeyboardInterrupt
        return original(self)

    with mock.patch.object(SingleLoop, "step", pause_after_solver):
        pending = api.solve(problem_file(tmp_path), policy="single", calls=3, transport="fake", out=tmp_path / "run")
    assert pending["calls_used"] == 1
    start = FakeTransport.start_once
    kinds = []

    def record(self, request):
        kinds.append(request.packet["binding"]["action_kind"])
        return start(self, request)

    with mock.patch.object(FakeTransport, "start_once", record):
        summary = api.resume(tmp_path / "run")
    assert sorted(kinds) == ["alignment_review", "whole_proof_review"]
    assert summary["outcome"] == "complete"
    assert summary["calls_used"] == summary["calls_limit"] == 3
    assert summary["deadline_utc"] == pending["deadline_utc"]


def test_expired_resume_abstains_without_starting(tmp_path):
    with mock.patch.object(api, "_run", return_value=None):
        api.solve(problem_file(tmp_path), policy="single", calls=3, transport="fake", out=tmp_path / "run")
    original_deadline = api.status(tmp_path / "run")["deadline_utc"]
    after = datetime.now(timezone.utc) + timedelta(hours=3)
    with mock.patch("victorique.single.datetime") as clock, \
         mock.patch.object(FakeTransport, "start_once", side_effect=AssertionError("deadline expired")):
        clock.now.return_value = after
        summary = api.resume(tmp_path / "run")
    assert summary["outcome"] == "abstain"
    assert summary["calls_used"] == 0
    assert summary["calls_limit"] == 3
    assert summary["deadline_utc"] == original_deadline
    assert api.export(tmp_path / "run") == summary


def test_two_call_budget_abstains_without_extending_it(tmp_path):
    summary = api.solve(problem_file(tmp_path), policy="single", calls=2, transport="fake", out=tmp_path / "run")
    assert summary["outcome"] == "abstain"
    assert summary["calls_used"] == summary["calls_limit"] == 2
    assert api.resume(tmp_path / "run") == summary
