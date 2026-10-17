"""U3 proof fixtures only. No provider execution or live visual claims."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from jsonschema.exceptions import ValidationError
from lib import production_request as request, production_execution as execution, openart_jobs as jobs
from lib.shot_contract import contract_digest, file_sha256
from tools import _openart_cli as cli

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'first_pass'


@pytest.fixture
def generated_carrier_preparation(neutral_package, monkeypatch):
    # Existing canonical image fixtures retain actual original host-call/receipt
    # shapes. All provider outcomes and semantic judgments are SYNTHETIC.
    from tests.lib.test_production_factory import prepared_frame_shapes
    root, prepared = prepared_frame_shapes(neutral_package, monkeypatch, 'grok_cli')
    return root, *prepared['s1']


def bind_generated_review(prepared, review):
    _, inputs, native, profile, compiled, _ = prepared
    return request.prepare_compiled_request(inputs, native, profile,
        coverage=compiled['coverage'], timing=compiled['timing'], review=review)



@pytest.mark.parametrize('damage', [None, 'conflict', 'missing_review', 'invalid_review', 'partial_write', 'semantic_failure'])
def test_public_preparation_persists_only_validated_immutable_evidence(generated_carrier_preparation, damage, monkeypatch):
    from tests.lib.test_production_factory import file_bytes
    root, inputs, native, profile, compiled, review = generated_carrier_preparation
    inputs = dict(inputs, compiled_request_id='persisted', preparation_review_id='persisted')
    judgment = {k: copy.deepcopy(v) for k, v in review.items()
                if k not in {'review_id', 'subject_sha256', 'composite_boards'}}
    if damage == 'semantic_failure':
        judgment['status'] = 'fail'
        judgment['predicates'][0]['status'] = 'fail'
    built, bound = request.prepare_compiled_request(inputs, native, profile,
        coverage=compiled['coverage'], timing=compiled['timing'], review=judgment)
    compiled_path = root / 'artifacts/compiled_request-persisted.json'
    review_path = root / 'artifacts/preparation_review-persisted.json'
    if damage == 'conflict':
        execution._write_new(review_path, {**bound, 'reviewer': 'different retained reviewer'})
    elif damage == 'partial_write':
        execution._write_new(compiled_path, built)
    elif damage == 'missing_review':
        judgment = None
    elif damage == 'invalid_review':
        del judgment['predicates']
    before = file_bytes(root)
    kwargs = dict(coverage=compiled['coverage'], timing=compiled['timing'], review=judgment, persist=True)
    if damage in {'conflict', 'missing_review', 'invalid_review'}:
        with pytest.raises(ValueError):
            request.prepare_compiled_request(inputs, native, profile, **kwargs)
        assert file_bytes(root) == before and not compiled_path.exists()
        return
    assert request.prepare_compiled_request(inputs, native, profile, **kwargs) == (built, bound)
    assert json.loads(compiled_path.read_text()) == built
    assert json.loads(review_path.read_text()) == bound
    assert compiled_path.stat().st_mode & 0o222 == review_path.stat().st_mode & 0o222 == 0
    saved = file_bytes(root)
    stats = [p.stat().st_mtime_ns for p in (compiled_path, review_path)]
    assert request.prepare_compiled_request(inputs, native, profile, **kwargs) == (built, bound)
    assert file_bytes(root) == saved
    assert [p.stat().st_mtime_ns for p in (compiled_path, review_path)] == stats
    if damage == 'semantic_failure':
        # Activated factory retains the exact failure as disclosure; generic
        # manifest admission still requires the real passing verdict.
        assert request.validate_preparation(inputs, native, profile)['review_sha256'] == request.digest(bound)
        assert bound['status'] == bound['predicates'][0]['status'] == 'fail'
        monkeypatch.setattr('lib.shot_contract.factory_project', lambda _: False)
        with pytest.raises(ValueError, match='nonpassing preparation review|critical preparation predicates'):
            request.validate_preparation(inputs, native, profile)

def test_public_preparation_derives_only_authenticated_review_bindings(generated_carrier_preparation):
    from tests.lib.test_production_factory import file_bytes
    root, inputs, native, profile, compiled, review = generated_carrier_preparation
    judgment = {k: copy.deepcopy(v) for k, v in review.items()
                if k not in {'review_id', 'subject_sha256', 'composite_boards'}}
    original = copy.deepcopy(judgment); before = file_bytes(root)
    built, bound = bind_generated_review(generated_carrier_preparation, judgment)
    assert built == compiled and bound == review
    assert judgment == original and file_bytes(root) == before
    assert bound['composite_boards'][0]['sha256'] == compiled['source_binding']['frame_input']['sha256']
    assert all(bound[k] == judgment[k] for k in judgment)
    assert request.validate_preparation_evidence(built, bound, inputs, native, profile,
        request.source_packet(root, 's1', provider='grok_cli', native=native))
    incomplete = copy.deepcopy(judgment); del incomplete['predicates']
    with pytest.raises(ValueError, match='preparation_review: invalid'):
        bind_generated_review(generated_carrier_preparation, incomplete)


def test_supplied_preparation_review_defaults_kind_without_changing_unknown_judgment(generated_carrier_preparation):
    root, inputs, native, profile, compiled, review = generated_carrier_preparation
    judgment = copy.deepcopy(review)
    del judgment['evidence_kind']
    judgment['status'] = 'unknown'
    for predicate in judgment['predicates']:
        predicate['status'] = 'unknown'
    original = copy.deepcopy(judgment)
    built, bound = bind_generated_review(generated_carrier_preparation, judgment)
    assert built == compiled
    assert bound == {**original, 'evidence_kind': 'reviewed'}
    assert judgment == original
    with pytest.raises(ValueError, match='nonpassing preparation review'):
        request.validate_preparation_evidence(built, bound, inputs, native, profile,
            request.source_packet(root, 's1', provider='grok_cli', native=native), factory=False)


@pytest.mark.parametrize('kind', ['fixture_only', 'reviewed', 'live', None])
def test_supplied_preparation_review_preserves_or_rejects_explicit_kind(generated_carrier_preparation, kind):
    judgment = copy.deepcopy(generated_carrier_preparation[-1])
    judgment['evidence_kind'] = kind
    if kind in {'fixture_only', 'reviewed'}:
        _, bound = bind_generated_review(generated_carrier_preparation, judgment)
        assert bound == judgment
    else:
        with pytest.raises(ValueError, match='preparation_review: invalid evidence_kind'):
            bind_generated_review(generated_carrier_preparation, judgment)


@pytest.mark.parametrize('damage', [None, 'review_status', 'predicate', 'duplicate_predicate',
                                   'live_fixture', 'timing', 'missing_review'])
def test_passing_preparation_uses_one_current_packet_before_publication(generated_carrier_preparation, monkeypatch, damage):
    from tests.lib.test_production_factory import file_bytes
    from unittest.mock import Mock
    root, inputs, native, profile, compiled, review = generated_carrier_preparation
    inputs = dict(inputs, compiled_request_id='passing-once', preparation_review_id='passing-once')
    judgment = {k: copy.deepcopy(v) for k, v in review.items()
                if k not in {'review_id', 'subject_sha256', 'composite_boards'}}
    timing = copy.deepcopy(compiled['timing'])
    if damage == 'review_status': judgment['status'] = 'unknown'
    elif damage == 'predicate': judgment['predicates'][0]['status'] = 'unknown'
    elif damage == 'duplicate_predicate': judgment['predicates'].append(copy.deepcopy(judgment['predicates'][0]))
    elif damage == 'live_fixture': profile = {'source': 'real'}
    elif damage == 'timing': timing['duration_seconds'] = 100
    elif damage == 'missing_review': judgment = None
    packet_reads = Mock(wraps=request.source_packet)
    monkeypatch.setattr(request, 'source_packet', packet_reads)
    before = file_bytes(root)
    kwargs = dict(coverage=compiled['coverage'], timing=timing, review=judgment,
                  persist=True, require_passing_review=True)
    if damage:
        with pytest.raises(ValueError): request.prepare_compiled_request(inputs, native, profile, **kwargs)
        assert file_bytes(root) == before
    else:
        built, bound = request.prepare_compiled_request(inputs, native, profile, **kwargs)
        assert built == compiled and bound['status'] == 'pass'
        assert json.loads((root / 'artifacts/compiled_request-passing-once.json').read_text()) == built
        assert json.loads((root / 'artifacts/preparation_review-passing-once.json').read_text()) == bound
    assert packet_reads.call_count == (0 if damage == 'missing_review' else 1)


def test_preparation_fills_only_omitted_mechanical_timing_bindings(package):
    root, inputs, native, profile, compiled, _ = package
    authored = copy.deepcopy(compiled['timing'])
    for window in authored['action_windows']:
        window.pop('value_sha256')
    for segment in authored['segments']:
        segment.pop('text_sha256')
        segment.pop('word_count')
    original = copy.deepcopy(authored)
    result = request.prepare_compiled_request(inputs, native, profile,
        coverage=compiled['coverage'], timing=authored)
    assert authored == original
    assert result == compiled
    assert request.prepare_compiled_request(inputs, native, profile,
        coverage=compiled['coverage'], timing=compiled['timing']) == compiled


@pytest.mark.parametrize('damage', ['action_hash', 'text_hash', 'word_count', 'action_pointer', 'dialogue_index'])
def test_preparation_rejects_conflicting_or_unresolvable_timing_bindings_without_writes(package, damage):
    from tests.lib.test_production_factory import file_bytes
    root, inputs, native, profile, compiled, review = package
    timing = copy.deepcopy(compiled['timing'])
    if damage == 'action_hash':
        timing['action_windows'][0]['value_sha256'] = '0' * 64
    elif damage == 'text_hash':
        timing['segments'][0]['text_sha256'] = '0' * 64
    elif damage == 'word_count':
        timing['segments'][0]['word_count'] += 1
    elif damage == 'action_pointer':
        timing['action_windows'][0].pop('value_sha256')
        timing['action_windows'][0]['source_pointer'] = '/shot_contract/shots/99/dominant_action'
    else:
        timing['segments'][0]['dialogue_index'] = 99
    before = file_bytes(root)
    with pytest.raises((ValueError, ValidationError)):
        request.prepare_compiled_request(inputs, native, profile,
            coverage=compiled['coverage'], timing=timing, review=review, persist=True)
    assert file_bytes(root) == before


def test_measured_audio_timing_is_not_rewritten(package):
    root, inputs, native, profile, compiled, _ = package
    measured(package)
    authored = copy.deepcopy(compiled['timing'])
    expected = copy.deepcopy(authored)
    rebuilt = request.prepare_compiled_request(inputs, native, profile,
        coverage=compiled['coverage'], timing=authored)
    assert rebuilt['timing'] == expected
    assert authored == expected


@pytest.mark.parametrize('field', ['review_id', 'subject_sha256', 'composite_boards'])
def test_public_preparation_rejects_conflicting_supplied_review_bindings(generated_carrier_preparation, field):
    from tests.lib.test_production_factory import file_bytes
    root, _, _, _, _, review = generated_carrier_preparation
    altered = copy.deepcopy(review)
    if field == 'composite_boards':
        altered[field][0]['members'][0]['cast_ids'] = ['SYNTHETIC-other-cast']
    else:
        altered[field] = 'wrong-review' if field == 'review_id' else '0'*64
    original = copy.deepcopy(altered); before = file_bytes(root)
    with pytest.raises(ValueError, match='conflicts with canonical binding'):
        bind_generated_review(generated_carrier_preparation, altered)
    assert altered == original and file_bytes(root) == before


@pytest.mark.parametrize('defect', ['carrier_bytes', 'source_bytes', 'original_snapshot', 'host_receipt', 'forged_source'])
def test_public_preparation_rejects_changed_generated_carrier_provenance(generated_carrier_preparation, defect):
    from lib import production_boards as boards, production_images as images
    from tests.lib.test_production_factory import file_bytes
    root, _, _, _, _, review = generated_carrier_preparation
    pack = boards.load_board_pack(root); binding = pack['board_bindings']['start']
    call = images._call(images._load(root), binding['call_id'])
    if defect == 'forged_source':
        contract = execution.load_shot_contract(root)
        end = next(a for a in contract['assets'] if a['id'] == 'end')
        binding['source_bindings']['end'] = {k: end[k] for k in ('path', 'sha256')}
        write(root / boards.ARTIFACT, pack)
    else:
        if defect == 'carrier_bytes':
            path = root / binding['path']
        elif defect == 'source_bytes':
            path = root / next(iter(binding['source_bindings'].values()))['path']
        elif defect == 'original_snapshot':
            path = Path(call['submitted_arguments']['referenced_image_paths'][0])
        else:
            path = root / call['host_receipt']['path']
        path.chmod(0o600); path.write_bytes(b'SYNTHETIC deliberately changed provenance')
    before = file_bytes(root)
    with pytest.raises((ValueError, KeyError, OSError)):
        bind_generated_review(generated_carrier_preparation, review)
    assert file_bytes(root) == before


@pytest.mark.parametrize('status,predicate_status', [('fail', 'fail'), ('unknown', 'unknown'), ('pass', 'fail')])
def test_public_preparation_preserves_semantic_failure(generated_carrier_preparation, status, predicate_status, monkeypatch):
    root, inputs, native, profile, _, review = generated_carrier_preparation
    judgment = copy.deepcopy(review); judgment['status'] = status
    judgment['predicates'][0]['status'] = predicate_status
    built, bound = bind_generated_review(generated_carrier_preparation, judgment)
    assert bound == judgment
    write(root / 'artifacts/compiled_request-frame-s1.json', built)
    write(root / 'artifacts/preparation_review-frame-s1.json', bound)
    if status == 'pass':
        with pytest.raises(ValueError, match='contradicts'):
            request.validate_preparation(inputs, native, profile)
    else:
        assert request.validate_preparation(inputs, native, profile)['review_sha256'] == request.digest(bound)
    monkeypatch.setattr('lib.shot_contract.factory_project', lambda _: False)
    with pytest.raises(ValueError, match='nonpassing preparation review|critical preparation predicates'):
        request.validate_preparation(inputs, native, profile)


def test_public_preparation_does_not_infer_provided_board_membership(generated_carrier_preparation):
    from lib import production_boards as boards
    root, _, _, _, _, review = generated_carrier_preparation
    pack = boards.load_board_pack(root); pack['board_bindings']['start']['origin'] = 'reuse'
    write(root / boards.ARTIFACT, pack)
    judgment = {k: copy.deepcopy(v) for k, v in review.items() if k != 'composite_boards'}
    _, bound = bind_generated_review(generated_carrier_preparation, judgment)
    assert 'composite_boards' not in bound
    _, bound = bind_generated_review(generated_carrier_preparation, review)
    assert bound == review  # Explicit exact reviewed membership remains supported.
    altered = copy.deepcopy(review); altered['composite_boards'][0]['members'][0]['sha256'] = '0'*64
    with pytest.raises(ValueError, match='one exact approved source'):
        bind_generated_review(generated_carrier_preparation, altered)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'private'))
    # Pure builder may read retained fixture evidence, but no subprocess is permitted.
    monkeypatch.setattr(cli, '_run_checked', lambda *a, **k: pytest.fail('preparation invoked CLI'))
    contract = json.loads((FIXTURES / 'valid_shot_contract.json').read_text())
    for asset in contract['assets']:
        path = root / asset['path']; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((FIXTURES / 'synthetic-board.svg').read_bytes())
        asset['sha256'] = file_sha256(path); asset['review']['subject_sha256'] = asset['sha256']
    shot = contract['shots'][0]
    shot['dialogue'] = [dict(speaker_id='patient', source='visible', text='Help me.', start_seconds=1, end_seconds=2),
                        dict(speaker_id='patient', source='visible', text='Help me.', start_seconds=3, end_seconds=4)]
    shot['required_visible_speakers'] = ['patient']
    sha = contract_digest(contract)
    contract['project_review']['subject_sha256'] = sha
    for s in contract['shots']: s['review']['subject_sha256'] = sha
    write(root / 'artifacts/shot_contract.json', contract)
    write(root / 'project.json', {'project_id':contract['project_id'], 'story_revision':contract['story_revision'],
                                'governance':{'version':'1.0','mode':'strict'}})
    write(root / 'artifacts/scene_plan.json', {'version':'1.0','scenes':[{'id':'entry','type':'generated','description':'Fixture entry','start_seconds':0,'end_seconds':8,'script_section_id':'s1'}]})
    write(root / 'artifacts/script.json', {'version':'1.0','title':'Fixture','total_duration_seconds':8,'sections':[{'id':'s1','text':'Help me. Help me.','start_seconds':0,'end_seconds':8}]})
    compiled_prompt = request.compile_prompt(root, 'entry')
    image_path = str(root / next(a['path'] for a in contract['assets'] if a['id']=='start'))
    inputs = {'project_dir':str(root),'governance':{'scope_id':'approved','shot_id':'entry'},
              'prompt':compiled_prompt['prompt'],'model':'fixture-model','mode':'image2video','operation':'image_to_video',
              'duration':8,'aspect_ratio':'16:9','resolution':'720p','image_path':image_path,'image_upload_id':'up-1',
              'output_path':str(root/'clip.mp4'),'compiled_request_id':'c1','preparation_review_id':'r1'}
    profile = {'source':'fixture','model':'fixture-model','mode':'image2video','cli_version':'fixture-0',
               'tier':'fixture-only','form_sha256':'f'*64,'form_defaults':{'duration':8},
               'profile_sha256':'e'*64,'account_id_sha256':'a'*64,'dry_run_endpoint':'POST /fixture',
               'upload':{'json_paths':{'upload_url':'url'},'url_hosts':['up.openart.test']}}
    from tests.lib.test_openart_jobs import captured
    guarantees = {'contract': {'nonspending': True, 'no_delayed_charge': True}}
    profile['upload']['guarantee'] = {
        'argv': ['account'],
        'nonspending': {'path': 'contract.nonspending', 'expected': True},
        'no_delayed_charge': {'path': 'contract.no_delayed_charge', 'expected': True}}
    profile['upload']['receipts'] = [captured('nonspending_guarantee', ['account'], guarantees)]
    # Fixture-only qualified retained upload; no upload calls.
    snapshot = jobs._upload_snapshot_path('up-1','.svg'); cli.write_private(snapshot,Path(image_path).read_bytes())
    snapshot.chmod(0o400)
    url = 'https://up.openart.test/start.svg?sig=private'
    parsed = {'url':url, **guarantees}; argv = ['upload','add',str(snapshot)] + cli.GLOBAL_FLAGS
    upload_receipt = captured('upload', argv[:-len(cli.GLOBAL_FLAGS)], parsed)
    profile['upload']['receipts'].append(upload_receipt)
    rid, rsha = upload_receipt['receipt_id'], upload_receipt['receipt_sha256']
    binding = jobs.upload_binding(profile)
    record = {'version':'2','upload_id':'up-1','profile_source':profile['source'],
              'binding':binding,'binding_sha256':jobs.sha256_json(binding),
              'approval_sha256':'b'*64,'model':profile['model'],'mode':profile['mode'],
              'account_id_sha256':profile['account_id_sha256'],'url_path':'url','receipt_id':rid,'receipt_sha256':rsha,
              'snapshot_name':snapshot.name,'source_sha256':file_sha256(snapshot),'source_size':snapshot.stat().st_size,
              'url':url,'url_sha256':hashlib.sha256(url.encode()).hexdigest()}
    cli.write_private(jobs._upload_record_path('up-1'),json.dumps(record).encode())
    native = preview(inputs,profile,url)
    timing = {'method':'segmented_estimate','duration_seconds':8,'language':'en','margin_seconds':0.5,
              'rationale':'Fixture-only explicit short utterances','overlap_policy':'serial','overlap_rationale':'No overlapping windows',
              'action_windows':[{'source_pointer':'/shot_contract/shots/0/dominant_action','value_sha256':request.digest(shot['dominant_action']),
                                 'start_seconds':4.1,'end_seconds':7,'rationale':'Fixture action'},
                                {'source_pointer':'/shot_contract/shots/0/completed_end_state','value_sha256':request.digest(shot['completed_end_state']),
                                 'start_seconds':7,'end_seconds':7.4,'rationale':'Fixture completed endpoint'}],
              'segments':[{'dialogue_index':i,'text_sha256':request.digest(l['text']),'language':'en',
                           'word_count':2,'words_per_minute':180,'pause_seconds':0,'rationale':'Fixture rate'} for i,l in enumerate(shot['dialogue'])]}
    compiled = request.prepare_compiled_request(inputs,native,profile,coverage=compiled_prompt['coverage'],timing=timing)
    review = {'version':'1.0','review_id':'r1','reviewer':'synthetic fixture author','status':'pass',
              'subject_sha256':request.digest(compiled),'evidence_kind':'fixture_only',
              'predicates':[{'name':p,'status':'pass','severity':'critical','evidence':'Fixture only'} for p in sorted(request.PREDICATES)]}
    write(root/'artifacts/compiled_request-c1.json',compiled)
    write(root/'artifacts/preparation_review-r1.json',review)
    (root/'approval.txt').write_text('Fixture approval https://private.test/?token=secret')
    scope = {'id':'approved','status':'approved','approved_by':'fixture author','project_id':contract['project_id'],
             'story_revision':contract['story_revision'],'phase':'first_pass','provider':'openart_cli',
             'approval_plan_sha256':execution.approval_plan_digest(contract),
             'evidence':{'path':'approval.txt','sha256':file_sha256(root/'approval.txt')},
             'requests':{'entry':execution.planned_request_digest(inputs,project_dir=root)},'attempts_per_shot':{'entry':1}}
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    monkeypatch.setattr(jobs,'load_qualification',lambda **kwargs:profile)
    return root, inputs, native, profile, compiled, review


def retain(parsed,argv,name):
    raw=json.dumps({'parsed':parsed,'argv':argv}).encode()
    rid='2026-10-05T000000-'+hashlib.sha256(raw).hexdigest()[:8]
    cli.write_private(cli.state_dir()/'receipts'/f'{rid}.json',raw)
    return rid,hashlib.sha256(raw).hexdigest()


def preview(inputs,profile,url):
    creative=cli.native_video_argv(inputs['prompt'],model=inputs['model'],mode=inputs['mode'],duration=inputs['duration'],
                                   aspect_ratio=inputs['aspect_ratio'],resolution=inputs['resolution'],image_url=url)
    body={'model':inputs['model'],'media':'video','mode':'image2video','params':{'prompt':inputs['prompt'],
           'duration':inputs['duration'],'aspectRatio':inputs['aspect_ratio'],'resolution':inputs['resolution'],'image':url}}
    rid,rsha=retain({'endpoint':profile['dry_run_endpoint'],'body':body},creative+['--dry-run']+cli.GLOBAL_FLAGS,'preview')
    inputs['native_dry_run_receipt_id']=rid; inputs['native_dry_run_receipt_sha256']=rsha
    return jobs.prepare_native_request(request.controls(inputs),profile)


def test_supported_pure_path_and_dispatcher_binding(package):
    root,inputs,native,profile,compiled,review=package
    assert request.validate_preparation(inputs,native,profile)['compiled_sha256']==request.digest(compiled)
    from tools.video.openart_cli_video import OpenArtCLIVideo
    assert execution.preflight(OpenArtCLIVideo(),inputs)['governed']
    assert request.compile_prompt(root,'entry')['prompt'].count('Help me.')==4 # two dialogue plus literal script


def test_empty_script_context_keeps_contract_coverage_and_script_binding(package):
    root, *_ = package
    script = json.loads((root/'artifacts/script.json').read_text())
    script['sections'][0]['text'] = ''
    write(root/'artifacts/script.json', script)

    authored = request.compile_prompt(root, 'entry')
    pointers = {row['source_pointer'] for row in authored['coverage']}
    assert '/script/sections/0/text' not in pointers
    assert '/shot_contract/shots/0/dialogue/0/text' in pointers
    assert '/shot_contract/shots/0/dialogue/1/text' in pointers
    assert '/shot_contract/shots/0/dominant_action' in pointers
    assert '/shot_contract/shots/0/completed_end_state' in pointers
    assert authored['source_binding']['script_sha256'] == request.digest(script)


def test_dropped_second_dialogue_fresh_generic_hashes_still_block(package):
    root,inputs,native,profile,compiled,review=package
    second=next(c for c in compiled['coverage'] if c['source_pointer'].endswith('/dialogue/1/text'))
    inputs['prompt']=inputs['prompt'][:second['start']]+(' '*len('Help me.'))+inputs['prompt'][second['end']:]
    native=preview(inputs,profile,'https://up.openart.test/start.svg?sig=private')
    compiled['request_sha256']=execution.planned_request_digest(inputs,project_dir=root)
    compiled['native_binding']=request._native_binding(native)
    second['fragment_sha256']=request.digest(' '*len('Help me.'))
    review['subject_sha256']=request.digest(compiled)
    write(root/'artifacts/compiled_request-c1.json',compiled);write(root/'artifacts/preparation_review-r1.json',review)
    with pytest.raises(ValueError,match='dropped/changed'):
        request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('key',['cli_version','tier','form_sha256','form_defaults_sha256','native_body_sha256','native_controls_sha256'])
def test_native_changes_invalidate(package,key):
    _,inputs,native,profile,_,_=package
    native=copy.deepcopy(native);native[key]='changed'
    with pytest.raises(ValueError,match='stale native'):
        request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('change',['reference','review','upstream_review','prompt'])
def test_current_inputs_invalidate(package,change):
    root,inputs,native,profile,_,_=package
    if change=='prompt': inputs['prompt']+=' changed'
    else:
        contract=json.loads((root/'artifacts/shot_contract.json').read_text())
        if change=='reference': (root/contract['assets'][0]['path']).write_text('stale')
        elif change=='review': contract['assets'][0]['review']['reviewer']='changed'
        else: contract['project_review']['reviewer']='changed upstream summary'
        write(root/'artifacts/shot_contract.json',contract)
    with pytest.raises(ValueError): request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('change',['no_language','impossible','bool_margin','overlap','no_action','duplicate_predicate','unknown_predicate'])
def test_timing_and_review_block(package,change):
    root,inputs,native,profile,compiled,review=package
    if change=='no_language':compiled['timing']['language']=' '
    elif change=='impossible':compiled['timing']['segments'][1]['words_per_minute']=1
    elif change=='bool_margin':compiled['timing']['margin_seconds']=True
    elif change=='overlap':compiled['timing']['action_windows'][0]['start_seconds']=0
    elif change=='no_action':compiled['timing']['action_windows']=[]
    elif change=='duplicate_predicate':review['predicates'][1]=copy.deepcopy(review['predicates'][0])
    else:review['predicates'][0]['name']='cosmetic'
    review['subject_sha256']=request.digest(compiled)
    write(root/'artifacts/compiled_request-c1.json',compiled);write(root/'artifacts/preparation_review-r1.json',review)
    with pytest.raises((ValueError,ValidationError)):request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('pause,should_fit', [(0.1, True), (0.10000001, False)])
def test_estimated_speech_decimal_boundary(package, pause, should_fit):
    root, _, native, _, compiled, _ = package
    packet = request.source_packet(root, 'entry')
    line = dict(packet['shot']['dialogue'][0], text='Three short words',
                start_seconds=1.3, end_seconds=2.3)
    packet['shot']['dialogue'] = [line]
    timing = copy.deepcopy(compiled['timing'])
    timing['segments'] = [dict(timing['segments'][0], text_sha256=request.digest(line['text']),
                               word_count=3, words_per_minute=200, pause_seconds=pause)]
    if should_fit:
        assert 3 * 60 / 200 + pause > line['end_seconds'] - line['start_seconds']
        request.validate_timing(timing, packet, root, request._body(native))
    else:
        with pytest.raises(ValueError, match='impossible dialogue timing'):
            request.validate_timing(timing, packet, root, request._body(native))


@pytest.mark.parametrize('field,value', [
    ('words_per_minute', 0), ('words_per_minute', -1),
    ('words_per_minute', float('nan')), ('words_per_minute', float('inf')),
    ('words_per_minute', True), ('words_per_minute', '200'),
    ('pause_seconds', -1), ('pause_seconds', float('nan')),
    ('pause_seconds', float('inf')), ('pause_seconds', True),
    ('start_seconds', -1), ('start_seconds', float('nan')),
    ('start_seconds', 2), ('end_seconds', 1),
])
def test_estimated_speech_rejects_invalid_numbers_and_order(package, field, value):
    root, _, native, _, compiled, _ = package
    packet = request.source_packet(root, 'entry')
    timing = copy.deepcopy(compiled['timing'])
    if field in {'start_seconds', 'end_seconds'}:
        packet['shot']['dialogue'][0][field] = value
    else:
        timing['segments'][0][field] = value
    with pytest.raises(ValueError):
        request.validate_timing(timing, packet, root, request._body(native))


def test_unknown_control_is_not_stripped(package):
    _,inputs,_,profile,_,_=package
    inputs['unknown_control']=True
    with pytest.raises(jobs.OpenArtCLIError,match='unsupported'):
        jobs.prepare_native_request(request.controls(inputs),profile)


def test_private_scope_and_evidence_public_counterparts(package):
    root,inputs,_,_,_,_=package
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope['requests']['entry']=execution.planned_request_template(inputs,project_dir=root)
    binding=request.freeze_approval('att-fixture',scope,(root/'approval.txt').read_bytes())
    public={'attempt_id':'att-fixture','scope':request.public_scope(scope),
            'approval_evidence':{'snapshot_id':'att-fixture','sha256':scope['evidence']['sha256']},
            'openart':{'approval_snapshot':binding}}
    assert 'Help me.' not in json.dumps(public)
    assert 'token=secret' not in json.dumps(public)
    private=request.load_private_approval(public)
    assert execution.approved_request_digest(private['scope']['requests']['entry'],project_dir=root)==execution.planned_request_digest(inputs,project_dir=root)
    public['scope']['approved_by']='attacker'
    with pytest.raises(ValueError,match='public/private'):request.load_private_approval(public)


def test_upload_approval_has_no_preview_cycle_and_stale_pack_blocks(package):
    root,inputs,_,_,_,_=package
    packet=request.source_packet(root,'entry')
    record={'version':'1.0','upload_id':'up-1','asset_id':'start','shot_id':'entry','source_sha256':file_sha256(inputs['image_path']),
            'source_binding':packet['binding'],'approved_by':'fixture-only approval','evidence_path':'approval.txt',
            'evidence_sha256':file_sha256(root/'approval.txt')}
    write(root/'artifacts/upload_approval-up-1.json',record)
    for p in root.glob('artifacts/*request*'):p.unlink()
    assert request.approved_upload_lookup(root,'up-1',record['source_sha256'])['state']=='approved'
    assert request.approved_upload_lookup(root,'up-1','0'*64) is None
    contract=json.loads((root/'artifacts/shot_contract.json').read_text());contract['assets'][0]['review']['reviewer']='changed'
    write(root/'artifacts/shot_contract.json',contract)
    assert request.approved_upload_lookup(root,'up-1',record['source_sha256']) is None


def publish(package):
    root,inputs,native,profile,compiled,review=package
    write(root/'artifacts/compiled_request-c1.json',compiled)
    review['subject_sha256']=request.digest(compiled)
    write(root/'artifacts/preparation_review-r1.json',review)


def measured(package):
    root,inputs,native,profile,compiled,review=package
    packet=request.source_packet(root,'entry')
    audio=root/'audio.wav';audio.write_bytes(b'fixture-only measured approved speech')
    measurement={'audio_sha256':file_sha256(audio),'duration_seconds':4,'measurer':'fixture measurement author','method':'fixture-only measurement'}
    approval={'version':'1.0','state':'approved','approved_by':'fixture review author',
              'audio_sha256':file_sha256(audio),'measurement_sha256':request.digest(measurement),
              'dialogue_sha256':request.digest(packet['shot']['dialogue']),
              'contract_sha256':packet['binding']['contract_sha256'],'language':'en',
              'evidence':{'path':'approval.txt','sha256':file_sha256(root/'approval.txt')}}
    write(root/'audio-approval.json',approval)
    timing=compiled['timing'];timing.pop('segments');timing['method']='measured_audio'
    timing['audio']={'path':'audio.wav','sha256':file_sha256(audio),'approval_path':'audio-approval.json',
                     'approval_sha256':file_sha256(root/'audio-approval.json'),'measurement':measurement}
    publish(package)
    return approval


def test_measured_audio_with_exact_approved_occurrences(package):
    measured(package)
    _,inputs,native,profile,_,_=package
    assert request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('change',['outside_approval','arbitrary_approval','dialogue','measurement','source','timing_margin'])
def test_measured_audio_negative_bindings(package,change):
    approval=measured(package)
    root,inputs,native,profile,compiled,_=package
    if change=='outside_approval':
        path=root.parent/'outside-approval.json';write(path,approval)
        compiled['timing']['audio']['approval_path']=str(path)
        compiled['timing']['audio']['approval_sha256']=file_sha256(path)
    elif change=='arbitrary_approval':
        write(root/'audio-approval.json',{'approved_by':'anyone'})
        compiled['timing']['audio']['approval_sha256']=file_sha256(root/'audio-approval.json')
    elif change=='dialogue':
        approval['dialogue_sha256']='0'*64;write(root/'audio-approval.json',approval)
        compiled['timing']['audio']['approval_sha256']=file_sha256(root/'audio-approval.json')
    elif change=='measurement':compiled['timing']['audio']['measurement']['duration_seconds']=1
    elif change=='source':(root/'audio.wav').write_bytes(b'changed speech')
    else:compiled['timing']['margin_seconds']=5
    publish(package)
    with pytest.raises(ValueError):request.validate_preparation(inputs,native,profile)


def test_resolved_template_supported_with_actual_validator(package):
    root,inputs,_,_,_,_=package
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope['requests']['entry']=execution.planned_request_template(inputs,project_dir=root)
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    from tools.video.openart_cli_video import OpenArtCLIVideo
    assert execution.preflight(OpenArtCLIVideo(),inputs)['governed']


def test_missing_boards_never_filtered(package):
    root,*_=package
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    contract['shots'][0]['asset_ids']=[]
    write(root/'artifacts/shot_contract.json',contract)
    with pytest.raises(ValueError):request.compile_prompt(root,'entry')


def test_unknown_mapping_and_duplicate_sections_block(package):
    root,*_=package
    scene={'version':'1.0','scenes':[{'id':'entry','type':'generated','description':'Fixture','start_seconds':0,'end_seconds':8}]}
    write(root/'artifacts/scene_plan.json',scene)
    with pytest.raises(ValueError,match='mapping'):request.compile_prompt(root,'entry')
    scene['scenes'][0]['script_section_id']='s1';write(root/'artifacts/scene_plan.json',scene)
    script=json.loads((root/'artifacts/script.json').read_text());script['sections']*=2
    write(root/'artifacts/script.json',script)
    with pytest.raises(ValueError,match='duplicate'):request.compile_prompt(root,'entry')


def test_action_windows_cannot_claim_unrelated_action(package):
    _,inputs,native,profile,compiled,_=package
    compiled['timing']['action_windows'][0]['value_sha256']=request.digest('unrelated simple movement')
    publish(package)
    with pytest.raises(ValueError,match='source binding'):request.validate_preparation(inputs,native,profile)


def test_frozen_preparation_replay_then_current_review_invalidation(package):
    root,inputs,native,profile,_,_=package
    snapshot=request.freeze_preparation('att-frozen',inputs,native,profile)
    frozen={'inputs':inputs,'native':native,'profile':profile}
    saved={'attempt_id':'att-frozen','scope_id':'approved','shot_id':'entry','input_assets':[{'role':'image_path',
           'path':inputs['image_path'],'original_path':inputs['image_path'],'sha256':file_sha256(inputs['image_path'])}],
           'openart':{'preparation_snapshot':snapshot}}
    assert request.validate_frozen_preparation(saved,frozen,root)==snapshot
    contract=json.loads((root/'artifacts/shot_contract.json').read_text());contract['assets'][0]['review']['reviewer']='new reviewer'
    write(root/'artifacts/shot_contract.json',contract)
    with pytest.raises(ValueError,match='stale source'):request.validate_frozen_preparation(saved,frozen,root)


def test_true_current_upstream_review_change_blocks(package):
    from tests.lib.test_shot_contract import dependency
    root,inputs,_,profile,compiled,review=package
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    selected=dependency((contract,root))
    write(root/'artifacts/shot_contract.json',contract)
    write(root/'artifacts/selected_attempts.json',selected)
    scene=json.loads((root/'artifacts/scene_plan.json').read_text())
    scene['scenes'].append(dict(scene['scenes'][0],id='interior'))
    write(root/'artifacts/scene_plan.json',scene)
    inputs['governance']['shot_id']='interior'
    authored=request.compile_prompt(root,'interior');inputs['prompt']=authored['prompt']
    native=preview(inputs,profile,'https://up.openart.test/start.svg?sig=private')
    for action in compiled['timing']['action_windows']:
        action['source_pointer']=action['source_pointer'].replace('/shots/0/','/shots/1/')
    compiled=request.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=compiled['timing'])
    package=(root,inputs,native,profile,compiled,review);publish(package)
    assert request.validate_preparation(inputs,native,profile)
    selected['entry']['review']['reviewer']='changed actual upstream reviewer'
    write(root/'artifacts/selected_attempts.json',selected)
    with pytest.raises(ValueError,match='review changed'):
        request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('control',['endpoint_requirement_id','audio_path','rich_references','last_image_path'])
def test_required_native_control_cannot_be_prompt_substituted(package,control):
    root,inputs,_,_,_,_=package
    inputs[control]='required even if prompt mentions it'
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    # Non-path unsupported endpoint control reaches actual native gate; local paths bind otherwise.
    if control in execution.INPUT_PATH_KEYS:inputs[control]=inputs['image_path']
    scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=root)
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    from tools.video.openart_cli_video import OpenArtCLIVideo
    with pytest.raises((ValueError,jobs.OpenArtCLIError)):
        execution.preflight(OpenArtCLIVideo(),inputs)
    assert not (root/'production_attempts').exists()


@pytest.mark.parametrize('key',['reviewer','review_id','evidence'])
def test_blank_named_review_evidence_blocks(package,key):
    root,inputs,native,profile,_,review=package
    if key=='evidence':review['predicates'][0]['evidence']='   '
    else:review[key]='   '
    write(root/'artifacts/preparation_review-r1.json',review)
    with pytest.raises(ValueError):request.validate_preparation(inputs,native,profile)


def test_later_mutable_sidecars_cannot_replace_frozen_original_review(package):
    root,inputs,native,profile,compiled,review=package
    proof=request.freeze_preparation('att-original',inputs,native,profile)
    saved={'attempt_id':'att-original','scope_id':'approved','shot_id':'entry','input_assets':[{'role':'image_path',
           'path':inputs['image_path'],'original_path':inputs['image_path'],'sha256':file_sha256(inputs['image_path'])}],
           'openart':{'preparation_snapshot':proof}}
    frozen={'inputs':inputs,'native':native,'profile':profile}
    review['reviewer']='later author';review['predicates'][0]['status']='fail'
    write(root/'artifacts/preparation_review-r1.json',review)
    assert request.validate_frozen_preparation(saved,frozen,root)==proof
    # Editing the original private evidence still fails even after recomputing its subject.
    path=cli.state_dir()/'preparation/att-original/review.json'
    raw=json.loads(path.read_text());raw['review']=review
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='snapshot bytes changed'):
        request.validate_frozen_preparation(saved,frozen,root)


@pytest.mark.parametrize('maximum',[7,8])
def test_actual_native_form_bounds_and_fixture_review_never_live(package,maximum):
    """Isolated real-shaped form check; fixture review always blocks live eligibility."""
    root,inputs,_,profile,compiled,review=package
    profile=copy.deepcopy(profile);profile['source']='real'
    form={'schema':{'type':'object','properties':{'prompt':{'type':'string'},'image':{'type':'string'},
        'duration':{'type':'number','maximum':maximum},'aspectRatio':{'type':'string'},'resolution':{'type':'string'}}}}
    rid,sha=retain(form,['model','form'],'form')
    profile['captured_receipts']=[{'kind':'form','receipt_id':rid,'receipt_sha256':sha}]
    # Rebuild this isolated synthetic record for the real-shaped profile; the
    # fixture-only semantic review still prevents live eligibility below.
    upload_path = jobs._upload_record_path('up-1')
    record = json.loads(upload_path.read_bytes())
    record.update(profile_source=profile['source'], binding=jobs.upload_binding(profile))
    record['binding_sha256'] = jobs.sha256_json(record['binding'])
    upload_path.write_text(json.dumps(record))
    native=jobs.prepare_native_request(request.controls(inputs),profile)
    compiled['native_binding']=request._native_binding(native);publish((root,inputs,native,profile,compiled,review))
    with pytest.raises(ValueError,match='native form' if maximum==7 else 'fixture-only'):
        request.validate_preparation(inputs,native,profile)


def _compile_with_synthetic_real_form(package, form, *, duration=8, resolution='720p'):
    """Exercise the real-profile native form validator with synthetic retained evidence."""
    root,inputs,_,fixture_profile,compiled,_=package
    profile=copy.deepcopy(fixture_profile); profile['source']='real'
    profile['form_sha256']=request.digest(form); profile['form_defaults']={'duration':5}
    profile['dry_run_endpoint']='POST /synthetic/native-form-test'
    rid,sha=retain(form,cli.model_form_argv(profile['model'],profile['mode']),'form')
    profile['captured_receipts']=[{'kind':'form','receipt_id':rid,'receipt_sha256':sha}]
    upload_path=jobs._upload_record_path('up-1')
    upload_record=json.loads(upload_path.read_bytes())
    upload_record.update(profile_source='real',binding=jobs.upload_binding(profile))
    upload_record['binding_sha256']=jobs.sha256_json(upload_record['binding'])
    upload_path.write_text(json.dumps(upload_record))
    inputs['duration']=duration; inputs['resolution']=resolution
    url='https://up.openart.test/start.svg?sig=private'
    native=preview(inputs,profile,url)
    result=request.prepare_compiled_request(inputs,native,profile,
        coverage=compiled['coverage'],timing=compiled['timing'])
    return result, inputs, native, profile


def test_jsonschema_form_bounds_reject_out_of_range_native_duration(package):
    form={'model':'fixture-model','media':'video','mode':'image2video','jsonSchema':{
        'type':'object','properties':{'prompt':{'type':'string'},'image':{'type':'string'},
            'duration':{'type':'number','minimum':5,'maximum':7},'aspectRatio':{'type':'string'},
            'resolution':{'type':'string','enum':['768P']}}}}
    with pytest.raises(ValueError,match='native form rejects field duration'):
        _compile_with_synthetic_real_form(package,form)


def _observed_jsonschema_form(model='fixture-model', media='video', mode='image2video'):
    return {'model':model,'media':media,'mode':mode,'jsonSchema':{
        'type':'object','properties':{'prompt':{'type':'string'},'image':{'type':'string'},
            'duration':{'type':'integer','minimum':5,'maximum':15,'default':5},
            'aspectRatio':{'type':'string'},'resolution':{'type':'string','enum':['768P']}}}}


def test_jsonschema_native_validation_accepts_observed_duration_and_resolution(package):
    result,_,_,_=_compile_with_synthetic_real_form(package,_observed_jsonschema_form(),
                                                   duration=8,resolution='768P')
    assert result['version']=='1.0'


@pytest.mark.parametrize('duration', [4,16])
def test_jsonschema_native_validation_rejects_out_of_range_duration(package,duration):
    with pytest.raises(ValueError,match='native form rejects field duration'):
        _compile_with_synthetic_real_form(package,_observed_jsonschema_form(),
                                          duration=duration,resolution='768P')


def test_jsonschema_native_validation_rejects_wrong_resolution(package):
    with pytest.raises(ValueError,match='native form rejects field resolution'):
        _compile_with_synthetic_real_form(package,_observed_jsonschema_form(),
                                          duration=8,resolution='720p')


@pytest.mark.parametrize('model,media,mode', [
    ('other-model','video','image2video'),
    ('fixture-model','image','image2video'),
    ('fixture-model','video','text2video'),
])
def test_jsonschema_native_validation_rejects_mismatched_wrapper_metadata(package,model,media,mode):
    with pytest.raises(ValueError,match='native form schema or metadata is unsupported'):
        _compile_with_synthetic_real_form(package,_observed_jsonschema_form(model,media,mode),
                                          duration=5,resolution='768P')


@pytest.mark.parametrize('case',['no_language','drop_line','bad_review','stale_prompt'])
def test_zero_cli_and_zero_reservation_preflight_negatives(package,case):
    root,inputs,native,profile,compiled,review=package
    if case=='no_language':compiled['timing']['language']=' '
    elif case=='drop_line':compiled['coverage'].pop(5)
    elif case=='bad_review':review['predicates'][0]['status']='unknown'
    else:inputs['prompt']+=' not approved'
    publish((root,inputs,native,profile,compiled,review))
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=root)
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    from tools.video.openart_cli_video import OpenArtCLIVideo
    with pytest.raises((execution.ProductionGovernanceError,jobs.OpenArtCLIError)):
        OpenArtCLIVideo().execute(inputs)
    assert not (root/'production_attempts').exists()


def test_resolved_upstream_template_with_real_structural_validator(package):
    from tests.lib.test_shot_contract import dependency, refresh
    from lib.shot_contract import selection_digest, review_digest
    root,inputs,_,profile,compiled,review=package
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    selected=dependency((contract,root))
    # An approved selected frame can coincide with a reviewed start board's exact bytes.
    selected['entry']['outgoing_frame']={'path':inputs['image_path'],'sha256':file_sha256(inputs['image_path'])}
    selected['entry']['review']['subject_sha256']=selection_digest(selected['entry'])
    binding=contract['shots'][1]['upstream'][0]
    binding['outgoing_frame_sha256']=selected['entry']['outgoing_frame']['sha256']
    binding['review_sha256']=review_digest(selected['entry']['review'])
    refresh(contract)
    write(root/'artifacts/shot_contract.json',contract);write(root/'artifacts/selected_attempts.json',selected)
    scene=json.loads((root/'artifacts/scene_plan.json').read_text());scene['scenes'].append(dict(scene['scenes'][0],id='interior'))
    write(root/'artifacts/scene_plan.json',scene)
    inputs['governance']['shot_id']='interior'
    authored=request.compile_prompt(root,'interior');inputs['prompt']=authored['prompt']
    native=preview(inputs,profile,'https://up.openart.test/start.svg?sig=private')
    for action in compiled['timing']['action_windows']:action['source_pointer']=action['source_pointer'].replace('/shots/0/','/shots/1/')
    compiled=request.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=compiled['timing'])
    publish((root,inputs,native,profile,compiled,review))
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    template_inputs=copy.deepcopy(inputs)
    template_inputs['image_path']={'$upstream':{'shot_id':'entry','role':'outgoing_frame'}}
    scope['requests']={'interior':execution.planned_request_template(template_inputs,project_dir=root)}
    scope['attempts_per_shot']={'interior':1};scope['approval_plan_sha256']=execution.approval_plan_digest(contract)
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    from tools.video.openart_cli_video import OpenArtCLIVideo
    assert execution.preflight(OpenArtCLIVideo(),inputs)['request_sha256']==execution.approved_request_digest(scope['requests']['interior'],project_dir=root)
    selected['entry']['review']['reviewer']='later review'
    write(root/'artifacts/selected_attempts.json',selected)
    with pytest.raises(execution.ProductionGovernanceError,match='review changed'):
        execution.preflight(OpenArtCLIVideo(),inputs)
    assert not (root/'production_attempts').exists()


def test_measured_audio_longer_than_approved_dialogue_windows_blocks(package):
    measured(package)
    root,inputs,native,profile,compiled,review=package
    timing=compiled['timing'];timing['audio']['measurement']['duration_seconds']=7
    approval=json.loads((root/'audio-approval.json').read_text())
    approval['measurement_sha256']=request.digest(timing['audio']['measurement'])
    write(root/'audio-approval.json',approval)
    timing['audio']['approval_sha256']=file_sha256(root/'audio-approval.json')
    timing['action_windows'][0].update(start_seconds=7,end_seconds=7.2)
    timing['action_windows'][1].update(start_seconds=7.2,end_seconds=7.4)
    publish(package)
    with pytest.raises(ValueError,match='approved dialogue windows'):
        request.validate_preparation(inputs,native,profile)


@pytest.mark.parametrize('origin',['scene_plan','approved_handoff','unapproved_handoff','reference_role'])
def test_declared_pin_omitted_from_inputs_blocks_zero_cli_zero_attempts(package,origin):
    root,inputs,native,profile,compiled,review=package
    assert not any(k in inputs for k in ('endpoint_requirement_id','last_image_path','last_frame','end_frame'))
    if origin=='scene_plan':
        scene=json.loads((root/'artifacts/scene_plan.json').read_text())
        scene['metadata']={'visual_development':{'shot_cards':{'entry':{'scene_id':'entry','pinned_final_frame':
            {'required':True,'requirement_id':'pin-entry-end','end_state':'approved exact ending'}}}}}
        write(root/'artifacts/scene_plan.json',scene)
    else:
        manifest={'version':'1.0','assets':[{'id':'start','type':'image','path':'assets/start.svg','source_tool':'fixture','scene_id':'entry'}],
                  'metadata':{}}
        if origin=='reference_role':
            manifest['metadata']['reference_assets']={'end':{'asset_id':'end','scene_id':'entry','temporal_use':'last_frame','requirement_id':'pin-entry-end'}}
        else:
            manifest['metadata']['motion_handoffs']={'start':{'asset_id':'start','scene_id':'entry','approved':True,
                'pinned_final_frame':{'requirement_id':'pin-entry-end','asset_id':'end','approved':origin=='approved_handoff'}}}
        write(root/'artifacts/asset_manifest.json',manifest)
    with pytest.raises(ValueError,match='ending-frame'):
        request.compile_prompt(root,'entry')
    # Even refreshable generic sidecar hashes cannot remove an authoritative requirement.
    from tools.video.openart_cli_video import OpenArtCLIVideo
    with pytest.raises(execution.ProductionGovernanceError,match='ending-frame'):
        OpenArtCLIVideo().execute(inputs)
    assert not (root/'production_attempts').exists()


def test_recording_output_does_not_rewrite_preparation_requirements(package):
    root,inputs,native,profile,compiled,_=package
    write(root/'artifacts/asset_manifest.json',{'version':'1.0','assets':[{'id':'new-output','type':'video',
        'path':'clip.mp4','source_tool':'openart_cli_video','scene_id':'entry'}]})
    assert request.validate_preparation(inputs,native,profile)['compiled_sha256']==request.digest(compiled)


def test_frozen_proof_survives_unrelated_serial_observation(package):
    from tests.lib.test_shot_contract import dependency, refresh
    root,inputs,native,profile,compiled,review=package
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    selected=dependency((contract,root))
    observed=copy.deepcopy(contract['shots'][1]['upstream'])
    contract['shots'][1]['upstream']=[{'shot_id':'entry'}]
    refresh(contract);write(root/'artifacts/shot_contract.json',contract)
    authored=request.compile_prompt(root,'entry')
    assert authored['prompt']==inputs['prompt']
    compiled=request.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=compiled['timing'])
    publish((root,inputs,native,profile,compiled,review))
    proof=request.freeze_preparation('att-serial',inputs,native,profile)
    saved={'attempt_id':'att-serial','scope_id':'approved','shot_id':'entry','input_assets':[{'role':'image_path',
           'path':inputs['image_path'],'original_path':inputs['image_path'],'sha256':file_sha256(inputs['image_path'])}],
           'openart':{'preparation_snapshot':proof}}
    contract['shots'][1]['upstream']=observed
    refresh(contract);write(root/'artifacts/shot_contract.json',contract)
    write(root/'artifacts/selected_attempts.json',selected)
    assert request.validate_frozen_preparation(saved,{'inputs':inputs,'native':native,'profile':profile},root)==proof


@pytest.fixture
def reference_free_package(tmp_path, monkeypatch):
    from tests.lib.test_shot_contract import reference_free_contract
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'private'))
    monkeypatch.setattr(cli, '_run_checked', lambda *a, **k: pytest.fail('reference-free preparation invoked CLI'))
    contract = reference_free_contract()
    write(root/'artifacts/shot_contract.json', contract)
    write(root/'project.json', {'project_id':contract['project_id'],'story_revision':contract['story_revision'],
                              'governance':{'version':'1.0','mode':'strict'}})
    write(root/'artifacts/scene_plan.json', {'version':'1.0','scenes':[{'id':'entry','type':'generated',
        'description':'Synthetic cube action','start_seconds':0,'end_seconds':1,'script_section_id':'s1'}]})
    write(root/'artifacts/script.json', {'version':'1.0','title':'Synthetic action','total_duration_seconds':1,
        'sections':[{'id':'s1','text':'A red cube drops and comes to rest.','start_seconds':0,'end_seconds':1}]})
    authored = request.compile_prompt(root, 'entry')
    inputs = {'project_dir':str(root),'governance':{'scope_id':'approved','shot_id':'entry'},
              'prompt':authored['prompt'],'model':'fixture-model','mode':'text2video','operation':'text_to_video',
              'duration':1,'aspect_ratio':'16:9','resolution':'720p','output_path':str(root/'clip.mp4'),
              'compiled_request_id':'c1','preparation_review_id':'r1'}
    profile = {'source':'fixture','model':'fixture-model','mode':'text2video','cli_version':'fixture-0',
               'tier':'fixture-only','form_sha256':'f'*64,'form_defaults':{'duration':1},
               'profile_sha256':'e'*64,'account_id_sha256':'a'*64,'dry_run_endpoint':'POST /fixture'}
    argv = cli.native_video_argv(inputs['prompt'], model=inputs['model'], mode='text2video',duration=1,
                                 aspect_ratio='16:9',resolution='720p')
    body = {'model':inputs['model'],'media':'video','mode':'text2video','params':{'prompt':inputs['prompt'],
             'duration':1,'aspectRatio':'16:9','resolution':'720p'}}
    rid, sha = retain({'endpoint':profile['dry_run_endpoint'],'body':body},argv+['--dry-run']+cli.GLOBAL_FLAGS,'preview')
    inputs.update(native_dry_run_receipt_id=rid, native_dry_run_receipt_sha256=sha)
    native = jobs.prepare_native_request(request.controls(inputs), profile)
    shot = contract['shots'][0]
    timing = {'method':'segmented_estimate','duration_seconds':1,'language':'en','margin_seconds':0.05,
              'rationale':'Synthetic no-speech timing','overlap_policy':'serial','overlap_rationale':'Sequential action/end',
              'segments':[], 'action_windows':[
                  {'source_pointer':'/shot_contract/shots/0/'+key,'value_sha256':request.digest(shot[key]),
                   'start_seconds':start,'end_seconds':end,'rationale':'Synthetic timing fixture'}
                  for key,start,end in [('dominant_action',0,0.6),('completed_end_state',0.6,0.95)]]}
    compiled = request.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
    review = {'version':'1.0','review_id':'r1','reviewer':'synthetic textual reviewer','status':'pass',
              'subject_sha256':request.digest(compiled),'evidence_kind':'fixture_only',
              'predicates':[{'name':p,'status':'pass','severity':'critical','evidence':'Synthetic fixture only'} for p in sorted(request.PREDICATES)]}
    result = (root,inputs,native,profile,compiled,review)
    publish(result)
    return result


def test_reference_free_preparation_keeps_actual_empty_references(reference_free_package):
    root,inputs,native,profile,compiled,review = reference_free_package
    assert compiled['source_binding']['references'] == []
    assert compiled['source_binding']['upstream'] == []
    assert native['image_upload'] is None
    assert not (root/'assets').exists()
    assert request.validate_preparation(inputs,native,profile)['compiled_sha256'] == request.digest(compiled)


@pytest.mark.parametrize('change', ['prompt','source','script','mapping','model','duration','timing','review'])
def test_reference_free_preparation_refuses_drift(reference_free_package, change):
    from tests.lib.test_shot_contract import refresh
    root,inputs,native,profile,compiled,review = reference_free_package
    if change == 'prompt':
        inputs['prompt'] += 'changed'
    elif change == 'source':
        contract = json.loads((root/'artifacts/shot_contract.json').read_text())
        contract['shots'][0]['completed_end_state'] = 'A different endpoint.'
        refresh(contract)
        write(root/'artifacts/shot_contract.json', contract)
    elif change == 'script':
        script = json.loads((root/'artifacts/script.json').read_text())
        script['sections'][0]['text'] = 'Different context.'
        write(root/'artifacts/script.json', script)
    elif change == 'mapping':
        scene = json.loads((root/'artifacts/scene_plan.json').read_text())
        scene['scenes'][0]['script_section_id'] = 'missing'
        write(root/'artifacts/scene_plan.json', scene)
    elif change in ('model','duration'):
        inputs[change] = 'different' if change == 'model' else 2
    elif change == 'timing':
        compiled['timing']['action_windows'][0]['end_seconds'] = 2
        publish(reference_free_package)
    else:
        review['review_id'] = 'another-review'
        write(root/'artifacts/preparation_review-r1.json', review)
    with pytest.raises(ValueError):
        request.validate_preparation(inputs,native,profile)
    assert not (root/'production_attempts').exists()


@pytest.mark.parametrize('pin', ['pinned_start_frame','pinned_initial_frame','pinned_first_frame','pinned_final_frame'])
def test_reference_free_declared_frame_pin_refused(reference_free_package, pin):
    root,*_ = reference_free_package
    scene = json.loads((root/'artifacts/scene_plan.json').read_text())
    scene['metadata'] = {'visual_development':{'shot_cards':{'entry':{pin:{'required':True,'requirement_id':'pin-entry','end_state':'Pinned endpoint'}}}}}
    write(root/'artifacts/scene_plan.json', scene)
    with pytest.raises(ValueError, match='frame pin|ending-frame'):
        request.compile_prompt(root, 'entry')
    assert not (root/'production_attempts').exists()


@pytest.mark.parametrize('kind', ['reference_assets','motion_handoffs'])
def test_reference_free_manifest_reference_refused(reference_free_package, kind):
    root,*_ = reference_free_package
    write(root/'artifacts/asset_manifest.json', {'version':'1.0','assets':[],
          'metadata':{kind:{'ref':{'scene_id':'entry','asset_id':'ref'}}}})
    with pytest.raises(ValueError, match='reference/handoff'):
        request.compile_prompt(root, 'entry')


@pytest.mark.parametrize('field,value', [('image_path','unused.png'), ('image_upload_id','upload'),
    ('first_frame','unused.png'),('reference_images',['unused.png']),('keyframes',[]),('audio',False)])
def test_reference_free_native_reference_inputs_refused(reference_free_package, field, value):
    _,inputs,native,profile,_,_ = reference_free_package
    inputs[field] = value
    with pytest.raises(ValueError, match='native preparation invalid'):
        request.validate_preparation(inputs,native,profile)


def test_reference_free_native_mode_and_grok_refused(reference_free_package):
    root,inputs,native,profile,_,_ = reference_free_package
    wrong = copy.deepcopy(native)
    wrong['mode'] = 'image2video'
    with pytest.raises(ValueError, match='requires OpenArt text2video'):
        request.validate_preparation(inputs,wrong,profile)
    with pytest.raises(ValueError, match='requires OpenArt text2video'):
        request.compile_provider_prompt(root,'entry',provider='grok_cli')


def test_reference_free_frozen_current_and_historical_evidence(reference_free_package):
    from tests.lib.test_shot_contract import refresh
    root,inputs,native,profile,compiled,review = reference_free_package
    proof = request.freeze_preparation('att-text',inputs,native,profile)
    saved = {'attempt_id':'att-text','scope_id':'approved','shot_id':'entry','input_assets':[],
             'openart':{'preparation_snapshot':proof}}
    frozen = {'inputs':inputs,'native':native,'profile':profile}
    assert request.validate_frozen_preparation(saved,frozen,root) == proof
    assert request.validate_frozen_preparation_history(saved,frozen,root) == proof
    contract = json.loads((root/'artifacts/shot_contract.json').read_text())
    contract['shots'][0]['initial_state'] = 'A different initial state.'
    refresh(contract)
    write(root/'artifacts/shot_contract.json', contract)
    with pytest.raises(ValueError, match='stale source'):
        request.validate_frozen_preparation(saved,frozen,root)
    assert request.validate_frozen_preparation_history(saved,frozen,root) == proof
    frozen['inputs'] = dict(inputs,prompt=inputs['prompt']+'mutation')
    with pytest.raises(ValueError):
        request.validate_frozen_preparation_history(saved,frozen,root)


def test_reference_free_snapshot_tampering_refused(reference_free_package):
    root,inputs,native,profile,_,_ = reference_free_package
    proof = request.freeze_preparation('att-text',inputs,native,profile)
    saved = {'attempt_id':'att-text','scope_id':'approved','shot_id':'entry','input_assets':[],
             'openart':{'preparation_snapshot':proof}}
    path = cli.state_dir()/'preparation/att-text/review.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError, match='snapshot bytes changed'):
        request.validate_frozen_preparation(saved,{'inputs':inputs,'native':native,'profile':profile},root)


def test_legacy_prompt_and_coverage_keep_pre_variant_golden_bytes(package):
    # Captured from pre-variant HEAD; exact canonical bytes were independently
    # compared against the prior compiler, including source and compiled bindings.
    root,*_ = package
    authored = request.compile_prompt(root,'entry')
    assert request.digest(authored['prompt']) == '29298259b873227a9451f66a6fb87a7db06a55d18f997db31e4ca20290db8915'
    assert request.digest(authored['coverage']) == 'bbe98fc2f92eebe92f64bee3227f2021ed6ce1bd60112c501bfb270e215561e3'
    grok = request._compile_packet(request.source_packet(root,'entry',provider='grok_cli'),native_pointer='/arguments/prompt')
    assert grok['prompt'] == authored['prompt']
    assert request.digest(grok['coverage']) == '7f86625f33b3d425026c7bb2d5b58e34fbc23c64425fbe8633dc95e17135d02d'


def test_legacy_board_contract_still_refuses_text2video(package):
    root,inputs,_,profile,compiled,_ = package
    candidate = copy.deepcopy(inputs)
    candidate.pop('image_path')
    candidate.pop('image_upload_id')
    candidate.update(mode='text2video',operation='text_to_video')
    profile = dict(profile,mode='text2video')
    argv = cli.native_video_argv(candidate['prompt'],model=candidate['model'],mode='text2video',
        duration=8,aspect_ratio='16:9',resolution='720p')
    body = {'model':candidate['model'],'media':'video','mode':'text2video','params':{'prompt':candidate['prompt'],
        'duration':8,'aspectRatio':'16:9','resolution':'720p'}}
    rid,sha = retain({'endpoint':profile['dry_run_endpoint'],'body':body},argv+['--dry-run']+cli.GLOBAL_FLAGS,'preview')
    candidate.update(native_dry_run_receipt_id=rid,native_dry_run_receipt_sha256=sha)
    native = jobs.prepare_native_request(request.controls(candidate),profile)
    authored = request.compile_prompt(root,'entry')
    with pytest.raises(ValueError, match='needs approved start board'):
        request.prepare_compiled_request(candidate,native,profile,coverage=authored['coverage'],timing=compiled['timing'])


def test_preboard_packet_does_not_waive_reviewed_motion_boards(package):
    from lib import production_images as images
    root, *_ = package
    story = root/'artifacts/script.json'
    approval = root/'image-approval.txt'; approval.write_text('Synthetic approval of current story for board preparation.')
    packet = images.build_preboard_packet(root,story={'path':'artifacts/script.json','sha256':file_sha256(story)},
        approval={'path':'image-approval.txt','sha256':file_sha256(approval),'approved_by':'synthetic-fixture'},
        references=[],board_slots=[{'id':'missing-start','shot_id':'entry','role':'start_frame','status':'unresolved'}])
    assert packet['kind'] == 'pre_board'
    contract = json.loads((root/'artifacts/shot_contract.json').read_text())
    start = next(a for a in contract['assets'] if a['id']=='start')
    (root/start['path']).unlink()
    with pytest.raises(ValueError):
        request.build_static_source_packet(root,'entry',provider='grok_cli')


# --- U1: neutral once-authored package preparation (offline, zero provider calls) ---
UNOBSERVED = 'UNOBSERVED: requires real current semantic review.'


def _tree(root):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}


def _bytes(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {'path': name, 'sha256': hashlib.sha256(data).hexdigest()}


def _unknown_review(review_id, names):
    return {'review_id': review_id, 'reviewer': 'persistent reviewer', 'story_revision': 'r1',
        'subject_sha256': '0' * 64, 'status': 'unknown',
        'predicates': [{'name': n, 'severity': 'critical', 'status': 'unknown', 'evidence': UNOBSERVED} for n in names]}


@pytest.fixture
def neutral_package(tmp_path):
    root = tmp_path / 'neutral'
    root.mkdir()
    write(root / 'project.json', {'project_id': 'neutral-offline-boards', 'story_revision': 'r1',
        'governance': {'mode': 'strict', 'version': '1.0'}})
    story = _bytes(root, 'artifacts/story.txt', b'SYNTHETIC supplied premise and script')
    approval = dict(_bytes(root, 'notes/approval.txt', b'SYNTHETIC fixture approval'), approved_by='fixture creator')
    cast = _bytes(root, 'assets/images/cast.png', b'synthetic unchanged reference')
    cast.update(id='cast', role='identity', applicability={'story_revision': 'r1',
        'roles': ['identity', 'start_frame', 'end_frame']}, review={'review_id': 'cast-review',
        'reviewer': 'persistent reviewer', 'story_revision': 'r1', 'subject_sha256': cast['sha256'], 'status': 'pass',
        'predicates': [{'name': n, 'severity': 'critical', 'status': 'pass', 'evidence': 'SYNTHETIC declared observation'}
                       for n in ('asset_quality', 'cast_identity')]})
    def node(slot, shot, role, deps=()):
        return {'slot_id': slot, 'shot_id': shot, 'role': role, 'prompt': 'SYNTHETIC ' + slot,
            'reference_ids': ['cast'], 'image_dependencies': list(deps), 'output_path': 'assets/images/' + slot + '.png',
            'required_predicates': ['asset_quality', 'cast_identity']}
    spec = {'version': '1.1', 'story': story, 'approval': approval, 'reviewer': 'persistent reviewer', 'references': [cast],
        'image_nodes': [node('payoff', 's2', 'payoff_board'), node('start', 's1', 'start_frame'),
                        node('end', 's1', 'end_frame', ['start']), node('other', 's3', 'start_frame')],
        'shared_roles': [{'asset_id': 'cast', 'role': 'identity'}, {'asset_id': 'payoff', 'role': 'payoff_board'}],
        'shots': [{'shot_id': 's1', 'roles': [{'asset_id': 'start', 'role': 'start_frame'}, {'asset_id': 'end', 'role': 'end_frame'}], 'video_dependencies': []},
                  {'shot_id': 's2', 'roles': [{'asset_id': 'cast', 'role': 'start_frame'}, {'asset_id': 'cast', 'role': 'end_frame'}], 'video_dependencies': []},
                  {'shot_id': 's3', 'roles': [{'asset_id': 'other', 'role': 'start_frame'}, {'asset_id': 'cast', 'role': 'end_frame'}], 'video_dependencies': ['s1']}]}
    template = {'version': '1.0', 'story': {k: 'SYNTHETIC ' + k for k in ('desire', 'action', 'consequence', 'payoff')},
        'late_cast_ids': [], 'payoff_speaker_ids': [], 'payoff_asset_id': 'payoff',
        'project_review': _unknown_review('project-unobserved', ('payoff', 'late_cast', 'cast_identity', 'cast_count', 'speaker_source')),
        'assets': [{'id': n['slot_id'], 'role': n['role'], 'cast_ids': []} for n in spec['image_nodes']], 'shots': []}
    for shot in spec['shots']:
        template['shots'].append({'id': shot['shot_id'], 'initial_state': 'SYNTHETIC initial', 'endpoint_completion': 'unknown',
            'cast_ids': [], 'required_visible_speakers': [], 'dialogue': [], 'prop_body_invariants': ['SYNTHETIC invariant'],
            'allowed_transformations': [], 'transition': {'type': 'continuous', 'rationale': 'SYNTHETIC'},
            'asset_ids': [r['asset_id'] for r in shot['roles'] if r['asset_id'] != 'cast'] or ['payoff'],
            'upstream': [{'shot_id': d} for d in shot['video_dependencies']],
            'review': _unknown_review(shot['shot_id'] + '-unobserved', ('cast_identity', 'cast_count', 'completed_action',
                'speaker_source', 'possession', 'transformation'))})
    spec['motion_binding'] = {'contract_template': template, 'motion_handoffs': {}}
    package = {'version': '1.0', 'project_id': 'neutral-offline-boards', 'story_revision': 'r1',
        'title': 'Neutral supplied package', 'boards': spec,
        'shots': [{'id': sid, 'script': '  Exact supplied line ' + sid + '\n', 'action': 'Exact action ' + sid,
                   'duration_seconds': d, 'end_state': 'Exact ending ' + sid, 'preserve': ['Exact cast']}
                  for sid, d in (('s1', 3), ('s2', 4), ('s3', 5))]}
    def bind(value=None):
        data = json.dumps(value or package).encode()
        return _bytes(root, 'notes/package.json', data)
    return root, package, bind


def test_neutral_package_compile_is_pure_and_prepare_is_byte_stable(neutral_package):
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    from lib import production_boards as boards, production_images as images
    root, package, bind = neutral_package
    binding = bind()
    before = _tree(root)
    compiled = request.compile_package(root, binding)
    assert set(compiled) == {'spec', 'records'}
    assert before == _tree(root)
    result = request.prepare_package(root, binding)
    assert (result['status'], result['provider_calls'], result['reservations']) == ('prepared', 0, 0)
    assert result['motion_preflight'].startswith('preparation only;')
    records = {n: json.loads((root / 'artifacts' / (n + '.json')).read_text()) for n in ('script', 'scene_plan', 'asset_manifest')}
    assert records == compiled['records']
    for name, record in records.items():
        Draft202012Validator(load_schema(name)).validate(record)
    assert [s['text'] for s in records['script']['sections']] == [s['script'] for s in package['shots']]
    assert [s['end_seconds'] for s in records['scene_plan']['scenes']] == [3, 7, 12]
    assert ['cast', 'cast--s2', 'cast--s3'] == [a['id'] for a in records['asset_manifest']['assets']]
    assert boards.load_board_pack(root)['specification'] == package['boards']
    assert images.image_usage(root)['total'] == 0
    after = _tree(root)
    assert result == request.prepare_package(root, binding)
    assert after == _tree(root)


def test_neutral_package_conflicts_and_frozen_scopes_reject_before_writes(neutral_package):
    root, package, bind = neutral_package
    binding = bind()
    write(root / 'production_scopes.json', {'version': '1.0', 'scopes': [{'id': 'frozen', 'phase': 'sample'}]})
    before = _tree(root)
    with pytest.raises(ValueError, match='frozen'):
        request.prepare_package(root, binding)
    assert before == _tree(root)
    (root / 'production_scopes.json').unlink()
    write(root / 'artifacts/asset_manifest.json', {'different': 'existing source'})
    before = _tree(root)
    with pytest.raises(ValueError, match='conflict'):
        request.prepare_package(root, binding)
    assert before == _tree(root)


LEGACY_PACKAGE_PROMPT_DIGEST = '6a22108d59352d232b0d14436affd1930366ce1082c4e458118103c17376629b'


def test_legacy_prompt_bytes_frozen_without_compiler_marker(package):
    compiled = request.compile_prompt(package[0], 'entry')
    assert 'prompt_compiler' not in compiled['source_binding']
    assert hashlib.sha256(json.dumps([compiled['prompt'], compiled['coverage']], sort_keys=True).encode()).hexdigest() \
        == LEGACY_PACKAGE_PROMPT_DIGEST


def shootable_project(tmp_path, *, cast=True, dialogue_source='offscreen'):
    """C87-shaped prospective shot: named cast, offscreen reply, camera and no-music sound intent."""
    from lib.shot_contract import project_review_subject, shot_review_subject
    root = tmp_path / 'shootable'
    contract = json.loads((FIXTURES / 'valid_shot_contract.json').read_text())
    for asset in contract['assets']:
        path = root / asset['path']; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((FIXTURES / 'synthetic-board.svg').read_bytes())
        asset['sha256'] = file_sha256(path); asset['review']['subject_sha256'] = asset['sha256']
    shot = contract['shots'][0]
    contract.update(planning_mode='shot_local_v1', prompt_compiler='shootable_v1', cast_names={
        'patient': 'Tung, the honey-brown cylindrical wooden figure', 'doctor': 'Sahur, the tall pale nurse'})
    if cast:
        shot['cast_ids'] = ['patient', 'doctor']; shot['asset_ids'] = shot['asset_ids'] + ['doctor']
        shot['required_visible_speakers'] = ['patient']
        shot['dialogue'] = [dict(speaker_id='patient', source='visible', text='Help me.', start_seconds=1, end_seconds=2),
                            dict(speaker_id='doctor', source=dialogue_source, text='Bro, let go of the fork.',
                                 start_seconds=3, end_seconds=4.5)]
        shot['allowed_transformations'] = [{'id': 'bend', 'description': 'The fork bends in his grip.',
                                            'preserved_invariants': ['Patient identity and body count remain fixed.']}]
    else:
        contract['cast_names'] = {}
        for key in ('late_cast_ids', 'payoff_speaker_ids'):
            contract[key] = []
        contract['assets'] = [a for a in contract['assets'] if a['role'] != 'identity_reference']
        shot.update(cast_ids=[], asset_ids=['start', 'end'], initial_state='An empty diner booth at dawn.',
                    dominant_action='A plate slides to the table edge.', completed_end_state='The plate rests on the edge.',
                    prop_body_invariants=['One white plate.'])
    shot['camera'] = 'Slow push-in from waist height to a close-up on the hands.'
    shot['sound'] = 'Dialogue and cutlery clatter SFX only; no background music.'
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for s in contract['shots']: s['review']['subject_sha256'] = shot_review_subject(contract, s['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    write(root / 'project.json', {'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
                                  'governance': {'version': '1.0', 'mode': 'strict'}})
    write(root / 'artifacts/scene_plan.json', {'version': '1.0', 'scenes': [{
        'id': 'entry', 'type': 'generated', 'description': 'Fixture entry', 'start_seconds': 0, 'end_seconds': 8,
        'script_section_id': 's1', 'framing': 'Medium two-shot across the diner table.',
        'shot_language': {'shot_size': 'medium', 'camera_movement': 'dolly_in', 'lens_mm': 35}}]})
    write(root / 'artifacts/script.json', {'version': '1.0', 'title': 'F', 'total_duration_seconds': 8,
        'sections': [{'id': 's1', 'text': 'Help me. Bro, let go of the fork.', 'start_seconds': 0, 'end_seconds': 8}]})
    return root, contract


def test_shootable_prompt_is_clean_named_and_fully_covered(tmp_path):
    root, contract = shootable_project(tmp_path)
    compiled = request.compile_prompt(root, 'entry')
    prompt = compiled['prompt']
    assert compiled['source_binding']['prompt_compiler'] == 'shootable_v1'
    assert compiled['source_binding']['references']  # canonical refs stay bound, outside the text
    # 'patient' remains authored prose in the fixture's action text; the ID-only cast member must not appear.
    for leak in ('Endpoint completion', 'unknown', 'Cast ids', 'doctor', 'identity_reference',
                 'start_frame', 'Reference', 'sha256', '_'):
        assert leak not in prompt
    assert ('Spoken line 1: Tung, the honey-brown cylindrical wooden figure (on screen), from 1s to 2s, '
            'says exactly: "Help me."') in prompt
    assert ('Spoken line 2: Sahur, the tall pale nurse (off screen, voice only), from 3s to 4.5s, '
            'says exactly: "Bro, let go of the fork."') in prompt
    assert 'Tung, the honey-brown cylindrical wooden figure must be clearly visible on screen while speaking.' in prompt
    for line in ('Camera: Slow push-in from waist height to a close-up on the hands.',
                 'Sound: Dialogue and cutlery clatter SFX only; no background music.',
                 'Framing: Medium two-shot across the diner table.', 'Shot style: medium shot, dolly in camera, 35mm lens.',
                 'Allowed change: The fork bends in his grip.',
                 'During this change keep: Patient identity and body count remain fixed.',
                 'Story context (do not speak or print): '):
        assert line in prompt
    # Every authored leaf is a literal, ordered, non-overlapping span whose bytes hash to the source.
    end = 0
    for span in compiled['coverage']:
        assert span['start'] >= end and prompt[span['start']:span['end']]
        assert request.digest(prompt[span['start']:span['end']]) == span['fragment_sha256'] == span['value_sha256']
        end = span['end']
    pointers = {span['source_pointer'] for span in compiled['coverage']}
    assert {'/shot_contract/shots/0/camera', '/shot_contract/shots/0/sound', '/shot_contract/cast_names/doctor',
            '/shot_contract/shots/0/dialogue/1/text', '/shot_contract/shots/0/allowed_transformations/0/description',
            '/scene_plan/scenes/0/framing'} <= pointers
    assert request.compile_prompt(root, 'entry') == compiled  # deterministic replay


def test_shootable_castless_shot_needs_no_dummy_names(tmp_path):
    root, _ = shootable_project(tmp_path, cast=False)
    prompt = request.compile_prompt(root, 'entry')['prompt']
    assert 'Characters in this shot' not in prompt and 'Spoken line' not in prompt
    assert 'No spoken lines in this shot.' in prompt
    assert 'Sound: Dialogue and cutlery clatter SFX only; no background music.' in prompt


@pytest.mark.parametrize('end_state', [
    'The plate is still sliding toward the edge.',
    'The plate has fallen and shattered on the floor.',
])
def test_shootable_ending_label_preserves_authored_state(tmp_path, end_state):
    from lib.shot_contract import project_review_subject, shot_review_subject
    root, contract = shootable_project(tmp_path, cast=False)
    shot = contract['shots'][0]
    # Identical authored directions at distinct source pointers remain distinct.
    shot['dominant_action'] = shot['completed_end_state'] = end_state
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for s in contract['shots']:
        s['review']['subject_sha256'] = shot_review_subject(contract, s['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    before = _tree(root)
    compiled = request.compile_prompt(root, 'entry')
    assert 'End of clip: ' + end_state + '\n' in compiled['prompt']
    assert 'the action is finished on screen' not in compiled['prompt']
    assert compiled['prompt'].count(end_state) == 2
    spans = [span for span in compiled['coverage'] if span['source_pointer'] in {
        '/shot_contract/shots/0/dominant_action', '/shot_contract/shots/0/completed_end_state'}]
    assert len(spans) == 2 and spans[0]['end'] <= spans[1]['start']
    for span in spans:
        assert compiled['prompt'][span['start']:span['end']] == end_state
        assert span['value_sha256'] == span['fragment_sha256'] == request.digest(end_state)
    assert _tree(root) == before


@pytest.mark.parametrize('provider,model', [
    ('openart_cli', None), ('grok_cli', None), ('openart_mcp', 'fixture-model'), ('openart_mcp', 'smart-shot')])
@pytest.mark.parametrize('labels', ['neutral', 'old_ending', 'old_ending_and_action'])
@pytest.mark.parametrize('no_music', [False, True])
def test_historical_shootable_labels_keep_exact_original_coverage(tmp_path, provider, model, labels, no_music):
    from lib.shot_contract import project_review_subject, shot_review_subject
    root, contract = shootable_project(tmp_path)
    del contract['shots'][0]['sound']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for s in contract['shots']:
        s['review']['subject_sha256'] = shot_review_subject(contract, s['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    before = _tree(root)
    packet = request._historical_source_packet(root, 'entry', provider=provider)
    retained = copy.deepcopy(packet)
    for row in retained['occurrences']:
        if row['source_pointer'].endswith('/completed_end_state'):
            row['before'] = ('End of clip: ' if labels == 'neutral' else
                             'Ending frame (the action is finished on screen): ')
        if labels == 'old_ending_and_action' and row['source_pointer'].endswith('/dominant_action'):
            row['before'] = 'Main action (one continuous beat): '
    pointer = ('/arguments/prompt' if provider == 'grok_cli' else
               '/params/sceneDescription' if model == 'smart-shot' else '/params/prompt')
    original = request._compile_packet(retained, native_pointer=pointer)
    prompt = original['prompt'] + (request.NO_MUSIC_DIRECTION if no_music else '')
    if provider == 'grok_cli':
        prompt = prompt.rstrip('\n')  # The native Grok seal retains this spelling.
    canonical = request._compile_historical_provider_prompt(root, 'entry', provider=provider,
        native={'model': model, 'body': {}})
    assert request.historical_preparation_matches(prompt, original['coverage'], canonical)
    assert not request.historical_preparation_matches(prompt + ' Changed direction.', original['coverage'], canonical)
    assert not request.historical_preparation_matches(
        prompt.replace(contract['shots'][0]['completed_end_state'], 'A different authored ending.'),
        original['coverage'], canonical)
    assert not request.historical_preparation_matches(
        prompt.replace('End of clip: ', 'End of clip (all motion stops): ').replace(
            'Ending frame (the action is finished on screen): ', 'Ending frame (all motion stops): '),
        original['coverage'], canonical)
    shifted = copy.deepcopy(original['coverage'])
    shifted[-1]['start'] += 1
    assert not request.historical_preparation_matches(prompt, shifted, canonical)
    if labels != 'neutral':
        assert not request.historical_preparation_matches(prompt, canonical['coverage'], canonical)
    # Original request text, source files and hashes are only read, never migrated.
    assert _tree(root) == before


def test_shootable_missing_name_fails_closed(tmp_path):
    root, contract = shootable_project(tmp_path)
    from lib.shot_contract import project_review_subject, shot_review_subject
    del contract['cast_names']['doctor']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for s in contract['shots']: s['review']['subject_sha256'] = shot_review_subject(contract, s['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    with pytest.raises(ValueError, match='cast_names'):
        request.compile_prompt(root, 'entry')


@pytest.mark.parametrize('field', ['camera', 'sound', 'cast_name'])
def test_shootable_tamper_is_detected(tmp_path, field):
    from lib.shot_contract import project_review_subject, shot_review_subject
    root, contract = shootable_project(tmp_path)
    original = request.compile_prompt(root, 'entry')
    if field == 'cast_name': contract['cast_names']['doctor'] = 'Someone else'
    else: contract['shots'][0][field] = 'Changed direction.'
    write(root / 'artifacts/shot_contract.json', contract)
    with pytest.raises(ValueError, match='stale review'):  # unreviewed edit cannot compile
        request.compile_prompt(root, 'entry')
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for s in contract['shots']: s['review']['subject_sha256'] = shot_review_subject(contract, s['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    rereviewed = request.compile_prompt(root, 'entry')  # re-reviewed edit binds a new contract digest
    assert rereviewed['source_binding']['contract_sha256'] != original['source_binding']['contract_sha256']
    assert rereviewed['prompt'] != original['prompt']


def test_compact_native_templates_preserve_timed_and_shared_consumers(neutral_package):
    from tests.lib.test_production_package_authoring import packages
    root, base, bind = neutral_package
    compact, _ = packages(base)
    first, second = compact['shots']
    first['duration_seconds'] = second['duration_seconds'] = 6
    boundary = copy.deepcopy(first['boards'][0]); boundary.update(slot_id='boundary', role='end_frame')
    middle = copy.deepcopy(first['boards'][0]); middle.update(slot_id='middle', role='timed_keyframe', time_seconds=2)
    first['boards'] += [boundary, middle]
    first['roles'] = [{'asset_id': 's1-start', 'role': 'start_frame'},
                      {'asset_id': 'boundary', 'role': 'end_frame'},
                      {'asset_id': 'middle', 'role': 'timed_keyframe', 'time_seconds': 2}]
    second['motion']['frame_input'] = {'asset_id': 'boundary', 'role': 'start_frame'}
    second['roles'] = [{'asset_id': 'boundary', 'role': 'start_frame'}]
    expanded = request._expand_compact_package(compact)
    assert len(expanded['boards']['image_nodes']) == 4
    assert expanded['boards']['shots'][0]['roles'][-1]['time_seconds'] == 2
    templates = request.build_video_request_templates(compact, {'provider': 'grok_cli', 'operation': 'reference_to_video', 'resolution': '720p'})
    assert templates['s1']['last_image_path'] == templates['s2']['image_path'] == {'$board': {'asset_id': 'boundary'}}
    assert templates['s1']['keyframes'] == [{'image': {'$board': {'asset_id': 'middle'}}, 'timestamp_s': 2}]
    request.compile_package(root, bind(compact))


@pytest.mark.parametrize('defect', [None, 'missing_end', 'end_role', 'end_hash', 'missing_middle', 'middle_time', 'extra_middle_time'])
def test_native_consumers_bind_exact_end_and_timed_sources(tmp_path, defect):
    from tests.lib.test_production_images import PNG
    from lib.shot_contract import file_sha256
    from tools.video.grok_cli_video import build_native_video_request
    paths = []
    for index in range(7):
        path = tmp_path / (str(index) + '.png'); path.write_bytes(PNG)
        paths.append(str(path))
    inputs = {'prompt': 'SYNTHETIC exact declared native roles', 'duration': 6, 'resolution': '720p',
              'operation': 'first_last_frame', 'image_path': paths[0], 'last_image_path': paths[1],
              'keyframes': [{'image': paths[i+2], 'timestamp_s': i+1} for i in range(4)],
              'reference_image_paths': [paths[6]]}
    native = {'provider': 'grok_cli', 'request': build_native_video_request(inputs, adapter_version='SYNTHETIC')}
    submitted = [{'role': a['role'], 'source_path': a['path'], 'source_sha256': a['sha256'],
                  **({'timestamp_s': a['timestamp_s']} if 'timestamp_s' in a else {})}
                 for a in native['request']['input_assets']]
    # Unique source hashes are not required: one image can intentionally serve
    # different declared times/roles. Membership must still bind role and time.
    refs = [{'id': 'end', 'role': 'end_frame', 'sha256': file_sha256(paths[1]), 'native_required': True},
            *[{'id': 'middle'+str(i), 'role': 'timed_keyframe', 'sha256': file_sha256(paths[i+2]), 'time_seconds': i+1} for i in range(4)],
            {'id': 'ref', 'role': 'reference_image', 'sha256': file_sha256(paths[6])}]
    packet = {'binding': {'references': refs, 'frame_input': {'asset_id': 'start', 'sha256': file_sha256(paths[0]), 'path': paths[0]}}}
    if defect == 'missing_end': submitted = [a for a in submitted if a['role'] != 'last_frame']
    if defect == 'end_role': next(a for a in submitted if a['role'] == 'last_frame')['role'] = 'reference'
    if defect == 'end_hash': next(a for a in submitted if a['role'] == 'last_frame')['source_sha256'] = 'f'*64
    if defect == 'missing_middle': submitted = [a for a in submitted if a.get('timestamp_s') != 2]
    if defect == 'middle_time': next(a for a in submitted if a.get('timestamp_s') == 2)['timestamp_s'] = 2.5
    if defect == 'extra_middle_time': submitted.append({'role': 'keyframe', 'source_path': paths[2], 'source_sha256': file_sha256(paths[2]), 'timestamp_s': 2.5})
    if defect:
        with pytest.raises(ValueError): request._validate_frame_native(packet, native, submitted=submitted)
    else:
        request._validate_frame_native(packet, native, submitted=submitted)
        assert native['request']['arguments']['keyframes'] == [{'image': paths[i+2], 'timestamp_s': i+1} for i in range(4)]
        assert native['request']['arguments']['images'] == [paths[6]]


def test_openart_planning_retains_nested_board_sources(neutral_package):
    from tests.lib.test_production_package_authoring import packages
    from lib import production_execution as execution
    root, base, bind = neutral_package
    package, _ = packages(base)
    request.prepare_package(root, bind(package))
    inputs = {'duration': 6, 'input_assets': [{'role': 'first_frame', 'source_path': {'$board': {'asset_id': 's1-start'}}}]}
    template = execution.planned_request_template(inputs, project_dir=root)
    assert template == {'inputs': inputs, 'static_input_assets': []}
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(ValueError): execution.approved_request_digest(template, project_dir=root)
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('defect', [None, 'role', 'time', 'hash', 'omitted'])
def test_original_reference_projection_preserves_old_and_new_bytes(neutral_package, defect):
    from tests.lib.test_production_package_authoring import packages
    root, base, bind = neutral_package
    package, _ = packages(base)
    package['shots'][0]['roles'] = [{'asset_id': 's1-start', 'role': 'reference_image'}]
    binding = bind(package)
    scene = {'metadata': {'package': binding}}
    original = [{'id': 's1-start', 'role': 'start_frame', 'cast_ids': [], 'sha256': 'a'*64, 'review_sha256': 'b'*64}]
    contract = {'assets': [{'id': 's1-start', 'role': 'start_frame'}]}
    legacy = copy.deepcopy(original)
    assert request._replay_reference_projection(root, 's1', scene, original, legacy, contract) == legacy
    saved = copy.deepcopy(original); saved[0]['role'] = 'reference_image'
    if defect == 'role': saved[0]['role'] = 'end_frame'
    if defect == 'time': saved[0]['time_seconds'] = 2
    if defect == 'hash': saved[0]['sha256'] = 'c'*64
    if defect == 'omitted': saved = []
    if defect:
        with pytest.raises(ValueError): request._replay_reference_projection(root, 's1', scene, original, saved, contract)
    else:
        assert request._replay_reference_projection(root, 's1', scene, original, saved, contract) == saved


@pytest.mark.parametrize('field,changed', [('role', 'reference_image'), ('time_seconds', 3), ('native_required', True)])
def test_retained_consumer_metadata_cannot_expand_original_package(neutral_package, field, changed):
    from tests.lib.test_production_package_authoring import packages
    root, base, bind = neutral_package
    package, _ = packages(base)
    package['shots'][0]['roles'] = [{'asset_id': 's1-start', 'role': 'timed_keyframe', 'time_seconds': 2}]
    original = [{'id': 's1-start', 'role': 'start_frame', 'cast_ids': [], 'sha256': 'a'*64, 'review_sha256': 'b'*64}]
    saved = [{**original[0], 'role': 'timed_keyframe', 'time_seconds': 2}]
    scene = {'metadata': {'package': bind(package)}}
    contract = {'assets': [{'id': 's1-start', 'role': 'start_frame'}]}
    assert request._replay_reference_projection(root, 's1', scene, original, saved, contract) == saved
    saved[0][field] = changed
    with pytest.raises(ValueError): request._replay_reference_projection(root, 's1', scene, original, saved, contract)


def test_shared_timed_image_keeps_each_consumers_timestamp(neutral_package):
    from tests.lib.test_production_package_authoring import packages
    from tests.lib.test_production_images import PNG
    from lib import production_execution as execution
    root, base, bind = neutral_package
    package, _ = packages(base)
    first, second = package['shots']; first['duration_seconds'] = 6; second['duration_seconds'] = 2
    node = copy.deepcopy(first['boards'][0]); node.update(slot_id='shared-middle', role='timed_keyframe')
    first['boards'].append(node)
    first['roles'] = [{'asset_id':'s1-start','role':'start_frame'}, {'asset_id':'shared-middle','role':'timed_keyframe','time_seconds':3}]
    second['roles'] = [{'asset_id':'s2-start','role':'start_frame'}, {'asset_id':'shared-middle','role':'timed_keyframe','time_seconds':1}]
    templates = request.build_video_request_templates(package, {'provider':'grok_cli','operation':'reference_to_video','resolution':'720p'})
    assert templates['s1']['keyframes'][0]['timestamp_s'] == 3
    assert templates['s2']['keyframes'][0]['timestamp_s'] == 1
    request.prepare_package(root, bind(package))
    contract = execution.load_shot_contract(root)
    for asset in contract['assets']:
        path = root / ('assets/' + asset['id'] + '.png'); path.write_bytes(PNG)
        asset.update(path=str(path.relative_to(root)), sha256=hashlib.sha256(PNG).hexdigest())
    assert next(a for a in contract['assets'] if a['id'] == 'shared-middle')['time_seconds'] == 3
    inputs = {'duration':2, 'operation':'reference_to_video', 'image_path':str(root/'assets/s2-start.png'),
              'keyframes':[{'image':str(root/'assets/shared-middle.png'),'timestamp_s':1}]}
    execution._check_motion_inputs(contract, 's2', inputs, root)
    inputs['keyframes'][0]['timestamp_s'] = 3
    with pytest.raises(ValueError, match='source/time'): execution._check_motion_inputs(contract, 's2', inputs, root)


@pytest.mark.parametrize('primary_role', ['reference_image', 'timed_keyframe'])
@pytest.mark.parametrize('defect', [None, 'missing', 'wrong_field', 'wrong_hash'])
def test_explicit_endpoint_validates_consumer_instead_of_primary_role(neutral_package, primary_role, defect):
    from tests.lib.test_production_package_authoring import packages
    from lib import production_execution as execution
    root, base, bind = neutral_package
    package, _ = packages(base)
    first = package['shots'][0]
    node = copy.deepcopy(first['boards'][0]); node.update(slot_id='native-end', role=primary_role)
    if primary_role == 'timed_keyframe': node['time_seconds'] = 2
    first['boards'].append(node)
    first['roles'] = [{'asset_id':'s1-start', 'role':'start_frame'}, {'asset_id':'native-end', 'role':'end_frame'}]
    request.prepare_package(root, bind(package))
    contract = execution.load_shot_contract(root)
    for asset in contract['assets']:
        path = root / ('assets/' + asset['id'] + '.png'); path.write_bytes(asset['id'].encode())
        asset.update(path=str(path.relative_to(root)), sha256=file_sha256(path))
    inputs = {'duration':first['duration_seconds'], 'operation':'image_to_video',
              'image_path':str(root/'assets/s1-start.png'), 'last_image_path':str(root/'assets/native-end.png')}
    if defect == 'missing': inputs.pop('last_image_path')
    elif defect == 'wrong_field': inputs['reference_image_paths'] = [inputs.pop('last_image_path')]
    elif defect == 'wrong_hash': inputs['last_image_path'] = inputs['image_path']
    if defect:
        with pytest.raises(ValueError, match='end_frame native-end'):
            execution._check_motion_inputs(contract, 's1', inputs, root)
    else:
        execution._check_motion_inputs(contract, 's1', inputs, root)


@pytest.mark.parametrize('defect', [None, 'missing_start', 'missing_end', 'wrong_start', 'wrong_end', 'missing_cast', 'wrong_cast', 'operation'])
def test_explicit_native_pin_pair_preserves_exact_identity_carrier_conditions(neutral_package, defect):
    from tests.lib.test_production_package_authoring import packages
    from lib import production_execution as execution
    root, base, bind = neutral_package
    package, _ = packages(base)
    first = package['shots'][0]
    node = copy.deepcopy(first['boards'][0]); node.update(slot_id='native-end', role='reference_image')
    first['boards'].append(node)
    first['roles'] = [{'asset_id':'s1-start', 'role':'start_frame'}, {'asset_id':'native-end', 'role':'end_frame'}]
    request.prepare_package(root, bind(package))
    contract = execution.load_shot_contract(root)
    contract['shots'][0]['cast_ids'] = ['actor']
    contract['assets'].append({'id':'actor', 'role':'identity_reference', 'cast_ids':['actor']})
    for asset in contract['assets']:
        path = root / ('assets/' + asset['id'] + '.png'); path.write_bytes(asset['id'].encode())
        asset.update(path=str(path.relative_to(root)), sha256=file_sha256(path), cast_ids=['actor'])
    inputs = {'duration':first['duration_seconds'], 'operation':'first_last_frame',
              'first_frame':str(root/'assets/s1-start.png'), 'last_frame':str(root/'assets/native-end.png')}
    if defect in {'missing_start','missing_end'}: inputs.pop('first_frame' if defect == 'missing_start' else 'last_frame')
    elif defect in {'wrong_start','wrong_end'}: inputs['first_frame' if defect == 'wrong_start' else 'last_frame'] = str(root/'assets/actor.png')
    elif defect in {'missing_cast','wrong_cast'}:
        next(a for a in contract['assets'] if a['id'] == 's1-start')['cast_ids'] = [] if defect == 'missing_cast' else ['other']
    elif defect == 'operation': inputs['operation'] = 'reference_to_video'
    if defect:
        with pytest.raises(ValueError, match='submitted inputs omit or change'):
            execution._check_motion_inputs(contract, 's1', inputs, root)
    else:
        execution._check_motion_inputs(contract, 's1', inputs, root)


def test_primary_timed_board_projects_only_timed_consumers_time(neutral_package, monkeypatch):
    from tests.lib.test_production_package_authoring import packages
    from lib import production_execution as execution, shot_contract
    root, base, bind = neutral_package
    package, _ = packages(base)
    first = package['shots'][0]
    first['boards'][0].update(role='timed_keyframe', time_seconds=2)
    first['roles'] = [{'asset_id':'s1-start','role':'start_frame'},
                      {'asset_id':'s1-start','role':'timed_keyframe','time_seconds':1}]
    request.prepare_package(root, bind(package))
    contract = execution.load_shot_contract(root)
    for asset in contract['assets']:
        path = root / ('assets/' + asset['id'] + '.png'); path.write_bytes(asset['id'].encode())
        asset.update(path=str(path.relative_to(root)), sha256=file_sha256(path), review=copy.deepcopy(contract['project_review']))
        asset['review']['subject_sha256'] = asset['sha256']
    write(root/'artifacts/shot_contract.json', contract)
    monkeypatch.setattr(request, 'validate_shot_contract', lambda *a, **kw: {'eligible':True,'errors':[]})
    monkeypatch.setattr(shot_contract, '_validate_original_shot_contract', lambda *a, **kw: {'eligible':True,'errors':[]})
    monkeypatch.setattr(shot_contract, 'frame_conditioned_start', lambda *a: {'asset_id':'s1-start', 'sha256':contract['assets'][0]['sha256'], 'path':str(root/'assets/s1-start.png')})
    packet = request.source_packet(root, 's1', provider='grok_cli', check_native_controls=False)
    refs = [row for row in packet['binding']['references'] if row['id'] == 's1-start']
    assert {row['role']:row.get('time_seconds') for row in refs} == {'start_frame':None, 'timed_keyframe':1}
    replay = request._historical_source_packet(root, 's1', provider='grok_cli', check_native_controls=False,
        reference_bindings=packet['binding']['references'])
    assert replay['binding']['references'] == packet['binding']['references']


@pytest.mark.parametrize('guided', [False, True])
def test_legacy_board_consumers_retain_semantic_endpoint_behavior(neutral_package, guided):
    from lib import production_execution as execution
    from tests.lib.test_production_factory import prospective_package
    root, package, bind = neutral_package
    request.prepare_package(root, bind(prospective_package(package)))
    contract = execution.load_shot_contract(root)
    for asset in contract['assets']:
        path = root / ('assets/' + asset['id'] + '.png'); path.write_bytes(asset['id'].encode())
        asset.update(path=str(path.relative_to(root)), sha256=file_sha256(path))
    if guided: contract['reference_mode'] = 'reference_guided'
    inputs = {'duration':3, 'operation':'reference_to_video' if guided else 'image_to_video',
              **({'reference_image_paths':[str(root/'assets/start.png')]} if guided
                 else {'image_path':str(root/'assets/start.png')})}
    execution._check_motion_inputs(contract, 's1', inputs, root)


def test_compact_guided_payoff_stays_semantic_without_native_consumer(neutral_package, monkeypatch):
    from tests.lib.test_production_package_authoring import packages
    from tests.lib.test_production_images import PNG
    from lib import production_execution as execution
    root, base, bind = neutral_package
    package, _ = packages(base)
    for shot in package['shots']:
        shot['motion']['reference_mode'] = 'reference_guided'; shot['motion'].pop('frame_input')
    package['shots'][0]['roles'] = [{'asset_id':'s1-start','role':'reference_image'}, {'asset_id':'s2-start','role':'payoff_board'}]
    package['shots'][1]['roles'] = [{'asset_id':'s1-start','role':'reference_image'}, {'asset_id':'s2-start','role':'payoff_board'}]
    request.prepare_package(root, bind(package))
    contract = execution.load_shot_contract(root)
    for asset in contract['assets']:
        path = root / ('assets/' + asset['id'] + '.png'); path.write_bytes(PNG)
        asset.update(path=str(path.relative_to(root)), sha256=hashlib.sha256(PNG).hexdigest())
    write(root/'artifacts/shot_contract.json', contract)
    for asset in contract['assets']:
        asset['review'] = copy.deepcopy(contract['project_review'])
        asset['review']['subject_sha256'] = asset['sha256']
    write(root/'artifacts/shot_contract.json', contract)
    monkeypatch.setattr(request, 'validate_shot_contract', lambda *a, **kw: {'eligible':True,'errors':[]})
    from lib import shot_contract
    monkeypatch.setattr(shot_contract, '_validate_original_shot_contract', lambda *a, **kw: {'eligible':True,'errors':[]})
    packet = request.source_packet(root, 's1', provider='openart_mcp', check_native_controls=False)
    payoff = next(r for r in packet['binding']['references'] if r['id'] == 's2-start')
    assert payoff['role'] == 'payoff_board' and payoff['native_required'] is False
    native = {'provider':'openart_mcp', 'mode':'element2video', 'input_assets':[{'role':'reference_image', 'source_sha256':contract['assets'][0]['sha256']}]}
    request.validate_openart_asset_roles(packet, native)
    replay = request._historical_source_packet(root, 's1', provider='openart_mcp', check_native_controls=False,
        reference_bindings=packet['binding']['references'])
    assert replay['binding']['references'] == packet['binding']['references']
    package['shots'][0]['roles'].append({'asset_id':'s2-start','role':'end_frame'})
    changed = bind(package)
    scene = {'metadata':{'package':changed}}
    original = [{k:v for k,v in payoff.items() if k != 'native_required'}]
    with pytest.raises(ValueError, match='native requirement'):
        request._replay_reference_projection(root, 's1', scene, original, [payoff], contract)


def test_grok_guided_route_requires_exact_operation():
    projection = {'shot': {'id': 's1', 'reference_mode': 'reference_guided'}, 'contract_snapshot': {}}
    assert request.route_treatment_error(projection, provider='grok_cli', operation='reference_to_video') is None
    for operation in (None, 'image_to_video', 'first_last_frame'):
        assert 'requires reference_to_video' in request.route_treatment_error(projection, provider='grok_cli', operation=operation)


@pytest.mark.parametrize('damage', [None, 'operation', 'missing', 'foreign', 'wrong_role', 'unsupported'])
def test_grok_guided_binds_exact_shot_reference_consumers(tmp_path, damage):
    path = str(tmp_path / 'identity.png')
    packet = {'binding': {'references': [{'id': 'tung', 'role': 'reference_image', 'sha256': 'a'*64, 'path': path}]}}
    native = {'request': {'operation': 'reference_to_video', 'input_assets': [{'role': 'reference', 'sha256': 'a'*64, 'path': path}]}}
    if damage == 'operation': native['request']['operation'] = 'first_last_frame'
    if damage == 'missing': native['request']['input_assets'] = []
    if damage == 'foreign': native['request']['input_assets'].append({'role': 'reference', 'sha256': 'b'*64, 'path': path})
    if damage == 'wrong_role': native['request']['input_assets'][0]['role'] = 'first_frame'
    if damage == 'unsupported': packet['binding']['references'].append({'id': 'audio', 'role': 'reference_audio', 'sha256': 'c'*64, 'path': path})
    if damage:
        with pytest.raises(ValueError): request._validate_grok_guided_assets(packet, native)
    else:
        request._validate_grok_guided_assets(packet, native)


@pytest.mark.parametrize('damage', ['unknown', 'missing_predicate', 'contradictory_pass'])
def test_factory_report_only_preparation_validates_before_immutable_publication(generated_carrier_preparation, damage):
    from tests.lib.test_production_factory import file_bytes
    root, inputs, native, profile, compiled, review = generated_carrier_preparation
    inputs = dict(inputs, compiled_request_id='factory-observation', preparation_review_id='factory-observation')
    judgment = {k: copy.deepcopy(v) for k, v in review.items()
                if k not in {'review_id', 'subject_sha256', 'composite_boards'}}
    judgment['status'] = 'unknown'
    for predicate in judgment['predicates']:
        predicate['status'] = 'unknown'
    if damage == 'missing_predicate':
        judgment['predicates'].pop()
    elif damage == 'contradictory_pass':
        judgment['status'] = 'pass'
    before = file_bytes(root)
    kwargs = dict(coverage=compiled['coverage'], timing=compiled['timing'], review=judgment, persist=True)
    if damage == 'unknown':
        _, bound = request.prepare_compiled_request(inputs, native, profile, **kwargs)
        assert bound['status'] == 'unknown'
        assert all(p['status'] == 'unknown' for p in bound['predicates'])
        assert request.validate_preparation(inputs, native, profile)['review_sha256'] == request.digest(bound)
    else:
        with pytest.raises(ValueError):
            request.prepare_compiled_request(inputs, native, profile, **kwargs)
        assert file_bytes(root) == before
    assert execution._attempts(root) == []


def test_proposed_package_preflight_uses_same_structural_core_without_writes(neutral_package):
    root, package, bind = neutral_package
    binding = bind()
    expected = request.compile_package(root, binding)
    # Prospective wrappers need not exist for structural projection.
    for name in ('story', 'approval'):
        (root / package['boards'][name]['path']).unlink()
    (root / binding['path']).unlink()
    before = _tree(root)
    assert request.preflight_package(root, package, package_binding=binding) == expected
    assert _tree(root) == before
    package['boards']['image_nodes'][0]['required_predicates'] = ['cast_identity']
    package['boards']['image_nodes'][1]['required_predicates'] = ['cast_identity']
    with pytest.raises(ValueError) as exc:
        request.preflight_package(root, package, package_binding=binding)
    assert 'payoff: missing' in str(exc.value) and 'start: missing' in str(exc.value)
    assert _tree(root) == before


@pytest.mark.parametrize('pins', [[], ['first_frame'], ['first_frame', 'last_frame'], ['last_frame', 'first_frame']])
@pytest.mark.parametrize('transition', ['hard_cut', 'continuous'])
def test_h3_projection_uses_native_pins_and_preserves_literal_occurrences(tmp_path, pins, transition):
    root, contract = shootable_project(tmp_path)
    from lib.shot_contract import project_review_subject, shot_review_subject
    shot = contract['shots'][0]
    shot['transition'] = {'type': transition, 'rationale': 'The fork remains held across the boundary.'}
    shot['cast_ids'].reverse()  # Speaker IDs follow speech order, not cast inventory order.
    # Identical intended dialogue occurrences retain separate tags, speakers and spans.
    shot['dialogue'][1]['text'] = shot['dialogue'][0]['text']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for value in contract['shots']:
        value['review']['subject_sha256'] = shot_review_subject(contract, value['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    packet = request.build_static_source_packet(root, 'entry', provider='openart_mcp')
    assets = [{'role': role} for role in pins]
    compiled = request._compile_provider_packet(packet, provider='openart_mcp',
        model='fal-h3-max-turbo', input_assets=assets)
    prompt = compiled['prompt']
    assert compiled['source_binding'] == packet['binding']
    assert prompt.count('<Picture ') == len(pins)  # semantic end asset is not an extra pin
    if pins:
        assert 'at 0.00 seconds' in prompt
    if 'last_frame' in pins:
        assert 'at 8.00 seconds' in prompt
        assert prompt.index('at 0.00 seconds') < prompt.index('at 8.00 seconds')
    assert 'integrated_multimodal_description: [Shot 1]' in prompt
    assert '(S1) says: <d>Help me.</d>' in prompt
    assert '(S2) (off screen, diegetic speech) says: <d>Help me.</d>' in prompt
    assert 'voiceover' not in prompt and 'lips' not in prompt and '[English]' not in prompt
    assert prompt.count('<d>Help me.</d>') == 2
    assert 'overall_soundscape: ' + shot['sound'] in prompt
    assert prompt.endswith('non_diegetic_music: N/A\n')  # explicit music intent has its own native field
    assert 'Story context' not in prompt
    expected = [row for row in packet['occurrences'] if not request._h3_administrative_row(packet, row)]
    assert [row['occurrence_id'] for row in compiled['coverage']] == [row['occurrence_id'] for row in expected]
    omitted = {row['source_pointer'] for row in packet['occurrences']} - {
        row['source_pointer'] for row in compiled['coverage']}
    assert omitted == ({'/script/sections/0/text', '/shot_contract/shots/0/transition/rationale'}
        if transition == 'hard_cut' else {'/script/sections/0/text'})
    assert {'/shot_contract/shots/0/dominant_action', '/shot_contract/shots/0/completed_end_state',
        '/shot_contract/shots/0/prop_body_invariants/0', '/shot_contract/shots/0/dialogue/0/text',
        '/shot_contract/shots/0/dialogue/1/text', '/shot_contract/shots/0/sound'} <= {
            row['source_pointer'] for row in compiled['coverage']}
    end = 0
    for row, span in zip(expected, compiled['coverage']):
        assert span['start'] >= end
        assert prompt[span['start']:span['end']] == row['value']
        assert span['fragment_sha256'] == span['value_sha256'] == row['value_sha256']
        end = span['end']
    assert (shot['transition']['rationale'] in prompt) is (transition == 'continuous')
    historical = request._compile_historical_provider_prompt(root, 'entry', provider='openart_mcp',
        native={'model': 'fal-h3-max-turbo', 'input_assets': assets, 'body': {}})
    assert request.historical_preparation_matches(prompt, compiled['coverage'], historical)
    generic = request._compile_packet(packet)
    assert request.historical_preparation_matches(generic['prompt'], generic['coverage'], historical)
    assert request._compile_provider_packet(packet, provider='openart_mcp', model='byte-plus-seedance-2-mini') == generic


@pytest.mark.parametrize('sound,no_music', [
    ('One brief ceramic rattle, ice sliding, a dry wooden clonk and a small startled grunt. No background music.', True),
    ('No background music; a cup clinks and the door creaks.', True),
    ('Soft piano background music under dialogue and cutlery clatter.', False),
    ('Dialogue and cutlery clatter.', False),
    ('No background music is forbidden; play a jaunty piano score.', False),
])
def test_h3_projects_only_explicit_no_music_and_replays_old_native_prompt(tmp_path, sound, no_music):
    from lib.shot_contract import project_review_subject, shot_review_subject
    root, contract = shootable_project(tmp_path)
    contract['shots'][0]['sound'] = sound
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    packet = request.build_static_source_packet(root, 'entry', provider='openart_mcp')
    original_packet = copy.deepcopy(packet)
    compiled = request._compile_provider_packet(packet, provider='openart_mcp', model='fal-h3-max-turbo')
    assert packet == original_packet
    assert ('non_diegetic_music: N/A' in compiled['prompt']) is no_music
    assert 'overall_soundscape: ' + sound + '\n' in compiled['prompt']
    assert '<d>Help me.</d>' in compiled['prompt']
    assert '<d>Bro, let go of the fork.</d>' in compiled['prompt']
    for span in compiled['coverage']:
        assert request.digest(compiled['prompt'][span['start']:span['end']]) == span['value_sha256']
    # Frozen H3 requests predate the dedicated field for authored no-music sound.
    old_prompt = compiled['prompt'].removesuffix('non_diegetic_music: N/A\n')
    historical = request._compile_historical_provider_prompt(root, 'entry', provider='openart_mcp',
        native={'model': 'fal-h3-max-turbo', 'body': {}})
    before = copy.deepcopy(historical)
    assert request.historical_preparation_matches(old_prompt, compiled['coverage'], historical)
    assert request.historical_preparation_matches(compiled['prompt'], compiled['coverage'], historical)
    assert historical == before
    assert not request.historical_preparation_matches(old_prompt + ' Changed sound.\n', compiled['coverage'], historical)
    generic = request._compile_packet(packet)
    assert request._compile_provider_packet(packet, provider='openart_cli', model='fal-h3-max-turbo') == generic
    assert request.historical_preparation_matches(generic['prompt'], generic['coverage'], historical)


def test_h3_projection_preserves_no_music_default_and_rejects_missing_required_coverage(tmp_path):
    root, contract = shootable_project(tmp_path, cast=False)
    from lib.shot_contract import project_review_subject, shot_review_subject
    del contract['shots'][0]['sound']
    contract['shots'][0]['transition'] = {'type': 'hard_cut', 'rationale': 'Fresh plate vignette.'}
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    packet = request.build_static_source_packet(root, 'entry', provider='openart_mcp')
    compiled = request._compile_provider_packet(packet, provider='openart_mcp', model='fal-h3-max-turbo')
    assert compiled['prompt'].endswith('non_diegetic_music: N/A\n')
    assert 'overall_soundscape' not in compiled['prompt']
    assert compiled['prompt'].count('No spoken dialogue.') == 1
    historical = request._compile_historical_provider_prompt(root, 'entry', provider='openart_mcp',
        native={'model': 'fal-h3-max-turbo', 'body': {}})
    assert not request.historical_preparation_matches(compiled['prompt'], compiled['coverage'][1:], historical)
    changed = copy.deepcopy(compiled['coverage'])
    changed[0]['start'] += 1
    assert not request.historical_preparation_matches(compiled['prompt'], changed, historical)


def test_h3_frozen_preparation_replays_before_explicit_music_projection(reference_free_package, tmp_path, monkeypatch):
    from lib import openart_mcp as mcp
    from tests.integration.test_openart_mcp_governance import fixture_observations
    from lib.shot_contract import project_review_subject, shot_review_subject
    fixture_observations.__wrapped__(tmp_path, monkeypatch)
    root, _, _, _, _, review = reference_free_package
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    contract.update(planning_mode='shot_local_v1', prompt_compiler='shootable_v1', cast_names={})
    contract['shots'][0].update(duration_seconds=5, sound='A plate clinks. No background music.')
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    write(root / 'artifacts/shot_contract.json', contract)
    inputs = {'project_dir': str(root), 'governance': {'scope_id': 'approved', 'shot_id': 'entry'},
        'operation': 'text_to_video', 'model': 'fal-h3-max-turbo', 'mode': 'text2video',
        'native_params': {'duration': 5, 'resolution': '768P'}, 'output_path': str(root / 'clip.mp4'),
        'compiled_request_id': 'c1', 'preparation_review_id': 'r1'}
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='candidate')
    current_compile = request._compile_h3_packet
    def previous_compile(*args, **kwargs):
        compiled = current_compile(*args, **kwargs)
        compiled['prompt'] = compiled['prompt'].removesuffix('non_diegetic_music: N/A\n')
        return compiled
    # Construct the actual immutable evidence using the prior compiler spelling.
    with monkeypatch.context() as old:
        old.setattr(request, '_compile_h3_packet', previous_compile)
        authored = request.compile_provider_prompt(root, 'entry', provider='openart_mcp', model=inputs['model'])
        inputs['prompt'] = authored['prompt']
        native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
        timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en', 'margin_seconds': .05,
            'rationale': 'Synthetic fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'Synthetic fixture',
            'segments': [], 'action_windows': [
                {'source_pointer': '/shot_contract/shots/0/' + key,
                 'value_sha256': request.digest(contract['shots'][0][key]),
                 'start_seconds': start, 'end_seconds': end, 'rationale': 'Synthetic fixture'}
                for key, start, end in [('dominant_action', 0, 2), ('completed_end_state', 2, 4.9)]]}
        compiled = request.prepare_compiled_request(inputs, native, profile, coverage=authored['coverage'], timing=timing)
        review['subject_sha256'] = request.digest(compiled)
        publish((root, inputs, native, profile, compiled, review))
        proof = request.freeze_preparation('att-h3-old-music', inputs, native, profile)
    saved = {'attempt_id': 'att-h3-old-music', 'scope_id': 'approved', 'shot_id': 'entry',
             'input_assets': [], 'openart': {'preparation_snapshot': proof}}
    frozen = {'inputs': inputs, 'native': native, 'profile': profile}
    assert request.validate_frozen_preparation_history(saved, frozen, root) == proof
    assert request._validate_frozen_preparation_original(saved, frozen, root) == proof
    with pytest.raises(ValueError, match='H3 prompt differs'):
        request.validate_frozen_preparation(saved, frozen, root)
