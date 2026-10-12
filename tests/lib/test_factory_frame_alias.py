"""Supported root frame aliases preserve exact governed dynamic board bindings."""
import copy

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib.shot_contract import file_sha256
from tests.lib.test_factory_board_resume import late_board_run
from tests.lib.test_production_factory import file_bytes
from tests.lib.test_production_request import neutral_package  # noqa: F401


def alternate_inputs(root):
    board = execution.resolve_board_input(root, {'asset_id': 'start'})
    return {'project_dir': str(root), 'governance': {'shot_id': 's1'},
        'first_frame': board['path'], 'duration': 5, 'model': 'byte-plus-seedance-2-mini',
        'mode': 'image2video', 'preferred_provider': 'openart_mcp',
        'allowed_providers': ['openart_mcp'], 'openart_project_id': 'fixture-mcp-project'}


def test_grok_dynamic_image_path_accepts_openart_first_frame_for_same_current_board(neutral_package, monkeypatch):
    root, transport, _, _ = late_board_run(neutral_package, monkeypatch)
    policy, _, _ = pa.require_active_policy(root)
    inputs = alternate_inputs(root)
    assert pa.retained_baselines(root, policy)['s1']['planned_request_template']['inputs']['image_path'] == {'$board': {'asset_id': 'start'}}
    before = file_bytes(root)
    pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    assert file_bytes(root) == before and len(transport.native_requests) == 1


@pytest.mark.parametrize('change', ['changed_bytes', 'wrong_slot', 'second_frame',
    'ambiguous_alias', 'input_assets_layout', 'list_layout', 'foreign_route'])
def test_dynamic_frame_alias_refuses_source_layout_and_route_changes(neutral_package, monkeypatch, change):
    root, transport, _, _ = late_board_run(neutral_package, monkeypatch)
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
    root, transport, _, _ = late_board_run(neutral_package, monkeypatch)
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
    root, transport, _, _ = late_board_run(neutral_package, monkeypatch)
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
