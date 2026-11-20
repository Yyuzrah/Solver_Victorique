"""INERT session helper and durable session tests; no mathematical work."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from victorique import adaptive_contracts as c
from victorique import adaptive_controller as controller
from victorique import adaptive_exchange as exchange
from victorique.adaptive_session import AdaptiveSession, completion_reserve, financial_admission, dependency_closure
from victorique.adaptive_session import CONTRACTS_VERSION, CONTROLLER_VERSION, SESSION_VERSION, TEMPLATES_VERSION
from tests.test_victorique_adaptive_exchange import manifest
from tests.test_victorique_adaptive_contracts import candidate, defect, disposition, PROBLEM
from unittest import mock
from tests.test_victorique_adaptive_contracts import NOW, limits, plan, node
from tests.test_victorique_adaptive_controller import planned, produce, reserve, reply, answer_candidate


def liabilities(state):
    return [{"action_id": row["action_id"], "role": row["proposal"]["role"],
             "cap_units": row["proposal"]["cap_units"], "cumulative_units": row["cost_units"],
             "final_bill": False} for row in state["actions"]]


class SessionHelperTests(unittest.TestCase):
    def test_completion_reserve_covers_first_plan_root_and_two_gates(self):
        from tests.test_victorique_adaptive_contracts import PROBLEM
        state = controller.create_state("helper", PROBLEM, limits())
        proposal = controller.choose(state, NOW)["action"]
        result = completion_reserve(state, proposal, NOW)
        self.assertEqual(result["calls"], 4)
        self.assertEqual(result["action_kinds"], ["propose_plan", "solve_obligation", "alignment_review", "whole_proof_review"])
        self.assertFalse(result["semantic_success_guaranteed"])

    def test_reserve_rejects_stale_or_type_changed_proposal(self):
        state = planned()
        value = controller.choose(state, NOW)["action"]
        for key, changed in (("cap_units", True), ("state_revision", 0)):
            wrong = deepcopy(value); wrong[key] = changed
            with self.assertRaises(c.AdaptiveContractError): completion_reserve(state, wrong, NOW)

    def test_final_bill_settlement_does_not_double_add_reducer_costs(self):
        state = planned(limits(total_budget_units=4, max_critiques_per_candidate=0))
        proposal = controller.choose(state, NOW)["action"]
        rows = liabilities(state)
        rows[0].update(cumulative_units=0, final_bill=True)
        result = financial_admission(state, proposal, rows, NOW)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["current_liability_units"], 0)
        self.assertEqual(result["completion_reserve"]["units"], 3)

    def test_unknown_bill_is_null_and_nonfinal_bill_keeps_cap(self):
        state = planned()
        proposal = controller.choose(state, NOW)["action"]
        result = financial_admission(state, proposal, liabilities(state), NOW)
        self.assertIsNone(result["actual_cost_units"])
        self.assertEqual(result["current_liability_units"], 1)
        rows = liabilities(state); rows[0]["cumulative_units"] = 0
        self.assertEqual(financial_admission(state, proposal, rows, NOW)["current_liability_units"], 1)

    def test_late_bill_request_cap_violation_blocks_remaining_reserve(self):
        state = planned()
        rows = liabilities(state); rows[0]["cumulative_units"] = 2
        result = financial_admission(state, controller.choose(state, NOW)["action"], rows, NOW)
        self.assertFalse(result["admitted"])
        self.assertIn("request_cap_exceeded:" + rows[0]["action_id"], result["reasons"])

    def test_finance_rejects_missing_duplicate_changed_or_erased_binding(self):
        state = planned()
        proposal = controller.choose(state, NOW)["action"]
        rows = liabilities(state)
        bad = [[], rows + rows, [dict(rows[0], cap_units=True)], [dict(rows[0], final_bill=True)]]
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises(c.AdaptiveContractError):
                financial_admission(state, proposal, candidate, NOW)
        state = reserve(planned(), "solve_obligation")
        state = reply(state, answer_candidate(state), cost=1)
        rows = liabilities(state); rows[-1]["cumulative_units"] = None
        with self.assertRaises(c.AdaptiveContractError):
            financial_admission(state, controller.choose(state, NOW)["action"], rows, NOW)

    def test_context_closure_has_exact_bodies_and_dependencies_first(self):
        state = produce(produce(planned(p=plan(branch=True)), oid="left"), oid="right")
        proposal = next(x for x in controller.legal_actions(state, NOW)["actions"] if x["kind"] == "integrate")
        result = dependency_closure(state, proposal)
        self.assertEqual([row["obligation_id"] for row in result], ["left", "right"])
        result[0]["content"] = "changed external copy"
        self.assertNotEqual(result[0]["content"], dependency_closure(state, proposal)[0]["content"])
        bad = deepcopy(proposal); bad["dependency_refs"][0]["revision"] = True
        with self.assertRaises(c.AdaptiveContractError): dependency_closure(state, bad)


class DurableSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='inert-')
        self.addCleanup(self.tmp.cleanup)
        self.root = (Path(self.tmp.name) / 'victorique-development') / 'session'
        self.manifest = manifest()
        self.manifest['implementation_versions'].update(contracts=CONTRACTS_VERSION, controller=CONTROLLER_VERSION,
            session=SESSION_VERSION, templates=TEMPLATES_VERSION)
        self.store = AdaptiveSession.create(self.root, self.manifest)

    def command(self):
        status = self.store.status()
        return {'session_id': self.manifest['session_id'], 'operation_id': 'op_' + uuid4().hex,
                'expected_session_sequence': status['session_sequence'], 'expected_state_revision': status['state_revision']}

    def base(self, kind, observation=False):
        return {'schema': 'victorique.adaptive_' + kind + '.v1', 'session_id': self.manifest['session_id'],
                'operation_id': 'op_' + uuid4().hex,
                'expected_event_sequence': None if observation else self.store.status()['event_sequence']}

    def start(self, kind=None, oid=None, route=None):
        listing = self.store.propose()['legal_action_listing']['actions']
        proposal = next(p for p in listing if (kind is None or p['kind'] == kind) and (oid is None or p['obligation_id'] == oid)
                        and (route is None or p['route_id'] == route))
        rid = 'req_' + uuid4().hex
        command = self.command() | {'request_id': rid, 'logical_action_id': 'logical_' + uuid4().hex,
                                   'action_id': 'act_' + uuid4().hex, 'selected_proposal': proposal}
        prepared = self.store.prepare(command)
        packet = self.store.export_packet(self.base('export') | {'request_id': rid})['packet']
        claim = self.store.claim(self.base('claim') | {'request_id': rid, 'packet_id': prepared['packet_id']})
        host = '/root/synthetic_' + uuid4().hex
        self.store.bind(self.base('bind', True) | {'request_id': rid, 'claim_id': claim['dispatch_claim'], 'host_reference': host})
        return {'command': command, 'proposal': proposal, 'request_id': rid, 'packet': packet,
                'claim_id': claim['dispatch_claim'], 'host_reference': host}

    def observe(self, started, output=None, *, raw=None, status='returned', units=None):
        charge = None if units is None else {'charge_id': 'bill_' + uuid4().hex, 'unit_id': 'inert_unit',
            'cumulative_units': units, 'final_bill': False, 'source': 'INERT declared units', 'usage': None}
        record = self.base('observation', True) | {'observation_id': 'obs_' + uuid4().hex,
            'request_id': started['request_id'], 'claim_id': started['claim_id'], 'host_reference': started['host_reference'],
            'transport_status': status, 'source_event_id': None, 'source_line': None, 'external_observed_at_utc': None,
            'model_observation': None, 'charge_observation': charge, 'reason': None}
        if raw is None and status == 'returned':
            raw = json.dumps({'schema': exchange.RESPONSE_SCHEMA, 'binding': started['packet']['binding'], 'output': output}).encode()
        result = self.store.ingest(record, raw)
        return record, result

    def solve(self, kind='solve_obligation', oid=None, route=None):
        started = self.start(kind, oid, route)
        p = started['proposal']; target = p['target_ref']
        output = candidate(revision=1 if target is None else target['revision'] + 1,
            oid=p['obligation_id'], route=p['route_id'], plan_revision=p['plan_ref']['revision'], dependencies=p['dependency_refs'],
            issues=[] if p['issue_id'] is None else [p['issue_id']])
        self.observe(started, output)
        return output

    def initialize(self, branching=False):
        self.observe(self.start('propose_plan'), plan(branch=branching))

    def finish_gate(self, kind):
        started = self.start(kind)
        self.observe(started, {'schema': 'victorique.adaptive_gate.v1', 'gate_id': kind,
            'problem_ref': {'artifact_id': PROBLEM['artifact_id'], 'revision': 1},
            'target_ref': started['proposal']['target_ref'], 'coverage': 'whole_problem_and_proof',
            'outcome': 'pass', 'findings': [], 'uncertainties': []})
        return started

    def handoff(self, role, issue_id='issue_1'):
        snap = self.store.snapshot(); state = snap['state']
        issue = next(i for i in state['issues'] if i['proposal']['issue_id'] == issue_id)
        receipt = issue['current_feedback_receipt_id']
        released = next((row for row in snap['handoffs'] if row['kind'] == 'release_feedback' and row['record']['receipt_id'] == receipt), None)
        release_id = 'release_' + uuid4().hex if released is None else released['record']['release_id']
        if released is None:
            self.store.release_feedback(self.command() | {'release_id': release_id, 'receipt_id': receipt,
                'target_ref': issue['target_ref'], 'allowed_roles': list(c.ROLES)})
        self.store.receive_feedback(self.command() | {'read_id': 'read_' + uuid4().hex, 'release_id': release_id,
            'recipient_role': role, 'recipient_actor_id': self.manifest['role_bindings'][role]['actor_id'], 'target_ref': issue['target_ref']})

    def planning_handoff(self):
        sources = self.store.propose()['planning_evidence_sources']
        for source in sources:
            rid = 'planning_release_' + uuid4().hex
            self.store.release_planning_evidence(self.command() | {'release_id': rid,
                **{k: source[k] for k in ('receipt_id', 'plan_ref', 'target_ref')}})
            self.store.receive_planning_evidence(self.command() | {'read_id': 'planning_read_' + uuid4().hex,
                'release_id': rid, 'recipient_role': 'planner',
                'recipient_actor_id': self.manifest['role_bindings']['planner']['actor_id'],
                'plan_ref': source['plan_ref'], 'target_ref': source['target_ref']})
        return sources

    def failed_gate_ready(self):
        self.initialize(); self.solve()
        critic = self.start('critique_candidate')
        self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': critic['proposal']['target_ref'],
            'findings': [], 'no_gap_identified': True})
        self.observe(self.start('alignment_review'), status='failed')

    def test_replan_frozen_diagnostic_source_context_and_reopen(self):
        self.failed_gate_ready()
        self.assertEqual(self.store.propose()['decision']['decision'], 'wait')
        sources = self.planning_handoff()
        self.assertEqual([s['status'] for s in sources], ['completed', 'failed'])
        started = self.start('replan'); ctx = started['packet']['context']
        self.assertEqual(ctx['planning_evidence_selection'], {'eligible_count': 2, 'selected_count': 2,
            'max_entries': 64, 'truncated': False, 'policy': 'all_current_plan_diagnostic_receipts_or_block'})
        self.assertEqual(len(started['packet']['binding']['planning_evidence_refs']), 2)
        self.assertEqual(ctx['planning_evidence'][0]['response']['no_gap_identified'], True)
        self.assertIsNone(ctx['planning_evidence'][1]['response'])
        self.assertEqual(ctx['dependency_closure'], []); self.assertIsNone(ctx['target_candidate'])
        self.assertEqual(ctx['released_feedback'], [])
        for entry, source in zip(ctx['planning_evidence'], sources):
            self.assertEqual(entry['source'], source['source']); self.assertFalse(entry['source']['authenticated'])
            self.assertEqual(c.candidate_ref(entry['target_candidate']), entry['target_ref'])
        before = self.store.snapshot()['state']['candidates']
        self.observe(started, plan(revision=2))
        self.assertEqual(self.store.snapshot()['state']['candidates'], before)
        self.assertEqual(self.store.propose()['planning_evidence_sources'], [])
        self.assertEqual(self.store.snapshot(), AdaptiveSession.open(self.root).snapshot())
        self.solve(); self.finish_gate('alignment_review'); self.finish_gate('whole_proof_review')
        self.assertEqual(self.store.propose()['decision']['decision'], 'complete')

    def test_multitarget_planning_preserves_null_failed_leaf_and_normal_evidence(self):
        self.manifest['limits']['global_call_limit'] = 8
        self.root = (Path(self.tmp.name) / 'victorique-development') / 'multitarget'
        self.store = AdaptiveSession.create(self.root, self.manifest)
        p = plan(branch=True); p['routes'][0]['obligations'].insert(2, node('third'))
        p['routes'][0]['obligations'][-1]['depends_on'].append('third')
        self.observe(self.start('propose_plan'), p); left = self.solve(oid='left')
        critic = self.start('critique_candidate', 'left')
        self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(left),
            'findings': [], 'no_gap_identified': True})
        failed = self.start('solve_obligation', 'right'); self.observe(failed, status='failed')
        sources = self.planning_handoff(); self.assertEqual(len(sources), 2)
        replan = self.start('replan'); evidence = replan['packet']['context']['planning_evidence']
        self.assertEqual(evidence[0]['target_candidate'], left)
        self.assertIsNone(evidence[1]['target_ref']); self.assertIsNone(evidence[1]['target_candidate'])
        self.assertIsNone(evidence[1]['response']); self.assertEqual(evidence[1]['status'], 'failed')
        self.assertEqual(self.store.status()['domain_actions'], 5)

    def test_planning_receive_requires_exact_actor_source_plan_target_and_release(self):
        self.failed_gate_ready(); source = self.store.propose()['planning_evidence_sources'][0]
        payload = {k: source[k] for k in ('receipt_id', 'plan_ref', 'target_ref')}
        self.store.release_planning_evidence(self.command() | payload | {'release_id': 'planning_release'})
        base = self.command() | {'read_id': 'planning_read', 'release_id': 'planning_release',
            'recipient_role': 'planner', 'recipient_actor_id': self.manifest['role_bindings']['planner']['actor_id'],
            'plan_ref': source['plan_ref'], 'target_ref': source['target_ref']}
        for key, value in (('recipient_actor_id', 'wrong_actor'), ('release_id', 'absent'), ('recipient_role', 'solver'),
                           ('plan_ref', dict(source['plan_ref'], revision=True)), ('target_ref', dict(source['target_ref'], revision=1.0))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.receive_planning_evidence(base | {key: value})
        self.assertEqual(self.store.propose()['decision']['decision'], 'wait')
        self.store.receive_planning_evidence(base)
        self.assertFalse(self.store.receive_planning_evidence(base)['recorded_new'])
        self.assertEqual(self.store.propose()['decision']['decision'], 'wait')  # second receipt still withheld

    def test_unknown_not_released_and_v1_session_version_refused(self):
        self.initialize(); started = self.start('solve_obligation'); self.observe(started, status='unknown')
        self.assertEqual(self.store.propose()['planning_evidence_sources'], [])
        self.assertEqual(self.store.propose()['decision']['reason'], 'recovery_required')
        bad = deepcopy(self.manifest); bad['implementation_versions']['session'] = 'victorique.adaptive_session.v1'
        with self.assertRaises(ValueError): AdaptiveSession.create((Path(self.tmp.name) / 'victorique-development') / 'old_version', bad)

    def test_replan_context_cannot_hide_or_replace_original_diagnostic_response(self):
        self.failed_gate_ready(); self.planning_handoff()
        proposal = next(p for p in self.store.propose()['legal_action_listing']['actions'] if p['kind'] == 'replan')
        import victorique.adaptive_session as session
        snap = self.store.snapshot()
        exact = session._context(snap, proposal, self.manifest)
        request = {'schema': exchange.REQUEST_SCHEMA, 'session_id': self.manifest['session_id'],
            'operation_id': 'forged_source', 'expected_event_sequence': snap['exchange']['sequence'],
            'request_id': 'forged_request', 'logical_action_id': 'forged_logical', 'action_id': 'forged_action',
            'prepared_state_revision': snap['state']['state_revision'], 'kind': 'replan', 'role': 'planner',
            'actor_id': self.manifest['role_bindings']['planner']['actor_id'], 'route_id': None, 'obligation_id': None,
            'dependency_refs': [], 'context': exact, 'template': session._template(snap['state'], proposal)}
        for change in ('omit', 'replace_source'):
            value = deepcopy(request)
            if change == 'omit':
                value['context']['planning_evidence'] = []
                value['context']['planning_evidence_selection'].update(eligible_count=0, selected_count=0)
            else:
                value['context']['planning_evidence'][0]['source']['source_event_id'] = 'invented_external_location'
            with self.subTest(change=change), self.assertRaises(ValueError):
                exchange.AdaptiveExchange.prepare(self.store, value)
        self.assertEqual(self.store.snapshot(), snap)

    def test_nonplanning_work_and_cold_gates_do_not_receive_planning_history(self):
        self.failed_gate_ready(); self.planning_handoff()
        started = self.start('replan'); self.observe(started, plan(revision=2))
        solve = self.start('solve_obligation')
        self.assertEqual(solve['packet']['context']['planning_evidence'], [])
        self.observe(solve, candidate(plan_revision=2))
        alignment = self.start('alignment_review')
        self.assertEqual(alignment['packet']['context']['planning_evidence'], [])
        self.assertEqual(alignment['packet']['binding']['planning_evidence_refs'], [])
        self.assertNotIn('bound_constraints', alignment['packet']['template']['instructions'])

    def test_durable_five_step_completion_and_reopen(self):
        base_replay = self.enterContext(mock.patch.object(self.store, '_replay', wraps=self.store._replay))
        domain_replay = self.enterContext(mock.patch.object(self.store, '_domain_replay', wraps=self.store._domain_replay))
        self.initialize(); self.solve()
        started = self.start('critique_candidate')
        self.observe(started, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': started['proposal']['target_ref'],
                              'findings': [], 'no_gap_identified': True})
        self.finish_gate('alignment_review'); self.finish_gate('whole_proof_review')
        choice = self.store.propose()['decision']
        self.assertEqual(choice['decision'], 'complete')
        self.store.finalize(self.command() | {'endpoint_id': 'endpoint_final', 'decision': choice['decision'], 'reason': choice['reason']})
        before = self.store.snapshot()
        reopened = AdaptiveSession.open(self.root)
        self.assertEqual(reopened.snapshot(), before)
        self.assertEqual(reopened.status()['host_bound_calls'], 5)
        self.assertTrue(reopened.status()['sealed'])
        self.assertFalse(reopened.status()['mathematical_truth_verified'])
        self.assertNotIn('content', json.dumps(reopened.status()))
        self.assertGreater(base_replay.call_count, 20)
        self.assertEqual(domain_replay.call_count, base_replay.call_count)

    def test_projection_requires_current_verified_transaction(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaisesRegex(ValueError, 'verified transaction'):
                self.store._verified_projection(db)
        with self.store._tx() as (db, base):
            self.assertEqual(self.store._verified_projection(db)['state']['session_id'], base['manifest']['session_id'])

    def test_prepare_rollback_discards_projection_and_reopens_unchanged(self):
        view = self.store.propose()
        command = self.command() | {'request_id': 'rollbackrequest', 'logical_action_id': 'rollbacklogical',
                                   'action_id': 'rollbackaction', 'selected_proposal': view['legal_action_listing']['actions'][0]}
        before = self.store.snapshot()
        def checkpoint(stage, identity):
            if stage == 'before_commit': raise RuntimeError('INERT transaction interrupted')
        with mock.patch.object(self.store, '_checkpoint', side_effect=checkpoint):
            with self.assertRaises(RuntimeError): self.store.prepare(command)
        self.assertEqual(self.store.snapshot(), before)
        self.store = AdaptiveSession.open(self.root)
        self.assertEqual(self.store.snapshot(), before)
        self.assertTrue(self.store.prepare(command)['prepared_new'])
        self.assertFalse(self.store.prepare(command)['prepared_new'])
        self.assertEqual(self.store.status()['domain_actions'], 1)

    def test_parallel_leaf_returns_use_binding_not_global_state_cas(self):
        self.initialize(True)
        left = self.start('solve_obligation', 'left'); right = self.start('solve_obligation', 'right')
        self.observe(left, candidate(oid='left'))
        self.observe(right, candidate(oid='right'))
        self.assertEqual(self.store.status()['candidate_count'], 2)
        AdaptiveSession.open(self.root)

    def test_malformed_raw_and_charge_survive_domain_failure(self):
        started = self.start('propose_plan')
        record, result = self.observe(started, raw=b'{broken', units=2)
        self.assertEqual(result['classification'], 'returned_invalid')
        snap = self.store.snapshot()
        self.assertEqual(snap['state']['actions'][0]['status'], 'failed')
        self.assertEqual(snap['exchange']['requests'][started['request_id']]['known_cost_units'], 2)
        self.assertEqual(self.store.propose()['legal_action_listing']['actions'], [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT raw FROM observations WHERE observation_id=?', (record['observation_id'],)).fetchone()[0], b'{broken')
        AdaptiveSession.open(self.root)

    def test_cache_or_derived_mapping_mutation_refuses_reopen(self):
        self.initialize()
        with closing(sqlite3.connect(self.store.path)) as db:
            cached = json.loads(db.execute('SELECT payload FROM session_cache').fetchone()[0])
            cached['state']['plan']['routes'][0]['obligations'][0]['statement'] = 'INERT changed cache'
            db.execute('UPDATE session_cache SET payload=?', (json.dumps(cached),))
            db.commit()
        with self.assertRaises(ValueError): self.store.status()
        with self.assertRaises(ValueError): AdaptiveSession.open(self.root)

    def test_repeat_prepare_does_not_reserve_another_call(self):
        started = self.start('propose_plan')
        result = self.store.prepare(started['command'])
        self.assertFalse(result['prepared_new'])
        self.assertEqual(self.store.status()['domain_actions'], 1)
        changed = deepcopy(started['command']); changed['selected_proposal']['cap_units'] = True
        with self.assertRaises(ValueError): self.store.prepare(changed)

    def test_unknown_is_audited_then_original_return_without_new_claim(self):
        started = self.start('propose_plan')
        self.observe(started, status='unknown')
        self.assertEqual(self.store.propose()['legal_action_listing']['actions'], [])
        self.observe(started, plan())
        self.assertEqual(self.store.status()['domain_actions'], 1)
        self.assertIsNotNone(self.store.snapshot()['state']['plan'])
        AdaptiveSession.open(self.root)

    def test_repair_feedback_recheck_and_old_dependency_freeze(self):
        self.initialize(True); left = self.solve(oid='left'); self.solve(oid='right')
        self.solve('integrate', 'root')
        critique = self.start('critique_candidate', 'left')
        self.observe(critique, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(left),
                              'findings': [defect(left)], 'no_gap_identified': False})
        self.assertTrue(any(r['reason'] == 'feedback_not_released_and_received' for r in self.store.propose()['legal_action_listing']['rejected']))
        self.handoff('investigator'); investigate = self.start('investigate', 'left')
        self.assertEqual(len(investigate['packet']['context']['released_feedback']), 1)
        self.observe(investigate, disposition(left, 'confirmed'))
        self.handoff('solver'); repaired = self.solve('repair_obligation', 'left')
        self.handoff('investigator'); recheck = self.start('investigate', 'left')
        selected = recheck['packet']['context']['selected_issue']
        self.assertEqual(selected['proposal']['target_ref']['revision'], 1)
        self.assertEqual(selected['target_ref']['revision'], 2)
        self.observe(recheck, disposition(repaired, 'refuted'))
        merged = self.solve('integrate', 'root')
        self.assertEqual(merged['revision'], 2)
        aligned = self.finish_gate('alignment_review'); self.finish_gate('whole_proof_review')
        self.assertIsNone(aligned['packet']['context']['plan'])
        self.assertEqual(aligned['packet']['context']['released_feedback'], [])
        self.assertNotIn('bound_constraints', json.loads(aligned['packet']['template']['instructions']))
        self.assertEqual(self.store.propose()['decision']['decision'], 'complete')
        AdaptiveSession.open(self.root)

    def test_stale_integrate_return_is_archived_and_releases_inflight(self):
        self.initialize(True); left = self.solve(oid='left'); self.solve(oid='right')
        old = self.start('integrate', 'root')
        critique = self.start('critique_candidate', 'left')
        self.observe(critique, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(left),
                              'findings': [defect(left)], 'no_gap_identified': False})
        self.handoff('investigator'); investigate = self.start('investigate', 'left')
        self.observe(investigate, disposition(left, 'confirmed'))
        self.handoff('solver'); self.solve('repair_obligation', 'left')
        record, result = self.observe(old, candidate(oid='root', dependencies=old['proposal']['dependency_refs']), units=1)
        self.assertEqual(result['projection_status'], 'stale_audit_only')
        action = next(a for a in self.store.snapshot()['state']['actions'] if a['action_id'] == old['command']['action_id'])
        self.assertEqual(action['status'], 'completed')
        self.assertEqual(action['cost_units'], 1)
        self.assertFalse(any(a['artifact']['obligation_id'] == 'root' for a in self.store.snapshot()['state']['candidates']))
        AdaptiveSession.open(self.root)

    def test_source_domain_derivation_cannot_be_changed_with_cache(self):
        self.initialize()
        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute('SELECT sequence,payload FROM session_journal ORDER BY sequence DESC LIMIT 1').fetchone()
            value = json.loads(row[1]); value['derived']['classification'] = 'invented_projection'
            db.execute('DROP TRIGGER immutable_session_journal_update')
            db.execute('UPDATE session_journal SET payload=? WHERE sequence=?', (json.dumps(value), row[0])); db.commit()
        with self.assertRaises(ValueError): self.store.propose()
        with self.assertRaises(ValueError): AdaptiveSession.open(self.root)

    def rewrite_journal(self, sequence, change):
        """Change one domain journal entry outside any object; its triggers are restored exactly."""
        with closing(sqlite3.connect(self.store.path)) as db, db:
            value = json.loads(db.execute('SELECT payload FROM session_journal WHERE sequence=?', (sequence,)).fetchone()[0])
            change(value)
            triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='session_journal'").fetchall()
            for name, _ in triggers: db.execute('DROP TRIGGER "' + name + '"')
            db.execute('UPDATE session_journal SET payload=? WHERE sequence=?', (json.dumps(value), sequence))
            for _, sql in triggers: db.execute(sql)

    def test_same_object_detects_domain_rows_changed_between_its_transactions(self):
        def invent(value): value['derived']['classification'] = 'invented_projection'
        for name in ('last_entry', 'journal_gap', 'entry_before_first', 'cache', 'endpoint'):
            with self.subTest(change=name):
                self.root = (Path(self.tmp.name) / 'victorique-development') / ('changed-' + name)
                self.store = AdaptiveSession.create(self.root, self.manifest)
                self.initialize(); self.store.status()
                if name == 'last_entry':  # schema unchanged; the last verified entry differs
                    self.rewrite_journal(self.store.status()['session_sequence'], invent)
                else:
                    with closing(sqlite3.connect(self.store.path)) as db, db:
                        if name in ('journal_gap', 'entry_before_first'):
                            seq, event_id, operation_id, raw = db.execute('SELECT * FROM session_journal ORDER BY sequence DESC LIMIT 1').fetchone()
                            db.execute('INSERT INTO session_journal VALUES (?,?,?,?)', (seq + 2 if name == 'journal_gap' else 0,
                                       event_id + '_outside', operation_id + '_outside', raw))
                        elif name == 'endpoint':
                            db.execute('INSERT INTO session_endpoint VALUES (1,?)', (json.dumps({'decision': 'complete'}),))
                        else:
                            cached = json.loads(db.execute('SELECT payload FROM session_cache').fetchone()[0])
                            cached['state']['state_revision'] += 1
                            db.execute('UPDATE session_cache SET payload=?', (json.dumps(cached),))
                with self.assertRaises(ValueError): self.store.status()
                with self.assertRaises(ValueError): self.store.propose()
                with self.assertRaises(ValueError): AdaptiveSession.open(self.root)

    def test_domain_entries_appended_by_another_object_are_applied_without_full_replay(self):
        self.initialize()
        other = AdaptiveSession.open(self.root)
        first = self.store
        with mock.patch.object(first, '_replay_full', wraps=first._replay_full) as full:
            self.store = other  # the helpers below now drive the second object
            root = self.solve()
            critic = self.start('critique_candidate')
            self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(root),
                                  'findings': [defect(root)], 'no_gap_identified': False})
            self.handoff('investigator')
            self.store = first
            self.assertEqual(first.snapshot(), other.snapshot())
            self.assertEqual(first.status(), other.status())
            self.assertEqual(full.call_count, 0)
        self.assertEqual(first.snapshot(), AdaptiveSession.open(self.root).snapshot())
        investigate = self.start('investigate', 'root')
        self.assertEqual(len(investigate['packet']['context']['released_feedback']), 1)
        self.assertEqual(other.status()['domain_actions'], 4)

    def test_only_the_first_transaction_of_a_session_replays_every_event(self):
        full = self.enterContext(mock.patch.object(self.store, '_replay_full', wraps=self.store._replay_full))
        self.initialize(); root = self.solve()
        stale = self.command() | {'request_id': 'stale', 'logical_action_id': 'stale', 'action_id': 'stale',
                                  'selected_proposal': self.store.propose()['decision']['action']}
        stale['expected_session_sequence'] -= 1
        with self.assertRaises(ValueError): self.store.prepare(stale)
        critic = self.start('critique_candidate')
        self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(root),
                              'findings': [defect(root)], 'no_gap_identified': False})
        self.handoff('investigator')
        self.assertEqual(self.store.snapshot(), AdaptiveSession.open(self.root).snapshot())
        self.assertEqual(full.call_count, 1)

    def test_open_after_a_domain_change_runs_the_full_replay(self):
        self.initialize(); self.solve(); self.store.status()
        # An earlier entry changes; the schema, row counts and last entry stay as they were.
        self.rewrite_journal(2, lambda value: value['derived'].update(classification='invented_projection'))
        with mock.patch.object(AdaptiveSession, '_replay_full', autospec=True, side_effect=AdaptiveSession._replay_full) as full:
            with self.assertRaisesRegex(ValueError, 'domain mapping differs'): AdaptiveSession.open(self.root)
        self.assertEqual(full.call_count, 1)

    def test_domain_projection_rejection_keeps_original_raw_and_fee(self):
        # A semantically valid plan with a test-only reducer failure exercises
        # intake atomicity, not a new mathematical or plan-validation rule.
        started = self.start('propose_plan')
        real_reduce = controller.reduce
        def fail_return(state, event):
            if event['kind'] == 'return_action': raise c.AdaptiveContractError('JSON exceeds byte limit')
            return real_reduce(state, event)
        with mock.patch('victorique.adaptive_session.controller.reduce', side_effect=fail_return):
            record, result = self.observe(started, plan(), units=1)
            self.assertEqual(result['projection_status'], 'projection_resource_exhausted')
            self.assertTrue(self.store.status()['sealed'])
            with closing(sqlite3.connect(self.store.path)) as db:
                self.assertIsNotNone(db.execute('SELECT raw FROM observations WHERE observation_id=?', (record['observation_id'],)).fetchone()[0])
            self.assertEqual(self.store.status()['budget']['actual_observed_units'], 1)
        # The patched reducer is not the frozen source, so reopening without
        # the same injected fault must refuse this synthetic cache/mapping.
        with self.assertRaises(ValueError): AdaptiveSession.open(self.root)

    def test_late_return_after_local_stop_never_changes_endpoint(self):
        started = self.start('propose_plan')
        self.store.close(self.base('close') | {'reason': 'synthetic explicit local stop'})
        endpoint = self.store.snapshot()['endpoint']
        _, result = self.observe(started, plan(), units=1)
        self.assertEqual(result['projection_status'], 'late_audit_only')
        self.assertEqual(self.store.snapshot()['endpoint'], endpoint)
        self.assertIsNone(self.store.snapshot()['state']['plan'])
        self.assertEqual(self.store.status()['budget']['actual_observed_units'], 1)
        AdaptiveSession.open(self.root)

    def test_new_known_issues_block_prepared_claim_completion_budget(self):
        self.manifest['limits']['global_call_limit'] = 10
        self.root = (Path(self.tmp.name) / 'victorique-development') / 'limited'
        self.store = AdaptiveSession.create(self.root, self.manifest)
        self.initialize(True); left = self.solve(oid='left'); self.solve(oid='right')
        proposal = next(p for p in self.store.propose()['legal_action_listing']['actions'] if p['kind'] == 'integrate')
        rid = 'prepared_integrate'
        prepared = self.store.prepare(self.command() | {'request_id': rid, 'logical_action_id': 'logical_integrate',
             'action_id': 'act_integrate', 'selected_proposal': proposal})
        self.store.export_packet(self.base('export') | {'request_id': rid})
        critic = self.start('critique_candidate', 'left')
        self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(left),
            'findings': [defect(left, 'issue_' + str(i)) for i in range(4)], 'no_gap_identified': False})
        with self.assertRaisesRegex(ValueError, 'completion call reserve'):
            self.store.claim(self.base('claim') | {'request_id': rid, 'packet_id': prepared['packet_id']})
        self.assertIsNone(self.store.snapshot()['exchange']['requests'][rid]['claim_id'])

    def test_unreleased_read_and_pre_handoff_prepare_are_rejected(self):
        self.initialize(); root = self.solve()
        critic = self.start('critique_candidate')
        self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(root),
            'findings': [defect(root)], 'no_gap_identified': False})
        with self.assertRaises(ValueError):
            self.store.receive_feedback(self.command() | {'read_id': 'badread', 'release_id': 'missing',
                'recipient_role': 'investigator', 'recipient_actor_id': 'actor_investigator', 'target_ref': c.candidate_ref(root)})
        old_revision = self.store.status()['state_revision']
        self.handoff('investigator')
        proposal = next(p for p in self.store.propose()['legal_action_listing']['actions'] if p['kind'] == 'investigate')
        command = self.command() | {'request_id': 'stale_prepare', 'logical_action_id': 'stale_logical', 'action_id': 'stale_action', 'selected_proposal': proposal}
        command['expected_state_revision'] = old_revision
        with self.assertRaises(ValueError): self.store.prepare(command)

    def test_base_prepare_cannot_bypass_controller_or_template_binding(self):
        from victorique.adaptive_exchange import AdaptiveExchange
        started = self.start('propose_plan')
        request = deepcopy(self.store.snapshot()['exchange']['requests'][started['request_id']]['record'])
        request.update(operation_id='bypassop', request_id='bypassreq', logical_action_id='bypasslogical', action_id='bypassaction',
                       expected_event_sequence=self.store.status()['event_sequence'])
        request['template']['instructions'] = 'INERT unauthorized replacement template'
        with self.assertRaises(ValueError): AdaptiveExchange.prepare(self.store, request)
        self.assertEqual(self.store.status()['domain_actions'], 1)

    def test_prepare_commit_crash_is_recoverable_without_new_intent(self):
        view = self.store.propose()
        command = self.command() | {'request_id': 'retryrequest', 'logical_action_id': 'retrylogical',
                                   'action_id': 'retryaction', 'selected_proposal': view['legal_action_listing']['actions'][0]}
        def checkpoint(stage, identity):
            if stage == 'prepare_committed': raise RuntimeError('INERT return lost')
        with mock.patch.object(self.store, '_checkpoint', side_effect=checkpoint):
            with self.assertRaises(RuntimeError): self.store.prepare(command)
        self.store = AdaptiveSession.open(self.root)
        self.assertFalse(self.store.prepare(command)['prepared_new'])
        self.assertEqual(self.store.status()['domain_actions'], 1)

    def test_raw_observation_tamper_cannot_be_hidden_by_domain_cache(self):
        self.initialize()
        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute('SELECT observation_id,raw FROM observations').fetchone()
            raw = json.loads(row[1]); raw['output']['routes'][0]['obligations'][0]['statement'] = 'INERT forged source'
            db.execute('DROP TRIGGER immutable_observations_update')
            db.execute('UPDATE observations SET raw=? WHERE observation_id=?', (json.dumps(raw).encode(), row[0])); db.commit()
        with self.assertRaises(ValueError): self.store.snapshot()
        with self.assertRaises(ValueError): AdaptiveSession.open(self.root)

    def test_completed_route_endpoint_uses_reviewed_root_not_last_active_route(self):
        self.observe(self.start('propose_plan'), plan(routes=2))
        self.solve(route='route0')
        self.finish_gate('alignment_review'); self.finish_gate('whole_proof_review')
        self.solve(route='route1')
        choice = self.store.propose()['decision']
        self.assertEqual(choice['decision'], 'complete')
        self.store.finalize(self.command() | {'endpoint_id': 'route_endpoint', 'decision': choice['decision'], 'reason': choice['reason']})
        endpoint = self.store.snapshot()['endpoint']
        self.assertEqual(endpoint['selected_route_id'], 'route0')
        self.assertEqual(endpoint['root_candidate']['route_id'], 'route0')
        self.assertEqual(len(endpoint['gates']), 2)
        AdaptiveSession.open(self.root)

    def test_prepared_gate_rechecks_current_issues_before_claim(self):
        self.manifest['limits']['global_call_limit'] = 8
        self.root = (Path(self.tmp.name) / 'victorique-development') / 'gate_limit'
        self.store = AdaptiveSession.create(self.root, self.manifest)
        self.initialize(); root = self.solve()
        proposal = next(p for p in self.store.propose()['legal_action_listing']['actions'] if p['kind'] == 'alignment_review')
        rid = 'prepared_gate'
        prepared = self.store.prepare(self.command() | {'request_id': rid, 'logical_action_id': 'gate_logical',
            'action_id': 'gate_action', 'selected_proposal': proposal})
        self.store.export_packet(self.base('export') | {'request_id': rid})
        critic = self.start('critique_candidate')
        self.observe(critic, {'schema': 'victorique.adaptive_critique.v1', 'target_ref': c.candidate_ref(root),
            'findings': [defect(root, 'issue_' + str(i)) for i in range(4)], 'no_gap_identified': False})
        with self.assertRaisesRegex(ValueError, 'current unresolved issues'):
            self.store.claim(self.base('claim') | {'request_id': rid, 'packet_id': prepared['packet_id']})
        self.assertIsNone(self.store.snapshot()['exchange']['requests'][rid]['claim_id'])
        self.assertEqual(self.store.status()['domain_actions'], 4)

    def test_capture_failure_extension_keeps_bill_without_inventing_raw(self):
        started = self.start('propose_plan')
        record = self.base('observation', True) | {'observation_id': 'capture_failure_observation',
            'request_id': started['request_id'], 'claim_id': started['claim_id'], 'host_reference': started['host_reference'],
            'transport_status': 'returned', 'source_event_id': None, 'source_line': None, 'external_observed_at_utc': None,
            'model_observation': None, 'charge_observation': {'charge_id': 'capture_bill', 'unit_id': 'inert_unit',
                'cumulative_units': 1, 'final_bill': False, 'source': 'INERT declared bill', 'usage': None}, 'reason': None}
        result = self.store.record_capture_failure(record, None, 'unreadable')
        self.assertEqual(result['classification'], 'capture_failed')
        self.assertFalse(result['raw_response_archived'])
        self.assertEqual(self.store.snapshot()['endpoint']['reason'], 'capture_failed')
        self.assertEqual(self.store.status()['budget']['actual_observed_units'], 1)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertIsNone(db.execute('SELECT raw FROM observations WHERE observation_id=?', ('capture_failure_observation',)).fetchone()[0])
        AdaptiveSession.open(self.root)


if __name__ == "__main__":
    unittest.main()
