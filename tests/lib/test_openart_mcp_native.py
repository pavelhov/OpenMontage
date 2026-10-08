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
