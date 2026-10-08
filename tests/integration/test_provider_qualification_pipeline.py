"""Offline qualification contracts. All approvals and reviews here are synthetic fixtures."""
from lib.pipeline_loader import load_pipeline


def test_qualification_pipeline_has_one_generation_gate():
    manifest = load_pipeline('provider-qualification')
    assert manifest['category'] == 'custom'
    assert manifest['stability'] == 'beta'
    assert [s['name'] for s in manifest['stages']] == ['prepare', 'generate']
    assert all(s['human_approval_default'] for s in manifest['stages'])
    assert 'openart_cli_video' not in manifest['stages'][0]['tools_available']
    assert manifest['stages'][1]['required_artifacts_in'] == ['provider_qualification_packet']

import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from lib.checkpoint import CheckpointValidationError, write_checkpoint
from lib.provider_qualification import QualificationValidationError, validate_qualification_stage
from schemas.artifacts import ARTIFACT_NAMES, validate_artifact


@pytest.fixture
def prepared(tmp_path):
    # Synthetic local approval and review fixtures; never live result evidence.
    root = tmp_path / 'synthetic'
    (root / 'artifacts').mkdir(parents=True)
    marker = {'project_id': 'synthetic', 'pipeline_type': 'provider-qualification', 'story_revision': 'synthetic-v1',
              'governance': {'mode': 'strict', 'version': '1.0'}}
    (root / 'project.json').write_text(json.dumps(marker))
    def binding(name):
        path = root / 'artifacts' / name
        path.write_text('synthetic offline fixture')
        return {'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    prompt = 'Synthetic transport test prompt'
    native = {key: str(index) * 64 for index, key in enumerate([
        'native_controls_sha256', 'native_argv_sha256', 'native_body_sha256', 'profile_sha256'], 1)}
    packet = {'version': '1.0', 'project_id': 'synthetic', 'story_revision': 'synthetic-v1', 'purpose': 'result_contract_qualification',
              'authorization_status': 'proposed', 'governance_mode': 'strict', 'tool': 'openart_cli_video',
              'prompt': prompt, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
              'request_sha256': 'a' * 64, 'native_binding': native,
              'compiled_request': binding('compiled_request-original.json'), 'preparation_review': binding('preparation_review-original.json'),
              'sources': [binding('source.txt')],
              'scope_id': 'synthetic', 'shot_id': 'sample', 'unknown_cost_authorization_id': 'original',
              'provider': 'openart_cli', 'model': 'synthetic-model', 'mode': 'text2video',
              'duration': 1, 'aspect_ratio': '16:9', 'resolution': '720p', 'native_no_reference': True,
              'unknown_exposure': {'kind': 'unknown_cost', 'credits': None, 'usd': None, 'guaranteed_ceiling': False},
              'attempt_cap': 1, 'repair_cap': 0, 'quality_scope': 'transport_only'}
    (root / 'artifacts/provider_qualification_packet.json').write_text(json.dumps(packet))
    inputs = {'prompt': prompt, 'compiled_request_id': 'original', 'preparation_review_id': 'original',
              'unknown_cost_authorization_id': 'original', 'model': 'synthetic-model', 'mode': 'text2video',
              'duration': 1, 'aspect_ratio': '16:9', 'resolution': '720p',
              'governance': {'scope_id': 'synthetic', 'shot_id': 'sample', 'stage': 'generate'}}
    authorization = {'version': '1', 'kind': 'openart_unknown_cost', 'status': 'approved',
                     'approved_by': 'synthetic-test-fixture', 'provider': 'openart_cli', 'project_root': str(root),
                     'project_id': root.name, 'story_revision': 'synthetic-v1', 'scope_id': 'synthetic',
                     'shot_id': 'sample', 'model': 'synthetic-model', 'mode': 'text2video',
                     'purpose': 'result_contract_qualification', 'count': 1,
                     'occurrences': [{'id': 'original', 'index': 0, 'request_sha256': packet['request_sha256'],
                                      'native_sha256': native['native_body_sha256'], 'profile_sha256': native['profile_sha256']}]}
    (root / 'artifacts/unknown_cost_authorization-original.json').write_text(json.dumps(authorization))
    scope = {'id': 'synthetic', 'status': 'approved', 'approved_by': 'synthetic-test-fixture',
             'provider': 'openart_cli', 'project_id': root.name, 'story_revision': 'synthetic-v1',
             'phase': 'first_pass', 'attempts_per_shot': {'sample': 1}, 'requests': {'sample': packet['request_sha256']},
             'unknown_cost_authorization_sha256': hashlib.sha256(json.dumps(authorization, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    (root / 'production_scopes.json').write_text(json.dumps({'version': '1.0', 'scopes': [scope]}))
    return root, packet, inputs, native, {'profile_sha256': native['profile_sha256']}


def checkpoint(prepared, status='completed', human=True, **kwargs):
    root, packet, *_ = prepared
    return write_checkpoint(root.parent, root.name, 'prepare', status,
                            {'provider_qualification_packet': packet}, human_approved=human, **kwargs)


def validate(prepared):
    root, packet, inputs, native, profile = prepared
    return validate_qualification_stage(root, inputs, packet['request_sha256'], native=native, profile=profile)


def test_registered_schemas_and_required_custom_artifacts(prepared):
    assert {'provider_qualification_packet', 'provider_qualification_report'} <= set(ARTIFACT_NAMES)
    root, *_ = prepared
    with pytest.raises(CheckpointValidationError, match='canonical artifact'):
        write_checkpoint(root.parent, root.name, 'prepare', 'awaiting_human', {})
    with pytest.raises(CheckpointValidationError, match='schema validation'):
        write_checkpoint(root.parent, root.name, 'prepare', 'awaiting_human', {'provider_qualification_packet': {}})


def test_preparation_is_proposed_and_awaiting_human_is_not_authority(prepared):
    checkpoint(prepared, status='awaiting_human', human=False)
    assert prepared[1]['authorization_status'] == 'proposed'
    with pytest.raises(QualificationValidationError, match='human approval'):
        validate(prepared)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION'):
        checkpoint(prepared, human=False)


def test_missing_and_unapproved_gate_refused(prepared):
    with pytest.raises(QualificationValidationError, match='missing'):
        validate(prepared)
    path = checkpoint(prepared)
    data = json.loads(path.read_text())
    data['human_approved'] = False
    path.write_text(json.dumps(data))
    with pytest.raises(QualificationValidationError, match='human approval'):
        validate(prepared)


def test_exact_synthetic_gate_accepts_but_packet_stays_proposed(prepared):
    checkpoint(prepared)
    assert validate(prepared)['authorization_status'] == 'proposed'


@pytest.mark.parametrize('drift', ['prompt', 'request', 'native', 'profile', 'packet', 'source', 'compiled', 'review', 'stage', 'missing_native'])
def test_drift_refused(prepared, drift):
    checkpoint(prepared)
    root, packet, inputs, native, profile = prepared
    digest = packet['request_sha256']
    if drift == 'prompt':
        inputs['prompt'] += ' changed'
    elif drift == 'request':
        digest = 'b' * 64
    elif drift == 'native':
        native['native_controls_sha256'] = 'b' * 64
    elif drift == 'profile':
        profile['profile_sha256'] = 'b' * 64
    elif drift == 'packet':
        changed = copy.deepcopy(packet)
        changed['prompt'] += ' changed'
        (root / 'artifacts/provider_qualification_packet.json').write_text(json.dumps(changed))
    elif drift in {'source', 'compiled', 'review'}:
        key = {'compiled': 'compiled_request', 'review': 'preparation_review'}.get(drift)
        item = packet['sources'][0] if drift == 'source' else packet[key]
        (root / item['path']).write_text('changed bytes')
    elif drift == 'stage':
        inputs['governance']['stage'] = 'prepare'
    elif drift == 'missing_native':
        native = None
    with pytest.raises(QualificationValidationError):
        validate_qualification_stage(root, inputs, digest, native=native, profile=profile)


@pytest.mark.parametrize('unsafe', ['/tmp/outside', '../outside', 'artifacts/link'])
def test_safe_local_bindings_required(prepared, tmp_path, unsafe):
    root, packet, *_ = prepared
    outside = tmp_path / 'outside'
    outside.write_text('synthetic offline fixture')
    (root / 'artifacts/link').symlink_to(outside)
    packet['sources'][0]['path'] = unsafe
    (root / 'artifacts/provider_qualification_packet.json').write_text(json.dumps(packet))
    checkpoint(prepared)
    with pytest.raises(QualificationValidationError, match='outside|project-relative'):
        validate(prepared)


@pytest.mark.parametrize('field,value', [('credits', 0), ('usd', 0), ('usd', 0.1), ('guaranteed_ceiling', True)])
def test_unknown_exposure_never_claims_zero_usd_or_ceiling(prepared, field, value):
    packet = copy.deepcopy(prepared[1])
    packet['unknown_exposure'][field] = value
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact('provider_qualification_packet', packet)


@pytest.mark.parametrize('field,value', [('attempt_cap', 2), ('repair_cap', 1), ('authorization_status', 'approved'), ('governance_mode', 'auto')])
def test_exact_single_attempt_proposed_limits(prepared, field, value):
    packet = copy.deepcopy(prepared[1])
    packet[field] = value
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact('provider_qualification_packet', packet)


def test_auto_policy_does_not_approve_qualification(prepared):
    with pytest.raises(CheckpointValidationError, match='exact human approval'):
        checkpoint(prepared, human=False, approval_basis={'kind': 'policy', 'decision_id': 'synthetic', 'policy_sha256': 'a' * 64})


def test_report_separates_transport_billing_and_quality(prepared):
    root, packet, *_ = prepared
    checkpoint(prepared)
    report = {'version': '1.0', 'project_id': root.name, 'purpose': 'result_contract_qualification',
              'request_sha256': packet['request_sha256'], 'original_attempt_id': 'synthetic-original',
              'original_status': 'uncertain', 'outputs': [], 'billing': packet['unknown_exposure'],
              'creative_quality': 'not_production_reviewed', 'production_certified': False,
              'quality_scope': 'transport_only', 'result_contract_status': 'unverified'}
    write_checkpoint(root.parent, root.name, 'generate', 'awaiting_human', {'provider_qualification_report': report})
    for key, value in [('production_certified', True), ('creative_quality', 'pass'), ('original_status', 'collected')]:
        invalid = dict(report, **{key: value})
        with pytest.raises(jsonschema.ValidationError):
            validate_artifact('provider_qualification_report', invalid)
    with pytest.raises(CheckpointValidationError, match='canonical artifact'):
        write_checkpoint(root.parent, root.name, 'generate', 'awaiting_human', {})


def test_generation_cannot_advance_missing_or_awaiting_preparation(prepared):
    root, *_ = prepared
    with pytest.raises(CheckpointValidationError, match='PREREQUISITE VIOLATION'):
        write_checkpoint(root.parent, root.name, 'generate', 'completed', {}, human_approved=True)
    checkpoint(prepared, status='awaiting_human', human=False)
    with pytest.raises(CheckpointValidationError, match='PREREQUISITE VIOLATION'):
        write_checkpoint(root.parent, root.name, 'generate', 'completed', {}, human_approved=True)


def test_generation_report_schema_is_not_skipped(prepared):
    root, *_ = prepared
    checkpoint(prepared)
    with pytest.raises(CheckpointValidationError, match='schema validation'):
        write_checkpoint(root.parent, root.name, 'generate', 'awaiting_human', {'provider_qualification_report': {}})


@pytest.mark.parametrize('field', ['compiled_request_id', 'preparation_review_id'])
def test_bookkeeping_id_substitution_cannot_select_unapproved_evidence(prepared, field):
    checkpoint(prepared)
    root, _, inputs, *_ = prepared
    prefix = 'compiled_request' if field == 'compiled_request_id' else 'preparation_review'
    (root / 'artifacts' / (prefix + '-substitute.json')).write_text('synthetic alternative passing fixture')
    inputs[field] = 'substitute'
    with pytest.raises(QualificationValidationError, match='artifact selection'):
        validate(prepared)


def test_changed_story_revision_refused(prepared):
    checkpoint(prepared)
    root, *_ = prepared
    path = root / 'project.json'
    marker = json.loads(path.read_text())
    marker['story_revision'] = 'synthetic-v2'
    path.write_text(json.dumps(marker))
    with pytest.raises(QualificationValidationError, match='story revision'):
        validate(prepared)


@pytest.mark.parametrize('unsafe', ['../secret', '/private/secret', None, ''])
def test_unsafe_bookkeeping_id_refused(prepared, unsafe):
    checkpoint(prepared)
    prepared[2]['compiled_request_id'] = unsafe
    with pytest.raises(QualificationValidationError, match='safe retained ID'):
        validate(prepared)


def test_invalid_packet_error_does_not_disclose_instance_or_private_path(prepared):
    checkpoint(prepared)
    root, packet, *_ = prepared
    invalid = dict(packet, secret='private-sensitive-instance')
    (root / 'artifacts/provider_qualification_packet.json').write_text(json.dumps(invalid))
    with pytest.raises(QualificationValidationError) as exc:
        validate(prepared)
    assert 'private-sensitive-instance' not in str(exc.value)
    assert str(root) not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize('field', ['scope_id', 'shot_id'])
def test_bookkeeping_scope_or_shot_substitution_refused(prepared, field):
    checkpoint(prepared)
    prepared[2]['governance'][field] = 'substitute'
    with pytest.raises(QualificationValidationError, match='scope|shot'):
        validate(prepared)


@pytest.mark.parametrize('change', ['repair', 'two_attempts', 'extra_shot', 'batch', 'ordinary_generation', 'two_occurrences', 'other_authority'])
def test_one_gate_cannot_authorize_repair_batch_or_ordinary_generation(prepared, change):
    checkpoint(prepared)
    root, _, inputs, *_ = prepared
    scope_path = root / 'production_scopes.json'
    scopes = json.loads(scope_path.read_text())
    scope = scopes['scopes'][0]
    auth_path = root / 'artifacts/unknown_cost_authorization-original.json'
    auth = json.loads(auth_path.read_text())
    if change == 'repair':
        scope['phase'] = 'repair'
    elif change == 'two_attempts':
        scope['attempts_per_shot']['sample'] = 2
    elif change == 'extra_shot':
        scope['attempts_per_shot']['other'] = 1
        scope['requests']['other'] = scope['requests']['sample']
    elif change == 'batch':
        scope['requests']['sample'] = [scope['requests']['sample']] * 2
    elif change == 'ordinary_generation':
        auth['purpose'] = 'generation'
    elif change == 'two_occurrences':
        auth['count'] = 2
        auth['occurrences'].append(dict(auth['occurrences'][0], id='second', index=1))
    elif change == 'other_authority':
        inputs['unknown_cost_authorization_id'] = 'substitute'
    scope['unknown_cost_authorization_sha256'] = hashlib.sha256(json.dumps(auth, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    auth_path.write_text(json.dumps(auth))
    scope_path.write_text(json.dumps(scopes))
    with pytest.raises(QualificationValidationError):
        validate(prepared)


@pytest.mark.parametrize('field,value', [('model', 'different-model'), ('mode', 'image2video'), ('duration', 2),
                                       ('aspect_ratio', '9:16'), ('resolution', '1080p'), ('image_path', 'reference.png')])
def test_explicit_reviewable_settings_and_reference_free_semantics(prepared, field, value):
    checkpoint(prepared)
    prepared[2][field] = value
    with pytest.raises(QualificationValidationError, match='settings|reference-free'):
        validate(prepared)


def test_referenced_qualification_requires_exact_retained_role_bytes(prepared):
    root, packet, inputs, native, profile = prepared
    source = root / 'starting-board.png'; source.write_bytes(b'synthetic reviewed board')
    asset = {'role': 'first_frame', 'path': source.name,
             'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'upload_id': 'retained-fixture'}
    packet.update(mode='image2video', native_no_reference=False, input_assets=[asset])
    inputs.update(mode='image2video', image_path=str(source), image_upload_id=asset['upload_id'])
    native = dict(native, image_upload={'source_sha256': asset['sha256'], 'upload_id': asset['upload_id']})
    prepared = (root, packet, inputs, native, profile)
    authority_path = root / 'artifacts/unknown_cost_authorization-original.json'
    authority = json.loads(authority_path.read_text()); authority['mode'] = 'image2video'
    authority_path.write_text(json.dumps(authority))
    scopes_path = root / 'production_scopes.json'; scopes = json.loads(scopes_path.read_text())
    scopes['scopes'][0]['unknown_cost_authorization_sha256'] = hashlib.sha256(
        json.dumps(authority, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    scopes_path.write_text(json.dumps(scopes))
    (root / 'artifacts/provider_qualification_packet.json').write_text(json.dumps(packet))
    checkpoint(prepared)
    assert validate(prepared)['native_no_reference'] is False
    native['image_upload']['source_sha256'] = 'f' * 64
    with pytest.raises(QualificationValidationError, match='asset roles'):
        validate(prepared)


def test_referenced_qualification_schema_requires_source_and_upload_bindings(prepared):
    packet = copy.deepcopy(prepared[1]); packet.update(mode='image2video', native_no_reference=False)
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact('provider_qualification_packet', packet)


def explicit_settings_packet(prepared, settings, *, mode='text2video'):
    root, packet, inputs, native, profile = prepared
    native = dict(native, native_controls=dict(settings))
    prepared = (root, packet, inputs, native, profile)
    for key in ('duration', 'aspect_ratio', 'resolution'):
        packet.pop(key); inputs.pop(key)
    packet['native_settings'] = dict(settings)
    inputs.update(settings)
    if mode == 'image2video':
        source = root / 'starting-board.png'; source.write_bytes(b'exact synthetic starting board')
        asset = {'role': 'first_frame', 'path': source.name,
                 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'upload_id': 'retained-upload'}
        packet.update(mode=mode, native_no_reference=False, input_assets=[asset])
        inputs.update(mode=mode, image_path=str(source), image_upload_id=asset['upload_id'])
        native['image_upload'] = {'source_sha256': asset['sha256'], 'upload_id': asset['upload_id']}
        path = root / 'artifacts/unknown_cost_authorization-original.json'
        authorization = json.loads(path.read_text()); authorization['mode'] = mode
        path.write_text(json.dumps(authorization))
        scope_path = root / 'production_scopes.json'; scopes = json.loads(scope_path.read_text())
        scopes['scopes'][0]['unknown_cost_authorization_sha256'] = hashlib.sha256(
            json.dumps(authorization, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        scope_path.write_text(json.dumps(scopes))
    (root / 'artifacts/provider_qualification_packet.json').write_text(json.dumps(packet))
    checkpoint(prepared)
    return prepared


@pytest.mark.parametrize('mode,settings', [
    ('text2video', {'duration': 5, 'aspect_ratio': '16:9'}),
    ('image2video', {'duration': 5, 'resolution': '720p'}),
    ('text2video', {})])
def test_exact_mode_qualification_can_omit_unsupported_native_flags(prepared, mode, settings):
    selected = explicit_settings_packet(prepared, settings, mode=mode)
    assert validate(selected)['native_settings'] == settings


@pytest.mark.parametrize('drift', ['extra', 'omitted', 'prepared_extra', 'native_param_extra', 'duplicate_alias', 'json_type'])
def test_native_settings_extra_omitted_or_ambiguous_flags_fail_before_dispatch(prepared, drift):
    selected = explicit_settings_packet(prepared, {'duration': 5, 'aspect_ratio': '16:9'})
    _, _, inputs, native, _ = selected
    if drift == 'extra': inputs['resolution'] = '720p'
    if drift == 'omitted': inputs.pop('aspect_ratio')
    if drift == 'prepared_extra': native['native_controls']['resolution'] = '720p'
    if drift == 'native_param_extra': inputs['native_params'] = {'generateAudio': True}
    if drift == 'duplicate_alias': inputs['native_params'] = {'duration': 5}
    if drift == 'json_type': inputs['duration'] = 5.0
    with pytest.raises(QualificationValidationError, match='setting|parameter'):
        validate(selected)


def test_native_settings_camel_aliases_bind_exact_prepared_settings(prepared):
    selected = explicit_settings_packet(prepared, {'duration': 5, 'aspect_ratio': '16:9'})
    _, _, inputs, native, _ = selected
    inputs.pop('duration'); inputs.pop('aspect_ratio')
    inputs['native_params'] = {'duration': 5, 'aspectRatio': '16:9'}
    native['native_params'] = dict(inputs['native_params'])
    assert validate(selected)['native_settings'] == {'duration': 5, 'aspect_ratio': '16:9'}
    native['native_params']['resolution'] = '720p'
    with pytest.raises(QualificationValidationError, match='prepared native settings'):
        validate(selected)


@pytest.mark.parametrize('legacy_key', ['duration', 'aspect_ratio', 'resolution'])
def test_packet_cannot_mix_legacy_and_explicit_native_settings(prepared, legacy_key):
    packet = copy.deepcopy(prepared[1])
    settings = {key: packet.pop(key) for key in ('duration', 'aspect_ratio', 'resolution')}
    packet['native_settings'] = {'duration': settings['duration']}
    packet[legacy_key] = settings[legacy_key]
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact('provider_qualification_packet', packet)
