"""Deterministic toy responses for exercising the public workflow offline."""
from datetime import datetime, timezone
import json

from .harness_bridge import OBSERVATION, TransportObservation


class FakeTransport:
    def describe(self):
        return {"adapter_id": "fake", "version": "v1", "mode": "synthetic",
                "can_start": True, "can_observe_existing": True,
                "model_execution": False, "fresh_context_required": True,
                "raw_response_preserved": True}

    def _response(self, request):
        binding = request.packet["binding"]
        context = request.packet["context"]
        kind = binding["action_kind"]
        if kind == "compare_candidates":
            output = {"schema": "victorique.adaptive_selection.v1", "outcome": "selected",
                      "winner_ref": binding["target_ref"], "reason": "Synthetic fixture preference."}
        elif kind in ("propose_plan", "replan"):
            parent = binding["plan_ref"]
            output = {"schema": "victorique.adaptive_plan.v1", "plan_id": "toy-plan",
                      "revision": 1 if parent is None else parent["revision"] + 1,
                      "parent_revision": None if parent is None else parent["revision"],
                      "problem_ref": binding["problem_ref"],
                      "routes": [{"route_id": "toy-route", "root_obligation_id": "root",
                                  "obligations": [{"obligation_id": "root",
                                      "statement": "Demonstrate the toy workflow",
                                      "assumptions": [], "depends_on": [],
                                      "claimed_goal_coverage": ["toy"],
                                      "failure_conditions": ["missing explanation"]}]}]}
        elif kind in ("solve_obligation", "integrate", "repair_obligation"):
            target = binding["target_ref"]
            revision = 1 if target is None else target["revision"] + 1
            content = ("# Synthetic toy proof\n\nThe conclusion holds.\n" if revision == 1 and binding["plan_ref"]["plan_id"] != "single-plan" else
                       "# Synthetic toy proof\n\nFor every integer n, adding the additive identity 0 leaves n unchanged, so n + 0 = n.\n")
            output = {"schema": "victorique.adaptive_candidate.v1",
                      "artifact_id": "toy-candidate", "revision": revision,
                      "parent_revision": None if target is None else target["revision"],
                      "plan_id": binding["plan_ref"]["plan_id"],
                      "plan_revision": binding["plan_ref"]["revision"],
                      "route_id": binding["route_id"], "obligation_id": binding["obligation_id"],
                      "content": content, "assumptions": [],
                      "dependency_refs": binding["dependency_refs"],
                      "addressed_issue_ids": [] if binding["issue_id"] is None else [binding["issue_id"]]}
            if kind == "repair_obligation" and binding["request_id"].startswith("ladder."):
                instructions = json.loads(request.packet["template"]["instructions"])
                issues = instructions["policy_instructions"]["issues"]
                output["assessments"] = [{"issue_id": issue["issue_id"], "decision": "confirmed",
                                           "reason": "Synthetic fixture repair."} for issue in issues]
        elif kind == "critique_candidate":
            findings = []
            if binding["target_ref"]["revision"] == 1:
                content = context["target_candidate"]["content"]
                findings = [{"schema": "victorique.adaptive_defect.v1", "issue_id": "toy-explanation",
                             "target_ref": binding["target_ref"], "kind": "invalid_step", "severity": "material",
                             "location": {"start_byte": 0, "end_byte": len(content.encode()), "quote": content},
                             "claim": "Synthetic fixture requires the additive-identity explanation."}]
            output = {"schema": "victorique.adaptive_critique.v1", "target_ref": binding["target_ref"],
                      "findings": findings, "no_gap_identified": not findings}
        elif kind == "investigate":
            content = context["target_candidate"]["content"]
            confirmed = binding["target_ref"]["revision"] == 1
            output = {"schema": "victorique.adaptive_disposition.v1", "issue_id": binding["issue_id"],
                      "target_ref": binding["target_ref"], "decision": "confirmed" if confirmed else "refuted",
                      "reason": "Synthetic fixture explanation check.",
                      "location": {"start_byte": 0, "end_byte": len(content.encode()), "quote": content} if confirmed else None}
        else:
            output = {"schema": "victorique.adaptive_gate.v1", "gate_id": kind,
                      "problem_ref": binding["problem_ref"], "target_ref": binding["target_ref"],
                      "coverage": "whole_problem_and_proof", "outcome": "pass", "findings": [], "uncertainties": []}
        return json.dumps({"schema": "victorique.adaptive_actor_response.v2", "binding": binding,
                           "output": output}, ensure_ascii=False).encode("utf-8")

    def start_once(self, request):
        metadata = {"schema": OBSERVATION, "observation_id": "fake." + request.job_id,
                    "job_id": request.job_id, "core_claim_id": request.core_claim_id,
                    "packet_id": request.packet["binding"]["packet_id"], "adapter_id": "fake",
                    "adapter_version": "v1", "mode": "synthetic", "status": "completed",
                    "terminal_observed": True, "host_reference": "/fake/" + request.job_id,
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "source_event_id": None, "source_line": None,
                    "model_observation": None, "charge_observation": None}
        return TransportObservation(metadata, self._response(request), b"Synthetic toy transport")

    def observe_existing(self, request):
        return self.start_once(request)

    def timeout_observation(self, request):
        """Fixed answers never reach a time limit."""
        return None
