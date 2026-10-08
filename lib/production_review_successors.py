"""Append verified listening evidence without rewriting generation authority.

The selected record and its historical review remain the source/preparation
subject. Only eligibility readers resolve this separately retained evidence.
Judgments are authored by named reviewers; hashes prove binding, not listening.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.shot_contract import file_sha256, review_digest, selection_digest, UPSTREAM_PREDICATES


def _require(condition, message):
    if not condition:
        raise ValueError('review successor: ' + message)


def _bound(root, binding, *, return_bytes=False):
    _require(isinstance(binding, dict) and set(binding) == {'path', 'sha256'}, 'exact evidence path/hash required')
    _require(isinstance(binding['path'], str) and bool(binding['path'].strip()), 'evidence path required')
    path = (root / binding['path']).resolve()
    _require(path.is_relative_to(root), 'evidence missing, changed or outside project')
    try:
        raw = path.read_bytes() if return_bytes else None
        digest = hashlib.sha256(raw).hexdigest() if return_bytes else file_sha256(path)
    except OSError as exc:
        raise ValueError('review successor: evidence missing or unreadable') from exc
    _require(digest == binding['sha256'], 'evidence missing, changed or outside project')
    return raw if return_bytes else path


def _context(root, shot_id, selection):
    from lib import production_execution as execution
    from lib.production_continuity import shot_planning_digest
    marker = execution._read(root / 'project.json')
    contract = execution.load_shot_contract(root)
    _require(marker.get('governance', {}).get('mode') == 'strict', 'strict project required')
    _require((contract.get('project_id'), contract.get('story_revision')) ==
             (marker.get('project_id'), marker.get('story_revision')), 'project/story differs')
    _require(isinstance(selection, dict), 'selected original required')
    for key in ('output', 'outgoing_frame'):
        _bound(root, selection.get(key))
    _require(selection.get('review', {}).get('subject_sha256') == selection_digest(selection)
             and selection['review'].get('story_revision') == marker['story_revision'], 'selection review subject differs')
    return {'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
            'shot_id': shot_id, 'selection_sha256': review_digest(selection),
            'subject_sha256': selection_digest(selection),
            'planning_sha256': shot_planning_digest(contract, shot_id)}


def _listening(root, evidence, context, selection, review):
    value = json.loads(_bound(root, evidence, return_bytes=True))
    expected = {k:context[k] for k in ('project_id','story_revision','shot_id','subject_sha256')}
    expected.update(version='1.0', kind='synchronized_audio_review', attempt_id=selection['attempt_id'],
                    output_sha256=selection['output']['sha256'], reviewer=review['reviewer'],
                    coverage='complete_clip', synchronized=True, listened=True, status='pass')
    _require(review_digest(value) == review_digest(expected), 'complete synchronized listening attestation differs from exact selected clip/reviewer')


def _transition(old, new):
    schema = load_schema('shot_contract')
    validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/review'})
    _require(validator.is_valid(old) and validator.is_valid(new), 'valid old/new named reviews required')
    _require(old['status'] == 'provisional', 'original provisional review required')
    _require(old['review_id'] != new['review_id'], 'new review identity required')
    _require(old['story_revision'] == new['story_revision'] and old['subject_sha256'] == new['subject_sha256'], 'review subject/story differs')
    before = {p['name']: p for p in old['predicates']}
    after = {p['name']: p for p in new['predicates']}
    _require(len(before) == len(old['predicates']) and len(after) == len(new['predicates'])
             and set(before) == set(after) and UPSTREAM_PREDICATES.issubset(before), 'complete unique unchanged predicate set required')
    _require(before['speaker_source']['status'] == 'unknown' and after['speaker_source']['status'] == 'pass'
             and bool(after['speaker_source']['evidence'].strip())
             and before['speaker_source']['evidence'] != after['speaker_source']['evidence']
             and before['speaker_source'].get('severity') == after['speaker_source'].get('severity'), 'only substantiated audio unknown-to-pass permitted')
    _require(all(before[name] == after[name] for name in before if name != 'speaker_source'), 'visual facts and failed predicates must remain unchanged')
    accepted = 'creator_draft_exception' in old
    from lib.shot_contract import CRITICAL_PREDICATES
    if not accepted:
        _require(all(p['status'] == 'pass' for name,p in before.items()
                     if name != 'speaker_source' and (name in CRITICAL_PREDICATES or p.get('severity','critical') == 'critical')),
                 'critical visual failure or uncertainty cannot advance to pass')
    _require(new['status'] == ('provisional' if accepted else 'pass'), 'accepted critical defect remains provisional')
    allowed = {'review_id', 'reviewer', 'status', 'predicates', 'draft_policy_sha256'}
    _require({k:v for k,v in old.items() if k not in allowed} ==
             {k:v for k,v in new.items() if k not in allowed}, 'review authority differs')
    if accepted:
        _require(new.get('draft_policy_sha256') == old.get('draft_policy_sha256'), 'draft authority must remain bound')
    else:
        _require('draft_policy_sha256' not in new, 'passing successor cannot retain draft policy')


def resolve_selection_review(project_dir, shot_id, selection, *, expected_review_sha256=None):
    """Resolve one unique audio advancement; never change the selected record.

    No successor for the current original means that original review. History
    for a superseded original or subject remains retained and does not apply.
    An embedded historical hash must still equal the current original.
    """
    root = Path(project_dir).resolve()
    original = selection.get('review') if isinstance(selection, dict) else None
    _require(isinstance(original, dict), 'original review missing')
    predecessor_sha256 = review_digest(original)
    subject_sha256 = selection_digest(selection)
    if expected_review_sha256 is not None:
        _require(predecessor_sha256 == expected_review_sha256, 'historical original review differs')
    directory = root / 'production_review_successors'
    _require(not directory.is_symlink(), 'history directory cannot be a symlink')
    matches = []
    for path in directory.glob('*.json'):
        raw = path.read_bytes()
        record = json.loads(raw)
        _require(isinstance(record, dict), 'malformed history')
        if (record.get('shot_id') == shot_id and record.get('attempt_id') == selection.get('attempt_id')
                and record.get('predecessor_sha256') == predecessor_sha256
                and record.get('subject_sha256') == subject_sha256):
            matches.append((path, record, hashlib.sha256(raw).hexdigest()))
    if not matches:
        return copy.deepcopy(original)
    _require(len(matches) == 1, 'conflicting successors')
    path, record, record_sha256 = matches[0]
    _require(path.parent.resolve() == directory.resolve() and not path.is_symlink()
             and path.name == record_sha256 + '.json', 'content-addressed immutable history required')
    context = _context(root, shot_id, selection)
    _require(set(record) == set(context) | {'version','attempt_id','predecessor_sha256','review','evidence'}, 'unsupported record fields')
    _require(record['version'] == '1.0' and all(record[k] == v for k,v in context.items())
             and record['predecessor_sha256'] == predecessor_sha256, 'stale or forged chain binding')
    _listening(root, record['evidence'], context, selection, record['review'])
    _transition(original, record['review'])
    return copy.deepcopy(record['review'])


def record_review_successor(project_dir, shot_id, review, *, evidence):
    """Append reviewer-authored audio evidence under the canonical project lock.

    ``evidence`` is exactly ``{path, sha256}``, pointing to JSON with version
    1.0, kind synchronized_audio_review, project_id/story_revision/shot_id,
    subject_sha256/attempt_id/output_sha256, the review's named reviewer,
    coverage complete_clip, synchronized/listened true and status pass.
    This authored listening attestation grants no generation or final status.
    """
    from lib import production_execution as execution
    from lib.production_provenance import validate_attempt_provenance
    from lib.shot_contract import validate_shot_contract
    root = Path(project_dir).resolve()
    with execution._lock(root):
        selected = execution.load_selected_attempts(root)
        selection = selected.get(shot_id)
        context = _context(root, shot_id, selection)
        original = selection['review']
        _listening(root, evidence, context, selection, review)
        _transition(original, review)
        # Validate the actual original and current planning, not caller claims.
        validate_attempt_provenance(root, selection['attempt_id'], shot_id=shot_id,
            story_revision=context['story_revision'], expected_output=selection['output'])
        readiness = validate_shot_contract(execution.load_shot_contract(root), project_dir=root,
            shot_id=shot_id, selected_upstream=selected)
        _require(readiness['eligible'], 'current planning is ineligible: ' + '; '.join(readiness['errors']))
        from lib.production_draft import accepted_draft_predicate
        from lib.shot_contract import provisional_audio_review
        _require(provisional_audio_review(original, root) or accepted_draft_predicate(selection, root, shot_id=shot_id) is not None,
                 'valid original draft authority required')
        record = {**context, 'version':'1.0', 'attempt_id': selection['attempt_id'],
                  'predecessor_sha256':review_digest(original), 'review':copy.deepcopy(review), 'evidence':copy.deepcopy(evidence)}
        raw = json.dumps(record, indent=2).encode()
        digest = hashlib.sha256(raw).hexdigest()
        directory = root / 'production_review_successors'
        _require(not directory.is_symlink(), 'history directory cannot be a symlink')
        directory.mkdir(exist_ok=True)
        path = directory / (digest + '.json')
        current = resolve_selection_review(root, shot_id, selection)
        if current != original:
            _require(path.is_file() and path.read_bytes() == raw, 'conflicting successor already retained')
        else:
            execution._write_new(path, record)
        _require(resolve_selection_review(root, shot_id, selection) == review, 'appended record failed replay')
        return {'path':str(path), 'sha256':digest, 'review_sha256':review_digest(review),
                'subject_sha256':context['subject_sha256'], 'review':copy.deepcopy(review)}
