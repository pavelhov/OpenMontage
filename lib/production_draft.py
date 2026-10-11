"""Creator draft evidence: duplicate-bill exceptions and first-cut previews.

Both record creator decisions only, never a semantic pass, certification,
publication or generation authority.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.shot_contract import (UPSTREAM_PREDICATES, CRITICAL_PREDICATES, file_sha256,
                               selection_digest, provisional_audio_review, draft_audio_policy_digest, audio_review_disabled)

_KEYS = {'version', 'mode', 'project_id', 'story_revision', 'shot_id', 'attempt_id',
         'contract', 'output', 'outgoing_frame', 'failed_review', 'failed_predicate',
         'defect', 'accepted_by', 'acceptance_evidence', 'outgoing_review'}


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
    from lib.production_continuity import shot_planning_digest
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
    from lib.production_review_successors import resolve_selection_review
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


def _candidate_provenance(root, shot_id, attempt_id, output, revision, *, allow_historical=False):
    from lib.production_provenance import validate_attempt_provenance
    output = {**output, 'path': str((root / output['path']).resolve())}
    try:
        return validate_attempt_provenance(root, attempt_id, shot_id=shot_id,
                                          story_revision=revision, expected_output=output), False
    except ValueError as exc:
        continuity_error = any(message in str(exc) for message in (
            'upstream selection changed since this attempt', 'canonical reviewed source/reference/upstream changed',
            'stale source/reference/review/upstream bindings', 'current MCP planning semantics changed',
            'approved planning semantics changed'))
        if not allow_historical or not continuity_error:
            raise
        from lib.production_provenance import validate_creator_repair_source_provenance
        proof = validate_creator_repair_source_provenance(root, attempt_id, shot_id=shot_id,
            story_revision=revision, expected_output=output)
        if (proof.get('historical_upstream_changed') is False
                and not proof.get('historical_source_changed')):
            raise exc
        return proof, True


def _continuity_source_warning(proof):
    if proof.get('historical_upstream_changed') is False:
        return {'name': 'source_applicability_changed', 'status': 'unknown', 'severity': 'critical',
                'evidence': 'Retained clip matches its original approved shot and source bytes. Unrelated episode planning changed; current cut applicability needs review.'}
    return {'name': 'continuity_source_changed', 'status': 'unknown', 'severity': 'critical',
            'evidence': 'Retained clip used an earlier reviewed upstream selection. Current continuity needs review after the selected repair.'}


def _candidate(root, shot_id, attempt_id, output, revision, selected, *, historical_source=True):
    proof, historical_source = _candidate_provenance(root, shot_id, attempt_id, output, revision,
                                                    allow_historical=historical_source)
    path = Path(output['path'])
    path = path if path.is_absolute() else root / path
    timing = (proof or {}).get('derived_timing') if isinstance(proof, dict) else None
    duration = timing['duration_seconds'] if timing else _probe(path)['duration_seconds']
    review, findings = _current_findings(root, shot_id, attempt_id, output['sha256'], revision, selected)
    strict = not historical_source and review is not None and review['status'] == 'pass'
    # A strict pass may still disclose cosmetic findings; provisional stays provisional.
    status = review['status'] if review is not None else ('fail' if findings else 'unknown')
    if historical_source:
        status = 'unknown'
        findings = [*findings, _continuity_source_warning(proof)]
    return {'shot_id': shot_id, 'status': 'candidate', 'attempt_id': attempt_id,
            'output': {'path': _rel(root, path), 'sha256': output['sha256']},
            'duration_seconds': duration, 'strict_selected': strict,
            'review': status, 'findings': findings}


def _replaces_candidate(attempts, attempt_id, original_id):
    """Use retained repair ancestry, not filesystem dates, to supersede a baseline."""
    by_id = {row['attempt_id']: row for row in attempts}
    pending, seen = [attempt_id], set()
    while pending:
        aid = pending.pop()
        if aid in seen:
            continue
        seen.add(aid)
        row = by_id.get(aid, {})
        scope = row.get('scope', row.get('scope_snapshot', {}))
        replaced = scope.get('replaces_attempt_ids', [])
        if original_id in replaced:
            return True
        pending.extend(replaced)
    return False


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
    attempts = [a for a in execution._attempts(root) if a.get('story_revision') == revision]
    from lib.production_repair_batches import (creator_repair_candidates, suppressed_creator_repair_attempts,
                                                creator_repair_source_candidates, accepted_first_cut_candidates)
    creator_candidates = creator_repair_candidates(root)
    suppressed = suppressed_creator_repair_attempts(root)
    retained_sources = creator_repair_source_candidates(root)
    accepted_candidates = accepted_first_cut_candidates(root)
    rows, by_shot = [], {}
    for shot in contract['shots']:
        sid = shot['id']
        selection = selected.get(sid)
        selection_review = _selection_review(root, sid, selection, revision)
        row = None
        creator_attempt = creator_candidates.get(sid)
        if (creator_attempt is not None and selection_review is not None
                and selection['attempt_id'] not in suppressed
                and _replaces_candidate(attempts, selection['attempt_id'], creator_attempt)):
            creator_attempt = selection['attempt_id']
        if creator_attempt is not None:
            try:
                state = execution.load_attempt_result(root, creator_attempt)
                row = _candidate(root, sid, creator_attempt, state['output'], revision, selected)
            except (execution.ProductionGovernanceError, OSError, ValueError, KeyError,
                    TypeError, subprocess.SubprocessError):
                row = None
        accepted = accepted_candidates.get(sid)
        if (row is None and accepted is not None and selection_review is not None
                and selection['attempt_id'] not in suppressed
                and _replaces_candidate(attempts, selection['attempt_id'], accepted['attempt_id'])):
            try:
                # A current selected repair supersedes its accepted source. An
                # older strict selection must not roll back an accepted preview.
                row = _candidate(root, sid, selection['attempt_id'], selection['output'], revision, selected)
            except (execution.ProductionGovernanceError, OSError, ValueError, KeyError,
                    TypeError, subprocess.SubprocessError):
                row = None
        if row is None and accepted is not None:
            try:
                row = _candidate(root, sid, accepted['attempt_id'], accepted['output'], revision, selected,
                                 historical_source=True)
            except (execution.ProductionGovernanceError, OSError, ValueError, KeyError,
                    TypeError, subprocess.SubprocessError):
                row = None
        if row is None and selection_review is not None:
            try:
                row = _candidate(root, sid, selection['attempt_id'], selection['output'], revision, selected)
            except (execution.ProductionGovernanceError, OSError, ValueError, KeyError,
                    TypeError, subprocess.SubprocessError):
                row = None
        retained = retained_sources.get(sid)
        if row is None and retained is not None:
            try:
                # Historical source-only inclusion preserves playable old footage
                # during partial repairs and reports its stale continuity openly.
                row = _candidate(root, sid, retained['attempt_id'], retained['output'], revision,
                                 selected, historical_source=True)
            except (execution.ProductionGovernanceError, OSError, ValueError, KeyError,
                    TypeError, subprocess.SubprocessError):
                row = None
        own = sorted((a for a in attempts if a.get('shot_id') == sid and a['attempt_id'] not in suppressed),
                     key=lambda a: _attempt_order(root, a), reverse=True)
        states = []
        if row is None:
            for attempt in own:
                try:
                    state = execution.load_attempt_result(root, attempt['attempt_id'])
                except execution.ProductionGovernanceError:
                    state = {'status': 'uncertain'}
                states.append(state.get('status'))
                if state.get('status') == 'generated' and row is None:
                    try:
                        row = _candidate(root, sid, attempt['attempt_id'], state['output'], revision, selected)
                    except (execution.ProductionGovernanceError, OSError, ValueError, KeyError,
                            TypeError, subprocess.SubprocessError) as exc:
                        states[-1] = 'unplayable:' + str(exc)[:200]
        if row is None:
            # An upstream counts only when its own row is a playable candidate
            # (current bytes and provenance) and its review passes the canonical gate.
            upstream = [b['shot_id'] for b in shot.get('upstream', [])
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


def _compose_bindings(root, clips, export_profile, audio_path):
    """Exact inputs a compose intent binds: current clip bytes, profile and audio."""
    bindings = {'clips': [{'shot_id': row['shot_id'], 'attempt_id': row['attempt_id'], 'output': row['output']}
                          for row in clips],
                'export_profile': export_profile, 'audio': None}
    for row in clips:
        source = root / row['output']['path']
        if not source.is_file() or file_sha256(source) != row['output']['sha256']:
            raise ValueError('first-cut candidate bytes changed before compose: ' + row['shot_id'])
    if audio_path:
        audio = Path(audio_path).resolve()
        bindings['audio'] = {'path': _rel(root, audio), 'sha256': file_sha256(audio)}
    return bindings


def _clip_media_bindings(clips, *, placed=False):
    """Ordered placement identity and timing, excluding mutable review notes."""
    cursor, bindings = 0.0, []
    for clip in clips:
        start = clip['master_start'] if placed else cursor
        end = clip['master_end'] if placed else round(cursor + clip['duration_seconds'], 3)
        bindings.append({key: clip[key] for key in ('shot_id', 'attempt_id', 'output')} |
                        {'master_start': round(start, 3), 'master_end': end})
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


def compose_first_cut(project_dir, *, audio_path=None, compose=None, export_profile=None, operation_id=None):
    """Render a versioned preview from current candidates and record it immutably.

    ``export_profile`` (explicit media profile name) is validated before any
    render and technically checked after it; without one the first clip's
    geometry is retained. ``operation_id`` writes a compose intent inside the
    render directory before rendering, so a crashed operation resumes: an
    existing record is returned, a complete local render is recorded without
    re-rendering. A confirmed technical mismatch or incomplete MP4 preserves
    the original beside its replacement; probe uncertainty preserves it for retry.
    Changed clip/profile/audio bindings reject the stale intent."""
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
    shots = {shot['id']: shot for shot in contract['shots']}
    rows = first_cut_candidates(root)
    clips = [row for row in rows if row['status'] == 'candidate']
    missing = [row for row in rows if row['status'] == 'missing']
    dialogue = any(shots[row['shot_id']].get('dialogue') for row in clips)
    if audio_path and dialogue:
        raise ValueError('external audio cannot replace approved native dialogue in a first cut')
    bindings = _compose_bindings(root, clips, export_profile, audio_path) if operation_id is not None else None
    bindings_sha256 = execution._digest(bindings) if operation_id is not None else None
    recorded = set()
    if operation_id is not None:
        # Same operation replays only with identical clip/profile/audio bindings.
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
            if (record['status'] != 'render_failed' and _export_intact(root, record)
                    and _clip_media_bindings(record['clips'], placed=True) == _clip_media_bindings(clips)):
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
    cursor, placed = 0.0, []
    for row in clips:
        start, cursor = cursor, round(cursor + row['duration_seconds'], 3)
        placed.append({'shot_id': row['shot_id'], 'attempt_id': row['attempt_id'], 'output': row['output'],
                       'master_start': round(start, 3), 'master_end': cursor,
                       'review': row['review'], 'findings': row['findings']})
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
            technical, problem = _verify_render(output, profile, cursor, len(placed))
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
            metadata = {'preserve_source_audio': not audio_path}
            if profile is None:
                metadata['compose_target'] = {'width': first['width'], 'height': first['height'], 'fit': 'pad'}
            edit = {'version': '1.0', 'render_runtime': 'ffmpeg',
                    'cuts': [{'id': clip['shot_id'], 'source': str(root / clip['output']['path']), 'in_seconds': 0,
                              'out_seconds': clip['master_end'] - clip['master_start']} for clip in placed],
                    'metadata': metadata}
            inputs = {'operation': 'compose', 'edit_decisions': edit, 'output_path': str(output)}
            if profile is not None:
                inputs['profile'] = profile.name
            if audio_path:
                inputs['audio_path'] = str(audio_path)
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
                technical, problem = _verify_render(output, profile, cursor, len(placed), verify_duration=False)
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
    # Audio is required only by included dialogue (native clip bytes must carry it;
    # padded export silence proves nothing) or an explicit external track.
    # Approved silent coverage completes without any audio stream. Future visual-only
    # projects retain availability facts below but do not gate completion on them.
    audio_required = bool(dialogue or audio_path)
    audio_missing = [row['shot_id'] for row in clips if shots[row['shot_id']].get('dialogue')
                     and not _probe(root / row['output']['path'])['has_audio']]
    visual_only = audio_review_disabled(root)
    complete = (export is not None and not missing
                and (visual_only or (not audio_missing and (technical['has_audio'] or not audio_path)))
                and abs(technical['duration_seconds'] - cursor) <= 0.05 + 0.04 * len(placed))
    record = {'version': '1.0', 'kind': 'first_cut', 'sequence': sequence, 'project_id': marker['project_id'],
              'story_revision': marker['story_revision'], 'contract_sha256': contract_digest(contract),
              'status': 'complete' if complete else ('render_failed' if error else 'incomplete'),
              'export': export, 'technical': technical, 'clips': placed, 'missing': missing,
              'audio': 'external_replacement' if audio_path else 'native_source',
              'audio_required': audio_required, 'audio_missing': audio_missing,
              'render_error': error, 'render_log': log}
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


def deliver_standalone(project_dir, *, export_profile=None, operation_id=None, audio_path=None, compose=None):
    """Engine-only saved draft cut from current candidates; no Studio, Drive or account.

    Idempotent per ``operation_id`` (default derives from the current clip,
    profile and audio bindings). Reports honest per-clip review/findings and
    missing shots; never certification, publication or provider work."""
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    if operation_id is None:
        if export_profile is not None:
            from lib.media_profiles import get_profile
            get_profile(export_profile)
        clips = [row for row in first_cut_candidates(root) if row['status'] == 'candidate']
        operation_id = 'deliver-' + execution._digest(_compose_bindings(root, clips, export_profile, audio_path))[:24]
    cut = compose_first_cut(root, audio_path=audio_path, compose=compose,
                            export_profile=export_profile, operation_id=operation_id)
    binding = {'path': _rel(root, cut['path']), 'sha256': cut['sha256']}
    status = first_cut_media_status(root, binding)
    return {'status': cut['status'], 'currency': status,
            'annotation_currency': first_cut_status(root, binding),
            'first_cut': {'path': _rel(root, cut['path']), 'sha256': cut['sha256']},
            'operation_id': operation_id, 'export': cut['export'],
            'export_path': str(root / cut['export']['path']) if cut['export'] else None,
            'export_profile': export_profile, 'technical': cut['technical'],
            'clips': [{**row, 'review': _current_review_status(root, clip, current['findings']),
                       'review_at_render': clip['review'], 'findings': current['findings']}
                      for row, clip, current in zip(disclosure_clip_map(cut), cut['clips'],
                                                    _clip_findings(root, cut['clips'], current=True))],
            'missing': cut['missing'], 'audio': cut['audio'], 'render_error': cut['render_error'],
            'not_certification': True, 'posted': False, 'provider_calls': 0}


def _current_review_status(root, clip, findings):
    """Current review status of a placed clip, resolved like ``_candidate``."""
    from lib import production_execution as execution
    revision = execution._read(root / 'project.json')['story_revision']
    review, _ = _current_findings(root, clip['shot_id'], clip['attempt_id'], clip['output']['sha256'],
                                  revision, execution.load_selected_attempts(root))
    try:
        _, historical = _candidate_provenance(root, clip['shot_id'], clip['attempt_id'], clip['output'],
                                              revision, allow_historical=True)
    except (ValueError, OSError, KeyError, TypeError):
        historical = False
    if historical:
        return 'unknown'
    return review['status'] if review is not None else ('fail' if findings else 'unknown')


def _clip_findings(root, clips, *, current):
    """Per-clip findings: retained at render time, or projected from current notes."""
    from lib import production_execution as execution
    if current:
        revision = execution._read(root / 'project.json')['story_revision']
        selected = execution.load_selected_attempts(root)
    findings = []
    for clip in clips:
        values = clip['findings']
        if current:
            values = _current_findings(root, clip['shot_id'], clip['attempt_id'], clip['output']['sha256'],
                                       revision, selected)[1]
            try:
                proof, historical = _candidate_provenance(root, clip['shot_id'], clip['attempt_id'],
                    clip['output'], revision, allow_historical=True)
            except (ValueError, OSError, KeyError, TypeError):
                historical = False
            if historical:
                values = [*values, _continuity_source_warning(proof)]
        findings.append({'shot_id': clip['shot_id'], 'findings': values})
    return findings


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
    if record['export'] is None or not (root / record['export']['path']).is_file() \
            or file_sha256(root / record['export']['path']) != record['export']['sha256']:
        reasons.append('export_changed')
    for clip in record['clips']:
        source = root / clip['output']['path']
        if not source.is_file() or file_sha256(source) != clip['output']['sha256']:
            reasons.append('source_changed:' + clip['shot_id'])
    if execution._read(root / 'project.json')['story_revision'] != record['story_revision']:
        reasons.append('story_revision_changed')
    if contract_digest(execution.load_shot_contract(root)) != record['contract_sha256']:
        reasons.append('contract_changed')
    if not reasons:
        current_clips = [row for row in first_cut_candidates(root) if row['status'] == 'candidate']
        if _clip_media_bindings(current_clips) != _clip_media_bindings(record['clips'], placed=True):
            reasons.append('candidate_bindings_changed')
    if (include_findings and _findings_digest(root, record['clips'], current=True)
            != _findings_digest(root, record['clips'], current=False)):
        reasons.append('findings_changed')
    if max(r['sequence'] for _, r in _cut_records(root)) > record['sequence']:
        reasons.append('superseded')
    return reasons


def first_cut_status(project_dir, cut):
    """Currency of the rendered record and its retained creator annotations."""
    reasons = _cut_reasons(Path(project_dir).resolve(), cut)
    return {'status': 'stale' if reasons else 'current', 'reasons': reasons}


def first_cut_media_status(project_dir, cut):
    """Media currency for producer completion; new notes do not change bytes.

    Annotation and acceptance currency remain separately governed by
    ``first_cut_status`` and ``first_cut_acceptance_status``.
    """
    reasons = _cut_reasons(Path(project_dir).resolve(), cut, include_findings=False)
    return {'status': 'stale' if reasons else 'current', 'reasons': reasons}


def disclosure_clip_map(record):
    """Complete stable clip map a disclosure binds: 1-based number, identity,
    exact source output and master interval for every placed clip, in order."""
    return [{'number': index, 'shot_id': clip['shot_id'], 'attempt_id': clip['attempt_id'],
             'output': clip['output'], 'master_start': clip['master_start'], 'master_end': clip['master_end']}
            for index, clip in enumerate(record['clips'], 1)]


def _check_disclosure(root, cut, record, disclosure):
    """A disclosure sidecar must describe this exact cut record, export and complete clip map."""
    from lib import production_execution as execution
    sidecar = execution._read(_bound(root, disclosure))
    if sidecar.get('first_cut') != cut or sidecar.get('export') != record['export']:
        raise ValueError('disclosure does not describe this exact first cut and export')
    if sidecar.get('clips') != disclosure_clip_map(record):
        raise ValueError('disclosure clip map differs from the complete first-cut record')


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
