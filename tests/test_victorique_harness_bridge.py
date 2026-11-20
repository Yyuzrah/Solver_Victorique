"""INERT bridge acceptance; no host/model/API/research execution."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from uuid import uuid4

from victorique import adaptive_exchange as exchange
from victorique.adaptive_session import AdaptiveSession, CONTRACTS_VERSION, CONTROLLER_VERSION, SESSION_VERSION, TEMPLATES_VERSION
from victorique.harness_bridge import (AdaptiveHarnessBridge, HarnessInertAdapter, DispatchRequest,
    TransportObservation, BridgeError, OBSERVATION, validate_adapter)
from tests.test_victorique_adaptive_exchange import manifest
from tests import test_victorique_adaptive_contracts as f




def response(packet, *, branching=False):
    b = packet['binding']; kind = b['action_kind']
    if kind in ('propose_plan', 'replan'):
        output = f.plan(branch=branching, revision=1 if b['plan_ref'] is None else b['plan_ref']['revision'] + 1)
    elif kind in ('solve_obligation', 'integrate', 'repair'):
        output = f.candidate(oid=b['obligation_id'], route=b['route_id'], plan_revision=b['plan_ref']['revision'],
            revision=1 if b['target_ref'] is None else b['target_ref']['revision'] + 1,
            dependencies=b['dependency_refs'], issues=[] if b['issue_id'] is None else [b['issue_id']])
    elif kind == 'critique_candidate':
        output = {'schema': 'victorique.adaptive_critique.v1', 'target_ref': b['target_ref'], 'findings': [], 'no_gap_identified': True}
    elif kind in ('alignment_review', 'whole_proof_review'):
        output = {'schema': 'victorique.adaptive_gate.v1', 'gate_id': kind, 'problem_ref': b['problem_ref'],
            'target_ref': b['target_ref'], 'coverage': 'whole_problem_and_proof', 'outcome': 'pass', 'findings': [], 'uncertainties': []}
    else: raise AssertionError('unimplemented INERT fixture kind ' + kind)
    return json.dumps({'schema': exchange.RESPONSE_SCHEMA, 'binding': b, 'output': output}, ensure_ascii=False).encode('utf-8')


class ScriptedTransport:
    def __init__(self, fixture=response):
        self.fixture = fixture; self.starts = 0; self.lock = threading.Lock(); self.observations = {}; self.barrier = None
    def describe(self):
        return {'adapter_id': 'scripted_inert', 'version': 'v1', 'mode': 'synthetic', 'can_start': True,
            'can_observe_existing': True, 'model_execution': False, 'fresh_context_required': True, 'raw_response_preserved': True}
    def observation(self, request, status='completed', raw='fixture', host=True, **changes):
        metadata = {'schema': OBSERVATION, 'observation_id': 'obs_' + uuid4().hex, 'job_id': request.job_id,
            'core_claim_id': request.core_claim_id, 'packet_id': request.packet['binding']['packet_id'],
            'adapter_id': 'scripted_inert', 'adapter_version': 'v1', 'mode': 'synthetic', 'status': status,
            'terminal_observed': status in ('completed', 'failed'), 'host_reference': '/inert/' + request.job_id if host else None,
            'observed_at_utc': datetime.now(timezone.utc).isoformat(), 'source_event_id': None, 'source_line': None,
            'model_observation': None, 'charge_observation': None}
        metadata.update(changes)
        if raw == 'fixture': raw = self.fixture(request.packet) if status == 'completed' else None
        return TransportObservation(metadata, raw, b'INERT transport source bytes; no model output.')
    def start_once(self, request):
        with self.lock: self.starts += 1
        if self.barrier: self.barrier.wait(15)
        observation = self.observation(request)
        self.observations[request.job_id] = observation
        return observation
    def observe_existing(self, request): return self.observations.get(request.job_id)


class Crash(BaseException): pass


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='inert-'); self.addCleanup(self.tmp.cleanup)
        self.root = (Path(self.tmp.name) / 'victorique-development'); self.m = manifest()
        self.m['implementation_versions'].update(contracts=CONTRACTS_VERSION, controller=CONTROLLER_VERSION,
            session=SESSION_VERSION, templates=TEMPLATES_VERSION)
        self.core = AdaptiveSession.create(self.root / 'core', self.m)
        self.adapter = ScriptedTransport()
        self.bridge = AdaptiveHarnessBridge.create(self.root / 'bridge', self.root / 'core', self.adapter.describe())
    def control(self):
        s = self.core.status()
        return {'session_id': self.m['session_id'], 'operation_id': 'op_' + uuid4().hex,
            'expected_session_sequence': s['session_sequence'], 'expected_state_revision': s['state_revision']}
    def prepare(self, kind=None, oid=None):
        actions = self.core.propose()['legal_action_listing']['actions']
        action = next(a for a in actions if (kind is None or kind == a['kind']) and (oid is None or oid == a['obligation_id']))
        rid = 'req_' + uuid4().hex
        self.core.prepare(self.control() | {'request_id': rid, 'logical_action_id': 'logical_' + uuid4().hex,
            'action_id': 'action_' + uuid4().hex, 'selected_proposal': action})
        return rid
    def request(self, rid): return self.bridge._request(self.bridge._job(rid))
    def rows(self):
        with closing(sqlite3.connect(self.bridge.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM observations')]
    def crash(self, rid, stage):
        def hook(actual, _):
            if actual == stage: raise Crash(stage)
        with mock.patch.object(self.bridge, '_checkpoint', side_effect=hook), self.assertRaises(Crash):
            self.bridge.dispatch(rid, self.adapter)
    def run_one(self, kind=None):
        rid = self.prepare(kind); self.bridge.dispatch(rid, self.adapter); return rid

    def test_environment_adapter_five_step_chain_and_explicit_finalization(self):
        adapter = HarnessInertAdapter(self.root / 'environment', self.m, response)
        bridge = AdaptiveHarnessBridge.create(self.root / 'environment-bridge', self.root / 'core', adapter.describe())
        for kind in ('propose_plan', 'solve_obligation', 'critique_candidate', 'alignment_review', 'whole_proof_review'):
            rid = self.prepare(kind); result = bridge.dispatch(rid, adapter)
            self.assertEqual(result['core_classification'], 'returned_valid')
            self.assertFalse(result['mathematical_truth_verified'])
        self.assertFalse(self.core.status()['sealed'])
        decision = self.core.propose()['decision']
        self.core.finalize(self.control() | {'endpoint_id': 'inert_endpoint', 'decision': decision['decision'], 'reason': decision['reason']})
        self.assertTrue(self.core.status()['sealed'])
        self.assertEqual(self.core.status()['host_bound_calls'], 5)
        self.assertEqual(adapter.journal.status()['calls_reserved'], 5)
        self.assertEqual(adapter.journal.status()['actual_model_calls'], 0)
        for row in adapter.journal.export()['jobs']:
            self.assertEqual(row['receipt']['response_origin'], 'scripted_inert_fixture_not_model_output')
        self.assertEqual(bridge.status()['independent_budget_grants'], 0)

    def test_duplicate_dispatch_and_reopen_do_not_start_again(self):
        rid = self.run_one()
        for bridge in (self.bridge, AdaptiveHarnessBridge(self.root / 'bridge')):
            self.assertFalse(bridge.dispatch(rid, self.adapter)['dispatched'])
        self.assertEqual(self.adapter.starts, 1)
        self.assertEqual(self.rows()[0]['response'], self.adapter.observations[self.request(rid).job_id].raw_response)

    def test_concurrent_same_request_starts_once(self):
        rid = self.prepare(); self.bridge.prepare(rid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: AdaptiveHarnessBridge(self.root / 'bridge').dispatch(rid, self.adapter), range(2)))
        self.assertEqual(sum(r.get('dispatched', False) for r in results), 1)
        self.assertEqual(self.adapter.starts, 1)

    def test_two_legal_leaf_calls_can_run_concurrently(self):
        self.adapter.fixture = lambda p: response(p, branching=True)
        self.run_one('propose_plan')
        rids = [self.prepare('solve_obligation', oid) for oid in ('left', 'right')]
        self.adapter.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda rid: AdaptiveHarnessBridge(self.root / 'bridge').dispatch(rid, self.adapter), rids))
        self.assertTrue(all(r['core_classification'] == 'returned_valid' for r in results))
        self.assertEqual(self.adapter.starts, 3)

    def test_crash_before_claim_never_creates_a_later_dispatch(self):
        rid = self.prepare(); self.crash(rid, 'before_core_claim')
        result = self.bridge.recover(rid, self.adapter)
        self.assertEqual(result['phase'], 'unclaimed_interrupted')
        self.assertIsNone(self.core.snapshot()['exchange']['requests'][rid]['claim_id'])
        self.assertFalse(self.bridge.dispatch(rid, self.adapter)['dispatched']); self.assertEqual(self.adapter.starts, 0)

    def test_crash_after_core_claim_recovers_ownership_but_not_execution(self):
        rid = self.prepare(); self.crash(rid, 'core_claim_committed')
        result = AdaptiveHarnessBridge(self.root / 'bridge').recover(rid, self.adapter)
        self.assertEqual(result['phase'], 'unknown'); self.assertEqual(self.adapter.starts, 0)
        self.assertEqual(self.core.snapshot()['exchange']['requests'][rid]['phase'], 'unknown')

    def test_crash_after_transport_fence_never_starts_on_recovery(self):
        rid = self.prepare(); self.crash(rid, 'transport_fenced')
        self.assertEqual(self.bridge.recover(rid, self.adapter)['phase'], 'unknown')
        self.assertFalse(self.bridge.dispatch(rid, self.adapter)['dispatched']); self.assertEqual(self.adapter.starts, 0)

    def test_lost_transport_return_uses_only_observe_existing(self):
        rid = self.prepare(); self.crash(rid, 'transport_returned_before_archive')
        result = self.bridge.recover(rid, self.adapter)
        self.assertEqual(result['core_classification'], 'returned_valid'); self.assertEqual(self.adapter.starts, 1)

    def test_crash_after_raw_archive_replays_original_bytes(self):
        rid = self.prepare(); self.crash(rid, 'raw_archived')
        self.assertIsNone(self.rows()[0]['result'])
        result = self.bridge.recover(rid)
        self.assertEqual(result['replayed_observations'][0]['core_classification'], 'returned_valid')
        self.assertEqual(self.adapter.starts, 1)

    def test_crash_after_core_ingest_does_not_duplicate_projection(self):
        rid = self.prepare(); self.crash(rid, 'core_ingest_committed')
        count = len(self.core.snapshot()['exchange']['observations'])
        self.bridge.recover(rid)
        self.assertEqual(len(self.core.snapshot()['exchange']['observations']), count)
        self.assertEqual(self.adapter.starts, 1)

    def test_unknown_no_redispatch_then_actual_late_terminal(self):
        rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
        unknown = self.adapter.observation(request, status='unknown', host=False)
        self.bridge.receive(rid, unknown)
        for _ in range(2): self.bridge.recover(rid)
        self.assertFalse(self.bridge.dispatch(rid, self.adapter)['dispatched'])
        result = self.bridge.receive(rid, self.adapter.observation(request))
        self.assertEqual(result['core_classification'], 'returned_valid')
        self.assertEqual(self.bridge.job_status(rid)['phase'], 'completed'); self.assertEqual(self.adapter.starts, 0)

    def test_wrong_claim_raw_quarantined_then_correct_new_receipt(self):
        rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
        wrong = self.adapter.observation(request, core_claim_id='wrong_claim')
        result = self.bridge.receive(rid, wrong)
        self.assertEqual(result['phase'], 'unknown'); self.assertEqual(self.rows()[0]['response'], wrong.raw_response)
        self.assertIn('quarantined_metadata', self.rows()[0]['result'])
        self.assertEqual(self.bridge.receive(rid, self.adapter.observation(request))['core_classification'], 'returned_valid')

    def test_duplicate_receipt_exact_only_and_wrong_host_retains_raw(self):
        rid = self.run_one(); request = self.request(rid); original = self.adapter.observations[request.job_id]
        before = len(self.core.snapshot()['exchange']['observations'])
        self.bridge.receive(rid, original)
        self.assertEqual(len(self.core.snapshot()['exchange']['observations']), before)
        with self.assertRaises(BridgeError):
            self.bridge.receive(rid, TransportObservation(original.metadata, b'changed', original.raw_transport))
        wrong = self.adapter.observation(request, host_reference='/inert/another_actual_role')
        self.assertEqual(self.bridge.receive(rid, wrong)['phase'], 'completed')
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.core.snapshot()['exchange']['requests'][rid]['host_reference'], original.metadata['host_reference'])

    def test_close_then_late_response_preserves_endpoint(self):
        rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
        self.bridge.close('INERT local stop; remote is not cancelled')
        endpoint = deepcopy(self.core.snapshot()['endpoint'])
        self.bridge.receive(rid, self.adapter.observation(request))
        self.assertEqual(self.core.snapshot()['endpoint'], endpoint)
        self.assertEqual(self.bridge.job_status(rid)['phase'], 'completed')
        self.assertFalse(self.core.status()['remote_shutdown_verified'])

    def test_process_success_invalid_native_response_is_not_domain_success(self):
        adapter = HarnessInertAdapter(self.root / 'invalid-environment', self.m, lambda _: b'INERT invalid native JSON')
        bridge = AdaptiveHarnessBridge.create(self.root / 'invalid-bridge', self.root / 'core', adapter.describe())
        result = bridge.dispatch(self.prepare(), adapter)
        self.assertEqual(result['core_classification'], 'returned_invalid')
        self.assertEqual(self.core.status()['candidate_count'], 0); self.assertFalse(self.core.status()['sealed'])

    def test_completed_missing_or_oversized_raw_uses_capture_failure(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                if oversized:
                    core = AdaptiveSession.create(self.root / 'core2', self.m); self.core = core
                    self.bridge = AdaptiveHarnessBridge.create(self.root / 'bridge2', core.root, self.adapter.describe())
                rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
                raw = b'x' * (self.m['storage_limits']['max_raw_response_bytes'] + 1) if oversized else None
                result = self.bridge.receive(rid, self.adapter.observation(request, raw=raw))
                self.assertEqual(result['core_classification'], 'capture_failed'); self.assertTrue(self.core.status()['sealed'])
                capture = json.loads(self.rows()[0]['capture'])
                self.assertEqual(capture['response'], 'size_limit' if oversized else 'absent')

    def test_host_recorded_contract_is_available_but_requires_exact_authorization(self):
        m = deepcopy(self.m); m['mode'] = 'host_recorded'; d = self.adapter.describe()
        d.update(mode='host_recorded', model_execution=True)
        grant = {'schema': 'victorique.bridge_execution_grant.v1', 'grant_id': 'inert_grant', 'session_id': m['session_id'],
            'protocol': m['protocol'], 'adapter_id': d['adapter_id'], 'adapter_version': d['version'], 'allow_execution': True,
            'source_ref': 'INERT contract-only authorization fixture; no execution',
            'expires_at_utc': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        self.assertEqual(validate_adapter(d, m, grant), d)
        for changed in (None, dict(grant, session_id='another'), dict(grant, allow_execution=1),
                        dict(grant, expires_at_utc='2020-01-01T00:00:00Z')):
            with self.subTest(changed=changed), self.assertRaises(ValueError): validate_adapter(d, m, changed)
        with self.assertRaises(BridgeError): HarnessInertAdapter(self.root / 'not-live', m)
        self.assertFalse((self.root / 'not-live').exists())

    def test_adapter_drift_rejected_before_claim_and_no_independent_cap(self):
        rid = self.prepare()
        with mock.patch.object(self.adapter, 'describe', return_value=dict(self.adapter.describe(), version='v2')):
            with self.assertRaises(BridgeError): self.bridge.dispatch(rid, self.adapter)
        self.assertIsNone(self.core.snapshot()['exchange']['requests'][rid]['claim_id'])
        self.assertEqual(self.adapter.starts, 0)
        with self.assertRaises(TypeError): AdaptiveHarnessBridge.create(self.root / 'grant', self.core.root, self.adapter.describe(), max_calls=999)

    def test_expired_execution_grant_does_not_block_read_only_observation_contract(self):
        m = deepcopy(self.m); m['mode'] = 'host_recorded'
        d = dict(self.adapter.describe(), mode='host_recorded', model_execution=True)
        grant = {'schema': 'victorique.bridge_execution_grant.v1', 'grant_id': 'expired_contract_only',
            'session_id': m['session_id'], 'protocol': m['protocol'], 'adapter_id': d['adapter_id'], 'adapter_version': d['version'],
            'allow_execution': True, 'source_ref': 'INERT pure authorization boundary', 'expires_at_utc': '2020-01-01T00:00:00Z'}
        with self.assertRaises(BridgeError): validate_adapter(d, m, grant)
        self.assertEqual(validate_adapter(d, m, grant, for_execution=False), d)
        with self.assertRaises(ValueError): validate_adapter(d, m, None, for_execution=False)

    def test_paid_invalid_response_preserves_bill_and_null_usage(self):
        rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
        charge = {'charge_id': 'inert_bill', 'unit_id': 'inert_unit', 'cumulative_units': 1,
            'final_bill': True, 'source': 'INERT declared liability', 'usage': None}
        result = self.bridge.receive(rid, self.adapter.observation(request, raw=b'not JSON', charge_observation=charge))
        self.assertEqual(result['core_classification'], 'returned_invalid')
        charges = self.core.snapshot()['exchange']['charges']
        self.assertEqual(charges[-1]['charge'], charge)
        self.assertEqual(json.loads(self.rows()[0]['metadata'])['charge_observation'], charge)

    def test_unserializable_metadata_still_archives_available_raw_without_fake_identity(self):
        rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
        raw = response(request.packet)
        result = self.bridge.receive(rid, TransportObservation({'not_json': float('nan')}, raw, b'INERT carrier'))
        self.assertEqual(result['phase'], 'unknown')
        self.assertEqual(self.rows()[0]['response'], raw)
        self.assertEqual(json.loads(self.rows()[0]['capture'])['metadata'], 'unserializable_or_over_limit')
        self.assertEqual(self.bridge.receive(rid, self.adapter.observation(request))['core_classification'], 'returned_valid')

    def test_reused_host_receipt_cannot_poison_later_correct_binding(self):
        first = self.run_one(); first_host = self.bridge.job_status(first)['host_reference']
        rid = self.prepare(); self.crash(rid, 'transport_fenced'); request = self.request(rid)
        result = self.bridge.receive(rid, self.adapter.observation(request, host_reference=first_host))
        self.assertEqual(result['phase'], 'unknown')
        self.assertIn('quarantined_binding', self.rows()[-1]['result'])
        self.assertEqual(self.bridge.receive(rid, self.adapter.observation(request))['core_classification'], 'returned_valid')

    def test_status_lists_every_job_in_creation_order(self):
        first = self.run_one('propose_plan')
        second = self.prepare(); self.bridge.prepare(second)
        jobs = self.bridge.status()['jobs']
        self.assertEqual(jobs, [self.bridge.job_status(rid) for rid in (first, second)])
        self.assertEqual([job['phase'] for job in jobs], ['completed', 'prepared'])

    def test_separate_bridge_root_cannot_reclaim_existing_request(self):
        rid = self.run_one()
        other = AdaptiveHarnessBridge.create(self.root / 'other-bridge', self.core.root, self.adapter.describe())
        with self.assertRaises(BridgeError): other.dispatch(rid, self.adapter)
        self.assertEqual(self.adapter.starts, 1)


if __name__ == '__main__': unittest.main()
