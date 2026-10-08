"""U5 offline first-pass E2E: real fake CLIs, real ledger/compiler, real FFmpeg media.

No live provider, network, or paid action. Semantic reviews are fixture-only; technical
duration/hash/frame evidence is real (ffmpeg/ffprobe on generated AV fixtures).
"""
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from lib import openart_dispatch as dispatch, openart_jobs as jobs
from lib import openart_download as download
from lib import production_execution as execution
from tests.integration.test_openart_dispatch_recovery import governed  # noqa: F401  (fixture)
from tools.video.openart_cli_video import OpenArtCLIVideo

FFMPEG_DIR = '/Users/pavel/homebrew/bin'
pytestmark = pytest.mark.skipif(not (shutil.which('ffmpeg') or Path(FFMPEG_DIR, 'ffmpeg').exists()),
                                reason='real FFmpeg required')


def _ffmpeg():
    return shutil.which('ffmpeg') or str(Path(FFMPEG_DIR, 'ffmpeg'))


def real_av_clip(path, *, seconds=8, color='testsrc', freq=440, size='320x180'):
    """Real H.264/AAC MP4 with picture and audio streams; never fabricated bytes."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', f'{color}=size={size}:rate=24:duration={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration={seconds}',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
                    str(path)], check=True)
    return path


@pytest.fixture
def media_path(monkeypatch):
    if FFMPEG_DIR not in os.environ.get('PATH', '').split(os.pathsep):
        monkeypatch.setenv('PATH', FFMPEG_DIR + os.pathsep + os.environ.get('PATH', ''))


class _LocalBytesSocket:
    """Narrow transport fake: only the pinned HTTPS socket serves local MP4 bytes."""
    def __init__(self, body, sent):
        self.body, self.sent = body, sent
    def sendall(self, data): self.sent.append(bytes(data))
    def makefile(self, mode):
        return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(self.body) + self.body)
    def settimeout(self, timeout): pass
    def close(self): pass


def serve_result(monkeypatch, body):
    sent = []
    monkeypatch.setattr(download, '_resolve_public',
                        lambda host, deadline: (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443)))
    def connect(self):
        assert self.host == 'cdn.openart.test'
        self.sock = _LocalBytesSocket(body, sent)
    monkeypatch.setattr(download._PinnedHTTPSConnection, 'connect', connect)
    return sent


def paid(tmp):
    path = tmp / 'paid'
    return path.read_text().splitlines() if path.exists() else []


def test_openart_original_counted_job_qualifies_collects_real_media_unchanged_profile(governed, monkeypatch, media_path):
    """Actual governed first OpenArt job: result qualification consumes the original counted
    job and collection downloads real AV bytes via the narrow pinned HTTPS fake."""
    root, inputs, profile, tmp = governed
    clip = real_av_clip(tmp / 'openart-source.mp4')
    body = clip.read_bytes()
    result = OpenArtCLIVideo().execute(inputs)
    attempt = result.data['production_attempt_id']
    assert len(paid(tmp)) == 1
    monkeypatch.setenv('FAKE_STATUS', 'done')
    promoted = jobs.promote_result_contract(attempt, json_paths={'url_hosts': ['cdn.openart.test']})
    assert len(paid(tmp)) == 1  # qualification consumed the original job; no extra generation
    assert promoted['level'] == 'full'
    assert promoted['profile_sha256'] == profile['profile_sha256']  # promotion keeps the origin profile SHA
    full = jobs.load_qualification(model='m1', mode='image2video', require='full')
    assert full['profile_sha256'] == profile['profile_sha256']
    request = json.loads((root / 'production_attempts' / attempt / 'request.json').read_text())
    sent = serve_result(monkeypatch, body)
    collected = execution.collect_openart_attempt(root, attempt, request_sha256=request['request_sha256'])
    assert sent, 'download went through the pinned connection fake'
    assert collected['status'] == 'generated', collected
    out = Path(inputs['output_path'])
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(body).hexdigest()
    assert len(paid(tmp)) == 1


# ---------------------------------------------------------------------------
# Mixed two-shot first pass: Grok `entry` (actual registry VideoSelector, singleton
# pin) -> reviewed selection -> OpenArt `interior` resolved from the selected
# outgoing frame, under a separately approved scope + credit authorization. The
# interior attempt is that shot's first original qualification attempt; it is never
# regenerated. Semantic reviews are fixture attestations; media is real FFmpeg.
# ---------------------------------------------------------------------------
import copy
import sys

from lib import openart_credit as credit, openart_setup as setup
from lib import production_request as preparation
from lib.production_review import certify_final, validate_final_review
from lib.shot_contract import (ASSET_PREDICATES, PROJECT_PREDICATES, UPSTREAM_PREDICATES,
    contract_digest, draft_audio_policy_digest, file_sha256, review_digest, selection_digest)
from tests.integration.test_openart_dispatch_recovery import FAKE as FAKE_OPENART
from tests.lib.test_openart_credit import PATHS
from tests.lib.test_production_request import package as compiler_package, write
from tests.lib.test_shot_contract import refresh
from tests.tools.test_grok_cli_media import CLI_HELP
from tools import _openart_cli as cli

FIXTURE_REVIEW = 'Synthetic fixture-only semantic attestation; no model or real audiovisual judgement.'

FAKE_GROK = r'''#!PYTHON
import json, os, shutil, sys
from pathlib import Path
from urllib.parse import quote
argv = sys.argv[1:]
with open(os.environ['FAKE_GROK_LOG'], 'a') as f: f.write(json.dumps(argv) + '\n')
if argv == ['--version']: print('grok 1.0.34'); raise SystemExit(0)
if argv == ['--help']: print(open(os.environ['FAKE_GROK_HELP']).read()); raise SystemExit(0)
sid = argv[argv.index('--session-id') + 1]; cwd = argv[argv.index('--cwd') + 1]
request = Path(os.environ['FAKE_GROK_PROJECT']) / 'production_attempts' / sid / 'request.json'
assert request.is_file() and not (request.stat().st_mode & 0o222), 'prelaunch journal must be sealed first'
args = json.loads(Path(argv[argv.index('--prompt-file') + 1]).read_text().splitlines()[1])
with open(os.environ['FAKE_GROK_PAID'], 'a') as f: f.write(json.dumps({'sid': sid, 'args': args}) + '\n')
artifact = Path(os.environ['GROK_SESSIONS_ROOT']) / quote(cwd, safe='') / sid / 'videos' / 'clip.mp4'
artifact.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(os.environ['FAKE_GROK_CLIP'], artifact)
for event in [{'type': 'tool_call', 'toolCallId': 'n1', 'toolName': 'reference_to_video', 'rawInput': args},
              {'type': 'tool_call_update', 'toolCallId': 'n1', 'status': 'completed',
               'rawOutput': {'type': 'ReferenceToVideo', 'path': str(artifact), 'filename': artifact.name}},
              {'type': 'end', 'stopReason': 'end_turn', 'sessionId': sid}]:
    print(json.dumps(event))
'''


def attest(subject, predicates, revision='story-1', status='pass'):
    return {'review_id': f'fixture-{subject[:12]}', 'reviewer': FIXTURE_REVIEW, 'story_revision': revision,
            'subject_sha256': subject, 'status': status,
            'predicates': [{'name': n, 'status': status, 'evidence': FIXTURE_REVIEW} for n in sorted(predicates)]}


def read_json(path):
    return json.loads(Path(path).read_text())


class Mixed:
    """Actual project chain; every step calls the production module that owns it."""

    def __init__(self, tmp, monkeypatch):
        self.tmp, self.mp = tmp, monkeypatch
        original_run, original_profile = cli._run_checked, jobs.load_qualification
        root, inputs, native, profile, compiled, review = compiler_package.__wrapped__(tmp, monkeypatch)
        monkeypatch.setattr(cli, '_run_checked', original_run)
        monkeypatch.setattr(jobs, 'load_qualification', original_profile)
        self.root, self.template_inputs, self.timing = root, inputs, compiled['timing']
        self.prep_review = review
        # Real fake CLIs (actual subprocesses), real ffmpeg media.
        openart = tmp / 'fake-openart'; openart.write_text(FAKE_OPENART.replace('PYTHON', sys.executable)); openart.chmod(0o755)
        grok = tmp / 'fake-grok'; grok.write_text(FAKE_GROK.replace('PYTHON', sys.executable)); grok.chmod(0o755)
        (tmp / 'grok-help.txt').write_text(CLI_HELP)
        self.grok_clip = real_av_clip(tmp / 'grok-source.mp4', size='640x360', color='testsrc', freq=440)
        self.openart_clip = real_av_clip(tmp / 'openart-source.mp4', size='640x360', color='testsrc2', freq=660)
        for key, value in {'OPENART_CLI_PATH': openart, 'FAKE_LOG': tmp / 'calls', 'FAKE_SERVER': tmp / 'server',
                           'FAKE_PAID': tmp / 'paid', 'GROK_CLI_PATH': grok, 'GROK_SESSIONS_ROOT': tmp / 'sessions',
                           'FAKE_GROK_LOG': tmp / 'grok-calls', 'FAKE_GROK_PAID': tmp / 'grok-paid',
                           'FAKE_GROK_HELP': tmp / 'grok-help.txt', 'FAKE_GROK_PROJECT': root,
                           'FAKE_GROK_CLIP': self.grok_clip}.items():
            monkeypatch.setenv(key, str(value))
        (tmp / 'sessions').mkdir()
        monkeypatch.delenv('OPENMONTAGE_OPENART_OFFLINE', raising=False)
        jobs.register_reservation_lookup(None)
        monkeypatch.setattr(jobs, '_UPLOAD_APPROVAL_LOOKUP', preparation.approved_upload_lookup)
        monkeypatch.setattr(jobs, '_ACCOUNT_CHECK', None)
        monkeypatch.setattr(execution, '_OPENART_COMPILED_REQUEST_CHECK', execution._compiled_request_check)
        self.plan_story()
        self.approve_grok_entry()

    # -- planning ----------------------------------------------------------
    def plan_story(self):
        root = self.root
        contract = read_json(root / 'artifacts/shot_contract.json')
        interior = copy.deepcopy(contract['shots'][0])
        interior.update(id='interior', asset_ids=['interior-start', 'end', 'patient'], upstream=[{'shot_id': 'entry'}],
                        transition={'type': 'location_cut', 'rationale': 'Entry completes; the cut lands inside the clinic.'})
        contract['shots'].append(interior)
        # Start board for interior is the reviewed outgoing frame of the selected entry attempt.
        contract['assets'].append({'id': 'interior-start', 'role': 'start_frame', 'cast_ids': ['patient'],
                                   'upstream_source': {'shot_id': 'entry', 'role': 'outgoing_frame'}})
        refresh(contract)
        self.contract = contract
        write(root / 'artifacts/shot_contract.json', contract)
        scene = read_json(root / 'artifacts/scene_plan.json')
        scene['scenes'].append({'id': 'interior', 'type': 'generated', 'description': 'Fixture interior',
                                'start_seconds': 8, 'end_seconds': 16, 'script_section_id': 's2'})
        write(root / 'artifacts/scene_plan.json', scene)
        script = read_json(root / 'artifacts/script.json')
        script['sections'].append({'id': 's2', 'text': 'Help me. Help me.', 'start_seconds': 8, 'end_seconds': 16})
        script['total_duration_seconds'] = 16
        write(root / 'artifacts/script.json', script)
        self.approval_plan = execution.approval_plan_digest(contract)

    def scopes(self):
        return read_json(self.root / 'production_scopes.json')['scopes']

    def save_scopes(self, scopes):
        write(self.root / 'production_scopes.json', {'version': '1.0', 'scopes': scopes})

    def approve_grok_entry(self):
        root = self.root
        start = root / 'assets/start.svg'; patient = root / 'assets/patient.svg'
        (root / 'assets/video').mkdir(parents=True, exist_ok=True)
        self.grok_inputs = {
            'operation': 'reference_to_video', 'prompt': self.template_inputs['prompt'],
            'first_frame': str(start), 'reference_image_paths': [str(patient)], 'voices': ['eve'],
            'duration': 8, 'aspect_ratio': '16:9', 'resolution': '480p',
            'output_path': str(root / 'assets/video/entry.mp4'), 'project_dir': str(root),
            'governance': {'scope_id': 'grok-entry', 'shot_id': 'entry'}, 'allow_unknown_cost': True,
            'preferred_provider': 'grok_cli', 'allowed_providers': ['grok_cli']}
        (root / 'grok-approval.txt').write_text('Fixture approval: one Grok CLI first-pass attempt for entry.')
        grok_scope = {'id': 'grok-entry', 'status': 'approved', 'approved_by': 'fixture author',
                      'project_id': self.contract['project_id'], 'story_revision': self.contract['story_revision'],
                      'phase': 'first_pass', 'provider': 'grok_cli', 'approval_plan_sha256': self.approval_plan,
                      'evidence': {'path': 'grok-approval.txt', 'sha256': file_sha256(root / 'grok-approval.txt')},
                      'requests': {'entry': execution.planned_request_digest(self.grok_inputs, project_dir=root)},
                      'attempts_per_shot': {'entry': 1}}
        openart_scope = self.scopes()[0]
        openart_scope.update(requests={}, attempts_per_shot={'interior': 1}, approval_plan_sha256=self.approval_plan)
        self.save_scopes([grok_scope, openart_scope])

    # -- generation / review -------------------------------------------------
    def generate_entry(self):
        from tools.video.video_selector import VideoSelector
        return VideoSelector().execute(copy.deepcopy(self.grok_inputs))

    def outgoing_frame(self, video, name):
        from tools.analysis.frame_sampler import FrameSampler
        sampled = FrameSampler().execute({'input_path': str(video), 'strategy': 'timestamps', 'timestamps': [7.5],
                                          'output_dir': str(self.root / 'assets/frames' / name), 'format': 'png'})
        assert sampled.success, sampled.error
        return Path(sampled.data['frames'][0]['path'])

    def select(self, shot, attempt, output, *, status='pass'):
        frame = self.outgoing_frame(output, shot)
        selection = {'attempt_id': attempt, 'output': {'path': str(output), 'sha256': file_sha256(output)},
                     'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
        selection['review'] = attest(selection_digest(selection), UPSTREAM_PREDICATES, status=status)
        execution.record_selection(self.root, shot, selection)
        return selection

    def resolve_interior_upstream(self):
        """Bind current selected entry facts; approval_plan_sha256 must not move."""
        selected = execution.load_selected_attempts(self.root)['entry']
        frame = Path(selected['outgoing_frame']['path'])
        asset = next(a for a in self.contract['assets'] if a['id'] == 'interior-start')
        asset.update(path=str(frame.relative_to(self.root)), sha256=selected['outgoing_frame']['sha256'],
                     review=attest(selected['outgoing_frame']['sha256'], ASSET_PREDICATES))
        shot = next(s for s in self.contract['shots'] if s['id'] == 'interior')
        shot['upstream'] = [{'shot_id': 'entry', 'attempt_id': selected['attempt_id'],
                             'output_sha256': selected['output']['sha256'],
                             'outgoing_frame_sha256': selected['outgoing_frame']['sha256'],
                             'review_sha256': review_digest(selected['review'])}]
        refresh(self.contract)
        write(self.root / 'artifacts/shot_contract.json', self.contract)
        assert execution.approval_plan_digest(self.contract) == self.approval_plan
        return frame

    def prepare_openart_interior(self, frame):
        """Real qualification/upload/preview/compiler/quote for the first interior attempt."""
        root = self.root
        setup.inspect_qualification('m1', 'image2video', json_paths=PATHS)
        guarantee = {'argv': ['account'], 'nonspending': {'path': 'upload.free', 'expected': True},
                     'no_delayed_charge': {'path': 'upload.noDelayed', 'expected': True}}
        setup.qualify_upload_guarantee('m1', 'image2video', guarantee=guarantee,
                                       json_paths={'upload_url': 'url'}, url_hosts=['up.openart.test'])
        packet = preparation.source_packet(root, 'interior')
        write(root / 'artifacts/upload_approval-interior-up.json', {
            'version': '1.0', 'upload_id': 'interior-up', 'asset_id': 'interior-start', 'shot_id': 'interior',
            'source_sha256': file_sha256(frame), 'source_binding': packet['binding'],
            'approved_by': 'Synthetic fixture author; not a live approval', 'evidence_path': 'approval.txt',
            'evidence_sha256': file_sha256(root / 'approval.txt')})
        jobs.upload_reference(root, 'interior-up', frame, model='m1', mode='image2video')
        authored = preparation.compile_prompt(root, 'interior')
        inputs = {'project_dir': str(root), 'governance': {'scope_id': 'approved', 'shot_id': 'interior'},
                  'prompt': authored['prompt'], 'model': 'm1', 'mode': 'image2video', 'operation': 'image_to_video',
                  'duration': 8, 'aspect_ratio': '16:9', 'resolution': '720p', 'image_path': str(frame),
                  'image_upload_id': 'interior-up', 'output_path': str(root / 'assets/video/interior.mp4'),
                  'compiled_request_id': 'c2', 'preparation_review_id': 'r2'}
        setup.qualify_preview('m1', 'image2video', prompt=inputs['prompt'], duration=8, aspect_ratio='16:9',
                              resolution='720p', image_upload_id='interior-up')
        profile = jobs.load_qualification(model='m1', mode='image2video', require='pre_submit')
        dry = next(e for e in profile['captured_receipts'] if e['kind'] == 'dry_run')
        inputs.update(native_dry_run_receipt_id=dry['receipt_id'], native_dry_run_receipt_sha256=dry['receipt_sha256'])
        native = jobs.prepare_native_request(execution._openart_controls(inputs), profile)
        timing = copy.deepcopy(self.timing)
        for action in timing['action_windows']:
            action['source_pointer'] = action['source_pointer'].replace('/shots/0/', '/shots/1/')
        compiled = preparation.prepare_compiled_request(inputs, native, profile, coverage=authored['coverage'], timing=timing)
        review = dict(self.prep_review, review_id='r2', subject_sha256=preparation.digest(compiled), evidence_kind='reviewed',
                      reviewer='Synthetic software-test fixture; no real AV review')
        write(root / 'artifacts/compiled_request-c2.json', compiled)
        write(root / 'artifacts/preparation_review-r2.json', review)
        # Separately approved OpenArt scope: dynamic upstream frame, static everything else.
        scopes = self.scopes(); scope = scopes[1]
        template = copy.deepcopy(inputs)
        template['image_path'] = {'$upstream': {'shot_id': 'entry', 'role': 'outgoing_frame'}}
        scope['requests'] = {'interior': execution.planned_request_template(template, project_dir=root)}
        contract = credit.QuoteContract('credits', 'quantum', 'balance', 'id', 'workspace', 'model', 'mode', 'maximum', 'all_settings')
        qcontract = credit.qualify_quote_contract(inputs, profile, contract)
        quote = credit.refresh_credit_evidence(inputs, profile, qualification_sha256=qcontract['qualification_sha256'])
        terms = {'version': '1', 'status': 'approved', 'approved_by': 'Synthetic software-test fixture; no live credit authorization',
                 'project_root': str(root), 'project_id': scope['project_id'], 'story_revision': scope['story_revision'],
                 'scope_id': scope['id'], 'shot_id': 'interior', 'account_id_sha256': profile['account_id_sha256'],
                 'workspace': 'workspace', 'allowance_id': 'budget', 'allowance': '10', 'ceiling': '3', 'count': 1,
                 'purpose': 'result_contract_qualification',
                 'occurrences': [{'id': 'interior-first-original', 'index': 0,
                                  'request_sha256': execution.planned_request_digest(inputs, project_dir=root),
                                  'native_sha256': native['native_body_sha256'], 'profile_sha256': native['profile_sha256'],
                                  'quote_sha256': quote['quote_sha256']}]}
        raw = json.dumps({'kind': 'openart_credit_authorization', 'terms': terms}, sort_keys=True).encode()
        (root / 'credit-approval.json').write_bytes(raw)
        terms['evidence'] = {'path': 'credit-approval.json', 'sha256': hashlib.sha256(raw).hexdigest()}
        scope['credit_authorization_sha256'] = credit.credit_authorization_digest(terms)
        write(root / 'artifacts/credit_authorization-credit.json', terms)
        self.save_scopes(scopes)
        inputs.update(credit_authorization_id='credit', credit_quote_id=quote['quote_id'],
                      credit_qualification_sha256=qcontract['qualification_sha256'])
        return inputs, profile

    # -- compose / certification -------------------------------------------
    def compose(self):
        from tools.video.video_compose import VideoCompose
        selected = execution.load_selected_attempts(self.root)
        master = self.root / 'renders/master.mp4'
        cuts = [{'id': sid, 'source': str(Path(selected[sid]['output']['path']).resolve()), 'in_seconds': 0, 'out_seconds': 8}
                for sid in ('entry', 'interior')]
        composed = VideoCompose().execute({'operation': 'compose', 'output_path': str(master),
            'edit_decisions': {'version': '1.0', 'render_runtime': 'ffmpeg', 'cuts': cuts,
                               'metadata': {'compose_target': {'width': 640, 'height': 360}}}})
        assert composed.success, composed.error
        return master

    def final_review(self, master, *, av=True):
        from lib.production_review import probe_master
        selected = execution.load_selected_attempts(self.root)
        duration = float(probe_master(master)['duration_seconds'])
        scenes, cursor = [], 0
        for sid in ('entry', 'interior'):
            sel = selected[sid]
            scenes.append({'scene_id': sid, 'attempt_id': sel['attempt_id'], 'output_sha256': sel['output']['sha256'],
                           'selection_sha256': selection_digest(sel), 'review_sha256': review_digest(sel['review']),
                           'start_seconds': cursor, 'end_seconds': cursor + 8})
            cursor += 8
        scenes[-1]['end_seconds'] = duration
        review = {'version': '2.0', 'project_id': self.contract['project_id'], 'story_revision': self.contract['story_revision'],
                  'contract_sha256': contract_digest(self.contract), 'output_path': str(master),
                  'output_sha256': file_sha256(master), 'duration_seconds': duration, 'release_status': 'final',
                  'reviewer': {'id': 'fixture-author', 'kind': 'agent', 'method': FIXTURE_REVIEW, 'reviewed_at': '2026-10-06T00:00:00Z'},
                  'dimensions': {n: {'status': 'pass', 'evidence': FIXTURE_REVIEW} for n in ('transport', 'technical', 'visual', 'audio', 'story')},
                  'scenes': scenes, 'predicates': attest('0' * 64, PROJECT_PREDICATES)['predicates']}
        if av:
            review['av_review'] = {'status': 'pass', 'mode': 'synchronized_av', 'watched_full': True, 'listened_full': True,
                                   'start_seconds': 0, 'end_seconds': duration, 'evidence': FIXTURE_REVIEW}
        return review

    def run_entry(self):
        result = self.generate_entry()
        assert result.success, result.error
        attempt = result.data['production_attempt_id']
        return attempt, Path(result.artifacts[0])


def grok_paid(tmp):
    path = tmp / 'grok-paid'
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def mixed(tmp_path, monkeypatch, media_path):
    production = Mixed(tmp_path, monkeypatch)
    yield production
    dispatch._CRASH_HOOK = None
    jobs.register_reservation_lookup(None)


def run_interior(m, monkeypatch):
    """First separately credit-authorized OpenArt original: dispatch, qualify, collect, select."""
    frame = m.resolve_interior_upstream()
    inputs, profile = m.prepare_openart_interior(frame)
    result = OpenArtCLIVideo().execute(inputs)
    # Async submit is not footage: the adapter reports submitted_async with success=False.
    assert not result.success and result.data['dispatch_status'] == 'submitted_async', (result.error, result.data)
    interior_attempt = result.data['production_attempt_id']
    assert len(paid(m.tmp)) == 1
    assert not Path(inputs['output_path']).exists()
    monkeypatch.setenv('FAKE_STATUS', 'done')
    promoted = jobs.promote_result_contract(interior_attempt, json_paths={'url_hosts': ['cdn.openart.test']})
    assert promoted['level'] == 'full' and promoted['profile_sha256'] == profile['profile_sha256']
    request = read_json(m.root / 'production_attempts' / interior_attempt / 'request.json')
    serve_result(monkeypatch, m.openart_clip.read_bytes())
    collected = execution.collect_openart_attempt(m.root, interior_attempt, request_sha256=request['request_sha256'])
    assert collected['status'] == 'generated', collected
    interior_out = Path(inputs['output_path'])
    assert file_sha256(interior_out) == file_sha256(m.openart_clip)
    m.select('interior', interior_attempt, interior_out)
    assert len(paid(m.tmp)) == 1 and len(grok_paid(m.tmp)) == 1  # no hidden qualification/retry jobs
    return interior_attempt


def run_entry_checked(m):
    entry_attempt, entry_out = m.run_entry()
    assert len(grok_paid(m.tmp)) == 1 and not paid(m.tmp)
    native_args = json.loads(grok_paid(m.tmp)[0])['args']
    assert 'Help me.' in native_args['prompt'] and native_args['voices'] == ['eve']
    assert file_sha256(entry_out) == file_sha256(m.grok_clip)
    return entry_attempt, entry_out


def test_mixed_grok_entry_openart_interior_first_pass_certifies_real_master(mixed, monkeypatch):
    m = mixed
    # 1. Grok entry through the actual registry VideoSelector with a singleton pin.
    entry_attempt, entry_out = run_entry_checked(m)
    m.select('entry', entry_attempt, entry_out)
    # 2-3. Upstream resolved from reviewed selection; OpenArt interior first original.
    run_interior(m, monkeypatch)
    # 4. Real VideoCompose master, real probe, full v2 certification.
    master = m.compose()
    review = m.final_review(master)
    checked = validate_final_review(m.root, review)
    assert checked['eligible'], checked['errors']
    certify_final(m.root, review)
    assert read_json(m.root / 'artifacts/final_review.json')['output_sha256'] == file_sha256(master)


def test_mixed_master_without_av_review_stays_uncertified_draft(mixed, monkeypatch):
    m = mixed
    entry_attempt, entry_out = run_entry_checked(m)
    m.select('entry', entry_attempt, entry_out)
    run_interior(m, monkeypatch)
    master = m.compose()
    review = m.final_review(master, av=False)
    checked = validate_final_review(m.root, review)
    assert not checked['eligible'] and checked['release_status'] == 'draft', checked
    with pytest.raises(Exception):
        certify_final(m.root, review)
    assert not (m.root / 'artifacts/final_review.json').exists()


def single_failure(subject, predicate, evidence):
    """Exactly one named predicate fails; every other upstream predicate passes."""
    review = attest(subject, UPSTREAM_PREDICATES)
    review['status'] = 'fail'
    next(x for x in review['predicates'] if x['name'] == predicate).update(status='fail', evidence=evidence)
    assert [x['name'] for x in review['predicates'] if x['status'] == 'fail'] == [predicate]
    return review


# shot.endpoint_completion is attested by the completed_action predicate (UPSTREAM_PREDICATES).
@pytest.mark.parametrize('predicate,evidence', [
    ('speaker_source', 'Synthetic negative: Help me. is voiced off-screen by the wrong speaker.'),
    ('completed_action', 'Synthetic negative: entry ends before the patient crosses the doorway (endpoint incomplete).')])
def test_failed_speaker_or_endpoint_rejects_entry_without_implicit_retry(mixed, predicate, evidence):
    m = mixed
    entry_attempt, entry_out = run_entry_checked(m)
    frame = m.outgoing_frame(entry_out, 'entry')
    selection = {'attempt_id': entry_attempt, 'output': {'path': str(entry_out), 'sha256': file_sha256(entry_out)},
                 'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    selection['review'] = single_failure(selection_digest(selection), predicate, evidence)
    with pytest.raises(execution.ProductionGovernanceError):
        execution.record_selection(m.root, 'entry', selection)
    execution.record_rejection(m.root, entry_attempt, single_failure(file_sha256(entry_out), predicate, evidence))
    assert 'entry' not in execution.load_selected_attempts(m.root)
    # No implicit corrective reroll: the approved single attempt is consumed; no provider call.
    with pytest.raises(execution.ProductionGovernanceError, match='exists|exhausted|reconcile'):
        m.generate_entry()
    assert len(grok_paid(m.tmp)) == 1 and not paid(m.tmp)
    with pytest.raises(Exception):
        m.resolve_interior_upstream()
    assert not paid(m.tmp)


def test_interrupted_interior_keeps_same_original_attempt_and_reservation(mixed):
    """Crash at launch_marker: honest unresolved original, no invented acceptance, no resubmit.

    Positive same-original recovery after job proof is covered by U4
    tests/integration/test_openart_dispatch_recovery.py; this guards the integration boundary.
    """
    m = mixed
    entry_attempt, entry_out = run_entry_checked(m)
    m.select('entry', entry_attempt, entry_out)
    inputs, _ = m.prepare_openart_interior(m.resolve_interior_upstream())
    seen = []
    def crash(stage, attempt):
        if stage == 'launch_marker':
            seen.append(attempt)
            raise RuntimeError('synthetic crash boundary')
    dispatch._CRASH_HOOK = crash
    with pytest.raises(RuntimeError, match='synthetic crash'):
        OpenArtCLIVideo().execute(copy.deepcopy(inputs))
    dispatch._CRASH_HOOK = None
    attempt = seen[0]
    before = dispatch.ledger().inspect(attempt)
    assert before['slot_state'] == 'submitting' and not paid(m.tmp)
    with pytest.raises(Exception):
        OpenArtCLIVideo().execute(copy.deepcopy(inputs))
    assert dispatch.ledger().inspect(attempt) == before
    assert not paid(m.tmp) and len(grok_paid(m.tmp)) == 1
    attempts = sorted(x.name for x in (m.root / 'production_attempts').iterdir())
    assert attempt in attempts and len(attempts) == 2  # entry + the one interior original
    assert not Path(inputs['output_path']).exists()


def test_draft_unknown_speaker_source_never_certifies_or_upgrades(mixed, monkeypatch):
    m = mixed
    evidence = m.root / 'draft-approval.txt'
    evidence.write_text('SYNTHETIC: explicit approval to defer unavailable audio review for draft only.')
    marker = read_json(m.root / 'project.json')
    marker['governance']['draft_review'] = {
        'version': '1.0', 'mode': 'audio_unavailable_draft', 'project_id': marker['project_id'],
        'story_revision': marker['story_revision'], 'evidence': {'path': evidence.name, 'sha256': file_sha256(evidence)}}
    write(m.root / 'project.json', marker)
    policy = draft_audio_policy_digest(m.root)
    entry_attempt, entry_out = run_entry_checked(m)
    frame = m.outgoing_frame(entry_out, 'entry')
    selection = {'attempt_id': entry_attempt, 'output': {'path': str(entry_out), 'sha256': file_sha256(entry_out)},
                 'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    selection['review'] = attest(selection_digest(selection), UPSTREAM_PREDICATES)
    selection['review'].update(status='provisional', draft_policy_sha256=policy)
    next(x for x in selection['review']['predicates'] if x['name'] == 'speaker_source').update(
        status='unknown', evidence='SYNTHETIC unavailable auditory review; not a pass.')
    execution.record_selection(m.root, 'entry', selection)
    run_interior(m, monkeypatch)
    master = m.compose()
    review = m.final_review(master)  # even a claimed full AV pass cannot upgrade unknown speaker_source
    checked = validate_final_review(m.root, review)
    assert not checked['eligible'] and checked['release_status'] == 'draft', checked
    assert any('selection review' in x or 'speaker_source' in x for x in checked['errors'])
    with pytest.raises(Exception):
        certify_final(m.root, review)
    stored = execution.load_selected_attempts(m.root)['entry']['review']
    assert stored['status'] == 'provisional'
    assert next(x for x in stored['predicates'] if x['name'] == 'speaker_source')['status'] == 'unknown'
