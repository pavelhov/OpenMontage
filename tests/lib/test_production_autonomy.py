"""Rooted Auto-continue authority tests. No CLI, network or real provider calls."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from lib import production_autonomy as pa
from lib.production_request import digest

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'first_pass' / 'valid_shot_contract.json'
H = lambda s: hashlib.sha256(s.encode()).hexdigest()
SCENE_PLAN = {'version': '1.0', 'scenes': [{'id': 'entry', 'type': 'generated', 'description': 'Offline patient entry', 'script_section_id': 's1', 'start_seconds': 0, 'end_seconds': 8}]}
SCRIPT = {'version': '1.0', 'title': 'Offline fixture', 'total_duration_seconds': 8, 'sections': [{'id': 's1', 'text': 'Help me. Help me.', 'start_seconds': 0, 'end_seconds': 8}]}


def projection():
    contract = json.loads(FIXTURE.read_text())
    return pa.static_planning_projection(contract, SCENE_PLAN, SCRIPT, 'entry')


def fixture_planning(root):
    root = Path(root)
    (root / 'artifacts').mkdir(parents=True, exist_ok=True)
    (root / 'assets').mkdir(exist_ok=True)
    contract = json.loads(FIXTURE.read_text())
    for asset in contract['assets']:
        raw = ('offline synthetic asset ' + asset['id']).encode()
        (root / asset['path']).write_bytes(raw)
        asset['sha256'] = hashlib.sha256(raw).hexdigest()
        asset['review']['subject_sha256'] = asset['sha256']
    for name, value in [('shot_contract', contract), ('script', SCRIPT), ('scene_plan', SCENE_PLAN)]:
        (root / 'artifacts' / (name + '.json')).write_text(json.dumps(value))
    return contract


def retained_fixture(root):
    root = Path(root)
    proj = pa.current_projection(root, 'entry')
    from lib.production_execution import planned_request_template
    template = planned_request_template({'duration': 8, 'image_path': str(root / 'assets/start.svg')}, project_dir=root)
    from lib.production_execution import approval_plan_digest
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    return {'entry': {'projection': proj, 'planned_request_template': template,
                      'script_snapshot': copy.deepcopy(SCRIPT), 'dialogue_coverage': pa.dialogue_occurrences(proj),
                      'approval_plan_sha256': approval_plan_digest(contract)}}


def test_unknown_cost_input_refused_before_policy_read(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, 'require_active_policy', lambda _: pytest.fail('policy read before refusal'))
    with pytest.raises(pa.AutonomyError, match='unknown-cost Auto-continue is unavailable'):
        pa._scope_material(tmp_path, {'unknown_cost_authorization_id': 'one'}, 'openart_cli')


def test_unknown_cost_template_cannot_activate_policy(tmp_path):
    fixture_planning(tmp_path)
    retained = retained_fixture(tmp_path)['entry']
    retained['planned_request_template']['inputs']['unknown_cost_authorization_id'] = 'one'
    with pytest.raises(pa.AutonomyError, match='unknown-cost Auto-continue is unavailable'):
        pa.baseline_descriptor(retained, tmp_path)


def make_policy(root=None, **over):
    # Descriptors are finalized from real retained planning by install(); root allows direct use.
    if root is not None:
        fixture_planning(root)
        baseline = pa.baseline_descriptor(retained_fixture(root)['entry'], root)
    else:
        baseline = {'planned_request_template_sha256': H('pending'), 'static_input_assets': [],
                    'upstream': [], 'approval_plan_digest': H('pending'), 'script_snapshot_sha256': H('pending'),
                    'dialogue_coverage_sha256': H('pending'), 'static_source_packet_sha256': H('pending')}
    policy = {'version': '1.0', 'policy_id': 'p1', 'mode': 'auto_continue', 'project_id': 'offline-first-pass',
              'story_revision': 'story-1',
              'providers': [{'id': 'openart_cli', 'models': ['kling-3'], 'billing': 'credits', 'ceiling': '40',
                             'account_id_sha256': H('acct'), 'workspace': 'ws'},
                            {'id': 'grok_cli', 'model_policy': 'cli_managed_media_unreported',
                             'billing': 'subscription_quota_unknown'}],
              'caps': {'max_total_attempts': 3, 'max_attempts_per_shot': 2, 'max_repair_attempts': 1},
              'shots': {'entry': baseline},
              'locked': {'cast': {'patient': [{'sha256': H('start'), 'role': 'start_frame'}]}, 'dialogue': {},
                         'sources': [], 'story_predicates': [], 'controls': {}},
              'flex': {'duration_s': [6, 10], 'resolution': [], 'references': {'droppable_roles': [], 'substitutes': []}},
              'checkpoint_stages': ['script'],
              'evidence': {'path': 'approvals/x.json', 'sha256': '0' * 64}}
    policy.update(over)
    return policy


def install_existing(root, policy, *, templates, selected=None, approved=True, marker=None):
    """Retain genuine current planning without replacing existing production fixtures."""
    root = Path(root)
    from lib.production_execution import approval_plan_digest, _artifact_path
    contract = json.loads(_artifact_path(root, 'shot_contract.json').read_text())
    script = json.loads(_artifact_path(root, 'script.json').read_text())
    baselines = {}
    for shot_id, template in templates.items():
        proj = pa.current_projection(root, shot_id)
        baselines[shot_id] = {'projection': proj, 'planned_request_template': copy.deepcopy(template),
            'script_snapshot': copy.deepcopy(script), 'dialogue_coverage': pa.dialogue_occurrences(proj),
            'approval_plan_sha256': approval_plan_digest(contract)}
    policy = copy.deepcopy(policy)
    policy['project_id'] = contract['project_id']
    policy['story_revision'] = contract['story_revision']
    policy['shots'] = {key: pa.baseline_descriptor(value, root) for key, value in baselines.items()}
    content = pa.evidence_content(policy, baselines)
    raw = json.dumps(content, sort_keys=True).encode()
    sha = digest(content)
    rel = f'approvals/autonomy-policy-{sha}.json'
    policy['evidence'] = {'path': rel, 'sha256': hashlib.sha256(raw).hexdigest()}
    (root / 'approvals').mkdir(exist_ok=True)
    (root / rel).write_bytes(raw)
    (root / 'artifacts/autonomy_policy.json').write_text(json.dumps(policy))
    (root / 'project.json').write_text(json.dumps(marker or (json.loads((root / 'project.json').read_text()) if (root / 'project.json').exists() else {
        'project_id': 'offline-first-pass', 'story_revision': 'story-1', 'pipeline_type': 'framework-smoke',
        'governance': {'mode': 'strict', 'version': '1.0'}})))
    choice = selected or f'auto_continue:{sha}'
    log = {'version': '1.0', 'project_id': policy['project_id'], 'decisions': [{
        'decision_id': 'd-act', 'stage': 'proposal', 'category': 'approval_policy',
        'subject': pa.ACTIVATION_SUBJECT, 'selected': choice, 'user_approved': approved,
        'reason': 'Offline synthetic retained human approval', 'options_considered': [{
            'option_id': choice, 'label': 'Auto-continue', 'score': 1, 'reason': 'Offline test choice'}]}]}
    (root / 'artifacts/decision_log.json').write_text(json.dumps(log))
    return policy, sha


def install(root, policy, *, selected=None, approved=True, marker=None):
    root = Path(root)
    fixture_planning(root)
    template = retained_fixture(root)['entry']['planned_request_template']
    return install_existing(root, policy, templates={'entry': template}, selected=selected,
                            approved=approved, marker=marker)


def snapshot(root):
    return {p: p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}


def test_active_policy_loads_and_reader_writes_nothing(tmp_path):
    _, sha = install(tmp_path, make_policy())
    before = snapshot(tmp_path)
    policy, got, decision_id, reasons = pa.load_active_policy(tmp_path)
    assert reasons == [] and got == sha and decision_id == 'd-act' and policy['policy_id'] == 'p1'
    assert snapshot(tmp_path) == before


def test_missing_policy_is_strict(tmp_path):
    assert pa.load_active_policy(tmp_path)[0] is None


@pytest.mark.parametrize('case', ['stale_selected', 'not_user', 'revoked', 'tampered', 'story', 'benchmark', 'schema'])
def test_invalid_policy_is_strict(tmp_path, case):
    policy, sha = install(tmp_path, make_policy(), selected='auto_continue:' + '1' * 64 if case == 'stale_selected' else None,
                          approved=case != 'not_user')
    if case == 'revoked':
        log_path = tmp_path / 'artifacts' / 'decision_log.json'
        log = json.loads(log_path.read_text())
        log['decisions'].append({'id': 'd2', 'category': 'approval_policy', 'subject': pa.ACTIVATION_SUBJECT,
                                 'selected': 'strict', 'user_approved': True})
        log_path.write_text(json.dumps(log))
    if case == 'tampered':
        (tmp_path / policy['evidence']['path']).write_bytes(b'{}')
    if case == 'story':
        (tmp_path / 'project.json').write_text(json.dumps({'project_id': 'offline-first-pass', 'story_revision': 'story-2',
                                                           'governance': {'mode': 'strict', 'version': '1.0'}}))
    if case == 'benchmark':
        (tmp_path / 'provider_benchmark').mkdir()
    if case == 'schema':
        (tmp_path / 'artifacts' / 'autonomy_policy.json').write_text(json.dumps(dict(policy, extra=1)))
    assert pa.load_active_policy(tmp_path)[0] is None
    with pytest.raises(pa.AutonomyError):
        pa.require_active_policy(tmp_path)


def test_caller_policy_dict_is_not_authority(tmp_path):
    policy, _ = install(tmp_path, make_policy())
    edited = copy.deepcopy(policy)
    edited['caps']['max_total_attempts'] = 99
    (tmp_path / 'artifacts' / 'autonomy_policy.json').write_text(json.dumps(edited))
    assert pa.load_active_policy(tmp_path)[0] is None


def test_conflicts_invalidate_whole_policy_but_model_lock_does_not():
    p = make_policy()
    p['locked']['controls'] = {'duration': {'value': 8}}
    assert pa.policy_conflicts(p)
    p = make_policy()
    p['flex']['references']['substitutes'] = [{'sha256': H('start'), 'role': 'start_frame'}]
    assert pa.policy_conflicts(p)
    p = make_policy()
    p['flex']['references']['droppable_roles'] = ['start_frame']
    assert pa.policy_conflicts(p)
    p = make_policy()
    p['locked']['controls'] = {'model': {'value': 'kling-3'}}
    assert pa.policy_conflicts(p) == []


def test_projection_excludes_reviews_and_retime_keeps_text():
    proj = projection()
    assert 'review' not in proj['shot'] and all('review' not in a for a in proj['assets'])
    out = pa.retime(proj, 10)
    assert out['shot']['duration_seconds'] == 10 and out['scene']['end_seconds'] == 10
    assert out['script_section']['text'] == proj['script_section']['text'] and out['story'] == proj['story']
    assert out['shot']['completed_end_state'] == proj['shot']['completed_end_state']


def test_retime_never_fabricates_fit():
    proj = projection()
    proj['shot']['dialogue'] = [{'id': 'l1', 'speaker_id': 'patient', 'text': 'Help', 'start_seconds': 0,
                                 'end_seconds': 4, 'measured_duration_seconds': 4}]
    assert pa.retime(proj, 10)['shot']['dialogue'][0]['end_seconds'] == 5
    with pytest.raises(pa.AutonomyError):
        pa.retime(proj, 6)


def test_contract_delta_only_allows_flexed_retiming(tmp_path):
    install(tmp_path, make_policy())
    proj = pa.current_projection(tmp_path, 'entry')
    assert pa.contract_delta(tmp_path, 'entry', proj) == (None, [])
    duration, paths = pa.contract_delta(tmp_path, 'entry', pa.retime(proj, 6))
    assert duration == 6 and '/shot/duration_seconds' in paths
    with pytest.raises(pa.AutonomyError):
        pa.contract_delta(tmp_path, 'entry', pa.retime(proj, 7))
    edited = copy.deepcopy(proj)
    edited['shot']['dominant_action'] = 'Patient flies.'
    with pytest.raises(pa.AutonomyError):
        pa.contract_delta(tmp_path, 'entry', edited)
    with pytest.raises(TypeError):
        pa.contract_delta(tmp_path, 'entry', edited, edited)


def openart_row(**model_over):
    model = {'model': 'kling-3', 'mode': 'i2v', 'level': 'full', 'profile_sha256': H('prof'),
             'account_id_sha256': H('acct'), 'catalog_verified': True,
             'controls': {'native_controls': {'resolution': {
                 'observed_in_form': True, 'qualified_in_exact_preview': True,
                 'preview': {'value': '720p', 'value_sha256': H('720p')}, 'required': False,
                 'enum': [{'value': '720p'}, {'value_redacted': True, 'value_sha256': H('1080p')}]}},
                 'required_unqualified': []}}
    model.update(model_over)
    return {'provider': 'openart_cli', 'status': 'available', 'production_available': True,
            'dispatch_readiness': {'ready': True, 'blockers': []}, 'models': [model],
            'qualification_candidates': [], 'not_live': []}


GROK_ROW = {'provider': 'grok_cli', 'generation_tool': 'grok_cli_video', 'status': 'available', 'models': [],
            'model_policy': 'cli_managed_media_unreported', 'model_selection': 'not_supported',
            'billing_kind': 'subscription_quota_unknown'}


def test_eligible_routes_actual_menu_shape():
    p = make_policy()
    ok, _ = pa.eligible_routes(p, 'entry', [openart_row(), GROK_ROW])
    assert {r['provider'] for r in ok} == {'openart_cli', 'grok_cli'}
    assert next(r for r in ok if r['provider'] == 'grok_cli')['model'] is None


def test_model_lock_excludes_only_grok():
    p = make_policy()
    p['locked']['controls'] = {'model': {'value': 'kling-3'}}
    ok, bad = pa.eligible_routes(p, 'entry', [openart_row(), GROK_ROW])
    assert [r['provider'] for r in ok] == ['openart_cli']
    assert any(b['provider'] == 'grok_cli' and 'media-model' in b['reason'] for b in bad)


def test_redacted_enum_never_supports_lock():
    p = make_policy()
    p['locked']['controls'] = {'resolution': {'value': '1080p'}}
    ok, _ = pa.eligible_routes(p, 'entry', [openart_row()])
    assert ok == []
    p['locked']['controls'] = {'resolution': {'value': '720p'}}
    assert len(pa.eligible_routes(p, 'entry', [openart_row()])[0]) == 1


@pytest.mark.parametrize('row', [
    dict(openart_row(), dispatch_readiness={'ready': False, 'blockers': ['refresh']}),
    dict(openart_row(), production_available=False),
    openart_row(account_id_sha256=H('other')),
    openart_row(level='partial'),
    dict(openart_row(), qualification_candidates=[{'model': 'kling-3'}]),
])
def test_openart_route_exclusions(row):
    assert pa.eligible_routes(make_policy(), 'entry', [row])[0] == []


def test_unverified_native_controls_block_locks():
    p = make_policy()
    p['locked']['controls'] = {'resolution': {'value': '720p'}}
    row = openart_row(controls={'native_controls': 'unverified_receipts'})
    assert pa.eligible_routes(p, 'entry', [row])[0] == []


def test_caps_count_every_state_and_fail_closed_on_outbox():
    p = make_policy()
    attempts = [{'attempt_id': 'a1', 'shot_id': 'entry', 'state': 'failed'}]
    counts = pa.attempt_counts(attempts)
    pa.check_caps(p, 'entry', counts, 'first_pass')
    counts = pa.attempt_counts(attempts, open_scope_attempts=[{'attempt_id': 'a2', 'shot_id': 'entry'}])
    with pytest.raises(pa.AutonomyError):
        pa.check_caps(p, 'entry', counts, 'first_pass')
    with pytest.raises(pa.AutonomyError):
        pa.check_caps(p, 'entry', pa.attempt_counts(attempts, outbox_attempt_ids=['zz']), 'first_pass')
    p['caps']['max_repair_attempts'] = 0
    with pytest.raises(pa.AutonomyError):
        pa.check_caps(p, 'entry', pa.attempt_counts([]), 'repair')


def test_derived_scope_shape_and_reload_validation(tmp_path):
    policy, sha = install(tmp_path, make_policy())
    kw = dict(shot_id='entry', provider='openart_cli', request_digest=H('req'), approval_plan_sha256=H('plan'),
              derivation_index=0, lock_digest=H('lock'))
    scope = pa._scope_record(policy, sha, 'd-act', **kw)
    assert scope['approved_by'] == f'policy:{sha}' and scope['attempts_per_shot'] == {'entry': 1}
    assert scope['credit_terms']['allowance_id'] == f'policy:{sha}:openart'
    with pytest.raises(TypeError):
        pa.validate_derived_scope(tmp_path, scope, **kw)
    with pytest.raises(pa.AutonomyError):
        pa.validate_derived_scope(tmp_path, dict(scope, derived_from_policy=None), inputs={})
    assert pa.validate_derived_scope(tmp_path, {'id': 'human'}, inputs={}) is None
    grok = pa._scope_record(policy, sha, 'd-act', **dict(kw, provider='grok_cli'))
    assert 'credit_terms' not in grok


def write_stage(root, critical=()):
    art = root / 'artifacts' / 'script.json'
    assert art.is_file()
    art_sha = hashlib.sha256(art.read_bytes()).hexdigest()
    review = root / 'artifacts' / 'script_review.json'
    review.write_text(json.dumps({'stage': 'script', 'artifact_sha256': art_sha, 'critical_findings': list(critical)}))
    return {'artifact_path': 'artifacts/script.json', 'artifact_sha256': art_sha, 'review_path': 'artifacts/script_review.json',
            'review_sha256': hashlib.sha256(review.read_bytes()).hexdigest(), 'critical_findings': []}


def test_preauth_current_bytes_and_findings(tmp_path):
    _, sha = install(tmp_path, make_policy())
    basis = {'kind': 'policy', 'policy_sha256': sha, 'decision_id': 'd-act'}
    pre = write_stage(tmp_path)
    before = snapshot(tmp_path)
    assert pa.validate_preauth(tmp_path, 'script', basis, pre) == sha
    assert snapshot(tmp_path) == before
    for stage in ('publish', 'compose'):
        with pytest.raises(pa.AutonomyError):
            pa.validate_preauth(tmp_path, stage, basis, pre)
    (tmp_path / 'artifacts' / 'script.json').write_text('{"sections": [1]}')
    with pytest.raises(pa.AutonomyError):
        pa.validate_preauth(tmp_path, 'script', basis, pre)
    pre = write_stage(tmp_path, critical=['speaker unknown'])
    with pytest.raises(pa.AutonomyError):
        pa.validate_preauth(tmp_path, 'script', basis, pre)


def test_writers_append_and_never_overwrite(tmp_path):
    _, sha = install(tmp_path, make_policy())
    data = pa.append_decisions(tmp_path, sha, 'd-act', [{'category': 'provider_selection', 'subject': 'entry',
                                                         'selected': 'openart_cli', 'reason': 'eligible'}])
    assert data['decisions'][0]['decision_id'] == 'd-act' and data['decisions'][-1]['user_approved'] is False
    assert f'policy:{sha}' in data['decisions'][-1]['reason']
    assert pa.require_active_policy(tmp_path)[1] == sha
    with pytest.raises(TypeError):
        pa.completion_report(tmp_path, sha, openart_credits_used='12', grok_attempts=2, shots={})


def test_r18_delegates_to_guard(monkeypatch):
    import lib.production_video_guard as guard
    calls = []
    monkeypatch.setattr(guard, 'read_video_duplicate_blocks', lambda root, shot: calls.append((root, shot)) or ['busy'])
    assert pa.cross_provider_block('/p', 'entry') == ['busy'] and calls == [('/p', 'entry')]
    assert pa.production_kind({'media_kind': 'video'}) == 'motion'
    assert pa.production_kind({}) is None


@pytest.mark.parametrize('mutation', ['late_cast', 'payoff_speaker', 'payoff_asset', 'identity', 'order', 'story', 'template_descriptor'])
def test_real_retained_baseline_rejects_drift(tmp_path, mutation):
    policy, _ = install(tmp_path, make_policy())
    assert pa.require_active_policy(tmp_path)
    path = tmp_path / 'artifacts/shot_contract.json'
    data = json.loads(path.read_text())
    if mutation == 'late_cast':
        data['late_cast_ids'] = []
    elif mutation == 'payoff_speaker':
        data['payoff_speaker_ids'] = []
    elif mutation == 'payoff_asset':
        data['payoff_asset_id'] = 'end'
    elif mutation == 'identity':
        data['assets'][-1]['sha256'] = H('changed identity')
    elif mutation == 'order':
        data['shots'].append(dict(data['shots'][0], id='other'))
    elif mutation == 'story':
        data['story']['payoff'] = 'Changed story'
    elif mutation == 'template_descriptor':
        path = tmp_path / 'artifacts/autonomy_policy.json'
        data = policy
        data['shots']['entry']['planned_request_template_sha256'] = H('forged')
    path.write_text(json.dumps(data))
    assert pa.load_active_policy(tmp_path)[0] is None


@pytest.mark.parametrize('mutation', ['schema', 'project', 'duplicate', 'conflict', 'revocation'])
def test_real_decision_history_fails_closed(tmp_path, mutation):
    install(tmp_path, make_policy())
    assert pa.require_active_policy(tmp_path)
    path = tmp_path / 'artifacts/decision_log.json'
    log = json.loads(path.read_text())
    if mutation == 'schema':
        del log['decisions'][0]['options_considered']
    elif mutation == 'project':
        log['project_id'] = 'other'
    elif mutation == 'duplicate':
        log['decisions'].append(copy.deepcopy(log['decisions'][0]))
    elif mutation == 'conflict':
        (tmp_path / 'decision_log.json').write_text('{}')
    else:
        revoke = copy.deepcopy(log['decisions'][0])
        revoke.update(decision_id='revoke', selected='strict')
        log['decisions'].append(revoke)
    path.write_text(json.dumps(log))
    assert pa.load_active_policy(tmp_path)[0] is None


def test_equal_legacy_decision_alias_and_provider_only_policy(tmp_path):
    install(tmp_path, make_policy(checkpoint_stages=[]))
    path = tmp_path / 'artifacts/decision_log.json'
    (tmp_path / 'decision_log.json').write_bytes(path.read_bytes())
    assert pa.require_active_policy(tmp_path)


def test_duplicate_provider_ids_reject_entire_policy(tmp_path):
    p = make_policy()
    p['providers'] = [p['providers'][0], copy.deepcopy(p['providers'][0])]
    install(tmp_path, p)
    assert pa.load_active_policy(tmp_path)[0] is None


def test_full_script_snapshot_drift_rejected(tmp_path):
    install(tmp_path, make_policy())
    assert pa.require_active_policy(tmp_path)
    path = tmp_path / 'artifacts/script.json'
    script = json.loads(path.read_text())
    script['title'] = 'Changed outside approved flex'
    path.write_text(json.dumps(script))
    assert pa.load_active_policy(tmp_path)[0] is None


def test_actual_static_bytes_changed_rejects_policy(tmp_path):
    install(tmp_path, make_policy())
    assert pa.require_active_policy(tmp_path)
    (tmp_path / 'assets/patient.svg').write_text('changed bytes, unchanged contract hash')
    assert pa.load_active_policy(tmp_path)[0] is None


def test_lock_proof_rejects_caller_authority_interface(tmp_path):
    install(tmp_path, make_policy())
    assert pa.require_active_policy(tmp_path)
    with pytest.raises(TypeError):
        pa.lock_proof(tmp_path, 'entry', frozen_inputs=[], native_args={}, coverage=[], cast_ids=[])


def reference_policy(root):
    from lib import production_execution as execution
    contract = fixture_planning(root)
    for ident in ('voice-a', 'voice-b'):
        asset = copy.deepcopy(contract['assets'][0])
        asset.update(id=ident, role='voice_reference', cast_ids=[], path=f'assets/{ident}.bin', sha256=H(ident))
        (root / asset['path']).write_text(ident)
        asset['review']['subject_sha256'] = asset['sha256']
        contract['assets'].append(asset)
        contract['shots'][0]['asset_ids'].append(ident)
    (root / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    inputs = {'governance': {'project_dir': str(root), 'shot_id': 'entry'}, 'duration': 8,
              'reference_image_paths': [str(root / f'assets/{ident}.bin') for ident in ('voice-a', 'voice-b')]}
    policy = make_policy(locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}},
        flex={'duration_s': [6, 10], 'resolution': [], 'references': {
            'droppable_roles': ['voice_reference'], 'substitutes': [{'sha256': H('replacement'), 'role': 'voice_reference'}]}})
    install_existing(root, policy, templates={'entry': execution.planned_request_template(inputs, project_dir=root)})
    policy = pa.require_active_policy(root)[0]
    pa._validate_request_delta(root, policy, 'entry', inputs, 'grok_cli')
    return policy, inputs, contract


def test_rooted_ordered_partial_reference_drop_and_reorder_rejection(tmp_path):
    policy, inputs, contract = reference_policy(tmp_path)
    candidate = copy.deepcopy(inputs)
    candidate['reference_image_paths'] = inputs['reference_image_paths'][1:]
    pa._validate_request_delta(tmp_path, policy, 'entry', candidate, 'grok_cli')
    candidate['reference_image_paths'] = list(reversed(inputs['reference_image_paths']))
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(tmp_path, policy, 'entry', candidate, 'grok_cli')
    contract['shots'][0]['asset_ids'].remove('voice-a')
    contract['assets'] = [a for a in contract['assets'] if a['id'] != 'voice-a']
    (tmp_path / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    assert pa.require_active_policy(tmp_path)


def test_reference_substitute_requires_actual_current_contract_role_and_bytes(tmp_path):
    policy, inputs, contract = reference_policy(tmp_path)
    substitute = tmp_path / 'assets/replacement.bin'
    substitute.write_text('replacement')
    candidate = copy.deepcopy(inputs)
    candidate['reference_image_paths'][0] = str(substitute)
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(tmp_path, policy, 'entry', candidate, 'grok_cli')
    asset = next(a for a in contract['assets'] if a['id'] == 'voice-a')
    asset.update(path='assets/replacement.bin', sha256=H('replacement'))
    asset['review']['subject_sha256'] = asset['sha256']
    (tmp_path / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    # Planning excludes mutable review attestation; exact declared reference flex
    # accepts the new content while downstream prep still requires a fresh review.
    assert pa.require_active_policy(tmp_path)
    pa._validate_request_delta(tmp_path, policy, 'entry', candidate, 'grok_cli')
    substitute.write_text('tampered')
    with pytest.raises(pa.AutonomyError):
        pa.require_active_policy(tmp_path)


@pytest.mark.parametrize('flex_kind', ['drop', 'substitute'])
def test_retained_actor_voice_reference_rejects_role_flex(tmp_path, flex_kind):
    from lib import production_execution as execution
    policy, inputs, contract = reference_policy(tmp_path)
    asset = next(a for a in contract['assets'] if a['id'] == 'voice-a')
    asset['cast_ids'] = ['patient']
    (tmp_path / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    policy['flex']['references'] = {'droppable_roles': [], 'substitutes': []}
    template = execution.planned_request_template(inputs, project_dir=tmp_path)
    install_existing(tmp_path, policy, templates={'entry': template})
    assert pa.require_active_policy(tmp_path)  # genuine actor-bound positive
    if flex_kind == 'drop':
        policy['flex']['references']['droppable_roles'] = ['voice_reference']
    else:
        policy['flex']['references']['substitutes'] = [{'sha256': H('replacement'), 'role': 'voice_reference'}]
    install_existing(tmp_path, policy, templates={'entry': template})
    with pytest.raises(pa.AutonomyError, match='conflict'):
        pa.require_active_policy(tmp_path)
