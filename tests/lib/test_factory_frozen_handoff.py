"""Already-generated outgoing-frame descendants survive authorized upstream edits."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict

import pytest

from lib import production_autonomy as autonomy, production_execution as execution
from lib import production_request as preparation, production_provenance as provenance
from lib import openart_mcp as mcp, openart_mcp_jobs as jobs, production_boards as boards
from lib.shot_contract import file_sha256, selection_digest, UPSTREAM_PREDICATES
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_openart_mcp_native import observations as guided_observations  # noqa: F401
from tests.lib.test_production_factory import frame_shapes_fixture, write, file_bytes
from tests.integration.test_first_pass_workflow import attestation
from tests.integration.test_openart_first_pass_workflow import real_av_clip
from tests.lib.test_first_cut_historical_mcp import collect_fixture
from tests.lib.test_trimmed_derivative import cut_fixture, binding, sample_record, reseal, save


def prepare(root, sid):
    contract = execution.load_shot_contract(root)
    index, shot = next((i, s) for i, s in enumerate(contract['shots']) if s['id'] == sid)
    frame = next(a for a in contract['assets'] if a['id'] == shot['frame_input']['asset_id'])
    path = str(root / frame['path'])
    account = mcp.account_summary()
    authority = autonomy.derive_standing_upload_authority(root, shot_id=sid, provider='openart_mcp',
        files=[path], account=account, openart_project_id='fixture-project')
    upload = jobs.prepare_upload(root, files=[path],
        billing_declaration={'upload_authorization_id': authority['upload_id']}, openart_project_id='fixture-project')
    jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result={'projectId': 'fixture-project',
        'results': [{'fileId': 'file-' + sid, 'status': 'SUCCESS', 'visualReference': {
            'id': 'reference-' + sid, 'type': 'image', 'url': 'https://fixture.invalid/' + sid + '.png',
            'label': 'SYNTHETIC ' + sid, 'imageUrl': 'https://fixture.invalid/' + sid + '.png',
            'thumbnailUrl': 'https://fixture.invalid/' + sid + '.png', 'metadata': {'fixture_only': True}}}]})
    authored = preparation.compile_provider_prompt(root, sid, provider='openart_mcp')
    inputs = {'project_dir': str(root), 'governance': {'shot_id': sid}, 'prompt': authored['prompt'],
        'duration': 5, 'resolution': '720p', 'aspect_ratio': '9:16',
        'model': 'byte-plus-seedance-2-mini', 'mode': 'image2video', 'openart_project_id': 'fixture-project',
        'image_path': path, 'output_path': str(root / 'assets/video' / (sid + '.mp4')),
        'compiled_request_id': 'original-' + sid, 'preparation_review_id': 'original-' + sid}
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='supported')
    native = preparation.prep_builder('openart_mcp')(inputs, profile)
    timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC timing only', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC',
        'segments': [], 'action_windows': [{'source_pointer': f'/shot_contract/shots/{index}/' + key,
            'value_sha256': preparation.digest(shot[key]), 'start_seconds': start, 'end_seconds': end,
            'rationale': 'SYNTHETIC timing only'} for key, start, end in [
                ('dominant_action', .1, 3), ('completed_end_state', 3.1, 4.4)]]}
    review = {'version': '1.0', 'reviewer': 'SYNTHETIC offline observer', 'status': 'unknown',
        'evidence_kind': 'fixture_only', 'predicates': [{'name': name, 'status': 'unknown',
            'severity': 'critical', 'evidence': 'SYNTHETIC fixture, no semantic certification'}
            for name in sorted(preparation.PREDICATES)]}
    preparation.prepare_compiled_request(inputs, native, profile, coverage=authored['coverage'],
        timing=timing, review=review, persist=True)
    scope = autonomy.derive_scope(root, inputs, provider='openart_mcp')
    inputs['governance']['scope_id'] = scope['id']
    aid = 'original-' + sid
    jobs.prepare(root, attempt_id=aid, generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, aid, authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, aid, outcome={'historyId': 'history-' + sid, 'status': 'PENDING'})
    return inputs


@pytest.fixture
def frozen_handoff(neutral_package, guided_observations, monkeypatch):
    from tests.lib import test_production_factory as factory
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    root, package, bind = neutral_package
    package = copy.deepcopy(package)
    node = next(n for n in package['boards']['image_nodes'] if n['slot_id'] == 'other')
    node.update(upstream_source={'shot_id': 's1', 'role': 'outgoing_frame'},
                reference_ids=[], image_dependencies=[], role='start_frame', transparent_background=False,
                prompt='Reuse s1 outgoing frame as start_frame.')
    original_prepare = preparation.prepare_package
    def with_dependency(root, binding):
        value = json.loads((root / binding['path']).read_text())
        # Before activation, bind the real declared outgoing-frame dependency.
        spec = value['boards']
        next(a for a in spec['motion_binding']['contract_template']['assets'] if a['id'] == 'other')['upstream_source'] = {'shot_id': 's1', 'role': 'outgoing_frame'}
        next(s for s in spec['shots'] if s['shot_id'] == 's2')['video_dependencies'] = ['s1']
        next(s for s in spec['motion_binding']['contract_template']['shots'] if s['id'] == 's2')['upstream'] = [{'shot_id': 's1'}]
        return original_prepare(root, bind(value))
    creator = factory.creator_fixture
    def author(root, name, record):
        record = copy.deepcopy(record)
        for term in record.get('source_transfer', []):
            term.update(max_file_bytes=10000000, max_batch_bytes=80000000)
        return creator(root, name, record)
    monkeypatch.setattr(factory, 'creator_fixture', author)
    retain = factory.retain_factory_board
    monkeypatch.setattr(preparation, 'prepare_package', with_dependency)
    monkeypatch.setattr(factory, 'retain_factory_board', lambda root, slot, **kw: None if slot == 'other' else retain(root, slot, **kw))
    frame_shapes_fixture((root, package, bind), monkeypatch, cast_bearing=True, public_layout=True)
    monkeypatch.setattr(preparation, 'prepare_package', original_prepare)
    inputs = {}
    media = real_av_clip(root / 'offline-original.mp4', seconds=5, size='720x1280')
    for sid in ('s1', 's2'):
        inputs[sid] = prepare(root, sid)
        collect_fixture(root, jobs, 'original-' + sid, media, monkeypatch)
        selected = execution._factory_outgoing_selection(root, sid, 'original-' + sid, sample=True)
        execution._publish_selection_locked(root, sid, selected, history_id='factory-original-original-' + sid)
        if sid == 's1':
            boards.bind_upstream_board(root, 'other', selected)
    return root, inputs


def derived(root, sid):
    value = SimpleNamespace(root=root, story=execution._read(root / 'project.json'))
    aid = 'original-' + sid
    parent = execution.load_attempt_result(root, aid)['output']
    _, _, _, record, _ = cut_fixture(value, aid, root / 'openart_mcp/attempts' / aid, parent,
        {'codec': 'libx264', 'audio': True, 'end': 2 if sid == 's1' else 2.5, 'name': sid, 'shot_id': sid})
    if sid == 's2':
        from tools.tool_registry import registry
        output = Path(record['output']['path'])
        inputs = {'operation': 'retime', 'input_path': parent['path'], 'output_path': str(output),
            'codec': 'libx264', 'reason': 'SYNTHETIC retained speech and reaction intervals',
            'intervals': [{'start_seconds': 0, 'end_seconds': 2, 'content': 'dialogue'},
                          {'start_seconds': 2.25, 'end_seconds': 3.6, 'content': 'nonspeaking'}]}
        result = registry.get('video_trimmer').execute(inputs)
        assert result.success, result.error
        recipe = Path(record['recipe']['path'])
        save(recipe, {'version': '1.0', 'operation': 'canonical_video_trimmer_retime', 'input': parent,
            'output_path': str(output), 'submitted_inputs': inputs, 'reason': inputs['reason']})
        record['recipe'], record['output'] = binding(recipe), binding(output)
        record['preserved_output'] = {'path': str(root / 'production_derived_edits' / file_sha256(output) / 'output.mp4'),
                                      'sha256': file_sha256(output)}
        receipt = Path(record['execution_receipt']['path'])
        save(receipt, {'tool': 'video_trimmer', 'provider': 'ffmpeg', 'canonical_registry_used': True,
            'success': True, 'generation': False, 'input': parent, 'recipe': record['recipe'],
            'output': record['output'], 'tool_result': asdict(result), 'audio': {
                'mode': 'retimed_intervals_pitch_preserved_aac', 'input_sha256': provenance._aac_sha256(parent['path']),
                'output_sha256': provenance._aac_sha256(output), 'intervals': result.data['intervals']}})
        record['execution_receipt'] = binding(receipt)
        sample_record(record, provenance.trimmed_outgoing_timestamp(output))
        reseal(record)
    execution.record_derived_edit(root, record)
    selection = {'attempt_id': aid, 'output': record['output'], 'outgoing_frame': record['outgoing_frame']}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES, revision='r1')
    selection['review']['status'] = 'unknown'
    for predicate in selection['review']['predicates']:
        predicate['status'] = 'unknown'
    execution.record_selection(root, sid, selection)
    return record


def test_factory_generated_descendant_keeps_frozen_sources_after_upstream_edit(frozen_handoff):
    from lib import production_draft as draft
    root, inputs = frozen_handoff
    originals = {p: p.read_bytes() for p in (root / 'openart_mcp/attempts').rglob('*') if p.is_file()}
    policy = autonomy.require_active_policy(root)[0]
    counts = autonomy.root_attempt_counts(root, policy)
    derived(root, 's1')
    parent = execution.load_attempt_result(root, 'original-s2')['output']
    with pytest.raises(ValueError, match='canonical reviewed source/reference/upstream changed'):
        provenance.validate_attempt_provenance(root, 'original-s2', shot_id='s2', story_revision='r1', expected_output=parent)
    with pytest.raises(ValueError):
        preparation.source_packet(root, 's2', provider='openart_mcp', check_native_controls=False)
    with pytest.raises(ValueError):
        execution.prepare_openart_mcp_handoff(inputs['s2'])
    proof = provenance.historical_source_provenance(root, 'original-s2', shot_id='s2', story_revision='r1', expected_output=parent)
    assert proof['historical_upstream_changed'] and proof['historical_source_only']
    assert jobs.collect(root, 'original-s2', downloaded_path=parent['path'])['status'] == 'collected'
    assert jobs.resume_original(root, 'original-s2')['status'] == 'collected'
    record = derived(root, 's2')
    passing = copy.deepcopy(execution.load_selected_attempts(root)['s2'])
    passing['review']['status'] = 'pass'
    for predicate in passing['review']['predicates']:
        predicate['status'] = 'pass'
    with pytest.raises(ValueError, match='canonical reviewed source/reference/upstream changed'):
        execution.record_selection(root, 's2', passing)
    rows = {row['shot_id']: row for row in draft.first_cut_candidates(root)}
    assert rows['s2']['output']['sha256'] == record['output']['sha256']
    assert rows['s2']['strict_selected'] is False and rows['s2']['duration_seconds'] == pytest.approx(2.5, abs=.1)
    assert any(f['name'] == 'upstream_source_changed' for f in rows['s2']['findings'])
    delivered = draft.deliver_standalone(root)
    assert delivered['status'] == 'partial' and delivered['provider_calls'] == 0
    assert next(c for c in delivered['clips'] if c['shot_id'] == 's2')['output']['sha256'] == record['output']['sha256']
    assert autonomy.root_attempt_counts(root, policy) == counts
    assert all(p.read_bytes() == raw for p, raw in originals.items())


@pytest.mark.parametrize('damage', ['outgoing_bytes', 'upstream_original', 'selection_history',
    'upload_receipt', 'upload_authority', 'account', 'counter'])
def test_frozen_handoff_keeps_exact_source_transfer_and_original_bounds(frozen_handoff, guided_observations, damage):
    root, _ = frozen_handoff
    selected = execution.load_selected_attempts(root)['s1']
    changed = copy.deepcopy(selected)
    changed['review']['reviewer'] = 'SYNTHETIC later observation of same original'
    execution._publish_selection_locked(root, 's1', changed)
    output = execution.load_attempt_result(root, 'original-s2')['output']
    def prove():
        return provenance.historical_source_provenance(root, 'original-s2', shot_id='s2',
            story_revision='r1', expected_output=output)
    assert prove()['historical_upstream_changed']
    if damage in {'outgoing_bytes', 'upstream_original'}:
        path = Path(selected['outgoing_frame' if damage == 'outgoing_bytes' else 'output']['path'])
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b'SYNTHETIC tampering')
    elif damage == 'selection_history':
        path = root / 'production_selections/factory-original-original-s1.json'
        row = execution._read(path)
        row['selection']['review']['reviewer'] = 'SYNTHETIC forged original observation'
        path.chmod(0o644)
        path.write_text(json.dumps(row))
    elif damage == 'upload_receipt':
        native = jobs.frozen_request(root, 'original-s2')['native']
        path = jobs._state_root(create=False) / 'mcp-uploads' / (native['input_assets'][0]['receipt_sha256'] + '.json')
        row = jobs._read(path)
        row['visual_reference']['url'] = 'https://fixture.invalid/foreign.png'
        jobs._write(path, row)
    elif damage == 'upload_authority':
        paths = list((root / 'artifacts').glob('openart_mcp_upload_authorization-*.json'))
        path = next(p for p in paths if execution._read(p)['derived_from_standing_run']['shot_id'] == 's2')
        row = execution._read(path)
        row['approved_by'] = 'SYNTHETIC foreign approval'
        path.chmod(0o644)
        path.write_text(json.dumps(row))
    elif damage == 'account':
        path = guided_observations / '2026-10-07-account-observation.json'
        row = json.loads(path.read_text())
        row['data']['user']['uid'] = 'SYNTHETIC foreign account'
        path.write_text(json.dumps(row))
    else:
        path = jobs._path(root, 'original-s2')
        row = jobs._read(path)
        row['frozen']['authority']['scope_attempt_index'] = 99
        row['frozen_sha256'] = preparation.digest(row['frozen'])
        row['authority_sha256'] = preparation.digest(row['frozen']['authority'])
        jobs._write(path, row)
    before = file_bytes(root)
    with pytest.raises(ValueError):
        prove()
    assert file_bytes(root) == before
