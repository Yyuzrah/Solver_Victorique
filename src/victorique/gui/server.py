"""A small local HTTP server for starting runs and reading their results.

Runs are started only through the public ``victorique solve`` command. Pages
and downloads read the run folders with ``victorique.shared_read``.
"""
import errno
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import socketserver
import stat
import sys
import tempfile
import threading
from urllib.parse import quote, unquote, urlsplit
import webbrowser
import zipfile

from .. import __version__, settings, shared_read, stop_request
from ..harness.codex_contract import REASONING_EFFORTS
from . import launch, runs

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Without --port the interface tries 8765, then these ports, then a port the system assigns.
FALLBACK_PORTS = tuple(range(DEFAULT_PORT + 1, 8786))
# Bind errors that mean another program holds the port or the system reserves it
# (Windows reports a port in an excluded range as access denied).
_PORT_TAKEN = {errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE),
               getattr(errno, "WSAEACCES", errno.EACCES)}
MAX_BODY = 1024 * 1024
PROBLEM_LIMIT = 256000
DOWNLOAD_LIMIT = 64 * 1024 * 1024
ARCHIVE_LIMIT = 1024 * 1024 * 1024
# The per-call limit of a solve command without a per-call time limit option.
FIXED_CALL_SECONDS = 600
# The first approach is the default and is listed first on the form.
POLICIES = ("single", "ladder", "full")
BACKENDS = ("codex", "claude", "fake")
# Model services of the two checks; "" keeps the solver's service.
CHECK_BACKENDS = ("", *BACKENDS)
# The first choice is the default; of the approaches on the form only ladder uses the choice.
UNDECIDED_CHOICES = settings.UNDECIDED_CHECKS
# The first choice is the default; every approach uses the choice.
MINOR_CHOICES = settings.MINOR_FINDINGS
# The page's fonts, one WOFF2 file per Google Fonts subset, and their SIL Open Font License texts.
FONT_FILES = ("michroma-400-latin.woff2", "michroma-400-latin-ext.woff2",
              "barlowsemicondensed-400-latin.woff2", "barlowsemicondensed-400-latin-ext.woff2",
              "barlowsemicondensed-500-latin.woff2", "barlowsemicondensed-500-latin-ext.woff2",
              "jetbrainsmono-var-latin.woff2", "jetbrainsmono-var-latin-ext.woff2", "jetbrainsmono-var-greek.woff2",
              "jetbrainsmono-var-math.woff2")
FONT_LICENSES = ("OFL-michroma.txt", "OFL-barlowsemicondensed.txt", "OFL-jetbrainsmono.txt")
STATIC_TYPES = {"app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8",
                **dict.fromkeys(FONT_FILES, "font/woff2"),
                **dict.fromkeys(FONT_LICENSES, "text/plain; charset=utf-8")}
DOWNLOAD_TYPES = {".md": "text/markdown; charset=utf-8", ".json": "application/json; charset=utf-8",
                  ".jsonl": "application/x-ndjson; charset=utf-8", ".log": "text/plain; charset=utf-8"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
}


class FormError(ValueError):
    """A form value that cannot start a run; the message is shown to the user."""


def is_loopback(host):
    """True for addresses that only this computer can reach."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_host(host, allow_remote=False):
    """Refuse a listening address that other computers can reach unless explicitly allowed."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError("--host must name an address")
    if not is_loopback(host) and not allow_remote:
        raise ValueError(
            f"refusing to listen on {host}: anyone who can reach the interface can start runs that use "
            f"your model account, so by default it listens only on this computer ({DEFAULT_HOST}). "
            f"Add --allow-remote to listen on {host} anyway.")


def _integer(value, label, low, high):
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise FormError(f"{label} must be a whole number from {low} to {high}.")
    return value


def _number(value, label, low, high):
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise FormError(f"{label} must be a number.") from None
    if type(value) not in (int, float) or not math.isfinite(value) or not low < value <= high:
        raise FormError(f"{label} must be more than {low:g} and at most {high:g}.")
    return float(value)


def _model_field(value, label):
    value = value or ""
    if not isinstance(value, str):
        raise FormError(f"The {label} name must be text.")
    value = value.strip()
    if value and (value.startswith("-") or any(character.isspace() for character in value)):
        raise FormError(f"The {label} name must be a single word without spaces, or empty for the default.")
    return value or None


def parse_form(body, timeout_option=None, undecided_option=None, undecided_default=None,
               minor_option=None, minor_default=None, *, retries_option=None, retries_default=None,
               check_options=None):
    """Check a submitted form and return the values for ``victorique solve``.

    ``timeout_option``, ``undecided_option``, ``minor_option`` and
    ``retries_option`` are the solve options found in its help, and
    ``check_options`` the three check service options, or None when the
    installed command does not have them. ``undecided_default`` is the choice
    for a ladder run whose form gives none (the settings file's
    ``undecided_checks``); None means abstain. ``minor_default`` is the choice
    for a form without one (the settings file's ``minor_findings``); None means
    nonblocking. ``retries_default`` is the number of retries after a call
    timeout for a form that gives none (the settings file's ``timeout_retries``,
    else 1).
    """
    if not isinstance(body, dict):
        raise FormError("The form could not be read.")
    statement = body.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise FormError("Write or paste the problem statement.")
    if len(statement.encode("utf-8")) > PROBLEM_LIMIT:
        raise FormError(f"The problem statement is longer than {PROBLEM_LIMIT:,} bytes.")
    title = body.get("title") or ""
    if not isinstance(title, str):
        raise FormError("The title must be text.")
    title = " ".join(title.split())[:200]
    policy = body.get("policy") or POLICIES[0]
    if policy not in POLICIES:
        raise FormError("Choose one of the three approaches.")
    backend = body.get("backend") or "codex"
    if backend not in BACKENDS:
        raise FormError("Choose a model service.")
    model = _model_field(body.get("model"), "model")
    effort = body.get("reasoning_effort") or None
    if effort is not None:
        if effort not in REASONING_EFFORTS:
            raise FormError("Choose a reasoning effort from the list.")
        if backend != "codex":
            raise FormError("Reasoning effort can be chosen only with the Codex CLI.")
    calls = _integer(body.get("calls", 12), "Maximum model calls", 2, 100)
    hours = _number(body.get("hours", 2), "Time limit for the whole run (hours)", 0, 168)
    call_timeout = body.get("call_timeout")
    if call_timeout in (None, ""):
        call_timeout = None
    else:
        if timeout_option is None:
            raise FormError(f"This version of Victorique gives every model call up to {FIXED_CALL_SECONDS} "
                            "seconds; leave the per-call time limit empty.")
        low, high = settings.CALL_TIMEOUT_RANGE
        call_timeout = _number(call_timeout, "Time limit per model call (seconds)", 0, high)
        if call_timeout < low:
            raise FormError(f"Time limit per model call (seconds) must be at least {low:g}.")
    undecided = body.get("undecided_checks")
    if not undecided:
        undecided = (undecided_default or UNDECIDED_CHOICES[0]) if policy == "ladder" else UNDECIDED_CHOICES[0]
    if undecided not in UNDECIDED_CHOICES:
        raise FormError("Choose what happens when a check is undecided.")
    if undecided != UNDECIDED_CHOICES[0]:
        if policy != "ladder":
            raise FormError("Repair once can be chosen only with the approach Solve, check and repair (ladder).")
        if undecided_option is None:
            raise FormError("This version of Victorique always stops without a proof when a check is undecided; "
                            "choose Abstain.")
    # Passed only where it has an effect, so it overrides a settings file for ladder runs.
    undecided = undecided if policy == "ladder" and undecided_option is not None else None
    minor = body.get("minor_findings") or minor_default or MINOR_CHOICES[0]
    if minor not in MINOR_CHOICES:
        raise FormError("Choose what happens when a check passes with minor findings.")
    if minor != MINOR_CHOICES[0] and minor_option is None:
        raise FormError("This version of Victorique always counts a check that passes with minor findings "
                        "as passed; choose Count it as passed.")
    # Every approach uses the choice, so it is always passed and overrides a settings file.
    minor = minor if minor_option is not None else None
    check_backend = body.get("check_backend") or ""
    if check_backend not in CHECK_BACKENDS:
        raise FormError("Choose a model service for the checks.")
    check_model = _model_field(body.get("check_model"), "check model")
    check_effort = body.get("check_reasoning_effort") or None
    if (check_backend or check_model or check_effort) and check_options is None:
        raise FormError("This version of Victorique checks with the solver's model service; leave the check fields "
                        "at their defaults.")
    check_service = check_backend or backend
    if (backend == "fake") != (check_service == "fake"):
        raise FormError("A practice run calls no model for solving or for the checks: choose Practice run for both "
                        "model services or for neither.")
    if check_effort is not None:
        if check_effort not in REASONING_EFFORTS:
            raise FormError("Choose a check reasoning effort from the list.")
        if check_service != "codex":
            raise FormError("Check reasoning effort can be chosen only when the checks use the Codex CLI.")
    retries = body.get("timeout_retries")
    if retries in (None, ""):
        retries = retries_default if retries_option is not None else None
    elif retries_option is None:
        raise FormError("This version of Victorique does not repeat a call that reached its time limit; leave the "
                        "retries field empty.")
    else:
        low, high = settings.TIMEOUT_RETRIES_RANGE
        retries = _integer(retries, "Retries after a call timeout", low, high)
    return statement, title, {"policy": policy, "backend": backend, "model": model,
                              "reasoning_effort": effort, "calls": calls, "hours": hours,
                              "call_timeout": call_timeout, "undecided_checks": undecided,
                              "minor_findings": minor,
                              "check_backend": check_backend or None, "check_model": check_model,
                              "check_reasoning_effort": check_effort, "timeout_retries": retries}


def _resource(name):
    return files("victorique.gui").joinpath("static").joinpath(name).read_bytes()


def attachment(filename):
    """A Content-Disposition value that keeps a non-ASCII file name (RFC 6266)."""
    fallback = "".join(character if 32 <= ord(character) < 127 and character not in '"\\' else "_"
                       for character in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


class RunsFolderError(OSError):
    """The runs folder cannot be created or written. The message is one line for the user."""


def _folder_reason(error, folder):
    """The reason of a failed folder check in one line, naming the path when it is not the folder itself."""
    name = None if error.filename is None else Path(os.fsdecode(error.filename))
    if name is not None and name.exists() and not name.is_dir():
        return "it is a file" if name == folder else f"{name} is a file"
    reason = " ".join(str(error.strerror or error).split()).rstrip(".。")
    if name is not None and name != folder and folder.is_relative_to(name):
        reason += f": {name}"
    return reason


def prepare_runs_folder(runs_dir):
    """Create the runs folder when it is missing and check that a new file can be written in it.

    Returns the folder's absolute path. Raises RunsFolderError, whose message names the
    folder, the reason and the option that chooses another folder.
    """
    folder = Path(runs_dir).absolute()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / (".victorique-write-check-" + secrets.token_hex(8))
        # On Windows O_TEMPORARY removes the file when it is closed; elsewhere it is removed below.
        os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_TEMPORARY", 0)))
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
    except OSError as error:
        raise RunsFolderError(f"cannot create or write the runs folder {folder} ({_folder_reason(error, folder)}); "
                              "choose another folder with --runs-dir") from None
    return folder


class GuiApp:
    """State shared by the request handlers: the runs folder, its runs and the page token."""

    def __init__(self, runs_dir, *, env=None, python=None, detect_options=True):
        self.runs_dir = prepare_runs_folder(runs_dir)
        self.reader = runs.RunReader(self.runs_dir)
        self.launcher = launch.Launcher(self.runs_dir, python=python, env=env)
        self.token = secrets.token_urlsafe(32)
        self.allowed_hosts = None
        options = (launch.solve_options(self.launcher.python, self.runs_dir, self.launcher.environment())
                   if detect_options else ())
        self.timeout_option = launch.call_timeout_option(options)
        self.undecided_option = launch.undecided_checks_option(options)
        self.minor_option = launch.minor_findings_option(options)
        self.retries_option = launch.timeout_retries_option(options)
        self.check_options = launch.check_options(options)
        self.static = {name: _resource(name) for name in STATIC_TYPES}
        self.index = (_resource("index.html").replace(b"__VICTORIQUE_TOKEN__", self.token.encode("ascii"))
                      .replace(b"__VICTORIQUE_VERSION__", __version__.encode("ascii")))

    def bind(self, host, port):
        """Accept only Host headers that name this server.

        A server on an unspecified address such as 0.0.0.0 can be reached under any
        of the computer's addresses, so it accepts every Host header.
        """
        if is_loopback(host):
            names = {host, "localhost", "127.0.0.1", "[::1]"}
        else:
            try:
                unspecified = ipaddress.ip_address(host.strip("[]")).is_unspecified
            except ValueError:
                unspecified = False
            if unspecified:
                self.allowed_hosts = None
                return
            names = {host, f"[{host}]"} if ":" in host else {host}
        self.allowed_hosts = {f"{name}:{port}".lower() for name in names}

    def _setting(self, key, builtin, option):
        """A solve setting's value for the form, and where it comes from.

        It is the solve command's own setting: ``key`` from the settings file
        named by VICTORIQUE_CONFIG in the environment of the runs, else the
        built-in value. A solve command without the option keeps the built-in one.
        """
        path = self.launcher.environment().get(settings.CONFIG_ENVIRONMENT) or None
        setting = {"default": builtin, "source": "built-in", "settings_file": path, "settings_error": None}
        if option is None or path is None:
            return setting
        try:
            value = settings.load_config(path).get(key)
        except (OSError, ValueError) as error:
            return {**setting, "settings_error": str(error)}
        if value is not None:
            setting.update(default=value, source="settings file")
        return setting

    def undecided_setting(self):
        """The choice for undecided checks that the form preselects: undecided_checks, else abstain."""
        return self._setting("undecided_checks", UNDECIDED_CHOICES[0], self.undecided_option)

    def minor_findings_setting(self):
        """The choice for a check that passes with minor findings that the form preselects:
        minor_findings, else nonblocking. A solve command without the option counts such
        a check as passed."""
        return self._setting("minor_findings", MINOR_CHOICES[0], self.minor_option)

    def retries_setting(self):
        """The retries after a call timeout that the form shows: timeout_retries, else 1."""
        return self._setting("timeout_retries", settings.TIMEOUT_RETRIES, self.retries_option)

    def options(self):
        undecided = self.undecided_setting()
        minor = self.minor_findings_setting()
        retries = self.retries_setting()
        return {
            "version": __version__, "runs_dir": str(self.runs_dir),
            "policies": [{"value": value, "label": runs.POLICY_LABELS[value]} for value in POLICIES],
            "backends": [{"value": value, "label": runs.BACKEND_LABELS[value]} for value in BACKENDS],
            "check_backends": [{"value": value, "label": runs.CHECK_BACKEND_LABELS[value]} for value in CHECK_BACKENDS],
            "reasoning_efforts": list(REASONING_EFFORTS),
            "defaults": {"policy": POLICIES[0], "backend": "codex", "calls": 12, "hours": 2,
                         "undecided_checks": undecided["default"], "minor_findings": minor["default"],
                         "check_backend": "", "timeout_retries": retries["default"]},
            "checks": {"configurable": self.check_options is not None, "options": self.check_options},
            "timeout_retries": {"configurable": self.retries_option is not None, "option": self.retries_option,
                                **retries, "range": list(settings.TIMEOUT_RETRIES_RANGE)},
            "limits": {"calls": [2, 100], "hours_max": 168, "statement_bytes": PROBLEM_LIMIT},
            "call_timeout": {"configurable": self.timeout_option is not None, "option": self.timeout_option,
                             "fixed_seconds": None if self.timeout_option else FIXED_CALL_SECONDS,
                             "default_seconds": settings.CALL_TIMEOUT_SECONDS,
                             "range": list(settings.CALL_TIMEOUT_RANGE)},
            "undecided_checks": {"configurable": self.undecided_option is not None, "option": self.undecided_option,
                                 **undecided,
                                 "choices": [{"value": value, "label": runs.UNDECIDED_LABELS[value]}
                                             for value in UNDECIDED_CHOICES]},
            "minor_findings": {"configurable": self.minor_option is not None, "option": self.minor_option,
                               **minor,
                               "choices": [{"value": value, "label": runs.MINOR_FINDINGS_LABELS[value]}
                                           for value in MINOR_CHOICES]},
        }

    def create(self, body):
        statement, title, values = parse_form(body, self.timeout_option, self.undecided_option,
                                              self.undecided_setting()["default"], self.minor_option,
                                              self.minor_findings_setting()["default"],
                                              retries_option=self.retries_option,
                                              retries_default=self.retries_setting()["default"],
                                              check_options=self.check_options)
        name = self.launcher.start(statement, title, values, lambda problem, directory: launch.solve_arguments(
            problem, directory, values, self.timeout_option, self.undecided_option, self.minor_option,
            retries_option=self.retries_option, check_options=self.check_options))
        return {"name": name}

    def stop(self, name):
        """Record a stop request for a run, with the function behind ``victorique stop``.

        This writes the run folder's ``stop-request.json``, the only file the
        interface causes to be written inside a run folder.
        """
        if not (self.runs_dir / name / "invocation.json").is_file():
            raise FormError("The run has not created its folder yet; try again in a moment.")
        result = stop_request.request(self.runs_dir / name)
        if result["status"] == "finished":
            raise FormError("The run has already finished.")
        return result

    def resume(self, name):
        """Start ``victorique resume`` for a run whose process ended without writing a summary."""
        if not self.reader.entry(name, self.launcher.liveness)["can_resume"]:
            raise FormError("Only a run whose process ended without writing a summary can be resumed here.")
        try:
            self.launcher.resume(name, self.runs_dir / name)
        except ValueError as error:
            raise FormError(str(error)) from None
        return {"name": name}

    def listing(self):
        return {"runs": self.reader.listing(self.launcher.liveness), "runs_dir": str(self.runs_dir)}

    def detail(self, name):
        if not self.known(name):
            return None
        return self.reader.detail(name, self.launcher.liveness)

    def known(self, name):
        return (isinstance(name, str) and runs.NAME.match(name) is not None
                and ((self.runs_dir / name / "invocation.json").is_file()
                     or (self.runs_dir / runs.GUI_FOLDER / name / "request.json").is_file()))

    def download(self, name, file_name):
        """Bytes and media type of one run file, or None."""
        if not self.known(name):
            return None
        path = self.reader.file_path(name, file_name)
        if path is None:
            return None
        data = shared_read.read_bytes(path, limit=DOWNLOAD_LIMIT)
        return data, DOWNLOAD_TYPES.get(Path(file_name).suffix, "application/octet-stream")

    def archive(self, name, stream):
        """Write a zip of a finished run's folder and its interface files to ``stream``."""
        entry = self.reader.entry(name, self.launcher.liveness)
        if not entry["finished"]:
            raise FormError("The run files can be downloaded as one archive after the run has finished.")
        run_dir = self.runs_dir / name
        skipped, total = [], 0
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            sources = [(run_dir, name)]
            gui_dir = self.runs_dir / runs.GUI_FOLDER / name
            if gui_dir.is_dir():
                sources.append((gui_dir, name + "/interface"))
            for root, prefix in sources:
                for folder, directories, file_names in os.walk(root, followlinks=False):
                    directories[:] = sorted(item for item in directories
                                            if not os.path.islink(os.path.join(folder, item)))
                    for file_name in sorted(file_names):
                        path = Path(folder) / file_name
                        relative = path.relative_to(root).as_posix()
                        if file_name.endswith((".lock", ".tmp")):
                            continue
                        try:
                            if not stat.S_ISREG(os.lstat(path).st_mode):
                                continue
                            source = shared_read.open_shared(path)
                        except OSError:
                            skipped.append(relative)
                            continue
                        with source, archive.open(prefix + "/" + relative, "w", force_zip64=True) as target:
                            while chunk := source.read(1024 * 1024):
                                total += len(chunk)
                                if total > ARCHIVE_LIMIT:
                                    raise FormError("The run folder is too large to download as one archive; "
                                                    "open it on disk instead: " + str(run_dir))
                                target.write(chunk)
            if skipped:
                archive.writestr(name + "/ARCHIVE-NOTES.txt",
                                 "These files could not be read when the archive was made:\n"
                                 + "".join(line + "\n" for line in skipped))


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows would let a second server share the port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, handler, app):
        self.app = app
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind would look up the host's DNS name, which can be slow.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server_version = "victorique-gui/" + __version__
    sys_version = ""

    def log_message(self, format, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def _headers(self, status, content_type, length, headers):
        self.send_response(status)
        for key, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self._headers(status, content_type, len(body), headers)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _host_ok(self):
        allowed = self.app.allowed_hosts
        return allowed is None or self.headers.get("Host", "").strip().lower() in allowed

    def _parts(self):
        path = urlsplit(self.path).path
        return [unquote(part) for part in path.split("/") if part]

    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "Unknown host name.")
        try:
            self._get(self._parts())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as error:  # Keep serving; report the problem to the page.
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "The interface could not read this: " + str(error))

    do_HEAD = do_GET

    def _get(self, parts):
        if parts in ([], ["index.html"]):
            return self._send(HTTPStatus.OK, self.app.index, "text/html; charset=utf-8")
        if len(parts) == 2 and parts[0] == "static" and parts[1] in STATIC_TYPES:
            return self._send(HTTPStatus.OK, self.app.static[parts[1]], STATIC_TYPES[parts[1]])
        if parts == ["api", "options"]:
            return self._json(HTTPStatus.OK, self.app.options())
        if parts == ["api", "runs"]:
            return self._json(HTTPStatus.OK, self.app.listing())
        if len(parts) == 3 and parts[:2] == ["api", "runs"]:
            detail = self.app.detail(parts[2])
            if detail is None:
                return self._error(HTTPStatus.NOT_FOUND, "There is no run with this name.")
            return self._json(HTTPStatus.OK, detail)
        if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "files":
            found = self.app.download(parts[2], parts[4])
            if found is None:
                return self._error(HTTPStatus.NOT_FOUND, "This file does not exist for this run.")
            data, media = found
            return self._send(HTTPStatus.OK, data, media,
                              {"Content-Disposition": attachment(parts[2] + "-" + parts[4])})
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "archive.zip":
            if not self.app.known(parts[2]):
                return self._error(HTTPStatus.NOT_FOUND, "There is no run with this name.")
            with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as buffer:
                try:
                    self.app.archive(parts[2], buffer)
                except FormError as error:
                    return self._error(HTTPStatus.CONFLICT, str(error))
                size = buffer.tell()
                buffer.seek(0)
                self._headers(HTTPStatus.OK, "application/zip", size,
                              {"Content-Disposition": attachment(parts[2] + ".zip")})
                if self.command != "HEAD":
                    shutil.copyfileobj(buffer, self.wfile, 1024 * 1024)
            return None
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        # Read a body of an accepted size before any answer. A server that closes the connection
        # with request bytes unread makes Windows reset it, and the client then gets no answer.
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = None
        data = self.rfile.read(length) if length is not None and 0 <= length <= MAX_BODY else None
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "Unknown host name.")
        origin = self.headers.get("Origin")
        if origin is not None and self.app.allowed_hosts is not None and (
                urlsplit(origin).netloc.lower() not in self.app.allowed_hosts):
            return self._error(HTTPStatus.FORBIDDEN, "Requests from other web pages are refused.")
        token = self.headers.get("X-Victorique-Token", "").encode("latin-1", "replace")
        if not hmac.compare_digest(token, self.app.token.encode("ascii")):
            return self._error(HTTPStatus.FORBIDDEN, "Reload the page and try again.")
        parts = self._parts()
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in ("stop", "resume"):
            if length is not None and data is None:
                return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The request is too large.")
            return self._run_action(parts[2], parts[3])
        if parts != ["api", "runs"]:
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "The form must be sent as JSON.")
        if length is None:
            return self._error(HTTPStatus.LENGTH_REQUIRED, "The form had no length.")
        if data is None:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The form is too large.")
        try:
            body = json.loads(data.decode("utf-8"))
            result = self.app.create(body)
        except FormError as error:
            return self._error(HTTPStatus.BAD_REQUEST, str(error))
        except (ValueError, UnicodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "The form could not be read.")
        except OSError as error:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "The run could not be started: " + str(error))
        self._json(HTTPStatus.CREATED, result)

    def _run_action(self, name, action):
        """POST /api/runs/NAME/stop or /resume, after the same Host, Origin and token checks as the form.

        The action takes no values; a body is read and ignored.
        """
        if not self.app.known(name):
            return self._error(HTTPStatus.NOT_FOUND, "There is no run with this name.")
        try:
            result = self.app.stop(name) if action == "stop" else self.app.resume(name)
        except FormError as error:
            return self._error(HTTPStatus.CONFLICT, str(error))
        except (ValueError, OSError) as error:
            verb = "stopped" if action == "stop" else "resumed"
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"The run could not be {verb}: {error}")
        self._json(HTTPStatus.OK, result)

    def _not_allowed(self):
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Not allowed.")

    do_PUT = do_DELETE = do_PATCH = _not_allowed


def _port_taken(error):
    return error.errno in _PORT_TAKEN or getattr(error, "winerror", None) in _PORT_TAKEN


class GuiServer:
    """The interface server for one runs folder.

    ``port=None`` listens on 8765, or when that port is taken on the first free
    port from 8766 to 8785, or else on a port the system assigns; ``skipped_ports``
    lists the taken ones. An explicit port is used as given, and a taken one is
    an error.
    """

    def __init__(self, runs_dir="runs", *, host=DEFAULT_HOST, port=None, allow_remote=False,
                 env=None, python=None, detect_options=True):
        check_host(host, allow_remote)
        if port is not None and (type(port) is not int or not 0 <= port <= 65535):
            raise ValueError("--port must be from 0 to 65535")
        candidates = [port] if port is not None else [DEFAULT_PORT, *FALLBACK_PORTS, 0]
        self.skipped_ports = []
        for index, candidate in enumerate(candidates, 1):
            try:
                self.httpd = _HTTPServer((host, candidate), _Handler, None)
                break
            except OSError as error:
                if index < len(candidates) and _port_taken(error):
                    self.skipped_ports.append(candidate)
                    continue
                raise OSError(f"cannot listen on {host}:{candidate} ({error.strerror or error}); "
                              "another program may use this port, so choose another --port") from None
        try:
            self.app = GuiApp(runs_dir, env=env, python=python, detect_options=detect_options)
        except BaseException:
            self.httpd.server_close()
            raise
        self.httpd.app = self.app
        self.host, self.port = host, self.httpd.server_address[1]
        self.app.bind(host, self.port)
        self._thread = None

    @property
    def url(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    def serve_forever(self):
        self.httpd.serve_forever(poll_interval=0.25)

    def start(self):
        """Serve in a background thread."""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="victorique-gui")
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)


def _say(text):
    """Print a line even when the console cannot encode a path in it."""
    stream = sys.stdout
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, "backslashreplace").decode(encoding), file=stream, flush=True)


def run_args(args):
    """Run ``victorique gui`` until interrupted; return the exit status."""
    server = GuiServer(args.runs_dir, host=args.host, port=args.port, allow_remote=args.allow_remote)
    if not is_loopback(args.host):
        _say(f"Warning: anyone who can reach {args.host} can use this interface and your model account.")
    skipped = server.skipped_ports
    if skipped:
        ports = (f"Port {skipped[0]} is" if len(skipped) == 1 else
                 f"Ports {skipped[0]} and {skipped[1]} are" if len(skipped) == 2 else
                 f"Ports {skipped[0]} to {skipped[-1]} are")
        _say(f"{ports} not available, so the interface listens on port {server.port}.")
    _say(f"Victorique interface: {server.url}")
    _say(f"Runs folder: {server.app.runs_dir}")
    _say("Press Ctrl+C to stop the interface. Runs in progress keep going; stop one with Stop run on its run page "
         "or with victorique stop DIR.")
    if not args.no_browser:
        threading.Thread(target=webbrowser.open, args=(server.url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    running = server.app.launcher.running()
    hint = ("stop one from its run page (run victorique gui again) or with victorique stop DIR, where DIR is "
            "the run folder.")
    if running:
        _say(f"Interface stopped. {len(running)} run(s) started here are still in progress and keep going until "
             f"they finish; {hint}")
    else:
        _say(f"Interface stopped. Runs in progress keep going until they finish; {hint}")
    return 0
