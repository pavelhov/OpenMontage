"""Unknown-cost shared motion guard; all evidence/provider seams are offline mocks."""
import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from lib import openart_dispatch as dispatch, production_video_guard as guard
from lib.provider_credit_ledger import (CreditLedger, LedgerError, UnpricedBinding,
    ValidatedUnpricedClaim, QualifiedSlotRelease, OriginalJobReceipt,
    CreditScale, read_existing_snapshot)
from tests.lib.test_provider_credit_ledger import packet
from tests.lib.test_production_video_guard import journal
from tests.lib.test_production_execution import project
from tests.lib.test_production_video_guard_integration import GrokMotion, approve, invoke
from lib import production_execution as execution

D = 'a' * 64


@pytest.fixture
def unknown_original(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    private = tmp_path / 'private'
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(private))
    ledger = CreditLedger(private)
    binding = UnpricedBinding('openart_cli', 'account', 'workspace', str(root),
        str(uuid4()), D, str(uuid4()), D, D, D, D)
    ledger.reserve_unpriced(ValidatedUnpricedClaim(binding, D))
    request = {'attempt_id': binding.attempt_id, 'request_sha256': D,
        'shot_id': 'entry', 'scope_id': 'scope', 'media_kind': 'motion',
        'scope': {'provider': 'openart_cli'}}
    manifest = {'authorization_kind': 'unknown_cost',
        'authorization': {'shot_id': 'entry', 'scope_id': 'scope'},
        'journal_records': {'request.json': request}}
    monkeypatch.setattr(dispatch, '_manifest', lambda attempt: (manifest, binding, {}))
    ready_calls = []
    def ready(b, m):
        ready_calls.append(b.attempt_id)
        assert b == binding and m == manifest
        raw = (root / 'production_attempts' / b.attempt_id / 'request.json').read_bytes()
        snap = read_existing_snapshot()
        proofs = [r for r in snap['unpriced_ready_journals'] if r['attempt_id'] == b.attempt_id]
        if (json.loads(raw) != request or len(proofs) != 1
                or proofs[0]['journal_sha256'] != hashlib.sha256(raw).hexdigest()):
            raise ValueError('mock immutable ready proof mismatch')
        return request, snap
    monkeypatch.setattr(dispatch, '_ready_journal', ready)
    monkeypatch.setattr(dispatch, 'existing_terminal_state', lambda *args: None)
    def forbidden(*args, **kwargs):
        raise AssertionError('guard initialized state or called provider')
    monkeypatch.setattr(dispatch, 'ledger', forbidden)
    monkeypatch.setattr(dispatch.cli, 'run_readonly', forbidden)
    return root, ledger, binding, manifest, ready_calls


def publish_ready(original):
    root, ledger, binding, manifest, _ = original
    directory = root / 'production_attempts' / binding.attempt_id
    directory.mkdir(parents=True)
    path = directory / 'request.json'
    path.write_text(json.dumps(manifest['journal_records']['request.json']))
    ledger.mark_unpriced(binding, 'ready', hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def test_unknown_private_prepared_publication_crash_blocks(unknown_original):
    root, ledger, binding, _, ready_calls = unknown_original
    before = read_existing_snapshot()
    reasons = guard.read_video_duplicate_blocks(root, 'entry')
    assert len(reasons) == 1 and 'prepared' in reasons[0] and binding.attempt_id in reasons[0]
    assert ready_calls == []
    assert not (root / 'production_attempts').exists()
    assert read_existing_snapshot() == before


@pytest.mark.parametrize('state', ['ready', 'submitting', 'uncertain'])
def test_unknown_pending_cannot_be_overridden_by_public_failed(unknown_original, state):
    root, ledger, binding, _, ready_calls = unknown_original
    path = publish_ready(unknown_original)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if state != 'ready': ledger.mark_unpriced(binding, 'submitting', digest)
    if state == 'uncertain': ledger.mark_unpriced(binding, 'uncertain', D)
    (path.parent / 'result.json').write_text(json.dumps({'status': 'failed'}))
    reasons = guard.read_video_duplicate_blocks(root, 'entry')
    assert len(reasons) == 1 and state in reasons[0]
    assert ready_calls == [binding.attempt_id]


def test_unknown_terminal_acceptance_independent_of_unknown_billing(unknown_original, monkeypatch):
    root, ledger, binding, _, _ = unknown_original
    path = publish_ready(unknown_original)
    ledger.mark_unpriced(binding, 'submitting', hashlib.sha256(path.read_bytes()).hexdigest())
    ledger.mark_unpriced_submitted(OriginalJobReceipt(binding, D, 'original-job'))
    ledger.release_unpriced(QualifiedSlotRelease(binding, 'terminal', D, D, 'original-job'))
    # The actual dispatcher verifies the qualified original; this fixture mocks
    # that verification seam, never a real provider result or billing proof.
    monkeypatch.setattr(dispatch, 'existing_terminal_state', lambda *args: {'status': 'terminal_unselected'})
    assert ledger.inspect_unpriced(binding.attempt_id)['billing_state'] == 'unknown'
    assert guard.read_video_duplicate_blocks(root, 'entry') == []
    path.write_bytes(path.read_bytes() + b'\n')
    assert 'unverifiable' in guard.read_video_duplicate_blocks(root, 'entry')[0]


@pytest.mark.parametrize('ready', [False, True])
def test_unknown_proven_no_dispatch_has_no_fake_accounting(unknown_original, ready):
    root, ledger, binding, _, _ = unknown_original
    if ready: publish_ready(unknown_original)
    ledger.release_unpriced(QualifiedSlotRelease(binding, 'proved_not_dispatched', D, D, ''))
    row = ledger.inspect_unpriced(binding.attempt_id)
    assert 'debit_state' not in row and 'charged_units' not in row
    assert guard.read_video_duplicate_blocks(root, 'entry') == []
    # Release preserves the occurrence registry even though acceptance is clear.
    with pytest.raises(LedgerError, match='occurrence'):
        ledger.reserve_unpriced(ValidatedUnpricedClaim(replace(binding, attempt_id=str(uuid4())), D))
    directory = dispatch.jobs.job_dir(binding.attempt_id)
    (directory / 'launch.json').write_text('{}')
    assert guard.read_video_duplicate_blocks(root, 'entry')


@pytest.mark.parametrize('mutation', ['claim', 'submitted_event', 'release_binding', 'release_evidence'])
def test_unknown_no_dispatch_rejects_contradictory_private_proof(unknown_original, monkeypatch, mutation):
    root, ledger, binding, _, _ = unknown_original
    ledger.release_unpriced(QualifiedSlotRelease(binding, 'proved_not_dispatched', D, D, ''))
    snapshot = read_existing_snapshot()
    row = snapshot['unpriced_reservations'][0]
    if mutation == 'claim':
        snapshot['unpriced_claims'].append({'account_key': binding.claim_key, 'attempt_id': binding.attempt_id})
    elif mutation == 'submitted_event':
        snapshot['unpriced_outbox'].append({'attempt_id': binding.attempt_id, 'kind': 'submitted'})
    else:
        release = json.loads(row['release_json'])
        if mutation == 'release_binding': release['binding']['native_sha256'] = 'b' * 64
        else: release['evidence_sha256'] = 'invalid'
        row['release_json'] = json.dumps(release, sort_keys=True, separators=(',', ':'))
    monkeypatch.setattr(guard, 'read_existing_snapshot', lambda: snapshot)
    assert guard.read_video_duplicate_blocks(root, 'entry')


@pytest.mark.parametrize('mutation', ['binding', 'shot', 'scope', 'kind', 'authorization_kind', 'slot_state'])
def test_unknown_malformed_private_origin_fails_closed(unknown_original, monkeypatch, mutation):
    root, _, binding, manifest, _ = unknown_original
    if mutation in {'binding', 'slot_state'}:
        snapshot = read_existing_snapshot()
        if mutation == 'binding':
            raw = json.loads(snapshot['unpriced_reservations'][0]['binding_json'])
            raw['evidence_sha256'] = 'invalid'
            snapshot['unpriced_reservations'][0]['binding_json'] = json.dumps(raw)
        else: snapshot['unpriced_reservations'][0]['slot_state'] = 'invented'
        monkeypatch.setattr(guard, 'read_existing_snapshot', lambda: snapshot)
    elif mutation == 'authorization_kind': manifest['authorization_kind'] = 'exact_credit'
    else:
        key = {'shot': 'shot_id', 'scope': 'scope_id', 'kind': 'media_kind'}[mutation]
        manifest['journal_records']['request.json'][key] = 'other'
    assert 'unverifiable' in guard.read_video_duplicate_blocks(root, 'entry')[0]


@pytest.mark.parametrize('key', ['unpriced_claims', 'unpriced_outbox'])
def test_unknown_orphan_private_pending_fails_closed(unknown_original, monkeypatch, key):
    root, _, binding, _, _ = unknown_original
    snapshot = read_existing_snapshot()
    snapshot['unpriced_reservations'] = []
    snapshot['unpriced_claims'] = []
    snapshot['unpriced_outbox'] = []
    snapshot[key] = [{'attempt_id': binding.attempt_id}]
    monkeypatch.setattr(guard, 'read_existing_snapshot', lambda: snapshot)
    assert 'without a reservation origin' in guard.read_video_duplicate_blocks(root, 'entry')[0]


def test_mixed_unknown_private_and_exact_public_history(unknown_original):
    root, _, binding, _, _ = unknown_original
    journal(root, status='failed', provider='openart_cli')
    reasons = guard.read_video_duplicate_blocks(root, 'entry')
    assert len(reasons) == 1 and binding.attempt_id in reasons[0]
    assert guard.read_video_duplicate_blocks(root, 'other') == []


def test_mixed_typed_private_origins_share_global_shot_guard(unknown_original, monkeypatch):
    root, ledger, binding, manifest, _ = unknown_original
    exact = packet(project=str(root))
    exact = replace(exact, binding=replace(exact.binding, account_id='different-account'))
    ledger.observe_account('openart_cli', 'different-account', 'workspace', CreditScale('0.25'), '20', D)
    ledger.reserve_prepared(exact)
    exact_manifest = {'authorization': {'shot_id': 'other', 'scope_id': 'exact-scope'},
        'journal_records': {'request.json': {'attempt_id': exact.binding.attempt_id,
            'request_sha256': D, 'shot_id': 'other', 'scope_id': 'exact-scope',
            'media_kind': 'motion', 'scope': {'provider': 'openart_cli'}}}}
    def original(attempt):
        if attempt == binding.attempt_id: return manifest, binding, {}
        assert attempt == exact.binding.attempt_id
        return exact_manifest, exact.binding, {}
    monkeypatch.setattr(dispatch, '_manifest', original)
    reasons = guard.read_video_duplicate_blocks(root, 'entry')
    assert len(reasons) == 1 and binding.attempt_id in reasons[0]
    reasons = guard.read_video_duplicate_blocks(root, 'other')
    assert len(reasons) == 1 and exact.binding.attempt_id in reasons[0]


def test_unknown_verified_other_project_is_independent(unknown_original):
    root, _, _, _, _ = unknown_original
    other = root.parent / 'other-project'
    other.mkdir()
    assert guard.read_video_duplicate_blocks(other, 'entry') == []


def test_unknown_other_shot_does_not_read_corrupt_public_ready(unknown_original):
    root, _, binding, _, ready_calls = unknown_original
    path = publish_ready(unknown_original)
    path.write_bytes(path.read_bytes() + b'\n')
    assert guard.read_video_duplicate_blocks(root, 'other') == []
    assert ready_calls == []


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('state', ['prepared', 'submitting', 'uncertain'])
def test_unknown_private_original_blocks_governed_grok_before_new_attempt(unknown_original, direct, state):
    root, ledger, binding, _, _ = unknown_original
    inputs, scope, _ = project(root, motion=True)
    tool = GrokMotion()
    approve(root, inputs, scope, tool)
    if state != 'prepared':
        path = publish_ready(unknown_original)
        ledger.mark_unpriced(binding, 'submitting', hashlib.sha256(path.read_bytes()).hexdigest())
        if state == 'uncertain': ledger.mark_unpriced(binding, 'uncertain', D)
    before = list(root.glob('production_attempts/*/request.json'))
    with pytest.raises(execution.ProductionGovernanceError, match='unresolved private job acceptance'):
        invoke(tool, inputs, direct)
    assert tool.calls == 0
    assert list(root.glob('production_attempts/*/request.json')) == before


def test_unknown_terminal_allows_later_retained_approved_different_attempt(unknown_original, monkeypatch):
    root, ledger, binding, _, _ = unknown_original
    inputs, scope, _ = project(root, motion=True)
    path = publish_ready(unknown_original)
    ledger.mark_unpriced(binding, 'submitting', hashlib.sha256(path.read_bytes()).hexdigest())
    ledger.mark_unpriced_submitted(OriginalJobReceipt(binding, D, 'original-job'))
    ledger.release_unpriced(QualifiedSlotRelease(binding, 'terminal', D, D, 'original-job'))
    monkeypatch.setattr(dispatch, 'existing_terminal_state', lambda *args: {'status': 'terminal_unselected'})
    tool = GrokMotion()
    approve(root, inputs, scope, tool, phase='repair', replaces=[binding.attempt_id])
    result = tool.execute(inputs)
    assert result.success and tool.calls == 1
    assert result.data['production_attempt_id'] != binding.attempt_id
    assert ledger.inspect_unpriced(binding.attempt_id)['billing_state'] == 'unknown'
