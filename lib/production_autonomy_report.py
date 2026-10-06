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
from pathlib import Path
from uuid import uuid4

from lib import production_autonomy as autonomy
from lib import production_execution as execution
from lib.provider_credit_ledger import Binding, CreditScale, read_existing_snapshot


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
            if provider == 'openart_cli' else None,
            'media_model_status': 'reported' if provider == 'openart_cli' else 'unreported' if provider == 'grok_cli' else 'unknown',
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
    reservations = {}
    for row in snapshot['reservations']:
        binding = Binding(**json.loads(row['binding_json']))
        binding.validate()
        if binding.project_root == str(root) and binding.allowance_id == autonomy.openart_allowance_id(sha):
            _require(binding.attempt_id not in reservations, 'duplicate ledger attempt')
            public_journal = root / 'production_attempts' / binding.attempt_id / 'request.json'
            _require(not (public_journal.exists() or public_journal.is_symlink()) or binding.attempt_id in journals,
                     'matching policy reservation has an existing journal under another authority')
            reservations[binding.attempt_id] = (row, binding)
    rows = []; totals = {}
    from lib import openart_dispatch as dispatch, openart_jobs as jobs
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
            provider_spec = next((p for p in policy['providers'] if p['id'] == 'openart_cli'), None)
            _require(provider_spec is not None and binding.provider == 'openart_cli'
                     and binding.account_id == provider_spec['account_id_sha256']
                     and binding.workspace_id == provider_spec['workspace']
                     and binding.allowance == provider_spec['ceiling'], 'ledger reservation outside active credit envelope')
            manifest, actual_binding, frozen = dispatch._manifest(aid)
            _require(actual_binding == binding and manifest['marker']['project_id'] == policy['project_id']
                     and manifest['marker']['story_revision'] == policy['story_revision'], 'private dispatch binding mismatch')
            intended = manifest['journal_records']['request.json']
            if request is None:
                request = intended
                findings.append({'attempt_id': aid, 'code': 'outbox_only_reservation'})
            else:
                _require(request == intended, 'public/private journal mismatch')
                if row['slot_state'] != 'prepared':
                    ready = [r for r in snapshot['ready_journals'] if r['attempt_id'] == aid]
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
            _require(len(accounts) == 1, 'missing or duplicate reservation account')
            account = accounts[0]; scale = CreditScale(account['quantum'])
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
    directory = root / 'artifacts' / 'autonomy_reports'
    _require(directory.resolve().is_relative_to(root), 'report directory escapes project')
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = directory / f'{stamp}-{uuid4().hex}.json'
    execution._write_new(path, report)
    return path
