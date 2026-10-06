"""Offline report tests: isolated authority cases plus genuine governed execution.

Provider transport and AV reviews are explicitly synthetic test fixtures; they
establish software behavior, never real provider pricing or real media quality.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from lib import production_autonomy as autonomy
from lib import production_execution as execution
from lib.production_autonomy_report import completion_report


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize('raw', [b'{invalid', b'[]', b'null'])
def test_malformed_current_final_review_is_honest_draft(project, raw):
    root, _, sha = project
    path = root / 'artifacts/final_review.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    report = json.loads(completion_report(root, sha).read_bytes())
    assert report['quality_status'] == 'draft'
    assert report['final_review_sha256'] == hashlib.sha256(raw).hexdigest()
    assert any(f.get('category') == 'malformed_current_final_review' for f in report['findings'])


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'absent-private-state'))
    baseline_inputs = {'prompt': 'PRIVATE APPROVED PROMPT', 'model': 'grok-agent',
                       'duration': 5, 'resolution': '720p', 'output_path': str(root / 'baseline.mp4')}
    baseline = {'request_sha256': execution._digest(baseline_inputs), 'static_input_assets': []}
    evidence = {'baselines': {'shot': {'planned_request_template': {'inputs': baseline_inputs}}}}
    write(root / 'approvals' / 'policy.json', evidence)
    policy = {'project_id': 'p', 'story_revision': 's', 'shots': {'shot': baseline},
              'providers': [{'id': 'openart_cli', 'account_id_sha256': 'account', 'workspace': 'workspace', 'ceiling': '10'}, {'id': 'grok_cli'}],
              'evidence': {'path': 'approvals/policy.json', 'sha256': execution.file_sha256(root / 'approvals' / 'policy.json')}}
    sha = 'a' * 64
    monkeypatch.setattr(autonomy, 'require_active_policy', lambda _: (copy.deepcopy(policy), sha, 'decision'))
    write(root / 'project.json', {'project_id': 'p', 'story_revision': 's'})
    return root, policy, sha


def attempt(root, policy, sha, aid='failed-session', status='failed'):
    directory = root / 'production_attempts' / aid
    inputs = {'prompt': 'PRIVATE ATTEMPT PROMPT', 'model': 'grok-agent', 'duration': 6,
              'resolution': '768p', 'output_path': str(root / f'{aid}.mp4'), 'cli_session_id': aid}
    request_hash = execution._digest(execution._clean({k: v for k, v in inputs.items() if k != 'cli_session_id'}))
    evidence_path = directory / 'approval.json'
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes((root / policy['evidence']['path']).read_bytes())
    evidence = {'path': str(evidence_path), 'sha256': execution.file_sha256(evidence_path)}
    scope = {'id': 'scope', 'status': 'approved', 'approved_by': f'policy:{sha}', 'project_id': 'p', 'story_revision': 's', 'provider': 'grok_cli',
             'requests': {'shot': request_hash}, 'evidence': evidence,
             'derived_from_policy': {'policy_sha256': sha, 'decision_id': 'decision', 'baseline_sha256': execution._digest(policy['shots']['shot'])}}
    request = {'attempt_id': aid, 'cli_session_id': aid, 'scope_id': 'scope', 'project_id': 'p',
               'story_revision': 's', 'shot_id': 'shot', 'scope': scope, 'submitted_inputs': inputs,
               'input_assets': [], 'request_sha256': request_hash, 'approval_evidence': evidence}
    write(directory / 'request.json', request)
    write(directory / 'result.json', {'status': status})
    return directory, request


def test_sha_mismatch_has_no_writes(project):
    root, _, _ = project
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(autonomy.AutonomyError, match='SHA mismatch'):
        completion_report(root, 'b' * 64)
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert not (root / 'artifacts').exists()


def test_empty_report_is_draft_and_does_not_initialize_private_ledger(project):
    root, _, sha = project
    report = json.loads(completion_report(root, sha).read_bytes())
    assert report['quality_status'] == 'draft'
    assert report['attempts'] == [] and report['openart_credits'] == []
    assert report['grok'] == 'subscription quota unknown, 0 attempts'
    assert not (root.parent / 'absent-private-state').exists()


def test_failed_grok_counts_exact_settings_delta_and_redacts(project):
    root, policy, sha = project
    attempt(root, policy, sha)
    path = completion_report(root, sha)
    report = json.loads(path.read_bytes())
    row = report['attempts'][0]
    assert report['grok'] == 'subscription quota unknown, 1 attempts'
    assert row['status'] == 'failed' and row['settings']['media_model'] is None
    assert row['settings']['media_model_status'] == 'unreported'
    assert row['settings']['agent_model'] == 'grok-agent'
    changes = row['baseline_delta']['settings_changes']
    assert changes['duration'] == {'baseline': 5, 'attempted': 6}
    assert changes['resolution'] == {'baseline': '720p', 'attempted': '768p'}
    assert 'PRIVATE' not in path.read_text() and str(root) not in path.read_text()


def test_reports_never_overwrite(project):
    root, _, sha = project
    first = completion_report(root, sha); before = first.read_bytes()
    second = completion_report(root, sha)
    assert first != second and first.read_bytes() == before
    assert len(list(first.parent.glob('*.json'))) == 2


@pytest.mark.parametrize('field,value', [('story_revision', 'changed'), ('project_id', 'other'), ('request_sha256', 'b' * 64)])
def test_journal_bindings_fail_before_report_creation(project, field, value):
    root, policy, sha = project
    directory, request = attempt(root, policy, sha)
    request[field] = value; write(directory / 'request.json', request)
    with pytest.raises(autonomy.AutonomyError): completion_report(root, sha)
    assert not (root / 'artifacts' / 'autonomy_reports').exists()


def test_tampered_retained_baseline_is_rejected(project):
    root, _, sha = project
    write(root / 'approvals' / 'policy.json', {'baselines': {}})
    with pytest.raises(autonomy.AutonomyError, match='retained policy evidence changed'):
        completion_report(root, sha)
    assert not (root / 'artifacts').exists()


def test_v1_passing_final_review_is_still_draft(project):
    root, _, sha = project
    write(root / 'artifacts' / 'final_review.json', {'version': '1.0', 'status': 'pass'})
    report = json.loads(completion_report(root, sha).read_bytes())
    assert report['quality_status'] == 'draft'
    assert any(f['code'] == 'final_certification_ineligible' for f in report['findings'])


def test_totals_and_certification_are_not_caller_arguments(project):
    root, _, sha = project
    with pytest.raises(TypeError): completion_report(root, sha, certified=True)
    with pytest.raises(TypeError): completion_report(root, sha, openart_credits_used='0')


def test_missing_baseline_provider_is_unknown_not_attempted_provider(project):
    root, policy, sha = project
    attempt(root, policy, sha)
    report = json.loads(completion_report(root, sha).read_bytes())
    delta = report['attempts'][0]['baseline_delta']
    assert delta['baseline_settings']['provider'] is None
    assert delta['baseline_settings']['media_model_status'] == 'unknown'
    assert 'provider' in delta['unresolved_dimensions']


def test_other_policy_credit_reservation_is_not_counted(project):
    from uuid import uuid4
    from lib.provider_credit_ledger import CreditLedger, CreditScale, Binding, ValidatedReservation
    root, _, sha = project
    ledger = CreditLedger(root.parent / 'absent-private-state')
    ledger.observe_account('openart_cli', 'account', 'workspace', CreditScale('0.25'), '20', 'c' * 64)
    binding = Binding('openart_cli', 'account', 'workspace', str(root), str(uuid4()),
                      'c' * 64, str(uuid4()), 'c' * 64, 'c' * 64, 'c' * 64, 'c' * 64,
                      'policy:' + 'b' * 64 + ':openart', '10', '3', '2.25')
    ledger.reserve_prepared(ValidatedReservation(binding, 'c' * 64))
    report = json.loads(completion_report(root, sha).read_bytes())
    assert report['openart_credits'] == [] and report['attempts'] == []


def test_matching_ledger_reservation_without_private_intent_blocks_report(project):
    from uuid import uuid4
    from lib.provider_credit_ledger import CreditLedger, CreditScale, Binding, ValidatedReservation
    root, _, sha = project
    ledger = CreditLedger(root.parent / 'absent-private-state')
    ledger.observe_account('openart_cli', 'account', 'workspace', CreditScale('0.25'), '20', 'c' * 64)
    binding = Binding('openart_cli', 'account', 'workspace', str(root), str(uuid4()),
                      'c' * 64, str(uuid4()), 'c' * 64, 'c' * 64, 'c' * 64, 'c' * 64,
                      autonomy.openart_allowance_id(sha), '10', '3', '2.25')
    ledger.reserve_prepared(ValidatedReservation(binding, 'c' * 64))
    before = ledger.inspect(binding.attempt_id)
    with pytest.raises(Exception, match='dispatch proof missing or changed'):
        completion_report(root, sha)
    assert not (root / 'artifacts').exists()
    assert ledger.inspect(binding.attempt_id) == before


@pytest.mark.parametrize('where,value', [('scope', None), ('derived_from_policy', None), ('derived_from_policy', []), ('evidence', None)])
def test_malformed_journal_authority_is_typed_and_never_silently_omitted(project, where, value):
    root, policy, sha = project
    directory, request = attempt(root, policy, sha)
    if where == 'scope': request['scope'] = value
    else: request['scope'][where] = value
    write(directory / 'request.json', request)
    with pytest.raises(autonomy.AutonomyError): completion_report(root, sha)
    assert not (root / 'artifacts').exists()


def test_template_baseline_unchanged_exact_request_is_false(project):
    root, policy, sha = project
    baseline = policy['shots']['shot']
    baseline.pop('request_sha256')
    template = {'inputs': {'prompt': 'PRIVATE ATTEMPT PROMPT', 'model': 'grok-agent',
                          'duration': 6, 'resolution': '768p', 'output_path': str(root / 'baseline.mp4')},
                'static_input_assets': []}
    baseline['planned_request_template_sha256'] = execution._digest(template)
    write(root / 'approvals' / 'policy.json', {'baselines': {'shot': {'planned_request_template': template}}})
    policy['evidence']['sha256'] = execution.file_sha256(root / 'approvals' / 'policy.json')
    attempt(root, policy, sha, aid='baseline')
    report = json.loads(completion_report(root, sha).read_bytes())
    delta = report['attempts'][0]['baseline_delta']
    assert delta['request_changed'] is False
    assert delta['resolved_baseline_request_sha256'] == report['attempts'][0]['request_sha256']


@pytest.fixture
def openart_project(tmp_path, monkeypatch):
    """Normal fake executable/dispatch/ledger chain with genuine policy activation.

    The fake provider is local and all reviews explicitly identify synthetic tests.
    No report authority/native/ledger reader is mocked, and no scope constructor
    substitutes for the public rooted derivation gate.
    OpenArt now bootstraps FULL qualification through an original Strict sample,
    settles that sample, and derives a public policy scope in a separate project.
    Synthetic semantic review never establishes real video quality.
    """
    from tests.integration.test_openart_dispatch_recovery import governed, _clone_authorized_project
    from tests.lib.test_production_autonomy import make_policy, install_existing
    from lib import openart_jobs as jobs
    fixture = governed.__wrapped__(tmp_path, monkeypatch)
    root, inputs, profile, private_parent = next(fixture)
    try:
        # Bootstrap FULL real-origin qualification through an original Strict fake-CLI sample.
        from tools.video.openart_cli_video import OpenArtCLIVideo
        sample = OpenArtCLIVideo().execute(inputs).data['production_attempt_id']
        settle_fake_openart(root, sample, monkeypatch, refund=None)
        profile = jobs.load_qualification(model='m1', mode='image2video', require='full')
        target = private_parent / 'report-project'
        inputs = _clone_authorized_project(root, inputs, profile, target)
        root = target
        marker = json.loads((root / 'project.json').read_bytes())
        policy = make_policy(project_id=marker['project_id'], story_revision=marker['story_revision'],
                             checkpoint_stages=[], locked={'cast': {}, 'dialogue': {}, 'sources': [],
                                                           'story_predicates': [], 'controls': {}},
                             providers=[{'id': 'openart_cli', 'models': ['m1'], 'billing': 'credits',
                                         'ceiling': '10', 'account_id_sha256': profile['account_id_sha256'],
                                         'workspace': 'workspace'},
                                        {'id': 'grok_cli', 'model_policy': 'cli_managed_media_unreported',
                                         'billing': 'subscription_quota_unknown'}])
        template = execution.planned_request_template(inputs, project_dir=root)
        policy, sha = install_existing(root, policy, templates={'entry': template})
        assert autonomy.require_active_policy(root)[1] == sha
        # Explicit synthetic board membership is required; equal fixture bytes do not infer cast roles.
        projection = autonomy.current_projection(root, 'entry')
        review_path = root / 'artifacts/preparation_review-r1.json'
        review = json.loads(review_path.read_bytes())
        review['composite_boards'] = [{'sha256': execution.file_sha256(inputs['image_path']), 'role': 'start_frame',
                                       'members': [{'sha256': a['sha256'], 'role': a['role'], 'cast_ids': a['cast_ids']}
                                                   for a in projection['assets']
                                                   if a['role'] in autonomy.CAST_ROLES or
                                                      (a['role'] == 'end_frame' and a['cast_ids'])]}]
        write(review_path, review)
        scope = autonomy.derive_scope(root, inputs, provider='openart_cli')
        assert inputs['governance']['scope_id'] == scope['id']
        assert inputs['credit_authorization_id'] == scope['id']
        yield root, inputs, profile, private_parent, policy, sha
    finally:
        try: next(fixture)
        except StopIteration: pass


def test_actual_openart_dispatch_report_has_private_settings_job_and_exact_holds(openart_project):
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, private, _, sha = openart_project
    result = OpenArtCLIVideo().execute(inputs)
    aid = result.data['production_attempt_id']
    report = json.loads(completion_report(root, sha).read_bytes())
    assert len(report['attempts']) == 1
    row = report['attempts'][0]
    assert row['attempt_id'] == aid and row['job_id'].startswith('job-')
    assert row['settings']['media_model'] == 'm1'
    assert row['settings']['duration'] == 8 and row['settings']['resolution'] == '720p'
    assert row['baseline_delta']['request_changed'] is False
    assert row['baseline_delta']['reference_bytes_changed'] is False
    assert row['credits']['original_reserved'] == '2.25'
    assert row['credits']['active_reserved'] == '0.00'
    assert row['credits']['unresolved_hold'] == row['credits']['active_hold'] == '2.25'
    assert row['credits']['settled_gross'] == row['credits']['settled_net'] == '0.00'
    assert report['quality_status'] == 'draft'
    assert len((private / 'paid').read_text().splitlines()) == 2
    public = json.dumps(report)
    assert str(root) not in public and str(private) not in public and '?sig=' not in public
    assert inputs['prompt'] not in public


def test_actual_prepared_outbox_only_reservation_is_counted_once(openart_project):
    from lib import openart_dispatch as dispatch
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, private, _, sha = openart_project
    consumed = []
    def crash(stage, aid):
        if stage == 'ledger_prepared':
            consumed.append(aid)
            raise RuntimeError('Synthetic outbox-only crash before journal publication')
    dispatch._CRASH_HOOK = crash
    with pytest.raises(RuntimeError, match='Synthetic outbox-only crash'):
        OpenArtCLIVideo().execute(inputs)
    assert consumed
    dispatch._CRASH_HOOK = None
    assert not (root / 'production_attempts' / consumed[0] / 'request.json').exists()
    report = json.loads(completion_report(root, sha).read_bytes())
    assert len(report['attempts']) == 1 and report['attempts'][0]['attempt_id'] == consumed[0]
    assert report['attempts'][0]['outbox_only'] is True
    credit = report['attempts'][0]['credits']
    assert credit['active_reserved'] == credit['active_hold'] == '2.25'
    assert credit['unresolved_hold'] == '0.00'
    assert len((private / 'paid').read_text().splitlines()) == 1


def settle_fake_openart(root, aid, monkeypatch, *, charge='2.25', refund='0.25'):
    """Qualify actual retained fake-provider receipts, then settle original job."""
    from lib import openart_dispatch as dispatch, openart_jobs as jobs
    launch = jobs.launch_record(aid)
    try:
        jobs.load_qualification(model=launch['profile']['model'], mode=launch['profile']['mode'], require='full')
    except jobs.OpenArtCLIError:
        monkeypatch.setenv('FAKE_STATUS', 'done')
        jobs.promote_result_contract(aid, json_paths={'url_hosts': ['cdn.openart.test']})
    monkeypatch.setenv('FAKE_STATUS', 'failed')
    full_profile = jobs.load_qualification(model=launch['profile']['model'], mode=launch['profile']['mode'], require='full')
    collected = jobs.collect_job(aid, output_path=jobs._bound_output_path(aid, launch),
                                 output_root=root, profile=full_profile)
    assert collected['status'] == 'failed_terminal', collected
    monkeypatch.setenv('FAKE_CHARGE', charge)
    if refund is None: monkeypatch.delenv('FAKE_REFUND', raising=False)
    else: monkeypatch.setenv('FAKE_REFUND', refund)
    contract = dispatch.BillingContract('billing.account', 'billing.workspace', 'billing.native', 'billing.job',
                                       'billing.amount', 'billing.quantum', 'billing.final', 'billing.per_job_authoritative',
                                       'billing.debit_id', 'billing.refund', 'billing.refund_id',
                                       'billing.refund_authoritative', 'billing.refund_of')
    binding = dispatch._manifest(aid)[1]
    proof = dispatch.qualify_resolution_contract(root, aid, binding.request_sha256, contract)
    dispatch.resolve_attempt(root, aid, binding.request_sha256, billing_proof_id=proof['billing_proof_id'])


def test_actual_settled_refunded_and_quarantined_credits_are_exact(openart_project, monkeypatch):
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, _, _, sha = openart_project
    aid = OpenArtCLIVideo().execute(inputs).data['production_attempt_id']
    settle_fake_openart(root, aid, monkeypatch, charge='4.00', refund='0.25')
    report = json.loads(completion_report(root, sha).read_bytes())
    credit = report['attempts'][0]['credits']
    assert credit['settled_gross'] == '4.00' and credit['refunded'] == '0.25' and credit['settled_net'] == '3.75'
    assert credit['active_hold'] == credit['active_reserved'] == credit['unresolved_hold'] == '0.00'
    assert any(f['code'] == 'credit_account_quarantined' for f in report['findings'])
    assert report['openart_credits'][0]['settled_net'] == '3.75'


def test_actual_openart_failure_then_generated_grok_mixed_report(openart_project, monkeypatch):
    from lib import production_request as preparation
    from tools.video.openart_cli_video import OpenArtCLIVideo
    from tools.video.grok_cli_video import GrokCLIVideo
    from tests.integration.test_first_pass_workflow import NativeTransport
    root, inputs, _, _, policy, sha = openart_project
    original = OpenArtCLIVideo().execute(inputs).data['production_attempt_id']
    settle_fake_openart(root, original, monkeypatch)
    transport = NativeTransport(root)
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', transport)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda name: '/offline/' + name)
    grok = {'project_dir': str(root), 'governance': {'shot_id': 'entry'}, 'operation': 'reference_to_video',
            'prompt': inputs['prompt'], 'first_frame': inputs['image_path'],
            'duration': 8, 'resolution': '720p',
            'aspect_ratio': '16:9', 'allow_unknown_cost': True, 'output_path': str(root / 'grok-repair.mp4')}
    canonical = preparation.compile_provider_prompt(root, 'entry', provider='grok_cli')
    grok.update(prompt=canonical['prompt'], compiled_request_id='grok-c1', preparation_review_id='grok-r1')
    native = preparation.prepare_grok_native(grok, {'cli_version': '1.0.34', 'grok_path': '/offline/grok'})
    retained_compiled = json.loads((root / 'artifacts/compiled_request-c1.json').read_bytes())
    compiled = preparation.prepare_compiled_request(grok, native, {'source': 'real'},
                                                   coverage=canonical['coverage'], timing=retained_compiled['timing'])
    review = json.loads((root / 'artifacts/preparation_review-r1.json').read_bytes())
    review.update(review_id='grok-r1', subject_sha256=preparation.digest(compiled))
    write(root / 'artifacts/compiled_request-grok-c1.json', compiled)
    write(root / 'artifacts/preparation_review-grok-r1.json', review)
    from tools._grok_cli_media import observe_grok_cli_compatibility
    from lib import openart_jobs as jobs
    terminal = jobs.verify_terminal_failure(original, jobs.load_qualification(model='m1', mode='image2video', require='full'))
    write(root / 'production_attempts' / original / 'rejection.json',
          {'status': 'fail', 'kind': 'generation_terminal_failure', 'attempt_id': original,
           'terminal_failure_sha256': terminal['terminal_failure_sha256'],
           'reviewer': 'Synthetic software-test engineering review of actual fake-provider terminal failure; no AV claim'})
    observation = observe_grok_cli_compatibility('grok', cwd=root)
    scope = autonomy.derive_scope(root, grok, provider='grok_cli', observation=observation,
                                  phase='repair', replaces_attempt_ids=[original])
    grok['governance']['scope_id'] = scope['id']
    result = GrokCLIVideo(grok_path='grok', sessions_root=str(root / 'sessions')).execute(grok)
    assert result.success, result.error
    report = json.loads(completion_report(root, sha).read_bytes())
    assert len(report['attempts']) == 2
    by_provider = {r['settings']['provider']: r for r in report['attempts']}
    assert by_provider['grok_cli']['status'] == 'generated'
    assert by_provider['grok_cli']['settings']['media_model'] is None
    assert by_provider['grok_cli']['settings']['media_model_status'] == 'unreported'
    assert by_provider['grok_cli']['settings']['agent_model'] is not None
    assert by_provider['openart_cli']['status'] == 'failed'
    assert report['grok'] == 'subscription quota unknown, 1 attempts'
    assert report['openart_credits'][0]['settled_net'] == '2.00'
    assert len(transport.native_requests) == 1


@pytest.mark.parametrize('state,expected', [
    ('reserved', {'active_reserved': 9, 'unresolved_hold': 0, 'active_hold': 9}),
    ('unresolved', {'active_reserved': 0, 'unresolved_hold': 9, 'active_hold': 9}),
    ('released', {'active_reserved': 0, 'unresolved_hold': 0, 'active_hold': 0}),
    ('settled', {'active_reserved': 0, 'unresolved_hold': 0, 'active_hold': 0}),
    ('refunded', {'active_reserved': 0, 'unresolved_hold': 0, 'active_hold': 0}),
])
def test_credit_hold_states_keep_historical_reserved_separate(state, expected):
    from lib.production_autonomy_report import _credit_units
    row = {'debit_state': state, 'reserved_units': 9, 'charged_units': 8 if state in {'settled', 'refunded'} else 0,
           'refunded_units': 2 if state == 'refunded' else 0}
    units = _credit_units(row)
    assert {key: units[key] for key in expected} == expected
    assert units['original_reserved'] == 9
    assert units['settled_net'] == row['charged_units'] - row['refunded_units']


@pytest.mark.parametrize('evidence', ['frozen_request', 'frozen_native', 'private_preparation', 'public_journal'])
def test_actual_openart_changed_bound_evidence_blocks_publication(openart_project, evidence):
    from lib import openart_jobs as jobs
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, private, _, sha = openart_project
    aid = OpenArtCLIVideo().execute(inputs).data['production_attempt_id']
    if evidence == 'frozen_request':
        target = jobs.job_dir(aid, create=False) / 'frozen_request.json'
        value = json.loads(target.read_bytes()); value['inputs']['resolution'] = '1080p'
    elif evidence == 'frozen_native':
        target = jobs.job_dir(aid, create=False) / 'frozen_request.json'
        value = json.loads(target.read_bytes())
        value['native']['native_body_sha256'] = '0' * 64
    elif evidence == 'private_preparation':
        target = private / 'private' / 'preparation' / aid / 'review.json'
        value = json.loads(target.read_bytes()); value['review']['reviewer'] = 'changed synthetic reviewer'
    else:
        target = root / 'production_attempts' / aid / 'request.json'
        value = json.loads(target.read_bytes()); value['submitted_inputs']['duration'] = 6
    target.chmod(0o600); target.write_text(json.dumps(value))
    with pytest.raises((ValueError, jobs.OpenArtCLIError)): completion_report(root, sha)
    assert not (root / 'artifacts' / 'autonomy_reports').exists()


@pytest.mark.parametrize('authority', ['different_policy', 'strict', 'different_provider'])
def test_matching_reservation_cannot_hide_existing_other_authority_journal(openart_project, authority):
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, _, _, sha = openart_project
    aid = OpenArtCLIVideo().execute(inputs).data['production_attempt_id']
    path = root / 'production_attempts' / aid / 'request.json'
    request = json.loads(path.read_bytes())
    if authority == 'different_policy':
        request['scope']['derived_from_policy']['policy_sha256'] = 'b' * 64
    elif authority == 'strict':
        del request['scope']['derived_from_policy']
        request['scope']['approved_by'] = 'Synthetic Strict fixture author'
    else:
        request['scope']['provider'] = 'grok_cli'
    path.chmod(0o600)
    path.write_text(json.dumps(request))
    expected = ('matching OpenArt reservation contradicts journal provider' if authority == 'different_provider'
                else 'existing journal under another authority')
    with pytest.raises(autonomy.AutonomyError, match=expected):
        completion_report(root, sha)
    assert not (root / 'artifacts/autonomy_reports').exists()


def test_outbox_journal_appearing_during_report_blocks_publication(openart_project, monkeypatch):
    from lib import openart_dispatch as dispatch
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, _, _, sha = openart_project
    consumed = []
    def crash(stage, aid):
        if stage == 'ledger_prepared':
            consumed.append(aid)
            raise RuntimeError('Synthetic outbox-only crash before journal publication')
    dispatch._CRASH_HOOK = crash
    try:
        with pytest.raises(RuntimeError, match='Synthetic outbox-only crash'):
            OpenArtCLIVideo().execute(inputs)
    finally:
        dispatch._CRASH_HOOK = None
    assert len(consumed) == 1
    aid = consumed[0]
    original_authority = autonomy.require_active_policy
    calls = []
    def authority(project_root):
        authorized = original_authority(project_root)
        calls.append(None)
        if len(calls) == 2:
            dispatch.repair_outbox(root, aid)
        return authorized
    monkeypatch.setattr(autonomy, 'require_active_policy', authority)
    with pytest.raises(autonomy.AutonomyError, match='outbox-only public journal appeared'):
        completion_report(root, sha)
    assert (root / 'production_attempts' / aid / 'request.json').is_file()
    assert not (root / 'artifacts/autonomy_reports').exists()


def test_actual_ledger_change_during_report_blocks_publication(project, monkeypatch):
    from lib.provider_credit_ledger import CreditLedger, CreditScale
    root, policy, sha = project
    calls = []
    def authority(_):
        calls.append(None)
        if len(calls) == 2:
            ledger = CreditLedger(root.parent / 'absent-private-state')
            ledger.observe_account('openart_cli', 'account', 'workspace', CreditScale('0.25'), '20', 'c' * 64)
        return copy.deepcopy(policy), sha, 'decision'
    monkeypatch.setattr(autonomy, 'require_active_policy', authority)
    with pytest.raises(autonomy.AutonomyError, match='credit ledger changed during report'):
        completion_report(root, sha)
    assert not (root / 'artifacts').exists()


def test_valid_different_policy_journal_is_excluded_with_finding(project):
    root, policy, sha = project
    directory, request = attempt(root, policy, sha)
    request['scope']['derived_from_policy']['policy_sha256'] = 'b' * 64
    request['scope']['approved_by'] = 'policy:' + 'b' * 64
    write(directory / 'request.json', request)
    report = json.loads(completion_report(root, sha).read_bytes())
    assert report['attempts'] == []
    assert any(f['code'] == 'excluded_other_policy_or_strict_attempt' for f in report['findings'])


def test_actual_historical_failed_attempt_survives_approved_current_retime(openart_project, monkeypatch):
    from tools.video.openart_cli_video import OpenArtCLIVideo
    root, inputs, _, _, policy, sha = openart_project
    aid = OpenArtCLIVideo().execute(inputs).data['production_attempt_id']
    settle_fake_openart(root, aid, monkeypatch)
    retained = autonomy.retained_baselines(root, policy)
    contract, scene, script = autonomy._canonical_global_planning(policy, retained, {'entry': 6})
    original_contract = json.loads((root / 'artifacts/shot_contract.json').read_bytes())
    original_assets = {a['id']: a for a in original_contract['assets']}
    for asset in contract['assets']:
        asset['review'] = copy.deepcopy(original_assets[asset['id']]['review'])
    from lib.shot_contract import contract_digest, PROJECT_PREDICATES, SHOT_PREDICATES
    from tests.integration.test_first_pass_workflow import attestation
    current_digest = contract_digest(contract)
    contract['project_review'] = attestation(current_digest, PROJECT_PREDICATES, policy['story_revision'])
    for shot in contract['shots']:
        shot['review'] = attestation(current_digest, SHOT_PREDICATES, policy['story_revision'])
    write(root / 'artifacts/shot_contract.json', contract)
    write(root / 'artifacts/scene_plan.json', scene)
    write(root / 'artifacts/script.json', script)
    assert autonomy.require_active_policy(root)[1] == sha
    report = json.loads(completion_report(root, sha).read_bytes())
    assert report['attempts'][0]['status'] == 'failed'
    assert report['attempts'][0]['settings']['duration'] == 8
    assert any(f['code'] == 'historical_preparation_current_source_changed' for f in report['findings'])


def test_current_synthetic_certification_revalidates_and_changed_master_becomes_draft(tmp_path, monkeypatch):
    """Actual final gate/provenance; synthetic AV review establishes software behavior only."""
    from tests.integration.test_first_pass_workflow import production
    from tests.lib.test_production_autonomy import make_policy, install_existing
    from lib.production_review import certify_final
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'absent-state'))
    fixture = production.__wrapped__(tmp_path, monkeypatch)
    p = next(fixture)
    try:
        # The fixture's original plan has no script links; add explicit matching links
        # before any original generation, without altering retained native history.
        for scene in p.plan['scenes']: scene['script_section_id'] = scene['id']
        write(p.root / 'artifacts/scene_plan.json', p.plan)
        p.complete_shots()
        master, _ = p.draft()
        certify_final(p.root, p.full_review(master))
        policy = make_policy(project_id=p.root.name, story_revision=p.story['story_revision'],
                             providers=[{'id': 'grok_cli', 'model_policy': 'cli_managed_media_unreported',
                                         'billing': 'subscription_quota_unknown'}], checkpoint_stages=[],
                             locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
        # A newly approved policy cannot retrospectively attribute earlier Strict attempts.
        policy, sha = install_existing(p.root, policy, templates={sid: execution.planned_request_template(inputs, project_dir=p.root)
                                                                  for sid, inputs in p.inputs.items()})
        report = json.loads(completion_report(p.root, sha).read_bytes())
        assert report['quality_status'] == 'certified' and report['attempts'] == []
        assert len([f for f in report['findings'] if f['code'] == 'excluded_other_policy_or_strict_attempt']) == 3
        master.write_bytes(b'CHANGED SYNTHETIC MASTER')
        changed = json.loads(completion_report(p.root, sha).read_bytes())
        assert changed['quality_status'] == 'draft'
        assert any(f['code'] == 'final_certification_ineligible' for f in changed['findings'])
    finally:
        try: next(fixture)
        except StopIteration: pass
