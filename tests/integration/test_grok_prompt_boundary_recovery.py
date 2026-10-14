"""Offline recovery of one terminal original protocol failure; no second dispatch."""
import copy
import json
from pathlib import Path
import subprocess
import shutil

_REAL_RUN = subprocess.run
_REAL_WHICH = shutil.which

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


def test_resume_revalidates_collected_original_without_collector_replay(rejected, monkeypatch):
    p, aid, directory, request, frozen = rejected
    first = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert first.success
    scopes = (p.root / 'production_scopes.json').read_bytes()
    monkeypatch.setattr(execution, 'collect_original_grok_prompt_boundary',
        lambda *a, **k: pytest.fail('already-collected original must not replay collector'))
    second = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert second.success and second.data['production_attempt_id'] == aid
    assert len(p.transport.native_requests) == 1
    assert (p.root / 'production_scopes.json').read_bytes() == scopes
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw
    receipt = directory / 'reconciliation.json'
    receipt.chmod(0o644)
    value = read(receipt); value['result']['data']['conditioning_receipt']['session_id'] = 'foreign-session'; save(receipt, value)
    with pytest.raises(execution.ProductionGovernanceError):
        execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])


@pytest.fixture
def pending_original(production, monkeypatch):
    p = production
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider', None); inputs.pop('allowed_providers', None)
    inputs['cwd'] = str(p.root)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    def interrupted(argv, **kwargs):
        result = p.transport(argv, **kwargs)
        if '--prompt-file' not in argv: return result
        events = [json.loads(line) for line in result.stdout.splitlines()]
        aid = events[-1]['sessionId']
        folder = p.transport.original_sessions[aid]['artifact'].parent.parent
        # Actual Grok logs emit this statusless original progress observation.
        events.insert(1, {'type': 'tool_call_update', 'toolCallId': events[0]['toolCallId']})
        (folder / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events[:2]))
        p.pending_events = events
        raise KeyboardInterrupt()
    monkeypatch.setattr('tools._grok_cli_media._run_process', interrupted)
    with pytest.raises(KeyboardInterrupt): p.cli.execute(inputs)
    request = execution._attempts(p.root)[0]
    aid = request['attempt_id']
    return p, aid, request, p.transport.original_sessions[aid]['artifact'].parent.parent


def original_log(folder, aid, events, shape):
    if shape == 'events':
        path = folder / 'events.jsonl'
        path.write_text('\n'.join(json.dumps(e) for e in events))
    else:
        path = folder / 'updates.jsonl'
        rows = []
        for event in events:
            update = ({'sessionUpdate': 'turn_completed', 'stop_reason': event['stopReason']}
                      if event['type'] == 'end' else
                      {'sessionUpdate': event['type'], **{k: v for k, v in event.items() if k != 'type'}})
            rows.append({'method': 'session/update', 'params': {'sessionId': aid, 'update': update}})
        path.write_text('\n'.join(json.dumps(row) for row in rows))
    return path


@pytest.mark.parametrize('shape', ['events', 'updates'])
@pytest.mark.parametrize('progress', ['call', 'progress_update', 'completed_before_end'])
def test_pending_original_resume_observes_then_collects_same_attempt(pending_original, shape, progress, monkeypatch):
    p, aid, request, folder = pending_original
    events = p.pending_events[:{'call': 1, 'progress_update': 2, 'completed_before_end': 3}[progress]]
    original_log(folder, aid, events, shape)
    frozen = {path: path.read_bytes() for path in p.root.rglob('*.json')}
    for dry_run in (True, False, False):
        result = execution.resume_original_attempt(p.root, aid,
            request_sha256=request['request_sha256'], dry_run=dry_run)
        assert result['status'] == 'pending' and result['attempt_id'] == aid
        assert result['provider_calls'] == result['reservations'] == 0
        assert execution.load_attempt_result(p.root, aid)['status'] == 'uncertain'
        assert {path: path.read_bytes() for path in p.root.rglob('*.json')} == frozen
        assert not Path(request['submitted_inputs']['output_path']).exists()
        assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1
    action = {'kind': 'collect_grok_original', 'action_id': 'same-original',
              'subject': aid, 'host_required': False,
              'binding': {'request_sha256': request['request_sha256']}}
    with monkeypatch.context() as observer:
        observer.setattr(execution, 'next_production_action', lambda root: action)
        pending = execution.advance(p.root)
    assert pending['status'] == 'blocked' and pending['action'] == action
    assert pending['provider_calls'] == 0 and 'still pending' in pending['reason']
    assert {path: path.read_bytes() for path in p.root.rglob('*.json')} == frozen
    original_log(folder, aid, p.pending_events, shape)
    recovered = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert recovered.success and recovered.data['production_attempt_id'] == aid
    assert execution.load_attempt_result(p.root, aid)['status'] == 'generated'
    replay = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert replay.success and replay.data['production_attempt_id'] == aid
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


@pytest.mark.parametrize('change', ['failed', 'end_without_completed', 'malformed',
    'wrong_tool', 'missing_call_id', 'foreign_update', 'unknown_status', 'input',
    'duplicate_completed', 'malformed_completed', 'failed_beside_completed'])
def test_pending_original_rejects_failed_malformed_or_unbound_records(pending_original, change):
    p, aid, request, folder = pending_original
    events = copy.deepcopy(p.pending_events[:2])
    if change == 'failed': events[1]['status'] = 'failed'
    elif change == 'end_without_completed': events.append(p.pending_events[-1])
    elif change == 'wrong_tool': events[0]['toolName'] = 'image_to_video'
    elif change == 'missing_call_id': events[0].pop('toolCallId')
    elif change == 'foreign_update': events[1]['toolCallId'] = 'foreign-call'
    elif change == 'unknown_status': events[1]['status'] = 'unrecognized'
    elif change == 'duplicate_completed': events.extend([p.pending_events[2], p.pending_events[2]])
    elif change == 'malformed_completed':
        events.append(copy.deepcopy(p.pending_events[2])); events[-1]['rawOutput']['type'] = 'Unexpected'
    elif change == 'failed_beside_completed':
        events[1]['status'] = 'failed'; events.append(p.pending_events[2])
    elif change == 'input':
        path = Path(request['input_assets'][0]['path']); path.chmod(0o644); path.write_bytes(b'changed')
    log = original_log(folder, aid, events, 'events')
    if change == 'malformed': log.write_text(log.read_text() + '\n{"type":')
    with pytest.raises((execution.ProductionGovernanceError, GrokCLIContractError, ValueError)):
        execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert not Path(request['submitted_inputs']['output_path']).exists()
    assert not (p.root / 'production_attempts' / aid / 'reconciliation.json').exists()
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


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


def test_recoverable_protocol_rejected_original_never_opens_second_route(rejected):
    from lib import production_autonomy as pa
    from tests.lib.test_episode_production_controls import activate
    p, aid, directory, request, frozen = rejected
    activate(p.root, 1, alternate='disabled', fallback=False)
    from lib import episode_production_controls as controls
    denied = controls.generation_admission(p.root, shot_id='entry', provider='openart_mcp',
                                           model='minimax-hailuo-02', purpose='first_pass')
    assert not denied['admitted']
    assert controls.generation_usage(p.root)['per_shot']['entry'] == 1
    for name, raw in frozen.items():
        assert (directory/name).read_bytes() == raw
    assert collect(rejected, dry_run=False).success
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize('shape', ['completed_wrong_raw_output', 'failed_beside_completed'])
def test_completed_native_update_labeled_terminal_never_opens_second_route(production, monkeypatch, shape):
    """The wrapper label is not proof: retained native logs must show no completed update."""
    from lib import production_autonomy as pa
    from tests.lib.test_episode_production_controls import activate
    p = production
    activate(p.root, 1, alternate='disabled', fallback=False)
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider', None)
    inputs.pop('allowed_providers', None)
    inputs['cwd'] = str(p.root)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    def transport(argv, **kwargs):
        result = p.transport(argv, **kwargs)
        if '--prompt-file' not in argv: return result
        events = [json.loads(line) for line in result.stdout.splitlines()]
        if shape == 'completed_wrong_raw_output':
            events[1]['rawOutput']['type'] = 'Unexpected'
            events[1]['content'] = 'spending limit reached'
        else:
            events.insert(1, {'type':'tool_call_update', 'toolCallId':'native-1', 'status':'failed',
                              'content':'HTTP 403 personal-team-blocked:spending-limit'})
        stream = '\n'.join(json.dumps(e) for e in events)
        (p.transport.original_sessions[events[-1]['sessionId']]['artifact'].parent.parent/'events.jsonl').write_text(stream)
        return subprocess.CompletedProcess(argv, 0, stream, '')
    monkeypatch.setattr('tools._grok_cli_media._run_process', transport)
    result = p.cli.execute(inputs)
    aid = result.data['production_attempt_id']
    directory = p.root/'production_attempts'/aid
    assert not result.success and result.data['error_category'] == 'spending_limit'
    assert result.data['dispatch_status'] == 'failed'
    frozen = {name:(directory/name).read_bytes() for name in ('request.json','raw_result.json','result.json')}
    from lib import episode_production_controls as controls
    denied = controls.generation_admission(p.root, shot_id='entry', provider='openart_mcp',
                                           model='minimax-hailuo-02', purpose='first_pass')
    assert not denied['admitted']
    assert controls.generation_usage(p.root)['per_shot']['entry'] == 1
    for name, raw in frozen.items():
        assert (directory/name).read_bytes() == raw
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize('tamper', [None, 'duration', 'session', 'second_call', 'no_terminal', 'input'])
@pytest.mark.parametrize('interruption', ['timeout', 'process_crash'])
def test_uncertain_completed_original_collected_from_exact_session(production, monkeypatch, interruption, tamper):
    p = production
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider', None); inputs.pop('allowed_providers', None)
    inputs['cwd'] = str(p.root)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    monkeypatch.setenv('GROK_SESSIONS_ROOT', str(p.root/'sessions'))
    def interrupted(argv, **kwargs):
        result = p.transport(argv, **kwargs)
        if '--prompt-file' not in argv: return result
        events = [json.loads(line) for line in result.stdout.splitlines()]
        aid = events[-1]['sessionId']
        rows = []
        for event in events:
            update = ({'sessionUpdate':'turn_completed','stop_reason':event['stopReason']}
                      if event['type']=='end' else {'sessionUpdate':event['type'], **{k:v for k,v in event.items() if k!='type'}})
            rows.append({'method':'session/update','params':{'sessionId':aid,'update':update}})
        folder = p.transport.original_sessions[aid]['artifact'].parent.parent
        (folder/'updates.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
        if interruption == 'process_crash': raise KeyboardInterrupt()
        raise GrokCLIContractError('timeout','synthetic interrupted transport',dispatch_status='indeterminate')
    monkeypatch.setattr('tools._grok_cli_media._run_process', interrupted)
    if interruption == 'process_crash':
        with pytest.raises(KeyboardInterrupt): p.cli.execute(inputs)
        aid = execution._attempts(p.root)[0]['attempt_id']
    else:
        aid = p.cli.execute(inputs).data['production_attempt_id']
    request = read(p.root/'production_attempts'/aid/'request.json')
    assert execution.load_attempt_result(p.root,aid)['status']=='uncertain'
    if tamper:
        folder = p.transport.original_sessions[aid]['artifact'].parent.parent
        path = folder/'updates.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if tamper=='duration': rows[0]['params']['update']['rawInput']['duration'] += 1
        elif tamper=='session': rows[-1]['params']['sessionId']='foreign'
        elif tamper=='second_call': rows.insert(1, copy.deepcopy(rows[0]))
        elif tamper=='no_terminal': rows.pop()
        else:
            asset=Path(request['input_assets'][0]['path']);asset.chmod(0o644);asset.write_bytes(b'changed')
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        if tamper == 'no_terminal':
            pending = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
            assert pending['status'] == 'pending' and pending['attempt_id'] == aid
            assert pending['provider_calls'] == pending['reservations'] == 0
            assert not Path(request['submitted_inputs']['output_path']).exists()
            assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1
            return
        with pytest.raises((execution.ProductionGovernanceError, GrokCLIContractError)):
            execution.collect_original_grok_prompt_boundary(p.root, aid,
                request_sha256=request['request_sha256'], dry_run=False)
        assert not Path(request['submitted_inputs']['output_path']).exists()
        assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1
        return
    before = len(p.transport.native_requests)
    retained_version = request['tool_version']
    monkeypatch.setattr(type(p.cli), 'version', 'future-version-must-not-rewrite-original')
    recovered = execution.collect_original_grok_prompt_boundary(p.root, aid,
        request_sha256=request['request_sha256'], dry_run=False)
    assert recovered.success and recovered.data['original_session_recovery']['mode']=='uncertain_original_session'
    assert recovered.data['conditioning_receipt']['adapter_version']==retained_version
    assert read(p.root/'production_attempts'/aid/'request.json')['request_sha256']==request['request_sha256']
    assert execution.load_attempt_result(p.root,aid)['status']=='generated'
    assert recovered.data['conditioning_receipt']['cli_version'] == '1.0.34'
    p.select('entry', recovered)
    from lib.production_draft import first_cut_candidates
    candidate = first_cut_candidates(p.root)[0]
    assert candidate['status'] == 'candidate' and candidate['attempt_id'] == aid
    assert candidate['strict_selected'] is True
    assert len(p.transport.native_requests)==before==len(execution._attempts(p.root))==1


@pytest.mark.parametrize('timeout', [1, True, 901, '600'])
def test_bad_local_timeout_rejects_before_governed_reservation(production, timeout):
    p = production
    inputs = copy.deepcopy(p.inputs['entry']); inputs['timeout_seconds']=timeout
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    with pytest.raises(GrokCLIContractError, match='timeout_seconds'):
        execution.governed_dry_run(p.cli,inputs)
    with pytest.raises(GrokCLIContractError, match='timeout_seconds'):
        p.cli.execute(inputs)
    assert len(execution._attempts(p.root))==len(p.transport.native_requests)==0


def test_historical_missing_adapter_version_stays_unknown(production, monkeypatch):
    # A journal written by an older engine must never borrow today's version.
    from tools.video.grok_cli_video import GrokCLIVideo
    p = production
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider', None); inputs.pop('allowed_providers', None)
    inputs['cwd'] = str(p.root)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    result = p.cli.execute(inputs)
    aid = result.data['production_attempt_id']
    directory = p.root/'production_attempts'/aid
    request = read(directory/'request.json');request.pop('tool_version')
    # Local synthetic legacy journal: immutable real production is never edited.
    (directory/'request.json').chmod(0o644)
    save(directory/'request.json',request)
    (directory/'request.json').chmod(0o444)
    for name in ('raw_result.json', 'result.json', 'grok_launch.json'):(directory/name).unlink()
    folder=p.transport.original_sessions[aid]['artifact'].parent.parent
    events=[json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
    rows=[]
    for event in events:
        update=({'sessionUpdate':'turn_completed','stop_reason':event['stopReason']} if event['type']=='end'
                else {'sessionUpdate':event['type'],**{k:v for k,v in event.items() if k!='type'}})
        rows.append({'method':'session/update','params':{'sessionId':aid,'update':update}})
    (folder/'updates.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    monkeypatch.setenv('GROK_SESSIONS_ROOT',str(p.root/'sessions'))
    monkeypatch.setattr(GrokCLIVideo,'version','invented-current-version')
    recovered=execution.collect_original_grok_prompt_boundary(p.root,aid,
        request_sha256=request['request_sha256'],dry_run=False)
    assert recovered.data['conditioning_receipt']['adapter_version'] is None
    assert recovered.data['conditioning_receipt']['cli_version'] is None
    assert len(p.transport.native_requests)==1


@pytest.mark.parametrize('setting,value', [('resolution','1080p'), ('duration',True),
    ('operation','upscale'), ('unexpected_native_control',True), ('voices',[' preset '])])
@pytest.mark.parametrize('route', ['direct', 'selector'])
def test_entire_unsupported_request_rejects_before_reservation(production,setting,value,route):
    p=production; inputs=copy.deepcopy(p.inputs['entry']);inputs[setting]=value
    if route=='direct':
        inputs.pop('preferred_provider',None);inputs.pop('allowed_providers',None)
    tool=p.cli if route=='direct' else p.selector
    p.scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=p.root)
    p.persist_scope()
    with pytest.raises((GrokCLIContractError,execution.ProductionGovernanceError)):execution.governed_dry_run(tool,inputs)
    with pytest.raises((GrokCLIContractError,execution.ProductionGovernanceError)):tool.execute(inputs)
    assert len(execution._attempts(p.root))==len(p.transport.native_requests)==0


def test_crash_custom_sessions_root_survives_later_config_changes(production,monkeypatch):
    import shutil
    from urllib.parse import quote
    from lib.production_draft import first_cut_candidates
    p=production;inputs=copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider',None);inputs.pop('allowed_providers',None)
    inputs['cwd']=str(p.root)
    p.scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=p.root);p.persist_scope()
    custom=p.root/'custom-original-sessions';p.cli._sessions_root=str(custom)
    monkeypatch.setenv('GROK_SESSIONS_ROOT',str(p.root/'wrong-env-root'))
    def interrupted(argv,**kwargs):
        result=p.transport(argv,**kwargs)
        if '--prompt-file' not in argv:return result
        events=[json.loads(line) for line in result.stdout.splitlines()]
        aid=events[-1]['sessionId'];original=p.transport.original_sessions[aid]['artifact'].parent.parent
        destination=custom/quote(str(p.root),safe='')/aid
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(original),str(destination))
        events[1]['rawOutput']['path']=str(destination/'videos/clip.mp4')
        rows=[]
        for event in events:
            update=({'sessionUpdate':'turn_completed','stop_reason':event['stopReason']} if event['type']=='end'
                    else {'sessionUpdate':event['type'],**{k:v for k,v in event.items() if k!='type'}})
            rows.append({'method':'session/update','params':{'sessionId':aid,'update':update}})
        (destination/'updates.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
        raise KeyboardInterrupt()
    monkeypatch.setattr('tools._grok_cli_media._run_process',interrupted)
    with pytest.raises(KeyboardInterrupt):p.cli.execute(inputs)
    request=execution._attempts(p.root)[0];aid=request['attempt_id']
    launch=read(p.root/'production_attempts'/aid/'grok_launch.json')
    assert launch['sessions_root']==str(custom) and launch['cli_version']=='1.0.34'
    p.cli._sessions_root=str(p.root/'new-config-root')
    monkeypatch.setenv('GROK_SESSIONS_ROOT',str(p.root/'new-env-root'))
    recovered=execution.collect_original_grok_prompt_boundary(p.root,aid,request_sha256=request['request_sha256'],dry_run=False)
    p.select('entry',recovered)
    row=first_cut_candidates(p.root)[0]
    assert row['status']=='candidate' and row['strict_selected'] and row['attempt_id']==aid
    assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1


@pytest.mark.parametrize('field', ['cli_version', 'adapter_version'])
def test_original_launch_versions_cannot_be_rewritten_in_success_receipt(production, field):
    p=production
    inputs=copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider',None);inputs.pop('allowed_providers',None)
    p.scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=p.root);p.persist_scope()
    result=p.cli.execute(inputs);aid=result.data['production_attempt_id']
    directory=p.root/'production_attempts'/aid
    # Synthetic adversarial receipt reseal. Original launch remains unchanged.
    raw=read(directory/'raw_result.json');state=read(directory/'result.json')
    raw['data']['conditioning_receipt'][field]='fabricated-version'
    if field=='cli_version':raw['data']['cli_version']='fabricated-version'
    else:
        receipt=raw['data']['conditioning_receipt']
        omitted={'session_id','cli_version','dispatch_status','submission_evidence','request_sha256'}
        receipt['request_sha256']=execution._digest({k:v for k,v in receipt.items() if k not in omitted})
    state['result']=copy.deepcopy(raw)
    for name,value in [('raw_result.json',raw),('result.json',state)]:
        (directory/name).chmod(0o644);save(directory/name,value);(directory/name).chmod(0o444)
    from lib.production_provenance import validate_attempt_provenance
    with pytest.raises(ValueError,match='version differs from retained original launch'):
        validate_attempt_provenance(p.root,aid,shot_id='entry',story_revision=p.story['story_revision'],expected_output=state['output'])
    assert len(p.transport.native_requests)==1


@pytest.mark.parametrize('tamper',['canonical_bytes','missing_logs','wrong_source'])
def test_legacy_timeout_success_reconciliation_requires_original_native_bytes(production,tamper):
    p=production;p.transport.timeout_next=True
    result=p.generate('entry');aid=result.data['production_attempt_id']
    directory=p.root/'production_attempts'/aid;request=read(directory/'request.json')
    output=Path(request['submitted_inputs']['output_path'])
    recovered=copy.deepcopy(result);recovered.success=True;recovered.error=None;recovered.artifacts=[str(output)]
    recovered.data['dispatch_status']='completed'
    recovered.data['conditioning_receipt'].update(submission_evidence='verified_native_call',dispatch_status='completed')
    assert not recovered.data.get('original_session_recovery')
    source=p.transport.original_sessions[aid]['artifact']
    if tamper=='canonical_bytes':output.write_bytes(b'arbitrary caller replaced media')
    elif tamper=='missing_logs':(source.parent.parent/'events.jsonl').unlink()
    else:recovered.data['source_artifact']=str(output)
    before={name:(directory/name).read_bytes() for name in ('request.json','raw_result.json','result.json')}
    with pytest.raises((execution.ProductionGovernanceError,GrokCLIContractError)):
        execution.reconcile_attempt(p.root,aid,recovered,request_sha256=request['request_sha256'])
    assert not (directory/'reconciliation.json').exists()
    assert execution.load_attempt_result(p.root,aid)['status']=='uncertain'
    assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1
    for name,raw in before.items():assert (directory/name).read_bytes()==raw


@pytest.mark.parametrize('evidence',['completed','missing_end','failure_output_pointer','true_failure'])
def test_motion_failure_reconciliation_requires_actual_original_terminal_failure(production,evidence):
    p=production;p.transport.timeout_next=True
    original=p.generate('entry');aid=original.data['production_attempt_id']
    directory=p.root/'production_attempts'/aid;request=read(directory/'request.json')
    failed=copy.deepcopy(original);failed.error='original native failure'
    failed.data['dispatch_status']='failed'
    failed.data['conditioning_receipt'].update(submission_evidence='verified_native_call',dispatch_status='failed')
    source=p.transport.original_sessions[aid]['artifact'];log=source.parent.parent/'events.jsonl'
    if evidence!='completed':
        events=[json.loads(line) for line in log.read_text().splitlines()]
        events[1]={'type':'tool_call_update','toolCallId':'native-1','status':'failed',
                   'rawOutput':{'error':'tool_execution_failed','message':'HTTP 403 spending limit reached'}}
        if evidence=='failure_output_pointer':events[1]['rawOutput']['path']=str(source)
        if evidence=='missing_end':events.pop()
        log.write_text('\n'.join(json.dumps(e) for e in events))
        source.unlink();Path(request['submitted_inputs']['output_path']).unlink()
    if evidence=='true_failure':
        record=execution.reconcile_attempt(p.root,aid,failed,request_sha256=request['request_sha256'])
        assert record['status']=='failed' and record['original_session_evidence']['outcome']=='failed'
    else:
        with pytest.raises(execution.ProductionGovernanceError,match='terminal failure'):
            execution.reconcile_attempt(p.root,aid,failed,request_sha256=request['request_sha256'])
        assert not (directory/'reconciliation.json').exists()
        assert execution.load_attempt_result(p.root,aid)['status']=='uncertain'
    assert len(p.transport.native_requests)==len(execution._attempts(p.root))==1


@pytest.fixture
def download_failed(production, monkeypatch):
    p = production
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs.pop('preferred_provider', None); inputs.pop('allowed_providers', None)
    inputs['cwd'] = str(p.root)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    url = 'https://vidgen.x.ai/xai-vidgen-bucket/xai-video-00000000-0000-0000-0000-000000000001.mp4'
    message = 'Failed to download video: error sending request for url (' + url + ')'
    def transport(argv, **kwargs):
        result = p.transport(argv, **kwargs)
        if '--prompt-file' not in argv: return result
        events = [json.loads(line) for line in result.stdout.splitlines()]
        events[1].update(status='failed', content=[{'type':'content', 'content':{'type':'text',
            'text':'Tool `reference_to_video` failed: ' + message}}],
            rawOutput={'error': 'tool_execution_failed', 'message': message})
        aid = events[-1]['sessionId']; folder = p.transport.original_sessions[aid]['artifact'].parent.parent
        original_log(folder, aid, events, 'updates')
        p.transport.original_sessions[aid]['artifact'].unlink()
        return subprocess.CompletedProcess(argv, 0, '\n'.join(json.dumps(e) for e in events), '')
    monkeypatch.setattr('tools._grok_cli_media._run_process', transport)
    result = p.cli.execute(inputs)
    aid = result.data['production_attempt_id']; directory = p.root / 'production_attempts' / aid
    assert not result.success and result.data['error_category'] == 'cli'
    request = read(directory / 'request.json')
    frozen = {name: (directory / name).read_bytes() for name in ('request.json', 'raw_result.json', 'result.json', 'grok_launch.json')}
    original_media = directory / 'SYNTHETIC-response.mp4'
    _REAL_RUN([_REAL_WHICH('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=blue:s=1280x720:r=2:d=8', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-pix_fmt', 'yuv420p', str(original_media)], check=True, capture_output=True)
    body = original_media.read_bytes(); original_media.unlink()
    class Download:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield body
    downloads = []
    def get(actual, **kwargs):
        assert actual == url and kwargs['allow_redirects'] is False
        downloads.append(actual); return Download()
    monkeypatch.setattr('requests.get', get)
    def probe_only(argv, **kwargs):
        assert Path(argv[0]).name == 'ffprobe', 'no CLI generation or observation during recovery'
        return _REAL_RUN(argv, **kwargs, capture_output=True, text=True)
    monkeypatch.setattr('tools._grok_cli_media._run_process', probe_only)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda name: _REAL_WHICH(name))
    return p, aid, directory, request, frozen, downloads


def test_failed_original_download_recovers_once_without_dispatch(download_failed):
    p, aid, directory, request, frozen, downloads = download_failed
    scopes = (p.root / 'production_scopes.json').read_bytes()
    dry = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'], dry_run=True)
    assert dry['would_download_original'] and not downloads
    result = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert result.success and len(downloads) == 1
    p.select('entry', result)
    again = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert again.success and len(downloads) == 1
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1
    assert (p.root / 'production_scopes.json').read_bytes() == scopes
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw


@pytest.mark.parametrize('damage', ['foreign_url', 'unmatched_url', 'other_error', 'completed', 'foreign_call', 'session', 'arguments', 'incomplete', 'input', 'ambiguous', 'duplicate_failure'])
def test_failed_download_recovery_refuses_unbound_evidence_before_network(download_failed, damage):
    p, aid, directory, request, frozen, downloads = download_failed
    folder = p.transport.original_sessions[aid]['artifact'].parent.parent
    log = folder / 'updates.jsonl'; rows = [json.loads(line) for line in log.read_text().splitlines()]
    failed = rows[1]['params']['update']
    if damage == 'foreign_url': failed['rawOutput']['message'] = failed['rawOutput']['message'].replace('vidgen.x.ai', 'foreign.invalid')
    elif damage == 'unmatched_url': failed['rawOutput']['message'] = failed['rawOutput']['message'].replace('000000000001', '000000000002')
    elif damage == 'other_error': failed['rawOutput']['message'] = 'account exhausted'
    elif damage == 'completed': failed['status'] = 'completed'
    elif damage == 'foreign_call': failed['toolCallId'] = 'foreign-call'
    elif damage == 'session': rows[1]['params']['sessionId'] = 'foreign-session'
    elif damage == 'arguments': rows[0]['params']['update']['rawInput']['duration'] = 7
    elif damage == 'incomplete': rows.pop()
    elif damage == 'input':
        path = Path(request['input_assets'][0]['path']); path.chmod(0o644); path.write_bytes(b'changed')
    elif damage == 'ambiguous': failed['rawOutput']['message'] += ' https://foreign.invalid/video.mp4'
    elif damage == 'duplicate_failure': rows.insert(2, copy.deepcopy(rows[1]))
    log.write_text('\n'.join(json.dumps(row) for row in rows))
    with pytest.raises((ValueError, GrokCLIContractError)):
        execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert not downloads and not (directory / 'reconciliation.json').exists()
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize('damage', ['receipt', 'media', 'log'])
def test_collected_failed_download_revalidates_all_original_evidence(download_failed, damage):
    p, aid, directory, request, frozen, downloads = download_failed
    result = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert result.success
    if damage == 'receipt':
        path = directory / 'original_download.json'; path.chmod(0o644)
        receipt = read(path); receipt['binding']['url'] = 'https://foreign.invalid/video.mp4'; save(path, receipt)
    elif damage == 'media':
        path = directory / 'original_download.mp4'; path.chmod(0o644); path.write_bytes(b'changed')
    else:
        path = p.transport.original_sessions[aid]['artifact'].parent.parent / 'updates.jsonl'
        path.write_text(path.read_text() + '\n{}')
    with pytest.raises((ValueError, GrokCLIContractError)):
        execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert len(downloads) == len(p.transport.native_requests) == 1


@pytest.mark.parametrize('failure', ['redirect', 'invalid_media'])
def test_original_download_rejects_redirects_and_invalid_media(download_failed, monkeypatch, failure):
    p, aid, directory, request, frozen, downloads = download_failed
    class BadDownload:
        status_code = 302 if failure == 'redirect' else 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield b'not video'
    def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        downloads.append(url); return BadDownload()
    monkeypatch.setattr('requests.get', get)
    with pytest.raises((ValueError, GrokCLIContractError)):
        execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    assert len(downloads) == len(p.transport.native_requests) == 1
    assert not (directory / 'original_download.json').exists()
    assert not (directory / 'reconciliation.json').exists()
    assert not Path(request['submitted_inputs']['output_path']).exists()
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw


@pytest.mark.parametrize('tamper', [False, True])
def test_original_download_receipt_interruption_revalidates_orphan_via_same_get(download_failed, monkeypatch, tamper):
    p, aid, directory, request, frozen, downloads = download_failed
    write_new = execution._write_new
    def interrupted(path, value):
        if Path(path).name == 'original_download.json':
            raise OSError('SYNTHETIC interruption after source publication')
        return write_new(path, value)
    with monkeypatch.context() as interruption:
        interruption.setattr(execution, '_write_new', interrupted)
        with pytest.raises(OSError, match='interruption'):
            execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
    source = directory / 'original_download.mp4'
    assert source.is_file() and not (directory / 'original_download.json').exists()
    if tamper:
        source.chmod(0o644); source.write_bytes(b'changed orphan')
    dry = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'], dry_run=True)
    assert dry['would_download_original'] and len(downloads) == 1
    if tamper:
        with pytest.raises(ValueError, match='orphan'):
            execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
        assert not (directory / 'original_download.json').exists()
        assert source.read_bytes() == b'changed orphan'
    else:
        result = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
        assert result.success
    assert len(downloads) == 2 and len(p.transport.native_requests) == 1
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw


@pytest.mark.parametrize('foreign_title', [False, True])
def test_download_recovery_accepts_actual_acp_title_and_statusless_progress_shape(download_failed, foreign_title):
    p, aid, directory, request, frozen, downloads = download_failed
    folder = p.transport.original_sessions[aid]['artifact'].parent.parent
    path = folder / 'updates.jsonl'; rows = [json.loads(line) for line in path.read_text().splitlines()]
    call = rows[0]['params']['update']
    call['title'] = 'foreign_tool' if foreign_title else call.pop('toolName')
    call.pop('toolName', None)
    # Actual C100 ACP: title names the tool; a statusless progress update has
    # a human prompt title; terminal comes through the _x.ai method alias.
    rows.insert(1, {'method':'session/update', 'params':{'sessionId':aid,
        'update':{'sessionUpdate':'tool_call_update', 'toolCallId':call['toolCallId'],
            'title':'SYNTHETIC human prompt progress', 'content':[]}}})
    rows[-1]['method'] = '_x.ai/session/update'
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    if foreign_title:
        with pytest.raises(ValueError, match='exact failed native call'):
            execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'], dry_run=True)
        assert not downloads
    else:
        dry = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'], dry_run=True)
        assert dry['would_download_original'] and not downloads
        result = execution.resume_original_attempt(p.root, aid, request_sha256=request['request_sha256'])
        assert result.success and len(downloads) == 1
        p.select('entry', result)
    assert len(p.transport.native_requests) == 1
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw
