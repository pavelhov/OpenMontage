"""One-shot boards recover the same native image original after host interruption."""
import pytest
import hashlib
from collections import Counter
from lib import production_boards as boards
from lib import production_execution as execution, production_images as images
from tests.lib.test_production_images import board_package, reserve, host_receipt  # noqa: F401


def shared_ancestry(tmp_path):
    bindings = {}
    for aid, sources in [('identity', []), ('anchor', ['identity']),
                         ('start', ['identity', 'anchor']), ('end', ['start', 'anchor'])]:
        path = tmp_path / (aid + '.png')
        path.write_bytes(('SYNTHETIC ' + aid).encode())
        bindings[aid] = {'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'source_bindings': {source: {key: bindings[source][key]
                            for key in ('path', 'sha256')} for source in sources}}
    return {'board_bindings': bindings}


def test_shared_start_end_ancestry_is_hashed_once_per_pass(tmp_path, monkeypatch):
    pack = shared_ancestry(tmp_path)
    calls = Counter()
    current = boards._current
    def counted(root, binding):
        calls[binding['path']] += 1
        return current(root, binding)
    monkeypatch.setattr(boards, '_current', counted)
    assert boards._bindings_current(tmp_path, pack, ['start', 'end']) == {'start': True, 'end': True}
    assert calls == Counter({aid + '.png': 1 for aid in pack['board_bindings']})
    (tmp_path / 'identity.png').write_bytes(b'changed ancestor')
    assert boards._bindings_current(tmp_path, pack, ['start', 'end']) == {'start': False, 'end': False}


@pytest.mark.parametrize('defect', ['cycle', 'missing', 'wrong_hash', 'invalid_record'])
def test_ancestry_reuse_does_not_hide_invalid_shared_sources(tmp_path, defect):
    pack = shared_ancestry(tmp_path)
    binding = pack['board_bindings']['anchor']
    if defect == 'cycle':
        binding['source_bindings']['end'] = {key: pack['board_bindings']['end'][key]
                                           for key in ('path', 'sha256')}
    elif defect == 'missing':
        del pack['board_bindings']['identity']
    elif defect == 'wrong_hash':
        binding['source_bindings']['identity']['sha256'] = '0' * 64
    else:
        del binding['source_bindings']['identity']['sha256']
    if defect == 'invalid_record':
        with pytest.raises(KeyError):
            boards._bindings_current(tmp_path, pack, ['start', 'end'])
    else:
        assert boards._bindings_current(tmp_path, pack, ['start', 'end']) == {'start': False, 'end': False}


def test_ancestry_currentness_does_not_replace_role_review(tmp_path):
    pack = shared_ancestry(tmp_path)
    pack.update(prompt_pack={'start': {'required_predicates': ['cast_identity']}}, observations=[])
    review = {'review_id': 'observed', 'subject_sha256': pack['board_bindings']['start']['sha256'],
              'status': 'pass', 'predicates': [{'name': 'cast_identity', 'severity': 'critical', 'status': 'pass'}]}
    pack['observations'].append({'asset_id': 'start', 'roles': ['start'], 'review': review})
    assert boards._bindings_current(tmp_path, pack, ['start', 'end'])['start']
    assert boards._review_blockers(pack, 'start', 'start') == []
    review['predicates'][0]['status'] = 'fail'
    assert boards.review_findings(pack, 'start', 'start')
    review['subject_sha256'] = '0' * 64
    assert boards._review_blockers(pack, 'start', 'start') == ['missing current role observation']


def test_single_root_preserves_seeded_cycle_guard_and_default_result(tmp_path):
    pack = shared_ancestry(tmp_path)
    assert boards._binding_current(tmp_path, pack, 'end') is True
    assert boards._binding_current(tmp_path, pack, 'missing') is False
    visiting = {'anchor'}
    assert boards._binding_current(tmp_path, pack, 'end', visiting) is False
    assert visiting == {'anchor'}


def test_pending_board_original_never_emits_second_envelope(board_package):
    root, *_ = board_package
    call = reserve(board_package, call_id='board-original')
    first = execution.advance(root)
    assert first['host_envelope']['call_id'] == call['call_id']
    pending = execution.advance(root)
    assert 'host_envelope' not in pending
    assert images.image_usage(root)['total'] == 1
    receipt = host_receipt(root, first['host_envelope'])
    result = execution.complete_action(root, pending['action'], outcome={'host_receipt': receipt})
    assert images.image_call_status(root, call['call_id'])['state'] == 'imported'
    assert result['provider_calls'] == 0
    assert images.image_usage(root)['total'] == 1
    replay = execution.complete_action(root, pending['action'], outcome={'host_receipt': receipt})
    assert replay['replayed'] and replay['provider_calls'] == 0
    assert images.image_usage(root)['total'] == 1
