"""Initial authored prompts ask for no added music unless sound is authored;
frozen originals compiled before this default remain readable."""
import json

from lib import production_request as request
from lib.shot_contract import project_review_subject, shot_review_subject
from tests.lib.test_production_request import shootable_project


def _drop_sound(root, contract):
    del contract['shots'][0]['sound']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for s in contract['shots']:
        s['review']['subject_sha256'] = shot_review_subject(contract, s['id'])
    (root / 'artifacts/shot_contract.json').write_text(json.dumps(contract))


def test_absent_sound_adds_no_music_direction_once(tmp_path):
    root, contract = shootable_project(tmp_path)
    _drop_sound(root, contract)
    packet = request.build_static_source_packet(root, 'entry', provider='openart_cli')
    compiled = request._compile_provider_packet(packet, provider='openart_cli')
    assert compiled['prompt'].endswith(request.NO_MUSIC_DIRECTION)
    assert compiled['prompt'].count('No background music') == 1
    # Coverage spans still address the same authored literal bytes.
    for span in compiled['coverage']:
        assert request.digest(compiled['prompt'][span['start']:span['end']]) == span['fragment_sha256']


def test_authored_sound_is_kept_without_default(tmp_path):
    root, _ = shootable_project(tmp_path)
    packet = request.build_static_source_packet(root, 'entry', provider='openart_cli')
    prompt = request._compile_provider_packet(packet, provider='openart_cli')['prompt']
    assert request.NO_MUSIC_DIRECTION not in prompt
    assert 'Sound: Dialogue and cutlery clatter SFX only; no background music.' in prompt


def test_historical_prompt_without_default_suffix_stays_authenticated(tmp_path):
    root, contract = shootable_project(tmp_path)
    _drop_sound(root, contract)
    packet = request.build_static_source_packet(root, 'entry', provider='openart_cli')
    historical = request._compile_provider_packet(packet, provider='openart_cli', historical=True)
    current = request._compile_provider_packet(packet, provider='openart_cli')
    assert request.NO_MUSIC_DIRECTION.strip() not in historical['prompt']
    # A pre-change frozen prompt and a post-change prompt both match; altered text does not.
    assert request.historical_prompt_matches(historical['prompt'], historical)
    assert request.historical_prompt_matches(current['prompt'], historical)
    assert not request.historical_prompt_matches(historical['prompt'] + ' extra', historical)


def test_grok_sealed_historical_canonical_accepts_appended_direction():
    # Grok's seal strips the trailing newline from the historical canonical, while
    # the frozen submitted prompt carries the no-music line after its own newline.
    canonical = {'prompt': 'Scene.\nNo spoken lines in this shot.'}
    sealed = 'Scene.\nNo spoken lines in this shot.\n' + request.NO_MUSIC_DIRECTION.rstrip('\n')
    assert request.historical_prompt_matches(sealed, canonical)
    assert not request.historical_prompt_matches(
        'Scene.\nNo spoken lines in this shot.' + request.NO_MUSIC_DIRECTION.rstrip('\n'), canonical)
