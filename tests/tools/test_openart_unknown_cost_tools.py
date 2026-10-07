"""Agent tool plumbing with mocked retained credit evidence; no CLI calls."""
import json
from pathlib import Path

import jsonschema
import pytest

from lib import openart_credit as credit, openart_jobs as jobs
from tools.openart_account import OpenArtAccount, READ_ONLY_ACTIONS
from tools.video.openart_cli_video import OpenArtCLIVideo

D = 'a' * 64
ACTION = 'refresh_unknown_cost_evidence'


def schema(name):
    return json.loads((Path(__file__).resolve().parents[2] / 'schemas/tools' / (name + '.schema.json')).read_text())


def test_unknown_account_action_registered_and_schema_requires_request():
    assert ACTION in READ_ONLY_ACTIONS
    doc = schema('openart_account')
    assert doc['properties'] == OpenArtAccount.input_schema['properties']
    jsonschema.validate({'action': ACTION, 'read_only': True, 'request': {}}, doc)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'action': ACTION, 'read_only': True}, doc)


def test_unknown_evidence_action_preserves_exact_summary(monkeypatch):
    request = {'model': 'qualified-model', 'mode': 'text2video',
        'native_dry_run_receipt_id': 'native-preview', 'native_dry_run_receipt_sha256': D}
    profile = {'profile_sha256': D}
    summary = {'version': '1', 'evidence_id': D, 'evidence_sha256': D,
        'requested_charge': 'unknown', 'price_classification': 'opaque',
        'observed_default_price': None, 'balance_observation': {'status': 'unknown'},
        'account_id_sha256': D, 'workspace_billing_guarantee': 'unverified',
        'native_body_sha256': D, 'profile_sha256': D, 'provider_calls': 2}
    seen = []
    def load(**kwargs):
        seen.append(('profile', kwargs))
        return profile
    def refresh(req, prof, *, timeout):
        seen.append(('refresh', req, prof, timeout))
        return summary
    monkeypatch.setattr(jobs, 'load_qualification', load)
    monkeypatch.setattr(credit, 'refresh_unknown_cost_evidence', refresh, raising=False)
    result = OpenArtAccount().execute({'action': ACTION, 'read_only': True,
        'request': request, 'timeout_seconds': 12})
    assert result.success
    assert result.data == {'action': ACTION, 'provider': 'openart', 'reservations': 0,
        'paid_submission': False, 'generation_enabled': False, 'evidence': summary}
    assert seen == [('profile', {'model': 'qualified-model', 'mode': 'text2video', 'require': 'pre_submit'}),
        ('refresh', request, profile, 12.0)]


@pytest.mark.parametrize('field', ['quote_contract', 'qualification_sha256', 'quote_id'])
@pytest.mark.parametrize('value', [None, '', {}])
def test_unknown_account_refuses_any_exact_quote_field_before_helpers(monkeypatch, field, value):
    def forbidden(*args, **kwargs):
        raise AssertionError('mixed action reached credit or profile helper')
    monkeypatch.setattr(jobs, 'load_qualification', forbidden)
    monkeypatch.setattr(credit, 'refresh_unknown_cost_evidence', forbidden, raising=False)
    result = OpenArtAccount().execute({'action': ACTION, 'read_only': True,
        'request': {'model': 'm', 'mode': 'text2video'}, field: value})
    assert not result.success and result.error.startswith('invalid_argument:')
    assert result.data['reservations'] == 0


def test_legacy_exact_refresh_dispatch_unchanged(monkeypatch):
    profile = {'profile_sha256': D}
    request = {'model': 'm', 'mode': 'text2video'}
    seen = []
    monkeypatch.setattr(jobs, 'load_qualification', lambda **kwargs: profile)
    monkeypatch.setattr(credit, 'refresh_credit_evidence', lambda req, prof, **kwargs:
        seen.append((req, prof, kwargs)) or {'quote_id': 'retained-exact'})
    result = OpenArtAccount().execute({'action': 'refresh_quote', 'read_only': True,
        'request': request, 'qualification_sha256': D, 'quote_id': 'original-exact'})
    assert result.success and result.data['evidence'] == {'quote_id': 'retained-exact'}
    assert seen == [(request, profile, {'qualification_sha256': D,
        'approved_quote_id': 'original-exact', 'timeout': 30.0})]


def video_inputs():
    return {'prompt': 'approved', 'model': 'm', 'mode': 'text2video',
        'duration': 5, 'aspect_ratio': '16:9', 'resolution': '720p',
        'output_path': '/tmp/offline-unused.mp4',
        'governance': {'shot_id': 'entry', 'scope_id': 'approved'}}


def test_video_schema_and_metadata_expose_unknown_pair(monkeypatch):
    from tools.base_tool import ToolStatus
    tool = OpenArtCLIVideo()
    monkeypatch.setattr(tool, 'get_status', lambda: ToolStatus.UNAVAILABLE)
    monkeypatch.setattr(tool, '_model_catalog', lambda: {})
    metadata = tool.get_info()['input_schema']
    document = schema('openart_cli_video')
    for key in ('unknown_cost_authorization_id', 'unknown_cost_evidence_id'):
        assert metadata['properties'][key] == document['properties'][key]
    good = {**video_inputs(), 'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': D}
    jsonschema.validate(good, metadata)
    jsonschema.validate(good, document)
    jsonschema.validate(video_inputs(), document)  # Legacy exact inputs remain accepted.


@pytest.mark.parametrize('key', ['unknown_cost_authorization_id', 'unknown_cost_evidence_id'])
@pytest.mark.parametrize('value', [None, '', 'retained', D])
def test_video_schemas_refuse_lone_unknown_key(key, value):
    for document in (schema('openart_cli_video'), OpenArtCLIVideo.input_schema):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**video_inputs(), key: value}, document)


@pytest.mark.parametrize('key', ['credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256'])
@pytest.mark.parametrize('value', [None, '', D])
def test_video_schemas_refuse_every_mixed_exact_field_presence(key, value):
    mixed = {**video_inputs(), 'unknown_cost_authorization_id': 'retained',
        'unknown_cost_evidence_id': D, key: value}
    for document in (schema('openart_cli_video'), OpenArtCLIVideo.input_schema):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(mixed, document)


@pytest.mark.parametrize('evidence', [None, '', 'BAD', 'A' * 64, 'a' * 63, 'a' * 65])
def test_video_schemas_require_lowercase_evidence_digest(evidence):
    inputs = {**video_inputs(), 'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': evidence}
    for document in (schema('openart_cli_video'), OpenArtCLIVideo.input_schema):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(inputs, document)


@pytest.mark.parametrize('credit_state', ['unknown_cost_authorization_required', 'unknown_cost_evidence_retained'])
def test_video_offline_readiness_passes_unknown_summary_without_rewriting(monkeypatch, credit_state):
    from lib import openart_dispatch as dispatch
    inputs = {**video_inputs(), 'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': D}
    expected = {'preparation': {'compiled_request_id': 'compiled'}, 'credit_state': credit_state,
        'requested_charge': 'unknown', 'price_classification': 'opaque', 'provider_calls': 0, 'reservations': 0}
    seen = []
    monkeypatch.setattr(dispatch, 'offline_readiness', lambda request: seen.append(request) or expected)
    assert OpenArtCLIVideo().prepare_offline(inputs, {}) == expected
    assert seen == [inputs]


@pytest.mark.parametrize('extra', [
    {'unknown_cost_authorization_id': 'retained'},
    {'unknown_cost_evidence_id': D},
    {'unknown_cost_authorization_id': None, 'unknown_cost_evidence_id': D},
    {'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': None},
    {'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': 'bad'},
    *[{'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': D, key: value}
      for key in ('credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256')
      for value in (None, '', D)]])
def test_video_runtime_refuses_mixed_or_lone_authority_before_any_effect(monkeypatch, tmp_path, extra):
    from lib import production_execution as execution, openart_dispatch as dispatch
    from tests.lib.test_production_execution import project, write_scopes
    inputs, scope, _ = project(tmp_path, motion=True)
    inputs.update(model='m', mode='image2video', **extra)
    scope['provider'] = 'openart_cli'
    scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=tmp_path)
    write_scopes(tmp_path, scope)
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob('*') if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid authority reached preparation, reservation, or launch')
    monkeypatch.setattr(execution, '_openart_prepare', forbidden)
    monkeypatch.setattr(dispatch, 'prepare_dispatch', forbidden)
    monkeypatch.setattr(jobs, 'launch_submit', forbidden)
    monkeypatch.setattr(credit, 'refresh_unknown_cost_evidence', forbidden, raising=False)
    with pytest.raises((execution.ProductionGovernanceError, jobs.cli.OpenArtCLIError), match='invalid_argument'):
        OpenArtCLIVideo().execute(inputs)
    assert {str(path.relative_to(tmp_path)): path.read_bytes()
            for path in tmp_path.rglob('*') if path.is_file()} == before
