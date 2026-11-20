"""INERT compatibility checks between role blueprints and the native contracts."""
from copy import deepcopy
import unittest

from victorique import adaptive_contracts as c
from victorique.adaptive_roles import output_contract, template
from tests.test_victorique_adaptive_contracts import PROBLEM, candidate, disposition, limits, plan
from tests.test_victorique_adaptive_controller import gate, planned, produce, reserve, reply


class AdaptiveRoleTests(unittest.TestCase):
    def test_only_executable_native_actions_have_templates(self):
        for kind in c.CALLS:
            t = template(kind)
            self.assertEqual(t['kind'], kind)
            self.assertEqual(t['revision'], 2)
            self.assertTrue(t['common_instructions'])
            self.assertTrue(output_contract(kind)['schema'].startswith('victorique.adaptive_'))
        for kind in (None, True, [], 'wait', 'stop', 'accept_fact'):
            with self.subTest(kind=kind), self.assertRaises(ValueError): template(kind)
            with self.subTest(kind=kind), self.assertRaises(ValueError): output_contract(kind)

    def test_detached_templates_and_nested_contracts_do_not_mutate_other_roles(self):
        original = output_contract('alignment_review')
        edited = output_contract('alignment_review')
        edited['findings'][0]['target_ref']['revision'] = True
        self.assertEqual(output_contract('alignment_review'), original)
        t = template('propose_plan'); t['limitations'].append('inert change')
        self.assertNotIn('inert change', template('propose_plan')['limitations'])

    def test_plan_blueprint_and_validated_plan_have_same_outer_and_node_fields(self):
        p = c.validate_plan(plan(branch=True), PROBLEM, limits())
        contract = output_contract('propose_plan')
        self.assertEqual(set(contract), set(p))
        self.assertEqual(set(contract['routes'][0]), set(p['routes'][0]))
        self.assertEqual(set(contract['routes'][0]['obligations'][0]), set(p['routes'][0]['obligations'][0]))
        revised = c.validate_plan(plan(revision=2), PROBLEM, limits(), prior=plan())
        self.assertEqual(set(output_contract('replan')), set(revised))

    def test_candidate_blueprint_matches_all_mutating_response_shapes(self):
        for kind in ('solve_obligation', 'repair_obligation', 'integrate'):
            value = c.validate_candidate(candidate(), plan(), limits())
            # Strict provider schemas make the repair judgments a required field.
            expected = set(value) | ({'assessments'} if kind == 'repair_obligation' else set())
            with self.subTest(kind=kind):
                self.assertEqual(set(output_contract(kind)), expected)
        self.assertEqual(output_contract('solve_obligation')['addressed_issue_ids'], [])
        self.assertEqual(output_contract('integrate')['addressed_issue_ids'], [])
        self.assertEqual(len(output_contract('repair_obligation')['addressed_issue_ids']), 1)

    def test_investigation_blueprint_matches_disposition(self):
        target = candidate()
        value = c.validate_disposition(disposition(target), 'issue_1', target)
        self.assertEqual(set(output_contract('investigate')), set(value))
        self.assertEqual(set(output_contract('investigate')['location']['shape']), set(value['location']))

    def test_gate_blueprints_match_actual_bound_reducer_responses(self):
        for kind in c.GATES:
            state = produce(planned())
            if kind == 'whole_proof_review':
                state = gate(state, 'alignment_review')
            state = reserve(state, kind)
            value = {'schema': 'victorique.adaptive_gate.v1', 'gate_id': kind,
                     'problem_ref': {k: PROBLEM[k] for k in ('artifact_id', 'revision')},
                     'target_ref': deepcopy(state['actions'][-1]['proposal']['target_ref']),
                     'coverage': 'whole_problem_and_proof', 'outcome': 'pass', 'findings': [], 'uncertainties': []}
            self.assertEqual(set(output_contract(kind)), set(value))
            self.assertEqual(reply(state, value)['actions'][-1]['status'], 'completed')

    def test_critique_blueprint_does_not_introduce_a_gate_verdict(self):
        state = reserve(produce(planned()), 'critique_candidate')
        value = {'schema': 'victorique.adaptive_critique.v1',
                 'target_ref': deepcopy(state['actions'][-1]['proposal']['target_ref']),
                 'findings': [], 'no_gap_identified': True}
        self.assertEqual(set(output_contract('critique_candidate')), set(value))
        result = reply(state, value)
        self.assertEqual(result['actions'][-1]['status'], 'completed')
        self.assertNotIn('outcome', output_contract('critique_candidate'))

    def test_role_instructions_disclose_byte_offsets_and_unverified_status(self):
        for kind in c.CALLS:
            t = template(kind)
            self.assertIn('UTF-8 BYTE', t['common_instructions'])
            self.assertIn('not', ' '.join(t['limitations']))
        for kind in c.GATES:
            instructions = template(kind)['instructions']
            self.assertIn('prior gate verdicts', instructions)
            self.assertIn('experimental arm', instructions)
            self.assertTrue('whole' in instructions or 'entire' in instructions)


if __name__ == '__main__':
    unittest.main()
