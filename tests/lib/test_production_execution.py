"""Offline factual authorization, reservations and immutable provider evidence."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tools.base_tool import BaseTool, ToolResult, ToolTier
from lib.production_execution import (ProductionGovernanceError, planned_request_digest,
    approval_plan_digest, preflight, reconcile_attempt)
from lib.shot_contract import file_sha256, contract_digest

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'first_pass'

class ImageTool(BaseTool):
    name = 'fake_image'
    provider = 'fake'
    capability = 'image_generation'
    tier = ToolTier.GENERATE
    calls = 0
    input_schema = {'properties': {'cli_session_id': {}}}
    def execute(self, inputs):
        self.calls += 1
        self.received = inputs
        directory = Path(inputs['output_path']).parents[0].parent / 'production_attempts' / inputs['cli_session_id']
        assert (directory / 'request.json').exists(), 'reservation must precede invocation'
        Path(inputs['output_path']).parent.mkdir(parents=True, exist_ok=True)
        Path(inputs['output_path']).write_bytes(b'mocked output')
        return ToolResult(success=True, artifacts=[inputs['output_path']], data={'nested': {'full': ['evidence']},'session_id':inputs['cli_session_id']})

class MotionTool(ImageTool):
    name = 'fake_video'
    capability = 'video_generation'
    def execute(self, inputs):
        # Call the implementation directly: inherited wrapper nesting is not a second dispatch.
        return ImageTool.execute.__wrapped__.__wrapped__(self, inputs)


def project(tmp_path, *, motion=False):
    (tmp_path/'project.json').write_text(json.dumps({'project_id':'offline-first-pass','story_revision':'story-1','governance':{'version':'1.0','mode':'strict'}}))
    (tmp_path/'approval.txt').write_text('User approved this exact offline batch.')
    (tmp_path/'assets').mkdir()
    (tmp_path/'assets'/'reference.png').write_bytes(b'approved input')
    inputs = {'project_dir':str(tmp_path), 'prompt':'Approved exact prompt',
              'image_path':str(tmp_path/'assets'/'reference.png'), 'output_path':str(tmp_path/'assets'/'output.mp4'),
              'governance':{'scope_id':'batch','shot_id':'entry'}}
    contract = None
    if motion:
        contract = json.loads((FIXTURES/'valid_shot_contract.json').read_text())
        for asset in contract['assets']:
            source = FIXTURES / asset['path']
            destination = tmp_path / asset['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        (tmp_path/'shot_contract.json').write_text(json.dumps(contract))
        inputs.update(image_path=str(tmp_path/'assets'/'start.svg'), last_image_path=str(tmp_path/'assets'/'end.svg'),
                      reference_image_paths=[str(tmp_path/'assets'/'patient.svg')],duration=8)
    scope = {'id':'batch','project_id':'offline-first-pass','story_revision':'story-1','status':'approved',
        'approved_by':'fixture user','evidence':{'path':'approval.txt','sha256':file_sha256(tmp_path/'approval.txt')},
        'phase':'first_pass' if motion else 'image','provider':'fake',
        'requests':{'entry':planned_request_digest(inputs, project_dir=tmp_path)}, 'attempts_per_shot':{'entry':5}}
    if motion:
        scope['approval_plan_sha256'] = approval_plan_digest(contract)
    write_scopes(tmp_path, scope)
    return inputs, scope, contract


def write_scopes(root, scope):
    (root/'production_scopes.json').write_text(json.dumps({'version':'1.0','scopes':[scope]}))


def attempt(root):
    return next((root/'production_attempts').glob('*/request.json'))


def test_image_bootstrap_does_not_require_motion_review_and_snapshots(tmp_path):
    inputs, _, _ = project(tmp_path)
    tool = ImageTool()
    result = tool.execute(inputs)
    assert tool.calls == 1
    assert 'governance' not in tool.received and 'project_dir' not in tool.received
    assert tool.received['image_path'] != inputs['image_path']
    Path(inputs['image_path']).write_bytes(b'mutated original')
    assert Path(tool.received['image_path']).read_bytes() == b'approved input'
    record = json.loads((attempt(tmp_path).parent/'result.json').read_text())
    assert record['status'] == 'generated'
    assert record['result']['data']['nested'] == {'full':['evidence']}
    assert record['output']['sha256'] == file_sha256(inputs['output_path'])
    assert result.data['production_attempt_id']


def test_motion_first_pass_ceiling_is_not_repair_permission(tmp_path):
    inputs, scope, contract = project(tmp_path, motion=True)
    tool = MotionTool()
    tool.execute(inputs)
    Path(inputs['output_path']).unlink()  # even deleting output does not authorize a reroll
    with pytest.raises(ProductionGovernanceError, match='first-pass ceiling'):
        tool.execute(inputs)
    assert tool.calls == 1


def test_exact_repair_scope_can_run_after_reconciled_attempt(tmp_path):
    inputs, scope, _ = project(tmp_path, motion=True)
    tool = MotionTool()
    first = tool.execute(inputs)
    scope.update(id='repair',phase='repair',replaces_attempt_ids=[first.data['production_attempt_id']])
    inputs['governance']['scope_id'] = 'repair'
    inputs['output_path'] = str(tmp_path/'assets'/'repair.mp4')
    scope['requests']['entry'] = planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    assert tool.execute(inputs).success
    assert len(list((tmp_path/'production_attempts').glob('*/request.json'))) == 2


@pytest.mark.parametrize('change', ['prompt','provider','approval','scope','contract_review'])
def test_invalid_binding_never_calls_provider(tmp_path,change):
    inputs,scope,contract = project(tmp_path,motion=True)
    if change == 'prompt': inputs['prompt'] = 'Another unapproved story'
    if change == 'provider': scope['provider'] = 'another'
    if change == 'approval': (tmp_path/'approval.txt').write_text('changed')
    if change == 'scope': scope['status'] = 'proposed'
    if change == 'contract_review':
        contract['project_review']['status'] = 'fail'
        (tmp_path/'shot_contract.json').write_text(json.dumps(contract))
    write_scopes(tmp_path,scope)
    tool = MotionTool()
    with pytest.raises(ProductionGovernanceError): tool.execute(inputs)
    assert tool.calls == 0
    assert not (tmp_path/'production_attempts').exists()


def test_offline_dry_run_has_same_gate_zero_reservations(tmp_path):
    inputs,_,_ = project(tmp_path,motion=True)
    tool = MotionTool()
    assert tool.dry_run(inputs)['provider_calls'] == 0
    assert not (tmp_path/'production_attempts').exists()
    inputs['prompt'] = 'Unapproved'
    with pytest.raises(ProductionGovernanceError): tool.dry_run(inputs)
    assert tool.calls == 0


def test_concurrent_allowance_reserves_once(tmp_path):
    inputs,scope,_ = project(tmp_path)
    scope['attempts_per_shot']['entry'] = 1
    write_scopes(tmp_path,scope)
    tool = ImageTool()
    def call():
        try: return tool.execute(inputs).success
        except ProductionGovernanceError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:call(), range(2))) == [False,True]
    assert tool.calls == 1


def test_timeout_and_interruption_preserve_original_session(tmp_path):
    inputs,_,_ = project(tmp_path)
    class Interrupted(ImageTool):
        def execute(self,inputs):
            self.session = inputs['cli_session_id']
            raise KeyboardInterrupt('uncertain original job')
    tool = Interrupted()
    with pytest.raises(KeyboardInterrupt): tool.execute(inputs)
    request = json.loads(attempt(tmp_path).read_text())
    assert request['cli_session_id'] == tool.session
    assert json.loads((attempt(tmp_path).parent/'result.json').read_text())['status'] == 'uncertain'
    with pytest.raises(ProductionGovernanceError,match='uncertain'): tool.execute(inputs)


def test_failure_retains_complete_result_data_and_blocks_retry(tmp_path):
    inputs,_,_ = project(tmp_path)
    class Timeout(ImageTool):
        def execute(self,inputs):
            return ToolResult(success=False,error='timeout',data={'dispatch_status':'indeterminate',
                 'session_id':inputs['cli_session_id'],'session_directory':'original-session','conditioning_receipt':{'raw':'retained'}})
    result = Timeout().execute(inputs)
    saved = json.loads((attempt(tmp_path).parent/'result.json').read_text())
    assert saved['result']['data']['conditioning_receipt'] == {'raw':'retained'}
    assert saved['status'] == 'uncertain'
    with pytest.raises(ProductionGovernanceError,match='uncertain'): Timeout().execute(inputs)


def test_arbitrary_existing_output_is_not_reusable(tmp_path):
    inputs,_,_ = project(tmp_path)
    Path(inputs['output_path']).write_bytes(b'large'*1000)
    with pytest.raises(ProductionGovernanceError,match='reconcile'): ImageTool().execute(inputs)


def test_reconcile_only_original_session_and_matching_inputs(tmp_path):
    inputs,_,_ = project(tmp_path)
    class Timeout(ImageTool):
        def execute(self,inputs): return ToolResult(success=False,data={'dispatch_status':'indeterminate','session_id':inputs['cli_session_id']})
    Timeout().execute(inputs)
    req = json.loads(attempt(tmp_path).read_text())
    output = Path(inputs['output_path']);output.write_bytes(b'original recovered output')
    result = ToolResult(success=True,artifacts=[str(output)],data={'session_id':'wrong','conditioning_receipt':{}})
    with pytest.raises(ProductionGovernanceError,match='original request/session'):
        reconcile_attempt(tmp_path, req['attempt_id'], result, request_sha256=req['request_sha256'])
    result.data = {'session_id':req['cli_session_id'],'conditioning_receipt':{'session_id':req['cli_session_id'],
        'submission_evidence':'verified_native_call','input_assets':req['input_assets']}}
    with pytest.raises(ProductionGovernanceError,match='native control binding'):
        reconcile_attempt(tmp_path,req['attempt_id'],result,request_sha256=req['request_sha256'])
    assert json.loads((attempt(tmp_path).parent/'result.json').read_text())['status'] == 'uncertain'


def test_nested_selector_and_provider_reserve_once(tmp_path):
    inputs,scope,_ = project(tmp_path)
    inputs['preferred_provider'] = 'fake'
    scope['requests']['entry'] = planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    provider = ImageTool()
    class Selector(ImageTool):
        provider = 'selector'
        def execute(self,inputs): return provider.execute(inputs)
    assert Selector().execute(inputs).success
    assert provider.calls == 1
    assert len(list((tmp_path/'production_attempts').glob('*/request.json'))) == 1
    assert (attempt(tmp_path).parent/'provider_result.json').exists()


def test_nested_selector_cannot_change_provider_or_retry(tmp_path):
    inputs,scope,_ = project(tmp_path)
    inputs['preferred_provider'] = 'fake'
    scope['requests']['entry'] = planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    other = ImageTool();other.provider = 'unapproved'
    class Selector(ImageTool):
        provider = 'selector'
        def execute(self,inputs): return other.execute(inputs)
    with pytest.raises(ProductionGovernanceError,match='switch providers'):
        Selector().execute(inputs)
    assert other.calls == 0


def test_motion_without_provider_end_pin_keeps_reviewed_end_board(tmp_path):
    inputs,scope,_ = project(tmp_path,motion=True)
    inputs['operation'] = 'image_to_video'
    del inputs['last_image_path'];del inputs['reference_image_paths']
    scope['requests']['entry'] = planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    assert MotionTool().execute(inputs).success


def test_governed_overridden_dry_run_never_calls_tool_body(tmp_path):
    inputs,_,_ = project(tmp_path)
    class UnsafeDryRun(ImageTool):
        def dry_run(self,inputs): raise AssertionError('would call network')
    assert UnsafeDryRun().dry_run(inputs)['reservations'] == 0


def test_preserved_output_is_immutable_original(tmp_path):
    inputs,_,_ = project(tmp_path)
    ImageTool().execute(inputs)
    record = json.loads((attempt(tmp_path).parent/'result.json').read_text())
    Path(inputs['output_path']).write_bytes(b'changed external output')
    assert Path(record['preserved_output']['path']).read_bytes() == b'mocked output'


def test_snapshot_digest_detects_source_changed_then_restored(tmp_path,monkeypatch):
    inputs,_,_ = project(tmp_path)
    source = Path(inputs['image_path'])
    original = Path.read_bytes
    def raced_read(path):
        if path == source: return b'unapproved snapshot bytes'
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',raced_read)
    tool = ImageTool()
    with pytest.raises(ProductionGovernanceError,match='changed while snapshotting|changed during reservation'):
        tool.execute(inputs)
    assert tool.calls == 0


def test_same_approved_repair_batch_covers_two_distinct_attempt_outputs(tmp_path):
    from lib.production_execution import record_rejection
    inputs,scope,_ = project(tmp_path,motion=True)
    tool = MotionTool()
    initial = tool.execute(inputs)
    scope.update(id='repair',phase='repair',replaces_attempt_ids=[initial.data['production_attempt_id']],attempts_per_shot={'entry':2})
    first = copy.deepcopy(inputs);first['governance']['scope_id']='repair';first['output_path']=str(tmp_path/'assets'/'repair-one.mp4')
    second = copy.deepcopy(first);second['output_path']=str(tmp_path/'assets'/'repair-two.mp4')
    scope['requests']['entry']=[planned_request_digest(first,project_dir=tmp_path),planned_request_digest(second,project_dir=tmp_path)]
    write_scopes(tmp_path,scope)
    rejected=tool.execute(first)
    review={'review_id':'reject-1','reviewer':'offline reviewer','story_revision':'story-1',
        'subject_sha256':file_sha256(first['output_path']),'status':'fail',
        'predicates':[{'name':'completed_action','status':'fail','evidence':'Synthetic incomplete action.'}]}
    record_rejection(tmp_path,rejected.data['production_attempt_id'],review)
    assert tool.execute(second).success
    assert Path(first['output_path']).read_bytes() == b'mocked output'
    assert (tmp_path/'production_attempts'/rejected.data['production_attempt_id']/'rejections').is_dir()
    with pytest.raises(ProductionGovernanceError,match='exhausted'):
        tool.execute(second)
    assert tool.calls == 3


def test_approval_plan_preserves_semantics_while_observed_handoff_fills(tmp_path):
    _,_,contract = project(tmp_path,motion=True)
    later=copy.deepcopy(contract['shots'][0]);later.update(id='later',upstream=[{'shot_id':'entry'}]);later.pop('review')
    contract['shots'].append(later)
    contract['assets'].append({'id':'dynamic','role':'start_frame','cast_ids':['patient'],
        'upstream_source':{'shot_id':'entry','role':'outgoing_frame'}})
    original=approval_plan_digest(contract)
    contract['assets'][-1].update(path='actual-outgoing.png',sha256='a'*64,review={'status':'pass'})
    contract['shots'][-1]['upstream'][0].update(attempt_id='actual',output_sha256='b'*64,outgoing_frame_sha256='a'*64,review_sha256='c'*64)
    assert approval_plan_digest(contract)==original
    contract['shots'][-1]['dominant_action']='Unapproved different action'
    assert approval_plan_digest(contract)!=original


def test_windows_lock_branch_does_not_import_fcntl(tmp_path,monkeypatch):
    import sys
    import os
    from types import SimpleNamespace
    from lib.production_execution import _lock
    calls=[]
    fake=SimpleNamespace(LK_LOCK=1,LK_UNLCK=2,locking=lambda fd,mode,size:calls.append((mode,size)))
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules,'msvcrt',fake)
        patch.setitem(sys.modules,'fcntl',None)
        patch.setattr(os,'name','nt')
        with _lock(tmp_path):
            pass
    assert calls == [(1,1),(2,1)]


def test_same_bytes_distinct_reference_occurrences_preserve_approval_paths(tmp_path):
    inputs,scope,_=project(tmp_path)
    twin=tmp_path/'assets'/'twin.png';twin.write_bytes(Path(inputs['image_path']).read_bytes())
    inputs['reference_image_paths']=[inputs['image_path'],str(twin)]
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    assert ImageTool().execute(inputs).success
    recorded=json.loads(attempt(tmp_path).read_text())['input_assets']
    assert recorded[-2]['original_path'] != recorded[-1]['original_path']
    assert recorded[-2]['path'] == recorded[-1]['path']


def test_strict_enrollment_preserves_existing_project_and_requires_revision(tmp_path):
    from lib.checkpoint import init_project, enroll_production_project
    with pytest.raises(ValueError,match='story_revision'):
        init_project('new',title='New',pipeline_type='cinematic',pipeline_dir=tmp_path,governance='strict')
    root=init_project('p',title='Existing',pipeline_type='cinematic',pipeline_dir=tmp_path)
    marker=json.loads((root/'project.json').read_text());marker['custom']='preserve'
    (root/'project.json').write_text(json.dumps(marker))
    enroll_production_project(root,story_revision='r1')
    enrolled=json.loads((root/'project.json').read_text())
    assert enrolled['custom']=='preserve' and enrolled['governance']['mode']=='strict'
    init_project('p',title='Existing',pipeline_type='cinematic',pipeline_dir=tmp_path)
    assert json.loads((root/'project.json').read_text())['governance']['mode']=='strict'


def test_template_static_approval_rejects_changed_source_bytes(tmp_path):
    from lib.production_execution import planned_request_template
    inputs,scope,_ = project(tmp_path)
    scope['requests']['entry'] = planned_request_template(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    Path(inputs['image_path']).write_bytes(b'not approved')
    tool = ImageTool()
    with pytest.raises(ProductionGovernanceError,match='static input'):
        tool.execute(inputs)
    assert tool.calls == 0
    assert not (tmp_path/'production_attempts').exists()


def test_historical_template_digest_uses_frozen_static_bindings(tmp_path):
    from lib.production_execution import planned_request_template, approved_request_digest
    inputs,_,_ = project(tmp_path)
    original = planned_request_digest(inputs,project_dir=tmp_path)
    template = planned_request_template(inputs,project_dir=tmp_path)
    Path(inputs['image_path']).unlink()
    assert approved_request_digest(template,project_dir=tmp_path,selected_attempts={}) == original


@pytest.mark.parametrize('field',['reference_audio_paths','reference_video_paths'])
def test_atlas_local_asset_arrays_are_hashed_and_snapshotted(tmp_path,field):
    inputs,scope,_=project(tmp_path)
    source=tmp_path/'assets'/('reference.wav' if field=='reference_audio_paths' else 'reference.mp4')
    source.write_bytes(b'approved media')
    inputs[field]=[str(source)]
    digest=planned_request_digest(inputs,project_dir=tmp_path)
    source.write_bytes(b'changed media')
    assert planned_request_digest(inputs,project_dir=tmp_path) != digest
    source.write_bytes(b'approved media')
    scope['requests']['entry']=digest
    write_scopes(tmp_path,scope)
    tool=ImageTool()
    assert tool.execute(inputs).success
    assert tool.received[field] != inputs[field]
    assert Path(tool.received[field][0]).read_bytes()==b'approved media'


@pytest.mark.parametrize('field,value',[('workflow_path','workflow.json'),('workflow_json',json.dumps({'1':{'inputs':{'image':'external.png'}}})),('reference_audio_urls',['https://example.test/audio.wav'])])
def test_strict_unsupported_media_fails_before_dispatch(tmp_path,field,value):
    inputs,_,_=project(tmp_path)
    (tmp_path/'workflow.json').write_text('{}')
    inputs[field]=value
    tool=ImageTool()
    with pytest.raises(ProductionGovernanceError):
        planned_request_digest(inputs,project_dir=tmp_path)
    assert tool.calls == 0


NATIVE_UNENCODED=['image_urls','image_input','image_uri','last_image','last_frame_uri','start_image_url',
                  'middle_image_url','mask','mask_url','audio_uri','audio_url','target_audio_url','video_uri',
                  'file','file_url','web_url','link']


@pytest.mark.parametrize('field',NATIVE_UNENCODED)
@pytest.mark.parametrize('form',['url','local','nested_url','nested_local'])
def test_strict_native_media_fields_fail_closed_before_dispatch(tmp_path,field,form):
    inputs,scope,_=project(tmp_path)
    value='https://example.test/mutable.bin' if form.endswith('url') else str(tmp_path/'assets'/'reference.png')
    if form.startswith('nested'):
        inputs['provider_params']={field:value}
    else:
        inputs[field]=value
    with pytest.raises(ProductionGovernanceError,match=field):
        planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    tool=ImageTool()
    with pytest.raises(ProductionGovernanceError):
        tool.execute(inputs)
    assert tool.calls == 0
    assert not (tmp_path/'production_attempts').exists()


def test_strict_canonical_mask_path_is_hashed(tmp_path):
    inputs,_,_=project(tmp_path)
    mask=tmp_path/'assets'/'mask.png'
    mask.write_bytes(b'approved mask')
    inputs['mask_path']=str(mask)
    digest=planned_request_digest(inputs,project_dir=tmp_path)
    mask.write_bytes(b'changed mask')
    assert planned_request_digest(inputs,project_dir=tmp_path) != digest


def test_boolean_audio_controls_are_not_media_inputs(tmp_path):
    inputs,_,_=project(tmp_path)
    inputs['provider_params']={'audio':True,'generate_audio':False}
    inputs['generate_audio']=True
    planned_request_digest(inputs,project_dir=tmp_path)


def test_strict_provider_job_resume_fails_closed_without_new_attempt(tmp_path):
    inputs,scope,_=project(tmp_path)
    # Even an approval digest covering the otherwise-identical request must not
    # authorize polling/resuming an earlier paid provider job as a new attempt.
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    resumed=dict(inputs,resume_job={'request_id':'paid-job','status_url':'https://example.test/status'})
    tool=ImageTool()
    with pytest.raises(ProductionGovernanceError,match='resume provider jobs'):
        tool.execute(resumed)
    assert tool.calls == 0
    assert not (tmp_path/'production_attempts').exists()


@pytest.mark.parametrize('nested',[False,True])
def test_strict_schema_media_native_mask_denied_before_post(tmp_path,monkeypatch,nested):
    """Real SchemaMedia adapter: a strict native local path never reaches the POST body raw."""
    import tools.schema_media as schema_media
    from tools.graphics.atlas_refresh_image import AtlasRefreshImage
    inputs,scope,_=project(tmp_path)
    tool=AtlasRefreshImage()
    scope['provider']=tool.provider
    posts=[]
    monkeypatch.setattr(schema_media,'request_json',lambda method,url,**kw: posts.append((method,url,kw)) or {})
    monkeypatch.setenv(tool.credential,'offline-test-key')
    native=dict(inputs)
    if nested:
        native['provider_params']={'mask':inputs['image_path']}
    else:
        native['mask']=inputs['image_path']
    write_scopes(tmp_path,scope)
    with pytest.raises(ProductionGovernanceError,match='mask'):
        tool.execute(native)
    assert posts == []
    assert not (tmp_path/'production_attempts').exists()


@pytest.mark.parametrize('via_selector',[False,True])
@pytest.mark.parametrize('field',['images','image'])
def test_strict_schema_media_native_image_local_path_never_posted(tmp_path,monkeypatch,via_selector,field):
    """Native ``images``/``image`` are frozen to snapshot paths in strict mode; the
    real SchemaMedia adapter must refuse to send that raw path to the paid route,
    whether called directly or nested under a governed selector."""
    import tools.schema_media as schema_media
    from tools.graphics.atlas_refresh_image import AtlasRefreshImage
    from tools.graphics.image_selector import ImageSelector
    from tools.base_tool import ToolStatus
    inputs,scope,_=project(tmp_path)
    tool=AtlasRefreshImage()
    posts=[]
    monkeypatch.setattr(schema_media,'request_json',lambda method,url,**kw: posts.append((method,url,kw)) or {})
    monkeypatch.setenv(tool.credential,'offline-test-key')
    native={k:v for k,v in inputs.items() if k!='image_path'}
    native[field]=[inputs['image_path']] if field=='images' else inputs['image_path']
    native['model']='gpt-image-2.5-flare'
    runner=tool
    if via_selector:
        monkeypatch.setattr(tool,'get_status',lambda: ToolStatus.AVAILABLE)
        runner=ImageSelector()
        monkeypatch.setattr(runner,'_providers',lambda: [tool])
        native.update(preferred_provider=tool.provider,preferred_tool=tool.name)
    scope['provider']=tool.provider
    scope['requests']['entry']=planned_request_digest(native,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    try:
        result=runner.execute(native)
    except (ProductionGovernanceError,ValueError):
        result=None
    assert result is None or not result.success
    assert posts == [], 'raw filesystem path must never reach the paid POST body'


def test_schema_media_native_local_path_rejected_outside_strict(tmp_path,monkeypatch):
    import tools.schema_media as schema_media
    from tools.graphics.atlas_refresh_image import AtlasRefreshImage
    source=tmp_path/'source.png'
    source.write_bytes(b'x')
    posts=[]
    monkeypatch.setattr(schema_media,'request_json',lambda method,url,**kw: posts.append(kw) or {})
    monkeypatch.setenv('ATLASCLOUD_API_KEY','offline-test-key')
    result=AtlasRefreshImage().execute({'prompt':'p','images':[str(source)],'output_path':str(tmp_path/'o.png')})
    assert not result.success and 'image_path' in result.error
    assert posts == []


@pytest.mark.parametrize('adapter',['schema','fal'])
@pytest.mark.parametrize('field,nested',[('last_image_path',False),('last_frame',False),('end_frame',True)])
def test_new_adapters_reject_unsupported_final_pin_instead_of_dropping(tmp_path,monkeypatch,adapter,field,nested):
    """SchemaMedia/FalMedia cannot pin a final frame; a requested pin must fail
    closed rather than be filtered out while the paid job runs anyway."""
    import tools.schema_media as schema_media
    import tools.fal_media as fal_media
    from tools.video.wan_atlas_video import WanAtlasVideo
    from tools.video.wan_fal_video import WanFalVideo
    start=tmp_path/'start.png'; start.write_bytes(b'x')
    end=tmp_path/'end.png'; end.write_bytes(b'y')
    posts=[]
    monkeypatch.setattr(schema_media,'request_json',lambda *a,**kw: posts.append(kw) or {})
    monkeypatch.setattr(fal_media,'request_json',lambda *a,**kw: posts.append(kw) or {})
    tool=WanAtlasVideo() if adapter=='schema' else WanFalVideo()
    monkeypatch.setenv(tool.credential if adapter=='schema' else 'FAL_KEY','offline-test-key')
    value=str(end)
    inputs={'prompt':'p','image_path':str(start),'output_path':str(tmp_path/'o.mp4')}
    if nested:
        inputs['provider_params']={field:value}
    else:
        inputs[field]=value
    with pytest.raises(ValueError,match='final-frame pin'):
        tool.build_request(inputs)
    result=tool.execute(inputs)
    assert not result.success and 'final-frame pin' in result.error
    assert posts == []


def test_strict_schema_media_last_image_path_not_silently_dropped(tmp_path,monkeypatch):
    """Strict approval may freeze last_image_path, but the adapter must refuse it
    rather than submit an unpinned paid job."""
    import tools.schema_media as schema_media
    from tools.video.wan_atlas_video import WanAtlasVideo
    inputs,scope,_=project(tmp_path)
    (tmp_path/'assets'/'end.png').write_bytes(b'end')
    tool=WanAtlasVideo()
    posts=[]
    monkeypatch.setattr(schema_media,'request_json',lambda *a,**kw: posts.append(kw) or {})
    monkeypatch.setenv(tool.credential,'offline-test-key')
    native=dict(inputs,last_image_path=str(tmp_path/'assets'/'end.png'),duration=5)
    scope['provider']=tool.provider
    scope['requests']['entry']=planned_request_digest(native,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    try:
        result=tool.execute(native)
    except (ProductionGovernanceError,ValueError):
        result=None
    assert result is None or not result.success
    assert posts == [], 'unpinned paid job must never be submitted'


@pytest.mark.parametrize('extra',[{},{'operation':'generate'},{'operation':'resume','resume_job':{'video_id':'paid'}}])
def test_strict_avatar_generation_and_resume_fail_closed(tmp_path,monkeypatch,extra):
    import tools.avatar.heygen_avatar as heygen
    from lib.production_execution import governed_dry_run
    inputs,scope,_=project(tmp_path)
    calls=[]
    monkeypatch.setattr(heygen,'request_json',lambda *a,**kw: calls.append(a) or {})
    monkeypatch.setenv('HEYGEN_API_KEY','offline-test-key')
    tool=heygen.HeyGenAvatar()
    scope['provider']=tool.provider
    write_scopes(tmp_path,scope)
    request=dict(inputs,avatar_id='look',script='hello',**extra)
    with pytest.raises(ProductionGovernanceError,match='avatar'):
        tool.execute(request)
    with pytest.raises(ProductionGovernanceError,match='avatar'):
        governed_dry_run(tool,request)
    assert calls == []
    assert not (tmp_path/'production_attempts').exists()


@pytest.mark.parametrize('operation',['list_looks','inspect_look'])
def test_avatar_catalog_lookups_stay_ungoverned(tmp_path,operation):
    from tools.avatar.heygen_avatar import HeyGenAvatar
    inputs,_,_=project(tmp_path)
    assert preflight(HeyGenAvatar(),dict(inputs,operation=operation,avatar_id='look'))['governed'] is False


def test_legacy_project_avatar_not_blocked(tmp_path):
    from tools.avatar.heygen_avatar import HeyGenAvatar
    (tmp_path/'project.json').write_text(json.dumps({'project_id':'legacy'}))
    assert preflight(HeyGenAvatar(),{'project_dir':str(tmp_path),'avatar_id':'look'})['label'] == 'ungoverned_legacy'


def test_motion_omitted_duration_rejected_before_reservation(tmp_path):
    inputs,scope,_=project(tmp_path,motion=True)
    del inputs['duration']
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    tool=MotionTool()
    with pytest.raises(ProductionGovernanceError,match='duration'):
        tool.execute(inputs)
    assert tool.calls==0 and not (tmp_path/'production_attempts').exists()


def test_exact_multi_shot_repair_batch_accepts_only_named_shots(tmp_path,monkeypatch):
    import lib.production_execution as execution
    inputs,scope,contract=project(tmp_path,motion=True)
    # This test isolates repair scope/allowance. Real contract handoffs are covered
    # by test_first_pass_workflow's selectors + adapters integration.
    monkeypatch.setattr(execution,'validate_shot_contract',lambda *a,**kw:{'eligible':True,'errors':[]})
    monkeypatch.setattr(execution,'_check_motion_inputs',lambda *a:None)
    tool=MotionTool()
    first=tool.execute(inputs)
    other=copy.deepcopy(inputs);other['governance']['shot_id']='payoff';other['output_path']=str(tmp_path/'assets'/'payoff.mp4')
    scope['requests']['payoff']=planned_request_digest(other,project_dir=tmp_path)
    scope['attempts_per_shot']['payoff']=1
    write_scopes(tmp_path,scope)
    second=tool.execute(other)
    scope.update(id='repairs',phase='repair',replaces_attempt_ids=[first.data['production_attempt_id'],second.data['production_attempt_id']],attempts_per_shot={'entry':1,'payoff':1})
    for item in (inputs,other):
        item['governance']['scope_id']='repairs'
        item['output_path']=str(Path(item['output_path']).with_stem('repair-'+item['governance']['shot_id']))
        scope['requests'][item['governance']['shot_id']]=planned_request_digest(item,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    assert tool.execute(inputs).success
    assert tool.execute(other).success
    assert tool.calls==4
    scope['replaces_attempt_ids'].append('unrelated-attempt')
    scope['attempts_per_shot']['entry']=2
    inputs['output_path']=str(tmp_path/'assets'/'unapproved-repair.mp4')
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    with pytest.raises(ProductionGovernanceError,match='existing exact attempts'):
        tool.execute(inputs)
    assert tool.calls==4


def interrupted_grok_image(tmp_path,operation):
    inputs,scope,_=project(tmp_path)
    inputs.update(operation=operation,aspect_ratio='1:1')
    if operation=='image_gen': del inputs['image_path']
    scope['provider']='grok_cli'
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    class Interrupted(ImageTool):
        provider='grok_cli'
        def execute(self,inputs): raise KeyboardInterrupt('interrupted')
    with pytest.raises(KeyboardInterrupt): Interrupted().execute(inputs)
    request=json.loads(attempt(tmp_path).read_text())
    submitted=request['submitted_inputs']
    arguments={'prompt':submitted['prompt'],'aspect_ratio':'1:1'}
    if operation=='image_edit': arguments['image']=[submitted['image_path']]
    receipt={'provider':'grok_cli','native_tool':operation,'submitted_arguments':arguments,
        'session_id':request['cli_session_id'],'submission_evidence':'verified_native_call',
        'input_assets':request['input_assets'],'dispatch_status':'completed'}
    result=ToolResult(success=True,artifacts=[inputs['output_path']],data={'session_id':request['cli_session_id'],'conditioning_receipt':receipt})
    return inputs,scope,request,result


@pytest.mark.parametrize('operation',['image_gen','image_edit'])
def test_receiptless_image_recovery_matches_native_image_shape(tmp_path,operation):
    inputs,_,request,result=interrupted_grok_image(tmp_path,operation)
    Path(inputs['output_path']).write_bytes(b'recovered original image')
    recovered=reconcile_attempt(tmp_path,request['attempt_id'],result,request_sha256=request['request_sha256'])
    assert recovered['status']=='generated'
    assert recovered['preserved_output']['sha256']==file_sha256(inputs['output_path'])


def test_verified_terminal_failure_recovery_consumes_attempt_and_allows_new_scope(tmp_path):
    inputs,scope,request,result=interrupted_grok_image(tmp_path,'image_edit')
    result.success=False;result.artifacts=[];result.error='native terminal failure'
    result.data['dispatch_status']='failed';result.data['conditioning_receipt']['dispatch_status']='failed'
    recovered=reconcile_attempt(tmp_path,request['attempt_id'],result,request_sha256=request['request_sha256'])
    assert recovered['status']=='failed' and recovered['output'] is None
    scope['id']='approved-followup';inputs['governance']['scope_id']=scope['id']
    inputs['output_path']=str(tmp_path/'assets'/'followup.png')
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    tool=ImageTool();tool.provider='grok_cli'
    assert tool.execute(inputs).success
    assert len(list((tmp_path/'production_attempts').glob('*/request.json')))==2


def test_receiptless_non_grok_recovery_requires_qualified_native_binding(tmp_path):
    inputs,_,_=project(tmp_path)
    class Interrupted(ImageTool):
        def execute(self,inputs): raise KeyboardInterrupt('interrupted')
    with pytest.raises(KeyboardInterrupt): Interrupted().execute(inputs)
    request=json.loads(attempt(tmp_path).read_text())
    Path(inputs['output_path']).write_bytes(b'planted output')
    result=ToolResult(success=True,artifacts=[inputs['output_path']],data={'session_id':request['cli_session_id'],
        'conditioning_receipt':{'session_id':request['cli_session_id'],'submission_evidence':'verified_native_call','input_assets':request['input_assets']}})
    with pytest.raises(ProductionGovernanceError,match='native control binding'):
        reconcile_attempt(tmp_path,request['attempt_id'],result,request_sha256=request['request_sha256'])
    assert not (attempt(tmp_path).parent/'reconciliation.json').exists()


def test_partial_journal_write_is_atomic_and_never_overwrites(tmp_path,monkeypatch):
    import lib.production_execution as execution
    target=tmp_path/'result.json'
    original=json.dump
    def partial(value,stream,**kwargs):
        stream.write('{"incomplete":')
        raise OSError('disk interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(json,'dump',partial)
        with pytest.raises(OSError): execution._write_new(target,{'status':'generated'})
    assert not target.exists()
    assert not list(tmp_path.glob('*.tmp'))
    execution._write_new(target,{'status':'uncertain'})
    with pytest.raises(FileExistsError): execution._write_new(target,{'status':'generated'})
    assert json.loads(target.read_text())=={'status':'uncertain'}


def test_output_preservation_failure_keeps_raw_return_and_uncertainty(tmp_path,monkeypatch):
    inputs,_,_=project(tmp_path)
    import shutil
    def fail_preserved(incoming,outgoing):
        outgoing.write(b'partial original')
        raise OSError('snapshot disk error')
    monkeypatch.setattr(shutil,'copyfileobj',fail_preserved)
    tool=ImageTool()
    with pytest.raises(OSError,match='snapshot disk error'): tool.execute(inputs)
    directory=attempt(tmp_path).parent
    raw=json.loads((directory/'raw_result.json').read_text())
    assert raw['success'] and raw['data']['nested']=={'full':['evidence']}
    assert not (directory/'output.mp4').exists()
    assert not list(directory.glob('.output-snapshot-*.tmp'))
    assert json.loads((directory/'result.json').read_text())['status']=='uncertain'
    assert tool.calls==1


def test_truncated_historical_result_is_uncertain_not_dispatchable(tmp_path):
    from lib.production_execution import load_attempt_result
    inputs,scope,_=project(tmp_path)
    class Interrupted(ImageTool):
        def execute(self,inputs): raise KeyboardInterrupt('interrupted')
    with pytest.raises(KeyboardInterrupt): Interrupted().execute(inputs)
    req=json.loads(attempt(tmp_path).read_text())
    target=attempt(tmp_path).parent/'result.json';target.chmod(0o644);target.write_text('{')
    assert load_attempt_result(tmp_path,req['attempt_id'])['status']=='uncertain'
    inputs['output_path']=str(tmp_path/'assets'/'other.mp4')
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path);write_scopes(tmp_path,scope)
    with pytest.raises(ProductionGovernanceError,match='original job is uncertain'): ImageTool().execute(inputs)


def test_different_shots_cannot_reserve_same_absent_output(tmp_path):
    from threading import Event
    inputs,scope,_=project(tmp_path)
    other=copy.deepcopy(inputs);other['governance']['shot_id']='other'
    scope['requests']['other']=planned_request_digest(other,project_dir=tmp_path)
    scope['attempts_per_shot']['other']=1;write_scopes(tmp_path,scope)
    entered,release=Event(),Event()
    class Held(ImageTool):
        def execute(self,inputs):
            self.calls+=1;entered.set();assert release.wait(5)
            return ToolResult(success=False,data={'dispatch_status':'failed'})
    tool=Held()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(tool.execute,inputs)
        assert entered.wait(5)
        second=pool.submit(tool.execute,other)
        try:
            with pytest.raises(ProductionGovernanceError,match='reserved'):
                second.result(timeout=2)
        finally: release.set()
        first.result(timeout=2)
    assert tool.calls==1


def test_unfrozen_static_template_cannot_approve_current_mutated_bytes(tmp_path):
    inputs,scope,_=project(tmp_path)
    scope['requests']['entry']={'inputs':copy.deepcopy(inputs)}
    write_scopes(tmp_path,scope)
    Path(inputs['image_path']).write_bytes(b'changed after user approved template')
    tool=ImageTool()
    with pytest.raises(ProductionGovernanceError,match='frozen static input'):
        tool.execute(inputs)
    assert tool.calls==0 and not (tmp_path/'production_attempts').exists()


@pytest.mark.parametrize('field',['reference_images','reference_videos','reference_audios'])
def test_atlas_supported_plural_aliases_freeze_before_real_media_resolution(tmp_path,monkeypatch,field):
    from tools.video.atlas_video import AtlasVideo
    assert AtlasVideo.input_schema['properties'][field]['items']['type']=='string'
    inputs,scope,_=project(tmp_path)
    source=tmp_path/'assets'/'atlas-reference.dat'
    source.write_bytes(b'approved atlas reference')
    inputs[field]=[str(source)]
    scope['requests']['entry']=planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    source.write_bytes(b'unapproved replacement')
    tool=ImageTool()
    with pytest.raises(ProductionGovernanceError,match='approved request'):
        tool.execute(inputs)
    assert tool.calls==0
    source.write_bytes(b'approved atlas reference')
    assert tool.execute(inputs).success
    snapshot=tool.received[field][0]
    assert snapshot != str(source)
    source.write_bytes(b'mutated after snapshot')
    uploaded=[]
    def upload_media(path,api_key):
        uploaded.append((path,Path(path).read_bytes()))
        return 'https://synthetic.invalid/frozen-upload'
    monkeypatch.setattr('tools.video.atlas_video.atlas_client.upload_media',upload_media)
    resolved=AtlasVideo()._resolve_media(tool.received,'synthetic-offline-key')
    assert (snapshot,b'approved atlas reference') in uploaded
    assert all(path != str(source) for path,_ in uploaded)
    assert resolved[field]==['https://synthetic.invalid/frozen-upload']
    remote=copy.deepcopy(inputs);remote[field]=['https://synthetic.invalid/unfrozen-media']
    with pytest.raises(ProductionGovernanceError,match='local immutable asset'):
        planned_request_digest(remote,project_dir=tmp_path)


def test_strict_dry_run_without_hook_is_unchanged(tmp_path):
    from lib.production_execution import governed_dry_run
    inputs,_,_ = project(tmp_path)
    tool = ImageTool()
    assert not hasattr(tool, 'prepare_offline')
    assert tool.dry_run(inputs) == governed_dry_run(tool, inputs)
    assert 'offline_preparation' not in tool.dry_run(inputs)
    assert tool.calls == 0 and not (tmp_path/'production_attempts').exists()


def test_strict_dry_run_offline_hook_is_pure_and_cannot_override_gate(tmp_path):
    from tools.base_tool import in_offline_preparation
    inputs,_,_ = project(tmp_path)
    seen = []
    class HookTool(ImageTool):
        def prepare_offline(self, inputs, governed):
            seen.append((in_offline_preparation(), governed['reservations']))
            return {'quote': 'quote_required', 'provider_calls': 99}
        def dry_run(self, inputs): raise AssertionError('would call network')
    tool = HookTool()
    result = tool.dry_run(inputs)
    assert seen == [(True, 0)] and not in_offline_preparation()
    assert result['provider_calls'] == 0 and result['reservations'] == 0 and result['paid_submission'] is False
    assert result['offline_preparation'] == {'quote': 'quote_required', 'provider_calls': 99}
    assert tool.calls == 0 and not (tmp_path/'production_attempts').exists()
    inputs['prompt'] = 'Unapproved'
    with pytest.raises(ProductionGovernanceError): tool.dry_run(inputs)
    assert len(seen) == 1


def test_offline_hook_blocks_openart_transport(tmp_path, monkeypatch):
    from tools import _openart_cli as cli
    inputs,_,_ = project(tmp_path)
    monkeypatch.setenv('OPENART_CLI_PATH', '/bin/echo')
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path.parent / (tmp_path.name + '-oa')))
    class HookTool(ImageTool):
        def prepare_offline(self, inputs, governed):
            try: cli.run_readonly(['version'])
            except cli.OpenArtCLIError as exc: return {'blocked': exc.kind}
            return {'blocked': None}
    assert HookTool().dry_run(inputs)['offline_preparation'] == {'blocked': 'offline_only'}

# Synthetic OpenArt component seams; no account authentication or native provider.
def test_openart_legacy_project_never_invokes(tmp_path):
    from tools.video.openart_cli_video import OpenArtCLIVideo
    (tmp_path/'project.json').write_text(json.dumps({'project_id':'synthetic-legacy'}))
    with pytest.raises(ProductionGovernanceError, match='OpenArt requires strict'):
        OpenArtCLIVideo().execute({'project_dir':str(tmp_path),'prompt':'synthetic',
                                 'output_path':str(tmp_path/'output.mp4')})
    assert not (tmp_path/'production_attempts').exists()

@pytest.mark.parametrize('change', ['extra_control','profile'])
def test_openart_nested_frozen_mismatch_zero_launch(tmp_path, monkeypatch, change):
    from lib import openart_jobs as jobs
    from tests.tools.test_openart_cli_video import synthetic_openart_project
    from tools.video.openart_cli_video import OpenArtCLIVideo
    inputs, scope, state = synthetic_openart_project(tmp_path, monkeypatch)
    inputs.update(preferred_provider='openart_cli', allowed_providers=['openart_cli'])
    scope['requests']['entry'] = planned_request_digest(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    class SyntheticSelector(BaseTool):
        name='video_selector'
        provider='selector'
        capability='video_generation'
        tier=ToolTier.GENERATE
        def execute(self, submitted):
            nested = dict(submitted)
            nested.pop('preferred_provider')
            nested.pop('allowed_providers')
            if change == 'extra_control': nested['seed'] = 901
            else:
                from lib.production_execution import _ACTIVE
                _ACTIVE.get()['openart_profile']['account_id_sha256'] = 'x'*64
            return OpenArtCLIVideo().execute(nested)
    with pytest.raises(ProductionGovernanceError, match='OpenArt'):
        SyntheticSelector().execute(inputs)
    assert state['submits'] == 0
    assert len(list((tmp_path/'production_attempts').glob('*/request.json'))) == 1

def test_openart_wrong_original_request_cannot_collect(tmp_path, monkeypatch):
    from tests.tools.test_openart_cli_video import synthetic_openart_project
    from tools.video.openart_cli_video import OpenArtCLIVideo
    from lib.production_execution import collect_openart_attempt
    inputs, _, state = synthetic_openart_project(tmp_path,monkeypatch)
    result = OpenArtCLIVideo().execute(inputs)
    with pytest.raises(ProductionGovernanceError, match='request/attempt'):
        collect_openart_attempt(tmp_path,result.data['production_attempt_id'],request_sha256='bad')
    assert state['collections'] == 0 and state['submits'] == 1
    with pytest.raises(ProductionGovernanceError, match='collect_openart_attempt'):
        reconcile_attempt(tmp_path,result.data['production_attempt_id'],result,
                          request_sha256=result.data['production_request_sha256'])
    assert state['submits'] == 1

def test_openart_empty_reference_intent_does_not_remove_required_boards(tmp_path, monkeypatch):
    from tests.tools.test_openart_cli_video import synthetic_openart_project
    from tools.video.openart_cli_video import OpenArtCLIVideo
    inputs, scope, state = synthetic_openart_project(tmp_path,monkeypatch)
    contract = json.loads((tmp_path/'shot_contract.json').read_text())
    contract['shots'][0]['asset_ids'] = []
    digest = contract_digest(contract)
    contract['project_review']['subject_sha256'] = digest
    for shot in contract['shots']: shot['review']['subject_sha256'] = digest
    (tmp_path/'shot_contract.json').write_text(json.dumps(contract))
    inputs.pop('image_path')
    inputs.update(mode='text2video',operation='text_to_video')
    scope['requests']['entry'] = planned_request_digest(inputs,project_dir=tmp_path)
    scope['approval_plan_sha256'] = approval_plan_digest(contract)
    write_scopes(tmp_path,scope)
    with pytest.raises(ProductionGovernanceError, match='asset_ids'):
        OpenArtCLIVideo().execute(inputs)
    assert state['submits'] == 0

@pytest.mark.parametrize('change',['request','snapshot','account','process'])
def test_openart_component_terminal_failure_binding_rejected(tmp_path,monkeypatch,change):
    """Synthetic component failure proof must match the original frozen attempt."""
    from tests.tools.test_openart_cli_video import synthetic_openart_project
    from tools.video.openart_cli_video import OpenArtCLIVideo
    from lib import openart_jobs as jobs, production_execution as execution
    inputs,_,state=synthetic_openart_project(tmp_path,monkeypatch)
    result=OpenArtCLIVideo().execute(inputs)
    aid=result.data['production_attempt_id']
    binding=copy.deepcopy(state['launch'][aid]['binding'])
    proof={'attempt_id':aid,'binding':binding,'job_id_sha256':'synthetic-job',
           'terminal_failure_sha256':'f'*64,'account_id_sha256':binding['account_id_sha256'],
           'process_state':'exited','billing':'unknown','release_authorized':False}
    if change=='request': binding['request_sha256']='wrong-request'
    elif change=='snapshot': binding['snapshot_sha256']='wrong-snapshot'
    elif change=='account': proof['account_id_sha256']='wrong-account'
    else: proof['process_state']='alive'
    monkeypatch.setattr(jobs,'collect_job',lambda *a,**kw:{'status':'failed_terminal','output':None})
    monkeypatch.setattr(jobs,'reconcile_job',lambda *a:{'state':'failed_terminal',
                        'binding':state['launch'][aid]['binding'],'events_sha256':'d'*64})
    monkeypatch.setattr(jobs,'verify_terminal_failure',lambda *a:proof)
    with pytest.raises(ProductionGovernanceError,match='OpenArt terminal'):
        execution.collect_openart_attempt(tmp_path,aid,request_sha256=result.data['production_request_sha256'])
    assert not (tmp_path/'production_attempts'/aid/'reconciliation.json').exists()
    assert state['submits']==1


def test_grok_analyze_bypasses_project_discovery_and_compatibility_probe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lib import production_execution as execution
    tool = SimpleNamespace(name='synthetic_grok_analyze', provider='grok_cli',
                           capability='video_generation', tier=ToolTier.ANALYZE)
    inputs = {'project_dir': str(tmp_path), 'action': 'inspect', 'read_only': True,
              'governance': {'scope_id': 'unused-diagnostic', 'shot_id': 'entry'}}
    monkeypatch.setattr(execution, 'discover_project', lambda *a: pytest.fail('Analyze discovered production project'))
    monkeypatch.setattr('tools._grok_cli_media.observe_grok_cli_compatibility',
                        lambda *a, **k: pytest.fail('Analyze probed Grok executable'))
    invoked = []
    result = execution.execute_governed(tool, inputs, lambda clean: invoked.append(clean) or 'diagnostic-result')
    assert result == 'diagnostic-result' and invoked == [inputs]
    assert not list(tmp_path.iterdir())


def test_grok_generation_unknown_project_still_fails_without_probe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lib import production_execution as execution
    tool = SimpleNamespace(name='synthetic_grok_motion', provider='grok_cli',
                           capability='video_generation', tier=ToolTier.GENERATE)
    monkeypatch.setattr('tools._grok_cli_media.observe_grok_cli_compatibility',
                        lambda *a, **k: pytest.fail('Unknown project probed Grok executable'))
    with pytest.raises(ProductionGovernanceError, match='missing project.json'):
        execution.execute_governed(tool, {'project_dir': str(tmp_path)},
                                   lambda clean: pytest.fail('Unknown project invoked provider'))
    assert not list(tmp_path.iterdir())


def _qualification_marker(root):
    marker = json.loads((root/'project.json').read_text())
    marker['pipeline_type'] = 'provider-qualification'
    (root/'project.json').write_text(json.dumps(marker))


def test_provider_qualification_pipeline_rejects_non_openart_motion_route(tmp_path):
    inputs, _, _ = project(tmp_path, motion=True)
    _qualification_marker(tmp_path)
    tool = MotionTool()
    with pytest.raises(ProductionGovernanceError, match='provider-qualification'):
        preflight(tool, inputs)
    assert tool.calls == 0 and not (tmp_path/'production_attempts').exists()


def test_non_qualification_pipeline_motion_preflight_unchanged(tmp_path):
    inputs, _, _ = project(tmp_path, motion=True)
    assert preflight(MotionTool(), inputs)['governed'] is True


def test_provider_qualification_hook_runs_for_openart_with_native_and_profile(tmp_path, monkeypatch):
    from tests.tools.test_openart_cli_video import synthetic_openart_project
    from lib import provider_qualification
    from tools.video.openart_cli_video import OpenArtCLIVideo
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    _qualification_marker(tmp_path)
    seen = []
    def gate(root, given, digest, *, native=None, profile=None):
        seen.append((Path(root), digest, native, profile))
        raise provider_qualification.QualificationValidationError('Provider qualification: synthetic refusal')
    monkeypatch.setattr(provider_qualification, 'validate_qualification_stage', gate)
    with pytest.raises(ProductionGovernanceError, match='synthetic refusal'):
        preflight(OpenArtCLIVideo(), inputs)
    assert len(seen) == 1 and seen[0][2] and seen[0][3]
    assert seen[0][1] == planned_request_digest(inputs, project_dir=tmp_path)
    assert state['submits'] == 0


@pytest.mark.parametrize('extra', [
    {'unknown_cost_authorization_id': 'retained'},
    {'unknown_cost_evidence_id': 'a' * 64},
    {'unknown_cost_authorization_id': None, 'unknown_cost_evidence_id': None},
    {'unknown_cost_authorization_id': 'retained', 'unknown_cost_evidence_id': 'a' * 64}])
def test_grok_cannot_carry_openart_unknown_authority(tmp_path, monkeypatch, extra):
    from lib import production_execution as execution
    class GrokMotion(MotionTool):
        provider = 'grok_cli'
    inputs, _, _ = project(tmp_path, motion=True)
    inputs.update(extra)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('cross-route unknown authority reached OpenArt preparation')
    monkeypatch.setattr(execution, '_openart_prepare', forbidden)
    tool = GrokMotion()
    with pytest.raises(ProductionGovernanceError, match='invalid_argument:.*OpenArt route'):
        tool.execute(inputs)
    assert tool.calls == 0
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('authorization', ['../escape', 'unsafe.id', 'with space', '/absolute', ''])
def test_openart_unknown_authorization_requires_safe_artifact_id(tmp_path, monkeypatch, authorization):
    from lib import production_execution as execution
    from tools.video.openart_cli_video import OpenArtCLIVideo
    inputs, _, _ = project(tmp_path, motion=True)
    inputs.update(unknown_cost_authorization_id=authorization, unknown_cost_evidence_id='a' * 64)
    def forbidden(*args, **kwargs):
        raise AssertionError('unsafe unknown authorization reached preparation')
    monkeypatch.setattr(execution, '_openart_prepare', forbidden)
    with pytest.raises(ProductionGovernanceError, match='invalid_argument:'):
        preflight(OpenArtCLIVideo(), inputs)
    assert not (tmp_path / 'production_attempts').exists()


def _reviewed_pin_request(tmp_path):
    inputs, scope, contract = project(tmp_path, motion=True)
    # Distinct native pin bytes and two cast members make role/coverage tests real.
    for asset in contract['assets']:
        if asset['role'] in {'start_frame', 'end_frame'}:
            path = tmp_path / asset['path']
            path.write_bytes(path.read_bytes() + ('<!-- ' + asset['role'] + ' -->').encode())
            asset['sha256'] = file_sha256(path)
            asset['review']['subject_sha256'] = asset['sha256']
            asset['cast_ids'] = ['patient', 'doctor']
    contract['shots'][0]['cast_ids'] = ['patient', 'doctor']
    from tests.lib.test_shot_contract import refresh
    refresh(contract)
    scope['approval_plan_sha256'] = approval_plan_digest(contract)
    (tmp_path / 'shot_contract.json').write_text(json.dumps(contract))
    inputs.update(operation='first_last_frame', first_frame=inputs.pop('image_path'),
                  last_frame=inputs.pop('last_image_path'))
    inputs.pop('reference_image_paths')
    scope['requests']['entry'] = planned_request_digest(inputs, project_dir=tmp_path)
    write_scopes(tmp_path, scope)
    return inputs, scope, contract


def test_reviewed_native_pin_pair_carries_identity_without_auxiliary_refs(tmp_path):
    inputs, _, contract = _reviewed_pin_request(tmp_path)
    tool = MotionTool()
    check = preflight(tool, inputs)
    assert check['governed'] and tool.calls == 0
    result = tool.execute(inputs)
    assert result.success and tool.calls == 1
    frozen = json.loads(attempt(tmp_path).read_text())
    assert 'reference_image_paths' not in frozen['submitted_inputs']
    assert {row['role'] for row in frozen['input_assets']} == {'first_frame', 'last_frame'}
    # Snapshot paths differ from original board paths, but immutable bytes match.
    from lib.production_execution import _check_motion_inputs
    _check_motion_inputs(contract, 'entry', frozen['submitted_inputs'], tmp_path)


@pytest.mark.parametrize('change', ['missing_first', 'missing_last', 'changed_first', 'changed_last',
                                  'unapproved_first', 'unapproved_last', 'wrong_role',
                                  'failed_first_review', 'failed_last_review', 'stale_first_review',
                                  'missing_cast', 'partial_cast', 'wrong_cast'])
def test_native_pin_identity_requires_reviewed_exact_pair_and_start_cast(tmp_path, change):
    inputs, scope, contract = _reviewed_pin_request(tmp_path)
    if change.startswith('missing_') and change != 'missing_cast':
        inputs.pop('first_frame' if change == 'missing_first' else 'last_frame')
    elif change.startswith('changed_'):
        Path(inputs['first_frame' if change == 'changed_first' else 'last_frame']).write_bytes(b'changed board')
    elif change.startswith('unapproved_'):
        path = tmp_path / 'assets/unapproved.svg'; path.write_bytes(b'unapproved board')
        inputs['first_frame' if change == 'unapproved_first' else 'last_frame'] = str(path)
    elif change == 'wrong_role':
        inputs['first_frame'], inputs['last_frame'] = inputs['last_frame'], inputs['first_frame']
    else:
        board = next(a for a in contract['assets'] if a['role'] == (
            'end_frame' if change == 'failed_last_review' else 'start_frame'))
        if change.startswith('failed_'): board['review']['status'] = 'fail'
        elif change == 'stale_first_review': board['review']['subject_sha256'] = '0' * 64
        else:
            board['cast_ids'] = [] if change == 'missing_cast' else ['patient'] if change == 'partial_cast' else ['doctor']
            # Fresh planning approval cannot manufacture cast identity in a board.
            from tests.lib.test_shot_contract import refresh
            refresh(contract)
            scope['approval_plan_sha256'] = approval_plan_digest(contract)
        (tmp_path / 'shot_contract.json').write_text(json.dumps(contract))
    # Rebind this exact negative request so approval digest does not mask its
    # missing pin, missing review, wrong native role or identity-coverage defect.
    scope['requests']['entry'] = planned_request_digest(inputs, project_dir=tmp_path)
    write_scopes(tmp_path, scope)
    tool = MotionTool()
    with pytest.raises(ProductionGovernanceError):
        preflight(tool, inputs)
    assert tool.calls == 0 and not (tmp_path / 'production_attempts').exists()


@pytest.mark.parametrize('operation,guided', [('reference_to_video', False), ('first_last_frame', True)])
def test_native_pins_do_not_replace_guided_or_reference_operation_identity_refs(tmp_path, operation, guided):
    inputs, _, contract = _reviewed_pin_request(tmp_path)
    inputs['operation'] = operation
    if guided:
        contract['shots'][0]['reference_mode'] = 'reference_guided'
    from lib.production_execution import _check_motion_inputs
    with pytest.raises(ProductionGovernanceError, match='identity_reference'):
        _check_motion_inputs(contract, 'entry', inputs, tmp_path)


def test_native_pin_identity_exemption_does_not_drop_other_reference_assets(tmp_path):
    inputs, scope, contract = _reviewed_pin_request(tmp_path)
    path = tmp_path / 'assets/prop.svg'; path.write_bytes(b'reviewed prop')
    from tests.lib.test_shot_contract import refresh
    reference = {'id': 'prop', 'role': 'reference_image', 'path': str(path),
                 'sha256': file_sha256(path), 'cast_ids': []}
    reference['review'] = copy.deepcopy(contract['assets'][0]['review'])
    reference['review']['subject_sha256'] = reference['sha256']
    contract['assets'].append(reference)
    contract['shots'][0]['asset_ids'].append('prop')
    refresh(contract)
    from lib.production_execution import _check_motion_inputs
    with pytest.raises(ProductionGovernanceError, match='reference_image prop'):
        _check_motion_inputs(contract, 'entry', inputs, tmp_path)
