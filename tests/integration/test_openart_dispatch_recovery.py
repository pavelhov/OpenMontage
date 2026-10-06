"""U4 offline dispatch crash proofs; no real provider operations."""
import json
import pytest
from lib import openart_dispatch as dispatch
from tools.video.openart_cli_video import OpenArtCLIVideo


def test_default_lookup_is_real_and_missing_attempt_fails_closed(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(tmp_path/'private'))
    with pytest.raises(Exception): dispatch.reservation_lookup(tmp_path/'project','missing','a'*64)


def test_adapter_offline_missing_quote_is_explicit_and_zero_calls(monkeypatch):
    monkeypatch.setattr(dispatch,'offline_readiness',lambda inputs:{'credit_state':'quote_required','provider_calls':0,'reservations':0})
    result=OpenArtCLIVideo().prepare_offline({}, {})
    assert result['credit_state']=='quote_required'
    assert result['provider_calls']==result['reservations']==0

import copy
import hashlib
import os
import sys
from pathlib import Path
from lib import openart_credit as credit, openart_jobs as jobs, openart_setup as setup
from lib import production_execution as execution, production_request as preparation
from tools import _openart_cli as cli
from tests.lib.test_production_request import package as compiler_package, write
from tests.lib.test_openart_credit import PATHS

FAKE=r'''#!PYTHON
import hashlib,json,os,sys,uuid,time
args=sys.argv[1:-2]
with open(os.environ['FAKE_LOG'],'a') as f:f.write(json.dumps(args)+'\n')
server=os.environ['FAKE_SERVER']
if args==['version']:out={'version':os.environ.get('FAKE_VERSION','1')}
elif args==['account']:out={'id':'account','tier':'turbo','workspace':'workspace','balance':'17.75' if os.path.exists(server) else '20','upload':{'free':True,'noDelayed':True}}
elif args[:2]==['model','form']:out={'type':'object','properties':{'prompt':{'type':'string'},'duration':{'type':'integer','default':8},'image':{'type':'string'},'aspectRatio':{'type':'string'},'resolution':{'type':'string'}},'required':['prompt','image']}
elif args[:2]==['model','cost']:out={'model':'m1','mode':'image2video','credits':'2.25','quantum':'0.25','maximum':True,'all_settings':True}
elif args[:2]==['upload','add']:out={'url':'https://up.openart.test/start.svg?sig=private','upload':{'free':True,'noDelayed':True}}
elif args[:2]==['creation','get']:
 history=json.load(open(server));record={'job':args[2],'native':history['jobs'][args[2]]};out={'creation':{'id':record['job'],'status':os.environ.get('FAKE_STATUS','failed'),'urls':['https://cdn.openart.test/result.mp4'] if os.environ.get('FAKE_STATUS')=='done' else []},'billing':{'account':'account','workspace':'workspace','native':record['native'],'job':record['job'],'amount':os.environ.get('FAKE_CHARGE','2.25'),'quantum':'0.25','final':True,'per_job_authoritative':True,'refund_authoritative':True,'debit_id':'debit-'+record['job'],'refund':os.environ.get('FAKE_REFUND'),'refund_id':'refund-'+record['job'],'refund_of':'debit-'+record['job']}}
elif args[:2]==['generate','video']:
 params={'prompt':args[2]}
 for flag,key in [('--duration','duration'),('--aspect-ratio','aspectRatio'),('--resolution','resolution'),('--image','image')]:
  if flag in args:params[key]=int(args[args.index(flag)+1]) if key=='duration' else args[args.index(flag)+1]
 body={'model':'m1','media':'video','mode':'image2video','params':params}
 if '--dry-run' in args:out={'endpoint':'POST /api/cli/v1/generate','body':body}
 else:
  job='job-'+uuid.uuid4().hex[:12]
  with open(os.environ['FAKE_PAID'],'a') as f:f.write(job+'\n')
  history=json.load(open(server))['jobs'] if os.path.exists(server) else {}
  native=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest();history[job]=native
  json.dump({'job':job,'native':native,'jobs':history},open(server,'w'))
  time.sleep(float(os.environ.get('FAKE_DELAY','0')))
  out={'job':{'id':job}}
else:raise SystemExit(7)
print(json.dumps(out))
'''


@pytest.fixture
def governed(tmp_path,monkeypatch):
    original_run=cli._run_checked; original_profile=jobs.load_qualification
    root,inputs,native,profile,compiled,review=compiler_package.__wrapped__(tmp_path,monkeypatch)
    monkeypatch.setattr(cli,'_run_checked',original_run);monkeypatch.setattr(jobs,'load_qualification',original_profile)
    binary=tmp_path/'fake';binary.write_text(FAKE.replace('PYTHON',sys.executable));binary.chmod(0o755)
    monkeypatch.setenv('OPENART_CLI_PATH',str(binary));monkeypatch.setenv('FAKE_LOG',str(tmp_path/'calls'))
    monkeypatch.setenv('FAKE_SERVER',str(tmp_path/'server'));monkeypatch.setenv('FAKE_PAID',str(tmp_path/'paid'))
    monkeypatch.delenv('OPENMONTAGE_OPENART_OFFLINE',raising=False)
    jobs.register_reservation_lookup(None)
    monkeypatch.setattr(jobs,'_UPLOAD_APPROVAL_LOOKUP',preparation.approved_upload_lookup)
    monkeypatch.setattr(jobs,'_ACCOUNT_CHECK',None)
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',execution._compiled_request_check)
    setup.inspect_qualification('m1','image2video',json_paths=PATHS)
    guarantee={'argv':['account'],'nonspending':{'path':'upload.free','expected':True},'no_delayed_charge':{'path':'upload.noDelayed','expected':True}}
    setup.qualify_upload_guarantee('m1','image2video',guarantee=guarantee,json_paths={'upload_url':'url'},url_hosts=['up.openart.test'])
    packet=preparation.source_packet(root,'entry'); source=Path(inputs['image_path'])
    uploadapproval={'version':'1.0','upload_id':'real-up','asset_id':'start','shot_id':'entry','source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'source_binding':packet['binding'],
        'approved_by':'Synthetic fixture author; not a live approval','evidence_path':'approval.txt','evidence_sha256':hashlib.sha256((root/'approval.txt').read_bytes()).hexdigest()}
    write(root/'artifacts/upload_approval-real-up.json',uploadapproval)
    jobs.upload_reference(root,'real-up',source,model='m1',mode='image2video')
    setup.qualify_preview('m1','image2video',prompt=inputs['prompt'],duration=8,aspect_ratio='16:9',resolution='720p',image_upload_id='real-up')
    profile=jobs.load_qualification(model='m1',mode='image2video',require='pre_submit')
    dr=next(e for e in profile['captured_receipts'] if e['kind']=='dry_run')
    inputs.update(model='m1',image_upload_id='real-up',native_dry_run_receipt_id=dr['receipt_id'],native_dry_run_receipt_sha256=dr['receipt_sha256'])
    native=jobs.prepare_native_request(execution._openart_controls(inputs),profile)
    compiled=preparation.prepare_compiled_request(inputs,native,profile,coverage=compiled['coverage'],timing=compiled['timing'])
    review.update(subject_sha256=preparation.digest(compiled),evidence_kind='reviewed',reviewer='Synthetic software-test fixture; no real AV review')
    write(root/'artifacts/compiled_request-c1.json',compiled);write(root/'artifacts/preparation_review-r1.json',review)
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=root)
    contract=credit.QuoteContract('credits','quantum','balance','id','workspace','model','mode','maximum','all_settings')
    qcontract=credit.qualify_quote_contract(inputs,profile,contract)
    quote=credit.refresh_credit_evidence(inputs,profile,qualification_sha256=qcontract['qualification_sha256'])
    a={'version':'1','status':'approved','approved_by':'Synthetic software-test fixture; no live credit authorization',
       'project_root':str(root),'project_id':scope['project_id'],'story_revision':scope['story_revision'],'scope_id':scope['id'],'shot_id':'entry','account_id_sha256':profile['account_id_sha256'],'workspace':'workspace',
       'allowance_id':'budget','allowance':'10','ceiling':'3','count':1,'purpose':'result_contract_qualification',
       'occurrences':[{'id':'qualification-fixture','index':0,'request_sha256':scope['requests']['entry'],'native_sha256':native['native_body_sha256'],'profile_sha256':native['profile_sha256'],'quote_sha256':quote['quote_sha256']}]}
    raw=json.dumps({'kind':'openart_credit_authorization','terms':a},sort_keys=True).encode();(root/'credit-approval.json').write_bytes(raw)
    a['evidence']={'path':'credit-approval.json','sha256':hashlib.sha256(raw).hexdigest()}
    scope['credit_authorization_sha256']=credit.credit_authorization_digest(a)
    write(root/'artifacts/credit_authorization-credit.json',a);write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    inputs.update(credit_authorization_id='credit',credit_quote_id=quote['quote_id'],credit_qualification_sha256=qcontract['qualification_sha256'])
    yield root,inputs,profile,tmp_path
    dispatch._CRASH_HOOK=None;jobs.register_reservation_lookup(None)


def test_real_compiler_quote_ledger_popen_chain_submits_once(governed):
    root,inputs,profile,tmp=governed
    assert preparation.validate_preparation(inputs,jobs.prepare_native_request(execution._openart_controls(inputs),profile),profile)
    result=OpenArtCLIVideo().execute(inputs)
    assert len((tmp/'paid').read_text().splitlines())==1
    attempt=result.data['production_attempt_id']
    assert (root/'production_attempts'/attempt/'request.json').exists()
    assert dispatch.ledger().inspect(attempt)['slot_state'] in {'submitting','uncertain'}
    with pytest.raises(Exception):OpenArtCLIVideo().execute(inputs)
    assert len((tmp/'paid').read_text().splitlines())==1


@pytest.mark.parametrize('stage,expected',[
    ('private_prepared',None),('ledger_prepared','prepared'),('journal_ready','ready'),
    ('ledger_submitting','submitting'),('current_prelaunch','submitting'),('launch_marker','submitting')])
def test_original_boundary_crash_is_durable_and_never_resubmits(governed,stage,expected):
    root,inputs,profile,tmp=governed
    seen=[]
    def crash(current,attempt):
        if current==stage:
            seen.append(attempt)
            raise RuntimeError('synthetic crash boundary')
    dispatch._CRASH_HOOK=crash
    with pytest.raises(RuntimeError,match='synthetic crash'):OpenArtCLIVideo().execute(inputs)
    dispatch._CRASH_HOOK=None
    attempt=seen[0]
    assert not (tmp/'paid').exists()
    assert (jobs.job_dir(attempt)/'credit_dispatch.json').exists()
    if expected is None:
        with pytest.raises(Exception):dispatch.ledger().inspect(attempt)
    else:
        assert dispatch.ledger().inspect(attempt)['slot_state']==expected
        repaired=dispatch.repair_outbox(root,attempt)
        assert repaired['slot_state']==('ready' if expected=='prepared' else expected)
        assert (root/'production_attempts'/attempt/'request.json').exists()
        assert (root/'production_attempts'/attempt/'credit_events').is_dir()
        with pytest.raises(Exception):OpenArtCLIVideo().execute(inputs)
        assert not (tmp/'paid').exists()


def test_qualified_original_billing_refund_positive_and_idempotent(governed,monkeypatch):
    root,inputs,profile,tmp=governed
    result=OpenArtCLIVideo().execute(inputs);attempt=result.data['production_attempt_id']
    monkeypatch.setenv('FAKE_STATUS','done')
    jobs.promote_result_contract(attempt,json_paths={'url_hosts':['cdn.openart.test']})
    monkeypatch.setenv('FAKE_STATUS','failed');monkeypatch.setenv('FAKE_REFUND','0.25')
    contract=dispatch.BillingContract('billing.account','billing.workspace','billing.native','billing.job',
        'billing.amount','billing.quantum','billing.final','billing.per_job_authoritative','billing.debit_id',
        'billing.refund','billing.refund_id','billing.refund_authoritative','billing.refund_of')
    proof=dispatch.qualify_resolution_contract(root,attempt,dispatch._manifest(attempt)[1].request_sha256,contract)
    resolved=dispatch.resolve_attempt(root,attempt,dispatch._manifest(attempt)[1].request_sha256,billing_proof_id=proof['billing_proof_id'])
    row=dispatch.ledger().inspect(attempt)
    assert resolved['slot_state']=='terminal'
    assert row['charged_units']==9 and row['refunded_units']==1
    dispatch.resolve_attempt(root,attempt,dispatch._manifest(attempt)[1].request_sha256,billing_proof_id=proof['billing_proof_id'])
    assert dispatch.ledger().inspect(attempt)['refunded_units']==1
    assert len((tmp/'paid').read_text().splitlines())==1


@pytest.mark.parametrize('stage',['provider_acceptance','parsed_job'])
def test_post_acceptance_crash_preserves_original_and_one_submit(governed,stage):
    root,inputs,profile,tmp=governed;seen=[]
    def crash(current,attempt):
        if current==stage:seen.append(attempt);raise RuntimeError('synthetic accepted crash')
    dispatch._CRASH_HOOK=crash
    with pytest.raises(RuntimeError):OpenArtCLIVideo().execute(inputs)
    dispatch._CRASH_HOOK=None;attempt=seen[0]
    assert len((tmp/'paid').read_text().splitlines())==1
    assert dispatch.ledger().inspect(attempt)['slot_state']=='submitting'
    jobs.recover_launch(attempt);dispatch.record_launch_result(attempt)
    assert dispatch.ledger().inspect(attempt)['slot_state']=='uncertain'
    dispatch.repair_outbox(root,attempt)
    with pytest.raises(Exception):OpenArtCLIVideo().execute(inputs)
    assert len((tmp/'paid').read_text().splitlines())==1


def _die_after_spawn(inputs):
    jobs.process_identity=lambda pid:None
    def die(stage,attempt):
        if stage=='spawned':os._exit(73)
    dispatch._CRASH_HOOK=die
    OpenArtCLIVideo().execute(inputs)


def test_parent_death_live_child_missing_birth_keeps_original_hold(governed,monkeypatch):
    import multiprocessing as mp,time
    root,inputs,profile,tmp=governed
    monkeypatch.setenv('FAKE_DELAY','2')
    proc=mp.get_context('spawn').Process(target=_die_after_spawn,args=(inputs,));proc.start();proc.join(timeout=15)
    assert proc.exitcode==73
    attempt=next((root/'production_attempts').iterdir()).name
    deadline=time.monotonic()+5
    while not (tmp/'paid').exists() and time.monotonic()<deadline:time.sleep(.01)
    assert len((tmp/'paid').read_text().splitlines())==1
    process=jobs.original_process_state(attempt)
    assert process['state']=='unknown'
    assert dispatch.ledger().inspect(attempt)['slot_state']=='submitting'
    time.sleep(2.2)
    jobs.recover_launch(attempt);dispatch.record_launch_result(attempt)
    assert dispatch.ledger().inspect(attempt)['slot_state']=='uncertain'
    assert dispatch.ledger().inspect(attempt)['debit_state'] in {'reserved','unresolved'}
    assert jobs.original_process_state(attempt)['state']=='unknown'


def _clone_authorized_project(root,inputs,profile,target):
    import shutil
    shutil.copytree(root,target,ignore=shutil.ignore_patterns('production_attempts'))
    def rebase(value):
        if isinstance(value,str):return value.replace(str(root),str(target))
        if isinstance(value,list):return [rebase(v) for v in value]
        if isinstance(value,dict):return {k:rebase(v) for k,v in value.items()}
        return value
    other=rebase(inputs)
    compiled=json.loads((target/'artifacts/compiled_request-c1.json').read_text())
    native=jobs.prepare_native_request(execution._openart_controls(other),profile)
    compiled=preparation.prepare_compiled_request(other,native,profile,coverage=compiled['coverage'],timing=compiled['timing'])
    review=json.loads((target/'artifacts/preparation_review-r1.json').read_text());review['subject_sha256']=preparation.digest(compiled)
    write(target/'artifacts/compiled_request-c1.json',compiled);write(target/'artifacts/preparation_review-r1.json',review)
    scope=json.loads((target/'production_scopes.json').read_text())['scopes'][0]
    scope['requests']['entry']=execution.planned_request_digest(other,project_dir=target)
    auth=json.loads((target/'artifacts/credit_authorization-credit.json').read_text())
    auth['project_root']=str(target);auth['occurrences'][0]['request_sha256']=scope['requests']['entry']
    auth['occurrences'][0]['id']='second-project-synthetic-occurrence'
    terms={k:v for k,v in auth.items() if k!='evidence'}
    raw=json.dumps({'kind':'openart_credit_authorization','terms':terms},sort_keys=True).encode()
    (target/'credit-approval.json').write_bytes(raw);auth['evidence']['sha256']=hashlib.sha256(raw).hexdigest()
    scope['credit_authorization_sha256']=credit.credit_authorization_digest(auth)
    write(target/'artifacts/credit_authorization-credit.json',auth);write(target/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    execution.preflight(OpenArtCLIVideo(),other)
    return other


def _actual_dispatch_race(inputs,barrier,queue):
    def checkpoint(stage,attempt):
        if stage=='private_prepared':barrier.wait(timeout=20)
    dispatch._CRASH_HOOK=checkpoint
    try:
        result=OpenArtCLIVideo().execute(inputs);queue.put(('submitted',result.data['production_attempt_id']))
    except Exception as exc:queue.put(('blocked',type(exc).__name__,str(exc)))


def test_spawn_barrier_two_actual_projects_share_one_paid_account_claim(governed):
    import multiprocessing as mp
    root,inputs,profile,tmp=governed
    other=_clone_authorized_project(root,inputs,profile,tmp/'other-project')
    ctx=mp.get_context('spawn');barrier=ctx.Barrier(2);queue=ctx.Queue()
    ps=[ctx.Process(target=_actual_dispatch_race,args=(value,barrier,queue)) for value in [inputs,other]]
    for proc in ps:proc.start()
    results=[queue.get(timeout=30) for _ in ps]
    for proc in ps:proc.join(timeout=10);assert proc.exitcode==0
    assert sorted(r[0] for r in results)==['blocked','submitted'],results
    assert len((tmp/'paid').read_text().splitlines())==1
    assert len(list((root/'production_attempts').glob('*/request.json')))+len(list((tmp/'other-project'/'production_attempts').glob('*/request.json')))==1


def _qualified_billing_origin(governed,monkeypatch):
    root,inputs,profile,tmp=governed
    result=OpenArtCLIVideo().execute(inputs);attempt=result.data['production_attempt_id']
    monkeypatch.setenv('FAKE_STATUS','done')
    jobs.promote_result_contract(attempt,json_paths={'url_hosts':['cdn.openart.test']})
    monkeypatch.setenv('FAKE_STATUS','failed')
    contract=dispatch.BillingContract('billing.account','billing.workspace','billing.native','billing.job',
        'billing.amount','billing.quantum','billing.final','billing.per_job_authoritative','billing.debit_id',
        'billing.refund','billing.refund_id','billing.refund_authoritative','billing.refund_of')
    return root,inputs,profile,tmp,attempt,dispatch._manifest(attempt)[1],contract


@pytest.mark.parametrize('changed',['account','workspace','native','job','guarantee','amount','quantum','refund_debit'])
def test_fresh_billing_mismatch_cannot_settle_original_hold(governed,monkeypatch,changed):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    proof=dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    binary=Path(os.environ['OPENART_CLI_PATH']);text=binary.read_text()
    changes={'account':("'account':'account'","'account':'wrong'"),
        'workspace':("'workspace':'workspace'","'workspace':'wrong'"),
        'native':("'native':record['native']","'native':'0'*64"),
        'job':("'job':record['job']","'job':'different'"),
        'guarantee':("'per_job_authoritative':True","'per_job_authoritative':False"),
        'amount':("'FAKE_CHARGE','2.25'","'FAKE_CHARGE','2.251'"),
        'quantum':("'quantum':'0.25'","'quantum':'0.5'"),
        'refund_debit':("'refund_of':'debit-'+record['job']","'refund_of':'wrong'")}
    old,new=changes[changed];assert old in text;binary.write_text(text.replace(old,new));monkeypatch.setenv('FAKE_REFUND','0.25')
    with pytest.raises(Exception):dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    row=dispatch.ledger().inspect(attempt)
    assert row['charged_units']==0 and row['refunded_units']==0
    assert row['debit_state'] in {'reserved','unresolved'}
    assert len((tmp/'paid').read_text().splitlines())==1


def test_terminal_unknown_billing_releases_slot_only_and_allows_budgeted_next_job(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    other=_clone_authorized_project(root,inputs,profile,tmp/'next-project')
    result=dispatch.resolve_attempt(root,attempt,b.request_sha256)
    assert result['resolution']=='terminal_unknown_billing'
    row=dispatch.ledger().inspect(attempt)
    assert row['slot_state']=='terminal' and row['debit_state']=='unresolved' and row['reserved_units']==9
    OpenArtCLIVideo().execute(other)
    assert len((tmp/'paid').read_text().splitlines())==2


def test_retained_billing_contract_tamper_and_caller_only_contract_cannot_settle(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    with pytest.raises(TypeError):dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_contract=contract)
    proof=dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    path=dispatch._billing_path(proof['billing_proof_id']);value=json.loads(path.read_bytes());value['contract']['amount']='billing.quantum';path.write_text(json.dumps(value))
    with pytest.raises(Exception):dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    assert dispatch.ledger().inspect(attempt)['charged_units']==0


@pytest.mark.parametrize('retained',[True,False])
def test_normal_strict_base_tool_offline_hook_has_zero_cli_and_ledger_calls(governed,monkeypatch,retained):
    root,inputs,profile,tmp=governed
    if not retained:inputs={k:v for k,v in inputs.items() if k!='credit_quote_id'}
    def forbidden(*args,**kwargs):raise AssertionError('offline hook invoked transport/ledger')
    monkeypatch.setattr(cli,'run_readonly',forbidden);monkeypatch.setattr(cli,'_run_checked',forbidden)
    monkeypatch.setattr(dispatch,'ledger',forbidden)
    result=OpenArtCLIVideo().dry_run(inputs)
    assert result['provider_calls']==result['reservations']==0
    assert result['offline_preparation']['credit_state']==('retained_quote' if retained else 'quote_required')
    assert not (tmp/'paid').exists() and not (root/'production_attempts').exists()


@pytest.mark.parametrize('change',['amount','quantum','guarantee','version','tier','preview'])
def test_final_held_transport_refresh_rejects_stale_evidence_before_popen(governed,monkeypatch,change):
    root,inputs,profile,tmp=governed;seen=[]
    def changed(stage,attempt):
        if stage!='ledger_submitting':return
        seen.append(attempt)
        if change=='version':monkeypatch.setenv('FAKE_VERSION','2');return
        path=Path(os.environ['OPENART_CLI_PATH']);text=path.read_text()
        old,new={'amount':("'credits':'2.25'","'credits':'2.50'"),
            'quantum':("'quantum':'0.25'","'quantum':'0.5'"),
            'guarantee':("'maximum':True","'maximum':False"),
            'tier':("'tier':'turbo'","'tier':'different'"),
            'preview':("params={'prompt':args[2]}","params={'prompt':args[2]+' changed'}")}[change]
        assert old in text;path.write_text(text.replace(old,new))
    dispatch._CRASH_HOOK=changed
    with pytest.raises(Exception):OpenArtCLIVideo().execute(inputs)
    assert not (tmp/'paid').exists()
    attempt=seen[0];row=dispatch.ledger().inspect(attempt)
    assert row['slot_state']=='submitting' and row['debit_state']=='unresolved'
    assert (root/'production_attempts'/attempt/'request.json').exists()
    assert not (jobs.job_dir(attempt)/'launch.json').exists()


def test_all_provider_calls_outside_project_and_ledger_locks_and_one_deadline(governed,monkeypatch):
    import contextlib,time
    root,inputs,profile,tmp=governed;locks={'project':0,'ledger':0};budgets=[]
    original_lock=execution._lock;original_transaction=dispatch.CreditLedger._transaction
    @contextlib.contextmanager
    def project_lock(path):
        with original_lock(path):
            locks['project']+=1
            try:yield
            finally:locks['project']-=1
    @contextlib.contextmanager
    def ledger_transaction(self):
        with original_transaction(self) as db:
            locks['ledger']+=1
            try:yield db
            finally:locks['ledger']-=1
    original_read=cli.run_readonly;original_spawn=jobs._popen
    def readonly(*args,**kwargs):
        assert locks=={'project':0,'ledger':0}
        budgets.append((time.monotonic(),kwargs['timeout']))
        return original_read(*args,**kwargs)
    def popen(*args,**kwargs):
        assert locks=={'project':0,'ledger':0}
        assert cli.held_lock_fd()>=0
        return original_spawn(*args,**kwargs)
    monkeypatch.setattr(execution,'_lock',project_lock);monkeypatch.setattr(dispatch.CreditLedger,'_transaction',ledger_transaction)
    monkeypatch.setattr(cli,'run_readonly',readonly);monkeypatch.setattr(jobs,'_popen',popen)
    OpenArtCLIVideo().execute(inputs)
    assert len((tmp/'paid').read_text().splitlines())==1
    # Nested helpers may shorten but never reset the shared absolute deadline.
    assert max(t+budget for t,budget in budgets)-min(t+budget for t,budget in budgets)<0.05


def test_settlement_crash_replays_original_refund_without_new_popen(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    monkeypatch.setenv('FAKE_REFUND','0.25')
    proof=dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    def crash(stage,aid):
        if stage=='ledger_settled':raise RuntimeError('synthetic settled crash')
    dispatch._CRASH_HOOK=crash
    with pytest.raises(RuntimeError):dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    dispatch._CRASH_HOOK=None
    row=dispatch.ledger().inspect(attempt)
    assert row['slot_state']=='terminal' and row['charged_units']==9 and row['refunded_units']==0
    assert (root/'production_attempts'/attempt/'request.json').exists()
    dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    row=dispatch.ledger().inspect(attempt)
    assert row['charged_units']==9 and row['refunded_units']==1
    assert not [e for e in dispatch.ledger().outbox() if e['attempt_id']==attempt]
    assert len((tmp/'paid').read_text().splitlines())==1


def test_actual_overquote_full_debit_quarantines_account_across_projects(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    monkeypatch.setenv('FAKE_CHARGE','4.00')
    proof=dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    row=dispatch.ledger().inspect(attempt)
    assert row['charged_units']==16 and row['violation']==1 and row['slot_state']=='terminal'
    other=_clone_authorized_project(root,inputs,profile,tmp/'quarantined-project')
    with pytest.raises(Exception):OpenArtCLIVideo().execute(other)
    assert len((tmp/'paid').read_text().splitlines())==1


@pytest.mark.parametrize('mutation',['delete','tamper'])
def test_retained_billing_raw_capture_missing_or_changed_keeps_debit_hold(governed,monkeypatch,mutation):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    proof=dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    value=json.loads(dispatch._billing_path(proof['billing_proof_id']).read_bytes())
    entry=value['observed']['status_receipt'];record=json.loads(cli.receipt_path(entry['receipt_id']).read_bytes())
    stream=cli.state_dir()/'streams'/record['streams']['stdout']
    if mutation=='delete':stream.unlink()
    else:stream.write_bytes(b'{"billing":{"amount":"0"}}')
    with pytest.raises(Exception):dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    assert dispatch.ledger().inspect(attempt)['charged_units']==0
    assert len((tmp/'paid').read_text().splitlines())==1


def test_agent_accessible_account_qualified_billing_capture_and_resolution(governed,monkeypatch):
    from tools.openart_account import OpenArtAccount
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    common={'read_only':True,'project_dir':str(root),'attempt_id':attempt,'request_sha256':b.request_sha256}
    captured=OpenArtAccount().execute({**common,'action':'qualify_resolution_contract','billing_contract':dispatch.asdict(contract)})
    assert captured.success,captured
    proof=captured.data['evidence']['billing_proof_id']
    resolved=OpenArtAccount().execute({**common,'action':'resolve_attempt','billing_proof_id':proof})
    assert resolved.success,resolved
    assert dispatch.ledger().inspect(attempt)['charged_units']==9
    assert len((tmp/'paid').read_text().splitlines())==1


def test_billing_axis_can_settle_while_original_generation_slot_remains_active(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    proof=dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    monkeypatch.setenv('FAKE_STATUS','running')
    result=dispatch.resolve_attempt(root,attempt,b.request_sha256,billing_proof_id=proof['billing_proof_id'])
    row=dispatch.ledger().inspect(attempt)
    assert result['resolution']=='active_hold'
    assert row['slot_state']=='submitted' and row['debit_state']=='settled' and row['charged_units']==9
    with pytest.raises(Exception):OpenArtCLIVideo().execute(_clone_authorized_project(root,inputs,profile,tmp/'active-next'))
    assert len((tmp/'paid').read_text().splitlines())==1


def _authorize_exact_same_project_repair(root,inputs,profile,attempt):
    other=copy.deepcopy(inputs);other['governance']['scope_id']='repair';other['output_path']=str(root/'repair.mp4')
    other.update(compiled_request_id='c2',preparation_review_id='r2',credit_authorization_id='repair')
    old=json.loads((root/'artifacts/compiled_request-c1.json').read_text())
    native=jobs.prepare_native_request(execution._openart_controls(other),profile)
    compiled=preparation.prepare_compiled_request(other,native,profile,coverage=old['coverage'],timing=old['timing'])
    review=json.loads((root/'artifacts/preparation_review-r1.json').read_text());review.update(review_id='r2',subject_sha256=preparation.digest(compiled))
    write(root/'artifacts/compiled_request-c2.json',compiled);write(root/'artifacts/preparation_review-r2.json',review)
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope.update(id='repair',phase='repair',replaces_attempt_ids=[attempt])
    scope['requests']['entry']=execution.planned_request_digest(other,project_dir=root)
    auth=json.loads((root/'artifacts/credit_authorization-credit.json').read_text());auth.update(scope_id='repair',purpose='generation')
    auth['occurrences'][0].update(id='exact-repair-synthetic-approval',request_sha256=scope['requests']['entry'])
    terms={k:v for k,v in auth.items() if k!='evidence'};raw=json.dumps({'kind':'openart_credit_authorization','terms':terms},sort_keys=True).encode()
    (root/'repair-credit-approval.json').write_bytes(raw);auth['evidence']={'path':'repair-credit-approval.json','sha256':hashlib.sha256(raw).hexdigest()}
    scope['credit_authorization_sha256']=credit.credit_authorization_digest(auth)
    write(root/'artifacts/credit_authorization-repair.json',auth);write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    return other


def test_same_project_qualified_terminal_is_unselected_exact_repair_can_dispatch_and_collect_original(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    other=_authorize_exact_same_project_repair(root,inputs,profile,attempt)
    with pytest.raises(Exception,match='uncertain'):execution.preflight(OpenArtCLIVideo(),other)
    dispatch.resolve_attempt(root,attempt,b.request_sha256)
    state=execution._state(root,{'attempt_id':attempt})
    assert state['status']=='terminal_unselected' and state['output'] is None and state['result'] is None
    assert dispatch.ledger().inspect(attempt)['debit_state']=='unresolved'
    with pytest.raises(Exception):execution.record_selection(root,'entry',{'attempt_id':attempt,'output':None})
    assert not (root/'production_attempts'/attempt/'reconciliation.json').exists()
    # The new exact repair can dispatch using terminal_unselected, before collection.
    OpenArtCLIVideo().execute(other)
    assert len((tmp/'paid').read_text().splitlines())==2
    # Original failure collection remains legal and never submits generation.
    collected=execution.collect_openart_attempt(root,attempt,request_sha256=b.request_sha256)
    assert collected['status']=='failed'
    assert len((tmp/'paid').read_text().splitlines())==2


@pytest.mark.parametrize('tamper',['raw_format','request','release_proof','unknown_process'])
def test_terminal_fallback_requires_immutable_ready_journal_and_qualified_original_slot(governed,monkeypatch,tamper):
    import sqlite3
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    other=_authorize_exact_same_project_repair(root,inputs,profile,attempt)
    if tamper=='unknown_process':monkeypatch.setattr(jobs,'original_process_state',lambda aid:{'attempt_id':aid,'state':'unknown','pid':123,'returncode':None})
    dispatch.resolve_attempt(root,attempt,b.request_sha256)
    path=root/'production_attempts'/attempt/'request.json'
    if tamper in {'raw_format','request'}:path.chmod(0o600)
    if tamper=='raw_format':path.write_bytes(path.read_bytes()+b'\n')
    elif tamper=='request':
        value=json.loads(path.read_bytes());value['request_sha256']='0'*64;path.write_text(json.dumps(value))
    elif tamper=='release_proof':
        db=sqlite3.connect(dispatch.ledger().path);db.execute("UPDATE reservations SET release_json='' WHERE attempt_id=?",(attempt,));db.commit();db.close()
    assert execution._state(root,{'attempt_id':attempt})['status']=='uncertain'
    with pytest.raises(Exception):execution.preflight(OpenArtCLIVideo(),other)
    assert len((tmp/'paid').read_text().splitlines())==1


def test_real_nested_governed_selector_reuses_one_original_credit_reservation(governed,monkeypatch):
    from tools.base_tool import BaseTool,ToolTier
    from lib.provider_credit_ledger import read_existing_snapshot
    root,inputs,profile,tmp=governed
    inputs=copy.deepcopy(inputs);inputs.update(preferred_provider='openart_cli',allowed_providers=['openart_cli'])
    compiled=json.loads((root/'artifacts/compiled_request-c1.json').read_text())
    native=jobs.prepare_native_request(execution._openart_controls(inputs),profile)
    compiled=preparation.prepare_compiled_request(inputs,native,profile,coverage=compiled['coverage'],timing=compiled['timing'])
    review=json.loads((root/'artifacts/preparation_review-r1.json').read_text());review['subject_sha256']=preparation.digest(compiled)
    write(root/'artifacts/compiled_request-c1.json',compiled);write(root/'artifacts/preparation_review-r1.json',review)
    scope=json.loads((root/'production_scopes.json').read_text())['scopes'][0]
    scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=root)
    auth=json.loads((root/'artifacts/credit_authorization-credit.json').read_text());auth['occurrences'][0]['request_sha256']=scope['requests']['entry']
    terms={k:v for k,v in auth.items() if k!='evidence'};raw=json.dumps({'kind':'openart_credit_authorization','terms':terms},sort_keys=True).encode()
    (root/'credit-approval.json').write_bytes(raw);auth['evidence']['sha256']=hashlib.sha256(raw).hexdigest()
    scope['credit_authorization_sha256']=credit.credit_authorization_digest(auth)
    write(root/'artifacts/credit_authorization-credit.json',auth);write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    class GovernedSelector(BaseTool):
        name='video_selector';provider='selector';capability='video_generation';tier=ToolTier.GENERATE
        def execute(self,submitted):
            nested=dict(submitted);nested.pop('preferred_provider');nested.pop('allowed_providers')
            return OpenArtCLIVideo().execute(nested)
    reserves=[];original=dispatch.CreditLedger.reserve_prepared
    def reserve(self,packet):reserves.append(packet.binding.attempt_id);return original(self,packet)
    monkeypatch.setattr(dispatch.CreditLedger,'reserve_prepared',reserve)
    result=GovernedSelector().execute(inputs);attempt=result.data['production_attempt_id']
    snap=read_existing_snapshot()
    assert reserves==[attempt] and len(snap['reservations'])==1 and len(snap['ready_journals'])==1
    assert len((tmp/'paid').read_text().splitlines())==1
    assert json.loads((root/'production_attempts'/attempt/'request.json').read_text())['tool_name']=='video_selector'


def test_changed_public_ready_request_before_prelaunch_refuses_popen(governed):
    root,inputs,profile,tmp=governed;seen=[]
    def tamper(stage,attempt):
        if stage=='ledger_submitting':
            seen.append(attempt);path=root/'production_attempts'/attempt/'request.json';path.chmod(0o600)
            path.write_bytes(path.read_bytes()+b'\n')
    dispatch._CRASH_HOOK=tamper
    with pytest.raises(Exception):OpenArtCLIVideo().execute(inputs)
    assert seen and not (tmp/'paid').exists()
    assert dispatch.ledger().inspect(seen[0])['slot_state']=='submitting'


def test_other_valid_retained_quote_cannot_qualify_original_billing(governed,monkeypatch):
    root,inputs,profile,tmp,attempt,b,contract=_qualified_billing_origin(governed,monkeypatch)
    other=copy.deepcopy(inputs);other['prompt']+=' A different native request.'
    native=jobs.prepare_native_request(execution._openart_controls(inputs),profile)
    argv=list(native['creative_argv']);argv[2]=other['prompt']
    with cli.allow_image_reference(True):capture=cli.run_readonly(argv+['--dry-run'])
    other.update(native_dry_run_receipt_id=capture['receipt_id'],native_dry_run_receipt_sha256=capture['receipt_sha256'])
    quote_contract=credit.QuoteContract('credits','quantum','balance','id','workspace','model','mode','maximum','all_settings')
    qualified=credit.qualify_quote_contract(other,profile,quote_contract)
    quote=credit.refresh_credit_evidence(other,profile,qualification_sha256=qualified['qualification_sha256'])
    assert credit.get_retained_quote(other,profile,quote['quote_id'])['quote_sha256']!=b.quote_sha256
    path=dispatch._manifest_path(attempt);manifest=json.loads(path.read_bytes());manifest['quote_id']=quote['quote_id'];path.write_text(json.dumps(manifest))
    with pytest.raises(Exception):dispatch.qualify_resolution_contract(root,attempt,b.request_sha256,contract)
    assert dispatch.ledger().inspect(attempt)['charged_units']==0
    assert len((tmp/'paid').read_text().splitlines())==1


def test_explicit_launch_wait_timeout_caps_long_internal_deadline_before_popen(governed,monkeypatch):
    import time
    root,inputs,profile,tmp=governed
    original=jobs.launch_submit
    def short_launch(*args,**kwargs):
        kwargs['wait_timeout']=0.05
        kwargs['deadline']=time.monotonic()+30
        return original(*args,**kwargs)
    original_account=jobs._current_account
    def delayed_account(*args,**kwargs):
        time.sleep(.08)
        return original_account(*args,**kwargs)
    monkeypatch.setattr(jobs,'launch_submit',short_launch);monkeypatch.setattr(jobs,'_current_account',delayed_account)
    with pytest.raises(Exception):OpenArtCLIVideo().execute(inputs)
    assert not (tmp/'paid').exists()


@pytest.mark.parametrize('bad_deadline',[True,float('nan'),float('inf')])
def test_invalid_internal_launch_deadline_fails_before_any_popen(governed,monkeypatch,bad_deadline):
    root,inputs,profile,tmp=governed;original=jobs.launch_submit
    def invalid(*args,**kwargs):kwargs['deadline']=bad_deadline;return original(*args,**kwargs)
    monkeypatch.setattr(jobs,'launch_submit',invalid)
    with pytest.raises(Exception,match='finite internal absolute'):OpenArtCLIVideo().execute(inputs)
    assert not (tmp/'paid').exists()
