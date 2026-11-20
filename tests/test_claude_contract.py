"""Claude contract tests use captured synthetic public help, never a live CLI."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from victorique.harness.claude_contract import (
    REQUIRED_FLAGS, claude_environment, complete_claude_profile, conservative_claude_argv, parse_claude_capture,
)
from victorique.harness.codex_process import DurableCodexProcess, _run_worker
from victorique.harness.hosts import parse_host_probe
from victorique.harness.safety import HarnessError
from tests.test_victorique_harness_codex_contract import packet, response, process


def public_profile():
    binary = str(Path(sys.executable).absolute())
    probes = [{"name": name, "argv": [binary, "--" + name], "stdout": text,
               "stderr": "", "exit_code": 0, "timed_out": False, "output_truncated": False, "error": None}
              for name, text in (("version", "2.1.999 (Claude Code)"),
                                 ("help", "Claude Code\n" + " ".join(REQUIRED_FLAGS) + " --model"))]
    return parse_host_probe("claude-code", binary, probes)


def stream(p=None, change=None):
    p = p or packet()
    init = {"type": "system", "subtype": "init", "session_id": "fake-session", "model": "fake-model",
            "tools": [], "mcp_servers": []}
    terminal = {"type": "result", "subtype": "success", "is_error": False, "session_id": "fake-session",
                "structured_output": response(p), "usage": {"input_tokens": 3, "output_tokens": 5}}
    rows = [init, terminal]
    if change:
        change(rows)
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8")


class ClaudeContractTests(unittest.TestCase):
    def test_hidden_max_turns_requires_specific_numeric_parser_evidence(self):
        profile = public_profile()
        profile["probes"][1]["stdout"] = profile["probes"][1]["stdout"].replace("--max-turns", "")
        def probe(argv, **kwargs):
            assert argv[-1] == "--help"
            assert kwargs["stdin"] is not None and kwargs["shell"] is False
            invalid = "--max-turns" in argv and "victorique-invalid-number" in argv
            return SimpleNamespace(returncode=1 if invalid else 0,
                                   stdout=b"" if invalid else b"Claude Code\nUsage: claude [options]",
                                   stderr=b"error: option '--max-turns <turns>' argument 'victorique-invalid-number' is invalid. must be a number\n" if invalid else b"")
        with patch("subprocess.run", side_effect=probe) as run:
            enriched = complete_claude_profile(profile)
        assert run.call_count == 3
        assert "--max-turns" in conservative_claude_argv(enriched, {})
        for field, value in (("exit_code", 0), ("stderr", ""), ("host_version", "other"),
                             ("argv", [profile["binary"], "--version"]), ("output_truncated", True)):
            bad = deepcopy(enriched)
            bad["argument_probes"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "--max-turns"):
                conservative_claude_argv(bad, {})
        # Help that ignores both known and unknown flags provides no evidence.
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"Claude Code", stderr=b"")):
            ignored = complete_claude_profile(profile)
        with self.assertRaisesRegex(ValueError, "--max-turns"):
            conservative_claude_argv(ignored, {})

    def test_capability_checked_isolation_retains_subscription_login(self):
        argv = conservative_claude_argv(public_profile(), {"type": "object"})
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__*")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        self.assertIn("--safe-mode", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--model", argv)
        self.assertFalse(set(argv) & {"--resume", "--continue", "--add-dir", "--dangerously-skip-permissions"})
        model_argv = conservative_claude_argv(public_profile(), {}, "fake-model")
        self.assertEqual(model_argv[-2:], ["--model", "fake-model"])

    def test_missing_capabilities_or_invalid_probes_fail_closed(self):
        for flag in REQUIRED_FLAGS:
            profile = public_profile()
            profile["probes"][1]["stdout"] = profile["probes"][1]["stdout"].replace(flag, "")
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "required Claude flag"):
                conservative_claude_argv(profile, {})
        profile = public_profile()
        profile["probes"][0]["output_truncated"] = True
        with self.assertRaises(ValueError):
            conservative_claude_argv(profile, {})

    def test_environment_removes_overrides_and_disables_ancestor_memory(self):
        env = claude_environment({"HOME": "/fake/home", "PATH": "/fake/bin", "ANTHROPIC_API_KEY": "inert",
                                  "ANTHROPIC_BASE_URL": "inert", "CLAUDE_CODE_USE_BEDROCK": "1",
                                  "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "0", "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                                  "CLAUDE_CONFIG_DIR": "/fake/login", "NODE_OPTIONS": "inert"})
        self.assertEqual(env["HOME"], "/fake/home")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/fake/login")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", env)

    def test_raw_json_and_usage_preserved(self):
        result = parse_claude_capture(stream(), b"", process(), packet())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response_bytes"], json.dumps(response(), ensure_ascii=False).encode())
        self.assertEqual(result["actual_model"], "fake-model")
        self.assertEqual(result["usage"]["input_tokens"], 3)

    def test_structured_output_tool_pair_is_the_only_allowed_tool(self):
        def paired(rows):
            rows[0]["tools"] = ["StructuredOutput"]
            rows[1:1] = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "StructuredOutput", "id": "s1"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "s1"}]}}]
        self.assertEqual(parse_claude_capture(stream(change=paired), b"", process(), packet())["status"], "completed")
        def orphan(rows):
            paired(rows)
            rows[2]["message"]["content"][0]["tool_use_id"] = "different"
        self.assertEqual(parse_claude_capture(stream(change=orphan), b"", process(), packet())["status"], "unknown")

    def test_uncertain_capture_never_completed(self):
        changes = [lambda r: r[0].update(tools=["Bash"]), lambda r: r[0].update(mcp_servers=[{"name": "inert"}]),
                   lambda r: r[-1].update(session_id="other"), lambda r: r.append(deepcopy(r[-1])),
                   lambda r: r[-1].pop("structured_output"),
                   lambda r: r.insert(1, {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}})]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(parse_claude_capture(stream(change=change), b"", process(), packet())["status"], "unknown")
        for evidence in (process(timed_out=True), process(capture_truncated=True), process(local_terminal=False), process(exit_code=1)):
            self.assertEqual(parse_claude_capture(stream(), b"", evidence, packet())["status"], "unknown")
        for malformed in (b"invalid", b"\xff", stream().replace(b'"is_error": false', b'"is_error": false, "is_error": false')):
            self.assertEqual(parse_claude_capture(malformed, b"", process(), packet())["status"], "unknown")

    def test_completed_invalid_response_is_failed_but_unfinished_remains_unknown(self):
        raw = stream(change=lambda r: r[-1]["structured_output"]["binding"].update(packet_id="other"))
        result = parse_claude_capture(raw, b"", process(), packet())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["terminal_observed"])
        self.assertIn("invalid_response", result["diagnostics"])
        self.assertIsNotNone(result["response_bytes"])
        for evidence in (process(timed_out=True), process(capture_truncated=True), process(local_terminal=False)):
            self.assertEqual(parse_claude_capture(raw, b"", evidence, packet())["status"], "unknown")

    def test_explicit_failure_and_unknown_usage(self):
        raw = stream(change=lambda r: r[-1].update(is_error=True, subtype="error_max_turns", usage={"input_tokens": True}))
        result = parse_claude_capture(raw, b"", process(exit_code=1), packet())
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["usage"]["input_tokens"])
        self.assertIsNone(result["response_bytes"])

    def test_authentication_error_with_success_subtype_is_terminal_failure(self):
        # Public event shape observed from the Claude Code CLI; no login or live CLI is used here.
        def auth_error(rows):
            rows.insert(1, {"type": "assistant", "error": "authentication_failed",
                            "message": {"content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
            rows[-1].pop("structured_output")
            rows[-1].update(subtype="success", is_error=True, usage={"input_tokens": 0, "output_tokens": 0,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "output_tokens_details": {"thinking_tokens": 0}})
        raw = stream(change=auth_error)
        parsed = parse_claude_capture(raw, b"", process(exit_code=1), packet())
        self.assertEqual(parsed["status"], "failed")
        self.assertTrue(parsed["terminal_observed"])
        self.assertEqual(parsed["usage"]["reasoning_output_tokens"], 0)
        for evidence in (process(exit_code=1, capture_truncated=True), process(exit_code=1, timed_out=True),
                         process(exit_code=1, local_terminal=False)):
            self.assertEqual(parse_claude_capture(raw, b"", evidence, packet())["status"], "unknown")

    def test_process_policy_is_fixed_and_default_environment_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = DurableCodexProcess.create(Path(folder) / "runner")
            binding = {"job_id": "j", "core_claim_id": "c", "packet_id": "p", "mode": "synthetic"}
            layout = runner.layout(binding)
            invocation = {"argv": [sys.executable, "--version"], "cwd": layout["workspace"], "final_file": layout["final_file"]}
            normalized, _ = runner._invocation(binding, invocation)
            self.assertEqual(normalized["environment"], "inherited_by_process_without_inspection")
            with self.assertRaisesRegex(HarnessError, "unsupported environment policy"):
                runner._invocation(binding, invocation | {"environment_policy": "anything"})
            with self.assertRaises(HarnessError):
                runner._invocation(binding, invocation | {"env": {}})

    def test_expiry_during_helper_delay_never_launches_child(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = DurableCodexProcess.create(Path(folder) / "runner")
            binding = {"job_id": "j", "core_claim_id": "c", "packet_id": "p", "mode": "synthetic"}
            layout = runner.layout(binding)
            expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
            invocation = {"argv": [sys.executable, "--version"], "cwd": layout["workspace"], "final_file": layout["final_file"],
                          "deadline_utc": expiry.isoformat()}
            # Fence and persist normally, then simulate the coordinator dying
            # immediately before its detached helper launch.
            def crash(stage, _):
                if stage == "before_helper_launch":
                    raise RuntimeError("simulated coordinator exit")
            with patch.object(runner, "_checkpoint", side_effect=crash):
                runner.start_once(binding, invocation, b"{}")
            with runner._db() as db:
                token = db.execute("SELECT launch_token FROM jobs").fetchone()[0]
            with patch("victorique.harness.codex_process.datetime") as clock, patch("subprocess.Popen", side_effect=AssertionError("late launch")):
                clock.fromisoformat = datetime.fromisoformat
                clock.now.return_value = expiry + timedelta(seconds=1)
                self.assertEqual(_run_worker(runner.root, "j", token), 0)
            capture = runner.observe_existing(binding)
            self.assertTrue(capture["timed_out"])
            self.assertIsNone(capture["child_pid"])
            self.assertFalse(capture["local_terminal_observed"])
