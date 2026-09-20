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

from lib.shot_contract import (
    ASSET_PREDICATES, CRITICAL_PREDICATES, PROJECT_PREDICATES, SHOT_PREDICATES,
    UPSTREAM_PREDICATES, contract_digest, file_sha256, review_digest,
    selection_digest,
)
from schemas.artifacts import load_schema


def _validate_attempt_provenance(
    project_dir: str | Path, attempt_id: str, *, shot_id: str,
    story_revision: str, expected_output: dict[str, str],
) -> dict[str, Any]:
    """Return validated ``request`` and authoritative ``result``, or fail closed.

    Only qualified Grok CLI motion receipts are currently supported. Other
    providers need their own real provenance adapters before strict selection;
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
            require(file_sha256(path) == record['sha256'], f'{label}: bytes differ from recorded hash')
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
    require(request['version'] == '1.0' and request['media_kind'] == 'motion', 'unsupported reservation version/media kind')
    require(request['attempt_id'] == attempt_id == request['cli_session_id'], 'reservation/session identity differs')
    require(request['project_id'] == marker.get('project_id') and request['story_revision'] == story_revision
            and request['shot_id'] == shot_id, 'reservation project/story/shot differs')
    scope = request['scope']
    require(isinstance(scope, dict), 'frozen approval scope missing')
    require(scope.get('status') == 'approved' and nonempty(scope.get('approved_by')), 'explicit named approval missing')
    require(scope.get('id') == request['scope_id'] and scope.get('project_id') == marker['project_id']
            and scope.get('story_revision') == story_revision and scope.get('phase') == request['phase'], 'scope identity/phase differs')
    require(scope.get('phase') in {'first_pass', 'repair'}, 'scope does not approve motion')
    require(scope.get('provider') == 'grok_cli', 'provider has no qualified strict motion provenance adapter')
    evidence = request['approval_evidence']
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
    else:
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
    require(scope.get('approval_plan_sha256') == execution.approval_plan_digest(contract)
            == execution.approval_plan_digest(current), 'approved planning semantics changed')
    shots = [shot for shot in contract['shots'] if shot['id'] == shot_id]
    require(len(shots) == 1, 'reserved shot missing or duplicated in contract')
    shot = shots[0]

    def bound_review(review, subject, names, label):
        review_schema = {'$defs': schema['$defs'], '$ref': '#/$defs/review'}
        require(not list(Draft202012Validator(review_schema).iter_errors(review)), f'{label}: incomplete semantic evidence')
        require(review['status'] == 'pass' and review['subject_sha256'] == subject
                and review['story_revision'] == story_revision, f'{label}: failed/stale review')
        seen = [predicate['name'] for predicate in review['predicates']]
        require(len(seen) == len(set(seen)) and names.issubset(seen), f'{label}: missing/duplicated required predicates')
        for predicate in review['predicates']:
            critical = predicate['name'] in CRITICAL_PREDICATES or predicate.get('severity', 'critical') == 'critical'
            require(not (predicate['name'] in CRITICAL_PREDICATES and predicate.get('severity') == 'cosmetic'), f'{label}: critical predicate downgraded')
            require(not critical or predicate['status'] == 'pass', f'{label}: critical evidence not passing')

    bound_review(contract['project_review'], digest, PROJECT_PREDICATES, 'frozen project review')
    bound_review(shot['review'], digest, SHOT_PREDICATES, 'frozen shot review')
    assets = {asset['id']: asset for asset in contract['assets']}
    require(len(assets) == len(contract['assets']), 'duplicate frozen asset IDs')
    required_assets = set(shot['asset_ids']) | {contract['payoff_asset_id']}
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
        bound_review(selection['review'], selection_digest(selection), UPSTREAM_PREDICATES, 'frozen upstream review')
        current_upstream = current_selected.get(binding['shot_id'])
        require(isinstance(current_upstream, dict) and selection_digest(current_upstream) == selection_digest(selection)
                and review_digest(current_upstream.get('review')) == review_digest(selection['review']),
                'upstream selection changed since this attempt')

    submitted = request['submitted_inputs']
    bindings = request['input_assets']
    require(isinstance(submitted, dict) and isinstance(bindings, list) and bool(bindings), 'immutable submitted inputs missing')
    remaining = iter(bindings)

    def restore_binding(role, path):
        record = next(remaining, None)
        require(isinstance(record, dict) and record.get('role') == role, 'submitted input occurrences differ')
        preserved = bound_file(record, directory / 'inputs', 'input snapshot')
        require(preserved == path.resolve(), 'submitted input path differs from snapshot')
        original = inside(record.get('original_path', ''), root)
        return {'path': str(original), 'sha256': record['sha256']}

    clean_submitted = copy.deepcopy(submitted)
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
    execution._check_motion_inputs(contract, shot_id, submitted, root)
    expected_hashes = {assets[asset_id]['sha256'] for asset_id in shot['asset_ids']}
    require(all(item['sha256'] in expected_hashes for item in bindings), 'submitted input outside approved shot assets')

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
        original_path = directory / 'result.json'
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


def validate_attempt_provenance(
    project_dir: str | Path, attempt_id: str, *, shot_id: str,
    story_revision: str, expected_output: dict[str, str],
) -> dict[str, Any]:
    """Require complete retained evidence; malformed data always fails closed."""
    from lib.production_execution import ProductionGovernanceError
    try:
        return _validate_attempt_provenance(
            project_dir, attempt_id, shot_id=shot_id,
            story_revision=story_revision, expected_output=expected_output,
        )
    except ProductionGovernanceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise ProductionGovernanceError(f'attempt provenance: incomplete/invalid evidence ({exc})') from exc
