import json
from pathlib import Path

import pytest

from lib.production_execution import (
    ProductionGovernanceError,
    reconcile_veo_invalid_key_rejection,
)


def _journal(tmp_path, *, error="400 INVALID_ARGUMENT {'reason': 'API_KEY_INVALID'}"):
    root = tmp_path / 'project'
    attempt = root / 'production_attempts' / 'original-1'
    attempt.mkdir(parents=True)
    output = root / 'assets' / 'video' / 'S01.mp4'
    result = {'success': False, 'data': {}, 'artifacts': [], 'error': error,
              'cost_usd': 0.0, 'duration_seconds': 0.0, 'seed': None, 'model': None}
    request = {'attempt_id': 'original-1', 'request_sha256': 'a' * 64,
               'scope': {'provider': 'veo'}, 'submitted_inputs': {'output_path': str(output)}}
    for name, value in [('request.json', request), ('raw_result.json', result),
                        ('result.json', {'status': 'uncertain', 'result': result, 'output': None,
                                         'preserved_output': None, 'exception': None})]:
        (attempt / name).write_text(json.dumps(value))
    return root, attempt, output


def test_veo_invalid_key_rejection_is_terminal_without_provider_call(tmp_path):
    root, attempt, _ = _journal(tmp_path)
    record = reconcile_veo_invalid_key_rejection(root, 'original-1', request_sha256='a'*64)
    assert record['status'] == 'failed'
    assert record['reconciliation_evidence']['kind'] == 'veo_api_key_invalid_400'
    assert json.loads((attempt / 'result.json').read_text())['status'] == 'uncertain'
    with pytest.raises(ProductionGovernanceError):
        reconcile_veo_invalid_key_rejection(root, 'original-1', request_sha256='a'*64)


@pytest.mark.parametrize('mutation', ['other_error', 'artifact', 'output', 'wrong_digest'])
def test_veo_invalid_key_reconciliation_rejects_weak_evidence(tmp_path, mutation):
    root, attempt, output = _journal(tmp_path)
    digest = 'a' * 64
    if mutation == 'other_error':
        data = json.loads((attempt / 'raw_result.json').read_text())
        data['error'] = 'timeout'
        (attempt / 'raw_result.json').write_text(json.dumps(data))
    elif mutation == 'artifact':
        data = json.loads((attempt / 'raw_result.json').read_text())
        data['artifacts'] = ['video.mp4']
        (attempt / 'raw_result.json').write_text(json.dumps(data))
    elif mutation == 'output':
        output.parent.mkdir(parents=True)
        output.write_bytes(b'video')
    else:
        digest = 'b' * 64
    with pytest.raises(ProductionGovernanceError):
        reconcile_veo_invalid_key_rejection(root, 'original-1', request_sha256=digest)
    assert not (attempt / 'reconciliation.json').exists()
