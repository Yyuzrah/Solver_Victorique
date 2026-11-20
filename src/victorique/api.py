"""Public problem-solving API and run-directory operations."""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from . import adaptive_contracts as contracts, settings, stop_request
from .fake import FakeTransport
from .harness.codex_contract import DEFAULT_MODEL, conservative_codex_argv, validate_reasoning_effort
from .harness.claude_contract import complete_claude_profile, conservative_claude_argv
from .harness.hosts import inspect_host
from .harness.safety import checked_path
from .loop import OriginalProblemLoop, CONFIG
from .prepare import build_manifest, prepare
from .single import SingleLoop
from .ladder import LadderLoop
from .resample import ResampleLoop
from .bestofn import BestofnLoop, estimate_from_full, selection_schema
from .telemetry import call_rows, totals, write_calls


def _bounded_deadline(started, hours, deadline_utc):
    """An explicit UTC deadline can only shorten the original duration."""
    try:
        deadline = started + timedelta(hours=hours)
    except OverflowError as error:
        raise ValueError("hours exceeds the supported date range") from error
    if deadline_utc is not None:
        if (type(deadline_utc) is not str or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", deadline_utc) is None):
            raise ValueError("deadline_utc requires an ISO timestamp with explicit UTC timezone")
        deadline = min(deadline, contracts.utc(deadline_utc))
        if deadline <= datetime.now(timezone.utc):
            raise ValueError("deadline_utc has expired")
    return deadline.isoformat()


def _write(path, value):
    checked_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _read(path):
    return json.loads(checked_path(path).read_text(encoding="utf-8"))


def _model_name(value, name):
    if value is not None and (type(value) is not str or not value or value.strip() != value or value.startswith("-")
                              or any(ch.isspace() for ch in value)):
        raise ValueError(name + " must be an identifier")


def check_service(transport, model, reasoning_effort, check_transport=None, check_model=None,
                  check_reasoning_effort=None):
    """The model service, model and reasoning effort of the statement and proof checks.

    An omitted check transport is the solver's. With the solver's transport, an
    omitted check model or effort is the solver's value; with another
    transport it is that CLI's own default. A practice run (fake) uses the fake
    transport for both.
    """
    check_transport = transport if check_transport is None else check_transport
    if check_transport not in ("codex", "claude", "fake"):
        raise ValueError("check_transport must be codex, claude or fake")
    if (transport == "fake") != (check_transport == "fake"):
        raise ValueError("a practice run uses the fake transport for solving and for the checks; "
                         "choose fake for both or for neither")
    _model_name(check_model, "check_model")
    try:
        validate_reasoning_effort(check_reasoning_effort)
    except ValueError as error:
        raise ValueError("check_" + str(error)) from None
    if check_reasoning_effort is not None and check_transport != "codex":
        raise ValueError("check_reasoning_effort is only supported by the codex transport")
    if check_transport == transport:
        check_model = model if check_model is None else check_model
        check_reasoning_effort = reasoning_effort if check_reasoning_effort is None else check_reasoning_effort
    return check_transport, check_model, check_reasoning_effort


def _probe(transport, model, reasoning_effort, directory):
    """The public profile of one CLI service, checked for the flags a run needs."""
    host = "claude-code" if transport == "claude" else "codex"
    binary = os.environ.get("VICTORIQUE_" + transport.upper() + "_BINARY")
    profile = inspect_host(host, binary) if binary else inspect_host(host)
    if profile["status"] != "public_interface_observed":
        raise ValueError(host + " CLI unavailable: install its native executable on PATH and complete its login")
    selected = model or DEFAULT_MODEL
    if transport == "claude":
        profile = complete_claude_profile(profile)
        conservative_claude_argv(profile, {"type": "object"}, selected)
    else:
        if reasoning_effort is not None:
            profile = {**profile, "reasoning_effort": reasoning_effort}
        conservative_codex_argv(profile, {"workspace": directory / "probe-workspace", "schema_file": directory / "probe-schema.json",
                                         "final_file": directory / "probe-output.json"}, selected)
    return profile


def _authorization(transport, run_id, profile, model, calls, deadline, name):
    return {"schema": "victorique." + transport + "_execution_authorization.v1", "authorization_id": run_id + "." + name,
            "source_ref": "User solve invocation in invocation.json", "allow_execution": True,
            "session_id": run_id, "protocol": {"protocol_id": "victorique.solve", "version": "v1"},
            "binary": profile["binary"], "host_version": profile["version"], "requested_model": model or DEFAULT_MODEL,
            "max_process_starts": calls, "expires_at_utc": deadline,
            "permission_review_ref": "User solve command; read-only role workspace",
            "authentication_precondition": "operator_completed_host_managed_login"}


def _route_checks(config, check_transport, profile, authorization):
    """Give the two check roles their own model service in a prepared run config."""
    from .harness import claude_transport, codex_transport
    from .harness.routed_transport import ADAPTER_ID, VERSION
    module = claude_transport if check_transport == "claude" else codex_transport
    module._authorize({"profile": profile, "authorization": authorization})
    spec = config["transport"]
    config["check_transport"] = {"kind": check_transport + "_cli", "version": module.VERSION, "profile": profile,
                                 "authorization": authorization, "timeout_seconds": spec["timeout_seconds"],
                                 "max_capture_bytes": spec["max_capture_bytes"]}
    request = {"provider_id": check_transport + "-cli", "model_id": authorization["requested_model"], "revision": None}
    for role in ("alignment_reviewer", "proof_reviewer"):
        config["manifest"]["role_bindings"][role]["model_request"] = dict(request)
    config["execution_grant"].update(adapter_id=ADAPTER_ID, adapter_version=VERSION)
    return config


def _mcp_names(binary, directory):
    """Read only public server names; never persist transport or environment data."""
    try:
        result = subprocess.run([binary, "--disable", "plugins", "--disable", "apps",
                                 "mcp", "list", "--json"], cwd=directory,
                                capture_output=True, timeout=30, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("Codex MCP inventory unavailable")
        rows = json.loads(result.stdout)
        if type(rows) is not list or any(type(row) is not dict or type(row.get("name")) is not str
                                        or not row["name"] for row in rows):
            raise ValueError("Codex MCP inventory has an unsupported shape")
        return sorted({row["name"] for row in rows})
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise ValueError("Codex MCP inventory unavailable") from error


def _open(directory):
    directory = checked_path(Path(directory).absolute())
    invocation = _read(directory / "invocation.json")
    if invocation.get("resume_module"):
        raise ValueError("resume this paired run with python -m " + invocation["resume_module"] + " resume PAIRED_DIRECTORY")
    config = _read(directory / "state" / "run-config.json")
    adapter = FakeTransport() if config["manifest"]["mode"] == "synthetic" else None
    policy = invocation["parameters"].get("policy", "full")
    loop_type = {"single": SingleLoop, "ladder": LadderLoop, "resample": ResampleLoop, "bestofn": BestofnLoop, "full": OriginalProblemLoop}[policy]
    return directory, loop_type.open(directory / "state", adapter=adapter)


def _summary(directory, loop, snapshot=None, rows=None):
    snapshot = snapshot or _snapshot(loop)
    endpoint = snapshot["endpoint"]
    requests = snapshot["exchange"]["requests"].values()
    claimed = sum(row["claim_id"] is not None for row in requests)
    decision = None if endpoint is None else (endpoint.get("reducer_endpoint") or {}).get("decision")
    outcome = {"complete": "complete", "abstain": "abstain", "stop": "partial"}.get(
        decision, "partial" if endpoint is not None else "pending")
    reason = None if endpoint is None else endpoint.get("reason")
    if reason is None and endpoint is not None and (
            (endpoint.get("reducer_endpoint") or {}).get("reason") == stop_request.REASON):
        # The full workflow records a stop request as its controller's local stop.
        reason = stop_request.REASON
    invocation = _read(directory / "invocation.json")
    policy = invocation["parameters"].get("policy", "full")
    rows = _calls(loop, policy) if rows is None else rows
    usage, model = totals(rows)
    parameters = invocation["parameters"]
    separate = "check_transport" in loop.config
    if separate:
        # The top-level model fields describe the solver's service; check_service the checks'.
        model = totals([row for row in rows if row.get("service") != "checks"])[1]
        checks = [row for row in rows if row.get("service") == "checks"]
    ended = datetime.now(timezone.utc) if endpoint is None else datetime.fromisoformat(endpoint["frozen_at_utc"].replace("Z", "+00:00"))
    result = {"run_id": loop.config["manifest"]["session_id"], "policy": policy, "outcome": outcome, "calls_used": claimed,
            "endpoint_reason": reason,
            "calls_limit": loop.config["manifest"]["limits"]["global_call_limit"],
            "wall_seconds": max(0, (ended - datetime.fromisoformat(invocation["started_at"])).total_seconds()),
            "deadline_utc": loop.config["manifest"]["limits"]["stop_condition"]["deadline_utc"],
            "transport": invocation["parameters"]["transport"], "synthetic": invocation["parameters"]["transport"] == "fake",
            "requested_model": invocation["parameters"]["model"], "reported_model": model,
            "cli_defaults": invocation.get("cli_defaults"),
            "cli_version": loop.config["transport"].get("profile", {}).get("version"), "tokens": usage,
            "proof_available": endpoint is not None and endpoint["root_candidate"] is not None,
            # Runs recorded before the setting existed repeat no call.
            "timeout_retries_per_call": invocation["parameters"].get("timeout_retries", 0),
            "timeout_retries_used": sum(row.get("retry_of") is not None for row in rows),
            # None when the two checks use the solver's model service.
            "check_service": None if not separate else {
                "transport": parameters.get("check_transport"), "requested_model": parameters.get("check_model"),
                "requested_reasoning_effort": parameters.get("check_reasoning_effort"),
                "cli_version": (loop.config["check_transport"].get("profile") or {}).get("version"),
                "reported_model": totals(checks)[1], "calls": len(checks)},
            "output_directory": str(directory)}
    if invocation["parameters"].get("reasoning_effort") is not None:
        result["requested_reasoning_effort"] = invocation["parameters"]["reasoning_effort"]
    if isinstance(loop, LadderLoop):
        result.update(loop.diagnostics(snapshot))
    if isinstance(loop, BestofnLoop):
        result.update(loop.diagnostics(snapshot, rows))
    return result


def _snapshot(loop):
    return loop.reporting_snapshot() if hasattr(loop, "reporting_snapshot") else loop.core.snapshot()


def _calls(loop, policy):
    rows = call_rows(loop, policy)
    if isinstance(loop, LadderLoop) and (loop.root / "escalated").exists():
        extra = call_rows(loop._child(), policy)
        for row in extra:
            row["request_id"] = "L3." + row["request_id"]
            row["retry_of"] = None if row["retry_of"] is None else "L3." + row["retry_of"]
            row["rung"] = "L3"
        rows.extend(extra)
    return rows


def status(directory):
    """Read a run's current state without starting a role."""
    directory, loop = _open(directory)
    return _summary(directory, loop)


def _export(directory, loop):
    snapshot = _snapshot(loop)
    policy = _read(directory / "invocation.json")["parameters"].get("policy", "full")
    rows = _calls(loop, policy)
    write_calls(directory, rows)
    result = _summary(directory, loop, snapshot, rows)
    endpoint = snapshot["endpoint"]
    candidate = None if endpoint is None else endpoint["root_candidate"]
    if candidate is None:
        proof = "# No proof delivered\n\nOutcome: " + result["outcome"] + "\n"
    else:
        proof = candidate["content"]
        root_ref = contracts.candidate_ref(candidate)
        for node in endpoint["root_dependency_closure"]:
            if not contracts.same_json(contracts.candidate_ref(node), root_ref):
                proof += "\n\n---\n\n## Dependency: " + node["obligation_id"] + "\n\n" + node["content"]
    checked_path(directory / "proof.md").write_text(proof, encoding="utf-8")
    _write(directory / "summary.json", result)
    return result


def export(directory):
    """Write the latest proof and summary into the existing run directory."""
    directory, loop = _open(directory)
    return _export(directory, loop)


def _deadline(loop):
    value = loop.config["manifest"]["limits"]["stop_condition"]["deadline_utc"]
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _run(directory, loop):
    initial = _snapshot(loop)
    policy = _read(directory / "invocation.json")["parameters"].get("policy", "full")
    write_calls(directory, _calls(loop, policy))
    used = sum(row["claim_id"] is not None for row in initial["exchange"]["requests"].values())
    limit = loop.config["manifest"]["limits"]["global_call_limit"]
    kinds = {rid: row["record"]["kind"] for rid, row in initial["exchange"]["requests"].items()}
    def progress(row):
        nonlocal used
        rid = row.get("request_id")
        if row.get("kind"):
            kinds[rid] = row["kind"]
        if row["phase"] == "original_prepared_dispatched":
            used += sum(bool(result.get("dispatched")) for result in row.get("results", [row.get("result", {})]))
        if rid is not None and rid not in kinds:
            # A call sent again after a timeout is prepared without a progress line of its own.
            current = loop.core.snapshot()["exchange"]["requests"].get(rid)
            if current is not None:
                kinds[rid] = current["record"]["kind"]
        kind = kinds.get(rid, row["phase"])
        if rid is None and row["phase"] == "native_endpoint" and row.get("reason") == stop_request.REASON:
            kind = stop_request.REASON  # The run ended at its stop request.
        role = (contracts.DEFAULT_ROLES | contracts.OPTIONAL_ROLES).get(kind, "controller")
        if row["phase"] in ("original_prepared_dispatched", "original_response_observed"):
            write_calls(directory, _calls(loop, policy))
        print(f"{kind} | {role} | {used}/{limit} | {row['phase']}", flush=True)
        if len(row.get("results", [])) > 1:
            current = loop.core.snapshot()["exchange"]["requests"]
            for extra in row["results"][1:]:
                extra_kind = current[extra["request_id"]]["record"]["kind"]
                print(f"{extra_kind} | {contracts.DEFAULT_ROLES[extra_kind]} | {used}/{limit} | {row['phase']}", flush=True)
    try:
        # One local observation bound (max_steps polls) can end before a long run does, with a
        # call still in flight. Keep observing the same run until it ends or its own absolute
        # deadline passes. This only waits longer; the loop decides every dispatch as before.
        while True:
            result = loop.run(on_step=progress)
            if result.get("phase") != "local_observation_bound" or datetime.now(timezone.utc) >= _deadline(loop):
                break
    except KeyboardInterrupt:
        print("Interrupted. Resume this directory to observe the existing request.", file=sys.stderr)
    return _export(directory, loop)


def resume(directory):
    """Continue with the original model, budget and absolute deadline.

    A run that has already ended, for example one stopped by its stop request,
    sends no call: resume says so on stderr and returns its summary.
    """
    directory, loop = _open(directory)
    if loop.core.snapshot()["endpoint"] is not None:
        result = _export(directory, loop)
        print(f"This run has already finished (outcome {result['outcome']}, end reason "
              f"{result['endpoint_reason']}); resume sends no model call.", file=sys.stderr, flush=True)
        return result
    return _run(directory, loop)


def stop(directory):
    """Ask the run in ``directory`` to stop; see victorique.stop_request.request.

    Records ``stop-request.json`` in the run folder. A running run then sends no
    new model call, ends the call in flight and finishes with the end reason
    stopped_by_user. Refuses a folder that is not a run; for a finished run it
    records nothing and says so.
    """
    return stop_request.request(directory)


def solve(problem, *, policy="single", model=None, calls=12, hours=2, out=None, transport="codex", repair_rounds=2, investigate=False, escalate=False, full_calls=None, full_references=None, deadline_utc=None, reasoning_effort=None, call_timeout_seconds=None, undecided_checks=None, minor_findings=None, timeout_retries=None, check_transport=None, check_model=None, check_reasoning_effort=None, _command=None):
    """Solve a UTF-8 problem file using a bounded workflow; return its summary.

    ``call_timeout_seconds`` limits each model call (None: 600 seconds).
    ``undecided_checks`` is ``"abstain"`` (None) or ``"repair"``; see docs/ladder.md.
    ``minor_findings`` is ``"nonblocking"`` (None) or ``"blocking"``; see docs/ladder.md.
    ``timeout_retries`` (0-3, None: 1) is how often a call that stopped at the
    per-call limit is sent again; each repetition is a new call within ``calls``
    and the run's deadline.
    ``check_transport``, ``check_model`` and ``check_reasoning_effort`` choose the
    model service of the statement and proof checks (None: the solver's, see
    ``check_service``); every other role uses the solver's service.
    """
    directory, loop = _create_run(problem, policy=policy, model=model, calls=calls, hours=hours,
        out=out, transport=transport, repair_rounds=repair_rounds, investigate=investigate,
        escalate=escalate, full_calls=full_calls, full_references=full_references, deadline_utc=deadline_utc,
        reasoning_effort=reasoning_effort, call_timeout_seconds=call_timeout_seconds,
        undecided_checks=undecided_checks, minor_findings=minor_findings, timeout_retries=timeout_retries,
        check_transport=check_transport, check_model=check_model, check_reasoning_effort=check_reasoning_effort,
        _command=_command)
    return _run(directory, loop)


def _create_run(problem, *, policy="single", model=None, calls=12, hours=2, out=None, transport="codex",
                repair_rounds=2, investigate=False, escalate=False, full_calls=None, full_references=None, deadline_utc=None,
                reasoning_effort=None, call_timeout_seconds=None, undecided_checks=None, minor_findings=None,
                timeout_retries=None, check_transport=None, check_model=None, check_reasoning_effort=None,
                _command=None, _loop_type=None, _process_starts=None, _started_at=None, _adapter=None,
                _resume_module=None):
    """Initialize the ordinary frozen run without driving its controller."""
    contracts.integer(calls, "calls", 2, 100)
    if policy not in ("single", "ladder", "resample", "bestofn", "full"):
        raise ValueError("policy must be single, ladder, resample, bestofn or full")
    contracts.integer(repair_rounds, "repair_rounds", 0, 100)
    call_timeout_seconds = settings.call_timeout_seconds(call_timeout_seconds)
    undecided_checks = settings.undecided_checks(undecided_checks)
    minor_findings = settings.minor_findings(minor_findings)
    timeout_retries = settings.timeout_retries(timeout_retries)
    if type(investigate) is not bool or type(escalate) is not bool:
        raise ValueError("investigate and escalate must be boolean")
    if policy == "resample" and (investigate or escalate):
        raise ValueError("resample does not support investigate or escalate")
    if policy == "bestofn" and (investigate or escalate or repair_rounds != 2):
        raise ValueError("bestofn does not support investigate, escalate or repair-rounds overrides")
    if policy != "bestofn" and (full_calls is not None or full_references is not None):
        raise ValueError("full-calls and full-references are only supported by bestofn")
    if type(hours) not in (int, float) or not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a positive finite number")
    if transport not in ("codex", "claude", "fake"):
        raise ValueError("transport must be codex, claude or fake")
    validate_reasoning_effort(reasoning_effort)
    if reasoning_effort is not None and transport != "codex":
        raise ValueError("reasoning_effort is only supported by the codex transport")
    _model_name(model, "model")
    check_transport, check_model, check_reasoning_effort = check_service(
        transport, model, reasoning_effort, check_transport, check_model, check_reasoning_effort)
    problem_path = checked_path(Path(problem).absolute())
    raw = problem_path.read_bytes()
    contracts.validate_problem({"artifact_id": "input", "revision": 1, "content": raw.decode("utf-8")})
    bestofn_estimate = estimate_from_full(full_calls, raw.decode("utf-8"), calls, full_references) if policy == "bestofn" else None
    unexecuted = bestofn_estimate is not None and bestofn_estimate["unexecuted_reason"] is not None
    started = _started_at or datetime.now(timezone.utc)
    deadline = _bounded_deadline(started, hours, deadline_utc)
    run_id = "run-" + uuid4().hex
    directory = checked_path(Path(out).absolute() if out is not None else Path.cwd() / "runs" / run_id)
    if directory.exists():
        raise ValueError("output directory already exists; use resume or choose a new --out")
    selected_model = model or DEFAULT_MODEL
    # The checks get their own transport only when their service, model or effort differs.
    separate = (transport != "fake" and not unexecuted
                and (check_transport, check_model, check_reasoning_effort) != (transport, model, reasoning_effort))
    profile = check_profile = None
    if transport != "fake" and not unexecuted:
        profile = _probe(transport, model, reasoning_effort, directory)
        if separate:
            check_profile = _probe(check_transport, check_model, check_reasoning_effort, directory)
    parameters = {"problem": str(problem), "policy": policy, "model": model, "calls": calls, "hours": hours, "out": None if out is None else str(out), "transport": transport, "repair_rounds": repair_rounds, "investigate": investigate, "escalate": escalate,
                  "call_timeout_seconds": call_timeout_seconds, "undecided_checks": undecided_checks,
                  "minor_findings": minor_findings, "timeout_retries": timeout_retries,
                  "check_transport": check_transport, "check_model": check_model,
                  "check_reasoning_effort": check_reasoning_effort}
    if deadline_utc is not None:
        parameters["deadline_utc"] = deadline_utc
    if reasoning_effort is not None:
        parameters["reasoning_effort"] = reasoning_effort
    if policy == "bestofn":
        parameters["full_calls"] = str(full_calls)
        parameters["full_references"] = None if full_references is None else str(full_references)
    invocation = {"started_at": started.isoformat(), "argv": _command, "parameters": parameters}
    if _resume_module is not None:
        invocation["resume_module"] = _resume_module
    directory.mkdir(parents=True)
    codex_profile = profile if transport == "codex" else check_profile if separate and check_transport == "codex" else None
    if codex_profile is not None and not unexecuted:
        from .harness.codex_defaults import read_cli_defaults
        invocation["cli_defaults"] = read_cli_defaults(codex_profile["binary"], directory)
    _write(directory / "invocation.json", invocation)
    # The plain path; a character that stdout cannot encode is written as a backslash escape.
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print("Output: " + str(directory).encode(encoding, "backslashreplace").decode(encoding), flush=True)
    prepared = directory / "prepared"
    if transport != "fake" and not unexecuted:
        for service_profile, service_transport in ((profile, transport), (check_profile, check_transport)):
            if service_profile is not None and service_transport == "codex":
                service_profile["disabled_mcp_servers"] = _mcp_names(service_profile["binary"], directory)
        _write(directory / "profile.json", profile)
        auth = _authorization(transport, run_id, profile, model, calls, deadline, "authorization")
        _write(directory / "authorization.json", auth)
        if separate:
            check_auth = _authorization(check_transport, run_id, check_profile, check_model, calls, deadline,
                                        "check-authorization")
            _write(directory / "check-profile.json", check_profile)
            _write(directory / "check-authorization.json", check_auth)
        prepare(problem_path=problem_path, profile_path=directory / "profile.json", authorization_path=directory / "authorization.json",
                session_id=run_id, run_id=run_id, provider_id=transport + "-cli", model=selected_model, calls=calls,
                deadline=deadline, output=prepared,
                timeout_seconds=settings.run_call_timeout(call_timeout_seconds, hours * 3600))
        adapter = None
    else:
        prepared.mkdir()
        schema = files("victorique").joinpath("adaptive-output-schemas.v1.json").read_bytes()
        (prepared / "adaptive-output-schemas.v1.json").write_bytes(schema)
        adapter = _adapter if _adapter is not None else FakeTransport()
        config = {"schema": CONFIG, "manifest": build_manifest(problem_raw=raw, session_id=run_id, run_id=run_id,
                      model="fake", calls=calls, deadline=deadline, source_ref="User fake solve", provider_id="synthetic", mode="synthetic"),
                  "transport": {"kind": "injected_inert", "description": adapter.describe()},
                  "execution_grant": None, "output_schemas_path": str(prepared / "adaptive-output-schemas.v1.json"), "poll_seconds": .05}
        _write(prepared / "config.json", config)
    config = _read(prepared / "config.json")
    if separate:
        config = _route_checks(config, check_transport, check_profile, check_auth)
    if policy == "bestofn":
        config["bestofn"] = bestofn_estimate
        config["manifest"]["limits"]["action_caps"]["compare_candidates"] = 1
        config["manifest"]["limits"]["action_roles"]["compare_candidates"] = "critic"
        schemas = _read(config["output_schemas_path"])
        schemas["compare_candidates"] = selection_schema()
        _write(config["output_schemas_path"], schemas)
    if policy in ("single", "ladder", "resample"):
        # Claude Code roles run one at a time, also when only the checks use it.
        claude = transport == "claude" or (separate and check_transport == "claude")
        config["manifest"]["limits"]["max_inflight"] = 1 if claude else 2
    if minor_findings != settings.MINOR_FINDINGS[0]:
        # Frozen with the run, so every policy, the full controller, an escalated full
        # child and resume read the same rule. Limits without the key mean nonblocking.
        config["manifest"]["limits"]["minor_findings"] = minor_findings
    if _process_starts is not None:
        contracts.integer(_process_starts, "physical process starts", 1, calls)
        if transport != "fake" and not unexecuted:
            config["transport"]["authorization"]["max_process_starts"] = _process_starts
    _write(prepared / "config.json", config)
    loop_type = _loop_type or {"single": SingleLoop, "ladder": LadderLoop, "resample": ResampleLoop, "bestofn": BestofnLoop, "full": OriginalProblemLoop}[policy]
    loop = loop_type.create(directory / "state", prepared / "config.json", adapter=adapter)
    return directory, loop
