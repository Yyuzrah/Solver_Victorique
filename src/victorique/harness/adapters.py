"""Pure host command candidates and conservative public-event decoding.

Nothing in this module launches a host, reads credentials, resumes a session,
admits a mathematical fact, or declares a real adapter ready for use.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

from .hosts import SUPPORTED_HOSTS


PROTOCOLS = {
    "codex": "victorique.harness.codex_exec.events.v1",
    "claude-code": "victorique.harness.claude_print.events.v1",
    "cursor": "victorique.harness.cursor_print.events.v1",
}
SOURCES = {
    "codex": ["https://learn.chatgpt.com/docs/non-interactive-mode"],
    "claude-code": ["https://code.claude.com/docs/en/headless",
                    "https://code.claude.com/docs/en/cli-reference",
                    "https://code.claude.com/docs/en/agent-sdk/typescript"],
    "cursor": ["https://cursor.com/docs/cli/reference/output-format",
               "https://cursor.com/docs/cli/reference/parameters",
               "https://cursor.com/docs/cli/reference/permissions",
               "https://cursor.com/docs/cli/headless"],
}
# Only a public help/version observation, not a live acceptance allowlist.
PUBLIC_HELP_OBSERVED_VERSIONS = {"codex": ("0.167.0-alpha.4.1",), "claude-code": (), "cursor": ()}


def _path(value: str | Path | None, label: str) -> str:
    if value is None or not Path(value).is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    value = str(value)
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _observed_flag(profile: dict, flag: str) -> bool:
    # Probe output is the evidence; a hand-edited capabilities boolean alone
    # cannot establish that the local executable advertises an option.
    probes = profile.get("probes", [])
    if not isinstance(probes, list):
        return False
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        if (probe.get("name") not in {"help", "exec_help"} or probe.get("exit_code") != 0
                or probe.get("error") or probe.get("timed_out") or probe.get("output_truncated")):
            continue
        expected = [profile["binary"], *(["exec", "--help"] if probe["name"] == "exec_help" else ["--help"])]
        if probe.get("argv") != expected:
            continue
        text = str(probe.get("stdout", "")) + "\n" + str(probe.get("stderr", ""))
        if re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text):
            return True
    return False


def build_command_plan(profile: dict, staging: str | Path, schema_file: str | Path | None = None,
                       *, output_schema: dict | None = None, requested_model: str | None = None,
                       max_turns: int = 1) -> dict:
    """Render an argv candidate, with blocking prerequisites, without IO.

    Claude takes schema JSON in its argument, not a schema filename. Supply
    ``output_schema`` after separately reading a user-selected file. Codex uses
    ``schema_file``. Cursor has no schema-enforcement claim in this contract.
    Every result is non-runnable, including versions with public help evidence.
    """
    if not isinstance(profile, dict) or profile.get("schema") != "victorique.harness.host_profile.v1":
        raise ValueError("expected a victorique.harness.host_profile.v1 profile")
    host = profile.get("host")
    if host not in SUPPORTED_HOSTS:
        raise ValueError("host identity is unresolved or unsupported")
    staging = _path(staging, "staging")
    if schema_file is not None:
        schema_file = _path(schema_file, "schema_file")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise ValueError("output_schema must be a JSON object")
    schema_text = _json(output_schema) if output_schema is not None else None
    if type(max_turns) is not int or not 1 <= max_turns <= 100:
        raise ValueError("max_turns must be an integer in [1, 100]")
    if requested_model is not None and (not isinstance(requested_model, str) or not requested_model.strip()
                                        or requested_model.startswith("-") or any(ord(c) < 32 for c in requested_model)):
        raise ValueError("requested_model must be a nonempty model identifier")
    issues = []
    if profile.get("status") != "public_interface_observed" or profile.get("identity", {}).get("verified") is not True:
        issues.append("identity_not_verified_by_public_probe")
    version = profile.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][\w.+-]*", version):
        issues.append("public_version_missing_or_unrecognized")
    binary = profile.get("binary")
    try:
        binary = _path(binary, "profile.binary")
    except (ValueError, TypeError):
        issues.append("absolute_binary_missing")
    if isinstance(binary, str) and Path(binary).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        issues.append("shell_wrapper_not_supported")
    if host == "codex":
        required = ("--json", "--output-schema", "--output-last-message", "--sandbox", "--cd", "--ignore-user-config")
        if schema_file is None:
            issues.append("schema_file_required")
        argv = [binary, "exec", "--json", "--output-schema", schema_file,
                "--output-last-message", str(Path(staging) / "host-final.json"),
                "--cd", staging, "--sandbox", "read-only", "--ignore-user-config"]
        # This sandbox limits writes. It does not remove every tool or establish
        # nested-delegation restrictions; those remain blockers below.
    elif host == "claude-code":
        required = ("--print", "--output-format", "--verbose", "--json-schema", "--max-turns",
                    "--restricted", "--bare", "--tools", "--disallowedTools")
        if schema_text is None:
            issues.append("schema_contents_required_for_claude")
        argv = [binary, "--print", "--output-format", "stream-json", "--verbose",
                "--json-schema", schema_text, "--max-turns", str(max_turns),
                "--restricted", "--bare", "--tools", "", "--disallowedTools", "mcp__*"]
    else:
        required = ("--print", "--output-format", "--workspace", "--sandbox")
        argv = [binary, "--print", "--output-format", "stream-json", "--workspace", staging, "--sandbox", "enabled"]
    if requested_model is not None:
        required += ("--model",)
        argv += ["--model", requested_model]
    if host == "codex":
        argv.append("-")
    missing = [flag for flag in required if not _observed_flag(profile, flag)] if not issues else []
    issues.extend("required_flag_not_observed:" + flag for flag in missing)
    blockers = ["no_real_host_acceptance_for_this_exact_version", "permissions_and_nested_delegation_not_verified",
                "core_dispatch_claim_bridge_not_connected", "live_execution_not_enabled_in_this_candidate"]
    if version not in PUBLIC_HELP_OBSERVED_VERSIONS[host]:
        blockers.append("host_version_not_locally_observed_by_this_candidate")
    if host == "cursor":
        blockers += ["cursor_print_can_write_and_use_tools_without_force",
                     "cursor_effective_permission_policy_not_verified", "cursor_schema_enforcement_unavailable"]
    return {
        "schema": "victorique.harness.command_plan.v1", "host": host, "protocol": PROTOCOLS[host],
        "status": "profile_rejected" if issues else "candidate_not_ready",
        "runnable": False, "candidate_argv": None if issues else argv,
        "cwd": staging, "stdin": {"source": "exact_frozen_packet", "encoding": "utf-8", "required": True},
        "output_schema_file": schema_file, "schema_contents_supplied": output_schema is not None,
        "requested_model": requested_model, "actual_model": None,
        "host_version": version, "public_help_version_observed": version in PUBLIC_HELP_OBSERVED_VERSIONS[host],
        "host_max_turns": max_turns if host == "claude-code" else None,
        "required_flags": list(required), "profile_errors": issues, "readiness_blockers": blockers,
        "shell": False, "nested_roles_allowed": False, "permission_bypass_allowed": False,
        "read_only_recovery": "unsupported", "resume_is_query": False,
        "live_execution_verified": False, "isolation_verified": False,
        "official_sources": SOURCES[host],
    }


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON constant")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _decode(text: str):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant, parse_float=_finite_float)


def _identity(values: list, issues: list, label: str) -> str | None:
    invalid = any(not isinstance(value, str) or not value.strip() for value in values)
    valid = list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))
    if invalid:
        issues.append("invalid_" + label)
    if len(valid) > 1:
        issues.append("conflicting_" + label)
        return None
    return valid[0] if valid else None


def _usage(terminal: dict | None, issues: list) -> dict | None:
    if terminal is None or terminal.get("usage") is None:
        return None
    usage = terminal["usage"]
    if not isinstance(usage, dict):
        issues.append("invalid_usage_object")
        return None
    keys = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens")
    result = {}
    for key in keys:
        if key in usage:
            value = usage[key]
            if type(value) is not int or value < 0:
                issues.append("invalid_usage:" + key)
            else:
                result[key] = value
    return result or None


def _structured(text, issues: list) -> dict | None:
    if not isinstance(text, str):
        issues.append("missing_final_text")
        return None
    try:
        output = _decode(text)
    except (ValueError, RecursionError):
        issues.append("final_text_not_strict_json")
        return None
    if not isinstance(output, dict):
        issues.append("final_output_not_object")
        return None
    return output


# Codex Desktop 2026-11-14 update: an older CLI reports, before the turn starts, that it
# cannot start the code-mode host and that code mode "will fail closed". No tool runs, so
# this exact notice is tolerated; any other pre-turn or non-message item is not.
CODE_MODE_FAIL_CLOSED = re.compile(
    r"Code Mode is unavailable because failed to spawn code-mode host [^\n]{1,500}\\codex-code-mode-host\.exe: "
    r"host executable was not found\. Code mode will fail closed; enable `features\.code_mode_host` "
    r"and install `codex-code-mode-host`\.")


def parse_event_stream(host: str, raw_stdout: str, *, exit_code: int | None,
                       raw_stderr: str = "", truncated: bool = False,
                       requested_model: str | None = None, require_structured: bool = True) -> dict:
    """Decode one fresh, tool-free role's captured JSONL stream.

    This is a software contract check on caller-supplied evidence. Even a valid
    result does not authenticate the host, prove schema conformance, or admit a
    mathematical result. Raw stdout/stderr and every decoded event are retained.
    Usage/cost is not inferred or billed: Claude's client estimate is separate
    from actual_cost_usd, which this parser cannot establish.
    """
    if host not in SUPPORTED_HOSTS:
        raise ValueError("unsupported host")
    if not isinstance(raw_stdout, str) or not isinstance(raw_stderr, str):
        raise ValueError("capture must be text")
    if exit_code is not None and type(exit_code) is not int:
        raise ValueError("exit_code must be an integer or None")
    if type(truncated) is not bool or type(require_structured) is not bool:
        raise ValueError("truncated and require_structured must be booleans")
    issues, events, malformed = [], [], []
    if truncated:
        issues.append("capture_truncated")
    if exit_code is None:
        issues.append("process_exit_unknown")
    elif exit_code != 0:
        issues.append("process_exit_nonzero")
    if raw_stdout and not raw_stdout.endswith("\n"):
        issues.append("jsonl_final_newline_missing")
    for number, line in enumerate(raw_stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = _decode(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("event must be an object with a type")
            events.append(event)
        except (ValueError, RecursionError):
            malformed.append(number)
    if malformed:
        issues.append("malformed_event_lines")
    if not events:
        issues.append("no_events")
    terminal = None
    text = None
    output = None
    session = None
    models = []
    policy_violations = []
    tolerated = []
    host_failure = False
    if host == "codex":
        starts = [e for e in events if e["type"] == "thread.started"]
        turns = [e for e in events if e["type"] == "turn.started"]
        ends = [e for e in events if e["type"] in {"turn.completed", "turn.failed"}]
        if len(starts) != 1 or not events or events[0]["type"] != "thread.started":
            issues.append("expected_one_initial_thread_start")
        session = _identity([e.get("thread_id") for e in starts] +
                            [e["thread_id"] for e in events if e["type"] != "thread.started" and "thread_id" in e], issues, "session_id")
        if len(turns) != 1:
            issues.append("expected_one_turn_start")
        if len(ends) != 1:
            issues.append("expected_one_terminal_event")
        else:
            terminal = ends[0]
            if events[-1] is not terminal:
                issues.append("events_after_terminal")
            if turns and events.index(turns[0]) > events.index(terminal):
                issues.append("turn_start_after_terminal")
        for event in events:
            kind = event["type"]
            item = event.get("item")
            if (kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error"
                    and set(item) <= {"id", "type", "message"} and isinstance(item.get("message"), str)
                    and CODE_MODE_FAIL_CLOSED.fullmatch(item["message"])
                    and turns and events.index(event) < events.index(turns[0])):
                tolerated.append(item["message"])
                continue
            if kind in {"turn.failed", "error"}:
                host_failure = True
            elif kind.startswith("item."):
                if kind not in {"item.started", "item.updated", "item.completed"}:
                    issues.append("unsupported_event_type:" + kind)
                if not turns or events.index(event) < events.index(turns[0]):
                    issues.append("item_before_turn_start")
                item = event.get("item")
                if not isinstance(item, dict):
                    issues.append("invalid_item")
                    continue
                if item.get("type") not in {"agent_message", "reasoning", "plan_update"}:
                    policy_violations.append("tool_or_unknown_item:" + str(item.get("type")))
                if kind == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text")
            elif kind not in {"thread.started", "turn.started", "turn.completed"}:
                issues.append("unsupported_event_type:" + kind)
        if require_structured:
            output = _structured(text, issues)
        elif not isinstance(text, str):
            issues.append("missing_final_text")
    else:
        inits = [e for e in events if e["type"] == "system" and e.get("subtype") == "init"]
        ends = [e for e in events if e["type"] == "result"]
        if len(inits) != 1 or not events or events[0] is not (inits[0] if inits else None):
            issues.append("expected_one_initial_system_event")
        session = _identity([e.get("session_id") for e in events], issues, "session_id")
        if len(ends) != 1:
            issues.append("expected_one_terminal_event")
        else:
            terminal = ends[0]
            if events[-1] is not terminal:
                issues.append("events_after_terminal")
            if terminal.get("subtype") != "success" or terminal.get("is_error") is not False:
                host_failure = True
            text = terminal.get("result")
            if not isinstance(text, str):
                issues.append("missing_final_text")
            if host == "claude-code":
                output = terminal.get("structured_output")
                if require_structured and not isinstance(output, dict):
                    issues.append("missing_structured_output_object")
                    output = None
            elif require_structured:
                output = _structured(text, issues)
        for event in events:
            kind = event["type"]
            if kind == "system" and event.get("subtype") == "init":
                if "model" in event:
                    models.append(event["model"])
                if host == "claude-code" and (event.get("tools") or event.get("mcp_servers") or event.get("agents")):
                    policy_violations.append("unexpected_available_tools_or_agents")
            elif kind in {"assistant", "user"}:
                message = event.get("message")
                if not isinstance(message, dict):
                    issues.append("invalid_message")
                    continue
                if event.get("parent_tool_use_id") is not None:
                    policy_violations.append("nested_role_message")
                if kind == "assistant" and "model" in message:
                    models.append(message["model"])
                content = message.get("content", [])
                if not isinstance(content, list):
                    issues.append("invalid_message_content")
                elif any(not isinstance(block, dict) or block.get("type") not in {"text", "thinking", "redacted_thinking"} for block in content):
                    policy_violations.append("tool_or_unknown_message_content")
            elif kind == "tool_call":
                policy_violations.append("tool_call_observed")
            elif kind == "system" and event.get("subtype") == "permission_denied":
                policy_violations.append("permission_denial_observed")
            elif kind not in {"result"}:
                issues.append("unsupported_event_type:" + kind)
        if terminal and terminal.get("permission_denials"):
            policy_violations.append("permission_denials_in_terminal")
    if session is None:
        issues.append("session_identity_unresolved")
    if host_failure:
        issues.append("host_reported_failure")
    if policy_violations:
        issues.append("tool_free_policy_violated")
    actual_model = _identity(models, issues, "model")
    # A model alias/display name remains precisely that returned string. We do
    # not resolve it to a model revision or fill it from requested_model.
    usage = _usage(terminal, issues) if host in {"codex", "claude-code"} else None
    reported_cost = None
    if host == "claude-code" and terminal is not None and terminal.get("total_cost_usd") is not None:
        amount = terminal["total_cost_usd"]
        if type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0:
            issues.append("invalid_reported_cost")
        else:
            reported_cost = amount
    return {
        "schema": "victorique.harness.parsed_capture.v1", "protocol": PROTOCOLS[host], "host": host,
        "software_contract_valid": not issues,
        "host_outcome": "reported_failure" if host_failure else ("reported_success" if not issues else "unknown"),
        "issues": list(dict.fromkeys(issues)), "policy_violations": list(dict.fromkeys(policy_violations)),
        **({"tolerated_host_notices": tolerated} if tolerated else {}),
        "session_id": session, "terminal_observed": terminal is not None,
        "final_text": text, "structured_output": output, "schema_validated": False,
        "requested_model": requested_model, "actual_model": actual_model,
        "reported_models": list(dict.fromkeys(model for model in models if isinstance(model, str))),
        "usage": usage, "usage_scope": "main_agent_loop_only" if host == "claude-code" and usage is not None else None,
        "model_usage_raw": terminal.get("modelUsage") if host == "claude-code" and terminal else None,
        "reported_cost_usd": reported_cost,
        "cost_kind": "host_client_estimate" if reported_cost is not None else None,
        "actual_cost_usd": None, "financially_settled": False,
        "exit_code": exit_code, "truncated": truncated, "malformed_line_numbers": malformed,
        "raw_stdout": raw_stdout, "raw_stderr": raw_stderr, "raw_events": events,
        "live_execution_verified": False, "isolation_verified": False,
        "mathematical_result": None, "official_sources": SOURCES[host],
    }
