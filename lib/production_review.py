"""Current final certification; objective bindings, never semantic inference.

A transport receipt or automated spot check is not a full AV review. Reviewers
supply accountable judgments. This module checks their completeness and binds
them to current story/selection/master bytes at both final selection and the
checkpoint boundary. Legacy v1 diagnostics remain readable, never certifiable.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

from lib.shot_contract import (
    CRITICAL_PREDICATES, PROJECT_PREDICATES, UPSTREAM_PREDICATES,
    contract_digest, file_sha256, review_digest, selection_digest,
    validate_shot_contract,
)
from schemas.artifacts import load_schema

CURRENT_VERSION = '2.0'
DIMENSIONS = ('transport', 'technical', 'visual', 'audio', 'story')
# Accommodate container timestamp rounding, not an omitted shot/tail.
TIMING_TOLERANCE = 0.05


class FinalCertificationError(ValueError):
    """The current master lacks matching complete passing review evidence."""


def probe_master(path: Path) -> dict[str, Any]:
    """Read actual container duration locally; no media generation or network."""
    process = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'json', str(path)], capture_output=True, text=True,
        check=True, timeout=30,
    )
    return {'duration_seconds': float(json.loads(process.stdout)['format']['duration'])}


def _resolve(root: Path, path: str) -> Path:
    target = Path(path)
    return (target if target.is_absolute() else root / target).resolve()


def _load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'{path.name} must be an object')
    return value


def validate_final_review(
    project_dir: str | Path, review: dict | None, *,
    probe: Callable[[Path], dict[str, Any]] | None = None,
    output_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Validate current eligibility using canonical project artifacts on disk.

    ``probe`` is an offline-test seam; production defaults to ffprobe. Every
    canonical caller uses this same gate. A v2 release_status='draft' records
    review progress without asserting final. This function performs no writes.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def result():
        return {'eligible': not errors, 'release_status': 'draft' if errors else 'final',
                'errors': errors, 'warnings': warnings}

    if not isinstance(review, dict) or review.get('version') != CURRENT_VERSION:
        errors.append('final_review: current v2 full AV review required; v1 is legacy/draft evidence')
        return result()
    try:
        json.dumps(review, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f'final_review: invalid JSON value ({exc})')
        return result()
    current_schema = load_schema('final_review')['oneOf'][1]
    for error in Draft202012Validator(current_schema, format_checker=FormatChecker()).iter_errors(review):
        field = '.'.join(map(str, error.absolute_path)) or '$'
        errors.append(f'final_review.{field}: {error.message}')
    if errors:
        return result()
    if review['release_status'] != 'final':
        errors.append('release_status: draft has not been submitted for final certification')

    root = Path(project_dir).resolve()
    # Shared loaders reject conflicting canonical/legacy sidecars and honor
    # reconciled uncertain jobs exactly as the common dispatch boundary does.
    from lib.production_execution import load_shot_contract, load_selected_attempts
    try:
        contract = load_shot_contract(root)
        selected = load_selected_attempts(root)
        marker = _load_object(root / 'project.json')
        if not isinstance(contract, dict):
            raise ValueError('shot_contract must be an object')
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f'project: missing/invalid current production evidence ({exc})')
        return result()
    if review['project_id'] != marker.get('project_id') or review['project_id'] != contract.get('project_id'):
        errors.append('project_id: review, marker and contract must identify the same project')
    governance = marker.get('governance')
    if not isinstance(governance, dict) or governance.get('mode') != 'strict' or governance.get('version') != '1.0':
        errors.append('project.governance: strict enrollment required for current certification')
    revision = contract.get('story_revision')
    if review['story_revision'] != revision:
        errors.append('story_revision: review does not match current contract')
    if not marker.get('story_revision') or marker['story_revision'] != revision:
        errors.append('story_revision: current project marker and contract conflict')
    try:
        if review['contract_sha256'] != contract_digest(contract):
            errors.append('contract_sha256: reviewed plan changed')
    except (ValueError, TypeError, AttributeError) as exc:
        errors.append(f'contract: malformed planning data ({exc})')
        return result()

    def check_file(record, label):
        try:
            path = _resolve(root, record['path'])
            if file_sha256(path) != record['sha256']:
                errors.append(f'{label}: bytes changed or hash mismatch')
        except (OSError, KeyError, TypeError, ValueError) as exc:
            errors.append(f'{label}: missing/unreadable media ({exc})')

    master = _resolve(root, review['output_path'])
    check_file({'path': str(master), 'sha256': review['output_sha256']}, 'master')
    if output_paths is not None:
        # A single review cannot certify an unreviewed alternate deliverable.
        if {_resolve(root, path) for path in output_paths} != {master}:
            errors.append('render_report.outputs: exact reviewed master required; review alternate outputs separately')
    try:
        duration = float((probe or probe_master)(master)['duration_seconds'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('duration must be finite and positive')
        if abs(duration - review['duration_seconds']) > TIMING_TOLERANCE:
            errors.append('duration_seconds: review does not cover actual master duration')
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        errors.append(f'master duration: technical probe unavailable/invalid ({exc})')
        duration = review['duration_seconds']
    for dimension in DIMENSIONS:
        if review['dimensions'][dimension]['status'] != 'pass':
            errors.append(f'{dimension}: {review["dimensions"][dimension]["status"]}')
    av = review['av_review']
    if av['status'] != 'pass' or av['mode'] != 'synchronized_av' or not av['watched_full'] or not av['listened_full']:
        errors.append('av_review: complete synchronized viewing AND listening must pass')
    if abs(av['start_seconds']) > TIMING_TOLERANCE or abs(av['end_seconds'] - duration) > TIMING_TOLERANCE:
        errors.append('av_review: playback evidence must cover the full master from start through ending')

    def predicates(items, required, label):
        seen = set()
        for item in items:
            name = item['name']
            if name in seen:
                errors.append(f'{label}.{name}: duplicate/conflicting predicate')
            seen.add(name)
            critical = name in CRITICAL_PREDICATES or item.get('severity', 'critical') == 'critical'
            if name in CRITICAL_PREDICATES and item.get('severity') == 'cosmetic':
                errors.append(f'{label}.{name}: critical predicate cannot be cosmetic')
            if item['status'] != 'pass':
                (errors if critical else warnings).append(f'{label}.{name}: {item["status"]}: {item["evidence"]}')
        for name in sorted(required - seen):
            errors.append(f'{label}.{name}: missing critical predicate')

    predicates(review['predicates'], PROJECT_PREDICATES, 'final_review.predicates')
    shots = contract.get('shots')
    if not isinstance(shots, list) or not shots or any(not isinstance(s, dict) or not isinstance(s.get('id'), str) for s in shots):
        errors.append('shot_contract.shots: missing/invalid scene coverage')
        return result()
    expected = [shot['id'] for shot in shots]
    covered = [scene['scene_id'] for scene in review['scenes']]
    if len(set(expected)) != len(expected) or covered != expected or set(selected) != set(expected):
        errors.append('scenes: review and current selections must exactly cover every planned scene in order')
    previous_end = 0.0
    shot_schema = load_schema('shot_contract')
    selected_review_schema = {'$defs': shot_schema['$defs'], '$ref': '#/$defs/review'}
    for scene in review['scenes']:
        shot_id = scene['scene_id']
        if (not all(math.isfinite(scene[key]) for key in ('start_seconds', 'end_seconds'))
                or scene['end_seconds'] <= scene['start_seconds']
                or abs(scene['start_seconds'] - previous_end) > TIMING_TOLERANCE):
            errors.append(f'scenes.{shot_id}: invalid, overlapping or incomplete timeline coverage')
        previous_end = scene['end_seconds']
        selection = selected.get(shot_id)
        if not isinstance(selection, dict):
            errors.append(f'selected.{shot_id}: missing selection')
            continue
        attempt_id = selection.get('attempt_id')
        if not isinstance(attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', attempt_id):
            errors.append(f'selected.{shot_id}: invalid attempt identity')
            continue
        for key in ('output', 'outgoing_frame'):
            check_file(selection.get(key), f'selected.{shot_id}.{key}')
        selected_review = selection.get('review')
        try:
            malformed = list(Draft202012Validator(selected_review_schema).iter_errors(selected_review))
            if malformed:
                raise ValueError(malformed[0].message)
            if (selected_review['status'] != 'pass' or selected_review['story_revision'] != revision
                    or selected_review['subject_sha256'] != selection_digest(selection)):
                errors.append(f'selected.{shot_id}.review: failed/stale selection review')
            if not selected_review['reviewer'].strip() or any(not item['evidence'].strip() for item in selected_review['predicates']):
                errors.append(f'selected.{shot_id}.review: named reviewer and concrete evidence required')
            predicates(selected_review['predicates'], UPSTREAM_PREDICATES, f'selected.{shot_id}.review')
            if (scene['attempt_id'] != attempt_id or scene['output_sha256'] != selection['output']['sha256']
                    or scene['selection_sha256'] != selection_digest(selection)
                    or scene['review_sha256'] != review_digest(selected_review)):
                errors.append(f'scenes.{shot_id}: selected attempt/output/review changed')
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f'selected.{shot_id}: malformed review or binding ({exc})')
        try:
            from lib.production_provenance import validate_attempt_provenance
            validate_attempt_provenance(
                root, attempt_id, shot_id=shot_id, story_revision=revision,
                expected_output=selection.get('output'),
            )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f'selected.{shot_id}: attempt provenance unavailable ({exc})')
        readiness = validate_shot_contract(contract, project_dir=root, shot_id=shot_id,
                                           story_revision=revision, selected_upstream=selected)
        errors.extend(readiness['errors'])
        warnings.extend(readiness['warnings'])
    if not math.isfinite(previous_end) or abs(previous_end - duration) > TIMING_TOLERANCE:
        errors.append('scenes: coverage must reach actual master ending')
    # Recheck after probing/read validation to reject a concurrently edited master.
    check_file({'path': str(master), 'sha256': review['output_sha256']}, 'master')
    return result()


def assert_final_eligible(project_dir: str | Path, review: dict | None, **kwargs) -> dict:
    """Shared gate for canonical selection, compose and publish checkpoints."""
    result = validate_final_review(project_dir, review, **kwargs)
    if not result['eligible']:
        raise FinalCertificationError('FINAL CERTIFICATION: ' + '; '.join(result['errors']))
    return result


def certify_final(project_dir: str | Path, review: dict, **kwargs) -> Path:
    """Select a reviewed final via the same gate used by checkpoint advancement.

    Writes only review evidence. Does not publish, schedule, regenerate, or grant
    publication authorization. Replaced certifications are retained in history.
    """
    assert_final_eligible(project_dir, review, **kwargs)
    root = Path(project_dir)
    path = root / 'artifacts/final_review.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        history = root / 'history' / f'final_review_{file_sha256(path)}.json'
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists():
            history.write_bytes(path.read_bytes())
    fd, temporary = tempfile.mkstemp(prefix='.final_review-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(review, handle, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
