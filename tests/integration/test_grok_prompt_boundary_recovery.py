"""Offline recovery of one terminal original protocol failure; no second dispatch."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from lib import production_execution as execution
from tools._grok_cli_media import GrokCLIContractError
from tests.integration.test_first_pass_workflow import production, read, save  # noqa: F401


@pytest.fixture
def rejected(production, monkeypatch):
    p = production
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider', None)
    inputs.pop('allowed_providers', None)
    inputs['prompt'] += ' \t'
    inputs['cwd'] = str(p.root)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    def transport(argv, **kwargs):
        result = p.transport(argv, **kwargs)
        if '--prompt-file' not in argv: return result
        events = [json.loads(line) for line in result.stdout.splitlines()]
        events[0]['rawInput']['prompt'] = events[0]['rawInput']['prompt'].strip(' \t\r\n\v\f')
        aid = events[-1]['sessionId']
        rows = []
        for event in events:
            if event['type'] == 'end':
                update = {'sessionUpdate':'turn_completed', 'stop_reason':event['stopReason']}
            else:
                update = {'sessionUpdate':event['type'], **{k:v for k,v in event.items() if k!='type'}}
            rows.append({'method':'session/update', 'params':{'sessionId':aid, 'update':update}})
        folder = p.transport.original_sessions[aid]['artifact'].parent.parent
        (folder/'updates.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
        return subprocess.CompletedProcess(argv, 0, '\n'.join(json.dumps(e) for e in events), '')
    with monkeypatch.context() as old:
        old.setattr('tools._grok_cli_media._run_process', transport)
        old.setattr('tools._grok_cli_media._sealed_arguments_match',
                    lambda actual, expected: actual == expected)  # Prior boundary-space defect.
        result = p.cli.execute(inputs)
    aid = result.data['production_attempt_id']
    directory = p.root/'production_attempts'/aid
    request = read(directory/'request.json')
    assert not result.success and execution.load_attempt_result(p.root,aid)['status']=='failed'
    assert len(p.transport.native_requests)==1
    frozen = {name:(directory/name).read_bytes() for name in ('request.json','raw_result.json','result.json')}
    return p, aid, directory, request, frozen


def collect(rejected, **kwargs):
    p, aid, _, request, _ = rejected
    return execution.collect_original_grok_prompt_boundary(p.root, aid, request_sha256=request['request_sha256'], **kwargs)


def test_original_collection_dry_run_and_append_only_recovery(rejected):
    p, aid, directory, request, frozen = rejected
    scopes = (p.root/'production_scopes.json').read_bytes()
    dry = collect(rejected)
    assert dry['provider_calls'] == dry['reservations'] == 0
    assert not Path(request['submitted_inputs']['output_path']).exists()
    result = collect(rejected, dry_run=False)
    assert result.success and result.data['production_attempt_id']==aid
    assert execution.load_attempt_result(p.root,aid)['status']=='generated'
    p.select('entry',result)  # Complete matching original-result provenance.
    assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1
    assert (p.root/'production_scopes.json').read_bytes()==scopes
    for name,raw in frozen.items(): assert (directory/name).read_bytes()==raw
    with pytest.raises(execution.ProductionGovernanceError): collect(rejected,dry_run=False)
    assert len(p.transport.native_requests)==1


@pytest.mark.parametrize('change', ['internal_text','path','duration','session','tool','second_call','failed_completion','no_terminal','source','input','request_digest'])
def test_original_collection_rejects_nonboundary_or_unbound_evidence_without_calls(rejected, change):
    p, aid, directory, request, _ = rejected
    if change == 'request_digest':
        with pytest.raises(execution.ProductionGovernanceError):
            execution.collect_original_grok_prompt_boundary(p.root,aid,request_sha256='a'*64,dry_run=False)
        return
    folder = p.transport.original_sessions[aid]['artifact'].parent.parent
    path = folder/'updates.jsonl'; rows=[json.loads(line) for line in path.read_text().splitlines()]
    call = rows[0]['params']['update']
    if change=='internal_text': call['rawInput']['prompt'] += ' changed action'
    elif change=='path': call['rawInput']['first_frame'] += ' '
    elif change=='duration': call['rawInput']['duration'] += 1
    elif change=='session': rows[-1]['params']['sessionId']='other-session'
    elif change=='tool': call['toolName']='image_to_video'
    elif change=='second_call': rows.insert(1,copy.deepcopy(rows[0]))
    elif change=='failed_completion': rows[1]['params']['update']['status']='failed'
    elif change=='no_terminal': rows.pop()
    elif change=='source': rows[1]['params']['update']['rawOutput']['path']=str(p.root/'foreign.mp4')
    else:
        target=Path(request['input_assets'][0]['path']);target.chmod(0o644);target.write_bytes(b'tampered')
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    with pytest.raises((execution.ProductionGovernanceError,GrokCLIContractError)):
        collect(rejected,dry_run=False)
    assert not Path(request['submitted_inputs']['output_path']).exists()
    assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1


def test_reconcile_failed_protocol_rejects_caller_self_attested_recovery(rejected):
    from tools.base_tool import ToolResult
    p, aid, directory, request, frozen = rejected
    original=execution.load_attempt_result(p.root,aid)['result']['data']
    data=copy.deepcopy(original)
    data['conditioning_receipt'].update(submission_evidence='verified_native_call',dispatch_status='completed')
    data.update(dispatch_status='completed',original_session_recovery={'claimed':'verified'})
    forged=ToolResult(success=True,data=data,artifacts=[])
    with pytest.raises((execution.ProductionGovernanceError,FileNotFoundError)):
        execution.reconcile_attempt(p.root,aid,forged,request_sha256=request['request_sha256'])
    assert not (directory/'reconciliation.json').exists()
    for name,raw in frozen.items(): assert (directory/name).read_bytes()==raw
    assert len(p.transport.native_requests)==1


def test_recovery_crash_after_canonical_copy_resumes_without_another_call(rejected,monkeypatch):
    p, aid, directory, request, frozen = rejected
    with monkeypatch.context() as interrupted:
        interrupted.setattr(execution,'reconcile_attempt',lambda *a,**k: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt): collect(rejected,dry_run=False)
    assert Path(request['submitted_inputs']['output_path']).is_file()
    assert not (directory/'reconciliation.json').exists()
    recovered=collect(rejected,dry_run=False)
    assert recovered.success
    assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1
    for name,raw in frozen.items(): assert (directory/name).read_bytes()==raw
