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


@pytest.mark.parametrize('change', [None, 'missing_asset', 'wrong_bytes', 'wrong_binding_hash', 'wrong_review', 'duplicate_id'])
def test_capture_evidence_resolves_pathless_reference_binding_by_asset_id(tmp_path, monkeypatch, change):
    import base64
    from lib import production_request as prep
    from lib.shot_contract import contract_digest, file_sha256, review_digest
    root=tmp_path.resolve();artifacts=root/'artifacts';artifacts.mkdir()
    image=root/'identity.png';image.write_bytes(b'SYNTHETIC immutable reviewed identity')
    review={'review_id':'synthetic-only','reviewer':'Synthetic unit fixture','status':'pass'}
    asset={'id':'identity-one','role':'identity_reference','cast_ids':['one'],
           'path':str(image),'sha256':file_sha256(image),'review':review}
    contract={'assets':[asset],'shots':[]}
    reference={key:copy.deepcopy(asset[key]) for key in ('id','role','cast_ids','sha256')}
    reference['review_sha256']=review_digest(review)
    if change=='missing_asset':contract['assets']=[]
    elif change=='duplicate_id':contract['assets'].append(copy.deepcopy(asset))
    elif change=='wrong_bytes':image.write_bytes(b'changed')
    elif change=='wrong_binding_hash':reference['sha256']='f'*64
    elif change=='wrong_review':reference['review_sha256']='f'*64
    packet={'binding':{'references':[reference],'contract_sha256':contract_digest(contract)}}
    compiled={'source_binding':copy.deepcopy(packet['binding'])};preparation={'synthetic':True}
    for name,value in [('shot_contract.json',contract),('compiled_request-one.json',compiled),('preparation_review-one.json',preparation)]:
        (artifacts/name).write_text(json.dumps(value))
    monkeypatch.setattr(prep,'source_packet',lambda *args,**kwargs:copy.deepcopy(packet))
    authority={'shot_id':'one','preparation':{'compiled_sha256':mcp.digest(compiled),'review_sha256':mcp.digest(preparation)},
               'scope':{'evidence':{'path':'creator-approval.txt'}}}
    inputs={'compiled_request_id':'one','preparation_review_id':'one'}
    assert set(reference)=={'id','role','cast_ids','sha256','review_sha256'} and 'path' not in reference
    if change:
        with pytest.raises(mcp.OpenArtMCPError,match='source reference'):
            jobs._capture_evidence(root,inputs,{'input_assets':[]},authority)
        assert not (root/'openart_mcp').exists()
    else:
        captured=jobs._capture_evidence(root,inputs,{'input_assets':[]},authority)
        archived=captured['files']['identity.png']
        assert archived['sha256']==reference['sha256']
        assert base64.b64decode(archived['bytes_base64'])==image.read_bytes()
        assert captured['source_packet']==packet
        assert not (root/'openart_mcp').exists()


@pytest.mark.parametrize('tamper', [None, 'history_id', 'resource_sha256', 'terminal_sha256', 'path', 'sha256', 'bytes', 'unreceipted'])
def test_download_receipt_crash_recovers_without_get(fixture_observations,tmp_path,monkeypatch,tamper):
    root,_,_=begun(tmp_path);jobs.receive(root,'one',outcome={'historyId':'original'})
    jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED',
        'resources':[{'id':'v','mediaType':'video','url':'https://fixture.invalid/original.mp4'}]})
    source=tmp_path/'synthetic.mp4';source.write_bytes(b'synthetic original bytes')
    install_fake_download(monkeypatch,jobs,source)
    original_save=jobs._save
    def crash(root,aid,state):
        if state.get('download_receipt'):raise KeyboardInterrupt()
        return original_save(root,aid,state)
    with monkeypatch.context() as interrupt:
        interrupt.setattr(jobs,'_save',crash)
        with pytest.raises(KeyboardInterrupt):jobs.download_original(root,'one')
    receipt_path=jobs._path(root,'one').parent/'download-receipt.json'
    if tamper=='unreceipted':receipt_path.unlink()
    elif tamper:
        receipt=jobs._read(receipt_path);receipt[tamper]=0 if tamper=='bytes' else 'foreign'
        jobs._write(receipt_path,receipt)
    from lib import openart_download
    monkeypatch.setattr(openart_download,'collect_output',lambda *a,**k:pytest.fail('recovery must never GET'))
    if tamper:
        with pytest.raises(ValueError):jobs.download_original(root,'one')
        assert not jobs._load(root,'one').get('download_receipt')
    else:
        result=jobs.download_original(root,'one')
        assert jobs.download_original(root,'one')==result
        assert jobs._load(root,'one')['download_receipt']['sha256']==result['sha256']


def test_interrupted_original_receive_replays_retained_exact_reply(fixture_observations,tmp_path,monkeypatch):
    root,_,_=begun(tmp_path); original_save=jobs._save
    reply={'historyId':'original-host-history','status':'PENDING'}
    def interrupt(root,aid,state):
        if state.get('receipt'):raise KeyboardInterrupt()
        return original_save(root,aid,state)
    with monkeypatch.context() as crash:
        crash.setattr(jobs,'_save',interrupt)
        with pytest.raises(KeyboardInterrupt):jobs.receive(root,'one',outcome=reply)
    before={str(p):p.read_bytes() for p in (root/'openart_mcp').rglob('*') if p.is_file()}
    guide=jobs.resume_original(root,'one')
    assert guide['next_action']['action']=='receive_original'
    assert guide['next_action']['outcome']==reply
    assert {str(p):p.read_bytes() for p in (root/'openart_mcp').rglob('*') if p.is_file()}==before
    digest=guide['request_sha256']
    with pytest.raises(ValueError):jobs.resume_original(root,'one',outcome={'historyId':'foreign'},request_sha256=digest)
    recovered=jobs.resume_original(root,'one',outcome=guide['next_action']['outcome'],request_sha256=digest)
    assert recovered['history_id']=='original-host-history'
    assert jobs.resume_original(root,'one',outcome=reply,request_sha256=digest)==recovered
    assert jobs.poll_args(root,'one')['arguments']=={'historyId':'original-host-history'}
    assert len(jobs.list_attempts(root))==1


def test_missing_original_host_reply_blocks_without_writes_or_retry(fixture_observations,tmp_path):
    root,_,_=begun(tmp_path)
    statepath=jobs._path(root,'one');before=statepath.read_bytes()
    guide=jobs.resume_original(root,'one')
    assert guide['next_action']['action']=='blocked_original_host_reply_unavailable'
    assert guide['provider_calls']==guide['reservations']==0
    assert statepath.read_bytes()==before
    with pytest.raises(ValueError):jobs.poll_args(root,'one')
    with pytest.raises(ValueError):jobs.begin(root,'one',authority_fn=execution.prepare_openart_mcp_handoff)


def test_prepared_original_resume_returns_begin_after_exact_revalidation(fixture_observations,tmp_path):
    inputs,_=prepare_project(tmp_path/'prepared');root=Path(inputs['project_dir'])
    jobs.prepare(root,attempt_id='one',generation_inputs=inputs,authority_fn=execution.prepare_openart_mcp_handoff)
    before=jobs._path(root,'one').read_bytes();guide=jobs.resume_original(root,'one')
    assert guide['next_action']['action']=='begin_original'
    assert jobs._path(root,'one').read_bytes()==before
    envelope=jobs.begin(root,'one',authority_fn=execution.prepare_openart_mcp_handoff)
    assert envelope['arguments']==jobs.frozen_request(root,'one')['native']['body']


@pytest.mark.parametrize('tamper',[None,'bytes','resource','terminal'])
def test_original_publish_before_return_recovers_receipt_without_second_get(fixture_observations,tmp_path,monkeypatch,tamper):
    from lib import openart_download
    root,inputs,_=begun(tmp_path);jobs.receive(root,'one',outcome={'historyId':'original'})
    jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED',
        'resources':[{'id':'v','mediaType':'video','url':'https://fixture.invalid/original.mp4'}]})
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    source=real_av_clip(tmp_path/'synthetic.mp4',seconds=1)
    install_fake_download(monkeypatch,jobs,source)
    original_collect=openart_download.collect_output
    def publish_then_interrupt(*args,**kwargs):
        original_collect(*args,**kwargs)
        raise KeyboardInterrupt()
    with monkeypatch.context() as crash:
        crash.setattr(openart_download,'collect_output',publish_then_interrupt)
        with pytest.raises(KeyboardInterrupt):jobs.download_original(root,'one')
    state=jobs._load(root,'one');directory=jobs._path(root,'one').parent
    assert not state.get('download_receipt') and (directory/'download-original.mp4').is_file()
    assert (directory/'download-receipt.json').is_file()
    if tamper=='bytes':(directory/'download-original.mp4').write_bytes(b'foreign')
    elif tamper:
        state['status_observations'][-1]['resources'][0]['id']='foreign' if tamper=='resource' else 'v'
        if tamper=='terminal':state['status_observations'][-1]['creditsCharged']=999
        jobs._save(root,'one',state)
    monkeypatch.setattr(openart_download,'collect_output',lambda *a,**k:pytest.fail('original recovery must never download twice'))
    if tamper:
        with pytest.raises(ValueError):jobs.download_original(root,'one')
    else:
        result=jobs.download_original(root,'one')
        assert jobs.collect(root,'one',downloaded_path=result['downloaded_path'])['status']=='collected'
        proof=jobs.provenance_record(root,'one')
        assert proof['download_receipt']['history_id']=='original'
        assert Path(inputs['output_path']).read_bytes()==source.read_bytes()
    assert len(jobs.list_attempts(root))==1


@pytest.mark.parametrize('tamper',[None,'staged_bytes','staged_path'])
def test_original_receipt_before_publish_resumes_verified_staging_without_get(fixture_observations,tmp_path,monkeypatch,tamper):
    from lib import openart_download
    root,_,_=begun(tmp_path);jobs.receive(root,'one',outcome={'historyId':'original'})
    jobs.record_status(root,'one',result={'historyId':'original','status':'COMPLETED',
        'resources':[{'id':'v','mediaType':'video','url':'https://fixture.invalid/original.mp4'}]})
    source=tmp_path/'synthetic.mp4';source.write_bytes(b'synthetic verified original stream')
    install_fake_download(monkeypatch,jobs,source)
    original_collect=openart_download.collect_output
    def interrupt_before_link(*args,**kwargs):
        callback=kwargs['before_publish']
        def retain_then_interrupt(metadata):
            callback(metadata)
            raise KeyboardInterrupt()
        return original_collect(*args,**dict(kwargs,before_publish=retain_then_interrupt))
    with monkeypatch.context() as crash:
        crash.setattr(openart_download,'collect_output',interrupt_before_link)
        with pytest.raises(KeyboardInterrupt):jobs.download_original(root,'one')
    directory=jobs._path(root,'one').parent;receiptpath=directory/'download-receipt.json'
    receipt=jobs._read(receiptpath);staged=Path(receipt['publication_source'])
    assert staged.is_file() and not (directory/'download-original.mp4').exists()
    assert staged.read_bytes()==source.read_bytes()
    if tamper=='staged_bytes':staged.write_bytes(b'foreign')
    if tamper=='staged_path':
        foreign=directory/'arbitrary.mp4';foreign.write_bytes(source.read_bytes())
        receipt['publication_source']=str(foreign);jobs._write(receiptpath,receipt)
    monkeypatch.setattr(openart_download,'collect_output',lambda *a,**k:pytest.fail('staging recovery must never GET'))
    if tamper:
        with pytest.raises(ValueError):jobs.download_original(root,'one')
        assert not (directory/'download-original.mp4').exists()
    else:
        result=jobs.download_original(root,'one')
        assert Path(result['downloaded_path']).read_bytes()==source.read_bytes()
        assert jobs.download_original(root,'one')==result
    assert len(jobs.list_attempts(root))==1
