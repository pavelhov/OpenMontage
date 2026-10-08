"""Actual ready receipts + reviewed boards + full native form + source compilation."""
import copy
import json
from pathlib import Path
import pytest
from lib import openart_mcp as mcp, production_request as prep, production_execution as execution
from tests.lib.test_openart_mcp_native import observations, source_project
from tests.lib.test_shot_contract import refresh
from tests.lib.test_production_request import write


@pytest.mark.parametrize('model,mode,reference_mode,roles',[
    ('fal-h3-max-turbo','image2video',None,[('first_frame','start')]),
    ('minimax-h3','element2video','reference_guided',[('reference_image','start')]),
    ('smart-shot','generate-shot-video','reference_guided',[('character_reference','start'),('environment_reference','end')]),
])
def test_exact_native_mode_composes_reviewed_source_without_invented_delivery_flags(source_project,model,mode,reference_mode,roles):
    root,assets,_,_=source_project
    contract=json.loads((root/'artifacts/shot_contract.json').read_text())
    contract['assets']=[a for a in contract['assets'] if a['id'] not in {'voice','motion'}]
    shot=contract['shots'][0];shot['asset_ids']=[a for a in shot['asset_ids'] if a not in {'voice','motion'}]
    shot['duration_seconds']=5
    if reference_mode:shot['reference_mode']=reference_mode
    refresh(contract);write(root/'artifacts/shot_contract.json',contract)
    write(root/'artifacts/scene_plan.json',{'version':'1.0','scenes':[{'id':'entry','type':'generated','description':'Synthetic native composition','start_seconds':0,'end_seconds':5,'script_section_id':'s1'}]})
    write(root/'artifacts/script.json',{'version':'1.0','title':'Synthetic native composition','total_duration_seconds':5,'sections':[{'id':'s1','text':'The patient completes the reviewed action.','start_seconds':0,'end_seconds':5}]})
    authored=prep.compile_provider_prompt(root,'entry',provider='openart_mcp',model=model)
    params={'videoDuration':5,'videoResolution':'720p'} if model=='smart-shot' else {'duration':5,'resolution':'768P'}
    inputs={'project_dir':str(root),'model':model,'mode':mode,'prompt':authored['prompt'],
        'operation':{'image2video':'image_to_video','element2video':'reference_to_video','generate-shot-video':'shot_video'}[mode],
        'native_params':params,'openart_project_id':'fixture-project','governance':{'shot_id':'entry','scope_id':'not-dispatched','stage':'generate'},
        'input_assets':[{'role':role,**assets[name]} for role,name in roles],'compiled_request_id':'original','preparation_review_id':'original'}
    profile=mcp.load_profile(model,mode,require='candidate')
    native=prep.prep_builder('openart_mcp')(inputs,profile)
    execution._check_motion_inputs(contract,'entry',inputs,root)
    timing={'method':'segmented_estimate','duration_seconds':5,'language':'en','margin_seconds':.05,
        'rationale':'Synthetic offline timing','overlap_policy':'serial','overlap_rationale':'Synthetic offline timing','segments':[],
        'action_windows':[{'source_pointer':'/shot_contract/shots/0/'+key,'value_sha256':prep.digest(shot[key]),
            'start_seconds':a,'end_seconds':b,'rationale':'Synthetic offline timing'}
            for key,a,b in [('dominant_action',0,2.5),('completed_end_state',2.5,4.95)]]}
    compiled=prep.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
    write(root/'artifacts/compiled_request-original.json',compiled)
    review={'version':'1.0','review_id':'original','reviewer':'Synthetic offline reviewer','status':'pass','subject_sha256':prep.digest(compiled),
        'evidence_kind':'fixture_only','predicates':[{'name':p,'status':'pass','severity':'critical','evidence':'Synthetic offline review'} for p in sorted(prep.PREDICATES)]}
    write(root/'artifacts/preparation_review-original.json',review)
    prep.validate_preparation(inputs,native,profile)
    assert 'aspectRatio' not in native['body']['params']
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
