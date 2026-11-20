"""Explicit reference-informed E2 diagnostic; never part of ordinary ladder."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import adaptive_contracts as c, adaptive_roles, api
from .grading import grade_proof, validate_grade
from .call_evidence import evidence
from .harness.safety import checked_path
from .ladder import LadderLoop, GATES, _best, _blocking, _candidates, _passing, _score
from .single import SingleLoop, _output
from .telemetry import token_counts, write_calls


def _lf(text):
    """Compare texts across Windows CRLF files and LF model/grader strings."""
    return text.replace("\r\n", "\n") if isinstance(text, str) else text


def load_l0_grade(source, prefix, candidate, task_id, repetition, directory):
    """Admit only the original task's durable, completed single/L0 grade."""
    source, directory = Path(source).absolute(), checked_path(Path(directory).absolute())
    if directory != source.parent / (task_id + "-grade-single"):
        raise ValueError("oracle grade must be this shared task's original single grade")
    identity = {"task_id": task_id, "repetition": repetition}
    if api._read(prefix.root.parent / "invocation.json").get("shared_identity") != identity or candidate is None:
        raise ValueError("oracle source task/repetition or candidate differs")
    request = api._read(directory / "request.json")
    result = api._read(directory / "result.json")
    context = request["packet"]["context"]
    ref = c.candidate_ref(candidate)
    if (request.get("candidate_ref") != ref or result.get("candidate_ref") != ref
            or _lf(context.get("candidate")) != _lf(candidate["content"])
            or _lf(context.get("problem")) != _lf(prefix.config["manifest"]["problem"]["content"])):
        raise ValueError("oracle grade must bind the exact original problem and L0 proof")
    if result.get("status") != "completed":
        raise ValueError("oracle grade is incomplete; no grade or repair is started")
    validate_grade(result)
    if (type(context.get("reference_solution")) is not str or not context["reference_solution"].strip()
            or type(context.get("guidelines", "")) is not str):
        raise ValueError("oracle grade has no valid reference context")
    return {"directory": str(directory), **identity, "candidate_ref": ref,
            "request_binding": request["packet"]["binding"], "score": result["score"], "reason": result["reason"],
            "reference_solution": context["reference_solution"], "guidelines": context.get("guidelines", ""),
            "transport": result.get("transport", request.get("transport")), "model": result.get("model")}


class OracleLadderLoop(LadderLoop):
    """Change only the initial trigger/repair information and V5 final selection."""

    @property
    def feedback(self):
        return api._read(self.root.parent / "invocation.json")["oracle_feedback"]

    def _check_candidate(self, base, candidate, prefix, round_no, previous_failed, evidence=None, **options):
        if round_no == 0:
            if self.feedback.get("variant") == "V7":
                failed = []
                if self.feedback["native_classification"] == "passed":
                    rid = "ladder.L0.cold_critique"
                    row = base["requests"].get(rid)
                    if row is None:
                        if len(base["requests"]) >= base["manifest"]["limits"]["global_call_limit"]:
                            return self._finish(base, "oracle_critique_budget")
                        return self._prepare(base, "critique_candidate", rid, candidate, extra={
                            "check_instructions": {kind: adaptive_roles.template(kind)["instructions"] for _, kind in GATES},
                            "instruction": "Inspect only this problem and manuscript using the supplied check instructions. Report concrete findings and severities; do not repair the manuscript."})
                    report = _output(base, rid)
                    if report is None:
                        return self._finish(base, "oracle_critique_incomplete")
                    issues = _blocking(report)
                else:
                    gates = [(suffix, _output(base, "ladder.L0." + suffix)) for suffix, _ in GATES]
                    issues = [issue for _, gate in gates for issue in _blocking(gate)]
                    failed = [suffix for suffix, gate in gates if gate is not None and not _passing(gate)]
                if not issues:
                    return self._finish(base, "oracle_native_feedback_missing")
                return {"issues": issues, "failed": failed}
            return {"issues": [{"schema": "victorique.adaptive_defect.v1", "issue_id": "oracle-l0",
                "target_ref": c.candidate_ref(candidate), "kind": "invalid_step", "severity": "material",
                "location": None, "claim": self.feedback["reason"]}], "failed": []}
        return super()._check_candidate(base, candidate, prefix, round_no, previous_failed, evidence, **options)

    def _prepare(self, base, kind, request_id, candidate, **kwargs):
        if kind == "repair_obligation" and request_id == "ladder.L1.repair.1" and self.feedback.get("variant") != "V7":
            extra = deepcopy(kwargs.get("extra") or {})
            extra["oracle_reference"] = {key: self.feedback[key] for key in ("score", "reason")}
            kwargs["extra"] = extra
        return super()._prepare(base, kind, request_id, candidate, **kwargs)

    def _repaired_best(self, base):
        choices = [candidate for candidate in _candidates(base)
                   if c.candidate_ref(candidate) != self.feedback["candidate_ref"]]
        return max(choices, key=lambda candidate: _score(base, candidate), default=None)

    def _finish(self, base, reason):
        best = self._repaired_best(base)
        # Passing old gates must not rescue a reference-rejected L0 after repair.
        reason = "ladder_complete" if best is not None and _score(base, best) == 2 else "oracle_unresolved"
        return SingleLoop._finish(self, base, reason)

    def reporting_snapshot(self):
        snapshot = super().reporting_snapshot()
        endpoint = snapshot["endpoint"]
        if endpoint is not None:
            best = self._repaired_best(snapshot["exchange"])
            accepted = best is not None and _score(snapshot["exchange"], best) == 2
            best = best or _best(snapshot["exchange"])
            endpoint.update(root_candidate=best, root_dependency_closure=[best] if best else [],
                            reducer_endpoint={"decision": "complete" if accepted else "abstain"})
        return snapshot


def run_arm(prefix, directory, plan, prefix_rows, candidate, classification, feedback, branch_id, adapter, *, variant="V5"):
    from .shared_prefix import _arm, _fork_l0
    from .harness.grade import grade_reference, resume_grade
    deadline = plan["deadline_utc"]
    if variant not in ("V5", "V6", "V7"):
        raise ValueError("unsupported mechanism variant")
    triggered = classification == "failed" if variant == "V6" else feedback["score"] < 7 and classification in ("passed", "failed")
    outcome = "complete" if classification == "passed" and not triggered else "abstain"
    best, extra = candidate, []
    control = feedback
    if variant == "V6":
        control = {key: feedback[key] for key in ("candidate_ref", "score", "reason")}
        control.update(variant=variant, native_classification=classification)
    elif variant == "V7":
        # Reference information decides authorization outside the loop. Neither
        # this loop nor its critic/repair packets receive a score or rationale.
        control = {"candidate_ref": feedback["candidate_ref"], "variant": variant,
                   "native_classification": classification, "reference_trigger": triggered}
    if triggered and (datetime.now(timezone.utc) < c.utc(deadline) or (directory / "state").exists()):
        loop = _fork_l0(prefix, directory, "ladder", adapter,
                       options={"repair_rounds": 12, "investigate": False, "escalate": False},
                       branch_id=branch_id, oracle_feedback=control)
        loop.run()
        snapshot = loop.reporting_snapshot()
        endpoint = snapshot["endpoint"]
        outcome = "pending" if endpoint is None else endpoint["reducer_endpoint"]["decision"]
        best = loop._repaired_best(snapshot["exchange"]) if endpoint is None else endpoint["root_candidate"]
        best = best or candidate
        prefix_ids = {row["request_id"] for row in prefix_rows}
        extra = [{**row, "branch_id": branch_id, "physical_call_id": branch_id + "/" + row["request_id"]}
                 for row in api._calls(loop, "ladder") if row["request_id"] not in prefix_ids]
    shared = best is not None and c.candidate_ref(best) == feedback["candidate_ref"] and best["content"] == candidate["content"]
    arm = _arm(directory, "ladder", outcome, best, prefix_rows, extra, plan, deadline)
    arm.update(variant=variant, branch_id=branch_id, parent="main_ladder", diagnostic_only=True,
               trigger_source="native" if variant == "V6" else "reference",
               feedback_source="native" if variant == "V7" else "reference",
               oracle_score=feedback["score"], oracle_grade_directory=feedback["directory"],
               shared_prefix=str(prefix.root.parent), grading_calls=0, grading_status="not_requested",
               candidate_binding=None if best is None else {"kind": "shared_L0" if shared else "E2_branch",
                   "run_directory": str(prefix.root.parent if shared else directory),
                   "branch_id": None if shared else branch_id, "candidate_ref": c.candidate_ref(best)})
    physical = list(extra)
    final = None
    if shared:
        final = {"status": "completed", "score": feedback["score"], "reason": feedback["reason"],
                 "candidate_ref": feedback["candidate_ref"], "calls_used": 0, "reused_from": feedback["directory"]}
    elif best is not None and outcome != "pending":
        grade_dir = directory / "grading-final"
        if grade_dir.exists():
            final = resume_grade(grade_dir, wait=False)
        elif datetime.now(timezone.utc) < c.utc(deadline):
            final = {}
            def invoke(request):
                final.update(grade_reference(request, directory=grade_dir, transport=plan["transport"],
                    model=None, candidate_ref=c.candidate_ref(best), deadline_utc=deadline))
                return final
            from .grading import GradingIncomplete
            try:
                grade_proof(invoke, problem=prefix.config["manifest"]["problem"]["content"], candidate=best["content"],
                            reference_solution=feedback["reference_solution"], guidelines=feedback["guidelines"])
            except GradingIncomplete:
                pass
        else:
            final = {"status": "not_run_deadline", "score": None, "calls_used": 0}
        if final.get("calls_used") or final["status"] == "unknown":
            physical.append({"run_id": branch_id, "policy": "oracle", "role": "reference_grader", "rung": "final",
                "request_id": "reference_grade.final", "branch_id": branch_id,
                "physical_call_id": branch_id + "/reference_grade.final", "status": final["status"],
                "tokens": token_counts(final.get("tokens"), plan["transport"] + "_cli"),
                "model": final.get("model"), "effort": final.get("effort"),
                **{key: final.get(key, value) for key, value in evidence().items()}})
    if final is not None:
        arm.update(final_grade=final, grading_status=final["status"], grading_calls=len(physical) - len(extra))
    arm["additional_physical_calls"] = len(physical)
    write_calls(directory, physical)
    api._write(directory / "summary.json", arm)
    return arm, physical
