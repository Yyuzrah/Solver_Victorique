"""Read public CLI defaults without creating a model session or saving config."""
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Thread
from time import monotonic


def read_cli_defaults(binary, cwd, timeout=10):
    """Return only model/effort from app-server config/read.

    These are configuration values, separate from model identity reported by an
    actual call. Unsupported CLI interfaces leave them unknown. No inference
    requests, environment values, full config, or diagnostic streams are saved.
    """
    result = {"model": None, "effort": None, "source": "cli_default_config",
              "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "unavailable"}
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise ValueError("config probe timeout must be in (0, 30] seconds")
    proc, reader = None, None
    inbox = Queue()
    deadline = monotonic() + timeout

    def receive_lines(stream):
        total = 0
        try:
            while True:
                line = stream.readline(1024 * 1024 + 1)
                if not line:
                    inbox.put(None)
                    return
                total += len(line)
                if total > 1024 * 1024:
                    raise ValueError("public config response exceeded limit")
                inbox.put(json.loads(line))
        except (OSError, ValueError):
            inbox.put(None)

    def send(value):
        proc.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def response(identifier):
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                message = inbox.get(timeout=remaining)
            except Empty as error:
                raise TimeoutError from error
            if type(message) is not dict:
                raise ValueError("public config response unavailable")
            if message.get("id") == identifier:
                if "error" in message or type(message.get("result")) is not dict:
                    raise ValueError("public config request rejected")
                return message["result"]

    try:
        proc = subprocess.Popen(
            [str(binary), "app-server", "--stdio", "--disable", "plugins", "--disable", "apps"],
            cwd=str(Path(cwd).absolute()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        reader = Thread(target=receive_lines, args=(proc.stdout,), daemon=True)
        reader.start()
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "victorique_config_probe", "version": "0.1.0"}}})
        response(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "config/read", "params": {
            "includeLayers": False, "cwd": str(Path(cwd).absolute())}})
        config = response(2).get("config")
        if type(config) is not dict:
            raise ValueError("public config missing")
        for destination, source in (("model", "model"), ("effort", "model_reasoning_effort")):
            value = config.get(source)
            if value is not None and (type(value) is not str or not value.strip() or len(value) > 200):
                raise ValueError("invalid public model setting")
            result[destination] = value
        result["status"] = "observed"
    except (OSError, ValueError, TimeoutError):
        result.update(model=None, effort=None, reason="public_config_unavailable")
    finally:
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            if reader is not None:
                reader.join(timeout=1)
            proc.stdout.close()
    return result
