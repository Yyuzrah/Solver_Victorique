from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from victorique import api, shared_prefix as paired
from victorique import adaptive_contracts as c
from tests.test_resample import Scripted


class Factory:
    def __init__(self, mode="fix"):
        self.mode = mode
        self.adapters = {}
        self.order = []

    def __call__(self, index, policy):
        key = (index, policy)
        if key not in self.adapters:
            adapter = Scripted(self.mode)
            original = adapter.start_once
            def start(request):
                self.order.append((index, policy, request.packet["binding"]["request_id"]))
                return original(request)
            adapter.start_once = start
            self.adapters[key] = adapter
        return self.adapters[key]


def run(tmp_path, mode="fix", count=1, **options):
    problems = []
    for index in range(count):
        problem = tmp_path / f"problem{index}.md"
        problem.write_text(f"Prove n + 0 = n for every integer n. Example {index}.")
        problems.append(problem)
    factory = Factory(mode)
    out = tmp_path / "paired"
    result = paired.solve(problems, out=out, transport="fake", adapter_factory=factory, **options)
    return result, out, factory


def read(path):
    return json.loads(path.read_text())


def test_absolute_deadline_shared_prefix_and_resume(tmp_path):
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    result, out, factory = run(tmp_path, mode="pass", calls=3, deadline_utc=deadline)
    assert read(out / "pair.json")["deadline_utc"] == deadline
    assert all(arm["deadline_utc"] == deadline for arm in result["problems"][0]["arms"].values())
    again = paired.resume(out, adapter_factory=factory)
    assert again["physical_calls"] == result["physical_calls"] == 3
    assert len(factory.order) == 3


@pytest.mark.parametrize("deadline", ["2001-01-01T00:00:00Z", "2028-01-01T00:00:00", "2028-01-01T00:00:00-05:00"])
def test_shared_deadline_rejected_without_directory_or_host_probe(tmp_path, monkeypatch, deadline):
    problem = tmp_path / "problem.md"
    problem.write_text("INERT problem")
    monkeypatch.setattr(api, "inspect_host", lambda *a: pytest.fail("host probe"))
    with pytest.raises(ValueError):
        paired.solve(problem, out=tmp_path / "out", deadline_utc=deadline)
    assert not (tmp_path / "out").exists()


def test_shared_cli_passes_absolute_deadline(monkeypatch, capsys):
    seen = []
    deadline = "2028-01-01T00:00:00Z"
    monkeypatch.setattr(sys, "argv", ["shared-prefix", "solve", "p.md", "--out", "out", "--deadline-utc", deadline])
    monkeypatch.setattr(paired, "solve", lambda **kwargs: seen.append(kwargs) or {})
    paired.main()
    assert seen[0]["deadline_utc"] == deadline


def test_fail_with_only_minor_findings_is_an_inconclusive_prefix(tmp_path):
    # The paired L0 uses the same pass rule as the policies: no arm delivers or forks.
    result, _, factory = run(tmp_path, "minor")
    problem = result["problems"][0]
    assert problem["prefix_status"] == "inconclusive"
    assert result["physical_calls"] == 3 == len(factory.order)
    assert {arm["outcome"] for arm in problem["arms"].values()} == {"abstain"}


@pytest.mark.parametrize("mode", ["pass", "minor_pass"])
def test_success_shares_three_physical_calls_and_same_version(tmp_path, mode):
    result, out, factory = run(tmp_path, mode)
    assert result["physical_calls"] == 3 == len(factory.order)
    problem = result["problems"][0]
    assert problem["prefix_status"] == "passed"
    arms = problem["arms"]
    assert {arm["logical_calls"] for arm in arms.values()} == {3}
    assert {arm["additional_physical_calls"] for arm in arms.values()} == {0}
    assert {arm["outcome"] for arm in arms.values()} == {"complete"}
    assert arms["single"]["candidate_ref"] == arms["ladder"]["candidate_ref"] == arms["resample"]["candidate_ref"]
    assert len({(out / "0000" / policy / "proof.md").read_text() for policy in paired.POLICIES}) == 1
    assert not (out / "0000" / "ladder" / "state").exists()
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == 3
    plan = read(out / "pair.json")
    plan["calls"] += 1
    (out / "pair.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="frozen original prefix"):
        paired.resume(out, adapter_factory=factory)
    assert len(factory.order) == 3


def test_failed_prefix_forks_isolated_writers_and_keeps_original_identities(tmp_path):
    result, out, factory = run(tmp_path, calls=6)
    arms = result["problems"][0]["arms"]
    assert result["physical_calls"] == 9 == len(factory.order)
    assert [arms[p]["logical_calls"] for p in paired.POLICIES] == [3, 6, 6]
    assert [arms[p]["outcome"] for p in paired.POLICIES] == ["abstain", "complete", "complete"]
    prefix = paired._PrefixLoop.open(out / "0000/prefix/state", adapter=factory(0, "prefix"))
    base = prefix.core.snapshot()["exchange"]
    branches = []
    for policy in ("ladder", "resample"):
        loop = paired.LOOPS[policy].open(out / "0000" / policy / "state", adapter=factory(0, policy))
        local = loop.core.snapshot()["exchange"]
        assert loop.config["manifest"] == prefix.config["manifest"]
        assert local["sequence"] > base["sequence"]
        assert {rid: local["requests"][rid] for rid in base["requests"]} == base["requests"]
        assert local["observations"][:3] == base["observations"]
        assert local["events"][:len(base["events"])] == base["events"]
        assert len(loop.bridge.status()["jobs"]) == 3
        assert local["manifest"]["limits"]["global_call_limit"] == 6
        assert arms[policy]["deadline_utc"] == base["manifest"]["limits"]["stop_condition"]["deadline_utc"]
        assert all(row["record"]["session_id"] == base["manifest"]["session_id"] for row in local["requests"].values())
        branches.append(set(local["requests"]) - set(base["requests"]))
    assert branches[0].isdisjoint(branches[1])
    assert prefix.core.snapshot()["exchange"] == base
    with pytest.raises(ValueError, match="victorique.shared_prefix"):
        api.resume(out / "0000/resample")
    new_solver = next(p for p in factory(0, "resample").packets if p["binding"]["action_kind"] == "solve_obligation")
    assert new_solver["context"]["target_candidate"] is None
    assert new_solver["context"]["selected_issue"] is None
    assert "PRIVATE" not in json.dumps(new_solver)
    assert "original manuscript" not in json.dumps(new_solver)
    for policy in ("ladder", "resample"):
        packets = factory(0, policy).packets
        candidate = next(json.loads(factory(0, policy)._response(type("R", (), {"packet": p})()))["output"]
                         for p in packets if p["binding"]["action_kind"] in ("solve_obligation", "repair_obligation"))
        for packet in packets:
            if packet["binding"]["action_kind"] in c.GATES:
                assert packet["binding"]["target_ref"] == c.candidate_ref(candidate)
                assert "assessments" not in packet["context"]["target_candidate"]
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == 9


@pytest.mark.parametrize("mode", ["placeholder", "empty"])
def test_precheck_failure_uses_actual_one_call_prefix(tmp_path, mode):
    result, out, factory = run(tmp_path, mode, calls=4)
    item = result["problems"][0]
    assert item["shared_calls"] == 1
    assert item["physical_calls"] == item["physical_call_limit"] == 7
    assert item["arms"]["single"]["logical_calls"] == 1
    assert item["arms"]["single"]["outcome"] == "abstain"
    assert item["arms"]["ladder"]["logical_calls"] == item["arms"]["resample"]["logical_calls"] == 4
    assert "standalone single" in result["baseline_note"]
    assert len(factory.order) == 7


@pytest.mark.parametrize("calls", [3, 4, 5])
def test_each_arm_keeps_its_original_budget(tmp_path, calls):
    result, _, factory = run(tmp_path, "always", calls=calls)
    item = result["problems"][0]
    assert item["physical_calls"] == len(factory.order) <= 3 + 2 * (calls - 3)
    assert item["arms"]["single"]["logical_calls"] == 3
    assert all(arm["logical_calls"] <= arm["calls_limit"] for arm in item["arms"].values())
    assert all(arm["outcome"] == "abstain" for arm in item["arms"].values())


@pytest.mark.parametrize("mode,status", [("unknown", "pending"), ("invalid", "incomplete"),
                                         ("cli_failed", "incomplete"), ("uncertain", "inconclusive")])
def test_unresolved_or_execution_failure_never_forks_or_reissues(tmp_path, mode, status):
    result, out, factory = run(tmp_path, mode)
    assert result["problems"][0]["prefix_status"] == status
    before = list(factory.order)
    for _ in range(2):
        assert paired.resume(out, adapter_factory=factory) == result
    assert factory.order == before
    assert all(policy == "prefix" for _, policy, _ in factory.order)
    assert not (out / "0000/ladder/state").exists()
    assert not (out / "0000/resample/state").exists()


def test_resume_observes_original_unknown_before_fork(tmp_path):
    result, out, factory = run(tmp_path, "unknown")
    assert result["physical_calls"] == 3
    adapter = factory(0, "prefix")
    adapter.mode = "fix"
    factory.mode = "fix"
    # Observe the original packet, not another start_once invocation.
    from victorique.fake import FakeTransport
    from victorique.harness_bridge import TransportObservation
    def completed(request):
        observed = FakeTransport.start_once(adapter, request)
        return TransportObservation({**observed.metadata, "observation_id": observed.metadata["observation_id"] + ".completed"},
                                    observed.raw_response, observed.raw_transport)
    adapter.observe_existing = completed
    resumed = paired.resume(out, adapter_factory=factory)
    assert resumed["physical_calls"] == 9
    assert len([p for _, p, _ in factory.order if p == "prefix"]) == 3


def test_resume_after_fork_creation_crash_does_not_duplicate_prefix(tmp_path, monkeypatch):
    original = paired._fork_l0
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted before branch dispatch")
    monkeypatch.setattr(paired, "_fork_l0", crash)
    with pytest.raises(RuntimeError):
        run(tmp_path, calls=6)
    monkeypatch.setattr(paired, "_fork_l0", original)
    factory = Factory()
    result = paired.resume(tmp_path / "paired", adapter_factory=factory)
    assert result["physical_calls"] == 9
    assert len(factory.order) == 6
    assert all(policy != "prefix" for _, policy, _ in factory.order)


def test_deadline_is_not_renewed_at_fork_or_resume(tmp_path, monkeypatch):
    original = paired._fork_l0
    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=1)
    def fork(*args, **kwargs):
        loop = original(*args, **kwargs)
        monkeypatch.setattr("victorique.ladder.datetime", Expired)
        monkeypatch.setattr("victorique.resample.datetime", Expired)
        return loop
    monkeypatch.setattr(paired, "_fork_l0", fork)
    result, out, factory = run(tmp_path)
    assert result["physical_calls"] == 3
    assert all(arm["outcome"] == "abstain" for arm in result["problems"][0]["arms"].values())
    assert len({arm["deadline_utc"] for arm in result["problems"][0]["arms"].values()}) == 1
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == 3


def test_seeded_order_is_problem_interleaved_and_reproducible(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    first, _, factory = run(a, count=2, seed=44)
    second, _, _ = run(b, "pass", count=2, seed=44)
    assert [p["order"] for p in first["problems"]] == [p["order"] for p in second["problems"]]
    assert [index for index, _, _ in factory.order] == sorted(index for index, _, _ in factory.order)
    for index, item in enumerate(first["problems"]):
        observed = [p for i, p, _ in factory.order if i == index and p != "prefix"]
        assert list(dict.fromkeys(observed)) == [p for p in item["order"] if p != "single"]


def test_later_problem_does_not_receive_a_fresh_deadline(tmp_path, monkeypatch):
    original = paired._arm
    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=1)
    def export(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(paired, "datetime", Expired)
        return result
    monkeypatch.setattr(paired, "_arm", export)
    result, out, factory = run(tmp_path, "pass", count=2)
    assert result["physical_calls"] == 3
    assert result["problems"][1]["prefix_status"] == "expired"
    assert result["problems"][1]["physical_calls"] == 0
    assert not (out / "0001/prefix").exists()
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == 3


@pytest.mark.parametrize("transport", ["codex", "claude"])
def test_live_adapters_with_only_offline_workers_have_distinct_remaining_authorizations(tmp_path, monkeypatch, transport):
    from victorique.harness.codex_transport import CodexCliTransport
    from victorique.harness.claude_transport import ClaudeCliTransport
    from tests.test_victorique_harness_codex_contract import public_profile as codex_profile
    from tests.test_claude_contract import public_profile as claude_profile
    profile = codex_profile() if transport == "codex" else claude_profile()
    monkeypatch.setattr(api, "inspect_host", lambda *args, **kwargs: profile)
    monkeypatch.setattr(api, "_mcp_names", lambda *args: [])
    monkeypatch.setattr("victorique.harness.codex_defaults.read_cli_defaults", lambda *args: {"status": "unavailable"})
    adapter_type = CodexCliTransport if transport == "codex" else ClaudeCliTransport
    original = adapter_type._invocation
    calls = []
    scripted = Scripted()
    def offline(self, request, binding):
        invocation = original(self, request, binding)
        calls.append((request, deepcopy(self.config["authorization"])))
        envelope = json.loads(scripted._response(request))
        if transport == "codex":
            rows = [{"type": "thread.started", "thread_id": request.job_id, "model": "offline-model"},
                    {"type": "turn.started"},
                    {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": json.dumps(envelope)}},
                    {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}]
        else:
            rows = [{"type": "system", "subtype": "init", "session_id": request.job_id, "model": "offline-model", "tools": [], "mcp_servers": []},
                    {"type": "result", "subtype": "success", "is_error": False, "session_id": request.job_id,
                     "structured_output": envelope, "usage": {"input_tokens": 7, "output_tokens": 3}}]
        script = "import sys,pathlib; sys.stdin.buffer.read(); "
        if transport == "codex":
            script += "pathlib.Path(" + repr(invocation["final_file"]) + ").write_text(" + repr(json.dumps(envelope)) + ",encoding='utf-8'); "
        script += "print(" + repr("\n".join(json.dumps(row) for row in rows)) + ")"
        return {**invocation, "argv": [sys.executable, "-I", "-c", script], "timeout_seconds": 5}
    monkeypatch.setattr(adapter_type, "_invocation", offline)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n + 0 = n.")
    out = tmp_path / "paired"
    result = paired.solve(problem, out=out, calls=6, transport=transport, model="offline-model",
                          **({"reasoning_effort": "low"} if transport == "codex" else {}))
    assert result["physical_calls"] == 9 == len(calls)
    assert result["physical_tokens"]["input_tokens"] == 63
    assert [result["problems"][0]["arms"][p]["logical_tokens"]["input_tokens"] for p in paired.POLICIES] == [21, 42, 42]
    configs = [read(out / "0000" / policy / "state/run-config.json") for policy in ("prefix", "ladder", "resample")]
    if transport == "codex":
        assert read(out / "pair.json")["reasoning_effort"] == "low"
        assert all(cfg["transport"]["profile"]["reasoning_effort"] == "low" for cfg in configs)
    assert len({cfg["transport"]["authorization"]["authorization_id"] for cfg in configs}) == 3
    assert all(cfg["transport"]["authorization"]["max_process_starts"] == 3 for cfg in configs)
    assert all(cfg["manifest"] == configs[0]["manifest"] for cfg in configs)
    for policy in ("prefix", "ladder", "resample"):
        with sqlite3.connect(out / "0000" / policy / "state/transport/adapter.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
    assert paired.resume(out) == result
    assert len(calls) == 9


@pytest.mark.parametrize("effort,transport", [("invalid", "codex"), ("low", "fake"), ("medium", "claude")])
def test_shared_reasoning_effort_rejects_invalid_settings_before_dispatch(tmp_path, effort, transport):
    problem = tmp_path / "problem.md"
    problem.write_text("INERT problem")
    with pytest.raises(ValueError, match="reasoning_effort"):
        paired.solve(problem, out=tmp_path / "out", reasoning_effort=effort, transport=transport)
    assert not (tmp_path / "out").exists()


def test_module_cli_exercises_the_same_paired_api(tmp_path, monkeypatch, capsys):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n + 0 = n.")
    out = tmp_path / "paired"
    monkeypatch.setattr(sys, "argv", ["shared_prefix", "solve", str(problem), "--out", str(out), "--transport", "fake", "--seed", "8"])
    paired.main()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["physical_calls"] == 3
    monkeypatch.setattr(sys, "argv", ["shared_prefix", "resume", str(out)])
    paired.main()
    assert json.loads(capsys.readouterr().out)["physical_calls"] == 3


@pytest.mark.parametrize("passing_round", [1, 3, None])
def test_explicit_third_round_uses_twelve_call_cap_and_stops_at_first_pass(tmp_path, monkeypatch, passing_round):
    original = Scripted._response
    def response(self, request):
        value = json.loads(original(self, request))
        binding = value["binding"]
        if binding["action_kind"] in c.GATES and ".L1." in binding["request_id"]:
            round_no = int(binding["request_id"].split(".")[2])
            # Alternate which gate blocks so the formerly failing gate passes
            # and the other gate is also checked on each new manuscript.
            failing_kind = "alignment_review" if round_no % 2 else "whole_proof_review"
            if round_no == passing_round or binding["action_kind"] != failing_kind:
                value["output"].update(outcome="pass", findings=[], uncertainties=[])
            else:
                value["output"].update(outcome="fail", uncertainties=[], findings=[{
                    "schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                    "target_ref": binding["target_ref"], "kind": "invalid_step",
                    "severity": "material", "location": None, "claim": "A blocking gap remains."}])
        return json.dumps(value).encode()
    monkeypatch.setattr(Scripted, "_response", response)
    result, out, factory = run(tmp_path, "always", repair_rounds=3, calls=12)
    item = result["problems"][0]
    expected = 6 if passing_round == 1 else 12
    assert item["physical_calls"] == 3 + 2 * (expected - 3) == len(factory.order)
    plan = read(out / "pair.json")
    assert plan["calls"] == 12 and plan["repair_rounds"] == 3
    for policy in ("ladder", "resample"):
        arm = item["arms"][policy]
        assert arm["logical_calls"] == expected and arm["calls_limit"] == 12
        assert arm["outcome"] == ("abstain" if passing_round is None else "complete")
        assert arm["deadline_utc"] == plan["deadline_utc"]
        loop = paired.LOOPS[policy].open(out / "0000" / policy / "state", adapter=factory(0, policy))
        assert loop.config["manifest"]["limits"]["global_call_limit"] == 12
        assert loop.config["manifest"]["limits"]["stop_condition"]["deadline_utc"] == plan["deadline_utc"]
        if expected == 12:
            assert any(".L1.3." in p["binding"]["request_id"] for p in factory(0, policy).packets)
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == item["physical_calls"]


def test_shared_cli_explicit_budget_rounds_keeps_default_two(tmp_path, monkeypatch, capsys):
    from unittest.mock import Mock
    mocked = Mock(return_value={})
    monkeypatch.setattr(paired, "solve", mocked)
    argv = ["shared_prefix", "solve", "problem.md", "--out", str(tmp_path / "paired")]
    monkeypatch.setattr(sys, "argv", argv)
    paired.main()
    assert mocked.call_args.kwargs["repair_rounds"] == 2
    monkeypatch.setattr(sys, "argv", argv + ["--repair-rounds", "12", "--calls", "12"])
    paired.main()
    assert mocked.call_args.kwargs["repair_rounds"] == 12
    assert mocked.call_args.kwargs["calls"] == 12


@pytest.mark.parametrize("mode,expected", [("always", 12), ("fix", 6)])
def test_budget_rounds_keep_first_gate_short_circuit_and_exhaust_original_cap(tmp_path, mode, expected):
    result, out, factory = run(tmp_path, mode, repair_rounds=12, calls=12)
    item = result["problems"][0]
    plan = read(out / "pair.json")
    assert plan["calls"] == plan["repair_rounds"] == 12
    assert item["physical_calls"] == len(factory.order) == 3 + 2 * (expected - 3)
    for policy in ("ladder", "resample"):
        arm = item["arms"][policy]
        assert arm["logical_calls"] == expected and arm["calls_limit"] == 12
        assert arm["deadline_utc"] == plan["deadline_utc"]
        assert arm["outcome"] == ("abstain" if mode == "always" else "complete")
        loop = paired.LOOPS[policy].open(out / "0000" / policy / "state", adapter=factory(0, policy))
        base = loop.core.snapshot()["exchange"]
        assert base["manifest"]["limits"]["global_call_limit"] == 12
        assert base["manifest"]["limits"]["stop_condition"]["deadline_utc"] == plan["deadline_utc"]
        if mode == "always":
            assert base["endpoint"]["reason"] == policy + "_budget"
            assert policy + ".L1.4.review" in base["requests"]
            assert policy + ".L1.4.alignment" not in base["requests"]
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == item["physical_calls"]


def frozen_e2_prefix(tmp_path, monkeypatch, mode="fix", **options):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove n + 0 = n for every integer n.")
    source = tmp_path / "source"
    with monkeypatch.context() as patch:
        patch.setattr(paired, "resume", lambda *args, **kwargs: None)
        paired.solve(problem, out=source, transport="fake", calls=12, repair_rounds=12,
                     task_id="shared-task-1", repetition=1, **options)
    factory = Factory(mode)
    plan = read(source / "pair.json")
    prefix = paired._prefix(source, plan, plan["problems"][0], factory)
    prefix.run()
    return source, prefix, factory


def test_prefix_only_never_runs_original_adaptive_arms_and_retains_four_hour_deadline(tmp_path):
    result, out, factory = run(tmp_path, mode="fix", calls=12, repair_rounds=12, hours=4,
                               prefix_only=True, task_id="block", repetition=1)
    saved = read(out / "pair.json")
    assert saved["prefix_only"] and saved["physical_call_limit"] == 3
    assert datetime.fromisoformat(saved["deadline_utc"]) - datetime.fromisoformat(saved["started_at"]) == timedelta(hours=4)
    assert result["physical_calls"] == 3 and result["problems"][0]["prefix_status"] == "failed"
    assert set(result["problems"][0]["arms"]) == {"single"}
    assert {policy for _, policy, _ in factory.order} == {"prefix"}
    assert paired.resume(out, adapter_factory=factory) == result
    assert len(factory.order) == 3


def test_independent_parent_requires_new_prefix_only_source(tmp_path, monkeypatch):
    source, _, _ = frozen_e2_prefix(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="prefix-only"):
        paired.branch_e2(source, out=tmp_path / "block", task_id="shared-task-1", repetition=1,
                         oracle_grade=tmp_path / "unused", include_parent=True)


def test_independent_block_unknown_parent_retains_slots_and_never_starts_later_arm(tmp_path, monkeypatch):
    from tests.test_oracle_branch import oracle_fixture
    original = paired.solve
    monkeypatch.setattr(paired, "solve", lambda *args, **kwargs: original(*args, **kwargs, prefix_only=True))
    source, prefix, factory, grade_dir = oracle_fixture(tmp_path, monkeypatch, mode="fix")
    starts = []
    def unknown(request):
        starts.append(request.packet["binding"]["request_id"])
        raise TimeoutError("synthetic unresolved original request")
    adapter = factory(0, "parent")
    monkeypatch.setattr(adapter, "start_once", unknown)
    monkeypatch.setattr(adapter, "observe_existing", lambda request: None)
    out = tmp_path / "branches"
    result = paired.branch_e2(source, out=out, task_id="shared-task-1", repetition=1,
                              adapter_factory=factory, oracle_grade=grade_dir, include_parent=True)
    assert list(result["arms"]) == ["parent"] and result["arms"]["parent"]["outcome"] == "pending"
    assert not (out / "V1").exists() and len(starts) == 1
    again = paired.resume_e2(out, adapter_factory=factory)
    assert list(again["arms"]) == ["parent"] and len(starts) == 1
    assert again["new_physical_calls"] == result["new_physical_calls"] == 1


def test_e2_pass_reuses_original_candidate_without_new_calls(tmp_path, monkeypatch):
    source, prefix, factory = frozen_e2_prefix(tmp_path, monkeypatch, "pass")
    before = prefix.core.snapshot()
    reservation = paired.e2_reservation(source, task_id="shared-task-1", repetition=1)
    assert reservation["new_physical_call_limit"] == reservation["slots"] == 0
    result = paired.branch_e2(source, out=tmp_path / "e2", task_id="shared-task-1", repetition=1, adapter_factory=factory)
    assert result["physical_calls"] == 0 and len(factory.order) == 3
    assert result["shared_calls"] == 3 and len(result["arms"]) == 4
    for variant, arm in result["arms"].items():
        assert arm["outcome"] == "complete" and arm["logical_calls"] == 3
        assert arm["candidate_binding"]["kind"] == "shared_L0"
        assert arm["candidate_binding"]["run_directory"] == str(source / "0000/prefix")
        assert not (tmp_path / "e2" / variant / "state").exists()
    assert paired.resume_e2(tmp_path / "e2", adapter_factory=factory) == result
    assert prefix.core.snapshot() == before
    assert not (source / "e2.json").exists()


def test_e2_failed_prefix_forks_four_isolated_bounded_branches(tmp_path, monkeypatch):
    source, prefix, factory = frozen_e2_prefix(tmp_path, monkeypatch)
    before = prefix.core.snapshot()
    reservation = paired.e2_reservation(source, task_id="shared-task-1", repetition=1)
    assert reservation["new_physical_call_limit"] == 36 and reservation["slots"] == 2
    result = paired.branch_e2(source, out=tmp_path / "e2", task_id="shared-task-1", repetition=1, adapter_factory=factory)
    assert result["physical_calls"] == result["new_physical_calls"] == 12
    assert len(factory.order) == 15  # Three source calls, then four three-call repairs.
    assert list(dict.fromkeys(policy for _, policy, _ in factory.order))[1:] == list(paired.E2_VARIANTS)
    bindings, physical_ids = [], []
    for variant, options in paired.E2_VARIANTS.items():
        directory = tmp_path / "e2" / variant
        arm = result["arms"][variant]
        invocation = read(directory / "invocation.json")
        assert all(invocation["parameters"][key] == value for key, value in options.items() if key != "parent")
        assert arm["logical_calls"] == 6 and arm["calls_limit"] == 12
        assert arm["deadline_utc"] == reservation["deadline_utc"]
        assert arm["parent"] == options["parent"]
        assert arm["candidate_binding"]["kind"] == "E2_branch"
        bindings.append((arm["candidate_binding"]["run_directory"], arm["candidate_binding"]["branch_id"]))
        rows = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
        assert len(rows) == 3 and not any(".L0." in row["request_id"] for row in rows)
        physical_ids.extend(row["physical_call_id"] for row in rows)
        loop = paired.LadderLoop.open(directory / "state", adapter=factory(0, variant))
        local = loop.core.snapshot()["exchange"]
        assert {rid: local["requests"][rid] for rid in before["exchange"]["requests"]} == before["exchange"]["requests"]
        assert loop.config["manifest"]["limits"]["global_call_limit"] == 12
    assert len(set(bindings)) == 4 and len(set(physical_ids)) == 12
    assert prefix.core.snapshot() == before
    assert paired.resume_e2(tmp_path / "e2", adapter_factory=factory) == result
    assert len(factory.order) == 15


@pytest.mark.parametrize("mode", ["unknown", "invalid", "cli_failed", "uncertain"])
def test_e2_unknown_and_execution_failure_never_become_mathematical_trigger(tmp_path, monkeypatch, mode):
    source, prefix, factory = frozen_e2_prefix(tmp_path, monkeypatch, mode)
    before = prefix.core.snapshot()
    assert paired.e2_reservation(source, task_id="shared-task-1", repetition=1)["new_physical_call_limit"] == 0
    result = paired.branch_e2(source, out=tmp_path / "e2", task_id="shared-task-1", repetition=1, adapter_factory=factory)
    assert result["physical_calls"] == 0 and len(factory.order) == 3
    assert all(not (tmp_path / "e2" / variant / "state").exists() for variant in paired.E2_VARIANTS)
    assert prefix.core.snapshot() == before


def test_e2_investigation_and_escalation_flags_really_activate(tmp_path, monkeypatch):
    original = Scripted._response
    def refuting(self, request):
        value = json.loads(original(self, request))
        if self.mode == "refute" and value["binding"]["action_kind"] == "repair_obligation":
            for assessment in value["output"]["assessments"]:
                assessment["decision"] = "refuted"
        if self.mode == "refute" and value["binding"]["action_kind"] == "whole_proof_review":
            value["output"].update(outcome="fail", findings=[{
                "schema": "victorique.adaptive_defect.v1", "issue_id": "gap",
                "target_ref": value["binding"]["target_ref"], "kind": "invalid_step", "severity": "material",
                "location": None, "claim": "PRIVATE previous criticism."}])
        return json.dumps(value).encode()
    monkeypatch.setattr(Scripted, "_response", refuting)
    source, prefix, factory = frozen_e2_prefix(tmp_path, monkeypatch, "always")
    factory(0, "V1").mode = "refute"
    result = paired.branch_e2(source, out=tmp_path / "e2", task_id="shared-task-1", repetition=1, adapter_factory=factory)
    assert result["physical_calls"] <= 36
    assert any(p["binding"]["action_kind"] == "investigate" for p in factory(0, "V1").packets)
    assert (tmp_path / "e2/V2/state/escalated").is_dir()
    assert result["arms"]["V3"]["logical_calls"] == 5
    assert result["arms"]["V4"]["logical_calls"] == 7
    assert all(arm["logical_calls"] <= 12 for arm in result["arms"].values())
    child = read(tmp_path / "e2/V2/state/escalated/run-config.json")
    assert child["manifest"]["limits"]["global_call_limit"] == 5
    assert child["manifest"]["limits"]["stop_condition"]["deadline_utc"] == result["deadline_utc"]


def test_e2_binding_and_original_deadline_are_not_replaceable(tmp_path, monkeypatch):
    source, prefix, factory = frozen_e2_prefix(tmp_path, monkeypatch)
    for identity in ({"task_id": "another", "repetition": 1}, {"task_id": "shared-task-1", "repetition": 2}):
        with pytest.raises(ValueError, match="identity"):
            paired.branch_e2(source, out=tmp_path / "wrong", **identity)
    assert not (tmp_path / "wrong").exists()
    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=1)
    monkeypatch.setattr(paired, "datetime", Expired)
    assert paired.e2_reservation(source, task_id="shared-task-1", repetition=1)["new_physical_call_limit"] == 0
    result = paired.branch_e2(source, out=tmp_path / "e2", task_id="shared-task-1", repetition=1, adapter_factory=factory)
    assert result["physical_calls"] == 0 and len(factory.order) == 3
    saved = read(tmp_path / "e2/e2.json")
    saved["deadline_utc"] = Expired.now().isoformat()
    (tmp_path / "e2/e2.json").write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="frozen E2"):
        paired.resume_e2(tmp_path / "e2", adapter_factory=factory)
