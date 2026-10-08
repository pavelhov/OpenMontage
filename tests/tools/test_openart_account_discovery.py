"""Account discovery action boundaries using retained synthetic CLI receipts only."""
import json
from pathlib import Path
import pytest
from tests.lib.test_openart_jobs import captured
from tools import _openart_cli as cli
from tools.openart_account import OpenArtAccount
from lib.openart_catalog import read_observed_catalog


def body(tool, inputs):
    function = OpenArtAccount.execute
    while hasattr(function, '__wrapped__'):
        function = function.__wrapped__
    return function(tool, inputs)


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'private'))
    form = {'model': 'synthetic', 'mode': 'text2video', 'media': 'video', 'schema': {'anyOf': [
        {'type': 'object', 'properties': {'prompt': {'type': 'string', 'default': 'PRIVATE_LITERAL'},
         'duration': {'type': 'integer', 'enum': [5], 'description': 'PRIVATE_LITERAL'}}, 'required': ['prompt']},
        {'type': 'object', 'properties': {'prompt': {'type': 'string'}, 'unsupported': {'type': 'string'}}, 'required': ['unsupported']}]}}
    calls = []
    def call(argv, inputs):
        calls.append(argv)
        parsed = {'version': '0.1.1', 'fixture_sequence': len(calls)} if argv == ['version'] else (
            [{'id': 'synthetic', 'modes': {'video': [{'mode': 'text2video'}]}}] if argv == ['model', 'list'] else form)
        ref = captured('discovery', argv, parsed)
        return {**ref, 'argv': argv + cli.GLOBAL_FLAGS, 'public': parsed, '_parsed': parsed}
    monkeypatch.setattr(OpenArtAccount, '_call', staticmethod(call))
    return OpenArtAccount(), calls


def test_catalog_and_union_form_capture_private_index_without_qualification(account):
    tool, calls = account
    catalog = body(tool, {'action': 'catalog', 'read_only': True})
    assert catalog.success
    result = body(tool, {'action': 'form', 'read_only': True, 'model': 'synthetic', 'mode': 'text2video'})
    assert result.success and result.data['evidence']['form_shape'] == 'anyOf'
    assert 'PRIVATE_LITERAL' not in json.dumps(result.data)
    candidate = read_observed_catalog()['synthetic']['modes']['text2video']
    assert candidate['inspected'] is True and candidate['production_ready'] is False
    assert calls == [['version'], ['model', 'list'], ['version'], ['model', 'form', 'synthetic', 'text2video']]
    state = cli.state_dir(create=False)
    assert not (state / 'qualification').exists() and not (state / 'credit.sqlite').exists()
    assert (state / 'discovery/index.json').stat().st_mode & 0o777 == 0o600
    evidence = result.data['evidence']
    surface = body(tool, {'action': 'native_surface', 'read_only': True, 'model': 'synthetic', 'mode': 'text2video',
        'form_receipt_id': evidence['form_receipt']['receipt_id'], 'form_receipt_sha256': evidence['form_receipt']['receipt_sha256'],
        'cli_version_receipt_id': evidence['version_receipt']['receipt_id'], 'cli_version_receipt_sha256': evidence['version_receipt']['receipt_sha256']})
    assert surface.success and surface.data['evidence']['production_ready'] is False
    assert len(calls) == 4


@pytest.mark.parametrize('action', ['catalog', 'form', 'native_surface'])
def test_discovery_refuses_caller_body_or_url_before_any_call(account, action):
    tool, calls = account
    result = body(tool, {'action': action, 'read_only': True, 'request': {'url': 'https://caller.test'}})
    assert not result.success and 'invalid_argument' in result.error
    assert calls == []


@pytest.mark.parametrize('action', ['cli_help', 'transport_surface_probe'])
def test_help_and_schema_probes_have_no_caller_media_surface(account, monkeypatch, action):
    tool, calls = account
    observed = []
    def probe(**kwargs):
        observed.append(kwargs)
        return {'receipt_id': 'opaque', 'receipt_sha256': 'a' * 64,
                'public': {'body_shape': {'params': {'image': 'string'}}}, 'parsed': {'private': 'NEVER_PUBLIC'}}
    monkeypatch.setattr(cli, 'readonly_video_help', probe)
    monkeypatch.setattr(cli, 'readonly_transport_surface_probe', probe)
    base = {'action': action, 'read_only': True}
    if action == 'transport_surface_probe': base['model'] = 'synthetic'
    refused = body(tool, {**base, 'prompt': 'caller controlled prompt'})
    assert not refused.success and observed == []
    accepted = body(tool, base)
    assert accepted.success and accepted.data['evidence']['schema_only'] is True
    assert accepted.data['evidence']['unqualified_for_dispatch'] is True
    assert 'NEVER_PUBLIC' not in json.dumps(accepted.data)
    assert len(observed) == 1 and calls == []
