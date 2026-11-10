"""Current-schema video requests; synthetic sources/credentials, no HTTP."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from lib import spicy_catalog as catalog, spicy_jobs as jobs, spicy_media, spicy_uploads

ROWS = json.loads((Path(__file__).parents[1] / 'fixtures/spicy/catalogue-2026-10-22.json').read_text())['items']
REF = 'env:SPICY_NATIVE_REQUEST_OFFLINE_KEY'


def observation(model):
    item = next(row for row in ROWS if row['model'] == model)
    return {'model':model, 'input_schema':copy.deepcopy(item['inputSchema']), 'schema_sha256':catalog.digest(item['inputSchema']),
        'task':item['tasks'][0], 'enabled':True, 'available':True, 'modality':'video',
        'credential_binding':{'credential_ref':REF,'credential_digest':'a'*64}}


def carrier(tmp_path, model, controls):
    return {'project_dir':str(tmp_path),'model':model,'mode':observation(model)['task'].replace('-','_'),
        'credential_ref':REF,'native_input':controls,'input_assets':[]}


def test_reference_free_text_video_is_ready(tmp_path):
    model = 'bytedance/seedance-2.0-fast/text-to-video'
    obs = observation(model)
    value = jobs.build_native_request_v2(carrier(tmp_path,model,{'prompt':'SYNTHETIC','duration_seconds':5,'generate_audio':False}),obs)
    assert value['sources'] == [] and value['payload_state'] == 'ready'
    assert value['input'] == {'prompt':'SYNTHETIC','duration_seconds':5,'generate_audio':False}
    jobs.require_ready_v2(value)


@pytest.mark.parametrize('model,pointer,role,mime', [
    ('bytedance/seedance-2.0-fast/image-to-video','/image_url','first_frame','image/png'),
    ('bytedance/seedance-2.0-fast/reference-to-video','/reference_image_urls/0','reference_image','image/png'),
    ('bytedance/seedance-2.0-fast/video-extend','/video_url','source_video','video/mp4'),
    ('kling/4.0/video-edit','/video_url','source_video','video/mp4'),
])
def test_distinct_media_routes_compile_from_local_sources(tmp_path,monkeypatch,model,pointer,role,mime):
    path=tmp_path/'source.bin'; path.write_bytes(b'SYNTHETIC SOURCE BYTES')
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(spicy_media,'probe',lambda path:{'mime':mime,'media_kind':mime.split('/')[0],
        'bytes':path.stat().st_size,'sha256':sha,'duration_seconds':5 if mime.startswith('video') else None})
    monkeypatch.setattr(spicy_uploads,'usable_receipt_v2',lambda *a:None)
    value=carrier(tmp_path,model,{'prompt':'SYNTHETIC','duration_seconds':5})
    if model.startswith('kling/'):
        value['native_input'].pop('duration_seconds')
    if 'generate_audio' in observation(model)['input_schema']['properties']:
        value['native_input']['generate_audio']=False
    value['input_assets']=[{'role':role,'pointer':pointer,'source_path':str(path),'source_sha256':sha}]
    result=jobs.build_native_request_v2(value,observation(model))
    assert result['payload_state']=='needs_upload' and result['sources'][0]['pointer']==pointer
    assert result['sources'][0]['mime']==mime
    with pytest.raises(jobs.SpicyError,match='unresolved'):
        jobs.require_ready_v2(result)


def test_raw_uri_externalweights_and_mode_mismatch_refused(tmp_path):
    model='bytedance/seedance-2.0-fast/text-to-video'
    value=carrier(tmp_path,model,{'prompt':'SYNTHETIC https://unsafe.invalid','duration_seconds':5,'generate_audio':False})
    with pytest.raises(jobs.SpicyError,match='raw source'):
        jobs.build_native_request_v2(value,observation(model))
    value['native_input']['prompt']='SYNTHETIC';value['mode']='image_to_video'
    with pytest.raises(jobs.SpicyError,match='task/mode'):
        jobs.build_native_request_v2(value,observation(model))


def test_observation_project_key_ttl_and_balance(tmp_path,monkeypatch):
    monkeypatch.setenv('SPICY_NATIVE_REQUEST_OFFLINE_KEY','SYNTHETIC-NEVER-WORKING')
    model='bytedance/seedance-2.0-fast/text-to-video'
    row=next(row for row in ROWS if row['model']==model)
    class HTTP:
        binding=jobs.credential_binding(REF)
        held='0.00'
        def get_model(self,model):
            assert model==row['model']
            return {'data':copy.deepcopy(row)}
        def get_balance(self):
            return {'data':{'available':'-0.1','held':self.held,'total':'-0.1'}}
    http=HTTP(); catalog.observe_account_v2(tmp_path,http,model,REF)
    assert catalog.load_account_observation_v2(tmp_path,model,REF)
    other=tmp_path/'other';other.mkdir()
    catalog.observation_path_v2(other,model).parent.mkdir()
    catalog.observation_path_v2(other,model).write_bytes(catalog.observation_path_v2(tmp_path,model).read_bytes())
    assert catalog.load_account_observation_v2(other,model,REF) is None
    monkeypatch.setenv('SPICY_NATIVE_REQUEST_OFFLINE_KEY','SYNTHETIC-DIFFERENT')
    assert catalog.load_account_observation_v2(tmp_path,model,REF) is None
    http.held='-0.1'
    with pytest.raises(catalog.SpicyCatalogError,match='held'):
        catalog.observe_account_v2(tmp_path,http,model,REF)


def test_source_array_fill_orders_numeric_pointer_components(tmp_path,monkeypatch):
    path=tmp_path/'image.bin';path.write_bytes(b'SYNTHETIC')
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    schema={'type':'object','properties':{'images':{'type':'array','maxItems':16,
        'items':{'type':'string','contentMediaType':['image/png']},'x-ui':{'widget':'multi-upload'}}},'required':['images']}
    obs={'model':'synthetic/reference-to-video','input_schema':schema,'schema_sha256':catalog.digest(schema),
        'task':'reference-to-video','enabled':True,'available':True,'modality':'video',
        'credential_binding':{'credential_ref':REF,'credential_digest':'a'*64}}
    monkeypatch.setattr(spicy_media,'probe',lambda path:{'mime':'image/png','media_kind':'image','bytes':9,'sha256':sha,'duration_seconds':None})
    monkeypatch.setattr(spicy_uploads,'usable_receipt_v2',lambda *a:None)
    value={'project_dir':str(tmp_path),'model':obs['model'],'credential_ref':REF,'native_input':{},
        'input_assets':[{'role':'first_frame','pointer':'/images/'+str(index),'source_path':str(path)} for index in range(11)]}
    result=jobs.build_native_request_v2(value,obs)
    assert len(result['input']['images'])==11 and len(result['sources'])==11


def test_cli_local_docs_never_resolves_a_key(monkeypatch,capsys):
    monkeypatch.setattr(jobs,'client_for_credential',lambda *a,**k:pytest.fail('Public local docs do not use credentials'))
    assert jobs.main(['account','docs','quote'])==0
    assert 'quotes-and-compatibility' in capsys.readouterr().out
