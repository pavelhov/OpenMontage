"""Rooted autonomy cap and shared OpenArt allowance boundary proofs.

The imported fixture uses a local fake CLI and synthetic AV/provider evidence.
It exercises the normal activation, preparation, dispatch, and credit paths; it
does not establish real provider or media quality claims.
"""
import json

import pytest

from lib import production_autonomy as autonomy
from tests.lib.test_production_autonomy_report import openart_project


def _retained_scope(root, scope_id):
    scopes = json.loads((root / 'production_scopes.json').read_text())['scopes']
    return next(scope for scope in scopes if scope['id'] == scope_id)


def test_unlaunched_rooted_scope_counts_once(openart_project):
    root, inputs, _, _, policy, _ = openart_project
    scope_id = inputs['governance']['scope_id']
    scope = _retained_scope(root, scope_id)
    assert 'derived_from_policy' in scope
    counts = autonomy.root_attempt_counts(root, policy)
    assert counts == {'total': 1, 'per_shot': {'entry': 1}, 'repair': 0, 'unmapped_outbox': []}
    assert not list((root / 'production_attempts').glob('*/request.json'))


def test_ledger_prepared_outbox_is_counted_once_and_blocks_same_shot_route(openart_project):
    from lib import openart_dispatch as dispatch
    from tools.video.openart_cli_video import OpenArtCLIVideo

    root, inputs, _, _, policy, _ = openart_project
    before = autonomy.root_attempt_counts(root, policy)
    assert before['total'] == 1  # the retained, unlaunched derived singleton
    observed = []

    def crash(stage, attempt_id):
        if stage == 'ledger_prepared':
            observed.append(attempt_id)
            raise RuntimeError('synthetic ledger_prepared boundary')

    dispatch._CRASH_HOOK = crash
    try:
        with pytest.raises(RuntimeError, match='synthetic ledger_prepared'):
            OpenArtCLIVideo().execute(inputs)
    finally:
        dispatch._CRASH_HOOK = None

    assert len(observed) == 1
    attempt_id = observed[0]
    assert not (root / 'production_attempts' / attempt_id / 'request.json').exists()
    from lib.provider_credit_ledger import read_existing_snapshot
    snapshot = read_existing_snapshot()
    assert any(row['attempt_id'] == attempt_id for row in snapshot['reservations'])
    counts = autonomy.root_attempt_counts(root, policy)
    assert counts == {'total': 1, 'per_shot': {'entry': 1}, 'repair': 0, 'unmapped_outbox': []}
    with pytest.raises(autonomy.AutonomyError):
        autonomy.derive_scope(root, inputs, provider='openart_cli')


def test_changed_retained_authorization_cannot_enlarge_shared_policy_allowance(openart_project):
    import hashlib

    from lib import openart_credit as credit
    from lib import production_execution as execution

    root, inputs, _, _, policy, _ = openart_project
    scope_id = inputs['governance']['scope_id']
    scope = _retained_scope(root, scope_id)
    auth_path = root / 'artifacts' / f'credit_authorization-{scope_id}.json'
    authorization = json.loads(auth_path.read_text())
    approval_path = root / authorization['evidence']['path']
    original_allowance = authorization['allowance']
    assert authorization['allowance_id'] == autonomy.openart_allowance_id(
        autonomy.require_active_policy(root)[1])
    assert original_allowance == next(p['ceiling'] for p in policy['providers'] if p['id'] == 'openart_cli')

    # Negative corruption probe: retain the same real captured authorization,
    # but alter its allowance and consistently rebind its evidence and scope
    # digest. The rooted validator must still enforce the active shared policy.
    enlarged = dict(authorization)
    enlarged['allowance'] = '999'
    terms = {key: value for key, value in enlarged.items() if key != 'evidence'}
    raw = json.dumps({'kind': 'openart_credit_authorization', 'terms': terms},
                     sort_keys=True, separators=(',', ':')).encode()
    approval_path.write_bytes(raw)
    enlarged['evidence'] = dict(authorization['evidence'], sha256=hashlib.sha256(raw).hexdigest())
    auth_path.write_text(json.dumps(enlarged, indent=2))
    scope['credit_authorization_sha256'] = credit.credit_authorization_digest(enlarged)
    scopes_path = root / 'production_scopes.json'
    scopes_doc = json.loads(scopes_path.read_text())
    scopes_doc['scopes'] = [scope if item['id'] == scope_id else item for item in scopes_doc['scopes']]
    scopes_path.write_text(json.dumps(scopes_doc, indent=2))

    with pytest.raises(autonomy.AutonomyError, match='exact policy allowance/count/purpose'):
        autonomy.validate_derived_scope(root, scope, inputs=inputs)


def test_distinct_authorization_digests_share_one_ledger_allowance(tmp_path):
    from dataclasses import replace

    from lib.provider_credit_ledger import LedgerError
    from tests.lib.test_provider_credit_ledger import ledger, packet, ready, terminal

    store = ledger(tmp_path)
    first = packet(quote='2.25', allowance='4')
    first = replace(first, binding=replace(first.binding, authorization_sha256='a' * 64))
    store.reserve_prepared(first)
    ready(store, first)
    terminal(store, first)
    assert store.inspect(first.binding.attempt_id)['debit_state'] == 'unresolved'

    over_budget = packet(quote='2.25', allowance='4')
    over_budget = replace(over_budget, binding=replace(over_budget.binding, authorization_sha256='b' * 64))
    assert over_budget.binding.authorization_sha256 != first.binding.authorization_sha256
    with pytest.raises(LedgerError, match='allowance'):
        store.reserve_prepared(over_budget)

    fits_remaining = packet(quote='1', allowance='4')
    fits_remaining = replace(fits_remaining, binding=replace(fits_remaining.binding,
                                                             authorization_sha256='c' * 64))
    store.reserve_prepared(fits_remaining)
    assert store.inspect(fits_remaining.binding.attempt_id)['reserved_units'] == 4
