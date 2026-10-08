"""R18 offline reader proofs; fake local CLIs never contact a provider."""
import json

import pytest

from lib import openart_dispatch as dispatch
from lib.production_video_guard import read_video_duplicate_blocks, classify_production_kind
from lib.provider_credit_ledger import QualifiedSlotRelease
from tests.integration.test_openart_dispatch_recovery import governed, _qualified_billing_origin
from tests.lib.test_production_execution import project, MotionTool
from tools.video.openart_cli_video import OpenArtCLIVideo


def journal(root, *, kind='motion', shot='entry', status='running', provider='other'):
    directory = root / 'production_attempts' / 'original'
    directory.mkdir(parents=True)
    request = {'attempt_id':'original', 'shot_id':shot, 'media_kind':kind,
               'scope_id':'old', 'scope':{'provider':provider}}
    (directory / 'request.json').write_text(json.dumps(request))
    if status is not None:
        (directory / 'result.json').write_text(json.dumps({'status':status}))
    return directory


@pytest.fixture
def strict_project(tmp_path, monkeypatch):
    project(tmp_path, motion=True)
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'absent-private'))
    return tmp_path


@pytest.mark.parametrize('provider', ['openart_cli', 'grok_cli', 'fake', 'other'])
@pytest.mark.parametrize('status', ['pending', 'running', 'submitting', 'uncertain', None, 'mystery'])
def test_every_provider_unresolved_original_blocks(strict_project, provider, status):
    journal(strict_project, provider=provider, status=status)
    reasons = read_video_duplicate_blocks(strict_project, 'entry')
    assert len(reasons) == 1 and 'original' in reasons[0]


@pytest.mark.parametrize('kind', ['image', 'audio', 'local_render'])
def test_explicit_other_phase_is_independent(strict_project, kind):
    journal(strict_project, kind=kind, status='uncertain')
    assert read_video_duplicate_blocks(strict_project, 'entry') == []


@pytest.mark.parametrize('kind', ['motion', None, 'unknown'])
def test_other_positively_named_shot_is_independent(strict_project, kind):
    journal(strict_project, kind=kind, shot='other', status='uncertain')
    assert read_video_duplicate_blocks(strict_project, 'entry') == []


@pytest.mark.parametrize('kind', [None, 'unknown', ''])
def test_same_shot_unknown_kind_fails_closed_even_if_terminal(strict_project, kind):
    journal(strict_project, kind=kind, status='failed')
    assert 'unclassifiable' in read_video_duplicate_blocks(strict_project, 'entry')[0]


@pytest.mark.parametrize('status', ['generated', 'failed', 'not_dispatched'])
def test_known_host_terminal_states_are_nonblocking(strict_project, status):
    journal(strict_project, status=status)
    assert read_video_duplicate_blocks(strict_project, 'entry') == []


def test_phase_or_tool_name_cannot_classify_missing_kind():
    assert classify_production_kind({'phase':'first_pass', 'tool_name':'video'}) is None
    assert classify_production_kind({'media_kind':'video'}) == 'motion'


@pytest.mark.parametrize('malformed', [[], {'media_kind': []}, {'media_kind': {}}])
def test_malformed_kind_is_unclassifiable(malformed):
    assert classify_production_kind(malformed) is None


def test_real_fake_tool_uncertainty_never_retries(strict_project):
    # The strict fixture already contains the retained approved request.
    scope = json.loads((strict_project / 'production_scopes.json').read_text())['scopes'][0]
    inputs = {'project_dir':str(strict_project), 'prompt':'Approved exact prompt',
              'image_path':str(strict_project / 'assets/start.svg'),
              'last_image_path':str(strict_project / 'assets/end.svg'),
              'reference_image_paths':[str(strict_project / 'assets/patient.svg')],
              'output_path':str(strict_project / 'assets/output.mp4'), 'duration':8,
              'governance':{'scope_id':scope['id'], 'shot_id':'entry'}}
    class Interrupted(MotionTool):
        calls = 0
        def execute(self, submitted):
            self.calls += 1
            raise KeyboardInterrupt('original acceptance unknown')
    tool = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        tool.execute(inputs)
    assert len(read_video_duplicate_blocks(strict_project, 'entry')) == 1
    with pytest.raises(Exception, match='uncertain|unresolved'):
        tool.execute(inputs)
    assert tool.calls == 1


def test_absent_private_state_is_read_only(strict_project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('reader initialized ledger or invoked CLI')
    monkeypatch.setattr(dispatch, 'ledger', forbidden)
    monkeypatch.setattr(dispatch.cli, 'run_readonly', forbidden)
    assert read_video_duplicate_blocks(strict_project, 'entry') == []
    assert not (strict_project / 'absent-private').exists()


def crash_after_reservation(governed):
    root, inputs, profile, tmp = governed
    seen = []
    def crash(stage, attempt):
        if stage == 'ledger_prepared':
            seen.append(attempt)
            raise RuntimeError('synthetic ledger_prepared crash')
    dispatch._CRASH_HOOK = crash
    with pytest.raises(RuntimeError, match='ledger_prepared'):
        OpenArtCLIVideo().execute(inputs)
    dispatch._CRASH_HOOK = None
    return root, tmp, seen[0]


def test_actual_private_reserved_before_journal_blocks_and_reader_has_zero_calls(governed, monkeypatch):
    root, tmp, attempt = crash_after_reservation(governed)
    assert not (root / 'production_attempts' / attempt / 'request.json').exists()
    assert not (tmp / 'paid').exists()
    calls = (tmp / 'calls').read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('reader initialized ledger or invoked CLI')
    monkeypatch.setattr(dispatch, 'ledger', forbidden)
    monkeypatch.setattr(dispatch.cli, 'run_readonly', forbidden)
    reasons = read_video_duplicate_blocks(root, 'entry')
    assert len(reasons) == 1 and attempt in reasons[0] and 'prepared' in reasons[0]
    assert read_video_duplicate_blocks(root, 'other') == []
    assert (tmp / 'calls').read_bytes() == calls
    assert not (root / 'production_attempts' / attempt / 'request.json').exists()


@pytest.mark.parametrize('mutation', ['missing', 'request', 'kind', 'shot', 'other_shot', 'audio', 'scope'])
def test_private_origin_cannot_be_absence_as_safe(governed, mutation):
    root, tmp, attempt = crash_after_reservation(governed)
    path = dispatch._manifest_path(attempt)
    if mutation == 'missing':
        path.unlink()
    else:
        manifest = json.loads(path.read_bytes())
        request = manifest['journal_records']['request.json']
        if mutation == 'request': request['request_sha256'] = '0' * 64
        if mutation == 'kind': request.pop('media_kind')
        if mutation == 'shot': request.pop('shot_id')
        if mutation == 'other_shot': request['shot_id'] = 'other'
        if mutation == 'audio': request['media_kind'] = 'audio'
        if mutation == 'scope': request['scope_id'] = 'other'
        path.write_text(json.dumps(manifest))
    assert len(read_video_duplicate_blocks(root, 'entry')) == 1
    assert not (tmp / 'paid').exists()


@pytest.mark.parametrize('ready', [False, True])
def test_actual_prelaunch_release_allows_next_without_launch(governed, ready):
    root, tmp, attempt = crash_after_reservation(governed)
    if ready:
        dispatch.repair_outbox(root, attempt)
    binding = dispatch._manifest(attempt)[1]
    dispatch.ledger().release_slot(QualifiedSlotRelease(binding, 'proved_not_dispatched', 'a' * 64, 'b' * 64, ''))
    assert (root / 'production_attempts' / attempt / 'request.json').exists() == ready
    assert read_video_duplicate_blocks(root, 'entry') == []
    assert not (tmp / 'paid').exists()
    if ready:
        path = root / 'production_attempts' / attempt / 'request.json'
        original = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(original + b'\n')
        assert len(read_video_duplicate_blocks(root, 'entry')) == 1
        path.write_bytes(original)
    # A subsequent actual launch marker contradicts the prelaunch release.
    (dispatch.jobs.job_dir(attempt) / 'launch.json').write_text('{}')
    assert len(read_video_duplicate_blocks(root, 'entry')) == 1


def test_qualified_terminal_unknown_billing_releases_motion_only(governed, monkeypatch):
    root, inputs, profile, tmp, attempt, binding, contract = _qualified_billing_origin(governed, monkeypatch)
    assert read_video_duplicate_blocks(root, 'entry')
    dispatch.resolve_attempt(root, attempt, binding.request_sha256)
    assert dispatch.ledger().inspect(attempt)['debit_state'] == 'unresolved'
    before = (tmp / 'calls').read_bytes()
    assert read_video_duplicate_blocks(root, 'entry') == []
    assert (tmp / 'calls').read_bytes() == before
    assert len((tmp / 'paid').read_text().splitlines()) == 1
    # A terminal ledger label cannot bypass the original ready-byte proof.
    path = root / 'production_attempts' / attempt / 'request.json'
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b'\n')
    assert len(read_video_duplicate_blocks(root, 'entry')) == 1
    assert (tmp / 'calls').read_bytes() == before


def test_public_terminal_cannot_override_private_pending(governed):
    root, tmp, attempt = crash_after_reservation(governed)
    dispatch.repair_outbox(root, attempt)
    (root / 'production_attempts' / attempt / 'result.json').write_text(json.dumps({'status':'generated'}))
    assert len(read_video_duplicate_blocks(root, 'entry')) == 1
    assert not (tmp / 'paid').exists()


def test_other_shot_private_identity_survives_broken_public_ready_bytes(governed):
    root, tmp, attempt = crash_after_reservation(governed)
    dispatch.repair_outbox(root, attempt)
    path = root / 'production_attempts' / attempt / 'request.json'
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b'\n')
    assert len(read_video_duplicate_blocks(root, 'entry')) == 1
    assert read_video_duplicate_blocks(root, 'other') == []
    assert not (tmp / 'paid').exists()
