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
