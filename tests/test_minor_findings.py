"""The minor_findings setting: a check that says pass with only minor findings.

``nonblocking`` (default, the 0.1.0b2 rule) counts it as passed. ``blocking``
counts it as not passed and treats its minor findings like blocking ones.
"""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

import pytest

from victorique import adaptive_contracts as c, api, bench, bestofn, cli, settings
from victorique.fake import FakeTransport
from tests.test_bestofn import reference, usage
from tests.test_pass_rule import REPORTS, report

PROBLEM = "Prove n + 0 = n for every integer n."
NOTE = "The last sentence restates the claim instead of concluding."


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch):
    monkeypatch.delenv(settings.CONFIG_ENVIRONMENT, raising=False)


class MinorNote(FakeTransport):
    """Every check says pass. The checks of the first version checked list one minor
    finding (with ``always``, the checks of every version); an investigator confirms
    that finding on the version it was reported for and refutes it on later ones."""

    def __init__(self, always=False):
        self.always = always
        self.noted = None
        self.kinds, self.packets = [], []

    def _response(self, request):
        value = json.loads(super()._response(request))
        binding, output = value["binding"], value["output"]
        kind, ref = binding["action_kind"], binding["target_ref"]
        if kind in c.GATES:
            self.noted = self.noted or ref
            if self.always or ref == self.noted:
                # One identity per check: the full controller keeps every issue identity.
                output["findings"] = [{"schema": "victorique.adaptive_defect.v1",
                                       "issue_id": "wording-" + binding["request_id"], "target_ref": ref,
                                       "kind": "ambiguity", "severity": "minor", "location": None, "claim": NOTE}]
        if kind == "investigate" and binding["issue_id"].startswith("wording-"):
            content = request.packet["context"]["target_candidate"]["content"]
            confirmed = ref == self.noted
            output.update(decision="confirmed" if confirmed else "refuted", location={
                "start_byte": 0, "end_byte": len(content.encode()), "quote": content} if confirmed else None)
        return json.dumps(value).encode()

    def start_once(self, request):
        self.kinds.append(request.packet["binding"]["action_kind"])
        self.packets.append(request.packet)
        return super().start_once(request)

    def observe_existing(self, request):
        return FakeTransport.start_once(self, request)


def problem_file(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    problem = tmp_path / "problem.md"
    problem.write_text(PROBLEM)
    return problem


def run(tmp_path, adapter, policy, **options):
    problem = problem_file(tmp_path)
    if policy == "bestofn":
        options = {"full_calls": reference(tmp_path), "calls": 5, **options}
    with patch.object(api, "FakeTransport", return_value=adapter), \
         patch.object(bestofn, "call_rows", side_effect=usage), redirect_stdout(io.StringIO()):
        result = api.solve(problem, policy=policy, transport="fake", out=tmp_path / "run", **options)
    return result, tmp_path / "run"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def issues_sent_to(adapter, kind):
    packet = next(p for p in adapter.packets if p["binding"]["action_kind"] == kind)
    return json.loads(packet["template"]["instructions"])["policy_instructions"]["issues"]


# ---- the rule and the setting ----------------------------------------------------------

@pytest.mark.parametrize("name", REPORTS)
def test_only_a_pass_with_minor_findings_changes(name):
    value = report(name)
    assert c.gate_passes(value) is c.gate_passes(value, "nonblocking") is (name in ("pass", "pass_minor"))
    assert c.gate_passes(value, "blocking") is (name == "pass")
    assert c.blocking_findings(value) == [f for f in value["findings"] if f["severity"] != "minor"]
    # Under blocking, the minor finding of a pass becomes blocking; a fail with only minor
    # findings and a pass with a doubt list no blocking finding (undecided_checks handles them).
    expected = value["findings"] if name == "pass_minor" else c.blocking_findings(value)
    assert c.blocking_findings(value, "blocking") == expected


def test_setting_values_and_errors():
    assert settings.MINOR_FINDINGS == c.MINOR_FINDINGS == ("nonblocking", "blocking")
    assert settings.minor_findings() == "nonblocking"
    assert settings.minor_findings("blocking") == "blocking"
    for value in ("sometimes", "", "BLOCKING", True, 1, ["blocking"]):
        with pytest.raises(ValueError, match="minor_findings"):
            settings.minor_findings(value)
    for value in ("", "block", None):
        with pytest.raises(ValueError, match="minor_findings"):
            c.gate_passes(report("pass"), value)
    # The frozen limits carry the value only when it is blocking; without it the rule is nonblocking.
    limits = read_limits()
    assert c.minor_findings_rule(limits) == "nonblocking"
    for value in c.MINOR_FINDINGS:
        assert c.minor_findings_rule(c.validate_limits({**limits, "minor_findings": value})) == value
    for value in ("BLOCKING", True, None):
        with pytest.raises(c.AdaptiveContractError, match="minor_findings"):
            c.validate_limits({**limits, "minor_findings": value})
    with pytest.raises(c.AdaptiveContractError, match="limits"):
        c.validate_limits({**limits, "minor": "blocking"})


def read_limits():
    from victorique.prepare import build_manifest
    return build_manifest(problem_raw=PROBLEM.encode(), session_id="run-x", run_id="run-x", model="fake", calls=12,
                          deadline="2099-01-01T00:00:00+00:00", source_ref="test", provider_id="synthetic",
                          mode="synthetic")["limits"]


def test_settings_file(tmp_path, monkeypatch):
    path = tmp_path / "victorique.toml"
    path.write_text('minor_findings = "blocking"\nundecided_checks = "repair"\n')
    assert settings.load_config(path) == {"minor_findings": "blocking", "undecided_checks": "repair"}
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(path))
    assert settings.command_settings(None, minor_findings=None) == {"minor_findings": "blocking"}
    assert settings.command_settings(None, minor_findings="nonblocking") == {"minor_findings": "nonblocking"}
    for text in ('minor_findings = "block"\n', "minor_findings = true\n"):
        path.write_text(text)
        with pytest.raises(ValueError, match="minor_findings"):
            settings.load_config(path)


@pytest.mark.parametrize("command", ["solve", "bench"])
def test_cli_flag_over_settings_file_over_default(tmp_path, command):
    config = tmp_path / "victorique.toml"
    config.write_text('minor_findings = "blocking"\n')
    target = (api, "solve") if command == "solve" else (bench, "bench")
    argument = "problem.md" if command == "solve" else "problems"
    for extra, expected in (([], None), (["--minor-findings", "blocking"], "blocking"),
                            (["--config", str(config)], "blocking"),
                            (["--config", str(config), "--minor-findings", "nonblocking"], "nonblocking")):
        with patch("sys.argv", ["victorique", command, argument, *extra]), \
             patch.object(*target, return_value={}) as called, redirect_stdout(io.StringIO()):
            cli.main()
        assert called.call_args.kwargs["minor_findings"] == expected


def test_verify_keeps_its_own_rule_and_has_no_option(tmp_path, capsys):
    with patch("sys.argv", ["victorique", "verify", "proof.md", "--problem", "problem.md",
                            "--minor-findings", "blocking"]), pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and "--minor-findings" in capsys.readouterr().err
    # A settings file may hold the key; verify does not use it.
    config = tmp_path / "victorique.toml"
    config.write_text('minor_findings = "blocking"\ncall_timeout_seconds = 90\n')
    from victorique import verify
    with patch("sys.argv", ["victorique", "verify", "proof.md", "--problem", "problem.md", "--config", str(config)]), \
         patch.object(verify, "verify", return_value={"outcome": "pass"}) as called, \
         redirect_stdout(io.StringIO()), pytest.raises(SystemExit):
        cli.main()
    assert called.call_args.kwargs["call_timeout_seconds"] == 90 and "minor_findings" not in called.call_args.kwargs


@pytest.mark.parametrize("value", ["sometimes", "", "BLOCKING", True, ["blocking"]])
def test_invalid_value_is_rejected_before_any_probe_or_file(tmp_path, value):
    with patch.object(api, "inspect_host", side_effect=AssertionError("no probe")), \
         pytest.raises(ValueError, match="minor_findings"):
        api.solve(tmp_path / "absent.md", out=tmp_path / "run", minor_findings=value)
    assert not (tmp_path / "run").exists()
    with pytest.raises(ValueError, match="minor_findings"):
        bench.bench(tmp_path, out=tmp_path / "bench", transport="fake", minor_findings=value)
    assert not (tmp_path / "bench").exists()


def test_bench_passes_the_setting_to_each_solve(tmp_path):
    from tests.test_bench import delivered, problem
    problem(tmp_path / "problems", "a")
    seen = []

    def solve_fn(path, *, out, **kwargs):
        seen.append(kwargs)
        return delivered(path, out=out)
    result = bench.bench(tmp_path / "problems", out=tmp_path / "out", transport="fake", solve_fn=solve_fn,
                         minor_findings="blocking")
    assert seen[-1]["minor_findings"] == "blocking" and result["minor_findings"] == "blocking"
    assert read(tmp_path / "out" / "bench.json")["minor_findings"] == "blocking"
    bench.bench(tmp_path / "problems", out=tmp_path / "default", transport="fake", solve_fn=solve_fn)
    assert seen[-1]["minor_findings"] == "nonblocking"


# ---- each policy with the fake transport -------------------------------------------------

@pytest.mark.parametrize("value,outcome,reason", [(None, "complete", "single_complete"),
                                                  ("nonblocking", "complete", "single_complete"),
                                                  ("blocking", "abstain", "single_review_not_passed")])
def test_single_delivers_a_pass_with_one_minor_finding_only_under_nonblocking(tmp_path, value, outcome, reason):
    adapter = MinorNote()
    result, out = run(tmp_path, adapter, "single", **({} if value is None else {"minor_findings": value}))
    assert (result["outcome"], result["endpoint_reason"], result["calls_used"]) == (outcome, reason, 3)
    assert read(out / "invocation.json")["parameters"]["minor_findings"] == (value or "nonblocking")
    assert ("minor_findings" in read(out / "state" / "run-config.json")["manifest"]["limits"]) is (value == "blocking")


@pytest.mark.parametrize("value,outcome", [("nonblocking", "complete"), ("blocking", "abstain")])
def test_bestofn_delivers_only_under_nonblocking(tmp_path, value, outcome):
    result, _ = run(tmp_path, MinorNote(), "bestofn", minor_findings=value)
    assert result["outcome"] == outcome
    assert result["endpoint_reason"] == ("bestofn_complete" if outcome == "complete" else "bestofn_review_not_passed")


def test_ladder_sends_the_minor_finding_to_repair_under_blocking(tmp_path):
    adapter = MinorNote()
    result, out = run(tmp_path / "blocking", adapter, "ladder", minor_findings="blocking")
    assert result["outcome"] == "complete" and result["endpoint_reason"] == "ladder_complete"
    assert result["calls_used"] == 6 and adapter.kinds.count("repair_obligation") == 1
    # The two checks reported the same claim; the repair judges it once, as the reviewer wrote it.
    (issue,) = issues_sent_to(adapter, "repair_obligation")
    assert issue["claim"] == NOTE and issue["severity"] == "minor"
    # The failed checks run again on the repaired version.
    assert adapter.kinds[-3:] == ["repair_obligation", "alignment_review", "whole_proof_review"]
    _, loop = api._open(out)
    assert loop.core.snapshot()["endpoint"]["root_candidate"]["revision"] == 2
    assert result["non_blocking"] == [] and result["unresolved_issues"] == []

    plain = MinorNote()
    result, _ = run(tmp_path / "nonblocking", plain, "ladder")
    assert result["outcome"] == "complete" and result["calls_used"] == 3 and "repair_obligation" not in plain.kinds
    assert [item["claim"] for item in result["non_blocking"]] == [NOTE, NOTE]


@pytest.mark.parametrize("policy,reason", [("ladder", "ladder_unresolved"), ("resample", "resample_unresolved")])
def test_checks_that_always_pass_with_a_minor_finding_never_deliver_under_blocking(tmp_path, policy, reason):
    adapter = MinorNote(always=True)
    result, _ = run(tmp_path, adapter, policy, minor_findings="blocking")
    # Two rounds after the first: each repairs (ladder) or solves again (resample), then the failed check fails again.
    assert (result["outcome"], result["endpoint_reason"], result["calls_used"]) == ("abstain", reason, 7)
    step = "repair_obligation" if policy == "ladder" else "solve_obligation"
    assert adapter.kinds.count(step) == (2 if policy == "ladder" else 3)
    # They are listed as blocking problems, not as minor remarks or undecided checks.
    assert [item["claim"] for item in result["unresolved_issues"]] == [NOTE, NOTE]
    assert result["non_blocking"] == [] and result["inconclusive_checks"] == []


def test_resample_starts_a_fresh_solution_under_blocking(tmp_path):
    adapter = MinorNote()
    result, _ = run(tmp_path, adapter, "resample", minor_findings="blocking")
    assert result["outcome"] == "complete" and result["endpoint_reason"] == "resample_complete"
    assert result["calls_used"] == 6 and adapter.kinds.count("solve_obligation") == 2
    fresh = [p for p in adapter.packets if p["binding"]["action_kind"] == "solve_obligation"][1]
    assert fresh["context"]["target_candidate"] is None and fresh["context"]["selected_issue"] is None
    assert NOTE not in json.dumps(fresh)


def test_full_sends_the_minor_finding_to_repair_under_blocking(tmp_path):
    adapter = MinorNote()
    result, out = run(tmp_path / "blocking", adapter, "full", calls=20, minor_findings="blocking")
    assert result["outcome"] == "complete"
    # The controller made the minor finding an issue: investigated, confirmed and repaired.
    worked = [(p["binding"]["action_kind"], p["binding"]["target_ref"]) for p in adapter.packets
              if (p["binding"]["issue_id"] or "").startswith("wording-")]
    assert [kind for kind, _ in worked] == ["investigate", "repair_obligation", "investigate"]
    assert worked[0][1] == adapter.noted and worked[2][1]["revision"] == adapter.noted["revision"] + 1
    _, loop = api._open(out)
    endpoint = loop.core.snapshot()["endpoint"]
    assert endpoint["root_candidate"]["revision"] == adapter.noted["revision"] + 1
    assert len(endpoint["gates"]) == 2 and all(not gate["report"]["findings"] for gate in endpoint["gates"])
    limits = read(out / "state" / "run-config.json")["manifest"]["limits"]
    assert limits["minor_findings"] == "blocking"
    # Role instructions are the same under both values: the limits they show leave the setting out.
    plan = next(p for p in adapter.packets if p["binding"]["action_kind"] == "propose_plan")
    shown = json.loads(plan["template"]["instructions"])["bound_constraints"]["controller_limits"]
    assert shown == {key: value for key, value in limits.items() if key != "minor_findings"}
    # Under nonblocking the same checks pass: tests/test_gate_contract.py, a pass with a minor note in full.


def test_escalated_full_child_keeps_the_setting(tmp_path):
    with redirect_stdout(io.StringIO()):
        _, loop = api._create_run(problem_file(tmp_path), policy="ladder", transport="fake", out=tmp_path / "run",
                                  repair_rounds=0, escalate=True, minor_findings="blocking")
        loop._escalate(loop.core.snapshot()["exchange"])
    child = read(tmp_path / "run" / "state" / "escalated" / "run-config.json")
    assert child["manifest"]["limits"]["minor_findings"] == "blocking"


# ---- the invocation record and resume ------------------------------------------------------

def test_resume_keeps_the_recorded_value_and_reads_no_settings_file(tmp_path, monkeypatch):
    with redirect_stdout(io.StringIO()):
        directory, _ = api._create_run(problem_file(tmp_path), policy="single", transport="fake",
                                       out=tmp_path / "run", minor_findings="blocking")
    assert read(directory / "invocation.json")["parameters"]["minor_findings"] == "blocking"
    config = tmp_path / "victorique.toml"
    config.write_text('minor_findings = "nonblocking"\n')
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(config))
    adapter = MinorNote()
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.resume(directory)
        assert result["outcome"] == "abstain" and result["endpoint_reason"] == "single_review_not_passed"
        assert api.resume(directory) == result


def test_a_run_recorded_before_the_setting_resumes_with_nonblocking(tmp_path, monkeypatch):
    with redirect_stdout(io.StringIO()):
        directory, _ = api._create_run(problem_file(tmp_path), policy="ladder", transport="fake",
                                       out=tmp_path / "run")
    invocation = read(directory / "invocation.json")
    del invocation["parameters"]["minor_findings"]
    (directory / "invocation.json").write_text(json.dumps(invocation), encoding="utf-8")
    config = tmp_path / "victorique.toml"
    config.write_text('minor_findings = "blocking"\n')
    monkeypatch.setenv(settings.CONFIG_ENVIRONMENT, str(config))
    adapter = MinorNote()
    with patch.object(api, "FakeTransport", return_value=adapter), redirect_stdout(io.StringIO()):
        result = api.resume(directory)
    assert result["outcome"] == "complete" and result["calls_used"] == 3
    assert "repair_obligation" not in adapter.kinds and len(result["non_blocking"]) == 2
