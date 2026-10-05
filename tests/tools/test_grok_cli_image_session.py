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
