"""Exact paired benchmark contracts and read-only production runtime binding.

Fixtures remain explicitly synthetic. Runtime facts are reread from retained
strict authority, current original attempts, full AV review and private ledger.
Neither reporting nor resume eligibility authorizes dispatch.
"""
from __future__ import annotations
import hashlib
import json
from fractions import Fraction
from typing import Any
from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES

CASES = ('identity_action','exact_dialogue','speaker_handoff','completed_prop_action','continuity','payoff_reference')
FAILURES = ('capability','preparation','identity','dialogue_speaker','action_endpoint','continuity','technical_transport','review_availability')
REVIEW_PREDICATES = PROJECT_PREDICATES | SHOT_PREDICATES | {'exact_dialogue','continuity'}

class BenchmarkError(ValueError):
    """The frozen offline contract or its fixture evidence is invalid."""

def digest(value: Any) -> str:
    try:
        raw=json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode()
    except (TypeError,ValueError) as exc:
        raise BenchmarkError('finite JSON required') from exc
    return hashlib.sha256(raw).hexdigest()

def _strict_numbers(value):
    # JSON Schema regards 1.0 as an integer; this contract never admits floats.
    if isinstance(value,float): raise BenchmarkError('exact numbers required; floats prohibited')
    if isinstance(value,dict):
        for item in value.values(): _strict_numbers(item)
    elif isinstance(value,list):
        for item in value: _strict_numbers(item)

def _validate(value, definition=None):
    _strict_numbers(value)
    schema=load_schema('provider_benchmark')
    if definition: schema={'$defs':schema['$defs'],'$ref':'#/$defs/'+definition}
    errors=list(Draft202012Validator(schema).iter_errors(value))
    if errors: raise BenchmarkError('; '.join(f'{e.json_path}: {e.message}' for e in errors))

def _exact(value):
    if type(value) is int: result=Fraction(value)
    elif isinstance(value,str):
        import re
        if not re.fullmatch(r'\d+(\.\d+)?',value): raise BenchmarkError('nonnegative exact decimal required')
        result=Fraction(value)
    elif isinstance(value,dict) and set(value)=={'numerator','denominator'} and type(value['numerator']) is int and type(value['denominator']) is int and value['denominator']>0:
        result=Fraction(value['numerator'],value['denominator'])
    else: raise BenchmarkError('exact integer, decimal string or rational required')
    if result<0: raise BenchmarkError('negative amount prohibited')
    return result

def _ratio(value):
    return {'numerator':str(value.numerator),'denominator':str(value.denominator)}

def validate_manifest(manifest):
    """Validate all twelve logical occurrences, paired semantics and exact budgets."""
    _validate(manifest)
    models=manifest['models']; profiles=manifest['profiles']; order=manifest['attempt_order']
    if models['turbo']==models['max'] or any(models[m].strip().lower() in ('turbo','max','h3 turbo','h3 max') for m in models):
        raise BenchmarkError('distinct exact observed model IDs required')
    for m,p in profiles.items():
        if p['exact_model_id']!=models[m]: raise BenchmarkError('profile exact observed model ID differs')
        if _exact(p['credit_quantum'])<=0: raise BenchmarkError('positive observed credit quantum required')
    for key in ('account_id_sha256','workspace_sha256','tier','cli_version','credit_quantum'):
        if profiles['turbo'][key]!=profiles['max'][key]: raise BenchmarkError('paired account/version/tier/workspace/quantum differs')
    cases={c['id']:c for c in manifest['cases']}
    if len(cases)!=6 or set(cases)!=set(CASES): raise BenchmarkError('six distinct cases required')
    pair_ids=[]; starts=[]; first=set()
    for i in range(0,12,2):
        pair=order[i:i+2]
        if pair[0]['case_id']!=pair[1]['case_id'] or {x['model'] for x in pair}!={'turbo','max'}:
            raise BenchmarkError('each case must be paired adjacently')
        pair_ids.append(pair[0]['case_id']); starts.append(pair[0]['model'])
    if len(set(pair_ids))!=6: raise BenchmarkError('each six-case identity must occur in exactly one pair')
    if starts.count('turbo')!=3 or starts.count('max')!=3 or any(starts[i]==starts[i+1] for i in range(5)):
        raise BenchmarkError('balanced alternating starts required')
    if len({o['occurrence_id'] for o in order})!=12: raise BenchmarkError('unique logical occurrence IDs required')
    for o in order:
        case=cases[o['case_id']]; m=o['model']; quantum=_exact(profiles[m]['credit_quantum'])
        for key in ('semantics_sha256','reference_sha256'):
            if o[key]!=case[key]: raise BenchmarkError('per-occurrence paired case binding differs')
        expected='original' if m in first else 'result_contract_qualification'
        if o['purpose']!=expected: raise BenchmarkError('first qualification consumes first model occurrence within twelve')
        first.add(m)
        quote=_exact(o['quoted_credits']); ceiling=_exact(o['ceiling_credits'])
        if quote>ceiling or (quote/quantum).denominator!=1 or (ceiling/quantum).denominator!=1:
            raise BenchmarkError('quote/ceiling must fit observed exact quantum')
    if sum((_exact(o['ceiling_credits']) for o in order),Fraction())>_exact(manifest['allowance']['credits']):
        raise BenchmarkError('twelve ceilings exceed observed allowance')
    deliveries=manifest['deliverables']
    if len({d['deliverable_id'] for d in deliveries})!=12 or {(d['case_id'],d['model']) for d in deliveries}!={(c,m) for c in CASES for m in models}:
        raise BenchmarkError('twelve unique paired deliverables required')
    occurrences={o['occurrence_id']:o for o in order}
    for d in deliveries:
        ids=d['original_occurrence_ids']
        if len(set(ids))!=len(ids) or any(i not in occurrences or occurrences[i]['model']!=d['model'] or occurrences[i]['case_id']!=d['case_id'] for i in ids):
            raise BenchmarkError('deliverable must bind its predeclared original occurrences')

def approval_digest(manifest):
    validate_manifest(manifest)
    return digest(manifest)

def _review_pass(review, *, subject, story, duration, predicates):
    if review is None: return False
    _validate(review,'review')
    names=[p['name'] for p in review['predicates']]
    if len(names)!=len(set(names)) or set(names)!=set(predicates): raise BenchmarkError('exact unique named review predicates required')
    if review['subject_sha256']!=subject or review['story_revision']!=story:
        raise BenchmarkError('review bytes/story binding differs')
    if _exact(review['start_seconds'])!=0 or _exact(review['end_seconds'])!=duration:
        raise BenchmarkError('full AV timing coverage required')
    return review['watched_full'] is True and review['listened_full'] is True and all(v=='pass' for v in review['dimensions'].values()) and all(p['status']=='pass' for p in review['predicates'])

def _review_failure_categories(review):
    """Declared failed checks constrain the accountable author's primary category."""
    if review is None or not review['watched_full'] or not review['listened_full']:
        return {'review_availability'}
    categories=set()
    for p in review['predicates']:
        if p['status']=='pass': continue
        name=p['name']
        if name in ('speaker_source','exact_dialogue'): categories.add('dialogue_speaker')
        elif name in ('completed_action','possession','transformation','payoff'): categories.add('action_endpoint')
        elif name=='continuity': categories.add('continuity')
        else: categories.add('identity')
    for dimension,status in review['dimensions'].items():
        if status=='pass': continue
        if dimension in ('transport','technical'): categories.add('technical_transport')
        elif dimension=='audio': categories.update(('dialogue_speaker','technical_transport'))
        else: categories.update(('identity','dialogue_speaker','action_endpoint','continuity'))
    return categories

def _billing_violations(manifest,records):
    """Observed debits remain facts even when they breach the frozen envelope."""
    flags=[]
    for o,r in zip(manifest['attempt_order'],records):
        if r['billing_status']!='known': continue
        charge=r['charged_credits'] if isinstance(r['charged_credits'],Fraction) else _exact(r['charged_credits']); quote=_exact(o['quoted_credits'])
        ceiling=_exact(o['ceiling_credits']); quantum=_exact(manifest['profiles'][r['model']]['credit_quantum'])
        reasons=[]
        if charge>quote: reasons.append('above_quote')
        if charge>ceiling: reasons.append('above_ceiling')
        if (charge/quantum).denominator!=1: reasons.append('off_quantum')
        if reasons:
            flags.append({'occurrence_id':r['occurrence_id'],'attempt_id':r['attempt_id'],'model':r['model'],
                'charged_credits':_ratio(charge),'quoted_credits':_ratio(quote),
                'ceiling_credits':_ratio(ceiling),'credit_quantum':_ratio(quantum),'reasons':reasons})
    return flags

def _record_labels(rows):
    return {'count':len(rows),'attempt_ids':[r['attempt_id'] for r in rows],
            'occurrence_ids':[r['occurrence_id'] for r in rows]}

def validate_fixture_records(manifest,records):
    """Closed fixture facts in frozen contiguous order; runtime UUIDs stay distinct."""
    validate_manifest(manifest)
    if not isinstance(records,list) or len(records)>12: raise BenchmarkError('at most twelve fixture records required')
    ids=set(); outputs=set(); accepted={}; deliveries={(d['case_id'],d['model']):d for d in manifest['deliverables']}
    for index,r in enumerate(records):
        _validate(r,'fixture_record')
        if index<len(records)-1 and r['status'] in ('pending','running','unknown_acceptance'):
            raise BenchmarkError('records cannot continue after unresolved submission acceptance')
        o=manifest['attempt_order'][index]
        if any(r[k]!=o[k] for k in ('occurrence_id','model','case_id')): raise BenchmarkError('records must match contiguous frozen order without holes')
        if r['attempt_id'] in ids: raise BenchmarkError('duplicate runtime attempt/source seconds')
        ids.add(r['attempt_id'])
        if r['billing_status']=='known':
            if r['charged_credits'] is None or _exact(r['held_credits'])!=0: raise BenchmarkError('known billing requires exact charge and no hold')
        elif r['charged_credits'] is not None or _exact(r['held_credits'])!=_exact(o['ceiling_credits']):
            raise BenchmarkError('unknown billing must retain exact frozen ceiling hold')
        seconds=_exact(r['source_seconds']); duration=Fraction(manifest['settings']['duration_seconds'])
        if seconds>duration: raise BenchmarkError('source seconds exceed frozen source duration')
        passes=_review_pass(r['review'],subject=r['output_sha256'],story=deliveries[(r['case_id'],r['model'])]['story_revision'],duration=seconds,predicates=REVIEW_PREDICATES)
        passing_result=r['status']=='complete' and r['output_sha256'] is not None and seconds>0 and passes
        if passing_result and r['failure_category'] is not None:
            raise BenchmarkError('passing complete result must have no failure category')
        is_accepted=passing_result and r['failure_category'] is None
        if r['status'] in ('complete','failed_terminal') and not is_accepted:
            if r['failure_category'] is None:
                raise BenchmarkError('unaccepted terminal result requires an actionable failure category')
            if r['status']=='complete' and r['failure_category'] not in _review_failure_categories(r['review']):
                raise BenchmarkError('failure category inconsistent with declared failed AV/story checks')
        if r['status']!='complete' and r['review'] is not None: raise BenchmarkError('noncomplete records cannot supply acceptance review')
        if is_accepted:
            if r['output_sha256'] in outputs: raise BenchmarkError('accepted output source reused')
            outputs.add(r['output_sha256'])
        accepted[r['attempt_id']]=is_accepted
    return accepted

def next_resumable_attempt(manifest,records,*,approved_digest,current_digest,fixture_resume_proof):
    """Offline selector only; caller facts NEVER establish live ledger eligibility."""
    validate_fixture_records(manifest,records); _validate(fixture_resume_proof,'fixture_resume_proof')
    binding=approval_digest(manifest)
    if approved_digest!=binding or current_digest!=binding or fixture_resume_proof['approval_digest']!=binding:
        raise BenchmarkError('current exact approval bindings required')
    if _billing_violations(manifest,records): return None
    spent_or_held=sum((_exact(r['charged_credits']) if r['billing_status']=='known' else _exact(r['held_credits']) for r in records),Fraction())
    available=_exact(manifest['allowance']['credits'])-spent_or_held
    if available<0: return None
    if _exact(fixture_resume_proof['remaining_credits'])>available:
        raise BenchmarkError('fixture remaining allowance exceeds observed allowance after charges/holds')
    if fixture_resume_proof['state']!='eligible': return None
    if any(r['status'] in ('pending','running','unknown_acceptance') for r in records): return None
    remaining=manifest['attempt_order'][len(records):]
    needed=sum((_exact(o['ceiling_credits']) for o in remaining),Fraction())
    if _exact(fixture_resume_proof['remaining_credits'])<needed: return None
    return remaining[0] if remaining else None

def summarize_metrics(manifest,records,*,certifications=()):
    """JSON-serializable exact fixture metrics; no live certified-delivery claim."""
    accepted=validate_fixture_records(manifest,records)
    if not isinstance(certifications,(list,tuple)): raise BenchmarkError('fixture certifications required')
    declared={d['deliverable_id']:d for d in manifest['deliverables']}; by_id={r['attempt_id']:r for r in records}; certified=set()
    for cert in certifications:
        _validate(cert,'fixture_certification')
        did=cert['deliverable_id']
        if did not in declared or did in certified: raise BenchmarkError('unknown/duplicate deliverable certification')
        d=declared[did]; ids=cert['attempt_ids']
        if len(set(ids))!=len(ids) or any(i not in by_id or not accepted[i] for i in ids): raise BenchmarkError('certification requires accepted original runtime attempts')
        if [by_id[i]['occurrence_id'] for i in ids]!=d['original_occurrence_ids']: raise BenchmarkError('certification original occurrence binding differs')
        duration=sum((_exact(by_id[i]['source_seconds']) for i in ids),Fraction())
        if not _review_pass(cert['review'],subject=cert['output_sha256'],story=d['story_revision'],duration=duration,predicates=REVIEW_PREDICATES):
            raise BenchmarkError('certification requires full passing v2 AV/story review')
        certified.add(did)
    violations=_billing_violations(manifest,records)
    out={'evidence_kind':'fixture_only','live_certification_ready':False,
         'billing_violations':violations,'billing_quarantined':bool(violations)}
    for m in manifest['models']:
        rows=[r for r in records if r['model']==m]; good=[r for r in rows if accepted[r['attempt_id']]]
        seconds=sum((_exact(r['source_seconds']) for r in good),Fraction())
        known=sum((_exact(r['charged_credits']) for r in rows if r['billing_status']=='known'),Fraction())
        held=sum((_exact(r['held_credits']) for r in rows),Fraction())
        unknown=any(r['billing_status']=='uncertain' for r in rows)
        model_flags=[v for v in violations if v['model']==m]
        unresolved=[r for r in rows if r['status'] in ('pending','running','unknown_acceptance')]
        ncert=sum(d['deliverable_id'] in certified for d in manifest['deliverables'] if d['model']==m)
        complete=len(rows)==6 and not unresolved and not violations
        out[m]={'attempted':len(rows),'first_attempt_acceptance':{'numerator':len(good),'denominator':len(rows),'rate':_ratio(Fraction(len(good),len(rows))) if rows else None},
            'certified_original_deliverables':0,'fixture_certified_original_deliverables':ncert,'predeclared_deliverables':6,
            'zero_corrective_delivery':{'numerator':ncert,'denominator':6,'rate':_ratio(Fraction(ncert,6))},
            'known_charged_credits':_ratio(known),'held_credits':_ratio(held),'outstanding_billing_holds':sum(r['billing_status']=='uncertain' for r in rows),
            'economics_complete':complete and not unknown,'accepted_source_seconds':_ratio(seconds),
            'credits_per_accepted_second':_ratio(known/seconds) if seconds and not unknown and not model_flags else None,
            'billing_violations':model_flags,'unresolved_acceptance':_record_labels(unresolved),
            'job_states':{s:_record_labels([r for r in rows if r['status']==s]) for s in ('pending','running','unknown_acceptance')},
            'comparison_complete':complete,'failure_categories':{f:sum(r['status'] in ('complete','failed_terminal') and r['failure_category']==f for r in rows) for f in FAILURES}}
    out['comparison_complete']=all(out[m]['comparison_complete'] for m in manifest['models'])
    out['comparison_conclusion']=None
    return out

# Runtime envelopes carry references, never caller-authored result/billing facts.
# Their plan is the same arithmetic contract, with a separate runtime label.
def _read_bound(record, *, root=None):
    from pathlib import Path
    path=Path(record['path']).resolve()
    if root is not None and not path.is_relative_to(root):
        raise BenchmarkError('runtime evidence escapes project')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:
        raise BenchmarkError('runtime evidence bytes changed')
    return json.loads(raw)


def _runtime_plan(plan):
    if plan.get('evidence_kind')!='runtime':
        raise BenchmarkError('runtime plan required; fixture facts cannot bind runtime')
    import copy
    fixture=copy.deepcopy(plan); fixture['evidence_kind']='fixture_only'
    validate_manifest(fixture)


def _runtime_source(plan, source):
    """Reread actual strict scopes, reviewed preparation and retained raw quote."""
    from pathlib import Path
    from uuid import uuid5, NAMESPACE_URL
    from lib import production_execution as execution, production_request as preparation
    from lib import openart_jobs as jobs, openart_credit as credit
    root=Path(source['project_root']).resolve()
    if str(root)!=source['project_root']: raise BenchmarkError('canonical project root required')
    inputs=_read_bound(source['inputs'],root=root)
    if Path(inputs['project_dir']).resolve()!=root: raise BenchmarkError('runtime inputs project differs')
    marker=execution._read(root/'project.json')
    governance=marker.get('governance',{})
    if governance.get('mode')!='strict' or governance.get('version')!='1.0':
        raise BenchmarkError('runtime requires current strict enrollment')
    # Auto-continue is a different envelope and cannot enlarge this benchmark.
    decisions_path=execution._artifact_path(root,'decision_log.json')
    decisions=execution._read(decisions_path) if decisions_path.exists() else {}
    policy_decisions=[d for d in decisions.get('decisions',[]) if d.get('category')=='approval_policy' and d.get('subject')=='Production autonomy policy']
    if governance.get('auto_continue') or marker.get('auto_continue') or (policy_decisions and str(policy_decisions[-1].get('selected','')).startswith('auto_continue:')):
        raise BenchmarkError('active Auto-continue prohibited in frozen benchmark')
    context=inputs['governance']; scopes=execution._read(root/'production_scopes.json')
    matches=[s for s in scopes.get('scopes',[]) if s.get('id')==context['scope_id']]
    if len(matches)!=1: raise BenchmarkError('exact current strict scope required')
    scope=matches[0]
    if scope.get('derived_from_policy') or scope.get('phase')!='first_pass': raise BenchmarkError('corrective/substituted scope prohibited')
    contract=execution.load_shot_contract(root)
    if scope.get('approval_plan_sha256')!=execution.approval_plan_digest(contract):
        raise BenchmarkError('current strict scope approval plan changed')
    profile=jobs.load_qualification(model=inputs.get('model'),mode=inputs.get('mode'),require='pre_submit')
    if profile.get('source')!='real': raise BenchmarkError('fixture/unqualified creative profile cannot bind runtime')
    native=jobs.prepare_native_request(credit._controls(inputs),profile)
    prepared=preparation.validate_preparation(inputs,native,profile)
    packet=preparation.source_packet(root,context['shot_id'])
    quote=credit.get_retained_quote(inputs,profile,inputs['credit_quote_id'])
    proof=credit._load(inputs['credit_quote_id'])
    authority=execution._read(execution._artifact_path(root,'credit_authorization-'+inputs['credit_authorization_id']+'.json'))
    request_sha=execution.planned_request_digest(inputs,project_dir=root)
    # An unreserved UUID validates approved terms but never creates a reservation.
    ident=str(uuid5(NAMESPACE_URL,'openmontage-benchmark:'+str(root)+':'+source['occurrence_id']))
    approved=credit.validate_credit_authorization(root,authority,scope=scope,marker=marker,inputs=inputs,
        profile=profile,quote_id=inputs['credit_quote_id'],request_sha256=request_sha,
        occurrence_index=source['scope_occurrence_index'],attempt_id=ident)
    o=next(o for o in plan['attempt_order'] if o['occurrence_id']==source['occurrence_id'])
    delivery=next(d for d in plan['deliverables'] if (d['case_id'],d['model'])==(o['case_id'],o['model']))
    if delivery['story_revision']!=marker['story_revision']:
        raise BenchmarkError('runtime source story differs from frozen predeclared deliverable')
    observed={'native_body_sha256':native['native_body_sha256'],'native_argv_sha256':native['native_argv_sha256'],
        'native_controls_sha256':native['native_controls_sha256'],'compiled_request_sha256':prepared['compiled_sha256'],
        'preparation_review_sha256':prepared['review_sha256'],'quote_sha256':quote['quote_sha256'],
        'semantics_sha256':digest([{k:v for k,v in row.items() if k!='occurrence_id'} for row in packet['occurrences']]),
        'reference_sha256':digest([{k:r[k] for k in ('id','role','cast_ids','sha256')} for r in packet['binding']['references']])}
    if any(o[k]!=v for k,v in observed.items()): raise BenchmarkError('runtime native/preparation/semantic/reference/quote plan differs')
    p=plan['profiles'][o['model']]; terms=proof['terms']
    actual={'exact_model_id':profile['model'],'origin_pre_submit_sha256':native['profile_sha256'],
        'account_id_sha256':profile['account_id_sha256'],'workspace_sha256':hashlib.sha256(terms['workspace'].encode()).hexdigest(),
        'tier':profile['tier'],'cli_version':profile['cli_version'],'form_sha256':profile['form_sha256'],
        'form_defaults_sha256':native['form_defaults_sha256']}
    if any(p[k]!=v for k,v in actual.items()) or _exact(p['credit_quantum'])!=_exact(terms['quantum']):
        raise BenchmarkError('observed creative profile/account/workspace/version/tier/form/defaults/quantum differs')
    b=approved.binding
    if approved.purpose!=('generation' if o['purpose']=='original' else o['purpose']) or _exact(b.quote)!=_exact(o['quoted_credits']) or _exact(b.ceiling)!=_exact(o['ceiling_credits']) \
            or _exact(b.allowance)!=_exact(plan['allowance']['credits']):
        raise BenchmarkError('runtime approved purpose/quote/ceiling/allowance differs')
    if inputs.get('duration')!=plan['settings']['duration_seconds'] or inputs.get('resolution')!=plan['settings']['resolution']:
        raise BenchmarkError('runtime duration/resolution differs')
    return {'root':root,'inputs':inputs,'profile':profile,'native':native,'binding':b,'scope':scope,
            'marker':marker,'authorization':authority,'quote':quote,'quote_proof':proof,'preparation':prepared}


def validate_runtime_manifest(manifest):
    """Read-only validation. No CLI, initialization, dispatch or certification."""
    _validate(manifest,'runtime_manifest'); _runtime_plan(manifest['plan'])
    sources=manifest['sources']; order=manifest['plan']['attempt_order']
    if [s['occurrence_id'] for s in sources]!=[o['occurrence_id'] for o in order]:
        raise BenchmarkError('runtime authority must freeze all twelve contiguous occurrences')
    terms={'kind':'openart_paired_benchmark','plan':manifest['plan'],'sources':sources}
    if _read_bound(manifest['approval'])!=terms:
        raise BenchmarkError('separately retained exact benchmark approval required')
    checked=[_runtime_source(manifest['plan'],s) for s in sources]
    observation=digest([{'account_receipt':c['quote_proof']['account_receipt'],'allowance_id':c['binding'].allowance_id,'allowance':c['binding'].allowance} for c in checked])
    if manifest['plan']['allowance']['observation_sha256']!=observation:
        raise BenchmarkError('runtime allowance observation must bind actual retained account receipts and approved allowance')
    if len({c['binding'].allowance_id for c in checked})!=1:
        raise BenchmarkError('runtime twelve originals require one shared immutable allowance ID')
    identities=[(str(c['root']),c['binding'].authorization_occurrence) for c in checked]
    if len(set(identities))!=12: raise BenchmarkError('runtime scope occurrence reused')
    return checked


def bind_runtime_manifest(plan, sources, approval):
    """Construct a verified envelope from retained sources and exact approval.

    Does not create approval, accept fixture authority, write evidence or reserve.
    The approval is a retained JSON capture matching kind/plan/sources exactly.
    """
    import copy
    manifest=copy.deepcopy({'version':'runtime-1','evidence_kind':'runtime','plan':plan,'sources':sources,'approval':approval})
    validate_runtime_manifest(manifest)
    return manifest



def _runtime_av_acceptance(root, attempt_id, output_sha256):
    """Current full production AV gate, independent of deliverable counting."""
    from lib.production_review import validate_final_review
    path=root/'artifacts/final_review.json'
    if not path.is_file(): return False
    raw=path.read_bytes(); review=json.loads(raw)
    passed=validate_final_review(root,review)['eligible'] and any(
        scene['attempt_id']==attempt_id and scene['output_sha256']==output_sha256 for scene in review.get('scenes',[]))
    if path.read_bytes()!=raw: raise BenchmarkError('current AV review changed during validation')
    return bool(passed)



def _runtime_source_measurement(path, planned_seconds):
    """Observe real duration independently from full AV acceptance."""
    from lib.production_review import probe_master, TIMING_TOLERANCE
    measured=Fraction(str(probe_master(path)['duration_seconds']))
    planned=Fraction(planned_seconds)
    eligible=measured>0 and abs(measured-planned)<=Fraction(str(TIMING_TOLERANCE))
    return measured, min(measured,planned) if eligible else Fraction(), eligible


def _runtime_source_seconds(path, planned_seconds):
    measured,seconds,eligible=_runtime_source_measurement(path,planned_seconds)
    if not eligible:
        raise BenchmarkError('actual source duration differs beyond qualified timing tolerance')
    return seconds  # Encoding padding never earns accepted seconds.

def _runtime_failures(root, attempt_id, story, output):
    """Classify retained named failed checks, never an asserted accepted flag."""
    categories=set(); receipts=[]
    if output is None: return [],receipts
    schema=load_schema('shot_contract')
    schema={'$defs':schema['$defs'],'$ref':'#/$defs/review'}
    directory=root/'production_attempts'/attempt_id/'rejections'
    for path in sorted(directory.glob('*.json')):
        raw=path.read_bytes(); review=json.loads(raw)
        if list(Draft202012Validator(schema).iter_errors(review)) or review.get('status')!='fail'                 or review.get('subject_sha256')!=output or review.get('story_revision')!=story:
            raise BenchmarkError('retained rejection review bytes/story/output binding differs')
        for p in review['predicates']:
            if p['status']=='pass': continue
            name=p['name']
            if name in ('speaker_source','exact_dialogue'): categories.add('dialogue_speaker')
            elif name in ('completed_action','possession','transformation','payoff'): categories.add('action_endpoint')
            elif name=='continuity': categories.add('continuity')
            elif name=='outgoing_frame': categories.add('technical_transport')
            else: categories.add('identity')
        receipts.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()})
    return sorted(categories),receipts

def _runtime_rows(manifest, checked, snapshot):
    from dataclasses import asdict, replace
    from lib import openart_dispatch as dispatch, production_execution as execution
    from lib import production_provenance as provenance, production_request as preparation
    from lib.provider_credit_ledger import Binding
    from lib.shot_contract import selection_digest, UPSTREAM_PREDICATES, file_sha256
    rows=[]; seen=set(); hole=False; unresolved=False
    for source,c,o in zip(manifest['sources'],checked,manifest['plan']['attempt_order']):
        b=c['binding']
        candidates=[]
        for r in snapshot['reservations']:
            rb=Binding(**json.loads(r['binding_json'])); rb.validate()
            if (rb.project_root,rb.authorization_occurrence)==(str(c['root']),b.authorization_occurrence): candidates.append((r,rb))
        shot=c['inputs']['governance']['shot_id']
        journals=[a for a in execution._attempts(c['root']) if a.get('shot_id')==shot and a.get('media_kind')=='motion']
        candidate_ids={rb.attempt_id for _,rb in candidates}
        if any(a['attempt_id'] not in candidate_ids for a in journals):
            raise BenchmarkError('unplanned corrective/substituted original journal prohibited')
        if not candidates: hole=True;continue
        if hole or unresolved or len(candidates)!=1: raise BenchmarkError('runtime attempts violate contiguous original order')
        row,rb=candidates[0]
        if replace(rb,attempt_id=b.attempt_id)!=b: raise BenchmarkError('actual reservation Binding differs from frozen runtime authority')
        if rb.attempt_id in seen: raise BenchmarkError('runtime attempt reused')
        seen.add(rb.attempt_id)
        dm,db,frozen=dispatch._manifest(rb.attempt_id)
        if db!=rb or dm['purpose']!=('generation' if o['purpose']=='original' else o['purpose']): raise BenchmarkError('private dispatch origin differs')
        request_path=c['root']/'production_attempts'/rb.attempt_id/'request.json'
        if row['slot_state']=='prepared' or (row['slot_state']=='no-dispatch' and not any(j['attempt_id']==rb.attempt_id for j in snapshot['ready_journals'])):
            request=dm['journal_records']['request.json']
            if request_path.exists() and json.loads(request_path.read_bytes())!=request:
                raise BenchmarkError('prepared original journal differs from private intent')
        else:
            request,_=dispatch._ready_journal(rb,dm)
        preparation.validate_frozen_preparation(request,frozen,c['root'])
        if frozen['profile'].get('source')!='real' or frozen['native']!=c['native']:
            raise BenchmarkError('original observed creative request differs')
        selected=execution.load_selected_attempts(c['root']); shot=c['inputs']['governance']['shot_id']
        selection=selected.get(shot); accepted=False; output=None;seconds=Fraction();review_hash=None;observed_seconds=None
        duration_status=None;duration_reason=None
        if selection and selection.get('attempt_id')!=rb.attempt_id: raise BenchmarkError('runtime selected substitute/corrective attempt prohibited')
        if selection:
            review=selection['review']
            provenance.validate_attempt_provenance(c['root'],rb.attempt_id,shot_id=shot,
                story_revision=c['marker']['story_revision'],expected_output=selection['output'])
            if review.get('status')=='pass' and review.get('story_revision')==c['marker']['story_revision'] \
                    and review.get('subject_sha256')==selection_digest(selection):
                predicates=review['predicates']; names=[p['name'] for p in predicates]
                accepted=len(names)==len(set(names)) and UPSTREAM_PREDICATES.issubset(names) and all(p['status']=='pass' for p in predicates)
            # A semantic selection is not evidence of synchronized viewing/listening.
            if accepted:
                accepted=_runtime_av_acceptance(c['root'],rb.attempt_id,selection['output']['sha256'])
            output=selection['output']['sha256'];review_hash=digest(review)
            if file_sha256((c['root']/selection['output']['path']).resolve())!=output: raise BenchmarkError('current output bytes changed')
        actual=execution._state(c['root'],request) if request_path.exists() else None
        if not selection and actual and actual.get('status')=='generated':
            provenance.validate_attempt_provenance(c['root'],rb.attempt_id,shot_id=shot,story_revision=c['marker']['story_revision'],expected_output=actual['output'])
            output=actual['output']['sha256']
        failures,rejection_receipts=_runtime_failures(c['root'],rb.attempt_id,c['marker']['story_revision'],output)
        output_record=selection['output'] if selection else actual.get('output') if actual and output else None
        if output_record:
            output_path=(c['root']/output_record['path']).resolve()
            import subprocess
            try:
                observed_seconds,bounded_seconds,duration_eligible=_runtime_source_measurement(output_path,manifest['plan']['settings']['duration_seconds'])
                duration_status='within_tolerance' if duration_eligible else 'outside_tolerance'
                duration_reason=None if duration_eligible else 'measured original duration differs from frozen planned duration'
            except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as exc:
                # An original that cannot supply usable duration metadata is a
                # technical source failure; its retained evidence still binds.
                bounded_seconds=Fraction();duration_eligible=False
                duration_status='unavailable';duration_reason=str(exc)
            # Current provenance was validated above; provider timing failure is
            # a retained original result, not a reason to discard the report.
            # Recheck after probing: changed evidence bytes remain a hard error.
            if file_sha256(output_path)!=output_record['sha256']:
                raise BenchmarkError('current output bytes changed during duration probe')
            if not duration_eligible:
                accepted=False
                failures=sorted(set(failures)|{'technical_transport'})
            elif accepted:
                seconds=bounded_seconds
        state=row['slot_state']
        terminal_status='proved_not_dispatched' if state=='no-dispatch' else 'terminal_unselected'
        if state in {'terminal','closed'} and not output:
            from lib import openart_jobs as jobs
            try: jobs.verify_terminal_failure(rb.attempt_id,frozen['profile'])
            except jobs.OpenArtCLIError: pass
            else: terminal_status='failed_terminal';failures.append('technical_transport')
        status='complete' if selection or output else (terminal_status if state in {'terminal','closed','no-dispatch'} else
            'unknown_acceptance' if state=='uncertain' else 'running' if state=='submitted' else 'pending')
        if not accepted and status in {'complete','terminal_unselected'} and not failures: failures=['review_availability']
        unresolved=status in {'pending','running','unknown_acceptance'}
        account=next((a for a in snapshot['accounts'] if a['account_key']==row['account_key']),None)
        if account is None: raise BenchmarkError('actual reservation account missing')
        quantum=_exact(account['quantum'])
        known=row['debit_state'] in {'settled','refunded','released'}
        if known and row['debit_state']!='released' and not row['debit_evidence_sha256']: raise BenchmarkError('settled debit lacks authoritative evidence')
        from lib import openart_credit as credit, openart_jobs as jobs
        def receipt(path):
            return {'path':str(path),'sha256':file_sha256(path)} if path.is_file() else None
        output_path=(c['root']/output_record['path']).resolve() if output_record else None
        selected_path=execution._artifact_path(c['root'],'selected_attempts.json')
        quote_path=credit._path(c['inputs']['credit_quote_id'])
        compiled_path=execution._artifact_path(c['root'],'compiled_request-'+c['inputs']['compiled_request_id']+'.json')
        prep_path=execution._artifact_path(c['root'],'preparation_review-'+c['inputs']['preparation_review_id']+'.json')
        rows.append({'occurrence_id':o['occurrence_id'],'attempt_id':rb.attempt_id,'case_id':o['case_id'],'model':o['model'],
            'status':status,'accepted':accepted,'exact_model_id':c['profile']['model'],'output_sha256':output,'selected_review_sha256':review_hash,
            'failure_categories':failures,'rejection_receipts':rejection_receipts,
            'source_available':output_record is not None,'output_receipt':receipt(output_path) if output_path else None,
            'source_seconds':seconds,'observed_source_seconds':observed_seconds,
            'source_duration_status':duration_status,'source_duration_reason':duration_reason,'billing_status':'known' if known else 'uncertain',
            'charged_credits':row['charged_units']*quantum if known else None,
            'refunded_credits':row['refunded_units']*quantum,
            'held_credits':row['reserved_units']*quantum if not known else Fraction(),
            'reservation':asdict(rb),'ledger_debit_evidence_sha256':row['debit_evidence_sha256'],
            'request_receipt':{'path':str(request_path),'sha256':file_sha256(request_path)} if request_path.exists() else None,
            'private_dispatch_receipt':{'path':str(dispatch._manifest_path(rb.attempt_id)),'sha256':file_sha256(dispatch._manifest_path(rb.attempt_id))},
            'snapshot_sha256':frozen['snapshot_sha256'],
            'snapshot_receipt':receipt(jobs.job_dir(rb.attempt_id,create=False)/'frozen_request.json'),
            'compiled_request_receipt':receipt(compiled_path),'preparation_review_receipt':receipt(prep_path),
            'quote_proof_receipt':receipt(quote_path),
            'raw_quote_capture_refs':{k:c['quote_proof'][k] for k in ('account_receipt','cost_receipt','preview_receipt')},
            'selection_receipt':receipt(selected_path),
            'current_av_review_receipt':receipt(c['root']/'artifacts/final_review.json')})
    hashes=[r['output_sha256'] for r in rows if r['accepted']]
    if len(hashes)!=len(set(hashes)): raise BenchmarkError('accepted original output reused')
    return rows


def summarize_runtime_metrics(manifest):
    """Derive facts from current original attempts, private ledger and certification.

    Never calls a provider, initializes a ledger, settles debits or writes final
    certification. Billing anomalies are physically quarantined by the existing
    dispatch.resolve_attempt -> CreditLedger.settle qualified debit path.
    """
    from lib.provider_credit_ledger import read_existing_snapshot
    from lib.production_review import validate_final_review, assert_final_eligible
    checked=validate_runtime_manifest(manifest); snapshot=read_existing_snapshot()
    rows=_runtime_rows(manifest,checked,snapshot); plan=manifest['plan']; certifications=[]
    by_occurrence={r['occurrence_id']:r for r in rows}
    for d in plan['deliverables']:
        selected_rows=[by_occurrence.get(i) for i in d['original_occurrence_ids']]
        if any(r is None or not r['accepted'] for r in selected_rows): continue
        sources=[manifest['sources'][next(i for i,o in enumerate(plan['attempt_order']) if o['occurrence_id']==oid)] for oid in d['original_occurrence_ids']]
        roots={s['project_root'] for s in sources}
        if len(roots)!=1: continue  # One canonical final review must map the originals.
        from pathlib import Path
        root=Path(next(iter(roots))); path=root/'artifacts/final_review.json'
        if not path.is_file(): continue
        raw=path.read_bytes(); review=json.loads(raw)
        if review.get('story_revision')!=d['story_revision']: continue
        result=validate_final_review(root,review)
        if not result['eligible']: continue
        expected=[r['attempt_id'] for r in selected_rows]
        if [s['attempt_id'] for s in review['scenes']]!=expected: continue
        assert_final_eligible(root,review)
        if path.read_bytes()!=raw: raise BenchmarkError('canonical certification changed during validation')
        certifications.append({'deliverable_id':d['deliverable_id'],'model':d['model'],
            'review_receipt':{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()},
            'output_sha256':review['output_sha256'],'attempt_ids':expected})
    violations=_billing_violations(plan,rows)
    quarantine=bool(violations)
    for c in checked:
        b=c['binding']
        quarantine=quarantine or any(q['claim_key']==b.claim_key for q in snapshot['account_quarantine']) \
            or any(a['account_key']==b.account_key and a['quarantined'] for a in snapshot['accounts'])
    out={'evidence_kind':'runtime','live_certification_ready':bool(certifications),'comparison_conclusion':None,
        'billing_violations':violations,'billing_quarantined':quarantine,'certifications':certifications,
        'ledger_snapshot_sha256':digest(snapshot)}
    for m in plan['models']:
        model_rows=[r for r in rows if r['model']==m]; good=[r for r in model_rows if r['accepted']]
        known=sum((r['charged_credits'] for r in model_rows if r['billing_status']=='known'),Fraction())
        refunded=sum((r['refunded_credits'] for r in model_rows),Fraction())
        held=sum((r['held_credits'] for r in model_rows),Fraction()); seconds=sum((r['source_seconds'] for r in good),Fraction())
        complete=len(model_rows)==6 and not any(r['status'] in {'pending','running','unknown_acceptance'} for r in model_rows)
        economic=complete and not quarantine and all(r['billing_status']=='known' for r in model_rows)
        ncert=sum(c['model']==m for c in certifications)
        out[m]={'attempted':len(model_rows),'first_attempt_acceptance':{'numerator':len(good),'denominator':len(model_rows),
            'rate':_ratio(Fraction(len(good),len(model_rows))) if model_rows else None},
            'certified_original_deliverables':ncert,'predeclared_deliverables':6,
            'zero_corrective_delivery':{'numerator':ncert,'denominator':6,'rate':_ratio(Fraction(ncert,6))},
            'known_charged_credits':_ratio(known),'refunded_credits':_ratio(refunded),'net_charged_credits':_ratio(known-refunded),'held_credits':_ratio(held),
            'outstanding_billing_holds':sum(r['billing_status']=='uncertain' for r in model_rows),
            'accepted_source_seconds':_ratio(seconds),'economics_complete':economic,
            'credits_per_accepted_second':_ratio((known-refunded)/seconds) if economic and seconds else None,
            'gross_credits_per_accepted_second':_ratio(known/seconds) if economic and seconds else None,
            'failure_categories':{f:sum(f in r['failure_categories'] for r in model_rows) for f in FAILURES},
            'comparison_complete':complete and not quarantine,
            'unresolved_acceptance':_record_labels([r for r in model_rows if r['status'] in {'pending','running','unknown_acceptance'}])}
    out['comparison_complete']=all(out[m]['comparison_complete'] for m in plan['models'])
    out['records']=[{k:(_ratio(v) if isinstance(v,Fraction) else v) for k,v in r.items()} for r in rows]
    return out


def next_runtime_attempt(manifest, *, timeout=30):
    """Explicit read-only provider refresh; derive eligibility from actual state.

    Saves fresh private capture receipts through the existing credit API. Never
    writes ledger rows/reserves/submits. Dispatch still rechecks under transport.
    No caller-provided eligible flag or budget is accepted.
    """
    from lib.provider_credit_ledger import read_existing_snapshot
    from lib import openart_credit as credit
    checked=validate_runtime_manifest(manifest); snapshot=read_existing_snapshot()
    rows=_runtime_rows(manifest,checked,snapshot)
    if len(rows)==12 or any(r['status'] in {'pending','running','unknown_acceptance'} for r in rows): return None
    if _billing_violations(manifest['plan'],rows): return None
    c=checked[len(rows)]; b=c['binding']
    if manifest['plan']['attempt_order'][len(rows)]['purpose']=='original':
        from lib import openart_jobs as jobs
        try: current=jobs.load_qualification(model=c['profile']['model'],mode=c['profile']['mode'],require='full')
        except jobs.OpenArtCLIError: return None
        if current.get('profile_sha256')!=c['native']['profile_sha256']: return None
    if any(q['claim_key']==b.claim_key for q in snapshot['account_quarantine']) \
            or any(a['account_key']==b.account_key and a['quarantined'] for a in snapshot['accounts']) \
            or any(claim['account_key']==b.claim_key for claim in snapshot['claims']): return None
    fresh=credit.refresh_credit_evidence(c['inputs'],c['profile'],qualification_sha256=c['inputs']['credit_qualification_sha256'],
        approved_quote_id=c['inputs']['credit_quote_id'],timeout=timeout)
    proof=credit._load(fresh['quote_id'])
    if fresh['quote_sha256']!=b.quote_sha256: raise BenchmarkError('fresh quote differs from frozen exact approval')
    snapshot=read_existing_snapshot()  # Refresh can race another project's reservation.
    if any(q['claim_key']==b.claim_key for q in snapshot['account_quarantine']) \
            or any(a['account_key']==b.account_key and a['quarantined'] for a in snapshot['accounts']) \
            or any(claim['account_key']==b.claim_key for claim in snapshot['claims']): return None
    quantum=_exact(proof['terms']['quantum']); balance=_exact(proof['balance'])
    net=holds=allowance_net=allowance_holds=Fraction()
    for r in snapshot['reservations']:
        if r['account_key']!=b.account_key: continue
        rb=json.loads(r['binding_json'])
        debit=(r['charged_units']-r['refunded_units'])*quantum
        hold=r['reserved_units']*quantum if r['debit_state'] in {'reserved','unresolved'} else Fraction()
        net+=debit;holds+=hold
        if r['allowance_id']==b.allowance_id:
            if _exact(rb['allowance'])!=_exact(b.allowance): raise BenchmarkError('immutable ledger allowance differs')
            allowance_net+=debit;allowance_holds+=hold
    account=next((a for a in snapshot['accounts'] if a['account_key']==b.account_key),None)
    if account:
        if _exact(account['quantum'])!=quantum: return None
        # Mirror reserve_prepared's actual divergence and available balance gate.
        expected=account['balance_units']*quantum-(net-account['anchor_units']*quantum)
        pending=sum((r['reserved_units']*quantum for r in snapshot['reservations'] if r['account_key']==b.account_key and r['debit_state']=='unresolved' and r['slot_state'] in {'submitting','submitted','uncertain','terminal','closed'}),Fraction())
        if not max(Fraction(),expected-pending)<=balance<=expected: return None
    needed=sum((_exact(o['ceiling_credits']) for o in manifest['plan']['attempt_order'][len(rows):]),Fraction())
    if _exact(b.allowance)-allowance_net-allowance_holds<needed or balance-holds<_exact(b.quote): return None
    return {'occurrence':manifest['plan']['attempt_order'][len(rows)],'source':manifest['sources'][len(rows)],
            'fresh_quote_id':fresh['quote_id'],'fresh_quote_sha256':fresh['quote_sha256'],
            'ledger_snapshot_sha256':digest(snapshot),'dispatch_authorized':False}
