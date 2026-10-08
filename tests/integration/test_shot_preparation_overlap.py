"""Fixed-plan overlap characterization; all reviews and media are synthetic.

Canonical preflight runs unchanged. The reused fixture blocks network transport;
one continuity setup uses the existing mocked native attempt and selection path.
Nothing here establishes real audiovisual quality or production authorization.
"""
from __future__ import annotations

import copy
import json

import pytest

from lib.production_execution import (
    ProductionGovernanceError, approval_plan_digest, governed_dry_run,
    planned_request_digest, planned_request_template,
)
from lib.shot_contract import ASSET_PREDICATES, file_sha256
from tests.integration.test_first_pass_workflow import (
    PIXEL, attestation, production, sign_planning_reviews,
)


def persisted_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def dry_run(p, sid, *, blocked=None):
    """Prove each preflight leaves requests, journals and transport untouched."""
    before = persisted_bytes(p.root)
    requests = copy.deepcopy(p.inputs)
    native = copy.deepcopy(p.transport.native_requests)
    try:
        if blocked:
            with pytest.raises(ProductionGovernanceError, match=blocked):
                governed_dry_run(p.cli, p.inputs[sid])
        else:
            result = governed_dry_run(p.cli, p.inputs[sid])
            assert result['governed'] and result['would_execute']
            assert result['paid_submission'] is False
            assert result['provider_calls'] == result['reservations'] == 0
    finally:
        assert persisted_bytes(p.root) == before
        assert p.inputs == requests
        assert p.transport.native_requests == native
        assert p.transport.actual_provider_calls == 0
        p.network.assert_not_called()


def candidate(p):
    path = p.root / 'assets/images/candidates/interior-start.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PIXEL + b'SYNTHETIC UNBOUND CANDIDATE')
    return path


def test_missing_future_review_does_not_block_reviewed_entry(production):
    p = production
    del p.contract['shots'][1]['review']
    p.persist_contract()
    dry_run(p, 'entry')
    dry_run(p, 'interior', blocked=r'shots\.interior\.review')
    assert not list((p.root / 'production_attempts').glob('*/request.json'))
    assert not p.transport.native_requests


def test_creating_and_editing_unbound_candidate_preserves_fixed_plan(production):
    p = production
    canonical = persisted_bytes(p.root)
    request_bytes = json.dumps(p.inputs, sort_keys=True).encode()
    scope_digest = approval_plan_digest(p.contract)
    request_digest = planned_request_digest(p.inputs['entry'], project_dir=p.root)
    dry_run(p, 'entry')
    path = candidate(p)
    for content in (path.read_bytes(), PIXEL + b'SYNTHETIC REVISED CANDIDATE'):
        path.write_bytes(content)
        dry_run(p, 'entry')
        current = persisted_bytes(p.root)
        assert {name: current[name] for name in canonical} == canonical
        assert set(current) - set(canonical) == {str(path.relative_to(p.root))}
        assert json.dumps(p.inputs, sort_keys=True).encode() == request_bytes
        assert approval_plan_digest(p.contract) == scope_digest
        assert planned_request_digest(p.inputs['entry'], project_dir=p.root) == request_digest
    assert not list((p.root / 'production_attempts').glob('*/request.json'))
    assert not p.transport.native_requests


def test_promoting_unrelated_static_board_stales_original_scope(production):
    p = production
    dry_run(p, 'entry')
    scope_bytes = (p.root / 'production_scopes.json').read_bytes()
    path = candidate(p)
    board = next(asset for asset in p.contract['assets'] if asset['id'] == 'interior-start')
    board.update(path=str(path.relative_to(p.root)), sha256=file_sha256(path),
                 review=attestation(file_sha256(path), ASSET_PREDICATES))
    # Fresh synthetic reviews cannot grant immunity to a changed authored plan.
    sign_planning_reviews(p.contract)
    p.persist_contract()
    assert approval_plan_digest(p.contract) != p.scope['approval_plan_sha256']
    dry_run(p, 'entry', blocked='approval scope has a stale contract binding')
    assert (p.root / 'production_scopes.json').read_bytes() == scope_bytes


@pytest.mark.parametrize('asset_id', ['payoff-board', 'sen'])
def test_shared_payoff_and_late_cast_identity_review_remain_required(production, asset_id):
    p = production
    candidate(p)
    dry_run(p, 'entry')
    asset = next(item for item in p.contract['assets'] if item['id'] == asset_id)
    if asset_id == 'payoff-board':
        del asset['review']
        error = rf"assets\.{p.contract['assets'].index(asset)}: 'review' is a required property"
    else:
        asset['review']['predicates'] = [predicate for predicate in asset['review']['predicates']
                                         if predicate['name'] != 'cast_identity']
        error = r'assets\.sen\.review\.cast_identity: missing critical review predicate'
    p.persist_contract()
    dry_run(p, 'entry', blocked=error)


@pytest.mark.parametrize('review_owner', ['project', 'target'])
def test_project_and_target_review_remain_required(production, review_owner):
    p = production
    dry_run(p, 'entry')
    if review_owner == 'project':
        del p.contract['project_review']
        error = 'project_review'
    else:
        del p.contract['shots'][0]['review']
        error = r'shots\.entry\.review'
    p.persist_contract()
    dry_run(p, 'entry', blocked=error)


def test_changed_bound_target_bytes_block_original_exact_request(production):
    p = production
    dry_run(p, 'entry')
    path = p.root / 'assets/images/entry-start.png'
    path.write_bytes(PIXEL + b'SYNTHETIC CHANGED BOUND TARGET')
    dry_run(p, 'entry', blocked='request differs from the exact approved request')


def test_planned_board_cannot_impersonate_selected_observed_outgoing_frame(production):
    p = production
    shot = p.contract['shots'][1]
    start = next(asset for asset in p.contract['assets'] if asset['id'] == 'interior-start')
    start['upstream_source'] = {'shot_id': 'entry', 'role': 'outgoing_frame'}
    for key in ('path', 'sha256', 'review'):
        start.pop(key)
    sign_planning_reviews(p.contract)
    p.persist_contract()
    template = copy.deepcopy(p.inputs['interior'])
    template['reference_image_path'] = {'$upstream': {'shot_id': 'entry', 'role': 'outgoing_frame'}}
    p.scope['requests']['interior'] = planned_request_template(template, project_dir=p.root)
    p.scope['approval_plan_sha256'] = approval_plan_digest(p.contract)
    p.persist_scope()
    dry_run(p, 'interior', blocked='dynamic upstream request lacks matching selected bytes')
    # Reuse one offline generation solely to establish genuine journal provenance.
    result = p.generate('entry')
    assert result.success, result.error
    selected = p.select('entry', result)
    start.update(path=selected['outgoing_frame']['path'], sha256=selected['outgoing_frame']['sha256'],
                 review=attestation(selected['outgoing_frame']['sha256'], ASSET_PREDICATES))
    p.inputs['interior']['reference_image_path'] = selected['outgoing_frame']['path']
    p.bind_upstream(shot['id'])
    dry_run(p, 'interior')
    path = candidate(p)
    start.update(path=str(path.relative_to(p.root)), sha256=file_sha256(path),
                 review=attestation(file_sha256(path), ASSET_PREDICATES))
    sign_planning_reviews(p.contract)
    p.persist_contract()
    # Keep the exact approved request: only the contract claims the planned board
    # is an observed frame. Preflight must reject that false source assertion.
    dry_run(p, 'interior', blocked='path/hash differ from current selected outgoing frame')
    assert len(p.transport.native_requests) == 1
    assert len(list((p.root / 'production_attempts').glob('*/request.json'))) == 1
