"""Native first; one OpenArt image only for an explicit native content-policy denial."""
import json
from pathlib import Path

import pytest

from lib import production_execution as execution, production_images as images, production_request as request
from lib.shot_contract import file_sha256
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_factory import board_factory_fixture
from tests.lib.test_production_images import PNG, host_receipt, write
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401

# Exact current nano-banana-2 jsonSchemas from read-only openart_model_form_get (2026-10-12).
_COMMON = {'prompt': {'type': 'string', 'minLength': 1},
    'imageCount': {'default': 1, 'type': 'integer', 'minimum': 1, 'maximum': 8},
    'aspectRatio': {'type': 'string', 'enum': ['21:9', '16:9', '3:2', '4:3', '5:4', '1:1', '4:5', '3:4', '2:3', '9:16'], 'default': '1:1'},
    'resolution': {'default': '1K', 'type': 'string', 'enum': ['1K', '2K', '4K']},
    'autoEnhancePrompt': {'default': False, 'type': 'boolean'}}
_IMAGE_REF = {'type': 'object', 'properties': {k: {'type': 'string'} for k in
    ('id', 'url', 'label', 'source', 'name', 'imageUrl', 'thumbnailUrl')}, 'required': ['type', 'id', 'url', 'label'],
    'additionalProperties': False}
_IMAGE_REF['properties']['type'] = {'type': 'string', 'const': 'image'}
FORMS = {'text2image': {'type': 'object', 'properties': _COMMON, 'required': ['prompt'], 'additionalProperties': False},
    'image2image': {'type': 'object', 'properties': {**_COMMON, 'visualReferences': {'maxItems': 14, 'type': 'array',
        'items': {'oneOf': [_IMAGE_REF]}, 'minItems': 1}}, 'required': ['prompt', 'visualReferences'],
        'additionalProperties': False}}
TERM = {'provider': 'openart_mcp', 'model': 'nano-banana-2', 'forms': FORMS,
        'params': {'aspectRatio': '9:16', 'imageCount': 1}}
ACCOUNT = {'uid_sha256': request.digest('fixture-mcp-account')}
POLICY = 'Request rejected by our content policy (safety system).'


def native_outcome(root, kind, message):
    state = execution.advance(root)
    action = state['action']
    assert action['kind'] == 'receive_native_image'
    call_id, envelope = action['subject'], state['host_envelope']
    if kind == 'returned':
        outcome = {'host_receipt': host_receipt(root, envelope, name=call_id,
                                                result={'isError': True, 'error': message})}
    else:
        path = root / 'notes' / (call_id + '-exception.json')
        write(path, {'call_id': call_id, 'tool_name': envelope['tool_name'],
                     'arguments': envelope['arguments'], 'exception': {'message': message}})
        outcome = {'host_exception': {'path': str(path), 'sha256': file_sha256(path)}}
    execution.complete_action(root, action, outcome=outcome)
    return call_id, envelope


def drive(root, fallback_id=None, *, until):
    """Advance; unrelated native originals succeed with fixture bytes."""
    for _ in range(20):
        action = execution.next_production_action(root)
        if action['kind'] in until or action['subject'] == fallback_id:
            return action
        if action['kind'] == 'receive_native_image':
            call = images.image_call_status(root, action['subject'])
            envelope = {'arguments': images._call(images._load(root), action['subject'])['submitted_arguments']}
            execution.complete_action(root, action, outcome={'host_receipt': host_receipt(
                root, envelope, name=call['call_id'])})
        else:
            execution.advance(root)
    pytest.fail('production did not reach ' + str(until))


HISTORY = 'hist-1'
STATUS_TOOL = 'mcp__codex_apps__openart_openart_creation_get'


def pending_then_collect(root, fallback, action, *, status='COMPLETED', resources=None):
    """Actual async shape: generate_image -> PENDING historyId, then creation_get on the same history."""
    execution.complete_action(root, action, outcome={'host_receipt': {
        'tool_name': fallback['tool_name'], 'arguments': fallback['arguments'],
        'result': {'historyId': HISTORY, 'status': 'PENDING'}, 'tool_call_id': 'gen-call-1'}})
    pending = images.image_call_status(root, fallback['call_id'])
    assert pending['state'] == 'returned' and 'output' not in pending
    downloaded = root / 'downloads/openart.png'; downloaded.parent.mkdir(exist_ok=True); downloaded.write_bytes(PNG)
    rows = resources if resources is not None else [{'id': 'res-1', 'url': 'https://fixture.invalid/res-1.png', 'mediaType': 'image'}]
    receipt = {'tool_name': STATUS_TOOL, 'arguments': {'historyId': HISTORY}, 'tool_call_id': 'get-call-1',
               'result': {'historyId': HISTORY, 'status': status, 'resources': rows}}
    download = {'path': str(downloaded), 'sha256': file_sha256(downloaded), 'resource_id': 'res-1',
                'url': 'https://fixture.invalid/res-1.png'}
    return receipt, download


@pytest.mark.parametrize('kind', ['returned', 'raised'])
def test_policy_denial_fallback_imports_promotes_and_reaches_motion(neutral_package, kind):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, envelope = native_outcome(root, kind, POLICY)
    original = images.image_call_status(root, denied)
    before = images.image_usage(root)['total']
    fallback = images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert fallback['tool_name'] == 'mcp__codex_apps__openart_openart_generate_image'
    assert fallback['arguments'] == {'model': 'nano-banana-2', 'mode': 'text2image', 'projectId': 'fixture-mcp',
        'params': {'aspectRatio': '9:16', 'imageCount': 1, 'prompt': envelope['arguments']['prompt']}}
    assert images.image_usage(root)['total'] == before + 1
    with pytest.raises(ValueError, match='one fallback'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    action = drive(root, fallback['call_id'], until=set())
    assert action['kind'] == 'receive_native_image'
    receipt, download = pending_then_collect(root, fallback, action)
    # Pending same call resumes collection; it never re-emits generate_image.
    resumed = execution.next_production_action(root)
    assert resumed['subject'] == fallback['call_id'] and resumed['kind'] == 'receive_native_image'
    assert resumed['inputs']['action'] == 'collect_openart_image' and resumed['inputs']['history_id'] == HISTORY
    assert 'host_envelope' not in execution.advance(root)
    assert images.image_usage(root)['total'] == before + 1
    imported = images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt, download=download)
    assert imported['state'] == 'imported' and imported['route'] == 'openart_mcp'
    assert imported['output']['path'].endswith('-policy-fallback.png')
    assert imported['collection']['history_id'] == HISTORY
    assert imported['replaces_policy_denial']['call_id'] == denied
    gen = json.loads((root / imported['host_receipt']['path']).read_text())
    assert gen['tool_name'] == fallback['tool_name'] and gen['result']['status'] == 'PENDING'
    assert imported['provenance']['host_tool_call_id'] == 'gen-call-1'
    assert images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt,
                                                   download=download) == imported
    with pytest.raises(ValueError, match='different collection'):
        images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt,
                                               download={**download, 'resource_id': 'res-2'})
    action = drive(root, until={'prepare_motion'})
    from lib import production_boards as boards
    binding = boards.load_board_pack(root)['board_bindings'][original['slot_id']]
    assert binding['call_id'] == fallback['call_id'] and binding['route'] == 'openart_mcp'
    assert images.image_call_status(root, denied) == original
    usage = images.image_usage(root)
    assert sum(r['route'] == 'openart_mcp' for r in usage['occurrences']) == 1


def _pending_fallback(neutral_package):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    fallback = images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    action = drive(root, fallback['call_id'], until=set())
    return root, fallback, action


@pytest.mark.parametrize('damage', ['history', 'status', 'no_resource', 'extra_resource', 'url', 'sha', 'tool'])
def test_collection_refuses_other_history_incomplete_or_unmatched_download(neutral_package, damage):
    root, fallback, action = _pending_fallback(neutral_package)
    receipt, download = pending_then_collect(root, fallback, action)
    result = receipt['result']
    if damage == 'history':
        receipt['arguments'] = {'historyId': 'other'}; result['historyId'] = 'other'
    elif damage == 'status':
        result['status'] = 'RUNNING'
    elif damage == 'no_resource':
        result['resources'] = []
    elif damage == 'extra_resource':
        result['resources'].append({'id': 'res-2', 'url': 'https://fixture.invalid/2.png', 'mediaType': 'image'})
    elif damage == 'url':
        download['url'] = 'https://fixture.invalid/elsewhere.png'
    elif damage == 'sha':
        download['sha256'] = '0' * 64
    else:
        receipt['tool_name'] = 'mcp__codex_apps__openart_openart_generate_image'
    before = images._load(root)
    with pytest.raises(ValueError):
        images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt, download=download)
    assert images._load(root) == before


def test_terminal_failed_collection_is_consumed_without_retry(neutral_package):
    root, fallback, action = _pending_fallback(neutral_package)
    receipt, download = pending_then_collect(root, fallback, action, status='FAILED', resources=[])
    total = images.image_usage(root)['total']
    with pytest.raises(ValueError, match='no downloadable resource'):
        images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt, download=download)
    failed = images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt)
    assert images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt) == failed
    assert failed['state'] == 'failed' and failed['collection']['history_id'] == HISTORY
    assert images.image_usage(root)['total'] == total
    with pytest.raises(ValueError, match='one fallback'):
        images.prepare_policy_fallback_image(root, failed['replaces_policy_denial']['call_id'], account=ACCOUNT)


def test_image_count_above_one_refuses_without_mutation(neutral_package):
    root = board_factory_fixture(neutral_package, image_policy_fallback={**TERM, 'params': {'imageCount': 8}},
                                 text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    before = (images.image_usage(root), images._load(root))
    with pytest.raises(ValueError, match='exactly one image'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert (images.image_usage(root), images._load(root)) == before


@pytest.mark.parametrize('field', ['request_sha256', 'prompt'])
def test_stale_or_forged_original_refuses_before_envelope(neutral_package, field):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    value = images._load(root); call = images._call(value, denied)
    if field == 'request_sha256':
        call['request_sha256'] = '0' * 64
    else:
        call['submitted_arguments']['prompt'] = 'forged other prompt'
    images._write(root, value)
    before = (images.image_usage(root), images._load(root))
    with pytest.raises(ValueError, match='no longer matches'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert (images.image_usage(root), images._load(root)) == before


def test_late_authentic_original_is_preserved_without_displacing_fallback(neutral_package):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, envelope = native_outcome(root, 'raised', POLICY)
    original = images.image_call_status(root, denied)
    fallback = images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    action = drive(root, fallback['call_id'], until=set())
    receipt, download = pending_then_collect(root, fallback, action)
    images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt, download=download)
    drive(root, until={'prepare_motion'})
    late = images.import_native_image(root, denied, host_receipt=host_receipt(root, envelope, name='late-original'))
    fb = images.image_call_status(root, fallback['call_id'])
    assert late['state'] == 'imported' and late['output']['path'] != fb['output']['path']
    assert (root / late['output']['path']).exists() and (root / fb['output']['path']).read_bytes() == PNG
    from lib import production_boards as boards
    # Advance through the scheduler's promotion pass: the late original is never promoted.
    action = drive(root, until={'prepare_motion'})
    assert action['kind'] == 'prepare_motion'
    pack = boards.load_board_pack(root)
    assert boards.imported_board_eligibility(root, pack, original['slot_id'], denied, images=images) == 'successor'
    assert pack['board_bindings'][original['slot_id']]['call_id'] == fallback['call_id']
    assert execution.resolve_board_input(root, {'asset_id': original['slot_id']})['sha256'] == fb['output']['sha256']


@pytest.mark.parametrize('kind,message', [
    ('returned', 'http 403 Forbidden'), ('raised', 'http 403 Forbidden'),
    ('raised', 'content policy check timed out'), ('returned', 'access denied by moderation gateway'),
    ('returned', 'moderation service unavailable'), ('raised', 'safety system timeout'),
    ('returned', 'content policy service unavailable'), ('returned', 'SYNTHETIC unknown error')])
def test_non_policy_failures_never_admit_openart(neutral_package, kind, message):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, _ = native_outcome(root, kind, message)
    before = images.image_usage(root)
    with pytest.raises(ValueError, match='explicit native content-policy denial'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert images.image_usage(root) == before


def test_run_without_explicit_term_refuses(neutral_package):
    root = board_factory_fixture(neutral_package, text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    with pytest.raises(ValueError, match='grants no OpenArt image policy fallback'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)


def test_wrong_account_refuses(neutral_package):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    with pytest.raises(ValueError, match='account differs'):
        images.prepare_policy_fallback_image(root, denied, account={'uid_sha256': '0' * 64})


def test_term_params_outside_current_form_refuse_before_envelope(neutral_package):
    term = {**TERM, 'params': {'aspectRatio': '7:3'}}
    root = board_factory_fixture(neutral_package, image_policy_fallback=term, text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    before = images.image_usage(root)
    with pytest.raises(ValueError, match='retained current form'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert images.image_usage(root) == before


@pytest.fixture
def mcp_account(tmp_path, monkeypatch):
    """Isolated synthetic connector account matching the fixture standing UID."""
    import hashlib
    discovery = tmp_path / 'discovery'; discovery.mkdir(mode=0o700)
    raw = json.dumps({'classification': 'synthetic_fixture', 'data': {'user': {'uid': 'fixture-mcp-account'}}}).encode()
    path = discovery / '2026-10-07-account-observation.json'; path.write_bytes(raw); path.chmod(0o600)
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT', hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR', str(discovery))
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'state'))


def test_reference_guided_denial_uploads_then_uses_image2image_same_prompt(neutral_package, mcp_account):
    """Board refs transfer through existing standing upload receipts; never a silent text-only fallback."""
    from lib import openart_mcp_jobs as jobs, production_autonomy as autonomy
    from tests.lib.test_policy_image_fallback_upload import upload_result
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM)
    denied, envelope = native_outcome(root, 'returned', POLICY)
    refs = images._call(images._load(root), denied)['host_arguments']['referenced_image_paths']
    assert refs
    before = (images.image_usage(root), images._load(root))
    need = images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert need['provider_calls'] == 0 and (images.image_usage(root), images._load(root)) == before
    args = need['upload_required']
    assert args['policy_fallback_call_id'] == denied and args['files'] == [str(Path(p)) for p in args['files']]
    assert len(args['files']) == len(refs)
    authority = autonomy.derive_standing_upload_authority(root, **args)
    upload = jobs.prepare_upload(root, files=args['files'], openart_project_id='fixture-mcp',
        billing_declaration={'upload_authorization_id': authority['upload_id']})
    jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=upload_result(args['files']))
    fallback = images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert fallback['arguments']['mode'] == 'image2image'
    params = fallback['arguments']['params']
    assert params['prompt'] == envelope['arguments']['prompt']
    assert [r['id'] for r in params['visualReferences']] == ['fixture-ref-' + str(i) for i in range(len(refs))]
    call = images._call(images._load(root), fallback['call_id'])
    assert len(call['replaces_policy_denial']['upload_receipt_sha256s']) == len(refs)
    # Same normal reference-board path through async collection, promotion and motion.
    slot = images.image_call_status(root, denied)['slot_id']
    action = drive(root, fallback['call_id'], until=set())
    receipt, download = pending_then_collect(root, fallback, action)
    imported = images.record_openart_image_collection(root, fallback['call_id'], status_receipt=receipt, download=download)
    assert drive(root, until={'prepare_motion'})['kind'] == 'prepare_motion'
    from lib import production_boards as boards
    binding = boards.load_board_pack(root)['board_bindings'][slot]
    assert binding['call_id'] == fallback['call_id'] and binding['sha256'] == imported['output']['sha256']
    frame = execution.resolve_board_input(root, {'asset_id': slot})
    assert frame['sha256'] == imported['output']['sha256'] and frame['path'].endswith('-policy-fallback.png')


def native_raw(root, *, result=None, exception=None):
    """Synthetic native failure with an exact retained return or raised exception payload."""
    state = execution.advance(root)
    action = state['action']; call_id, envelope = action['subject'], state['host_envelope']
    if result is not None:
        outcome = {'host_receipt': host_receipt(root, envelope, name=call_id, result=result)}
    else:
        path = root / 'notes' / (call_id + '-exception.json')
        write(path, {'call_id': call_id, 'tool_name': envelope['tool_name'],
                     'arguments': envelope['arguments'], 'exception': exception})
        outcome = {'host_exception': {'path': str(path), 'sha256': file_sha256(path)}}
    execution.complete_action(root, action, outcome=outcome)
    return call_id


REAL = ('This image generation request did not follow our content policy.',
        'Your request does not comply with our content policy.')


@pytest.mark.parametrize('kind', ['returned', 'raised', 'text_block', 'json_text_block'])
@pytest.mark.parametrize('message', REAL)
def test_actual_refusal_wording_admits_fallback(neutral_package, kind, message):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    if kind == 'raised':
        denied = native_raw(root, exception={'message': message})
    elif kind == 'returned':
        denied = native_raw(root, result={'isError': True, 'error': message})
    else:
        text = message if kind == 'text_block' else json.dumps({'error': {'code': 'content_policy_violation', 'message': message}})
        denied = native_raw(root, result={'isError': True, 'content': [{'type': 'text', 'text': text}]})
    fallback = images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert fallback['tool_name'] == 'mcp__codex_apps__openart_openart_generate_image'


ECHO = 'SYNTHETIC prompt that did not follow our content policy and violates our content policy'


@pytest.mark.parametrize('payload', [
    {'result': {'isError': True, 'error': 'unknown', 'prompt': ECHO, 'metadata': {'note': ECHO}}},
    {'result': {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(
        {'error': 'unknown', 'prompt': ECHO, 'metadata': {'echo': ECHO}})}]}},
    {'result': {'isError': True, 'error': {'code': 'unknown', 'message': 'internal error'}, 'arguments': {'prompt': ECHO}}}])
def test_echoed_prompt_or_metadata_never_counts_as_denial(neutral_package, payload):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied = native_raw(root, **payload)
    before = (images.image_usage(root), images._load(root))
    with pytest.raises(ValueError, match='explicit native content-policy denial'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert (images.image_usage(root), images._load(root)) == before


def test_exhausted_image_allowance_refuses_before_reservation(neutral_package, monkeypatch):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM, text_only_boards=True)
    denied, _ = native_outcome(root, 'returned', POLICY)
    limit = images._scope(root, images._call(images._load(root), denied)['scope_id'])['image_allowance']
    real = images.image_usage
    # Remaining episode allowance already consumed by other counted images.
    monkeypatch.setattr(images, 'image_usage', lambda r: {**real(r), 'total': limit})
    before = images._load(root)
    with pytest.raises(ValueError, match='approved episode image allowance exhausted'):
        images.prepare_policy_fallback_image(root, denied, account=ACCOUNT)
    assert images._load(root) == before
