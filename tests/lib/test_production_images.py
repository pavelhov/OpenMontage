"""Offline canonical image calls. Receipts use explicit synthetic host bytes."""
import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib import production_images as images
from lib.production_request import digest
from lib.shot_contract import file_sha256


# A real one-pixel PNG fixture, retained byte-for-byte rather than re-encoded.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQkdMAAACkAFt2Ll/jAAAAAElFTkSuQmCC')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def board_package(tmp_path):
    root = tmp_path / 'boards'; root.mkdir()
    write(root / 'project.json', {'project_id':'boards', 'story_revision':'story-1',
        'governance':{'version':'1.0','mode':'strict'}})
    story = root / 'artifacts/script.json'
    write(story, {'version':'1.0','title':'Synthetic story', 'total_duration_seconds':2,
        'sections':[{'id':'s1','text':'The doctor arrives.','start_seconds':0,'end_seconds':2}]})
    approval = root / 'approval.txt'; approval.write_text('Synthetic approval for this story and exact image requests.')
    source = root / 'assets/images/doctor.png'; source.parent.mkdir(parents=True); source.write_bytes(PNG)
    review = {'review_id':'r1','reviewer':'synthetic-fixture','story_revision':'story-1',
        'subject_sha256':file_sha256(source),'status':'pass', 'predicates':[
            {'name':'identity','status':'pass','severity':'critical','evidence':'Synthetic fixture only.'}]}
    reference = {'id':'doctor','role':'identity_reference','path':'assets/images/doctor.png',
        'sha256':file_sha256(source),'review':review, 'applicability':{'story_revision':'story-1',
            'roles':['identity_reference','start_frame']}}
    packet = images.build_preboard_packet(root, story={'path':'artifacts/script.json','sha256':file_sha256(story)},
        approval={'path':'approval.txt','sha256':file_sha256(approval),'approved_by':'synthetic-fixture'},
        references=[reference], board_slots=[{'id':'s1-start','shot_id':'s1','role':'start_frame','status':'unresolved'}])
    args = {'prompt':'Synthetic doctor arriving.', 'referenced_image_paths':[str(source)],'transparent_background':False}
    target = 'assets/images/s1-start.png'
    request_sha = images.image_request_digest(packet, 's1-start', args, output_path=target)
    scope = {'id':'image-episode','status':'approved','approved_by':'synthetic-fixture',
        'project_id':'boards','story_revision':'story-1','phase':'image','provider':'native_imagegen',
        'evidence':{'path':'approval.txt','sha256':file_sha256(approval)},
        'image_allowance':2,'allowed_image_routes':['native_imagegen','grok_cli'],
        'requests':{'s1-start':request_sha,'s1-end':request_sha}, 'attempts_per_shot':{'s1-start':2,'s1-end':2}}
    write(root / 'production_scopes.json', {'version':'1.0','scopes':[scope]})
    return root, packet, args, target


def reserve(package, **kwargs):
    root, packet, args, target = package
    return images.reserve_native_image(root, packet, 's1-start', args, output_path=target,
        scope_id='image-episode', **kwargs)


def host_receipt(root, envelope, *, name='host-return', result=None):
    result = result or {'image_url':'data:image/png;base64,' + base64.b64encode(PNG).decode(),
        'output_hint':'Synthetic fixture'}
    path = root / 'receipts' / (name + '.json')
    write(path, {'tool_name':'image_gen.imagegen','arguments':envelope['arguments'],
        'result':result, 'tool_call_id':'actual-synthetic-host-call'})
    return {'path':str(path),'sha256':file_sha256(path)}


def test_preboard_reuse_and_changed_reference_binding(board_package):
    root, packet, args, _ = board_package
    reused = images.validate_image_reuse(root, packet['references'][0], role='start_frame')
    assert reused['sha256'] == file_sha256(root/'assets/images/doctor.png')
    assert images.image_usage(root)['total'] == 0
    assert packet['board_slots'][0]['status'] == 'unresolved'
    assert not (root/'artifacts/shot_contract.json').exists()
    (root/'assets/images/doctor.png').write_bytes(PNG + b'changed')
    with pytest.raises(ValueError, match='reference bytes changed'):
        reserve(board_package)
    assert images.image_usage(root)['total'] == 0


def test_native_one_envelope_real_return_original_bytes_and_resume(board_package):
    root, packet, args, target = board_package
    call = reserve(board_package, call_id='local-call-1')
    assert 'arguments' not in call, 'reserve must not grant invocation before begin'
    assert call['state'] == 'reserved'
    envelope = images.begin_native_image(root, call['call_id'])
    assert envelope['tool_name'] == 'image_gen.imagegen'
    assert envelope['arguments']['prompt'] == args['prompt']
    assert 'n' not in envelope['arguments']
    with pytest.raises(ValueError, match='already emitted'):
        images.begin_native_image(root, call['call_id'])
    receipt = host_receipt(root, envelope)
    imported = images.import_native_image(root, call['call_id'], host_receipt=receipt)
    assert imported['state'] == 'imported'
    assert (root/target).read_bytes() == PNG
    assert (root/imported['output']['preserved_path']).read_bytes() == PNG
    assert imported['provenance']['kind'] == 'agent_recorded_host_return'
    assert 'provider_job_id' not in imported
    assert images.import_native_image(root, call['call_id'], host_receipt=receipt) == imported
    usage = images.image_usage(root)
    assert usage['total'] == 1 and usage['unresolved_originals'] == []
    assert [event['kind'] for event in imported['events']] == ['packet_ready','reserved','submitted','tool_return','imported']
    assert images.image_usage(root) == usage, 'status is free'


def test_allowance_atomic_and_uncertainty_blocks_same_slot(board_package):
    root, packet, args, target = board_package
    scope_path = root/'production_scopes.json'
    packet = copy.deepcopy(packet)
    packet['board_slots'].append({'id':'s1-end','shot_id':'s1','role':'end_frame','status':'unresolved'})
    packet['packet_sha256'] = digest({k:v for k,v in packet.items() if k!='packet_sha256'})
    scopes = json.loads(scope_path.read_text()); scopes['scopes'][0]['image_allowance'] = 1
    for slot in ['s1-start','s1-end']:
        scopes['scopes'][0]['requests'][slot] = images.image_request_digest(packet,slot,args,output_path=target)
    write(scope_path, scopes)
    def attempt(slot_id):
        try:
            return images.reserve_native_image(root,packet,slot_id,args,output_path=target,scope_id='image-episode',call_id=slot_id)['call_id']
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['s1-start','s1-end']))
    assert len([item for item in results if item]) == 1
    assert images.image_usage(root)['total'] == 1
    call_id = next(item for item in results if item)
    images.mark_image_uncertain(root, call_id, reason='Interrupted before attributable return.')
    with pytest.raises(ValueError, match='uncertain|pending'):
        images.reserve_native_image(root,packet,call_id,args,output_path=target,scope_id='image-episode',call_id='duplicate')
    assert images.image_usage(root)['total'] == 1


def test_never_submitted_only_before_envelope_and_failed_calls_count(board_package):
    root, *_ = board_package
    call = reserve(board_package, call_id='not-invoked')
    released = images.release_unsubmitted_image(root, call['call_id'], reason='Producer cancelled before host handoff.')
    assert released['state'] == 'never_submitted'
    assert images.image_usage(root)['total'] == 0
    assert images.image_usage(root)['excluded_never_submitted'] == [call['call_id']]
    call = reserve(board_package, call_id='terminal-error')
    envelope = images.begin_native_image(root, call['call_id'])
    with pytest.raises(ValueError, match='envelope'):
        images.release_unsubmitted_image(root, call['call_id'], reason='Cannot prove invocation absence.')
    failed = images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope,
        result={'error':'Synthetic terminal host failure','isError':True}))
    assert failed['state'] == 'failed'
    assert images.image_usage(root)['total'] == 1
    assert images.image_usage(root)['unresolved_originals'] == []


def test_import_requires_actual_argument_and_result_binding(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root, call['call_id'])
    receipt = host_receipt(root, envelope)
    path = root/receipt['path']
    data = json.loads(path.read_text()); data['arguments']['prompt'] = 'Different call'; write(path, data)
    receipt['sha256'] = file_sha256(path)
    with pytest.raises(ValueError, match='arguments'):
        images.import_native_image(root, call['call_id'], host_receipt=receipt)
    assert images.image_usage(root)['total'] == 1
    assert not (root/'assets/images/s1-start.png').exists()


def test_grok_shared_allowance_retains_exact_scope_and_session(board_package):
    root, packet, args, target = board_package
    from lib.production_execution import planned_request_digest
    grok_inputs = {'prompt':'Synthetic fallback','operation':'image_edit',
        'image_paths':[str(root/'assets/images/doctor.png')],'allow_unknown_cost':True,
        'output_path':str(root/'assets/images/fallback.png'),'project_dir':str(root),
        'governance':{'scope_id':'grok-image','shot_id':'s1-start'}}
    sha = planned_request_digest(grok_inputs, project_dir=root)
    scopes = json.loads((root/'production_scopes.json').read_text())
    scope = {**scopes['scopes'][0], 'id':'grok-image','provider':'grok_cli',
        'requests':{'s1-start':sha},'attempts_per_shot':{'s1-start':1},'image_allowance_scope_id':'image-episode'}
    scopes['scopes'].append(scope); write(root/'production_scopes.json',scopes)
    native = reserve(board_package, call_id='native-first')
    envelope = images.begin_native_image(root,native['call_id'])
    images.import_native_image(root,native['call_id'],host_receipt=host_receipt(root,envelope,
        result={'error':'Synthetic failure','isError':True}))
    assert images.check_grok_image_admission(root, scope=scope, shot_id='s1-start', request_sha256=sha)['remaining'] == 1
    # Existing canonical Grok reservation/session, not a new image ledger counter.
    write(root/'production_attempts/grok-original/request.json', {'version':'1.0','attempt_id':'grok-original',
        'cli_session_id':'grok-original','project_id':'boards','story_revision':'story-1',
        'shot_id':'s1-start','scope_id':'grok-image','phase':'image','media_kind':'image',
        'tool_name':'grok_cli_image','request_sha256':sha,'scope':scope,'submitted_inputs':grok_inputs})
    assert images.image_usage(root)['total'] == 2
    assert images.image_usage(root)['unresolved_originals'] == ['grok-original']
    with pytest.raises(ValueError, match='uncertain|pending|allowance'):
        images.check_grok_image_admission(root,scope=scope,shot_id='s1-start',request_sha256=sha)
    detached = copy.deepcopy(scope); detached.pop('image_allowance_scope_id')
    with pytest.raises(ValueError, match='shared'):
        images.check_grok_image_admission(root,scope=detached,shot_id='s1-start',request_sha256=sha)


@pytest.mark.parametrize('native_state', ['exhausted', 'pending'])
def test_grok_dry_run_rejects_native_shared_admission_before_reservation(board_package, native_state):
    from lib.production_execution import ProductionGovernanceError, governed_dry_run, planned_request_digest
    from tests.lib.test_production_execution import ImageTool

    root, packet, args, target = board_package
    scope_path = root/'production_scopes.json'
    scopes = json.loads(scope_path.read_text())
    scopes['scopes'][0]['image_allowance'] = 1
    scope = {**scopes['scopes'][0], 'id':'grok-image', 'provider':'grok_cli',
        'image_allowance_scope_id':'image-episode', 'requests':{},
        'attempts_per_shot':{'s1-start':1}}
    grok_inputs = {'prompt':'Synthetic fallback', 'operation':'image_edit',
        'image_paths':[str(root/'assets/images/doctor.png')], 'allow_unknown_cost':True,
        'output_path':str(root/'assets/images/fallback.png'), 'project_dir':str(root),
        'governance':{'scope_id':'grok-image','shot_id':'s1-start'}}
    sha = planned_request_digest(grok_inputs, project_dir=root)
    scope['requests'] = {'s1-start':sha}
    scopes['scopes'].append(scope)
    write(scope_path, scopes)

    native = reserve(board_package, call_id='native-'+native_state)
    if native_state == 'pending':
        images.begin_native_image(root, native['call_id'])
    else:
        envelope = images.begin_native_image(root, native['call_id'])
        images.import_native_image(root, native['call_id'], host_receipt=host_receipt(root, envelope,
            result={'error':'Synthetic terminal host failure','isError':True}))

    tool = ImageTool()
    tool.provider = 'grok_cli'
    before = images.image_usage(root)
    with pytest.raises(ProductionGovernanceError, match='allowance|pending|uncertain'):
        governed_dry_run(tool, grok_inputs)
    assert tool.calls == 0
    assert images.image_usage(root) == before
    assert not (root/'production_attempts').exists()


def test_grok_dry_run_allows_remaining_shared_image_capacity(board_package):
    from lib.production_execution import governed_dry_run, planned_request_digest
    from tests.lib.test_production_execution import ImageTool

    root, packet, args, target = board_package
    grok_inputs = {'prompt':'Synthetic fallback', 'operation':'image_edit',
        'image_paths':[str(root/'assets/images/doctor.png')], 'allow_unknown_cost':True,
        'output_path':str(root/'assets/images/fallback.png'), 'project_dir':str(root),
        'governance':{'scope_id':'grok-image','shot_id':'s1-start'}}
    scope = json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope = {**scope, 'id':'grok-image', 'provider':'grok_cli',
        'image_allowance_scope_id':'image-episode', 'requests':{},
        'attempts_per_shot':{'s1-start':1}}
    sha = planned_request_digest(grok_inputs, project_dir=root)
    scope['requests'] = {'s1-start':sha}
    scopes = json.loads((root/'production_scopes.json').read_text())
    scopes['scopes'].append(scope)
    write(root/'production_scopes.json', scopes)

    native = reserve(board_package, call_id='native-completed')
    envelope = images.begin_native_image(root, native['call_id'])
    images.import_native_image(root, native['call_id'], host_receipt=host_receipt(root, envelope,
        result={'error':'Synthetic terminal host failure','isError':True}))
    tool = ImageTool()
    tool.provider = 'grok_cli'
    result = governed_dry_run(tool, grok_inputs)
    assert result['would_execute'] is True and result['provider_calls'] == 0 and result['reservations'] == 0
    assert tool.calls == 0
    assert not (root/'production_attempts').exists()


def test_closed_schema_roundtrip_and_native_no_batch(board_package):
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    root, packet, args, target = board_package
    call = reserve(board_package)
    artifact = json.loads((root/'artifacts/production_image_calls.json').read_text())
    validator = Draft202012Validator(load_schema('production_image_calls'))
    validator.validate(artifact)
    artifact['calls'][0]['provider_job_id'] = 'invented-local-provider-id'
    with pytest.raises(Exception):
        validator.validate(artifact)
    with pytest.raises(ValueError, match='native.*arguments|unsupported'):
        images.image_request_digest(packet,'s1-start',{**args,'n':2},output_path=target)


def test_returned_local_artifact_is_attributable_and_original(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root,call['call_id'])
    original = root.parent/'actual-host-image.png'; original.write_bytes(PNG)
    receipt = host_receipt(root,envelope,result={'image_path':str(original),'output_hint':'Synthetic local host artifact'})
    imported = images.import_native_image(root,call['call_id'],host_receipt=receipt)
    assert imported['output']['source'] == {'kind':'local_artifact','field':'image_path','path':str(original)}
    assert (root/imported['output']['path']).read_bytes() == original.read_bytes()
    (root/imported['output']['path']).write_bytes(PNG+b'stale replacement')
    with pytest.raises(ValueError,match='imported image bytes changed'):
        images.import_native_image(root,call['call_id'],host_receipt=receipt)
    assert images.image_usage(root)['total'] == 1


def test_uncertain_reservation_can_collect_original_return_without_new_call(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root,call['call_id'])
    images.mark_image_uncertain(root,call['call_id'],reason='Original host call returned while producer was interrupted.')
    receipt = host_receipt(root,envelope)
    collected = images.import_native_image(root,call['call_id'],host_receipt=receipt)
    assert collected['state'] == 'imported'
    assert images.image_usage(root)['total'] == 1
    assert len(json.loads((root/'artifacts/production_image_calls.json').read_text())['calls']) == 1


def test_unsupported_actual_return_retains_receipt_and_uncertainty(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root,call['call_id'])
    receipt = host_receipt(root,envelope,result={'output_hint':'Unattributable prose is not an image path.'})
    with pytest.raises(ValueError,match='unresolved'):
        images.import_native_image(root,call['call_id'],host_receipt=receipt)
    saved = json.loads((root/'artifacts/production_image_calls.json').read_text())['calls'][0]
    assert saved['state'] == 'uncertain'
    assert saved['host_receipt']['sha256'] == receipt['sha256']
    assert (root/saved['host_receipt']['path']).read_bytes() == Path(receipt['path']).read_bytes()
    assert images.image_usage(root)['unresolved_originals'] == [call['call_id']]


def test_native_status_reports_stale_current_output_without_new_generation(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root,call['call_id'])
    images.import_native_image(root,call['call_id'],host_receipt=host_receipt(root,envelope))
    status = images.image_call_status(root,call['call_id'])
    assert status['state'] == 'imported' and status['output_current'] is True
    (root/status['output']['path']).write_bytes(PNG+b'changed current board')
    stale = images.image_call_status(root,call['call_id'])
    assert stale['state'] == 'imported' and stale['output_current'] is False
    assert stale['preserved_output_valid'] is True
    assert 'host_arguments' not in stale
    assert images.image_usage(root)['total'] == 1


def test_reservation_resume_keeps_bounded_shape_and_mcp_error_content(board_package):
    root, *_ = board_package
    call = reserve(board_package,call_id='bounded-resume')
    resumed = reserve(board_package,call_id='bounded-resume')
    assert resumed == call
    assert 'packet' not in resumed and 'host_arguments' not in resumed
    envelope = images.begin_native_image(root,call['call_id'])
    receipt = host_receipt(root,envelope,result={'isError':True,
        'content':[{'type':'text','text':'Synthetic host call failed before image output.'}]})
    failed = images.import_native_image(root,call['call_id'],host_receipt=receipt)
    assert failed['state'] == 'failed'
    assert 'Synthetic host call failed' in failed['error']
    assert images.image_usage(root)['total'] == 1
    assert images.image_usage(root)['unresolved_originals'] == []


def exception_receipt(root, envelope, *, name='host-exception'):
    path = root / 'receipts' / (name + '.json')
    write(path, {'call_id':envelope['call_id'], 'tool_name':envelope['tool_name'],
        'arguments':envelope['arguments'], 'exception':{
            'message':'image generation failed: connection failed: error sending request',
            'type':'SyntheticTransportError'}, 'tool_call_id':'synthetic-exception-call'})
    return {'path':str(path), 'sha256':file_sha256(path)}


def test_host_exception_retained_unknown_counted_and_delayed_original_collects(board_package):
    root, *_ = board_package
    call = reserve(board_package, call_id='transport-exception')
    envelope = images.begin_native_image(root, call['call_id'])
    receipt = exception_receipt(root, envelope)
    observed = images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt)
    assert observed['state'] == 'uncertain'
    assert 'host_receipt' not in observed and 'provenance' not in observed
    assert 'output' not in observed and 'never_submitted_proof' not in observed
    assert (root/observed['host_exception']['path']).read_bytes() == Path(receipt['path']).read_bytes()
    assert observed['host_exception']['sha256'] == receipt['sha256']
    assert observed['host_exception']['kind'] == 'agent_recorded_host_exception'
    assert observed['host_exception']['arguments_sha256'] == envelope['arguments_sha256']
    status = images.image_call_status(root, call['call_id'])
    assert status['backend_submission_status'] == 'unknown'
    assert status['next_action'] == 'collect_original_or_authoritative_reconciliation'
    assert images.image_usage(root)['total'] == 1
    assert images.image_usage(root)['unresolved_originals'] == [call['call_id']]
    assert images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt) == observed
    with pytest.raises(ValueError, match='pending|uncertain'):
        reserve(board_package, call_id='forbidden-replacement')
    with pytest.raises(ValueError, match='envelope'):
        images.release_unsubmitted_image(root, call['call_id'], reason='Only three seconds elapsed.')
    with pytest.raises(ValueError, match='already emitted'):
        images.begin_native_image(root, call['call_id'])
    collected = images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope))
    assert collected['state'] == 'imported'
    assert collected['host_exception'] == observed['host_exception']
    assert images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt) == collected
    assert images.image_usage(root)['total'] == 1
    assert images.image_usage(root)['unresolved_originals'] == []
    assert 'backend_submission_status' not in images.image_call_status(root, call['call_id'])


@pytest.mark.parametrize('field,value', [
    ('call_id','another-call'), ('tool_name','another.tool'),
    ('arguments',{'prompt':'Different request'}), ('result',{'isError':True}),
    ('exception',{'message':' ', 'terminal':True}), ('exception',{'message':' '}),
    ('tool_call_id','')])
def test_host_exception_rejects_unbound_or_forged_evidence(board_package, field, value):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root, call['call_id'])
    receipt = exception_receipt(root, envelope)
    path = Path(receipt['path']); payload = json.loads(path.read_text()); payload[field] = value
    write(path, payload); receipt['sha256'] = file_sha256(path)
    with pytest.raises(ValueError):
        images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt)
    assert images.image_call_status(root, call['call_id'])['state'] == 'submitted'
    assert not (root/'production_images'/call['call_id']/'host-exception.json').exists()


def test_host_exception_requires_emission_hash_and_immutable_original(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    fake_envelope = {'call_id':call['call_id'], 'tool_name':images.NATIVE_TOOL, 'arguments':{'prompt':'Synthetic'}}
    receipt = exception_receipt(root, fake_envelope)
    with pytest.raises(ValueError, match='envelope'):
        images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt)
    envelope = images.begin_native_image(root, call['call_id'])
    receipt = exception_receipt(root, envelope)
    with pytest.raises(ValueError, match='bytes changed'):
        images.record_native_image_host_exception(root, call['call_id'], host_exception={**receipt, 'sha256':'0'*64})
    observed = images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt)
    different = exception_receipt(root, envelope, name='different-exception')
    path = Path(different['path']); payload = json.loads(path.read_text())
    payload['exception']['message'] = 'Different actual event'; write(path, payload)
    different['sha256'] = file_sha256(path)
    with pytest.raises(ValueError, match='different'):
        images.record_native_image_host_exception(root, call['call_id'], host_exception=different)
    retained = root/observed['host_exception']['path']; retained.chmod(0o644); retained.write_text('{}')
    with pytest.raises(ValueError, match='bytes changed'):
        images.record_native_image_host_exception(root, call['call_id'], host_exception=receipt)


def test_host_exception_allows_authentic_terminal_error_collection(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root, call['call_id'])
    images.record_native_image_host_exception(root, call['call_id'], host_exception=exception_receipt(root, envelope))
    failed = images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope,
        result={'isError':True,'error':'Synthetic authentic terminal host return'}))
    assert failed['state'] == 'failed' and 'host_exception' in failed
    assert images.image_usage(root)['total'] == 1
    assert images.image_usage(root)['unresolved_originals'] == []
    assert reserve(board_package, call_id='permitted-after-terminal')['state'] == 'reserved'


@pytest.mark.parametrize('terminal_result', [None, {'isError':True,'error':'Synthetic terminal result'}])
def test_host_exception_cannot_change_terminal_original(board_package, terminal_result):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root, call['call_id'])
    terminal = images.import_native_image(root, call['call_id'],
        host_receipt=host_receipt(root, envelope, result=terminal_result))
    with pytest.raises(ValueError, match='terminal|actual host return'):
        images.record_native_image_host_exception(root, call['call_id'], host_exception=exception_receipt(root, envelope))
    assert images.image_call_status(root, call['call_id'])['state'] == terminal['state']
    assert images.image_usage(root)['total'] == 1
    assert images.image_usage(root)['unresolved_originals'] == []


def test_host_exception_cannot_replace_already_retained_actual_return(board_package):
    root, *_ = board_package
    call = reserve(board_package)
    envelope = images.begin_native_image(root, call['call_id'])
    receipt = host_receipt(root, envelope, result={'output_hint':'Synthetic unusable actual return'})
    with pytest.raises(ValueError, match='unresolved'):
        images.import_native_image(root, call['call_id'], host_receipt=receipt)
    before = images.image_call_status(root, call['call_id'])
    with pytest.raises(ValueError, match='actual host return'):
        images.record_native_image_host_exception(root, call['call_id'], host_exception=exception_receipt(root, envelope))
    assert images.image_call_status(root, call['call_id']) == before
    assert not (root/'production_images'/call['call_id']/'host-exception.json').exists()


def recovery_original(package, *, call_id='original'):
    root, *_ = package
    original = reserve(package, call_id=call_id)
    envelope = images.begin_native_image(root, call_id)
    receipt = exception_receipt(root, envelope)
    images.record_native_image_host_exception(root, call_id, host_exception=receipt)
    return original, envelope, receipt


def recovery_scope(package, *, target='assets/images/recovery.png', scope_id='image-recovery'):
    root, packet, args, _ = package
    scopes = json.loads((root/'production_scopes.json').read_text())
    scope = copy.deepcopy(scopes['scopes'][0])
    scope.update(id=scope_id, image_allowance=5,
        requests={'s1-start':images.image_request_digest(packet,'s1-start',args,output_path=target)},
        attempts_per_shot={'s1-start':2})
    scopes['scopes'].append(scope)
    write(root/'production_scopes.json',scopes)
    return scope, target


def reserve_recovery(package, *, call_id='recovery', original='original', target='assets/images/recovery.png', scope_id='image-recovery'):
    root, packet, args, _ = package
    return images.reserve_native_image(root,packet,'s1-start',args,output_path=target,
        scope_id=scope_id,call_id=call_id,replace_exception_call_id=original)


def test_explicit_exception_successor_counts_both_and_emits_only_once(board_package):
    root, *_ = board_package
    original, _, receipt = recovery_original(board_package)
    recovery_scope(board_package)
    successor = reserve_recovery(board_package)
    assert successor['state'] == 'reserved'
    assert successor['replaces_host_exception'] == {
        'call_id':'original','receipt_sha256':receipt['sha256'],'request_sha256':original['request_sha256']}
    assert images.image_call_status(root,'original')['state'] == 'uncertain'
    assert images.image_usage(root)['total'] == 2
    assert reserve_recovery(board_package) == successor
    envelope = images.begin_native_image(root,'recovery')
    assert envelope['call_id'] == 'recovery'
    with pytest.raises(ValueError,match='already emitted'):
        images.begin_native_image(root,'recovery')
    with pytest.raises(ValueError,match='another|linked|override'):
        images.reserve_native_image(root,board_package[1],'s1-start',board_package[2],
            output_path='assets/images/recovery.png',scope_id='image-recovery',call_id='recovery')


def test_recovery_requires_recorded_exception_and_does_not_upgrade_reservation(board_package):
    root, *_ = board_package
    recovery_scope(board_package)
    ordinary = reserve_recovery(board_package,original=None)
    with pytest.raises(ValueError,match='another|linked|override'):
        reserve_recovery(board_package,original='not-real')
    images.release_unsubmitted_image(root,ordinary['call_id'],reason='Synthetic cancelled reservation')
    call = reserve(board_package,call_id='original')
    images.begin_native_image(root,call['call_id'])
    images.mark_image_uncertain(root,call['call_id'],reason='No actual exception evidence')
    with pytest.raises(ValueError,match='exception'):
        reserve_recovery(board_package,call_id='new')
    with pytest.raises(ValueError,match='pending|uncertain'):
        reserve_recovery(board_package,call_id='ordinary-new',original=None)


def test_only_one_successor_across_scopes_and_no_chained_recovery(board_package):
    root, *_ = board_package
    recovery_original(board_package)
    recovery_scope(board_package)
    reserve_recovery(board_package)
    recovery_scope(board_package,target='assets/images/second.png',scope_id='second-scope')
    with pytest.raises(ValueError,match='successor'):
        reserve_recovery(board_package,call_id='second',target='assets/images/second.png',scope_id='second-scope')
    envelope=images.begin_native_image(root,'recovery')
    images.record_native_image_host_exception(root,'recovery',host_exception=exception_receipt(root,envelope,name='child-exception'))
    with pytest.raises(ValueError,match='chain|successor'):
        reserve_recovery(board_package,call_id='chained',original='recovery',target='assets/images/second.png',scope_id='second-scope')
    assert images.image_usage(root)['total'] == 2


def test_recovery_concurrent_reservations_admit_one_successor(board_package):
    recovery_original(board_package)
    recovery_scope(board_package)
    recovery_scope(board_package,target='assets/images/other-recovery.png',scope_id='other-recovery')
    def attempt(values):
        call_id,target,scope_id=values
        try:
            return reserve_recovery(board_package,call_id=call_id,target=target,scope_id=scope_id)['call_id']
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(attempt,[('first','assets/images/recovery.png','image-recovery'),
            ('second','assets/images/other-recovery.png','other-recovery')]))
    assert sum(result is not None for result in outcomes) == 1
    assert images.image_usage(board_package[0])['total'] == 2


@pytest.mark.parametrize('defect',['tampered','stale_story','cross_slot','same_output','existing_output','wrong_route','episode_cap','slot_cap','wrong_request'])
def test_recovery_preserves_exception_binding_authority_and_unique_output(board_package,defect):
    root,packet,args,target=board_package
    recovery_original(board_package)
    recovery_scope(board_package)
    if defect == 'tampered':
        path=root/images.image_call_status(root,'original')['host_exception']['path']
        path.chmod(0o644);path.write_text('{}')
    elif defect == 'stale_story':
        journal=json.loads((root/images.ARTIFACT).read_text());journal['calls'][0]['story_revision']='old-story'
        write(root/images.ARTIFACT,journal)
    elif defect == 'cross_slot':
        journal=json.loads((root/images.ARTIFACT).read_text());journal['calls'][0]['slot_id']='different-slot'
        write(root/images.ARTIFACT,journal)
    elif defect == 'same_output':
        target=board_package[3]
    elif defect == 'existing_output':
        path=root/'assets/images/recovery.png';path.write_bytes(PNG)
    else:
        scopes=json.loads((root/'production_scopes.json').read_text());scope=scopes['scopes'][-1]
        if defect == 'wrong_route': scope['provider']='grok_cli'
        if defect == 'episode_cap': scope['image_allowance']=1
        if defect == 'slot_cap': scope['attempts_per_shot']['s1-start']=0
        if defect == 'wrong_request': scope['requests']['s1-start']='0'*64
        write(root/'production_scopes.json',scopes)
    with pytest.raises(ValueError):
        reserve_recovery(board_package,target=target if defect=='same_output' else 'assets/images/recovery.png')
    assert images.image_usage(root)['total'] == 1


@pytest.mark.parametrize('terminal_result',[None,{'isError':True,'error':'Synthetic actual terminal return'}])
def test_original_return_before_child_emission_refuses_child_but_can_release(board_package,terminal_result):
    root, *_ = board_package
    _,envelope,_=recovery_original(board_package)
    recovery_scope(board_package)
    reserve_recovery(board_package)
    images.import_native_image(root,'original',host_receipt=host_receipt(root,envelope,result=terminal_result))
    with pytest.raises(ValueError,match='return|output|unresolved'):
        images.begin_native_image(root,'recovery')
    released=images.release_unsubmitted_image(root,'recovery',reason='Original actual return arrived before child host handoff')
    assert released['state']=='never_submitted'
    assert images.image_usage(root)['total']==1
    with pytest.raises(ValueError,match='successor'):
        reserve_recovery(board_package,call_id='third')


def test_both_late_authentic_results_import_independently_after_child_emission(board_package):
    root, *_ = board_package
    _,original_envelope,_=recovery_original(board_package)
    recovery_scope(board_package)
    reserve_recovery(board_package)
    child_envelope=images.begin_native_image(root,'recovery')
    original=images.import_native_image(root,'original',host_receipt=host_receipt(root,original_envelope,name='original-return'))
    child=images.import_native_image(root,'recovery',host_receipt=host_receipt(root,child_envelope,name='child-return'))
    assert original['state']==child['state']=='imported'
    assert original['output']['path'] != child['output']['path']
    assert original['host_receipt']['sha256'] != child['host_receipt']['sha256']
    assert images.image_usage(root)['total']==2
    assert images.image_usage(root)['unresolved_originals']==[]


@pytest.mark.parametrize('defect',['link','original_arguments','late_output'])
def test_successor_revalidation_rejects_changed_link_or_output(board_package,defect):
    root, *_ = board_package
    recovery_original(board_package)
    recovery_scope(board_package)
    reserve_recovery(board_package)
    if defect=='late_output':
        (root/'assets/images/recovery.png').write_bytes(PNG)
    else:
        journal=json.loads((root/images.ARTIFACT).read_text())
        if defect=='link': journal['calls'][1]['replaces_host_exception']['receipt_sha256']='0'*64
        else: journal['calls'][0]['submitted_arguments']['prompt']='Changed emitted original'
        write(root/images.ARTIFACT,journal)
    with pytest.raises(ValueError):
        images.begin_native_image(root,'recovery')
    assert images.image_call_status(root,'recovery')['state']=='reserved'
    assert images.image_usage(root)['total']==2


def test_recovery_does_not_ignore_another_pending_original(board_package):
    root, *_ = board_package
    recovery_original(board_package)
    recovery_scope(board_package)
    journal=json.loads((root/images.ARTIFACT).read_text())
    other=copy.deepcopy(journal['calls'][0]);other['call_id']='other-pending';other.pop('host_exception')
    other['state']='submitted';other['events']=[event for event in other['events'] if event['kind'] not in {'host_exception','uncertain'}]
    other['output_path']='assets/images/other-pending.png'
    journal['calls'].append(other);write(root/images.ARTIFACT,journal)
    with pytest.raises(ValueError,match='pending|uncertain'):
        reserve_recovery(board_package)
    assert images.image_usage(root)['total']==2


def test_recovery_output_cannot_collide_with_existing_provider_session(board_package):
    root, *_ = board_package
    recovery_original(board_package)
    recovery_scope(board_package)
    write(root/'production_attempts/provider-original/request.json',{
        'attempt_id':'provider-original','media_kind':'image','shot_id':'another-slot',
        'scope_id':'provider-scope','scope':{'provider':'grok_cli'},
        'submitted_inputs':{'output_path':str(root/'assets/images/recovery.png')}})
    with pytest.raises(ValueError,match='unique|output'):
        reserve_recovery(board_package)
