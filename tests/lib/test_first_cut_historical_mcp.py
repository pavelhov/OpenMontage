"""Offline MCP originals remain attributable across unrelated planning edits."""
import copy
import json

import pytest

from lib import production_draft as draft, production_execution as execution
from lib.production_provenance import (validate_attempt_provenance,
                                      validate_creator_repair_source_provenance)
from lib.shot_contract import file_sha256
from tests.lib import test_production_repair_batches as fixtures
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
    validate = validate_creator_repair_source_provenance if historical else validate_attempt_provenance
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
    assert cut['status'] == 'incomplete' and cut['export'] is not None
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
