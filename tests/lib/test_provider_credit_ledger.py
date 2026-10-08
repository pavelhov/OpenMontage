"""Offline ledger proofs; typed evidence represents validation by the host adapter."""
import multiprocessing as mp
import os
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from lib.provider_credit_ledger import (CreditLedger, LedgerError, CreditScale, Binding,
    ValidatedReservation, JournalReady, LaunchIntent, OriginalJobReceipt,
    QualifiedSlotRelease, QualifiedBilling, QualifiedRefund)

D = 'a' * 64

def packet(project='/projects/a', *, quote='2.25', allowance='10', ceiling='3', occurrence=None):
    b = Binding('openart_cli', 'account', 'workspace', project, str(uuid4()), D,
        occurrence or str(uuid4()), D, D, D, D, 'budget', allowance, ceiling, quote)
    return ValidatedReservation(b, D)


def ledger(tmp_path):
    l = CreditLedger(tmp_path / 'private')
    l.observe_account('openart_cli', 'account', 'workspace', CreditScale('0.25'), '20', D)
    return l


def ready(l, p):
    l.mark_ready(JournalReady(p.binding, D))
    l.mark_submitting(LaunchIntent(p.binding, D))


def terminal(l, p, job='job'):
    state=l.inspect(p.binding.attempt_id)
    if not state['job_id']:
        l.mark_submitted(OriginalJobReceipt(p.binding,D,job))
    l.release_slot(QualifiedSlotRelease(p.binding, 'terminal', D, D, job))


def test_exact_amounts():
    s = CreditScale('0.25')
    assert s.units('1.75') == 7
    assert s.display(7) == '1.75'
    for bad in [True, 1.0, '-1', 'NaN', 'Infinity', '0.01', '1e1000']:
        with pytest.raises(LedgerError):
            s.units(bad)
    for bad in ['0', '-1', True, 0.1, 'NaN']:
        with pytest.raises(LedgerError):
            CreditScale(bad)


def test_reservation_replay_and_occurrence(tmp_path):
    l = ledger(tmp_path); p = packet()
    first = l.reserve_prepared(p)
    assert first['slot_state'] == 'prepared'
    assert l.reserve_prepared(p) == first
    assert len(l.outbox()) == 1
    with pytest.raises(LedgerError):
        l.reserve_prepared(packet(occurrence=p.binding.authorization_occurrence))
    with pytest.raises(LedgerError):
        l.reserve_prepared(replace(p, binding=replace(p.binding, quote='2.5')))


def test_terminal_unknown_keeps_hold_and_further_allowance(tmp_path):
    l = ledger(tmp_path); p = packet(allowance='4')
    l.reserve_prepared(p); ready(l,p); terminal(l,p)
    assert l.inspect(p.binding.attempt_id)['debit_state'] == 'unresolved'
    with pytest.raises(LedgerError, match='allowance'):
        l.reserve_prepared(packet(allowance='4'))
    q = packet(quote='1', allowance='4'); l.reserve_prepared(q)
    assert l.inspect(q.binding.attempt_id)['reserved_units'] == 4


def test_balance_ceiling_and_allowance(tmp_path):
    l = ledger(tmp_path)
    for p in [packet(quote='4', ceiling='3'), packet(quote='21', ceiling='30', allowance='30'),
              packet(quote='2', allowance='1')]:
        with pytest.raises(LedgerError): l.reserve_prepared(p)
    assert l.outbox() == []


def test_full_overcharge_quarantine_and_refund(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p)
    l.mark_submitted(OriginalJobReceipt(p.binding,D,'job'))
    e=QualifiedBilling(p.binding,D,'job','5')
    l.settle(e); l.settle(e)
    assert l.inspect(p.binding.attempt_id)['charged_units'] == 20
    terminal(l,p)
    with pytest.raises(LedgerError, match='quarantin'): l.reserve_prepared(packet())
    with pytest.raises(LedgerError): l.settle(replace(e, amount='4'))
    r=QualifiedRefund(p.binding,'b'*64,D,'job','1')
    l.refund(r); l.refund(r)
    assert l.inspect(p.binding.attempt_id)['refunded_units'] == 4
    with pytest.raises(LedgerError): l.refund(replace(r, evidence_sha256='c'*64, amount='5'))


def test_unknown_never_expires_or_manual_unlock(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p)
    l.mark_uncertain(p.binding,D)
    reopened=CreditLedger(tmp_path/'private')
    with pytest.raises(LedgerError): reopened.reserve_prepared(packet())
    with pytest.raises(LedgerError): reopened.release_slot({'purpose':'manual attestation'})
    with pytest.raises(LedgerError): reopened.release_slot(QualifiedSlotRelease(p.binding,'empty_history',D,D,''))
    assert reopened.inspect(p.binding.attempt_id)['slot_state']=='uncertain'


def test_durable_boundaries_and_outbox_repair(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    for state, action in [('prepared',lambda: l.mark_ready(JournalReady(p.binding,D))),
                          ('ready',lambda: l.mark_submitting(LaunchIntent(p.binding,D))),
                          ('submitting',lambda: l.mark_uncertain(p.binding,D)),
                          ('uncertain',lambda: l.mark_submitted(OriginalJobReceipt(p.binding,D,'job')))]:
        assert CreditLedger(tmp_path/'private').inspect(p.binding.attempt_id)['slot_state']==state
        action()
    terminal(l,p)
    l.settle(QualifiedBilling(p.binding,D,'job','2'))
    assert CreditLedger(tmp_path/'private').inspect(p.binding.attempt_id)['debit_state']=='settled'
    assert len(l.outbox()) >= 7
    event=l.outbox()[0]; l.ack_outbox(event['event_id'],p.binding,D)
    assert event['event_id'] not in [x['event_id'] for x in l.outbox()]


def test_drift_quarantine(tmp_path):
    l=ledger(tmp_path)
    with pytest.raises(LedgerError, match='divergence'):
        l.observe_account('openart_cli','account','workspace',CreditScale('0.25'),'19',D)
    with pytest.raises(LedgerError, match='quarantin'): l.reserve_prepared(packet())


def _race(root, project, barrier, queue):
    try:
        l=CreditLedger(Path(root)); p=packet(project)
        barrier.wait(timeout=15)
        l.reserve_prepared(p); queue.put('claimed')
    except LedgerError:
        queue.put('blocked')
    except BaseException as e:
        queue.put(type(e).__name__+':'+str(e))


def test_two_process_two_project_one_account(tmp_path):
    l=ledger(tmp_path); ctx=mp.get_context('spawn'); barrier=ctx.Barrier(2); q=ctx.Queue()
    ps=[ctx.Process(target=_race,args=(str(tmp_path/'private'),'/projects/'+n,barrier,q)) for n in ['a','b']]
    for p in ps: p.start()
    result=[q.get(timeout=20) for _ in ps]
    for p in ps: p.join(timeout=20); assert p.exitcode==0
    assert sorted(result)==['blocked','claimed']


def test_private_files_and_unsafe_sidefiles(tmp_path):
    l=ledger(tmp_path)
    assert (l.path.stat().st_mode & 0o777)==0o600
    assert (l.path.parent.stat().st_mode & 0o777)==0o700
    side=Path(str(l.path)+'-wal'); side.write_text('bad'); side.chmod(0o644)
    with pytest.raises(LedgerError): CreditLedger(tmp_path/'private')


def test_symlink_state_rejected(tmp_path):
    other=tmp_path/'other'; other.mkdir(mode=0o700)
    link=tmp_path/'private'; link.symlink_to(other,target_is_directory=True)
    with pytest.raises(LedgerError): CreditLedger(link)


def test_pending_balance_observation_then_actual_settlement(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p)
    l.observe_account('openart_cli','account','workspace',CreditScale('0.25'),'17.75','b'*64)
    terminal(l,p)
    l.settle(QualifiedBilling(p.binding,D,'job','2.25'))
    # Aggregate balance could have reflected this pending hold already; never attribute
    # that balance delta as a separate debit or double count the eventual actual receipt.
    l.observe_account('openart_cli','account','workspace',CreditScale('0.25'),'17.75','c'*64)
    l.reserve_prepared(packet())


def test_launch_intent_must_match_authoritative_ready_journal(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    l.mark_ready(JournalReady(p.binding,D))
    with pytest.raises(LedgerError):
        l.mark_submitting(LaunchIntent(p.binding,'b'*64))


def test_wrong_origin_and_refund_without_debit(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    with pytest.raises(LedgerError): l.mark_ready(JournalReady(replace(p.binding,project_root='/other'),D))
    with pytest.raises(LedgerError): l.refund(QualifiedRefund(p.binding,D,D,'job','1'))
    assert l.inspect(p.binding.attempt_id)['slot_state']=='prepared'


@pytest.mark.parametrize('suffix',['-wal','-shm','-journal'])
def test_symlink_database_sidefile_rejected(tmp_path,suffix):
    l=ledger(tmp_path); other=tmp_path/'other'; other.write_text('safe'); other.chmod(0o600)
    side=Path(str(l.path)+suffix)
    side.unlink(missing_ok=True)
    side.symlink_to(other)
    with pytest.raises(LedgerError): CreditLedger(tmp_path/'private')


def test_claim_shared_across_billing_workspaces(tmp_path):
    l=ledger(tmp_path)
    l.observe_account('openart_cli','account','other',CreditScale('0.25'),'20',D)
    p=packet(); l.reserve_prepared(p)
    q=packet(); q=replace(q,binding=replace(q.binding,workspace_id='other'))
    with pytest.raises(LedgerError,match='active generation'): l.reserve_prepared(q)


def test_unidentified_inactivity_releases_slot_only(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p); l.mark_uncertain(p.binding,D)
    with pytest.raises(LedgerError):
        l.release_slot(QualifiedSlotRelease(p.binding,'proved_not_dispatched',D,D,''))
    l.release_slot(QualifiedSlotRelease(p.binding,'inactive_unidentified',D,D,''))
    assert l.inspect(p.binding.attempt_id)['slot_state']=='closed'
    assert l.inspect(p.binding.attempt_id)['debit_state']=='unresolved'
    l.reserve_prepared(packet())


def test_lower_balance_cannot_reanchor_repeated_unknown_holds(tmp_path):
    l=CreditLedger(tmp_path/'private')
    l.observe_account('openart_cli','account','workspace',CreditScale('1'),'1000',D)
    p=packet(quote='100',ceiling='100',allowance='1000'); l.reserve_prepared(p); ready(l,p)
    l.observe_account('openart_cli','account','workspace',CreditScale('1'),'900',D)
    with pytest.raises(LedgerError,match='divergence'):
        l.observe_account('openart_cli','account','workspace',CreditScale('1'),'800',D)


def test_prelaunch_abort_releases_both_axes(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    l.release_slot(QualifiedSlotRelease(p.binding,'proved_not_dispatched',D,D,''))
    assert l.inspect(p.binding.attempt_id)['debit_state']=='released'
    assert l.inspect(p.binding.attempt_id)['slot_state']=='no-dispatch'


def test_authorization_occurrence_cannot_replay_in_other_workspace(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    l.release_slot(QualifiedSlotRelease(p.binding,'proved_not_dispatched',D,D,''))
    l.observe_account('openart_cli','account','other',CreditScale('0.25'),'20',D)
    q=packet(occurrence=p.binding.authorization_occurrence)
    q=replace(q,binding=replace(q.binding,workspace_id='other'))
    with pytest.raises(LedgerError,match='occurrence'): l.reserve_prepared(q)


def test_fractional_units_without_decimal_context_rounding():
    s=CreditScale('0.000000000000000001')
    assert s.units('9.223372036854775807') == 2**63-1
    assert s.display(2**63-1)=='9.223372036854775807'
    with pytest.raises(LedgerError): s.units('9.223372036854775808')


def test_wrong_billing_account_and_failed_job_still_charged(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p)
    with pytest.raises(LedgerError):
        l.settle(QualifiedBilling(replace(p.binding,account_id='wrong'),D,'failed-job','1'))
    l.settle(QualifiedBilling(p.binding,D,'failed-job','1'))
    terminal(l,p,'failed-job')
    assert l.inspect(p.binding.attempt_id)['charged_units']==4
    assert l.inspect(p.binding.attempt_id)['debit_state']=='settled'


def test_prelaunch_hold_cannot_explain_external_balance_drift(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    with pytest.raises(LedgerError,match='divergence'):
        l.observe_account('openart_cli','account','workspace',CreditScale('0.25'),'17.75',D)
    l.mark_ready(JournalReady(p.binding,D))
    with pytest.raises(LedgerError,match='quarantin'): l.mark_submitting(LaunchIntent(p.binding,D))


def test_overquote_quarantines_every_workspace_of_account(tmp_path):
    l=ledger(tmp_path); l.observe_account('openart_cli','account','other',CreditScale('0.25'),'20',D)
    p=packet(); l.reserve_prepared(p); ready(l,p); l.settle(QualifiedBilling(p.binding,D,'job','5')); terminal(l,p)
    q=packet(); q=replace(q,binding=replace(q.binding,workspace_id='other'))
    with pytest.raises(LedgerError,match='quarantin'): l.reserve_prepared(q)


def test_billing_does_not_identify_or_release_unknown_job(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p); l.mark_uncertain(p.binding,D)
    l.settle(QualifiedBilling(p.binding,D,'billing-job','1'))
    assert l.inspect(p.binding.attempt_id)['job_id']==''
    with pytest.raises(LedgerError): l.release_slot(QualifiedSlotRelease(p.binding,'terminal',D,D,'billing-job'))
    with pytest.raises(LedgerError): l.mark_submitted(OriginalJobReceipt(p.binding,'b'*64,'different-job'))
    l.mark_submitted(OriginalJobReceipt(p.binding,'b'*64,'billing-job'))
    terminal(l,p,'billing-job')


def test_closed_unknown_stays_unknown_after_settlement(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p); l.mark_uncertain(p.binding,D)
    l.release_slot(QualifiedSlotRelease(p.binding,'inactive_unidentified',D,D,''))
    l.settle(QualifiedBilling(p.binding,D,'job','1'))
    assert l.inspect(p.binding.attempt_id)['slot_state']=='closed'
    assert l.inspect(p.binding.attempt_id)['job_id']==''


def test_closed_prelaunch_abort_cannot_accept_billing(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p)
    l.release_slot(QualifiedSlotRelease(p.binding,'proved_not_dispatched',D,D,'')); l.close(p.binding,D)
    with pytest.raises(LedgerError): l.settle(QualifiedBilling(p.binding,D,'job','1'))


def test_terminal_release_replay_after_close(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p); terminal(l,p); l.close(p.binding,D)
    terminal(l,p)


def test_one_provider_job_cannot_bind_multiple_attempts(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p)
    l.mark_submitted(OriginalJobReceipt(p.binding,D,'same-job')); terminal(l,p,'same-job')
    q=packet(); l.reserve_prepared(q); ready(l,q)
    with pytest.raises(LedgerError): l.mark_submitted(OriginalJobReceipt(q.binding,'b'*64,'same-job'))


def test_one_billing_evidence_cannot_settle_multiple_attempts(tmp_path):
    l=ledger(tmp_path); ps=[]
    for index in range(2):
        p=packet(); l.reserve_prepared(p); ready(l,p); l.mark_uncertain(p.binding,D)
        l.release_slot(QualifiedSlotRelease(p.binding,'inactive_unidentified',str(index+1)*64,D,'')); ps.append(p)
    l.settle(QualifiedBilling(ps[0].binding,D,'job-a','1'))
    with pytest.raises(LedgerError): l.settle(QualifiedBilling(ps[1].binding,D,'job-b','1'))


def test_equivalent_quantum_does_not_quarantine(tmp_path):
    l=CreditLedger(tmp_path/'private'); l.observe_account('openart_cli','account','workspace',CreditScale('1'),'1000',D)
    l.observe_account('openart_cli','account','workspace',CreditScale('1.0'),'1000',D)
    assert CreditScale('0.250').quantum==CreditScale('0.25').quantum


def test_checkout_case_alias_rejected_before_mkdir(tmp_path,monkeypatch):
    # Simulate case-insensitive filesystem samefile without creating checkout state.
    from lib import provider_credit_ledger as module
    root=tmp_path/'OPENMONTAGE'; root.mkdir(mode=0o700)
    actual=tmp_path/'openmontage'; actual.mkdir(mode=0o700,exist_ok=True)
    monkeypatch.setattr(module,'REPO_ROOT',actual)
    original=Path.samefile
    monkeypatch.setattr(Path,'samefile',lambda self,other:True if self==root and other==actual else original(self,other))
    with pytest.raises(LedgerError): CreditLedger(root/'private')
    assert not (root/'private').exists()


def test_fresh_lower_balance_still_subtracts_all_unknown_holds(tmp_path):
    l=CreditLedger(tmp_path/'private'); l.observe_account('openart_cli','account','workspace',CreditScale('1'),'1000',D)
    p=packet(quote='100',ceiling='100',allowance='1000'); l.reserve_prepared(p); ready(l,p)
    l.observe_account('openart_cli','account','workspace',CreditScale('1'),'900',D); terminal(l,p)
    with pytest.raises(LedgerError,match='balance'):
        l.reserve_prepared(packet(quote='900',ceiling='900',allowance='1000'))
    l.reserve_prepared(packet(quote='800',ceiling='800',allowance='1000'))


def test_known_submitted_job_never_downgrades_on_transport_error(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p)
    l.mark_submitted(OriginalJobReceipt(p.binding,D,'job'))
    with pytest.raises(LedgerError): l.mark_uncertain(p.binding,'b'*64)
    assert l.inspect(p.binding.attempt_id)['slot_state']=='submitted'


def test_unrelated_private_os_metadata_does_not_block_ledger(tmp_path):
    l=ledger(tmp_path); unrelated=l.directory/'.DS_Store'; unrelated.write_text('metadata'); unrelated.chmod(0o644)
    assert CreditLedger(tmp_path/'private').path==l.path


def test_late_charge_quarantine_blocks_other_workspace_already_ready(tmp_path):
    l=ledger(tmp_path); l.observe_account('openart_cli','account','other',CreditScale('0.25'),'20',D)
    p=packet(); l.reserve_prepared(p); ready(l,p); terminal(l,p)
    q=packet(); q=replace(q,binding=replace(q.binding,workspace_id='other')); l.reserve_prepared(q); l.mark_ready(JournalReady(q.binding,'b'*64))
    l.settle(QualifiedBilling(p.binding,D,'job','5'))
    with pytest.raises(LedgerError,match='quarantin'): l.mark_submitting(LaunchIntent(q.binding,'b'*64))


def test_settlement_replay_cannot_change_unknown_billing_job(tmp_path):
    l=ledger(tmp_path); p=packet(); l.reserve_prepared(p); ready(l,p); l.mark_uncertain(p.binding,D)
    l.settle(QualifiedBilling(p.binding,D,'job-a','1'))
    with pytest.raises(LedgerError): l.settle(QualifiedBilling(p.binding,D,'job-b','1'))


def test_large_quantum_normalization_does_not_round():
    quantum='12345678901234567890123456789012345678'
    scale=CreditScale(quantum)
    assert scale.quantum==quantum
    assert scale.units(quantum)==1
    assert scale.display(1)==quantum


def test_read_existing_snapshot_absent_creates_no_files(tmp_path):
    from lib.provider_credit_ledger import read_existing_snapshot
    root=tmp_path/'absent'/'private'
    result=read_existing_snapshot(root)
    assert result['initialized'] is False and result['reservations']==[] and result['outbox']==[]
    assert not root.parent.exists()


def test_read_existing_snapshot_reads_current_wal_without_row_mutation(tmp_path):
    from lib.provider_credit_ledger import read_existing_snapshot
    l=ledger(tmp_path);p=packet();l.reserve_prepared(p)
    # Hold a real writer open so the latest commit remains in WAL.
    db=sqlite3.connect(l.path);db.execute('PRAGMA journal_mode=WAL')
    db.execute("UPDATE accounts SET observation_sha256=?",('f'*64,));db.commit()
    before=db.execute('SELECT * FROM reservations').fetchall()
    snapshot=read_existing_snapshot(tmp_path/'private')
    assert snapshot['initialized'] is True
    assert snapshot['accounts'][0]['observation_sha256']=='f'*64
    assert snapshot['reservations'][0]['attempt_id']==p.binding.attempt_id
    assert snapshot['outbox'][0]['attempt_id']==p.binding.attempt_id
    assert snapshot['claims'][0]['attempt_id']==p.binding.attempt_id
    assert db.execute('SELECT * FROM reservations').fetchall()==before
    db.close()


def test_read_existing_snapshot_rejects_unsafe_sidefile_and_future_schema(tmp_path):
    from lib.provider_credit_ledger import read_existing_snapshot
    l=ledger(tmp_path)
    side=Path(str(l.path)+'-journal');side.symlink_to(tmp_path/'outside')
    with pytest.raises(LedgerError,match='unsafe'):read_existing_snapshot(tmp_path/'private')
    side.unlink()
    db=sqlite3.connect(l.path);db.execute('PRAGMA user_version=99');db.commit();db.close()
    with pytest.raises(LedgerError,match='version'):read_existing_snapshot(tmp_path/'private')
    db=sqlite3.connect(l.path);assert db.execute('PRAGMA user_version').fetchone()[0]==99;db.close()
