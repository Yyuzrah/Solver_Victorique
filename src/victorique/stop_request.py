"""Stop requests: one small file in a run folder that asks the run to end.

``victorique stop DIR`` and ``victorique.stop(DIR)`` write ``DIR/stop-request.json``
with the time of the request. A run looks for this file before it prepares or
sends each model call and while it waits for one. When it finds the file it
sends no new call, and a call in flight ends the way a call that reaches its
per-call time limit ends: the worker process that runs the model CLI stops it.
Such a call is not sent again. The run then ends with the reason
``stopped_by_user`` (outcome ``partial``) and writes ``summary.json``,
``calls.jsonl`` and its best draft in ``proof.md``. The file stays in the run
folder, so resuming the run ends it in the same way.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time

from . import shared_read
from .harness.safety import checked_path

FILE = "stop-request.json"
SCHEMA = "victorique.stop_request.v1"
# The end reason of a stopped run, in summary.json endpoint_reason.
REASON = "stopped_by_user"
# Summary outcomes of a run that has ended; "pending" is a run that can still continue.
FINISHED_OUTCOMES = ("complete", "abstain", "partial", "unexecuted")


class StopRequested(Exception):
    """Raised in a run's step when its stop request exists before a model call is prepared or sent."""


def requested(path):
    """True when the stop request file ``path`` exists; reads nothing."""
    return path is not None and os.path.lexists(path)


def _json(path):
    try:
        return shared_read.read_json(path, limit=1024 * 1024)
    except (OSError, ValueError):
        return None


def read(directory):
    """The run's stop request as recorded, {} when the file cannot be read, or None without a request."""
    path = Path(directory) / FILE
    if not requested(path):
        return None
    value = _json(path)
    return value if isinstance(value, dict) else {}


def _write(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    temporary.write_bytes(data)
    try:
        for attempt in range(100):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # A classic replace on Windows fails while another process holds the target open.
                if attempt == 99:
                    raise
                time.sleep(0.02)
    finally:
        if temporary.exists():
            temporary.unlink()


def request(directory):
    """Ask the run in ``directory`` to stop; return what was done.

    ``status`` is ``requested`` (the request was written now), ``already_requested``
    (an earlier request stands; ``requested_at`` is its time) or ``finished``
    (the run's summary has a final outcome; nothing is written). A folder that is
    not a Victorique run (no ``invocation.json`` with parameters) is refused with
    ValueError.
    """
    directory = checked_path(Path(directory).absolute())
    invocation = _json(directory / "invocation.json") if directory.is_dir() else None
    if not isinstance(invocation, dict) or not isinstance(invocation.get("parameters"), dict):
        raise ValueError(f"{directory} is not a Victorique run folder: it has no readable invocation.json")
    path = checked_path(directory / FILE)
    result = {"output_directory": str(directory), "stop_request": str(path)}
    summary = _json(directory / "summary.json")
    outcome = summary.get("outcome") if isinstance(summary, dict) else None
    if outcome in FINISHED_OUTCOMES:
        return {**result, "status": "finished", "outcome": outcome,
                "endpoint_reason": summary.get("endpoint_reason"),
                "message": "The run has already finished; no stop request was recorded."}
    existing = read(directory)
    if existing is not None:
        return {**result, "status": "already_requested", "requested_at": existing.get("requested_at"),
                "message": "A stop request was already recorded for this run."}
    requested_at = datetime.now(timezone.utc).isoformat()
    _write(path, {"schema": SCHEMA, "requested_at": requested_at})
    return {**result, "status": "requested", "requested_at": requested_at,
            "message": ("Stop requested. A running run sends no new model call, ends the call in flight and "
                        "finishes with the reason stopped_by_user within seconds. A run whose process has "
                        "ended does so when it is resumed.")}
