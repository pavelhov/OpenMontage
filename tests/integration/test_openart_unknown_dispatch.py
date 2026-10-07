"""Additional offline dispatch crash, authority and pure refusal proofs."""
import copy
import json
import os

import pytest
from lib import openart_dispatch as dispatch, openart_jobs as jobs
from tests.integration.test_openart_unknown_cost_workflow import workflow, submissions, read, write


@pytest.mark.parametrize('stage',['private_prepared','ledger_prepared','journal_ready','ledger_submitting','launch_marker','spawned'])
def test_crash_never_authorizes_second_original(workflow,stage):
    root,inputs,_,tmp,tool,_=workflow
    seen=[]
    def crash(current,attempt):
        if current==stage:
            seen.append(attempt)
            raise RuntimeError('synthetic crash')
    dispatch._CRASH_HOOK=crash
    with pytest.raises(RuntimeError,match='synthetic crash'): tool.execute(inputs)
    dispatch._CRASH_HOOK=None
    attempt=seen[0]
    dispatch.repair_outbox(root,attempt)
    with pytest.raises(Exception): tool.execute(copy.deepcopy(inputs))
    assert len(submissions(tmp))<=1
    manifest,b,_=dispatch._manifest(attempt)
    assert manifest['authorization_kind']=='unknown_cost'
    assert dispatch.ledger().inspect_unpriced(attempt)['billing_state']=='unknown'


def test_current_scope_revocation_prevents_popen(workflow):
    root,inputs,_,tmp,tool,_=workflow
    def revoke(stage,attempt):
        if stage=='ledger_submitting':
            scopes=read(root/'production_scopes.json');scopes['scopes'][0]['status']='revoked'
            write(root/'production_scopes.json',scopes)
    dispatch._CRASH_HOOK=revoke
    with pytest.raises(Exception,match='approval/marker/scope changed'): tool.execute(inputs)
    assert submissions(tmp)==[]


def test_refusal_verification_is_pure_and_checks_original_bytes(workflow,monkeypatch):
    root,inputs,_,tmp,tool,_=workflow
    monkeypatch.setenv('FAKE_REFUSAL','insufficient_credit')
    result=tool.execute(inputs); attempt=result.data['production_attempt_id']
    before=(tmp/'calls').read_bytes()
    proof=dispatch.verify_provider_refusal(attempt)
    assert proof['code']=='insufficient_credit' and 'message' not in proof
    assert (tmp/'calls').read_bytes()==before
    path=jobs.job_dir(attempt,create=False)/'submit.stdout'
    os.chmod(path,0o600);path.write_bytes(b'{"error":{"code":"insufficient_credit","message":"changed"}}')
    assert dispatch.verify_provider_refusal(attempt) is None
    assert (tmp/'calls').read_bytes()==before


def test_absent_refusal_verification_creates_no_state(tmp_path,monkeypatch):
    path=tmp_path/'absent';monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(path))
    assert dispatch.verify_provider_refusal('missing') is None
    assert not path.exists()


@pytest.mark.parametrize('raw',[b'{"error":{"code":"insufficient_credit","message":"private"},"job":{"id":"a"}}',
    b'{"error":{"code":"insufficient_credit","message":4}}',
    b'{"error":{"code":"insufficient_credit","message":"x","extra":true}}',
    b'{"error":{"code":"unavailable_plan","code":"insufficient_credit","message":"x"}}',
    b'{"error":{"code":"transient_error","message":"x"}}'])
def test_refusal_grammar_is_narrow(raw):
    assert jobs.provider_refusal_code(raw) is None


@pytest.mark.parametrize('inputs',[
    {'unknown_cost_authorization_id':None,'unknown_cost_evidence_id':'a'*64},
    {'unknown_cost_authorization_id':[],'unknown_cost_evidence_id':'a'*64},
    {'unknown_cost_authorization_id':'../escape','unknown_cost_evidence_id':'a'*64},
    {'unknown_cost_authorization_id':'ok','unknown_cost_evidence_id':'bad'},
    {'unknown_cost_authorization_id':'ok','unknown_cost_evidence_id':'a'*64,'credit_quote_id':None},
])
def test_direct_unknown_fields_fail_before_refresh(inputs):
    from tools import _openart_cli as cli
    with pytest.raises(cli.OpenArtCLIError) as caught: dispatch._unknown(inputs)
    assert caught.value.kind=='invalid_argument'


def test_unknown_offline_readiness_has_truthful_state(workflow):
    _,inputs,_,tmp,_,_=workflow
    before=(tmp/'calls').read_bytes()
    result=dispatch.offline_readiness(inputs)
    assert result['credit_state']=='unknown_cost_evidence_retained'
    assert (tmp/'calls').read_bytes()==before


def test_provider_refusal_keeps_legacy_exact_hold(tmp_path,monkeypatch):
    (tmp_path/'submit.stdout').write_bytes(b'{"error":{"code":"insufficient_credit","message":"private"}}')
    events=[]
    monkeypatch.setattr(jobs,'job_dir',lambda *args,**kwargs:tmp_path)
    monkeypatch.setattr(jobs,'parse_submit',lambda *args:None)
    monkeypatch.setattr(jobs,'launch_record',lambda *args:{'authorization_kind':'exact_credit'})
    monkeypatch.setattr(jobs,'original_process_state',lambda *args:{'state':'exited','returncode':1})
    monkeypatch.setattr(jobs,'append_event',lambda attempt,event:events.append(event))
    monkeypatch.setattr(jobs,'_launch_result',lambda attempt,status,job:{'status':status})
    assert jobs._finish_parse('original',1,{})['status']=='hold_unknown_job'
    assert [e['type'] for e in events]==['hold_unknown_job']


@pytest.mark.parametrize('revoked',['human_gate','exact_packet'])
def test_current_qualification_gate_and_packet_rechecked_before_launch(workflow,revoked):
    root,inputs,_,tmp,tool,_=workflow
    def revoke(stage,attempt):
        if stage=='ledger_submitting':
            if revoked=='human_gate':
                path=root/'checkpoint_prepare.json';value=read(path);value['human_approved']=False
            else:
                path=root/'artifacts/provider_qualification_packet.json';value=read(path);value['prompt']='changed after authority'
            write(path,value)
    dispatch._CRASH_HOOK=revoke
    with pytest.raises(Exception): tool.execute(inputs)
    assert submissions(tmp)==[]


@pytest.mark.parametrize('revoked',['human_gate','scope'])
def test_authority_rechecked_after_fresh_provider_observations(workflow,monkeypatch,revoked):
    from lib import openart_credit as credit
    root,inputs,_,tmp,tool,_=workflow
    original=credit.refresh_unknown_cost_evidence;calls=[]
    def refresh(*args,**kwargs):
        result=original(*args,**kwargs);calls.append(result)
        if len(calls)==2:
            if revoked=='human_gate':
                path=root/'checkpoint_prepare.json';value=read(path);value['human_approved']=False
            else:
                path=root/'production_scopes.json';value=read(path);value['scopes'][0]['status']='revoked'
            write(path,value)
        return result
    monkeypatch.setattr(credit,'refresh_unknown_cost_evidence',refresh)
    with pytest.raises(Exception): tool.execute(inputs)
    assert len(calls)==2 and submissions(tmp)==[]
