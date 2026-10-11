"""Offline policy scope checks; no authored verdict asserts real media quality."""
import copy
from pathlib import Path
import json
import pytest
from lib.shot_contract import audio_review_disabled, file_sha256, required_review_predicates, UPSTREAM_PREDICATES, review_digest
from lib.production_review import validate_final_review
from tests.lib.test_production_review import production, save


def authorize(root):
    marker = json.loads((root / 'project.json').read_text())
    evidence = root / 'audio-policy.json'
    save(evidence, {'identity':'thisaccountisawarcrime', 'audio_review':'disabled','scope':'future_projects_only'})
    marker['governance']['audio_review'] = {'version':'1.0','mode':'disabled',
        'project_id':marker['project_id'],'story_revision':marker['story_revision'],
        'evidence':{'path':evidence.name,'sha256':file_sha256(evidence)}}
    save(root / 'project.json', marker)


def test_policy_binding_and_identity(tmp_path):
    save(tmp_path / 'project.json', {'project_id':'thisaccountisawarcrime-offline',
        'story_revision':'s1','governance':{'mode':'strict'}})
    assert not audio_review_disabled(tmp_path)
    authorize(tmp_path)
    assert audio_review_disabled(tmp_path)
    marker=json.loads((tmp_path/'project.json').read_text());marker['story_revision']='s2'
    save(tmp_path/'project.json',marker)
    assert audio_review_disabled(tmp_path)
    assert 'speaker_source' not in required_review_predicates(tmp_path, UPSTREAM_PREDICATES)
    (tmp_path / 'audio-policy.json').write_text('{}')
    assert not audio_review_disabled(tmp_path)
    assert 'speaker_source' in required_review_predicates(tmp_path, UPSTREAM_PREDICATES)


def test_other_identity_cannot_opt_out(tmp_path):
    save(tmp_path / 'project.json', {'project_id':'another-identity',
        'story_revision':'s1','governance':{'mode':'strict'}})
    authorize(tmp_path)
    assert not audio_review_disabled(tmp_path)


def visual_review(production):
    root, original, _, selection = production
    authorize(root)
    review = copy.deepcopy(original)
    review['version'] = '3.0'
    review['visual_review'] = review.pop('av_review')
    review['visual_review']['mode'] = 'visual_only'
    review['visual_review'].pop('listened_full')
    review['dimensions'].pop('audio')
    review['predicates'] = [p for p in review['predicates'] if p['name'] != 'speaker_source']
    selected_review = selection['review']
    next(p for p in selected_review['predicates'] if p['name']=='speaker_source')['status']='unknown'
    save(root / 'artifacts/selected_attempts.json', {'entry':selection})
    review['scenes'][0]['review_sha256']=review_digest(selected_review)
    return review


def test_visual_final_requires_no_listening_or_speaker_pass(production):
    root = production[0]
    review = visual_review(production)
    result=validate_final_review(root,review,probe=lambda _: {'duration_seconds':8})
    assert result['eligible'],result
    from lib.production_execution import record_selection
    record_selection(root,'entry',production[3])
    # The old byte-for-byte v2 review cannot become visual certification.
    assert not validate_final_review(root,production[1],probe=lambda _: {'duration_seconds':8})['eligible']


@pytest.mark.parametrize('audio_status', ['unknown', 'fail'])
def test_optional_audio_does_not_block_future_selection_status_or_final(production, monkeypatch, audio_status):
    from lib.episode_production_controls import episode_production_status
    from lib.production_execution import record_selection
    from lib.production_review import certify_final
    root, _, _, selection = production
    review = visual_review(production)
    audio = next(p for p in selection['review']['predicates'] if p['name'] == 'speaker_source')
    audio['status'] = audio_status
    audio['severity'] = 'critical'
    review['predicates'].append(copy.deepcopy(audio))
    review['scenes'][0]['review_sha256'] = review_digest(selection['review'])
    record_selection(root, 'entry', selection)
    retained = (root / 'artifacts/selected_attempts.json').read_bytes()
    status = episode_production_status(root)
    assert status['shot_readiness'] == {'entry': 'ready'}, status
    assert status['selections']['entry']['unknown'] == []
    assert status['selections']['entry']['critical'] == []
    assert status['delivery'] == 'shots_ready_final_pending'
    certify_final(root, review, probe=lambda _: {'duration_seconds': 8})
    monkeypatch.setattr('lib.production_review.probe_master', lambda _: {'duration_seconds': 8})
    status = episode_production_status(root)
    assert status['final_review']['eligible'], status
    assert status['delivery'] == 'certified_final', status
    assert (root / 'artifacts/selected_attempts.json').read_bytes() == retained


@pytest.mark.parametrize('audio_status,readiness', [('unknown', 'pending'), ('fail', 'critical')])
def test_historical_audio_status_and_selection_gate_are_retained(production, audio_status, readiness):
    from lib.episode_production_controls import episode_production_status
    from lib.production_execution import ProductionGovernanceError, record_selection
    root, review, _, selection = production
    next(p for p in selection['review']['predicates'] if p['name'] == 'speaker_source')['status'] = audio_status
    save(root / 'artifacts/selected_attempts.json', {'entry': selection})
    retained = (root / 'artifacts/selected_attempts.json').read_bytes()
    status = episode_production_status(root)
    assert status['shot_readiness']['entry'] == readiness, status
    assert status['delivery'] == 'draft_only'
    with pytest.raises(ProductionGovernanceError):
        record_selection(root, 'entry', selection)
    review['scenes'][0]['review_sha256'] = review_digest(selection['review'])
    assert not validate_final_review(root, review, probe=lambda _: {'duration_seconds': 8})['eligible']
    assert (root / 'artifacts/selected_attempts.json').read_bytes() == retained


@pytest.mark.parametrize('defect',['visual','master','tail','authority'])
def test_visual_policy_preserves_other_gates(production,defect):
    root=production[0]; review=visual_review(production)
    if defect=='visual':review['dimensions']['visual']['status']='unknown'
    if defect=='master':(root/'master.bin').write_bytes(b'changed')
    if defect=='tail':review['visual_review']['end_seconds']=7
    if defect=='authority':(root/'audio-policy.json').write_text('{}')
    assert not validate_final_review(root,review,probe=lambda _: {'duration_seconds':8})['eligible']

from tests.lib.test_shot_contract import package, refresh


def test_future_motion_requires_visual_predicates_but_no_speaker(package):
    from lib.shot_contract import validate_shot_contract
    contract, root=package
    contract['project_id']='thisaccountisawarcrime-future-motion'
    save(root/'project.json',{'project_id':contract['project_id'],'story_revision':contract['story_revision'],'governance':{'mode':'strict'}})
    authorize(root)
    for review in [contract['project_review']]+[s['review'] for s in contract['shots']]:
        review['predicates']=[p for p in review['predicates'] if p['name']!='speaker_source']
    refresh(contract)
    assert validate_shot_contract(contract,project_dir=root,shot_id='entry')['eligible']
    next(p for p in contract['shots'][0]['review']['predicates'] if p['name']=='completed_action')['status']='fail'
    assert not validate_shot_contract(contract,project_dir=root,shot_id='entry')['eligible']


def test_future_firstcut_missing_audio_is_diagnostic(production,monkeypatch):
    from lib import production_draft as draft
    from tools.base_tool import ToolResult
    root=production[0];authorize(root)
    monkeypatch.setattr(draft,'_probe',lambda path:{'duration_seconds':8,'video_codec':'h264','audio_codec':None,'has_audio':False,'width':1280,'height':720})
    def compose(inputs):
        Path(inputs['output_path']).write_bytes(b'synthetic export')
        return ToolResult(success=True,data={'output':inputs['output_path']})
    record=draft.compose_first_cut(root,compose=compose)
    assert record['status']=='complete',record
    assert record['audio_missing']==['entry']
    assert record['technical']['has_audio'] is False


def authorize_project_setting(root, evidence_value=None):
    """Generic 1.1 explicit project setting; no identity name is required."""
    marker = json.loads((root / 'project.json').read_text())
    evidence = root / 'audio-setting.json'
    save(evidence, evidence_value or {'audio_review': 'disabled', 'scope': 'future_projects_only'})
    marker['governance']['audio_review'] = {'version': '1.1', 'mode': 'disabled',
        'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
        'evidence': {'path': evidence.name, 'sha256': file_sha256(evidence)}}
    save(root / 'project.json', marker)


def test_neutral_project_setting_disables_audio_review_with_bound_evidence(tmp_path):
    save(tmp_path / 'project.json', {'project_id': 'neutral-channel-ep1',
        'story_revision': 's1', 'governance': {'mode': 'strict'}})
    authorize_project_setting(tmp_path)
    assert audio_review_disabled(tmp_path)
    assert 'speaker_source' not in required_review_predicates(tmp_path, UPSTREAM_PREDICATES)
    (tmp_path / 'audio-setting.json').write_text('{}')
    assert not audio_review_disabled(tmp_path)


def test_neutral_project_setting_requires_disabled_future_scope(tmp_path):
    save(tmp_path / 'project.json', {'project_id': 'neutral-channel-ep1',
        'story_revision': 's1', 'governance': {'mode': 'strict'}})
    authorize_project_setting(tmp_path, {'audio_review': 'enabled', 'scope': 'future_projects_only'})
    assert not audio_review_disabled(tmp_path)
    marker = json.loads((tmp_path / 'project.json').read_text())
    marker['governance']['mode'] = 'guided'
    save(tmp_path / 'project.json', marker)
    authorize_project_setting(tmp_path)
    assert not audio_review_disabled(tmp_path)


def test_neutral_project_setting_is_bound_to_current_story_revision(tmp_path):
    save(tmp_path / 'project.json', {'project_id': 'neutral-channel-ep1',
        'story_revision': 's1', 'governance': {'mode': 'strict'}})
    authorize_project_setting(tmp_path)
    assert audio_review_disabled(tmp_path)
    marker = json.loads((tmp_path / 'project.json').read_text())
    marker['story_revision'] = 's2'
    save(tmp_path / 'project.json', marker)
    assert not audio_review_disabled(tmp_path)
