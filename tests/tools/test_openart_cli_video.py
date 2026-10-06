"""Synthetic U2 integration boundaries; never authenticate or call a provider."""
import pytest
from lib.production_execution import ProductionGovernanceError, preflight
from tools.video.openart_cli_video import OpenArtCLIVideo
from tools.provider_pricing import PriceQuoteRequired

@pytest.mark.parametrize('inputs', [{}, {'prompt':'synthetic','output_path':'/tmp/synthetic.mp4'}, {'resume_job':'synthetic'}])
def test_direct_openart_is_strict_only(inputs):
    with pytest.raises(ProductionGovernanceError, match='OpenArt requires strict'):
        preflight(OpenArtCLIVideo(), inputs)

def test_cost_unknown_and_no_retry():
    tool = OpenArtCLIVideo()
    with pytest.raises(PriceQuoteRequired):
        tool.estimate_cost({})
    assert tool.retry_policy.max_retries == 0
    assert tool.supports['explicit_selection_only'] is True

def synthetic_openart_project(tmp_path, monkeypatch):
    # Explicit U2/U3 synthetic transport isolation: this helper has no credit approval.
    from lib import openart_dispatch
    monkeypatch.setattr(openart_dispatch,"prepare_dispatch",lambda *args,**kwargs:None)
    """Component seam only: fake qualification/transport, not live compatibility."""
    import copy
    import json
    from lib import production_provenance
    monkeypatch.setattr(production_provenance,'_ALLOW_OPENART_FIXTURE_PROVENANCE',True)
    monkeypatch.setattr(production_provenance,'_ALLOW_OPENART_COMPONENT_PREPARATION',True)
    from pathlib import Path
    from lib import openart_jobs as jobs, production_execution as execution
    from tests.lib.test_production_execution import project, write_scopes
    from lib.shot_contract import file_sha256
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',lambda *args: None)
    inputs, scope, contract = project(tmp_path, motion=True)
    inputs.update(model='synthetic-model', mode='image2video', operation='image_to_video',
                  aspect_ratio='16:9', resolution='synthetic-resolution')
    # Approved single-image path carries reviewed identity; no required native ending pin.
    inputs.pop('last_image_path')
    inputs.pop('reference_image_paths')
    scope['provider'] = 'openart_cli'
    scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=tmp_path)
    write_scopes(tmp_path, scope)
    profile = {'source':'fixture','profile_sha256':'b'*64,'account_id_sha256':'a'*64,
               'cli_version':'synthetic','tier':'synthetic','form_sha256':'f'*64,
               'form_defaults_sha256':'d'*64,'mode':'image2video','model':'synthetic-model'}
    state = {'submits':0,'collections':0,'pending':False, 'frozen':{}, 'launch':{}, 'events':{}, 'output':None}
    def native(inputs, profile, dry_run=None):
        controls = copy.deepcopy(inputs)
        controls['image_path'] = {'sha256':file_sha256(inputs['image_path'])}
        argv = ['synthetic-cli', jobs.sha256_json(controls), '--async']
        return {'argv':argv,'native_controls':controls,'native_controls_sha256':jobs.sha256_json(controls),
                'native_argv_sha256':jobs.sha256_json(argv),'profile_sha256':profile['profile_sha256'],
                'account_id_sha256':profile['account_id_sha256'],'cli_version':'synthetic',
                'tier':'synthetic','form_sha256':'f'*64,'native_body_sha256':'c'*64}
    def freeze(aid, inputs, native, profile):
        value = {'inputs':copy.deepcopy(inputs),'native':copy.deepcopy(native),'profile':copy.deepcopy(profile)}
        digest = jobs.sha256_json(value)
        state['frozen'][aid] = dict(value,snapshot_id=aid,snapshot_sha256=digest)
        return {'snapshot_id':aid,'snapshot_sha256':digest,'profile_sha256':profile['profile_sha256']}
    def evidence(aid):
        return {'state':'collected' if state['output'] else 'open', 'binding':state['launch'][aid]['binding'],
                'job_id_sha256':'j'*64,'events_sha256':jobs.sha256_json(state['events'][aid]),
                'output':state['output'],'billing':'unknown','release_authorized':False}
    def launch(root,binding,native,profile,**kwargs):
        state['submits'] += 1
        aid = binding['attempt_id']
        assert aid not in state['launch']
        state['launch'][aid] = {'binding':copy.deepcopy(binding),'argv':native['argv'] + jobs.cli.GLOBAL_FLAGS,
                                'cli_version':'synthetic','tier':'synthetic','form_sha256':'f'*64}
        state['events'][aid] = [{'type':'parsed','job_id_sha256':'j'*64}]
        return {'attempt_id':aid,'status':'submitted','job_id_sha256':'j'*64,
                'launch_sha256':jobs.sha256_json(state['launch'][aid])}
    def collect(aid, *, output_path, output_root, profile, timeout):
        state['collections'] += 1
        if state['pending']:
            return {'status':'pending','output':None,'billing':'held','release_authorized':False}
        assert output_path.is_relative_to(output_root)
        if state.get('recovery_path'):
            output_path = output_path.with_name(output_path.stem + '.r1' + output_path.suffix)
        output_path.write_bytes(b'synthetic original OpenArt footage')
        state['output'] = {'path':str(output_path),'sha256':file_sha256(output_path),'size':output_path.stat().st_size}
        state['events'][aid].append(dict(state['output'], type='collected', receipt_sha256='r'*64))
        return {'status':'collected','output':state['output'],'billing':'unknown','release_authorized':False}
    def verify(aid, profile=None):
        ev = state['events'][aid]
        terminal = [event for event in ev if event.get('type') == 'collected']
        if ev[0].get('job_id_sha256') != 'j'*64 or not terminal[-1].get('receipt_sha256'):
            raise ProductionGovernanceError('synthetic original receipt differs')
        got = state['output']
        if file_sha256(got['path']) != got['sha256']:
            raise ProductionGovernanceError('synthetic collected bytes differ')
        return {'attempt_id':aid,'job_record_sha256':'e'*64, 'binding':copy.deepcopy(state['launch'][aid]['binding']),'job_id_sha256':'j'*64,
                'evidence':{'snapshot_sha256':state['frozen'][aid]['snapshot_sha256']},'output':copy.deepcopy(got),'billing':'unknown','release_authorized':False}
    monkeypatch.setattr(jobs,'load_qualification',lambda *a,**k:copy.deepcopy(profile))
    monkeypatch.setattr(jobs,'native_request',native)
    monkeypatch.setattr(jobs,'prepare_native_request',native,raising=False)
    monkeypatch.setattr(jobs,'freeze_request',freeze,raising=False)
    monkeypatch.setattr(jobs,'load_frozen_request',lambda aid:copy.deepcopy(state['frozen'][aid]),raising=False)
    monkeypatch.setattr(jobs,'launch_submit',launch)
    monkeypatch.setattr(jobs,'launch_record',lambda aid:copy.deepcopy(state['launch'].get(aid)))
    monkeypatch.setattr(jobs,'read_events',lambda aid:copy.deepcopy(state['events'].get(aid,[])))
    monkeypatch.setattr(jobs,'recover_launch',lambda aid: {'status':'submitted'})
    monkeypatch.setattr(jobs,'collect_job',collect)
    monkeypatch.setattr(jobs,'reconcile_job',evidence)
    monkeypatch.setattr(jobs,'verify_collection_receipt',verify,raising=False)
    monkeypatch.setattr(jobs,'_RESERVATION_LOOKUP',lambda root,aid,digest:{'state':'active','attempt_id':aid,
                        'request_sha256':digest,'reservation_id':aid})
    return inputs, scope, state

def test_missing_ledger_zero_launch(tmp_path, monkeypatch):
    from lib import openart_jobs as jobs
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs,'_RESERVATION_LOOKUP',jobs._no_ledger)
    with pytest.raises(Exception, match='reservation'):
        OpenArtCLIVideo().execute(inputs)
    assert state['submits'] == 0
    assert not list((tmp_path/'production_attempts').glob('*/provider_result.json'))

def test_synthetic_submit_collect_original_once(tmp_path, monkeypatch):
    from lib.production_execution import collect_openart_attempt
    from lib.production_provenance import validate_attempt_provenance
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    result = OpenArtCLIVideo().execute(inputs)
    assert result.data['dispatch_status'] == 'submitted_async'
    assert result.cost_usd is None
    aid = result.data['production_attempt_id']
    digest = result.data['production_request_sha256']
    state['pending'] = True
    assert collect_openart_attempt(tmp_path,aid,request_sha256=digest)['status'] == 'pending'
    state['pending'] = False
    collected = collect_openart_attempt(tmp_path,aid,request_sha256=digest)
    assert collected['status'] == 'generated'
    assert collect_openart_attempt(tmp_path,aid,request_sha256=digest) == collected
    checked = validate_attempt_provenance(tmp_path,aid,shot_id='entry',story_revision='story-1',
                                         expected_output=collected['output'])
    assert checked['result']['status'] == 'generated'
    assert state['submits'] == 1 and state['collections'] == 2

def test_missing_compiled_preparation_zero_launch(tmp_path, monkeypatch):
    from lib import production_execution as execution
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',None)
    with pytest.raises(ProductionGovernanceError, match='validator is not installed'):
        OpenArtCLIVideo().execute(inputs)
    assert state['submits'] == 0 and not (tmp_path/'production_attempts').exists()

def test_original_job_authorized_recovery_path_preserves_occupied_original(tmp_path, monkeypatch):
    from pathlib import Path
    from lib.production_execution import collect_openart_attempt
    from lib.production_provenance import validate_attempt_provenance
    inputs, _, state = synthetic_openart_project(tmp_path,monkeypatch)
    result = OpenArtCLIVideo().execute(inputs)
    original = Path(inputs['output_path'])
    original.write_bytes(b'occupied unrelated original destination')
    state['recovery_path'] = True
    aid = result.data['production_attempt_id']
    collected = collect_openart_attempt(tmp_path,aid,request_sha256=result.data['production_request_sha256'])
    assert collected['output']['path'] != str(original)
    assert original.read_bytes() == b'occupied unrelated original destination'
    validate_attempt_provenance(tmp_path,aid,shot_id='entry',story_revision='story-1',
                                expected_output=collected['output'])
    # Later billing/status events are separate from the immutable collection proof.
    state['events'][aid].append({'type':'billing_unresolved'})
    validate_attempt_provenance(tmp_path,aid,shot_id='entry',story_revision='story-1',
                                expected_output=collected['output'])
    assert state['submits'] == 1

# Reuse the existing fake executable fixture; actual U2 APIs remain unmocked.
from tests.lib.test_openart_jobs import env as actual_jobs_env

@pytest.mark.parametrize('terminal', ['collected','failed_terminal'])
def test_actual_jobs_api_dispatch_collect_compatibility(actual_jobs_env, monkeypatch, terminal, legacy_u2_u3_bridge_isolation):
    """Actual private receipts/builders, synthetic CLI only; no live qualification."""
    import contextlib
    import json
    from pathlib import Path
    import subprocess
    from lib import openart_jobs as jobs, production_execution as execution, production_provenance
    from tools import _openart_cli as cli
    from tests.lib.test_openart_jobs import upload_profile, approve, receipt, active_ledger, fake_download, calls
    from tests.lib.test_production_execution import project, write_scopes
    from lib.shot_contract import file_sha256
    root = actual_jobs_env['tmp']/'governed'
    root.mkdir()
    inputs, scope, contract = project(root,motion=True)
    from lib import production_request as preparation
    from tests.lib.test_production_request import write
    write(root/'artifacts/scene_plan.json',{'version':'1.0','scenes':[{'id':'entry','type':'generated',
        'description':'Fixture entry','start_seconds':0,'end_seconds':8,'script_section_id':'entry-script'}]})
    write(root/'artifacts/script.json',{'version':'1.0','title':'Fixture','total_duration_seconds':8,
        'sections':[{'id':'entry-script','text':'Enter creature','start_seconds':0,'end_seconds':8}]})
    authored = preparation.compile_prompt(root,'entry')
    inputs['prompt'] = authored['prompt']
    inputs.pop('last_image_path')
    inputs.pop('reference_image_paths')
    inputs.update(operation='image_to_video',mode='image2video',model='m-turbo',image_upload_id='up-integration')
    profile = upload_profile(actual_jobs_env)
    # Match the retained fixture contract with fresh fake account/version/form
    # observations. These actual checks stay enabled; no real provider is used.
    form = {'schema': {'type': 'object', 'properties': {'prompt': {'type': 'string'},
            'duration': {'type': 'integer', 'default': 5}, 'image': {'type': 'string'}}}}
    binary = actual_jobs_env['tmp']/'bin/openart'
    fake = binary.read_text()
    fake = fake.replace('if args[:1] == ["account"]:',
        'if args[:1] == ["version"]:\n    print(json.dumps({"version": "0.1.1"}))\n'
        'elif args[:2] == ["model", "form"]:\n    print(' + repr(json.dumps(form)) + ')\n'
        'elif args[:1] == ["account"]:')
    guarantee = {'nonspending': True, 'no_delayed_charge': True}
    fake = fake.replace('{"user": {"id": os.environ.get("FAKE_ACCOUNT", "acct-1")}}',
        '{"user": {"id": os.environ.get("FAKE_ACCOUNT", "acct-1"), "tier": "subscription"}, '
        '"contract": ' + repr(guarantee) + '}')
    fake = fake.replace('{"url": "https://up.openart.test/r.png"}',
        '{"url": "https://up.openart.test/r.png", "contract": ' + repr(guarantee) + '}')
    binary.write_text(fake)
    profile.pop('profile_sha256', None)
    profile['json_paths']['account_tier'] = 'user.tier'
    profile['form_sha256'] = jobs.sha256_json(form)
    jobs.profile_path_for(profile['model'], profile['mode']).write_text(json.dumps(profile))
    profile = jobs.load_qualification(model=profile['model'], mode=profile['mode'], allow_fixture=True)
    original_load = jobs.load_qualification
    monkeypatch.setattr(jobs,'load_qualification',lambda *args,**kw: original_load(*args,**dict(kw,allow_fixture=True)))
    monkeypatch.setattr(jobs,'_ALLOW_FIXTURE_LAUNCH',True)
    monkeypatch.setattr(jobs,'_ALLOW_FIXTURE_UPLOAD',True)
    monkeypatch.setattr(production_provenance,'_ALLOW_OPENART_FIXTURE_PROVENANCE',True)
    monkeypatch.setattr(execution,'_OPENART_COMPILED_REQUEST_CHECK',execution._compiled_request_check)
    write(root/'artifacts/upload_approval-up-integration.json',{'version':'1.0','upload_id':'up-integration',
        'asset_id':'start','shot_id':'entry','source_sha256':file_sha256(inputs['image_path']),
        'source_binding':preparation.source_packet(root,'entry')['binding'],'approved_by':'Fixture-only author',
        'evidence_path':'approval.txt','evidence_sha256':file_sha256(root/'approval.txt')})
    jobs.register_upload_approval_lookup(preparation.approved_upload_lookup)
    jobs.upload_reference(root,'up-integration',Path(inputs['image_path']),model='m-turbo',mode='image2video')
    url = jobs.upload_url_for('up-integration',profile=profile,source_sha256=file_sha256(inputs['image_path']))
    creative = cli.native_video_argv(inputs['prompt'],model=inputs['model'],mode=inputs['mode'],
                                    duration=inputs['duration'],image_url=url)
    body = {'model':inputs['model'],'media':'video','mode':inputs['mode'],
            'params':{'prompt':inputs['prompt'],'duration':inputs['duration'],'image':url}}
    rid,sha = receipt({'endpoint':profile['dry_run_endpoint'],'body':body},creative+['--dry-run']+cli.GLOBAL_FLAGS)
    inputs.update(native_dry_run_receipt_id=rid,native_dry_run_receipt_sha256=sha)
    inputs.update(compiled_request_id='actual-compiled',preparation_review_id='actual-review')
    native=jobs.prepare_native_request(execution._openart_controls(inputs),profile)
    shot=contract['shots'][0]
    timing={'method':'segmented_estimate','duration_seconds':8,'language':'en','margin_seconds':0.5,
        'rationale':'Fixture no dialogue; entry and completion windows','overlap_policy':'serial',
        'overlap_rationale':'Sequential fixture action windows','segments':[],
        'action_windows':[{'source_pointer':'/shot_contract/shots/0/dominant_action',
            'value_sha256':preparation.digest(shot['dominant_action']),'start_seconds':0,'end_seconds':6,'rationale':'Entry'},
            {'source_pointer':'/shot_contract/shots/0/completed_end_state',
            'value_sha256':preparation.digest(shot['completed_end_state']),'start_seconds':6,'end_seconds':7,'rationale':'Completion'}]}
    compiled=preparation.prepare_compiled_request(inputs,native,profile,coverage=authored['coverage'],timing=timing)
    review={'version':'1.0','review_id':'actual-review','reviewer':'Fixture-only author','status':'pass',
        'subject_sha256':preparation.digest(compiled),'evidence_kind':'fixture_only',
        'predicates':[{'name':name,'status':'pass','severity':'critical','evidence':'Fixture-only judgment'}
                      for name in sorted(preparation.PREDICATES)]}
    write(root/'artifacts/compiled_request-actual-compiled.json',compiled)
    write(root/'artifacts/preparation_review-actual-review.json',review)
    scope['provider']='openart_cli'
    scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=root)
    write_scopes(root,scope)
    jobs.register_reservation_lookup(lambda root,aid,digest:dict(active_ledger(root,aid,digest),reservation_id=aid))
    # Assert every subprocess boundary stays outside the project lock.
    locked = {'value':False}
    original_lock = execution._lock
    @contextlib.contextmanager
    def checked_lock(path):
        with original_lock(path):
            locked['value']=True
            try: yield
            finally: locked['value']=False
    monkeypatch.setattr(execution,'_lock',checked_lock)
    original_read = cli.run_readonly
    def checked_read(*args,**kw):
        assert not locked['value'], 'CLI under project lock'
        return original_read(*args,**kw)
    monkeypatch.setattr(cli,'run_readonly',checked_read)
    def checked_popen(*args,**kw):
        assert not locked['value'], 'submit under project lock'
        return subprocess.Popen(*args,**kw)
    monkeypatch.setattr(jobs,'_POPEN',checked_popen)
    result = OpenArtCLIVideo().execute(inputs)
    assert result.data['dispatch_status']=='submitted_async'
    aid=result.data['production_attempt_id']
    if terminal=='failed_terminal': monkeypatch.setenv('FAKE_STATUS','failed')
    else: fake_download(monkeypatch)
    record=execution.collect_openart_attempt(root,aid,request_sha256=result.data['production_request_sha256'])
    assert record['status']==('generated' if terminal=='collected' else 'failed')
    assert record['result']['cost_usd'] is None
    assert record['result']['data']['release_authorized'] is False
    if terminal=='collected':
        production_provenance.validate_attempt_provenance(root,aid,shot_id='entry',story_revision='story-1',
                                                         expected_output=record['output'])
    else:
        proof=jobs.verify_terminal_failure(aid,profile)
        assert record['result']['data']['openart_evidence']==proof
        assert proof['binding']['request_sha256']==result.data['production_request_sha256']
    assert len([call for call in calls(actual_jobs_env) if '--async' in call])==1


@pytest.fixture
def legacy_u2_u3_bridge_isolation(monkeypatch):
    # These synthetic transport/provenance tests predate credit authority. U4 uses
    # the real default bridge in integration/test_openart_dispatch_recovery.py.
    from lib import openart_dispatch
    monkeypatch.setattr(openart_dispatch,'prepare_dispatch',lambda *args,**kwargs:None)
