import json
import subprocess
import sys

import pytest

from victorique.harness.codex_defaults import read_cli_defaults


@pytest.mark.parametrize("mode", ["success", "rejected", "timeout", "bad_settings"])
def test_public_defaults_without_inference_or_config_leak(tmp_path, monkeypatch, mode):
    script = tmp_path / "public_server.py"
    transcript = tmp_path / "methods.jsonl"
    script.write_text('''import json, sys, time
mode, transcript = sys.argv[1:]
for line in sys.stdin:
    message = json.loads(line)
    with open(transcript, "a") as f:
        f.write(json.dumps(message) + "\\n")
    method = message["method"]
    if method == "initialize":
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif method == "initialized":
        continue
    elif method == "config/read":
        if mode == "timeout":
            time.sleep(5)
        elif mode == "rejected":
            print(json.dumps({"id": 2, "error": {"message": "SECRET_DIAGNOSTIC"}}), flush=True)
        else:
            config = {"model": "fixture-model", "model_reasoning_effort": "high",
                      "unrelated_secret_fixture": "SECRET_CONFIG"}
            if mode == "bad_settings":
                config["model"] = {"unexpected": "SECRET_CONFIG"}
            print(json.dumps({"id": 2, "result": {"config": config}}), flush=True)
    else:
        raise AssertionError("inference or unsupported request")
''', encoding="utf-8")
    popen = subprocess.Popen
    launched = []
    def server(argv, **kwargs):
        assert argv == ["codex-fixture", "app-server", "--stdio", "--disable", "plugins", "--disable", "apps"]
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = popen([sys.executable, "-I", "-B", str(script), mode, str(transcript)], **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", server)
    result = read_cli_defaults("codex-fixture", tmp_path, timeout=.5 if mode == "timeout" else 5)
    assert len(launched) == 1 and launched[0].poll() is not None
    assert "SECRET" not in json.dumps(result)
    assert result["source"] == "cli_default_config"
    methods = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [row["method"] for row in methods] == ["initialize", "initialized", "config/read"]
    assert methods[-1]["params"] == {"includeLayers": False, "cwd": str(tmp_path)}
    if mode == "success":
        assert result["status"] == "observed"
        assert result["model"] == "fixture-model" and result["effort"] == "high"
    else:
        assert result["status"] == "unavailable"
        assert result["model"] is result["effort"] is None


def test_missing_native_cli_leaves_defaults_unknown(tmp_path):
    result = read_cli_defaults(tmp_path / "missing-cli.exe", tmp_path)
    assert result["status"] == "unavailable"
    assert result["model"] is result["effort"] is None
