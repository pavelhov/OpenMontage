"""Pure offline role binding contracts; synthetic models never establish readiness."""
import copy
import hashlib
import pytest
from lib.production_request import validate_openart_asset_roles
from lib.production_execution import _paths, _openart_controls, planned_request_digest, ProductionGovernanceError


def packet():
    return {'binding': {'references': [
        {'id': 'start', 'role': 'start_frame', 'sha256': 'a' * 64},
        {'id': 'end', 'role': 'end_frame', 'sha256': 'b' * 64},
        {'id': 'cast', 'role': 'identity_reference', 'sha256': 'c' * 64}]},
        'shot': {'asset_ids': ['start', 'end', 'cast']}}


def test_boarded_native_start_and_end_are_bound_by_role_and_bytes():
    native = {'mode': 'image2video', 'input_assets': [
        {'role': 'first_frame', 'source_sha256': 'a' * 64},
        {'role': 'last_frame', 'source_sha256': 'b' * 64}]}
    validate_openart_asset_roles(packet(), native)
    changed = copy.deepcopy(native)
    changed['input_assets'][1]['role'] = 'first_frame'
    with pytest.raises(ValueError, match='role/source'):
        validate_openart_asset_roles(packet(), changed)


def test_multireference_mode_cannot_drop_cast_or_start():
    native = {'mode': 'element2video', 'input_assets': [
        {'role': 'first_frame', 'source_sha256': 'a' * 64},
        {'role': 'last_frame', 'source_sha256': 'b' * 64},
        {'role': 'reference_image', 'source_sha256': 'c' * 64}]}
    validate_openart_asset_roles(packet(), native)
    native['input_assets'].pop()
    with pytest.raises(ValueError, match='omits approved reference'):
        validate_openart_asset_roles(packet(), native)
    with pytest.raises(ValueError, match='approved start board'):
        validate_openart_asset_roles(packet(), {'mode': 'text2video', 'input_assets': []})


def test_explicit_role_paths_snapshot_and_detect_byte_drift(tmp_path):
    source = tmp_path / 'start.png'; source.write_bytes(b'approved fixture')
    inputs = {'input_assets': [{'role': 'first_frame', 'source_path': str(source),
                              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'upload_id': 'up-1'}]}
    visits = []
    _paths(inputs, tmp_path, lambda role, path: visits.append((role, path)) or str(path))
    assert visits == [('first_frame', source)]
    before = planned_request_digest(inputs, project_dir=tmp_path)
    source.write_bytes(b'drift')
    with pytest.raises(ProductionGovernanceError, match='source bytes'):
        planned_request_digest(inputs, project_dir=tmp_path)
    assert len(before) == 64


def test_canonical_openart_aliases_cannot_disagree(tmp_path):
    first = tmp_path / 'first.png'; second = tmp_path / 'second.png'
    assert _openart_controls({'first_frame': str(first), 'last_frame': str(second)}) == {
        'image_path': str(first), 'last_image_path': str(second)}
    with pytest.raises(ProductionGovernanceError, match='conflicting'):
        _openart_controls({'first_frame': str(first), 'image_path': str(second)})


@pytest.mark.parametrize('bad', ['wrong_target', 'unapproved', 'requirement', 'missing'])
def test_required_ending_cannot_be_waived_by_an_unrelated_native_role(tmp_path, bad):
    from lib.production_request import _check_required_native_controls
    start = tmp_path / 'start.png'; end = tmp_path / 'end.png'
    start.write_bytes(b'approved start'); end.write_bytes(b'exact approved ending')
    scene = {'scenes': [{'id': 'shot'}], 'metadata': {'visual_development': {'shot_cards': {
        'shot': {'pinned_final_frame': {'required': True, 'requirement_id': 'ending-1', 'end_state': 'exact'}}}}}}
    handoff = {'asset_id': 'start', 'scene_id': 'shot', 'approved': True,
               'pinned_final_frame': {'asset_id': 'end', 'requirement_id': 'ending-1', 'approved': True}}
    manifest = {'assets': [{'id': name, 'type': 'image', 'scene_id': 'shot', 'path': name + '.png'}
                           for name in ('start', 'end')], 'metadata': {'motion_handoffs': {'start': handoff}}}
    native = {'model': 'synthetic-model', 'input_assets': [
        {'role': 'first_frame', 'source_sha256': hashlib.sha256(start.read_bytes()).hexdigest()},
        {'role': 'last_frame', 'source_sha256': hashlib.sha256(end.read_bytes()).hexdigest()}]}
    _check_required_native_controls(scene, manifest, 'shot', native=native, project_dir=tmp_path)
    if bad == 'wrong_target': native['input_assets'][1]['source_sha256'] = 'f' * 64
    if bad == 'unapproved': handoff['pinned_final_frame']['approved'] = False
    if bad == 'requirement': handoff['pinned_final_frame']['requirement_id'] = 'other'
    if bad == 'missing': native['input_assets'].pop()
    with pytest.raises(ValueError, match='ending-frame'):
        _check_required_native_controls(scene, manifest, 'shot', native=native, project_dir=tmp_path)


def test_reviewed_ending_target_without_native_pin_allows_start_only_input_assets():
    contract = packet()
    contract['shot']['asset_ids'] = ['start', 'end']
    validate_openart_asset_roles(contract, {'mode': 'image2video', 'input_assets': [
        {'role': 'first_frame', 'source_sha256': 'a' * 64}]})


def test_public_capabilities_recursively_redact_union_branch_defaults():
    from tools.video.openart_cli_video import OpenArtCLIVideo
    caps = {'params': {}, 'branches': [{'params': {'prompt': {'default': 'PRIVATE_SECRET'},
        'resolution': {'enum': ['720p', 'https://private.example/?token=PRIVATE_SECRET']},
        'startFrame': {'const': 'https://private.example/?token=PRIVATE_SECRET'}}}]}
    safe = OpenArtCLIVideo._public_capabilities(caps)
    import json
    assert 'PRIVATE_SECRET' not in json.dumps(safe) and 'https://' not in json.dumps(safe)


def test_declared_multimodal_reference_roles_never_drop_or_cross(tmp_path):
    contract = packet()
    contract['binding']['references'].extend([
        {'id': 'video', 'role': 'reference_video', 'sha256': 'd' * 64},
        {'id': 'audio', 'role': 'reference_audio', 'sha256': 'e' * 64}])
    contract['shot']['asset_ids'].extend(['video', 'audio'])
    native = {'mode': 'element2video', 'input_assets': [
        {'role': 'first_frame', 'source_sha256': 'a' * 64},
        {'role': 'reference_image', 'source_sha256': 'c' * 64},
        {'role': 'reference_video', 'source_sha256': 'd' * 64},
        {'role': 'reference_audio', 'source_sha256': 'e' * 64}]}
    validate_openart_asset_roles(contract, native)
    native['input_assets'][-1]['role'] = 'reference_video'
    with pytest.raises(ValueError, match='role/source'):
        validate_openart_asset_roles(contract, native)
