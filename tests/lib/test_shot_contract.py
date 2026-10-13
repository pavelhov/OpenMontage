"""Offline evidence contracts: synthetic fixtures do not establish visual quality."""
import copy
import json
from pathlib import Path

import pytest

from lib.shot_contract import (
    contract_digest, file_sha256, review_digest, selection_digest,
    validate_shot_contract,
)
from schemas.artifacts import validate_artifact

FIXTURES = Path(__file__).parents[1] / "fixtures" / "first_pass"


@pytest.fixture
def package(tmp_path):
    contract = json.loads((FIXTURES / "valid_shot_contract.json").read_text())
    for asset in contract["assets"]:
        path = tmp_path / asset["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((FIXTURES / "synthetic-board.svg").read_bytes())
        asset["sha256"] = file_sha256(path)
        asset["review"]["subject_sha256"] = asset["sha256"]
    refresh(contract)
    return contract, tmp_path


def refresh(contract):
    digest = contract_digest(contract)
    contract["project_review"]["subject_sha256"] = digest
    for shot in contract["shots"]:
        shot["review"]["subject_sha256"] = digest


def check(package, shot="entry", **kwargs):
    contract, root = package
    return validate_shot_contract(contract, project_dir=root, shot_id=shot, **kwargs)


def test_valid_completed_shot_and_legacy_scene_plan(package):
    assert check(package)["eligible"]
    validate_artifact("shot_contract", package[0])
    validate_artifact("scene_plan", {"version": "1.0", "scenes": [{
        "id": "entry", "type": "generated", "description": "Entry",
        "start_seconds": 0, "end_seconds": 8}]})


@pytest.mark.parametrize("case", json.loads((FIXTURES / "c50_contract_negatives.json").read_text()))
def test_labelled_c50_failure_blocks(package, case):
    contract, _ = package
    target = contract
    for key in case["path"][:-1]:
        target = target[key]
    target[case["path"][-1]] = case["value"]
    refresh(contract)  # Even a fresh signature cannot turn failed evidence into pass.
    result = check(package)
    assert not result["eligible"], case["label"]
    assert any(case["error_contains"] in error for error in result["errors"]), result


def test_missing_field_reports_planner_path(package):
    del package[0]["shots"][0]["completed_end_state"]
    result = check(package)
    assert not result["eligible"]
    assert "completed_end_state" in " ".join(result["errors"])


def test_changed_asset_and_story_are_stale(package):
    contract, root = package
    (root / contract["assets"][0]["path"]).write_text("replacement")
    assert not check(package)["eligible"]
    assert not check(package, story_revision="new revision")["eligible"]


def test_changed_plan_invalidates_review(package):
    package[0]["shots"][0]["dominant_action"] = "A different approved action"
    assert not check(package)["eligible"]


def test_cosmetic_warning_does_not_block(package):
    package[0]["shots"][0]["review"]["predicates"].append({
        "name": "lighting_polish", "status": "fail", "severity": "cosmetic",
        "evidence": "Slight mismatch; action and cast still read."})
    result = check(package)
    assert result["eligible"] and result["warnings"]


def test_critical_cannot_be_downgraded(package):
    predicate = package[0]["shots"][0]["review"]["predicates"][0]
    predicate.update(status="fail", severity="cosmetic")
    assert not check(package)["eligible"]


def dependency(package):
    contract, root = package
    for name in ["upstream-output.bin", "upstream-frame.svg"]:
        (root / name).write_bytes(b"synthetic upstream evidence")
    selection = {
        "attempt_id": "entry-first-1",
        "output": {"path": "upstream-output.bin", "sha256": file_sha256(root / "upstream-output.bin")},
        "outgoing_frame": {"path": "upstream-frame.svg", "sha256": file_sha256(root / "upstream-frame.svg")},
        "review": copy.deepcopy(contract["shots"][0]["review"]),
    }
    selection["review"]["predicates"].append({"name": "outgoing_frame", "status": "pass", "evidence": "Frame sampled after completed entry."})
    selection["review"]["subject_sha256"] = selection_digest(selection)
    shot = copy.deepcopy(contract["shots"][0])
    shot.update(id="interior", transition={"type": "location_cut", "rationale": "Entry completes before interior coverage."})
    shot["upstream"] = [{"shot_id": "entry", "attempt_id": selection["attempt_id"],
        "output_sha256": selection["output"]["sha256"],
        "outgoing_frame_sha256": selection["outgoing_frame"]["sha256"],
        "review_sha256": review_digest(selection["review"])}]
    contract["shots"].append(shot)
    refresh(contract)
    return {"entry": selection}


def test_deliberate_location_cut_requires_current_completed_upstream(package):
    selected = dependency(package)
    assert check(package, "interior", selected_upstream=selected)["eligible"]
    assert not check(package, "interior")["eligible"]
    selected["entry"]["attempt_id"] = "entry-repair-1"
    assert not check(package, "interior", selected_upstream=selected)["eligible"]


@pytest.mark.parametrize("change", ["review", "output", "frame", "missing_frame_review", "action_fail"])
def test_upstream_changed_or_unknown_evidence_blocks(package, change):
    selected = dependency(package)
    item = selected["entry"]
    if change == "review":
        item["review"]["reviewer"] = "new reviewer"
    elif change in ("output", "frame"):
        key = "output" if change == "output" else "outgoing_frame"
        (package[1] / item[key]["path"]).write_text("changed bytes")
    elif change == "missing_frame_review":
        item["review"]["predicates"].pop()
    else:
        next(p for p in item["review"]["predicates"] if p["name"] == "completed_action")["status"] = "fail"
    assert not check(package, "interior", selected_upstream=selected)["eligible"]


def test_same_bytes_keep_endpoint_and_identity_roles(package):
    assert check(package)["eligible"]
    contract = package[0]
    assert len({a["sha256"] for a in contract["assets"]}) == 1
    contract["assets"][0]["role"] = "identity_reference"
    refresh(contract)
    assert not check(package)["eligible"]


def test_unknown_version_is_ineligible(package):
    package[0]["version"] = "0.9"
    assert not check(package)["eligible"]


def test_digest_projection_excludes_reviews_but_binds_asset_and_upstream_hashes(package):
    contract = package[0]
    before = contract_digest(contract)
    contract["project_review"]["subject_sha256"] = "f" * 64
    contract["assets"][0]["review"]["reviewer"] = "Another reviewer"
    contract["shots"][0]["review"]["subject_sha256"] = "e" * 64
    assert contract_digest(contract) == before
    contract["assets"][0]["sha256"] = "d" * 64
    assert contract_digest(contract) != before
    selected = dependency(package)
    before = contract_digest(contract)
    contract["shots"][1]["upstream"][0]["review_sha256"] = "c" * 64
    assert contract_digest(contract) != before
    selection = selected["entry"]
    before = selection_digest(selection)
    selection["review"]["subject_sha256"] = "b" * 64
    assert selection_digest(selection) == before
    selection["outgoing_frame"]["sha256"] = "a" * 64
    assert selection_digest(selection) != before


@pytest.mark.parametrize("predicate", ["cast_identity", "cast_count", "completed_action", "speaker_source", "possession", "transformation"])
def test_missing_named_critical_predicate_blocks(package, predicate):
    review = package[0]["shots"][0]["review"]
    review["predicates"] = [p for p in review["predicates"] if p["name"] != predicate]
    result = check(package)
    assert not result["eligible"]
    assert predicate in " ".join(result["errors"])


def test_duplicate_conflicting_critical_evidence_blocks(package):
    review = package[0]["shots"][0]["review"]
    review["predicates"].append({"name": "completed_action", "status": "unknown", "evidence": "Unclear final frame."})
    assert not check(package)["eligible"]


@pytest.mark.parametrize("change", ["missing_payoff", "missing_late_cast", "stale_payoff", "missing_payoff_predicate"])
def test_project_payoff_gate_blocks_ready_opening(package, change):
    contract = package[0]
    if change == "missing_payoff":
        contract["assets"] = [a for a in contract["assets"] if a["id"] != "payoff"]
    elif change == "missing_late_cast":
        contract["assets"] = [a for a in contract["assets"] if a["id"] != "doctor"]
    elif change == "stale_payoff":
        contract["assets"][3]["review"]["story_revision"] = "old story"
    else:
        contract["project_review"]["predicates"] = [p for p in contract["project_review"]["predicates"] if p["name"] != "payoff"]
    refresh(contract)
    assert not check(package)["eligible"]


def test_valid_transformation_and_visible_dialogue(package):
    shot = package[0]["shots"][0]
    shot["allowed_transformations"] = [{"id": "stretch", "description": "Stretch patient while crawling", "preserved_invariants": shot["prop_body_invariants"]}]
    shot["transition"] = {"type": "transformation", "transformation_id": "stretch", "rationale": "Stretch supports the action."}
    shot["required_visible_speakers"] = ["patient"]
    shot["dialogue"] = [{"speaker_id": "patient", "source": "visible", "text": "I am going in.", "start_seconds": 0.5, "end_seconds": 2}]
    refresh(package[0])
    assert check(package)["eligible"]
    shot["dialogue"][0]["end_seconds"] = 10
    refresh(package[0])
    assert not check(package)["eligible"]


@pytest.mark.parametrize("factory", [False, True])
@pytest.mark.parametrize("matching_wording", [False, True])
def test_transformation_invariant_wording_is_report_only_for_factory(
    package, monkeypatch, factory, matching_wording,
):
    from lib import shot_contract
    monkeypatch.setattr(shot_contract, "factory_project", lambda root: factory)
    contract, _ = package
    shot = contract["shots"][0]
    shot["prop_body_invariants"] = [
        "Same continuous red wool strand.", "Tung intact body and face.",
    ]
    preserved = list(shot["prop_body_invariants"]) if matching_wording else [
        "Preserve one continuous red wool strand throughout the action.",
        "Keep Tung's body and facial features intact while he pulls the wool.",
    ]
    shot["allowed_transformations"] = [{
        "id": "unravel", "description": "The wool unravels as Tung pulls.",
        "preserved_invariants": preserved,
    }]
    shot["transition"] = {
        "type": "transformation", "transformation_id": "unravel",
        "rationale": "The transformation is the authored action.",
    }
    refresh(contract)
    before = copy.deepcopy(contract)
    digest = contract_digest(contract)

    result = check(package)

    assert result["eligible"] is (factory or matching_wording), result
    if matching_wording:
        assert result["errors"] == [] and result["warnings"] == []
    elif factory:
        assert result["errors"] == []
        assert len(result["warnings"]) == 1
        assert "prop/body invariants" in result["warnings"][0]
        assert "semantic preservation not verified" in result["warnings"][0]
    else:
        assert result["warnings"] == []
        assert result["errors"] == [
            "shots.entry.allowed_transformations: must preserve prop/body invariants",
        ]
    assert contract == before
    assert contract_digest(contract) == digest


@pytest.mark.parametrize("failure,error_contains", [
    ("unknown_transformation", "unapproved transformation"),
    ("duplicate_transformation", "duplicate id"),
    ("wrong_transition_type", "transformation_id requires transformation type"),
    ("undeclared_dialogue_speaker", "undeclared speaker"),
    ("dialogue_timing", "invalid timing"),
    ("duration", "duration_seconds"),
    ("source_bytes", "input bytes changed"),
])
def test_factory_wording_warning_does_not_relax_factual_checks(
    package, monkeypatch, failure, error_contains,
):
    from lib import shot_contract
    monkeypatch.setattr(shot_contract, "factory_project", lambda root: True)
    contract, root = package
    shot = contract["shots"][0]
    shot["allowed_transformations"] = [{
        "id": "stretch", "description": "Stretch patient while crawling",
        "preserved_invariants": ["Different authored preservation wording."],
    }]
    shot["transition"] = {
        "type": "transformation", "transformation_id": "stretch",
        "rationale": "Stretch supports the action.",
    }
    if failure == "unknown_transformation":
        shot["transition"]["transformation_id"] = "undeclared"
    elif failure == "duplicate_transformation":
        duplicate = copy.deepcopy(shot["allowed_transformations"][0])
        duplicate["description"] = "Another transformation with the same id."
        shot["allowed_transformations"].append(duplicate)
    elif failure == "wrong_transition_type":
        shot["transition"]["type"] = "hard_cut"
    elif failure in {"undeclared_dialogue_speaker", "dialogue_timing"}:
        shot["dialogue"] = [{
            "speaker_id": "undeclared" if failure == "undeclared_dialogue_speaker" else "patient",
            "source": "offscreen", "text": "I am going in.",
            "start_seconds": 0.5, "end_seconds": 10 if failure == "dialogue_timing" else 2,
        }]
    elif failure == "duration":
        shot["duration_seconds"] = 0
    else:
        (root / contract["assets"][0]["path"]).write_bytes(b"changed source bytes")
    refresh(contract)

    result = check(package)

    assert not result["eligible"], result
    assert any(error_contains in error for error in result["errors"]), result


def test_upstream_must_precede_dependent_shot(package):
    selected = dependency(package)
    package[0]["shots"].reverse()
    refresh(package[0])
    assert not check(package, "interior", selected_upstream=selected)["eligible"]


def test_fixture_is_directly_valid_and_synthetic():
    contract = json.loads((FIXTURES / "valid_shot_contract.json").read_text())
    assert validate_shot_contract(contract, project_dir=FIXTURES, shot_id="entry")["eligible"]


def local_reviews(contract):
    from lib.shot_contract import project_review_subject, shot_review_subject
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])


@pytest.mark.parametrize('asset_ids', [['start', 'patient'], ['end', 'patient'],
                                      ['payoff', 'patient'], ['patient']])
@pytest.mark.parametrize('mode_location', ['project', 'shot'])
def test_local_guided_treatment_uses_only_authored_reviewed_boards(package, asset_ids, mode_location):
    contract, _ = package
    contract['planning_mode'] = 'shot_local_v1'
    target = contract if mode_location == 'project' else contract['shots'][0]
    target['reference_mode'] = 'reference_guided'
    contract['shots'][0]['asset_ids'] = asset_ids
    local_reviews(contract)
    result = check(package)
    assert result['eligible'], result


@pytest.mark.parametrize('local,guided', [(False, True), (True, False)])
def test_guided_or_local_flag_alone_preserves_literal_board_pair(package, local, guided):
    contract, _ = package
    if local:
        contract['planning_mode'] = 'shot_local_v1'
    if guided:
        contract['reference_mode'] = 'reference_guided'
    contract['shots'][0]['asset_ids'] = ['start', 'patient']
    local_reviews(contract)
    result = check(package)
    assert not result['eligible']
    assert any('require exactly one end_frame' in error for error in result['errors'])


def test_local_unknown_endpoint_is_frozen_planning_with_current_action_review(package):
    contract, _ = package
    contract['planning_mode'] = 'shot_local_v1'
    contract['shots'][0]['endpoint_completion'] = 'unknown'
    local_reviews(contract)
    before = copy.deepcopy(contract)
    result = check(package)
    assert result['eligible'], result
    assert contract == before


@pytest.mark.parametrize('failure', ['missing', 'unknown', 'fail', 'stale'])
def test_local_unknown_endpoint_still_requires_current_completed_action(package, failure):
    contract, _ = package
    contract['planning_mode'] = 'shot_local_v1'
    shot = contract['shots'][0]
    shot['endpoint_completion'] = 'unknown'
    local_reviews(contract)
    review = shot['review']
    if failure == 'missing':
        review['predicates'] = [p for p in review['predicates'] if p['name'] != 'completed_action']
    elif failure == 'stale':
        review['subject_sha256'] = 'f' * 64
    else:
        next(p for p in review['predicates'] if p['name'] == 'completed_action')['status'] = failure
    result = check(package)
    assert not result['eligible']
    assert any('completed_action' in error or 'stale review binding' in error for error in result['errors'])


@pytest.mark.parametrize('local,endpoint', [(True, 'partial'), (False, 'unknown'), (False, 'partial')])
def test_partial_or_legacy_unknown_endpoint_remains_blocked(package, local, endpoint):
    contract, _ = package
    if local:
        contract['planning_mode'] = 'shot_local_v1'
    contract['shots'][0]['endpoint_completion'] = endpoint
    local_reviews(contract)
    result = check(package)
    assert not result['eligible']
    assert any('endpoint_completion: completed endpoint required' in error for error in result['errors'])


def local_unknown_upstream(package):
    selected = dependency(package)
    contract = package[0]
    contract['planning_mode'] = 'shot_local_v1'
    contract['shots'][0]['endpoint_completion'] = 'unknown'
    local_reviews(contract)
    return selected


def test_local_unknown_upstream_requires_current_selected_completed_media(package):
    selected = local_unknown_upstream(package)
    before = copy.deepcopy(package[0])
    result = check(package, 'interior', selected_upstream=selected)
    assert result['eligible'], result
    assert package[0] == before


@pytest.mark.parametrize('failure', ['missing_selection', 'pending_binding', 'changed_attempt',
    'changed_output', 'missing_frame', 'changed_review', 'stale_review', 'missing_action',
    'unknown_action', 'failed_action', 'failed_frame', 'partial_endpoint'])
def test_local_unknown_upstream_does_not_waive_actual_evidence(package, failure):
    selected = local_unknown_upstream(package)
    contract, root = package
    item = selected['entry']
    if failure == 'missing_selection':
        selected = {}
    elif failure == 'pending_binding':
        contract['shots'][1]['upstream'] = [{'shot_id': 'entry'}]
    elif failure == 'changed_attempt':
        item['attempt_id'] = 'entry-repair-1'
    elif failure == 'changed_output':
        (root / item['output']['path']).write_bytes(b'changed selected footage')
    elif failure == 'missing_frame':
        (root / item['outgoing_frame']['path']).unlink()
    elif failure == 'changed_review':
        item['review']['reviewer'] = 'another reviewer'
    elif failure == 'partial_endpoint':
        contract['shots'][0]['endpoint_completion'] = 'partial'
    else:
        review = item['review']
        if failure == 'stale_review':
            review['subject_sha256'] = 'f' * 64
        elif failure == 'missing_action':
            review['predicates'] = [p for p in review['predicates'] if p['name'] != 'completed_action']
        else:
            name = 'outgoing_frame' if failure == 'failed_frame' else 'completed_action'
            next(p for p in review['predicates'] if p['name'] == name)['status'] = (
                'unknown' if failure == 'unknown_action' else 'fail')
        # Bind the authored expectation to this review to exercise its verdict,
        # rather than merely failing because the retained hash differs.
        contract['shots'][1]['upstream'][0]['review_sha256'] = review_digest(review)
    local_reviews(contract)
    result = check(package, 'interior', selected_upstream=selected)
    assert not result['eligible']
    assert any('upstream.entry' in error for error in result['errors'])


def test_prospective_independent_shot_ignores_unready_later_boards(package):
    contract, _ = package
    later = copy.deepcopy(contract['shots'][0]); later['id'] = 'S06'
    later['asset_ids'] = ['unprepared-start', 'unprepared-end']
    contract['shots'].append(later)
    contract['planning_mode'] = 'shot_local_v1'
    contract['assets'] = [a for a in contract['assets'] if a['id'] not in {'payoff', 'doctor'}]
    local_reviews(contract)
    assert check(package)['eligible']
    assert not check(package, 'S06')['eligible']
    before = contract['shots'][0]['review']['subject_sha256']
    later['dominant_action'] = 'A different future action'
    assert check(package)['eligible']
    assert contract['shots'][0]['review']['subject_sha256'] == before


def test_local_shared_source_only_invalidates_actual_consumers(package):
    contract, _ = package
    future = copy.deepcopy(contract['shots'][0]); future.update(id='S06', cast_ids=['doctor'])
    contract['shots'].append(future); contract['planning_mode'] = 'shot_local_v1'
    local_reviews(contract)
    doctor = next(a for a in contract['assets'] if a['id'] == 'doctor')
    doctor['sha256'] = 'a' * 64
    assert check(package)['eligible']
    assert not check(package, 'S06')['eligible']
    patient = next(a for a in contract['assets'] if a['id'] == 'patient')
    patient['sha256'] = 'b' * 64
    assert not check(package)['eligible']


def test_local_pending_upstream_and_protected_story_still_block(package):
    contract, _ = package
    selected = dependency(package)
    contract['planning_mode'] = 'shot_local_v1'; local_reviews(contract)
    assert check(package, 'interior', selected_upstream=selected)['eligible']
    contract['shots'][1]['upstream'] = [{'shot_id': 'entry'}]
    local_reviews(contract)
    assert not check(package, 'interior', selected_upstream=selected)['eligible']
    contract['story']['payoff'] = 'Changed protected ending'
    assert not check(package)['eligible']


def test_declared_serial_handoff_binds_current_selected_frame(package):
    selected = dependency(package)
    contract = package[0]
    asset = copy.deepcopy(contract["assets"][0])
    asset.update(id="interior-start", **selected["entry"]["outgoing_frame"])
    asset["upstream_source"] = {"shot_id": "entry", "role": "outgoing_frame"}
    asset["review"]["subject_sha256"] = asset["sha256"]
    contract["assets"].append(asset)
    contract["shots"][1]["asset_ids"] = ["interior-start", "end", "patient"]
    refresh(contract)
    assert check(package, "interior", selected_upstream=selected)["eligible"]
    # Identical bytes at an unrelated path must not impersonate the selected frame.
    (package[1] / "unrelated.svg").write_bytes((package[1] / asset["path"]).read_bytes())
    asset["path"] = "unrelated.svg"
    refresh(contract)
    assert not check(package, "interior", selected_upstream=selected)["eligible"]
    asset["path"] = selected["entry"]["outgoing_frame"]["path"]
    contract["shots"][1]["upstream"] = []
    refresh(contract)
    assert not check(package, "interior", selected_upstream=selected)["eligible"]


def test_pending_future_handoff_does_not_fabricate_evidence_or_block_entry(package):
    contract,root = package
    future = copy.deepcopy(contract['shots'][0])
    future.update(id='interior',upstream=[{'shot_id':'entry'}],asset_ids=['pending-start','end','patient'])
    del future['review']
    contract['shots'].append(future)
    contract['assets'].append({'id':'pending-start','role':'start_frame','cast_ids':['patient'],
        'upstream_source':{'shot_id':'entry','role':'outgoing_frame'}})
    digest = contract_digest(contract)
    contract['project_review']['subject_sha256'] = digest
    contract['shots'][0]['review']['subject_sha256'] = digest
    validate_artifact('shot_contract',contract)
    assert check(package)['eligible']
    blocked = check(package,shot='interior')
    assert not blocked['eligible']
    assert any('review' in error or 'unreadable' in error for error in blocked['errors'])


@pytest.mark.parametrize('cut', ['hard_cut','match_cut'])
def test_generic_intentional_cuts_are_valid(package,cut):
    package[0]['shots'][0]['transition']={'type':cut,'rationale':'Deliberate temporal coverage change.'}
    refresh(package[0])
    assert check(package)['eligible']


def reference_free_contract():
    """Synthetic textual intent only; no image or provider quality evidence."""
    contract = json.loads((FIXTURES / 'valid_shot_contract.json').read_text())
    contract.update(reference_mode='reference_free', assets=[], late_cast_ids=[], payoff_speaker_ids=[])
    del contract['payoff_asset_id']
    shot = contract['shots'][0]
    shot.update(initial_state='A plain red cube rests above a flat surface.',
                dominant_action='The cube drops onto the surface.',
                completed_end_state='The cube rests motionless on the surface.',
                duration_seconds=1, cast_ids=[], required_visible_speakers=[], dialogue=[],
                asset_ids=[], upstream=[], prop_body_invariants=['The cube remains red.'],
                allowed_transformations=[], transition={'type':'hard_cut','rationale':'A single completed action.'})
    contract['shots'] = [shot]
    contract['story'] = dict(desire='The cube is above the surface.', action=shot['dominant_action'],
                             consequence=shot['completed_end_state'], payoff='The cube comes to rest.')
    for review in (contract['project_review'], shot['review']):
        review['reviewer'] = 'synthetic textual fixture reviewer'
        for predicate in review['predicates']:
            predicate['evidence'] = 'Synthetic textual contract only; no footage reviewed.'
    refresh(contract)
    return contract


def test_explicit_reference_free_textual_contract_needs_no_boards(tmp_path):
    contract = reference_free_contract()
    result = validate_shot_contract(contract, project_dir=tmp_path, shot_id='entry')
    assert result['eligible'], result
    assert contract['assets'] == [] and not list(tmp_path.iterdir())


def test_untagged_empty_reference_contract_keeps_legacy_requirement(tmp_path):
    contract = reference_free_contract()
    del contract['reference_mode']
    refresh(contract)
    assert not validate_shot_contract(contract, project_dir=tmp_path, shot_id='entry')['eligible']


@pytest.mark.parametrize('field,value', [
    ('cast_ids',['actor']), ('required_visible_speakers',['actor']),
    ('dialogue',[{'speaker_id':'actor','source':'visible','text':'Hello','start_seconds':0,'end_seconds':0.5}]),
    ('asset_ids',['start']), ('upstream',[{'shot_id':'other'}])])
def test_reference_free_rejects_shot_reference_obligations(tmp_path, field, value):
    contract = reference_free_contract()
    contract['shots'][0][field] = value
    refresh(contract)
    assert not validate_shot_contract(contract, project_dir=tmp_path, shot_id='entry')['eligible']


@pytest.mark.parametrize('field,value', [('late_cast_ids',['actor']), ('payoff_speaker_ids',['actor']),
                                        ('payoff_asset_id','payoff')])
def test_reference_free_rejects_global_reference_obligations(tmp_path, field, value):
    contract = reference_free_contract()
    contract[field] = value
    refresh(contract)
    assert not validate_shot_contract(contract, project_dir=tmp_path, shot_id='entry')['eligible']


def test_reference_free_rejects_identity_asset_and_missing_story_review(tmp_path):
    contract = reference_free_contract()
    contract['assets'] = [copy.deepcopy(json.loads((FIXTURES/'valid_shot_contract.json').read_text())['assets'][2])]
    refresh(contract)
    assert not validate_shot_contract(contract, project_dir=tmp_path, shot_id='entry')['eligible']
    contract = reference_free_contract()
    contract['shots'][0]['review']['predicates'] = [p for p in contract['shots'][0]['review']['predicates'] if p['name'] != 'completed_action']
    result = validate_shot_contract(contract, project_dir=tmp_path, shot_id='entry')
    assert not result['eligible'] and 'completed_action' in ' '.join(result['errors'])


def frame_conditioned_fixture(package, *, payoff=False):
    """Synthetic retained role observations; never production evidence."""
    from lib import production_boards as boards
    from lib.shot_contract import project_review_subject, shot_review_subject
    contract, root = package
    contract['planning_mode'] = 'shot_local_v1'
    contract['reference_mode'] = 'frame_conditioned'
    shot = contract['shots'][0]
    shot['frame_input'] = {'asset_id': 'payoff' if payoff else 'start', 'role': 'start_frame'}
    if payoff:
        contract['payoff_speaker_ids'] = list(shot['cast_ids'])
        shot['asset_ids'] = ['payoff']
    else:
        shot['asset_ids'] = ['start']  # No authored ending board for this consumer.
    asset = next(a for a in contract['assets'] if a['id'] == shot['frame_input']['asset_id'])
    asset['cast_ids'] = list(shot['cast_ids'])
    asset['review']['predicates'].append({'name': 'cast_count', 'status': 'pass',
        'evidence': 'SYNTHETIC exact declared composition.'})
    story = root / 'story.txt'; story.write_text('SYNTHETIC retained story')
    approval = root / 'approval.txt'; approval.write_text('SYNTHETIC authority')
    spec = {'story': {'path': 'story.txt', 'sha256': file_sha256(story)},
        'approval': {'path': 'approval.txt', 'sha256': file_sha256(approval)},
        'image_nodes': [], 'shots': [{'shot_id': shot['id'], 'roles': [
            {'asset_id': asset['id'], 'role': 'start_frame'},
            *([{'asset_id': asset['id'], 'role': 'payoff_board'}] if payoff else [])]}]}
    pack = {'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'specification': spec, 'specification_sha256': boards._digest(spec), 'prompt_pack': {},
        'board_bindings': {asset['id']: {'path': asset['path'], 'sha256': asset['sha256']}},
        'observations': [{'asset_id': asset['id'], 'roles': ['start_frame', asset['role']],
            'review': copy.deepcopy(asset['review'])}]}
    (root / 'artifacts').mkdir(exist_ok=True)
    (root / boards.ARTIFACT).write_text(json.dumps(pack))
    (root / 'project.json').write_text(json.dumps({'project_id': contract['project_id'],
        'story_revision': contract['story_revision']}))
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    return pack


@pytest.mark.parametrize('payoff', [False, True])
def test_frame_conditioned_sparse_start_or_same_payoff_is_eligible(package, payoff):
    frame_conditioned_fixture(package, payoff=payoff)
    assert check(package)['eligible'], check(package)
    if payoff:
        assert next(a for a in package[0]['assets'] if a['id'] == 'payoff')['role'] == 'payoff_board'
        assert package[0]['shots'][0]['asset_ids'] == ['payoff']


@pytest.mark.parametrize('defect', ['declaration', 'observation', 'cast', 'cast_count', 'bytes', 'legacy'])
def test_frame_conditioned_requires_exact_declared_observed_composition(package, defect):
    from lib import production_boards as boards
    pack = frame_conditioned_fixture(package, payoff=True)
    if defect == 'declaration':
        pack['specification']['shots'][0]['roles'].pop(0)
        pack['specification_sha256'] = boards._digest(pack['specification'])
    elif defect == 'observation':
        pack['observations'][0]['roles'] = ['payoff_board']
    elif defect == 'cast':
        next(a for a in package[0]['assets'] if a['id'] == 'payoff')['cast_ids'].append('unexpected')
    elif defect == 'cast_count':
        pack['observations'][0]['review']['predicates'] = [p for p in pack['observations'][0]['review']['predicates'] if p['name'] != 'cast_count']
    elif defect == 'bytes':
        (package[1] / pack['board_bindings']['payoff']['path']).write_text('changed')
    else:
        package[0].pop('planning_mode')
    (package[1] / boards.ARTIFACT).write_text(json.dumps(pack))
    assert not check(package)['eligible']
