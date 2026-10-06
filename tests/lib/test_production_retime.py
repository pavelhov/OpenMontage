"""Offline timeline tests; the pure transformer never grants policy authority."""
import copy
from fractions import Fraction
import math

import pytest

from lib.production_retime import RetimingError, retime_planning


def planning(origin=0, boundary_offset=0, trailing=0):
    contract = {'version': '1.0', 'story': {'payoff': 'Preserved ending'},
                'assets': [{'id': 'a1', 'role': 'timed_keyframe', 'time_seconds': 4,
                            'sha256': 'a' * 64, 'cast_ids': ['speaker']},
                           {'id': 'a2', 'role': 'start_frame', 'sha256': 'b' * 64}],
                'shots': []}
    for i in (1, 2):
        contract['shots'].append({
            'id': f'shot{i}', 'duration_seconds': 8, 'asset_ids': [f'a{i}'],
            'cast_ids': ['speaker'], 'completed_end_state': f'Ending {i}',
            'dialogue': [{'speaker_id': 'speaker', 'source': 'visible', 'text': f'Line {i}',
                          'start_seconds': 1, 'end_seconds': 5,
                          'fit_status': 'unknown', 'measured_duration_seconds': None}],
            'upstream': [{'shot_id': 'producer', 'role': 'outgoing_frame'}],
        })
    spans = [(origin, origin + 8), (origin + 8 + boundary_offset, origin + 16 + boundary_offset)]
    scene = {'version': '1.0', 'metadata': {'native_requirement': 'preserved'}, 'scenes': []}
    script = {'version': '1.0', 'title': 'Retained title',
              'total_duration_seconds': spans[-1][1] + trailing, 'sections': []}
    for i, (start, end) in enumerate(spans, 1):
        scene['scenes'].append({'id': f'shot{i}', 'script_section_id': f'section{i}',
                                'description': f'Scene {i}', 'start_seconds': start, 'end_seconds': end,
                                'character_actions': [{'character_id': 'speaker', 'action_sequence': ['wave']}]})
        script['sections'].append({'id': f'section{i}', 'text': f'Section {i}',
                                   'start_seconds': start, 'end_seconds': end,
                                   'enhancement_cues': [{'type': 'overlay', 'timestamp_seconds': 2,
                                                         'description': 'Time semantics unspecified'}],
                                   'delivery_cues': {'pause_after_seconds': 0.5}})
    return contract, scene, script


def spans(rows):
    return [(row['start_seconds'], row['end_seconds']) for row in rows]


@pytest.mark.parametrize('durations,expected,total', [
    ({'shot1': 6}, [(0, 6), (6, 14)], 14),
    ({'shot2': 10}, [(0, 8), (8, 18)], 18),
    ({'shot1': 6, 'shot2': 10}, [(0, 6), (6, 16)], 16),
    ({'shot1': 10, 'shot2': 6}, [(0, 10), (10, 16)], 16),
])
def test_multiple_shot_timeline_ripples_contiguously(durations, expected, total):
    original = planning()
    result = retime_planning(*original, durations)
    assert spans(result['scene_plan']['scenes']) == expected
    assert spans(result['script']['sections']) == expected
    assert result['script']['total_duration_seconds'] == total
    assert [shot['duration_seconds'] for shot in result['contract']['shots']] == [
        durations.get('shot1', 8), durations.get('shot2', 8)]


def test_nonzero_origin_and_explicit_trailing_time_are_preserved():
    result = retime_planning(*planning(origin=3, trailing=2), {'shot1': 6})
    assert spans(result['scene_plan']['scenes']) == [(3, 9), (9, 17)]
    assert spans(result['script']['sections']) == [(3, 9), (9, 17)]
    assert result['script']['total_duration_seconds'] == 19


def test_unmodified_later_transition_and_section_shift_by_preceding_delta():
    contract, scene, script = planning()
    scene['scenes'].append({'id': 'transition', 'script_section_id': 'tail',
                           'start_seconds': 16, 'end_seconds': 20, 'description': 'Approved tail'})
    script['sections'].append({'id': 'tail', 'text': 'Approved ending', 'start_seconds': 16, 'end_seconds': 20})
    script['total_duration_seconds'] = 20
    result = retime_planning(contract, scene, script, {'shot1': 6})
    assert spans(result['scene_plan']['scenes']) == [(0, 6), (6, 14), (14, 18)]
    assert spans(result['script']['sections']) == [(0, 6), (6, 14), (14, 18)]
    assert result['script']['total_duration_seconds'] == 18


@pytest.mark.parametrize('offset', [2, -2])
def test_approved_gap_or_overlap_is_preserved_without_creating_a_new_one(offset):
    result = retime_planning(*planning(boundary_offset=offset), {'shot1': 6})
    for rows in (result['scene_plan']['scenes'], result['script']['sections']):
        assert rows[1]['start_seconds'] - rows[0]['end_seconds'] == offset
        assert rows[1]['end_seconds'] - rows[1]['start_seconds'] == 8
    assert result['script']['total_duration_seconds'] == 14 + offset


def test_inputs_and_every_non_time_field_remain_exactly_retained():
    original = planning()
    saved = copy.deepcopy(original)
    result = retime_planning(*original, {'shot1': 6})
    assert original == saved
    expected = copy.deepcopy(saved)
    expected[0]['shots'][0]['duration_seconds'] = 6
    expected[0]['shots'][0]['dialogue'][0].update(start_seconds=0.75, end_seconds=3.75)
    expected[0]['assets'][0]['time_seconds'] = 3
    for doc, key in ((expected[1], 'scenes'), (expected[2], 'sections')):
        doc[key][0]['end_seconds'] = 6
        doc[key][1].update(start_seconds=6, end_seconds=14)
    expected[2]['total_duration_seconds'] = 14
    assert result == dict(zip(('contract', 'scene_plan', 'script'), expected))
    result['contract']['story']['payoff'] = 'Changed returned copy'
    assert original == saved


def test_noop_is_exact_deep_copy_and_no_policy_authority_is_created():
    original = planning()
    original[0]['caller_annotation'] = {'approved': False, 'manual_current_plan': True}
    result = retime_planning(*original, {})
    assert result == dict(zip(('contract', 'scene_plan', 'script'), original))
    assert result['contract']['caller_annotation']['approved'] is False
    assert set(result) == {'contract', 'scene_plan', 'script'}
    assert result['script'] is not original[2]


@pytest.mark.parametrize('duration', [True, False, 0, -1, math.nan, math.inf, -math.inf, '6', None, 10 ** 1000])
def test_invalid_duration_fails_without_mutating_sources(duration):
    original = planning()
    saved = copy.deepcopy(original)
    with pytest.raises(RetimingError):
        retime_planning(*original, {'shot1': duration})
    assert original == saved


def test_unknown_shot_is_rejected():
    with pytest.raises(RetimingError, match='unknown shot'):
        retime_planning(*planning(), {'invented-shot': 6})


def test_real_duration_is_normalized_to_a_plain_json_number():
    result = retime_planning(*planning(), {'shot1': Fraction(13, 2)})
    assert type(result['contract']['shots'][0]['duration_seconds']) is float
    assert spans(result['scene_plan']['scenes']) == [(0, 6.5), (6.5, 14.5)]


def test_preserving_an_overlap_cannot_reverse_global_timeline_order():
    original = planning(origin=10, boundary_offset=-7)
    with pytest.raises(RetimingError, match='reverse timeline order'):
        retime_planning(*original, {'shot1': 6})


@pytest.mark.parametrize('case', ['shot_id', 'scene_id', 'section_id', 'asset_id', 'missing_scene',
                                  'missing_section', 'reverse_mapping'])
def test_ambiguous_or_missing_mapping_is_rejected(case):
    contract, scene, script = planning()
    if case in {'shot_id', 'scene_id', 'section_id', 'asset_id'}:
        rows = {'shot_id': contract['shots'], 'scene_id': scene['scenes'],
                'section_id': script['sections'], 'asset_id': contract['assets']}[case]
        rows[1]['id'] = rows[0]['id']
    elif case == 'missing_scene':
        scene['scenes'][0]['id'] = 'unmapped'
    elif case == 'missing_section':
        scene['scenes'][0]['script_section_id'] = 'unmapped'
    else:
        scene['scenes'][0]['script_section_id'], scene['scenes'][1]['script_section_id'] = 'section2', 'section1'
    with pytest.raises(RetimingError):
        retime_planning(contract, scene, script, {'shot1': 6})


def shared_section_planning():
    contract, scene, script = planning()
    scene['scenes'][1]['script_section_id'] = 'section1'
    script['sections'] = [script['sections'][0]]
    script['sections'][0]['end_seconds'] = 16
    return contract, scene, script


def test_shared_section_with_incompatible_changes_is_rejected():
    with pytest.raises(RetimingError, match='shared script section'):
        retime_planning(*shared_section_planning(), {'shot1': 6})


def test_shared_section_with_compatible_changes_has_one_deterministic_result():
    result = retime_planning(*shared_section_planning(), {'shot1': 6, 'shot2': 6})
    assert spans(result['scene_plan']['scenes']) == [(0, 6), (6, 12)]
    assert spans(result['script']['sections']) == [(0, 12)]
    assert result['script']['total_duration_seconds'] == 12


def test_shared_section_with_ambiguous_bounds_is_rejected():
    original = shared_section_planning()
    original[2]['sections'][0]['end_seconds'] = 12
    with pytest.raises(RetimingError, match='ambiguous time bounds'):
        retime_planning(*original, {'shot1': 6, 'shot2': 6})


@pytest.mark.parametrize('duration', [6, 10, 4])
def test_changed_sole_mapped_section_with_unequal_bounds_is_rejected(duration):
    original = planning()
    original[2]['sections'][0]['end_seconds'] = 4
    with pytest.raises(RetimingError, match='ambiguous time bounds'):
        retime_planning(*original, {'shot1': duration})


def test_unchanged_sole_mapped_section_with_unequal_bounds_is_preserved():
    original = planning()
    original[2]['sections'][0]['end_seconds'] = 4
    result = retime_planning(*original, {'shot1': 8})
    assert result == dict(zip(('contract', 'scene_plan', 'script'), original))


def test_unequal_section_bounds_can_shift_when_its_own_shot_duration_is_unchanged():
    original = planning()
    original[2]['sections'][1]['end_seconds'] = 12
    result = retime_planning(*original, {'shot1': 6})
    assert spans(result['scene_plan']['scenes']) == [(0, 6), (6, 14)]
    assert spans(result['script']['sections']) == [(0, 6), (6, 10)]
    assert result['script']['total_duration_seconds'] == 14


def test_shared_timed_asset_conflict_is_rejected_without_last_write_wins():
    original = planning()
    original[0]['shots'][1]['asset_ids'].append('a1')
    with pytest.raises(RetimingError, match='shared timed asset'):
        retime_planning(*original, {'shot1': 6})
    assert original[0]['assets'][0]['time_seconds'] == 4


def test_shared_timed_asset_with_equal_local_times_is_safe():
    original = planning()
    original[0]['shots'][1]['asset_ids'].append('a1')
    result = retime_planning(*original, {'shot1': 6, 'shot2': 6})
    assert result['contract']['assets'][0]['time_seconds'] == 3


def test_measured_speech_cannot_be_squeezed_below_its_existing_measurement():
    original = planning()
    original[0]['shots'][0]['dialogue'][0]['measured_duration_seconds'] = 4
    with pytest.raises(RetimingError, match='measured speech'):
        retime_planning(*original, {'shot1': 6})
    result = retime_planning(*original, {'shot1': 10})
    line = result['contract']['shots'][0]['dialogue'][0]
    assert line['measured_duration_seconds'] == 4
    assert line['fit_status'] == 'unknown'


@pytest.mark.parametrize('case', ['objects', 'map', 'duration', 'interval', 'dialogue', 'asset_time', 'total',
                                  'section_mapping_type', 'asset_mapping_type', 'repeated_asset_mapping'])
def test_malformed_timing_is_rejected(case):
    contract, scene, script = planning()
    if case == 'objects':
        contract = []
    elif case == 'duration':
        contract['shots'][0]['duration_seconds'] = True
    elif case == 'interval':
        scene['scenes'][0]['end_seconds'] = 0
    elif case == 'dialogue':
        contract['shots'][0]['dialogue'][0]['end_seconds'] = 9
    elif case == 'asset_time':
        contract['assets'][0]['time_seconds'] = math.nan
    elif case == 'total':
        script['total_duration_seconds'] = 15
    elif case == 'section_mapping_type':
        scene['scenes'][0]['script_section_id'] = []
    elif case == 'asset_mapping_type':
        contract['shots'][0]['asset_ids'] = [{}]
    elif case == 'repeated_asset_mapping':
        contract['shots'][0]['asset_ids'] *= 2
    with pytest.raises(RetimingError):
        retime_planning(contract, scene, script, [] if case == 'map' else {'shot1': 6})
