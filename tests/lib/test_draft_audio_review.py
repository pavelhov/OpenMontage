"""Offline provisional-review boundaries; no real media or auditory judgments."""
import copy
import json
import pytest
from lib.checkpoint import init_project
from lib.production_execution import record_selection, ProductionGovernanceError
from lib.production_review import validate_final_review
from lib.shot_contract import (draft_audio_policy_digest, file_sha256, selection_digest,
                               UPSTREAM_PREDICATES)
from tests.integration.test_first_pass_workflow import Production, attestation, save, read

@pytest.fixture
def production(tmp_path, monkeypatch):
    root = init_project('offline-courier', title='Synthetic draft evidence test',
                        pipeline_type='cinematic', pipeline_dir=tmp_path,
                        governance='strict', story_revision='courier-story-1')
    p = Production(root, monkeypatch)
    yield p
    p.network.assert_not_called()


def authorize(p):
    path = p.root / 'draft-approval.txt'
    path.write_text('SYNTHETIC: explicit approval to defer unavailable audio for draft only.')
    marker = read(p.root / 'project.json')
    marker['governance']['draft_review'] = {
        'version':'1.0', 'mode':'audio_unavailable_draft', 'project_id':p.root.name,
        'story_revision':p.story['story_revision'],
        'evidence':{'path':path.name, 'sha256':file_sha256(path)}}
    save(p.root / 'project.json', marker)
    return draft_audio_policy_digest(p.root)


def candidate(p, sid, result, policy):
    aid = result.data['production_attempt_id']
    result_record = read(p.root / 'production_attempts' / aid / 'result.json')
    frame = p.root / 'assets/images' / (sid + '-outgoing.bin')
    frame.write_bytes(b'SYNTHETIC completed visual state')
    value = {'attempt_id':aid, 'output':result_record['output'],
             'outgoing_frame':{'path':str(frame), 'sha256':file_sha256(frame)}}
    value['review'] = attestation(selection_digest(value), UPSTREAM_PREDICATES)
    value['review'].update(status='provisional', draft_policy_sha256=policy)
    next(x for x in value['review']['predicates'] if x['name']=='speaker_source').update(
        status='unknown', evidence='SYNTHETIC unavailable auditory review; not a pass.')
    return value


def test_draft_runs_dependents_preserves_scope_and_never_certifies(production):
    p=production; original_scope=(p.root/'production_scopes.json').read_bytes()
    policy=authorize(p)
    for shot in p.contract['shots']:
        sid=shot['id']
        if shot['upstream']: p.bind_upstream(sid)
        result=p.generate(sid)
        assert result.success, result.error
        record_selection(p.root,sid,candidate(p,sid,result,policy))
    assert (p.root/'production_scopes.json').read_bytes()==original_scope
    assert len(p.transport.native_requests)==3
    master,_=p.draft()
    final=p.full_review(master)  # Even a claimed passing master cannot override unknown selections.
    checked=validate_final_review(p.root,final)
    assert not checked['eligible'] and checked['release_status']=='draft'
    assert any('selection review' in x or 'speaker_source' in x for x in checked['errors'])


@pytest.mark.parametrize('change', ['no_policy','policy_hash','approval_bytes','story',
    'failed_speaker','missing_speaker','visual_unknown','visual_fail','subject','output','duplicate'])
def test_draft_selection_rejects_every_other_uncertainty(production,change):
    p=production;policy=authorize(p); result=p.generate('entry');assert result.success
    selection=candidate(p,'entry',result,policy)
    review=selection['review']
    if change=='no_policy':
        marker=read(p.root/'project.json');del marker['governance']['draft_review'];save(p.root/'project.json',marker)
    elif change=='policy_hash': review['draft_policy_sha256']='f'*64
    elif change=='approval_bytes': (p.root/'draft-approval.txt').write_text('changed')
    elif change=='story':
        marker=read(p.root/'project.json');marker['governance']['draft_review']['story_revision']='changed';save(p.root/'project.json',marker)
    elif change=='failed_speaker': next(x for x in review['predicates'] if x['name']=='speaker_source')['status']='fail'
    elif change=='missing_speaker': review['predicates']=[x for x in review['predicates'] if x['name']!='speaker_source']
    elif change.startswith('visual_'): next(x for x in review['predicates'] if x['name']=='completed_action')['status']=change.split('_')[1]
    elif change=='subject': review['subject_sha256']='f'*64
    elif change=='output': selection['output']['sha256']='f'*64
    elif change=='duplicate': review['predicates'].append(copy.deepcopy(review['predicates'][0]))
    with pytest.raises(ProductionGovernanceError): record_selection(p.root,'entry',selection)


def test_changed_policy_blocks_already_selected_dependency(production):
    p=production;policy=authorize(p);result=p.generate('entry');assert result.success
    record_selection(p.root,'entry',candidate(p,'entry',result,policy));p.bind_upstream('interior')
    (p.root/'draft-approval.txt').write_text('changed approval bytes')
    with pytest.raises(ProductionGovernanceError): p.generate('interior')
    assert len(p.transport.native_requests)==1
