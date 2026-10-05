import json

import pytest

from lib.production_execution import (
    ProductionGovernanceError,
    reconcile_veo_unsupported_audio_flag_rejection,
)


ERROR = ('Veo direct API generation failed: generate_audio parameter is only supported '
         'in Gemini Enterprise Agent Platform mode, not in Gemini Developer API mode.')


def _journal(tmp_path):
    root = tmp_path / 'project'
    attempt = root / 'production_attempts' / 'original-1'
    attempt.mkdir(parents=True)
    output = root / 'assets' / 'video' / 'S01.mp4'
    result = {'success': False, 'data': {}, 'artifacts': [], 'error': ERROR,
              'cost_usd': 0.0, 'duration_seconds': 0.0, 'seed': None, 'model': None}
    request = {'attempt_id': 'original-1', 'request_sha256': 'a' * 64,
               'scope': {'provider': 'veo'},
               'submitted_inputs': {'output_path': str(output), 'generate_audio': True}}
    for name, value in [('request.json', request), ('raw_result.json', result),
                        ('result.json', {'status': 'uncertain', 'result': result, 'output': None,
                                         'preserved_output': None, 'exception': None})]:
        (attempt / name).write_text(json.dumps(value))
    return root, attempt, output


def test_unsupported_audio_flag_rejection_is_terminal(tmp_path):
    root, attempt, _ = _journal(tmp_path)
    record = reconcile_veo_unsupported_audio_flag_rejection(root, 'original-1', request_sha256='a'*64)
    assert record['status'] == 'failed'
    assert record['reconciliation_evidence']['kind'] == 'veo_generate_audio_unsupported_developer_api'
    assert json.loads((attempt / 'result.json').read_text())['status'] == 'uncertain'
    with pytest.raises(ProductionGovernanceError):
        reconcile_veo_unsupported_audio_flag_rejection(root, 'original-1', request_sha256='a'*64)


@pytest.mark.parametrize('change', ['wrong_error', 'missing_flag', 'output_exists', 'wrong_digest'])
def test_unsupported_audio_rejects_weak_evidence(tmp_path, change):
    root, attempt, output = _journal(tmp_path)
    digest = 'a'*64
    if change == 'wrong_error':
        data = json.loads((attempt / 'raw_result.json').read_text())
        data['error'] = 'timeout'
        (attempt / 'raw_result.json').write_text(json.dumps(data))
    elif change == 'missing_flag':
        data = json.loads((attempt / 'request.json').read_text())
        data['submitted_inputs'].pop('generate_audio')
        (attempt / 'request.json').write_text(json.dumps(data))
    elif change == 'output_exists':
        output.parent.mkdir(parents=True)
        output.write_bytes(b'video')
    else:
        digest = 'b'*64
    with pytest.raises(ProductionGovernanceError):
        reconcile_veo_unsupported_audio_flag_rejection(root, 'original-1', request_sha256=digest)
    assert not (attempt / 'reconciliation.json').exists()
