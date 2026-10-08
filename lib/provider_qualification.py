"""Pure, fail-closed authority checks for the bounded qualification pipeline.

No submission, approval creation, profile promotion, or provider calls occur here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema.exceptions import ValidationError

from lib.checkpoint import validate_checkpoint
from schemas.artifacts import validate_artifact


class QualificationValidationError(ValueError):
    """Missing exact human approval or drift in a qualification request."""


def _fail(message):
    raise QualificationValidationError('Provider qualification: ' + message)


def _read_local(root, path):
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        _fail('retained evidence is missing or outside its project')
    return json.loads(resolved.read_text(encoding='utf-8'))


def _binding(root, binding):
    path = Path(binding['path'])
    if path.is_absolute() or '..' in path.parts:
        _fail('binding path must be safe and project-relative')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        _fail('binding file missing or outside project')
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != binding['sha256']:
        _fail('current binding bytes differ from preparation')


_NATIVE_SETTING_ALIASES = {'duration': 'duration', 'aspect_ratio': 'aspectRatio', 'resolution': 'resolution'}


def _settings_equal(left, right):
    # Python considers True == 1; exact native approval binds JSON value types.
    return json.dumps(left, sort_keys=True, separators=(',', ':'), allow_nan=False) == json.dumps(
        right, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _explicit_native_settings(inputs):
    """Normalize explicit flags only; never fill omitted controls from defaults."""
    settings = {key: inputs[key] for key in _NATIVE_SETTING_ALIASES if key in inputs}
    params = inputs.get('native_params', {})
    if not isinstance(params, dict):
        _fail('native_params must be an exact settings object')
    inverse = {alias: key for key, alias in _NATIVE_SETTING_ALIASES.items()}
    if set(params) - set(inverse):
        _fail('qualification contains an unapproved extra native parameter')
    for alias, value in params.items():
        key = inverse[alias]
        if key in settings:
            _fail('qualification contains ambiguous duplicate native setting aliases')
        settings[key] = value
    return settings


def validate_qualification_stage(root, inputs, request_sha256, *, native=None, profile=None):
    """Require the explicit generation stage and currently approved exact packet.

    ``native`` and ``profile`` are required observations, despite optional keyword
    defaults for a clear fail-closed error at older callers. The caller computes
    its normal governed request digest and prepares native controls first.
    """
    root = Path(root).resolve()
    try:
        marker = _read_local(root, 'project.json')
        if marker.get('pipeline_type') != 'provider-qualification':
            _fail('project is not enrolled in this pipeline')
        if marker.get('governance', {}).get('mode') != 'strict':
            _fail('strict governance is required')
        if inputs.get('governance', {}).get('stage') != 'generate':
            _fail('request must belong to the generate stage')
        checkpoint = _read_local(root, 'checkpoint_prepare.json')
        validate_checkpoint(checkpoint)
        if (checkpoint.get('pipeline_type') != 'provider-qualification'
                or checkpoint.get('project_id') != marker.get('project_id')
                or checkpoint.get('stage') != 'prepare'
                or checkpoint.get('status') != 'completed'
                or checkpoint.get('human_approved') is not True):
            _fail('preparation requires completed exact human approval')
        packet = _read_local(root, 'artifacts/provider_qualification_packet.json')
        validate_artifact('provider_qualification_packet', packet)
        if checkpoint['artifacts'].get('provider_qualification_packet') != packet:
            _fail('current packet differs from human-approved preparation')
        if packet['project_id'] != marker.get('project_id'):
            _fail('packet project identity differs')
        if packet['story_revision'] != marker.get('story_revision'):
            _fail('current story revision differs from preparation')
        if packet['provider'] == 'openart_mcp':
            return _validate_mcp_packet(root, packet, inputs, request_sha256, native, profile)
        from lib.production_request import _id
        for name in ('compiled_request', 'preparation_review'):
            try:
                selected_id = _id(inputs.get(name + '_id'))
            except ValueError:
                _fail('artifact selection requires a safe retained ID')
            expected_path = 'artifacts/' + name + '-' + selected_id + '.json'
            if packet[name]['path'] != expected_path:
                _fail('artifact selection differs from approved preparation')
        governance = inputs['governance']
        if any(governance.get(key) != packet[key] for key in ('scope_id', 'shot_id')):
            _fail('scope or shot differs from approved preparation')
        authority_id = _id(inputs.get('unknown_cost_authorization_id'))
        if authority_id != packet['unknown_cost_authorization_id']:
            _fail('unknown-cost authority selection differs from preparation')
        scopes = _read_local(root, 'production_scopes.json')
        if scopes.get('version') != '1.0':
            _fail('current qualification scopes version is invalid')
        matches = [item for item in scopes.get('scopes', []) if item.get('id') == packet['scope_id']]
        if len(matches) != 1:
            _fail('one selected qualification scope is required')
        scope = matches[0]
        shot = packet['shot_id']
        allowance = scope.get('attempts_per_shot', {}).get(shot)
        if (scope.get('phase') != 'first_pass' or type(allowance) is not int or allowance != 1
                or set(scope.get('attempts_per_shot', {})) != {shot}
                or set(scope.get('requests', {})) != {shot}
                or isinstance(scope['requests'][shot], list) and len(scope['requests'][shot]) != 1):
            _fail('qualification scope must allow exactly one first-pass shot and zero repairs')
        authorization_path = 'artifacts/unknown_cost_authorization-' + authority_id + '.json'
        authorization = _read_local(root, authorization_path)
        terms = {key: value for key, value in authorization.items() if key != 'evidence'}
        authority_sha = hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        occurrence = authorization.get('occurrences', [])
        if (authorization.get('version') != '1' or authorization.get('kind') != 'openart_unknown_cost'
                or authorization.get('purpose') != 'result_contract_qualification'
                or type(authorization.get('count')) is not int or authorization['count'] != 1
                or len(occurrence) != 1 or type(occurrence[0].get('index')) is not int or occurrence[0]['index'] != 0
                or scope.get('unknown_cost_authorization_sha256') != authority_sha):
            _fail('one exact unknown-cost qualification occurrence is required')
        for item in (scope, authorization):
            if (item.get('status') != 'approved' or not item.get('approved_by')
                    or item.get('provider') != packet['provider']
                    or item.get('project_id') != packet['project_id']
                    or item.get('story_revision') != packet['story_revision']):
                _fail('selected scope and authority must have matching strict approval bindings')
        if (authorization.get('scope_id') != packet['scope_id']
                or authorization.get('shot_id') != shot
                or authorization.get('project_root') != str(root)
                or occurrence[0].get('request_sha256') != request_sha256
                or occurrence[0].get('native_sha256') != packet['native_binding']['native_body_sha256']
                or occurrence[0].get('profile_sha256') != packet['native_binding']['profile_sha256']):
            _fail('unknown-cost qualification occurrence differs from exact preparation')
        for key in ('model', 'mode'):
            if inputs.get(key) != packet[key]:
                _fail('explicit sample settings differ from preparation')
        if 'native_settings' in packet:
            approved_settings = packet['native_settings']
            submitted_settings = _explicit_native_settings(inputs)
            if not _settings_equal(submitted_settings, approved_settings):
                _fail('exact explicit native settings differ from preparation')
        else:
            for key in ('duration', 'aspect_ratio', 'resolution'):
                if inputs.get(key) != packet[key]:
                    _fail('explicit sample settings differ from preparation')
        if any(authorization.get(key) != packet[key] for key in ('model', 'mode')):
            _fail('authority model or mode differs from preparation')
        prompt = inputs.get('prompt')
        if (prompt != packet['prompt'] or not isinstance(prompt, str)
                or hashlib.sha256(prompt.encode('utf-8')).hexdigest() != packet['prompt_sha256']
                or request_sha256 != packet['request_sha256']):
            _fail('exact prompt or governed request digest differs')
        if not isinstance(native, dict) or not isinstance(profile, dict):
            _fail('current native preview and profile are required')
        if 'native_settings' in packet:
            if isinstance(native.get('native_params'), dict):
                effective = _explicit_native_settings({'native_params': native['native_params']})
            elif isinstance(native.get('native_controls'), dict):
                effective = {key: native['native_controls'][key] for key in _NATIVE_SETTING_ALIASES
                             if native['native_controls'].get(key) is not None}
            else:
                _fail('current prepared native explicit settings are required')
            if not _settings_equal(effective, approved_settings):
                _fail('prepared native settings differ from explicit approval')
        for observed in (native, profile):
            if any(key in observed and observed[key] != packet[key] for key in ('model', 'mode')):
                _fail('native preview/profile exact model or mode differs')
        submitted_assets = native.get('input_assets')
        if submitted_assets is None:
            legacy = native.get('image_upload')
            submitted_assets = ([{'role': 'first_frame', **legacy}] if legacy else [])
        if packet['native_no_reference']:
            from lib.production_execution import INPUT_PATH_KEYS
            if packet['mode'] != 'text2video' or submitted_assets or any(
                    inputs.get(key) for key in INPUT_PATH_KEYS | {'image_upload_id', 'input_assets'}):
                _fail('qualification sample requires native reference-free text-to-video')
        else:
            expected = packet['input_assets']
            normalized = [{'role': a.get('role'), 'sha256': a.get('source_sha256'),
                           'upload_id': a.get('upload_id')} for a in submitted_assets]
            if normalized != [{key: a[key] for key in ('role', 'sha256', 'upload_id')} for a in expected]:
                _fail('qualification native asset roles or upload proofs differ')
            for asset in expected:
                _binding(root, asset)
            # Paths and bytes must also occur in the exact governed request.
            from lib.production_execution import _paths, _input_sha256
            actual = []
            _paths(inputs, root, lambda key, path: actual.append((str(path.resolve()), _input_sha256(path))) or str(path))
            for asset in expected:
                if (str((root / asset['path']).resolve()), asset['sha256']) not in actual:
                    _fail('qualification approved source is absent from request')
        if any(native.get(key) != value for key, value in packet['native_binding'].items()):
            _fail('native preview binding differs')
        if profile.get('profile_sha256') != packet['native_binding']['profile_sha256']:
            _fail('profile binding differs')
        for binding in [packet['compiled_request'], packet['preparation_review'], *packet['sources']]:
            _binding(root, binding)
        return packet
    except QualificationValidationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ValidationError):
        raise QualificationValidationError(
            'Provider qualification: missing or invalid preparation evidence'
        ) from None


def _validate_mcp_packet(root, packet, inputs, request_sha256, native, profile):
    """Fresh connector qualification approval cannot borrow any CLI authority."""
    from lib.production_request import _id, _native_binding, digest
    if packet.get('tool') != 'openart_mcp_video' or not isinstance(native, dict) or not isinstance(profile, dict):
        _fail('MCP qualification requires canonical native/profile observations')
    if native.get('provider') != 'openart_mcp' or profile.get('provider') != 'openart_mcp':
        _fail('MCP qualification cannot use a CLI profile or native body')
    if request_sha256 != packet['request_sha256']:
        _fail('MCP governed request differs from exact approved packet')
    for key in ('scope_id', 'shot_id'):
        if inputs.get('governance', {}).get(key) != packet[key]:
            _fail('MCP scope or shot differs from preparation')
    for key in ('model', 'mode'):
        if inputs.get(key) != packet[key] or native.get(key) != packet[key] or profile.get(key) != packet[key]:
            _fail('MCP exact model/mode differs from preparation')
    for name in ('compiled_request', 'preparation_review'):
        if packet[name]['path'] != 'artifacts/' + name + '-' + _id(inputs.get(name + '_id')) + '.json':
            _fail('MCP artifact selection differs from preparation')
    prompt = inputs.get('prompt')
    if prompt != packet['prompt'] or not isinstance(prompt, str) or hashlib.sha256(prompt.encode()).hexdigest() != packet['prompt_sha256']:
        _fail('MCP prompt differs from exact preparation')
    binding = _native_binding(native)
    if digest(packet['native_binding']) != digest(binding) or profile.get('profile_sha256') != binding['profile_sha256']:
        _fail('MCP native/schema/account/reference/profile binding differs')
    from lib.production_request import mcp_native_settings
    settings = mcp_native_settings(native)
    if not _settings_equal(settings, packet.get('native_settings')):
        _fail('MCP native settings differ from exact prepared body')
    scopes = _read_local(root, 'production_scopes.json')
    matches = [row for row in scopes.get('scopes', []) if row.get('id') == packet['scope_id']]
    if len(matches) != 1:
        _fail('one retained MCP qualification scope is required')
    scope = matches[0]; shot = packet['shot_id']
    requests = scope.get('requests', {}).get(shot)
    if (scope.get('status') != 'approved' or not scope.get('approved_by') or scope.get('provider') != 'openart_mcp'
            or scope.get('phase') != 'first_pass' or digest(scope.get('attempts_per_shot')) != digest({shot: 1})
            or set(scope.get('requests', {})) != {shot}
            or isinstance(requests, list) and len(requests) != 1):
        _fail('MCP qualification requires one fresh first-pass occurrence and zero repairs')
    if _id(inputs.get('unknown_cost_authorization_id')) != packet['unknown_cost_authorization_id']:
        _fail('MCP billing authorization selection differs')
    submitted = native.get('input_assets', [])
    if packet['native_no_reference']:
        if submitted or packet['mode'] != 'text2video':
            _fail('MCP reference-free packet carries references or different mode')
    else:
        expected = packet.get('input_assets', [])
        actual = [{'role': row['role'], 'sha256': row['source_sha256'], 'upload_id': row.get('upload_id', row.get('reference_id'))} for row in submitted]
        if actual != [{key: row[key] for key in ('role', 'sha256', 'upload_id')} for row in expected]:
            _fail('MCP native reference roles or upload IDs differ')
        for row in expected: _binding(root, row)
    for row in [packet['compiled_request'], packet['preparation_review'], *packet['sources']]:
        _binding(root, row)
    return packet
