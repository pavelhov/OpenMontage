"""Offline exact Strict billing proof validation; no connector operations."""
import copy
import hashlib
import json
import pytest
from lib import openart_mcp as mcp,openart_mcp_dispatch as dispatch

@pytest.fixture
def packet(tmp_path):
    (tmp_path/'artifacts').mkdir();evidence=tmp_path/'approval.txt';evidence.write_text('Synthetic fixture approval: accept no enforceable credit ceiling for one exact request.')
    native={'model':'fixture-model','mode':'text2video','body_sha256':'b'*64,'source_binding_sha256':'c'*64,
        'account_binding':{'uid_sha256':'d'*64},'body':{'params':{'prompt':'fixture'}}}
    checked={'root':tmp_path,'marker':{'project_id':'fixture','story_revision':'r1','pipeline_type':'provider-qualification'},
        'scope':{'id':'scope'},'shot_id':'shot','request_sha256':'a'*64,'scope_attempt_index':0}
    approval={'provider':'openart_mcp','status':'approved','project_id':'fixture','story_revision':'r1',
        'scope_id':'scope','shot_id':'shot','request_sha256':'a'*64,'model':'fixture-model','mode':'text2video',
        'native_body_sha256':'b'*64,'source_binding_sha256':'c'*64,'account_uid_sha256':'d'*64,
        'max_attempts':1,'cli_authority':False,'no_enforceable_credit_ceiling':True,
        'purpose':'result_contract_qualification','approved_by':'Synthetic fixture',
        'evidence':{'path':'approval.txt','sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}}
    path=tmp_path/'artifacts/openart_mcp_unknown_cost-original.json';path.write_text(json.dumps(approval))
    checked['scope']['openart_mcp_billing_authorization_sha256']=dispatch.billing_authorization_digest(approval)
    return {'unknown_cost_authorization_id':'original'},checked,native,approval,path


def test_exact_unknown_cost_packet_and_requestmap(packet):
    inputs,checked,native,approval,path=packet
    result=dispatch.validate_billing_authority(inputs,checked,native)
    assert result['enforceable_credit_ceiling'] is False and result['credits'] is None and result['usd'] is None
    assert result['purpose']=='qualification'
    sha=checked['scope'].pop('openart_mcp_billing_authorization_sha256')
    checked['scope']['openart_mcp_billing_authorizations']={checked['request_sha256']:sha}
    dispatch.validate_billing_authority(inputs,checked,native)

@pytest.mark.parametrize('key,value',[
    ('provider','openart_cli'),('model','foreign'),('mode','image2video'),('account_uid_sha256','f'*64),
    ('native_body_sha256','f'*64),('source_binding_sha256','f'*64),('request_sha256','f'*64),
    ('project_id','other'),('story_revision','old'),('max_attempts',True),('max_attempts',1.0),
    ('no_enforceable_credit_ceiling',1),('cli_authority',True),('purpose','generation')])
def test_strict_approval_identity_tamper(packet,key,value):
    inputs,checked,native,approval,path=packet;approval[key]=value;path.write_text(json.dumps(approval))
    checked['scope']['openart_mcp_billing_authorization_sha256']=dispatch.billing_authorization_digest(approval)
    with pytest.raises(mcp.OpenArtMCPError):dispatch.validate_billing_authority(inputs,checked,native)


def test_scope_evidence_cardinality_cli_and_consumption(packet):
    inputs,checked,native,approval,path=packet
    for field in ('credit_authorization_id','credit_quote_id','unknown_cost_evidence_id'):
        with pytest.raises(mcp.OpenArtMCPError):dispatch.validate_billing_authority({**inputs,field:'caller'},checked,native)
    checked['scope_attempt_index']=1
    with pytest.raises(mcp.OpenArtMCPError):dispatch.validate_billing_authority(inputs,checked,native)
    checked['scope_attempt_index']=0
    native['body']['params']['videoCount']=2
    with pytest.raises(mcp.OpenArtMCPError):dispatch.validate_billing_authority(inputs,checked,native)
    native['body']['params'].pop('videoCount')
    (checked['root']/'approval.txt').write_text('changed')
    with pytest.raises(mcp.OpenArtMCPError):dispatch.validate_billing_authority(inputs,checked,native)
