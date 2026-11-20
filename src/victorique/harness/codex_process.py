"""Durable local process evidence for one already claimed Codex role.

The caller owns authorization, host policy and the core budget. This layer has
no new-call budget, model selection, remote query or retry path. A persistent
fence precedes helper launch; an uncertain launch is never repeated. A separate
helper can finish capturing the same child after its coordinator exits.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from .safety import HarnessError, checked_path, json_bytes, write_new


SCHEMA = "victorique.harness.codex_process.v1"
CAPTURE_SCHEMA = "victorique.harness.codex_process_capture.v1"
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
_JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
# kill_reason of a call ended because its run's stop request appeared (the invocation's stop_file).
STOP_KILL_REASON = "stopped_by_user"
# How often a running worker looks for the stop request file.
STOP_POLL_SECONDS = 0.1


def _stop_requested(invocation):
    """True when the invocation names a stop request file and it exists."""
    path = invocation.get("stop_file")
    return path is not None and os.path.lexists(path)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clone(value):
    try:
        return json.loads(_encode(value))
    except (ValueError, TypeError, RecursionError) as error:
        raise HarnessError("expected finite JSON metadata") from error


def _binding(binding):
    if not isinstance(binding, dict):
        raise HarnessError("binding must be an object")
    result = _clone(binding)
    required = {"job_id", "core_claim_id", "packet_id", "mode"}
    if not required.issubset(result) or set(result) - required - {"session_id", "protocol"}:
        raise HarnessError("binding has missing or unsupported fields")
    for name in ("job_id", "core_claim_id", "packet_id"):
        if not isinstance(result[name], str) or not result[name] or len(result[name]) > 200:
            raise HarnessError("binding identifiers must be bounded nonempty strings")
    if not _JOB.fullmatch(result["job_id"]):
        raise HarnessError("job_id is not a safe directory identifier")
    if result["mode"] not in {"synthetic", "host_recorded"}:
        raise HarnessError("mode must be synthetic or host_recorded")
    if len(_encode(result).encode("utf-8")) > 16384:
        raise HarnessError("binding metadata is too large")
    return result


def _bounded_number(value, low, high, label):
    if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
        raise HarnessError(f"{label} must be finite and between {low} and {high}")
    return value


def _deadline(value):
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
        return deadline
    except (ValueError, TypeError, AttributeError) as error:
        raise HarnessError("deadline_utc must be an absolute UTC timestamp") from error


class DurableCodexProcess:
    def __init__(self, root):
        self.root = checked_path(root)
        self.db_path = checked_path(self.root / "processes.sqlite3")
        if not self.db_path.is_file():
            raise HarnessError("not an initialized Codex process root")
        try:
            with self._db() as db:
                row = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if row is None or row[0] != SCHEMA:
                    raise HarnessError("unsupported Codex process schema")
                workspace = db.execute("SELECT value FROM metadata WHERE key='workspace_root'").fetchone()
                self.workspace_root = checked_path(workspace[0]) if workspace else None
        except sqlite3.DatabaseError as error:
            raise HarnessError("invalid Codex process database") from error

    @classmethod
    def create(cls, root, *, workspace_root=None):
        target = checked_path(root)
        if target.exists() or not target.parent.is_dir():
            raise HarnessError("a new process root with an existing parent is required")
        workspace = checked_path(workspace_root) if workspace_root is not None else None
        if workspace is not None and (workspace.is_relative_to(target) or target.is_relative_to(workspace)):
            raise HarnessError("external workspace root must be disjoint from process state")
        target.mkdir()
        (target / "jobs").mkdir()
        with closing(sqlite3.connect(target / "processes.sqlite3")) as db:
            with db:
                db.executescript("""
                  CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                  CREATE TABLE jobs(
                    job_id TEXT PRIMARY KEY, core_claim_id TEXT UNIQUE NOT NULL,
                    binding TEXT NOT NULL, invocation TEXT NOT NULL,
                    packet BLOB NOT NULL, schema_bytes BLOB,
                    state TEXT NOT NULL, created_at TEXT NOT NULL,
                    launch_token TEXT NOT NULL, worker_pid INTEGER,
                    child_pid INTEGER, worker_started_at TEXT,
                    launch_error TEXT, capture TEXT);
                """)
                db.execute("INSERT INTO metadata VALUES ('schema', ?)", (SCHEMA,))
                db.execute("INSERT INTO metadata VALUES ('created_at', ?)", (_now(),))
                if workspace is not None:
                    db.execute("INSERT INTO metadata VALUES ('workspace_root', ?)", (str(workspace),))
        return cls(target)

    @contextmanager
    def _db(self, write=False):
        checked_path(self.db_path)
        for suffix in ("-journal", "-wal", "-shm"):
            checked_path(Path(str(self.db_path) + suffix), transient_sqlite_sidecar=True)
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if write and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def layout(self, binding):
        binding = _binding(binding)
        base = checked_path(self.root / "jobs" / binding["job_id"])
        workspace = checked_path(self.workspace_root / binding['job_id'] if self.workspace_root else base / "workspace")
        files = base if self.workspace_root else workspace
        return {"job_root": str(base), "workspace": str(workspace),
                "final_file": str(checked_path(files / "host-final.json")),
                "schema_file": str(checked_path(files / "response-schema.json")),
                "stdout_file": str(checked_path(base / "stdout.bin")),
                "stderr_file": str(checked_path(base / "stderr.bin")),
                "packet_file": str(checked_path(base / "stdin.bin")),
                "spec_file": str(checked_path(base / "invocation.json")),
                "terminal_file": str(checked_path(base / "terminal.json"))}

    def _invocation(self, binding, invocation):
        if not isinstance(invocation, dict):
            raise HarnessError("invocation must be an object")
        allowed = {"argv", "cwd", "final_file", "timeout_seconds", "max_capture_bytes", "schema_bytes", "environment_policy",
                   "deadline_utc", "stop_file"}
        if set(invocation) - allowed or not {"argv", "cwd", "final_file"}.issubset(invocation):
            raise HarnessError("invocation contains missing or unsupported policy fields; arbitrary env overrides are not supported")
        policy = invocation.get("environment_policy")
        if policy not in (None, "claude_subscription_isolated"):
            raise HarnessError("unsupported environment policy")
        optional_fields = {}
        if "deadline_utc" in invocation:
            optional_fields["deadline_utc"] = _deadline(invocation["deadline_utc"]).isoformat()
        if "stop_file" in invocation:
            # The worker ends the call when this file appears, as it does at the per-call time limit.
            if not isinstance(invocation["stop_file"], str):
                raise HarnessError("stop_file must be an absolute path")
            optional_fields["stop_file"] = str(checked_path(invocation["stop_file"]))
        argv = invocation["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise HarnessError("argv must be a nonempty string list without NUL")
        if sum(len(arg.encode("utf-8")) for arg in argv) > 262144:
            raise HarnessError("argv exceeds bounded metadata size")
        binary = checked_path(argv[0])
        if not binary.is_file() or binary.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise HarnessError("argv[0] must be an explicit native executable")
        layout = self.layout(binding)
        cwd = checked_path(invocation["cwd"])
        final_file = checked_path(invocation["final_file"])
        if str(cwd) != layout["workspace"] or str(final_file) != layout["final_file"]:
            raise HarnessError("cwd and final_file must be this job's fixed independent layout")
        timeout = _bounded_number(invocation.get("timeout_seconds", 300), 0.05, 86400, "timeout_seconds")
        limit = invocation.get("max_capture_bytes", MAX_CAPTURE_BYTES)
        if type(limit) is not int or not 1024 <= limit <= MAX_CAPTURE_BYTES:
            raise HarnessError("max_capture_bytes must be an integer from 1024 to 33554432")
        schema_bytes = invocation.get("schema_bytes")
        if schema_bytes is not None and (not isinstance(schema_bytes, bytes) or len(schema_bytes) > MAX_INPUT_BYTES):
            raise HarnessError("schema_bytes must be bytes at most 1 MiB")
        return {"argv": list(argv), "cwd": str(cwd), "final_file": str(final_file),
                "timeout_seconds": timeout, "max_capture_bytes": limit,
                "schema_file": layout["schema_file"] if schema_bytes is not None else None,
                "shell": False, "environment": policy or "inherited_by_process_without_inspection", **optional_fields}, schema_bytes

    def _checkpoint(self, stage, binding):
        """In-process fault-injection hook for software tests; no configured code."""

    def start_once(self, binding, invocation, packet_bytes, *, wait_seconds=0):
        binding = _binding(binding)
        normalized, schema_bytes = self._invocation(binding, invocation)
        if not isinstance(packet_bytes, bytes) or len(packet_bytes) > MAX_INPUT_BYTES:
            raise HarnessError("packet_bytes must be exact bytes at most 1 MiB")
        _bounded_number(wait_seconds, 0, 60, "wait_seconds")
        fresh = False
        with self._db(True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (binding["job_id"],)).fetchone()
            if row is not None:
                if row["binding"] != _encode(binding) or row["invocation"] != _encode(normalized) or bytes(row["packet"]) != packet_bytes or row["schema_bytes"] != schema_bytes:
                    raise HarnessError("job is already frozen to a different binding, invocation or packet")
            else:
                if db.execute("SELECT job_id FROM jobs WHERE core_claim_id=?", (binding["core_claim_id"],)).fetchone():
                    raise HarnessError("core claim already belongs to another process job")
                token = uuid.uuid4().hex
                db.execute("INSERT INTO jobs(job_id,core_claim_id,binding,invocation,packet,schema_bytes,state,created_at,launch_token) VALUES (?,?,?,?,?,?,'fenced',?,?)",
                           (binding["job_id"], binding["core_claim_id"], _encode(binding), _encode(normalized), packet_bytes, schema_bytes, _now(), token))
                fresh = True
        if fresh:
            self._checkpoint("after_fence", binding)
            layout = self.layout(binding)
            try:
                Path(layout["job_root"]).mkdir()
                Path(layout["workspace"]).mkdir(parents=True)
                write_new(layout["packet_file"], packet_bytes)
                if schema_bytes is not None:
                    write_new(layout["schema_file"], schema_bytes)
                write_new(layout["spec_file"], json_bytes({"schema": SCHEMA, "binding": binding,
                    "invocation": normalized, "packet_file": layout["packet_file"], "packet_bytes": len(packet_bytes)}))
                self._checkpoint("before_helper_launch", binding)
                worker = checked_path(Path(__file__).with_name("codex_worker.py"))
                kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "cwd": str(worker.parent), "shell": False}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True
                process = subprocess.Popen([sys.executable, "-I", "-B", str(worker), str(self.root), binding["job_id"], token], **kwargs)
                with self._db(True) as db:
                    db.execute("UPDATE jobs SET worker_pid=COALESCE(worker_pid,?) WHERE job_id=?", (process.pid, binding["job_id"]))
                # Reap only this created helper. Its lifetime and capture do not
                # depend on this optional coordinator-side thread surviving.
                threading.Thread(target=process.wait, daemon=True).start()
                self._checkpoint("after_helper_launch", binding)
            except Exception as error:
                with self._db(True) as db:
                    db.execute("UPDATE jobs SET launch_error=? WHERE job_id=?", (type(error).__name__, binding["job_id"]))
        deadline = time.monotonic() + wait_seconds
        while True:
            result = self.observe_existing(binding)
            if result is None or result["worker_finished"] or time.monotonic() >= deadline:
                return result
            time.sleep(min(0.025, max(0, deadline - time.monotonic())))

    def observe_existing(self, binding):
        binding = _binding(binding)
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (binding["job_id"],)).fetchone()
        if row is None:
            return None
        if row["binding"] != _encode(binding):
            raise HarnessError("binding does not match the existing process")
        layout = self.layout(binding)
        capture = json.loads(row["capture"]) if row["capture"] is not None else None
        terminal = checked_path(layout["terminal_file"])
        if capture is None and terminal.is_file():
            # A helper may have committed its final file and died before SQLite
            # acknowledgement. Observation consumes that same receipt, never starts.
            if terminal.stat().st_size > MAX_INPUT_BYTES:
                raise HarnessError("terminal metadata exceeds limit")
            capture = json.loads(terminal.read_bytes())
        if capture is not None:
            if capture.get("schema") != CAPTURE_SCHEMA or capture.get("binding") != binding:
                raise HarnessError("terminal capture belongs to a different process")
            for key, path_key in (("stdout", "stdout_file"), ("stderr", "stderr_file"), ("final", "final_file")):
                item = capture["streams"][key]
                if item["path"] != layout[path_key]:
                    raise HarnessError("capture artifact escaped fixed layout")
                path = checked_path(item["path"])
                if item["captured_bytes"] is not None and (not path.is_file() or path.stat().st_size != item["captured_bytes"]):
                    raise HarnessError("capture artifact size changed or is missing")
            return capture
        return {"schema": CAPTURE_SCHEMA, "binding": binding, "invocation": json.loads(row["invocation"]),
                "state": "unknown", "worker_finished": False, "worker_pid": row["worker_pid"],
                "child_pid": row["child_pid"], "worker_started_at": row["worker_started_at"],
                "local_terminal_observed": False, "exit_code": None, "timed_out": False,
                "kill_requested": False, "kill_reason": None, "launch_error": row["launch_error"],
                "streams": {key: {"path": layout[path_key], "captured_bytes": None,
                                   "observed_bytes": None, "truncated": None, "complete": False}
                            for key, path_key in (("stdout", "stdout_file"), ("stderr", "stderr_file"), ("final", "final_file"))},
                "host_terminal_observed": False, "restarted": False,
                "read_only_recovery": "local_capture_only", "environment_recorded": False}

    def _worker_enter(self, job_id, token):
        with self._db(True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["launch_token"] != token or row["state"] != "fenced":
                return None
            started = _now()
            db.execute("UPDATE jobs SET state='worker_entered',worker_pid=?,worker_started_at=? WHERE job_id=?",
                       (os.getpid(), started, job_id))
            return {**dict(row), "worker_pid": os.getpid(), "worker_started_at": started}

    def _worker_finish(self, job, capture):
        layout = self.layout(json.loads(job["binding"]))
        write_new(layout["terminal_file"], json_bytes(capture))
        with self._db(True) as db:
            db.execute("UPDATE jobs SET state='worker_finished',capture=? WHERE job_id=? AND capture IS NULL",
                       (_encode(capture), job["job_id"]))
        if self.workspace_root is not None:
            # Only empty disposable cwd directories are removed. Captures and
            # final output remain in the durable job directory for recovery.
            try:
                checked_path(layout["workspace"]).rmdir()
                checked_path(self.workspace_root).rmdir()
            except OSError:
                pass


def _capture_pipe(pipe, path, limit, result):
    observed = 0
    captured = 0
    try:
        with checked_path(path).open("xb") as output:
            # BufferedReader.read(n) may wait for n bytes or EOF. read1 returns
            # currently available pipe bytes so a small thread/session event is
            # persisted while the role is still running, before any later crash.
            read_chunk = getattr(pipe, "read1", pipe.read)
            while True:
                chunk = read_chunk(65536)
                if not chunk:
                    break
                observed += len(chunk)
                keep = chunk[:max(0, limit - captured)]
                if keep:
                    output.write(keep)
                    captured += len(keep)
                    output.flush()
                    os.fsync(output.fileno())
            output.flush()
            os.fsync(output.fileno())
        result.update(path=str(path), captured_bytes=captured, observed_bytes=observed,
                      truncated=observed > captured, complete=observed == captured)
    except BaseException as error:
        result.update(path=str(path), captured_bytes=None, observed_bytes=observed,
                      truncated=None, complete=False, capture_error=type(error).__name__)
    finally:
        pipe.close()


def _final_capture(path, limit):
    target = checked_path(path)
    if not target.exists():
        return {"path": str(target), "captured_bytes": None, "observed_bytes": None,
                "truncated": False, "complete": False, "capture_error": "final_file_missing"}
    if not target.is_file():
        raise HarnessError("final capture is not a regular file")
    observed = target.stat().st_size
    if observed > limit:
        # Only this newly created, fixed job output may be reduced to a prefix.
        # checked_path rejects symlinks/reparse points/hardlink aliases before IO.
        # This is not a defence against a privileged concurrent path swap.
        checked_path(target)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "r+b") as output:
            opened = os.fstat(output.fileno())
            checked_path(target)
            current = target.lstat()
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(opened, current):
                raise HarnessError("final capture changed identity before prefix retention")
            output.truncate(limit)
            output.flush()
            os.fsync(output.fileno())
    captured = min(observed, limit)
    return {"path": str(target), "captured_bytes": captured, "observed_bytes": observed,
            "truncated": observed > captured, "complete": observed == captured}


def _run_worker(root, job_id, token):
    runner = DurableCodexProcess(root)
    job = runner._worker_enter(job_id, token)
    if job is None:
        return 0
    binding = json.loads(job["binding"])
    invocation = json.loads(job["invocation"])
    layout = runner.layout(binding)
    streams = {key: {"path": layout[path_key], "captured_bytes": None,
                     "observed_bytes": None, "truncated": None, "complete": False}
               for key, path_key in (("stdout", "stdout_file"), ("stderr", "stderr_file"), ("final", "final_file"))}
    capture = {"schema": CAPTURE_SCHEMA, "binding": binding, "invocation": invocation,
               "state": "unknown", "worker_finished": True, "worker_pid": os.getpid(),
               "child_pid": None, "worker_started_at": job["worker_started_at"],
               "local_terminal_observed": False, "exit_code": None, "timed_out": False,
               "kill_requested": False, "kill_reason": None, "launch_error": None,
               "streams": streams, "host_terminal_observed": False,
               "restarted": False, "read_only_recovery": "local_capture_only", "environment_recorded": False}
    process = None
    try:
        packet_path = checked_path(layout["packet_file"])
        if packet_path.stat().st_size != len(job["packet"]) or packet_path.read_bytes() != bytes(job["packet"]):
            raise HarnessError("stdin file differs from the frozen packet")
        checked_path(invocation["argv"][0])
        checked_path(invocation["cwd"])
        if Path(layout["final_file"]).exists():
            raise HarnessError("fresh role final file already exists")
        if job["schema_bytes"] is not None:
            schema_path = checked_path(layout["schema_file"])
            if schema_path.stat().st_size != len(job["schema_bytes"]) or schema_path.read_bytes() != bytes(job["schema_bytes"]):
                raise HarnessError("schema differs from the frozen invocation")
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "cwd": invocation["cwd"], "shell": False}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        # The default host environment remains inherited. Claude accepts only
        # one fixed policy; values are passed to its process, never serialized.
        if invocation["environment"] == "claude_subscription_isolated":
            from .claude_contract import claude_environment
            kwargs["env"] = claude_environment()
        timeout = invocation["timeout_seconds"]
        if invocation.get("deadline_utc"):
            remaining = (_deadline(invocation["deadline_utc"]) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                capture["timed_out"] = True
                raise HarnessError("authorization deadline passed before child launch")
            timeout = min(timeout, remaining)
        if _stop_requested(invocation):
            capture.update(kill_requested=True, kill_reason=STOP_KILL_REASON)
            raise HarnessError("stop requested before child launch")
        started = time.monotonic()
        process = subprocess.Popen(invocation["argv"], **kwargs)
        capture["child_pid"] = process.pid
        with runner._db(True) as db:
            db.execute("UPDATE jobs SET child_pid=? WHERE job_id=?", (process.pid, job_id))
        readers = [threading.Thread(target=_capture_pipe, args=(pipe, layout[path_key], invocation["max_capture_bytes"], streams[key]), daemon=True)
                   for pipe, path_key, key in ((process.stdout, "stdout_file", "stdout"), (process.stderr, "stderr_file", "stderr"))]
        for reader in readers:
            reader.start()
        input_error = []
        def send_input():
            try:
                process.stdin.write(bytes(job["packet"]))
                process.stdin.flush()
            except BrokenPipeError:
                input_error.append("BrokenPipeError")
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
        writer = threading.Thread(target=send_input, daemon=True)
        writer.start()
        next_stop_check = started
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= timeout:
                capture.update(timed_out=True, kill_requested=True, kill_reason="timeout")
                process.kill()
                break
            if now >= next_stop_check:
                # The run's stop request ends the call the same way, without counting as a timeout.
                next_stop_check = now + STOP_POLL_SECONDS
                if _stop_requested(invocation):
                    capture.update(kill_requested=True, kill_reason=STOP_KILL_REASON)
                    process.kill()
                    break
            time.sleep(0.01)
        process.wait()
        capture.update(state="exited", local_terminal_observed=True, exit_code=process.returncode)
        # The timeout covers the direct host process; pipe closure can be held by
        # descendants. Do not wait indefinitely or claim a complete capture.
        for thread in [writer, *readers]:
            thread.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            capture["capture_error"] = "pipe_not_closed_after_process_exit"
        if writer.is_alive() or input_error:
            capture["stdin_complete"] = False
        else:
            capture["stdin_complete"] = True
        streams["final"] = _final_capture(layout["final_file"], invocation["max_capture_bytes"])
    except BaseException as error:
        capture["launch_error"] = type(error).__name__
        if process is not None and process.poll() is None:
            capture.update(kill_requested=True, kill_reason="worker_error")
            process.kill()
            process.wait()
            capture.update(local_terminal_observed=True, exit_code=process.returncode, state="exited")
    capture["finished_at_utc"] = _now()
    try:
        runner._worker_finish(job, capture)
    except BaseException:
        # A failed write is an unknown observation; never start another process.
        return 1
    return 0


def fake_codex_argv(final_file, *, scenario="success", response_file=None, delay=0.0, output_bytes=4096):
    """Fixed bundled INERT executable for tests; it cannot launch a model."""
    scenarios = {"success", "timeout", "nonzero", "malformed", "mismatch", "no-final", "binary", "oversize", "delay-after-thread-start",
                 "reconnect-success", "reconnect-failed"}
    if scenario not in scenarios:
        raise HarnessError("unknown INERT fake scenario")
    _bounded_number(delay, 0, 10, "fixture delay")
    if type(output_bytes) is not int or not 0 <= output_bytes <= 64 * 1024 * 1024:
        raise HarnessError("invalid fixture output size")
    argv = [sys.executable, "-I", "-B", str(Path(__file__).with_name("codex_fake_host.py")),
            "--scenario", scenario, "--final-file", str(checked_path(final_file)),
            "--delay", str(delay), "--output-bytes", str(output_bytes)]
    if response_file is not None:
        argv += ["--response-file", str(checked_path(response_file))]
    return argv
