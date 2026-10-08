"""Append-only public summaries derived from retained production evidence.

This reader never dispatches, repairs journals, initializes billing state, or
accepts caller totals. Private native inputs are projected to nonsecret settings.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from lib import production_autonomy as autonomy
from lib import production_execution as execution
from lib.provider_credit_ledger import Binding, UnpricedBinding, CreditScale, read_existing_snapshot


def _require(ok, message):
    if not ok:
        raise autonomy.AutonomyError('autonomy report: ' + message)


def _object(value, label):
    _require(isinstance(value, dict), label + ' must be an object')
    return value


def _read(path):
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError, TypeError):
        raise autonomy.AutonomyError('autonomy report: missing or malformed evidence object') from None
    _require(isinstance(value, dict), 'evidence must be an object')
    return value


def _token(value):
    _require(value is None or isinstance(value, (int, float, bool)) or
             isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/+ -]{1,128}', value)
             and '://' not in value and not value.startswith('/'), 'unsafe settings value')
    return value


def _validate_request(root, request, policy, sha, decision_id, inputs, scope):
    _object(request, 'attempt request'); _object(scope, 'attempt scope')
    _object(scope.get('evidence'), 'scope evidence')
    _object(scope.get('derived_from_policy'), 'policy derivation')
    _require({'attempt_id', 'shot_id', 'input_assets', 'request_sha256'}.issubset(request), 'incomplete attempt request')
    _require(isinstance(request['input_assets'], list), 'input assets must be a list')
    aid = request['attempt_id']
    _require(isinstance(aid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', aid) is not None, 'unsafe attempt ID')
    _require(request.get('cli_session_id') == aid and request.get('project_id') == policy['project_id']
             and request.get('story_revision') == policy['story_revision'], 'attempt project/story/session mismatch')
    _require(scope.get('id') == request.get('scope_id') and scope.get('project_id') == policy['project_id']
             and scope.get('story_revision') == policy['story_revision'], 'scope identity mismatch')
    shot = request['shot_id']
    _require(shot in policy['shots'], 'attempt shot absent from policy')
    _require(scope.get('status') == 'approved' and scope.get('approved_by') == f'policy:{sha}'
             and scope.get('evidence', {}).get('sha256') == policy['evidence']['sha256'], 'scope approval differs from active policy evidence')
    derivation = scope.get('derived_from_policy', {})
    _require(derivation.get('policy_sha256') == sha and derivation.get('decision_id') == decision_id and
             derivation.get('baseline_sha256') == execution._digest(policy['shots'][shot]), 'policy/baseline binding mismatch')
    records = iter(request['input_assets'])
    def restore(role, path):
        record = next(records, None)
        _object(record, 'input snapshot')
        _require(record is not None and record['role'] == role and Path(record['path']).resolve() == path.resolve(),
                 'input snapshot occurrence mismatch')
        _require(path.resolve().is_relative_to(root / 'production_attempts' / aid / 'inputs')
                 and execution._input_sha256(path) == record['sha256'], 'input snapshot bytes mismatch')
        original = execution._inside(record['original_path'], root)
        return {'path': str(original), 'sha256': record['sha256']}
    clean = execution._clean(copy.deepcopy(inputs))
    session = clean.pop('cli_session_id', None)
    _require(session is None or session == aid, 'submitted session mismatch')
    restored = execution._paths(clean, root, restore)
    _require(next(records, None) is None and execution._digest(restored) == request['request_sha256'],
             'request digest mismatch')
    approved = scope['requests'][shot]
    if isinstance(approved, list):
        approved = approved[request['scope_attempt_index']]
    if not isinstance(approved, str):
        selected_path = root / 'production_attempts' / aid / 'selected_attempts.json'
        approved = execution.approved_request_digest(approved, project_dir=root, selected_attempts=_read(selected_path))
    _require(approved == request['request_sha256'], 'approved request binding mismatch')


def _settings(inputs, request, provider):
    return {'provider': provider, 'media_model': _token(inputs.get('model') or inputs.get('model_name'))
            if provider in {'openart_cli', 'openart_mcp'} else None,
            'media_model_status': 'reported' if provider in {'openart_cli', 'openart_mcp'} else 'unreported' if provider == 'grok_cli' else 'unknown',
            'declared_model': _token(inputs.get('model') or inputs.get('model_name')),
            'agent_model': _token(inputs.get('agent_model') or inputs.get('model')) if provider == 'grok_cli' else None,
            'duration': _token(inputs.get('duration')), 'resolution': _token(inputs.get('resolution')),
            'aspect_ratio': _token(inputs.get('aspect_ratio')),
            'references': [{'input_key': _token(a['role']), 'sha256': a['sha256']} for a in request['input_assets']]}


def _credit_units(row):
    state = row['debit_state']
    _require(state in {'reserved', 'unresolved', 'settled', 'refunded', 'released'}, 'unknown debit state')
    net = row['charged_units'] - row['refunded_units']
    _require(net >= 0, 'refund exceeds settled debit')
    reserved = row['reserved_units'] if state == 'reserved' else 0
    unresolved = row['reserved_units'] if state == 'unresolved' else 0
    return {'original_reserved': row['reserved_units'], 'active_reserved': reserved,
            'unresolved_hold': unresolved, 'active_hold': reserved + unresolved,
            'settled_gross': row['charged_units'], 'refunded': row['refunded_units'], 'settled_net': net}


def _finding_field(error):
    # Keep a bounded schema/check label, never its potentially private exception body.
    label = str(error).split(':', 1)[0]
    return label if re.fullmatch(r'[A-Za-z0-9_. -]{1,128}', label) else 'current_final_binding'


def _observed_credits(value):
    _require(isinstance(value, str), 'provider credits require a decimal string')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise autonomy.AutonomyError('autonomy report: invalid observed credits') from None
    _require(amount.is_finite() and amount >= 0, 'invalid observed credits')
    return value


def unknown_cost_report(project_root):
    """Read-only unknown-cost summary for Strict and policy-derived attempts.

    Unknown exposure never enters exact credit totals. Policy-derived attempts
    (rooted unknown-cost Auto-continue) appear here as well and in the
    ``openart_unknown_cost`` section of ``completion_report``.

    Reads typed private originals and safe evidence summaries without calling
    the provider, initializing state, approving, repairing, or writing files.
    Balance observations carry no per-job attribution or affordability claim.
    """
    from lib import openart_dispatch as dispatch, openart_credit as credit
    root = Path(project_root).resolve()
    snapshot = read_existing_snapshot()
    rows = snapshot.get('unpriced_reservations', [])
    origins = {row['attempt_id'] for row in rows}
    _require(len(origins) == len(rows), 'duplicate unpriced reservation')
    _require(all(item['attempt_id'] in origins for key in ('unpriced_claims', 'unpriced_outbox')
                 for item in snapshot.get(key, [])), 'unpriced pending state lacks original')
    attempts = []
    for row in rows:
        binding = UnpricedBinding(**json.loads(row['binding_json']))
        binding.validate()
        if binding.project_root != str(root):
            continue
        _require(binding.provider == 'openart_cli' and binding.attempt_id == row['attempt_id']
                 and binding.account_key == row['account_key'] and binding.claim_key == row['claim_key']
                 and binding.authorization_occurrence == row['authorization_occurrence'], 'unpriced ledger binding differs')
        manifest, original, frozen = dispatch._manifest(binding.attempt_id)
        _require(original == binding and manifest.get('authorization_kind') == 'unknown_cost', 'unpriced private original differs')
        authority = manifest['unknown_cost_authorization']
        _require(authority['sha256'] == binding.authorization_sha256
                 and authority['occurrence'] == binding.authorization_occurrence, 'unpriced authority binding differs')
        request = manifest['journal_records']['request.json']
        _require(request['request_sha256'] == binding.request_sha256, 'unpriced request binding differs')
        summary = manifest['unknown_cost_evidence']
        call_keys = {'provider_calls', 'read_only_cli_calls', 'generation_calls'}
        current = credit.load_unknown_evidence(summary['evidence_id'])
        _require({k: v for k, v in current.items() if k not in call_keys}
                 == {k: v for k, v in summary.items() if k not in call_keys}, 'unknown evidence summary changed')
        _require(summary['evidence_sha256'] == binding.evidence_sha256
                 and summary['account_id_sha256'] == binding.account_id
                 and summary['native_body_sha256'] == binding.native_sha256
                 and summary['profile_sha256'] == binding.profile_sha256
                 and frozen['native']['native_body_sha256'] == binding.native_sha256
                 and frozen['profile']['profile_sha256'] == binding.profile_sha256,
                 'unknown evidence account/native/profile binding differs')
        _require(summary['requested_charge'] == 'unknown'
                 and summary['workspace_billing_guarantee'] == 'unverified', 'unknown economics were overstated')
        slot = row['slot_state']; billing = row['billing_state']
        _require(slot in {'prepared', 'ready', 'submitting', 'submitted', 'uncertain', 'terminal', 'closed', 'no-dispatch'}
                 and billing in {'unknown', 'qualified'}, 'invalid unpriced state')
        if slot not in {'prepared', 'no-dispatch'}:
            dispatch._ready_journal(binding, manifest)
        classification = summary['price_classification']
        _require(classification in {'settings_matched', 'mismatched_default', 'missing', 'unavailable'}, 'invalid price classification')
        balance = summary['balance_observation']
        _require(balance['label'] == 'unattributed_observation', 'balance observation cannot be attributed to a job')
        item = {'attempt_id': binding.attempt_id, 'shot_id': _token(request['shot_id']),
                'authorization_kind': 'unknown_cost', 'request_sha256': binding.request_sha256,
                'requested_charge': 'unknown', 'price_classification': classification,
                'balance_observation': {'label': 'unattributed_observation', 'credits': _observed_credits(balance['credits'])},
                'workspace_billing_guarantee': 'unverified', 'slot_state': slot, 'billing_state': billing,
                'settings': _settings(frozen['inputs'], request, 'openart_cli')}
        if classification == 'mismatched_default' and summary.get('observed_default_price') is not None:
            default = summary['observed_default_price']
            item['observed_default_price'] = {'label': 'model_default_not_requested',
                'credits': _observed_credits(default['credits']),
                'settings': {key: _token(default['settings'].get(key)) for key in ('duration', 'resolution', 'aspect_ratio')}}
        if billing == 'qualified':
            _require(re.fullmatch('[a-f0-9]{64}', row['billing_evidence_sha256']) is not None
                     and bool(row['job_id']), 'qualified per-job billing proof missing')
            item['billed_amount'] = _observed_credits(row['billed_amount'])
        if row.get('release_kind'):
            _require(row['release_kind'] in {'terminal', 'proved_not_dispatched', 'inactive_unidentified'}, 'invalid unpriced release kind')
            item['release_kind'] = row['release_kind']
            if row['release_kind'] == 'inactive_unidentified' and not row.get('job_id'):
                refusal = dispatch.verify_provider_refusal(binding.attempt_id)
                if refusal is not None:
                    _require(refusal['code'] in {'insufficient_credit', 'unavailable_plan'}, 'invalid provider refusal code')
                    item['provider_refusal'] = {'code': refusal['code'], 'evidence_sha256': refusal['evidence_sha256']}
        attempts.append(item)
    _require(read_existing_snapshot() == snapshot, 'credit ledger changed during report; retry with current evidence')
    return {'version': '1.0', 'authorization_kind': 'unknown_cost',
            'as_of_utc': datetime.now(timezone.utc).isoformat(),
            'attempts': sorted(attempts, key=lambda item: item['attempt_id']), 'quality_status': 'unreviewed'}


def _mcp_dimensions(params):
    """Only exact ordinary/SmartShot fields actually present; never defaults."""
    aliases = {'duration': ('duration', 'videoDuration'),
               'resolution': ('resolution', 'videoResolution'),
               'aspect_ratio': ('aspectRatio', 'videoAspectRatio')}
    return {name: _token(next((params[field] for field in fields if field in params), None))
            for name, fields in aliases.items()}


def _mcp_policy_rows(root, policy, sha):
    """Read immutable connector origins separately from the CLI credit ledger."""
    spec = autonomy._provider_spec(policy, 'openart_mcp')
    if spec is None:
        return [], None, None
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    attempts = jobs.list_attempts(root)
    snapshot = {'attempts': attempts, 'frozen_sha256': {}}
    rows = []
    scopes = _read(root / 'production_scopes.json')['scopes'] if (root / 'production_scopes.json').exists() else []
    for item in attempts:
        frozen = jobs.frozen_request(root, item['attempt_id'])
        snapshot['frozen_sha256'][item['attempt_id']] = execution._digest(frozen)
        authority, native, inputs = frozen['authority'], frozen['native'], frozen['generation_inputs']
        scope = authority['scope']
        if (scope.get('derived_from_policy') or {}).get('policy_sha256') != sha:
            continue
        _require(scope['provider'] == 'openart_mcp' and execution._digest(scope) == authority['scope_sha256']
                 and len([s for s in scopes if s == scope]) == 1, 'MCP retained scope binding changed')
        profile = mcp.load_profile(native['model'], native['mode'], require='supported')
        autonomy.validate_policy_mcp_attempt(root, scope, inputs=inputs, native=native, profile=profile,
                                             authority=authority.get('billing'))
        state = execution.load_attempt_result(root, item['attempt_id'])
        status = state.get('status', 'uncertain')
        settings = _settings(inputs, {'input_assets': []}, 'openart_mcp')
        settings['references'] = [{'input_key': _token(asset['role']), 'sha256': asset['source_sha256']} for asset in native.get('input_assets', [])]
        if status == 'generated':
            from lib.production_provenance import validate_attempt_provenance
            output = execution._inside(inputs['output_path'], root)
            validate_attempt_provenance(root, item['attempt_id'], shot_id=authority['shot_id'],
                story_revision=policy['story_revision'], expected_output={'path': str(output), 'sha256': execution.file_sha256(output)})
        params = native['body']['params']
        settings.update(media_model=_token(native['model']), **_mcp_dimensions(params))
        baseline = policy['shots'][authority['shot_id']]
        template = autonomy.retained_baselines(root, policy)[authority['shot_id']]['planned_request_template']
        baseline_inputs = template['inputs']
        baseline_settings = _settings(baseline_inputs, {'input_assets': []},
                                      baseline_inputs.get('preferred_provider') or baseline_inputs.get('provider'))
        baseline_params = baseline_inputs.get('native_params') or {}
        for key, value in _mcp_dimensions(baseline_params).items():
            baseline_settings[key] = _token(baseline_inputs[key]) if key in baseline_inputs else value
        baseline_refs = [{'role': a['role'], 'sha256': a['sha256']} for a in baseline['static_input_assets']]
        baseline_settings['references'] = baseline_refs
        changes = {key: {'baseline': baseline_settings[key], 'attempted': value}
                   for key, value in settings.items() if key != 'references' and value != baseline_settings[key]}
        baseline_request_sha = baseline.get('request_sha256')
        if baseline_request_sha is None:
            baseline_request_sha = execution.approved_request_digest(template, project_dir=root,
                selected_attempts=scope['derived_from_policy'].get('resolved_upstream', {}))
        rows.append({'attempt_id': item['attempt_id'], 'job_id': _token(state.get('history_id')),
            'shot_id': authority['shot_id'], 'request_sha256': authority['request_sha256'],
            'status': _token(status), 'outbox_only': False, 'settings': settings,
            'credits': {'authorization_kind': 'unknown_cost', 'requested_charge': 'unknown',
                        'enforceable_credit_ceiling': False},
            'native_body_sha256': native['body_sha256'],
            'source_binding_sha256': native['source_binding_sha256'],
            'baseline_delta': {'baseline_sha256': execution._digest(baseline),
                'request_changed': authority['request_sha256'] != baseline_request_sha,
                'resolved_baseline_request_sha256': baseline_request_sha,
                'unresolved_dimensions': [] if baseline_settings['provider'] is not None else ['provider', 'media_model_attribution'],
                'baseline_references': baseline_refs, 'settings_comparison': 'retained_approved_evidence',
                'settings_changes': changes, 'baseline_settings': baseline_settings,
                'reference_bytes_changed': sorted(a['sha256'] for a in settings['references']) != sorted(a['sha256'] for a in baseline_refs)}})
    exposure = {'provider': 'openart_mcp', 'cost_status': 'unknown', 'billing': spec['billing'],
        'exposure_acknowledgement': spec['exposure_acknowledgement'], 'enforceable_credit_ceiling': False,
        'policy_sha256': sha, 'uid_sha256': spec['uid_sha256'], 'project_id': spec['project_id'],
        'routes': copy.deepcopy(spec['routes']), 'attempt_ids': sorted(row['attempt_id'] for row in rows),
        'attempts': len(rows)}
    return rows, exposure, snapshot


def completion_report(project_root, policy_sha):
    """Write a new report using current policy, attempts, ledger and certification.

    Inconsistent bindings raise before publication. Missing result/certification
    evidence yields explicit draft findings; it never becomes a passing review.
    """
    root = Path(project_root).resolve()
    policy, sha, decision_id = autonomy.require_active_policy(root)
    _require(sha == policy_sha, 'active policy SHA mismatch')
    marker = _read(root / 'project.json')
    _require((marker.get('project_id'), marker.get('story_revision')) ==
             (policy['project_id'], policy['story_revision']), 'current project/story mismatch')
    _object(policy.get('evidence'), 'policy evidence')
    evidence_path = execution._inside(policy['evidence']['path'], root)
    evidence_raw = evidence_path.read_bytes()
    _require(hashlib.sha256(evidence_raw).hexdigest() == policy['evidence']['sha256'], 'retained policy evidence changed')
    retained = json.loads(evidence_raw)['baselines']
    ledger_as_of = datetime.now(timezone.utc).isoformat()
    snapshot = read_existing_snapshot()
    journals = {}; journal_hashes = {}; findings = []
    for path in sorted((root / 'production_attempts').glob('*/request.json')):
        raw = path.read_bytes(); request = json.loads(raw)
        _require(isinstance(request, dict), 'journal must be an object')
        _require(request.get('attempt_id') == path.parent.name and path.resolve().is_relative_to(root), 'journal path mismatch')
        scope = _object(request.get('scope'), 'journal scope')
        _object(scope.get('evidence'), 'scope evidence')
        _object(request.get('approval_evidence'), 'attempt approval evidence')
        derivation = _object(scope['derived_from_policy'], 'policy derivation') if 'derived_from_policy' in scope else None
        if derivation is not None:
            _require(isinstance(derivation.get('policy_sha256'), str) and
                     re.fullmatch('[a-f0-9]{64}', derivation['policy_sha256']) is not None, 'malformed derived policy SHA')
        elif scope.get('approved_by') == f'policy:{sha}':
            _require(False, 'policy-approved attempt lacks derivation')
        if derivation is not None and derivation['policy_sha256'] == sha:
            journals[request['attempt_id']] = request
            journal_hashes[path] = hashlib.sha256(raw).hexdigest()
        else:
            findings.append({'attempt_id': request['attempt_id'], 'code': 'excluded_other_policy_or_strict_attempt'})
    reservations = {}; unpriced = {}
    provider_spec = next((p for p in policy['providers'] if p['id'] == 'openart_cli'), None)
    unknown_mode = autonomy._is_unknown(provider_spec)
    from lib import openart_dispatch as dispatch, openart_jobs as jobs
    for row in snapshot['reservations']:
        binding = Binding(**json.loads(row['binding_json']))
        binding.validate()
        if binding.project_root == str(root) and binding.allowance_id == autonomy.openart_allowance_id(sha):
            _require(not unknown_mode, 'exact credit reservation under unknown-cost policy')
            _require(binding.attempt_id not in reservations, 'duplicate ledger attempt')
            public_journal = root / 'production_attempts' / binding.attempt_id / 'request.json'
            _require(not (public_journal.exists() or public_journal.is_symlink()) or binding.attempt_id in journals,
                     'matching policy reservation has an existing journal under another authority')
            reservations[binding.attempt_id] = (row, binding)
    for row in snapshot.get('unpriced_reservations', []):
        binding = UnpricedBinding(**json.loads(row['binding_json']))
        binding.validate()
        if binding.project_root != str(root):
            continue
        manifest, actual_binding, frozen = dispatch._manifest(binding.attempt_id)
        _require(actual_binding == binding and manifest.get('authorization_kind') == 'unknown_cost',
                 'unpriced private original differs')
        derived = manifest.get('scope', {}).get('derived_from_policy') or {}
        if derived.get('policy_sha256') != sha:
            continue
        _require(unknown_mode, 'unknown-cost reservation under exact-credit policy')
        _require(binding.attempt_id not in unpriced, 'duplicate unpriced ledger attempt')
        public_journal = root / 'production_attempts' / binding.attempt_id / 'request.json'
        _require(not (public_journal.exists() or public_journal.is_symlink()) or binding.attempt_id in journals,
                 'matching policy reservation has an existing journal under another authority')
        unpriced[binding.attempt_id] = (row, binding)
    reservations.update(unpriced)
    rows = []; totals = {}
    unknown_attempts = []
    from lib.production_request import load_private_approval, validate_frozen_preparation_history, validate_frozen_preparation, _body
    for aid in sorted(journals.keys() | reservations.keys()):
        request = journals.get(aid)
        scope = request['scope'] if request else None
        provider = scope.get('provider') if scope else 'openart_cli'
        _require(aid not in reservations or provider == 'openart_cli',
                 'matching OpenArt reservation contradicts journal provider')
        credit = None; job_id = None; outbox_only = request is None
        if provider == 'openart_cli':
            _require(aid in reservations, 'OpenArt journal lacks ledger reservation')
            row, binding = reservations[aid]
            _require(provider_spec is not None and binding.provider == 'openart_cli'
                     and binding.account_id == provider_spec['account_id_sha256']
                     and binding.workspace_id == provider_spec['workspace']
                     and (unknown_mode or binding.allowance == provider_spec['ceiling']),
                     'ledger reservation outside active credit envelope')
            manifest, actual_binding, frozen = dispatch._manifest(aid)
            _require(actual_binding == binding and manifest['marker']['project_id'] == policy['project_id']
                     and manifest['marker']['story_revision'] == policy['story_revision'], 'private dispatch binding mismatch')
            if unknown_mode:
                authority = manifest['unknown_cost_authorization']
                _require(authority['sha256'] == binding.authorization_sha256
                         and authority['occurrence'] == binding.authorization_occurrence
                         and manifest['scope'].get('unknown_cost_authorization_sha256') == binding.authorization_sha256,
                         'unpriced authority binding differs')
            intended = manifest['journal_records']['request.json']
            if request is None:
                request = intended
                findings.append({'attempt_id': aid, 'code': 'outbox_only_reservation'})
            else:
                _require(request == intended, 'public/private journal mismatch')
                if row['slot_state'] != 'prepared':
                    ready = [r for r in snapshot['unpriced_ready_journals' if unknown_mode else 'ready_journals']
                             if r['attempt_id'] == aid]
                    _require(len(ready) == 1 and ready[0]['journal_sha256'] == hashlib.sha256(
                        (root / 'production_attempts' / aid / 'request.json').read_bytes()).hexdigest(), 'ready journal hash mismatch')
            frozen = execution.load_openart_frozen(request)
            validate_frozen_preparation_history(request, frozen, root)
            try:
                validate_frozen_preparation(request, frozen, root)
            except ValueError as exc:
                if str(exc) != 'stale source/reference/review/upstream bindings': raise
                findings.append({'attempt_id': aid, 'code': 'historical_preparation_current_source_changed'})
            scope = load_private_approval(request)['scope']
            _require(scope == manifest['scope'] and request['request_sha256'] == binding.request_sha256,
                     'private approval/request ledger binding mismatch')
            inputs = frozen['inputs']
            _require(frozen['profile'].get('source') == 'real', 'nonreal private request')
            accounts = [a for a in snapshot['accounts'] if a['account_key'] == row['account_key']]
            # Unpriced reservations never create a priced account/allowance row;
            # only exact-credit reservations require exactly one.
            _require(len(accounts) <= 1 if unknown_mode else len(accounts) == 1,
                     'missing or duplicate reservation account')
            account = accounts[0] if accounts else {'quarantined': 0}
            if unknown_mode:
                slot, billing = row['slot_state'], row['billing_state']
                _require(slot in {'prepared', 'ready', 'submitting', 'submitted', 'uncertain', 'terminal', 'closed', 'no-dispatch'}
                         and billing in {'unknown', 'qualified'}, 'invalid unpriced state')
                credit = {'authorization_kind': 'unknown_cost', 'requested_charge': 'unknown',
                          'slot_state': slot, 'billing_state': billing}
                if billing == 'qualified':
                    _require(re.fullmatch('[a-f0-9]{64}', row['billing_evidence_sha256'] or '') is not None
                             and bool(row['job_id']), 'qualified per-job billing proof missing')
                    credit['billed_amount'] = _observed_credits(row['billed_amount'])
                else:
                    findings.append({'attempt_id': aid, 'code': 'openart_cost_unknown'})
                unknown_attempts.append(aid)
            else:
                scale = CreditScale(account['quantum'])
                known = row['debit_state'] in {'settled', 'refunded', 'released'}
                _require(not known or row['debit_state'] == 'released' or bool(row['debit_evidence_sha256']), 'debit evidence missing')
                units = _credit_units(row)
                credit = {'quantum': scale.quantum, 'debit_state': row['debit_state'],
                          **{k: scale.display(v) for k, v in units.items()}}
                group = totals.setdefault(scale.quantum, {k: 0 for k in units if k != 'original_reserved'})
                for k, v in units.items():
                    if k != 'original_reserved': group[k] += v
                if row['debit_state'] == 'unresolved': findings.append({'attempt_id': aid, 'code': 'unresolved_openart_debit'})
            if account['quarantined'] or any(q['claim_key'] == binding.claim_key for q in snapshot['account_quarantine']):
                findings.append({'attempt_id': aid, 'code': 'credit_account_quarantined'})
            job_id = jobs.original_job_id(aid)
            if job_id:
                jobs._verify_raw_submit(aid, jobs.launch_record(aid), job_id, 'report_job_binding_invalid', proven=False)
        else:
            _require(provider == 'grok_cli', 'unsupported policy provider')
            inputs = request['submitted_inputs']
            evidence = request['approval_evidence']
            path = execution._inside(evidence['path'], root)
            _require(path.is_relative_to(root / 'production_attempts' / aid) and
                     execution.file_sha256(path) == evidence['sha256'] == scope['evidence']['sha256'], 'approval evidence mismatch')
        _validate_request(root, request, policy, sha, decision_id, inputs, scope)
        settings = _settings(inputs, request, provider)
        if provider == 'openart_cli':
            native_body = _body(frozen['native'])
            settings.update(media_model=_token(native_body['model']),
                            duration=_token(native_body['params'].get('duration')),
                            resolution=_token(native_body['params'].get('resolution')),
                            aspect_ratio=_token(native_body['params'].get('aspectRatio')))
        baseline = policy['shots'][request['shot_id']]
        baseline_refs = [{'role': a['role'], 'sha256': a['sha256']} for a in baseline['static_input_assets']]
        state = {'status': 'pending'} if outbox_only else execution._state(root, request)
        status = state.get('status', 'uncertain')
        if provider == 'openart_cli' and row['slot_state'] in {'terminal', 'closed'} and status in {'uncertain', 'terminal_unselected'}:
            try:
                jobs.verify_terminal_failure(aid, frozen['profile'])
            except jobs.OpenArtCLIError:
                findings.append({'attempt_id': aid, 'code': 'terminal_failure_evidence_unavailable'})
            else:
                status = 'failed'
        if status not in {'generated', 'failed'}:
            findings.append({'attempt_id': aid, 'code': 'attempt_result_unresolved'})
        if status == 'generated':
            from lib.production_provenance import validate_attempt_provenance
            verified = validate_attempt_provenance(root, aid, shot_id=request['shot_id'],
                                                  story_revision=policy['story_revision'], expected_output=state['output'])
            if provider == 'grok_cli':
                receipt = verified['result']['result']['data']['conditioning_receipt']
                settings['agent_model'] = _token(receipt['agent_model'])
                settings['duration'] = _token(receipt['submitted_arguments'].get('duration'))
                settings['resolution'] = _token(receipt['submitted_arguments'].get('resolution_name'))
                settings['aspect_ratio'] = _token(receipt['submitted_arguments'].get('aspect_ratio'))
        baseline_template = retained[request['shot_id']]['planned_request_template']
        baseline_inputs = baseline_template['inputs']
        baseline_settings = _settings(baseline_inputs, {'input_assets': []},
                                      baseline_inputs.get('preferred_provider') or baseline_inputs.get('provider'))
        baseline_settings['references'] = baseline_refs
        changes = {k: {'baseline': baseline_settings[k], 'attempted': settings[k]}
                   for k in settings if k != 'references' and settings[k] != baseline_settings[k]
                   and (baseline_settings['provider'] is not None
                        or k in {'duration', 'resolution', 'aspect_ratio', 'declared_model'})}
        unresolved_dimensions = [] if baseline_settings['provider'] is not None else ['provider', 'media_model_attribution']
        if baseline.get('request_sha256') is not None:
            baseline_request_sha = baseline['request_sha256']
        else:
            _require(execution._digest(baseline_template) == baseline.get('planned_request_template_sha256'),
                     'retained template descriptor mismatch')
            selected_path = root / 'production_attempts' / aid / 'selected_attempts.json'
            selected = manifest['journal_records']['selected_attempts.json'] if provider == 'openart_cli' else \
                       _read(selected_path) if selected_path.is_file() else {}
            try:
                baseline_request_sha = execution.approved_request_digest(baseline_template, project_dir=root, selected_attempts=selected)
            except execution.ProductionGovernanceError as exc:
                if str(exc) != 'production governance: dynamic upstream request lacks matching selected bytes': raise
                baseline_request_sha = None
                unresolved_dimensions.append('baseline_request_comparison')
        if 'provider' in unresolved_dimensions:
            findings.append({'attempt_id': aid, 'code': 'baseline_provider_attribution_unknown'})
        if 'baseline_request_comparison' in unresolved_dimensions:
            findings.append({'attempt_id': aid, 'code': 'baseline_dynamic_upstream_unresolved'})
        rows.append({'attempt_id': aid, 'job_id': _token(job_id), 'shot_id': request['shot_id'],
                     'request_sha256': request['request_sha256'], 'status': _token(status),
                     'outbox_only': outbox_only, 'settings': settings, 'credits': credit,
                     'baseline_delta': {'baseline_sha256': execution._digest(baseline),
                        'request_changed': None if baseline_request_sha is None else request['request_sha256'] != baseline_request_sha,
                        'resolved_baseline_request_sha256': baseline_request_sha,
                        'reference_bytes_changed': sorted(r['sha256'] for r in settings['references']) != sorted(r['sha256'] for r in baseline_refs),
                        'baseline_references': baseline_refs, 'settings_comparison': 'retained_approved_evidence',
                        'settings_changes': changes, 'baseline_settings': baseline_settings,
                        'unresolved_dimensions': unresolved_dimensions}})
    mcp_rows, mcp_exposure, mcp_snapshot = _mcp_policy_rows(root, policy, sha)
    rows.extend(mcp_rows)
    findings.extend({'attempt_id': row['attempt_id'], 'code': 'openart_mcp_cost_unknown'} for row in mcp_rows)
    findings.extend({'attempt_id': row['attempt_id'], 'code': 'attempt_result_unresolved'} for row in mcp_rows if row['status'] not in {'generated', 'failed'})
    from lib.production_review import validate_final_review, CURRENT_VERSION
    final_path = root / 'artifacts' / 'final_review.json'
    final_raw = final_path.read_bytes() if final_path.exists() else None
    malformed_final = False
    try:
        review = json.loads(final_raw) if final_raw is not None else None
    except (ValueError, UnicodeError):
        review = None
        malformed_final = True
    result = validate_final_review(root, review)
    findings.extend({'code': 'final_certification_ineligible', 'message': 'Current final certification failed a required check',
                                        'category': 'malformed_current_final_review' if malformed_final or (final_raw is not None and not isinstance(review, dict)) else
                                                    'missing_current_final_review' if review is None else
                                                    'unsupported_review_version' if review.get('version') != CURRENT_VERSION else
                                                    'current_final_binding_or_review_failure',
                                        'field': _finding_field(e), 'detail_sha256': hashlib.sha256(str(e).encode()).hexdigest()}
                    for e in result['errors'])
    grok_count = sum(r['settings']['provider'] == 'grok_cli' for r in rows)
    report = {'version': '1.0', 'project_id': policy['project_id'], 'story_revision': policy['story_revision'],
              'policy_sha256': sha, 'decision_id': decision_id, 'attempts': rows,
              'credit_ledger_as_of_utc': ledger_as_of, 'credit_ledger_snapshot_sha256': execution._digest(snapshot),
              'openart_credits': [{'quantum': q, **{k: CreditScale(q).display(v) for k, v in group.items()}}
                                 for q, group in sorted(totals.items())],
              'grok': f'subscription quota unknown, {grok_count} attempts',
              'quality_status': 'certified' if result['eligible'] else 'draft', 'findings': findings,
              'final_review_sha256': hashlib.sha256(final_raw).hexdigest() if final_raw is not None else None}
    if unknown_mode:
        report['openart_unknown_cost'] = {
            'cost_status': 'unknown', 'billing': provider_spec['billing'],
            'exposure_acknowledgement': provider_spec['exposure_acknowledgement'],
            'policy_sha256': sha, 'account_id_sha256': provider_spec['account_id_sha256'],
            'workspace': provider_spec['workspace'],
            'routes': [{'model': r['model'], 'mode': r['mode']} for r in provider_spec['routes']],
            'attempt_ids': sorted(unknown_attempts), 'attempts': len(unknown_attempts)}
    if mcp_exposure is not None:
        report['openart_mcp_unknown_cost'] = mcp_exposure
    # Recheck authorization after evidence collection, still before any writes.
    current, current_sha, current_decision = autonomy.require_active_policy(root)
    _require(current_sha == sha and current_decision == decision_id and current == policy, 'policy changed during report')
    _require(evidence_path.read_bytes() == evidence_raw, 'retained policy evidence changed during report')
    for journal_path, expected_hash in journal_hashes.items():
        _require(execution.file_sha256(journal_path) == expected_hash, 'attempt journal changed during report')
    for attempt in rows:
        if attempt['outbox_only']:
            path = root / 'production_attempts' / attempt['attempt_id'] / 'request.json'
            _require(not (path.exists() or path.is_symlink()), 'outbox-only public journal appeared during report; retry')
    _require((final_path.read_bytes() if final_path.exists() else None) == final_raw, 'final certification changed during report')
    _require(read_existing_snapshot() == snapshot, 'credit ledger changed during report; retry with current evidence')
    if mcp_snapshot is not None:
        from lib.openart_mcp_jobs import list_attempts as list_mcp_attempts, frozen_request as frozen_mcp_request
        _require(list_mcp_attempts(root) == mcp_snapshot['attempts'], 'MCP attempt state changed during report; retry')
        _require(all(execution._digest(frozen_mcp_request(root, aid)) == expected
                     for aid, expected in mcp_snapshot['frozen_sha256'].items()), 'MCP original evidence changed during report; retry')
    directory = root / 'artifacts' / 'autonomy_reports'
    _require(directory.resolve().is_relative_to(root), 'report directory escapes project')
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = directory / f'{stamp}-{uuid4().hex}.json'
    execution._write_new(path, report)
    return path
