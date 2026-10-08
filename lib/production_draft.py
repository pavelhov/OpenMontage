"""Creator acceptance of one duplicate-bill defect in existing footage, draft only.

This records an exception, never a semantic pass or generation authority.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.shot_contract import (UPSTREAM_PREDICATES, CRITICAL_PREDICATES, file_sha256,
                               selection_digest, provisional_audio_review, draft_audio_policy_digest)

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
