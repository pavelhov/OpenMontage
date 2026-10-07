"""Offline reference-free selection/final proof, never live production evidence.

Uses the real source/compiler, CLI qualification, original job, collection and
provenance adapters. The executable CLI replies, downloaded bytes, outgoing
frame, approvals, semantic reviews and duration probe are explicitly synthetic.
No provider calls or uploads occur, and neither fixture provenance flag is used.
"""
import copy
import io
import json
import socket
from pathlib import Path

import pytest

from lib import openart_download as download, openart_jobs as jobs
from lib import production_execution as execution, production_provenance as provenance
from lib.production_review import certify_final, validate_final_review
from lib.shot_contract import (
    PROJECT_PREDICATES, UPSTREAM_PREDICATES, contract_digest, file_sha256,
    review_digest, selection_digest,
)
from tests.integration.test_openart_unknown_cost_workflow import (
    FIXTURE, read, submissions, workflow,
)
from tests.lib.test_production_request import write as write_fixture


def write(path, value):
    """Deliberate fixture tampering of retained read-only public evidence."""
    if path.exists():
        path.chmod(0o644)
    write_fixture(path, value)


@pytest.fixture
def collected(workflow, monkeypatch):
    root, inputs, profile, tmp, tool, packet = workflow
    assert not provenance._ALLOW_OPENART_FIXTURE_PROVENANCE
    assert not provenance._ALLOW_OPENART_COMPONENT_PREPARATION
    assert profile['source'] == 'real'
    result = tool.execute(copy.deepcopy(inputs))
    assert result.error is None
    attempt = result.data['production_attempt_id']
    monkeypatch.setenv('FAKE_STATUS', 'done')
    jobs.promote_result_contract(attempt, json_paths={'url_hosts': ['cdn.openart.test']})
    qualified = jobs.load_qualification(model=inputs['model'], mode='text2video', require='full')
    assert qualified['source'] == 'real'
    assert qualified['profile_sha256'] == profile['profile_sha256']
    body = b'SYNTHETIC TEST VIDEO BYTES; NOT PROVIDER FOOTAGE'

    class SyntheticSocket:
        def sendall(self, data):
            pass

        def makefile(self, mode):
            return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(body) + body)

        def settimeout(self, value):
            pass

        def close(self):
            pass

    monkeypatch.setattr(download, '_resolve_public', lambda *args: (
        socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443)))
    monkeypatch.setattr(download._PinnedHTTPSConnection, 'connect',
                        lambda self: setattr(self, 'sock', SyntheticSocket()))
    result = execution.collect_openart_attempt(root, attempt, request_sha256=packet['request_sha256'])
    assert result['status'] == 'generated'
    assert Path(result['output']['path']).read_bytes() == body
    assert submissions(tmp) == ['fixture-original']
    request = read(root / 'production_attempts' / attempt / 'request.json')
    assert request['input_assets'] == []
    assert 'preparation_snapshot' in request['openart']
    yield root, attempt, result['output'], tmp
    calls = [json.loads(line) for line in (tmp / 'calls').read_text().splitlines()]
    assert all(args[:1] != ['upload'] for args in calls)
    assert submissions(tmp) == ['fixture-original']


def attest(subject, revision, names):
    return {'review_id': 'synthetic-reference-free-review', 'reviewer': FIXTURE,
            'story_revision': revision, 'subject_sha256': subject, 'status': 'pass',
            'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical',
                            'evidence': FIXTURE} for name in sorted(names)]}


def selection(root, attempt, output):
    outgoing = root / 'synthetic-outgoing-frame.svg'
    outgoing.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><!-- synthetic fixture frame --></svg>')
    value = {'attempt_id': attempt, 'output': output,
             'outgoing_frame': {'path': str(outgoing), 'sha256': file_sha256(outgoing)}}
    value['review'] = attest(selection_digest(value), read(root / 'project.json')['story_revision'], UPSTREAM_PREDICATES)
    return value


def final_review(root, chosen):
    contract = execution.load_shot_contract(root)
    master = root / 'synthetic-master.mp4'
    master.write_bytes(b'SYNTHETIC FINAL TEST BYTES; NO LIVE FOOTAGE OR AV REVIEW')
    return {'version': '2.0', 'project_id': contract['project_id'],
            'story_revision': contract['story_revision'], 'contract_sha256': contract_digest(contract),
            'output_path': str(master), 'output_sha256': file_sha256(master),
            'duration_seconds': 1, 'release_status': 'final',
            'reviewer': {'id': 'synthetic-fixture-author', 'kind': 'agent', 'method': FIXTURE,
                         'reviewed_at': '2026-10-07T00:00:00Z'},
            'dimensions': {name: {'status': 'pass', 'evidence': FIXTURE}
                           for name in ('transport', 'technical', 'visual', 'audio', 'story')},
            'av_review': {'status': 'pass', 'mode': 'synchronized_av', 'watched_full': True,
                          'listened_full': True, 'start_seconds': 0, 'end_seconds': 1, 'evidence': FIXTURE},
            'scenes': [{'scene_id': 'entry', 'attempt_id': chosen['attempt_id'],
                        'output_sha256': chosen['output']['sha256'], 'selection_sha256': selection_digest(chosen),
                        'review_sha256': review_digest(chosen['review']), 'start_seconds': 0, 'end_seconds': 1}],
            'predicates': attest('0' * 64, contract['story_revision'], PROJECT_PREDICATES)['predicates']}


def synthetic_duration_probe(path):
    return {'duration_seconds': 1}


def test_collected_reference_free_attempt_selects_and_reaches_final_provenance(collected):
    root, attempt, output, tmp = collected
    chosen = selection(root, attempt, output)
    assert execution.record_selection(root, 'entry', chosen) == chosen
    assert execution.load_selected_attempts(root)['entry'] == chosen
    # Final validation replays complete attempt provenance after selection.
    calls_before = (tmp / 'calls').read_bytes()
    final = final_review(root, chosen)
    checked = validate_final_review(root, final, probe=synthetic_duration_probe)
    assert checked['eligible'], checked['errors']
    certify_final(root, final, probe=synthetic_duration_probe)
    assert read(root / 'artifacts/final_review.json') == final
    assert (tmp / 'calls').read_bytes() == calls_before


def test_reference_free_provenance_forbids_extra_input_snapshot(collected):
    root, attempt, output, _ = collected
    directory = root / 'production_attempts' / attempt
    request = read(directory / 'request.json')
    snapshot = directory / 'inputs' / 'unsubmitted.svg'
    snapshot.parent.mkdir(exist_ok=True)
    snapshot.write_bytes(b'<svg><!-- synthetic unapproved input --></svg>')
    request['input_assets'] = [{'role': 'image_path', 'path': str(snapshot),
                              'original_path': str(root / 'unsubmitted.svg'), 'sha256': file_sha256(snapshot)}]
    write(directory / 'request.json', request)
    with pytest.raises(execution.ProductionGovernanceError, match='reference-free submitted input snapshots forbidden'):
        execution.record_selection(root, 'entry', selection(root, attempt, output))
    assert not execution.load_selected_attempts(root)


@pytest.mark.parametrize('change', ['missing_predicate', 'unknown', 'fail', 'cosmetic',
                                    'stale_review', 'output_bytes', 'outgoing_bytes'])
def test_reference_free_selection_keeps_semantic_and_current_byte_guards(collected, change):
    root, attempt, output, _ = collected
    chosen = selection(root, attempt, output)
    predicate = chosen['review']['predicates'][0]
    if change == 'missing_predicate':
        chosen['review']['predicates'].pop()
    elif change in {'unknown', 'fail'}:
        predicate['status'] = change
    elif change == 'cosmetic':
        predicate['severity'] = 'cosmetic'
    elif change == 'stale_review':
        chosen['review']['subject_sha256'] = '0' * 64
    else:
        role = 'output' if change == 'output_bytes' else 'outgoing_frame'
        Path(chosen[role]['path']).write_bytes(b'SYNTHETIC ALTERED BYTES')
    with pytest.raises((execution.ProductionGovernanceError, jobs.OpenArtCLIError)):
        execution.record_selection(root, 'entry', chosen)
    assert not execution.load_selected_attempts(root)


@pytest.mark.parametrize('change', ['preparation_missing', 'frozen_preparation_bytes',
                                    'script', 'duration', 'pin', 'story_revision'])
def test_reference_free_selection_keeps_frozen_preparation_and_source_guards(collected, change):
    root, attempt, output, _ = collected
    directory = root / 'production_attempts' / attempt
    if change == 'preparation_missing':
        request = read(directory / 'request.json')
        del request['openart']['preparation_snapshot']
        write(directory / 'request.json', request)
    elif change == 'frozen_preparation_bytes':
        from tools import _openart_cli as cli
        path = cli.state_dir() / 'preparation' / attempt / 'review.json'
        path.chmod(0o600)
        path.write_text('{}')
    elif change == 'script':
        script = read(root / 'artifacts/script.json')
        script['sections'][0]['text'] = 'A different synthetic action.'
        write(root / 'artifacts/script.json', script)
    elif change == 'duration':
        contract = execution.load_shot_contract(root)
        contract['shots'][0]['duration_seconds'] = 2
        write(root / 'artifacts/shot_contract.json', contract)
    elif change == 'pin':
        scenes = read(root / 'artifacts/scene_plan.json')
        scenes['metadata'] = {'visual_development': {'shot_cards': {'entry': {
            'pinned_first_frame': {'required': True, 'requirement_id': 'synthetic-pin'}}}}}
        write(root / 'artifacts/scene_plan.json', scenes)
    else:
        marker = read(root / 'project.json')
        marker['story_revision'] = 'different-story'
        write(root / 'project.json', marker)
    with pytest.raises(execution.ProductionGovernanceError):
        execution.record_selection(root, 'entry', selection(root, attempt, output))
    assert not execution.load_selected_attempts(root)


@pytest.mark.parametrize('change', ['preparation_missing', 'output_bytes', 'selection_unknown'])
def test_reference_free_final_rechecks_current_attempt_and_selection(collected, change):
    root, attempt, output, _ = collected
    chosen = selection(root, attempt, output)
    execution.record_selection(root, 'entry', chosen)
    final = final_review(root, chosen)
    if change == 'preparation_missing':
        directory = root / 'production_attempts' / attempt
        request = read(directory / 'request.json')
        del request['openart']['preparation_snapshot']
        write(directory / 'request.json', request)
    elif change == 'output_bytes':
        Path(output['path']).write_bytes(b'SYNTHETIC ALTERED OUTPUT AFTER SELECTION')
    else:
        chosen['review']['predicates'][0]['status'] = 'unknown'
        write(root / 'artifacts/selected_attempts.json', {'entry': chosen})
    if change == 'output_bytes':
        # The existing source adapter fails closed with its native error type.
        with pytest.raises(jobs.OpenArtCLIError, match='collection_receipt_invalid'):
            validate_final_review(root, final, probe=synthetic_duration_probe)
    else:
        checked = validate_final_review(root, final, probe=synthetic_duration_probe)
        assert not checked['eligible']
    with pytest.raises((ValueError, jobs.OpenArtCLIError)):
        certify_final(root, final, probe=synthetic_duration_probe)
    assert not (root / 'artifacts/final_review.json').exists()


@pytest.mark.parametrize('field,value', [('mode', 'image2video'), ('operation', 'image_to_video'),
                                        ('image_upload_id', 'synthetic-upload'), ('last_frame', 'synthetic-pin')])
def test_reference_free_conflicting_native_variants_refused_before_reservation(workflow, field, value):
    root, inputs, _, tmp, tool, _ = workflow
    inputs[field] = value
    with pytest.raises(execution.ProductionGovernanceError):
        tool.execute(inputs)
    assert submissions(tmp) == []
    assert not (root / 'production_attempts').exists()
