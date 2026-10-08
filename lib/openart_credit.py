"""Retained settings-qualified credit evidence. No submission or reservation here.

Declared JSON paths locate actual provider statements; declarations alone are not
provider guarantees. Only validated raw read-only captures establish quote terms.
Private proofs contain receipt references; public summaries contain opaque hashes.
"""
from __future__ import annotations
import math
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from tools import _openart_cli as cli
from lib import openart_jobs as jobs, openart_setup as setup, openart_qualification as qual
from lib.provider_credit_ledger import Binding, CreditScale, LedgerError, ValidatedReservation

DEFAULT_WORKSPACE='__observed_account_without_workspace__'


def _fail(message,kind='credit_unqualified'):
    raise cli.OpenArtCLIError(kind,message)


def _controls(inputs):
    from lib.production_execution import _openart_controls
    return _openart_controls(inputs)


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class QuoteContract:
    amount_path: str
    quantum_path: str
    balance_path: str
    account_path: str
    workspace_path: str | None
    model_path: str
    mode_path: str
    maximum_path: str
    all_settings_path: str | None = None
    exact_native_path: str | None = None
    account_global_path: str | None = None
    workspace_absence_path: str | None = None

    def validate(self):
        for key,value in asdict(self).items():
            if value is None and key in {'workspace_path','all_settings_path','exact_native_path','account_global_path','workspace_absence_path'}: continue
            if not qual._path(value): _fail('declared typed provider JSON paths required')
        if (self.all_settings_path is None)==(self.exact_native_path is None):
            _fail('one explicit all-settings or exact-native coverage contract required')
        cost_paths=[self.amount_path,self.quantum_path,self.model_path,self.mode_path,self.maximum_path,self.all_settings_path or self.exact_native_path]
        account_paths=[self.account_path,self.balance_path]+([self.workspace_path] if self.workspace_path else [self.account_global_path,self.workspace_absence_path])
        if len(set(cost_paths))!=len(cost_paths) or len(set(account_paths))!=len(account_paths):
            _fail('distinct semantic fields require distinct paths within each raw command')
        if self.workspace_path is None and (self.account_global_path is None or self.workspace_absence_path is None):
            _fail('actual qualified account-global/no-workspace guarantee required')
        if self.workspace_path is not None and (self.account_global_path or self.workspace_absence_path):
            _fail('workspace scope and account-global scope cannot be mixed')


def _entry(kind,receipt):
    return {'kind':kind,'receipt_id':receipt['receipt_id'],'receipt_sha256':receipt['receipt_sha256']}


def _raw(entry,expected):
    try: record=qual._record(entry,'credit_unqualified')
    except qual.OpenArtQualificationError as e: _fail(e.message)
    if record['argv'] != expected+cli.GLOBAL_FLAGS: _fail('credit receipt command mismatch')
    raw=(cli.state_dir()/'streams'/cli._safe_part(record['streams']['stdout'])).read_bytes()
    # Preserve actual decimal lexemes; never convert binary floats to Decimal.
    return json.loads(raw,parse_float=Decimal)


def _terms(native,profile,contract,cost_entry,account_entry):
    contract.validate()
    cost=_raw(cost_entry,cli.model_cost_argv(profile['model'],profile['mode']))
    account=_raw(account_entry,['account'])
    take=lambda obj,path: qual.lookup_path(obj,path)
    identity=take(account,contract.account_path)
    if not qual._text(identity) or hashlib.sha256(identity.encode()).hexdigest()!=profile['account_id_sha256']:
        _fail('quote account differs from reviewed profile')
    if contract.workspace_path is None:
        if take(account,contract.account_global_path) is not True:
            _fail('provider does not guarantee account-global billing without workspace')
        parts=contract.workspace_absence_path.split('.')
        parent=account
        for part in parts[:-1]:
            if not isinstance(parent,dict) or part not in parent: _fail('explicit observed absent workspace field required')
            parent=parent[part]
        if not isinstance(parent,dict) or parts[-1] not in parent or parent[parts[-1]] is not None:
            _fail('provider workspace exists or absence is not explicitly observed')
    workspace=take(account,contract.workspace_path) if contract.workspace_path else DEFAULT_WORKSPACE
    if not qual._text(workspace): _fail('observed billing workspace missing')
    if take(cost,contract.model_path)!=profile['model'] or take(cost,contract.mode_path)!=profile['mode']:
        _fail('quote model/mode mismatch')
    if take(cost,contract.maximum_path) is not True:
        _fail('provider does not guarantee a conservative maximum per generation')
    if contract.all_settings_path:
        if take(cost,contract.all_settings_path) is not True:
            _fail('model-only cost does not cover all supported settings')
        coverage='all_supported_settings'
    else:
        if take(cost,contract.exact_native_path)!=native['native_body_sha256']:
            _fail('provider exact settings quote does not match native body')
        coverage='exact_native_body'
    try:
        scale=CreditScale(take(cost,contract.quantum_path))
        amount=scale.display(scale.units(take(cost,contract.amount_path)))
        balance=scale.display(scale.units(take(account,contract.balance_path)))
    except LedgerError as e: _fail(str(e))
    terms={'version':'1','account_id_sha256':profile['account_id_sha256'],'workspace':workspace,
        'workspace_observed':contract.workspace_path is not None,'model':profile['model'],'mode':profile['mode'],
        'native_body_sha256':native['native_body_sha256'],'native_controls_sha256':native['native_controls_sha256'],
        'native_argv_sha256':native['native_argv_sha256'],'profile_sha256':native['profile_sha256'],
        'tier':profile['tier'],'cli_version':profile['cli_version'],'form_sha256':profile['form_sha256'],
        'form_defaults_sha256':native['form_defaults_sha256'],'amount':amount,'quantum':scale.quantum,
        'coverage':coverage,'maximum_per_generation':True,'contract':asdict(contract)}
    return terms,balance


@dataclass(frozen=True)
class ValidatedCreditAuthorization:
    reservation: ValidatedReservation
    purpose: str
    quote_id: str
    account_receipt: dict

    @property
    def binding(self):
        return self.reservation.binding


def internal_ledger_packet(approval):
    if not isinstance(approval,ValidatedCreditAuthorization):
        _fail('validated exact credit authorization required')
    return approval.reservation


def _path(quote_id):
    return cli.private_dir('credit_quotes')/(cli._safe_part(quote_id)+'.json')


def _load(quote_id):
    try:
        path=_path(quote_id); cli.verify_private_state(); raw=path.read_bytes(); proof=json.loads(raw)
        if proof.get('version')!='1' or proof.get('quote_id')!=quote_id or _hash({k:v for k,v in proof.items() if k!='quote_id'})!=quote_id or _hash(proof['terms'])!=proof['quote_sha256']:
            raise ValueError()
        return proof
    except (OSError,ValueError,KeyError,TypeError): _fail('retained credit proof missing or changed')


def _summary(proof):
    return {'version':'1','credit_state':'retained_quote','quote_id':proof['quote_id'],
            'quote_sha256':proof['quote_sha256'],'proof_sha256':_hash(proof),
            'qualification_sha256':proof['qualification_sha256'],'contract_sha256':proof['contract_sha256'],
            'amount':proof['terms']['amount'],'quantum':proof['terms']['quantum'],
            'account_id_sha256':proof['terms']['account_id_sha256'],
            'workspace_sha256':hashlib.sha256(proof['terms']['workspace'].encode()).hexdigest()}


def get_retained_quote(inputs,profile,quote_id):
    """Offline readiness: zero calls/reservations. Missing quote remains unknown."""
    if not quote_id: return {'credit_state':'quote_required','provider_calls':0,'reservations':0}
    jobs.validate_profile(profile,require='pre_submit')
    native=jobs.prepare_native_request(_controls(inputs),profile)
    proof=_load(quote_id); qualified=_load_qualification(proof['qualification_sha256'],inputs,profile)
    if proof['contract']!=qualified['contract'] or proof['contract_sha256']!=qualified['contract_sha256']: _fail('retained quote differs from qualified contract')
    contract=QuoteContract(**proof['contract'])
    _verify_preview(proof['preview_receipt'],inputs,profile,native)
    terms,balance=_terms(native,profile,contract,proof['cost_receipt'],proof['account_receipt'])
    if terms!=proof['terms'] or balance!=proof['balance']: _fail('retained quote settings/economics changed')
    return _summary(proof)


def _verify_preview(entry,inputs,profile,native):
    _raw(entry,native['creative_argv']+['--dry-run'])
    observed=jobs.native_request(_controls(inputs),profile,dry_run={k:entry[k] for k in ('receipt_id','receipt_sha256')})
    for key in ('native_body_sha256','native_controls_sha256','native_argv_sha256','profile_sha256'):
        if observed[key]!=native[key]: _fail('retained qualification/quote preview differs from exact native request')


def _capture_credit(inputs,profile,contract,deadline):
    native=jobs.prepare_native_request(_controls(inputs),profile)
    remaining=lambda:cli.lock_remaining(deadline)
    setup.verify_current(profile,timeout=remaining())
    with cli.allow_image_reference(native['image_upload'] is not None):
        preview=cli.run_readonly(native['creative_argv']+['--dry-run'],timeout=remaining())
    current=jobs.native_request(_controls(inputs),profile,dry_run={'receipt_id':preview['receipt_id'],'receipt_sha256':preview['receipt_sha256']})
    for key in ('native_body_sha256','native_controls_sha256','native_argv_sha256','profile_sha256'):
        if current[key]!=native[key]: _fail('fresh native preview differs from approved settings')
    account=cli.run_readonly(['account'],timeout=remaining())
    cost=cli.run_readonly(cli.model_cost_argv(profile['model'],profile['mode']),timeout=remaining())
    ce,ae=_entry('credit_cost',cost),_entry('credit_account',account)
    terms,balance=_terms(native,profile,contract,ce,ae)
    return native,terms,balance,ce,ae,preview


def _qualification_path(sha):
    if not isinstance(sha,str) or not qual._SHA.fullmatch(sha): _fail('immutable qualified quote contract proof required')
    return cli.private_dir('credit_contracts')/(sha+'.json')


def qualify_quote_contract(inputs,profile,contract,*,timeout=cli.DEFAULT_TIMEOUT):
    """Explicit nonspending qualification; additive proof preserves creative profile SHA."""
    if not isinstance(contract,QuoteContract): _fail('typed quote contract required')
    contract.validate(); jobs.validate_profile(profile,require='pre_submit')
    timeout=cli.validate_timeout(timeout); deadline=time.monotonic()+timeout
    with cli.transport_lock(wait_timeout=timeout):
        native,terms,balance,ce,ae,preview=_capture_credit(inputs,profile,contract,deadline)
        proof={'version':'1','contract':asdict(contract),'contract_sha256':_hash(asdict(contract)),
            'terms':terms,'balance':balance,'cost_receipt':ce,'account_receipt':ae,
            'preview_receipt':_entry('credit_preview',preview)}
        sha=_hash(proof); path=_qualification_path(sha)
        data=json.dumps(proof,sort_keys=True,separators=(',',':')).encode()
        try: cli.write_private(path,data)
        except FileExistsError:
            if path.read_bytes()!=data: _fail('qualified quote contract proof changed')
    return {'qualification_sha256':sha,'contract_sha256':proof['contract_sha256'],'profile_sha256':native['profile_sha256']}


def _load_qualification(sha,inputs,profile):
    try:
        path=_qualification_path(sha); cli.verify_private_state(); proof=json.loads(path.read_bytes())
        if _hash(proof)!=sha or proof['version']!='1' or _hash(proof['contract'])!=proof['contract_sha256']:
            _fail('qualified quote contract bytes changed')
        native=jobs.prepare_native_request(_controls(inputs),profile)
        contract=QuoteContract(**proof['contract'])
        _verify_preview(proof['preview_receipt'],inputs,profile,native)
        terms,balance=_terms(native,profile,contract,proof['cost_receipt'],proof['account_receipt'])
        if terms!=proof['terms'] or balance!=proof['balance']: _fail('qualified quote contract origin changed')
        return proof
    except (OSError,ValueError,KeyError,TypeError): _fail('qualified quote contract missing or malformed')


def refresh_credit_evidence(inputs,profile,contract=None,*,qualification_sha256=None,approved_quote_id=None,timeout=cli.DEFAULT_TIMEOUT,deadline=None):
    """Explicit readonly refresh requires the immutable qualified contract, not caller labels.

    One deadline and reentrant transport span current eligibility and native preview.
    Raw qualification and fresh quote captures remain private and separate; receipt
    IDs never enter semantic request/quote terms. Later prelaunch calls reuse this
    API under the held transport, outside project/ledger locks.
    """
    entry_time=time.monotonic()
    timeout=cli.validate_timeout(timeout)
    if deadline is not None and (isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline)):
        _fail("finite internal absolute deadline required")
    deadline=min(entry_time+timeout,deadline) if deadline is not None else entry_time+timeout
    cli.lock_remaining(deadline)
    if contract is not None:
        if not isinstance(contract,QuoteContract): _fail('typed quote contract required')
        contract.validate()
    jobs.validate_profile(profile,require='pre_submit')
    qualified=_load_qualification(qualification_sha256,inputs,profile)
    frozen=QuoteContract(**qualified['contract'])
    if contract is not None and contract!=frozen: _fail('caller quote contract differs from qualified proof')
    if approved_quote_id: get_retained_quote(inputs,profile,approved_quote_id)
    with cli.transport_lock(wait_timeout=cli.lock_remaining(deadline)):
        native,terms,balance,ce,ae,preview=_capture_credit(inputs,profile,frozen,deadline)
        if terms!=qualified['terms']: _fail('fresh economics or guarantee differs from qualified contract','credit_quote_changed')
        if approved_quote_id:
            approved=_load(approved_quote_id)
            if terms!=approved['terms'] or approved['qualification_sha256']!=qualification_sha256:
                _fail('fresh quote differs from exact approved proof','credit_quote_changed')
        proof={'version':'1','quote_sha256':_hash(terms),'terms':terms,'balance':balance,
            'contract':asdict(frozen),'qualification_sha256':qualification_sha256,'contract_sha256':qualified['contract_sha256'],
            'cost_receipt':ce,'account_receipt':ae,'preview_receipt':_entry('credit_preview',preview)}
        proof['quote_id']=_hash(proof)
        cli.write_private(_path(proof['quote_id']),json.dumps(proof,sort_keys=True,separators=(',',':')).encode())
    return _summary(proof)


def _inside(root,path):
    candidate=(root/str(path)).resolve()
    if candidate==root or root not in candidate.parents: _fail('approval evidence must be inside project')
    return candidate


def credit_authorization_digest(authorization):
    """Exact terms approved by current strict scope; evidence bytes validated separately.

    Local retained approved scope/evidence is the repository's trusted human
    capture boundary. This API does not establish external proof of a human or
    manufacture approval; rewriting only a sidecar/copy cannot change scope authority.
    """
    return _hash({k:v for k,v in authorization.items() if k!='evidence'})


def canonical_scope_occurrence(authorization,occurrence):
    """Structural consumption identity survives caller ID or approval document rewrites."""
    return _hash({'provider':'openart_cli',**{k:authorization[k] for k in
        ('project_root','project_id','story_revision','scope_id','shot_id','account_id_sha256','workspace')},
        'request_sha256':occurrence['request_sha256'],'index':occurrence['index']})


def validate_credit_authorization(project_root,authorization,*,scope,marker,inputs,profile,quote_id,
                                  request_sha256,occurrence_index,attempt_id):
    """Validate retained credit sidecar independently of ordinary tool/USD approval.

    scope/marker must be the actual current strict preflight objects. Integration
    must call normal production preflight first; this function repeats exact scope
    occurrence and retained approval-byte checks before constructing ledger input.
    """
    from lib import production_execution as execution
    import jsonschema
    root=Path(project_root).resolve()
    schema=Path(__file__).resolve().parents[1]/'schemas/artifacts/credit_authorization.schema.json'
    try: jsonschema.validate(authorization,json.loads(schema.read_text()))
    except (jsonschema.ValidationError,TypeError): _fail('explicit credit authorization sidecar required','credit_authorization_invalid')
    a=authorization
    if scope.get('unknown_cost_authorization_sha256'):
        _fail('scope cannot authorize both exact credit and unknown cost')
    if scope.get('credit_authorization_sha256')!=credit_authorization_digest(a):
        _fail('current approved scope does not authorize these exact credit terms')
    if type(occurrence_index) is not int or occurrence_index<0: _fail('exact request occurrence required')
    if scope.get('status')!='approved' or not scope.get('approved_by') or scope.get('provider')!='openart_cli':
        _fail('matching strict approved OpenArt scope required')
    for field,expected in [('project_root',str(root)),('project_id',marker.get('project_id')),
        ('story_revision',marker.get('story_revision')),('scope_id',scope.get('id'))]:
        if a[field]!=expected: _fail('credit authorization project/story/scope mismatch')
    if scope.get('project_id')!=marker.get('project_id') or scope.get('story_revision')!=marker.get('story_revision'):
        _fail('strict scope project/story mismatch')
    governance=inputs.get('governance',{})
    if governance.get('scope_id')!=scope.get('id') or governance.get('shot_id')!=a['shot_id']:
        _fail('credit authorization shot/scope mismatch')
    credit_evidence_raw = None
    for index, evidence in enumerate((a['evidence'],scope.get('evidence',{}))):
        if not evidence.get('path') or not evidence.get('sha256'): _fail('retained approval evidence required')
        evidence_raw = _inside(root,evidence['path']).read_bytes()
        if hashlib.sha256(evidence_raw).hexdigest()!=evidence['sha256']:
            _fail('retained approval bytes changed')
        if index == 0:
            credit_evidence_raw = evidence_raw
    try:
        credit_evidence=json.loads(credit_evidence_raw)
    except (OSError,ValueError): _fail('explicit structured credit approval capture required')
    expected={'kind':'openart_credit_authorization','terms':{k:v for k,v in a.items() if k!='evidence'}}
    if credit_evidence!=expected:
        _fail('retained approval does not bind these exact credit terms')
    approved=scope.get('requests',{}).get(a['shot_id'])
    if isinstance(approved,list):
        if occurrence_index>=len(approved): _fail('scope request occurrences exhausted')
        approved=approved[occurrence_index]
    if execution._approved_request(approved,root)!=request_sha256 or execution.planned_request_digest(inputs,project_dir=root)!=request_sha256:
        _fail('exact scope request digest mismatch')
    if a['count']!=len(a['occurrences']) or a['count']>scope.get('attempts_per_shot',{}).get(a['shot_id'],0):
        _fail('credit authorization count exceeds approved attempts')
    allowance=scope.get('attempts_per_shot',{}).get(a['shot_id'])
    if type(allowance) is not int or allowance<1: _fail('positive strict scope occurrence allowance required')
    ids=set(); indices=set(); previous=-1
    approved_requests=scope.get('requests',{}).get(a['shot_id'])
    for item in a['occurrences']:
        index=item['index']
        if item['id'] in ids or index in indices or index<=previous or not 0<=index<allowance:
            _fail('all credit occurrences require unique ordered IDs/indices within scope allowance')
        ids.add(item['id']); indices.add(index); previous=index
        if isinstance(approved_requests,list):
            if index>=len(approved_requests): _fail('credit sibling outside exact approved request list')
            expected_request=approved_requests[index]
        else: expected_request=approved_requests
        if execution._approved_request(expected_request,root)!=item['request_sha256']:
            _fail('credit sibling differs from ordered approved scope request')
    matches=[x for x in a['occurrences'] if x['index']==occurrence_index]
    if len(matches)!=1: _fail('credit authorization exact occurrence missing/duplicated')
    occurrence=matches[0]; quote=get_retained_quote(inputs,profile,quote_id); proof=_load(quote_id)
    if a['account_id_sha256']!=proof['terms']['account_id_sha256'] or a['workspace']!=proof['terms']['workspace']:
        _fail('credit authorization observed account/workspace mismatch')
    native=jobs.prepare_native_request(_controls(inputs),profile)
    for field,expected in [('request_sha256',request_sha256),('native_sha256',native['native_body_sha256']),
                          ('profile_sha256',native['profile_sha256']),('quote_sha256',quote['quote_sha256'])]:
        if occurrence[field]!=expected: _fail('credit authorization immutable occurrence mismatch')
    try:
        scale=CreditScale(proof['terms']['quantum'])
        if scale.units(quote['amount'])>scale.units(a['ceiling']) or scale.units(quote['amount'])>scale.units(a['allowance']):
            _fail('credit authorization ceiling/allowance insufficient')
        binding=Binding('openart_cli',a['account_id_sha256'],a['workspace'],str(root),attempt_id,request_sha256,
            canonical_scope_occurrence(a,occurrence),credit_authorization_digest(a),native['native_body_sha256'],native['profile_sha256'],quote['quote_sha256'],
            a['allowance_id'],a['allowance'],a['ceiling'],quote['amount'])
        binding.validate()
    except LedgerError as e: _fail(str(e))
    if execution._OPENART_COMPILED_REQUEST_CHECK is None:
        _fail('current compilation validator unavailable')
    execution._OPENART_COMPILED_REQUEST_CHECK(inputs,native,profile)
    return ValidatedCreditAuthorization(
        ValidatedReservation(binding,_hash({'authorization':a,'quote_proof_sha256':quote['proof_sha256'],'scope':scope})),
        a['purpose'],quote_id,proof['account_receipt'])


def qualify_billing_evidence(*args,**kwargs):
    """Per-job raw debit/refund contract is not yet qualified. Unknown stays held."""
    return {'billing_state':'unsupported_hold','reason':'per_job_billing_contract_unqualified'}


# ---- Explicit unknown-cost authorization (strict opt-in; no ceiling, quote or USD) ----

from lib.provider_credit_ledger import UnpricedBinding, ValidatedUnpricedClaim  # noqa: E402

UNOBSERVED_WORKSPACE='__unobserved_workspace__'
UNKNOWN_EVIDENCE_KIND='openart_unknown_cost_authorization'
_SETTING_KEYS=(('duration','duration'),('resolution','resolution'),('aspect_ratio','aspectRatio'))


def _observed_setting(key,value):
    """Narrow provider-observed settings to safe scalars; anything else is None."""
    if isinstance(value,bool): return None
    if key=='duration':
        if isinstance(value,int): return value if 0<value<=600 else None
        if isinstance(value,Decimal) and value.is_finite() and value==value.to_integral_value():
            n=int(value); return n if 0<n<=600 else None
        return None
    if not isinstance(value,str) or not 0<len(value)<=16: return None
    if key=='resolution':
        return value if all(c.isalnum() for c in value) else None
    parts=value.split(':')
    return value if len(parts)==2 and all(p.isdigit() and 0<len(p)<=4 and int(p)>0 for p in parts) else None


def _uk_fail(message,kind='unknown_cost_unqualified'):
    raise cli.OpenArtCLIError(kind,message)


def _unknown_path(evidence_id,*,create):
    if not isinstance(evidence_id,str) or not qual._SHA.fullmatch(evidence_id): _uk_fail('unknown-cost evidence ID must be 64 hex')
    if create: return cli.private_dir('unknown_cost_evidence')/(evidence_id+'.json')
    root=cli.state_dir(create=False)
    return root/'unknown_cost_evidence'/(evidence_id+'.json')


def _number_text(value):
    if isinstance(value,bool) or not isinstance(value,(int,Decimal,str)): return None
    try: d=Decimal(str(value))
    except Exception: return None
    if not d.is_finite() or d<0: return None
    return str(value)


def _classify(cost,profile,controls):
    """Price evidence is model-level; only an exact settings match is labelled matched.

    Even a matched price is an observation, never a provider-guaranteed charge.
    Maximum/guarantee fields are never read.
    """
    if cost is None or cost=={'supported':False}: return 'unavailable',None
    if not isinstance(cost,dict) or cost.get('currency')!='credits' or not isinstance(cost.get('items'),list):
        _uk_fail('malformed available price envelope')
    items=cost['items']
    if any(not isinstance(i,dict) or not qual._text(i.get('model')) or not qual._text(i.get('mode')) for i in items):
        _uk_fail('malformed available price rows')
    rows=[i for i in items if i.get('model')==profile['model'] and i.get('mode')==profile['mode']]
    if not rows: return 'missing',None
    if len(rows)!=1 or cost.get('currency')!='credits': _uk_fail('malformed available price response')
    row=rows[0]; cfg=row.get('config'); credits=_number_text(row.get('totalCredits'))
    if not isinstance(cfg,dict) or credits is None: _uk_fail('malformed available price amount/settings')
    settings={ours:_observed_setting(ours,cfg.get(theirs)) for ours,theirs in _SETTING_KEYS}
    if not cfg or any(theirs in cfg and settings[ours] is None for ours,theirs in _SETTING_KEYS):
        _uk_fail('malformed available price settings')
    quantity=row.get('quantity'); count=cfg.get('videoCount'); audio=cfg.get('generateAudio')
    if (quantity is not None and (isinstance(quantity,bool) or not isinstance(quantity,(int,Decimal)) or not Decimal(quantity).is_finite() or quantity<1 or Decimal(quantity)!=Decimal(quantity).to_integral_value())) \
            or (count is not None and (type(count) is not int or count<1)) \
            or (audio is not None and type(audio) is not bool):
        _uk_fail('malformed available price count/audio')
    requested={ours:controls.get(ours) for ours,_ in _SETTING_KEYS}
    defaults=profile.get('form_defaults',{})
    expected_count=controls.get('videoCount',defaults.get('videoCount'))
    expected_audio=controls.get('generateAudio',defaults.get('generateAudio'))
    matched=all(requested[k] is not None and requested[k]==settings[k] for k in requested) \
        and quantity==1 and type(expected_count) is int and count is not None and count==expected_count \
        and type(expected_audio) is bool and audio is not None and audio==expected_audio
    if matched: return 'settings_matched',{'credits':credits,'settings':settings,'label':'model_price_for_requested_settings_unguaranteed'}
    return 'mismatched_default',{'credits':credits,'settings':settings,'label':'model_default_not_requested'}


def _unknown_terms(native,profile,controls,cost_entry,account_entry):
    cost=_raw(cost_entry,cli.model_cost_argv(profile['model'],profile['mode']))
    account=_raw(account_entry,['account'])
    identity=qual.lookup_path(account,profile['json_paths']['account_id'])
    if not qual._text(identity) or hashlib.sha256(identity.encode()).hexdigest()!=profile['account_id_sha256']:
        _uk_fail('observed account differs from reviewed profile')
    classification,price=_classify(cost,profile,controls)
    balance=_number_text(account.get('credits')) if isinstance(account,dict) else None
    if balance is None: _uk_fail('fresh account balance observation required (unattributed)')
    terms={'version':'1','requested_charge':'unknown','price_classification':classification,
        'observed_default_price':price,
        'balance_observation':{'credits':balance,'label':'unattributed_observation'},
        'account_id_sha256':profile['account_id_sha256'],'workspace':UNOBSERVED_WORKSPACE,
        'workspace_billing_guarantee':'unverified','model':profile['model'],'mode':profile['mode'],
        'requested_settings':{k:controls.get(k) for k,_ in _SETTING_KEYS},
        'native_body_sha256':native['native_body_sha256'],'native_controls_sha256':native['native_controls_sha256'],
        'native_argv_sha256':native['native_argv_sha256'],'profile_sha256':native['profile_sha256'],
        'cli_version':profile['cli_version'],'form_sha256':profile['form_sha256']}
    return terms


def _unknown_public(proof):
    t=proof['terms']
    return {**{k:t[k] for k in ('version','requested_charge','price_classification','observed_default_price',
        'balance_observation','account_id_sha256','workspace','workspace_billing_guarantee','model','mode',
        'native_body_sha256','profile_sha256')},
        'evidence_id':proof['evidence_id'],'evidence_sha256':_hash(t),'provider_calls':0}


def _unknown_references(inputs,profile,native):
    """Source-bound reference digest (None when reference-free). Only already-qualified
    retained upload proofs bound into the exact native request are accepted."""
    try: references=jobs.native_reference_digest(inputs,profile,native=native)
    except cli.OpenArtCLIError as e: _uk_fail(f'unknown-cost native references unverified: {e.message}')
    if references is None and (native.get('image_upload') or native.get('native_controls',{}).get('image_url_sha256')):
        _uk_fail('unknown-cost native reference binding missing')
    return references


def refresh_unknown_cost_evidence(inputs,profile,*,timeout=cli.DEFAULT_TIMEOUT,deadline=None):
    """Explicit read-only capture under transport serialization: eligibility, native
    preview, account identity/balance and model price. Never submits or reserves.

    The requested charge stays 'unknown'; balances are unattributed observations and
    a mismatched default price proves neither affordability nor insufficiency.
    """
    entry_time=time.monotonic(); timeout=cli.validate_timeout(timeout)
    if deadline is not None and (isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline)):
        _uk_fail('finite internal absolute deadline required')
    deadline=min(entry_time+timeout,deadline) if deadline is not None else entry_time+timeout
    jobs.validate_profile(profile,require='pre_submit')
    controls=_controls(inputs)
    native=jobs.prepare_native_request(controls,profile)
    references=_unknown_references(inputs,profile,native)
    remaining=lambda:cli.lock_remaining(deadline)
    with cli.transport_lock(wait_timeout=remaining()):
        setup.verify_current(profile,timeout=remaining())
        with cli.allow_image_reference(references is not None):
            preview=cli.run_readonly(native['creative_argv']+['--dry-run'],timeout=remaining())
        current=jobs.native_request(controls,profile,dry_run={'receipt_id':preview['receipt_id'],'receipt_sha256':preview['receipt_sha256']})
        for key in ('native_body_sha256','native_controls_sha256','native_argv_sha256','profile_sha256'):
            if current[key]!=native[key]: _uk_fail('fresh native preview differs from approved settings','unknown_cost_evidence_changed')
        account=cli.run_readonly(['account'],timeout=remaining())
        cost=cli.run_readonly(cli.model_cost_argv(profile['model'],profile['mode']),timeout=remaining())
        ce,ae,pe=_entry('unknown_cost_price',cost),_entry('unknown_cost_account',account),_entry('unknown_cost_preview',preview)
        terms=_unknown_terms(native,profile,controls,ce,ae)
        if references is not None:
            if jobs.native_reference_digest(inputs,profile,native=current)!=references:
                _uk_fail('fresh native preview references differ from approved sources','unknown_cost_evidence_changed')
            terms['references_sha256']=references
        proof={'version':'1','kind':'openart_unknown_cost_evidence','terms':terms,
               'cost_receipt':ce,'account_receipt':ae,'preview_receipt':pe}
        proof['evidence_id']=_hash(proof)
        path=_unknown_path(proof['evidence_id'],create=True)
        data=json.dumps(proof,sort_keys=True,separators=(',',':')).encode()
        try: cli.write_private(path,data)
        except FileExistsError:
            if path.read_bytes()!=data: _uk_fail('retained unknown-cost evidence changed')
    # verify_current re-inspects version/account/form (3) + fresh preview/account/cost (3); none generate.
    out=_unknown_public(proof); out.update(provider_calls=6,read_only_cli_calls=6,generation_calls=0)
    return out


def _load_unknown_evidence_proof(evidence_id):
    """PRIVATE raw proof. Pure read: no state/dir creation, private checks, content-addressed bytes."""
    try:
        path=_unknown_path(evidence_id,create=False)
        root=path.parent.parent
        if not root.exists(): raise FileNotFoundError()
        cli._check_private(root,True); cli._check_private(path.parent,True); cli._check_private(path,False)
        proof=json.loads(path.read_bytes())
        if not isinstance(proof,dict) or proof.get('version')!='1' or proof.get('kind')!='openart_unknown_cost_evidence' \
                or proof.get('evidence_id')!=evidence_id or _hash({k:v for k,v in proof.items() if k!='evidence_id'})!=evidence_id:
            raise ValueError()
        return proof
    except cli.OpenArtCLIError: _uk_fail('retained unknown-cost evidence missing or unsafe')
    except (OSError,ValueError,KeyError,TypeError): _uk_fail('retained unknown-cost evidence missing or changed')


def load_unknown_evidence(evidence_id):
    """Public-safe summary of retained evidence (zero calls, creates nothing); never raw receipts."""
    return _unknown_public(_load_unknown_evidence_proof(evidence_id))


def classify_price_evidence(cost,profile,controls):
    """Stable public entry: ('settings_matched'|'mismatched_default'|'missing'|'unavailable', price|None)."""
    return _classify(cost,profile,controls)


def get_retained_unknown_evidence(inputs,profile,evidence_id):
    """Zero-call reverification of retained raw receipts against current native/profile."""
    jobs.validate_profile(profile,require='pre_submit')
    proof=_load_unknown_evidence_proof(evidence_id)
    controls=_controls(inputs); native=jobs.prepare_native_request(controls,profile)
    try:
        _raw(proof['preview_receipt'],native['creative_argv']+['--dry-run'])
        observed=jobs.native_request(controls,profile,dry_run={k:proof['preview_receipt'][k] for k in ('receipt_id','receipt_sha256')})
    except cli.OpenArtCLIError: _uk_fail('retained unknown-cost preview invalid')
    for key in ('native_body_sha256','native_controls_sha256','native_argv_sha256','profile_sha256'):
        if observed[key]!=native[key]: _uk_fail('retained unknown-cost preview differs from exact native request','unknown_cost_evidence_changed')
    try: terms=_unknown_terms(native,profile,controls,proof['cost_receipt'],proof['account_receipt'])
    except cli.OpenArtCLIError as e:
        if e.kind=='unknown_cost_unqualified': raise
        _uk_fail('retained unknown-cost receipts invalid')
    references=_unknown_references(inputs,profile,observed)
    if references is not None: terms['references_sha256']=references
    if terms!=proof['terms']: _uk_fail('retained unknown-cost evidence differs from current request/profile','unknown_cost_evidence_changed')
    return _unknown_public(proof)


def unknown_cost_authorization_digest(authorization):
    return _hash({k:v for k,v in authorization.items() if k!='evidence'})


@dataclass(frozen=True)
class ValidatedUnknownCostAuthorization:
    claim: ValidatedUnpricedClaim
    purpose: str
    evidence_id: str
    authorization: dict

    @property
    def binding(self):
        return self.claim.binding


def internal_unpriced_packet(approval):
    if not isinstance(approval,ValidatedUnknownCostAuthorization): _uk_fail('validated unknown-cost authorization required')
    return approval.claim


def validate_unknown_cost_authorization(project_root,authorization,*,scope,marker,inputs,profile,evidence_id,
                                        request_sha256,occurrence_index,attempt_id):
    """Strict explicit unknown-cost opt-in. Mirrors exact validator; never an amount.

    Binds the retained human capture, current approved scope digest, exact ordered
    occurrence request/native body/profile, observed account and fresh evidence.
    """
    from lib import production_execution as execution
    import jsonschema
    bad=lambda m:_uk_fail(m,'unknown_cost_authorization_invalid')
    root=Path(project_root).resolve()
    schema=Path(__file__).resolve().parents[1]/'schemas/artifacts/unknown_cost_authorization.schema.json'
    try: jsonschema.validate(authorization,json.loads(schema.read_text()))
    except (jsonschema.ValidationError,TypeError): bad('explicit unknown-cost authorization sidecar required')
    a=authorization
    if not isinstance(scope,dict) or not isinstance(marker,dict): bad('current strict scope/marker required')
    if scope.get('credit_authorization_sha256'): bad('scope cannot authorize both exact credit and unknown cost')
    if scope.get('unknown_cost_authorization_sha256')!=unknown_cost_authorization_digest(a):
        bad('current approved scope does not authorize these unknown-cost terms')
    if type(occurrence_index) is not int or occurrence_index<0: bad('exact request occurrence required')
    if scope.get('status')!='approved' or not scope.get('approved_by') or scope.get('provider')!='openart_cli':
        bad('matching strict approved OpenArt scope required')
    for field,expected in [('project_root',str(root)),('project_id',marker.get('project_id')),
        ('story_revision',marker.get('story_revision')),('scope_id',scope.get('id'))]:
        if a[field]!=expected: bad('unknown-cost authorization project/story/scope mismatch')
    if scope.get('project_id')!=marker.get('project_id') or scope.get('story_revision')!=marker.get('story_revision'):
        bad('strict scope project/story mismatch')
    governance=inputs.get('governance',{}) if isinstance(inputs,dict) else {}
    if governance.get('scope_id')!=scope.get('id') or governance.get('shot_id')!=a['shot_id']:
        bad('unknown-cost authorization shot/scope mismatch')
    if a['workspace']!=UNOBSERVED_WORKSPACE: bad('unknown-cost workspace must be explicitly unobserved')
    if a['model']!=profile.get('model') or a['mode']!=profile.get('mode'): bad('unknown-cost model/mode differs from profile')
    raw=None
    for index,evidence in enumerate((a['evidence'],scope.get('evidence') or {})):
        if not evidence.get('path') or not evidence.get('sha256'): bad('retained approval evidence required')
        try: data=_inside(root,evidence['path']).read_bytes()
        except (OSError,cli.OpenArtCLIError): bad('retained approval evidence missing')
        if hashlib.sha256(data).hexdigest()!=evidence['sha256']: bad('retained approval bytes changed')
        if index==0: raw=data
    try: captured=json.loads(raw)
    except ValueError: bad('explicit structured unknown-cost approval capture required')
    if captured!={'kind':UNKNOWN_EVIDENCE_KIND,'terms':{k:v for k,v in a.items() if k!='evidence'}}:
        bad('retained approval does not bind these exact unknown-cost terms')
    allowance=scope.get('attempts_per_shot',{}).get(a['shot_id'])
    if type(allowance) is not int or allowance<1: bad('positive strict scope occurrence allowance required')
    if a['count']!=len(a['occurrences']) or a['count']>allowance: bad('unknown-cost count exceeds approved attempts')
    approved_requests=scope.get('requests',{}).get(a['shot_id'])
    approved=approved_requests
    if isinstance(approved,list):
        if occurrence_index>=len(approved): bad('scope request occurrences exhausted')
        approved=approved[occurrence_index]
    if execution._approved_request(approved,root)!=request_sha256 or execution.planned_request_digest(inputs,project_dir=root)!=request_sha256:
        bad('exact scope request digest mismatch')
    ids=set(); previous=-1
    for item in a['occurrences']:
        index=item['index']
        if item['id'] in ids or index<=previous or not 0<=index<allowance:
            bad('all unknown-cost occurrences require unique ordered IDs/indices within scope allowance')
        ids.add(item['id']); previous=index
        if isinstance(approved_requests,list):
            if index>=len(approved_requests): bad('unknown-cost sibling outside exact approved request list')
            expected_request=approved_requests[index]
        else: expected_request=approved_requests
        if execution._approved_request(expected_request,root)!=item['request_sha256']:
            bad('unknown-cost sibling differs from ordered approved scope request')
    matches=[x for x in a['occurrences'] if x['index']==occurrence_index]
    if len(matches)!=1: bad('unknown-cost exact occurrence missing/duplicated')
    occurrence=matches[0]
    native=jobs.prepare_native_request(_controls(inputs),profile)
    try: references=jobs.native_reference_digest(inputs,profile,native=native)
    except cli.OpenArtCLIError as e: bad(f'unknown-cost native references unverified: {e.message}')
    if references is None:
        if 'references_sha256' in occurrence or native.get('image_upload'): bad('reference-free unknown-cost occurrence must not bind references')
    elif occurrence.get('references_sha256')!=references:
        bad('unknown-cost occurrence references_sha256 differs from exact source-bound native references')
    for field,expected in [('request_sha256',request_sha256),('native_sha256',native['native_body_sha256']),
                          ('profile_sha256',native['profile_sha256'])]:
        if occurrence[field]!=expected: bad('unknown-cost authorization immutable occurrence mismatch')
    evidence=get_retained_unknown_evidence(inputs,profile,evidence_id)
    if a['account_id_sha256']!=evidence['account_id_sha256'] or a['account_id_sha256']!=profile.get('account_id_sha256') \
            or evidence['workspace']!=a['workspace']:
        bad('unknown-cost authorization observed account/workspace mismatch')
    if execution._OPENART_COMPILED_REQUEST_CHECK is None: bad('current compilation validator unavailable')
    execution._OPENART_COMPILED_REQUEST_CHECK(inputs,native,profile)
    if a['purpose']=='generation':
        jobs._proof_or_none(profile)  # optional empirical proof; an invalid proof is reported, never a gate
    elif occurrence_index!=0 or a['count']!=1:
        bad('result qualification requires one first original occurrence')
    digest=unknown_cost_authorization_digest(a)
    try:
        binding=UnpricedBinding('openart_cli',a['account_id_sha256'],a['workspace'],str(root),attempt_id,request_sha256,
            canonical_scope_occurrence(a,occurrence),digest,native['native_body_sha256'],native['profile_sha256'],
            evidence['evidence_sha256'])
        binding.validate()
    except LedgerError as e: bad(str(e))
    validation=_hash({'authorization':a,'evidence_id':evidence_id,'scope':scope})
    return ValidatedUnknownCostAuthorization(ValidatedUnpricedClaim(binding,validation),a['purpose'],evidence_id,a)
