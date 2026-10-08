"""Read-only same-shot motion exclusivity, independent of billing settlement.

Call under the production project lock before reserving or dispatching. This
reader never creates a ledger, repairs an outbox, or contacts a provider.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from lib.provider_credit_ledger import (Binding, UnpricedBinding, LedgerError, QualifiedSlotRelease,
                                        _digest, read_existing_snapshot)


def _proved_no_dispatch(binding, row, snapshot):
    """Validate the existing host-qualified prelaunch release, never a timeout."""
    from lib import openart_jobs as jobs
    if row['slot_state'] != 'no-dispatch' or row['release_kind'] != 'proved_not_dispatched':
        return False
    unpriced = isinstance(binding, UnpricedBinding)
    prefix = 'unpriced_' if unpriced else ''
    raw = json.loads(row['release_json'])
    binding_type = UnpricedBinding if unpriced else Binding
    release = QualifiedSlotRelease(binding=binding_type(**raw.pop('binding')), **raw)
    _digest(release.evidence_sha256)
    _digest(release.original_process_exit_sha256)
    if (release.binding != binding or release.kind != 'proved_not_dispatched'
            or release.job_id or row['job_id']
            or (not unpriced and (row['charged_units'] or row['debit_state'] != 'released'))
            or row['release_json'] != json.dumps(asdict(release), sort_keys=True, separators=(',', ':'))
            or any(c['attempt_id'] == binding.attempt_id for c in snapshot[prefix + 'claims'])):
        return False
    marker = jobs.job_dir(binding.attempt_id, create=False) / 'launch.json'
    if marker.exists() or marker.is_symlink():
        return False
    for event in snapshot[prefix + 'outbox']:
        if event['attempt_id'] != binding.attempt_id:
            continue
        if event['kind'] not in {'prepared', 'ready', 'no-dispatch'}:
            return False
        if event['kind'] == 'no-dispatch' and event['evidence_sha256'] != release.evidence_sha256:
            return False
    return True


def classify_production_kind(request):
    """Classify only the journal's explicit host media kind; phase is no proof."""
    if not isinstance(request, dict):
        return None
    kind = request.get('media_kind')
    if not isinstance(kind, str):
        return None
    if kind in {'motion', 'video'}:
        return 'motion'
    if kind in {'image', 'audio', 'local_render', 'avatar'}:
        return kind
    return None


def read_video_duplicate_blocks(project_dir, shot_id):
    """Return fail-closed reasons for another video of this shot on any route.

    Private reservation acceptance dominates public statuses. Terminal release
    is read through the existing qualified-original proof, even if its debit is
    unresolved. Neither age nor a human assertion releases unknown acceptance.
    """
    from lib import openart_dispatch as dispatch, production_execution as execution
    from tools import _openart_cli as cli

    root = Path(project_dir).resolve()
    reasons = []
    private_ids = set()

    def block(attempt_id, detail):
        reasons.append(f'shot {shot_id}: original video attempt {attempt_id} {detail}')

    try:
        snapshot = read_existing_snapshot()
    except (cli.OpenArtCLIError, LedgerError, OSError, ValueError, TypeError) as exc:
        return [f'shot {shot_id}: private video reservation state cannot be verified: {exc}']

    try:
        rows = []
        for prefix, binding_type in (('', Binding), ('unpriced_', UnpricedBinding)):
            origins = {row['attempt_id']: row for row in snapshot[prefix + 'reservations']}
            # Event kind never classifies media. Both claim types need their own
            # typed reservation origin, even if the other table has this ID.
            pending = {item['attempt_id'] for key in ('claims', 'outbox')
                       for item in snapshot[prefix + key]}
            for attempt_id in sorted(pending - origins.keys()):
                block(attempt_id, 'has private pending state without a reservation origin')
                private_ids.add(attempt_id)
            rows.extend((attempt_id, row, prefix, binding_type) for attempt_id, row in origins.items())
        if len({item[0] for item in rows}) != len(rows):
            raise ValueError('attempt has conflicting typed reservation origins')
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return [f'shot {shot_id}: private video reservation state cannot be verified: {exc}']

    for attempt_id, row, prefix, binding_type in rows:
        try:
            binding = binding_type(**json.loads(row['binding_json']))
            binding.validate()
            if Path(binding.project_root).resolve() != root:
                continue
            private_ids.add(attempt_id)
            manifest, original, _ = dispatch._manifest(attempt_id)
            if (binding != original or binding.attempt_id != attempt_id
                    or binding.account_key != row['account_key']
                    or binding.authorization_occurrence != row['authorization_occurrence']
                    or (not prefix and binding.allowance_id != row['allowance_id'])):
                raise ValueError('reservation binding differs from private original')
            expected_kind = 'unknown_cost' if prefix else 'exact_credit'
            if manifest.get('authorization_kind', 'exact_credit') != expected_kind:
                raise ValueError('reservation authorization kind differs from private original')
            if prefix and (row['record_version'] != 2 or row['claim_key'] != binding.claim_key
                    or row['binding_json'] != json.dumps(asdict(binding), sort_keys=True, separators=(',', ':'))
                    or row['slot_state'] not in {'prepared', 'ready', 'submitting', 'submitted', 'uncertain',
                                                 'no-dispatch', 'terminal', 'closed'}):
                raise ValueError('invalid private unpriced reservation state')
            request = manifest['journal_records']['request.json']
            if (request.get('attempt_id') != attempt_id
                    or request.get('request_sha256') != binding.request_sha256
                    or request.get('scope', {}).get('provider') != binding.provider
                    or not request.get('shot_id') or not request.get('scope_id')
                    or request['shot_id'] != manifest['authorization']['shot_id']
                    or request['scope_id'] != manifest['authorization']['scope_id']
                    or request.get('media_kind') != 'motion'):
                raise ValueError('private request origin mismatch')
            prior_shot = request.get('shot_id')
            if not isinstance(prior_shot, str) or not prior_shot:
                raise ValueError('private request has no classifiable shot')
            if prior_shot != shot_id:
                continue
            # Exact private identity establishes other-shot independence before
            # checking this original's public readiness or acceptance state.
            # Prepared origins legitimately have no public journal.
            no_dispatch = _proved_no_dispatch(binding, row, snapshot)
            has_ready = any(item['attempt_id'] == attempt_id for item in snapshot[prefix + 'ready_journals'])
            if (row['slot_state'] != 'prepared' and not no_dispatch) or has_ready:
                dispatch._ready_journal(binding, manifest)
            kind = classify_production_kind(request)
            if kind is None:
                block(attempt_id, 'has missing or unclassifiable media kind')
            elif kind == 'motion':
                if not no_dispatch and dispatch.existing_terminal_state(root, attempt_id) is None:
                    block(attempt_id, 'has unresolved private job acceptance (' + row['slot_state'] + ')')
        except (cli.OpenArtCLIError, LedgerError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            private_ids.add(attempt_id)
            block(attempt_id, 'has unverifiable private origin: ' + str(exc))

    try:
        attempts = execution._attempts(root)
    except (execution.ProductionGovernanceError, OSError, ValueError, TypeError) as exc:
        reasons.append(f'shot {shot_id}: public video journal cannot be verified: {exc}')
        return reasons
    for request in attempts:
        if not isinstance(request, dict):
            block('<missing>', 'has a malformed public request')
            continue
        attempt_id = request.get('attempt_id', '<missing>')
        if attempt_id in private_ids:
            continue
        prior_shot = request.get('shot_id')
        if isinstance(prior_shot, str) and prior_shot and prior_shot != shot_id:
            continue
        kind = classify_production_kind(request)
        if kind is None:
            block(attempt_id, 'has missing or unclassifiable media kind')
            continue
        if kind != 'motion':
            continue
        if not isinstance(prior_shot, str) or not prior_shot:
            block(attempt_id, 'has missing shot identity')
            continue
        try:
            state = execution._state(root, request)
            if state.get('status') not in {'generated', 'failed', 'not_dispatched'}:
                block(attempt_id, 'is unresolved (' + str(state.get('status', 'unknown')) + ')')
        except (execution.ProductionGovernanceError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            block(attempt_id, 'has unverifiable job state: ' + str(exc))
    return reasons
