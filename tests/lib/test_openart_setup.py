"""First-account staging uses executable fake CLI and actual private transport.

Observed bootstrap red before implementation (2026-10-05):
`python -c 'from lib.openart_setup import inspect_qualification'` raised
ModuleNotFoundError. The legacy upload/dry-run helpers required full prior result
qualification, so no first-account path existed. This suite establishes the
actual captured readonly -> guaranteed upload -> preview path without fabricated
six-receipt profiles or any live provider calls.
"""
import json
import sys

import pytest

from lib import openart_jobs as jobs, openart_setup as setup
from tools import _openart_cli as cli
from tools.openart_account import OpenArtAccount

PATHS = {'account_id':'id','account_tier':'tier','submit_job_id':'job','result_job_id':'job',
         'status':'status','urls':'urls','status_terminal_ok':'done','status_terminal_fail':'failed',
         'url_hosts':['cdn.example.test']}
GUARANTEE = {'argv':['account'], 'nonspending':{'path':'upload.free','expected':True},
             'no_delayed_charge':{'path':'upload.noDelayed','expected':True}}
FAKE = r'''#!PYTHON
import json, os, sys, time
assert os.environ.get('OPENART_TOKEN') is None and os.environ.get('OPENART_API_KEY') is None
time.sleep(float(os.environ.get('FAKE_SLEEP','0')))
args=sys.argv[1:-2]
with open(os.environ['FAKE_LOG'],'a') as f: f.write(json.dumps(args)+'\n')
url='https://cdn.example.test/ref.png?X-Amz-Signature=private'
g={'free':True,'noDelayed':os.environ.get('DELAYED','true')=='true'}
if args==['version']: out={'version':os.environ.get('VERSION','1')}
elif args==['account']: out={'id':os.environ.get('ACCOUNT','private-account'),'tier':os.environ.get('TIER','turbo'),'upload':g}
elif args[:2]==['model','form']:
 out={'properties':{'prompt':{'type':'string'},'image':{'type':'string'},'duration':{'type':'integer','default':int(os.environ.get('DEFAULT','5'))}},'required':['prompt']}
 if os.environ.get('FORM_WRAPPER')=='true': out={'model':os.environ.get('FORM_MODEL','m1'),'media':os.environ.get('FORM_MEDIA','video'),'mode':os.environ.get('FORM_MODE','image2video'),'jsonSchema':out}
 if os.environ.get('FORM_CONFLICT')=='true': out['schema']={'properties':{'prompt':{'type':'string'}}}
elif args[:2]==['upload','add']: out={'url':url,'upload':g}
elif args[:2]==['generate','video']:
 params={'prompt':args[2]}
 if '--image' in args: params['image']=url if os.environ.get('MISMATCH')!='true' else 'https://cdn.example.test/wrong.png'
 out={'endpoint':'POST /api/cli/v1/generate','body':{'model':'m1','media':'video','mode':'image2video' if '--image' in args else 'text2video','params':params}}
else: raise SystemExit(7)
print(json.dumps(out))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    binary=tmp_path/'fake-openart'; binary.write_text(FAKE.replace('PYTHON',sys.executable)); binary.chmod(0o755)
    monkeypatch.setenv('OPENART_CLI_PATH',str(binary))
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR',str(tmp_path/'state'))
    log=tmp_path/'calls'; monkeypatch.setenv('FAKE_LOG',str(log))
    monkeypatch.setenv('OPENART_TOKEN','sk-secretsecretsecret')
    monkeypatch.setenv('OPENART_API_KEY','sk-secretsecretsecret')
    jobs.register_upload_approval_lookup(lambda root, uid, sha: {'state':'approved','upload_id':uid,'source_sha256':sha,'approval_sha256':'a'*64})
    yield tmp_path, log
    jobs.register_upload_approval_lookup(None)


def _inspect():
    return setup.inspect_qualification('m1','image2video',json_paths=PATHS)


def _guarantee():
    return setup.qualify_upload_guarantee('m1','image2video',guarantee=GUARANTEE,json_paths={'upload_url':'url'},url_hosts=['cdn.example.test'])


def _upload(fake):
    root,_=fake; source=root/'ref.png'; source.write_bytes(b'approved image')
    return jobs.upload_reference(root,'ref1',source,model='m1',mode='image2video')


def test_first_account_real_capture_to_pre_submit(fake):
    assert _inspect()['level']=='inspected'
    with pytest.raises(cli.OpenArtCLIError):
        jobs.load_qualification(model='m1',mode='image2video',require='pre_submit')
    _guarantee(); uploaded=_upload(fake)
    out=setup.qualify_preview('m1','image2video',prompt='private prompt',image_upload_id='ref1')
    assert out['level']=='pre_submit'
    profile=jobs.load_qualification(model='m1',mode='image2video',require='pre_submit')
    assert jobs.upload_url_for('ref1',profile=profile).endswith('Signature=private')
    assert profile['result_contract']=={'state':'unqualified'}
    assert jobs.qualification_status('m1','image2video')['level']=='pre_submit'
    with pytest.raises(cli.OpenArtCLIError):
        jobs.load_qualification(model='m1',mode='image2video',require='full')
    public=json.dumps([out,uploaded])
    assert 'private prompt' not in public and 'Signature=private' not in public and str(fake[0]) not in public
    calls=[json.loads(x) for x in fake[1].read_text().splitlines()]
    assert sum(x[:2]==['upload','add'] for x in calls)==1
    assert sum(x[:2]==['generate','video'] for x in calls)==1
    assert all('--async' not in x for x in calls)


def test_jsonschema_wrapped_form_capture_preserves_raw_hash_and_defaults(fake, monkeypatch):
    # Synthetic wrapper representative of the observed official 0.1.1 response.
    monkeypatch.setenv('FORM_WRAPPER', 'true')
    result = _inspect()
    profile = jobs.load_qualification(model='m1', mode='image2video', require='inspected')
    raw_form = {'model':'m1','media':'video','mode':'image2video','jsonSchema':
        {'properties':{'prompt':{'type':'string'},'image':{'type':'string'},'duration':{'type':'integer','default':5}},'required':['prompt']}}
    assert result['level'] == 'inspected'
    assert profile['form_defaults'] == {'duration': 5}
    assert profile['form_sha256'] == setup.qual._hash(raw_form)


@pytest.mark.parametrize('key,value', [('FORM_MODEL','another-model'), ('FORM_MODE','image2video-extra'), ('FORM_MEDIA','image')])
def test_jsonschema_wrapper_metadata_must_match_target(fake, monkeypatch, key, value):
    monkeypatch.setenv('FORM_WRAPPER', 'true')
    monkeypatch.setenv(key, value)
    with pytest.raises(cli.OpenArtCLIError, match='form_shape_unqualified'):
        _inspect()
    assert jobs.qualification_status('m1','image2video')['level'] == 'none'


def test_conflicting_jsonschema_wrapper_is_rejected(fake, monkeypatch):
    monkeypatch.setenv('FORM_WRAPPER', 'true')
    monkeypatch.setenv('FORM_CONFLICT', 'true')
    with pytest.raises(cli.OpenArtCLIError, match='form_shape_unqualified'):
        _inspect()
    assert jobs.qualification_status('m1','image2video')['level'] == 'none'


@pytest.mark.parametrize('guarantee',[None,{'argv':['account'],'nonspending':{'path':'upload.free','expected':True}},
    {'argv':['account'],'nonspending':{'path':'upload.free','expected':True},'no_delayed_charge':{'path':'upload.noDelayed','expected':False}}])
def test_absent_or_false_guarantee_declaration_zero_calls(fake,guarantee):
    _inspect(); before=fake[1].read_text()
    with pytest.raises(cli.OpenArtCLIError):
        setup.qualify_upload_guarantee('m1','image2video',guarantee=guarantee,json_paths={'upload_url':'url'},url_hosts=['cdn.example.test'])
    assert fake[1].read_text()==before
    with pytest.raises(cli.OpenArtCLIError): _upload(fake)
    assert fake[1].read_text()==before


def test_provider_false_delayed_guarantee_never_uploads(fake,monkeypatch):
    _inspect(); monkeypatch.setenv('DELAYED','false')
    with pytest.raises(cli.OpenArtCLIError): _guarantee()
    before=fake[1].read_text()
    with pytest.raises(cli.OpenArtCLIError): _upload(fake)
    assert fake[1].read_text()==before


@pytest.mark.parametrize('key,value',[('VERSION','2'),('ACCOUNT','other'),('TIER','max'),('DEFAULT','6')])
def test_changed_contract_fails_before_preview(fake,monkeypatch,key,value):
    _inspect(); _guarantee(); _upload(fake)
    monkeypatch.setenv(key,value)
    with pytest.raises(cli.OpenArtCLIError,match='qualification_changed'):
        setup.qualify_preview('m1','image2video',prompt='private prompt',image_upload_id='ref1')
    assert 'generate' not in fake[1].read_text()


def test_preview_url_mismatch_never_promotes(fake,monkeypatch):
    _inspect(); _guarantee(); _upload(fake); monkeypatch.setenv('MISMATCH','true')
    with pytest.raises(cli.OpenArtCLIError):
        setup.qualify_preview('m1','image2video',prompt='private prompt',image_upload_id='ref1')
    assert jobs.qualification_status('m1','image2video')['level']=='inspected'


def test_account_setup_requires_readonly_and_returns_only_opaque_metadata(fake):
    account=OpenArtAccount()
    out=account.execute({'action':'qualify_inspection','read_only':True,'model':'m1','mode':'image2video','json_paths':PATHS})
    assert out.success and out.data['evidence']['level']=='inspected'
    public=json.dumps(out.data)
    assert 'private-account' not in public and str(fake[0]) not in public
    before=fake[1].read_text()
    assert not account.execute({'action':'qualify_inspection','read_only':False}).success
    assert fake[1].read_text()==before


def test_inspection_uses_one_overall_timeout_budget(fake,monkeypatch):
    monkeypatch.setenv('FAKE_SLEEP','0.08')
    with pytest.raises(cli.OpenArtCLIError,match='timeout'):
        setup.inspect_qualification('m1','image2video',json_paths=PATHS,timeout=0.12)
    calls=[json.loads(x) for x in fake[1].read_text().splitlines()] if fake[1].exists() else []
    assert calls in ([], [['version']])
    assert jobs.qualification_status('m1','image2video')['level']=='none'


def test_changed_delayed_guarantee_blocks_first_upload_before_upload_add(fake,monkeypatch):
    # Observed red before fresh guarantee recheck: `upload add` was actually
    # logged, then the false response guarantee rejected only after upload.
    _inspect(); _guarantee(); monkeypatch.setenv('DELAYED','false')
    with pytest.raises(cli.OpenArtCLIError):
        _upload(fake)
    calls=[json.loads(x) for x in fake[1].read_text().splitlines()]
    assert not any(x[:2]==['upload','add'] for x in calls)


def test_changed_delayed_guarantee_blocks_preview_before_generation(fake,monkeypatch):
    # Observed red before fresh guarantee recheck: preview completed and saved
    # pre_submit, despite the current provider no-delayed-charge field being false.
    _inspect(); _guarantee(); _upload(fake); monkeypatch.setenv('DELAYED','false')
    with pytest.raises(cli.OpenArtCLIError,match='qualification_changed'):
        setup.qualify_preview('m1','image2video',prompt='private prompt',image_upload_id='ref1')
    calls=[json.loads(x) for x in fake[1].read_text().splitlines()]
    assert not any(x[:2]==['generate','video'] for x in calls)
    assert jobs.qualification_status('m1','image2video')['level']=='inspected'
