"""One pass rule: the same check report gets the same verdict in every policy.

Each run below receives the same report from both checks. A run delivers exactly
when ``adaptive_contracts.gate_passes`` accepts that report; the paired runner's
shared first check classifies it the same way.
"""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

import pytest

from victorique import adaptive_contracts as c, api, bestofn, ladder, shared_prefix
from victorique.fake import FakeTransport
from tests.test_bestofn import PROBLEM, reference, usage

REPORTS = {
    "pass": ("pass", [], []),
    "pass_minor": ("pass", ["minor"], []),
    "pass_doubt": ("pass", [], ["Is the base case stated?"]),
    "pass_material": ("pass", ["material"], []),
    "fail_minor": ("fail", ["minor"], []),
    "fail_material": ("fail", ["material"], []),
    "unknown_doubt": ("unknown", [], ["The final step is unclear."]),
    "unknown": ("unknown", [], []),
}
PASSING = {"pass", "pass_minor"}


class SameReport(FakeTransport):
    """Every alignment and whole-proof check returns one fixed report."""

    def __init__(self, name):
        self.outcome, self.severities, self.doubts = REPORTS[name]
        self.kinds = []

    def start_once(self, request):
        self.kinds.append(request.packet["binding"]["action_kind"])
        return super().start_once(request)

    def observe_existing(self, request):
        return FakeTransport.start_once(self, request)

    def _response(self, request):
        value = json.loads(super()._response(request))
        binding = value["binding"]
        if binding["action_kind"] in c.GATES:
            value["output"].update(outcome=self.outcome, uncertainties=list(self.doubts), findings=[
                {"schema": "victorique.adaptive_defect.v1", "issue_id": "note-" + str(i),
                 "target_ref": binding["target_ref"], "kind": "invalid_step", "severity": severity,
                 "location": None, "claim": "The same concrete " + severity + " concern."}
                for i, severity in enumerate(self.severities)])
        return json.dumps(value).encode()


def report(name):
    outcome, severities, doubts = REPORTS[name]
    return {"outcome": outcome, "uncertainties": list(doubts),
            "findings": [{"severity": severity} for severity in severities]}


@pytest.mark.parametrize("name", REPORTS)
def test_every_rule_is_gate_passes(name):
    assert c.gate_passes(report(name)) is (name in PASSING)
    assert ladder._passing(report(name)) is (name in PASSING)
    assert ladder._passing(None) is False


@pytest.mark.parametrize("policy", ["single", "ladder", "resample", "bestofn"])
@pytest.mark.parametrize("name", REPORTS)
def test_same_report_same_verdict_in_every_policy(tmp_path, policy, name):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    adapter = SameReport(name)
    options = {"full_calls": reference(tmp_path), "calls": 5} if policy == "bestofn" else {}
    with patch.object(api, "FakeTransport", return_value=adapter), \
         patch.object(bestofn, "call_rows", side_effect=usage), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy=policy, transport="fake", out=tmp_path / "run", **options)
    assert result["outcome"] == ("complete" if name in PASSING else "abstain")
    if policy in ("ladder", "resample"):
        if name in PASSING:
            assert result["endpoint_reason"] == policy + "_complete"
        elif name in ("pass_material", "fail_material"):
            assert result["unresolved_issues"]  # Blocking findings still trigger the repair step.
        else:
            # No blocking finding: inconclusive, which stops the run by default.
            assert result["endpoint_reason"] == policy + "_check_inconclusive"
            assert len(result["inconclusive_checks"]) == 2
            assert adapter.kinds.count("solve_obligation") == 1 and "repair_obligation" not in adapter.kinds


@pytest.mark.parametrize("name", REPORTS)
def test_paired_first_check_uses_the_same_rule(tmp_path, name):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    result = shared_prefix.solve(problem, out=tmp_path / "paired", transport="fake",
                                 adapter_factory=lambda index, policy: SameReport(name))
    status = result["problems"][0]["prefix_status"]
    blocking = name in ("pass_material", "fail_material")
    assert status == ("passed" if name in PASSING else "failed" if blocking else "inconclusive")
    # single, ladder and resample arms: deliver exactly on a passing report.
    assert {arm["outcome"] for arm in result["problems"][0]["arms"].values()} == {
        "complete" if name in PASSING else "abstain"}
