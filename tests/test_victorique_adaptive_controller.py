"""Pure INERT state transitions; these receipts are synthetic declarations."""
from copy import deepcopy
import unittest
from unittest import mock

from victorique import adaptive_contracts as c
from victorique import adaptive_controller as a
from tests.test_victorique_adaptive_contracts import PROBLEM, NOW, limits, plan, candidate, defect, disposition, node


def emit(state, kind, payload):
    return a.reduce(state, {"event_id": "event_" + str(state["state_revision"]),
                           "expected_state_revision": state["state_revision"], "recorded_at_utc": NOW,
                           "kind": kind, "payload": payload})


def reserve(state, kind=None, oid=None, route=None):
    available = a.legal_actions(state, NOW)["actions"]
    selected = next(x for x in available if (kind is None or x["kind"] == kind)
                    and (oid is None or x["obligation_id"] == oid)
                    and (route is None or x["route_id"] == route))
    return emit(state, "reserve_action", {"action_id": "action_" + str(len(state["actions"])), "proposal": selected})


def reserve_issue(state, kind, issue_id):
    selected = next(x for x in a.legal_actions(state, NOW)["actions"] if x["kind"] == kind and x["issue_id"] == issue_id)
    return emit(state, "reserve_action", {"action_id": "action_" + str(len(state["actions"])), "proposal": selected})


def reply(state, response, *, action_id=None, status="completed", cost=None, host=True):
    action_id = action_id or state["actions"][-1]["action_id"]
    return emit(state, "return_action", {"action_id": action_id, "status": status,
        "receipt_id": "receipt_" + str(state["state_revision"]), "host_reference": "synthetic_host_" + action_id if host else None,
        "source_event_id": None, "source_line": None, "cost_units": cost,
        "cost_source": None if cost is None else "synthetic_declared_cost", "response": response})


def planned(config=None, p=None):
    state = a.create_state("inert_session", PROBLEM, config or limits())
    return reply(reserve(state, "propose_plan"), p or plan())


def answer_candidate(state):
    action = state["actions"][-1]["proposal"]
    target = action["target_ref"]
    return candidate(revision=1 if target is None else target["revision"] + 1,
                     oid=action["obligation_id"], route=action["route_id"],
                     plan_revision=action["plan_ref"]["revision"], dependencies=action["dependency_refs"],
                     issues=[] if action["issue_id"] is None else [action["issue_id"]])


def produce(state, kind="solve_obligation", oid=None, route=None):
    state = reserve(state, kind, oid, route)
    return reply(state, answer_candidate(state))


def current(state, oid="root", route="route0"):
    return a._heads(state)[(route, oid)]


def critique(state, findings=(), oid="root"):
    state = reserve(state, "critique_candidate", oid)
    target = a._resolve(state, state["actions"][-1]["proposal"]["target_ref"])
    return reply(state, {"schema": "victorique.adaptive_critique.v1", "target_ref": c.candidate_ref(target),
                         "findings": deepcopy(list(findings)), "no_gap_identified": not findings})


def handoff(state, role, issue_id="issue_1"):
    issue = next(i for i in state["issues"] if i["proposal"]["issue_id"] == issue_id)
    row = next(f for f in state["feedback"] if f["receipt_id"] == issue["current_feedback_receipt_id"])
    payload = {"receipt_id": row["receipt_id"], "target_ref": row["target_ref"]}
    if not row["released"]:
        state = emit(state, "release_feedback", payload)
    return emit(state, "receive_feedback", dict(payload, recipient_role=role))


def investigate(state, decision="confirmed", issue_id="issue_1"):
    state = handoff(state, state["limits"]["action_roles"]["investigate"], issue_id)
    state = reserve(state, "investigate")
    target = a._resolve(state, state["actions"][-1]["proposal"]["target_ref"])
    return reply(state, disposition(target, decision, issue_id))


def planning_handoff(state):
    for source in a.planning_evidence_sources(state):
        payload = {key: source[key] for key in ("receipt_id", "plan_ref", "target_ref")}
        row = next((r for r in state["planning_handoffs"] if r["receipt_id"] == source["receipt_id"]), None)
        if row is None:
            state = emit(state, "release_planning_evidence", payload)
        if row is None or "planner" not in row["received_by"]:
            state = emit(state, "receive_planning_evidence", payload | {"recipient_role": "planner"})
    return state


def gate(state, kind, outcome="pass", findings=(), uncertainties=()):
    state = reserve(state, kind)
    target = state["actions"][-1]["proposal"]["target_ref"]
    return reply(state, {"schema": "victorique.adaptive_gate.v1", "gate_id": kind,
                         "problem_ref": {"artifact_id": PROBLEM["artifact_id"], "revision": 1},
                         "target_ref": target, "coverage": "whole_problem_and_proof", "outcome": outcome,
                         "findings": deepcopy(list(findings)), "uncertainties": list(uncertainties)})


class ControllerTests(unittest.TestCase):
    def test_proposal_and_replay_reject_type_changed_integer_bindings(self):
        state = planned()
        for replacement in (True, 1.0):
            proposal = deepcopy(a.choose(state, NOW)["action"]); proposal["cap_units"] = replacement
            with self.assertRaises(c.AdaptiveContractError): emit(state, "reserve_action", {"action_id": "typed", "proposal": proposal})
        event = {"event_id": "typed_replay", "expected_state_revision": state["state_revision"], "recorded_at_utc": NOW,
                 "kind": "reserve_action", "payload": {"action_id": "typed", "proposal": a.choose(state, NOW)["action"]}}
        state = a.reduce(state, event)
        altered = deepcopy(event); altered["payload"]["proposal"]["cap_units"] = True
        with self.assertRaises(c.AdaptiveContractError): a.reduce(state, altered)

    def test_gate_and_empty_critique_strictly_validate_typed_references(self):
        for gate_kind in ("alignment_review", "critique_candidate"):
            for field, replacement in (("revision", True), ("plan_revision", 1.0)):
                state = reserve(produce(planned()), gate_kind)
                ref = deepcopy(state["actions"][-1]["proposal"]["target_ref"]); ref[field] = replacement
                if gate_kind == "critique_candidate":
                    value = {"schema": "victorique.adaptive_critique.v1", "target_ref": ref, "findings": [], "no_gap_identified": True}
                else:
                    value = {"schema": "victorique.adaptive_gate.v1", "gate_id": gate_kind,
                             "problem_ref": {"artifact_id": PROBLEM["artifact_id"], "revision": 1}, "target_ref": ref,
                             "coverage": "whole_problem_and_proof", "outcome": "pass", "findings": [], "uncertainties": []}
                with self.subTest(kind=gate_kind, field=field), self.assertRaises(c.AdaptiveContractError): reply(state, value)
        state = reserve(produce(planned()), "alignment_review")
        value["target_ref"] = deepcopy(state["actions"][-1]["proposal"]["target_ref"])
        value.update(schema="victorique.adaptive_gate.v1", gate_id="alignment_review",
                     problem_ref={"artifact_id": PROBLEM["artifact_id"], "revision": True},
                     coverage="whole_problem_and_proof", outcome="pass", uncertainties=[])
        value.pop("no_gap_identified", None)
        with self.assertRaises(c.AdaptiveContractError): reply(state, value)

    def _old_investigation_after_repair(self):
        state = produce(planned()); old = deepcopy(current(state))
        state = critique(state, [defect(old, "issue_1"), defect(old, "issue_2")])
        state = handoff(state, "investigator", "issue_1")
        state = reserve_issue(state, "investigate", "issue_1"); first = state["actions"][-1]["action_id"]
        state = reserve_issue(state, "investigate", "issue_2"); second = state["actions"][-1]["action_id"]
        state = reply(state, disposition(old, "confirmed", "issue_1"), action_id=first)
        state = handoff(state, "solver", "issue_1"); state = produce(state, "repair_obligation")
        return state, old, second

    def test_stale_completed_investigation_preserves_raw_receipt_and_cost(self):
        state, old, second = self._old_investigation_after_repair()
        candidates = deepcopy(state["candidates"]); issues = deepcopy(state["issues"])
        raw = disposition(old, "refuted", "issue_2")
        result = reply(state, raw, action_id=second, cost=1)
        row = next(x for x in result["actions"] if x["action_id"] == second)
        self.assertEqual(row["status"], "completed"); self.assertEqual(row["projection"], "stale_audit_only")
        self.assertEqual(row["response"], raw); self.assertEqual(row["cost_units"], 1)
        self.assertEqual(row["receipt"]["host_reference"], "synthetic_host_" + second)
        self.assertEqual(result["candidates"], candidates); self.assertEqual(result["issues"], issues)
        self.assertEqual(a.budget_view(result)["inflight"], 0)

    def test_stale_does_not_excuse_malformed_or_wrong_version_result(self):
        state, old, second = self._old_investigation_after_repair()
        for bad in (disposition(current(state), "refuted", "issue_2"), {"outcome": "pass"}):
            with self.assertRaises(c.AdaptiveContractError): reply(state, bad, action_id=second)
        typed = disposition(old, "refuted", "issue_2"); typed["target_ref"]["revision"] = True
        with self.assertRaises(c.AdaptiveContractError): reply(state, typed, action_id=second)
        self.assertEqual(a.budget_view(state)["inflight"], 1)

    def test_stale_completed_integration_settles_without_candidate_projection(self):
        state = planned(p=plan(branch=True)); state = produce(state, oid="left"); state = produce(state, oid="right")
        state = reserve(state, "integrate"); operation = state["actions"][-1]["action_id"]; raw = answer_candidate(state)
        state = critique(state, [defect(current(state, "left"))], "left")
        state = investigate(state); state = handoff(state, "solver"); state = produce(state, "repair_obligation", "left")
        prior = deepcopy(state["candidates"])
        malformed = deepcopy(raw); malformed["dependency_refs"][0]["revision"] = 2
        with self.assertRaises(c.AdaptiveContractError): reply(state, malformed, action_id=operation)
        state = reply(state, raw, action_id=operation)
        self.assertEqual(state["candidates"], prior); self.assertIsNone(a._root(state, "route0"))
        row = next(x for x in state["actions"] if x["action_id"] == operation)
        self.assertEqual(row["projection"], "stale_audit_only"); self.assertEqual(row["stale_reason"], "dependency_version_changed")
        self.assertEqual(a.budget_view(state)["inflight"], 0)

    def test_stale_completed_gate_cannot_support_new_candidate(self):
        state = produce(planned()); state = reserve(state, "alignment_review")
        operation = state["actions"][-1]["action_id"]; target = deepcopy(state["actions"][-1]["proposal"]["target_ref"])
        state = critique(state, [defect(current(state))]); state = investigate(state)
        state = handoff(state, "solver"); state = produce(state, "repair_obligation")
        report = {"schema": "victorique.adaptive_gate.v1", "gate_id": "alignment_review",
                  "problem_ref": {"artifact_id": PROBLEM["artifact_id"], "revision": 1}, "target_ref": target,
                  "coverage": "whole_problem_and_proof", "outcome": "pass", "findings": [], "uncertainties": []}
        state = reply(state, report, action_id=operation)
        self.assertEqual(state["gates"], [])
        self.assertEqual(next(x for x in state["actions"] if x["action_id"] == operation)["projection"], "stale_audit_only")

    def test_other_known_issues_reserve_calls_fees_and_role_capacity(self):
        configurations = [(limits(global_call_limit=6, max_plan_revisions=1), "completion_call_reserve"),
                          (limits(total_budget_units=6, max_plan_revisions=1), "completion_fee_reserve")]
        for field, reason in (("role_call_limits", "completion_role_call_reserve:investigator"),
                              ("role_budget_units", "completion_role_fee_reserve:investigator")):
            config = limits(max_plan_revisions=1); config[field]["investigator"] = 1
            configurations.append((config, reason))
        for config, reason in configurations:
            state = produce(planned(config)); target = current(state)
            state = critique(state, [defect(target, "issue_1"), defect(target, "issue_2")]); state = handoff(state, "investigator")
            listing = a.legal_actions(state, NOW)
            self.assertFalse(any(x["kind"] == "investigate" for x in listing["actions"]))
            self.assertTrue(any(x["reason"] == reason for x in listing["rejected"]))
            self.assertEqual(a.choose(state, NOW)["decision"], "abstain")

    def test_exact_pending_other_issue_is_not_reserved_twice(self):
        state = produce(planned(limits(global_call_limit=7, total_budget_units=7, max_plan_revisions=1)))
        target = current(state); state = critique(state, [defect(target, "issue_1"), defect(target, "issue_2")])
        state = handoff(state, "investigator")
        state = reserve_issue(state, "investigate", "issue_1"); first = state["actions"][-1]["action_id"]
        state = reserve_issue(state, "investigate", "issue_2"); second = state["actions"][-1]["action_id"]
        self.assertEqual(a.budget_view(state)["inflight"], 2)
        state = reply(state, disposition(target, "refuted", "issue_1"), action_id=first)
        state = reply(state, disposition(target, "refuted", "issue_2"), action_id=second)
        state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        self.assertEqual(a.choose(state, NOW)["decision"], "complete")
        self.assertEqual(len(state["actions"]), 7)

    def test_pure_full_leaf_loop_and_finalization(self):
        with mock.patch("builtins.open", side_effect=AssertionError("no files")), mock.patch("socket.socket", side_effect=AssertionError("no network")):
            state = planned(); state = produce(state); state = critique(state)
            state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
            decision = a.choose(state, NOW)
            self.assertEqual(decision["decision"], "complete")
            state = emit(state, "finalize", {k: decision[k] for k in ("decision", "reason")})
            self.assertFalse(state["endpoint"]["mathematical_truth_verified"])
            self.assertFalse(state["provenance_authenticated"])
            self.assertEqual(len(state["actions"]), 5)
            self.assertIsNone(a.budget_view(state)["actual_cost_units"])

    def test_proposal_is_not_actual_or_candidate_projection(self):
        state = a.create_state("inert_session", PROBLEM, limits()); original = deepcopy(state)
        proposal = a.choose(state, NOW)
        self.assertEqual(state, original)
        self.assertFalse(proposal["dispatch_authorized"])
        self.assertEqual(state["actions"], [])
        with self.assertRaises(c.AdaptiveContractError): reply(state, plan(), action_id="fake_action")
        state = reserve(state)
        self.assertIsNone(state["plan"]); self.assertEqual(state["actions"][0]["status"], "reserved")
        with self.assertRaises(c.AdaptiveContractError): reply(state, plan(), host=False)

    def test_event_cas_idempotence_and_conflicting_replay(self):
        state = a.create_state("inert_session", PROBLEM, limits())
        event = {"event_id": "first", "expected_state_revision": 0, "recorded_at_utc": NOW,
                 "kind": "reserve_action", "payload": {"action_id": "a0", "proposal": a.choose(state, NOW)["action"]}}
        result = a.reduce(state, event)
        self.assertEqual(a.reduce(result, event), result)
        event["event_id"] = "second"
        with self.assertRaises(c.AdaptiveContractError): a.reduce(result, event)
        event["event_id"] = "first"; event["payload"]["action_id"] = "changed"
        with self.assertRaises(c.AdaptiveContractError): a.reduce(result, event)

    def test_exact_budget_allows_two_reserved_leaves_without_double_count(self):
        config = limits(global_call_limit=6, total_budget_units=6, max_critiques_per_candidate=0)
        state = planned(config, plan(branch=True))
        state = reserve(state, "solve_obligation", "left")
        left_id = state["actions"][-1]["action_id"]
        state = reserve(state, "solve_obligation", "right")
        self.assertEqual(a.budget_view(state)["inflight"], 2)
        state = reply(state, candidate(oid="right"))
        state = reply(state, candidate(oid="left"), action_id=left_id)
        state = produce(state, "integrate", "root")
        state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        self.assertEqual(a.choose(state, NOW)["decision"], "complete")
        self.assertEqual(len(state["actions"]), 6)

    def test_same_leaf_cannot_have_two_inflight_actions(self):
        state = reserve(planned(p=plan(branch=True)), "solve_obligation", "left")
        listing = a.legal_actions(state, NOW)
        self.assertFalse(any(x["obligation_id"] == "left" for x in listing["actions"]))
        self.assertTrue(any(x["reason"] == "work_already_inflight" for x in listing["rejected"]))

    def test_unknown_stops_new_dispatch_and_recovers_same_identity_only(self):
        state = reserve(planned(), "solve_obligation")
        action_id = state["actions"][-1]["action_id"]
        state = reply(state, None, status="unknown")
        self.assertEqual(a.choose(state, NOW)["reason"], "recovery_required")
        self.assertEqual(a.legal_actions(state, NOW)["actions"], [])
        count = len(state["actions"])
        state = reply(state, candidate(), action_id=action_id)
        self.assertEqual(len(state["actions"]), count)
        self.assertEqual(current(state)["revision"], 1)

    def test_unknown_recovery_cannot_change_host_reference(self):
        state = reply(reserve(planned(), "solve_obligation"), None, status="unknown")
        event = {"event_id": "different", "expected_state_revision": state["state_revision"], "recorded_at_utc": NOW,
                 "kind": "return_action", "payload": {"action_id": state["actions"][-1]["action_id"], "status": "completed",
                 "receipt_id": "new_receipt", "host_reference": "different_host", "source_event_id": None, "source_line": None,
                 "cost_units": None, "cost_source": None, "response": candidate()}}
        with self.assertRaises(c.AdaptiveContractError): a.reduce(state, event)

    def test_unknown_bill_cannot_be_erased_by_null_or_lower_recovery(self):
        state = reply(reserve(planned(), "solve_obligation"), None, status="unknown", cost=3)
        self.assertTrue(a.budget_view(state)["request_cap_exceeded"])
        with self.assertRaises(c.AdaptiveContractError): reply(state, candidate(), cost=1)
        state = reply(state, candidate(), cost=None)
        self.assertEqual(state["actions"][-1]["cost_units"], 3)
        self.assertEqual(state["actions"][-1]["cost_source"], "synthetic_declared_cost")
        self.assertEqual(a.choose(state, NOW)["reason"], "observed_overspend")

    def test_unknown_with_small_bill_keeps_original_cap(self):
        config = limits(); config["action_caps"]["solve_obligation"] = 4
        state = reply(reserve(planned(config), "solve_obligation"), None, status="unknown", cost=1)
        self.assertEqual(a.budget_view(state)["committed_units"], 5)  # planner 1 + unresolved solver cap 4

    def test_event_and_decision_timestamps_cannot_backdate_handoff(self):
        state = planned()
        event = {"event_id": "backdated", "expected_state_revision": state["state_revision"],
                 "recorded_at_utc": "2026-11-12T04:00:00Z", "kind": "stop", "payload": {"reason": "INERT"}}
        with self.assertRaises(c.AdaptiveContractError): a.reduce(state, event)
        with self.assertRaises(c.AdaptiveContractError): a.choose(state, event["recorded_at_utc"])

    def test_shared_dag_freshness_memoizes_each_candidate(self):
        # Construct only this internal synthetic fixture to count visits; public reducer tests cover provenance.
        state = planned(); p = state["plan"]; p["routes"][0]["root_obligation_id"] = "n23"
        p["routes"][0]["obligations"] = [node("n" + str(i), ["n" + str(j) for j in range(i)]) for i in range(24)]
        refs = []
        for i in range(24):
            value = candidate(oid="n" + str(i), dependencies=refs)
            state["candidates"].append({"artifact": value, "source": {"synthetic_internal_fixture": True}, "status": "candidate_unverified"})
            refs.append(c.candidate_ref(value))
        original = a._resolve
        with mock.patch.object(a, "_resolve", wraps=original) as resolve:
            self.assertTrue(a._fresh(state, state["candidates"][-1]["artifact"]))
            self.assertEqual(resolve.call_count, 276)

    def test_receipt_cannot_be_reused_by_other_action(self):
        state = reserve(planned(), "solve_obligation")
        with self.assertRaises(c.AdaptiveContractError):
            emit(state, "return_action", {"action_id": state["actions"][-1]["action_id"], "status": "completed",
                "receipt_id": state["actions"][0]["receipt"]["receipt_id"], "host_reference": "new_host",
                "source_event_id": None, "source_line": None, "cost_units": None, "cost_source": None, "response": candidate()})

    def test_old_unknown_receipt_identity_stays_reserved_after_recovery(self):
        state = reply(reserve(planned(), "solve_obligation"), None, status="unknown")
        old_receipt = state["actions"][-1]["receipt"]["receipt_id"]
        state = reply(state, candidate()); state = reserve(state, "critique_candidate")
        with self.assertRaises(c.AdaptiveContractError):
            emit(state, "return_action", {"action_id": state["actions"][-1]["action_id"], "status": "unknown",
                "receipt_id": old_receipt, "host_reference": None, "source_event_id": None, "source_line": None,
                "cost_units": None, "cost_source": None, "response": None})

    def test_final_gate_inflight_cannot_trigger_replan(self):
        state = produce(planned(limits(max_critiques_per_candidate=0)))
        state = gate(state, "alignment_review"); state = reserve(state, "whole_proof_review")
        self.assertFalse(any(x["kind"] == "replan" for x in a.legal_actions(state, NOW)["actions"]))
        self.assertEqual(a.choose(state, NOW)["decision"], "wait")

    def test_optional_critic_cannot_consume_call_or_fee_gate_reserve(self):
        for config, reason in ((limits(global_call_limit=4), "completion_call_reserve"),
                               (limits(total_budget_units=4), "completion_fee_reserve")):
            state = produce(planned(config)); listing = a.legal_actions(state, NOW)
            self.assertTrue(any(x["action"]["kind"] == "critique_candidate" and x["reason"] == reason for x in listing["rejected"]))
            self.assertTrue(any(x["kind"] == "alignment_review" for x in listing["actions"]))

    def test_critic_reviewer_alias_cannot_consume_gate_role_calls_or_fees(self):
        for field, reason in (("role_call_limits", "completion_role_call_reserve:alignment_reviewer"),
                              ("role_budget_units", "completion_role_fee_reserve:alignment_reviewer")):
            config = limits(); config["action_roles"]["critique_candidate"] = "alignment_reviewer"
            config[field]["alignment_reviewer"] = 1
            listing = a.legal_actions(produce(planned(config)), NOW)
            self.assertTrue(any(x["action"]["kind"] == "critique_candidate" and x["reason"] == reason for x in listing["rejected"]))

    def test_investigator_reviewer_alias_cannot_consume_gate_role(self):
        config = limits(); config["action_roles"]["investigate"] = "proof_reviewer"
        config["role_call_limits"]["proof_reviewer"] = 1
        state = produce(planned(config)); state = critique(state, [defect(current(state))])
        state = handoff(state, "proof_reviewer")
        listing = a.legal_actions(state, NOW)
        self.assertTrue(any(x["action"]["kind"] == "investigate" and x["reason"] == "completion_role_call_reserve:proof_reviewer" for x in listing["rejected"]))

    def test_feedback_must_be_released_then_received(self):
        state = produce(planned()); state = critique(state, [defect(current(state))])
        self.assertEqual(a.choose(state, NOW)["reason"], "feedback_handoff_pending")
        f = state["feedback"][0]
        with self.assertRaises(c.AdaptiveContractError):
            emit(state, "receive_feedback", {"receipt_id": f["receipt_id"], "target_ref": f["target_ref"], "recipient_role": "investigator"})
        state = handoff(state, "investigator")
        self.assertEqual(a.choose(state, NOW)["action"]["kind"], "investigate")

    def test_confirmed_repair_claim_requires_explicit_recheck(self):
        state = produce(planned()); state = critique(state, [defect(current(state))])
        state = investigate(state); state = handoff(state, "solver")
        self.assertEqual(a.choose(state, NOW)["action"]["kind"], "repair_obligation")
        state = produce(state, "repair_obligation")
        self.assertEqual(state["issues"][0]["disposition"], "addressed_claim")
        self.assertFalse(any(x["kind"] in c.GATES for x in a.legal_actions(state, NOW)["actions"]))
        state = investigate(state, "refuted")
        state = critique(state)
        state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        self.assertEqual(a.choose(state, NOW)["decision"], "complete")

    def test_refuted_criticism_does_not_force_repair(self):
        state = produce(planned()); state = critique(state, [defect(current(state))])
        state = investigate(state, "refuted")
        self.assertFalse(any(x["kind"] == "repair_obligation" for x in a.legal_actions(state, NOW)["actions"]))
        self.assertTrue(any(x["kind"] == "alignment_review" for x in a.legal_actions(state, NOW)["actions"]))

    def test_unresolved_math_is_not_transport_unknown(self):
        state = produce(planned()); state = critique(state, [defect(current(state))])
        state = investigate(state, "unresolved"); state = handoff(state, "investigator")
        self.assertEqual(a.budget_view(state)["unknown"], 0)
        self.assertEqual(a.choose(state, NOW)["action"]["kind"], "investigate")

    def test_repair_reserves_recheck_reintegration_and_two_new_gates(self):
        config = limits(global_call_limit=10, max_plan_revisions=1)
        state = planned(config, plan(branch=True)); state = produce(state, oid="left"); state = produce(state, oid="right")
        state = produce(state, "integrate"); state = critique(state, [defect(current(state, "left"))], "left")
        state = investigate(state); state = handoff(state, "solver")
        # Six calls used; repair+recheck+integrate+two gates needs five more.
        listing = a.legal_actions(state, NOW)
        self.assertTrue(any(x["action"]["kind"] == "repair_obligation" and x["reason"] == "completion_call_reserve" for x in listing["rejected"]))

    def test_new_leaf_invalidates_root_and_both_old_gates(self):
        state = planned(p=plan(branch=True)); state = produce(state, oid="left"); state = produce(state, oid="right")
        state = produce(state, "integrate")
        state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        state = critique(state, [defect(current(state, "left"))], "left")
        state = investigate(state); state = handoff(state, "solver"); state = produce(state, "repair_obligation", "left")
        self.assertFalse(a._fresh(state, current(state)))
        self.assertFalse(any(a._passed(state, g, current(state)) for g in c.GATES))
        state = investigate(state, "refuted"); state = produce(state, "integrate")
        self.assertEqual(current(state)["revision"], 2)
        self.assertFalse(any(a._passed(state, g, current(state)) for g in c.GATES))
        self.assertEqual(len(state["gates"]), 2)

    def test_two_leaf_defect_repair_reintegration_and_final_gates_complete(self):
        state = planned(p=plan(branch=True)); state = produce(state, oid="left"); state = produce(state, oid="right")
        state = produce(state, "integrate")
        state = critique(state, [defect(current(state, "left"))], "left")
        state = investigate(state); state = handoff(state, "solver")
        state = produce(state, "repair_obligation", "left"); state = investigate(state, "refuted")
        state = produce(state, "integrate"); state = critique(state)
        state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        decision = a.choose(state, NOW)
        self.assertEqual(decision["decision"], "complete")
        self.assertEqual(current(state)["revision"], 2)
        self.assertEqual(state["issues"][0]["repairs"], 1)
        self.assertEqual(state["issues"][0]["investigations"], 2)
        state = emit(state, "finalize", {k: decision[k] for k in ("decision", "reason")})
        self.assertFalse(state["endpoint"]["mathematical_truth_verified"])

    def test_exhausted_route_selects_existing_alternative_without_reset(self):
        config = limits(max_investigations_per_issue=1, max_plan_revisions=1)
        state = produce(planned(config, plan(routes=2)), route="route0")
        state = critique(state, [defect(current(state))]); state = investigate(state)
        decision = a.choose(state, NOW)
        self.assertEqual(decision["action"]["route_id"], "route1")
        before = len(state["actions"])
        state = produce(state, route="route1")
        self.assertEqual(len(state["actions"]), before + 1)
        self.assertEqual(state["issues"][0]["disposition"], "confirmed")
        state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        self.assertEqual(a.choose(state, NOW)["decision"], "complete")

    def test_repair_does_not_forget_other_unresolved_issue_on_old_version(self):
        state = produce(planned()); target = current(state)
        state = critique(state, [defect(target, "issue_1"), defect(target, "issue_2")])
        state = investigate(state, issue_id="issue_1"); state = handoff(state, "solver", "issue_1")
        state = produce(state, "repair_obligation")
        other = next(i for i in state["issues"] if i["proposal"]["issue_id"] == "issue_2")
        self.assertEqual(other["target_ref"]["revision"], 2)
        self.assertEqual(other["proposal"]["target_ref"]["revision"], 1)
        self.assertEqual(other["disposition"], "unresolved")

    def test_failed_investigation_consumes_issue_attempt(self):
        config = limits(max_investigations_per_issue=1, max_plan_revisions=1)
        state = produce(planned(config)); state = critique(state, [defect(current(state))])
        state = handoff(state, "investigator"); state = reply(reserve(state, "investigate"), None, status="failed")
        self.assertEqual(state["issues"][0]["investigations"], 1)
        self.assertFalse(any(x["kind"] == "investigate" for x in a.legal_actions(state, NOW)["actions"]))

    def test_failed_repair_consumes_issue_attempt_without_version_reset(self):
        config = limits(max_repairs_per_issue=1, max_plan_revisions=1)
        state = produce(planned(config)); state = critique(state, [defect(current(state))])
        state = investigate(state); state = handoff(state, "solver")
        state = reply(reserve(state, "repair_obligation"), None, status="failed")
        self.assertEqual(state["issues"][0]["repairs"], 1)
        self.assertEqual(current(state)["revision"], 1)
        self.assertFalse(any(x["kind"] == "repair_obligation" for x in a.legal_actions(state, NOW)["actions"]))

    def test_replan_allowed_when_existing_route_has_no_affordable_completion(self):
        config = limits(global_call_limit=5)
        p = plan(branch=True)
        p["routes"][0]["obligations"].insert(2, node("third"))
        p["routes"][0]["obligations"][-1]["depends_on"].append("third")
        state = planned(config, p)
        self.assertEqual(a.choose(state, NOW)["action"]["kind"], "replan")
        state = reply(reserve(state, "replan"), plan(revision=2))
        state = produce(state); state = gate(state, "alignment_review"); state = gate(state, "whole_proof_review")
        self.assertEqual(a.choose(state, NOW)["decision"], "complete")

    def test_replan_preserves_budget_and_invalidates_prior_candidates(self):
        state = produce(planned(limits(max_critiques_per_candidate=0)))
        state = gate(state, "alignment_review", "unknown", uncertainties=["INERT unresolved gate"])
        state = planning_handoff(state)
        state = reply(reserve(state, "replan"), plan(revision=2))
        self.assertEqual(len(state["actions"]), 4)
        self.assertEqual(a.candidate_statuses(state)[0]["status"], "stale_unverified")
        self.assertIsNone(a._root(state, "route0"))

    def test_replan_requires_diagnostic_release_then_planner_receive(self):
        state = gate(produce(planned(limits(max_critiques_per_candidate=0))), "alignment_review", "unknown", uncertainties=["INERT doubt"])
        self.assertEqual(a.choose(state, NOW)["decision"], "wait")
        source = a.planning_evidence_sources(state)[0]
        payload = {key: source[key] for key in ("receipt_id", "plan_ref", "target_ref")}
        state = emit(state, "release_planning_evidence", payload)
        self.assertFalse(any(p["kind"] == "replan" for p in a.legal_actions(state, NOW)["actions"]))
        state = emit(state, "receive_planning_evidence", payload | {"recipient_role": "planner"})
        self.assertEqual(a.choose(state, NOW)["action"]["planning_receipt_ids"], [source["receipt_id"]])
        self.assertTrue(all(not e["model_consumption_verified"] for e in state["planning_handoffs"][0]["handoff_events"]))

    def test_planning_handoff_rejects_wrong_source_plan_target_and_recipient(self):
        state = critique(produce(planned()))
        source = a.planning_evidence_sources(state)[0]
        payload = {key: source[key] for key in ("receipt_id", "plan_ref", "target_ref")}
        for key, value in (("receipt_id", "missing"), ("plan_ref", {"plan_id": "inert_plan", "revision": True}),
                           ("target_ref", dict(payload["target_ref"], revision=1.0))):
            with self.assertRaises(c.AdaptiveContractError): emit(state, "release_planning_evidence", payload | {key: value})
        with self.assertRaises(c.AdaptiveContractError): emit(state, "receive_planning_evidence", payload | {"recipient_role": "planner"})
        state = emit(state, "release_planning_evidence", payload)
        with self.assertRaises(c.AdaptiveContractError): emit(state, "receive_planning_evidence", payload | {"recipient_role": "solver"})

    def test_planning_sources_keep_normal_counterevidence_and_exclude_old_plan(self):
        state = critique(produce(planned()))
        state = gate(state, "alignment_review")
        state = gate(state, "whole_proof_review", "unknown", uncertainties=["INERT doubt"])
        sources = a.planning_evidence_sources(state)
        self.assertEqual([r["action_kind"] for r in sources], ["critique_candidate", "alignment_review", "whole_proof_review"])
        state = reply(reserve(planning_handoff(state), "replan"), plan(revision=2))
        self.assertEqual(a.planning_evidence_sources(state), [])
        self.assertEqual(len(state["planning_handoffs"]), 3)

    def test_planning_sources_keep_historical_targets_and_null_failed_target(self):
        state = planned(p=plan(branch=True)); state = produce(state, oid="left")
        state = critique(state, [], "left")
        state = reply(reserve(state, "solve_obligation", "right"), None, status="failed", host=False)
        sources = a.planning_evidence_sources(state)
        self.assertEqual([r["status"] for r in sources], ["completed", "failed"])
        self.assertIsNone(sources[1]["target_ref"]); self.assertIsNone(sources[1]["source"]["host_reference"])
        self.assertIsNone(sources[1]["projection_status"])
        state = planning_handoff(state)
        self.assertEqual(len(state["planning_handoffs"]), 2)

    def test_unknown_receipt_is_not_planning_evidence(self):
        state = reply(reserve(produce(planned()), "critique_candidate"), None, status="unknown")
        self.assertEqual(a.planning_evidence_sources(state), [])
        self.assertEqual(a.choose(state, NOW)["reason"], "recovery_required")

    def test_capacity_blocks_entire_required_planning_set_without_truncation(self):
        state = gate(produce(planned(limits(max_critiques_per_candidate=0))), "alignment_review", "unknown", uncertainties=["INERT doubt"])
        # Lower the tested bound only; source is still a genuine reducer receipt.
        with mock.patch.object(a, "PLANNING_EVIDENCE_MAX", 0):
            listing = a.legal_actions(state, NOW)
        self.assertFalse(listing["actions"])
        self.assertEqual(listing["rejected"][0]["reason"], "planning_evidence_capacity")
        self.assertEqual(len(listing["rejected"][0]["action"]["planning_receipt_ids"]), 1)

    def test_declared_dependency_assumption_conflict_blocks_projection(self):
        state = produce(planned(p=plan(branch=True)), oid="left")
        state = produce(state, oid="right"); state = reserve(state, "integrate")
        # Corrupt a frozen dependency after a valid plan and candidates were accepted.
        dependency_ref = state["actions"][-1]["proposal"]["dependency_refs"][0]
        a._resolve(state, dependency_ref)["assumptions"].append("INERT extra condition")
        with self.assertRaisesRegex(c.AdaptiveContractError, "frozen dependency scope is missing or incompatible"):
            reply(state, answer_candidate(state))

    def test_gate_wrong_version_rejected_and_pass_notes_judged_by_one_rule(self):
        def report(state):
            return {"schema": "victorique.adaptive_gate.v1", "gate_id": "alignment_review",
                    "problem_ref": {"artifact_id": PROBLEM["artifact_id"], "revision": 1},
                    "target_ref": state["actions"][-1]["proposal"]["target_ref"], "coverage": "whole_problem_and_proof",
                    "outcome": "pass", "findings": [], "uncertainties": []}
        for change in ("version", "outcome_type"):
            state = reserve(produce(planned()), "alignment_review")
            value = report(state)
            if change == "version": value["target_ref"] = dict(value["target_ref"], revision=2)
            else: value["outcome"] = []
            with self.subTest(change=change), self.assertRaises(c.AdaptiveContractError): reply(state, value)
        # A pass may list doubts or minor findings; c.gate_passes decides.
        state = reserve(produce(planned()), "alignment_review")
        value = report(state)
        value["uncertainties"] = ["INERT doubt"]
        state = reply(state, value)
        self.assertEqual(len(state["gates"]), 1)
        self.assertFalse(a._passed(state, "alignment_review", current(state)))
        state = reserve(produce(planned()), "alignment_review")
        value = report(state)
        value["findings"] = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "note_1",
                              "target_ref": value["target_ref"], "kind": "ambiguity", "severity": "minor",
                              "location": None, "claim": "INERT wording note"}]
        state = reply(state, value)
        self.assertTrue(a._passed(state, "alignment_review", current(state)))
        self.assertEqual(state["issues"], [])

    def test_direct_finalization_without_gates_rejected(self):
        state = produce(planned())
        with self.assertRaises(c.AdaptiveContractError): emit(state, "finalize", {"decision": "complete", "reason": "claimed"})

    def test_deadline_and_overspend_stop_without_dispatch(self):
        config = limits(stop_condition={"deadline_utc": NOW})
        state = a.create_state("inert_session", PROBLEM, config)
        self.assertEqual(a.choose(state, NOW)["reason"], "deadline_reached")
        state = a.create_state("inert_session", PROBLEM, limits())
        state = reply(reserve(state), plan(), cost=100)
        self.assertEqual(a.choose(state, NOW)["reason"], "observed_overspend")

    def test_stop_then_late_completion_keeps_endpoint_and_candidate_empty(self):
        state = reserve(planned(), "solve_obligation")
        state = emit(state, "stop", {"reason": "INERT requested stop"}); endpoint = deepcopy(state["endpoint"])
        state = reply(state, candidate(), cost=1)
        self.assertEqual(state["endpoint"], endpoint); self.assertEqual(state["candidates"], [])
        self.assertEqual(state["actions"][-1]["projection"], "late_audit_only")
        self.assertEqual(a.budget_view(state)["inflight"], 0)
        self.assertEqual(a.choose(state, NOW)["reason"], "endpoint_frozen")

    def test_action_result_and_event_type_bad_json_types_are_rejected(self):
        state = reserve(planned(), "solve_obligation")
        with self.assertRaises(c.AdaptiveContractError): reply(state, None, status=[])
        with self.assertRaises(c.AdaptiveContractError): emit(state, [], {})

    def test_unaffordable_completion_abstains(self):
        config = limits(global_call_limit=3)
        state = a.create_state("inert_session", PROBLEM, config)
        self.assertEqual(a.choose(state, NOW)["decision"], "abstain")


if __name__ == "__main__":
    unittest.main()
