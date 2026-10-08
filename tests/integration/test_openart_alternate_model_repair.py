"""Offline canonical MCP semantic repair across two approved native models.

Accounts, approvals, connector outcomes and semantic judgments are synthetic.
FFmpeg supplies real video bytes; no paid call or prior qualification is made.
"""
import copy
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from lib import openart_mcp as mcp, production_provenance as provenance
from lib import production_autonomy as pa, production_execution as execution
from lib import production_request as preparation
from tests.integration.test_openart_first_pass_workflow import real_av_clip, media_path  # noqa: F401
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401
from tests.integration.test_openart_mcp_governance import FIXTURE, install_fake_download
from tests.lib.test_production_autonomy import install_existing
from tests.lib.test_shot_contract import refresh

MODEL_A = 'pixverseV6'
MODEL_B = 'wan3-0'
ORIGINAL = 'semantic-original'
REPAIR = 'semantic-alternate'


def _read(path):
    return json.loads(path.read_text())


def _write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Keep this proof offline even if a caller opts other tests into live APIs."""
    def forbidden(*args, **kwargs):
        pytest.fail('alternate-model regression attempted a network connection')
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def _compile(root, inputs, request_id, timing):
    inputs = copy.deepcopy(inputs)
    inputs.update(compiled_request_id=request_id, preparation_review_id=request_id)
    authored = preparation.compile_provider_prompt(root, 'entry', provider='openart_mcp', model=inputs['model'])
    inputs['prompt'] = authored['prompt']
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='supported')
    assert profile['readiness']['empirical_result']['status'] == 'not_tested'
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    compiled = preparation.prepare_compiled_request(inputs, native, profile,
        coverage=authored['coverage'], timing=timing)
    review = _read(root / 'artifacts/preparation_review-original.json')
    review.update(review_id=request_id, subject_sha256=preparation.digest(compiled))
    _write(root / f'artifacts/compiled_request-{request_id}.json', compiled)
    _write(root / f'artifacts/preparation_review-{request_id}.json', review)
    preparation.validate_preparation(inputs, native, profile)
    return inputs, native, compiled


@pytest.fixture
def repair_context(lifecycle, tmp_path, monkeypatch, media_path, request):
    root, old_policy, _, inputs, _, _, jobs = lifecycle
    root = root.resolve()
    # All private discovery and account state must remain in this fixture, with
    # canonical paths even on hosts whose temporary directory is a symlink.
    for key in ('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR', 'OPENMONTAGE_OPENART_STATE_DIR'):
        private = Path(os.environ[key]).resolve()
        assert private.is_relative_to(tmp_path.resolve())
        assert private != Path('~/.openmontage/openart').expanduser().resolve()
        private.mkdir(mode=0o700, exist_ok=True)
        assert private.stat().st_mode & 0o777 == 0o700
        monkeypatch.setenv(key, str(private))
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    inputs['project_dir'] = str(root)
    inputs['output_path'] = str(root / 'clip.mp4')
    # Two seconds is native to both observed forms (Wan's minimum is two).
    inputs['native_params']['duration'] = 2
    contract_path = root / 'artifacts/shot_contract.json'
    contract = _read(contract_path)
    contract['shots'][0]['duration_seconds'] = 2
    refresh(contract)
    _write(contract_path, contract)
    for name in ('scene_plan', 'script'):
        path = root / f'artifacts/{name}.json'
        value = _read(path)
        if name == 'scene_plan':
            value['scenes'][0]['end_seconds'] = 2
        else:
            value['total_duration_seconds'] = 2
            value['sections'][0]['end_seconds'] = 2
        _write(path, value)
    timing = _read(root / 'artifacts/compiled_request-original.json')['timing']
    timing['duration_seconds'] = 2
    for window, start, end in zip(timing['action_windows'], (0, 1.2), (1.15, 1.95)):
        window.update(start_seconds=start, end_seconds=end)
    inputs, native, compiled = _compile(root, inputs, 'original', timing)
    policy = copy.deepcopy(old_policy)
    policy['caps'] = {'max_total_attempts': 2, 'max_attempts_per_shot': 2, 'max_repair_attempts': 1}
    policy['locked']['controls'] = {'duration': {'value': 2}, 'resolution': {'value': '720p'}}
    mcp_provider = next(p for p in policy['providers'] if p['id'] == 'openart_mcp')
    mcp_provider['routes'] = [{'model': model, 'mode': 'text2video'} for model in (MODEL_A, MODEL_B)]
    pool = [{'provider': 'openart_mcp', 'model': model, 'tool': 'openart_mcp_video'} for model in (MODEL_A, MODEL_B)]
    intent = {'mode': 'auto', 'approved_pool': pool}
    variant = getattr(request, 'param', 'auto')
    if variant in ('prefer', 'exact'):
        intent.update(mode=variant, provider='openart_mcp', model=MODEL_A)
    elif variant == 'outside_pool':
        intent['approved_pool'] = pool[:1]
    policy['model_selection_intents'] = {'entry': intent}
    policy, sha = install_existing(root, policy,
        templates={'entry': execution.planned_request_template(inputs, project_dir=root)})
    _write(root / 'production_scopes.json', {'version': '1.0', 'scopes': []})
    for model in (MODEL_A, MODEL_B):
        candidate = mcp.load_profile(model, 'text2video', require='candidate')
        assert jobs.qualified_profile(model, 'text2video', candidate) is None
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    inputs['governance']['scope_id'] = scope['id']
    return root, policy, sha, inputs, native, compiled, timing, jobs


def _begin(context, inputs, attempt_id, native):
    root, _, _, _, _, _, _, jobs = context
    jobs.prepare(root, attempt_id=attempt_id, generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    envelope = jobs.begin(root, attempt_id, authority_fn=execution.prepare_openart_mcp_handoff)
    assert envelope['arguments'] == native['body']
    jobs.receive(root, attempt_id, outcome={'historyId': f'fixture-{attempt_id}', 'status': 'PENDING'})
    assert jobs.poll_args(root, attempt_id)['arguments'] == {'historyId': f'fixture-{attempt_id}'}


def _complete(context, attempt_id):
    root, _, _, _, _, _, _, jobs = context
    jobs.record_status(root, attempt_id, result={'historyId': f'fixture-{attempt_id}', 'status': 'COMPLETED',
        'resources': [{'id': f'resource-{attempt_id}', 'mediaType': 'video', 'status': 'COMPLETED',
            'url': f'https://fixture.invalid/{attempt_id}.mp4'}], 'creditsCharged': 1})


def _collect(context, inputs, attempt_id, monkeypatch, tmp_path):
    root, policy, _, _, _, _, _, jobs = context
    _complete(context, attempt_id)
    downloaded = real_av_clip(tmp_path.resolve() / f'{attempt_id}.mp4', seconds=2,
        freq=440 if attempt_id == ORIGINAL else 660)
    install_fake_download(monkeypatch, jobs, downloaded)
    receipt = jobs.download_original(root, attempt_id)
    jobs.collect(root, attempt_id, downloaded_path=receipt['downloaded_path'])
    expected = {'path': inputs['output_path'], 'sha256': hashlib.sha256(downloaded.read_bytes()).hexdigest()}
    proof = provenance.validate_attempt_provenance(root, attempt_id, shot_id='entry',
        story_revision=policy['story_revision'], expected_output=expected)
    assert proof['result']['status'] == 'generated'
    assert proof['request']['transport'] == 'agent_mediated_connector'
    return expected


def _rejection(context, output, *, name='completed_end_state', status='fail', severity='critical'):
    root, policy, _, _, _, _, _, _ = context
    evidence = ('Synthetic semantic failure: the cube does not finish resting on the surface.'
        if status == 'fail' and severity == 'critical' else
        'Synthetic fixture-only review: endpoint not judged.' if status == 'unknown' else
        'Synthetic fixture-only review: slight cosmetic grain.')
    review = {'review_id': f'fixture-{name}-{status}', 'reviewer': FIXTURE,
        'story_revision': policy['story_revision'], 'subject_sha256': output['sha256'], 'status': 'fail',
        'predicates': [{'name': name, 'status': status, 'severity': severity,
            'evidence': evidence}]}
    assert execution.record_rejection(root, ORIGINAL, review) == review
    return review


def _alternate(context, *, missing_resolution=False):
    root, _, _, inputs, _, _, timing, _ = context
    candidate = copy.deepcopy(inputs)
    candidate.update(model=MODEL_B, output_path=str(root / 'alternate.mp4'))
    if missing_resolution:
        candidate['native_params'].pop('resolution')
    return _compile(root, candidate, 'alternate', timing)


def _planning_bytes(root):
    return {name: (root / f'artifacts/{name}.json').read_bytes()
        for name in ('shot_contract', 'script', 'scene_plan')}


@pytest.mark.parametrize('repair_context', ['auto', 'prefer'], indirect=True)
def test_canonical_semantic_failure_repairs_with_other_approved_native_model(
        repair_context, monkeypatch, tmp_path):
    root, policy, sha, inputs, native, compiled, _, jobs = repair_context
    planning = _planning_bytes(root)
    _begin(repair_context, inputs, ORIGINAL, native)
    original_output = _collect(repair_context, inputs, ORIGINAL, monkeypatch, tmp_path)
    assert pa.root_attempt_counts(root, policy)['total'] == 1
    _rejection(repair_context, original_output)
    original_journal = (root / f'openart_mcp/attempts/{ORIGINAL}/state.json').read_bytes()
    original_bytes = Path(original_output['path']).read_bytes()
    candidate, alternate_native, alternate_compiled = _alternate(repair_context)
    assert candidate['model'] != inputs['model']
    assert alternate_native['account_binding'] == native['account_binding']
    assert alternate_compiled['source_binding'] == compiled['source_binding']
    assert alternate_compiled['native_binding']['native_body_sha256'] != compiled['native_binding']['native_body_sha256']
    assert alternate_compiled['request_sha256'] != compiled['request_sha256']
    repair = pa.derive_scope(root, candidate, provider='openart_mcp', phase='repair',
        replaces_attempt_ids=[ORIGINAL])
    assert repair['phase'] == 'repair' and repair['replaces_attempt_ids'] == [ORIGINAL]
    candidate['governance']['scope_id'] = repair['id']
    _begin(repair_context, candidate, REPAIR, alternate_native)
    repaired_output = _collect(repair_context, candidate, REPAIR, monkeypatch, tmp_path)
    for aid, model in ((ORIGINAL, MODEL_A), (REPAIR, MODEL_B)):
        frozen = jobs.frozen_request(root, aid)
        assert frozen['native']['model'] == model
        assert frozen['authority']['billing']['policy_sha256'] == sha
        assert frozen['authority']['billing']['enforceable_credit_ceiling'] is False
        profile = mcp.load_profile(model, 'text2video', require='supported')
        assert profile['readiness']['empirical_result']['status'] == 'not_tested'
        assert jobs.qualified_profile(model, 'text2video', profile) is None
    assert Path(original_output['path']).read_bytes() == original_bytes
    assert (root / f'openart_mcp/attempts/{ORIGINAL}/state.json').read_bytes() == original_journal
    assert repaired_output['sha256'] != original_output['sha256']
    assert _planning_bytes(root) == planning
    assert not list(Path(os.environ['OPENMONTAGE_OPENART_STATE_DIR']).glob('mcp*qualifications/*.json'))
    counts = pa.root_attempt_counts(root, policy)
    assert counts['total'] == 2 and counts['repair'] == 1
    with pytest.raises(ValueError, match='total attempts'):
        pa.derive_scope(root, candidate, provider='openart_mcp', phase='repair', replaces_attempt_ids=[ORIGINAL])
    repair_only_cap = copy.deepcopy(policy)
    repair_only_cap['caps'].update(max_total_attempts=99, max_attempts_per_shot=99)
    with pytest.raises(ValueError, match='repair attempts'):
        pa.check_caps(repair_only_cap, 'entry', counts, 'repair')


@pytest.mark.parametrize('repair_context', ['exact', 'outside_pool'], indirect=True)
def test_semantic_failure_does_not_expand_exact_intent_or_approved_pool(
        repair_context, monkeypatch, tmp_path):
    root, policy, _, inputs, native, _, _, jobs = repair_context
    _begin(repair_context, inputs, ORIGINAL, native)
    output = _collect(repair_context, inputs, ORIGINAL, monkeypatch, tmp_path)
    _rejection(repair_context, output)
    candidate, _, _ = _alternate(repair_context)
    with pytest.raises(ValueError, match='model selection intent'):
        pa.derive_scope(root, candidate, provider='openart_mcp', phase='repair', replaces_attempt_ids=[ORIGINAL])
    assert [row['attempt_id'] for row in jobs.list_attempts(root)] == [ORIGINAL]
    assert pa.root_attempt_counts(root, policy)['total'] == 1


@pytest.mark.parametrize('predicate', [
    {'name': 'completed_end_state', 'status': 'unknown', 'severity': 'critical'},
    {'name': 'grain', 'status': 'fail', 'severity': 'cosmetic'},
])
def test_unknown_or_cosmetic_review_does_not_grant_semantic_repair(
        repair_context, monkeypatch, tmp_path, predicate):
    root, policy, _, inputs, native, _, _, jobs = repair_context
    _begin(repair_context, inputs, ORIGINAL, native)
    output = _collect(repair_context, inputs, ORIGINAL, monkeypatch, tmp_path)
    _rejection(repair_context, output, **predicate)
    candidate, _, _ = _alternate(repair_context)
    with pytest.raises(ValueError, match='named failed critical'):
        pa.derive_scope(root, candidate, provider='openart_mcp', phase='repair', replaces_attempt_ids=[ORIGINAL])
    assert [row['attempt_id'] for row in jobs.list_attempts(root)] == [ORIGINAL]
    assert pa.root_attempt_counts(root, policy)['total'] == 1


def test_alternate_missing_required_native_control_blocks_before_begin(repair_context):
    root, policy, _, _, _, _, _, jobs = repair_context
    candidate, native, _ = _alternate(repair_context, missing_resolution=True)
    profile = mcp.load_profile(MODEL_B, 'text2video', require='supported')
    assert 'resolution' not in native['body']['params']
    with pytest.raises(ValueError, match='native locked control absent: resolution'):
        pa.lock_proof(root, 'entry', inputs=candidate, native=native, profile=profile)
    with pytest.raises(ValueError, match='resolution changed outside declared flex'):
        pa.derive_scope(root, candidate, provider='openart_mcp')
    assert jobs.list_attempts(root) == []
    assert pa.root_attempt_counts(root, policy)['repair'] == 0


@pytest.mark.parametrize('invalid_original', ['wrong_output_sha', 'uncollected', 'tampered_output'])
def test_canonical_rejection_requires_collected_current_original(
        repair_context, monkeypatch, tmp_path, invalid_original):
    root, _, _, inputs, native, _, _, _ = repair_context
    _begin(repair_context, inputs, ORIGINAL, native)
    if invalid_original == 'uncollected':
        _complete(repair_context, ORIGINAL)
        output = {'path': inputs['output_path'], 'sha256': 'f' * 64}
    else:
        output = _collect(repair_context, inputs, ORIGINAL, monkeypatch, tmp_path)
        if invalid_original == 'wrong_output_sha':
            output['sha256'] = 'f' * 64
        else:
            path = Path(output['path'])
            path.write_bytes(path.read_bytes() + b'synthetic changed output')
    with pytest.raises(ValueError):
        _rejection(repair_context, output)
    assert not list((root / f'openart_mcp/attempts/{ORIGINAL}/rejections').glob('*.json'))


@pytest.mark.parametrize('outcome', ['pending', 'uncertain'])
def test_unresolved_original_prevents_alternate_begin(repair_context, outcome):
    root, policy, _, inputs, native, _, _, jobs = repair_context
    jobs.prepare(root, attempt_id=ORIGINAL, generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, ORIGINAL, authority_fn=execution.prepare_openart_mcp_handoff)
    if outcome == 'pending':
        jobs.receive(root, ORIGINAL, outcome={'historyId': 'fixture-pending', 'status': 'PENDING'})
    else:
        jobs.receive(root, ORIGINAL, error='Synthetic interrupted handoff with no original receipt.')
    candidate, _, _ = _alternate(repair_context)
    with pytest.raises(ValueError, match='pending|uncertain|unresolved'):
        pa.derive_scope(root, candidate, provider='openart_mcp', phase='repair', replaces_attempt_ids=[ORIGINAL])
    # The canonical handoff cannot bypass rooted derivation with the old scope.
    with pytest.raises(ValueError):
        jobs.prepare(root, attempt_id=REPAIR, generation_inputs=candidate,
            authority_fn=execution.prepare_openart_mcp_handoff)
    assert [row['attempt_id'] for row in jobs.list_attempts(root)] == [ORIGINAL]
    assert jobs.attempt_state(root, ORIGINAL)['status'] == ('submitted' if outcome == 'pending' else 'uncertain')
    assert pa.root_attempt_counts(root, policy)['total'] == 1
