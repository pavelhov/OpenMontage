"""One temporary offline episode across canonical engine and Studio owners.

Synthetic host receipts/downloads and tiny tone clips prove software transport,
never generated dialogue quality or a live provider render time.
"""
import copy
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import production_draft as draft, production_execution as execution
from lib import production_images as images, episode_production_controls as controls
from lib.events import read_events
from lib.shot_contract import file_sha256
from tests.lib.test_production_images import PNG, host_receipt
from tests.lib.test_production_execution import MotionTool, project
from tests.lib.test_production_repair_batches import (
    mcp_two as base_two, media_path, real_av_clip, collect_fixture, mcp_compile, approval,
)
from tests.integration.test_openart_mcp_autonomy import lifecycle as base_lifecycle


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline episode proof attempted a network connection')
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    # Set the fixture identity before mcp_two compiles/approves/reserves originals.
    # All actual Studio identity and engine provenance checks remain active.
    values = base_lifecycle.__wrapped__(tmp_path, monkeypatch)
    root = values[0]
    from tests.lib.test_shot_contract import refresh
    contract = execution.load_shot_contract(root)
    contract['project_id'] = 'thisaccountisawarcrime-offline-episode'
    refresh(contract)
    (root / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    marker = execution._read(root / 'project.json')
    marker['project_id'] = contract['project_id']
    (root / 'project.json').write_text(json.dumps(marker))
    return values


@pytest.fixture
def mcp_two(lifecycle, monkeypatch, media_path):
    # The shared fixture authors/approves the complete two-shot package before
    # its first prepare. Insert board work at that boundary, then call the REAL
    # original preparation function; no authority or dispatch check is waived.
    boards = pytest.importorskip('prepare_boards')
    jobs = lifecycle[-1]
    real_prepare = jobs.prepare
    prepared_boards = False
    def prepare_after_boards(root, **kwargs):
        nonlocal prepared_boards
        if not prepared_boards:
            prepare_episode_boards(root, boards)
            prepared_boards = True
        return real_prepare(root, **kwargs)
    with monkeypatch.context() as setup:
        setup.setattr(jobs, 'prepare', prepare_after_boards)
        values = base_two.__wrapped__(lifecycle, monkeypatch, media_path)
    assert prepared_boards
    return values


def test_governed_invocation_has_exact_observed_boundaries(tmp_path):
    inputs, _, _ = project(tmp_path, motion=True)
    result = MotionTool().execute(inputs)
    aid = result.data['production_attempt_id']
    points = [row for row in read_events(tmp_path) if row.get('attempt_id') == aid]
    assert [row['event'] for row in points] == ['governed-invocation-start', 'governed-invocation-return',
                                              'governed-result-retained']
    assert all(row['request_sha256'] == result.data['production_request_sha256'] for row in points)
    assert all(row['shot_id'] == 'entry' for row in points)
    assert points[-1]['output_sha256'] == file_sha256(inputs['output_path'])
    timing = pytest.importorskip('production_timing')
    receipt = timing.summarize_project(tmp_path)
    assert receipt['first_video_retained']['attempt_id'] == aid
    assert receipt['first_video_retained']['media_kind'] == 'motion'


def prepare_episode_boards(root, boards):
    marker = execution._read(root / 'project.json')
    reference = root / 'assets/images/shared.png'
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(PNG)
    review = {'review_id': 'reuse-rv', 'reviewer': 'persistent fixture reviewer',
              'story_revision': marker['story_revision'], 'subject_sha256': file_sha256(reference),
              'status': 'pass', 'predicates': [{'name': 'identity', 'severity': 'critical',
                  'status': 'pass', 'evidence': 'Synthetic applicable current role observation.'}]}
    evidence = approval(root, 'board-approval')
    spec = {'version': '1.0', 'reviewer': review['reviewer'],
            'story': {'path': 'artifacts/script.json', 'sha256': file_sha256(root / 'artifacts/script.json')},
            'approval': evidence,
            'references': [{'id': 'shared', 'role': 'identity', 'path': 'assets/images/shared.png',
                'sha256': file_sha256(reference), 'review': review,
                'applicability': {'story_revision': marker['story_revision'], 'roles': ['identity', 'start', 'end']}}],
            'shared_roles': [],
            'image_nodes': [{'slot_id': 'other-start', 'shot_id': 'other', 'role': 'start',
                'prompt': 'SYNTHETIC independently prepared start board.', 'reference_ids': ['shared'],
                'image_dependencies': [], 'output_path': 'assets/images/other-start.png',
                'required_predicates': ['identity']}],
            'shots': [{'shot_id': sid, 'video_dependencies': [], 'roles': [
                {'asset_id': 'shared' if sid == 'entry' else 'other-start', 'role': 'start'},
                {'asset_id': 'shared', 'role': 'end'}]} for sid in ('entry', 'other')]}
    pack = boards.prepare_board_pack(root, spec, images=images)
    assert boards.prepare_board_pack(root, spec, images=images) == pack
    assert images.image_usage(root)['total'] == 0
    schedule = boards.board_schedule(root, images=images)
    assert schedule['board_ready_shots'] == ['entry'] and schedule['ready_images'] == ['other-start']
    request = boards.prepare_image_request(root, 'other-start', images=images)
    scopes = execution._read(root / 'production_scopes.json')
    scopes['scopes'].append({'id': 'board-image', 'status': 'approved', 'approved_by': evidence['approved_by'],
        'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
        'evidence': {k: evidence[k] for k in ('path', 'sha256')}, 'phase': 'image', 'provider': 'native_imagegen',
        'requests': {'other-start': request['request_sha256']}, 'attempts_per_shot': {'other-start': 1},
        'image_allowance': 1, 'allowed_image_routes': ['native_imagegen']})
    (root / 'production_scopes.json').write_text(json.dumps(scopes))
    boards.reserve_board_image(root, 'other-start', scope_id='board-image', call_id='board-call', images=images)
    envelope = images.begin_native_image(root, 'board-call')
    images.import_native_image(root, 'board-call', host_receipt=host_receipt(root, envelope))
    binding = boards.bind_imported_board(root, 'other-start', 'board-call', images=images)
    started = datetime.now(timezone.utc).isoformat()
    reviewed = {**review, 'review_id': 'fresh-board-rv', 'subject_sha256': binding['sha256']}
    boards.record_board_review(root, 'other-start', reviewed, roles=['start'], started_at=started, images=images)
    ready = boards.board_schedule(root, images=images)
    assert ready['board_ready_shots'] == ['entry', 'other']
    assert ready['video_dependencies'] == {'entry': [], 'other': []}
    assert images.image_usage(root)['total'] == 1


def test_offline_episode_board_cut_disclosure_mixed_repairs_acceptance(mcp_two, monkeypatch, tmp_path):
    entry = pytest.importorskip('production_entry')
    boards = pytest.importorskip('prepare_boards')
    timing = pytest.importorskip('production_timing')
    from lib import production_repair_batches as batches
    root, policy, originals, template_timing, jobs = mcp_two
    engine = Path(execution.__file__).resolve().parents[1]
    source = real_av_clip(tmp_path / 'source.mp4', seconds=2)
    for sid in originals:
        collect_fixture(root, jobs, 'original-' + sid, source, monkeypatch)
    output = execution.load_attempt_result(root, 'original-entry')['output']
    from tests.integration.test_first_pass_workflow import attestation
    review = attestation(output['sha256'], ['grain'], policy['story_revision'])
    review.update(status='fail', predicates=[{'name': 'grain', 'status': 'fail', 'severity': 'cosmetic',
                                            'evidence': 'Synthetic grain warning, retained before repair.'}])
    execution.record_rejection(root, 'original-entry', review)
    cut = entry.compose_first_cut(engine, root)
    handoff = entry.cut_handoff(engine, root)
    assert cut['status'] == 'complete' and cut['technical']['has_audio']
    assert [clip['master_start'] for clip in cut['clips']] == [0, 2]
    assert [clip['master_end'] for clip in cut['clips']] == [2, 4]
    assert cut['clips'][0]['review'] == 'fail' and handoff['provider_calls'] == 0
    observations = [{'clip': 1, 'at': 0.5, 'defect': 'Synthetic grain warning.', 'severity': 'minor',
        'viewing': 'viewed', 'listening': 'listened', 'basis': 'synthetic synchronized AV fixture',
        'proposed': 'Creator can keep it or select a clearer framing.', 'reviewer': 'fixture reviewer'}]
    disclosure = entry.record_cut_disclosure(engine, root, observations)
    assert entry.record_cut_disclosure(engine, root, observations) == disclosure
    assert len([e for e in read_events(root) if e['event'] == 'cut-disclosure-retained']) == 1
    selections = []
    for number, (sid, model) in enumerate((('entry', 'pixverseV6'), ('other', 'wan3-0')), 1):
        inputs = copy.deepcopy(originals[sid])
        inputs.update(model=model, output_path=str(root / ('repair-' + sid + '.mp4')))
        inputs, _ = mcp_compile(root, inputs, sid, 'repair-' + sid, template_timing)
        selections.append({'clip': number, 'item_id': sid, 'selected_route': {'provider': 'openart_mcp',
            'tool': 'openart_mcp_video', 'model': model, 'mode': 'text2video'},
            'changes': 'Creator requests clearer framing.', 'inputs': inputs})
    evidence = approval(root, 'mixed-intent')
    status = entry.record_repair_batch(engine, root, {'batch_id': 'episode-mixed', 'cut': handoff['first_cut'],
        'evidence': evidence['path'], 'approved_by': evidence['approved_by'], 'selections': selections})
    assert all(item['state'] == 'planned' for item in status['items'])
    entry.prepare_repair(engine, root, 'episode-mixed', 'entry')
    actions = entry.resume_repair(engine, root, 'episode-mixed')
    assert len(actions['actions']) == 2
    assert entry.resume_repair(engine, root, 'episode-mixed')['actions'] == []
    repaired = real_av_clip(tmp_path / 'repaired.mp4', seconds=2, freq=660)
    for item in actions['items']:
        jobs.receive(root, item['attempt_id'], outcome={'historyId': 'history-' + item['attempt_id']})
        collect_fixture(root, jobs, item['attempt_id'], repaired, monkeypatch)
    successor = entry.compose_first_cut(engine, root)
    assert [c['attempt_id'] for c in successor['clips']] == [i['attempt_id'] for i in actions['items']]
    assert successor['technical']['has_audio']
    # Actual decoded source and master audio energies remain nonzero in both placed intervals.
    from tests.lib.test_first_cut import REAL_RUN
    decoded = REAL_RUN(['ffmpeg', '-v', 'error', '-i', str(root / successor['export']['path']),
                        '-vn', '-f', 'f32le', '-ac', '1', '-ar', '8000', '-'], capture_output=True, check=True)
    import array
    samples = array.array('f', decoded.stdout)
    assert all(sum(x*x for x in samples[start:start+8000]) > 0.1 for start in (0, 16000))
    # A retained 660Hz source tone survives each segment, rather than only an
    # arbitrary nonsilent replacement audio track being present in the master.
    import math
    def tone_energy(segment, freq):
        return sum(x * math.sin(2*math.pi*freq*i/8000) for i, x in enumerate(segment)) ** 2 + \
               sum(x * math.cos(2*math.pi*freq*i/8000) for i, x in enumerate(segment)) ** 2
    for start in (2000, 18000):
        segment = samples[start:start+4000]
        assert tone_energy(segment, 660) > 100 * tone_energy(segment, 440)
    successor_disclosure = entry.record_cut_disclosure(engine, root, [])
    accepted = approval(root, 'accept-successor')
    acceptance = entry.accept_cut(engine, root, accepted_by=accepted['approved_by'],
                                  evidence=accepted['path'], disclosure=successor_disclosure['path'])
    # Acceptance's existing exclusive publication contract rejects an identical
    # second append, and that failed append must not claim another observation.
    with pytest.raises(FileExistsError):
        entry.accept_cut(engine, root, accepted_by=accepted['approved_by'],
                         evidence=accepted['path'], disclosure=successor_disclosure['path'])
    assert len([e for e in read_events(root) if e['event'] == 'first-cut-accepted']) == 1
    assert acceptance['qc_pass'] is False and acceptance['re_encoded'] is False
    assert entry.cut_acceptance_status(engine, root, acceptance['acceptance'])['status'] == 'current'
    assert entry.repair_status(engine, root, 'episode-mixed')['closed']
    assert batches.resume_repair_batch(root, 'episode-mixed')['actions'] == []
    assert controls.generation_usage(root)['per_shot'] == {'entry': 2, 'other': 2}
    assert len(jobs.list_attempts(root)) == 4 and images.image_usage(root)['total'] == 1
    assert not any(c['strict_selected'] for c in draft.first_cut_candidates(root))
    receipt = timing.summarize_project(root)
    assert len(receipt['first_cuts']) == 2
    assert {row['attempt_id'] for cut in receipt['first_cuts'] for row in cut['clips']} == \
           {'original-entry', 'original-other'} | {item['attempt_id'] for item in actions['items']}
    assert receipt['backend_render_seconds'] is None
    board_ready = next(p for p in receipt['observations'] if p['event'] == 'board_review_retained')
    first_handoff = next(p for p in receipt['observations'] if p['event'] == 'begin_consumed_before_handoff')
    assert timing.parse_time(board_ready['ts']) <= timing.parse_time(first_handoff['ts'])
    assert {'host_handoff_retention', 'collection', 'local_compose', 'board_review'} <= {r['stage'] for r in receipt['stages']}
    assert any(point['event'] == 'first-cut-accepted' for point in receipt['observations'])
    assert {row['item_id'] for row in receipt['stages'] if row.get('batch_id') == 'episode-mixed'} == {'entry', 'other'}
    states = [jobs._load(root, item['attempt_id']) for item in actions['items']]
    for state in states:
        kinds = [row['kind'] for row in state['events']]
        assert kinds.count('begin_consumed_before_handoff') == kinds.count('collected') == 1
        assert all(row['observed_at'] for row in state['events'])
    assert all(row['duration_seconds'] is None or row['duration_seconds'] >= 0 for row in receipt['stages'])
