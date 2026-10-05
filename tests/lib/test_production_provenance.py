"""Real common dispatcher/adapter with synthetic native transport, no spending."""
import json
from pathlib import Path

import pytest

from lib.production_provenance import validate_attempt_provenance

from lib.checkpoint import init_project
from lib.production_execution import ProductionGovernanceError
from tests.integration.test_first_pass_workflow import Production


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    if path.exists():
        path.chmod(0o644)
    path.write_text(json.dumps(value))


@pytest.fixture
def production(tmp_path, monkeypatch):
    root = init_project('offline-courier', title='Synthetic provenance proof', pipeline_type='cinematic',
        pipeline_dir=tmp_path, governance='strict', story_revision='courier-story-1')
    value = Production(root, monkeypatch)
    yield value
    value.network.assert_not_called()


def attempt(production):
    result = production.generate('entry')
    assert result.success, result.error
    aid = result.data['production_attempt_id']
    directory = production.root / 'production_attempts' / aid
    record = read(directory / 'result.json')
    return aid, directory, record['output']


def check(production, aid, output, shot_id='entry'):
    return validate_attempt_provenance(production.root, aid, shot_id=shot_id,
        story_revision=production.story['story_revision'], expected_output=output)


def test_real_dispatch_and_native_receipt_pass(production):
    aid, _, output = attempt(production)
    assert check(production, aid, output)['result']['status'] == 'generated'
    assert len(production.transport.native_requests) == 1


@pytest.mark.parametrize('field', ['version','scope','scope_id','cli_session_id','request_sha256','contract_sha256','input_assets','submitted_inputs','media_kind','scope_attempt_index','approval_evidence'])
def test_missing_reservation_field_blocks(production, field):
    aid, directory, output = attempt(production)
    value = read(directory / 'request.json')
    del value[field]
    save(directory / 'request.json', value)
    with pytest.raises(ProductionGovernanceError):
        check(production, aid, output)


@pytest.mark.parametrize('change', ['approval','input','preserved_output','raw_missing','receipt_missing','receipt_controls','receipt_roles','receipt_session','request_digest','contract_digest','scope_provider','scope_allowance','upstream_snapshot_missing', 'provider_request_missing', 'provider_result_missing'])
def test_missing_or_changed_retained_provenance_blocks(production, change):
    aid, directory, output = attempt(production)
    request = read(directory / 'request.json')
    result = read(directory / 'result.json')
    if change in {'approval', 'input', 'preserved_output'}:
        target = request['approval_evidence']['path'] if change == 'approval' else request['input_assets'][0]['path'] if change == 'input' else result['preserved_output']['path']
        path = Path(target); path.chmod(0o644); path.write_bytes(b'tampered synthetic evidence')
    elif change == 'raw_missing':
        (directory / 'raw_result.json').unlink()
    elif change == 'upstream_snapshot_missing':
        (directory / 'selected_attempts.json').unlink()
    elif change == 'provider_request_missing':
        (directory / 'provider_request.json').unlink()
    elif change == 'provider_result_missing':
        (directory / 'provider_result.json').unlink()
    elif change.startswith('receipt'):
        data = result['result']['data']
        if change == 'receipt_missing': del data['conditioning_receipt']
        elif change == 'receipt_controls': data['conditioning_receipt']['submitted_arguments']['prompt'] = 'unapproved story'
        elif change == 'receipt_roles': data['conditioning_receipt']['input_assets'][0]['role'] = 'reference'
        else: data['conditioning_receipt']['session_id'] = 'another-session'
        save(directory / 'result.json', result)
    else:
        if change == 'request_digest': request['request_sha256'] = '0'*64
        elif change == 'contract_digest': request['contract_sha256'] = '0'*64
        elif change == 'scope_provider': request['scope']['provider'] = 'unqualified'
        else: request['scope_attempt_index'] = request['scope']['attempts_per_shot']['entry']
        save(directory / 'request.json', request)
    with pytest.raises(ProductionGovernanceError):
        check(production, aid, output)


def test_skeletal_old_fixture_cannot_certify(production):
    aid, directory, output = attempt(production)
    request = read(directory / 'request.json')
    save(directory / 'request.json', {key: request[key] for key in ['attempt_id','project_id','story_revision','shot_id']})
    save(directory / 'result.json', {'status':'generated','result':{'success':True},'output':output})
    with pytest.raises(ProductionGovernanceError, match='missing fields'):
        check(production, aid, output)


def test_serial_observations_do_not_stale_earlier_approval(production):
    production.complete_shots()
    for sid, selection in __import__('lib.production_execution',fromlist=['load_selected_attempts']).load_selected_attempts(production.root).items():
        assert check(production, selection['attempt_id'], selection['output'], shot_id=sid)['result']['status'] == 'generated'


def test_direct_actual_grok_dispatch_has_complete_provenance(production):
    from lib.production_execution import planned_request_digest
    inputs = dict(production.inputs['entry'])
    inputs.pop('preferred_provider')
    inputs.pop('allowed_providers', None)
    production.scope['requests']['entry'] = planned_request_digest(inputs, project_dir=production.root)
    production.persist_scope()
    result = production.cli.execute(inputs)
    assert result.success, result.error
    aid = result.data['production_attempt_id']
    directory = production.root / 'production_attempts' / aid
    assert not (directory / 'provider_request.json').exists()
    assert check(production, aid, read(directory / 'result.json')['output'])['result']['status'] == 'generated'


def test_historical_binding_uses_preserved_input_not_mutable_original(production):
    aid, directory, output = attempt(production)
    request = read(directory / 'request.json')
    Path(request['input_assets'][0]['original_path']).write_bytes(b'later source mutation')
    # Current planning/final review separately revalidates current assets. The
    # historical proof must remain evidence of what was actually submitted.
    assert check(production, aid, output)['result']['status'] == 'generated'


def test_recovery_can_use_full_return_retained_in_uncertain_record(production):
    aid, directory, output = attempt(production)
    retained = read(directory / 'result.json')
    (directory / 'raw_result.json').unlink()
    save(directory / 'result.json', {'status': 'uncertain', 'result': retained['result']})
    save(directory / 'reconciliation.json', retained)
    assert check(production, aid, output)['result']['status'] == 'generated'
