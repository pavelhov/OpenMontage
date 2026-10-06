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
    OriginalJobReceipt, QualifiedSlotRelease, QualifiedBilling, QualifiedRefund, LedgerError)

_BOOKKEEPING=('credit_authorization_id','credit_quote_id','credit_qualification_sha256')
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
        cli.verify_private_state(); raw=path.read_bytes(); value=json.loads(raw)
        if value.get('version')!='1' or value.get('attempt_id')!=attempt_id: raise ValueError()
        binding=Binding(**value['binding']); binding.validate()
        snapshot=jobs.load_frozen_request(attempt_id)
        if snapshot['snapshot_sha256']!=value['snapshot_sha256'] or snapshot['native']['native_body_sha256']!=binding.native_sha256:
            raise ValueError()
        if value['authorization_sha256']!=credit.credit_authorization_digest(value['authorization']): raise ValueError()
        if value['authorization_sha256']!=binding.authorization_sha256: raise ValueError()
        if value['purpose']!=value['authorization']['purpose']: raise ValueError()
        return value,binding,snapshot
    except (OSError,ValueError,KeyError,TypeError):
        raise cli.OpenArtCLIError('credit_dispatch_invalid','original immutable credit dispatch proof missing or changed') from None


def _authorization(root,inputs):
    from lib.production_execution import _artifact_path
    ident=cli._safe_part(inputs.get('credit_authorization_id'))
    return json.loads(_artifact_path(root,'credit_authorization-'+ident+'.json').read_text())


def prepare_dispatch(inputs,checked,attempt_id,deadline):
    """Readonly fresh eligibility/quote before acquiring a project lock."""
    profile,native=checked['openart']; authorization=_authorization(checked['root'],inputs)
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
    rechecked=credit.validate_credit_authorization(checked['root'],prepared['authorization'],scope=checked['scope'],marker=checked['marker'],
        inputs=prepared['original_inputs'],profile=profile,quote_id=prepared['fresh_quote']['quote_id'],
        request_sha256=checked['request_sha256'],occurrence_index=checked['scope_attempt_index'],attempt_id=b.attempt_id)
    if rechecked.binding!=b: raise cli.OpenArtCLIError('credit_authorization_changed','authority changed during reservation')
    records={'request.json':request,'selected_attempts.json':__import__('lib.production_execution',fromlist=['load_selected_attempts']).load_selected_attempts(checked['root'])}
    if checked['contract']: records['shot_contract.json']=checked['contract']
    manifest={'version':'1','attempt_id':b.attempt_id,'binding':asdict(b),'purpose':approval.purpose,
        'authorization':prepared['authorization'],'authorization_sha256':b.authorization_sha256,
        'scope':checked['scope'],'marker':checked['marker'],'original_inputs':prepared['original_inputs'],
        'quote_id':prepared['fresh_quote']['quote_id'],'original_quote_id':prepared['original_inputs']['credit_quote_id'],
        'qualification_sha256':prepared['original_inputs']['credit_qualification_sha256'],
        'snapshot_sha256':frozen['snapshot_sha256'],'journal_records':records}
    cli.write_private(jobs.job_dir(b.attempt_id)/'credit_dispatch.json',json.dumps(manifest,sort_keys=True,separators=(',',':')).encode())
    _checkpoint('private_prepared',b.attempt_id)
    proof=credit._load(manifest['quote_id']); l=ledger()
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
    digest=hashlib.sha256((directory/'request.json').read_bytes()).hexdigest(); l=ledger(); row=l.inspect(attempt_id)
    if row['slot_state']=='prepared': l.mark_ready(JournalReady(b,digest))
    _checkpoint('journal_ready',attempt_id)
    if submitting:
        l.mark_submitting(LaunchIntent(b,digest)); _checkpoint('ledger_submitting',attempt_id)
    repair_outbox(root,attempt_id,publish=False)


def reservation_lookup(project_root,attempt_id,request_sha256):
    """Only trusted durable submitting state can authorize paid launch/nesting."""
    manifest,b,frozen=_manifest(attempt_id); row=ledger().require_submitting(b)
    if (Path(project_root).resolve()!=Path(b.project_root) or request_sha256!=b.request_sha256 or row['slot_state']!='submitting'
            or row['binding_json']!=json.dumps(asdict(b),sort_keys=True,separators=(',',':'))):
        raise cli.OpenArtCLIError('no_active_reservation','matching original submitting credit reservation required')
    native=frozen['native']
    return {'state':'active','slot_state':'submitting','reservation_id':attempt_id,'attempt_id':attempt_id,
        'request_sha256':b.request_sha256,'account_id_sha256':b.account_id,'workspace':b.workspace_id,
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
        ready=[r for r in snap['ready_journals'] if r['attempt_id']==binding.attempt_id]
        if request!=manifest['journal_records']['request.json'] or request.get('attempt_id')!=binding.attempt_id \
                or request.get('request_sha256')!=binding.request_sha256 or len(ready)!=1 \
                or ready[0]['journal_sha256']!=hashlib.sha256(raw).hexdigest():
            raise ValueError()
        return request,snap
    except (OSError,ValueError,KeyError,TypeError,LedgerError):
        raise cli.OpenArtCLIError('journal_not_ready','current original journal differs from immutable ready proof') from None


def validate_prelaunch(project_root,attempt_id,request_sha256,deadline):
    """Under held transport; authority is reread from private original snapshots."""
    manifest,b,frozen=_manifest(attempt_id); reservation_lookup(project_root,attempt_id,request_sha256)
    from lib.production_request import validate_frozen_preparation
    public_request,_=_ready_journal(b,manifest)
    validate_frozen_preparation(public_request,frozen,Path(b.project_root))
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


def record_launch_result(attempt_id):
    """Reconcile qualified original stdout only; billing remains independent."""
    manifest,b,frozen=_manifest(attempt_id); l=ledger(); row=l.inspect(attempt_id)
    job=jobs.original_job_id(attempt_id)
    if job:
        try:
            proof=jobs._verify_raw_submit(attempt_id,jobs.launch_record(attempt_id),job,'credit_original_job_invalid')
        except cli.OpenArtCLIError:
            job=None  # Staged tentative parse is not qualified original-job identity.
        else:
            if row['slot_state'] in {'submitting','uncertain'}:
                l.mark_submitted(OriginalJobReceipt(b,_hash(proof),job)); _checkpoint('ledger_submitted',attempt_id)
    if not job and row['slot_state']=='submitting': l.mark_uncertain(b,_hash(jobs.reconcile_job(attempt_id)))
    return l.inspect(attempt_id)


def repair_outbox(project_root,attempt_id,*,publish=True):
    """Repair original journal publication/inspection only; never advance to launch."""
    from lib import production_execution as execution
    manifest,b,_=_manifest(attempt_id)
    if Path(project_root).resolve()!=Path(b.project_root): raise cli.OpenArtCLIError('credit_origin_mismatch','outbox project mismatch')
    directory=Path(b.project_root)/'production_attempts'/attempt_id; directory.mkdir(parents=True,exist_ok=True)
    if publish:
        for name,record in manifest['journal_records'].items():
            path=directory/name
            if path.exists():
                if json.loads(path.read_text())!=record: raise cli.OpenArtCLIError('journal_conflict','outbox cannot replace changed original journal')
            else: execution._write_new(path,record)
        row=ledger().inspect(attempt_id)
        if row['slot_state']=='prepared':
            digest=hashlib.sha256((directory/'request.json').read_bytes()).hexdigest()
            ledger().mark_ready(JournalReady(b,digest))
    events=directory/'credit_events'; events.mkdir(exist_ok=True)
    for event in ledger().outbox():
        if event['attempt_id']!=attempt_id: continue
        path=events/(event['event_id']+'.json'); record={k:v for k,v in event.items() if k not in {'acknowledged','journal_sha256'}}
        if path.exists():
            if json.loads(path.read_text())!=record: raise cli.OpenArtCLIError('journal_conflict','credit event publication changed')
        else: execution._write_new(path,record)
        ledger().ack_outbox(event['event_id'],b,hashlib.sha256(path.read_bytes()).hexdigest())
    return {'attempt_id':attempt_id,'slot_state':ledger().inspect(attempt_id)['slot_state'],'paid_submission':False}


def offline_readiness(inputs):
    """Retained preparation/quote readiness only; no CLI, ledger, or reservation."""
    profile=jobs.load_qualification(model=inputs.get('model'),mode=inputs.get('mode'),require='pre_submit')
    native=jobs.prepare_native_request(credit._controls(inputs),profile)
    from lib.production_request import validate_preparation
    preparation=validate_preparation(inputs,native,profile)
    quote=credit.get_retained_quote(inputs,profile,inputs.get('credit_quote_id'))
    return {'preparation':preparation,**quote,'provider_calls':0,'reservations':0}


def resolve_attempt(project_root,attempt_id,request_sha256,*,timeout=cli.DEFAULT_TIMEOUT,billing_proof_id=None):
    """Original-attempt resolution: qualified terminal receipt frees slot, not debit."""
    manifest,b,frozen=_manifest(attempt_id)
    if Path(project_root).resolve()!=Path(b.project_root) or request_sha256!=b.request_sha256:
        raise cli.OpenArtCLIError('credit_origin_mismatch','resolution original project/request mismatch')
    jobs.recover_launch(attempt_id); row=record_launch_result(attempt_id); process=jobs.original_process_state(attempt_id)
    if not row['job_id']:
        return {'attempt_id':attempt_id,'slot_state':row['slot_state'],'billing_state':row['debit_state'],'resolution':'unsupported_hold','paid_submission':False}
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
            ledger().release_slot(QualifiedSlotRelease(b,'terminal',evidence,_hash(process),row['job_id']))
        if billing_proof_id is not None:
            proof,contract=_load_billing(billing_proof_id,b,frozen)
            if proof['job_id']!=row['job_id']: raise cli.OpenArtCLIError('billing_origin_mismatch','billing original job changed')
            observed=_billing_observation(b,row['job_id'],contract,account,status,profile)
            ledger().settle(QualifiedBilling(b,observed['debit_evidence'],row['job_id'],observed['amount']))
            _checkpoint('ledger_settled',attempt_id)
            if observed['refund'] is not None:
                ledger().refund(QualifiedRefund(b,observed['refund_evidence'],observed['debit_evidence'],row['job_id'],observed['refund']))
                _checkpoint('ledger_refunded',attempt_id)
    repair_outbox(project_root,attempt_id,publish=False)
    row=ledger().inspect(attempt_id)
    resolution=('terminal_unknown_billing' if row['debit_state']=='unresolved' else 'terminal_billing_resolved') if row['slot_state'] in {'terminal','closed'} else 'active_hold'
    return {'attempt_id':attempt_id,'slot_state':row['slot_state'],'billing_state':row['debit_state'],'resolution':resolution,'paid_submission':False}


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
        rows=[r for r in snap['reservations'] if r['attempt_id']==attempt_id]
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
                'credit':{'slot_state':row['slot_state'],'debit_state':row['debit_state']}}
    except (cli.OpenArtCLIError,LedgerError,OSError,ValueError,KeyError,TypeError):
        return None
