"""Independent offline v2 recovery/output/billing boundary checks."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil

import pytest

from lib import spicy_jobs as jobs, spicy_lifecycle as lifecycle
from tests.lib.test_spicy_lifecycle_v2 import original, accept, collector, no_network


def test_pending_empty_asset_and_null_cost_become_same_original_ready(original, monkeypatch):
    o = original
    accept(o)
    collector(monkeypatch)
    o['http'].task.update(state='succeeded', settled=False, cost=None,
        output={'assets': [{'key': '', 'pending': True, 'mime': 'video/mp4'}]})
    assert not jobs.resume_original(o['root'], o['aid']).success
    pending = json.loads(o['journal'].read_text())
    assert not pending.get('billing_anomaly')
    o['http'].task.update(settled=True, cost='0.10',
        output={'assets': [{'key': 'ready.mp4', 'mime': 'video/mp4', 'durationSeconds': 5}]})
    assert jobs.resume_original(o['root'], o['aid']).success
    assert len([call for call in o['http'].calls if call[1] == '/jobs/createTask']) == 2


@pytest.mark.parametrize('bad_cost', ['-0.01', '0.16', 'NaN'])
def test_invalid_settled_billing_is_sticky_even_after_valid_observation(original, bad_cost):
    o = original
    accept(o)
    o['http'].task.update(settled=True, cost=bad_cost)
    jobs.resume_original(o['root'], o['aid'])
    first = json.loads(o['journal'].read_text())['billing_anomaly']
    o['http'].task.update(settled=True, cost='0.10')
    jobs.resume_original(o['root'], o['aid'])
    assert json.loads(o['journal'].read_text())['billing_anomaly'] == first


def test_unknown_acceptance_after_original_window_never_creates_again(original, monkeypatch):
    o = original
    assert o['submit']().data['dispatch_status'] == 'acceptance_unknown'
    monkeypatch.setattr(jobs, '_now', lambda: datetime.now(timezone.utc) + timedelta(days=2))
    before = len(o['http'].calls)
    result = jobs.resume_original(o['root'], o['aid'])
    assert result.data['dispatch_status'] == 'acceptance_unknown'
    assert result.data['error_category'] == 'replay_window_closed'
    assert len(o['http'].calls) == before


def test_collected_original_byte_tampering_fails_locally_without_provider(original, monkeypatch):
    o = original
    accept(o)
    collector(monkeypatch)
    o['http'].task.update(state='succeeded', output={'assets': [
        {'key': 'ready.mp4', 'mime': 'video/mp4', 'durationSeconds': 5}]})
    assert jobs.resume_original(o['root'], o['aid']).success
    Path(o['inputs']['output_path']).write_bytes(b'ALTERED SYNTHETIC ORIGINAL')
    monkeypatch.setattr(o['http'], 'request_json', lambda *a, **k: pytest.fail('Local proof must fail before HTTP'))
    with pytest.raises(jobs.SpicyError):
        jobs.resume_original(o['root'], o['aid'])


@pytest.mark.parametrize('controls,media', [
    ({'aspect_ratio': '16:9'}, {'width': 64, 'height': 64, 'duration_seconds': 5}),
    ({'resolution': '720p'}, {'width': 64, 'height': 36, 'duration_seconds': 5}),
])
def test_primary_must_preserve_concrete_authored_visual_controls(controls, media):
    native = {'input': {'duration_seconds': 5, **controls}}
    with pytest.raises(jobs.SpicyError):
        lifecycle._primary_controls_v2(native, media)


def test_partial_metadata_refresh_does_not_erase_prior_asset_identity(original, monkeypatch):
    o = original
    accept(o)
    collector(monkeypatch)
    o['http'].task.update(state='succeeded', output={'assets': [
        {'key': 'ready.mp4', 'mime': 'video/mp4', 'pending': True, 'width': 64, 'height': 64,
         'durationSeconds': 5}]})
    assert not jobs.resume_original(o['root'], o['aid']).success
    o['http'].task['output']['assets'] = [{'key': 'ready.mp4', 'mime': 'video/mp4'}]
    assert jobs.resume_original(o['root'], o['aid']).success
    retained = json.loads(o['journal'].read_text())['assets'][0]
    assert retained['width'] == 64 and retained['height'] == 64
    assert retained['duration_seconds'] == 5


def test_still_archive_cannot_substitute_another_path_within_same_project(original, monkeypatch):
    o = original
    accept(o)
    collector(monkeypatch)
    o['http'].task.update(state='succeeded', output={'assets': [
        {'key': 'ready.mp4', 'mime': 'video/mp4', 'durationSeconds': 5},
        {'key': 'last.png', 'mime': 'image/png', 'role': 'last_frame'}]})
    assert jobs.resume_original(o['root'], o['aid']).success
    record = json.loads(o['journal'].read_text())
    intent = record['collection_intents_v2']['last.png']
    unrelated = o['root'] / 'assets' / 'different-attempt-still.png'
    shutil.copyfile(intent['path'], unrelated)
    intent['path'] = str(unrelated)
    o['journal'].write_text(json.dumps(record))
    with pytest.raises(jobs.SpicyError):
        lifecycle.collected_outputs(o['root'], o['aid'])
