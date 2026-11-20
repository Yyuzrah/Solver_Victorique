"""Capability-checked Claude subscription CLI contract; no dispatch or login IO."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .codex_contract import (
    DEFAULT_MODEL, _absolute, _canonical, _loads, _need, _packet_contract,
    build_response_schema, packet_scope,
)
from .hosts import parse_host_probe


REQUIRED_FLAGS = (
    "--print", "--output-format", "--verbose", "--json-schema", "--max-turns",
    "--safe-mode", "--setting-sources", "--settings", "--tools",
    "--disallowedTools", "--strict-mcp-config", "--mcp-config",
    "--no-session-persistence", "--disable-slash-commands", "--no-chrome",
)
ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_CRON": "1",
    "CLAUDE_CODE_DISABLE_FILE_MENTIONS": "1",
    "CLAUDE_CODE_AUTO_CONNECT_IDE": "0",
}
_ARGUMENT_PROBES = {
    "max_turns_invalid": ["--max-turns", "victorique-invalid-number", "--help"],
    "max_turns_valid": ["--max-turns", "1", "--help"],
    "unknown_control": ["--victorique-nonexistent-option", "victorique-invalid-number", "--help"],
}


def _observed_flag(profile, flag):
    text = "\n".join(p.get("stdout", "") + "\n" + p.get("stderr", "")
                     for p in profile.get("probes", []) if p.get("name") == "help")
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text) is not None


def complete_claude_profile(profile):
    """Check a hidden, documented numeric flag using help-only parser probes.

    --version and unknown-option --help can bypass argument validation. The
    positive evidence is the CLI's specific invalid-number diagnostic for the
    documented --max-turns option, paired with valid-value help and a control.
    """
    if _observed_flag(profile, "--max-turns"):
        return profile
    binary = _absolute(profile.get("binary"), "profile.binary")
    probes = []
    with tempfile.TemporaryDirectory(prefix="victorique-claude-help-") as cwd:
        for name, args in _ARGUMENT_PROBES.items():
            row = {"name": name, "argv": [binary, *args], "host_version": profile.get("version"), "exit_code": None,
                   "stdout": "", "stderr": "", "timed_out": False, "output_truncated": False, "error": None}
            try:
                observed = subprocess.run(row["argv"], cwd=cwd, stdin=subprocess.DEVNULL,
                                          capture_output=True, timeout=10, shell=False,
                                          env=claude_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                row.update(exit_code=observed.returncode, stdout=observed.stdout[:262144].decode("utf-8", errors="replace"),
                           stderr=observed.stderr[:262144].decode("utf-8", errors="replace"),
                           output_truncated=max(len(observed.stdout), len(observed.stderr)) > 262144)
            except subprocess.TimeoutExpired:
                row.update(timed_out=True, error="public_argument_probe_timeout")
            except OSError:
                row["error"] = "public_argument_probe_unavailable"
            probes.append(row)
    return profile | {"argument_probes": probes}


def _max_turns_validated(profile):
    probes = profile.get("argument_probes")
    if type(probes) is not list or len(probes) != 3 or any(type(p) is not dict for p in probes):
        return False
    if {p.get("name") for p in probes} != set(_ARGUMENT_PROBES):
        return False
    for p in probes:
        if (p.get("argv") != [profile["binary"], *_ARGUMENT_PROBES[p["name"]]]
                or p.get("host_version") != profile.get("version")
                or type(p.get("exit_code")) is not int or p.get("timed_out") is not False
                or p.get("output_truncated") is not False or p.get("error") is not None
                or type(p.get("stdout")) is not str or type(p.get("stderr")) is not str):
            return False
    by_name = {p["name"]: p for p in probes}
    invalid, valid, control = (by_name[k] for k in _ARGUMENT_PROBES)
    pattern = r"option ['\"]--max-turns <turns>['\"] argument ['\"]victorique-invalid-number['\"] is invalid\. must be a number"
    return (invalid["exit_code"] != 0 and not invalid["stdout"].strip()
            and re.search(pattern, invalid["stderr"]) is not None
            and valid["exit_code"] == 0 and "Claude Code" in valid["stdout"]
            and control["exit_code"] == 0 and "Claude Code" in control["stdout"]
            and re.search(pattern, control["stderr"]) is None)


def claude_environment(source=None):
    """Pass OS/login locations, never API/provider overrides or agent settings.

    Values are supplied to the child only, never logged or persisted. The CLI
    reads its own subscription login; this function opens no credential files.
    """
    source = os.environ if source is None else source
    allowed = {
        "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
        "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
        "USER", "LOGNAME", "SHELL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
        "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH",
    }
    result = {key: value for key, value in source.items() if key.upper() in allowed}
    result.update(ISOLATION_ENV)
    return result


def conservative_claude_argv(profile, response_schema, requested_model=DEFAULT_MODEL):
    """Require every isolation flag in successful public version/help probes."""
    _need(type(profile) is dict and profile.get("schema") == "victorique.harness.host_profile.v1"
          and profile.get("host") == "claude-code", "Claude Code public host profile required")
    binary = _absolute(profile.get("binary"), "profile.binary")
    _need(Path(binary).suffix.lower() not in {".cmd", ".bat", ".ps1"}, "native executable required")
    probes = profile.get("probes")
    _need(type(probes) is list and len(probes) == 2 and all(type(p) is dict for p in probes),
          "exact Claude version/help probes required")
    _need({p.get("name") for p in probes} == {"version", "help"}, "exact Claude version/help probes required")
    for probe in probes:
        _need(type(probe.get("exit_code")) is int and probe["exit_code"] == 0
              and probe.get("timed_out", False) is False and probe.get("output_truncated", False) is False
              and probe.get("error") is None and type(probe.get("stdout")) is str
              and type(probe.get("stderr")) is str, "incomplete Claude public probe")
    observed = parse_host_probe("claude-code", binary, probes)
    _need(observed["identity"]["verified"] and profile.get("version") == observed["version"]
          and profile.get("version_banner") == observed["version_banner"]
          and profile.get("status") == observed["status"], "Claude identity/version differs from public probes")
    help_probe = next(p for p in probes if p["name"] == "help")
    help_text = help_probe["stdout"] + "\n" + help_probe["stderr"]
    flags = REQUIRED_FLAGS + (("--model",) if requested_model not in (None, DEFAULT_MODEL) else ())
    for flag in flags:
        _need(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text)
              or (flag == "--max-turns" and _max_turns_validated(profile)),
              "required Claude flag not observed: " + flag)
    _need(requested_model is None or (type(requested_model) is str and requested_model
          and not requested_model.startswith("-") and not any(ord(c) < 33 for c in requested_model)),
          "invalid requested model")
    _need(type(response_schema) is dict, "response schema must be an object")
    argv = [binary, "--print", "--output-format", "stream-json", "--verbose",
            "--json-schema", _canonical(response_schema), "--max-turns", "1",
            "--safe-mode", "--setting-sources", "", "--settings",
            '{"disableAllHooks":true,"autoMemoryEnabled":false}',
            "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence",
            "--disable-slash-commands", "--no-chrome"]
    if requested_model not in (None, DEFAULT_MODEL):
        argv.extend(["--model", requested_model])
    return argv


def _raw_field(line, name):
    """Extract a JSON member's original UTF-8 bytes after strict JSON validation."""
    decoder = json.JSONDecoder()
    pos = line.index("{") + 1
    while True:
        while line[pos].isspace() or line[pos] == ",":
            pos += 1
        if line[pos] == "}":
            return None
        key, pos = decoder.raw_decode(line, pos)
        while line[pos].isspace() or line[pos] == ":":
            pos += 1
        start = pos
        _, pos = decoder.raw_decode(line, pos)
        if key == name:
            return line[start:pos].encode("utf-8")


def parse_claude_capture(stdout, stderr, process, packet):
    """Conservatively decode stream-json, preserving structured_output bytes."""
    result = {"status": "unknown", "terminal_observed": False, "host_reference": None,
              "source_line": None, "source_event_id": None, "usage": None,
              "actual_model": None, "diagnostics": [], "response_bytes": None}
    completed_response = False
    try:
        _need(type(stdout) is bytes and type(stderr) is bytes, "raw_bytes_required")
        stderr.decode("utf-8", errors="strict")
        contract = _packet_contract(packet)
        _need(type(process) is dict and all(type(process.get(k)) is bool for k in
              ("local_terminal", "timed_out", "capture_truncated")), "invalid_process_evidence")
        rows = [(n, line, _loads(line)) for n, line in enumerate(stdout.decode("utf-8").splitlines(), 1) if line.strip()]
        _need(rows and all(type(row) is dict for _, _, row in rows), "invalid_event_stream")
        init = [row for _, _, row in rows if row.get("type") == "system" and row.get("subtype") == "init"]
        terminals = [(n, line, row) for n, line, row in rows if row.get("type") == "result"]
        _need(len(init) == 1 and len(terminals) == 1 and rows[-1][2].get("type") == "result",
              "missing_or_conflicting_terminal")
        first = init[0]
        number, line, terminal = terminals[0]
        session = first.get("session_id")
        _need(type(session) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", session), "invalid_session")
        _need(all(row.get("session_id", session) == session for _, _, row in rows), "conflicting_session")
        _need(type(first.get("tools")) is list and set(first["tools"]) <= {"StructuredOutput"}
              and first.get("mcp_servers", []) == [], "unexpected_tools_or_mcp")
        structured_calls = set()
        for _, _, row in rows:
            _need(row.get("type") in {"system", "assistant", "user", "result", "rate_limit_event"}, "unexpected_event")
            if row.get("type") == "system":
                _need(row.get("subtype") == "init", "unexpected_system_event")
            _need(row.get("parent_tool_use_id") is None, "unexpected_subagent")
            for block in row.get("message", {}).get("content", []):
                _need(type(block) is dict, "invalid_content_block")
                if block.get("type") == "tool_use":
                    _need(block.get("name") == "StructuredOutput" and type(block.get("id")) is str
                          and block["id"] not in structured_calls, "unexpected_tool_use")
                    structured_calls.add(block["id"])
                elif block.get("type") == "tool_result":
                    _need(block.get("tool_use_id") in structured_calls, "unexpected_tool_result")
                    structured_calls.remove(block["tool_use_id"])
                else:
                    _need(block.get("type") in {"text", "thinking", "redacted_thinking"}, "unexpected_content_block")
        _need(not structured_calls, "incomplete_structured_output_tool")
        result.update(host_reference="/claude-session/" + session, source_line=number)
        model = first.get("model")
        if type(model) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model):
            result["actual_model"] = model
            result["actual_model_source"] = "claude_system_init.model"
        usage = terminal.get("usage")
        if type(usage) is dict:
            keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                    "reasoning_output_tokens")
            result["usage"] = {k: usage.get(k) if type(usage.get(k)) is int and usage[k] >= 0 else None for k in keys}
            details = usage.get("output_tokens_details")
            thinking = details.get("thinking_tokens") if type(details) is dict else None
            if result["usage"]["reasoning_output_tokens"] is None and type(thinking) is int and thinking >= 0:
                result["usage"]["reasoning_output_tokens"] = thinking
            result["usage_source"] = "claude_result.usage"
        _need(process["local_terminal"] and type(process.get("exit_code")) is int
              and not process["timed_out"] and not process["capture_truncated"], "incomplete_local_capture")
        if terminal.get("is_error") is True:
            result.update(status="failed", terminal_observed=True)
            return result
        _need(process["exit_code"] == 0 and terminal.get("is_error") is False
              and terminal.get("subtype") == "success", "unsuccessful_result")
        envelope = terminal.get("structured_output")
        if "structured_output" in terminal:
            result["response_bytes"] = _raw_field(line, "structured_output")
            completed_response = True
        _need(type(envelope) is dict and set(envelope) == set(contract["fields"]), "structured_output_missing_or_invalid")
        _need(envelope["schema"] == contract["schema"] and _canonical(envelope["binding"]) == _canonical(packet["binding"]),
              "response_binding_mismatch")
        if packet["schema"] == "strong_controller.selector_packet.v1":
            _need(type(envelope["menu_index"]) is int and 0 <= envelope["menu_index"] < len(packet["menu"]), "invalid_menu_index")
        result.update(status="completed", terminal_observed=True, response_bytes=_raw_field(line, "structured_output"))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, AttributeError, IndexError) as error:
        result["diagnostics"].append(str(error)[:300])
        if completed_response:
            result.update(status="failed", terminal_observed=True)
            result["diagnostics"].append("invalid_response")
    return result
