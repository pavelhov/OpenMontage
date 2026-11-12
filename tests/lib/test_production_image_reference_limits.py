"""Offline preflight of exact route reference arrays and retained originals."""
import copy
import json

import pytest

from lib import production_boards as boards, production_images as images, production_request as request
from lib.shot_contract import file_sha256
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_images import board_package, host_receipt  # noqa: F401


def add_sources(root, spec, count):
    original = spec['references'][0]
    spec['references'] = []
    for index in range(count):
        reference = copy.deepcopy(original)
        reference['id'] = 'source-' + str(index)
        reference['path'] = 'assets/images/source-' + str(index) + '.png'
        # Identical hashes still occupy distinct entries in the actual envelope.
        (root / reference['path']).write_bytes((root / original['path']).read_bytes())
        spec['references'].append(reference)
    return [row['id'] for row in spec['references']]


def package_sources(root, package, count):
    spec = package['boards']
    original = copy.deepcopy(spec['references'][0])
    ids = add_sources(root, spec, count)
    # Keep the fixture's original video consumers and their semantic declarations.
    spec['references'].append(original)
    spec['image_nodes'][-1]['reference_ids'] = ids
    return spec


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_later_six_reference_board_rejects_entire_package_before_writes(neutral_package):
    root, package, bind = neutral_package
    package_sources(root, package, 6)
    binding = bind()
    before = files(root)
    for operation in (
        lambda: request.preflight_package(root, package, package_binding=binding),
        lambda: request.compile_package(root, binding),
        lambda: request.prepare_package(root, binding),
    ):
        with pytest.raises(ValueError, match='slot other route native_imagegen has 6 references; limit 5'):
            operation()
        assert files(root) == before
    assert images.image_usage(root)['total'] == 0
    assert not (root / 'production_attempts').exists()


def test_five_sources_and_missing_generated_dependencies_are_counted(neutral_package):
    root, package, bind = neutral_package
    spec = package_sources(root, package, 4)
    node = spec['image_nodes'][-1]
    node['image_dependencies'] = ['start']
    binding = bind()
    assert not (root / 'assets/images/start.png').exists()
    request.preflight_package(root, package, package_binding=binding)
    assert images.validate_board_reference_limits(spec)[-1]['reference_count'] == 5
    node['image_dependencies'].append('end')
    binding = bind()
    before = files(root)
    with pytest.raises(ValueError, match='slot other route native_imagegen has 6 references; limit 5'):
        request.preflight_package(root, package, package_binding=binding)
    assert files(root) == before


def test_native_exact_envelope_rejects_before_reservation(board_package):
    root, packet, args, target = board_package
    args['referenced_image_paths'] *= 6
    # Request hashes remain reproducible for historical over-limit originals.
    images.image_request_digest(packet, 's1-start', args, output_path=target)
    with pytest.raises(ValueError, match='slot s1-start route native_imagegen has 6 references; limit 5'):
        images.reserve_native_image(root, packet, 's1-start', args, output_path=target, scope_id='image-episode')
    assert images.image_usage(root)['total'] == 0
    assert not (root / images.ARTIFACT).exists()


def test_explicit_native_envelope_rejects_later_template_before_activation(board_package):
    root, packet, _, target = board_package
    template = {'prompt': 'Synthetic source-bound frame.', 'transparent_background': False,
        'output_path': target, 'reference_ids': ['doctor'] * 6}
    before = files(root)
    with pytest.raises(ValueError, match='slot s1-start route native_imagegen has 6 references; limit 5'):
        images.prepare_native_image_envelope(root, packet=packet, board_plan=packet['story'],
            templates={'s1-start': template}, source_inventory=packet['references'], image_allowance=1)
    assert files(root) == before


def test_native_five_entry_envelope_and_defensive_begin_limit(board_package, monkeypatch):
    root, packet, args, target = board_package
    args['referenced_image_paths'] *= 5
    scope_path = root / 'production_scopes.json'
    scopes = json.loads(scope_path.read_text())
    scopes['scopes'][0]['requests']['s1-start'] = images.image_request_digest(packet, 's1-start', args, output_path=target)
    scope_path.write_text(json.dumps(scopes))
    call = images.reserve_native_image(root, packet, 's1-start', args, output_path=target, scope_id='image-episode')
    monkeypatch.setitem(images.NATIVE_INPUT_SCHEMA['properties']['referenced_image_paths'], 'maxItems', 4)
    before = files(root)
    with pytest.raises(ValueError, match='has 5 references; limit 4'):
        images.begin_native_image(root, call['call_id'])
    assert files(root) == before
    monkeypatch.setitem(images.NATIVE_INPUT_SCHEMA['properties']['referenced_image_paths'], 'maxItems', 5)
    envelope = images.begin_native_image(root, call['call_id'])
    assert len(envelope['arguments']['referenced_image_paths']) == 5


def test_historical_over_limit_original_is_still_importable(board_package, monkeypatch):
    root, packet, args, target = board_package
    args['referenced_image_paths'] *= 6
    scope_path = root / 'production_scopes.json'
    scopes = json.loads(scope_path.read_text())
    scopes['scopes'][0]['requests']['s1-start'] = images.image_request_digest(packet, 's1-start', args, output_path=target)
    scope_path.write_text(json.dumps(scopes))
    monkeypatch.setitem(images.NATIVE_INPUT_SCHEMA['properties']['referenced_image_paths'], 'maxItems', 6)
    call = images.reserve_native_image(root, packet, 's1-start', args, output_path=target, scope_id='image-episode')
    envelope = images.begin_native_image(root, call['call_id'])
    monkeypatch.setitem(images.NATIVE_INPUT_SCHEMA['properties']['referenced_image_paths'], 'maxItems', 5)
    assert images.image_call_status(root, call['call_id'])['state'] == 'submitted'
    replayed = images.reserve_native_image(root, packet, 's1-start', args,
        output_path=target, scope_id='image-episode', call_id=call['call_id'])
    assert replayed['state'] == 'submitted'
    result = images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope))
    assert result['state'] == 'imported'
    assert file_sha256(root / target) == result['output']['sha256']
    replayed = images.reserve_native_image(root, packet, 's1-start', args,
        output_path=target, scope_id='image-episode', call_id=call['call_id'])
    assert replayed['state'] == 'imported'


def test_routes_read_their_own_reference_contract(monkeypatch):
    from tools.graphics.grok_cli_image import GrokCLIImage
    assert images.image_reference_limit('grok_cli') == 5
    monkeypatch.setitem(GrokCLIImage.input_schema['properties']['image_paths'], 'maxItems', 3)
    assert images.validate_image_reference_count('grok-slot', ['a'] * 3, route='grok_cli')['reference_limit'] == 3
    with pytest.raises(ValueError, match='grok-slot route grok_cli has 4 references; limit 3'):
        images.validate_image_reference_count('grok-slot', ['a'] * 4, route='grok_cli')
    form = {'properties': {'visualReferences': {'type': 'array', 'maxItems': 8}}}
    assert images.validate_image_reference_count('fallback', ['a'] * 6, route='openart_mcp', form=form)['reference_limit'] == 8
    with pytest.raises(ValueError, match='fallback route openart_mcp has 9 references; limit 8'):
        images.validate_image_reference_count('fallback', ['a'] * 9, route='openart_mcp', form=form)
    assert images.image_reference_limit() == 5


def test_retained_package_readable_but_new_image_work_rejects_all_boards(neutral_package, monkeypatch):
    root, package, bind = neutral_package
    package_sources(root, package, 6)
    binding = bind()
    monkeypatch.setitem(images.NATIVE_INPUT_SCHEMA['properties']['referenced_image_paths'], 'maxItems', 6)
    request.prepare_package(root, binding)
    monkeypatch.setitem(images.NATIVE_INPUT_SCHEMA['properties']['referenced_image_paths'], 'maxItems', 5)
    assert boards.load_board_pack(root)['specification'] == package['boards']
    request.compile_package(root, binding)
    before = files(root)
    with pytest.raises(ValueError, match='slot other route native_imagegen has 6 references; limit 5'):
        boards.prepare_image_request(root, 'start', images=images)
    # A transaction lock may be opened, but no image packets or reservations are written.
    assert files(root) == before
    assert images.image_usage(root)['total'] == 0
