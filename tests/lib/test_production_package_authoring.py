"""Compact authoring expands repetition, never approval or observed evidence."""
import copy
import hashlib
import json

import pytest

from lib import production_request as request, production_images as images
from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_factory import file_bytes, write, standing_treatment_fixture
from lib import production_autonomy as pa, production_execution as execution


def packages(base):
    header = {'reviewer': 'persistent reviewer', 'story': base['boards']['story'],
        'approval': base['boards']['approval'], 'references': [],
        'story_meaning': base['boards']['motion_binding']['contract_template']['story'],
        'late_cast_ids': [], 'payoff_speaker_ids': [], 'payoff_asset_id': 's2-start'}
    compact = {k: copy.deepcopy(base[k]) for k in ('version', 'project_id', 'story_revision', 'title')}
    compact.update(authoring_form=request.COMPACT_AUTHORING_FORM, planning_mode='shot_local_v1', compact=header, shots=[])
    def review(sid, names):
        return {'review_id': sid + '-unobserved', 'reviewer': header['reviewer'],
            'story_revision': base['story_revision'], 'subject_sha256': '0'*64,
            'status': 'unknown', 'predicates': [{'name': n, 'severity': 'critical',
                'status': 'unknown', 'evidence': 'UNOBSERVED: review current bytes.'} for n in sorted(names)]}
    spec = {'version': '1.1', **{k: copy.deepcopy(header[k]) for k in ('reviewer', 'story', 'approval', 'references')},
        'image_nodes': [], 'shared_roles': [], 'shots': []}
    template = {'version': '1.0', 'planning_mode': 'shot_local_v1', 'reference_mode': 'frame_conditioned',
        'story': header['story_meaning'], 'late_cast_ids': [], 'payoff_speaker_ids': [],
        'payoff_asset_id': 's2-start', 'project_review': review('project', PROJECT_PREDICATES), 'assets': [], 'shots': []}
    full = {k: copy.deepcopy(compact[k]) for k in ('version', 'project_id', 'story_revision', 'title', 'planning_mode')}
    full.update(boards=spec, shots=[])
    for index, original in enumerate(base['shots'][:2]):
        shot = copy.deepcopy(original); shot['cast'] = []
        sid = shot['id']; aid = sid + '-start'; deps = [] if index == 0 else ['s1']
        motion = {'reference_mode': 'frame_conditioned', 'frame_input': {'asset_id': aid, 'role': 'start_frame'},
            'initial_state': 'SYNTHETIC explicit initial', 'endpoint_completion': 'unknown',
            'required_visible_speakers': [], 'dialogue': [], 'allowed_transformations': [],
            'transition': {'type': 'continuous', 'rationale': 'SYNTHETIC explicit continuous action'}}
        node = {'slot_id': aid, 'role': 'payoff_board' if index else 'start_frame', 'prompt': 'SYNTHETIC explicit ' + aid,
            'reference_ids': [], 'image_dependencies': [], 'required_predicates': ['asset_quality', 'cast_identity'],
            'transparent_background': False, 'cast_ids': []}
        compact['shots'].append({**copy.deepcopy(shot), 'motion': motion, 'boards': [node], 'video_dependencies': deps})
        full['shots'].append({**shot, 'motion': {k: v for k, v in motion.items() if k not in {'reference_mode', 'frame_input'}}})
        spec['image_nodes'].append({k: v for k, v in node.items() if k != 'cast_ids'})
        spec['image_nodes'][-1].update(shot_id=sid, output_path='assets/images/candidates/' + aid + '.png')
        spec['shots'].append({'shot_id': sid, 'roles': [{'asset_id': aid, 'role': 'start_frame'}], 'video_dependencies': deps})
        template['assets'].append({'id': aid, 'role': node['role'], 'cast_ids': []})
        template['shots'].append({'id': sid, **motion, 'asset_ids': [aid], 'upstream': [{'shot_id': d} for d in deps],
            'review': review(sid, SHOT_PREDICATES)})
    spec['motion_binding'] = {'contract_template': template, 'motion_handoffs': {}}
    return compact, full


def without_binding(compiled):
    result = copy.deepcopy(compiled)
    for record in result['records'].values():
        record['metadata'].pop('package')
    return result


def test_compact_full_projection_and_replay(neutral_package):
    root, base, bind = neutral_package
    compact, full = packages(base)
    full_binding = bind(full); before = file_bytes(root)
    full_projection = request.compile_package(root, full_binding)
    assert file_bytes(root) == before
    binding = bind(compact); before = file_bytes(root)
    projection = request.compile_package(root, binding)
    assert file_bytes(root) == before
    assert without_binding(projection) == without_binding(full_projection)
    reviews = [projection['spec']['motion_binding']['contract_template']['project_review']]
    reviews += [s['review'] for s in projection['spec']['motion_binding']['contract_template']['shots']]
    assert all(r['status'] == 'unknown' and all(p['status'] == 'unknown' for p in r['predicates']) for r in reviews)
    prepared = request.prepare_package(root, binding)
    before = file_bytes(root)
    request.prepare_package(root, binding)
    assert file_bytes(root) == before
    for name in ('script', 'scene_plan', 'asset_manifest'):
        path = root / ('artifacts/' + name + '.json'); payload = path.read_bytes(); path.unlink()
        request.prepare_package(root, binding)
        assert path.read_bytes() == payload
    assert hashlib.sha256((root / binding['path']).read_bytes()).hexdigest() == binding['sha256']
    assert images.image_usage(root)['total'] == prepared['provider_calls'] == prepared['reservations'] == 0


@pytest.mark.parametrize('damage', ['mixed', 'unknown_form', 'missing_semantics', 'missing_story', 'unsafe_slot',
    'duplicate_slot', 'duplicate_shot', 'wrong_frame', 'wrong_payoff_role', 'wrong_treatment', 'ambiguous_start', 'wrong_hash', 'output_conflict', 'canonical_conflict'])
def test_compact_fails_without_writes(neutral_package, damage):
    root, base, bind = neutral_package
    compact, full = packages(base)
    if damage == 'mixed': compact['boards'] = full['boards']
    elif damage == 'unknown_form': compact['authoring_form'] = 'compact-maybe'
    elif damage == 'missing_semantics': del compact['shots'][0]['motion']['dialogue']
    elif damage == 'missing_story': del compact['compact']['story_meaning']
    elif damage == 'unsafe_slot': compact['shots'][0]['boards'][0]['slot_id'] = '../escape'
    elif damage == 'duplicate_slot': compact['shots'][1]['boards'][0]['slot_id'] = 's1-start'
    elif damage == 'duplicate_shot': compact['shots'][1]['id'] = 's1'
    elif damage == 'wrong_payoff_role': compact['shots'][1]['boards'][0]['role'] = 'start_frame'
    elif damage == 'wrong_treatment': compact['shots'][0]['motion']['reference_mode'] = 'reference_guided'
    elif damage == 'wrong_frame': compact['shots'][0]['motion']['frame_input']['asset_id'] = 's2-start'
    elif damage == 'ambiguous_start':
        node = copy.deepcopy(compact['shots'][0]['boards'][0]); node['slot_id'] = 'extra-start'
        compact['shots'][0]['boards'].append(node)
    elif damage == 'wrong_hash': compact['compact']['story']['sha256'] = 'a'*64
    elif damage == 'output_conflict': write(root, 'assets/images/candidates/s1-start.png', {'occupied': True})
    elif damage == 'canonical_conflict': write(root, 'artifacts/script.json', {'conflict': True})
    binding = bind(compact); before = file_bytes(root)
    with pytest.raises(ValueError): request.compile_package(root, binding)
    with pytest.raises(ValueError): request.prepare_package(root, binding)
    assert file_bytes(root) == before
    assert images.image_usage(root)['total'] == 0


@pytest.mark.parametrize('damage', [None, 'command_authorization', 'cap'])
def test_compact_activation_uses_real_standing_gates(neutral_package, damage):
    root, base, bind = neutral_package
    compact, _ = packages(base)
    request.prepare_package(root, bind(compact))
    kwargs = standing_treatment_fixture(root)
    if damage == 'command_authorization':
        command = json.loads((root / kwargs['run_command']['path']).read_text())
        command.pop('authorization')
        kwargs['run_command'] = write(root, kwargs['run_command']['path'], command)
    elif damage == 'cap':
        kwargs['policy']['caps']['max_total_attempts'] += 100
    with execution._lock(root):
        pass
    before = file_bytes(root)
    if damage:
        with pytest.raises((pa.StandingSetupRequired, ValueError)):
            pa.activate_standing_run(root, **kwargs)
        assert file_bytes(root) == before
    else:
        result = pa.activate_standing_run(root, **kwargs)
        assert result['provider_calls'] == 0
        assert pa.activate_standing_run(root, **kwargs) == dict(result, replayed=True)
        assert all((root / path).read_bytes() == raw for path, raw in before.items())
    assert images.image_usage(root)['total'] == 0


def test_compact_explicit_identity_reference_binds_nonempty_cast(neutral_package):
    root, base, bind = neutral_package
    compact, _ = packages(base)
    reference = copy.deepcopy(base['boards']['references'][0])
    reference.update(role='identity_reference', cast_ids=['person'])
    reference['applicability']['roles'].append('identity_reference')
    compact['compact']['references'] = [reference]
    for shot in compact['shots']:
        shot['cast'] = ['person']; shot['boards'][0]['cast_ids'] = ['person']
        shot['boards'][0]['reference_ids'] = [reference['id']]
    projection = request.compile_package(root, bind(compact))
    template = projection['spec']['motion_binding']['contract_template']
    assert template['assets'][-1] == {'id': 'cast', 'role': 'identity_reference', 'cast_ids': ['person']}
    assert projection['spec']['shared_roles'] == [{'asset_id': 'cast', 'role': 'identity_reference'}]
    request.prepare_package(root, bind(compact))
    contract = execution.load_shot_contract(root)
    identity = next(a for a in contract['assets'] if a['id'] == 'cast')
    assert identity['sha256'] == reference['sha256'] and identity['cast_ids'] == ['person']
    assert identity['review'] == reference['review']  # retained source evidence, never synthesized
    del compact['compact']['references'][0]['cast_ids']
    with pytest.raises(ValueError, match='explicit cast_ids'):
        request.compile_package(root, bind(compact))
