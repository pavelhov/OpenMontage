"""All captured native forms offline; receipts and account are clearly synthetic."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from lib import openart_mcp as mcp

@pytest.fixture
def observations(tmp_path, monkeypatch):
    discovery=tmp_path/'discovery'; discovery.mkdir(mode=0o700)
    schema=(Path(__file__).parents[1]/'fixtures/openart/mcp_native_forms.json').read_bytes()
    account=json.dumps({'classification':'synthetic_fixture','data':{'user':{'uid':'fixture-only'},'credits':100}}).encode()
    for kind,raw in [('schema',schema),('account',account)]:
        p=discovery/f'2026-10-07-{kind}-observation.json';p.write_bytes(raw);p.chmod(0o600)
        monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_'+kind.upper(),hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR',str(discovery))
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(tmp_path/'state'))
    return discovery


def minimum(schema):
    """Generate one bounded fixture value from the complete schema, no runtime defaults."""
    for key in ('oneOf','anyOf'):
        if key in schema: return minimum(schema[key][0])
    if 'const' in schema:return schema['const']
    if 'enum' in schema:return schema['enum'][0]
    typ=schema.get('type')
    if typ=='object':return {k:minimum(schema['properties'][k]) for k in schema.get('required',[])}
    if typ=='array':return [minimum(schema['items']) for _ in range(max(1,schema.get('minItems',0)))]
    if typ=='integer':return max(1,schema.get('minimum',1))
    if typ=='number':return max(1,schema.get('minimum',1))
    if typ=='boolean':return False
    if typ=='string':return 'https://fixture.example.test/image.png' if schema.get('format')=='uri' else 'fixture'
    raise AssertionError(schema)


def test_all_45_intact_forms_and_17_models(observations):
    rows=mcp.video_catalog();assert len(rows)==45;assert len({r['model'] for r in rows})==17
    for row in rows:
        p=mcp.load_profile(row['model'],row['mode'],require='candidate')
        assert p['production_ready'] is False
        assert p['form']['jsonSchema']==mcp.form(row['model'],row['mode'])['jsonSchema']
        value=minimum(p['form']['jsonSchema'])
        assert mcp._validator(p['form']['jsonSchema']).is_valid(value)
        assert set(p['native_capabilities']['params'])==set(mcp._properties(p['form']['jsonSchema']))


def test_every_reference_free_builder(observations):
    for row in mcp.video_catalog():
        if row['mode'] not in ('text2video','generate-shot-video'):continue
        p=mcp.load_profile(row['model'],row['mode'],require='candidate')
        params=minimum(p['form']['jsonSchema'])
        n=mcp.prepare_native_request({'model':row['model'],'mode':row['mode'],'native_params':params},p)
        assert n['body']['params']==params
        mcp.validate_native_request(n)
        assert 'argv' not in n and 'cli_version' not in n


def test_alias_conflicts_type_exact_and_no_defaults(observations):
    p=mcp.load_profile('pixverseV6','text2video',require='candidate')
    base={'model':'pixverseV6','mode':'text2video','prompt':'fixture','duration':1}
    for conflict in (True,1.0):
        with pytest.raises(mcp.OpenArtMCPError):mcp.prepare_native_request({**base,'native_params':{'duration':conflict}},p)
    n=mcp.prepare_native_request({**base,'native_params':{'duration':1}},p)
    assert n['body']['params']=={'prompt':'fixture','duration':1}
    n['body']['params']['duration']=2
    with pytest.raises(mcp.OpenArtMCPError):mcp.validate_native_request(n)
    p['form']['jsonSchema']['properties']['resolution']['enum'].append('invented')
    with pytest.raises(mcp.OpenArtMCPError):mcp.prepare_native_request(base,p)


def test_native_missing_controls_roles_unions(observations):
    p=mcp.load_profile('gemini-omni-flash','text2video',require='candidate')
    with pytest.raises(mcp.OpenArtMCPError):mcp.prepare_native_request({'model':p['model'],'mode':p['mode'],'resolution':'720p'},p)
    p=mcp.load_profile('wan3-0','text2video',require='candidate')
    assert not p['native_capabilities']['roles']['reference_audio']['supported']
    n=mcp.prepare_native_request({'model':p['model'],'mode':p['mode'],'prompt':'fixture','native_params':{'audio':True,'duration':-1}},p)
    assert n['body']['params']['audio'] is True
    with pytest.raises(mcp.OpenArtMCPError):mcp.prepare_native_request({'model':p['model'],'mode':p['mode'],'native_params':{'visualReferences':[]}},p)
    p=mcp.load_profile('kling-3-omni','text2video',require='candidate')
    with pytest.raises(mcp.OpenArtMCPError):mcp.prepare_native_request({'model':p['model'],'mode':p['mode'],'prompt':'fixture','native_params':{'multiShot':True}},p)


def test_account_credit_changes_do_not_change_profile(observations,monkeypatch):
    old=mcp.load_profile('pixverseV6','text2video',require='candidate')
    path=observations/'2026-10-07-account-observation.json'; x=json.loads(path.read_text());x['data']['credits']=99
    raw=json.dumps(x).encode();path.write_bytes(raw)
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT',hashlib.sha256(raw).hexdigest())
    new=mcp.load_profile('pixverseV6','text2video',require='candidate');assert new==old
    x['data']['user']['uid']='other-fixture';raw=json.dumps(x).encode();path.write_bytes(raw)
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT',hashlib.sha256(raw).hexdigest())
    with pytest.raises(mcp.OpenArtMCPError):mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','prompt':'fixture'},old)


def test_private_paths_and_refresh_index(observations,monkeypatch):
    account=mcp.load_account();account['data']['credits']=90
    result=mcp.retain_observation('account',account)
    assert result['dispatch_authority'] is False
    monkeypatch.delenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT')
    assert mcp.load_account()['data']['credits']==90
    current=observations/'current-observations.json';current.chmod(0o644)
    with pytest.raises(mcp.OpenArtMCPError):mcp.load_account()


def test_role_projections_and_exact_branches(observations):
    schema=mcp.form('fal-h3-max','image2video')['jsonSchema']['properties']['startFrame']
    raw={'id':'fixture-provider-id','type':'image','url':'https://fixture.example.test/start.png','label':'fixture','metadata':{'anything':'retained only'}}
    projected=mcp._project_reference(raw,schema)
    assert projected=={k:v for k,v in raw.items() if k!='metadata'}
    assert 'metadata' in raw
    h3=mcp.load_profile('fal-h3-max','image2video',require='candidate')
    assert h3['native_capabilities']['roles']['first_frame']['supported']
    assert not h3['native_capabilities']['roles']['reference_audio']['supported']
    assert 'aspectRatio' not in h3['native_capabilities']['params']
    smart=mcp.load_profile('smart-shot','generate-shot-video',require='candidate')
    assert smart['native_capabilities']['roles']['character_reference']['supported']
    assert smart['native_capabilities']['roles']['environment_reference']['supported']
    assert not smart['native_capabilities']['roles']['reference_image']['supported']
    caps=mcp.load_profile('veo3-1','element2video',require='candidate')['native_capabilities']
    assert caps['roles']['reference_image']['supported']
    assert not caps['roles']['reference_audio']['supported']


def test_fixture_refresh_relabel_cannot_become_live(observations,monkeypatch):
    schema=mcp.load_schema();schema['classification']='schema_observation_only_not_dispatch_authority'
    account=mcp.load_account();account.update(classification='agent_recorded_connector_observation_not_dispatch_authority',tool='openart_account_get',transport='openart_mcp')
    mcp.retain_observation('schema',schema);mcp.retain_observation('account',account)
    for kind in ('SCHEMA','ACCOUNT'):monkeypatch.delenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_'+kind)
    assert mcp.load_profile('pixverseV6','text2video',require='candidate')['source']=='fixture'


def test_reference_projection_does_not_forward_upload_metadata_or_auxiliary_urls(observations):
    raw={'id':'fixture','type':'image','url':'https://fixture.example.test/image.png','label':'fixture',
         'metadata':{'sourceUrl':'https://unbound.example.test/other.png'}}
    spec=mcp.form('fal-h3-max','element2video')['jsonSchema']['properties']['visualReferences']['items']
    assert spec['additionalProperties']=={}
    assert mcp._project_reference(raw,spec)=={k:v for k,v in raw.items() if k!='metadata'}
    wan=mcp.form('wan2-7','element2video')['jsonSchema']['properties']['visualReferences']['items']
    contaminated={**raw,'voiceReference':{'url':'https://unbound.example.test/voice.wav'}}
    with pytest.raises(mcp.OpenArtMCPError):mcp._project_reference(contaminated,wan)

@pytest.fixture
def source_project(observations,tmp_path,request):
    from lib import openart_mcp_jobs as jobs
    from tests.lib.test_shot_contract import FIXTURES,refresh
    root=tmp_path/'source-project';root.mkdir();(root/'artifacts').mkdir();(root/'assets').mkdir()
    contract=json.loads((FIXTURES/'valid_shot_contract.json').read_text())
    contract['project_id']=root.name
    for original in contract['assets']:
        path=root/original['path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((FIXTURES/'synthetic-board.svg').read_bytes())
        original['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();original['review']['subject_sha256']=original['sha256']
    rows=[]
    for i,(name,kind,extension) in enumerate([('start','image','svg'),('end','image','svg'),('voice','audio','wav'),('motion','video','mp4')]):
        path=root/'assets'/f'{name}.{extension}'
        path.write_bytes((FIXTURES/'synthetic-board.svg').read_bytes() if kind=='image' else ('Synthetic offline '+kind+' source bytes.').encode())
        row=copy.deepcopy(next((a for a in contract['assets'] if a['id']==name),contract['assets'][-1]));row.update(id=name,path=str(path.relative_to(root)),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        row['review']['subject_sha256']=row['sha256'];rows.append(row)
    contract['assets']=[a for a in contract['assets'] if a['id'] not in {r['id'] for r in rows}]+rows
    contract['shots'][0]['asset_ids']=list(dict.fromkeys(contract['shots'][0]['asset_ids']+[a['id'] for a in rows]))
    refresh(contract)
    marker={'project_id':root.name,'story_revision':contract['story_revision'],'governance':{'mode':'strict','version':'1.0'}}
    (root/'project.json').write_text(json.dumps(marker));(root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    approval=root/'upload-approval.txt';approval.write_text('Synthetic offline explicit approval of unknown upload costs and delayed charges; source transfer only.')
    manifest=[{'path':str(root/a['path']),'sha256':a['sha256'],'type':kind} for a,kind in zip(rows,['image','image','audio','video'])]
    authority={'provider':'openart_mcp','status':'approved','purpose':'source_transfer_only','project_id':root.name,
        'story_revision':contract['story_revision'],'account_uid_sha256':mcp.account_summary()['uid_sha256'],
        'openart_project_id':'fixture-project','files':manifest,'no_enforceable_credit_ceiling':True,
        'delayed_charges_unknown':True,'max_upload_batches':1,'approved_by':'Synthetic fixture approval',
        'evidence':{'path':approval.name,'sha256':hashlib.sha256(approval.read_bytes()).hexdigest()}}
    (root/'artifacts/openart_mcp_upload_authorization-fixture.json').write_text(json.dumps(authority))
    prepared=jobs.prepare_upload(root,files=[r['path'] for r in manifest],billing_declaration={'upload_authorization_id':'fixture'},openart_project_id='fixture-project')
    result={'projectId':'fixture-project','results':[{'fileId':'fixture-file-'+str(i),'status':'SUCCESS',
        'visualReference':{'id':'fixture-ready-'+str(i),'type':row['type'],'url':'https://fixture.example.test/'+str(i),
                           'label':'Synthetic fixture '+str(i),'metadata':{'fixture_only':True}}} for i,row in enumerate(manifest)]}
    for row in result['results']:
        ref=row['visualReference']
        if ref['type']=='image':ref.update(imageUrl=ref['url'],thumbnailUrl=ref['url'])
    result['results'][0]['visualReference'].update(copy.deepcopy(getattr(request,'param',{})))
    recorded=jobs.record_upload_receipt(root,upload_id=prepared['upload_id'],result=result)
    assets={name:{'source_path':row['path'],'upload_id':ref['upload_id']} for name,row,ref in zip(['start','end','voice','motion'],manifest,recorded['references'])}
    return root,assets,prepared,result


def test_all_45_native_builders_use_canonical_ready_source_receipts(source_project):
    root,assets,prepared,result=source_project
    for row in mcp.video_catalog():
        profile=mcp.load_profile(row['model'],row['mode'],require='candidate')
        params=minimum(profile['form']['jsonSchema'])
        for field in {v[0] for v in mcp.ROLES.values()}:params.pop(field,None)
        bindings=[]
        for role,name in [('first_frame','start'),('last_frame','end'),('reference_image','start'),('reference_video','motion'),('reference_audio','voice'),('character_reference','start'),('environment_reference','end')]:
            if profile['native_capabilities']['roles'][role]['supported']:
                bindings.append({'role':role,**assets[name]})
        controls={'model':row['model'],'mode':row['mode'],'project_dir':str(root),'native_params':params,
                  'input_assets':bindings,'openart_project_id':'fixture-project'}
        native=mcp.prepare_native_request(controls,profile)
        mcp.validate_native_request(native,profile)
        assert [b['role'] for b in native['input_assets']]==[b['role'] for b in bindings]
        for bound in native['input_assets']:
            assert bound['source_sha256']==hashlib.sha256(Path(bound['source_path']).read_bytes()).hexdigest()
            assert bound['upload_id'].startswith('fixture-ready-')
            assert 'metadata' not in native['body']['params'].get('startFrame',{})
        if row['mode']=='generate-shot-video':
            assert native['body']['params']['characterReferenceImageUrls']==['https://fixture.example.test/0']
            assert native['body']['params']['environmentReferenceImageUrl']=='https://fixture.example.test/1'


def alias_source_contract(root, *, independent_shot=False):
    """Synthetic payoff/end roles share one file, as an intentional physical alias."""
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    end=next(a for a in contract['assets'] if a['id']=='end')
    payoff=next(a for a in contract['assets'] if a['id']=='payoff')
    payoff['path']=end['path'];payoff['sha256']=end['sha256']
    payoff['review']['subject_sha256']=end['sha256']
    if independent_shot:
        other=copy.deepcopy(end);other.update(id='other-end',path='assets/other-end.svg')
        (root/other['path']).write_bytes((root/end['path']).read_bytes())
        contract['assets'].append(other)
        shot=copy.deepcopy(contract['shots'][0]);shot['id']='independent'
        shot['asset_ids']=['other-end' if aid=='end' else aid for aid in shot['asset_ids']]
        contract['shots'].insert(0,shot)
    return contract,end,payoff


def prepare_alias_source_upload(root, end):
    """Fresh synthetic one-file approval; the original four-file batch is preserved."""
    from lib import openart_mcp_jobs as jobs
    path=str((root/end['path']).resolve())
    authority=json.loads((root/'artifacts/openart_mcp_upload_authorization-fixture.json').read_text())
    authority['files']=[{'path':path,'sha256':end['sha256'],'type':'image'}]
    (root/'artifacts/openart_mcp_upload_authorization-alias.json').write_text(json.dumps(authority))
    return jobs.prepare_upload(root,files=[path],billing_declaration={'upload_authorization_id':'alias'},
                               openart_project_id='fixture-project')


@pytest.mark.parametrize('reverse_assets',[False,True])
def test_source_transfer_accepts_current_same_path_payoff_and_end_aliases(source_project,reverse_assets):
    from lib import openart_mcp_jobs as jobs
    from tests.lib.test_shot_contract import refresh
    root,assets,original,result=source_project
    contract,end,_=alias_source_contract(root)
    if reverse_assets:contract['assets'].reverse()
    refresh(contract);(root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    prepared=prepare_alias_source_upload(root,end)
    frozen=json.loads((root/'openart_mcp/uploads'/f"{prepared['upload_id']}.json").read_text())['frozen']
    assert len(frozen['manifest'])==len(frozen['source_snapshots'])==len(prepared['arguments']['files'])==1
    assert frozen['manifest'][0]['sha256']==end['sha256']
    row=copy.deepcopy(result['results'][1]);row['fileId']='synthetic-alias-file'
    row['visualReference']['id']='synthetic-alias-ready'
    recorded=jobs.record_upload_receipt(root,upload_id=prepared['upload_id'],
                                      result={'projectId':'fixture-project','results':[row]})
    resolved=jobs.resolve_input_asset(root,{'source_path':str(root/end['path']),
        'source_sha256':end['sha256'],'upload_id':recorded['references'][0]['upload_id']},
        account_binding=mcp.account_summary(),openart_project_id='fixture-project')
    assert resolved['source_sha256']==end['sha256'] and resolved['upload_id']=='synthetic-alias-ready'
    assert json.loads((root/'openart_mcp/uploads'/f"{original['upload_id']}.json").read_text())['status']=='recorded'
    with pytest.raises(mcp.OpenArtMCPError,match='approved upload batch already consumed'):
        prepare_alias_source_upload(root,end)


@pytest.mark.parametrize('change',[
    'failed_alias','stale_alias','wrong_alias_subject','missing_alias_predicate','invalid_alias_role',
    'duplicate_alias_id','orphan_alias','unreviewed_orphan_alias','wrong_alias_sha',
    'unreviewed_same_bytes_other_path','other_project_alias','stale_story','changed_bytes',
    'failed_project_review','failed_shot_review',
])
def test_source_transfer_aliases_cannot_borrow_unrelated_review_authority(source_project,change):
    from lib import openart_mcp_jobs as jobs
    from lib.shot_contract import validate_shot_contract
    from tests.lib.test_shot_contract import refresh
    root,_,_,_=source_project
    contract,end,payoff=alias_source_contract(root,independent_shot=True)
    if change=='failed_alias':end['review']['status']='fail'
    elif change=='stale_alias':end['review']['story_revision']='older-story'
    elif change=='wrong_alias_subject':end['review']['subject_sha256']='f'*64
    elif change=='missing_alias_predicate':end['review']['predicates'].pop()
    elif change=='invalid_alias_role':end['role']='invented-role'
    elif change=='duplicate_alias_id':contract['assets'].append(copy.deepcopy(end))
    elif change in ('orphan_alias','unreviewed_orphan_alias'):
        orphan=copy.deepcopy(end);orphan.update(id='unused-alias',role='reference_image')
        if change=='unreviewed_orphan_alias':orphan['review']['status']='fail'
        contract['assets'].append(orphan)
    elif change=='wrong_alias_sha':end['sha256']='f'*64;end['review']['subject_sha256']=end['sha256']
    elif change=='unreviewed_same_bytes_other_path':payoff['path']='assets/payoff.svg';end['review']['status']='fail'
    elif change=='other_project_alias':payoff['path']=str(root.parent/'foreign.svg');Path(payoff['path']).write_bytes((root/end['path']).read_bytes())
    elif change=='stale_story':contract['story_revision']='older-story'
    elif change=='changed_bytes':(root/end['path']).write_bytes(b'Changed synthetic source')
    elif change=='failed_project_review':contract['project_review']['status']='fail'
    elif change=='failed_shot_review':contract['shots'][-1]['review']['status']='fail'
    refresh(contract);(root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    if change in ('failed_alias','stale_alias','wrong_alias_subject','missing_alias_predicate',
                  'orphan_alias','unreviewed_orphan_alias','wrong_alias_sha',
                  'unreviewed_same_bytes_other_path','failed_shot_review'):
        assert validate_shot_contract(contract,project_dir=root,shot_id='independent')['eligible']
    original_batches=set((root/'openart_mcp/uploads').glob('*.json'))
    with pytest.raises(mcp.OpenArtMCPError):prepare_alias_source_upload(root,end)
    assert set((root/'openart_mcp/uploads').glob('*.json'))==original_batches


@pytest.mark.parametrize('change',[None,'selected_attempt','upstream_review'])
def test_source_transfer_alias_eligibility_uses_current_selected_upstream(source_project,change):
    from lib import openart_mcp_jobs as jobs
    from lib import production_request as preparation
    from tests.lib.test_shot_contract import dependency,refresh
    root,assets,_,_=source_project
    contract,end,_=alias_source_contract(root)
    selected=dependency((contract,root))
    # Only the dependent shot consumes this end alias; the earlier shot still
    # reviews the global payoff alias but cannot approve the dependent role.
    other=copy.deepcopy(end);other.update(id='other-end',path='assets/other-end.svg')
    (root/other['path']).write_bytes((root/end['path']).read_bytes());contract['assets'].append(other)
    contract['shots'][0]['asset_ids']=['other-end' if aid=='end' else aid for aid in contract['shots'][0]['asset_ids']]
    refresh(contract);(root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    if change=='selected_attempt':selected['entry']['attempt_id']='unapproved-replacement'
    elif change=='upstream_review':selected['entry']['review']['status']='fail'
    (root/'artifacts/selected_attempts.json').write_text(json.dumps(selected))
    if change:
        with pytest.raises(mcp.OpenArtMCPError):prepare_alias_source_upload(root,end)
        profile=mcp.load_profile('fal-h3-max','image2video',require='candidate')
        controls={'project_dir':str(root),'model':profile['model'],'mode':profile['mode'],
            'prompt':'Synthetic current source check','openart_project_id':'fixture-project',
            'native_params':{'duration':8,'resolution':'768P'},
            'input_assets':[{'role':'first_frame',**assets['start']},{'role':'last_frame',**assets['end']}]}
        native=mcp.prepare_native_request(controls,profile)
        with pytest.raises(ValueError,match='selected attempt changed|selected review changed'):
            preparation.source_packet(root,'interior',provider='openart_mcp',native=native)
        with pytest.raises(mcp.OpenArtMCPError,match='unsupported public controls'):
            mcp.prepare_native_request({**controls,'original_sources':True},profile)
    else:
        assert prepare_alias_source_upload(root,end)['generation_enabled'] is False


def test_receipted_source_for_another_shot_cannot_pass_current_preparation(source_project,monkeypatch):
    from lib import openart_mcp_jobs as jobs, production_execution as execution, production_request as preparation
    from lib.shot_contract import validate_shot_contract
    from tests.lib.test_shot_contract import refresh
    from tests.lib.test_production_request import write
    root,assets,_,_=source_project
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    target_start=copy.deepcopy(next(a for a in contract['assets'] if a['id']=='start'))
    target_start.update(id='target-start',path='assets/target-start.svg')
    (root/target_start['path']).write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><text>Synthetic different shot B board</text></svg>')
    target_start['sha256']=hashlib.sha256((root/target_start['path']).read_bytes()).hexdigest()
    target_start['review']['subject_sha256']=target_start['sha256'];contract['assets'].append(target_start)
    target=copy.deepcopy(contract['shots'][0]);target['id']='target'
    target['asset_ids']=['target-start' if aid=='start' else aid for aid in target['asset_ids']]
    contract['shots'].append(target);refresh(contract);write(root/'artifacts/shot_contract.json',contract)
    assert validate_shot_contract(contract,project_dir=root,shot_id='target')['eligible']
    write(root/'artifacts/scene_plan.json',{'version':'1.0','scenes':[
        {'id':sid,'type':'generated','description':'Synthetic source role test','start_seconds':i*8,
         'end_seconds':(i+1)*8,'script_section_id':sid} for i,sid in enumerate(('entry','target'))]})
    write(root/'artifacts/script.json',{'version':'1.0','title':'Synthetic source role test','total_duration_seconds':16,
        'sections':[{'id':sid,'text':'Synthetic current reviewed action.','start_seconds':i*8,'end_seconds':(i+1)*8}
                    for i,sid in enumerate(('entry','target'))]})
    authored=preparation.compile_provider_prompt(root,'target',provider='openart_mcp',model='fal-h3-max')
    inputs={'project_dir':str(root),'model':'fal-h3-max','mode':'image2video','operation':'image_to_video',
        'prompt':authored['prompt'],'native_params':{'duration':8,'resolution':'768P'},
        'openart_project_id':'fixture-project','governance':{'shot_id':'target','scope_id':'target'},
        'input_assets':[{'role':'first_frame',**assets['start']}],'output_path':str(root/'assets/target.mp4'),
        'compiled_request_id':'target','preparation_review_id':'target','unknown_cost_authorization_id':'target'}
    profile=mcp.load_profile(inputs['model'],inputs['mode'],require='candidate')
    native=preparation.prep_builder('openart_mcp')(inputs,profile)
    assert native['input_assets'][0]['source_sha256']!=target_start['sha256']
    timing={'method':'segmented_estimate','duration_seconds':8,'language':'en','margin_seconds':.05,
        'rationale':'Synthetic offline timing','overlap_policy':'serial','overlap_rationale':'Synthetic offline timing','segments':[],
        'action_windows':[{'source_pointer':'/shot_contract/shots/1/'+key,'value_sha256':preparation.digest(target[key]),
            'start_seconds':a,'end_seconds':b,'rationale':'Synthetic offline timing'}
            for key,a,b in [('dominant_action',0,4),('completed_end_state',4,7.95)]]}
    with pytest.raises(ValueError,match='approved start board'):
        preparation.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
    # Even a synthetic exact target-shot request approval cannot turn shot A's
    # retained upload into shot B's required current source at preparation.
    scope={'id':'target','provider':'openart_mcp','status':'approved','approved_by':'Synthetic offline creator',
        'project_id':contract['project_id'],'story_revision':contract['story_revision'],'phase':'first_pass',
        'evidence':{'path':'upload-approval.txt','sha256':hashlib.sha256((root/'upload-approval.txt').read_bytes()).hexdigest()},
        'requests':{'target':execution.planned_request_digest(inputs,project_dir=root)},'attempts_per_shot':{'target':1},
        'approval_plan_sha256':execution.approval_plan_digest(contract)}
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    with pytest.raises(ValueError,match='start_frame|approved start board'):
        jobs.prepare(root,attempt_id='invalid-target',generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
    assert not (root/'openart_mcp/attempts/invalid-target/state.json').exists()


@pytest.mark.parametrize('change',['caller_sha','receipt_id','project','source_bytes','source_review','account','receipt_metadata','receipt_reseal','snapshot','upload_approval'])
def test_canonical_ready_source_binding_tamper(source_project,observations,monkeypatch,change):
    from lib import openart_mcp_jobs as jobs
    root,assets,prepared,result=source_project
    profile=mcp.load_profile('fal-h3-max','image2video',require='candidate')
    controls={'project_dir':str(root),'model':profile['model'],'mode':profile['mode'],'prompt':'Synthetic fixture',
        'input_assets':[{'role':'first_frame',**assets['start']}],'openart_project_id':'fixture-project'}
    native=mcp.prepare_native_request(controls,profile)
    if change=='caller_sha':native['controls']['input_assets'][0]['source_sha256']='f'*64
    elif change=='receipt_id':native['controls']['input_assets'][0]['upload_id']='foreign-reference'
    elif change=='project':native['controls']['openart_project_id']='foreign-project'
    elif change=='source_bytes':Path(assets['start']['source_path']).write_bytes(b'changed')
    elif change=='source_review':
        path=root/'artifacts/shot_contract.json';contract=json.loads(path.read_text());next(a for a in contract['assets'] if a['id']=='start')['review']['status']='fail';path.write_text(json.dumps(contract))
    elif change=='account':
        path=observations/'2026-10-07-account-observation.json';x=json.loads(path.read_text());x['data']['user']['uid']='other-fixture'
        raw=json.dumps(x).encode();path.write_bytes(raw);monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_ACCOUNT',hashlib.sha256(raw).hexdigest())
    elif change=='snapshot':Path(prepared['arguments']['files'][0]).write_bytes(b'changed snapshot')
    elif change=='upload_approval':(root/'upload-approval.txt').write_text('changed approval')
    else:
        store=Path(__import__('os').environ['OPENMONTAGE_OPENART_STATE_DIR'])/'mcp-uploads'
        receipt_path=next(p for p in store.glob('*.json') if json.loads(p.read_text())['visual_reference']['id']==assets['start']['upload_id']);value=json.loads(receipt_path.read_text());value['visual_reference']['url']='https://foreign.example.test/altered'
        if change=='receipt_reseal':
            value['raw_result']['visualReference']=copy.deepcopy(value['visual_reference'])
            value['receipt_sha256']=mcp.digest({k:v for k,v in value.items() if k!='receipt_sha256'})
        receipt_path.write_text(json.dumps(value))
    with pytest.raises((mcp.OpenArtMCPError,ValueError)):
        mcp.validate_native_request(native,profile)


@pytest.mark.parametrize('source_project',[{'voiceReference':{'url':'https://unbound.example.test/voice.wav'}}],indirect=True)
def test_actual_upload_receipt_auxiliary_voice_fails_native_binding(source_project):
    root,assets,prepared,result=source_project
    profile=mcp.load_profile('wan2-7','element2video',require='candidate')
    with pytest.raises(mcp.OpenArtMCPError):
        mcp.prepare_native_request({'model':profile['model'],'mode':profile['mode'],'prompt':'fixture',
            'project_dir':str(root),'openart_project_id':'fixture-project',
            'input_assets':[{'role':'reference_image',**assets['start']}]},profile)


def test_video_count_must_be_exactly_one_for_every_purpose(observations):
    """Collection accepts one original video, so dispatch never requests more."""
    rows=[r for r in mcp.video_catalog() if r['mode'] in ('text2video','generate-shot-video')
          and 'videoCount' in mcp._properties(mcp.form(r['model'],r['mode'])['jsonSchema'])]
    assert rows
    for row in rows:
        p=mcp.load_profile(row['model'],row['mode'],require='candidate')
        params=minimum(p['form']['jsonSchema']); params.pop('videoCount',None)
        base={'model':row['model'],'mode':row['mode']}
        n=mcp.prepare_native_request({**base,'native_params':params},p)
        assert 'videoCount' not in n['body']['params']
        n=mcp.prepare_native_request({**base,'native_params':{**params,'videoCount':1}},p)
        assert n['body']['params']['videoCount']==1
        for bad in (2,True,1.0):
            with pytest.raises(mcp.OpenArtMCPError) as exc:
                mcp.prepare_native_request({**base,'native_params':{**params,'videoCount':bad}},p)
            assert exc.value.kind in ('output_count_unsupported','control_invalid')
        with pytest.raises(mcp.OpenArtMCPError) as exc:
            mcp.prepare_native_request({**base,'native_params':{**params,'videoCount':2}},p)
        assert exc.value.kind=='output_count_unsupported'
    tampered=mcp.prepare_native_request({**base,'native_params':{**params,'videoCount':1}},p)
    tampered['controls']['native_params']['videoCount']=2
    with pytest.raises(mcp.OpenArtMCPError):mcp.validate_native_request(tampered)


def test_supported_route_is_ready_without_prior_result_and_drift_fails(observations,monkeypatch):
    """Native catalog/form/account readiness needs no generated result; evidence stays optional."""
    with pytest.raises(mcp.OpenArtMCPError) as exc:
        mcp.load_profile('pixverseV6','text2video',require='supported')
    assert exc.value.kind=='profile_not_ready'  # fixtures never become production by default
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    p=mcp.load_profile('pixverseV6','text2video',require='supported')
    assert p['production_ready'] is True and p['profile_status']=='supported'
    assert p['readiness']['empirical_result']=={'status':'not_tested','qualification_sha256':None}
    assert mcp.load_profile('pixverseV6','text2video',require='qualified')==p  # legacy alias, same view
    row=mcp.model_catalog()['pixverseV6']['modes']['text2video']
    assert row['production_ready'] is True and row['empirical_result_status']=='not_tested'
    params=minimum(p['form']['jsonSchema'])
    n=mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':params},p)
    mcp.validate_native_request(n)
    with pytest.raises(mcp.OpenArtMCPError):  # invalid native params fail before dispatch
        mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':{**params,'notANativeField':1}},p)
    with pytest.raises(mcp.OpenArtMCPError):  # forged readiness view cannot pass
        mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':params},{**p,'form_sha256':'0'*64})
    # Schema drift: the retained form changes, so the earlier ready profile is rejected.
    path=observations/'2026-10-07-schema-observation.json';schema=json.loads(path.read_text())
    text=json.dumps(schema);assert 'pixverseV6' in text
    def drift(node):
        if isinstance(node,dict):
            if node.get('model')=='pixverseV6' and node.get('mode')=='text2video' and isinstance(node.get('jsonSchema'),dict):
                node['jsonSchema'].setdefault('properties',{})['driftField']={'type':'string'};return True
            return any(drift(v) for v in node.values())
        if isinstance(node,list):return any(drift(v) for v in node)
        return False
    if drift(schema):
        raw=json.dumps(schema).encode();path.write_bytes(raw)
        monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_SCHEMA',hashlib.sha256(raw).hexdigest())
        with pytest.raises(mcp.OpenArtMCPError):
            mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':params},p)


def test_retained_form_outside_current_catalog_is_not_supported(observations,monkeypatch):
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    path=observations/'2026-10-07-schema-observation.json';schema=json.loads(path.read_text())
    for model in schema['catalog']['data']['models']:
        if model['id']=='pixverseV6':
            model['modes']['video']=[m for m in model['modes']['video'] if m['mode']!='text2video']
    raw=json.dumps(schema).encode();path.write_bytes(raw)
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_SCHEMA',hashlib.sha256(raw).hexdigest())
    assert mcp.form('pixverseV6','text2video')  # form still retained and valid
    with pytest.raises(mcp.OpenArtMCPError) as exc:
        mcp.load_profile('pixverseV6','text2video',require='supported')
    assert exc.value.kind=='profile_not_ready'


def test_invalid_optional_evidence_is_reported_and_never_blocks(observations,monkeypatch):
    from lib import openart_mcp_jobs as jobs
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    def stale(*_):raise jobs.OpenArtMCPError('qualification_invalid','stale') if hasattr(jobs,'OpenArtMCPError') else mcp.OpenArtMCPError('qualification_invalid','stale')
    monkeypatch.setattr(jobs,'qualified_profile',stale)
    p=mcp.load_profile('pixverseV6','text2video',require='supported')
    assert p['production_ready'] is True
    assert p['readiness']['empirical_result']['status']=='invalid'
    assert p['readiness']['empirical_result']['reason']=='qualification_invalid'
    row=mcp.model_catalog()['pixverseV6']['modes']['text2video']
    assert row['production_ready'] is True and row['empirical_result_status']=='invalid'
    # The supported view carrying invalid optional evidence still prepares a real native request.
    params=minimum(p['form']['jsonSchema'])
    n=mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':params},p)
    mcp.validate_native_request(n)
    # The plain supported (not_tested) view is still accepted while evidence is invalid.
    plain=mcp._ready_view(mcp.load_profile('pixverseV6','text2video',require='candidate'))
    mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':params},plain)
    # A forged artifact claiming qualified evidence must still validate its own legacy proof.
    forged={**p,'status':'qualified','profile_status':'qualified',
            'readiness':{**p['readiness'],'empirical_result':{'status':'result_verified','qualification_sha256':'a'*64}}}
    with pytest.raises(mcp.OpenArtMCPError) as exc:
        mcp.prepare_native_request({'model':'pixverseV6','mode':'text2video','native_params':params},forged)
    assert exc.value.kind=='qualification_invalid'
    # Evidence never grants readiness: a valid-looking qualified view on a non-ready fixture stays blocked.
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',False)
    monkeypatch.setattr(jobs,'qualified_profile',lambda m,mo,c:{**c,'status':'qualified','production_ready':True,'evidence_kind':'fixture_only','qualification_sha256':'f'*64})
    with pytest.raises(mcp.OpenArtMCPError):mcp.load_profile('pixverseV6','text2video',require='supported')
