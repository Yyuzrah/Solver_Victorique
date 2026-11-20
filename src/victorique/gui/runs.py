"""Read-only views of run directories for the local interface.

Every read of a run file goes through ``victorique.shared_read``, so the
interface never denies delete or rename sharing to a process that writes the
run. Nothing here starts, changes or resumes a run.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import stat
import threading

from .. import shared_read
from ..adaptive_contracts import gate_passes
from ..stop_request import FILE as STOP_FILE, REASON as STOP_REASON

GUI_FOLDER = ".victorique-gui"
# A run folder name: one path component, not hidden, without characters Windows reserves.
NAME = re.compile(r'(?![.\s])[^\\/:*?"<>|\x00-\x1f]{1,128}(?<![.\s])\Z')
JSON_LIMIT = 32 * 1024 * 1024
TEXT_LIMIT = 8 * 1024 * 1024
LOG_TAIL_BYTES = 64 * 1024
LOG_TAIL_LINES = 80
# A run that nobody here started and that has no summary is taken to be running
# until this long after its own deadline.
DEADLINE_GRACE = timedelta(minutes=15)

RUN_FILES = ("proof.md", "summary.json", "calls.jsonl", "invocation.json")
GUI_FILES = ("problem.md", "console.log")
FINISHED = ("delivered", "abstained", "failed", "interrupted", "stopped")
# Statuses of a run whose process is starting or running.
ACTIVE = ("starting", "running", "stopping")

STATUS_LABELS = {
    "starting": "Starting",
    "running": "Running",
    # Running, with a stop request in its folder (victorique stop or the Stop run button).
    "stopping": "Stopping",
    "delivered": "Proof delivered",
    "abstained": "No proof delivered",
    "failed": "Failed",
    "interrupted": "Stopped before finishing",
    # Ended at its stop request: summary.json endpoint_reason stopped_by_user.
    "stopped": "Stopped by you",
}
POLICY_LABELS = {
    "single": "Solve once and check",
    "ladder": "Solve, check and repair",
    "full": "Full workflow",
    "resample": "Solve again after a failed check",
    "bestofn": "Best of several attempts",
}
BACKEND_LABELS = {
    "codex": "Codex CLI",
    "claude": "Claude Code CLI",
    "fake": "Practice run (no model)",
}
# The form's choices for the checks' model service; "" keeps the solver's.
CHECK_BACKEND_LABELS = {"": "Same as the solver", **BACKEND_LABELS}
ROLE_LABELS = {
    "planner": "Planner",
    "solver": "Solver",
    "critic": "Critic",
    "investigator": "Investigator",
    "integrator": "Integrator",
    "alignment_reviewer": "Statement check",
    "proof_reviewer": "Proof check",
}
CALL_STATUS_LABELS = {
    "completed": "Done",
    "timed_out": "Timed out",
    "stopped": "Stopped",
    "failed": "Failed",
    "returned_invalid": "Answer could not be used",
    "returned_invalid_model": "Answer could not be used",
}
SEVERITY_LABELS = {
    "fatal": "Fatal",
    "material": "Serious",
    "minor": "Minor",
}
KIND_LABELS = {
    "goal_gap": "Does not reach the stated goal",
    "invalid_step": "Invalid step",
    "missing_case": "Missing case",
    "assumption_mismatch": "Uses an assumption the problem does not give",
    "dependency_interface": "Parts of the proof do not fit together",
    "ambiguity": "Ambiguous or unclear",
}
PHASE_LABELS = {
    "native_prepared": "preparing the request",
    "original_prepared_dispatched": "sent to the model",
    "original_response_observed": "answer received",
    "pending_original_observation": "waiting for the model",
    "native_handoff": "choosing the next step",
    "native_endpoint": "finished",
    "call_timed_out": "reached its time limit; sending it again",
}
STEP_LABELS = {
    "propose_plan": "Plan", "replan": "Revised plan", "solve_obligation": "Solve",
    "repair_obligation": "Repair", "integrate": "Integrate", "critique_candidate": "Critique",
    "investigate": "Investigate", "alignment_review": "Statement check",
    "whole_proof_review": "Proof check", "compare_candidates": "Compare drafts",
    STOP_REASON: "Stop request",
}
# Endpoint reasons are "<policy>_<suffix>"; the full workflow uses controller codes.
REASON_TEXTS = {
    "complete": "Both checks passed on the same final version of the proof.",
    "review_not_passed": "At least one of the two checks did not pass.",
    "deadline": "The run reached its time limit before both checks passed.",
    "budget": "The run used its whole call budget before both checks passed.",
    "call_failed": "A model call failed, for example it timed out or returned an unusable answer.",
    "solve_failed": "The solving call failed, for example it timed out or returned an unusable answer.",
    "repair_failed": "A repair call failed, for example it timed out or returned an unusable answer.",
    "unresolved": "Problems reported by the checks remained after the allowed repair rounds.",
    "check_incomplete": ("A check returned no usable report, and repeating it once (when calls remained) "
                         "did not give one either."),
    "check_inconclusive": ("A check neither passed nor reported a serious or fatal problem: it gave no clear "
                           "verdict, kept open questions, or said fail with only minor remarks."),
    # Runs made by 0.1.0b2 end with this code where later versions use check_inconclusive.
    "nonblocking_or_unknown": "A check ended with open questions or without a clear verdict.",
    "precheck_unresolved": "The draft still contained placeholders or unfinished parts.",
    "repair_assessment_missing": "A repair answer did not assess every reported problem.",
    "escalated": "The run switched to the full workflow.",
    "no_legal_bounded_completion_action": "The full workflow had no allowed next step within its limits.",
    "authoritative_completion_reserve": "The remaining budget was too small to finish the final checks.",
    STOP_REASON: ("You stopped the run. It sent no new model call, abandoned the call in progress and kept the "
                  "best draft."),
}
UNDECIDED_LABELS = {
    "abstain": "Abstain",
    "repair": "Repair once",
}
# The minor_findings setting: how a check that says pass with only minor findings counts.
MINOR_FINDINGS_LABELS = {
    "nonblocking": "Count it as passed",
    "blocking": "Count it as not passed",
}
# What a check that neither passed nor reported a blocking problem said, by its outcome.
UNDECIDED_OUTCOMES = {
    "unknown": "gave no clear verdict",
    "pass": "said pass but kept open questions",
    "fail": "said fail but reported no serious or fatal problem",
}
_PROGRESS = re.compile(r"^(\w+) \| (\w+) \| (\d+)/(\d+) \| (\w+)\s*$")
_POLICY_PREFIXES = ("single_", "ladder_", "resample_", "bestofn_")
# The policies whose runs use the undecided_checks setting.
_UNDECIDED_POLICIES = ("ladder", "resample")


class FileCache:
    """Parsed run files keyed by path, reread only when size or modification time change."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, path, loader):
        try:
            info = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        key = (info.st_mtime_ns, info.st_size)
        with self._lock:
            entry = self._entries.get(path)
        if entry is not None and entry[0] == key:
            return entry[1]
        try:
            value = loader(path)
        except (OSError, ValueError):
            return entry[1] if entry is not None else None
        with self._lock:
            self._entries[path] = (key, value)
        return value


def _json(path):
    return shared_read.read_json(path, limit=JSON_LIMIT)


def _jsonl(path):
    return shared_read.read_jsonl(path, limit=JSON_LIMIT)


def _text(path):
    return shared_read.read_text(path, limit=TEXT_LIMIT, errors="replace")


def _log_tail(path):
    return shared_read.read_tail(path, LOG_TAIL_BYTES).decode("utf-8", "replace")


def _config_problem(path):
    content = _dict(_dict(_dict(_json(path)).get("manifest")).get("problem")).get("content")
    return content if isinstance(content, str) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def reason_text(code):
    """A plain sentence for an endpoint reason code."""
    if not isinstance(code, str) or not code:
        return None
    suffix = next((code[len(prefix):] for prefix in _POLICY_PREFIXES if code.startswith(prefix)), code)
    return REASON_TEXTS.get(suffix, "The run ended with reason code " + code + ".")


def deadline(invocation):
    """The run's own deadline from its invocation record, if it can be computed."""
    invocation = _dict(invocation)
    parameters = _dict(invocation.get("parameters"))
    started = _utc(invocation.get("started_at"))
    hours = parameters.get("hours")
    if started is None or type(hours) not in (int, float):
        return None
    try:
        result = started + timedelta(hours=hours)
    except OverflowError:
        return None
    explicit = _utc(parameters.get("deadline_utc"))
    return min(result, explicit) if explicit is not None else result


def run_names(runs_dir):
    """Names of run folders in ``runs_dir`` and of submissions this interface recorded."""
    names = set()
    for root, marker in ((Path(runs_dir), "invocation.json"), (Path(runs_dir) / GUI_FOLDER, "request.json")):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                if (NAME.match(entry.name) and entry.is_dir(follow_symlinks=False)
                        and os.path.isfile(os.path.join(entry.path, marker))):
                    names.add(entry.name)
            except OSError:
                continue
    return names


def console_lines(text):
    """The last lines of a run's console output."""
    return text.splitlines()[-LOG_TAIL_LINES:] if text else []


def current_step(lines):
    """The run's latest progress line, in words."""
    for line in reversed(lines):
        match = _PROGRESS.match(line)
        if match:
            kind, role, used, limit, phase = match.groups()
            step = STEP_LABELS.get(kind, "Controller" if role == "controller" else kind.replace("_", " "))
            return {"step": step, "phase": PHASE_LABELS.get(phase, phase.replace("_", " ")),
                    "calls_used": int(used), "calls_limit": int(limit)}
    return None


def failure_message(lines):
    """The error the command printed before it stopped, if any."""
    for line in reversed(lines):
        if line.startswith("victorique: "):
            return line[len("victorique: "):].strip()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("Traceback (most recent call last)"):
            tail = [line for line in lines[index:] if line.strip()]
            return tail[-1].strip() if tail else None
    return None


def _tokens(value):
    value = _dict(value)
    known = {key: value.get(key) for key in ("input_tokens_including_cache", "input_tokens", "output_tokens",
                                             "reasoning_output_tokens", "cached_input_tokens")
             if type(value.get(key)) is int}
    if not known:
        return None
    return {"input": known.get("input_tokens_including_cache", known.get("input_tokens")),
            "output": known.get("output_tokens"), "reasoning": known.get("reasoning_output_tokens"),
            "cached_input": known.get("cached_input_tokens")}


def _sum_tokens(rows):
    totals = None
    for row in rows:
        tokens = _tokens(row.get("tokens"))
        if tokens is None:
            continue
        totals = totals or {"input": 0, "output": 0}
        for key in ("input", "output"):
            if type(tokens.get(key)) is int:
                totals[key] += tokens[key]
    return totals


def _finding(item):
    item = _dict(item)
    severity = item.get("severity")
    kind = item.get("kind")
    location = _dict(item.get("location"))
    quote = location.get("quote") if isinstance(location.get("quote"), str) else None
    if quote is not None and len(quote) > 1200:
        quote = quote[:1200] + " ..."
    return {"severity": severity if isinstance(severity, str) else None,
            "severity_label": SEVERITY_LABELS.get(severity, "Unrated"),
            "blocking": severity != "minor",
            "kind": kind if isinstance(kind, str) else None,
            "kind_label": KIND_LABELS.get(kind, kind.replace("_", " ") if isinstance(kind, str) else "Finding"),
            "claim": item.get("claim") if isinstance(item.get("claim"), str) else None,
            "quote": quote}


def review(output, minor_findings=None):
    """A check or critique report, with its findings and open questions.

    ``minor_findings`` is the run's recorded setting; runs recorded without it
    are read as nonblocking.
    """
    output = _dict(output)
    if not output:
        return None
    findings = [_finding(item) for item in _list(output.get("findings"))]
    questions = [str(item) for item in _list(output.get("uncertainties"))]
    if output.get("schema") == "victorique.adaptive_gate.v1":
        rule = minor_findings if isinstance(minor_findings, str) and minor_findings in MINOR_FINDINGS_LABELS \
            else "nonblocking"
        try:
            passed = bool(gate_passes(output, rule))
            # Under blocking, the minor findings of a check that says pass block it.
            minor_blocks = not passed and bool(gate_passes(output))
        except (KeyError, TypeError, AttributeError):
            passed, minor_blocks = None, False
        for item in findings:
            item["blocking"] = item["blocking"] or minor_blocks
        # Undecided: neither a pass nor a serious or fatal finding (the run's inconclusive check).
        undecided = passed is False and not any(item["blocking"] for item in findings)
        return {"type": "check", "gate": output.get("gate_id"), "outcome": output.get("outcome"),
                "passed": passed, "undecided": undecided, "findings": findings, "questions": questions}
    if output.get("schema") == "victorique.adaptive_critique.v1":
        return {"type": "critique", "passed": not any(item["blocking"] for item in findings),
                "findings": findings, "questions": questions}
    return {"type": "other", "passed": None, "findings": findings, "questions": questions,
            "score": output.get("score")}


def _note(request_id):
    parts = request_id.split(".")
    notes = []
    if parts and parts[0] == "L3":
        notes.append("after switching to the full workflow")
    if "repair" in parts and parts[-1].isdigit():
        notes.append("repair round " + parts[-1])
    elif len(parts) >= 3 and parts[1] == "L1" and parts[2].isdigit():
        notes.append(("fresh attempt " if parts[0] == "resample" else "after repair round ") + parts[2])
    elif len(parts) >= 2 and parts[1] == "L0":
        notes.append("first attempt")
    timeout = re.fullmatch(r"timeout-retry-(\d+)", parts[-1]) if parts else None
    if timeout:
        notes.append("sent again after a timeout" + ("" if timeout.group(1) == "1" else " (" + timeout.group(1) + ")"))
        parts = parts[:-1]
    if parts and parts[-1] == "retry":
        notes.append("repeated once after a failed call")
    if "investigate" in parts:
        notes.append("checks whether a reported problem is real")
    return ", ".join(notes) or None


def _count(number, word):
    return f"{number} {word}" + ("" if number == 1 else "s")


def inconclusive_check(item):
    """One entry of summary.json ``inconclusive_checks``, with a sentence in words."""
    item = _dict(item)
    request_id = str(item.get("request_id") or "")
    check = item.get("check")
    outcome = item.get("outcome")
    doubts, minor = item.get("doubts"), item.get("minor_findings")
    step = STEP_LABELS.get(check, "A check") if isinstance(check, str) else "A check"
    note = _note(request_id)
    counts = [_count(value, word) for value, word in ((doubts, "open question"), (minor, "minor remark"))
              if type(value) is int]
    said = UNDECIDED_OUTCOMES.get(outcome) if isinstance(outcome, str) else None
    text = (step + (f" ({note})" if note else "") + " " + (said or "ended without a clear verdict")
            + (" (" + ", ".join(counts) + ")" if counts else "") + ".")
    return {"request_id": request_id, "check": check, "step": step, "note": note, "outcome": outcome,
            "doubts": doubts, "minor_findings": minor, "text": text}


def call_view(row, index, running, minor_findings=None):
    request_id = str(row.get("request_id") or "")
    role = row.get("role")
    status = row.get("status")
    started, ended = _utc(row.get("started_at_utc")), _utc(row.get("ended_at_utc"))
    if status == "unknown":
        status_label = "In progress" if running else "No answer recorded"
    else:
        status_label = CALL_STATUS_LABELS.get(status, str(status).replace("_", " "))
    return {"index": index, "request_id": request_id, "role": role,
            "step": ROLE_LABELS.get(role, str(role).replace("_", " ")), "note": _note(request_id),
            "status": status, "status_label": status_label,
            "started_at": row.get("started_at_utc"), "ended_at": row.get("ended_at_utc"),
            "seconds": round((ended - started).total_seconds(), 1) if started and ended else None,
            "model": row.get("model"), "effort": row.get("effort"), "tokens": _tokens(row.get("tokens")),
            # "checks" when the call went to the run's separate check service.
            "service": row.get("service", "solver"), "retry_of": row.get("retry_of"),
            "review": review(row.get("review_output"), minor_findings)}


class RunReader:
    """Builds list and detail views of the runs in one runs folder."""

    def __init__(self, runs_dir):
        self.runs_dir = Path(runs_dir)
        self.inputs = self.runs_dir / GUI_FOLDER
        self.cache = FileCache()

    def _paths(self, name):
        return self.runs_dir / name, self.inputs / name

    def _problem(self, run_dir, gui_dir):
        text = self.cache.get(str(gui_dir / "problem.md"), _text)
        if text is not None:
            return text
        for candidate in (run_dir / "prepared" / "config.json", run_dir / "state" / "run-config.json"):
            content = self.cache.get(str(candidate), _config_problem)
            if content is not None:
                return content
        return None

    @staticmethod
    def _title(request, problem, name):
        title = _dict(request).get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        for line in (problem or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line if len(line) <= 90 else line[:87] + "..."
        return name

    def entry(self, name, liveness=None, now=None):
        """The list view of one run.

        ``liveness(name, request)`` returns True or False when the run's process
        is known to be running or ended, and None when that is unknown.
        """
        now = now or datetime.now(timezone.utc)
        run_dir, gui_dir = self._paths(name)
        request = self.cache.get(str(gui_dir / "request.json"), _json)
        exit_info = self.cache.get(str(gui_dir / "exit.json"), _json)
        invocation = self.cache.get(str(run_dir / "invocation.json"), _json)
        summary = self.cache.get(str(run_dir / "summary.json"), _json)
        lines = console_lines(self.cache.get(str(gui_dir / "console.log"), _log_tail))
        alive = False if exit_info is not None else liveness(name, request) if liveness else None
        problem = self._problem(run_dir, gui_dir)
        parameters = _dict(_dict(invocation).get("parameters"))
        form = _dict(_dict(request).get("form"))
        ends = deadline(invocation)
        # The stop request that victorique stop or the Stop run button wrote in the run folder.
        stop_requested = os.path.lexists(run_dir / STOP_FILE)
        stop_request = self.cache.get(str(run_dir / STOP_FILE), _json) if stop_requested else None
        reason = None
        if alive:
            status = "running" if invocation is not None else "starting"
        elif isinstance(summary, dict):
            status = {"complete": "delivered", "abstain": "abstained", "partial": "failed"}.get(
                summary.get("outcome"), "interrupted")
            if status == "failed" and summary.get("endpoint_reason") == STOP_REASON:
                status = "stopped"
            reason = reason_text(summary.get("endpoint_reason"))
            if status == "interrupted":
                reason = "The run was interrupted before it reached a decision."
            elif status == "failed" and reason is None:
                reason = "The run stopped before it reached a decision."
        elif alive is False:
            message = failure_message(lines)
            if message is not None or invocation is None:
                status = "failed"
                reason = message or "The command ended before it created the run."
            else:
                status = "interrupted"
                reason = "The run stopped before it finished and wrote no summary."
        elif ends is not None and now > ends + DEADLINE_GRACE:
            status = "interrupted"
            reason = "The run passed its time limit without writing a summary."
        else:
            status = "running" if invocation is not None else "starting"
        if status == "running" and stop_requested:
            status = "stopping"
        # A run whose process ended without a summary can be resumed; with a stop request in its
        # folder the resumed run ends at once, without a new model call.
        can_resume = status in ("interrupted", "failed") and invocation is not None and not isinstance(summary, dict)
        if can_resume and stop_requested:
            reason += " A stop was requested: resuming ends the run without a new model call."
        rows = None
        if not isinstance(summary, dict) or status in ACTIVE:
            rows = self.cache.get(str(run_dir / "calls.jsonl"), _jsonl) or []
        if isinstance(summary, dict) and status not in ACTIVE:
            calls_used = summary.get("calls_used")
            tokens = _tokens(summary.get("tokens"))
        else:
            calls_used = len(rows or [])
            tokens = _sum_tokens(rows or [])
        progress = current_step(lines)
        if progress is not None and status in ACTIVE:
            calls_used = max(calls_used or 0, progress["calls_used"])
        policy = parameters.get("policy", form.get("policy"))
        backend = parameters.get("transport", form.get("backend"))
        # Runs made before these settings existed record neither value.
        call_timeout = parameters.get("call_timeout_seconds", form.get("call_timeout"))
        undecided = parameters.get("undecided_checks", form.get("undecided_checks"))
        if policy not in _UNDECIDED_POLICIES or not isinstance(undecided, str) or undecided not in UNDECIDED_LABELS:
            undecided = None
        minor = parameters.get("minor_findings", form.get("minor_findings"))
        if not isinstance(minor, str) or minor not in MINOR_FINDINGS_LABELS:
            minor = None
        # The checks' model service, shown only when it differs from the solver's (older runs record none).
        check_service = _dict(summary).get("check_service")
        check_backend = parameters.get("check_transport")
        separate = isinstance(check_service, dict) or (
            isinstance(check_backend, str) and (check_backend, parameters.get("check_model"),
                                                parameters.get("check_reasoning_effort")) != (
                backend, parameters.get("model"), parameters.get("reasoning_effort")))
        retries = parameters.get("timeout_retries", form.get("timeout_retries"))
        retries_used = _dict(summary).get("timeout_retries_used")
        if type(retries_used) is not int:
            retries_used = sum(1 for row in rows or [] if isinstance(row, dict) and row.get("retry_of"))
        return {
            "name": name, "title": self._title(request, problem, name),
            "status": status, "status_label": STATUS_LABELS[status], "finished": status in FINISHED,
            "reason": reason, "policy": policy, "policy_label": POLICY_LABELS.get(policy, policy),
            "backend": backend, "backend_label": BACKEND_LABELS.get(backend, backend),
            "model": parameters.get("model", form.get("model")),
            "reported_model": _dict(summary).get("reported_model"),
            "reasoning_effort": parameters.get("reasoning_effort", form.get("reasoning_effort")),
            "call_timeout_seconds": call_timeout if type(call_timeout) in (int, float) else None,
            "undecided_checks": undecided, "undecided_checks_label": UNDECIDED_LABELS.get(undecided),
            "minor_findings": minor, "minor_findings_label": MINOR_FINDINGS_LABELS.get(minor),
            "timeout_retries": retries if type(retries) is int else None,
            "timeout_retries_used": retries_used if type(retries) is int else None,
            "check_separate": separate,
            "check_backend": check_backend if separate else None,
            "check_backend_label": BACKEND_LABELS.get(check_backend, check_backend) if separate else None,
            "check_model": parameters.get("check_model") if separate else None,
            "check_reported_model": _dict(check_service).get("reported_model") if separate else None,
            "check_reasoning_effort": parameters.get("check_reasoning_effort") if separate else None,
            "started_at": _dict(invocation).get("started_at") or _dict(request).get("created_at"),
            "deadline": ends.isoformat() if ends is not None else None,
            "calls_used": calls_used,
            "calls_limit": _dict(summary).get("calls_limit", parameters.get("calls", form.get("calls"))),
            "tokens": tokens, "wall_seconds": _dict(summary).get("wall_seconds"),
            "started_here": request is not None, "progress": progress if status in ACTIVE else None,
            "stop_requested": stop_requested, "stop_requested_at": _dict(stop_request).get("requested_at"),
            # The Stop run button: a running run whose folder holds no stop request yet.
            "can_stop": status == "running",
            "can_resume": can_resume,
            "_internal": {"summary": summary, "invocation": invocation, "request": request, "rows": rows,
                          "lines": lines, "problem": problem},
        }

    def detail(self, name, liveness=None):
        """The detail view of one run."""
        run_dir, gui_dir = self._paths(name)
        entry = self.entry(name, liveness)
        internal = entry.pop("_internal")
        summary = _dict(internal["summary"])
        running = entry["status"] in ACTIVE
        rows = internal["rows"]
        if rows is None:
            rows = self.cache.get(str(run_dir / "calls.jsonl"), _jsonl) or []
        calls = [call_view(row, index, running, entry["minor_findings"]) for index, row in enumerate(rows, 1)]
        proof = None
        if summary.get("proof_available") and entry["status"] in FINISHED:
            proof = self.cache.get(str(run_dir / "proof.md"), _text)
        abstention = None
        if entry["status"] in ("abstained", "failed", "interrupted", "stopped"):
            latest = {}
            for call in calls:
                if call["review"] is not None and call["review"]["type"] == "check":
                    latest[call["role"]] = call
            abstention = {
                "reason_code": summary.get("endpoint_reason"), "reason": entry["reason"],
                "unresolved_issues": [_finding(item) for item in _list(summary.get("unresolved_issues"))],
                "unresolved_questions": [str(item) for item in _list(summary.get("unresolved_questions"))],
                "incomplete_checks": [str(_dict(item).get("request_id", item)) for item in
                                      _list(summary.get("incomplete_checks"))],
                # Absent in summaries written by 0.1.0b2.
                "inconclusive_checks": [inconclusive_check(item) for item in
                                        _list(summary.get("inconclusive_checks"))],
                "minor_findings": [_finding(item) for item in _list(summary.get("non_blocking"))],
                "failed_checks": [call for call in latest.values() if call["review"]["passed"] is False],
                "checks_not_run": [ROLE_LABELS[role] for role in ("alignment_reviewer", "proof_reviewer")
                                   if not any(call["role"] == role for call in calls)],
            }
        files = []
        for directory, names, source in ((run_dir, RUN_FILES, "run"), (gui_dir, GUI_FILES, "interface")):
            for file_name in names:
                try:
                    size = os.stat(directory / file_name).st_size
                except OSError:
                    continue
                files.append({"name": file_name, "size": size, "source": source})
        entry.update({
            "problem": internal["problem"],
            "run_directory": str(run_dir) if run_dir.is_dir() else None,
            "parameters": _dict(internal["invocation"]).get("parameters"),
            "endpoint_reason": summary.get("endpoint_reason"),
            "proof": proof,
            "proof_kind": None if proof is None else "delivered" if entry["status"] == "delivered" else "draft",
            "calls": calls,
            "checks": [call for call in calls if call["review"] is not None],
            "abstention": abstention,
            "files": files,
            "archive_available": run_dir.is_dir() and entry["finished"],
            "console": internal["lines"],
        })
        return entry

    def listing(self, liveness=None):
        """List views of every run, newest first."""
        rows = []
        for name in self.run_names():
            row = self.entry(name, liveness)
            row.pop("_internal")
            rows.append(row)
        rows.sort(key=lambda row: (row["started_at"] or "", row["name"]), reverse=True)
        return rows

    def run_names(self):
        return run_names(self.runs_dir)

    def file_path(self, name, file_name):
        """The path of a downloadable file of a run, or None."""
        run_dir, gui_dir = self._paths(name)
        if file_name in RUN_FILES:
            path = run_dir / file_name
        elif file_name in GUI_FILES:
            path = gui_dir / file_name
        else:
            return None
        try:
            info = os.lstat(path)
        except OSError:
            return None
        return path if stat.S_ISREG(info.st_mode) else None
