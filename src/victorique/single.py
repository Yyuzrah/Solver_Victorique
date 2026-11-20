"""Fixed whole-problem solver and two reviews using the shared request lifecycle."""
from copy import deepcopy
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from . import adaptive_contracts as c, adaptive_exchange as exchange, adaptive_roles, retries
from .harness_bridge import AdaptiveHarnessBridge
from .loop import OriginalProblemLoop
from .stop_request import REASON as STOPPED, StopRequested


def _output(base, request_id):
    """The valid output of exactly this request, or None."""
    row = base["requests"].get(request_id)
    if row is None or row["phase"] != "returned_valid":
        return None
    observed = next(item for item in base["observations"]
                    if item["observation_id"] == row["first_result_observation_id"])
    return observed["parsed_response"]["output"]


def _result(base, request_id):
    """The valid output of a call, from its newest request when a timed-out request was sent again."""
    return _output(base, retries.latest(base, request_id))


def _failed(base):
    """Requests that ended without a valid output and were not sent again."""
    return [rid for rid, row in base["requests"].items()
            if row["phase"] in exchange.TERMINAL and row["phase"] != "returned_valid"
            and not retries.superseded(base, rid)]


class _SingleExchange(exchange.AdaptiveExchange):
    def snapshot(self):
        base = super().snapshot()
        endpoint = base["endpoint"]
        if endpoint is not None:
            candidate = _result(base, "single.solve")
            # A run stopped by its stop request ends without a decision (outcome partial).
            decision = ("complete" if endpoint["reason"] == "single_complete" else
                        "stop" if endpoint["reason"] == STOPPED else "abstain")
            endpoint = {**endpoint,
                        "reducer_endpoint": {"decision": decision},
                        "root_candidate": candidate,
                        "root_dependency_closure": [] if candidate is None else [candidate]}
        return {"exchange": base, "endpoint": endpoint}


class _SingleBridge(AdaptiveHarnessBridge):
    core_type = _SingleExchange


class SingleLoop(OriginalProblemLoop):
    core_type = _SingleExchange
    bridge_type = _SingleBridge

    def status(self):
        return self.core.status()

    def _finish(self, base, reason):
        self.core.close({"schema": "victorique.adaptive_close.v1",
                         "session_id": base["manifest"]["session_id"],
                         "operation_id": "single.close", "expected_event_sequence": base["sequence"],
                         "reason": reason})
        return {"phase": "native_endpoint", "status": self.status()}

    def _finish_stopped(self):
        snapshot = self.core.snapshot()
        if snapshot["endpoint"] is None:
            # ladder and resample keep a draft that already passed both checks as complete.
            self._finish(snapshot["exchange"], STOPPED)
            snapshot = self.core.snapshot()
        return {"phase": "native_endpoint", "reason": snapshot["endpoint"]["reason"], "status": self.status()}

    def _prepare(self, base, kind, request_id, candidate, *, issues=None, extra=None, plan_id="single-plan"):
        self._check_stop()
        manifest = base["manifest"]
        problem = manifest["problem"]
        # A single literal whole-problem obligation needs no model planning call.
        plan = {"schema": "victorique.adaptive_plan.v1", "plan_id": plan_id,
                "revision": 1, "parent_revision": None,
                "problem_ref": {key: problem[key] for key in ("artifact_id", "revision")},
                "routes": [{"route_id": "single-route", "root_obligation_id": "root",
                            "obligations": [{"obligation_id": "root", "statement": "Establish the entire original problem exactly as stated in context.problem.content.",
                                             "assumptions": [], "depends_on": [],
                                             "claimed_goal_coverage": ["Entire original problem"],
                                             "failure_conditions": ["Any unresolved gap in the original claim"]}]}]}
        template = adaptive_roles.template(kind)
        instructions = {"common": template["common_instructions"], "role": template["instructions"],
                        "limitations": template["limitations"], "output_contract": adaptive_roles.output_contract(kind),
                        "response_format": "Return the exact schema/binding/output response envelope. The contract is a blueprint: shape, nullable and descriptive strings are helper instructions, not output fields or literal output values."}
        if extra:
            instructions["policy_instructions"] = extra
            if kind == "repair_obligation":
                instructions["output_contract"]["assessments"] = [{"issue_id": "each supplied issue ID",
                    "decision": "confirmed | refuted | unresolved", "reason": "reason for this judgment"}]
        role = manifest["limits"]["action_roles"][kind]
        issue = None if not issues else {"proposal": issues[0], "original_target_candidate": candidate,
                                        "target_ref": c.candidate_ref(candidate), "disposition": "proposed"}
        self.core.prepare({"schema": exchange.REQUEST_SCHEMA, "session_id": manifest["session_id"],
                           "operation_id": request_id + ".prepare", "expected_event_sequence": base["sequence"],
                           "request_id": request_id, "logical_action_id": request_id, "action_id": request_id,
                           "prepared_state_revision": len(base["requests"]), "kind": kind, "role": role,
                           "actor_id": manifest["role_bindings"][role]["actor_id"],
                           "route_id": "single-route", "obligation_id": "root", "dependency_refs": [],
                           "template": {"template_id": template["template_id"], "version": "v2",
                                        "instructions": exchange._dump(instructions)},
                           "context": {"problem": problem, "plan": plan, "target_candidate": candidate,
                                       "dependency_closure": [], "selected_issue": issue, "released_feedback": [],
                                       "planning_evidence": [], "planning_evidence_selection": {
                                           "eligible_count": 0, "selected_count": 0, "max_entries": 64,
                                           "truncated": False, "policy": exchange.PLANNING_SELECTION_POLICY}}})
        return {"phase": "native_prepared", "request_id": request_id, "kind": kind}

    def _prepare_retry(self, base, request_id):
        """Prepare the same call again as a new request; returns the new exchange state."""
        self._check_stop()
        new = retries.next_id(request_id)
        record = deepcopy(base["requests"][request_id]["record"])
        record.update(operation_id=new + ".prepare", expected_event_sequence=base["sequence"], request_id=new,
                      logical_action_id=new, action_id=new, prepared_state_revision=len(base["requests"]))
        self.core.prepare(record)
        return self.core.snapshot()["exchange"]

    def _prepare_due_retries(self, base):
        """Prepare the repetition of each settled timed-out call that has none yet, as limits allow."""
        limits = base["manifest"]["limits"]
        for rid in list(base["requests"]):
            if not retries.settled(base, rid) or retries.superseded(base, rid):
                continue
            inflight = sum(row["phase"] not in exchange.TERMINAL for row in base["requests"].values())
            if (retries.attempt(rid) >= self.timeout_retry_limit or inflight >= limits["max_inflight"]
                    or len(base["requests"]) >= limits["global_call_limit"]
                    or datetime.now(timezone.utc) >= c.utc(limits["stop_condition"]["deadline_utc"])):
                continue
            base = self._prepare_retry(base, rid)
        return base

    def _retry_timeouts(self):
        """Settle and prepare again every unknown call that stopped at its time limit, if all may be repeated."""
        base = self.core.snapshot()["exchange"]
        unknown = [rid for rid, row in base["requests"].items() if row["phase"] == "unknown"]
        if self._timeouts_to_retry(base, unknown) is None:
            return None
        for rid in unknown:
            self.bridge.settle_timeout(rid, self.adapter)
        self._prepare_due_retries(self.core.snapshot()["exchange"])
        return {"phase": "call_timed_out", "request_id": unknown[0],
                "retry_request_ids": [retries.next_id(rid) for rid in unknown]}

    def _pending(self, base):
        """Dispatch independent reserved requests together, then observe originals only."""
        base = self._prepare_due_retries(base)
        jobs = {job["request_id"]: job for job in self.bridge.status()["jobs"]}
        pending = [(rid, row) for rid, row in base["requests"].items() if row["phase"] not in exchange.TERMINAL]
        ready = [rid for rid, row in pending if row["claim_id"] is None
                 and (rid not in jobs or jobs[rid]["phase"] == "prepared")]
        if ready and not any(row["phase"] == "unknown" for _, row in pending):
            self._check_stop()
            adapter = self.adapter
            barrier = None
            if len(ready) == 2:
                original = adapter
                barrier = Barrier(2, timeout=20)
                class PairedAdapter:
                    def describe(self):
                        return original.describe()
                    def start_once(self, request):
                        # Both durable claims exist before either asynchronous start
                        # can return unknown and fence further dispatches.
                        barrier.wait()
                        return original.start_once(request)
                adapter = PairedAdapter()

            def dispatch(rid):
                try:
                    return self.bridge.dispatch(rid, adapter)
                except StopRequested:
                    # The bridge refused this call because the stop request appeared just now;
                    # the other call of the pair must not wait for it.
                    if barrier is not None:
                        barrier.abort()
                    raise
            with ThreadPoolExecutor(max_workers=min(2, len(ready))) as pool:
                results = list(pool.map(dispatch, ready))
            return {"phase": "original_prepared_dispatched", "request_id": ready[0],
                    "results": results, "result": results[0]}
        changed = []
        for rid, row in pending:
            if row["claim_id"] is None:
                continue
            if rid not in jobs:
                return {"phase": "blocked_original_responsibility", "request_id": rid}
            result = self.bridge.recover(rid, self.adapter)
            current = self.core.snapshot()["exchange"]["requests"][rid]
            if current["phase"] in exchange.TERMINAL:
                changed.append((rid, result))
        if changed:
            return {"phase": "original_response_observed", "request_id": changed[0][0], "result": changed[0][1]}
        if pending:
            # Unknown is an unfinished responsibility, never a retry permission.
            if self.config["transport"]["kind"] in ("codex_cli", "claude_cli"):
                for rid, row in pending:
                    if row["claim_id"] is None or rid not in jobs:
                        continue
                    request = self.bridge._request(self.bridge._job(rid))
                    capture = self.adapter.local_capture(request)
                    if not capture or not capture.get("worker_finished"):
                        return {"phase": "pending_original_observation", "request_id": rid}
                    # A process can finish between recover and this observation.
                    # Ingest its original result once more before handing back.
                    result = self.bridge.recover(rid, self.adapter)
                    current = self.core.snapshot()["exchange"]["requests"][rid]
                    if current["phase"] in exchange.TERMINAL:
                        return {"phase": "original_response_observed", "request_id": rid, "result": result}
            # Every claimed call has finished without a result. Calls stopped at their
            # time limit are sent again when the run's settings and limits allow it.
            retried = self._retry_timeouts()
            if retried is not None:
                return retried
            return {"phase": "blocked_original_result", "request_id": pending[0][0]}
        return None

    def _gate_pair(self, base, candidate, prefix):
        for suffix, kind in (("alignment", "alignment_review"), ("review", "whole_proof_review")):
            rid = prefix + "." + suffix
            if rid not in base["requests"] and len(base["requests"]) < base["manifest"]["limits"]["global_call_limit"]:
                self._prepare(base, kind, rid, candidate)
                base = self.core.snapshot()["exchange"]
                if sum(row["phase"] not in exchange.TERMINAL for row in base["requests"].values()) >= base["manifest"]["limits"]["max_inflight"]:
                    break
        return {"phase": "native_prepared", "request_id": prefix + ".alignment", "kind": "alignment_review"}

    def step(self):
        snapshot = self.core.snapshot()
        base = snapshot["exchange"]
        if snapshot["endpoint"] is not None:
            return {"phase": "native_endpoint", "status": self.status()}
        requests = base["requests"]
        alignment = _result(base, "single.alignment")
        review = _result(base, "single.review")
        if alignment is not None and review is not None:
            # The run's frozen minor_findings setting; runs recorded without it use nonblocking.
            rule = c.minor_findings_rule(base["manifest"]["limits"])
            passed = (c.gate_passes(alignment, rule) and c.gate_passes(review, rule)
                      and c.same_json(alignment["target_ref"], review["target_ref"]))
            return self._finish(base, "single_complete" if passed else "single_review_not_passed")
        deadline = c.utc(base["manifest"]["limits"]["stop_condition"]["deadline_utc"])
        if datetime.now(timezone.utc) >= deadline:
            return self._finish(base, "single_deadline")
        pending = self._pending(base)
        if pending:
            return pending
        if _failed(base):
            return self._finish(base, "single_call_failed")
        if len(requests) >= base["manifest"]["limits"]["global_call_limit"]:
            return self._finish(base, "single_budget")
        candidate = _result(base, "single.solve")
        if candidate is None:
            return self._prepare(base, "solve_obligation", "single.solve", None)
        return self._gate_pair(base, candidate, "single")
