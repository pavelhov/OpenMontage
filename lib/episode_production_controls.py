"""Episode production controls: an approved, append-only per-episode overlay.

artifacts/episode_production_controls.json = {version:'1.0', project_id, records:[...]}.
Records chain by previous_sha256 and bind retained approval evidence
{path, sha256, approved_by}. Authority is {kind:'episode_controls_approval',
grants:[]}: controls never manufacture scopes, billing, accounts or provider
authority. Existing scopes, pins, locks and policies still bind; controls only
narrow admission. A missing artifact means legacy behavior.
"""
from __future__ import annotations

import copy
import datetime
import json
import os
from pathlib import Path
import uuid

VERSION = '1.0'
ARTIFACT = Path('artifacts') / 'episode_production_controls.json'
ALTERNATE_MODES = ('different_provider_or_media_model', 'disabled')
PURPOSES = ('first_pass', 'repair', 'local_continuation', 'mcp_begin')
CONTROL_KEYS = {'max_generations_per_shot', 'alternate_repair', 'access_fallback'}
AUTHORITY = {'kind': 'episode_controls_approval', 'grants': []}


class EpisodeControlsError(ValueError):
    """Controls are invalid, stale, or refuse admission."""


def _fail(message):
    raise EpisodeControlsError('episode controls: ' + message)


def _ex():
    from lib import production_execution as execution
    return execution


def _root(project_dir):
    root = Path(project_dir).expanduser().resolve()
    if not (root / 'project.json').is_file():
        _fail('project.json missing')
    return root


def _marker(root):
    try:
        marker = json.loads((root / 'project.json').read_text())
    except (OSError, ValueError) as exc:
        _fail(f'cannot read project.json: {exc}')
    if not isinstance(marker, dict) or not marker.get('project_id') or not marker.get('story_revision'):
        _fail('project.json lacks project_id/story_revision')
    return marker


def _load(root):
    path = root / ARTIFACT
    if path.is_symlink():
        _fail('controls artifact cannot be a symlink')
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _fail(f'cannot read controls artifact: {exc}')
    if not isinstance(data, dict) or data.get('version') != VERSION or not isinstance(data.get('records'), list):
        _fail('malformed controls artifact')
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parent.parent / 'schemas' / 'artifacts'
                         / 'episode_production_controls.schema.json').read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        _fail('controls artifact schema: ' + errors[0].message)
    previous = None
    for record in data['records']:
        if not isinstance(record, dict) or record.get('previous_sha256') != previous:
            _fail('controls record chain is broken')
        if record.get('project_id') != data['project_id']:
            _fail('controls record belongs to another project')
        # Prospective replay: retained approval evidence must still be the
        # approved bytes, or dispatch stops honouring these controls.
        if _evidence(root, record.get('evidence')) != record.get('evidence'):
            _fail('controls record evidence is not canonical')
        if record.get('authority') != AUTHORITY:
            _fail('controls record claims authority beyond controls approval')
        if record.get('kind') == 'controls':
            _check_controls(record.get('controls'))
        elif record.get('kind') == 'provider_restriction':
            providers = (record.get('restriction') or {}).get('providers')
            if not isinstance(providers, list) or not providers:
                _fail('malformed provider restriction record')
        else:
            _fail('unknown controls record kind')
        previous = _ex()._digest(record)
    return data


def _evidence(root, evidence):
    execution = _ex()
    if not isinstance(evidence, dict) or set(evidence) != {'path', 'sha256', 'approved_by'}:
        _fail('evidence must be exactly {path, sha256, approved_by}')
    if not isinstance(evidence['approved_by'], str) or not evidence['approved_by'].strip():
        _fail('evidence approved_by required')
    try:
        path = execution._inside(evidence['path'], root)
    except ValueError as exc:
        _fail(str(exc))
    if not path.is_file() or execution.file_sha256(path) != evidence['sha256']:
        _fail('approval evidence is missing or changed')
    return {'path': str(path.relative_to(root)), 'sha256': evidence['sha256'],
            'approved_by': evidence['approved_by']}


def _check_controls(controls):
    if not isinstance(controls, dict) or not CONTROL_KEYS <= set(controls) or set(controls) - CONTROL_KEYS - {'first_cut'}:
        _fail('controls require ' + ', '.join(sorted(CONTROL_KEYS)) + '; only first_cut is optional')
    limit = controls['max_generations_per_shot']
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        _fail('max_generations_per_shot must be an integer >= 1')
    if controls['alternate_repair'] not in ALTERNATE_MODES:
        _fail('alternate_repair must be one of ' + ', '.join(ALTERNATE_MODES))
    if not isinstance(controls['access_fallback'], bool):
        _fail('access_fallback must be boolean')
    if 'first_cut' in controls and (not isinstance(controls['first_cut'], dict)
            or set(controls['first_cut']) != {'enabled'} or not isinstance(controls['first_cut']['enabled'], bool)):
        _fail('first_cut must be exactly {enabled: boolean}')
    return copy.deepcopy(controls)


def _effective(data, marker):
    if data is None:
        return None
    if data.get('project_id') != marker['project_id']:
        _fail('controls artifact belongs to another project')
    controls, restricted = None, set()
    for record in data['records']:
        if record.get('story_revision') != marker['story_revision']:
            continue  # stale story revisions never govern the current episode
        if record['kind'] == 'controls':
            controls = record['controls']
        else:
            restricted.update(record['restriction']['providers'])
    if controls is None and not restricted:
        return None
    base = controls or {'max_generations_per_shot': None, 'alternate_repair': 'disabled', 'access_fallback': False}
    return {**copy.deepcopy(base), 'restricted_providers': sorted(restricted),
            'head_sha256': _ex()._digest(data['records'][-1]), 'records': len(data['records'])}


def effective_controls(project_dir):
    """None for legacy projects; otherwise current-story effective controls."""
    root = _root(project_dir)
    return _effective(_load(root), _marker(root))


def first_cut_enabled(project_dir):
    """Episode opt-in; absent controls and older records remain default-off."""
    effective = effective_controls(project_dir)
    return bool(effective and (effective.get('first_cut') or {}).get('enabled'))


def _append(root, kind, key, payload, evidence):
    execution = _ex()
    with execution._lock(root):
        marker = _marker(root)
        governance = marker.get('governance') or {}
        if governance.get('mode') != 'strict' or governance.get('version') != '1.0':
            # Controls are enforced only by strict governed preflight; recording
            # them on a legacy project would report enforcement that never runs.
            _fail('episode controls require a strict governed project')
        data = _load(root) or {'version': VERSION, 'project_id': marker['project_id'], 'records': []}
        if data['project_id'] != marker['project_id']:
            _fail('controls artifact belongs to another project')
        record = {'kind': kind, 'record_id': str(uuid.uuid4()),
                  'recorded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
                  'previous_sha256': execution._digest(data['records'][-1]) if data['records'] else None,
                  'evidence': evidence, 'authority': dict(AUTHORITY, grants=[]), key: payload}
        data = {**data, 'records': [*data['records'], record]}
        path = root / ARTIFACT
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name('.' + path.name + '-' + str(uuid.uuid4()) + '.tmp')
        try:
            with temp.open('x') as stream:
                json.dump(data, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return record, execution._digest(record), _effective(data, marker)


def record_episode_controls(project_dir, controls, *, evidence):
    """Record approved episode controls -> {record_id, record_sha256, effective}."""
    root = _root(project_dir)
    controls = _check_controls(controls)
    evidence = _evidence(root, evidence)
    record, digest, effective = _append(root, 'controls', 'controls', controls, evidence)
    return {'record_id': record['record_id'], 'record_sha256': digest, 'effective': effective}


def restrict_episode_provider(project_dir, providers, *, evidence, reason):
    """Prospectively stop new submissions to providers. Pending originals stay
    collectable and historic footage stays valid; nothing is cancelled remotely."""
    root = _root(project_dir)
    if isinstance(providers, str):
        providers = [providers]
    if (not isinstance(providers, (list, tuple)) or not providers
            or any(not isinstance(item, str) or not item for item in providers)):
        _fail('providers must be a non-empty list of provider names')
    if not isinstance(reason, str) or not reason.strip():
        _fail('reason required')
    evidence = _evidence(root, evidence)
    payload = {'providers': sorted(set(providers)), 'reason': reason}
    record, digest, effective = _append(root, 'provider_restriction', 'restriction', payload, evidence)
    restricted = set(effective['restricted_providers'])
    usage = generation_usage(root)
    prepared = [i['attempt_id'] for i in usage['occurrences']
                if i['provider'] in restricted and i['reason'] == 'mcp_prepared_not_begun']
    local = [i['attempt_id'] for i in usage['occurrences']
             if i['provider'] in restricted and i['local_continuation_eligible']]
    pending = [i['attempt_id'] for i in usage['occurrences']
               if i['provider'] in restricted and i['attempt_id'] in usage['unresolved_originals']]
    scope_ids = sorted(s.get('id') for s in _scopes(root)
                       if s.get('provider') in restricted and s.get('status') == 'approved')
    return {'record_id': record['record_id'], 'record_sha256': digest,
            'restricted_providers': effective['restricted_providers'],
            'blocked_unsubmitted': {'scope_ids': scope_ids, 'mcp_prepared_attempt_ids': prepared,
                                    'local_continuation_attempt_ids': local},
            'pending_originals': pending, 'remote_cancellation': False, 'effective': effective}


def _scopes(root):
    path = root / 'production_scopes.json'
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get('scopes', [])
    except (OSError, ValueError, AttributeError):
        return []


def _unresolved_ledger_attempts():
    """Attempt IDs with unacknowledged private credit outbox rows, or None
    when the ledger cannot be read (which never proves non-submission)."""
    try:
        from lib.provider_credit_ledger import read_existing_snapshot
        snapshot = read_existing_snapshot()
    except Exception:
        return None
    rows = list(snapshot.get('outbox', [])) + list(snapshot.get('unpriced_outbox', []))
    return {row.get('attempt_id') for row in rows}


def _journal_occurrence(root, request, ledger):
    """(state, counted, reason, local_continuation_eligible) for one journal."""
    execution = _ex()
    aid = request['attempt_id']
    directory = root / 'production_attempts' / aid
    if (directory / 'local_continuation_claim.json').exists():
        # The prepared original was continued: that submission counts once.
        try:
            state = execution._state(root, request).get('status', 'uncertain')
        except ValueError:
            state = 'uncertain'
        return state, True, 'local_continuation_submitted', False
    result_path = directory / 'result.json'
    if not result_path.exists():
        return 'uncertain', True, 'no_result', False
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        return 'uncertain', True, 'unreadable_result', False
    inner = result.get('result') or {}
    proven = (result.get('status') == 'failed' and result.get('exception') is None
              and result.get('output') is None and inner.get('success') is False
              and (inner.get('data') or {}).get('dispatch_status') == 'not_dispatched'
              and (directory / 'raw_result.json').is_file()
              and not (directory / 'provider_request.json').exists()
              and not (directory / 'provider_result.json').exists()
              and ledger is not None and aid not in ledger)
    if proven:
        # A wrapper label alone is not proof: only the canonical retained
        # pre-CLI refusal receipt shows no provider ever received the call.
        try:
            proven = bool(execution._local_grok_failure(directory, request))
        except Exception:
            proven = False
    if proven:
        return 'failed', False, 'proven_not_dispatched', True
    try:
        state = execution._state(root, request).get('status', 'uncertain')
    except ValueError:
        state = 'uncertain'
    if (state == 'failed' and (inner.get('data') or {}).get('dispatch_status') == 'not_dispatched'):
        # Claimed but unproven non-dispatch: the provider may hold the job.
        return 'uncertain', True, 'unproven_not_dispatched', False
    return state, True, 'submitted_or_uncertain', False


UNRESOLVED_STATES = frozenset({'uncertain', 'submitted', 'awaiting_original_result'})
_MANAGED_MEDIA_PROVIDERS = frozenset({'grok_cli', 'grok_video'})


def _media_model(provider, model):
    """Reported media model only. Grok's CLI-managed media model is unreported
    (None); an agent model, mode or prompt never stands in for it."""
    return None if provider in _MANAGED_MEDIA_PROVIDERS else model


def alternate_route_eligible(original_provider, original_model, provider, model):
    """True when (provider, model) is an alternate to the original route: a
    different provider or a different reported media model. Mode, tool or
    route key alone never makes an alternate."""
    original_model = _media_model(original_provider, original_model)
    model = _media_model(provider, model)
    return original_provider != provider or (model is not None and model != original_model)


def generation_usage(project_dir):
    """Trusted per-shot motion generation occurrences from journals and MCP state.

    Each attempt_id counts once whether submitted, accepted, terminal or
    uncertain; status/collection never adds one. Only canonical positive
    never-submitted proof (or an unbegun MCP preparation) excludes it."""
    root = _root(project_dir)
    ledger = _unresolved_ledger_attempts()
    occurrences = []
    for path in sorted((root / 'production_attempts').glob('*/request.json')):
        request = json.loads(path.read_text())
        if request.get('media_kind') != 'motion':
            continue
        state, counted, reason, local = _journal_occurrence(root, request, ledger)
        scope = request.get('scope') or {}
        provider = scope.get('provider')
        occurrences.append({'attempt_id': request['attempt_id'], 'shot_id': request.get('shot_id'),
                            'provider': provider,
                            'model': _media_model(provider, (request.get('submitted_inputs') or {}).get('model')),
                            'phase': request.get('phase'), 'state': state, 'counted': counted,
                            'reason': reason, 'local_continuation_eligible': local})
    if (root / 'openart_mcp' / 'attempts').exists():
        from lib import openart_mcp_jobs as jobs
        for row in jobs.list_attempts(root):
            raw = jobs._load(root, row['attempt_id'])
            unbegun = raw['status'] == 'prepared' and raw.get('begin_envelope') is None
            occurrences.append({'attempt_id': row['attempt_id'], 'shot_id': row['shot_id'],
                                'provider': 'openart_mcp',
                                'model': (row.get('submitted_inputs') or {}).get('model'),
                                'phase': row.get('phase'), 'state': raw['status'], 'counted': not unbegun,
                                'reason': 'mcp_prepared_not_begun' if unbegun else 'submitted_or_uncertain',
                                'local_continuation_eligible': False})
    per_shot, repair = {}, {}
    for item in occurrences:
        if item['counted']:
            per_shot[item['shot_id']] = per_shot.get(item['shot_id'], 0) + 1
            if item['phase'] == 'repair':
                repair[item['shot_id']] = repair.get(item['shot_id'], 0) + 1
    unresolved = [i['attempt_id'] for i in occurrences if i['counted']
                  and i['state'] in UNRESOLVED_STATES]
    # A completed MCP original still awaiting collection is pending media, not
    # uncertainty: collection costs no slot but the shot is not ready yet.
    pending = unresolved + [i['attempt_id'] for i in occurrences
                            if i['provider'] == 'openart_mcp' and i['state'] == 'completed']
    return {'total': sum(per_shot.values()), 'per_shot': per_shot, 'repair': repair,
            'occurrences': occurrences,
            'excluded_never_submitted': [i['attempt_id'] for i in occurrences if not i['counted']],
            'unresolved_originals': unresolved, 'pending': pending}


def shot_usage(project_dir, shot_id, *, exclude_attempt_id=None):
    usage = generation_usage(project_dir)
    return sum(1 for i in usage['occurrences'] if i['counted'] and i['shot_id'] == shot_id
               and i['attempt_id'] != exclude_attempt_id)


def generation_admission(project_dir, *, shot_id, provider, model=None, purpose,
                         replaces_attempt_ids=(), exclude_attempt_id=None,
                         repair_basis='critical_review', creator_repair=None):
    """Whether one new generation for shot_id may proceed under episode controls.

    -> {admitted, reasons, limit, used, controls_present}. Controls only narrow;
    existing scope/policy caps are enforced separately by their own checks."""
    if purpose not in PURPOSES:
        _fail('purpose must be one of ' + ', '.join(PURPOSES))
    root = _root(project_dir)
    effective = effective_controls(root)
    if effective is None:
        return {'admitted': True, 'reasons': [], 'limit': None, 'used': None, 'controls_present': False}
    creator_item = False
    if repair_basis == 'creator_batch':
        from lib.production_repair_batches import validate_creator_repair_intent
        validate_creator_repair_intent(root, creator_repair, shot_id=shot_id, provider=provider,
            model=model, replaces_attempt_ids=list(replaces_attempt_ids))
        creator_item = True
    reasons = []
    repairing = purpose == 'repair' or purpose == 'mcp_begin' and bool(replaces_attempt_ids)
    if repairing and (effective.get('first_cut') or {}).get('enabled') and repair_basis == 'critical_review':
        reasons.append('first-cut mode defers creative generation until an exact creator-selected repair')
    if provider in effective['restricted_providers']:
        reasons.append(f'provider {provider} is restricted for this episode')
    usage = generation_usage(root)
    used = sum(1 for i in usage['occurrences'] if i['counted'] and i['shot_id'] == shot_id
               and i['attempt_id'] != exclude_attempt_id)
    pending = set(usage['pending'])
    if purpose != 'mcp_begin' and any(
            i['counted'] and i['shot_id'] == shot_id and i['attempt_id'] in pending
            and i['attempt_id'] != exclude_attempt_id for i in usage['occurrences']):
        reasons.append(f'a pending original for {shot_id} blocks a duplicate generation')
    limit = effective['max_generations_per_shot']
    if limit is not None and used >= limit:
        reasons.append(f'episode generation limit reached for {shot_id} ({used}/{limit})')
    routes = {i['attempt_id']: (i['provider'], i['model']) for i in usage['occurrences']}
    # MCP begin replays the repair route rule so an amendment between prepare
    # and begin cannot let a same-route reroll through.
    if repairing and not creator_item and effective['alternate_repair'] == 'different_provider_or_media_model':
        for original in replaces_attempt_ids or ():
            old_provider, old_model = routes.get(original, (None, None))
            if not alternate_route_eligible(old_provider, old_model, provider, model):
                reasons.append(f'repair of {original} must use a different provider or media model')
    elif (not creator_item and repair_basis != 'access_fallback'
          and (purpose == 'repair' or (purpose == 'mcp_begin' and replaces_attempt_ids))
          and effective['alternate_repair'] == 'disabled'):
        # Ordinary critical repair stays on the original route; the separately
        # preapproved access fallback keeps its own different-route rule.
        model = _media_model(provider, model)
        for original in replaces_attempt_ids or ():
            old_provider, old_model = routes.get(original, (None, None))
            if old_provider != provider or (model is not None and old_model is not None and model != old_model):
                reasons.append(f'alternate repair is disabled: repair of {original} must keep its provider and media model')
    if repair_basis == 'access_fallback' and not effective['access_fallback']:
        reasons.append('access fallback is not approved in episode controls')
    return {'admitted': not reasons, 'reasons': reasons, 'limit': limit, 'used': used,
            'controls_present': True}


def require_admission(project_dir, **kwargs):
    decision = generation_admission(project_dir, **kwargs)
    if not decision['admitted']:
        _fail('; '.join(decision['reasons']))
    return decision


def _review_entry(root, shot_id, selection, selected=None):
    from lib.production_review_successors import resolve_selection_review
    entry = {'attempt_id': (selection or {}).get('attempt_id') if isinstance(selection, dict) else None,
             'review_status': None, 'warnings': [], 'critical': [], 'unknown': [], 'error': None}
    try:
        review = resolve_selection_review(root, shot_id, selection)
        _validate_current_selection(root, shot_id, selection, selected)
    except Exception as exc:  # surfaced, never silently replaced by the original
        entry.update(review_status='invalid', error=f'{type(exc).__name__}: {exc}')
        return entry
    entry['review_status'] = review.get('status') or review.get('verdict')
    for item in review.get('findings') or review.get('predicates') or []:
        if not isinstance(item, dict) or item.get('status') == 'pass':
            continue
        name = item.get('name') or item.get('id')
        if item.get('status') == 'unknown' or item.get('severity') == 'unknown':
            entry['unknown'].append(name)
        elif item.get('severity', 'critical') == 'critical':
            entry['critical'].append(name)
        else:
            entry['warnings'].append(name)
    entry['warnings'].extend(w for w in review.get('warnings') or [] if isinstance(w, str))
    return entry


def _validate_current_selection(root, shot_id, selection, selected):
    """Read-only: a review only reports while the selected bytes and current
    planning still validate. No lock is taken and nothing is written."""
    from lib.production_review_successors import _context
    from lib.production_provenance import validate_attempt_provenance
    from lib.shot_contract import validate_shot_contract
    context = _context(root, shot_id, selection)
    validate_attempt_provenance(root, selection['attempt_id'], shot_id=shot_id,
        story_revision=context['story_revision'], expected_output=selection['output'])
    if selected is None:
        selected = _ex().load_selected_attempts(root)
    readiness = validate_shot_contract(_ex().load_shot_contract(root), project_dir=root,
        shot_id=shot_id, selected_upstream=selected)
    if not readiness['eligible']:
        _fail('current selected bytes/planning are ineligible: ' + '; '.join(readiness['errors']))


def episode_production_status(project_dir):
    """Read-only status derived from controls, journals, scopes and selections."""
    root = _root(project_dir)
    marker = _marker(root)
    controls = effective_controls(root)
    usage = generation_usage(root)
    selections = {}
    selected = _ex().load_selected_attempts(root)
    for shot_id, selection in sorted(selected.items()):
        selections[shot_id] = _review_entry(root, shot_id, selection, selected)
    # Reported separately: per-scope allowances are not combined into one cap,
    # and policy-level caps are enforced by their own checks (not shown here).
    caps = {}
    limit = controls['max_generations_per_shot'] if controls else None
    for scope in _scopes(root):
        if scope.get('status') != 'approved':
            continue
        for shot_id, allowance in (scope.get('attempts_per_shot') or {}).items():
            cap = caps.setdefault(shot_id, {'controls_limit': limit, 'scope_allowances': {}})
            if isinstance(allowance, int) and not isinstance(allowance, bool):
                cap['scope_allowances'][scope.get('id')] = allowance
    for shot_id in usage['per_shot']:
        caps.setdefault(shot_id, {'controls_limit': limit, 'scope_allowances': {}})
    for shot_id, cap in caps.items():
        cap['used'] = usage['per_shot'].get(shot_id, 0)
    # Shot readiness covers every planned shot; a missing, invalid,
    # provisional, unknown or critical entry keeps the episode pending.
    try:
        planned = [shot['id'] for shot in _ex().load_shot_contract(root).get('shots', [])]
    except Exception as exc:
        planned, contract_error = [], f'{type(exc).__name__}: {exc}'
    else:
        contract_error = None
    shots = {}
    for shot_id in planned:
        entry = selections.get(shot_id)
        if entry is None:
            shots[shot_id] = 'missing'
        elif entry['error'] is not None:
            shots[shot_id] = 'invalid'
        elif entry['critical']:
            shots[shot_id] = 'critical'
        elif entry['unknown'] or entry['review_status'] != 'pass':
            shots[shot_id] = 'pending'
        else:
            shots[shot_id] = 'ready_with_warnings' if entry['warnings'] else 'ready'
    shots_ready = (contract_error is None and bool(planned)
                   and all(v in {'ready', 'ready_with_warnings'} for v in shots.values())
                   and not usage['pending'])
    # Final certification is only ever the canonical current final review
    # replayed against current master bytes; shot readiness never implies it.
    final = {'eligible': False, 'errors': [], 'warnings': []}
    final_path = root / 'artifacts' / 'final_review.json'
    if not final_path.is_file():
        final['errors'].append('final_review: no certified final review')
    else:
        try:
            from lib.production_review import validate_final_review
            result = validate_final_review(root, json.loads(final_path.read_text()))
            final.update(eligible=bool(result['eligible']), errors=list(result['errors']),
                         warnings=list(result['warnings']))
        except Exception as exc:
            final['errors'].append(f'final_review: {type(exc).__name__}: {exc}')
    if final['eligible'] and shots_ready:
        delivery = 'certified_final'
    elif shots_ready:
        delivery = 'shots_ready_final_pending'
    else:
        delivery = 'draft_only'
    return {'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
            'controls': controls, 'usage': usage, 'pending_originals': usage['unresolved_originals'],
            'selections': selections, 'shot_readiness': shots, 'contract_error': contract_error,
            'final_review': final, 'delivery': delivery, 'provider_calls': 0, 'generation_caps': caps,
            'generation_caps_note': 'controls_limit is the active episode control; scope_allowances are per-scope; policy caps not included'}
