"""Bounded L0-L4 policy over the shared persistent request lifecycle."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from . import adaptive_contracts as c, adaptive_exchange as exchange, retries
from .single import SingleLoop, _SingleExchange, _SingleBridge, _output, _result
from .loop import OriginalProblemLoop, STOP_WAIT_SECONDS
from .precheck import precheck
from .stop_request import REASON as STOPPED

GATES = (("alignment", "alignment_review"), ("review", "whole_proof_review"))
CHECK_NAMES = {"alignment_review": "alignment", "whole_proof_review": "whole-proof"}


def _rule(base):
    """The run's frozen minor_findings setting; runs recorded without it use nonblocking."""
    return c.minor_findings_rule(base["manifest"]["limits"])


def _passing(gate, rule=c.MINOR_FINDINGS[0]):
    """The pass rule of every policy, c.gate_passes; a missing report never passes."""
    return gate is not None and c.gate_passes(gate, rule)


def _blocking(gate, rule=c.MINOR_FINDINGS[0]):
    """The findings that send a draft to repair, c.blocking_findings."""
    return [] if gate is None else c.blocking_findings(gate, rule)


def _undecided_issues(candidate, undecided):
    """Repair input for inconclusive or incomplete checks that list no blocking finding.

    A doubt or a fail's minor finding is passed on as the reviewer wrote it; a check
    without a report, or an unknown outcome without a doubt, becomes one stated issue.
    """
    ref = c.candidate_ref(candidate)
    issues = []

    def stated(claim):
        return {"schema": "victorique.adaptive_defect.v1", "issue_id": "undecided-" + str(len(issues)),
                "target_ref": ref, "kind": "ambiguity", "severity": "material", "location": None, "claim": claim}

    for kind, gate in undecided:
        name = CHECK_NAMES[kind]
        if gate is None:
            issues.append(stated(f"The {name} check returned no valid report for this draft, after one retry. "
                                 "Check that every step and case of the argument is stated explicitly."))
            continue
        before = len(issues)
        for doubt in gate["uncertainties"]:
            issues.append(stated(f"Open doubt from the {name} check: {doubt}"))
        if gate["outcome"] == "fail":
            issues.extend(gate["findings"])
        if len(issues) == before:
            issues.append(stated(f"The {name} check returned outcome {gate['outcome']} without a stated doubt "
                                 "or blocking finding."))
    return issues


def _candidates(base):
    candidates = []
    for rid, row in base["requests"].items():
        output = _output(base, rid)
        if output is not None and row["record"]["kind"] in ("solve_obligation", "repair_obligation"):
            if row["record"]["kind"] == "repair_obligation":
                expected = json.loads(row["record"]["template"]["instructions"])["policy_instructions"]["issues"]
                assessments = output.get("assessments", [])
                if ({x["issue_id"] for x in assessments} != {x["issue_id"] for x in expected}
                        or not any(x["decision"] == "confirmed" for x in assessments)):
                    continue
            candidates.append({key: value for key, value in output.items() if key != "assessments"})
    return candidates


def _score(base, candidate):
    latest = {}
    ref = c.candidate_ref(candidate)
    for rid, row in base["requests"].items():
        target = row["record"]["context"]["target_candidate"]
        if row["record"]["kind"] in c.GATES and target is not None and c.candidate_ref(target) == ref:
            latest[row["record"]["kind"]] = _output(base, rid)
    rule = _rule(base)
    return sum(_passing(gate, rule) for gate in latest.values())


def _best(base):
    return max(_candidates(base), key=lambda candidate: _score(base, candidate), default=None)


def _child_export(child):
    endpoint = child["endpoint"]
    if endpoint is None:
        return None
    base = child["exchange"]
    choices = []
    for row in base["requests"].values():
        context = row["record"]["context"]
        if row["record"]["kind"] in c.GATES:
            target = context["target_candidate"]
            choices.append((target, [target, *context["dependency_closure"]]))
    if endpoint["root_candidate"] is not None:
        choices.append((endpoint["root_candidate"], endpoint["root_dependency_closure"]))
    if not choices:
        return endpoint
    candidate, closure = max(choices, key=lambda pair: _score(base, pair[0]))
    ref = c.candidate_ref(candidate)
    return {**endpoint, "root_candidate": candidate, "root_dependency_closure": closure,
            "selected_route_id": candidate["route_id"],
            "gates": [gate for gate in child.get("state", {}).get("gates", [])
                      if gate["report"]["target_ref"] == ref],
            "reducer_endpoint": {"decision": "complete" if _score(base, candidate) == 2 else "abstain"}}


class _LadderExchange(_SingleExchange):
    complete_reason = "ladder_complete"

    def snapshot(self):
        snapshot = super().snapshot()
        if snapshot["endpoint"] is not None:
            candidate = _best(snapshot["exchange"])
            reason = snapshot["endpoint"]["reason"]
            snapshot["endpoint"].update(root_candidate=candidate,
                root_dependency_closure=[] if candidate is None else [candidate],
                reducer_endpoint={"decision": "complete" if reason == self.complete_reason else
                                  "stop" if reason == STOPPED else "abstain"})
        return snapshot


class _LadderBridge(_SingleBridge):
    core_type = _LadderExchange


class LadderLoop(SingleLoop):
    core_type = _LadderExchange
    bridge_type = _LadderBridge
    policy_name = "ladder"
    l0_prefix = "ladder.L0"

    @property
    def options(self):
        return json.loads((self.root.parent / "invocation.json").read_text(encoding="utf-8"))["parameters"]

    def _finish(self, base, reason):
        candidate = _best(base)
        if candidate is not None and _score(base, candidate) == 2:
            reason = self.policy_name + "_complete"
        return super()._finish(base, reason)

    def _child(self):
        if not hasattr(self, "_child_loop"):
            self._child_loop = OriginalProblemLoop.open(self.root / "escalated", adapter=self.adapter if self.config["manifest"]["mode"] == "synthetic" else None)
            # The escalated full workflow is part of this run: it repeats timed-out calls and
            # follows the stop request as the run does.
            self._child_loop._timeout_retry_limit = self.timeout_retry_limit
            self._child_loop.stop_path = self.stop_path
        return self._child_loop

    def _end_calls_in_flight(self, wait_seconds=STOP_WAIT_SECONDS):
        super()._end_calls_in_flight(wait_seconds)
        if (self.root / "escalated").exists():
            self._child()._end_calls_in_flight(wait_seconds)

    def reporting_snapshot(self):
        snapshot = self.core.snapshot()
        if (self.root / "escalated").exists():
            child = self._child().core.snapshot()
            parent_best = _best(snapshot["exchange"])
            parent_score = 0 if parent_best is None else _score(snapshot["exchange"], parent_best)
            snapshot["exchange"]["requests"].update({"L3." + rid: row for rid, row in child["exchange"]["requests"].items()})
            snapshot["exchange"]["observations"].extend(child["exchange"]["observations"])
            endpoint = _child_export(child)
            if endpoint is not None and endpoint["root_candidate"] is not None:
                # Compare every reviewed full version, including pre-replan drafts.
                child_score = _score(child["exchange"], endpoint["root_candidate"])
                if child_score > parent_score or parent_best is None:
                    snapshot["endpoint"] = endpoint
        return snapshot

    def diagnostics(self, snapshot):
        endpoint = snapshot["endpoint"]
        candidate = None if endpoint is None else endpoint["root_candidate"]
        latest = {}
        for rid, row in snapshot["exchange"]["requests"].items():
            target = row["record"]["context"]["target_candidate"]
            if (candidate is not None and row["record"]["kind"] in c.GATES and target is not None
                    and c.candidate_ref(target) == c.candidate_ref(candidate)):
                latest[row["record"]["kind"]] = (rid, row, _output(snapshot["exchange"], rid))
        blocking = [] if candidate is None else precheck(candidate, snapshot["exchange"]["manifest"]["problem"]["content"])
        rule = _rule(snapshot["exchange"])
        minor, questions, incomplete, inconclusive = [], [], [], []
        for rid, row, gate in latest.values():
            if gate is None:
                incomplete.append({"request_id": rid, "status": row["phase"]})
                continue
            blocked = _blocking(gate, rule)
            blocking.extend(blocked)
            # Minor findings that the minor_findings setting made blocking are listed once, as blocking.
            minor.extend(issue for issue in gate["findings"] if issue["severity"] == "minor" and issue not in blocked)
            questions.extend(gate["uncertainties"])
            if not _passing(gate, rule) and not blocked:
                # Neither a pass nor a blocking finding: the reason a run can stop undecided.
                inconclusive.append({"request_id": rid, "check": row["record"]["kind"], "outcome": gate["outcome"],
                                     "doubts": len(gate["uncertainties"]),
                                     "minor_findings": len(gate["findings"])})
        return {"unresolved_issues": blocking, "non_blocking": minor,
                "unresolved_questions": questions, "incomplete_checks": incomplete,
                "inconclusive_checks": inconclusive}

    def _escalate(self, base):
        remaining = base["manifest"]["limits"]["global_call_limit"] - len(base["requests"])
        if not self.options.get("escalate") or remaining < 1:
            return self._finish(base, "ladder_unresolved")
        config = deepcopy(self.config)
        limits = config["manifest"]["limits"]
        limits.update(global_call_limit=remaining, total_budget_units=remaining, max_inflight=1)
        limits["role_call_limits"] = {role: remaining for role in c.ROLES}
        limits["role_budget_units"] = {role: remaining for role in c.ROLES}
        config["output_schemas_path"] = str(self.root / "role-output-schemas.json")
        path = self.root / "escalated-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        OriginalProblemLoop.create(self.root / "escalated", path,
            adapter=self.adapter if config["manifest"]["mode"] == "synthetic" else None)
        return {"phase": "native_escalated"}

    def _empty_draft(self, base, request_id="ladder.L0.solve"):
        """Recover only an empty body as a visibly unfinished repair scaffold."""
        request_id = retries.latest(base, request_id)
        row = base["requests"].get(request_id)
        if row is None or row["phase"] != "returned_invalid":
            return None
        raw = base["raw_observations"].get(row["first_result_observation_id"])
        try:
            value = c.parse_json(raw.decode("utf-8"))
            if type(value["output"]["content"]) is not str or value["output"]["content"].strip():
                return None
            # Validate the entire response/binding with only the empty-body defect
            # replaced by an explicit unresolved marker. Raw bytes stay untouched.
            value["output"]["content"] = "TODO: empty manuscript returned by the solver."
            checked = exchange.validate_response(json.dumps(value).encode(), row["packet"], row["record"], base["manifest"])
            observation = next(item for item in base["observations"] if item["observation_id"] == row["first_result_observation_id"])
            model = observation["record"]["model_observation"]
            expected = row["packet"]["binding"]["model_request"]
            if model and any(v is not None and expected[k] is not None and v != expected[k] for k, v in model.items()):
                return None
            return checked["output"]
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            return None

    def _review(self, base, candidate, prefix, suffix, kind, evidence):
        rid = prefix + "." + suffix
        attempts = [rid, rid + ".retry"]
        for attempt in attempts:
            # A request that stopped at its time limit and was sent again is replaced by its repetition.
            row = base["requests"].get(retries.latest(base, attempt))
            if row is None:
                if len(base["requests"]) >= base["manifest"]["limits"]["global_call_limit"]:
                    return {"failed_call": True}
                return self._prepare(base, kind, attempt, candidate, extra=evidence)
            if row["phase"] == "returned_valid":
                return _result(base, attempt)
        return {"failed_call": True}

    def _undecided_mode(self):
        """How inconclusive and incomplete checks are handled: abstain (default) or repair."""
        return self.options.get("undecided_checks", "abstain")

    def _check_candidate(self, base, candidate, prefix, round_no, previous_failed, evidence=None, *,
                         undecided_repair=False):
        """Shared deterministic trigger and cold, version-bound gate sequence.

        A check is undecided when it neither passes nor lists a blocking finding
        (inconclusive) or returns no valid report after its retry (incomplete).
        With ``undecided_repair`` such a round, without any blocking finding, is
        treated like a failed check once; otherwise the run stops and names it.
        Under the run's minor_findings setting ``blocking``, a check that says
        pass with minor findings fails, and those findings are its blocking ones.
        """
        available = base["manifest"]["limits"]["global_call_limit"] - len(base["requests"])
        issues = precheck(candidate, base["manifest"]["problem"]["content"])
        from_precheck = bool(issues)
        rule = _rule(base)
        failed, gates, undecided = [], {}, []
        if not issues:
            order = sorted(GATES, key=lambda pair: pair[0] not in previous_failed)
            for suffix, kind in order:
                if available == 0 and not any(prefix + "." + suffix + end in base["requests"] for end in ("", ".retry")):
                    return self._finish(base, self.policy_name + "_budget")
                gate = self._review(base, candidate, prefix, suffix, kind, evidence)
                if gate.get("phase"):
                    # Only L0 gates are independent; reserve both before dispatch.
                    if (round_no == 0 and available >= 2 and base["manifest"]["limits"]["max_inflight"] >= 2
                            and not any(prefix + "." + other in base["requests"] for other, _ in GATES if other != suffix)):
                        other, other_kind = next(pair for pair in GATES if pair[0] != suffix)
                        self._prepare(self.core.snapshot()["exchange"], other_kind, prefix + "." + other, candidate)
                    return gate
                if gate.get("failed_call"):
                    if self._undecided_mode() != "repair":
                        return self._finish(base, self.policy_name + "_check_incomplete")
                    gate = None  # Treated like a failed check, as is an inconclusive report.
                else:
                    gates[suffix] = gate
                if not _passing(gate, rule):
                    failed.append(suffix)
                    blocking = _blocking(gate, rule)
                    issues.extend(blocking)
                    if not blocking:
                        undecided.append((kind, gate))
                    if round_no > 0:
                        break
            if len(gates) == 2 and all(_passing(gate, rule) for gate in gates.values()):
                return self._finish(base, self.policy_name + "_complete")
            if not issues:
                if not undecided_repair:
                    incomplete = any(gate is None for _, gate in undecided)
                    return self._finish(base, self.policy_name + ("_check_incomplete" if incomplete
                                                                  else "_check_inconclusive"))
                return {"issues": _undecided_issues(candidate, undecided), "failed": failed,
                        "precheck": False, "undecided": True}
        return {"issues": issues, "failed": failed, "precheck": from_precheck}

    def step(self):
        base = self.core.snapshot()["exchange"]
        if base["endpoint"] is not None:
            return {"phase": "native_endpoint", "status": self.status()}
        if (self.root / "escalated").exists():
            result = self._child().step()
            if result["phase"] == "native_endpoint":
                return self._finish(base, "ladder_escalated")
            return result
        deadline = c.utc(base["manifest"]["limits"]["stop_condition"]["deadline_utc"])
        if datetime.now(timezone.utc) >= deadline:
            return self._finish(base, "ladder_deadline")
        pending = self._pending(base)
        if pending:
            return pending
        available = base["manifest"]["limits"]["global_call_limit"] - len(base["requests"])
        candidate = _result(base, self.l0_prefix + ".solve")
        if candidate is None:
            candidate = self._empty_draft(base, self.l0_prefix + ".solve")
        if candidate is None:
            if self.l0_prefix + ".solve" in base["requests"]:
                return self._finish(base, "ladder_solve_failed")
            return self._prepare(base, "solve_obligation", self.l0_prefix + ".solve", None)
        candidate = {key: value for key, value in candidate.items() if key != "assessments"}
        round_no = 0
        previous_failed = []
        evidence = None
        prior_refutations = set()
        # Replayed from round 0 on every step, so this count is the same on resume.
        undecided_used = False
        while True:
            prefix = self.l0_prefix if round_no == 0 else "ladder.L1." + str(round_no)
            checked = self._check_candidate(base, candidate, prefix, round_no, previous_failed, evidence,
                undecided_repair=self._undecided_mode() == "repair" and not undecided_used)
            if checked.get("phase"):
                return checked
            undecided_used = undecided_used or checked.get("undecided", False)
            issues, failed = checked["issues"], checked["failed"]
            # Different reviewers may use the same local issue ID. Give each
            # distinct claim a stable, local identity before requesting judgments.
            unique = {issue["claim"]: issue for issue in issues}
            issues = [{**issue, "issue_id": "ladder-issue-" + str(i)} for i, issue in enumerate(unique.values())]
            # Stable identity is the concrete claim, since reviewers may mint new IDs.
            recurring = next((issue for issue in issues if issue["claim"] in prior_refutations), None)
            investigation_id = "ladder.L2.investigate"
            if self.options.get("investigate") and recurring:
                investigation = _result(base, investigation_id)
                if investigation is None and investigation_id not in base["requests"]:
                    if available == 0:
                        return self._finish(base, "ladder_budget")
                    return self._prepare(base, "investigate", investigation_id, candidate, issues=[recurring])
                if investigation is not None:
                    # Explicit role-scoped release: only this completed investigation is selected.
                    evidence = {"released_investigation": {"request_id": investigation_id,
                        "recipients": ["solver", "alignment_reviewer", "proof_reviewer"],
                        "target_ref": investigation["target_ref"], "conclusion": investigation},
                        "instruction": "Consider this explicitly released investigation as a fallible hypothesis. Recheck the complete current proof yourself."}
            if round_no >= self.options.get("repair_rounds", 2):
                return self._escalate(base) if available else self._finish(base, "ladder_budget")
            rid = "ladder.L1.repair." + str(round_no + 1)
            repaired = _result(base, rid)
            if repaired is None:
                if rid in base["requests"]:
                    return self._finish(base, "ladder_repair_failed")
                if available == 0:
                    return self._finish(base, "ladder_budget")
                instruction = {"issues": issues, "released_evidence": evidence,
                    "instruction": "First judge every listed issue with an assessments entry {issue_id, decision: confirmed|refuted|unresolved, reason}. Repair only confirmed issues; preserve the complete original content if none are confirmed. Return the entire manuscript in content. Unresolved is not refuted. Include assessments as an additional candidate field."}
                return self._prepare(base, "repair_obligation", rid, candidate, issues=issues, extra=instruction)
            assessments = repaired.get("assessments", [])
            if {item["issue_id"] for item in assessments} != {issue["issue_id"] for issue in issues}:
                return self._finish(base, "ladder_repair_assessment_missing")
            decisions = {item["issue_id"]: item["decision"] for item in assessments}
            prior_refutations.update(issue["claim"] for issue in issues if decisions[issue["issue_id"]] == "refuted")
            if any(item["decision"] == "confirmed" for item in assessments):
                # Judgments belong to the solver response, not the cold proof view.
                candidate = {key: value for key, value in repaired.items() if key != "assessments"}
            elif checked["precheck"]:
                # The precheck is deterministic: the unchanged manuscript would fail it
                # again, so further repair rounds cannot change the outcome.
                return self._finish(base, self.policy_name + "_precheck_unresolved")
            previous_failed = failed
            round_no += 1
