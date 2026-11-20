"""Pure, capability-checked Codex argv and native-envelope capture contracts.

This module performs no IO or dispatch. The caller retains every original byte
and supplies local-process evidence. Domain output validation stays in the core.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

from .hosts import parse_host_probe
from .adapters import parse_event_stream


CODEX_VERSION = "0.167.0-alpha.4.1"  # Historical test fixture only.
DEFAULT_MODEL = "cli-default"
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CONTRACT_VERSION = "victorique.harness.codex_contract.v4"
REQUIRED_EXEC_FLAGS = ("--json", "--output-schema", "--output-last-message", "--cd", "--sandbox",
                       "--config", "--disable", "--ephemeral", "--skip-git-repo-check", "--color", "--model")
DISABLED_FEATURES = ("multi_agent", "shell_tool", "apps", "goals", "memories", "plugins")


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def validate_reasoning_effort(value):
    """Validate an optional request; model-specific availability belongs to Codex."""
    _need(value is None or (type(value) is str and value in REASONING_EFFORTS),
          "reasoning_effort must be one of " + ", ".join(REASONING_EFFORTS))


def _finite_json(value):
    """Validate ordinary finite JSON types without coercing keys or tuples."""
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is float:
        _need(math.isfinite(value), "non-finite JSON number")
        return
    if type(value) is list:
        for item in value:
            _finite_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            _need(type(key) is str, "JSON object keys must be strings")
            _finite_json(key)
            _finite_json(item)
        return
    raise ValueError("non-JSON value")


def _canonical(value):
    try:
        _finite_json(value)
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (RecursionError, UnicodeError, TypeError) as error:
        raise ValueError("invalid finite UTF-8 JSON") from error


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        _need(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("non-finite JSON constant")


def _float(value):
    result = float(value)
    _need(math.isfinite(result), "non-finite JSON number")
    return result


def _loads(text):
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
        _canonical(value)
        return value
    except (RecursionError, UnicodeError, TypeError) as error:
        raise ValueError("invalid finite UTF-8 JSON") from error


def _absolute(value, label):
    _need(isinstance(value, (str, Path)), label + " must be a path")
    text = str(value)
    _need(text and not any(ord(char) < 32 for char in text), label + " contains control characters")
    path = Path(text)
    _need(path.is_absolute(), label + " must be absolute")
    _need(".." not in path.parts, label + " must not contain parent traversal")
    return str(path)


def conservative_codex_argv(profile, layout, requested_model):
    """Build one fresh-role argv using the observed public interface.

    Reparse public probe evidence; do not trust advertised booleans alone.
    This is an argument contract, not proof that permissions or auth work.
    """
    _need(type(profile) is dict and profile.get("schema") == "victorique.harness.host_profile.v1"
          and profile.get("host") == "codex", "Codex public host profile required")
    binary = _absolute(profile.get("binary"), "profile.binary")
    _need(Path(binary).suffix.lower() not in {".cmd", ".bat", ".ps1"}, "native executable required")
    probes = profile.get("probes")
    _need(type(probes) is list and len(probes) == 3 and all(type(probe) is dict for probe in probes),
          "exact version/help/exec_help public probes required")
    _need({probe.get("name") for probe in probes} == {"version", "help", "exec_help"},
          "exact version/help/exec_help public probes required")
    for probe in probes:
        _need(type(probe.get("exit_code")) is int and probe["exit_code"] == 0,
              "all public probes must exit successfully")
        _need(type(probe.get("stdout")) is str and type(probe.get("stderr")) is str,
              "public probe output must be text")
        _need(probe.get("timed_out", False) is False and probe.get("output_truncated", False) is False
              and probe.get("error") is None, "public probe is incomplete")
    observed = parse_host_probe("codex", profile["binary"], probes)
    _need(observed["status"] == "public_interface_observed" and observed["identity"]["verified"] is True,
          "public Codex identity did not match")
    _need(profile.get("version") == observed["version"]
          and profile.get("version_banner") == observed["version_banner"], "Codex version differs from public probe")
    _need(profile.get("status") == observed["status"] and type(profile.get("identity")) is dict
          and profile["identity"].get("verified") is True, "profile declaration differs from public evidence")
    help_probe = next(probe for probe in probes if probe["name"] == "exec_help")
    help_text = help_probe["stdout"] + "\n" + help_probe["stderr"]
    for flag in REQUIRED_EXEC_FLAGS:
        if flag == "--model" and requested_model in (None, DEFAULT_MODEL):
            continue
        _need(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) is not None,
              "required exec flag not observed: " + flag)
    _need(type(layout) is dict, "layout must be an object")
    workspace = _absolute(layout.get("workspace"), "workspace")
    schema_file = _absolute(layout.get("schema_file"), "schema_file")
    final_file = _absolute(layout.get("final_file"), "final_file")
    _need(len({Path(workspace), Path(schema_file), Path(final_file), Path(binary)}) == 4,
          "workspace, schema, final output and binary must be distinct")
    _need(requested_model is None or (type(requested_model) is str and requested_model.strip() == requested_model and requested_model
          and not requested_model.startswith("-") and not any(ord(char) < 33 for char in requested_model)),
          "explicit model identifier required")
    argv = [binary, "exec", "--json", "--output-schema", schema_file,
            "--output-last-message", final_file, "--cd", workspace, "--sandbox", "read-only",
            "--config", 'approval_policy="never"', "--config", "agents.enabled=false",
            "--config", 'web_search="disabled"', "--config", "project_doc_max_bytes=0"]
    effort = profile.get("reasoning_effort")
    validate_reasoning_effort(effort)
    if effort is not None:
        argv.extend(["--config", "model_reasoning_effort=" + json.dumps(effort)])
    names = profile.get("disabled_mcp_servers", [])
    _need(type(names) is list and all(type(name) is str and name for name in names),
          "MCP server names must be nonempty strings")
    if names:
        # Config tables merge: {} would preserve inherited servers. Override each
        # server's enabled field, quoting names as TOML keys inside one table.
        entries = [json.dumps(name) + "={enabled=false}" for name in names]
        argv.extend(["--config", "mcp_servers={" + ",".join(entries) + "}"])
    for feature in DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    argv.extend(["--ephemeral", "--skip-git-repo-check", "--color", "never"])
    if requested_model not in (None, DEFAULT_MODEL):
        argv.extend(["--model", requested_model])
    argv.append("-")
    return argv


def _packet_contract(packet):
    _need(type(packet) is dict and type(packet.get("binding")) is dict, "packet binding required")
    contract = packet.get("response_contract")
    _need(type(contract) is dict and set(contract) == {"schema", "fields", "binding"}, "native response contract required")
    _need(type(contract["schema"]) is str and contract["schema"].strip() == contract["schema"]
          and contract["schema"], "response schema identifier required")
    family = packet.get("schema")
    if family == "victorique.adaptive_actor_packet.v2":
        _need(packet["binding"].get("schema") == "victorique.adaptive_binding.v2"
              and contract["schema"] == "victorique.adaptive_actor_response.v2"
              and contract["fields"] == ["schema", "binding", "output"], "native response family differs")
    elif family == "strong_controller.selector_packet.v1":
        _need(set(packet["binding"]) == {"version", "protocol_id", "session_id", "observation_id", "request_id",
              "packet_id", "policy_id", "native_cursor", "model_request"}, "selector binding fields differ")
        _need(contract["schema"] == "proposed.controller_menu_selection.v1"
              and contract["fields"] == ["schema", "binding", "menu_index"], "selector response family differs")
        _need(type(packet.get("menu")) is list and len(packet["menu"]) > 0, "original selector menu required")
    else:
        raise ValueError("unsupported packet family")
    _canonical(contract)
    _need(_canonical(contract["binding"]) == _canonical(packet["binding"]), "packet contract binding differs")
    return contract


def packet_scope(packet):
    """Return explicit transport scope without rewriting the original packet.

    Selector protocol is its original protocol_id string; native protocol is
    its original object. The transport authorization must use that exact value.
    """
    _packet_contract(packet)
    b = packet["binding"]
    selector = packet["schema"] == "strong_controller.selector_packet.v1"
    try:
        protocol = b["protocol_id"] if selector else b["protocol"]
        _need(type(protocol) is str if selector else type(protocol) is dict, "packet protocol type differs")
        return {"family": packet["schema"], "session_id": b["session_id"], "packet_id": b["packet_id"],
                "protocol": deepcopy(protocol), "model_id": b["model_request"]["model_id"],
                "output_schema_key": None if selector else b["action_kind"]}
    except (KeyError, TypeError) as exc:
        raise ValueError("packet transport scope missing") from exc


def _output_schema(schema):
    """Clone an inert schema and rebase local JSON-pointer refs into output."""
    _need(type(schema) is dict, "output_schema must be an object")
    _canonical(schema)
    value = deepcopy(schema)

    def visit(node):
        if type(node) is dict:
            for key, item in list(node.items()):
                # These introduce remote bases/meta-schemas or dynamic scope.
                # They are outside this deliberately local schema subset.
                _need(key not in {"$schema", "$id", "$dynamicRef", "$recursiveRef", "$anchor", "$dynamicAnchor"},
                      "external or dynamic schema resolution is unsupported: " + key)
                if key == "$ref":
                    _need(type(item) is str and (item == "#" or item.startswith("#/")),
                          "only local JSON-pointer schema references are allowed")
                    node[key] = "#/properties/output" + item[1:]
                else:
                    visit(item)
        elif type(node) is list:
            for item in node:
                visit(item)
    visit(value)
    return value


def _binding_schema(value):
    """Describe this frozen binding's shape; exact values remain checked locally."""
    if type(value) is dict:
        return {"type": "object", "properties": {key: _binding_schema(item) for key, item in value.items()},
                "required": list(value), "additionalProperties": False}
    if type(value) is list:
        if not value:
            return {"type": "array", "items": {"type": "string"}, "maxItems": 0}
        choices = []
        for item in value:
            schema = _binding_schema(item)
            if schema not in choices:
                choices.append(schema)
        return {"type": "array", "items": choices[0] if len(choices) == 1 else {"anyOf": choices}}
    if value is None:
        # Null-bearing native binding fields are nullable identifiers. Keep the
        # provider's documented nullable union; local comparison requires null.
        return {"type": ["string", "null"]}
    types = {str: "string", bool: "boolean", int: "integer", float: "number"}
    _need(type(value) in types, "unsupported native binding value")
    return {"type": types[type(value)]}


def build_response_schema(packet, output_schema=None):
    """Wrap the packet with closed provider-facing binding objects.

    This describes the frozen binding recursively without replacing the local
    exact typed-value check or domain validators. No URI is fetched.
    """
    contract = _packet_contract(packet)
    if packet["schema"] == "strong_controller.selector_packet.v1":
        _need(output_schema is None, "selector response schema comes only from the original menu")
        return {"type": "object", "properties": {
            "schema": {"type": "string", "enum": [contract["schema"]]},
            "binding": _binding_schema(contract["binding"]),
            "menu_index": {"type": "integer", "minimum": 0, "maximum": len(packet["menu"]) - 1}},
            "required": ["schema", "binding", "menu_index"], "additionalProperties": False}
    return {"type": "object", "properties": {
        "schema": {"type": "string", "enum": [contract["schema"]]},
        "binding": _binding_schema(contract["binding"]), "output": _output_schema(output_schema)},
        "required": ["schema", "binding", "output"], "additionalProperties": False}


def parse_codex_capture(stdout, stderr, final, process, packet):
    """Classify complete caller-owned byte captures; never repair a response.

    ``failed`` needs a complete official turn.failed in a unique host session
    and an observed local exit. Missing/invalid final output is then immaterial.
    A completed, locally exited turn with exact but invalid final-envelope bytes
    is a failed response. Missing, conflicting, truncated or tool-using evidence
    stays unknown.
    This function does not persist or claim ownership of the original bytes.
    """
    _need(type(stdout) is bytes and type(stderr) is bytes and (final is None or type(final) is bytes),
          "stdout, stderr and final must be raw bytes (or absent final)")
    result = {"schema": CONTRACT_VERSION, "status": "unknown", "terminal_observed": False,
              "host_reference": None, "source_line": None, "source_event_id": None,
              "parsed": None, "diagnostics": [], "usage": None, "actual_model": None}
    diagnostics = result["diagnostics"]
    if type(process) is not dict or any(type(process.get(key)) is not bool for key in
                                      ("local_terminal", "timed_out", "capture_truncated")):
        diagnostics.append("invalid_local_process_evidence")
        return result
    if process.get("exit_code") is not None and type(process["exit_code"]) is not int:
        diagnostics.append("invalid_exit_code")
        return result
    try:
        contract = _packet_contract(packet)
    except (ValueError, RecursionError):
        diagnostics.append("invalid_packet_contract")
        return result
    try:
        out_text = stdout.decode("utf-8", errors="strict")
        err_text = stderr.decode("utf-8", errors="strict")
    except UnicodeError:
        diagnostics.append("invalid_capture_utf8")
        return result
    # Strict pre-decoding also rejects lone surrogate escapes, duplicate keys,
    # and overflowed floats before returning any JSON diagnostic payload.
    rows = []
    try:
        for line_number, line in enumerate(out_text.split("\n"), 1):
            if line.strip():
                event = _loads(line)
                _need(type(event) is dict and type(event.get("type")) is str, "invalid event")
                rows.append((line_number, event))
    except (ValueError, RecursionError):
        diagnostics.append("invalid_event_stream")
        return result
    parsed = parse_event_stream("codex", out_text, exit_code=process.get("exit_code"),
                                raw_stderr=err_text, truncated=process["capture_truncated"],
                                require_structured=False)
    result["parsed"] = parsed
    if parsed.get("tolerated_host_notices"):
        result["tolerated_host_notices"] = parsed["tolerated_host_notices"]
    result["usage"] = parsed["usage"]
    models = [event.get("model") for _, event in rows if event.get("type") in {"thread.started", "turn.started", "turn.completed"}]
    models.extend(re.findall(r"(?m)^model:\s*([^\s]+)\s*$", err_text))
    models = list(dict.fromkeys(model for model in models if type(model) is str
                               and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model)))
    result["actual_model"] = models[0] if len(models) == 1 else None
    if result["actual_model"] is not None:
        sources = []
        if any(event.get("model") == result["actual_model"] for _, event in rows
               if event.get("type") in {"thread.started", "turn.started", "turn.completed"}):
            sources.append("codex_event.model")
        if result["actual_model"] in re.findall(r"(?m)^model:\s*([^\s]+)\s*$", err_text):
            sources.append("codex_stderr.model")
        result["actual_model_source"] = "+".join(sources)
    if result["usage"] is not None:
        result["usage_source"] = "codex_terminal.usage"
    diagnostics.extend(parsed["issues"])
    if not process["local_terminal"] or process.get("exit_code") is None:
        diagnostics.append("local_terminal_not_observed")
    if process["timed_out"]:
        diagnostics.append("local_timeout")
    terminals = [(line, event) for line, event in rows if event["type"] in {"turn.completed", "turn.failed"}]
    terminal = terminals[0][1] if len(terminals) == 1 else None
    session = parsed["session_id"]
    if session is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", session):
        diagnostics.append("host_thread_id_not_usable")
        session = None
    if session is not None:
        # Core host references are absolute logical paths. The prefix is our
        # namespace, not a claimed vendor filesystem path; the suffix is the
        # exact observed thread ID, also retained as parsed.session_id.
        result["host_reference"] = "/codex-thread/" + session
    if terminal is not None:
        result["source_line"] = terminals[0][0]
    # The documented Codex terminal events provide no stable event identifier.
    # Never replace it with an item ID, local UUID, PID, or generated checksum.
    stream_issues = set(parsed["issues"]) - {"process_exit_nonzero", "process_exit_unknown", "host_reported_failure", "missing_final_text"}
    complete_stream = not stream_issues and session is not None and terminal is not None
    local_exit = process["local_terminal"] and type(process.get("exit_code")) is int
    if complete_stream and local_exit and terminal["type"] == "turn.failed":
        # Explicit complete failure evidence can close a host attempt even if
        # the final-answer file never existed or the local process timed out
        # while shutting down. No successful envelope is manufactured.
        result.update(status="failed", terminal_observed=True)
        return result
    exact_final = False
    valid_envelope = False
    if final is None:
        diagnostics.append("final_output_missing")
    else:
        try:
            final_text = final.decode("utf-8", errors="strict")
            last_text = parsed["final_text"]
            exact_final = type(last_text) is str and last_text.encode("utf-8", errors="strict") == final
            envelope = _loads(final_text)
            _need(type(envelope) is dict and set(envelope) == set(contract["fields"]),
                  "packet-family envelope fields differ")
            _need(envelope["schema"] == contract["schema"] and type(envelope["schema"]) is str,
                  "native envelope schema differs")
            _need(_canonical(envelope["binding"]) == _canonical(packet["binding"]), "native envelope binding differs")
            if packet["schema"] == "strong_controller.selector_packet.v1":
                _need(type(envelope["menu_index"]) is int and 0 <= envelope["menu_index"] < len(packet["menu"]),
                      "selector index outside original menu")
            last_text = parsed["final_text"]
            _need(type(last_text) is str and last_text.encode("utf-8", errors="strict") == final,
                  "final bytes differ from last complete agent message")
            valid_envelope = True
        except (ValueError, UnicodeError, RecursionError):
            diagnostics.append("final_output_invalid_or_mismatched")
    if (complete_stream and local_exit and not process["timed_out"] and not process["capture_truncated"]
            and process["exit_code"] == 0 and terminal["type"] == "turn.completed" and exact_final
            and set(diagnostics) == {"final_output_invalid_or_mismatched"}):
        result.update(status="failed", terminal_observed=True)
        diagnostics.append("invalid_response")
    if (complete_stream and local_exit and not process["timed_out"] and not process["capture_truncated"]
            and process["exit_code"] == 0 and terminal["type"] == "turn.completed"
            and valid_envelope and exact_final and set(diagnostics) == {"host_reported_failure"}):
        # Codex can report a transient 429 as an error event, reconnect, and then
        # finish successfully. Only this exact observed message is recoverable;
        # keep the generic parser's original issues and all error bytes intact.
        errors = [(line, event) for line, event in rows if event["type"] == "error"]
        recovered = []
        for line, event in errors:
            message = event.get("message")
            match = None
            if type(message) is str:
                match = (re.fullmatch(r"Reconnecting\.\.\. ([1-9][0-9]{0,2})/([1-9][0-9]{0,2}) "
                                      r"\(exceeded retry limit, last status: 429 Too Many Requests\)", message)
                         # Transient websocket refusal observed 2026-11-14; the CLI
                         # reconnected and completed the same turn. Only this exact form qualifies.
                         or re.fullmatch(r"Reconnecting\.\.\. ([1-9][0-9]{0,2})/([1-9][0-9]{0,2}) "
                                         r"\(unexpected status 403 Forbidden: Unknown error, "
                                         r"url: wss://chatgpt\.com/backend-api/codex/responses, "
                                         r"cf-ray: [0-9a-f]{16}-[A-Z]{3}\)", message))
            if (set(event) - {"type", "message", "thread_id"} or match is None
                    or int(match[1]) > int(match[2]) or line >= terminals[0][0]):
                break
            recovered.append({"source_line": line, "message": message})
        if errors and len(recovered) == len(errors):
            diagnostics.remove("host_reported_failure")
            result["recovered_reconnect_errors"] = recovered
    if (complete_stream and local_exit and not process["timed_out"] and process["exit_code"] == 0
            and terminal["type"] == "turn.completed" and not diagnostics):
        result.update(status="completed", terminal_observed=True)
    result["diagnostics"] = list(dict.fromkeys(diagnostics))
    return result
