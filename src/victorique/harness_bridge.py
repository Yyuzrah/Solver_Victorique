"""Claim-owned adaptive/environment bridge; transport is replaceable, not a gate.

No live host adapter is supplied here. HarnessInertAdapter executes only the
environment's fixed INERT process and supplies explicitly scripted test bytes.
Core requests own all budget liability; this bridge has no budget-grant API.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Protocol
from uuid import uuid4

from . import adaptive_contracts as c
from . import adaptive_exchange as exchange
from . import stop_request
from .adaptive_session import AdaptiveSession
from .harness.safety import _check_leaf, _check_path


VERSION = 'victorique.harness_bridge.v1'
OBSERVATION = 'victorique.bridge_observation.v1'
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
TERMINAL = {'completed', 'failed'}
# The reason recorded for a call whose own process stopped at its per-call time limit.
LOCAL_TIMEOUT = exchange.LOCAL_TIMEOUT


class BridgeError(ValueError):
    pass


def need(value, message):
    if not value: raise BridgeError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def clone(value):
    return c.copy_json(value)


@dataclass(frozen=True)
class DispatchRequest:
    job_id: str
    core_claim_id: str
    packet: dict
    packet_bytes: bytes
    mode: str


@dataclass(frozen=True)
class TransportObservation:
    """Raw bytes are never reconstructed from an output object by the bridge."""
    metadata: dict
    raw_response: bytes | None
    raw_transport: bytes | None


class Transport(Protocol):
    def describe(self) -> dict: ...
    def start_once(self, request: DispatchRequest) -> TransportObservation: ...
    def observe_existing(self, request: DispatchRequest) -> TransportObservation | None: ...


def validate_adapter(description, manifest, grant=None, *, for_execution=True):
    d = clone(description)
    c.fields(d, {'adapter_id', 'version', 'mode', 'can_start', 'can_observe_existing',
                 'model_execution', 'fresh_context_required', 'raw_response_preserved'}, 'bridge transport')
    c.identifier(d['adapter_id']); c.identifier(d['version'])
    need(d['mode'] == manifest['mode'], 'adapter/core mode differs')
    for key in ('can_start', 'can_observe_existing', 'model_execution', 'fresh_context_required', 'raw_response_preserved'):
        need(type(d[key]) is bool, 'typed adapter capability required')
    need(d['can_start'] and d['fresh_context_required'] and d['raw_response_preserved'], 'required adapter capabilities absent')
    if d['mode'] == 'synthetic':
        need(not d['model_execution'] and grant is None, 'synthetic execution cannot authorize models')
    else:
        need(d['mode'] == 'host_recorded' and d['model_execution'], 'real adapter is unavailable')
        g = clone(grant)
        c.fields(g, {'schema', 'grant_id', 'session_id', 'protocol', 'adapter_id', 'adapter_version',
                     'allow_execution', 'source_ref', 'expires_at_utc'}, 'explicit execution grant')
        need(g['schema'] == 'victorique.bridge_execution_grant.v1' and g['allow_execution'] is True,
             'explicit execution grant required')
        c.identifier(g['grant_id']); c.text(g['source_ref'], 'authorization source', 4000)
        need(g['session_id'] == manifest['session_id'] and c.same_json(g['protocol'], manifest['protocol'])
             and g['adapter_id'] == d['adapter_id'] and g['adapter_version'] == d['version'], 'grant scope differs')
        expires = c.utc(g['expires_at_utc'])
        if for_execution: need(expires > c.utc(now()), 'execution grant expired')
    return d


class AdaptiveHarnessBridge:
    """One bridge job per already-reserved core request; cross-DB intent replay.

The caller prepares core work through AdaptiveSession first. This class exports
and claims that exact work. It never accepts a caller-provided claim as fresh.
Core public bind/ingest/close APIs remain the only domain mutation path.
"""

    core_type = AdaptiveSession
    # The run's stop request file (victorique.stop_request), set by the loop. Once
    # it exists, dispatch claims and sends nothing.
    stop_file = None

    def __init__(self, root):
        self.root = exchange._root(root)
        self.path = self.root / 'bridge.sqlite3'
        need(self.path.is_file(), 'bridge is not initialized')
        with self._db() as db:
            self.config = json.loads(db.execute('SELECT value FROM meta WHERE key="config"').fetchone()[0])
        need(self.config['schema'] == VERSION, 'bridge schema differs')
        self.core = self.core_type.open(self.config['core_root'])
        need(c.same_json(self.core.snapshot()['exchange']['manifest'], self.config['manifest']), 'core manifest differs')

    @classmethod
    def create(cls, root, core_root, adapter_description, *, execution_grant=None):
        target = exchange._root(root); core = cls.core_type.open(core_root)
        manifest = core.snapshot()['exchange']['manifest']
        descriptor = validate_adapter(adapter_description, manifest, execution_grant)
        need(not target.exists() and target.parent.is_dir(), 'new independent bridge directory required')
        target.mkdir()
        config = {'schema': VERSION, 'core_root': str(core.root), 'manifest': manifest,
                  'adapter': descriptor, 'execution_grant': execution_grant, 'created_at_utc': now()}
        with closing(sqlite3.connect(target / 'bridge.sqlite3')) as db:
            db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                'CREATE TABLE jobs(request_id TEXT PRIMARY KEY,job_id TEXT UNIQUE NOT NULL,body TEXT NOT NULL);'
                'CREATE TABLE operations(key TEXT PRIMARY KEY,method TEXT NOT NULL,record TEXT NOT NULL,result TEXT);'
                'CREATE TABLE observations(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,metadata TEXT NOT NULL,'
                'response BLOB,transport BLOB,capture TEXT NOT NULL,result TEXT);'
                'CREATE TABLE audit(sequence INTEGER PRIMARY KEY,event TEXT NOT NULL,detail TEXT NOT NULL);')
            db.execute('INSERT INTO meta VALUES ("config",?)', (encode(config),))
            db.execute('INSERT INTO meta VALUES ("closed","false")')
            db.commit()
        return cls(target)

    @contextmanager
    def _db(self, write=False):
        _check_path(self.path)  # the database file and each of its parents
        for suffix in ('-journal', '-wal', '-shm'):
            _check_leaf(Path(str(self.path) + suffix), transient_sqlite_sidecar=True)
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            if write: db.execute('BEGIN IMMEDIATE')
            yield db
            if write: db.execute('COMMIT')
        except BaseException:
            if db.in_transaction: db.execute('ROLLBACK')
            raise
        finally: db.close()

    def _checkpoint(self, stage, request_id):
        """Test fault hook; no production callback or manifest-loaded code."""

    def _event(self, db, kind, detail):
        db.execute('INSERT INTO audit(event,detail) VALUES (?,?)', (kind, encode({'at_utc': now(), **detail})))

    def _job(self, request_id):
        with self._db() as db:
            row = db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()
        need(row is not None, 'unknown bridge request')
        return json.loads(row[0])

    def _save(self, job):
        with self._db(True) as db:
            db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(job), job['request_id']))

    @contextmanager
    def _core_serial(self):
        """Serialize local CAS intents, never hold this across transport calls."""
        path = self.root / 'core-operation.lock'; _check_path(path)
        with path.open('a+b') as handle:
            if path.stat().st_size == 0: handle.write(b'0'); handle.flush()
            handle.seek(0); started = time.monotonic()
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    need(time.monotonic() - started < 20, 'core operation is busy')
                    time.sleep(.02)
            try: yield
            finally:
                handle.seek(0)
                if os.name == 'nt': msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _source(self, request_id):
        snapshot = self.core.snapshot()
        need(c.same_json(snapshot['exchange']['manifest'], self.config['manifest']), 'core manifest changed')
        row = snapshot['exchange']['requests'].get(request_id)
        need(row is not None, 'core must reserve request before bridge use')
        return row

    def _base(self, kind):
        status = self.core.status()
        return {'schema': 'victorique.adaptive_' + kind + '.v1', 'session_id': status['session_id'],
                'operation_id': 'bridge_' + uuid4().hex,
                'expected_event_sequence': None if kind in ('bind', 'observation', 'charge') else status['event_sequence']}

    def _operation(self, key, method, factory, raw=None, capture=None):
        with self._core_serial():
            return self._operation_serial(key, method, factory, raw, capture)

    def _operation_serial(self, key, method, factory, raw, capture):
        with self._db(True) as db:
            prior = db.execute('SELECT * FROM operations WHERE key=?', (key,)).fetchone()
            fresh = prior is None
            if fresh:
                record = factory()
                db.execute('INSERT INTO operations VALUES (?,?,?,NULL)', (key, method, encode(record)))
            else:
                need(prior['method'] == method, 'operation method differs')
                record = json.loads(prior['record'])
        if method == 'ingest': result = self.core.ingest(record, raw)
        elif method == 'record_capture_failure': result = self.core.record_capture_failure(record, *capture)
        else: result = getattr(self.core, method)(record)
        self._checkpoint('core_' + method + '_committed', record.get('request_id'))
        saved = {k: v for k, v in result.items() if k not in ('packet', 'packet_bytes')}
        with self._db(True) as db:
            db.execute('UPDATE operations SET result=? WHERE key=?', (encode(saved), key))
        return result, fresh

    def prepare(self, request_id):
        source = self._source(request_id)
        with self._db(True) as db:
            row = db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            if row is None:
                need(db.execute('SELECT value FROM meta WHERE key="closed"').fetchone()[0] == 'false', 'bridge closed')
                need(source['claim_id'] is None, 'already claimed outside this bridge')
                job = {'request_id': request_id, 'job_id': 'bridge_' + uuid4().hex, 'phase': 'prepared',
                    'core_claim_id': None, 'host_reference': None, 'packet': source['packet'],
                    'first_terminal_observation': None, 'created_at_utc': now()}
                db.execute('INSERT INTO jobs VALUES (?,?,?)', (request_id, job['job_id'], encode(job)))
                self._event(db, 'prepared_core_reservation', {'request_id': request_id})
            else:
                job = json.loads(row[0])
                need(c.same_json(job['packet'], source['packet']), 'frozen packet differs')
        # Idempotent export is local; it grants no permission to execute.
        exported, _ = self._operation(request_id + ':export', 'export_packet',
            lambda: self._base('export') | {'request_id': request_id})
        need(c.same_json(exported['packet'], job['packet']), 'export differs from reserved packet')
        return self.job_status(request_id)

    def _request(self, job):
        need(job['core_claim_id'] is not None, 'core claim missing')
        source = self._source(job['request_id'])
        need(source['claim_id'] == job['core_claim_id'] and c.same_json(source['packet'], job['packet']), 'core claim/packet ownership differs')
        if job['host_reference'] is not None:
            need(source['host_reference'] == job['host_reference'], 'host binding changed')
        packet = clone(job['packet'])
        return DispatchRequest(job['job_id'], job['core_claim_id'], packet,
            (exchange._dump(packet) + '\n').encode('utf-8'), self.config['manifest']['mode'])

    def _adapter(self, adapter, *, for_execution=True):
        described = validate_adapter(adapter.describe(), self.config['manifest'], self.config['execution_grant'],
                                     for_execution=for_execution)
        need(c.same_json(described, self.config['adapter']), 'adapter version/capabilities changed')

    def dispatch(self, request_id, adapter):
        if stop_request.requested(self.stop_file):
            # Before export and claim: the request stays prepared and is never sent.
            raise stop_request.StopRequested(str(self.stop_file))
        self._adapter(adapter)
        self.prepare(request_id)
        with self._db(True) as db:
            job = json.loads(db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()[0])
            if job['phase'] != 'prepared':
                return {'dispatched': False, 'request_id': request_id, 'phase': job['phase']}
            need(db.execute('SELECT value FROM meta WHERE key="closed"').fetchone()[0] == 'false', 'bridge closed')
            job['phase'] = 'claim_intent'
            db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(job), request_id))
        self._checkpoint('before_core_claim', request_id)
        claimed, fresh_operation = self._operation(request_id + ':claim', 'claim',
            lambda: self._base('claim') | {'request_id': request_id, 'packet_id': job['packet']['binding']['packet_id']})
        with self._db(True) as db:
            current = json.loads(db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()[0])
            current['core_claim_id'] = claimed['historical_claim_id']
            if current['phase'] == 'claim_intent': current['phase'] = 'claimed'
            job = current
            db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(job), request_id))
        if not fresh_operation or claimed['dispatch_claim'] is None:
            return self._unknown(request_id, 'historical_claim_never_authorizes_transport')
        with self._db(True) as db:
            job = json.loads(db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()[0])
            if db.execute('SELECT value FROM meta WHERE key="closed"').fetchone()[0] != 'false' or job['phase'] != 'claimed':
                return {'dispatched': False, 'phase': 'claimed', 'reason': 'closed_before_transport'}
            job['phase'] = 'entered'
            db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(job), request_id))
            self._event(db, 'transport_entered', {'request_id': request_id, 'core_claim_id': job['core_claim_id']})
        self._checkpoint('transport_fenced', request_id)
        try:
            observation = adapter.start_once(self._request(job))
        except Exception as exc:
            with self._db(True) as db:
                self._event(db, 'transport_exception', {'request_id': request_id, 'exception_type': type(exc).__name__})
            return {'dispatched': True, **self._unknown(request_id, 'transport_exception')}
        self._checkpoint('transport_returned_before_archive', request_id)
        return {'dispatched': True, **self.receive(request_id, observation)}

    def _archive(self, request_id, observation, settlement=None):
        need(isinstance(observation, TransportObservation), 'typed transport observation required')
        need(type(observation.raw_response) is bytes or observation.raw_response is None, 'original response bytes required')
        need(type(observation.raw_transport) is bytes or observation.raw_transport is None, 'original transport bytes required')
        metadata_capture = 'full'
        try:
            metadata = encode(observation.metadata)
            if len(metadata.encode('utf-8')) > 1_000_000: raise ValueError('metadata limit')
        except (ValueError, TypeError, RecursionError):
            # This diagnostic is not the original metadata. Available response
            # and carrier bytes are still archived before validation fails.
            metadata_capture = 'unserializable_or_over_limit'
            metadata = encode({'bridge_metadata_capture': metadata_capture,
                               'original_type': type(observation.metadata).__name__})
        mid = observation.metadata.get('observation_id') if type(observation.metadata) is dict and metadata_capture == 'full' else None
        key = mid if type(mid) is str and 0 < len(mid) <= 120 else 'unbound_' + uuid4().hex
        raw, carrier = observation.raw_response, observation.raw_transport
        cap = min(self.config['manifest']['storage_limits']['max_raw_response_bytes'], 16 * 1024 * 1024)
        capture = {'metadata': metadata_capture, 'response_bytes': None if raw is None else len(raw), 'transport_bytes': None if carrier is None else len(carrier),
                   'response': 'absent' if raw is None else 'full', 'transport': 'absent' if carrier is None else 'full'}
        if settlement is not None:
            capture['settlement'] = settlement
        if raw is not None and len(raw) > cap: capture['response'] = 'size_limit'; raw = raw[:4096]
        if carrier is not None and len(carrier) > 1024 * 1024: capture['transport'] = 'size_limit'; carrier = carrier[:4096]
        with self._db(True) as db:
            existing = db.execute('SELECT * FROM observations WHERE id=?', (key,)).fetchone()
            if existing is not None:
                need(existing['request_id'] == request_id and existing['metadata'] == metadata
                     and existing['response'] == raw and existing['transport'] == carrier
                     and existing['capture'] == encode(capture), 'observation ID reused with different original data')
            else:
                used = db.execute('SELECT coalesce(sum(coalesce(length(response),0)+coalesce(length(transport),0)),0) FROM observations').fetchone()[0]
                need(used + len(raw or b'') + len(carrier or b'') <= MAX_ARCHIVE_BYTES, 'bridge archive capacity reached; original must be retained externally')
                db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,NULL)',
                           (key, request_id, metadata, raw, carrier, encode(capture)))
                self._event(db, 'raw_archived', {'request_id': request_id, 'observation_id': key, 'capture': capture})
        self._checkpoint('raw_archived', request_id)
        return key

    def _validate(self, job, metadata, reason=None):
        m = clone(metadata)
        c.fields(m, {'schema', 'observation_id', 'job_id', 'core_claim_id', 'packet_id', 'adapter_id',
            'adapter_version', 'mode', 'status', 'terminal_observed', 'host_reference',
            'observed_at_utc', 'source_event_id', 'source_line', 'model_observation', 'charge_observation'}, 'transport observation')
        c.identifier(m['observation_id'])
        need(m['schema'] == OBSERVATION and m['job_id'] == job['job_id'] and m['core_claim_id'] == job['core_claim_id']
             and m['packet_id'] == job['packet']['binding']['packet_id'], 'receipt belongs to another claim/packet')
        descriptor = self.config['adapter']
        need(m['adapter_id'] == descriptor['adapter_id'] and m['adapter_version'] == descriptor['version']
             and m['mode'] == descriptor['mode'], 'receipt adapter/mode differs')
        need(type(m['status']) is str and m['status'] in TERMINAL | {'unknown'}, 'transport status differs')
        need(type(m['terminal_observed']) is bool and m['terminal_observed'] == (m['status'] in TERMINAL), 'actual terminal declaration required')
        if m['status'] in TERMINAL: need(m['host_reference'] is not None, 'terminal result has no actual host reference')
        record = self._observation_record(job, m, reason)
        exchange._observation_fields(record, self.config['manifest'])
        return m

    def _observation_record(self, job, m, reason=None):
        return self._base('observation') | {'observation_id': m['observation_id'], 'request_id': job['request_id'],
            'claim_id': job['core_claim_id'], 'host_reference': m['host_reference'],
            'transport_status': {'completed': 'returned', 'failed': 'failed', 'unknown': 'unknown'}[m['status']],
            'source_event_id': m['source_event_id'], 'source_line': m['source_line'],
            'external_observed_at_utc': m['observed_at_utc'], 'model_observation': m['model_observation'],
            'charge_observation': m['charge_observation'], 'reason': reason}

    def receive(self, request_id, observation):
        self._job(request_id)
        key = self._archive(request_id, observation)  # raw first, even wrong ownership
        return self._project(request_id, key)

    def _project(self, request_id, key):
        job = self._job(request_id); self._request(job)
        with self._db() as db: row = dict(db.execute('SELECT * FROM observations WHERE id=?', (key,)).fetchone())
        need(row['request_id'] == request_id, 'observation request differs')
        capture = json.loads(row['capture'])
        # A settlement keeps its reason through recovery: it is stored with the archived observation.
        reason = capture.get('settlement')
        try:
            m = self._validate(job, json.loads(row['metadata']), reason)
            need(reason is None or (m['status'] == 'failed' and m['host_reference'] is not None),
                 'a timeout settlement is a failed call with a host reference')
            source = self._source(request_id)
            need(m['host_reference'] is None or source['host_reference'] in (None, m['host_reference']),
                 'late receipt cannot change host identity')
        except (ValueError, TypeError, KeyError) as exc:
            with self._db(True) as db:
                db.execute('UPDATE observations SET result=? WHERE id=?', (encode({'classification': 'quarantined_metadata', 'error_type': type(exc).__name__}), key))
            return self._unknown(request_id, 'quarantined_metadata')
        if m['host_reference'] is not None:
            try:
                self._operation(request_id + ':bind:' + key, 'bind', lambda: self._base('bind') |
                    {'request_id': request_id, 'claim_id': job['core_claim_id'], 'host_reference': m['host_reference']})
            except ValueError as exc:
                with self._db(True) as db:
                    db.execute('UPDATE observations SET result=? WHERE id=?',
                               (encode({'classification': 'quarantined_binding', 'error_type': type(exc).__name__}), key))
                return self._unknown(request_id, 'quarantined_binding')
            with self._db(True) as db:
                current = json.loads(db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()[0])
                current['host_reference'] = m['host_reference']
                db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(current), request_id))
            job = current
        if m['status'] == 'completed' and capture['response'] != 'full':
            failure = 'size_limit' if capture['response'] == 'size_limit' else 'unreadable'
            result, _ = self._operation('observation:' + key, 'record_capture_failure',
                lambda: self._observation_record(job, m), capture=(capture['response_bytes'], failure))
        else:
            result, _ = self._operation('observation:' + key, 'ingest',
                lambda: self._observation_record(job, m, reason), raw=row['response'] if capture['response'] == 'full' else None)
        need(not result['classification'].startswith('quarantined'), 'core quarantined the bound receipt')
        with self._db(True) as db:
            current = json.loads(db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()[0])
            if current['first_terminal_observation'] is None:
                current['phase'] = m['status']
                if m['status'] in TERMINAL: current['first_terminal_observation'] = key
            db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(current), request_id))
            db.execute('UPDATE observations SET result=? WHERE id=?', (encode(result), key))
        return {'request_id': request_id, 'transport_status': m['status'], 'core_classification': result['classification'],
                'projection_status': result.get('projection_status'), 'mathematical_truth_verified': False}

    def _unknown(self, request_id, reason):
        job = self._job(request_id)
        if job['first_terminal_observation'] is not None:
            return {'request_id': request_id, 'phase': job['phase'], 'redispatched': False}
        if job['core_claim_id'] is not None:
            self._request(job)
            self._operation(request_id + ':local_unknown', 'mark_unknown', lambda: self._base('observation') |
                {'observation_id': 'unknown_' + uuid4().hex, 'request_id': request_id, 'claim_id': job['core_claim_id'],
                 'host_reference': job['host_reference'], 'transport_status': 'unknown', 'source_event_id': None,
                 'source_line': None, 'external_observed_at_utc': now(), 'model_observation': None,
                 'charge_observation': None, 'reason': reason})
        with self._db(True) as db:
            current = json.loads(db.execute('SELECT body FROM jobs WHERE request_id=?', (request_id,)).fetchone()[0])
            if current['first_terminal_observation'] is None: current['phase'] = 'unknown'
            db.execute('UPDATE jobs SET body=? WHERE request_id=?', (encode(current), request_id))
        return {'request_id': request_id, 'phase': current['phase'], 'redispatched': False, 'external_observation_verified': False}

    def recover(self, request_id, adapter=None):
        job = self._job(request_id)
        if job['phase'] == 'claim_intent' and job['core_claim_id'] is None:
            source = self._source(request_id)
            if source['claim_id'] is None:
                job['phase'] = 'unclaimed_interrupted'; self._save(job)
                return {'request_id': request_id, 'phase': job['phase'], 'redispatched': False}
            with self._db() as db: op = db.execute('SELECT record FROM operations WHERE key=?', (request_id + ':claim',)).fetchone()
            need(op is not None, 'claim belongs to another bridge')
            # Existing claim only: exact replay identifies ownership, never
            # dispatches transport even if an old result was lost.
            result = self.core.claim(json.loads(op['record']))
            need(result['dispatch_claim'] is None and result['historical_claim_id'] == source['claim_id'], 'claim ownership recovery failed')
            job['core_claim_id'] = source['claim_id']; job['phase'] = 'unknown'; self._save(job)
        with self._db() as db:
            pending = [r['id'] for r in db.execute('SELECT id FROM observations WHERE request_id=? AND result IS NULL ORDER BY rowid', (request_id,))]
        if pending:
            results = [self._project(request_id, key) for key in pending]
            return {'request_id': request_id, 'redispatched': False, 'replayed_observations': results}
        if job['phase'] in TERMINAL or job['phase'] in ('prepared', 'unclaimed_interrupted'):
            return {'request_id': request_id, 'phase': job['phase'], 'redispatched': False}
        if adapter is not None:
            self._adapter(adapter, for_execution=False)
            need(self.config['adapter']['can_observe_existing'], 'non-executing observation unsupported')
            observed = adapter.observe_existing(self._request(self._job(request_id)))
            if observed is not None: return {'redispatched': False, **self.receive(request_id, observed)}
        return self._unknown(request_id, 'observation_lost_no_redispatch')

    def timeout_evidence(self, request_id, adapter):
        """The transport's settlement for an unknown call stopped at its per-call limit, or None.

        Reads only: the transport builds the observation from its own local capture
        of this claimed call. Nothing is recorded or dispatched.
        """
        job = self._job(request_id)
        if job['phase'] != 'unknown' or job['first_terminal_observation'] is not None or job['core_claim_id'] is None:
            return None
        self._adapter(adapter, for_execution=False)
        settle = getattr(adapter, 'timeout_observation', None)
        return None if settle is None else settle(self._request(job))

    def settle_timeout(self, request_id, adapter):
        """Record that an unknown call's own process stopped at its per-call time limit.

        The call becomes failed with the reason local_timeout, so the coordinator
        may send a new call in its place. Returns the projection, or None when the
        transport has no such evidence. This never dispatches.
        """
        observation = self.timeout_evidence(request_id, adapter)
        if observation is None:
            return None
        key = self._archive(request_id, observation, settlement=LOCAL_TIMEOUT)
        return self._project(request_id, key)

    def close(self, reason):
        c.text(reason, 'close reason', 2000)
        with self._db(True) as db:
            db.execute('UPDATE meta SET value="true" WHERE key="closed"')
            self._event(db, 'closed_locally', {'reason': reason})
        if not self.core.status()['sealed']:
            self._operation('close', 'close', lambda: self._base('close') | {'reason': reason})
        return self.status()

    @staticmethod
    def _job_status(job):
        return {k: job[k] for k in ('request_id', 'job_id', 'phase', 'core_claim_id', 'host_reference', 'first_terminal_observation')}

    def job_status(self, request_id):
        return self._job_status(self._job(request_id))

    def status(self):
        with self._db() as db:
            jobs = [self._job_status(json.loads(row[0])) for row in db.execute('SELECT body FROM jobs ORDER BY rowid')]
            closed = db.execute('SELECT value FROM meta WHERE key="closed"').fetchone()[0] == 'true'
            raw_count = db.execute('SELECT count(*) FROM observations').fetchone()[0]
        return {'schema': VERSION, 'audience': 'metadata_only', 'mode': self.config['manifest']['mode'],
            'closed': closed, 'jobs': jobs, 'archived_observations': raw_count,
            'budget_owner': 'AdaptiveSession', 'independent_budget_grants': 0, 'core': self.core.status(),
            'mathematical_truth_verified': False, 'host_identity_authenticated': False}


class HarnessInertAdapter:
    """Current environment wiring, deliberately never an actual model adapter.

The fixed mock process supplies transport evidence. fixture supplies separate
scripted INERT adaptive bytes, labelled as such in the preserved carrier.
"""
    def __init__(self, root, manifest, fixture=None, *, process_mode='success'):
        from .harness.journal import HarnessJournal
        need(manifest['mode'] == 'synthetic', 'INERT harness cannot execute a real session')
        self.root = exchange._root(root); self.manifest = clone(manifest)
        self.fixture = fixture; self.process_mode = process_mode
        if self.root.exists(): self.journal = HarnessJournal(self.root)
        else: self.journal = HarnessJournal.create(self.root, max_calls=manifest['limits']['global_call_limit'],
                                                  max_in_flight=manifest['limits']['max_inflight'])
        status = self.journal.status()
        need(status['max_calls'] == manifest['limits']['global_call_limit'] and
             status['max_in_flight'] == manifest['limits']['max_inflight'], 'environment caps differ from core')

    def describe(self):
        return {'adapter_id': 'environment_inert', 'version': 'v1', 'mode': 'synthetic',
            'can_start': True, 'can_observe_existing': True, 'model_execution': False,
            'fresh_context_required': True, 'raw_response_preserved': True}

    def _packet(self, request):
        need(request.mode == 'synthetic' and request.packet['binding']['session_id'] == self.manifest['session_id'], 'inert adapter core differs')
        return {'data_classification': 'synthetic_software_test', 'inert': True, 'requested_model': None,
                'core_claim_id': request.core_claim_id, 'core_packet': request.packet}

    def start_once(self, request):
        from .harness.transport import MockProcessTransport
        self.journal.prepare(request.job_id, self._packet(request))
        def execute(packet, claim):
            receipt = MockProcessTransport(self.process_mode)(packet, claim)
            raw = None if self.fixture is None else self.fixture(clone(request.packet))
            need(raw is None or type(raw) is bytes, 'INERT fixture must return original bytes or null')
            receipt['core_response_base64'] = None if raw is None else base64.b64encode(raw).decode('ascii')
            receipt['response_origin'] = 'scripted_inert_fixture_not_model_output'
            return receipt
        self.journal.start_once(request.job_id, execute)
        observed = self.observe_existing(request)
        if observed is None: raise BridgeError('INERT transport has no completed observation')
        return observed

    def observe_existing(self, request):
        packet = self._packet(request)
        exported = self.journal.export()
        row = next((j for j in exported['jobs'] if j['job_id'] == request.job_id), None)
        if row is None: return None
        need(c.same_json(row['packet'], packet), 'environment job belongs to a different core claim')
        receipt = row['receipt']
        if receipt is None: return None
        event = next((item for item in reversed(exported['events']) if item['job_id'] == request.job_id
                      and item['kind'] in ('receipt', 'late_receipt') and c.same_json(item['detail'].get('receipt'), receipt)), None)
        need(event is not None, 'environment receipt source event missing')
        raw = receipt.get('core_response_base64')
        raw = None if raw is None else base64.b64decode(raw, validate=True)
        descriptor = self.describe(); status = receipt['transport_status']
        metadata = {'schema': OBSERVATION, 'observation_id': 'env_' + row['dispatch_id'] + '_' + str(event['seq']), 'job_id': request.job_id,
            'core_claim_id': request.core_claim_id, 'packet_id': request.packet['binding']['packet_id'],
            'adapter_id': descriptor['adapter_id'], 'adapter_version': descriptor['version'], 'mode': 'synthetic',
            'status': status, 'terminal_observed': status in TERMINAL,
            'host_reference': '/synthetic-harness/' + row['dispatch_id'], 'observed_at_utc': event['at'],
            'source_event_id': 'inert_event:' + str(event['seq']), 'source_line': None,
            'model_observation': None, 'charge_observation': None}
        return TransportObservation(metadata, raw, encode(receipt).encode('utf-8'))
