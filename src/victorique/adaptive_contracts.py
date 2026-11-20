"""Pure, bounded contracts for proposed adaptive proof work.

No I/O, model execution, proof verification, or authentication is performed here.
All source identities are declarations; structural validity is not a proof.
"""
from datetime import datetime
import json
import re


class AdaptiveContractError(ValueError):
    pass


CALLS = ("propose_plan", "critique_candidate", "investigate", "solve_obligation",
         "repair_obligation", "integrate", "replan", "alignment_review", "whole_proof_review")
GATES = ("alignment_review", "whole_proof_review")
ROLES = ("planner", "solver", "critic", "investigator", "integrator",
         "alignment_reviewer", "proof_reviewer")
DEFAULT_ROLES = dict(zip(CALLS, ("planner", "critic", "investigator", "solver", "solver",
                              "integrator", "planner", "alignment_reviewer", "proof_reviewer")))
OPTIONAL_ROLES = {"compare_candidates": "critic"}
REF_FIELDS = {"artifact_id", "revision", "plan_id", "plan_revision", "route_id", "obligation_id"}
LIMIT_FIELDS = {"max_routes", "max_obligations_per_plan", "max_dag_depth", "max_plan_revisions",
                "max_candidate_revisions", "max_repairs_per_issue", "max_investigations_per_issue",
                "max_critiques_per_candidate", "global_call_limit", "role_call_limits", "max_inflight",
                "total_budget_units", "role_budget_units", "action_caps", "action_roles",
                "required_gate_caps", "stop_condition"}
# How a gate report that says pass, keeps no doubt and lists only minor findings is
# read. The first value is the default; limits without the optional key use it.
MINOR_FINDINGS = ("nonblocking", "blocking")


def need(condition, message):
    if not condition:
        raise AdaptiveContractError(message)


def fields(value, expected, name):
    need(type(value) is dict and set(value) == set(expected), "invalid " + name + " fields")


def identifier(value, name="identity"):
    need(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value) is not None,
         "invalid " + name)


def integer(value, name, low=0, high=10000):
    need(type(value) is int and low <= value <= high, "invalid " + name)


def text(value, name, cap=256000, empty=False):
    need(type(value) is str and (empty or bool(value.strip())), "invalid " + name)
    try:
        need(len(value.encode("utf-8")) <= cap, name + " exceeds byte limit")
    except UnicodeError as exc:
        raise AdaptiveContractError("invalid UTF-8 " + name) from exc


def utc(value):
    text(value, "UTC timestamp", 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        need(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0, "UTC timestamp required")
        return parsed
    except (ValueError, OverflowError) as exc:
        raise AdaptiveContractError("invalid UTC timestamp") from exc


def copy_json(value):
    def visit(item, depth=0):
        need(depth <= 30, "JSON nesting limit")
        if type(item) is dict:
            need(all(type(k) is str for k in item), "JSON keys must be strings")
            for child in item.values():
                visit(child, depth + 1)
        elif type(item) is list:
            need(len(item) <= 10000, "JSON list limit")
            for child in item:
                visit(child, depth + 1)
        else:
            need(item is None or type(item) in (str, int, float, bool), "invalid JSON value")
    try:
        visit(value)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        text(raw, "JSON", 4_000_000)
        return json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, AdaptiveContractError):
            raise
        raise AdaptiveContractError("invalid bounded finite JSON") from exc


def same_json(left, right):
    """Type-sensitive equality for already bounded JSON values.

    In particular True, 1 and 1.0 are three different contract values.
    """
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(same_json(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return type(left) in (str, int, float, bool, type(None)) and left == right


def parse_json(raw):
    text(raw, "JSON input", 4_000_000)
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        return copy_json(json.loads(raw, object_pairs_hook=pairs))
    except (ValueError, RecursionError) as exc:
        raise AdaptiveContractError("invalid strict JSON") from exc


def strings(values, name, *, ids=False, maximum=64):
    need(type(values) is list and len(values) <= maximum, "invalid " + name)
    for value in values:
        identifier(value, name) if ids else text(value, name, 10000)
    need(len(values) == len(set(values)), "duplicate " + name)


def problem_ref(value):
    fields(value, {"artifact_id", "revision"}, "problem reference")
    identifier(value["artifact_id"])
    integer(value["revision"], "problem revision", 1, 64)


def reference(value):
    fields(value, REF_FIELDS, "candidate reference")
    for key in REF_FIELDS - {"revision", "plan_revision"}:
        identifier(value[key], key)
    integer(value["revision"], "candidate revision", 1, 64)
    integer(value["plan_revision"], "plan revision", 1, 64)
    return copy_json(value)


def candidate_ref(candidate):
    return {key: candidate[key] for key in REF_FIELDS}


def validate_problem(value):
    value = copy_json(value)
    fields(value, {"artifact_id", "revision", "content"}, "problem")
    problem_ref({key: value[key] for key in ("artifact_id", "revision")})
    text(value["content"], "problem content")
    return value


def validate_limits(value):
    value = copy_json(value)
    optional = {"minor_findings"} if type(value) is dict and "minor_findings" in value else set()
    fields(value, LIMIT_FIELDS | optional, "adaptive limits")
    if optional:
        need(type(value["minor_findings"]) is str and value["minor_findings"] in MINOR_FINDINGS,
             "unsupported minor_findings")
    bounds = {"max_routes": 8, "max_obligations_per_plan": 64, "max_dag_depth": 32,
              "max_plan_revisions": 16, "max_candidate_revisions": 64,
              "max_repairs_per_issue": 8, "max_investigations_per_issue": 16,
              "max_critiques_per_candidate": 4, "global_call_limit": 512, "max_inflight": 16}
    for key, upper in bounds.items():
        integer(value[key], key, 0 if key == "max_critiques_per_candidate" else 1, upper)
    integer(value["total_budget_units"], "total budget", 1, 10**12)
    for key in ("role_call_limits", "role_budget_units"):
        fields(value[key], ROLES, key)
        for amount in value[key].values():
            integer(amount, key, 0, 10**12 if key == "role_budget_units" else 512)
    actions = set(value["action_caps"]) if type(value["action_caps"]) is dict else set()
    need(set(CALLS) <= actions <= set(CALLS) | set(OPTIONAL_ROLES), "invalid action caps fields")
    for cap in value["action_caps"].values():
        integer(cap, "action cap", 1, 10**12)
    fields(value["action_roles"], actions, "action roles")
    for kind, role in value["action_roles"].items():
        permitted = {(DEFAULT_ROLES | OPTIONAL_ROLES)[kind]}
        if kind in {"critique_candidate", "investigate"}:
            permitted |= {"alignment_reviewer", "proof_reviewer"}
        need(type(role) is str and role in permitted, "unsupported action role")
    fields(value["required_gate_caps"], GATES, "required gate caps")
    need(all(type(value["required_gate_caps"][g]) is int
             and value["required_gate_caps"][g] == value["action_caps"][g] for g in GATES), "gate caps differ")
    fields(value["stop_condition"], {"deadline_utc"}, "stop condition")
    if value["stop_condition"]["deadline_utc"] is not None:
        utc(value["stop_condition"]["deadline_utc"])
    need(sum(value["required_gate_caps"].values()) <= value["total_budget_units"], "gate reserve exceeds budget")
    return value


def nodes(plan):
    return {(route["route_id"], node["obligation_id"]): node
            for route in plan["routes"] for node in route["obligations"]}


def validate_plan(value, problem, limits, prior=None):
    problem = validate_problem(problem)
    limits = validate_limits(limits)
    value = copy_json(value)
    fields(value, {"schema", "plan_id", "revision", "parent_revision", "problem_ref", "routes"}, "plan")
    need(value["schema"] == "victorique.adaptive_plan.v1", "unsupported plan schema")
    identifier(value["plan_id"])
    integer(value["revision"], "plan revision", 1, limits["max_plan_revisions"])
    problem_ref(value["problem_ref"])
    need(same_json(value["problem_ref"], {k: problem[k] for k in ("artifact_id", "revision")}), "plan problem differs")
    if prior is None:
        need(value["revision"] == 1 and value["parent_revision"] is None, "initial plan must start at one")
    else:
        need(value["plan_id"] == prior["plan_id"] and type(value["parent_revision"]) is int
             and value["parent_revision"] == prior["revision"]
             and value["revision"] == prior["revision"] + 1, "plan revision CAS mismatch")
    need(type(value["routes"]) is list and 1 <= len(value["routes"]) <= limits["max_routes"], "route limit")
    route_ids, total = set(), 0
    for route in value["routes"]:
        fields(route, {"route_id", "root_obligation_id", "obligations"}, "route")
        identifier(route["route_id"]); identifier(route["root_obligation_id"])
        need(route["route_id"] not in route_ids, "duplicate route")
        route_ids.add(route["route_id"])
        need(type(route["obligations"]) is list and bool(route["obligations"]), "empty obligations")
        total += len(route["obligations"])
        need(total <= limits["max_obligations_per_plan"], "obligation limit")
        graph = {}
        for node in route["obligations"]:
            fields(node, {"obligation_id", "statement", "assumptions", "depends_on", "claimed_goal_coverage", "failure_conditions"}, "obligation")
            identifier(node["obligation_id"])
            need(node["obligation_id"] not in graph, "duplicate obligation")
            graph[node["obligation_id"]] = node
            text(node["statement"], "obligation statement", 20000)
            for key in ("assumptions", "claimed_goal_coverage", "failure_conditions"):
                strings(node[key], key)
            strings(node["depends_on"], "dependencies", ids=True)
        visited, visiting, depths = set(), set(), {}
        def walk(oid):
            need(oid in graph and oid not in visiting, "unknown or cyclic obligation")
            if oid in depths:
                return depths[oid]
            visiting.add(oid)
            depth = 1 + max((walk(dependency) for dependency in graph[oid]["depends_on"]), default=0)
            need(depth <= limits["max_dag_depth"], "DAG depth limit")
            visiting.remove(oid); visited.add(oid)
            depths[oid] = depth
            return depth
        walk(route["root_obligation_id"])
        need(visited == set(graph), "orphan obligation outside root closure")
        need(bool(graph[route["root_obligation_id"]]["claimed_goal_coverage"]), "root requires explicit coverage claim")
    graph = nodes(value)
    for (route_id, obligation_id), node in graph.items():
        for dependency_id in node["depends_on"]:
            dependency = graph[(route_id, dependency_id)]
            need(set(dependency["assumptions"]).issubset(set(node["assumptions"])),
                 f"plan dependency scope incompatible: route={route_id}, "
                 f"consumer={obligation_id}, dependency={dependency_id}; "
                 "dependency assumptions must be a subset of consumer assumptions")
    return value


def validate_candidate(value, plan, limits):
    """Validate against a previously validated immutable plan and limits."""
    limits = validate_limits(limits)
    value = copy_json(value)
    fields(value, {"schema", *REF_FIELDS, "parent_revision", "content", "assumptions", "dependency_refs", "addressed_issue_ids"} | ({"assessments"} if "assessments" in value else set()), "candidate")
    need(value["schema"] == "victorique.adaptive_candidate.v1", "unsupported candidate schema")
    reference(candidate_ref(value))
    need(same_json(value["plan_id"], plan["plan_id"]) and same_json(value["plan_revision"], plan["revision"]), "candidate plan differs")
    integer(value["revision"], "candidate revision", 1, limits["max_candidate_revisions"])
    need((value["revision"] == 1 and value["parent_revision"] is None)
         or (value["revision"] > 1 and type(value["parent_revision"]) is int
             and value["parent_revision"] == value["revision"] - 1), "candidate parent differs")
    text(value["content"], "candidate content")
    strings(value["assumptions"], "candidate assumptions")
    strings(value["addressed_issue_ids"], "addressed issues", ids=True)
    if "assessments" in value:
        need(type(value["assessments"]) is list and len(value["assessments"]) <= 64, "bounded repair assessments required")
        seen = set()
        for assessment in value["assessments"]:
            fields(assessment, {"issue_id", "decision", "reason"}, "repair assessment")
            identifier(assessment["issue_id"])
            need(assessment["issue_id"] not in seen, "duplicate repair assessment")
            seen.add(assessment["issue_id"])
            need(assessment["decision"] in ("confirmed", "refuted", "unresolved"), "repair assessment decision")
            text(assessment["reason"], "repair assessment reason")
    graph = nodes(plan); key = (value["route_id"], value["obligation_id"])
    need(key in graph, "candidate obligation missing")
    need(same_json(value["assumptions"], graph[key]["assumptions"]), "candidate declared assumptions differ from obligation")
    refs = value["dependency_refs"]
    need(type(refs) is list and len(refs) <= 64, "invalid dependency references")
    for ref in refs:
        reference(ref)
        need(same_json(ref["plan_id"], plan["plan_id"]) and same_json(ref["plan_revision"], plan["revision"])
             and same_json(ref["route_id"], value["route_id"]), "dependency plan or route differs")
    need(len(refs) == len(graph[key]["depends_on"])
         and {r["obligation_id"] for r in refs} == set(graph[key]["depends_on"]), "immediate dependencies differ")
    return value


def validate_location(value, content):
    fields(value, {"start_byte", "end_byte", "quote"}, "location")
    text(value["quote"], "location quote", 20000)
    raw = content.encode("utf-8")
    integer(value["start_byte"], "start byte", 0, len(raw))
    integer(value["end_byte"], "end byte", value["start_byte"] + 1, len(raw))
    need(raw[value["start_byte"]:value["end_byte"]] == value["quote"].encode("utf-8"), "quote does not match exact UTF-8 bytes")


def validate_defect(value, target):
    value = copy_json(value)
    fields(value, {"schema", "issue_id", "target_ref", "kind", "severity", "location", "claim"}, "defect")
    need(value["schema"] == "victorique.adaptive_defect.v1", "unsupported defect schema")
    identifier(value["issue_id"]); reference(value["target_ref"])
    need(same_json(value["target_ref"], candidate_ref(target)), "defect target differs")
    need(type(value["kind"]) is str and value["kind"] in {"goal_gap", "invalid_step", "missing_case", "assumption_mismatch", "dependency_interface", "ambiguity"}, "unsupported defect kind")
    need(type(value["severity"]) is str and value["severity"] in {"fatal", "material", "minor"}, "unsupported defect severity")
    text(value["claim"], "defect claim", 20000)
    if value["location"] is not None:
        validate_location(value["location"], target["content"])
    return value


def minor_findings_rule(limits):
    """The minor_findings value frozen in a run's limits; limits without it use nonblocking."""
    value = limits.get("minor_findings", MINOR_FINDINGS[0])
    need(value in MINOR_FINDINGS, "unsupported minor_findings")
    return value


def _passes_with_minor_only(report):
    """Outcome pass, no unresolved doubt, and no fatal or material finding."""
    return (report["outcome"] == "pass" and not report["uncertainties"]
            and all(finding["severity"] == "minor" for finding in report["findings"]))


def gate_passes(report, minor_findings=MINOR_FINDINGS[0]):
    """One passing rule for a validated gate report.

    A pass may carry minor findings (wording, presentation) or unresolved doubts;
    the report passes only when it says pass, keeps no unresolved doubt and has
    no fatal or material finding. With ``minor_findings="blocking"`` it must
    also list no minor finding.
    """
    need(minor_findings in MINOR_FINDINGS, "unsupported minor_findings")
    return _passes_with_minor_only(report) and (minor_findings == "nonblocking" or not report["findings"])


def blocking_findings(report, minor_findings=MINOR_FINDINGS[0]):
    """The findings of a validated gate report that block delivery and go to repair.

    Fatal and material findings always block. With ``minor_findings="blocking"``
    the minor findings of a report that says pass with no doubt block as well. A
    report that says fail, or keeps a doubt, with only minor findings lists no
    blocking finding under either value.
    """
    need(minor_findings in MINOR_FINDINGS, "unsupported minor_findings")
    if minor_findings == "blocking" and _passes_with_minor_only(report):
        return list(report["findings"])
    return [finding for finding in report["findings"] if finding["severity"] != "minor"]


def validate_disposition(value, issue_id, target):
    value = copy_json(value)
    fields(value, {"schema", "issue_id", "target_ref", "decision", "reason", "location"}, "disposition")
    need(value["schema"] == "victorique.adaptive_disposition.v1", "unsupported disposition schema")
    reference(value["target_ref"])
    need(same_json(value["issue_id"], issue_id) and same_json(value["target_ref"], candidate_ref(target)), "disposition binding differs")
    need(type(value["decision"]) is str and value["decision"] in {"confirmed", "refuted", "unresolved"}, "unsupported disposition")
    text(value["reason"], "disposition reason", 20000)
    need(value["decision"] != "confirmed" or value["location"] is not None, "confirmed defect needs exact location")
    if value["location"] is not None:
        validate_location(value["location"], target["content"])
    return value
