"""Unknown-cost evidence: fresh account/price/native capture; never a guaranteed maximum.

Fixture shape mirrors the retained 2026-10-07 smoke (Free plan, 40 credits; model cost
default config 5s/540p = 50 credits). Never real OpenArt.
"""
import json
import os
import sys

import pytest
from lib import openart_credit as credit, openart_setup as setup, openart_jobs as jobs
from tools import _openart_cli as cli

FAKE=r'''#!PYTHON
import json,os,sys
args=[x for x in sys.argv[1:] if x not in ['--json','--no-input']]
with open(os.environ['FAKE_LOG'],'a') as f: f.write(json.dumps(args)+'\n')
if args==['version']: out={'version':os.environ.get('VERSION','0.1.1')}
elif args==['account']:
 out={'id':os.environ.get('ACCOUNT','account'),'tier':'free','plan':'Free'}
 c=os.environ.get('CREDITS','40')
 if c!='missing': out['credits']=int(c) if c.lstrip('-').isdigit() else c
elif args[:2]==['model','form']: out={'properties':{'prompt':{'type':'string'},'duration':{'type':'integer','default':5},'resolution':{'type':'string','default':'540p'},'aspectRatio':{'type':'string','default':'16:9'}},'required':['prompt']}
elif args[:2]==['model','cost']:
 shape=os.environ.get('COST','default')
 cfg={'aspectRatio':'16:9','duration':5,'generateAudio':False,'resolution':'540p','videoCount':1}
 if shape=='matched': cfg.update(duration=1,resolution='720p')
 items=[] if shape=='missing' else [{'model':os.environ.get('COST_MODEL','m1'),'mode':'text2video','media':'video','config':cfg,'unitCredits':50,'quantity':1,'totalCredits':50}]
 if shape=='guarantee': items[0]['maximum_per_generation']=True
 out={'currency':'credits','items':items}
 if shape=='malformed_amount': out['items'][0]['totalCredits']='NaN'
 if shape=='malformed_items': out['items']={}
 if shape=='malformed_currency': out['currency']='USD'
elif args[:2]==['generate','video'] and '--dry-run' in args:
 params={'prompt':args[2]}
 for flag,key,cast in (('--duration','duration',int),('--resolution','resolution',str),('--aspect-ratio','aspectRatio',str)):
  if flag in args: params[key]=cast(args[args.index(flag)+1])
 if os.environ.get('DRIFT_BODY'): params['extra']=1
 out={'endpoint':'POST /api/cli/v1/generate','body':{'model':'m1','media':'video','mode':'text2video','params':params}}
else: raise SystemExit(7)
print(json.dumps(out))
'''
PATHS={'account_id':'id','account_tier':'tier','submit_job_id':'job.id','result_job_id':'creation.id','status':'creation.status','status_terminal_ok':'done','status_terminal_fail':'failed','urls':'creation.urls','url_hosts':['cdn.example.test']}


@pytest.fixture
def env(tmp_path,monkeypatch):
    binary=tmp_path/'fake'; binary.write_text(FAKE.replace('PYTHON',sys.executable)); binary.chmod(0o755)
    state=tmp_path/'state'
    monkeypatch.setenv('OPENART_CLI_PATH',str(binary)); monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(state))
    log=tmp_path/'calls'; monkeypatch.setenv('FAKE_LOG',str(log)); monkeypatch.delenv('OPENMONTAGE_OPENART_OFFLINE',raising=False)
    setup.inspect_qualification('m1','text2video',json_paths=PATHS)
    setup.qualify_preview('m1','text2video',prompt='private prompt',duration=1,resolution='720p',aspect_ratio='16:9')
    profile=jobs.load_qualification(model='m1',mode='text2video',require='pre_submit')
    dr=next(r for r in profile['captured_receipts'] if r['kind']=='dry_run')
    inputs={'prompt':'private prompt','model':'m1','mode':'text2video','duration':1,'resolution':'720p','aspect_ratio':'16:9',
            'native_dry_run_receipt_id':dr['receipt_id'],'native_dry_run_receipt_sha256':dr['receipt_sha256']}
    return tmp_path,log,profile,inputs


def _calls(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_mismatched_default_price_proves_neither_affordability_nor_insufficiency(env):
    """AE2: 1s/720p requested, provider prices only default 5s/540p at 50, balance 40."""
    root,log,profile,inputs=env
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    assert ev['requested_charge']=='unknown'
    assert ev['price_classification']=='mismatched_default'
    assert ev['observed_default_price']=={'credits':'50','label':'model_default_not_requested',
        'settings':{'aspect_ratio':'16:9','duration':5,'resolution':'540p'}}
    assert ev['balance_observation']=={'credits':'40','label':'unattributed_observation'}
    assert ev['workspace']==credit.UNOBSERVED_WORKSPACE and ev['workspace_billing_guarantee']=='unverified'
    text=json.dumps(ev)
    for forbidden in ('affordable','insufficient','ceiling','maximum','usd','private prompt'):
        assert forbidden not in text.lower()
    assert all('--async' not in c for c in _calls(log))
    native=jobs.prepare_native_request(inputs,profile)
    assert ev['native_body_sha256']==native['native_body_sha256'] and ev['profile_sha256']==native['profile_sha256']


def test_retained_evidence_reverified_with_zero_calls(env):
    root,log,profile,inputs=env
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    before=log.read_text()
    again=credit.get_retained_unknown_evidence(inputs,profile,ev['evidence_id'])
    assert again['provider_calls']==0 and ev['provider_calls']==6
    strip=lambda d:{k:v for k,v in d.items() if k not in ('provider_calls','read_only_cli_calls','generation_calls')}
    assert strip(again)==strip(ev) and log.read_text()==before


def test_load_absent_state_creates_nothing(tmp_path,monkeypatch):
    state=tmp_path/'absent'
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(state))
    with pytest.raises(cli.OpenArtCLIError):
        credit.load_unknown_evidence('a'*64)
    assert not state.exists()


def test_incomplete_effective_settings_and_missing_price_classified(env,monkeypatch):
    root,log,profile,inputs=env
    monkeypatch.setenv('COST','matched')
    assert credit.refresh_unknown_cost_evidence(inputs,profile)['price_classification']=='mismatched_default'
    monkeypatch.setenv('COST','missing')
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    assert ev['price_classification']=='missing' and ev['observed_default_price'] is None
    assert ev['requested_charge']=='unknown'


def test_provider_guarantee_fields_never_promote_unknown(env,monkeypatch):
    root,log,profile,inputs=env
    monkeypatch.setenv('COST','guarantee')
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    assert ev['requested_charge']=='unknown' and 'maximum' not in json.dumps(ev)


@pytest.mark.parametrize('key,value',[('ACCOUNT','other'),('VERSION','9'),('DRIFT_BODY','1')])
def test_account_cli_native_drift_refuses_before_evidence(env,monkeypatch,key,value):
    root,log,profile,inputs=env
    monkeypatch.setenv(key,value)
    with pytest.raises(cli.OpenArtCLIError):
        credit.refresh_unknown_cost_evidence(inputs,profile)
    d=cli.state_dir()/'unknown_cost_evidence'
    assert not d.exists() or not list(d.iterdir())


def test_retained_evidence_tamper_refused(env):
    root,log,profile,inputs=env
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    path=cli.state_dir()/'unknown_cost_evidence'/(ev['evidence_id']+'.json')
    data=json.loads(path.read_bytes()); data['balance']='4000'
    os.chmod(path,0o600); path.write_text(json.dumps(data))
    with pytest.raises(cli.OpenArtCLIError):
        credit.get_retained_unknown_evidence(inputs,profile,ev['evidence_id'])


def test_balance_change_yields_new_evidence_id(env,monkeypatch):
    root,log,profile,inputs=env
    a=credit.refresh_unknown_cost_evidence(inputs,profile)
    monkeypatch.setenv('CREDITS','30')
    b=credit.refresh_unknown_cost_evidence(inputs,profile)
    assert a['evidence_id']!=b['evidence_id'] and b['balance_observation']['credits']=='30'


def test_public_load_returns_safe_summary_never_raw_receipts(env):
    root,log,profile,inputs=env
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    public=credit.load_unknown_evidence(ev['evidence_id'])
    assert public=={k:v for k,v in ev.items() if k not in ('read_only_cli_calls','generation_calls')}|{'provider_calls':0}
    text=json.dumps(public)
    for raw in ('receipt','terms','stdout','private prompt'):
        assert raw not in text


@pytest.mark.parametrize('credits',['missing','x','-1'])
def test_missing_or_invalid_balance_refused_before_evidence(env,monkeypatch,credits):
    root,log,profile,inputs=env
    monkeypatch.setenv('CREDITS',credits)
    with pytest.raises(cli.OpenArtCLIError):
        credit.refresh_unknown_cost_evidence(inputs,profile)
    d=cli.state_dir()/'unknown_cost_evidence'
    assert not d.exists() or not list(d.iterdir())


def test_fresh_capture_reports_exact_read_only_call_count(env):
    root,log,profile,inputs=env
    before=len(_calls(log))
    ev=credit.refresh_unknown_cost_evidence(inputs,profile)
    calls=_calls(log)[before:]
    assert ev['read_only_cli_calls']==len(calls)==ev['provider_calls'] and ev['generation_calls']==0
    assert all('--dry-run' in c for c in calls if c[:2]==['generate','video'])


def test_public_classifier_entry(env):
    root,log,profile,inputs=env
    cost={'currency':'credits','items':[{'model':'m1','mode':'text2video','config':{'aspectRatio':'16:9','duration':5,'resolution':'540p','videoCount':1},'quantity':1,'totalCredits':50}]}
    kind,price=credit.classify_price_evidence(cost,profile,{'duration':1,'resolution':'720p','aspect_ratio':'16:9'})
    assert kind=='mismatched_default' and price['credits']=='50'


def _cost_row(cfg,quantity=1,credits=50):
    return {'currency':'credits','items':[{'model':'m1','mode':'text2video','config':cfg,'quantity':quantity,'totalCredits':credits}]}


_REQ={'duration':1,'resolution':'720p','aspect_ratio':'16:9'}


def test_integral_decimal_duration_normalised_and_hashable(env):
    from decimal import Decimal
    root,log,profile,inputs=env
    cfg={'aspectRatio':'16:9','duration':Decimal('1.0'),'resolution':'720p','videoCount':1,'generateAudio':False}
    kind,price=credit.classify_price_evidence(_cost_row(cfg),profile,_REQ)
    assert kind=='mismatched_default' and price['settings']['duration']==1 and type(price['settings']['duration']) is int
    json.dumps(price)


@pytest.mark.parametrize('cfg,quantity',[
    ({'aspectRatio':'16:9','duration':Decimal_half,'resolution':'720p'},1) for Decimal_half in ['5.5']
]+[
    ({'aspectRatio':'16:9','duration':True,'resolution':'720p'},1),
    ({'aspectRatio':'16:9','duration':5,'resolution':'https://x.example/a'},1),
    ({'aspectRatio':'16:9','duration':5,'resolution':['720p']},1),
    ({'aspectRatio':'wide','duration':5,'resolution':'540p'},1),
    ({'aspectRatio':'16:9','duration':5,'resolution':'540p','videoCount':True},1),
    ({'aspectRatio':'16:9','duration':5,'resolution':'540p'},True),
])
def test_malformed_observed_settings_refused(env,cfg,quantity):
    from decimal import Decimal
    root,log,profile,inputs=env
    if isinstance(cfg.get('duration'),str): cfg={**cfg,'duration':Decimal(cfg['duration'])}
    with pytest.raises(cli.OpenArtCLIError,match='malformed'):
        credit.classify_price_evidence(_cost_row(cfg,quantity),profile,_REQ)


@pytest.mark.parametrize('missing',['videoCount','generateAudio'])
def test_missing_cost_relevant_controls_cannot_match(env,missing):
    _,_,profile,_=env
    cfg={'aspectRatio':'16:9','duration':1,'resolution':'720p','videoCount':1,'generateAudio':False}
    del cfg[missing]
    kind,_=credit.classify_price_evidence(_cost_row(cfg),profile,_REQ)
    assert kind!='settings_matched'


def test_malformed_available_price_raises(env):
    _,_,profile,_=env
    with pytest.raises(cli.OpenArtCLIError,match='malformed'):
        credit.classify_price_evidence(_cost_row({'aspectRatio':'16:9','duration':1,'resolution':'720p'},credits='NaN'),profile,_REQ)


def test_observed_form_defaults_allow_full_price_match(env):
    _,_,profile,_=env
    profile={**profile,'form_defaults':{**profile['form_defaults'],'videoCount':1,'generateAudio':False}}
    cfg={'aspectRatio':'16:9','duration':1,'resolution':'720p','videoCount':1,'generateAudio':False}
    assert credit.classify_price_evidence(_cost_row(cfg),profile,_REQ)[0]=='settings_matched'


@pytest.mark.parametrize('quantity',['NaN','Infinity'])
def test_nonfinite_price_quantity_is_malformed(env,quantity):
    from decimal import Decimal
    _,_,profile,_=env
    with pytest.raises(cli.OpenArtCLIError,match='malformed'):
        credit.classify_price_evidence(_cost_row({'aspectRatio':'16:9','duration':1,'resolution':'720p'},Decimal(quantity)),profile,_REQ)


@pytest.mark.parametrize('cost',[
    {'currency':'credits','items':{}},
    {'currency':'USD','items':[]},
    {'currency':'credits','items':[None]},
    {'currency':'credits','items':[{}]},
    {'currency':'credits','items':[{'model':'m1','mode':'text2video','config':{},'totalCredits':50}]},
])
def test_malformed_available_cost_envelope_is_refused(env,cost):
    _,_,profile,_=env
    with pytest.raises(cli.OpenArtCLIError,match='malformed'):
        credit.classify_price_evidence(cost,profile,_REQ)


@pytest.mark.parametrize('cost',[None,{'supported':False}])
def test_explicit_absent_or_unsupported_cost_observation_is_unavailable(env,cost):
    _,_,profile,_=env
    assert credit.classify_price_evidence(cost,profile,_REQ)==('unavailable',None)


@pytest.mark.parametrize('shape',['malformed_amount','malformed_items','malformed_currency'])
def test_fresh_malformed_cost_creates_no_unknown_proof(env,monkeypatch,shape):
    _,log,profile,inputs=env
    monkeypatch.setenv('COST',shape)
    with pytest.raises(cli.OpenArtCLIError,match='malformed'):
        credit.refresh_unknown_cost_evidence(inputs,profile)
    path=cli.state_dir(create=False)/'unknown_cost_evidence'
    assert not path.exists()
    assert all('--async' not in argv for argv in _calls(log))
