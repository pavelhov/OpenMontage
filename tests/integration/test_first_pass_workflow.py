"""Cross-layer offline proof, with synthetic semantic attestations only.

The actual selector, CLI adapter, execution journal, contracts and final gate run.
Only subprocess/network transport and media probe boundaries are mocked. These
fixtures never establish model quality, visual correctness or real AV review.
"""
from __future__ import annotations

import base64
import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from lib.checkpoint import CheckpointValidationError, init_project, write_checkpoint
from lib.pinned_final_frame import pinned_final_frame_params
from lib.production_execution import (ProductionGovernanceError, approval_plan_digest,
    load_selected_attempts, planned_request_digest, reconcile_attempt)
from lib.production_review import certify_final, validate_final_review
from lib.shot_contract import (ASSET_PREDICATES, PROJECT_PREDICATES, SHOT_PREDICATES,
    UPSTREAM_PREDICATES, contract_digest, file_sha256, review_digest, selection_digest)
from tools.video.grok_cli_video import GrokCLIVideo
from tools.video.video_selector import VideoSelector
from tests.tools.test_grok_cli_media import CLI_HELP
from tests.contracts.test_phase0_contracts import sample_artifact

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'first_pass'
SYNTHETIC = 'Synthetic software-test attestation; no model or real audiovisual review.'
PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ1sAAAAASUVORK5CYII=')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def read(path):
    return json.loads(path.read_text())


def attestation(subject, predicates, revision='courier-story-1'):
    return {'review_id': f'synthetic-{subject[:12]}', 'reviewer': SYNTHETIC,
        'story_revision': revision, 'subject_sha256': subject, 'status': 'pass',
        'predicates': [{'name': name, 'status': 'pass', 'evidence': SYNTHETIC} for name in sorted(predicates)]}


def sign_planning_reviews(contract):
    """Fixture author supplies explicit attestations; production does not infer these."""
    digest = contract_digest(contract)
    contract['project_review'] = attestation(digest, PROJECT_PREDICATES)
    for shot in contract['shots']:
        shot['review'] = attestation(digest, SHOT_PREDICATES)


class NativeTransport:
    """Simulate the CLI executable + ffprobe, checking prelaunch persistence."""
    actual_provider_calls = 0

    def __init__(self, root):
        self.root = root
        self.native_requests = []
        self.timeout_next = False
        self.original_sessions = {}
        self.duration = 8

    def __call__(self, argv, **kwargs):
        if '--version' in argv:
            return subprocess.CompletedProcess(argv, 0, 'grok 1.0.34', '')
        if '--help' in argv:
            return subprocess.CompletedProcess(argv, 0, CLI_HELP, '')
        if '--prompt-file' in argv:
            sid = argv[argv.index('--session-id') + 1]
            directory = self.root / 'production_attempts' / sid
            request_path = directory / 'request.json'
            assert request_path.is_file(), 'reservation must exist before native launch'
            assert request_path.stat().st_mode & 0o222 == 0
            request = read(request_path)
            assert request['cli_session_id'] == sid
            args = json.loads(Path(argv[argv.index('--prompt-file') + 1]).read_text().splitlines()[1])
            self.duration = args['duration']
            self.native_requests.append({'session_id': sid, 'arguments': args, 'request': request})
            artifact = self.root / 'sessions' / quote(str(self.root), safe='') / sid / 'videos' / 'clip.mp4'
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(f'SYNTHETIC NATIVE VIDEO {request["shot_id"]} {sid}'.encode())
            events = [
                {'type':'tool_call', 'toolCallId':'native-1', 'toolName':'reference_to_video', 'rawInput':args},
                {'type':'tool_call_update', 'toolCallId':'native-1', 'status':'completed',
                 'rawOutput':{'type':'ReferenceToVideo', 'path':str(artifact), 'filename':artifact.name}},
                {'type':'end', 'stopReason':'end_turn', 'sessionId':sid},
            ]
            stream = '\n'.join(json.dumps(event) for event in events) + '\n'
            (artifact.parent.parent / 'events.jsonl').write_text(stream)
            self.original_sessions[sid] = {'stream':stream, 'artifact':artifact, 'arguments':args}
            if self.timeout_next:
                self.timeout_next = False
                # Plausible output bytes may exist when the host loses the result.
                Path(request['submitted_inputs']['output_path']).write_bytes(artifact.read_bytes())
                raise subprocess.TimeoutExpired(argv, kwargs.get('timeout', 600), output='')
            return subprocess.CompletedProcess(argv, 0, stream, '')
        if Path(str(argv[0])).name == 'ffprobe':
            return subprocess.CompletedProcess(argv, 0, json.dumps({'streams':[
                {'codec_type':'video', 'codec_name':'h264', 'width':1280, 'height':720,
                 'duration':str(self.duration)}], 'format':{'duration':str(self.duration)}}), '')
        pytest.fail(f'Unexpected subprocess in offline proof: {argv!r}')


class Production:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.story = read(FIXTURES / 'integration/story-package.json')
        self.contract = read(FIXTURES / 'valid_shot_contract.json')
        self.contract.update(project_id=root.name, story_revision=self.story['story_revision'],
            story=self.story['story'], assets=[], shots=[], late_cast_ids=['sen'],
            payoff_speaker_ids=['sen'], payoff_asset_id='payoff-board')
        self.transport = NativeTransport(root)
        self.network = Mock(side_effect=AssertionError('network forbidden in offline proof'))
        monkeypatch.setattr('socket.socket.connect', self.network)
        monkeypatch.setattr('tools._grok_cli_media.subprocess.run', self.transport)
        monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda name: '/offline/' + name)
        monkeypatch.setattr('requests.post', Mock(side_effect=AssertionError('network forbidden')))
        monkeypatch.setattr('requests.get', Mock(side_effect=AssertionError('network forbidden')))
        monkeypatch.setattr('lib.production_review.probe_master', lambda path: {'duration_seconds':23})
        self.cli = GrokCLIVideo(grok_path='grok', sessions_root=str(root / 'sessions'))
        self.selector = VideoSelector()
        monkeypatch.setattr(self.selector, '_providers', lambda: [self.cli])
        self.add_asset('mira', 'identity_reference', ['mira'])
        self.add_asset('sen', 'identity_reference', ['sen'])
        self.add_asset('payoff-board', 'payoff_board', ['mira', 'sen'])
        for planned in self.story['shots']:
            sid = planned['id']
            self.add_asset(sid + '-start', 'start_frame', planned['cast_ids'])
            self.add_asset(sid + '-end', 'end_frame', planned['cast_ids'])
            shot = {key: copy.deepcopy(value) for key, value in planned.items() if key != 'upstream_shot_id'}
            shot.update(endpoint_completion='completed', required_visible_speakers=[line['speaker_id'] for line in shot['dialogue']],
                prop_body_invariants=self.story['invariants'], allowed_transformations=[],
                asset_ids=[sid+'-start', sid+'-end', *shot['cast_ids']],
                upstream=[{'shot_id':planned['upstream_shot_id']}] if planned['upstream_shot_id'] else [])
            self.contract['shots'].append(shot)
        # Endpoint and identity can have equal bytes without losing their roles.
        self.add_asset('entry-mid', 'timed_keyframe', ['mira'], time_seconds=4)
        self.contract['shots'][0]['asset_ids'].append('entry-mid')
        sign_planning_reviews(self.contract)
        self.persist_contract()
        self.plan, self.manifest = self.canonical_boards()
        self.inputs = {shot['id']: self.request(shot) for shot in self.contract['shots']}
        self.write_planning_checkpoints()
        evidence = root / 'artifacts/approval.txt'
        evidence.write_text(SYNTHETIC + '\nApproved first generation only: entry, interior, payoff; locked Grok CLI subscription route.')
        self.scope = {'id':'first-generation', 'status':'approved', 'approved_by':'synthetic user fixture',
            'project_id':root.name, 'story_revision':self.story['story_revision'], 'phase':'first_pass',
            'provider':'grok_cli', 'evidence':{'path':str(evidence), 'sha256':file_sha256(evidence)},
            'approval_plan_sha256':approval_plan_digest(self.contract),
            'requests':{sid:planned_request_digest(inputs, project_dir=root) for sid, inputs in self.inputs.items()},
            'attempts_per_shot':{sid:1 for sid in self.inputs}}
        self.persist_scope()

    def add_asset(self, aid, role, cast, **extra):
        path = self.root / 'assets/images' / (aid + '.png')
        path.write_bytes(PIXEL)
        digest = file_sha256(path)
        self.contract['assets'].append({'id':aid, 'role':role, 'path':str(path.relative_to(self.root)),
            'sha256':digest, 'cast_ids':cast, 'review':attestation(digest, ASSET_PREDICATES), **extra})

    def persist_contract(self):
        save(self.root / 'artifacts/shot_contract.json', self.contract)

    def persist_scope(self):
        save(self.root / 'production_scopes.json', {'version':'1.0', 'scopes':[self.scope]})

    def canonical_boards(self):
        scenes, assets, cards, handoffs = [], [], {}, {}
        cursor = 0
        for shot in self.contract['shots']:
            sid = shot['id']
            scenes.append({'id':sid, 'type':'generated', 'description':shot['dominant_action'],
                           'start_seconds':cursor, 'end_seconds':cursor+shot['duration_seconds']})
            cursor += shot['duration_seconds']
            cards[sid] = {'pinned_final_frame':{'required':True, 'requirement_id':sid+'-endpoint', 'end_state':shot['completed_end_state']}}
            for suffix in ('start','end'):
                aid = sid+'-'+suffix
                assets.append({'id':aid, 'scene_id':sid, 'type':'image', 'path':'assets/images/'+aid+'.png', 'source_tool':'synthetic_fixture'})
            handoffs[sid+'-start'] = {'approved':True, 'scene_id':sid, 'asset_id':sid+'-start',
                'pinned_final_frame':{'requirement_id':sid+'-endpoint', 'asset_id':sid+'-end', 'approved':True}}
        return ({'version':'1.0', 'scenes':scenes, 'metadata':{'visual_development':{'shot_cards':cards}}},
                {'version':'1.0', 'assets':assets, 'metadata':{'motion_handoffs':handoffs}})

    def request(self, shot):
        sid = shot['id']
        params = pinned_final_frame_params(self.plan, self.manifest, scene_id=sid,
            keyframe_asset_id=sid+'-start', provider='grok_cli', project_dir=self.root)
        dialogue = ' '.join(f"{line['speaker_id']} says {line['text']!r} at {line['start_seconds']}–{line['end_seconds']}s." for line in shot['dialogue'])
        prompt = ' '.join([shot['initial_state'], shot['dominant_action'], shot['completed_end_state'],
                           dialogue, *shot['prop_body_invariants']])
        params.update(prompt=prompt,
            duration=shot['duration_seconds'], aspect_ratio='16:9', resolution='720p', voices=['eve'],
            reference_image_paths=[str(self.root/'assets/images'/f'{cast}.png') for cast in shot['cast_ids']],
            output_path=str(self.root/'assets/video'/f'{sid}.mp4'), project_dir=str(self.root),
            governance={'scope_id':'first-generation', 'shot_id':sid}, allow_unknown_cost=True)
        if sid == 'entry':
            params['keyframes'] = [{'image':str(self.root/'assets/images/entry-mid.png'), 'timestamp_s':4}]
        return params

    def write_planning_checkpoints(self):
        research = sample_artifact('research_brief')
        research['topic'] = SYNTHETIC
        proposal = sample_artifact('proposal_packet')
        proposal['production_plan'].update(pipeline='cinematic', render_runtime='ffmpeg')
        for option in proposal['concept_options']:
            option.update(title='Synthetic courier template fixture', target_duration_seconds=23)
        script = {'version':'1.0', 'title':'Synthetic courier fixture', 'total_duration_seconds':23,
            'sections':[{'id':scene['id'], 'text':scene['description'], 'start_seconds':scene['start_seconds'],
                         'end_seconds':scene['end_seconds']} for scene in self.plan['scenes']]}
        for stage, artifact_name, artifact in [('research','research_brief',research), ('proposal','proposal_packet',proposal),
            ('script','script',script), ('scene_plan','scene_plan',self.plan)]:
            write_checkpoint(self.root.parent, self.root.name, stage, 'completed', {artifact_name:artifact}, human_approved=True)
            save(self.root/'artifacts'/f'{artifact_name}.json', artifact)

    def generate(self, sid):
        return self.selector.execute(copy.deepcopy(self.inputs[sid]))

    def select(self, sid, result, *, review_status='pass'):
        from lib.production_execution import record_selection
        aid = result.data['production_attempt_id']
        frame = self.root/'assets/images'/f'{sid}-observed-outgoing.png'
        frame.write_bytes(PIXEL + aid.encode())
        selection = {'attempt_id':aid, 'output':{'path':result.artifacts[0], 'sha256':file_sha256(result.artifacts[0])},
                     'outgoing_frame':{'path':str(frame), 'sha256':file_sha256(frame)}}
        selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES)
        selection['review']['status'] = review_status
        record_selection(self.root, sid, selection)
        return selection

    def bind_upstream(self, sid):
        shot = next(item for item in self.contract['shots'] if item['id'] == sid)
        selected = load_selected_attempts(self.root)
        for binding in shot['upstream']:
            upstream = selected[binding['shot_id']]
            binding.update(attempt_id=upstream['attempt_id'], output_sha256=upstream['output']['sha256'],
                outgoing_frame_sha256=upstream['outgoing_frame']['sha256'], review_sha256=review_digest(upstream['review']))
        sign_planning_reviews(self.contract)
        self.persist_contract()

    def complete_shots(self):
        for shot in self.contract['shots']:
            if shot['upstream']:
                self.bind_upstream(shot['id'])
            result = self.generate(shot['id'])
            assert result.success, result.error
            self.select(shot['id'], result)

    def draft(self):
        selected = load_selected_attempts(self.root)
        manifest = {'version':'1.0', 'assets':[{'id':sid+'-video', 'scene_id':sid, 'type':'video',
            'path':value['output']['path'], 'source_tool':'grok_cli_video'} for sid,value in selected.items()]}
        write_checkpoint(self.root.parent, self.root.name, 'assets', 'completed', {'asset_manifest':manifest}, human_approved=True)
        cuts = {'version':'1.0', 'render_runtime':'ffmpeg', 'cuts':[{'id':shot['id'], 'source':shot['id']+'-video', 'in_seconds':0,
            'out_seconds':shot['duration_seconds']} for shot in self.contract['shots']]}
        write_checkpoint(self.root.parent, self.root.name, 'edit', 'completed', {'edit_decisions':cuts})
        master = self.root/'renders/synthetic-master.mp4'
        master.write_bytes(b'SYNTHETIC ASSEMBLY\n'+b'\n'.join(Path(selected[s['id']]['output']['path']).read_bytes() for s in self.contract['shots']))
        report = {'version':'1.0', 'outputs':[{'path':str(master), 'format':'mp4', 'resolution':'1280x720', 'duration_seconds':23}],
                  'verification_notes':[SYNTHETIC]}
        path = write_checkpoint(self.root.parent, self.root.name, 'compose', 'completed', {'render_report':report})
        assert read(path)['metadata']['release_status'] == 'draft'
        return master, report

    def full_review(self, master):
        selected = load_selected_attempts(self.root)
        scenes, cursor = [], 0
        for shot in self.contract['shots']:
            sid = shot['id']; selection = selected[sid]
            scenes.append({'scene_id':sid, 'attempt_id':selection['attempt_id'], 'output_sha256':selection['output']['sha256'],
                'selection_sha256':selection_digest(selection), 'review_sha256':review_digest(selection['review']),
                'start_seconds':cursor, 'end_seconds':cursor+shot['duration_seconds']})
            cursor += shot['duration_seconds']
        return {'version':'2.0', 'project_id':self.root.name, 'story_revision':self.story['story_revision'],
            'contract_sha256':contract_digest(self.contract), 'output_path':str(master), 'output_sha256':file_sha256(master),
            'duration_seconds':23, 'release_status':'final',
            'reviewer':{'id':'synthetic-fixture-author', 'kind':'agent', 'method':SYNTHETIC, 'reviewed_at':'2026-09-20T00:00:00Z'},
            'dimensions':{name:{'status':'pass','evidence':SYNTHETIC} for name in ('transport','technical','visual','audio','story')},
            'av_review':{'status':'pass','mode':'synchronized_av','watched_full':True,'listened_full':True,
                'start_seconds':0,'end_seconds':23,'evidence':SYNTHETIC}, 'scenes':scenes,
            'predicates':attestation('0'*64, PROJECT_PREDICATES)['predicates']}


@pytest.fixture
def production(tmp_path, monkeypatch):
    root = init_project('offline-courier', title='Synthetic 23s courier proof', pipeline_type='cinematic',
        pipeline_dir=tmp_path, governance='strict', story_revision='courier-story-1')
    result = Production(root, monkeypatch)
    yield result
    assert result.transport.actual_provider_calls == 0
    result.network.assert_not_called()


def test_valid_story_to_mocked_cli_attempts_and_current_final(production):
    p = production
    assert all(set(shot['upstream'][0]) == {'shot_id'} for shot in p.contract['shots'][1:])
    approved_scope_bytes = (p.root/'production_scopes.json').read_bytes()
    p.complete_shots()
    assert (p.root/'production_scopes.json').read_bytes() == approved_scope_bytes
    assert len(p.transport.native_requests) == 3
    first = p.transport.native_requests[0]
    assert {'first_frame','last_frame','images','keyframes','voices'} <= first['arguments'].keys()
    assert 'Please stop ringing.' in first['arguments']['prompt']
    assert 'It needs a nap.' in p.transport.native_requests[-1]['arguments']['prompt']
    journal = p.root/'production_attempts'/first['session_id']
    result = read(journal/'result.json')
    assert result['status'] == 'generated'
    receipt = result['result']['data']['conditioning_receipt']
    roles = [item['role'] for item in receipt['input_assets']]
    assert roles == ['first_frame','last_frame','reference','keyframe']
    assert receipt['media_model'] is None
    assert all(file_sha256(item['path']) == item['sha256'] for item in receipt['input_assets'])
    assert (journal/'request.json').stat().st_mode & 0o222 == 0
    assert (journal/'result.json').stat().st_mode & 0o222 == 0
    assert len(list((p.root/'production_selections').glob('*.json'))) == 3
    master, report = p.draft()
    review = p.full_review(master)
    checked = validate_final_review(p.root, review)
    assert checked['eligible'], checked['errors']
    certify_final(p.root, review)
    path = write_checkpoint(p.root.parent, p.root.name, 'compose', 'completed', {'render_report':report,'final_review':review})
    assert read(path)['metadata']['release_status'] == 'final'


@pytest.mark.parametrize('bad_asset', ['sen', 'payoff-board'])
@pytest.mark.parametrize('remaining_budget', [0, 25])
def test_rejected_payoff_or_doctor_blocks_before_first_motion(production, bad_asset, remaining_budget):
    p = production
    asset = next(item for item in p.contract['assets'] if item['id'] == bad_asset)
    asset['review']['status'] = 'fail'
    for predicate in asset['review']['predicates']:
        if predicate['name'] == 'cast_identity':
            predicate.update(status='fail', evidence='Synthetic negative: incorrect payoff doctor identity.')
    p.persist_contract()
    p.scope['remaining_budget_usd'] = remaining_budget
    if remaining_budget == 0:
        p.scope['attempts_per_shot']['entry'] = 0
    p.persist_scope()
    with pytest.raises(ProductionGovernanceError, match=f'assets.{bad_asset}.review'):
        p.generate('entry')
    assert not p.transport.native_requests
    assert not list((p.root/'production_attempts').glob('*/request.json'))


def test_required_endpoint_missing_blocks_before_native_dispatch(production):
    p = production
    del p.inputs['entry']['last_image_path']
    # Explicitly approved malformed request still cannot bypass the endpoint gate.
    p.scope['requests']['entry'] = planned_request_digest(p.inputs['entry'], project_dir=p.root)
    p.persist_scope()
    with pytest.raises(ProductionGovernanceError, match='end_frame|endpoint'):
        p.generate('entry')
    assert not p.transport.native_requests


@pytest.mark.parametrize('entry_review', ['unreviewed', 'failed'])
def test_incomplete_entry_blocks_interior_without_another_motion_call(production, entry_review):
    from lib.production_execution import record_rejection
    p = production
    result = p.generate('entry')
    assert result.success, result.error
    if entry_review == 'failed':
        rejection = attestation(file_sha256(result.artifacts[0]), UPSTREAM_PREDICATES)
        rejection['status'] = 'fail'
        next(item for item in rejection['predicates'] if item['name']=='completed_action').update(
            status='fail', evidence='Synthetic negative: torso remains outside doorway; entry never completes.')
        record_rejection(p.root, result.data['production_attempt_id'], rejection)
    with pytest.raises(ProductionGovernanceError, match='upstream|pending|evidence'):
        p.generate('interior')
    assert len(p.transport.native_requests) == 1
    assert 'interior' not in load_selected_attempts(p.root)


def test_replacing_upstream_requires_fresh_dependent_review(production):
    p = production
    first = p.generate('entry'); assert first.success, first.error
    p.select('entry', first)
    p.bind_upstream('interior')
    original_binding = copy.deepcopy(p.contract['shots'][1]['upstream'])
    # A separate exact repair scope grants the fixture's single replacement only.
    repair = copy.deepcopy(p.scope)
    repair.update(id='entry-repair', phase='repair', replaces_attempt_ids=[first.data['production_attempt_id']])
    repair_inputs = copy.deepcopy(p.inputs['entry'])
    repair_inputs['governance']['scope_id'] = 'entry-repair'
    repair_inputs['output_path'] = str(p.root/'assets/video/entry-repair.mp4')
    repair['requests'] = {'entry':planned_request_digest(repair_inputs, project_dir=p.root)}
    repair['attempts_per_shot'] = {'entry':1}
    save(p.root/'production_scopes.json', {'version':'1.0','scopes':[p.scope,repair]})
    second = p.selector.execute(repair_inputs); assert second.success, second.error
    p.select('entry', second)
    assert p.contract['shots'][1]['upstream'] == original_binding
    with pytest.raises(ProductionGovernanceError, match='selected attempt changed|selected hash changed|selected review changed'):
        p.generate('interior')
    assert len(p.transport.native_requests) == 2
    p.bind_upstream('interior')
    interior = p.generate('interior')
    assert interior.success, interior.error
    assert len(p.transport.native_requests) == 3


def test_timeout_file_cannot_be_reused_but_original_session_can_reconcile(production):
    from lib.production_execution import load_attempt_result
    from tools._grok_cli_media import _parse_stream
    p = production
    p.transport.timeout_next = True
    result = p.generate('entry')
    assert not result.success and result.data['dispatch_status'] == 'indeterminate'
    aid = result.data['production_attempt_id']
    request = read(p.root/'production_attempts'/aid/'request.json')
    output = Path(request['submitted_inputs']['output_path'])
    assert output.is_file() and output.stat().st_size > 0
    assert load_attempt_result(p.root, aid)['status'] == 'uncertain'
    with pytest.raises(ProductionGovernanceError, match='reconcile|uncertain|output already exists'):
        p.generate('entry')
    assert len(p.transport.native_requests) == 1
    # Recover the already recorded original native transcript through the real
    # adapter parser; never invoke another CLI process or infer success by size.
    session = p.transport.original_sessions[aid]
    artifact, _, sid = _parse_stream(session['stream'], tool_name='reference_to_video', expected_arguments=session['arguments'])
    assert sid == aid and file_sha256(artifact) == file_sha256(output)
    recovered = copy.deepcopy(result)
    recovered.success, recovered.error, recovered.artifacts = True, None, [str(output)]
    recovered.data['conditioning_receipt']['submission_evidence'] = 'verified_native_call'
    recovered.data['conditioning_receipt']['dispatch_status'] = 'completed'
    recovered.data['dispatch_status'] = 'completed'
    wrong = copy.deepcopy(recovered); wrong.data['session_id'] = 'different-original-session'
    with pytest.raises(ProductionGovernanceError, match='session'):
        reconcile_attempt(p.root, aid, wrong, request_sha256=request['request_sha256'])
    original_result_bytes = (p.root/'production_attempts'/aid/'result.json').read_bytes()
    state = reconcile_attempt(p.root, aid, recovered, request_sha256=request['request_sha256'])
    assert state['status'] == 'generated'
    assert (p.root/'production_attempts'/aid/'result.json').read_bytes() == original_result_bytes
    p.select('entry', recovered)
    p.bind_upstream('interior')
    assert p.generate('interior').success
    assert len(p.transport.native_requests) == 2


@pytest.mark.parametrize('missing', ['full_av', 'listen', 'story'])
def test_technical_pass_and_many_review_rounds_never_certify_without_full_av(production, missing):
    p = production
    p.complete_shots()
    master, report = p.draft()
    review = p.full_review(master)
    if missing == 'full_av':
        del review['av_review']
    elif missing == 'listen':
        review['av_review']['listened_full'] = False
    else:
        review['dimensions']['story']['status'] = 'unknown'
    for round_number in (1, 3, 100):
        checked = validate_final_review(p.root, review)
        assert not checked['eligible'] and checked['release_status'] == 'draft'
        with pytest.raises(CheckpointValidationError, match='FINAL CERTIFICATION|schema validation'):
            write_checkpoint(p.root.parent, p.root.name, 'compose', 'completed',
                {'render_report':report,'final_review':review}, metadata={'review_round':round_number, 'remaining_budget':0})
    assert read(p.root/'checkpoint_compose.json')['metadata']['release_status'] == 'draft'
    assert len(p.transport.native_requests) == 3


@pytest.mark.parametrize('changed', ['master', 'upstream_frame'])
def test_changed_bytes_invalidate_dependent_selection_and_final(production, changed):
    from lib.shot_contract import validate_shot_contract
    p = production
    p.complete_shots()
    master, report = p.draft()
    review = p.full_review(master)
    assert validate_final_review(p.root, review)['eligible']
    certify_final(p.root, review)
    selected = load_selected_attempts(p.root)
    if changed == 'master':
        master.write_bytes(master.read_bytes()+b'changed master')
    else:
        frame = Path(selected['entry']['outgoing_frame']['path'])
        frame.write_bytes(frame.read_bytes()+b'changed upstream')
        readiness = validate_shot_contract(p.contract, project_dir=p.root, shot_id='interior',
            story_revision=p.story['story_revision'], selected_upstream=selected)
        assert not readiness['eligible'] and any('bytes changed' in error for error in readiness['errors'])
    assert not validate_final_review(p.root, review)['eligible']
    with pytest.raises(CheckpointValidationError, match='FINAL CERTIFICATION'):
        write_checkpoint(p.root.parent, p.root.name, 'compose', 'completed', {'render_report':report,'final_review':review})
    assert len(p.transport.native_requests) == 3


def test_planned_dynamic_outgoing_frame_is_bound_after_selection_without_new_approval(production):
    """Same-location technical variant: an observed end becomes the next start.

    The main template test uses honest hard cuts with separate interior boards.
    This additional variant isolates the approved deferred-input mechanism.
    """
    p = production
    shot = p.contract['shots'][1]
    shot.update(initial_state='Same exterior doorway, empty after completed entry.',
        dominant_action='The empty paper doorway settles shut.',
        completed_end_state='The doorway is fully closed; no person or parcel remains outside.',
        cast_ids=[], required_visible_speakers=[], dialogue=[],
        asset_ids=['interior-start','interior-end'],
        transition={'type':'continuous','rationale':'Same-location continuation uses the selected observed outgoing frame.'})
    start = next(item for item in p.contract['assets'] if item['id']=='interior-start')
    start['upstream_source'] = {'shot_id':'entry','role':'outgoing_frame'}
    start['cast_ids'] = []
    for key in ('path','sha256','review'):
        start.pop(key)
    next(item for item in p.contract['assets'] if item['id']=='interior-end')['cast_ids'] = []
    sign_planning_reviews(p.contract)
    p.persist_contract()
    p.inputs['interior']['prompt'] = 'Same-location continuation: empty paper doorway settles shut.'
    del p.inputs['interior']['reference_image_paths']
    del p.inputs['interior']['voices']
    template = copy.deepcopy(p.inputs['interior'])
    template['reference_image_path'] = {'$upstream':{'shot_id':'entry','role':'outgoing_frame'}}
    from lib.production_execution import planned_request_template
    p.scope['requests']['interior'] = planned_request_template(template, project_dir=p.root)
    p.scope['approval_plan_sha256'] = approval_plan_digest(p.contract)
    p.persist_scope()
    approval_before = (p.root/'production_scopes.json').read_bytes()
    assert 'path' not in start and set(shot['upstream'][0]) == {'shot_id'}
    first = p.generate('entry'); assert first.success, first.error
    selected = p.select('entry', first)
    p.inputs['interior']['reference_image_path'] = selected['outgoing_frame']['path']
    start.update(path=selected['outgoing_frame']['path'], sha256=selected['outgoing_frame']['sha256'],
                 review=attestation(selected['outgoing_frame']['sha256'], ASSET_PREDICATES))
    p.bind_upstream('interior')
    assert approval_plan_digest(p.contract) == p.scope['approval_plan_sha256']
    second = p.generate('interior'); assert second.success, second.error
    assert (p.root/'production_scopes.json').read_bytes() == approval_before
    submitted = p.transport.native_requests[-1]['arguments']
    assert file_sha256(submitted['first_frame']) == selected['outgoing_frame']['sha256']
    assert len(p.transport.native_requests) == 2


@pytest.mark.parametrize('mode,source_shape',[('generate',None),('edit','image_path'),('edit','image_paths')])
def test_interrupted_native_image_recovery_uses_real_adapter_receipt(production,monkeypatch,mode,source_shape):
    """Replay only adapter normalization against a cached synthetic native return.

    The first adapter invocation records the request then loses control before
    returning a receipt. Recovery does not launch another native operation.
    """
    from tools.graphics.grok_cli_image import GrokCLIImage
    from tools.base_tool import ToolResult
    p=production
    inputs={'project_dir':str(p.root),'prompt':'Approved image prompt\n',
        'generation_mode':mode,'operation':'generate','allow_unknown_cost':True,
        'output_path':str(p.root/'assets/images/recovery.png'),
        'governance':{'scope_id':'image-approval','shot_id':'image-board'}}
    if source_shape:
        # Native image validator accepts a scalar image_paths as well as a list.
        inputs[source_shape]=str(p.root/'assets/images/mira.png')
    scope=copy.deepcopy(p.scope)
    scope.update(id='image-approval',phase='image',requests={'image-board':planned_request_digest(inputs,project_dir=p.root)},attempts_per_shot={'image-board':1})
    save(p.root/'production_scopes.json',{'version':'1.0','scopes':[scope]})
    cached={};native_calls=[]
    def interrupted_native(**kwargs):
        native_calls.append(kwargs)
        Path(kwargs['output_path']).write_bytes(PIXEL)
        cached['result']=ToolResult(success=True,artifacts=[kwargs['output_path']],data={
            'session_id':kwargs['session_id'],'dispatch_status':'completed',
            'submission_evidence':'verified_native_call','cli_version':'grok 1.0.34'})
        raise KeyboardInterrupt('lost adapter return after synthetic original completion')
    monkeypatch.setattr('tools.graphics.grok_cli_image.execute_grok_cli_media',interrupted_native)
    tool=GrokCLIImage()
    with pytest.raises(KeyboardInterrupt): tool.execute(inputs)
    assert len(native_calls)==1
    aid=native_calls[0]['session_id']
    request=read(p.root/'production_attempts'/aid/'request.json')
    # Rebuild the real adapter receipt around the cached original outcome; this
    # bypasses dispatch and the provider boundary returns only the cached object.
    monkeypatch.setattr('tools.graphics.grok_cli_image.execute_grok_cli_media',lambda **kwargs:copy.deepcopy(cached['result']))
    recovered=GrokCLIImage.execute.__wrapped__.__wrapped__(tool,request['submitted_inputs'])
    assert recovered.success,recovered.error
    receipt=recovered.data['conditioning_receipt']
    assert receipt['submitted_arguments']==native_calls[0]['arguments']
    assert receipt['native_tool']==('image_edit' if mode=='edit' else 'image_gen')
    assert receipt['submitted_arguments']['aspect_ratio']=='auto'
    state=reconcile_attempt(p.root,aid,recovered,request_sha256=request['request_sha256'])
    assert state['status']=='generated'
    assert len(native_calls)==1 and not p.transport.native_requests


def test_native_terminal_failure_reconciliation_allows_exact_motion_repair(production):
    p=production
    p.transport.timeout_next=True
    original=p.generate('entry')
    aid=original.data['production_attempt_id']
    request=read(p.root/'production_attempts'/aid/'request.json')
    failed=copy.deepcopy(original)
    failed.error='Synthetic authoritative original native failure'
    failed.data['conditioning_receipt'].update(submission_evidence='verified_native_call',dispatch_status='failed')
    # Partial/indeterminate failure must not clear uncertainty.
    with pytest.raises(ProductionGovernanceError,match='terminal failure'):
        reconcile_attempt(p.root,aid,failed,request_sha256=request['request_sha256'])
    failed.data['dispatch_status']='failed'
    record=reconcile_attempt(p.root,aid,failed,request_sha256=request['request_sha256'])
    assert record['status']=='failed' and record['preserved_output'] is None
    repair=copy.deepcopy(p.scope)
    repair.update(id='entry-repair',phase='repair',replaces_attempt_ids=[aid],attempts_per_shot={'entry':1})
    inputs=copy.deepcopy(p.inputs['entry'])
    inputs['governance']['scope_id']='entry-repair'
    inputs['output_path']=str(p.root/'assets/video/entry-approved-repair.mp4')
    repair['requests']={'entry':planned_request_digest(inputs,project_dir=p.root)}
    save(p.root/'production_scopes.json',{'version':'1.0','scopes':[p.scope,repair]})
    result=p.selector.execute(inputs)
    assert result.success,result.error
    assert len(p.transport.native_requests)==2
    assert read(p.root/'production_attempts'/result.data['production_attempt_id']/'request.json')['scope_attempt_index']==0
    with pytest.raises(ProductionGovernanceError,match='exists|exhausted'):
        p.selector.execute(inputs)
    assert len(p.transport.native_requests)==2


def test_raw_native_return_recovers_after_output_snapshot_io_failure(production,monkeypatch):
    from lib import production_execution
    from tools.base_tool import ToolResult
    p=production
    def broken_snapshot(*args):
        raise OSError('synthetic snapshot failure')
    with monkeypatch.context() as patch:
        patch.setattr(production_execution,'_preserve_output',broken_snapshot)
        with pytest.raises(OSError,match='snapshot failure'): p.generate('entry')
    aid=p.transport.native_requests[0]['session_id']
    directory=p.root/'production_attempts'/aid
    request=read(directory/'request.json')
    assert read(directory/'result.json')['status']=='uncertain'
    recovered=ToolResult(**read(directory/'raw_result.json'))
    state=reconcile_attempt(p.root,aid,recovered,request_sha256=request['request_sha256'])
    assert state['status']=='generated'
    recovered.data['production_attempt_id']=aid
    p.select('entry',recovered)
    assert len(p.transport.native_requests)==1


def test_multi_shot_repair_batch_preserves_serial_bindings_and_exact_scope(production):
    p=production
    p.complete_shots()
    selected=load_selected_attempts(p.root)
    repair=copy.deepcopy(p.scope)
    repair.update(id='two-shot-repair',phase='repair',
        replaces_attempt_ids=[selected[sid]['attempt_id'] for sid in ('entry','interior')],
        attempts_per_shot={'entry':1,'interior':1},requests={})
    for sid in ('entry','interior'):
        p.inputs[sid]['governance']['scope_id']=repair['id']
        p.inputs[sid]['output_path']=str(p.root/'assets/video'/f'{sid}-repair.mp4')
        repair['requests'][sid]=planned_request_digest(p.inputs[sid],project_dir=p.root)
    save(p.root/'production_scopes.json',{'version':'1.0','scopes':[p.scope,repair]})
    frozen_scope=(p.root/'production_scopes.json').read_bytes()
    first=p.generate('entry');assert first.success,first.error
    p.select('entry',first)
    with pytest.raises(ProductionGovernanceError,match='selected attempt changed|selected hash changed|selected review changed'):
        p.generate('interior')
    p.bind_upstream('interior')
    second=p.generate('interior');assert second.success,second.error
    p.select('interior',second)
    assert len(p.transport.native_requests)==5
    assert (p.root/'production_scopes.json').read_bytes()==frozen_scope
