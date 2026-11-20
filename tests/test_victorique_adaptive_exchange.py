"""Exchange tests: synthetic storage faults, exact packets and bills; no mathematical work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from victorique import adaptive_contracts as c
from victorique.adaptive_exchange import AdaptiveExchange, AdaptiveExchangeError, VERSION, MANIFEST_SCHEMA, REQUEST_SCHEMA, RESPONSE_SCHEMA, PLANNING_SELECTION_POLICY, build_packet, validate_manifest, validate_request
from tests import test_victorique_adaptive_contracts as f




def manifest():
    return {'schema': MANIFEST_SCHEMA, 'session_id': 'inert_exchange',
        'protocol': {'protocol_id': 'inert_protocol', 'version': 'v1'}, 'mode': 'synthetic',
        'problem': deepcopy(f.PROBLEM), 'limits': f.limits(),
        'role_bindings': {role: {'actor_id': 'actor_' + role, 'model_request': {'provider_id': 'synthetic_host',
            'model_id': 'inert_model', 'revision': None}, 'fresh_context': True} for role in c.ROLES},
        'implementation_versions': {'contracts': 'frozen_A1', 'controller': 'not_used_A3a', 'exchange': VERSION, 'session': None, 'templates': 'inert_v1'},
        'storage_limits': {'max_packet_bytes': 200000, 'max_decoded_response_bytes': 200000,
            'max_raw_response_bytes': 400000, 'max_session_archive_bytes': 8000000},
        'budget_unit': {'unit_id': 'inert_unit', 'meaning': 'synthetic accounting unit; not money or tokens', 'currency': None},
        'authorization_ref': {'scope': 'synthetic_software_only', 'source_id': 'inert_test'},
        'forbidden_host_references': ['/root/prior_inert_role']}


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='inert-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'session'
        self.manifest = manifest()
        self.store = AdaptiveExchange.create(self.root, self.manifest)

    def base(self, name, sequence='current'):
        return {'schema': REQUEST_SCHEMA if name == 'request' else 'victorique.adaptive_' + name + '.v1', 'session_id': self.manifest['session_id'],
                'operation_id': 'op_' + uuid4().hex,
                'expected_event_sequence': self.store.status()['event_sequence'] if sequence == 'current' else sequence}

    def request(self, kind='propose_plan', *, target=None, plan=None, deps=(), closure=(), issue=None):
        rid = 'request_' + uuid4().hex
        role = self.manifest['limits']['action_roles'][kind]
        result = self.base('request') | {'request_id': rid, 'logical_action_id': 'logical_' + rid,
            'action_id': 'action_' + rid, 'prepared_state_revision': 0, 'kind': kind, 'role': role,
            'actor_id': self.manifest['role_bindings'][role]['actor_id'],
            'route_id': None if kind in ('propose_plan', 'replan') else 'route0',
            'obligation_id': None if kind in ('propose_plan', 'replan') else 'root',
            'dependency_refs': deepcopy(list(deps)),
            'context': {'problem': deepcopy(f.PROBLEM), 'plan': deepcopy(plan), 'target_candidate': deepcopy(target),
                'dependency_closure': deepcopy(list(closure)), 'selected_issue': deepcopy(issue), 'released_feedback': [],
                'planning_evidence': [], 'planning_evidence_selection': {'eligible_count': 0, 'selected_count': 0,
                    'max_entries': 64, 'truncated': False, 'policy': PLANNING_SELECTION_POLICY}},
            'template': {'template_id': 'inert_template', 'version': 'v1', 'instructions': 'INERT software only; no mathematics.'}}
        return result

    def begin(self, request=None):
        request = request or self.request()
        prepared = self.store.prepare(request)
        export_record = self.base('export') | {'request_id': request['request_id']}
        exported = self.store.export_packet(export_record)
        claim_record = self.base('claim') | {'request_id': request['request_id'], 'packet_id': prepared['packet_id']}
        claim = self.store.claim(claim_record)
        host = '/root/inert_' + claim['dispatch_claim']
        bind = self.base('bind', None) | {'request_id': request['request_id'], 'claim_id': claim['dispatch_claim'], 'host_reference': host}
        self.store.bind(bind)
        return request, exported['packet'], claim, host

    def charge(self, units=1, final=True):
        return {'charge_id': 'bill_' + uuid4().hex, 'unit_id': 'inert_unit', 'cumulative_units': units,
            'final_bill': final, 'source': None if units is None else 'inert bill declaration', 'usage': None}

    def observation(self, begun, status='returned', charge=None, sequence=None):
        req, packet, claim, host = begun
        return self.base('observation', sequence) | {'observation_id': 'observation_' + uuid4().hex,
            'request_id': req['request_id'], 'claim_id': claim['dispatch_claim'], 'host_reference': host,
            'transport_status': status, 'source_event_id': None, 'source_line': None, 'external_observed_at_utc': None,
            'model_observation': None, 'charge_observation': charge, 'reason': None}

    def response(self, begun, output=None):
        return json.dumps({'schema': RESPONSE_SCHEMA, 'binding': begun[1]['binding'],
                           'output': f.plan() if output is None else output}, ensure_ascii=False).encode('utf-8')

    def planning_entry(self, sequence=1, kind='critique_candidate', status='completed', target=None):
        if target is None and status == 'completed': target = f.candidate()
        ref = None if target is None else c.candidate_ref(target)
        if status == 'failed': response = None
        elif kind == 'critique_candidate':
            response = {'schema': 'victorique.adaptive_critique.v1', 'target_ref': ref, 'findings': [], 'no_gap_identified': True}
        elif kind == 'investigate': response = f.disposition(target, 'refuted')
        else:
            response = {'schema': 'victorique.adaptive_gate.v1', 'gate_id': kind,
                        'problem_ref': {key: f.PROBLEM[key] for key in ('artifact_id', 'revision')},
                        'target_ref': ref, 'coverage': 'whole_problem_and_proof', 'outcome': 'pass', 'findings': [], 'uncertainties': []}
        return {'receipt_id': 'history_receipt_' + str(sequence), 'action_id': 'history_action_' + str(sequence),
                'plan_ref': {'plan_id': f.plan()['plan_id'], 'revision': 1}, 'action_kind': kind, 'status': status,
                'target_ref': ref, 'target_candidate': deepcopy(target), 'response': deepcopy(response),
                'projection_status': None if status == 'failed' else 'declared_receipt_projected',
                'source': {'receipt_id': 'history_receipt_' + str(sequence), 'host_reference': None if status == 'failed' else '/root/inert_history_' + str(sequence),
                           'event_id': 'history_event_' + str(sequence), 'sequence': sequence, 'source_event_id': None, 'source_line': None,
                           'recorded_at_utc': '2026-11-12T05:00:00Z', 'authenticated': False},
                'release_id': 'planning_release_' + str(sequence), 'read_id': 'planning_read_' + str(sequence),
                'recipient_role': 'planner', 'recipient_actor_id': self.manifest['role_bindings']['planner']['actor_id']}

    def add_planning(self, request, entries):
        request['context']['planning_evidence'] = deepcopy(entries)
        selection = request['context']['planning_evidence_selection']
        selection.update(eligible_count=len(entries), selected_count=len(entries))
        return request

    def test_fresh_manifest_reopen_and_no_adaptive_claim(self):
        self.assertEqual(AdaptiveExchange.open(self.root).status(), self.store.status())
        self.assertFalse(self.store.status()['completion_suffix_protected'])
        self.assertFalse(self.store.status()['adaptive_projection_performed'])
        self.assertEqual(self.store.status()['external_calls_performed'], 0)
        changed = deepcopy(self.manifest); changed['problem']['content'] += ' drift'
        with self.assertRaises(ValueError): AdaptiveExchange.create(self.root, changed)
        unknown = Path(self.tmp.name) / 'occupied'; unknown.mkdir(); (unknown / 'note.txt').write_text('INERT')
        with self.assertRaises(ValueError): AdaptiveExchange.create(unknown, self.manifest)

    def test_v2_replanning_packet_keeps_multiple_exact_targets_normal_results_and_failed_source(self):
        entries = [self.planning_entry(),
                   self.planning_entry(2, 'whole_proof_review', target=f.candidate(revision=2, content='INERT newer body')),
                   self.planning_entry(3, 'investigate'), self.planning_entry(4, 'solve_obligation', 'failed')]
        request = self.add_planning(self.request('replan', plan=f.plan()), entries)
        begun = self.begin(request)
        packet = begun[1]
        self.assertEqual(packet['schema'], 'victorique.adaptive_actor_packet.v2')
        self.assertEqual(packet['context']['planning_evidence'], entries)
        self.assertEqual(packet['binding']['planning_evidence_refs'][1]['target_ref']['revision'], 2)
        self.assertEqual(packet['binding']['planning_evidence_refs'][3]['source_sequence'], 4)
        self.assertIsNone(packet['context']['target_candidate'])
        self.assertEqual(packet['context']['released_feedback'], [])
        self.assertFalse(packet['feedback_release_verified'])
        self.assertEqual(self.store.ingest(self.observation(begun), self.response(begun, f.plan(revision=2)))['classification'], 'returned_valid')

    def test_v2_replanning_rejects_bad_typed_sources_recipients_targets_and_responses(self):
        base = self.add_planning(self.request('replan', plan=f.plan()), [self.planning_entry()])
        bad = []
        def altered(path, value):
            copy = deepcopy(base); current = copy
            for key in path[:-1]: current = current[key]
            current[path[-1]] = value; bad.append(copy)
        prefix = ['context', 'planning_evidence', 0]
        altered(prefix + ['plan_ref', 'revision'], True)
        altered(prefix + ['target_ref', 'revision'], 1.0)
        altered(prefix + ['target_candidate'], None)
        altered(prefix + ['source', 'sequence'], True)
        altered(prefix + ['source', 'source_line'], False)
        altered(prefix + ['source', 'authenticated'], True)
        altered(prefix + ['source', 'receipt_id'], 'other_receipt')
        altered(prefix + ['recipient_role'], 'critic')
        altered(prefix + ['recipient_actor_id'], 'other_actor')
        altered(prefix + ['projection_status'], 'invented_projection')
        altered(prefix + ['response', 'target_ref', 'revision'], True)
        altered(['context', 'planning_evidence_selection', 'eligible_count'], 2)
        altered(['context', 'planning_evidence_selection', 'max_entries'], 64.0)
        altered(['context', 'planning_evidence_selection', 'truncated'], True)
        for index, request in enumerate(bad):
            with self.subTest(index=index), self.assertRaises(ValueError): self.store.prepare(request)
        self.assertEqual(self.store.status()['counts']['reserved_calls'], 0)

    def test_v2_planning_evidence_is_bounded_unique_ordered_and_planner_only(self):
        for entries in ([self.planning_entry(), self.planning_entry()],
                        [self.planning_entry(2), self.planning_entry(1)],
                        [self.planning_entry(i) for i in range(1, 66)]):
            with self.subTest(length=len(entries)), self.assertRaises(ValueError):
                self.store.prepare(self.add_planning(self.request('replan', plan=f.plan()), entries))
        for kind, kwargs in (('propose_plan', {}), ('whole_proof_review', {'plan': f.plan(), 'target': f.candidate()})):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.store.prepare(self.add_planning(self.request(kind, **kwargs), [self.planning_entry()]))
        failed = self.planning_entry(kind='solve_obligation', status='failed'); failed['response'] = {'invented': True}
        with self.assertRaises(ValueError): self.store.prepare(self.add_planning(self.request('replan', plan=f.plan()), [failed]))

    def test_v2_planning_receipt_body_is_frozen_across_requests(self):
        entry = self.planning_entry()
        begun = self.begin(self.add_planning(self.request('replan', plan=f.plan()), [entry]))
        self.store.ingest(self.observation(begun, 'failed'), b'INERT planner failed')
        entry['response'].update(findings=[f.defect(entry['target_candidate'])], no_gap_identified=False)
        with self.assertRaises(ValueError):
            self.store.prepare(self.add_planning(self.request('replan', plan=f.plan()), [entry]))

    def test_v1_manifest_and_response_do_not_silently_migrate_into_v2(self):
        old = deepcopy(self.manifest); old['schema'] = 'victorique.adaptive_session_manifest.v1'
        with self.assertRaises(ValueError): AdaptiveExchange.create(Path(self.tmp.name) / 'old', old)
        begun = self.begin(); response = json.loads(self.response(begun))
        response['schema'] = 'victorique.adaptive_actor_response.v1'
        result = self.store.ingest(self.observation(begun), json.dumps(response).encode('utf-8'))
        self.assertEqual(result['classification'], 'returned_invalid')

    def test_concurrent_prepare_grants_one_reservation(self):
        request = self.request()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.prepare(request), range(2)))
        self.assertEqual(sum(r['prepared_new'] for r in results), 1)
        self.assertEqual(len({r['packet_id'] for r in results}), 1)
        self.assertEqual(self.store.status()['counts']['reserved_calls'], 1)
        changed = deepcopy(request); changed['context']['problem']['content'] += ' changed'
        with self.assertRaises(ValueError): self.store.prepare(changed)

    def test_control_cas_failure_reserves_nothing(self):
        first, second = self.request(), self.request()
        self.store.prepare(first)
        with self.assertRaises(ValueError): self.store.prepare(second)
        self.assertEqual(self.store.status()['counts']['reserved_calls'], 1)

    def test_exact_packet_dependency_bodies_and_no_extra_fields(self):
        p = f.plan(branch=True)
        left, right = f.candidate(oid='left'), f.candidate(oid='right')
        refs = [c.candidate_ref(left), c.candidate_ref(right)]
        req = self.request('integrate', plan=p, deps=refs, closure=[left, right])
        packet, raw = build_packet(req, self.manifest, 'inert_packet')
        self.assertEqual(packet['context']['dependency_closure'], [left, right])
        self.assertEqual(json.loads(raw)['binding']['dependency_refs'], refs)
        missing = deepcopy(req); missing['context']['dependency_closure'].pop()
        with self.assertRaises(ValueError): validate_request(missing, self.manifest)
        extra = deepcopy(req); extra['context']['worker_memory_path'] = 'NEVER_READ'
        with self.assertRaises(ValueError): validate_request(extra, self.manifest)

    def test_cold_view_hides_plan_feedback_and_prior_votes(self):
        target = f.candidate()
        req = self.request('whole_proof_review', plan=f.plan(), target=target)
        packet, raw = build_packet(req, self.manifest, 'inert_packet')
        self.assertIsNone(packet['context']['plan'])
        self.assertEqual(packet['context']['released_feedback'], [])
        self.assertEqual(packet['context']['target_candidate'], target)
        self.assertNotIn('prior_votes', json.loads(raw))
        req['context']['released_feedback'] = [{'receipt_id': 'feedback', 'target_ref': c.candidate_ref(target),
            'kind': 'critique', 'content': 'INERT prior vote', 'release_id': 'release', 'read_id': 'read',
            'recipient_role': req['role'], 'recipient_actor_id': req['actor_id']}]
        with self.assertRaises(ValueError): validate_request(req, self.manifest)

    def test_versioned_issue_keeps_original_quote_and_new_target(self):
        old, current = f.candidate(), f.candidate(revision=2, content='CHANGED inert candidate')
        issue = {'proposal': f.defect(old), 'original_target_candidate': old,
                 'target_ref': c.candidate_ref(current), 'disposition': 'addressed_claim'}
        request = self.request('investigate', plan=f.plan(), target=current, issue=issue)
        begun = self.begin(request)
        result = self.store.ingest(self.observation(begun), self.response(begun, f.disposition(current)))
        self.assertEqual(result['classification'], 'returned_valid')
        self.assertEqual(begun[1]['context']['selected_issue']['proposal']['location']['quote'], 'INERT')

    def test_versioned_issue_target_rejects_boolean_and_float_versions_before_reservation(self):
        target = f.candidate()
        for field in ('revision', 'plan_revision'):
            for malformed in (True, 1.0):
                with self.subTest(field=field, value=malformed):
                    issue = {'proposal': f.defect(target), 'original_target_candidate': deepcopy(target),
                             'target_ref': c.candidate_ref(target), 'disposition': 'proposed'}
                    issue['target_ref'][field] = malformed
                    request = self.request('investigate', plan=f.plan(), target=target, issue=issue)
                    with self.assertRaises(ValueError): self.store.prepare(request)
                    self.assertEqual(self.store.status()['counts']['reserved_calls'], 0)

    def test_wrong_first_candidate_obligation_is_retained_not_accepted(self):
        req = self.request('solve_obligation', plan=f.plan(branch=True)); req['obligation_id'] = 'left'
        begun = self.begin(req)
        result = self.store.ingest(self.observation(begun), self.response(begun, f.candidate(oid='right')))
        self.assertEqual(result['classification'], 'returned_invalid')
        self.assertFalse(result['adaptive_projection_performed'])

    def test_before_commit_crash_rolls_back_without_budget_reservation(self):
        request = self.request()
        def crash(stage, identity):
            if stage == 'before_commit': raise RuntimeError('INERT crash')
        with mock.patch.object(self.store, '_checkpoint', side_effect=crash), self.assertRaises(RuntimeError):
            self.store.prepare(request)
        self.assertEqual(AdaptiveExchange.open(self.root).status()['counts']['reserved_calls'], 0)
        self.assertTrue(self.store.prepare(request)['prepared_new'])

    def test_prepare_after_commit_crash_reopens_same_reservation(self):
        request = self.request()
        def crash(stage, identity):
            if stage == 'prepare_committed': raise RuntimeError('INERT lost return')
        with mock.patch.object(self.store, '_checkpoint', side_effect=crash), self.assertRaises(RuntimeError):
            self.store.prepare(request)
        reopened = AdaptiveExchange.open(self.root)
        self.assertFalse(reopened.prepare(request)['prepared_new'])
        self.assertEqual(reopened.status()['counts']['reserved_calls'], 1)

    def test_lost_claim_return_never_regenerates_dispatch_right(self):
        request = self.request(); prepared = self.store.prepare(request)
        self.store.export_packet(self.base('export') | {'request_id': request['request_id']})
        record = self.base('claim') | {'request_id': request['request_id'], 'packet_id': prepared['packet_id']}
        def crash(stage, identity):
            if stage == 'claim_committed': raise RuntimeError('INERT lost claim')
        with mock.patch.object(self.store, '_checkpoint', side_effect=crash), self.assertRaises(RuntimeError): self.store.claim(record)
        result = AdaptiveExchange.open(self.root).claim(record)
        self.assertIsNone(result['dispatch_claim'])
        self.assertIsNotNone(result['historical_claim_id'])

    def test_unique_host_binding_and_forbidden_reference(self):
        first = self.begin()
        second = self.begin()
        record = self.base('bind', None) | {'request_id': second[0]['request_id'], 'claim_id': second[2]['dispatch_claim'], 'host_reference': first[3]}
        with self.assertRaises(ValueError): self.store.bind(record)
        record['host_reference'] = '/root/prior_inert_role'
        with self.assertRaises(ValueError): self.store.bind(record)

    def test_unknown_blocks_new_work_but_actual_original_return_resolves(self):
        begun = self.begin()
        unknown = self.observation(begun, 'unknown')
        self.store.mark_unknown(unknown)
        self.assertEqual(self.store.status()['counts']['unknown_calls'], 1)
        with self.assertRaises(ValueError): self.store.prepare(self.request())
        result = self.store.ingest(self.observation(begun), self.response(begun))
        self.assertEqual(result['classification'], 'returned_valid')
        self.assertEqual(self.store.status()['counts']['claims'], 1)
        self.assertEqual(self.store.status()['counts']['unknown_calls'], 0)

    def test_stale_external_sequence_preserves_return_and_actual_charge(self):
        begun = self.begin(); record = self.observation(begun, charge=self.charge(), sequence=0)
        raw = self.response(begun)
        result = self.store.ingest(record, raw)
        self.assertEqual(result['classification'], 'returned_valid')
        snap = self.store.snapshot()
        self.assertTrue(snap['observations'][0]['sequence_conflict'])
        self.assertEqual(snap['raw_observations'][record['observation_id']], raw)
        self.assertEqual(snap['budgets']['actual_observed_units'], 1)

    def test_malformed_utf8_json_and_binding_are_archived_with_charge(self):
        for raw in (b'\xff', b'{"x":1,"x":2}', b'{"x":NaN}', b'[' * 50 + b'0' + b']' * 50):
            begun = self.begin(); record = self.observation(begun, charge=self.charge())
            result = self.store.ingest(record, raw)
            self.assertEqual(result['classification'], 'returned_invalid')
            self.assertEqual(self.store.snapshot()['raw_observations'][record['observation_id']], raw)
            self.assertEqual(result['charge_classification'], 'accepted')

    def test_wrong_host_is_quarantined_with_raw_and_suspense_bill(self):
        begun = self.begin(); record = self.observation(begun, charge=self.charge()); record['host_reference'] = '/root/wrong_host'
        result = self.store.ingest(record, self.response(begun))
        self.assertEqual(result['classification'], 'quarantined_binding')
        self.assertEqual(result['charge_classification'], 'quarantined')
        self.assertTrue(self.store.status()['budgets']['financial_blocked'])
        self.assertEqual(len(self.store.snapshot()['raw_observations']), 1)

    def test_observation_idempotency_and_conflicting_raw(self):
        begun = self.begin(); record = self.observation(begun); raw = self.response(begun)
        self.assertTrue(self.store.ingest(record, raw)['recorded_new'])
        self.assertFalse(self.store.ingest(record, raw)['recorded_new'])
        with self.assertRaises(ValueError): self.store.ingest(record, raw + b' ')
        self.assertEqual(self.store.status()['counts']['observations'], 1)

    def test_decode_oversize_still_fully_archived(self):
        begun = self.begin(); raw = b'x' * (self.manifest['storage_limits']['max_decoded_response_bytes'] + 1)
        record = self.observation(begun, charge=self.charge())
        result = self.store.ingest(record, raw)
        self.assertEqual(result['classification'], 'returned_invalid')
        self.assertEqual(result['capture']['status'], 'full')
        self.assertEqual(self.store.snapshot()['raw_observations'][record['observation_id']], raw)

    def test_storage_failure_seals_and_retains_cost_without_claiming_full_capture(self):
        begun = self.begin(); raw = b'x' * (self.manifest['storage_limits']['max_raw_response_bytes'] + 1)
        record = self.observation(begun, charge=self.charge())
        result = self.store.ingest(record, raw)
        self.assertEqual(result['classification'], 'capture_failed')
        self.assertTrue(result['original_bytes_must_be_preserved'])
        self.assertTrue(self.store.status()['sealed'])
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 1)
        self.assertIsNone(self.store.snapshot()['raw_observations'][record['observation_id']])
        with self.assertRaises(ValueError): self.store.ingest(record, raw)

    def test_explicit_size_capture_failure_keeps_bill_and_replays_without_raw(self):
        begun = self.begin(); record = self.observation(begun, charge=self.charge(1))
        result = self.store.record_capture_failure(record, 10_000_000, 'size_limit')
        self.assertEqual(result['classification'], 'capture_failed')
        self.assertEqual(result['capture'], {'status': 'size_limit', 'original_byte_length': 10_000_000})
        self.assertEqual(result['charge_classification'], 'accepted')
        self.assertFalse(result['raw_response_archived'])
        self.assertFalse(result['redispatch_permitted'])
        snap = self.store.snapshot()
        self.assertEqual(snap['endpoint']['decision'], 'capture_failed')
        self.assertIsNone(snap['raw_observations'][record['observation_id']])
        self.assertEqual(snap['budgets']['actual_observed_units'], 1)
        self.assertEqual(AdaptiveExchange.open(self.root).snapshot(), snap)
        with self.assertRaises(ValueError): self.store.prepare(self.request())

    def test_unreadable_capture_unknown_size_and_unknown_cost_are_not_zero(self):
        begun = self.begin(); record = self.observation(begun)
        result = self.store.record_capture_failure(record, None, 'unreadable')
        self.assertIsNone(result['capture']['original_byte_length'])
        self.assertIsNone(self.store.status()['budgets']['actual_observed_units'])
        self.assertEqual(self.store.snapshot()['requests'][begun[0]['request_id']]['phase'], 'bound')
        self.assertEqual(self.store.snapshot()['observations'][0]['record']['transport_status'], 'returned')
        self.assertFalse(self.store.record_capture_failure(record, None, 'unreadable')['recorded_new'])
        with self.assertRaises(ValueError): self.store.record_capture_failure(record, 10, 'unreadable')
        with self.assertRaises(ValueError): self.store.ingest(record, b'changed source cannot replace audit')

    def test_capture_failure_late_original_result_preserves_frozen_endpoint_and_bill(self):
        begun = self.begin(); record = self.observation(begun, charge=self.charge(1, final=False))
        self.store.record_capture_failure(record, 100, 'unreadable')
        frozen = self.store.snapshot()['endpoint']
        returned = self.store.ingest(self.observation(begun, charge=self.charge(1)), self.response(begun))
        self.assertTrue(returned['historical_only'])
        self.assertEqual(returned['classification'], 'returned_valid')
        self.assertEqual(self.store.snapshot()['endpoint'], frozen)
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 1)
        self.assertTrue(self.store.status()['budgets']['financially_settled'])

    def test_capture_failure_wrong_host_quarantines_without_charging_bound_request(self):
        begun = self.begin(); record = self.observation(begun, 'unknown', charge=self.charge(1))
        record['host_reference'] = '/root/unrelated_actual_role'
        result = self.store.record_capture_failure(record, 10, 'unreadable')
        self.assertEqual(result['classification'], 'quarantined_binding')
        self.assertEqual(result['charge_classification'], 'quarantined')
        snap = self.store.snapshot()
        self.assertIsNone(snap['endpoint'])
        self.assertIsNone(snap['requests'][begun[0]['request_id']]['known_cost_units'])
        self.assertTrue(snap['financial_blocked'])

    def test_quarantined_charge_id_does_not_hide_later_correctly_attributed_bill(self):
        begun = self.begin(); bill = self.charge(1)
        wrong = self.observation(begun, charge=deepcopy(bill))
        wrong['host_reference'] = '/root/unrelated_billing_role'
        self.assertEqual(self.store.record_capture_failure(wrong, 100, 'size_limit')['charge_classification'], 'quarantined')
        correct = self.observation(begun, charge=deepcopy(bill))
        self.assertEqual(self.store.record_capture_failure(correct, 100, 'size_limit')['charge_classification'], 'accepted')
        snap = self.store.snapshot()
        self.assertEqual(snap['requests'][begun[0]['request_id']]['known_cost_units'], 1)
        self.assertEqual(snap['budgets']['actual_observed_units'], 1)
        self.assertEqual([row['classification'] for row in snap['charges']], ['quarantined', 'accepted'])
        self.assertTrue(snap['financial_blocked'])
        duplicate = self.observation(begun, charge=deepcopy(bill))
        self.assertEqual(self.store.record_capture_failure(duplicate, 100, 'size_limit')['charge_classification'], 'duplicate')
        conflict = self.observation(begun, charge=dict(bill, cumulative_units=2))
        self.assertEqual(self.store.record_capture_failure(conflict, 100, 'size_limit')['charge_classification'], 'quarantined')
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 1)
        self.assertEqual(AdaptiveExchange.open(self.root).snapshot(), self.store.snapshot())

    def test_capture_failure_lengths_are_strict_and_do_not_reserve_fake_archive(self):
        begun = self.begin()
        for size, kind in ((True, 'unreadable'), (1.0, 'size_limit'), (-1, 'unreadable'),
                           (None, 'size_limit'), (0, 'size_limit'), (1, 'invented')):
            with self.subTest(size=size, kind=kind):
                with self.assertRaises(ValueError):
                    self.store.record_capture_failure(self.observation(begun), size, kind)
        self.assertEqual(self.store.status()['counts']['observations'], 0)

    def test_terminal_late_bill_and_result_do_not_rewrite_endpoint(self):
        begun = self.begin()
        self.store.ingest(self.observation(begun), self.response(begun))
        self.store.close(self.base('close') | {'reason': 'INERT frozen endpoint'})
        before = self.store.snapshot()['endpoint']
        bill = self.base('charge', None) | {'request_id': begun[0]['request_id'], 'claim_id': begun[2]['dispatch_claim'],
                'host_reference': begun[3], 'charge_observation': self.charge()}
        self.store.record_charge(bill)
        late = self.store.ingest(self.observation(begun), b'INERT different late response')
        self.assertTrue(late['historical_only'])
        self.assertEqual(self.store.snapshot()['endpoint'], before)
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 1)
        self.assertEqual(self.store.status()['counts']['terminal_calls'], 1)

    def test_cumulative_charges_not_summed_and_regression_preserved(self):
        begun = self.begin()
        charge1 = self.charge(units=0, final=False)
        self.store.ingest(self.observation(begun, charge=charge1), self.response(begun))
        def submit(value):
            return self.store.record_charge(self.base('charge', None) | {'request_id': begun[0]['request_id'],
                'claim_id': begun[2]['dispatch_claim'], 'host_reference': begun[3], 'charge_observation': value})
        submit(self.charge(units=1, final=True)); submit(self.charge(units=0, final=True))
        snap = self.store.snapshot()
        self.assertEqual(snap['budgets']['actual_observed_units'], 1)
        self.assertEqual(snap['charges'][-1]['classification'], 'quarantined')
        self.assertTrue(snap['budgets']['financial_blocked'])

    def test_duplicate_cumulative_charge_not_double_charged(self):
        begun = self.begin(); charge = self.charge()
        self.store.ingest(self.observation(begun, charge=charge), self.response(begun))
        self.store.record_charge(self.base('charge', None) | {'request_id': begun[0]['request_id'],
            'claim_id': begun[2]['dispatch_claim'], 'host_reference': begun[3], 'charge_observation': charge})
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 1)
        self.assertEqual(self.store.snapshot()['charges'][-1]['classification'], 'duplicate')

    def test_unknown_units_remain_null_and_cap_responsibility_survives_failure(self):
        begun = self.begin(); self.store.ingest(self.observation(begun, 'failed'), b'INERT host failure')
        budget = self.store.status()['budgets']
        self.assertIsNone(budget['actual_observed_units'])
        self.assertEqual(budget['committed_units'], 1)
        self.assertIsNone(budget['usage'])
        self.assertFalse(budget['financially_settled'])

    def test_true_cap_overspend_retained_and_blocks_later_work(self):
        begun = self.begin(); self.store.ingest(self.observation(begun, charge=self.charge(units=2)), self.response(begun))
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 2)
        self.assertTrue(self.store.status()['budgets']['request_or_total_cap_exceeded'])
        with self.assertRaises(ValueError): self.store.prepare(self.request())

    def test_call_role_fee_and_inflight_limits_are_exact(self):
        self.begin(); self.begin()
        with self.assertRaises(ValueError): self.store.prepare(self.request())
        for field, replacement in [('global_call_limit', 1), ('total_budget_units', 2),
                                   ('role_call_limits', {r: 1 for r in c.ROLES}), ('role_budget_units', {r: 1 for r in c.ROLES})]:
            m = manifest(); m['limits'][field] = replacement
            oldstore, oldmanifest = self.store, self.manifest
            self.store = AdaptiveExchange.create(Path(self.tmp.name) / ('limit_' + field), m); self.manifest = m
            begun = self.begin(); self.store.ingest(self.observation(begun), self.response(begun))
            if field == 'total_budget_units':
                extra = self.begin(); self.store.ingest(self.observation(extra), self.response(extra))
            with self.assertRaises(ValueError): self.store.prepare(self.request())
            self.store, self.manifest = oldstore, oldmanifest

    def test_same_frozen_reference_cannot_change_body_between_requests(self):
        first = self.begin(self.request('critique_candidate', plan=f.plan(), target=f.candidate()))
        self.store.ingest(self.observation(first, 'failed'), b'INERT failed role')
        changed = self.request('critique_candidate', plan=f.plan(), target=f.candidate(content='CHANGED same reference'))
        with self.assertRaises(ValueError): self.store.prepare(changed)
        changed_plan = f.plan(); changed_plan['routes'][0]['obligations'][0]['statement'] += ' changed'
        with self.assertRaises(ValueError):
            self.store.prepare(self.request('critique_candidate', plan=changed_plan, target=f.candidate()))

    def test_known_observed_model_mismatch_preserves_output_and_cost(self):
        begun = self.begin(); record = self.observation(begun, charge=self.charge())
        record['model_observation'] = {'provider_id': 'synthetic_host', 'model_id': 'other_model', 'revision': None}
        result = self.store.ingest(record, self.response(begun))
        self.assertEqual(result['classification'], 'returned_invalid_model')
        self.assertEqual(result['charge_classification'], 'accepted')
        self.assertEqual(self.store.status()['budgets']['actual_observed_units'], 1)
        self.assertEqual(self.store.snapshot()['requests'][begun[0]['request_id']]['phase'], 'returned_invalid_model')
        self.assertEqual(self.store.status()['counts']['terminal_calls'], 1)

    def test_critique_reference_rejects_boolean_and_float_versions_but_keeps_receipt_and_fee(self):
        target = f.candidate()
        for field in ('revision', 'plan_revision'):
            for malformed in (True, 1.0):
                with self.subTest(field=field, value=malformed):
                    begun = self.begin(self.request('critique_candidate', plan=f.plan(), target=target))
                    output = {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(target),
                              'findings': [], 'no_gap_identified': True}
                    output['target_ref'][field] = malformed
                    raw = self.response(begun, output); record = self.observation(begun, charge=self.charge())
                    result = self.store.ingest(record, raw)
                    self.assertEqual(result['classification'], 'returned_invalid')
                    self.assertEqual(result['charge_classification'], 'accepted')
                    self.assertEqual(self.store.snapshot()['raw_observations'][record['observation_id']], raw)

    def test_gate_references_reject_boolean_and_float_versions_but_keep_receipt_and_fee(self):
        target = f.candidate()
        for reference, field in (('target_ref', 'revision'), ('target_ref', 'plan_revision'), ('problem_ref', 'revision')):
            for malformed in (True, 1.0):
                with self.subTest(reference=reference, field=field, value=malformed):
                    begun = self.begin(self.request('whole_proof_review', plan=f.plan(), target=target))
                    output = {'schema': 'victorique.adaptive_gate.v1', 'gate_id': 'whole_proof_review',
                              'problem_ref': deepcopy(begun[1]['binding']['problem_ref']), 'target_ref': c.candidate_ref(target),
                              'coverage': 'whole_problem_and_proof', 'outcome': 'pass', 'findings': [], 'uncertainties': []}
                    output[reference][field] = malformed
                    raw = self.response(begun, output); record = self.observation(begun, charge=self.charge())
                    result = self.store.ingest(record, raw)
                    self.assertEqual(result['classification'], 'returned_invalid')
                    self.assertEqual(result['charge_classification'], 'accepted')
                    self.assertEqual(self.store.snapshot()['raw_observations'][record['observation_id']], raw)

    def test_reopen_rejects_boolean_and_float_event_sequence_even_when_sqlite_index_matches(self):
        for index, malformed in enumerate((True, 1.0)):
            with self.subTest(value=malformed):
                store = AdaptiveExchange.create(Path(self.tmp.name) / ('typed-sequence-' + str(index)), self.manifest)
                request = self.request(); request['expected_event_sequence'] = 0
                store.prepare(request)
                with closing(sqlite3.connect(store.path)) as db, db:
                    triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'").fetchall()
                    for (name,) in triggers:
                        db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
                    row = db.execute('SELECT sequence,payload FROM events').fetchone()
                    self.assertEqual(type(row[0]), int)
                    event = json.loads(row[1]); event['sequence'] = malformed
                    db.execute('UPDATE events SET payload=? WHERE sequence=?', (json.dumps(event), row[0]))
                with self.assertRaises(ValueError): AdaptiveExchange.open(store.root)

    def test_deadline_is_checked_without_inventing_research_timeout(self):
        m = manifest(); m['limits']['stop_condition']['deadline_utc'] = '2000-01-01T00:00:00Z'
        self.store = AdaptiveExchange.create(Path(self.tmp.name) / 'expired', m); self.manifest = m
        with self.assertRaises(ValueError): self.store.prepare(self.request())
        self.assertEqual(self.store.status()['counts']['reserved_calls'], 0)

    def test_replay_rejects_modified_cache_and_partial_database(self):
        self.begin()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            row = db.execute('SELECT request_id,payload FROM requests').fetchone()
            value = json.loads(row[1]); value['phase'] = 'returned_valid'
            db.execute('UPDATE requests SET payload=? WHERE request_id=?', (json.dumps(value), row[0]))
        with self.assertRaises(ValueError): AdaptiveExchange.open(self.root)
        with closing(sqlite3.connect(self.store.path)) as db, db: db.execute('DROP TABLE charges')
        with self.assertRaises(ValueError): AdaptiveExchange.open(self.root)

    def test_replay_checks_raw_bytes_and_immutable_tables_refuse_mutation(self):
        begun = self.begin(); record = self.observation(begun)
        self.store.ingest(record, self.response(begun))
        with closing(sqlite3.connect(self.store.path)) as db, db:
            with self.assertRaises(sqlite3.DatabaseError): db.execute("UPDATE observations SET raw=X'00'")
            db.execute('DROP TRIGGER immutable_observations_update')
            db.execute("UPDATE observations SET raw=X'00'")
        with self.assertRaises(ValueError): AdaptiveExchange.open(self.root)

    def rewrite(self, path, table, statement, args=()):
        """Change a protected table outside any object, then restore its triggers exactly."""
        with closing(sqlite3.connect(path)) as db, db:
            triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
            for name, _ in triggers: db.execute('DROP TRIGGER "' + name + '"')
            db.execute(statement, args)
            for _, sql in triggers: db.execute(sql)

    def first_call(self):
        begun = self.begin()
        self.store.ingest(self.observation(begun, charge=self.charge()), self.response(begun))
        return begun

    def test_same_object_detects_rows_changed_between_its_transactions(self):
        def event_payload(path, which):
            with closing(sqlite3.connect(path)) as db:
                sequence, raw = db.execute('SELECT sequence,payload FROM events ORDER BY sequence ' + which + ' LIMIT 1').fetchone()
            value = json.loads(raw); value['event_id'] = 'event_changed_outside'
            return sequence, json.dumps(value)
        def request_row(path):
            with closing(sqlite3.connect(path)) as db, db:
                rid, raw = db.execute('SELECT request_id,payload FROM requests').fetchone()
                value = json.loads(raw); value['known_cost_units'] = 0
                db.execute('UPDATE requests SET payload=? WHERE request_id=?', (json.dumps(value), rid))
        def last_event(path):  # schema unchanged; the last verified event row differs
            sequence, payload = event_payload(path, 'DESC')
            self.rewrite(path, 'events', 'UPDATE events SET payload=? WHERE sequence=?', (payload, sequence))
        def earlier_event(path):  # trigger left dropped
            sequence, payload = event_payload(path, 'ASC')
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('DROP TRIGGER immutable_events_update')
                db.execute('UPDATE events SET payload=? WHERE sequence=?', (payload, sequence))
        def raw_bytes(path):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('DROP TRIGGER immutable_observations_update'); db.execute("UPDATE observations SET raw=X'00'")
        def extra_packet(path):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO packets VALUES (?,?,?)', ('packet_outside', 'request_outside', b'{}'))
        def event_gap(path):
            with closing(sqlite3.connect(path)) as db, db:
                sequence, raw = db.execute('SELECT sequence,payload FROM events ORDER BY sequence DESC LIMIT 1').fetchone()
                db.execute('INSERT INTO events VALUES (?,?,?,?)', (sequence + 2, 'event_outside', 'operation_outside', raw))
        def event_before_first(path):
            with closing(sqlite3.connect(path)) as db, db:
                raw = db.execute('SELECT payload FROM events WHERE sequence=1').fetchone()[0]
                db.execute('INSERT INTO events VALUES (?,?,?,?)', (0, 'event_outside', 'operation_outside', raw))
        def metadata(path):  # schema unchanged
            self.rewrite(path, 'metadata', "UPDATE metadata SET payload=? WHERE key='root'",
                         (json.dumps(str(Path(self.tmp.name) / 'elsewhere')),))
        def endpoint(path):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO endpoint VALUES (1,?)', (json.dumps({'decision': 'closed'}),))
        for change in (request_row, last_event, earlier_event, raw_bytes, extra_packet, event_gap, event_before_first, metadata, endpoint):
            with self.subTest(change=change.__name__):
                self.store = AdaptiveExchange.create(Path(self.tmp.name) / ('changed-' + change.__name__), self.manifest)
                self.first_call(); self.store.status()
                change(self.store.path)
                with self.assertRaises(ValueError): self.store.status()
                with self.assertRaises(ValueError): self.store.status()
                with self.assertRaises(ValueError): AdaptiveExchange.open(self.store.root)

    def test_any_other_difference_is_decided_by_the_full_replay(self):
        self.first_call(); self.store.status()
        with closing(sqlite3.connect(self.store.path)) as db, db:  # same content, other JSON form
            rid, raw = db.execute('SELECT request_id,payload FROM requests').fetchone()
            db.execute('UPDATE requests SET payload=? WHERE request_id=?', (json.dumps(json.loads(raw), indent=1), rid))
        with mock.patch.object(self.store, '_replay_full', wraps=self.store._replay_full) as full:
            self.assertEqual(self.store.status()['counts']['terminal_calls'], 1)
            self.assertEqual(self.store.status()['counts']['terminal_calls'], 1)
        self.assertEqual(full.call_count, 1)

    def test_events_appended_by_another_object_are_applied_without_full_replay(self):
        begun = self.begin()
        other = AdaptiveExchange.open(self.root)
        with mock.patch.object(self.store, '_replay_full', wraps=self.store._replay_full) as full:
            other.ingest(self.observation(begun, charge=self.charge()), self.response(begun))
            other.record_charge(self.base('charge', None) | {'request_id': begun[0]['request_id'],
                'claim_id': begun[2]['dispatch_claim'], 'host_reference': begun[3], 'charge_observation': self.charge()})
            status = self.store.status(); snapshot = self.store.snapshot()
            self.assertEqual(full.call_count, 0)
        self.assertEqual(status['counts']['terminal_calls'], 1)
        self.assertEqual(status['event_sequence'], other.status()['event_sequence'])
        self.assertEqual(snapshot, other.snapshot())
        self.assertEqual(snapshot, AdaptiveExchange.open(self.root).snapshot())
        self.assertTrue(self.store.prepare(self.request())['prepared_new'])
        self.assertEqual(other.status()['counts']['reserved_calls'], 2)

    def test_only_the_first_transaction_of_an_object_replays_every_event(self):
        full = self.enterContext(mock.patch.object(self.store, '_replay_full', wraps=self.store._replay_full))
        begun = self.first_call()
        with self.assertRaises(ValueError): self.store.prepare(self.request() | {'expected_event_sequence': 0})
        self.store.close(self.base('close') | {'reason': 'INERT frozen endpoint'})
        self.store.record_charge(self.base('charge', None) | {'request_id': begun[0]['request_id'],
            'claim_id': begun[2]['dispatch_claim'], 'host_reference': begun[3], 'charge_observation': self.charge()})
        self.assertEqual(self.store.snapshot(), AdaptiveExchange.open(self.root).snapshot())
        self.assertEqual(full.call_count, 1)

    def test_open_after_a_change_runs_the_full_replay(self):
        self.first_call(); self.store.status()
        # The first event changes; the schema, row counts and last event stay as they were.
        with closing(sqlite3.connect(self.store.path)) as db:
            value = json.loads(db.execute('SELECT payload FROM events WHERE sequence=1').fetchone()[0])
        value['event_id'] = 'event_changed_outside'
        self.rewrite(self.store.path, 'events', 'UPDATE events SET payload=? WHERE sequence=1', (json.dumps(value),))
        with mock.patch.object(AdaptiveExchange, '_replay_full', autospec=True, side_effect=AdaptiveExchange._replay_full) as full:
            with self.assertRaisesRegex(ValueError, 'event columns differ'): AdaptiveExchange.open(self.root)
            with self.assertRaisesRegex(ValueError, 'event columns differ'): AdaptiveExchange.create(self.root, self.manifest)
        self.assertEqual(full.call_count, 2)

    def test_arbitrary_new_directories_can_be_created_and_reopened(self):
        for relative in ('new-output', 'nested/project/run', 'observers/trial',
                         'learning/session', 'runtime/victorique/session'):
            with self.subTest(relative=relative):
                root = Path(self.tmp.name) / relative
                self.assertFalse(root.exists())
                store = AdaptiveExchange.create(root, manifest())
                self.assertEqual(AdaptiveExchange.open(root).snapshot(), store.snapshot())

    def test_git_metadata_and_parent_traversal_refused(self):
        for relative in ('.git', '.git/trial', '.GIT/trial', 'nested/../outside'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                AdaptiveExchange.create(Path(self.tmp.name) / relative, manifest())

    def test_hardlinked_database_refused(self):
        other = Path(self.tmp.name) / 'db-alias'
        try: other.hardlink_to(self.store.path)
        except OSError: self.skipTest('hard links not available')
        with self.assertRaises(ValueError): AdaptiveExchange.open(self.root)

    def test_hardlinked_sidecar_refused_on_every_transaction(self):
        self.store.status()
        target = Path(self.tmp.name) / 'outside-file'; target.write_bytes(b'INERT')
        sidecar = Path(str(self.store.path) + '-wal')
        try: sidecar.hardlink_to(target)
        except OSError: self.skipTest('hard links not available')
        with self.assertRaisesRegex(ValueError, 'hardlinked'): self.store.status()
        sidecar.unlink()
        self.assertEqual(self.store.status()['event_sequence'], 0)

    def test_symlink_root_and_parent_refused(self):
        alias = Path(self.tmp.name) / 'session-alias'
        try: alias.symlink_to(self.root, target_is_directory=True)
        except OSError: self.skipTest('symbolic links not available')
        with self.assertRaises(ValueError): AdaptiveExchange.open(alias)
        with self.assertRaises(ValueError): AdaptiveExchange.create(alias / 'child', manifest())

    def test_source_subclass_extensions_share_transaction_and_replay(self):
        class Extension(AdaptiveExchange):
            EXTRA_TABLES = ('inert_extension',)
            def _create_extension(self, db, manifest): db.execute('CREATE TABLE inert_extension(sequence INTEGER PRIMARY KEY)')
            def _after_append(self, db, prior_base, new_base, event, raw): db.execute('INSERT INTO inert_extension VALUES (?)', (event['sequence'],))
            def _verify_extension(self, db, base_state):
                if [r[0] for r in db.execute('SELECT sequence FROM inert_extension ORDER BY sequence')] != list(range(1, base_state['sequence'] + 1)):
                    raise AdaptiveExchangeError('extension replay differs')
        self.store = Extension.create(Path(self.tmp.name) / 'extension', self.manifest)
        self.begin()
        self.assertEqual(Extension.open(self.store.root).status()['counts']['claims'], 1)
        with self.assertRaises(ValueError): AdaptiveExchange.open(self.store.root)

    def test_metadata_status_does_not_export_prompt_or_proof(self):
        self.begin()
        raw = json.dumps(self.store.status())
        self.assertNotIn('INERT SOFTWARE FIXTURE', raw)
        self.assertNotIn('instructions', raw)
        self.assertNotIn('context', raw)


if __name__ == '__main__':
    unittest.main()
