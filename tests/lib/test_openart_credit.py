"""Credit qualification uses executable read-only captures; never real OpenArt."""
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from lib import openart_credit as credit, openart_setup as setup, openart_jobs as jobs
from tools import _openart_cli as cli

FAKE=r'''#!PYTHON
import json,os,sys
args=[x for x in sys.argv[1:] if x not in ['--json','--no-input']]
with open(os.environ['FAKE_LOG'],'a') as f: f.write(json.dumps(args)+'\n')
if args==['version']: out={'version':os.environ.get('VERSION','1')}
elif args==['account']: out={'id':os.environ.get('ACCOUNT','account'),'tier':os.environ.get('TIER','turbo'),'balance':'20','workspace':None if os.environ.get('GLOBAL_ACCOUNT')=='true' else 'workspace','account_global':os.environ.get('GLOBAL_ACCOUNT')=='true'}
elif args[:2]==['model','form']: out={'properties':{'prompt':{'type':'string'},'duration':{'type':'integer','default':int(os.environ.get('DEFAULT','5'))}},'required':['prompt']}
elif args[:2]==['model','cost']: out={'model':'m1','mode':'text2video','credits':os.environ.get('AMOUNT','2.25'),'quantum':'0.25','maximum_per_generation':True,'covers_all_supported_settings':os.environ.get('QUALIFIED','true')=='true'}
elif args[:2]==['generate','video'] and '--dry-run' in args:
 params={'prompt':args[2]}
 if '--duration' in args: params['duration']=int(args[args.index('--duration')+1])
 out={'endpoint':'POST /api/cli/v1/generate','body':{'model':'m1','media':'video','mode':'text2video','params':params}}
else: raise SystemExit(7)
print(json.dumps(out))
'''
PATHS={'account_id':'id','account_tier':'tier','submit_job_id':'job.id','result_job_id':'creation.id','status':'creation.status','status_terminal_ok':'done','status_terminal_fail':'failed','urls':'creation.urls','url_hosts':['cdn.example.test']}

@pytest.fixture
def env(tmp_path,monkeypatch):
    binary=tmp_path/'fake'; binary.write_text(FAKE.replace('PYTHON',sys.executable)); binary.chmod(0o755)
    monkeypatch.setenv('OPENART_CLI_PATH',str(binary)); monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(tmp_path/'state'))
    log=tmp_path/'calls'; monkeypatch.setenv('FAKE_LOG',str(log)); monkeypatch.delenv('OPENMONTAGE_OPENART_OFFLINE',raising=False)
    setup.inspect_qualification('m1','text2video',json_paths=PATHS)
    setup.qualify_preview('m1','text2video',prompt='private prompt',duration=5)
    profile=jobs.load_qualification(model='m1',mode='text2video',require='pre_submit')
    dr=next(r for r in profile['captured_receipts'] if r['kind']=='dry_run')
    inputs={'prompt':'private prompt','model':'m1','duration':5,'native_dry_run_receipt_id':dr['receipt_id'],'native_dry_run_receipt_sha256':dr['receipt_sha256']}
    contract=credit.QuoteContract('credits','quantum','balance','id','workspace','model','mode','maximum_per_generation','covers_all_supported_settings')
    qualified=credit.qualify_quote_contract(inputs,profile,contract)
    (tmp_path/'qualification').write_text(qualified['qualification_sha256'])
    return tmp_path,log,profile,inputs,contract


def _refresh(inputs,profile,contract,**kwargs):
    qualified=(cli.state_dir().parent/'qualification').read_text()
    return credit.refresh_credit_evidence(inputs,profile,contract,qualification_sha256=qualified,**kwargs)


def test_qualified_refresh_retained_zero_call_readiness(env):
    root,log,profile,inputs,contract=env
    summary=_refresh(inputs,profile,contract)
    before=log.read_text()
    retained=credit.get_retained_quote(inputs,profile,summary['quote_id'])
    assert retained['quote_sha256']==summary['quote_sha256']
    assert retained['amount']=='2.25'
    assert log.read_text()==before
    assert 'private prompt' not in json.dumps(summary)
    assert all('--async' not in json.loads(x) for x in before.splitlines())
    fresh=_refresh(inputs,profile,contract,approved_quote_id=summary['quote_id'])
    assert fresh['quote_sha256']==summary['quote_sha256']
    assert fresh['quote_id']!=summary['quote_id']


def test_model_only_quote_is_unqualified(env,monkeypatch):
    root,log,profile,inputs,contract=env
    monkeypatch.setenv('QUALIFIED','false')
    with pytest.raises(cli.OpenArtCLIError): _refresh(inputs,profile,contract)


@pytest.mark.parametrize('key,value',[('AMOUNT','3'),('AMOUNT','NaN'),('TIER','other'),('DEFAULT','6'),('VERSION','2'),('ACCOUNT','other')])
def test_changed_current_terms_invalidate(env,monkeypatch,key,value):
    root,log,profile,inputs,contract=env
    approved=_refresh(inputs,profile,contract)
    monkeypatch.setenv(key,value)
    with pytest.raises(cli.OpenArtCLIError):
        _refresh(inputs,profile,contract,approved_quote_id=approved['quote_id'])
    assert all('--async' not in json.loads(x) for x in log.read_text().splitlines())


def test_changed_settings_and_quote_required_offline(env):
    root,log,profile,inputs,contract=env
    before=log.read_text()
    assert credit.get_retained_quote(inputs,profile,None)['credit_state']=='quote_required'
    assert log.read_text()==before
    quote=_refresh(inputs,profile,contract)
    with pytest.raises(cli.OpenArtCLIError): credit.get_retained_quote(dict(inputs,duration=6),profile,quote['quote_id'])


def authorization(env):
    from lib import production_execution as execution
    root,log,profile,inputs,contract=env
    project=root/'project'; project.mkdir(); (project/'ordinary.txt').write_text('approve exact tool request')
    inputs=dict(inputs,output_path=str(project/'v.mp4'),governance={'scope_id':'scope','shot_id':'shot'})
    digest=execution.planned_request_digest(inputs,project_dir=project)
    scope={'id':'scope','provider':'openart_cli','status':'approved','approved_by':'human','project_id':'project','story_revision':'story',
        'evidence':{'path':'ordinary.txt','sha256':hashlib.sha256((project/'ordinary.txt').read_bytes()).hexdigest()},
        'requests':{'shot':digest},'attempts_per_shot':{'shot':1}}
    marker={'project_id':'project','story_revision':'story'}
    quote=_refresh(inputs,profile,contract); native=jobs.prepare_native_request(credit._controls(inputs),profile)
    a={'version':'1','status':'approved','approved_by':'human','project_root':str(project),'project_id':'project','story_revision':'story','scope_id':'scope','shot_id':'shot',
       'account_id_sha256':native['account_id_sha256'],'workspace':'workspace','allowance_id':'budget','allowance':'10','ceiling':'3','count':1,'purpose':'generation',
       'occurrences':[{'id':'unique-occurrence','index':0,'request_sha256':digest,'native_sha256':native['native_body_sha256'],'profile_sha256':native['profile_sha256'],'quote_sha256':quote['quote_sha256']}]}
    raw=json.dumps({'kind':'openart_credit_authorization','terms':a},sort_keys=True).encode()
    (project/'credit-approval.json').write_bytes(raw); a['evidence']={'path':'credit-approval.json','sha256':hashlib.sha256(raw).hexdigest()}
    kwargs=dict(scope=scope,marker=marker,inputs=inputs,profile=profile,quote_id=quote['quote_id'],request_sha256=digest,occurrence_index=0,attempt_id=str(uuid4()))
    scope['credit_authorization_sha256']=credit.credit_authorization_digest(a)
    return project,a,kwargs


def test_credit_authorization_retained_bytes_to_internal_packet(env,monkeypatch):
    project,a,kwargs=authorization(env)
    from lib import production_execution as execution
    checked=[]
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',lambda inputs,native,profile:checked.append(native['native_body_sha256']))
    packet=credit.validate_credit_authorization(project,a,**kwargs)
    assert packet.binding.quote=='2.25'
    assert packet.binding.authorization_occurrence==credit.canonical_scope_occurrence(a,a['occurrences'][0])
    assert checked
    assert packet.purpose=='generation'
    assert credit.internal_ledger_packet(packet).binding==packet.binding


def test_exact_credit_scope_refuses_co_tagged_unknown_cost_authority(env,monkeypatch):
    from lib import production_execution as execution
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',lambda *args:None)
    project,a,kwargs=authorization(env)
    # Prove every exact-credit term is valid before introducing the scope conflict.
    packet=credit.validate_credit_authorization(project,a,**kwargs)
    assert packet.binding.quote=='2.25' and packet.binding.ceiling=='3'
    before=env[1].read_text()
    kwargs['scope']['unknown_cost_authorization_sha256']='b'*64
    with pytest.raises(cli.OpenArtCLIError,match='scope cannot authorize both exact credit and unknown cost'):
        credit.validate_credit_authorization(project,a,**kwargs)
    assert not (cli.state_dir()/'credits').exists()
    assert env[1].read_text()==before


@pytest.mark.parametrize('field,value',[('scope_id','wrong'),('workspace','wrong'),('account_id_sha256','b'*64),('count',2),('purpose',True),('ceiling','1')])
def test_wrong_credit_approval_fails_without_ledger_or_submit(env,field,value):
    project,a,kwargs=authorization(env); a[field]=value
    with pytest.raises(cli.OpenArtCLIError): credit.validate_credit_authorization(project,a,**kwargs)
    assert not (cli.state_dir()/'credits').exists()
    assert all('--async' not in json.loads(x) for x in env[1].read_text().splitlines())


def test_old_tool_approval_bytes_are_not_credit_authority(env):
    project,a,kwargs=authorization(env); a['evidence']=kwargs['scope']['evidence']
    with pytest.raises(cli.OpenArtCLIError): credit.validate_credit_authorization(project,a,**kwargs)


def test_missing_actual_compilation_cannot_construct_ledger_packet(env):
    from lib.production_execution import ProductionGovernanceError
    project,a,kwargs=authorization(env)
    with pytest.raises((cli.OpenArtCLIError,ProductionGovernanceError)):
        credit.validate_credit_authorization(project,a,**kwargs)
    assert not (cli.state_dir()/'credits').exists()


def test_unsupported_billing_keeps_hold():
    assert credit.qualify_billing_evidence()['billing_state']=='unsupported_hold'
    with pytest.raises(cli.OpenArtCLIError): credit.internal_ledger_packet({'purpose':'result_contract_qualification'})


def _rewrite_credit(project,a):
    raw=json.dumps({'kind':'openart_credit_authorization','terms':{k:v for k,v in a.items() if k!='evidence'}},sort_keys=True).encode()
    (project/'credit-approval.json').write_bytes(raw)
    a['evidence']={'path':'credit-approval.json','sha256':hashlib.sha256(raw).hexdigest()}


def test_alias_amount_quantum_and_guarantees_fail_before_calls(env):
    root,log,profile,inputs,c=env; before=log.read_text()
    bad=[credit.QuoteContract('quantum','quantum','balance','id','workspace','model','mode','maximum_per_generation','covers_all_supported_settings'),
         credit.QuoteContract('credits','quantum','balance','id','workspace','model','mode','covers_all_supported_settings','covers_all_supported_settings')]
    for contract in bad:
        with pytest.raises(cli.OpenArtCLIError): _refresh(inputs,profile,contract)
    assert log.read_text()==before


def test_missing_workspace_qualification_fails(env):
    root,log,profile,inputs,c=env
    contract=credit.QuoteContract('credits','quantum','balance','id',None,'model','mode','maximum_per_generation','covers_all_supported_settings')
    with pytest.raises(cli.OpenArtCLIError): _refresh(inputs,profile,contract)


def test_bad_occurrence_siblings_and_index_outside_scope_fail(env,monkeypatch):
    from lib import production_execution as ex
    monkeypatch.setattr(ex,'_OPENART_COMPILED_REQUEST_CHECK',lambda *args:None)
    project,a,kwargs=authorization(env)
    kwargs['scope']['attempts_per_shot']['shot']=3
    sibling=dict(a['occurrences'][0],index=99,request_sha256='0'*64)
    a['occurrences'].append(sibling); a['count']=2; _rewrite_credit(project,a)
    kwargs['scope']['credit_authorization_sha256']=credit.credit_authorization_digest(a)
    with pytest.raises(cli.OpenArtCLIError): credit.validate_credit_authorization(project,a,**kwargs)
    a['occurrences']=a['occurrences'][:1]; a['occurrences'][0]['index']=7; a['count']=1; _rewrite_credit(project,a)
    kwargs['scope']['credit_authorization_sha256']=credit.credit_authorization_digest(a)
    with pytest.raises(cli.OpenArtCLIError): credit.validate_credit_authorization(project,a,**dict(kwargs,occurrence_index=7))


def test_rewriting_sidecar_and_own_capture_does_not_change_scope_authority(env,monkeypatch):
    from lib import production_execution as ex
    monkeypatch.setattr(ex,'_OPENART_COMPILED_REQUEST_CHECK',lambda *args:None)
    project,a,kwargs=authorization(env)
    a['occurrences'][0]['id']='new-id'; _rewrite_credit(project,a)
    with pytest.raises(cli.OpenArtCLIError): credit.validate_credit_authorization(project,a,**kwargs)


def test_refresh_contract_without_immutable_qualification_is_not_authority(env):
    root,log,profile,inputs,c=env; before=log.read_text()
    with pytest.raises(cli.OpenArtCLIError): credit.refresh_credit_evidence(inputs,profile,c)
    assert log.read_text()==before


def test_canonical_occurrence_survives_reapproved_caller_id(env,monkeypatch):
    from lib import production_execution as ex
    from lib.provider_credit_ledger import CreditLedger, CreditScale, QualifiedSlotRelease, LedgerError
    monkeypatch.setattr(ex,'_OPENART_COMPILED_REQUEST_CHECK',lambda *args:None)
    project,a,kwargs=authorization(env)
    first=credit.validate_credit_authorization(project,a,**kwargs)
    l=CreditLedger(env[0]/'ledger-state')
    l.observe_account('openart_cli',a['account_id_sha256'],'workspace',CreditScale('0.25'),'20','a'*64)
    l.reserve_prepared(credit.internal_ledger_packet(first))
    l.release_slot(QualifiedSlotRelease(first.binding,'proved_not_dispatched','a'*64,'a'*64,''))
    a['occurrences'][0]['id']='reapproved-new-caller-id'; _rewrite_credit(project,a)
    kwargs['scope']['credit_authorization_sha256']=credit.credit_authorization_digest(a)
    second=credit.validate_credit_authorization(project,a,**dict(kwargs,attempt_id=str(uuid4())))
    assert first.binding.authorization_occurrence==second.binding.authorization_occurrence
    with pytest.raises(LedgerError,match='occurrence'): l.reserve_prepared(credit.internal_ledger_packet(second))


def test_all_sibling_request_digests_checked_even_if_current_entry_valid(env,monkeypatch):
    from lib import production_execution as ex
    monkeypatch.setattr(ex,'_OPENART_COMPILED_REQUEST_CHECK',lambda *args:None)
    project,a,kwargs=authorization(env); kwargs['scope']['attempts_per_shot']['shot']=2
    a['occurrences'].append(dict(a['occurrences'][0],id='second',index=1,request_sha256='0'*64)); a['count']=2
    _rewrite_credit(project,a); kwargs['scope']['credit_authorization_sha256']=credit.credit_authorization_digest(a)
    with pytest.raises(cli.OpenArtCLIError,match='sibling'): credit.validate_credit_authorization(project,a,**kwargs)


def test_qualification_is_additive_and_wrong_contract_fails_before_calls(env):
    root,log,profile,inputs,c=env; original=copy.deepcopy(profile); before=log.read_text()
    frozen=(root/'qualification').read_text()
    assert credit.get_retained_quote(inputs,profile,None)['credit_state']=='quote_required'
    altered=credit.QuoteContract('different_price','quantum','balance','id','workspace','model','mode','maximum_per_generation','covers_all_supported_settings')
    with pytest.raises(cli.OpenArtCLIError): credit.refresh_credit_evidence(inputs,profile,altered,qualification_sha256=frozen)
    assert log.read_text()==before
    assert profile==original
    assert credit._load_qualification(frozen,inputs,profile)['terms']['profile_sha256']==jobs.prepare_native_request(inputs,profile)['profile_sha256']


def test_actual_global_workspace_absence_requires_both_raw_assertions(env,monkeypatch):
    root,log,profile,inputs,c=env; monkeypatch.setenv('GLOBAL_ACCOUNT','true')
    global_contract=credit.QuoteContract('credits','quantum','balance','id',None,'model','mode','maximum_per_generation','covers_all_supported_settings',account_global_path='account_global',workspace_absence_path='workspace')
    qualified=credit.qualify_quote_contract(inputs,profile,global_contract)
    q=credit.refresh_credit_evidence(inputs,profile,qualification_sha256=qualified['qualification_sha256'])
    assert credit._load(q['quote_id'])['terms']['workspace']==credit.DEFAULT_WORKSPACE
    monkeypatch.setenv('GLOBAL_ACCOUNT','false')
    with pytest.raises(cli.OpenArtCLIError): credit.refresh_credit_evidence(inputs,profile,qualification_sha256=qualified['qualification_sha256'],approved_quote_id=q['quote_id'])


def test_qualified_preview_raw_stream_reverified_offline(env):
    root,log,profile,inputs,c=env
    q=_refresh(inputs,profile,c); frozen=credit._load(q['quote_id'])
    qualification=credit._load_qualification(frozen['qualification_sha256'],inputs,profile)
    record=jobs._load_receipt(qualification['preview_receipt']['receipt_id'],qualification['preview_receipt']['receipt_sha256'],'test')
    (cli.state_dir()/'streams'/record['streams']['stdout']).unlink()
    before=log.read_text()
    with pytest.raises(cli.OpenArtCLIError): credit.get_retained_quote(inputs,profile,q['quote_id'])
    assert log.read_text()==before


def test_retained_preview_substitution_cannot_keep_same_opaque_id(env):
    root,log,profile,inputs,c=env; q=_refresh(inputs,profile,c)
    path=credit._path(q['quote_id']); proof=json.loads(path.read_text())
    proof['preview_receipt']=next(r for r in profile['captured_receipts'] if r['kind']=='dry_run')
    path.write_text(json.dumps(proof)); before=log.read_text()
    with pytest.raises(cli.OpenArtCLIError): credit.get_retained_quote(inputs,profile,q['quote_id'])
    assert log.read_text()==before
