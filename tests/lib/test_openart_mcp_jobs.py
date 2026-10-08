"""Durable originals and HTTP-byte proof; isolated synthetic evidence only."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from lib import openart_mcp as mcp, openart_mcp_jobs as jobs, production_execution as execution
from tests.integration.test_openart_mcp_governance import fixture_observations, prepare_project, install_fake_download


def begun(tmp_path):
    inputs,native=prepare_project(tmp_path/'original')
    root=Path(inputs['project_dir'])
    jobs.prepare(root,attempt_id='one',generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root,'one',authority_fn=execution.prepare_openart_mcp_handoff)
    return root,inputs,native


def test_parallel_original_begin_consumes_once(fixture_observations,tmp_path):
    inputs,_=prepare_project(tmp_path/'parallel');root=Path(inputs['project_dir'])
    jobs.prepare(root,attempt_id='one',generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
    def call():
        try:return jobs.begin(root,'one',authority_fn=execution.prepare_openart_mcp_handoff)
        except mcp.OpenArtMCPError:return None
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:call(),range(2)))
    assert sum(r is not None for r in results)==1
    assert jobs.attempt_state(root,'one')['status']=='uncertain'
    with pytest.raises(ValueError):jobs.begin(root,'one',authority_fn=execution.prepare_openart_mcp_handoff)


def test_receive_identical_original_is_idempotent_and_missing_id_uncertain(fixture_observations,tmp_path):
    root,_,_=begun(tmp_path)
    jobs.receive(root,'one',outcome={})
    assert jobs.attempt_state(root,'one')['status']=='uncertain'
    receipt={'historyId':'original','status':'PENDING'}
    jobs.receive(root,'one',outcome=receipt)
    assert jobs.receive(root,'one',outcome=copy.deepcopy(receipt))['history_id']=='original'
    with pytest.raises(ValueError):jobs.receive(root,'one',outcome={'historyId':'foreign'})
    with pytest.raises(ValueError):jobs.record_status(root,'one',result={'historyId':'foreign','status':'COMPLETED'})
    with pytest.raises(ValueError):jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED','capabilityId':'wrong:mode'})


@pytest.mark.parametrize('resources,extra',[
    ([{'id':'v','mediaType':'video','url':'https://fixture.invalid/v.mp4'},{'id':'v2','mediaType':'video','url':'https://fixture.invalid/v2.mp4'}],{}),
    ([{'id':'v','mediaType':'video','url':'https://fixture.invalid/v.mp4'},{'id':'i','mediaType':'image','url':'','status':'FAILED'}],{}),
    ([{'id':'v','mediaType':'video','url':'https://fixture.invalid/v.mp4'}],{'resourceCount':2}),
    ([{'id':'i','mediaType':'image','url':'https://fixture.invalid/i.png'}],{'nextStep':{'tool':'paid_continuation'}}),
])
def test_incomplete_or_sheet_only_original_never_downloads(fixture_observations,tmp_path,monkeypatch,resources,extra):
    root,_,_=begun(tmp_path);jobs.receive(root,'one',outcome={'historyId':'original'})
    jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED','resources':resources,**extra})
    from lib import openart_download
    monkeypatch.setattr(openart_download,'_PinnedHTTPSConnection',lambda *a,**k:pytest.fail('invalid original reached HTTP'))
    with pytest.raises(ValueError):jobs.download_original(root,'one')


def test_collection_requires_original_http_bytes_and_recovers_same_output(fixture_observations,tmp_path,monkeypatch):
    root,inputs,_=begun(tmp_path);jobs.receive(root,'one',outcome={'historyId':'original'})
    jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED','resources':[{'id':'v','mediaType':'video','url':'https://fixture.invalid/original.mp4'}]})
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    arbitrary=real_av_clip(tmp_path/'arbitrary.mp4',seconds=1)
    with pytest.raises(ValueError,match='download_receipt_required'):jobs.collect(root,'one',downloaded_path=arbitrary)
    install_fake_download(monkeypatch,jobs,arbitrary)
    download=jobs.download_original(root,'one')
    # Simulate crash after copy/fsync and before journal finalization.
    Path(inputs['output_path']).write_bytes(Path(download['downloaded_path']).read_bytes())
    result=jobs.collect(root,'one',downloaded_path=download['downloaded_path'])
    assert result['status']=='collected'
    assert jobs.collect(root,'one',downloaded_path=download['downloaded_path'])['status']=='collected'
    assert jobs.provenance_record(root,'one')['download_receipt']['resource_url']=='https://fixture.invalid/original.mp4'
    with pytest.raises(ValueError):jobs.qualify_result(root,'one')


def test_bare_self_hashed_marker_cannot_mint_qualification(fixture_observations,tmp_path):
    candidate=mcp.load_profile('pixverseV6','text2video',require='candidate')
    candidate['source']='real'  # local adversarial marker test, never real provider evidence
    q={'provider':'openart_mcp','candidate_sha256':candidate['profile_sha256'],'history_id':'invented'}
    q['qualification_sha256']=mcp.digest(q)
    jobs._write(jobs._state_root()/'mcp-qualifications'/'pixverseV6__text2video.json',q)
    with pytest.raises(ValueError,match='original transport proof'):jobs.qualified_profile('pixverseV6','text2video',candidate)

@pytest.mark.parametrize('mutation',['story_revision','q_reseal','archive_reseal','output_snapshot','native_control'])
def test_successful_fixture_archive_historical_lookup_and_tamper_rejection(fixture_observations,tmp_path,monkeypatch,mutation):
    from lib import production_provenance
    root,_,native=begun(tmp_path)
    jobs.receive(root,'one',outcome={'historyId':'original'})
    jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED','creditsCharged':1,
        'resources':[{'id':'v','mediaType':'video','url':'https://fixture.invalid/original.mp4'}]})
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    media=real_av_clip(tmp_path/'synthetic.mp4',seconds=1)
    install_fake_download(monkeypatch,jobs,media)
    downloaded=jobs.download_original(root,'one')
    jobs.collect(root,'one',downloaded_path=downloaded['downloaded_path'])
    monkeypatch.setattr(jobs,'_ALLOW_FIXTURE_QUALIFICATION',True)
    monkeypatch.setattr(production_provenance,'_ALLOW_OPENART_FIXTURE_PROVENANCE',True)
    qualified=jobs.qualify_result(root,'one')
    assert qualified['source']=='fixture' and qualified['evidence_kind']=='fixture_only'
    assert qualified['production_ready'] is False
    candidate=mcp.load_profile(native['model'],native['mode'],require='candidate')
    qpath=jobs._state_root()/'mcp-fixture-qualifications'/'pixverseV6__text2video.json'
    q=jobs._read(qpath);archive_path=Path(q['archive_path']);archive=jobs._read(archive_path)
    if mutation=='story_revision':
        marker=json.loads((root/'project.json').read_text());marker['story_revision']='later-unrelated-story'
        (root/'project.json').write_text(json.dumps(marker))
        assert jobs.qualified_profile(native['model'],native['mode'],candidate)['source']=='fixture'
    elif mutation=='q_reseal':
        q['original_frozen_sha256']='a'*64
        q['qualification_sha256']=mcp.digest({k:v for k,v in q.items() if k!='qualification_sha256'})
        jobs._write(qpath,q)
        with pytest.raises(ValueError):jobs.qualified_profile(native['model'],native['mode'],candidate)
    elif mutation=='output_snapshot':
        Path(archive['output_snapshot_path']).write_bytes(b'foreign output')
        with pytest.raises(ValueError):jobs.qualified_profile(native['model'],native['mode'],candidate)
    else:
        if mutation=='native_control':archive['provenance']['native']['body']['params']['duration']=True
        else:archive['evidence']['review']['status']='fail'
        jobs._write(archive_path,archive)
        q['archive_sha256']=mcp.digest(archive)
        q['qualification_sha256']=mcp.digest({k:v for k,v in q.items() if k!='qualification_sha256'})
        jobs._write(qpath,q)
        with pytest.raises(ValueError):jobs.qualified_profile(native['model'],native['mode'],candidate)
    monkeypatch.setattr(jobs,'_ALLOW_FIXTURE_QUALIFICATION',False)
    assert jobs.qualified_profile(native['model'],native['mode'],candidate) is None
    assert not (jobs._state_root()/'mcp-qualifications'/'pixverseV6__text2video.json').exists()
    with pytest.raises(ValueError,match='profile_not_ready'):mcp.load_profile(native['model'],native['mode'],require='qualified')
