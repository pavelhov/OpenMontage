"""Compact authoring expands repetition, never approval or observed evidence."""
import copy
import hashlib
import json

import pytest

from lib import production_request as request, production_images as images
from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_factory import file_bytes, write, creator_fixture, standing_treatment_fixture, guided_observations  # noqa: F401
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


def supplied_payoff_package(root, base):
    compact, _ = packages(base)
    compact['shots'] = compact['shots'][:1]
    shot = compact['shots'][0]
    shot.update(duration_seconds=5, cast=['actor', 'partner'], boards=[], roles=[
        {'asset_id': aid, 'role': 'reference_image'} for aid in ('actor', 'partner', 'R1')])
    shot['motion'].update(reference_mode='reference_guided')
    shot['motion'].pop('frame_input')
    references = []
    for aid, role, cast in [('actor', 'identity_reference', ['actor']),
                            ('partner', 'identity_reference', ['partner']),
                            ('R1', 'payoff_board', ['actor', 'partner'])]:
        path = root / 'assets/images' / (aid + '.png')
        path.write_bytes(('SYNTHETIC supplied ' + aid).encode())
        reference = copy.deepcopy(base['boards']['references'][0])
        reference.update(id=aid, role=role, cast_ids=cast, path=str(path.relative_to(root)),
                         sha256=request.file_sha256(path))
        reference['applicability']['roles'] = [role, 'reference_image']
        reference['review']['subject_sha256'] = reference['sha256']
        references.append(reference)
    compact['compact'].update(references=references, payoff_asset_id='R1')
    return compact


def test_compact_character_references_only_reaches_compiled_motion_without_images(neutral_package):
    """Public author-once path needs no invented payoff design or image call."""
    from lib import production_boards as boards
    root, base, bind = neutral_package
    compact = supplied_payoff_package(root, base)
    compact['shots'][0]['script'] = 'SYNTHETIC exact supplied context'
    del compact['compact']['payoff_asset_id']
    compact['compact']['references'] = compact['compact']['references'][:2]
    # Deliberately differ from lexical asset order: native consumer order is authored.
    compact['shots'][0]['roles'] = [{'asset_id': aid, 'role': 'reference_image'}
                                    for aid in ('partner', 'actor')]
    settings = {'provider': 'grok_cli', 'operation': 'reference_to_video'}
    templates = request.build_video_request_templates(compact, settings)
    binding = bind(compact)
    before = file_bytes(root)
    projection = request.compile_package(root, binding)
    assert file_bytes(root) == before
    assert projection['spec']['image_nodes'] == []
    assert 'payoff_asset_id' not in projection['spec']['motion_binding']['contract_template']
    request.prepare_package(root, binding)
    kwargs = standing_treatment_fixture(root)
    kwargs['baselines']['s1']['planned_request_template'] = execution.planned_request_template(
        templates['s1'], project_dir=root)
    assert pa.activate_standing_run(root, **kwargs)['provider_calls'] == 0
    action = execution.advance(root)['action']
    assert action['kind'] == 'prepare_motion'
    draft = execution.draft_motion_preparation(root, action, provider='grok_cli')
    inputs = draft['inputs']
    paths = [str(root / r['path']) for aid in ('partner', 'actor')
             for r in compact['compact']['references'] if r['id'] == aid]
    assert inputs['reference_image_paths'] == paths
    native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'})
    assert [(r['role'], r['path']) for r in native['request']['input_assets']] == [
        ('reference', path) for path in paths]
    timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC offline fixture', 'overlap_policy': 'serial',
        'overlap_rationale': 'SYNTHETIC serial', 'segments': [],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
            'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC offline fixture'}
            for key, start, end in [('dominant_action', .1, 3), ('completed_end_state', 3.1, 4.4)]]}
    compiled = request.prepare_compiled_request(inputs, native, {'source': 'real'},
        coverage=draft['coverage'], timing=timing)
    assert {r['id'] for r in compiled['source_binding']['references']} == {'actor', 'partner'}
    assert all(r['role'] == 'reference_image' for r in compiled['source_binding']['references'])
    assert boards.load_board_pack(root)['prompt_pack'] == {}
    assert images.image_usage(root)['total'] == 0 and execution._attempts(root) == []
    assert request.file_sha256(root / binding['path']) == binding['sha256']


def test_compact_reference_only_openart_authored_preparation_uses_retained_uploads(neutral_package, guided_observations, monkeypatch):
    """The existing preparation outcome seals real native inputs, with no host call."""
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs, production_provenance as provenance
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    root, base, bind = neutral_package
    compact = supplied_payoff_package(root, base)
    compact['shots'][0]['script'] = 'SYNTHETIC exact supplied context'
    del compact['compact']['payoff_asset_id']
    compact['compact']['references'] = compact['compact']['references'][:2]
    compact['shots'][0]['roles'] = [{'asset_id': aid, 'role': 'reference_image'}
                                    for aid in ('partner', 'actor')]
    settings = {'provider': 'openart_mcp', 'model': 'byte-plus-seedance-2-mini',
        'mode': 'element2video', 'operation': 'reference_to_video', 'resolution': '720p'}
    templates = request.build_video_request_templates(compact, settings)
    request.prepare_package(root, bind(compact))
    kwargs = standing_treatment_fixture(root, guided_routes=[{'model': settings['model'], 'mode': settings['mode']}])
    next(p for p in kwargs['policy']['providers'] if p['id'] == 'openart_mcp')['uid_sha256'] = mcp.account_summary()['uid_sha256']
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    setup['providers'] = copy.deepcopy(kwargs['policy']['providers'])
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    kwargs['baselines']['s1']['planned_request_template'] = execution.planned_request_template(templates['s1'], project_dir=root)
    pa.activate_standing_run(root, **kwargs)
    action = execution.advance(root)['action']
    draft = execution.draft_motion_preparation(root, action)
    authority = pa.derive_standing_upload_authority(root, **draft['upload_authority_kwargs'])
    upload = jobs.prepare_upload(root, files=draft['upload_authority_kwargs']['files'],
        billing_declaration={'upload_authorization_id': authority['upload_id']},
        openart_project_id=draft['inputs']['openart_project_id'])
    jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result={
        'projectId': draft['inputs']['openart_project_id'], 'results': [
            {'fileId': 'SYNTHETIC-file-' + str(i), 'status': 'SUCCESS', 'visualReference': {
                'id': 'SYNTHETIC-reference-' + str(i), 'type': 'image',
                'url': 'https://fixture.invalid/' + str(i) + '.png', 'label': 'SYNTHETIC ' + str(i),
                'imageUrl': 'https://fixture.invalid/' + str(i) + '.png',
                'thumbnailUrl': 'https://fixture.invalid/' + str(i) + '.png', 'metadata': {'fixture_only': True}}}
            for i in range(2)]})
    ready = execution.draft_motion_preparation(root, action)
    assert ready['upload_authority_kwargs']['files'] == [] and len(ready['reused_uploads']) == 2
    timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC offline fixture', 'overlap_policy': 'serial',
        'overlap_rationale': 'SYNTHETIC serial', 'segments': [],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
            'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC offline fixture'}
            for key, start, end in [('dominant_action', .1, 3), ('completed_end_state', 3.1, 4.4)]]}
    judgment = {'version': '1.0', 'reviewer': 'SYNTHETIC offline reviewer', 'status': 'unknown',
        'evidence_kind': 'fixture_only', 'predicates': [{'name': name, 'status': 'unknown',
            'severity': 'critical', 'evidence': 'SYNTHETIC unobserved ' + name} for name in sorted(request.PREDICATES)]}
    result = execution.complete_action(root, action, outcome={'preparation': {'timing': timing, 'review': judgment}})
    assert result['status'] == 'dispatched' and result['result']['success'], result
    assert result['dry_run']['provider_calls'] == 0
    assert result['action']['kind'] == 'begin_openart_original'
    [attempt] = execution._attempts(root)
    assert attempt['shot_id'] == 's1'
    record_id = ready['inputs']['compiled_request_id']
    compiled = json.loads((root / ('artifacts/compiled_request-' + record_id + '.json')).read_text())
    retained_review = json.loads((root / ('artifacts/preparation_review-' + record_id + '.json')).read_text())
    assert retained_review['status'] == 'unknown' and retained_review['reviewer'] == judgment['reviewer']
    native = jobs.frozen_request(root, attempt['attempt_id'])['native']
    assert request.digest(native['body']) == compiled['native_binding']['native_body_sha256']
    assert [(a['role'], a['source_sha256']) for a in native['input_assets']] == [
        ('reference_image', r['sha256']) for aid in ('partner', 'actor')
        for r in compact['compact']['references'] if r['id'] == aid]
    assert len(native['body']['params']['visualReferences']) == 2
    assert images.image_usage(root)['total'] == 0


def test_payoff_omission_does_not_relax_legacy_motion_template(neutral_package):
    root, base, bind = neutral_package
    del base['boards']['motion_binding']['contract_template']['payoff_asset_id']
    binding = bind(base); before = file_bytes(root)
    with pytest.raises(ValueError, match="payoff_asset_id.*required"):
        request.compile_package(root, binding)
    assert file_bytes(root) == before


def test_compact_all_supplied_payoff_reaches_motion_preparation_without_images(neutral_package, guided_observations, monkeypatch):
    from lib import production_boards as boards, openart_mcp as mcp
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    root, base, bind = neutral_package
    compact = supplied_payoff_package(root, base)
    original = copy.deepcopy(compact)
    settings = {'provider': 'openart_mcp', 'model': 'minimax-h3', 'mode': 'element2video',
                'operation': 'reference_to_video', 'native_params': {'duration': 5, 'resolution': '768P'}}
    templates = request.build_video_request_templates(compact, settings)
    binding = bind(compact)
    before = file_bytes(root)
    projection = request.compile_package(root, binding)
    assert file_bytes(root) == before and compact == original
    spec = projection['spec']
    assert spec['image_nodes'] == []
    assert spec['motion_binding']['contract_template']['assets'][-1] == {
        'id': 'R1', 'role': 'payoff_board', 'cast_ids': ['actor', 'partner']}
    prepared = request.prepare_package(root, binding)
    assert prepared['provider_calls'] == prepared['reservations'] == 0
    kwargs = standing_treatment_fixture(root, guided_routes=[{'model': settings['model'], 'mode': settings['mode']}])
    kwargs['baselines']['s1']['planned_request_template'] = execution.planned_request_template(templates['s1'], project_dir=root)
    pa.activate_standing_run(root, **kwargs)
    result = execution.advance(root)
    assert result['action']['kind'] == 'prepare_motion'
    pack = boards.load_board_pack(root)
    assert pack['prompt_pack'] == {} and all(b['origin'] == 'reuse' for b in pack['board_bindings'].values())
    contract = execution.load_shot_contract(root)
    payoff = next(a for a in contract['assets'] if a['id'] == 'R1')
    assert payoff['role'] == 'payoff_board' and payoff['cast_ids'] == ['actor', 'partner']
    assert payoff['review'] == compact['compact']['references'][-1]['review']
    assert images.image_usage(root)['total'] == 0 and execution._attempts(root) == []
    assert request.file_sha256(root / binding['path']) == binding['sha256']


@pytest.mark.parametrize('damage', ['missing_cast', 'duplicate_cast', 'invalid_cast', 'unsupported_semantic_role',
    'unsupported_native_role', 'missing_role_applicability', 'stale_applicability', 'stale_review',
    'wrong_review_hash', 'failed_review', 'duplicate_reference', 'generated_reference_collision'])
def test_compact_supplied_payoff_preserves_source_and_role_guards(neutral_package, damage):
    root, base, bind = neutral_package
    compact = supplied_payoff_package(root, base)
    reference = compact['compact']['references'][-1]
    if damage == 'missing_cast': reference.pop('cast_ids')
    elif damage == 'duplicate_cast': reference['cast_ids'] = ['actor', 'actor']
    elif damage == 'invalid_cast': reference['cast_ids'] = [None]
    elif damage == 'unsupported_semantic_role': reference['role'] = 'end_frame'
    elif damage == 'unsupported_native_role': compact['shots'][0]['roles'][-1]['role'] = 'reference_audio'
    elif damage == 'missing_role_applicability': reference['applicability']['roles'].remove('reference_image')
    elif damage == 'stale_applicability': reference['applicability']['story_revision'] = 'old'
    elif damage == 'stale_review': reference['review']['story_revision'] = 'old'
    elif damage == 'wrong_review_hash': reference['review']['subject_sha256'] = '0' * 64
    elif damage == 'failed_review': reference['review']['status'] = 'fail'
    elif damage == 'duplicate_reference': compact['compact']['references'].append(copy.deepcopy(reference))
    else:
        generated = copy.deepcopy(packages(base)[0]['shots'][0]['boards'][0])
        generated.update(slot_id='R1', role='payoff_board')
        compact['shots'][0]['boards'] = [generated]
    binding = bind(compact)
    before = file_bytes(root)
    with pytest.raises(ValueError): request.prepare_package(root, binding)
    assert file_bytes(root) == before and images.image_usage(root)['total'] == 0


def test_shootable_opt_in_compact_and_full_paths(neutral_package):
    root, base, bind = neutral_package
    compact, full = packages(base)
    legacy = request.compile_package(root, bind(compact))
    assert 'prompt_compiler' not in legacy['spec']['motion_binding']['contract_template']
    for package in (compact, full):
        package.update(prompt_compiler='shootable_v1', cast_names={})  # castless: explicit empty map, no dummy names
        package['shots'][0]['camera'] = 'Locked-off wide shot.'
        package['shots'][0]['sound'] = 'Room tone and footsteps only; no background music.'
    for package in (compact, full):
        template = request.compile_package(root, bind(package))['spec']['motion_binding']['contract_template']
        assert template['prompt_compiler'] == 'shootable_v1' and template['cast_names'] == {}
        assert template['shots'][0]['camera'] == 'Locked-off wide shot.'
        assert template['shots'][0]['sound'].endswith('no background music.')
        assert 'camera' not in template['shots'][1]


@pytest.mark.parametrize('damage', ['names_without_marker', 'camera_without_marker', 'missing_name',
                                    'missing_cast_names', 'unknown_compiler', 'blank_name'])
def test_shootable_opt_in_fails_closed(neutral_package, damage):
    root, base, bind = neutral_package
    compact, _ = packages(base)
    compact.update(prompt_compiler='shootable_v1', cast_names={'person': 'Mira, the red-coated courier'})
    for shot in compact['shots']:
        shot['cast'] = ['person']
    if damage == 'names_without_marker': del compact['prompt_compiler']
    elif damage == 'camera_without_marker':
        del compact['prompt_compiler'], compact['cast_names']; compact['shots'][0]['camera'] = 'Wide.'
    elif damage == 'missing_name': compact['cast_names'] = {}
    elif damage == 'missing_cast_names': del compact['cast_names']
    elif damage == 'unknown_compiler': compact['prompt_compiler'] = 'shootable_v2'
    elif damage == 'blank_name': compact['cast_names']['person'] = '  '
    binding = bind(compact); before = file_bytes(root)
    with pytest.raises(ValueError):
        request.compile_package(root, binding)
    with pytest.raises(ValueError):
        request.prepare_package(root, binding)
    assert file_bytes(root) == before


@pytest.mark.parametrize('explicit_roles', [False, True])
def test_compact_omitted_frame_uses_one_explicit_start_role(neutral_package, explicit_roles):
    root, base, bind = neutral_package
    compact, _ = packages(base)
    shot = compact['shots'][0]
    if explicit_roles:
        shot['roles'] = [copy.deepcopy(shot['motion']['frame_input'])]
    expected = request.compile_package(root, bind(compact))
    del shot['motion']['frame_input']
    original = copy.deepcopy(compact)
    actual = request.compile_package(root, bind(compact))
    assert without_binding(actual) == without_binding(expected)
    assert compact == original


@pytest.mark.parametrize('starts', [0, 2])
def test_compact_omitted_frame_rejects_missing_or_ambiguous_start(neutral_package, starts):
    root, base, bind = neutral_package
    compact, _ = packages(base)
    shot = compact['shots'][0]
    del shot['motion']['frame_input']
    shot['roles'] = ([{'asset_id': 's1-start', 'role': 'end_frame'}] if starts == 0 else
        [{'asset_id': 's1-start', 'role': 'start_frame'}, {'asset_id': 's2-start', 'role': 'start_frame'}])
    binding = bind(compact); before = file_bytes(root)
    with pytest.raises(ValueError, match='one explicit start_frame'):
        request.compile_package(root, binding)
    assert file_bytes(root) == before


def test_upstream_reuse_needs_no_authored_image_prompt(neutral_package):
    root, base, bind = neutral_package
    compact, _ = packages(base)
    board = compact['shots'][1]['boards'][0]
    # The payoff asset remains explicitly declared, with a separate native start consumer.
    board.update(slot_id='s2-upstream', role='start_frame', upstream_source={'shot_id': 's1', 'role': 'outgoing_frame'})
    compact['shots'][1]['motion']['frame_input'] = {'asset_id': 's2-upstream', 'role': 'start_frame'}
    compact['shots'][1]['boards'].append({'slot_id':'s2-start', 'role':'payoff_board', 'prompt':'SYNTHETIC payoff',
        'reference_ids':[], 'image_dependencies':[], 'required_predicates':['asset_quality','cast_identity'],
        'transparent_background':False, 'cast_ids':[]})
    del board['prompt']
    binding = bind(compact)
    before = file_bytes(root)
    projected = request.compile_package(root, binding)
    node = projected['spec']['image_nodes'][1]
    assert node['upstream_source'] == {'shot_id': 's1', 'role': 'outgoing_frame'}
    assert node['prompt'] == 'Reuse s1 outgoing frame as start_frame.'
    assert images.image_usage(root)['total'] == 0
    assert file_bytes(root) == before
