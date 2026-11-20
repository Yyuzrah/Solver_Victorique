"""Bounded standalone proof review through the shared persistent transport."""
import argparse
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import sys
from uuid import uuid4

from . import adaptive_contracts as c, adaptive_exchange as exchange, adaptive_roles, settings
from .api import _mcp_names
from .call_evidence import evidence
from .fake import FakeTransport
from .harness.codex_contract import DEFAULT_MODEL, conservative_codex_argv
from .harness.claude_contract import complete_claude_profile, conservative_claude_argv
from .harness.hosts import inspect_host
from .harness.safety import checked_path
from .harness_bridge import AdaptiveHarnessBridge
from .loop import OriginalProblemLoop, CONFIG
from .prepare import build_manifest, prepare
from .telemetry import call_rows, totals, write_calls


def _write(path, value):
    checked_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                           allow_nan=False) + "\n", encoding="utf-8")


def _read(path):
    return json.loads(checked_path(path).read_text(encoding="utf-8"))


def _result(base, row):
    if row["phase"] != "returned_valid":
        return None
    observation = next(x for x in base["observations"]
                       if x["observation_id"] == row["first_result_observation_id"])
    return observation["parsed_response"]["output"]


def _verdict(output):
    if output["outcome"] == "unknown":
        return "incomplete"
    if any(x["severity"] in ("fatal", "material") for x in output["findings"]):
        return "fail"
    if output["uncertainties"]:
        return "incomplete"
    # Minor-only findings may come with outcome pass or fail. Preserve the raw
    # report, while treating wording issues as non-blocking.
    return "pass"


class _VerifyExchange(exchange.AdaptiveExchange):
    def snapshot(self):
        base = super().snapshot()
        return {"exchange": base, "endpoint": base["endpoint"]}


class _VerifyBridge(AdaptiveHarnessBridge):
    core_type = _VerifyExchange


class VerifyLoop(OriginalProblemLoop):
    core_type = _VerifyExchange
    bridge_type = _VerifyBridge

    @classmethod
    def open(cls, root, **kwargs):
        loop = super().open(root, **kwargs)
        loop.options = _read(loop.root.parent / "verify.json")
        return loop

    def status(self):
        return self.core.status()

    def _finish(self, base, reason):
        self.core.close({"schema": "victorique.adaptive_close.v1",
                         "session_id": base["manifest"]["session_id"],
                         "operation_id": "verify.close", "expected_event_sequence": base["sequence"],
                         "reason": reason})
        return {"phase": "native_endpoint", "status": self.status()}

    def _prepare_review(self, base):
        manifest = base["manifest"]
        problem = manifest["problem"]
        candidate = self.options["candidate"]
        plan = {"schema": "victorique.adaptive_plan.v1", "plan_id": "verify-plan",
                "revision": 1, "parent_revision": None,
                "problem_ref": {key: problem[key] for key in ("artifact_id", "revision")},
                "routes": [{"route_id": "verify-route", "root_obligation_id": "root",
                            "obligations": [{"obligation_id": "root",
                                "statement": "Establish the entire original problem exactly as stated.",
                                "assumptions": [], "depends_on": [],
                                "claimed_goal_coverage": ["Entire original problem"],
                                "failure_conditions": ["Any unresolved gap in the original claim"]}]}]}
        kind = "whole_proof_review"
        template = adaptive_roles.template(kind)
        supplement = ("Actively search for counterexamples and invalid inferences. Classify mathematical "
                      "blocking defects as fatal or material, and wording/presentation issues as minor. "
                      "For minor-only findings use outcome fail (required by the output contract); "
                      "the caller will report them separately as non-blocking. Use unknown for unresolved "
                      "doubt, never a fabricated blocking defect.")
        if self.options["mode"] == "stepwise":
            supplement += (" In your analysis, first enumerate the proof's reasoning steps in order; "
                           "then check every step individually, asking what could make it false, "
                           "whether its premises hold, and whether it establishes the next step. "
                           "Check all cases and the final conclusion against the original problem. "
                           "Locate each discovered defect in the supplied proof. Return the existing "
                           "gate contract after this step-by-step check.")
        instructions = {"common": template["common_instructions"],
                        "role": template["instructions"] + " " + supplement,
                        "limitations": template["limitations"],
                        "output_contract": adaptive_roles.output_contract(kind),
                        "response_format": "Return the exact schema/binding/output response envelope. "
                            "The contract is a blueprint: descriptive strings are instructions, not literal values."}
        rid = "verify.review." + str(len(base["requests"]) + 1)
        role = manifest["limits"]["action_roles"][kind]
        self.core.prepare({"schema": exchange.REQUEST_SCHEMA, "session_id": manifest["session_id"],
            "operation_id": rid + ".prepare", "expected_event_sequence": base["sequence"],
            "request_id": rid, "logical_action_id": rid, "action_id": rid,
            "prepared_state_revision": len(base["requests"]), "kind": kind, "role": role,
            "actor_id": manifest["role_bindings"][role]["actor_id"],
            "route_id": "verify-route", "obligation_id": "root", "dependency_refs": [],
            "template": {"template_id": template["template_id"], "version": "v2",
                         "instructions": exchange._dump(instructions)},
            "context": {"problem": problem, "plan": plan, "target_candidate": candidate,
                        "dependency_closure": [], "selected_issue": None, "released_feedback": [],
                        "planning_evidence": [], "planning_evidence_selection": {
                            "eligible_count": 0, "selected_count": 0, "max_entries": 64,
                            "truncated": False, "policy": exchange.PLANNING_SELECTION_POLICY}}})
        return {"phase": "native_prepared", "request_id": rid, "kind": kind}

    def step(self):
        base = self.core.snapshot()["exchange"]
        if base["endpoint"] is not None:
            return {"phase": "native_endpoint", "status": self.status()}
        rows = list(base["requests"].values())
        verdicts = [_verdict(output) for row in rows if (output := _result(base, row)) is not None]
        if "fail" in verdicts:
            return self._finish(base, "fail")
        if "incomplete" in verdicts or any(row["phase"] in exchange.TERMINAL
                                          and row["phase"] != "returned_valid" for row in rows):
            return self._finish(base, "incomplete")
        if len(verdicts) == self.options["reviews_required"]:
            return self._finish(base, "pass")
        deadline = c.utc(base["manifest"]["limits"]["stop_condition"]["deadline_utc"])
        if datetime.now(timezone.utc) >= deadline:
            return self._finish(base, "deadline")
        jobs = {job["request_id"]: job for job in self.bridge.status()["jobs"]}
        for row in rows:
            if row["phase"] in exchange.TERMINAL:
                continue
            rid = row["request_id"]
            job = jobs.get(rid)
            if row["phase"] == "unknown" and self.config["transport"]["kind"] not in ("codex_cli", "claude_cli"):
                return {"phase": "blocked_unknown_result", "request_id": rid}
            if row["claim_id"] is None and (job is None or job["phase"] == "prepared"):
                return {"phase": "original_prepared_dispatched", "request_id": rid,
                        "result": self.bridge.dispatch(rid, self.adapter)}
            if job is None:
                return {"phase": "blocked_original_responsibility", "request_id": rid}
            try:
                result = self.bridge.recover(rid, self.adapter)
            except (OSError, ValueError) as error:
                return {"phase": "blocked_observation_error", "request_id": rid,
                        "error_type": type(error).__name__}
            current = self.core.snapshot()["exchange"]["requests"][rid]
            if current["phase"] in exchange.TERMINAL:
                return {"phase": "original_response_observed", "request_id": rid, "result": result}
            if self.config["transport"]["kind"] in ("codex_cli", "claude_cli"):
                request = self.bridge._request(self.bridge._job(rid))
                try:
                    capture = self.adapter.runner.observe_existing(self.adapter._binding(request))
                except (OSError, ValueError) as error:
                    return {"phase": "blocked_observation_error", "request_id": rid,
                            "error_type": type(error).__name__}
                if not capture or not capture.get("worker_finished"):
                    return {"phase": "pending_original_observation", "request_id": rid}
                # The process may have completed after recover read its pending
                # snapshot. Ingest that same request once more before stopping.
                try:
                    result = self.bridge.recover(rid, self.adapter)
                except (OSError, ValueError) as error:
                    return {"phase": "blocked_observation_error", "request_id": rid,
                            "error_type": type(error).__name__}
                current = self.core.snapshot()["exchange"]["requests"][rid]
                if current["phase"] in exchange.TERMINAL:
                    return {"phase": "original_response_observed", "request_id": rid, "result": result}
            return {"phase": "blocked_original_result", "request_id": rid}
        if len(rows) >= base["manifest"]["limits"]["global_call_limit"]:
            return self._finish(base, "call_limit")
        return self._prepare_review(base)


def _summary(directory, loop, stopped=None, rows=None):
    base = loop.core.snapshot()["exchange"]
    rows = call_rows(loop, "verify", base) if rows is None else rows
    calls = {row["request_id"]: row for row in rows}
    reports, blocking, non_blocking = [], [], []
    for rid, row in base["requests"].items():
        output = _result(base, row)
        report = {"request_id": rid, "phase": row["phase"], "output": output,
                  "verdict": "incomplete" if output is None else _verdict(output)}
        fallback = evidence(config=loop.config, output=output)
        report.update({key: calls.get(rid, {}).get(key, value) for key, value in fallback.items()})
        reports.append(report)
        for finding in [] if output is None else output["findings"]:
            target = non_blocking if finding["severity"] == "minor" else blocking
            target.append({"request_id": rid, **finding})
    endpoint = base["endpoint"]
    reason = (stopped or {}).get("phase", "pending") if endpoint is None else endpoint["reason"]
    usage, model = totals(rows)
    invocation = _read(directory / "invocation.json")
    ended = datetime.now(timezone.utc) if endpoint is None else c.utc(endpoint["frozen_at_utc"])
    return {"run_id": base["manifest"]["session_id"], "policy": "verify",
            "outcome": reason if reason in ("pass", "fail") else "incomplete", "reason": reason,
            "mode": loop.options["mode"], "passes": loop.options["passes"],
            "reviews_required": loop.options["reviews_required"],
            "reviews_passed": sum(x["verdict"] == "pass" for x in reports),
            "calls_used": sum(row["claim_id"] is not None for row in base["requests"].values()),
            "calls_limit": base["manifest"]["limits"]["global_call_limit"],
            "wall_seconds": max(0, (ended - datetime.fromisoformat(invocation["started_at"])).total_seconds()),
            "deadline_utc": base["manifest"]["limits"]["stop_condition"]["deadline_utc"],
            "tokens": usage, "reported_model": model,
            "cli_defaults": invocation.get("cli_defaults"),
            "synthetic": base["manifest"]["mode"] == "synthetic",
            "problem_ref": {k: base["manifest"]["problem"][k] for k in ("artifact_id", "revision")},
            "target_ref": c.candidate_ref(loop.options["candidate"]),
            "blocking": blocking, "non_blocking": non_blocking, "reviews": reports,
            "output_directory": str(directory)}


def _run(directory, loop):
    stopped = None
    try:
        stopped = loop.run(on_step=lambda row: print("verify | " + row["phase"], flush=True))
    except KeyboardInterrupt:
        print("Interrupted; existing requests remain available for observation.", file=sys.stderr)
    rows = call_rows(loop, "verify")
    write_calls(directory, rows)
    summary = _summary(directory, loop, stopped, rows)
    _write(directory / "summary.json", summary)
    return summary


def resume_verify(directory, *, _adapter=None):
    """Observe existing requests with the original inputs, budget and deadline."""
    directory = checked_path(Path(directory).absolute())
    config = _read(directory / "state" / "run-config.json")
    adapter = (_adapter or FakeTransport()) if config["manifest"]["mode"] == "synthetic" else None
    return _run(directory, VerifyLoop.open(directory / "state", adapter=adapter))


def verify(proof, *, problem, mode="single", passes=1, model=None, calls=12,
           hours=2, out=None, transport="codex", call_timeout_seconds=None,
           _command=None, _adapter=None, _deadline=None):
    """Review UTF-8 proof and problem files; return pass, fail, or incomplete.

    ``call_timeout_seconds`` limits each review call (None: 600 seconds).
    """
    c.need(mode in ("single", "stepwise", "multi"), "mode must be single, stepwise or multi")
    c.integer(passes, "passes", 1, 100)
    c.integer(calls, "calls", 1, 100)
    c.need(type(hours) in (int, float) and math.isfinite(hours) and hours > 0,
           "hours must be a positive finite number")
    call_timeout_seconds = settings.call_timeout_seconds(call_timeout_seconds)
    c.need(transport in ("codex", "claude", "fake"), "transport must be codex, claude or fake")
    c.need(_adapter is None or transport == "fake", "adapter injection requires fake transport")
    c.need(model is None or (type(model) is str and model and not model.startswith("-")
                            and not any(ch.isspace() for ch in model)), "model must be an identifier")
    problem_path = checked_path(Path(problem).absolute())
    problem_raw = problem_path.read_bytes()
    c.validate_problem({"artifact_id": "input", "revision": 1, "content": problem_raw.decode("utf-8")})
    content = checked_path(Path(proof).absolute()).read_bytes().decode("utf-8")
    c.text(content, "proof content")
    started = datetime.now(timezone.utc)
    try:
        deadline_at = started + timedelta(hours=hours)
        if _deadline is not None:
            deadline_at = min(deadline_at, c.utc(_deadline))
        deadline = deadline_at.isoformat()
    except OverflowError as error:
        raise ValueError("hours exceeds the supported date range") from error
    run_id = "verify-" + uuid4().hex
    directory = checked_path(Path(out).absolute() if out is not None else Path.cwd() / "runs" / run_id)
    c.need(not directory.exists(), "output directory already exists; use resume_verify or a new --out")
    selected_model = model or DEFAULT_MODEL
    profile = None
    if transport != "fake":
        host = "claude-code" if transport == "claude" else "codex"
        binary = os.environ.get("VICTORIQUE_" + transport.upper() + "_BINARY")
        profile = inspect_host(host, binary) if binary else inspect_host(host)
        c.need(profile["status"] == "public_interface_observed", host + " CLI unavailable")
        if transport == "claude":
            profile = complete_claude_profile(profile)
            conservative_claude_argv(profile, {"type": "object"}, selected_model)
        else:
            conservative_codex_argv(profile, {"workspace": directory / "probe-workspace",
                "schema_file": directory / "probe-schema.json", "final_file": directory / "probe-output.json"}, selected_model)
    directory.mkdir(parents=True)
    candidate = {"schema": "victorique.adaptive_candidate.v1", "artifact_id": run_id + ".proof",
                 "revision": 1, "parent_revision": None, "plan_id": "verify-plan", "plan_revision": 1,
                 "route_id": "verify-route", "obligation_id": "root", "content": content,
                 "assumptions": [], "dependency_refs": [], "addressed_issue_ids": []}
    _write(directory / "verify.json", {"candidate": candidate, "mode": mode, "passes": passes,
                                      "reviews_required": passes * (3 if mode == "multi" else 1)})
    invocation = {"started_at": started.isoformat(), "argv": _command,
        "parameters": {"proof": str(proof), "problem": str(problem), "policy": "verify",
                       "mode": mode, "passes": passes, "model": model, "calls": calls,
                       "hours": hours, "transport": transport,
                       "call_timeout_seconds": call_timeout_seconds}}
    if transport == "codex":
        from .harness.codex_defaults import read_cli_defaults
        invocation["cli_defaults"] = read_cli_defaults(profile["binary"], directory)
    _write(directory / "invocation.json", invocation)
    # Preserve the exact caller's text, including its original line endings.
    (directory / "proof.md").write_bytes(content.encode("utf-8"))
    prepared = directory / "prepared"
    if transport != "fake":
        if transport == "codex":
            profile["disabled_mcp_servers"] = _mcp_names(profile["binary"], directory)
        _write(directory / "profile.json", profile)
        auth = {"schema": "victorique." + transport + "_execution_authorization.v1", "authorization_id": run_id + ".authorization",
                "source_ref": "User verify invocation in invocation.json", "allow_execution": True,
                "session_id": run_id, "protocol": {"protocol_id": "victorique.verify", "version": "v1"},
                "binary": profile["binary"], "host_version": profile["version"], "requested_model": selected_model,
                "max_process_starts": calls, "expires_at_utc": deadline,
                "permission_review_ref": "User verify command; read-only review workspace",
                "authentication_precondition": "operator_completed_host_managed_login"}
        _write(directory / "authorization.json", auth)
        prepare(problem_path=problem_path, profile_path=directory / "profile.json",
                authorization_path=directory / "authorization.json", session_id=run_id, run_id=run_id,
                provider_id=transport + "-cli", model=selected_model, calls=calls, deadline=deadline,
                output=prepared, timeout_seconds=settings.run_call_timeout(call_timeout_seconds, hours * 3600))
        adapter = None
    else:
        prepared.mkdir()
        schema_path = prepared / "adaptive-output-schemas.v1.json"
        schema_path.write_bytes(files("victorique").joinpath("adaptive-output-schemas.v1.json").read_bytes())
        adapter = _adapter or FakeTransport()
        config = {"schema": CONFIG, "manifest": build_manifest(problem_raw=problem_raw,
                    session_id=run_id, run_id=run_id, model="fake", calls=calls, deadline=deadline,
                    source_ref="User fake verify", provider_id="synthetic", mode="synthetic",
                    protocol={"protocol_id": "victorique.verify", "version": "v1"}),
                  "transport": {"kind": "injected_inert", "description": adapter.describe()},
                  "execution_grant": None, "output_schemas_path": str(schema_path), "poll_seconds": .05}
        _write(prepared / "config.json", config)
    loop = VerifyLoop.create(directory / "state", prepared / "config.json", adapter=adapter)
    return _run(directory, loop)


def _arguments(parser):
    parser.add_argument("proof")
    parser.add_argument("--problem", required=True)
    parser.add_argument("--mode", choices=("single", "stepwise", "multi"), default="single")
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--model")
    parser.add_argument("--calls", type=int, default=12)
    parser.add_argument("--hours", type=float, default=2)
    parser.add_argument("--out")
    parser.add_argument("--transport", choices=("codex", "claude", "fake"), default="codex")
    # verify has its own verdict rule; undecided_checks, minor_findings and
    # timeout_retries are solve settings.
    settings.add_arguments(parser, undecided=False, minor=False, retries=False)
    return parser


def add_parser(subparsers):
    parser = _arguments(subparsers.add_parser("verify", help="review an existing proof"))
    parser.set_defaults(handler=run_args)
    return parser


def run_args(args):
    options = settings.command_settings(getattr(args, "config", None),
                                        call_timeout_seconds=getattr(args, "call_timeout_seconds", None))
    result = verify(**{key: getattr(args, key) for key in (
        "proof", "problem", "mode", "passes", "model", "calls", "hours", "out", "transport")},
        _command=list(sys.argv), **options)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result["outcome"] == "pass" else 1 if result["outcome"] == "fail" else 2


def main(argv=None):
    parser = _arguments(argparse.ArgumentParser(description=__doc__))
    try:
        return run_args(parser.parse_args(argv))
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
