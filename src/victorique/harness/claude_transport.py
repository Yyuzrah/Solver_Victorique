"""Claude Code CLI implementation of the existing claim-bound bridge protocol."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4

from .adapters import _decode
from .claude_contract import build_response_schema, conservative_claude_argv, packet_scope, parse_claude_capture
from .codex_process import DurableCodexProcess
from .codex_transport import CodexCliTransport, _authorize as _codex_authorize, _clone, _json, _now
from .safety import HarnessError, checked_path, json_bytes, write_new


VERSION = "claude_cli_v1"
SCHEMA = "victorique.claude_transport.v1"
AUTHORIZATION = "victorique.claude_execution_authorization.v1"


def _authorize(config, packet=None):
    """Use the same session/protocol/model/start/deadline authorization fields."""
    from .codex_transport import AUTHORIZATION as CODEX_AUTHORIZATION
    auth = config.get("authorization")
    if type(auth) is not dict or auth.get("schema") != AUTHORIZATION:
        raise HarnessError("explicit scoped Claude execution authorization is required")
    _codex_authorize(config | {"authorization": auth | {"schema": CODEX_AUTHORIZATION}}, packet)
    return auth


def _init_session(path, limit):
    """The session ID of the stream's leading system init event, or None."""
    try:
        with checked_path(path).open("rb") as stream:
            data = stream.read(limit)
    except (OSError, HarnessError):
        return None
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            row = _decode(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return None
        if type(row) is not dict or row.get("type") != "system" or row.get("subtype") != "init":
            return None
        session = row.get("session_id")
        return session if type(session) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", session) else None
    return None


class ClaudeCliTransport(CodexCliTransport):
    """Reuse the bridge fence and durable local runner, with Claude decoding.

    start_once launches at most once per claim; observe_existing reads only
    local captures, including after a crash or deadline. No CLI resume calls.
    """

    ADAPTER_ID = "claude_cli"

    def __init__(self, root):
        self.root = checked_path(root)
        self.path = checked_path(self.root / "adapter.sqlite3")
        if not self.path.is_file():
            raise HarnessError("existing Claude transport state is required")
        with self._db() as db:
            row = db.execute("SELECT body FROM config WHERE id=1").fetchone()
        self.config = _decode(row[0]) if row else {}
        if (self.config.get("schema") != SCHEMA or self.config.get("version") != VERSION
                or self.config.get("mode") != self.MODE):
            raise HarnessError("Claude transport schema or mode differs")
        self.runner = DurableCodexProcess(self.root / "runner")
        configured_workspace = self.config.get("workspace_root")
        if configured_workspace is not None and self.runner.workspace_root != checked_path(configured_workspace):
            raise HarnessError("Claude temporary workspace differs from frozen configuration")

    @classmethod
    def create(cls, root, *, profile, authorization, output_schemas, timeout_seconds=120, max_capture_bytes=131072):
        config = {"schema": SCHEMA, "version": VERSION, "mode": cls.MODE,
                  "profile": _clone(profile), "authorization": _clone(authorization),
                  "output_schemas": _clone(output_schemas), "timeout_seconds": timeout_seconds,
                  "max_capture_bytes": max_capture_bytes}
        _authorize(config)
        target = checked_path(root)
        source = Path(__file__).absolute().parents[2]
        if target.is_relative_to(source) or target == Path.home() or target == Path(target.anchor):
            raise HarnessError("live role staging must be outside the source checkout and global home")
        conservative_claude_argv(profile, {"type": "object"}, authorization["requested_model"])
        workspace = checked_path(Path(tempfile.gettempdir()) / ("victorique-claude-" + uuid4().hex))
        if workspace.is_relative_to(Path.cwd()) or workspace.is_relative_to(source) or workspace.is_relative_to(target):
            raise HarnessError("Claude temporary directory must be outside the current workspace and run state")
        # Create each empty cwd only at dispatch, then discard it after capture.
        config["workspace_root"] = str(workspace)
        return cls._create(target, config)

    def describe(self):
        return super().describe() | {"version": VERSION}

    def _session_reference(self, capture):
        # Claude names its session in the first event; a stopped stream has no result event.
        stdout = ((capture.get("streams") or {}).get("stdout") or {}).get("path")
        session = None if stdout is None else _init_session(stdout, self.config["max_capture_bytes"])
        return None if session is None else "/claude-session/" + session

    def _register(self, request, binding, *, start=False):
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (request.job_id,)).fetchone()
            if row is not None:
                if row["binding"] != _json(binding) or row["packet"] != request.packet_bytes:
                    raise HarnessError("job already belongs to different exact packet/claim")
                return False
            if not start:
                return False
            if self.MODE == "host_recorded":
                auth = _authorize(self.config, request.packet)
                if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= auth["max_process_starts"]:
                    raise HarnessError("authorization process ceiling exhausted")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,1)",
                       (request.job_id, request.core_claim_id, _json(binding), request.packet_bytes))
            return True

    def _invocation(self, request, binding):
        layout = self.runner.layout(binding)
        scope = packet_scope(request.packet)
        key = scope["output_schema_key"]
        schema = build_response_schema(request.packet, None if key is None else self.config["output_schemas"][key])
        remaining = (datetime.fromisoformat(self.config["authorization"]["expires_at_utc"].replace("Z", "+00:00"))
                     - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise HarnessError("Claude execution authorization expired")
        return {"argv": conservative_claude_argv(self.config["profile"], schema, scope["model_id"]),
                "deadline_utc": self.config["authorization"]["expires_at_utc"],
                "cwd": layout["workspace"], "final_file": layout["final_file"],
                "timeout_seconds": min(self.config["timeout_seconds"], max(.05, remaining)),
                "max_capture_bytes": self.config["max_capture_bytes"], "schema_bytes": json_bytes(schema),
                "environment_policy": "claude_subscription_isolated"}

    def _observe(self, request, binding):
        capture = self.runner.observe_existing(binding)
        phase = "worker_finished" if capture and capture.get("worker_finished") else "pending"
        with self._db() as db:
            row = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?", (request.job_id, phase)).fetchone()
            if row is not None:
                return self._typed(row)
        streams = {}
        for name in ("stdout", "stderr"):
            record = capture.get("streams", {}).get(name) if capture else None
            path = checked_path(record["path"]) if phase == "worker_finished" and record and record.get("path") else None
            if path and path.is_file():
                if path.stat().st_size > self.config["max_capture_bytes"]:
                    raise HarnessError("capture exceeds frozen byte ceiling")
                streams[name] = path.read_bytes()
            else:
                streams[name] = b""
        process = {"local_terminal": bool(capture and capture.get("local_terminal_observed")),
                   "exit_code": capture.get("exit_code") if capture else None,
                   "timed_out": capture.get("timed_out", False) if capture else False,
                   "capture_truncated": bool(capture and (
                       capture.get("stdin_complete") is False or capture.get("capture_error") or capture.get("launch_error")
                       or any(capture.get("streams", {}).get(name, {}).get("complete") is not True for name in streams)))}
        parsed = parse_claude_capture(streams["stdout"], streams["stderr"], process, request.packet)
        response = parsed.pop("response_bytes")
        metadata = {"schema": "victorique.bridge_observation.v1", "observation_id": "claude_obs_" + uuid4().hex,
                    **{name: binding[name] for name in ("job_id", "core_claim_id", "packet_id")},
                    "adapter_id": self.ADAPTER_ID, "adapter_version": VERSION, "mode": self.MODE,
                    "status": parsed["status"], "terminal_observed": parsed["terminal_observed"],
                    "host_reference": parsed["host_reference"], "observed_at_utc": _now(),
                    "source_event_id": parsed["source_event_id"], "source_line": parsed["source_line"],
                    "model_observation": None, "charge_observation": None}
        carrier = json_bytes({"schema": "victorique.claude_carrier.v1", "binding": binding,
                              "process_capture": capture, "parser": parsed,
                              "streams_base64": {key: base64.b64encode(data).decode("ascii") for key, data in streams.items()}})
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                       (request.job_id, phase, _json(metadata), response, carrier))
            row = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?", (request.job_id, phase)).fetchone()
            return self._typed(row)


class ClaudeFixtureTransport(ClaudeCliTransport):
    """Fixed offline executable using the same persistence, environment and parser."""

    MODE = "synthetic"
    ADAPTER_ID = "claude_cli_inert_fixture"

    @classmethod
    def create(cls, root, *, response_bytes, scenario="success", output_schemas=None,
               timeout_seconds=2, max_capture_bytes=131072, delay_seconds=0):
        if type(response_bytes) is not bytes:
            raise HarnessError("explicit response byte fixture required")
        if scenario not in {"success", "failure", "malformed", "tool", "oversize", "timeout", "mismatch"}:
            raise HarnessError("unknown Claude fixture scenario")
        if type(delay_seconds) not in (int, float) or not 0 <= delay_seconds <= 10:
            raise HarnessError("fixture delay must be in [0,10]")
        config = {"schema": SCHEMA, "version": VERSION, "mode": cls.MODE, "profile": None, "authorization": None,
                  "output_schemas": output_schemas or {"propose_plan": {"type": "object"}},
                  "timeout_seconds": timeout_seconds, "max_capture_bytes": max_capture_bytes,
                  "scenario": scenario, "delay_seconds": delay_seconds}
        result = cls._create(root, config)
        write_new(result.root / "fixture-response.bin", response_bytes)
        return result

    def _invocation(self, request, binding):
        layout = self.runner.layout(binding)
        return {"argv": [sys.executable, "-I", "-B", str(Path(__file__).with_name("claude_fake_host.py")),
                         self.config["scenario"], str(self.root / "fixture-response.bin"), str(self.config["delay_seconds"])],
                "cwd": layout["workspace"], "final_file": layout["final_file"],
                "timeout_seconds": self.config["timeout_seconds"], "max_capture_bytes": self.config["max_capture_bytes"],
                "environment_policy": "claude_subscription_isolated"}
