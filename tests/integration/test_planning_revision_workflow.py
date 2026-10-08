"""Offline proof: an authorized append-only planning revision of one shot's choreography.

Mirrors C76: entry selected under the original plan, interior board repaired and
its repaired attempt generated, the unused payoff carried, then interior's
``dominant_action``/``completed_end_state`` revised to match the footage. The
original entry stays eligible across both plan changes, interior needs a fresh
review bound to the revision, and payoff dispatches once under its retained
carried scope. Every other difference fails closed.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib import production_continuity as continuity
from lib.production_execution import ProductionGovernanceError, approval_plan_digest, record_selection
from lib.shot_contract import UPSTREAM_PREDICATES, contract_digest, file_sha256, selection_digest
from tests.integration.test_first_pass_workflow import (PIXEL, SYNTHETIC, attestation, production,  # noqa: F401
    read, save)
from tests.integration.test_repair_continuity_workflow import (carry, commit, provenance, rebyte, scopes,
    shot, stage)

ACTION = 'Courier hands the parcel over directly in one clean motion.'
END = 'Parcel rests in the recipient hands; courier steps back.'


def _bind(p, rel, data):
    path = p.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return {'path': rel, 'sha256': file_sha256(path)}


def _story_proof(p, record, rel, sources):
    proof = {'version': '1.0', 'project_id': record['project_id'],
             'story_revision': record['story_revision'],
             'prior_contract_sha256': record['prior_contract']['sha256'],
             'revised_contract_sha256': record['revised_contract']['sha256'],
             'target_shots': sorted(continuity.changed_shots(record)),
             'sources': copy.deepcopy(sources)}
    proof['review'] = attestation(continuity.execution._digest(proof), {'story_protected'},
                                  revision=record['story_revision'])
    return dict(_bind(p, rel, proof), reviewed_by=proof['review']['reviewer'])


def _setup(p):
    """Board repair, repaired interior attempt, carried payoff, prior snapshot."""
    entry_selection, rejected_id, repair_inputs = stage(p)
    repaired = p.selector.execute(copy.deepcopy(repair_inputs)); assert repaired.success, repaired.error
    old_interior = p.select('interior', repaired)
    continuity.append_carried_scope(p.root, carry(p, entry_selection))
    prior = _bind(p, 'approvals/rev/prior.json', p.contract)
    return entry_selection, repaired, old_interior, prior


def _revise(p, entry_selection, repaired, prior, *, mutate=None, **overrides):
    before = {field: shot(p, 'interior')[field] for field in continuity.REVISION_FIELDS}
    shot(p, 'interior').update(dominant_action=ACTION, completed_end_state=END)
    if mutate:
        mutate(p)
    commit(p)
    revised = _bind(p, 'approvals/rev/revised.json', p.contract)
    story = p.story['story_revision']
    authority = {'consent': _bind(p, 'approvals/rev/consent.md', SYNTHETIC + '\nEnd-to-end consent.'),
                 'policy': _bind(p, 'approvals/rev/policy.json', {'end_to_end': True}),
                 'activation': _bind(p, 'approvals/rev/activation.json',
                                     {'project_id': p.root.name, 'story_revision': story}),
                 'approved_by': 'synthetic-owner'}
    output_sha = file_sha256(repaired.artifacts[0])
    by_id = {item['id']: item for item in scopes(p)}
    record = {'revision_id': 'interior-choreography-1', 'project_id': p.root.name, 'story_revision': story,
              'prior_contract': prior, 'revised_contract': revised,
              'changes': [{'shot_id': 'interior', 'field': field, 'before': before[field],
                           'after': {'dominant_action': ACTION, 'completed_end_state': END}[field]}
                          for field in continuity.REVISION_FIELDS],
              'authority': authority,
              'retained_attempts': [
                  {'attempt_id': entry_selection['attempt_id'], 'shot_id': 'entry',
                   'output_sha256': entry_selection['output']['sha256']},
                  {'attempt_id': repaired.data['production_attempt_id'], 'shot_id': 'interior',
                   'output_sha256': output_sha}],
              'retained_scopes': [{'scope_id': sid, 'scope_sha256': continuity.execution._digest(by_id[sid])}
                                  for sid in ('first-generation', 'interior-repair', 'payoff-carried')]}
    record.update(overrides)
    record['protected_story_proof'] = _story_proof(p, record, 'approvals/rev/proof.json',
                                                [record['retained_attempts'][1]])
    record['revision_sha256'] = continuity.revision_digest(record)
    return record


def _fresh_selection(p, repaired, old, revision=None):
    selection = {key: copy.deepcopy(old[key]) for key in ('attempt_id', 'output', 'outgoing_frame')}
    if revision is not None:
        selection['planning_revision'] = revision
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    return selection


def test_revision_keeps_original_entry_requires_fresh_review_and_carries_payoff(production):
    p = production
    entry_selection, repaired, old_interior, prior = _setup(p)
    record = _revise(p, entry_selection, repaired, prior)
    # Before the revision is recorded the revised plan invalidates entry.
    with pytest.raises(ProductionGovernanceError):
        provenance(p, 'entry', entry_selection)
    continuity.append_planning_revision(p.root, record)
    with pytest.raises(ProductionGovernanceError, match='already exists'):
        continuity.append_planning_revision(p.root, record)
    # Original entry is retained through the board repair and the revision.
    provenance(p, 'entry', entry_selection)
    # The old passing interior selection cannot feed payoff after the revision.
    p.bind_upstream('payoff')
    stale_inputs = copy.deepcopy(p.inputs['payoff'])
    stale_inputs['governance']['scope_id'] = 'payoff-carried'
    before_calls = len(p.transport.native_requests)
    with pytest.raises(ProductionGovernanceError, match='predates the revision'):
        p.selector.execute(copy.deepcopy(stale_inputs))
    assert len(p.transport.native_requests) == before_calls
    binding = {'revision_id': record['revision_id'], 'revision_sha256': record['revision_sha256'],
               'contract_sha256': contract_digest(p.contract)}
    # Changed interior: old selection, unbound fresh review, or old review with
    # the new binding all fail; a fresh bound review passes.
    with pytest.raises(ProductionGovernanceError):
        record_selection(p.root, 'interior', copy.deepcopy(old_interior))
    with pytest.raises(ProductionGovernanceError, match='planning_revision'):
        record_selection(p.root, 'interior', _fresh_selection(p, repaired, old_interior))
    reused = dict(copy.deepcopy(old_interior), planning_revision=binding)
    with pytest.raises(ProductionGovernanceError, match='stale'):
        record_selection(p.root, 'interior', reused)
    stale = _fresh_selection(p, repaired, old_interior, dict(binding, contract_sha256='0' * 64))
    with pytest.raises(ProductionGovernanceError, match='planning_revision'):
        record_selection(p.root, 'interior', stale)
    fresh = _fresh_selection(p, repaired, old_interior, binding)
    record_selection(p.root, 'interior', fresh)
    # Unchanged payoff dispatches once under its retained carried scope.
    p.bind_upstream('payoff')
    provenance(p, 'entry', entry_selection)
    calls = len(p.transport.native_requests)
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = 'payoff-carried'
    result = p.selector.execute(copy.deepcopy(inputs)); assert result.success, result.error
    selection = p.select('payoff', result)
    provenance(p, 'payoff', selection)
    provenance(p, 'entry', entry_selection)
    provenance(p, 'interior', fresh)
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(copy.deepcopy(inputs))
    assert len(p.transport.native_requests) == calls + 1


def test_selection_digest_backward_compatible_and_binding_changes_subject():
    base = {'attempt_id': 'a', 'output': {'path': 'o', 'sha256': '1' * 64},
            'outgoing_frame': {'path': 'f', 'sha256': '2' * 64}}
    legacy = continuity.execution._digest({k: base[k] for k in ('attempt_id', 'output', 'outgoing_frame')})
    assert selection_digest(dict(base, review={'x': 1})) == legacy
    bound = dict(base, planning_revision={'revision_id': 'r', 'revision_sha256': '3' * 64,
                                          'contract_sha256': '4' * 64})
    assert selection_digest(bound) != legacy


def _forbidden(kind):
    def dialogue(p): shot(p, 'interior')['dialogue'][0]['text'] += ' Changed.'
    def other_shot(p): shot(p, 'payoff')['dominant_action'] += ' Changed.'
    def board(p): rebyte(p, 'payoff-start', 'rev')
    def duration(p): shot(p, 'interior')['duration_seconds'] += 1
    return locals()[kind]


@pytest.mark.parametrize('kind', ['dialogue', 'other_shot', 'board', 'duration'])
def test_revision_rejects_changes_outside_choreography(production, kind):
    p = production
    entry_selection, repaired, _, prior = _setup(p)
    record = _revise(p, entry_selection, repaired, prior, mutate=_forbidden(kind))
    with pytest.raises(ProductionGovernanceError):
        continuity.append_planning_revision(p.root, record)
    with pytest.raises(ProductionGovernanceError):
        provenance(p, 'entry', entry_selection)


def _tamper(kind):
    def wrong_before(r, p): r['changes'][0]['before'] = 'Something else.'
    def forbidden_field(r, p): r['changes'][0]['field'] = 'dialogue'
    def digest(r, p): r['revision_id'] = 'renamed'
    def bad_output(r, p): r['retained_attempts'][1]['output_sha256'] = '0' * 64
    def missing_authority(r, p): r['authority'].pop('activation')
    def proof_targets(r, p): (p.root / r['protected_story_proof']['path']).write_text('{"target_shots": ["entry"]}')
    def consent_bytes(r, p): (p.root / r['authority']['consent']['path']).write_text('tampered')
    def snapshot_bytes(r, p): (p.root / r['prior_contract']['path']).write_text('{}')
    def scope_digest(r, p): r['retained_scopes'][2]['scope_sha256'] = '0' * 64
    return locals()[kind]


@pytest.mark.parametrize('kind', ['wrong_before', 'forbidden_field', 'digest', 'bad_output', 'missing_authority',
                                  'proof_targets', 'consent_bytes', 'snapshot_bytes', 'scope_digest'])
def test_revision_tamper_rejects(production, kind):
    p = production
    entry_selection, repaired, _, prior = _setup(p)
    record = _revise(p, entry_selection, repaired, prior)
    sealed = kind not in {'digest'}
    _tamper(kind)(record, p)
    if sealed:
        record['revision_sha256'] = continuity.revision_digest(record)
    with pytest.raises(ProductionGovernanceError):
        continuity.append_planning_revision(p.root, record)


@pytest.mark.parametrize('kind', ['failed_review', 'unknown_review', 'provisional_review',
    'failed_story', 'unknown_story', 'missing_story', 'cosmetic_story', 'extra_failed_critical',
    'extra_unknown_critical', 'duplicate_predicate', 'missing_evidence', 'stale_subject',
    'stale_story', 'stale_project', 'stale_prior', 'stale_revised', 'missing_sources',
    'foreign_source', 'changed_source_bytes', 'wrong_reviewer', 'legacy_proof'])
def test_story_proof_rejects_failed_incomplete_stale_or_unbound_evidence(production, kind):
    p = production
    entry, repaired, _, prior = _setup(p)
    record = _revise(p, entry, repaired, prior)
    path = p.root / record['protected_story_proof']['path']
    proof = read(path)
    review = proof['review']
    if kind.endswith('_review'):
        review['status'] = 'fail' if kind == 'failed_review' else kind.removesuffix('_review')
    elif kind in {'failed_story', 'unknown_story'}:
        review['predicates'][0]['status'] = 'fail' if kind == 'failed_story' else 'unknown'
    elif kind == 'missing_story':
        review['predicates'][0]['name'] = 'unrelated'
    elif kind == 'cosmetic_story':
        review['predicates'][0]['severity'] = 'cosmetic'
    elif kind.startswith('extra_'):
        review['predicates'].append({'name': 'protected_dialogue', 'severity': 'critical',
            'status': 'fail' if kind == 'extra_failed_critical' else 'unknown', 'evidence': SYNTHETIC})
    elif kind == 'duplicate_predicate':
        review['predicates'].append(copy.deepcopy(review['predicates'][0]))
    elif kind == 'missing_evidence':
        review['predicates'][0].pop('evidence')
    elif kind == 'stale_subject':
        review['subject_sha256'] = '0' * 64
    elif kind == 'stale_story':
        proof['story_revision'] = 'old-story'
    elif kind == 'stale_project':
        proof['project_id'] = 'other-project'
    elif kind == 'stale_prior':
        proof['prior_contract_sha256'] = '0' * 64
    elif kind == 'stale_revised':
        proof['revised_contract_sha256'] = '0' * 64
    elif kind == 'missing_sources':
        proof['sources'] = []
    elif kind == 'foreign_source':
        proof['sources'] = [record['retained_attempts'][0]]
    elif kind == 'changed_source_bytes':
        Path(repaired.artifacts[0]).write_bytes(b'changed footage')
    elif kind == 'wrong_reviewer':
        review['reviewer'] = 'another-reviewer'
    elif kind == 'legacy_proof':
        proof = {'target_shots': ['interior']}
    # Reseal the evidence and record: rejection must enforce the proof's meaning,
    # rather than merely noticing bytes differ from their retained hashes.
    if kind != 'stale_subject' and 'review' in proof:
        proof['review']['subject_sha256'] = continuity.execution._digest(
            {key: value for key, value in proof.items() if key != 'review'})
    save(path, proof)
    record['protected_story_proof']['sha256'] = file_sha256(path)
    record['revision_sha256'] = continuity.revision_digest(record)
    calls = len(p.transport.native_requests)
    with pytest.raises(ProductionGovernanceError, match='protected story proof'):
        continuity.append_planning_revision(p.root, record)
    assert not (p.root / 'production_revisions.json').exists()
    assert len(p.transport.native_requests) == calls


@pytest.mark.parametrize('include_all_sources', [False, True])
def test_story_proof_requires_actual_footage_for_every_changed_shot(production, include_all_sources):
    p = production
    entry, repaired, _, prior = _setup(p)
    original = read(p.root / prior['path'])['shots'][0]['dominant_action']
    changed = original + ' Revised incidental choreography.'
    record = _revise(p, entry, repaired, prior,
                     mutate=lambda p: shot(p, 'entry').update(dominant_action=changed))
    record['changes'].append({'shot_id': 'entry', 'field': 'dominant_action',
                              'before': original, 'after': changed})
    sources = record['retained_attempts'] if include_all_sources else record['retained_attempts'][1:]
    record['protected_story_proof'] = _story_proof(p, record, 'approvals/rev/proof.json', sources)
    record['revision_sha256'] = continuity.revision_digest(record)
    if include_all_sources:
        continuity.append_planning_revision(p.root, record)
    else:
        with pytest.raises(ProductionGovernanceError, match='footage for every changed shot'):
            continuity.append_planning_revision(p.root, record)


def test_recorded_revision_tamper_and_unretained_scope_reject(production):
    p = production
    entry_selection, repaired, old_interior, prior = _setup(p)
    record = _revise(p, entry_selection, repaired, prior)
    record['retained_scopes'] = record['retained_scopes'][:2]
    record['retained_attempts'] = record['retained_attempts'][1:]
    record['revision_sha256'] = continuity.revision_digest(record)
    continuity.append_planning_revision(p.root, record)
    # Entry is not retained; payoff's carried scope is not retained.
    with pytest.raises(ProductionGovernanceError, match='not retained'):
        provenance(p, 'entry', entry_selection)
    revision = continuity.revision_for_current(p.root, p.contract)
    carried = next(item for item in scopes(p) if item['id'] == 'payoff-carried')
    with pytest.raises(ProductionGovernanceError, match='scope payoff-carried is not retained'):
        continuity.require_retained_scope(revision, carried, 'payoff')
    # The dispatch reaches the scope check: upstream interior is freshly bound first.
    record_selection(p.root, 'interior', _fresh_selection(p, repaired, old_interior, {
        'revision_id': record['revision_id'], 'revision_sha256': record['revision_sha256'],
        'contract_sha256': contract_digest(p.contract)}))
    p.bind_upstream('payoff')
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = 'payoff-carried'
    calls = len(p.transport.native_requests)
    with pytest.raises(ProductionGovernanceError, match='scope payoff-carried is not retained'):
        p.selector.execute(copy.deepcopy(inputs))
    assert len(p.transport.native_requests) == calls
    # Rewriting the stored record breaks its digest.
    path = p.root / 'production_revisions.json'
    document = read(path)
    document['revisions'][0]['retained_attempts'].append(
        {'attempt_id': entry_selection['attempt_id'], 'shot_id': 'entry',
         'output_sha256': entry_selection['output']['sha256']})
    save(path, document)
    with pytest.raises(ProductionGovernanceError, match='revision_sha256'):
        provenance(p, 'entry', entry_selection)
    # A second revision targeting the same plan is ambiguous/refused.
    save(path, {'version': '1.0', 'revisions': []})
    continuity.append_planning_revision(p.root, record)
    second = dict(copy.deepcopy(record), revision_id='second')
    second['revision_sha256'] = continuity.revision_digest(second)
    with pytest.raises(ProductionGovernanceError, match='already targets'):
        continuity.append_planning_revision(p.root, second)


# --- Linked revisions: C76 S01 original board, S02 R1, S03 frozen pre-R2, S04 after R2 ---

TUG = 'Sen tugs the parcel by its rim and holds it; it stops ringing and snores.'


@pytest.fixture
def production4(tmp_path, monkeypatch):
    """Four serial shots: entry, interior, payoff, coda (coda upstream payoff)."""
    import tests.integration.test_first_pass_workflow as base

    def read4(path):
        value = original(path)
        if str(path).endswith('story-package.json'):
            coda = copy.deepcopy(value['shots'][-1])
            coda.update(id='coda', duration_seconds=4, upstream_shot_id='payoff',
                        initial_state='Snoring parcel on table, Sen and Mira beside it.',
                        dominant_action='Mira tiptoes away from the snoring parcel.',
                        completed_end_state='Mira gone from frame; parcel still snoring.',
                        dialogue=[{'speaker_id': 'sen', 'source': 'visible', 'text': 'Shh.',
                                   'start_seconds': 1, 'end_seconds': 2}])
            value['shots'].append(coda)
            value['clip_count'] = len(value['shots'])
        return value

    original = base.read
    monkeypatch.setattr(base, 'read', read4)
    root = base.init_project('offline-courier4', title='Synthetic courier linked revisions', pipeline_type='cinematic',
                             pipeline_dir=tmp_path, governance='strict', story_revision='courier-story-1')
    result = base.Production(root, monkeypatch)
    monkeypatch.setattr(base, 'read', original)
    yield result
    assert result.transport.actual_provider_calls == 0
    result.network.assert_not_called()


def _linked(p, rid, sid, field_values, prior, retained, source, tag):
    """Revision ``rid`` of shot ``sid`` choreography; snapshots under approvals/<tag>/."""
    before = {field: shot(p, sid)[field] for field in field_values}
    shot(p, sid).update(field_values)
    commit(p)
    story = p.story['story_revision']
    record = {'revision_id': rid, 'project_id': p.root.name, 'story_revision': story, 'prior_contract': prior,
              'revised_contract': _bind(p, f'approvals/{tag}/revised.json', p.contract),
              'changes': [{'shot_id': sid, 'field': f, 'before': before[f], 'after': v} for f, v in field_values.items()],
              'authority': {'consent': _bind(p, f'approvals/{tag}/consent.md', SYNTHETIC + '\nEnd-to-end consent.'),
                            'policy': _bind(p, f'approvals/{tag}/policy.json', {'end_to_end': True}),
                            'activation': _bind(p, f'approvals/{tag}/activation.json',
                                                {'project_id': p.root.name, 'story_revision': story}),
                            'approved_by': 'synthetic-owner'},
              'retained_attempts': [{'attempt_id': s['attempt_id'], 'shot_id': shot_id,
                                     'output_sha256': s['output']['sha256']} for shot_id, s in retained],
              'retained_scopes': [{'scope_id': item['id'], 'scope_sha256': continuity.execution._digest(item)}
                                  for item in scopes(p)]}
    record['protected_story_proof'] = _story_proof(p, record, f'approvals/{tag}/proof.json',
        [{'attempt_id': source['attempt_id'], 'shot_id': sid, 'output_sha256': source['output']['sha256']}])
    record['revision_sha256'] = continuity.revision_digest(record)
    return record


def _binding(record, p):
    return {'revision_id': record['revision_id'], 'revision_sha256': record['revision_sha256'],
            'contract_sha256': contract_digest(p.contract)}


def _dispatch(p, sid):
    p.bind_upstream(sid)
    inputs = copy.deepcopy(p.inputs[sid])
    inputs['governance']['scope_id'] = 'payoff-carried'
    return inputs


def _chain_to_r2(p, *, omit_entry=False):
    entry_selection, rejected_id, repair_inputs = stage(p)
    repaired = p.selector.execute(copy.deepcopy(repair_inputs)); assert repaired.success, repaired.error
    old_interior = p.select('interior', repaired)
    continuity.append_carried_scope(p.root, carry(p, entry_selection, shot_ids=('payoff', 'coda')))
    # R1: interior choreography; entry original + repaired interior retained.
    prior1 = _bind(p, 'approvals/r1/prior.json', p.contract)
    r1 = _linked(p, 'interior-choreography-1', 'interior', {'dominant_action': ACTION, 'completed_end_state': END},
                 prior1, [('entry', entry_selection), ('interior', old_interior)], old_interior, 'r1')
    continuity.append_planning_revision(p.root, r1)
    interior = _fresh_selection(p, repaired, old_interior, _binding(r1, p))
    record_selection(p.root, 'interior', interior)
    # Payoff dispatched under the pre-R1 carried scope, frozen at the R1 plan.
    result = p.selector.execute(_dispatch(p, 'payoff')); assert result.success, result.error
    old_payoff = p.select('payoff', result)
    provenance(p, 'payoff', old_payoff)
    # R2: payoff choreography only, linked to R1's revised plan.
    prior2 = _bind(p, 'approvals/r2/prior.json', p.contract)
    retained = [('interior', old_interior), ('payoff', old_payoff)]
    if not omit_entry:
        retained.insert(0, ('entry', entry_selection))
    r2 = _linked(p, 'payoff-choreography-2', 'payoff', {'dominant_action': TUG}, prior2, retained, old_payoff, 'r2')
    return entry_selection, interior, old_payoff, result, r1, r2


def test_linked_revisions_retain_original_entry_r1_interior_and_dispatch_after_r2(production4):
    p = production4
    entry_selection, interior, old_payoff, payoff_result, r1, r2 = _chain_to_r2(p)
    with pytest.raises(ProductionGovernanceError):
        provenance(p, 'entry', entry_selection)  # R2 not yet recorded
    continuity.append_planning_revision(p.root, r2)
    chain = continuity.revision_chain(p.root, p.contract)
    assert [item['record']['revision_id'] for item in chain] == ['payoff-choreography-2', 'interior-choreography-1']
    # Original entry (board-repair plan), R1 interior, pre-R2 payoff all remain eligible.
    provenance(p, 'entry', entry_selection)
    provenance(p, 'interior', interior)
    provenance(p, 'payoff', old_payoff)
    # Coda cannot consume the payoff selection that predates R2.
    calls = len(p.transport.native_requests)
    with pytest.raises(ProductionGovernanceError, match='predates the revision'):
        p.selector.execute(_dispatch(p, 'coda'))
    assert len(p.transport.native_requests) == calls
    # Changed payoff needs a fresh review bound to R2; none, R1, or reused old review fail.
    with pytest.raises(ProductionGovernanceError):
        record_selection(p.root, 'payoff', copy.deepcopy(old_payoff))
    with pytest.raises(ProductionGovernanceError, match='planning_revision'):
        record_selection(p.root, 'payoff', _fresh_selection(p, payoff_result, old_payoff))
    with pytest.raises(ProductionGovernanceError, match='planning_revision'):
        record_selection(p.root, 'payoff', _fresh_selection(p, payoff_result, old_payoff, _binding(r1, p)))
    with pytest.raises(ProductionGovernanceError, match='stale'):
        record_selection(p.root, 'payoff', dict(copy.deepcopy(old_payoff), planning_revision=_binding(r2, p)))
    payoff = _fresh_selection(p, payoff_result, old_payoff, _binding(r2, p))
    record_selection(p.root, 'payoff', payoff)
    # Interior keeps its R1 binding: an R2 binding is rejected (its R1 selection stays recorded).
    with pytest.raises(ProductionGovernanceError, match='planning_revision'):
        record_selection(p.root, 'interior', _fresh_selection(p, None, interior, _binding(r2, p)))
    continuity.require_selection_binding(chain, interior, read(p.root / r1['revised_contract']['path']), 'interior')
    # Coda dispatches once under the carried scope after R2, then is selected.
    inputs = _dispatch(p, 'coda')
    coda = p.selector.execute(copy.deepcopy(inputs)); assert coda.success, coda.error
    coda_selection = p.select('coda', coda)
    for sid, selection in [('entry', entry_selection), ('payoff', payoff), ('coda', coda_selection)]:
        provenance(p, sid, selection)
    assert len(p.transport.native_requests) == calls + 1
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(copy.deepcopy(inputs))
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(_dispatch(p, 'payoff'))
    assert len(p.transport.native_requests) == calls + 1


def test_linked_revision_omitting_entry_or_unlinked_or_cyclic_rejects(production4):
    p = production4
    entry_selection, _, old_payoff, _, r1, r2 = _chain_to_r2(p, omit_entry=True)
    # A revision that undoes R1 back to its prior plan would form a cycle.
    current = p.contract
    p.contract = read(p.root / r2['prior_contract']['path'])
    commit(p)
    cyclic = _linked(p, 'undo-1', 'interior', {
        'dominant_action': r1['changes'][0]['before'], 'completed_end_state': r1['changes'][1]['before']},
        r2['prior_contract'], [(item['shot_id'], {'attempt_id': item['attempt_id'], 'output': {
            'sha256': item['output_sha256']}}) for item in r1['retained_attempts']],
        {'attempt_id': r1['retained_attempts'][1]['attempt_id'],
         'output': {'sha256': r1['retained_attempts'][1]['output_sha256']}}, 'cyc')
    with pytest.raises(ProductionGovernanceError, match='cycle'):
        continuity.append_planning_revision(p.root, cyclic)
    # A revision from R1's prior plan (not its revised plan) does not link.
    p.contract = read(p.root / r1['prior_contract']['path'])
    commit(p)
    unlinked = _linked(p, 'unlinked', 'payoff', {'dominant_action': TUG}, r1['prior_contract'],
                       [(item['shot_id'], {'attempt_id': item['attempt_id'], 'output': {'sha256': item['output_sha256']}})
                        for item in r2['retained_attempts']], old_payoff, 'unl')
    unlinked['revision_sha256'] = continuity.revision_digest(unlinked)
    with pytest.raises(ProductionGovernanceError, match='does not link'):
        continuity.append_planning_revision(p.root, unlinked)
    # R2 that omits the original entry: appended, but entry is no longer eligible.
    p.contract = current
    commit(p)
    continuity.append_planning_revision(p.root, r2)
    with pytest.raises(ProductionGovernanceError, match='not retained'):
        provenance(p, 'entry', entry_selection)
