"""Actual registered local renderer/dispatcher with offline mocked CLI, real sampler."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.production_execution import (ProductionGovernanceError, planned_request_template,
    approval_plan_digest, sample_local_render_outgoing, record_selection)
from lib.production_provenance import validate_attempt_provenance
from lib.shot_contract import file_sha256
from tests.lib.test_production_execution import project, write_scopes
from tools.video.hyperframes_compose import HyperFramesCompose


def save(path, value):
    if path.exists(): path.chmod(0o644)
    path.write_text(json.dumps(value))


@pytest.fixture
def local(tmp_path, monkeypatch):
    _, scope, contract = project(tmp_path, motion=True)
    workspace = tmp_path / 'authored'
    workspace.mkdir()
    for asset in contract['assets']:
        source = tmp_path / asset['path']
        (workspace / source.name).write_bytes(source.read_bytes())
    (workspace / 'index.html').write_text('<html><img src="start.svg"><img src="patient.svg"><img src="end.svg"></html>')
    inputs = {'project_dir':str(tmp_path),'governance':{'scope_id':'batch','shot_id':'entry'},
        'operation':'render_existing','workspace_path':str(workspace),
        'output_path':str(tmp_path/'assets/local.mp4'),'duration':8,'fps':24,
        'quality':'high','strict_check':True}
    scope.update(provider='hyperframes',phase='local_render',attempts_per_shot={'entry':1})
    scope['requests']['entry'] = planned_request_template(inputs,project_dir=tmp_path)
    write_scopes(tmp_path,scope)
    tool = HyperFramesCompose()
    provisioned = tmp_path / 'tooling' / 'hyperframes'
    provisioned.parent.mkdir()
    provisioned.write_text('SYNTHETIC provisioned executable; actual CLI transport mocked')
    monkeypatch.setattr(HyperFramesCompose,'_cli_command',classmethod(lambda cls:(str(provisioned),)))
    calls = []
    monkeypatch.setattr(tool,'_runtime_check',lambda:{'runtime_available':True})
    def run(args, **kwargs):
        calls.append((args,kwargs))
        if args[0] == 'render':
            out = args[args.index('--output')+1]
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=32x32:r=24:d=8',
                            '-c:v','libx264','-pix_fmt','yuv420p',out],check=True)
        return SimpleNamespace(returncode=0,stdout='0.8.101' if args[0]=='--version' else '{}',stderr='',args=[*tool._cli_command(),*args])
    monkeypatch.setattr(tool,'_run_hf',run)
    return tmp_path,inputs,scope,tool,calls


def rendered(local):
    root,inputs,scope,tool,calls = local
    result = tool.execute(inputs)
    assert result.success, result.error
    aid = result.data['production_attempt_id']
    directory = root/'production_attempts'/aid
    output = json.loads((directory/'result.json').read_text())['output']
    return aid,directory,output


def check(local, aid, output):
    return validate_attempt_provenance(local[0],aid,shot_id='entry',story_revision='story-1',expected_output=output)


def test_real_local_renderer_dispatch_and_actual_sampler(local):
    aid,directory,output = rendered(local)
    valid = check(local,aid,output)
    assert valid['request']['media_kind'] == 'local_render'
    assert valid['request']['tool_name'] == 'hyperframes_compose'
    assert valid['request']['submitted_inputs']['workspace_path'] != local[1]['workspace_path']
    assert [args[0] for args,_ in local[4]] == ['--version','check','render']
    frame = sample_local_render_outgoing(local[0],aid)
    assert Path(frame['path']).is_file()
    assert check(local,aid,output)['local_render_outgoing'] == frame


@pytest.mark.parametrize('changed',['index.html','patient.svg','extra.js'])
def test_source_changed_after_scope_freeze_never_dispatches(local,changed):
    (Path(local[1]['workspace_path'])/changed).write_text('changed authored bytes')
    with pytest.raises(ProductionGovernanceError): local[3].execute(local[1])
    assert local[4] == []


@pytest.mark.parametrize('changed',['index.html','patient.svg','output','receipt','raw','provider','tool','sampler'])
def test_mutation_or_forgery_cannot_select(local,changed):
    aid,directory,output = rendered(local)
    result = json.loads((directory/'result.json').read_text())
    request = json.loads((directory/'request.json').read_text())
    if changed in {'index.html','patient.svg'}:
        path=Path(request['submitted_inputs']['workspace_path'])/changed
        path.chmod(0o644);path.write_bytes(b'changed')
    elif changed == 'output': Path(output['path']).write_bytes(b'changed')
    elif changed == 'raw': (directory/'raw_result.json').unlink()
    elif changed in {'provider','tool'}:
        if changed == 'tool': request['tool_name']='video_compose'
        else: request['scope']['provider']='grok_cli'
        save(directory/'request.json',request)
    elif changed == 'sampler':
        sample_local_render_outgoing(local[0],aid)
        sampled=json.loads((directory/'outgoing_sampling.json').read_text())
        sampled['submitted_inputs']['timestamps']=[0]
        save(directory/'outgoing_sampling.json',sampled)
    else:
        result['result']['data']['local_render_receipt']['tool']='grok_cli_video'
        save(directory/'result.json',result)
    with pytest.raises(ProductionGovernanceError): check(local,aid,output)


@pytest.mark.parametrize('controls',[
    {'operation':'render'}, {'strict_check':False}, {'skip_contrast':True},
    {'duration':7}, {'arbitrary_recipe':'injected'}])
def test_unsupported_render_control_rejected_even_when_exactly_scoped(local,controls):
    local[1].update(controls)
    local[2]['requests']['entry']=planned_request_template(local[1],project_dir=local[0])
    write_scopes(local[0],local[2])
    with pytest.raises(ProductionGovernanceError):local[3].execute(local[1])
    assert local[4] == []


@pytest.mark.parametrize('html',[
    '<script src="https://cdn.example/gsap.js"></script>',
    '<img src="../assets/start.svg">','<script>fetch("asset.png")</script>',
    '<style>body{background:url(/absolute.png)}</style>'])
def test_unretained_dependency_rejected_before_dispatch(local,html):
    (Path(local[1]['workspace_path'])/'index.html').write_text(html)
    local[2]['requests']['entry']=planned_request_template(local[1],project_dir=local[0])
    write_scopes(local[0],local[2])
    with pytest.raises(ProductionGovernanceError):local[3].execute(local[1])
    assert local[4] == []


def test_dependency_mutated_during_render_is_not_generated(local,monkeypatch):
    original=local[3]._run_hf
    def run(args,**kwargs):
        result=original(args,**kwargs)
        if args[0]=='render':
            path=kwargs['cwd']/'patient.svg';path.chmod(0o644);path.write_bytes(b'mutated')
        return result
    monkeypatch.setattr(local[3],'_run_hf',run)
    result=local[3].execute(local[1])
    assert not result.success
    aid=result.data['production_attempt_id']
    state=json.loads((local[0]/'production_attempts'/aid/'result.json').read_text())
    assert state['status'] != 'generated'


def reviewed_selection(local, aid, output, frame):
    from lib.shot_contract import selection_digest, UPSTREAM_PREDICATES
    value={'attempt_id':aid,'output':output,'outgoing_frame':frame}
    value['review']={'review_id':'offline-local-review','reviewer':'synthetic fixture author',
        'story_revision':'story-1','subject_sha256':selection_digest(value),'status':'pass',
        'predicates':[{'name':name,'status':'pass','evidence':'SYNTHETIC offline declared review'}
                      for name in sorted(UPSTREAM_PREDICATES)]}
    return value


def test_selection_requires_actual_sampler_and_passes_with_bound_frame(local):
    aid,_,output=rendered(local)
    other=local[0]/'invented.png';other.write_bytes(b'unrelated image')
    candidate=reviewed_selection(local,aid,output,{'path':str(other),'sha256':file_sha256(other)})
    with pytest.raises(ProductionGovernanceError,match='actual outgoing sampling'):
        record_selection(local[0],'entry',candidate)
    frame=sample_local_render_outgoing(local[0],aid)
    with pytest.raises(ProductionGovernanceError,match='actual local render sampling'):
        record_selection(local[0],'entry',candidate)
    record_selection(local[0],'entry',reviewed_selection(local,aid,output,frame))


@pytest.mark.parametrize('status',['unknown','fail'])
def test_local_visual_uncertainty_remains_blocking(local,status):
    aid,_,output=rendered(local);frame=sample_local_render_outgoing(local[0],aid)
    candidate=reviewed_selection(local,aid,output,frame)
    next(p for p in candidate['review']['predicates'] if p['name']=='completed_action')['status']=status
    with pytest.raises(ProductionGovernanceError):record_selection(local[0],'entry',candidate)


def test_local_draft_audio_only_defers_unknown_speaker(local):
    from lib.shot_contract import draft_audio_policy_digest
    root=local[0]; marker=json.loads((root/'project.json').read_text())
    approval=root/'draft-approval.txt';approval.write_text('SYNTHETIC explicit audio unavailable draft approval')
    marker['governance']['draft_review']={'version':'1.0','mode':'audio_unavailable_draft',
        'project_id':marker['project_id'],'story_revision':marker['story_revision'],
        'evidence':{'path':str(approval),'sha256':file_sha256(approval)}}
    save(root/'project.json',marker)
    aid,_,output=rendered(local);frame=sample_local_render_outgoing(root,aid)
    candidate=reviewed_selection(local,aid,output,frame)
    candidate['review'].update(status='provisional',draft_policy_sha256=draft_audio_policy_digest(root))
    speaker=next(p for p in candidate['review']['predicates'] if p['name']=='speaker_source')
    speaker['status']='unknown'
    record_selection(root,'entry',candidate)
    speaker['status']='fail'
    with pytest.raises(ProductionGovernanceError):record_selection(root,'entry',candidate)


def test_actual_render_duration_mismatch_rejected(local,monkeypatch):
    original=local[3]._run_hf
    def run(args,**kwargs):
        result=original(args,**kwargs)
        if args[0]=='render':
            out=args[args.index('--output')+1]
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=s=32x32:r=24:d=1',
                            '-c:v','libx264','-pix_fmt','yuv420p',out],check=True)
        return result
    monkeypatch.setattr(local[3],'_run_hf',run)
    aid,_,output=rendered(local)
    with pytest.raises(ProductionGovernanceError,match='actual render duration'):
        check(local,aid,output)


@pytest.mark.parametrize('change',['scope_contract','contract_review','missing_shot_asset','scope_version','symlink'])
def test_local_contract_and_scope_guards_stay_strict(local,change):
    root,inputs,scope,tool,calls=local
    if change=='scope_contract':
        scope['approval_plan_sha256']='0'*64;write_scopes(root,scope)
    elif change=='scope_version':
        save(root/'production_scopes.json',{'version':'unsupported','scopes':[scope]})
    elif change=='contract_review':
        contract=json.loads((root/'shot_contract.json').read_text())
        contract['shots'][0]['review']['status']='fail';save(root/'shot_contract.json',contract)
    elif change=='missing_shot_asset':
        (Path(inputs['workspace_path'])/'patient.svg').unlink()
        scope['requests']['entry']=planned_request_template(inputs,project_dir=root);write_scopes(root,scope)
    else:
        (Path(inputs['workspace_path'])/'alias.svg').symlink_to(root/'assets/patient.svg')
    with pytest.raises(ProductionGovernanceError):tool.execute(inputs)
    assert calls == []


@pytest.mark.parametrize('prefix', [['arbitrary-local-import'], ['npx','--yes','hyperframes']])
def test_forged_consistent_raw_non_hyperframes_invocation_rejected(local,prefix):
    aid,directory,output=rendered(local)
    value=json.loads((directory/'result.json').read_text())
    receipt=value['result']['data']['local_render_receipt']
    receipt['cli_command']=[*prefix,*receipt['render_argv']]
    save(directory/'result.json',value)
    save(directory/'raw_result.json',value['result'])
    with pytest.raises(ProductionGovernanceError,match='unsupported local render executable'):
        check(local,aid,output)


def test_actual_submitted_source_snapshot_survives_later_original_edit(local):
    aid,_,output=rendered(local)
    (Path(local[1]['workspace_path'])/'index.html').write_text('later unsubmitted draft')
    assert check(local,aid,output)['result']['status']=='generated'


@pytest.mark.parametrize('html',[
    '<img src=https://example.com/changed.png>',
    '<script src=//example.com/changed.js></script>',
    '<img srcset="https://example.com/changed.png 1x">',
    '<picture><source srcset="patient.svg 1x, https://example.com/b.png 2x"></picture>',
    '<link imagesrcset="//example.com/a.png 1x">',
    '<img src="https&#58;//example.com/a.png">',
    '<img src="https&colon;//example.com/a.png">',
    '<video poster=//example.com/poster.png></video>',
    '<object data=https://example.com/object.svg></object>',
    '<iframe srcdoc="&lt;img src=https://example.com/a.png&gt;"></iframe>',
    '<base href=https://example.com/>',
    '<meta http-equiv=refresh content="1;url=https://example.com/">',
    '<svg><image xlink:href="https://example.com/a.png"/></svg>',
    '<div style="background:url(https://example.com/a.png)"></div>',
    r'<style>body{background:u\72l(\68 ttps://example.com/a.png)}</style>',
    '<style>@import "https://example.com/style.css";</style>',
    '<style>body{background:image-set("https://example.com/a.png" 1x)}</style>',
    '<svg><set attributeName=href to=https://example.com/a.png /></svg>',
    '<script type="module">import "https://example.com/external.js";</script>',
    '<svg><rect filter="url(https://example.com/filters.svg#blur)" /></svg>',
    '<svg><rect fill="url(//example.com/pattern.svg#fill)" /></svg>',
    '<svg><rect clip-path="url(https://example.com/clip.svg#clip)" /></svg>',
])
def test_all_static_html_resource_forms_are_closed(local,html):
    (Path(local[1]['workspace_path'])/'index.html').write_text(html)
    local[2]['requests']['entry']=planned_request_template(local[1],project_dir=local[0])
    write_scopes(local[0],local[2])
    with pytest.raises(ProductionGovernanceError):local[3].execute(local[1])
    assert local[4] == []


def test_unquoted_and_srcset_local_dependencies_are_allowed(local):
    workspace=Path(local[1]['workspace_path'])
    (workspace/'index.html').write_text('<img src=start.svg srcset="patient.svg 1x, end.svg 2x">')
    local[2]['requests']['entry']=planned_request_template(local[1],project_dir=local[0])
    write_scopes(local[0],local[2])
    aid,_,output=rendered(local)
    assert check(local,aid,output)['result']['status']=='generated'


def test_known_unavailable_runtime_is_failed_and_next_approved_scope_runs(local,monkeypatch):
    monkeypatch.setattr(local[3],'_runtime_check',lambda:{'runtime_available':False,'reasons':['absent']})
    result=local[3].execute(local[1]);assert not result.success
    aid=result.data['production_attempt_id']
    old_path=local[0]/'production_attempts'/aid/'result.json'
    original=old_path.read_bytes()
    assert json.loads(original)['status']=='failed' and local[4]==[]
    monkeypatch.setattr(local[3],'_runtime_check',lambda:{'runtime_available':True})
    scope=copy.deepcopy(local[2]);scope['id']='new-authorized-local-batch'
    local[1]['governance']['scope_id']=scope['id']
    local[1]['output_path']=str(local[0]/'assets/local-next-approved.mp4')
    scope['requests']['entry']=planned_request_template(local[1],project_dir=local[0])
    save(local[0]/'production_scopes.json',{'version':'1.0','scopes':[local[2],scope]})
    aid,_,output=rendered(local)
    assert check(local,aid,output)['result']['status']=='generated'
    assert old_path.read_bytes()==original


@pytest.mark.parametrize('exit_code, expected',[(1,'failed'),(124,'uncertain')])
def test_authoritative_failed_render_and_unknown_timeout_remain_distinct(local,monkeypatch,exit_code,expected):
    original=local[3]._run_hf
    def run(args,**kwargs):
        if args[0]=='render':
            return SimpleNamespace(returncode=exit_code,stdout='',stderr='synthetic rejected or timeout',
                                   args=[*local[3]._cli_command(),*args])
        return original(args,**kwargs)
    monkeypatch.setattr(local[3],'_run_hf',run)
    result=local[3].execute(local[1]);assert not result.success
    aid=result.data['production_attempt_id']
    state=json.loads((local[0]/'production_attempts'/aid/'result.json').read_text())
    assert state['status']==expected


def test_strict_local_renderer_never_installs_npx_fallback(local,monkeypatch):
    monkeypatch.setattr(HyperFramesCompose,'_cli_command',classmethod(lambda cls:('npx','--yes','hyperframes')))
    with pytest.raises(ProductionGovernanceError,match='npx install is not authorized'):
        local[3].execute(local[1])
    assert local[4]==[]
    assert not list((local[0]/'production_attempts').glob('*/request.json'))


def test_inline_module_and_svg_fragment_local_resources_are_allowed(local):
    workspace=Path(local[1]['workspace_path'])
    (workspace/'logic.js').write_text('export const duration = 8;')
    (workspace/'index.html').write_text('<script type="module">import "./logic.js";</script>'
        '<svg><defs><filter id="blur"/></defs><rect filter="url(#blur)" fill="url(patient.svg#paint)"/></svg>')
    local[2]['requests']['entry']=planned_request_template(local[1],project_dir=local[0])
    write_scopes(local[0],local[2])
    aid,_,output=rendered(local)
    assert check(local,aid,output)['result']['status']=='generated'
