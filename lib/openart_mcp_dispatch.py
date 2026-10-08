"""Rooted connector billing validation and agent-mediated action routing. No network."""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from pathlib import Path
from lib import openart_mcp as mcp

def billing_authorization_digest(authority):
    return mcp.digest(authority)

def _fail(message):
    raise mcp.OpenArtMCPError('billing_authority_invalid',message)

def validate_billing_authority(inputs, checked, native, *, historical_authority=None, profile=None):
    root=Path(checked['root']).resolve();scope=checked['scope'];marker=checked['marker']
    if any(k in inputs for k in ('credit_authorization_id','credit_quote_id','credit_qualification_sha256','unknown_cost_evidence_id')):
        _fail('CLI or exact-credit authority cannot authorize connector generation')
    if scope.get('derived_from_policy') is not None:
        if 'unknown_cost_authorization_id' in inputs:
            _fail('caller authority IDs cannot authorize Auto-continue connector requests')
        from lib.production_autonomy import rooted_policy_mcp_authority,validate_policy_mcp_attempt
        profile = profile or checked.get('openart',(None,))[0]
        if profile is None: _fail('canonical connector profile required for policy authority')
        if historical_authority is not None:
            return validate_policy_mcp_attempt(root,scope,inputs=inputs,native=native,profile=profile,
                authority=historical_authority,return_authority=True)
        return rooted_policy_mcp_authority(root,inputs,scope,native,profile)
    ident=inputs.get('unknown_cost_authorization_id')
    if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}',ident):
        _fail('exact retained Strict connector approval ID required')
    from lib.production_execution import _artifact_path
    try:
        path=_artifact_path(root,'openart_mcp_unknown_cost-'+ident+'.json').resolve();path.relative_to(root)
        authority=json.loads(path.read_bytes())
    except (OSError,ValueError,TypeError):
        _fail('retained Strict connector approval unavailable')
    expected={'provider':'openart_mcp','status':'approved','project_id':marker['project_id'],
        'story_revision':marker['story_revision'],'request_sha256':checked['request_sha256'],
        'scope_id':scope['id'],'shot_id':checked['shot_id'],'model':native['model'],'mode':native['mode'],
        'account_uid_sha256':native['account_binding']['uid_sha256'],'native_body_sha256':native['body_sha256'],
        'source_binding_sha256':native['source_binding_sha256'],'no_enforceable_credit_ceiling':True,
        'max_attempts':1,'cli_authority':False}
    for key,value in expected.items():
        if mcp.digest(authority.get(key)) != mcp.digest(value):
            _fail('retained connector approval differs at '+key)
    if not isinstance(authority.get('approved_by'),str) or not authority['approved_by'].strip():
        _fail('retained connector approval lacks approver evidence')
    sha=billing_authorization_digest(authority)
    if scope.get('openart_mcp_billing_authorization_sha256') != sha and scope.get('openart_mcp_billing_authorizations',{}).get(checked['request_sha256']) != sha:
        _fail('scope does not bind exact per-request connector billing approval')
    evidence=authority.get('evidence',{})
    try:
        path=(root/evidence['path']).resolve();path.relative_to(root)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=evidence['sha256']:
            _fail('retained connector approval bytes changed')
    except (OSError,KeyError,TypeError,ValueError):
        _fail('retained connector approval evidence unavailable or changed')
    if type(checked.get('scope_attempt_index',0)) is not int or checked.get('scope_attempt_index',0) != 0:
        _fail('retained one-attempt connector approval is already consumed')
    qualification=marker.get('pipeline_type')=='provider-qualification'
    purpose='result_contract_qualification' if qualification else 'generation'
    if authority.get('purpose') != purpose:
        _fail('approval purpose differs from exact connector production stage')
    if qualification and (type(native['body']['params'].get('videoCount',1)) is not int or native['body']['params'].get('videoCount',1)!=1):
        _fail('bounded connector qualification requires exactly one original video')
    return {'kind':'unknown_cost','provider':'openart_mcp','authority_sha256':sha,'authorization_sha256':sha,
        'account_uid_sha256':native['account_binding']['uid_sha256'],'request_sha256':checked['request_sha256'],
        'native_body_sha256':native['body_sha256'],'source_binding_sha256':native['source_binding_sha256'],
        'enforceable_credit_ceiling':False,'credits':None,'usd':None,'max_attempts':1,
        'purpose':'qualification' if qualification else 'production','evidence_sha256':evidence['sha256']}

def invoke(inputs):
    from lib import openart_mcp_jobs as jobs
    from lib.production_execution import prepare_openart_mcp_handoff
    if not isinstance(inputs,dict) or not inputs.get('project_dir'):
        raise mcp.OpenArtMCPError('invalid_argument','rooted connector generation inputs required')
    attempt_id=inputs.get('attempt_id') or 'mcp-'+uuid.uuid4().hex
    return jobs.prepare(inputs['project_dir'],attempt_id=attempt_id,generation_inputs=inputs,
        authority_fn=prepare_openart_mcp_handoff)

def invoke_action(action,project_dir,attempt_id=None,**kwargs):
    from lib import openart_mcp_jobs as jobs
    from lib.production_execution import prepare_openart_mcp_handoff
    if action=='begin':return jobs.begin(project_dir,attempt_id,authority_fn=prepare_openart_mcp_handoff)
    if action=='receive':return jobs.receive(project_dir,attempt_id,**kwargs)
    if action=='poll':return jobs.poll_args(project_dir,attempt_id)
    if action=='record_status':return jobs.record_status(project_dir,attempt_id,**kwargs)
    if action=='download':return jobs.download_original(project_dir,attempt_id,**kwargs)
    if action=='collect':return jobs.collect(project_dir,attempt_id,**kwargs)
    if action=='state':return jobs.attempt_state(project_dir,attempt_id)
    if action=='list':return jobs.list_attempts(project_dir)
    if action=='provenance':return jobs.provenance_record(project_dir,attempt_id)
    if action=='qualify':return jobs.qualify_result(project_dir,attempt_id)
    if action=='upload_prepare':return jobs.prepare_upload(project_dir,**kwargs)
    if action=='upload_receipt':return jobs.record_upload_receipt(project_dir,**kwargs)
    raise mcp.OpenArtMCPError('invalid_action','unsupported connector attempt action')
