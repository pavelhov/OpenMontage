"""Independent review repros: v2 asset inventory must never substitute the selected original.

The implementation owner fixed the independently reproduced cases; both remain regressions.
"""
import json
from pathlib import Path
import pytest
from lib import spicy_jobs as jobs, spicy_lifecycle as lifecycle
from tests.lib.test_spicy_lifecycle_v2 import original, accept, collector, no_network  # noqa: F401


def _creates(o):
    return [c for c in o['http'].calls if c[1] == '/jobs/createTask']


def test_keyless_pending_video_slot_never_collapses_to_auto_primary(original, monkeypatch):
    o = original; accept(o); collector(monkeypatch)
    o['http'].task.update(state='succeeded', output={'assets': [
        {'key': 'a.mp4', 'mime': 'video/mp4'},
        {'key': '', 'mime': 'video/mp4', 'pending': True}]})
    first = jobs.resume_original(o['root'], o['aid'])
    assert not first.success
    # Second observation omits the not-yet-keyed slot (documented pending/omitted shape).
    o['http'].task['output']['assets'] = [{'key': 'a.mp4', 'mime': 'video/mp4'}]
    second = jobs.resume_original(o['root'], o['aid'])
    assert len(_creates(o)) == 2  # no extra paid create either way
    # Required: an earlier observation had two video slots, so no automatic primary.
    assert not second.success
    assert not Path(o['inputs']['output_path']).exists()


def test_late_second_video_keeps_every_explicit_primary_selectable(original, monkeypatch):
    o = original; accept(o); collector(monkeypatch)
    o['http'].task.update(state='succeeded', output={'assets': [
        {'key': 'a.mp4', 'mime': 'video/mp4'},
        {'key': 'b.mp4', 'pending': True}]})  # pending, MIME not yet known
    jobs.resume_original(o['root'], o['aid'])
    o['http'].task['output']['assets'] = [
        {'key': 'a.mp4', 'mime': 'video/mp4'}, {'key': 'b.mp4', 'mime': 'video/mp4'}]
    result = jobs.resume_original(o['root'], o['aid'])
    assert result.data['dispatch_status'] == 'collected_needs_primary'
    chosen = lifecycle.select_primary_output(o['root'], o['aid'], 'b.mp4')
    assert chosen['receipt']['primary_asset_key'] == 'b.mp4'
    assert len(_creates(o)) == 2
