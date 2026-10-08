"""Validate retained factual provenance before selecting or certifying motion.

This is an evidence-completeness check, not an authenticity or pixel classifier.
Historical approvals bind immutable snapshots; current stable planning semantics
must still match. Legacy diagnostic readers do not call this strict boundary.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from lib.shot_contract import (
    ASSET_PREDICATES, CRITICAL_PREDICATES, PROJECT_PREDICATES, SHOT_PREDICATES,
    UPSTREAM_PREDICATES, contract_digest, file_sha256, review_digest,
    selection_digest, provisional_audio_review,
)
from schemas.artifacts import load_schema


_ALLOW_OPENART_FIXTURE_PROVENANCE = False  # module test seam, never caller-authorized
_ALLOW_OPENART_COMPONENT_PREPARATION = False  # isolated U2 component fixtures only

def _validate_attempt_provenance(
    project_dir: str | Path, attempt_id: str, *, shot_id: str,
    story_revision: str, expected_output: dict[str, str],
) -> dict[str, Any]:
    """Return validated ``request`` and authoritative ``result``, or fail closed.

    Qualified Grok CLI motion and explicit HyperFrames authored local renders
    have provenance adapters. Other providers need real adapters before selection;
    a made-up generic receipt must never certify an unqualified provider.
    """
    # Lazy import permits record_selection to call this module without a cycle.
    from lib import production_execution as execution

    def fail(message):
        raise execution.ProductionGovernanceError('attempt provenance: ' + message)

    def require(condition, message):
        if not condition:
            fail(message)

    def nonempty(value):
        return isinstance(value, str) and bool(value.strip())

    def inside(path, parent):
        require(nonempty(str(path)) and isinstance(path, (str, Path)), 'missing file path')
        resolved = (Path(path) if Path(path).is_absolute() else root / path).resolve()
        require(resolved.is_relative_to(parent), f'path escapes retained evidence: {path}')
        return resolved

    def read(path):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            require(isinstance(value, dict), f'{path.name} must be an object')
            json.dumps(value, allow_nan=False)
            return value
        except (OSError, ValueError, TypeError) as exc:
            fail(f'cannot read complete {path.name}: {exc}')

    def bound_file(record, parent, label):
        require(isinstance(record, dict), f'{label}: missing file binding')
        require(isinstance(record.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', record['sha256']), f'{label}: invalid hash')
        path = inside(record.get('path', ''), parent)
        try:
            require(execution._input_sha256(path) == record['sha256'], f'{label}: bytes differ from recorded hash')
        except OSError as exc:
            fail(f'{label}: missing/unreadable bytes ({exc})')
        return path

    root = Path(project_dir).resolve()
    require(isinstance(attempt_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', attempt_id), 'invalid attempt ID')
    directory = inside(root / 'production_attempts' / attempt_id, root / 'production_attempts')
    marker = read(root / 'project.json')
    require(marker.get('governance', {}).get('mode') == 'strict'
            and marker.get('governance', {}).get('version') == '1.0', 'strict enrollment required')
    require(marker.get('story_revision') == story_revision and nonempty(story_revision), 'current story revision differs')
    request = read(directory / 'request.json')
    required = {'version', 'attempt_id', 'cli_session_id', 'project_id', 'story_revision',
                'shot_id', 'scope_id', 'phase', 'request_sha256', 'contract_sha256',
                'scope', 'submitted_inputs', 'input_assets', 'media_kind',
                'scope_attempt_index', 'approval_evidence'}
    require(required.issubset(request), f'request missing fields: {sorted(required - request.keys())}')
    require(request['version'] == '1.0' and request['media_kind'] in {'motion','local_render'}, 'unsupported reservation version/media kind')
    require(request['attempt_id'] == attempt_id == request['cli_session_id'], 'reservation/session identity differs')
    require(request['project_id'] == marker.get('project_id') and request['story_revision'] == story_revision
            and request['shot_id'] == shot_id, 'reservation project/story/shot differs')
    scope = request['scope']
    require(isinstance(scope, dict), 'frozen approval scope missing')
    require(scope.get('status') == 'approved' and nonempty(scope.get('approved_by')), 'explicit named approval missing')
    require(scope.get('id') == request['scope_id'] and scope.get('project_id') == marker['project_id']
            and scope.get('story_revision') == story_revision and scope.get('phase') == request['phase'], 'scope identity/phase differs')
    local_render = request['media_kind'] == 'local_render'
    require(scope.get('phase') in ({'local_render'} if local_render else {'first_pass', 'repair'}), 'scope does not approve motion kind')
    openart = scope.get('provider') == 'openart_cli' and not local_render
    require(scope.get('provider') in ({'hyperframes'} if local_render else {'grok_cli','openart_cli'}), 'provider has no qualified strict motion provenance adapter')
    if local_render:
        require(request.get('tool_name') == 'hyperframes_compose', 'local render tool identity differs')
    evidence = request['approval_evidence']
    if openart:
        from lib.production_request import load_private_approval
        private_approval = load_private_approval(request)
        scope = private_approval['scope']
        evidence = private_approval['approval_evidence']
        bound_file(evidence, private_approval['private_parent'], 'preserved private approval')
    else:
        bound_file(evidence, directory, 'preserved approval')
    require(isinstance(scope.get('evidence'), dict) and evidence['sha256'] == scope['evidence'].get('sha256'), 'preserved approval hash differs from frozen scope')
    index = request['scope_attempt_index']
    allowance = scope.get('attempts_per_shot', {}).get(shot_id)
    require(type(index) is int and type(allowance) is int and 0 <= index < allowance, 'scope attempt index exceeds exact allowance')
    peers = []
    for path in (root / 'production_attempts').glob('*/request.json'):
        peer = read(path)
        if peer.get('scope_id') == request['scope_id'] and peer.get('shot_id') == shot_id:
            peers.append(peer)
    require(len(peers) <= allowance, 'scope allowance exceeded')
    indices = [peer.get('scope_attempt_index') for peer in peers]
    require(all(type(value) is int for value in indices) and len(set(indices)) == len(indices), 'scope attempt indices missing/duplicated')
    if scope['phase'] == 'first_pass':
        require(index == 0 and len(peers) == 1, 'first-pass allowance cannot authorize a corrective attempt')
    elif scope['phase'] == 'repair':
        replacements = scope.get('replaces_attempt_ids')
        require(isinstance(replacements, list) and bool(replacements), 'repair replacement scope missing')
        own_replacements = []
        for previous_id in replacements:
            require(isinstance(previous_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', previous_id), 'invalid replaced attempt ID')
            previous = read(root / 'production_attempts' / previous_id / 'request.json')
            if previous.get('shot_id') == shot_id:
                require(previous_id != attempt_id and previous.get('project_id') == marker['project_id']
                        and previous.get('story_revision') == story_revision, 'repair replacement identity differs')
                own_replacements.append(previous_id)
        require(bool(own_replacements), 'repair scope lacks an exact replacement for this shot')

    contract = read(directory / 'shot_contract.json')
    selected_snapshot = read(directory / 'selected_attempts.json')
    current_selected = execution.load_selected_attempts(root)
    schema = load_schema('shot_contract')
    malformed = list(Draft202012Validator(schema).iter_errors(contract))
    require(not malformed, 'frozen shot contract is incomplete')
    digest = contract_digest(contract)
    require(request['contract_sha256'] == digest, 'frozen contract digest differs from reservation')
    current = execution.load_shot_contract(root)
    require(contract['project_id'] == marker['project_id'] and contract['story_revision'] == story_revision, 'frozen contract story/project differs')
    policy_derived = 'derived_from_policy' in scope
    if policy_derived:
        from lib.production_autonomy import validate_policy_attempt
        validate_policy_attempt(root, request, scope, contract,
                                frozen_openart=execution.load_openart_frozen(request) if openart else None)
    else:
        # The frozen scope must still equal its own frozen contract. A later
        # approved repair may change another shot's own static board (which
        # changes the global plan digest); this attempt stays eligible only
        # when its shot-scoped planning projection is unchanged.
        from lib import production_continuity as continuity
        planning = contract
        try:
            # An authorized append-only planning revision maps the current plan
            # back to its prior plan. Historical attempts must be named by the
            # revision; fresh attempts dispatched after it under a prior-plan
            # scope must use a scope (and lineage) the revision retains.
            chain = continuity.revision_chain(root, current)
            index = 0
            frozen_plan = execution.approval_plan_digest(contract)
            # Historical phase: undo newer revisions from current until it
            # reaches this attempt's frozen plan; each must retain the attempt.
            while index < len(chain) and execution.approval_plan_digest(current) != frozen_plan:
                if index == 0:
                    state = execution.load_attempt_result(root, attempt_id)
                    output_sha = (state.get('preserved_output') or state.get('output') or {}).get('sha256')
                continuity.require_retained_attempt(chain[index], attempt_id, shot_id, output_sha)
                current = continuity.undo_revision(current, chain[index])
                index += 1
            # Fresh phase: an attempt dispatched under a prior-plan scope after
            # older revisions; undo both frozen and current via retained scopes.
            while index < len(chain) and scope.get('approval_plan_sha256') != execution.approval_plan_digest(planning):
                require(execution.approval_plan_digest(planning)
                        == execution.approval_plan_digest(chain[index]['revised']),
                        'approved planning semantics changed')
                continuity.require_retained_scope(chain[index], scope, shot_id)
                planning = continuity.undo_revision(planning, chain[index])
                current = continuity.undo_revision(current, chain[index])
                index += 1
            require(scope.get('approval_plan_sha256') == execution.approval_plan_digest(planning),
                    'approved planning semantics changed')
            # Fast path: unchanged global plan keeps the original exact rule.
            same_shot_plan = (execution.approval_plan_digest(planning) == execution.approval_plan_digest(current)
                              or continuity.shot_planning_digest(planning, shot_id)
                              == continuity.shot_planning_digest(current, shot_id))
            if 'carried_from' in scope:
                scopes = read(root / 'production_scopes.json').get('scopes', [])
                continuity.validate_carried_scope(root, scope, planning, scopes)
        except (KeyError, TypeError) as exc:
            fail(f'shot planning continuity unreadable: {exc}')
        require(same_shot_plan, 'approved planning semantics changed')
    shots = [shot for shot in contract['shots'] if shot['id'] == shot_id]
    require(len(shots) == 1, 'reserved shot missing or duplicated in contract')
    shot = shots[0]
    project_reference_free = contract.get('reference_mode') == 'reference_free'
    reference_free = project_reference_free or shot.get('reference_mode') == 'reference_free'
    require(not reference_free or openart, 'reference-free contract requires OpenArt text2video')

    def bound_review(review, subject, names, label, *, allow_draft=False, selection=None, upstream_id=None):
        review_schema = {'$defs': schema['$defs'], '$ref': '#/$defs/review'}
        require(not list(Draft202012Validator(review_schema).iter_errors(review)), f'{label}: incomplete semantic evidence')
        from lib.production_draft import accepted_draft_predicate
        accepted = accepted_draft_predicate(selection, root, shot_id=upstream_id) if allow_draft and selection else None
        audio_provisional = allow_draft and provisional_audio_review(review, root)
        provisional = audio_provisional or accepted is not None
        require((review['status'] == 'pass' or provisional) and review['subject_sha256'] == subject
                and review['story_revision'] == story_revision, f'{label}: failed/stale review')
        seen = [predicate['name'] for predicate in review['predicates']]
        require(len(seen) == len(set(seen)) and names.issubset(seen), f'{label}: missing/duplicated required predicates')
        for predicate in review['predicates']:
            critical = predicate['name'] in CRITICAL_PREDICATES or predicate.get('severity', 'critical') == 'critical'
            require(not (predicate['name'] in CRITICAL_PREDICATES and predicate.get('severity') == 'cosmetic'), f'{label}: critical predicate downgraded')
            deferred = (audio_provisional and predicate['name'] == 'speaker_source' and predicate['status'] == 'unknown') or (predicate['name'] == accepted and predicate['status'] == 'fail')
            require(not critical or predicate['status'] == 'pass' or deferred, f'{label}: critical evidence not passing')

    bound_review(contract['project_review'], digest, PROJECT_PREDICATES, 'frozen project review')
    bound_review(shot['review'], digest, SHOT_PREDICATES, 'frozen shot review')
    assets = {asset['id']: asset for asset in contract['assets']}
    require(len(assets) == len(contract['assets']), 'duplicate frozen asset IDs')
    required_assets = set(shot['asset_ids'])
    if not project_reference_free:
        required_assets.add(contract['payoff_asset_id'])
    for asset in assets.values():
        if asset['role'] == 'identity_reference' and set(asset['cast_ids']) & (set(shot['cast_ids']) | set(contract['late_cast_ids'])):
            required_assets.add(asset['id'])
    for asset_id in required_assets:
        require(asset_id in assets, 'required frozen asset missing')
        asset = assets[asset_id]
        bound_review(asset['review'], asset['sha256'], ASSET_PREDICATES, f'frozen asset {asset_id}')
    for binding in shot['upstream']:
        selection = selected_snapshot.get(binding['shot_id'])
        require(isinstance(selection, dict) and selection.get('attempt_id') == binding['attempt_id'], 'frozen upstream attempt differs')
        for role in ('output', 'outgoing_frame'):
            require(isinstance(selection.get(role), dict) and selection[role].get('sha256') == binding[role + '_sha256'], 'frozen upstream hash differs')
        require(review_digest(selection.get('review')) == binding['review_sha256'], 'frozen upstream review differs')
        bound_review(selection['review'], selection_digest(selection), UPSTREAM_PREDICATES, 'frozen upstream review', allow_draft=True, selection=selection, upstream_id=binding['shot_id'])
        current_upstream = current_selected.get(binding['shot_id'])
        require(isinstance(current_upstream, dict) and selection_digest(current_upstream) == selection_digest(selection)
                and review_digest(current_upstream.get('review')) == review_digest(selection['review']),
                'upstream selection changed since this attempt')
        # Original source authority is frozen above. Appended listening evidence
        # belongs to current eligibility; it cannot revoke unchanged generation
        # facts. Current selected original media must still match its hashes.
        for role in ('output', 'outgoing_frame'):
            bound_file(current_upstream[role], root, 'current upstream ' + role)

    submitted = request['submitted_inputs']
    frozen_openart = execution.load_openart_frozen(request) if openart else None
    if openart:
        require(request.get('tool_name') in {'openart_cli_video','video_selector'}, 'OpenArt tool identity differs')
        submitted = frozen_openart['inputs']
        if not reference_free and 'input_assets' in frozen_openart['native']:
            from lib.production_request import validate_openart_asset_roles
            try:
                validate_openart_asset_roles({'binding': {'references': [
                    {'id': a['id'], 'role': a['role'], 'sha256': a['sha256'], 'cast_ids': a['cast_ids']} for a in contract['assets']]},
                    'shot': shot}, frozen_openart['native'])
            except (ValueError, KeyError, TypeError) as exc:
                fail('frozen OpenArt native role bindings differ: ' + str(exc))
        if reference_free:
            require(submitted.get('mode') == frozen_openart['native'].get('mode')
                    == frozen_openart['profile'].get('mode') == 'text2video',
                    'reference-free contract requires OpenArt text2video')
    bindings = request['input_assets']
    require(isinstance(submitted, dict) and isinstance(bindings, list), 'immutable submitted inputs missing')
    if reference_free:
        require(not bindings, 'reference-free submitted input snapshots forbidden')
    else:
        require(bool(bindings), 'immutable submitted inputs missing')
    remaining = iter(bindings)

    def restore_binding(role, path):
        record = next(remaining, None)
        require(isinstance(record, dict) and record.get('role') == role, 'submitted input occurrences differ')
        preserved = bound_file(record, directory / 'inputs', 'input snapshot')
        require(preserved == path.resolve(), 'submitted input path differs from snapshot')
        original = inside(record.get('original_path', ''), root)
        return {'path': str(original), 'sha256': record['sha256']}

    clean_submitted = execution._clean(submitted)
    session = clean_submitted.pop('cli_session_id', None)
    require(session is None or session == request['cli_session_id'], 'submitted session differs')
    try:
        restored = execution._paths(clean_submitted, root, restore_binding)
    except (TypeError, ValueError, OSError) as exc:
        fail(f'cannot reconstruct immutable approved request: {exc}')
    require(next(remaining, None) is None, 'unsubmitted extra input snapshot')
    require(execution._digest(restored) == request['request_sha256'], 'submitted request digest differs')
    approved = scope.get('requests', {}).get(shot_id)
    if isinstance(approved, list):
        require(index < len(approved), 'approved ordered request missing')
        approved = approved[index]
    if isinstance(approved, str):
        approved_digest = approved
    else:
        approved_digest = execution.approved_request_digest(approved, project_dir=root, selected_attempts=selected_snapshot)
    require(approved_digest == request['request_sha256'], 'submitted request differs from exact frozen approval')
    if local_render:
        execution._check_local_render_inputs(contract, shot_id, submitted, root)
    else:
        if policy_derived:
            execution._check_motion_inputs(contract, shot_id, submitted, root, policy_composite=True)
        else:
            execution._check_motion_inputs(contract, shot_id, submitted, root)
    expected_hashes = {assets[asset_id]['sha256'] for asset_id in shot['asset_ids']}
    require(all(item['sha256'] in expected_hashes or (local_render and item['role'] == 'workspace_path') for item in bindings), 'submitted input outside approved shot assets')
    if openart:
        if 'preparation_snapshot' in request['openart']:
            if policy_derived:
                from lib.production_request import validate_frozen_preparation_history
                validate_frozen_preparation_history(request, frozen_openart, root)
            else:
                from lib.production_request import _validate_frozen_preparation_original
                _validate_frozen_preparation_original(request, frozen_openart, root)
        elif not _ALLOW_OPENART_COMPONENT_PREPARATION:
            fail('OpenArt immutable preparation snapshot missing')
        return _validate_openart_result(root, directory, request, frozen_openart, expected_output, bound_file, read, require)
    if local_render:
        return _validate_local_render_result(root, directory, request, expected_output, bound_file, read, require)

    provider_path = directory / 'provider_request.json'
    if 'preferred_provider' in submitted:
        require(provider_path.exists(), 'selector actual provider request missing')
    provider_inputs = read(provider_path) if provider_path.exists() else submitted
    require(provider_inputs.get('cli_session_id') == request['cli_session_id'], 'actual provider request session missing/different')
    expected_args = execution._grok_native_arguments(provider_inputs)
    require(expected_args == execution._grok_native_arguments(submitted), 'provider changed approved native controls')
    native_tool = execution._grok_native_operation(provider_inputs)
    require(expected_args.get('duration') == shot['duration_seconds'], 'provider duration differs from approved shot')
    result = execution.load_attempt_result(root, attempt_id)
    require(isinstance(result, dict) and result.get('status') == 'generated', 'attempt is failed/uncertain/incomplete')
    payload = result.get('result')
    require(isinstance(payload, dict) and payload.get('success') is True, 'complete successful provider result required')
    require({'success', 'data', 'artifacts', 'error', 'cost_usd', 'duration_seconds', 'model'}.issubset(payload), 'provider result fields missing')
    output = result.get('output')
    actual_path = bound_file(output, root, 'selected output')
    require(output == expected_output and actual_path == inside(submitted.get('output_path', ''), root), 'selected output differs from reserved output')
    preserved = result.get('preserved_output')
    bound_file(preserved, directory, 'preserved original output')
    require(preserved['sha256'] == output['sha256'], 'preserved output differs from selected bytes')
    require(isinstance(payload['artifacts'], list) and str(actual_path) in payload['artifacts'], 'result omitted actual output artifact')
    data = payload['data']
    require(isinstance(data, dict) and data.get('provider') == scope['provider']
            and data.get('session_id') == request['cli_session_id'] and data.get('dispatch_status') == 'completed', 'provider result route/session/status differs')
    receipt = data.get('conditioning_receipt')
    require(isinstance(receipt, dict), 'actual conditioning receipt missing')
    require(receipt.get('version') == '1.0' and receipt.get('provider') == scope['provider']
            and receipt.get('native_tool') == native_tool and receipt.get('session_id') == request['cli_session_id']
            and receipt.get('dispatch_status') == 'completed' and receipt.get('submission_evidence') == 'verified_native_call', 'native receipt route/session/submission differs')
    require(nonempty(receipt.get('adapter_version')) and nonempty(receipt.get('cli_version')), 'actual adapter/CLI version missing')
    require(receipt.get('agent_model') and receipt.get('model_role') == 'agent'
            and receipt.get('media_model') is None and receipt.get('media_model_status') == 'unreported', 'CLI/media model provenance misrepresented')
    require(receipt.get('submitted_arguments') == expected_args, 'native receipt changed approved controls')
    expected_assets = []

    def receipt_asset(path, role, **extra):
        resolved = inside(path, directory / 'inputs')
        expected_assets.append({'role': role, 'path': str(resolved), 'sha256': file_sha256(resolved), **extra})

    for key in ('image', 'first_frame', 'last_frame'):
        if key in expected_args:
            receipt_asset(expected_args[key], 'first_frame' if key == 'image' else key)
    for index, path in enumerate(expected_args.get('images', [])):
        receipt_asset(path, 'reference', index=index)
    for index, frame in enumerate(expected_args.get('keyframes', [])):
        receipt_asset(frame['image'], 'keyframe', index=index, timestamp_s=frame['timestamp_s'])
    require(receipt.get('input_assets') == expected_assets, 'native receipt input roles/hashes differ')
    runtime_fields = {'session_id', 'cli_version', 'dispatch_status', 'submission_evidence', 'request_sha256'}
    stable_receipt = {key: value for key, value in receipt.items() if key not in runtime_fields}
    require(receipt.get('request_sha256') == execution._digest(stable_receipt), 'native receipt digest differs')
    raw_path = directory / 'raw_result.json'
    original_path = directory / 'result.json'
    if (directory / 'local_continuation_claim.json').exists():
        claim = execution.validate_local_grok_continuation_record(root, request)
        require(claim['native_request_sha256'] == receipt['request_sha256'],
                'local continuation native request differs from one-time claim')
        raw_path = directory / 'local_continuation_raw_result.json'
        original_path = directory / 'local_continuation_result.json'
    reconciliation = (directory / 'reconciliation.json').exists()
    if provider_path.exists() and not reconciliation:
        native_result = read(directory / 'provider_result.json')
        require(native_result.get('success') is True and native_result.get('data', {}).get('conditioning_receipt') == receipt
                and native_result.get('artifacts') == payload['artifacts'],
                'actual provider result differs from selected outer result')
    if not reconciliation:
        require(raw_path.exists() and read(raw_path) == payload, 'original raw provider result missing/different')
    elif raw_path.exists():
        raw = read(raw_path)
        original_receipt = raw.get('data', {}).get('conditioning_receipt')
        if original_receipt:
            require({key: value for key, value in original_receipt.items() if key not in runtime_fields} == stable_receipt,
                    'reconciliation changed original submitted controls')
    else:
        # A host interruption may precede a provider return. Recovered complete
        # native evidence is bound above to the durable prelaunch reservation.
        if original_path.exists():
            try:
                original = read(original_path)
            except execution.ProductionGovernanceError:
                # A truncated historical result remains untouched for diagnosis.
                # The authoritative reconciliation has already passed every
                # reservation, native-receipt, session and byte-binding check
                # above; corrupt old JSON must not invalidate that recovery.
                original = {'status': 'uncertain', 'result': None}
            require(original.get('status') == 'uncertain', 'reconciliation requires original uncertainty')
            retained_return = original.get('result')
            if retained_return is not None:
                # If the raw-result file write failed, the uncertain record may
                # have retained the full provider return instead. It is equally
                # inspectable historical evidence, never a generated success.
                require(isinstance(retained_return, dict) and {'success', 'data', 'artifacts', 'error', 'cost_usd', 'duration_seconds', 'model'}.issubset(retained_return),
                        'original provider return is incomplete')
                original_receipt = retained_return.get('data', {}).get('conditioning_receipt')
                if original_receipt:
                    require({key: value for key, value in original_receipt.items() if key not in runtime_fields} == stable_receipt,
                            'reconciliation changed retained original controls')
    return {'request': request, 'result': result}


def _validate_openart_result(root, directory, request, frozen, expected_output, bound_file, read, require):
    """Pure local original-job proof; never calls CLI or rechecks live account."""
    from lib import openart_jobs as jobs, production_execution as execution
    attempt_id = request['attempt_id']
    require(frozen['profile'].get('source') == 'real' or _ALLOW_OPENART_FIXTURE_PROVENANCE,
            'OpenArt fixture qualification cannot certify live footage')
    launch = jobs.launch_record(attempt_id)
    binding = request['openart']['binding']
    require(isinstance(launch, dict) and launch.get('binding') == binding,
            'OpenArt original private launch binding differs')
    native = frozen['native']
    require(launch.get('argv') == native['argv'] + jobs.cli.GLOBAL_FLAGS,
            'OpenArt original argv differs')
    require(launch.get('cli_version') == native['cli_version'] and launch.get('tier') == native['tier']
            and launch.get('form_sha256') == native['form_sha256'], 'OpenArt launch qualification differs')
    stable = jobs.verify_collection_receipt(attempt_id, frozen['profile'])
    require(stable.get('attempt_id') == attempt_id
            and stable.get('binding') == binding
            and stable.get('evidence',{}).get('snapshot_sha256') == request['openart']['snapshot']['snapshot_sha256']
            and stable.get('job_id_sha256') and stable.get('job_record_sha256'),
            'OpenArt stable original collection binding differs')
    result = execution.load_attempt_result(root, attempt_id)
    require(result.get('status') == 'generated' and (directory / 'reconciliation.json').is_file(),
            'OpenArt original collection reconciliation missing')
    payload = result.get('result')
    require(isinstance(payload, dict) and payload.get('success') is True,
            'OpenArt collection result incomplete')
    data = payload.get('data', {})
    expected_evidence = stable
    require(data.get('provider') == 'openart_cli' and data.get('attempt_id') == attempt_id
            and data.get('dispatch_status') == 'completed' and data.get('openart_evidence') == expected_evidence,
            'OpenArt result original job/request/account evidence differs')
    require(payload.get('cost_usd') is None and data.get('release_authorized') is False,
            'OpenArt unresolved billing misrepresented')
    output = result.get('output')
    actual = bound_file(output, root, 'OpenArt collected output')
    require(output == expected_output, 'OpenArt selected output differs')
    require(stable.get('output', {}).get('path') == str(actual)
            and stable.get('output', {}).get('sha256') == output['sha256'], 'OpenArt original-job output binding differs')
    preserved = result.get('preserved_output')
    bound_file(preserved, directory, 'OpenArt preserved output')
    require(preserved['sha256'] == output['sha256'] and str(actual) in payload.get('artifacts', []),
            'OpenArt preserved output/artifact differs')
    return {'request':request, 'result':result}


def _validate_local_render_result(root, directory, request, expected_output, bound_file, read, require):
    """Real registered renderer result, never a fabricated native generation receipt."""
    from lib import production_execution as execution
    from tools.video.hyperframes_compose import HyperFramesCompose
    submitted = request['submitted_inputs']
    require(set(submitted).issubset({'operation','workspace_path','output_path','duration','fps','quality',
                                    'profile','strict_check','skip_contrast','snapshots'}), 'unsupported local render controls')
    result = execution.load_attempt_result(root, request['attempt_id'])
    require(result.get('status') == 'generated' and not (directory / 'reconciliation.json').exists(),
            'local render requires original complete execution')
    payload = result.get('result')
    require(isinstance(payload, dict) and payload.get('success') is True and read(directory / 'raw_result.json') == payload,
            'actual renderer return missing/different')
    output = result.get('output')
    path = bound_file(output, root, 'local render output')
    require(output == expected_output and str(path) == submitted['output_path']
            and str(path) in payload.get('artifacts', []), 'local render output differs')
    bound_file(result.get('preserved_output'), directory, 'preserved local render')
    require(result['preserved_output']['sha256'] == output['sha256'], 'preserved local render bytes differ')
    data = payload.get('data', {})
    receipt = data.get('local_render_receipt', {})
    workspace = Path(submitted['workspace_path'])
    manifest = execution.workspace_manifest(workspace)
    require(data.get('operation') == 'render_existing' and data.get('workspace') == str(workspace)
            and data.get('output') == str(path) and data.get('authored_entry_preserved') is True,
            'actual render operation/workspace/output differs')
    require(isinstance(receipt.get('cli_version'),str) and bool(re.fullmatch(r'v?\d+\.\d+\.\d+(?:-[\w.-]+)?(?:\+[\w.-]+)?',receipt['cli_version'])), 'actual HyperFrames CLI version missing')
    command = receipt.get('cli_command')
    require(isinstance(command,list) and all(isinstance(arg,str) for arg in command)
            and len(command) > len(receipt.get('render_argv',[]))
            and command[-len(receipt.get('render_argv',[])):] == receipt.get('render_argv'), 'actual CLI invocation missing/different')
    prefix = command[:-len(receipt['render_argv'])]
    require(len(prefix) == 1 and Path(prefix[0]).is_absolute()
            and Path(prefix[0]).name.lower() in {'hyperframes','hyperframes.cmd'},
            'unsupported local render executable')
    require(receipt == {'version':'1.0','tool':HyperFramesCompose.name,'provider':HyperFramesCompose.provider,
                       'adapter_version':HyperFramesCompose.version,'operation':'render_existing',
                       'source_manifest':manifest,'source_sha256':execution._digest(manifest),
                       'cli_command':command,'cli_version':receipt['cli_version'],
                       'output':output,'render_argv':['render','--output',str(path),'--fps',str(data.get('fps')),
                                                    '--quality',data.get('quality'),'--strict']},
            'local render receipt source/route/command/output differs')
    require(data.get('fps') == HyperFramesCompose._resolve_dimensions(submitted.get('profile'),submitted.get('fps',30))[2]
            and data.get('quality') == submitted.get('quality','standard'), 'render controls differ')
    import subprocess
    from fractions import Fraction
    try:
        probe = subprocess.run(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=duration,avg_frame_rate,nb_frames','-of','json',str(path)],
            capture_output=True,text=True,check=True,timeout=30)
        stream = json.loads(probe.stdout)['streams'][0]
        fps = float(Fraction(stream['avg_frame_rate']))
        duration = float(stream['duration'])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        require(False, 'cannot verify actual local render video timing: ' + str(exc))
    require(abs(fps - data['fps']) < 0.001 and abs(duration - submitted['duration']) <= 1 / fps + 0.0001,
            'actual render duration/fps differs from approved shot')
    require(data.get('steps', {}).get('check', {}).get('exit_code') == 0
            and data.get('steps', {}).get('render', {}).get('exit_code') == 0, 'renderer quality/render gate failed')
    sampled_path = directory / 'outgoing_sampling.json'
    # Rendering can be inspected before sampling; selection separately requires it.
    if sampled_path.exists():
        sampled = read(sampled_path)
        frame = sampled.get('outgoing_frame')
        frame_path = bound_file(frame, directory / 'outgoing_frames', 'actual render outgoing')
        sample_inputs = {'input_path':output['path'],'strategy':'timestamps',
            'timestamps':[submitted['duration'] - 1 / data['fps']], 'format':'png',
            'output_dir':str(directory / 'outgoing_frames')}
        require(sampled.get('tool') == 'frame_sampler' and sampled.get('provider') == 'ffmpeg'
                and sampled.get('input') == output and sampled.get('submitted_inputs') == sample_inputs,
                'actual outgoing sampling request differs')
        sample_result = sampled.get('tool_result', {})
        frames = sample_result.get('data', {}).get('frames', [])
        require(sample_result.get('success') is True and len(frames) == 1
                and frames[0].get('path') == str(frame_path)
                and frames[0].get('timestamp_seconds') == sample_inputs['timestamps'][0],
                'actual outgoing sampler result differs')
        return {'request':request,'result':result,'local_render_outgoing':frame}
    return {'request':request,'result':result}


def validate_attempt_provenance(
    project_dir: str | Path, attempt_id: str, *, shot_id: str,
    story_revision: str, expected_output: dict[str, str],
) -> dict[str, Any]:
    """Require complete retained evidence; malformed data always fails closed."""
    from lib.production_execution import ProductionGovernanceError
    try:
        digest = expected_output.get('sha256') if isinstance(expected_output, dict) else None
        if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
            record = Path(project_dir).resolve() / 'production_derived_edits' / digest / 'record.json'
            if record.exists():
                return validate_derived_edit(project_dir, record, attempt_id=attempt_id,
                    shot_id=shot_id, story_revision=story_revision, expected_output=expected_output)
        if (Path(project_dir).resolve() / 'openart_mcp' / 'attempts' / attempt_id).is_dir():
            return _validate_mcp_attempt(project_dir, attempt_id, shot_id=shot_id,
                story_revision=story_revision, expected_output=expected_output)
        return _validate_attempt_provenance(
            project_dir, attempt_id, shot_id=shot_id,
            story_revision=story_revision, expected_output=expected_output,
        )
    except ProductionGovernanceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, ValidationError) as exc:
        raise ProductionGovernanceError(f'attempt provenance: incomplete/invalid evidence ({exc})') from exc


def _aac_sha256(path):
    """Recompute elementary AAC bytes, rather than trusting a receipt claim."""
    import hashlib
    import subprocess
    try:
        result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path),
            '-map', '0:a:0', '-c:a', 'copy', '-f', 'adts', 'pipe:1'],
            capture_output=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('cannot verify copied AAC stream') from exc
    if not result.stdout:
        raise ValueError('copied AAC stream is empty')
    return hashlib.sha256(result.stdout).hexdigest()


def trimmed_outgoing_timestamp(path):
    """Actual final decoded video PTS in FrameSampler's input-seek timeline.

    Producers use this for timestamp sampling of CFR and VFR trims. The format
    start time is subtracted because FFmpeg input ``-ss`` is relative to it.
    """
    import math
    import subprocess
    from fractions import Fraction
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
            '-show_entries', 'stream=time_base:format=start_time:frame=best_effort_timestamp',
            '-of', 'json', str(path)], capture_output=True, text=True, timeout=30, check=True)
        data = json.loads(probe.stdout)
        time_base = Fraction(data['streams'][0]['time_base'])
        origin = Fraction(data.get('format', {}).get('start_time', '0'))
        final = max(int(frame['best_effort_timestamp']) * time_base for frame in data['frames'])
        timestamp = float(final - origin)
        if time_base <= 0 or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError('invalid final presentation timestamp')
        return timestamp
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        raise ValueError('cannot probe actual final retained presentation timestamp') from exc


def _validate_trimmed_cut(value, recipe, receipt, native, bound, require):
    """Bind and replay the exact canonical local cut on every validation.

    AAC hashes here describe the retained interval, never unchanged whole-clip
    audio. Semantic/audio completeness remains the selection review's job.
    """
    import math
    import subprocess
    import tempfile
    from fractions import Fraction
    from tools.video.video_trimmer import VideoTrimmer

    require(set(recipe) == {'version', 'operation', 'input', 'output_path', 'submitted_inputs', 'reason'}
            and recipe['version'] == '1.0', 'complete typed canonical trim recipe required')
    submitted = recipe['submitted_inputs']
    require(isinstance(submitted, dict) and set(submitted) == {
        'operation', 'input_path', 'output_path', 'start_seconds', 'end_seconds', 'codec', 'reason'},
        'exact canonical cut inputs required')
    start, end, codec = submitted['start_seconds'], submitted['end_seconds'], submitted['codec']
    require(type(start) in (int, float) and type(end) in (int, float)
            and math.isfinite(start) and math.isfinite(end)
            and 0 <= start < end <= native['result']['result']['data']['duration_seconds'],
            'bounded retained cut interval required')
    require(submitted['operation'] == 'cut' and submitted['input_path'] == value['parent_output']['path']
            and submitted['output_path'] == value['output']['path']
            and isinstance(codec, str) and bool(codec)
            and isinstance(recipe['reason'], str) and bool(recipe['reason'].strip())
            and submitted['reason'] == recipe['reason'], 'canonical cut input/output/reason differs')
    require(receipt.get('tool') == VideoTrimmer.name and receipt.get('provider') == VideoTrimmer.provider
            and receipt.get('canonical_registry_used') is True and receipt.get('success') is True
            and receipt.get('generation') is False and receipt.get('input') == value['parent_output']
            and receipt.get('recipe') == value['recipe'] and receipt.get('output') == value['output'],
            'successful canonical existing-footage cut receipt required')
    result = receipt.get('tool_result', {})
    data = result.get('data', {})
    actual = data.get('cut_receipt', {})
    require(result.get('success') is True and value['output']['path'] in result.get('artifacts', [])
            and data.get('operation') == 'cut' and data.get('input') == submitted['input_path']
            and data.get('output') == submitted['output_path'] and data.get('start_seconds') == start
            and data.get('end_seconds') == end, 'actual canonical cut return differs')
    command = actual.get('command_argv')
    require(isinstance(command, list) and all(isinstance(arg, str) for arg in command)
            and bool(command) and Path(command[0]).name.lower() in {'ffmpeg', 'ffmpeg.exe'},
            'actual canonical FFmpeg invocation required')
    expected = [command[0], '-y', '-i', submitted['input_path'], '-ss', str(start), '-to', str(end)]
    expected += ['-c', 'copy'] if codec == 'copy' else ['-c:v', codec, '-c:a', 'aac']
    expected.append(submitted['output_path'])
    adapter_version = actual.get('adapter_version')
    require(isinstance(adapter_version, str) and bool(re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', adapter_version)),
            'retained cut adapter version required')
    require(actual == {'version': '1.0', 'tool': VideoTrimmer.name, 'provider': VideoTrimmer.provider,
        'adapter_version': adapter_version, 'operation': 'cut', 'input': value['parent_output'],
        'output': value['output'], 'submitted_inputs': submitted, 'command_argv': expected,
        'command_exit_code': 0}, 'actual canonical command/return/bindings differ')
    parent, output = bound(value['parent_output'], 'native trim input'), bound(value['output'], 'trim output')

    def media(path):
        probe = subprocess.run(['ffprobe', '-v', 'error',
            '-show_entries', 'stream=codec_type,duration,avg_frame_rate', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30, check=True)
        streams = json.loads(probe.stdout)['streams']
        stream = next(stream for stream in streams if stream.get('codec_type') == 'video')
        duration, fps = float(stream['duration']), float(Fraction(stream['avg_frame_rate']))
        require(math.isfinite(duration) and math.isfinite(fps) and duration > 0 and fps > 0,
                'actual trim video timing invalid')
        return {'duration_seconds': duration, 'fps': fps}, any(stream.get('codec_type') == 'audio' for stream in streams)

    try:
        (source_timing, source_audio), (output_timing, output_audio) = media(parent), media(output)
        require(end <= source_timing['duration_seconds'] + 0.0001
                and abs(output_timing['duration_seconds'] - (end - start)) <= 1 / output_timing['fps'] + 0.0001,
                'actual retained duration differs from cut interval (copy cuts may need re-encoding)')
        audio = receipt.get('audio', {})
        require(source_audio == output_audio, 'native source audio was lost or added by cut')
        if source_audio:
            mode = 'retained_interval_stream_copy' if codec == 'copy' else 'retained_interval_aac_reencode'
            require(audio == {'mode': mode, 'input_sha256': _aac_sha256(parent),
                'output_sha256': _aac_sha256(output), 'retained_interval_seconds': [start, end]},
                'actual retained interval AAC evidence differs')
        else:
            require(audio == {'mode': 'no_audio', 'input_has_audio': False, 'output_has_audio': False,
                'retained_interval_seconds': [start, end]}, 'actual no-audio interval evidence differs')
        # On-disk records and in-memory imports have the same caller-writable
        # evidence. Neither proves that registration ran, so both must replay.
        with tempfile.TemporaryDirectory(prefix='openmontage-trim-proof-') as temporary:
            replayed = Path(temporary) / output.name
            # Receipt facts may name a resolved executable; never execute a
            # caller-supplied binary path during verification.
            subprocess.run(['ffmpeg', *expected[1:-1], str(replayed)],
                           capture_output=True, timeout=120, check=True)
            require(file_sha256(replayed) == value['output']['sha256'],
                    'output is not the canonical retained interval cut')
        output_timing['final_frame_timestamp_seconds'] = trimmed_outgoing_timestamp(output)
        return output_timing
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError, StopIteration) as exc:
        require(False, 'cannot verify actual canonical trim media: ' + str(exc))


def _validate_trim_sampling(value, sampled, timing, outgoing, require):
    """Require the actual final retained frame, not the native endpoint."""
    import subprocess
    import tempfile
    timestamp = timing['final_frame_timestamp_seconds']
    inputs = {'input_path': value['output']['path'], 'strategy': 'timestamps', 'timestamps': [timestamp],
        'format': 'png', 'output_dir': str(outgoing.parent)}
    result = sampled.get('tool_result', {})
    frames = result.get('data', {}).get('frames', [])
    require(sampled.get('provider') == 'ffmpeg' and sampled.get('submitted_inputs') == inputs
            and result.get('success') is True and result.get('data', {}).get('strategy') == 'timestamps'
            and result.get('data', {}).get('frame_count') == 1 and frames == [{
                'path': str(outgoing), 'timestamp_seconds': timestamp, 'index': 0}],
            'actual final retained-frame sampling differs')
    try:
        with tempfile.TemporaryDirectory(prefix='openmontage-trim-outgoing-') as temporary:
            replayed = Path(temporary) / 'outgoing.png'
            subprocess.run(['ffmpeg', '-y', '-ss', str(timestamp), '-i', value['output']['path'],
                '-frames:v', '1', str(replayed)], capture_output=True, timeout=60, check=True)
            require(file_sha256(replayed) == value['outgoing_frame']['sha256'],
                    'outgoing bytes are not the actual final retained frame')
    except (OSError, subprocess.SubprocessError) as exc:
        require(False, 'cannot verify retained outgoing frame: ' + str(exc))


def validate_derived_edit(project_dir, record_path, *, attempt_id, shot_id,
                          story_revision, expected_output, record=None):
    """Validate an exact approved local edit after complete native-parent proof.

    No predicate exception is introduced. Native journals remain authoritative
    and untouched; the derived clip and its actual outgoing need a fresh review.
    """
    from lib import production_execution as execution
    root = Path(project_dir).resolve()

    def require(condition, message):
        if not condition:
            raise execution.ProductionGovernanceError('derived edit: ' + message)

    def bound(value, label):
        require(isinstance(value, dict) and set(value) == {'path', 'sha256'}, label + ': complete binding required')
        require(isinstance(value['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', value['sha256']), label + ': invalid hash')
        path = execution._inside(value['path'], root)
        require(path.is_file() and file_sha256(path) == value['sha256'], label + ': missing/changed bytes')
        return path

    def read(binding, label):
        value = json.loads(bound(binding, label).read_text())
        require(isinstance(value, dict), label + ': object required')
        json.dumps(value, allow_nan=False)
        return value

    try:
        path = execution._inside(record_path, root)
        value = record if record is not None else json.loads(path.read_text())
        keys = {'version', 'project_id', 'story_revision', 'shot_id', 'parent_attempt_id',
                'parent_output', 'recipe', 'execution_receipt', 'output', 'preserved_output',
                'outgoing_frame', 'outgoing_receipt', 'approval'}
        require(isinstance(value, dict) and set(value) == keys, 'complete closed record required')
        require(value['version'] == '1.0' and value['parent_attempt_id'] == attempt_id
                and value['shot_id'] == shot_id and value['story_revision'] == story_revision,
                'parent/shot/story identity differs')
        require(value['output'] == expected_output, 'selected output differs from derived record')
        directory = root / 'production_derived_edits' / value['output']['sha256']
        require(path == directory / 'record.json', 'record must be hash-addressed outside native attempts')
        native = _validate_attempt_provenance(root, attempt_id, shot_id=shot_id,
            story_revision=story_revision, expected_output=value['parent_output'])
        require(value['project_id'] == native['request']['project_id'], 'project differs from native parent')
        output = bound(value['output'], 'derived output')
        preserved = bound(value['preserved_output'], 'preserved derived output')
        require(output != bound(value['parent_output'], 'native parent output')
                and preserved.is_relative_to(directory) and preserved != output
                and value['preserved_output']['sha256'] == value['output']['sha256'],
                'separate preserved derived bytes required')
        outgoing = bound(value['outgoing_frame'], 'actual derived outgoing frame')
        recipe = read(value['recipe'], 'exact local recipe')
        require(recipe.get('operation') in {'local_existing_footage_edit', 'canonical_video_trimmer_cut'}
                and recipe.get('input') == value['parent_output']
                and execution._inside(recipe.get('output_path', ''), root) == output,
                'recipe parent/output differs')
        receipt = read(value['execution_receipt'], 'actual edit receipt')
        derived_timing = None
        if recipe['operation'] == 'canonical_video_trimmer_cut':
            derived_timing = _validate_trimmed_cut(value, recipe, receipt, native, bound, require)
        else:
            window = recipe.get('time_window_seconds')
            require(isinstance(window, list) and len(window) == 2
                    and all(type(t) in (int, float) for t in window)
                    and 0 <= window[0] < window[1] <= native['result']['result']['data']['duration_seconds'],
                    'bounded edit window required')
            require(all(isinstance(recipe.get(k), dict) and bool(recipe[k]) for k in ('mask', 'displacement'))
                    and isinstance(recipe.get('command_argv'), list) and bool(recipe['command_argv'])
                    and all(isinstance(arg, str) for arg in recipe['command_argv'])
                    and isinstance(recipe.get('ffmpeg_version'), str) and bool(recipe['ffmpeg_version'])
                    and recipe.get('audio_mode') == 'stream_copy', 'complete deterministic local recipe required')
            mask = recipe['mask']
            bound({'path': mask.get('path'), 'sha256': mask.get('sha256')}, 'retained displacement mask')
            argv = recipe['command_argv']
            inputs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == '-i']
            implementation = bound({'path': str(bound(value['recipe'], 'recipe').parent / recipe.get('implementation', '')),
                                    'sha256': recipe.get('implementation_sha256')}, 'retained local pixel edit implementation')
            require(implementation.suffix == '.py', 'local pipe edit needs retained implementation')
            require(Path(argv[0]).name == 'ffmpeg' and argv[-1] == str(output)
                    and inputs == ['pipe:0', value['parent_output']['path']]
                    and '-c:a' in argv and argv[argv.index('-c:a') + 1] == 'copy',
                    'recipe must edit only native input and retained mask with copied audio')
            require(receipt.get('tool') == 'local_numpy_masked_edit' and receipt.get('provider') == 'local'
                    and receipt.get('encoder') == 'ffmpeg' and receipt.get('receipt_author') == 'build_edit.py'
                    and receipt.get('canonical_registry_used') is False
                    and receipt.get('success') is True and receipt.get('generation') is False
                    and receipt.get('input') == value['parent_output']
                    and receipt.get('recipe') == value['recipe'] and receipt.get('output') == value['output'],
                    'successful explicit local existing-footage edit receipt required')
            tool_result = receipt.get('tool_result', {})
            require(tool_result.get('success') is True and tool_result.get('command_exit_code') == 0
                    and value['output'] in tool_result.get('artifacts', []),
                    'actual local execution result omits bound derived output')
            audio = receipt.get('audio', {})
            require(audio.get('mode') == 'stream_copy' and isinstance(audio.get('input_sha256'), str)
                    and re.fullmatch(r'[0-9a-f]{64}', audio['input_sha256'])
                    and audio['input_sha256'] == audio.get('output_sha256'), 'stream-copy audio evidence missing/different')
            require(_aac_sha256(bound(value['parent_output'], 'native audio input')) == audio['input_sha256']
                    and _aac_sha256(output) == audio['output_sha256'], 'actual elementary AAC bytes differ')
        sampled = read(value['outgoing_receipt'], 'actual outgoing sampling receipt')
        require(sampled.get('tool') == 'frame_sampler' and sampled.get('input') == value['output']
                and sampled.get('outgoing_frame') == value['outgoing_frame'],
                'outgoing frame is not bound to derived clip sampling')
        sample_result = sampled.get('tool_result', {})
        require(sample_result.get('success') is True and any(
            execution._inside(f.get('path', ''), root) == outgoing
            for f in sample_result.get('data', {}).get('frames', [])), 'actual sampler result omits outgoing frame')
        if derived_timing:
            _validate_trim_sampling(value, sampled, derived_timing, outgoing, require)
        approval = value['approval']
        require(isinstance(approval, dict) and set(approval) == {'approved_by', 'evidence'}
                and isinstance(approval['approved_by'], str) and bool(approval['approved_by']), 'named edit approval required')
        accepted = read(approval['evidence'], 'exact root edit approval')
        bindings = {k: v for k, v in value.items() if k != 'approval'}
        require(accepted.get('status') == 'approved' and accepted.get('approved_by') == approval['approved_by']
                and accepted.get('derived_edit_sha256') == execution._digest(bindings), 'approval is stale/not exact')
        bound(accepted.get('authorization'), 'retained user edit authorization')
        return {**native, 'selected_output': value['output'], 'derived_edit': value,
                'selected_outgoing_frame': value['outgoing_frame'],
                **({'derived_timing': derived_timing} if derived_timing else {})}
    except execution.ProductionGovernanceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise execution.ProductionGovernanceError('derived edit: malformed retained evidence: ' + str(exc)) from exc


def _validate_mcp_attempt(project_dir, attempt_id, *, shot_id, story_revision, expected_output):
    """Agent-recorded original connector provenance, never a Python remote call."""
    from lib import production_execution as execution, production_request as preparation
    from lib import openart_mcp as connector, openart_mcp_jobs as jobs
    def require(condition, message):
        if not condition:
            execution._fail('MCP attempt provenance: ' + message)
    root = Path(project_dir).resolve()
    marker = execution._read(root / 'project.json')
    require(marker.get('governance', {}).get('mode') == 'strict'
            and marker.get('governance', {}).get('version') == '1.0'
            and marker.get('story_revision') == story_revision, 'current Strict/story binding differs')
    retained = jobs.provenance_record(root, attempt_id)
    require(retained.get('provenance') == 'agent_recorded_connector' and retained.get('attempt_id') == attempt_id,
            'original connector evidence missing')
    native, authority, inputs = retained['native'], retained['authority'], retained['generation_inputs']
    connector.validate_native_request(native)
    require(native['source'] == 'real' or _ALLOW_OPENART_FIXTURE_PROVENANCE, 'fixture-only connector evidence cannot certify live production')
    require(authority['provider'] == native['provider'] == 'openart_mcp'
            and authority['shot_id'] == shot_id, 'exact provider/shot differs')
    require(native['body_sha256'] == authority['native_body_sha256']
            and native['source_binding_sha256'] == authority['source_binding_sha256']
            and native['account_binding']['uid_sha256'] == authority['account_uid_sha256'],
            'native/account/reference binding differs')
    require(authority['request_sha256'] == execution.planned_request_digest(inputs, project_dir=root),
            'original governed request/source bytes differ')
    scopes = execution._read(root / 'production_scopes.json')['scopes']
    current_scopes = [scope for scope in scopes if scope.get('id') == authority['scope_id']]
    require(len(current_scopes) == 1 and preparation.digest(current_scopes[0]) == authority['scope_sha256'],
            'scope revoked or changed after original approval')
    scope = current_scopes[0]
    require(scope.get('status') == 'approved' and scope.get('provider') == 'openart_mcp'
            and scope.get('project_id') == marker['project_id']
            and scope.get('story_revision') == story_revision, 'current scope identity/approval differs')
    evidence = execution._inside(scope['evidence']['path'], root)
    require(file_sha256(evidence) == scope['evidence']['sha256'], 'approval evidence bytes differ')
    source = preparation._historical_source_packet(root, shot_id, provider='openart_mcp', native=native)
    profile = connector.load_profile(native['model'], native['mode'], require='candidate' if retained['purpose'] == 'qualification' else 'qualified')
    if 'derived_from_policy' in scope:
        from lib.production_autonomy import validate_policy_mcp_attempt
        validate_policy_mcp_attempt(root, scope, inputs=inputs, native=native, profile=profile, authority=authority['billing'])
    # Historical preparation is replayed against its original immutable source
    # packet. Later upstream observations may change the global contract hash;
    # current relevant planning, references and selected subjects stay exact.
    import base64
    import copy
    import hashlib
    from lib.production_continuity import shot_planning_digest
    snapshot_path = jobs._path(root, attempt_id).parent / 'evidence.json'
    snapshot = jobs._read(snapshot_path)
    require(preparation.digest(snapshot) == retained['evidence_snapshot_sha256'], 'frozen preparation snapshot changed')
    files = snapshot['files']
    for name, binding in files.items():
        raw = base64.b64decode(binding['bytes_base64'], validate=True)
        require(hashlib.sha256(raw).hexdigest() == binding['sha256'], 'frozen source bytes changed')
    contract_name = str(execution._artifact_path(root, 'shot_contract.json').relative_to(root))
    frozen_contract = json.loads(base64.b64decode(files[contract_name]['bytes_base64'], validate=True))
    current_contract = execution.load_shot_contract(root)
    packet = snapshot['source_packet']
    frozen_shots = [item for item in frozen_contract['shots'] if item['id'] == shot_id]
    require(len(frozen_shots) == 1 and frozen_shots[0] == packet['shot'], 'frozen source shot differs')
    require(contract_digest(frozen_contract) == packet['binding']['contract_sha256']
            and preparation.digest([frozen_contract['project_review'], frozen_shots[0].get('review')])
                == packet['binding']['reviews_sha256'], 'frozen contract/reviews differ')
    require(shot_planning_digest(frozen_contract, shot_id) == shot_planning_digest(current_contract, shot_id),
            'current MCP planning semantics changed')
    require(preparation.digest(snapshot['scope']) == authority['scope_sha256'], 'frozen scope differs')
    stable = set(packet['binding']) - {'contract_sha256', 'reviews_sha256'}
    require(set(source['binding']) == set(packet['binding'])
            and all(source['binding'][key] == packet['binding'][key] for key in stable),
            'canonical reviewed source/reference/upstream changed')
    reconstructed = copy.deepcopy(source)
    reconstructed['binding'] = copy.deepcopy(packet['binding'])
    reconstructed['shot']['review'] = frozen_shots[0].get('review')
    require(preparation.digest(reconstructed) == preparation.digest(packet)
            and preparation.digest(packet['binding']) == authority['compiled_source_binding_sha256'],
            'frozen original packet derivation differs')
    compiled, review = snapshot['compiled'], snapshot['review']
    require(preparation._read(root, 'compiled_request-' + inputs['compiled_request_id'] + '.json') == compiled
            and preparation._read(root, 'preparation_review-' + inputs['preparation_review_id'] + '.json') == review,
            'original preparation sidecars changed')
    proof = preparation.validate_preparation_evidence(compiled, review, inputs, native, profile, reconstructed)
    if retained['purpose'] == 'qualification':
        from lib.provider_qualification import validate_qualification_stage
        validate_qualification_stage(root, inputs, authority['request_sha256'], native=native, profile=profile)
    require(preparation.digest(proof) == preparation.digest(authority['preparation']), 'compiled preparation evidence differs')
    from lib.openart_mcp_dispatch import validate_billing_authority
    billing = validate_billing_authority(inputs, {'root': root, 'scope': scope, 'marker': marker,
        'shot_id': shot_id, 'request_sha256': authority['request_sha256'],
        'scope_attempt_index': authority['scope_attempt_index']}, native,
        historical_authority=authority['billing'], profile=profile)
    require(preparation.digest(billing) == preparation.digest(authority['billing']), 'fresh MCP billing approval differs')
    require(preparation.digest(retained['begin_envelope']) == preparation.digest(
        {'tool': 'mcp__codex_apps__openart_openart_generate_video', 'arguments': native['body']}),
        'original generation envelope differs')
    receipt = retained['receipt']; statuses = retained['status_observations']
    history_id = receipt.get('historyId')
    require(isinstance(history_id, str) and bool(history_id) and statuses, 'original history ID/status evidence absent')
    terminal = statuses[-1]
    require(terminal.get('historyId') == history_id and terminal.get('status') == 'COMPLETED',
            'completed status is not bound to original history ID')
    output = retained['output']
    require(output == expected_output and execution._inside(output['path'], root) == execution._inside(inputs['output_path'], root)
            and file_sha256(execution._inside(output['path'], root)) == output['sha256'], 'collected original output bytes differ')
    request = {'attempt_id': attempt_id, 'project_id': marker['project_id'], 'story_revision': story_revision,
               'shot_id': shot_id, 'scope_id': authority['scope_id'], 'scope': scope, 'media_kind': 'motion',
               'submitted_inputs': inputs, 'request_sha256': authority['request_sha256'],
               'transport': 'agent_mediated_connector', 'tool_name': 'openart_mcp_video'}
    return {'request': request, 'result': {'status': 'generated', 'output': output},
            'connector_provenance': retained}
