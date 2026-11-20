"""Offline preparation and installed module entry points."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from victorique import adaptive_exchange, loop, prepare
from victorique.harness.codex_contract import CODEX_VERSION, REQUIRED_EXEC_FLAGS
from victorique.harness.hosts import parse_host_probe


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class OfflinePrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='victorique-prepare-')
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve()
        self.inputs = self.out / 'inputs'
        self.inputs.mkdir()
        self.deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.problem = self.inputs / 'problem.txt'
        self.problem.write_bytes(b'INERT SOFTWARE INPUT. No mathematics.\r\nExact trailing newline.\n')
        binary = str(self.inputs / 'NONEXISTENT_INERT_CODEX.exe')
        probes = []
        for name, args, stdout in [
            ('version', ['--version'], 'codex-cli ' + CODEX_VERSION),
            ('help', ['--help'], 'Codex CLI'),
            ('exec_help', ['exec', '--help'], ' '.join(REQUIRED_EXEC_FLAGS)),
        ]:
            probes.append({'name': name, 'argv': [binary, *args], 'exit_code': 0,
                           'stdout': stdout, 'stderr': '', 'timed_out': False,
                           'output_truncated': False, 'error': None})
        self.profile = parse_host_probe('codex', binary, probes)
        self.profile_path = self.inputs / 'profile.json'
        dump(self.profile_path, self.profile)
        self.auth = {
            'schema': 'victorique.codex_execution_authorization.v1',
            'authorization_id': 'INERT.authorization',
            'source_ref': 'INERT software check', 'allow_execution': True,
            'session_id': 'prepare.inert.session',
            'protocol': {'protocol_id': 'prepare.inert', 'version': 'v1'},
            'binary': binary, 'host_version': CODEX_VERSION,
            'requested_model': 'INERT_NOT_A_MODEL', 'max_process_starts': 9,
            'expires_at_utc': self.deadline, 'permission_review_ref': 'INERT.fixture',
            'authentication_precondition': 'operator_completed_host_managed_login',
        }
        self.auth_path = self.inputs / 'authorization.json'
        dump(self.auth_path, self.auth)
        self.kw = dict(problem_path=self.problem, profile_path=self.profile_path,
                       authorization_path=self.auth_path, session_id='prepare.inert.session',
                       run_id='prepare.inert.run', provider_id='INERT_REQUESTED_PROVIDER',
                       model='INERT_NOT_A_MODEL', calls=9, deadline=self.deadline)

    def call(self, **kwargs):
        with ExitStack() as stack:
            for target in ('sqlite3.connect', 'subprocess.Popen', 'os.system', 'socket.socket.connect'):
                stack.enter_context(mock.patch(target, side_effect=AssertionError('prepare executed ' + target)))
            return prepare.prepare(**kwargs)

    def test_validators_and_isolated_module_cli(self):
        out = self.out / 'positive'
        result = self.call(**self.kw, output=out)
        config = json.loads((out / 'config.json').read_text(encoding='utf-8'))
        schemas = json.loads((out / 'adaptive-output-schemas.v1.json').read_text(encoding='utf-8'))
        adaptive_exchange.validate_manifest(config['manifest'])
        loop.validate_config(config, schemas)
        self.assertEqual(config['manifest']['problem']['content'].encode(), self.problem.read_bytes())
        self.assertEqual(config['manifest']['limits']['global_call_limit'], 9)
        self.assertEqual(config['manifest']['limits']['total_budget_units'], 9)
        self.assertEqual(config['manifest']['limits']['max_inflight'], 1)
        self.assertTrue(all(binding['model_request']['revision'] is None
                            for binding in config['manifest']['role_bindings'].values()))
        self.assertEqual(result['unknown_usage'], {'tokens': None, 'money': None, 'actual_model_revision': None})
        self.assertEqual(set(config), {'schema', 'manifest', 'transport', 'execution_grant',
                                      'output_schemas_path', 'poll_seconds'})
        args = [sys.executable, '-I', '-B', '-m', 'victorique.prepare',
                '--problem', str(self.problem), '--profile', str(self.profile_path),
                '--authorization', str(self.auth_path), '--session-id', self.kw['session_id'],
                '--run-id', self.kw['run_id'], '--provider-id', self.kw['provider_id'],
                '--model', self.kw['model'], '--calls', '9', '--deadline', self.deadline,
                '--output', str(self.out / 'cli-positive')]
        completed = subprocess.run(args, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=self.out, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors='replace'))
        for module in ('victorique.prepare', 'victorique.loop', 'victorique.export'):
            completed = subprocess.run([sys.executable, '-I', '-B', '-m', module, '--help'],
                                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=self.out, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors='replace'))
        self.assertEqual(list(self.out.rglob('*.sqlite3')), [])

    def test_one_review_authorization_keeps_one_process_ceiling(self):
        self.auth['max_process_starts'] = 1
        self.auth['protocol']['protocol_id'] = 'victorique.verify'
        dump(self.auth_path, self.auth)
        kwargs = dict(self.kw, calls=1)
        out = self.out / 'one-review'
        self.call(**kwargs, output=out)
        config = json.loads((out / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['manifest']['limits']['global_call_limit'], 1)
        self.assertEqual(config['transport']['authorization']['max_process_starts'], 1)

    def test_missing_or_expired_scope_rejects_before_output(self):
        missing = deepcopy(self.auth)
        del missing['source_ref']
        denied = dict(self.auth, allow_execution=False)
        expired = dict(self.auth, expires_at_utc='2000-01-01T00:00:00+00:00')
        variants = [('missing', missing, {}), ('denied', denied, {}),
                    ('expired', expired, {'deadline': expired['expires_at_utc']}),
                    ('excess_calls', self.auth, {'calls': 10})]
        for name, value, changes in variants:
            with self.subTest(name=name):
                path = self.inputs / (name + '.json')
                dump(path, value)
                kwargs = dict(self.kw, authorization_path=path, **changes)
                out = self.out / ('reject-' + name)
                with self.assertRaises(ValueError):
                    self.call(**kwargs, output=out)
                self.assertFalse(out.exists())

    def test_exact_input_schema_and_profile_bindings(self):
        out = self.out / 'positive'
        self.call(**self.kw, output=out)
        for source, name in ((self.problem, 'problem.txt'), (self.profile_path, 'public-profile.json'),
                             (self.auth_path, 'authorization.json')):
            self.assertEqual((out / name).read_bytes(), source.read_bytes())
        config_path = out / 'config.json'
        self.assertEqual(json.loads(config_path.read_text())['output_schemas_path'],
                         str(out / 'adaptive-output-schemas.v1.json'))
        mutant = dict(self.profile, binary=str(self.inputs / 'DIFFERENT.exe'))
        wrong_profile = self.inputs / 'wrong-profile.json'
        dump(wrong_profile, mutant)
        with self.assertRaises(ValueError):
            self.call(**dict(self.kw, profile_path=wrong_profile), output=self.out / 'wrong-profile')
        self.assertFalse((self.out / 'wrong-profile').exists())
        with self.assertRaises(ValueError):
            self.call(**self.kw, output=Path(prepare.__file__).parent / 'inside-source')
        read = prepare._read

        def missing_planner(path, cap=4 * 1024 * 1024):
            path, raw = read(path, cap)
            if path.name == 'adaptive-output-schemas.v1.json':
                schemas = json.loads(raw)
                del schemas['propose_plan']
                raw = json.dumps(schemas).encode()
            return path, raw

        with mock.patch.object(prepare, '_read', side_effect=missing_planner):
            with self.assertRaises(ValueError):
                self.call(**self.kw, output=self.out / 'missing-planner-schema')
        self.assertFalse((self.out / 'missing-planner-schema').exists())
        config_before, problem_before = config_path.read_bytes(), self.problem.read_bytes()
        self.problem.write_bytes(b'CHANGED INERT INPUT')
        self.assertEqual(config_path.read_bytes(), config_before)
        self.assertEqual((out / 'problem.txt').read_bytes(), problem_before)


if __name__ == '__main__':
    unittest.main()
