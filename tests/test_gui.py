"""The local web interface, driven over HTTP with the offline fake transport."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import errno
from importlib.resources import files
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
from types import SimpleNamespace
import unicodedata
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pytest

from victorique import api, shared_read
from victorique.fake import FakeTransport
from victorique.gui import launch, runs
from victorique.gui.server import (FONT_FILES, FONT_LICENSES, STATIC_TYPES, GuiServer, check_host, parse_form,
                                   FormError)

STATEMENT = "Prove that $n + 0 = n$ for every integer $n$.\n\nUse only the definition of $0$."
FINISHED = {"delivered", "abstained", "failed", "interrupted", "stopped"}


@pytest.fixture
def gui(tmp_path):
    server = GuiServer(tmp_path / "runs", port=0).start()
    yield server
    try:
        assert server.app.launcher.wait(120)
    finally:
        server.stop()


def call(server, path, *, body=None, headers=None, token=True, method=None):
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
        if token:
            request_headers.setdefault("X-Victorique-Token", server.app.token)
    request = urllib.request.Request(server.url.rstrip("/") + path, data=data, headers=request_headers,
                                     method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def get_json(server, path):
    status, _, body = call(server, path)
    assert status == 200, body
    return json.loads(body)


def wait_finished(server, name, timeout=150):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        detail = get_json(server, "/api/runs/" + name)
        seen.append(detail["status"])
        if detail["status"] in FINISHED:
            return detail, seen
        time.sleep(0.3)
    raise AssertionError(f"{name} did not finish; statuses seen: {seen[-5:]}")


def test_submit_poll_and_read_results_with_the_fake_backend(gui):
    status, headers, page = call(gui, "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert gui.app.token.encode() in page and b"__VICTORIQUE_TOKEN__" not in page
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert call(gui, "/static/app.js")[0] == 200
    options = get_json(gui, "/api/options")
    assert [item["value"] for item in options["policies"]] == ["single", "ladder", "full"]
    # The solve command of this version lists both options, so both fields are active.
    assert options["call_timeout"] == {"configurable": True, "option": "--call-timeout-seconds",
                                       "fixed_seconds": None, "default_seconds": 600, "range": [0.05, 86400]}
    assert options["undecided_checks"]["configurable"] is True
    assert [item["value"] for item in options["undecided_checks"]["choices"]] == ["abstain", "repair"]

    status, _, body = call(gui, "/api/runs", body={
        "title": "Toy identity", "statement": STATEMENT, "policy": "single", "backend": "fake",
        "model": "", "reasoning_effort": None, "call_timeout": None, "calls": "12", "hours": "1"})
    assert status == 201, body
    delivered_name = json.loads(body)["name"]
    status, _, body = call(gui, "/api/runs", body={
        "statement": STATEMENT, "policy": "ladder", "backend": "fake", "calls": 2, "hours": 1})
    assert status == 201, body
    abstained_name = json.loads(body)["name"]

    delivered, statuses = wait_finished(gui, delivered_name)
    assert delivered["status"] == "delivered", delivered
    assert set(statuses) <= {"starting", "running", "delivered"}
    assert delivered["title"] == "Toy identity"
    assert delivered["policy"] == "single" and delivered["backend"] == "fake"
    assert delivered["calls_used"] == 3 and delivered["calls_limit"] == 12
    assert delivered["proof_kind"] == "delivered" and "n + 0 = n" in delivered["proof"]
    assert delivered["abstention"] is None
    assert [(check["step"], check["review"]["passed"]) for check in delivered["checks"]] == [
        ("Statement check", True), ("Proof check", True)]
    assert [call_row["status_label"] for call_row in delivered["calls"]] == ["Done", "Done", "Done"]
    assert delivered["problem"] == STATEMENT
    assert {item["name"] for item in delivered["files"]} >= {"proof.md", "summary.json", "calls.jsonl",
                                                             "invocation.json", "problem.md", "console.log"}

    # The run was started through the ordinary command and wrote its ordinary files.
    run_dir = gui.app.runs_dir / delivered_name
    invocation = json.loads((run_dir / "invocation.json").read_text(encoding="utf-8"))
    assert invocation["parameters"]["policy"] == "single"
    assert invocation["parameters"]["transport"] == "fake"
    assert invocation["parameters"]["calls"] == 12
    assert invocation["argv"][1] == "solve"
    gui_problem = gui.app.runs_dir / runs.GUI_FOLDER / delivered_name / "problem.md"
    assert gui_problem.read_bytes() == STATEMENT.encode("utf-8")

    abstained, _ = wait_finished(gui, abstained_name)
    assert abstained["status"] == "abstained", abstained
    assert abstained["abstention"]["reason_code"] == "ladder_budget"
    assert "call budget" in abstained["abstention"]["reason"]
    assert abstained["abstention"]["checks_not_run"] == ["Proof check"]
    assert abstained["proof_kind"] == "draft"
    assert abstained["title"].startswith("Prove that $n + 0 = n$")

    listing = get_json(gui, "/api/runs")
    rows = {row["name"]: row for row in listing["runs"]}
    assert rows[delivered_name]["status"] == "delivered"
    assert rows[abstained_name]["status"] == "abstained"
    assert rows[abstained_name]["policy_label"] == "Solve, check and repair"
    assert rows[delivered_name]["started_here"] is True

    status, headers, proof = call(gui, f"/api/runs/{delivered_name}/files/proof.md")
    assert status == 200
    assert headers["Content-Disposition"].startswith(f'attachment; filename="{delivered_name}-proof.md";')
    assert proof == shared_read.read_bytes(run_dir / "proof.md")
    assert call(gui, f"/api/runs/{delivered_name}/files/state")[0] == 404
    assert call(gui, f"/api/runs/{delivered_name}/files/..%2Finvocation.json")[0] == 404

    status, headers, archive = call(gui, f"/api/runs/{delivered_name}/archive.zip")
    assert status == 200 and headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        names = set(opened.namelist())
        assert {f"{delivered_name}/proof.md", f"{delivered_name}/summary.json",
                f"{delivered_name}/interface/problem.md"} <= names
        assert opened.read(f"{delivered_name}/proof.md") == proof


def test_command_failure_is_reported_with_its_message(gui):
    values = {"policy": "single", "backend": "fake", "calls": 1, "hours": 1}
    name = gui.app.launcher.start(STATEMENT, "Bad budget", values, lambda problem, directory: launch.solve_arguments(
        problem, directory, values))
    detail, _ = wait_finished(gui, name)
    assert detail["status"] == "failed"
    assert "invalid calls" in detail["reason"]
    assert detail["run_directory"] is None
    assert any(line.startswith("victorique: ") for line in detail["console"])


def test_runs_started_from_the_command_line_are_listed(gui, tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text("# Additive identity\n\nProve that n + 0 = n for every integer n.", encoding="utf-8")
    name = "命令行 run-1"
    with redirect_stdout(io.StringIO()):
        api.solve(str(problem), policy="single", transport="fake", out=str(gui.app.runs_dir / name))
    (row,) = get_json(gui, "/api/runs")["runs"]
    assert row["name"] == name and row["status"] == "delivered"
    assert row["title"] == "Additive identity" and row["started_here"] is False
    quoted = urllib.parse.quote(name)
    detail = get_json(gui, "/api/runs/" + quoted)
    assert detail["problem"].startswith("# Additive identity")
    assert detail["proof_kind"] == "delivered"
    status, headers, _ = call(gui, f"/api/runs/{quoted}/files/summary.json")
    assert status == 200
    assert headers["Content-Disposition"].endswith("filename*=UTF-8''" + urllib.parse.quote(name + "-summary.json",
                                                                                              safe=""))
    for bad in ("..", ".victorique-gui", "a%2Fb", "%2E%2E"):
        assert call(gui, "/api/runs/" + bad)[0] == 404


def test_interface_reads_a_run_while_its_summary_is_replaced(tmp_path):
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        run_dir = server.app.runs_dir / "replaced"
        run_dir.mkdir()
        started = datetime.now(timezone.utc)
        (run_dir / "invocation.json").write_text(json.dumps({
            "started_at": started.isoformat(), "argv": ["victorique", "solve"],
            "parameters": {"policy": "single", "transport": "fake", "calls": 12, "hours": 1, "model": None}}),
            encoding="utf-8")
        summary = run_dir / "summary.json"

        def write(number):
            outcome = "complete" if number % 2 else "abstain"
            temporary = run_dir / f"summary.{number}.tmp"
            temporary.write_text(json.dumps({"outcome": outcome, "calls_used": 3, "calls_limit": 12,
                                             "endpoint_reason": "single_" + outcome, "padding": "x" * 4096}),
                                 encoding="utf-8")
            for _ in range(500):
                try:
                    os.replace(temporary, summary)  # Retried as on Windows any open handle refuses it.
                    return
                except PermissionError:
                    time.sleep(0.001)
            raise AssertionError("replacement never succeeded")

        write(1)
        done, problems = threading.Event(), []

        def writer():
            try:
                for number in range(2, 202):
                    write(number)
            except BaseException as error:
                problems.append(repr(error))
            finally:
                done.set()

        thread = threading.Thread(target=writer)
        thread.start()
        statuses = set()
        while not done.is_set():
            statuses.add(server.app.detail("replaced")["status"])
            status, _, body = call(server, "/api/runs/replaced")
            assert status == 200, body
            statuses.add(json.loads(body)["status"])
        thread.join(60)
        assert problems == []
        assert statuses <= {"delivered", "abstained"} and statuses
    finally:
        server.stop()


def test_refuses_hosts_other_computers_can_reach(tmp_path):
    for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
        check_host(host)
    for host in ("0.0.0.0", "::", "192.168.1.20", "example.org"):
        with pytest.raises(ValueError, match="--allow-remote"):
            check_host(host)
        check_host(host, allow_remote=True)
    with pytest.raises(ValueError, match="--allow-remote"):
        GuiServer(tmp_path / "runs", host="0.0.0.0", port=0, detect_options=False)
    assert not (tmp_path / "runs").exists()
    result = subprocess.run([sys.executable, "-m", "victorique", "gui", "--host", "0.0.0.0", "--no-browser",
                             "--runs-dir", str(tmp_path / "runs")], capture_output=True, text=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 2
    assert "--allow-remote" in result.stderr and "127.0.0.1" in result.stderr
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False)
    try:
        assert server.httpd.server_address[0] == "127.0.0.1"
        assert server.app.allowed_hosts == {f"{name}:{server.port}" for name in
                                            ("127.0.0.1", "localhost", "[::1]")}
        with pytest.raises(OSError, match="choose another --port"):
            GuiServer(tmp_path / "second", port=server.port, detect_options=False)
        assert not (tmp_path / "second").exists()
    finally:
        server.httpd.server_close()
    app = server.app
    app.bind("192.168.1.20", 8765)
    assert app.allowed_hosts == {"192.168.1.20:8765"}
    app.bind("0.0.0.0", 8765)
    assert app.allowed_hosts is None


def test_cross_site_requests_and_unknown_host_names_are_refused(tmp_path):
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        form = {"statement": STATEMENT, "backend": "fake", "policy": "single"}
        assert call(server, "/api/runs", body=form, token=False)[0] == 403
        assert call(server, "/api/runs", body=form, headers={"X-Victorique-Token": "wrong"})[0] == 403
        assert call(server, "/api/runs", body=form, headers={"Origin": "http://example.org"})[0] == 403
        assert call(server, "/api/runs", headers={"Host": f"example.org:{server.port}"})[0] == 403
        status, headers, _ = call(server, "/api/runs")
        assert status == 200 and "Access-Control-Allow-Origin" not in headers
        assert call(server, "/api/runs", method="DELETE")[0] == 405
        assert get_json(server, "/api/runs")["runs"] == []
        assert not (server.app.runs_dir / runs.GUI_FOLDER).exists()
    finally:
        server.stop()


@pytest.mark.parametrize("change,message", [
    ({"statement": "  "}, "problem statement"),
    ({"backend": "claude", "reasoning_effort": "high"}, "Codex CLI"),
    ({"calls": 1}, "Maximum model calls"),
    ({"hours": 0}, "Time limit"),
    ({"model": "two words"}, "model name"),
    ({"policy": "bestofn"}, "approach"),
    # Without detected options, as with a solve command older than these two options:
    ({"call_timeout": 300}, "per-call time limit"),
    ({"undecided_checks": "repair"}, "always stops without a proof"),
    ({"undecided_checks": "sometimes"}, "check is undecided"),
])
def test_form_errors_are_plain_and_start_nothing(tmp_path, change, message):
    with pytest.raises(FormError, match=message):
        parse_form({"statement": STATEMENT, "policy": "ladder", "backend": "codex", **change})
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        status, _, body = call(server, "/api/runs", body={"statement": STATEMENT, "policy": "ladder",
                                                          "backend": "codex", **change})
        assert status == 400 and message in json.loads(body)["error"]
        assert get_json(server, "/api/runs")["runs"] == []
    finally:
        server.stop()


def test_form_values_become_solve_arguments(tmp_path):
    statement, title, values = parse_form({
        "statement": STATEMENT, "title": "  A   title ", "policy": "full", "backend": "codex",
        "model": " gpt-x ", "reasoning_effort": "high", "calls": "20", "hours": "1.5"})
    assert statement == STATEMENT and title == "A title"
    arguments = launch.solve_arguments(tmp_path / "p.md", tmp_path / "out", values)
    assert arguments == ["solve", str(tmp_path / "p.md"), "--policy", "full", "--transport", "codex",
                         "--calls", "20", "--hours", "1.5", "--out", str(tmp_path / "out"),
                         "--model", "gpt-x", "--reasoning-effort", "high"]
    _, _, timed = parse_form({"statement": STATEMENT, "policy": "ladder", "backend": "fake", "call_timeout": "90",
                              "undecided_checks": "repair"},
                             timeout_option="--call-timeout-seconds", undecided_option="--undecided-checks")
    assert timed["policy"] == "ladder" and timed["undecided_checks"] == "repair"
    assert launch.solve_arguments("p", "o", timed, "--call-timeout-seconds", "--undecided-checks")[-4:] == [
        "--call-timeout-seconds", "90.0", "--undecided-checks", "repair"]
    with pytest.raises(ValueError):
        launch.solve_arguments("p", "o", timed, None, "--undecided-checks")
    with pytest.raises(ValueError):
        launch.solve_arguments("p", "o", timed, "--call-timeout-seconds", None)
    # A form without an approach starts the default, single.
    _, _, default = parse_form({"statement": STATEMENT, "backend": "fake"}, undecided_option="--undecided-checks")
    assert default["policy"] == "single" and default["undecided_checks"] is None
    assert launch.solve_arguments("p", "o", default)[2:4] == ["--policy", "single"]
    # Abstain is passed explicitly for ladder; single and full do not use the choice, so nothing is passed.
    _, _, ladder = parse_form({"statement": STATEMENT, "policy": "ladder", "backend": "fake"},
                              undecided_option="--undecided-checks")
    assert launch.solve_arguments("p", "o", ladder, None, "--undecided-checks")[-2:] == [
        "--undecided-checks", "abstain"]
    for policy in ("single", "full"):
        _, _, other = parse_form({"statement": STATEMENT, "backend": "fake", "policy": policy},
                                 undecided_option="--undecided-checks")
        assert other["undecided_checks"] is None
        assert "--undecided-checks" not in launch.solve_arguments("p", "o", other, None, "--undecided-checks")
    _, _, older = parse_form({"statement": STATEMENT, "backend": "fake", "undecided_checks": "abstain"})
    assert older["undecided_checks"] is None and older["call_timeout"] is None


def test_the_form_lists_single_first_and_preselects_it(tmp_path):
    page = files("victorique.gui").joinpath("static").joinpath("index.html").read_text(encoding="utf-8")
    radios = re.findall(r'<input type="radio" name="policy" value="(\w+)"( checked)?>', page)
    assert radios == [("single", " checked"), ("ladder", ""), ("full", "")]
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        options = get_json(server, "/api/options")
    finally:
        server.stop()
    assert [item["value"] for item in options["policies"]] == ["single", "ladder", "full"]
    assert options["defaults"]["policy"] == "single"


def test_option_names_are_read_from_the_solve_help():
    # The exact per-call option is preferred; otherwise the one option whose name contains "timeout".
    assert launch.call_timeout_option({"--calls", "--call-timeout-seconds", "--grading-timeout"}) == (
        "--call-timeout-seconds")
    assert launch.call_timeout_option({"--calls", "--call-timeout"}) == "--call-timeout"
    assert launch.call_timeout_option({"--calls", "--call-timeout", "--grading-timeout"}) is None
    assert launch.call_timeout_option({"--calls", "--hours"}) is None
    assert launch.undecided_checks_option({"--calls", "--undecided-checks"}) == "--undecided-checks"
    assert launch.undecided_checks_option({"--calls", "--undecided"}) is None


@pytest.mark.parametrize("change,message", [
    ({"call_timeout": "0.01"}, "at least 0.05"),
    ({"call_timeout": "90000"}, "at most 86400"),
    ({"call_timeout": "soon"}, "must be a number"),
    ({"policy": "single", "undecided_checks": "repair"}, "only with the approach"),
    ({"undecided_checks": "REPAIR"}, "check is undecided"),
])
def test_new_fields_are_checked_before_a_run_starts(change, message):
    with pytest.raises(FormError, match=message):
        parse_form({"statement": STATEMENT, "policy": "ladder", "backend": "fake", **change},
                   timeout_option="--call-timeout-seconds", undecided_option="--undecided-checks")


def _run_files(root, name, *, summary=None, hours=1, started=None, console=None, exit_code=None):
    run_dir = root / name
    run_dir.mkdir(parents=True)
    started = started or datetime.now(timezone.utc)
    (run_dir / "invocation.json").write_text(json.dumps({"started_at": started.isoformat(), "parameters": {
        "policy": "ladder", "transport": "codex", "hours": hours, "calls": 12, "model": None}}), encoding="utf-8")
    if summary is not None:
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if console is not None or exit_code is not None:
        gui_dir = root / runs.GUI_FOLDER / name
        gui_dir.mkdir(parents=True)
        (gui_dir / "request.json").write_text(json.dumps({"name": name, "title": None}), encoding="utf-8")
        (gui_dir / "console.log").write_text(console or "", encoding="utf-8")
        if exit_code is not None:
            (gui_dir / "exit.json").write_text(json.dumps({"exit_code": exit_code}), encoding="utf-8")


def test_status_follows_the_run_files_and_the_process(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    _run_files(tmp_path, "pending", summary={"outcome": "pending"})
    _run_files(tmp_path, "partial", summary={"outcome": "partial", "endpoint_reason": "session_sealed"})
    _run_files(tmp_path, "crashed", console="solve_obligation | solver | 1/12 | original_prepared_dispatched\n",
               exit_code=1)
    _run_files(tmp_path, "refused", console="victorique: codex CLI unavailable\n", exit_code=2)
    _run_files(tmp_path, "quiet-old", started=old, hours=1)
    _run_files(tmp_path, "quiet-new")
    _run_files(tmp_path, "live", console="whole_proof_review | proof_reviewer | 3/12 | original_prepared_dispatched\n")
    reader = runs.RunReader(tmp_path)
    alive = {"live": True}
    status = {name: reader.entry(name, lambda name, request: alive.get(name)) for name in reader.run_names()}
    assert {name: entry["status"] for name, entry in status.items()} == {
        "pending": "interrupted", "partial": "failed", "crashed": "interrupted", "refused": "failed",
        "quiet-old": "interrupted", "quiet-new": "running", "live": "running"}
    assert status["refused"]["reason"] == "codex CLI unavailable"
    assert status["live"]["progress"] == {"step": "Proof check", "phase": "sent to the model",
                                          "calls_used": 3, "calls_limit": 12}
    assert status["live"]["calls_used"] == 3
    assert "session_sealed" in status["partial"]["reason"]


def test_a_recorded_process_is_recognised_until_it_ends(tmp_path):
    not_before = time.time()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(tmp_path)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    not_after = time.time()
    try:
        assert launch.process_running(child.pid, not_before, not_after, str(tmp_path)) is True
        if os.name == "nt":  # A reused process number has a different creation time.
            assert launch.process_running(child.pid, not_before - 3600, not_after - 3600) is False
    finally:
        child.kill()
        child.wait(30)
    assert launch.process_running(child.pid, not_before, not_after, str(tmp_path)) is False


def test_interface_files_ship_as_package_data_without_remote_resources():
    static = files("victorique.gui").joinpath("static")
    for name in ("index.html", "app.js", "style.css"):
        text = static.joinpath(name).read_text(encoding="utf-8")
        assert re.search(r"https?:", text, re.IGNORECASE) is None, name
        # No protocol-relative address either, such as url(//fonts.example) or src="//cdn.example".
        assert re.search(r"""(?:url\(\s*["']?|(?:src|href)\s*=\s*["'])//""", text) is None, name
    page = static.joinpath("index.html").read_text(encoding="utf-8")
    assert '<script src="/static/app.js"' in page and "<script>" not in page
    # The content security policy refuses inline styles: no style element or attribute, and the
    # script sets bar, gauge and timeline values through the CSS object model only.
    assert "<style" not in page and "style=" not in page
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    assert 'setAttribute("style"' not in script and ".cssText" not in script and "style.setProperty(" in script
    for name in (*FONT_FILES, *FONT_LICENSES):
        assert static.joinpath(name).is_file(), name
    assert set(STATIC_TYPES) <= {item.name for item in static.iterdir()}


def _stylesheet():
    return files("victorique.gui").joinpath("static").joinpath("style.css").read_text(encoding="utf-8")


def _font_faces():
    return [dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", block))
            for block in re.findall(r"@font-face\s*\{([^}]*)\}", _stylesheet())]


_SOURCE = r'(url|local)\("([^"]+)"\)(?: format\("([^"]+)"\))?'


def _sources(src):
    """The (kind, name, format) of each source in an @font-face src list."""
    # Every part of the list is recognised: sources separated by commas and nothing else.
    assert re.fullmatch(rf"{_SOURCE}(?:,\s*{_SOURCE})*", src.strip()), src
    return re.findall(_SOURCE, src)


def test_every_font_face_names_a_packaged_font_that_the_server_sends_as_woff2(tmp_path):
    static = files("victorique.gui").joinpath("static")
    faces = _font_faces()
    urls, packaged = [], []
    for face in faces:
        sources = _sources(face["src"])
        if all(kind == "local" for kind, _, _ in sources):
            continue  # Installed fonts only: the browser fetches nothing for this rule.
        # A rule with a url() source names exactly one file, in the WOFF2 format.
        assert [(kind, form) for kind, _, form in sources] == [("url", "woff2")], face["src"]
        urls.append(sources[0][1])
        packaged.append(face)
    assert sorted(urls) == sorted(FONT_FILES)
    assert {(face["font-family"], face["font-weight"]) for face in packaged} == {
        ('"Michroma"', "400"), ('"Barlow Semi Condensed"', "400"), ('"Barlow Semi Condensed"', "500"),
        ('"JetBrains Mono"', "400 500")}
    assert all(face["font-display"] == "swap" and face["unicode-range"].startswith("U+") for face in faces)
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        status, headers, _ = call(server, "/")
        assert status == 200
        policy = headers["Content-Security-Policy"]
        assert "font-src 'self'" in policy and "default-src 'none'" in policy
        for url in urls:
            # Relative to the stylesheet, /static/style.css.
            status, headers, body = call(server, urllib.parse.urljoin("/static/style.css", url))
            assert status == 200 and headers["Content-Type"] == "font/woff2", url
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert body[:4] == b"wOF2" and body == static.joinpath(url).read_bytes()
        for name in FONT_LICENSES:
            status, headers, body = call(server, "/static/" + name)
            assert status == 200 and headers["Content-Type"] == "text/plain; charset=utf-8"
            assert body.startswith(b"Copyright ") and b"SIL Open Font License, Version 1.1" in body
        for missing in ("/static/index.html", "/static/OFL.txt", "/static/..%2Fserver.py", "/static/fonts/x.woff2"):
            assert call(server, missing)[0] == 404, missing
    finally:
        server.stop()


def _code_points(unicode_range):
    points = set()
    for part in unicode_range.split(","):
        low, _, high = part.strip().removeprefix("U+").partition("-")
        points.update(range(int(low, 16), int(high or low, 16) + 1))
    return points


def _jetbrains_mono_faces():
    return {re.fullmatch(r'url\("([^"]+)"\) format\("woff2"\)', face["src"].strip()).group(1): face
            for face in _font_faces() if face["font-family"] == '"JetBrains Mono"'}


def test_the_math_symbol_subset_forms_one_face_with_the_other_jetbrains_mono_files():
    faces = _jetbrains_mono_faces()
    assert sorted(faces) == ["jetbrainsmono-var-greek.woff2", "jetbrainsmono-var-latin-ext.woff2",
                             "jetbrainsmono-var-latin.woff2", "jetbrainsmono-var-math.woff2"]
    math = faces.pop("jetbrainsmono-var-math.woff2")
    descriptors = ("font-style", "font-weight", "font-display")
    assert [math[name] for name in descriptors] == ["normal", "400 500", "swap"]
    for name, face in faces.items():
        assert [face[key] for key in descriptors] == [math[key] for key in descriptors], name
    covered = _code_points(math["unicode-range"])
    # ∀ ∈ → ≤ ∣ are glyphs of the file. U+0338 is left out, and so are the negated forms such as ∤ (U+2224)
    # that the browser would compose with it: the composed slash lands in the next character cell.
    assert {0x2200, 0x2208, 0x2192, 0x2264, 0x2223} <= covered
    assert len(covered) == 36 and not {0x0338, 0x2224} & covered
    # Space, =, ¬, …, ′ and ″ are in the character maps of both files and stay with the latin file.
    for name, face in faces.items():
        assert not covered & _code_points(face["unicode-range"]), name


def test_negated_math_symbols_come_from_installed_math_fonts():
    (fallback,) = [face for face in _font_faces() if face["font-family"] == '"Math Fallback"']
    assert [fallback[name] for name in ("font-style", "font-display")] == ["normal", "swap"]
    # Installed fonts only, so the page fetches nothing for this rule.
    assert {kind for kind, _, _ in _sources(fallback["src"])} == {"local"}
    covered = _code_points(fallback["unicode-range"])
    # ∤ ≢ ≮ ≰ ⊈: JetBrains Mono and Consolas compose these with U+0338 and draw the slash in the next cell.
    assert {0x2224, 0x2262, 0x226E, 0x2270, 0x2288} <= covered
    # U+0338 too, so that a negated form written as its base character and U+0338 gets the same glyph.
    assert 0x0338 in covered
    # No JetBrains Mono rule covers any of these code points, U+0338 included.
    jetbrains_mono = set().union(*(_code_points(face["unicode-range"]) for face in _jetbrains_mono_faces().values()))
    assert not covered & jetbrains_mono
    # The rule covers U+0338 and every form whose canonical decomposition is a base character and U+0338, except
    # the six that are glyphs of the JetBrains Mono math file.
    negated = {point for point in range(sys.maxunicode + 1)
               if unicodedata.decomposition(chr(point)).endswith(" 0338")}
    assert "".join(sorted(chr(point) for point in negated & jetbrains_mono)) == "∉∌≉≠⊄⊅"
    assert len(negated - jetbrains_mono) == 39
    assert covered == (negated - jetbrains_mono) | {0x0338} and len(covered) == 40
    # Problem, proof and number text ask for this family right after JetBrains Mono.
    stacks = dict(re.findall(r"^\s*(--[md])\s*:\s*([^;]+);", _stylesheet(), re.MULTILINE))
    for name in ("--m", "--d"):
        assert [font.strip() for font in stacks[name].split(",")][:2] == ['"JetBrains Mono"', '"Math Fallback"'], name


def test_math_characters_that_the_bundled_monospace_font_covers():
    """docs/gui.md lists this result: the JetBrains Mono rules cover 35 of these 57 characters."""
    covered = set().union(*(_code_points(face["unicode-range"]) for face in _jetbrains_mono_faces().values()))
    math = ("≡ ≤ ≥ ≠ ≈ ∣ ∤ ∑ ∏ ∫ ∈ ∉ ∀ ∃ → ← ↔ ⇒ ⇐ ⇔ √ ∞ ∘ ⊂ ⊆ ⊃ ⊇ ∪ ∩ ∅ ∂ ∇ ⌊ ⌋ ⌈ ⌉ ℕ ℤ ℚ ℝ ℂ ⟨ ⟩ "
            "′ ″ … ∧ ∨ ¬ ⊕ ⊗ ≅ ∼ ≺ ≻ ⊥ ∥").split()
    assert len(math) == len(set(math)) == 57
    assert sum(ord(character) in covered for character in math) == 35
    # JetBrains Mono has no glyph for these: ∤ comes from "Math Fallback", the others from the next fonts in the stack.
    assert "".join(character for character in math if ord(character) not in covered) == "∤⇒⇐⇔∪∩∅∇⌊⌋⌈⌉ℤℚℝℂ⊕⊗≅≺≻⊥"
    assert all(ord(character) in covered for character in "∋∌≉⊄⊅±×·")


def test_package_metadata_declares_the_font_license_and_includes_its_texts():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0 AND OFL-1.1"
    matched = {path.relative_to(root).as_posix()
               for pattern in project["license-files"] for path in root.glob(pattern)}
    static = "src/victorique/gui/static/"
    assert matched == {"LICENSE", "NOTICE", *(static + name for name in FONT_LICENSES)}
    assert sorted(path.name for path in (root / static).glob("OFL*")) == sorted(FONT_LICENSES)


@pytest.mark.parametrize("no_browser", [False, True])
def test_gui_command_prints_its_address_and_stops_on_ctrl_c(tmp_path, monkeypatch, capsys, no_browser):
    import argparse
    from victorique import gui as gui_command
    from victorique.gui import server as module
    opened = []
    monkeypatch.setattr(module.webbrowser, "open", opened.append)

    def interrupted(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(module.GuiServer, "serve_forever", interrupted)
    parser = argparse.ArgumentParser()
    gui_command.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["gui", "--port", "0", "--runs-dir", str(tmp_path / "runs")]
                             + (["--no-browser"] if no_browser else []))
    assert args.host == "127.0.0.1" and args.allow_remote is False
    assert gui_command.run_args(args) == 0
    out = capsys.readouterr().out
    url = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("Victorique interface: "))
    assert url.startswith("http://127.0.0.1:") and "Interface stopped." in out
    # Runs keep going after the interface stops; the messages say how to stop one.
    shutdown = next(line for line in out.splitlines() if line.startswith("Interface stopped."))
    assert "keep going" in shutdown and "run page" in shutdown and "victorique stop DIR" in shutdown
    assert "Stop run" in next(line for line in out.splitlines() if line.startswith("Press Ctrl+C"))
    deadline = time.monotonic() + 5
    while not no_browser and not opened and time.monotonic() < deadline:
        time.sleep(0.05)
    assert opened == ([] if no_browser else [url])


def _hold_port():
    """A listening socket on a free loopback port, as another program would hold it."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    return holder, holder.getsockname()[1]


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_without_a_port_a_taken_default_port_falls_back(tmp_path, monkeypatch):
    from victorique.gui import server as module
    assert (module.DEFAULT_PORT, module.FALLBACK_PORTS) == (8765, tuple(range(8766, 8786)))
    first, taken = _hold_port()
    second, also_taken = _hold_port()
    try:
        free = _free_port()
        monkeypatch.setattr(module, "DEFAULT_PORT", taken)
        monkeypatch.setattr(module, "FALLBACK_PORTS", (also_taken, free))
        server = GuiServer(tmp_path / "runs", detect_options=False)
        try:
            assert (server.port, server.skipped_ports) == (free, [taken, also_taken])
            assert server.url == f"http://127.0.0.1:{free}/"
            assert server.app.allowed_hosts == {f"{name}:{free}" for name in ("127.0.0.1", "localhost", "[::1]")}
        finally:
            server.httpd.server_close()
        # When every listed port is taken, the system assigns one.
        monkeypatch.setattr(module, "FALLBACK_PORTS", (also_taken,))
        server = GuiServer(tmp_path / "runs", detect_options=False)
        try:
            assert server.port not in (0, taken, also_taken) and server.skipped_ports == [taken, also_taken]
        finally:
            server.httpd.server_close()
        # An explicit port is used as given: a taken one is an error, and no runs folder is made.
        with pytest.raises(OSError, match="choose another --port"):
            GuiServer(tmp_path / "explicit", port=taken, detect_options=False)
        assert not (tmp_path / "explicit").exists()
    finally:
        first.close()
        second.close()


def test_gui_command_without_a_port_prints_and_opens_the_address_it_uses(tmp_path, monkeypatch, capsys):
    import argparse
    from victorique import gui as gui_command
    from victorique.gui import server as module
    opened = []
    monkeypatch.setattr(module.webbrowser, "open", opened.append)

    def interrupted(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(module.GuiServer, "serve_forever", interrupted)
    parser = argparse.ArgumentParser()
    gui_command.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["gui", "--runs-dir", str(tmp_path / "runs")])
    assert args.port is None
    holder, taken = _hold_port()
    try:
        free = _free_port()
        monkeypatch.setattr(module, "DEFAULT_PORT", taken)
        monkeypatch.setattr(module, "FALLBACK_PORTS", (free,))
        assert gui_command.run_args(args) == 0
    finally:
        holder.close()
    out = capsys.readouterr().out
    assert f"Port {taken} is not available, so the interface listens on port {free}." in out
    assert f"Victorique interface: http://127.0.0.1:{free}/" in out
    deadline = time.monotonic() + 5
    while not opened and time.monotonic() < deadline:
        time.sleep(0.05)
    assert opened == [f"http://127.0.0.1:{free}/"]


def test_an_explicit_port_that_is_taken_stops_the_command_with_status_2(tmp_path):
    holder, taken = _hold_port()
    try:
        result = subprocess.run([sys.executable, "-m", "victorique", "gui", "--port", str(taken), "--no-browser",
                                 "--runs-dir", str(tmp_path / "runs")], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        holder.close()
    assert result.returncode == 2
    assert f"victorique: cannot listen on 127.0.0.1:{taken}" in result.stderr
    assert "choose another --port" in result.stderr and "Victorique interface:" not in result.stdout
    assert not (tmp_path / "runs").exists()


def test_a_runs_folder_that_cannot_be_created_or_written_stops_the_command_with_status_2(tmp_path, monkeypatch,
                                                                                         capsys):
    from victorique import cli
    from victorique.gui import server as module
    # A file stands where a folder of the runs path should be: one line and exit status 2, no traceback.
    blocker = tmp_path / "notes.txt"
    blocker.write_text("x", encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "victorique", "gui", "--port", "0", "--no-browser",
                             "--runs-dir", str(blocker / "runs")], capture_output=True, encoding="utf-8",
                            errors="replace", timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 2 and result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith(f"victorique: cannot create or write the runs folder {blocker / 'runs'} (")
    assert lines[0].endswith("); choose another folder with --runs-dir")
    if os.name == "nt":
        assert f"({blocker} is a file)" in lines[0]

    # A folder that exists but takes no new file gives the same line with the system's reason.
    folder = tmp_path / "no-new-files"
    folder.mkdir()
    real_open = os.open

    def refused(path, flags, *args, **kwargs):
        if Path(path).parent == folder:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, "open", refused)
    monkeypatch.setattr(sys, "argv", ["victorique", "gui", "--port", "0", "--no-browser", "--runs-dir", str(folder)])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [f"victorique: cannot create or write the runs folder {folder} "
                                         "(Permission denied); choose another folder with --runs-dir"]
    assert list(folder.iterdir()) == []
    # The check leaves nothing behind in a folder it can write.
    assert module.prepare_runs_folder(tmp_path / "fine") == tmp_path / "fine"
    assert list((tmp_path / "fine").iterdir()) == []


def test_gui_command_help_lists_its_options():
    result = subprocess.run([sys.executable, "-m", "victorique", "gui", "--help"], capture_output=True, text=True,
                            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0
    for option in ("--host", "--port", "--runs-dir", "--no-browser", "--allow-remote", "127.0.0.1"):
        assert option in result.stdout


def _request(server, name):
    return json.loads((server.app.runs_dir / runs.GUI_FOLDER / name / "request.json").read_text(encoding="utf-8"))


def _parameters(server, name):
    return json.loads((server.app.runs_dir / name / "invocation.json").read_text(encoding="utf-8"))["parameters"]


def test_per_call_limit_and_undecided_choice_reach_the_solve_command(gui, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    # Both options were read from this version's solve --help when the interface started.
    assert gui.app.timeout_option == "--call-timeout-seconds"
    assert gui.app.undecided_option == "--undecided-checks"
    status, _, body = call(gui, "/api/runs", body={
        "statement": STATEMENT, "policy": "ladder", "backend": "fake", "call_timeout": "90",
        "undecided_checks": "repair", "calls": 12, "hours": 1})
    assert status == 201, body
    chosen = json.loads(body)["name"]
    status, _, body = call(gui, "/api/runs", body={
        "statement": STATEMENT, "policy": "single", "backend": "fake", "call_timeout": "",
        "undecided_checks": None, "calls": 3, "hours": 1})
    assert status == 201, body
    defaults = json.loads(body)["name"]

    detail, _ = wait_finished(gui, chosen)
    assert detail["status"] == "delivered", detail
    command = _request(gui, chosen)["command"]
    assert command[command.index("--call-timeout-seconds") + 1] == "90.0"
    assert command[command.index("--undecided-checks") + 1] == "repair"
    parameters = _parameters(gui, chosen)
    assert parameters["call_timeout_seconds"] == 90 and parameters["undecided_checks"] == "repair"
    assert detail["call_timeout_seconds"] == 90
    assert (detail["undecided_checks"], detail["undecided_checks_label"]) == ("repair", "Repair once")

    # An empty per-call field passes nothing, and single does not use the undecided choice.
    detail, _ = wait_finished(gui, defaults)
    assert detail["status"] == "delivered", detail
    command = _request(gui, defaults)["command"]
    assert "--call-timeout-seconds" not in command and "--undecided-checks" not in command
    parameters = _parameters(gui, defaults)
    assert parameters["call_timeout_seconds"] == 600 and parameters["undecided_checks"] == "abstain"
    assert detail["call_timeout_seconds"] == 600 and detail["undecided_checks_label"] is None


def test_without_the_options_both_fields_stay_inactive(tmp_path, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False)
    try:
        options = server.app.options()
        assert options["call_timeout"]["configurable"] is False and options["call_timeout"]["fixed_seconds"] == 600
        assert options["undecided_checks"] == {
            "configurable": False, "option": None, "default": "abstain", "source": "built-in",
            "settings_file": None, "settings_error": None,
            "choices": [{"value": "abstain", "label": "Abstain"}, {"value": "repair", "label": "Repair once"}]}
    finally:
        server.httpd.server_close()


def test_the_undecided_choice_is_preselected_from_the_settings_file(tmp_path, monkeypatch):
    config = tmp_path / "victorique.toml"
    config.write_text('undecided_checks = "repair"\n', encoding="utf-8")
    monkeypatch.setenv("VICTORIQUE_CONFIG", str(config))
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False)
    try:
        app = server.app
        # A solve command without --undecided-checks always abstains, whatever the file says.
        assert (app.options()["undecided_checks"]["default"], app.options()["defaults"]["undecided_checks"]) == (
            "abstain", "abstain")
        app.undecided_option = "--undecided-checks"
        options = app.options()
        assert options["defaults"]["undecided_checks"] == "repair"
        assert {key: options["undecided_checks"][key] for key in ("default", "source", "settings_file",
                                                                  "settings_error")} == {
            "default": "repair", "source": "settings file", "settings_file": str(config), "settings_error": None}

        # A form without a choice uses the preselected value for ladder; single and full pass nothing.
        def undecided(**change):
            body = {"statement": STATEMENT, "policy": "ladder", "backend": "fake", **change}
            return parse_form(body, undecided_option="--undecided-checks",
                              undecided_default=app.undecided_setting()["default"])[2]["undecided_checks"]
        assert undecided() == "repair" and undecided(undecided_checks=None) == "repair"
        assert undecided(undecided_checks="abstain") == "abstain"
        assert undecided(policy="single") is None and undecided(policy="full") is None

        # A file without the setting, or no file, leaves the built-in default.
        config.write_text("call_timeout_seconds = 90\n", encoding="utf-8")
        assert app.undecided_setting() == {"default": "abstain", "source": "built-in", "settings_file": str(config),
                                           "settings_error": None}
        monkeypatch.delenv("VICTORIQUE_CONFIG")
        assert app.undecided_setting()["settings_file"] is None and undecided() == "abstain"
        # A file that cannot be read leaves the built-in default and reports why.
        for text in ("undecided_checks = 'sometimes'\n", "not toml [\n", None):
            broken = tmp_path / "broken.toml"
            broken.unlink(missing_ok=True)
            if text is not None:
                broken.write_text(text, encoding="utf-8")
            monkeypatch.setenv("VICTORIQUE_CONFIG", str(broken))
            setting = app.undecided_setting()
            assert setting["default"] == "abstain" and setting["source"] == "built-in"
            assert "broken.toml" in setting["settings_error"], text
    finally:
        server.httpd.server_close()


def test_a_relative_settings_path_reaches_child_commands_as_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VICTORIQUE_CONFIG", os.path.join("conf", "victorique.toml"))
    launcher = launch.Launcher(tmp_path / "runs")
    resolved = launcher.environment()["VICTORIQUE_CONFIG"]
    assert os.path.isabs(resolved) and Path(resolved) == tmp_path / "conf" / "victorique.toml"
    # Resolved against the folder where the interface started, not a later working directory.
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert Path(launcher.environment()["VICTORIQUE_CONFIG"]) == tmp_path / "conf" / "victorique.toml"
    # Absolute and home-relative paths stay where they point; an empty value stays empty.
    monkeypatch.setenv("VICTORIQUE_CONFIG", str(tmp_path / "absolute.toml"))
    assert Path(launcher.environment()["VICTORIQUE_CONFIG"]) == tmp_path / "absolute.toml"
    monkeypatch.setenv("VICTORIQUE_CONFIG", os.path.join("~", "victorique.toml"))
    assert Path(launcher.environment()["VICTORIQUE_CONFIG"]) == Path.home() / "victorique.toml"
    monkeypatch.setenv("VICTORIQUE_CONFIG", "")
    assert launcher.environment()["VICTORIQUE_CONFIG"] == ""
    monkeypatch.delenv("VICTORIQUE_CONFIG")
    assert "VICTORIQUE_CONFIG" not in launcher.environment()
    # A value given to the launcher itself is resolved the same way.
    other = launch.Launcher(tmp_path / "runs", env={"VICTORIQUE_CONFIG": "other.toml"})
    assert Path(other.environment()["VICTORIQUE_CONFIG"]) == tmp_path / "elsewhere" / "other.toml"


def test_runs_started_here_read_a_relative_settings_file_from_the_start_folder(tmp_path, monkeypatch):
    start = tmp_path / "start"
    (start / "conf").mkdir(parents=True)
    (start / "conf" / "victorique.toml").write_text('call_timeout_seconds = 90\nundecided_checks = "repair"\n',
                                                     encoding="utf-8")
    monkeypatch.chdir(start)
    monkeypatch.setenv("VICTORIQUE_CONFIG", os.path.join("conf", "victorique.toml"))
    # The runs folder is elsewhere, and the runs start there.
    server = GuiServer(tmp_path / "runs", port=0).start()
    try:
        undecided = get_json(server, "/api/options")["undecided_checks"]
        assert (undecided["default"], undecided["source"]) == ("repair", "settings file")
        status, _, body = call(server, "/api/runs", body={"statement": STATEMENT, "policy": "ladder",
                                                          "backend": "fake", "calls": 12, "hours": 1})
        assert status == 201, body
        name = json.loads(body)["name"]
        detail, _ = wait_finished(server, name)
        assert detail["status"] == "delivered", detail
        command = _request(server, name)["command"]
        assert command[command.index("--undecided-checks") + 1] == "repair"
        assert "--call-timeout-seconds" not in command
        # The per-call limit was passed by nobody: the run read it from the settings file.
        parameters = _parameters(server, name)
        assert parameters["call_timeout_seconds"] == 90 and parameters["undecided_checks"] == "repair"
    finally:
        try:
            assert server.app.launcher.wait(120)
        finally:
            server.stop()


class _UndecidedFake(FakeTransport):
    """The first proof check gives no verdict and keeps a doubt, or returns no valid report."""

    def __init__(self, kind):
        self.kind = kind

    def _response(self, request):
        value = json.loads(super()._response(request))
        binding = value["binding"]
        if binding["action_kind"] == "whole_proof_review" and ".L0." in binding["request_id"]:
            if self.kind == "incomplete":
                return b"invalid JSON"  # No valid report, also on its retry.
            value["output"].update(outcome="unknown", uncertainties=["Is the case n < 0 covered?"])
        return json.dumps(value).encode()


@pytest.mark.parametrize("policy", ["ladder", "resample"])
@pytest.mark.parametrize("kind", ["inconclusive", "incomplete"])
def test_undecided_checks_are_named_in_words(tmp_path, monkeypatch, policy, kind):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    problem = tmp_path / "problem.md"
    problem.write_text(STATEMENT, encoding="utf-8")
    with patch.object(api, "FakeTransport", return_value=_UndecidedFake(kind)), redirect_stdout(io.StringIO()):
        summary = api.solve(problem, policy=policy, transport="fake", out=tmp_path / "runs" / "undecided")
    assert summary["endpoint_reason"] == f"{policy}_check_{kind}"
    detail = runs.RunReader(tmp_path / "runs").detail("undecided")
    assert detail["status"] == "abstained"
    assert detail["reason"] == runs.REASON_TEXTS["check_" + kind]
    assert detail["undecided_checks_label"] == "Abstain"
    abstention = detail["abstention"]
    if kind == "inconclusive":
        assert abstention["inconclusive_checks"] == [{
            "request_id": policy + ".L0.review", "check": "whole_proof_review", "step": "Proof check",
            "note": "first attempt", "outcome": "unknown", "doubts": 1, "minor_findings": 0,
            "text": "Proof check (first attempt) gave no clear verdict (1 open question, 0 minor remarks)."}]
        assert abstention["incomplete_checks"] == []
        (check,) = [row for row in detail["checks"] if row["role"] == "proof_reviewer"]
        assert check["review"]["passed"] is False and check["review"]["undecided"] is True
    else:
        assert abstention["inconclusive_checks"] == []
        assert abstention["incomplete_checks"] == [policy + ".L0.review.retry"]


def test_inconclusive_check_sentences():
    assert runs.inconclusive_check({"request_id": "ladder.L1.1.review", "check": "whole_proof_review",
                                    "outcome": "fail", "doubts": 0, "minor_findings": 2})["text"] == (
        "Proof check (after repair round 1) said fail but reported no serious or fatal problem "
        "(0 open questions, 2 minor remarks).")
    assert runs.inconclusive_check({"request_id": "resample.L1.2.alignment", "check": "alignment_review",
                                    "outcome": "pass", "doubts": 2, "minor_findings": 1})["text"] == (
        "Statement check (fresh attempt 2) said pass but kept open questions (2 open questions, 1 minor remark).")
    assert runs.inconclusive_check("unreadable")["text"] == "A check ended without a clear verdict."


def test_reason_codes_of_earlier_runs_still_read_in_words(tmp_path):
    for policy in ("single", "ladder", "resample", "bestofn"):
        for suffix in ("check_inconclusive", "check_incomplete", "nonblocking_or_unknown"):
            assert runs.reason_text(f"{policy}_{suffix}") == runs.REASON_TEXTS[suffix]
    # A run made by 0.1.0b2 has the earlier code, no inconclusive_checks and neither setting.
    _run_files(tmp_path, "b2-run", summary={"outcome": "abstain", "endpoint_reason": "ladder_nonblocking_or_unknown",
                                            "unresolved_questions": ["Is n = 0 covered?"], "incomplete_checks": []})
    detail = runs.RunReader(tmp_path).detail("b2-run")
    assert detail["status"] == "abstained"
    assert detail["reason"] == "A check ended with open questions or without a clear verdict."
    assert detail["abstention"]["inconclusive_checks"] == []
    assert detail["abstention"]["unresolved_questions"] == ["Is n = 0 covered?"]
    assert detail["call_timeout_seconds"] is None and detail["undecided_checks_label"] is None


@pytest.mark.parametrize("isolated,flags", [(0, ["-P"]), (1, ["-I", "-P"])])
def test_child_commands_use_the_safe_path_flag(monkeypatch, isolated, flags):
    monkeypatch.setattr(launch, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=isolated),
                                                       executable=sys.executable))
    assert launch.python_command("python") == ["python", *flags]


def test_a_victorique_package_in_the_working_directory_changes_nothing(tmp_path, monkeypatch):
    work = tmp_path / "work"
    shadow = work / "victorique"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "__main__.py").write_text(
        "import pathlib, sys\n"
        "pathlib.Path(__file__).with_name('ran.txt').write_text(' '.join(sys.argv[1:]))\n"
        "print('usage: victorique solve [--shadow-timeout SECONDS] [--undecided-checks MODE]')\n",
        encoding="utf-8")
    monkeypatch.chdir(work)
    started = []
    popen = subprocess.Popen

    def recorded(args, **kwargs):
        started.append((list(args), kwargs.get("cwd")))
        return popen(args, **kwargs)
    monkeypatch.setattr(launch.subprocess, "Popen", recorded)
    # The runs folder is the working directory too, so the run's own process starts there.
    server = GuiServer(".", port=0).start()
    try:
        assert server.app.timeout_option == "--call-timeout-seconds"
        assert server.app.undecided_option == "--undecided-checks"
        status, _, body = call(server, "/api/runs", body={"statement": STATEMENT, "policy": "single",
                                                          "backend": "fake", "calls": 3, "hours": 1})
        assert status == 201, body
        detail, _ = wait_finished(server, json.loads(body)["name"])
        assert detail["status"] == "delivered", detail
    finally:
        try:
            assert server.app.launcher.wait(120)
        finally:
            server.stop()
    assert not (shadow / "ran.txt").exists()
    # One help call and one run, both with the safe-path flag and the runs folder as working directory.
    prefix = [*launch.python_command(sys.executable), "-m", "victorique"]
    assert "-P" in prefix
    (help_args, help_cwd), (run_args, run_cwd) = started
    assert help_args == prefix + ["solve", "--help"]
    assert run_args[:len(prefix) + 1] == prefix + ["solve"] and "--out" in run_args
    assert os.path.samefile(help_cwd, work) and os.path.samefile(run_cwd, work)


# ---- When a check passes with minor findings (the minor_findings setting) -------------------

MINOR_CHOICES = [{"value": "nonblocking", "label": "Count it as passed"},
                 {"value": "blocking", "label": "Count it as not passed"}]


def test_the_minor_findings_choice_applies_to_every_approach():
    assert launch.minor_findings_option({"--calls", "--minor-findings"}) == "--minor-findings"
    assert launch.minor_findings_option({"--calls", "--minor"}) is None
    for policy in ("single", "ladder", "full"):
        def chosen(default=None, **change):
            body = {"statement": STATEMENT, "backend": "fake", "policy": policy, **change}
            return parse_form(body, minor_option="--minor-findings", minor_default=default)[2]
        values = chosen(minor_findings="blocking")
        assert values["minor_findings"] == "blocking"
        assert launch.solve_arguments("p", "o", values, None, None, "--minor-findings")[-2:] == [
            "--minor-findings", "blocking"]
        # A form without a choice passes the preselected value, else nonblocking.
        assert chosen()["minor_findings"] == "nonblocking"
        assert chosen(minor_findings=None, default="blocking")["minor_findings"] == "blocking"
        assert chosen(minor_findings="nonblocking", default="blocking")["minor_findings"] == "nonblocking"
    # A solve command without the option gets nothing, and only its own rule can be chosen.
    _, _, older = parse_form({"statement": STATEMENT, "backend": "fake", "minor_findings": "nonblocking"})
    assert older["minor_findings"] is None
    assert "--minor-findings" not in launch.solve_arguments("p", "o", older)
    with pytest.raises(ValueError):
        launch.solve_arguments("p", "o", {**older, "minor_findings": "blocking"})
    with pytest.raises(FormError, match="always counts a check that passes with minor findings as passed"):
        parse_form({"statement": STATEMENT, "backend": "fake", "minor_findings": "blocking"})
    for value in ("BLOCKING", "sometimes", ["blocking"]):
        with pytest.raises(FormError, match="passes with minor findings"):
            parse_form({"statement": STATEMENT, "backend": "fake", "minor_findings": value},
                       minor_option="--minor-findings")


def test_the_minor_findings_choice_is_preselected_from_the_settings_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False)
    try:
        app = server.app
        # Without --minor-findings in solve --help the field is inactive.
        assert app.options()["minor_findings"] == {
            "configurable": False, "option": None, "default": "nonblocking", "source": "built-in",
            "settings_file": None, "settings_error": None, "choices": MINOR_CHOICES}
        config = tmp_path / "victorique.toml"
        config.write_text('minor_findings = "blocking"\n', encoding="utf-8")
        monkeypatch.setenv("VICTORIQUE_CONFIG", str(config))
        assert app.options()["defaults"]["minor_findings"] == "nonblocking"
        app.minor_option = "--minor-findings"
        options = app.options()
        assert options["defaults"]["minor_findings"] == "blocking"
        assert options["minor_findings"] == {
            "configurable": True, "option": "--minor-findings", "default": "blocking", "source": "settings file",
            "settings_file": str(config), "settings_error": None, "choices": MINOR_CHOICES}
        # The undecided choice keeps its own default.
        assert options["defaults"]["undecided_checks"] == "abstain"
        # A file without the key, or a file that cannot be read, leaves the built-in value.
        config.write_text("call_timeout_seconds = 90\n", encoding="utf-8")
        assert app.minor_findings_setting() == {"default": "nonblocking", "source": "built-in",
                                                "settings_file": str(config), "settings_error": None}
        for text in ("minor_findings = 'sometimes'\n", "not toml [\n", None):
            broken = tmp_path / "broken.toml"
            broken.unlink(missing_ok=True)
            if text is not None:
                broken.write_text(text, encoding="utf-8")
            monkeypatch.setenv("VICTORIQUE_CONFIG", str(broken))
            setting = app.minor_findings_setting()
            assert setting["default"] == "nonblocking" and setting["source"] == "built-in"
            assert "broken.toml" in setting["settings_error"], text
    finally:
        server.httpd.server_close()


def test_the_page_has_the_minor_findings_field():
    static = files("victorique.gui").joinpath("static")
    page = static.joinpath("index.html").read_text(encoding="utf-8")
    assert '<label for="minor">When a check passes with minor findings</label>' in page
    assert '<select id="minor" name="minor_findings" disabled>' in page
    assert '<option value="nonblocking" selected>' in page and '<option value="blocking">' in page
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    assert "options.minor_findings" in script and "minor_findings: document.getElementById(\"minor\")" in script
    assert "When a check passes with minor findings" in script


def test_the_minor_findings_choice_reaches_the_solve_command(gui, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    assert gui.app.minor_option == "--minor-findings"
    options = get_json(gui, "/api/options")
    assert options["minor_findings"]["configurable"] is True
    assert options["minor_findings"]["choices"] == MINOR_CHOICES
    assert options["defaults"]["minor_findings"] == "nonblocking"
    status, _, body = call(gui, "/api/runs", body={"statement": STATEMENT, "policy": "single", "backend": "fake",
                                                   "minor_findings": "blocking", "calls": 3, "hours": 1})
    assert status == 201, body
    name = json.loads(body)["name"]
    detail, _ = wait_finished(gui, name)
    # The fake checks list no finding, so the proof is delivered under either value.
    assert detail["status"] == "delivered", detail
    command = _request(gui, name)["command"]
    assert command[command.index("--minor-findings") + 1] == "blocking"
    assert _parameters(gui, name)["minor_findings"] == "blocking"
    assert (detail["minor_findings"], detail["minor_findings_label"]) == ("blocking", "Count it as not passed")


@pytest.mark.parametrize("value", ["nonblocking", "blocking"])
def test_the_run_page_reads_checks_with_the_recorded_setting(tmp_path, monkeypatch, value):
    from tests.test_minor_findings import MinorNote, NOTE
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    problem = tmp_path / "problem.md"
    problem.write_text(STATEMENT, encoding="utf-8")
    with patch.object(api, "FakeTransport", return_value=MinorNote()), redirect_stdout(io.StringIO()):
        api.solve(problem, policy="single", transport="fake", out=tmp_path / "runs" / "minor", minor_findings=value)
    detail = runs.RunReader(tmp_path / "runs").detail("minor")
    blocking = value == "blocking"
    assert detail["status"] == ("abstained" if blocking else "delivered")
    assert detail["minor_findings_label"] == ("Count it as not passed" if blocking else "Count it as passed")
    checks = [row["review"] for row in detail["checks"]]
    assert sorted(row["step"] for row in detail["checks"]) == ["Proof check", "Statement check"]
    for check in checks:
        # A check that says pass with a minor finding: Passed, or under blocking Did not pass (not Undecided).
        assert check["outcome"] == "pass" and check["passed"] is (not blocking) and check["undecided"] is False
        assert [(item["claim"], item["severity"], item["blocking"]) for item in check["findings"]] == [
            (NOTE, "minor", blocking)]
    if blocking:
        assert sorted(call["step"] for call in detail["abstention"]["failed_checks"]) == ["Proof check",
                                                                                          "Statement check"]
    # A run recorded before the setting shows no value and reads its checks as nonblocking.
    _run_files(tmp_path / "runs", "earlier", summary={"outcome": "abstain", "endpoint_reason": "ladder_budget"})
    assert runs.RunReader(tmp_path / "runs").detail("earlier")["minor_findings_label"] is None
    report = {"schema": "victorique.adaptive_gate.v1", "outcome": "pass", "uncertainties": [],
              "findings": [{"severity": "minor", "claim": NOTE}]}
    assert runs.review(report)["passed"] is True and runs.review(report, "unknown")["passed"] is True
    assert runs.review(report, "blocking")["passed"] is False


# ---- the check service and the retries after a call timeout ------------------------


def test_options_list_the_check_services_and_the_retries_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False)
    try:
        app = server.app
        # A solve command without the options keeps the solver's service and repeats nothing.
        options = app.options()
        assert options["checks"] == {"configurable": False, "options": None}
        assert options["timeout_retries"]["configurable"] is False and options["timeout_retries"]["default"] == 1
        assert [(item["value"], item["label"]) for item in options["check_backends"]] == [
            ("", "Same as the solver"), ("codex", "Codex CLI"), ("claude", "Claude Code CLI"),
            ("fake", "Practice run (no model)")]
        app.retries_option, app.check_options = "--timeout-retries", dict(launch.CHECK_OPTIONS)
        options = app.options()
        assert options["checks"]["configurable"] is True
        assert {key: options["timeout_retries"][key] for key in ("configurable", "option", "default", "source",
                                                                 "range")} == {
            "configurable": True, "option": "--timeout-retries", "default": 1, "source": "built-in", "range": [0, 3]}
        assert options["defaults"]["timeout_retries"] == 1 and options["defaults"]["check_backend"] == ""
        # The default comes from the settings file, as the solve command's own does.
        config = tmp_path / "victorique.toml"
        config.write_text("timeout_retries = 2\n", encoding="utf-8")
        monkeypatch.setenv("VICTORIQUE_CONFIG", str(config))
        options = app.options()
        assert (options["timeout_retries"]["default"], options["timeout_retries"]["source"]) == (2, "settings file")
        assert options["defaults"]["timeout_retries"] == 2
        config.write_text("timeout_retries = 9\n", encoding="utf-8")
        setting = app.retries_setting()
        assert setting["default"] == 1 and "timeout_retries" in setting["settings_error"]
    finally:
        server.httpd.server_close()


def test_check_and_retry_fields_become_solve_arguments(tmp_path):
    detected = {"retries_option": "--timeout-retries", "check_options": dict(launch.CHECK_OPTIONS)}
    _, _, values = parse_form({"statement": STATEMENT, "backend": "codex", "check_backend": "claude",
                               "check_model": " claude-x ", "timeout_retries": "2"}, retries_default=1, **detected)
    assert {key: values[key] for key in ("check_backend", "check_model", "check_reasoning_effort",
                                         "timeout_retries")} == {
        "check_backend": "claude", "check_model": "claude-x", "check_reasoning_effort": None, "timeout_retries": 2}
    arguments = launch.solve_arguments("p", "o", values, **detected)
    assert arguments[-6:] == ["--check-transport", "claude", "--check-model", "claude-x", "--timeout-retries", "2"]
    # Same as the solver passes no check option; an empty retries field passes the preselected default.
    _, _, values = parse_form({"statement": STATEMENT, "backend": "codex", "check_reasoning_effort": "high",
                               "timeout_retries": ""}, retries_default=3, **detected)
    assert values["check_backend"] is None and values["timeout_retries"] == 3
    arguments = launch.solve_arguments("p", "o", values, **detected)
    assert "--check-transport" not in arguments
    assert arguments[-4:] == ["--check-reasoning-effort", "high", "--timeout-retries", "3"]
    _, _, values = parse_form({"statement": STATEMENT, "backend": "fake", "check_backend": "fake",
                               "timeout_retries": 0}, **detected)
    assert launch.solve_arguments("p", "o", values, **detected)[-4:] == [
        "--check-transport", "fake", "--timeout-retries", "0"]
    # Without the options the form passes none of these.
    _, _, older = parse_form({"statement": STATEMENT, "backend": "codex"})
    assert older["timeout_retries"] is None and older["check_backend"] is None
    assert "--timeout-retries" not in launch.solve_arguments("p", "o", older)
    with pytest.raises(ValueError):
        launch.solve_arguments("p", "o", {**older, "timeout_retries": 1})
    with pytest.raises(ValueError):
        launch.solve_arguments("p", "o", {**older, "check_backend": "claude"})


@pytest.mark.parametrize("change,message", [
    ({"check_backend": "claude", "check_reasoning_effort": "high"}, "only when the checks use the Codex CLI"),
    ({"backend": "claude", "check_reasoning_effort": "high"}, "only when the checks use the Codex CLI"),
    ({"check_backend": "fake"}, "practice run"),
    ({"backend": "fake", "check_backend": "codex"}, "practice run"),
    ({"check_backend": "gemini"}, "model service for the checks"),
    ({"check_model": "two words"}, "check model name"),
    ({"check_reasoning_effort": "extreme"}, "check reasoning effort from the list"),
    ({"timeout_retries": "4"}, "Retries after a call timeout must be a whole number from 0 to 3"),
    ({"timeout_retries": "-1"}, "Retries after a call timeout"),
])
def test_check_and_retry_fields_are_checked(change, message):
    with pytest.raises(FormError, match=message):
        parse_form({"statement": STATEMENT, "backend": "codex", **change}, retries_option="--timeout-retries",
                   check_options=dict(launch.CHECK_OPTIONS))


@pytest.mark.parametrize("change,message", [
    ({"check_backend": "claude"}, "checks with the solver's model service"),
    ({"check_model": "x"}, "checks with the solver's model service"),
    ({"timeout_retries": "1"}, "does not repeat a call"),
])
def test_an_older_solve_command_refuses_the_new_fields(change, message):
    with pytest.raises(FormError, match=message):
        parse_form({"statement": STATEMENT, "backend": "codex", **change})


def test_new_option_names_are_read_from_the_solve_help():
    assert launch.timeout_retries_option({"--calls", "--timeout-retries"}) == "--timeout-retries"
    assert launch.timeout_retries_option({"--calls"}) is None
    assert launch.check_options({"--check-transport", "--check-model", "--check-reasoning-effort"}) == {
        "transport": "--check-transport", "model": "--check-model", "effort": "--check-reasoning-effort"}
    assert launch.check_options({"--check-transport", "--check-model"}) is None


def test_this_solve_command_lists_the_new_options(gui):
    assert gui.app.retries_option == "--timeout-retries"
    assert gui.app.check_options == launch.CHECK_OPTIONS


def test_the_form_has_the_checks_group_and_the_retries_field():
    static = files("victorique.gui").joinpath("static")
    page = static.joinpath("index.html").read_text(encoding="utf-8")
    for identity in ("check-backend", "check-model", "check-effort", "timeout-retries"):
        assert f'id="{identity}"' in page
    check_select = page[page.index('id="check-backend"'):].split("</select>")[0]
    assert re.findall(r'<option value="(\w*)"', check_select) == ["", "codex", "claude", "fake"]
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    # The Claude Code help names the Console account login, for solving and for the checks.
    assert "Log Claude Code in with an Anthropic Console account" in script and script.count("CLAUDE_LOGIN") >= 3
    assert "Victorique removes ANTHROPIC_API_KEY from the Claude Code process environment" in script


def test_a_form_run_records_the_retries_and_the_check_service(gui, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    status, _, body = call(gui, "/api/runs", body={
        "statement": STATEMENT, "backend": "fake", "check_backend": "fake", "timeout_retries": "2",
        "calls": 3, "hours": 1})
    assert status == 201, body
    name = json.loads(body)["name"]
    detail, _ = wait_finished(gui, name)
    assert detail["status"] == "delivered", detail
    command = _request(gui, name)["command"]
    assert command[command.index("--timeout-retries") + 1] == "2"
    assert command[command.index("--check-transport") + 1] == "fake"
    parameters = _parameters(gui, name)
    assert parameters["policy"] == "single" and parameters["timeout_retries"] == 2
    assert parameters["check_transport"] == "fake"
    assert (detail["timeout_retries"], detail["timeout_retries_used"]) == (2, 0)
    assert detail["check_separate"] is False and detail["check_backend_label"] is None


def _run_with_check_service(root, name, parameters, summary=None, calls=()):
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "invocation.json").write_text(json.dumps({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {"policy": "single", "transport": "codex", "hours": 1, "calls": 12, "model": None,
                       **parameters}}), encoding="utf-8")
    if summary is not None:
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "calls.jsonl").write_text("".join(json.dumps(row) + "\n" for row in calls), encoding="utf-8")


def test_run_page_names_a_separate_check_service_and_the_retries(tmp_path):
    rows = [{"request_id": "single.solve", "role": "solver", "status": "completed", "service": "solver",
             "retry_of": None},
            {"request_id": "single.alignment", "role": "alignment_reviewer", "status": "completed",
             "service": "checks", "retry_of": None},
            {"request_id": "single.review", "role": "proof_reviewer", "status": "timed_out", "service": "checks",
             "retry_of": None},
            {"request_id": "single.review.timeout-retry-1", "role": "proof_reviewer", "status": "completed",
             "service": "checks", "retry_of": "single.review"}]
    _run_with_check_service(tmp_path, "routed", {"check_transport": "claude", "check_model": None,
                                                 "check_reasoning_effort": None, "timeout_retries": 1},
                            summary={"outcome": "complete", "endpoint_reason": "single_complete", "calls_used": 4,
                                     "timeout_retries_used": 1,
                                     "check_service": {"transport": "claude", "requested_model": None,
                                                       "requested_reasoning_effort": None,
                                                       "reported_model": "claude-opus-5-5", "calls": 3}},
                            calls=rows)
    detail = runs.RunReader(tmp_path).detail("routed", lambda *args: False)
    assert detail["check_separate"] is True
    assert (detail["check_backend"], detail["check_backend_label"], detail["check_reported_model"]) == (
        "claude", "Claude Code CLI", "claude-opus-5-5")
    assert (detail["timeout_retries"], detail["timeout_retries_used"]) == (1, 1)
    assert [(call["service"], call["note"]) for call in detail["calls"]] == [
        ("solver", None), ("checks", None), ("checks", None), ("checks", "sent again after a timeout")]
    assert detail["calls"][2]["status_label"] == "Timed out"
    # Checks on the solver's service, and runs recorded before these settings, show neither.
    _run_with_check_service(tmp_path, "same", {"check_transport": "codex", "check_model": None,
                                               "check_reasoning_effort": None, "timeout_retries": 0},
                            summary={"outcome": "complete", "check_service": None, "timeout_retries_used": 0})
    _run_with_check_service(tmp_path, "older", {}, summary={"outcome": "complete"})
    same = runs.RunReader(tmp_path).detail("same", lambda *args: False)
    older = runs.RunReader(tmp_path).detail("older", lambda *args: False)
    assert same["check_separate"] is False and (same["timeout_retries"], same["timeout_retries_used"]) == (0, 0)
    assert older["check_separate"] is False and older["timeout_retries"] is None
    assert runs.PHASE_LABELS["call_timed_out"].startswith("reached its time limit")


# ---- Stop run and Resume --------------------------------------------------------------------


def test_the_stop_endpoint_checks_each_request_and_records_a_stop(tmp_path):
    from victorique import stop_request
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        root = server.app.runs_dir
        _run_files(root, "live", console="whole_proof_review | proof_reviewer | 3/12 | original_prepared_dispatched\n")
        _run_files(root, "done", summary={"outcome": "complete", "endpoint_reason": "single_complete"})
        starting = root / runs.GUI_FOLDER / "starting"
        starting.mkdir(parents=True)
        (starting / "request.json").write_text(json.dumps({"name": "starting", "title": None}), encoding="utf-8")
        before = get_json(server, "/api/runs/live")
        assert (before["status"], before["can_stop"], before["stop_requested"]) == ("running", True, False)
        # The same Host, Origin and token checks as the form; nothing is written when one fails.
        stop = "/api/runs/live/stop"
        assert call(server, stop, body={}, token=False)[0] == 403
        assert call(server, stop, body={}, headers={"X-Victorique-Token": "wrong"})[0] == 403
        assert call(server, stop, body={}, headers={"Origin": "http://example.org"})[0] == 403
        assert call(server, stop, body={}, headers={"Host": f"example.org:{server.port}"})[0] == 403
        assert call(server, stop)[0] == 404 and call(server, stop, method="DELETE")[0] == 405
        assert call(server, "/api/runs/missing/stop", body={})[0] == 404
        assert call(server, "/api/runs/live/pause", body={})[0] == 404
        assert not (root / "live" / stop_request.FILE).exists()
        # The request is recorded with the function of victorique stop: stop-request.json in the run folder.
        status, _, body = call(server, stop, body={}, headers={"Origin": f"http://127.0.0.1:{server.port}"})
        assert status == 200, body
        result = json.loads(body)
        assert result["status"] == "requested" and result["output_directory"] == str(root / "live")
        recorded = json.loads((root / "live" / stop_request.FILE).read_text(encoding="utf-8"))
        assert recorded == {"schema": stop_request.SCHEMA, "requested_at": result["requested_at"]}
        detail = get_json(server, "/api/runs/live")
        assert (detail["status"], detail["status_label"], detail["can_stop"]) == ("stopping", "Stopping", False)
        assert detail["stop_requested"] is True and detail["stop_requested_at"] == result["requested_at"]
        assert detail["finished"] is False and detail["progress"]["step"] == "Proof check"
        status, _, body = call(server, stop, body={})
        assert status == 200 and json.loads(body)["status"] == "already_requested"
        # A finished run and a run without a folder yet take no request.
        status, _, body = call(server, "/api/runs/done/stop", body={})
        assert status == 409 and "already finished" in json.loads(body)["error"]
        status, _, body = call(server, "/api/runs/starting/stop", body={})
        assert status == 409 and "not created its folder" in json.loads(body)["error"]
        assert not (root / "done" / stop_request.FILE).exists() and not (root / "starting").exists()
    finally:
        server.stop()


def test_run_page_labels_of_a_stopped_run(tmp_path):
    _run_files(tmp_path, "stopped", summary={"outcome": "partial", "endpoint_reason": "stopped_by_user",
                                             "proof_available": True, "calls_used": 2})
    (tmp_path / "stopped" / "proof.md").write_text("# Draft\n\nn + 0 = n\n", encoding="utf-8")
    (tmp_path / "stopped" / "stop-request.json").write_text(
        json.dumps({"requested_at": "2026-11-19T10:00:00+00:00"}), encoding="utf-8")
    (tmp_path / "stopped" / "calls.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
        {"request_id": "ladder.L0.solve", "role": "solver", "status": "completed"},
        {"request_id": "ladder.L0.alignment", "role": "alignment_reviewer", "status": "stopped"})), encoding="utf-8")
    detail = runs.RunReader(tmp_path).detail("stopped", lambda *args: False)
    assert (detail["status"], detail["status_label"], detail["finished"]) == ("stopped", "Stopped by you", True)
    assert detail["reason"].startswith("You stopped the run.") and "best draft" in detail["reason"]
    assert detail["endpoint_reason"] == "stopped_by_user" and detail["abstention"]["reason_code"] == "stopped_by_user"
    assert detail["proof_kind"] == "draft" and "n + 0 = n" in detail["proof"]
    assert [call["status_label"] for call in detail["calls"]] == ["Done", "Stopped"]
    assert detail["can_stop"] is False and detail["can_resume"] is False
    assert detail["stop_requested_at"] == "2026-11-19T10:00:00+00:00"
    # The page shows these words with Chinese captions and marker shapes of their own, and has the buttons.
    static = files("victorique.gui").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    style = static.joinpath("style.css").read_text(encoding="utf-8")
    for text in ('stopping: "停止中"', 'stopped: "已停止"', '"Stop run"', '"Stop the run"', '"Keep running"',
                 '"Resume"', "The current model call is abandoned, the run ends", "without a proof, and the best "
                 "draft is kept.", '"/" + action'):
        assert text in script, text
    for selector in (".status-stopping::before", ".status-stopped::before", ".call-stopped::before",
                     "button.stop::before", ".run-actions"):
        assert selector in style, selector
    assert "Stop run" in static.joinpath("index.html").read_text(encoding="utf-8")


def test_run_page_captions_use_the_short_wording():
    script = files("victorique.gui").joinpath("static").joinpath("app.js").read_text(encoding="utf-8")
    for text in ('cap("已交付证明")', '"最佳草稿（未交付）"', 'cap: "发现问题"', 'cap: "未发现问题"',
                 'returned_invalid: "回答无效"', 'unrecorded: "无回答"', 'cap: "无结论"'):
        assert text in script, text
    for old in ("交付的证明", "报告问题", "回答无法使用", "无回答记录", "无明确结论", "最佳草稿，未交付"):
        assert old not in script, old


def test_a_run_whose_process_ended_without_a_summary_resumes_from_its_page(tmp_path, monkeypatch):
    started = []
    popen = subprocess.Popen

    def recorded(args, **kwargs):
        started.append((list(args), kwargs.get("cwd")))
        return popen(args, **kwargs)
    monkeypatch.setattr(launch.subprocess, "Popen", recorded)
    server = GuiServer(tmp_path / "runs", port=0, detect_options=False).start()
    try:
        root = server.app.runs_dir
        problem = tmp_path / "problem.md"
        problem.write_text("Prove that n + 0 = n for every integer n.", encoding="utf-8")
        # The run folder exists, but its process ended (exit.json) before it wrote a summary.
        with redirect_stdout(io.StringIO()):
            api._create_run(problem, transport="fake", out=root / "halted")
        _run_files(root, "other", summary={"outcome": "abstain", "endpoint_reason": "single_review_not_passed"})
        gui_dir = root / runs.GUI_FOLDER / "halted"
        gui_dir.mkdir(parents=True)
        (gui_dir / "request.json").write_text(json.dumps({"name": "halted", "title": "Halted run", "pid": None}),
                                              encoding="utf-8")
        (gui_dir / "console.log").write_text("solve_obligation | solver | 0/12 | native_prepared\n", encoding="utf-8")
        (gui_dir / "exit.json").write_text(json.dumps({"exit_code": 1}), encoding="utf-8")
        before = get_json(server, "/api/runs/halted")
        assert (before["status"], before["can_resume"], before["can_stop"]) == ("interrupted", True, False)
        resume = "/api/runs/halted/resume"
        assert call(server, resume, body={}, token=False)[0] == 403
        assert call(server, resume, body={}, headers={"Origin": "http://example.org"})[0] == 403
        assert started == []
        status, _, body = call(server, resume, body={})
        assert status == 200 and json.loads(body) == {"name": "halted"}
        detail, statuses = wait_finished(server, "halted")
        assert detail["status"] == "delivered", detail
        assert detail["title"] == "Halted run" and detail["calls_used"] == 3
        assert any(line.startswith("victorique resume started from the interface") for line in detail["console"])
        # victorique resume ran like a form's solve: python -P, the runs folder as working directory.
        ((args, cwd),) = started
        assert args == [*launch.python_command(sys.executable), "-m", "victorique", "resume", str(root / "halted")]
        assert "-P" in args and os.path.samefile(cwd, root)
        request = json.loads((gui_dir / "request.json").read_text(encoding="utf-8"))
        assert request["resume_command"] == ["victorique", "resume", str(root / "halted")]
        assert type(request["pid"]) is int and len(request["process_started"]) == 2
        # Only a run whose process ended without a summary can be resumed here.
        status, _, body = call(server, resume, body={})
        assert status == 409 and "without writing a summary" in json.loads(body)["error"]
        assert call(server, "/api/runs/other/resume", body={})[0] == 409
        assert len(started) == 1
    finally:
        try:
            assert server.app.launcher.wait(120)
        finally:
            server.stop()
