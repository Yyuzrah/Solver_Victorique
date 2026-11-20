"""Offline Claude executable and bridge integration; every output uses tempfile."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from victorique.harness.claude_transport import ClaudeCliTransport, ClaudeFixtureTransport, AUTHORIZATION
from victorique.harness.safety import HarnessError
from victorique.harness_bridge import AdaptiveHarnessBridge, DispatchRequest
from victorique.adaptive_session import AdaptiveSession, CONTRACTS_VERSION, CONTROLLER_VERSION, SESSION_VERSION, TEMPLATES_VERSION
from tests.test_victorique_adaptive_exchange import manifest
from tests.test_victorique_harness_bridge import response
from tests.test_claude_contract import public_profile


def wait(adapter, request):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        capture = adapter.runner.observe_existing(adapter._binding(request))
        if capture and capture["worker_finished"]:
            return adapter.observe_existing(request)
        time.sleep(.02)
    raise AssertionError("offline fixture exceeded its deadline")


class ClaudeTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="victorique-claude-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        m = manifest()
        m["implementation_versions"].update(contracts=CONTRACTS_VERSION, controller=CONTROLLER_VERSION,
                                            session=SESSION_VERSION, templates=TEMPLATES_VERSION)
        self.core = AdaptiveSession.create(self.root / "core", m)
        state = self.core.status()
        self.rid = "request_" + uuid4().hex
        self.core.prepare({"session_id": m["session_id"], "operation_id": "op_" + uuid4().hex,
                           "expected_session_sequence": state["session_sequence"], "expected_state_revision": state["state_revision"],
                           "request_id": self.rid, "logical_action_id": "logical_" + uuid4().hex,
                           "action_id": "action_" + uuid4().hex,
                           "selected_proposal": self.core.propose()["legal_action_listing"]["actions"][0]})
        self.packet = self.core.snapshot()["exchange"]["requests"][self.rid]["packet"]

    def adapter(self, **kwargs):
        return ClaudeFixtureTransport.create(self.root / ("adapter_" + uuid4().hex), response_bytes=response(self.packet), **kwargs)

    def request(self, mode="synthetic"):
        return DispatchRequest("job_" + uuid4().hex, "claim_" + uuid4().hex, self.packet,
                               json.dumps(self.packet).encode(), mode)

    def test_bridge_uses_original_structured_bytes_and_only_observes_afterward(self):
        adapter = self.adapter()
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        self.assertTrue(bridge.dispatch(self.rid, adapter)["dispatched"])
        request = bridge._request(bridge._job(self.rid))
        observed = wait(adapter, request)
        self.assertEqual(observed.metadata["status"], "completed")
        self.assertEqual(observed.raw_response, response(self.packet).strip())
        self.assertEqual(bridge.recover(self.rid, adapter)["core_classification"], "returned_valid")
        self.assertFalse(bridge.dispatch(self.rid, adapter)["dispatched"])
        with patch.object(adapter.runner, "start_once", side_effect=AssertionError("redispatch")):
            self.assertEqual(adapter.start_once(request), observed)
        self.assertEqual(ClaudeFixtureTransport(adapter.root).observe_existing(request), observed)
        layout = adapter.runner.layout(adapter._binding(request))
        self.assertEqual(Path(layout["workspace"]).joinpath("stdin-observed.bin").read_bytes(), request.packet_bytes)
        self.assertTrue(all(json.loads(Path(layout["workspace"]).joinpath("isolation.json").read_text()).values()))

    def test_concurrent_instances_reserve_one_start(self):
        adapter, request = self.adapter(delay_seconds=.1), self.request()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: ClaudeFixtureTransport(adapter.root).start_once(request), range(3)))
        self.assertEqual(wait(adapter, request).metadata["status"], "completed")
        with adapter.runner._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_fault_after_adapter_fence_never_restarts(self):
        adapter, request = self.adapter(), self.request()
        with patch.object(adapter.runner, "start_once", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                adapter.start_once(request)
        with patch.object(adapter.runner, "start_once", side_effect=AssertionError("redispatch")):
            self.assertEqual(adapter.start_once(request).metadata["status"], "unknown")
        self.assertEqual(ClaudeFixtureTransport(adapter.root).observe_existing(request).metadata["status"], "unknown")

    def test_bad_captures_are_unknown_and_official_failure_is_failed(self):
        for scenario in ("failure", "malformed", "tool", "oversize", "timeout", "mismatch"):
            with self.subTest(scenario=scenario):
                adapter = self.adapter(scenario=scenario, timeout_seconds=.2 if scenario == "timeout" else 2,
                                       max_capture_bytes=4096 if scenario == "oversize" else 131072)
                request = self.request()
                adapter.start_once(request)
                self.assertEqual(wait(adapter, request).metadata["status"], "failed" if scenario in ("failure", "mismatch") else "unknown")
                with patch.object(adapter.runner, "start_once", side_effect=AssertionError("redispatch")):
                    adapter.start_once(request)

    def live_config(self):
        profile = public_profile()
        b = self.packet["binding"]
        auth = {"schema": AUTHORIZATION, "authorization_id": "test", "source_ref": "offline test", "allow_execution": True,
                "session_id": b["session_id"], "protocol": b["protocol"], "binary": profile["binary"], "host_version": profile["version"],
                "requested_model": b["model_request"]["model_id"], "max_process_starts": 1,
                "expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "permission_review_ref": "offline test", "authentication_precondition": "operator_completed_host_managed_login"}
        return profile, auth

    def test_authorization_budget_expiry_binding_and_invocation_without_execution(self):
        profile, auth = self.live_config()
        adapter = ClaudeCliTransport.create(self.root / "live-shaped", profile=profile, authorization=auth,
                                           output_schemas={"propose_plan": {"type": "object"}})
        request = self.request("host_recorded")
        invocation = adapter._invocation(request, adapter._binding(request))
        self.assertEqual(invocation["environment_policy"], "claude_subscription_isolated")
        self.assertIn("--safe-mode", invocation["argv"])
        # A mocked runner makes reaching any real executable impossible.
        with patch.object(adapter.runner, "start_once") as start:
            adapter.start_once(request)
            adapter.start_once(request)
            self.assertEqual(start.call_count, 1)
            with self.assertRaisesRegex(HarnessError, "ceiling"):
                adapter.start_once(self.request("host_recorded"))
        adapter.config["authorization"]["expires_at_utc"] = "2000-01-01T00:00:00+00:00"
        with patch.object(adapter.runner, "start_once", side_effect=AssertionError("redispatch")):
            adapter.start_once(request)
            self.assertIsNotNone(adapter.observe_existing(request))
            with self.assertRaisesRegex(HarnessError, "expired"):
                adapter.start_once(self.request("host_recorded"))

    def test_mismatched_packet_scope_fails_before_dispatch(self):
        profile, auth = self.live_config()
        auth["session_id"] = "different"
        adapter = ClaudeCliTransport.create(self.root / "wrong-scope", profile=profile, authorization=auth,
                                           output_schemas={"propose_plan": {"type": "object"}})
        with patch.object(adapter.runner, "start_once", side_effect=AssertionError("dispatch")):
            with self.assertRaisesRegex(HarnessError, "outside"):
                adapter.start_once(self.request("host_recorded"))
