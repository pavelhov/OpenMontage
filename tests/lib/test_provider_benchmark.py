"""Offline fixture proof only; never provider certification or paid authorization."""
import copy
import json
from fractions import Fraction
import pytest
from lib import provider_benchmark as b


def sha(text): return b.digest(text)

def manifest():
    models={'turbo':'observed-turbo-id','max':'observed-max-id'}
    cases=[dict(id=c,semantics_sha256=sha(c),reference_sha256=sha(c+'ref')) for c in b.CASES]
    profiles={m:dict(exact_model_id=id,origin_pre_submit_sha256=sha(m),account_id_sha256=sha('account'),
        workspace_sha256=sha('workspace'),tier='observed-tier',cli_version='observed-version',
        form_sha256=sha(m+'form'),form_defaults_sha256=sha(m+'defaults'),credit_quantum='0.25') for m,id in models.items()}
    order=[]; deliveries=[]; seen=set()
    for i,c in enumerate(cases):
        labels=('turbo','max') if i%2==0 else ('max','turbo')
        for m in labels:
            oid=f'logical-{len(order)+1}'
            o=dict(occurrence_id=oid,case_id=c['id'],model=m,purpose='original' if m in seen else 'result_contract_qualification',
                   semantics_sha256=c['semantics_sha256'],reference_sha256=c['reference_sha256'],quoted_credits='1.25',ceiling_credits='1.5')
            for k in ('native_body_sha256','native_argv_sha256','native_controls_sha256','compiled_request_sha256','preparation_review_sha256','quote_sha256'):o[k]=sha(oid+k)
            order.append(o);seen.add(m)
            deliveries.append(dict(deliverable_id=oid+'delivery',model=m,case_id=c['id'],original_occurrence_ids=[oid],story_revision='story-v1'))
    return dict(version='draft-2',evidence_kind='fixture_only',benchmark_id='offline',models=models,cases=cases,
        profiles=profiles,settings=dict(duration_seconds=5,resolution='768p',submissions=12,
            order_policy='paired_adjacent_alternating_three_starts_each'),allowance=dict(credits='18',currency_unit='credits',observation_sha256=sha('observed allowance')),
        attempt_order=order,deliverables=deliveries)

def review(subject,duration='5'):
    return dict(version='2.0',evidence_kind='fixture_only',reviewer='Fixture reviewer',review_id='fixture-review',subject_sha256=subject,
        story_revision='story-v1',mode='synchronized_av',watched_full=True,listened_full=True,start_seconds=0,end_seconds=duration,
        dimensions={k:'pass' for k in ('transport','technical','visual','audio','story')},
        predicates=[dict(name=k,status='pass',severity='critical',evidence='Fixture named judgment') for k in sorted(b.REVIEW_PREDICATES)])

def records(value,count=12):
    out=[]
    for i,o in enumerate(value['attempt_order'][:count]):
        output=sha('actual output'+str(i))
        out.append(dict(evidence_kind='fixture_only',occurrence_id=o['occurrence_id'],attempt_id='runtime-uuid-'+str(i),case_id=o['case_id'],model=o['model'],
            status='complete',billing_status='known',charged_credits='1.25',held_credits=0,source_seconds='5',output_sha256=output,
            review=review(output),failure_category=None,corrective_video_generations=0))
    return out

def proof(value,remaining='18'):
    return dict(evidence_kind='fixture_only',approval_digest=b.approval_digest(value),ledger_sha256=sha('fixture ledger'),state='eligible',remaining_credits=remaining)

def resume(value,rows,**kw):
    binding=b.approval_digest(value)
    remaining=Fraction(value['allowance']['credits'])-sum((Fraction(r['charged_credits']) if r['billing_status']=='known' else Fraction(r['held_credits']) for r in rows),Fraction())
    ledger_proof=proof(value,{'numerator':remaining.numerator,'denominator':remaining.denominator})
    return b.next_resumable_attempt(value,rows,approved_digest=kw.pop('approved_digest',binding),current_digest=binding,
        fixture_resume_proof=kw.pop('fixture_resume_proof',ledger_proof),**kw)

def fraction(value):return Fraction(int(value['numerator']),int(value['denominator']))

def test_complete_whole_frozen_twelve_separate_runtime_ids():
    value=manifest();rows=records(value);b.validate_manifest(value)
    result=b.summarize_metrics(value,rows)
    assert result['comparison_complete'] is True
    assert result['comparison_conclusion'] is None
    assert result['live_certification_ready'] is False
    assert result['turbo']['attempted']==6
    assert fraction(result['turbo']['accepted_source_seconds'])==30
    assert fraction(result['turbo']['known_charged_credits'])==Fraction(15,2)
    assert result['turbo']['certified_original_deliverables']==0
    assert result['turbo']['fixture_certified_original_deliverables']==0
    json.dumps(result,allow_nan=False)
    assert resume(value,rows) is None


def test_repeated_case_pairs_rejected():
    value=manifest()
    for o in value['attempt_order']:o['case_id']='identity_action'
    with pytest.raises(b.BenchmarkError,match='exactly one pair'):b.validate_manifest(value)

@pytest.mark.parametrize('index',range(12))
@pytest.mark.parametrize('field',['native_body_sha256','native_argv_sha256','native_controls_sha256','compiled_request_sha256','preparation_review_sha256','quote_sha256'])
def test_all_per_occurrence_bindings_invalidate_exact_approval(index,field):
    value=manifest();frozen=b.approval_digest(value);value['attempt_order'][index][field]=sha('changed')
    assert b.approval_digest(value)!=frozen
    with pytest.raises(b.BenchmarkError,match='current exact approval'):resume(value,[],approved_digest=frozen)

@pytest.mark.parametrize('field',['semantics_sha256','reference_sha256'])
@pytest.mark.parametrize('index',range(12))
def test_each_occurrence_must_match_frozen_pair(index,field):
    value=manifest();value['attempt_order'][index][field]=sha('changed')
    with pytest.raises(b.BenchmarkError,match='paired case'):b.validate_manifest(value)

@pytest.mark.parametrize('field',['account_id_sha256','workspace_sha256','tier','cli_version','credit_quantum'])
def test_account_version_tier_workspace_quantum_parity(field):
    value=manifest();value['profiles']['max'][field]='0.5' if field=='credit_quantum' else sha('changed')
    with pytest.raises(b.BenchmarkError,match='paired account'):b.validate_manifest(value)

@pytest.mark.parametrize('bad',[True,0.1,float('nan'),-1,'NaN',{'numerator':1,'denominator':0}])
def test_credit_inputs_reject_inexact_negative_nonfinite_types(bad):
    value=manifest();value['allowance']['credits']=bad
    with pytest.raises(b.BenchmarkError):b.validate_manifest(value)


def test_fractional_observed_quantum_and_allowance_no_float_or_rounding():
    value=manifest();value['allowance']['credits']={'numerator':36,'denominator':2}
    value['profiles']['turbo']['credit_quantum']=value['profiles']['max']['credit_quantum']={'numerator':1,'denominator':4}
    b.validate_manifest(value)
    value['attempt_order'][0]['quoted_credits']='1.26'
    with pytest.raises(b.BenchmarkError,match='quantum'):b.validate_manifest(value)


def test_arbitrary_precision_sum_is_json_serializable_and_exact():
    value=manifest();big='12345678901234567890123456789012345678.25'
    for o in value['attempt_order']:o['quoted_credits']=o['ceiling_credits']=big
    value['allowance']['credits']=str(Fraction(big)*12).split('/')[0] if (Fraction(big)*12).denominator==1 else {'numerator':(Fraction(big)*12).numerator,'denominator':(Fraction(big)*12).denominator}
    rows=records(value)
    for r in rows:r['charged_credits']=big
    result=b.summarize_metrics(value,rows)
    assert fraction(result['turbo']['known_charged_credits'])==Fraction(big)*6
    assert fraction(result['turbo']['credits_per_accepted_second'])==Fraction(big)/5
    json.dumps(result,allow_nan=False)

@pytest.mark.parametrize('change',[{'model':'unknown'},{'accepted':True},{'corrective_video_generations':True},{'source_seconds':True},{'charged_credits':0.1},{'held_credits':-1},{'evidence_kind':'live'},{'failure_category':'other'},{'attempt_id':' '},{'status':'invented'}])
def test_every_metric_row_closed_known_typed(change):
    value=manifest();rows=records(value,1);rows[0].update(change)
    with pytest.raises(b.BenchmarkError):b.summarize_metrics(value,rows)

@pytest.mark.parametrize('kind',['duplicate_attempt','duplicate_occurrence','duplicate_output','wrong_case','hole','reorder','extra'])
def test_duplicates_source_reuse_order_and_unknown_occurrence_rejected(kind):
    value=manifest();rows=records(value,3)
    if kind=='duplicate_attempt':rows[1]['attempt_id']=rows[0]['attempt_id']
    elif kind=='duplicate_occurrence':rows[1]['occurrence_id']=rows[0]['occurrence_id']
    elif kind=='duplicate_output':rows[1]['output_sha256']=rows[0]['output_sha256'];rows[1]['review']=copy.deepcopy(rows[0]['review'])
    elif kind=='wrong_case':rows[1]['case_id']='continuity'
    elif kind=='hole':rows.pop(0)
    elif kind=='reorder':rows.reverse()
    else:rows=records(value)+[records(value,1)[0]]
    with pytest.raises(b.BenchmarkError):b.summarize_metrics(value,rows)

@pytest.mark.parametrize('kind',['speaker','endpoint','no_watch','no_listen','story','provisional','missing_av','duplicate_predicate','cosmetic','stale_bytes'])
def test_acceptance_requires_full_named_v2_av_story_facts(kind):
    value=manifest();rows=records(value,1);r=rows[0];v=r['review']
    if kind in ('speaker','endpoint'):next(p for p in v['predicates'] if p['name']==('speaker_source' if kind=='speaker' else 'completed_action'))['status']='fail'
    elif kind=='no_watch':v['watched_full']=False
    elif kind=='no_listen':v['listened_full']=False
    elif kind=='story':v['dimensions']['story']='fail'
    elif kind=='provisional':v['version']='1.0'
    elif kind=='missing_av':r['review']=None
    elif kind=='duplicate_predicate':v['predicates'].append(v['predicates'][0])
    elif kind=='cosmetic':v['predicates'][0]['severity']='cosmetic'
    else:v['subject_sha256']=sha('stale')
    if kind in ('speaker','endpoint','no_watch','no_listen','story','missing_av'):
        r['failure_category']={'speaker':'dialogue_speaker','endpoint':'action_endpoint',
            'story':'continuity'}.get(kind,'review_availability')
    if kind in ('provisional','duplicate_predicate','cosmetic','stale_bytes'):
        with pytest.raises(b.BenchmarkError):b.summarize_metrics(value,rows)
    else:assert b.summarize_metrics(value,rows)['turbo']['first_attempt_acceptance']['numerator']==0


def certificate(value,rows):
    output=sha('deliverybytes')
    return dict(evidence_kind='fixture_only',deliverable_id=value['deliverables'][0]['deliverable_id'],attempt_ids=[rows[0]['attempt_id']],
        output_sha256=output,provenance_sha256=sha('fixture provenance'),review=review(output))

def test_acceptance_is_separate_from_predeclared_delivery_certification():
    value=manifest();rows=records(value,1);cert=certificate(value,rows)
    result=b.summarize_metrics(value,rows,certifications=[cert])
    assert result['turbo']['first_attempt_acceptance']['numerator']==1
    assert result['turbo']['fixture_certified_original_deliverables']==1
    assert result['turbo']['certified_original_deliverables']==0
    assert result['turbo']['zero_corrective_delivery']['denominator']==6
    cert['attempt_ids']=['unknown-runtime-id']
    with pytest.raises(b.BenchmarkError):b.summarize_metrics(value,rows,certifications=[cert])

@pytest.mark.parametrize('kind',['duplicate','nonfull','provisional','wrong_original','wrong_story'])
def test_certification_cannot_reuse_or_bypass_original_full_review(kind):
    value=manifest();rows=records(value,2);cert=certificate(value,rows);certs=[cert]
    if kind=='duplicate':certs.append(cert)
    elif kind=='nonfull':cert['review']['listened_full']=False
    elif kind=='provisional':cert['review']['version']='1.0'
    elif kind=='wrong_original':cert['attempt_ids']=[rows[1]['attempt_id']]
    else:cert['review']['story_revision']='changed'
    with pytest.raises(b.BenchmarkError):b.summarize_metrics(value,rows,certifications=certs)


def test_unknown_charge_with_accepted_seconds_no_complete_ratio_known_and_held_visible():
    value=manifest();rows=records(value,4);rows[3].update(status='failed_terminal',review=None,failure_category='technical_transport',
        billing_status='uncertain',charged_credits=None,held_credits='1.5')
    result=b.summarize_metrics(value,rows)['turbo']
    assert result['credits_per_accepted_second'] is None
    assert result['economics_complete'] is False
    assert fraction(result['known_charged_credits'])==Fraction(5,4)
    assert fraction(result['held_credits'])==Fraction(3,2)
    assert fraction(result['accepted_source_seconds'])==5


def test_zero_accepted_seconds_and_partial_no_comparison_conclusion():
    value=manifest();rows=records(value,1);rows[0].update(review=None,failure_category='review_availability')
    result=b.summarize_metrics(value,rows)
    assert result['turbo']['credits_per_accepted_second'] is None
    assert result['comparison_complete'] is False
    assert result['comparison_conclusion'] is None

@pytest.mark.parametrize('state',['pending','running','unknown_acceptance'])
def test_uncertain_submission_pauses_frozen_order(state):
    value=manifest();rows=records(value,1);rows[0].update(status=state,review=None)
    assert resume(value,rows) is None


def test_closed_unknown_billing_can_continue_only_safe_fixture_remaining_allowance():
    value=manifest();rows=records(value,1);rows[0].update(status='failed_terminal',review=None,failure_category='technical_transport',billing_status='uncertain',charged_credits=None,held_credits='1.5')
    assert resume(value,rows,fixture_resume_proof=proof(value,'16.5'))==value['attempt_order'][1]
    assert resume(value,rows,fixture_resume_proof=proof(value,'0')) is None
    p=proof(value,'16.5');p['state']='blocked';assert resume(value,rows,fixture_resume_proof=p) is None
    p=proof(value);p['evidence_kind']='live'
    with pytest.raises(b.BenchmarkError):resume(value,rows,fixture_resume_proof=p)


def test_resume_prefix_no_holes_out_of_order_substitution_retry():
    value=manifest();rows=records(value,2)
    assert resume(value,rows)==value['attempt_order'][2]
    with pytest.raises(b.BenchmarkError):resume(value,rows[1:])
    with pytest.raises(b.BenchmarkError):resume(value,rows[::-1])
    with pytest.raises(b.BenchmarkError):resume(value,[rows[0],rows[0]])


def test_first_qualification_is_within_original_twelve_no_extra_or_adaptation():
    value=manifest();assert [o['purpose'] for o in value['attempt_order']].count('result_contract_qualification')==2
    value['attempt_order'][2]['purpose']='result_contract_qualification'
    with pytest.raises(b.BenchmarkError):b.validate_manifest(value)
    value=manifest();value['auto_adapt']=True
    with pytest.raises(b.BenchmarkError):b.validate_manifest(value)


def test_finite_json_and_no_live_boolean_authority():
    with pytest.raises(b.BenchmarkError):b.digest({'value':float('nan')})
    value=manifest();p=proof(value);p['blocker_cleared']=True
    with pytest.raises(b.BenchmarkError):resume(value,[],fixture_resume_proof=p)


def test_resume_fixture_ledger_cannot_inflate_remaining_observed_allowance():
    value=manifest();rows=records(value,1)
    with pytest.raises(b.BenchmarkError,match='remaining'):
        resume(value,rows,fixture_resume_proof=proof(value,'19'))


def test_actual_charge_must_use_frozen_observed_credit_quantum():
    value=manifest();rows=records(value,1);rows[0]['charged_credits']='1.26'
    result=b.summarize_metrics(value,rows)
    assert 'off_quantum' in result['billing_violations'][0]['reasons']
    assert fraction(result['turbo']['known_charged_credits'])==Fraction('1.26')


def test_records_cannot_continue_after_unresolved_submission_acceptance():
    value=manifest();rows=records(value,2);rows[0].update(status='unknown_acceptance',review=None)
    with pytest.raises(b.BenchmarkError,match='unresolved'):
        b.summarize_metrics(value,rows)


@pytest.mark.parametrize('charge,reasons',[
    ('2',['above_quote','above_ceiling']), ('1.3',['above_quote','off_quantum']),
    ('1.5',['above_quote'])])
def test_observed_billing_anomaly_preserves_full_truthful_report(charge,reasons):
    value=manifest();rows=records(value);rows[0]['charged_credits']=charge
    result=b.summarize_metrics(value,rows)
    assert fraction(result['turbo']['known_charged_credits'])==Fraction(charge)+Fraction('1.25')*5
    flag=result['billing_violations'][0]
    assert flag['occurrence_id']==rows[0]['occurrence_id']
    assert flag['attempt_id']==rows[0]['attempt_id'] and flag['model']=='turbo'
    assert fraction(flag['charged_credits'])==Fraction(charge)
    assert fraction(flag['quoted_credits'])==Fraction('1.25')
    assert fraction(flag['ceiling_credits'])==Fraction('1.5')
    assert fraction(flag['credit_quantum'])==Fraction('0.25')
    assert flag['reasons']==reasons
    assert result['turbo']['economics_complete'] is False
    assert result['turbo']['credits_per_accepted_second'] is None
    assert result['comparison_complete'] is False
    assert result['comparison_conclusion'] is None


@pytest.mark.parametrize('status',['failed_terminal','complete'])
def test_unaccepted_terminal_requires_actionable_failure_category(status):
    value=manifest();rows=records(value,1);rows[0].update(status=status,review=None)
    with pytest.raises(b.BenchmarkError,match='failure category'):
        b.summarize_metrics(value,rows)


def test_unknown_acceptance_with_known_zero_charge_has_explicit_unresolved_label():
    value=manifest();rows=records(value,1)
    rows[0].update(status='unknown_acceptance',review=None,charged_credits=0)
    result=b.summarize_metrics(value,rows)
    assert result['turbo']['unresolved_acceptance']['count']==1
    assert result['turbo']['unresolved_acceptance']['attempt_ids']==[rows[0]['attempt_id']]
    assert result['turbo']['unresolved_acceptance']['occurrence_ids']==[rows[0]['occurrence_id']]
    assert result['turbo']['job_states']['unknown_acceptance']['count']==1
    assert result['turbo']['failure_categories']['technical_transport']==0
    assert fraction(result['turbo']['known_charged_credits'])==0
    assert result['turbo']['outstanding_billing_holds']==0
    assert resume(value,rows) is None


@pytest.mark.parametrize('charge',['2','1.3','100'])
def test_billing_anomalies_quarantine_resume_even_with_inflated_eligible_proof(charge):
    value=manifest();rows=records(value,1);rows[0]['charged_credits']=charge
    assert resume(value,rows,fixture_resume_proof=proof(value,'999')) is None


def test_failed_speaker_cannot_be_hidden_as_unrelated_failure_category():
    value=manifest();rows=records(value,1);rows[0]['failure_category']='continuity'
    next(p for p in rows[0]['review']['predicates'] if p['name']=='speaker_source')['status']='fail'
    with pytest.raises(b.BenchmarkError,match='failure category'):
        b.summarize_metrics(value,rows)


def test_passing_complete_result_cannot_claim_failure_category():
    value=manifest();rows=records(value,1);rows[0]['failure_category']='identity'
    with pytest.raises(b.BenchmarkError,match='failure category'):
        b.summarize_metrics(value,rows)


@pytest.mark.parametrize('status',['pending','running','unknown_acceptance'])
def test_separate_job_state_labels_preserve_authoritative_fixture_zero_billing(status):
    value=manifest();rows=records(value,1);rows[0].update(status=status,review=None,charged_credits=0)
    result=b.summarize_metrics(value,rows)['turbo']
    assert result['job_states'][status]==dict(count=1,attempt_ids=[rows[0]['attempt_id']],occurrence_ids=[rows[0]['occurrence_id']])
    assert result['unresolved_acceptance']['count']==1
    assert result['first_attempt_acceptance']==dict(numerator=0,denominator=1,rate={'numerator':'0','denominator':'1'})
    assert sum(result['failure_categories'].values())==0
    assert result['outstanding_billing_holds']==0
    assert resume(value,rows) is None


@pytest.mark.parametrize('change',[{'charged_credits':None},{'held_credits':'0.25'},
                                  {'charged_credits':-1},{'charged_credits':True}])
def test_billing_structure_remains_hard_fail_despite_observed_anomaly_reporting(change):
    value=manifest();rows=records(value,1);rows[0].update(change)
    with pytest.raises(b.BenchmarkError):b.summarize_metrics(value,rows)


@pytest.mark.parametrize('status,category',[('failed_terminal','technical_transport'),('complete','review_availability')])
def test_explicit_terminal_failure_counts_keep_full_attempt_denominator(status,category):
    value=manifest();rows=records(value);rows[0].update(status=status,review=None,failure_category=category)
    result=b.summarize_metrics(value,rows)['turbo']
    assert result['first_attempt_acceptance']['numerator']==5
    assert result['first_attempt_acceptance']['denominator']==6
    assert result['failure_categories'][category]==1
    assert sum(result['failure_categories'].values())==1
    assert result['unresolved_acceptance']['count']==0


def test_negative_available_anomalous_debit_remains_known_and_quarantines_resume():
    value=manifest();rows=records(value,1);rows[0]['charged_credits']='100'
    result=b.summarize_metrics(value,rows)
    assert fraction(result['turbo']['known_charged_credits'])==100
    assert result['turbo']['outstanding_billing_holds']==0
    assert result['billing_quarantined'] is True
    assert resume(value,rows,fixture_resume_proof=proof(value,'0')) is None


def runtime_envelope(tmp_path):
    plan=manifest();plan['evidence_kind']='runtime'
    sources=[]
    for o in plan['attempt_order']:
        path=tmp_path/(o['occurrence_id']+'.json');path.write_text('{}')
        sources.append({'occurrence_id':o['occurrence_id'],'project_root':str(tmp_path),
            'inputs':{'path':str(path),'sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest()},'scope_occurrence_index':0})
    capture=tmp_path/'benchmark-approval.json'
    capture.write_text(json.dumps({'kind':'openart_paired_benchmark','plan':plan,'sources':sources}))
    return {'version':'runtime-1','evidence_kind':'runtime','plan':plan,'sources':sources,
        'approval':{'path':str(capture),'sha256':__import__('hashlib').sha256(capture.read_bytes()).hexdigest()}}


def test_runtime_envelope_closed_schema_and_fixture_separation(tmp_path):
    value=runtime_envelope(tmp_path)
    b._validate(value,'runtime_manifest');b._runtime_plan(value['plan'])
    with pytest.raises(b.BenchmarkError):b.validate_manifest(value['plan'])
    value['accepted']=True
    with pytest.raises(b.BenchmarkError):b._validate(value,'runtime_manifest')


@pytest.mark.parametrize('mutation',['approval_bytes','changed_plan','fixture_plan','hole','eligibility'])
def test_runtime_envelope_rejects_asserted_authority_before_any_project_read(tmp_path,mutation):
    value=runtime_envelope(tmp_path)
    if mutation=='approval_bytes':(tmp_path/'benchmark-approval.json').write_text('{}')
    elif mutation=='changed_plan':value['plan']['attempt_order'][0]['native_body_sha256']=sha('replacement')
    elif mutation=='fixture_plan':value['plan']['evidence_kind']='fixture_only'
    elif mutation=='hole':value['sources'][0]['occurrence_id']='logical-2'
    else:value['eligible']=True
    with pytest.raises(b.BenchmarkError):b.validate_runtime_manifest(value)

# The shared production fixture runs actual governed original provenance; its
# semantic judgments and media bytes are explicitly synthetic test evidence.
from tests.lib.test_production_review import production


def test_runtime_av_requires_actual_current_full_v2_gate(production,monkeypatch):
    from lib import production_review
    root,review,contract,selection=production
    monkeypatch.setattr(production_review,'probe_master',lambda path:{'duration_seconds':8})
    path=root/'artifacts/final_review.json';path.write_text(json.dumps(review))
    assert b._runtime_av_acceptance(root,selection['attempt_id'],selection['output']['sha256']) is True
    review['av_review']['listened_full']=False;path.write_text(json.dumps(review))
    assert b._runtime_av_acceptance(root,selection['attempt_id'],selection['output']['sha256']) is False
    review['av_review']['listened_full']=True;path.write_text(json.dumps(review))
    (root/'shot.bin').write_bytes(b'changed actual source bytes')
    assert b._runtime_av_acceptance(root,selection['attempt_id'],selection['output']['sha256']) is False


def test_runtime_failure_categories_derive_actual_bound_rejection_predicates(production):
    from lib.production_execution import record_rejection
    root,full_review,contract,selection=production
    review=copy.deepcopy(selection['review']);review['status']='fail';review['subject_sha256']=selection['output']['sha256']
    for predicate in review['predicates']:
        if predicate['name'] in ('speaker_source','completed_action'):predicate['status']='fail'
    record_rejection(root,selection['attempt_id'],review)
    categories,receipts=b._runtime_failures(root,selection['attempt_id'],contract['story_revision'],selection['output']['sha256'])
    assert categories==['action_endpoint','dialogue_speaker']
    assert len(receipts)==1
    path=__import__('pathlib').Path(receipts[0]['path']);path.chmod(0o600)
    review['subject_sha256']='0'*64;path.write_text(json.dumps(review))
    with pytest.raises(b.BenchmarkError,match='rejection review'):b._runtime_failures(root,selection['attempt_id'],contract['story_revision'],selection['output']['sha256'])
