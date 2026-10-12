"""Supported root frame aliases preserve exact governed dynamic board bindings."""
import copy

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import production_images as images, production_request as request
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import NativeTransport
from tests.lib.test_production_factory import (file_bytes, prospective_package,
    retain_factory_board, standing_treatment_fixture, write)
from tests.lib.test_production_request import neutral_package  # noqa: F401


def original_frame_run(neutral_package, monkeypatch):
    """Current prepared frames and one original dispatch, with no successor setup."""
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['shots'] = [s for s in package['shots'] if s['id'] != 's2']
    for shot in package['shots']:
        shot['duration_seconds'] = 5
        shot['script'] = shot['script'].strip()
    spec = package['boards']
    spec['shots'] = [s for s in spec['shots'] if s['shot_id'] != 's2']
    next(n for n in spec['image_nodes'] if n['slot_id'] == 'payoff')['shot_id'] = 's1'
    template = spec['motion_binding']['contract_template']
    template['shots'] = [s for s in template['shots'] if s['id'] != 's2']
    template['reference_mode'] = 'frame_conditioned'
    for shot in template['shots']:
        shot['frame_input'] = {'asset_id': 'start' if shot['id'] == 's1' else 'other',
                               'role': 'start_frame'}
    request.prepare_package(root, bind(package))
    kwargs = standing_treatment_fixture(root, treatment='frame_conditioned')
    for sid, baseline in kwargs['baselines'].items():
        baseline['planned_request_template'] = execution.planned_request_template({
            'duration': 5, 'image_path': {'$board': {'asset_id': 'start' if sid == 's1' else 'other'}}},
            project_dir=root)
    pa.activate_standing_run(root, **kwargs)
    images.activate_standing_image_envelope(root)
    for slot in ('payoff', 'start', 'end', 'other'):
        retain_factory_board(root, slot)
    transport = NativeTransport(root)
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', transport)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda name: '/SYNTHETIC/offline/' + name)
    from tools.video.grok_cli_video import GrokCLIVideo
    tool = GrokCLIVideo(grok_path='/SYNTHETIC/offline/grok', sessions_root=str(root / 'sessions'))
    authored = request.compile_provider_prompt(root, 's1', provider='grok_cli')
    frame = execution.resolve_board_input(root, {'asset_id': 'start'})
    inputs = {'project_dir': str(root), 'governance': {'shot_id': 's1'}, 'prompt': authored['prompt'],
        'duration': 5, 'operation': 'reference_to_video', 'allow_unknown_cost': True,
        'image_path': frame['path'], 'output_path': str(root / 'assets/video/original-s1.mp4'),
        'compiled_request_id': 'original-s1', 'preparation_review_id': 'original-s1'}
    observation = {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'}
    native = request.prepare_grok_native(inputs, observation)
    contract = execution.load_shot_contract(root)
    shot = contract['shots'][0]
    timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en',
        'margin_seconds': .5, 'rationale': 'SYNTHETIC fixture', 'overlap_policy': 'serial',
        'overlap_rationale': 'SYNTHETIC fixture', 'segments': [], 'action_windows': [
            {'source_pointer': '/shot_contract/shots/0/' + key, 'value_sha256': request.digest(shot[key]),
             'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC fixture'}
            for key, start, end in [('dominant_action', .1, 3), ('completed_end_state', 3.1, 4.4)]]}
    judgment = {'version': '1.0', 'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass',
        'evidence_kind': 'reviewed', 'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC fixture only'} for name in sorted(request.PREDICATES)]}
    compiled, review = request.prepare_compiled_request(inputs, native, {'source': 'real'},
        coverage=authored['coverage'], timing=timing, review=judgment)
    write(root, 'artifacts/compiled_request-original-s1.json', compiled)
    write(root, 'artifacts/preparation_review-original-s1.json', review)
    scope = pa.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    inputs['governance']['scope_id'] = scope['id']
    generated = tool.execute(inputs)
    assert generated.success, generated.error
    return root, transport, generated.data['production_attempt_id']


def alternate_inputs(root):
    board = execution.resolve_board_input(root, {'asset_id': 'start'})
    return {'project_dir': str(root), 'governance': {'shot_id': 's1'},
        'first_frame': board['path'], 'duration': 5, 'model': 'byte-plus-seedance-2-mini',
        'mode': 'image2video', 'preferred_provider': 'openart_mcp',
        'allowed_providers': ['openart_mcp'], 'openart_project_id': 'fixture-mcp-project'}


def test_grok_dynamic_image_path_accepts_openart_first_frame_for_same_current_board(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    policy, _, _ = pa.require_active_policy(root)
    inputs = alternate_inputs(root)
    assert pa.retained_baselines(root, policy)['s1']['planned_request_template']['inputs']['image_path'] == {'$board': {'asset_id': 'start'}}
    before = file_bytes(root)
    pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    assert file_bytes(root) == before and len(transport.native_requests) == 1


@pytest.mark.parametrize('change', ['changed_bytes', 'wrong_slot', 'second_frame',
    'ambiguous_alias', 'input_assets_layout', 'list_layout', 'foreign_route'])
def test_dynamic_frame_alias_refuses_source_layout_and_route_changes(neutral_package, monkeypatch, change):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    policy, _, _ = pa.require_active_policy(root)
    inputs = alternate_inputs(root)
    if change == 'changed_bytes':
        changed = root / 'assets/images/SYNTHETIC-other-bytes.png'
        changed.write_bytes(b'SYNTHETIC different bytes, not an approved board')
        inputs['first_frame'] = str(changed)
    elif change == 'wrong_slot':
        # An identity source is retained and reviewed but is not this frame slot.
        inputs['first_frame'] = str(root / 'assets/images/cast.png')
    elif change == 'second_frame':
        inputs['last_frame'] = inputs['first_frame']
    elif change == 'ambiguous_alias':
        inputs['image_path'] = inputs['first_frame']
    elif change == 'input_assets_layout':
        inputs['input_assets'] = [{'role': 'first_frame', 'source_path': inputs.pop('first_frame')}]
    elif change == 'list_layout':
        inputs['first_frame'] = [inputs['first_frame']]
    elif change == 'foreign_route':
        inputs['preferred_provider'] = 'grok_cli'
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    assert file_bytes(root) == before and len(transport.native_requests) == 1


def test_frame_alias_normalization_rejects_duplicate_retained_occurrences(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    policy, _, _ = pa.require_active_policy(root)
    baselines = copy.deepcopy(pa.retained_baselines(root, policy))
    baseline = baselines['s1']['planned_request_template']['inputs']
    baseline['first_frame'] = copy.deepcopy(baseline['image_path'])
    # Isolate malformed retained evidence without rewriting frozen approval files.
    monkeypatch.setattr(pa, 'retained_baselines', lambda *args: baselines)
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError, match='ambiguous duplicated semantic input aliases'):
        pa._validate_request_delta(root, policy, 's1', alternate_inputs(root), 'openart_mcp')
    assert file_bytes(root) == before and len(transport.native_requests) == 1


def test_dynamic_root_alias_keeps_nested_index_order(neutral_package, monkeypatch):
    root, transport, _ = original_frame_run(neutral_package, monkeypatch)
    policy, _, _ = pa.require_active_policy(root)
    baselines = copy.deepcopy(pa.retained_baselines(root, policy))
    template = baselines['s1']['planned_request_template']
    source = template['inputs'].pop('image_path')
    cast = str(root / 'assets/images/cast.png')
    template['inputs']['image_paths'] = [source, cast]
    template['static_input_assets'] = [{'role': 'image_paths', 'path': cast,
        'sha256': file_sha256(cast)}]
    # Isolate an ordered retained template; no frozen approval is rewritten.
    monkeypatch.setattr(pa, 'retained_baselines', lambda *args: baselines)
    inputs = alternate_inputs(root)
    inputs['images'] = [inputs.pop('first_frame'), cast]
    before = file_bytes(root)
    pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    inputs['images'].reverse()
    with pytest.raises(pa.AutonomyError, match='exact upstream pointer/layout/order changed'):
        pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    assert file_bytes(root) == before and len(transport.native_requests) == 1
