"""Defect-specific original occurrence continuation through the canonical wrapper.

Synthetic offline journals only; the transport fixture forbids real network.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from lib import production_execution as execution
from lib.production_provenance import validate_attempt_provenance
from lib.shot_contract import file_sha256
from tools._grok_cli_media import GrokCLIContractError
from tests.integration.test_first_pass_workflow import production, read, save  # noqa: F401


@pytest.fixture
def reserved(production, monkeypatch):
    p = production
    inputs = copy.deepcopy(p.inputs['entry'])
    inputs['reference_image_paths'] = []
    inputs.pop('preferred_provider', None)
    inputs.pop('allowed_providers', None)
    p.scope['requests']['entry'] = execution.planned_request_digest(inputs, project_dir=p.root)
    p.persist_scope()
    def prior_builder(*args, **kwargs):
        raise GrokCLIContractError('invalid_argument',
            'reference_to_video requires between 1 and 14 local image path(s)')
    with monkeypatch.context() as old:
        old.setattr('tools.video.grok_cli_video.build_native_video_request', prior_builder)
        result = p.cli.execute(inputs)
    assert not result.success and not p.transport.native_requests
    aid = result.data['production_attempt_id']
    directory = p.root / 'production_attempts' / aid
    request = read(directory / 'request.json')
    frozen = {name: (directory / name).read_bytes() for name in execution._LOCAL_CONTINUATION_FILES}
    return p, aid, directory, request, frozen


def continue_attempt(reserved, **kwargs):
    p, aid, _, request, _ = reserved
    return execution.continue_local_grok_attempt(p.cli, p.root, aid,
        request_sha256=request['request_sha256'], **kwargs)


def test_canonical_same_occurrence_dry_run_dispatch_and_selection_provenance(reserved):
    p, aid, directory, request, frozen = reserved
    counts = len(execution._attempts(p.root))
    scopes = (p.root / 'production_scopes.json').read_bytes()
    dry = continue_attempt(reserved)
    assert dry['provider_calls'] == dry['reservations'] == 0
    assert not (directory / 'local_continuation_claim.json').exists()
    result = continue_attempt(reserved, dry_run=False)
    assert result.success, result.error
    assert result.data['production_attempt_id'] == aid
    assert result.data['production_request_sha256'] == request['request_sha256']
    assert len(p.transport.native_requests) == 1
    assert p.transport.native_requests[0]['session_id'] == aid
    assert 'images' not in p.transport.native_requests[0]['arguments']
    assert len(execution._attempts(p.root)) == counts
    assert (p.root / 'production_scopes.json').read_bytes() == scopes
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw
    selected = p.select('entry', result)
    validate_attempt_provenance(p.root, aid, shot_id='entry', story_revision=p.story['story_revision'],
                               expected_output=selected['output'])
    with pytest.raises(execution.ProductionGovernanceError, match='already claimed'):
        continue_attempt(reserved, dry_run=False)
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize('change', ['uncertain', 'dispatched', 'cli_version', 'conditioning', 'session',
                                  'scope', 'input_bytes', 'original_bytes', 'plan', 'output',
                                  'prior_claim', 'provider_begin', 'failed_evidence', 'request_digest'])
def test_local_continuation_refuses_nonlocal_or_changed_authority_without_calls(reserved, change):
    p, aid, directory, request, _ = reserved
    if change in {'uncertain', 'dispatched', 'cli_version', 'conditioning'}:
        state = read(directory / 'result.json'); raw = read(directory / 'raw_result.json')
        if change == 'uncertain': state['status'] = 'uncertain'
        elif change == 'dispatched': raw['data']['dispatch_status'] = 'failed'
        elif change == 'cli_version': raw['data']['cli_version'] = '1.0.34'
        else: raw['data']['conditioning_receipt'] = {'submission_evidence': 'unconfirmed'}
        state['result'] = raw
        for name, value in [('result.json', state), ('raw_result.json', raw)]:
            (directory / name).chmod(0o644); save(directory / name, value)
    elif change == 'session':
        cwd = str(Path(request['submitted_inputs']['output_path']).parent.resolve())
        (Path(p.cli._sessions_root) / quote(cwd, safe='') / aid).mkdir(parents=True)
    elif change == 'scope':
        p.scope['approved_by'] = 'changed approver'; p.persist_scope()
    elif change in {'input_bytes', 'original_bytes'}:
        binding = request['input_assets'][0]
        path = Path(binding['path' if change == 'input_bytes' else 'original_path'])
        path.chmod(0o644); path.write_bytes(b'changed input')
    elif change == 'plan':
        p.contract['shots'][0]['dominant_action'] += ' unapproved'; p.persist_contract()
    elif change == 'output': Path(request['submitted_inputs']['output_path']).write_bytes(b'existing')
    elif change == 'prior_claim': save(directory / 'local_continuation_claim.json', {})
    elif change == 'provider_begin': save(directory / 'provider_request.json', {})
    elif change == 'failed_evidence':
        Path(request['approval_evidence']['path']).chmod(0o644)
        Path(request['approval_evidence']['path']).write_bytes(b'changed preserved approval')
    else:
        with pytest.raises(execution.ProductionGovernanceError):
            execution.continue_local_grok_attempt(p.cli, p.root, aid, request_sha256='0' * 64, dry_run=False)
        assert not p.transport.native_requests
        return
    with pytest.raises((execution.ProductionGovernanceError, GrokCLIContractError)):
        continue_attempt(reserved, dry_run=False)
    assert not p.transport.native_requests


def test_same_occurrence_race_has_one_atomic_claim_and_one_native_dispatch(reserved):
    def run():
        try: return continue_attempt(reserved, dry_run=False)
        except execution.ProductionGovernanceError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run(), range(2)))
    assert sum(result is not None and result.success for result in outcomes) == 1
    assert len(reserved[0].transport.native_requests) == 1
    assert len(execution._attempts(reserved[0].root)) == 1


@pytest.mark.parametrize('nested', ['retry', 'switch', 'selector'])
def test_continuation_restores_canonical_context_and_blocks_nested_dispatch(reserved, monkeypatch, nested):
    p, aid, directory, request, _ = reserved
    import tools.video.grok_cli_video as adapter
    media = adapter.execute_grok_cli_media
    observed = []

    def guarded_media(*args, **kwargs):
        active = execution._ACTIVE.get()
        assert active['provider'] == 'grok_cli' and active['provider_called'] is True
        assert active['session_id'] == aid and active['root'] == p.root
        assert active['directory'] == directory
        assert active['submitted_inputs'] == request['submitted_inputs']
        assert active['snapshot_paths'] == {item['path'] for item in request['input_assets']}
        assert active['contract'] == p.contract and active['shot_id'] == 'entry'
        with pytest.raises(execution.ProductionGovernanceError, match='switch providers, retry, or fall back'):
            if nested == 'retry':
                p.cli.execute(copy.deepcopy(request['submitted_inputs']))
            elif nested == 'selector':
                p.selector.execute(copy.deepcopy(p.inputs['entry']))
            else:
                tool = SimpleNamespace(provider='another_provider', name='other_video',
                                       capability='video_generation')
                execution.execute_governed(tool, copy.deepcopy(request['submitted_inputs']),
                                           lambda _: pytest.fail('nested provider implementation invoked'))
        observed.append(nested)
        return media(*args, **kwargs)

    monkeypatch.setattr(adapter, 'execute_grok_cli_media', guarded_media)
    result = continue_attempt(reserved, dry_run=False)
    assert result.success, result.error
    assert observed == [nested]
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1
    assert execution._ACTIVE.get() is None


def test_claimed_uncertain_media_can_only_reconcile_original_session(reserved):
    p, aid, directory, request, frozen = reserved
    p.transport.timeout_next = True
    result = continue_attempt(reserved, dry_run=False)
    assert not result.success
    assert execution.load_attempt_result(p.root, aid)['status'] == 'uncertain'
    with pytest.raises(execution.ProductionGovernanceError, match='already claimed'):
        continue_attempt(reserved, dry_run=False)
    # Replay the synthetic original stream; no second provider invocation.
    from tools._grok_cli_media import _parse_stream
    original = p.transport.original_sessions[aid]
    _parse_stream(original['stream'], tool_name='reference_to_video',
                  expected_arguments=execution._grok_native_arguments(request['submitted_inputs']))
    recovered = copy.deepcopy(result)
    recovered.success, recovered.error = True, None
    recovered.artifacts = [request['submitted_inputs']['output_path']]
    recovered.data['dispatch_status'] = 'completed'
    recovered.data['conditioning_receipt']['dispatch_status'] = 'completed'
    recovered.data['conditioning_receipt']['submission_evidence'] = 'verified_native_call'
    execution.reconcile_attempt(p.root, aid, recovered, request_sha256=request['request_sha256'])
    selected = p.select('entry', recovered)
    validate_attempt_provenance(p.root, aid, shot_id='entry', story_revision=p.story['story_revision'],
                               expected_output=selected['output'])
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


def test_crash_after_claim_never_allows_redispatch(reserved, monkeypatch):
    p, aid, directory, _, frozen = reserved
    def crash(**kwargs):
        raise KeyboardInterrupt('synthetic host interruption after claim')
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media', crash)
    with pytest.raises(KeyboardInterrupt):
        continue_attempt(reserved, dry_run=False)
    assert execution._ACTIVE.get() is None
    assert execution.load_attempt_result(p.root, aid)['status'] == 'uncertain'
    with pytest.raises(execution.ProductionGovernanceError, match='already claimed'):
        continue_attempt(reserved, dry_run=False)
    assert not p.transport.native_requests
    assert len(execution._attempts(p.root)) == 1
    for name, raw in frozen.items(): assert (directory / name).read_bytes() == raw


def test_continuation_provenance_rejects_mutated_original_failure(reserved):
    p, aid, directory, _, _ = reserved
    result = continue_attempt(reserved, dry_run=False)
    assert result.success
    prior = read(directory / 'result.json')
    prior['result']['data']['error_category'] = 'mutated'
    (directory / 'result.json').chmod(0o644)
    save(directory / 'result.json', prior)
    with pytest.raises(execution.ProductionGovernanceError, match='original evidence changed'):
        execution.load_attempt_result(p.root, aid)
    assert len(p.transport.native_requests) == 1


def test_direct_wrapper_continuation_rejects_changed_inputs_without_dispatch(reserved):
    p, aid, _, request, _ = reserved
    inputs = execution._local_grok_inputs(p.root, request)
    inputs['governance'].update(continue_local_attempt_id=aid,
                                continue_local_request_sha256=request['request_sha256'])
    inputs['prompt'] += ' unapproved change'
    with pytest.raises(execution.ProductionGovernanceError, match='inputs differ'):
        p.cli.execute(inputs)
    assert not p.transport.native_requests


def test_continuation_cannot_ignore_an_additional_reserved_occurrence(reserved):
    p, aid, _, request, _ = reserved
    duplicate = p.root / 'production_attempts' / 'another-consumed-occurrence'
    duplicate.mkdir()
    other = copy.deepcopy(request)
    other.update(attempt_id=duplicate.name, cli_session_id=duplicate.name)
    save(duplicate / 'request.json', other)
    save(duplicate / 'result.json', {'status': 'failed'})
    with pytest.raises(execution.ProductionGovernanceError):
        continue_attempt(reserved, dry_run=False)
    assert not p.transport.native_requests
    assert len(execution._attempts(p.root)) == 2


@pytest.mark.parametrize('retain_uncertain_result', [False, True])
def test_original_continuation_recovery_after_result_write_failure(reserved, monkeypatch,
                                                                  retain_uncertain_result):
    p, aid, directory, request, frozen = reserved
    captured = {}
    real_save = execution._save_result

    def lose_return(directory, result=None, error=None, *, prefix=''):
        if prefix == 'local_continuation_' and result is not None:
            captured['result'] = copy.deepcopy(result)
            if retain_uncertain_result:
                real_save(directory, error=OSError('synthetic result-write failure'), prefix=prefix)
            raise OSError('synthetic result-write failure before raw record')
        return real_save(directory, result=result, error=error, prefix=prefix)

    with monkeypatch.context() as late:
        late.setattr(execution, '_save_result', lose_return)
        with pytest.raises(OSError, match='synthetic result-write failure'):
            continue_attempt(reserved, dry_run=False)
    assert len(p.transport.native_requests) == 1
    assert execution.load_attempt_result(p.root, aid)['status'] == 'uncertain'
    assert not (directory / 'local_continuation_raw_result.json').exists()
    claim = (directory / 'local_continuation_claim.json').read_bytes()
    recovered = captured['result']
    execution.reconcile_attempt(p.root, aid, recovered, request_sha256=request['request_sha256'])
    assert execution.load_attempt_result(p.root, aid)['status'] == 'generated'
    recovered.data['production_attempt_id'] = aid
    recovered.data['production_request_sha256'] = request['request_sha256']
    selected = p.select('entry', recovered)
    validate_attempt_provenance(p.root, aid, shot_id='entry', story_revision=p.story['story_revision'],
                               expected_output=selected['output'])
    with pytest.raises(execution.ProductionGovernanceError, match='already claimed'):
        continue_attempt(reserved, dry_run=False)
    for name, raw in frozen.items():
        assert (directory / name).read_bytes() == raw
    assert (directory / 'local_continuation_claim.json').read_bytes() == claim
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


@pytest.fixture(params=["legacy", "phase"])
def missing_cwd_reserved(production, monkeypatch, request):
    """Retain the 0.4.0 journal shape from the exact strict-cwd failure site."""
    p = production
    inputs = copy.deepcopy(p.inputs["entry"])
    inputs.pop("preferred_provider", None)
    inputs.pop("allowed_providers", None)
    inputs.pop("cwd", None)
    inputs["output_path"] = str(p.root / "assets" / "videos" / "entry.mp4")
    p.scope["requests"]["entry"] = execution.planned_request_digest(
        inputs, project_dir=p.root
    )
    p.persist_scope()
    with monkeypatch.context() as old:
        # The old cwd check ran before this existing output-parent preparer.
        old.setattr(
            "tools._grok_cli_media._prepare_output_path", lambda path: Path(path)
        )
        result = p.cli.execute(inputs)
    assert not result.success and not p.transport.native_requests
    aid = result.data["production_attempt_id"]
    directory = p.root / "production_attempts" / aid
    raw = read(directory / "raw_result.json")
    if request.param == "legacy":
        raw["data"].pop("local_setup_evidence", None)
    retained = read(directory / "result.json")
    retained["result"] = raw
    for name, value in [("raw_result.json", raw), ("result.json", retained)]:
        (directory / name).chmod(0o644)
        save(directory / name, value)
    request = read(directory / "request.json")
    frozen = {
        name: (directory / name).read_bytes()
        for name in execution._LOCAL_CONTINUATION_FILES
    }
    return p, aid, directory, request, frozen


def test_missing_output_parent_continues_original_once_without_rewriting_history(
    missing_cwd_reserved,
):
    from lib import episode_production_controls as controls

    p, aid, directory, request, frozen = missing_cwd_reserved
    usage = controls.generation_usage(p.root)
    assert usage["excluded_never_submitted"] == [aid]
    assert usage["total"] == 0
    assert continue_attempt(missing_cwd_reserved)["provider_calls"] == 0
    result = continue_attempt(missing_cwd_reserved, dry_run=False)
    assert result.success, result.error
    assert result.data["production_attempt_id"] == aid
    assert controls.generation_usage(p.root)["total"] == 1
    assert len(execution._attempts(p.root)) == len(p.transport.native_requests) == 1
    selected = p.select("entry", result)
    validate_attempt_provenance(
        p.root,
        aid,
        shot_id="entry",
        story_revision=p.story["story_revision"],
        expected_output=selected["output"],
    )
    for name, original in frozen.items():
        assert (directory / name).read_bytes() == original
    with pytest.raises(execution.ProductionGovernanceError, match="already claimed"):
        continue_attempt(missing_cwd_reserved, dry_run=False)
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "receipt",
        "provider",
        "error_cwd",
        "session",
        "provider_begin",
        "output",
        "after_launch",
        "request_hash",
        "phase",
        "request_output",
        "request_cwd",
        "frozen_hash",
    ],
)
def test_missing_cwd_legacy_proof_rejects_unbound_or_launched_attempts(
    missing_cwd_reserved, change
):
    from lib import episode_production_controls as controls

    p, aid, directory, request, _ = missing_cwd_reserved
    raw = read(directory / "raw_result.json")
    retained = read(directory / "result.json")
    if change == "version":
        raw["data"]["conditioning_receipt"]["adapter_version"] = "unknown"
    elif change == "receipt":
        raw["data"]["conditioning_receipt"]["submitted_arguments"]["duration"] += 1
    elif change == "provider":
        raw["data"]["provider"] = "other"
    elif change == "error_cwd":
        raw["error"] = raw["error"].replace("/videos", "/other")
    elif change == "session":
        (
            Path(p.cli._sessions_root)
            / quote(
                str(Path(request["submitted_inputs"]["output_path"]).parent), safe=""
            )
            / aid
        ).mkdir(parents=True)
    elif change == "provider_begin":
        save(directory / "provider_request.json", {})
    elif change == "output":
        output = Path(request["submitted_inputs"]["output_path"])
        output.parent.mkdir(parents=True)
        output.write_bytes(b"possible original")
    elif change == "phase":
        raw["data"]["local_setup_evidence"] = {
            "version": "1.0",
            "phase": "after_cli_launch",
            "cli_launch_started": True,
            "cwd": str(Path(request["submitted_inputs"]["output_path"]).parent),
            "output_path": request["submitted_inputs"]["output_path"],
        }
    elif change in {"request_output", "request_cwd", "frozen_hash"}:
        tampered = copy.deepcopy(request)
        if change == "request_output":
            tampered["submitted_inputs"]["output_path"] = str(p.root / "other" / "clip.mp4")
        elif change == "request_cwd":
            tampered["submitted_inputs"]["cwd"] = str(p.root / "other")
        else:
            tampered["request_sha256"] = "0" * 64
        (directory / "request.json").chmod(0o644)
        save(directory / "request.json", tampered)
    elif change == "after_launch":
        raw["data"]["dispatch_status"] = "indeterminate"
        raw["data"]["cli_version"] = "1.0.34"
    else:
        with pytest.raises(execution.ProductionGovernanceError):
            execution.continue_local_grok_attempt(
                p.cli, p.root, aid, request_sha256="0" * 64, dry_run=False
            )
        assert not p.transport.native_requests
        return
    retained["result"] = raw
    for name, value in [("raw_result.json", raw), ("result.json", retained)]:
        (directory / name).chmod(0o644)
        save(directory / name, value)
    assert controls.generation_usage(p.root)["total"] == 1
    with pytest.raises(execution.ProductionGovernanceError):
        continue_attempt(missing_cwd_reserved, dry_run=False)
    assert not p.transport.native_requests


def test_same_original_continuation_survives_only_unrelated_plan_and_selection(missing_cwd_reserved):
    from tests.integration.test_first_pass_workflow import sign_planning_reviews

    p, aid, directory, request, frozen = missing_cwd_reserved
    other = next(shot for shot in p.contract["shots"] if shot["id"] != "entry")
    other["prop_body_invariants"] = [
        *other["prop_body_invariants"],
        "Synthetic clarified invariant of another shot only",
    ]
    sign_planning_reviews(p.contract)
    p.persist_contract()
    save(
        p.root / "artifacts" / "selected_attempts.json",
        {"unrelated": {"attempt_id": "offline-unrelated"}},
    )
    assert continue_attempt(missing_cwd_reserved)["provider_calls"] == 0
    result = continue_attempt(missing_cwd_reserved, dry_run=False)
    assert result.success, result.error
    selected = p.select("entry", result)
    validate_attempt_provenance(
        p.root,
        aid,
        shot_id="entry",
        story_revision=p.story["story_revision"],
        expected_output=selected["output"],
    )
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1
    for name, original in frozen.items():
        assert (directory / name).read_bytes() == original
    # Materialize that other shot's own upstream so the ordinary dispatch reaches
    # the stale whole-plan gate rather than failing earlier pending eligibility.
    p.bind_upstream(other["id"])
    # That applicability grants no new ordinary dispatch under the stale scope.
    with pytest.raises(execution.ProductionGovernanceError, match="stale contract"):
        p.cli.execute(p.inputs[other["id"]])
    assert len(p.transport.native_requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "action",
        "dialogue",
        "cast",
        "duration",
        "global",
        "order",
        "story",
        "board",
        "identity",
        "payoff",
        "membership",
        "review",
        "source_bytes",
    ],
)
def test_same_original_applicability_refuses_changed_own_authority(
    missing_cwd_reserved, change
):
    from tests.integration.test_first_pass_workflow import sign_planning_reviews

    p, aid, directory, request, _ = missing_cwd_reserved
    shot = next(s for s in p.contract["shots"] if s["id"] == "entry")
    if change == "action":
        shot["dominant_action"] += " changed"
    elif change == "dialogue":
        shot["dialogue"][0]["text"] += " changed"
    elif change == "cast":
        shot["cast_ids"].append("sen")
    elif change == "duration":
        shot["duration_seconds"] += 1
    elif change == "global":
        p.contract["late_cast_ids"] = []
    elif change == "order":
        p.contract["shots"].reverse()
    elif change == "story":
        p.contract["story"]["creative_brief"] = "changed story"
    elif change in {"board", "identity", "payoff"}:
        asset_id = {
            "board": "entry-start",
            "identity": "mira",
            "payoff": "payoff-board",
        }[change]
        asset = next(a for a in p.contract["assets"] if a["id"] == asset_id)
        asset["cast_ids"] = ["sen"]
    elif change == "membership":
        shot["asset_ids"].remove("entry-end")
    elif change == "source_bytes":
        asset = next(a for a in p.contract["assets"] if a["id"] == "entry-start")
        (p.root / asset["path"]).write_bytes(b"changed current bytes")
    sign_planning_reviews(p.contract)
    if change == "review":
        shot["review"]["status"] = "fail"
        shot["review"]["predicates"][0]["status"] = "fail"
    p.persist_contract()
    with pytest.raises(execution.ProductionGovernanceError):
        continue_attempt(missing_cwd_reserved, dry_run=False)
    assert not p.transport.native_requests
    assert not (directory / "local_continuation_claim.json").exists()


@pytest.fixture
def upstream_missing_cwd_reserved(production, monkeypatch):
    p = production
    first = p.generate("entry")
    assert first.success, first.error
    p.select("entry", first)
    p.bind_upstream("interior")
    inputs = copy.deepcopy(p.inputs["interior"])
    inputs.pop("preferred_provider", None)
    inputs.pop("allowed_providers", None)
    inputs.pop("cwd", None)
    inputs["output_path"] = str(p.root / "assets" / "videos" / "interior.mp4")
    p.scope["requests"]["interior"] = execution.planned_request_digest(
        inputs, project_dir=p.root
    )
    p.persist_scope()
    with monkeypatch.context() as old:
        old.setattr(
            "tools._grok_cli_media._prepare_output_path", lambda path: Path(path)
        )
        result = p.cli.execute(inputs)
    assert not result.success
    aid = result.data["production_attempt_id"]
    directory = p.root / "production_attempts" / aid
    request = read(directory / "request.json")
    frozen = {
        name: (directory / name).read_bytes()
        for name in execution._LOCAL_CONTINUATION_FILES
    }
    return p, aid, directory, request, frozen


@pytest.mark.parametrize("change", ["attempt", "review", "output", "frame"])
def test_same_original_refuses_relevant_upstream_selection_changes(
    upstream_missing_cwd_reserved, change
):
    p, aid, directory, request, _ = upstream_missing_cwd_reserved
    selections = execution.load_selected_attempts(p.root)
    selected = selections["entry"]
    if change == "attempt":
        selected["attempt_id"] = "changed-upstream"
    elif change == "review":
        selected["review"]["review_id"] += "-changed"
    else:
        selected["output" if change == "output" else "outgoing_frame"]["sha256"] = (
            "0" * 64
        )
    save(p.root / "artifacts" / "selected_attempts.json", selections)
    with pytest.raises(execution.ProductionGovernanceError, match="selected upstream"):
        continue_attempt(upstream_missing_cwd_reserved, dry_run=False)
    assert len(p.transport.native_requests) == 1
    assert not (directory / "local_continuation_claim.json").exists()


def test_same_original_preserves_bound_upstream_and_strict_postclaim_current_checks(
    upstream_missing_cwd_reserved,
):
    from tests.integration.test_first_pass_workflow import sign_planning_reviews

    p, aid, directory, request, frozen = upstream_missing_cwd_reserved
    other = next(s for s in p.contract["shots"] if s["id"] == "payoff")
    other["dominant_action"] += " synthetic unrelated clarification"
    sign_planning_reviews(p.contract)
    p.persist_contract()
    result = continue_attempt(upstream_missing_cwd_reserved, dry_run=False)
    assert result.success, result.error
    selected = p.select("interior", result)
    validate_attempt_provenance(
        p.root,
        aid,
        shot_id="interior",
        story_revision=p.story["story_revision"],
        expected_output=selected["output"],
    )
    assert len(p.transport.native_requests) == 2
    own = next(s for s in p.contract["shots"] if s["id"] == "interior")
    own["dominant_action"] += " unapproved own change"
    sign_planning_reviews(p.contract)
    p.persist_contract()
    with pytest.raises(
        execution.ProductionGovernanceError, match="own sources or contract plan"
    ):
        validate_attempt_provenance(
            p.root,
            aid,
            shot_id="interior",
            story_revision=p.story["story_revision"],
            expected_output=selected["output"],
        )
    for name, original in frozen.items():
        assert (directory / name).read_bytes() == original
    assert len(p.transport.native_requests) == 2
