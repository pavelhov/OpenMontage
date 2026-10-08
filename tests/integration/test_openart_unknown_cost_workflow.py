"""Offline registered unknown-cost rehearsal; no live provider or network.

Real: ToolRegistry/OpenArtCLIVideo, reference-free compiler and native preview
validation, exact preparation checkpoint, typed SQLite ledger, durable dispatch,
original launch/receipt recovery, qualification, collection and canonical report.
Mocked: executable official-CLI-shaped subprocess replies, narrow pinned HTTPS
socket serving explicit synthetic bytes, semantic review and human approvals.
One 1s PixVerse V6 original at 720p/16:9; zero uploads, repairs or quality claims.
"""
from lib import openart_credit as credit

import copy
import hashlib
import io
import json
import socket
import sys
from pathlib import Path

import pytest
from lib import openart_dispatch as dispatch, openart_jobs as jobs, openart_setup as setup
from lib import openart_download as download, production_execution as execution, production_request as preparation
from lib.checkpoint import write_checkpoint
from tests.lib.test_production_request import reference_free_package, write
from tests.lib.test_openart_credit import PATHS
from tools import _openart_cli as cli
from tools.tool_registry import ToolRegistry
from tools.video.openart_cli_video import OpenArtCLIVideo

FIXTURE = 'Synthetic software-test approval and review only; no live authority or footage judgment.'
FAKE = r'''#!PYTHON
import hashlib,json,os,sys
args=sys.argv[1:-2]
with open(os.environ['FAKE_LOG'],'a') as f:f.write(json.dumps(args)+'\n')
if args==['version']:out={'version':'0.1.1-fixture'}
elif args==['account']:out={'id':'fixture-account','plan':'Free','credits':40}
elif args[:2]==['model','form']:out={'type':'object','properties':{'prompt':{'type':'string'},'duration':{'type':'integer','default':5},'aspectRatio':{'type':'string'},'resolution':{'type':'string'}},'required':['prompt']}
elif args[:2]==['model','cost']:out={'currency':'credits','items':[{'model':'pixverseV6','mode':'text2video','media':'video','config':{'aspectRatio':'16:9','duration':5,'generateAudio':False,'resolution':'540p','videoCount':1},'unitCredits':50,'quantity':1,'totalCredits':50}]}
elif args[:2]==['creation','get']:out={'creation':{'id':'fixture-original','status':os.environ.get('FAKE_STATUS','running'),'urls':['https://cdn.openart.test/original.mp4'] if os.environ.get('FAKE_STATUS')=='done' else []}}
elif args[:2]==['generate','video']:
 params={'prompt':args[2]}
 for flag,key in [('--duration','duration'),('--aspect-ratio','aspectRatio'),('--resolution','resolution')]:
  if flag in args:params[key]=int(args[args.index(flag)+1]) if key=='duration' else args[args.index(flag)+1]
 body={'model':'pixverseV6','media':'video','mode':'text2video','params':params}
 if '--dry-run' in args:out={'endpoint':'POST /api/cli/v1/generate','body':body}
 else:
  with open(os.environ['FAKE_PAID'],'a') as f:f.write('fixture-original\n')
  failure=os.environ.get('FAKE_REFUSAL')
  if failure:
   print(json.dumps({'error':{'code':failure,'message':'synthetic refusal'}}));raise SystemExit(1)
  out={'job':{'id':'fixture-original'}}
else:raise SystemExit(7)
print(json.dumps(out))
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def submissions(tmp):
    path=tmp/'paid'
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    original_run, original_profile = cli._run_checked, jobs.load_qualification
    root,inputs,_,_,compiled,review = reference_free_package.__wrapped__(tmp_path,monkeypatch)
    root=root.rename(root.parent/read(root/'project.json')['project_id'])
    inputs['model']='pixverseV6'
    inputs['project_dir']=str(root)
    inputs['output_path']=str(root/'clip.mp4')
    monkeypatch.setattr(cli,'_run_checked',original_run)
    monkeypatch.setattr(jobs,'load_qualification',original_profile)
    binary=tmp_path/'fake-openart';binary.write_text(FAKE.replace('PYTHON',sys.executable));binary.chmod(0o755)
    for key,value in {'OPENART_CLI_PATH':binary,'FAKE_LOG':tmp_path/'calls','FAKE_PAID':tmp_path/'paid'}.items():monkeypatch.setenv(key,str(value))
    monkeypatch.delenv('OPENMONTAGE_OPENART_OFFLINE',raising=False)
    jobs.register_reservation_lookup(None)
    monkeypatch.setattr(jobs,'_ACCOUNT_CHECK',None)
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',execution._compiled_request_check)
    paths=dict(PATHS,account_tier='plan',url_hosts=['cdn.openart.test'])
    setup.inspect_qualification('pixverseV6','text2video',json_paths=paths)
    setup.qualify_preview('pixverseV6','text2video',prompt=inputs['prompt'],duration=1,aspect_ratio='16:9',resolution='720p')
    profile=jobs.load_qualification(model=inputs['model'],mode=inputs['mode'],require='pre_submit')
    dr=next(e for e in profile['captured_receipts'] if e['kind']=='dry_run')
    inputs.update(native_dry_run_receipt_id=dr['receipt_id'],native_dry_run_receipt_sha256=dr['receipt_sha256'],unknown_cost_authorization_id='original')
    inputs['governance']['stage']='generate'
    native=jobs.prepare_native_request(execution._openart_controls(inputs),profile)
    compiled=preparation.prepare_compiled_request(inputs,native,profile,coverage=compiled['coverage'],timing=compiled['timing'])
    review.update(subject_sha256=preparation.digest(compiled),reviewer=FIXTURE,evidence_kind='reviewed')
    write(root/'artifacts/compiled_request-c1.json',compiled);write(root/'artifacts/preparation_review-r1.json',review)
    marker=read(root/'project.json');marker['pipeline_type']='provider-qualification';write(root/'project.json',marker)
    contract=read(root/'artifacts/shot_contract.json')
    observed=credit.refresh_unknown_cost_evidence(inputs,profile,timeout=30)
    inputs['unknown_cost_evidence_id']=observed['evidence_id']
    request_sha=execution.planned_request_digest(inputs,project_dir=root)
    authority={'version':'1','kind':'openart_unknown_cost','status':'approved','approved_by':FIXTURE,
      'exposure_acknowledgement':'no_enforceable_credit_ceiling','provider':'openart_cli','project_root':str(root),
      'project_id':marker['project_id'],'story_revision':marker['story_revision'],'scope_id':'approved','shot_id':'entry',
      'account_id_sha256':profile['account_id_sha256'],'workspace':'__unobserved_workspace__','workspace_observed':False,
      'workspace_billing_guarantee':'unverified','model':inputs['model'],'mode':'text2video','count':1,
      'purpose':'result_contract_qualification','occurrences':[{'id':'fixture-original','index':0,'request_sha256':request_sha,
       'native_sha256':native['native_body_sha256'],'profile_sha256':native['profile_sha256']}]}
    raw=json.dumps({'kind':'openart_unknown_cost_authorization','terms':authority},sort_keys=True).encode()
    (root/'unknown-approval.json').write_bytes(raw)
    authority['evidence']={'path':'unknown-approval.json','sha256':sha(root/'unknown-approval.json')}
    write(root/'artifacts/unknown_cost_authorization-original.json',authority)
    (root/'scope-approval.txt').write_text(FIXTURE)
    scope={'id':'approved','status':'approved','approved_by':FIXTURE,'provider':'openart_cli',
      'project_id':marker['project_id'],'story_revision':marker['story_revision'],'phase':'first_pass',
      'approval_plan_sha256':execution.approval_plan_digest(contract),
      'evidence':{'path':'scope-approval.txt','sha256':sha(root/'scope-approval.txt')},
      'requests':{'entry':request_sha},'attempts_per_shot':{'entry':1},
      'unknown_cost_authorization_sha256':preparation.digest({k:v for k,v in authority.items() if k!='evidence'})}
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    def binding(path):return {'path':path,'sha256':sha(root/path)}
    packet={'version':'1.0','project_id':marker['project_id'],'story_revision':marker['story_revision'],
      'purpose':'result_contract_qualification','authorization_status':'proposed','governance_mode':'strict','tool':'openart_cli_video',
      'prompt':inputs['prompt'],'prompt_sha256':hashlib.sha256(inputs['prompt'].encode()).hexdigest(),'request_sha256':request_sha,
      'native_binding':{k:native[k] for k in ('native_controls_sha256','native_argv_sha256','native_body_sha256','profile_sha256')},
      'compiled_request':binding('artifacts/compiled_request-c1.json'),'preparation_review':binding('artifacts/preparation_review-r1.json'),
      'sources':[binding('artifacts/shot_contract.json'),binding('artifacts/script.json'),binding('artifacts/scene_plan.json')],
      'scope_id':'approved','shot_id':'entry','unknown_cost_authorization_id':'original','provider':'openart_cli',
      'model':inputs['model'],'mode':'text2video','duration':1,'aspect_ratio':'16:9','resolution':'720p','native_no_reference':True,
      'unknown_exposure':{'kind':'unknown_cost','credits':None,'usd':None,'guaranteed_ceiling':False},
      'attempt_cap':1,'repair_cap':0,'quality_scope':'transport_only'}
    write(root/'artifacts/provider_qualification_packet.json',packet)
    write_checkpoint(root.parent,root.name,'prepare','completed',{'provider_qualification_packet':packet},human_approved=True)
    registry=ToolRegistry();registry.register(OpenArtCLIVideo())
    yield root,inputs,profile,tmp_path,registry.get('openart_cli_video'),packet
    dispatch._CRASH_HOOK=None;jobs.register_reservation_lookup(None)


def test_registered_original_dispatch_and_original_collection(workflow,monkeypatch):
    root,inputs,profile,tmp,tool,packet=workflow
    result=tool.execute(copy.deepcopy(inputs))
    attempt=result.data['production_attempt_id']
    assert submissions(tmp)==['fixture-original']
    assert not Path(inputs['output_path']).exists()
    assert packet['repair_cap']==0 and packet['quality_scope']=='transport_only'
    assert not (root/'assets').exists()
    with pytest.raises(Exception):tool.execute(copy.deepcopy(inputs))
    monkeypatch.setenv('FAKE_STATUS','done')
    promoted=jobs.promote_result_contract(attempt,json_paths={'url_hosts':['cdn.openart.test']})
    assert promoted['profile_sha256']==profile['profile_sha256']
    # Explicitly synthetic transport bytes: this asserts collection, never video quality.
    body=b'SYNTHETIC TEST VIDEO BYTES; NOT PROVIDER FOOTAGE'
    class Sock:
        def sendall(self,data):pass
        def makefile(self,mode):return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n'%len(body)+body)
        def settimeout(self,value):pass
        def close(self):pass
    monkeypatch.setattr(download,'_resolve_public',lambda *a:(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('93.184.216.34',443)))
    monkeypatch.setattr(download._PinnedHTTPSConnection,'connect',lambda self:setattr(self,'sock',Sock()))
    collected=execution.collect_openart_attempt(root,attempt,request_sha256=packet['request_sha256'])
    assert collected['status']=='generated'
    assert Path(inputs['output_path']).read_bytes()==body
    assert submissions(tmp)==['fixture-original']
    dispatch.resolve_attempt(root,attempt,packet['request_sha256'])
    row=dispatch.ledger().inspect_unpriced(attempt)
    assert row['slot_state']=='terminal' and row['billed_amount']==''
    assert dispatch.ledger().authorization_kind(attempt)=='unknown_cost'
    assert row['billing_state']=='unknown'
    from lib.production_autonomy_report import unknown_cost_report
    ledger_before=dispatch.ledger().path.read_bytes()
    calls_before=(tmp/'calls').read_bytes()
    report=unknown_cost_report(root)
    assert dispatch.ledger().path.read_bytes()==ledger_before
    assert (tmp/'calls').read_bytes()==calls_before
    entry=next(item for item in report['attempts'] if item['attempt_id']==attempt)
    assert report['quality_status']=='unreviewed'
    assert entry['requested_charge']=='unknown' and entry['billing_state']=='unknown'
    assert entry['price_classification']=='mismatched_default'
    assert entry['balance_observation']['label']=='unattributed_observation'
    assert 'billed_amount' not in entry and 'ceiling' not in entry and 'usd' not in entry
    assert execution.collect_openart_attempt(root,attempt,request_sha256=packet['request_sha256'])==collected
    assert submissions(tmp)==['fixture-original']


def test_exact_human_preparation_gate_required_before_submission(workflow):
    root,inputs,_,tmp,tool,_=workflow
    gate=read(root/'checkpoint_prepare.json');gate['human_approved']=False;write(root/'checkpoint_prepare.json',gate)
    with pytest.raises(Exception,match='human approval'):tool.execute(inputs)
    assert submissions(tmp)==[]


def test_post_acceptance_crash_recovers_same_original_without_retry(workflow):
    root,inputs,_,tmp,tool,packet=workflow
    seen=[]
    def crash(stage,attempt):
        if stage=='provider_acceptance':seen.append(attempt);raise RuntimeError('synthetic acceptance crash')
    dispatch._CRASH_HOOK=crash
    with pytest.raises(RuntimeError,match='synthetic acceptance'):tool.execute(inputs)
    dispatch._CRASH_HOOK=None
    attempt=seen[0]
    jobs.recover_launch(attempt);dispatch.record_launch_result(attempt)
    dispatch.repair_outbox(root,attempt)
    with pytest.raises(Exception):tool.execute(copy.deepcopy(inputs))
    assert submissions(tmp)==['fixture-original']
    assert dispatch.ledger().inspect_unpriced(attempt)['slot_state'] in {'submitting','submitted','uncertain'}


def test_default_fifty_credit_price_does_not_decide_one_second_affordability(workflow):
    root,inputs,profile,tmp,tool,_=workflow
    call_count=len((tmp/'calls').read_text().splitlines())
    observed=credit.refresh_unknown_cost_evidence(inputs,profile,timeout=30)
    fresh_calls=[json.loads(line) for line in (tmp/'calls').read_text().splitlines()[call_count:]]
    assert len(fresh_calls)==observed['provider_calls']==6
    assert all(args[:2]!=['upload','add'] and (args[:2]!=['generate','video'] or '--dry-run' in args) for args in fresh_calls)
    assert observed['requested_charge']=='unknown'
    assert observed['price_classification']=='mismatched_default'
    assert observed['observed_default_price']['credits']=='50'
    assert observed['observed_default_price']['settings']['duration']==5
    assert observed['observed_default_price']['settings']['resolution']=='540p'
    assert observed['balance_observation']['credits']=='40'
    assert observed['balance_observation']['label']=='unattributed_observation'
    assert 'affordable' not in observed and 'insufficient_funds' not in observed
    before=(tmp/'calls').read_bytes()
    retained=credit.get_retained_unknown_evidence(inputs,profile,observed['evidence_id'])
    assert retained['provider_calls']==0
    for key in ('evidence_id','evidence_sha256','requested_charge','price_classification','observed_default_price','balance_observation'):
        assert retained[key]==observed[key]
    assert (tmp/'calls').read_bytes()==before
    assert submissions(tmp)==[]


def test_cross_route_guard_reads_original_unpriced_hold(workflow):
    from lib.production_video_guard import read_video_duplicate_blocks
    root,inputs,_,tmp,tool,_=workflow
    result=tool.execute(inputs)
    attempt=result.data['production_attempt_id']
    reasons=read_video_duplicate_blocks(root,'entry')
    assert reasons and any(attempt in reason for reason in reasons)
    # Exercise the route-independent guard at the shared preflight boundary.
    # The alternative project marker is fixture setup, never permission to generate.
    marker=read(root/'project.json');marker['pipeline_type']='cinematic';write(root/'project.json',marker)
    from tools.video.grok_cli_video import GrokCLIVideo
    alternate={'project_dir':str(root),'governance':{'scope_id':'grok-alternative','shot_id':'entry'},
               'prompt':'synthetic alternate route','operation':'text_to_video','output_path':str(root/'alternate.mp4')}
    with pytest.raises(execution.ProductionGovernanceError,match='original video attempt'):
        execution.preflight(GrokCLIVideo(),alternate)
    assert submissions(tmp)==['fixture-original']


@pytest.mark.parametrize('field,value',[('exposure_acknowledgement','estimated_ceiling'),('ceiling','50'),('purpose','generation')])
def test_unknown_approval_cannot_become_ceiling_or_ordinary_generation(workflow,field,value):
    root,inputs,_,tmp,tool,_=workflow
    path=root/'artifacts/unknown_cost_authorization-original.json'
    authority=read(path);authority[field]=value;write(path,authority)
    with pytest.raises(Exception):tool.execute(inputs)
    assert submissions(tmp)==[]
    assert not (root/'production_attempts').exists()


def test_reference_pin_rejected_before_reservation_or_upload(workflow):
    root,inputs,_,tmp,tool,_=workflow
    inputs=dict(inputs,last_frame='synthetic forbidden pin')
    with pytest.raises(Exception):tool.execute(inputs)
    assert submissions(tmp)==[]
    assert not (root/'production_attempts').exists()
    assert not any(read_line[:1]==['upload'] for read_line in map(json.loads,(tmp/'calls').read_text().splitlines()))


@pytest.mark.parametrize('code',['insufficient_credit','unavailable_plan','transient_error'])
def test_provider_refusal_stops_original_and_ambiguous_error_holds(workflow,monkeypatch,code):
    root,inputs,_,tmp,tool,packet=workflow
    monkeypatch.setenv('FAKE_REFUSAL',code)
    result=tool.execute(inputs)
    attempt=result.data['production_attempt_id']
    assert submissions(tmp)==['fixture-original']
    row=dispatch.ledger().inspect_unpriced(attempt)
    assert row['billing_state']=='unknown' and row['billed_amount']==''
    if code in {'insufficient_credit','unavailable_plan'}:
        assert row['slot_state']=='closed' and row['release_kind']=='inactive_unidentified'
        assert jobs.recover_launch(attempt)['status']=='provider_refused'
    else:
        assert row['slot_state']=='uncertain' and not row['release_kind']
        assert jobs.recover_launch(attempt)['status']=='hold_unknown_job'
    assert not Path(inputs['output_path']).exists()
    with pytest.raises(Exception):tool.execute(copy.deepcopy(inputs))
    assert submissions(tmp)==['fixture-original']
