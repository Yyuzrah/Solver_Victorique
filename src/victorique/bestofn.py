"""Sequential independent sampling and a selection-only pairwise tournament."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from . import adaptive_contracts as c, adaptive_exchange as exchange, adaptive_roles, retries
from .harness.safety import checked_path
from .harness_bridge import AdaptiveHarnessBridge
from .single import SingleLoop, _output, _result
from .stop_request import REASON as STOPPED
from .telemetry import call_rows


METHOD = "sequential_observed_mean_reserve_v1"
ESTIMATE_ROLES = ("solver", "critic", "alignment_reviewer", "proof_reviewer")


def measured_tokens(row):
    """Input includes cache; reasoning output is already part of output tokens."""
    usage = row.get("tokens")
    if type(usage) is not dict:
        return None
    parts = [usage.get(key) for key in ("input_tokens_including_cache", "output_tokens")]
    if any(type(value) is not int or value < 0 for value in parts):
        return None
    return sum(parts)


def _full_reference(full_calls):
    """Read only named public run files, including the terminal summary."""
    path = checked_path(Path(full_calls).absolute())
    if path.name != "calls.jsonl":
        raise ValueError("full-calls must name the paired full run's calls.jsonl")
    directory = path.parent
    def read(name):
        return c.parse_json(checked_path(directory / name).read_text(encoding="utf-8"))
    invocation = read("invocation.json")
    config = read("state/run-config.json")
    c.need(type(invocation) is dict and type(invocation.get("parameters")) is dict,
           "invalid full invocation")
    c.need(type(config) is dict and type(config.get("manifest")) is dict,
           "invalid full run config")
    manifest = config["manifest"]
    c.need(type(manifest.get("problem")) is dict and type(manifest["problem"].get("content")) is str,
           "full original problem is unavailable")
    c.identifier(manifest.get("session_id"), "full run identity")
    if invocation["parameters"].get("policy", "full") != "full":
        raise ValueError("token reference must be a full run")
    try:
        summary = read("summary.json")
    except FileNotFoundError:
        summary = None
    terminal = False
    summary_calls = None
    if summary is not None:
        c.need(type(summary) is dict and summary.get("run_id") == manifest["session_id"]
               and summary.get("policy") == "full", "full summary belongs to an unrelated run")
        terminal = summary.get("outcome") in ("complete", "abstain", "partial")
        if "calls_used" in summary:
            summary_calls = summary["calls_used"]
            c.integer(summary_calls, "full summary calls_used", 0, 100)
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if len(rows) >= 100 or len(line.encode("utf-8")) > 64000:
                raise ValueError("full calls projection exceeds bounds")
            rows.append(c.parse_json(line))
    seen = set()
    values = {role: [] for role in ESTIMATE_ROLES}
    costs = []
    for row in rows:
        c.need(type(row) is dict, "full call must be an object")
        if row.get("policy") != "full" or row.get("run_id") != manifest["session_id"]:
            raise ValueError("full calls projection contains an unrelated run")
        rid = row.get("request_id")
        c.identifier(rid, "full request ID")
        if rid in seen:
            raise ValueError("duplicate full call")
        seen.add(rid)
        cost = measured_tokens(row)
        status = row.get("status")
        c.need(status in ("completed", "failed", "timed_out", "returned_invalid", "returned_invalid_model", "unknown",
                          "stopped"), "invalid full call status")
        if status in ("unknown", "stopped"):
            # A call ended by a stop request has no reported usage.
            cost = None
        costs.append(cost)
        if cost is not None and type(row.get("role")) is str and row["role"] in values:
            values[row["role"]].append(cost)
    return {"path": str(path), "run_id": manifest["session_id"], "problem": manifest["problem"]["content"],
            "total": sum(costs) if terminal and summary_calls == len(rows)
                     and all(cost is not None for cost in costs) else None,
            "values": values, "calls": len(rows), "terminal": terminal and summary_calls == len(rows)}


def estimate_from_full(full_calls, problem_content, calls, full_references=None):
    """Freeze the target and role cost estimates from an explicit reference list."""
    if full_calls is None:
        raise ValueError("bestofn requires --full-calls from the paired full run")
    paired = _full_reference(full_calls)
    if paired["problem"] != problem_content:
        raise ValueError("full token reference belongs to a different original problem")
    references = []
    difficulty = None
    if full_references is not None:
        source = checked_path(Path(full_references).absolute())
        listing = c.parse_json(source.read_text(encoding="utf-8"))
        c.fields(listing, {"difficulty", "runs"}, "full references")
        difficulty = listing["difficulty"]
        if difficulty is not None:
            c.text(difficulty, "difficulty", 120)
        c.need(type(listing["runs"]) is list and len(listing["runs"]) <= 10000, "invalid full reference runs")
        seen = {paired["run_id"]}
        for entry in listing["runs"]:
            c.fields(entry, {"calls", "difficulty"}, "full reference entry")
            c.text(entry["calls"], "full calls path", 10000)
            if entry["difficulty"] is not None:
                c.text(entry["difficulty"], "difficulty", 120)
            reference = _full_reference(source.parent / entry["calls"])
            c.need(reference["run_id"] not in seen, "duplicate full reference run")
            seen.add(reference["run_id"])
            references.append({**reference, "difficulty": entry["difficulty"]})
    target = paired["total"]
    fallback = "paired_full" if target is not None else "unavailable"
    sources = [paired["run_id"]] if target is not None else []
    if target is None:
        for kind, pool in (
            ("same_problem_median", [ref for ref in references if ref["problem"] == problem_content]),
            ("same_difficulty_median", [ref for ref in references if difficulty is not None and ref["difficulty"] == difficulty]),
        ):
            measured = [ref for ref in pool if ref["total"] is not None]
            if measured:
                target = median(ref["total"] for ref in measured)
                fallback, sources = kind, [ref["run_id"] for ref in measured]
                break
    means, sample_counts, role_sources = {}, {}, {}
    for role in ESTIMATE_ROLES:
        values = paired["values"][role]
        amount = mean(values) if values else None
        kind, run_ids = "paired_full_mean", [paired["run_id"]]
        if amount is None:
            for source_kind, pool in (
                ("same_difficulty_full_median", [ref for ref in references if difficulty is not None and ref["difficulty"] == difficulty]),
                ("all_full_median", references),
            ):
                measured = [ref for ref in pool if ref["terminal"] and ref["values"][role]]
                values = [cost for ref in measured for cost in ref["values"][role]]
                if values:
                    amount = median(values)
                    kind, run_ids = source_kind, [ref["run_id"] for ref in measured]
                    break
        if amount is None:
            kind, run_ids = "current_generation_mean", []
        means[role], sample_counts[role] = amount, len(values)
        role_sources[role] = {"kind": kind, "run_ids": run_ids,
                              "unit": "generated_manuscripts" if kind == "current_generation_mean" else "measured_role_calls"}
    reason = "missing_target_tokens" if target is None else None
    estimate = {"method": METHOD, "full_calls": paired["path"], "full_run_id": paired["run_id"],
                "target_tokens": target, "reference_calls": paired["calls"],
                "target_fallback": fallback, "target_source_run_ids": sources,
                "difficulty": difficulty, "unexecuted_reason": reason,
                "role_mean_tokens": means, "role_sample_counts": sample_counts, "role_sources": role_sources,
                "minimum_candidates": 2, "token_metric": "input_tokens_including_cache + output_tokens"}
    validate_estimate(estimate, calls)
    return estimate


def validate_estimate(value, calls):
    import math
    extended = "target_fallback" in value
    extra = {"target_fallback", "target_source_run_ids", "difficulty", "unexecuted_reason"} if extended else set()
    role_fallback = "role_sources" in value
    if role_fallback:
        c.need(extended, "role fallback requires frozen target metadata")
        extra.add("role_sources")
    c.fields(value, {"method", "full_calls", "full_run_id", "target_tokens", "reference_calls",
                     "role_mean_tokens", "role_sample_counts", "minimum_candidates", "token_metric"} | extra, "bestofn estimate")
    c.need(value["method"] == METHOD and value["minimum_candidates"] == 2
           and value["token_metric"] == "input_tokens_including_cache + output_tokens", "unsupported bestofn estimate method")
    c.text(value["full_calls"], "full calls path", 10000)
    c.identifier(value["full_run_id"], "reference run")
    target = value["target_tokens"]
    if target is not None or not extended:
        c.need(type(target) in (int, float) and math.isfinite(target) and 0 <= target <= 10**15, "invalid reference target tokens")
    c.integer(value["reference_calls"], "reference calls", 0 if extended else 1, 100)
    for key in ("role_mean_tokens", "role_sample_counts"):
        c.fields(value[key], ESTIMATE_ROLES, key)
    for role, amount in value["role_mean_tokens"].items():
        if amount is not None or not extended:
            c.need(type(amount) in (int, float) and math.isfinite(amount) and 0 <= amount <= 10**15
                   and (role_fallback or amount > 0), "missing/invalid role cost estimate")
        c.integer(value["role_sample_counts"][role], "role cost samples", 0 if amount is None else 1,
                  1000000 if role_fallback else value["reference_calls"])
    if role_fallback:
        c.fields(value["role_sources"], ESTIMATE_ROLES, "role sources")
        for role, source in value["role_sources"].items():
            c.fields(source, {"kind", "run_ids", "unit"}, "role source")
            c.need(source["kind"] in ("paired_full_mean", "same_difficulty_full_median", "all_full_median", "current_generation_mean"),
                   "invalid role source kind")
            ids = source["run_ids"]
            c.need(type(ids) is list and len(ids) <= 10000, "invalid role source runs")
            for run_id in ids:
                c.identifier(run_id, "role source run")
            c.need(len(ids) == len(set(ids)), "duplicate role source run")
            if source["kind"] == "current_generation_mean":
                c.need(value["role_mean_tokens"][role] is None and value["role_sample_counts"][role] == 0 and not ids,
                       "current generation estimate must remain unfrozen")
            else:
                c.need(value["role_mean_tokens"][role] is not None and bool(ids), "missing external role estimate")
            c.need(source["unit"] == ("generated_manuscripts" if source["kind"] == "current_generation_mean" else "measured_role_calls"),
                   "invalid role sample unit")
            if source["kind"] == "paired_full_mean":
                c.need(ids == [value["full_run_id"]] and value["role_sample_counts"][role] <= value["reference_calls"],
                       "invalid paired role source")
            if source["kind"] == "same_difficulty_full_median":
                c.need(value["difficulty"] is not None, "missing role fallback difficulty")
    if extended:
        kind, sources = value["target_fallback"], value["target_source_run_ids"]
        c.need(kind in ("paired_full", "same_problem_median", "same_difficulty_median", "unavailable"), "invalid target fallback")
        c.need(type(sources) is list and len(sources) <= 10000, "invalid target source runs")
        for source in sources:
            c.identifier(source, "target source run")
        c.need(len(set(sources)) == len(sources), "duplicate target source run")
        c.need((target is None) == (kind == "unavailable") == (not sources), "inconsistent target fallback")
        if kind == "paired_full":
            c.need(sources == [value["full_run_id"]], "invalid paired target source")
        if value["difficulty"] is not None:
            c.text(value["difficulty"], "difficulty", 120)
        c.need(kind != "same_difficulty_median" or value["difficulty"] is not None, "missing fallback difficulty")
        missing = any(amount is None for amount in value["role_mean_tokens"].values())
        reason = "missing_target_tokens" if target is None else "missing_role_estimates" if missing and not role_fallback else None
        c.need(value["unexecuted_reason"] == reason, "inconsistent unexecuted reason")
    c.need(calls >= 5, "bestofn requires at least 5 calls: 2 solves, 1 comparison and 2 final reviews")
    return value


def selection_schema():
    ref = {"type": "object", "additionalProperties": False,
           "required": sorted(c.REF_FIELDS),
           "properties": {key: {"type": "integer" if key in ("revision", "plan_revision") else "string"}
                          for key in sorted(c.REF_FIELDS)}}
    return {"type": "object", "additionalProperties": False,
            "required": ["schema", "outcome", "winner_ref", "reason"],
            "properties": {"schema": {"type": "string", "enum": ["victorique.adaptive_selection.v1"]},
                           "outcome": {"type": "string", "enum": ["selected", "unknown"]},
                           "winner_ref": {"anyOf": [ref, {"type": "null"}]}, "reason": {"type": "string"}}}


def _candidates(base):
    return [(rid, _output(base, rid)) for rid, row in base["requests"].items()
            if row["record"]["kind"] == "solve_obligation" and _output(base, rid) is not None]


def _failed_generation(base):
    """The generation request that ended without a valid manuscript and was not sent again, if any."""
    return next((rid for rid, row in base["requests"].items()
                 if row["record"]["kind"] == "solve_obligation" and row["phase"] in exchange.TERMINAL
                 and row["phase"] != "returned_valid" and not retries.superseded(base, rid)), None)


def _winner(base):
    candidates = _candidates(base)
    if not candidates:
        return None
    winner = candidates[0][1]
    for index in range(1, len(candidates)):
        result = _result(base, f"bestofn.compare.{index}")
        if result is None or result["outcome"] != "selected":
            break
        if c.same_json(result["winner_ref"], c.candidate_ref(candidates[index][1])):
            winner = candidates[index][1]
    return winner


class _BestofnExchange(exchange.AdaptiveExchange):
    def snapshot(self):
        base = super().snapshot()
        endpoint = base["endpoint"]
        if endpoint is not None:
            candidate = _winner(base)
            decision = ("unexecuted" if endpoint["reason"] == "bestofn_unexecuted" else
                        "complete" if endpoint["reason"] == "bestofn_complete" else
                        "stop" if endpoint["reason"] == STOPPED else "abstain")
            endpoint = {**endpoint,
                        "reducer_endpoint": {"decision": decision},
                        "root_candidate": candidate,
                        "root_dependency_closure": [] if candidate is None else [candidate]}
        return {"exchange": base, "endpoint": endpoint}


class _BestofnBridge(AdaptiveHarnessBridge):
    core_type = _BestofnExchange


class BestofnLoop(SingleLoop):
    core_type = _BestofnExchange
    bridge_type = _BestofnBridge

    def _compare(self, base, winner, challenger, index):
        self._check_stop()
        # Copy only a clean generation's literal original-problem plan, never
        # any earlier comparison rationale, review or reference-run manuscript.
        sources = {row["record"]["context"]["plan"]["plan_id"]: row["record"]
                   for row in base["requests"].values() if row["record"]["kind"] == "solve_obligation"}
        record = deepcopy(sources[winner["plan_id"]])
        rid = f"bestofn.compare.{index}"
        kind = "compare_candidates"
        template = adaptive_roles.template(kind)
        instructions = {"common": template["common_instructions"], "role": template["instructions"],
                        "alignment_standard": adaptive_roles.INSTRUCTIONS["alignment_review"],
                        "proof_standard": adaptive_roles.INSTRUCTIONS["whole_proof_review"],
                        "output_contract": adaptive_roles.output_contract(kind),
                        "response_format": "Return the exact schema/binding/output envelope. Select only; never rewrite either manuscript. Standards guide preference; only the later independent gates decide delivery."}
        role = base["manifest"]["limits"]["action_roles"][kind]
        record.update(operation_id=rid + ".prepare", expected_event_sequence=base["sequence"],
                      request_id=rid, logical_action_id=rid, action_id=rid, prepared_state_revision=len(base["requests"]),
                      kind=kind, role=role, actor_id=base["manifest"]["role_bindings"][role]["actor_id"],
                      template={"template_id": template["template_id"], "version": "v2", "instructions": exchange._dump(instructions)})
        record["context"]["target_candidate"] = winner
        record["context"]["opponent"] = {"candidate": challenger,
                                           "plan": deepcopy(sources[challenger["plan_id"]]["context"]["plan"])}
        self.core.prepare(record)
        return {"phase": "native_prepared", "request_id": rid, "kind": kind}

    def _usage(self):
        return call_rows(self, "bestofn")

    def _role_estimates(self, rows, generation_ids=None):
        """Derive current-run proxies without rewriting frozen reference costs."""
        estimate = self.config["bestofn"]
        generated = [row for row in rows if (row["request_id"] in generation_ids if generation_ids is not None
                                            else row["request_id"].startswith("bestofn.solve."))]
        costs = [measured_tokens(row) for row in generated]
        measured = bool(costs) and all(cost is not None for cost in costs)
        if generation_ids is not None:
            measured = measured and len(costs) == len(generation_ids)
        result = {}
        for role, amount in estimate["role_mean_tokens"].items():
            source = estimate.get("role_sources", {}).get(role, {
                "kind": "paired_full_mean", "run_ids": [estimate["full_run_id"]] if "full_run_id" in estimate else [],
                "unit": "measured_role_calls"})
            count = estimate.get("role_sample_counts", {}).get(role, 0)
            if source["kind"] == "current_generation_mean" and measured:
                amount, count = mean(costs), len(costs)
                source = {**source, "run_ids": [self.config["manifest"]["session_id"]]}
            result[role] = {"tokens": amount, "sample_count": count, **source}
        return result

    def _generation_stop(self, base, count, rows=None):
        """Stop expansion on cost or call limits, or incomplete measurements."""
        # Calls sent again after a timeout have used part of the call budget; the
        # timed-out requests they replaced produced no manuscript to measure.
        requests = base.get("requests", {})
        replaced = {rid for rid in requests if retries.next_id(rid) in requests}
        if 2 * (count + 1) + 1 + retries.used(requests) > base["manifest"]["limits"]["global_call_limit"]:
            return "call_budget"
        rows = self._usage() if rows is None else rows
        costs = [measured_tokens(row) for row in rows
                 if row["request_id"].startswith("bestofn.solve.") and row["request_id"] not in replaced]
        if len(costs) != count or any(cost is None for cost in costs):
            return "missing_usage"
        generated = None if not replaced else {row["request_id"] for row in rows if row["request_id"].startswith(
            "bestofn.solve.") and row["request_id"] not in replaced}
        means = {role: value["tokens"] for role, value in self._role_estimates(rows, generated).items()}
        if any(amount is None for amount in means.values()):
            return "missing_usage"
        next_generation = mean(costs) if count >= 2 else means["solver"]
        projected = (sum(costs) + next_generation + count * means["critic"]
                     + means["alignment_reviewer"] + means["proof_reviewer"])
        return "token_target" if projected > self.config["bestofn"]["target_tokens"] else None

    def step(self):
        snapshot = self.core.snapshot()
        base = snapshot["exchange"]
        if snapshot["endpoint"] is not None:
            return {"phase": "native_endpoint", "status": self.status()}
        if self.config["bestofn"].get("unexecuted_reason") is not None:
            return self._finish(base, "bestofn_unexecuted")
        if datetime.now(timezone.utc) >= c.utc(base["manifest"]["limits"]["stop_condition"]["deadline_utc"]):
            return self._finish(base, "bestofn_deadline")
        pending = self._pending(base)
        if pending:
            return pending
        requests = base["requests"]
        # A failed generation (for example a timed-out solve) ends expansion but keeps
        # the manuscripts already returned; a failed selection or gate call ends the run.
        if any(row["phase"] != "returned_valid" and row["record"]["kind"] != "solve_obligation"
               and not retries.superseded(base, rid) for rid, row in requests.items()):
            return self._finish(base, "bestofn_call_failed")
        generation_failed = _failed_generation(base) is not None
        candidates = _candidates(base)
        count = len(candidates)
        if generation_failed and not count:
            return self._finish(base, "bestofn_call_failed")
        comparisons_started = any(row["record"]["kind"] == "compare_candidates" for row in requests.values())
        limit = base["manifest"]["limits"]["global_call_limit"]
        if not comparisons_started and not generation_failed:
            if count < 2:
                # A second manuscript needs one more solve, one selection and the two checks; calls
                # sent again after a timeout may leave too few, and one manuscript then goes to the checks.
                stop = None if count == 0 or 2 * 2 + 1 + retries.used(requests) <= limit else "call_budget"
            else:
                stop = self._generation_stop(base, count)
            if stop == "missing_usage" and "role_sources" not in self.config["bestofn"]:
                return {"phase": "blocked_token_usage", "reason": "bestofn generation usage is incomplete"}
            if stop is None:
                return self._prepare(base, "solve_obligation", f"bestofn.solve.{count}", None,
                                     plan_id=f"bestofn-plan-{count}")
        winner = candidates[0][1]
        for index in range(1, count):
            result = _result(base, f"bestofn.compare.{index}")
            if result is None:
                if len(requests) >= limit:
                    return self._finish(base, "bestofn_budget")
                return self._compare(base, winner, candidates[index][1], index)
            if result["outcome"] == "unknown":
                return self._finish(base, "bestofn_selection_unknown")
            if c.same_json(result["winner_ref"], c.candidate_ref(candidates[index][1])):
                winner = candidates[index][1]
        for suffix, kind in (("alignment", "alignment_review"), ("review", "whole_proof_review")):
            rid = "bestofn." + suffix
            if rid not in requests:
                if len(requests) >= limit:
                    return self._finish(base, "bestofn_budget")
                return self._prepare(base, kind, rid, winner, plan_id=winner["plan_id"])
        gates = [_result(base, "bestofn." + name) for name in ("alignment", "review")]
        rule = c.minor_findings_rule(base["manifest"]["limits"])
        passed = all(gate is not None and c.gate_passes(gate, rule)
                     and c.same_json(gate["target_ref"], c.candidate_ref(winner)) for gate in gates)
        return self._finish(base, "bestofn_complete" if passed else "bestofn_review_not_passed")

    def diagnostics(self, snapshot, rows):
        estimate = self.config["bestofn"]
        base = snapshot["exchange"]
        count = len(_candidates(base))
        generation_failed = _failed_generation(base) is not None
        costs = [measured_tokens(row) for row in rows]
        missing = sum(cost is None for cost in costs)
        actual = None if missing else sum(costs)
        # After a failed generation, a single returned manuscript is the selection.
        # One manuscript is the selection once generation failed or its checks began.
        selected = (count >= 2 or (count == 1 and (generation_failed or "bestofn.alignment" in base["requests"]))) and all(_result(base, f"bestofn.compare.{i}") is not None
                    and _result(base, f"bestofn.compare.{i}")["outcome"] == "selected" for i in range(1, count))
        winner = _winner(base) if selected else None
        role_estimates = self._role_estimates(rows, {rid for rid, _ in _candidates(base)})
        means = {role: value["tokens"] for role, value in role_estimates.items()}
        reason = estimate.get("unexecuted_reason")
        if reason is not None:
            return {"outcome": "unexecuted", "reason": reason, "bestofn": {
                "estimate": estimate, "effective_role_estimates": role_estimates,
                "n": 0, "n_frozen": True, "planned_calls_for_n": None,
                "selected_ref": None, "actual_tokens": None, "known_tokens": 0,
                "missing_usage_calls": 0, "actual_to_full_ratio": None, "token_overrun": None,
                "minimum_workflow_estimated_tokens": None, "minimum_workflow_estimate_over_target": None,
                "generation_stop_reason": None, "token_alignment_tolerance": None,
                "stop_reason": reason}}
        minimum_estimate = None if any(amount is None for amount in means.values()) else (
            2 * means["solver"] + means["critic"] + means["alignment_reviewer"] + means["proof_reviewer"])
        return {"bestofn": {"estimate": estimate, "effective_role_estimates": role_estimates,
                "n": count, "n_frozen": any(
                    row["record"]["kind"] == "compare_candidates" for row in base["requests"].values()),
                "planned_calls_for_n": 2 * count + 1 if count else None,
                "selected_ref": None if winner is None else c.candidate_ref(winner),
                "actual_tokens": actual, "known_tokens": sum(cost for cost in costs if cost is not None),
                "missing_usage_calls": missing, "actual_to_full_ratio": None if actual is None or not estimate["target_tokens"] else actual / estimate["target_tokens"],
                "token_overrun": None if actual is None else max(0, actual - estimate["target_tokens"]),
                "minimum_workflow_estimated_tokens": minimum_estimate,
                "minimum_workflow_estimate_over_target": None if minimum_estimate is None else minimum_estimate > estimate["target_tokens"],
                "generation_stop_reason": "call_failed" if generation_failed else (
                    self._generation_stop(base, count, rows) if count >= 2 else None),
                "token_alignment_tolerance": None,
                "stop_reason": None if snapshot["endpoint"] is None else snapshot["endpoint"]["reason"]}}
