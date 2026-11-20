"""One passing rule for gate reports: a pass may keep minor notes or doubts."""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

import pytest

from victorique import adaptive_contracts as c, api
from victorique.fake import FakeTransport

PROBLEM = "Prove n + 0 = n for every integer n."


def report(outcome="pass", severities=(), uncertainties=()):
    return {"outcome": outcome, "uncertainties": list(uncertainties),
            "findings": [{"severity": severity} for severity in severities]}


@pytest.mark.parametrize("value,passes", [
    (report(), True),
    (report(severities=["minor", "minor"]), True),
    (report(uncertainties=["Is the base case stated?"]), False),
    (report(severities=["material"]), False),
    (report(severities=["minor", "fatal"]), False),
    (report("fail", severities=["minor"]), False),
    (report("unknown", uncertainties=["Unclear."]), False),
])
def test_gate_passes(value, passes):
    assert c.gate_passes(value) is passes


class GateNotes(FakeTransport):
    """Every gate says pass, optionally with one finding or one doubt."""

    def __init__(self, severity=None, doubt=False):
        self.severity, self.doubt = severity, doubt

    def _response(self, request):
        value = json.loads(super()._response(request))
        output = value["output"]
        if value["binding"]["action_kind"] in c.GATES:
            if self.severity is not None:
                output["findings"] = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "note",
                                       "target_ref": value["binding"]["target_ref"], "kind": "ambiguity",
                                       "severity": self.severity, "location": None,
                                       "claim": "The notation could be clearer."}]
            if self.doubt:
                output["uncertainties"] = ["Is the base case stated?"]
        return json.dumps(value).encode()


def solve(tmp_path, policy, adapter):
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        return api.solve(problem, policy=policy, transport="fake", out=tmp_path / "run")


@pytest.mark.parametrize("policy", ["single", "ladder", "full"])
def test_pass_with_a_minor_note_is_a_valid_passing_report(tmp_path, policy):
    # Before b2 the validator rejected this report as "pass retains doubts".
    result = solve(tmp_path, policy, GateNotes(severity="minor"))
    assert result["outcome"] == "complete"


@pytest.mark.parametrize("policy", ["single", "ladder"])
@pytest.mark.parametrize("severity,doubt", [(None, True), ("material", False)])
def test_pass_with_a_doubt_or_material_finding_does_not_pass(tmp_path, policy, severity, doubt):
    result = solve(tmp_path, policy, GateNotes(severity=severity, doubt=doubt))
    assert result["outcome"] == "abstain"
