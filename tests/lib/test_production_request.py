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
