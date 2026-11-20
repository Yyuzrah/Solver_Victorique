"""Adaptive domain projection helpers for the durable session protocol.

The durable wrapper replays source events and exact raw responses before it
accepts a cached projection. No external host is invoked by this module.
"""
from . import adaptive_contracts as c
from . import adaptive_controller as controller
from . import adaptive_exchange as exchange
from . import adaptive_roles
from . import seeded_repair as seed
from copy import deepcopy
from uuid import uuid4


CONTROLLER_VERSION = "bounded_rules_v2"
SESSION_VERSION = "victorique.adaptive_session.v2"
CONTRACTS_VERSION = "adaptive_contracts.v1"
TEMPLATES_VERSION = "adaptive_roles.v2"


def completion_reserve(state, proposal, now_utc):
    """Return the controller's pinned completion reserve for an exact legal proposal.

    The proposed call is included. These units are reservation units, not
    observed currency. The suffix assumes only structural feasibility.
    """
    listing = controller.legal_actions(state, now_utc)
    c.need(any(c.same_json(proposal, item) for item in listing["actions"]),
           "completion reserve requires an exact current legal proposal")
    suffix, blocked = controller._minimum_suffix(state, proposal)
    c.need(blocked is None, "completion reserve blocked")
    kinds = [proposal["kind"], *suffix]
    limits = state["limits"]
    roles = {role: {"calls": 0, "units": 0} for role in c.ROLES}
    for kind in kinds:
        role = limits["action_roles"][kind]
        roles[role]["calls"] += 1
        roles[role]["units"] += limits["action_caps"][kind]
    return {"schema": "victorique.adaptive_completion_reserve.v1",
            "controller_version": seed.CONTROLLER_VERSION if seed.is_state(state) else CONTROLLER_VERSION,
            "state_revision": state["state_revision"],
            "proposal": c.copy_json(proposal), "action_kinds": kinds,
            "calls": len(kinds),
            "units": sum(limits["action_caps"][kind] for kind in kinds),
            "roles": roles, "semantic_success_guaranteed": False}


def financial_admission(state, proposal, liabilities, now_utc):
    """Check current per-action accounting without double-adding controller costs.

    ``liabilities`` is a normalized, source-audited ledger view supplied by
    the durable layer, with exactly one row per existing reducer action.
    This function cannot authenticate a caller's billing assertions.
    """
    reserve = completion_reserve(state, proposal, now_utc)
    c.need(type(liabilities) is list, "liabilities must be a list")
    actions = {row["action_id"]: row for row in state["actions"]}
    seen = set()
    roles = {role: {"calls": 0, "units": 0} for role in c.ROLES}
    total = 0
    reasons = []
    actual = 0
    unknown = False
    for value in liabilities:
        row = c.copy_json(value)
        c.fields(row, {"action_id", "role", "cap_units", "cumulative_units", "final_bill"},
                 "normalized financial liability")
        c.identifier(row["action_id"])
        c.need(row["action_id"] not in seen and row["action_id"] in actions,
               "financial action is duplicate or unknown")
        seen.add(row["action_id"])
        action = actions[row["action_id"]]
        c.need(c.same_json(row["role"], action["proposal"]["role"])
               and c.same_json(row["cap_units"], action["proposal"]["cap_units"]),
               "financial role or cap differs from reserved action")
        c.need(type(row["final_bill"]) is bool, "final bill must be boolean")
        if row["cumulative_units"] is not None:
            c.integer(row["cumulative_units"], "cumulative units", 0, 10**12)
        c.need(not row["final_bill"] or row["cumulative_units"] is not None,
               "final bill cannot have unknown units")
        if action["cost_units"] is not None:
            c.need(row["cumulative_units"] is not None
                   and row["cumulative_units"] >= action["cost_units"],
                   "financial ledger erased an observed reducer bill")
        if row["cumulative_units"] is None:
            unknown = True
        else:
            actual += row["cumulative_units"]
        known = row["cumulative_units"] or 0
        units = known if row["final_bill"] else max(row["cap_units"], known)
        if known > row["cap_units"]:
            reasons.append("request_cap_exceeded:" + row["action_id"])
        role = row["role"]
        total += units
        roles[role]["calls"] += 1
        roles[role]["units"] += units
    c.need(seen == set(actions), "financial ledger lacks a reserved action")
    limits = state["limits"]
    if len(actions) + reserve["calls"] > limits["global_call_limit"]:
        reasons.append("completion_call_reserve")
    if total + reserve["units"] > limits["total_budget_units"]:
        reasons.append("completion_fee_reserve")
    for role in c.ROLES:
        if roles[role]["calls"] + reserve["roles"][role]["calls"] > limits["role_call_limits"][role]:
            reasons.append("completion_role_call_reserve:" + role)
        if roles[role]["units"] + reserve["roles"][role]["units"] > limits["role_budget_units"][role]:
            reasons.append("completion_role_fee_reserve:" + role)
    return {"admitted": not reasons, "reasons": reasons,
            "current_liability_units": total, "current_roles": roles,
            "actual_cost_units": None if unknown else actual,
            "completion_reserve": reserve, "source_authenticated": False}


def dependency_closure(state, proposal, *, include_target=False):
    """Freeze exact bodies; new inputs are fresh, old target inputs historic."""
    state = controller._state(state)
    direct = proposal["dependency_refs"]
    c.need(type(direct) is list, "dependency references must be a list")
    visited = set()
    visiting = set()
    result = []

    def walk(ref, require_fresh=True):
        c.reference(ref)
        identity = tuple(ref[key] for key in sorted(ref))
        c.need(identity not in visiting, "candidate dependency cycle")
        artifact = controller._resolve(state, ref)
        c.need(artifact is not None and (not require_fresh or controller._fresh(state, artifact)),
               "context dependency is missing or stale")
        if identity in visited:
            return
        visiting.add(identity)
        for child in artifact["dependency_refs"]:
            walk(child, require_fresh)
        visiting.remove(identity)
        visited.add(identity)
        result.append(c.copy_json(artifact))

    for ref in direct:
        walk(ref)
    if include_target and proposal.get("target_ref") is not None:
        target = controller._resolve(state, proposal["target_ref"])
        c.need(target is not None, "context target missing")
        for ref in target["dependency_refs"]:
            walk(ref, False)
    return result


def _event(state, identity, at, kind, payload):
    return {"event_id": identity, "expected_state_revision": state["state_revision"],
            "recorded_at_utc": at, "kind": kind, "payload": payload}


def _initial_projection(manifest):
    expected = {"contracts": CONTRACTS_VERSION, "controller": CONTROLLER_VERSION,
                "exchange": exchange.VERSION, "session": SESSION_VERSION, "templates": TEMPLATES_VERSION}
    seeded = manifest['schema'] == seed.MANIFEST_SCHEMA
    c.need(c.same_json(manifest["implementation_versions"], seed.versions() if seeded else expected), "adaptive implementation versions differ")
    state = (controller.create_seeded_state(manifest['session_id'], manifest['problem'], manifest['limits'], manifest['initial_input'])
             if seeded else controller.create_state(manifest['session_id'], manifest['problem'], manifest['limits']))
    return {"state": state,
            "journal_sequence": 0, "base_sequence": 0, "handoffs": [], "endpoint": None}


def _next_projection(projection):
    """Return a projection the _derive helpers may change while the given one stays valid.

    They replace state (controller.reduce returns a new state), endpoint and
    the sequence fields, and append to handoffs. Nothing else is changed in
    place, so the rest is shared instead of deep-copied.
    """
    return {**projection, "handoffs": list(projection["handoffs"])}


def _derive_seed(projection, base, entry):
    """Create-time local source event. Never a model call or transport receipt."""
    state = projection['state']; record = entry['record']
    c.fields(record, {'session_id', 'operation_id', 'expected_session_sequence', 'expected_state_revision'}, 'seed creation record')
    c.need(base['manifest']['schema'] == seed.MANIFEST_SCHEMA and projection['journal_sequence'] == 0
           and base['sequence'] == 0 and not base['requests'] and projection['endpoint'] is None,
           'seed is creation-only before all exchange operations')
    c.need(record['session_id'] == base['manifest']['session_id'] and record['operation_id'] == entry['operation_id']
           and c.same_json(record['expected_session_sequence'], 0) and c.same_json(record['expected_state_revision'], 0),
           'seed creation identity or sequence differs')
    c.identifier(record['operation_id'])
    event = _event(state, entry['event_id'], entry['recorded_at_utc'], 'seed_input',
                   {'input_ref': seed.input_ref(base['manifest']['initial_input'])})
    result = _next_projection(projection)
    result['state'] = controller.reduce(state, event)
    return result, {'domain_event': event, 'classification': 'external_input_seeded', 'error': None}


def _template(state, proposal):
    kind = proposal["kind"]
    template = adaptive_roles.template(kind)
    c.need(type(template["revision"]) is int and template["revision"] == 2, "role template revision differs")
    instructions = {"common": template["common_instructions"], "role": template["instructions"],
                    "limitations": template["limitations"], "output_contract": adaptive_roles.output_contract(kind),
                    "response_format": "Return the exact schema/binding/output response envelope. The contract is a blueprint: shape, nullable and descriptive strings are helper instructions, not output fields or literal output values."}
    if kind not in c.GATES:
        # minor_findings only changes how the controller reads gate reports, so role
        # instructions are the same under both values.
        limits = {key: value for key, value in state["limits"].items() if key != "minor_findings"}
        instructions["bound_constraints"] = {"controller_limits": limits,
            "current_budget": controller.budget_view(state), "selected_proposal": proposal}
    return {"template_id": template["template_id"], "version": "v2",
            "instructions": exchange._dump(instructions)}


def _planning_context(projection, proposal, manifest):
    state = projection["state"]
    sources = controller.planning_evidence_sources(state) if proposal["kind"] == "replan" else []
    c.need(len(sources) <= controller.PLANNING_EVIDENCE_MAX, "planning evidence capacity exceeded")
    c.need(c.same_json(proposal["planning_receipt_ids"], [row["receipt_id"] for row in sources]),
           "planning evidence selection differs from current source set")
    actor = manifest["role_bindings"]["planner"]["actor_id"]
    entries = []
    for source in sources:
        release = next((r["record"] for r in projection["handoffs"] if r["kind"] == "release_planning_evidence"
                        and r["record"]["receipt_id"] == source["receipt_id"]), None)
        c.need(release is not None, "planning source lacks explicit release")
        read = next((r["record"] for r in projection["handoffs"] if r["kind"] == "receive_planning_evidence"
                     and r["record"]["release_id"] == release["release_id"]
                     and r["record"]["recipient_role"] == "planner"
                     and r["record"]["recipient_actor_id"] == actor), None)
        c.need(read is not None, "planning source lacks exact planner receive")
        action = next(a for a in state["actions"] if a["action_id"] == source["action_id"])
        target = None if source["target_ref"] is None else controller._resolve(state, source["target_ref"])
        c.need(source["target_ref"] is None or target is not None, "planning target body missing")
        entries.append({**source, "target_candidate": target,
            "response": action["response"] if source["status"] == "completed" else None,
            "release_id": release["release_id"], "read_id": read["read_id"],
            "recipient_role": "planner", "recipient_actor_id": actor})
    return entries, {"eligible_count": len(sources), "selected_count": len(entries),
                     "max_entries": controller.PLANNING_EVIDENCE_MAX, "truncated": False,
                     "policy": "all_current_plan_diagnostic_receipts_or_block"}


def _context(projection, proposal, manifest):
    state = projection["state"]
    target = None if proposal["target_ref"] is None else controller._resolve(state, proposal["target_ref"])
    issue = None
    if proposal["issue_id"] is not None:
        source = next(row for row in state["issues"] if row["proposal"]["issue_id"] == proposal["issue_id"])
        original = controller._resolve(state, source["proposal"]["target_ref"])
        c.need(original is not None, "original defect target missing")
        issue = {"proposal": source["proposal"], "original_target_candidate": original,
                 "target_ref": source["target_ref"], "disposition": source["disposition"]}
    feedback = []
    actor = manifest["role_bindings"][proposal["role"]]["actor_id"]
    for receipt_id in proposal["feedback_receipt_ids"]:
        release = next((row for row in projection["handoffs"] if row["kind"] == "release_feedback"
                        and row["record"]["receipt_id"] == receipt_id), None)
        c.need(release is not None, "missing source-bound feedback release")
        read = next((row for row in projection["handoffs"] if row["kind"] == "receive_feedback"
                     and row["record"]["release_id"] == release["record"]["release_id"]
                     and row["record"]["recipient_role"] == proposal["role"]
                     and row["record"]["recipient_actor_id"] == actor), None)
        c.need(read is not None, "missing exact actor feedback handoff")
        response = next((row["response"] for row in state["actions"] if row["receipt"] is not None
                         and row["receipt"]["receipt_id"] == receipt_id), None)
        c.need(response is not None, "released feedback lacks original response")
        row = next(row for row in state["feedback"] if row["receipt_id"] == receipt_id)
        feedback.append({"receipt_id": receipt_id, "target_ref": proposal["target_ref"], "kind": row["kind"],
            "content": exchange._dump(response), "release_id": release["record"]["release_id"],
            "read_id": read["record"]["read_id"], "recipient_role": proposal["role"], "recipient_actor_id": actor})
    closure = sorted(dependency_closure(state, proposal, include_target=True), key=lambda item: exchange._refkey(c.candidate_ref(item)))
    planning, selection = _planning_context(projection, proposal, manifest)
    return {"problem": state["problem"], "plan": state["plan"], "target_candidate": target,
            "dependency_closure": closure, "selected_issue": issue, "released_feedback": feedback,
            "planning_evidence": planning, "planning_evidence_selection": selection}


def _liabilities(base):
    return [{"action_id": row["record"]["action_id"], "role": row["record"]["role"],
             "cap_units": row["packet"]["binding"]["cap_units"],
             "cumulative_units": row["known_cost_units"], "final_bill": row["bill_final"]}
            for row in base["requests"].values()]


def _claim_completion_admission(state, proposal, base):
    """Recompute remaining suffix after this action was already reserved."""
    reserve_state = deepcopy(state)
    if proposal["kind"] == "investigate":
        issue = next(i for i in reserve_state["issues"] if i["proposal"]["issue_id"] == proposal["issue_id"])
        # The pinned controller helper adds the selected attempt. It is already in the
        # durable count here; remove that one only from the helper input.
        c.need(issue["investigations"] > 0, "reserved investigation count missing")
        issue["investigations"] -= 1
    suffix, blocked = controller._minimum_suffix(reserve_state, proposal)
    c.need(blocked is None, "claim completion responsibility blocked")
    budget = exchange._budgets(base); limits = state["limits"]
    c.need(not budget["financial_blocked"] and not budget["request_or_total_cap_exceeded"], "claim financial evidence blocks work")
    c.need(budget["reserved_calls"] + len(suffix) <= limits["global_call_limit"], "claim completion call reserve")
    c.need(budget["committed_units"] + sum(limits["action_caps"][k] for k in suffix) <= limits["total_budget_units"], "claim completion fee reserve")
    for role in c.ROLES:
        kinds = [k for k in suffix if limits["action_roles"][k] == role]
        c.need(budget["roles"][role]["reserved_calls"] + len(kinds) <= limits["role_call_limits"][role], "claim completion role calls")
        c.need(budget["roles"][role]["committed_units"] + sum(limits["action_caps"][k] for k in kinds) <= limits["role_budget_units"][role], "claim completion role fees")


def _matching_proposal(state, request, at):
    candidates = controller.legal_actions(state, at)["actions"]
    expected = {"kind": request["kind"], "role": request["role"], "route_id": request["route_id"],
                "obligation_id": request["obligation_id"], "state_revision": request["prepared_state_revision"],
                "dependency_refs": request["dependency_refs"],
                "target_ref": None if request["context"]["target_candidate"] is None else c.candidate_ref(request["context"]["target_candidate"]),
                "issue_id": None if request["context"]["selected_issue"] is None else request["context"]["selected_issue"]["proposal"]["issue_id"]}
    matches = [p for p in candidates if all(c.same_json(p[k], value) for k, value in expected.items())]
    c.need(len(matches) == 1, "request does not map to one current legal action")
    return matches[0]


def _frozen_endpoint(projection, base, identity, at, reason=None):
    state = projection["state"]
    selected_route = state["active_route"]
    if state["endpoint"] is not None and state["endpoint"]["decision"] == "complete":
        selected_route = next(route["route_id"] for route in state["plan"]["routes"]
            if all(controller._passed(state, gate, controller._root(state, route["route_id"])) for gate in c.GATES)
            and all(issue["disposition"] == "refuted" for issue in controller._current_issues(state, route["route_id"])))
    root = None if selected_route is None else controller._root(state, selected_route)
    root_ref = None if root is None else c.candidate_ref(root)
    gates = [deepcopy(gate) for gate in state["gates"] if c.same_json(gate["report"]["target_ref"], root_ref)]
    closure = [] if root is None else dependency_closure(state, {"dependency_refs": [], "target_ref": root_ref}, include_target=True)
    result = {"schema": seed.ENDPOINT_SCHEMA if seed.is_state(state) else "victorique.adaptive_endpoint.v1", "endpoint_id": identity,
            "frozen_at_utc": at, "reason": reason,
            "reducer_endpoint": deepcopy(state["endpoint"]), "selected_route_id": selected_route,
            "root_candidate": deepcopy(root), "root_dependency_closure": closure,
            "gates": gates, "state_revision": state["state_revision"],
            "base_sequence": base["sequence"], "budgets_at_close": exchange._budgets(base),
            "unsettled_request_ids": [rid for rid, request in base["requests"].items()
                                      if request["phase"] not in exchange.TERMINAL or not request["bill_final"]],
            "mathematical_truth_verified": False, "expert_verified": False,
            "remote_shutdown_verified": False, "provenance_authenticated": False}
    if seed.is_state(state):
        result['initial_input'] = deepcopy(state['initial_input'])
        result['input_import_count'] = 1
        result['candidate_lineage'] = [{'candidate_ref': c.candidate_ref(row['artifact']), 'source': deepcopy(row['source'])}
                                       for row in state['candidates']]
    return result


def _derive_base(projection, before, after, event):
    """Recompute one domain mapping from the validated source event and raw."""
    result = _next_projection(projection); state = result["state"]
    kind = event["kind"]; record = event["data"]["record"]; at = event["recorded_at_utc"]
    derived = {"domain_event": None, "classification": "exchange_audit_only", "error": None}
    if kind == "prepare":
        c.need(result["endpoint"] is None, "session is sealed")
        proposal = _matching_proposal(state, record, at)
        c.need(c.same_json(record["context"], _context(result, proposal, before["manifest"]))
               and c.same_json(record["template"], _template(state, proposal)), "request context or template differs from replay")
        decision = financial_admission(state, proposal, _liabilities(before), at)
        c.need(decision["admitted"], "authoritative completion liability reserve refused")
        derived["domain_event"] = _event(state, event["event_id"], at, "reserve_action",
            {"action_id": record["action_id"], "proposal": proposal})
        result["state"] = controller.reduce(state, derived["domain_event"])
        derived["classification"] = "action_reserved"
    elif kind == "claim":
        c.need(result["endpoint"] is None, "session is sealed")
        request = after["requests"][record["request_id"]]
        action = next((a for a in state["actions"] if a["action_id"] == request["record"]["action_id"]), None)
        c.need(action is not None and action["status"] == "reserved"
               and controller._stale_reason(state, action) is None, "claim target is not current reserved work")
        c.need(not any(a["status"] == "unknown" for a in state["actions"]), "unknown domain action blocks claim")
        proposal = action["proposal"]
        c.need(proposal['kind'] == 'critique_candidate' or seed.critique_ready(state),
               'initial seed critique required before claim')
        if proposal["kind"] == "replan":
            planning, selection = _planning_context(result, proposal, before["manifest"])
            c.need(c.same_json(planning, request["record"]["context"]["planning_evidence"])
                   and c.same_json(selection, request["record"]["context"]["planning_evidence_selection"]),
                   "planning evidence changed since prepare")
        if proposal["kind"] in c.GATES:
            c.need(all(issue["disposition"] == "refuted"
                       for issue in controller._current_issues(state, proposal["route_id"])),
                   "current unresolved issues block prepared gate claim")
            if proposal["kind"] == "whole_proof_review":
                c.need(controller._passed(state, "alignment_review", controller._root(state, proposal["route_id"])),
                       "current alignment gate is required before whole-proof claim")
        _claim_completion_admission(state, action["proposal"], before)
        derived["classification"] = "current_action_claimed"
    elif kind == "observe":
        observation = next(row for row in after["observations"] if row["observation_id"] == record["observation_id"])
        classification = observation["classification"]
        if result["endpoint"] is not None or observation["historical_only"]:
            derived["classification"] = "late_audit_only"
        elif classification in {"returned_valid", "returned_invalid", "returned_invalid_model", "failed", "unknown"}:
            request = after["requests"][record["request_id"]]
            action = next((a for a in state["actions"] if a["action_id"] == request["record"]["action_id"]), None)
            c.need(action is not None, "observation lacks reserved domain action")
            status = "completed" if classification == "returned_valid" else "unknown" if classification == "unknown" else "failed"
            if classification == "failed" and record["reason"] == exchange.LOCAL_TIMEOUT:
                status = "timed_out"  # The loop settles such a call only when it sends it again.
            cost = request["known_cost_units"]
            charges = [r for r in after["charges"] if r["request_id"] == record["request_id"] and r["classification"] == "accepted"
                       and r["charge"]["cumulative_units"] == cost and cost is not None]
            payload = {"action_id": action["action_id"], "status": status, "receipt_id": record["observation_id"],
                "host_reference": request["host_reference"], "source_event_id": event["event_id"], "source_line": None,
                "cost_units": cost, "cost_source": None if cost is None else "charge:" + charges[-1]["charge"]["charge_id"],
                "response": observation["parsed_response"]["output"] if classification == "returned_valid" else None}
            trial_event = _event(state, event["event_id"], at, "return_action", payload)
            try:
                result["state"] = controller.reduce(state, trial_event)
                derived["domain_event"] = trial_event
                row = next(a for a in result["state"]["actions"] if a["action_id"] == action["action_id"])
                derived["classification"] = row["projection"] or "transport_" + status
            except (c.AdaptiveContractError, ValueError, TypeError, KeyError) as exc:
                clock_regressed = bool(state["events"]) and c.utc(at) < c.utc(state["events"][-1]["recorded_at_utc"])
                resource_limit = str(exc) in {"JSON exceeds byte limit", "JSON nesting limit", "JSON list limit"}
                derived["classification"] = "clock_anomaly" if clock_regressed else "projection_resource_exhausted" if resource_limit else "domain_projection_rejected"
                derived["error"] = str(exc)[:2000]
                result["endpoint"] = _frozen_endpoint(result, after, event["event_id"], at, derived["classification"])
        elif classification == "capture_failed":
            derived["classification"] = "capture_failed"
            result["endpoint"] = _frozen_endpoint(result, after, event["event_id"], at, "capture_failed")
        else:
            derived["classification"] = classification
    elif kind == "close":
        c.need(result["endpoint"] is None, "session already sealed")
        trial_event = _event(state, event["event_id"], at, "stop", {"reason": record["reason"]})
        result["state"] = controller.reduce(state, trial_event)
        derived["domain_event"] = trial_event; derived["classification"] = "local_stop"
        result["endpoint"] = _frozen_endpoint(result, after, event["event_id"], at)
    elif kind == "export":
        c.need(result["endpoint"] is None, "sealed session cannot export new packet")
    result["base_sequence"] = after["sequence"]
    return result, derived


CONTROL_COMMON = {"session_id", "operation_id", "expected_session_sequence", "expected_state_revision"}


def _derive_control(projection, base, entry):
    result = _next_projection(projection); state = result["state"]
    kind, record = entry["kind"], entry["record"]
    c.need(result["endpoint"] is None and base["endpoint"] is None, "session already sealed")
    c.need(c.same_json(record["expected_session_sequence"], projection["journal_sequence"])
           and c.same_json(record["expected_state_revision"], state["state_revision"]), "session/state CAS differs")
    c.need(record["session_id"] == state["session_id"], "session binding differs")
    for key in ("operation_id",): c.identifier(record[key])
    if kind == "release_feedback":
        c.fields(record, CONTROL_COMMON | {"release_id", "receipt_id", "target_ref", "allowed_roles"}, "feedback release")
        c.identifier(record["release_id"])
        c.need(type(record["allowed_roles"]) is list and record["allowed_roles"]
               and all(type(role) is str and role in c.ROLES for role in record["allowed_roles"])
               and len(set(record["allowed_roles"])) == len(record["allowed_roles"]), "allowed feedback roles differ")
        c.need(not any(row["kind"] == kind and row["record"].get("release_id") == record["release_id"] for row in projection["handoffs"]), "release ID reused")
        payload = {"receipt_id": record["receipt_id"], "target_ref": record["target_ref"]}
    elif kind == "receive_feedback":
        c.fields(record, CONTROL_COMMON | {"read_id", "release_id", "recipient_role", "recipient_actor_id", "target_ref"}, "feedback read")
        c.identifier(record["read_id"])
        release = next((row for row in projection["handoffs"] if row["kind"] == "release_feedback"
                        and row["record"]["release_id"] == record["release_id"]), None)
        c.need(release is not None and record["recipient_role"] in release["record"]["allowed_roles"], "unreleased feedback or recipient")
        actor = base["manifest"]["role_bindings"][record["recipient_role"]]["actor_id"]
        c.need(record["recipient_actor_id"] == actor and c.same_json(record["target_ref"], release["record"]["target_ref"]), "feedback read actor/target differs")
        c.need(not any(row["kind"] == kind and row["record"].get("read_id") == record["read_id"] for row in projection["handoffs"]), "read ID reused")
        payload = {"receipt_id": release["record"]["receipt_id"], "target_ref": record["target_ref"], "recipient_role": record["recipient_role"]}
    elif kind == "release_planning_evidence":
        c.fields(record, CONTROL_COMMON | {"release_id", "receipt_id", "plan_ref", "target_ref"}, "planning release")
        c.identifier(record["release_id"])
        c.need(not any(r["kind"] == kind and r["record"]["release_id"] == record["release_id"]
                       for r in projection["handoffs"]), "planning release ID reused")
        payload = {key: record[key] for key in ("receipt_id", "plan_ref", "target_ref")}
    elif kind == "receive_planning_evidence":
        c.fields(record, CONTROL_COMMON | {"read_id", "release_id", "recipient_role", "recipient_actor_id", "plan_ref", "target_ref"}, "planning receive")
        c.identifier(record["read_id"])
        release = next((r["record"] for r in projection["handoffs"] if r["kind"] == "release_planning_evidence"
                        and r["record"]["release_id"] == record["release_id"]), None)
        c.need(release is not None and record["recipient_role"] == "planner", "planning evidence requires released planner handoff")
        c.need(record["recipient_actor_id"] == base["manifest"]["role_bindings"]["planner"]["actor_id"]
               and c.same_json(record["plan_ref"], release["plan_ref"])
               and c.same_json(record["target_ref"], release["target_ref"]), "planning receive actor/plan/target differs")
        c.need(not any(r["kind"] == kind and r["record"]["read_id"] == record["read_id"]
                       for r in projection["handoffs"]), "planning read ID reused")
        payload = {"receipt_id": release["receipt_id"], "plan_ref": record["plan_ref"],
                   "target_ref": record["target_ref"], "recipient_role": "planner"}
    elif kind == "finalize":
        c.fields(record, CONTROL_COMMON | {"endpoint_id", "decision", "reason"}, "finalization")
        c.identifier(record["endpoint_id"])
        budget = exchange._budgets(base)
        c.need(record["decision"] != "complete" or (not budget["financial_blocked"]
               and not budget["request_or_total_cap_exceeded"]), "financial anomaly prevents completion")
        payload = {"decision": record["decision"], "reason": record["reason"]}
    else:
        raise c.AdaptiveContractError("unsupported session command")
    domain_event = _event(state, entry["event_id"], entry["recorded_at_utc"], kind, payload)
    result["state"] = controller.reduce(state, domain_event)
    if kind in {"release_feedback", "receive_feedback", "release_planning_evidence", "receive_planning_evidence"}:
        result["handoffs"].append({"kind": kind, "record": deepcopy(record), "domain_event_id": entry["event_id"],
                                   "model_consumption_verified": False})
    else:
        result["endpoint"] = _frozen_endpoint(result, base, record["endpoint_id"], entry["recorded_at_utc"])
    return result, {"domain_event": domain_event, "classification": kind, "error": None}


class AdaptiveSession(exchange.AdaptiveExchange):
    """Single-DB domain replay; explicit host claims, never automatic dispatch."""
    EXTRA_TABLES = {"session_journal", "session_cache", "session_endpoint"}
    SUPPORTS_SEEDED = True

    def _verified_projection(self, db):
        # Private to paths yielded by _tx: the domain replay has already
        # verified this projection against the source and the cache under
        # BEGIN IMMEDIATE. Internal appends keep it current; never use it
        # across transactions. It is shared with the verified state, so
        # callers do not change it.
        work = getattr(db, "work", None)
        c.need(db.in_transaction and work is not None, "domain projection requires an active verified transaction")
        return work["extension"]["projection"]

    def _create_extension(self, db, manifest):
        db.execute("CREATE TABLE session_journal(sequence INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,operation_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL)")
        db.execute("CREATE TABLE session_cache(identity INTEGER PRIMARY KEY CHECK(identity=1),payload TEXT NOT NULL)")
        db.execute("CREATE TABLE session_endpoint(identity INTEGER PRIMARY KEY CHECK(identity=1),payload TEXT NOT NULL)")
        for table in ("session_journal", "session_endpoint"):
            for operation in ("UPDATE", "DELETE"):
                db.execute(f'CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, "immutable record"); END')
        initial = _initial_projection(manifest)
        db.execute("INSERT INTO session_cache VALUES (1,?)", (exchange._dump(initial),))
        if manifest['schema'] == seed.MANIFEST_SCHEMA:
            operation_id = 'seed_' + uuid4().hex
            entry = {'sequence': 1, 'event_id': 'domain_' + uuid4().hex, 'operation_id': operation_id,
                     'recorded_at_utc': exchange._now(), 'kind': 'seed_input',
                     'record': {'session_id': manifest['session_id'], 'operation_id': operation_id,
                                'expected_session_sequence': 0, 'expected_state_revision': 0}}
            projection, derived = _derive_seed(initial, exchange._initial(manifest), entry)
            self._save_projection(db, initial, projection, entry, derived)
            self._checkpoint('seed_bootstrap_precommit', entry['event_id'])

    def _verify_extension(self, db, base_state):
        return self._domain_replay(db, base_state)

    def _verify_extension_since(self, db, base_state, verified, delta):
        return self._domain_replay(db, base_state, since=(verified, delta))

    @staticmethod
    def _journal_entry(row, projection):
        entry = exchange._json(row["payload"])
        c.fields(entry, {"sequence", "event_id", "operation_id", "recorded_at_utc", "kind", "derived"}
                 | ({"source_event_id"} if entry.get("kind") == "base" else {"record"}), "session journal entry")
        c.need(c.same_json(entry["sequence"], projection["journal_sequence"] + 1)
               and c.same_json(entry["sequence"], row["sequence"])
               and entry["event_id"] == row["event_id"] and entry["operation_id"] == row["operation_id"], "domain journal sequence/identity differs")
        return entry

    def _domain_replay(self, db, base_state, pending_event=None, since=None):
        """Verify the domain journal and cache; return what the next check needs.

        With since=(verified, delta) only the journal entries appended after
        verified are derived, from the base states the exchange computed for
        the new events. Otherwise the whole journal is replayed from the start.
        """
        if since is not None:
            return self._domain_since(db, base_state, *since)
        base = exchange._initial(base_state["manifest"])
        projection = _initial_projection(base["manifest"])
        events = [exchange._json(row["payload"]) for row in db.execute("SELECT payload FROM events ORDER BY sequence")]
        event_index = 0; last = None
        for row in db.execute("SELECT * FROM session_journal ORDER BY sequence"):
            entry = self._journal_entry(row, projection)
            if entry["kind"] == "base":
                c.need(event_index < len(events), "domain source event missing")
                event = events[event_index]; event_index += 1
                c.need(entry["source_event_id"] == event["event_id"] and entry["operation_id"] == event["operation_id"]
                       and entry["recorded_at_utc"] == event["recorded_at_utc"], "domain source mapping differs")
                raw = None
                if event["kind"] == "observe":
                    raw = db.execute("SELECT raw FROM observations WHERE observation_id=?", (event["data"]["record"]["observation_id"],)).fetchone()[0]
                next_base = exchange._apply(exchange._fork(base), event, raw)
                projection, expected = _derive_base(projection, base, next_base, event)
                base = next_base
            elif entry['kind'] == 'seed_input':
                projection, expected = _derive_seed(projection, base, entry)
            else:
                projection, expected = _derive_control(projection, base, entry)
            c.need(c.same_json(entry["derived"], expected), "domain mapping differs from source replay")
            projection["journal_sequence"] = entry["sequence"]
            last = exchange._row(row, exchange.EVENT_ROW)
        c.need(base['manifest']['schema'] != seed.MANIFEST_SCHEMA or projection['state']['plan'] is not None,
               'seed bootstrap journal is missing')
        if pending_event is None:
            c.need(event_index == len(events) and c.same_json(base, base_state), "unmapped base events")
        else:
            c.need(event_index + 1 == len(events) and c.same_json(events[-1], pending_event)
                   and c.same_json(base, base_state), "unexpected pending base event")
        cache = list(db.execute("SELECT identity,payload FROM session_cache"))
        c.need(len(cache) == 1 and cache[0][0] == 1 and c.same_json(exchange._json(cache[0][1]), projection), "domain projection cache differs")
        endpoints = list(db.execute("SELECT payload FROM session_endpoint"))
        c.need((not endpoints and projection["endpoint"] is None) or
               (len(endpoints) == 1 and c.same_json(exchange._json(endpoints[0][0]), projection["endpoint"])), "immutable session endpoint differs")
        return {"projection": projection, "journal": last, "cache": cache[0][1],
                "endpoint": endpoints[0][0] if endpoints else None}

    def _domain_since(self, db, base_state, verified, delta):
        """Derive the journal entries appended since verified; raise on any other change.

        The journal and session endpoint refuse UPDATE and DELETE by trigger
        (the exchange has compared the schema), so the journal rows must run
        1..count with the last verified row unchanged. The cache has no trigger
        and is compared with the projection in full.
        """
        projection = verified["projection"]; known = projection["journal_sequence"]
        count, low, high = db.execute("SELECT count(*),min(sequence),max(sequence) FROM session_journal").fetchone()
        c.need(exchange._appended_rows(count, low, high, known), "domain journal rows changed")
        if known:
            row = db.execute("SELECT sequence,event_id,operation_id,payload FROM session_journal WHERE sequence=?", (known,)).fetchone()
            c.need(row is not None and tuple(row) == verified["journal"], "verified domain entry changed")
        base = delta[0][2] if delta else base_state
        event_index = 0; last = verified["journal"]
        rows = db.execute("SELECT * FROM session_journal WHERE sequence>? ORDER BY sequence", (known,)).fetchall() if count > known else []
        for row in rows:
            entry = self._journal_entry(row, projection)
            if entry["kind"] == "base":
                c.need(event_index < len(delta), "domain source event missing")
                event, _, before, after = delta[event_index]; event_index += 1
                c.need(entry["source_event_id"] == event["event_id"] and entry["operation_id"] == event["operation_id"]
                       and entry["recorded_at_utc"] == event["recorded_at_utc"], "domain source mapping differs")
                projection, expected = _derive_base(projection, before, after, event)
                base = after
            elif entry['kind'] == 'seed_input':
                projection, expected = _derive_seed(projection, base, entry)
            else:
                projection, expected = _derive_control(projection, base, entry)
            c.need(c.same_json(entry["derived"], expected), "domain mapping differs from source replay")
            projection["journal_sequence"] = entry["sequence"]
            last = exchange._row(row, exchange.EVENT_ROW)
        c.need(event_index == len(delta), "unmapped base events")
        cache = list(db.execute("SELECT identity,payload FROM session_cache"))
        text = exchange._dump(projection) if rows else verified["cache"]
        c.need(len(cache) == 1 and cache[0][0] == 1 and cache[0][1] == text, "domain projection cache differs")
        endpoint = (verified["endpoint"] if verified["projection"]["endpoint"] is not None
                    else None if projection["endpoint"] is None else exchange._dump(projection["endpoint"]))
        endpoints = [r[0] for r in db.execute("SELECT payload FROM session_endpoint")]
        c.need(endpoints == ([] if endpoint is None else [endpoint]), "immutable session endpoint differs")
        return {"projection": projection, "journal": last, "cache": text, "endpoint": endpoint}

    def _save_projection(self, db, prior, projection, entry, derived):
        entry["derived"] = derived
        projection["journal_sequence"] = entry["sequence"]
        payload = exchange._dump(entry); cache = exchange._dump(projection); endpoint = None
        db.execute("INSERT INTO session_journal VALUES (?,?,?,?)", (entry["sequence"], entry["event_id"], entry["operation_id"], payload))
        db.execute("UPDATE session_cache SET payload=? WHERE identity=1", (cache,))
        if prior["endpoint"] is None and projection["endpoint"] is not None:
            endpoint = exchange._dump(projection["endpoint"])
            db.execute("INSERT INTO session_endpoint VALUES (1,?)", (endpoint,))
        work = getattr(db, "work", None)
        if work is not None:
            held = work["extension"]
            work["extension"] = {"projection": projection, "cache": cache,
                                 "journal": (entry["sequence"], entry["event_id"], entry["operation_id"], payload),
                                 "endpoint": held["endpoint"] if endpoint is None else endpoint}

    def _after_append(self, db, prior_base, new_base, event, raw):
        # Called by _append inside _tx, after _verify_extension. _append only
        # adds the current base event; the prior domain projection
        # was verified at transaction entry and updated after every append.
        prior = self._verified_projection(db)
        projection, derived = _derive_base(prior, prior_base, new_base, event)
        entry = {"sequence": prior["journal_sequence"] + 1, "event_id": "domain_" + event["event_id"],
                 "operation_id": event["operation_id"], "recorded_at_utc": event["recorded_at_utc"],
                 "kind": "base", "source_event_id": event["event_id"]}
        self._save_projection(db, prior, projection, entry, derived)

    def propose(self):
        with self._tx() as (db, base):
            projection = self._verified_projection(db); at = exchange._now()
            state = projection["state"]
            listing = controller.legal_actions(state, at)
            if projection["endpoint"] is not None:
                listing["actions"] = []; listing["blocked_reason"] = "session_sealed"
            else:
                admitted = []
                for proposal in listing["actions"]:
                    result = financial_admission(state, proposal, _liabilities(base), at)
                    if result["admitted"] and not base["financial_blocked"]:
                        admitted.append(proposal)
                    else:
                        listing["rejected"].append({"action": proposal, "reason": "authoritative_financial_reserve"})
                listing["actions"] = admitted
            decision = controller.choose(state, at)
            if projection["endpoint"] is not None:
                decision.update(decision="stop", action=None, reason="session_sealed", legal_actions=[])
            elif base["financial_blocked"] or exchange._budgets(base)["request_or_total_cap_exceeded"]:
                decision.update(decision="stop", action=None, reason="authoritative_financial_anomaly", legal_actions=[])
            elif decision["decision"] == "propose":
                decision.update(legal_actions=listing["actions"], rejected_actions=listing["rejected"])
                if listing["actions"]:
                    decision["action"] = listing["actions"][0]
                else:
                    decision.update(decision="abstain", action=None, reason="authoritative_completion_reserve")
            return {"state_revision": state["state_revision"], "session_sequence": projection["journal_sequence"],
                    "event_sequence": base["sequence"], "legal_action_listing": listing,
                    "decision": decision, "planning_evidence_sources": controller.planning_evidence_sources(state),
                    "dispatch_authorized": False}

    def prepare(self, record):
        record = c.copy_json(record)
        c.fields(record, CONTROL_COMMON | {"request_id", "logical_action_id", "action_id", "selected_proposal"}, "session prepare")
        with self._tx() as (db, base):
            projection = self._verified_projection(db); state = projection["state"]
            prior = db.execute("SELECT payload FROM events WHERE operation_id=?", (record["operation_id"],)).fetchone()
            if prior is not None:
                source = exchange._json(prior[0]); request = base["requests"][source["data"]["record"]["request_id"]]
                original = next(row for row in state["actions"] if row["action_id"] == request["record"]["action_id"])
                journal_row = exchange._json(db.execute("SELECT payload FROM session_journal WHERE operation_id=?", (record["operation_id"],)).fetchone()[0])
                c.need(source["kind"] == "prepare" and request["record"]["request_id"] == record["request_id"]
                       and request["record"]["logical_action_id"] == record["logical_action_id"]
                       and request["record"]["action_id"] == record["action_id"]
                       and record["session_id"] == state["session_id"]
                       and c.same_json(record["expected_session_sequence"], journal_row["sequence"] - 1)
                       and c.same_json(record["expected_state_revision"], original["proposal"]["state_revision"])
                       and c.same_json(record["selected_proposal"], original["proposal"]), "prepare retry changed intent")
                return {"request_id": request["request_id"], "packet_id": request["packet"]["binding"]["packet_id"], "prepared_new": False, "dispatch_claim": None}
            c.need(c.same_json(record["expected_session_sequence"], projection["journal_sequence"])
                   and c.same_json(record["expected_state_revision"], state["state_revision"])
                   and record["session_id"] == state["session_id"], "session prepare CAS differs")
            proposal = record["selected_proposal"]
            c.need(any(c.same_json(proposal, p) for p in controller.legal_actions(state, exchange._now())["actions"]), "selected action not currently legal")
            role = proposal["role"]
            request = {"schema": exchange.REQUEST_SCHEMA, "session_id": state["session_id"], "operation_id": record["operation_id"],
                "expected_event_sequence": base["sequence"], "request_id": record["request_id"], "logical_action_id": record["logical_action_id"],
                "action_id": record["action_id"], "prepared_state_revision": state["state_revision"], "kind": proposal["kind"],
                "role": role, "actor_id": base["manifest"]["role_bindings"][role]["actor_id"],
                "route_id": proposal["route_id"], "obligation_id": proposal["obligation_id"],
                "context": _context(projection, proposal, base["manifest"]), "dependency_refs": proposal["dependency_refs"],
                "template": _template(state, proposal)}
            new, _, created = self._append(db, base, "prepare", request, {"packet_id": "packet_" + uuid4().hex})
            request = new["requests"][record["request_id"]]
            result = {"request_id": request["request_id"], "packet_id": request["packet"]["binding"]["packet_id"],
                      "prepared_new": created, "dispatch_claim": None, "controller_admission_verified": True}
        self._checkpoint("prepare_committed", record["request_id"])
        return result

    def _control(self, kind, record):
        record = c.copy_json(record)
        with self._tx() as (db, base):
            prior = self._verified_projection(db)
            existing = db.execute("SELECT payload FROM session_journal WHERE operation_id=?", (record["operation_id"],)).fetchone()
            if existing is not None:
                row = exchange._json(existing[0])
                c.need(row["kind"] == kind and c.same_json(row["record"], record), "domain operation reused differently")
                return {"recorded_new": False, "session_sequence": prior["journal_sequence"]}
            entry = {"sequence": prior["journal_sequence"] + 1, "event_id": "domain_" + uuid4().hex,
                     "operation_id": record["operation_id"], "recorded_at_utc": exchange._now(), "kind": kind, "record": record}
            projection, derived = _derive_control(prior, base, entry)
            self._save_projection(db, prior, projection, entry, derived)
        return {"recorded_new": True, "session_sequence": projection["journal_sequence"], "model_consumption_verified": False}

    def release_feedback(self, record):
        return self._control("release_feedback", record)

    def receive_feedback(self, record):
        return self._control("receive_feedback", record)

    def release_planning_evidence(self, record):
        return self._control("release_planning_evidence", record)

    def receive_planning_evidence(self, record):
        return self._control("receive_planning_evidence", record)

    def finalize(self, record):
        return self._control("finalize", record)

    def ingest(self, record, raw_bytes=None):
        result = super().ingest(record, raw_bytes)
        with self._tx() as (db, base):
            row = db.execute("SELECT payload FROM session_journal WHERE operation_id=?", (record["operation_id"],)).fetchone()
            derived = exchange._json(row[0])["derived"]
            result.update(projection_status=derived["classification"], projection_error=derived["error"],
                          adaptive_projection_performed=derived["domain_event"] is not None)
        return result

    def snapshot(self):
        with self._tx() as (db, base):
            return {"schema": seed.SESSION_VERSION if base['manifest']['schema'] == seed.MANIFEST_SCHEMA else SESSION_VERSION,
                    "audience": "coordinator_only", "exchange": deepcopy(base),
                    **deepcopy(self._verified_projection(db)), "mathematical_truth_verified": False}

    def status(self):
        with self._tx() as (db, base):
            projection = self._verified_projection(db); state = projection["state"]
            result = {"schema": seed.SESSION_VERSION if seed.is_state(state) else SESSION_VERSION,
                    "audience": "metadata_only", "session_id": state["session_id"],
                    "session_sequence": projection["journal_sequence"], "state_revision": state["state_revision"],
                    "event_sequence": base["sequence"], "sealed": projection["endpoint"] is not None,
                    "domain_actions": len(state["actions"]), "host_bound_calls": sum(row["host_reference"] is not None for row in base["requests"].values()),
                    "candidate_count": len(state["candidates"]), "issue_count": len(state["issues"]),
                    "budget": exchange._budgets(base), "mathematical_truth_verified": False,
                    "remote_shutdown_verified": False, "external_calls_performed": 0}
            if seed.is_state(state):
                result.update(workflow_profile=seed.PROFILE, input_import_count=1,
                              initial_critique_completed=seed.critique_ready(state))
            return result
