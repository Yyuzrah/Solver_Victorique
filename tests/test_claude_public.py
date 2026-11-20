"""Exercise public Claude wiring using only a fixed Python print subprocess."""
import json
from pathlib import Path
import sys
from unittest import mock

import pytest

from victorique import api, cli, verify
from victorique.fake import FakeTransport
from victorique.harness.claude_transport import ClaudeCliTransport
from tests.test_claude_contract import public_profile


@pytest.fixture
def inputs(tmp_path):
    problem, proof = tmp_path / "problem.md", tmp_path / "proof.md"
    problem.write_text("Prove n + 0 = n for every integer n.")
    proof.write_text("The additive identity gives n + 0 = n.")
    return problem, proof


@pytest.fixture
def offline_claude(monkeypatch):
    monkeypatch.setattr(api, "inspect_host", lambda *a, **k: public_profile())
    monkeypatch.setattr(verify, "inspect_host", lambda *a, **k: public_profile())
    def no_mcp(*args):
        raise AssertionError("Claude must not query Codex MCP inventory")
    monkeypatch.setattr(api, "_mcp_names", no_mcp)
    monkeypatch.setattr(verify, "_mcp_names", no_mcp)
    invocations = []
    original = ClaudeCliTransport._invocation
    def fake_invocation(self, request, binding):
        invocation = original(self, request, binding)
        invocations.append((request, dict(invocation)))
        envelope = json.loads(FakeTransport()._response(request))
        rows = [
            {"type": "system", "subtype": "init", "session_id": request.job_id, "model": "offline-claude",
             "tools": [], "mcp_servers": []},
            {"type": "result", "subtype": "success", "is_error": False, "session_id": request.job_id,
             "structured_output": envelope,
             "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2}}]
        data = "\n".join(json.dumps(row) for row in rows)
        script = ("import os,sys; sys.stdin.buffer.read(); "
                  "assert os.environ['CLAUDE_CODE_DISABLE_CLAUDE_MDS']=='1'; "
                  "assert 'ANTHROPIC_API_KEY' not in os.environ; print(" + repr(data) + ")")
        return invocation | {"argv": [sys.executable, "-I", "-c", script], "timeout_seconds": 5}
    monkeypatch.setattr(ClaudeCliTransport, "_invocation", fake_invocation)
    return invocations


def test_cli_single_claude_uses_serial_external_workspaces_and_records_usage(tmp_path, inputs, offline_claude, monkeypatch):
    problem, _ = inputs
    out = tmp_path / "single"
    argv = ["victorique", "solve", str(problem), "--transport", "claude", "--policy", "single", "--calls", "3", "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    summary = api.status(out)
    assert summary["outcome"] == "complete"
    assert summary["calls_used"] == 3
    assert summary["reported_model"] == "offline-claude"
    assert summary["tokens"]["input_tokens"] == 21
    assert summary["tokens"]["cached_input_tokens"] == 6
    invocation = json.loads((out / "invocation.json").read_text())
    assert invocation["argv"] == argv
    config = json.loads((out / "state" / "run-config.json").read_text())
    assert config["manifest"]["limits"]["max_inflight"] == 1
    assert config["transport"]["authorization"]["max_process_starts"] == 3
    assert config["transport"]["authorization"]["expires_at_utc"] == summary["deadline_utc"]
    workspaces = [Path(inv["cwd"]) for _, inv in offline_claude]
    assert len(workspaces) == len(set(workspaces)) == 3
    for _, inv in offline_claude:
        assert not Path(inv["cwd"]).is_relative_to(out)
        assert not Path(inv["cwd"]).is_relative_to(Path.cwd())
        assert inv["argv"][inv["argv"].index("--tools") + 1] == ""
        assert "--safe-mode" in inv["argv"]
        assert inv["deadline_utc"] == summary["deadline_utc"]
    assert all(not p.exists() for p in workspaces)
    rows = [json.loads(row) for row in (out / "calls.jsonl").read_text().splitlines()]
    assert all(row["started_at_utc"] and row["ended_at_utc"] for row in rows)
    assert api.resume(out) == summary
    assert len(offline_claude) == 3


def test_cli_verify_claude_and_resume_preserve_original_request(tmp_path, inputs, offline_claude, monkeypatch):
    problem, proof = inputs
    out = tmp_path / "verify"
    monkeypatch.setattr(sys, "argv", ["victorique", "verify", str(proof), "--problem", str(problem),
                                      "--transport", "claude", "--calls", "2", "--out", str(out)])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    result = verify.resume_verify(out)
    assert result["outcome"] == "pass"
    assert result["calls_used"] == 1
    assert result["reported_model"] == "offline-claude"
    assert result["tokens"]["input_tokens"] == 7
    assert len(offline_claude) == 1
    assert offline_claude[0][0].packet["context"]["target_candidate"]["content"] == proof.read_text()


@pytest.mark.parametrize("policy", ["full", "ladder"])
def test_other_policies_prepare_the_live_claude_adapter(tmp_path, inputs, offline_claude, policy):
    out = tmp_path / policy
    with mock.patch.object(api, "_run", side_effect=lambda directory, loop: loop):
        loop = api.solve(inputs[0], transport="claude", policy=policy, out=out, calls=3)
    assert isinstance(loop.adapter, ClaudeCliTransport)
    assert loop.config["manifest"]["mode"] == "host_recorded"
    assert loop.config["manifest"]["limits"]["max_inflight"] == 1
    assert loop.config["execution_grant"]["adapter_id"] == "claude_cli"
    assert not offline_claude


def test_unknown_claude_response_observes_same_job_without_retry(tmp_path, inputs, offline_claude, monkeypatch):
    original = ClaudeCliTransport._invocation
    def malformed(self, request, binding):
        invocation = original(self, request, binding)
        return invocation | {"argv": [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(); print('incomplete')"]}
    monkeypatch.setattr(ClaudeCliTransport, "_invocation", malformed)
    out = tmp_path / "unknown"
    first = api.solve(inputs[0], transport="claude", policy="single", out=out, calls=3)
    assert first["calls_used"] == 1
    assert first["outcome"] == "pending"
    again = api.resume(out)
    assert again["calls_used"] == 1
    assert len(offline_claude) == 1


def test_missing_claude_isolation_flag_refuses_before_creating_run(tmp_path, inputs, monkeypatch):
    profile = public_profile()
    profile["probes"][1]["stdout"] = profile["probes"][1]["stdout"].replace("--safe-mode", "")
    monkeypatch.setattr(api, "inspect_host", lambda *a, **k: profile)
    with pytest.raises(ValueError, match="required Claude flag not observed: --safe-mode"):
        api.solve(inputs[0], transport="claude", out=tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()
