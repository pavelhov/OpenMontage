"""Unknown-cost (unpriced) ledger claims share the exact v1 account slot."""
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from lib.provider_credit_ledger import (CreditLedger, LedgerError, CreditScale, Binding,
    ValidatedReservation, JournalReady, LaunchIntent, OriginalJobReceipt, QualifiedSlotRelease,
    QualifiedBilling, SCHEMA_VERSION, read_existing_snapshot)

D = 'a' * 64
V2_TABLES = ('unpriced_reservations', 'unpriced_claims', 'unpriced_authorization_claims',
             'unpriced_outbox', 'unpriced_job_affinity', 'unpriced_evidence_affinity')


def exact(project='/projects/a', workspace='workspace', occurrence=None):
    return ValidatedReservation(Binding('openart_cli', 'account', workspace, project, str(uuid4()), D,
        occurrence or str(uuid4()), D, D, D, D, 'budget', '10', '3', '2.25'), D)


def unpriced(project='/projects/a', workspace='workspace', occurrence=None, account='account'):
    from lib.provider_credit_ledger import UnpricedBinding, ValidatedUnpricedClaim
    return ValidatedUnpricedClaim(UnpricedBinding('openart_cli', account, workspace, project, str(uuid4()), D,
        occurrence or str(uuid4()), D, D, D, D), D)


def ledger(tmp_path):
    l = CreditLedger(tmp_path / 'private')
    l.observe_account('openart_cli', 'account', 'workspace', CreditScale('0.25'), '40', D)
    return l


def _downgrade(path):
    with sqlite3.connect(path) as db:
        for table in V2_TABLES:
            db.execute('DROP TABLE ' + table)
        db.execute('PRAGMA user_version=1')


def test_schema_version_two_and_unpriced_claim_without_balance_math(tmp_path):
    assert SCHEMA_VERSION == 2
    l = ledger(tmp_path); p = unpriced()
    row = l.reserve_unpriced(p)
    assert row['slot_state'] == 'prepared' and row['billing_state'] == 'unknown'
    assert 'reserved_units' not in row and 'ceiling_units' not in row
    assert l.reserve_unpriced(p) == row
    with pytest.raises(LedgerError, match='replay'):
        l.reserve_unpriced(replace(p, validation_sha256='b' * 64))


def test_unpriced_requires_no_observed_balance_but_refuses_quarantine(tmp_path):
    l = CreditLedger(tmp_path / 'private')
    l.reserve_unpriced(unpriced(account='fresh'))
    l2 = ledger(tmp_path / 'q')
    with sqlite3.connect(l2.path) as db:
        db.execute("INSERT INTO account_quarantine VALUES(?,?)", ('["openart_cli","account"]', 'balance_divergence'))
    with pytest.raises(LedgerError, match='quarantined'):
        l2.reserve_unpriced(unpriced())


@pytest.mark.parametrize('first,second', [('exact', 'unpriced'), ('unpriced', 'exact'), ('unpriced', 'unpriced')])
def test_one_active_slot_across_modes_workspaces_projects(tmp_path, first, second):
    l = ledger(tmp_path)
    l.observe_account('openart_cli', 'account', 'other', CreditScale('0.25'), '40', D)
    make = {'exact': exact, 'unpriced': unpriced}
    reserve = {'exact': l.reserve_prepared, 'unpriced': l.reserve_unpriced}
    reserve[first](make[first]('/projects/a', 'workspace'))
    with pytest.raises(LedgerError, match='active generation claim'):
        reserve[second](make[second]('/projects/b', 'other'))


@pytest.mark.parametrize('first,second', [('exact', 'unpriced'), ('unpriced', 'exact'), ('unpriced', 'unpriced')])
def test_scope_occurrence_consumed_once_across_modes(tmp_path, first, second):
    l = ledger(tmp_path); occ = str(uuid4())
    make = {'exact': exact, 'unpriced': unpriced}
    reserve = {'exact': l.reserve_prepared, 'unpriced': l.reserve_unpriced}
    p = make[first](occurrence=occ); reserve[first](p)
    release = {'exact': l.release_slot, 'unpriced': l.release_unpriced}
    release[first](QualifiedSlotRelease(p.binding, 'proved_not_dispatched', D, D, ''))
    with pytest.raises(LedgerError, match='occurrence already consumed'):
        reserve[second](make[second](occurrence=occ))


def test_unpriced_lifecycle_terminal_releases_slot_billing_stays_unknown(tmp_path):
    l = ledger(tmp_path); p = unpriced(); b = p.binding
    l.reserve_unpriced(p)
    l.mark_unpriced(b, 'ready', D)
    l.mark_unpriced(b, 'submitting', D)
    assert l.require_unpriced_submitting(b)['slot_state'] == 'submitting'
    l.mark_unpriced_submitted(OriginalJobReceipt(b, D, 'job-1'))
    l.release_unpriced(QualifiedSlotRelease(b, 'terminal', D, D, 'job-1'))
    state = l.inspect_unpriced(b.attempt_id)
    assert state['slot_state'] == 'terminal' and state['billing_state'] == 'unknown'
    l.reserve_prepared(exact())
    with sqlite3.connect(l.path) as db:
        assert db.execute('SELECT balance_units,observed_units FROM accounts').fetchone() == (160, 160)
    with pytest.raises(LedgerError):
        l.settle(QualifiedBilling(b, D, 'job-1', '50'))


def test_unpriced_uncertain_stays_held_and_job_affinity_is_shared(tmp_path):
    l = ledger(tmp_path); p = unpriced(); b = p.binding
    l.reserve_unpriced(p); l.mark_unpriced(b, 'ready', D); l.mark_unpriced(b, 'submitting', D)
    l.mark_unpriced(b, 'uncertain', D)
    with pytest.raises(LedgerError, match='submitting'):
        l.require_unpriced_submitting(b)
    with pytest.raises(LedgerError, match='active generation claim'):
        l.reserve_prepared(exact())
    with pytest.raises(LedgerError, match='invalid'):
        l.mark_unpriced(b, 'submitting', D)
    l.mark_unpriced_submitted(OriginalJobReceipt(b, D, 'job-x'))
    l.release_unpriced(QualifiedSlotRelease(b, 'terminal', D, D, 'job-x'))
    e = exact(); l.reserve_prepared(e)
    l.mark_ready(JournalReady(e.binding, D)); l.mark_submitting(LaunchIntent(e.binding, D))
    with pytest.raises(LedgerError, match='another attempt'):
        l.mark_submitted(OriginalJobReceipt(e.binding, D, 'job-x'))


def test_unpriced_inactive_unidentified_closes_slot_billing_unknown(tmp_path):
    l = ledger(tmp_path); p = unpriced(); b = p.binding
    l.reserve_unpriced(p); l.mark_unpriced(b, 'ready', D); l.mark_unpriced(b, 'submitting', D)
    with pytest.raises(LedgerError):
        l.release_unpriced(QualifiedSlotRelease(b, 'proved_not_dispatched', D, D, ''))
    l.release_unpriced(QualifiedSlotRelease(b, 'inactive_unidentified', D, D, ''))
    assert l.inspect_unpriced(b.attempt_id)['billing_state'] == 'unknown'


def test_unpriced_binding_immutable_and_outbox(tmp_path):
    l = ledger(tmp_path); p = unpriced(); l.reserve_unpriced(p)
    with pytest.raises(LedgerError, match='binding'):
        l.mark_unpriced(replace(p.binding, profile_sha256='c' * 64), 'ready', D)
    events = [e for e in l.outbox() if e['attempt_id'] == p.binding.attempt_id]
    assert [e['kind'] for e in events] == ['prepared'] and events[0]['authorization_kind'] == 'unknown_cost'
    l.ack_outbox(events[0]['event_id'], p.binding, D)
    assert l.outbox() == []


def test_v1_populated_migrates_atomically_preserving_rows(tmp_path):
    l = ledger(tmp_path); p = exact(); l.reserve_prepared(p)
    _downgrade(l.path)
    with sqlite3.connect(l.path) as db:
        before = {t: db.execute('SELECT * FROM ' + t).fetchall() for t in ('reservations', 'claims', 'outbox', 'accounts')}
    snap = read_existing_snapshot(tmp_path / 'private')
    assert snap['schema_version'] == 1 and snap['unpriced_reservations'] == []
    with sqlite3.connect(l.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    l2 = CreditLedger(tmp_path / 'private')
    with sqlite3.connect(l2.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert {t: db.execute('SELECT * FROM ' + t).fetchall() for t in before} == before
    with pytest.raises(LedgerError, match='active generation claim'):
        l2.reserve_unpriced(unpriced())


def test_v1_migration_failure_rolls_back(tmp_path, monkeypatch):
    l = ledger(tmp_path); _downgrade(l.path)
    import lib.provider_credit_ledger as mod
    monkeypatch.setattr(mod, '_SCHEMA_V2_ADDITIONS', mod._SCHEMA_V2_ADDITIONS[:2] + ['CREATE TABLE broken(('])
    with pytest.raises(sqlite3.Error):
        CreditLedger(tmp_path / 'private')
    with sqlite3.connect(l.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'unpriced_reservations' not in names


def test_snapshot_reads_unpriced_rows_and_rejects_future(tmp_path):
    l = ledger(tmp_path); p = unpriced(); l.reserve_unpriced(p)
    snap = read_existing_snapshot(tmp_path / 'private')
    assert snap['schema_version'] == 2
    assert snap['unpriced_reservations'][0]['attempt_id'] == p.binding.attempt_id
    assert snap['claims'] == [] and snap['unpriced_claims'][0]['attempt_id'] == p.binding.attempt_id
    with sqlite3.connect(l.path) as db:
        db.execute('PRAGMA user_version=3')
    with pytest.raises(LedgerError, match='version'):
        read_existing_snapshot(tmp_path / 'private')
    with pytest.raises(LedgerError, match='version'):
        CreditLedger(tmp_path / 'private')


def _terminal_unpriced(l, job='job-1', evidence=D):
    p = unpriced(); b = p.binding
    l.reserve_unpriced(p); l.mark_unpriced(b, 'ready', D); l.mark_unpriced(b, 'submitting', D)
    l.mark_unpriced_submitted(OriginalJobReceipt(b, evidence, job))
    l.mark_unpriced_submitted(OriginalJobReceipt(b, evidence, job))  # same-attempt replay is idempotent
    l.release_unpriced(QualifiedSlotRelease(b, 'terminal', D, D, job))
    return p


@pytest.mark.parametrize('released', [False, True])
def test_exact_reservation_refuses_existing_unpriced_attempt_id(tmp_path, released):
    l = ledger(tmp_path)
    p = _terminal_unpriced(l) if released else unpriced()
    if not released:
        l.reserve_unpriced(p)
        l.release_unpriced(QualifiedSlotRelease(p.binding, 'proved_not_dispatched', D, D, ''))
    e = exact()
    same = replace(e, binding=replace(e.binding, attempt_id=p.binding.attempt_id))
    with pytest.raises(LedgerError, match='unpriced'):
        l.reserve_prepared(same)
    assert l.authorization_kind(p.binding.attempt_id) == 'unknown_cost'


def test_original_receipt_evidence_unique_across_modes_unpriced_first(tmp_path):
    l = ledger(tmp_path); receipt = 'e' * 64
    p = _terminal_unpriced(l, 'job-u', receipt)
    e = exact(); l.reserve_prepared(e)
    l.mark_ready(JournalReady(e.binding, D)); l.mark_submitting(LaunchIntent(e.binding, D))
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.mark_submitted(OriginalJobReceipt(e.binding, receipt, 'job-e'))


def test_original_receipt_evidence_unique_across_modes_exact_first(tmp_path):
    l = ledger(tmp_path); receipt = 'e' * 64
    e = exact(); l.reserve_prepared(e)
    l.mark_ready(JournalReady(e.binding, D)); l.mark_submitting(LaunchIntent(e.binding, D))
    l.mark_submitted(OriginalJobReceipt(e.binding, receipt, 'job-e'))
    l.release_slot(QualifiedSlotRelease(e.binding, 'terminal', D, D, 'job-e'))
    p = unpriced(); b = p.binding
    l.reserve_unpriced(p); l.mark_unpriced(b, 'ready', D); l.mark_unpriced(b, 'submitting', D)
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.mark_unpriced_submitted(OriginalJobReceipt(b, receipt, 'job-u'))
    assert l.inspect_unpriced(b.attempt_id)['slot_state'] == 'submitting'


def test_unpriced_billing_requires_typed_qualified_proof(tmp_path):
    from lib.provider_credit_ledger import QualifiedUnpricedBilling
    l = ledger(tmp_path); p = _terminal_unpriced(l, 'job-1'); b = p.binding
    with pytest.raises(LedgerError, match='qualified'):
        l.record_unpriced_billing({'balance_delta': '10'})
    with pytest.raises(LedgerError, match='qualified'):
        l.record_unpriced_billing(QualifiedBilling(b, D, 'job-1', '10'))
    with pytest.raises(LedgerError, match='job'):
        l.record_unpriced_billing(QualifiedUnpricedBilling(b, D, 'job-2', '10'))
    l.record_unpriced_billing(QualifiedUnpricedBilling(b, D, 'job-1', '10'))
    l.record_unpriced_billing(QualifiedUnpricedBilling(b, D, 'job-1', '10'))
    with pytest.raises(LedgerError, match='conflicting'):
        l.record_unpriced_billing(QualifiedUnpricedBilling(b, D, 'job-1', '11'))
    assert l.inspect_unpriced(b.attempt_id)['billing_state'] == 'qualified'


def _submitted_unpriced(l, job, receipt):
    p = unpriced(); b = p.binding
    l.reserve_unpriced(p); l.mark_unpriced(b, 'ready', D); l.mark_unpriced(b, 'submitting', D)
    l.mark_unpriced_submitted(OriginalJobReceipt(b, receipt, job))
    return p


def _submitted_exact(l, job, receipt):
    e = exact(); l.reserve_prepared(e)
    l.mark_ready(JournalReady(e.binding, D)); l.mark_submitting(LaunchIntent(e.binding, D))
    l.mark_submitted(OriginalJobReceipt(e.binding, receipt, job))
    return e


def test_unpriced_release_evidence_cannot_be_replayed_on_another_unpriced_attempt(tmp_path):
    l = ledger(tmp_path); proof = 'f' * 64
    p1 = _submitted_unpriced(l, 'job-1', '1' * 64)
    l.release_unpriced(QualifiedSlotRelease(p1.binding, 'terminal', proof, D, 'job-1'))
    l.release_unpriced(QualifiedSlotRelease(p1.binding, 'terminal', proof, D, 'job-1'))  # same-attempt replay
    p2 = _submitted_unpriced(l, 'job-2', '2' * 64)
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.release_unpriced(QualifiedSlotRelease(p2.binding, 'terminal', proof, D, 'job-2'))
    assert l.inspect_unpriced(p2.binding.attempt_id)['slot_state'] == 'submitted'


def test_exact_release_evidence_cannot_release_unpriced_attempt(tmp_path):
    l = ledger(tmp_path); proof = 'f' * 64
    e = _submitted_exact(l, 'job-e', '1' * 64)
    l.release_slot(QualifiedSlotRelease(e.binding, 'terminal', proof, D, 'job-e'))
    p = _submitted_unpriced(l, 'job-u', '2' * 64)
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.release_unpriced(QualifiedSlotRelease(p.binding, 'terminal', proof, D, 'job-u'))


def test_unpriced_release_evidence_cannot_release_exact_attempt(tmp_path):
    l = ledger(tmp_path); proof = 'f' * 64
    p = _submitted_unpriced(l, 'job-u', '1' * 64)
    l.release_unpriced(QualifiedSlotRelease(p.binding, 'terminal', proof, D, 'job-u'))
    e = _submitted_exact(l, 'job-e', '2' * 64)
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.release_slot(QualifiedSlotRelease(e.binding, 'terminal', proof, D, 'job-e'))


def test_billing_evidence_unique_across_attempts_and_modes(tmp_path):
    from lib.provider_credit_ledger import QualifiedUnpricedBilling
    l = ledger(tmp_path); bill = 'b' * 64
    p1 = _submitted_unpriced(l, 'job-1', '1' * 64)
    l.release_unpriced(QualifiedSlotRelease(p1.binding, 'terminal', '3' * 64, D, 'job-1'))
    l.record_unpriced_billing(QualifiedUnpricedBilling(p1.binding, bill, 'job-1', '10'))
    l.record_unpriced_billing(QualifiedUnpricedBilling(p1.binding, bill, 'job-1', '10'))  # same-attempt replay
    p2 = _submitted_unpriced(l, 'job-2', '2' * 64)
    l.release_unpriced(QualifiedSlotRelease(p2.binding, 'terminal', '4' * 64, D, 'job-2'))
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.record_unpriced_billing(QualifiedUnpricedBilling(p2.binding, bill, 'job-2', '10'))
    assert l.inspect_unpriced(p2.binding.attempt_id)['billing_state'] == 'unknown'
    e = _submitted_exact(l, 'job-e', '5' * 64)
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.settle(QualifiedBilling(e.binding, bill, 'job-e', '2'))
    exact_bill = 'c' * 64
    l.settle(QualifiedBilling(e.binding, exact_bill, 'job-e', '2'))
    l.release_slot(QualifiedSlotRelease(e.binding, 'terminal', '6' * 64, D, 'job-e'))
    p3 = _submitted_unpriced(l, 'job-3', '7' * 64)
    l.release_unpriced(QualifiedSlotRelease(p3.binding, 'terminal', '8' * 64, D, 'job-3'))
    with pytest.raises(LedgerError, match='evidence already belongs'):
        l.record_unpriced_billing(QualifiedUnpricedBilling(p3.binding, exact_bill, 'job-3', '10'))


def test_unpriced_release_job_affinity_shared(tmp_path):
    l = ledger(tmp_path)
    e = _submitted_exact(l, 'job-e', '1' * 64)
    l.release_slot(QualifiedSlotRelease(e.binding, 'terminal', '2' * 64, D, 'job-e'))
    p = unpriced(); b = p.binding
    l.reserve_unpriced(p); l.mark_unpriced(b, 'ready', D); l.mark_unpriced(b, 'submitting', D)
    l.mark_unpriced(b, 'uncertain', D)
    with pytest.raises(LedgerError):
        l.release_unpriced(QualifiedSlotRelease(b, 'terminal', '3' * 64, D, 'job-e'))
