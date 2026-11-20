"""Host probe and instruction-plan contracts; no real model or host execution."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from victorique.harness.hosts import inspect_host, parse_host_probe




def probe(binary, name, text, **extra):
    args = {"version": ["--version"], "help": ["--help"], "exec_help": ["exec", "--help"]}[name]
    result = {"name": name, "argv": [str(binary), *args], "exit_code": 0,
              "stdout": text, "stderr": "", "timed_out": False, "error": None,
              "output_truncated": False}
    result.update(extra)
    return result


class HostProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hosts-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "host.exe"
        self.binary.write_text("INERT DO NOT EXECUTE", encoding="utf-8")

    def codex_probes(self):
        return [probe(self.binary, "version", "codex-cli 0.167.0-alpha.4.1"),
                probe(self.binary, "help", "Codex CLI\nUsage: codex [OPTIONS]"),
                probe(self.binary, "exec_help", "--json --output-schema --sandbox --output-last-message")]

    def test_codex_public_flags_are_not_live_acceptance(self):
        result = parse_host_probe("codex", str(self.binary), self.codex_probes())
        self.assertEqual(result["status"], "public_interface_observed")
        self.assertEqual(result["version"], "0.167.0-alpha.4.1")
        self.assertTrue(result["identity"]["verified"])
        self.assertTrue(result["capabilities"]["--json"]["advertised"])
        self.assertFalse(result["capabilities"]["--json"]["execution_verified"])
        self.assertFalse(result["capabilities"]["--ignore-user-config"]["advertised"])
        for field in ("live_execution_verified", "isolation_verified", "skill_discovery_verified", "authentication_checked", "transport_enabled"):
            self.assertFalse(result[field])
        self.assertIsNone(result["actual_model"])

    def test_claude_branded_version_and_help(self):
        probes = [probe(self.binary, "version", "2.1.248 (Claude Code)"),
                  probe(self.binary, "help", "Claude Code\n--print --tools --restricted --json-schema")]
        result = parse_host_probe("claude-code", str(self.binary), probes)
        self.assertTrue(result["identity"]["verified"])
        self.assertEqual(result["version"], "2.1.248")
        self.assertTrue(result["capabilities"]["--restricted"]["advertised"])
        self.assertFalse(result["capabilities"]["--bare"]["advertised"])

    def test_cursor_branded_help_can_pair_with_plain_version(self):
        probes = [probe(self.binary, "version", "2026.11.12-abc123"),
                  probe(self.binary, "help", "Cursor Agent\n--print --output-format --workspace")]
        result = parse_host_probe("cursor", str(self.binary), probes)
        self.assertTrue(result["identity"]["verified"])
        self.assertEqual(result["version"], "2026.11.12-abc123")

    def test_grok_agent_is_not_cursor(self):
        probes = [probe(self.binary, "version", "1.2.3"),
                  probe(self.binary, "help", "Grok Agent\nUsage: agent --print --output-format")]
        result = parse_host_probe("cursor", str(self.binary), probes)
        self.assertEqual(result["status"], "identity_unverified")
        self.assertFalse(result["identity"]["verified"])
        self.assertEqual(result["capabilities"], {})

    def test_filename_and_generic_help_do_not_verify_cursor(self):
        probes = [probe(self.binary, "version", "1.2.3"), probe(self.binary, "help", "Usage: agent --print")]
        result = parse_host_probe("cursor", str(self.binary), probes)
        self.assertFalse(result["identity"]["verified"])

    def test_nonzero_or_truncated_identity_probe_fails(self):
        for alteration in ({"exit_code": 1}, {"timed_out": True}, {"output_truncated": True}):
            with self.subTest(alteration=alteration):
                probes = self.codex_probes()
                probes[0].update(alteration)
                self.assertEqual(parse_host_probe("codex", str(self.binary), probes)["status"], "probe_failed")

    def test_flag_prefix_does_not_advertise_shorter_flag(self):
        probes = self.codex_probes()
        probes[2]["stdout"] = "--json-output --output-schema-something"
        result = parse_host_probe("codex", str(self.binary), probes)
        self.assertFalse(result["capabilities"]["--json"]["advertised"])
        self.assertFalse(result["capabilities"]["--output-schema"]["advertised"])

    def test_parser_rejects_nonprobe_argv(self):
        probes = self.codex_probes()
        probes[2]["argv"] = [str(self.binary), "exec", "INERT but this would invoke inference"]
        with self.assertRaises(ValueError):
            parse_host_probe("codex", str(self.binary), probes)

    def test_parser_rejects_duplicate_failed_probe(self):
        p = probe(self.binary, "version", "", exit_code=1)
        with self.assertRaises(ValueError):
            parse_host_probe("codex", str(self.binary), [p, p])

    def test_inspect_invokes_only_allowed_argv_and_empty_stdin(self):
        outputs = [subprocess.CompletedProcess([], 0, p["stdout"], "") for p in self.codex_probes()]
        with mock.patch("victorique.harness.hosts.subprocess.run", side_effect=outputs) as run:
            result = inspect_host("codex", self.binary)
        self.assertTrue(result["identity"]["verified"])
        self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["--version"], ["--help"], ["exec", "--help"]])
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotEqual(Path(call.kwargs["cwd"]), Path.cwd())

    def test_timeout_is_recorded_not_retried(self):
        with mock.patch("victorique.harness.hosts.subprocess.run", side_effect=subprocess.TimeoutExpired([], 0.1)) as run:
            result = inspect_host("claude-code", self.binary, timeout=0.1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["status"], "probe_failed")
        self.assertTrue(all(p["timed_out"] for p in result["probes"]))

    def test_cursor_auto_discovery_never_uses_generic_agent(self):
        with mock.patch("victorique.harness.hosts.shutil.which", return_value=None) as which, mock.patch("victorique.harness.hosts.subprocess.run") as run:
            result = inspect_host("cursor")
        which.assert_called_once_with("cursor-agent")
        run.assert_not_called()
        self.assertEqual(result["status"], "not_found_on_path")

    def test_unresolved_products_make_no_probes(self):
        with mock.patch("victorique.harness.hosts.shutil.which") as which, mock.patch("victorique.harness.hosts.subprocess.run") as run:
            for host in ("hermes", "dsh"):
                self.assertEqual(inspect_host(host)["status"], "identity_unresolved")
        which.assert_not_called()
        run.assert_not_called()

    def test_invalid_binary_and_timeout_rejected_before_process(self):
        wrapper = self.root / "codex.cmd"
        wrapper.write_text("INERT", encoding="utf-8")
        with mock.patch("victorique.harness.hosts.subprocess.run") as run:
            for binary in ("relative.exe", self.root / "missing.exe", self.root, wrapper):
                with self.subTest(binary=binary), self.assertRaises(ValueError):
                    inspect_host("codex", binary)
            for timeout in (0, -1, True, 31):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    inspect_host("codex", self.binary, timeout=timeout)
        run.assert_not_called()

    def test_unsupported_host_is_explicit(self):
        with self.assertRaises(ValueError):
            inspect_host("made-up-host")


if __name__ == '__main__':
    unittest.main()
