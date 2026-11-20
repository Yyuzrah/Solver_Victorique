"""Offline dispatch responsibility for Victorique role requests.

An entered dispatch is never executed again.  This prevents local re-dispatch,
not exactly-once execution by an external system.  The only accepted receipts
in this candidate are explicitly inert software fixtures.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Callable
import uuid


SCHEMA = "victorique.harness.journal.v1"
PACKET_CLASS = "synthetic_software_test"
RECEIPT_SCHEMA = "victorique.harness.mock_receipt.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


class HarnessError(ValueError):
    """Invalid request, unsupported state, or unsafe local path."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HarnessError("value must be finite JSON") from exc
    if len(result.encode("utf-8")) > 1024 * 1024:
        raise HarnessError("JSON record exceeds 1 MiB")
    return result


def _safe_path(path: Path) -> Path:
    """Reject symlinks and Windows reparse points, without following them first."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise HarnessError("parent traversal is not a state path")
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise HarnessError("state paths must not contain links or reparse points")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise HarnessError("state files must not have hardlink aliases")
    return path


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 10000:
        raise HarnessError(f"{name} must be an integer from 1 to 10000")
    return value


def validate_packet(packet: object) -> dict:
    if not isinstance(packet, dict):
        raise HarnessError("packet must be a JSON object")
    if packet.get("data_classification") != PACKET_CLASS or packet.get("inert") is not True:
        raise HarnessError("this candidate accepts only explicitly INERT software packets")
    requested_model = packet.get("requested_model")
    if requested_model is not None and not isinstance(requested_model, str):
        raise HarnessError("requested_model must be a string or null")
    return json.loads(_json(packet))


def validate_receipt(receipt: object, job: dict) -> dict:
    if not isinstance(receipt, dict):
        raise HarnessError("receipt must be a JSON object")
    result = json.loads(_json(receipt))
    if result.get("schema") != RECEIPT_SCHEMA or result.get("data_classification") != PACKET_CLASS:
        raise HarnessError("only mock transport receipts are supported")
    for field in ("job_id", "dispatch_id"):
        if result.get(field) != job[field]:
            raise HarnessError(f"receipt {field} does not match the frozen dispatch")
    if result.get("transport_status") not in {"completed", "failed", "unknown"}:
        raise HarnessError("invalid transport_status")
    for field in ("stdout", "stderr"):
        if not isinstance(result.get(field), str):
            raise HarnessError(f"receipt {field} must preserve captured text")
    process = result.get("process")
    if not isinstance(process, dict) or type(process.get("timed_out")) is not bool:
        raise HarnessError("receipt process metadata is missing")
    if process.get("pid") is not None and (type(process["pid"]) is not int or process["pid"] < 1):
        raise HarnessError("process pid must be a positive integer or null")
    if process.get("exit_code") is not None and type(process["exit_code"]) is not int:
        raise HarnessError("process exit_code must be an integer or null")
    for field in ("actual_model", "usage", "cost"):
        if field not in result or result[field] is not None:
            raise HarnessError(f"mock receipt {field} must explicitly remain null")
    if result.get("requested_model") != job["packet"].get("requested_model"):
        raise HarnessError("requested_model must match the packet")
    if result["transport_status"] == "completed":
        if process.get("exit_code") != 0 or process["timed_out"]:
            raise HarnessError("completion requires observed successful process termination")
        output = result.get("output")
        if not isinstance(output, dict) or output.get("inert") is not True:
            raise HarnessError("completion requires a validated inert output")
        if output.get("received_job_id") != job["job_id"] or output.get("received_dispatch_id") != job["dispatch_id"]:
            raise HarnessError("output must belong to this dispatch")
        if result.get("host_session_id") != f"inert-{job['dispatch_id']}":
            raise HarnessError("completion requires the fixture session identity")
    elif result["transport_status"] == "failed":
        if process.get("exit_code") is None or process["timed_out"]:
            raise HarnessError("failure requires observed process termination without timeout")
    return result


class HarnessJournal:
    """Persistent local call accounting; no model, research or credential access.

    Construction opens only an existing harness journal.  Use create() for a
    fresh state root.  Each method uses a short SQLite transaction; execution
    happens outside its lock, after the unique claim is committed.
    """

    def __init__(self, root: str | Path):
        self.root = _safe_path(Path(root))
        self.path = _safe_path(self.root / "harness.sqlite3")
        if not self.path.is_file():
            raise HarnessError("state root does not contain a harness journal")
        try:
            with self._connection() as db:
                meta = dict(db.execute("SELECT key, value FROM metadata"))
        except sqlite3.DatabaseError as exc:
            raise HarnessError("not a harness journal") from exc
        if meta.get("schema") != SCHEMA:
            raise HarnessError("unsupported harness journal schema")

    @classmethod
    def create(cls, root: str | Path, *, max_calls: int = 1, max_in_flight: int = 1) -> "HarnessJournal":
        max_calls = _positive_int(max_calls, "max_calls")
        max_in_flight = _positive_int(max_in_flight, "max_in_flight")
        if max_in_flight > max_calls:
            raise HarnessError("max_in_flight must not exceed max_calls")
        target = _safe_path(Path(root))
        if target.exists():
            raise HarnessError("state root must be a new directory")
        if not target.parent.is_dir():
            raise HarnessError("state root parent must already exist")
        target.mkdir()
        path = target / "harness.sqlite3"
        # A failed creation intentionally leaves the partial directory for inspection.
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE jobs(
                    job_id TEXT PRIMARY KEY, packet TEXT NOT NULL,
                    state TEXT NOT NULL, dispatch_id TEXT UNIQUE,
                    created_at TEXT NOT NULL, entered_at TEXT,
                    receipt TEXT, error_type TEXT);
                CREATE TABLE events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    kind TEXT NOT NULL, job_id TEXT, detail TEXT NOT NULL);
                """)
                db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                    ("schema", SCHEMA), ("created_at", _now()),
                    ("max_calls", str(max_calls)), ("max_in_flight", str(max_in_flight)),
                    ("closed", "false"), ("close_reason", ""),
                ])
        return cls(target)

    @contextmanager
    def _connection(self, *, write: bool = False):
        _safe_path(self.path)
        for suffix in ("-journal", "-wal", "-shm"):
            _safe_path(Path(str(self.path) + suffix))
        # mode=rw prevents a path disappearing from silently creating an empty DB.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
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

    @staticmethod
    def _event(db, kind: str, job_id: str | None, detail: dict) -> None:
        db.execute("INSERT INTO events(at, kind, job_id, detail) VALUES (?, ?, ?, ?)",
                   (_now(), kind, job_id, _json(detail)))

    @staticmethod
    def _job(db, job_id: str) -> dict:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise HarnessError("unknown job_id")
        result = dict(row)
        result["packet"] = json.loads(result["packet"])
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        return result

    def prepare(self, job_id: str, packet: dict) -> dict:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise HarnessError("job_id must use 1-100 ASCII letters, digits, dot, underscore or hyphen")
        frozen = validate_packet(packet)
        encoded = _json(frozen)
        with self._connection(write=True) as db:
            existing = db.execute("SELECT packet FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise HarnessError("job_id already belongs to a different frozen packet")
                return {"prepared": False, "job_id": job_id}
            if db.execute("SELECT value FROM metadata WHERE key='closed'").fetchone()[0] == "true":
                raise HarnessError("journal is closed")
            db.execute("INSERT INTO jobs(job_id, packet, state, created_at) VALUES (?, ?, 'prepared', ?)",
                       (job_id, encoded, _now()))
            self._event(db, "prepared", job_id, {})
        return {"prepared": True, "job_id": job_id}

    def start_once(self, job_id: str, execute: Callable[[dict, dict], dict]) -> dict:
        if not callable(execute):
            raise HarnessError("execute must be callable")
        with self._connection(write=True) as db:
            job = self._job(db, job_id)
            if job["state"] != "prepared":
                return {"dispatched": False, "job_id": job_id, "state": job["state"], "dispatch_id": job["dispatch_id"]}
            meta = dict(db.execute("SELECT key, value FROM metadata"))
            if meta["closed"] == "true":
                raise HarnessError("journal is closed")
            used = db.execute("SELECT count(*) FROM jobs WHERE dispatch_id IS NOT NULL").fetchone()[0]
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('entered', 'unknown')").fetchone()[0]
            if used >= int(meta["max_calls"]):
                raise HarnessError("cumulative call budget exhausted")
            if active >= int(meta["max_in_flight"]):
                raise HarnessError("in-flight capacity held by entered or unknown dispatch")
            dispatch_id = uuid.uuid4().hex
            entered_at = _now()
            db.execute("UPDATE jobs SET state='entered', dispatch_id=?, entered_at=? WHERE job_id=?",
                       (dispatch_id, entered_at, job_id))
            claim = {"job_id": job_id, "dispatch_id": dispatch_id, "entered_at": entered_at,
                     "data_classification": PACKET_CLASS}
            self._event(db, "entered", job_id, claim)
            job["dispatch_id"] = dispatch_id
        try:
            receipt = execute(json.loads(_json(job["packet"])), dict(claim))
            # Preserve a JSON-shaped but invalid response before declaring uncertainty.
            # No content is projected into research state by this journal.
            try:
                receipt = validate_receipt(receipt, job)
            except HarnessError:
                try:
                    raw = json.loads(_json(receipt))
                except HarnessError:
                    raw = {"unserializable_type": type(receipt).__name__}
                with self._connection(write=True) as db:
                    self._event(db, "invalid_receipt", job_id, {"raw_receipt": raw})
                raise
        except BaseException as exc:
            with self._connection(write=True) as db:
                # A simultaneous explicit reconcile may already have observed termination.
                db.execute("UPDATE jobs SET state='unknown', error_type=? WHERE job_id=? AND state IN ('entered', 'unknown')",
                           (type(exc).__name__, job_id))
                self._event(db, "execute_exception", job_id, {"error_type": type(exc).__name__})
            if not isinstance(exc, Exception):
                raise
            return {"dispatched": True, "job_id": job_id, "dispatch_id": dispatch_id,
                    "state": self.job_status(job_id)["state"], "error_type": type(exc).__name__}
        self._record_receipt(job_id, receipt, "local_mock_process", from_execute=True)
        return {"dispatched": True, "job_id": job_id, "dispatch_id": dispatch_id,
                "state": self.job_status(job_id)["state"], "receipt": receipt}

    def _record_receipt(self, job_id: str, receipt: dict, evidence_ref: str, *, from_execute: bool = False) -> dict:
        with self._connection(write=True) as db:
            job = self._job(db, job_id)
            if job["dispatch_id"] is None:
                raise HarnessError("a prepared job has no dispatch to reconcile")
            valid = validate_receipt(receipt, job)
            encoded = _json(valid)
            if job["receipt"] is not None:
                if _json(job["receipt"]) == encoded:
                    return {"recorded": False, "state": job["state"]}
                if job["state"] in {"completed", "failed"}:
                    # Preserve contradictory observations without replacing the first terminal result.
                    self._event(db, "conflicting_receipt", job_id,
                                {"evidence_ref": evidence_ref, "receipt": valid})
                    return {"recorded": False, "state": job["state"], "conflict": True}
            closed = db.execute("SELECT value FROM metadata WHERE key='closed'").fetchone()[0] == "true"
            late = closed or job["state"] == "unknown" or not from_execute
            state = valid["transport_status"]
            # Receipt and derived software status commit atomically. No research gate is projected.
            db.execute("UPDATE jobs SET receipt=?, state=?, error_type=NULL WHERE job_id=?", (encoded, state, job_id))
            self._event(db, "late_receipt" if late else "receipt", job_id,
                        {"evidence_ref": evidence_ref, "receipt": valid, "after_close": closed})
            return {"recorded": True, "state": state, "late": late}

    def reconcile(self, job_id: str, receipt: dict, evidence_ref: str) -> dict:
        if not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref) > 1000:
            raise HarnessError("a nonempty bounded evidence_ref is required")
        # The reference is an audit label, never a file to read or a proof of identity.
        return self._record_receipt(job_id, receipt, evidence_ref)

    def recover(self, job_id: str) -> dict:
        with self._connection(write=True) as db:
            job = self._job(db, job_id)
            if job["state"] == "entered":
                db.execute("UPDATE jobs SET state='unknown' WHERE job_id=?", (job_id,))
                self._event(db, "observation_lost", job_id, {"dispatch_id": job["dispatch_id"]})
                job["state"] = "unknown"
            return {"job_id": job_id, "state": job["state"], "dispatch_id": job["dispatch_id"],
                    "redispatched": False, "remote_observation": "unsupported"}

    def close(self, reason: str) -> dict:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise HarnessError("a nonempty bounded close reason is required")
        with self._connection(write=True) as db:
            meta = dict(db.execute("SELECT key, value FROM metadata"))
            if meta["closed"] == "true":
                return {"closed": True, "reason": meta["close_reason"]}
            db.execute("UPDATE metadata SET value='true' WHERE key='closed'")
            db.execute("UPDATE metadata SET value=? WHERE key='close_reason'", (reason,))
            self._event(db, "closed", None, {"reason": reason})
        return {"closed": True, "reason": reason}

    @staticmethod
    def _summary(job: dict) -> dict:
        receipt = job["receipt"]
        return {"job_id": job["job_id"], "state": job["state"], "dispatch_id": job["dispatch_id"],
                "created_at": job["created_at"], "entered_at": job["entered_at"],
                "requested_model": job["packet"].get("requested_model"),
                "actual_model": receipt.get("actual_model") if receipt else None,
                "usage": receipt.get("usage") if receipt else None,
                "cost": receipt.get("cost") if receipt else None,
                "host_session_id": receipt.get("host_session_id") if receipt else None,
                "error_type": job["error_type"]}

    def job_status(self, job_id: str) -> dict:
        with self._connection() as db:
            return self._summary(self._job(db, job_id))

    def status(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            meta = dict(db.execute("SELECT key, value FROM metadata"))
            jobs = [self._summary(self._job(db, row[0])) for row in db.execute("SELECT job_id FROM jobs ORDER BY job_id").fetchall()]
            calls = sum(job["dispatch_id"] is not None for job in jobs)
            outstanding = sum(job["state"] in {"entered", "unknown"} for job in jobs)
            return {"schema": SCHEMA, "data_classification": PACKET_CLASS,
                    "closed": meta["closed"] == "true", "close_reason": meta["close_reason"] or None,
                    "max_calls": int(meta["max_calls"]), "calls_reserved": calls,
                    "calls_remaining": int(meta["max_calls"]) - calls,
                    "max_in_flight": int(meta["max_in_flight"]), "outstanding_calls": outstanding,
                    "observed_cost": None, "financially_settled": False,
                    "actual_model_calls": 0, "research_gate_projection": False, "jobs": jobs}

    def export(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            meta = dict(db.execute("SELECT key, value FROM metadata"))
            jobs = [self._job(db, row[0]) for row in db.execute("SELECT job_id FROM jobs ORDER BY job_id").fetchall()]
            events = [{**dict(row), "detail": json.loads(row["detail"])}
                      for row in db.execute("SELECT * FROM events ORDER BY seq")]
            return {"schema": "victorique.harness.export.v1", "data_classification": PACKET_CLASS,
                    "metadata": meta, "jobs": jobs, "events": events,
                    "authenticates_external_identity": False, "research_gate_projection": False}
