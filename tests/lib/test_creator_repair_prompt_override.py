"""Exact retained creator prompt overrides; synthetic offline native footage only."""
import copy
import json

import pytest

from lib import production_request as request, production_repair_batches as batches
from lib import production_execution as execution, production_draft as draft
from tests.lib.test_first_cut import p  # noqa: F401
from tests.integration.test_first_pass_workflow import production  # noqa: F401
from tests.lib.test_production_repair_batches import fake_cut, cut_binding, exact_scope
from tests.lib.test_episode_production_controls import approval


def retained(p, *, rejected=False):
    plan = execution._read(p.root / 'artifacts/scene_plan.json')
    for scene in plan['scenes']:
        scene['script_section_id'] = scene['id']
    (p.root / 'artifacts/scene_plan.json').write_text(json.dumps(plan))
    original = p.generate('entry')
    if rejected:
        from tests.integration.test_first_pass_workflow import attestation
        from lib.shot_contract import file_sha256
        review = attestation(file_sha256(original.artifacts[0]), ['completed_action'])
        review.update(status='fail', predicates=[{'name': 'completed_action', 'status': 'fail',
                      'severity': 'critical', 'evidence': 'SYNTHETIC wrong endpoint'}])
        execution.record_rejection(p.root, original.data['production_attempt_id'], review)
    else:
        p.select('entry', original)
    cut = draft.compose_first_cut(p.root, compose=fake_cut)
    shot = next(s for s in p.contract['shots'] if s['id'] == 'entry')
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs['prompt'] = 'Creator-authored shorter staging. ' + ' '.join(line['text'] for line in shot['dialogue'])
    inputs['output_path'] = str(p.root / 'assets/video/exact-creator.mp4')
    inputs['compiled_request_id'] = 'creator-shorter'
    inputs['preparation_review_id'] = 'creator-shorter'
    item = {'item_id': 'shorter', 'shot_id': 'entry', 'source_attempt_id': original.data['production_attempt_id'],
            'selected_route': {'provider': 'grok_cli', 'tool': 'grok_cli_video', 'model': None,
                               'mode': inputs['operation']},
            'changes': 'Exact shorter staging prompt; original speech, story, cast and native inputs stay bound.',
            'inputs': inputs}
    batches.record_creator_repair_batch(p.root, batch_id='exact-shorter', cut=cut_binding(cut),
                                       items=[item], evidence=approval(p.root, 'shorter-approved'))
    bridge = batches.prepare_creator_repair_prompt(p.root, 'exact-shorter', 'shorter')
    inputs = bridge['inputs']
    native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/offline/grok'})
    profile = {'source': 'fixture'}
    timing = {'method': 'segmented_estimate', 'duration_seconds': shot['duration_seconds'], 'language': 'en',
              'margin_seconds': 0.1, 'rationale': 'SYNTHETIC source timing retained',
              'overlap_policy': 'reviewed_parallel', 'overlap_rationale': 'SYNTHETIC timing fixture',
              'segments': [{'dialogue_index': i, 'text_sha256': request.digest(line['text']),
                            'language': 'en', 'word_count': len(line['text'].split()),
                            'words_per_minute': 240, 'pause_seconds': 0,
                            'rationale': 'SYNTHETIC timing fixture'} for i, line in enumerate(shot['dialogue'])],
              'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
                                  'value_sha256': request.digest(shot[key]),
                                  'start_seconds': start, 'end_seconds': end,
                                  'rationale': 'SYNTHETIC unchanged action timing'}
                                 for key, start, end in [('dominant_action', 0, 4), ('completed_end_state', 4, 5)]]}
    compiled = request.prepare_compiled_request(inputs, native, profile, coverage=bridge['coverage'],
                                               timing=timing, creator_repair=bridge['creator_repair'])
    return bridge, inputs, native, profile, compiled, timing, item, cut


def test_exact_creator_prompt_retains_full_sources_and_honest_dialogue_trace(p):
    bridge, inputs, native, profile, compiled, timing, _, _ = retained(p)
    packet = request.source_packet(p.root, 'entry', provider='grok_cli', native=native)
    assert compiled['source_binding'] == packet['binding'] == bridge['source_binding']
    assert compiled['creator_prompt_override']['source_occurrences_sha256'] == request.digest(packet['occurrences'])
    assert len(compiled['coverage']) == len(packet['shot']['dialogue']) < len(packet['occurrences'])
    assert all('/dialogue/' in row['source_pointer'] and row['source_pointer'].endswith('/text')
               for row in compiled['coverage'])
    assert native['request']['arguments']['prompt'] == inputs['prompt']
    assert request.validate_creator_prompt_override(compiled, inputs, native, packet)['shot_id'] == 'entry'
    assert len(p.transport.native_requests) == 1  # only the synthetic original; preparation makes no call
    with pytest.raises(ValueError, match='coverage omits'):
        request.prepare_compiled_request(inputs, native, profile, coverage=bridge['coverage'], timing=timing)
    assert 'status' not in compiled and 'review' not in compiled


@pytest.mark.parametrize('tamper', ['prompt', 'prompt_sha', 'source_sha', 'intent_sha', 'coverage', 'extra_coverage'])
def test_override_rejects_changed_prompt_marker_or_fake_coverage(p, tamper):
    bridge, inputs, native, profile, compiled, _, _, _ = retained(p)
    if tamper == 'prompt':
        inputs['prompt'] += ' unapproved edit'
        native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/offline/grok'})
    elif tamper == 'coverage':
        compiled['coverage'][0]['start'] += 1
    elif tamper == 'extra_coverage':
        compiled['coverage'].append(copy.deepcopy(compiled['coverage'][0]))
    else:
        key = {'prompt_sha': 'prompt_sha256', 'source_sha': 'source_occurrences_sha256'}.get(tamper)
        if key:
            compiled['creator_prompt_override'][key] = '0' * 64
        else:
            compiled['creator_prompt_override']['creator_repair']['intent_sha256'] = '0' * 64
    packet = request.source_packet(p.root, 'entry', provider='grok_cli', native=native)
    with pytest.raises(ValueError):
        request._validate_compiled(compiled, inputs, native, profile, packet)
    assert len(p.transport.native_requests) == 1


def test_pending_original_collection_precedes_invalid_creator_preparation(p, monkeypatch):
    _, _, _, _, _, _, item, _ = retained(p, rejected=True)
    original_state = execution._state
    def pending_state(root, attempt):
        if attempt['attempt_id'] == item['source_attempt_id']:
            return {'status': 'uncertain'}
        return original_state(root, attempt)
    monkeypatch.setattr(execution, '_state', pending_state)
    def invalid_prepared(root):
        raise ValueError('SYNTHETIC stale creator binding')
    monkeypatch.setattr(batches, 'prepared_creator_repair_items', invalid_prepared)
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'collect_grok_original'
    assert action['subject'] == item['source_attempt_id']
    assert len(p.transport.native_requests) == 1


def test_override_cannot_claim_ordinary_scope(p):
    _, inputs, native, profile, compiled, _, _, _ = retained(p)
    inputs['governance']['scope_id'] = 'first-generation'
    packet = request.source_packet(p.root, 'entry', provider='grok_cli', native=native)
    with pytest.raises(ValueError, match='exact retained repair scope'):
        request._validate_compiled(compiled, inputs, native, profile, packet)


def test_override_literal_duplicate_dialogue_needs_distinct_ordered_spans():
    rows = [{'occurrence_id': 'occ-' + str(i), 'source_pointer': '/shot_contract/shots/0/dialogue/' + str(i) + '/text',
             'value': 'Help me.', 'value_sha256': request.digest('Help me.')} for i in range(2)]
    packet = {'occurrences': rows}
    with pytest.raises(ValueError, match='ordered dialogue'):
        request.creator_prompt_coverage(packet, 'Help me.')
    coverage = request.creator_prompt_coverage(packet, 'Help me. Pause. Help me.')
    assert coverage[0]['end'] <= coverage[1]['start']
    assert [r['occurrence_id'] for r in coverage] == ['occ-0', 'occ-1']


@pytest.mark.parametrize('tamper', ['approval', 'batch', 'source_bytes', 'cut_bytes'])
def test_override_revalidates_retained_authority_and_sources(p, tamper):
    bridge, inputs, native, profile, compiled, _, _, _ = retained(p)
    batch = batches._load(p.root, 'exact-shorter')
    path = {'approval': p.root / batch['evidence']['path'],
            'batch': p.root / batches.DIRECTORY / 'exact-shorter/intent.json',
            'source_bytes': execution._inside(batch['items'][0]['source_output']['path'], p.root),
            'cut_bytes': p.root / batch['cut']['path']}[tamper]
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b' changed')
    packet = request.source_packet(p.root, 'entry', provider='grok_cli', native=native)
    with pytest.raises(ValueError):
        request._validate_compiled(compiled, inputs, native, profile, packet)


def test_acceptance_closes_fresh_override_but_historical_preparation_stays_valid(p):
    _, inputs, native, profile, compiled, _, item, cut = retained(p)
    scope = exact_scope(p, item, 'creator-scope')
    batches.prepare_repair_item(p.root, 'exact-shorter', 'shorter', scope_id=scope['id'])
    inputs['governance']['scope_id'] = scope['id']
    packet = request.source_packet(p.root, 'entry', provider='grok_cli', native=native)
    review = {'version': '1.0', 'review_id': 'creator-shorter', 'subject_sha256': request.digest(compiled),
              'reviewer': 'SYNTHETIC reviewer', 'status': 'pass', 'evidence_kind': 'fixture_only',
              'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical',
                              'evidence': 'SYNTHETIC preparation fixture'} for name in sorted(request.PREDICATES)]}
    assert request.validate_preparation_evidence(compiled, review, inputs, native, profile, packet)
    draft.record_first_cut_acceptance(p.root, cut_binding(cut), accepted_by='SYNTHETIC creator',
                                      evidence={k: v for k, v in approval(p.root, 'cut-accepted').items() if k != 'approved_by'})
    with pytest.raises(ValueError, match='intent is closed'):
        request.validate_preparation_evidence(compiled, review, inputs, native, profile, packet)
    assert request.validate_preparation_evidence(compiled, review, inputs, native, profile, packet, historical=True)
    assert len(p.transport.native_requests) == 1


def test_prepared_exact_creator_scope_takes_priority_over_older_critical_request(p, monkeypatch):
    bridge, inputs, native, profile, compiled, _, item, _ = retained(p, rejected=True)
    scope = exact_scope(p, item, 'creator-scope')
    batches.prepare_repair_item(p.root, 'exact-shorter', 'shorter', scope_id=scope['id'])
    monkeypatch.setattr(execution, '_critical_repair', lambda *args: {
        'provider': 'grok_cli', 'model': None, 'replaces_attempt_ids': [item['source_attempt_id']]})
    frozen = (p.root / 'production_scopes.json').read_bytes()
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion'
    assert action['binding']['creator_repair'] == bridge['creator_repair']
    assert action['binding']['scope_id'] == scope['id']
    assert 'repair' not in action['binding']
    drafted = execution.draft_motion_preparation(p.root, action)
    assert drafted['inputs']['prompt'] == item['inputs']['prompt']
    assert drafted['creator_repair'] == bridge['creator_repair']
    assert drafted['coverage'] == compiled['coverage']
    assert (p.root / 'production_scopes.json').read_bytes() == frozen
    assert len(p.transport.native_requests) == 1
