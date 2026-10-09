"""Shot-scoped planning continuity for strict motion approvals.

A repair that replaces one shot's reviewed static board changes the global
``approval_plan_digest``. Retained attempts of *other* shots stay eligible only
when their own stable planning projection is unchanged; unused shots from the
original first-pass scope can continue through explicit linked carried scopes
that bind the original scope, each exact repair scope and their approvals.
Nothing here grants new calls, providers, shots or allowances.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

from lib import production_execution as execution
from lib.shot_contract import contract_digest, file_sha256

_BYTE_FIELDS = ('path', 'sha256', 'review')
_SHARED_ROLES = {'identity_reference', 'payoff_board'}


def _fail(message):
    execution._fail('carried scope: ' + message)


def _closure(contract, shot):
    """Own static assets, payoff and identity references relevant to a shot."""
    cast = set(shot.get('cast_ids', [])) | set(contract.get('late_cast_ids', []))
    ids = set(shot.get('asset_ids', []))
    if contract.get('payoff_asset_id'):  # optional for reference-free contracts
        ids.add(contract['payoff_asset_id'])
    ids |= {asset['id'] for asset in contract.get('assets', [])
            if asset.get('role') == 'identity_reference' and set(asset.get('cast_ids', [])) & cast}
    return ids


def _projection(contract, keep_bytes):
    """Stable plan minus reviews/observed upstream; byte fields kept only for ``keep_bytes``."""
    plan = copy.deepcopy(contract)
    plan.pop('project_review', None)
    for shot in plan['shots']:
        shot.pop('review', None)
        shot['upstream'] = [{'shot_id': item['shot_id']} for item in shot.get('upstream', [])]
    for asset in plan.get('assets', []):
        if asset.get('upstream_source') or asset['id'] not in keep_bytes:
            for field in _BYTE_FIELDS:
                asset.pop(field, None)
    return plan


def shot_planning_digest(contract, shot_id):
    """Digest of every global/story/shot-order/semantic field plus this shot's own static closure.

    Other shots' board bytes and reviews, project/shot reviews and observed
    upstream selections are excluded; those are validated separately.
    """
    shots = [shot for shot in contract.get('shots', []) if shot.get('id') == shot_id]
    if len(shots) != 1:
        _fail(f'shot {shot_id} missing or duplicated in contract')
    return execution._digest(_projection(contract, _closure(contract, shots[0])))


def historical_shot_planning_digest(contract, shot_id):
    """Source-only applicability: globals/order, own semantics and static closure.

    Later shots may materialize their own boards/roles without rewriting an
    earlier authentic original. This projection grants no prospective approval,
    selection or certification; their whole-plan digest remains unchanged.
    """
    shots = [shot for shot in contract.get('shots', []) if shot.get('id') == shot_id]
    if len(shots) != 1:
        _fail(f'shot {shot_id} missing or duplicated in contract')
    closure = _closure(contract, shots[0])
    plan = _projection(contract, closure)
    plan['shots'] = [shot if shot['id'] == shot_id else {'id':shot['id']} for shot in plan['shots']]
    plan['assets'] = [asset for asset in plan.get('assets', []) if asset['id'] in closure]
    return execution._digest(plan)


def _scope(scopes, scope_id):
    matches = [item for item in scopes if item.get('id') == scope_id]
    if len(matches) != 1:
        _fail(f'scope {scope_id} must exist exactly once')
    return matches[0]


def _evidence(root, record, label):
    if (not isinstance(record, dict) or not record.get('path') or not record.get('sha256')
            or file_sha256(execution._inside(record['path'], root)) != record['sha256']):
        _fail(label + ' approval evidence missing or changed')


def _check_repair_delta(source_contract, contract, repair, carried_shots):
    """All static asset changes are the repair targets' own boards; nothing else moved."""
    shots = {shot['id']: shot for shot in contract['shots']}
    protected = set()
    for shot_id in carried_shots:
        protected |= _closure(contract, shots[shot_id])
    roles = {asset['id']: asset.get('role') for asset in contract.get('assets', [])}
    payoff = contract.get('payoff_asset_id')
    mutable = set()
    for shot_id in repair['requests']:
        if shot_id not in shots:
            _fail('repair target shot missing from contract')
        mutable |= {aid for aid in shots[shot_id].get('asset_ids', [])
                    if roles.get(aid) not in _SHARED_ROLES and aid != payoff}
    mutable -= protected
    every = ({asset['id'] for asset in contract.get('assets', [])}
             | {asset['id'] for asset in source_contract.get('assets', [])})
    keep = every - mutable
    if _projection(source_contract, keep) != _projection(contract, keep):
        _fail('contract changed outside the exact repair targets\' own static boards')


def _carry_lineage(scopes, scope):
    """Immediate scope through original approval; every link must exist once."""
    chain, seen = [], set()
    while True:
        sid = scope.get('id')
        if not isinstance(sid, str) or sid in seen:
            _fail('carry lineage contains a cycle or missing scope ID')
        seen.add(sid)
        chain.append(scope)
        link = scope.get('carried_from')
        if link is None:
            return chain
        if not isinstance(link, dict) or not isinstance(link.get('scope_id'), str):
            _fail('malformed carried_from')
        scope = _scope(scopes, link['scope_id'])


def _replay_source_request(root, directory, request, source):
    """Replay preserved request/input/approval bytes, never mutable original assets."""
    if (request.get('project_id') != source.get('project_id')
            or request.get('story_revision') != source.get('story_revision')
            or request.get('phase') != 'first_pass' or request.get('media_kind') != 'motion'
            or request.get('cli_session_id') != request.get('attempt_id')):
        _fail('source snapshot project/story/session/phase differs')
    if execution._digest(request.get('scope')) != execution._digest(source):
        _fail('source snapshot froze a different source scope')
    evidence = request.get('approval_evidence', {})
    if evidence.get('sha256') != source.get('evidence', {}).get('sha256'):
        _fail('source snapshot approval differs from source scope')
    _evidence(directory, evidence, 'preserved source')
    bindings = iter(request.get('input_assets', []))
    def restore(role, path):
        record = next(bindings, None)
        if not isinstance(record, dict) or record.get('role') != role:
            _fail('source snapshot input occurrences differ')
        preserved = execution._inside(record.get('path', ''), directory / 'inputs')
        if preserved != path or file_sha256(preserved) != record.get('sha256'):
            _fail('source snapshot input bytes changed')
        return {'path': str(execution._inside(record.get('original_path', ''), root)),
                'sha256': record['sha256']}
    submitted = execution._clean(request.get('submitted_inputs', {}))
    session = submitted.pop('cli_session_id', None)
    if session not in (None, request['attempt_id']):
        _fail('source snapshot session differs')
    restored = execution._paths(submitted, root, restore)
    if next(bindings, None) is not None or execution._digest(restored) != request.get('request_sha256'):
        _fail('source snapshot request digest differs')
    sid = request.get('shot_id')
    index = request.get('scope_attempt_index')
    allowance = source.get('attempts_per_shot', {}).get(sid)
    if type(index) is not int or type(allowance) is not int or index != 0 or not index < allowance:
        _fail('source snapshot allowance differs')
    approved = source.get('requests', {}).get(sid)
    if isinstance(approved, list):
        if index >= len(approved):
            _fail('source snapshot approved request missing')
        approved = approved[index]
    selected = execution._read(directory / 'selected_attempts.json')
    if execution.approved_request_digest(approved, project_dir=root, selected_attempts=selected) != request['request_sha256']:
        _fail('source snapshot request differs from frozen approval')


def validate_carried_scope(project_dir, scope, contract, scopes):
    """Fail closed unless ``scope`` is an exact carry-forward valid against ``contract``.

    Returns the immediate source scope ID. All ancestors retain shared allowances
    and their immutable source contracts and exact repair approvals are replayed.
    """
    root = Path(project_dir).resolve()
    lineage = _carry_lineage(scopes, scope)
    carried = scope.get('carried_from')
    keys = {'scope_id', 'scope_sha256', 'source_contract', 'repair_scope_id', 'repair_scope_sha256'}
    if not isinstance(carried, dict) or set(carried) != keys:
        _fail('malformed carried_from')
    if scope.get('phase') != 'first_pass' or 'derived_from_policy' in scope or scope.get('provider') == 'openart_cli':
        _fail('only manual non-OpenArt first-pass scopes can be carried')
    source = _scope(scopes, carried['scope_id'])
    repair = _scope(scopes, carried['repair_scope_id'])
    if execution._digest(source) != carried['scope_sha256']:
        _fail('source scope record changed')
    if execution._digest(repair) != carried['repair_scope_sha256']:
        _fail('repair scope record changed')
    if 'derived_from_policy' in source or source.get('phase') != 'first_pass':
        _fail('source must be a manual first-pass scope')
    if (source.get('status') != 'approved' or repair.get('status') != 'approved'
            or repair.get('phase') != 'repair' or 'derived_from_policy' in repair):
        _fail('source/repair approval missing')
    for record in (source, repair):
        if not isinstance(record.get('approved_by'), str) or not record['approved_by'].strip():
            _fail('source/repair named approver missing')
    for key in ('project_id', 'story_revision', 'provider'):
        if not (scope.get(key) == source.get(key) == repair.get(key)):
            _fail(f'{key} differs across lineage')
    if scope.get('evidence') != repair.get('evidence') or scope.get('approved_by') != repair.get('approved_by'):
        _fail('carried scope must bind exactly the repair approval')
    _evidence(root, source.get('evidence'), 'source')
    _evidence(root, repair.get('evidence'), 'repair')
    plan = execution.approval_plan_digest(contract)
    if not (scope.get('approval_plan_sha256') == repair.get('approval_plan_sha256') == plan):
        _fail('carried/repair scope is not bound to this contract plan')
    binding = carried['source_contract']
    if not isinstance(binding, dict) or set(binding) != {'path', 'sha256'}:
        _fail('malformed source contract binding')
    path = execution._inside(binding['path'], root)
    attempts_root = (root / 'production_attempts').resolve()
    if path.name != 'shot_contract.json' or path.parent.parent != attempts_root:
        _fail('source contract must be a frozen attempt snapshot')
    if file_sha256(path) != binding['sha256']:
        _fail('source contract bytes changed')
    frozen_request = execution._read(path.parent / 'request.json')
    if frozen_request.get('scope_id') != source['id'] or frozen_request.get('attempt_id') != path.parent.name:
        _fail('source contract snapshot belongs to another scope')
    if (not isinstance(frozen_request.get('scope'), dict)
            or execution._digest(frozen_request['scope']) != carried['scope_sha256']):
        _fail('source snapshot froze a different source scope')
    source_contract = execution._read(path)
    if frozen_request.get('contract_sha256') != contract_digest(source_contract):
        _fail('source contract snapshot differs from its frozen reservation')
    if execution.approval_plan_digest(source_contract) != source.get('approval_plan_sha256'):
        _fail('source contract is not the plan the source scope approved')
    _replay_source_request(root, path.parent, frozen_request, source)
    if 'carried_from' in source:
        validate_carried_scope(root, source, source_contract, scopes)
    shots = set(scope.get('requests', {}))
    if not shots or shots != set(scope.get('attempts_per_shot', {})):
        _fail('carried requests and allowances must name the same shots')
    for shot_id in shots:
        if (scope['requests'][shot_id] != source.get('requests', {}).get(shot_id)
                or scope['attempts_per_shot'][shot_id] != source.get('attempts_per_shot', {}).get(shot_id)):
            _fail('carried request/allowance differs from source approval')
    targets = set(repair.get('requests') or {})
    if not targets or targets != set(repair.get('attempts_per_shot') or {}):
        _fail('repair requests and allowances must name the same shots')
    if shots & targets:
        _fail('carried shots overlap repair targets')
    attempts = execution._attempts(root)
    by_id = {item['attempt_id']: item for item in attempts}
    replaced_ids = repair.get('replaces_attempt_ids')
    if not isinstance(replaced_ids, list) or not replaced_ids:
        _fail('repair replaces no source attempt')
    replaced_shots = set()
    for replaced in replaced_ids:
        item = by_id.get(replaced, {})
        if item.get('scope_id') != source['id'] or item.get('shot_id') not in targets:
            _fail('repair does not replace a same-target attempt of the source scope')
        replaced_directory = execution._inside(Path('production_attempts') / replaced, root)
        replaced_contract = execution._read(replaced_directory / 'shot_contract.json')
        if (contract_digest(replaced_contract) != item.get('contract_sha256')
                or execution.approval_plan_digest(replaced_contract) != source.get('approval_plan_sha256')):
            _fail('repair replacement source contract differs from frozen approval')
        _replay_source_request(root, replaced_directory, item, source)
        replaced_shots.add(item['shot_id'])
    if replaced_shots != targets:
        _fail('every repair target must replace its own source-scope attempt')
    ancestor_ids = {item['id'] for item in lineage[1:]}
    if any(item['scope_id'] in ancestor_ids and item['shot_id'] in shots for item in attempts):
        _fail('carried shot already consumed under the source scope')
    for other in scopes:
        if other.get('id') == scope.get('id') or other.get('id') in ancestor_ids:
            continue
        if not isinstance(other.get('carried_from'), dict):
            continue
        other_lineage = _carry_lineage(scopes, other)
        # A linear successor may overlap its own ancestors, never another branch.
        if scope['id'] in {item['id'] for item in other_lineage[1:]}:
            continue
        if (other_lineage[-1]['id'] == lineage[-1]['id']
                and shots & set(other.get('requests', {}))):
            _fail('source shot already carried by another scope')
    _check_repair_delta(source_contract, contract, repair, shots)
    return source['id']


def derive_carried_scope(project_dir, *, source_scope_id, repair_scope_id, successor_id,
                         shot_ids, source_contract_path):
    """Build (do not write) a carried scope for unused source shots under the current plan."""
    root = Path(project_dir).resolve()
    scopes = execution._read(root / 'production_scopes.json').get('scopes', [])
    source = _scope(scopes, source_scope_id)
    repair = _scope(scopes, repair_scope_id)
    shot_ids = list(shot_ids)
    if not shot_ids or len(set(shot_ids)) != len(shot_ids):
        _fail('shot_ids must be nonempty and unique')
    contract_path = execution._inside(source_contract_path, root)
    scope = {key: copy.deepcopy(source[key]) for key in ('project_id', 'story_revision', 'provider')}
    scope.update({
        'id': successor_id, 'status': 'approved', 'phase': 'first_pass',
        'approved_by': repair['approved_by'], 'evidence': copy.deepcopy(repair['evidence']),
        'requests': {sid: copy.deepcopy(source['requests'][sid]) for sid in shot_ids if sid in source.get('requests', {})},
        'attempts_per_shot': {sid: source['attempts_per_shot'][sid] for sid in shot_ids if sid in source.get('attempts_per_shot', {})},
        'approval_plan_sha256': execution.approval_plan_digest(execution.load_shot_contract(root)),
        'carried_from': {'scope_id': source_scope_id, 'scope_sha256': execution._digest(source),
                         'source_contract': {'path': str(contract_path.relative_to(root)),
                                             'sha256': file_sha256(contract_path)},
                         'repair_scope_id': repair_scope_id, 'repair_scope_sha256': execution._digest(repair)},
    })
    if set(scope['requests']) != set(shot_ids) or set(scope['attempts_per_shot']) != set(shot_ids):
        _fail('shot not approved by the source scope')
    validate_carried_scope(root, scope, execution.load_shot_contract(root), [*scopes, scope])
    return scope


def append_carried_scope(project_dir, scope):
    """Revalidate and atomically append a carried scope under the project lock."""
    root = Path(project_dir).resolve()
    path = root / 'production_scopes.json'
    with execution._lock(root):
        document = execution._read(path)
        scopes = document.get('scopes', [])
        if any(item.get('id') == scope.get('id') for item in scopes):
            _fail('scope ID already exists')
        validate_carried_scope(root, scope, execution.load_shot_contract(root), [*scopes, scope])
        document['scopes'] = [*scopes, copy.deepcopy(scope)]
        temp = path.with_name('.production-scopes-' + str(uuid.uuid4()) + '.tmp')
        with temp.open('x') as stream:
            json.dump(document, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    return copy.deepcopy(scope)


# ---------------------------------------------------------------------------
# Append-only planning revisions (authorized incidental choreography changes)
# ---------------------------------------------------------------------------
#
# A revision records one exact change of a shot's ``dominant_action`` and/or
# ``completed_end_state`` between two immutable in-project contract snapshots.
# Every other approved planning difference fails closed. ``revision_sha256`` is
# ``execution._digest(record without the revision_sha256 key)`` (sha256 of the
# sorted-key compact JSON). Records live in ``production_revisions.json`` and are
# never rewritten. A matching revision only maps the current plan back to the
# prior plan for the attempts and scopes it names; it grants no calls, shots,
# providers or allowances, and a changed shot still needs a fresh selection
# review bound to the revision and current contract.

REVISION_FIELDS = ('dominant_action', 'completed_end_state')
_RECORD_KEYS = {'revision_id', 'project_id', 'story_revision', 'prior_contract', 'revised_contract',
                'changes', 'authority', 'protected_story_proof', 'retained_attempts', 'retained_scopes',
                'revision_sha256'}
_REVISIONS = 'production_revisions.json'


def _rfail(message):
    execution._fail('planning revision: ' + message)


def revision_digest(record):
    """Record hash excluding the ``revision_sha256`` field itself."""
    return execution._digest({key: value for key, value in record.items() if key != 'revision_sha256'})


def _bound(root, record, label, keys=('path', 'sha256')):
    if not isinstance(record, dict) or set(record) != set(keys):
        _rfail(label + ' binding malformed')
    if not isinstance(record['path'], str) or not isinstance(record['sha256'], str):
        _rfail(label + ' binding malformed')
    path = execution._inside(record['path'], root)
    if not path.is_file() or file_sha256(path) != record['sha256']:
        _rfail(label + ' bytes missing or changed')
    return path


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _one_shot(contract, shot_id, label):
    shots = [item for item in contract.get('shots', []) if item.get('id') == shot_id]
    if len(shots) != 1:
        _rfail(f'{label} shot {shot_id} missing or duplicated')
    return shots[0]


def _apply(contract, changes, side):
    result = copy.deepcopy(contract)
    for change in changes:
        _one_shot(result, change['shot_id'], 'contract')[change['field']] = copy.deepcopy(change[side])
    return result


def changed_shots(record):
    return {change['shot_id'] for change in record['changes']}


def _validate_story_proof(root, record, proof):
    """Require a current passing review of the exact revision and existing footage."""
    from jsonschema import Draft202012Validator
    from lib.shot_contract import CRITICAL_PREDICATES

    keys = {'version', 'project_id', 'story_revision', 'prior_contract_sha256',
            'revised_contract_sha256', 'target_shots', 'sources', 'review'}
    if not isinstance(proof, dict) or set(proof) != keys or proof['version'] != '1.0':
        _rfail('protected story proof must use the typed revision proof contract')
    expected = {'project_id': record['project_id'], 'story_revision': record['story_revision'],
                'prior_contract_sha256': record['prior_contract']['sha256'],
                'revised_contract_sha256': record['revised_contract']['sha256']}
    if any(proof[key] != value for key, value in expected.items()):
        _rfail('protected story proof project/story/snapshot binding is stale')
    targets = proof['target_shots']
    changed = changed_shots(record)
    if (not isinstance(targets, list) or any(not _nonempty(sid) for sid in targets)
            or len(set(targets)) != len(targets) or set(targets) != changed):
        _rfail('protected story proof targets differ from changed shots')
    sources = proof['sources']
    if not isinstance(sources, list) or not sources:
        _rfail('protected story proof requires existing changed-shot footage')
    seen, covered = set(), set()
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {'attempt_id', 'shot_id', 'output_sha256'}
                or any(not _nonempty(value) for value in source.values())
                or source['attempt_id'] in seen or source['shot_id'] not in changed
                or source not in record['retained_attempts']):
            _rfail('protected story proof source is not an exact retained changed-shot output')
        seen.add(source['attempt_id'])
        covered.add(source['shot_id'])
        state = execution.load_attempt_result(root, source['attempt_id'])
        outputs = [state.get(name) for name in ('output', 'preserved_output')]
        matching = [output for output in outputs if isinstance(output, dict)
                    and output.get('sha256') == source['output_sha256']]
        try:
            intact = matching and all(file_sha256(execution._inside(output['path'], root))
                                      == source['output_sha256'] for output in matching)
        except (OSError, KeyError, TypeError, ValueError):
            intact = False
        if state.get('status') != 'generated' or not intact:
            _rfail('protected story proof source footage bytes are missing or changed')
    if covered != changed:
        _rfail('protected story proof requires footage for every changed shot')
    # Use the same typed review/predicate contract as shot and selection reviews.
    schema = execution._read(Path(__file__).resolve().parents[1] / 'schemas/artifacts/shot_contract.schema.json')
    review = proof['review']
    errors = list(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/review'}).iter_errors(review))
    if errors:
        _rfail('protected story proof review is incomplete or malformed')
    subject = execution._digest({key: value for key, value in proof.items() if key != 'review'})
    if (review['status'] != 'pass' or review['story_revision'] != record['story_revision']
            or review['subject_sha256'] != subject
            or review['reviewer'] != record['protected_story_proof']['reviewed_by']):
        _rfail('protected story proof review is failed, unknown, or stale')
    predicates = {}
    for predicate in review['predicates']:
        name = predicate['name']
        if name in predicates:
            _rfail('protected story proof review has duplicate predicates')
        predicates[name] = predicate
        critical = name in CRITICAL_PREDICATES or predicate.get('severity', 'critical') == 'critical'
        if name in CRITICAL_PREDICATES and predicate.get('severity') == 'cosmetic':
            _rfail('protected story proof cannot downgrade a required critical predicate')
        if critical and predicate['status'] != 'pass':
            _rfail('protected story proof has a failed or unknown critical predicate')
    protected = predicates.get('story_protected')
    if (protected is None or protected.get('severity', 'critical') != 'critical'
            or protected['status'] != 'pass'):
        _rfail('protected story proof requires a passing critical story_protected predicate')


def validate_planning_revision(project_dir, record):
    """Fail closed unless ``record`` is an exact authorized revision; returns snapshots."""
    root = Path(project_dir).resolve()
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        _rfail('record fields differ from the revision schema')
    if record['revision_sha256'] != revision_digest(record):
        _rfail('revision_sha256 differs from record contents')
    if not _nonempty(record['revision_id']):
        _rfail('revision_id missing')
    marker = execution._read(root / 'project.json')
    if record['project_id'] != marker.get('project_id') or record['story_revision'] != marker.get('story_revision'):
        _rfail('project/story differs from current project')
    prior = execution._read(_bound(root, record['prior_contract'], 'prior contract'))
    revised = execution._read(_bound(root, record['revised_contract'], 'revised contract'))
    for contract in (prior, revised):
        if contract.get('project_id') != record['project_id'] or contract.get('story_revision') != record['story_revision']:
            _rfail('snapshot project/story differs')
    authority = record['authority']
    if not isinstance(authority, dict) or set(authority) != {'consent', 'policy', 'activation', 'approved_by'}:
        _rfail('authority must bind consent, policy, activation and approved_by')
    if not _nonempty(authority['approved_by']):
        _rfail('authority approver missing')
    for name in ('consent', 'policy'):
        _bound(root, authority[name], name)
    activation = execution._read(_bound(root, authority['activation'], 'activation'))
    if activation.get('project_id') != record['project_id'] or activation.get('story_revision') != record['story_revision']:
        _rfail('activation belongs to another project/story')
    proof_record = record['protected_story_proof']
    proof = execution._read(_bound(root, proof_record, 'protected story proof', ('path', 'sha256', 'reviewed_by')))
    if not _nonempty(proof_record['reviewed_by']):
        _rfail('protected story proof reviewer missing')
    changes = record['changes']
    if not isinstance(changes, list) or not changes:
        _rfail('changes missing')
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'shot_id', 'field', 'before', 'after'}:
            _rfail('change entry malformed')
        if change['field'] not in REVISION_FIELDS:
            _rfail(f"field {change['field']} is not a permitted choreography field")
        key = (change['shot_id'], change['field'])
        if key in seen:
            _rfail('duplicated change')
        seen.add(key)
        if not _nonempty(change['before']) or not _nonempty(change['after']) or change['before'] == change['after']:
            _rfail('change must replace one nonempty value with a different one')
        if _one_shot(prior, change['shot_id'], 'prior')[change['field']] != change['before']:
            _rfail('change before differs from prior snapshot')
        if _one_shot(revised, change['shot_id'], 'revised')[change['field']] != change['after']:
            _rfail('change after differs from revised snapshot')
    if execution.approval_plan_digest(_apply(revised, changes, 'before')) != execution.approval_plan_digest(prior):
        _rfail('revised plan differs from prior outside the listed choreography changes')
    retained = record['retained_attempts']
    if not isinstance(retained, list) or not retained:
        _rfail('retained attempts missing')
    ids = set()
    for item in retained:
        if not isinstance(item, dict) or set(item) != {'attempt_id', 'shot_id', 'output_sha256'} or item['attempt_id'] in ids:
            _rfail('retained attempt malformed or duplicated')
        ids.add(item['attempt_id'])
        request = execution._read(execution._inside(Path('production_attempts') / item['attempt_id'] / 'request.json', root))
        state = execution.load_attempt_result(root, item['attempt_id'])
        outputs = {(state.get('output') or {}).get('sha256'), (state.get('preserved_output') or {}).get('sha256')}
        if (request.get('shot_id') != item['shot_id'] or request.get('project_id') != record['project_id']
                or state.get('status') != 'generated' or item['output_sha256'] not in outputs - {None}):
            _rfail(f"retained attempt {item['attempt_id']} differs from its generated output")
    _validate_story_proof(root, record, proof)
    scopes = execution._read(root / 'production_scopes.json').get('scopes', [])
    retained_scopes = record['retained_scopes']
    if not isinstance(retained_scopes, list):
        _rfail('retained scopes malformed')
    scope_ids = set()
    for item in retained_scopes:
        if not isinstance(item, dict) or set(item) != {'scope_id', 'scope_sha256'} or item['scope_id'] in scope_ids:
            _rfail('retained scope malformed or duplicated')
        scope_ids.add(item['scope_id'])
        matches = [scope for scope in scopes if scope.get('id') == item['scope_id']]
        if len(matches) != 1 or execution._digest(matches[0]) != item['scope_sha256']:
            _rfail(f"retained scope {item['scope_id']} missing or changed")
    return {'record': copy.deepcopy(record), 'prior': prior, 'revised': revised}


def _load_revisions(root):
    path = root / _REVISIONS
    if not path.exists():
        return []
    document = execution._read(path)
    revisions = document.get('revisions')
    if document.get('version') != '1.0' or not isinstance(revisions, list):
        _rfail('unsupported revisions file')
    ids = [item.get('revision_id') if isinstance(item, dict) else None for item in revisions]
    if len(set(ids)) != len(ids):
        _rfail('duplicated revision IDs')
    return revisions


def revision_chain(project_dir, contract):
    """Linked revisions replayed back from ``contract``'s plan, newest first.

    Starts with the single revision whose revised stable plan equals the
    current plan, then follows each prior plan to the revision that produced
    it. Matching uses ``approval_plan_digest`` so observed upstream bindings
    and review refreshes between serial shots do not break it. Two revisions
    producing the same plan, or a cycle, fail closed.
    """
    root = Path(project_dir).resolve()
    by_revised = {}
    for record in _load_revisions(root):
        checked = validate_planning_revision(root, record)
        plan = execution.approval_plan_digest(checked['revised'])
        if plan in by_revised:
            _rfail('ambiguous: several revisions match the current plan')
        by_revised[plan] = checked
    chain, seen = [], set()
    plan = execution.approval_plan_digest(contract)
    while plan in by_revised:
        if plan in seen:
            _rfail('revision chain contains a cycle')
        seen.add(plan)
        chain.append(by_revised[plan])
        plan = execution.approval_plan_digest(by_revised[plan]['prior'])
    return chain


def revision_for_current(project_dir, contract):
    """The newest revision producing ``contract``'s plan, or ``None``."""
    chain = revision_chain(project_dir, contract)
    return chain[0] if chain else None


def newest_change(chain, shot_id):
    """The newest revision in ``chain`` that changed ``shot_id``, or ``None``."""
    return next((revision for revision in chain if shot_id in changed_shots(revision['record'])), None)


def undo_revision(contract, revision):
    """Map a revised-plan contract back to the prior plan; fail unless exact."""
    record = revision['record']
    for change in record['changes']:
        if _one_shot(contract, change['shot_id'], 'contract')[change['field']] != change['after']:
            _rfail('contract does not carry the revised values')
    result = _apply(contract, record['changes'], 'before')
    if execution.approval_plan_digest(result) != execution.approval_plan_digest(revision['prior']):
        _rfail('contract differs from the revised plan beyond observed state')
    return result


def require_retained_attempt(revision, attempt_id, shot_id, output_sha256):
    if not any(item == {'attempt_id': attempt_id, 'shot_id': shot_id, 'output_sha256': output_sha256}
               for item in revision['record']['retained_attempts']):
        _rfail(f'attempt {attempt_id} is not retained by revision {revision["record"]["revision_id"]}')


def require_retained_scope(revision, scope, shot_id):
    """A prior-plan scope may continue only when the revision names it (and its lineage)."""
    retained = {item['scope_id']: item['scope_sha256'] for item in revision['record']['retained_scopes']}
    if retained.get(scope.get('id')) != execution._digest(scope):
        _rfail(f"scope {scope.get('id')} is not retained by the revision")
    carried = scope.get('carried_from')
    if isinstance(carried, dict):
        if (retained.get(carried.get('scope_id')) != carried.get('scope_sha256')
                or retained.get(carried.get('repair_scope_id')) != carried.get('repair_scope_sha256')):
            _rfail('carried scope lineage is not retained by the revision')
    if shot_id in changed_shots(revision['record']):
        _rfail(f'changed shot {shot_id} cannot dispatch under a prior-plan scope')


def require_selection_binding(chain, selection, current_contract, shot_id):
    """A changed shot's selection must bind its newest revision and the current contract."""
    revision = newest_change(chain, shot_id)
    if revision is None:
        return
    expected = {'revision_id': revision['record']['revision_id'],
                'revision_sha256': revision['record']['revision_sha256'],
                'contract_sha256': contract_digest(current_contract)}
    if selection.get('planning_revision') != expected:
        _rfail('changed-shot selection must bind planning_revision to the revision and current contract')


def require_revised_upstream(chain, contract, shot_id, selected):
    """Upstream selections of changed shots must be fresh reviews of their newest revision."""
    for binding in _one_shot(contract, shot_id, 'contract').get('upstream', []):
        revision = newest_change(chain, binding.get('shot_id'))
        if revision is None:
            continue
        expected = {'revision_id': revision['record']['revision_id'],
                    'revision_sha256': revision['record']['revision_sha256']}
        selection = selected.get(binding['shot_id'])
        bound = selection.get('planning_revision') if isinstance(selection, dict) else None
        if not isinstance(bound, dict) or {k: bound.get(k) for k in expected} != expected:
            _rfail(f"upstream {binding['shot_id']} selection predates the revision; record a fresh bound review")


def append_planning_revision(project_dir, record):
    """Revalidate and atomically append a revision; IDs and records are immutable."""
    root = Path(project_dir).resolve()
    path = root / _REVISIONS
    with execution._lock(root):
        revisions = _load_revisions(root)
        if any(item.get('revision_id') == record.get('revision_id') for item in revisions):
            _rfail('revision ID already exists')
        checked = validate_planning_revision(root, record)
        plan = execution.approval_plan_digest(checked['revised'])
        prior_plan = execution.approval_plan_digest(checked['prior'])
        linked = False
        for other in revisions:
            other_checked = validate_planning_revision(root, other)
            other_revised = execution.approval_plan_digest(other_checked['revised'])
            if other_revised == plan:
                _rfail('another revision already targets this plan')
            if execution.approval_plan_digest(other_checked['prior']) == plan:
                _rfail('revision would form a cycle with an earlier revision')
            linked = linked or other_revised == prior_plan
        if revisions and not linked:
            _rfail('revision prior plan does not link to an earlier revision')
        current = execution.approval_plan_digest(execution.load_shot_contract(root))
        if current not in {plan, execution.approval_plan_digest(checked['prior'])}:
            _rfail('current contract is neither the prior nor the revised plan')
        temp = path.with_name('.production-revisions-' + str(uuid.uuid4()) + '.tmp')
        with temp.open('x') as stream:
            json.dump({'version': '1.0', 'revisions': [*revisions, copy.deepcopy(record)]}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    return copy.deepcopy(record)
