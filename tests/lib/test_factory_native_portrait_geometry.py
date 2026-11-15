"""Synthetic offline geometry compatibility; canonical provenance is a strict seam.

The reviewer observations below are fixture evidence, never production reviews.
"""
import copy
import hashlib

import pytest
from PIL import Image

from lib import production_boards as boards, production_execution as execution
from lib import production_autonomy as autonomy, production_provenance as provenance


@pytest.fixture
def native_board(tmp_path, monkeypatch):
    def image(name, size):
        path = tmp_path / name
        Image.new('RGB', size, 'navy').save(path)
        return {'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    source = image('frozen-first.png', (941, 1672))
    frame = image('sampled-outgoing.png', (768, 1344))
    binding = {**frame, 'origin': 'reuse', 'upstream_source': {
        'shot_id': 'A', 'attempt_id': 'original-A', 'output_sha256': 'a' * 64,
        'outgoing_frame_sha256': frame['sha256'], 'review_sha256': 'b' * 64}}
    names = list(boards.START_IMAGE_PREDICATES) + ['cast_identity', 'asset_quality']
    review = {'review_id': 'SYNTHETIC-board-review', 'reviewer': 'SYNTHETIC reviewer',
        'story_revision': 'r1', 'subject_sha256': frame['sha256'], 'status': 'pass',
        'predicates': [{'name': name, 'severity': 'critical', 'status': 'pass',
            'evidence': 'SYNTHETIC authored fixture observation'} for name in names]}
    pack = {'story_revision': 'r1', 'board_bindings': {'start': binding}, 'image_packets': {},
        'prompt_pack': {'start': {'shot_id': 'B', 'role': 'start_frame',
            'upstream_source': {'shot_id': 'A', 'role': 'outgoing_frame'},
            'required_predicates': ['cast_identity', 'asset_quality']}},
        'specification': {'reviewer': 'SYNTHETIC reviewer', 'shots': [{'shot_id': 'B',
            'roles': [{'asset_id': 'start', 'role': 'start_frame'}]}]},
        'observations': [{'asset_id': 'start', 'roles': ['start_frame'],
            'origin': 'designated_reviewer', 'image_binding': copy.deepcopy(binding), 'review': review}]}
    output = {'path': 'native.mp4', 'sha256': 'a' * 64}
    selected = {'attempt_id': 'original-A', 'output': copy.deepcopy(output),
                'outgoing_frame': copy.deepcopy(frame)}
    native = {'provider': 'openart_mcp', 'model': 'fal-h3-max-turbo', 'mode': 'image2video',
        'body': {'params': {'resolution': '768P', 'prompt': 'SYNTHETIC camera: portrait 9:16; whole cast visible.'}},
        'input_assets': [{'role': 'first_frame', 'source_path': source['path'], 'source_sha256': source['sha256']}]}
    proof = {'connector_provenance': {'provenance': 'agent_recorded_connector',
        'native': native, 'output': copy.deepcopy(output)}}
    probes = []
    def probe(path):
        probes.append(str(path))
        return {'video_width': 768, 'video_height': 1344}
    monkeypatch.setattr(autonomy, 'standing_image_start_review', lambda root: True)
    monkeypatch.setattr(boards, 'load_board_pack', lambda root: pack)
    monkeypatch.setattr(execution, 'resolve_board_input', lambda root, requested: {
        'path': str(tmp_path / binding['path']), 'sha256': binding['sha256']})
    monkeypatch.setattr(execution, 'load_selected_attempts', lambda root: {'A': selected})
    monkeypatch.setattr(provenance, 'validate_attempt_provenance', lambda *a, **kw: proof)
    monkeypatch.setattr('tools.video._shared.probe_output', probe)
    return tmp_path, pack, native, proof, selected, probes


def row(fixture):
    return boards.starting_image_reviews(fixture[0], 'B')[0]


def test_proven_h3_native_rounding_is_disclosed_without_claiming_exact_pixels(native_board):
    value = row(native_board)
    assert value['dimensions'] == [768, 1344] and not value['blockers']
    assert 'Observed encoded 768x1344 (4:7)' in value['geometry_note']
    assert 'not exact 9:16 pixels' in value['geometry_note']


def test_matching_registered_derived_output_retains_native_rounding_eligibility(native_board):
    root, _, _, proof, selected, probes = native_board
    proof['derived_edit'] = {'parent_attempt_id': 'original-A'}  # validator's proven return
    selected['output'] = {'path': 'registered-retimed.mp4', 'sha256': 'c' * 64}
    proof['selected_output'] = copy.deepcopy(selected['output'])
    assert not row(native_board)['blockers']
    assert str(root / 'native.mp4') in probes and str(root / 'registered-retimed.mp4') in probes


def test_native_geometry_never_overrides_failed_full_cast_review(native_board):
    review = native_board[1]['observations'][0]['review']
    review['status'] = 'fail'
    next(p for p in review['predicates'] if p['name'] == 'portrait_full_cast').update(
        status='fail', evidence='SYNTHETIC cropped foot remains a failed visual observation')
    value = row(native_board)
    assert value['geometry_note']
    assert any('portrait_full_cast' in reason for reason in value['blockers'])
    assert value['status'] == 'fail'


@pytest.mark.parametrize('defect', ['provider', 'model', 'resolution', 'aspect', 'prompt', 'source_aspect',
                                    'source_hash', 'direct_image', 'unbound', 'unverified', 'derived_resized'])
def test_rounding_exception_requires_every_native_discriminator(native_board, monkeypatch, defect):
    root, pack, native, _, selected, _ = native_board
    if defect == 'provider':
        native['provider'] = 'grok_cli'
    elif defect == 'model':
        native['model'] = 'another-model'
    elif defect == 'resolution':
        native['body']['params']['resolution'] = '1080P'
    elif defect == 'aspect':
        native['body']['params']['aspectRatio'] = '16:9'
    elif defect == 'prompt':
        native['body']['params']['prompt'] = 'SYNTHETIC portrait 4:7'
    elif defect == 'source_aspect':
        source = root / 'frozen-first.png'
        Image.new('RGB', (1024, 1536)).save(source)
        native['input_assets'][0]['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    elif defect == 'source_hash':
        native['input_assets'][0]['source_sha256'] = '0' * 64
    elif defect == 'direct_image':
        pack['board_bindings']['start']['origin'] = 'canonical_image_import'
    elif defect == 'unbound':
        monkeypatch.setattr(execution, 'resolve_board_input', lambda *a: {'path': str(root / 'foreign.png'), 'sha256': '0' * 64})
    elif defect == 'unverified':
        def reject(*a, **kw):
            raise ValueError('SYNTHETIC canonical provenance rejects forged original')
        monkeypatch.setattr(provenance, 'validate_attempt_provenance', reject)
    else:
        selected['output'] = {'path': 'resized-derived.mp4', 'sha256': 'd' * 64}
        monkeypatch.setattr('tools.video._shared.probe_output', lambda path: {
            'video_width': 720 if path.name == 'resized-derived.mp4' else 768,
            'video_height': 1280 if path.name == 'resized-derived.mp4' else 1344})
    value = row(native_board)
    assert any('9:16 pixel dimensions' in reason for reason in value['blockers'])
    assert 'geometry_note' not in value


@pytest.mark.parametrize('size, accepted', [((941, 1672), True), ((720, 1264), False),
                                           ((1024, 1536), False), ((1344, 768), False)])
def test_regular_one_pixel_rule_and_other_formats_remain_unchanged(native_board, size, accepted):
    root, pack, _, _, _, _ = native_board
    path = root / pack['board_bindings']['start']['path']
    Image.new('RGB', size).save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    pack['board_bindings']['start']['sha256'] = sha
    observation = pack['observations'][0]
    observation['image_binding'] = copy.deepcopy(pack['board_bindings']['start'])
    observation['review']['subject_sha256'] = sha
    value = row(native_board)
    assert (not value['blockers']) == accepted
    assert 'geometry_note' not in value
