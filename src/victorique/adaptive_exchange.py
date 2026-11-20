"""A bounded, replay-checked exchange, without adaptive projection or dispatch.

The coordinator supplies a frozen typed action/context. This layer checks that
declaration and records one dispatch responsibility; it does not establish that
the controller selected it, that feedback was released, or that mathematics is
correct. No transport, credentials, content hash or legacy workflow is used.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from . import adaptive_contracts as c
from . import seeded_repair as seed
from .harness.safety import _check_leaf, _check_path


VERSION = 'victorique.adaptive_exchange.v2'
MANIFEST_SCHEMA = 'victorique.adaptive_session_manifest.v2'
REQUEST_SCHEMA = 'victorique.adaptive_request.v2'
PACKET_SCHEMA = 'victorique.adaptive_actor_packet.v2'
RESPONSE_SCHEMA = 'victorique.adaptive_actor_response.v2'
PLANNING_SELECTION_POLICY = 'all_current_plan_diagnostic_receipts_or_block'
TERMINAL = {'returned_valid', 'returned_invalid', 'returned_invalid_model', 'failed'}
# The observation reason of a call whose own process stopped at its per-call time limit.
LOCAL_TIMEOUT = 'local_timeout'
COMMON = {'schema', 'session_id', 'operation_id', 'expected_event_sequence'}
PREPARE_FIELDS = COMMON | {'request_id', 'logical_action_id', 'action_id', 'prepared_state_revision',
    'kind', 'role', 'actor_id', 'route_id', 'obligation_id', 'context', 'dependency_refs', 'template'}
OBS_FIELDS = COMMON | {'observation_id', 'request_id', 'claim_id', 'host_reference',
    'transport_status', 'source_event_id', 'source_line', 'external_observed_at_utc',
    'model_observation', 'charge_observation', 'reason'}
TABLES = {'metadata', 'events', 'requests', 'packets', 'observations', 'charges', 'endpoint'}


class AdaptiveExchangeError(ValueError):
    pass


def _need(condition, message):
    if not condition:
        raise AdaptiveExchangeError(message)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    def invalid(_):
        raise AdaptiveExchangeError('nonfinite JSON')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AdaptiveExchangeError('invalid JSON') from exc


def _equal(left, right):
    return _dump(left) == _dump(right)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _host(value, nullable=False):
    if value is None and nullable:
        return
    c.text(value, 'canonical host reference', 2000)
    _need(value.startswith('/') and not any(ch.isspace() for ch in value), 'canonical host reference required')


def _root(root):
    _need('..' not in Path(root).parts, 'parent traversal refused')
    path = Path(os.path.abspath(os.fspath(root)))
    parts = [part.lower() for part in path.parts]
    _need('.git' not in parts, 'git metadata directory refused')
    _check_path(path)
    return path


def validate_manifest(value):
    value = c.copy_json(value)
    seeded = type(value) is dict and value.get('schema') == seed.MANIFEST_SCHEMA
    c.fields(value, {'schema', 'session_id', 'protocol', 'mode', 'problem', 'limits', 'role_bindings',
        'implementation_versions', 'storage_limits', 'budget_unit', 'authorization_ref', 'forbidden_host_references'}
        | ({'workflow_profile', 'initial_input'} if seeded else set()), 'adaptive exchange manifest')
    _need(value['schema'] in (MANIFEST_SCHEMA, seed.MANIFEST_SCHEMA) and value['mode'] in ('synthetic', 'host_recorded'), 'unsupported adaptive manifest')
    c.identifier(value['session_id']); c.validate_problem(value['problem']); c.validate_limits(value['limits'])
    c.fields(value['protocol'], {'protocol_id', 'version'}, 'protocol')
    for item in value['protocol'].values(): c.identifier(item)
    c.fields(value['role_bindings'], c.ROLES, 'role bindings')
    actors = []
    for role, actor in value['role_bindings'].items():
        c.fields(actor, {'actor_id', 'model_request', 'fresh_context'}, 'actor binding')
        c.identifier(actor['actor_id']); actors.append(actor['actor_id'])
        _need(actor['fresh_context'] is True, 'fresh context required')
        c.fields(actor['model_request'], {'provider_id', 'model_id', 'revision'}, 'model request')
        for key, item in actor['model_request'].items():
            if key == 'revision' and item is None: continue
            c.text(item, 'requested model identity', 2000)
    _need(len(set(actors)) == len(actors), 'actor identity reused across roles')
    c.fields(value['implementation_versions'], {'contracts', 'controller', 'exchange', 'session', 'templates'}, 'implementation versions')
    for item in value['implementation_versions'].values():
        if item is not None: c.text(item, 'implementation version', 2000)
    _need(value['implementation_versions']['exchange'] == (seed.VERSION if seeded else VERSION), 'exchange version differs')
    storage = value['storage_limits']
    c.fields(storage, {'max_packet_bytes', 'max_decoded_response_bytes', 'max_raw_response_bytes', 'max_session_archive_bytes'}, 'storage limits')
    for cap in storage.values(): c.integer(cap, 'storage cap', 1, 1024 * 1024 * 1024)
    _need(storage['max_packet_bytes'] <= 4_000_000 and storage['max_decoded_response_bytes'] <= 4_000_000,
          'packet/decode cap exceeds pure-contract boundary')
    _need(storage['max_decoded_response_bytes'] <= storage['max_raw_response_bytes'] <= storage['max_session_archive_bytes'], 'storage caps inconsistent')
    c.fields(value['budget_unit'], {'unit_id', 'meaning', 'currency'}, 'budget unit')
    c.identifier(value['budget_unit']['unit_id']); c.text(value['budget_unit']['meaning'], 'budget unit meaning', 2000)
    if value['budget_unit']['currency'] is not None: c.identifier(value['budget_unit']['currency'])
    c.fields(value['authorization_ref'], {'scope', 'source_id'}, 'authorization declaration')
    for item in value['authorization_ref'].values(): c.text(item, 'authorization declaration', 4000)
    _need(type(value['forbidden_host_references']) is list, 'forbidden host list required')
    for item in value['forbidden_host_references']: _host(item)
    _need(len(set(value['forbidden_host_references'])) == len(value['forbidden_host_references']), 'duplicate forbidden host')
    if seeded:
        _need(value['workflow_profile'] == seed.PROFILE, 'unsupported seeded workflow')
        _need(c.same_json(value['implementation_versions'], seed.versions()), 'seeded implementation versions differ')
        seed.bootstrap(value['problem'], value['limits'], value['initial_input'])
        _need(value['initial_input']['byte_length'] <= storage['max_session_archive_bytes'], 'seed archive exceeds limit')
    return value


def _plan(value, manifest):
    # This layer validates the declared parent's shape; existence/semantic currency
    # of that parent belongs to the session replay, never certified by this local adapter.
    _need(type(value) is dict, 'explicit plan required')
    revision = value.get('revision')
    c.integer(revision, 'plan revision', 1, 64)
    parent = None if revision == 1 else {'plan_id': value.get('plan_id'), 'revision': revision - 1}
    return c.validate_plan(value, manifest['problem'], manifest['limits'], parent)


def _refkey(ref):
    c.reference(ref)
    return tuple(ref[key] for key in sorted(c.REF_FIELDS))


def _frozen_inputs(record):
    ctx = record['context']
    if ctx['plan'] is not None:
        yield ('plan', ctx['plan']['plan_id'], ctx['plan']['revision']), ctx['plan']
    items = list(ctx['dependency_closure'])
    if ctx['target_candidate'] is not None: items.append(ctx['target_candidate'])
    if 'opponent' in ctx:
        opponent = ctx['opponent']
        items.append(opponent['candidate'])
        yield ('plan', opponent['plan']['plan_id'], opponent['plan']['revision']), opponent['plan']
    if ctx['selected_issue'] is not None: items.append(ctx['selected_issue']['original_target_candidate'])
    for item in items:
        yield ('candidate', *_refkey(c.candidate_ref(item))), item
    for item in ctx['released_feedback']:
        core = {k: item[k] for k in ('receipt_id', 'target_ref', 'kind', 'content')}
        yield ('feedback', item['receipt_id']), core
        yield ('release', item['release_id']), core
        yield ('read', item['read_id']), item
    for item in ctx['planning_evidence']:
        core = {key: item[key] for key in ('receipt_id', 'action_id', 'plan_ref', 'action_kind', 'status',
                'target_ref', 'target_candidate', 'response', 'projection_status', 'source')}
        yield ('planning_receipt', item['receipt_id']), core
        yield ('planning_release', item['release_id']), core
        yield ('planning_read', item['read_id']), item
        if item['target_candidate'] is not None:
            candidate = item['target_candidate']
            yield ('candidate', *_refkey(c.candidate_ref(candidate))), candidate


def _review_output(kind, out, target, problem_ref, issue_id=None):
    if kind == 'investigate':
        c.validate_disposition(out, issue_id, target)
    elif kind == 'critique_candidate':
        c.fields(out, {'schema', 'target_ref', 'findings', 'no_gap_identified'}, 'critique')
        c.reference(out['target_ref'])
        _need(out['schema'] == 'victorique.adaptive_critique.v1' and _equal(out['target_ref'], c.candidate_ref(target)), 'critique target differs')
        _need(type(out['findings']) is list and len(out['findings']) <= 16
              and type(out['no_gap_identified']) is bool and out['no_gap_identified'] == (not out['findings']), 'critique findings differ')
        for finding in out['findings']: c.validate_defect(finding, target)
    else:
        _need(kind in c.GATES, 'unsupported review kind')
        c.fields(out, {'schema', 'gate_id', 'problem_ref', 'target_ref', 'coverage', 'outcome', 'findings', 'uncertainties'}, 'gate report')
        c.problem_ref(out['problem_ref']); c.reference(out['target_ref'])
        _need(out['schema'] == 'victorique.adaptive_gate.v1' and out['gate_id'] == kind
              and _equal(out['problem_ref'], problem_ref) and _equal(out['target_ref'], c.candidate_ref(target))
              and out['coverage'] == 'whole_problem_and_proof', 'gate binding differs')
        _need(type(out['outcome']) is str and out['outcome'] in ('pass', 'fail', 'unknown'), 'gate outcome differs')
        _need(type(out['findings']) is list and len(out['findings']) <= 16, 'gate findings differ')
        c.strings(out['uncertainties'], 'gate uncertainty')
        # A pass may keep minor findings or doubts; c.gate_passes, with the minor_findings
        # setting in the manifest limits (validate_manifest), decides whether it passes.
        _need(out['outcome'] != 'fail' or bool(out['findings']), 'fail lacks defect')
        for finding in out['findings']: c.validate_defect(finding, target)


def _planning_evidence(record, manifest, plan):
    ctx = record['context']; values = ctx['planning_evidence']; selection = ctx['planning_evidence_selection']
    _need(type(values) is list and len(values) <= 64, 'bounded planning evidence required')
    c.fields(selection, {'eligible_count', 'selected_count', 'max_entries', 'truncated', 'policy'}, 'planning evidence selection')
    for field in ('eligible_count', 'selected_count', 'max_entries'):
        c.integer(selection[field], 'planning evidence selection count', 0, 64)
    _need(selection['max_entries'] == 64 and selection['truncated'] is False
          and selection['policy'] == PLANNING_SELECTION_POLICY
          and selection['eligible_count'] == selection['selected_count'] == len(values), 'planning evidence cannot be silently omitted')
    if record['kind'] != 'replan':
        _need(not values, 'planning evidence is only for replanning')
        return
    seen = {field: set() for field in ('receipt_id', 'action_id', 'release_id', 'read_id')}
    previous_sequence = 0
    expected_plan = {'plan_id': plan['plan_id'], 'revision': plan['revision']}
    problem_ref = {key: manifest['problem'][key] for key in ('artifact_id', 'revision')}
    for item in values:
        c.fields(item, {'receipt_id', 'action_id', 'plan_ref', 'action_kind', 'status', 'target_ref',
                 'target_candidate', 'response', 'projection_status', 'source', 'release_id', 'read_id',
                 'recipient_role', 'recipient_actor_id'}, 'planning evidence entry')
        for field, prior in seen.items():
            c.identifier(item[field]); _need(item[field] not in prior, 'duplicate planning evidence identity'); prior.add(item[field])
        c.fields(item['plan_ref'], {'plan_id', 'revision'}, 'planning evidence plan reference')
        c.identifier(item['plan_ref']['plan_id']); c.integer(item['plan_ref']['revision'], 'plan revision', 1, 64)
        _need(_equal(item['plan_ref'], expected_plan), 'planning evidence references another plan')
        _need(type(item['action_kind']) is str and item['action_kind'] in c.CALLS, 'planning evidence action kind')
        _need(type(item['status']) is str and item['status'] in ('completed', 'failed'), 'planning evidence action status')
        _need(item['recipient_role'] == 'planner' and item['recipient_actor_id'] == manifest['role_bindings']['planner']['actor_id'], 'planning evidence recipient differs')
        target = item['target_candidate']
        if target is None:
            _need(item['target_ref'] is None, 'missing planning evidence target body')
        else:
            c.validate_candidate(target, plan, manifest['limits']); c.reference(item['target_ref'])
            _need(_equal(item['target_ref'], c.candidate_ref(target)), 'planning evidence target differs')
        source = item['source']
        c.fields(source, {'receipt_id', 'host_reference', 'event_id', 'sequence', 'source_event_id',
                 'source_line', 'recorded_at_utc', 'authenticated'}, 'planning evidence source')
        c.identifier(source['receipt_id']); c.identifier(source['event_id'])
        _need(source['receipt_id'] == item['receipt_id'] and source['authenticated'] is False, 'planning evidence source binding differs')
        _host(source['host_reference'], nullable=item['status'] == 'failed')
        c.integer(source['sequence'], 'planning evidence source sequence', 1, 10**12)
        _need(source['sequence'] > previous_sequence, 'planning evidence source order differs')
        previous_sequence = source['sequence']; c.utc(source['recorded_at_utc'])
        if source['source_event_id'] is not None: c.text(source['source_event_id'], 'source event', 4000)
        if source['source_line'] is not None: c.integer(source['source_line'], 'source line', 1, 10**12)
        if item['status'] == 'failed':
            _need(item['response'] is None and item['projection_status'] is None, 'failed planning evidence cannot invent a response')
        else:
            _need(item['action_kind'] in ('critique_candidate', 'investigate', *c.GATES)
                  and item['projection_status'] in ('declared_receipt_projected', 'stale_audit_only')
                  and target is not None, 'completed planning evidence lacks an eligible diagnostic')
            response = item['response']
            issue_id = response.get('issue_id') if item['action_kind'] == 'investigate' and type(response) is dict else None
            _review_output(item['action_kind'], response, target, problem_ref, issue_id)


def _check_frozen_inputs(state, record):
    known = {}
    for source in [r['record'] for r in state['requests'].values()] + [record]:
        for key, value in _frozen_inputs(source):
            _need(key not in known or _equal(known[key], value), 'frozen input identity has changed content')
            known[key] = value


def validate_request(record, manifest):
    record = c.copy_json(record)
    c.fields(record, PREPARE_FIELDS, 'request')
    _need(record['schema'] == REQUEST_SCHEMA, 'request schema differs')
    _common(record, manifest, observation=False)
    for key in ('request_id', 'logical_action_id', 'action_id', 'actor_id'): c.identifier(record[key])
    c.integer(record['prepared_state_revision'], 'declared state revision', 0, 10000)
    kind = record['kind']
    _need(type(kind) is str and kind in manifest['limits']['action_roles'] and record['role'] == manifest['limits']['action_roles'][kind], 'action role differs')
    actor = manifest['role_bindings'][record['role']]
    _need(record['actor_id'] == actor['actor_id'], 'actor differs')
    template = record['template']
    c.fields(template, {'template_id', 'version', 'instructions'}, 'frozen template')
    c.identifier(template['template_id']); c.identifier(template['version']); c.text(template['instructions'], 'frozen instructions', 64000)
    ctx = record['context']
    c.fields(ctx, {'problem', 'plan', 'target_candidate', 'dependency_closure', 'selected_issue', 'released_feedback',
                  'planning_evidence', 'planning_evidence_selection'} | ({'opponent'} if kind == 'compare_candidates' else set()), 'typed context')
    _need(_equal(ctx['problem'], manifest['problem']), 'problem content differs')
    plan = None if ctx['plan'] is None else _plan(ctx['plan'], manifest)
    _need((kind == 'propose_plan') == (plan is None), 'plan availability differs from action')
    if kind in ('propose_plan', 'replan'):
        _need(record['route_id'] is None and record['obligation_id'] is None, 'planner cannot bind an obligation')
    else:
        c.identifier(record['route_id']); c.identifier(record['obligation_id'])
        graph = c.nodes(plan)
        _need((record['route_id'], record['obligation_id']) in graph, 'selected obligation missing')
    target = ctx['target_candidate']
    if target is not None:
        _need(plan is not None, 'target without plan'); c.validate_candidate(target, plan, manifest['limits'])
        _need((target['route_id'], target['obligation_id']) == (record['route_id'], record['obligation_id']), 'target obligation differs')
    if kind in {'critique_candidate', 'investigate', 'repair_obligation', *c.GATES}:
        _need(target is not None, 'action requires exact target')
    if kind == 'compare_candidates':
        _need(target is not None, 'comparison requires exact target')
        opponent = ctx['opponent']
        c.fields(opponent, {'plan', 'candidate'}, 'comparison opponent')
        other_plan = _plan(opponent['plan'], manifest)
        c.validate_candidate(opponent['candidate'], other_plan, manifest['limits'])
        _need(not _equal(c.candidate_ref(target), c.candidate_ref(opponent['candidate'])), 'comparison requires distinct candidate versions')
        _need(not target['dependency_refs'] and not opponent['candidate']['dependency_refs']
              and not record['dependency_refs'] and not ctx['released_feedback'] and not ctx['planning_evidence'],
              'comparison requires standalone manuscripts without feedback')
    if kind in {'propose_plan', 'replan'}:
        _need(target is None and record['dependency_refs'] == [] and ctx['selected_issue'] is None, 'planner target leakage')
    _need(type(record['dependency_refs']) is list and type(ctx['dependency_closure']) is list, 'explicit dependency lists required')
    refs = record['dependency_refs']
    for ref in refs: c.reference(ref)
    _need(len({_refkey(r) for r in refs}) == len(refs), 'duplicate dependency reference')
    if kind not in ('propose_plan', 'replan'):
        node = c.nodes(plan)[(record['route_id'], record['obligation_id'])]
        _need(len(refs) == len(node['depends_on']) and {r['obligation_id'] for r in refs} == set(node['depends_on']), 'selected direct dependencies differ')
        _need(all(r['route_id'] == record['route_id'] and r['plan_id'] == plan['plan_id'] and r['plan_revision'] == plan['revision'] for r in refs), 'selected dependency plan/route differs')
    candidates = {}
    for item in ctx['dependency_closure']:
        _need(plan is not None, 'dependency without plan')
        c.validate_candidate(item, plan, manifest['limits']); key = _refkey(c.candidate_ref(item))
        _need(key not in candidates, 'duplicate closure candidate'); candidates[key] = item
    roots = list(refs) + ([] if target is None else target['dependency_refs'])
    visited, visiting = set(), set()
    def walk(ref):
        key = _refkey(ref)
        _need(key in candidates and key not in visiting, 'missing/cyclic exact dependency closure')
        if key in visited: return
        visiting.add(key)
        for child in candidates[key]['dependency_refs']: walk(child)
        visiting.remove(key); visited.add(key)
    for ref in roots: walk(ref)
    _need(visited == set(candidates), 'unreferenced context candidate refused')
    _need(list(candidates) == sorted(candidates), 'dependency closure must use deterministic reference order')
    issue = ctx['selected_issue']
    if kind in {'investigate', 'repair_obligation'}:
        _need(issue is not None, 'selected issue required')
        c.fields(issue, {'proposal', 'original_target_candidate', 'target_ref', 'disposition'}, 'versioned issue context')
        c.validate_candidate(issue['original_target_candidate'], plan, manifest['limits'])
        c.validate_defect(issue['proposal'], issue['original_target_candidate'])
        c.reference(issue['target_ref'])
        _need(_equal(issue['target_ref'], c.candidate_ref(target)), 'current issue target differs')
        _need(type(issue['disposition']) is str and issue['disposition'] in ('proposed', 'confirmed', 'refuted', 'unresolved', 'addressed_claim'), 'issue disposition differs')
    else:
        _need(issue is None, 'unexpected selected issue')
    feedback = ctx['released_feedback']
    _need(type(feedback) is list and len(feedback) <= 64, 'bounded declared feedback list required')
    ids = set()
    for item in feedback:
        c.fields(item, {'receipt_id', 'target_ref', 'kind', 'content', 'release_id', 'read_id', 'recipient_role', 'recipient_actor_id'}, 'declared feedback')
        for key in ('receipt_id', 'kind', 'release_id', 'read_id'): c.identifier(item[key])
        c.reference(item['target_ref']); c.text(item['content'], 'feedback content')
        _need(item['recipient_role'] == record['role'] and item['recipient_actor_id'] == record['actor_id'], 'feedback recipient differs')
        _need(item['receipt_id'] not in ids, 'duplicate feedback receipt'); ids.add(item['receipt_id'])
        _need(target is not None and item['target_ref'] == c.candidate_ref(target), 'feedback target differs')
    if kind in c.GATES:
        _need(not feedback, 'cold review cannot receive feedback')
    _planning_evidence(record, manifest, plan)
    return record


def build_packet(record, manifest, packet_id):
    record = validate_request(record, manifest); c.identifier(packet_id)
    ctx = deepcopy(record['context']); target = ctx['target_candidate']; plan = ctx['plan']
    binding = {'schema': 'victorique.adaptive_binding.v2', 'session_id': manifest['session_id'],
        'protocol': manifest['protocol'], 'request_id': record['request_id'], 'logical_action_id': record['logical_action_id'],
        'action_id': record['action_id'], 'packet_id': packet_id, 'prepared_state_revision': record['prepared_state_revision'],
        'route_id': record['route_id'], 'obligation_id': record['obligation_id'],
        'role': record['role'], 'actor_id': record['actor_id'], 'model_request': manifest['role_bindings'][record['role']]['model_request'],
        'action_kind': record['kind'], 'problem_ref': {k: manifest['problem'][k] for k in ('artifact_id', 'revision')},
        'plan_ref': None if plan is None else {'plan_id': plan['plan_id'], 'revision': plan['revision']},
        'target_ref': None if target is None else c.candidate_ref(target), 'dependency_refs': record['dependency_refs'],
        'dependency_closure_refs': [c.candidate_ref(x) for x in ctx['dependency_closure']],
        'issue_id': None if ctx['selected_issue'] is None else ctx['selected_issue']['proposal']['issue_id'],
        'feedback_release_ids': [x['release_id'] for x in ctx['released_feedback']],
        'feedback_read_ids': [x['read_id'] for x in ctx['released_feedback']],
        'planning_evidence_refs': [{key: item[key] for key in ('receipt_id', 'action_id', 'plan_ref', 'target_ref',
            'release_id', 'read_id', 'recipient_role', 'recipient_actor_id')} |
            {'source_event_id': item['source']['event_id'], 'source_sequence': item['source']['sequence']}
            for item in ctx['planning_evidence']],
        'template_id': record['template']['template_id'], 'template_version': record['template']['version'],
        'cap_units': manifest['limits']['action_caps'][record['kind']], 'budget_unit_id': manifest['budget_unit']['unit_id']}
    if record['kind'] == 'compare_candidates':
        binding['opponent_ref'] = c.candidate_ref(ctx['opponent']['candidate'])
    if record['kind'] in c.GATES:
        ctx['plan'] = None  # Full coordinator plan is not the cold role view.
    packet = {'schema': PACKET_SCHEMA, 'binding': binding, 'context': ctx, 'template': record['template'],
        'response_contract': {'schema': RESPONSE_SCHEMA, 'fields': ['schema', 'binding', 'output'], 'binding': binding},
        'controller_admission_verified': False, 'feedback_release_verified': False,
        'model_consumption_verified': False, 'mathematical_truth_verified': False}
    raw = (_dump(packet) + '\n').encode('utf-8')
    _need(len(raw) <= manifest['storage_limits']['max_packet_bytes'], 'packet storage limit')
    return packet, raw


def validate_response(raw, packet, request, manifest):
    _need(type(raw) is bytes, 'raw bytes required')
    _need(len(raw) <= manifest['storage_limits']['max_decoded_response_bytes'], 'decode limit')
    value = c.parse_json(raw.decode('utf-8'))
    c.fields(value, {'schema', 'binding', 'output'}, 'response envelope')
    _need(value['schema'] == RESPONSE_SCHEMA and _equal(value['binding'], packet['binding']), 'response full binding differs')
    kind, out, ctx = request['kind'], value['output'], request['context']
    target = ctx['target_candidate']; plan = ctx['plan']
    if kind == 'compare_candidates':
        c.fields(out, {'schema', 'outcome', 'winner_ref', 'reason'}, 'candidate selection')
        _need(out['schema'] == 'victorique.adaptive_selection.v1'
              and out['outcome'] in ('selected', 'unknown'), 'selection outcome differs')
        c.text(out['reason'], 'selection reason', 10000)
        if out['outcome'] == 'selected':
            c.reference(out['winner_ref'])
            _need(any(_equal(out['winner_ref'], ref) for ref in
                      (packet['binding']['target_ref'], packet['binding']['opponent_ref'])), 'selection winner is outside frozen pair')
        else:
            _need(out['winner_ref'] is None, 'unknown selection cannot name a winner')
    elif kind in ('propose_plan', 'replan'):
        c.validate_plan(out, manifest['problem'], manifest['limits'], plan)
    elif kind in ('solve_obligation', 'repair_obligation', 'integrate'):
        c.validate_candidate(out, plan, manifest['limits'])
        _need((out['route_id'], out['obligation_id']) == (request['route_id'], request['obligation_id']), 'response obligation differs')
        _need(out['dependency_refs'] == request['dependency_refs'], 'response dependency binding differs')
        if target is not None:
            _need(out['artifact_id'] == target['artifact_id'] and out['revision'] == target['revision'] + 1
                  and out['route_id'] == target['route_id'] and out['obligation_id'] == target['obligation_id'], 'response target lineage differs')
        else:
            _need(out['revision'] == 1, 'new candidate must be first revision')
        addressed = [] if kind != 'repair_obligation' else [ctx['selected_issue']['proposal']['issue_id']]
        _need(out['addressed_issue_ids'] == addressed, 'repair declaration differs')
    else:
        issue_id = ctx['selected_issue']['proposal']['issue_id'] if kind == 'investigate' else None
        _review_output(kind, out, target, packet['binding']['problem_ref'], issue_id)
    return value


def _common(record, manifest, observation):
    _need(record['session_id'] == manifest['session_id'], 'session differs')
    c.identifier(record['operation_id'])
    sequence = record['expected_event_sequence']
    if observation and sequence is None: return
    c.integer(sequence, 'expected event sequence', 0, 100000)


def _source(record):
    if record['source_event_id'] is not None: c.text(record['source_event_id'], 'source event', 4000)
    if record['source_line'] is not None: c.integer(record['source_line'], 'source line', 1, 10**12)


def _charge_result(state, request, value, source_matches):
    """Preserve malformed/conflicting bills as observations, never as zero."""
    if value is None:
        return 'not_observed'
    try:
        c.fields(value, {'charge_id', 'unit_id', 'cumulative_units', 'final_bill', 'source', 'usage'}, 'charge observation')
        c.identifier(value['charge_id'])
        _need(value['unit_id'] == state['manifest']['budget_unit']['unit_id'], 'charge unit differs')
        _need(type(value['final_bill']) is bool, 'final bill flag required')
        if value['cumulative_units'] is None:
            _need(value['source'] is None and not value['final_bill'], 'unknown bill cannot be final')
        else:
            c.integer(value['cumulative_units'], 'cumulative charge', 0, 10**12)
            c.text(value['source'], 'billing source', 4000)
        _need(value['usage'] is None or type(value['usage']) is dict, 'usage must be null or explicit metadata')
        _need(source_matches and request is not None, 'unattributed billing source')
        # A quarantined declaration never establishes ownership of a charge
        # ID. The original anomaly stays in the ledger and keeps dispatch
        # blocked, but it must not hide a later correctly attributed bill.
        existing = next((r for r in state['charges'] if r['classification'] == 'accepted'
                         and type(r['charge']) is dict and r['charge'].get('charge_id') == value['charge_id']), None)
        if existing is not None:
            _need(existing['request_id'] == request['request_id'] and _equal(existing['charge'], value), 'charge ID conflict')
            return 'duplicate'
        old = request['known_cost_units']
        if value['cumulative_units'] is not None:
            _need(old is None or value['cumulative_units'] >= old, 'cumulative bill regressed')
            if request['bill_final']:
                _need(value['cumulative_units'] == old and value['final_bill'], 'final bill conflict')
            request['known_cost_units'] = value['cumulative_units']
            request['bill_final'] = value['final_bill']
        return 'accepted'
    except (ValueError, TypeError, KeyError):
        state['financial_blocked'] = True
        return 'quarantined'


def _budgets(state):
    roles = {role: {'reserved_calls': 0, 'committed_units': 0} for role in c.ROLES}
    actual = 0; unknown = False; total = 0; overspent = False
    for request in state['requests'].values():
        known = request['known_cost_units']; cap = request['packet']['binding']['cap_units']
        liability = known if request['bill_final'] and known is not None else max(cap, known or 0)
        role = request['record']['role']; roles[role]['reserved_calls'] += 1; roles[role]['committed_units'] += liability
        total += liability; unknown |= known is None
        if known is not None: actual += known; overspent |= known > cap
    limits = state['manifest']['limits']
    overspent |= total > limits['total_budget_units'] or any(roles[r]['committed_units'] > limits['role_budget_units'][r] for r in c.ROLES)
    return {'reserved_calls': len(state['requests']), 'committed_units': total,
            'actual_observed_units': None if unknown else actual, 'roles': roles,
            'request_or_total_cap_exceeded': overspent, 'financial_blocked': state['financial_blocked'],
            'usage': None, 'financially_settled': all(r['bill_final'] for r in state['requests'].values())}


def _admit(state, record=None, claiming=None, at_utc=None):
    _need(state['endpoint'] is None, 'exchange is sealed')
    budget = _budgets(state); limits = state['manifest']['limits']
    deadline = limits['stop_condition']['deadline_utc']
    _need(deadline is None or (at_utc is not None and c.utc(at_utc) < c.utc(deadline)), 'stopping deadline reached')
    _need(not budget['financial_blocked'] and not budget['request_or_total_cap_exceeded'], 'financial responsibility blocks dispatch')
    _need(not any(r['phase'] == 'unknown' for r in state['requests'].values()), 'unknown outcome blocks dispatch')
    if record is not None:
        role, cap = record['role'], limits['action_caps'][record['kind']]
        _need(budget['reserved_calls'] < limits['global_call_limit'], 'global call cap')
        _need(budget['roles'][role]['reserved_calls'] < limits['role_call_limits'][role], 'role call cap')
        _need(budget['committed_units'] + cap <= limits['total_budget_units'], 'global fee cap')
        _need(budget['roles'][role]['committed_units'] + cap <= limits['role_budget_units'][role], 'role fee cap')
        _need(sum(r['phase'] not in TERMINAL for r in state['requests'].values()) < limits['max_inflight'], 'inflight cap')
    if claiming is not None:
        _need(claiming['phase'] == 'prepared' and claiming['exported'], 'claim requires prepared exported packet')


def _observation_fields(record, manifest):
    c.fields(record, OBS_FIELDS, 'host observation'); _common(record, manifest, True)
    _need(record['schema'] == 'victorique.adaptive_observation.v1', 'observation schema')
    for key in ('observation_id', 'request_id', 'claim_id'): c.identifier(record[key])
    _host(record['host_reference'], nullable=True); _source(record)
    _need(record['transport_status'] in ('returned', 'failed', 'unknown'), 'transport outcome')
    if record['external_observed_at_utc'] is not None: c.utc(record['external_observed_at_utc'])
    if record['model_observation'] is not None:
        c.fields(record['model_observation'], {'provider_id', 'model_id', 'revision'}, 'model observation metadata')
        for value in record['model_observation'].values():
            if value is not None: c.text(value, 'observed model identity', 2000)
    if record['reason'] is not None: c.text(record['reason'], 'observation reason', 4000)


def _initial(manifest):
    return {'manifest': manifest, 'requests': {}, 'observations': [], 'charges': [], 'endpoint': None,
            'financial_blocked': False, 'sequence': 0,
            'archive_bytes': manifest['initial_input']['byte_length'] if manifest['schema'] == seed.MANIFEST_SCHEMA else 0}


def _fork(state):
    """Return a copy that _apply may change while the given state stays as it was.

    _apply assigns top-level fields, adds request rows, assigns scalar fields of
    request rows and appends observation and charge rows. It never changes a
    record, packet, observation, charge, endpoint or manifest after creating
    it, so those are shared instead of deep-copied.
    """
    return {**state, 'requests': {rid: dict(row) for rid, row in state['requests'].items()},
            'observations': list(state['observations']), 'charges': list(state['charges'])}


REQUEST_ROW = ('logical_action_id', 'claim_id', 'host_reference', 'payload')
EVENT_ROW = ('sequence', 'event_id', 'operation_id', 'payload')


def _row(row, names):
    return tuple(row[name] for name in names)


def _schema(db):
    """Schema rows and user_version, compared directly between transactions."""
    rows = tuple(tuple(row) for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
    return rows, db.execute('PRAGMA user_version').fetchone()[0]


def _stored_event(row):
    event = _json(row['payload'])
    c.fields(event, {'sequence', 'event_id', 'operation_id', 'recorded_at_utc', 'kind', 'data'}, 'event')
    c.integer(event['sequence'], 'event sequence', 1, 10**12)
    _need(event['sequence'] == row['sequence'] and event['event_id'] == row['event_id'] and event['operation_id'] == row['operation_id'], 'event columns differ')
    _need(event['operation_id'] == event['data']['record']['operation_id'], 'operation identity differs')
    return event


def _appended_rows(count, low, high, known):
    """Rows 1..count with no gap, and at least the known rows."""
    return count >= known and count == (high or 0) and (not count or low == 1)


class _Connection(sqlite3.Connection):
    """Transaction connection; work holds the state verified for this transaction."""
    work = None


def _apply(state, event, raw):
    """Recompute every index from an immutable event; no model or reducer state."""
    kind, data = event['kind'], event['data']; manifest = state['manifest']
    c.utc(event['recorded_at_utc'])
    c.integer(event['sequence'], 'event sequence', 1, 10**12)
    _need(event['sequence'] == state['sequence'] + 1, 'event gap')
    record = data['record']; _common(record, manifest, kind in ('bind', 'observe', 'charge'))
    if kind not in ('bind', 'observe', 'charge'):
        _need(record['expected_event_sequence'] == state['sequence'], 'control CAS differs')
    if kind == 'prepare':
        c.fields(data, {'record', 'packet_id'}, 'prepare event')
        record = validate_request(record, manifest); _admit(state, record, at_utc=event['recorded_at_utc'])
        _check_frozen_inputs(state, record)
        _need(record['request_id'] not in state['requests'], 'duplicate request')
        _need(not any(r['record']['logical_action_id'] == record['logical_action_id'] or r['record']['action_id'] == record['action_id'] for r in state['requests'].values()), 'duplicate action identity')
        packet, packet_bytes = build_packet(record, manifest, data['packet_id'])
        state['requests'][record['request_id']] = {'request_id': record['request_id'], 'record': record, 'packet': packet,
            'phase': 'prepared', 'exported': False, 'claim_id': None, 'host_reference': None, 'first_result_observation_id': None,
            'known_cost_units': None, 'bill_final': False, 'prepared_at_utc': event['recorded_at_utc']}
        state['archive_bytes'] += len(packet_bytes)
    elif kind == 'export':
        c.fields(data, {'record'}, 'export event')
        c.fields(record, COMMON | {'request_id'}, 'export record')
        _need(record['schema'] == 'victorique.adaptive_export.v1', 'export schema')
        request = state['requests'][record['request_id']]
        _need(state['endpoint'] is None, 'sealed exchange cannot first export')
        request['exported'] = True
    elif kind == 'claim':
        c.fields(data, {'record', 'claim_id'}, 'claim event')
        c.fields(record, COMMON | {'request_id', 'packet_id'}, 'claim record')
        _need(record['schema'] == 'victorique.adaptive_claim.v1', 'claim schema')
        request = state['requests'][record['request_id']]; _admit(state, claiming=request, at_utc=event['recorded_at_utc'])
        _need(record['packet_id'] == request['packet']['binding']['packet_id'], 'claim packet differs')
        c.identifier(data['claim_id']); request.update(claim_id=data['claim_id'], phase='claimed')
    elif kind == 'bind':
        c.fields(data, {'record'}, 'bind event')
        c.fields(record, COMMON | {'request_id', 'claim_id', 'host_reference'}, 'bind record')
        _need(record['schema'] == 'victorique.adaptive_bind.v1', 'bind schema')
        request = state['requests'][record['request_id']]
        _host(record['host_reference'])
        _need(record['claim_id'] == request['claim_id'] and request['claim_id'] is not None, 'bind claim differs')
        _need(request['host_reference'] in (None, record['host_reference']), 'host binding changed')
        _need(record['host_reference'] not in manifest['forbidden_host_references'], 'forbidden host reused')
        _need(not any(r is not request and r['host_reference'] == record['host_reference'] for r in state['requests'].values()), 'host used by another request')
        request['host_reference'] = record['host_reference']
        if request['phase'] == 'claimed': request['phase'] = 'bound'
    elif kind == 'observe':
        c.fields(data, {'record', 'capture'}, 'observation event')
        _observation_fields(record, manifest)
        _need(not any(r['observation_id'] == record['observation_id'] for r in state['observations']), 'observation ID conflict')
        request = state['requests'].get(record['request_id'])
        matched = request is not None and request['claim_id'] is not None and record['claim_id'] == request['claim_id']
        host_matches = matched and request['host_reference'] is not None and record['host_reference'] == request['host_reference']
        charge_status = _charge_result(state, request, record['charge_observation'], host_matches)
        if record['charge_observation'] is not None:
            state['charges'].append({'event_id': event['event_id'], 'request_id': record['request_id'],
                'charge': record['charge_observation'], 'classification': charge_status})
        capture = data['capture']; c.fields(capture, {'status', 'original_byte_length'}, 'raw capture')
        _need(capture['status'] in ('full', 'absent', 'storage_limit_exceeded', 'size_limit', 'unreadable'), 'capture status')
        if capture['status'] == 'absent':
            _need(capture['original_byte_length'] is None, 'absent response length differs')
        elif capture['status'] == 'unreadable' and capture['original_byte_length'] is None:
            pass
        else:
            c.integer(capture['original_byte_length'], 'original byte length', 0, 10**12)
            if capture['status'] == 'size_limit':
                _need(capture['original_byte_length'] > 0, 'size-limited source requires a known positive length')
        if capture['status'] == 'full':
            _need(type(raw) is bytes and len(raw) == capture['original_byte_length'], 'raw BLOB differs')
            _need(len(raw) <= manifest['storage_limits']['max_raw_response_bytes'], 'raw BLOB exceeds request cap')
            state['archive_bytes'] += len(raw)
        else:
            _need(raw is None, 'unexpected raw BLOB')
        source_capture_failure = capture['status'] in ('size_limit', 'unreadable')
        classification = 'quarantined_binding' if not matched or ((record['transport_status'] != 'unknown' or source_capture_failure) and not host_matches) else None
        parsed = None
        if classification is None:
            if capture['status'] in ('storage_limit_exceeded', 'size_limit', 'unreadable'):
                classification = 'capture_failed'
            elif record['transport_status'] == 'unknown':
                classification = 'unknown'
            elif record['transport_status'] == 'failed':
                classification = 'failed'
            else:
                try:
                    parsed = validate_response(raw, request['packet'], request['record'], manifest)
                    classification = 'returned_valid'
                    if record['model_observation'] is not None:
                        requested = request['packet']['binding']['model_request']
                        if any(value is not None and requested[key] is not None and value != requested[key]
                               for key, value in record['model_observation'].items()):
                            classification, parsed = 'returned_invalid_model', None
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                    classification = 'returned_invalid'
        historical = state['endpoint'] is not None or (request is not None and request['phase'] in TERMINAL)
        row = {'observation_id': record['observation_id'], 'request_id': record['request_id'], 'event_id': event['event_id'],
            'classification': classification, 'historical_only': historical, 'capture': capture,
            'charge_classification': charge_status, 'declared_event_sequence': record['expected_event_sequence'],
            'actual_event_sequence': event['sequence'], 'sequence_conflict': record['expected_event_sequence'] not in (None, state['sequence']),
            'parsed_response': parsed, 'record': record}
        state['observations'].append(row)
        if request is not None and matched and not historical and classification in TERMINAL | {'unknown'}:
            request['phase'] = classification
            if classification in TERMINAL: request['first_result_observation_id'] = record['observation_id']
        if classification == 'capture_failed' and state['endpoint'] is None:
            state['endpoint'] = {'decision': 'capture_failed', 'reason': 'original inbox must be preserved; raw not fully archived',
                'frozen_at_utc': event['recorded_at_utc'], 'event_sequence': event['sequence'], 'remote_shutdown_verified': False,
                'mathematical_truth_verified': False}
    elif kind == 'charge':
        c.fields(data, {'record'}, 'charge event')
        c.fields(record, COMMON | {'request_id', 'claim_id', 'host_reference', 'charge_observation'}, 'billing record')
        _need(record['schema'] == 'victorique.adaptive_charge.v1', 'charge schema')
        request = state['requests'].get(record['request_id'])
        matched = request is not None and request['claim_id'] is not None and record['claim_id'] == request['claim_id']
        matched = matched and request['host_reference'] is not None and record['host_reference'] == request['host_reference']
        classification = _charge_result(state, request, record['charge_observation'], matched)
        state['charges'].append({'event_id': event['event_id'], 'request_id': record['request_id'],
            'charge': record['charge_observation'], 'classification': classification})
    elif kind == 'close':
        c.fields(data, {'record'}, 'close event')
        c.fields(record, COMMON | {'reason'}, 'closure record')
        _need(record['schema'] == 'victorique.adaptive_close.v1' and state['endpoint'] is None, 'closure differs')
        c.text(record['reason'], 'close reason', 4000)
        state['endpoint'] = {'decision': 'closed', 'reason': record['reason'], 'frozen_at_utc': event['recorded_at_utc'],
            'event_sequence': event['sequence'], 'budgets_at_close': _budgets(state),
            'remote_shutdown_verified': False, 'mathematical_truth_verified': False}
    else:
        raise AdaptiveExchangeError('unsupported event kind')
    _need(state['archive_bytes'] <= manifest['storage_limits']['max_session_archive_bytes'], 'session archive cap exceeded')
    state['sequence'] = event['sequence']
    return state


class AdaptiveExchange:
    """Replay-checked exchange. No reducer projection and no external execution."""
    SUPPORTS_SEEDED = False
    EXTRA_TABLES = ()
    # The state this object last verified. It is read and replaced only while
    # the database write lock is held, and a published value is never changed.
    _verified = None

    def _create_extension(self, db, manifest):
        """Trusted source subclass hook only; never loaded from a manifest."""

    def _after_append(self, db, prior_base, new_base, event, raw):
        """Subclass must record domain rejection without losing base evidence."""

    def _verify_extension(self, db, base_state):
        """Trusted subclass verifies its own replay in this same transaction."""

    def _verify_extension_since(self, db, base_state, verified, delta):
        """Check the extension after only the events in delta were applied.

        verified is what the extension check returned for this object's last
        verified state; delta lists (event, raw, prior state, new state) for
        each new event. A subclass without its own incremental check verifies
        in full, as before.
        """
        return self._verify_extension(db, base_state)

    def __init__(self, root):
        self.root = _root(root); self.path = self.root / 'adaptive_session.sqlite3'

    @staticmethod
    def _checkpoint(stage, identity=None):
        """Synthetic fault injection point; never invokes a host."""

    @classmethod
    def create(cls, root, manifest):
        manifest = validate_manifest(manifest); store = cls(root)
        _need(manifest['schema'] != seed.MANIFEST_SCHEMA or cls.SUPPORTS_SEEDED,
              'seeded profile requires durable AdaptiveSession')
        if store.path.exists():
            with store._tx() as (db, state):
                _need(_equal(state['manifest'], manifest), 'frozen manifest differs')
            return store
        _need(not store.root.exists(), 'fresh session directory required; missing DB not rebuilt')
        store.root.mkdir(parents=True, exist_ok=False)
        store._guard()
        with store.path.open('xb'): pass
        db = sqlite3.connect(store.path, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL'); db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE events(sequence INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,operation_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE requests(request_id TEXT PRIMARY KEY,logical_action_id TEXT UNIQUE NOT NULL,claim_id TEXT UNIQUE,host_reference TEXT UNIQUE,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE packets(packet_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,raw BLOB NOT NULL)')
            db.execute('CREATE TABLE observations(observation_id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,raw BLOB,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE charges(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE endpoint(identity INTEGER PRIMARY KEY CHECK(identity=1),payload TEXT NOT NULL)')
            db.execute('INSERT INTO metadata VALUES (?,?)', ('manifest', _dump(manifest)))
            db.execute('INSERT INTO metadata VALUES (?,?)', ('root', _dump(str(store.root))))
            for table in ('metadata', 'events', 'packets', 'observations', 'charges', 'endpoint'):
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, "immutable record"); END')
            store._create_extension(db, manifest)
            db.execute('PRAGMA user_version=1'); db.commit()
        except BaseException:
            db.rollback(); raise
        finally:
            db.close()
        store._checkpoint('initialized', None)
        return store

    @classmethod
    def open(cls, root):
        store = cls(root)
        with store._tx(): pass
        return store

    def _guard(self):
        _root(self.root)  # the root and each of its parents, once per transaction
        for suffix in ('', '-wal', '-shm', '-journal'):
            _check_leaf(Path(str(self.path) + suffix), transient_sqlite_sidecar=bool(suffix))

    @contextmanager
    def _tx(self):
        self._guard(); _need(self.path.is_file(), 'session database missing')
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None, factory=_Connection); db.row_factory = sqlite3.Row
        locked = False; restore = None
        try:
            db.execute('PRAGMA busy_timeout=10000'); db.execute('PRAGMA synchronous=FULL'); db.execute('BEGIN IMMEDIATE')
            locked = True
            state = self._replay(db)
            restore = dict(db.work)  # a failed body rolls the database back to this verified state
            yield db, state
            self._verified, restore = dict(db.work), None  # a failed commit leaves nothing verified
            db.commit()
        except BaseException:
            if locked: self._verified = restore
            db.rollback(); raise
        finally:
            db.work = None; db.close()

    def _replay(self, db):
        """Return this transaction's verified state.

        The first transaction of an object replays every event: open, create
        on an existing database, and each newly opened session, bridge or loop.
        Later transactions of the same object apply only the events appended
        since the state it last verified, after checking that nothing else
        changed. Any other difference runs the full replay, which fails as before.
        """
        verified, self._verified = self._verified, None
        work = None
        if verified is not None:
            try:
                work = self._replay_since(db, verified)
            except Exception:
                work = None
        if work is None:
            work = self._replay_full(db)
        db.work = work
        return work['state']

    def _replay_full(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        _need(tables == TABLES | set(self.EXTRA_TABLES) and db.execute('PRAGMA user_version').fetchone()[0] == 1, 'partial/foreign adaptive schema')
        metadata = tuple(_row(r, ('key', 'payload')) for r in db.execute('SELECT key,payload FROM metadata ORDER BY key'))
        meta = {key: _json(payload) for key, payload in metadata}
        _need(set(meta) == {'manifest', 'root'} and meta['root'] == str(self.root), 'session metadata differs')
        manifest = validate_manifest(meta['manifest'])
        _need(manifest['schema'] != seed.MANIFEST_SCHEMA or self.SUPPORTS_SEEDED,
              'seeded profile requires durable AdaptiveSession')
        state = _initial(manifest)
        observed = {r['observation_id']: r for r in db.execute('SELECT * FROM observations')}
        events = list(db.execute('SELECT * FROM events ORDER BY sequence'))
        for row in events:
            event = _stored_event(row)
            raw = None
            if event['kind'] == 'observe':
                obs = observed.get(event['data']['record']['observation_id'])
                _need(obs is not None and obs['event_id'] == event['event_id'], 'raw observation row missing')
                raw = obs['raw']
            state = _apply(state, event, raw)
        requests = list(db.execute('SELECT * FROM requests'))
        actual_requests = {r['request_id']: _json(r['payload']) for r in requests}
        _need(_equal(actual_requests, state['requests']), 'request cache differs from replay')
        for row in requests:
            item = state['requests'][row['request_id']]
            _need(row['logical_action_id'] == item['record']['logical_action_id'] and row['claim_id'] == item['claim_id'] and row['host_reference'] == item['host_reference'], 'request indexed identity differs')
        packets = {r['request_id']: r for r in db.execute('SELECT * FROM packets')}
        _need(set(packets) == set(state['requests']), 'packet coverage differs')
        for rid, request in state['requests'].items():
            expected = (_dump(request['packet']) + '\n').encode('utf-8')
            _need(packets[rid]['raw'] == expected and packets[rid]['packet_id'] == request['packet']['binding']['packet_id'], 'exact packet bytes differ')
        _need(set(observed) == {o['observation_id'] for o in state['observations']}, 'extra observation row')
        for item in state['observations']:
            _need(_equal(_json(observed[item['observation_id']]['payload']), item), 'observation classification differs from replay')
        charges = {r['event_id']: _json(r['payload']) for r in db.execute('SELECT * FROM charges')}
        _need(_equal(charges, {x['event_id']: x for x in state['charges']}), 'billing cache differs from replay')
        endpoint = list(db.execute('SELECT * FROM endpoint'))
        _need((not endpoint and state['endpoint'] is None) or (len(endpoint) == 1 and _equal(_json(endpoint[0]['payload']), state['endpoint'])), 'endpoint differs from replay')
        work = {'state': state, 'schema': _schema(db), 'metadata': metadata,
                'event': _row(events[-1], EVENT_ROW) if events else None,
                'requests': {r['request_id']: _row(r, REQUEST_ROW) for r in requests}}
        work['extension'] = self._verify_extension(db, state)
        return work

    def _replay_since(self, db, verified):
        """Apply the events appended since verified; raise on any other change.

        The tables other than requests refuse UPDATE and DELETE by trigger, so
        the schema (with its triggers) and metadata are compared directly; the
        event rows must run 1..count with the last verified row unchanged; the
        rows the new events added are compared with the state, and the row
        counts must match it. The requests table has no trigger, so every row
        is compared.
        """
        _need(_schema(db) == verified['schema'], 'schema changed')
        metadata = tuple(_row(r, ('key', 'payload')) for r in db.execute('SELECT key,payload FROM metadata ORDER BY key'))
        _need(metadata == verified['metadata'], 'session metadata changed')
        state = verified['state']; known = state['sequence']
        count, low, high = db.execute('SELECT count(*),min(sequence),max(sequence) FROM events').fetchone()
        _need(_appended_rows(count, low, high, known), 'event rows changed')
        if known:
            row = db.execute('SELECT sequence,event_id,operation_id,payload FROM events WHERE sequence=?', (known,)).fetchone()
            _need(row is not None and tuple(row) == verified['event'], 'verified event changed')
        delta, last = [], verified['event']
        rows = db.execute('SELECT * FROM events WHERE sequence>? ORDER BY sequence', (known,)).fetchall() if count > known else []
        for row in rows:
            event = _stored_event(row); raw = None
            if event['kind'] == 'observe':
                obs = db.execute('SELECT event_id,raw FROM observations WHERE observation_id=?',
                                 (event['data']['record']['observation_id'],)).fetchone()
                _need(obs is not None and obs['event_id'] == event['event_id'], 'raw observation row missing')
                raw = obs['raw']
            prior = state
            state = _apply(_fork(prior), event, raw)
            delta.append((event, raw, prior, state))
            last = _row(row, EVENT_ROW)
        for event, _, prior, after in delta:
            record = event['data']['record']
            if event['kind'] == 'prepare':
                request = after['requests'][record['request_id']]
                packet = db.execute('SELECT packet_id,raw FROM packets WHERE request_id=?', (record['request_id'],)).fetchone()
                _need(packet is not None and packet['packet_id'] == request['packet']['binding']['packet_id']
                      and packet['raw'] == (_dump(request['packet']) + '\n').encode('utf-8'), 'exact packet bytes differ')
            if event['kind'] == 'observe':
                item = after['observations'][-1]
                stored = db.execute('SELECT payload FROM observations WHERE observation_id=?', (item['observation_id'],)).fetchone()
                _need(stored is not None and stored[0] == _dump(item), 'observation classification differs from replay')
            if len(after['charges']) > len(prior['charges']):
                stored = db.execute('SELECT payload FROM charges WHERE event_id=?', (event['event_id'],)).fetchone()
                _need(stored is not None and stored[0] == _dump(after['charges'][-1]), 'billing cache differs from replay')
        counts = db.execute('SELECT (SELECT count(*) FROM packets),(SELECT count(*) FROM observations),(SELECT count(*) FROM charges)').fetchone()
        _need(tuple(counts) == (len(state['requests']), len(state['observations']), len(state['charges'])), 'row count differs from replay')
        endpoint = [r[0] for r in db.execute('SELECT payload FROM endpoint')]
        _need(endpoint == ([] if state['endpoint'] is None else [_dump(state['endpoint'])]), 'endpoint differs from replay')
        # _apply changes only the request named by an event's record.
        touched = {event['data']['record'].get('request_id') for event, *_ in delta}
        expected = {rid: (request['record']['logical_action_id'], request['claim_id'], request['host_reference'], _dump(request))
                    if rid in touched or rid not in verified['requests'] else verified['requests'][rid]
                    for rid, request in state['requests'].items()}
        actual = {r['request_id']: _row(r, REQUEST_ROW) for r in db.execute('SELECT request_id,logical_action_id,claim_id,host_reference,payload FROM requests')}
        _need(actual == expected, 'request cache differs from replay')
        work = {'state': state, 'schema': verified['schema'], 'metadata': metadata, 'event': last, 'requests': expected}
        work['extension'] = self._verify_extension_since(db, state, verified['extension'], delta)
        return work

    def _append(self, db, state, kind, record, extra=None, raw=None):
        existing = db.execute('SELECT payload FROM events WHERE operation_id=?', (record['operation_id'],)).fetchone()
        if existing:
            event = _json(existing[0]); _need(event['kind'] == kind and _equal(event['data']['record'], record), 'operation ID reused differently')
            if kind == 'observe':
                if extra is not None and 'capture' in extra:
                    _need(_equal(event['data']['capture'], extra['capture']), 'observation capture changed')
                stored = db.execute('SELECT raw FROM observations WHERE observation_id=?', (record['observation_id'],)).fetchone()[0]
                _need(stored == raw, 'observation raw changed')
            return state, event, False
        event = {'sequence': state['sequence'] + 1, 'event_id': 'event_' + uuid4().hex, 'operation_id': record['operation_id'],
            'recorded_at_utc': _now(), 'kind': kind, 'data': {'record': record, **(extra or {})}}
        new = _apply(_fork(state), event, raw)
        self._checkpoint('before_event_insert', record['operation_id'])
        payload = _dump(event)
        db.execute('INSERT INTO events VALUES (?,?,?,?)', (event['sequence'], event['event_id'], event['operation_id'], payload))
        if kind == 'prepare':
            req = new['requests'][record['request_id']]
            db.execute('INSERT INTO packets VALUES (?,?,?)', (req['packet']['binding']['packet_id'], record['request_id'], (_dump(req['packet']) + '\n').encode('utf-8')))
        requests = {}
        for req in new['requests'].values():
            requests[req['request_id']] = row = (req['record']['logical_action_id'], req['claim_id'], req['host_reference'], _dump(req))
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET claim_id=excluded.claim_id,host_reference=excluded.host_reference,payload=excluded.payload',
                (req['request_id'], *row))
        if kind == 'observe':
            obs = new['observations'][-1]
            db.execute('INSERT INTO observations VALUES (?,?,?,?)', (obs['observation_id'], event['event_id'], raw, _dump(obs)))
        if len(new['charges']) > len(state['charges']):
            db.execute('INSERT INTO charges VALUES (?,?)', (event['event_id'], _dump(new['charges'][-1])))
        if state['endpoint'] is None and new['endpoint'] is not None:
            db.execute('INSERT INTO endpoint VALUES (1,?)', (_dump(new['endpoint']),))
        self._after_append(db, state, new, event, raw)
        work = getattr(db, 'work', None)
        if work is not None:
            work.update(state=new, event=(event['sequence'], event['event_id'], event['operation_id'], payload), requests=requests)
        self._checkpoint('before_commit', record['operation_id'])
        return new, event, True

    def prepare(self, record):
        with self._tx() as (db, state):
            record = validate_request(record, state['manifest'])
            new, event, created = self._append(db, state, 'prepare', record, {'packet_id': 'packet_' + uuid4().hex})
            request = new['requests'][record['request_id']]
            result = {'request_id': record['request_id'], 'packet_id': request['packet']['binding']['packet_id'],
                'prepared_new': created, 'dispatch_claim': None, 'event_sequence': new['sequence'], 'controller_admission_verified': False}
        self._checkpoint('prepare_committed', record['request_id']); return result

    def export_packet(self, record):
        record = c.copy_json(record)
        with self._tx() as (db, state):
            new, _, created = self._append(db, state, 'export', record)
            request = new['requests'][record['request_id']]
            result = {'packet': deepcopy(request['packet']), 'packet_bytes': (_dump(request['packet']) + '\n').encode('utf-8'),
                'exported_new': created, 'historical_only': new['endpoint'] is not None or request['claim_id'] is not None,
                'event_sequence': new['sequence'], 'dispatch_authorized': False}
        self._checkpoint('export_committed', record['request_id']); return result

    def claim(self, record):
        record = c.copy_json(record)
        with self._tx() as (db, state):
            new, _, created = self._append(db, state, 'claim', record, {'claim_id': 'claim_' + uuid4().hex})
            request = new['requests'][record['request_id']]
            result = {'request_id': record['request_id'], 'dispatch_claim': request['claim_id'] if created else None,
                'historical_claim_id': request['claim_id'], 'event_sequence': new['sequence'], 'external_call_performed': False}
        self._checkpoint('claim_committed', record['request_id']); return result

    def bind(self, record):
        record = c.copy_json(record)
        with self._tx() as (db, state):
            new, _, changed = self._append(db, state, 'bind', record)
        self._checkpoint('bind_committed', record['request_id'])
        return {'bound_new': changed, 'event_sequence': new['sequence']}

    def ingest(self, record, raw_bytes=None):
        record = c.copy_json(record)
        _need(raw_bytes is None or type(raw_bytes) is bytes, 'explicit raw bytes required')
        with self._tx() as (db, state):
            new, event, created = self._ingest_tx(db, state, record, raw_bytes)
            result = next(o for o in new['observations'] if o['observation_id'] == record['observation_id'])
        self._checkpoint('observation_committed', record['observation_id'])
        return {'observation_id': record['observation_id'], 'recorded_new': created, 'classification': result['classification'],
            'historical_only': result['historical_only'], 'capture': deepcopy(result['capture']), 'event_sequence': new['sequence'],
            'charge_classification': result['charge_classification'], 'original_bytes_must_be_preserved': result['capture']['status'] != 'full',
            'adaptive_projection_performed': False, 'mathematical_truth_verified': False}

    def _ingest_tx(self, db, state, record, raw_bytes):
        _observation_fields(record, state['manifest'])
        _need(raw_bytes is None or type(raw_bytes) is bytes, 'explicit raw bytes required')
        existing = db.execute('SELECT payload FROM events WHERE operation_id=?', (record['operation_id'],)).fetchone()
        if existing:
            prior = _json(existing[0])
            _need(prior['kind'] == 'observe' and _equal(prior['data']['record'], record), 'observation operation conflict')
            _need(prior['data']['capture']['status'] in ('full', 'absent'), 'unarchived raw cannot be certified identical')
            return self._append(db, state, 'observe', record, {'capture': prior['data']['capture']}, raw_bytes)
        size = None if raw_bytes is None else len(raw_bytes)
        limits = state['manifest']['storage_limits']
        too_large = size is not None and (size > limits['max_raw_response_bytes'] or state['archive_bytes'] + size > limits['max_session_archive_bytes'])
        capture = {'status': 'storage_limit_exceeded' if too_large else 'absent' if size is None else 'full', 'original_byte_length': size}
        return self._append(db, state, 'observe', record, {'capture': capture}, None if too_large else raw_bytes)

    def record_capture_failure(self, record, byte_count, failure_kind):
        """Record a local source failure without inventing transport outcome.

        A filesystem caller may have an oversized or unreadable original. Only
        its explicit size observation is recorded; no prefix is called a full
        response. The source and any known bill remain the caller's records.
        """
        record = c.copy_json(record)
        _need(failure_kind in ('size_limit', 'unreadable'), 'explicit capture failure kind required')
        if byte_count is not None:
            c.integer(byte_count, 'original byte length', 0, 10**12)
        _need(failure_kind != 'size_limit' or (byte_count is not None and byte_count > 0),
              'size-limited source requires a known positive length')
        capture = {'status': failure_kind, 'original_byte_length': byte_count}
        with self._tx() as (db, state):
            _observation_fields(record, state['manifest'])
            new, _, created = self._append(db, state, 'observe', record, {'capture': capture}, None)
            result = next(o for o in new['observations'] if o['observation_id'] == record['observation_id'])
        self._checkpoint('capture_failure_committed', record['observation_id'])
        return {'observation_id': record['observation_id'], 'recorded_new': created,
                'classification': result['classification'], 'historical_only': result['historical_only'],
                'capture': deepcopy(result['capture']), 'event_sequence': new['sequence'],
                'charge_classification': result['charge_classification'],
                'original_bytes_must_be_preserved': True, 'raw_response_archived': False,
                'remote_shutdown_verified': False, 'redispatch_permitted': False,
                'adaptive_projection_performed': False, 'mathematical_truth_verified': False}

    def mark_unknown(self, record):
        _need(record.get('transport_status') == 'unknown', 'unknown observation required')
        return self.ingest(record, None)

    def record_charge(self, record):
        record = c.copy_json(record)
        with self._tx() as (db, state):
            new, _, created = self._append(db, state, 'charge', record)
        self._checkpoint('charge_committed', record['operation_id'])
        return {'recorded_new': created, 'event_sequence': new['sequence'], 'budgets': _budgets(new), 'adaptive_projection_performed': False}

    def close(self, record):
        record = c.copy_json(record)
        with self._tx() as (db, state):
            new, _, created = self._append(db, state, 'close', record)
        self._checkpoint('close_committed', record['operation_id'])
        return {'closed_new': created, 'endpoint': deepcopy(new['endpoint']), 'event_sequence': new['sequence']}

    def snapshot(self):
        with self._tx() as (db, state):
            return {'schema': seed.VERSION if state['manifest']['schema'] == seed.MANIFEST_SCHEMA else VERSION,
                    'audience': 'coordinator_only', **deepcopy(state),
                'events': [_json(r['payload']) for r in db.execute('SELECT * FROM events ORDER BY sequence')],
                'raw_observations': {r['observation_id']: r['raw'] for r in db.execute('SELECT * FROM observations')},
                'budgets': _budgets(state), 'adaptive_projection_performed': False,
                'completion_suffix_protected': False, 'external_calls_performed': 0, 'mathematical_truth_verified': False}

    def status(self):
        with self._tx() as (_, state):
            rows = list(state['requests'].values())
            return {'schema': seed.VERSION if state['manifest']['schema'] == seed.MANIFEST_SCHEMA else VERSION,
                    'audience': 'metadata_only', 'session_id': state['manifest']['session_id'],
                'event_sequence': state['sequence'], 'sealed': state['endpoint'] is not None,
                'counts': {'reserved_calls': len(rows), 'claims': sum(r['claim_id'] is not None for r in rows),
                    'host_bound_calls': sum(r['host_reference'] is not None for r in rows),
                    'terminal_calls': sum(r['phase'] in TERMINAL for r in rows), 'unknown_calls': sum(r['phase'] == 'unknown' for r in rows),
                    'observations': len(state['observations'])}, 'budgets': _budgets(state),
                'completion_suffix_protected': False, 'controller_admission_verified': False,
                'adaptive_projection_performed': False, 'external_calls_performed': 0, 'mathematical_truth_verified': False}
