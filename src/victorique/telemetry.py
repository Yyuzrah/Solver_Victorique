"""Call-level projections of existing request and CLI transport records."""
import base64
from contextlib import closing
import json
import re
import sqlite3

from . import retries
from .adaptive_exchange import LOCAL_TIMEOUT, TERMINAL
from .call_evidence import evidence
from .harness.codex_process import STOP_KILL_REASON
from .harness.routed_transport import members, service_of
from .harness.safety import checked_path


TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens",
              "cache_creation_input_tokens", "input_tokens_including_cache")


def token_counts(usage, transport):
    """Preserve vendor counters; normalize cache-inclusive input separately."""
    if not isinstance(usage, dict):
        return None
    tokens = {key: usage[key] if type(usage.get(key)) is int and usage[key] >= 0 else None
              for key in TOKEN_KEYS}
    if transport == "claude_cli":
        read = usage.get("cache_read_input_tokens")
        tokens["cached_input_tokens"] = read if type(read) is int and read >= 0 else None
        parts = [tokens[k] for k in ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens")]
        tokens["input_tokens_including_cache"] = sum(parts) if all(p is not None for p in parts) else None
    elif transport == "codex_cli":
        tokens["input_tokens_including_cache"] = tokens["input_tokens"]
    return tokens


def _effort(carrier):
    encoded = carrier.get("streams_base64", {}).get("stderr")
    if encoded is None:
        return None
    try:
        stderr = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
    values = set(re.findall(r"(?m)^reasoning effort:\s*([A-Za-z0-9_-]+)\s*$", stderr))
    return next(iter(values)) if len(values) == 1 else None


def call_rows(loop, policy, base=None):
    # Read existing projections without replaying the controller or mutating its
    # state. This output never authorizes work or serves as a recovery source.
    loop.core._guard()
    invocation_path = checked_path(loop.root.parent / "invocation.json")
    invocation = json.loads(invocation_path.read_text(encoding="utf-8")) if invocation_path.is_file() else {}
    defaults = invocation.get("cli_defaults") or {}
    parameters = invocation.get("parameters") or {}
    # The solver's service, and the checks' when they use another one.
    specs = {"solver": loop.config["transport"]}
    if "check_transport" in loop.config:
        specs["checks"] = loop.config["check_transport"]
    with closing(sqlite3.connect(loop.core.path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events ORDER BY sequence")]
        if base is None:
            requests = [json.loads(row[0]) for row in db.execute("SELECT payload FROM requests ORDER BY rowid")]
            base = {"manifest": loop.config["manifest"], "requests": {row["request_id"]: row for row in requests},
                    "observations": [json.loads(row[0]) for row in db.execute("SELECT payload FROM observations")]}
        raw_responses = {}
        for observation in base["observations"]:
            if observation.get("parsed_response") is None:
                row = db.execute("SELECT raw FROM observations WHERE observation_id=?", (observation["observation_id"],)).fetchone()
                if row is not None:
                    raw_responses[observation["observation_id"]] = row[0]
    carriers = {}
    if loop.config["transport"]["kind"] in ("codex_cli", "claude_cli"):
        for service, adapter in members(loop.adapter):
            with adapter._db() as db:
                rows = db.execute("SELECT j.claim_id, o.carrier FROM jobs j LEFT JOIN observations o USING(job_id) "
                                  "ORDER BY CASE WHEN o.phase LIKE 'worker_finished:victorique.harness.codex_contract.v%' THEN 2 "
                                  "WHEN o.phase='worker_finished' THEN 1 ELSE 0 END, o.rowid").fetchall()
            carriers.update({row[0]: {} if row[1] is None else json.loads(row[1]) for row in rows})
    claims = {event["data"]["record"]["request_id"]: event["recorded_at_utc"]
              for event in events if event["kind"] == "claim"}
    observations = {row["observation_id"]: row for row in base["observations"]}
    result = []
    for rid, request in base["requests"].items():
        if request["claim_id"] is None:
            continue
        carrier = carriers.get(request["claim_id"], {})
        parser = carrier.get("parser") or {}
        capture = carrier.get("process_capture") or {}
        service = service_of(request["record"]["kind"]) if "checks" in specs else "solver"
        spec = specs[service]
        transport = spec["kind"]
        # Codex CLI configuration defaults say nothing about another CLI's calls.
        configured = defaults if transport == "codex_cli" or "checks" not in specs else {}
        tokens = token_counts(parser.get("usage"), transport)
        observation_id = request["first_result_observation_id"]
        observation = observations.get(observation_id, {})
        observed = observation.get("record", {})
        response = observation.get("parsed_response")
        if response is None and raw_responses.get(observation_id) is not None:
            try:
                response = json.loads(raw_responses[observation_id])
            except (ValueError, UnicodeError, RecursionError):
                pass
        output = response.get("output") if isinstance(response, dict) else None
        synthetic = loop.config["transport"]["kind"] not in ("codex_cli", "claude_cli")
        started = claims.get(rid) if synthetic else capture.get("worker_started_at")
        ended = observed.get("external_observed_at_utc") if synthetic else capture.get("finished_at_utc")
        phase = request["phase"]
        status = "completed" if phase == "returned_valid" else phase if phase in TERMINAL else "unknown"
        if capture.get("timed_out") or observed.get("reason") == LOCAL_TIMEOUT:
            status = "timed_out"
        elif capture.get("kill_reason") == STOP_KILL_REASON and phase not in TERMINAL:
            # The run's stop request ended this call in flight (or before its CLI started).
            status = "stopped"
        result.append({"run_id": base["manifest"]["session_id"], "request_id": rid,
                       # The timed-out request that this call sends again, if any.
                       "retry_of": retries.retry_of(rid),
                       "policy": policy, "rung": (next((part for part in rid.split(".") if part in ("L0", "L1", "L2", "L3")), None) if policy == "ladder" else None), "role": request["record"]["role"],
                       "model": parser.get("actual_model"), "effort": _effort(carrier),
                       # solver: the solver's model service; checks: the separate service of the two checks.
                       "service": service, "transport": transport,
                       "requested_model": parameters.get("check_model" if service == "checks" else "model"),
                       "requested_reasoning_effort": parameters.get(
                           "check_reasoning_effort" if service == "checks" else "reasoning_effort"),
                       "model_source": parser.get("actual_model_source"),
                       "model_observed_at_utc": (ended or started) if parser.get("actual_model") else None,
                       "cli_version": (spec.get("profile") or {}).get("version"),
                       "configured_model_source": configured.get("source"),
                       "token_source": parser.get("usage_source"),
                       "input_token_semantics": {"codex_cli": "includes_cached_input",
                                                 "claude_cli": "excludes_cache_read_and_creation"}.get(transport),
                       "configured_model": configured.get("model"), "configured_effort": configured.get("effort"),
                       "started_at_utc": started, "ended_at_utc": ended, "status": status, "tokens": tokens,
                       **evidence(carrier, loop.config if service == "solver" else spec, output)})
    return result


def totals(rows):
    tokens = None if not rows or all(row["tokens"] is None for row in rows) else {
        key: sum(row["tokens"][key] for row in rows)
        if all(row["tokens"] is not None and row["tokens"].get(key) is not None for row in rows) else None
        for key in TOKEN_KEYS}
    models = [row["model"] for row in rows]
    model = models[0] if models and all(value and value == models[0] for value in models) else None
    return tokens, model


def write_calls(directory, rows):
    # Rebuild the public projection so resume/export never append duplicate calls.
    checked_path(directory / "calls.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
