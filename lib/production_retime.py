"""Pure timeline transformation; this module grants no production authority.

Only declared shot-local dialogue/keyframe times and global scene/section
boundaries are transformed. Cues with unspecified local/global semantics are
retained verbatim; their suitability still needs the caller's normal review.
"""
from __future__ import annotations

import copy
import math
from numbers import Real


class RetimingError(ValueError):
    """The supplied planning cannot be transformed unambiguously."""


def _number(value, label, *, positive=False):
    try:
        finite = not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise RetimingError(f'{label} must be a finite real number')
    if value < 0 or (positive and value == 0):
        raise RetimingError(f'{label} must be {"positive" if positive else "nonnegative"}')
    return value if type(value) in (int, float) else float(value)


def _index(rows, label):
    if not isinstance(rows, list) or not rows:
        raise RetimingError(f'{label} must be a nonempty list')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise RetimingError(f'{label} requires named objects')
        if row['id'] in result:
            raise RetimingError(f'{label} has an ambiguous duplicate ID')
        result[row['id']] = row
    return result


def _interval(row, label):
    start = _number(row.get('start_seconds'), f'{label} start')
    end = _number(row.get('end_seconds'), f'{label} end', positive=True)
    if end <= start:
        raise RetimingError(f'{label} has a nonpositive interval')
    return start, end


def _ripple(rows, deltas, label):
    shift = 0
    previous_start = -1
    previous_retimed_start = -1
    for row in rows:
        start, end = _interval(row, label)
        if start < previous_start:
            raise RetimingError(f'{label} order contradicts its timeline')
        previous_start = start
        delta = deltas.get(row['id'], 0)
        if shift or delta:
            row['start_seconds'] = round(start + shift, 6)
            row['end_seconds'] = round(end + shift + delta, 6)
            _interval(row, f'retimed {label}')
        if row['start_seconds'] < previous_retimed_start:
            raise RetimingError(f'retimed {label} would reverse timeline order')
        previous_retimed_start = row['start_seconds']
        shift += delta


def retime_planning(contract_snapshot, scene_snapshot, script_snapshot, duration_by_shot):
    """Deep-copy and ripple named shot duration changes through retained planning.

    This accepts planning data, not policy evidence. The caller must establish
    retained approval authority, permitted flex and subsequent preparation/fit
    review. Baseline gaps, overlaps and the script's trailing allowance survive
    as fixed offsets. Time arithmetic follows the existing six-decimal rule.
    """
    if not all(isinstance(value, dict) for value in
               (contract_snapshot, scene_snapshot, script_snapshot, duration_by_shot)):
        raise RetimingError('planning snapshots and duration map must be objects')
    contract, scene_plan, script = copy.deepcopy((contract_snapshot, scene_snapshot, script_snapshot))
    shots = _index(contract.get('shots'), 'shots')
    scenes = _index(scene_plan.get('scenes'), 'scenes')
    sections = _index(script.get('sections'), 'script sections')
    assets = _index(contract.get('assets'), 'assets')
    if set(duration_by_shot) - set(shots):
        raise RetimingError('duration map names an unknown shot')
    if set(shots) - set(scenes):
        raise RetimingError('shot has no exact scene mapping')
    if [sid for sid in scenes if sid in shots] != list(shots):
        raise RetimingError('shot and scene order disagree')

    factors, deltas = {}, {}
    for shot_id, shot in shots.items():
        old = _number(shot.get('duration_seconds'), f'{shot_id} baseline duration', positive=True)
        new = _number(duration_by_shot.get(shot_id, old), f'{shot_id} duration', positive=True)
        factors[shot_id] = new / old
        deltas[shot_id] = new - old
        if new != old:
            shot['duration_seconds'] = new
        dialogue = shot.get('dialogue', [])
        if not isinstance(dialogue, list):
            raise RetimingError('shot dialogue must be a list')
        for line in dialogue:
            if not isinstance(line, dict):
                raise RetimingError('dialogue line must be an object')
            start, end = _interval(line, 'shot-local dialogue')
            if end > old:
                raise RetimingError('baseline dialogue exceeds shot duration')
            if new != old:
                line['start_seconds'] = round(start * factors[shot_id], 6)
                line['end_seconds'] = round(end * factors[shot_id], 6)
                _interval(line, 'retimed dialogue')
            measured = line.get('measured_duration_seconds')
            if measured is not None:
                _number(measured, 'measured speech duration')
                if line['end_seconds'] - line['start_seconds'] + 1e-9 < measured:
                    raise RetimingError('measured speech no longer fits its dialogue interval')

    # An explicitly shared local keyframe cannot acquire two different times.
    proposals = {}
    for shot_id, shot in shots.items():
        asset_ids = shot.get('asset_ids')
        if (not isinstance(asset_ids, list)
                or any(not isinstance(aid, str) or aid not in assets for aid in asset_ids)
                or len(asset_ids) != len(set(asset_ids))):
            raise RetimingError('shot asset mapping is missing or unknown')
        for asset_id in asset_ids:
            asset = assets[asset_id]
            if 'time_seconds' not in asset:
                continue
            old_time = _number(asset['time_seconds'], 'shot-local asset time')
            new_time = old_time if factors[shot_id] == 1 else round(old_time * factors[shot_id], 6)
            if asset_id in proposals and proposals[asset_id] != new_time:
                raise RetimingError('shared timed asset has contradictory local times')
            proposals[asset_id] = new_time
    for asset_id, value in proposals.items():
        assets[asset_id]['time_seconds'] = value

    section_deltas, mapped_scenes = {}, {}
    section_positions = {sid: i for i, sid in enumerate(sections)}
    previous_position = -1
    for scene in scenes.values():
        section_id = scene.get('script_section_id')
        if not isinstance(section_id, str) or section_id not in sections:
            raise RetimingError('scene has no exact script-section mapping')
        position = section_positions[section_id]
        if position < previous_position:
            raise RetimingError('scene-to-script mapping contradicts section order')
        previous_position = position
        mapped_scenes.setdefault(section_id, []).append(scene)
        section_deltas[section_id] = section_deltas.get(section_id, 0) + deltas.get(scene['id'], 0)
    for section_id, group in mapped_scenes.items():
        if len(group) < 2:
            scene = group[0]
            if factors.get(scene['id'], 1) != 1:
                section_bounds = _interval(sections[section_id], 'script section')
                if section_bounds != _interval(scene, 'scene'):
                    raise RetimingError('script section has ambiguous time bounds')
            continue
        group_factors = [factors.get(row['id'], 1) for row in group]
        if len(set(group_factors)) != 1:
            raise RetimingError('shared script section has incompatible shot durations')
        if group_factors[0] != 1:
            section_start, section_end = _interval(sections[section_id], 'shared script section')
            if section_start != group[0]['start_seconds'] or section_end != group[-1]['end_seconds']:
                raise RetimingError('shared script section has ambiguous time bounds')

    baseline_end = max(_interval(row, 'script section')[1] for row in sections.values())
    baseline_total = _number(script.get('total_duration_seconds'), 'script total duration', positive=True)
    if baseline_total < baseline_end:
        raise RetimingError('script total duration precedes its section end')
    _ripple(list(scenes.values()), deltas, 'scene')
    _ripple(list(sections.values()), section_deltas, 'script section')
    total_delta = sum(deltas.values())
    if total_delta:
        script['total_duration_seconds'] = round(baseline_total + total_delta, 6)
    _number(script['total_duration_seconds'], 'retimed script total duration', positive=True)
    if script['total_duration_seconds'] + 1e-9 < max(row['end_seconds'] for row in sections.values()):
        raise RetimingError('retimed script total duration precedes its section end')
    return {'contract': contract, 'scene_plan': scene_plan, 'script': script}
