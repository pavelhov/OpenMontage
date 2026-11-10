"""Schema-bound native factory contracts using synthetic files and no live API."""
import copy
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lib import production_autonomy as autonomy, production_execution as execution, production_request as preparation
from lib.spicy_catalog import digest


MODEL = 'synthetic/video/text-to-video'
H = 'a' * 64
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['prompt', 'duration_seconds'],
          'properties': {'prompt': {'type': 'string', 'minLength': 1},
                         'duration_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 8}}}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    import socket
    def forbidden(*args, **kwargs):
        pytest.fail('Factory v2 tests cannot access the network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    private_home = (tmp_path / 'private-home').resolve()
    private_home.mkdir(mode=0o700)
    monkeypatch.setenv('HOME', str(private_home))
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(private_home / 'openart-state'))
    monkeypatch.delenv('SPICY_API_KEY', raising=False)


def native(sources=None):
    payload = {'prompt': 'SYNTHETIC cube falls.', 'duration_seconds': 1}
    return {'provider': 'spicy', 'transport': 'spicy_native_jobs_v2', 'model': MODEL,
            'mode': 'text_to_video', 'input': payload, 'sources': sources or [], 'payload_state': 'ready',
            'input_schema': copy.deepcopy(SCHEMA), 'schema_sha256': digest(SCHEMA),
            'request_sha256': digest({'model': MODEL, 'input': payload})}


def route(value):
    return {'model': value['model'], 'mode': value['mode'], 'schema_sha256': value['schema_sha256'],
            'source_roles': [{key: source[key] for key in ('role', 'pointer', 'media_kind', 'mime')} for source in value['sources']]}


def source():
    return {'role': 'reference_video', 'pointer': '/reference_video_urls/0', 'media_kind': 'video',
            'mime': 'video/mp4', 'source_sha256': H, 'source_path': '/synthetic/video.mp4'}


def test_legacy_two_field_allowance_never_authorizes_v2():
    value = native()
    spec = {'routes': [{'model': MODEL, 'mode': 'text_to_video'}]}
    with pytest.raises(ValueError, match='outside approved route'):
        autonomy._spicy_approved_route(spec, value)
    spec['routes'] = [route(value)]
    assert autonomy._spicy_approved_route(spec, value) == spec['routes'][0]
    legacy = dict(value, transport='spicy_native_jobs_v1')
    with pytest.raises(ValueError, match='outside approved route'):
        autonomy._spicy_approved_route(spec, legacy)


@pytest.mark.parametrize('field,replacement', [('role', 'reference_audio'), ('pointer', '/reference_video_urls/1'),
                                             ('media_kind', 'image'), ('mime', 'video/webm')])
def test_every_typed_source_selection_is_exact_authority(field, replacement):
    value = native([source()])
    spec = {'routes': [route(value)]}
    value['sources'][0][field] = replacement
    with pytest.raises(ValueError, match='outside approved route'):
        autonomy._spicy_approved_route(spec, value)


def test_schema_drift_and_duplicate_source_slots_cannot_match_authority():
    value = native([source()])
    spec = {'routes': [route(value)]}
    value['input_schema']['properties']['duration_seconds']['maximum'] = 100
    with pytest.raises(ValueError, match='schema binding'):
        autonomy._spicy_approved_route(spec, value)
    value = native([source(), source()])
    with pytest.raises(ValueError, match='more than once'):
        autonomy._spicy_approved_route({'routes': [route(value)]}, value)


def test_compiled_binding_freezes_schema_pointer_kind_mime_and_hash():
    value = native([source()])
    bound = preparation._native_binding(value)
    assert bound['schema_sha256'] == digest(SCHEMA)
    for field, replacement in (('role', 'reference_audio'), ('pointer', '/reference_video_urls/1'),
                               ('media_kind', 'image'), ('mime', 'video/webm'), ('source_sha256', 'b' * 64)):
        changed = copy.deepcopy(value)
        changed['sources'][0][field] = replacement
        assert preparation._native_binding(changed) != bound
    changed = copy.deepcopy(value)
    changed['sources'][0]['transfer_binding'] = {'qualified_storage_hosts': ['historical-source.r2.cloudflarestorage.com'], 'journal_sha256': H}
    assert preparation._native_binding(changed) != bound


def test_policy_and_compiled_schema_separate_legacy_and_v2():
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    schema = load_schema('autonomy_policy')
    choices = schema['$defs']['spicy_quoted_usd_budget']['properties']['routes']['items']
    validator = Draft202012Validator(choices)
    assert not list(validator.iter_errors(route(native())))
    assert not list(validator.iter_errors({'model': 'legacy/model', 'mode': 'image_to_video'}))
    for damage in ({'schema_sha256': 'bad'}, {'source_roles': [dict(source(), source_path=None)]}, {'mode': 'video_analyze'}):
        assert list(validator.iter_errors(dict(route(native()), **damage)))
    branch = next(row for row in load_schema('compiled_request')['properties']['native_binding']['oneOf']
                  if row.get('properties', {}).get('provider', {}).get('const') == 'spicy')
    validator = Draft202012Validator(branch)
    bound = preparation._native_binding(native())
    assert not list(validator.iter_errors(bound))
    del bound['schema_sha256']
    assert list(validator.iter_errors(bound))


def test_exact_positive_native_duration_and_duplicate_alias_guards():
    inputs = {'native_input': {'duration_seconds': 5}}
    assert execution.explicit_motion_duration(inputs, provider='spicy') == 5
    assert execution.explicit_motion_duration(dict(inputs, duration=5), provider='spicy') == 5
    for value in (None, -1, True, float('nan')):
        with pytest.raises(ValueError):
            execution.explicit_motion_duration({'native_input': {'duration_seconds': value}}, provider='spicy')
    with pytest.raises(ValueError):
        execution.explicit_motion_duration(dict(inputs, duration=4), provider='spicy')
    for value in ({}, {'duration_seconds': -1}, {'duration_seconds': True}):
        with pytest.raises(ValueError, match='exact positive native duration'):
            preparation._body({'provider': 'spicy', 'input': value})
    value = native()
    before = copy.deepcopy(value)
    assert preparation._body(value)['params']['duration'] == 1
    assert value == before


def test_upload_role_does_not_prove_native_exact_frame_pin():
    schema = {'type': 'object', 'properties': {'image_url': {'type': 'string', 'description': 'A source reference image.'}}}
    assert not preparation.spicy_frame_pin_supported('synthetic/model', 'image_to_video', schema, 'image_url')
    schema['properties']['image_url']['description'] = 'Opening frame. The clip starts from this image.'
    assert preparation.spicy_frame_pin_supported('synthetic/model', 'image_to_video', schema, 'image_url')
    assert not preparation.spicy_frame_pin_supported('synthetic/model', 'talking_avatar', schema, 'image_url')
    assert not preparation.spicy_frame_pin_supported('synthetic/model', 'text_to_video', schema, 'image_url')


def test_selector_modern_closing_pin_requires_current_exact_native_semantics(tmp_path, monkeypatch):
    from lib import spicy_catalog
    from tools.video.video_selector import VideoSelector
    tool = SimpleNamespace(provider='spicy', supports={'first_last_frame': True},
                           first_last_frame_models={'legacy/only'}, input_schema={})
    schema = {'type': 'object', 'properties': {'last_image_url': {'type': 'string', 'description': 'The final frame of the video.'}}}
    observation = {'mode': 'image_to_video', 'input_schema': schema}
    calls = []
    def load(root, model, credential):
        calls.append((root, model, credential))
        return observation
    monkeypatch.setattr(spicy_catalog, 'load_account_observation_v2', load)
    inputs = {'project_dir': str(tmp_path), 'model': 'modern/known/image-to-video', 'mode': 'image_to_video',
              'credential_ref': 'env:SYNTHETIC', 'native_input': {'prompt': 'SYNTHETIC', 'duration_seconds': 1},
              'input_assets': [{'role': 'last_frame', 'pointer': '/last_image_url', 'source_path': str(tmp_path / 'ending.png')}]}
    assert VideoSelector._final_frame_eligible(tool, inputs)
    assert calls == [(str(tmp_path), inputs['model'], 'env:SYNTHETIC')]
    schema['properties']['last_image_url']['description'] = 'A reference image that guides style.'
    assert not VideoSelector._final_frame_eligible(tool, inputs)
    schema['properties']['last_image_url']['description'] = 'The final frame of the video.'
    inputs['input_assets'][0]['pointer'] = '/reference_image_urls/0'
    assert not VideoSelector._final_frame_eligible(tool, inputs)
    observation.clear()
    assert not VideoSelector._final_frame_eligible(tool, inputs)


def test_selector_exact_model_route_uses_current_v2_observation_without_static_enum(tmp_path, monkeypatch):
    from lib import spicy_catalog
    from tools.video.spicy_video import SpicyVideo
    from tools.video.video_selector import VideoSelector
    tool = SpicyVideo()
    observation = {'model': 'synthetic/modern/text-to-video', 'mode': 'text_to_video', 'input_schema': {}}
    monkeypatch.setattr(spicy_catalog, 'load_account_observation_v2', lambda *args: observation)
    inputs = {'project_dir': str(tmp_path), 'model': observation['model'], 'mode': observation['mode'],
              'operation': 'text_to_video', 'preferred_provider': 'spicy', 'preferred_tool': 'spicy_video',
              'allowed_providers': ['spicy'], 'native_input': {'prompt': 'SYNTHETIC', 'duration_seconds': 1}}
    assert VideoSelector()._filter_candidates(inputs, [tool]) == [tool]
    assert VideoSelector()._filter_candidates(dict(inputs, preferred_tool='another_tool'), [tool]) == []
    assert VideoSelector()._filter_candidates(dict(inputs, hosting_provider='another_host'), [tool]) == []
    assert VideoSelector()._filter_candidates(dict(inputs, model='synthetic/unobserved'), [tool]) == []
    observation.clear()
    assert VideoSelector()._filter_candidates(inputs, [tool]) == []


def test_template_mode_comes_from_observed_task_not_model_name(tmp_path, monkeypatch):
    from lib import spicy_catalog
    observed = {'model': 'synthetic/version-without-task-suffix', 'mode': 'text_to_video',
                'task': 'text-to-video', 'input_schema': copy.deepcopy(SCHEMA)}
    monkeypatch.setattr(spicy_catalog, 'load_account_observation_v2', lambda *args: observed)
    package = {'shots': [{'id': 'entry', 'action': 'SYNTHETIC cube falls.', 'duration_seconds': 1}],
               'boards': {'shots': [{'shot_id': 'entry', 'roles': []}]}}
    settings = {'project_dir': str(tmp_path), 'model': observed['model'], 'mode': 'text_to_video', 'native_input': {}}
    templates = preparation._build_spicy_v2_templates(package, settings)
    assert templates['entry']['native_input'] == {'prompt': 'SYNTHETIC cube falls.', 'duration_seconds': 1}
    observed['mode'] = 'video_edit'
    with pytest.raises(ValueError, match='current observed task'):
        preparation._build_spicy_v2_templates(package, settings)


def test_primary_selection_keeps_explicit_choice_after_historical_ambiguity(tmp_path, monkeypatch):
    from lib import spicy_lifecycle
    (tmp_path / 'project.json').write_text(json.dumps({'project_id': 'SYNTHETIC', 'story_revision': 'SYNTHETIC'}))
    video = {'key': 'retained-video', 'path': str(tmp_path / 'retained.mp4'), 'sha256': H, 'bytes': 1, 'mime': 'video/mp4'}
    inventory = {'requires_primary_selection': True, 'unresolved_asset_count': 0, 'outputs': [video]}
    monkeypatch.setattr(spicy_lifecycle, 'collected_outputs', lambda *args: inventory)
    request = {'attempt_id': 'SYNTHETIC-original', 'request_sha256': H,
               'spicy_native': {'transport': 'spicy_native_jobs_v2'}}
    action = execution._spicy_primary_selection_action(tmp_path, request, {'counted_original': True})
    assert action['kind'] == 'blocked' and action['inputs']['selectable_outputs'] == [video]
    inventory['unresolved_asset_count'] = 1
    assert execution._spicy_primary_selection_action(tmp_path, request, {}) is None


def test_schema_coverage_is_explicit_upper_bound_not_factory_readiness():
    from pathlib import Path
    stored = json.loads((Path(__file__).parents[1] / 'fixtures/spicy/factory-coverage-2026-10-22.json').read_text())
    assert stored['counts'] == {'video_schema_records': 160, 'native_schema_eligible': 155, 'canonical_schema_candidates_upper_bound': 139}
    assert all(row['production_authority'] == 'none' for row in stored['items'])
    assert any(any(blocker['code'] == 'canonical_prompt_unrepresentable' for blocker in row['blockers']) for row in stored['items'])


def test_typed_media_snapshot_binds_pointer_order_and_bytes(tmp_path):
    media = tmp_path / 'synthetic.mp4'
    media.write_bytes(b'SYNTHETIC encoded provider fixture')
    inputs = {'preferred_provider': 'spicy', 'native_input': {'duration_seconds': 1}, 'input_assets': [
        {'role': 'source_video', 'pointer': '/video_url', 'source_path': str(media)}]}
    first = execution.planned_request_digest(inputs, project_dir=tmp_path)
    changed = copy.deepcopy(inputs)
    changed['input_assets'][0]['pointer'] = '/reference_video_urls/0'
    assert execution.planned_request_digest(changed, project_dir=tmp_path) != first
    changed = copy.deepcopy(inputs)
    changed['native_input']['watermark'] = True
    assert execution.planned_request_digest(changed, project_dir=tmp_path) != first
    media.write_bytes(b'DIFFERENT synthetic bytes')
    assert execution.planned_request_digest(inputs, project_dir=tmp_path) != first


def test_v2_roles_allow_typed_reviewed_video_without_requiring_start_frame():
    value = native([source()])
    value['mode'] = 'reference_to_video'
    packet = {'binding': {'references': [{'id': 'video', 'role': 'reference_video', 'sha256': H, 'native_required': True}]},
              'shot': {'asset_ids': ['video']}}
    preparation._validate_spicy_asset_roles(packet, value)
    value['sources'][0]['source_sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='role/hash'):
        preparation._validate_spicy_asset_roles(packet, value)
    preparation._validate_spicy_asset_roles({'binding': {'references': []}}, native())


def test_current_schema_must_match_native_before_quote_authority(tmp_path, monkeypatch):
    from lib import spicy_jobs
    value = native()
    spec = {'id': 'spicy', 'billing': 'quoted_usd_budget', 'routes': [route(value)],
            'credential_digest': H, 'account_label': 'synthetic', 'credential_ref': 'env:SYNTHETIC',
            'per_attempt_cap_usd': '1', 'total_cap_usd': '2', 'qualified_storage_hosts': ['fixture.r2.cloudflarestorage.com']}
    now = datetime.now(timezone.utc)
    quote = {'model': MODEL, 'currency': 'USD', 'native_request_sha256': value['request_sha256'],
             'credential_digest': H, 'account_label': 'synthetic', 'estimated_cost_usd': '.1', 'max_charge_usd': '0.1',
             'obtained_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=4)).isoformat()}
    monkeypatch.setattr(autonomy, 'verify_spicy_account', lambda *args: {'schema_sha256': 'b' * 64})
    monkeypatch.setattr(spicy_jobs, 'load_quote_artifact', lambda *args: quote)
    with pytest.raises(ValueError, match='current account schema'):
        autonomy.verify_spicy_dispatch(tmp_path, {'governance': {'shot_id': 'shot'}, 'spicy_quote_id': 'quote'}, value, policy={'providers': [spec]})
    assert {path.name for path in tmp_path.iterdir()} == {'private-home'}


def _reference_free_standing_project(root, monkeypatch, binding, *, model=MODEL, schema=SCHEMA, source_row=None, native_extra=None):
    """Retain real canonical standing records containing only synthetic evidence."""
    from tests.lib.test_production_factory import standing_fixture, creator_fixture
    from lib.shot_contract import project_review_subject, shot_review_subject
    kwargs = standing_fixture(root)
    contract = execution.load_shot_contract(root)
    original_assets = copy.deepcopy(contract['assets'])
    contract.update(reference_mode='reference_free', assets=[], late_cast_ids=[], payoff_speaker_ids=[])
    contract.pop('payoff_asset_id', None)
    for shot in contract['shots']:
        shot.update(reference_mode='reference_free', asset_ids=[], cast_ids=[], dialogue=[], required_visible_speakers=[], upstream=[])
    if source_row:
        from lib.shot_contract import file_sha256
        asset = original_assets[0]
        asset.update(id='typed-source', role=source_row['contract_role'], cast_ids=[], path=source_row['path'],
                     sha256=file_sha256(root / source_row['path']))
        asset.pop('upstream_source', None)
        asset['review']['subject_sha256'] = asset['sha256']
        contract['assets'] = [asset]
        contract.pop('reference_mode')
        for shot in contract['shots']:
            shot.pop('reference_mode')
            shot['asset_ids'] = [asset['id']]
        if source_row['contract_role'] == 'start_frame':
            end = next(row for row in original_assets if row['role'] == 'end_frame')
            end['cast_ids'] = []
            contract['assets'].append(end)
            for shot in contract['shots']:
                shot['asset_ids'].append(end['id'])
        else:
            contract['reference_mode'] = 'reference_guided'
            for shot in contract['shots']:
                shot['reference_mode'] = 'reference_guided'
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    (root / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    authored = preparation.compile_provider_prompt(root, 'entry', provider='spicy')
    mode = model.rsplit('/', 1)[-1].replace('-', '_')
    inputs = {'project_dir': str(root), 'governance': {'shot_id': 'entry'}, 'model': model,
              'mode': mode, 'operation': mode, 'preferred_provider': 'spicy',
              'allowed_providers': ['spicy'], 'preferred_tool': 'spicy_video',
              'native_input': {'prompt': authored['prompt'], 'duration_seconds': 8},
              'account_label': 'offline-fixture', 'credential_ref': binding['credential_ref'],
              'input_assets': [], 'compiled_request_id': 'v2-t2v', 'preparation_review_id': 'v2-t2v',
              'output_path': str(root / 'assets/video/v2-t2v.mp4')}
    inputs['native_input'].update(native_extra or {})
    if source_row:
        inputs['input_assets'] = [{'role': source_row['role'], 'pointer': source_row['pointer'], 'source_path': str(root / source_row['path'])}]
    (root / 'assets/video').mkdir(parents=True, exist_ok=True)
    spec = {'id': 'spicy', 'billing': 'quoted_usd_budget', 'account_label': 'offline-fixture',
            'credential_ref': binding['credential_ref'], 'credential_digest': binding['credential_digest'],
            'qualified_storage_hosts': ['fixture-upload.r2.cloudflarestorage.com', 'fixture-account.r2.cloudflarestorage.com'],
            'routes': [{'model': model, 'mode': mode, 'schema_sha256': digest(schema), 'source_roles': [] if source_row is None else [
                {key: source_row[key] for key in ('role', 'pointer', 'media_kind', 'mime')}]}],
            'total_cap_usd': '0.30', 'per_attempt_cap_usd': '0.20'}
    kwargs['policy']['providers'] = [spec]
    kwargs['policy']['locked'] = {'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}}
    kwargs['baselines'] = autonomy.standing_baselines(root, {'entry': inputs})
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup.update(providers=[spec], provider_accounts={'spicy': 'offline-fixture'}, approved_source_sha256=[row['sha256'] for row in contract['assets']])
    setup['source_transfer'] = [{**setup['source_transfer'][0], 'provider': 'spicy', 'account': 'offline-fixture',
                               'roles': ['identity_reference', 'start_frame', 'end_frame', 'payoff_board', 'reference_image', 'reference_video', 'reference_audio'],
                               'max_file_bytes': 1024 * 1024, 'max_batch_bytes': 8 * 1024 * 1024}]
    kwargs['standing_setup'] = creator_fixture(root, 'v2-standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'v2-command', command)
    autonomy.activate_standing_run(root, **kwargs)
    return inputs, authored, contract


def test_actual_reference_free_v2_native_compiles_quotes_and_reserves_once(tmp_path, monkeypatch):
    """Actual builder/compiled gates with fake HTTP; no upload or paid transport."""
    from lib import spicy_jobs, spicy_catalog
    monkeypatch.setenv('SPICY_FACTORY_V2_TEST_KEY', 'SYNTHETIC-NONWORKING-FACTORY-KEY')
    binding = spicy_jobs.credential_binding('env:SPICY_FACTORY_V2_TEST_KEY')
    calls = []
    class HTTP:
        def __init__(self):
            self.binding = binding
            self.qualified_storage_hosts = {'fixture-account.r2.cloudflarestorage.com'}
        def get_model(self, model, **kwargs):
            return self.request_json('GET', '/models/' + model)
        def list_models(self, **query):
            return self.request_json('GET', '/models', query=query or None)
        def quote_task(self, *, model, input_data):
            return self.request_json('POST', '/jobs/quote', {'model': model, 'input': input_data})
        def get_balance(self):
            return self.request_json('GET', '/chat/credit')
        def request_json(self, method, path, body=None, *, query=None, **kwargs):
            calls.append((method, path, copy.deepcopy(body)))
            if method == 'GET' and path in ('/models', '/models/' + MODEL, '/models/synthetic%2Fvideo%2Ftext-to-video'):
                row = {'model': MODEL, 'modality': 'video', 'tasks': ['text-to-video'], 'enabled': True,
                       'available': True, 'inputSchema': copy.deepcopy(SCHEMA)}
                return {'code': 200, 'data': {'total': 1, 'items': [row]} if path == '/models' else row}
            if method == 'POST' and path == '/jobs/quote':
                return {'code': 200, 'data': {'quoteId': 'SYNTHETIC-v2-quote', 'model': MODEL, 'currency': 'USD',
                        'estimatedCost': '0.10', 'maxCharge': '0.15',
                        'expiresAt': (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()}}
            if method == 'GET' and path == '/chat/credit':
                return {'code': 200, 'data': {'available': '1.00', 'held': '0', 'total': '1.00'}}
            pytest.fail('Unexpected synthetic transport: generation/upload is forbidden in this test')
    root = tmp_path / 'project'
    root.mkdir()
    inputs, authored, contract = _reference_free_standing_project(root, monkeypatch, binding)
    http = HTTP()
    spicy_catalog.observe_account_v2(root, http, MODEL, binding['credential_ref'])
    value = spicy_jobs.build_native_request(inputs)
    assert value['transport'] == 'spicy_native_jobs_v2' and value['sources'] == [] and value['payload_state'] == 'ready'
    quote = spicy_jobs.quote_native_request(root, value, account_label='offline-fixture', credential_ref=binding['credential_ref'], client=http)
    inputs['spicy_quote_id'] = quote['quote_id']
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
              'rationale': 'SYNTHETIC timing fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC separated actions',
              'segments': [], 'action_windows': [
                  {'source_pointer': '/shot_contract/shots/0/' + key, 'start_seconds': start, 'end_seconds': end,
                   'rationale': 'SYNTHETIC authored window'}
                  for key, start, end in [('dominant_action', 0, 6), ('completed_end_state', 6, 7.4)]]}
    review = {'version': '1.0', 'reviewer': 'SYNTHETIC textual reviewer', 'status': 'pass', 'evidence_kind': 'reviewed',
              'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical', 'evidence': 'SYNTHETIC fixture, no real media claim'}
                             for name in sorted(preparation.PREDICATES)]}
    compiled, _ = preparation.prepare_compiled_request(inputs, value, {'source': 'real'}, coverage=authored['coverage'],
                                                    timing=timing, review=review, persist=True)
    assert compiled['native_binding']['schema_sha256'] == digest(SCHEMA)
    scope = autonomy.derive_scope(root, inputs, provider='spicy')
    inputs['governance']['scope_id'] = scope['id']
    tool = SimpleNamespace(provider='spicy', name='spicy_video', capability='video_generation', version='SYNTHETIC')
    checked = execution.preflight(tool, inputs)
    assert checked['request_sha256'] == execution.planned_request_digest(inputs, project_dir=root)
    assert not list(root.glob('artifacts/spicy_source_transfer-*.json'))
    assert not (root / 'production_attempts').exists()
    assert all(path != '/jobs/createTask' and 'upload' not in path for _, path, _ in calls)


@pytest.mark.parametrize('task_name', ['text-to-video', 'image-to-video', 'reference-to-video', 'video-extend', 'video-edit', 'multi-output'])
@pytest.mark.parametrize('dispatch_selector', [False, True])
def test_actual_v2_roundtrip_preserves_counted_original_and_validates_delivery(tmp_path, monkeypatch, task_name, dispatch_selector):
    """Real canonical gateway and native lifecycle, entirely synthetic transport."""
    import shutil
    import subprocess
    from PIL import Image
    from lib import spicy_jobs, spicy_catalog, production_provenance
    from tests.lib.test_spicy_factory import _NativeJobsHTTPFixture, _install_synthetic_download_transport
    from tools.video.spicy_video import SpicyVideo
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Synthetic video verification requires local FFmpeg')
    monkeypatch.setenv('SPICY_FACTORY_V2_TEST_KEY', 'SYNTHETIC-NONWORKING-FACTORY-KEY')
    binding = spicy_jobs.credential_binding('env:SPICY_FACTORY_V2_TEST_KEY')
    multiple = task_name == 'multi-output'
    if multiple:
        task_name = 'reference-to-video'
    model = 'synthetic/factory/' + task_name
    schema = copy.deepcopy(SCHEMA)
    if multiple:
        schema['properties']['return_last_frame'] = {'type': 'boolean'}
    source_row = None
    root = tmp_path / 'project'
    root.mkdir()
    provider_video = tmp_path / 'SYNTHETIC-provider.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=720x720:r=24:d=8',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    str(provider_video)], check=True, capture_output=True)
    if task_name == 'image-to-video':
        Image.new('RGB', (512, 512), 'blue').save(root / 'SYNTHETIC-source.png')
        schema['properties']['image_url'] = {'type': 'string', 'description': 'Opening frame. The clip starts from this image.',
                                             'contentMediaType': ['image/png'], 'x-ui': {'widget': 'upload'}}
        schema['required'].append('image_url')
        source_row = {'role': 'first_frame', 'pointer': '/image_url', 'media_kind': 'image', 'mime': 'image/png',
                      'contract_role': 'start_frame', 'path': 'SYNTHETIC-source.png'}
    elif task_name == 'reference-to-video':
        Image.new('RGB', (512, 512), 'blue').save(root / 'SYNTHETIC-source.png')
        schema['properties']['reference_image_urls'] = {'type': 'array', 'minItems': 1, 'maxItems': 3,
                'items': {'type': 'string', 'contentMediaType': ['image/png']}, 'x-ui': {'widget': 'multi-upload'}}
        schema['required'].append('reference_image_urls')
        source_row = {'role': 'reference_image', 'pointer': '/reference_image_urls/0', 'media_kind': 'image', 'mime': 'image/png',
                      'contract_role': 'reference_image', 'path': 'SYNTHETIC-source.png'}
    elif task_name in {'video-extend', 'video-edit'}:
        shutil.copyfile(provider_video, root / 'SYNTHETIC-source.mp4')
        schema['properties']['video_url'] = {'type': 'string', 'contentMediaType': ['video/mp4'], 'x-ui': {'widget': 'upload'}}
        schema['required'].append('video_url')
        source_row = {'role': 'source_video', 'pointer': '/video_url', 'media_kind': 'video', 'mime': 'video/mp4',
                      'contract_role': 'reference_video', 'path': 'SYNTHETIC-source.mp4'}
    inputs, authored, contract = _reference_free_standing_project(root, monkeypatch, binding, model=model, schema=schema,
        source_row=source_row, native_extra={'return_last_frame': True} if multiple else None)
    still_bytes = (root / source_row['path']).read_bytes() if multiple else b''
    class HTTP(_NativeJobsHTTPFixture):
        def request_json(self, method, path, body=None, *, query=None, headers=None, **kwargs):
            if path.startswith('/models/'):
                self.calls.append((method, path, copy.deepcopy(body), {}))
                return {'code': 200, 'data': {'model': model, 'modality': 'video', 'tasks': [task_name],
                                             'enabled': True, 'available': True, 'inputSchema': schema}}
            if path == '/chat/credit':
                self.calls.append((method, path, copy.deepcopy(body), {}))
                return {'code': 200, 'data': {'available': '1.00', 'held': '0', 'total': '1.00'}}
            result = super().request_json(method, path, body, query=query, headers=headers, **kwargs)
            if 'model' in result['data']:
                result['data']['model'] = model
            if path == '/common/download-url':
                result['data']['key'] = body['key']
                if multiple:
                    result['data']['url'] = self.output_url.replace('original.mp4', {'fixture-last-frame': 'last.png', 'fixture-alternate': 'alternate.mp4'}.get(body['key'], 'original.mp4'))
            if path == '/jobs/recordInfo' and multiple:
                result['data']['output']['assets'].extend([
                    {'key': 'fixture-alternate', 'mime': 'video/mp4', 'bytes': len(self.video_bytes), 'width': 720, 'height': 720, 'durationSeconds': 8},
                    {'key': 'fixture-last-frame', 'mime': 'image/png', 'bytes': len(still_bytes), 'width': 512, 'height': 512, 'role': 'last_frame'}])
            return result
    http = HTTP(binding, provider_video.read_bytes())
    downloads = _install_synthetic_download_transport(monkeypatch, http.video_bytes)
    if multiple:
        from lib import openart_download
        from tests.lib.test_openart_download import Response
        class Connection:
            def __init__(self, host, address, timeout):
                downloads.append({'host': host})
            def request(self, method, target, headers):
                assert method == 'GET' and not any(key.lower() in {'authorization', 'cookie'} for key in headers)
                self.target = target
            def getresponse(self):
                is_still = self.target.endswith('/last.png')
                data = still_bytes if is_still else http.video_bytes
                return Response(data, headers={'Content-Type': 'image/png' if is_still else 'video/mp4', 'Content-Length': str(len(data))})
            def set_timeout(self, timeout):
                pass
            def close(self):
                pass
        monkeypatch.setattr(openart_download, '_PinnedHTTPSConnection', Connection)
    monkeypatch.setattr(spicy_jobs, 'client_for_credential', lambda *args, **kwargs: http)
    spicy_catalog.observe_account_v2(root, http, model, binding['credential_ref'])
    if source_row:
        spicy_jobs.upload_sources(root, inputs, client=http)
    value = spicy_jobs.build_native_request(inputs)
    assert value['payload_state'] == 'ready'
    inputs['spicy_quote_id'] = spicy_jobs.quote_native_request(root, value, account_label='offline-fixture', credential_ref=binding['credential_ref'], client=http)['quote_id']
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
              'rationale': 'SYNTHETIC timing fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC separated actions',
              'segments': [], 'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
                  'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC authored window'}
                  for key, start, end in [('dominant_action', 0, 6), ('completed_end_state', 6, 7.4)]]}
    review = {'version': '1.0', 'reviewer': 'SYNTHETIC reviewer', 'status': 'pass', 'evidence_kind': 'reviewed',
              'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical', 'evidence': 'SYNTHETIC fixture only'}
                             for name in sorted(preparation.PREDICATES)]}
    preparation.prepare_compiled_request(inputs, value, {'source': 'real'}, coverage=authored['coverage'], timing=timing, review=review, persist=True)
    scope = autonomy.derive_scope(root, inputs, provider='spicy')
    inputs['governance']['scope_id'] = scope['id']
    assert 'duration' not in inputs and 'duration_seconds' not in inputs
    assert inputs['native_input']['duration_seconds'] == 8
    dispatch_tool = SpicyVideo()
    if dispatch_selector:
        from tools.video.video_selector import VideoSelector
        monkeypatch.setattr(VideoSelector, '_providers', lambda self: [SpicyVideo()])
        dispatch_tool = VideoSelector()
    try:
        result = dispatch_tool.execute(inputs)
        assert not result.success
    except spicy_jobs.SpicyError:
        pass
    originals = execution._attempts(root)
    assert len(originals) == 1 and len(http.creates) == 1
    original = originals[0]
    assert original['tool_name'] == ('video_selector' if dispatch_selector else 'spicy_video')
    assert original['scope']['provider'] == 'spicy'
    completed = execution.resume_original_attempt(root, original['attempt_id'], request_sha256=original['request_sha256'])
    if multiple:
        assert completed['status'] == 'blocked'
        assert not Path(inputs['output_path']).exists()
        from lib import spicy_lifecycle
        calls_before_choice = (len(http.creates), len(downloads), len(http.calls))
        action = execution.next_production_action(root)
        assert action['kind'] == 'blocked'
        assert action['inputs']['action'] == 'select_primary_original_video'
        assert {row['key'] for row in action['inputs']['selectable_outputs']} == {'fixture-original', 'fixture-alternate'}
        assert all(Path(row['path']).is_file() for row in action['inputs']['selectable_outputs'])
        paused = execution.advance(root, max_steps=3)
        assert paused['status'] == 'blocked' and paused['steps'] == 0
        repeated = execution.resume_original_attempt(root, original['attempt_id'], request_sha256=original['request_sha256'])
        assert repeated['status'] == 'blocked'
        assert (len(http.creates), len(downloads), len(http.calls)) == calls_before_choice
        selected = spicy_lifecycle.select_primary_output(root, original['attempt_id'], 'fixture-alternate')
        assert selected['receipt']['primary_asset_key'] == 'fixture-alternate'
        completed = execution.resume_original_attempt(root, original['attempt_id'], request_sha256=original['request_sha256'])
    assert completed['status'] == 'generated'
    assert len(http.creates) == 2 and http.creates[0] == http.creates[1]
    assert len(execution._attempts(root)) == 1 and len(downloads) == (3 if multiple else 1)
    if multiple:
        collected = spicy_jobs.validate_collected_original(root, original['attempt_id'])
        assert len(collected['outputs']) == 3
        assert len([row for row in collected['outputs'] if row['mime'].startswith('video/')]) == 2
        still = next(row for row in collected['outputs'] if row['mime'] == 'image/png')
        assert '/stills/' in still['path'] and Path(still['path']).read_bytes() == still_bytes
    if task_name == 'text-to-video':
        def no_current_admission(*args, **kwargs):
            raise AssertionError('Collected original must use its frozen proof, not current admission')
        monkeypatch.setattr(spicy_jobs, 'require_ready_v2', no_current_admission)
        monkeypatch.setattr(spicy_catalog, 'load_account_observation_v2', no_current_admission)
    proof = production_provenance.validate_attempt_provenance(root, original['attempt_id'], shot_id='entry',
                    story_revision=contract['story_revision'], expected_output=completed['output'])
    assert proof['result']['output'] == completed['output']
    from lib import production_draft
    delivery = production_draft.deliver_standalone(root, operation_id='SYNTHETIC-v2-' + task_name)
    assert delivery['status'] in {'complete', 'partial'} and delivery['provider_calls'] == 0
    assert delivery['not_certification'] is True and delivery['posted'] is False
    assert len(http.creates) == 2 and len(execution._attempts(root)) == 1
    assert bool(list(root.glob('artifacts/spicy_source_transfer-*.json'))) is bool(source_row)
