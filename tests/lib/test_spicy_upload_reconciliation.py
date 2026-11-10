"""Explicit pre-PUT ticket rejection recovery; synthetic transport only."""
import json

import pytest

from lib import spicy_jobs as jobs, spicy_uploads as uploads
from tests.lib.test_spicy_typed_uploads import fake_home, setup_upload  # noqa: F401


def rejected(tmp_path, monkeypatch):
    result = setup_upload(tmp_path, monkeypatch)
    root, _, inputs, native, client, calls, reservations, puts = result
    original = client.create_upload_url

    def invalid(**kwargs):
        response = original(**kwargs)
        response['data']['expiresAt'] = 'invalid'
        return response

    monkeypatch.setattr(client, 'create_upload_url', invalid)
    with pytest.raises(jobs.SpicyError, match='malformed or expired'):
        uploads.upload_sources_v2(root, inputs, client=client)
    monkeypatch.setattr(client, 'create_upload_url', original)
    return result


def reconcile(result):
    root, _, inputs, native, _, _, _, _ = result
    source = native['sources'][0]
    return uploads.reconcile_rejected_ticket_v2(root,
        source_sha256=source['source_sha256'], credential_ref=inputs['credential_ref'],
        media_kind=source['media_kind'], mime=source['mime'],
        reason='Synthetic local ticket parser rejection before PUT; explicit recovery.')


def test_rejected_ticket_preserves_history_and_needs_distinct_counted_batch(tmp_path, monkeypatch):
    result = rejected(tmp_path, monkeypatch)
    root, _, inputs, native, client, calls, reservations, puts = result
    assert len(calls) == 1 and not puts
    recovery = reconcile(result)
    assert recovery['provider_calls'] == 0 and not recovery['replayed']
    assert reconcile(result)['replayed']
    assert len(list((root / 'artifacts').glob('*.rejected-*.json'))) == 1
    with pytest.raises(jobs.SpicyError, match='fresh counted'):
        uploads.upload_sources_v2(root, inputs, client=client)
    assert len(calls) == 1 and not puts
    from lib import production_autonomy
    old_reserve = production_autonomy.reserve_spicy_source_transfer

    def fresh(project, request):
        authority = old_reserve(project, request)
        return dict(authority, batch_id='typed-fixture-batch-2')

    monkeypatch.setattr(production_autonomy, 'reserve_spicy_source_transfer', fresh)
    receipts = uploads.upload_sources_v2(root, inputs, client=client)
    assert len(receipts) == 1 and len(puts) == 1
    journal = uploads._v2_paths(root, native['sources'][0]['source_sha256'],
                                client.binding['credential_digest'], 'image')[1]
    saved = json.loads(journal.read_text())
    assert saved['state'] == 'committed'
    assert saved['previous_rejected_ticket']['state'] == 'rejected_before_put'
    assert saved['previous_rejected_ticket']['batch_id'] == 'typed-fixture-batch'
    before = len(calls)
    assert uploads.upload_sources_v2(root, inputs, client=client) == receipts
    assert len(calls) == before


@pytest.mark.parametrize('failure', ['ticket', 'put', 'commit'])
def test_uncertain_network_and_any_storage_progress_never_reconcile(tmp_path, monkeypatch, failure):
    result = setup_upload(tmp_path, monkeypatch, failure=failure)
    root, _, inputs, _, client, calls, _, _ = result
    with pytest.raises(jobs.SpicyError):
        uploads.upload_sources_v2(root, inputs, client=client)
    before = len(calls)
    with pytest.raises(jobs.SpicyError, match='may have reached storage'):
        reconcile(result)
    assert len(calls) == before


def test_progress_metadata_blocks_even_if_failure_kind_claims_ticket_rejection(tmp_path, monkeypatch):
    result = rejected(tmp_path, monkeypatch)
    root = result[0]
    journal = next((root / 'artifacts').glob('spicy_upload_v2_attempt-*.json'))
    state = json.loads(journal.read_text())
    state['file_id'] = 'synthetic-storage-progress'
    uploads._write_state(journal, state)
    with pytest.raises(jobs.SpicyError, match='may have reached storage'):
        reconcile(result)


def test_changed_history_cannot_enable_another_upload(tmp_path, monkeypatch):
    result = rejected(tmp_path, monkeypatch)
    root, _, inputs, _, client, calls, _, _ = result
    reconcile(result)
    history = next((root / 'artifacts').glob('*.rejected-*.json'))
    history.chmod(0o600)
    state = json.loads(history.read_text())
    state['binding']['source_sha256'] = 'b' * 64
    uploads._write_state(history, state)
    before = len(calls)
    with pytest.raises(jobs.SpicyError, match='pre-PUT proof'):
        uploads.upload_sources_v2(root, inputs, client=client)
    assert len(calls) == before
