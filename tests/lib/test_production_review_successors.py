"""Synthetic review advancement through real offline governed attempts."""
import copy
import json
import pytest
import subprocess
import shutil
_REAL_RUN = subprocess.run
_REAL_WHICH = shutil.which
from lib.production_execution import record_selection, load_selected_attempts
from lib.production_review import validate_final_review
from lib.production_provenance import validate_attempt_provenance
from lib.shot_contract import file_sha256, review_digest, validate_shot_contract
from tests.lib.test_draft_audio_review import production, authorize, candidate
from tests.lib.test_production_request import package  # noqa: F401


def upgrade(p, sid, *, evidence_name=None):
    from lib.production_review_successors import record_review_successor
    selection = load_selected_attempts(p.root)[sid]
    review = copy.deepcopy(selection['review'])
    review.update(review_id='synthetic-audio-' + sid, reviewer='Synthetic named listening reviewer', status='pass')
    review.pop('draft_policy_sha256', None)
    next(x for x in review['predicates'] if x['name']=='speaker_source').update(
        status='pass', evidence='SYNTHETIC synchronized full-clip listening: dialogue matches visible source.')
    evidence = p.root / (evidence_name or 'listening-' + sid + '.txt')
    evidence.write_text(json.dumps({'version':'1.0','kind':'synchronized_audio_review',
        'project_id':p.root.name,'story_revision':p.story['story_revision'],'shot_id':sid,
        'subject_sha256':review['subject_sha256'],'attempt_id':selection['attempt_id'],
        'output_sha256':selection['output']['sha256'],'reviewer':review['reviewer'],
        'coverage':'complete_clip','synchronized':True,'listened':True,'status':'pass'}))
    return review, {'path': evidence.name, 'sha256':file_sha256(evidence)}


def complete_provisional(p):
    policy = authorize(p)
    for shot in p.contract['shots']:
        sid=shot['id']
        if shot['upstream']: p.bind_upstream(sid)
        result=p.generate(sid)
        assert result.success, result.error
        record_selection(p.root,sid,candidate(p,sid,result,policy))


def test_audio_successors_preserve_frozen_dispatch_and_enable_final(production):
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    p=production; complete_provisional(p)
    before={str(path):path.read_bytes() for path in p.root.rglob('*') if path.is_file()}
    selected=load_selected_attempts(p.root)
    for sid, selection in selected.items():
        review,evidence=upgrade(p,sid)
        receipt=record_review_successor(p.root,sid,review,evidence=evidence)
        assert receipt['review_sha256']==review_digest(review)
        assert resolve_selection_review(p.root,sid,selection)==review
        assert validate_attempt_provenance(p.root,selection['attempt_id'],shot_id=sid,
            story_revision=p.story['story_revision'],expected_output=selection['output'])
        assert validate_shot_contract(p.contract,project_dir=p.root,shot_id=sid,
            selected_upstream=selected)['eligible']
    assert all(__import__('pathlib').Path(path).read_bytes()==raw for path,raw in before.items())
    assert len(p.transport.native_requests)==3
    master,_=p.draft(); final=p.full_review(master)
    for scene in final['scenes']:
        scene['review_sha256']=review_digest(resolve_selection_review(p.root,scene['scene_id'],selected[scene['scene_id']]))
    assert validate_final_review(p.root,final)['eligible']


@pytest.mark.parametrize('change',['audio_fail','visual_change','visual_fail','subject','missing_predicate','reviewer','evidence','media','plan','selection'])
def test_successor_rejects_changed_subject_or_nonmonotonic_evidence(production,change):
    from lib.production_review_successors import record_review_successor
    p=production;policy=authorize(p); result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy))
    review,evidence=upgrade(p,'entry')
    if change=='audio_fail': next(x for x in review['predicates'] if x['name']=='speaker_source')['status']='fail'
    elif change in {'visual_change','visual_fail'}:
        next(x for x in review['predicates'] if x['name']=='completed_action')['status' if change=='visual_fail' else 'evidence']='fail' if change=='visual_fail' else 'new visual judgment'
    elif change=='subject':review['subject_sha256']='0'*64
    elif change=='missing_predicate':review['predicates'].pop()
    elif change=='reviewer':review['reviewer']=' '
    elif change=='evidence':(p.root/evidence['path']).unlink()
    elif change=='media':__import__('pathlib').Path(load_selected_attempts(p.root)['entry']['output']['path']).write_bytes(b'changed')
    elif change=='plan':
        p.contract['shots'][0]['dominant_action']='changed';p.persist_contract()
    elif change=='selection':
        value=load_selected_attempts(p.root);value['entry']['attempt_id']='foreign';(p.root/'artifacts/selected_attempts.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):record_review_successor(p.root,'entry',review,evidence=evidence)

from tests.lib.test_openart_mcp_native import observations  # noqa: F401


def mcp_interior(p, monkeypatch, tmp_path):
    """Real native upload receipt, compilation, Strict begin and collection."""
    from lib import production_execution as ex, production_request as prep
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs, openart_mcp_dispatch as dispatch
    from tests.integration.test_first_pass_workflow import save
    from tests.integration.test_openart_mcp_governance import install_fake_download
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    from lib import production_provenance as provenance
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    root=p.root;sid='interior';model='byte-plus-seedance-2-mini'
    for scene in p.plan['scenes']: scene['script_section_id']=scene['id']
    save(root/'artifacts/scene_plan.json',p.plan)
    save(root/'artifacts/asset_manifest.json',p.manifest)
    shot=next(s for s in p.contract['shots'] if s['id']==sid)
    asset_rows=[next(a for a in p.contract['assets'] if a['id']==sid+'-'+suffix) for suffix in ('start','end')]
    manifest=[{'path':str(root/a['path']),'sha256':a['sha256'],'type':'image'} for a in asset_rows]
    approval=root/'synthetic-upload-approval.txt';approval.write_text('Synthetic transfer only unknown cost approval.')
    upload={'provider':'openart_mcp','status':'approved','purpose':'source_transfer_only',
        'project_id':root.name,'story_revision':p.story['story_revision'],
        'account_uid_sha256':mcp.account_summary()['uid_sha256'],'openart_project_id':'fixture-project',
        'files':manifest,'no_enforceable_credit_ceiling':True,'delayed_charges_unknown':True,
        'max_upload_batches':1,'approved_by':'Synthetic creator',
        'evidence':{'path':approval.name,'sha256':file_sha256(approval)}}
    save(root/'artifacts/openart_mcp_upload_authorization-chain.json',upload)
    upload_prepared=jobs.prepare_upload(root,files=[row['path'] for row in manifest],
        billing_declaration={'upload_authorization_id':'chain'},openart_project_id='fixture-project')
    uploaded=jobs.record_upload_receipt(root,upload_id=upload_prepared['upload_id'],result={
        'projectId':'fixture-project','results':[{'fileId':'fixture-file-'+str(i),'status':'SUCCESS',
        'visualReference':{'id':'fixture-ref-'+str(i),'type':'image','url':'https://fixture.example.test/'+str(i),
        'label':'Synthetic start/end '+str(i),'metadata':{'fixture_only':True},
        'imageUrl':'https://fixture.example.test/'+str(i),'thumbnailUrl':'https://fixture.example.test/'+str(i)}} for i in range(2)]})
    authored=prep.compile_provider_prompt(root,sid,provider='openart_mcp',model=model)
    inputs={'project_dir':str(root),'governance':{'scope_id':'mcp-original','shot_id':sid,'stage':'generate'},
        'model':model,'mode':'image2video','operation':'image_to_video','prompt':authored['prompt'],
        'native_params':{'duration':shot['duration_seconds'],'resolution':'720p','aspectRatio':'16:9','generateAudio':True},
        'openart_project_id':'fixture-project','input_assets':[{'role':role,'source_path':row['path'],
            'source_sha256':row['sha256'],'upload_id':ref['upload_id']}
            for role,row,ref in zip(('first_frame','last_frame'),manifest,uploaded['references'])],
        'output_path':str(root/'assets/video/interior-mcp.mp4'), 'compiled_request_id':'chain',
        'preparation_review_id':'chain','unknown_cost_authorization_id':'chain'}
    profile=mcp.load_profile(model,'image2video',require='supported')
    native=prep.prep_builder('openart_mcp')(inputs,profile)
    duration=shot['duration_seconds']
    speech_end=max((l['end_seconds'] for l in shot['dialogue']),default=0)
    timing={'method':'segmented_estimate','duration_seconds':duration,'language':'en','margin_seconds':.05,
        'rationale':'Synthetic offline timing','overlap_policy':'serial','overlap_rationale':'Synthetic sequential beats',
        'segments':[{'dialogue_index':i,'text_sha256':prep.digest(line['text']),'language':'en',
            'word_count':len(line['text'].split()),'words_per_minute':240,'pause_seconds':0,'rationale':'Synthetic rate'}
            for i,line in enumerate(shot['dialogue'])],
        'action_windows':[{'source_pointer':'/shot_contract/shots/1/'+key,'value_sha256':prep.digest(shot[key]),
            'start_seconds':a,'end_seconds':b,'rationale':'Synthetic action timing'} for key,a,b in
            [('dominant_action',speech_end+.01,duration-.6),('completed_end_state',duration-.6,duration-.06)]]}
    compiled=prep.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
    review={'version':'1.0','review_id':'chain','reviewer':'Synthetic preparation reviewer','status':'pass',
        'subject_sha256':prep.digest(compiled),'evidence_kind':'fixture_only',
        'predicates':[{'name':name,'status':'pass','severity':'critical','evidence':'Synthetic complete prep'} for name in sorted(prep.PREDICATES)]}
    save(root/'artifacts/compiled_request-chain.json',compiled);save(root/'artifacts/preparation_review-chain.json',review)
    evidence=root/'synthetic-mcp-approval.txt';evidence.write_text('Synthetic creator explicitly accepts no enforceable credit ceiling for one original.')
    evidence_binding={'path':evidence.name,'sha256':file_sha256(evidence)}
    request_sha=ex.planned_request_digest(inputs,project_dir=root)
    authority={'provider':'openart_mcp','status':'approved','approved_by':'Synthetic creator','evidence':evidence_binding,
        'project_id':root.name,'story_revision':p.story['story_revision'],'request_sha256':request_sha,
        'scope_id':'mcp-original','shot_id':sid,'model':model,'mode':'image2video',
        'account_uid_sha256':native['account_binding']['uid_sha256'],'native_body_sha256':native['body_sha256'],
        'source_binding_sha256':native['source_binding_sha256'],'max_attempts':1,
        'no_enforceable_credit_ceiling':True,'cli_authority':False,'purpose':'generation'}
    save(root/'artifacts/openart_mcp_unknown_cost-chain.json',authority)
    scopes=json.loads((root/'production_scopes.json').read_text())
    scopes['scopes'].append({'id':'mcp-original','provider':'openart_mcp','status':'approved','approved_by':'Synthetic creator',
        'project_id':root.name,'story_revision':p.story['story_revision'],'evidence':evidence_binding,'phase':'first_pass',
        'requests':{sid:request_sha},'attempts_per_shot':{sid:1},'approval_plan_sha256':ex.approval_plan_digest(p.contract),
        'openart_mcp_billing_authorization_sha256':dispatch.billing_authorization_digest(authority)})
    save(root/'production_scopes.json',scopes)
    jobs.prepare(root,attempt_id='synthetic-mcp-original',generation_inputs=inputs,authority_fn=ex.prepare_openart_mcp_handoff)
    jobs.begin(root,'synthetic-mcp-original',authority_fn=ex.prepare_openart_mcp_handoff)
    jobs.receive(root,'synthetic-mcp-original',outcome={'historyId':'synthetic-original-history','status':'PENDING'})
    jobs.record_status(root,'synthetic-mcp-original',result={'historyId':'synthetic-original-history','status':'COMPLETED',
        'resources':[{'id':'synthetic-output','mediaType':'video','status':'COMPLETED','url':'https://fixture.invalid/original.mp4'}],'creditsCharged':1})
    # Preserve actual local media probe behavior; only HTTP bytes are injected.
    with monkeypatch.context() as media_tools:
        media_tools.setattr(subprocess, 'run', _REAL_RUN)
        media_tools.setattr(shutil, 'which', _REAL_WHICH)
        media=real_av_clip(tmp_path/'synthetic-mcp.mp4',seconds=duration)
    install_fake_download(monkeypatch,jobs,media)
    download=jobs.download_original(root,'synthetic-mcp-original')
    jobs.collect(root,'synthetic-mcp-original',downloaded_path=download['downloaded_path'])
    frame=root/'assets/images/interior-mcp-outgoing.bin';frame.write_bytes(b'Synthetic completed outgoing state')
    selection={'attempt_id':'synthetic-mcp-original','output':{'path':inputs['output_path'],'sha256':file_sha256(inputs['output_path'])},
        'outgoing_frame':{'path':str(frame),'sha256':file_sha256(frame)}}
    from tests.integration.test_first_pass_workflow import attestation
    from lib.shot_contract import UPSTREAM_PREDICATES, selection_digest
    selection['review']=attestation(selection_digest(selection),UPSTREAM_PREDICATES)
    selection['review'].update(status='provisional',draft_policy_sha256=authorize(p))
    next(x for x in selection['review']['predicates'] if x['name']=='speaker_source').update(status='unknown',evidence='Synthetic unheard audio')
    record_selection(root,sid,selection)
    return selection, compiled


def test_real_grok_mcp_grok_chain_replays_immutable_original_packets(production,observations,tmp_path,monkeypatch):
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    p=production;policy=authorize(p);result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy));p.bind_upstream('interior')
    middle,compiled=mcp_interior(p,monkeypatch,tmp_path)
    # Projected references deliberately omit paths: capture must resolve source contract.
    assert compiled['source_binding']['references'] and all('path' not in row for row in compiled['source_binding']['references'])
    # Retain newly added MCP scope while binding the downstream Grok original.
    p.bind_upstream('payoff');result=p.generate('payoff');assert result.success,result.error
    record_selection(p.root,'payoff',candidate(p,'payoff',result,policy))
    selected=load_selected_attempts(p.root)
    before={str(path):path.read_bytes() for path in p.root.rglob('*') if path.is_file()}
    for sid,selection in selected.items():
        review,evidence=upgrade(p,sid);record_review_successor(p.root,sid,review,evidence=evidence)
        assert resolve_selection_review(p.root,sid,selection)==review
    for sid,selection in selected.items():
        assert validate_attempt_provenance(p.root,selection['attempt_id'],shot_id=sid,
            story_revision=p.story['story_revision'],expected_output=selection['output'])
    assert all(__import__('pathlib').Path(path).read_bytes()==raw for path,raw in before.items())
    assert len(p.transport.native_requests)==2
    master,_=p.draft();final=p.full_review(master)
    for scene in final['scenes']:
        scene['review_sha256']=review_digest(resolve_selection_review(p.root,scene['scene_id'],selected[scene['scene_id']]))
    checked=validate_final_review(p.root,final)
    assert checked['eligible'],checked['errors']
    # Changed current bytes/planning or retained preparation still reject the
    # historical original; an audio successor grants no new source authority.
    from pathlib import Path
    targets=[p.root/'assets/images/interior-start.png',p.root/'artifacts/shot_contract.json',
             p.root/'artifacts/compiled_request-chain.json',
             p.root/'openart_mcp/attempts/synthetic-mcp-original/evidence.json']
    for target in targets:
        raw=target.read_bytes();mode=target.stat().st_mode & 0o777
        target.chmod(0o600)
        target.write_bytes(b'changed immutable or current source bytes')
        try:
            with pytest.raises((ValueError,OSError)):
                validate_attempt_provenance(p.root,middle['attempt_id'],shot_id='interior',
                    story_revision=p.story['story_revision'],expected_output=middle['output'])
        finally:
            target.write_bytes(raw);target.chmod(mode)


@pytest.mark.parametrize('change', ['changed', 'missing', 'forged'])
def test_mixed_route_original_authority_survives_listening_sidecar_drift(
    production, observations, tmp_path, monkeypatch, change,
):
    """Historical originals stay proven; current audio eligibility fails closed."""
    import hashlib
    from pathlib import Path
    from lib import production_request as preparation
    from lib.episode_production_controls import episode_production_status
    from lib.production_review_successors import record_review_successor, resolve_selection_review

    p = production
    policy = authorize(p)
    result = p.generate('entry')
    assert result.success, result.error
    record_selection(p.root, 'entry', candidate(p, 'entry', result, policy))
    p.bind_upstream('interior')
    mcp_interior(p, monkeypatch, tmp_path)
    p.bind_upstream('payoff')
    result = p.generate('payoff')
    assert result.success, result.error
    record_selection(p.root, 'payoff', candidate(p, 'payoff', result, policy))
    selected = load_selected_attempts(p.root)
    receipts = {}
    for sid, selection in selected.items():
        review, evidence = upgrade(p, sid)
        receipts[sid] = record_review_successor(p.root, sid, review, evidence=evidence)
        assert resolve_selection_review(p.root, sid, selection) == review
    master, _ = p.draft()
    final = p.full_review(master)
    for scene in final['scenes']:
        scene['review_sha256'] = receipts[scene['scene_id']]['review_sha256']
    checked = validate_final_review(p.root, final)
    assert checked['eligible'], checked['errors']

    evidence_path = p.root / 'listening-entry.txt'
    retained = {path: path.read_bytes() for path in p.root.rglob('*') if path.is_file()
                and path != evidence_path and 'production_review_successors' not in path.parts}
    if change == 'changed':
        evidence_path.write_text('{}')
    elif change == 'missing':
        evidence_path.unlink()
    else:
        attestation = json.loads(evidence_path.read_text())
        attestation['listened'] = False
        evidence_path.write_text(json.dumps(attestation))
        old_path = Path(receipts['entry']['path'])
        record = json.loads(old_path.read_bytes())
        record['evidence']['sha256'] = file_sha256(evidence_path)
        raw = json.dumps(record, indent=2).encode()
        old_path.unlink()
        (old_path.parent / (hashlib.sha256(raw).hexdigest() + '.json')).write_bytes(raw)

    # All three routes re-prove the frozen original request and media, including
    # the MCP source packet that depends on entry's embedded provisional review.
    for sid, selection in selected.items():
        assert validate_attempt_provenance(p.root, selection['attempt_id'], shot_id=sid,
            story_revision=p.story['story_revision'], expected_output=selection['output'])
    assert all(path.read_bytes() == raw for path, raw in retained.items())
    assert len(p.transport.native_requests) == 2

    with pytest.raises(ValueError):
        resolve_selection_review(p.root, 'entry', selected['entry'])
    with pytest.raises(ValueError, match='selected review changed or missing'):
        preparation.source_packet(p.root, 'interior', provider='openart_mcp')
    assert not validate_shot_contract(p.contract, project_dir=p.root, shot_id='interior',
        selected_upstream=selected)['eligible']
    assert not validate_final_review(p.root, final)['eligible']
    status = episode_production_status(p.root)
    assert status['shot_readiness']['entry'] == 'invalid'
    assert status['selections']['entry']['review_status'] == 'invalid'
    assert status['delivery'] == 'draft_only'

    # Independent current original-subject and media checks still reject drift,
    # even though the listening sidecar has no historical generation authority.
    for producer, dependent in [('entry', 'interior'), ('interior', 'payoff')]:
        for role in ('output', 'outgoing_frame'):
            path = Path(selected[producer][role]['path'])
            raw = path.read_bytes()
            path.write_bytes(b'changed original selected media')
            try:
                selection = selected[dependent]
                with pytest.raises(ValueError):
                    validate_attempt_provenance(p.root, selection['attempt_id'], shot_id=dependent,
                        story_revision=p.story['story_revision'], expected_output=selection['output'])
            finally:
                path.write_bytes(raw)
        path = p.root / 'artifacts/selected_attempts.json'
        raw = path.read_bytes()
        altered = json.loads(raw)
        altered[producer]['review']['reviewer'] = 'forged original reviewer'
        path.write_text(json.dumps(altered))
        try:
            selection = selected[dependent]
            with pytest.raises(ValueError):
                validate_attempt_provenance(p.root, selection['attempt_id'], shot_id=dependent,
                    story_revision=p.story['story_revision'], expected_output=selection['output'])
        finally:
            path.write_bytes(raw)


@pytest.mark.parametrize('change', ['missing_predicate', 'duplicate_predicate', 'fixture_only_live'])
def test_current_and_historical_preparation_reject_the_same_review_defects(package, change):
    """Use canonical readers and retained native/upload proof, not stub validation."""
    from lib import production_request as preparation
    from tools import _openart_cli as cli
    from tests.lib.test_production_request import _compile_with_synthetic_real_form, publish, write

    root, inputs, native, profile, compiled, review = package
    if change == 'fixture_only_live':
        form = {'schema': {'type': 'object', 'properties': {
            'prompt': {'type': 'string'}, 'image': {'type': 'string'},
            'duration': {'type': 'number', 'maximum': 8},
            'aspectRatio': {'type': 'string'}, 'resolution': {'type': 'string'}}}}
        compiled, inputs, native, profile = _compile_with_synthetic_real_form(package, form)
        review['evidence_kind'] = 'reviewed'
    publish((root, inputs, native, profile, compiled, review))
    proof = preparation.freeze_preparation('semantic-parity', inputs, native, profile)
    saved = {'attempt_id': 'semantic-parity', 'scope_id': 'approved', 'shot_id': 'entry',
        'input_assets': [{'role': 'image_path', 'path': inputs['image_path'],
            'original_path': inputs['image_path'], 'sha256': file_sha256(inputs['image_path'])}],
        'openart': {'preparation_snapshot': proof}}
    frozen = {'inputs': inputs, 'native': native, 'profile': profile}
    assert preparation.validate_frozen_preparation_history(saved, frozen, root) == proof
    if change == 'missing_predicate':
        review['predicates'].pop()
    elif change == 'duplicate_predicate':
        review['predicates'][1] = copy.deepcopy(review['predicates'][0])
    else:
        review['evidence_kind'] = 'fixture_only'
    write(root / 'artifacts/preparation_review-r1.json', review)

    # Rehash the synthetic snapshot to isolate the semantic validator. A stale
    # snapshot hash would otherwise mask a missing historical predicate check.
    path = cli.state_dir() / 'preparation/semantic-parity/review.json'
    payload = json.loads(path.read_text())
    payload['review'] = copy.deepcopy(review)
    path.chmod(0o600)
    path.write_text(json.dumps(payload))
    proof['snapshot_sha256'] = file_sha256(path)
    proof['review_sha256'] = preparation.digest(review)
    with pytest.raises(ValueError) as current:
        preparation.validate_preparation(inputs, native, profile)
    with pytest.raises(ValueError) as historical:
        preparation.validate_frozen_preparation_history(saved, frozen, root)
    assert str(current.value) == str(historical.value)
    if change == 'fixture_only_live':
        assert 'fixture-only' in str(current.value)


@pytest.mark.parametrize('change',['evidence_bytes','plan','outgoing','visual_forgery','selection_forgery','conflict','legacy_path','history_symlink'])
def test_resolver_replays_exact_binding_and_rejects_conflicting_history(production,change):
    from pathlib import Path
    import hashlib
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    p=production;policy=authorize(p);result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy))
    review,evidence=upgrade(p,'entry');receipt=record_review_successor(p.root,'entry',review,evidence=evidence)
    selection=load_selected_attempts(p.root)['entry']
    path=Path(receipt['path'])
    if change=='evidence_bytes': (p.root/evidence['path']).write_text('{}')
    elif change=='plan':p.contract['shots'][0]['dominant_action']='changed';p.persist_contract()
    elif change=='outgoing':Path(selection['outgoing_frame']['path']).write_bytes(b'changed')
    elif change=='history_symlink':
        directory=path.parent; moved=p.root/'external-review-history';directory.rename(moved);directory.symlink_to(moved,target_is_directory=True)
    else:
        value=json.loads(path.read_text())
        if change=='visual_forgery':next(x for x in value['review']['predicates'] if x['name']=='completed_action')['evidence']='forged'
        elif change=='selection_forgery':value['selection_sha256']='0'*64
        elif change=='conflict':value['review']['review_id']='competing reviewer'
        raw=json.dumps(value,indent=2).encode()
        new=path.parent/(hashlib.sha256(raw).hexdigest()+'.json' if change!='legacy_path' else 'unhashed.json')
        if change!='conflict':path.unlink()
        new.write_bytes(raw)
    with pytest.raises(ValueError):resolve_selection_review(p.root,'entry',selection)


@pytest.mark.parametrize('change',['predecessor','subject'])
def test_other_predecessor_or_subject_history_is_retained_and_ignored(production,change):
    from pathlib import Path
    import hashlib
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    p=production;policy=authorize(p);result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy))
    review,evidence=upgrade(p,'entry')
    receipt=record_review_successor(p.root,'entry',review,evidence=evidence)
    path=Path(receipt['path']);original_bytes=path.read_bytes()
    other=json.loads(original_bytes)
    other['predecessor_sha256' if change=='predecessor' else 'subject_sha256']='0'*64
    raw=json.dumps(other,indent=2).encode()
    unrelated=path.parent/(hashlib.sha256(raw).hexdigest()+'.json')
    unrelated.write_bytes(raw);unrelated.chmod(0o444)
    assert resolve_selection_review(p.root,'entry',load_selected_attempts(p.root)['entry'])==review
    assert path.read_bytes()==original_bytes
    assert unrelated.read_bytes()==raw


@pytest.mark.parametrize('target',['evidence','history'])
def test_resolver_hashes_and_parses_one_captured_file_version(production,monkeypatch,target):
    """An atomic replacement during a read cannot mix old hash/new JSON."""
    from pathlib import Path
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    p=production;policy=authorize(p);result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy))
    review,evidence=upgrade(p,'entry')
    receipt=record_review_successor(p.root,'entry',review,evidence=evidence)
    selection=load_selected_attempts(p.root)['entry']
    path=(p.root/evidence['path']) if target=='evidence' else Path(receipt['path'])
    changed=json.loads(path.read_bytes())
    if target=='evidence':changed['listened']=False
    else:next(x for x in changed['review']['predicates'] if x['name']=='completed_action')['evidence']='forged'
    replacement=path.with_suffix('.replacement')
    replacement.write_text(json.dumps(changed))
    real_open=Path.open
    reads=0
    def replace_after_open(self,*args,**kwargs):
        nonlocal reads
        stream=real_open(self,*args,**kwargs)
        if self==path:
            reads+=1
            if reads==1:replacement.replace(path)
        return stream
    monkeypatch.setattr(Path,'open',replace_after_open)
    assert resolve_selection_review(p.root,'entry',selection)==review
    assert reads==1
    with pytest.raises(ValueError):resolve_selection_review(p.root,'entry',selection)


def test_final_review_reports_unreadable_successor_history(production):
    p=production;p.complete_shots()
    master,_=p.draft();review=p.full_review(master)
    history=p.root/'production_review_successors';history.mkdir()
    (history/'unreadable.json').mkdir()
    checked=validate_final_review(p.root,review)
    assert not checked['eligible']
    assert any('selected.entry: malformed review or binding' in error for error in checked['errors'])


def test_duplicate_record_is_idempotent_and_a_second_audio_judgment_conflicts(production):
    from lib.production_review_successors import record_review_successor
    p=production;policy=authorize(p);result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy))
    review,evidence=upgrade(p,'entry');first=record_review_successor(p.root,'entry',review,evidence=evidence)
    assert record_review_successor(p.root,'entry',review,evidence=evidence)==first
    review['review_id']='competing audio judgment'
    with pytest.raises(ValueError,match='conflicting'):record_review_successor(p.root,'entry',review,evidence=evidence)
    assert len(list((p.root/'production_review_successors').glob('*.json')))==1
    assert len(p.transport.native_requests)==1


from tests.lib.test_creator_draft_exception import accepted  # noqa: F401


def test_audio_successor_preserves_creator_accepted_critical_defect_as_draft(accepted):
    from pathlib import Path
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    from lib.production_draft import append_creator_draft_exception
    from lib.shot_contract import selection_digest
    from tests.integration.test_first_pass_workflow import save
    p,record,selection,failure,_,_=accepted
    policy=authorize(p)
    next(x for x in failure['predicates'] if x['name']=='speaker_source').update(status='unknown',evidence='Synthetic audio not yet heard')
    failed_path=Path(record['failed_review']['path']);failed_path.chmod(0o644);save(failed_path,failure)
    record['failed_review']['sha256']=file_sha256(failed_path)
    reference=append_creator_draft_exception(p.root,record)
    selection['review']=copy.deepcopy(failure)
    selection['review'].update(review_id='synthetic-defect-draft',status='provisional',
        subject_sha256=selection_digest(selection),creator_draft_exception=reference,draft_policy_sha256=policy)
    record_selection(p.root,'entry',selection)
    review,evidence=upgrade(p,'entry');review['status']='provisional';review['draft_policy_sha256']=policy
    recorded=record_review_successor(p.root,'entry',review,evidence=evidence)
    resolved=resolve_selection_review(p.root,'entry',selection)
    assert resolved['status']=='provisional'
    assert next(x for x in resolved['predicates'] if x['name']==record['failed_predicate'])['status']=='fail'
    assert recorded['review']==resolved
    for sid in ('interior','payoff'):
        p.bind_upstream(sid);result=p.generate(sid);assert result.success,result.error;p.select(sid,result)
    master,_=p.draft();final=p.full_review(master)
    final['scenes'][0]['review_sha256']=review_digest(resolved)
    assert not validate_final_review(p.root,final)['eligible']
    review['status']='pass';review.pop('draft_policy_sha256')
    with pytest.raises(ValueError):record_review_successor(p.root,'entry',review,evidence=evidence)


def test_fresh_same_attempt_planning_review_takes_its_own_successor(production):
    """Use the real append-planning/record-selection path; retain old history."""
    from pathlib import Path
    from lib import production_continuity as continuity
    from lib.production_review_successors import record_review_successor, resolve_selection_review
    from lib.shot_contract import contract_digest, selection_digest
    from tests.integration.test_planning_revision_workflow import _setup, _revise, _fresh_selection
    p=production
    entry,repaired,old,prior=_setup(p)
    policy=authorize(p)
    original=copy.deepcopy(old)
    original['review'].update(status='provisional',draft_policy_sha256=policy)
    next(x for x in original['review']['predicates'] if x['name']=='speaker_source').update(
        status='unknown',evidence='Synthetic original audio not yet heard')
    record_selection(p.root,'interior',original)
    review,evidence=upgrade(p,'interior')
    old_receipt=record_review_successor(p.root,'interior',review,evidence=evidence)
    old_bytes=Path(old_receipt['path']).read_bytes()
    old_evidence=(p.root/evidence['path']).read_bytes()
    revision=_revise(p,entry,repaired,prior)
    continuity.append_planning_revision(p.root,revision)
    binding={'revision_id':revision['revision_id'],'revision_sha256':revision['revision_sha256'],
             'contract_sha256':contract_digest(p.contract)}
    fresh=_fresh_selection(p,repaired,old,binding)
    fresh['review'].update(status='provisional',draft_policy_sha256=policy,review_id='synthetic-fresh-planning-review')
    next(x for x in fresh['review']['predicates'] if x['name']=='speaker_source').update(
        status='unknown',evidence='Synthetic fresh-plan audio awaiting review')
    record_selection(p.root,'interior',fresh)
    assert fresh['attempt_id']==original['attempt_id']
    assert fresh['output']==original['output']
    assert fresh['outgoing_frame']==original['outgoing_frame']
    assert selection_digest(fresh)!=selection_digest(original)
    assert resolve_selection_review(p.root,'interior',fresh)==fresh['review']
    # The old chain remains retained under its historical subject; it cannot
    # certify this new review without new bound listening evidence.
    new_review,new_evidence=upgrade(p,'interior',evidence_name='listening-interior-revised.txt')
    new_review['review_id']='synthetic-revised-planning-audio'
    new_receipt=record_review_successor(p.root,'interior',new_review,evidence=new_evidence)
    assert resolve_selection_review(p.root,'interior',fresh)==new_review
    assert new_receipt['path']!=old_receipt['path']
    assert Path(old_receipt['path']).read_bytes()==old_bytes
    assert (p.root/evidence['path']).read_bytes()==old_evidence
    assert Path(old_receipt['path']).stat().st_mode & 0o222==0
    assert len(list((p.root/'production_review_successors').glob('*.json')))==2
    assert len(p.transport.native_requests)==3
