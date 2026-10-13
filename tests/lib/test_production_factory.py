"""Prospective factory seams with retained synthetic evidence; no provider calls."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import production_request as request, production_images as images
from lib.shot_contract import project_review_subject, shot_review_subject
from tests.lib.test_production_autonomy import fixture_planning, make_policy, SCRIPT
from tests.lib.test_production_images import board_package, reserve, host_receipt  # noqa: F401
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_first_cut import p, production  # noqa: F401
from tests.integration.test_first_pass_workflow import attestation
from tests.lib.test_openart_mcp_native import observations as guided_observations  # noqa: F401


def write(root, name, value):
    path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def creator_fixture(root, name, record):
    """Clearly synthetic fixture of an actual retained creator-message binding."""
    record = copy.deepcopy(record)
    record['authorization'] = write(root, 'notes/' + name + '-authorization.json', {
        'version': '1.0', 'kind': 'creator_authorization', 'origin': 'user_message',
        'identity_id': 'fixture-identity', 'message_id': 'SYNTHETIC-' + name,
        'text': 'SYNTHETIC offline creator authorization fixture; not production authority.',
        'subject_sha256': request.digest(record)})
    return write(root, 'notes/' + name + '.json', record)


def standing_fixture(root):
    contract = fixture_planning(root); contract['planning_mode'] = 'shot_local_v1'
    contract['shots'] = [s for s in contract['shots'] if s['id'] == 'entry']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    write(root, 'artifacts/shot_contract.json', contract)
    write(root, 'project.json', {'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'identity_id': 'fixture-identity', 'pipeline_type': 'cinematic',
        'governance': {'mode': 'strict', 'version': '1.0'}})
    policy = make_policy(locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    policy['checkpoint_stages'] = ['proposal', 'script', 'scene_plan', 'assets']
    template = execution.planned_request_template({'duration': 8, 'image_path': str(root / 'assets/start.svg')}, project_dir=root)
    projection = pa.current_projection(root, 'entry')
    baselines = {'entry': {'projection': projection, 'planned_request_template': template,
        'script_snapshot': copy.deepcopy(SCRIPT), 'dialogue_coverage': pa.dialogue_occurrences(projection),
        'approval_plan_sha256': execution.approval_plan_digest(contract, 'entry')}}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': copy.deepcopy(policy['providers']),
        'provider_accounts': {'grok_cli': 'fixture-subscription', 'openart_cli': 'fixture-credit-account'},
        'caps': copy.deepcopy(policy['caps']), 'checkpoint_stages': policy['checkpoint_stages'],
        'flex': copy.deepcopy(policy['flex']), 'approved_source_sha256': [a['sha256'] for a in contract['assets']],
        'required_permissions': [], 'source_transfer': [
            {'provider': p['id'], 'account': 'fixture-subscription' if p['id'] == 'grok_cli' else 'fixture-credit-account',
             'roles': ['identity_reference', 'start_frame', 'end_frame', 'payoff_board'],
             'generated_board_sources': False, 'max_files': 8, 'max_file_bytes': 10000,
             'max_batch_bytes': 80000, 'max_batches': 3} for p in policy['providers']]}
    setup_binding = creator_fixture(root, 'standing', setup)
    command_binding = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'command': 'make_video', 'standing_setup_sha256': setup_binding['sha256']})
    return dict(policy=policy, baselines=baselines, standing_setup=setup_binding,
        run_command=command_binding, identity_id='fixture-identity')


def test_standing_activation_exact_replay_and_current_creator_bytes(tmp_path):
    kwargs = standing_fixture(tmp_path)
    result = pa.activate_standing_run(tmp_path, **kwargs)
    assert result['provider_calls'] == 0 and not result['replayed']
    active, sha, decision, reasons = pa.load_active_policy(tmp_path)
    assert not reasons and sha == result['policy_sha256'] and decision == result['decision_id']
    assert active['activation_basis']['plan_sha256'] == request.digest(kwargs['baselines'])
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(result, replayed=True)
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    (tmp_path / kwargs['run_command']['path']).write_text('{}')
    assert pa.load_active_policy(tmp_path)[0] is None
    assert not list(tmp_path.glob('**/*checkpoint*'))


@pytest.mark.parametrize('source', ['static', 'board', 'input_assets'])
@pytest.mark.parametrize('limit', [1, 2])
def test_standing_source_file_capacity_counts_native_binding_occurrences_before_writes(tmp_path, source, limit):
    kwargs = standing_fixture(tmp_path)
    path = str(tmp_path/'assets/start.svg')
    inputs = {'duration':8, 'image_path':path, 'last_image_path':path}
    if source == 'board':
        inputs = {'duration':8, 'image_path':{'$board':{'asset_id':'start'}},
                  'keyframes':[{'image':{'$board':{'asset_id':'start'}}, 'timestamp_s':2}]}
    elif source == 'input_assets':
        inputs = {'duration':8, 'input_assets':[{'role':role, 'source_path':path} for role in ('first_frame','last_frame')]}
    kwargs['baselines']['entry']['planned_request_template'] = execution.planned_request_template(inputs, project_dir=tmp_path)
    setup = json.loads((tmp_path/kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    for term in setup['source_transfer']: term['max_files'] = limit
    kwargs['standing_setup'] = creator_fixture(tmp_path, 'standing', setup)
    command = json.loads((tmp_path/kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'command', command)
    with execution._lock(tmp_path): pass
    before = file_bytes(tmp_path)
    if limit == 1:
        with pytest.raises(pa.StandingSetupRequired, match='planned source batch.*max_files'):
            pa.activate_standing_run(tmp_path, **kwargs)
        assert file_bytes(tmp_path) == before
        assert images.image_usage(tmp_path)['total'] == 0
        assert pa.root_attempt_counts(tmp_path, kwargs['policy'])['total'] == 0
    else:
        result = pa.activate_standing_run(tmp_path, **kwargs)
        before = file_bytes(tmp_path)
        assert pa.activate_standing_run(tmp_path, **kwargs) == dict(result, replayed=True)
        assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('lock', ['sources', 'cast'])
def test_semantic_only_payoff_cannot_satisfy_explicit_source_or_cast_lock(tmp_path, lock):
    kwargs = standing_fixture(tmp_path)
    contract = execution.load_shot_contract(tmp_path)
    contract['shots'][0]['asset_ids'].append(contract['payoff_asset_id'])
    contract['shots'][0]['review']['subject_sha256'] = shot_review_subject(contract, 'entry')
    write(tmp_path, 'artifacts/shot_contract.json', contract)
    kwargs['baselines']['entry']['projection'] = pa.current_projection(tmp_path, 'entry')
    kwargs['baselines']['entry']['approval_plan_sha256'] = execution.approval_plan_digest(contract, 'entry')
    pa.activate_standing_run(tmp_path, **kwargs)
    policy, _, _ = pa.require_active_policy(tmp_path)
    assets = kwargs['baselines']['entry']['projection']['assets']
    payoff = next(a for a in assets if a['role'] == 'payoff_board')
    if lock == 'sources': policy['locked']['sources'] = [payoff['sha256']]
    else: policy['locked']['cast'] = {'patient': [{key:payoff[key] for key in ('sha256','role')}]}
    content = pa.evidence_content(policy, kwargs['baselines'])
    sha = pa.digest(content)
    policy['evidence'] = write(tmp_path, 'approvals/autonomy-policy-' + sha + '.json', content)
    inputs = {'duration':8, 'input_assets':[{'role':{'start_frame':'first_frame', 'end_frame':'last_frame',
                'identity_reference':'reference_image'}[a['role']], 'source_path':str(tmp_path/a['path'])}
                for a in assets if a['id'] != payoff['id']]}
    # The full canonical preparation path already tests the semantic-only
    # marker. Exercise the lock boundary directly with rooted retained bytes.
    compiled = {'source_binding':{'references':[{'id':payoff['id'], 'role':'payoff_board',
        'sha256':payoff['sha256'], 'native_required':False}]}}
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError, match='locked source/cast payoff|explicit cast lock'):
        pa._root_lock_proof(tmp_path, 'entry', inputs=inputs, native={}, profile={},
            historical={'policy':policy, 'policy_sha256':sha, 'compiled':compiled, 'review':{}})
    assert file_bytes(tmp_path) == before


def standing_controls_fixture(root, controls):
    """Explicit controls in the exact synthetic creator-approved setup."""
    kwargs = standing_fixture(root)
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup['episode_controls'] = copy.deepcopy(controls)
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    return kwargs


def test_fresh_standing_activation_projects_explicit_creator_episode_controls(tmp_path):
    from lib import episode_production_controls as controls
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled',
        'access_fallback': False, 'first_cut': {'enabled': True}}
    kwargs = standing_controls_fixture(tmp_path, payload)
    before = pa.root_attempt_counts(tmp_path, kwargs['policy'])
    result = pa.activate_standing_run(tmp_path, **kwargs)
    effective = controls.effective_controls(tmp_path)
    assert effective is not None
    assert {key: effective[key] for key in payload} == payload
    assert controls.first_cut_enabled(tmp_path)
    assert effective['records'] == 1 and effective['restricted_providers'] == []
    assert pa.root_attempt_counts(tmp_path, kwargs['policy']) == before
    assert result['provider_calls'] == 0
    records = json.loads((tmp_path / controls.ARTIFACT).read_text())['records']
    assert records[0]['evidence'] == {**kwargs['standing_setup'],
        'approved_by': 'creator:fixture-identity:SYNTHETIC-standing'}
    assert records[0]['authority']['grants'] == []
    assert result['decision_id'].endswith('-controls-' + request.digest(records[0]))
    assert pa.require_active_policy(tmp_path)[2] == result['decision_id']


@pytest.mark.parametrize('payload', [None, {'max_generations_per_shot': 1,
    'alternate_repair': 'disabled', 'access_fallback': False, 'first_cut': {'enabled': False}}])
def test_standing_episode_controls_absence_and_explicit_overrides_preserve_defaults(tmp_path, payload):
    from lib import episode_production_controls as controls
    kwargs = standing_fixture(tmp_path) if payload is None else standing_controls_fixture(tmp_path, payload)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    assert not controls.first_cut_enabled(tmp_path)
    if payload is None:
        assert controls.effective_controls(tmp_path) is None
        assert not (tmp_path / controls.ARTIFACT).exists()
    else:
        effective = controls.effective_controls(tmp_path)
        assert {key: effective[key] for key in payload} == payload
    before = file_bytes(tmp_path)
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(activation, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('payload', [None, {}, {'first_cut': {'enabled': True}},
    {'max_generations_per_shot': True, 'alternate_repair': 'disabled', 'access_fallback': False},
    {'max_generations_per_shot': 0, 'alternate_repair': 'disabled', 'access_fallback': False},
    {'max_generations_per_shot': 1, 'alternate_repair': 'unknown', 'access_fallback': False},
    {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': 'yes'},
    {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False,
     'first_cut': {'enabled': True, 'extra': True}},
    {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False, 'provider': 'anything'}])
def test_standing_episode_controls_invalid_explicit_snapshot_writes_no_authority(tmp_path, payload):
    kwargs = standing_controls_fixture(tmp_path, payload)
    with execution._lock(tmp_path): pass
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='episode controls'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['setup_bytes', 'creator_subject', 'creator_identity', 'command_setup'])
def test_standing_episode_controls_require_exact_creator_setup_and_command(tmp_path, change):
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False}
    kwargs = standing_controls_fixture(tmp_path, payload)
    setup = json.loads((tmp_path / kwargs['standing_setup']['path']).read_text())
    if change == 'setup_bytes':
        setup['episode_controls']['max_generations_per_shot'] = 3
        kwargs['standing_setup'] = write(tmp_path, kwargs['standing_setup']['path'], setup)
    elif change.startswith('creator'):
        auth = json.loads((tmp_path / setup['authorization']['path']).read_text())
        auth['subject_sha256' if change == 'creator_subject' else 'identity_id'] = 'forged'
        setup['authorization'] = write(tmp_path, setup['authorization']['path'], auth)
        kwargs['standing_setup'] = write(tmp_path, kwargs['standing_setup']['path'], setup)
    command = json.loads((tmp_path / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = '0' * 64 if change == 'command_setup' else kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'command', command)
    with execution._lock(tmp_path): pass
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('payload', [{'first_cut': 'malformed-old-ignored-extra'},
    {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False,
     'first_cut': {'enabled': True}}])
def test_old_standing_activation_never_retrofits_ignored_episode_controls(tmp_path, monkeypatch, payload):
    from lib import episode_production_controls as controls
    kwargs = standing_controls_fixture(tmp_path, payload)
    # Reproduce only the old activation's omission in this synthetic fixture.
    with monkeypatch.context() as patch:
        patch.setattr(pa, '_standing_episode_controls', lambda *args: None)
        old = pa.activate_standing_run(tmp_path, **kwargs)
    assert controls.effective_controls(tmp_path) is None
    before = file_bytes(tmp_path)
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(old, replayed=True)
    assert file_bytes(tmp_path) == before


def test_standing_episode_controls_replay_preserves_later_creator_records(tmp_path):
    from lib import episode_production_controls as controls
    from tests.lib.test_episode_production_controls import approval
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False,
        'first_cut': {'enabled': True}}
    kwargs = standing_controls_fixture(tmp_path, payload)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    amended = {**payload, 'max_generations_per_shot': 1, 'first_cut': {'enabled': False}}
    controls.record_episode_controls(tmp_path, amended, evidence=approval(tmp_path, 'SYNTHETIC-later-controls'))
    controls.restrict_episode_provider(tmp_path, ['grok_cli'], evidence=approval(tmp_path, 'SYNTHETIC-restrict'),
        reason='SYNTHETIC prospective creator restriction')
    before = file_bytes(tmp_path)
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(activation, replayed=True)
    assert file_bytes(tmp_path) == before
    effective = controls.effective_controls(tmp_path)
    assert effective['max_generations_per_shot'] == 1 and effective['records'] == 3
    assert effective['restricted_providers'] == ['grok_cli'] and not controls.first_cut_enabled(tmp_path)
    assert pa.require_active_policy(tmp_path)[2] == activation['decision_id']


@pytest.mark.parametrize('stage', ['controls', 'evidence', 'policy', 'decision', 'after_decision'])
def test_standing_episode_controls_interrupted_fresh_activation_reuses_exact_record(tmp_path, monkeypatch, stage):
    from lib import episode_production_controls as controls
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False,
        'first_cut': {'enabled': True}}
    kwargs = standing_controls_fixture(tmp_path, payload)
    original_write = execution._write_new
    original_replace = Path.replace
    def fail_write(path, value):
        if (stage == 'evidence' and path.parent.name == 'approvals'
                or stage == 'policy' and path.name == 'autonomy_policy.json'):
            raise OSError('SYNTHETIC activation interruption')
        return original_write(path, value)
    def fail_replace(path, target):
        if Path(target).name == 'decision_log.json':
            if stage == 'after_decision': original_replace(path, target)
            raise OSError('SYNTHETIC activation interruption')
        return original_replace(path, target)
    with monkeypatch.context() as patch:
        if stage == 'controls':
            patch.setattr(controls, '_record_episode_controls_locked', lambda *a, **k: (_ for _ in ()).throw(OSError('SYNTHETIC activation interruption')))
        elif stage in {'evidence', 'policy'}: patch.setattr(execution, '_write_new', fail_write)
        else: patch.setattr(Path, 'replace', fail_replace)
        with pytest.raises(OSError, match='SYNTHETIC activation interruption'):
            pa.activate_standing_run(tmp_path, **kwargs)
    assert pa.load_active_policy(tmp_path)[0] is None or stage == 'after_decision'
    artifact = tmp_path / controls.ARTIFACT
    recorded = artifact.read_bytes() if artifact.exists() else None
    recovered = pa.activate_standing_run(tmp_path, **kwargs)
    assert pa.require_active_policy(tmp_path)[2] == recovered['decision_id']
    assert controls.effective_controls(tmp_path)['records'] == 1
    if recorded is not None: assert artifact.read_bytes() == recorded
    before = file_bytes(tmp_path)
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(recovered, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['remove', 'payload', 'record_identity', 'project', 'revoke'])
def test_standing_episode_controls_initial_binding_refuses_tampering_or_revocation(tmp_path, change):
    from lib import episode_production_controls as controls
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False}
    kwargs = standing_controls_fixture(tmp_path, payload)
    pa.activate_standing_run(tmp_path, **kwargs)
    path = tmp_path / controls.ARTIFACT
    if change == 'remove': path.unlink()
    elif change == 'revoke':
        log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
        log['decisions'][-1]['user_approved'] = False
        write(tmp_path, 'artifacts/decision_log.json', log)
    else:
        data = json.loads(path.read_text())
        if change == 'payload': data['records'][0]['controls']['max_generations_per_shot'] = 3
        elif change == 'record_identity': data['records'][0]['record_id'] = 'forged-record'
        else: data['project_id'] = 'foreign'
        write(tmp_path, str(controls.ARTIFACT), data)
    before = file_bytes(tmp_path)
    assert pa.load_active_policy(tmp_path)[0] is None
    with pytest.raises((pa.StandingSetupRequired, controls.EpisodeControlsError),
            match='initial episode controls|activation decision|controls record belongs to another project'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


def test_standing_episode_controls_refuse_different_preexisting_current_controls(tmp_path):
    from lib import episode_production_controls as controls
    from tests.lib.test_episode_production_controls import approval
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False}
    kwargs = standing_controls_fixture(tmp_path, payload)
    controls.record_episode_controls(tmp_path, {**payload, 'first_cut': {'enabled': True}},
        evidence=approval(tmp_path, 'SYNTHETIC-existing-controls'))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='initial episode controls differ'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


def test_standing_absent_controls_replay_preserves_manual_episode_controls(tmp_path):
    from lib import episode_production_controls as controls
    from tests.lib.test_episode_production_controls import approval
    kwargs = standing_fixture(tmp_path)
    activation = pa.activate_standing_run(tmp_path, **kwargs)
    controls.record_episode_controls(tmp_path, {'max_generations_per_shot': 1,
        'alternate_repair': 'disabled', 'access_fallback': False,
        'first_cut': {'enabled': False}}, evidence=approval(tmp_path, 'SYNTHETIC-manual-override'))
    before = file_bytes(tmp_path)
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(activation, replayed=True)
    assert file_bytes(tmp_path) == before
    assert controls.effective_controls(tmp_path)['records'] == 1
    assert not controls.first_cut_enabled(tmp_path)


def test_standing_episode_controls_validate_existing_log_before_authority_writes(tmp_path):
    from jsonschema import ValidationError
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False}
    kwargs = standing_controls_fixture(tmp_path, payload)
    write(tmp_path, 'artifacts/decision_log.json', {'version': '1.0',
        'project_id': kwargs['policy']['project_id'], 'decisions': [{'decision_id': 'malformed'}]})
    with execution._lock(tmp_path): pass
    before = file_bytes(tmp_path)
    with pytest.raises(ValidationError): pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


def test_standing_episode_controls_duplicate_valid_decisions_write_no_authority(tmp_path):
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False,
        'first_cut': {'enabled': True}}
    kwargs = standing_controls_fixture(tmp_path, payload)
    row = {'decision_id': 'duplicate-existing', 'stage': 'edit', 'category': 'composition_mode',
        'subject': 'SYNTHETIC unrelated composition choice', 'selected': 'fixture',
        'reason': 'SYNTHETIC schema-valid unrelated decision',
        'options_considered': [{'option_id': 'fixture', 'label': 'SYNTHETIC composition',
            'score': 1, 'reason': 'SYNTHETIC unrelated choice'}]}
    write(tmp_path, 'artifacts/decision_log.json', {'version': '1.0',
        'project_id': kwargs['policy']['project_id'], 'decisions': [row, copy.deepcopy(row)]})
    with execution._lock(tmp_path): pass
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError, match='duplicate decision IDs'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


def test_standing_episode_controls_interrupted_full_decision_id_collision_writes_nothing(tmp_path, monkeypatch):
    from lib import episode_production_controls as controls
    payload = {'max_generations_per_shot': 1, 'alternate_repair': 'disabled', 'access_fallback': False,
        'first_cut': {'enabled': True}}
    kwargs = standing_controls_fixture(tmp_path, payload)
    original_replace = Path.replace
    def interrupt(path, target):
        if Path(target).name == 'decision_log.json': raise OSError('SYNTHETIC before decision publication')
        return original_replace(path, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', interrupt)
        with pytest.raises(OSError, match='SYNTHETIC before decision publication'):
            pa.activate_standing_run(tmp_path, **kwargs)
    policy = json.loads((tmp_path / pa.POLICY_ARTIFACT).read_text())
    sha = pa.policy_sha256(policy, pa.retained_baselines(tmp_path, policy))
    initial = json.loads((tmp_path / controls.ARTIFACT).read_text())['records'][0]
    collision = 'standing-' + sha[:24] + '-controls-' + request.digest(initial)
    row = {'decision_id': collision, 'stage': 'edit', 'category': 'composition_mode',
        'subject': 'SYNTHETIC unrelated composition choice', 'selected': 'fixture',
        'reason': 'SYNTHETIC unique existing row owns the full candidate ID',
        'options_considered': [{'option_id': 'fixture', 'label': 'SYNTHETIC composition',
            'score': 1, 'reason': 'SYNTHETIC unrelated choice'}]}
    write(tmp_path, 'artifacts/decision_log.json', {'version': '1.0',
        'project_id': kwargs['policy']['project_id'], 'decisions': [row]})
    assert pa._decision_entries(tmp_path) == [row]
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='activation decision ID conflicts'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


def standing_stage_fixture(root, stages):
    kwargs = standing_fixture(root)
    kwargs['policy']['checkpoint_stages'] = stages
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization'); setup['checkpoint_stages'] = stages
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization'); command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    return kwargs


def delivery_admission_fixture(root, *, profile='tiktok_720p', retained_ratio=...,
                               approved=True, scene_approval=None, metadata_delivery=None,
                               ratio_lock=None):
    """Synthetic exact approved delivery chain; no real consent or qualification."""
    kwargs = standing_fixture(root)
    proposal = write(root, 'notes/delivery-proposal.json', {
        'version': '1.0', 'identity_id': 'fixture-identity',
        'delivery': {'profile': profile} if profile is not None else {}})
    approval = write(root, 'notes/delivery-approval.json', {
        'version': '1.0', 'status': 'approved' if approved else 'proposed',
        'approved_proposal': proposal})
    for name in ('script', 'scene_plan'):
        value = json.loads((root / 'artifacts' / (name + '.json')).read_text())
        value['metadata'] = {'approval': scene_approval if name == 'scene_plan' and scene_approval else approval}
        if metadata_delivery is not None:
            value['metadata']['delivery'] = copy.deepcopy(metadata_delivery)
        write(root, 'artifacts/' + name + '.json', value)
    projection = pa.current_projection(root, 'entry')
    template_inputs = {'duration': 8, 'image_path': str(root / 'assets/start.svg')}
    if retained_ratio is not ...:
        template_inputs['aspect_ratio'] = retained_ratio
    kwargs['baselines']['entry'].update(projection=projection,
        script_snapshot=json.loads((root / 'artifacts/script.json').read_text()),
        planned_request_template=execution.planned_request_template(template_inputs, project_dir=root))
    mcp = {'id': 'openart_mcp', 'billing': 'unknown_cost_no_ceiling',
        'exposure_acknowledgement': 'no_enforceable_credit_ceiling',
        'uid_sha256': request.digest('fixture-mcp-account'), 'project_id': 'fixture-mcp',
        'routes': [{'model': 'byte-plus-seedance-2-mini', 'mode': 'image2video'},
                   {'model': 'smart-shot', 'mode': 'generate-shot-video'}]}
    kwargs['policy']['providers'].append(mcp)
    if ratio_lock is not None:
        kwargs['policy']['locked']['controls']['aspect_ratio'] = ratio_lock
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup['providers'] = kwargs['policy']['providers']
    setup['provider_accounts']['openart_mcp'] = 'fixture-mcp-account'
    setup['source_transfer'].append({**setup['source_transfer'][0],
        'provider': 'openart_mcp', 'account': 'fixture-mcp-account'})
    kwargs['standing_setup'] = creator_fixture(root, 'standing-delivery', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command-delivery', command)
    activation = pa.activate_standing_run(root, **kwargs)
    inputs = {**template_inputs, 'project_dir': str(root), 'prompt': 'Synthetic fixture',
        'operation': 'reference_to_video', 'compiled_request_id': 'fixture',
        'preparation_review_id': 'fixture', 'governance': {'shot_id': 'entry'}}
    return kwargs, activation, inputs


@pytest.mark.parametrize('provider', ['grok_cli', 'openart_mcp'])
def test_approved_delivery_portrait_enters_real_scope_derivation(tmp_path, monkeypatch, provider):
    kwargs, activation, inputs = delivery_admission_fixture(tmp_path)
    inputs['aspect_ratio'] = '9:16'
    if provider == 'openart_mcp':
        inputs.update(model='byte-plus-seedance-2-mini', mode='image2video', operation='image_to_video')
        # Exact admission/retention is real; downstream native/preparation/billing
        # fixtures do not establish provider readiness or a semantic review.
        monkeypatch.setattr(pa, '_prepare_mcp_policy_native', lambda *a: ({}, {}))
        monkeypatch.setattr(pa, '_mcp_policy_terms', lambda *a: {'synthetic_fixture': True})
    monkeypatch.setattr(request, 'validate_preparation', lambda *a: {'compiled_sha256': request.digest('synthetic')})
    monkeypatch.setattr(pa, 'lock_proof', lambda *a, **kw: request.digest('synthetic'))
    frozen = file_bytes(tmp_path)
    scope = pa.derive_scope(tmp_path, inputs, provider=provider,
        observation={'cli_version': '1.0.34', 'grok_path': '/synthetic/grok'})
    assert scope['derived_from_policy']['policy_sha256'] == activation['policy_sha256']
    assert scope['requests']['entry'] == execution.planned_request_digest(inputs, project_dir=tmp_path)
    assert all((tmp_path / path).read_bytes() == raw for path, raw in frozen.items()
               if path != Path('artifacts/decision_log.json'))
    # A prospective scope occupies its existing allowance without a submission.
    assert execution._attempts(tmp_path) == []
    assert pa.root_attempt_counts(tmp_path, kwargs['policy'])['total'] == 1
    assert images.image_usage(tmp_path)['total'] == 0


@pytest.mark.parametrize('provider,model,ratio_location', [
    ('grok_cli', None, 'public'), ('openart_cli', 'kling-3', 'public'),
    ('openart_cli', 'kling-3', 'aspectRatio'),
    ('openart_mcp', 'byte-plus-seedance-2-mini', 'public'),
    ('openart_mcp', 'byte-plus-seedance-2-mini', 'aspectRatio'),
    ('openart_mcp', 'smart-shot', 'videoAspectRatio')])
@pytest.mark.parametrize('profile,ratio', [('tiktok_720p', '9:16'), ('generic_hd', '16:9'), ('instagram_feed', '1:1')])
def test_exact_delivery_format_admission_across_transport_aliases(tmp_path, provider, model, ratio_location, profile, ratio):
    _, _, inputs = delivery_admission_fixture(tmp_path, profile=profile)
    if model:
        inputs['model'] = model
    if ratio_location == 'public':
        inputs['aspect_ratio'] = ratio
    else:
        inputs['native_params'] = {ratio_location: ratio}
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    before = file_bytes(tmp_path)
    pa._validate_request_delta(tmp_path, policy, 'entry', inputs, provider)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', [
    {'aspect_ratio': '16:9'}, {'aspect_ratio': '1:1'},
    {'aspect_ratio': '9:16', 'native_params': {'aspectRatio': '9:16'}},
    {'aspect_ratio': '9:16', 'native_params': {'aspectRatio': '16:9'}},
    {'native_params': {'aspectRatio': '9:16', 'videoAspectRatio': '9:16'}},
    {'aspect_ratio': '9:16', 'native_params': {'seed': 42}},
    {'native_params': {'aspect_ratio': '9:16'}},
    {'aspect_ratio': '9:16', 'aspectRatio': '9:16'}])
def test_delivery_admission_keeps_wrong_duplicate_and_unrelated_controls_rejected(tmp_path, monkeypatch, change):
    _, _, inputs = delivery_admission_fixture(tmp_path)
    inputs.update(model='byte-plus-seedance-2-mini', mode='image2video', operation='image_to_video', **change)
    monkeypatch.setattr(pa, '_prepare_mcp_policy_native', lambda *a: pytest.fail('native preparation reached after invalid admission'))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError):
        pa.derive_scope(tmp_path, inputs, provider='openart_mcp')
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('profile,approved,scene_approval', [
    (None, True, None), ('unknown-profile', True, None), ('cinematic', True, None),
    ('generic_hd', True, None), ('tiktok_720p', False, None),
    ('tiktok_720p', True, {'path': 'notes/foreign.json', 'sha256': 'f' * 64})])
def test_new_ratio_requires_agreeing_exact_approved_delivery(tmp_path, profile, approved, scene_approval):
    _, _, inputs = delivery_admission_fixture(tmp_path, profile=profile, approved=approved, scene_approval=scene_approval)
    inputs['aspect_ratio'] = '9:16'
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('path', ['notes/delivery-proposal.json', 'notes/delivery-approval.json'])
def test_delivery_authority_bytes_cannot_be_tampered(tmp_path, path):
    _, _, inputs = delivery_admission_fixture(tmp_path)
    inputs['aspect_ratio'] = '9:16'
    (tmp_path / path).write_text('{}')
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(tmp_path) == before


def test_delivery_profile_cannot_change_explicit_retained_native_ratio(tmp_path):
    _, _, inputs = delivery_admission_fixture(tmp_path, retained_ratio='16:9')
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    before = file_bytes(tmp_path)
    inputs['aspect_ratio'] = '9:16'
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    inputs['aspect_ratio'] = '16:9'
    pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('retained_ratio', ['', None])
def test_empty_or_null_retained_ratio_is_not_an_absent_control(tmp_path, retained_ratio):
    _, _, inputs = delivery_admission_fixture(tmp_path, retained_ratio=retained_ratio)
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    inputs['aspect_ratio'] = '9:16'
    before = file_bytes(tmp_path)
    with pytest.raises(pa.AutonomyError, match='aspect ratio must be an explicit native string'):
        pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('declaration', [{'profile': 'generic_hd'}, {'aspect_ratio': '16:9'}, {'width': 1080}])
def test_retained_delivery_declarations_cannot_contradict_approved_profile(tmp_path, declaration):
    _, _, inputs = delivery_admission_fixture(tmp_path, metadata_delivery=declaration)
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    inputs['aspect_ratio'] = '9:16'
    with pytest.raises(pa.AutonomyError, match='format declarations disagree'):
        pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')


@pytest.mark.parametrize('locked_ratio,admitted', [('9:16', True), ('16:9', False)])
def test_delivery_projection_honors_hash_only_native_ratio_lock(tmp_path, locked_ratio, admitted):
    _, _, inputs = delivery_admission_fixture(tmp_path,
        ratio_lock={'value_sha256': hashlib.sha256(locked_ratio.encode()).hexdigest()})
    policy = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    inputs['aspect_ratio'] = '9:16'
    before = file_bytes(tmp_path)
    if admitted:
        pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    else:
        with pytest.raises(pa.AutonomyError, match='aspect ratio lock'):
            pa._validate_request_delta(tmp_path, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(tmp_path) == before


def standing_proposal_delivery_fixture(root, *, profile='tiktok_720p', full_native=False, package_binding=None):
    """Canonical delegated proposal checkpoint; every verdict is synthetic."""
    from lib.checkpoint import write_checkpoint
    from tests.contracts.test_phase0_contracts import sample_artifact
    kwargs = standing_fixture(root)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    for name in ('script', 'scene_plan'):
        value = json.loads((root / 'artifacts' / (name + '.json')).read_text())
        value['metadata'] = {'approval': copy.deepcopy(command['authorization'])}
        if package_binding is not None:
            value['metadata']['package'] = copy.deepcopy(package_binding)
        write(root, 'artifacts/' + name + '.json', value)
    kwargs['baselines']['entry'].update(projection=pa.current_projection(root, 'entry'),
        script_snapshot=json.loads((root / 'artifacts/script.json').read_text()))
    from lib.shot_contract import required_asset_ids
    contract = execution.load_shot_contract(root)
    consumer = required_asset_ids(contract, contract['shots'][0])
    references = [str(root / asset['path']) for asset in contract['assets']
                  if asset['id'] in consumer and asset['id'] != 'start'] if full_native else []
    if references:
        kwargs['baselines']['entry']['planned_request_template'] = execution.planned_request_template(
            {'duration': 8, 'image_path': str(root / 'assets/start.svg'), 'reference_image_paths': references}, project_dir=root)
    active = pa.activate_standing_run(root, **kwargs)
    proposal = sample_artifact('proposal_packet')
    proposal['approval']['status'] = 'pending'
    proposal['production_plan'].update(pipeline='cinematic', render_runtime='ffmpeg')
    proposal['metadata'] = {'identity_id': 'fixture-identity',
        'project_id': kwargs['policy']['project_id'], 'story_revision': kwargs['policy']['story_revision'],
        'delivery': {'profile': profile}}
    if package_binding is not None:
        proposal['metadata']['package'] = copy.deepcopy(package_binding)
    artifact = write(root, 'artifacts/proposal_packet.json', proposal)
    review = write(root, 'notes/SYNTHETIC-proposal-review.json', {'version': '1.0', 'stage': 'proposal',
        'artifact_path': artifact['path'], 'artifact_sha256': artifact['sha256'],
        'critical_findings': [], 'findings': []})
    preauth = {'artifact_path': artifact['path'], 'artifact_sha256': artifact['sha256'],
        'review_path': review['path'], 'review_sha256': review['sha256'], 'critical_findings': []}
    basis = {'kind': 'supplied_script_scenes', 'artifacts': [
        {'path': 'artifacts/' + name + '.json',
         'sha256': hashlib.sha256((root / 'artifacts' / (name + '.json')).read_bytes()).hexdigest()}
        for name in ('script', 'scene_plan')]}
    write_checkpoint(root.parent, root.name, 'research', 'completed', {},
        pipeline_type='cinematic', metadata={'supplied_basis': basis})
    approval = {'kind': 'policy', 'policy_sha256': active['policy_sha256'], 'decision_id': active['decision_id']}
    write_checkpoint(root.parent, root.name, 'proposal', 'completed', {'proposal_packet': proposal},
        pipeline_type='cinematic', approval_basis=approval, review={'preauth': preauth})
    pa.validate_preauth(root, 'proposal', approval, preauth)
    inputs = {'duration': 8, 'image_path': str(root / 'assets/start.svg'), 'aspect_ratio': '9:16',
        'project_dir': str(root), 'prompt': 'Synthetic fixture', 'operation': 'reference_to_video',
        'compiled_request_id': 'fixture', 'preparation_review_id': 'fixture', 'governance': {'shot_id': 'entry'}}
    if references:
        inputs['reference_image_paths'] = references
    return kwargs, active, inputs


def test_standing_policy_proposal_delivery_enters_real_scope_derivation(tmp_path, monkeypatch):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    kwargs, active, inputs = standing_proposal_delivery_fixture(root)
    # Native/preparation/semantic readiness is a separate fixture boundary;
    # standing activation and canonical proposal checkpoint admission are real.
    monkeypatch.setattr(request, 'validate_preparation', lambda *a: {'compiled_sha256': request.digest('synthetic')})
    monkeypatch.setattr(pa, 'lock_proof', lambda *a, **kw: request.digest('synthetic'))
    before = file_bytes(root)
    scope = pa.derive_scope(root, inputs, provider='grok_cli',
        observation={'cli_version': '1.0.34', 'grok_path': '/synthetic/grok'})
    assert scope['derived_from_policy']['policy_sha256'] == active['policy_sha256']
    assert scope['requests']['entry'] == execution.planned_request_digest(inputs, project_dir=root)
    proof = scope['derived_from_policy']['delivery_approval']
    assert scope['derived_from_policy']['delivery_approval_sha256'] == request.digest(proof)
    assert scope['id'].endswith(request.digest(proof)[:12])
    assert json.loads(proof['artifact']['content'])['approval']['status'] == 'pending'
    assert all((root / path).read_bytes() == raw for path, raw in before.items()
               if path != Path('artifacts/decision_log.json'))
    assert execution._attempts(root) == []
    assert pa.root_attempt_counts(root, kwargs['policy'])['total'] == 1


def refresh_fixture_proposal(root, mutate):
    """Rebind isolated synthetic review bytes to exercise the named inner gate."""
    proposal = json.loads((root / 'artifacts/proposal_packet.json').read_text())
    mutate(proposal)
    artifact = write(root, 'artifacts/proposal_packet.json', proposal)
    review = json.loads((root / 'notes/SYNTHETIC-proposal-review.json').read_text())
    review['artifact_sha256'] = artifact['sha256']
    retained = write(root, 'notes/SYNTHETIC-proposal-review.json', review)
    saved = json.loads((root / 'checkpoint_proposal.json').read_text())
    saved['artifacts']['proposal_packet'] = proposal
    saved['review']['preauth'].update(artifact_sha256=artifact['sha256'], review_sha256=retained['sha256'])
    write(root, 'checkpoint_proposal.json', saved)


@pytest.mark.parametrize('change', ['missing_checkpoint', 'missing_artifact', 'missing_review', 'supplied_only',
    'wrong_policy', 'wrong_decision', 'identity', 'project', 'story', 'unknown_profile', 'no_profile',
    'ratio_conflict', 'rejected', 'critical_review', 'malformed_review', 'embedded_artifact', 'ratio_alias',
    'wrong_ratio', 'unrelated_control', 'revoked'])
def test_standing_delivery_proposal_refuses_unbound_conflicting_or_revoked_evidence(tmp_path, change):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    _, _, inputs = standing_proposal_delivery_fixture(root)
    if change.startswith('missing_'):
        name = {'missing_checkpoint': 'checkpoint_proposal.json', 'missing_artifact': 'artifacts/proposal_packet.json',
                'missing_review': 'notes/SYNTHETIC-proposal-review.json'}[change]
        (root / name).unlink()
    elif change in {'identity', 'project', 'story', 'unknown_profile', 'no_profile', 'ratio_conflict', 'rejected'}:
        def mutate(proposal):
            if change in {'identity', 'project', 'story'}:
                proposal['metadata'][{'identity': 'identity_id', 'project': 'project_id', 'story': 'story_revision'}[change]] = 'foreign'
            elif change == 'unknown_profile': proposal['metadata']['delivery']['profile'] = 'unknown-profile'
            elif change == 'no_profile': proposal['metadata']['delivery'] = {}
            elif change == 'ratio_conflict': proposal['metadata']['delivery']['aspect_ratio'] = '16:9'
            else: proposal['approval']['status'] = 'rejected'
        refresh_fixture_proposal(root, mutate)
    elif change in {'critical_review', 'malformed_review'}:
        review = json.loads((root / 'notes/SYNTHETIC-proposal-review.json').read_text())
        if change == 'critical_review': review['critical_findings'] = ['SYNTHETIC actual critical defect']
        else: review.pop('findings')
        binding = write(root, 'notes/SYNTHETIC-proposal-review.json', review)
        saved = json.loads((root / 'checkpoint_proposal.json').read_text())
        saved['review']['preauth']['review_sha256'] = binding['sha256']
        write(root, 'checkpoint_proposal.json', saved)
    elif change in {'supplied_only', 'wrong_policy', 'wrong_decision', 'embedded_artifact'}:
        saved = json.loads((root / 'checkpoint_proposal.json').read_text())
        if change == 'supplied_only': saved['artifacts'] = {}; saved.pop('review')
        elif change == 'wrong_policy': saved['metadata']['approval_basis']['policy_sha256'] = '1' * 64
        elif change == 'wrong_decision': saved['metadata']['approval_basis']['decision_id'] = 'foreign'
        else: saved['artifacts']['proposal_packet']['metadata']['delivery']['profile'] = 'generic_hd'
        write(root, 'checkpoint_proposal.json', saved)
    elif change == 'ratio_alias': inputs['native_params'] = {'aspect_ratio': '9:16'}
    elif change == 'wrong_ratio': inputs['aspect_ratio'] = '16:9'
    elif change == 'unrelated_control': inputs['unapproved_control'] = True
    else:
        log = json.loads((root / 'artifacts/decision_log.json').read_text())
        log['decisions'].append(dict(log['decisions'][0], decision_id='SYNTHETIC-revocation', selected='strict'))
        write(root, 'artifacts/decision_log.json', log)
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError):
        policy, _, _ = pa.require_active_policy(root)
        pa._validate_request_delta(root, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(root) == before


def test_standing_delivery_original_proof_survives_current_proposal_and_revocation(tmp_path, monkeypatch):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    _, _, inputs = standing_proposal_delivery_fixture(root)
    observation = {'cli_version': '1.0.34', 'grok_path': '/synthetic/grok'}
    monkeypatch.setattr(request, 'validate_preparation', lambda *a: {'compiled_sha256': request.digest('synthetic')})
    monkeypatch.setattr(pa, 'lock_proof', lambda *a, **kw: request.digest('synthetic'))
    monkeypatch.setattr(pa, '_historical_current_preparation', lambda *a: {'compiled_sha256': request.digest('synthetic')})
    monkeypatch.setattr(pa, '_root_lock_proof', lambda *a, **kw: request.digest('synthetic'))
    scope = pa.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    pa.validate_derived_scope(root, scope, inputs=inputs, observation=observation)
    refresh_fixture_proposal(root, lambda p: p['metadata']['delivery'].update(profile='generic_hd'))
    with pytest.raises(pa.AutonomyError, match='current delivery approval differs'):
        pa.validate_derived_scope(root, scope, inputs=inputs, observation=observation)
    log = json.loads((root / 'artifacts/decision_log.json').read_text())
    log['decisions'].append(dict(log['decisions'][0], decision_id='SYNTHETIC-revocation', selected='strict'))
    write(root, 'artifacts/decision_log.json', log)
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError, match='revoked'):
        pa._scope_material(root, inputs, 'grok_cli', observation)
    policy, sha, decision, material = pa._scope_material(root, inputs, 'grok_cli', observation, historical_scope=scope)
    assert pa._scope_record(policy, sha, decision, derivation_index=0, **material) == scope
    with pytest.raises(pa.AutonomyError, match='ratio differs'):
        pa._scope_material(root, dict(inputs, aspect_ratio='16:9'), 'grok_cli', observation, historical_scope=scope)
    tampered = copy.deepcopy(scope)
    tampered['derived_from_policy']['delivery_approval']['artifact']['content'] += ' '
    with pytest.raises(pa.AutonomyError, match='proof hash differs'):
        pa._scope_material(root, inputs, 'grok_cli', observation, historical_scope=tampered)
    tampered['derived_from_policy']['delivery_approval_sha256'] = request.digest(tampered['derived_from_policy']['delivery_approval'])
    with pytest.raises(pa.AutonomyError, match='proof bytes changed'):
        pa._scope_material(root, inputs, 'grok_cli', observation, historical_scope=tampered)
    assert file_bytes(root) == before


def test_manual_delivery_conflict_cannot_fallback_to_policy_checkpoint(tmp_path):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    standing_proposal_delivery_fixture(root)
    policy, _, _ = pa.require_active_policy(root)
    retained = copy.deepcopy(pa.retained_baselines(root, policy)['entry'])
    proposal = write(root, 'notes/SYNTHETIC-manual-proposal.json', {'version': '1.0',
        'identity_id': 'fixture-identity', 'delivery': {'profile': 'generic_hd'}})
    approval = write(root, 'notes/SYNTHETIC-manual-approval.json', {'version': '1.0',
        'status': 'proposed', 'approved_proposal': proposal})
    retained['script_snapshot']['metadata']['approval'] = approval
    retained['projection']['scene_snapshot']['metadata']['approval'] = approval
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError, match='delivery proposal is not approved'):
        pa._retained_delivery_aspect_ratio(root, policy, retained)
    assert file_bytes(root) == before


@pytest.mark.parametrize('profile,ratio', [('generic_hd', '16:9'), ('instagram_feed', '1:1')])
def test_standing_delivery_known_nonportrait_profiles(tmp_path, profile, ratio):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    _, _, inputs = standing_proposal_delivery_fixture(root, profile=profile)
    inputs['aspect_ratio'] = ratio
    policy, _, _ = pa.require_active_policy(root)
    before = file_bytes(root)
    assert pa._validate_request_delta(root, policy, 'entry', inputs, 'grok_cli')['kind'] == 'policy_proposal'
    assert file_bytes(root) == before


@pytest.mark.parametrize('change', [None, 'different', 'missing', 'changed_bytes'])
def test_standing_delivery_binds_the_exact_retained_package(tmp_path, change):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    package = write(root, 'notes/SYNTHETIC-retained-package.json', {'SYNTHETIC': 'Exact frozen authoring binding fixture'})
    _, _, inputs = standing_proposal_delivery_fixture(root, package_binding=package)
    if change == 'different':
        refresh_fixture_proposal(root, lambda p: p['metadata'].update(package={'path': package['path'], 'sha256': '1' * 64}))
    elif change == 'missing': refresh_fixture_proposal(root, lambda p: p['metadata'].pop('package'))
    elif change == 'changed_bytes': (root / package['path']).write_text('{}')
    policy, _, _ = pa.require_active_policy(root)
    before = file_bytes(root)
    if change is None:
        assert pa._validate_request_delta(root, policy, 'entry', inputs, 'grok_cli')['kind'] == 'policy_proposal'
    else:
        with pytest.raises(pa.AutonomyError, match='package'):
            pa._validate_request_delta(root, policy, 'entry', inputs, 'grok_cli')
    assert file_bytes(root) == before


def test_standing_delivery_complete_grok_preparation_and_scope_replay(tmp_path):
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    _, _, inputs = standing_proposal_delivery_fixture(root, full_native=True)
    observation = {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'}
    authored = request.compile_provider_prompt(root, 'entry', provider='grok_cli')
    inputs['prompt'] = authored['prompt']
    native = request.prepare_grok_native(inputs, observation)
    shot = execution.load_shot_contract(root)['shots'][0]
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC software fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC serial windows',
        'segments': [{'dialogue_index': i, 'text_sha256': request.digest(line['text']), 'language': 'en',
            'word_count': 2, 'words_per_minute': 180, 'pause_seconds': 0, 'rationale': 'SYNTHETIC fixture'}
            for i, line in enumerate(shot['dialogue'])],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key, 'value_sha256': request.digest(shot[key]),
            'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC fixture'}
            for key, start, end in [('dominant_action', 4.1, 7), ('completed_end_state', 7, 7.4)]]}
    compiled = request.prepare_compiled_request(inputs, native, {'source': 'real'}, coverage=authored['coverage'], timing=timing)
    write(root, 'artifacts/compiled_request-fixture.json', compiled)
    write(root, 'artifacts/preparation_review-fixture.json', {'version': '1.0', 'review_id': 'fixture',
        'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass', 'subject_sha256': request.digest(compiled),
        'evidence_kind': 'reviewed', 'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC software fixture only'} for p in sorted(request.PREDICATES)]})
    request.validate_preparation(inputs, native, {'source': 'real'})
    before = file_bytes(root)
    scope = pa.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    pa.validate_derived_scope(root, scope, inputs=inputs, observation=observation)
    assert scope['derived_from_policy']['compiled_request_sha256'] == request.digest(compiled)
    assert all((root / path).read_bytes() == raw for path, raw in before.items()
               if path != Path('artifacts/decision_log.json'))
    assert execution._attempts(root) == []


def legacy_ungated_standing_activation(root, monkeypatch):
    kwargs = standing_stage_fixture(root, ['proposal', 'script', 'scene_plan', 'assets', 'edit', 'compose'])
    # Reproduce only the old writer defect; all other canonical checks remain real.
    with monkeypatch.context() as patch:
        patch.setattr(pa, '_validate_checkpoint_stages', lambda *args: None, raising=False)
        activation = pa.activate_standing_run(root, **kwargs)
    assert pa.load_active_policy(root)[3] == ['checkpoint stages are not actual gated pipeline stages']
    return kwargs, activation


def file_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def standing_treatment_fixture(root, *, guided_routes=(), mode_location='project', treatment='reference_guided'):
    """Exact synthetic route pool over actual authored, possibly pending sources."""
    from lib import production_boards as boards
    if not (root / 'artifacts/shot_contract.json').exists():
        standing_fixture(root)
    contract = execution.load_shot_contract(root)
    if mode_location == 'project':
        contract['reference_mode'] = treatment
    else:
        contract['shots'][0]['reference_mode'] = treatment
    write(root, 'artifacts/shot_contract.json', contract)
    marker = json.loads((root / 'project.json').read_text())
    marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    policy = make_policy(project_id=contract['project_id'], story_revision=contract['story_revision'],
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}},
        flex={'duration_s': [1, 10], 'resolution': [], 'references': {'droppable_roles': [], 'substitutes': []}})
    policy['providers'] = [copy.deepcopy(policy['providers'][1]), {
        'id': 'openart_mcp', 'billing': 'unknown_cost_no_ceiling',
        'exposure_acknowledgement': 'no_enforceable_credit_ceiling', 'uid_sha256': request.digest('fixture-only'),
        'project_id': 'fixture-mcp-project', 'routes': [
            {'model': 'byte-plus-seedance-2-mini', 'mode': 'image2video'}, *copy.deepcopy(guided_routes)]}]
    script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        baselines[shot['id']] = {'projection': projection,
            'planned_request_template': execution.planned_request_template({'duration': shot['duration_seconds']}, project_dir=root),
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    accounts = {p['id']: 'fixture-' + p['id'] for p in policy['providers']}
    pending = (root / boards.ARTIFACT).exists()
    if pending:
        accounts['native_imagegen'] = 'fixture-native-account'
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [a['sha256'] for a in contract['assets'] if a.get('sha256')],
        'required_permissions': [], 'source_transfer': [{'provider': provider, 'account': account,
            'roles': ['identity', 'identity_reference', 'start_frame', 'end_frame', 'payoff_board',
                      'reference_image', 'reference_video', 'reference_audio'],
            'generated_board_sources': pending, 'max_files': 8, 'max_file_bytes': 10000,
            'max_batch_bytes': 80000, 'max_batches': 4} for provider, account in accounts.items()]}
    if pending:
        setup['approved_source_sha256'].extend(r['sha256'] for r in boards.load_board_pack(root)['specification']['references'])
        setup['native_images'] = {'account': accounts['native_imagegen'], 'image_allowance': 4,
                                  'allowed_image_routes': ['native_imagegen']}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'], 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    return dict(policy=policy, baselines=baselines, standing_setup=standing, run_command=command,
                identity_id='fixture-identity')


@pytest.mark.parametrize('mode_location', ['project', 'shot'])
def test_standing_guided_treatment_rejects_entire_frame_only_pool_before_authority_writes(tmp_path, mode_location):
    kwargs = standing_treatment_fixture(tmp_path, mode_location=mode_location)
    # Activation's existing filesystem lock is mechanical, not media authority.
    with execution._lock(tmp_path):
        pass
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='entry.*openart_mcp.*byte-plus-seedance-2-mini.*image2video'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before
    assert not (tmp_path / pa.POLICY_ARTIFACT).exists()
    assert not list((tmp_path / 'approvals').glob('*'))
    assert not (tmp_path / 'artifacts/decision_log.json').exists()
    assert not (tmp_path / 'production_scopes.json').exists()
    assert images.image_usage(tmp_path)['total'] == 0


@pytest.mark.parametrize('route', [
    {'model': 'minimax-h3', 'mode': 'element2video'},
    {'model': 'smart-shot', 'mode': 'generate-shot-video'}])
def test_standing_guided_treatment_allows_mixed_pool_and_replays_without_new_preflight(tmp_path, guided_observations, monkeypatch, route):
    kwargs = standing_treatment_fixture(tmp_path, guided_routes=[route])
    # Sparse guided sources retain identity without a mandatory board pair.
    contract = execution.load_shot_contract(tmp_path)
    contract['shots'][0]['asset_ids'] = ['patient']
    write(tmp_path, 'artifacts/shot_contract.json', contract)
    kwargs = standing_treatment_fixture(tmp_path, guided_routes=[route])
    original_pool = copy.deepcopy(kwargs['policy']['providers'])
    original_sources = file_bytes(tmp_path)
    result = pa.activate_standing_run(tmp_path, **kwargs)
    active = pa.require_active_policy(tmp_path, shot_id='entry')[0]
    assert active['providers'] == original_pool and result['provider_calls'] == 0
    assert all((tmp_path / path).read_bytes() == raw for path, raw in original_sources.items())
    monkeypatch.setattr(pa, '_validate_standing_route_treatments', lambda *args: pytest.fail('retained activation rechecked prospective feasibility'))
    before = file_bytes(tmp_path)
    assert pa.load_active_policy(tmp_path)[0] is not None
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('restriction', ['intent', 'model_lock'])
def test_standing_guided_treatment_compatible_route_must_be_allowed_for_this_shot(tmp_path, guided_observations, restriction):
    kwargs = standing_treatment_fixture(tmp_path, guided_routes=[{'model': 'minimax-h3', 'mode': 'element2video'}])
    if restriction == 'intent':
        kwargs['policy']['model_selection_intents'] = {'entry': {'mode': 'exact', 'provider': 'openart_mcp',
            'model': 'byte-plus-seedance-2-mini', 'approved_pool': [
                {'provider': 'openart_mcp', 'model': 'byte-plus-seedance-2-mini'},
                {'provider': 'openart_mcp', 'model': 'minimax-h3'}]}}
    else:
        kwargs['policy']['locked']['controls']['model'] = {'value': 'byte-plus-seedance-2-mini'}
    with execution._lock(tmp_path):
        pass
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='entry.*minimax-h3.*excludes route'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('compatible', [False, True])
def test_standing_guided_treatment_checks_actual_native_reference_roles(tmp_path, guided_observations, compatible):
    standing_fixture(tmp_path)
    contract = execution.load_shot_contract(tmp_path)
    next(asset for asset in contract['assets'] if asset['id'] == 'patient')['role'] = 'reference_video'
    contract['shots'][0]['asset_ids'] = ['patient']
    write(tmp_path, 'artifacts/shot_contract.json', contract)
    model = 'minimax-h3' if compatible else 'veo3-1'
    kwargs = standing_treatment_fixture(tmp_path, guided_routes=[{'model': model, 'mode': 'element2video'}])
    if compatible:
        assert pa.activate_standing_run(tmp_path, **kwargs)['provider_calls'] == 0
    else:
        with pytest.raises(pa.StandingSetupRequired, match='entry.*veo3-1.*reference_video.*supported native role'):
            pa.activate_standing_run(tmp_path, **kwargs)
        assert not (tmp_path / pa.POLICY_ARTIFACT).exists()


def test_standing_guided_treatment_honors_per_shot_reference_free_override(tmp_path):
    standing_fixture(tmp_path)
    contract = execution.load_shot_contract(tmp_path)
    shot = contract['shots'][0]
    shot.update(reference_mode='reference_free', cast_ids=[], dialogue=[], required_visible_speakers=[], asset_ids=[], upstream=[])
    write(tmp_path, 'artifacts/shot_contract.json', contract)
    kwargs = standing_treatment_fixture(tmp_path)
    assert kwargs['baselines']['entry']['projection']['contract_snapshot']['reference_mode'] == 'reference_guided'
    assert pa.activate_standing_run(tmp_path, **kwargs)['provider_calls'] == 0


@pytest.mark.parametrize('route', [None,
    {'model': 'minimax-h3', 'mode': 'element2video'},
    {'model': 'smart-shot', 'mode': 'generate-shot-video'}])
def test_standing_guided_treatment_preflight_uses_pending_unobserved_authored_boards(neutral_package, guided_observations, monkeypatch, route):
    root, package, bind = neutral_package
    package = prospective_package(package)
    template = package['boards']['motion_binding']['contract_template']
    template['reference_mode'] = 'reference_guided'
    # Actual sparse authoring: first shot has a starting composition target only.
    package['boards']['shots'][0]['roles'] = [{'asset_id': 'start', 'role': 'start_frame'}]
    template['shots'][0]['asset_ids'] = ['start']
    template['assets'] = [a for a in template['assets'] if a['id'] != 'end']
    package['boards']['image_nodes'] = [a for a in package['boards']['image_nodes'] if a['slot_id'] != 'end']
    request.prepare_package(root, bind(package))
    kwargs = standing_treatment_fixture(root, guided_routes=[route] if route else [])
    assert all(a.get('preparation_pending') for a in execution.load_shot_contract(root)['assets'])
    monkeypatch.setattr(request, 'source_packet', lambda *args, **kw: pytest.fail('prospective route check requires reviewed media'))
    with execution._lock(root):
        pass
    before = file_bytes(root)
    if route:
        assert pa.activate_standing_run(root, **kwargs)['provider_calls'] == 0
        assert all((root / path).read_bytes() == raw for path, raw in before.items())
    else:
        with pytest.raises(pa.StandingSetupRequired, match='s1.*image2video.*s2.*image2video.*s3.*image2video'):
            pa.activate_standing_run(root, **kwargs)
        assert file_bytes(root) == before
        assert not (root / pa.POLICY_ARTIFACT).exists()
        assert not (root / 'artifacts/decision_log.json').exists()
    assert images.image_usage(root)['total'] == 0
    assert not (root / 'production_scopes.json').exists()


def test_standing_guided_treatment_preflight_preserves_existing_incompatible_activation(tmp_path, monkeypatch):
    kwargs = standing_treatment_fixture(tmp_path)
    # Reproduce only the previous activation writer; downstream guards stay real.
    with monkeypatch.context() as patch:
        patch.setattr(pa, '_validate_standing_route_treatments', lambda *args: None)
        result = pa.activate_standing_run(tmp_path, **kwargs)
    write(tmp_path, 'production_attempts/retained-original/request.json', {
        'attempt_id': 'retained-original', 'shot_id': 'entry', 'synthetic_original_pending': True})
    before = file_bytes(tmp_path)
    assert pa.load_active_policy(tmp_path, shot_id='entry')[0] is not None
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('stages', [['edit'], ['compose'], ['proposal', 'edit', 'compose'], ['research'], ['idea']])
def test_standing_activation_rejects_ungated_stages_before_authority_writes(tmp_path, stages):
    kwargs = standing_stage_fixture(tmp_path, stages)
    with pytest.raises(pa.StandingSetupRequired, match='not actual gated pipeline stages'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert not (tmp_path / pa.POLICY_ARTIFACT).exists()
    assert not list((tmp_path / 'approvals').glob('*'))
    assert not (tmp_path / 'artifacts/decision_log.json').exists()


def test_reconcile_standing_checkpoint_stages_freezes_original_and_replays_after_progress(tmp_path, monkeypatch):
    kwargs, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    policy_path = tmp_path / pa.POLICY_ARTIFACT
    original_bytes = policy_path.read_bytes()
    old_policy = json.loads(original_bytes)
    evidence_bytes = (tmp_path / old_policy['evidence']['path']).read_bytes()
    old_log = json.loads((tmp_path / 'artifacts/decision_log.json').read_bytes())
    result = pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert result['provider_calls'] == result['reservations'] == 0 and not result['replayed']
    active, sha, decision_id, reasons = pa.load_active_policy(tmp_path, shot_id='entry')
    assert not reasons and (sha, decision_id) == (result['policy_sha256'], result['decision_id'])
    assert active['checkpoint_stages'] == ['proposal', 'script', 'scene_plan', 'assets']
    assert {k: v for k, v in active.items() if k not in {'checkpoint_stages', 'evidence'}} == {
        k: v for k, v in old_policy.items() if k not in {'checkpoint_stages', 'evidence'}}
    assert (tmp_path / old_policy['evidence']['path']).read_bytes() == evidence_bytes
    provenance = json.loads((tmp_path / result['reconciliation']['path']).read_bytes())
    assert (tmp_path / provenance['original_policy']['path']).read_bytes() == original_bytes
    assert provenance['original_policy_sha256'] == original['policy_sha256']
    assert provenance['successor_policy_sha256'] == sha
    assert provenance['removed_checkpoint_stages'] == ['edit', 'compose']
    new_log = json.loads((tmp_path / 'artifacts/decision_log.json').read_bytes())
    assert new_log['decisions'][:-1] == old_log['decisions']
    assert new_log['decisions'][-1]['options_considered'][0]['option_id'] == old_log['decisions'][-1]['selected']
    assert new_log['decisions'][-1]['options_considered'][0]['rejected_because']
    assert pa.root_attempt_counts(tmp_path, active) == pa.root_attempt_counts(tmp_path, old_policy)
    amendment = copy.deepcopy(kwargs)
    amendment['policy']['checkpoint_stages'] = active['checkpoint_stages']
    amendment['policy']['caps']['max_total_attempts'] += 1
    with pytest.raises(pa.StandingSetupRequired, match='caps, checkpoint stages or flex exceed standing setup'):
        pa.activate_standing_run(tmp_path, **amendment)
    # A repeat is a pure no-op even after legitimate production has begun.
    write(tmp_path, 'production_attempts/retained-progress/request.json', {'retained_original': True})
    before = file_bytes(tmp_path)
    assert pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256']) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['expected_sha', 'revoked', 'superseded', 'creator', 'source', 'story', 'planning', 'benchmark', 'policy'])
def test_reconcile_standing_checkpoint_stages_rejects_other_invalid_authority_without_writes(tmp_path, monkeypatch, change):
    kwargs, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    expected = original['policy_sha256']
    if change == 'expected_sha': expected = '0' * 64
    elif change in {'revoked', 'superseded'}:
        path = tmp_path / 'artifacts/decision_log.json'; log = json.loads(path.read_text())
        decision = copy.deepcopy(log['decisions'][-1]); decision['decision_id'] = 'later-choice'
        decision['selected'] = 'strict' if change == 'revoked' else 'auto_continue:' + '1' * 64
        log['decisions'].append(decision); path.write_text(json.dumps(log))
    elif change == 'creator': (tmp_path / kwargs['run_command']['path']).write_text('{}')
    elif change == 'source': (tmp_path / 'assets/start.svg').write_bytes(b'changed source')
    elif change == 'story':
        path = tmp_path / 'project.json'; value = json.loads(path.read_text()); value['story_revision'] = 'changed'
        path.write_text(json.dumps(value))
    elif change == 'planning':
        path = tmp_path / 'artifacts/scene_plan.json'; value = json.loads(path.read_text()); value['scenes'][0]['description'] = 'Different authored action'
        path.write_text(json.dumps(value))
    elif change == 'benchmark': (tmp_path / 'provider_benchmark').mkdir()
    elif change == 'policy':
        path = tmp_path / pa.POLICY_ARTIFACT; value = json.loads(path.read_text()); value['caps']['max_total_attempts'] += 1
        path.chmod(0o644)
        path.write_text(json.dumps(value))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=expected)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('kind', ['motion_journal', 'native_reserved', 'native_uncertain', 'scope', 'image_envelope', 'mcp_attempt', 'mcp_upload', 'cli_upload_authority', 'mcp_upload_authority', 'credit_reservation', 'unpriced_reservation'])
def test_reconcile_standing_checkpoint_stages_refuses_media_or_reservations(tmp_path, monkeypatch, kind):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    if kind == 'motion_journal': write(tmp_path, 'production_attempts/original/request.json', {'attempt_id': 'original'})
    elif kind.startswith('native_'):
        monkeypatch.setattr(images, '_load', lambda _: {'calls': [{'state': kind.removeprefix('native_')}]})
    elif kind == 'scope': write(tmp_path, 'production_scopes.json', {'version': '1.0', 'scopes': [{'id': 'prepared'}]})
    elif kind == 'image_envelope': write(tmp_path, 'production_scopes.json', {'version': '1.0', 'scopes': [], 'image_envelope': {}})
    elif kind == 'mcp_attempt': write(tmp_path, 'openart_mcp/attempts/original/state.json', {'status': 'prepared'})
    elif kind == 'mcp_upload': write(tmp_path, 'openart_mcp/uploads/original.json', {'status': 'consumed_awaiting_original_receipt'})
    elif kind == 'cli_upload_authority': write(tmp_path, 'artifacts/upload_approval-original.json', {})
    elif kind == 'mcp_upload_authority': write(tmp_path, 'artifacts/openart_mcp_upload_authorization-original.json', {})
    else:
        from lib import provider_credit_ledger as ledger
        snapshot = ledger.read_existing_snapshot()
        snapshot['reservations' if kind == 'credit_reservation' else 'unpriced_reservations'] = [
            {'attempt_id': 'private-only', 'binding_json': json.dumps({'project_root': str(tmp_path)})}]
        monkeypatch.setattr(ledger, 'read_existing_snapshot', lambda: snapshot)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='media|reservation|upload|scope'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['revoked', 'successor', 'provenance', 'original_snapshot', 'original_decision'])
def test_reconcile_standing_checkpoint_stages_replay_cannot_revive_or_amend(tmp_path, monkeypatch, change):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    result = pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    if change == 'successor':
        path = tmp_path / pa.POLICY_ARTIFACT; value = json.loads(path.read_text()); value['caps']['max_total_attempts'] += 1
        path.chmod(0o644)
        path.write_text(json.dumps(value))
    elif change == 'provenance':
        path = tmp_path / result['reconciliation']['path']; path.chmod(0o644); path.write_text('{}')
    elif change == 'original_snapshot':
        record = json.loads((tmp_path / result['reconciliation']['path']).read_text())
        path = tmp_path / record['original_policy']['path']; path.chmod(0o644); path.write_text('{}')
    else:
        path = tmp_path / 'artifacts/decision_log.json'; log = json.loads(path.read_text())
        if change == 'original_decision': log['decisions'][0]['user_approved'] = False
        else:
            decision = copy.deepcopy(log['decisions'][-1]); decision.update(decision_id='revoked', selected='strict')
            log['decisions'].append(decision)
        path.chmod(0o644)
        path.write_text(json.dumps(log))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(tmp_path) == before


def test_reconcile_standing_checkpoint_stages_refuses_stage_absent_from_manifest(tmp_path, monkeypatch):
    kwargs = standing_stage_fixture(tmp_path, ['proposal', 'edit', 'idea'])
    with monkeypatch.context() as patch:
        patch.setattr(pa, '_validate_checkpoint_stages', lambda *args: None)
        activation = pa.activate_standing_run(tmp_path, **kwargs)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='absent from current pipeline'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=activation['policy_sha256'])
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('interrupt', ['evidence', 'temporary_policy', 'temporary_log', 'log_pointer', 'policy_pointer'])
def test_reconcile_standing_checkpoint_stages_resumes_exact_interrupted_publication(tmp_path, monkeypatch, interrupt):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    original_policy = (tmp_path / pa.POLICY_ARTIFACT).read_bytes()
    before_log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
    with monkeypatch.context() as patch:
        if interrupt in {'evidence', 'temporary_policy', 'temporary_log'}:
            write_new = execution._write_new
            def stop_after_evidence(path, value):
                write_new(path, value)
                if (interrupt == 'evidence' and path.name.startswith('autonomy-checkpoint-reconciliation-')
                        or interrupt == 'temporary_policy' and path.name == 'autonomy_policy.json.checkpoints-tmp'
                        or interrupt == 'temporary_log' and path.name == 'decision_log.json.checkpoints-tmp'):
                    raise OSError('synthetic interruption after immutable provenance')
            patch.setattr(execution, '_write_new', stop_after_evidence)
        else:
            replace = Path.replace
            def stop_pointer(path, target):
                if str(target).endswith('decision_log.json' if interrupt == 'log_pointer' else 'autonomy_policy.json'):
                    raise OSError('synthetic pointer publication interruption')
                return replace(path, target)
            patch.setattr(Path, 'replace', stop_pointer)
        with pytest.raises(pa.StandingSetupRequired, match='synthetic'):
            pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert (tmp_path / pa.POLICY_ARTIFACT).read_bytes() == original_policy
    assert pa.load_active_policy(tmp_path)[0] is None
    result = pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert result['replayed'] and result['provider_calls'] == result['reservations'] == 0
    assert pa.load_active_policy(tmp_path)[1:3] == (result['policy_sha256'], result['decision_id'])
    log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
    assert log['decisions'][:-1] == before_log['decisions']
    before = file_bytes(tmp_path)
    assert pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256']) == result
    assert file_bytes(tmp_path) == before


def test_reconcile_standing_checkpoint_stages_partial_publication_cannot_revive_revocation(tmp_path, monkeypatch):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    replace = Path.replace
    with monkeypatch.context() as patch:
        def stop_policy(path, target):
            if str(target).endswith('autonomy_policy.json'): raise OSError('synthetic interruption')
            return replace(path, target)
        patch.setattr(Path, 'replace', stop_policy)
        with pytest.raises(pa.StandingSetupRequired):
            pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    path = tmp_path / 'artifacts/decision_log.json'; log = json.loads(path.read_text())
    decision = copy.deepcopy(log['decisions'][-1]); decision.update(decision_id='later-revocation', selected='strict')
    log['decisions'].append(decision); path.chmod(0o644); path.write_text(json.dumps(log))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='revoked or superseded'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(tmp_path) == before


def test_reconcile_standing_checkpoint_stages_preserves_unrelated_shared_source_uploads(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    kwargs, original = legacy_ungated_standing_activation(root, monkeypatch)
    from tools import _openart_cli as cli
    private = tmp_path / 'private-openart'
    write(private, 'uploads/earlier-episode.json', {'source_sha256': kwargs['baselines']['entry']['projection']['assets'][0]['sha256']})
    private.chmod(0o700)
    monkeypatch.setattr(cli, 'state_dir', lambda create=False: private)
    before = file_bytes(private)
    pa.reconcile_standing_checkpoint_stages(root, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(private) == before


@pytest.mark.parametrize('change', ['policy', 'revocation', 'planning'])
def test_reconcile_standing_checkpoint_stages_racing_change_cannot_be_overwritten(tmp_path, monkeypatch, change):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    write_new = execution._write_new
    changed_path = tmp_path / (pa.POLICY_ARTIFACT if change == 'policy' else
        'artifacts/decision_log.json' if change == 'revocation' else 'artifacts/scene_plan.json')
    def change_after_provenance(path, value):
        write_new(path, value)
        if path.name.startswith('autonomy-checkpoint-reconciliation-'):
            changed = json.loads(changed_path.read_text())
            if change == 'policy': changed['caps']['max_total_attempts'] += 1
            elif change == 'planning': changed['scenes'][0]['description'] = 'Unapproved racing plan'
            else:
                entry = copy.deepcopy(changed['decisions'][-1]); entry.update(decision_id='racing-revocation', selected='strict')
                changed['decisions'].append(entry)
            # Simulate a non-cooperating writer. Atomic replacement preserves the
            # immutable original inode and must never be overwritten by recovery.
            temporary = changed_path.with_suffix('.race.json')
            temporary.write_text(json.dumps(changed)); temporary.replace(changed_path)
    monkeypatch.setattr(execution, '_write_new', change_after_provenance)
    with pytest.raises(pa.StandingSetupRequired, match='changed during reconciliation'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert pa.load_active_policy(tmp_path)[0] is None
    if change == 'policy': assert json.loads(changed_path.read_text())['caps']['max_total_attempts'] == 4
    elif change == 'planning': assert json.loads(changed_path.read_text())['scenes'][0]['description'] == 'Unapproved racing plan'
    else: assert pa.activation_decision(tmp_path)['selected'] == 'strict'


def test_supplied_cinematic_work_passes_proposal_with_actual_standing_scope(tmp_path):
    from lib.checkpoint import write_checkpoint
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    kwargs = standing_fixture(root)
    active = pa.activate_standing_run(root, **kwargs)
    basis = {'kind': 'supplied_script_scenes', 'artifacts': [
        {'path': 'artifacts/' + name + '.json', 'sha256': hashlib.sha256((root / 'artifacts' / (name + '.json')).read_bytes()).hexdigest()}
        for name in ('script', 'scene_plan')]}
    approval = {'kind': 'policy', 'policy_sha256': active['policy_sha256'], 'decision_id': active['decision_id']}
    write_checkpoint(tmp_path, root.name, 'research', 'completed', {},
        pipeline_type='cinematic', metadata={'supplied_basis': basis})
    path = write_checkpoint(tmp_path, root.name, 'proposal', 'completed', {},
        pipeline_type='cinematic', metadata={'supplied_basis': basis}, approval_basis=approval)
    checkpoint = json.loads(path.read_text())
    assert checkpoint['human_approved'] is False
    assert 'proposal_packet' not in checkpoint['artifacts']
    assert checkpoint.get('review', {}) == {}
    script_binding = {'artifact_path': 'artifacts/script.json', 'artifact_sha256': basis['artifacts'][0]['sha256']}
    review_binding = write(root, 'notes/script-review.json', {'version': '1.0', 'stage': 'script',
        **script_binding, 'critical_findings': [], 'findings': []})
    preauth = {**script_binding, 'review_path': review_binding['path'], 'review_sha256': review_binding['sha256'],
        'critical_findings': []}
    path = write_checkpoint(tmp_path, root.name, 'script', 'completed', {'script': SCRIPT},
        pipeline_type='cinematic', approval_basis=approval, review={'preauth': preauth})
    assert json.loads(path.read_text())['human_approved'] is False


@pytest.mark.parametrize('change', ['account', 'source', 'caps', 'creator', 'permissions'])
def test_standing_setup_gaps_fail_before_activation(tmp_path, change):
    kwargs = standing_fixture(tmp_path)
    setup = json.loads((tmp_path / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    if change == 'account': setup['provider_accounts']['grok_cli'] = ''
    elif change == 'source': setup['approved_source_sha256'] = []
    elif change == 'caps': setup['caps']['max_total_attempts'] = 1
    elif change == 'permissions': setup['required_permissions'] = [write(tmp_path, 'notes/permission.json', {'status': 'pending'})]
    kwargs['standing_setup'] = creator_fixture(tmp_path, 'standing', setup)
    command = json.loads((tmp_path / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'command', command)
    if change == 'creator':
        evidence = json.loads((tmp_path / kwargs['run_command']['path']).read_text())['authorization']
        (tmp_path / evidence['path']).write_text('{"origin":"creator"}')
    with pytest.raises(pa.StandingSetupRequired): pa.activate_standing_run(tmp_path, **kwargs)
    assert not (tmp_path / 'artifacts/autonomy_policy.json').exists()
    assert not (tmp_path / 'artifacts/decision_log.json').exists()


def test_begun_native_host_action_is_durable_and_never_reemitted(board_package):
    root, *_ = board_package
    call = reserve(board_package, call_id='factory-original')
    first = execution.advance(root)
    assert first['status'] == 'host_action' and first['host_envelope']['call_id'] == call['call_id']
    assert first['action']['kind'] == 'receive_native_image'
    second = execution.advance(root)
    assert 'host_envelope' not in second and second['action'] == first['action']
    assert images.image_usage(root)['total'] == 1
    altered = copy.deepcopy(second['action']); altered['inputs']['call_id'] = 'replacement'
    with pytest.raises(execution.ProductionGovernanceError, match='stale, foreign or modified'):
        execution.complete_action(root, altered, outcome={})
    receipt = host_receipt(root, first['host_envelope'])
    completed = execution.complete_action(root, second['action'], outcome={'host_receipt': receipt})
    assert completed['status'] == 'blocked'  # This image-only fixture has no authored motion plan.
    assert images.image_usage(root)['total'] == 1
    replayed = execution.complete_action(root, second['action'], outcome={'host_receipt': receipt})
    assert replayed['replayed'] and replayed['provider_calls'] == 0
    assert images.image_usage(root)['total'] == 1


def test_unsupported_strict_original_retains_exact_id_and_count(tmp_path):
    write(tmp_path, 'project.json', {'project_id': 'SYNTHETIC-unsupported', 'story_revision': 'r1',
        'governance': {'mode': 'strict', 'version': '1.0'}})
    original = {'attempt_id': 'original-only', 'cli_session_id': 'original-native-id',
        'shot_id': 's1', 'media_kind': 'motion', 'story_revision': 'r1',
        'request_sha256': 'a'*64, 'scope': {'provider': 'unsupported_strict_provider'}}
    binding = write(tmp_path, 'production_attempts/original-only/request.json', original)
    resumed = execution.resume_original_attempt(tmp_path, 'original-only', request_sha256='a'*64)
    assert resumed['status'] == 'blocked' and resumed['original_id'] == 'original-native-id'
    assert resumed['counted_original'] and resumed['provider_calls'] == resumed['reservations'] == 0
    action = execution.next_production_action(tmp_path)
    assert action['kind'] == 'blocked' and action['inputs']['original_id'] == 'original-native-id'
    assert len(execution._attempts(tmp_path)) == 1
    assert hashlib.sha256((tmp_path / binding['path']).read_bytes()).hexdigest() == binding['sha256']


def prospective_package(package):
    package = copy.deepcopy(package); package['planning_mode'] = 'shot_local_v1'
    # Canonical authored shot facts replace duplicated template facts.
    for shot in package['boards']['motion_binding']['contract_template']['shots']:
        shot.pop('prop_body_invariants', None)
    return package


def test_local_package_partial_contract_and_lost_derived_view_restoration(neutral_package, monkeypatch):
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package); binding = bind(package)
    prepared = request.prepare_package(root, binding)
    assert prepared['motion_projection']['pending_assets']
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    assert all(a.get('preparation_pending') and 'sha256' not in a and 'review' not in a for a in contract['assets'])
    original = (root / 'artifacts/script.json').read_bytes()
    (root / 'artifacts/script.json').unlink()
    # Retained same-pack source proves the original authored bytes after usage.
    monkeypatch.setattr(images, 'image_usage', lambda _: {'total': 1})
    assert execution.next_production_action(root)['kind'] == 'restore_derived_views'
    request.prepare_package(root, binding)
    assert (root / 'artifacts/script.json').read_bytes() == original
    assert boards.load_board_pack(root)['package_binding'] == binding


def test_ready_board_loop_and_completed_image_restore_emit_zero_new_calls(neutral_package):
    from lib import production_boards as boards
    from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES, ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['boards']['motion_binding']['contract_template']['shots'][0]['endpoint_completion'] = 'completed'
    binding = bind(package); request.prepare_package(root, binding)
    for slot in ('start', 'end'):
        prepared = boards.prepare_image_request(root, slot, images=images)
        scope = {'id': 'exact-' + slot, 'status': 'approved', 'approved_by': 'SYNTHETIC creator fixture',
            'project_id': package['project_id'], 'story_revision': 'r1', 'phase': 'image', 'provider': 'native_imagegen',
            'evidence': {k: package['boards']['approval'][k] for k in ('path', 'sha256')},
            'image_allowance': 2, 'allowed_image_routes': ['native_imagegen'],
            'requests': {slot: prepared['request_sha256']}, 'attempts_per_shot': {slot: 1}}
        scope_path = root / 'production_scopes.json'
        scopes = json.loads(scope_path.read_text()) if scope_path.exists() else {'version': '1.0', 'scopes': []}
        scopes['scopes'].append(scope); write(root, 'production_scopes.json', scopes)
        call = boards.reserve_board_image(root, slot, scope_id=scope['id'], call_id='original-' + slot, images=images)
        envelope = images.begin_native_image(root, call['call_id'])
        receipt = host_receipt(root, envelope, name=slot)
        images.import_native_image(root, call['call_id'], host_receipt=receipt)
        source = boards.bind_imported_board(root, slot, call['call_id'], images=images)
        review = attestation(source['sha256'], ASSET_PREDICATES, revision='r1')
        review['reviewer'] = 'persistent reviewer'
        boards.record_board_review(root, slot, review, roles=[slot + '_frame'], images=images)
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    assert boards.board_schedule(root, images=images)['board_ready_shots'] == ['s1']
    assert execution.next_production_action(root)['kind'] == 'prepare_motion'
    retained = {p: p.read_bytes() for p in (root / 'production_images').rglob('*') if p.is_file()}
    expected = {name: (root / 'artifacts' / (name + '.json')).read_bytes() for name in ('script', 'scene_plan', 'asset_manifest')}
    for name in expected: (root / 'artifacts' / (name + '.json')).unlink()
    resumed = execution.advance(root)
    assert resumed['status'] == 'host_action' and resumed['action']['kind'] == 'prepare_motion'
    assert 'host_envelope' not in resumed and images.image_usage(root)['total'] == 2
    assert retained == {p: p.read_bytes() for p in (root / 'production_images').rglob('*') if p.is_file()}
    for name, raw in expected.items(): assert (root / 'artifacts' / (name + '.json')).read_bytes() == raw


def test_local_package_rejects_contradictory_retyped_motion_fact(neutral_package):
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['boards']['motion_binding']['contract_template']['shots'][0]['dominant_action'] = 'Contradictory duplicate'
    with pytest.raises(ValueError, match='duplicated motion fact contradicts'):
        request.compile_package(root, bind(package))
    assert not (root / 'artifacts/board_preparation.json').exists()


@pytest.mark.parametrize('insufficient', [None, 'motion', 'images', 'revoked', 'import_crash', 'import_crash_revoked'])
def test_actual_standing_run_derives_native_image_authority_without_new_creator_gate(neutral_package, insufficient):
    from lib import production_boards as boards
    from lib.shot_contract import ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package); binding = bind(package)
    request.prepare_package(root, binding)
    marker = json.loads((root / 'project.json').read_text()); marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root)
    script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        first = projection['assets'][0]['id']
        template = execution.planned_request_template({'duration': shot['duration_seconds'],
            'image_path': {'$board': {'asset_id': first}}}, project_dir=root)
        baselines[shot['id']] = {'projection': projection, 'planned_request_template': template,
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    policy = make_policy(project_id=package['project_id'], story_revision='r1',
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}},
        flex={'duration_s': [1, 10], 'resolution': [], 'references': {'droppable_roles': [], 'substitutes': []}})
    accounts = {'grok_cli': 'fixture-subscription', 'openart_cli': 'fixture-credit-account', 'native_imagegen': 'fixture-native-account'}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in package['boards']['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': 4, 'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': route, 'account': account,
            'roles': ['identity', 'identity_reference', 'start_frame', 'end_frame', 'payoff_board'],
            'generated_board_sources': True, 'max_files': 8, 'max_file_bytes': 10000,
            'max_batch_bytes': 80000, 'max_batches': 4} for route, account in accounts.items()]}
    if insufficient == 'motion':
        policy['caps']['max_total_attempts'] = 2
    elif insufficient == 'images':
        setup['native_images']['image_allowance'] = 3
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': package['project_id'], 'story_revision': 'r1', 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    kwargs = dict(policy=policy, baselines=baselines, standing_setup=standing, run_command=command, identity_id='fixture-identity')
    if insufficient in {'motion', 'images'}:
        with pytest.raises(pa.StandingSetupRequired, match='fixed .* allowance cannot cover'):
            pa.activate_standing_run(root, **kwargs)
        assert not (root / 'artifacts/autonomy_policy.json').exists()
        assert images.image_usage(root)['total'] == 0
        return
    active = pa.activate_standing_run(root, **kwargs)
    begun = execution.advance(root)
    assert begun['status'] == 'host_action' and begun['action']['kind'] == 'receive_native_image', begun.get('reason', begun)
    assert begun['host_envelope']['call_id']
    slot = images.image_call_status(root, begun['host_envelope']['call_id'])['slot_id']
    if insufficient in {'revoked', 'import_crash_revoked'}:
        log = json.loads((root / 'artifacts/decision_log.json').read_text())
        log['decisions'][0]['user_approved'] = False
        write(root, 'artifacts/decision_log.json', log)
    receipt = host_receipt(root, begun['host_envelope'])
    if insufficient in {'import_crash', 'import_crash_revoked'}:
        # Crash after the canonical original import, before derived pack binding.
        images.import_native_image(root, begun['action']['subject'], host_receipt=receipt)
        assert execution.next_production_action(root)['kind'] == 'promote_imported_board'
        recovered = execution.advance(root)
        assert recovered['action']['kind'] not in {'review_board', 'review_planning', 'review_selection'}
        if insufficient == 'import_crash':
            assert recovered.get('host_envelope', {}).get('call_id') != begun['host_envelope']['call_id']
            assert images.image_usage(root)['total'] == 2
        else:
            assert recovered['status'] == 'blocked'
            assert images.image_usage(root)['total'] == 1
        received = execution.complete_action(root, begun['action'], outcome={'host_receipt': receipt})
        assert received['replayed']
    else:
        received = execution.complete_action(root, begun['action'], outcome={'host_receipt': receipt})
    assert received['action']['kind'] not in {'review_board', 'review_planning', 'review_selection'}
    assert slot in boards.load_board_pack(root)['board_bindings']
    continued = received
    scopes = json.loads((root / 'production_scopes.json').read_text())
    assert scopes['image_envelope']['activation_basis']['policy_sha256'] == active['policy_sha256']
    assert scopes['image_envelope']['approval']['approved_by'].startswith('standing_media_authority:')
    if insufficient in {'revoked', 'import_crash_revoked'}:
        assert 'host_envelope' not in continued
        assert execution.advance(root)['status'] == 'blocked'
        assert images.image_usage(root)['total'] == 1
        with pytest.raises(pa.AutonomyError): images.activate_standing_image_envelope(root)
    else:
        assert pa.activate_standing_run(root, **kwargs) == dict(active, replayed=True)


def test_loop_prefers_ready_independent_motion_over_unrelated_host_recovery(p, monkeypatch):
    p.contract['planning_mode'] = 'shot_local_v1'
    p.contract['project_review']['subject_sha256'] = project_review_subject(p.contract)
    for shot in p.contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(p.contract, shot['id'])
    p.persist_contract()
    # The source closure is real; unrelated submitted host intent cannot hold it.
    (p.root / images.ARTIFACT).write_text('{}')
    monkeypatch.setattr(images, '_load', lambda _: {'calls': [{'call_id': 'later-original', 'state': 'submitted',
        'request_sha256': 'a'*64, 'scope_sha256': 'b'*64}]})
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion' and action['subject'] == 'entry'


def test_loop_discloses_usable_failed_candidate_for_draft_without_fake_selection(p):
    result = p.generate('entry')
    aid = result.data['production_attempt_id']
    output_sha = hashlib.sha256((p.root / result.artifacts[0]).read_bytes()).hexdigest()
    review = attestation(output_sha, ['possession'])
    review.update(status='fail', predicates=[{'name': 'possession', 'status': 'fail', 'evidence': 'SYNTHETIC duplicate bill'}])
    execution.record_rejection(p.root, aid, review)
    # Semantically failed playable footage remains eligible for the cut.
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'compose_cut'
    candidate = action['inputs']['candidates'][0]
    assert candidate['review'] == 'fail' and candidate['strict_selected'] is False
    assert execution.load_selected_attempts(p.root) == {}


def test_completed_original_cannot_be_relabelled_failed_and_status_stays_unknown(p):
    from lib.production_draft import first_cut_candidates
    p.transport.timeout_next = True
    original = p.generate('entry'); aid = original.data['production_attempt_id']
    retained = json.loads((p.root / 'production_attempts' / aid / 'request.json').read_text())
    failed = copy.deepcopy(original)
    failed.data['conditioning_receipt'].update(submission_evidence='verified_native_call', dispatch_status='failed')
    failed.data['dispatch_status'] = 'failed'
    with pytest.raises(execution.ProductionGovernanceError, match='failed native completion'):
        execution.reconcile_attempt(p.root, aid, failed, request_sha256=retained['request_sha256'])
    row = first_cut_candidates(p.root)[0]
    assert row['reason'] == 'original_pending' and row['detail']['status'] == 'uncertain'
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


def test_local_packet_and_baseline_survive_unrelated_asset_insert_and_prior_timing(p):
    p.contract['planning_mode'] = 'shot_local_v1'
    next(s for s in p.contract['shots'] if s['id'] == 'interior')['upstream'] = []
    p.contract['project_review']['subject_sha256'] = project_review_subject(p.contract)
    for shot in p.contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(p.contract, shot['id'])
    p.persist_contract()
    scene_path = p.root / 'artifacts/scene_plan.json'; scenes = json.loads(scene_path.read_text())
    sections = []
    for i, scene in enumerate(scenes['scenes']):
        scene['script_section_id'] = 'section-' + scene['id']
        scene.update(start_seconds=i*8, end_seconds=(i+1)*8)
        sections.append({'id': scene['script_section_id'], 'text': 'SYNTHETIC local ' + scene['id'],
            'start_seconds': i*8, 'end_seconds': (i+1)*8})
    write(p.root, 'artifacts/scene_plan.json', scenes)
    write(p.root, 'artifacts/script.json', {'version': '1.0', 'title': 'SYNTHETIC local timing',
        'total_duration_seconds': len(sections)*8, 'sections': sections})
    # Interior follows entry; its local timing changes only in cut placement.
    before = pa.current_projection(p.root, 'interior')
    packet = request.source_packet(p.root, 'interior', provider='grok_cli')
    unrelated = copy.deepcopy(p.contract['assets'][-1]); unrelated['id'] = 'unrelated-insert'
    p.contract['assets'].insert(0, unrelated)
    p.contract['shots'][0]['dominant_action'] = 'Unrelated earlier action'
    p.persist_contract()
    scene_path = p.root / 'artifacts/scene_plan.json'; scenes = json.loads(scene_path.read_text())
    script_path = p.root / 'artifacts/script.json'; script = json.loads(script_path.read_text())
    for scene in scenes['scenes'][1:]:
        scene['start_seconds'] += 1; scene['end_seconds'] += 1
    scenes['scenes'][0]['end_seconds'] += 1
    for section in script['sections'][1:]:
        section['start_seconds'] += 1; section['end_seconds'] += 1
    script['sections'][0]['end_seconds'] += 1; script['total_duration_seconds'] += 1
    write(p.root, 'artifacts/scene_plan.json', scenes); write(p.root, 'artifacts/script.json', script)
    after = pa.current_projection(p.root, 'interior')
    for value in (before, after):
        value.pop('contract_snapshot'); value.pop('scene_snapshot')
    assert before == after
    assert packet == request.source_packet(p.root, 'interior', provider='grok_cli')


def test_prepare_motion_preserves_mcp_stage_at_registered_schema_boundary(p, monkeypatch):
    from jsonschema import Draft202012Validator
    from tools.base_tool import ToolResult
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    from tools.tool_registry import registry
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion'
    inputs = {'project_dir': str(p.root), 'governance': {'scope_id': 'placeholder',
        'shot_id': 'entry', 'stage': 'assets'}, 'model': 'pixverseV6', 'mode': 'text2video', 'prompt': 'Synthetic offline shot'}
    tool = OpenArtMCPVideo(); submitted = []
    monkeypatch.setattr(pa, 'derive_scope', lambda *a, **kw: {'id': 'derived'})
    monkeypatch.setattr(registry, 'ensure_discovered', lambda *a: None)
    monkeypatch.setattr(registry, 'get', lambda name: tool)
    def dry(selected, actual):
        Draft202012Validator(selected.input_schema).validate(actual)
        return {'scope_id': actual['governance']['scope_id']}
    monkeypatch.setattr(execution, 'governed_dry_run', dry)
    monkeypatch.setattr(tool, 'execute', lambda actual: submitted.append(copy.deepcopy(actual)) or ToolResult(success=True))
    execution.complete_action(p.root, action, outcome={'provider': 'openart_mcp', 'inputs': inputs})
    assert submitted[0]['governance'] == {'scope_id': 'derived', 'shot_id': 'entry', 'stage': 'assets'}


def test_motion_scope_crash_reuses_exact_scope_before_one_submission(tmp_path, monkeypatch):
    from tools.base_tool import ToolResult
    from tools.tool_registry import registry
    kwargs = standing_fixture(tmp_path); pa.activate_standing_run(tmp_path, **kwargs)
    policy, sha, decision = pa.require_active_policy(tmp_path)
    inputs = {'governance': {'shot_id': 'entry'}, 'duration': 8}
    material = dict(shot_id='entry', provider='grok_cli',
        request_digest=execution.planned_request_digest(inputs, project_dir=tmp_path),
        approval_plan_sha256=execution.approval_plan_digest(execution.load_shot_contract(tmp_path), 'entry'),
        lock_digest='a'*64, compiled_request_sha256='b'*64, preparation_review_id='fixture-review', resolved_upstream={})
    monkeypatch.setattr(pa, '_scope_material', lambda *a, **kw: (policy, sha, decision, copy.deepcopy(material)))
    action = execution._production_action(tmp_path, 'prepare_motion', subject='entry', binding={}, host=True)
    monkeypatch.setattr(execution, 'next_production_action', lambda _: action)
    class OfflineTool:
        def execute(self, actual):
            submitted.append(copy.deepcopy(actual))
            return ToolResult(success=True)
    submitted = []; tool = OfflineTool()
    monkeypatch.setattr(registry, 'ensure_discovered', lambda *a: None)
    monkeypatch.setattr(registry, 'get', lambda name: tool)
    def crash(*args): raise RuntimeError('synthetic crash after scope derivation before reservation')
    monkeypatch.setattr(execution, 'governed_dry_run', crash)
    with pytest.raises(RuntimeError, match='after scope derivation'):
        execution.complete_action(tmp_path, action, outcome={'provider': 'grok_cli', 'inputs': inputs})
    scopes = json.loads((tmp_path / 'production_scopes.json').read_text())['scopes']
    assert len(scopes) == 1 and submitted == []
    monkeypatch.setattr(execution, 'governed_dry_run', lambda *a: {})
    execution.complete_action(tmp_path, action, outcome={'provider': 'grok_cli', 'inputs': inputs})
    assert json.loads((tmp_path / 'production_scopes.json').read_text())['scopes'] == scopes
    assert len(submitted) == 1 and submitted[0]['governance']['scope_id'] == scopes[0]['id']
    changed = dict(material, request_digest='c'*64)
    monkeypatch.setattr(pa, '_scope_material', lambda *a, **kw: (policy, sha, decision, changed))
    with pytest.raises(pa.AutonomyError, match='undispatched scope differs'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', reuse_undispatched=True)
    assert len(submitted) == 1


@pytest.mark.parametrize('provider', ['grok_cli', 'openart_mcp'])
def test_motion_waiting_original_does_not_hold_independent_ready_shot(p, monkeypatch, provider):
    from lib import production_draft as draft, openart_mcp_jobs as jobs
    p.contract['planning_mode'] = 'shot_local_v1'
    next(s for s in p.contract['shots'] if s['id'] == 'interior')['upstream'] = []
    p.contract['project_review']['subject_sha256'] = project_review_subject(p.contract)
    for shot in p.contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(p.contract, shot['id'])
    p.persist_contract()
    original = {'attempt_id': 'synthetic-waiting-original', 'shot_id': 'entry', 'media_kind': 'motion',
        'request_sha256': 'a'*64, 'provider': provider, 'scope': {'provider': provider}}
    monkeypatch.setattr(execution, '_attempts', lambda _: [original])
    monkeypatch.setattr(execution, '_state', lambda *a: {'status': 'uncertain'})
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda _: [])
    monkeypatch.setattr(jobs, 'resume_original', lambda *a: {'request_sha256': 'a'*64,
        'next_action': {'action': 'poll_original', 'attempt_id': original['attempt_id']}})
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion' and action['subject'] == 'interior'
    assert execution._attempts(p.root) == [original]


@pytest.mark.parametrize('cut', [
    {'status': 'incomplete', 'technical': {'has_audio': True}, 'missing': ['payoff']},
    {'status': 'complete', 'technical': None, 'missing': []}])
def test_current_first_cut_requires_complete_technical_master(p, monkeypatch, cut):
    from lib import production_draft as draft
    for shot in p.contract['shots']:
        sid = shot['id']
        if shot['upstream']:
            p.bind_upstream(sid)
        result = p.generate(sid)
        assert result.success
        p.select(sid, result)
    candidates = draft.first_cut_candidates(p.root)
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda _: candidates)
    monkeypatch.setattr(draft, 'latest_first_cut', lambda _: dict(cut, path='existing-cut.json', sha256='a'*64))
    monkeypatch.setattr(draft, 'first_cut_status', lambda *a: {'status': 'current'})
    assert execution.advance(p.root)['status'] == 'ready_to_compose'


def upload_standing_fixture(root, *, provider, max_batches=1, uid_sha256=None):
    kwargs = standing_fixture(root)
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    if provider == 'openart_mcp':
        spec = {'id': provider, 'billing': 'unknown_cost_no_ceiling',
            'exposure_acknowledgement': 'no_enforceable_credit_ceiling', 'uid_sha256': uid_sha256,
            'project_id': 'fixture-openart-project', 'routes': [{'model': 'pixverseV6', 'mode': 'image2video'}]}
        kwargs['policy']['providers'].append(spec); setup['providers'].append(copy.deepcopy(spec))
        setup['provider_accounts'][provider] = 'fixture-mcp-account'
        setup['source_transfer'].append(dict(setup['source_transfer'][0], provider=provider, account='fixture-mcp-account'))
    for term in setup['source_transfer']: term['max_batches'] = max_batches
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    pa.activate_standing_run(root, **kwargs)
    return pa.require_active_policy(root)[0]


def test_inline_transfer_final_batch_revalidates_same_original_and_zero_sources(tmp_path):
    policy = upload_standing_fixture(tmp_path, provider='grok_cli')
    inputs = {'governance': {'shot_id': 'entry'}, 'image_path': str(tmp_path / 'assets/start.svg')}
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')
    write(tmp_path, 'production_attempts/final-original/request.json', {'attempt_id': 'final-original',
        'scope_id': 'exact-final-scope', 'scope': {'provider': 'grok_cli'}, 'submitted_inputs': inputs})
    # Adapter revalidation validates the current original, not another batch.
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli', exclude_scope_id='exact-final-scope')
    with pytest.raises(pa.StandingSetupRequired, match='batch allowance exhausted'):
        pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')
    pa._validate_standing_transfer(tmp_path, policy, {'governance': {'shot_id': 'entry'}}, 'grok_cli')
    # A zero-source original never consumes source-transfer allowance.
    write(tmp_path, 'production_attempts/no-source/request.json', {'attempt_id': 'no-source',
        'scope_id': 'no-source', 'scope': {'provider': 'grok_cli'}, 'submitted_inputs': {'duration': 8}})
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli', exclude_scope_id='exact-final-scope')


def test_cli_standing_upload_exact_final_batch_replay_account_bytes_and_revocation(tmp_path, monkeypatch):
    policy = upload_standing_fixture(tmp_path, provider='openart_cli')
    spec = pa._provider_spec(policy, 'openart_cli')
    account = {'account_id_sha256': spec['account_id_sha256'], 'workspace': spec['workspace']}
    source = str(tmp_path / 'assets/start.svg')
    kwargs = dict(shot_id='entry', provider='openart_cli', files=[source], account=account)
    first = pa.derive_standing_upload_authority(tmp_path, **kwargs)
    assert first['provider_calls'] == 0 and first['authority']['approved_by'].startswith('standing_media_authority:')
    # Already-counted motion using a remote upload is not a fresh upload batch.
    write(tmp_path, 'production_attempts/motion/request.json', {'attempt_id': 'motion', 'scope_id': 'motion',
        'scope': {'provider': 'openart_cli'}, 'submitted_inputs': {'image_path': source}})
    assert pa.derive_standing_upload_authority(tmp_path, **kwargs) == dict(first, replayed=True)
    sha = hashlib.sha256((tmp_path / 'assets/start.svg').read_bytes()).hexdigest()
    assert request.approved_upload_lookup(tmp_path, first['upload_id'], sha)['state'] == 'approved'
    with pytest.raises(pa.StandingSetupRequired, match='upload batch allowance exhausted'):
        pa.derive_standing_upload_authority(tmp_path, **dict(kwargs, files=[str(tmp_path / 'assets/end.svg')]))
    with pytest.raises(pa.StandingSetupRequired, match='account/workspace'):
        pa.derive_standing_upload_authority(tmp_path, **dict(kwargs, account=dict(account, workspace='other')))
    from lib import openart_jobs as cli_jobs
    monkeypatch.setattr(cli_jobs, '_upload_profile', lambda *a: {'mode': 'image2video', 'model': 'kling-3', 'account_id_sha256': 'f'*64})
    monkeypatch.setattr(cli_jobs, 'upload_binding', lambda *a: {})
    monkeypatch.setattr(cli_jobs, '_UPLOAD_APPROVAL_LOOKUP', request.approved_upload_lookup)
    with pytest.raises(cli_jobs.OpenArtCLIError, match='account/model differs'):
        cli_jobs.upload_reference(tmp_path, first['upload_id'], tmp_path / 'assets/start.svg', model='kling-3', mode='image2video')
    raw = (tmp_path / 'assets/start.svg').read_bytes()
    (tmp_path / 'assets/start.svg').write_bytes(raw + b'changed')
    assert request.approved_upload_lookup(tmp_path, first['upload_id'], sha) is None
    (tmp_path / 'assets/start.svg').write_bytes(raw)
    log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text()); log['decisions'][0]['user_approved'] = False
    write(tmp_path, 'artifacts/decision_log.json', log)
    assert request.approved_upload_lookup(tmp_path, first['upload_id'], sha) is None
    assert len(list((tmp_path / 'artifacts').glob('upload_approval-*.json'))) == 1


from tests.integration.test_openart_mcp_governance import fixture_observations  # noqa: F401,E402
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401,E402


def test_mcp_standing_upload_final_batch_original_receipt_and_account_revalidation(tmp_path, fixture_observations):
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    root = tmp_path / 'standing-mcp'; root.mkdir()
    account = mcp.account_summary()
    upload_standing_fixture(root, provider='openart_mcp', uid_sha256=account['uid_sha256'])
    args = dict(shot_id='entry', provider='openart_mcp', files=[str(root / 'assets/start.svg')],
        account={'uid_sha256': account['uid_sha256']}, openart_project_id='fixture-openart-project')
    authority = pa.derive_standing_upload_authority(root, **args)
    declaration = {'upload_authorization_id': authority['upload_id']}
    begun = jobs.prepare_upload(root, files=args['files'], billing_declaration=declaration,
        openart_project_id=args['openart_project_id'])
    # The canonical consumed batch is revalidated at its original receipt.
    assert pa.derive_standing_upload_authority(root, **args) == dict(authority, replayed=True)
    result = {'projectId': args['openart_project_id'], 'results': [{'fileId': 'fixture-source',
        'status': 'SUCCESS', 'visualReference': {'id': 'fixture-ref', 'type': 'image',
            'url': 'https://fixture.invalid/source.svg', 'metadata': {}}}]}
    with pytest.raises(ValueError, match='different|invalid|exact'):
        jobs._upload_authority(root, authority['authority']['files'], declaration,
            {'uid_sha256': 'f'*64}, args['openart_project_id'])
    # Exact existing owner can still resolve the final already-started batch.
    jobs.record_upload_receipt(root, upload_id=begun['upload_id'], result=result)
    with pytest.raises(pa.StandingSetupRequired, match='upload batch allowance exhausted'):
        pa.derive_standing_upload_authority(root, **dict(args, files=[str(root / 'assets/end.svg')]))
    with pytest.raises(ValueError, match='already consumed'):
        jobs.prepare_upload(root, files=args['files'], billing_declaration=declaration,
            openart_project_id=args['openart_project_id'])


@pytest.mark.parametrize('unrelated', [False, True])
def test_standing_mcp_zero_source_prepare_then_begin_at_final_media_slot(lifecycle, monkeypatch, tmp_path, unrelated):
    root, policy, _, inputs, _, _, jobs = lifecycle
    contract = execution.load_shot_contract(root); contract['planning_mode'] = 'shot_local_v1'
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    contract['shots'][0]['review']['subject_sha256'] = shot_review_subject(contract, 'entry')
    if unrelated:
        other = copy.deepcopy(contract['shots'][0]); other['id'] = 'other'; contract['shots'].append(other)
        contract['project_review']['subject_sha256'] = project_review_subject(contract)
        for shot in contract['shots']: shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
        scenes = json.loads((root / 'artifacts/scene_plan.json').read_text())
        scene = copy.deepcopy(scenes['scenes'][0]); scene['id'] = 'other'; scenes['scenes'].append(scene)
        write(root, 'artifacts/scene_plan.json', scenes)
        policy['caps']['max_total_attempts'] = 2
    write(root, 'artifacts/shot_contract.json', contract)
    marker = json.loads((root / 'project.json').read_text()); marker['identity_id'] = 'fixture-identity'
    write(root, 'project.json', marker)
    timing = json.loads((root / 'artifacts/compiled_request-original.json').read_text())['timing']
    inputs, native, _ = compile_original_mcp(root, inputs, 'standing-zero-source', timing)
    projection = pa.current_projection(root, 'entry')
    baselines = {'entry': {'projection': projection,
        'planned_request_template': execution.planned_request_template(inputs, project_dir=root),
        'script_snapshot': json.loads((root / 'artifacts/script.json').read_text()),
        'dialogue_coverage': pa.dialogue_occurrences(projection),
        'approval_plan_sha256': execution.approval_plan_digest(contract, 'entry')}}
    if unrelated:
        other_projection = pa.current_projection(root, 'other')
        baselines['other'] = dict(copy.deepcopy(baselines['entry']), projection=other_projection,
            dialogue_coverage=pa.dialogue_occurrences(other_projection),
            approval_plan_sha256=execution.approval_plan_digest(contract, 'other'))
    accounts = {p['id']: 'fixture-' + p['id'] for p in policy['providers']}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': accounts, 'caps': policy['caps'],
        'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [], 'required_permissions': [],
        'source_transfer': [{'provider': provider, 'account': account, 'roles': [],
            'generated_board_sources': False, 'max_files': 1, 'max_file_bytes': 1,
            'max_batch_bytes': 1, 'max_batches': 1} for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'command': 'make_video', 'standing_setup_sha256': standing['sha256']})
    # Fixture replaces unspent fixture authority, never a production record.
    (root / 'artifacts/autonomy_policy.json').unlink(); (root / 'artifacts/decision_log.json').unlink()
    write(root, 'production_scopes.json', {'version': '1.0', 'scopes': []})
    activation = pa.activate_standing_run(root, policy=policy, baselines=baselines,
        standing_setup=standing, run_command=command, identity_id='fixture-identity')
    scope = pa.derive_scope(root, inputs, provider='openart_mcp'); inputs['governance']['scope_id'] = scope['id']
    jobs.prepare(root, attempt_id='standing-final-original', generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    begun = jobs.begin(root, 'standing-final-original', authority_fn=execution.prepare_openart_mcp_handoff)
    assert begun['arguments'] == native['body']
    assert jobs.frozen_request(root, 'standing-final-original')['authority']['billing']['policy_sha256'] == activation['policy_sha256']
    assert pa.root_attempt_counts(root, pa.require_active_policy(root)[0])['total'] == 1
    with pytest.raises(ValueError): jobs.begin(root, 'standing-final-original', authority_fn=execution.prepare_openart_mcp_handoff)
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    from tests.integration.test_openart_mcp_governance import install_fake_download
    from lib import production_provenance as provenance
    from lib.shot_contract import file_sha256, selection_digest, UPSTREAM_PREDICATES
    from tests.lib.test_production_images import PNG
    aid = 'standing-final-original'
    jobs.receive(root, aid, outcome={'historyId': 'fixture-standing-original', 'status': 'PENDING'})
    jobs.record_status(root, aid, result={'historyId': 'fixture-standing-original', 'status': 'COMPLETED',
        'resources': [{'id': 'fixture-video', 'mediaType': 'video', 'status': 'COMPLETED',
            'url': 'https://fixture.invalid/standing.mp4'}]})
    media = real_av_clip(tmp_path / 'fixture-standing.mp4', seconds=1)
    install_fake_download(monkeypatch, jobs, media)
    downloaded = jobs.download_original(root, aid); jobs.collect(root, aid, downloaded_path=downloaded['downloaded_path'])
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    output = {'path': inputs['output_path'], 'sha256': file_sha256(inputs['output_path'])}
    validate = lambda: provenance.validate_attempt_provenance(root, aid, shot_id='entry',
        story_revision=contract['story_revision'], expected_output=output)
    assert validate()['result']['status'] == 'generated'
    if unrelated:
        current = execution.load_shot_contract(root)
        current['shots'][1]['dominant_action'] = 'SYNTHETIC unrelated new action'
        write(root, 'artifacts/shot_contract.json', current)
        assert validate()['result']['status'] == 'generated'
    frame = root / 'observed-outgoing.png'; frame.write_bytes(PNG)
    selection = {'attempt_id': aid, 'output': output, 'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES, revision=contract['story_revision'])
    execution.record_selection(root, 'entry', selection)
    assert execution.load_selected_attempts(root)['entry'] == selection
    current = execution.load_shot_contract(root); current['shots'][0]['dominant_action'] = 'SYNTHETIC changed target action'
    write(root, 'artifacts/shot_contract.json', current)
    with pytest.raises(ValueError): validate()


def grok_final_fixture(tmp_path, **extra):
    """Real activated Grok policy with reviewed compiled request; no provider calls."""
    kwargs = standing_fixture(tmp_path)
    contract = execution.load_shot_contract(tmp_path); shot = contract['shots'][0]
    inputs = {'project_dir': str(tmp_path), 'governance': {'shot_id': 'entry'},
        'prompt': 'Synthetic placeholder before activation', 'operation': 'reference_to_video', 'duration': 8, 'image_path': str(tmp_path / 'assets/start.svg'),
        'output_path': str(tmp_path / 'clip.mp4'), 'compiled_request_id': 'final', 'preparation_review_id': 'final'}
    from lib.shot_contract import required_asset_ids
    consumer = required_asset_ids(contract, shot)
    inputs['reference_image_paths'] = [str(tmp_path / a['path']) for a in contract['assets']
        if a['id'] in consumer and a['id'] != 'start']
    inputs.update(extra)
    kwargs['baselines']['entry']['planned_request_template'] = execution.planned_request_template(inputs, project_dir=tmp_path)
    setup = json.loads((tmp_path / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    next(t for t in setup['source_transfer'] if t['provider'] == 'grok_cli')['max_batches'] = 1
    kwargs['standing_setup'] = creator_fixture(tmp_path, 'standing', setup)
    command = json.loads((tmp_path / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'command', command)
    pa.activate_standing_run(tmp_path, **kwargs)
    observation = {'cli_version': '1.0.34', 'grok_path': '/synthetic-offline/grok'}
    authored = request.compile_provider_prompt(tmp_path, 'entry', provider='grok_cli')
    inputs['prompt'] = authored['prompt']
    native = request.prepare_grok_native(inputs, observation)
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'Synthetic offline fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'Synthetic serial windows',
        'segments': [{'dialogue_index': i, 'text_sha256': request.digest(line['text']), 'language': 'en',
            'word_count': 2, 'words_per_minute': 180, 'pause_seconds': 0, 'rationale': 'Synthetic fixture'}
            for i, line in enumerate(shot['dialogue'])],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key, 'value_sha256': request.digest(shot[key]),
            'start_seconds': start, 'end_seconds': end, 'rationale': 'Synthetic fixture'}
            for key, start, end in [('dominant_action', 4.1, 7), ('completed_end_state', 7, 7.4)]]}
    compiled = request.prepare_compiled_request(inputs, native, {'source': 'real'}, coverage=authored['coverage'], timing=timing)
    write(tmp_path, 'artifacts/compiled_request-final.json', compiled)
    write(tmp_path, 'artifacts/preparation_review-final.json', {'version': '1.0', 'review_id': 'final',
        'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass', 'subject_sha256': request.digest(compiled),
        'evidence_kind': 'reviewed', 'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC software fixture only'} for p in sorted(request.PREDICATES)]})
    return contract, inputs, observation


def test_grok_final_inline_batch_scope_revalidates_after_original_reservation(tmp_path):
    contract, inputs, observation = grok_final_fixture(tmp_path)
    scope = pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    inputs['governance']['scope_id'] = scope['id']
    write(tmp_path, 'production_attempts/final-reservation/request.json', {'attempt_id': 'final-reservation',
        'scope_id': scope['id'], 'scope': scope, 'shot_id': 'entry', 'story_revision': contract['story_revision'],
        'media_kind': 'motion', 'phase': 'first_pass', 'request_sha256': scope['requests']['entry'],
        'submitted_inputs': copy.deepcopy(inputs)})
    assert pa.validate_derived_scope(tmp_path, scope, inputs=inputs, observation=observation)
    with pytest.raises(pa.StandingSetupRequired, match='batch allowance exhausted'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    assert len(execution._attempts(tmp_path)) == 1


@pytest.mark.parametrize('second_referenced', [False, True])
def test_standing_two_native_boards_zero_source_calls_preserve_transfer_batch(neutral_package, second_referenced):
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package); spec = package['boards']
    keep = {'start', 'other'}
    spec['image_nodes'] = [n for n in spec['image_nodes'] if n['slot_id'] in keep]
    for node in spec['image_nodes']:
        node['reference_ids'] = ['cast'] if second_referenced and node['slot_id'] == 'other' else []
        node['image_dependencies'] = []
    spec['shots'] = [s for s in spec['shots'] if s['shot_id'] in {'s1', 's3'}]
    for shot in spec['shots']: shot['roles'] = [r for r in shot['roles'] if r['asset_id'] in keep or r['asset_id'] == 'cast']
    spec['shared_roles'] = [r for r in spec['shared_roles'] if r['asset_id'] == 'cast']
    template = spec['motion_binding']['contract_template']; template['payoff_asset_id'] = 'other'
    template['assets'] = [a for a in template['assets'] if a['id'] in keep]
    template['shots'] = [s for s in template['shots'] if s['id'] in {'s1', 's3'}]
    for shot in template['shots']: shot['asset_ids'] = [a for a in shot['asset_ids'] if a in keep]
    package['shots'] = [s for s in package['shots'] if s['id'] in {'s1', 's3'}]
    request.prepare_package(root, bind(package))
    marker = json.loads((root / 'project.json').read_text()); marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root); script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        baselines[shot['id']] = {'projection': projection,
            'planned_request_template': execution.planned_request_template({'duration': shot['duration_seconds']}, project_dir=root),
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    policy = make_policy(project_id=contract['project_id'], story_revision=contract['story_revision'],
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    accounts = {'grok_cli': 'fixture-grok', 'openart_cli': 'fixture-cli', 'native_imagegen': 'fixture-native'}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in spec['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': 2, 'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': provider, 'account': account, 'roles': ['identity', 'start_frame', 'end_frame'],
            'generated_board_sources': True, 'max_files': 8, 'max_file_bytes': 10000,
            'max_batch_bytes': 80000, 'max_batches': 1} for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'], 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    pa.activate_standing_run(root, policy=policy, baselines=baselines, standing_setup=standing,
        run_command=command, identity_id='fixture-identity')
    retained = images.activate_standing_image_envelope(root)
    for slot in ('start', 'other'):
        prepared = boards.prepare_image_request(root, slot, images=images)
        assert bool(prepared['packet']['references']) == (second_referenced and slot == 'other')
        scope = images.derive_native_image_scope(root, prepared['packet'], slot, prepared['arguments'], output_path=prepared['output_path'])
        call = boards.reserve_board_image(root, slot, scope_id=scope, call_id='fixture-' + slot, images=images)
        envelope = images.begin_native_image(root, call['call_id'])
        # Exact already-counted original admission excludes itself from batch count.
        images._standing_native_transfer(root, retained, prepared['packet'], exclude_call_id=call['call_id'])
        images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope, name='receipt-' + slot))
        boards.bind_imported_board(root, slot, call['call_id'], images=images)
    assert images.image_usage(root)['total'] == 2
    calls = images._load(root)['calls']
    assert sum(bool(call['packet']['references']) for call in calls) == int(second_referenced)
    if second_referenced:
        with pytest.raises(pa.StandingSetupRequired, match='batch allowance exhausted'):
            images._standing_native_transfer(root, retained, calls[-1]['packet'])


@pytest.mark.parametrize('tamper', [None, 'handoff', 'reference', 'mixed', 'spec', 'pointer', 'opaque'])
def test_standing_board_materialization_derives_exact_authored_handoff_only(neutral_package, tamper):
    """Promoted reviewed boards may add only the retained package's exact handoff rows."""
    from lib import production_boards as boards
    from lib.shot_contract import ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['shots'][0]['pinned_final_frame'] = {'required': True, 'requirement_id': 's1-ending',
                                                 'end_state': package['shots'][0]['end_state']}
    first = 'cast' if tamper in ('mixed', 'opaque') else 'start'
    end = 'end--final' if tamper == 'opaque' else 'end'
    if tamper == 'opaque':
        # Opaque base IDs may contain '--'; only the exact shot alias suffix is special.
        package['boards'] = json.loads(json.dumps(package['boards']).replace('"end"', '"end--final"'))
    if tamper in ('mixed', 'opaque'):
        # Retained reviewed reuse starts the handoff; the same-run board ends it.
        package['boards']['motion_binding']['contract_template']['assets'].append(
            {'id': 'cast', 'role': 'start_frame', 'cast_ids': []})
    package['boards']['motion_binding']['motion_handoffs'] = {first: {'asset_id': first, 'scene_id': 's1',
        'approved': True, 'pinned_final_frame': {'asset_id': end, 'approved': True, 'requirement_id': 's1-ending'}}}
    request.prepare_package(root, bind(package))
    marker = json.loads((root / 'project.json').read_text()); marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root); script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        baselines[shot['id']] = {'projection': projection,
            'planned_request_template': execution.planned_request_template({'duration': shot['duration_seconds']}, project_dir=root),
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    assert baselines['s1']['projection']['manifest_requirements']['motion_handoffs'] == {}
    policy = make_policy(project_id=contract['project_id'], story_revision=contract['story_revision'],
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    accounts = {'grok_cli': 'fixture-grok', 'openart_cli': 'fixture-cli', 'native_imagegen': 'fixture-native'}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in package['boards']['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': 4, 'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': provider, 'account': account,
            'roles': ['identity', 'start_frame', 'end_frame', 'payoff_board'], 'generated_board_sources': True,
            'max_files': 8, 'max_file_bytes': 10000, 'max_batch_bytes': 80000, 'max_batches': 4}
            for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'], 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    pa.activate_standing_run(root, policy=policy, baselines=baselines, standing_setup=standing,
        run_command=command, identity_id='fixture-identity')
    step = execution.advance(root)
    reviewed = set()
    while {'start', end} - reviewed:
        assert step['status'] == 'host_action' and step['action']['kind'] == 'receive_native_image', step.get('reason', step)
        slot = images.image_call_status(root, step['host_envelope']['call_id'])['slot_id']
        received = execution.complete_action(root, step['action'],
            outcome={'host_receipt': host_receipt(root, step['host_envelope'], name='receipt-' + slot)})
        assert slot in boards.load_board_pack(root)['board_bindings']
        step = received
        reviewed.add(slot)
    manifest_path = root / 'artifacts/asset_manifest.json'
    current = pa.current_projection(root, 's1')['manifest_requirements']
    assert current['motion_handoffs'] == package['boards']['motion_binding']['motion_handoffs']
    assert {a['id'] for a in current['assets']} >= {first, end}
    if tamper in ('mixed', 'opaque'):
        row = next(a for a in current['assets'] if a['id'] == 'cast')
        assert row['source_tool'] == 'provided'
    if tamper in ('handoff', 'reference', 'spec', 'pointer'):
        manifest = json.loads(manifest_path.read_text())
        if tamper in ('handoff', 'spec', 'pointer'):
            manifest['metadata']['motion_handoffs']['start']['pinned_final_frame']['requirement_id'] = 'other-ending'
        if tamper == 'spec':
            # Live pack spec rewritten consistently (digest recomputed) while the
            # hash-bound retained package and activation stay unchanged.
            pack_path = root / boards.ARTIFACT
            pack = json.loads(pack_path.read_text())
            pack['specification']['motion_binding']['motion_handoffs']['start']['pinned_final_frame']['requirement_id'] = 'other-ending'
            pack['specification_sha256'] = boards._digest(pack['specification'])
            pack_path.write_text(json.dumps(pack))
            assert boards.load_board_pack(root)['specification']['motion_binding']['motion_handoffs'] == \
                manifest['metadata']['motion_handoffs']
        if tamper == 'pointer':
            # New hash-bound package with the altered handoff, pack pointer and live
            # spec repointed consistently; the retained image envelope is unchanged.
            altered = copy.deepcopy(package)
            altered['boards']['motion_binding']['motion_handoffs']['start']['pinned_final_frame']['requirement_id'] = 'other-ending'
            data = json.dumps(altered).encode()
            (root / 'notes/package-altered.json').write_bytes(data)
            pack_path = root / boards.ARTIFACT
            pack = json.loads(pack_path.read_text())
            pack['package_binding'] = {'path': 'notes/package-altered.json', 'sha256': hashlib.sha256(data).hexdigest()}
            pack['specification'] = altered['boards']
            pack['specification_sha256'] = boards._digest(pack['specification'])
            pack_path.write_text(json.dumps(pack))
            boards.load_board_pack(root)
        else:
            manifest['metadata'].setdefault('reference_assets', {})['cast'] = {'scene_id': 's1', 'role': 'identity'}
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(pa.AutonomyError, match='changed outside approved flex: /manifest_requirements'):
            pa.require_active_policy(root, shot_id='s1')
        return
    pa.require_active_policy(root, shot_id='s1')
    projection = pa.current_projection(root, 's1')
    assert pa.contract_delta(root, 's1', projection) == (None, [])


def test_prepare_motion_actual_grok_dry_run_observes_fresh_outside_lock_and_resumes_scope(tmp_path, monkeypatch):
    """Actual derive, canonical dry run, preflight and registered execute; only CLI boundaries are fake."""
    import fcntl
    from tools import _grok_cli_media as media
    from tools.base_tool import ToolResult
    from tools.video import grok_cli_video
    planning = {'preferred_tool': 'grok_cli_video', 'hosting_provider': 'grok_cli', 'preferred_provider': 'grok_cli',
        'preferred_provider_gap': 0.1, 'allowed_providers': ['grok_cli'], 'task_context': 'synthetic offline plan',
        'target_operation': 'reference_to_video'}
    contract, inputs, observation = grok_final_fixture(tmp_path, allow_unknown_cost=True, **planning)
    lock = tmp_path / '.production-execution.lock'
    observed, submitted = [], []
    def observe(path, *, cwd):
        if lock.exists():
            with lock.open('a+b') as probe:  # raises if production lock is held during observation
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        observed.append(str(cwd)); return dict(observation)
    def transport(**kw):
        submitted.append(copy.deepcopy(kw))
        return ToolResult(success=True, data={'cli_version': observation['cli_version'],
            'session_id': kw['session_id'], 'dispatch_status': 'synthetic_offline'})
    monkeypatch.setattr(media, 'observe_grok_cli_compatibility', observe)
    monkeypatch.setattr(grok_cli_video, 'execute_grok_cli_media', transport)
    monkeypatch.setenv('GROK_CLI_PATH', observation['grok_path'])
    action = execution._production_action(tmp_path, 'prepare_motion', subject='entry', binding={}, host=True)
    monkeypatch.setattr(execution, 'next_production_action', lambda _: action)
    real = execution._execute_governed
    def crash(*a): raise RuntimeError('synthetic crash after canonical dry run before reservation')
    monkeypatch.setattr(execution, '_execute_governed', crash)
    outcome = {'provider': 'grok_cli', 'inputs': copy.deepcopy(inputs), 'observation': dict(observation)}
    with pytest.raises(RuntimeError, match='before reservation'):
        execution.complete_action(tmp_path, action, outcome=copy.deepcopy(outcome))
    scopes = json.loads((tmp_path / 'production_scopes.json').read_text())['scopes']
    assert len(scopes) == 1 and submitted == [] and not execution._attempts(tmp_path)
    assert len(observed) == 2  # dry run and execute each observe afresh
    monkeypatch.setattr(execution, '_execute_governed', real)
    result = execution.complete_action(tmp_path, action, outcome=copy.deepcopy(outcome))
    assert json.loads((tmp_path / 'production_scopes.json').read_text())['scopes'] == scopes
    assert result['status'] == 'dispatched' and result['dry_run']['governed'] and result['dry_run']['provider_calls'] == 0
    assert result['result']['success'] is True, result['result']
    assert len(submitted) == 1 and len(execution._attempts(tmp_path)) == 1 and len(observed) == 4
    # Native transport receives only CLI controls; the durable record keeps the
    # full governed request (planning fields included) bound to its digest.
    assert not set(planning) & set(submitted[0]['arguments'])
    attempt = execution._attempts(tmp_path)[0]
    assert {k: attempt['submitted_inputs'][k] for k in planning} == planning
    assert attempt['request_sha256'] == scopes[0]['requests']['entry']


def test_shared_identity_reference_first_consumed_by_later_shot_keeps_stable_aliases(neutral_package):
    # Shared identity_reference actually consumed only by s2: compile, validation
    # and local motion promotion must agree on one alias per consumer shot.
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package)
    spec = package['boards']
    reference = spec['references'][0]
    reference['role'] = 'identity_reference'
    reference['applicability']['roles'] = ['identity_reference', 'start_frame', 'end_frame']
    spec['shared_roles'][0]['role'] = 'identity_reference'
    template = spec['motion_binding']['contract_template']
    template['assets'].append({'id': 'cast', 'role': 'identity_reference', 'cast_ids': ['actor']})
    for shot in template['shots']:
        shot['cast_ids'] = ['actor'] if shot['id'] == 's2' else []
    binding = bind(package)
    assert request.prepare_package(root, binding)['status'] == 'prepared'
    manifest = json.loads((root / 'artifacts/asset_manifest.json').read_text())
    pairs = [(a['id'], a['scene_id']) for a in manifest['assets'] if a['id'].startswith('cast')]
    assert pairs == [('cast', 's1'), ('cast--s2', 's2'), ('cast--s3', 's3')]
    assert all(boards.motion_reference_alias(spec, 'cast', sid) == aid for aid, sid in pairs)
    first = (root / 'artifacts/asset_manifest.json').read_bytes()
    request.prepare_package(root, binding)
    assert (root / 'artifacts/asset_manifest.json').read_bytes() == first
    assert images.image_usage(root)['total'] == 0


def file_sha256_of(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def board_factory_fixture(neutral_package, *, image_allowance=8, required_start=(), extra_start_role=False,
                          image_policy_fallback=None, text_only_boards=False):
    """Activated synthetic board run; every host return below is fixture bytes."""
    root, package, bind = neutral_package
    package = prospective_package(package)
    next(n for n in package['boards']['image_nodes'] if n['slot_id'] == 'start')['required_predicates'] += list(required_start)
    if extra_start_role:
        package['boards']['shots'][0]['roles'].append({'asset_id': 'start', 'role': 'end_frame'})
    if text_only_boards:
        for node in package['boards']['image_nodes']:
            node['reference_ids'], node['image_dependencies'] = [], []
    request.prepare_package(root, bind(package))
    marker = json.loads((root / 'project.json').read_text())
    marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root)
    script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        baselines[shot['id']] = {'projection': projection,
            'planned_request_template': execution.planned_request_template({'duration': shot['duration_seconds']}, project_dir=root),
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    policy = make_policy(project_id=contract['project_id'], story_revision='r1',
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    accounts = {'grok_cli': 'fixture-grok', 'openart_cli': 'fixture-cli', 'native_imagegen': 'fixture-native'}
    if image_policy_fallback is not None:
        # Same standing MCP video account; the image model lives only in the run term.
        policy['providers'].append({'id': 'openart_mcp', 'billing': 'unknown_cost_no_ceiling',
            'exposure_acknowledgement': 'no_enforceable_credit_ceiling',
            'uid_sha256': request.digest('fixture-mcp-account'), 'project_id': 'fixture-mcp',
            'routes': [{'model': 'byte-plus-seedance-2-mini', 'mode': 'image2video'}]})
        accounts['openart_mcp'] = 'fixture-mcp-account'
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in package['boards']['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': image_allowance,
                          'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': provider, 'account': account,
            'roles': ['identity', 'identity_reference', 'start_frame', 'end_frame', 'payoff_board'],
            'generated_board_sources': True, 'max_files': 8, 'max_file_bytes': 10000,
            'max_batch_bytes': 80000, 'max_batches': 8} for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing-boards', setup)
    command = creator_fixture(root, 'command-boards', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': 'r1', 'command': 'make_video',
        'standing_setup_sha256': standing['sha256'],
        **({'image_policy_fallback': image_policy_fallback} if image_policy_fallback is not None else {})})
    pa.activate_standing_run(root, policy=policy, baselines=baselines,
        standing_setup=standing, run_command=command, identity_id='fixture-identity')
    images.activate_standing_image_envelope(root)
    return root


def retain_factory_board(root, slot, *, predicates=None, omitted=(), roles=None):
    from lib import production_boards as boards
    prepared = boards.prepare_image_request(root, slot, images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], slot, prepared['arguments'], output_path=prepared['output_path'])
    call = boards.reserve_board_image(root, slot, scope_id=scope, call_id='fixture-' + slot, images=images)
    envelope = images.begin_native_image(root, call['call_id'])
    images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope, name='fixture-' + slot))
    source = boards.bind_imported_board(root, slot, call['call_id'], images=images)
    names = set(boards.load_board_pack(root)['prompt_pack'][slot]['required_predicates']) | set(predicates or {})
    review = attestation(source['sha256'], sorted(names - set(omitted)), revision='r1')
    review.update(review_id='fixture-review-' + slot, reviewer='persistent reviewer')
    for predicate in review['predicates']:
        predicate['severity'] = 'critical'
        predicate['status'] = (predicates or {}).get(predicate['name'], 'pass')
    review['status'] = ('fail' if any(p['status'] == 'fail' for p in review['predicates']) else
                        'unknown' if any(p['status'] == 'unknown' for p in review['predicates']) else 'pass')
    boards.record_board_review(root, slot, review,
        roles=roles or boards._roles(boards.load_board_pack(root), slot), images=images)
    return source, review


def reference_review_factory_fixture(neutral_package, *, motion_identity=True, direct_pack=False):
    """Synthetic prospective run with retained identity evidence lacking quality."""
    root, package, bind = neutral_package
    package = copy.deepcopy(package)
    package['shots'] = package['shots'][:1]
    spec = package['boards']
    spec['shots'] = spec['shots'][:1]
    spec['image_nodes'] = [n for n in spec['image_nodes'] if n['slot_id'] != 'other']
    for node in spec['image_nodes']:
        node['shot_id'] = 's1'
    spec['shared_roles'] = [{'asset_id': 'cast', 'role': 'identity_reference'},
                            {'asset_id': 'payoff', 'role': 'payoff_board'}]
    reference = spec['references'][0]
    reference['role'] = 'identity_reference'
    reference['applicability']['roles'] = ['identity_reference', 'start_frame']
    reference['review']['predicates'] = [
        {'name': name, 'status': 'pass', 'severity': 'critical',
         'evidence': 'SYNTHETIC retained identity observation: ' + name}
        for name in ('cast_identity', 'cast_count', 'prop_body_invariants')]
    # This second applicable role is deliberately outside the motion contract.
    spec['shots'][0]['roles'].append({'asset_id': 'cast', 'role': 'start_frame'})
    template = spec['motion_binding']['contract_template']
    template['shots'] = template['shots'][:1]
    template['assets'] = [a for a in template['assets'] if a['id'] != 'other']
    if motion_identity:
        template['assets'].append({'id': 'cast', 'role': 'identity_reference', 'cast_ids': ['actor']})
        template['shots'][0]['cast_ids'] = ['actor']
    if direct_pack:
        from lib import production_boards as boards
        boards.prepare_board_pack(root, spec, images=images)
        return root
    root = board_factory_fixture((root, package, bind))
    for slot in ('start', 'end', 'payoff'):
        retain_factory_board(root, slot)
    return root


def test_motion_reference_evidence_never_adds_factory_review_gate(neutral_package):
    root = reference_review_factory_fixture(neutral_package, direct_pack=True)
    assert not (root / 'artifacts/shot_contract.json').exists()
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert action['kind'] not in {'review_board', 'review_planning', 'review_selection'}
    assert file_bytes(root) == before


def frame_shapes_fixture(neutral_package, monkeypatch, *, cast_bearing=False, episode_controls=None, public_layout=False):
    """Legacy incompatible synthetic activation, complete current board evidence."""
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package)
    spec = package['boards']; template = spec['motion_binding']['contract_template']
    template['reference_mode'] = 'frame_conditioned'
    node_by_id = {n['slot_id']: n for n in spec['image_nodes']}
    node_by_id['payoff']['shot_id'] = 's4'
    node_by_id['other']['shot_id'] = 's2'
    third_start = copy.deepcopy(node_by_id['start']); third_start.update(slot_id='third-start', shot_id='s3', output_path='assets/images/third-start.png')
    third_end = copy.deepcopy(node_by_id['end']); third_end.update(slot_id='third-end', shot_id='s3', output_path='assets/images/third-end.png', image_dependencies=['third-start'])
    spec['image_nodes'] += [third_start, third_end]
    spec['shots'] = [{'shot_id': sid, 'roles': roles, 'video_dependencies': []} for sid, roles in [
        ('s1', [{'asset_id': 'start', 'role': 'start_frame'}, {'asset_id': 'end', 'role': 'end_frame'}]),
        ('s2', [{'asset_id': 'other', 'role': 'start_frame'}]),
        ('s3', [{'asset_id': 'third-start', 'role': 'start_frame'}, {'asset_id': 'third-end', 'role': 'end_frame'}]),
        ('s4', [{'asset_id': 'payoff', 'role': 'start_frame'}, {'asset_id': 'payoff', 'role': 'payoff_board'}])]]
    template['assets'] = [{'id': n['slot_id'], 'role': n['role'], 'cast_ids': []} for n in spec['image_nodes']]
    if cast_bearing:
        identity = spec['references'][0]
        identity['role'] = 'identity_reference'
        identity['applicability']['roles'] = ['identity_reference']
        identity['review']['predicates'].append({'name': 'cast_count', 'severity': 'critical',
            'status': 'pass', 'evidence': 'SYNTHETIC current identity observation'})
        second = copy.deepcopy(identity)
        path = root / 'assets/images/second-cast.png'; path.write_bytes(b'SYNTHETIC second identity')
        second.update(id='second-cast', path=str(path.relative_to(root)), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        second['review'].update(review_id='second-cast-review', subject_sha256=second['sha256'])
        spec['references'].append(second)
        spec['shared_roles'] = [{'asset_id': r['id'], 'role': 'identity_reference'} for r in spec['references']]
        spec['shared_roles'].append({'asset_id': 'payoff', 'role': 'payoff_board'})
        for asset in template['assets']:
            asset['cast_ids'] = ['actor'] + (['second-actor'] if asset['id'] in {'payoff', 'third-start', 'third-end'} else [])
        template['assets'] += [{'id': aid, 'role': 'identity_reference', 'cast_ids': [cid]}
            for aid, cid in [('cast', 'actor'), ('second-cast', 'second-actor')]]
        for node in spec['image_nodes']:
            if node['shot_id'] in {'s3', 's4'}:
                node['reference_ids'].append('second-cast')
    original_shot = copy.deepcopy(template['shots'][0])
    template['shots'] = []
    authored_shot = copy.deepcopy(package['shots'][0]); package['shots'] = []
    for mapping in spec['shots']:
        sid = mapping['shot_id']; shot = copy.deepcopy(original_shot)
        shot.update(id=sid, asset_ids=list(dict.fromkeys(r['asset_id'] for r in mapping['roles'])), upstream=[])
        shot['frame_input'] = {'asset_id': next(r['asset_id'] for r in mapping['roles'] if r['role'] == 'start_frame'), 'role': 'start_frame'}
        if cast_bearing:
            shot['cast_ids'] = ['actor'] + (['second-actor'] if sid in {'s3', 's4'} else [])
        template['shots'].append(shot)
        authored = copy.deepcopy(authored_shot); authored.update(id=sid, duration_seconds=5, script='SYNTHETIC exact ' + sid)
        if cast_bearing:
            authored['cast'] = shot['cast_ids']
        package['shots'].append(authored)
    request.prepare_package(root, bind(package))
    kwargs = standing_treatment_fixture(root, treatment='frame_conditioned')
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    setup['native_images']['image_allowance'] = 8
    if public_layout:
        # Exact synthetic account/project and public controls are retained at activation.
        from lib import openart_mcp as mcp
        provider = next(p for p in kwargs['policy']['providers'] if p['id'] == 'openart_mcp')
        provider.update(uid_sha256=mcp.account_summary()['uid_sha256'], project_id='fixture-project')
        setup['providers'] = copy.deepcopy(kwargs['policy']['providers'])
    kwargs['policy']['caps'].update(max_total_attempts=4, max_attempts_per_shot=1, max_repair_attempts=0)
    setup['caps'] = copy.deepcopy(kwargs['policy']['caps'])
    for term in setup['source_transfer']:
        term['max_batches'] = 8
    if episode_controls is not None:
        setup['episode_controls'] = copy.deepcopy(episode_controls)
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    for sid, value in kwargs['baselines'].items():
        mapping = next(s for s in spec['shots'] if s['shot_id'] == sid)
        aid = next(r['asset_id'] for r in mapping['roles'] if r['role'] == 'start_frame')
        value['planned_request_template'] = execution.planned_request_template(
            {'duration': value['projection']['shot']['duration_seconds'], 'image_path': {'$board': {'asset_id': aid}},
             **({'resolution': '720p', 'aspect_ratio': '9:16'} if public_layout else {})}, project_dir=root)
    pa.activate_standing_run(root, **kwargs)
    images.activate_standing_image_envelope(root)
    for slot in ('payoff', 'start', 'end', 'other', 'third-start', 'third-end'):
        retain_factory_board(root, slot, predicates={'cast_count': 'pass'})
    contract = execution.load_shot_contract(root)
    from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    for shot in contract['shots']:
        shot['review'] = attestation(shot_review_subject(contract, shot['id']), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    return root


def test_frame_conditioned_prospective_activation_allows_pending_board_observations(neutral_package):
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package)
    template = package['boards']['motion_binding']['contract_template']
    template['reference_mode'] = 'frame_conditioned'
    # Existing authored role declarations select inputs; no observed bytes yet.
    package['boards']['shots'][1]['roles'] = [{'asset_id': 'payoff', 'role': 'start_frame'}, {'asset_id': 'payoff', 'role': 'payoff_board'}]
    template['shots'][1]['asset_ids'] = ['payoff']
    for shot, mapping in zip(template['shots'], package['boards']['shots']):
        shot['frame_input'] = next(r for r in mapping['roles'] if r['role'] == 'start_frame')
    request.prepare_package(root, bind(package))
    kwargs = standing_treatment_fixture(root)
    contract = execution.load_shot_contract(root); contract['reference_mode'] = 'frame_conditioned'
    write(root, 'artifacts/shot_contract.json', contract)
    for sid, baseline in kwargs['baselines'].items():
        baseline['projection'] = pa.current_projection(root, sid)
        baseline['approval_plan_sha256'] = execution.approval_plan_digest(contract, sid)
    result = pa.activate_standing_run(root, **kwargs)
    assert result['provider_calls'] == 0 and not boards.load_board_pack(root)['observations'][-1].get('asset_id') == 'start'
    assert images.image_usage(root)['total'] == 0
    assert all(a.get('preparation_pending') for a in contract['assets'])


def prepared_frame_shapes(neutral_package, monkeypatch, provider, *, public_layout=False):
    from lib import openart_mcp as mcp, openart_mcp_jobs as mcp_jobs
    root = frame_shapes_fixture(neutral_package, monkeypatch, cast_bearing=True, public_layout=public_layout)
    contract = execution.load_shot_contract(root)
    assert [s['duration_seconds'] for s in contract['shots']] == [5]*4
    frame_sources = {}
    if provider == 'openart_mcp':
        profile = mcp.load_profile('byte-plus-seedance-2-mini', 'image2video', require='candidate')
        assets = {a['id']: a for a in contract['assets']}
        ids = [s['frame_input']['asset_id'] for s in contract['shots']]
        files = [{'path': str(root / assets[aid]['path']), 'sha256': assets[aid]['sha256'], 'type': 'image'} for aid in ids]
        evidence = write(root, 'notes/SYNTHETIC-upload-approval.json', {'SYNTHETIC': 'offline upload receipts only'})
        write(root, 'artifacts/openart_mcp_upload_authorization-frame-fixture.json', {
            'provider':'openart_mcp','status':'approved','purpose':'source_transfer_only', 'project_id':contract['project_id'],
            'story_revision':'r1','account_uid_sha256':mcp.account_summary()['uid_sha256'], 'openart_project_id':'fixture-project',
            'files':files,'no_enforceable_credit_ceiling':True,'delayed_charges_unknown':True,'max_upload_batches':1,
            'approved_by':'SYNTHETIC software fixture', 'evidence':evidence})
        upload = mcp_jobs.prepare_upload(root, files=[f['path'] for f in files],
            billing_declaration={'upload_authorization_id':'frame-fixture'}, openart_project_id='fixture-project')
        returned = {'projectId':'fixture-project','results':[{'fileId':'fixture-'+str(i), 'status':'SUCCESS',
            'visualReference':{'id':'frame-ready-'+str(i),'type':'image','url':'https://fixture.example.test/'+str(i),
                'label':'SYNTHETIC frame '+str(i),'imageUrl':'https://fixture.example.test/'+str(i),
                'thumbnailUrl':'https://fixture.example.test/'+str(i),'metadata':{'fixture_only':True}}} for i in range(4)]}
        ready = mcp_jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=returned)
        frame_sources = {aid: {'source_path': row['path'], 'upload_id': ref['upload_id']}
            for aid, row, ref in zip(ids, files, ready['references'])}
    else:
        profile = {'source':'fixture'}
    before_usage = images.image_usage(root)
    prepared = {}
    for index, shot in enumerate(contract['shots']):
        sid = shot['id']; aid = shot['frame_input']['asset_id']
        authored = request.compile_provider_prompt(root, sid, provider=provider)
        inputs = {'project_dir':str(root), 'governance':{'shot_id':sid}, 'prompt':authored['prompt'],
            'duration':5, **({'resolution': '720p', 'aspect_ratio': '9:16'} if public_layout else {}),
            'output_path':str(root/'assets/video'/('SYNTHETIC-'+sid+'.mp4')),
            'compiled_request_id':'frame-'+sid, 'preparation_review_id':'frame-'+sid}
        if provider == 'grok_cli':
            asset = next(a for a in contract['assets'] if a['id'] == aid)
            inputs.update(operation='reference_to_video', image_path=str(root / asset['path']))
            native = request.prepare_grok_native(inputs, {'cli_version':'1.0.34','grok_path':'/SYNTHETIC/offline/grok'})
        else:
            inputs.update(model=profile['model'], mode=profile['mode'], openart_project_id='fixture-project',
                input_assets=[{'role':'first_frame', **frame_sources[aid]}])
            if public_layout:
                inputs['image_path'] = inputs.pop('input_assets')[0]['source_path']
            native = request.prep_builder(provider)(inputs, profile)
        timing = {'method':'segmented_estimate','duration_seconds':5,'language':'en','margin_seconds':.5,
            'rationale':'SYNTHETIC software fixture', 'overlap_policy':'serial','overlap_rationale':'SYNTHETIC serial windows',
            'segments':[], 'action_windows':[{'source_pointer':f'/shot_contract/shots/{index}/'+key,
                'value_sha256':request.digest(shot[key]), 'start_seconds':start, 'end_seconds':end,
                'rationale':'SYNTHETIC action window'} for key,start,end in [('dominant_action',.1,3),('completed_end_state',3.1,4.4)]]}
        compiled = request.prepare_compiled_request(inputs, native, profile, coverage=authored['coverage'], timing=timing)
        review = {'version':'1.0','review_id':'frame-'+sid,'reviewer':'SYNTHETIC fixture only','status':'pass',
            'subject_sha256':request.digest(compiled),'evidence_kind':'fixture_only',
            'predicates':[{'name':p,'status':'pass','severity':'critical','evidence':'SYNTHETIC software fixture'} for p in sorted(request.PREDICATES)]}
        frame = next(a for a in contract['assets'] if a['id'] == aid)
        review['composite_boards'] = [{'sha256': frame['sha256'], 'role': 'start_frame',
            'members': [{key: a[key] for key in ('sha256', 'role', 'cast_ids')}
                for a in contract['assets'] if a['role'] == 'identity_reference'
                and set(a['cast_ids']).intersection(shot['cast_ids'])]}]
        write(root, 'artifacts/compiled_request-frame-'+sid+'.json', compiled)
        write(root, 'artifacts/preparation_review-frame-'+sid+'.json', review)
        assert request.validate_preparation(inputs, native, profile)['compiled_sha256'] == request.digest(compiled)
        before = file_bytes(root)
        assert len(pa.lock_proof(root, sid, inputs=inputs, native=native, profile=profile)) == 64
        assert file_bytes(root) == before
        assert compiled['source_binding']['frame_input']['asset_id'] == aid
        native_assets = native['request']['input_assets'] if provider == 'grok_cli' else native['input_assets']
        assert len([a for a in native_assets if a['role'] == 'first_frame']) == 1
        if sid == 's4':
            assert aid == 'payoff' and next(a for a in contract['assets'] if a['id'] == aid)['role'] == 'payoff_board'
        prepared[sid] = (inputs, native, profile, compiled, review)
    assert images.image_usage(root) == before_usage and before_usage['total'] == 6
    return root, prepared


@pytest.mark.parametrize('provider', ['grok_cli', 'openart_mcp'])
def test_four_sparse_frame_shapes_prepare_normal_native_and_compiled_evidence_offline(neutral_package, monkeypatch, guided_observations, provider):
    root, prepared = prepared_frame_shapes(neutral_package, monkeypatch, provider)
    policy, sha, _ = pa.require_active_policy(root)
    for sid, (inputs, native, profile, compiled, review) in prepared.items():
        current = pa.lock_proof(root, sid, inputs=inputs, native=native, profile=profile)
        # Newly admitted proofs replay against their exact retained digest too.
        assert pa._root_lock_proof(root, sid, inputs=inputs, native=native, profile=profile,
            historical={'policy': policy, 'policy_sha256': sha, 'compiled': compiled, 'review': review},
            retained_lock_digest=current) == current


@pytest.mark.parametrize('defect', ['target_bytes', 'target_review', 'start_role', 'same_bytes_wrong_path',
    'same_bytes_wrong_id', 'unknown_member', 'later_endpoint_member', 'wrong_cast_member', 'forged_source_binding',
    'changed_original_snapshot', 'explicit_source', 'explicit_cast', 'explicit_native_pin', 'canonical_suffix'])
def test_frame_target_and_carrier_admission_remains_exact(neutral_package, monkeypatch, defect):
    from lib import production_boards as boards
    root, prepared = prepared_frame_shapes(neutral_package, monkeypatch, 'grok_cli')
    inputs, native, profile, compiled, review = prepared['s1']
    contract = execution.load_shot_contract(root)
    assets = {a['id']: a for a in contract['assets']}
    policy, sha, _ = pa.require_active_policy(root)
    baselines = pa.retained_baselines(root, policy)
    if defect == 'target_bytes':
        (root / assets['end']['path']).write_bytes(b'SYNTHETIC changed endpoint target')
    elif defect == 'target_review':
        assets['end']['review']['subject_sha256'] = '0'*64
        write(root, 'artifacts/shot_contract.json', contract)
    elif defect == 'start_role':
        pack = boards.load_board_pack(root)
        pack['observations'] = [o for o in pack['observations'] if o['asset_id'] != 'start']
        write(root, boards.ARTIFACT, pack)
    elif defect == 'same_bytes_wrong_path':
        path = root / 'assets/images/SYNTHETIC-wrong-start.png'
        path.write_bytes((root / assets['start']['path']).read_bytes())
        inputs['image_path'] = str(path)
        native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'})
    elif defect == 'same_bytes_wrong_id':
        compiled['source_binding']['frame_input']['asset_id'] = 'other'
        review['subject_sha256'] = request.digest(compiled)
    elif defect in {'unknown_member', 'later_endpoint_member', 'wrong_cast_member'}:
        member = review['composite_boards'][0]['members'][0]
        if defect == 'unknown_member':
            member['sha256'] = '0'*64
        elif defect == 'later_endpoint_member':
            review['composite_boards'][0]['members'].append({k: assets['end'][k] for k in ('sha256', 'role', 'cast_ids')})
        else:
            member['cast_ids'] = ['second-actor']
    elif defect == 'forged_source_binding':
        pack = boards.load_board_pack(root)
        # A locally consistent extra binding cannot make the endpoint an actual
        # earlier start-image generation input.
        pack['board_bindings']['start']['source_bindings']['end'] = {k: assets['end'][k] for k in ('path', 'sha256')}
        write(root, boards.ARTIFACT, pack)
        review['composite_boards'][0]['members'].append({k: assets['end'][k] for k in ('sha256', 'role', 'cast_ids')})
    elif defect == 'changed_original_snapshot':
        pack = boards.load_board_pack(root)
        call = images._call(images._load(root), pack['board_bindings']['start']['call_id'])
        snapshot = Path(call['submitted_arguments']['referenced_image_paths'][0]); snapshot.chmod(0o644)
        snapshot.write_bytes(b'SYNTHETIC tampered frozen identity')
    elif defect == 'canonical_suffix':
        inputs['prompt'] += '\nSYNTHETIC additional native direction'
        native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'})
        compiled = request.prepare_compiled_request(inputs, native, profile,
            coverage=compiled['coverage'], timing=compiled['timing'])
        review['subject_sha256'] = request.digest(compiled)
    else:
        # Isolated exact-policy variants exercise explicit checks after complete
        # native/preparation validation; they are never production authority.
        if defect == 'explicit_source':
            # Composite identity proof cannot satisfy an explicit submitted
            # source-byte lock. Fixture generated boards share PNG bytes.
            policy['locked']['sources'] = [assets['cast']['sha256']]
        elif defect == 'explicit_cast':
            policy['locked']['cast'] = {'actor': [{k: assets['end'][k] for k in ('sha256', 'role')}]}
        else:
            policy['locked']['controls'] = {'last_frame': {'value_sha256': assets['end']['sha256']}}
        content = pa.evidence_content(policy, baselines)
        sha = pa.digest(content)
        policy['evidence'] = write(root, 'approvals/autonomy-policy-' + sha + '.json', content)
        before = file_bytes(root)
        with pytest.raises(pa.AutonomyError, match='explicit source|explicit cast|native locked control absent'):
            pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile,
                historical={'policy': policy, 'policy_sha256': sha, 'compiled': compiled, 'review': review})
        assert file_bytes(root) == before
        return
    write(root, 'artifacts/compiled_request-frame-s1.json', compiled)
    write(root, 'artifacts/preparation_review-frame-s1.json', review)
    before = file_bytes(root)
    with pytest.raises((ValueError, KeyError, OSError)):
        pa.lock_proof(root, 's1', inputs=inputs, native=native, profile=profile)
    assert file_bytes(root) == before


def test_legacy_frame_proof_replays_only_exact_retained_digest(neutral_package, monkeypatch):
    root, prepared = prepared_frame_shapes(neutral_package, monkeypatch, 'grok_cli')
    inputs, native, profile, compiled, review = prepared['s1']
    contract = execution.load_shot_contract(root)
    end = next(a for a in contract['assets'] if a['id'] == 'end')
    review['composite_boards'][0]['members'].append({k: end[k] for k in ('sha256', 'role', 'cast_ids')})
    policy, sha, _ = pa.require_active_policy(root)
    historical = {'policy': policy, 'policy_sha256': sha, 'compiled': compiled, 'review': review}
    # Exact legacy digest freezes the formerly accepted declared member. The
    # same row cannot authorize a new request or an arbitrary replay digest.
    legacy = pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile,
        historical=historical, _legacy_frame=True)
    before = file_bytes(root)
    assert pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile,
        historical=historical, retained_lock_digest=legacy) == legacy
    with pytest.raises(pa.AutonomyError, match='actual generated carrier input'):
        pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile,
            historical=historical, retained_lock_digest='0'*64)
    assert file_bytes(root) == before


@pytest.mark.parametrize('variant', ['ordered_original_requests', 'provided_distinct_roles', 'duplicate_exact_member'])
def test_frame_carrier_supported_provenance_and_member_roles(neutral_package, monkeypatch, variant):
    from lib import production_boards as boards
    from lib.shot_contract import frame_conditioned_start
    root, prepared = prepared_frame_shapes(neutral_package, monkeypatch, 'grok_cli')
    inputs, native, profile, _, review = prepared['s1']
    contract = execution.load_shot_contract(root)
    pack = boards.load_board_pack(root)
    frame = frame_conditioned_start(contract, contract['shots'][0], root)
    if variant == 'ordered_original_requests':
        journal = images._load(root)
        call = images._call(journal, pack['board_bindings']['start']['call_id'])
        scopes = json.loads((root / 'production_scopes.json').read_text())
        scope = next(s for s in scopes['scopes'] if s['id'] == call['scope_id'])
        scope['requests'][call['slot_id']] = [call['request_sha256']]
        call['scope_sha256'] = images.digest(scope)
        before = file_bytes(root)
        # Isolate the supported ordered-scope representation without rewriting
        # the immutable original image authority of this fixture.
        with monkeypatch.context() as patch:
            patch.setattr(images, '_load', lambda _: journal)
            patch.setattr(images, '_scope', lambda _, sid: scope if sid == call['scope_id'] else None)
            pa._frame_carrier_members(root, frame, pa.current_projection(root, 's1'), review['composite_boards'])
    else:
        # Pure conditional-provenance compatibility: provided/reused boards
        # retain exact named semantic proof without inventing image receipts.
        pack['board_bindings']['start']['origin'] = 'reuse'
        write(root, boards.ARTIFACT, pack)
        projection = pa.current_projection(root, 's1')
        members = [{k: a[k] for k in ('sha256', 'role', 'cast_ids')}
            for a in projection['assets'] if a['id'] in {'start', 'end'}]
        assert members[0]['sha256'] == members[1]['sha256'] and members[0]['role'] != members[1]['role']
        composites = [{'sha256': frame['sha256'], 'role': 'start_frame', 'members': members}]
        before = file_bytes(root)
        if variant == 'duplicate_exact_member':
            composites[0]['members'].append(copy.deepcopy(members[0]))
            with pytest.raises(pa.AutonomyError, match='one exact approved source'):
                pa._frame_carrier_members(root, frame, projection, composites)
        else:
            pa._frame_carrier_members(root, frame, projection, composites)
    assert file_bytes(root) == before


@pytest.mark.parametrize('defect', [None, 'source_bytes', 'script', 'native_prompt', 'compiled_coverage',
    'review', 'delivery_proof', 'scope_cap', 'request_digest', 'checkpoint'])
def test_submitted_delivery_original_replays_after_revocation_without_new_admission(tmp_path, monkeypatch, defect):
    """Only compatibility observation/transport are synthetic; all owners run."""
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    _, _, inputs = standing_proposal_delivery_fixture(root, full_native=True)
    inputs['output_path'] = str(root/'SYNTHETIC-original.mp4')
    inputs['allow_unknown_cost'] = True  # existing SYNTHETIC standing Grok authority
    observation = {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'}
    authored = request.compile_provider_prompt(root, 'entry', provider='grok_cli')
    inputs['prompt'] = authored['prompt']
    native = request.prepare_grok_native(inputs, observation)
    shot = execution.load_shot_contract(root)['shots'][0]
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC software fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC serial windows',
        'segments': [{'dialogue_index': i, 'text_sha256': request.digest(line['text']), 'language': 'en',
            'word_count': 2, 'words_per_minute': 180, 'pause_seconds': 0, 'rationale': 'SYNTHETIC fixture'}
            for i, line in enumerate(shot['dialogue'])],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key, 'value_sha256': request.digest(shot[key]),
            'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC fixture'}
            for key, start, end in [('dominant_action', 4.1, 7), ('completed_end_state', 7, 7.4)]]}
    compiled = request.prepare_compiled_request(inputs, native, {'source': 'real'}, coverage=authored['coverage'], timing=timing)
    write(root, 'artifacts/compiled_request-fixture.json', compiled)
    write(root, 'artifacts/preparation_review-fixture.json', {'version': '1.0', 'review_id': 'fixture',
        'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass', 'subject_sha256': request.digest(compiled),
        'evidence_kind': 'reviewed', 'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC software fixture only'} for p in sorted(request.PREDICATES)]})
    request.validate_preparation(inputs, native, {'source': 'real'})
    before = file_bytes(root)
    scope = pa.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    pa.validate_derived_scope(root, scope, inputs=inputs, observation=observation)
    assert scope['derived_from_policy']['compiled_request_sha256'] == request.digest(compiled)
    assert all((root / path).read_bytes() == raw for path, raw in before.items()
               if path != Path('artifacts/decision_log.json'))
    assert execution._attempts(root) == []

    from tools import _grok_cli_media as media
    from tools.video import grok_cli_video
    from tools.base_tool import ToolResult
    calls=[]
    monkeypatch.setattr(media, 'observe_grok_cli_compatibility', lambda *a, **kw: dict(observation))
    def transport(**kw):
        calls.append(copy.deepcopy(kw))
        return ToolResult(success=True, data={'cli_version': observation['cli_version'],
            'session_id': kw['session_id'], 'dispatch_status': 'SYNTHETIC submitted original'})
    monkeypatch.setattr(grok_cli_video, 'execute_grok_cli_media', transport)
    monkeypatch.setenv('GROK_CLI_PATH', observation['grok_path'])
    inputs['governance']['scope_id']=scope['id']
    tool=grok_cli_video.GrokCLIVideo(grok_path=observation['grok_path'], sessions_root=str(root/'sessions'))
    log_path = root / 'artifacts/decision_log.json'
    approved_log = log_path.read_bytes()
    revoked_log = json.loads(approved_log)
    revoked_log['decisions'].append(dict(revoked_log['decisions'][0],
        decision_id='SYNTHETIC-before-submission-revocation', selected='strict'))
    write(root, 'artifacts/decision_log.json', revoked_log)
    with pytest.raises(execution.ProductionGovernanceError, match='revoked'):
        tool.execute(inputs)
    assert not calls and execution._attempts(root) == []
    log_path.write_bytes(approved_log)
    result=tool.execute(inputs)
    assert result.success, result.error
    assert len(calls)==1
    attempts=execution._attempts(root)
    assert len(attempts)==1
    original=attempts[0]
    assert original['scope']['derived_from_policy']['delivery_approval']==scope['derived_from_policy']['delivery_approval']
    frozen=json.loads((root/'production_attempts'/original['attempt_id']/'shot_contract.json').read_text())
    pa.validate_policy_attempt(root, original, original['scope'], frozen)
    refresh_fixture_proposal(root, lambda proposal: proposal['metadata']['delivery'].update(profile='generic_hd'))
    pa.validate_policy_attempt(root, original, original['scope'], frozen)  # changed proposal alone still accepts original proof
    log=json.loads((root/'artifacts/decision_log.json').read_text())
    log['decisions'].append(dict(log['decisions'][0],decision_id='SYNTHETIC-independent-revocation',selected='strict'))
    write(root,'artifacts/decision_log.json',log)
    before=file_bytes(root)
    with pytest.raises(pa.AutonomyError,match='revoked'):
        pa.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    pa.validate_policy_attempt(root, original, original['scope'], frozen)
    assert file_bytes(root)==before and len(calls)==1
    with pytest.raises(pa.AutonomyError, match='revoked'):
        request.compile_provider_prompt(root, 'entry', provider='grok_cli')
    mutated = copy.deepcopy(original)
    directory = root / 'production_attempts' / original['attempt_id']
    if defect == 'source_bytes':
        Path(original['input_assets'][0]['original_path']).write_bytes(b'SYNTHETIC changed source')
    elif defect == 'script':
        script = json.loads((root / 'artifacts/script.json').read_text())
        script['sections'][0]['text'] += ' SYNTHETIC altered dialogue'
        write(root, 'artifacts/script.json', script)
    elif defect in {'native_prompt', 'compiled_coverage', 'review'}:
        path = directory / 'autonomy_preparation.json'
        payload = json.loads(path.read_text())
        if defect == 'native_prompt':
            payload['native']['request']['arguments']['prompt'] += ' SYNTHETIC altered prompt'
        elif defect == 'compiled_coverage':
            payload['compiled']['coverage'][0]['fragment_sha256'] = '0' * 64
        else:
            payload['review']['predicates'][0]['status'] = 'unknown'
        path.chmod(0o600)  # Deliberate adversarial tampering of immutable fixture bytes.
        write(root, str(path.relative_to(root)), payload)
        # Even rebinding the public file pointer cannot forge the retained proof.
        mutated['autonomy_preparation']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif defect == 'delivery_proof':
        mutated['scope']['derived_from_policy']['delivery_approval']['artifact']['content'] += ' '
    elif defect == 'scope_cap':
        mutated['scope']['attempts_per_shot']['entry'] = 2
    elif defect == 'request_digest':
        mutated['request_sha256'] = '0' * 64
    elif defect == 'checkpoint':
        mutated['scope']['derived_from_policy']['delivery_approval']['checkpoint']['content'] += ' '
    before = file_bytes(root)
    if defect is None:
        pa.validate_policy_attempt(root, mutated, mutated['scope'], frozen)
    else:
        with pytest.raises((pa.AutonomyError, ValueError)):
            pa.validate_policy_attempt(root, mutated, mutated['scope'], frozen)
    assert file_bytes(root) == before and len(calls) == 1


def compile_original_mcp(root, inputs, request_id, timing):
    from lib import openart_mcp as mcp
    inputs = copy.deepcopy(inputs)
    inputs.update(compiled_request_id=request_id, preparation_review_id=request_id)
    authored = request.compile_provider_prompt(root, 'entry', provider='openart_mcp', model=inputs['model'])
    inputs['prompt'] = authored['prompt']
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='supported')
    assert profile['readiness']['empirical_result']['status'] == 'not_tested'
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    compiled = request.prepare_compiled_request(inputs, native, profile,
        coverage=authored['coverage'], timing=timing)
    review = execution._read(root / 'artifacts/preparation_review-original.json')
    review.update(review_id=request_id, subject_sha256=request.digest(compiled))
    write(root, f'artifacts/compiled_request-{request_id}.json', compiled)
    write(root, f'artifacts/preparation_review-{request_id}.json', review)
    request.validate_preparation(inputs, native, profile)
    return inputs, native, compiled


def test_adjacent_native_pin_clips_share_one_prepared_boundary(tmp_path):
    """One authored image occupies clip one's end pin and clip two's start pin."""
    from tests.lib.test_production_execution import (
        _reviewed_pin_request, ImageTool, MotionTool, write_scopes,
    )
    from tests.lib.test_shot_contract import refresh
    from lib.shot_contract import file_sha256
    first, motion_scope, contract = _reviewed_pin_request(tmp_path)
    boundary = tmp_path / 'assets/end.svg'
    # Prepare the declared boundary once, before either original motion request.
    boundary.unlink()
    image_inputs = {'project_dir': str(tmp_path), 'prompt': 'SYNTHETIC shared authored boundary',
        'image_path': str(tmp_path / 'assets/reference.png'), 'output_path': str(boundary),
        'governance': {'scope_id': 'boundary-image', 'shot_id': 'boundary'}}
    image_scope = copy.deepcopy(motion_scope)
    image_scope.update(id='boundary-image', phase='image',
        requests={'boundary': execution.planned_request_digest(image_inputs, project_dir=tmp_path)},
        attempts_per_shot={'boundary': 1})
    image_scope.pop('approval_plan_sha256')
    write_scopes(tmp_path, image_scope)
    image_tool = ImageTool()
    assert image_tool.execute(image_inputs).success and image_tool.calls == 1
    end = next(a for a in contract['assets'] if a['id'] == 'end')
    end['sha256'] = end['review']['subject_sha256'] = file_sha256(boundary)
    shared_start = copy.deepcopy(end)
    shared_start.update(id='boundary-start', role='start_frame')
    second_end = copy.deepcopy(end)
    second_end.update(id='second-end', path='assets/second-end.svg')
    (tmp_path / second_end['path']).write_bytes(b'SYNTHETIC distinct second endpoint')
    second_end['sha256'] = second_end['review']['subject_sha256'] = file_sha256(tmp_path / second_end['path'])
    contract['assets'].extend([shared_start, second_end])
    second_shot = copy.deepcopy(contract['shots'][0])
    second_shot.update(id='second', initial_state=contract['shots'][0]['completed_end_state'],
        asset_ids=['boundary-start', 'second-end', 'patient'])
    contract['shots'].append(second_shot)
    refresh(contract)
    (tmp_path / 'shot_contract.json').write_text(json.dumps(contract))
    second = copy.deepcopy(first)
    second.update(first_frame=str(boundary), last_frame=str(tmp_path / second_end['path']),
        output_path=str(tmp_path / 'assets/second.mp4'))
    second['governance']['shot_id'] = 'second'
    motion_scope['approval_plan_sha256'] = execution.approval_plan_digest(contract)
    motion_scope['requests'] = {i['governance']['shot_id']:
        execution.planned_request_digest(i, project_dir=tmp_path) for i in (first, second)}
    motion_scope['attempts_per_shot'] = {'entry': 1, 'second': 1}
    (tmp_path / 'production_scopes.json').write_text(json.dumps(
        {'version': '1.0', 'scopes': [image_scope, motion_scope]}))
    tool = MotionTool()
    for inputs in (first, second):
        assert inputs['operation'] == 'first_last_frame'
        assert execution.preflight(tool, inputs)['governed']
        assert tool.execute(inputs).success
    records = [json.loads(path.read_text()) for path in (tmp_path / 'production_attempts').glob('*/request.json')]
    originals = {r['shot_id']: r for r in records if r['phase'] == 'first_pass'}
    first_end = next(a for a in originals['entry']['input_assets'] if a['role'] == 'last_frame')
    next_start = next(a for a in originals['second']['input_assets'] if a['role'] == 'first_frame')
    assert first['last_frame'] == second['first_frame'] == str(boundary)
    assert first_end['sha256'] == next_start['sha256'] == file_sha256(boundary)
    assert Path(first_end['path']).read_bytes() == Path(next_start['path']).read_bytes() == boundary.read_bytes()
    assert all(not shot['upstream'] for shot in contract['shots'])
    assert image_tool.calls == 1 and tool.calls == 2
    assert len(records) == 3 and sum(r['phase'] == 'image' for r in records) == 1
