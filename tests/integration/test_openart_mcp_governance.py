"""Actual Strict preparation -> agent handoff -> original collection; offline only.

Retained public native forms are observed, but all account, approval, connector
receipts and media bytes here are synthetic. Fixtures cannot qualify live MCP.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from lib import production_execution as execution, production_request as preparation
from lib import openart_mcp as mcp, openart_mcp_jobs as jobs, openart_mcp_dispatch as dispatch
from lib.checkpoint import write_checkpoint
from lib import production_provenance as provenance
from tests.lib.test_shot_contract import reference_free_contract, refresh
from tests.lib.test_production_request import write

FIXTURE = 'Synthetic offline review/approval; never provider or creative qualification.'


@pytest.fixture
def fixture_observations(tmp_path, monkeypatch):
    discovery = tmp_path / 'discovery'; discovery.mkdir(mode=0o700)
    raw_schema = (Path(__file__).parents[1] / 'fixtures/openart/mcp_integration_forms.json').read_bytes()
    account = {'version': '1.0', 'transport': 'agent_mediated_connector',
        'classification': 'synthetic_fixture', 'observed_at': '2026-10-07T00:00:00Z',
        'tool': 'openart_me', 'arguments': {},
        'data': {'user': {'uid': 'synthetic-mcp-user', 'email': 'fixture@example.test'},
                 'plan': 'Synthetic', 'credits': 1000}}
    for kind, raw in [('schema', raw_schema), ('account', json.dumps(account).encode())]:
        path = discovery / f'2026-10-07-{kind}-observation.json'
        path.write_bytes(raw); path.chmod(0o600)
        monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_' + kind.upper(), hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR', str(discovery))
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'private'))
    return discovery


def prepare_project(root, *, model='pixverseV6', duration=1, audio=None, selector=False):
    root.mkdir()
    contract = reference_free_contract()
    contract['project_id'] = root.name
    contract['shots'][0]['duration_seconds'] = duration
    refresh(contract)
    marker = {'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'pipeline_type': 'provider-qualification', 'governance': {'mode': 'strict', 'version': '1.0'}}
    write(root / 'project.json', marker)
    write(root / 'artifacts/shot_contract.json', contract)
    write(root / 'artifacts/scene_plan.json', {'version': '1.0', 'scenes': [{'id': 'entry',
        'type': 'generated', 'description': FIXTURE, 'start_seconds': 0, 'end_seconds': duration, 'script_section_id': 's1'}]})
    write(root / 'artifacts/script.json', {'version': '1.0', 'title': FIXTURE, 'total_duration_seconds': duration,
        'sections': [{'id': 's1', 'text': 'A red cube drops and comes to rest.', 'start_seconds': 0, 'end_seconds': duration}]})
    authored = preparation.compile_provider_prompt(root, 'entry', provider='openart_mcp', model=model)
    params = {'duration': duration, 'resolution': '720p'}
    if audio is not None: params['audio'] = audio
    inputs = {'project_dir': str(root), 'governance': {'scope_id': 'approved', 'shot_id': 'entry', 'stage': 'generate'},
        'operation': 'text_to_video', 'model': model, 'mode': 'text2video', 'prompt': authored['prompt'],
        'native_params': params, 'output_path': str(root / 'clip.mp4'),
        'compiled_request_id': 'original', 'preparation_review_id': 'original',
        'unknown_cost_authorization_id': 'original'}
    if selector:
        inputs.update(preferred_provider='openart_mcp', allowed_providers=['openart_mcp'])
    profile = mcp.load_profile(model, 'text2video', require='candidate')
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    shot = contract['shots'][0]
    timing = {'method': 'segmented_estimate', 'duration_seconds': duration, 'language': 'en', 'margin_seconds': .05,
        'rationale': FIXTURE, 'overlap_policy': 'serial', 'overlap_rationale': FIXTURE, 'segments': [],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
            'value_sha256': preparation.digest(shot[key]), 'start_seconds': a * duration,
            'end_seconds': b * duration - .05, 'rationale': FIXTURE}
            for key, a, b in [('dominant_action', 0, .6), ('completed_end_state', .6, 1)]]}
    compiled = preparation.prepare_compiled_request(inputs, native, profile, coverage=authored['coverage'], timing=timing)
    review = {'version': '1.0', 'review_id': 'original', 'reviewer': FIXTURE, 'status': 'pass',
        'subject_sha256': preparation.digest(compiled), 'evidence_kind': 'fixture_only',
        'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical', 'evidence': FIXTURE} for p in sorted(preparation.PREDICATES)]}
    write(root / 'artifacts/compiled_request-original.json', compiled)
    write(root / 'artifacts/preparation_review-original.json', review)
    request_sha = execution.planned_request_digest(inputs, project_dir=root)
    approval = root / 'approval.txt'; approval.write_text(FIXTURE + ' Explicitly accept no enforceable credit ceiling for exactly one original MCP sample.')
    evidence = {'path': 'approval.txt', 'sha256': hashlib.sha256(approval.read_bytes()).hexdigest()}
    authority = {'provider': 'openart_mcp', 'status': 'approved', 'approved_by': FIXTURE, 'evidence': evidence,
        'project_id': marker['project_id'], 'story_revision': marker['story_revision'], 'request_sha256': request_sha,
        'scope_id': 'approved', 'shot_id': 'entry', 'model': model, 'mode': 'text2video',
        'account_uid_sha256': native['account_binding']['uid_sha256'], 'native_body_sha256': native['body_sha256'],
        'source_binding_sha256': native['source_binding_sha256'], 'max_attempts': 1,
        'no_enforceable_credit_ceiling': True, 'cli_authority': False, 'purpose': 'result_contract_qualification'}
    write(root / 'artifacts/openart_mcp_unknown_cost-original.json', authority)
    scope = {'id': 'approved', 'provider': 'openart_mcp', 'status': 'approved', 'approved_by': FIXTURE,
        'evidence': evidence, 'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
        'phase': 'first_pass', 'requests': {'entry': request_sha}, 'attempts_per_shot': {'entry': 1},
        'approval_plan_sha256': execution.approval_plan_digest(contract),
        'openart_mcp_billing_authorization_sha256': dispatch.billing_authorization_digest(authority)}
    write(root / 'production_scopes.json', {'version': '1.0', 'scopes': [scope]})
    def binding(name):
        path = root / 'artifacts' / name
        return {'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    packet = {'version': '1.0', 'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
        'purpose': 'result_contract_qualification', 'authorization_status': 'proposed', 'governance_mode': 'strict',
        'tool': 'openart_mcp_video', 'provider': 'openart_mcp', 'prompt': inputs['prompt'],
        'prompt_sha256': hashlib.sha256(inputs['prompt'].encode()).hexdigest(), 'request_sha256': request_sha,
        'native_binding': preparation._native_binding(native), 'native_settings': preparation.mcp_native_settings(native),
        'compiled_request': binding('compiled_request-original.json'), 'preparation_review': binding('preparation_review-original.json'),
        'sources': [binding('shot_contract.json'), binding('script.json'), binding('scene_plan.json')],
        'scope_id': 'approved', 'shot_id': 'entry', 'unknown_cost_authorization_id': 'original',
        'model': model, 'mode': 'text2video', 'native_no_reference': True,
        'unknown_exposure': {'kind': 'unknown_cost', 'credits': None, 'usd': None, 'guaranteed_ceiling': False},
        'attempt_cap': 1, 'repair_cap': 0, 'quality_scope': 'transport_only'}
    write(root / 'artifacts/provider_qualification_packet.json', packet)
    write_checkpoint(root.parent, root.name, 'prepare', 'completed', {'provider_qualification_packet': packet}, human_approved=True)
    return inputs, native


def install_fake_download(monkeypatch, jobs_module, media_path):
    """Inject only HTTP bytes; the actual helper writes and proves its receipt."""
    import io, socket
    from lib import openart_download
    raw = Path(media_path).read_bytes()
    class OriginalResponse(io.BytesIO):
        status = 200
        def getheader(self, name): return str(len(raw)) if name == 'Content-Length' else None
    class OriginalConnection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def set_timeout(self, *args): pass
        def getresponse(self): return OriginalResponse(raw)
        def close(self): pass
    monkeypatch.setattr(openart_download.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(openart_download, '_PinnedHTTPSConnection', OriginalConnection)


def test_wan3_generated_audio_is_not_reference_audio(fixture_observations, tmp_path):
    inputs, native = prepare_project(tmp_path / 'wan', model='wan3-0', duration=2, audio=True)
    authority = execution.prepare_openart_mcp_handoff(inputs)
    assert native['body']['params']['audio'] is True
    assert native['input_assets'] == []
    assert authority['purpose'] == 'qualification'
    assert 'native_argv_sha256' not in preparation._native_binding(native)


@pytest.mark.parametrize('matching', [True, False])
def test_strict_scope_can_bind_exact_per_request_billing_for_a_batch(fixture_observations, tmp_path, matching):
    inputs, _ = prepare_project(tmp_path / 'batch')
    root = Path(inputs['project_dir'])
    path = root / 'production_scopes.json'; retained = json.loads(path.read_text())
    scope = retained['scopes'][0]; authority_sha = scope.pop('openart_mcp_billing_authorization_sha256')
    request_sha = scope['requests']['entry'] if matching else 'f' * 64
    scope['openart_mcp_billing_authorizations'] = {request_sha: authority_sha}
    write(path, retained)
    if matching:
        assert execution.prepare_openart_mcp_handoff(inputs)['billing']['enforceable_credit_ceiling'] is False
    else:
        with pytest.raises(ValueError): execution.prepare_openart_mcp_handoff(inputs)


def test_approved_packet_original_connector_flow_and_provenance(fixture_observations, tmp_path, monkeypatch):
    inputs, native = prepare_project(tmp_path / 'flow')
    root = Path(inputs['project_dir'])
    jobs.prepare(root, attempt_id='original', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    envelope = jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    assert envelope['arguments'] == native['body']
    jobs.receive(root, 'original', outcome={'historyId': 'original-history', 'status': 'PENDING'})
    assert jobs.poll_args(root, 'original')['arguments'] == {'historyId': 'original-history'}
    with pytest.raises(ValueError):
        jobs.record_status(root, 'original', result={'historyId': 'foreign-history', 'status': 'COMPLETED'})
    jobs.record_status(root, 'original', result={'historyId': 'original-history', 'status': 'PENDING'})
    jobs.record_status(root, 'original', result={'historyId': 'original-history', 'status': 'COMPLETED',
        'resources': [{'id':'video-original', 'mediaType':'video', 'status':'COMPLETED', 'url': 'https://fixture.invalid/original.mp4'}], 'creditsCharged': 1})
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    downloaded = real_av_clip(tmp_path / 'fixture-download.mp4', seconds=1)
    install_fake_download(monkeypatch, jobs, downloaded)
    concrete = jobs.download_original(root, 'original')
    jobs.collect(root, 'original', downloaded_path=concrete['downloaded_path'])
    expected = {'path': inputs['output_path'], 'sha256': hashlib.sha256(downloaded.read_bytes()).hexdigest()}
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    proof = provenance.validate_attempt_provenance(root, 'original', shot_id='entry',
        story_revision=json.loads((root / 'project.json').read_text())['story_revision'], expected_output=expected)
    assert proof['request']['transport'] == 'agent_mediated_connector'
    with pytest.raises(ValueError): jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    with pytest.raises(ValueError): jobs.qualify_result(root, 'original')


@pytest.mark.parametrize('change', ['revoke_scope', 'billing_provider', 'request_duration', 'approval_bytes'])
def test_prepare_begin_rechecks_retained_authority(fixture_observations, tmp_path, change):
    inputs, _ = prepare_project(tmp_path / change)
    root = Path(inputs['project_dir'])
    jobs.prepare(root, attempt_id='original', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    if change == 'revoke_scope':
        value = json.loads((root / 'production_scopes.json').read_text()); value['scopes'][0]['status'] = 'revoked'
        write(root / 'production_scopes.json', value)
    elif change == 'billing_provider':
        path = root / 'artifacts/openart_mcp_unknown_cost-original.json'; value = json.loads(path.read_text())
        value['provider'] = 'openart_cli'; write(path, value)
    elif change == 'request_duration':
        path = root / 'artifacts/compiled_request-original.json'; value = json.loads(path.read_text())
        value['native_binding']['native_body_sha256'] = 'f' * 64; write(path, value)
    else: (root / 'approval.txt').write_text('changed retained approval')
    with pytest.raises(ValueError): jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)


def test_uncertain_original_never_resubmits(fixture_observations, tmp_path):
    inputs, _ = prepare_project(tmp_path / 'uncertain')
    root = Path(inputs['project_dir'])
    jobs.prepare(root, attempt_id='original', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, 'original', error='synthetic connector interruption')
    assert jobs.attempt_state(root, 'original')['status'] == 'uncertain'
    with pytest.raises(ValueError): jobs.begin(root, 'original', authority_fn=execution.prepare_openart_mcp_handoff)
    with pytest.raises(ValueError): jobs.prepare(root, attempt_id='replacement', generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)


@pytest.mark.parametrize('extra', [
    {'preferred_provider': 'openart_cli'}, {'allowed_providers': ['openart_mcp', 'grok_cli']},
    {'preferred_tool': 'openart_cli_video'}, {'hosting_provider': 'openart_cli'},
    {'unknown_cost_evidence_id': 'a' * 64}, {'credit_authorization_id': 'cli-authority'},
])
def test_direct_mcp_cannot_use_foreign_route_or_cli_authority(fixture_observations, tmp_path, extra):
    inputs, _ = prepare_project(tmp_path / 'wrongroute')
    with pytest.raises(execution.ProductionGovernanceError):
        execution.prepare_openart_mcp_handoff({**inputs, **extra})


@pytest.mark.parametrize('change', ['wrong_profile', 'wrong_mode', 'wrong_request_hash', 'wrong_account_binding', 'bool_duration'])
def test_mcp_prepare_rejects_exact_identity_or_native_drift(fixture_observations, tmp_path, change):
    inputs, _ = prepare_project(tmp_path / change)
    root = Path(inputs['project_dir'])
    if change == 'wrong_profile':
        path = root / 'artifacts/compiled_request-original.json'
        value = json.loads(path.read_text()); value['native_binding']['profile_sha256'] = 'a' * 64
        write(path, value)
    elif change == 'wrong_mode': inputs['mode'] = 'image2video'
    elif change == 'wrong_request_hash':
        path = root / 'production_scopes.json'; value = json.loads(path.read_text())
        value['scopes'][0]['requests']['entry'] = 'a' * 64; write(path, value)
    elif change == 'wrong_account_binding':
        path = root / 'artifacts/openart_mcp_unknown_cost-original.json'
        value = json.loads(path.read_text()); value['account_uid_sha256'] = 'a' * 64
        write(path, value)
    else: inputs['native_params']['duration'] = True
    with pytest.raises(ValueError):
        jobs.prepare(root, attempt_id='original', generation_inputs=inputs,
                     authority_fn=execution.prepare_openart_mcp_handoff)


def pinned_selector(monkeypatch):
    from types import SimpleNamespace
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    from tools.video.video_selector import VideoSelector
    from tools.base_tool import ToolStatus
    trap = SimpleNamespace(name='forbidden_fallback', provider='forbidden', capability='video_generation',
        supports={'text_to_video': True}, input_schema={'properties': {}},
        get_info=lambda: {'model_catalog': {}}, get_status=lambda: ToolStatus.AVAILABLE,
        execute=lambda _: pytest.fail('MCP selector attempted a forbidden provider fallback'))
    selector = VideoSelector()
    monkeypatch.setattr(selector, '_providers', lambda: [OpenArtMCPVideo(), trap])
    return selector


@pytest.mark.parametrize('change', ['unknown_mode', 'unqualified_normal', 'unsupported_control', 'unsupported_role'])
def test_selector_exact_catalog_never_uses_global_flags_or_fallback(fixture_observations, tmp_path, monkeypatch, change):
    inputs, _ = prepare_project(tmp_path / change, selector=True)
    root = Path(inputs['project_dir'])
    if change == 'unknown_mode': inputs['mode'] = 'unlisted-mode'
    elif change == 'unqualified_normal':
        marker = json.loads((root / 'project.json').read_text()); marker['pipeline_type'] = 'cinematic'
        write(root / 'project.json', marker)
    elif change == 'unsupported_control': inputs['native_params']['notAProviderField'] = True
    else:
        path = root / 'unapproved.mp4'; path.write_bytes(b'fake')
        inputs['input_assets'] = [{'role': 'reference_video', 'source_path': str(path),
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'upload_id': 'unapproved'}]
    result = pinned_selector(monkeypatch).execute(inputs)
    assert not result.success
    assert result.data.get('fallback_attempted') is False
    assert result.data.get('fallback_tools') == []
    assert not list((root / 'openart_mcp/attempts').glob('*'))


def test_selector_known_candidate_only_prepares_under_explicit_qualification(fixture_observations, tmp_path, monkeypatch):
    inputs, _ = prepare_project(tmp_path / 'candidate', selector=True)
    result = pinned_selector(monkeypatch).execute(inputs)
    assert result.success, result.error
    assert list((Path(inputs['project_dir']) / 'openart_mcp/attempts').glob('*'))
    assert result.data.get('fallback_attempted') is not True
