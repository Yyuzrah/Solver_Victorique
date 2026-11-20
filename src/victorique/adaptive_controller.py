"""Pure advisory reducer and deterministic bounded adaptive policy.

State and receipts are caller-supplied declarations, never authentication.
This module performs no dispatch, I/O, mathematics, or external cancellation.
Only states returned by create_state/reduce are supported. Loading untrusted
snapshots requires the durable session's event replay and relationship auditing, not _state alone.
"""
from . import adaptive_contracts as c
from . import seeded_repair as seed


SCHEMA = "victorique.adaptive_state.v2"
PLANNING_EVIDENCE_MAX = 64
MODIFY = {"solve_obligation", "repair_obligation", "integrate"}
STATE_FIELDS = {"schema", "session_id", "state_revision", "problem", "limits", "plan", "active_route",
                "candidates", "issues", "actions", "gates", "feedback", "planning_handoffs", "events", "endpoint",
                "mathematical_truth_verified", "dispatch_authorized", "provenance_authenticated"}


def create_state(session_id, problem, limits):
    c.identifier(session_id, "session ID")
    return {"schema": SCHEMA, "session_id": session_id, "state_revision": 0,
            "problem": c.validate_problem(problem), "limits": c.validate_limits(limits), "plan": None,
            "active_route": None, "candidates": [], "issues": [], "actions": [], "gates": [],
            "feedback": [], "planning_handoffs": [], "events": [], "endpoint": None, "mathematical_truth_verified": False,
            "dispatch_authorized": False, "provenance_authenticated": False}


def create_seeded_state(session_id, problem, limits, initial_input):
    state = create_state(session_id, problem, seed.validate_limits(limits))
    state['schema'] = seed.STATE_SCHEMA
    state['initial_input'] = seed.validate_input(initial_input, state['problem'])
    return state


def _state(value):
    value = c.copy_json(value)
    seeded = seed.is_state(value)
    c.fields(value, STATE_FIELDS | ({'initial_input'} if seeded else set()), "adaptive state")
    c.need(value["schema"] in (SCHEMA, seed.STATE_SCHEMA) and value["mathematical_truth_verified"] is False
           and value["dispatch_authorized"] is False and value["provenance_authenticated"] is False,
           "unsupported or promoted adaptive state")
    c.identifier(value["session_id"])
    c.integer(value["state_revision"], "state revision", 0, 10000)
    c.need(value["state_revision"] == len(value["events"]), "state event sequence differs")
    c.validate_problem(value["problem"]); c.validate_limits(value["limits"])
    if seeded:
        seed.validate_input(value['initial_input'], value['problem']); seed.validate_limits(value['limits'])
    return value


def _heads(state):
    result = {}
    if state["plan"] is None:
        return result
    for entry in state["candidates"]:
        item = entry["artifact"]
        if c.same_json([item["plan_id"], item["plan_revision"]], [state["plan"]["plan_id"], state["plan"]["revision"]]):
            key = (item["route_id"], item["obligation_id"])
            if key not in result or result[key]["revision"] < item["revision"]:
                result[key] = item
    return result


def _resolve(state, ref):
    c.reference(ref)
    return next((x["artifact"] for x in state["candidates"] if c.same_json(c.candidate_ref(x["artifact"]), ref)), None)


def _fresh(state, item, heads=None, seen=None, memo=None):
    heads = _heads(state) if heads is None else heads
    memo = {} if memo is None else memo
    if item is None:
        return False
    key = (item["route_id"], item["obligation_id"])
    if key not in heads or not c.same_json(c.candidate_ref(heads[key]), c.candidate_ref(item)):
        return False
    seen = set() if seen is None else seen
    marker = (item["artifact_id"], item["revision"])
    if marker in memo:
        return memo[marker]
    if marker in seen:
        return False
    seen = seen | {marker}
    memo[marker] = all(_fresh(state, _resolve(state, ref), heads, seen, memo) for ref in item["dependency_refs"])
    return memo[marker]


def candidate_statuses(state):
    state = _state(state); heads = _heads(state)
    return [{"reference": c.candidate_ref(x["artifact"]),
             "status": "candidate_unverified" if _fresh(state, x["artifact"], heads) else "stale_unverified",
             "mathematical_truth_verified": False} for x in state["candidates"]]


def _route(state, route_id):
    return next(r for r in state["plan"]["routes"] if r["route_id"] == route_id)


def _root(state, route_id):
    route = _route(state, route_id)
    return _heads(state).get((route_id, route["root_obligation_id"]))


def _ancestors(state, route_id, oid):
    graph = c.nodes(state["plan"])
    changed = {oid}
    while True:
        added = {key[1] for key, node in graph.items() if key[0] == route_id and set(node["depends_on"]) & changed}
        new = changed | added
        if new == changed:
            return changed - {oid}
        changed = new


def _current_issues(state, route_id):
    return [issue for issue in state["issues"]
            if issue["target_ref"]["route_id"] == route_id
            and _fresh(state, _resolve(state, issue["target_ref"]))]


def _passed(state, gate, target):
    rule = c.minor_findings_rule(state["limits"])
    return target is not None and _fresh(state, target) and any(
        row["report"]["gate_id"] == gate and c.same_json(row["report"]["target_ref"], c.candidate_ref(target))
        and c.gate_passes(row["report"], rule) for row in state["gates"])


def _gate_attempted(state, kind, target):
    # A call that stopped at its time limit and is sent again is not an attempt.
    return any(a["proposal"]["kind"] == kind and c.same_json(a["proposal"]["target_ref"], c.candidate_ref(target))
               and a["status"] != "timed_out" for a in state["actions"])


def planning_evidence_sources(state):
    """Current-plan diagnostic receipts, including normal review counterevidence."""
    state = _state(state)
    if state["plan"] is None:
        return []
    current_plan = {"plan_id": state["plan"]["plan_id"], "revision": state["plan"]["revision"]}
    rows = []
    for action in state["actions"]:
        proposal = action["proposal"]
        if not c.same_json(proposal["plan_ref"], current_plan) or action["receipt"] is None:
            continue
        if action["status"] == "failed":
            target = proposal["target_ref"]
        elif (action["status"] == "completed" and proposal["kind"] in {"critique_candidate", "investigate", *c.GATES}
              and action["projection"] in {"declared_receipt_projected", "stale_audit_only"}):
            target = action["response"]["target_ref"]
        else:
            continue
        rows.append({"receipt_id": action["receipt"]["receipt_id"], "action_id": action["action_id"],
                     "plan_ref": current_plan, "action_kind": proposal["kind"], "status": action["status"],
                     "target_ref": target, "projection_status": action["projection"], "source": action["receipt"]})
    return c.copy_json(sorted(rows, key=lambda row: (row["source"]["sequence"], row["receipt_id"])))


def _planning_handoff_reason(state, proposal):
    if proposal["kind"] != "replan":
        return None
    if len(proposal["planning_receipt_ids"]) > PLANNING_EVIDENCE_MAX:
        return "planning_evidence_capacity"
    for receipt in proposal["planning_receipt_ids"]:
        row = next((r for r in state["planning_handoffs"] if r["receipt_id"] == receipt
                    and c.same_json(r["plan_ref"], proposal["plan_ref"])), None)
        if row is None or not row["released"] or "planner" not in row["received_by"]:
            return "planning_evidence_not_released_and_received"
    return None


def _proposal(state, kind, route_id=None, oid=None, issue_id=None):
    plan = state["plan"]; heads = _heads(state)
    target = heads.get((route_id, oid))
    refs = []
    if plan is not None and oid is not None:
        node = c.nodes(plan)[(route_id, oid)]
        refs = [c.candidate_ref(heads[(route_id, dep)]) for dep in node["depends_on"] if (route_id, dep) in heads]
    affected = sorted(_ancestors(state, route_id, oid)) if kind in MODIFY else []
    feedback_ids = []
    if issue_id is not None:
        issue = next(i for i in state["issues"] if i["proposal"]["issue_id"] == issue_id)
        feedback_ids = [issue["current_feedback_receipt_id"]]
    return {"state_revision": state["state_revision"], "kind": kind,
            "role": state["limits"]["action_roles"][kind],
            "plan_ref": None if plan is None else {"plan_id": plan["plan_id"], "revision": plan["revision"]},
            "route_id": route_id, "obligation_id": oid,
            "target_ref": None if target is None else c.candidate_ref(target),
            "dependency_refs": refs, "issue_id": issue_id, "feedback_receipt_ids": feedback_ids,
            "planning_receipt_ids": [r["receipt_id"] for r in planning_evidence_sources(state)] if kind == "replan" else [],
            "predicted_invalidated_obligations": affected,
            "cap_units": state["limits"]["action_caps"][kind], "dispatch_authorized": False}


def budget_view(state):
    state = _state(state); limits = state["limits"]
    roles = {role: {"calls": 0, "committed_units": 0} for role in c.ROLES}
    total = 0
    for action in state["actions"]:
        used = action["cost_units"] if action["cost_units"] is not None else action["proposal"]["cap_units"]
        if action["status"] in {"reserved", "unknown"}:
            used = max(action["proposal"]["cap_units"], used)
        role = action["proposal"]["role"]
        roles[role]["calls"] += 1; roles[role]["committed_units"] += used; total += used
    request_cap_exceeded = any(row["cost_units"] is not None and row["cost_units"] > row["proposal"]["cap_units"] for row in state["actions"])
    return {"reserved_calls": len(state["actions"]), "committed_units": total,
            "inflight": sum(a["status"] in {"reserved", "unknown"} for a in state["actions"]),
            "unknown": sum(a["status"] == "unknown" for a in state["actions"]),
            "roles": roles, "actual_cost_units": None if any(a["cost_units"] is None for a in state["actions"])
            else sum(a["cost_units"] for a in state["actions"]),
            "request_cap_exceeded": request_cap_exceeded,
            "over_budget": request_cap_exceeded or total > limits["total_budget_units"] or any(
                roles[role]["committed_units"] > limits["role_budget_units"][role] for role in c.ROLES)}


def _minimum_suffix(state, action):
    """Conservative structural completion reserve, never semantic success."""
    kind = action["kind"]
    if kind in c.GATES:
        return (["whole_proof_review"] if kind == "alignment_review" else []), None
    if kind in {"propose_plan", "replan"}:
        return ["solve_obligation", *c.GATES], None
    route_id = action["route_id"]; heads = _heads(state)
    suffix = []
    pending = [row["proposal"] for row in state["actions"] if row["status"] == "reserved"
               and row["proposal"]["route_id"] == route_id
               and c.same_json(row["proposal"]["plan_ref"], action["plan_ref"])
               and _stale_reason(state, row) is None]
    modifiers = [p for p in pending if p["kind"] in MODIFY] + ([action] if kind in MODIFY else [])
    changed = {oid for p in modifiers for oid in [p["obligation_id"], *p["predicted_invalidated_obligations"]]}
    issues = [i for i in state["issues"] if i["target_ref"]["route_id"] == route_id
              and c.same_json(i["target_ref"]["plan_id"], action["plan_ref"]["plan_id"])
              and c.same_json(i["target_ref"]["plan_revision"], action["plan_ref"]["revision"])
              and i["disposition"] != "refuted"]
    # Known confirmed defects require a changed candidate before completion.
    # One repair may address a shared target; every remaining issue still gets
    # an explicit disposition against the eventual version.
    repair_nodes = set()
    for issue in issues:
        oid = issue["target_ref"]["obligation_id"]
        if issue["disposition"] == "confirmed" and oid not in changed:
            eligible = [i for i in issues if i["target_ref"]["obligation_id"] == oid
                        and i["disposition"] == "confirmed"
                        and i["repairs"] < state["limits"]["max_repairs_per_issue"]]
            target = heads.get((route_id, oid))
            if not eligible or target is None or target["revision"] >= state["limits"]["max_candidate_revisions"]:
                return [], "completion_known_defect_repair_limit"
            repair_nodes.add(oid)
            changed.add(oid); changed.update(_ancestors(state, route_id, oid))
    suffix.extend("repair_obligation" for _ in sorted(repair_nodes))
    # Do not credit pending work whose frozen inputs will be invalidated by
    # this action or a known required repair before it can cover the final DAG.
    pending_nodes = set()
    for p in pending:
        if p["kind"] in MODIFY and not any(ref["obligation_id"] in changed for ref in p["dependency_refs"]):
            pending_nodes.add(p["obligation_id"])
    for node in _route(state, route_id)["obligations"]:
        oid = node["obligation_id"]
        if kind in MODIFY and oid == action["obligation_id"]:
            continue
        if oid in pending_nodes or oid in repair_nodes:
            continue
        if oid in changed or not _fresh(state, heads.get((route_id, oid)), heads):
            suffix.append("integrate" if node["depends_on"] else "solve_obligation")
    for issue in issues:
        issue_id = issue["proposal"]["issue_id"]
        changed_target = issue["target_ref"]["obligation_id"] in changed
        already_reserved = (not changed_target and any(p["kind"] == "investigate"
            and p["issue_id"] == issue_id and c.same_json(p["target_ref"], issue["target_ref"]) for p in pending))
        current_covers = (not changed_target and kind == "investigate" and action["issue_id"] == issue_id
                          and c.same_json(action["target_ref"], issue["target_ref"]))
        future = 0 if already_reserved or current_covers else 1
        current_attempt = int(kind == "investigate" and action["issue_id"] == issue_id)
        if issue["investigations"] + current_attempt + future > state["limits"]["max_investigations_per_issue"]:
            return [], "completion_issue_disposition_limit"
        suffix.extend(["investigate"] * future)
    root = _root(state, route_id)
    suffix.extend(g for g in c.GATES if changed or not _passed(state, g, root))
    return suffix, None


def _budget_reason(state, action):
    limits = state["limits"]; budget = budget_view(state)
    if budget["inflight"] >= limits["max_inflight"]:
        return "max_inflight"
    suffix, blocked = _minimum_suffix(state, action)
    if blocked is not None:
        return blocked
    future = [action["kind"], *suffix]
    if budget["reserved_calls"] + len(future) > limits["global_call_limit"]:
        return "completion_call_reserve"
    if budget["committed_units"] + sum(limits["action_caps"][kind] for kind in future) > limits["total_budget_units"]:
        return "completion_fee_reserve"
    for role in c.ROLES:
        kinds = [kind for kind in future if limits["action_roles"][kind] == role]
        if budget["roles"][role]["calls"] + len(kinds) > limits["role_call_limits"][role]:
            return "completion_role_call_reserve:" + role
        if budget["roles"][role]["committed_units"] + sum(limits["action_caps"][kind] for kind in kinds) > limits["role_budget_units"][role]:
            return "completion_role_fee_reserve:" + role
    return None


def _candidates_for_route(state, route_id):
    heads = _heads(state); result = []; limits = state["limits"]
    issues = _current_issues(state, route_id)
    for issue in issues:
        target = _resolve(state, issue["target_ref"])
        if issue["disposition"] in {"proposed", "unresolved", "addressed_claim"}:
            if issue["investigations"] < limits["max_investigations_per_issue"]:
                result.append((0, _proposal(state, "investigate", route_id, target["obligation_id"], issue["proposal"]["issue_id"])))
        elif (issue["disposition"] == "confirmed" and issue["repairs"] < limits["max_repairs_per_issue"]
              and issue["investigations"] < limits["max_investigations_per_issue"]):
            if target["revision"] < limits["max_candidate_revisions"]:
                result.append((1, _proposal(state, "repair_obligation", route_id, target["obligation_id"], issue["proposal"]["issue_id"])))
    for node in _route(state, route_id)["obligations"]:
        oid = node["obligation_id"]; item = heads.get((route_id, oid))
        if _fresh(state, item, heads):
            root_oid = _route(state, route_id)["root_obligation_id"]
            if oid != root_oid:
                count = sum(a["proposal"]["kind"] == "critique_candidate" and a["status"] != "timed_out"
                            and c.same_json(a["proposal"]["target_ref"], c.candidate_ref(item)) for a in state["actions"])
                if count < limits["max_critiques_per_candidate"]:
                    result.append((3, _proposal(state, "critique_candidate", route_id, oid)))
            continue
        if item is not None and item["revision"] >= limits["max_candidate_revisions"]:
            continue
        if not all(_fresh(state, heads.get((route_id, dep)), heads) for dep in node["depends_on"]):
            continue
        result.append((2 if not node["depends_on"] else 4,
                       _proposal(state, "integrate" if node["depends_on"] else "solve_obligation", route_id, oid)))
    root = _root(state, route_id)
    if _fresh(state, root, heads):
        root_ref = c.candidate_ref(root)
        critiques = sum(a["proposal"]["kind"] == "critique_candidate" and a["status"] != "timed_out"
                        and c.same_json(a["proposal"]["target_ref"], root_ref) for a in state["actions"])
        if critiques < limits["max_critiques_per_candidate"]:
            result.append((5, _proposal(state, "critique_candidate", route_id, root["obligation_id"])))
        unresolved = [i for i in issues if i["disposition"] != "refuted"]
        if not unresolved:
            if not _passed(state, "alignment_review", root) and not _gate_attempted(state, "alignment_review", root):
                result.append((6, _proposal(state, "alignment_review", route_id, root["obligation_id"])))
            elif _passed(state, "alignment_review", root) and not _passed(state, "whole_proof_review", root) and not _gate_attempted(state, "whole_proof_review", root):
                result.append((7, _proposal(state, "whole_proof_review", route_id, root["obligation_id"])))
    return result


def _same_work(left, right):
    keys = ("kind", "plan_ref", "route_id", "obligation_id", "target_ref", "issue_id")
    return all(c.same_json(left[k], right[k]) for k in keys)


def legal_actions(state, now_utc):
    state = _state(state); now = c.utc(now_utc)
    c.need(not state["events"] or now >= c.utc(state["events"][-1]["recorded_at_utc"]), "decision timestamp moved backwards")
    budget = budget_view(state); limits = state["limits"]
    result = {"actions": [], "rejected": [], "budget": budget, "blocked_reason": None,
              "dispatch_authorized": False}
    if state["endpoint"] is not None:
        result["blocked_reason"] = "endpoint_frozen"; return result
    if limits["stop_condition"]["deadline_utc"] is not None and now >= c.utc(limits["stop_condition"]["deadline_utc"]):
        result["blocked_reason"] = "deadline_reached"; return result
    if budget["over_budget"]:
        result["blocked_reason"] = "observed_overspend"; return result
    if budget["unknown"]:
        result["blocked_reason"] = "recovery_required"; return result
    if seed.is_state(state) and state['plan'] is None:
        result['blocked_reason'] = 'seed_initialization_required'; return result
    if state["plan"] is None:
        candidates = [(0, _proposal(state, "propose_plan"))]
    else:
        routes = sorted(state["plan"]["routes"], key=lambda r: (r["route_id"] != state["active_route"], r["route_id"]))
        candidates = []
        for index, route in enumerate(routes):
            candidates.extend((index * 10 + priority, action) for priority, action in _candidates_for_route(state, route["route_id"]))
        if not candidates and budget["inflight"] == 0 and state["plan"]["revision"] < limits["max_plan_revisions"]:
            candidates.append((1000, _proposal(state, "replan")))
    ranked = []
    for priority, action in candidates:
        conflict = any(a["status"] in {"reserved", "unknown"} and (
            _same_work(a["proposal"], action)
            or (a["proposal"]["kind"] in MODIFY and action["kind"] in MODIFY
                and (a["proposal"]["route_id"], a["proposal"]["obligation_id"]) == (action["route_id"], action["obligation_id"])))
            for a in state["actions"])
        missing_feedback = any(not any(f["receipt_id"] == rid and c.same_json(f["target_ref"], action["target_ref"])
                                      and f["released"] and action["role"] in f["received_by"] for f in state["feedback"])
                               for rid in action["feedback_receipt_ids"])
        seed_barrier = seed.is_state(state) and not seed.critique_ready(state) and action['kind'] != 'critique_candidate'
        reason = ('initial_seed_critique_required' if seed_barrier else "work_already_inflight" if conflict else "feedback_not_released_and_received" if missing_feedback
                  else _planning_handoff_reason(state, action) or _budget_reason(state, action))
        if reason is not None:
            result["rejected"].append({"action": action, "reason": reason})
        else:
            ranked.append((priority, len(action["predicted_invalidated_obligations"]), action["cap_units"],
                           action["route_id"] or "", action["obligation_id"] or "", action["issue_id"] or "", action))
    result["actions"] = [row[-1] for row in sorted(ranked, key=lambda row: row[:-1])]
    # An oversized current route is not evidence that a smaller replan is illegal.
    if (not result["actions"] and budget["inflight"] == 0 and state["plan"] is not None
            and state["plan"]["revision"] < limits["max_plan_revisions"]
            and not any(row["reason"] == "feedback_not_released_and_received" for row in result["rejected"])
            and not any(row["action"]["kind"] == "replan" for row in result["rejected"])):
        replan = _proposal(state, "replan")
        reason = _planning_handoff_reason(state, replan) or _budget_reason(state, replan)
        if reason is None:
            result["actions"].append(replan)
        else:
            result["rejected"].append({"action": replan, "reason": reason})
    return result


def choose(state, now_utc):
    state = _state(state); listing = legal_actions(state, now_utc)
    result = {"schema": "victorique.adaptive_decision.v1", "state_revision": state["state_revision"],
              "policy_version": seed.CONTROLLER_VERSION if seed.is_state(state) else "bounded_rules_v2", "decision": None, "action": None,
              "reason": None, "legal_actions": listing["actions"], "rejected_actions": listing["rejected"],
              "dispatch_authorized": False, "mathematical_truth_verified": False}
    blocked = listing["blocked_reason"]
    if blocked is not None:
        result.update(decision="wait" if blocked == "recovery_required" else "stop", reason=blocked)
        return result
    if listing["budget"]["inflight"] == 0 and state["plan"] is not None:
        for route in state["plan"]["routes"]:
            root = _root(state, route["route_id"])
            if all(_passed(state, gate, root) for gate in c.GATES) and all(
                    i["disposition"] == "refuted" for i in _current_issues(state, route["route_id"])):
                result.update(decision="complete", reason="current_candidate_has_two_declared_cold_gate_receipts")
                return result
    if listing["actions"]:
        result.update(decision="propose", action=listing["actions"][0], reason="deterministic_bounded_rule")
    elif listing["budget"]["inflight"]:
        result.update(decision="wait", reason="work_inflight")
    elif any(r["reason"] in {"feedback_not_released_and_received", "planning_evidence_not_released_and_received"} for r in listing["rejected"]):
        result.update(decision="wait", reason="feedback_handoff_pending")
    else:
        result.update(decision="abstain", reason="no_legal_bounded_completion_action")
    return result


def _source(payload, event):
    return {"receipt_id": payload["receipt_id"], "host_reference": payload["host_reference"],
            "event_id": event["event_id"], "sequence": event["expected_state_revision"] + 1,
            "source_event_id": payload["source_event_id"], "source_line": payload["source_line"],
            "recorded_at_utc": event["recorded_at_utc"], "authenticated": False}


def _frozen_plan(state, plan_ref):
    if plan_ref is None:
        return None
    c.fields(plan_ref, {"plan_id", "revision"}, "action plan reference")
    c.identifier(plan_ref["plan_id"]); c.integer(plan_ref["revision"], "action plan revision", 1, 64)
    for value in [state["plan"], *(a["response"] for a in state["actions"]
                                  if a["proposal"]["kind"] in {"propose_plan", "replan"} and a["status"] == "completed")]:
        if value is not None and c.same_json({"plan_id": value["plan_id"], "revision": value["revision"]}, plan_ref):
            return value
    raise c.AdaptiveContractError("action frozen plan is missing")


def _validate_bound_response(state, action, value):
    """Validate against the immutable original action, before any stale test."""
    proposal = action["proposal"]; kind = proposal["kind"]
    value = c.copy_json(value)
    if kind in {"propose_plan", "replan"}:
        return c.validate_plan(value, state["problem"], state["limits"], _frozen_plan(state, proposal["plan_ref"]))
    target = None if proposal["target_ref"] is None else _resolve(state, proposal["target_ref"])
    c.need(proposal["target_ref"] is None or target is not None, "action frozen candidate is missing")
    if kind in MODIFY:
        plan = _frozen_plan(state, proposal["plan_ref"])
        value = c.validate_candidate(value, plan, state["limits"])
        c.need(c.same_json([value["route_id"], value["obligation_id"]], [proposal["route_id"], proposal["obligation_id"]]), "candidate target differs from action")
        c.need(c.same_json(value["revision"], 1 if target is None else target["revision"] + 1), "candidate revision differs from action")
        c.need(target is None or c.same_json(value["artifact_id"], target["artifact_id"]), "candidate lineage differs from action")
        c.need(c.same_json(value["dependency_refs"], proposal["dependency_refs"]), "candidate dependencies differ from frozen action")
        for ref in value["dependency_refs"]:
            dependency = _resolve(state, ref)
            c.need(dependency is not None and set(dependency["assumptions"]).issubset(set(value["assumptions"])), "frozen dependency scope is missing or incompatible")
        c.need(c.same_json(value["addressed_issue_ids"], [] if kind != "repair_obligation" else [proposal["issue_id"]]), "candidate repair claim differs from action")
        return value
    c.need(target is not None, "response requires frozen candidate")
    if kind == "investigate":
        return c.validate_disposition(value, proposal["issue_id"], target)
    if kind == "critique_candidate":
        c.fields(value, {"schema", "target_ref", "findings", "no_gap_identified"}, "critique")
        c.reference(value["target_ref"])
        c.need(value["schema"] == "victorique.adaptive_critique.v1"
               and c.same_json(value["target_ref"], proposal["target_ref"]), "critique binding differs")
        c.need(type(value["no_gap_identified"]) is bool and type(value["findings"]) is list
               and value["no_gap_identified"] == (not value["findings"]), "critique findings conflict")
    else:
        c.need(kind in c.GATES, "unsupported response action")
        c.fields(value, {"schema", "gate_id", "problem_ref", "target_ref", "coverage", "outcome", "findings", "uncertainties"}, "gate report")
        c.reference(value["target_ref"]); c.problem_ref(value["problem_ref"])
        c.need(value["schema"] == "victorique.adaptive_gate.v1" and value["gate_id"] == kind
               and c.same_json(value["problem_ref"], {k: state["problem"][k] for k in ("artifact_id", "revision")})
               and c.same_json(value["target_ref"], proposal["target_ref"])
               and value["coverage"] == "whole_problem_and_proof", "gate binding or coverage differs")
        c.need(type(value["outcome"]) is str and value["outcome"] in {"pass", "fail", "unknown"}, "invalid gate outcome")
        c.strings(value["uncertainties"], "uncertainties")
        # A pass may keep minor findings or doubts; c.gate_passes, with the run's
        # minor_findings setting, decides whether it passes.
        c.need(value["outcome"] != "fail" or bool(value["findings"]), "fail requires exact defects")
    c.need(type(value["findings"]) is list and len(value["findings"]) <= 16, "invalid findings")
    ids = set()
    for finding in value["findings"]:
        checked = c.validate_defect(finding, target)
        c.need(checked["issue_id"] not in ids, "duplicate issue in response")
        ids.add(checked["issue_id"])
    return value


def _stale_reason(state, action):
    proposal = action["proposal"]; plan = state["plan"]
    current_plan = None if plan is None else {"plan_id": plan["plan_id"], "revision": plan["revision"]}
    if not c.same_json(proposal["plan_ref"], current_plan):
        return "plan_version_changed"
    if proposal["kind"] in {"propose_plan", "replan"}:
        return None
    if proposal["kind"] in MODIFY:
        head = _heads(state).get((proposal["route_id"], proposal["obligation_id"]))
        if not c.same_json(None if head is None else c.candidate_ref(head), proposal["target_ref"]):
            return "candidate_head_changed"
        if not all(_fresh(state, _resolve(state, ref)) for ref in proposal["dependency_refs"]):
            return "dependency_version_changed"
    elif not _fresh(state, _resolve(state, proposal["target_ref"])):
        return "candidate_or_dependency_stale"
    if proposal["issue_id"] is not None:
        issue = next((i for i in state["issues"] if i["proposal"]["issue_id"] == proposal["issue_id"]), None)
        if issue is None or not c.same_json(issue["target_ref"], proposal["target_ref"]):
            return "issue_target_changed"
    return None


def _apply_response(state, action, payload, event):
    proposal = action["proposal"]; kind = proposal["kind"]; value = payload["response"]
    source = _source(payload, event)
    plan = state["plan"]
    if kind in {"propose_plan", "replan"}:
        c.need(c.same_json(proposal["plan_ref"], None if plan is None else {"plan_id": plan["plan_id"], "revision": plan["revision"]}), "late plan response")
        value = c.validate_plan(value, state["problem"], state["limits"], plan)
        state["plan"] = value; state["active_route"] = value["routes"][0]["route_id"]
        # Prior plans/candidates remain in immutable action responses; no implicit carry-forward.
    elif kind in MODIFY:
        c.need(plan is not None and c.same_json(proposal["plan_ref"], {"plan_id": plan["plan_id"], "revision": plan["revision"]}), "stale candidate plan")
        value = c.validate_candidate(value, plan, state["limits"])
        c.need((value["route_id"], value["obligation_id"]) == (proposal["route_id"], proposal["obligation_id"]), "candidate target differs")
        old = _heads(state).get((value["route_id"], value["obligation_id"]))
        c.need(c.same_json(None if old is None else c.candidate_ref(old), proposal["target_ref"]), "candidate target changed during action")
        c.need(value["revision"] == (1 if old is None else old["revision"] + 1), "candidate revision CAS differs")
        if old is not None:
            c.need(value["artifact_id"] == old["artifact_id"], "lineage identity differs")
        else:
            c.need(not any(x["artifact"]["artifact_id"] == value["artifact_id"] for x in state["candidates"]), "artifact identity already used")
        c.need(c.same_json(value["dependency_refs"], proposal["dependency_refs"]), "candidate dependency binding differs")
        c.need(all(_fresh(state, _resolve(state, ref)) for ref in value["dependency_refs"]), "dependency changed during action")
        c.need(all(set(_resolve(state, ref)["assumptions"]).issubset(set(value["assumptions"]))
                   for ref in value["dependency_refs"]), "dependency assumptions are not discharged by declared scope")
        expected = [] if kind != "repair_obligation" else [proposal["issue_id"]]
        c.need(c.same_json(value["addressed_issue_ids"], expected), "repair claim differs from selected issue")
        state["candidates"].append({"artifact": value, "source": source, "status": "candidate_unverified"})
        affected_issues = [i for i in state["issues"] if c.same_json(i["target_ref"], proposal["target_ref"]) and i["disposition"] != "refuted"]
        if affected_issues:
            _feedback(state, source, c.candidate_ref(value), "candidate_changed_requires_recheck")
        for issue in affected_issues:
            selected = kind == "repair_obligation" and issue["proposal"]["issue_id"] == proposal["issue_id"]
            issue["target_ref"] = c.candidate_ref(value)
            issue["disposition"] = "addressed_claim" if selected else "unresolved"
            issue["current_feedback_receipt_id"] = source["receipt_id"]
            issue["history"].append({"kind": "author_repair_claim" if selected else "version_changed_requires_recheck",
                                     "target_ref": issue["target_ref"], "source": source})
    else:
        target = _resolve(state, proposal["target_ref"])
        c.need(_fresh(state, target), "response target is stale")
        if kind == "critique_candidate":
            c.fields(value, {"schema", "target_ref", "findings", "no_gap_identified"}, "critique")
            c.reference(value["target_ref"])
            c.need(value["schema"] == "victorique.adaptive_critique.v1" and c.same_json(value["target_ref"], c.candidate_ref(target)), "critique binding differs")
            c.need(type(value["findings"]) is list and len(value["findings"]) <= 16
                   and type(value["no_gap_identified"]) is bool and value["no_gap_identified"] == (not value["findings"]), "critique findings conflict")
            _add_issues(state, value["findings"], target, source)
        elif kind == "investigate":
            value = c.validate_disposition(value, proposal["issue_id"], target)
            issue = next(i for i in state["issues"] if i["proposal"]["issue_id"] == proposal["issue_id"])
            c.need(c.same_json(issue["target_ref"], value["target_ref"]), "investigation issue version changed")
            issue["disposition"] = value["decision"]
            issue["current_feedback_receipt_id"] = source["receipt_id"]
            _feedback(state, source, c.candidate_ref(target), "investigation")
            issue["history"].append({"kind": "investigation", "report": value, "source": source})
        elif kind in c.GATES:
            c.fields(value, {"schema", "gate_id", "problem_ref", "target_ref", "coverage", "outcome", "findings", "uncertainties"}, "gate report")
            c.reference(value["target_ref"]); c.problem_ref(value["problem_ref"])
            c.need(value["schema"] == "victorique.adaptive_gate.v1" and value["gate_id"] == kind
                   and c.same_json(value["problem_ref"], {k: state["problem"][k] for k in ("artifact_id", "revision")})
                   and c.same_json(value["target_ref"], c.candidate_ref(target))
                   and value["coverage"] == "whole_problem_and_proof", "gate binding or coverage differs")
            c.need(type(value["outcome"]) is str and value["outcome"] in {"pass", "fail", "unknown"}, "invalid gate outcome")
            c.need(type(value["findings"]) is list and len(value["findings"]) <= 16, "invalid gate findings")
            c.strings(value["uncertainties"], "uncertainties")
            # A pass may keep minor findings or doubts; c.gate_passes, with the run's
            # minor_findings setting, decides whether it passes.
            c.need(value["outcome"] != "fail" or bool(value["findings"]), "fail requires exact defects")
            for finding in value["findings"]:
                c.validate_defect(finding, target)
            if not c.gate_passes(value, c.minor_findings_rule(state["limits"])):
                # Minor notes on a passing report stay non-blocking, as in the other policies,
                # unless the run's minor_findings setting is blocking: then they become issues.
                _add_issues(state, value["findings"], target, source)
            state["gates"].append({"report": c.copy_json(value), "source": source})


def _add_issues(state, findings, target, source):
    if findings:
        _feedback(state, source, c.candidate_ref(target), "defect_proposals")
    for finding in findings:
        item = c.validate_defect(finding, target)
        c.need(not any(i["proposal"]["issue_id"] == item["issue_id"] for i in state["issues"]), "issue identity reused")
        state["issues"].append({"proposal": item, "target_ref": item["target_ref"], "disposition": "proposed",
                                "investigations": 0, "repairs": 0, "source": source, "history": [],
                                "current_feedback_receipt_id": source["receipt_id"]})


def _feedback(state, source, target_ref, kind):
    c.need(not any(f["receipt_id"] == source["receipt_id"] for f in state["feedback"]), "feedback receipt reused")
    state["feedback"].append({"receipt_id": source["receipt_id"], "target_ref": target_ref, "kind": kind,
                              "source": source, "released": False, "received_by": [], "handoff_events": []})


def reduce(state, event):
    """Apply one explicit declared event without changing the input object.

    reserve_action records an advisory reservation, not an actual host call.
    return_action requires distinct, bound receipt provenance for projection.
    """
    state = _state(state); event = c.copy_json(event)
    c.fields(event, {"event_id", "expected_state_revision", "recorded_at_utc", "kind", "payload"}, "event")
    c.identifier(event["event_id"]); c.utc(event["recorded_at_utc"])
    c.identifier(event["kind"], "event kind")
    for existing in state["events"]:
        if existing["event_id"] == event["event_id"]:
            c.need(c.same_json(existing, event), "event identity reused with changed payload")
            return state
    c.need(type(event["expected_state_revision"]) is int and event["expected_state_revision"] == state["state_revision"], "state CAS mismatch")
    c.need(not state["events"] or c.utc(event["recorded_at_utc"]) >= c.utc(state["events"][-1]["recorded_at_utc"]), "event timestamp moved backwards")
    kind = event["kind"]; payload = event["payload"]
    if kind == 'seed_input':
        c.fields(payload, {'input_ref'}, 'seed bootstrap')
        c.need(seed.is_state(state) and state['state_revision'] == 0 and not state['events']
               and state['plan'] is None and not any(state[key] for key in
               ('candidates', 'issues', 'actions', 'gates', 'feedback', 'planning_handoffs'))
               and state['endpoint'] is None, 'seed requires an untouched new state')
        c.need(c.same_json(payload['input_ref'], seed.input_ref(state['initial_input'])), 'seed input binding differs')
        plan, candidate = seed.bootstrap(state['problem'], state['limits'], state['initial_input'])
        state['plan'] = plan; state['active_route'] = 'main'
        source = {'kind': 'external_input', 'input_ref': seed.input_ref(state['initial_input']),
                  'problem_ref': seed.input_ref(state['problem']), 'candidate_ref': c.candidate_ref(candidate),
                  'event_id': event['event_id'], 'sequence': 1,
                  'recorded_at_utc': event['recorded_at_utc'], 'authenticated': False}
        state['candidates'].append({'artifact': candidate, 'source': source, 'status': 'candidate_unverified'})
    elif kind == "reserve_action":
        c.fields(payload, {"action_id", "proposal"}, "reservation")
        c.identifier(payload["action_id"])
        c.need(not any(a["action_id"] == payload["action_id"] for a in state["actions"]), "action identity reused")
        listing = legal_actions(state, event["recorded_at_utc"])
        c.need(any(c.same_json(payload["proposal"], proposed) for proposed in listing["actions"]), "proposal is not currently legal")
        state["actions"].append({"action_id": payload["action_id"], "proposal": payload["proposal"],
                                  "status": "reserved", "cost_units": None, "cost_source": None,
                                  "receipt": None, "response": None, "projection": None})
        if payload["proposal"]["issue_id"] is not None:
            issue = next(i for i in state["issues"] if i["proposal"]["issue_id"] == payload["proposal"]["issue_id"])
            if payload["proposal"]["kind"] == "investigate":
                issue["investigations"] += 1
            elif payload["proposal"]["kind"] == "repair_obligation":
                issue["repairs"] += 1
        if payload["proposal"]["route_id"] is not None:
            state["active_route"] = payload["proposal"]["route_id"]
    elif kind == "return_action":
        c.fields(payload, {"action_id", "status", "receipt_id", "host_reference", "source_event_id", "source_line", "cost_units", "cost_source", "response"}, "returned action")
        action = next((a for a in state["actions"] if a["action_id"] == payload["action_id"]), None)
        c.need(action is not None and action["status"] in {"reserved", "unknown"}, "return lacks open reserved action")
        # timed_out: the call stopped at its per-call time limit and the run sends it again.
        c.need(type(payload["status"]) is str and payload["status"] in {"completed", "failed", "unknown", "timed_out"},
               "invalid action result status")
        c.identifier(payload["receipt_id"], "receipt ID")
        c.need(not any(e["kind"] == "return_action" and e["payload"]["receipt_id"] == payload["receipt_id"]
                       for e in state["events"]), "receipt identity already recorded in event history")
        if payload["host_reference"] is not None:
            c.text(payload["host_reference"], "host reference", 2000)
        c.need(payload["status"] != "completed" or payload["host_reference"] is not None, "completed result lacks declared host binding")
        if payload["source_event_id"] is not None:
            c.identifier(payload["source_event_id"])
        if payload["source_line"] is not None:
            c.integer(payload["source_line"], "source line", 1, 10**9)
        if payload["cost_units"] is not None:
            c.integer(payload["cost_units"], "actual cost", 0, 10**12)
            c.text(payload["cost_source"], "cost source", 2000)
            c.need(action["cost_units"] is None or payload["cost_units"] >= action["cost_units"], "recovery cannot erase observed cost")
        else:
            c.need(payload["cost_source"] is None, "unknown cost source must be null")
        c.need(payload["status"] == "completed" or payload["response"] is None, "noncompleted result contains response")
        for old in state["actions"]:
            if old["receipt"] is not None and old is not action:
                c.need(old["receipt"]["receipt_id"] != payload["receipt_id"], "receipt reused across actions")
                c.need(payload["host_reference"] is None or old["receipt"]["host_reference"] != payload["host_reference"], "host reference reused across actions")
        if action["receipt"] is not None and action["receipt"]["host_reference"] is not None:
            c.need(payload["host_reference"] == action["receipt"]["host_reference"], "recovery host binding differs")
        if payload["status"] == "completed" and state["endpoint"] is None:
            _validate_bound_response(state, action, payload["response"])
            stale_reason = _stale_reason(state, action)
            if stale_reason is not None:
                action["projection"] = "stale_audit_only"
                action["stale_reason"] = stale_reason
            else:
                _apply_response(state, action, payload, event)
                action["projection"] = "declared_receipt_projected"
        elif state["endpoint"] is not None:
            action["projection"] = "late_audit_only"
        if payload["cost_units"] is not None:
            action.update(cost_units=payload["cost_units"], cost_source=payload["cost_source"])
        if payload["status"] == "timed_out" and action["proposal"]["issue_id"] is not None:
            # The repetition is the attempt on this issue, not the stopped call.
            issue = next(i for i in state["issues"] if i["proposal"]["issue_id"] == action["proposal"]["issue_id"])
            if action["proposal"]["kind"] == "investigate":
                issue["investigations"] -= 1
            elif action["proposal"]["kind"] == "repair_obligation":
                issue["repairs"] -= 1
        action.update(status=payload["status"], receipt=_source(payload, event), response=payload["response"])
    elif kind in {"release_planning_evidence", "receive_planning_evidence"}:
        expected = {"receipt_id", "plan_ref", "target_ref"} | ({"recipient_role"} if kind == "receive_planning_evidence" else set())
        c.fields(payload, expected, "planning evidence handoff")
        c.need(state["endpoint"] is None, "endpoint already frozen")
        source = next((row for row in planning_evidence_sources(state) if row["receipt_id"] == payload["receipt_id"]), None)
        c.need(source is not None and c.same_json(source["plan_ref"], payload["plan_ref"])
               and c.same_json(source["target_ref"], payload["target_ref"]), "planning evidence source or exact target differs")
        row = next((r for r in state["planning_handoffs"] if r["receipt_id"] == payload["receipt_id"]), None)
        if kind == "release_planning_evidence":
            c.need(row is None, "planning evidence already released")
            row = {"receipt_id": payload["receipt_id"], "plan_ref": payload["plan_ref"], "target_ref": payload["target_ref"],
                   "released": True, "received_by": [], "handoff_events": []}
            state["planning_handoffs"].append(row)
        else:
            c.need(row is not None and row["released"] and payload["recipient_role"] == "planner", "planning evidence requires released planner handoff")
            c.need("planner" not in row["received_by"], "planning evidence already received")
            row["received_by"].append("planner")
        row["handoff_events"].append({"event_id": event["event_id"], "sequence": event["expected_state_revision"] + 1,
                                    "kind": kind, "recorded_at_utc": event["recorded_at_utc"], "model_consumption_verified": False})
    elif kind in {"release_feedback", "receive_feedback"}:
        expected = {"receipt_id", "target_ref"} | ({"recipient_role"} if kind == "receive_feedback" else set())
        c.fields(payload, expected, "feedback handoff")
        c.need(state["endpoint"] is None, "endpoint already frozen")
        c.reference(payload["target_ref"])
        row = next((f for f in state["feedback"] if f["receipt_id"] == payload["receipt_id"]), None)
        c.need(row is not None and c.same_json(row["target_ref"], payload["target_ref"])
               and _fresh(state, _resolve(state, payload["target_ref"])), "feedback target is missing or stale")
        if kind == "release_feedback":
            c.need(not row["released"], "feedback already released")
            row["released"] = True
        else:
            c.need(row["released"] and payload["recipient_role"] in c.ROLES, "feedback not released or unknown recipient")
            c.need(payload["recipient_role"] not in row["received_by"], "feedback recipient already recorded")
            row["received_by"].append(payload["recipient_role"])
        row["handoff_events"].append({"event_id": event["event_id"], "sequence": event["expected_state_revision"] + 1,
                                       "kind": kind, "recorded_at_utc": event["recorded_at_utc"],
                                       "model_consumption_verified": False})
    elif kind == "finalize":
        c.fields(payload, {"decision", "reason"}, "finalization")
        c.need(state["endpoint"] is None, "endpoint already frozen")
        decision = choose(state, event["recorded_at_utc"])
        c.need(c.same_json(payload, {"decision": decision["decision"], "reason": decision["reason"]})
               and decision["decision"] in {"complete", "abstain", "stop"}, "finalization differs from bounded decision")
        state["endpoint"] = {**payload, "frozen_at_utc": event["recorded_at_utc"],
                              "state_revision": state["state_revision"], "external_shutdown_verified": False,
                              "mathematical_truth_verified": False}
    elif kind == "stop":
        c.fields(payload, {"reason"}, "stop")
        c.text(payload["reason"], "stop reason", 2000)
        c.need(state["endpoint"] is None, "endpoint already frozen")
        state["endpoint"] = {"decision": "stop", "reason": payload["reason"],
                              "frozen_at_utc": event["recorded_at_utc"], "state_revision": state["state_revision"],
                              "external_shutdown_verified": False, "mathematical_truth_verified": False}
    else:
        raise c.AdaptiveContractError("unsupported event kind")
    state["events"].append(event); state["state_revision"] += 1
    return c.copy_json(state)
