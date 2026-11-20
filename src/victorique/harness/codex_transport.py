"""Claim-bound Codex CLI bridge transport, with disk-only recovery.

The actual CLI is used only by CodexCliTransport with an explicit bounded
authorization. This implementation's acceptance uses CodexFixtureTransport,
which can launch only the bundled inert executable. Core admission is lazy.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from .adapters import _decode
from .codex_contract import CONTRACT_VERSION
from .safety import HarnessError, checked_path, json_bytes, write_new


VERSION = "codex_cli_v3"
SCHEMA = "victorique.codex_transport.v1"
AUTHORIZATION = "victorique.codex_execution_authorization.v1"
REDECODE_PHASE = "worker_finished:" + CONTRACT_VERSION


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _clone(value):
    return _decode(_json(value))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _authorize(config, packet=None, *, check_expiry=True):
    auth = config.get("authorization")
    required = {"schema", "authorization_id", "source_ref", "allow_execution", "session_id", "protocol",
                "binary", "host_version", "requested_model", "max_process_starts", "expires_at_utc",
                "permission_review_ref", "authentication_precondition"}
    if not isinstance(auth, dict) or set(auth) != required or auth.get("schema") != AUTHORIZATION or auth.get("allow_execution") is not True:
        raise HarnessError("explicit scoped Codex execution authorization is required")
    for name in ("authorization_id", "source_ref", "session_id", "permission_review_ref"):
        if not isinstance(auth[name], str) or not auth[name].strip() or len(auth[name]) > 4000:
            raise HarnessError("missing authorization scope: " + name)
    if auth["authentication_precondition"] != "operator_completed_host_managed_login":
        raise HarnessError("host-managed authentication precondition is not declared")
    if type(auth["max_process_starts"]) is not int or not 1 <= auth["max_process_starts"] <= 100:
        raise HarnessError("bounded process-start ceiling is required; this grants no core budget")
    profile = config["profile"]
    if auth["binary"] != profile["binary"] or auth["host_version"] != profile["version"]:
        raise HarnessError("authorization does not match the exact selected public host profile")
    if not isinstance(auth["requested_model"], str) or not auth["requested_model"].strip():
        raise HarnessError("an explicit requested model is required before live dispatch")
    try:
        expiry = datetime.fromisoformat(auth["expires_at_utc"].replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HarnessError("authorization expiry must be UTC") from exc
    if expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
        raise HarnessError("authorization expiry must be UTC")
    if check_expiry and expiry <= datetime.now(timezone.utc):
        raise HarnessError("Codex execution authorization expired; observe existing only")
    if packet is not None:
        from .codex_contract import packet_scope
        scope = packet_scope(packet)
        if (scope["session_id"] != auth["session_id"] or scope["protocol"] != auth["protocol"]
                or scope["model_id"] != auth["requested_model"]):
            raise HarnessError("core packet is outside the exact authorized session/protocol/model")
    return auth


class CodexCliTransport:
    """Adapter for AdaptiveHarnessBridge.Transport; no automatic CLI registration.

    start_once returns a local snapshot and may immediately return unknown.
    observe_existing reads only this adapter's runner artifacts. It does not
    call Codex resume, poll a remote host, or inspect historical host logs.
    """

    MODE = "host_recorded"
    ADAPTER_ID = "codex_cli"
    # The run's stop request file, set by the loop that uses this transport. Each
    # call's worker process ends the call when the file appears.
    stop_file = None

    def __init__(self, root):
        from .codex_process import DurableCodexProcess
        self.root = checked_path(root)
        self.path = checked_path(self.root / "adapter.sqlite3")
        if not self.path.is_file():
            raise HarnessError("existing Codex transport state is required")
        with self._db() as db:
            row = db.execute("SELECT body FROM config WHERE id=1").fetchone()
        if row is None:
            raise HarnessError("incomplete Codex transport state")
        self.config = _decode(row[0])
        if (self.config.get("schema") != SCHEMA or self.config.get("mode") != self.MODE
                or self.config.get("version") != VERSION):
            raise HarnessError("transport schema or mode differs")
        self.runner = DurableCodexProcess(self.root / "runner")

    @classmethod
    def create(cls, root, *, profile, authorization, output_schemas, timeout_seconds=120,
               max_capture_bytes=131072, workspace_root=None):
        from .codex_contract import conservative_codex_argv
        config = {"schema": SCHEMA, "version": VERSION, "mode": cls.MODE,
                  "profile": _clone(profile), "authorization": _clone(authorization),
                  "output_schemas": _clone(output_schemas), "timeout_seconds": timeout_seconds,
                  "max_capture_bytes": max_capture_bytes}
        if workspace_root is not None:
            config["workspace_root"] = str(checked_path(workspace_root))
        _authorize(config)
        target = checked_path(root)
        # A fresh directory inside a source checkout does not isolate its ancestor
        # instructions. Live callers must select a separate staging area.
        package = Path(__file__).absolute().parent
        source = package.parents[1] if package.name == "harness" else package.parent
        if target.is_relative_to(source) or target == Path.home() or target == Path(target.anchor):
            raise HarnessError("live role staging must be outside the source checkout and global home")
        layout = {"workspace": target / "example-workspace", "schema_file": target / "example-schema.json",
                  "final_file": target / "example-final.json"}
        conservative_codex_argv(profile, layout, authorization["requested_model"])
        return cls._create(target, config)

    @classmethod
    def _create(cls, root, config):
        from .codex_process import DurableCodexProcess
        target = checked_path(root)
        if target.exists():
            raise HarnessError("transport root must be a new directory")
        if not isinstance(config["output_schemas"], dict) or not config["output_schemas"]:
            raise HarnessError("explicit actor output schemas are required")
        for schema in config["output_schemas"].values():
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise HarnessError("each actor output schema must describe an object")
        from ..settings import check_seconds
        try:
            check_seconds(config["timeout_seconds"], "timeout_seconds")
        except ValueError as error:
            raise HarnessError(str(error)) from error
        if type(config["max_capture_bytes"]) is not int or not 1024 <= config["max_capture_bytes"] <= 32 * 1024 * 1024:
            raise HarnessError("capture byte ceiling is out of range")
        target.mkdir(parents=True, exist_ok=False)
        with closing(sqlite3.connect(target / "adapter.sqlite3")) as db:
            db.executescript("""
                CREATE TABLE config(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE jobs(job_id TEXT PRIMARY KEY, claim_id TEXT UNIQUE NOT NULL,
                    binding TEXT NOT NULL, packet BLOB NOT NULL, start_permitted INTEGER NOT NULL);
                CREATE TABLE observations(job_id TEXT NOT NULL, phase TEXT NOT NULL,
                    metadata TEXT NOT NULL, response BLOB, carrier BLOB NOT NULL,
                    PRIMARY KEY(job_id,phase));
            """)
            db.execute("INSERT INTO config VALUES(1,?)", (_json(config),))
            db.commit()
        DurableCodexProcess.create(target / "runner", workspace_root=config.get("workspace_root"))
        return cls(target)

    @contextmanager
    def _db(self, write=False):
        for suffix in ("", "-journal", "-wal", "-shm"):
            checked_path(str(self.path) + suffix, transient_sqlite_sidecar=bool(suffix))
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def describe(self):
        return {"adapter_id": self.ADAPTER_ID, "version": VERSION, "mode": self.MODE,
                "can_start": True, "can_observe_existing": True, "model_execution": self.MODE == "host_recorded",
                "fresh_context_required": True, "raw_response_preserved": True}

    def _binding(self, request):
        if request.mode != self.MODE or type(request.packet_bytes) is not bytes:
            raise HarnessError("request mode or original packet bytes differ")
        try:
            decoded = _decode(request.packet_bytes.decode("utf-8"))
            if _json(decoded) != _json(request.packet):
                raise HarnessError("original packet bytes do not match the packet object")
            from .codex_contract import packet_scope
            scope = packet_scope(request.packet)
            key = scope["output_schema_key"]
            if key is not None and key not in self.config["output_schemas"]:
                raise HarnessError("no frozen output schema for this action")
            result = {"job_id": request.job_id, "core_claim_id": request.core_claim_id,
                      "packet_id": scope["packet_id"], "mode": request.mode,
                      "session_id": scope["session_id"], "protocol": scope["protocol"]}
            for name in ("job_id", "core_claim_id", "packet_id", "session_id"):
                value = result[name]
                if not isinstance(value, str) or not value or len(value) > 120 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in value):
                    raise HarnessError("invalid claim-bound identifier")
            return result
        except (KeyError, TypeError, UnicodeError) as exc:
            raise HarnessError("malformed frozen core packet") from exc

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
                count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
                if count >= auth["max_process_starts"]:
                    raise HarnessError("authorization process ceiling exhausted; no core budget is granted")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,1)", (request.job_id, request.core_claim_id,
                       _json(binding), request.packet_bytes))
            return True

    def _invocation(self, request, binding):
        from .codex_contract import build_response_schema, conservative_codex_argv, packet_scope
        layout = self.runner.layout(binding)
        scope = packet_scope(request.packet)
        key = scope["output_schema_key"]
        output_schema = None if key is None else self.config["output_schemas"][key]
        schema = build_response_schema(request.packet, output_schema)
        model = scope["model_id"]
        remaining = (datetime.fromisoformat(self.config["authorization"]["expires_at_utc"].replace("Z", "+00:00"))
                     - datetime.now(timezone.utc)).total_seconds()
        return {"argv": conservative_codex_argv(self.config["profile"], layout, model),
                "deadline_utc": self.config["authorization"]["expires_at_utc"],
                "cwd": str(layout["workspace"]), "final_file": str(layout["final_file"]),
                "timeout_seconds": min(self.config["timeout_seconds"], max(.05, remaining)), "max_capture_bytes": self.config["max_capture_bytes"],
                "schema_bytes": json_bytes(schema)}

    def start_once(self, request):
        binding = self._binding(request)
        # Adapter fence is an additional no-retry responsibility record. The
        # bridge's fresh core claim is still the sole domain dispatch authority.
        fresh = self._register(request, binding, start=True)
        if fresh:
            invocation = self._invocation(request, binding)
            if self.stop_file is not None:
                invocation = {**invocation, "stop_file": str(self.stop_file)}
            self.runner.start_once(binding, invocation, request.packet_bytes)
        return self._observe(request, binding)

    def observe_existing(self, request):
        binding = self._binding(request)
        self._register(request, binding)
        with self._db() as db:
            row = db.execute("SELECT 1 FROM jobs WHERE job_id=?", (request.job_id,)).fetchone()
        if row is None:
            return None
        return self._observe(request, binding)

    def redecode_existing(self, request):
        """Explicitly recover an archived transient-429 success without dispatch.

        Preserve the original worker_finished row. A successful reinterpretation
        adds one decoder-version row; ordinary observation can subsequently use
        it, including grade workflows without a core bridge. Rejections leave
        the archive unchanged. Native callers may pass the returned observation
        to their existing bridge.receive(request_id, observation).
        """
        binding = self._binding(request)
        self._register(request, binding)
        with self._db() as db:
            if db.execute("SELECT 1 FROM jobs WHERE job_id=?", (request.job_id,)).fetchone() is None:
                return None
            corrected = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?",
                                   (request.job_id, REDECODE_PHASE)).fetchone()
            original = db.execute("SELECT * FROM observations WHERE job_id=? AND phase='worker_finished'",
                                  (request.job_id,)).fetchone()
        if corrected is not None:
            return self._typed(corrected)
        if original is None:
            return None
        old_metadata, old_carrier = _decode(original["metadata"]), json.loads(original["carrier"])
        old_diagnostics = set(old_carrier.get("parser", {}).get("diagnostics", []))
        if (old_metadata["status"] != "unknown" or not old_diagnostics
                or not old_diagnostics <= {"host_reported_failure", "item_before_turn_start", "tool_free_policy_violated"}):
            return self._typed(original)
        if old_carrier.get("binding") != binding:
            raise HarnessError("archived capture belongs to a different exact packet/claim")
        capture = old_carrier.get("process_capture")
        if not capture or capture.get("worker_finished") is not True:
            return self._typed(original)
        try:
            streams = {name: None if old_carrier["streams_base64"][name] is None else
                       base64.b64decode(old_carrier["streams_base64"][name], validate=True)
                       for name in ("stdout", "stderr", "final")}
        except (KeyError, TypeError, ValueError) as error:
            raise HarnessError("original archived capture bytes unavailable") from error
        if streams["final"] != original["response"]:
            raise HarnessError("archived final bytes differ from original response")
        if any(data is not None and len(data) > self.config["max_capture_bytes"] for data in streams.values()):
            raise HarnessError("capture exceeds the frozen byte ceiling")
        metadata, carrier = self._decode_capture(request, binding, capture, streams)
        if metadata["status"] != "completed" or not (carrier["parser"].get("recovered_reconnect_errors")
                                                     or carrier["parser"].get("tolerated_host_notices")):
            return self._typed(original)
        carrier["explicit_redecode"] = {"decoder_version": CONTRACT_VERSION,
                                      "reconnect_whitelist": "429 retry limit; websocket 403; code-mode fail-closed notice",
                                      "original_observation_id": old_metadata["observation_id"],
                                      "original_phase": "worker_finished"}
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                       (request.job_id, REDECODE_PHASE, _json(metadata), streams["final"], json_bytes(carrier)))
            row = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?",
                             (request.job_id, REDECODE_PHASE)).fetchone()
        return self._typed(row)

    def local_capture(self, request):
        """This transport's local process record of a claimed request, or None; launches nothing."""
        return self.runner.observe_existing(self._binding(request))

    def timeout_observation(self, request):
        """A failed observation for a call that this transport stopped at its time limit, or None.

        The evidence is this transport's own local capture: the worker finished,
        the process was stopped because it reached the per-call limit, and the
        call's first observation is unknown. The host session must be known. The
        same observation is built each time; nothing is launched or recorded.
        """
        binding = self._binding(request)
        with self._db() as db:
            if db.execute("SELECT 1 FROM jobs WHERE job_id=?", (request.job_id,)).fetchone() is None:
                return None
        capture = self.runner.observe_existing(binding)
        if not (capture and capture.get("worker_finished") is True and capture.get("timed_out") is True
                and capture.get("kill_reason") == "timeout" and capture.get("local_terminal_observed") is True):
            return None
        observed = self._observe(request, binding)
        if observed.metadata.get("status") != "unknown":
            return None
        host = observed.metadata.get("host_reference") or self._session_reference(capture)
        if host is None:
            return None
        metadata = {**observed.metadata, "observation_id": "timeout_" + request.job_id, "status": "failed",
                    "terminal_observed": True, "host_reference": host, "source_event_id": None, "source_line": None,
                    "observed_at_utc": capture.get("finished_at_utc") or observed.metadata["observed_at_utc"]}
        carrier = {"schema": "victorique.timeout_settlement.v1", "binding": binding, "reason": "local_timeout",
                   "timeout_seconds": (capture.get("invocation") or {}).get("timeout_seconds"),
                   "finished_at_utc": capture.get("finished_at_utc"),
                   "unknown_observation_id": observed.metadata["observation_id"]}
        from victorique.harness_bridge import TransportObservation
        return TransportObservation(metadata, None, json_bytes(carrier))

    def _session_reference(self, capture):
        """The host session of a stopped call when its first observation names none."""
        return None

    @staticmethod
    def _typed(row):
        # This is the only core import. Independent sidecar doctor and planning
        # work without an installed native core; bridge use requires that core.
        from victorique.harness_bridge import TransportObservation
        return TransportObservation(_decode(row["metadata"]), row["response"], row["carrier"])

    def _observe(self, request, binding):
        # Only an explicit redecode call can create this correction. Reading it
        # never silently reparses or replaces the historic cached observation.
        with self._db() as db:
            corrected = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?",
                                   (request.job_id, REDECODE_PHASE)).fetchone()
        if corrected is not None:
            return self._typed(corrected)
        capture = self.runner.observe_existing(binding)
        phase = "worker_finished" if capture and capture.get("worker_finished") else "pending"
        with self._db() as db:
            row = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?", (request.job_id, phase)).fetchone()
            if row is not None:
                return self._typed(row)
        streams = {}
        for name in ("stdout", "stderr", "final"):
            record = capture.get("streams", {}).get(name) if capture else None
            path = checked_path(record["path"]) if phase == "worker_finished" and record and record.get("path") else None
            if path and path.is_file():
                if path.stat().st_size > self.config["max_capture_bytes"]:
                    raise HarnessError("capture exceeds the frozen byte ceiling")
                streams[name] = path.read_bytes()
            else:
                streams[name] = None
        metadata, carrier = self._decode_capture(request, binding, capture, streams)
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                       (request.job_id, phase, _json(metadata), streams["final"], json_bytes(carrier)))
            row = db.execute("SELECT * FROM observations WHERE job_id=? AND phase=?", (request.job_id, phase)).fetchone()
            return self._typed(row)

    def _decode_capture(self, request, binding, capture, streams):
        from .codex_contract import parse_codex_capture
        final_phase = bool(capture and capture.get("local_terminal_observed"))
        incomplete = bool(capture and capture.get("worker_finished") and (
            any(capture.get("streams", {}).get(name, {}).get("complete") is not True for name in ("stdout", "stderr"))
            or capture.get("stdin_complete") is False or capture.get("capture_error") or capture.get("launch_error")))
        process = {"local_terminal": final_phase, "exit_code": capture.get("exit_code") if capture else None,
                   "timed_out": capture.get("timed_out", False) if capture else False,
                   "capture_truncated": incomplete or (any((capture.get("streams", {}).get(name) or {}).get("truncated", False) for name in streams) if capture else False)}
        parsed = parse_codex_capture(streams["stdout"] or b"", streams["stderr"] or b"", streams["final"], process, request.packet)
        metadata = {"schema": "victorique.bridge_observation.v1", "observation_id": "codex_obs_" + uuid4().hex,
                    **{name: binding[name] for name in ("job_id", "core_claim_id", "packet_id")},
                    "adapter_id": self.ADAPTER_ID, "adapter_version": VERSION, "mode": self.MODE,
                    "status": parsed["status"], "terminal_observed": parsed["terminal_observed"],
                    "host_reference": parsed["host_reference"], "observed_at_utc": _now(),
                    "source_event_id": parsed.get("source_event_id"), "source_line": parsed.get("source_line"),
                    "model_observation": None, "charge_observation": None}
        carrier = {"schema": "victorique.codex_carrier.v1", "classification": "synthetic_software_test" if self.MODE == "synthetic" else "host_capture",
                              "binding": binding, "process_capture": capture, "parser": parsed,
                              "streams_base64": {key: None if data is None else base64.b64encode(data).decode("ascii") for key, data in streams.items()},
                              "streams_encoding": "reversible original bytes; capture limits explicitly recorded",
                              "financially_settled": False, "mathematical_truth_verified": False}
        return metadata, carrier


class CodexFixtureTransport(CodexCliTransport):
    """The exact same archive/decoder path with a fixed, non-model child."""

    MODE = "synthetic"
    ADAPTER_ID = "codex_cli_inert_fixture"

    @classmethod
    def create(cls, root, *, response_bytes, scenario="success", output_schemas=None,
               timeout_seconds=2, max_capture_bytes=131072, delay_seconds=0):
        if type(response_bytes) is not bytes:
            raise HarnessError("an explicit INERT response byte fixture is required")
        config = {"schema": SCHEMA, "version": VERSION, "mode": cls.MODE,
                  "profile": None, "authorization": None,
                  "output_schemas": output_schemas or {"propose_plan": {"type": "object"}},
                  "timeout_seconds": timeout_seconds, "max_capture_bytes": max_capture_bytes,
                  "scenario": scenario, "delay_seconds": delay_seconds}
        result = cls._create(root, config)
        write_new(result.root / "inert-response.bin", response_bytes)
        return result

    def _invocation(self, request, binding):
        from .codex_contract import build_response_schema
        from .codex_process import fake_codex_argv
        layout = self.runner.layout(binding)
        invocation = {"argv": fake_codex_argv(layout["final_file"], scenario=self.config["scenario"],
                                              response_file=self.root / "inert-response.bin", delay=self.config["delay_seconds"]),
                      "cwd": str(layout["workspace"]), "final_file": str(layout["final_file"])}
        from .codex_contract import packet_scope
        key = packet_scope(request.packet)["output_schema_key"]
        output_schema = None if key is None else self.config["output_schemas"][key]
        invocation.update(timeout_seconds=self.config["timeout_seconds"], max_capture_bytes=self.config["max_capture_bytes"],
                          schema_bytes=json_bytes(build_response_schema(request.packet, output_schema)))
        return invocation
