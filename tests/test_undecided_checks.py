"""Inconclusive and incomplete checks: abstain (default) or treat once like a fail."""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

import pytest

from victorique import api, cli
from victorique.fake import FakeTransport

PROBLEM = "Prove n + 0 = n for every integer n."
DOUBT = "Is the case n < 0 covered?"


class Undecided(FakeTransport):
    """The first whole-proof check is undecided; later ones pass unless ``always``."""

    def __init__(self, kind="inconclusive", always=False):
        self.kind, self.always = kind, always
        self.kinds, self.packets = [], []

    def _response(self, request):
        value = json.loads(super()._response(request))
        binding, output = value["binding"], value["output"]
        if binding["action_kind"] == "whole_proof_review" and (".L0." in binding["request_id"] or self.always):
            if self.kind == "incomplete":
                return b"invalid JSON"  # No valid report, also on the retry.
            if self.kind == "inconclusive":
                output.update(outcome="unknown", uncertainties=[DOUBT])
            if self.kind == "fail_minor":
                output.update(outcome="fail", findings=[{
                    "schema": "victorique.adaptive_defect.v1", "issue_id": "wording",
                    "target_ref": binding["target_ref"], "kind": "ambiguity", "severity": "minor",
                    "location": None, "claim": "The last sentence restates the claim."}])
        return json.dumps(value).encode()

    def start_once(self, request):
        self.kinds.append(request.packet["binding"]["action_kind"])
        self.packets.append(request.packet)
        return super().start_once(request)

    def observe_existing(self, request):
        return FakeTransport.start_once(self, request)


def run(tmp_path, adapter, policy="ladder", **options):
    tmp_path.mkdir(parents=True, exist_ok=True)
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    out = tmp_path / "run"
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy=policy, out=out, transport="fake", **options)
    return result, out


def repair_issues(adapter):
    packet = next(p for p in adapter.packets if p["binding"]["action_kind"] == "repair_obligation")
    return json.loads(packet["template"]["instructions"])["policy_instructions"]["issues"]


@pytest.mark.parametrize("policy", ["ladder", "resample"])
def test_default_abstains_on_an_inconclusive_check_and_names_it(tmp_path, policy):
    adapter = Undecided("inconclusive")
    result, out = run(tmp_path, adapter, policy=policy)
    assert result["outcome"] == "abstain" and result["calls_used"] == 3
    assert result["endpoint_reason"] == policy + "_check_inconclusive"
    assert result["inconclusive_checks"] == [{"request_id": policy + ".L0.review", "check": "whole_proof_review",
                                              "outcome": "unknown", "doubts": 1, "minor_findings": 0}]
    assert result["unresolved_questions"] == [DOUBT]
    assert adapter.kinds.count("solve_obligation") == 1 and "repair_obligation" not in adapter.kinds
    assert json.loads((out / "invocation.json").read_text())["parameters"]["undecided_checks"] == "abstain"


@pytest.mark.parametrize("policy", ["ladder", "resample"])
def test_default_abstains_on_an_incomplete_check_after_its_retry(tmp_path, policy):
    adapter = Undecided("incomplete")
    result, _ = run(tmp_path, adapter, policy=policy)
    assert result["outcome"] == "abstain" and result["calls_used"] == 4
    assert result["endpoint_reason"] == policy + "_check_incomplete"
    assert result["incomplete_checks"] == [{"request_id": policy + ".L0.review.retry", "status": "returned_invalid"}]
    assert adapter.kinds.count("whole_proof_review") == 2
    assert adapter.kinds.count("solve_obligation") == 1 and "repair_obligation" not in adapter.kinds


@pytest.mark.parametrize("kind,claim", [("inconclusive", "Open doubt from the whole-proof check: " + DOUBT),
                                        ("fail_minor", "The last sentence restates the claim."),
                                        ("incomplete", "The whole-proof check returned no valid report")])
def test_repair_mode_treats_an_undecided_check_like_a_fail(tmp_path, kind, claim):
    adapter = Undecided(kind)
    result, out = run(tmp_path, adapter, undecided_checks="repair")
    assert result["outcome"] == "complete" and result["endpoint_reason"] == "ladder_complete"
    retry = 1 if kind == "incomplete" else 0
    assert result["calls_used"] == 6 + retry
    assert adapter.kinds.count("repair_obligation") == 1
    issues = repair_issues(adapter)
    assert len(issues) == 1 and issues[0]["claim"].startswith(claim)
    # The failed check runs first on the repaired version, then the other check.
    assert adapter.kinds[-3:] == ["repair_obligation", "whole_proof_review", "alignment_review"]
    _, loop = api._open(out)
    assert loop.core.snapshot()["endpoint"]["root_candidate"]["revision"] == 2
    assert json.loads((out / "invocation.json").read_text())["parameters"]["undecided_checks"] == "repair"
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        assert api.resume(out) == result


@pytest.mark.parametrize("kind,reason,calls", [("inconclusive", "ladder_check_inconclusive", 5),
                                               ("incomplete", "ladder_check_incomplete", 7)])
def test_repair_mode_repairs_only_once(tmp_path, kind, reason, calls):
    adapter = Undecided(kind, always=True)
    result, _ = run(tmp_path, adapter, undecided_checks="repair", repair_rounds=2)
    assert result["outcome"] == "abstain" and result["endpoint_reason"] == reason
    assert result["calls_used"] == calls
    assert adapter.kinds.count("repair_obligation") == 1


def test_resample_repair_mode_draws_one_fresh_solution(tmp_path):
    adapter = Undecided("inconclusive")
    result, _ = run(tmp_path, adapter, policy="resample", undecided_checks="repair")
    assert result["outcome"] == "complete" and result["endpoint_reason"] == "resample_complete"
    assert result["calls_used"] == 6 and adapter.kinds.count("solve_obligation") == 2
    fresh = [p for p in adapter.packets if p["binding"]["action_kind"] == "solve_obligation"][1]
    assert fresh["context"]["target_candidate"] is None and fresh["context"]["selected_issue"] is None
    assert DOUBT not in json.dumps(fresh)
    again = Undecided("inconclusive", always=True)
    result, _ = run(tmp_path / "again", again, policy="resample", undecided_checks="repair")
    assert result["outcome"] == "abstain" and result["endpoint_reason"] == "resample_check_inconclusive"
    assert result["calls_used"] == 5 and again.kinds.count("solve_obligation") == 2


def test_blocking_finding_beside_an_incomplete_check_is_repaired_in_repair_mode(tmp_path):
    class Mixed(Undecided):
        def _response(self, request):
            value = json.loads(FakeTransport._response(self, request))
            binding = value["binding"]
            if ".L0." in binding["request_id"]:
                if binding["action_kind"] == "alignment_review":
                    return b"invalid JSON"
                if binding["action_kind"] == "whole_proof_review":
                    value["output"].update(outcome="fail", findings=[{
                        "schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                        "target_ref": binding["target_ref"], "kind": "invalid_step", "severity": "material",
                        "location": None, "claim": "Step 2 divides by zero."}])
            return json.dumps(value).encode()
    abstain, repair = Mixed(), Mixed()
    result, _ = run(tmp_path / "abstain", abstain)
    assert result["outcome"] == "abstain" and result["endpoint_reason"] == "ladder_check_incomplete"
    result, _ = run(tmp_path / "repair", repair, undecided_checks="repair")
    assert result["outcome"] == "complete"
    assert [issue["claim"] for issue in repair_issues(repair)] == ["Step 2 divides by zero."]


@pytest.mark.parametrize("value", ["sometimes", "", "REPAIR", True, ["repair"]])
def test_invalid_setting_is_rejected_before_any_probe_or_file(tmp_path, value):
    with patch.object(api, "inspect_host", side_effect=AssertionError("no probe")), \
         pytest.raises(ValueError, match="undecided_checks"):
        api.solve(tmp_path / "absent.md", out=tmp_path / "run", undecided_checks=value)
    assert not (tmp_path / "run").exists()


def test_cli_flag_and_config_file_reach_solve(tmp_path, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    config = tmp_path / "victorique.toml"
    config.write_text('undecided_checks = "repair"\n')
    for extra, expected in (([], None), (["--undecided-checks", "repair"], "repair"),
                            (["--config", str(config)], "repair"),
                            (["--config", str(config), "--undecided-checks", "abstain"], "abstain")):
        with patch("sys.argv", ["victorique", "solve", "problem.md", *extra]), \
             patch.object(api, "solve", return_value={}) as solve, redirect_stdout(io.StringIO()):
            cli.main()
        assert solve.call_args.kwargs["undecided_checks"] == expected
