"""Retained-original compatibility readers (CE #1, #5, #7).

SYNTHETIC retained records only; no providers, network, writers or replay.
"""
import copy
import hashlib
import json

import pytest

from lib import production_request as request
from lib.shot_contract import file_sha256
from tools import _openart_cli as cli
from tests.lib.test_production_request import package, preview, write  # noqa: F401


CREATOR = 'Creator exact framing. The patient says: Help me. A beat. Again: Help me. Hold.'


def dialogue_coverage(prompt, packet, pointer='/params/prompt'):
    """Independent re-derivation of ordered dialogue-only literal spans."""
    rows, cursor = [], 0
    for row in packet['occurrences']:
        if '/dialogue/' not in row['source_pointer'] or not row['source_pointer'].endswith('/text'):
            continue
        start = prompt.index(row['value'], cursor); end = start + len(row['value'])
        rows.append({'occurrence_id': row['occurrence_id'], 'source_pointer': row['source_pointer'],
                     'value_sha256': row['value_sha256'], 'native_pointer': pointer,
                     'start': start, 'end': end, 'fragment_sha256': request.digest(prompt[start:end])})
        cursor = end
    return rows


def retained_override(package, aid='att-creator'):
    """Write a pre-retirement creator-prompt original exactly as its frozen snapshot."""
    from lib.production_execution import planned_request_digest
    root, inputs, _, profile, compiled, review = package
    inputs = copy.deepcopy(inputs); inputs['prompt'] = CREATOR
    native = preview(inputs, profile, 'https://up.openart.test/start.svg?sig=private')
    packet = request.source_packet(root, 'entry', native=native)
    old = copy.deepcopy(compiled)
    old.update(request_sha256=planned_request_digest(inputs, project_dir=inputs['project_dir']),
               native_binding=request._native_binding(native),
               coverage=dialogue_coverage(CREATOR, packet),
               creator_prompt_override={'kind': 'creator_exact_prompt',
                   'creator_repair': {'batch_id': 'b1', 'item_id': 'i1', 'intent_sha256': 'c' * 64},
                   'prompt_sha256': request.digest(CREATOR),
                   'source_occurrences_sha256': request.digest(packet['occurrences'])})
    old_review = dict(copy.deepcopy(review), subject_sha256=request.digest(old))
    saved = {'attempt_id': aid, 'scope_id': 'approved', 'shot_id': 'entry',
             'input_assets': [{'role': 'image_path', 'path': inputs['image_path'],
                               'original_path': inputs['image_path'], 'sha256': file_sha256(inputs['image_path'])}],
             'openart': {'preparation_snapshot': freeze(aid, old, old_review, packet)}}
    return root, saved, {'inputs': inputs, 'native': native, 'profile': profile}, (old, old_review, packet)


def freeze(aid, compiled, review, packet):
    raw = json.dumps({'compiled': compiled, 'review': review, 'source_packet': packet},
                     sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    path = cli.state_dir() / 'preparation' / aid / 'review.json'
    if path.exists():
        path.chmod(0o600); path.unlink()
    cli.write_private(path, raw)
    return {'snapshot_id': aid, 'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
            'compiled_sha256': request.digest(compiled), 'review_sha256': request.digest(review)}


# ------------------------------------------------------------------ #1 CLI

def test_retained_creator_prompt_reads_as_history_and_fresh_paths_refuse(package):
    root, saved, frozen, (compiled, _, packet) = retained_override(package)
    proof = saved['openart']['preparation_snapshot']
    assert request.validate_frozen_preparation_history(saved, frozen, root) == proof
    assert request._validate_frozen_preparation_original(saved, frozen, root) == proof
    assert request.validate_frozen_preparation_history(saved, frozen, root) == proof  # stable reread
    assert request.historical_creator_prompt_coverage(compiled, CREATOR, packet,
        native_pointer='/params/prompt') == compiled['coverage']
    with pytest.raises(ValueError, match='retired'):
        request.validate_frozen_preparation(saved, frozen, root)
    # A fresh override published as current preparation is still refused.
    _, review = compiled, dict(package[5], subject_sha256=request.digest(compiled))
    write(root / 'artifacts/compiled_request-c1.json', compiled)
    write(root / 'artifacts/preparation_review-r1.json', review)
    with pytest.raises(ValueError):
        request.validate_preparation(frozen['inputs'], frozen['native'], frozen['profile'])


def tamper_prompt(compiled, packet):
    compiled['creator_prompt_override']['prompt_sha256'] = request.digest(CREATOR + ' edited')


def tamper_occurrences(compiled, packet):
    compiled['creator_prompt_override']['source_occurrences_sha256'] = 'd' * 64


def tamper_coverage_drop(compiled, packet):
    compiled['coverage'] = compiled['coverage'][:1]


def tamper_coverage_span(compiled, packet):
    first = compiled['coverage'][0]; first['start'] += 1; first['end'] += 1
    first['fragment_sha256'] = request.digest(CREATOR[first['start']:first['end']])


def tamper_full_coverage(compiled, packet):
    # Claiming the canonical full-occurrence coverage for a creator prompt fails.
    compiled['coverage'] = compiled['coverage'] + compiled['coverage'][:1]


@pytest.mark.parametrize('tamper', [tamper_prompt, tamper_occurrences, tamper_coverage_drop,
                                    tamper_coverage_span, tamper_full_coverage])
def test_tampered_retained_creator_prompt_rejected_even_when_rehashed(package, tamper):
    root, saved, frozen, (compiled, review, packet) = retained_override(package)
    compiled = copy.deepcopy(compiled); tamper(compiled, packet)
    review = dict(review, subject_sha256=request.digest(compiled))
    saved['openart']['preparation_snapshot'] = freeze(saved['attempt_id'], compiled, review, packet)
    with pytest.raises(ValueError):
        request.validate_frozen_preparation_history(saved, frozen, root)
    with pytest.raises(ValueError):
        request.historical_creator_prompt_coverage(compiled, CREATOR, packet, native_pointer='/params/prompt')


def test_changed_retained_snapshot_bytes_rejected(package):
    root, saved, frozen, _ = retained_override(package)
    path = cli.state_dir() / 'preparation' / saved['attempt_id'] / 'review.json'
    raw = json.loads(path.read_text()); raw['compiled']['creator_prompt_override']['creator_repair']['item_id'] = 'i2'
    path.chmod(0o600); path.write_text(json.dumps(raw)); path.chmod(0o400)
    with pytest.raises(ValueError, match='snapshot'):
        request.validate_frozen_preparation_history(saved, frozen, root)


def test_changed_dialogue_source_rejects_retained_creator_prompt(package):
    root, saved, frozen, _ = retained_override(package)
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    contract['shots'][0]['dialogue'][1]['text'] = 'Help us.'
    from lib.shot_contract import contract_digest
    sha = contract_digest(contract); contract['project_review']['subject_sha256'] = sha
    for s in contract['shots']: s['review']['subject_sha256'] = sha
    write(root / 'artifacts/shot_contract.json', contract)
    with pytest.raises(ValueError):
        request._validate_frozen_preparation_original(saved, frozen, root)


# ------------------------------------------------------------------ #5 ledger

from tests.lib.test_production_provenance import production, attempt, check, read, save  # noqa: E402,F401


def test_retained_revision_ledger_does_not_hide_authentic_original(production):
    from lib.production_provenance import historical_source_provenance
    aid, directory, output = attempt(production)
    baseline = check(production, aid, output)
    assert 'retained_planning_revision_ledger' not in baseline
    ledger = production.root / 'production_revisions.json'
    ledger.write_text(json.dumps({'version': '1.0', 'revisions': [{'id': 'retired-synthetic'}]}))
    before = ledger.read_bytes()
    proof = check(production, aid, output)
    assert proof['result']['status'] == 'generated'
    assert proof['retained_planning_revision_ledger'] == 'unknown_applicability'
    hist = historical_source_provenance(production.root, aid, shot_id='entry',
        story_revision=production.story['story_revision'], expected_output=output)
    assert hist['retained_planning_revision_ledger'] == 'unknown_applicability'
    assert ledger.read_bytes() == before  # never replayed or rewritten


@pytest.mark.parametrize('change', ['own_shot_planning', 'media'])
def test_retained_revision_ledger_still_protects_own_shot_and_media(production, change):
    from lib.production_execution import ProductionGovernanceError
    from pathlib import Path
    aid, directory, output = attempt(production)
    (production.root / 'production_revisions.json').write_text(json.dumps({'version': '1.0', 'revisions': []}))
    if change == 'media':
        path = Path(read(directory / 'result.json')['preserved_output']['path'])
        path.chmod(0o644); path.write_bytes(b'changed synthetic media')
    else:
        from tests.lib.test_shot_contract import refresh
        contract_path = production.root / 'artifacts/shot_contract.json'
        contract = read(contract_path)
        contract['shots'][0]['dominant_action'] += ' Changed own-shot staging.'
        refresh(contract); save(contract_path, contract)
    with pytest.raises((ProductionGovernanceError, ValueError)):
        check(production, aid, output)


# ------------------------------------------------------------------ #7 successor

from tests.integration.test_first_pass_workflow import (  # noqa: E402
    PIXEL, SYNTHETIC, attestation, production as first_pass)  # noqa: F401
from lib import shot_contract as sc  # noqa: E402


@pytest.fixture
def retained_successor(first_pass):
    p = first_pass
    result = p.generate('entry'); assert result.success, result.error
    aid = result.data['production_attempt_id']
    frame = p.root / 'assets/images/entry-observed-outgoing.png'; frame.write_bytes(PIXEL + aid.encode())
    selection = {'attempt_id': aid, 'output': {'path': result.artifacts[0], 'sha256': file_sha256(result.artifacts[0])},
                 'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    review = attestation(sc.selection_digest(selection), sc.UPSTREAM_PREDICATES)
    review.update(status='provisional', draft_policy_sha256='e' * 64)
    speaker = next(x for x in review['predicates'] if x['name'] == 'speaker_source')
    speaker.update(status='unknown', evidence='Audio not yet listened (synthetic).')
    selection['review'] = review
    return p.root, selection


def write_successor(root, selection, *, evidence_override=None, record_override=None, name=None):
    review = copy.deepcopy(selection['review'])
    review.update(review_id='synthetic-successor', status='pass'); review.pop('draft_policy_sha256')
    speaker = next(x for x in review['predicates'] if x['name'] == 'speaker_source')
    speaker.update(status='pass', evidence='Complete synchronized listening (synthetic fixture).')
    context = sc._successor_context(root, 'entry', selection)
    evidence = {'version': '1.0', 'kind': 'synchronized_audio_review', 'attempt_id': selection['attempt_id'],
                'output_sha256': selection['output']['sha256'], 'reviewer': review['reviewer'],
                'coverage': 'complete_clip', 'synchronized': True, 'listened': True, 'status': 'pass',
                **{k: context[k] for k in ('project_id', 'story_revision', 'shot_id', 'subject_sha256')},
                **(evidence_override or {})}
    epath = root / 'evidence' / ('listening-' + (name or 'a') + '.json'); epath.parent.mkdir(exist_ok=True)
    epath.write_text(json.dumps(evidence))
    record = {**context, 'version': '1.0', 'attempt_id': selection['attempt_id'],
              'predecessor_sha256': sc.review_digest(selection['review']), 'review': review,
              'evidence': {'path': str(epath.relative_to(root)), 'sha256': file_sha256(epath)},
              **(record_override or {})}
    raw = json.dumps(record).encode()
    directory = root / 'production_review_successors'; directory.mkdir(exist_ok=True)
    path = directory / ((name if name and name.endswith('.json') else hashlib.sha256(raw).hexdigest() + '.json'))
    path.write_bytes(raw)
    return review, path, epath


def tree(root):
    return {p: p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


def test_absent_successor_returns_original(retained_successor):
    root, selection = retained_successor
    assert sc.resolve_selection_review(root, 'entry', selection) == selection['review']


def test_retained_successor_resolves_stably_without_writes(retained_successor):
    root, selection = retained_successor
    successor, _, _ = write_successor(root, selection)
    before = tree(root)
    expected = sc.review_digest(selection['review'])
    for _ in range(2):
        assert sc.resolve_selection_review(root, 'entry', selection) == successor
        assert sc.resolve_selection_review(root, 'entry', selection, expected_review_sha256=expected) == successor
    assert tree(root) == before
    with pytest.raises(ValueError, match='historical original review differs'):
        sc.resolve_selection_review(root, 'entry', selection, expected_review_sha256=sc.review_digest(successor))


def misnamed(root, selection):
    write_successor(root, selection, name='not-content-addressed.json')


def conflicting(root, selection):
    write_successor(root, selection)
    write_successor(root, selection, name='b', record_override={})  # second distinct evidence path → distinct record


def changed_evidence(root, selection):
    _, _, epath = write_successor(root, selection)
    epath.write_text(epath.read_text().replace('complete_clip', 'partial_clip'))


def false_listening(root, selection):
    write_successor(root, selection, evidence_override={'listened': False})


def changed_media(root, selection):
    write_successor(root, selection)
    from pathlib import Path
    out = Path(selection['output']['path']); out.chmod(0o644); out.write_bytes(b'changed synthetic media')


def changed_context(root, selection):
    write_successor(root, selection)
    from tests.lib.test_shot_contract import refresh
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    contract['shots'][0]['dominant_action'] += ' Changed own-shot staging.'
    refresh(contract)
    path = root / 'artifacts/shot_contract.json'; path.chmod(0o644); path.write_text(json.dumps(contract))


def visual_change(root, selection):
    review = copy.deepcopy(selection['review'])
    write_successor(root, selection)
    path = next((root / 'production_review_successors').glob('*.json'))
    record = json.loads(path.read_text())
    next(x for x in record['review']['predicates'] if x['name'] == 'possession')['evidence'] = 'Rewritten visual.'
    path.unlink(); raw = json.dumps(record).encode()
    (path.parent / (hashlib.sha256(raw).hexdigest() + '.json')).write_bytes(raw)
    assert review == selection['review']


@pytest.mark.parametrize('defect', [misnamed, conflicting, changed_evidence, false_listening,
                                    changed_media, changed_context, visual_change])
def test_changed_or_conflicting_successor_fails_closed(retained_successor, defect):
    root, selection = retained_successor
    defect(root, selection)
    with pytest.raises(ValueError):
        sc.resolve_selection_review(root, 'entry', selection)


# ------------------------------------------------- #1 MCP root lock-proof branch

from lib import production_autonomy as pa  # noqa: E402
from tests.lib.test_production_factory import (  # noqa: E402,F401
    neutral_package, guided_observations, prepared_frame_shapes, file_bytes as factory_bytes)


@pytest.fixture
def retained_mcp_override(neutral_package, monkeypatch, guided_observations):
    """Synthetic retained OpenArt MCP original whose frozen compiled carries a
    pre-retirement creator prompt marker; nothing here can dispatch."""
    root, prepared = prepared_frame_shapes(neutral_package, monkeypatch, 'openart_mcp')
    inputs, native, profile, compiled, review = (copy.deepcopy(v) for v in prepared['s1'])
    inputs['prompt'] = 'Creator exact framing. ' + inputs['prompt'] + ' Hold.'
    native = request.prep_builder('openart_mcp')(inputs, profile)
    pointer = native.get('prompt_param_path') if native.get('prompt_param_path') == '/params/sceneDescription' else '/params/prompt'
    prompt = request._body(native)['params'][pointer.rsplit('/', 1)[1]]
    packet = request._historical_source_packet(root, 's1', provider='openart_mcp', native=native)
    compiled.update(coverage=dialogue_coverage(prompt, packet, pointer),
        creator_prompt_override={'kind': 'creator_exact_prompt',
            'creator_repair': {'batch_id': 'b1', 'item_id': 'i1', 'intent_sha256': 'c' * 64},
            'prompt_sha256': request.digest(prompt),
            'source_occurrences_sha256': request.digest(packet['occurrences'])})
    review['subject_sha256'] = request.digest(compiled)
    policy, sha, _ = pa.require_active_policy(root)
    return root, inputs, native, profile, {'policy': policy, 'policy_sha256': sha,
                                           'compiled': compiled, 'review': review}


def test_mcp_root_lock_accepts_retained_override_and_refuses_fresh(retained_mcp_override):
    root, inputs, native, profile, historical = retained_mcp_override
    before = factory_bytes(root)
    first = pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile, historical=historical)
    assert len(first) == 64
    assert pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile,
                               historical=copy.deepcopy(historical)) == first
    # Fresh: the same override published as current preparation is refused,
    # both on the active path and on retained-policy replay without historical opt-in.
    from tests.lib.test_production_factory import write as fwrite
    fwrite(root, 'artifacts/compiled_request-frame-s1.json', historical['compiled'])
    fwrite(root, 'artifacts/preparation_review-frame-s1.json', historical['review'])
    with pytest.raises((ValueError, pa.AutonomyError)):
        pa.lock_proof(root, 's1', inputs=inputs, native=native, profile=profile)
    with pytest.raises((ValueError, pa.AutonomyError)):
        pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile,
            policy_context=(historical['policy'], historical['policy_sha256']))
    assert {k: v for k, v in factory_bytes(root).items() if 'frame-s1' not in str(k)} == \
        {k: v for k, v in before.items() if 'frame-s1' not in str(k)}


def _tamper_prompt_marker(h):
    h['compiled']['creator_prompt_override']['prompt_sha256'] = '0' * 64


def _tamper_occurrences(h):
    h['compiled']['creator_prompt_override']['source_occurrences_sha256'] = '0' * 64


def _tamper_coverage(h):
    h['compiled']['coverage'] = [dict(r, start=r['start'] + 1, end=r['end'] + 1) for r in h['compiled']['coverage']] \
        or [{'occurrence_id': 'forged', 'source_pointer': '/x', 'value_sha256': '0' * 64,
             'native_pointer': '/params/prompt', 'start': 0, 'end': 1, 'fragment_sha256': '0' * 64}]


def _tamper_marker_shape(h):
    h['compiled']['creator_prompt_override']['kind'] = 'fresh_prompt'


@pytest.mark.parametrize('tamper', [_tamper_prompt_marker, _tamper_occurrences, _tamper_coverage, _tamper_marker_shape])
def test_mcp_root_lock_rejects_tampered_retained_override(retained_mcp_override, tamper):
    root, inputs, native, profile, historical = retained_mcp_override
    tamper(historical)
    historical['review']['subject_sha256'] = request.digest(historical['compiled'])
    before = factory_bytes(root)
    with pytest.raises(pa.AutonomyError, match='not authenticated history'):
        pa._root_lock_proof(root, 's1', inputs=inputs, native=native, profile=profile, historical=historical)
    assert factory_bytes(root) == before
