"""Image bootstrap reservations use the same original CLI session on every exit."""
import hashlib
import json
from pathlib import Path

import pytest

from tools.graphics.grok_cli_image import GrokCLIImage
from tests.tools.test_grok_cli_media import FakeProcesses, _install_fake, _stream


@pytest.mark.parametrize('failure', ['success','timeout','nonzero','parse','preflight'])
def test_image_reservation_and_conditioning_survive_native_exit(tmp_path, monkeypatch, failure):
    sid = 'caller-reserved-image'
    sessions = tmp_path/'sessions'
    artifact = sessions/sid/'images/output.jpg'
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'synthetic image artifact')
    source = tmp_path/'source.png'; source.write_bytes(b'synthetic source')
    arguments = {'prompt':'Identity fixture', 'aspect_ratio':'1:1', 'image':[str(source)]}
    stream = _stream('image_edit', 'ImageEdit', artifact, raw_input=arguments).replace('"session-1"', json.dumps(sid))
    fake = FakeProcesses(media_stdout='malformed' if failure=='parse' else stream,
        media_returncode=1 if failure=='nonzero' else 0, media_stderr='failed' if failure=='nonzero' else '',
        timeout_media=failure=='timeout', version='grok 1.0.1' if failure=='preflight' else 'grok 1.0.34',
        probe={'streams':[{'codec_type':'video','codec_name':'mjpeg','width':720,'height':720}], 'format':{}})
    _install_fake(monkeypatch, fake)
    tool = GrokCLIImage(grok_path='grok', sessions_root=str(sessions))
    result = tool.execute({'prompt':'Identity fixture', 'operation':'image_edit', 'image_paths':[str(source)],
        'aspect_ratio':'1:1','output_path':str(tmp_path/'identity.jpg'),'allow_unknown_cost':True,'cli_session_id':sid})
    assert result.success == (failure=='success'), result.error
    assert result.data['session_id'] == sid
    if failure!='preflight':
        argv = fake.media_calls[0][0]
        assert argv[argv.index('--session-id')+1] == sid
    receipt = result.data['conditioning_receipt']
    assert receipt['session_id'] == sid
    assert receipt['submitted_arguments'] == arguments
    assert receipt['media_model'] is None
    assert receipt['input_assets'] == [{'role':'reference','index':0,'path':str(source),
        'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]
    assert receipt['submission_evidence'] == ('verified_native_call' if failure=='success' else 'unconfirmed')
    assert len(fake.media_calls) == (0 if failure=='preflight' else 1)


@pytest.mark.parametrize('sid', ['../escape', '/tmp/escape', '', None, 123])
def test_image_rejects_unsafe_caller_identity_before_subprocess(tmp_path, monkeypatch, sid):
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', lambda *a, **k: pytest.fail('must not dispatch'))
    result = GrokCLIImage().execute({'prompt':'fixture', 'output_path':str(tmp_path/'fixture.jpg'),
        'allow_unknown_cost':True, 'cli_session_id':sid})
    assert not result.success
    assert result.data['dispatch_status'] == 'not_dispatched'


def test_strict_image_bootstrap_reserves_before_real_adapter_dispatch(tmp_path, monkeypatch):
    from lib.checkpoint import init_project
    from lib.production_execution import planned_request_digest
    from lib.shot_contract import file_sha256

    root = init_project('image-bootstrap', title='Synthetic image prerequisite', pipeline_type='cinematic',
        pipeline_dir=tmp_path, governance='strict', story_revision='story-1')
    source = root/'assets/images/source.png'; source.write_bytes(b'synthetic bootstrap image')
    sessions = root/'sessions'
    inputs = {'prompt':'Identity fixture','operation':'image_edit','image_paths':[str(source)],
        'output_path':str(root/'assets/images/identity.jpg'),'allow_unknown_cost':True,
        'project_dir':str(root),'governance':{'scope_id':'image-approval','shot_id':'doctor-reference'}}
    evidence = root/'approval.txt'; evidence.write_text('Synthetic user approval for one image bootstrap, no motion authority.')
    scope = {'id':'image-approval','status':'approved','approved_by':'synthetic-fixture',
        'project_id':root.name,'story_revision':'story-1','phase':'image','provider':'grok_cli',
        'evidence':{'path':str(evidence),'sha256':file_sha256(evidence)},
        'requests':{'doctor-reference':planned_request_digest(inputs, project_dir=root)},
        'attempts_per_shot':{'doctor-reference':1}}
    (root/'production_scopes.json').write_text(json.dumps({'version':'1.0','scopes':[scope]}))

    class BootstrapProcesses(FakeProcesses):
        def __call__(self, argv, **kwargs):
            if '--prompt-file' in argv:
                sid = argv[argv.index('--session-id')+1]
                request = json.loads((root/'production_attempts'/sid/'request.json').read_text())
                assert request['cli_session_id'] == sid
                assert request['input_assets'][0]['original_path'] == str(source)
                snapshot = request['submitted_inputs']['image_paths'][0]
                assert 'production_attempts' in snapshot and file_sha256(snapshot)==file_sha256(source)
                native_args = json.loads(Path(argv[argv.index('--prompt-file')+1]).read_text().splitlines()[1])
                artifact = sessions/sid/'images/result.jpg'
                artifact.parent.mkdir(parents=True); artifact.write_bytes(b'synthetic generated image')
                self.media_stdout = _stream('image_edit','ImageEdit',artifact,raw_input=native_args).replace('"session-1"',json.dumps(sid))
            return super().__call__(argv, **kwargs)

    fake = BootstrapProcesses(media_stdout='',version='grok 1.0.34',
        probe={'streams':[{'codec_type':'video','codec_name':'mjpeg','width':720,'height':720}],'format':{}})
    _install_fake(monkeypatch,fake)
    result = GrokCLIImage(grok_path='grok',sessions_root=str(sessions)).execute(inputs)
    assert result.success, result.error
    sid = result.data['production_attempt_id']
    assert result.data['session_id'] == sid
    assert result.data['conditioning_receipt']['session_id'] == sid
    assert len(fake.media_calls) == 1
    assert not (root/'artifacts/shot_contract.json').exists(), 'image prerequisites must be authorable before motion evidence'


def test_canonical_native_and_grok_share_allowance_without_duplicate_counter(tmp_path, monkeypatch):
    """Needs the canonical admission hook under the execution project lock."""
    from lib import production_images as images
    from lib.production_execution import planned_request_digest
    from lib.shot_contract import file_sha256
    from tests.lib.test_production_images import PNG, write

    root = tmp_path/'shared-images'; root.mkdir()
    write(root/'project.json', {'project_id':root.name,'story_revision':'story-1',
        'governance':{'version':'1.0','mode':'strict'}})
    source = root/'assets/images/source.png'; source.parent.mkdir(parents=True); source.write_bytes(PNG)
    story = root/'story.txt'; story.write_text('Synthetic current story.')
    evidence = root/'approval.txt'; evidence.write_text('Synthetic approval: two image calls across native host and Grok CLI only.')
    packet = images.build_preboard_packet(root,story={'path':'story.txt','sha256':file_sha256(story)},
        approval={'path':'approval.txt','sha256':file_sha256(evidence),'approved_by':'synthetic-fixture'},
        references=[],board_slots=[{'id':'board','shot_id':'s1','role':'start_frame','status':'unresolved'}])
    native_args = {'prompt':'Synthetic native image.'}
    native_sha = images.image_request_digest(packet,'board',native_args,output_path='assets/images/native.png')
    common = {'id':'shared','status':'approved','approved_by':'synthetic-fixture','project_id':root.name,
        'story_revision':'story-1','phase':'image','provider':'native_imagegen',
        'evidence':{'path':'approval.txt','sha256':file_sha256(evidence)},'image_allowance':2,
        'allowed_image_routes':['native_imagegen','grok_cli'],'requests':{'board':native_sha},'attempts_per_shot':{'board':1}}
    inputs = {'prompt':'Synthetic fallback','operation':'image_gen','aspect_ratio':'1:1',
        'output_path':str(root/'assets/images/fallback.jpg'),'allow_unknown_cost':True,
        'project_dir':str(root),'governance':{'scope_id':'grok-approved','shot_id':'board'}}
    grok_sha = planned_request_digest(inputs,project_dir=root)
    second_inputs = dict(inputs,output_path=str(root/'assets/images/excess.jpg'))
    second_sha = planned_request_digest(second_inputs,project_dir=root)
    grok_scope = {k:v for k,v in common.items() if k not in {'image_allowance','allowed_image_routes'}}
    grok_scope.update(id='grok-approved',provider='grok_cli',image_allowance_scope_id='shared',
        requests={'board':[grok_sha,second_sha]},attempts_per_shot={'board':2})
    write(root/'production_scopes.json',{'version':'1.0','scopes':[common,grok_scope]})
    native = images.reserve_native_image(root,packet,'board',native_args,output_path='assets/images/native.png',scope_id='shared')
    envelope = images.begin_native_image(root,native['call_id'])
    raw = root/'host-error.json'; write(raw,{'tool_name':'image_gen.imagegen','arguments':envelope['arguments'],
        'result':{'isError':True,'error':'Synthetic terminal native failure.'}})
    images.import_native_image(root,native['call_id'],host_receipt={'path':str(raw),'sha256':file_sha256(raw)})
    sessions = root/'sessions'

    class SharedImageProcesses(FakeProcesses):
        def __call__(self,argv,**kwargs):
            if '--prompt-file' in argv:
                sid = argv[argv.index('--session-id')+1]
                request = json.loads((root/'production_attempts'/sid/'request.json').read_text())
                assert request['cli_session_id'] == sid
                artifact = sessions/sid/'images/result.jpg'
                artifact.parent.mkdir(parents=True); artifact.write_bytes(PNG)
                native = json.loads(Path(argv[argv.index('--prompt-file')+1]).read_text().splitlines()[1])
                self.media_stdout = _stream('image_gen','ImageGen',artifact,raw_input=native).replace('"session-1"',json.dumps(sid))
            return super().__call__(argv,**kwargs)

    fake = SharedImageProcesses(media_stdout='',version='grok 1.0.34',
        probe={'streams':[{'codec_type':'video','codec_name':'mjpeg','width':720,'height':720}],'format':{}})
    _install_fake(monkeypatch,fake)
    tool = GrokCLIImage(grok_path='grok',sessions_root=str(sessions))
    result = tool.execute(inputs)
    assert result.success, result.error
    assert images.image_usage(root)['total'] == 2
    assert len(fake.media_calls) == 1
    with pytest.raises(ValueError,match='image allowance'):
        tool.execute(second_inputs)
    assert images.image_usage(root)['total'] == 2
    assert len(fake.media_calls) == 1
    assert len(list((root/'production_attempts').glob('*/request.json'))) == 1


def test_canonical_usage_releases_only_qualified_grok_predispatch_refusal(tmp_path,monkeypatch):
    from lib.checkpoint import init_project
    from lib.production_execution import planned_request_digest
    from lib.production_images import image_usage
    from lib.shot_contract import file_sha256
    from tests.lib.test_production_images import write
    root = init_project('image-never-submitted',title='Synthetic preflight image refusal',pipeline_type='cinematic',
        pipeline_dir=tmp_path,governance='strict',story_revision='story-1')
    evidence = root/'approval.txt'; evidence.write_text('Synthetic approved image bootstrap.')
    inputs = {'prompt':'Synthetic native image','operation':'image_gen','output_path':str(root/'assets/images/result.jpg'),
        'allow_unknown_cost':True,'project_dir':str(root),'governance':{'scope_id':'approved','shot_id':'board'}}
    scope = {'id':'approved','project_id':root.name,'story_revision':'story-1','phase':'image','provider':'grok_cli',
        'status':'approved','approved_by':'synthetic-fixture','evidence':{'path':str(evidence),'sha256':file_sha256(evidence)},
        'requests':{'board':planned_request_digest(inputs,project_dir=root)},'attempts_per_shot':{'board':1}}
    write(root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    fake = FakeProcesses(media_stdout='',version='grok 1.0.1')
    _install_fake(monkeypatch,fake)
    result = GrokCLIImage(grok_path='grok',sessions_root=str(root/'sessions')).execute(inputs)
    assert not result.success
    assert result.data['dispatch_status'] == 'not_dispatched'
    assert fake.media_calls == []
    usage = image_usage(root)
    assert usage['total'] == 0
    assert usage['excluded_never_submitted'] == [result.data['production_attempt_id']]
    # Tampering with the actual conditioning receipt destroys the proof;
    # a generic not_dispatched label cannot release the ceiling.
    raw_path = root/'production_attempts'/result.data['production_attempt_id']/'raw_result.json'
    raw = json.loads(raw_path.read_text()); raw['data'].pop('conditioning_receipt')
    raw_path.chmod(0o644); write(raw_path,raw)
    assert image_usage(root)['total'] == 1
