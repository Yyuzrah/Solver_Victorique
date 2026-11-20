"""Explicit create-time external proof input profile; no I/O or model calls.

The v2 adaptive profile is unchanged. These declarations do not authenticate an
author, provide a model receipt, establish correctness, or authorize dispatch.
"""
from . import adaptive_contracts as c


PROFILE = 'seeded_repair_v1'
MANIFEST_SCHEMA = 'victorique.adaptive_session_manifest.v3'
STATE_SCHEMA = 'victorique.adaptive_state.v3'
VERSION = 'victorique.adaptive_exchange.v3'
SESSION_VERSION = 'victorique.adaptive_session.v3'
CONTROLLER_VERSION = 'bounded_rules_v3'
CONTRACTS_VERSION = 'adaptive_contracts.v2'
INPUT_SCHEMA = 'victorique.external_proof_input.v1'
ENDPOINT_SCHEMA = 'victorique.adaptive_endpoint.v2'


def versions():
    return {'contracts': CONTRACTS_VERSION, 'controller': CONTROLLER_VERSION,
            'exchange': VERSION, 'session': SESSION_VERSION, 'templates': 'adaptive_roles.v2'}


def input_ref(value):
    return {key: value[key] for key in ('artifact_id', 'revision')}


def validate_input(value, problem):
    problem = c.validate_problem(problem)
    value = c.copy_json(value)
    c.fields(value, {'schema', 'artifact_id', 'revision', 'problem_ref', 'content',
                     'encoding', 'byte_length', 'trust', 'source'}, 'external proof input')
    c.need(value['schema'] == INPUT_SCHEMA and value['encoding'] == 'utf-8'
           and value['trust'] == 'untrusted_input', 'unsupported external input')
    c.problem_ref(input_ref(value)); c.problem_ref(value['problem_ref'])
    c.need(c.same_json(value['problem_ref'], input_ref(problem)), 'seed problem differs')
    c.text(value['content'], 'external proof content')
    c.integer(value['byte_length'], 'external proof byte length', 1, 256000)
    c.need(value['byte_length'] == len(value['content'].encode('utf-8')), 'seed byte length differs')
    source = value['source']
    c.fields(source, {'kind', 'channel', 'source_id', 'declared_origin', 'received_at_utc',
                      'authenticated', 'upstream_byte_identity_verified'}, 'external proof source')
    c.need(source['kind'] == 'user_provided' and source['channel'] in ('explicit_text', 'explicit_utf8_file')
           and source['declared_origin'] in ('user_authored', 'third_party', 'model_generated', 'unknown')
           and source['authenticated'] is False and source['upstream_byte_identity_verified'] is False,
           'unsupported or promoted external source')
    if source['source_id'] is not None:
        c.text(source['source_id'], 'declared source locator', 4000)
    if source['received_at_utc'] is not None:
        c.utc(source['received_at_utc'])
    return value


def validate_limits(value):
    value = c.validate_limits(value)
    for key in ('max_routes', 'max_obligations_per_plan', 'max_plan_revisions', 'max_critiques_per_candidate'):
        c.need(value[key] == 1, 'seed profile requires ' + key + '=1')
    c.need(value['max_candidate_revisions'] >= 2 and value['max_repairs_per_issue'] >= 1
           and value['max_investigations_per_issue'] >= 2, 'seed profile lacks repair/recheck capacity')
    return value


def bootstrap(problem, limits, initial_input):
    """Deterministic structural wrapper; adds no mathematical reasoning."""
    initial_input = validate_input(initial_input, problem); limits = validate_limits(limits)
    plan = c.validate_plan({'schema': 'victorique.adaptive_plan.v1', 'plan_id': 'seed_plan',
        'revision': 1, 'parent_revision': None, 'problem_ref': input_ref(problem),
        'routes': [{'route_id': 'main', 'root_obligation_id': 'whole_problem', 'obligations': [
            {'obligation_id': 'whole_problem', 'statement': problem['content'], 'assumptions': [],
             'depends_on': [], 'claimed_goal_coverage': ['entire frozen problem'], 'failure_conditions': []}]}]},
        problem, limits)
    candidate = c.validate_candidate({'schema': 'victorique.adaptive_candidate.v1',
        'artifact_id': 'seed_candidate', 'revision': 1, 'parent_revision': None,
        'plan_id': plan['plan_id'], 'plan_revision': 1, 'route_id': 'main',
        'obligation_id': 'whole_problem', 'content': initial_input['content'], 'assumptions': [],
        'dependency_refs': [], 'addressed_issue_ids': []}, plan, limits)
    return plan, candidate


def is_state(value):
    return type(value) is dict and value.get('schema') == STATE_SCHEMA


def critique_ready(state):
    """A real projected initial critique, not a reservation or author claim."""
    if not is_state(state):
        return True
    _, candidate = bootstrap(state['problem'], state['limits'], state['initial_input'])
    reference = c.candidate_ref(candidate)
    return any(row['status'] == 'completed' and row['projection'] == 'declared_receipt_projected'
               and row['proposal']['kind'] == 'critique_candidate'
               and c.same_json(row['proposal']['target_ref'], reference)
               for row in state['actions'])
