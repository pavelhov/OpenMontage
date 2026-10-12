"""Offline MCP originals remain attributable across unrelated planning edits."""
import copy
import json

import pytest

from lib import production_draft as draft, production_execution as execution
from lib.production_provenance import (validate_attempt_provenance,
                                      historical_source_provenance)
from lib.shot_contract import file_sha256
from tests.lib import test_first_cut_historical_mcp as fixtures
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401
from tests.lib.test_episode_production_controls import approval

def enable_first_cut(root):
    from lib import episode_production_controls as controls
    return controls.record_episode_controls(root, {
        "max_generations_per_shot": 1, "alternate_repair": "disabled",
        "access_fallback": False, "first_cut": {"enabled": True},
    }, evidence=approval(root))

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
    policy=copy.deepcopy(policy); policy['caps']={'max_total_attempts':4,'max_attempts_per_shot':1,'max_repair_attempts':0}
    provider=next(p for p in policy['providers'] if p['id']=='openart_mcp')
    provider['routes']=[{'model':m,'mode':'text2video'} for m in ('pixverseV6','wan3-0')]
    policy['model_selection_intents']={sid:{'mode':'auto','approved_pool':[
        {'provider':'openart_mcp','tool':'openart_mcp_video','model':m} for m in ('pixverseV6','wan3-0')]} for sid in originals}
    policy,_=install_existing(root,policy,templates={sid:execution.planned_request_template(req,project_dir=root) for sid,req in originals.items()})
    (root/'production_scopes.json').write_text(json.dumps({'version':'1.0','scopes':[]}))
    enable_first_cut(root)
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
    policy=make_policy();policy.update(caps={'max_total_attempts':4,'max_attempts_per_shot':1,'max_repair_attempts':0},
        providers=[{'id':'openart_mcp','billing':'unknown_cost_no_ceiling','exposure_acknowledgement':'no_enforceable_credit_ceiling',
            'uid_sha256':mcp.account_summary()['uid_sha256'],'project_id':'fixture-project','routes':[{'model':'minimax-h3','mode':'element2video'}]}],
        locked={'cast':{},'dialogue':{},'sources':[],'story_predicates':[],'controls':{}},checkpoint_stages=[],
        flex={'duration_s':[],'resolution':[],'references':{'droppable_roles':[],'substitutes':[]}},
        model_selection_intents={sid:{'mode':'auto','approved_pool':[{'provider':'openart_mcp','tool':'openart_mcp_video','model':'minimax-h3'}]} for sid in originals})
    policy,_=install_existing(root,policy,templates={sid:execution.planned_request_template(req,project_dir=root) for sid,req in originals.items()})
    (root/'production_scopes.json').write_text(json.dumps({'version':'1.0','scopes':[]}))
    enable_first_cut(root)
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


from tests.lib.test_openart_mcp_native import observations, source_project  # noqa: F401
from tests.integration.test_openart_first_pass_workflow import media_path, real_av_clip  # noqa: F401
from tests.lib.test_shot_contract import refresh


def save(path, value):
    mode = path.stat().st_mode & 0o777
    path.chmod(mode | 0o200)
    path.write_text(json.dumps(value))
    path.chmod(mode)


@pytest.fixture
def retained(source_project, monkeypatch, media_path, tmp_path):
    from tests.lib import test_shot_contract
    compile_original = fixtures.mcp_compile
    refresh_original = test_shot_contract.refresh
    added = False

    def refresh_with_unrelated_shot(contract):
        if [shot['id'] for shot in contract['shots']] == ['entry', 'other']:
            root = source_project[0]
            board = copy.deepcopy(next(a for a in contract['assets'] if a['id'] == 'start'))
            board.update(id='unrelated-board', path='assets/unrelated-board.svg', cast_ids=[])
            (root / board['path']).write_bytes((root / 'assets/start.svg').read_bytes())
            unrelated = copy.deepcopy(contract['shots'][0])
            unrelated.update(id='unrelated', asset_ids=['unrelated-board'], upstream=[])
            contract['assets'].append(board)
            contract['shots'].append(unrelated)
        return refresh_original(contract)

    def compile_with_unrelated_shot(root, *args):
        nonlocal added
        if not added:
            added = True
            scenes = execution._read(root / 'artifacts/scene_plan.json')
            scenes['scenes'].append({**copy.deepcopy(scenes['scenes'][0]), 'id': 'unrelated',
                                    'start_seconds': 10, 'end_seconds': 15, 'script_section_id': 's3'})
            save(root / 'artifacts/scene_plan.json', scenes)
            script = execution._read(root / 'artifacts/script.json')
            script['sections'].append({**copy.deepcopy(script['sections'][0]), 'id': 's3',
                                       'start_seconds': 10, 'end_seconds': 15})
            script['total_duration_seconds'] = 15
            save(root / 'artifacts/script.json', script)
            review = execution._read(root / 'artifacts/preparation_review-original.json')
            for board in review['composite_boards']:
                board['members'] = [member for member in board['members'] if member['cast_ids']]
            save(root / 'artifacts/preparation_review-original.json', review)
        return compile_original(root, *args)

    monkeypatch.setattr(fixtures, 'mcp_compile', compile_with_unrelated_shot)
    monkeypatch.setattr(test_shot_contract, 'refresh', refresh_with_unrelated_shot)
    root, policy, originals, timing, jobs = fixtures.mcp_dependent.__wrapped__(
        source_project, monkeypatch, media_path)
    media = real_av_clip(tmp_path / 'retained-child.mp4', seconds=5)
    fixtures.collect_fixture(root, jobs, 'original-other', media, monkeypatch)
    child = fixtures.mcp_select(root, 'other', 'original-other')
    upstream = copy.deepcopy(execution.load_selected_attempts(root)['entry'])
    return root, policy['story_revision'], jobs, child, upstream


def check_original(retained, *, historical=True):
    root, revision, _, child, _ = retained
    validate = historical_source_provenance if historical else validate_attempt_provenance
    return validate(root, 'original-other', shot_id='other', story_revision=revision,
                    expected_output=child['output'])


def change_unrelated(root, change):
    if change.startswith('contract'):
        contract = execution.load_shot_contract(root)
        if change == 'contract_action':
            contract['shots'][2]['dominant_action'] += ' Unrelated new staging.'
        else:
            board = next(a for a in contract['assets'] if a['id'] == 'unrelated-board')
            (root / board['path']).write_bytes(b'Synthetic replacement unrelated board')
            board['sha256'] = file_sha256(root / board['path'])
            board['review']['subject_sha256'] = board['sha256']
        refresh(contract)
        save(root / 'artifacts/shot_contract.json', contract)
    elif change == 'script_section':
        script = execution._read(root / 'artifacts/script.json')
        script['sections'][2]['text'] += ' Unrelated new context.'
        save(root / 'artifacts/script.json', script)
    else:
        scene = execution._read(root / 'artifacts/scene_plan.json')
        if change == 'scene_description':
            scene['scenes'][2]['description'] += ' Unrelated new framing.'
        else:
            scene['metadata'] = {'visual_development': {'shot_cards': {
                'unrelated': {'notes': 'Unrelated board notes.'}}}}
        save(root / 'artifacts/scene_plan.json', scene)


@pytest.mark.parametrize('change', ['contract_action', 'contract_board', 'script_section',
                                   'scene_description', 'scene_card'])
def test_unrelated_edit_retains_actual_policy_mcp_candidate_with_unchanged_upstream(retained, change):
    root, _, _, child, upstream = retained
    assert check_original(retained, historical=False)['result']['status'] == 'generated'
    assert next(row for row in draft.first_cut_candidates(root) if row['shot_id'] == 'other')['strict_selected']
    change_unrelated(root, change)
    with pytest.raises(ValueError):
        check_original(retained, historical=False)
    proof = check_original(retained)
    assert proof['historical_source_changed'] and proof['historical_upstream_changed'] is False
    assert execution.load_selected_attempts(root)['entry'] == upstream
    candidate = next(row for row in draft.first_cut_candidates(root) if row['shot_id'] == 'other')
    assert candidate['status'] == 'candidate' and candidate['attempt_id'] == child['attempt_id']
    assert candidate['output']['sha256'] == child['output']['sha256']
    assert candidate['duration_seconds'] == pytest.approx(5)
    assert candidate['review'] == 'unknown' and candidate['strict_selected'] is False
    assert [f['name'] for f in candidate['findings']] == ['source_applicability_changed']
    assert 'Unrelated episode planning changed' in candidate['findings'][0]['evidence']


def test_historical_applicability_warning_stays_current_on_composed_cut(retained):
    root, _, _, child, _ = retained
    change_unrelated(root, 'contract_action')
    cut = draft.compose_first_cut(root)
    assert cut['status'] == 'partial' and cut['export'] is not None
    clip = next(clip for clip in cut['clips'] if clip['shot_id'] == 'other')
    assert clip['attempt_id'] == child['attempt_id']
    assert [finding['name'] for finding in clip['findings']] == ['source_applicability_changed']
    assert draft.first_cut_status(root, {'path': cut['path'], 'sha256': cut['sha256']}) == {
        'status': 'current', 'reasons': []}


@pytest.mark.parametrize('change', ['own_action', 'own_duration', 'own_dialogue', 'own_cast',
                                   'story', 'global_cast', 'order', 'static_review', 'static_bytes',
                                   'script_section', 'scene_description', 'scene_mapping',
                                   'scene_card', 'frozen_evidence', 'frozen_scope', 'authority'])
def test_historical_preview_rejects_relevant_or_frozen_evidence_drift(retained, change):
    root, _, jobs, _, _ = retained
    change_unrelated(root, 'contract_action')
    if change.startswith('own_') or change in {'story', 'global_cast', 'order', 'static_review', 'static_bytes'}:
        contract = execution.load_shot_contract(root)
        shot = contract['shots'][1]
        if change == 'own_action':
            shot['dominant_action'] += ' Changed retained action.'
        elif change == 'own_duration':
            shot['duration_seconds'] = 6
        elif change == 'own_dialogue':
            shot['dialogue'].append({'speaker_id': shot['cast_ids'][0], 'source': 'offscreen',
                                    'text': 'Changed spoken line.', 'start_seconds': 0, 'end_seconds': 1})
        elif change == 'own_cast':
            shot['cast_ids'].append('doctor')
        elif change == 'story':
            contract['story']['payoff'] += ' Changed protected story.'
        elif change == 'global_cast':
            contract['late_cast_ids'].append('patient')
        elif change == 'order':
            contract['shots'].reverse()
        elif change == 'static_review':
            contract['assets'][0]['review']['reviewer'] = 'Changed static reviewer'
        else:
            (root / contract['assets'][0]['path']).write_bytes(b'Changed relevant static reference')
        refresh(contract)
        save(root / 'artifacts/shot_contract.json', contract)
    elif change == 'script_section':
        script = execution._read(root / 'artifacts/script.json')
        script['sections'][1]['text'] += ' Changed retained section.'
        save(root / 'artifacts/script.json', script)
    elif change.startswith('scene_'):
        scene = execution._read(root / 'artifacts/scene_plan.json')
        if change == 'scene_description':
            scene['scenes'][1]['description'] += ' Changed retained scene.'
        elif change == 'scene_mapping':
            scene['scenes'][1]['script_section_id'] = 's3'
        else:
            scene['metadata'] = {'visual_development': {'shot_cards': {'other': {'notes': 'Changed own card.'}}}}
        save(root / 'artifacts/scene_plan.json', scene)
    elif change == 'frozen_evidence':
        path = jobs._path(root, 'original-other').parent / 'evidence.json'
        snapshot = execution._read(path)
        snapshot['source_packet']['shot']['dominant_action'] += ' Forged snapshot.'
        save(path, snapshot)
    else:
        path = jobs._path(root, 'original-other')
        state = execution._read(path)
        if change == 'frozen_scope':
            state['frozen']['authority']['scope_sha256'] = '0' * 64
        else:
            state['frozen']['authority']['billing']['policy_sha256'] = '0' * 64
        from lib.openart_mcp import digest
        # Even a locally resealed private state cannot replace the retained
        # scope/activation/billing evidence checked by historical provenance.
        state['frozen_sha256'] = digest(state['frozen'])
        state['authority_sha256'] = digest(state['frozen']['authority'])
        save(path, state)
    with pytest.raises(ValueError):
        check_original(retained)
    candidate = next(row for row in draft.first_cut_candidates(root) if row['shot_id'] == 'other')
    assert candidate['status'] == 'missing'


def materialize_unrelated_assets(root):
    contract = execution.load_shot_contract(root)
    board = next(asset for asset in contract["assets"] if asset["id"] == "unrelated-board")
    board["role"] = "reference_image"
    endpoint = copy.deepcopy(board)
    endpoint.update(id="unrelated-end", role="end_frame")
    contract["assets"].append(endpoint)
    contract["shots"][2]["asset_ids"].append(endpoint["id"])
    refresh(contract)
    save(root / "artifacts/shot_contract.json", contract)


def test_unrelated_asset_materialization_retains_mcp_historical_draft_and_rejection(retained):
    root, revision, jobs, child, _ = retained
    materialize_unrelated_assets(root)
    proof = check_original(retained)
    assert proof["historical_source_only"]
    candidate = next(row for row in draft.first_cut_candidates(root) if row["shot_id"] == "other")
    assert candidate["status"] == "candidate" and candidate["strict_selected"] is False
    assert candidate["output"]["sha256"] == child["output"]["sha256"]
    review = copy.deepcopy(child["review"])
    review.update(review_id="actual-source-defect", subject_sha256=child["output"]["sha256"], status="fail")
    review["predicates"][0].update(status="fail", severity="critical")
    assert execution.record_rejection(root, child["attempt_id"], review) == review
    assert (
        jobs.collect(root, child["attempt_id"], downloaded_path=child["output"]["path"])["status"]
        == "collected"
    )
    with pytest.raises(ValueError):
        check_original(retained, historical=False)


@pytest.mark.parametrize("change", ["required_role", "own_source_added"])
def test_materialization_of_queried_mcp_shot_still_rejects(retained, change):
    root, *_ = retained
    materialize_unrelated_assets(root)
    contract = execution.load_shot_contract(root)
    if change == "required_role":
        own = next(
            asset
            for asset in contract["assets"]
            if asset["id"] in contract["shots"][1]["asset_ids"] and not asset.get("upstream_source")
        )
        own["role"] = "reference_image" if own["role"] != "reference_image" else "start_frame"
    else:
        contract["shots"][1]["asset_ids"].append("unrelated-end")
    refresh(contract)
    save(root / "artifacts/shot_contract.json", contract)
    with pytest.raises(ValueError):
        check_original(retained)
