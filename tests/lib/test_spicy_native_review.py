"""Independent v2 native-source attacks; synthetic schemas, no keys or network."""
from decimal import localcontext
from datetime import datetime, timedelta, timezone
import builtins
import json
from pathlib import Path
import socket

import pytest

from lib import spicy_routes as routes
from lib import spicy_jobs as jobs, spicy_uploads as uploads
from lib.spicy_catalog import atomic_write_json, digest as canonical_digest


def declared(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


def source(pointer='/reference_image_urls/0', role='reference_image', mime='image/png'):
    return {'pointer': pointer, 'role': role, 'media_kind': 'image', 'mime': mime,
            'source_sha256': 'a' * 64, 'uri': 'spicy://f/synthetic-source'}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Native schema review must never open a network connection')
    monkeypatch.setattr(socket, 'socket', forbidden)


def test_source_role_matches_the_declared_control_pointer():
    schema = declared({'reference_image_urls': {'type': 'array', 'minItems': 1,
        'items': {'type': 'string', 'contentMediaType': ['image/png']}}})
    payload = {'reference_image_urls': ['spicy://f/synthetic-source']}
    assert routes.validate_payload(schema, payload, [source()])
    with pytest.raises(routes.SchemaError):
        routes.validate_payload(schema, payload, [source(role='first_frame')])


def test_duplicate_conditional_media_declarations_cannot_widen_the_slot():
    schema = declared({'image_url': {'type': 'string'}})
    schema['allOf'] = [
        {'properties': {'image_url': {'type': 'string', 'contentMediaType': ['image/png']}}},
        {'properties': {'image_url': {'type': 'string', 'contentMediaType': ['image/jpeg']}}},
    ]
    with pytest.raises(routes.SchemaError):
        routes.validate_payload(schema, {'image_url': 'spicy://f/synthetic-source'},
            [source('/image_url', 'first_frame', 'image/jpeg')])


def test_duplicate_conditional_source_limits_keep_the_narrowest_declaration():
    schema = declared({'image_url': {'type': 'string', 'contentMediaType': ['image/png'],
        'x-ui': {'max_size_mb': 1, 'max_duration_seconds': 3}}})
    schema['allOf'] = [{'properties': {'image_url': {'type': 'string',
        'contentMediaType': ['image/png'], 'x-ui': {'max_size_mb': 10, 'max_duration_seconds': 5}}}}]
    limits = routes.media_declaration(schema, '/image_url')['limits']
    assert limits['max_size_mb'] == 1
    assert limits['max_duration_seconds'] == 3


def test_untyped_url_named_leaf_is_not_an_unregistered_source_workflow():
    schema = declared({'source_url': {'type': 'string'}})
    for value in ('//external.invalid/input.mp4', 'https://external.invalid/input.mp4'):
        with pytest.raises(routes.SchemaError):
            routes.validate_payload(schema, {'source_url': value})


def test_real_wan_web_reference_branch_is_not_emittable():
    fixture = Path(__file__).parents[1] / 'fixtures/spicy/catalogue-2026-10-22.json'
    rows = json.loads(fixture.read_text())['items']
    row = next(row for row in rows if row['model'] == 'alibaba/wan-3.0-prime/reference-to-video')
    payload = routes.materialize_defaults(row['inputSchema'],
        {'reference_link_url': 'https://external.invalid/unregistered-page'})
    with pytest.raises(routes.SchemaError):
        routes.validate_payload(row['inputSchema'], payload)


def test_nested_source_binding_cannot_move_between_array_slots_or_mime_types():
    schema = declared({'references': {'type': 'array', 'minItems': 2, 'maxItems': 2,
        'items': declared({'image_url': {'type': 'string', 'contentMediaType': ['image/png']},
                          'strength': {'type': 'number', 'minimum': 0, 'maximum': 1}},
                         ['image_url'])}})
    payload = {'references': [{'image_url': 'spicy://f/synthetic-source'},
                              {'image_url': 'spicy://f/synthetic-source'}]}
    bindings = [source('/references/0/image_url', 'first_frame'),
                source('/references/1/image_url', 'first_frame')]
    assert routes.validate_payload(schema, payload, bindings)
    for damaged in ([bindings[0]], [bindings[0], bindings[0]],
                    [bindings[0], source('/references/1/image_url', 'first_frame', 'image/jpeg')]):
        with pytest.raises(routes.SchemaError):
            routes.validate_payload(schema, payload, damaged)
    payload['references'][0]['unadvertised_source'] = 'https://external.invalid/x'
    with pytest.raises(routes.SchemaError):
        routes.validate_payload(schema, payload, bindings)


@pytest.mark.parametrize('change', [
    {'$ref': 'https://external.invalid/schema.json'},
    {'$dynamicRef': '#/$defs/control'},
    {'$id': 'https://external.invalid/other.json', '$ref': '#/$defs/control'},
])
def test_no_remote_reference_or_changed_identifier_scope(change):
    schema = declared({'value': change})
    schema['$defs'] = {'control': {'type': 'string'}}
    assert routes.describe_route(schema)['status'] == 'blocked'


def test_local_refs_apply_full_nested_controls_and_exact_numeric_steps():
    schema = declared({'options': {'$ref': '#/$defs/options'}})
    schema['$defs'] = {'options': declared({'strength': {'type': 'number', 'multipleOf': 0.01},
                                          'count': {'type': 'integer', 'minimum': 1}},
                                         ['strength', 'count'])}
    with localcontext() as context:
        context.prec = 2
        assert routes.validate_payload(schema, {'options': {'strength': 1.25, 'count': 2}})
    for options in ({'strength': 1.251, 'count': 2}, {'strength': 1.25, 'count': True},
                    {'strength': float('nan'), 'count': 2},
                    {'strength': 1.25, 'count': 2, 'unknown': 'fixture'}):
        with pytest.raises((routes.SchemaError, ValueError)):
            routes.validate_payload(schema, {'options': options})


@pytest.mark.parametrize('bad_hosts', [['unqualified.invalid'], [{}],
    ['synthetic.r2.cloudflarestorage.com', True], []])
def test_cached_upload_rejects_malformed_historical_transfer_hosts(tmp_path, bad_hosts):
    source_digest, credential = 'a' * 64, 'b' * 64
    directory = tmp_path / 'artifacts'
    directory.mkdir(mode=0o700)
    receipt_path, journal_path = uploads._v2_paths(tmp_path, source_digest, credential, 'image')
    receipt = {'version': '2.0', 'source_sha256': source_digest, 'credential_digest': credential,
        'media_kind': 'image', 'mime': 'image/png', 'content_type': 'image/png', 'bytes': 64,
        'uri': 'spicy://f/synthetic-source', 'file_id': 'synthetic-source',
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'batch_id': 'synthetic-batch'}
    binding = {'source_sha256': source_digest, 'credential_digest': credential,
        'credential_ref': 'env:SPICY_SYNTHETIC_REVIEW', 'account_label': 'synthetic-account',
        'media_kind': 'image', 'mime': 'image/png', 'bytes': 64,
        'qualified_storage_hosts': ['synthetic.r2.cloudflarestorage.com']}
    transfer_binding = dict(binding, batch_id='synthetic-batch')
    receipt.update(transfer_binding=transfer_binding,
                   transfer_binding_sha256=canonical_digest(transfer_binding))
    journal = {'version': '2.0', 'state': 'committed', 'batch_id': 'synthetic-batch',
               'binding': binding, 'receipt': receipt}
    atomic_write_json(receipt_path, receipt)
    atomic_write_json(journal_path, journal)
    original = uploads.usable_receipt_v2(tmp_path, source_digest, credential, 'image', 'image/png')
    assert original['transfer_binding'] == transfer_binding
    journal['binding']['qualified_storage_hosts'] = bad_hosts
    atomic_write_json(journal_path, journal)
    with pytest.raises(jobs.SpicyError):
        uploads.usable_receipt_v2(tmp_path, source_digest, credential, 'image', 'image/png')


def test_custom_shot_duration_sum_is_exact_under_low_ambient_precision():
    with localcontext() as context:
        context.prec = 2
        jobs._native_semantics_v2('kling/fixture/text-to-video', {
            'duration_seconds': 10, 'multi_prompt': [
                {'duration_seconds': 5.01}, {'duration_seconds': 4.99}]})
        with pytest.raises(routes.SchemaError):
            jobs._native_semantics_v2('kling/fixture/text-to-video', {
                'duration_seconds': 10, 'multi_prompt': [
                    {'duration_seconds': 5.01}, {'duration_seconds': 5.01}]})


def test_v2_conflicting_creative_carrier_cannot_be_silently_dropped(tmp_path):
    schema = declared({'prompt': {'type': 'string'}, 'generate_audio': {'type': 'boolean'}},
                      ['prompt', 'generate_audio'])
    reference = 'env:SPICY_SYNTHETIC_REVIEW'
    observation = {'model': 'fixture/text-to-video', 'task': 'text-to-video',
        'modality': 'video', 'enabled': True, 'available': True, 'input_schema': schema,
        'schema_sha256': canonical_digest(schema), 'credential_binding': {
            'credential_ref': reference, 'credential_digest': 'b' * 64}}
    inputs = {'project_dir': str(tmp_path), 'model': observation['model'],
        'credential_ref': reference, 'native_input': {'prompt': 'fixture', 'generate_audio': False},
        'generate_audio': True}
    with pytest.raises(jobs.SpicyError):
        jobs.build_native_request_v2(inputs, observation=observation)


def test_retained_ready_proof_rejects_unhashable_historical_host_cleanly():
    schema = declared({'image_url': {'type': 'string', 'contentMediaType': ['image/png']}})
    binding = source('/image_url', 'first_frame')
    transfer = {'source_sha256': binding['source_sha256'],
        'credential_ref': 'env:SPICY_SYNTHETIC_REVIEW', 'credential_digest': 'b' * 64,
        'account_label': 'synthetic-account', 'media_kind': 'image', 'mime': 'image/png',
        'bytes': 64, 'qualified_storage_hosts': [{}], 'batch_id': 'synthetic-batch'}
    binding.update(bytes=64, upload_credential_digest='b' * 64,
        transfer_binding=transfer, transfer_binding_sha256=canonical_digest(transfer))
    payload = {'image_url': binding['uri']}
    native = {'transport': jobs.TRANSPORT_V2, 'payload_state': 'ready',
        'model': 'synthetic/image-to-video', 'task': 'image-to-video', 'mode': 'image_to_video',
        'input_schema': schema, 'schema_sha256': canonical_digest(schema), 'input': payload,
        'request_sha256': canonical_digest({'model': 'synthetic/image-to-video', 'input': payload}),
        'sources': [binding]}
    with pytest.raises(jobs.SpicyError):
        jobs.require_ready_v2(native)


def test_public_docs_cli_does_not_load_the_production_dotenv_import_hook(monkeypatch, capsys):
    real_import = builtins.__import__
    def production_import_denied(name, *args, **kwargs):
        if name in ('lib.spicy_lifecycle', 'tools.base_tool'):
            pytest.fail('Public docs must not initialize production credentials or lifecycle')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', production_import_denied)
    monkeypatch.setattr(jobs, 'client_for_credential', lambda *a, **k: pytest.fail('Public docs need no key'))
    assert jobs.main(['account', 'docs']) == 0
    assert json.loads(capsys.readouterr().out)
