"""Pure command/event contracts using INERT fixtures, never a real host."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from victorique.harness.adapters import build_command_plan, parse_event_stream
from victorique.harness.hosts import parse_host_probe


ROOT = Path(__file__).resolve().parents[1]
BINARY = str(ROOT / "INERT-NOT-A-HOST.exe")
STAGING = ROOT / "INERT-staging"
SCHEMA_FILE = STAGING / "schema.json"
SCHEMA = {"type": "object", "properties": {"inert": {"type": "boolean"}}, "required": ["inert"], "additionalProperties": False}


def profile(host, *, version=None, omit_flag=None):
    version, banner = {
        "codex": (version or "0.167.0-alpha.4.1", "Codex CLI"),
        "claude-code": (version or "2.1.248", "Claude Code"),
        "cursor": (version or "2026.11.12-inert", "Cursor Agent"),
    }[host]
    version_text = "codex-cli " + version if host == "codex" else (version + " (Claude Code)" if host == "claude-code" else version)
    flags = "--json --output-schema --output-last-message --sandbox --cd --ignore-user-config --print --output-format --verbose --json-schema --max-turns --restricted --bare --tools --disallowedTools --workspace --model"
    if omit_flag:
        flags = " ".join(flag for flag in flags.split() if flag != omit_flag)
    probes = [{"name": "version", "argv": [BINARY, "--version"], "stdout": version_text, "stderr": "", "exit_code": 0},
              {"name": "help", "argv": [BINARY, "--help"], "stdout": banner + "\n" + flags, "stderr": "", "exit_code": 0}]
    return parse_host_probe(host, BINARY, probes)


def codex_events():
    return [{"type": "thread.started", "thread_id": "INERT-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "INERT-item", "type": "agent_message", "text": '{"inert":true}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 9, "cached_input_tokens": 4, "output_tokens": 2}}]


def print_events(host):
    init = {"type": "system", "subtype": "init", "session_id": "INERT-session", "model": "INERT-reported-model"}
    result = {"type": "result", "subtype": "success", "session_id": "INERT-session", "is_error": False,
              "result": '{"inert":true}'}
    if host == "claude-code":
        init.update(tools=[], mcp_servers=[], agents=[])
        result.update(structured_output={"inert": True}, usage={"input_tokens": 9, "output_tokens": 2},
                      total_cost_usd=0.01, modelUsage={"INERT-reported-model": {"inputTokens": 9, "outputTokens": 2}})
    return [init, result]


def capture(events):
    return "".join(json.dumps(event) + "\n" for event in events)


class CommandPlanTests(unittest.TestCase):
    def plan(self, host, **kwargs):
        return build_command_plan(profile(host), STAGING, SCHEMA_FILE, output_schema=SCHEMA, **kwargs)

    def test_three_candidate_argv_are_nonrunnable_and_do_not_execute(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("must not execute")):
            for host in ("codex", "claude-code", "cursor"):
                with self.subTest(host=host):
                    plan = self.plan(host)
                    self.assertEqual(plan["status"], "candidate_not_ready")
                    self.assertIsInstance(plan["candidate_argv"], list)
                    self.assertFalse(plan["runnable"])
                    self.assertFalse(plan["live_execution_verified"])
                    self.assertFalse(plan["isolation_verified"])
                    self.assertFalse(plan["nested_roles_allowed"])
                    self.assertTrue(plan["stdin"]["required"])

    def test_codex_stdin_and_read_only_sandbox(self):
        plan = self.plan("codex", requested_model="INERT-requested")
        argv = plan["candidate_argv"]
        self.assertEqual(argv[-1], "-")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--output-schema") + 1], str(SCHEMA_FILE))
        self.assertEqual(plan["requested_model"], "INERT-requested")
        self.assertIsNone(plan["actual_model"])

    def test_claude_schema_is_json_not_filename_and_tools_are_disabled(self):
        plan = self.plan("claude-code", max_turns=2)
        argv = plan["candidate_argv"]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__*")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "2")
        self.assertIn("--verbose", argv)

    def test_cursor_permissions_remain_blocking(self):
        plan = self.plan("cursor")
        self.assertIn("cursor_effective_permission_policy_not_verified", plan["readiness_blockers"])
        self.assertIn("cursor_schema_enforcement_unavailable", plan["readiness_blockers"])

    def test_no_resume_force_yolo_or_permission_bypass(self):
        forbidden = {"resume", "--resume", "--continue", "--force", "--yolo", "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox"}
        for host in ("codex", "claude-code", "cursor"):
            plan = self.plan(host)
            self.assertFalse(set(plan["candidate_argv"]) & forbidden)
            self.assertFalse(plan["resume_is_query"])
            self.assertEqual(plan["read_only_recovery"], "unsupported")

    def test_missing_flag_rejects_command_including_unmapped_verbose(self):
        for host, flag in (("codex", "--json"), ("claude-code", "--verbose"), ("cursor", "--workspace")):
            with self.subTest(host=host):
                plan = build_command_plan(profile(host, omit_flag=flag), STAGING, SCHEMA_FILE, output_schema=SCHEMA)
                self.assertIsNone(plan["candidate_argv"])
                self.assertIn("required_flag_not_observed:" + flag, plan["profile_errors"])

    def test_profile_capability_boolean_is_not_enough_without_probe(self):
        p = profile("codex")
        p["probes"] = []
        plan = build_command_plan(p, STAGING, SCHEMA_FILE)
        self.assertIsNone(plan["candidate_argv"])

    def test_wrong_probe_argv_is_not_capability_evidence(self):
        p = profile("codex")
        p["probes"][1]["argv"] = [BINARY, "exec", "INERT"]
        plan = build_command_plan(p, STAGING, SCHEMA_FILE)
        self.assertIsNone(plan["candidate_argv"])

    def test_unverified_identity_and_missing_version_reject_command(self):
        for key in ("identity", "version"):
            p = profile("codex")
            p[key] = {"verified": False} if key == "identity" else None
            plan = build_command_plan(p, STAGING, SCHEMA_FILE)
            self.assertIsNone(plan["candidate_argv"])

    def test_unknown_runtime_version_never_implies_readiness(self):
        plan = build_command_plan(profile("codex", version="99.999.0"), STAGING, SCHEMA_FILE)
        self.assertFalse(plan["public_help_version_observed"])
        self.assertFalse(plan["runnable"])
        self.assertIn("host_version_not_locally_observed_by_this_candidate", plan["readiness_blockers"])

    def test_claude_cannot_pass_schema_filename_as_json(self):
        plan = build_command_plan(profile("claude-code"), STAGING, SCHEMA_FILE)
        self.assertIsNone(plan["candidate_argv"])
        self.assertIn("schema_contents_required_for_claude", plan["profile_errors"])

    def test_bad_model_and_relative_paths_raise_before_any_io(self):
        for bad_model in ("--resume", "", "bad\nmodel"):
            with self.subTest(model=bad_model), self.assertRaises(ValueError):
                self.plan("codex", requested_model=bad_model)
        for staging in ("relative", str(STAGING) + "\nINERT"):
            with self.assertRaises(ValueError):
                build_command_plan(profile("codex"), staging, SCHEMA_FILE)


class EventParserTests(unittest.TestCase):
    def parse(self, host="codex", events=None, **kwargs):
        if events is None:
            events = codex_events() if host == "codex" else print_events(host)
        return parse_event_stream(host, capture(events), exit_code=kwargs.pop("exit_code", 0), **kwargs)

    def test_three_successful_inert_contracts_are_not_math_or_host_acceptance(self):
        for host in ("codex", "claude-code", "cursor"):
            with self.subTest(host=host):
                result = self.parse(host)
                self.assertTrue(result["software_contract_valid"], result["issues"])
                self.assertEqual(result["host_outcome"], "reported_success")
                self.assertEqual(result["structured_output"], {"inert": True})
                self.assertIsNone(result["mathematical_result"])
                self.assertFalse(result["live_execution_verified"])
                self.assertFalse(result["schema_validated"])

    def test_requested_model_does_not_fill_absent_actual_model(self):
        result = self.parse(requested_model="INERT-requested")
        self.assertEqual(result["requested_model"], "INERT-requested")
        self.assertIsNone(result["actual_model"])

    def test_actual_reported_model_is_separate_from_request(self):
        result = self.parse("claude-code", requested_model="INERT-requested")
        self.assertEqual(result["actual_model"], "INERT-reported-model")
        self.assertNotEqual(result["actual_model"], result["requested_model"])

    def test_claude_estimated_cost_and_main_loop_usage_not_actual_bill(self):
        result = self.parse("claude-code")
        self.assertEqual(result["reported_cost_usd"], 0.01)
        self.assertEqual(result["cost_kind"], "host_client_estimate")
        self.assertIsNone(result["actual_cost_usd"])
        self.assertFalse(result["financially_settled"])
        self.assertEqual(result["usage_scope"], "main_agent_loop_only")
        self.assertIn("INERT-reported-model", result["model_usage_raw"])

    def test_missing_usage_cost_stay_null_not_zero(self):
        result = self.parse("cursor")
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["reported_cost_usd"])
        result = self.parse(events=codex_events()[:-1] + [{"type": "turn.completed"}])
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["actual_cost_usd"])

    def test_nonzero_and_unknown_exit_do_not_succeed(self):
        for code in (1, 143, None):
            with self.subTest(code=code):
                result = self.parse(exit_code=code)
                self.assertFalse(result["software_contract_valid"])
                self.assertEqual(result["host_outcome"], "unknown")

    def test_truncated_and_missing_newline_do_not_succeed(self):
        self.assertFalse(self.parse(truncated=True)["software_contract_valid"])
        result = parse_event_stream("codex", capture(codex_events()).rstrip(), exit_code=0)
        self.assertIn("jsonl_final_newline_missing", result["issues"])

    def test_malformed_line_retains_entire_capture(self):
        raw = capture(codex_events()) + "{INERT malformed\n"
        result = parse_event_stream("codex", raw, raw_stderr="INERT diagnostic", exit_code=0)
        self.assertFalse(result["software_contract_valid"])
        self.assertEqual(result["raw_stdout"], raw)
        self.assertEqual(result["raw_stderr"], "INERT diagnostic")
        self.assertEqual(result["malformed_line_numbers"], [5])

    def test_duplicate_terminal_rejected_without_selecting_one_cost(self):
        events = print_events("claude-code")
        events.append(copy.deepcopy(events[-1]))
        result = self.parse("claude-code", events)
        self.assertIn("expected_one_terminal_event", result["issues"])
        self.assertIsNone(result["reported_cost_usd"])

    def test_session_conflict_never_guesses_identity(self):
        events = print_events("cursor")
        events[-1]["session_id"] = "INERT-other"
        result = self.parse("cursor", events)
        self.assertIn("conflicting_session_id", result["issues"])
        self.assertIsNone(result["session_id"])

    def test_events_after_terminal_rejected(self):
        events = codex_events() + [{"type": "turn.started"}]
        self.assertIn("events_after_terminal", self.parse(events=events)["issues"])

    def test_message_before_turn_start_rejected(self):
        events = codex_events()
        events[1], events[2] = events[2], events[1]
        self.assertIn("item_before_turn_start", self.parse(events=events)["issues"])

    def test_cursor_uses_terminal_text_not_duplicate_stream_deltas(self):
        events = print_events("cursor")
        events.insert(1, {"type": "assistant", "session_id": "INERT-session", "message": {"content": [{"type": "text", "text": "INERT duplicate"}]}})
        result = self.parse("cursor", events)
        self.assertEqual(result["final_text"], '{"inert":true}')
        self.assertEqual(result["structured_output"], {"inert": True})

    def test_nested_and_tool_calls_violate_text_only_contract(self):
        fixtures = {
            "codex": {"type": "item.started", "item": {"type": "command_execution", "command": "INERT"}},
            "cursor": {"type": "tool_call", "subtype": "started", "session_id": "INERT-session"},
            "claude-code": {"type": "assistant", "session_id": "INERT-session", "parent_tool_use_id": "INERT-agent-call", "message": {"content": [{"type": "text", "text": "INERT"}]}},
        }
        for host, event in fixtures.items():
            events = codex_events() if host == "codex" else print_events(host)
            events.insert(-1, event)
            with self.subTest(host=host):
                self.assertIn("tool_free_policy_violated", self.parse(host, events)["issues"])

    def test_host_reported_failure_retains_usage(self):
        events = print_events("claude-code")
        events[-1].update(subtype="error_max_turns", is_error=True)
        result = self.parse("claude-code", events)
        self.assertEqual(result["host_outcome"], "reported_failure")
        self.assertEqual(result["reported_cost_usd"], 0.01)
        self.assertEqual(result["usage"]["input_tokens"], 9)

    def test_invalid_json_or_missing_structured_output_not_repaired(self):
        events = codex_events()
        events[2]["item"]["text"] = '```json\n{"inert":true}\n```'
        self.assertIn("final_text_not_strict_json", self.parse(events=events)["issues"])
        events = print_events("claude-code")
        del events[-1]["structured_output"]
        self.assertIn("missing_structured_output_object", self.parse("claude-code", events)["issues"])

    def test_duplicate_json_keys_and_nonfinite_numbers_rejected(self):
        for line in ('{"type":"turn.started","type":"turn.completed"}\n', '{"type":"turn.started","x":NaN}\n', '{"type":"turn.started","x":1e999}\n'):
            result = parse_event_stream("codex", line, exit_code=0)
            self.assertIn("malformed_event_lines", result["issues"])

    def test_invalid_usage_and_cost_rejected_but_raw_event_kept(self):
        events = print_events("claude-code")
        events[-1]["usage"]["input_tokens"] = True
        events[-1]["total_cost_usd"] = -1
        result = self.parse("claude-code", events)
        self.assertIn("invalid_usage:input_tokens", result["issues"])
        self.assertIn("invalid_reported_cost", result["issues"])
        self.assertEqual(result["raw_events"][-1]["total_cost_usd"], -1)

    def test_conflicting_reported_models_are_not_collapsed(self):
        events = print_events("claude-code")
        events.insert(1, {"type": "assistant", "session_id": "INERT-session", "message": {"model": "INERT-other-model", "content": []}})
        result = self.parse("claude-code", events)
        self.assertIsNone(result["actual_model"])
        self.assertIn("conflicting_model", result["issues"])


if __name__ == "__main__":
    unittest.main()
