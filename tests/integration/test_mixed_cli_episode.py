"""Offline mixed subscription-CLI episode; all approvals/reviews are fixture-only.

Real registry planning, policy replay, request compiler, original process/ledger
journals, selection and FFmpeg assembly. External CLIs emit synthetic receipts
and the OpenArt HTTPS seam serves local FFmpeg test media. No live qualification,
provider quality, native dialogue, H3 comparison, spending or episode activation.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from lib import openart_credit as credit, openart_dispatch as dispatch, openart_jobs as jobs
from lib import production_autonomy as autonomy, production_execution as execution
from lib import production_request as preparation
from lib.shot_contract import ASSET_PREDICATES, UPSTREAM_PREDICATES, file_sha256, selection_digest
from tests.integration.test_openart_unknown_cost_workflow import workflow as unknown_workflow
from tests.integration.test_openart_first_pass_workflow import (FAKE_GROK, attest, real_av_clip,
    serve_result, media_path)
from tests.lib.test_production_autonomy import make_policy, install_existing
from tests.lib.test_production_request import write
from tests.lib.test_shot_contract import refresh
from tests.tools.test_grok_cli_media import CLI_HELP
from tools.tool_registry import ToolRegistry
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.openart_cli_video import OpenArtCLIVideo
from tools.video.video_selector import VideoSelector
from tools.video.video_compose import VideoCompose

FIXTURE = 'Synthetic offline software-test approval/review; no live or semantic quality authority.'


def read(path):
    return json.loads(Path(path).read_text())


def paid(path):
    path = Path(path)
    return path.read_text().splitlines() if path.exists() else []


class Episode:
    def __init__(self, tmp, monkeypatch, sample):
        self.tmp, self.mp = tmp, monkeypatch
        def forbidden_api(*args,**kwargs):raise AssertionError('No external API/purchase path is authorized in this fixture')
        monkeypatch.setattr('requests.post',forbidden_api)
        monkeypatch.setattr('requests.get',forbidden_api)
        sample_root, sample_inputs, profile, _, sample_tool, packet = sample
        self.staged_profile=copy.deepcopy(profile)
        self.clip = real_av_clip(tmp/'synthetic-source.mp4', seconds=1, size='1280x720')
        sample_result = sample_tool.execute(sample_inputs)
        sample_id = sample_result.data['production_attempt_id']
        monkeypatch.setenv('FAKE_STATUS','done')
        jobs.promote_result_contract(sample_id,json_paths={'url_hosts':['cdn.openart.test']})
        serve_result(monkeypatch,self.clip.read_bytes())
        execution.collect_openart_attempt(sample_root,sample_id,request_sha256=packet['request_sha256'])
        dispatch.resolve_attempt(sample_root,sample_id,packet['request_sha256'])
        self.profile = jobs.load_qualification(model='pixverseV6',mode='text2video',require='full')
        # The episode uses a distinct synthetic original job identity.
        binary=Path(os.environ['OPENART_CLI_PATH'])
        binary.write_text(binary.read_text().replace('fixture-original','episode-original'))
        self.bootstrap_count = len(paid(tmp/'paid'))
        assert self.bootstrap_count == 1
        self.root = tmp/'episode'; self.root.mkdir()
        contract = read(sample_root/'artifacts/shot_contract.json')
        contract.pop('reference_mode')
        contract['project_id'] = self.root.name
        entry = contract['shots'][0]
        entry.update(asset_ids=['start','end','payoff'])
        interior = copy.deepcopy(entry)
        interior.update(id='interior',reference_mode='reference_free',asset_ids=[],upstream=[])
        contract['shots'].append(interior)
        contract['assets'] = []
        for name,role,color in [('start','start_frame','red'),('end','end_frame','blue'),('payoff','payoff_board','white')]:
            path=self.root/'assets'/f'{name}.png';path.parent.mkdir(exist_ok=True)
            Image.new('RGB',(8,8),color).save(path)
            contract['assets'].append({'id':name,'role':role,'cast_ids':[],'path':str(path.relative_to(self.root)),
              'sha256':file_sha256(path),'review':attest(file_sha256(path),ASSET_PREDICATES)})
        contract['payoff_asset_id']='payoff'
        refresh(contract)
        self.contract=contract
        write(self.root/'artifacts/shot_contract.json',contract)
        write(self.root/'project.json',{'project_id':self.root.name,'story_revision':contract['story_revision'],
            'pipeline_type':'cinematic','governance':{'version':'1.0','mode':'strict'}})
        write(self.root/'artifacts/script.json',{'version':'1.0','title':'Synthetic mixed episode','total_duration_seconds':2,
            'sections':[{'id':f's{i+1}','text':'A red cube drops and comes to rest.','start_seconds':i,'end_seconds':i+1} for i in range(2)]})
        write(self.root/'artifacts/scene_plan.json',{'version':'1.0','scenes':[
            {'id':sid,'type':'generated','description':FIXTURE,'script_section_id':f's{i+1}','start_seconds':i,'end_seconds':i+1}
            for i,sid in enumerate(('entry','interior'))]})
        self.openart_inputs={'project_dir':str(self.root),'governance':{'scope_id':'planned','shot_id':'interior'},
            'operation':'text_to_video','model':'pixverseV6','mode':'text2video','duration':1,'aspect_ratio':'16:9',
            'resolution':'720p','output_path':str(self.root/'interior.mp4'),'compiled_request_id':'oa','preparation_review_id':'oar',
            'preferred_provider':'openart_cli','allowed_providers':['openart_cli'],'preferred_tool':'openart_cli_video'}
        self.grok_inputs={'project_dir':str(self.root),'governance':{'scope_id':'planned','shot_id':'entry'},
            'operation':'reference_to_video','first_frame':str(self.root/'assets/start.png'),
            'reference_image_paths':[str(self.root/'assets/payoff.png')],'duration':1,'aspect_ratio':'16:9',
            'resolution':'720p','output_path':str(self.root/'entry.mp4'),'compiled_request_id':'g','preparation_review_id':'gr',
            'allow_unknown_cost':True,'preferred_provider':'grok_cli','allowed_providers':['grok_cli'],'preferred_tool':'grok_cli_video'}
        grok=tmp/'fake-grok';grok.write_text(FAKE_GROK.replace('PYTHON',sys.executable).replace("artifact = Path(os.environ['GROK_SESSIONS_ROOT'])","if os.environ.get('FAKE_GROK_REFUSAL'):\n    print('HTTP 403 {\"code\":\"personal-team-blocked:spending-limit\"}',file=sys.stderr)\n    raise SystemExit(2)\nartifact = Path(os.environ['GROK_SESSIONS_ROOT'])"));grok.chmod(0o755)
        (tmp/'grok-help.txt').write_text(CLI_HELP)
        for key,value in {'GROK_CLI_PATH':grok,'GROK_SESSIONS_ROOT':tmp/'sessions','FAKE_GROK_LOG':tmp/'grok-calls',
          'FAKE_GROK_PAID':tmp/'grok-paid','FAKE_GROK_HELP':tmp/'grok-help.txt','FAKE_GROK_PROJECT':self.root,
          'FAKE_GROK_CLIP':self.clip}.items():monkeypatch.setenv(key,str(value))
        (tmp/'sessions').mkdir()
        self.registry=ToolRegistry()
        for tool in (GrokCLIVideo(),OpenArtCLIVideo(),VideoSelector(),VideoCompose()):self.registry.register(tool)
        monkeypatch.setattr('tools.tool_registry.registry',self.registry)
        monkeypatch.setattr(self.registry,'discover',lambda *a,**k:None)
        self.observation={'cli_version':'1.0.34','grok_path':str(grok)}

    def prepare(self,inputs,provider):
        authored=preparation.compile_provider_prompt(self.root,inputs['governance']['shot_id'],provider=provider)
        inputs['prompt']=authored['prompt']
        if provider=='openart_cli':
            from tools import _openart_cli as cli
            argv=cli.native_dry_run_argv(inputs['prompt'],model=inputs['model'],mode='text2video',duration=1,aspect_ratio='16:9',resolution='720p')
            observed=cli.run_readonly(argv)
            inputs.update(native_dry_run_receipt_id=observed['receipt_id'],native_dry_run_receipt_sha256=observed['receipt_sha256'])
            native=jobs.prepare_native_request(execution._openart_controls(inputs),self.profile)
            profile=self.profile
        else:
            native=preparation.prepare_grok_native(inputs,self.observation);profile={'source':'real'}
        index=next(i for i,shot in enumerate(self.contract['shots']) if shot['id']==inputs['governance']['shot_id'])
        shot=self.contract['shots'][index]
        timing={'method':'segmented_estimate','duration_seconds':1,'language':'en','margin_seconds':.05,
          'rationale':FIXTURE,'overlap_policy':'serial','overlap_rationale':FIXTURE,'segments':[],
          'action_windows':[{'source_pointer':f'/shot_contract/shots/{index}/'+key,'value_sha256':preparation.digest(shot[key]),
            'start_seconds':start,'end_seconds':end,'rationale':FIXTURE}
            for key,start,end in [('dominant_action',0,.6),('completed_end_state',.6,.95)]]}
        compiled=preparation.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
        review={'version':'1.0','review_id':inputs['preparation_review_id'],'reviewer':FIXTURE,'status':'pass',
          'subject_sha256':preparation.digest(compiled),'evidence_kind':'reviewed',
          'predicates':[{'name':p,'status':'pass','severity':'critical','evidence':FIXTURE} for p in sorted(preparation.PREDICATES)]}
        write(self.root/f"artifacts/compiled_request-{inputs['compiled_request_id']}.json",compiled)
        write(self.root/f"artifacts/preparation_review-{inputs['preparation_review_id']}.json",review)
        if provider=='openart_cli':
            inputs['unknown_cost_evidence_id']=credit.refresh_unknown_cost_evidence(inputs,self.profile)['evidence_id']
        return inputs

    def activate(self,*,caps=None):
        policy=make_policy(checkpoint_stages=[],locked={'cast':{},'dialogue':{},'sources':[],'story_predicates':[],'controls':{}},
          providers=[{'id':'grok_cli','model_policy':'cli_managed_media_unreported','billing':'subscription_quota_unknown'},
            {'id':'openart_cli','billing':'unknown_cost_no_ceiling','exposure_acknowledgement':'no_enforceable_credit_ceiling',
              'account_id_sha256':self.profile['account_id_sha256'],'workspace':'__unobserved_workspace__',
              'routes':[{'model':'pixverseV6','mode':'text2video'}]}],
          flex={'duration_s':[],'resolution':[],'references':{'droppable_roles':[],'substitutes':[]}})
        policy['model_selection_intents']={
          'entry':{'mode':'exact','provider':'grok_cli','approved_pool':[{'provider':'grok_cli','tool':'grok_cli_video'}]},
          'interior':{'mode':'exact','provider':'openart_cli','model':'pixverseV6',
            'approved_pool':[{'provider':'openart_cli','model':'pixverseV6','tool':'openart_cli_video'}]}}
        if hasattr(self,'quota_inputs'):policy['model_selection_intents']['quota']=copy.deepcopy(policy['model_selection_intents']['entry'])
        if caps:policy['caps']=caps
        templates={'entry':execution.planned_request_template(self.grok_inputs,project_dir=self.root),
          'interior':execution.planned_request_template(self.openart_inputs,project_dir=self.root)}
        if hasattr(self,'quota_inputs'):templates['quota']=execution.planned_request_template(self.quota_inputs,project_dir=self.root)
        self.policy,self.policy_sha=install_existing(self.root,policy,templates=templates)
        assert autonomy.require_active_policy(self.root)[1]==self.policy_sha

    def derive(self,inputs,provider):
        scope=autonomy.derive_scope(self.root,inputs,provider=provider,
          observation=self.observation if provider=='grok_cli' else None)
        inputs['governance']['scope_id']=scope['id']
        return scope

    def select(self,sid,result):
        aid=result.data['production_attempt_id'] if hasattr(result,'data') else result
        state=execution.load_attempt_result(self.root,aid)
        from tools.analysis.frame_sampler import FrameSampler
        frame=FrameSampler().execute({'input_path':state['output']['path'],'strategy':'timestamps','timestamps':[.9],
          'output_dir':str(self.root/'frames'/sid),'format':'png'})
        assert frame.success,frame.error
        path=Path(frame.data['frames'][0]['path'])
        selection={'attempt_id':aid,'output':state['output'],'outgoing_frame':{'path':str(path),'sha256':file_sha256(path)}}
        selection['review']=attest(selection_digest(selection),UPSTREAM_PREDICATES)
        execution.record_selection(self.root,sid,selection)
        return selection


@pytest.fixture
def episode(tmp_path,monkeypatch,media_path):
    bootstrap=unknown_workflow.__wrapped__(tmp_path,monkeypatch)
    sample=next(bootstrap)
    try:yield Episode(tmp_path,monkeypatch,sample)
    finally:
        try:next(bootstrap)
        except StopIteration:pass


def test_mixed_registry_policy_episode_preserves_selected_outputs_and_assembles(episode):
    e=episode
    pool=[{'provider':'grok_cli','tool':'grok_cli_video'},{'provider':'openart_cli','model':'pixverseV6','tool':'openart_cli_video'}]
    for inputs,provider in ((e.grok_inputs,'grok_cli'),(e.openart_inputs,'openart_cli')):
        inputs['model_selection_intent']={'mode':'exact','provider':provider,'approved_pool':pool}
        if provider=='openart_cli':inputs['model_selection_intent']['model']='pixverseV6'
        ranked=e.registry.get('video_selector').execute({**inputs,'operation':'rank','target_operation':inputs['operation']})
        assert ranked.success,ranked.error
        planned=ranked.data['planned_request']
        assert planned['preferred_provider']==provider and planned['allowed_providers']==[provider]
        inputs.pop('model_selection_intent',None)
        inputs.update(planned)
    e.activate()
    e.prepare(e.grok_inputs,'grok_cli');e.prepare(e.openart_inputs,'openart_cli')
    e.derive(e.grok_inputs,'grok_cli')
    first=e.registry.get('video_selector').execute(e.grok_inputs)
    assert first.success,first.error
    first_selection=e.select('entry',first)
    scope=e.derive(e.openart_inputs,'openart_cli')
    assert e.openart_inputs['unknown_cost_authorization_id']==scope['id']
    assert read(e.root/f"artifacts/unknown_cost_authorization-{scope['id']}.json")['approved_by']==f'policy:{e.policy_sha}'
    second=e.registry.get('video_selector').execute(e.openart_inputs)
    aid=second.data['production_attempt_id']
    collected=execution.collect_openart_attempt(e.root,aid,request_sha256=read(e.root/'production_attempts'/aid/'request.json')['request_sha256'])
    assert collected['status']=='generated'
    dispatch.resolve_attempt(e.root,aid,read(e.root/'production_attempts'/aid/'request.json')['request_sha256'])
    second_selection=e.select('interior',aid)
    counts=autonomy.root_attempt_counts(e.root,e.policy)
    assert counts['total']==2 and counts['per_shot']=={'entry':1,'interior':1} and counts['repair']==0
    decisions=read(e.root/'artifacts/decision_log.json')['decisions']
    route_choices=[d for d in decisions if d['category']=='provider_selection']
    assert len(route_choices)==2 and all(d['user_approved'] is False for d in route_choices)
    assert len(paid(e.tmp/'grok-paid'))==1 and len(paid(e.tmp/'paid'))-e.bootstrap_count==1
    master=e.root/'renders/master.mp4'
    composed=e.registry.get('video_compose').execute({'operation':'compose','output_path':str(master),
      'edit_decisions':{'version':'1.0','render_runtime':'ffmpeg','cuts':[
        {'id':sid,'source':chosen['output']['path'],'in_seconds':0,'out_seconds':1}
        for sid,chosen in [('entry',first_selection),('interior',second_selection)]],
        'metadata':{'compose_target':{'width':320,'height':180}}}})
    assert composed.success,composed.error
    from lib.production_review import probe_master
    assert abs(float(probe_master(master)['duration_seconds'])-2)<.1
    assert execution.load_selected_attempts(e.root)=={'entry':first_selection,'interior':second_selection}
    assert not (e.root/'artifacts/final_review.json').exists()
    assert_unknown_draft_report(e)


@pytest.mark.parametrize('case',['model_lock','unsupported_audio','outside_pool'])
def test_exact_planning_blocks_candidate_or_missing_native_control_without_dispatch(episode,case):
    e=episode
    inputs=copy.deepcopy(e.openart_inputs)
    intent={'mode':'exact','provider':'openart_cli','model':'pixverseV6',
      'approved_pool':[{'provider':'openart_cli','model':'pixverseV6','tool':'openart_cli_video'},
                       {'provider':'grok_cli','tool':'grok_cli_video'}]}
    if case=='model_lock':intent.update(model='unqualified-fixture-model');intent['approved_pool'].append({'provider':'openart_cli','model':'unqualified-fixture-model'})
    if case=='unsupported_audio':inputs['native_audio']=True
    if case=='outside_pool':intent['approved_pool']=[{'provider':'grok_cli','tool':'grok_cli_video'}]
    result=e.registry.get('video_selector').execute({**inputs,'model_selection_intent':intent,'operation':'rank','target_operation':'text_to_video'})
    assert not result.success and result.data['dispatch_status']=='not_dispatched'
    assert result.data['model_selection']['blockers']
    assert len(paid(e.tmp/'paid'))==e.bootstrap_count and not paid(e.tmp/'grok-paid')


def prepare_policy(e,caps=None):
    e.activate(caps=caps);e.prepare(e.grok_inputs,'grok_cli');e.prepare(e.openart_inputs,'openart_cli')


def test_one_off_unknown_approval_is_not_policy_and_revocation_stops_next_shot(episode):
    e=episode
    e.prepare(e.openart_inputs,'openart_cli')
    # The Strict sample's actual retained approval is in its own project and is
    # never promoted to episode policy authority.
    with pytest.raises(autonomy.AutonomyError):e.derive(e.openart_inputs,'openart_cli')
    e.activate();e.prepare(e.grok_inputs,'grok_cli')
    e.derive(e.grok_inputs,'grok_cli')
    first=e.registry.get('video_selector').execute(e.grok_inputs)
    assert first.success,first.error
    chosen=e.select('entry',first)
    log=read(e.root/'artifacts/decision_log.json')
    log['decisions'].append({'decision_id':'fixture-revoke','stage':'assets','category':'approval_policy',
      'subject':autonomy.ACTIVATION_SUBJECT,'selected':'strict','user_approved':True,'reason':FIXTURE,
      'options_considered':[{'option_id':'strict','label':'Strict','score':1,'reason':FIXTURE}]})
    write(e.root/'artifacts/decision_log.json',log)
    with pytest.raises(autonomy.AutonomyError):e.derive(e.openart_inputs,'openart_cli')
    assert execution.load_selected_attempts(e.root)['entry']==chosen
    assert file_sha256(chosen['output']['path'])==chosen['output']['sha256']
    assert len(paid(e.tmp/'grok-paid'))==1 and len(paid(e.tmp/'paid'))==e.bootstrap_count


def test_cumulative_policy_cap_stops_second_route_without_losing_first(episode):
    e=episode
    prepare_policy(e,{'max_total_attempts':1,'max_attempts_per_shot':1,'max_repair_attempts':0})
    e.derive(e.grok_inputs,'grok_cli')
    first=e.registry.get('video_selector').execute(e.grok_inputs)
    assert first.success,first.error
    chosen=e.select('entry',first)
    with pytest.raises(autonomy.AutonomyError,match='total attempts'):e.derive(e.openart_inputs,'openart_cli')
    assert execution.load_selected_attempts(e.root)['entry']==chosen
    assert file_sha256(chosen['output']['path'])==chosen['output']['sha256']
    assert len(paid(e.tmp/'grok-paid'))==1 and len(paid(e.tmp/'paid'))==e.bootstrap_count


def test_uncertain_openart_original_blocks_cross_route_and_never_resubmits(episode):
    e=episode
    prepare_policy(e)
    e.derive(e.grok_inputs,'grok_cli')
    first=e.registry.get('video_selector').execute(e.grok_inputs)
    assert first.success,first.error
    chosen=e.select('entry',first)
    e.derive(e.openart_inputs,'openart_cli')
    seen=[]
    def crash(stage,aid):
        if stage=='provider_acceptance':seen.append(aid);raise RuntimeError('synthetic uncertain original')
    dispatch._CRASH_HOOK=crash
    try:
        with pytest.raises(RuntimeError,match='synthetic uncertain'):e.registry.get('video_selector').execute(e.openart_inputs)
    finally:dispatch._CRASH_HOOK=None
    assert len(seen)==1
    original=seen[0]
    jobs.recover_launch(original);dispatch.record_launch_result(original)
    with pytest.raises(Exception):e.registry.get('video_selector').execute(copy.deepcopy(e.openart_inputs))
    alternate={'project_dir':str(e.root),'governance':{'scope_id':'alternate','shot_id':'interior'},
      'preferred_provider':'grok_cli','allowed_providers':['grok_cli'],'operation':'reference_to_video',
      'first_frame':str(e.root/'assets/start.png'),'prompt':FIXTURE,'duration':1,'resolution':'720p',
      'output_path':str(e.root/'alternate.mp4'),'allow_unknown_cost':True}
    with pytest.raises(execution.ProductionGovernanceError,match='original video attempt'):
        e.registry.get('video_selector').execute(alternate)
    assert execution.load_selected_attempts(e.root)['entry']==chosen
    assert file_sha256(chosen['output']['path'])==chosen['output']['sha256']
    assert len(paid(e.tmp/'grok-paid'))==1 and len(paid(e.tmp/'paid'))-e.bootstrap_count==1


def test_known_quota_refusal_preserves_earlier_shot_and_only_in_policy_independent_choice(episode,monkeypatch):
    e=episode
    quota=copy.deepcopy(e.contract['shots'][0]);quota['id']='quota';e.contract['shots'].append(quota);refresh(e.contract)
    write(e.root/'artifacts/shot_contract.json',e.contract)
    script=read(e.root/'artifacts/script.json');script['total_duration_seconds']=3
    script['sections'].append({'id':'s3','text':'A red cube drops and comes to rest.','start_seconds':2,'end_seconds':3})
    write(e.root/'artifacts/script.json',script)
    scene=read(e.root/'artifacts/scene_plan.json')
    scene['scenes'].append({'id':'quota','type':'generated','description':FIXTURE,'script_section_id':'s3','start_seconds':2,'end_seconds':3})
    write(e.root/'artifacts/scene_plan.json',scene)
    e.quota_inputs=copy.deepcopy(e.grok_inputs)
    e.quota_inputs.update(output_path=str(e.root/'quota.mp4'),compiled_request_id='q',preparation_review_id='qr')
    e.quota_inputs['governance']['shot_id']='quota'
    prepare_policy(e);e.prepare(e.quota_inputs,'grok_cli')
    e.derive(e.grok_inputs,'grok_cli')
    first=e.registry.get('video_selector').execute(e.grok_inputs)
    assert first.success,first.error
    chosen=e.select('entry',first)
    e.derive(e.quota_inputs,'grok_cli')
    monkeypatch.setenv('FAKE_GROK_REFUSAL','quota')
    refused=e.registry.get('video_selector').execute(e.quota_inputs)
    assert not refused.success
    assert refused.data['error_category']=='spending_limit' and refused.data['dispatch_status']=='failed'
    assert refused.data['retry_attempted'] is False and refused.data['fallback_attempted'] is False
    quota_aid=refused.data['production_attempt_id']
    assert execution.load_attempt_result(e.root,quota_aid)['status']=='failed'
    assert not Path(e.quota_inputs['output_path']).exists()
    with pytest.raises(Exception):e.registry.get('video_selector').execute(copy.deepcopy(e.quota_inputs))
    # Native board requirements cannot be silently dropped to make this blocked
    # shot an OpenArt reference-free replacement, even with both routes in policy.
    replacement=copy.deepcopy(e.openart_inputs);replacement['governance']['shot_id']='quota'
    with pytest.raises(Exception):e.prepare(replacement,'openart_cli')
    monkeypatch.delenv('FAKE_GROK_REFUSAL')
    e.derive(e.openart_inputs,'openart_cli')
    original=e.registry.get('video_selector').execute(e.openart_inputs)
    aid=original.data['production_attempt_id']
    receipt=read(e.root/'production_attempts'/aid/'request.json')
    assert execution.collect_openart_attempt(e.root,aid,request_sha256=receipt['request_sha256'])['status']=='generated'
    dispatch.resolve_attempt(e.root,aid,receipt['request_sha256'])
    e.select('interior',aid)
    selected=execution.load_selected_attempts(e.root)
    assert selected['entry']==chosen and set(selected)=={'entry','interior'}
    assert file_sha256(chosen['output']['path'])==chosen['output']['sha256']
    assert len(paid(e.tmp/'grok-paid'))==2 and len(paid(e.tmp/'paid'))-e.bootstrap_count==1
    assert autonomy.root_attempt_counts(e.root,e.policy)['total']==3
    assert not (e.root/'artifacts/final_review.json').exists()  # required quota shot remains missing
    report=assert_unknown_draft_report(e)
    assert report['grok']=='subscription quota unknown, 2 attempts'
    assert any(row['shot_id']=='quota' and row['status']=='failed' for row in report['attempts'])


def test_active_policy_exact_model_lock_rejects_other_route_before_scope(episode):
    e=episode
    prepare_policy(e)
    changed=copy.deepcopy(e.openart_inputs)
    changed['governance']['shot_id']='entry'
    with pytest.raises(autonomy.AutonomyError,match='model selection intent'):
        e.derive(changed,'openart_cli')
    assert not (e.root/'production_scopes.json').exists()
    assert len(paid(e.tmp/'paid'))==e.bootstrap_count and not paid(e.tmp/'grok-paid')


def test_local_reference_free_wrong_native_mode_is_blocked_before_dispatch(episode):
    e=episode
    prepare_policy(e)
    changed=copy.deepcopy(e.openart_inputs)
    changed.update(mode='image2video',operation='reference_to_video',
      first_frame=str(e.root/'assets/start.png'))
    with pytest.raises(Exception):e.prepare(changed,'openart_cli')
    assert len(paid(e.tmp/'paid'))==e.bootstrap_count and not paid(e.tmp/'grok-paid')
    assert not (e.root/'production_attempts').exists()


def test_local_reference_free_provenance_rejects_extra_input_snapshot(episode):
    e=episode
    prepare_policy(e)
    e.derive(e.openart_inputs,'openart_cli')
    result=e.registry.get('video_selector').execute(e.openart_inputs)
    aid=result.data['production_attempt_id']
    path=e.root/'production_attempts'/aid/'request.json'
    request=read(path)
    assert request['input_assets']==[]
    assert execution.collect_openart_attempt(e.root,aid,request_sha256=request['request_sha256'])['status']=='generated'
    dispatch.resolve_attempt(e.root,aid,request['request_sha256'])
    # Dedicated negative fixture: corrupt its unselected public test journal.
    # The immutable private originals and positive episode fixture stay intact.
    request['input_assets']=[{'role':'first_frame','path':str(e.root/'assets/start.png'),
      'original_path':str(e.root/'assets/start.png'),'sha256':file_sha256(e.root/'assets/start.png')}]
    path.unlink()  # Replace only this isolated negative fixture's read-only journal.
    write(path,request)
    with pytest.raises(execution.ProductionGovernanceError,match='historical extra submitted inputs'):
        e.select('interior',aid)
    assert execution.load_selected_attempts(e.root)=={}
    assert len(paid(e.tmp/'paid'))-e.bootstrap_count==1 and not paid(e.tmp/'grok-paid')


def test_supported_pre_submit_profile_plans_as_episode_production_route_without_prior_result(episode):
    e=episode
    from tools import _openart_cli as cli
    plan_inputs={**e.openart_inputs,'operation':'rank','target_operation':'text_to_video',
      'model_selection_intent':{'mode':'exact','provider':'openart_cli','model':'pixverseV6',
        'approved_pool':[{'provider':'openart_cli','model':'pixverseV6'}]}}
    available=e.registry.get('video_selector').execute(plan_inputs)
    assert available.success,available.error
    assert available.data['planned_request']['model']=='pixverseV6'
    # Restore genuine pre-submit fixture bytes (native form + exact preview + account, no
    # prior generated result). Supported native readiness alone is production-ready.
    for path in (jobs.profile_path(),jobs.profile_path_for('pixverseV6','text2video')):
        if path.exists():path.unlink()
        cli.write_private(path,json.dumps(e.staged_profile,sort_keys=True).encode())
    result=e.registry.get('video_selector').execute({**e.openart_inputs,'operation':'rank','target_operation':'text_to_video',
      'model_selection_intent':{'mode':'exact','provider':'openart_cli','model':'pixverseV6',
        'approved_pool':[{'provider':'openart_cli','model':'pixverseV6'}]}})
    assert result.success,result.error
    assert result.data['planned_request']['model']=='pixverseV6'
    assert result.data['dispatch_status']=='not_dispatched'  # planning never submits
    assert len(paid(e.tmp/'paid'))==e.bootstrap_count and not paid(e.tmp/'grok-paid')


def assert_unknown_draft_report(e):
    report=read(autonomy.completion_report(e.root,e.policy_sha))
    assert report['quality_status']=='draft' and report['openart_credits']==[]
    exposure=report['openart_unknown_cost']
    assert exposure['cost_status']=='unknown' and exposure['billing']=='unknown_cost_no_ceiling'
    assert exposure['exposure_acknowledgement']=='no_enforceable_credit_ceiling'
    rows=[row for row in report['attempts'] if row['settings']['provider']=='openart_cli']
    assert len(rows)==1
    assert rows[0]['credits']['requested_charge']=='unknown'
    assert rows[0]['credits']['billing_state']=='unknown'
    assert rows[0]['credits']['slot_state']=='terminal'
    assert 'billed_amount' not in rows[0]['credits']
    assert 'ceiling' not in exposure and 'cost_usd' not in exposure
    return report
