"""Offline proof: one shot's board repair keeps other shots' approvals exact.

Mirrors C76: shot 1 selected, shot 2 first pass rejected, shot 2 start board
replaced under an exact repair approval (global plan digest changes), shot 1
stays eligible, and the unused shot 3 continues only through an explicit
carried scope sharing quota with the original first-pass scope.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib import production_continuity as continuity
from lib.production_execution import (ProductionGovernanceError, approval_plan_digest,
    load_selected_attempts, planned_request_digest)
from lib.production_provenance import validate_attempt_provenance
from lib.shot_contract import ASSET_PREDICATES, file_sha256
from tests.integration.test_first_pass_workflow import (PIXEL, SYNTHETIC, attestation, production,  # noqa: F401
    read, save, sign_planning_reviews)


def scopes(p):
    return read(p.root / 'production_scopes.json')['scopes']


def write_scopes(p, items):
    save(p.root / 'production_scopes.json', {'version': '1.0', 'scopes': items})


def asset(p, aid):
    return next(item for item in p.contract['assets'] if item['id'] == aid)


def shot(p, sid):
    return next(item for item in p.contract['shots'] if item['id'] == sid)


def rebyte(p, aid, tag):
    """Replace an asset's reviewed bytes under a new path (originals preserved)."""
    item = asset(p, aid)
    path = p.root / 'assets/images' / f'{aid}-{tag}.png'
    path.write_bytes(PIXEL + tag.encode())
    item.update(path=str(path.relative_to(p.root)), sha256=file_sha256(path))
    item['review'] = attestation(item['sha256'], ASSET_PREDICATES)


def commit(p):
    sign_planning_reviews(p.contract)
    p.persist_contract()


def provenance(p, sid, selection):
    return validate_attempt_provenance(p.root, selection['attempt_id'], shot_id=sid,
        story_revision=p.story['story_revision'], expected_output=selection['output'])


def stage(p):
    """Select entry, reject interior first pass, repair interior's own start board."""
    entry = p.generate('entry'); assert entry.success, entry.error
    entry_selection = p.select('entry', entry)
    p.bind_upstream('interior')
    rejected = p.generate('interior'); assert rejected.success, rejected.error
    rejected_id = rejected.data['production_attempt_id']
    old_plan = approval_plan_digest(p.contract)
    # Repair: new reviewed start board for interior only.
    rebyte(p, 'interior-start', 'repair')
    p.manifest['assets'] = [dict(item, path=asset(p, item['id'])['path']) if item['id'] == 'interior-start' else item
                            for item in p.manifest['assets']]
    commit(p)
    assert approval_plan_digest(p.contract) != old_plan
    evidence = p.root / 'artifacts/repair-approval.txt'
    evidence.write_text(SYNTHETIC + '\nApproved exactly one interior corrective attempt; preserve entry and unused payoff first pass.')
    inputs = copy.deepcopy(p.inputs['interior'])
    inputs['governance']['scope_id'] = 'interior-repair'
    inputs['output_path'] = str(p.root / 'assets/video/interior-repair.mp4')
    for key in ('image_path', 'reference_image_path'):
        if key in inputs:
            inputs[key] = str(p.root / asset(p, 'interior-start')['path'])
    repair = {**{k: copy.deepcopy(p.scope[k]) for k in ('project_id', 'story_revision', 'provider', 'approved_by')},
              'id': 'interior-repair', 'status': 'approved', 'phase': 'repair',
              'evidence': {'path': str(evidence), 'sha256': file_sha256(evidence)},
              'approval_plan_sha256': approval_plan_digest(p.contract),
              'replaces_attempt_ids': [rejected_id], 'attempts_per_shot': {'interior': 1},
              'requests': {'interior': planned_request_digest(inputs, project_dir=p.root)}}
    write_scopes(p, [p.scope, repair])
    return entry_selection, rejected_id, inputs


def carry(p, entry_selection, shot_ids=('payoff',), successor='payoff-carried', **overrides):
    kwargs = dict(source_scope_id='first-generation', repair_scope_id='interior-repair', successor_id=successor,
                  shot_ids=list(shot_ids),
                  source_contract_path=f"production_attempts/{entry_selection['attempt_id']}/shot_contract.json")
    kwargs.update(overrides)
    return continuity.derive_carried_scope(p.root, **kwargs)


def test_board_repair_then_carried_remaining_scope_workflow(production):
    p = production
    entry_selection, rejected_id, repair_inputs = stage(p)
    # Unrelated interior board repair keeps the immutable entry attempt eligible.
    provenance(p, 'entry', entry_selection)
    # The original first-pass scope can no longer dispatch the unused payoff.
    with pytest.raises(ProductionGovernanceError):
        p.generate('payoff')
    calls = len(p.transport.native_requests)
    # Exactly one corrective interior attempt runs under the repair scope.
    repaired = p.selector.execute(copy.deepcopy(repair_inputs)); assert repaired.success, repaired.error
    p.select('interior', repaired)
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(copy.deepcopy(repair_inputs))
    # Explicit carry of the unused payoff shot; quotas equal the source.
    before = scopes(p)
    scope = carry(p, entry_selection)
    assert scope['requests'] == {'payoff': p.scope['requests']['payoff']}
    assert scope['attempts_per_shot'] == {'payoff': 1}
    continuity.append_carried_scope(p.root, scope)
    assert scopes(p)[:2] == before
    with pytest.raises(ProductionGovernanceError, match='already exists'):
        continuity.append_carried_scope(p.root, scope)
    with pytest.raises(ProductionGovernanceError, match='already carried'):
        carry(p, entry_selection, successor='payoff-carried-again')
    p.bind_upstream('payoff')
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = 'payoff-carried'
    result = p.selector.execute(copy.deepcopy(inputs)); assert result.success, result.error
    selection = p.select('payoff', result)
    provenance(p, 'payoff', selection)
    provenance(p, 'entry', entry_selection)
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(copy.deepcopy(inputs))
    assert len(p.transport.native_requests) == calls + 2
    assert set(load_selected_attempts(p.root)) == {'entry', 'interior', 'payoff'}


def _mutate(kind):
    def own_board(p): rebyte(p, 'entry-end', 'x')
    def dialogue(p): shot(p, 'entry')['dialogue'][0]['text'] += ' Changed.'
    def action(p): shot(p, 'entry')['dominant_action'] += ' Changed.'
    def duration(p): shot(p, 'entry')['duration_seconds'] += 1
    def identity(p): rebyte(p, 'sen', 'x')
    def payoff(p): rebyte(p, 'payoff-board', 'x')
    def story(p): p.contract['story'] = str(p.contract['story']) + ' Changed.'
    def order(p): p.contract['shots'][1], p.contract['shots'][2] = p.contract['shots'][2], p.contract['shots'][1]
    def upstream(p): shot(p, 'payoff')['upstream'] = [{'shot_id': 'entry'}]
    def other_semantics(p): shot(p, 'payoff')['dominant_action'] += ' Changed.'
    return locals()[kind]


@pytest.mark.parametrize('kind', ['own_board', 'dialogue', 'action', 'duration', 'identity', 'payoff',
                                  'story', 'order', 'upstream', 'other_semantics'])
def test_retained_attempt_rejects_semantic_mutation(production, kind):
    p = production
    entry_selection, _, _ = stage(p)
    _mutate(kind)(p)
    commit(p)
    with pytest.raises(ProductionGovernanceError):
        provenance(p, 'entry', entry_selection)


def test_frozen_scope_digest_and_evidence_tamper_reject(production):
    p = production
    entry_selection, _, _ = stage(p)
    request_path = p.root / 'production_attempts' / entry_selection['attempt_id'] / 'request.json'
    request_path.chmod(0o644)
    original = request_path.read_bytes()
    request = json.loads(original)
    request['scope']['approval_plan_sha256'] = approval_plan_digest(p.contract)
    request_path.write_text(json.dumps(request))
    with pytest.raises(ProductionGovernanceError):
        provenance(p, 'entry', entry_selection)
    request_path.write_bytes(original)
    preserved = request_path.parent / request['approval_evidence']['path'] if not str(
        request['approval_evidence']['path']).startswith('/') else p.root / request['approval_evidence']['path']
    preserved.chmod(0o644)
    preserved.write_text('tampered')
    with pytest.raises(ProductionGovernanceError):
        provenance(p, 'entry', entry_selection)


def test_carry_rejects_consumed_overlapping_and_broadened_shots(production):
    p = production
    entry_selection, _, _ = stage(p)
    for shot_ids, message in ((['entry'], 'already consumed'), (['interior'], 'overlap'),
                              (['payoff', 'missing'], 'not approved')):
        with pytest.raises(ProductionGovernanceError, match=message):
            carry(p, entry_selection, shot_ids=shot_ids)
    scope = carry(p, entry_selection)
    for field, value in (('attempts_per_shot', {'payoff': 2}), ('requests', {'payoff': '0' * 64}),
                         ('approval_plan_sha256', p.scope['approval_plan_sha256']), ('provider', 'openart_cli')):
        broadened = dict(copy.deepcopy(scope), **{field: value})
        with pytest.raises(ProductionGovernanceError):
            continuity.append_carried_scope(p.root, broadened)
    wrong_root = copy.deepcopy(scope)
    wrong_root['carried_from']['scope_id'] = 'interior-repair'
    with pytest.raises(ProductionGovernanceError):
        continuity.append_carried_scope(p.root, wrong_root)


def test_carry_rejects_scope_record_mutations_after_append(production):
    p = production
    entry_selection, _, repair_inputs = stage(p)
    repaired = p.selector.execute(copy.deepcopy(repair_inputs)); assert repaired.success, repaired.error
    p.select('interior', repaired)
    continuity.append_carried_scope(p.root, carry(p, entry_selection))
    p.bind_upstream('payoff')
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = 'payoff-carried'
    pristine = scopes(p)
    for index, field, value in ((0, 'approved_by', 'someone else'), (1, 'attempts_per_shot', {'interior': 2}),
                                (1, 'replaces_attempt_ids', [entry_selection['attempt_id']])):
        mutated = copy.deepcopy(pristine)
        mutated[index][field] = value
        write_scopes(p, mutated)
        with pytest.raises(ProductionGovernanceError, match='record changed'):
            p.selector.execute(copy.deepcopy(inputs))
    write_scopes(p, pristine)
    assert len(p.transport.native_requests) == 3


def test_carry_rejects_tampered_or_rebound_source_snapshot(production):
    p = production
    entry_selection, _, _ = stage(p)
    directory = p.root / 'production_attempts' / entry_selection['attempt_id']
    snapshot = directory / 'shot_contract.json'
    original = snapshot.read_bytes()
    snapshot.chmod(0o644)
    frozen = json.loads(original)
    frozen['shots'][0]['dominant_action'] += ' Rewritten.'  # rewritten frozen authority
    snapshot.write_text(json.dumps(frozen))
    with pytest.raises(ProductionGovernanceError, match='frozen reservation'):
        carry(p, entry_selection)
    snapshot.write_bytes(original)
    # Rebinding the source scope record (same id/plan) to the carry is refused.
    items = scopes(p)
    items[0]['attempts_per_shot'] = dict(items[0]['attempts_per_shot'], payoff=3)
    write_scopes(p, items)
    with pytest.raises(ProductionGovernanceError, match='different source scope'):
        carry(p, entry_selection)


def _wrong_target(p, rejected_id):
    items = scopes(p)
    items[1]['requests'] = {'payoff': p.scope['requests']['payoff']}
    items[1]['attempts_per_shot'] = {'payoff': 1}
    write_scopes(p, items)


@pytest.mark.parametrize('kind', ['wrong_target', 'other_board', 'identity', 'payoff_board', 'mismatched_keys'])
def test_carry_rejects_wrong_repair_target_or_unrelated_asset_delta(production, kind):
    p = production
    entry_selection, rejected_id, _ = stage(p)
    shot_ids = ['payoff']
    if kind == 'wrong_target':
        _wrong_target(p, rejected_id)
    elif kind == 'mismatched_keys':
        items = scopes(p)
        items[1]['attempts_per_shot'] = {'interior': 1, 'payoff': 1}
        write_scopes(p, items)
    else:
        target = {'other_board': 'payoff-start', 'identity': 'mira', 'payoff_board': 'payoff-board'}[kind]
        rebyte(p, target, 'unapproved')
        commit(p)
        items = scopes(p)
        items[1]['approval_plan_sha256'] = approval_plan_digest(p.contract)
        write_scopes(p, items)
    with pytest.raises(ProductionGovernanceError):
        carry(p, entry_selection, shot_ids=shot_ids)


def repeated_stage(p):
    """Two separately approved board repairs with untouched payoff first pass."""
    first = p.generate('entry'); assert first.success, first.error
    first_id = first.data['production_attempt_id']
    rebyte(p, 'entry-start', 'first-repair')
    p.manifest['assets'] = [dict(item, path=asset(p, item['id'])['path']) if item['id'] == 'entry-start' else item
                            for item in p.manifest['assets']]
    commit(p)

    def repair(sid, source_id, scope_id):
        evidence = p.root / 'artifacts' / (scope_id + '.txt')
        evidence.write_text(SYNTHETIC + '\nExact board repair and retained unused first attempts.')
        inputs = copy.deepcopy(p.inputs[sid])
        inputs['governance']['scope_id'] = scope_id
        inputs['output_path'] = str(p.root / 'assets/video' / (scope_id + '.mp4'))
        for key in ('image_path', 'reference_image_path'):
            if key in inputs:
                inputs[key] = str(p.root / asset(p, sid + '-start')['path'])
        record = {**{k: copy.deepcopy(p.scope[k]) for k in ('project_id', 'story_revision', 'provider', 'approved_by')},
                  'id': scope_id, 'status': 'approved', 'phase': 'repair',
                  'evidence': {'path': str(evidence), 'sha256': file_sha256(evidence)},
                  'approval_plan_sha256': approval_plan_digest(p.contract),
                  'replaces_attempt_ids': [source_id], 'attempts_per_shot': {sid: 1},
                  'requests': {sid: planned_request_digest(inputs, project_dir=p.root)}}
        write_scopes(p, [*scopes(p), record])
        return inputs

    first_repair = repair('entry', first_id, 'entry-repair')
    first_carry = continuity.derive_carried_scope(p.root, source_scope_id=p.scope['id'],
        repair_scope_id='entry-repair', successor_id='remaining-first', shot_ids=['interior', 'payoff'],
        source_contract_path=f'production_attempts/{first_id}/shot_contract.json')
    continuity.append_carried_scope(p.root, first_carry)
    result = p.selector.execute(first_repair); assert result.success, result.error
    p.select('entry', result)
    p.bind_upstream('interior')
    inputs = copy.deepcopy(p.inputs['interior'])
    inputs['governance']['scope_id'] = first_carry['id']
    second = p.selector.execute(inputs); assert second.success, second.error
    second_id = second.data['production_attempt_id']
    rebyte(p, 'interior-start', 'second-repair')
    p.manifest['assets'] = [dict(item, path=asset(p, item['id'])['path']) if item['id'] == 'interior-start' else item
                            for item in p.manifest['assets']]
    commit(p)
    second_repair = repair('interior', second_id, 'second-repair')
    kwargs = dict(source_scope_id=first_carry['id'], repair_scope_id='second-repair',
                  successor_id='remaining-second', shot_ids=['payoff'],
                  source_contract_path=f'production_attempts/{second_id}/shot_contract.json')
    return kwargs, second_repair, first_id, second_id


def test_repeated_carry_keeps_original_allowance_and_dispatches_once(production):
    p = production
    kwargs, repair_inputs, _, _ = repeated_stage(p)
    before = copy.deepcopy(scopes(p))
    result = p.selector.execute(repair_inputs); assert result.success, result.error
    p.select('interior', result)
    successor = continuity.derive_carried_scope(p.root, **kwargs)
    assert successor['requests'] == {'payoff': p.scope['requests']['payoff']}
    assert successor['attempts_per_shot'] == {'payoff': 1}
    continuity.append_carried_scope(p.root, successor)
    assert scopes(p)[:-1] == before
    with pytest.raises(ProductionGovernanceError, match='already carried'):
        continuity.derive_carried_scope(p.root, **dict(kwargs, successor_id='duplicate'))
    p.bind_upstream('payoff')
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = successor['id']
    calls = len(p.transport.native_requests)
    result = p.selector.execute(inputs); assert result.success, result.error
    provenance(p, 'payoff', p.select('payoff', result))
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(inputs)
    assert len(p.transport.native_requests) == calls + 1


@pytest.mark.parametrize('ancestor', ['first-generation', 'remaining-first'])
@pytest.mark.parametrize('state', ['reserved', 'uncertain', 'generated'])
def test_repeated_carry_rejects_consumption_in_every_ancestor(production, ancestor, state):
    p = production
    kwargs, _, _, _ = repeated_stage(p)
    directory = p.root / 'production_attempts' / 'consumed-payoff'
    directory.mkdir()
    save(directory / 'request.json', {'attempt_id': 'consumed-payoff', 'scope_id': ancestor, 'shot_id': 'payoff'})
    save(directory / 'result.json', {'status': state})
    with pytest.raises(ProductionGovernanceError, match='already consumed'):
        continuity.derive_carried_scope(p.root, **kwargs)


@pytest.mark.parametrize('kind', ['ancestor_scope', 'ancestor_repair', 'ancestor_request', 'ancestor_input',
                                  'ancestor_contract', 'ancestor_approval', 'missing', 'cycle', 'stale', 'overlap'])
def test_repeated_carry_replays_ancestors_and_rejects_tampering(production, kind):
    p = production
    kwargs, _, first_id, _ = repeated_stage(p)
    items = scopes(p)
    if kind in {'ancestor_scope', 'ancestor_repair', 'missing', 'cycle', 'stale', 'overlap'}:
        if kind == 'ancestor_scope': items[0]['attempts_per_shot']['payoff'] = 2
        elif kind == 'ancestor_repair': items[1]['requests']['entry'] = '0' * 64
        elif kind == 'missing': items = [item for item in items if item['id'] != 'first-generation']
        elif kind == 'cycle': items[0]['carried_from'] = {'scope_id': 'remaining-first'}
        elif kind == 'stale': items[-1]['approval_plan_sha256'] = p.scope['approval_plan_sha256']
        elif kind == 'overlap': kwargs['shot_ids'] = ['interior']
        write_scopes(p, items)
    else:
        directory = p.root / 'production_attempts' / first_id
        name = 'request.json' if kind == 'ancestor_request' else 'shot_contract.json'
        if kind == 'ancestor_approval':
            binding = read(directory / 'request.json')['approval_evidence']
            path = p.root / binding['path']
            path.chmod(0o644); path.write_bytes(b'tampered approval')
        elif kind == 'ancestor_input':
            binding = read(directory / 'request.json')['input_assets'][0]
            path = p.root / binding['path']
            path.chmod(0o644); path.write_bytes(b'tampered input')
        else:
            path = directory / name
            path.chmod(0o644)
            data = read(path)
            if kind == 'ancestor_request': data['submitted_inputs']['prompt'] += ' unapproved'
            else: data['shots'][0]['dominant_action'] += ' unapproved'
            save(path, data)
    with pytest.raises(ProductionGovernanceError):
        continuity.derive_carried_scope(p.root, **kwargs)


def test_repeated_carry_still_requires_current_selected_upstream_evidence(production):
    p = production
    kwargs, repair_inputs, _, _ = repeated_stage(p)
    result = p.selector.execute(repair_inputs); assert result.success, result.error
    selection = p.select('interior', result)
    successor = continuity.derive_carried_scope(p.root, **kwargs)
    continuity.append_carried_scope(p.root, successor)
    p.bind_upstream('payoff')
    inputs = copy.deepcopy(p.inputs['payoff'])
    inputs['governance']['scope_id'] = successor['id']
    calls = len(p.transport.native_requests)
    Path(selection['outgoing_frame']['path']).write_bytes(b'changed selected bytes')
    with pytest.raises(ProductionGovernanceError):
        p.selector.execute(inputs)
    assert len(p.transport.native_requests) == calls
