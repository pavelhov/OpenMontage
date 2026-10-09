"""Exact creator repair intents use canonical attempts; all media is offline."""
import copy
import json

import pytest

from lib import episode_production_controls as controls
from lib import production_execution as execution
from lib.shot_contract import file_sha256
from tests.lib.test_production_execution import MotionTool, project, write_scopes
from tests.lib.test_episode_production_controls import approval, repair_request


def enable(root, limit=2, alternate='different_provider_or_media_model'):
    return controls.record_episode_controls(root, {
        'max_generations_per_shot': limit, 'alternate_repair': alternate,
        'access_fallback': True, 'first_cut': {'enabled': True},
    }, evidence=approval(root))


def test_first_cut_mode_defers_automatic_creative_repair(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    enable(tmp_path, alternate='disabled')
    tool = MotionTool()
    first = tool.execute(inputs)
    replacement = repair_request(tmp_path, inputs, scope, first.data['production_attempt_id'], 1)
    with pytest.raises(ValueError, match='creator-selected repair'):
        tool.execute(replacement)
    assert tool.calls == 1


def test_first_cut_defaults_off_for_legacy_controls(tmp_path):
    project(tmp_path, motion=True)
    from tests.lib.test_episode_production_controls import activate
    activate(tmp_path)
    assert controls.first_cut_enabled(tmp_path) is False

from lib import production_repair_batches as batches
from lib import production_draft as draft
from tests.lib.test_first_cut import p  # noqa: F401
from tests.integration.test_first_pass_workflow import production  # noqa: F401
from tools.base_tool import ToolResult


def fake_cut(inputs):
    from pathlib import Path
    Path(inputs['output_path']).write_bytes(b'SYNTHETIC CUT')
    return ToolResult(success=True, artifacts=[inputs['output_path']])


def batch_item(p, sid, source, name='item', *, provider='grok_cli', tool='grok_cli_video'):
    inputs = copy.deepcopy(p.inputs[sid])
    inputs['output_path'] = str(p.root / 'assets/video' / ('creator-' + name + '.mp4'))
    inputs['prompt'] += ' Creator requested clearer staging.'
    return {'item_id': name, 'shot_id': sid, 'source_attempt_id': source,
            'selected_route': {'provider': provider, 'tool': tool, 'model': None, 'mode': inputs.get('operation')},
            'changes': 'Improve this passing shot staging on the same route.', 'inputs': inputs}


def exact_scope(p, item, scope_id):
    scope = copy.deepcopy(p.scope)
    scope.update(id=scope_id, phase='repair', provider=item['selected_route']['provider'],
                 replaces_attempt_ids=[item['source_attempt_id']],
                 requests={item['shot_id']: execution.planned_request_digest(item['inputs'], project_dir=p.root)},
                 attempts_per_shot={item['shot_id']: 1})
    scope['approval_plan_sha256'] = execution.approval_plan_digest(execution.load_shot_contract(p.root))
    data = execution._read(p.root / 'production_scopes.json')
    data['scopes'].append(scope)
    (p.root / 'production_scopes.json').write_text(json.dumps(data))
    return scope


def cut_binding(cut):
    return {'path': cut['path'], 'sha256': cut['sha256']}


def test_same_model_pass_unwanted_does_not_change_global_policy_or_reviews(p):
    enable(p.root)
    first = p.generate('entry')
    selected = p.select('entry', first)
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    original = (p.root / 'artifacts/selected_attempts.json').read_bytes()
    item = batch_item(p, 'entry', first.data['production_attempt_id'])
    saved = batches.record_creator_repair_batch(p.root, batch_id='creator-1', cut=cut_binding(cut),
        items=[item], evidence=approval(p.root, 'creator-intent'))
    assert saved['items'][0]['state'] == 'planned'
    exact_scope(p, item, 'creator-scope')
    batches.prepare_repair_item(p.root, 'creator-1', 'item', scope_id='creator-scope')
    receipt = batches.resume_repair_batch(p.root, 'creator-1', tools={'grok_cli': p.selector})
    assert receipt['errors'] == [] and receipt['items'][0]['state'] == 'completed', receipt
    replacement = receipt['items'][0]['attempt_id']
    assert replacement != selected['attempt_id']
    assert controls.effective_controls(p.root)['alternate_repair'] == 'different_provider_or_media_model'
    assert (p.root / 'artifacts/selected_attempts.json').read_bytes() == original
    assert draft.first_cut_candidates(p.root)[0]['attempt_id'] == replacement
    assert draft.first_cut_candidates(p.root)[0]['strict_selected'] is False
    successor = draft.compose_first_cut(p.root, compose=fake_cut)
    assert successor['clips'][0]['attempt_id'] == replacement
    assert successor['sequence'] == cut['sequence'] + 1
    assert controls.generation_usage(p.root)['total'] == 2
    replay = batches.resume_repair_batch(p.root, 'creator-1', tools={'grok_cli': p.selector})
    assert replay['actions'] == [] and replay['errors'] == []
    assert len(p.transport.native_requests) == 2


@pytest.mark.parametrize('mapping', ['exact', 'legacy'])
def test_native_tool_mapping_keeps_exact_and_legacy_callers_compatible(p, mapping):
    enable(p.root)
    first = p.generate('entry'); p.select('entry', first)
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    item = batch_item(p, 'entry', first.data['production_attempt_id'])
    item['inputs'].pop('preferred_provider')
    item['inputs'].pop('allowed_providers')
    batches.record_creator_repair_batch(p.root, batch_id='mapping', cut=cut_binding(cut),
        items=[item], evidence=approval(p.root, 'creator-intent'))
    exact_scope(p, item, 'mapping-scope')
    batches.prepare_repair_item(p.root, 'mapping', 'item', scope_id='mapping-scope')
    key = ('grok_cli', 'grok_cli_video')
    # A correct exact key takes precedence over an unusable legacy entry.
    tools = {key: p.cli, 'grok_cli': None} if mapping == 'exact' else {'grok_cli': p.cli}
    receipt = batches.resume_repair_batch(p.root, 'mapping', tools=tools)
    assert receipt['errors'] == [] and receipt['items'][0]['state'] == 'completed', receipt
    assert len(p.transport.native_requests) == 2
    assert batches.resume_repair_batch(p.root, 'mapping', tools=tools)['actions'] == []
    assert len(p.transport.native_requests) == 2


@pytest.mark.parametrize('invalid', ['none', 'wrong_tool', 'wrong_provider'])
def test_invalid_exact_native_mapping_never_falls_back_to_legacy_entry(p, invalid):
    from types import SimpleNamespace
    enable(p.root)
    first = p.generate('entry'); p.select('entry', first)
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    item = batch_item(p, 'entry', first.data['production_attempt_id'])
    batches.record_creator_repair_batch(p.root, batch_id='invalid-mapping', cut=cut_binding(cut),
        items=[item], evidence=approval(p.root, 'creator-intent'))
    exact_scope(p, item, 'mapping-scope')
    batches.prepare_repair_item(p.root, 'invalid-mapping', 'item', scope_id='mapping-scope')
    tool = {'none': None,
            'wrong_tool': SimpleNamespace(name='wrong_video', provider='grok_cli'),
            'wrong_provider': SimpleNamespace(name='grok_cli_video', provider='other')}[invalid]
    receipt = batches.resume_repair_batch(p.root, 'invalid-mapping',
        tools={('grok_cli', 'grok_cli_video'): tool, 'grok_cli': p.cli})
    assert receipt['errors'] == [{'item_id': 'item',
        'error': 'exact canonical tool required to execute this repair item'}]
    assert receipt['actions'] == [] and receipt['items'][0]['state'] == 'planned'
    assert len(p.transport.native_requests) == 1


def test_failed_and_unplayable_replacement_preserves_original_candidate(p, monkeypatch):
    enable(p.root)
    first = p.generate('entry'); selected = p.select('entry', first)
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    item = batch_item(p, 'entry', selected['attempt_id'])
    batches.record_creator_repair_batch(p.root, batch_id='failed-repair', cut=cut_binding(cut),
        items=[item], evidence=approval(p.root, 'creator-intent'))
    exact_scope(p, item, 'creator-scope')
    batches.prepare_repair_item(p.root, 'failed-repair', 'item', scope_id='creator-scope')
    p.transport.timeout_next = True
    receipt = batches.resume_repair_batch(p.root, 'failed-repair', tools={'grok_cli': p.selector})
    assert receipt['items'][0]['state'] == 'uncertain', receipt
    assert draft.first_cut_candidates(p.root)[0]['attempt_id'] == selected['attempt_id']
    assert batches.resume_repair_batch(p.root, 'failed-repair', tools={'grok_cli': p.selector})['actions'] == []
    aid = receipt['items'][0]['attempt_id']
    directory = p.root / 'production_attempts' / aid
    original_state = execution._read(directory / 'result.json')
    # Characterize a terminal no-output native journal without a generation retry.
    (directory / 'result.json').chmod(0o644)
    (directory / 'result.json').write_text(json.dumps({**original_state, 'status': 'failed'}))
    assert batches.repair_batch_status(p.root, 'failed-repair')['items'][0]['state'] == 'failed'
    assert draft.first_cut_candidates(p.root)[0]['attempt_id'] == selected['attempt_id']
    assert controls.generation_usage(p.root)['total'] == 2


def test_acceptance_closes_pending_intent_later_explicit_batch_allowed(p):
    enable(p.root, limit=3)
    first = p.generate('entry'); p.select('entry', first)
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    item = batch_item(p, 'entry', first.data['production_attempt_id'])
    evidence = approval(p.root, 'creator-intent')
    batches.record_creator_repair_batch(p.root, batch_id='old-intent', cut=cut_binding(cut), items=[item], evidence=evidence)
    exact_scope(p, item, 'creator-old')
    batches.prepare_repair_item(p.root, 'old-intent', 'item', scope_id='creator-old')
    accept = approval(p.root, 'accept-cut')
    draft.record_first_cut_acceptance(p.root, cut_binding(cut), accepted_by=accept['approved_by'],
        evidence={k:v for k,v in accept.items() if k != 'approved_by'})
    assert batches.repair_batch_status(p.root, 'old-intent')['items'][0]['closed']
    assert batches.resume_repair_batch(p.root, 'old-intent', tools={'grok_cli': p.selector})['actions'] == []
    old_scope = next(s for s in execution._read(p.root / 'production_scopes.json')['scopes'] if s['id']=='creator-old')
    blocked = copy.deepcopy(item['inputs']); blocked['governance']['scope_id'] = old_scope['id']
    with pytest.raises(ValueError, match='intent is closed'):
        p.cli.execute(blocked)
    newer = batch_item(p, 'entry', first.data['production_attempt_id'], 'later')
    batches.record_creator_repair_batch(p.root, batch_id='later-intent', cut=cut_binding(cut),
        items=[newer], evidence=approval(p.root, 'later-intent'))
    exact_scope(p, newer, 'creator-later')
    batches.prepare_repair_item(p.root, 'later-intent', 'later', scope_id='creator-later')
    receipt = batches.resume_repair_batch(p.root, 'later-intent', tools={'grok_cli': p.selector})
    assert receipt['errors'] == [] and receipt['items'][0]['state'] == 'completed', receipt
    assert controls.generation_usage(p.root)['total'] == 2


def test_duplicate_intent_scope_mismatch_and_shared_limit(p, monkeypatch):
    enable(p.root, limit=1)
    first = p.generate('entry')
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    item = batch_item(p, 'entry', first.data['production_attempt_id'])
    evidence = approval(p.root, 'creator-intent')
    kwargs = dict(batch_id='exact', cut=cut_binding(cut), items=[item], evidence=evidence)
    assert batches.record_creator_repair_batch(p.root, **kwargs) == batches.record_creator_repair_batch(p.root, **kwargs)
    changed = copy.deepcopy(item); changed['changes'] = 'An unrelated request'
    with pytest.raises(ValueError, match='different exact intent'):
        batches.record_creator_repair_batch(p.root, **{**kwargs, 'items':[changed]})
    wrong = exact_scope(p, item, 'wrong'); wrong['requests']['entry'] = 'f'*64
    data=execution._read(p.root/'production_scopes.json'); data['scopes'][-1]=wrong
    (p.root/'production_scopes.json').write_text(json.dumps(data))
    with pytest.raises(ValueError, match='independently authorize'):
        batches.prepare_repair_item(p.root,'exact','item',scope_id='wrong')
    exact_scope(p, item, 'good')
    original_scopes=(p.root/'production_scopes.json').read_bytes()
    from lib import provider_credit_ledger as ledger,openart_dispatch as dispatch
    with monkeypatch.context() as private:
        # Stub the existing canonical original-query boundary only: its manifest
        # already identifies an unpublished reservation for this exact scope.
        snapshot={'reservations':[{'attempt_id':'original-private','binding_json':json.dumps({'project_root':str(p.root)})}]}
        private.setattr(ledger,'read_existing_snapshot',lambda:snapshot)
        private.setattr(dispatch,'_manifest',lambda _:({'journal_records':{'request.json':{'scope_id':'good'}}},None,None))
        with pytest.raises(ValueError,match='already consumed scope'):
            batches.prepare_repair_item(p.root,'exact','item',scope_id='good')
        assert (p.root/'production_scopes.json').read_bytes()==original_scopes
        assert not (p.root/'production_repair_batches/exact/item.scope.json').exists()
        assert len(p.transport.native_requests)==1
        assert snapshot['reservations'][0]['attempt_id']=='original-private'
    with monkeypatch.context() as unavailable:
        unavailable.setattr(ledger,'read_existing_snapshot',lambda:(_ for _ in ()).throw(OSError('unreadable originals')))
        with pytest.raises(ValueError,match='already consumed scope'):
            batches.prepare_repair_item(p.root,'exact','item',scope_id='good')
        assert (p.root/'production_scopes.json').read_bytes()==original_scopes
    batches.prepare_repair_item(p.root,'exact','item',scope_id='good')
    receipt=batches.resume_repair_batch(p.root,'exact',tools={'grok_cli':p.selector})
    assert 'episode generation limit reached' in receipt['errors'][0]['error']
    assert receipt['items'][0]['state']=='planned' and controls.generation_usage(p.root)['total']==1

from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401
from tests.integration.test_openart_first_pass_workflow import media_path, real_av_clip  # noqa: F401


def mcp_compile(root, inputs, sid, rid, template_timing):
    from lib import production_request as prep, openart_mcp as mcp
    inputs = copy.deepcopy(inputs)
    inputs.update(compiled_request_id=rid, preparation_review_id=rid)
    inputs['governance'] = {'shot_id': sid}
    authored = prep.compile_provider_prompt(root, sid, provider='openart_mcp', model=inputs['model'])
    inputs['prompt'] = authored['prompt']
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='supported')
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    timing = copy.deepcopy(template_timing)
    index = 0 if sid == 'entry' else 1
    seconds=inputs['native_params']['duration']
    timing['duration_seconds'] = seconds
    for window, start, end in zip(timing['action_windows'], (0,1.2), (1.15,1.95)):
        window.update(source_pointer=window['source_pointer'].replace('/shots/0/', '/shots/'+str(index)+'/'),
                      start_seconds=start*seconds/2, end_seconds=end*seconds/2)
    compiled = prep.prepare_compiled_request(inputs, native, profile, coverage=authored['coverage'], timing=timing)
    review = execution._read(root / 'artifacts/preparation_review-original.json')
    review.update(review_id=rid, subject_sha256=prep.digest(compiled))
    (root / ('artifacts/compiled_request-'+rid+'.json')).write_text(json.dumps(compiled))
    (root / ('artifacts/preparation_review-'+rid+'.json')).write_text(json.dumps(review))
    return inputs, native


@pytest.fixture
def mcp_two(lifecycle, monkeypatch, media_path):
    from lib import production_autonomy as pa, production_provenance as provenance
    from tests.lib.test_production_autonomy import install_existing
    from tests.lib.test_shot_contract import refresh
    root, policy, _, base, _, _, jobs = lifecycle
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    contract = execution.load_shot_contract(root)
    contract['shots'][0]['duration_seconds'] = 2
    other = copy.deepcopy(contract['shots'][0]); other['id']='other'
    contract['shots'].append(other); refresh(contract)
    (root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    scenes=execution._read(root/'artifacts/scene_plan.json'); scenes['scenes'][0]['end_seconds']=2
    next_scene=copy.deepcopy(scenes['scenes'][0]); next_scene.update(id='other',start_seconds=2,end_seconds=4,script_section_id='s2')
    scenes['scenes'].append(next_scene); (root/'artifacts/scene_plan.json').write_text(json.dumps(scenes))
    script=execution._read(root/'artifacts/script.json'); script['total_duration_seconds']=4
    script['sections'][0]['end_seconds']=2
    section=copy.deepcopy(script['sections'][0]); section.update(id='s2',start_seconds=2,end_seconds=4)
    script['sections'].append(section); (root/'artifacts/script.json').write_text(json.dumps(script))
    timing=execution._read(root/'artifacts/compiled_request-original.json')['timing']
    base['native_params']['duration']=2
    originals={}
    for sid in ('entry','other'):
        candidate=copy.deepcopy(base); candidate['output_path']=str(root/(sid+'.mp4'))
        originals[sid],_=mcp_compile(root,candidate,sid,'original-'+sid,timing)
    policy=copy.deepcopy(policy); policy['caps']={'max_total_attempts':4,'max_attempts_per_shot':2,'max_repair_attempts':2}
    provider=next(p for p in policy['providers'] if p['id']=='openart_mcp')
    provider['routes']=[{'model':m,'mode':'text2video'} for m in ('pixverseV6','wan3-0')]
    policy['model_selection_intents']={sid:{'mode':'auto','approved_pool':[
        {'provider':'openart_mcp','tool':'openart_mcp_video','model':m} for m in ('pixverseV6','wan3-0')]} for sid in originals}
    policy,_=install_existing(root,policy,templates={sid:execution.planned_request_template(req,project_dir=root) for sid,req in originals.items()})
    (root/'production_scopes.json').write_text(json.dumps({'version':'1.0','scopes':[]}))
    enable(root)
    for sid,inputs in originals.items():
        scope=pa.derive_scope(root,inputs,provider='openart_mcp'); inputs['governance']['scope_id']=scope['id']
        jobs.prepare(root,attempt_id='original-'+sid,generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
        jobs.begin(root,'original-'+sid,authority_fn=execution.prepare_openart_mcp_handoff)
        jobs.receive(root,'original-'+sid,outcome={'historyId':'history-original-'+sid})
    return root,policy,originals,timing,jobs


def mcp_select(root,sid,aid):
    from tools.analysis.frame_sampler import FrameSampler
    from lib.shot_contract import selection_digest,UPSTREAM_PREDICATES
    from tests.integration.test_first_pass_workflow import attestation
    output=execution.load_attempt_result(root,aid)['output']
    sampled=FrameSampler().execute({'input_path':output['path'],'strategy':'timestamps','timestamps':[1.9],
        'output_dir':str(root/('frames-'+aid)),'format':'png'})
    assert sampled.success,sampled.error
    frame=sampled.data['frames'][0]['path']
    selection={'attempt_id':aid,'output':output,'outgoing_frame':{'path':frame,'sha256':file_sha256(frame)}}
    selection['review']=attestation(selection_digest(selection),UPSTREAM_PREDICATES,execution._read(root/'project.json')['story_revision'])
    return execution.record_selection(root,sid,selection)


def bind_mcp_upstream(root,sid,selection):
    from lib.shot_contract import review_digest
    from tests.lib.test_shot_contract import refresh
    contract=execution.load_shot_contract(root)
    next(s for s in contract['shots'] if s['id']==sid)['upstream']=[{'shot_id':'entry','attempt_id':selection['attempt_id'],
        'output_sha256':selection['output']['sha256'],'outgoing_frame_sha256':selection['outgoing_frame']['sha256'],
        'review_sha256':review_digest(selection['review'])}]
    refresh(contract);(root/'artifacts/shot_contract.json').write_text(json.dumps(contract))


def collect_fixture(root, jobs, aid, source, monkeypatch):
    from tests.integration.test_openart_mcp_governance import install_fake_download
    state=jobs.attempt_state(root,aid)
    jobs.record_status(root,aid,result={'historyId':state['history_id'],'status':'COMPLETED',
        'resources':[{'id':'resource-'+aid,'mediaType':'video','status':'COMPLETED','url':'https://fixture.invalid/'+aid+'.mp4'}]})
    install_fake_download(monkeypatch,jobs,source)
    receipt=jobs.download_original(root,aid)
    return jobs.collect(root,aid,downloaded_path=receipt['downloaded_path'])


def test_mixed_mcp_batch_real_successor_and_interrupted_replay(mcp_two, monkeypatch, tmp_path):
    root,policy,originals,timing,jobs=mcp_two
    originals_media=real_av_clip(tmp_path.resolve()/'original.mp4',seconds=2)
    for sid in originals:
        collect_fixture(root,jobs,'original-'+sid,originals_media,monkeypatch)
    cut=draft.compose_first_cut(root)
    assert cut['status']=='complete' and cut['technical']['has_audio']
    items=[]
    for sid,model in (('entry','pixverseV6'),('other','wan3-0')):
        inputs=copy.deepcopy(originals[sid]); inputs.update(model=model,output_path=str(root/('repair-'+sid+'.mp4')))
        inputs,_=mcp_compile(root,inputs,sid,'repair-'+sid,timing)
        items.append({'item_id':sid,'shot_id':sid,'source_attempt_id':'original-'+sid,
                      'selected_route':{'provider':'openart_mcp','tool':'openart_mcp_video','model':model,'mode':'text2video'},
                      'changes':'Creator requests clearer framing, even with cosmetic/pass review.', 'inputs':inputs})
    # Cosmetic review stays cosmetic; no critical verdict is invented.
    output=execution.load_attempt_result(root,'original-entry')['output']
    from tests.integration.test_first_pass_workflow import attestation
    review=attestation(output['sha256'],['grain'],policy['story_revision'])
    review.update(status='fail',predicates=[{'name':'grain','status':'fail','severity':'cosmetic','evidence':'Synthetic cosmetic grain.'}])
    execution.record_rejection(root,'original-entry',review)
    # Current export findings need to be disclosed before exact intent is recorded.
    cut=draft.compose_first_cut(root)
    batches.record_creator_repair_batch(root,batch_id='mixed',cut=cut_binding(cut),items=items,evidence=approval(root,'creator-mixed'))
    first=batches.prepare_repair_item(root,'mixed','entry')
    assert first['state']=='planned' and first['attempt_id'] is None
    # Interrupt after derivation and before binding persistence: reuse the exact scope.
    scope_path=root/'production_repair_batches/mixed/entry.scope.json'
    scope_path.unlink()
    recovered=batches.prepare_repair_item(root,'mixed','entry')
    assert recovered['scope_id']==first['scope_id']
    receipt=batches.resume_repair_batch(root,'mixed')
    assert receipt['errors']==[],receipt
    assert len(receipt['actions'])==2
    assert {i['state'] for i in receipt['items']}=={'uncertain'}
    assert {i['original_status'] for i in receipt['items']}=={'uncertain'}
    assert batches.resume_repair_batch(root,'mixed')['actions']==[]
    repaired=real_av_clip(tmp_path.resolve()/'repaired.mp4',seconds=2,freq=660)
    for row in receipt['items']:
        jobs.receive(root,row['attempt_id'],outcome={'historyId':'history-'+row['attempt_id']})
        collect_fixture(root,jobs,row['attempt_id'],repaired,monkeypatch)
    successor=draft.compose_first_cut(root)
    assert successor['status']=='complete' and successor['technical']['has_audio']
    assert [c['attempt_id'] for c in successor['clips']]==[i['attempt_id'] for i in receipt['items']]
    assert all(c['review']=='unknown' for c in successor['clips'])
    assert controls.effective_controls(root)['alternate_repair']=='different_provider_or_media_model'
    assert controls.generation_usage(root)['per_shot']=={'entry':2,'other':2}
    assert controls.generation_usage(root)['total']==4
    assert batches.resume_repair_batch(root,'mixed')['actions']==[]
    assert len(jobs.list_attempts(root))==4
    acceptance=approval(root,'accept-repaired-successor')
    draft.record_first_cut_acceptance(root,cut_binding(successor),accepted_by=acceptance['approved_by'],
        evidence={k:v for k,v in acceptance.items() if k!='approved_by'})
    assert batches.repair_batch_status(root,'mixed')['closed']
    recomposed=draft.compose_first_cut(root)
    assert [c['attempt_id'] for c in recomposed['clips']]==[i['attempt_id'] for i in receipt['items']]
    assert not any(c['strict_selected'] for c in draft.first_cut_candidates(root))
    assert len(jobs.list_attempts(root))==4


def test_partial_native_batch_resumes_only_unsubmitted_selected_items(p, monkeypatch):
    enable(p.root)
    p.complete_shots()
    cut=draft.compose_first_cut(p.root,compose=fake_cut)
    selected=execution.load_selected_attempts(p.root)
    before=copy.deepcopy(selected)
    items=[batch_item(p,sid,selected[sid]['attempt_id'],sid) for sid in ('entry','payoff')]
    batches.record_creator_repair_batch(p.root,batch_id='partial',cut=cut_binding(cut),items=items,evidence=approval(p.root,'partial-intent'))
    for item in items:
        exact_scope(p,item,'repair-'+item['shot_id'])
        batches.prepare_repair_item(p.root,'partial',item['item_id'],scope_id='repair-'+item['shot_id'])
    bound=copy.deepcopy(items[0]['inputs']); bound['governance']['scope_id']='repair-entry'
    execution.preflight(p.selector,bound)
    invoke=p.selector.execute
    calls=[]
    def interrupted(inputs):
        calls.append(inputs['governance']['shot_id'])
        if len(calls)==2:
            raise KeyboardInterrupt('Synthetic interruption before canonical reservation')
        return invoke(inputs)
    monkeypatch.setattr(p.selector,'execute',interrupted)
    with pytest.raises(KeyboardInterrupt):
        batches.resume_repair_batch(p.root,'partial',tools={'grok_cli':p.selector})
    status=batches.repair_batch_status(p.root,'partial')
    assert [i['state'] for i in status['items']]==['completed','planned']
    assert status['affected_dependency_shots']==['interior']
    monkeypatch.setattr(p.selector,'execute',invoke)
    receipt=batches.resume_repair_batch(p.root,'partial',tools={'grok_cli':p.selector})
    assert receipt['errors']==[] and len(receipt['actions'])==1
    assert receipt['actions'][0]['item_id']=='payoff'
    assert controls.generation_usage(p.root)['per_shot']=={'entry':2,'interior':1,'payoff':2}
    assert execution.load_selected_attempts(p.root)==before
    assert len(p.transport.native_requests)==5


def test_cancel_grok_blocks_planned_batch_without_substitute_authority(p):
    enable(p.root)
    first=p.generate('entry')
    cut=draft.compose_first_cut(p.root,compose=fake_cut)
    item=batch_item(p,'entry',first.data['production_attempt_id'])
    batches.record_creator_repair_batch(p.root,batch_id='cancelled',cut=cut_binding(cut),items=[item],evidence=approval(p.root,'intent'))
    exact_scope(p,item,'prepared-scope')
    batches.prepare_repair_item(p.root,'cancelled','item',scope_id='prepared-scope')
    result=controls.restrict_episode_provider(p.root,['grok_cli'],evidence=approval(p.root,'cancel-grok'),reason='Creator cancelled further Grok generation')
    assert result['remote_cancellation'] is False
    receipt=batches.resume_repair_batch(p.root,'cancelled',tools={'grok_cli':p.selector})
    assert 'restricted' in receipt['errors'][0]['error']
    assert receipt['items'][0]['state']=='planned' and len(p.transport.native_requests)==1
    assert execution.load_attempt_result(p.root,first.data['production_attempt_id'])['status']=='generated'


def test_collected_mcp_originals_are_current_first_cut_candidates(mcp_two,monkeypatch,tmp_path):
    root,_,_,_,jobs=mcp_two
    source=real_av_clip(tmp_path.resolve()/'source.mp4',seconds=2)
    for sid in ('entry','other'):
        collect_fixture(root,jobs,'original-'+sid,source,monkeypatch)
    raw=jobs.list_attempts(root)
    assert all('story_revision' not in a for a in raw)
    rows=execution._attempts(root)
    assert all(a['story_revision']==a['scope_snapshot']['story_revision'] for a in rows)
    candidates=draft.first_cut_candidates(root)
    assert [a['attempt_id'] for a in candidates]==['original-entry','original-other']
    assert all(a['status']=='candidate' and a['strict_selected'] is False for a in candidates)
    assert controls.generation_usage(root)['total']==2


def test_accepted_cut_keeps_original_while_submitted_mcp_repairs_collect(mcp_two,monkeypatch,tmp_path):
    root,_,originals,timing,jobs=mcp_two
    source=real_av_clip(tmp_path.resolve()/'source.mp4',seconds=2)
    for sid in originals:
        collect_fixture(root,jobs,'original-'+sid,source,monkeypatch)
    cut=draft.compose_first_cut(root)
    inputs=copy.deepcopy(originals['entry']); inputs['output_path']=str(root/'late-repair.mp4')
    inputs,_=mcp_compile(root,inputs,'entry','late-repair',timing)
    item={'item_id':'entry','shot_id':'entry','source_attempt_id':'original-entry',
          'selected_route':{'provider':'openart_mcp','tool':'openart_mcp_video','model':inputs['model'],'mode':inputs['mode']},
          'changes':'Creator explicitly improves the shot.', 'inputs':inputs}
    batches.record_creator_repair_batch(root,batch_id='in-flight',cut=cut_binding(cut),items=[item],evidence=approval(root,'intent'))
    receipt=batches.resume_repair_batch(root,'in-flight')
    assert receipt['errors']==[] and len(receipt['actions'])==1
    aid=receipt['items'][0]['attempt_id']
    accept=approval(root,'accept-cut')
    draft.record_first_cut_acceptance(root,cut_binding(cut),accepted_by=accept['approved_by'],evidence={k:v for k,v in accept.items() if k!='approved_by'})
    jobs.receive(root,aid,outcome={'historyId':'history-'+aid})
    collect_fixture(root,jobs,aid,real_av_clip(tmp_path.resolve()/'repair.mp4',seconds=2,freq=660),monkeypatch)
    status=batches.repair_batch_status(root,'in-flight')
    assert status['closed'] and status['items'][0]['state']=='completed'
    assert draft.first_cut_candidates(root)[0]['attempt_id']=='original-entry'
    assert batches.resume_repair_batch(root,'in-flight')['actions']==[]
    assert controls.generation_usage(root)['total']==3


def test_creator_decision_keeps_exact_intent_and_allows_same_model_without_defect():
    from lib.video_model_selection import validate_creator_repair_decision
    intent={'mode':'exact','provider':'openart_mcp','model':'pixverseV6','approved_pool':[
        {'provider':'openart_mcp','model':m,'tool':'openart_mcp_video'} for m in ('pixverseV6','wan3-0')]}
    item={'changes':'The creator requests an improvement despite passing review.',
          'selected_route':{'provider':'openart_mcp','tool':'openart_mcp_video','model':'pixverseV6','mode':'text2video'}}
    valid=validate_creator_repair_decision(item,proposed_request={'model':'pixverseV6','mode':'text2video'},intent=intent)
    assert valid['grants_authority'] is False
    alternate=copy.deepcopy(item); alternate['selected_route']['model']='wan3-0'
    with pytest.raises(ValueError,match='existing exact model intent'):
        validate_creator_repair_decision(alternate,proposed_request={'model':'wan3-0','mode':'text2video'},intent=intent)


def test_unplayable_completed_creator_output_does_not_replace_strict_original(p,monkeypatch):
    enable(p.root)
    first=p.generate('entry'); original=p.select('entry',first)
    cut=draft.compose_first_cut(p.root,compose=fake_cut)
    item=batch_item(p,'entry',original['attempt_id'])
    batches.record_creator_repair_batch(p.root,batch_id='unplayable',cut=cut_binding(cut),items=[item],evidence=approval(p.root,'intent'))
    exact_scope(p,item,'repair-scope'); batches.prepare_repair_item(p.root,'unplayable','item',scope_id='repair-scope')
    receipt=batches.resume_repair_batch(p.root,'unplayable',tools={'grok_cli':p.selector})
    assert receipt['errors']==[]
    aid=receipt['items'][0]['attempt_id']
    actual_probe=draft._probe
    def reject(path):
        if str(path)==item['inputs']['output_path']:
            raise ValueError('Synthetic unusable replacement media')
        return actual_probe(path)
    monkeypatch.setattr(draft,'_probe',reject)
    assert draft.first_cut_candidates(p.root)[0]['attempt_id']==original['attempt_id']
    assert execution.load_selected_attempts(p.root)['entry']==original
    assert batches.resume_repair_batch(p.root,'unplayable',tools={'grok_cli':p.selector})['actions']==[]

from tests.lib.test_trimmed_derivative import trimmed  # noqa: F401


def test_creator_repair_can_target_canonical_derived_trim_from_current_cut(trimmed):
    from lib.shot_contract import UPSTREAM_PREDICATES, selection_digest
    from tests.integration.test_first_pass_workflow import attestation
    value,aid,directory,record,_=trimmed
    execution.record_derived_edit(value.root,record)
    selection={'attempt_id':aid,'output':record['output'],'outgoing_frame':record['outgoing_frame']}
    selection['review']=attestation(selection_digest(selection),UPSTREAM_PREDICATES)
    execution.record_selection(value.root,'entry',selection)
    enable(value.root)
    cut=draft.compose_first_cut(value.root)
    assert cut['export'] is not None
    assert cut['clips'][0]['output']['sha256']==record['output']['sha256']
    native=execution.load_attempt_result(value.root,aid)['output']
    assert native['sha256']!=record['output']['sha256']
    item=batch_item(value,'entry',aid,'derived-source')
    batches.record_creator_repair_batch(value.root,batch_id='derived-intent',cut=cut_binding(cut),items=[item],evidence=approval(value.root,'creator-trimmed'))
    exact_scope(value,item,'creator-derived')
    batches.prepare_repair_item(value.root,'derived-intent','derived-source',scope_id='creator-derived')
    binding=batches.creator_repair_binding(value.root,'derived-intent','derived-source')
    retained=batches.validate_creator_repair_intent(value.root,binding,shot_id='entry',provider='grok_cli',model=None,
        replaces_attempt_ids=[aid],inputs=item['inputs'])
    assert retained['source_output']==cut['clips'][0]['output']
    assert execution.load_attempt_result(value.root,aid)['output']==native
    assert controls.generation_usage(value.root)['total']==1


@pytest.mark.parametrize('accept_partial', [False, True])
def test_dependent_item_uses_canonical_template_only_after_reviewed_promotion(p, accept_partial):
    from lib.shot_contract import ASSET_PREDICATES
    from tests.integration.test_first_pass_workflow import attestation,sign_planning_reviews
    enable(p.root)
    shot=p.contract['shots'][1]
    shot.update(initial_state='Same exterior doorway, empty after completed entry.',
        dominant_action='The empty paper doorway settles shut.',
        completed_end_state='The doorway is fully closed; no person or parcel remains outside.',
        cast_ids=[],required_visible_speakers=[],dialogue=[],asset_ids=['interior-start','interior-end'],
        transition={'type':'continuous','rationale':'Selected actual outgoing frame is the continuation source.'})
    start=next(a for a in p.contract['assets'] if a['id']=='interior-start')
    start['upstream_source']={'shot_id':'entry','role':'outgoing_frame'}; start['cast_ids']=[]
    for key in ('path','sha256','review'): start.pop(key)
    next(a for a in p.contract['assets'] if a['id']=='interior-end')['cast_ids']=[]
    sign_planning_reviews(p.contract); p.persist_contract()
    p.inputs['interior']['prompt']='Same-location continuation: empty paper doorway settles shut.'
    del p.inputs['interior']['reference_image_paths']; del p.inputs['interior']['voices']
    deferred=copy.deepcopy(p.inputs['interior'])
    deferred['reference_image_path']={'$upstream':{'shot_id':'entry','role':'outgoing_frame'}}
    p.scope['requests']['interior']=execution.planned_request_template(deferred,project_dir=p.root)
    p.scope['approval_plan_sha256']=execution.approval_plan_digest(p.contract);p.persist_scope()
    first=p.generate('entry'); selected=p.select('entry',first)
    p.inputs['interior']['reference_image_path']=selected['outgoing_frame']['path']
    start.update(path=selected['outgoing_frame']['path'],sha256=selected['outgoing_frame']['sha256'],
        review=attestation(selected['outgoing_frame']['sha256'],ASSET_PREDICATES))
    p.bind_upstream('interior')
    original_child=p.generate('interior');child_selection=p.select('interior',original_child)
    cut=draft.compose_first_cut(p.root,compose=fake_cut)
    ordinary=draft._candidate(p.root,'interior',child_selection['attempt_id'],child_selection['output'],
        p.story['story_revision'],execution.load_selected_attempts(p.root),historical_source=True)
    assert ordinary['strict_selected'] and not any(f['name']=='continuity_source_changed' for f in ordinary['findings'])
    entry=batch_item(p,'entry',selected['attempt_id'],'entry')
    child=batch_item(p,'interior',child_selection['attempt_id'],'interior')
    child['inputs']['reference_image_path']={'$upstream':{'shot_id':'entry','role':'outgoing_frame'}}
    batch_items=[entry,child]
    batches.record_creator_repair_batch(p.root,batch_id='dependent',cut=cut_binding(cut),items=batch_items,evidence=approval(p.root,'dependent-intent'))
    exact_scope(p,entry,'repair-entry')
    child_scope=copy.deepcopy(p.scope)
    child_scope.update(id='repair-child',phase='repair',replaces_attempt_ids=[child['source_attempt_id']],
        requests={'interior':execution.planned_request_template(child['inputs'],project_dir=p.root)},attempts_per_shot={'interior':1})
    child_scope['approval_plan_sha256']=execution.approval_plan_digest(p.contract)
    scopes=execution._read(p.root/'production_scopes.json');scopes['scopes'].append(child_scope)
    (p.root/'production_scopes.json').write_text(json.dumps(scopes))
    batches.prepare_repair_item(p.root,'dependent','entry',scope_id='repair-entry')
    pending=batches.prepare_repair_item(p.root,'dependent','interior',scope_id='repair-child')
    assert pending['state']=='planned' and pending['blocked_by']==['entry']
    first_resume=batches.resume_repair_batch(p.root,'dependent',tools={'grok_cli':p.selector})
    assert first_resume['errors']==[] and [a['item_id'] for a in first_resume['actions']]==['entry']
    assert controls.generation_usage(p.root)['per_shot']=={'entry':2,'interior':1}
    assert batches.resume_repair_batch(p.root,'dependent',tools={'grok_cli':p.selector})['actions']==[]
    repair=first_resume['items'][0]
    from tests.integration.test_first_pass_workflow import PIXEL
    from lib.shot_contract import UPSTREAM_PREDICATES,selection_digest
    frame=p.root/'assets/images/entry-repaired-outgoing.png';frame.write_bytes(PIXEL+repair['attempt_id'].encode())
    promoted={'attempt_id':repair['attempt_id'],'output':{'path':entry['inputs']['output_path'],'sha256':file_sha256(entry['inputs']['output_path'])},
        'outgoing_frame':{'path':str(frame),'sha256':file_sha256(frame)}}
    promoted['review']=attestation(selection_digest(promoted),UPSTREAM_PREDICATES)
    execution.record_selection(p.root,'entry',promoted)
    start.update(path=promoted['outgoing_frame']['path'],sha256=promoted['outgoing_frame']['sha256'],
        review=attestation(promoted['outgoing_frame']['sha256'],ASSET_PREDICATES))
    p.bind_upstream('interior')
    from lib.production_provenance import validate_attempt_provenance
    with pytest.raises(ValueError,match='upstream selection changed'):
        validate_attempt_provenance(p.root,child_selection['attempt_id'],shot_id='interior',story_revision=p.story['story_revision'],expected_output=child_selection['output'])
    partial=draft.compose_first_cut(p.root,compose=fake_cut)
    assert len(partial['clips'])==2
    old_child=next(c for c in partial['clips'] if c['shot_id']=='interior')
    assert old_child['attempt_id']==child_selection['attempt_id']
    assert old_child['review']=='unknown' and any(f['name']=='continuity_source_changed' for f in old_child['findings'])
    assert draft.first_cut_status(p.root,cut_binding(partial))=={'status':'current','reasons':[]}
    if accept_partial:
        accepted=approval(p.root,'accept-partial')
        binding=draft.record_first_cut_acceptance(p.root,cut_binding(partial),accepted_by=accepted['approved_by'],
            evidence={k:v for k,v in accepted.items() if k!='approved_by'})
        assert draft.first_cut_acceptance_status(p.root,binding)['status']=='current'
        closed=batches.repair_batch_status(p.root,'dependent')
        assert closed['closed'] and next(i for i in closed['items'] if i['shot_id']=='interior')['closed']
        assert batches.resume_repair_batch(p.root,'dependent',tools={'grok_cli':p.selector})['actions']==[]
        assert controls.generation_usage(p.root)['per_shot']=={'entry':2,'interior':1}
        return
    after=batches.resume_repair_batch(p.root,'dependent',tools={'grok_cli':p.selector})
    assert after['errors']==[],after
    assert [a['item_id'] for a in after['actions']]==['interior']
    assert controls.generation_usage(p.root)['per_shot']=={'entry':2,'interior':2}
    assert execution.load_selected_attempts(p.root)['interior']==child_selection
    assert len(p.transport.native_requests)==4
    assert file_sha256(p.transport.native_requests[-1]['arguments']['first_frame'])==promoted['outgoing_frame']['sha256']


def test_accept_prepared_policy_intent_allows_new_batch_with_unused_remaining_slot(mcp_two,monkeypatch,tmp_path):
    from lib import production_autonomy as pa
    root,policy,originals,timing,jobs=mcp_two
    source=real_av_clip(tmp_path.resolve()/'source.mp4',seconds=2)
    for sid in originals:
        collect_fixture(root,jobs,'original-'+sid,source,monkeypatch)
    cut=draft.compose_first_cut(root)
    def item(name):
        candidate=copy.deepcopy(originals['entry']);candidate['output_path']=str(root/(name+'.mp4'))
        candidate,_=mcp_compile(root,candidate,'entry',name,timing)
        return {'item_id':'entry','shot_id':'entry','source_attempt_id':'original-entry',
            'selected_route':{'provider':'openart_mcp','tool':'openart_mcp_video','model':candidate['model'],'mode':candidate['mode']},
            'changes':'Explicit creator instruction for '+name, 'inputs':candidate}
    old=item('old-planned')
    batches.record_creator_repair_batch(root,batch_id='old-planned',cut=cut_binding(cut),items=[old],evidence=approval(root,'old-intent'))
    prepared=batches.prepare_repair_item(root,'old-planned','entry')
    retained_scope=copy.deepcopy(next(s for s in execution._read(root/'production_scopes.json')['scopes'] if s['id']==prepared['scope_id']))
    assert prepared['state']=='planned' and prepared['attempt_id'] is None
    assert pa.root_attempt_counts(root,policy)['per_shot']['entry']==2
    accept=approval(root,'accept-prepared')
    draft.record_first_cut_acceptance(root,cut_binding(cut),accepted_by=accept['approved_by'],evidence={k:v for k,v in accept.items() if k!='approved_by'})
    assert pa.root_attempt_counts(root,policy)['per_shot']['entry']==1
    # A private original reservation still holds its slot even if no public
    # journal was published. Failure to read reservations cannot prove absence.
    from lib import provider_credit_ledger as ledger,openart_dispatch as dispatch
    with monkeypatch.context() as pending:
        pending.setattr(ledger,'read_existing_snapshot',lambda:{'reservations':[{
            'attempt_id':'private-original','binding_json':json.dumps({'project_root':str(root)})}]})
        pending.setattr(dispatch,'_manifest',lambda _:({'journal_records':{'request.json':{'scope_id':retained_scope['id']}}},None,None))
        assert not batches.closed_unsubmitted_creator_scope(root,retained_scope)
    with monkeypatch.context() as unreadable:
        unreadable.setattr(ledger,'read_existing_snapshot',lambda:(_ for _ in ()).throw(OSError('unavailable')))
        assert not batches.closed_unsubmitted_creator_scope(root,retained_scope)
    new=item('new-explicit')
    batches.record_creator_repair_batch(root,batch_id='new-explicit',cut=cut_binding(cut),items=[new],evidence=approval(root,'new-intent'))
    # Reproduce the pre-fix refusal and phantom open-scope charge without calls.
    with monkeypatch.context() as baseline:
        baseline.setattr(batches,'closed_unsubmitted_creator_scope',lambda *args:False)
        assert pa.root_attempt_counts(root,policy)['per_shot']['entry']==2
        with pytest.raises(ValueError,match='undispatched derived scope'):
            batches.prepare_repair_item(root,'new-explicit','entry')
    later=batches.prepare_repair_item(root,'new-explicit','entry')
    assert later['scope_id']!=prepared['scope_id']
    receipt=batches.resume_repair_batch(root,'new-explicit')
    assert receipt['errors']==[],receipt
    assert len(receipt['actions'])==1
    assert controls.generation_usage(root)['per_shot']['entry']==2
    assert next(s for s in execution._read(root/'production_scopes.json')['scopes'] if s['id']==retained_scope['id'])==retained_scope
    assert batches.closed_unsubmitted_creator_scope(root,retained_scope)
    assert not batches.closed_unsubmitted_creator_scope(root,next(s for s in execution._read(root/'production_scopes.json')['scopes'] if s['id']==later['scope_id']))


from tests.lib.test_openart_mcp_native import observations,source_project  # noqa: F401


@pytest.fixture
def mcp_dependent(source_project,monkeypatch,media_path):
    from lib import openart_mcp as mcp,openart_mcp_jobs as jobs,production_autonomy as pa,production_request as prep,production_provenance as provenance
    from tests.lib.test_production_autonomy import make_policy,install_existing
    from tests.lib.test_shot_contract import refresh
    root,assets,_,_=source_project;root=root.resolve()
    monkeypatch.setattr(mcp,'_ALLOW_FIXTURE_PRODUCTION',True)
    monkeypatch.setattr(provenance,'_ALLOW_OPENART_FIXTURE_PROVENANCE',True)
    marker=execution._read(root/'project.json');marker['pipeline_type']='cinematic';(root/'project.json').write_text(json.dumps(marker))
    contract=execution.load_shot_contract(root)
    contract['assets']=[a for a in contract['assets'] if a['id'] not in {'voice','motion'}]
    shot=contract['shots'][0];shot.update(duration_seconds=5,reference_mode='reference_guided')
    shot['asset_ids']=[a for a in shot['asset_ids'] if a not in {'voice','motion'}]
    other=copy.deepcopy(shot);other['id']='other';contract['shots'].append(other)
    refresh(contract);(root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    scenes={'version':'1.0','scenes':[{'id':sid,'type':'generated','description':'Synthetic MCP dependency',
        'start_seconds':n*5,'end_seconds':(n+1)*5,'script_section_id':'s'+str(n+1)} for n,sid in enumerate(('entry','other'))]}
    script={'version':'1.0','title':'Synthetic MCP dependency','total_duration_seconds':10,
        'sections':[{'id':'s'+str(n+1),'text':'The patient completes the reviewed action.',
        'start_seconds':n*5,'end_seconds':(n+1)*5} for n in range(2)]}
    (root/'artifacts/scene_plan.json').write_text(json.dumps(scenes));(root/'artifacts/script.json').write_text(json.dumps(script))
    review={'version':'1.0','review_id':'original','reviewer':'Offline source reviewer','status':'pass','subject_sha256':'0'*64,
        'evidence_kind':'fixture_only','predicates':[{'name':n,'status':'pass','severity':'critical','evidence':'Synthetic source review'} for n in sorted(prep.PREDICATES)]}
    review['composite_boards']=[{'role':'start_frame','sha256':next(a['sha256'] for a in contract['assets'] if a['id']=='start'),
        'members':[{k:a[k] for k in ('sha256','role','cast_ids')} for a in contract['assets']]}]
    (root/'artifacts/preparation_review-original.json').write_text(json.dumps(review))
    timing={'method':'segmented_estimate','duration_seconds':5,'language':'en','margin_seconds':.05,'rationale':'Synthetic timing',
        'overlap_policy':'serial','overlap_rationale':'Synthetic timing','segments':[],
        'action_windows':[{'source_pointer':'/shot_contract/shots/0/'+key,'value_sha256':prep.digest(shot[key]),
            'start_seconds':start,'end_seconds':end,'rationale':'Synthetic timing'} for key,start,end in [('dominant_action',0,3),('completed_end_state',3,4.95)]]}
    base={'project_dir':str(root),'model':'minimax-h3','mode':'element2video','operation':'reference_to_video',
        'native_params':{'duration':5,'resolution':'768P'},'openart_project_id':'fixture-project',
        'input_assets':[{'role':'reference_image',**assets['start']}]}
    originals={}
    for sid in ('entry','other'):
        candidate={**copy.deepcopy(base),'output_path':str(root/(sid+'.mp4'))}
        originals[sid],_=mcp_compile(root,candidate,sid,'original-'+sid,timing)
    contract['shots'][1]['upstream']=[{'shot_id':'entry'}];refresh(contract)
    (root/'artifacts/shot_contract.json').write_text(json.dumps(contract))
    policy=make_policy();policy.update(caps={'max_total_attempts':4,'max_attempts_per_shot':2,'max_repair_attempts':2},
        providers=[{'id':'openart_mcp','billing':'unknown_cost_no_ceiling','exposure_acknowledgement':'no_enforceable_credit_ceiling',
            'uid_sha256':mcp.account_summary()['uid_sha256'],'project_id':'fixture-project','routes':[{'model':'minimax-h3','mode':'element2video'}]}],
        locked={'cast':{},'dialogue':{},'sources':[],'story_predicates':[],'controls':{}},checkpoint_stages=[],
        flex={'duration_s':[],'resolution':[],'references':{'droppable_roles':[],'substitutes':[]}},
        model_selection_intents={sid:{'mode':'auto','approved_pool':[{'provider':'openart_mcp','tool':'openart_mcp_video','model':'minimax-h3'}]} for sid in originals})
    policy,_=install_existing(root,policy,templates={sid:execution.planned_request_template(req,project_dir=root) for sid,req in originals.items()})
    (root/'production_scopes.json').write_text(json.dumps({'version':'1.0','scopes':[]}))
    enable(root)
    for sid in ('entry','other'):
        if sid=='other':
            bind_mcp_upstream(root,'other',execution.load_selected_attempts(root)['entry'])
        originals[sid],_=mcp_compile(root,originals[sid],sid,'original-'+sid,timing)
        inputs=originals[sid];scope=pa.derive_scope(root,inputs,provider='openart_mcp');inputs['governance']['scope_id']=scope['id']
        jobs.prepare(root,attempt_id='original-'+sid,generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
        jobs.begin(root,'original-'+sid,authority_fn=execution.prepare_openart_mcp_handoff)
        jobs.receive(root,'original-'+sid,outcome={'historyId':'history-original-'+sid})
        if sid=='entry':
            media=real_av_clip(root/'entry-source.mp4',seconds=5);collect_fixture(root,jobs,'original-entry',media,monkeypatch)
            mcp_select(root,'entry','original-entry')
    return root,policy,originals,timing,jobs


def test_mcp_historical_source_replays_exact_selection_history_after_promotion(mcp_dependent,monkeypatch,tmp_path):
    from lib.production_provenance import validate_attempt_provenance,validate_creator_repair_source_provenance
    root,policy,originals,timing,jobs=mcp_dependent
    source=real_av_clip(tmp_path.resolve()/'dependent-source.mp4',seconds=5)
    collect_fixture(root,jobs,'original-other',source,monkeypatch)
    original_child=mcp_select(root,'other','original-other')
    old_entry=execution.load_selected_attempts(root)['entry']
    cut=draft.compose_first_cut(root)
    assert cut['status']=='complete' and len(cut['clips'])==2
    # Observing the later shot's dependency does not invalidate entry footage.
    validate_attempt_provenance(root,'original-entry',shot_id='entry',story_revision=policy['story_revision'],expected_output=old_entry['output'])
    inputs=copy.deepcopy(originals['entry']);inputs['output_path']=str(root/'repair-entry.mp4')
    inputs,_=mcp_compile(root,inputs,'entry','repair-entry',timing)
    item={'item_id':'entry','shot_id':'entry','source_attempt_id':'original-entry',
        'selected_route':{'provider':'openart_mcp','tool':'openart_mcp_video','model':inputs['model'],'mode':inputs['mode']},
        'changes':'Explicit clearer creator framing.', 'inputs':inputs}
    batches.record_creator_repair_batch(root,batch_id='mcp-dependent',cut=cut_binding(cut),items=[item],evidence=approval(root,'mcp-dependent'))
    receipt=batches.resume_repair_batch(root,'mcp-dependent')
    assert receipt['errors']==[],receipt
    aid=receipt['items'][0]['attempt_id']
    jobs.receive(root,aid,outcome={'historyId':'history-'+aid})
    repaired=real_av_clip(tmp_path.resolve()/'promoted-source.mp4',seconds=5,freq=660)
    collect_fixture(root,jobs,aid,repaired,monkeypatch)
    promoted=mcp_select(root,'entry',aid);bind_mcp_upstream(root,'other',promoted)
    revision=policy['story_revision']
    with pytest.raises(ValueError,match='source/reference/upstream changed|source/reference/review/upstream|historical named preparation'):
        validate_attempt_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    proof=validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    assert proof['historical_source_only']
    partial=draft.compose_first_cut(root)
    child=next(c for c in partial['clips'] if c['shot_id']=='other')
    assert child['attempt_id']=='original-other' and child['review']=='unknown'
    assert not next(c for c in draft.first_cut_candidates(root) if c['shot_id']=='other')['strict_selected']
    assert any(f['name']=='continuity_source_changed' for f in child['findings'])
    assert draft.first_cut_status(root,cut_binding(partial))['status']=='current'
    history=[p for p in (root/'production_selections').glob('*.json') if execution._read(p).get('selection')==old_entry]
    assert len(history)==1
    raw=history[0].read_bytes()
    duplicate=history[0].with_name('duplicate.json');duplicate.write_bytes(raw)
    assert validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])['historical_source_only']
    duplicate.unlink();history[0].unlink()
    with pytest.raises(ValueError,match='original upstream selection'):
        validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    history[0].write_bytes(raw)
    changed=execution._read(history[0]);changed['selection']['review']['reviewer']='Changed original reviewer'
    history[0].write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='original upstream selection'):
        validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    history[0].write_bytes(raw)
    frame=root/old_entry['outgoing_frame']['path'];old_bytes=frame.read_bytes();frame.write_bytes(b'changed frame')
    with pytest.raises(ValueError,match='bytes|hash|sha256'):
        validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    frame.write_bytes(old_bytes)
    # Same-shot semantics and static references stay current under source-only
    # replay; only the selected upstream identity is allowed to differ.
    from tests.lib.test_shot_contract import refresh
    contract_path=root/'artifacts/shot_contract.json';contract_raw=contract_path.read_bytes()
    changed=execution.load_shot_contract(root);changed['shots'][1]['dominant_action']+=' New creative action.'
    refresh(changed);contract_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='planning'):
        validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    contract_path.write_bytes(contract_raw)
    changed=execution.load_shot_contract(root);changed['assets'][0]['review']['reviewer']='Changed static reviewer'
    contract_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='static reference review/bytes|planning'):
        validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    contract_path.write_bytes(contract_raw)
    script_path=root/'artifacts/script.json';script_raw=script_path.read_bytes()
    changed=execution._read(script_path);changed['sections'][1]['text']+=' Changed line.';script_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='source/reference/upstream changed|planning'):
        validate_creator_repair_source_provenance(root,'original-other',shot_id='other',story_revision=revision,expected_output=original_child['output'])
    script_path.write_bytes(script_raw)
    candidate=copy.deepcopy(originals['other']);candidate['output_path']=str(root/'repair-other.mp4')
    candidate,_=mcp_compile(root,candidate,'other','repair-other',timing)
    child_item={'item_id':'other','shot_id':'other','source_attempt_id':'original-other',
        'selected_route':{'provider':'openart_mcp','tool':'openart_mcp_video','model':candidate['model'],'mode':candidate['mode']},
        'changes':'Repair the exact historical clip with current reviewed continuity.', 'inputs':candidate}
    batches.record_creator_repair_batch(root,batch_id='mcp-child',cut=cut_binding(partial),items=[child_item],evidence=approval(root,'mcp-child'))
    batches.prepare_repair_item(root,'mcp-child','other')
    assert controls.generation_usage(root)['per_shot']=={'entry':2,'other':1}
