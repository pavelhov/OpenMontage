"""Synthetic v2 original recovery, multiple outputs and durable publication."""
import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from lib import production_autonomy as autonomy, production_execution as execution
from lib import spicy_catalog as catalog, spicy_jobs as jobs, spicy_lifecycle as lifecycle

MODEL='synthetic/video/text-to-video'
HOST='fixture-account.r2.cloudflarestorage.com'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('No actual network'))
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:pytest.fail('No actual DNS'))


@pytest.fixture
def original(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    aid=str(uuid.uuid4()); directory=root/'production_attempts'/aid;directory.mkdir(parents=True)
    schema={'type':'object','properties':{'prompt':{'type':'string'},'duration_seconds':{'type':'integer'}},'required':['prompt','duration_seconds']}
    payload={'prompt':'SYNTHETIC fixture','duration_seconds':5}
    native={'provider':'spicy','transport':jobs.TRANSPORT_V2,'model':MODEL,'mode':'text_to_video','task':'text-to-video',
        'input':payload,'input_schema':schema,'schema_sha256':catalog.digest(schema),'sources':[],
        'payload_state':'ready','request_sha256':catalog.digest({'model':MODEL,'input':payload})}
    binding={'credential_ref':'env:SPICY_V2_LIFECYCLE_OFFLINE_KEY','credential_digest':'a'*64}
    quote={'quote_id':'spq-synthetic','provider_quote_id':'synthetic-provider-quote','currency':'USD',
        'native_request_sha256':native['request_sha256'],'model':MODEL,'account_label':'offline-fixture',
        'credential_digest':binding['credential_digest'],'estimated_cost_usd':'0.10','max_charge_usd':'0.15'}
    authority=dict(binding,account_label='offline-fixture',qualified_storage_hosts=[HOST],quote=quote)
    inputs={'project_dir':str(root),'cli_session_id':aid,'credential_ref':binding['credential_ref'],
        'account_label':'offline-fixture','spicy_quote_id':quote['quote_id'],'input_assets':[],
        'native_input':copy.deepcopy(payload),'output_path':str(root/'assets/video/original.mp4')}
    request={'attempt_id':aid,'request_sha256':'b'*64,'scope':{'provider':'spicy'},'submitted_inputs':inputs,
        'spicy_native':native,'spicy_authority':authority,'input_assets':[]}
    catalog.atomic_write_json(directory/'request.json',request)
    class HTTP:
        qualified_storage_hosts={HOST}
        def __init__(self):
            self.binding=binding;self.calls=[];self.lose=True
            self.task={'taskId':'synthetic-original','model':MODEL,'state':'running','settled':False,'cost':None}
        def get_task(self, task_id):
            return self.request_json('GET', '/jobs/recordInfo', query={'taskId': task_id})

        def create_task(self, *, model, input_data, idempotency_key, quote_id, expected_cost):
            body = {'model': model, 'input': input_data, 'quoteId': quote_id, 'expectedCost': expected_cost}
            return self.request_json('POST', '/jobs/createTask', body, headers={'Idempotency-Key': idempotency_key})

        def create_download_url(self, task_id, key=None):
            return self.request_json('POST', '/common/download-url', {'taskId': task_id, 'key': key})

        def request_json(self,method,path,body=None,*,query=None,headers=None):
            self.calls.append((method,path,copy.deepcopy(body),copy.deepcopy(headers)))
            if path=='/jobs/createTask':
                if self.lose:
                    self.lose=False
                    raise jobs.SpicyTransportError('SYNTHETIC loss',kind='transport_uncertain')
                return {'data':{'taskId':'synthetic-original','model':MODEL,'state':'queued','estimatedCost':'0.10'}}
            if path=='/jobs/recordInfo':return {'data':copy.deepcopy(self.task)}
            if path=='/common/download-url':
                return {'data':{'key':body['key'],'url':'https://'+HOST+'/'+body['key']+'?signature=NEVER-RETAIN',
                    'expiresAt':(datetime.now(timezone.utc)+timedelta(minutes=4)).isoformat()}}
            pytest.fail('No other endpoint is permitted')
    http=HTTP()
    monkeypatch.setattr(jobs,'client_for_credential',lambda *a,**k:http)
    monkeypatch.setattr(autonomy,'verify_spicy_dispatch',lambda *a,**k:copy.deepcopy(authority))
    def submit():
        token=execution._ACTIVE.set({'provider':'spicy','provider_called':True,'session_id':aid,
            'root':root,'directory':directory,'submitted_inputs':inputs})
        try:return jobs.submit_original(inputs,client=http)
        finally:execution._ACTIVE.reset(token)
    return {'root':root,'aid':aid,'directory':directory,'journal':directory/'spicy_job.json',
        'request':request,'inputs':inputs,'http':http,'submit':submit}


def accept(original):
    assert original['submit']().data['dispatch_status']=='acceptance_unknown'
    assert jobs.resume_original(original['root'],original['aid']).data['task_id']=='synthetic-original'


def collector(monkeypatch):
    from lib import openart_download
    def probe(path,native,asset):
        return {'mime':asset['mime'],'width':64,'height':64,'duration_seconds':asset.get('duration_seconds',5) if asset['mime'].startswith('video/') else None}
    monkeypatch.setattr(lifecycle,'_probe_v2',probe)
    def collect(url,target,**kwargs):
        target=Path(target);temporary=target.parent/('.openart-download-'+uuid.uuid4().hex)
        data=('SYNTHETIC '+url.split('?')[0]).encode();temporary.write_bytes(data)
        kwargs['before_publish']({'publication_source':str(temporary),'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)})
        temporary.rename(target)
    monkeypatch.setattr(openart_download,'collect_output',collect)


def test_zero_source_original_replay_is_same_key_and_body(original,monkeypatch):
    o=original;accept(o)
    creates=[call for call in o['http'].calls if call[1]=='/jobs/createTask']
    assert len(creates)==2 and creates[0][2:]==creates[1][2:]
    record=json.loads(o['journal'].read_text());assert record['version']=='2.0'
    monkeypatch.setattr(catalog,'load_account_observation_v2',lambda *a:pytest.fail('Known original must not refresh account'))
    jobs.resume_original(o['root'],o['aid'])
    assert len([call for call in o['http'].calls if call[1]=='/jobs/createTask'])==2


def test_known_original_older_than_window_still_polls(original,monkeypatch):
    o=original;accept(o)
    record=json.loads(o['journal'].read_text())
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(jobs,'_now',lambda:now+timedelta(days=5))
    assert jobs.resume_original(o['root'],o['aid']).data['task_id']=='synthetic-original'
    assert len([call for call in o['http'].calls if call[1]=='/jobs/createTask'])==2


def test_multivideo_and_ancillary_still_require_explicit_primary(original,monkeypatch):
    o=original;accept(o);collector(monkeypatch)
    o['http'].task.update(state='succeeded',output={'assets':[
        {'key':'joined.mp4','mime':'video/mp4','durationSeconds':15,'role':'joined'},
        {'key':'segment.mp4','mime':'video/mp4','durationSeconds':5,'role':'segment'},
        {'key':'last.png','mime':'image/png','role':'last_frame'}]})
    result=jobs.resume_original(o['root'],o['aid'])
    assert not result.success and result.data['dispatch_status']=='collected_needs_primary'
    assert len(result.data['outputs'])==3
    assert not Path(o['inputs']['output_path']).exists()
    chosen=lifecycle.select_primary_output(o['root'],o['aid'],'segment.mp4')
    assert chosen['receipt']['primary_asset_key']=='segment.mp4' and len(chosen['outputs'])==3
    assert any('/stills/' in row['path'] for row in chosen['outputs'])
    monkeypatch.setattr(o['http'],'request_json',lambda *a,**k:pytest.fail('Collected original resumes locally'))
    assert jobs.resume_original(o['root'],o['aid']).success
    assert 'NEVER-RETAIN' not in o['journal'].read_text()


def test_partial_refresh_preserves_asset_inventory(original,monkeypatch):
    o=original;accept(o);collector(monkeypatch)
    o['http'].task.update(state='succeeded',output={'assets':[
        {'key':'a.mp4','mime':'video/mp4','pending':True}, {'key':'b.mp4','mime':'video/mp4'}]})
    jobs.resume_original(o['root'],o['aid'])
    o['http'].task['output']['assets']=[{'key':'b.mp4','mime':'video/mp4'}]
    result=jobs.resume_original(o['root'],o['aid'])
    assert not result.success and not Path(o['inputs']['output_path']).exists()
    assert {row['key'] for row in json.loads(o['journal'].read_text())['assets']}=={'a.mp4','b.mp4'}


def test_retry_basis_requires_settled_terminal_and_never_posts(original):
    o=original;accept(o)
    with pytest.raises(jobs.SpicyError):lifecycle.retry_basis(o['root'],o['aid'])
    o['http'].task.update(state='failed',settled=True,cost='0.05')
    jobs.resume_original(o['root'],o['aid'])
    before=len(o['http'].calls)
    value=lifecycle.retry_basis(o['root'],o['aid'])
    assert value['authority']=='none' and value['parent_task_id']=='synthetic-original'
    assert len(o['http'].calls)==before and all(call[1]!='/jobs/retry' for call in o['http'].calls)
    proof=lifecycle.original_resolver(o['root'],'openmontage-project',str(o['journal'].relative_to(o['root'])))
    assert proof['record_sha256']==hashlib.sha256(o['journal'].read_bytes()).hexdigest()


def test_selected_primary_checks_exact_visual_controls(original,monkeypatch):
    o=original;collector(monkeypatch)
    request=o['request'];native=request['spicy_native']
    native['input'].update(resolution='720p',aspect_ratio='16:9')
    native['input_schema']['properties'].update(resolution={'type':'string'},aspect_ratio={'type':'string'})
    native['schema_sha256']=catalog.digest(native['input_schema'])
    native['request_sha256']=catalog.digest({'model':native['model'],'input':native['input']})
    request['spicy_authority']['quote']['native_request_sha256']=native['request_sha256']
    catalog.atomic_write_json(o['directory']/'request.json',request)
    accept(o)
    o['http'].task.update(state='succeeded',output={'assets':[{'key':'bad.mp4','mime':'video/mp4'}]})
    with pytest.raises(jobs.SpicyError,match='resolution'):
        jobs.resume_original(o['root'],o['aid'])
    assert not Path(o['inputs']['output_path']).exists()
    record=json.loads(o['journal'].read_text())
    assert Path(record['collection_intents_v2']['bad.mp4']['path']).exists()


def test_known_original_skips_later_schema_and_semantic_qualification(original,monkeypatch):
    o=original;accept(o)
    from lib import spicy_routes
    monkeypatch.setattr(jobs,'_native_semantics_v2',lambda *a:pytest.fail('Do not requalify known original'))
    monkeypatch.setattr(spicy_routes,'qualify',lambda *a:pytest.fail('Do not requalify known original'))
    assert jobs.resume_original(o['root'],o['aid']).data['task_id']=='synthetic-original'


def test_missing_mime_blocks_honestly_then_same_original_metadata_resolves(original,monkeypatch):
    o=original;accept(o);collector(monkeypatch)
    o['http'].task.update(state='succeeded',output={'assets':[{'key':'video.mp4'}]})
    result=jobs.resume_original(o['root'],o['aid'])
    assert result.data['dispatch_status']=='output_metadata_incomplete'
    assert not Path(o['inputs']['output_path']).exists()
    o['http'].task['output']['assets']=[{'key':'video.mp4','mime':'video/mp4'}]
    assert jobs.resume_original(o['root'],o['aid']).success
    assert len([call for call in o['http'].calls if call[1]=='/jobs/createTask'])==2


def test_real_local_mp4_webm_and_still_probes(tmp_path):
    import shutil
    import subprocess
    from PIL import Image
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Local media verification requires FFmpeg')
    native={'input':{'duration_seconds':5}}
    for suffix,codec,mime in [('mp4','libx264','video/mp4'),('webm','libvpx-vp9','video/webm')]:
        path=tmp_path/('SYNTHETIC.'+suffix)
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=64x64:r=2:d=5','-c:v',codec,
            '-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
        media=lifecycle._probe_v2(path,native,{'mime':mime})
        assert media['mime']==mime and 4.9<=media['duration_seconds']<=5.1
    path=tmp_path/'SYNTHETIC.png';Image.new('RGB',(64,64),'blue').save(path)
    media=lifecycle._probe_v2(path,native,{'mime':'image/png'})
    assert media['mime']=='image/png' and media['duration_seconds'] is None
