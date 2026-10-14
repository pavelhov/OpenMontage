"""Actual ready receipts + reviewed boards + full native form + source compilation."""
import copy
import json
from pathlib import Path
import pytest
from lib import openart_mcp as mcp, production_request as prep, production_execution as execution
from tests.lib.test_openart_mcp_native import observations, source_project
from tests.lib.test_shot_contract import refresh
from tests.lib.test_production_request import write
from tests.lib.test_production_request import neutral_package  # noqa: F401


@pytest.mark.parametrize('model,mode,reference_mode,roles,duration',[
    ('fal-h3-max-turbo','image2video',None,[('first_frame','start')],5),
    ('minimax-h3','element2video','reference_guided',[('reference_image','start')],5),
    ('smart-shot','generate-shot-video','reference_guided',[('character_reference','start'),('environment_reference','end')],5),
    ('byte-plus-seedance-2-5','element2video','reference_guided',
        [('reference_image','start'),('reference_image','end')],30),
])
def test_exact_native_mode_composes_reviewed_source_without_invented_delivery_flags(source_project,monkeypatch,model,mode,reference_mode,roles,duration):
    root,assets,_,_=source_project
    if duration==30:
        account=mcp.load_account();account['data']['credits']=6248
        mcp.retain_observation('account',account)
        monkeypatch.delenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT')
        monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    contract['assets']=[a for a in contract['assets'] if a['id'] not in {'voice','motion'}]
    shot=contract['shots'][0];shot['asset_ids']=[a for a in shot['asset_ids'] if a not in {'voice','motion'}]
    shot['duration_seconds']=duration
    if reference_mode:shot['reference_mode']=reference_mode
    if duration==30:
        # The two supplied fixture sources are object/style references; this
        # offline story declares no additional off-request cast identity locks.
        shot.update(cast_ids=[],required_visible_speakers=[],dialogue=[])
        contract['late_cast_ids']=[];contract['payoff_speaker_ids']=[]
        contract['planning_mode']='shot_local_v1'
        contract['payoff_asset_id']='end'
        for asset in contract['assets']:asset['cast_ids']=[]
        next(asset for asset in contract['assets'] if asset['id']=='end')['role']='payoff_board'
    refresh(contract)
    if duration==30:
        from lib.shot_contract import project_review_subject,shot_review_subject
        contract['project_review']['subject_sha256']=project_review_subject(contract)
        for row in contract['shots']:row['review']['subject_sha256']=shot_review_subject(contract,row['id'])
    write(root/'artifacts/shot_contract.json',contract)
    write(root/'artifacts/scene_plan.json',{'version':'1.0','scenes':[{'id':'entry','type':'generated','description':'Synthetic native composition','start_seconds':0,'end_seconds':duration,'script_section_id':'s1'}]})
    write(root/'artifacts/script.json',{'version':'1.0','title':'Synthetic native composition','total_duration_seconds':duration,'sections':[{'id':'s1','text':'The patient completes the reviewed action.','start_seconds':0,'end_seconds':duration}]})
    authored=prep.compile_provider_prompt(root,'entry',provider='openart_mcp',model=model)
    params={'videoDuration':5,'videoResolution':'720p'} if model=='smart-shot' else {'duration':5,'resolution':'768P'}
    if model=='byte-plus-seedance-2-5':
        params={'duration':30,'resolution':'720p','aspectRatio':'9:16','generateAudio':True,'videoCount':1}
    inputs={'project_dir':str(root),'model':model,'mode':mode,'prompt':authored['prompt'],
        'operation':{'image2video':'image_to_video','element2video':'reference_to_video','generate-shot-video':'shot_video'}[mode],
        'native_params':params,'openart_project_id':'fixture-project','governance':{'shot_id':'entry','scope_id':'not-dispatched','stage':'generate'},
        'input_assets':[{'role':role,**assets[name]} for role,name in roles],'compiled_request_id':'original','preparation_review_id':'original'}
    if duration==30:inputs['output_path']=str(root/'assets/whole-story-original.mp4')
    if duration==30:
        authored=prep.compile_provider_prompt(root,'entry',provider='openart_mcp',model=model,
            input_assets=inputs['input_assets'],openart_project_id=inputs['openart_project_id'])
        inputs['prompt']=authored['prompt']
        assert '"native_label": "Synthetic fixture 0", "source_ids": ["start"]' in inputs['prompt']
        assert '"native_label": "Synthetic fixture 1", "source_ids": ["end"]' in inputs['prompt']
    profile=mcp.load_profile(model,mode,require='candidate')
    native=prep.prep_builder('openart_mcp')(inputs,profile)
    execution._check_motion_inputs(contract,'entry',inputs,root)
    timing={'method':'segmented_estimate','duration_seconds':duration,'language':'en','margin_seconds':.05,
        'rationale':'Synthetic offline timing','overlap_policy':'serial','overlap_rationale':'Synthetic offline timing','segments':[],
        'action_windows':[{'source_pointer':'/shot_contract/shots/0/'+key,'value_sha256':prep.digest(shot[key]),
            'start_seconds':a,'end_seconds':b,'rationale':'Synthetic offline timing'}
            for key,a,b in [('dominant_action',0,duration/2),('completed_end_state',duration/2,duration-.05)]]}
    compiled=prep.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
    write(root/'artifacts/compiled_request-original.json',compiled)
    review={'version':'1.0','review_id':'original','reviewer':'Synthetic offline reviewer','status':'pass','subject_sha256':prep.digest(compiled),
        'evidence_kind':'fixture_only','predicates':[{'name':p,'status':'pass','severity':'critical','evidence':'Synthetic offline review'} for p in sorted(prep.PREDICATES)]}
    write(root/'artifacts/preparation_review-original.json',review)
    prep.validate_preparation(inputs,native,profile)
    if model=='byte-plus-seedance-2-5':
        assert native['body']['params']['aspectRatio']=='9:16'
        assert native['body']['params']['duration']==30
        assert [row['label'] for row in native['body']['params']['visualReferences']]==['Synthetic fixture 0','Synthetic fixture 1']
        # Canonical full-reference 30s compilation reaches the actual one-shot
        # connector envelope under quote-budget authority, without a host call.
        from lib import production_autonomy as pa, openart_mcp_jobs as jobs
        from tests.lib.test_production_autonomy import make_policy, install_existing
        marker=json.loads((root/'project.json').read_text());marker['pipeline_type']='cinematic'
        write(root/'project.json',marker)
        policy=make_policy();policy.update(caps={'max_total_attempts':1,'max_attempts_per_shot':1,'max_repair_attempts':0},
            locked={'cast':{},'dialogue':{},'sources':[],'story_predicates':[],'controls':{}},
            flex={'duration_s':[],'resolution':[],'references':{'droppable_roles':[],'substitutes':[]}},checkpoint_stages=[])
        policy['providers']=[{'id':'openart_mcp','billing':'quoted_credit_budget',
            'uid_sha256':native['account_binding']['uid_sha256'],'project_id':'fixture-project',
            'routes':[{'model':model,'mode':mode}],'max_credit_budget':4000,
            'cost_observation_path':'artifacts/SYNTHETIC-whole-story-cost.json'}]
        quote={'version':'1','transport':'openart_mcp','classification':'agent_recorded_connector_quote_not_backend_ceiling',
            'observed_at':'2026-10-14T00:00:00Z','tool':'openart_model_cost','account_binding':native['account_binding'],
            'native_request_binding':{k:native['body'][k] for k in ('model','mode','params')},
            'arguments':{'model':model,'mode':mode,'params':params},
            'data':{'currency':'credits','balance':6248,'items':[{'model':model,'mode':mode,'config':params,
                'unitCredits':3905,'quantity':1,'totalCredits':3905,'affordable':True,'mediaType':'video'}]}}
        write(root/'artifacts/SYNTHETIC-whole-story-cost.json',quote)
        # The legacy fixture installer uses a global digest. Bind its fixture
        # construction to the actual local-shot digest; production is unpatched.
        policy['planning_mode']='shot_local_v1'
        original_digest=execution.approval_plan_digest
        with monkeypatch.context() as fixture_patch:
            fixture_patch.setattr(execution,'approval_plan_digest',lambda c,shot_id=None:original_digest(c,shot_id or 'entry'))
            policy,_=install_existing(root,policy,templates={'entry':execution.planned_request_template(inputs,project_dir=root)})
        write(root/'production_scopes.json',{'version':'1.0','scopes':[]})
        scope=pa.derive_scope(root,inputs,provider='openart_mcp');inputs['governance']['scope_id']=scope['id']
        jobs.prepare(root,attempt_id='whole-story-original',generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
        envelope=jobs.begin(root,'whole-story-original',authority_fn=execution.prepare_openart_mcp_handoff)
        assert envelope['arguments']==native['body']
        assert envelope['arguments']['params']['visualReferences']==native['body']['params']['visualReferences']
        billing=jobs.frozen_request(root,'whole-story-original')['authority']['billing']
        assert billing['quoted_credits']==3905 and billing['max_credit_budget']==4000
        assert pa.root_attempt_counts(root,policy)['total']==1
        with pytest.raises(ValueError):jobs.begin(root,'whole-story-original',authority_fn=execution.prepare_openart_mcp_handoff)
        return
    else:assert 'aspectRatio' not in native['body']['params']
    if mode=='image2video':assert native['body']['params']['startFrame']['id']==assets['start']['upload_id']
    else:
        assert 'startFrame' not in native['body']['params'] and 'endFrame' not in native['body']['params']
        boarded=copy.deepcopy(contract);boarded['shots'][0].pop('reference_mode');refresh(boarded)
        write(root/'artifacts/shot_contract.json',boarded)
        with pytest.raises(ValueError,match='approved start board'):
            prep.validate_openart_asset_roles(prep.source_packet(root,'entry',provider='openart_mcp',native=native),native)
        write(root/'artifacts/shot_contract.json',contract)
        manifest={'assets':{},'metadata':{'visual_development':{'shot_cards':{'entry':{'pinned_final_frame':{'required':True}}}}}}
        with pytest.raises(ValueError,match='native ending-frame pin'):
            prep._check_required_native_controls(manifest,None,'entry',native=native,project_dir=root)
    changed=copy.deepcopy(inputs);changed['input_assets'][0]['source_sha256']='f'*64
    with pytest.raises(ValueError):prep.prep_builder('openart_mcp')(changed,profile)


def test_generic_action_prefix_preserves_old_original_prompt_matching_without_editing_authored_literals(tmp_path):
    from tests.lib.test_production_request import shootable_project
    root,_=shootable_project(tmp_path)
    packet=prep.build_static_source_packet(root,'entry',provider='openart_cli')
    action=next(row for row in packet['occurrences'] if row['source_pointer'].endswith('/dominant_action'))
    action['value']='A sign says Main action: DO NOT MOVE; '+prep.REFERENCE_MAPPING_HEADER+'Required payoff.'
    canonical=prep._compile_provider_packet(packet,provider='openart_cli',historical=True)
    legacy=canonical['_historical_prompt_variants'][1]
    assert 'Main action (one continuous beat): A sign says Main action: DO NOT MOVE' in legacy
    assert prep.historical_prompt_matches(canonical['prompt'],canonical)
    assert prep.historical_prompt_matches(legacy,canonical)
    assert not prep.historical_prompt_matches(canonical['prompt'].split(prep.REFERENCE_MAPPING_HEADER)[0],canonical)
    assert not prep.historical_prompt_matches(legacy.replace('Main action: DO NOT MOVE','Main action (one continuous beat): DO NOT MOVE'),canonical)
    assert canonical['_historical_compilations'][0]['coverage'] != canonical['_historical_compilations'][1]['coverage']
    legacy_coverage=canonical['_historical_compilations'][1]['coverage']
    assert prep.historical_preparation_matches(legacy,legacy_coverage,canonical)
    assert not prep.historical_preparation_matches(legacy,canonical['coverage'],canonical)
    assert not prep.historical_preparation_matches(canonical['prompt'],legacy_coverage,canonical)


def test_future_real_seedance_dimensions_block_before_native_admission_but_retained_original_remains_readable(source_project,monkeypatch):
    """An explicit real-profile seam checks actual pixel bytes, never a host call."""
    root,assets,_,_=source_project
    profile=copy.deepcopy(mcp.load_profile('byte-plus-seedance-2-5','element2video',require='candidate'))
    profile['source']='real'
    monkeypatch.setattr(mcp,'_check_profile',lambda exact:exact)
    controls={'project_dir':str(root),'model':profile['model'],'mode':profile['mode'],
        'openart_project_id':'fixture-project','prompt':'SYNTHETIC original',
        'native_params':{'duration':30,'resolution':'720p','aspectRatio':'9:16','generateAudio':True,'videoCount':1},
        'input_assets':[{'role':'reference_image',**assets['start']}]}
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(ValueError,match='reference_dimensions_invalid'):
        mcp.prepare_native_request(controls,profile)
    assert {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}==before
    # Construct only the already-retained pre-check request fixture. Current
    # history reconstruction must preserve those original bytes and bindings.
    with monkeypatch.context() as old_preparation:
        old_preparation.setattr(mcp,'validate_image_source_dimensions',lambda *a,**kw:None)
        original=mcp.prepare_native_request(controls,profile)
    assert mcp.validate_native_request(original,profile)==original


@pytest.mark.parametrize('provider,public,native,extra,accepted',[
    ('openart_mcp',30,30,{},True),('openart_cli',30,30,{},False),('grok_cli',30,30,{},False),
    (None,30,30,{},False),('openart_mcp',30,29,{},False),('openart_mcp',True,True,{},False),
    ('openart_mcp',30,30,{'videoDuration':30},False),('openart_mcp',30,30,{'project_missing':True},False)])
def test_legacy_duplicate_duration_exception_requires_exact_mcp_context(provider,public,native,extra,accepted):
    inputs={'model':'byte-plus-seedance-2-5','mode':'element2video','openart_project_id':'fixture-project',
        'duration':public,'native_params':{'duration':native,**{k:v for k,v in extra.items() if k!='project_missing'}}}
    if extra.get('project_missing'):inputs.pop('openart_project_id')
    if accepted:assert execution.explicit_motion_duration(inputs,provider=provider)==30
    else:
        with pytest.raises(ValueError):execution.explicit_motion_duration(inputs,provider=provider)


def test_compact_factory_mixed_supplied_identity_and_generated_reference_reaches_one_exact_30s_envelope(neutral_package, observations, monkeypatch):
    """Real public package/template/draft/complete seams, entirely synthetic media."""
    from lib import production_autonomy as pa, production_boards as boards, production_images as images, openart_mcp_jobs as jobs
    from tests.lib.test_production_package_authoring import packages
    from tests.lib.test_production_factory import creator_fixture, retain_factory_board, file_bytes
    from tests.lib.test_production_autonomy import make_policy
    from tests.lib.test_production_images import PNG
    root,base,bind=neutral_package
    account=mcp.load_account();account['data']['credits']=6248;mcp.retain_observation('account',account)
    monkeypatch.delenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT')
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    package,_=packages(base);package['shots']=package['shots'][:1]
    package.update(prompt_compiler='shootable_v1',cast_names={'tung':'Tung','pickle':'Pickle'})
    package['compact']['payoff_asset_id']='R1'
    refs=[]
    for aid in ('tung','pickle'):
        path=root/'assets/images'/('01-'+aid+'-character.png');path.write_bytes(PNG+aid.encode())
        ref=copy.deepcopy(base['boards']['references'][0])
        ref.update(id=aid,role='identity_reference',path=str(path.relative_to(root)),
            sha256=prep.file_sha256(path),cast_ids=[aid])
        ref['applicability']['roles']=['identity_reference','reference_image']
        ref['review']['subject_sha256']=ref['sha256']
        refs.append(ref)
    package['compact']['references']=refs
    shot=package['shots'][0];shot.update(duration_seconds=30,cast=['tung','pickle'],
        action='0-4s entrance; 4-8s hard cut; 8-12s hard cut; 12-16s hard cut; 16-20s hard cut; 20-25s hard cut; 25-30s payoff.')
    shot['motion'].update(reference_mode='reference_guided');shot['motion'].pop('frame_input')
    shot['boards'][0].update(slot_id='R1',role='payoff_board',reference_ids=['tung','pickle'],cast_ids=['tung','pickle'])
    shot['roles']=[{'asset_id':aid,'role':'reference_image'} for aid in ('tung','pickle','R1')]
    settings={'preferred_provider':'openart_mcp','allowed_providers':['openart_mcp'],
        'operation':'reference_to_video','model':'byte-plus-seedance-2-5','mode':'element2video',
        'native_params':{'duration':30,'resolution':'720p','aspectRatio':'9:16','generateAudio':True,'videoCount':1}}
    templates=prep.build_video_request_templates(package,settings)
    assert 'duration' not in templates['s1']
    # Retain a genuine pre-fix compact template spelling; the native body still
    # carries only one duration. This exercises original frozen compatibility.
    templates['s1']['duration']=30
    prep.prepare_package(root,bind(package))
    marker=json.loads((root/'project.json').read_text());marker.update(identity_id='fixture-identity',pipeline_type='cinematic');write(root/'project.json',marker)
    contract=execution.load_shot_contract(root);projection=pa.current_projection(root,'s1')
    baseline={'projection':projection,'planned_request_template':execution.planned_request_template(templates['s1'],project_dir=root),
        'script_snapshot':json.loads((root/'artifacts/script.json').read_text()),'dialogue_coverage':pa.dialogue_occurrences(projection),
        'approval_plan_sha256':execution.approval_plan_digest(contract,'s1')}
    spec={'id':'openart_mcp','billing':'quoted_credit_budget','uid_sha256':mcp.account_summary()['uid_sha256'],
        'project_id':'fixture-project','routes':[{'model':settings['model'],'mode':settings['mode']}],
        'max_credit_budget':4000,'cost_observation_path':'artifacts/SYNTHETIC-compact-cost.json'}
    policy=make_policy(project_id=contract['project_id'],story_revision='r1',providers=[spec],
        caps={'max_total_attempts':1,'max_attempts_per_shot':1,'max_repair_attempts':0},checkpoint_stages=[],
        locked={'cast':{},'dialogue':{},'sources':[],'story_predicates':[],'controls':{}},
        flex={'duration_s':[30],'resolution':['720p'],'references':{'droppable_roles':[],'substitutes':[]}})
    setup={'version':'1.0','identity_id':'fixture-identity','enabled':True,'providers':[spec],
        'provider_accounts':{'openart_mcp':'fixture-mcp'},'caps':policy['caps'],'checkpoint_stages':[],
        'flex':policy['flex'],'approved_source_sha256':[r['sha256'] for r in refs],'required_permissions':[],
        'native_images':{'account':'fixture-native','image_allowance':1,'allowed_image_routes':['native_imagegen']},
        'source_transfer':[{'provider':provider,'account':account,'roles':['identity_reference','payoff_board','reference_image'],
            'generated_board_sources':True,'max_files':3,'max_file_bytes':10000,'max_batch_bytes':30000,'max_batches':1}
            for provider,account in [('openart_mcp','fixture-mcp'),('native_imagegen','fixture-native')]]}
    standing=creator_fixture(root,'compact-standing',setup)
    command=creator_fixture(root,'compact-command',{'version':'1.0','identity_id':'fixture-identity','project_id':contract['project_id'],
        'story_revision':'r1','command':'make_video','standing_setup_sha256':standing['sha256']})
    pa.activate_standing_run(root,policy=policy,baselines={'s1':baseline},standing_setup=standing,run_command=command,identity_id='fixture-identity')
    images.activate_standing_image_envelope(root);retain_factory_board(root,'R1')
    pack=boards.load_board_pack(root);assert pack['board_bindings']['tung']['origin']=='reuse'
    assert pack['board_bindings']['R1']['origin']=='canonical_image_import'
    contract=execution.load_shot_contract(root);assets={a['id']:a for a in contract['assets']}
    files=[{'path':str(root/assets[aid]['path']),'sha256':assets[aid]['sha256'],'type':'image'} for aid in ('tung','pickle','R1')]
    evidence=root/'notes/SYNTHETIC-upload.txt';evidence.write_text('SYNTHETIC offline upload evidence')
    write(root/'artifacts/openart_mcp_upload_authorization-compact.json',{'provider':'openart_mcp','status':'approved','purpose':'source_transfer_only',
        'project_id':contract['project_id'],'story_revision':'r1','account_uid_sha256':spec['uid_sha256'],'openart_project_id':'fixture-project',
        'files':files,'no_enforceable_credit_ceiling':True,'delayed_charges_unknown':True,'max_upload_batches':1,
        'approved_by':'SYNTHETIC fixture','evidence':{'path':str(evidence.relative_to(root)),'sha256':prep.file_sha256(evidence)}})
    # Real-account admission measures source pixels before snapshot/upload. The
    # synthetic tiny images are intentionally unsuitable for live Seedance.
    account_summary=mcp.account_summary
    with jobs._lock(root):
        pass
    before=file_bytes(root)
    with monkeypatch.context() as real_account:
        real_account.setattr(mcp,'account_summary',lambda:{**account_summary(),'classification':'agent_recorded_real_connector_observation'})
        with pytest.raises(ValueError,match='reference_dimensions_invalid'):
            jobs.prepare_upload(root,files=[f['path'] for f in files],billing_declaration={'upload_authorization_id':'compact'},openart_project_id='fixture-project')
    assert file_bytes(root)==before
    upload=jobs.prepare_upload(root,files=[f['path'] for f in files],billing_declaration={'upload_authorization_id':'compact'},openart_project_id='fixture-project')
    jobs.record_upload_receipt(root,upload_id=upload['upload_id'],result={'projectId':'fixture-project','results':[
        {'fileId':'SYNTHETIC-'+str(i),'status':'SUCCESS','visualReference':{'id':'SYNTHETIC-'+str(i),'type':'image',
        'url':'https://fixture.example.test/'+str(i),'label':str(i)+'.png',
        'imageUrl':'https://fixture.example.test/'+str(i),'thumbnailUrl':'https://fixture.example.test/'+str(i),
        'metadata':{'fixture_only':True}}} for i in range(3)]})
    action=execution.next_production_action(root);assert action['kind']=='prepare_motion'
    draft=execution.draft_motion_preparation(root,action);inputs=draft['inputs']
    profile=mcp.load_profile(settings['model'],settings['mode'],require='supported');native=prep.prep_builder('openart_mcp')(inputs,profile)
    assert native['body']['params']['duration']==30 and [r['label'] for r in native['body']['params']['visualReferences']]==['0.png','1.png','2.png']
    assert '"source_ids": ["tung"]' in inputs['prompt'] and '"source_ids": ["R1"]' in inputs['prompt']
    timing={'method':'segmented_estimate','duration_seconds':30,'language':'en','margin_seconds':.05,'rationale':'SYNTHETIC seven-cut estimate',
        'overlap_policy':'serial','overlap_rationale':'SYNTHETIC serial windows','segments':[],
        'action_windows':[{'source_pointer':'/shot_contract/shots/0/'+key,'start_seconds':a,'end_seconds':b,'rationale':'SYNTHETIC window'}
        for key,a,b in [('dominant_action',0,25),('completed_end_state',25,29.95)]]}
    review={'version':'1.0','reviewer':'SYNTHETIC fixture','status':'pass','evidence_kind':'fixture_only',
        'predicates':[{'name':p,'status':'pass','severity':'critical','evidence':'SYNTHETIC'} for p in sorted(prep.PREDICATES)]}
    prep.prepare_compiled_request(inputs,native,profile,coverage=draft['coverage'],timing=timing,review=review,persist=True)
    write(root/spec['cost_observation_path'],{'version':'1','transport':'openart_mcp','classification':'agent_recorded_connector_quote_not_backend_ceiling',
        'observed_at':'2026-10-14T00:00:00Z','tool':'openart_model_cost','account_binding':native['account_binding'],
        'arguments':{'model':settings['model'],'mode':settings['mode'],'params':settings['native_params']},
        'native_request_binding':{k:native['body'][k] for k in ('model','mode','params')},
        'data':{'currency':'credits','balance':6248,'items':[{'model':settings['model'],'mode':settings['mode'],'config':settings['native_params'],
            'unitCredits':3905,'quantity':1,'totalCredits':3905,'affordable':True,'mediaType':'video'}]}})
    before=file_bytes(root);pa._scope_material(root,inputs,'openart_mcp');assert file_bytes(root)==before
    result=execution.complete_action(root,action,outcome={'inputs':inputs,'provider':'openart_mcp'})
    assert result['status']=='dispatched'
    handed=execution.advance(root)
    assert handed['status']=='host_action' and handed['host_envelope']['arguments']==native['body']
    assert pa.root_attempt_counts(root,policy)['total']==1
    assert images.image_usage(root)['total']==1
    with pytest.raises(ValueError):
        execution.complete_action(root,action,outcome={'inputs':inputs,'provider':'openart_mcp'})
    assert pa.root_attempt_counts(root,policy)['total']==1
