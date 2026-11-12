"""Creator draft evidence: duplicate-bill exceptions and first-cut previews.

Both record creator decisions only, never a semantic pass, certification,
publication or generation authority.
"""
from __future__ import annotations

import copy
import contextlib
import contextvars
import json
import os
import subprocess
import threading
from pathlib import Path

from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.shot_contract import (UPSTREAM_PREDICATES, CRITICAL_PREDICATES, file_sha256,
                               selection_digest, provisional_audio_review, draft_audio_policy_digest)

_KEYS = {'version', 'mode', 'project_id', 'story_revision', 'shot_id', 'attempt_id',
         'contract', 'output', 'outgoing_frame', 'failed_review', 'failed_predicate',
         'defect', 'accepted_by', 'acceptance_evidence', 'outgoing_review'}

_DELIVERY_LOCKS = contextvars.ContextVar('production_delivery_locks', default=frozenset())


@contextlib.contextmanager
def delivery_lock(project_dir):
    """Serialize cut selection, rendering and closeout with original completion.

    Nested delivery calls share the existing project lock in this thread only.
    Other threads/processes must acquire it and resnapshot their current cut.
    """
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    owner = (os.getpid(), threading.get_ident(), root)
    held = _DELIVERY_LOCKS.get()
    if owner in held:
        yield
        return
    with execution._lock(root):
        token = _DELIVERY_LOCKS.set(held | {owner})
        try:
            yield
        finally:
            _DELIVERY_LOCKS.reset(token)


def _delivery_serialized(function):
    import functools

    @functools.wraps(function)
    def wrapper(project_dir, *args, **kwargs):
        with delivery_lock(project_dir):
            return function(project_dir, *args, **kwargs)
    return wrapper


def _status_read(function):
    """Read-only status entry: bounded per-call validation reuse (see
    ``production_autonomy.status_reuse``); writers opt out via ``_write``."""
    import functools

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        from lib import production_autonomy as autonomy
        with autonomy.status_reuse():
            return function(*args, **kwargs)
    return wrapper


def _write(function):
    """Mutation entry: always fresh validation, even under an outer status scope."""
    import functools

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        from lib import production_autonomy as autonomy
        with autonomy.no_status_reuse():
            return function(*args, **kwargs)
    return wrapper


def _bound(root, binding):
    if not isinstance(binding, dict) or set(binding) != {'path', 'sha256'}:
        raise ValueError('draft exception requires exact hashed file binding')
    path = (root / binding['path']).resolve()
    if not path.is_relative_to(root) or file_sha256(path) != binding['sha256']:
        raise ValueError('draft exception file missing, changed, or outside project')
    return path


def _review(review, subject, revision):
    schema = load_schema('shot_contract')
    if (list(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/review'}).iter_errors(review))
            or review['subject_sha256'] != subject or review['story_revision'] != revision):
        raise ValueError('draft exception review is invalid or stale')
    names = [p['name'] for p in review['predicates']]
    if len(names) != len(set(names)):
        raise ValueError('draft exception review has duplicated predicates')
    return {p['name']: p for p in review['predicates']}


def validate_creator_draft_exception(project_dir, record, selection, *, shot_id=None, creating=False):
    """Replay exact original failure, creator evidence and clean outgoing review."""
    from lib import production_execution as execution
    from lib.production_provenance import shot_planning_digest
    root = Path(project_dir).resolve()
    marker = execution._read(root / 'project.json')
    if (not isinstance(record, dict) or set(record) != _KEYS
            or record['version'] != '1.0' or record['mode'] != 'creator_accepted_existing_footage_draft'
            or record['defect'] != 'duplicate_bill'
            or record['failed_predicate'] not in {'prop_body_invariants', 'possession', 'transformation'}
            or not isinstance(record['accepted_by'], str) or not record['accepted_by'].strip()
            or marker.get('governance', {}).get('mode') != 'strict'
            or (record['project_id'], record['story_revision']) != (marker['project_id'], marker['story_revision'])):
        raise ValueError('unsupported or mismatched creator draft exception')
    if shot_id is not None and record['shot_id'] != shot_id:
        raise ValueError('creator draft exception belongs to another shot')
    if any(record[key] != selection.get(key) for key in ('attempt_id', 'output', 'outgoing_frame')):
        raise ValueError('creator draft exception selection differs')
    prior = execution._read(_bound(root, record['contract']))
    current = execution.load_shot_contract(root)
    if (prior.get('project_id') != record['project_id'] or prior.get('story_revision') != record['story_revision']
            or (creating and prior != current)
            or shot_planning_digest(prior, record['shot_id']) != shot_planning_digest(current, record['shot_id'])):
        raise ValueError('creator draft exception planning changed')
    _bound(root, record['acceptance_evidence'])
    _bound(root, record['output']); _bound(root, record['outgoing_frame'])
    aid = record['attempt_id']
    requests = [a for a in execution._attempts(root) if a['attempt_id'] == aid]
    if len(requests) != 1 or requests[0]['shot_id'] != record['shot_id']:
        raise ValueError('creator draft exception requires actual same-shot attempt')
    state = execution.load_attempt_result(root, aid)
    if state['status'] != 'generated' or state['output'] != record['output']:
        raise ValueError('creator draft exception requires exact existing generated output')
    from lib.production_provenance import validate_attempt_provenance
    validate_attempt_provenance(root, aid, shot_id=record['shot_id'],
                               story_revision=record['story_revision'], expected_output=record['output'])
    failed_path = _bound(root, record['failed_review'])
    directory = (root / 'production_attempts' / aid / 'rejections').resolve()
    if failed_path.parent != directory:
        raise ValueError('creator draft exception requires retained original rejection')
    failure = execution._read(failed_path)
    predicates = _review(failure, record['output']['sha256'], record['story_revision'])
    name = record['failed_predicate']
    if (failure['status'] != 'fail' or name not in predicates
            or predicates[name]['status'] != 'fail' or predicates[name].get('severity', 'critical') != 'critical'):
        raise ValueError('creator draft exception must preserve named critical failure')
    review = selection.get('review')
    audio_valid = provisional_audio_review(review, root) if review is not None else False
    if creating:
        # Creation may precede the new selection review, but cannot invent audio authority.
        try:
            draft_audio_policy_digest(root)
            audio_valid = True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for retained_path in directory.glob('*.json'):
        retained = execution._read(retained_path)
        findings = _review(retained, record['output']['sha256'], record['story_revision'])
        if retained['status'] != 'fail':
            raise ValueError('retained rejection is not a failed review')
        for key, finding in findings.items():
            critical = key in CRITICAL_PREDICATES or finding.get('severity', 'critical') == 'critical'
            if critical and finding['status'] != 'pass':
                if key == name and finding == predicates[name]:
                    continue
                if key == 'speaker_source' and finding['status'] == 'unknown' and audio_valid:
                    continue
                raise ValueError('creator draft exception cannot hide another retained critical finding')
    outgoing = record['outgoing_review']
    clean = _review(outgoing, record['outgoing_frame']['sha256'], record['story_revision'])
    required = UPSTREAM_PREDICATES | {'prop_body_invariants', name}
    if (outgoing['status'] != 'pass' or not required.issubset(clean)
            or any(p['status'] != 'pass' or p.get('severity', 'critical') != 'critical' for p in clean.values())):
        raise ValueError('creator draft exception requires independently passing clean outgoing evidence')
    if review is not None:
        selected = _review(review, selection_digest(selection), record['story_revision'])
        if (review['status'] != 'provisional' or not UPSTREAM_PREDICATES.issubset(selected)
                or selected.get(name) != predicates[name]
                or any(p['status'] != 'pass' and not (key == 'speaker_source' and p['status'] == 'unknown'
                    and provisional_audio_review(review, root)) for key, p in selected.items() if key != name)
                or any(p.get('severity', 'critical') != 'critical' for p in selected.values())):
            raise ValueError('creator draft exception preserves one failure and no other uncertainty')
    return name


def accepted_draft_predicate(selection, project_dir, *, shot_id=None):
    """Fail closed predicate allowance for the three draft selection/upstream gates."""
    review = selection.get('review', {})
    if review.get('status') != 'provisional' or 'creator_draft_exception' not in review:
        return None
    try:
        root = Path(project_dir).resolve()
        path = _bound(root, review['creator_draft_exception'])
        # Only the canonical append helper's content-addressed record location.
        if path.parent != root / 'production_draft_exceptions' or path.name != file_sha256(path) + '.json':
            return None
        return validate_creator_draft_exception(root, json.loads(path.read_text()), selection, shot_id=shot_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None


@_write
def append_creator_draft_exception(project_dir, record):
    """Append reviewed existing-footage exception; never reserve or dispatch."""
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    with execution._lock(root):
        selection = {k: record[k] for k in ('attempt_id', 'output', 'outgoing_frame')}
        validate_creator_draft_exception(root, record, selection, shot_id=record['shot_id'], creating=True)
        # Hash exact serialized bytes, matching _write_new's durable encoding.
        directory = root / 'production_draft_exceptions'
        directory.mkdir(exist_ok=True)
        raw = json.dumps(record, indent=2).encode()
        import hashlib
        digest = hashlib.sha256(raw).hexdigest()
        path = directory / (digest + '.json')
        execution._write_new(path, copy.deepcopy(record))
        return {'path': str(path), 'sha256': file_sha256(path)}


# ---------------------------------------------------------------------------
# First cut: playable attributable candidates, preview export, creator acceptance.
# Explicit calls only; never writes checkpoints, reserves attempts or repairs.

_CUTS, _ACCEPTANCES = 'production_first_cuts', 'production_first_cut_acceptances'
_SOURCES = 'production_source_clips'
_TITLE_CARD_KEYS = ('before_shot_id', 'text', 'duration_seconds')
_SAFE_ID = r'[A-Za-z0-9][A-Za-z0-9_-]*'


def _probe(path):
    """Current-byte technical evidence for a playable media file."""
    process = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type,codec_name,width,height', '-of', 'json', str(path)],
        capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(process.stdout)
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if video is None:
        raise ValueError('no video stream')
    return {'duration_seconds': round(float(data['format']['duration']), 3),
            'video_codec': video.get('codec_name'), 'audio_codec': audio and audio.get('codec_name'),
            'has_audio': audio is not None, 'width': video.get('width'), 'height': video.get('height')}


def _rel(root, path):
    path = Path(path).resolve()
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def _selection_review(root, shot_id, selection, revision):
    """Canonical current review of a selection, or None when invalid or stale.

    Reuses successor resolution and the selection binding (schema, story
    revision, selection_digest subject, unique predicates)."""
    if not isinstance(selection, dict):
        return None
    from lib.shot_contract import resolve_selection_review
    try:
        review = resolve_selection_review(root, shot_id, selection)
        _review(review, selection_digest(selection), revision)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return review


def _dependency_ok(root, shot_id, selection, review):
    """Upstream status gate shared with shot_contract.check_review(allow_draft=True)."""
    return review is not None and (review['status'] == 'pass' or provisional_audio_review(review, root)
                                   or accepted_draft_predicate(selection, root, shot_id=shot_id) is not None)


def _current_findings(root, shot_id, attempt_id, output_sha, revision, selected):
    """Single findings resolution for candidates and cut/acceptance currency.

    A current selection of these exact bytes supersedes older rejections: its
    resolved review's non-pass predicates (cosmetic, unknown) are the findings.
    Otherwise retained rejections bound to the bytes and revision apply."""
    from lib import production_execution as execution
    selection = selected.get(shot_id)
    review = None
    if (isinstance(selection, dict) and selection.get('attempt_id') == attempt_id
            and (selection.get('output') or {}).get('sha256') == output_sha):
        review = _selection_review(root, shot_id, selection, revision)
    if review is not None:
        reviews = [review]
    else:
        reviews = []
        for base in (root / 'production_attempts', root / 'openart_mcp' / 'attempts'):
            for path in sorted((base / attempt_id / 'rejections').glob('*.json')):
                rejection = execution._read(path)
                if (rejection.get('subject_sha256') == output_sha
                        and rejection.get('story_revision', revision) == revision):
                    reviews.append(rejection)
    found, seen = [], set()
    for item in (p for r in reviews for p in r.get('predicates', [])):
        if item.get('status') == 'pass':
            continue
        entry = {'name': item.get('name'), 'status': item.get('status'),
                 'severity': item.get('severity', 'critical'), 'evidence': item.get('evidence')}
        key = json.dumps(entry, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            found.append(entry)
    return review, found


def _attempt_order(root, row):
    """Retained creation chronology: the file each attempt writes once at creation
    (native request.json, MCP evidence.json), then its scope attempt index."""
    if row.get('provider') == 'openart_mcp':
        path = root / 'openart_mcp' / 'attempts' / row['attempt_id'] / 'evidence.json'
    else:
        path = root / 'production_attempts' / row['attempt_id'] / 'request.json'
    return (path.stat().st_mtime_ns if path.exists() else 0, row.get('scope_attempt_index', 0))


def _candidate_provenance(root, shot_id, attempt_id, output, revision, *, allow_historical=True):
    """Current attempt provenance, or the pure historical source reader when only
    unrelated planning/upstream changed after the original request."""
    from lib.production_provenance import validate_attempt_provenance
    output = {**output, 'path': str((root / output['path']).resolve())}
    try:
        return validate_attempt_provenance(root, attempt_id, shot_id=shot_id,
                                           story_revision=revision, expected_output=output), False
    except ValueError as exc:
        changed = any(message in str(exc) for message in (
            'upstream selection changed since this attempt', 'canonical reviewed source/reference/upstream changed',
            'stale source/reference/review/upstream bindings', 'current MCP planning semantics changed',
            'approved planning semantics changed'))
        if not allow_historical or not changed:
            raise
        from lib.production_provenance import historical_source_provenance
        proof = historical_source_provenance(root, attempt_id, shot_id=shot_id,
            story_revision=revision, expected_output=output)
        if proof.get('historical_upstream_changed') is False and not proof.get('historical_source_changed'):
            raise exc
        return proof, True


def _historical_source_warning(proof):
    if proof.get('historical_upstream_changed') is False:
        return {'name': 'source_applicability_changed', 'status': 'unknown', 'severity': 'critical',
                'evidence': 'Retained clip matches its original shot and source bytes. Unrelated episode planning changed; current cut applicability is unreviewed.'}
    return {'name': 'upstream_source_changed', 'status': 'unknown', 'severity': 'critical',
            'evidence': 'Retained clip used an earlier upstream selection; current applicability is unreviewed.'}


def _candidate(root, shot_id, attempt_id, output, revision, selected):
    """A playable collected original; semantic findings are disclosed, never excluding."""
    proof, historical = _candidate_provenance(root, shot_id, attempt_id, output, revision)
    path = Path(output['path'])
    path = path if path.is_absolute() else root / path
    timing = (proof or {}).get('derived_timing') if isinstance(proof, dict) else None
    duration = timing['duration_seconds'] if timing else _probe(path)['duration_seconds']
    review, findings = _current_findings(root, shot_id, attempt_id, output['sha256'], revision, selected)
    if proof.get('derived_edit'):
        # Disk-loaded editorial selections retain the same outgoing and review
        # bindings as registration; a stale review cannot select changed bytes.
        selection = selected.get(shot_id)
        if (not isinstance(selection, dict) or selection.get('attempt_id') != attempt_id
                or selection.get('output', {}).get('sha256') != output['sha256']
                or selection.get('outgoing_frame') != proof['selected_outgoing_frame'] or review is None):
            raise ValueError('derived selection outgoing frame or current review differs')
        from lib.shot_contract import (factory_project, frozen_factory_authority,
                                       required_review_predicates, UPSTREAM_PREDICATES, audio_review_disabled)
        required = required_review_predicates(root, UPSTREAM_PREDICATES)
        predicates = {item['name']: item for item in review['predicates']}
        if (not required.issubset(predicates)
                or any(predicates[name].get('severity') == 'cosmetic' for name in required)):
            raise ValueError('derived selection has missing/invalid review predicates')
        if review['status'] == 'pass':
            if any(item['status'] != 'pass' for name, item in predicates.items()
                   if not (name == 'speaker_source' and audio_review_disabled(root))
                   and (item.get('severity', 'critical') == 'critical' or name in required)):
                raise ValueError('derived selection has failed critical predicates')
        elif (not provisional_audio_review(review, root)
                and accepted_draft_predicate(selection, root, shot_id=shot_id) is None):
            if (review['status'] not in {'unknown', 'fail'} or not factory_project(root)
                    or not frozen_factory_authority(root, proof['request'].get('scope'))):
                raise ValueError('derived selection lacks current factory observation authority')
    strict = review is not None and review['status'] == 'pass' and not historical
    status = review['status'] if review is not None else ('fail' if findings else 'unknown')
    if historical:
        # A failed observation bound to these exact bytes stays truthful; the
        # historical source warning only adds unknown applicability.
        findings, status = [*findings, _historical_source_warning(proof)], ('fail' if any(
            f.get('status') == 'fail' for f in findings) else 'unknown')
    return {'shot_id': shot_id, 'status': 'candidate', 'attempt_id': attempt_id,
            'output': {'path': _rel(root, path), 'sha256': output['sha256']},
            'duration_seconds': duration, 'strict_selected': strict,
            'review': status, 'findings': findings}


def first_cut_candidates(project_dir):
    """One row per planned shot, contract order: a playable candidate or a precise gap.

    Missing reasons: no_attempt, original_pending{status} (newest non-terminal
    native or MCP state), attempt_failed (all attempts terminally failed),
    unplayable (provenance or current-byte probe failed), upstream_blocked
    (no attempt and the contract upstream is not strictly selected).
    """
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    marker = execution._read(root / 'project.json')
    revision = marker['story_revision']
    contract = execution.load_shot_contract(root)
    selected = execution.load_selected_attempts(root)
    try:
        attempts = execution._attempts(root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Diagnostic enumeration alone may isolate an invalid MCP original.
        # Raw frozen identity only locates its gap; it never admits its media
        # or replaces the fail-closed authority/counter enumeration.
        from lib import openart_mcp_jobs as jobs
        attempts = [execution._read(path) for path in sorted((root / 'production_attempts').glob('*/request.json'))]
        for path in sorted((root / 'openart_mcp' / 'attempts').glob('*/state.json')):
            try:
                raw = execution._read(path)
            except (OSError, ValueError):
                continue  # An unreadable identity cannot locate a shot gap.
            error = None
            try:
                raw = jobs._load(root, path.parent.name)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                error = str(exc)[:200]
            try:
                authority = raw['frozen']['authority']
                shot_id, story_revision = authority['shot_id'], authority['scope']['story_revision']
                index = authority.get('scope_attempt_index', 0)
                if not isinstance(shot_id, str) or not isinstance(story_revision, str):
                    continue
            except (KeyError, TypeError, AttributeError):
                continue  # Malformed diagnostic identity never admits media.
            attempts.append({'provider': 'openart_mcp', 'attempt_id': path.parent.name,
                'shot_id': shot_id, 'story_revision': story_revision,
                'scope_attempt_index': index if isinstance(index, int) else 0,
                **({'unplayable_evidence': error} if error is not None else {})})
    attempts = [a for a in attempts if a.get('story_revision') == revision]
    from lib.shot_contract import factory_project
    factory = factory_project(root)
    rows, by_shot = [], {}
    errors = (execution.ProductionGovernanceError, OSError, ValueError, KeyError, TypeError,
              subprocess.SubprocessError)
    for shot in contract['shots']:
        sid = shot['id']
        selection = selected.get(sid)
        row = None
        if isinstance(selection, dict) and selection.get('output'):
            try:
                row = _candidate(root, sid, selection['attempt_id'], selection['output'], revision, selected)
            except errors:
                row = None
        own = sorted((a for a in attempts if a.get('shot_id') == sid and a.get('media_kind', 'motion') == 'motion'),
                     key=lambda a: _attempt_order(root, a), reverse=True)
        states = []
        if row is None:
            for attempt in own:
                if attempt.get('unplayable_evidence'):
                    states.append('unplayable:' + attempt['unplayable_evidence'])
                    continue
                try:
                    state = execution.load_attempt_result(root, attempt['attempt_id'])
                except execution.ProductionGovernanceError:
                    state = {'status': 'uncertain'}
                states.append(state.get('status'))
                if state.get('status') == 'generated' and row is None:
                    try:
                        row = _candidate(root, sid, attempt['attempt_id'], state['output'], revision, selected)
                    except errors as exc:
                        states[-1] = 'unplayable:' + str(exc)[:200]
        external = root / _SOURCES / (sid + '.json')
        if row is None and external.is_file():
            # A registered completion clip from another project fills only a
            # shot this project cannot place; its own playable row always wins.
            try:
                row = _source_clip_row(root, execution._read(external))
            except errors as exc:
                row = {'shot_id': sid, 'status': 'missing', 'reason': 'unplayable',
                       'detail': {'error': 'external source clip: ' + str(exc)[:200]}}
        if row is None:
            # An upstream counts only when its own row is a playable candidate
            # (current bytes and provenance) and its review passes the canonical gate.
            # One-shot factory: each clip has its own prepared frame, so an
            # upstream is an ordering note; generic manifests keep the gate.
            upstream = [] if factory else [b['shot_id'] for b in shot.get('upstream', [])
                        if by_shot.get(b['shot_id'], {}).get('status') != 'candidate'
                        or not _dependency_ok(root, b['shot_id'], selected.get(b['shot_id']),
                                              _selection_review(root, b['shot_id'], selected.get(b['shot_id']), revision))]
            pending = [s for s in states if s not in ('generated', 'failed') and not str(s).startswith('unplayable')]
            if pending:
                # Non-terminal native/MCP state (uncertain, prepared, submitted,
                # awaiting receipt, completed-not-collected): truthful pending.
                row = {'reason': 'original_pending', 'detail': {'status': pending[0]}}
            elif any(str(s).startswith('unplayable') for s in states):
                row = {'reason': 'unplayable',
                       'detail': {'error': next(s for s in states if str(s).startswith('unplayable'))[11:]}}
            elif states:
                row = {'reason': 'attempt_failed', 'detail': {'attempts': len(states)}}
            elif upstream:
                prior = by_shot.get(upstream[0], {})
                why = ('failed' if prior.get('review') == 'fail' else
                       'unreviewed' if prior.get('status') == 'candidate' else 'missing')
                row = {'reason': 'upstream_blocked', 'detail': {'upstream_shot_id': upstream[0], 'why': why}}
            else:
                row = {'reason': 'no_attempt', 'detail': {}}
            row = {'shot_id': sid, 'status': 'missing', **row}
        by_shot[sid] = row
        rows.append(row)
    return rows


def _source_clip_row(root, record):
    """Current candidate row for a registered external completion clip.

    The source project's own candidate rules decide provenance, duration,
    review and findings. The final project places its retained byte copy.
    """
    from lib import production_execution as execution
    source = record['source_project']
    copy = record['copy']
    path = (root / copy['path']).resolve()
    if (path != root / _SOURCES / source['output']['sha256'] / 'output.mp4' or not path.is_file()
            or copy['sha256'] != source['output']['sha256'] or file_sha256(path) != copy['sha256']):
        raise ValueError('external source clip copy is missing or changed')
    source_root = Path(source['project_dir'])
    marker = execution._read(source_root / 'project.json')
    if (marker['project_id'], marker['story_revision']) != (source['project_id'], source['story_revision']):
        raise ValueError('external source project identity or story revision changed')
    row = _candidate(source_root, source['shot_id'], source['attempt_id'], source['output'],
                     source['story_revision'], execution.load_selected_attempts(source_root))
    return {**row, 'shot_id': record['shot_id'], 'output': dict(copy), 'source_project': source}


def _register_source_clips(root, source_clips):
    """Retain exact external completion clips for shots this project cannot place.

    Preserve every registration and copy. A current registration may advance
    only to a validated derivative of the same source project's native attempt.
    """
    import re
    import shutil
    import tempfile
    from lib import production_execution as execution
    if not isinstance(source_clips, list):
        raise ValueError('source_clips must be a list')
    marker = execution._read(root / 'project.json')
    rows = {row['shot_id']: row for row in first_cut_candidates(root)}
    for entry in source_clips:
        if (not isinstance(entry, dict) or not {'shot_id', 'project_dir', 'attempt_id', 'output'} <= set(entry)
                or not set(entry) <= {'shot_id', 'project_dir', 'attempt_id', 'output', 'source_shot_id'}):
            raise ValueError('source clip needs shot_id, project_dir, attempt_id, output and optional source_shot_id')
        shot_id, source_shot = entry['shot_id'], entry.get('source_shot_id', entry['shot_id'])
        output = entry['output']
        if (any(not isinstance(v, str) or not re.fullmatch(_SAFE_ID, v)
                for v in (shot_id, source_shot, entry['attempt_id']))
                or not isinstance(entry['project_dir'], str) or not Path(entry['project_dir']).is_absolute()
                or not isinstance(output, dict) or set(output) != {'path', 'sha256'}
                or not isinstance(output['path'], str) or not output['path'] or Path(output['path']).is_absolute()
                or not isinstance(output['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', output['sha256'])):
            raise ValueError('source clip identity or output binding is invalid')
        if shot_id not in rows:
            raise ValueError('source clip shot is not planned in this project: ' + shot_id)
        source_root = Path(entry['project_dir']).resolve()
        source_path = (source_root / output['path']).resolve()
        if (source_root == root or not source_path.is_relative_to(source_root)
                or not source_path.is_file() or file_sha256(source_path) != output['sha256']):
            raise ValueError('source clip must be exact bytes inside a different project')
        source_marker = execution._read(source_root / 'project.json')
        source = {'project_dir': str(source_root), 'project_id': source_marker['project_id'],
                  'story_revision': source_marker['story_revision'], 'attempt_id': entry['attempt_id'],
                  'shot_id': source_shot,
                  'output': {'path': _rel(source_root, source_path), 'sha256': output['sha256']}}
        record = {'version': '1.0', 'kind': 'external_source_clip', 'project_id': marker['project_id'],
                  'shot_id': shot_id, 'source_project': source,
                  'copy': {'path': f"{_SOURCES}/{output['sha256']}/output.mp4", 'sha256': output['sha256']}}
        path = root / _SOURCES / (shot_id + '.json')
        previous = execution._read(path) if path.exists() else None
        if path.exists():
            if previous == record:
                _source_clip_row(root, record)
                continue
            stable = ('project_dir', 'project_id', 'story_revision', 'attempt_id', 'shot_id')
            if any(previous['source_project'][key] != source[key] for key in stable):
                raise ValueError('shot already has a different registered external source clip: ' + shot_id)
        if rows[shot_id]['status'] == 'candidate' and 'source_project' not in rows[shot_id]:
            raise ValueError('shot already has a playable clip in this project: ' + shot_id)
        # Validate under the source project's own candidate rules before copying.
        _candidate(source_root, source_shot, entry['attempt_id'], source['output'],
                   source['story_revision'], execution.load_selected_attempts(source_root))
        if previous is not None:
            proof, _ = _candidate_provenance(source_root, source_shot, source['attempt_id'],
                                            source['output'], source['story_revision'])
            derived = proof.get('derived_edit')
            if not derived or derived['parent_attempt_id'] != source['attempt_id']:
                raise ValueError('external source reselection requires a validated same-parent derivative')
            old_source = previous['source_project']
            old_proof, _ = _candidate_provenance(source_root, source_shot, source['attempt_id'],
                                                old_source['output'], source['story_revision'])
            old_parent = old_proof.get('derived_edit', {}).get('parent_output', old_source['output'])
            old_parent = {**old_parent, 'path': str((source_root / old_parent['path']).resolve())}
            if old_parent != derived['parent_output']:
                raise ValueError('external source reselection changed the native parent')
        copy = root / record['copy']['path']
        copy.parent.mkdir(parents=True, exist_ok=True)
        if not copy.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=copy.parent, suffix='.tmp', delete=False) as retained:
                    temporary = Path(retained.name)
                    with source_path.open('rb') as incoming:
                        shutil.copyfileobj(incoming, retained)
                    retained.flush()
                    os.fsync(retained.fileno())
                if file_sha256(temporary) != output['sha256']:
                    raise ValueError('external source bytes changed while copying')
                temporary.chmod(0o444)
                # Publish complete verified bytes without replacing any final
                # file another writer may already have retained.
                try:
                    os.link(temporary, copy)
                except FileExistsError:
                    if file_sha256(copy) != output['sha256']:
                        raise ValueError('external source clip copy is changed')
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        _source_clip_row(root, record)
        history = root / _SOURCES / 'registrations' / shot_id
        history.mkdir(parents=True, exist_ok=True)
        for retained_record in ([previous, record] if previous is not None else [record]):
            retained_path = history / (execution._digest(retained_record) + '.json')
            if retained_path.exists():
                if execution._read(retained_path) != retained_record:
                    raise ValueError('retained external source registration changed')
            else:
                execution._write_new(retained_path, retained_record)
        # Every immutable historical binding exists before the current pointer
        # changes. A crash leaves either the old or the complete new record.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, suffix='.tmp', delete=False) as current:
                temporary = Path(current.name)
                json.dump(record, current, indent=2, allow_nan=False)
                current.flush()
                os.fsync(current.fileno())
            temporary.chmod(0o444)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _resolve_title_cards(root, title_cards):
    """Explicit card specs; None keeps the latest first cut's cards, [] removes them."""
    import math
    from lib import production_execution as execution
    if title_cards is None:
        latest = latest_first_cut(root)
        return [{key: card[key] for key in _TITLE_CARD_KEYS} for card in (latest or {}).get('title_cards', [])]
    shots = {shot['id'] for shot in execution.load_shot_contract(root)['shots']}
    if not isinstance(title_cards, list):
        raise ValueError('title_cards must be a list')
    cards = []
    for card in title_cards:
        if not isinstance(card, dict) or set(card) != set(_TITLE_CARD_KEYS):
            raise ValueError('title card needs exactly before_shot_id, text and duration_seconds')
        duration, text = card['duration_seconds'], card['text']
        if (card['before_shot_id'] not in shots or not isinstance(text, str) or not text.strip()
                or len(text) > 120 or type(duration) not in (int, float)
                or not math.isfinite(duration) or not 0 < duration <= 3):
            raise ValueError('title card needs a planned shot, 1-120 characters and 0 < duration <= 3 seconds')
        cards.append({key: card[key] for key in _TITLE_CARD_KEYS})
    if len({card['before_shot_id'] for card in cards}) != len(cards):
        raise ValueError('at most one title card may precede each shot')
    return cards


def _render_title_card(path, card, width, height):
    """Silent black FFmpeg drawtext card at the cut geometry; written atomically."""
    lines = card['text'].splitlines() or [card['text']]
    size = int(min(height / 14, width * 0.9 / (0.6 * max(len(line) for line in lines))))
    text = path.with_suffix('.txt')
    text.write_text(card['text'])
    temporary = path.with_name(path.stem + '.tmp.mp4')
    duration = card['duration_seconds']
    subprocess.run(['ffmpeg', '-v', 'error', '-y',
        '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r=30:d={duration}',
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
        '-vf', f'drawtext=textfile={text.name}:fontcolor=white:fontsize={max(size, 12)}'
               ':line_spacing=8:expansion=none:x=(w-text_w)/2:y=(h-text_h)/2',
        '-t', str(duration), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-map_metadata', '-1', temporary.name],
        cwd=path.parent, capture_output=True, check=True, timeout=120)
    temporary.replace(path)


def _cut_records(root):
    from lib import production_execution as execution
    return [(path, execution._read(path)) for path in sorted((root / _CUTS).glob('*.json'))]


def latest_first_cut(project_dir):
    """Highest-sequence first-cut record with its binding, or None."""
    root = Path(project_dir).resolve()
    records = _cut_records(root)
    if not records:
        return None
    path, record = max(records, key=lambda item: item[1]['sequence'])
    return {'path': str(path), 'sha256': file_sha256(path), **record}


def _profile_issues(path, profile):
    """Proportionate technical checks for an explicitly requested export profile.

    Playable geometry/timing only; never an audio listening or semantic gate."""
    from lib.media_profiles import delivery_timing_issues, delivery_first_video_packet_issues
    process = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                             capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(process.stdout)
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
    issues = []
    if (video.get('width'), video.get('height')) != (profile.width, profile.height):
        issues.append(f"geometry {video.get('width')}x{video.get('height')} differs from "
                      f'{profile.name} {profile.width}x{profile.height}')
    issues += delivery_timing_issues(data, target_fps=profile.fps)
    issues += delivery_first_video_packet_issues(path)
    return issues


class RenderProbeUncertain(RuntimeError):
    """Retryable local verification failure; not evidence to replace media."""


def _confirmed_incomplete_render(exc):
    """A completed ffprobe explicitly identifies missing MP4 metadata.

    A timeout, absent tool or unspecified nonzero exit proves no media defect.
    Keep this allowance narrow rather than guessing from any probe failure.
    """
    return (isinstance(exc, subprocess.CalledProcessError) and exc.returncode == 1
            and isinstance(exc.stderr, str) and 'moov atom not found' in exc.stderr
            and 'Invalid data found when processing input' in exc.stderr)


def _verify_render(output, profile, expected_duration, count, *, verify_duration=True):
    """Return confirmed technical issues; preserve media on probe uncertainty.

    Fresh previews retain playable duration gaps for incomplete disclosure;
    resumed intents require their expected duration before reusing the output.
    """
    try:
        technical = _probe(output)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if _confirmed_incomplete_render(exc):
            return None, 'render incomplete: ' + exc.stderr.strip()
        raise RenderProbeUncertain(f'render probe failed ({type(exc).__name__}): {exc}; '
                                   'existing output preserved; retry this compose operation') from exc
    if verify_duration and abs(technical['duration_seconds'] - expected_duration) > 0.05 + 0.04 * count:
        return technical, f"render duration {technical['duration_seconds']} differs from {expected_duration}"
    if profile is not None:
        try:
            issues = _profile_issues(output, profile)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            if _confirmed_incomplete_render(exc):
                return technical, 'render incomplete: ' + exc.stderr.strip()
            raise RenderProbeUncertain(f'render profile probe failed ({type(exc).__name__}): {exc}; '
                                       'existing output preserved; retry this compose operation') from exc
        if any(issue.startswith('First-packet timing check failed:') for issue in issues):
            raise RenderProbeUncertain('render profile probe failed: ' + '; '.join(issues)
                                       + '; existing output preserved; retry this compose operation')
        if issues:
            return technical, '; '.join(issues)
    return technical, None


_INTENT = 'compose-intent.json'


def _export_intact(root, record):
    """Recorded export still exists with its exact bytes (or none was rendered)."""
    export = record.get('export')
    if export is None:
        return not record.get('clips')
    path = root / export['path']
    return path.is_file() and file_sha256(path) == export['sha256']


def _title_cards_intact(root, record):
    """Every inserted card file still has its recorded bytes."""
    return all((root / card['output']['path']).is_file()
               and file_sha256(root / card['output']['path']) == card['output']['sha256']
               for card in record.get('title_cards', []))


def _compose_bindings(root, clips, export_profile, title_cards=()):
    """Exact inputs a compose intent binds: current clip bytes and profile.

    Keep the historical ``audio: null`` slot so already-saved no-music intents
    retain their digest. The factory no longer writes or accepts external audio.
    Title cards and external source projects appear only when used, so older
    operation digests are unchanged.
    """
    bindings = {'clips': [{'shot_id': row['shot_id'], 'attempt_id': row['attempt_id'], 'output': row['output'],
                           **({'source_project': row['source_project']} if 'source_project' in row else {})}
                          for row in clips],
                'export_profile': export_profile, 'audio': None}
    if title_cards:
        bindings['title_cards'] = [{key: card[key] for key in _TITLE_CARD_KEYS} for card in title_cards]
    for row in clips:
        source = root / row['output']['path']
        if not source.is_file() or file_sha256(source) != row['output']['sha256']:
            raise ValueError('first-cut candidate bytes changed before compose: ' + row['shot_id'])
    return bindings


def _clip_media_bindings(clips, *, placed=False, title_cards=()):
    """Ordered placement identity and timing, excluding mutable review notes.

    Unplaced candidates are shifted by the retained card durations so they
    compare with a placed record that inserted those cards."""
    before = {card['before_shot_id']: card['master_end'] - card['master_start'] for card in title_cards}
    cursor, bindings = 0.0, []
    for clip in clips:
        if not placed:
            cursor = round(cursor + before.get(clip['shot_id'], 0), 3)
        start = clip['master_start'] if placed else cursor
        end = clip['master_end'] if placed else round(cursor + clip['duration_seconds'], 3)
        bindings.append({key: clip[key] for key in ('shot_id', 'attempt_id', 'output', 'source_project')
                         if key in clip} | {'master_start': round(start, 3), 'master_end': end})
        cursor = end
    return bindings


def _find_intent(root, operation_id):
    """Latest (highest-sequence) compose intent for ``operation_id``."""
    found = (None, None)
    for path in sorted((root / 'renders' / 'first_cut').glob('v*/' + _INTENT)):
        try:
            intent = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if intent.get('operation_id') == operation_id and (
                found[1] is None or intent.get('sequence', 0) > found[1].get('sequence', 0)):
            found = (path, intent)
    return found


@_write
@_delivery_serialized
def compose_first_cut(project_dir, *, compose=None, export_profile=None, operation_id=None,
                      title_cards=None, source_clips=None):
    """Render a versioned preview from current candidates and record it immutably.

    ``export_profile`` (explicit media profile name) is validated before any
    render and technically checked after it; without one the first clip's
    geometry is retained. ``operation_id`` writes a compose intent inside the
    render directory before rendering, so a crashed operation resumes: an
    existing record is returned, a complete local render is recorded without
    re-rendering. A confirmed technical mismatch or incomplete MP4 preserves
    the original beside its replacement; probe uncertainty preserves it for retry.
    Changed clip/profile bindings reject the stale intent.

    ``title_cards`` inserts short silent edit-only cards before named shots
    (None keeps the latest cut's cards, [] removes them). ``source_clips``
    registers exact external completion clips for shots this project cannot
    place; registrations persist for later calls."""
    from lib import production_execution as execution
    from lib.shot_contract import contract_digest
    from lib.events import emit_event
    from lib.media_profiles import get_profile
    root = Path(project_dir).resolve()
    profile = get_profile(export_profile) if export_profile is not None else None
    if operation_id is not None and (not isinstance(operation_id, str) or not operation_id.strip()):
        raise ValueError('compose operation_id must be a nonempty string')
    marker = execution._read(root / 'project.json')
    contract = execution.load_shot_contract(root)
    if source_clips is not None:
        _register_source_clips(root, source_clips)
    cards = _resolve_title_cards(root, title_cards)
    rows = first_cut_candidates(root)
    clips = [row for row in rows if row['status'] == 'candidate']
    missing = [row for row in rows if row['status'] == 'missing']
    placed_shots = {row['shot_id'] for row in clips}
    if any(card['before_shot_id'] not in placed_shots for card in cards):
        raise ValueError('title card must precede a shot placed in this cut')
    bindings = _compose_bindings(root, clips, export_profile, cards) if operation_id is not None else None
    bindings_sha256 = execution._digest(bindings) if operation_id is not None else None
    recorded = set()
    latest_sequence = max([record['sequence'] for _, record in _cut_records(root)], default=0)
    if operation_id is not None:
        # Same operation replays only with identical clip/profile bindings.
        # A retained render failure stays as history; the operation resumes with a
        # new local render under a higher sequence so the retry is latest.
        # Newest valid matching record wins, so a restored older export never
        # shadows a later local recovery.
        for path, record in sorted(_cut_records(root), key=lambda item: item[1]['sequence'], reverse=True):
            if record.get('operation_id') != operation_id:
                continue
            if record.get('compose_bindings_sha256') != bindings_sha256:
                raise ValueError('compose operation was recorded with different clips, profile or audio')
            recorded.add(record['sequence'])
            if (record['sequence'] == latest_sequence and record['status'] != 'render_failed'
                    and _export_intact(root, record)
                    and _title_cards_intact(root, record)
                    and _clip_media_bindings(record['clips'], placed=True)
                    == _clip_media_bindings(clips, title_cards=record.get('title_cards', []))):
                return {'path': str(path), 'sha256': file_sha256(path), **record}
    intent_path, intent = _find_intent(root, operation_id) if operation_id is not None else (None, None)
    if intent is not None:
        if {key: intent.get(key) for key in bindings} != bindings:
            raise ValueError('compose intent bindings differ from current clips, profile or audio')
        if intent['sequence'] in recorded:
            intent = None
    if intent is not None:
        sequence, directory = intent['sequence'], intent_path.parent
    else:
        sequence = 1 + max([record['sequence'] for _, record in _cut_records(root)], default=0)
        while True:
            directory = root / 'renders' / 'first_cut' / f'v{sequence:03d}'
            try:
                directory.mkdir(parents=True)
                break
            except FileExistsError:
                sequence += 1
    card_geometry = None
    if cards:
        profile_geometry = (profile.width, profile.height) if profile is not None else None
        first = _probe(root / clips[0]['output']['path'])
        card_geometry = profile_geometry or (first['width'], first['height'])
    cursor, placed, placed_cards = 0.0, [], []
    by_shot = {card['before_shot_id']: card for card in cards}
    for row in clips:
        card = by_shot.get(row['shot_id'])
        if card is not None:
            card_path = directory / f"title-{row['shot_id']}.mp4"
            if not card_path.is_file():
                _render_title_card(card_path, card, *card_geometry)
            rendered_duration = _probe(card_path)['duration_seconds']
            start, cursor = cursor, round(cursor + rendered_duration, 3)
            placed_cards.append({**card, 'output': {'path': _rel(root, card_path), 'sha256': file_sha256(card_path)},
                                 'rendered_duration_seconds': rendered_duration,
                                 'master_start': round(start, 3), 'master_end': cursor})
        start, cursor = cursor, round(cursor + row['duration_seconds'], 3)
        placed.append({'shot_id': row['shot_id'], 'attempt_id': row['attempt_id'], 'output': row['output'],
                       'master_start': round(start, 3), 'master_end': cursor,
                       'review': row['review'], 'findings': row['findings'],
                       **({'source_project': row['source_project']} if 'source_project' in row else {})})
    segments = sorted([*placed, *placed_cards], key=lambda item: item['master_start'])
    segment_count = len(segments)
    output = directory / 'first_cut.mp4'
    if operation_id is not None and intent is None:
        intent = {'version': '1.0', 'kind': 'first_cut_compose_intent', 'operation_id': operation_id,
                  'sequence': sequence, 'output': _rel(root, output), **bindings}
        temporary = directory / (_INTENT + '.tmp')
        temporary.write_text(json.dumps(intent, indent=2))
        temporary.replace(directory / _INTENT)
    export = technical = error = log = None
    observation = {'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
                   'cut_sequence': sequence, 'output_path': _rel(root, output)}
    if placed:
        resumed = None
        if operation_id is not None and output.is_file():
            technical, problem = _verify_render(output, profile, cursor, segment_count)
            if problem is None:
                resumed = technical
                emit_event(root, {'event': 'first-cut-compose-resumed', **observation, 'operation_id': operation_id})
            else:
                # A completed probe established a media defect. Preserve even those
                # rejected bytes before the same operation renders a replacement.
                retained = directory / ('first_cut.invalid-' + file_sha256(output) + '.mp4')
                if retained.exists():
                    if file_sha256(retained) != file_sha256(output):
                        raise ValueError('retained invalid render bytes changed')
                    output.unlink()
                else:
                    output.replace(retained)
                technical = None
        if resumed is None:
            first = _probe(root / placed[0]['output']['path'])
            metadata = {'preserve_source_audio': True}
            if profile is None:
                metadata['compose_target'] = {'width': first['width'], 'height': first['height'], 'fit': 'pad'}
            edit = {'version': '1.0', 'render_runtime': 'ffmpeg',
                    'cuts': [{'id': item.get('shot_id') or 'title-' + item['before_shot_id'],
                              'source': str(root / item['output']['path']), 'in_seconds': 0,
                              'out_seconds': item['master_end'] - item['master_start']} for item in segments],
                    'metadata': metadata}
            inputs = {'operation': 'compose', 'edit_decisions': edit, 'output_path': str(output)}
            if profile is not None:
                inputs['profile'] = profile.name
            if compose is None:
                from tools.video.video_compose import VideoCompose
                compose = VideoCompose().execute
            emit_event(root, {'event': 'first-cut-compose-start', **observation})
            try:
                result = compose(inputs)
            except BaseException:
                emit_event(root, {'event': 'first-cut-compose-error', **observation})
                raise
            emit_event(root, {'event': 'first-cut-compose-return', **observation, 'success': result.success})
        try:
            if resumed is None:
                if not result.success:
                    raise RuntimeError(result.error or 'compose failed')
                technical, problem = _verify_render(output, profile, cursor, segment_count, verify_duration=False)
                if problem:
                    raise ValueError('export technical check failed: ' + problem)
            export = {'path': _rel(root, output), 'sha256': file_sha256(output)}
            emit_event(root, {'event': 'first-cut-export-verified', **observation, 'export': export})
        except RenderProbeUncertain:
            # Keep the unrecorded output and its intent available for the same
            # operation's verification-only retry, including the first encode.
            raise
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            error = str(exc)
            log_path = directory / 'render_error.log'
            log_path.write_text(error + '\n')
            log = {'path': _rel(root, log_path), 'sha256': file_sha256(log_path)}
    # Audio presence remains available as factual probe metadata, but dialogue
    # streams and audio quality never gate this draft delivery.
    complete = (export is not None and not missing
                and abs(technical['duration_seconds'] - cursor) <= 0.05 + 0.04 * segment_count)
    record = {'version': '1.0', 'kind': 'first_cut', 'sequence': sequence, 'project_id': marker['project_id'],
              'story_revision': marker['story_revision'], 'contract_sha256': contract_digest(contract),
              'status': ('complete' if complete else 'render_failed' if error
                         else 'partial' if export is not None else 'incomplete'),
              'export': export, 'technical': technical, 'clips': placed, 'missing': missing,
              'audio': 'native_source',
              'render_error': error, 'render_log': log}
    if placed_cards:
        record['title_cards'] = placed_cards
    if export_profile is not None:
        record['export_profile'] = export_profile
    if operation_id is not None:
        record['operation_id'] = operation_id
        record['compose_bindings_sha256'] = bindings_sha256
    (root / _CUTS).mkdir(exist_ok=True)
    import hashlib
    path = root / _CUTS / (hashlib.sha256(json.dumps(record, indent=2).encode()).hexdigest() + '.json')
    if not path.exists():
        execution._write_new(path, record)
    emit_event(root, {'event': 'first-cut-retained', 'project_id': marker['project_id'],
                     'story_revision': marker['story_revision'], 'cut_sequence': sequence,
                     'first_cut': {'path': _rel(root, path), 'sha256': file_sha256(path)},
                     'export': export, 'status': record['status']})
    return {'path': str(path), 'sha256': file_sha256(path), **record}


@_write
@_delivery_serialized
def deliver_standalone(project_dir, *, export_profile=None, operation_id=None, compose=None,
                       title_cards=None, source_clips=None):
    """Engine-only saved draft cut from current candidates; no Studio, Drive or account.

    Idempotent per ``operation_id`` (default derives from current clip and
    profile/title-card bindings). Reports honest per-clip review/findings and
    missing shots; never certification, publication or provider work."""
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    if source_clips is not None:
        _register_source_clips(root, source_clips)
    cards = _resolve_title_cards(root, title_cards)
    if operation_id is None:
        if export_profile is not None:
            from lib.media_profiles import get_profile
            get_profile(export_profile)
        clips = [row for row in first_cut_candidates(root) if row['status'] == 'candidate']
        operation_id = 'deliver-' + execution._digest(_compose_bindings(root, clips, export_profile, cards))[:24]
    cut = compose_first_cut(root, compose=compose, title_cards=cards,
                            export_profile=export_profile, operation_id=operation_id)
    binding = {'path': _rel(root, cut['path']), 'sha256': cut['sha256']}
    status = first_cut_media_status(root, binding)
    return {'status': cut['status'], 'currency': status,
            'annotation_currency': first_cut_status(root, binding),
            'first_cut': {'path': _rel(root, cut['path']), 'sha256': cut['sha256']},
            'operation_id': operation_id, 'export': cut['export'],
            'export_path': str(root / cut['export']['path']) if cut['export'] else None,
            'export_profile': export_profile, 'technical': cut['technical'],
            'clips': [{**row, 'review': review, 'review_at_render': clip['review'], 'findings': findings}
                      for row, clip, (findings, review) in zip(disclosure_clip_map(cut), cut['clips'],
                                                               _current_clip_rows(root, cut['clips']))],
            'missing': cut['missing'], 'audio': cut['audio'], 'render_error': cut['render_error'],
            'title_cards': cut.get('title_cards', []),
            'not_certification': True, 'posted': False, 'provider_calls': 0}


def _current_clip_projection(root, clip, revision, selected):
    """One current resolution of a placed clip: (findings, review status).

    Resolves findings and provenance once, exactly like ``_candidate``, for both
    annotation currency and the delivered per-clip review. An external clip
    resolves inside its source project, exactly like that project's own row."""
    if 'source_project' in clip:
        from lib import production_execution as execution
        source = clip['source_project']
        root, revision = Path(source['project_dir']), source['story_revision']
        selected = execution.load_selected_attempts(root)
        clip = {**clip, 'shot_id': source['shot_id'], 'output': source['output']}
    review, values = _current_findings(root, clip['shot_id'], clip['attempt_id'], clip['output']['sha256'],
                                       revision, selected)
    try:
        proof, historical = _candidate_provenance(root, clip['shot_id'], clip['attempt_id'],
            clip['output'], revision, allow_historical=True)
    except (ValueError, OSError, KeyError, TypeError):
        historical = False
    if historical:
        return [*values, _historical_source_warning(proof)], ('fail' if any(
            v.get('status') == 'fail' for v in values) else 'unknown')
    return values, review['status'] if review is not None else ('fail' if values else 'unknown')


def _current_inputs(root):
    from lib import production_execution as execution
    return execution._read(root / 'project.json')['story_revision'], execution.load_selected_attempts(root)


@_status_read
def _current_review_status(root, clip, findings):
    """Current review status of a placed clip, resolved like ``_candidate``."""
    return _current_clip_projection(root, clip, *_current_inputs(root))[1]


@_status_read
def _clip_findings(root, clips, *, current):
    """Per-clip findings: retained at render time, or projected from current notes."""
    if current:
        inputs = _current_inputs(root)
    return [{'shot_id': clip['shot_id'],
             'findings': _current_clip_projection(root, clip, *inputs)[0] if current else clip['findings']}
            for clip in clips]


@_status_read
def _current_clip_rows(root, clips):
    """Delivered per-clip (findings, review status) from one resolution each."""
    inputs = _current_inputs(root)
    return [_current_clip_projection(root, clip, *inputs) for clip in clips]


def _findings_digest(root, clips, *, current):
    from lib import production_execution as execution
    return execution._digest(_clip_findings(root, clips, current=current))


def _cut_reasons(root, cut, *, include_findings=True):
    """Stale reasons for a first-cut binding {path, sha256}; empty when current."""
    from lib import production_execution as execution
    from lib.shot_contract import contract_digest
    path = (root / cut['path']).resolve()
    if (path.parent != root / _CUTS or not path.is_file() or file_sha256(path) != cut['sha256']
            or path.name != cut['sha256'] + '.json'):
        return ['record_changed']
    record = execution._read(path)
    reasons = []
    if record['export'] is None:
        # Intentional absence: an incomplete cut retains no usable clip.
        if record.get('status') != 'incomplete' or record['clips']:
            reasons.append('export_changed')
    elif not (root / record['export']['path']).is_file() \
            or file_sha256(root / record['export']['path']) != record['export']['sha256']:
        reasons.append('export_changed')
    for clip in record['clips']:
        source = root / clip['output']['path']
        if not source.is_file() or file_sha256(source) != clip['output']['sha256']:
            reasons.append('source_changed:' + clip['shot_id'])
    if not _title_cards_intact(root, record):
        reasons.append('title_card_changed')
    if execution._read(root / 'project.json')['story_revision'] != record['story_revision']:
        reasons.append('story_revision_changed')
    if contract_digest(execution.load_shot_contract(root)) != record['contract_sha256']:
        reasons.append('contract_changed')
    current_findings = None
    if not reasons:
        candidates = first_cut_candidates(root)
        current_clips = [row for row in candidates if row['status'] == 'candidate']
        if (_clip_media_bindings(current_clips, title_cards=record.get('title_cards', []))
                != _clip_media_bindings(record['clips'], placed=True)):
            reasons.append('candidate_bindings_changed')
        else:
            # Identical placed clips: each candidate row already resolved these
            # exact bytes' current findings and provenance like _clip_findings.
            current_findings = [{'shot_id': row['shot_id'], 'findings': row['findings']} for row in current_clips]
        if include_findings and [row for row in candidates if row['status'] == 'missing'] != record['missing']:
            reasons.append('missing_disposition_changed')
    if include_findings:
        if current_findings is None:
            current_findings = _clip_findings(root, record['clips'], current=True)
        if execution._digest(current_findings) != _findings_digest(root, record['clips'], current=False):
            reasons.append('findings_changed')
    if max(r['sequence'] for _, r in _cut_records(root)) > record['sequence']:
        reasons.append('superseded')
    return reasons


@_status_read
def first_cut_status(project_dir, cut):
    """Currency of the rendered record and its retained creator annotations."""
    reasons = _cut_reasons(Path(project_dir).resolve(), cut)
    return {'status': 'stale' if reasons else 'current', 'reasons': reasons}


@_status_read
def first_cut_media_status(project_dir, cut):
    """Media currency for producer completion; new notes do not change bytes.

    Annotation and acceptance currency remain separately governed by
    ``first_cut_status`` and ``first_cut_acceptance_status``.
    """
    reasons = _cut_reasons(Path(project_dir).resolve(), cut, include_findings=False)
    return {'status': 'stale' if reasons else 'current', 'reasons': reasons}


@_status_read
def first_cut_missing_status(project_dir, cut):
    """Current missing-shot disposition, independent of unchanged export bytes.

    A pending original can fail without changing the placed clips. Delivery
    callers refresh their disclosure from ``missing`` without reencoding media.
    The immutable cut record continues to describe the state at render time.
    """
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    path = _bound(root, {key: cut[key] for key in ('path', 'sha256')})
    if path.parent != root / _CUTS or path.name != cut['sha256'] + '.json':
        raise ValueError('first-cut missing status requires a canonical cut record')
    record = execution._read(path)
    missing = [row for row in first_cut_candidates(root) if row['status'] == 'missing']
    reasons = ['missing_disposition_changed'] if missing != record['missing'] else []
    return {'status': 'stale' if reasons else 'current', 'reasons': reasons, 'missing': missing}


def disclosure_clip_map(record):
    """Complete stable clip map a disclosure binds: 1-based number, identity,
    exact source output and master interval for every placed clip, in order.
    External completion clips also name their source project."""
    return [{'number': index, 'shot_id': clip['shot_id'], 'attempt_id': clip['attempt_id'],
             'output': clip['output'], 'master_start': clip['master_start'], 'master_end': clip['master_end'],
             **({'source_project': clip['source_project']} if 'source_project' in clip else {})}
            for index, clip in enumerate(record['clips'], 1)]


def _check_disclosure(root, cut, record, disclosure):
    """A disclosure sidecar must describe this exact cut record, export and complete clip map."""
    from lib import production_execution as execution
    sidecar = execution._read(_bound(root, disclosure))
    if sidecar.get('first_cut') != cut or sidecar.get('export') != record['export']:
        raise ValueError('disclosure does not describe this exact first cut and export')
    if sidecar.get('clips') != disclosure_clip_map(record):
        raise ValueError('disclosure clip map differs from the complete first-cut record')
    if sidecar.get('title_cards', []) != record.get('title_cards', []):
        raise ValueError('disclosure title cards differ from the first-cut record')
    return sidecar


@_status_read
def current_cut_disclosure(project_dir, cut):
    """Latest retained actual-export observations for this current cut, or None.

    Reuse the recorder's event binding and disclosure contract. Clip annotation
    changes require the recorder's matching findings digest; neither this lookup
    nor a retained observation establishes review coverage or certification.
    """
    from lib import production_execution as execution
    from lib.events import read_events
    root = Path(project_dir).resolve()
    currency = first_cut_status(root, cut)
    if currency['status'] != 'current' and currency['reasons'] != ['findings_changed']:
        return None
    record = execution._read(_bound(root, cut))
    for event in reversed(read_events(root)):
        if (event.get('event') != 'cut-disclosure-retained'
                or event.get('project_id') != record['project_id']
                or event.get('story_revision') != record['story_revision']
                or event.get('first_cut') != cut or event.get('export') != record['export']):
            continue
        try:
            binding = event['disclosure']
            path = _bound(root, binding)
            if (path.parent != root / 'production_first_cut_disclosures'
                    or path.name != binding['sha256'] + '.json'
                    or binding['path'] != str(path.relative_to(root))):
                return None
            sidecar = _check_disclosure(root, cut, record, binding)
            if (sidecar.get('kind') != 'first_cut_disclosure' or sidecar.get('version') != '1.0'
                    or not isinstance(sidecar.get('observations'), list)):
                return None
            findings_digest = sidecar.get('current_findings_sha256',
                _findings_digest(root, record['clips'], current=False))
            if findings_digest != _findings_digest(root, record['clips'], current=True):
                return None
            return {'disclosure': binding, 'observations': sidecar['observations']}
        except (OSError, ValueError, TypeError, KeyError):
            # Do not resurrect an older review when the latest matching retained
            # disclosure has changed or no longer describes current annotations.
            return None
    return None


@_write
def record_first_cut_acceptance(project_dir, cut, *, accepted_by, evidence, disclosure=None):
    """Creator accepts this exact export and disclosed findings; never certification.

    ``disclosure`` optionally binds the exact {path, sha256} actual-export review
    sidecar the creator saw; acceptance goes stale when that file changes."""
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    if not isinstance(accepted_by, str) or not accepted_by.strip():
        raise ValueError('first-cut acceptance requires accepted_by')
    with execution._lock(root):
        if set(cut) != {'path', 'sha256'} or _cut_reasons(root, cut):
            raise ValueError('first-cut acceptance requires the current first-cut record')
        record = execution._read((root / cut['path']).resolve())
        if record['export'] is None:
            raise ValueError('first-cut acceptance requires an actual export')
        _bound(root, evidence)
        if disclosure is not None:
            _check_disclosure(root, {'path': _rel(root, root / cut['path']), 'sha256': cut['sha256']},
                              record, disclosure)
        acceptance = {'version': '1.0', 'kind': 'creator_first_cut_acceptance', 'not_certification': True,
                      'project_id': record['project_id'], 'story_revision': record['story_revision'],
                      'first_cut': {'path': _rel(root, root / cut['path']), 'sha256': cut['sha256']},
                      'export': record['export'],
                      'findings_sha256': _findings_digest(root, record['clips'], current=False),
                      'sources': {clip['shot_id']: clip['output']['sha256'] for clip in record['clips']},
                      'status_at_acceptance': record['status'], 'accepted_by': accepted_by,
                      'evidence': {'path': _rel(root, root / evidence['path']), 'sha256': evidence['sha256']}}
        if disclosure is not None:
            acceptance['disclosure'] = {'path': _rel(root, root / disclosure['path']),
                                        'sha256': disclosure['sha256']}
        (root / _ACCEPTANCES).mkdir(exist_ok=True)
        import hashlib
        path = root / _ACCEPTANCES / (hashlib.sha256(json.dumps(acceptance, indent=2).encode()).hexdigest() + '.json')
        new = not path.exists()
        execution._write_new(path, acceptance)
        if new:
            from lib.events import emit_event
            emit_event(root, {'event': 'first-cut-accepted', 'project_id': record['project_id'],
                             'story_revision': record['story_revision'], 'first_cut': acceptance['first_cut'],
                             'export': record['export'], 'disclosure': acceptance.get('disclosure'),
                             'acceptance': {'path': _rel(root, path), 'sha256': file_sha256(path)}})
        return {'path': str(path), 'sha256': file_sha256(path)}


@_status_read
def first_cut_acceptance_status(project_dir, acceptance):
    """current only while acceptance, cut record, export, sources and findings are unchanged."""
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    path = (root / acceptance['path']).resolve()
    if path.parent != root / _ACCEPTANCES or not path.is_file() or file_sha256(path) != acceptance['sha256']:
        return {'status': 'stale', 'reasons': ['acceptance_changed']}
    record = execution._read(path)
    reasons = _cut_reasons(root, record['first_cut'])
    if 'disclosure' in record:
        bound = root / record['disclosure']['path']
        if not bound.is_file() or file_sha256(bound) != record['disclosure']['sha256']:
            reasons.append('disclosure_changed')
    return {'status': 'stale' if reasons else 'current', 'reasons': reasons}
