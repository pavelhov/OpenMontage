"""Runtime authority seams exercised by an executable fake CLI.

All account, approval, creative and semantic facts here are synthetic software
fixtures; these tests do not qualify OpenArt or perform real audiovisual review.
"""
import copy
import hashlib
import json
from dataclasses import replace
import pytest
from lib import provider_benchmark as b, openart_jobs as jobs, openart_credit as credit
from lib import production_execution as execution, production_request as preparation
from lib import openart_dispatch as dispatch
from lib.provider_credit_ledger import read_existing_snapshot
from tools.video.openart_cli_video import OpenArtCLIVideo
from tools._openart_cli import OpenArtCLIError
from tests.integration.test_openart_dispatch_recovery import governed
from tests.lib.test_provider_benchmark import manifest


def runtime_source(governed):
    root,inputs,profile,tmp=governed
    path=root/'benchmark-inputs.json';path.write_text(json.dumps(inputs))
    source={'occurrence_id':'logical-1','project_root':str(root),'inputs':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},'scope_occurrence_index':0}
    plan=manifest();plan['evidence_kind']='runtime'
    native=jobs.prepare_native_request(credit._controls(inputs),profile)
    proof=credit._load(inputs['credit_quote_id']);packet=preparation.source_packet(root,'entry')
    o=plan['attempt_order'][0]
    plan['deliverables'][0]['story_revision']=json.loads((root/'project.json').read_text())['story_revision']
    o.update(native_body_sha256=native['native_body_sha256'],native_argv_sha256=native['native_argv_sha256'],native_controls_sha256=native['native_controls_sha256'],
        compiled_request_sha256=preparation.validate_preparation(inputs,native,profile)['compiled_sha256'],
        preparation_review_sha256=preparation.validate_preparation(inputs,native,profile)['review_sha256'],quote_sha256=proof['quote_sha256'],
        semantics_sha256=b.digest([{k:v for k,v in row.items() if k!='occurrence_id'} for row in packet['occurrences']]),
        reference_sha256=b.digest([{k:r[k] for k in ('id','role','cast_ids','sha256')} for r in packet['binding']['references']]),quoted_credits='2.25',ceiling_credits='3')
    # Bounded source-authority test uses the fixture's actual 8s/720p package.
    # A full benchmark envelope still rejects these non-benchmark settings.
    plan['settings'].update(duration_seconds=8,resolution='720p');plan['allowance']['credits']='10'
    plan['profiles']['turbo'].update(exact_model_id=profile['model'],origin_pre_submit_sha256=native['profile_sha256'],
        account_id_sha256=profile['account_id_sha256'],workspace_sha256=hashlib.sha256(proof['terms']['workspace'].encode()).hexdigest(),
        tier=profile['tier'],cli_version=profile['cli_version'],form_sha256=profile['form_sha256'],form_defaults_sha256=native['form_defaults_sha256'])
    return plan,source


def test_runtime_source_uses_actual_retained_native_preparation_quote_and_approval(governed):
    plan,source=runtime_source(governed)
    checked=b._runtime_source(plan,source)
    assert checked['binding'].native_sha256==plan['attempt_order'][0]['native_body_sha256']
    assert read_existing_snapshot()['initialized'] is False
    assert not (governed[3]/'paid').exists()


@pytest.mark.parametrize('mutation',['input_bytes','native','preparation','profile','approval','auto_continue','story'])
def test_runtime_source_actual_tamper_fails_without_submit(governed,mutation):
    plan,source=runtime_source(governed);root,inputs,profile,tmp=governed
    if mutation=='input_bytes':
        (root/'benchmark-inputs.json').write_text('{}')
    elif mutation=='native':plan['attempt_order'][0]['native_body_sha256']='0'*64
    elif mutation=='preparation':
        path=root/'artifacts/preparation_review-r1.json';value=json.loads(path.read_text());value['status']='fail';path.write_text(json.dumps(value))
    elif mutation=='profile':plan['profiles']['turbo']['origin_pre_submit_sha256']='0'*64
    elif mutation=='approval':(root/'credit-approval.json').write_text('{}')
    elif mutation=='story':plan['deliverables'][0]['story_revision']='unapproved-story'
    else:
        (root/'artifacts/decision_log.json').write_text(json.dumps({'decisions':[{'category':'approval_policy','subject':'Production autonomy policy','selected':'auto_continue:'+('a'*64)}]}))
    with pytest.raises((ValueError, OSError, OpenArtCLIError)):b._runtime_source(plan,source)
    assert not (tmp/'paid').exists()
    assert read_existing_snapshot()['initialized'] is False


def test_full_runtime_envelope_rejects_fixture_and_nonbenchmark_settings(governed):
    plan,source=runtime_source(governed)
    with pytest.raises(b.BenchmarkError):b.bind_runtime_manifest(plan,[source]*12,source['inputs'])
    fixture=manifest()
    with pytest.raises(b.BenchmarkError,match='fixture'):b._runtime_plan(fixture)


def test_runtime_original_rows_keep_job_and_unknown_billing_independent(governed):
    plan,source=runtime_source(governed);checked=b._runtime_source(plan,source)
    result=OpenArtCLIVideo().execute(governed[1]);attempt=result.data['production_attempt_id']
    report=b._runtime_rows({'plan':plan,'sources':[source]},[checked],read_existing_snapshot())
    assert report[0]['attempt_id']==attempt
    assert report[0]['billing_status']=='uncertain'
    assert report[0]['charged_credits'] is None
    assert str(report[0]['held_credits'])=='9/4'
    assert report[0]['accepted'] is False
    assert report[0]['source_seconds']==0
    assert len((governed[3]/'paid').read_text().splitlines())==1
    # Exact request receipt rereads current bytes, even after durable reservation.
    path=governed[0]/'production_attempts'/attempt/'request.json';value=json.loads(path.read_text());value['shot_id']='substitute';path.chmod(0o600);path.write_text(json.dumps(value))
    with pytest.raises((ValueError, OpenArtCLIError)):b._runtime_rows({'plan':plan,'sources':[source]},[checked],read_existing_snapshot())


def test_runtime_original_binding_cannot_substitute_frozen_scope_occurrence(governed):
    plan,source=runtime_source(governed);checked=b._runtime_source(plan,source)
    OpenArtCLIVideo().execute(governed[1])
    checked['binding']=replace(checked['binding'],native_sha256='0'*64)
    with pytest.raises(b.BenchmarkError,match='Binding differs'):
        b._runtime_rows({'plan':plan,'sources':[source]},[checked],read_existing_snapshot())


def test_actual_overquote_runtime_report_retains_full_debit_and_physical_quarantine(governed,monkeypatch):
    from tests.integration.test_openart_dispatch_recovery import _qualified_billing_origin
    plan,source=runtime_source(governed);checked=b._runtime_source(plan,source)
    root,inputs,profile,tmp,attempt,binding,contract=_qualified_billing_origin(governed,monkeypatch)
    monkeypatch.setenv('FAKE_CHARGE','4.00')
    proof=dispatch.qualify_resolution_contract(root,attempt,binding.request_sha256,contract)
    dispatch.resolve_attempt(root,attempt,binding.request_sha256,billing_proof_id=proof['billing_proof_id'])
    # Narrow authority seam isolates reporting from the full twelve-source envelope;
    # all reservation, receipt, native/prep and ledger facts are actual test runtime.
    envelope={'plan':plan,'sources':[source]}
    monkeypatch.setattr(b,'validate_runtime_manifest',lambda m:[checked])
    report=b.summarize_runtime_metrics(envelope)
    assert report['turbo']['known_charged_credits']=={'numerator':'4','denominator':'1'}
    assert report['billing_violations'][0]['reasons']==['above_quote','above_ceiling']
    assert report['billing_quarantined'] is True
    assert report['turbo']['economics_complete'] is False
    assert report['turbo']['certified_original_deliverables']==0
    assert b.next_runtime_attempt(envelope) is None
    snapshot=read_existing_snapshot()
    assert snapshot['account_quarantine']
    assert snapshot['reservations'][0]['charged_units']==16
    assert len((tmp/'paid').read_text().splitlines())==1


def test_runtime_resume_rechecks_real_account_claim_and_keeps_terminal_unknown_hold(governed,monkeypatch):
    from tests.integration.test_openart_dispatch_recovery import _qualified_billing_origin
    plan,source=runtime_source(governed);checked=b._runtime_source(plan,source)
    root,inputs,profile,tmp,attempt,binding,contract=_qualified_billing_origin(governed,monkeypatch)
    # Test one original source authority and a repeated checked input solely to
    # inspect the resume decision; not a valid full benchmark envelope/approval.
    envelope={'plan':plan,'sources':[source,source]}
    monkeypatch.setattr(b,'validate_runtime_manifest',lambda m:[checked,checked])
    original_rows=b._runtime_rows
    rows=original_rows({'plan':plan,'sources':[source]},[checked],read_existing_snapshot())
    monkeypatch.setattr(b,'_runtime_rows',lambda *args:rows)
    assert b.next_runtime_attempt(envelope) is None  # actual active account claim
    dispatch.resolve_attempt(root,attempt,binding.request_sha256)
    # Account debit stayed unknown; its actual hold remains in allowance arithmetic.
    rows=original_rows({'plan':plan,'sources':[source]},[checked],read_existing_snapshot())
    assert rows[0]['status']=='terminal_unselected' and rows[0]['billing_status']=='uncertain'
    assert rows[0]['held_credits']==b.Fraction('2.25')
    # Remaining twelve-plan ceilings exceed actual remaining allowance. Eligibility
    # must refuse rather than manufacture a caller-provided 'eligible' result.
    monkeypatch.setattr(b,'_runtime_rows',lambda *args:rows)
    before=read_existing_snapshot()
    assert b.next_runtime_attempt(envelope) is None
    after=read_existing_snapshot()
    assert before==after
    assert len((tmp/'paid').read_text().splitlines())==1


def test_runtime_source_fixture_profile_cannot_replace_observed_origin(governed,monkeypatch):
    plan,source=runtime_source(governed)
    fixture=copy.deepcopy(governed[2]);fixture['source']='fixture'
    monkeypatch.setattr(jobs,'load_qualification',lambda **kwargs:fixture)
    with pytest.raises(b.BenchmarkError,match='fixture/unqualified'):
        b._runtime_source(plan,source)
    assert read_existing_snapshot()['initialized'] is False


def twelve_runtime_envelope(governed,monkeypatch):
    """Full public 5s/768p envelope with twelve real local authority chains.

    The executable provider, approvals and semantic facts remain test fixtures.
    """
    import shutil,sys
    from pathlib import Path
    from tests.integration.test_openart_dispatch_recovery import FAKE,write
    from lib import openart_setup as setup
    from tests.lib.test_openart_credit import PATHS
    from lib.shot_contract import contract_digest,file_sha256
    root,base,old_profile,tmp=governed
    fake=FAKE.replace("'model':'m1','mode':'image2video','credits'", "'model':args[args.index('--model')+1],'mode':'image2video','credits'")
    fake=fake.replace("body={'model':'m1'", "body={'model':args[args.index('--model')+1]")
    (tmp/'fake').write_text(fake.replace('PYTHON',sys.executable))
    contract=json.loads((root/'artifacts/shot_contract.json').read_text());contract['shots'][0]['duration_seconds']=5
    sha=contract_digest(contract);contract['project_review']['subject_sha256']=sha
    for shot in contract['shots']:shot['review']['subject_sha256']=sha
    write(root/'artifacts/shot_contract.json',contract)
    scene=json.loads((root/'artifacts/scene_plan.json').read_text());scene['scenes'][0]['end_seconds']=5;write(root/'artifacts/scene_plan.json',scene)
    script=json.loads((root/'artifacts/script.json').read_text());script['total_duration_seconds']=5;script['sections'][0]['end_seconds']=5;write(root/'artifacts/script.json',script)
    timing=json.loads((root/'artifacts/compiled_request-c1.json').read_text())['timing'];timing['duration_seconds']=5
    timing['action_windows'][0].update(start_seconds=4.1,end_seconds=4.3)
    timing['action_windows'][1].update(start_seconds=4.3,end_seconds=4.5)
    plan=manifest();plan['evidence_kind']='runtime';plan['models']={'turbo':'m1','max':'m2'};plan['allowance']['credits']='40'
    sources=[];observations=[];inputs_by_occ=[];profiles={}
    guarantee={'argv':['account'],'nonspending':{'path':'upload.free','expected':True},'no_delayed_charge':{'path':'upload.noDelayed','expected':True}}
    for model in ('m1','m2'):
        setup.inspect_qualification(model,'image2video',json_paths=PATHS)
        setup.qualify_upload_guarantee(model,'image2video',guarantee=guarantee,json_paths={'upload_url':'url'},url_hosts=['up.openart.test'])
    for index,o in enumerate(plan['attempt_order']):
        target=tmp/('benchmark-'+o['occurrence_id']);shutil.copytree(root,target,ignore=shutil.ignore_patterns('production_attempts'))
        def rebase(value):
            if isinstance(value,str):return value.replace(str(root),str(target))
            if isinstance(value,list):return [rebase(v) for v in value]
            if isinstance(value,dict):return {k:rebase(v) for k,v in value.items()}
            return value
        inputs=rebase(base);inputs.update(model=plan['models'][o['model']],duration=5,resolution='768p',image_upload_id='benchmark-up-'+str(index))
        inputs['prompt']=preparation.compile_prompt(target,'entry')['prompt']
        packet=preparation.source_packet(target,'entry');uid=inputs['image_upload_id']
        uploadapproval={'version':'1.0','upload_id':uid,'asset_id':'start','shot_id':'entry','source_sha256':file_sha256(Path(inputs['image_path'])),'source_binding':packet['binding'],
            'approved_by':'Synthetic benchmark fixture author; no live authority','evidence_path':'approval.txt','evidence_sha256':file_sha256(target/'approval.txt')}
        write(target/('artifacts/upload_approval-'+uid+'.json'),uploadapproval)
        jobs.upload_reference(target,uid,Path(inputs['image_path']),model=inputs['model'],mode='image2video')
        if inputs['model'] not in profiles:
            setup.qualify_preview(inputs['model'],'image2video',prompt=inputs['prompt'],duration=5,aspect_ratio='16:9',resolution='768p',image_upload_id=uid)
            profiles[inputs['model']]=jobs.load_qualification(model=inputs['model'],mode='image2video',require='pre_submit')
        profile=profiles[inputs['model']];dr=next(e for e in profile['captured_receipts'] if e['kind']=='dry_run')
        inputs.update(native_dry_run_receipt_id=dr['receipt_id'],native_dry_run_receipt_sha256=dr['receipt_sha256'])
        native=jobs.prepare_native_request(credit._controls(inputs),profile)
        compiled=preparation.prepare_compiled_request(inputs,native,profile,coverage=preparation.compile_prompt(target,'entry')['coverage'],timing=timing)
        review=json.loads((target/'artifacts/preparation_review-r1.json').read_text());review['subject_sha256']=preparation.digest(compiled)
        write(target/'artifacts/compiled_request-c1.json',compiled);write(target/'artifacts/preparation_review-r1.json',review)
        scope=json.loads((target/'production_scopes.json').read_text())['scopes'][0]
        scope['approval_plan_sha256']=execution.approval_plan_digest(contract);scope['requests']['entry']=execution.planned_request_digest(inputs,project_dir=target)
        qcontract=credit.qualify_quote_contract(inputs,profile,credit.QuoteContract('credits','quantum','balance','id','workspace','model','mode','maximum','all_settings'))
        quote=credit.refresh_credit_evidence(inputs,profile,qualification_sha256=qcontract['qualification_sha256'])
        proof=credit._load(quote['quote_id'])
        auth=json.loads((target/'artifacts/credit_authorization-credit.json').read_text())
        auth.update(project_root=str(target),allowance='40',allowance_id='paired-benchmark',purpose='generation' if o['purpose']=='original' else o['purpose'])
        auth['occurrences']=[{'id':o['occurrence_id'],'index':0,'request_sha256':scope['requests']['entry'],'native_sha256':native['native_body_sha256'],'profile_sha256':native['profile_sha256'],'quote_sha256':quote['quote_sha256']}]
        raw=json.dumps({'kind':'openart_credit_authorization','terms':{k:v for k,v in auth.items() if k!='evidence'}},sort_keys=True).encode()
        (target/'credit-approval.json').write_bytes(raw);auth['evidence']['sha256']=hashlib.sha256(raw).hexdigest();scope['credit_authorization_sha256']=credit.credit_authorization_digest(auth)
        write(target/'artifacts/credit_authorization-credit.json',auth);write(target/'production_scopes.json',{'version':'1.0','scopes':[scope]})
        inputs.update(credit_quote_id=quote['quote_id'],credit_qualification_sha256=qcontract['qualification_sha256'])
        path=target/'benchmark-inputs.json';write(path,inputs)
        source={'occurrence_id':o['occurrence_id'],'project_root':str(target),'inputs':{'path':str(path),'sha256':file_sha256(path)},'scope_occurrence_index':0}
        semantics=b.digest([{k:v for k,v in row.items() if k!='occurrence_id'} for row in packet['occurrences']]);refs=b.digest([{k:r[k] for k in ('id','role','cast_ids','sha256')} for r in packet['binding']['references']])
        o.update(semantics_sha256=semantics,reference_sha256=refs,native_body_sha256=native['native_body_sha256'],native_argv_sha256=native['native_argv_sha256'],native_controls_sha256=native['native_controls_sha256'],compiled_request_sha256=b.digest(compiled),preparation_review_sha256=b.digest(review),quote_sha256=quote['quote_sha256'],quoted_credits='2.25',ceiling_credits='3')
        case=next(c for c in plan['cases'] if c['id']==o['case_id']);case.update(semantics_sha256=semantics,reference_sha256=refs)
        plan['profiles'][o['model']].update(exact_model_id=profile['model'],origin_pre_submit_sha256=native['profile_sha256'],account_id_sha256=profile['account_id_sha256'],workspace_sha256=hashlib.sha256(proof['terms']['workspace'].encode()).hexdigest(),tier=profile['tier'],cli_version=profile['cli_version'],form_sha256=profile['form_sha256'],form_defaults_sha256=native['form_defaults_sha256'])
        next(d for d in plan['deliverables'] if d['original_occurrence_ids']==[o['occurrence_id']])['story_revision']=contract['story_revision']
        sources.append(source);observations.append({'account_receipt':proof['account_receipt'],'allowance_id':auth['allowance_id'],'allowance':auth['allowance']});inputs_by_occ.append(inputs)
    plan['allowance']['observation_sha256']=b.digest(observations)
    path=tmp/'benchmark-approval.json';write(path,{'kind':'openart_paired_benchmark','plan':plan,'sources':sources})
    envelope=b.bind_runtime_manifest(plan,sources,{'path':str(path),'sha256':file_sha256(path)})
    return envelope,inputs_by_occ


@pytest.mark.parametrize('crash',[None,'ledger_prepared'])
def test_public_twelve_runtime_envelope_partial_report_and_actual_resume(governed,monkeypatch,crash):
    envelope,inputs=twelve_runtime_envelope(governed,monkeypatch)
    empty=b.summarize_runtime_metrics(envelope)
    assert empty['evidence_kind']=='runtime' and empty['turbo']['attempted']==empty['max']['attempted']==0
    candidate=b.next_runtime_attempt(envelope)
    assert candidate['occurrence']['occurrence_id']=='logical-1' and candidate['dispatch_authorized'] is False
    assert read_existing_snapshot()['initialized'] is False
    assert not (governed[3]/'paid').exists()
    if crash:
        def die(stage,attempt_id):
            if stage==crash: raise RuntimeError('Synthetic original-boundary crash')
        dispatch._CRASH_HOOK=die
    if crash:
        with pytest.raises(RuntimeError,match='Synthetic original-boundary crash'):
            OpenArtCLIVideo().execute(inputs[0])
    else:
        result=OpenArtCLIVideo().execute(inputs[0])
    dispatch._CRASH_HOOK=None
    attempt=read_existing_snapshot()['reservations'][0]['attempt_id']
    partial=b.summarize_runtime_metrics(envelope)
    assert partial['turbo']['attempted']==1 and partial['max']['attempted']==0
    assert partial['records'][0]['attempt_id']==attempt
    assert partial['turbo']['held_credits']=={'numerator':'9','denominator':'4'}
    assert partial['turbo']['certified_original_deliverables']==0
    assert b.next_runtime_attempt(envelope) is None
    if crash:
        assert partial['records'][0]['status']=='pending'
        assert partial['records'][0]['request_receipt'] is None
        assert partial['records'][0]['private_dispatch_receipt']['sha256']
        assert not (governed[3]/'paid').exists()
        return
    monkeypatch.setenv('FAKE_STATUS','done');jobs.promote_result_contract(attempt,json_paths={'url_hosts':['cdn.openart.test']})
    monkeypatch.setenv('FAKE_STATUS','failed');binding=dispatch._manifest(attempt)[1]
    dispatch.resolve_attempt(governed[3]/'benchmark-logical-1',attempt,binding.request_sha256)
    before=read_existing_snapshot()
    candidate=b.next_runtime_attempt(envelope)
    assert candidate['occurrence']['occurrence_id']=='logical-2'
    assert before==read_existing_snapshot()
    assert len((governed[3]/'paid').read_text().splitlines())==1


def test_runtime_duration_uses_real_ffprobe_and_never_counts_encoding_padding(tmp_path):
    import shutil,subprocess
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):pytest.skip('local FFmpeg unavailable')
    path=tmp_path/'fixture-5s-with-container-padding.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=32x32:r=125',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','5.016',
        '-c:v','libx264','-c:a','aac',str(path)],check=True,timeout=30)
    from lib.production_review import probe_master
    measured=b.Fraction(str(probe_master(path)['duration_seconds']))
    assert measured>5 and measured<b.Fraction('5.05')
    assert b._runtime_source_seconds(path,5)==5
    with pytest.raises(b.BenchmarkError,match='timing tolerance'):b._runtime_source_seconds(path,4)


def collect_current_av_fixture(envelope, inputs, index, duration, monkeypatch):
    """Actual original collection/provenance/v2 gates; only download is local-fixture transport."""
    import shutil,subprocess
    from pathlib import Path
    from tests.lib.test_openart_jobs import fake_download
    from lib.production_review import probe_master, assert_final_eligible, certify_final
    from lib.shot_contract import (file_sha256, contract_digest, selection_digest,
        review_digest, UPSTREAM_PREDICATES, PROJECT_PREDICATES)
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):pytest.skip('local FFmpeg unavailable')
    root=Path(inputs[index]['project_dir']);fixture=root/'provider-fixture.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=32x32:r=125',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t',str(duration),
        '-c:v','libx264','-c:a','aac',str(fixture)],check=True,timeout=30)
    result=OpenArtCLIVideo().execute(inputs[index]);attempt=result.data['production_attempt_id']
    monkeypatch.setenv('FAKE_STATUS','done')
    jobs.promote_result_contract(attempt,json_paths={'url_hosts':['cdn.openart.test']})
    fake_download(monkeypatch,payload=fixture.read_bytes())
    binding=dispatch._manifest(attempt)[1]
    record=execution.collect_openart_attempt(root,attempt,request_sha256=binding.request_sha256)
    assert record['status']=='generated'
    outgoing=root/'outgoing.png'
    subprocess.run(['ffmpeg','-v','error','-i',record['output']['path'],'-frames:v','1',str(outgoing)],check=True,timeout=30)
    story=json.loads((root/'project.json').read_text())['story_revision']
    attestation=lambda subject,names:{'review_id':'synthetic-offduration-review','reviewer':'Synthetic fixture; no live AV claims',
        'story_revision':story,'subject_sha256':subject,'status':'pass',
        'predicates':[{'name':n,'status':'pass','evidence':'Synthetic software-test AV/story judgment'} for n in sorted(names)]}
    selection={'attempt_id':attempt,'output':record['output'],'outgoing_frame':{'path':str(outgoing),'sha256':file_sha256(outgoing)}}
    selection['review']=attestation(selection_digest(selection),UPSTREAM_PREDICATES)
    execution.record_selection(root,'entry',selection)
    contract=execution.load_shot_contract(root);observed=probe_master(Path(record['output']['path']))['duration_seconds']
    review={'version':'2.0','project_id':contract['project_id'],'story_revision':story,'contract_sha256':contract_digest(contract),
        'output_path':record['output']['path'],'output_sha256':record['output']['sha256'],'duration_seconds':observed,'release_status':'final',
        'reviewer':{'id':'synthetic-offduration-fixture','kind':'agent','method':'Synthetic test judgments; not real quality/AV certification','reviewed_at':'2026-10-06T00:00:00Z'},
        'dimensions':{n:{'status':'pass','evidence':'Synthetic test judgment'} for n in ('transport','technical','visual','audio','story')},
        'av_review':{'status':'pass','mode':'synchronized_av','watched_full':True,'listened_full':True,'start_seconds':0,'end_seconds':observed,'evidence':'Synthetic test judgment'},
        'scenes':[{'scene_id':'entry','attempt_id':attempt,'output_sha256':selection['output']['sha256'],
            'selection_sha256':selection_digest(selection),'review_sha256':review_digest(selection['review']),'start_seconds':0,'end_seconds':observed}],
        'predicates':attestation('0'*64,PROJECT_PREDICATES)['predicates']}
    # Both current provenance and actual full v2 gate must pass before reproducing
    # benchmark duration rejection. No validator/authority return is patched.
    from lib.production_provenance import validate_attempt_provenance
    validate_attempt_provenance(root,attempt,shot_id='entry',story_revision=story,expected_output=selection['output'])
    assert assert_final_eligible(root,review)['eligible'] is True
    certify_final(root,review)
    dispatch.resolve_attempt(root,attempt,binding.request_sha256)
    return attempt,binding,observed


def test_public_runtime_offduration_originals_are_retained_failures_not_report_errors(governed,monkeypatch):
    envelope,inputs=twelve_runtime_envelope(governed,monkeypatch)
    first,binding,short=collect_current_av_fixture(envelope,inputs,0,'4.8',monkeypatch)
    # Retain one authoritative debit and a distinct original unknown hold.
    contract=dispatch.BillingContract('billing.account','billing.workspace','billing.native','billing.job',
        'billing.amount','billing.quantum','billing.final','billing.per_job_authoritative','billing.debit_id',
        'billing.refund','billing.refund_id','billing.refund_authoritative','billing.refund_of')
    proof=dispatch.qualify_resolution_contract(inputs[0]['project_dir'],first,binding.request_sha256,contract)
    dispatch.resolve_attempt(inputs[0]['project_dir'],first,binding.request_sha256,billing_proof_id=proof['billing_proof_id'])
    second,_,long=collect_current_av_fixture(envelope,inputs,1,'5.2',monkeypatch)
    report=b.summarize_runtime_metrics(envelope)
    assert [r['attempt_id'] for r in report['records']]==[first,second]
    assert all(r['accepted'] is False and r['source_seconds']=={'numerator':'0','denominator':'1'} for r in report['records'])
    assert all(r['failure_categories']==['technical_transport'] for r in report['records'])
    assert all(r['source_duration_status']=='outside_tolerance' and r['source_duration_reason'] for r in report['records'])
    assert [r['observed_source_seconds'] for r in report['records']]==[b._ratio(b.Fraction(str(short))),b._ratio(b.Fraction(str(long)))]
    assert report['turbo']['known_charged_credits']=={'numerator':'9','denominator':'4'}
    assert report['max']['held_credits']=={'numerator':'9','denominator':'4'}
    assert report['turbo']['certified_original_deliverables']==report['max']['certified_original_deliverables']==0
    before=read_existing_snapshot();candidate=b.next_runtime_attempt(envelope)
    assert candidate['occurrence']['occurrence_id']=='logical-3'
    assert before==read_existing_snapshot()
    assert len((governed[3]/'paid').read_text().splitlines())==2
    # A distinct original with authoritative unchanged bytes but no usable
    # ffprobe duration is likewise a technical outcome, not evidence tampering.
    from tests.lib.test_openart_jobs import fake_download
    third_result=OpenArtCLIVideo().execute(inputs[2]);third=third_result.data['production_attempt_id']
    fake_download(monkeypatch,payload=b'Synthetic original undecodable provider transport')
    third_binding=dispatch._manifest(third)[1]
    generated=execution.collect_openart_attempt(inputs[2]['project_dir'],third,request_sha256=third_binding.request_sha256)
    assert generated['status']=='generated'
    from lib.production_provenance import validate_attempt_provenance
    validate_attempt_provenance(inputs[2]['project_dir'],third,shot_id='entry',story_revision='story-1',expected_output=generated['output'])
    dispatch.resolve_attempt(inputs[2]['project_dir'],third,third_binding.request_sha256)
    unavailable=b.summarize_runtime_metrics(envelope)
    assert [r['attempt_id'] for r in unavailable['records']]==[first,second,third]
    assert unavailable['records'][2]['source_duration_status']=='unavailable'
    assert unavailable['records'][2]['observed_source_seconds'] is None
    assert unavailable['records'][2]['failure_categories']==['technical_transport']
    assert unavailable['max']['held_credits']=={'numerator':'9','denominator':'2'}
    before=read_existing_snapshot();candidate=b.next_runtime_attempt(envelope)
    assert candidate['occurrence']['occurrence_id']=='logical-4'
    assert before==read_existing_snapshot()
    assert len((governed[3]/'paid').read_text().splitlines())==3
    # Evidence tampering remains a hard failure, not another technical category.
    path=__import__('pathlib').Path(inputs[0]['output_path']);path.chmod(0o600);path.write_bytes(b'tampered-current-output')
    with pytest.raises((ValueError,OpenArtCLIError)):b.summarize_runtime_metrics(envelope)
