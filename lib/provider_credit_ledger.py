"""Private installation credit ledger, independent of provider transport and journals.

The host must validate raw provider/approval evidence BEFORE constructing the
Validated*/Qualified* packets below. These internal packets are not public user
inputs and do not themselves prove provider statements or human authorization.
No network, filesystem journal reads, callbacks, expiry, or automatic resubmission
occurs here. Host lock order is project lock -> short ledger transaction.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4


class LedgerError(ValueError):
    pass


MAX_UNITS = 2**63 - 1
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise LedgerError('credits require exact decimal strings, Decimal, or integers')
    if len(str(value)) > 128:
        raise LedgerError('credit amount too large')
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError):
        raise LedgerError('invalid credit amount') from None
    if not d.is_finite() or d < 0 or abs(d.as_tuple().exponent) > 18 or len(d.as_tuple().digits) > 38:
        raise LedgerError('negative, nonfinite, or excessive precision/size credits')
    return d


@dataclass(frozen=True)
class CreditScale:
    quantum: str

    def __post_init__(self):
        q = _decimal(self.quantum)
        if q <= 0:
            raise LedgerError('credit quantum must be positive')
        # Normalize by tuple arithmetic, never Decimal.normalize (context rounding).
        sign,digits,exponent=q.as_tuple()
        digits=list(digits)
        while len(digits)>1 and digits[-1]==0:
            digits.pop(); exponent+=1
        object.__setattr__(self, 'quantum', format(Decimal((sign,tuple(digits),exponent)), 'f'))

    def units(self, value):
        # Integer arithmetic avoids Decimal context rounding, including non-power-of-ten quantum.
        d, q = _decimal(value), Decimal(self.quantum)
        exp = min(d.as_tuple().exponent, q.as_tuple().exponent)
        def coefficient(v):
            t = v.as_tuple()
            return int(''.join(map(str, t.digits))) * 10**(t.exponent-exp)
        amount, quantum = coefficient(d), coefficient(q)
        count, remainder = divmod(amount, quantum)
        if remainder or count > MAX_UNITS:
            raise LedgerError('credits exceed quantum precision or integer range')
        return count

    def display(self, units):
        if isinstance(units, bool) or not isinstance(units, int) or not 0 <= units <= MAX_UNITS:
            raise LedgerError('invalid credit unit count')
        q = Decimal(self.quantum).as_tuple()
        coefficient = int(''.join(map(str, q.digits))) * units
        return format(Decimal((0, tuple(map(int, str(coefficient))), q.exponent)), 'f')


def _text(value, label):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value:
        raise LedgerError('invalid '+label)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise LedgerError('invalid evidence digest')


@dataclass(frozen=True)
class Binding:
    provider: str
    account_id: str
    workspace_id: str
    project_root: str
    attempt_id: str
    request_sha256: str
    authorization_occurrence: str
    authorization_sha256: str
    native_sha256: str
    profile_sha256: str
    quote_sha256: str
    allowance_id: str
    allowance: str
    ceiling: str
    quote: str

    def validate(self):
        for field in ('provider', 'account_id', 'workspace_id', 'authorization_occurrence', 'allowance_id'):
            _text(getattr(self, field), field)
        try:
            if str(UUID(self.attempt_id)) != self.attempt_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise LedgerError('attempt must be canonical UUID') from None
        _text(self.project_root,'project root')
        if not Path(self.project_root).is_absolute():
            raise LedgerError('project root must be absolute')
        if os.path.normpath(self.project_root) != self.project_root:
            raise LedgerError('project root must be canonical')
        for field in ('request_sha256', 'authorization_sha256', 'native_sha256', 'profile_sha256', 'quote_sha256'):
            _digest(getattr(self, field))
        for field in ('allowance', 'ceiling', 'quote'):
            if not isinstance(getattr(self, field), str):
                raise LedgerError('bound approved amounts must be decimal strings')
            _decimal(getattr(self, field))

    @property
    def claim_key(self):
        # Generation exclusivity spans billing workspaces for this account.
        return json.dumps([self.provider, self.account_id], separators=(',', ':'))

    @property
    def account_key(self):
        return json.dumps([self.provider, self.account_id, self.workspace_id], separators=(',', ':'))


@dataclass(frozen=True)
class UnpricedBinding:
    """Unknown-cost origin: no allowance, quote, ceiling or amount fields exist.

    Shares the exact-mode account claim key and scope occurrence registry.
    evidence_sha256 binds the fresh account/price/native evidence captured
    under transport serialization before the claim.
    """
    provider: str
    account_id: str
    workspace_id: str
    project_root: str
    attempt_id: str
    request_sha256: str
    authorization_occurrence: str
    authorization_sha256: str
    native_sha256: str
    profile_sha256: str
    evidence_sha256: str

    def validate(self):
        for field in ('provider', 'account_id', 'workspace_id', 'authorization_occurrence'):
            _text(getattr(self, field), field)
        try:
            if str(UUID(self.attempt_id)) != self.attempt_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise LedgerError('attempt must be canonical UUID') from None
        _text(self.project_root, 'project root')
        if not Path(self.project_root).is_absolute() or os.path.normpath(self.project_root) != self.project_root:
            raise LedgerError('project root must be absolute and canonical')
        for field in ('request_sha256', 'authorization_sha256', 'native_sha256', 'profile_sha256', 'evidence_sha256'):
            _digest(getattr(self, field))

    @property
    def claim_key(self):
        return json.dumps([self.provider, self.account_id], separators=(',', ':'))

    @property
    def account_key(self):
        return json.dumps([self.provider, self.account_id, self.workspace_id], separators=(',', ':'))


@dataclass(frozen=True)
class ValidatedUnpricedClaim:
    binding: UnpricedBinding
    validation_sha256: str


@dataclass(frozen=True)
class ValidatedReservation:
    binding: Binding
    validation_sha256: str


@dataclass(frozen=True)
class JournalReady:
    binding: Binding
    journal_sha256: str


@dataclass(frozen=True)
class LaunchIntent:
    binding: Binding
    journal_sha256: str


@dataclass(frozen=True)
class OriginalJobReceipt:
    binding: Binding
    evidence_sha256: str
    job_id: str


@dataclass(frozen=True)
class QualifiedSlotRelease:
    """Host-qualified original process exit + terminal or no-pending acceptance proof.

    terminal requires an original job ID. inactive_unidentified excludes running
    jobs and delayed/pending acceptance but cannot establish billing; it closes
    the slot while retaining the debit hold. proved_not_dispatched is a validated
    pre-launch abort and alone releases both axes.
    """
    binding: Binding
    kind: str
    evidence_sha256: str
    original_process_exit_sha256: str
    job_id: str


@dataclass(frozen=True)
class QualifiedBilling:
    binding: Binding
    evidence_sha256: str
    job_id: str
    amount: str


@dataclass(frozen=True)
class QualifiedUnpricedBilling:
    """Provider per-job billing proof bound to the original unpriced job.

    Account balance deltas are never this proof; they stay unattributed.
    """
    binding: UnpricedBinding
    evidence_sha256: str
    job_id: str
    amount: str


@dataclass(frozen=True)
class QualifiedRefund:
    binding: Binding
    evidence_sha256: str
    debit_evidence_sha256: str
    job_id: str
    amount: str


def _private(path, directory=False):
    try:
        s = path.lstat()
    except FileNotFoundError:
        raise LedgerError('private state disappeared') from None
    expected = stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)
    if not expected or stat.S_ISLNK(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != (0o700 if directory else 0o600):
        raise LedgerError('unsafe private state: '+str(path))


class CreditLedger:
    """One durable slot per provider/account; billing facts are workspace scoped.

    Isolation covers governed local dispatchers only. External website/CLI spending
    can be detected by observation drift but cannot be prevented by SQLite.
    """
    def __init__(self, state_root=None):
        if state_root is None:
            from tools._openart_cli import state_dir
            state_root = state_dir()
        root = Path(os.path.abspath(os.path.expanduser(str(state_root))))
        # Reject symlink ancestors before mkdir/resolve: resolving first hides unsafe links.
        for parent in (root, *root.parents):
            if parent.is_symlink():
                raise LedgerError('symlink in private state path')
        for ancestor in (root,*root.parents):
            if ancestor.exists() and REPO_ROOT.exists() and ancestor.samefile(REPO_ROOT):
                raise LedgerError('ledger state aliases checkout')
        resolved=root.resolve()
        if resolved == REPO_ROOT.resolve() or REPO_ROOT.resolve() in resolved.parents:
            raise LedgerError('ledger state must be outside actual checkout')
        if root == REPO_ROOT or REPO_ROOT in root.parents:
            raise LedgerError('ledger state must be outside checkout')
        missing=[]; current=root
        while not current.exists():
            missing.append(current); current=current.parent
        for p in reversed(missing):
            try: p.mkdir(mode=0o700)
            except FileExistsError: pass
            _private(p, True)
        _private(root, True)
        self.directory=root/'credits'
        try: self.directory.mkdir(mode=0o700)
        except FileExistsError: pass
        _private(self.directory, True)
        self.path=self.directory/'ledger.sqlite3'
        # O_NOFOLLOW and O_EXCL ensure we never follow an existing database symlink.
        try:
            fd=os.open(self.path, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        except FileExistsError: pass
        else: os.close(fd)
        self._verify_files()
        with self._transaction() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, *SUPPORTED_SCHEMA_VERSIONS):
                raise LedgerError('unsupported ledger schema version')
            if version == 0:
                existing=db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if existing: raise LedgerError('unversioned existing ledger schema')
                for sql in _SCHEMA:
                    db.execute(sql)
            if version in (0, 1):
                # Additive v1->v2 upgrade inside the same BEGIN IMMEDIATE: v1 rows untouched.
                for sql in _SCHEMA_V2_ADDITIONS:
                    db.execute(sql)
                db.execute('PRAGMA user_version=2')

    def _verify_files(self):
        _private(self.directory, True)
        _private(self.directory.parent, True)
        for suffix in ('','-wal','-shm','-journal'):
            file=Path(str(self.path)+suffix)
            if file.exists() or file.is_symlink():
                _private(file)

    @contextlib.contextmanager
    def _transaction(self):
        self._verify_files()
        db=sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=15000')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            self._verify_files()
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, row, kind, evidence):
        db.execute('INSERT INTO outbox(event_id,attempt_id,kind,evidence_sha256) VALUES(?,?,?,?)',
                   (str(uuid4()), row['attempt_id'], kind, evidence))

    @staticmethod
    def _bound(db, binding):
        if not isinstance(binding, Binding): raise LedgerError('typed immutable binding required')
        binding.validate()
        row=db.execute('SELECT * FROM reservations WHERE attempt_id=?',(binding.attempt_id,)).fetchone()
        if row is None or row['binding_json'] != _binding_json(binding):
            raise LedgerError('reservation immutable origin binding mismatch')
        return row

    def observe_account(self, provider, account_id, workspace_id, scale, balance, evidence_sha256):
        if not isinstance(scale,CreditScale): raise LedgerError('observed credit scale required')
        for value in (provider,account_id,workspace_id): _text(value,'account identity')
        _digest(evidence_sha256); units=scale.units(balance)
        key=json.dumps([provider,account_id,workspace_id],separators=(',',':'))
        divergence=False
        with self._transaction() as db:
            row=db.execute('SELECT * FROM accounts WHERE account_key=?',(key,)).fetchone()
            net,holds=self._totals(db,key)
            if row:
                if row['quantum'] != scale.quantum:
                    db.execute('UPDATE accounts SET quarantined=1 WHERE account_key=?',(key,))
                    divergence=True
                else:
                    expected=row['balance_units']-(net-row['anchor_units'])
                    pending=sum(r['reserved_units'] for r in db.execute(
                        "SELECT reserved_units FROM reservations WHERE account_key=? AND debit_state='unresolved' AND slot_state IN ('submitting','submitted','uncertain','terminal','closed')",(key,)))
                    if not max(0, expected-pending) <= units <= expected:
                        db.execute('UPDATE accounts SET quarantined=1 WHERE account_key=?',(key,))
                        divergence=True
                    else:
                        # Preserve the original accounting anchor while holds can explain
                        # some of an aggregate balance change. Never attribute that
                        # change to a specific job or double debit later settlement.
                        db.execute('UPDATE accounts SET balance_units=?,anchor_units=?,observed_units=?,observation_sha256=? WHERE account_key=?',
                                   (row['balance_units'] if holds else units, row['anchor_units'] if holds else net, units,evidence_sha256,key))
            else:
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,0)',(key,scale.quantum,units,net,units,evidence_sha256))
            if divergence:
                claim_key=json.dumps([provider,account_id],separators=(',',':'))
                db.execute('INSERT OR REPLACE INTO account_quarantine VALUES(?,?)',(claim_key,'balance_divergence'))
        if divergence: raise LedgerError('account balance/quantum divergence; account quarantined')

    @staticmethod
    def _totals(db,key,allowance=None):
        sql='SELECT reserved_units,charged_units,refunded_units,debit_state FROM reservations WHERE account_key=?'
        args=[key]
        if allowance is not None: sql+=' AND allowance_id=?'; args.append(allowance)
        net=holds=0
        for r in db.execute(sql,args):
            net+=r['charged_units']-r['refunded_units']
            if r['debit_state'] in ('reserved','unresolved'): holds+=r['reserved_units']
        return net,holds

    def reserve_prepared(self, packet):
        if not isinstance(packet,ValidatedReservation): raise LedgerError('host-validated reservation packet required')
        b=packet.binding
        if not isinstance(b,Binding): raise LedgerError('typed binding required')
        b.validate(); _digest(packet.validation_sha256)
        with self._transaction() as db:
            existing=db.execute('SELECT * FROM reservations WHERE attempt_id=?',(b.attempt_id,)).fetchone()
            if existing:
                row=self._bound(db,b)
                if row['validation_sha256']!=packet.validation_sha256: raise LedgerError('validation replay conflict')
                return dict(row)
            if db.execute('SELECT 1 FROM unpriced_reservations WHERE attempt_id=?',(b.attempt_id,)).fetchone():
                raise LedgerError('attempt already bound to unpriced reservation')
            account=db.execute('SELECT * FROM accounts WHERE account_key=?',(b.account_key,)).fetchone()
            if not account: raise LedgerError('observed account required')
            if account['quarantined'] or db.execute('SELECT 1 FROM account_quarantine WHERE claim_key=?',(b.claim_key,)).fetchone():
                raise LedgerError('account quarantined')
            scale=CreditScale(account['quantum'])
            quote,ceiling,allowance=map(scale.units,(b.quote,b.ceiling,b.allowance))
            if quote>ceiling: raise LedgerError('quote exceeds approved ceiling')
            prior=db.execute('SELECT allowance_units FROM allowances WHERE account_key=? AND allowance_id=?',
                             (b.account_key,b.allowance_id)).fetchone()
            if prior and prior[0]!=allowance: raise LedgerError('immutable allowance mismatch')
            net,holds=self._totals(db,b.account_key,b.allowance_id)
            if net+holds+quote>allowance: raise LedgerError('credit allowance exhausted')
            account_net,account_holds=self._totals(db,b.account_key)
            available=min(account['observed_units']-account_holds, account['balance_units']-(account_net-account['anchor_units'])-account_holds)
            if quote>available: raise LedgerError('insufficient observed balance')
            _shared_gates(db,b)
            db.execute('INSERT OR IGNORE INTO allowances VALUES(?,?,?)',(b.account_key,b.allowance_id,allowance))
            db.execute('''INSERT INTO reservations(attempt_id,account_key,authorization_occurrence,allowance_id,
                binding_json,validation_sha256,reserved_units,ceiling_units,slot_state,debit_state)
                VALUES(?,?,?,?,?,?,?,?,'prepared','reserved')''',
                (b.attempt_id,b.account_key,b.authorization_occurrence,b.allowance_id,_binding_json(b),packet.validation_sha256,quote,ceiling))
            db.execute('INSERT INTO claims VALUES(?,?)',(b.claim_key,b.attempt_id))
            db.execute('INSERT INTO authorization_claims VALUES(?,?,?)',(b.claim_key,b.authorization_occurrence,b.attempt_id))
            row=self._bound(db,b); self._event(db,row,'prepared',packet.validation_sha256)
            return dict(row)

    @staticmethod
    def _affinity(db,binding,job_id,kind,evidence):
        if job_id:
            _shared_job_affinity(db,binding,job_id)
            db.execute('INSERT OR IGNORE INTO job_affinity VALUES(?,?,?)',(binding.claim_key,job_id,binding.attempt_id))
        _shared_evidence_affinity(db,binding,evidence)
        db.execute('INSERT OR IGNORE INTO evidence_affinity VALUES(?,?,?,?)',(binding.claim_key,kind,evidence,binding.attempt_id))

    def _slot_transition(self, binding, target, allowed, evidence, job_id=None):
        _digest(evidence)
        with self._transaction() as db:
            row=self._bound(db,binding)
            if row['slot_state']==target:
                if row['slot_evidence_sha256']!=evidence or (job_id is not None and row['job_id']!=job_id):
                    raise LedgerError('conflicting transition replay')
                return
            if row['slot_state'] not in allowed: raise LedgerError('invalid slot transition')
            if target=='submitting' and (db.execute('SELECT quarantined FROM accounts WHERE account_key=?',(row['account_key'],)).fetchone()[0] or db.execute('SELECT 1 FROM account_quarantine WHERE claim_key=?',(binding.claim_key,)).fetchone()):
                raise LedgerError('account quarantined before launch')
            if target=='submitting' and row['slot_evidence_sha256'] != evidence:
                raise LedgerError('launch intent must match authoritative ready journal')
            if job_id is not None and row['billing_job_ref'] and row['billing_job_ref']!=job_id:
                raise LedgerError('identified job contradicts original billing reference')
            if job_id is not None:
                self._affinity(db,binding,job_id,'original_job_receipt',evidence)
            if job_id is not None and row['job_id'] and row['job_id']!=job_id:
                raise LedgerError('original job binding mismatch')
            db.execute('UPDATE reservations SET slot_state=?,slot_evidence_sha256=?,job_id=COALESCE(?,job_id) WHERE attempt_id=?',
                       (target,evidence,job_id,binding.attempt_id))
            if target in ('submitting','uncertain','submitted','terminal'):
                db.execute("UPDATE reservations SET debit_state='unresolved' WHERE attempt_id=? AND debit_state='reserved'",(binding.attempt_id,))
            self._event(db,row,target,evidence)

    def mark_ready(self, proof):
        if not isinstance(proof,JournalReady): raise LedgerError('authoritative ready journal proof required')
        self._slot_transition(proof.binding,'ready',{'prepared'},proof.journal_sha256)

    def mark_submitting(self, proof):
        if not isinstance(proof,LaunchIntent): raise LedgerError('durable launch intent required')
        self._slot_transition(proof.binding,'submitting',{'ready'},proof.journal_sha256)

    def mark_uncertain(self,binding,evidence_sha256):
        self._slot_transition(binding,'uncertain',{'submitting'},evidence_sha256)

    def mark_submitted(self,proof):
        if not isinstance(proof,OriginalJobReceipt): raise LedgerError('validated original job receipt required')
        _text(proof.job_id,'job ID')
        self._slot_transition(proof.binding,'submitted',{'submitting','uncertain'},proof.evidence_sha256,proof.job_id)

    def release_slot(self,proof):
        if not isinstance(proof,QualifiedSlotRelease): raise LedgerError('qualified original process/provider release proof required')
        if proof.kind not in ('terminal','proved_not_dispatched','inactive_unidentified'): raise LedgerError('unqualified release kind')
        _digest(proof.original_process_exit_sha256); _digest(proof.evidence_sha256)
        if proof.kind=='terminal': _text(proof.job_id,'original terminal job')
        elif proof.job_id: raise LedgerError('no-dispatch evidence cannot have a job')
        with self._transaction() as db:
            row=self._bound(db,proof.binding)
            target={'terminal':'terminal','proved_not_dispatched':'no-dispatch','inactive_unidentified':'closed'}[proof.kind]
            serialized=json.dumps(asdict(proof),sort_keys=True,separators=(',',':'))
            if row['slot_state']==target or (row['slot_state']=='closed' and row['release_kind']==proof.kind):
                if row['release_json']!=serialized: raise LedgerError('conflicting release replay')
                return
            if row['slot_state'] not in {'prepared','ready','submitting','submitted','uncertain'}:
                raise LedgerError('invalid slot release transition')
            if target=='terminal' and (not row['job_id'] or row['job_id']!=proof.job_id):
                raise LedgerError('terminal release requires independent original job receipt')
            if target=='terminal' and row['slot_state'] not in {'submitting','submitted','uncertain'}:
                raise LedgerError('terminal evidence before submission')
            if row['job_id'] and row['job_id']!=proof.job_id: raise LedgerError('release original job mismatch')
            if target=='no-dispatch' and row['slot_state'] not in {'prepared','ready'}:
                raise LedgerError('post-launch inactivity cannot release unknown credit hold')
            if target=='closed' and row['slot_state'] not in {'submitting','uncertain'}:
                raise LedgerError('unidentified inactivity requires original uncertain launch')
            if target=='no-dispatch' and row['charged_units']:
                raise LedgerError('charged invocation cannot be proven no-dispatch')
            debit='released' if target=='no-dispatch' else ('unresolved' if row['debit_state']=='reserved' else row['debit_state'])
            self._affinity(db,proof.binding,proof.job_id,'slot_release_'+proof.kind,proof.evidence_sha256)
            db.execute('UPDATE reservations SET slot_state=?,debit_state=?,release_json=?,release_kind=? WHERE attempt_id=?',
                       (target,debit,serialized,proof.kind,proof.binding.attempt_id))
            db.execute('DELETE FROM claims WHERE account_key=? AND attempt_id=?',(proof.binding.claim_key,row['attempt_id']))
            self._event(db,row,target,proof.evidence_sha256)

    def settle(self,proof):
        if not isinstance(proof,QualifiedBilling): raise LedgerError('qualified original billing evidence required')
        _digest(proof.evidence_sha256); _text(proof.job_id,'original job')
        with self._transaction() as db:
            row=self._bound(db,proof.binding)
            scale=CreditScale(db.execute('SELECT quantum FROM accounts WHERE account_key=?',(row['account_key'],)).fetchone()[0])
            amount=scale.units(proof.amount)
            if row['job_id'] and row['job_id']!=proof.job_id: raise LedgerError('billing original job mismatch')
            if row['debit_evidence_sha256']:
                if row['debit_evidence_sha256']!=proof.evidence_sha256 or row['charged_units']!=amount or row['billing_job_ref']!=proof.job_id:
                    raise LedgerError('conflicting settlement')
                return
            if row['release_kind']=='proved_not_dispatched':
                raise LedgerError('proved prelaunch abort cannot receive billing')
            if row['slot_state'] not in {'submitting','submitted','uncertain','terminal','closed'}:
                raise LedgerError('cannot settle an unsubmitted invocation')
            self._affinity(db,proof.binding,proof.job_id,'billing',proof.evidence_sha256)
            violation=amount>row['reserved_units'] or amount>row['ceiling_units']
            db.execute("UPDATE reservations SET charged_units=?,debit_state='settled',debit_evidence_sha256=?,billing_job_ref=?,violation=? WHERE attempt_id=?",
                       (amount,proof.evidence_sha256,proof.job_id,int(violation),row['attempt_id']))
            if violation:
                db.execute('UPDATE accounts SET quarantined=1 WHERE account_key=?',(row['account_key'],))
                db.execute('INSERT OR REPLACE INTO account_quarantine VALUES(?,?)',(proof.binding.claim_key,'billing_violation'))
            self._event(db,row,'settled',proof.evidence_sha256)

    def refund(self,proof):
        if not isinstance(proof,QualifiedRefund): raise LedgerError('qualified debit-bound refund required')
        _digest(proof.evidence_sha256); _digest(proof.debit_evidence_sha256)
        with self._transaction() as db:
            row=self._bound(db,proof.binding)
            if row['debit_evidence_sha256']!=proof.debit_evidence_sha256 or row['billing_job_ref']!=proof.job_id:
                raise LedgerError('refund original debit binding mismatch')
            scale=CreditScale(db.execute('SELECT quantum FROM accounts WHERE account_key=?',(row['account_key'],)).fetchone()[0])
            amount=scale.units(proof.amount)
            prior=db.execute('SELECT units FROM refunds WHERE attempt_id=? AND evidence_sha256=?',(row['attempt_id'],proof.evidence_sha256)).fetchone()
            if prior:
                if prior[0]!=amount: raise LedgerError('conflicting refund replay')
                return
            total=row['refunded_units']+amount
            if total>row['charged_units']: raise LedgerError('refund exceeds actual original debit')
            self._affinity(db,proof.binding,proof.job_id,'refund',proof.evidence_sha256)
            db.execute('INSERT INTO refunds VALUES(?,?,?)',(row['attempt_id'],proof.evidence_sha256,amount))
            db.execute('UPDATE reservations SET refunded_units=?,debit_state=? WHERE attempt_id=?',
                       (total,'refunded' if total==row['charged_units'] else 'settled',row['attempt_id']))
            self._event(db,row,'refunded',proof.evidence_sha256)

    def close(self,binding,evidence_sha256):
        self._slot_transition(binding,'closed',{'terminal','no-dispatch'},evidence_sha256)

    def require_submitting(self,binding):
        """Trusted launch lookup verifies active claim, quarantine and current holds."""
        with self._transaction() as db:
            row=self._bound(db,binding)
            claim=db.execute('SELECT attempt_id FROM claims WHERE account_key=?',(binding.claim_key,)).fetchone()
            account=db.execute('SELECT * FROM accounts WHERE account_key=?',(binding.account_key,)).fetchone()
            if row['slot_state']!='submitting' or not claim or claim[0]!=binding.attempt_id:
                raise LedgerError('matching durable submitting account claim required')
            if account['quarantined'] or db.execute('SELECT 1 FROM account_quarantine WHERE claim_key=?',(binding.claim_key,)).fetchone():
                raise LedgerError('account quarantined before paid launch')
            net,holds=self._totals(db,binding.account_key)
            if min(account['observed_units']-holds, account['balance_units']-(net-account['anchor_units'])-holds)<0:
                raise LedgerError('fresh balance insufficient for all unresolved holds')
            return dict(row)

    def inspect(self,attempt_id):
        with self._transaction() as db:
            row=db.execute('SELECT * FROM reservations WHERE attempt_id=?',(attempt_id,)).fetchone()
            if row is None: raise LedgerError('unknown attempt')
            return dict(row)

    def outbox(self):
        with self._transaction() as db:
            exact=[dict(row,authorization_kind='exact_credit') for row in db.execute('SELECT * FROM outbox WHERE acknowledged=0 ORDER BY sequence')]
            unknown=[dict(row,authorization_kind='unknown_cost') for row in db.execute('SELECT * FROM unpriced_outbox WHERE acknowledged=0 ORDER BY sequence')]
            return exact+unknown

    def ack_outbox(self,event_id,binding,journal_sha256):
        """Host confirms matching durable filesystem repair; original reservation only."""
        _digest(journal_sha256)
        if isinstance(binding,UnpricedBinding):
            with self._transaction() as db:
                self._unpriced_bound(db,binding)
                row=db.execute('SELECT * FROM unpriced_outbox WHERE event_id=?',(event_id,)).fetchone()
                if row is None or row['attempt_id']!=binding.attempt_id: raise LedgerError('outbox origin mismatch')
                if row['acknowledged'] and row['journal_sha256']!=journal_sha256: raise LedgerError('outbox acknowledgment conflict')
                db.execute('UPDATE unpriced_outbox SET acknowledged=1,journal_sha256=? WHERE event_id=?',(journal_sha256,event_id))
            return
        with self._transaction() as db:
            self._bound(db,binding)
            row=db.execute('SELECT * FROM outbox WHERE event_id=?',(event_id,)).fetchone()
            if row is None or row['attempt_id']!=binding.attempt_id: raise LedgerError('outbox origin mismatch')
            if row['acknowledged'] and row['journal_sha256']!=journal_sha256: raise LedgerError('outbox acknowledgment conflict')
            db.execute('UPDATE outbox SET acknowledged=1,journal_sha256=? WHERE event_id=?',(journal_sha256,event_id))


    # ---- Unknown-cost (unpriced) claims: no balance/allowance/ceiling arithmetic ----

    @staticmethod
    def _unpriced_bound(db, binding):
        if not isinstance(binding, UnpricedBinding): raise LedgerError('typed immutable unpriced binding required')
        binding.validate()
        row=db.execute('SELECT * FROM unpriced_reservations WHERE attempt_id=?',(binding.attempt_id,)).fetchone()
        if row is None or row['binding_json'] != _binding_json(binding):
            raise LedgerError('unpriced reservation immutable origin binding mismatch')
        return row

    @staticmethod
    def _unpriced_event(db, attempt_id, kind, evidence):
        db.execute('INSERT INTO unpriced_outbox(event_id,attempt_id,kind,evidence_sha256) VALUES(?,?,?,?)',
                   (str(uuid4()), attempt_id, kind, evidence))

    @staticmethod
    def _quarantined(db, binding):
        if db.execute('SELECT 1 FROM account_quarantine WHERE claim_key=?',(binding.claim_key,)).fetchone():
            return True
        prefix=binding.claim_key[:-1]+','
        return any(r['quarantined'] for r in db.execute('SELECT account_key,quarantined FROM accounts')
                   if r['account_key'].startswith(prefix))

    def reserve_unpriced(self, packet):
        """Claim the shared account slot for an explicitly unknown-cost launch.

        Never reads or writes exact balance anchors, never clears quarantine.
        """
        if not isinstance(packet, ValidatedUnpricedClaim): raise LedgerError('host-validated unpriced claim packet required')
        b=packet.binding
        if not isinstance(b, UnpricedBinding): raise LedgerError('typed unpriced binding required')
        b.validate(); _digest(packet.validation_sha256)
        with self._transaction() as db:
            existing=db.execute('SELECT * FROM unpriced_reservations WHERE attempt_id=?',(b.attempt_id,)).fetchone()
            if existing:
                row=self._unpriced_bound(db,b)
                if row['validation_sha256']!=packet.validation_sha256: raise LedgerError('validation replay conflict')
                return dict(row)
            if db.execute('SELECT 1 FROM reservations WHERE attempt_id=?',(b.attempt_id,)).fetchone():
                raise LedgerError('attempt already bound to exact credit reservation')
            if self._quarantined(db,b): raise LedgerError('account quarantined')
            _shared_gates(db,b)
            db.execute('''INSERT INTO unpriced_reservations(attempt_id,account_key,claim_key,authorization_occurrence,
                binding_json,validation_sha256,slot_state,billing_state) VALUES(?,?,?,?,?,?,'prepared','unknown')''',
                (b.attempt_id,b.account_key,b.claim_key,b.authorization_occurrence,_binding_json(b),packet.validation_sha256))
            db.execute('INSERT INTO unpriced_claims VALUES(?,?)',(b.claim_key,b.attempt_id))
            db.execute('INSERT INTO unpriced_authorization_claims VALUES(?,?,?)',(b.claim_key,b.authorization_occurrence,b.attempt_id))
            self._unpriced_event(db,b.attempt_id,'prepared',packet.validation_sha256)
            return dict(self._unpriced_bound(db,b))

    _UNPRICED_ALLOWED={'ready':{'prepared'},'submitting':{'ready'},'uncertain':{'submitting'},'submitted':{'submitting','uncertain'}}

    def _unpriced_transition(self, binding, target, evidence, job_id=None):
        _digest(evidence)
        with self._transaction() as db:
            row=self._unpriced_bound(db,binding)
            if row['slot_state']==target:
                if row['slot_evidence_sha256']!=evidence or (job_id is not None and row['job_id']!=job_id):
                    raise LedgerError('conflicting transition replay')
                return
            if row['slot_state'] not in self._UNPRICED_ALLOWED[target]: raise LedgerError('invalid slot transition')
            if target=='submitting':
                if self._quarantined(db,binding): raise LedgerError('account quarantined before launch')
                if row['slot_evidence_sha256']!=evidence: raise LedgerError('launch intent must match authoritative ready journal')
            if job_id is not None:
                _text(job_id,'job ID')
                _shared_job_affinity(db,binding,job_id)
                if row['job_id'] and row['job_id']!=job_id: raise LedgerError('original job binding mismatch')
                db.execute('INSERT OR IGNORE INTO unpriced_job_affinity VALUES(?,?,?)',(binding.claim_key,job_id,binding.attempt_id))
                _shared_evidence_affinity(db,binding,evidence)
                db.execute('INSERT OR IGNORE INTO unpriced_evidence_affinity VALUES(?,?,?,?)',
                           (binding.claim_key,'original_job_receipt',evidence,binding.attempt_id))
            db.execute('UPDATE unpriced_reservations SET slot_state=?,slot_evidence_sha256=?,job_id=COALESCE(?,job_id) WHERE attempt_id=?',
                       (target,evidence,job_id,binding.attempt_id))
            self._unpriced_event(db,binding.attempt_id,target,evidence)

    def mark_unpriced(self, binding, target, evidence_sha256):
        if target not in ('ready','submitting','uncertain'): raise LedgerError('invalid unpriced transition target')
        self._unpriced_transition(binding,target,evidence_sha256)

    def mark_unpriced_submitted(self, proof):
        if not isinstance(proof,OriginalJobReceipt) or not isinstance(proof.binding,UnpricedBinding):
            raise LedgerError('validated original unpriced job receipt required')
        self._unpriced_transition(proof.binding,'submitted',proof.evidence_sha256,proof.job_id)

    def release_unpriced(self, proof):
        """Qualified terminal/no-dispatch/inactive evidence releases the slot; billing stays unknown."""
        if not isinstance(proof,QualifiedSlotRelease) or not isinstance(proof.binding,UnpricedBinding):
            raise LedgerError('qualified original unpriced release proof required')
        if proof.kind not in ('terminal','proved_not_dispatched','inactive_unidentified'): raise LedgerError('unqualified release kind')
        _digest(proof.original_process_exit_sha256); _digest(proof.evidence_sha256)
        if proof.kind=='terminal': _text(proof.job_id,'original terminal job')
        elif proof.job_id: raise LedgerError('no-dispatch evidence cannot have a job')
        target={'terminal':'terminal','proved_not_dispatched':'no-dispatch','inactive_unidentified':'closed'}[proof.kind]
        serialized=json.dumps(asdict(proof),sort_keys=True,separators=(',',':'))
        with self._transaction() as db:
            row=self._unpriced_bound(db,proof.binding)
            if row['slot_state']==target:
                if row['release_json']!=serialized: raise LedgerError('conflicting release replay')
                return
            allowed={'terminal':{'submitting','submitted','uncertain'},'no-dispatch':{'prepared','ready'},
                     'closed':{'submitting','uncertain'}}[target]
            if row['slot_state'] not in allowed: raise LedgerError('invalid slot release transition')
            if target=='terminal' and (not row['job_id'] or row['job_id']!=proof.job_id):
                raise LedgerError('terminal release requires independent original job receipt')
            _unpriced_affinity(db,proof.binding,proof.job_id,'slot_release_'+proof.kind,proof.evidence_sha256)
            db.execute('UPDATE unpriced_reservations SET slot_state=?,release_json=?,release_kind=? WHERE attempt_id=?',
                       (target,serialized,proof.kind,proof.binding.attempt_id))
            db.execute('DELETE FROM unpriced_claims WHERE account_key=? AND attempt_id=?',(proof.binding.claim_key,row['attempt_id']))
            self._unpriced_event(db,row['attempt_id'],target,proof.evidence_sha256)

    def record_unpriced_billing(self, proof):
        """Retain authoritative per-job provider billing only when bound to the original job.

        Balance deltas are never accepted here; they remain unattributed observations.
        """
        if not isinstance(proof,QualifiedUnpricedBilling) or not isinstance(proof.binding,UnpricedBinding):
            raise LedgerError('qualified original unpriced billing proof required')
        binding,evidence_sha256,job_id,amount=proof.binding,proof.evidence_sha256,proof.job_id,proof.amount
        _digest(evidence_sha256); _text(job_id,'original job')
        if not isinstance(amount,str): raise LedgerError('billing amount must be provider decimal string')
        _decimal(amount)
        with self._transaction() as db:
            row=self._unpriced_bound(db,binding)
            if row['billing_state']=='qualified':
                if (row['billing_evidence_sha256'],row['billed_amount'],row['job_id'])!=(evidence_sha256,amount,job_id):
                    raise LedgerError('conflicting unpriced billing replay')
                return
            if not row['job_id'] or row['job_id']!=job_id: raise LedgerError('billing original job mismatch')
            _unpriced_affinity(db,binding,job_id,'billing',evidence_sha256)
            db.execute("UPDATE unpriced_reservations SET billing_state='qualified',billing_evidence_sha256=?,billed_amount=? WHERE attempt_id=?",
                       (evidence_sha256,amount,row['attempt_id']))
            self._unpriced_event(db,row['attempt_id'],'billing_qualified',evidence_sha256)

    def require_unpriced_submitting(self, binding):
        with self._transaction() as db:
            row=self._unpriced_bound(db,binding)
            claim=db.execute('SELECT attempt_id FROM unpriced_claims WHERE account_key=?',(binding.claim_key,)).fetchone()
            if row['slot_state']!='submitting' or not claim or claim[0]!=binding.attempt_id:
                raise LedgerError('matching durable submitting account claim required')
            if self._quarantined(db,binding): raise LedgerError('account quarantined before paid launch')
            return dict(row)

    def inspect_unpriced(self, attempt_id):
        with self._transaction() as db:
            row=db.execute('SELECT * FROM unpriced_reservations WHERE attempt_id=?',(attempt_id,)).fetchone()
            if row is None: raise LedgerError('unknown attempt')
            return dict(row)

    def authorization_kind(self, attempt_id):
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM reservations WHERE attempt_id=?',(attempt_id,)).fetchone(): return 'exact_credit'
            if db.execute('SELECT 1 FROM unpriced_reservations WHERE attempt_id=?',(attempt_id,)).fetchone(): return 'unknown_cost'
        raise LedgerError('unknown attempt')


def _shared_gates(db, binding):
    """One active account slot and one occurrence consumption across exact and unpriced modes."""
    for table in ('claims','unpriced_claims'):
        if db.execute('SELECT 1 FROM '+table+' WHERE account_key=?',(binding.claim_key,)).fetchone():
            raise LedgerError('account already has active generation claim')
    for table in ('authorization_claims','unpriced_authorization_claims'):
        if db.execute('SELECT 1 FROM '+table+' WHERE claim_key=? AND authorization_occurrence=?',
                      (binding.claim_key,binding.authorization_occurrence)).fetchone():
            raise LedgerError('authorization occurrence already consumed')


def _shared_job_affinity(db, binding, job_id):
    for table in ('job_affinity','unpriced_job_affinity'):
        prior=db.execute('SELECT attempt_id FROM '+table+' WHERE claim_key=? AND job_id=?',(binding.claim_key,job_id)).fetchone()
        if prior and prior[0]!=binding.attempt_id: raise LedgerError('provider job already belongs to another attempt')


def _shared_evidence_affinity(db, binding, evidence):
    """Original receipt evidence belongs to exactly one attempt across exact and unpriced modes."""
    for table in ('evidence_affinity','unpriced_evidence_affinity'):
        prior=db.execute('SELECT attempt_id FROM '+table+' WHERE claim_key=? AND evidence_sha256=?',(binding.claim_key,evidence)).fetchone()
        if prior and prior[0]!=binding.attempt_id: raise LedgerError('provider evidence already belongs to another attempt')


def _unpriced_affinity(db, binding, job_id, kind, evidence):
    """Release/billing proof is bound to one attempt across exact and unpriced modes."""
    if job_id:
        _shared_job_affinity(db,binding,job_id)
        db.execute('INSERT OR IGNORE INTO unpriced_job_affinity VALUES(?,?,?)',(binding.claim_key,job_id,binding.attempt_id))
    _shared_evidence_affinity(db,binding,evidence)
    db.execute('INSERT OR IGNORE INTO unpriced_evidence_affinity VALUES(?,?,?,?)',(binding.claim_key,kind,evidence,binding.attempt_id))


def _binding_json(binding):
    return json.dumps(asdict(binding),sort_keys=True,separators=(',',':'))


_SCHEMA = [
'''CREATE TABLE accounts(account_key TEXT PRIMARY KEY,quantum TEXT NOT NULL,
 balance_units INTEGER NOT NULL CHECK(balance_units>=0),anchor_units INTEGER NOT NULL,
 observed_units INTEGER NOT NULL CHECK(observed_units>=0),observation_sha256 TEXT NOT NULL,quarantined INTEGER NOT NULL CHECK(quarantined IN(0,1)))''',
'''CREATE TABLE allowances(account_key TEXT NOT NULL REFERENCES accounts(account_key),
 allowance_id TEXT NOT NULL,allowance_units INTEGER NOT NULL CHECK(allowance_units>=0),PRIMARY KEY(account_key,allowance_id))''',
'''CREATE TABLE reservations(attempt_id TEXT PRIMARY KEY,record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version=1),account_key TEXT NOT NULL REFERENCES accounts(account_key),
 authorization_occurrence TEXT NOT NULL,allowance_id TEXT NOT NULL,binding_json TEXT NOT NULL,validation_sha256 TEXT NOT NULL,
 reserved_units INTEGER NOT NULL CHECK(reserved_units>=0),ceiling_units INTEGER NOT NULL CHECK(ceiling_units>=0),
 charged_units INTEGER NOT NULL DEFAULT 0 CHECK(charged_units>=0),refunded_units INTEGER NOT NULL DEFAULT 0 CHECK(refunded_units>=0),
 slot_state TEXT NOT NULL CHECK(slot_state IN('prepared','ready','submitting','submitted','uncertain','terminal','closed','no-dispatch')),
 debit_state TEXT NOT NULL CHECK(debit_state IN('reserved','unresolved','settled','refunded','released')),
 slot_evidence_sha256 TEXT NOT NULL DEFAULT '',debit_evidence_sha256 TEXT NOT NULL DEFAULT '',job_id TEXT NOT NULL DEFAULT '',
 release_json TEXT NOT NULL DEFAULT '',release_kind TEXT NOT NULL DEFAULT '',billing_job_ref TEXT NOT NULL DEFAULT '',violation INTEGER NOT NULL DEFAULT 0 CHECK(violation IN(0,1)),
 UNIQUE(account_key,authorization_occurrence))''',
'''CREATE TABLE claims(account_key TEXT PRIMARY KEY,
 attempt_id TEXT UNIQUE NOT NULL REFERENCES reservations(attempt_id))''',
'''CREATE TABLE account_quarantine(claim_key TEXT PRIMARY KEY,reason TEXT NOT NULL)''',
'''CREATE TABLE job_affinity(claim_key TEXT NOT NULL,job_id TEXT NOT NULL,attempt_id TEXT NOT NULL REFERENCES reservations(attempt_id),PRIMARY KEY(claim_key,job_id))''',
'''CREATE TABLE evidence_affinity(claim_key TEXT NOT NULL,kind TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,attempt_id TEXT NOT NULL REFERENCES reservations(attempt_id),PRIMARY KEY(claim_key,kind,evidence_sha256))''',
'''CREATE TABLE authorization_claims(claim_key TEXT NOT NULL,authorization_occurrence TEXT NOT NULL,
 attempt_id TEXT UNIQUE NOT NULL REFERENCES reservations(attempt_id),PRIMARY KEY(claim_key,authorization_occurrence))''',
'''CREATE TABLE refunds(attempt_id TEXT NOT NULL REFERENCES reservations(attempt_id),
 evidence_sha256 TEXT NOT NULL,units INTEGER NOT NULL CHECK(units>=0),PRIMARY KEY(attempt_id,evidence_sha256))''',
'''CREATE TABLE outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version=1),event_id TEXT UNIQUE NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES reservations(attempt_id),kind TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,
 acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN(0,1)),journal_sha256 TEXT NOT NULL DEFAULT '')''',
]


_SCHEMA_V2_ADDITIONS = [
'''CREATE TABLE unpriced_reservations(attempt_id TEXT PRIMARY KEY,record_version INTEGER NOT NULL DEFAULT 2 CHECK(record_version=2),
 account_key TEXT NOT NULL,claim_key TEXT NOT NULL,authorization_occurrence TEXT NOT NULL,binding_json TEXT NOT NULL,validation_sha256 TEXT NOT NULL,
 slot_state TEXT NOT NULL CHECK(slot_state IN('prepared','ready','submitting','submitted','uncertain','terminal','closed','no-dispatch')),
 billing_state TEXT NOT NULL CHECK(billing_state IN('unknown','qualified')),
 slot_evidence_sha256 TEXT NOT NULL DEFAULT '',job_id TEXT NOT NULL DEFAULT '',release_json TEXT NOT NULL DEFAULT '',release_kind TEXT NOT NULL DEFAULT '',
 billing_evidence_sha256 TEXT NOT NULL DEFAULT '',billed_amount TEXT NOT NULL DEFAULT '',
 UNIQUE(claim_key,authorization_occurrence))''',
'''CREATE TABLE unpriced_claims(account_key TEXT PRIMARY KEY,
 attempt_id TEXT UNIQUE NOT NULL REFERENCES unpriced_reservations(attempt_id))''',
'''CREATE TABLE unpriced_authorization_claims(claim_key TEXT NOT NULL,authorization_occurrence TEXT NOT NULL,
 attempt_id TEXT UNIQUE NOT NULL REFERENCES unpriced_reservations(attempt_id),PRIMARY KEY(claim_key,authorization_occurrence))''',
'''CREATE TABLE unpriced_job_affinity(claim_key TEXT NOT NULL,job_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES unpriced_reservations(attempt_id),PRIMARY KEY(claim_key,job_id))''',
'''CREATE TABLE unpriced_evidence_affinity(claim_key TEXT NOT NULL,kind TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES unpriced_reservations(attempt_id),PRIMARY KEY(claim_key,kind,evidence_sha256))''',
'''CREATE TABLE unpriced_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES unpriced_reservations(attempt_id),kind TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,
 acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN(0,1)),journal_sha256 TEXT NOT NULL DEFAULT '')''',
]


def read_existing_snapshot(state_root=None):
    """Inspect existing WAL state without CLI, schema initialization or row writes.

    SQLite mode=ro reads the current WAL, unlike immutable=1. SQLite may manage
    read-side WAL/SHM bookkeeping for an existing DB; private modes are checked
    before and after. An absent root/database creates no directory or file.
    """
    if state_root is None:
        from tools._openart_cli import state_dir
        state_root=state_dir(create=False)
    root=Path(os.path.abspath(os.path.expanduser(str(state_root))))
    for ancestor in (root,*root.parents):
        if ancestor.is_symlink():raise LedgerError('symlink in private state path')
        if ancestor.exists() and REPO_ROOT.exists() and ancestor.samefile(REPO_ROOT):
            raise LedgerError('ledger state aliases checkout')
    resolved=root.resolve();repo=REPO_ROOT.resolve()
    if resolved==repo or repo in resolved.parents:raise LedgerError('ledger state must be outside actual checkout')
    empty={'initialized':False,'schema_version':None,'reservations':[],'outbox':[],
           'accounts':[],'account_quarantine':[],'claims':[],'ready_journals':[],
           'unpriced_reservations':[],'unpriced_claims':[],'unpriced_outbox':[],'unpriced_ready_journals':[]}
    if not root.exists():return empty
    _private(root,True);directory=root/'credits';path=directory/'ledger.sqlite3'
    if not directory.exists() and not directory.is_symlink():return empty
    _private(directory,True)
    def verify():
        for suffix in ('','-wal','-shm','-journal'):
            candidate=Path(str(path)+suffix)
            if candidate.exists() or candidate.is_symlink():_private(candidate)
    verify()
    if not path.exists():
        if any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
            raise LedgerError('orphan SQLite sidefiles without existing ledger database')
        return empty
    db=None
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version not in SUPPORTED_SCHEMA_VERSIONS:raise LedgerError('unsupported ledger schema version')
        result={'initialized':True,'schema_version':version}
        for table in ('reservations','accounts','account_quarantine','claims'):
            result[table]=[dict(row) for row in db.execute('SELECT * FROM '+table)]
        result['outbox']=[dict(row) for row in db.execute('SELECT * FROM outbox WHERE acknowledged=0 ORDER BY rowid')]
        result['ready_journals']=[dict(row) for row in db.execute("SELECT attempt_id,evidence_sha256 AS journal_sha256 FROM outbox WHERE kind='ready' ORDER BY rowid")]
        result.update(unpriced_reservations=[],unpriced_claims=[],unpriced_outbox=[],unpriced_ready_journals=[])
        if version>=2:
            result['unpriced_reservations']=[dict(row) for row in db.execute('SELECT * FROM unpriced_reservations')]
            result['unpriced_claims']=[dict(row) for row in db.execute('SELECT * FROM unpriced_claims')]
            result['unpriced_outbox']=[dict(row) for row in db.execute('SELECT * FROM unpriced_outbox WHERE acknowledged=0 ORDER BY rowid')]
            result['unpriced_ready_journals']=[dict(row) for row in db.execute("SELECT attempt_id,evidence_sha256 AS journal_sha256 FROM unpriced_outbox WHERE kind='ready' ORDER BY rowid")]
        verify();return result
    except sqlite3.Error as exc:
        raise LedgerError('invalid existing ledger snapshot: '+str(exc)) from None
    finally:
        if db is not None:db.close()
