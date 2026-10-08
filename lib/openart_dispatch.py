"""U4 bridge: private immutable authority -> account ledger -> ready journal -> Popen.

Project -> ledger is the only lock order. Provider refresh is outside project and
ledger locks; final validation runs under the separate reentrant transport lock.
Neither outbox repair nor resolution submits a new invocation.
"""
from __future__ import annotations
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from tools import _openart_cli as cli
from lib import openart_credit as credit, openart_jobs as jobs
from lib.provider_credit_ledger import (CreditLedger, CreditScale, Binding, JournalReady, LaunchIntent,
    OriginalJobReceipt, QualifiedSlotRelease, QualifiedBilling, QualifiedRefund, LedgerError, UnpricedBinding, QualifiedUnpricedBilling)

_EXACT_KEYS=('credit_authorization_id','credit_quote_id','credit_qualification_sha256')
_UNKNOWN_KEYS=('unknown_cost_authorization_id','unknown_cost_evidence_id')
_BOOKKEEPING=_EXACT_KEYS+_UNKNOWN_KEYS

def _unknown(inputs):
    present=any(k in inputs for k in _UNKNOWN_KEYS)
    if not present: return False
    invalid=lambda: cli.OpenArtCLIError('invalid_argument','paired safe unknown-cost authority IDs are exclusive with exact-credit fields')
    if any(k in inputs for k in _EXACT_KEYS) or not all(k in inputs for k in _UNKNOWN_KEYS): raise invalid()
    ident=inputs['unknown_cost_authorization_id']; evidence=inputs['unknown_cost_evidence_id']
    if not isinstance(ident,str) or not ident or len(ident)>128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in ident): raise invalid()
    if not isinstance(evidence,str) or len(evidence)!=64 or any(c not in '0123456789abcdef' for c in evidence): raise invalid()
    return True

def _unpriced(binding):
    return isinstance(binding,UnpricedBinding)

def _inspect(l,b):
    return l.inspect_unpriced(b.attempt_id) if _unpriced(b) else l.inspect(b.attempt_id)

def _mark(l,b,target,evidence):
    if _unpriced(b): return l.mark_unpriced(b,target,evidence)
    if target=='ready': return l.mark_ready(JournalReady(b,evidence))
    if target=='submitting': return l.mark_submitting(LaunchIntent(b,evidence))
    return l.mark_uncertain(b,evidence)

def _billing_state(row):
    return row.get('billing_state',row.get('debit_state'))
_CRASH_HOOK=None  # offline tests only; never selected from public caller fields


def _checkpoint(stage,attempt_id):
    if _CRASH_HOOK is not None: _CRASH_HOOK(stage,attempt_id)


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def ledger():
    return CreditLedger()


def _manifest_path(attempt_id):
    return jobs.job_dir(cli._safe_part(attempt_id),create=False)/'credit_dispatch.json'


def _manifest(attempt_id):
    path=_manifest_path(attempt_id)
    try:
        cli.verify_private_state(cli.state_dir(create=False)); cli._check_private(path.parent,True); cli._check_private(path,False); raw=path.read_bytes(); value=json.loads(raw)
        if value.get('version')!='1' or value.get('attempt_id')!=attempt_id: raise ValueError()
        kind=value.get('authorization_kind','exact_credit')
        if kind not in {'exact_credit','unknown_cost'}: raise ValueError()
        binding=(UnpricedBinding if kind=='unknown_cost' else Binding)(**value['binding']); binding.validate()
        snapshot=jobs.load_frozen_request(attempt_id)
        if snapshot['snapshot_sha256']!=value['snapshot_sha256'] or snapshot['native']['native_body_sha256']!=binding.native_sha256:
            raise ValueError()
        digest=credit.unknown_cost_authorization_digest if _unpriced(binding) else credit.credit_authorization_digest
        if value['authorization_sha256']!=digest(value['authorization']): raise ValueError()
        if _unpriced(binding):
            _unknown(value['original_inputs'])
            evidence=credit.get_retained_unknown_evidence(value['original_inputs'],snapshot['profile'],value['evidence_id'])
            if evidence['evidence_sha256']!=binding.evidence_sha256 or evidence!=value['unknown_cost_evidence']: raise ValueError()
        if value['authorization_sha256']!=binding.authorization_sha256: raise ValueError()
        if value['purpose']!=value['authorization']['purpose']: raise ValueError()
        return value,binding,snapshot
    except (OSError,ValueError,KeyError,TypeError):
        raise cli.OpenArtCLIError('credit_dispatch_invalid','original immutable credit dispatch proof missing or changed') from None


def _authorization(root,inputs):
    from lib.production_execution import _artifact_path
    kind='unknown_cost' if _unknown(inputs) else 'credit'
    ident=cli._safe_part(inputs.get(kind+'_authorization_id'))
    return json.loads(_artifact_path(root,kind+'_authorization-'+ident+'.json').read_text())


def prepare_dispatch(inputs,checked,attempt_id,deadline):
    """Readonly fresh eligibility/quote before acquiring a project lock."""
    profile,native=checked['openart']; authorization=_authorization(checked['root'],inputs)
    if _unknown(inputs):
        credit.get_retained_unknown_evidence(inputs,profile,inputs['unknown_cost_evidence_id'])
        fresh=credit.refresh_unknown_cost_evidence(inputs,profile,timeout=cli.lock_remaining(deadline),deadline=deadline)
        approval=credit.validate_unknown_cost_authorization(checked['root'],authorization,scope=checked['scope'],marker=checked['marker'],
            inputs=inputs,profile=profile,evidence_id=fresh['evidence_id'],request_sha256=checked['request_sha256'],
            occurrence_index=checked['scope_attempt_index'],attempt_id=attempt_id)
        return {'approval':approval,'authorization':authorization,'fresh_evidence':fresh,'original_inputs':inputs,'deadline':deadline}
    fresh=credit.refresh_credit_evidence(inputs,profile,
        qualification_sha256=inputs.get('credit_qualification_sha256'),approved_quote_id=inputs.get('credit_quote_id'),
        timeout=cli.lock_remaining(deadline),deadline=deadline)
    approval=credit.validate_credit_authorization(checked['root'],authorization,scope=checked['scope'],marker=checked['marker'],
        inputs=inputs,profile=profile,quote_id=fresh['quote_id'],request_sha256=checked['request_sha256'],
        occurrence_index=checked['scope_attempt_index'],attempt_id=attempt_id)
    return {'approval':approval,'authorization':authorization,'fresh_quote':fresh,'original_inputs':inputs,'deadline':deadline}


def reserve_dispatch(prepared,checked,request,frozen):
    """Called under project lock: private snapshots first, then atomic ledger/outbox."""
    approval=prepared['approval']; b=approval.binding; profile,native=checked['openart']
    # Recheck all factual authority under the project lock; no provider calls.
    if _unpriced(b):
        rechecked=credit.validate_unknown_cost_authorization(checked['root'],prepared['authorization'],scope=checked['scope'],marker=checked['marker'],
            inputs=prepared['original_inputs'],profile=profile,evidence_id=prepared['fresh_evidence']['evidence_id'],
            request_sha256=checked['request_sha256'],occurrence_index=checked['scope_attempt_index'],attempt_id=b.attempt_id)
    else:
        rechecked=credit.validate_credit_authorization(checked['root'],prepared['authorization'],scope=checked['scope'],marker=checked['marker'],
            inputs=prepared['original_inputs'],profile=profile,quote_id=prepared['fresh_quote']['quote_id'],
            request_sha256=checked['request_sha256'],occurrence_index=checked['scope_attempt_index'],attempt_id=b.attempt_id)
    if rechecked.binding!=b: raise cli.OpenArtCLIError('credit_authorization_changed','authority changed during reservation')
    records={'request.json':request,'selected_attempts.json':__import__('lib.production_execution',fromlist=['load_selected_attempts']).load_selected_attempts(checked['root'])}
    if checked['contract']: records['shot_contract.json']=checked['contract']
    manifest={'version':'1','attempt_id':b.attempt_id,'binding':asdict(b),'purpose':approval.purpose,
        'authorization':prepared['authorization'],'authorization_sha256':b.authorization_sha256,
        'scope':checked['scope'],'marker':checked['marker'],'original_inputs':prepared['original_inputs'],
        'snapshot_sha256':frozen['snapshot_sha256'],'journal_records':records}
    manifest['authorization_kind']='unknown_cost' if _unpriced(b) else 'exact_credit'
    if _unpriced(b):
        manifest.update(evidence_id=prepared['fresh_evidence']['evidence_id'],unknown_cost_evidence=credit.load_unknown_evidence(prepared['fresh_evidence']['evidence_id']),
            unknown_cost_authorization={'id':prepared['original_inputs']['unknown_cost_authorization_id'],'sha256':b.authorization_sha256,'occurrence':b.authorization_occurrence})
    else:
        manifest.update(quote_id=prepared['fresh_quote']['quote_id'],original_quote_id=prepared['original_inputs']['credit_quote_id'],
            qualification_sha256=prepared['original_inputs']['credit_qualification_sha256'])
    cli.write_private(jobs.job_dir(b.attempt_id)/'credit_dispatch.json',json.dumps(manifest,sort_keys=True,separators=(',',':')).encode())
    _checkpoint('private_prepared',b.attempt_id)
    l=ledger()
    if _unpriced(b):
        reservation=l.reserve_unpriced(credit.internal_unpriced_packet(approval))
        _checkpoint('ledger_prepared',b.attempt_id)
        return reservation
    proof=credit._load(manifest['quote_id'])
    l.observe_account(b.provider,b.account_id,b.workspace_id,CreditScale(proof['terms']['quantum']),proof['balance'],proof['account_receipt']['receipt_sha256'])
    reservation=l.reserve_prepared(credit.internal_ledger_packet(approval))
    _checkpoint('ledger_prepared',b.attempt_id)
    return reservation


def journal_ready(attempt_id,*,submitting=True):
    """Authoritative ready files must all match retained private publication intent."""
    manifest,b,_=_manifest(attempt_id); root=Path(b.project_root); directory=root/'production_attempts'/attempt_id
    for name,expected in manifest['journal_records'].items():
        path=directory/name
        if json.loads(path.read_text())!=expected: raise cli.OpenArtCLIError('journal_not_ready','original journal publication incomplete or changed')
    digest=hashlib.sha256((directory/'request.json').read_bytes()).hexdigest(); l=ledger(); row=_inspect(l,b)
    if row['slot_state']=='prepared': _mark(l,b,'ready',digest)
    _checkpoint('journal_ready',attempt_id)
    if submitting:
        _mark(l,b,'submitting',digest); _checkpoint('ledger_submitting',attempt_id)
    repair_outbox(root,attempt_id,publish=False)


def reservation_lookup(project_root,attempt_id,request_sha256):
    """Only trusted durable submitting state can authorize paid launch/nesting."""
    manifest,b,frozen=_manifest(attempt_id); row=ledger().require_unpriced_submitting(b) if _unpriced(b) else ledger().require_submitting(b)
    if (Path(project_root).resolve()!=Path(b.project_root) or request_sha256!=b.request_sha256 or row['slot_state']!='submitting'
            or row['binding_json']!=json.dumps(asdict(b),sort_keys=True,separators=(',',':'))):
        raise cli.OpenArtCLIError('no_active_reservation','matching original submitting credit reservation required')
    native=frozen['native']
    return {'state':'active','slot_state':'submitting','reservation_id':attempt_id,'attempt_id':attempt_id,
        'request_sha256':b.request_sha256,'account_id_sha256':b.account_id,'workspace':b.workspace_id,
        'authorization_kind':manifest.get('authorization_kind','exact_credit'),
        'native_body_sha256':b.native_sha256,'profile_sha256':b.profile_sha256,
        'native_controls_sha256':native['native_controls_sha256'],'native_argv_sha256':native['native_argv_sha256'],
        'snapshot_sha256':manifest['snapshot_sha256'],'purpose':manifest['purpose'],
        'authorization_occurrence_id':b.authorization_occurrence,'authorization_sha256':b.authorization_sha256}



def _ready_journal(binding,manifest):
    """Current public bytes must match private intent and the durable ready event."""
    from lib.provider_credit_ledger import read_existing_snapshot
    path=Path(binding.project_root)/'production_attempts'/binding.attempt_id/'request.json'
    try:
        raw=path.read_bytes();request=json.loads(raw);snap=read_existing_snapshot()
        ready=[r for r in snap['unpriced_ready_journals' if _unpriced(binding) else 'ready_journals'] if r['attempt_id']==binding.attempt_id]
        if request!=manifest['journal_records']['request.json'] or request.get('attempt_id')!=binding.attempt_id \
                or request.get('request_sha256')!=binding.request_sha256 or len(ready)!=1 \
                or ready[0]['journal_sha256']!=hashlib.sha256(raw).hexdigest():
            raise ValueError()
        return request,snap
    except (OSError,ValueError,KeyError,TypeError,LedgerError):
        raise cli.OpenArtCLIError('journal_not_ready','current original journal differs from immutable ready proof') from None


def _validate_current_unknown_authority(manifest,b,frozen):
    root=Path(b.project_root)
    try:
        current_marker=json.loads((root/'project.json').read_bytes())
        scopes=json.loads((root/'production_scopes.json').read_bytes())
        matches=[x for x in scopes.get('scopes',[]) if x.get('id')==manifest['scope']['id']]
        current_authorization=_authorization(root,manifest['original_inputs'])
        if scopes.get('version')!='1.0' or len(matches)!=1 or current_marker!=manifest['marker'] or matches[0]!=manifest['scope'] or current_authorization!=manifest['authorization']:
            raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):
        raise cli.OpenArtCLIError('unknown_cost_authorization_invalid','current original approval/marker/scope changed') from None
    if current_marker.get('pipeline_type')=='provider-qualification':
        from lib.provider_qualification import validate_qualification_stage
        try:
            validate_qualification_stage(root,manifest['original_inputs'],b.request_sha256,native=frozen['native'],profile=frozen['profile'])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            raise cli.OpenArtCLIError('unknown_cost_authorization_invalid',str(exc)) from None
    rechecked=credit.validate_unknown_cost_authorization(b.project_root,current_authorization,scope=matches[0],marker=current_marker,
        inputs=manifest['original_inputs'],profile=frozen['profile'],evidence_id=manifest['evidence_id'],request_sha256=b.request_sha256,
        occurrence_index=next(x['index'] for x in current_authorization['occurrences'] if credit.canonical_scope_occurrence(current_authorization,x)==b.authorization_occurrence),attempt_id=b.attempt_id)
    if rechecked.binding!=b: raise cli.OpenArtCLIError('unknown_cost_authorization_invalid','original authority changed')


def validate_prelaunch(project_root,attempt_id,request_sha256,deadline):
    """Under held transport; authority is reread from private original snapshots."""
    manifest,b,frozen=_manifest(attempt_id); reservation_lookup(project_root,attempt_id,request_sha256)
    from lib.production_request import validate_frozen_preparation
    public_request,_=_ready_journal(b,manifest)
    validate_frozen_preparation(public_request,frozen,Path(b.project_root))
    if _unpriced(b):
        _validate_current_unknown_authority(manifest,b,frozen)
        fresh=credit.refresh_unknown_cost_evidence(manifest['original_inputs'],frozen['profile'],timeout=cli.lock_remaining(deadline),deadline=deadline)
        _validate_current_unknown_authority(manifest,b,frozen)
        for key,expected in [('account_id_sha256',b.account_id),('native_body_sha256',b.native_sha256),('profile_sha256',b.profile_sha256)]:
            if fresh[key]!=expected: raise cli.OpenArtCLIError('unknown_cost_evidence_changed','fresh original binding changed')
        reservation_lookup(project_root,attempt_id,request_sha256)
        _checkpoint('current_prelaunch',attempt_id)
        return fresh
    fresh=credit.refresh_credit_evidence(manifest['original_inputs'],frozen['profile'],
        qualification_sha256=manifest['qualification_sha256'],approved_quote_id=manifest['original_quote_id'],
        timeout=cli.lock_remaining(deadline),deadline=deadline)
    if fresh['quote_sha256']!=b.quote_sha256: raise cli.OpenArtCLIError('credit_quote_changed','exact approved quote changed before Popen')
    proof=credit._load(fresh['quote_id'])
    ledger().observe_account(b.provider,b.account_id,b.workspace_id,CreditScale(proof['terms']['quantum']),proof['balance'],proof['account_receipt']['receipt_sha256'])
    # Observe_account can quarantine globally; lookup cannot silently clear it.
    reservation_lookup(project_root,attempt_id,request_sha256)
    _checkpoint('current_prelaunch',attempt_id)
    return fresh


def _refusal_observation(attempt_id):
    """Read-only original process/response proof; requires no identified original job."""
    try:
        launch=jobs.launch_record(attempt_id)
        if not launch or launch.get('authorization_kind')!='unknown_cost' or jobs.original_job_id(attempt_id): return None
        jobs._verify_origin_frozen(attempt_id,launch,'credit_original_job_invalid')
        process=jobs.original_process_state(attempt_id)
        rc=process.get('returncode')
        if process['state']!='exited' or type(rc) is not int or rc==0: return None
        path=jobs.job_dir(attempt_id,create=False)/'submit.stdout'
        cli._check_private(path,False)
        import os
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as fh: raw=fh.read(cli.MAX_STDOUT+1)
        if len(raw)>cli.MAX_STDOUT: return None
        code=jobs.provider_refusal_code(raw)
        sha=hashlib.sha256(raw).hexdigest()
        events=[e for e in jobs.read_events(attempt_id) if e.get('type')=='provider_refused']
        if not code or len(events)!=1 or any(events[0].get(k)!=v for k,v in {'code':code,'returncode':rc,'stdout_sha256':sha}.items()): return None
        return {'code':code,'returncode':rc,'stdout_sha256':sha,'evidence_sha256':_hash({'code':code,'stdout_sha256':sha})}
    except (OSError,ValueError,TypeError,KeyError,cli.OpenArtCLIError): return None


def verify_provider_refusal(attempt_id):
    """Pure report verification against original response and qualified release proof."""
    from lib.provider_credit_ledger import read_existing_snapshot
    try:
        manifest,b,_=_manifest(attempt_id)
        if not _unpriced(b): return None
        refusal=_refusal_observation(attempt_id)
        if refusal is None: return None
        rows=[r for r in read_existing_snapshot()['unpriced_reservations'] if r['attempt_id']==attempt_id]
        if len(rows)!=1: return None
        row=rows[0]; release=json.loads(row['release_json'])
        if row['binding_json']!=json.dumps(asdict(b),sort_keys=True,separators=(',',':')) or row['job_id'] \
                or row['slot_state']!='closed' or row['release_kind']!='inactive_unidentified' \
                or release['binding']!=asdict(b) or release['kind']!='inactive_unidentified' \
                or release['job_id'] or release['evidence_sha256']!=refusal['evidence_sha256'] \
                or release['original_process_exit_sha256']!=_hash(jobs.original_process_state(attempt_id)): return None
        return refusal
    except (OSError,ValueError,TypeError,KeyError,cli.OpenArtCLIError,LedgerError): return None


def record_launch_result(attempt_id):
    """Reconcile qualified original stdout only; billing remains independent."""
    manifest,b,frozen=_manifest(attempt_id); l=ledger(); row=_inspect(l,b)
    job=jobs.original_job_id(attempt_id)
    if job:
        try:
            proof=jobs._verify_raw_submit(attempt_id,jobs.launch_record(attempt_id),job,'credit_original_job_invalid')
        except cli.OpenArtCLIError:
            job=None  # Staged tentative parse is not qualified original-job identity.
        else:
            if row['slot_state'] in {'submitting','uncertain'}:
                (l.mark_unpriced_submitted if _unpriced(b) else l.mark_submitted)(OriginalJobReceipt(b,_hash(proof),job)); _checkpoint('ledger_submitted',attempt_id)
    if not job and _unpriced(b):
        refusal=_refusal_observation(attempt_id)
        if refusal and row['slot_state'] in {'submitting','uncertain'}:
            l.release_unpriced(QualifiedSlotRelease(b,'inactive_unidentified',refusal['evidence_sha256'],_hash(jobs.original_process_state(attempt_id)),None))
        elif row['slot_state']=='submitting': _mark(l,b,'uncertain',_hash(jobs.reconcile_job(attempt_id)))
    elif not job and row['slot_state']=='submitting': _mark(l,b,'uncertain',_hash(jobs.reconcile_job(attempt_id)))
    return _inspect(l,b)


def repair_outbox(project_root,attempt_id,*,publish=True):
    """Repair original journal publication/inspection only; never advance to launch."""
    from lib import production_execution as execution
    manifest,b,frozen=_manifest(attempt_id)
    if Path(project_root).resolve()!=Path(b.project_root): raise cli.OpenArtCLIError('credit_origin_mismatch','outbox project mismatch')
    if _unpriced(b):
        try: ledger().inspect_unpriced(attempt_id)
        except LedgerError as exc:
            if str(exc)!='unknown attempt': raise
            # Private proof existed before the atomic claim. Reconstruct only the
            # original claim with retained evidence; recovery never calls or launches.
            root=Path(b.project_root)
            marker=json.loads((root/'project.json').read_bytes())
            scopes=json.loads((root/'production_scopes.json').read_bytes())
            matches=[x for x in scopes.get('scopes',[]) if x.get('id')==manifest['scope']['id']]
            if scopes.get('version')!='1.0' or len(matches)!=1 or matches[0]!=manifest['scope'] or marker!=manifest['marker'] or _authorization(root,manifest['original_inputs'])!=manifest['authorization']:
                raise cli.OpenArtCLIError('unknown_cost_authorization_invalid','current original authority changed during recovery')
            approval=credit.validate_unknown_cost_authorization(root,manifest['authorization'],scope=matches[0],marker=marker,
                inputs=manifest['original_inputs'],profile=frozen['profile'],evidence_id=manifest['evidence_id'],request_sha256=b.request_sha256,
                occurrence_index=next(x['index'] for x in manifest['authorization']['occurrences'] if credit.canonical_scope_occurrence(manifest['authorization'],x)==b.authorization_occurrence),attempt_id=attempt_id)
            if approval.binding!=b: raise cli.OpenArtCLIError('unknown_cost_authorization_invalid','original recovered binding differs')
            ledger().reserve_unpriced(credit.internal_unpriced_packet(approval))
    directory=Path(b.project_root)/'production_attempts'/attempt_id; directory.mkdir(parents=True,exist_ok=True)
    if publish:
        for name,record in manifest['journal_records'].items():
            path=directory/name
            if path.exists():
                if json.loads(path.read_text())!=record: raise cli.OpenArtCLIError('journal_conflict','outbox cannot replace changed original journal')
            else: execution._write_new(path,record)
        row=_inspect(ledger(),b)
        if row['slot_state']=='prepared':
            digest=hashlib.sha256((directory/'request.json').read_bytes()).hexdigest()
            _mark(ledger(),b,'ready',digest)
    events=directory/'credit_events'; events.mkdir(exist_ok=True)
    for event in ledger().outbox():
        if event['attempt_id']!=attempt_id: continue
        path=events/(event['event_id']+'.json'); record={k:v for k,v in event.items() if k not in {'acknowledged','journal_sha256'}}
        if path.exists():
            if json.loads(path.read_text())!=record: raise cli.OpenArtCLIError('journal_conflict','credit event publication changed')
        else: execution._write_new(path,record)
        ledger().ack_outbox(event['event_id'],b,hashlib.sha256(path.read_bytes()).hexdigest())
    return {'attempt_id':attempt_id,'slot_state':_inspect(ledger(),b)['slot_state'],'paid_submission':False}


def offline_readiness(inputs):
    """Retained preparation/quote readiness only; no CLI, ledger, or reservation."""
    _unknown(inputs)
    profile=jobs.load_qualification(model=inputs.get('model'),mode=inputs.get('mode'),require='pre_submit')
    native=jobs.prepare_native_request(credit._controls(inputs),profile)
    from lib.production_request import validate_preparation
    preparation=validate_preparation(inputs,native,profile)
    if _unknown(inputs):
        evidence=credit.get_retained_unknown_evidence(inputs,profile,inputs['unknown_cost_evidence_id'])
        return {'preparation':preparation,'authorization_kind':'unknown_cost','credit_state':'unknown_cost_evidence_retained',**evidence,'provider_calls':0,'reservations':0}
    quote=credit.get_retained_quote(inputs,profile,inputs.get('credit_quote_id'))
    return {'preparation':preparation,**quote,'provider_calls':0,'reservations':0}


def resolve_attempt(project_root,attempt_id,request_sha256,*,timeout=cli.DEFAULT_TIMEOUT,billing_proof_id=None):
    """Original-attempt resolution: qualified terminal receipt frees slot, not debit."""
    manifest,b,frozen=_manifest(attempt_id)
    if Path(project_root).resolve()!=Path(b.project_root) or request_sha256!=b.request_sha256:
        raise cli.OpenArtCLIError('credit_origin_mismatch','resolution original project/request mismatch')
    jobs.recover_launch(attempt_id); row=record_launch_result(attempt_id); process=jobs.original_process_state(attempt_id)
    if not row['job_id']:
        return {'attempt_id':attempt_id,'slot_state':row['slot_state'],'billing_state':_billing_state(row),'resolution':'provider_refused' if row['slot_state']=='closed' and verify_provider_refusal(attempt_id) else 'unsupported_hold','paid_submission':False}
    profile=frozen['profile']; deadline=time.monotonic()+cli.validate_timeout(timeout)
    with cli.transport_lock(wait_timeout=cli.lock_remaining(deadline)):
        account=cli.run_readonly(['account'],timeout=cli.lock_remaining(deadline))
        record=jobs._load_receipt(account['receipt_id'],account['receipt_sha256'],'credit_account_invalid')
        if jobs._check_account_receipt(record,profile)!=b.account_id: raise cli.OpenArtCLIError('credit_origin_mismatch','resolution account changed')
        status=cli.run_readonly(['creation','get',row['job_id']],timeout=cli.lock_remaining(deadline))
        record=jobs._load_receipt(status['receipt_id'],status['receipt_sha256'],'credit_terminal_invalid')
        observed,_=jobs._check_status_receipt(record,row['job_id'],profile)
        paths=jobs._effective_paths(profile)
        terminal=observed in {paths['status_terminal_ok'],paths['status_terminal_fail']}
        evidence=_hash({'status_receipt':status['receipt_sha256'],'account_receipt':account['receipt_sha256'],
            'native_sha256':b.native_sha256,'job_id':row['job_id']})
        if terminal and process['state'] in {'exited','dead'} and row['slot_state'] not in {'terminal','closed'}:
            (ledger().release_unpriced if _unpriced(b) else ledger().release_slot)(QualifiedSlotRelease(b,'terminal',evidence,_hash(process),row['job_id']))
        if billing_proof_id is not None:
            proof,contract=_load_billing(billing_proof_id,b,frozen)
            if proof['job_id']!=row['job_id']: raise cli.OpenArtCLIError('billing_origin_mismatch','billing original job changed')
            observed=_billing_observation(b,row['job_id'],contract,account,status,profile)
            if _unpriced(b):
                ledger().record_unpriced_billing(QualifiedUnpricedBilling(b,observed['debit_evidence'],row['job_id'],observed['amount']))
            else:
                ledger().settle(QualifiedBilling(b,observed['debit_evidence'],row['job_id'],observed['amount']))
            _checkpoint('ledger_settled',attempt_id)
            if observed['refund'] is not None:
                ledger().refund(QualifiedRefund(b,observed['refund_evidence'],observed['debit_evidence'],row['job_id'],observed['refund']))
                _checkpoint('ledger_refunded',attempt_id)
    repair_outbox(project_root,attempt_id,publish=False)
    row=_inspect(ledger(),b)
    resolution=('terminal_unknown_billing' if _billing_state(row) in {'unknown','unresolved'} else 'terminal_billing_resolved') if row['slot_state'] in {'terminal','closed'} else 'active_hold'
    return {'attempt_id':attempt_id,'slot_state':row['slot_state'],'billing_state':_billing_state(row),'resolution':resolution,'paid_submission':False}


@dataclass(frozen=True)
class BillingContract:
    """Declared paths are qualified only by retained actual per-job statements."""
    account: str
    workspace: str
    native: str
    job: str
    amount: str
    quantum: str
    final: str
    authoritative: str
    debit_id: str
    refund: str
    refund_id: str
    refund_authoritative: str
    refund_debit: str

    def validate(self):
        from lib import openart_qualification as qual
        paths=list(asdict(self).values())
        if len(set(paths))!=len(paths) or any(not qual._path(p) for p in paths):
            raise cli.OpenArtCLIError('billing_contract_unqualified','billing semantic paths must be distinct qualified JSON paths')


def _origin(project_root,attempt_id,request_sha256):
    manifest,b,frozen=_manifest(attempt_id)
    if Path(project_root).resolve()!=Path(b.project_root) or request_sha256!=b.request_sha256:
        raise cli.OpenArtCLIError('credit_origin_mismatch','original project/request binding required')
    return manifest,b,frozen


def _exact_receipt(out,kind):
    """Verify retained capture then decode actual financial lexemes exactly."""
    from decimal import Decimal
    from lib import openart_qualification as qual
    entry={'kind':kind,'receipt_id':out['receipt_id'],'receipt_sha256':out['receipt_sha256']}
    record=qual._record(entry,'billing_contract_unqualified')
    raw=(cli.state_dir()/'streams'/record['streams']['stdout']).read_bytes()
    parsed=json.loads(raw,parse_float=Decimal)
    return entry,record,parsed


def _billing_observation(b,job_id,contract,account_out,status_out,profile):
    contract.validate()
    ae,ar,account=_exact_receipt(account_out,'account'); se,sr,status=_exact_receipt(status_out,'creation_get')
    if ar['argv']!=['account']+cli.GLOBAL_FLAGS or sr['argv']!=['creation','get',job_id]+cli.GLOBAL_FLAGS:
        raise cli.OpenArtCLIError('billing_origin_mismatch','billing captures do not refer to original commands')
    from lib.openart_qualification import lookup_path as at
    if _unpriced(b):
        account_id=at(account,profile['json_paths']['account_id'])
        if not isinstance(account_id,str) or hashlib.sha256(account_id.encode()).hexdigest()!=b.account_id \
                or at(status,contract.account)!=account_id or at(status,contract.native)!=b.native_sha256 \
                or at(status,contract.job)!=job_id or at(status,contract.workspace) is not None:
            raise cli.OpenArtCLIError('billing_origin_mismatch','unknown-cost per-job billing origin differs')
        if at(status,contract.final) is not True or at(status,contract.authoritative) is not True:
            raise cli.OpenArtCLIError('billing_contract_unqualified','actual final authoritative per-job statement required')
        amount=credit._number_text(at(status,contract.amount)); debit=at(status,contract.debit_id)
        if amount is None or not isinstance(debit,str) or not debit:
            raise cli.OpenArtCLIError('billing_contract_unqualified','actual per-job debit amount/identity required')
        evidence=_hash({'provider':b.provider,'account':b.account_id,'workspace':b.workspace_id,'attempt':b.attempt_id,
            'native':b.native_sha256,'job':job_id,'debit':debit,'amount':amount})
        return {'account_receipt':ae,'status_receipt':se,'amount':amount,'debit_evidence':evidence,'refund':None,'refund_evidence':None,'quantum':None}
    quote=credit._load(_manifest(b.attempt_id)[0]['quote_id'])
    if quote['quote_sha256']!=b.quote_sha256:
        raise cli.OpenArtCLIError('billing_origin_mismatch','retained billing quote differs from original reservation')
    terms=quote['terms']
    quote_contract=credit.QuoteContract(**quote['contract'])
    # Both the actual fresh account and per-job statement must explicitly bind billing scope.
    account_id=at(account,profile['json_paths']['account_id'])
    workspace=at(account,quote_contract.workspace_path) if quote_contract.workspace_path else None
    if not isinstance(account_id,str) or hashlib.sha256(account_id.encode()).hexdigest()!=b.account_id \
            or workspace!=b.workspace_id or at(status,contract.account)!=account_id \
            or at(status,contract.workspace)!=b.workspace_id or at(status,contract.native)!=b.native_sha256 \
            or at(status,contract.job)!=job_id:
        raise cli.OpenArtCLIError('billing_origin_mismatch','per-job billing account/workspace/native/job mismatch')
    if at(status,contract.final) is not True or at(status,contract.authoritative) is not True:
        raise cli.OpenArtCLIError('billing_contract_unqualified','actual provider statement lacks final authoritative per-job debit guarantee')
    scale=CreditScale(at(status,contract.quantum))
    if scale.quantum!=CreditScale(terms['quantum']).quantum:
        raise cli.OpenArtCLIError('billing_quantum_changed','billing quantum changed')
    amount=scale.display(scale.units(at(status,contract.amount)))
    debit=at(status,contract.debit_id)
    if not isinstance(debit,str) or not debit: raise cli.OpenArtCLIError('billing_contract_unqualified','original debit identity missing')
    refund=at(status,contract.refund); refund_id=at(status,contract.refund_id)
    if refund is not None:
        if at(status,contract.refund_authoritative) is not True or at(status,contract.refund_debit)!=debit or not isinstance(refund_id,str) or not refund_id:
            raise cli.OpenArtCLIError('billing_contract_unqualified','actual provider statement lacks original-debit refund guarantee')
        refund=scale.display(scale.units(refund))
        if scale.units(refund)>scale.units(amount): raise cli.OpenArtCLIError('billing_refund_invalid','refund exceeds original debit')
    # Semantic evidence identity makes fresh same-authoritative-debit reads idempotent.
    debit_sha=_hash({'provider':b.provider,'account':b.account_id,'workspace':b.workspace_id,
        'attempt':b.attempt_id,'native':b.native_sha256,'job':job_id,'debit':debit,'amount':amount,'quantum':scale.quantum})
    refund_sha=_hash({'debit':debit_sha,'refund':refund_id,'amount':refund}) if refund is not None else None
    return {'account_receipt':ae,'status_receipt':se,'amount':amount,'debit_evidence':debit_sha,
        'refund':refund,'refund_evidence':refund_sha,'quantum':scale.quantum}


def _billing_path(proof_id):
    if not isinstance(proof_id,str) or len(proof_id)!=64 or any(c not in '0123456789abcdef' for c in proof_id):
        raise cli.OpenArtCLIError('billing_contract_unqualified','retained billing proof SHA required')
    return cli.state_dir()/'billing_contracts'/(proof_id+'.json')


def qualify_resolution_contract(project_root,attempt_id,request_sha256,contract,*,timeout=cli.DEFAULT_TIMEOUT):
    """Explicit nonspending qualification; caller path declarations alone never settle."""
    if not isinstance(contract,BillingContract): raise cli.OpenArtCLIError('billing_contract_unqualified','typed billing contract required')
    contract.validate(); manifest,b,frozen=_origin(project_root,attempt_id,request_sha256)
    jobs.recover_launch(attempt_id);row=record_launch_result(attempt_id)
    if not row['job_id']: raise cli.OpenArtCLIError('billing_contract_unqualified','independently qualified original job required')
    deadline=time.monotonic()+cli.validate_timeout(timeout)
    with cli.transport_lock(wait_timeout=cli.lock_remaining(deadline)):
        account=cli.run_readonly(['account'],timeout=cli.lock_remaining(deadline))
        status=cli.run_readonly(['creation','get',row['job_id']],timeout=cli.lock_remaining(deadline))
        observed=_billing_observation(b,row['job_id'],contract,account,status,frozen['profile'])
    proof={'version':'1','binding':asdict(b),'snapshot_sha256':manifest['snapshot_sha256'],
        'job_id':row['job_id'],'contract':asdict(contract),'observed':observed}
    ident=_hash(proof); path=_billing_path(ident)
    path.parent.mkdir(mode=0o700,exist_ok=True);cli._check_private(path.parent,True)
    if not path.exists():cli.write_private(path,json.dumps(proof,sort_keys=True,separators=(',',':')).encode())
    return {'billing_proof_id':ident,'attempt_id':attempt_id,'qualified':True,'paid_submission':False}


def _load_billing(proof_id,b,frozen):
    path=_billing_path(proof_id);cli._check_private(path,False);proof=json.loads(path.read_bytes())
    if _hash(proof)!=proof_id or proof['binding']!=asdict(b) or proof['snapshot_sha256']!=frozen['snapshot_sha256']:
        raise cli.OpenArtCLIError('billing_contract_unqualified','retained billing proof changed or wrong origin')
    contract=BillingContract(**proof['contract'])
    # Reverify original qualification receipt bytes and all declared factual bindings.
    observed=proof['observed']
    checked=_billing_observation(b,proof['job_id'],contract,observed['account_receipt'],observed['status_receipt'],frozen['profile'])
    if checked!=observed: raise cli.OpenArtCLIError('billing_contract_unqualified','retained billing observation changed')
    return proof,contract


def existing_terminal_state(project_root,attempt_id):
    """Pure uncertain-journal fallback; terminal slot is never selection evidence."""
    try:
        root=Path(project_root).resolve();path=root/'production_attempts'/cli._safe_part(attempt_id)/'request.json'
        raw=path.read_bytes();request=json.loads(raw)
        if request.get('scope',{}).get('provider')!='openart_cli':return None
        manifest,b,frozen=_origin(root,attempt_id,request['request_sha256'])
        request,snap=_ready_journal(b,manifest)
        rows=[r for r in snap['unpriced_reservations' if _unpriced(b) else 'reservations'] if r['attempt_id']==attempt_id]
        if len(rows)!=1:return None
        row=rows[0]
        if row['binding_json']!=json.dumps(asdict(b),sort_keys=True,separators=(',',':')) \
                or row['slot_state'] not in {'terminal','closed'} or row['release_kind'] not in {'terminal','inactive_unidentified'}:
            return None
        release=json.loads(row['release_json'])
        if release['binding']!=asdict(b) or release['kind']!=row['release_kind'] \
                or (release['kind']=='terminal' and (not row['job_id'] or release['job_id']!=row['job_id'])):
            return None
        return {'status':'terminal_unselected','result':None,'output':None,'preserved_output':None,
                'credit':{'slot_state':row['slot_state'],('billing_state' if _unpriced(b) else 'debit_state'):_billing_state(row)}}
    except (cli.OpenArtCLIError,LedgerError,OSError,ValueError,KeyError,TypeError):
        return None
