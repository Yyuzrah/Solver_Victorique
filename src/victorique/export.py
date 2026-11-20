"""Read-only export of the original native endpoint and unchanged candidate text."""
from pathlib import Path
import argparse
import json
import sys

from .loop import _native, _read, validate_config


def export(root, output):
    c, _, session, _, _, checked_path = _native()
    root = checked_path(root); destination = checked_path(output)
    c.need(not destination.exists() and not destination.is_relative_to(root) and not root.is_relative_to(destination),
           'new independent output directory required')
    config = json.loads(_read(root / 'run-config.json'))
    validate_config(config, json.loads(_read(root / 'role-output-schemas.json')))
    core = session.open(root / 'native'); snapshot = core.snapshot()
    c.need(c.same_json(snapshot['exchange']['manifest'], config['manifest']), 'original native manifest differs')
    endpoint = snapshot['endpoint']
    candidate = None if endpoint is None else endpoint['root_candidate']
    native_endpoint = None if endpoint is None else endpoint.get('reducer_endpoint')
    decision = None if native_endpoint is None else native_endpoint.get('decision')
    outcome = 'pending' if endpoint is None else 'complete' if decision == 'complete' else 'abstain' if decision == 'abstain' else 'partial'
    destination.mkdir(parents=True)
    with (destination / 'endpoint.json').open('x', encoding='utf8') as stream:
        json.dump(endpoint, stream, ensure_ascii=False, indent=2)
    files = []
    if candidate is not None:
        raw = candidate['content'].encode('utf8')
        with (destination / 'proof.md').open('xb') as stream: stream.write(raw)
        files.append({'file': 'proof.md', 'candidate_ref': c.candidate_ref(candidate), 'bytes': len(raw), 'original_content_unchanged': True})
        for node in endpoint['root_dependency_closure']:
            if c.same_json(c.candidate_ref(node), c.candidate_ref(candidate)): continue
            name = 'dependency-' + str(len(files)) + '.md'; data = node['content'].encode('utf8')
            with (destination / name).open('xb') as stream: stream.write(data)
            files.append({'file': name, 'candidate_ref': c.candidate_ref(node), 'bytes': len(data), 'original_content_unchanged': True})
    result = {'schema': 'victorique.original_problem_export.v1', 'native_outcome': outcome,
        'candidate_file_available': candidate is not None, 'delivery_state': 'no_candidate_delivery' if candidate is None else outcome + '_native_candidate',
        'native_decision': decision, 'original_endpoint_id': None if endpoint is None else endpoint['endpoint_id'],
        'files': files, 'evaluation_or_verification_performed': False, 'mathematical_truth_verified': False,
        'expert_verified': False, 'new_claims': 0, 'output_directory': str(destination)}
    with (destination / 'export-status.json').open('x', encoding='utf8') as stream: json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.root, args.output), ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
