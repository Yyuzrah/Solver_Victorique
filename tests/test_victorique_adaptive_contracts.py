"""INERT synthetic contracts; no model, mathematics, files, or network."""
from copy import deepcopy
import unittest

from victorique import adaptive_contracts as c


PROBLEM = {"artifact_id": "inert_problem", "revision": 1, "content": "INERT SOFTWARE FIXTURE. No mathematical task."}
NOW = "2026-11-12T05:00:00Z"


def limits(**changes):
    result = dict(max_routes=2, max_obligations_per_plan=64, max_dag_depth=32,
                  max_plan_revisions=2, max_candidate_revisions=8, max_repairs_per_issue=2,
                  max_investigations_per_issue=4, max_critiques_per_candidate=1,
                  global_call_limit=40, max_inflight=2, total_budget_units=40,
                  role_call_limits={role: 20 for role in c.ROLES},
                  role_budget_units={role: 40 for role in c.ROLES},
                  action_caps={kind: 1 for kind in c.CALLS}, action_roles=dict(c.DEFAULT_ROLES),
                  required_gate_caps={kind: 1 for kind in c.GATES}, stop_condition={"deadline_utc": None})
    result.update(changes)
    return result


def node(oid, dependencies=()):
    return {"obligation_id": oid, "statement": "INERT statement " + oid,
            "assumptions": ["INERT declared scope"], "depends_on": list(dependencies),
            "claimed_goal_coverage": ["INERT declared coverage"], "failure_conditions": ["INERT failure condition"]}


def plan(*, branch=False, revision=1, routes=1):
    return {"schema": "victorique.adaptive_plan.v1", "plan_id": "inert_plan", "revision": revision,
            "parent_revision": None if revision == 1 else revision - 1,
            "problem_ref": {"artifact_id": PROBLEM["artifact_id"], "revision": 1},
            "routes": [{"route_id": "route" + str(i), "root_obligation_id": "root",
                        "obligations": [node("left"), node("right"), node("root", ("left", "right"))]
                        if branch else [node("root")]} for i in range(routes)]}


def candidate(*, revision=1, oid="root", route="route0", plan_revision=1, dependencies=(), issues=(), content=None):
    return {"schema": "victorique.adaptive_candidate.v1", "artifact_id": f"artifact_{route}_{oid}_p{plan_revision}",
            "revision": revision, "parent_revision": None if revision == 1 else revision - 1,
            "plan_id": "inert_plan", "plan_revision": plan_revision, "route_id": route, "obligation_id": oid,
            "content": content or "INERT candidate text " + oid, "assumptions": ["INERT declared scope"],
            "dependency_refs": deepcopy(list(dependencies)), "addressed_issue_ids": list(issues)}


def defect(target, issue_id="issue_1", location=True):
    quote = target["content"].split()[0]
    return {"schema": "victorique.adaptive_defect.v1", "issue_id": issue_id,
            "target_ref": c.candidate_ref(target), "kind": "invalid_step", "severity": "material",
            "location": {"start_byte": 0, "end_byte": len(quote.encode("utf-8")), "quote": quote} if location else None,
            "claim": "INERT alleged defect, not a mathematical finding."}


def disposition(target, decision="confirmed", issue_id="issue_1"):
    return {"schema": "victorique.adaptive_disposition.v1", "issue_id": issue_id,
            "target_ref": c.candidate_ref(target), "decision": decision,
            "reason": "INERT disposition declaration.",
            "location": defect(target)["location"] if decision == "confirmed" else None}


class ContractTests(unittest.TestCase):
    def test_type_sensitive_json_identity_distinguishes_bool_int_float(self):
        for left, right in ((True, 1), (1, 1.0), ({"cap": 1}, {"cap": True}),
                            ([{"revision": 1}], [{"revision": 1.0}])):
            self.assertFalse(c.same_json(left, right))
        self.assertTrue(c.same_json({"a": [1, True, None]}, {"a": [1, True, None]}))

    def test_valid_plan_and_candidate_are_copied(self):
        p = plan(); got = c.validate_plan(p, PROBLEM, limits())
        self.assertEqual(got, p); self.assertIsNot(got, p)
        self.assertEqual(c.validate_candidate(candidate(), p, limits()), candidate())

    def test_strict_json_duplicate_nonfinite_depth_and_size(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '[' * 32 + '0' + ']' * 32, '"' + 'x' * 4_000_001 + '"'):
            with self.subTest(raw_length=len(raw)), self.assertRaises(c.AdaptiveContractError):
                c.parse_json(raw)

    def test_cycle_unknown_orphan_and_duplicate_obligation_rejected(self):
        bad = []
        p = plan(); p["routes"][0]["obligations"][0]["depends_on"] = ["root"]; bad.append(p)
        p = plan(); p["routes"][0]["obligations"][0]["depends_on"] = ["missing"]; bad.append(p)
        p = plan(); p["routes"][0]["obligations"].append(node("orphan")); bad.append(p)
        p = plan(); p["routes"][0]["obligations"].append(node("root")); bad.append(p)
        for p in bad:
            with self.subTest(p=p), self.assertRaises(c.AdaptiveContractError):
                c.validate_plan(p, PROBLEM, limits())

    def test_shared_diamond_and_dense_dag_have_bounded_height(self):
        p = plan(); route = p["routes"][0]
        route["obligations"] = [node("leaf"), node("left", ["leaf"]), node("right", ["leaf"]), node("root", ["left", "right"])]
        self.assertEqual(c.validate_plan(p, PROBLEM, limits(max_dag_depth=3)), p)
        with self.assertRaises(c.AdaptiveContractError):
            c.validate_plan(p, PROBLEM, limits(max_dag_depth=2))
        # This DAG has exponentially many paths but only 32 vertices/496 edges.
        route["root_obligation_id"] = "n31"
        route["obligations"] = [node("n" + str(i), ["n" + str(j) for j in range(i)]) for i in range(32)]
        self.assertEqual(c.validate_plan(p, PROBLEM, limits()), p)
        with self.assertRaises(c.AdaptiveContractError):
            c.validate_plan(p, PROBLEM, limits(max_dag_depth=31))

    def test_wrong_problem_plan_parent_and_bounds(self):
        p = plan(); p["problem_ref"]["revision"] = 2
        with self.assertRaises(c.AdaptiveContractError): c.validate_plan(p, PROBLEM, limits())
        with self.assertRaises(c.AdaptiveContractError): c.validate_plan(plan(revision=2), PROBLEM, limits())
        self.assertEqual(c.validate_plan(plan(revision=2), PROBLEM, limits(), plan())["revision"], 2)
        with self.assertRaises(c.AdaptiveContractError): c.validate_plan(plan(branch=True), PROBLEM, limits(max_obligations_per_plan=2))

    def test_plan_dependency_scope_multilevel_and_route_local(self):
        p = plan(routes=2)
        for index, route in enumerate(p["routes"]):
            leaf = node("leaf")
            middle = node("middle", ["leaf"])
            root = node("root", ["middle", "leaf"])
            scope = f"INERT route {index} scope"
            leaf["assumptions"] = [scope]
            middle["assumptions"] = [scope, "INERT middle condition"]
            root["assumptions"] = ["INERT root condition", "INERT middle condition", scope]
            # Forward references and distinct scopes for the same IDs in each route.
            route["obligations"] = [root, middle, leaf]
        self.assertEqual(c.validate_plan(p, PROBLEM, limits()), p)

    def test_plan_dependency_scope_accepts_empty_and_added_consumer_assumptions(self):
        for dependency_scope, consumer_scope in (([], []), ([], ["INERT extra"]),
                                                  (["INERT base"], ["INERT extra", "INERT base"])):
            p = plan()
            dependency, consumer = node("leaf"), node("root", ["leaf"])
            dependency["assumptions"] = dependency_scope
            consumer["assumptions"] = consumer_scope
            p["routes"][0]["obligations"] = [dependency, consumer]
            with self.subTest(dependency=dependency_scope, consumer=consumer_scope):
                self.assertEqual(c.validate_plan(p, PROBLEM, limits()), p)

    def test_initial_plan_and_replan_reject_missing_or_rephrased_dependency_scope(self):
        for prior in (None, plan()):
            for consumer_scope in ([], ["INERT unrelated condition"],
                                   ["INERT the declared scope"], ["INERT declared scope "]):
                p = plan(branch=True, revision=1 if prior is None else 2)
                p["routes"][0]["obligations"][-1]["assumptions"] = consumer_scope
                with self.subTest(revision=p["revision"], scope=consumer_scope), self.assertRaisesRegex(
                        c.AdaptiveContractError,
                        "route=route0, consumer=root, dependency=left; dependency assumptions must be a subset"):
                    c.validate_plan(p, PROBLEM, limits(), prior=prior)

    def test_plan_dependency_scope_checks_intermediate_edges(self):
        p = plan()
        leaf, middle, root = node("leaf"), node("middle", ["leaf"]), node("root", ["middle"])
        leaf["assumptions"].append("INERT extra condition")
        root["assumptions"].append("INERT extra condition")
        p["routes"][0]["obligations"] = [root, middle, leaf]
        with self.assertRaisesRegex(c.AdaptiveContractError, "route=route0, consumer=middle, dependency=leaf"):
            c.validate_plan(p, PROBLEM, limits())

    def test_plan_dependency_scope_cannot_borrow_assumptions_from_another_route(self):
        p = plan(branch=True, routes=2)
        p["routes"][0]["obligations"][-1]["assumptions"].append("INERT extra condition")
        p["routes"][1]["obligations"][0]["assumptions"].append("INERT extra condition")
        with self.assertRaisesRegex(c.AdaptiveContractError, "route=route1, consumer=root, dependency=left"):
            c.validate_plan(p, PROBLEM, limits())

    def test_plan_graph_errors_precede_dependency_scope_errors(self):
        for dependencies in (["missing"], ["root"]):
            p = plan(branch=True, routes=2)
            p["routes"][0]["obligations"][-1]["assumptions"] = []
            p["routes"][1]["obligations"][-1]["depends_on"] = dependencies
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(
                    c.AdaptiveContractError, "^unknown or cyclic obligation$"):
                c.validate_plan(p, PROBLEM, limits())

    def test_plan_dependency_nodes_cannot_come_from_another_route(self):
        p = plan(branch=True, routes=2)
        p["routes"][0]["obligations"].pop(0)
        with self.assertRaisesRegex(c.AdaptiveContractError, "^unknown or cyclic obligation$"):
            c.validate_plan(p, PROBLEM, limits())

    def test_first_candidate_cannot_have_parent_zero(self):
        for parent in (0, 1, False):
            v = candidate(); v["parent_revision"] = parent
            with self.subTest(parent=parent), self.assertRaises(c.AdaptiveContractError):
                c.validate_candidate(v, plan(), limits())

    def test_dependency_scope_duplicates_and_revision(self):
        p = plan(branch=True)
        refs = [c.candidate_ref(candidate(oid=oid)) for oid in ("left", "right")]
        self.assertEqual(c.validate_candidate(candidate(dependencies=refs), p, limits())["dependency_refs"], refs)
        for change in ("wrong_route", "wrong_version", "duplicate"):
            modified = deepcopy(refs)
            if change == "wrong_route": modified[0]["route_id"] = "other"
            elif change == "wrong_version": modified[0]["plan_revision"] = 2
            else: modified[1] = modified[0]
            with self.subTest(change=change), self.assertRaises(c.AdaptiveContractError):
                c.validate_candidate(candidate(dependencies=modified), p, limits())

    def test_utf8_location_exact_bytes_and_unknown_location(self):
        target = candidate(content="惯性 INERT")
        v = defect(target); self.assertEqual(c.validate_defect(v, target), v)
        v["location"]["end_byte"] = 2
        with self.assertRaises(c.AdaptiveContractError): c.validate_defect(v, target)
        self.assertIsNone(c.validate_defect(defect(target, location=False), target)["location"])

    def test_defect_and_disposition_wrong_target_rejected(self):
        target = candidate(); v = defect(target); v["target_ref"]["revision"] = 2
        with self.assertRaises(c.AdaptiveContractError): c.validate_defect(v, target)
        v = disposition(target); v["location"] = None
        with self.assertRaises(c.AdaptiveContractError): c.validate_disposition(v, "issue_1", target)
        self.assertEqual(c.validate_disposition(disposition(target, "refuted"), "issue_1", target)["decision"], "refuted")

    def test_enum_container_types_are_contract_errors(self):
        target = candidate()
        for field in ("kind", "severity"):
            for value in ([], {}, True):
                v = defect(target); v[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(c.AdaptiveContractError):
                    c.validate_defect(v, target)
        for value in ([], {}, True):
            v = disposition(target); v["decision"] = value
            with self.assertRaises(c.AdaptiveContractError): c.validate_disposition(v, "issue_1", target)

    def test_limits_reviewer_alias_is_explicit_gate_caps_exact(self):
        v = limits(); v["action_roles"]["investigate"] = "proof_reviewer"
        self.assertEqual(c.validate_limits(v), v)
        v["required_gate_caps"]["whole_proof_review"] = 2
        with self.assertRaises(c.AdaptiveContractError): c.validate_limits(v)
        v = limits(); v["action_roles"]["whole_proof_review"] = "solver"
        with self.assertRaises(c.AdaptiveContractError): c.validate_limits(v)

    def test_semantic_correctness_is_not_certified_by_structure(self):
        p = plan(); p["routes"][0]["obligations"][0]["statement"] = "INERT deliberately inadequate model proposal"
        result = c.validate_plan(p, PROBLEM, limits())
        self.assertNotIn("accepted_fact", result)
        self.assertNotIn("mathematical_truth_verified", result)


if __name__ == "__main__":
    unittest.main()
