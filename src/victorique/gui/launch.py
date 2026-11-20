"""Start runs through the public ``victorique solve`` command and follow their processes.

The interface writes the problem statement to a file, runs the ordinary
command in a separate process and records that process. Choosing actions,
budgets and checks stays with the command itself.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

from ..settings import CONFIG_ENVIRONMENT
from .runs import GUI_FOLDER

_WINDOWS = os.name == "nt"
_OPTION = re.compile(r"--[a-z][a-z0-9-]*")
# Options of ``victorique solve`` that the form uses when the installed command lists them.
CALL_TIMEOUT_OPTION = "--call-timeout-seconds"
UNDECIDED_CHECKS_OPTION = "--undecided-checks"
MINOR_FINDINGS_OPTION = "--minor-findings"
TIMEOUT_RETRIES_OPTION = "--timeout-retries"
CHECK_OPTIONS = {"transport": "--check-transport", "model": "--check-model", "effort": "--check-reasoning-effort"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def python_command(python):
    """The interpreter and flags for every child ``python -m victorique`` call.

    ``-P`` leaves the working directory off the module search path, so a
    ``victorique`` folder or file there cannot take the place of the installed
    package. Isolated mode (``-I``) carries over from this process.
    """
    return [python, "-I", "-P"] if sys.flags.isolated else [python, "-P"]


def write_json(path, value):
    """Write a small JSON file atomically, retrying while a reader briefly holds the target."""
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    temporary.write_bytes(data)
    try:
        for attempt in range(100):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # A classic replace on Windows fails while any handle is open on the target.
                if attempt == 99:
                    raise
                time.sleep(0.02)
    finally:
        if temporary.exists():
            temporary.unlink()


def solve_options(python, cwd, env=None):
    """Option names that ``victorique solve --help`` lists, read from the installed command.

    ``cwd`` is the folder the runs start in, so the help comes from the same
    command and folder as a run.
    """
    try:
        result = subprocess.run([*python_command(python), "-m", "victorique", "solve", "--help"],
                                cwd=str(cwd), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=60, env=env, creationflags=_no_window())
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(_OPTION.findall(result.stdout)) if result.returncode == 0 else frozenset()


def call_timeout_option(options):
    """The solve option for a per-call time limit.

    ``--call-timeout-seconds`` when the installed command lists it; otherwise the
    one listed option whose name contains ``timeout``, if there is exactly one.
    """
    if CALL_TIMEOUT_OPTION in options:
        return CALL_TIMEOUT_OPTION
    matches = sorted(option for option in options if "timeout" in option)
    return matches[0] if len(matches) == 1 else None


def undecided_checks_option(options):
    """``--undecided-checks`` when the installed command lists it, else None."""
    return UNDECIDED_CHECKS_OPTION if UNDECIDED_CHECKS_OPTION in options else None


def minor_findings_option(options):
    """``--minor-findings`` when the installed command lists it, else None."""
    return MINOR_FINDINGS_OPTION if MINOR_FINDINGS_OPTION in options else None


def timeout_retries_option(options):
    """``--timeout-retries`` when the installed command lists it, else None."""
    return TIMEOUT_RETRIES_OPTION if TIMEOUT_RETRIES_OPTION in options else None


def check_options(options):
    """The three check service options when the installed command lists all of them, else None."""
    return dict(CHECK_OPTIONS) if all(option in options for option in CHECK_OPTIONS.values()) else None


def solve_arguments(problem, run_directory, values, timeout_option=None, undecided_option=None, minor_option=None, *,
                    retries_option=None, check_options=None):
    """Arguments of ``victorique solve`` for one form submission."""
    arguments = ["solve", str(problem), "--policy", values["policy"], "--transport", values["backend"],
                 "--calls", str(values["calls"]), "--hours", repr(float(values["hours"])),
                 "--out", str(run_directory)]
    if values.get("model"):
        arguments += ["--model", values["model"]]
    if values.get("reasoning_effort"):
        arguments += ["--reasoning-effort", values["reasoning_effort"]]
    if values.get("call_timeout") is not None:
        if timeout_option is None:
            raise ValueError("this version of victorique solve has no per-call time limit option")
        arguments += [timeout_option, repr(float(values["call_timeout"]))]
    if values.get("undecided_checks") is not None:
        if undecided_option is None:
            raise ValueError("this version of victorique solve has no option for undecided checks")
        arguments += [undecided_option, values["undecided_checks"]]
    if values.get("minor_findings") is not None:
        if minor_option is None:
            raise ValueError("this version of victorique solve has no option for minor findings")
        arguments += [minor_option, values["minor_findings"]]
    checks = [(key, values.get(field)) for key, field in (("transport", "check_backend"), ("model", "check_model"),
                                                          ("effort", "check_reasoning_effort"))]
    if any(value for _, value in checks):
        if check_options is None:
            raise ValueError("this version of victorique solve has no check service options")
        arguments += [part for key, value in checks if value for part in (check_options[key], value)]
    if values.get("timeout_retries") is not None:
        if retries_option is None:
            raise ValueError("this version of victorique solve has no option for retries after a timeout")
        arguments += [retries_option, str(values["timeout_retries"])]
    return arguments


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WINDOWS else 0


if _WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _open_process = _kernel32.OpenProcess
    _open_process.restype = wintypes.HANDLE
    _open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _exit_code = _kernel32.GetExitCodeProcess
    _exit_code.restype = wintypes.BOOL
    _exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _process_times = _kernel32.GetProcessTimes
    _process_times.restype = wintypes.BOOL
    _process_times.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _close_handle = _kernel32.CloseHandle
    _close_handle.restype = wintypes.BOOL
    _close_handle.argtypes = (wintypes.HANDLE,)

    def process_running(pid, not_before, not_after, marker=None):
        """True or False when the recorded process is or is not running; None when unknown."""
        handle = _open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None if ctypes.get_last_error() == 5 else False
        try:
            code = wintypes.DWORD()
            if not _exit_code(handle, ctypes.byref(code)):
                return None
            if code.value != 259:  # STILL_ACTIVE
                return False
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _process_times(handle, *(ctypes.byref(item) for item in times)):
                return None
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created = ticks / 1e7 - 11644473600
            # A later process can reuse the number; compare creation times.
            return not_before - 2 <= created <= not_after + 2
        finally:
            _close_handle(handle)
else:
    def process_running(pid, not_before, not_after, marker=None):
        """True or False when the recorded process is or is not running; None when unknown."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        try:
            command = Path("/proc", str(pid), "cmdline").read_bytes()
        except OSError:
            return True
        return marker is None or os.fsencode(marker) in command


class Launcher:
    """Starts ``victorique solve`` processes for one runs folder and follows them."""

    def __init__(self, runs_dir, *, python=None, env=None):
        self.runs_dir = Path(runs_dir)
        self.inputs = self.runs_dir / GUI_FOLDER
        self.python = python or sys.executable
        self.env = dict(env or {})
        # The folder the interface started in; a relative settings path names a file there.
        self.start_dir = os.getcwd()
        self._lock = threading.Lock()
        self._processes = {}

    def environment(self):
        """The environment of every command started here.

        The commands run in the runs folder, so a relative ``VICTORIQUE_CONFIG``
        becomes an absolute path in the folder where the interface started.
        """
        environment = {**os.environ, **self.env, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        config = environment.get(CONFIG_ENVIRONMENT)
        if config:
            environment[CONFIG_ENVIRONMENT] = os.path.abspath(
                os.path.join(self.start_dir, os.path.expanduser(config)))
        return environment

    def _new_name(self):
        while True:
            name = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S-") + secrets.token_hex(3)
            if not (self.runs_dir / name).exists() and not (self.inputs / name).exists():
                return name

    def start(self, statement, title, values, arguments_for):
        """Write the problem, start the command and return the run's name.

        ``arguments_for(problem_path, run_directory)`` builds the command's arguments.
        """
        with self._lock:
            name = self._new_name()
            folder = self.inputs / name
            problem = folder / "problem.md"
            run_directory = self.runs_dir / name
            arguments = arguments_for(problem, run_directory)
            folder.mkdir(parents=True)
        problem.write_bytes(statement.encode("utf-8"))
        request = {"schema": "victorique.gui_request.v1", "name": name, "title": title or None,
                   "created_at": utc_now(), "form": values, "command": ["victorique", *arguments],
                   "run_directory": str(run_directory), "pid": None}
        write_json(folder / "request.json", request)
        options = {"creationflags": _no_window()} if _WINDOWS else {"start_new_session": True}
        not_before = time.time()
        with open(folder / "console.log", "wb") as log:
            try:
                process = subprocess.Popen([*python_command(self.python), "-m", "victorique", *arguments],
                                           cwd=str(self.runs_dir),
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           env=self.environment(), **options)
            except OSError as error:
                log.write(("victorique: could not start the command: " + str(error) + "\n").encode("utf-8"))
                write_json(folder / "exit.json", {"exit_code": None, "ended_at": utc_now(),
                                                   "error": str(error)})
                return name
        not_after = time.time()
        with self._lock:
            self._processes[name] = process
        write_json(folder / "request.json", {**request, "pid": process.pid,
                                             "process_started": [not_before, not_after]})
        threading.Thread(target=self._wait, args=(name, process, folder), daemon=True,
                         name="victorique-gui-wait-" + name).start()
        return name

    def resume(self, name, run_directory):
        """Start ``victorique resume`` for a run whose process ended without a summary.

        The command starts as a form's solve command does: ``python -P`` in a
        separate process with the runs folder as working directory. Its output is
        added to the run's ``console.log``; ``request.json`` records the new
        process, and the earlier ``exit.json`` is removed, so the page follows the
        run like a running run.
        """
        folder = self.inputs / name
        arguments = ["resume", str(run_directory)]
        with self._lock:
            process = self._processes.get(name)
            if process is not None and process.poll() is None:
                raise ValueError("The run's process is still running.")
            folder.mkdir(parents=True, exist_ok=True)
            try:
                request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = None
            if not isinstance(request, dict):
                # A run started with victorique solve in a terminal has no record of the interface yet.
                request = {"schema": "victorique.gui_request.v1", "name": name, "title": None, "created_at": utc_now(),
                           "form": None, "command": None, "run_directory": str(run_directory)}
            request.update(resume_command=["victorique", *arguments], pid=None, process_started=None)
            write_json(folder / "request.json", request)
            try:
                (folder / "exit.json").unlink()
            except FileNotFoundError:
                pass
            options = {"creationflags": _no_window()} if _WINDOWS else {"start_new_session": True}
            not_before = time.time()
            with open(folder / "console.log", "ab") as log:
                log.write(("\nvictorique resume started from the interface at " + utc_now() + "\n").encode("utf-8"))
                log.flush()
                try:
                    process = subprocess.Popen([*python_command(self.python), "-m", "victorique", *arguments],
                                               cwd=str(self.runs_dir), stdin=subprocess.DEVNULL, stdout=log,
                                               stderr=subprocess.STDOUT, env=self.environment(), **options)
                except OSError as error:
                    log.write(("victorique: could not start the command: " + str(error) + "\n").encode("utf-8"))
                    write_json(folder / "exit.json", {"exit_code": None, "ended_at": utc_now(), "error": str(error)})
                    return name
            not_after = time.time()
            self._processes[name] = process
        write_json(folder / "request.json", {**request, "pid": process.pid, "process_started": [not_before, not_after]})
        threading.Thread(target=self._wait, args=(name, process, folder), daemon=True,
                         name="victorique-gui-wait-" + name).start()
        return name

    def _wait(self, name, process, folder):
        code = process.wait()
        try:
            write_json(folder / "exit.json", {"exit_code": code, "ended_at": utc_now()})
        except OSError:
            pass

    def liveness(self, name, request):
        """Whether the run's process is running: True, False, or None when unknown."""
        with self._lock:
            process = self._processes.get(name)
        if process is not None:
            return process.poll() is None
        request = request if isinstance(request, dict) else {}
        pid, window = request.get("pid"), request.get("process_started")
        if type(pid) is not int or not isinstance(window, list) or len(window) != 2:
            return None
        if not all(type(value) in (int, float) for value in window):
            return None
        return process_running(pid, window[0], window[1], request.get("run_directory"))

    def running(self):
        with self._lock:
            return [name for name, process in self._processes.items() if process.poll() is None]

    def wait(self, timeout=None):
        """Wait for the processes started here; used by tests and on shutdown."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            try:
                process.wait(remaining)
            except subprocess.TimeoutExpired:
                return False
        return True
