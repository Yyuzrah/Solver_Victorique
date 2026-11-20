"""Prepare an original-problem loop config; never initialize or start a run.

The supplied authorization is a declared scope, not proof of identity, login or
permission. This command copies its original bytes and does not grant new scope.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import sys

VERSION = 'victorique.original_problem_prepare.v1'


def _dependencies():
    from . import loop
    from victorique import adaptive_contracts as c, adaptive_exchange as exchange
    from victorique.harness import codex_contract, codex_transport
    from victorique.harness_bridge import validate_adapter
    from victorique.harness.safety import checked_path, write_new
    return loop, c, exchange, codex_contract, codex_transport, validate_adapter, checked_path, write_new


def _read(path, cap=4 * 1024 * 1024):
    checked_path = _dependencies()[-2]
    path = checked_path(path)
    with path.open('rb') as stream:
        raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise ValueError('input exceeds bounded configuration size')
    return path, raw


def _json(raw):
    # Reuse the installed strict JSON reader (duplicate keys/non-finite rejected).
    return _dependencies()[3]._loads(raw.decode('utf-8', errors='strict'))


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def build_manifest(*, problem_raw, session_id, run_id, model, calls, deadline,
                   source_ref, provider_id, model_revision=None, mode="host_recorded", protocol=None):
    """Build the shared bounded native manifest for both supported transports."""
    _, c, exchange, *_ = _dependencies()
    protocol = protocol or {"protocol_id": "victorique.solve", "version": "v1"}
    model_request = {'provider_id': provider_id, 'model_id': model, 'revision': model_revision}
    limits = {
        'max_routes': 2, 'max_obligations_per_plan': 64, 'max_dag_depth': 32,
        'max_plan_revisions': 2, 'max_candidate_revisions': 8,
        'max_repairs_per_issue': 2, 'max_investigations_per_issue': 4,
        'max_critiques_per_candidate': 1, 'global_call_limit': calls, 'max_inflight': 1,
        # The shared schema reserves both solve gates. Standalone verification
        # can request just one review; its global/process call ceiling remains 1.
        'total_budget_units': max(calls, len(c.GATES)),
        'role_call_limits': {role: calls for role in c.ROLES},
        'role_budget_units': {role: calls for role in c.ROLES},
        'action_caps': {kind: 1 for kind in c.CALLS}, 'action_roles': dict(c.DEFAULT_ROLES),
        'required_gate_caps': {gate: 1 for gate in c.GATES},
        'stop_condition': {'deadline_utc': deadline}}
    manifest = {
        'schema': exchange.MANIFEST_SCHEMA, 'session_id': session_id, 'protocol': protocol,
        'mode': mode,
        'problem': {'artifact_id': run_id + '.problem', 'revision': 1,
                    'content': problem_raw.decode('utf-8', errors='strict')},
        'limits': limits,
        'role_bindings': {role: {'actor_id': run_id + '.' + role, 'model_request': dict(model_request),
                                'fresh_context': True} for role in c.ROLES},
        'implementation_versions': {'contracts': 'adaptive_contracts.v1', 'controller': 'bounded_rules_v2',
            'exchange': exchange.VERSION, 'session': 'victorique.adaptive_session.v2', 'templates': 'adaptive_roles.v2'},
        'storage_limits': {'max_packet_bytes': 4000000, 'max_decoded_response_bytes': 4000000,
                          'max_raw_response_bytes': 8 * 1024 * 1024, 'max_session_archive_bytes': 256 * 1024 * 1024},
        'budget_unit': {'unit_id': 'reserved_role_invocation',
            'meaning': 'one native reserved role invocation; not tokens or money', 'currency': None},
        'authorization_ref': {'scope': 'host_recorded original-problem run within supplied authorization',
                              'source_id': source_ref},
        'forbidden_host_references': []}
    return exchange.validate_manifest(manifest)


def prepare(*, problem_path, profile_path, authorization_path, session_id, run_id,
            provider_id, model, calls, deadline, output, model_revision=None,
            timeout_seconds=120):
    loop, c, exchange, contract, transport, validate_adapter, checked_path, write_new = _dependencies()
    here = checked_path(Path(__file__).absolute().parent)
    target = checked_path(output)
    if target.exists():
        raise ValueError('new output directory required; preparation never overwrites or resumes')
    c.identifier(session_id); c.identifier(run_id)
    c.integer(calls, 'shared invocation ceiling', 1, 100)
    c.need(c.utc(deadline) > datetime.now(timezone.utc), 'absolute deadline already expired')
    from .settings import check_seconds
    check_seconds(timeout_seconds, 'timeout_seconds')
    problem_file, problem_raw = _read(problem_path, 256000)
    profile_file, profile_raw = _read(profile_path)
    auth_file, auth_raw = _read(authorization_path)
    schema_file, schema_raw = _read(here / 'adaptive-output-schemas.v1.json')
    inputs = [problem_file, profile_file, auth_file, schema_file]
    c.need(len(set(inputs)) == len(inputs), 'problem/profile/authorization/schema/source paths must be distinct')
    c.need(not target.is_relative_to(here) and not here.is_relative_to(target),
           'prepared output must be disjoint from the installed source directory')
    c.need(all(not path.is_relative_to(target) for path in inputs), 'output contains an input path')
    profile, auth, schemas = _json(profile_raw), _json(auth_raw), _json(schema_raw)
    kind = 'codex_cli'
    adapter_type = transport.CodexCliTransport
    if profile.get('host') == 'claude-code':
        from victorique.harness import claude_contract as contract, claude_transport as transport
        kind = 'claude_cli'
        adapter_type = transport.ClaudeCliTransport
    # Pure installed checks only: no probe, transport constructor, store or process.
    transport._authorize({'profile': profile, 'authorization': auth})
    c.need(auth['session_id'] == session_id and auth['requested_model'] == model,
           'session/model differs from supplied authorization')
    c.need(auth['expires_at_utc'] == deadline, 'deadline differs from supplied authorization')
    c.need(calls <= auth['max_process_starts'], 'calls exceed supplied authorization')
    protocol = auth['protocol']
    if kind == 'claude_cli':
        contract.conservative_claude_argv(profile, {'type': 'object'}, model)
    else:
        contract.conservative_codex_argv(profile, {
            'workspace': target / 'future-workspace', 'schema_file': target / 'future-schema.json',
            'final_file': target / 'future-final.json'}, model)
    manifest = build_manifest(problem_raw=problem_raw, session_id=session_id, run_id=run_id,
        model=model, calls=calls, deadline=deadline, source_ref=auth['source_ref'],
        provider_id=provider_id, model_revision=model_revision, protocol=protocol)
    grant = {'schema': 'victorique.bridge_execution_grant.v1', 'grant_id': run_id + '.grant',
        'session_id': session_id, 'protocol': protocol, 'adapter_id': adapter_type.ADAPTER_ID,
        'adapter_version': transport.VERSION, 'allow_execution': auth['allow_execution'],
        'source_ref': auth['source_ref'], 'expires_at_utc': deadline}
    config = {'schema': loop.CONFIG, 'manifest': manifest,
        'transport': {'kind': kind, 'version': transport.VERSION, 'profile': profile,
            'authorization': auth, 'timeout_seconds': timeout_seconds, 'max_capture_bytes': 4 * 1024 * 1024},
        'execution_grant': grant, 'output_schemas_path': str(target / 'adaptive-output-schemas.v1.json'),
        'poll_seconds': .5}
    exchange.validate_manifest(manifest)
    loop.validate_config(config, schemas)
    validate_adapter({'adapter_id': adapter_type.ADAPTER_ID, 'version': transport.VERSION,
        'mode': 'host_recorded', 'can_start': True, 'can_observe_existing': True, 'model_execution': True,
        'fresh_context_required': True, 'raw_response_preserved': True}, manifest, grant)
    copies = {'problem.txt': (problem_file, problem_raw), 'public-profile.json': (profile_file, profile_raw),
        'authorization.json': (auth_file, auth_raw), 'adaptive-output-schemas.v1.json': (schema_file, schema_raw)}
    receipt = {'schema': VERSION, 'status': 'prepared_not_started', 'run_id': run_id,
        'config_path': str(target / 'config.json'), 'session_id': session_id,
        'global_calls': calls, 'total_invocation_units': calls, 'max_inflight': 1, 'deadline_utc': deadline,
        'input_copies': [{'source_path': str(path), 'copy_path': name, 'byte_length': len(raw)}
                         for name, (path, raw) in copies.items()],
        'unknown_usage': {'tokens': None, 'money': None, 'actual_model_revision': None},
        'native_stores_created': 0, 'claims_created': 0, 'model_calls': 0,
        'external_authorization_authenticated': False, 'host_login_verified': False,
        'note': 'Only binds the supplied declared scope. Starting requires an explicit separate loop init/run.'}
    # All semantic checks precede output creation; partial filesystem errors are retained.
    target.mkdir(parents=True, exist_ok=False)
    for name, (_, raw) in copies.items():
        write_new(target / name, raw)
    write_new(target / 'config.json', _bytes(config))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, help_text in (
        ('problem', 'absolute path to original UTF-8 problem text; no seed candidate'),
        ('profile', 'absolute path to existing public Codex profile JSON; not credentials'),
        ('authorization', 'absolute path to existing scoped Codex authorization JSON'),
        ('session-id', 'must match authorization.session_id'), ('run-id', 'new logical actor/problem identity prefix'),
        ('provider-id', 'requested provider identity; not an observed identity'),
        ('model', 'must match authorization.requested_model'),
        ('deadline', 'original absolute UTC deadline; must exactly match authorization expiry'),
        ('output', 'new absolute output directory, separate from installation')):
        parser.add_argument('--' + name, required=True, help=help_text)
    parser.add_argument('--calls', required=True, type=int, help='shared invocation ceiling, 1..100; no extra role pool')
    parser.add_argument('--model-revision', default=None, help='requested revision; omit when unknown')
    parser.add_argument('--timeout-seconds', type=float, default=120, help='per-process bound, .05..86400 (default 120)')
    args = parser.parse_args()
    result = prepare(problem_path=args.problem, profile_path=args.profile, authorization_path=args.authorization,
        session_id=args.session_id, run_id=args.run_id, provider_id=args.provider_id, model=args.model,
        calls=args.calls, deadline=args.deadline, output=args.output,
        model_revision=args.model_revision, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
