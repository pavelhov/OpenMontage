"""Offline factory card/source regressions through existing delivery utilities."""
import copy
import json
from pathlib import Path

import pytest

from lib import production_draft as draft
from lib.shot_contract import file_sha256
from tests.integration.test_first_pass_workflow import production  # noqa: F401
from tests.lib.test_first_cut import p, needs_ff  # noqa: F401
from tests.integration.test_standalone_delivery import clips, counting  # noqa: F401
from tests.lib.test_trimmed_derivative import factory_trimmed  # noqa: F401

REAL_CANDIDATES = draft.first_cut_candidates


@needs_ff
def test_cards_reuse_replace_remove_and_reversion_becomes_latest(p, clips):
    calls = []
    deliver = lambda **kw: draft.deliver_standalone(p.root, compose=counting(calls), **kw)
    original = deliver()
    card = {'before_shot_id': 'interior', 'text': 'Later', 'duration_seconds': 0.5}
    inserted = deliver(title_cards=[card])
    assert inserted['title_cards'][0]['text'] == 'Later'
    assert inserted['clips'][1]['master_start'] == 1.5
    assert deliver()['first_cut'] == inserted['first_cut']
    reverted = deliver(title_cards=[])
    assert reverted['first_cut'] != original['first_cut']
    assert draft.latest_first_cut(p.root)['export'] == reverted['export']
    assert draft.latest_first_cut(p.root)['sequence'] == 3
    assert not reverted['title_cards']
    assert deliver(title_cards=[])['first_cut'] == reverted['first_cut']
    assert len(calls) == 3
    replacement = deliver(title_cards=[dict(card, text='Meanwhile')])
    retained = p.root / replacement['title_cards'][0]['output']['path']
    retained.write_bytes(retained.read_bytes() + b'changed')
    assert 'title_card_changed' in draft.first_cut_media_status(p.root, replacement['first_cut'])['reasons']
    recovered = deliver()
    assert recovered['first_cut'] != replacement['first_cut']
    assert retained.read_bytes().endswith(b'changed')


@needs_ff
def test_fractional_card_placement_binds_rendered_duration(p, clips):
    cut = draft.deliver_standalone(p.root, title_cards=[
        {'before_shot_id': 'interior', 'text': 'A moment', 'duration_seconds': 0.01}])
    card = cut['title_cards'][0]
    measured = draft._probe(p.root / card['output']['path'])['duration_seconds']
    assert measured > 0.01
    assert card['duration_seconds'] == 0.01
    assert card['rendered_duration_seconds'] == measured
    assert card['master_end'] - card['master_start'] == pytest.approx(measured)
    assert cut['clips'][1]['master_start'] == card['master_end']
    assert cut['technical']['duration_seconds'] == pytest.approx(3 + measured, abs=0.05)
    assert draft.deliver_standalone(p.root)['first_cut'] == cut['first_cut']


@pytest.fixture
def external(p, tmp_path, monkeypatch):
    result = p.generate('entry')
    assert result.success, result.error
    source = Path(result.artifacts[0])
    target = tmp_path / 'completion-target'
    target.mkdir()
    marker = json.loads((p.root / 'project.json').read_text())
    marker['project_id'] = target.name
    (target / 'project.json').write_text(json.dumps(marker))
    missing = {'shot_id': 'entry', 'status': 'missing', 'reason': 'no_attempt', 'detail': {}}
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: [missing])
    entry = {'shot_id': 'entry', 'project_dir': str(p.root),
             'attempt_id': result.data['production_attempt_id'],
             'output': {'path': str(source.relative_to(p.root)), 'sha256': file_sha256(source)}}
    return p, target, source, entry


def test_external_source_retains_exact_provenance_and_refuses_mutation(external):
    source_project, root, source, entry = external
    draft._register_source_clips(root, [entry])
    record_path = root / 'production_source_clips/entry.json'
    before = record_path.read_bytes()
    record = json.loads(before)
    row = draft._source_clip_row(root, record)
    assert row['source_project']['project_dir'] == str(source_project.root)
    assert row['attempt_id'] == entry['attempt_id'] and row['shot_id'] == 'entry'
    assert (root / row['output']['path']).read_bytes() == source.read_bytes()
    draft._register_source_clips(root, [entry])
    assert record_path.read_bytes() == before
    changed = copy.deepcopy(entry)
    changed['attempt_id'] = 'another-attempt'
    with pytest.raises(ValueError, match='different registered'):
        draft._register_source_clips(root, [changed])
    source.write_bytes(source.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        draft._source_clip_row(root, record)


def test_interrupted_source_copy_retries_without_poisoning_final(external, monkeypatch):
    import shutil
    _, root, source, entry = external
    final = root / 'production_source_clips' / entry['output']['sha256'] / 'output.mp4'
    real_copy = shutil.copyfileobj

    def interrupted(incoming, retained):
        retained.write(incoming.read(7))
        raise KeyboardInterrupt('synthetic copy interruption')

    monkeypatch.setattr(shutil, 'copyfileobj', interrupted)
    with pytest.raises(KeyboardInterrupt):
        draft._register_source_clips(root, [entry])
    assert not final.exists()
    assert not list(final.parent.glob('*.tmp'))
    monkeypatch.setattr(shutil, 'copyfileobj', real_copy)
    draft._register_source_clips(root, [entry])
    assert final.read_bytes() == source.read_bytes()
    final.chmod(0o644)
    final.write_bytes(b'changed retained final')
    with pytest.raises(ValueError, match='missing or changed'):
        draft._register_source_clips(root, [entry])
    assert final.read_bytes() == b'changed retained final'


def test_external_source_cannot_override_playable_own_clip(external, monkeypatch):
    _, root, _, entry = external
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda root: [{'shot_id': 'entry', 'status': 'candidate'}])
    with pytest.raises(ValueError, match='already has a playable'):
        draft._register_source_clips(root, [entry])
    assert not (root / 'production_source_clips').exists()


def test_registered_source_enters_normal_candidate_order_and_tracks_source_currency(external, monkeypatch):
    import shutil
    source_project, root, source, entry = external
    draft._register_source_clips(root, [entry])
    (root / 'artifacts').mkdir()
    shutil.copyfile(source_project.root / 'artifacts/shot_contract.json', root / 'artifacts/shot_contract.json')
    monkeypatch.setattr(draft, 'first_cut_candidates', REAL_CANDIDATES)
    rows = draft.first_cut_candidates(root)
    assert rows[0]['shot_id'] == 'entry' and rows[0]['status'] == 'candidate'
    assert rows[0]['source_project']['project_dir'] == str(source_project.root)
    assert rows[0]['output']['sha256'] == entry['output']['sha256']
    assert len(rows) == len(source_project.contract['shots'])
    source.write_bytes(source.read_bytes() + b'changed')
    changed = draft.first_cut_candidates(root)[0]
    assert changed['status'] == 'missing' and changed['reason'] == 'unplayable'


def test_external_same_parent_derivative_reselection_preserves_history_and_cuts(factory_trimmed, tmp_path, monkeypatch):
    from lib import production_execution as execution
    from lib.shot_contract import UPSTREAM_PREDICATES, selection_digest
    from tests.integration.test_first_pass_workflow import attestation
    from tests.lib.test_trimmed_derivative import read, save

    value, attempt_id, _, derived, _ = factory_trimmed
    root = tmp_path / 'external-final'
    root.mkdir()
    marker = read(value.root / 'project.json')
    marker['project_id'] = root.name
    save(root / 'project.json', marker)
    (root / 'artifacts').mkdir()
    save(root / 'artifacts/shot_contract.json', read(value.root / 'artifacts/shot_contract.json'))
    original = {'shot_id': 'entry', 'project_dir': str(value.root), 'attempt_id': attempt_id,
                'output': {'path': str(Path(derived['parent_output']['path']).relative_to(value.root)),
                           'sha256': derived['parent_output']['sha256']}}
    first = draft.deliver_standalone(root, source_clips=[original])
    old_record = (root / first['first_cut']['path']).read_bytes()
    old_export = Path(first['export_path']).read_bytes()
    current_path = root / 'production_source_clips/entry.json'
    original_registration = current_path.read_bytes()
    old_copy = root / read(current_path)['copy']['path']
    old_media = old_copy.read_bytes()

    execution.record_derived_edit(value.root, derived)
    selection = {'attempt_id': attempt_id, 'output': derived['output'], 'outgoing_frame': derived['outgoing_frame']}
    review = attestation(selection_digest(selection), UPSTREAM_PREDICATES, revision=value.story['story_revision'])
    review['status'] = 'unknown'
    for predicate in review['predicates']:
        predicate.update(status='unknown', evidence='SYNTHETIC software test observation only.')
    selection['review'] = review
    execution.record_selection(value.root, 'entry', selection)
    replacement = {**original, 'output': {'path': str(Path(derived['output']['path']).relative_to(value.root)),
                                         'sha256': derived['output']['sha256']}}
    # A playable own clip cannot be replaced, even after an external registration.
    with monkeypatch.context() as patch:
        patch.setattr(draft, 'first_cut_candidates', lambda _: [{'shot_id': 'entry', 'status': 'candidate'}])
        with pytest.raises(ValueError, match='already has a playable'):
            draft._register_source_clips(root, [replacement])
    assert current_path.read_bytes() == original_registration

    # An interrupted current-pointer publication leaves the complete old value.
    real_replace = Path.replace
    with monkeypatch.context() as patch:
        def interrupted(path, target):
            if Path(target) == current_path:
                raise KeyboardInterrupt('synthetic registration interruption')
            return real_replace(path, target)
        patch.setattr(Path, 'replace', interrupted)
        with pytest.raises(KeyboardInterrupt):
            draft._register_source_clips(root, [replacement])
    assert current_path.read_bytes() == original_registration
    second = draft.deliver_standalone(root, source_clips=[replacement])
    assert second['first_cut'] != first['first_cut']
    assert second['clips'][0]['source_project']['attempt_id'] == attempt_id
    assert second['clips'][0]['output']['sha256'] == derived['output']['sha256']
    assert second['technical']['duration_seconds'] == pytest.approx(3.5, abs=0.05)
    assert (root / first['first_cut']['path']).read_bytes() == old_record
    assert Path(first['export_path']).read_bytes() == old_export
    assert old_copy.read_bytes() == old_media
    history = list((root / 'production_source_clips/registrations/entry').glob('*.json'))
    assert len(history) == 2
    assert any(path.read_bytes() == original_registration for path in history)
    current_registration = current_path.read_bytes()
    assert draft.deliver_standalone(root)['first_cut'] == second['first_cut']
    assert draft.deliver_standalone(root, source_clips=[replacement])['first_cut'] == second['first_cut']
    assert current_path.read_bytes() == current_registration
    assert len(list((root / 'production_source_clips/registrations/entry').glob('*.json'))) == 2

    changed_parent = {**replacement, 'attempt_id': 'different-native-parent'}
    with pytest.raises(ValueError, match='different registered'):
        draft._register_source_clips(root, [changed_parent])
    foreign = tmp_path / 'foreign-source'
    foreign.mkdir()
    save(foreign / 'project.json', {**read(value.root / 'project.json'), 'project_id': foreign.name})
    foreign_clip = foreign / 'clip.mp4'
    foreign_clip.write_bytes(Path(derived['output']['path']).read_bytes())
    changed_project = {**replacement, 'project_dir': str(foreign),
                       'output': {'path': 'clip.mp4', 'sha256': derived['output']['sha256']}}
    with pytest.raises(ValueError, match='different registered'):
        draft._register_source_clips(root, [changed_project])
    assert current_path.read_bytes() == current_registration
