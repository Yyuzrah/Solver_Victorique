"""Public host-interface probes. This module never sends an inference prompt.

Public banners are evidence of the interface presented by a local binary, not
cryptographic provenance or a successful model/isolation acceptance test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SUPPORTED_HOSTS = ("codex", "claude-code", "cursor")
KNOWN_HOSTS = SUPPORTED_HOSTS + ("hermes", "dsh")
_COMMANDS = {"codex": "codex", "claude-code": "claude", "cursor": "cursor-agent"}
_PROBE_ARGS = {"version": ("--version",), "help": ("--help",),
               "exec_help": ("exec", "--help")}
_FLAGS = {
    "codex": ("--json", "--output-schema", "--output-last-message", "--sandbox",
              "--ignore-user-config", "--ignore-rules", "--ephemeral"),
    "claude-code": ("--print", "--output-format", "--json-schema", "--max-turns",
                    "--restricted", "--bare", "--tools", "--disallowedTools"),
    "cursor": ("--print", "--output-format", "--workspace", "--sandbox", "--resume"),
}
_SOURCES = {
    "codex": ["https://learn.chatgpt.com/docs/non-interactive-mode",
              "https://learn.chatgpt.com/docs/build-skills"],
    "claude-code": ["https://code.claude.com/docs/en/cli-reference",
                    "https://code.claude.com/docs/en/skills"],
    "cursor": ["https://cursor.com/docs/cli/reference/parameters",
               "https://cursor.com/docs/skills"],
}
MAX_PROBE_OUTPUT_CHARS = 262144


def _host(host: str) -> str:
    if host not in KNOWN_HOSTS:
        raise ValueError(f"unknown host {host!r}; choose one of {KNOWN_HOSTS}")
    return host


def _base(host: str, binary: str | None) -> dict:
    return {
        "schema": "victorique.harness.host_profile.v1",
        "host": host, "binary": binary, "version": None, "version_banner": None,
        "status": "not_probed",
        "identity": {"verified": False, "method": "public_banner_only",
                     "reason": "no successful public probe"},
        "capabilities": {}, "probes": [],
        "skill_discovery_verified": False, "live_execution_verified": False,
        "isolation_verified": False, "authentication_checked": False,
        "model_calls": 0, "requested_model": None, "actual_model": None,
        "transport_enabled": False,
        "read_only_remote_recovery": "unsupported_by_this_harness",
        "official_sources": list(_SOURCES.get(host, [])),
    }


def _identity(host: str, version_text: str, help_text: str) -> tuple[str | None, str | None]:
    # Deliberately require product branding; a filename/path is insufficient.
    if host == "codex":
        version_re = r"^codex-cli\s+([0-9][\w.+-]*)\s*$"
        help_re = r"(?im)^\s*(?:Codex CLI|Usage:\s*codex(?:\s|$))"
    elif host == "claude-code":
        version_re = r"^([0-9][\w.+-]*)\s+\(Claude Code\)\s*$"
        help_re = r"(?i)\bClaude Code\b"
    else:
        version_re = r"^(?:Cursor(?: Agent| CLI)?\s+)?([0-9][\w.+-]*)\s*$"
        help_re = r"(?im)^\s*(?:Cursor Agent|Cursor CLI)(?:\s|$)"
    if not re.search(help_re, help_text):
        return None, None
    for line in version_text.splitlines():
        match = re.fullmatch(version_re, line.strip(), re.IGNORECASE)
        if match:
            return match[1], line.strip()
    return None, None


def parse_host_probe(host: str, binary: str | None, probes: list[dict]) -> dict:
    """Parse captured *public* probes; caller must label simulated fixtures.

    Required probe keys: name, argv, exit_code, stdout, stderr, timed_out,
    error. Capabilities are advertised flags, never verified execution.
    """
    _host(host)
    result = _base(host, str(binary) if binary is not None else None)
    if host not in SUPPORTED_HOSTS:
        result.update(status="identity_unresolved")
        result["identity"]["reason"] = "product identity and official interface must be supplied"
        return result
    # Copy JSON-compatible probe objects, without mutating the caller's capture.
    result["probes"] = [dict(probe, argv=list(probe.get("argv", []))) for probe in probes]
    successful = {}
    seen = set()
    for probe in probes:
        name = probe.get("name")
        argv = probe.get("argv", [])
        expected = [str(binary), *_PROBE_ARGS.get(name, ())]
        if name not in _PROBE_ARGS or argv != expected:
            raise ValueError("probe argv is not an allowed public help/version invocation")
        if name in seen:
            raise ValueError("duplicate public probe")
        seen.add(name)
        if probe.get("exit_code") == 0 and not probe.get("timed_out") and not probe.get("error"):
            if probe.get("output_truncated"):
                continue
            successful[name] = str(probe.get("stdout", "")) + "\n" + str(probe.get("stderr", ""))
    if "version" not in successful or "help" not in successful:
        result["status"] = "probe_failed"
        result["identity"]["reason"] = "version and top-level help must both finish successfully"
        return result
    version, banner = _identity(host, successful["version"], successful["help"])
    if version is None:
        result["status"] = "identity_unverified"
        result["identity"]["reason"] = "public output lacks matching product branding and version"
        return result
    result.update(status="public_interface_observed", version=version, version_banner=banner)
    result["identity"] = {
        "verified": True, "method": "public_banner_only",
        "reason": "product-branded version and help match; binary provenance is not authenticated",
    }
    text = "\n".join(successful.values())
    result["capabilities"] = {
        flag: {"advertised": bool(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text)),
               "execution_verified": False}
        for flag in _FLAGS[host]
    }
    return result


def inspect_host(host: str, binary: str | Path | None = None, *, timeout: float = 10.0) -> dict:
    """Inspect a supplied absolute executable or one host-specific PATH command.

    No shell, inference prompt, login, config/history scan, or model listing is
    used. Cursor's ambiguous ``agent`` name is never discovered automatically.
    An explicit path is executable code chosen by the caller, not a trust check.
    """
    _host(host)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        raise ValueError("probe timeout must be in (0, 30] seconds")
    if host not in SUPPORTED_HOSTS:
        return parse_host_probe(host, None, [])
    selected = str(binary) if binary is not None else shutil.which(_COMMANDS[host])
    if not selected:
        result = _base(host, None)
        result["status"] = "not_found_on_path"
        result["identity"]["reason"] = "host-specific PATH command was not found; this is not proof of absence"
        result["discovery_command"] = _COMMANDS[host]
        if host == "cursor":
            result["next_step"] = "supply a confirmed absolute Cursor CLI path; generic agent is not auto-probed"
        return result
    path = Path(selected)
    if not path.is_absolute():
        raise ValueError("binary must be an absolute path")
    if not path.is_file():
        raise ValueError("binary must name an existing file")
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("shell wrapper binaries are not supported by this probe; supply the native executable")
    selected = str(path.absolute())
    probes = []
    names = ("version", "help", "exec_help") if host == "codex" else ("version", "help")
    # Use an empty working directory so help cannot accidentally use this repo
    # as its task workspace. We do not read or copy user authentication files.
    with tempfile.TemporaryDirectory(prefix="victorique-public-probe-") as cwd:
        for name in names:
            argv = [selected, *_PROBE_ARGS[name]]
            probe = {"name": name, "argv": argv, "exit_code": None,
                     "stdout": "", "stderr": "", "timed_out": False,
                     "error": None, "output_truncated": False}
            try:
                proc = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=timeout, shell=False,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                probe.update(exit_code=proc.returncode,
                             stdout=proc.stdout[:MAX_PROBE_OUTPUT_CHARS],
                             stderr=proc.stderr[:MAX_PROBE_OUTPUT_CHARS],
                             output_truncated=(len(proc.stdout) > MAX_PROBE_OUTPUT_CHARS or
                                               len(proc.stderr) > MAX_PROBE_OUTPUT_CHARS))
            except subprocess.TimeoutExpired:
                probe.update(timed_out=True, error="public_probe_timeout")
            except OSError as error:
                # Error class/errno suffice; don't copy environment or diagnostics
                # that could come from authentication settings into this profile.
                probe["error"] = f"{type(error).__name__}:{error.errno}"
            probes.append(probe)
    result = parse_host_probe(host, selected, probes)
    result["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result
