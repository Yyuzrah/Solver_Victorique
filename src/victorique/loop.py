"""Thin original-problem loop over the installed native session and bridge.

The native core owns decisions and budgets. The bridge alone claims prepared
requests. This module adds no action state machine or responsibility database.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import sys
import time

VERSION = 'victorique.original_problem_loop.v1'
CONFIG = 'victorique.original_problem_loop_config.v1'
# After a stop request, the longest wait for the worker processes of the calls in flight to end.
STOP_WAIT_SECONDS = 15
# Step results that wait for a call in flight, or for something that only a resume can change.
WAITING = ('pending_original_observation', 'native_wait')


def _native():
    from victorique import adaptive_contracts as c, adaptive_exchange as exchange
    from victorique.adaptive_session import AdaptiveSession
    from victorique.harness_bridge import AdaptiveHarnessBridge
    from victorique.harness import codex_transport
    from victorique.harness.safety import checked_path
    return c, exchange, AdaptiveSession, AdaptiveHarnessBridge, codex_transport, checked_path


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _live_transport(kind):
    if kind == 'claude_cli':
        from .harness import claude_transport
        return claude_transport, claude_transport.ClaudeCliTransport
    if kind == 'codex_cli':
        from .harness import codex_transport
        return codex_transport, codex_transport.CodexCliTransport
    raise ValueError('unsupported live transport')


def _read(path, limit=4 * 1024 * 1024):
    checked = _native()[-1](path)
    with checked.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('bounded loop configuration required')
    return raw


def _validate_cli_spec(spec, manifest, deadline, label):
    c, *_ = _native()
    transport, _ = _live_transport(spec.get('kind'))
    c.fields(spec, {'kind', 'version', 'profile', 'authorization', 'timeout_seconds', 'max_capture_bytes'}, label)
    c.need(spec['version'] == transport.VERSION and manifest['mode'] == 'host_recorded', 'same installed CLI transport version/mode required')
    c.need(spec['authorization']['expires_at_utc'] == deadline, 'transport/native original deadline differs')


def validate_config(value, schemas):
    c, exchange, _, _, transport, _ = _native()
    optional = {'bestofn'} if 'compare_candidates' in value.get('manifest', {}).get('limits', {}).get('action_caps', {}) else set()
    # A separate model service for the two checks (check_transport) is optional.
    routed = {'check_transport'} & set(value)
    c.fields(value, {'schema', 'manifest', 'transport', 'execution_grant', 'output_schemas_path', 'poll_seconds'} | optional | routed, 'original problem config')
    if optional:
        from .bestofn import validate_estimate
        validate_estimate(value['bestofn'], value['manifest']['limits']['global_call_limit'])
    c.need(value['schema'] == CONFIG, 'original problem config version')
    manifest = value['manifest']
    c.need(type(manifest) is dict and manifest.get('schema') == exchange.MANIFEST_SCHEMA
           and 'initial_input' not in manifest, 'ordinary original-problem manifest required; no seed import')
    deadline = manifest['limits']['stop_condition']['deadline_utc']
    c.need(deadline is not None, 'an original absolute deadline is required')
    c.utc(deadline)
    c.need(type(schemas) is dict and set(schemas) == set(manifest['limits']['action_caps']), 'all enabled native output schemas required')
    c.need(all(type(x) is dict and x.get('type') == 'object' for x in schemas.values()), 'native object output schemas required')
    c.need(type(value['poll_seconds']) in (int, float) and .05 <= value['poll_seconds'] <= 10, 'bounded observation interval')
    spec = value['transport']
    if spec.get('kind') in ('codex_cli', 'claude_cli'):
        transport, _ = _live_transport(spec['kind'])
        c.fields(spec, {'kind', 'version', 'profile', 'authorization', 'timeout_seconds', 'max_capture_bytes'}, 'CLI configuration')
        c.need(spec['version'] == transport.VERSION and manifest['mode'] == 'host_recorded', 'same installed CLI transport version/mode required')
        c.need(value['execution_grant'] is not None, 'original bridge execution grant required')
        c.need(spec['authorization']['expires_at_utc'] == deadline
               and value['execution_grant']['expires_at_utc'] == deadline, 'transport/bridge/native original deadline differs')
        if 'check_transport' in value:
            from .harness.routed_transport import ADAPTER_ID, VERSION as ROUTED_VERSION
            _validate_cli_spec(value['check_transport'], manifest, deadline, 'check service CLI configuration')
            c.need(value['execution_grant']['adapter_id'] == ADAPTER_ID
                   and value['execution_grant']['adapter_version'] == ROUTED_VERSION, 'routed execution grant required')
    else:
        c.need('check_transport' not in value, 'a separate check service needs a live CLI transport')
        c.fields(spec, {'kind', 'description'}, 'explicit INERT adapter configuration')
        c.need(spec['kind'] == 'injected_inert' and manifest['mode'] == 'synthetic'
               and value['execution_grant'] is None and spec['description'].get('model_execution') is False,
               'INERT injection cannot authorize a model')
    return value


class OriginalProblemLoop:
    core_type = None
    bridge_type = None

    @classmethod
    def create(cls, root, config_path, *, adapter=None):
        *_, checked_path = _native()
        target = checked_path(root)
        raw = _read(config_path); config = json.loads(raw)
        schema_raw = _read(config['output_schemas_path']); schemas = json.loads(schema_raw)
        validate_config(config, schemas)
        if target.exists():
            raise ValueError('new loop root required; use resume without changing frozen config')
        target.mkdir(parents=True)
        with (target / 'run-config.json').open('xb') as stream: stream.write(raw)
        with (target / 'role-output-schemas.json').open('xb') as stream: stream.write(schema_raw)
        return cls.open(target, config_path=config_path, adapter=adapter)

    @classmethod
    def open(cls, root, *, config_path=None, adapter=None):
        c, exchange, session, bridge, transport, checked_path = _native()
        session = cls.core_type or session
        bridge = cls.bridge_type or bridge
        self = cls(); self.root = checked_path(root)
        raw = _read(self.root / 'run-config.json')
        if config_path is not None:
            c.need(raw == _read(config_path), 'resume configuration differs from frozen original bytes')
        self.config = json.loads(raw)
        self.schemas = json.loads(_read(self.root / 'role-output-schemas.json'))
        validate_config(self.config, self.schemas)
        manifest = self.config['manifest']; core_root = self.root / 'native'
        if core_root.exists(): self.core = session.open(core_root)
        else: self.core = session.create(core_root, manifest)
        snapshot = self.core.snapshot()
        c.need(c.same_json(snapshot['exchange']['manifest'], manifest), 'frozen original native manifest differs')
        spec = self.config['transport']
        if spec['kind'] in ('codex_cli', 'claude_cli'):
            c.need(adapter is None, 'live loop uses the installed CLI transport only')
            self.adapter = self._live_adapter(spec, self.root / 'transport', snapshot)
            if 'check_transport' in self.config:
                # The two checks use their own model service; every other role uses the solver's.
                from .harness.routed_transport import RoutedTransport
                checks = self._live_adapter(self.config['check_transport'], self.root / 'check-transport', snapshot)
                self.adapter = RoutedTransport(self.adapter, checks)
        else:
            c.need(adapter is not None and c.same_json(adapter.describe(), spec['description']), 'explicit original INERT adapter required')
            self.adapter = adapter
        bridge_root = self.root / 'bridge'
        if bridge_root.exists(): self.bridge = bridge(bridge_root)
        else:
            c.need(not snapshot['exchange']['requests'], 'missing original bridge after native work; no recreation')
            self.bridge = bridge.create(bridge_root, core_root, self.adapter.describe(), execution_grant=self.config['execution_grant'])
        c.need(c.same_json(self.bridge.config['adapter'], self.adapter.describe())
               and c.same_json(self.bridge.config['execution_grant'], self.config['execution_grant']), 'original bridge binding differs')
        c.need(self.bridge.core.root == self.core.root, 'bridge belongs to another native session')
        self._watch_stop()
        return self

    def _live_adapter(self, spec, transport_root, snapshot):
        """Open (or, before any native work, create) one frozen CLI transport."""
        c, *_ = _native()
        _, adapter_type = _live_transport(spec['kind'])
        if transport_root.exists():
            adapter = adapter_type(transport_root)
        else:
            c.need(not snapshot['exchange']['requests'], 'missing live transport after native work; no recreation')
            adapter = adapter_type.create(transport_root, profile=spec['profile'], authorization=spec['authorization'],
                output_schemas=self.schemas, timeout_seconds=spec['timeout_seconds'], max_capture_bytes=spec['max_capture_bytes'])
        expected = {'profile': spec['profile'], 'authorization': spec['authorization'], 'output_schemas': self.schemas,
                    'timeout_seconds': spec['timeout_seconds'], 'max_capture_bytes': spec['max_capture_bytes']}
        c.need(all(c.same_json(adapter.config[key], value) for key, value in expected.items()), 'frozen live transport configuration differs')
        return adapter

    @property
    def timeout_retry_limit(self):
        """Repetitions allowed for each call that stopped at its per-call limit.

        Read from this run's invocation record; a run recorded without the
        setting (or a loop without a record) repeats nothing.
        """
        if not hasattr(self, '_timeout_retry_limit'):
            try:
                parameters = json.loads((self.root.parent / 'invocation.json').read_text(encoding='utf-8'))['parameters']
                value = parameters.get('timeout_retries', 0)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                value = 0
            self._timeout_retry_limit = value if type(value) is int and 0 <= value <= 3 else 0
        return self._timeout_retry_limit

    def _timeouts_to_retry(self, base, request_ids):
        """Settlement evidence when every listed unknown call may be sent again, else None.

        Each call must have a repetition left, the call budget must cover one new
        call per listed call, the run's deadline must not have passed, and the
        transport must show that each call's process stopped at its time limit.
        """
        from . import retries
        limits = base['manifest']['limits']
        if (not request_ids or any(retries.attempt(rid) >= self.timeout_retry_limit for rid in request_ids)
                or len(base['requests']) + len(request_ids) > limits['global_call_limit']
                or datetime.now(timezone.utc) >= datetime.fromisoformat(
                    limits['stop_condition']['deadline_utc'].replace('Z', '+00:00'))):
            return None
        evidence = [self.bridge.timeout_evidence(rid, self.adapter) for rid in request_ids]
        return None if any(item is None for item in evidence) else evidence

    def _retry_work(self, listing):
        """The legal proposal that repeats the latest request, when it was settled for a repetition."""
        from . import retries
        c, *_ = _native()
        base = self.core.snapshot()['exchange']
        if not base['requests']:
            return None
        rid = list(base['requests'])[-1]
        if not retries.settled(base, rid) or retries.superseded(base, rid):
            return None
        record = base['requests'][rid]['record']
        context = record['context']
        work = {'kind': record['kind'], 'route_id': record['route_id'], 'obligation_id': record['obligation_id'],
                'dependency_refs': record['dependency_refs'],
                'target_ref': None if context['target_candidate'] is None else c.candidate_ref(context['target_candidate']),
                'issue_id': None if context['selected_issue'] is None else context['selected_issue']['proposal']['issue_id'],
                'plan_ref': None if context['plan'] is None else {key: context['plan'][key] for key in ('plan_id', 'revision')}}
        match = next((proposal for proposal in listing['legal_action_listing']['actions']
                      if all(c.same_json(proposal[key], value) for key, value in work.items())), None)
        return None if match is None else (retries.next_id(rid), match)

    @property
    def stop_path(self):
        """The run's stop request file, RUN/stop-request.json (see victorique.stop_request)."""
        path = getattr(self, '_stop_path', None)
        if path is None:
            from .stop_request import FILE
            path = self.root.parent / FILE
        return path

    @stop_path.setter
    def stop_path(self, path):
        # An escalated full workflow uses the stop request of the run that contains it.
        self._stop_path = path
        self._watch_stop()

    def _watch_stop(self):
        """Give the bridge and the CLI transports this run's stop request file.

        The bridge then sends no call once the file exists, and the worker process
        of a call in flight ends the call when it appears.
        """
        from .harness.routed_transport import members
        self.bridge.stop_file = self.stop_path
        for _, member in members(self.adapter):
            if hasattr(member, 'stop_file'):
                member.stop_file = self.stop_path

    def stop_requested(self):
        from .stop_request import requested
        return requested(self.stop_path)

    def _check_stop(self):
        """Called before a model call is prepared or sent: raise StopRequested when the run's stop request exists."""
        if self.stop_requested():
            from .stop_request import StopRequested
            raise StopRequested(str(self.stop_path))

    def _end_calls_in_flight(self, wait_seconds=STOP_WAIT_SECONDS):
        """Record how each call in flight ended after a stop request; this never sends a call.

        The worker process of a live CLI call stops the CLI when the stop request
        appears, as it does at the per-call time limit. Wait for those workers, at
        most wait_seconds in all, then let the bridge record what each transport
        observed. A call recorded as unknown stays unknown and is not sent again.
        """
        _, exchange, *_ = _native()
        requests = self.core.snapshot()['exchange']['requests']
        jobs = {job['request_id']: job for job in self.bridge.status()['jobs']}
        live = self.config['transport']['kind'] in ('codex_cli', 'claude_cli')
        deadline = time.monotonic() + wait_seconds
        for rid, row in requests.items():
            if row['claim_id'] is None or row['phase'] in exchange.TERMINAL or rid not in jobs:
                continue
            try:
                if live and jobs[rid]['core_claim_id'] is not None:
                    request = self.bridge._request(self.bridge._job(rid))
                    while time.monotonic() < deadline:
                        capture = self.adapter.local_capture(request)
                        if not capture or capture.get('worker_finished') or capture.get('launch_error'):
                            break
                        time.sleep(.05)
                self.bridge.recover(rid, self.adapter)
            except (ValueError, OSError):
                # The run still ends; this call keeps its last recorded observation.
                continue

    def stop(self):
        """End the run after its stop request.

        No new call is sent; each call in flight is ended by its worker process and
        recorded, and the run ends with the reason stopped_by_user (outcome partial).
        Returns the endpoint step.
        """
        self._end_calls_in_flight()
        return self._finish_stopped()

    def _finish_stopped(self):
        from .stop_request import REASON
        current = self.core.snapshot()
        if current['endpoint'] is None:
            # The native local stop: the controller records the reason and freezes the current draft.
            base = current['exchange']
            self.core.close({'schema': 'victorique.adaptive_close.v1', 'session_id': self.config['manifest']['session_id'],
                             'operation_id': 'loop.stop.' + str(base['sequence']),
                             'expected_event_sequence': base['sequence'], 'reason': REASON})
            current = self.core.snapshot()
        endpoint = current['endpoint']
        return {'phase': 'native_endpoint', 'reason': (endpoint.get('reducer_endpoint') or {}).get('reason') or endpoint.get('reason'),
                'status': self.status()}

    def _control(self, snapshot, label, **values):
        return {'session_id': self.config['manifest']['session_id'],
                'operation_id': 'loop.' + label + '.' + str(snapshot['journal_sequence']),
                'expected_session_sequence': snapshot['journal_sequence'],
                'expected_state_revision': snapshot['state']['state_revision'], **values}

    def status(self):
        _, exchange, *_ = _native()
        snapshot = self.core.snapshot(); endpoint = snapshot['endpoint']
        native_endpoint = None if endpoint is None else endpoint.get('reducer_endpoint')
        decision = None if native_endpoint is None else native_endpoint.get('decision')
        outcome = None if endpoint is None else 'complete' if decision == 'complete' else 'abstain' if decision == 'abstain' else 'partial'
        pending = [{'request_id': row['request_id'], 'phase': row['phase'], 'claim_id': row['claim_id'], 'host_reference': row['host_reference']}
                   for row in snapshot['exchange']['requests'].values() if row['phase'] not in exchange.TERMINAL]
        return {'schema': VERSION, 'native_outcome': outcome, 'endpoint': endpoint, 'pending_original_response_responsibilities': pending,
                'native': self.core.status(), 'bridge': self.bridge.status(), 'mathematical_truth_verified': False,
                'independent_budget_grants': 0, 'seeded_input_imported': False}

    def _handoff(self, snapshot, listing):
        """One source-bound native handoff, then obtain a fresh native decision."""
        c, *_ = _native(); handoffs = snapshot['handoffs']; seq = snapshot['journal_sequence']
        for item in listing['legal_action_listing']['rejected']:
            proposal = item['action']; reason = item['reason']
            if reason == 'feedback_not_released_and_received':
                for receipt in proposal['feedback_receipt_ids']:
                    row = next(x for x in snapshot['state']['feedback'] if x['receipt_id'] == receipt)
                    release = next((x['record'] for x in handoffs if x['kind'] == 'release_feedback' and x['record']['receipt_id'] == receipt), None)
                    if release is None:
                        # These are the native action kinds that consume critique/investigation feedback.
                        roles = sorted({self.config['manifest']['limits']['action_roles'][kind] for kind in ('investigate', 'repair_obligation')})
                        c.need(proposal['role'] in roles, 'unexpected native feedback recipient')
                        record = self._control(snapshot, 'release_feedback', release_id='loop_release_' + str(seq), receipt_id=receipt,
                                               target_ref=proposal['target_ref'], allowed_roles=roles)
                        return self.core.release_feedback(record)
                    if proposal['role'] not in row['received_by']:
                        record = self._control(snapshot, 'receive_feedback', read_id='loop_read_' + str(seq), release_id=release['release_id'],
                            recipient_role=proposal['role'], recipient_actor_id=self.config['manifest']['role_bindings'][proposal['role']]['actor_id'],
                            target_ref=proposal['target_ref'])
                        return self.core.receive_feedback(record)
            if reason == 'planning_evidence_not_released_and_received':
                for source in listing['planning_evidence_sources']:
                    release = next((x['record'] for x in handoffs if x['kind'] == 'release_planning_evidence'
                                    and x['record']['receipt_id'] == source['receipt_id']), None)
                    if release is None:
                        record = self._control(snapshot, 'release_planning', release_id='loop_plan_release_' + str(seq),
                            receipt_id=source['receipt_id'], plan_ref=source['plan_ref'], target_ref=source['target_ref'])
                        return self.core.release_planning_evidence(record)
                    received = any(x['kind'] == 'receive_planning_evidence' and x['record']['release_id'] == release['release_id'] for x in handoffs)
                    if not received:
                        record = self._control(snapshot, 'receive_planning', read_id='loop_plan_read_' + str(seq), release_id=release['release_id'],
                            recipient_role='planner', recipient_actor_id=self.config['manifest']['role_bindings']['planner']['actor_id'],
                            plan_ref=source['plan_ref'], target_ref=source['target_ref'])
                        return self.core.receive_planning_evidence(record)
        return None

    def step(self):
        c, exchange, *_ = _native()
        snapshot = self.core.snapshot()
        jobs = {job['request_id']: job for job in self.bridge.status()['jobs']}
        pending = [row for row in snapshot['exchange']['requests'].values() if row['phase'] not in exchange.TERMINAL]
        for row in pending:
            rid = row['request_id']; job = jobs.get(rid)
            if snapshot['endpoint'] is None and row['claim_id'] is None and (job is None or job['phase'] == 'prepared'):
                if c.utc(datetime.now(timezone.utc).isoformat()) >= c.utc(self.config['manifest']['limits']['stop_condition']['deadline_utc']):
                    listing = self.core.propose(); decision = listing['decision']; current = self.core.snapshot()
                    c.need(decision['decision'] == 'stop' and current['journal_sequence'] == listing['session_sequence']
                           and current['state']['state_revision'] == listing['state_revision'], 'original deadline stop changed')
                    self.core.finalize(self._control(current, 'finalize', endpoint_id='loop_endpoint_' + str(current['journal_sequence']),
                        decision=decision['decision'], reason=decision['reason']))
                    return {'phase': 'native_endpoint', 'status': self.status()}
                self._check_stop()
                return {'phase': 'original_prepared_dispatched', 'request_id': rid, 'result': self.bridge.dispatch(rid, self.adapter)}
            if job is not None:
                result = self.bridge.recover(rid, self.adapter)
                current = self.core.snapshot()
                if current['exchange']['requests'][rid]['phase'] in exchange.TERMINAL:
                    return {'phase': 'original_response_observed', 'request_id': rid, 'result': result}
                if (current['exchange']['requests'][rid]['phase'] == 'unknown'
                        and self._timeouts_to_retry(current['exchange'], [rid]) is not None):
                    # The call stopped at its time limit: settle it so the same work is sent again.
                    from . import retries
                    settled = self.bridge.settle_timeout(rid, self.adapter)
                    return {'phase': 'call_timed_out', 'request_id': rid, 'result': settled,
                            'retry_request_id': retries.next_id(rid)}
            else:
                return {'phase': 'blocked_original_responsibility', 'request_id': rid, 'reason': 'native claim has no original bridge job; no replacement'}
            # Deadline finalization is the native stop decision; obligations remain retained.
            listing = self.core.propose()
            if snapshot['endpoint'] is None and listing['decision']['decision'] == 'stop':
                current = self.core.snapshot(); decision = listing['decision']
                record = self._control(current, 'finalize', endpoint_id='loop_endpoint_' + str(current['journal_sequence']),
                    decision=decision['decision'], reason=decision['reason'])
                self.core.finalize(record)
                return {'phase': 'native_endpoint', 'status': self.status()}
            if self.config['transport']['kind'] in ('codex_cli', 'claude_cli'):
                request = self.bridge._request(self.bridge._job(rid))
                capture = self.adapter.local_capture(request)
                if capture and capture.get('worker_finished'):
                    # Completion can race with recover's earlier pending read.
                    # Read the original response once more before classifying it.
                    result = self.bridge.recover(rid, self.adapter)
                    if self.core.snapshot()['exchange']['requests'][rid]['phase'] in exchange.TERMINAL:
                        return {'phase': 'original_response_observed', 'request_id': rid, 'result': result}
                    return {'phase': 'blocked_original_result', 'request_id': rid,
                            'reason': 'local process finished with unresolved response; no replacement dispatch'}
            return {'phase': 'pending_original_observation', 'request_id': rid, 'status': self.status()}
        if snapshot['endpoint'] is not None:
            return {'phase': 'native_endpoint', 'status': self.status()}
        listing = self.core.propose(); decision = listing['decision']
        # The exact public proposal is passed unchanged to native prepare/CAS.
        if decision['decision'] == 'propose':
            sequence = listing['session_sequence']
            request_id, action = 'loop_request_' + str(sequence), decision['action']
            retry = self._retry_work(listing)
            if retry is not None:
                # A settled timed-out call is sent again while the controller still lists the same work.
                request_id, action = retry
            self._check_stop()
            record = {'session_id': self.config['manifest']['session_id'], 'operation_id': 'loop.prepare.' + str(sequence),
                'expected_session_sequence': sequence, 'expected_state_revision': listing['state_revision'],
                'request_id': request_id, 'logical_action_id': 'loop_action_' + str(sequence),
                'action_id': 'loop_action_' + str(sequence), 'selected_proposal': action}
            result = self.core.prepare(record)
            # No export or claim in this layer. The next step gives the original request to bridge.
            return {'phase': 'native_prepared', 'request_id': result['request_id'], 'kind': action['kind']}
        if decision['decision'] == 'wait' and decision['reason'] == 'feedback_handoff_pending':
            current = self.core.snapshot()
            c.need(current['journal_sequence'] == listing['session_sequence']
                   and current['state']['state_revision'] == listing['state_revision'], 'native handoff decision changed')
            result = self._handoff(current, listing)
            return {'phase': 'native_handoff' if result is not None else 'blocked_native_handoff', 'result': result}
        if decision['decision'] in ('complete', 'abstain', 'stop'):
            current = self.core.snapshot()
            c.need(current['journal_sequence'] == listing['session_sequence']
                   and current['state']['state_revision'] == listing['state_revision'], 'native decision changed before finalization')
            record = self._control(current, 'finalize', endpoint_id='loop_endpoint_' + str(current['journal_sequence']),
                decision=decision['decision'], reason=decision['reason'])
            self.core.finalize(record)
            return {'phase': 'native_endpoint', 'status': self.status()}
        return {'phase': 'native_wait', 'decision': decision}

    def run(self, *, max_steps=1000, on_step=None):
        from .stop_request import StopRequested
        if type(max_steps) is not int or not 1 <= max_steps <= 10000:
            raise ValueError('bounded local step count required')
        for index in range(max_steps):
            # Every policy's step raises StopRequested before it would prepare or send a model
            # call once the run's stop request exists; a step that only waits is followed by the
            # same check. A step that ends the run without another call still ends it as before.
            try:
                result = self.step()
            except StopRequested:
                result = self.stop()
            else:
                if ((result['phase'] in WAITING or result['phase'].startswith('blocked_'))
                        and self.stop_requested()):
                    result = self.stop()
            if on_step is not None: on_step({'step': index + 1, **result})
            if result['phase'] == 'native_endpoint': return result
            if result['phase'].startswith('blocked_'): return result
            if result['phase'] in WAITING:
                time.sleep(self.config['poll_seconds'])
        return {'phase': 'local_observation_bound', 'status': self.status(), 'native_endpoint_created_by_this_bound': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'status', 'step', 'run'))
    parser.add_argument('--root', required=True); parser.add_argument('--config')
    parser.add_argument('--max-steps', type=int, default=1000)
    args = parser.parse_args()
    if args.command == 'init':
        if args.config is None: parser.error('init requires --config')
        result = OriginalProblemLoop.create(args.root, args.config).status()
    else:
        loop = OriginalProblemLoop.open(args.root, config_path=args.config)
        result = loop.run(max_steps=args.max_steps, on_step=lambda row: print(_json(row), flush=True)) if args.command == 'run' else getattr(loop, args.command)()
    print(_json(result), flush=True)


if __name__ == '__main__':
    main()
