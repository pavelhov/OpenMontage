"""Rooted optional Auto-continue policy authority and governed scope derivation.

Strict is the default. Any missing, malformed, stale, revoked, conflicting or
benchmark-bound policy yields ``None`` from :func:`load_active_policy`; callers
must treat that as Strict. Readers never write. Writers retain exact derived scopes, their credit authorization and decision
audit, or append completion reports.

Caller-supplied policy dicts are never activation authority: dispatch must call
:func:`load_active_policy` (which recomputes the approved evidence hash and the
decision-log activation) every time. The R18 duplicate-video guard lives in
``lib/production_video_guard.py`` (separate owner) and is not reimplemented here.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from lib.production_request import digest
from lib.shot_contract import CRITICAL_PREDICATES
from schemas.artifacts import load_schema

ACTIVATION_SUBJECT = 'Production autonomy policy'
ACTIVATION_CATEGORY = 'approval_policy'
EVIDENCE_KIND = 'openart_autonomy_policy'
POLICY_ARTIFACT = 'artifacts/autonomy_policy.json'
BUDGET_COPY = ('Grok CLI uses your existing subscription; its remaining quota is unknown and is not a '
               'cost ceiling. Auto-continue never uses paid Grok API calls and never buys plans, '
               'credits or top-ups.')
GROK_MODEL_POLICY = 'cli_managed_media_unreported'
CAST_ROLES = frozenset({'identity_reference', 'start_frame', 'payoff_board'})
NON_PREAUTH_STAGES = frozenset({'publish'})
CHECKPOINT_STAGES_ERROR = 'checkpoint stages are not actual gated pipeline stages'


class AutonomyError(ValueError):
    """A policy-derived action is not provably inside the approved envelope."""


class StandingSetupRequired(AutonomyError):
    """Actual one-time account/source authority is missing or insufficient."""


def _pipeline_checkpoint_stages(root):
    from lib.pipeline_loader import load_pipeline_readonly
    marker = _json(Path(root) / 'project.json')
    manifest = load_pipeline_readonly(marker['pipeline_type'])
    gated = {stage['name'] for stage in manifest['stages']
             if stage.get('human_approval_default') is True and stage['name'] not in NON_PREAUTH_STAGES}
    return {stage['name'] for stage in manifest['stages']}, gated


def _validate_checkpoint_stages(root, stages):
    """Return the manifest's preauthorizable gates; reject every other stage."""
    _, gated = _pipeline_checkpoint_stages(root)
    if not set(stages).issubset(gated):
        raise StandingSetupRequired(CHECKPOINT_STAGES_ERROR)
    return gated


def _bound_authorization(root, record, identity_id):
    binding = record.get('authorization')
    if not isinstance(binding, dict) or set(binding) != {'path', 'sha256'}:
        raise StandingSetupRequired('retained actual creator authorization required')
    path = _inside(root, binding['path'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != binding['sha256']:
        raise StandingSetupRequired('creator authorization missing or changed')
    evidence = _json(path)
    _authorization_evidence(record, identity_id, evidence)


def _authorization_evidence(record, identity_id, evidence):
    """Same exact creator subject check for live bindings and frozen originals."""
    subject = digest({key: value for key, value in record.items() if key != 'authorization'})
    if (not isinstance(evidence, dict) or evidence.get('version') != '1.0'
            or evidence.get('kind') != 'creator_authorization' or evidence.get('origin') != 'user_message'
            or evidence.get('identity_id') != identity_id or evidence.get('subject_sha256') != subject
            or not isinstance(evidence.get('message_id'), str) or not evidence['message_id'].strip()
            or not isinstance(evidence.get('text'), str) or not evidence['text'].strip()):
        raise StandingSetupRequired('actual creator-message evidence does not bind exact authority')


def _bound_document(root, binding, label):
    if not isinstance(binding, dict) or set(binding) != {'path', 'sha256'}:
        raise StandingSetupRequired(label + ' needs exact retained binding')
    path = _inside(root, binding['path'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != binding['sha256']:
        raise StandingSetupRequired(label + ' missing or changed')
    return _json(path)


def _native_audio_off_record(root, policy, sha, record):
    """Only exact creator-approved MCP audio-off; no other control authority."""
    if not isinstance(record, dict):
        raise AutonomyError('native audio-off approval must be an exact authority object')
    keys = {'version', 'kind', 'project_id', 'story_revision', 'policy_sha256',
            'shots', 'route', 'controls', 'approved_packet', 'form_sha256', 'authorization'}
    route = record.get('route', {})
    shots = record.get('shots')
    if (set(record) != keys or record['version'] != '1.0'
            or record['kind'] != 'native_audio_off_approval'
            or record['project_id'] != policy['project_id']
            or record['story_revision'] != policy['story_revision']
            or record['policy_sha256'] != sha
            or not isinstance(shots, list) or not shots or any(not isinstance(s, str) for s in shots)
            or len(set(shots)) != len(shots) or not set(shots) <= set(policy['shots'])
            or not isinstance(route, dict) or set(route) != {'provider', 'model', 'mode'}
            or route['provider'] != 'openart_mcp'
            or record['controls'] != {'generateAudio': False}
            or record['controls'].get('generateAudio') is not False
            or not policy.get('activation_basis')):
        raise AutonomyError('native audio-off approval differs from exact project/policy/shots/route/false control')
    spec = _provider_spec(policy, route['provider'])
    if not _openart_route_approved(spec, route['model'], route['mode']):
        raise AutonomyError('native audio-off route is outside approved policy')
    baselines = retained_baselines(root, policy)
    for shot in shots:
        baseline = baselines[shot]['planned_request_template']['inputs']
        if (not _intent_allows(policy, shot, route['provider'], route['model'])
                or any(key in baseline or key in baseline.get('native_params', {})
                       for key in ('audio', 'generateAudio', 'generateSound', 'native_audio'))):
            raise AutonomyError('native audio-off cannot change retained audio controls or route intent')
    for name in ('audio', 'generateAudio', 'generateSound', 'native_audio'):
        lock = policy['locked']['controls'].get(name)
        if lock is not None and (('value' in lock and lock['value'] is not False)
                or 'value_sha256' in lock and lock['value_sha256'] != hashlib.sha256(str(False).encode()).hexdigest()):
            raise AutonomyError('native audio-off conflicts with retained control lock')
    from lib import openart_mcp as mcp
    profile = mcp.load_profile(route['model'], route['mode'], require='supported')
    schema = profile['form']['jsonSchema'].get('properties', {}).get('generateAudio')
    if record['form_sha256'] != profile['form_sha256'] or not isinstance(schema, dict):
        raise AutonomyError('native audio-off requires exact supported form switch')
    try:
        Draft202012Validator(schema).validate(False)
    except ValidationError as exc:
        raise AutonomyError('native audio-off is outside exact native schema') from exc


def adopt_native_audio_off_approval(project_dir, *, expected_policy_sha256, approval):
    """Retain an already-approved exact false-only MCP setting, without calls.

    The creator subject binds the packet, current project/policy, named shots,
    route and form. Original policies, scopes, requests and counters stay frozen.
    """
    from lib import production_execution as execution, production_images as images
    root = Path(project_dir).resolve()
    with execution._lock(root):
        policy, sha, _ = require_active_policy(root)
        if sha != expected_policy_sha256:
            raise AutonomyError('native audio-off requires exact active policy SHA')
        record = _bound_document(root, approval, 'native audio-off approval')
        _native_audio_off_record(root, policy, sha, record)
        _bound_document(root, record['approved_packet'], 'approved native audio-off packet')
        _bound_authorization(root, record, policy['activation_basis']['identity_id'])
        path = root / 'production_scopes.json'
        data = _json(path)
        approvals = data.setdefault('native_audio_off_approvals', [])
        if data.get('version') != '1.0' or not isinstance(approvals, list):
            raise AutonomyError('invalid production scopes native audio-off approvals')
        if approval in approvals:
            return {'approval': copy.deepcopy(approval), 'replayed': True, 'provider_calls': 0}
        for binding in approvals:
            other = _bound_document(root, binding, 'native audio-off approval')
            if other['route'] == record['route'] and set(other['shots']).intersection(record['shots']):
                raise AutonomyError('native audio-off approval already retained for exact shot/route')
        approvals.append(copy.deepcopy(approval))
        images._write_image_scopes(root, data)
        return {'approval': copy.deepcopy(approval), 'replayed': False, 'provider_calls': 0}


def _native_audio_off_proof(root, policy, sha, shot_id, inputs, provider, *, proof=None, historical=False):
    """New admission reads authority; originals replay their exact frozen proof."""
    params = inputs.get('native_params', {})
    if (provider != 'openart_mcp' or not isinstance(params, dict)
            or params.get('generateAudio') is not False):
        if proof is not None:
            raise AutonomyError('native audio-off proof differs from original request control')
        return None
    baseline = retained_baselines(root, policy)[shot_id]['planned_request_template']['inputs']
    if 'generateAudio' in baseline.get('native_params', {}):
        if proof is not None:
            raise AutonomyError('native audio-off proof cannot change retained audio control')
        return None
    if not historical:
        data = _json(Path(root) / 'production_scopes.json')
        bindings = data.get('native_audio_off_approvals', [])
        if not isinstance(bindings, list):
            raise AutonomyError('invalid production scopes native audio-off approvals')
        matches = []
        for binding in bindings:
            record = _bound_document(root, binding, 'native audio-off approval')
            if shot_id in record.get('shots', []) and record.get('route') == {
                    'provider': provider, 'model': inputs.get('model'), 'mode': inputs.get('mode')}:
                _native_audio_off_record(root, policy, sha, record)
                _bound_document(root, record['approved_packet'], 'approved native audio-off packet')
                _bound_authorization(root, record, policy['activation_basis']['identity_id'])
                def capture(item):
                    return {**item, 'content': _inside(root, item['path']).read_text()}
                matches.append({'approval': capture(binding), 'authorization': capture(record['authorization'])})
        if len(matches) != 1 or proof is not None and proof != matches[0]:
            raise AutonomyError('exact retained creator native audio-off approval required')
        return matches[0]
    if not isinstance(proof, dict) or set(proof) != {'approval', 'authorization'}:
        raise AutonomyError('original native audio-off requires frozen scope authority')
    records = {}
    for key, item in proof.items():
        if (not isinstance(item, dict) or set(item) != {'path', 'sha256', 'content'}
                or not isinstance(item['content'], str)
                or hashlib.sha256(item['content'].encode()).hexdigest() != item['sha256']):
            raise AutonomyError('frozen native audio-off authority bytes differ')
        _inside(root, item['path'])
        records[key] = json.loads(item['content'])
    record = records['approval']
    _native_audio_off_record(root, policy, sha, record)
    if (shot_id not in record['shots'] or record['route'] != {
            'provider': provider, 'model': inputs.get('model'), 'mode': inputs.get('mode')}
            or record['authorization'] != {key: proof['authorization'][key] for key in ('path', 'sha256')}):
        raise AutonomyError('original native audio-off scope/request authority differs')
    _authorization_evidence(record, policy['activation_basis']['identity_id'], records['authorization'])
    return copy.deepcopy(proof)


def _validate_generated_board_source(root, asset, *, expected):
    """Pending slots resolve only to a same-project reviewed canonical original."""
    from lib import production_boards as boards, production_execution as execution
    aid = expected['id']
    pack = boards.load_board_pack(root)
    declaration = next((a for a in pack['specification'].get('motion_binding', {}).get('contract_template', {}).get('assets', [])
                        if a['id'] == aid), None)
    if (declaration is None or aid not in pack['prompt_pack']
            or any(asset.get(key) != expected.get(key) or declaration.get(key) != expected.get(key)
                   for key in ('id', 'role', 'cast_ids'))):
        raise AutonomyError('generated source differs from exact retained board declaration')
    record = execution.resolve_board_input(root, {'asset_id': aid})
    binding = pack['board_bindings'][aid]
    # Declared endpoint reuse: resolve_board_input above re-validated the exact
    # current selected attempt/output/review/outgoing frame and path. Plain reuse
    # (no declared upstream_source on node, template and binding) stays refused.
    source = pack['prompt_pack'][aid].get('upstream_source')
    declared_reuse = (binding.get('origin') == 'reuse' and isinstance(source, dict)
        and source.get('role') == 'outgoing_frame'
        and declaration.get('upstream_source') == source == expected.get('upstream_source')
        and isinstance(binding.get('upstream_source'), dict)
        and binding['upstream_source'].get('shot_id') == source.get('shot_id'))
    if ((binding.get('origin') != 'canonical_image_import' and not declared_reuse)
            or record['sha256'] != asset.get('sha256')
            or _inside(root, record['path']) != _inside(root, asset.get('path', ''))
            or boards._review_blockers(pack, aid, asset['role'])):
        raise AutonomyError('generated source is not reviewed same-run original board bytes')


def _materialized_projection(root, projection):
    from lib.production_execution import load_shot_contract
    current = {a['id']: a for a in load_shot_contract(root)['assets']}
    value = copy.deepcopy(projection)
    for index, source in enumerate(value['assets']):
        if source.get('preparation_pending'):
            asset = current.get(source['id'])
            if not asset or asset.get('preparation_pending'):
                raise AutonomyError('required board slot remains pending')
            _validate_generated_board_source(root, asset, expected=source)
            value['assets'][index] = {k: copy.deepcopy(v) for k, v in asset.items() if k != 'review'}
    return value


def _materialized_manifest_requirements(root, shot_id, baseline, materialized):
    """Expected manifest obligations after exact reviewed board promotion.

    Only the retained package's authored handoff for this scene is admitted.
    Each endpoint is either a same-run board original validated above or the
    retained package's own reviewed, byte-current reuse reference; aliases and
    scene ownership follow the canonical board row builder.
    """
    from lib import production_boards as boards, production_images as images
    from lib.production_execution import load_shot_contract
    from lib.shot_contract import required_asset_ids
    pack = boards.load_board_pack(root)
    scopes = root / 'production_scopes.json'
    plan = (json.loads(scopes.read_text()).get('image_envelope') or {}).get('definition', {}).get('board_plan') \
        if scopes.exists() else None
    binding = pack.get('package_binding')
    # The pack pointer is mutable; only the package frozen in the retained image
    # envelope that produced these boards carries authored handoff authority.
    if not binding or not plan or any(binding.get(k) != plan.get(k) for k in ('path', 'sha256')):
        return copy.deepcopy(baseline), {row['id']: row for row in baseline['assets']}
    # Authority comes from the hash-bound retained package bytes; the live pack
    # specification is only co-located and may not widen handoff scope.
    package = json.loads(images._bound_file(root, pack['package_binding'], 'production package').read_text())
    if 'authoring_form' in package or 'compact' in package:
        from lib.production_request import _expand_compact_package
        package = _expand_compact_package(package)
    spec = package['boards']
    contract = load_shot_contract(root)
    roles = {a['id']: a['role'] for a in contract['assets']}
    references = {r['id']: r for r in spec['references']}
    expected = copy.deepcopy(baseline)
    rows = {row['id']: row for row in expected['assets']}

    def row(alias):
        base = alias[:-len('--' + shot_id)] if alias.endswith('--' + shot_id) else alias
        users = [s['id'] for s in contract['shots'] if base in required_asset_ids(contract, s)]
        users = users or [spec['shots'][0]['shot_id']]
        if shot_id not in users or alias != boards.motion_reference_alias(spec, base, shot_id):
            return None
        binding = pack['board_bindings'].get(base)
        if base in materialized:
            return base, {'id': alias, 'type': 'image', 'path': materialized[base]['path'], 'scene_id': shot_id,
                          'source_tool': binding.get('route', 'native_imagegen')}
        reference = references.get(base)
        if (not binding or binding.get('origin') != 'reuse' or not reference
                or any(binding[k] != reference[k] for k in ('path', 'sha256'))
                or not boards._binding_current(root, pack, base)
                or base not in roles or boards._review_blockers(pack, base, roles[base])):
            return None
        return base, {'id': alias, 'type': 'image', 'path': str(boards._inside(root, binding['path']).relative_to(root)),
                      'scene_id': shot_id, 'source_tool': 'provided'}

    for aid, handoff in spec.get('motion_binding', {}).get('motion_handoffs', {}).items():
        if handoff.get('scene_id') != shot_id or aid in expected['motion_handoffs']:
            continue
        endpoints = [row(alias) for alias in (aid, handoff.get('pinned_final_frame', {}).get('asset_id'))]
        # At least one endpoint must be a same-run promotion; pure reuse was baseline.
        if not all(endpoints) or not any(base in materialized for base, _ in endpoints):
            continue
        expected['motion_handoffs'][aid] = copy.deepcopy(handoff)
        for _, endpoint in endpoints:
            rows.setdefault(endpoint['id'], endpoint)
    return expected, rows


def _validate_standing_basis(root, policy, baselines, *, check_first_pass_capacity=True):
    try:
        return _validate_standing_basis_impl(root, policy, baselines,
            check_first_pass_capacity=check_first_pass_capacity)
    except StandingSetupRequired:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StandingSetupRequired('standing setup cannot be validated: ' + str(exc)) from exc


IMAGE_POLICY_FALLBACK = 'image_policy_fallback'


def _image_policy_fallback_term(term):
    """Shape of the optional creator-bound, run-local OpenArt image exception.

    ``forms`` retains the exact current ``openart_model_form_get`` jsonSchema per
    admitted mode (text2image for text boards, image2image for reference boards);
    the creator's command digest binds them. ``params`` are extra form fields the
    producer chose; prompt and visualReferences always come from the native call.
    """
    forms = term.get('forms') if isinstance(term, dict) else None
    params = term.get('params', {}) if isinstance(term, dict) else None
    return (isinstance(term, dict) and set(term) - {'params'} == {'provider', 'model', 'forms'}
            and term['provider'] == 'openart_mcp'
            and isinstance(term['model'], str) and bool(term['model'].strip())
            and isinstance(forms, dict) and bool(forms) and set(forms) <= {'text2image', 'image2image'}
            and all(isinstance(f, dict) and f.get('type') == 'object' for f in forms.values())
            and isinstance(params, dict) and not {'prompt', 'visualReferences'} & set(params))


def standing_image_policy_fallback(root):
    """``(term, openart_mcp spec)`` from the active run's exact command, else None."""
    policy, _, _ = require_active_policy(root)
    basis = policy.get('activation_basis') or {}
    if not basis.get('run_command'):
        return None
    command = _bound_document(root, basis['run_command'], 'run command')
    term = command.get(IMAGE_POLICY_FALLBACK)
    if term is None:
        return None
    _bound_authorization(root, command, basis.get('identity_id'))
    spec = _provider_spec(policy, 'openart_mcp')
    if not _image_policy_fallback_term(term) or not spec:
        raise StandingSetupRequired('image policy fallback needs its exact term and the standing OpenArt MCP account')
    return copy.deepcopy(term), copy.deepcopy(spec)


def _validate_standing_source_role(source, transfer, provider):
    """The same explicit source-role permission for prospective and retained work."""
    if source['role'] not in transfer['roles']:
        raise StandingSetupRequired('source role outside transport permission: asset '
            + str(source['id']) + ', role ' + source['role'] + ', provider ' + provider
            + '; permitted roles: ' + ', '.join(transfer['roles']))


def _validate_standing_basis_impl(root, policy, baselines, *, check_first_pass_capacity=True):
    """Validate declared actual message provenance and containment, never invent it."""
    basis = policy.get('activation_basis', {})
    identity = basis.get('identity_id')
    setup = _bound_document(root, basis.get('standing_setup'), 'standing setup')
    command = _bound_document(root, basis.get('run_command'), 'run command')
    marker = _json(Path(root) / 'project.json')
    required = {'version', 'identity_id', 'enabled', 'providers', 'provider_accounts', 'caps',
                'approved_source_sha256', 'checkpoint_stages', 'flex', 'source_transfer',
                'required_permissions', 'authorization'}
    if (not isinstance(setup, dict) or not required.issubset(setup)
            or setup.get('version') != '1.0' or setup.get('enabled') is not True
            or setup.get('identity_id') != identity):
        raise StandingSetupRequired('complete enabled one-time identity setup required')
    if (not isinstance(command, dict) or set(command) - {IMAGE_POLICY_FALLBACK} != {'version', 'identity_id', 'project_id',
            'story_revision', 'command', 'standing_setup_sha256', 'authorization'}
            or IMAGE_POLICY_FALLBACK in command and not _image_policy_fallback_term(command[IMAGE_POLICY_FALLBACK])
            or command.get('version') != '1.0' or command.get('identity_id') != identity
            or command.get('command') != 'make_video'
            or command.get('standing_setup_sha256') != basis['standing_setup']['sha256']
            or any(command.get(key) != marker.get(key) or policy.get(key) != marker.get(key)
                   for key in ('project_id', 'story_revision'))):
        raise StandingSetupRequired('actual run command must bind identity, setup and current project/story')
    if marker.get('identity_id', identity) != identity:
        raise StandingSetupRequired('run identity differs from project identity')
    _bound_authorization(root, setup, identity); _bound_authorization(root, command, identity)
    if policy.get('planning_mode') != 'shot_local_v1':
        raise StandingSetupRequired('standing runs require prospective local planning')
    if basis.get('plan_sha256') != digest(baselines):
        raise StandingSetupRequired('internally retained exact plan differs from run binding')
    profiles = [baseline.get('export_profile') for baseline in baselines.values()]
    if any(profile is not None for profile in profiles) and len(set(profiles)) != 1:
        raise StandingSetupRequired('retained export profiles disagree or are incomplete')
    if any(provider not in setup['providers'] for provider in policy['providers']):
        raise StandingSetupRequired('provider/account/billing terms exceed standing setup')
    accounts = setup['provider_accounts']
    if not isinstance(accounts, dict) or any(not isinstance(accounts.get(provider['id']), str)
            or not accounts[provider['id']].strip() for provider in policy['providers']):
        raise StandingSetupRequired('named configured account context required for every provider')
    if (not isinstance(setup['caps'], dict) or any(type(setup['caps'].get(key)) is not int
            or policy['caps'][key] > setup['caps'][key] for key in policy['caps'])
            or not set(policy['checkpoint_stages']).issubset(setup['checkpoint_stages'])
            or policy['flex'] != setup['flex']):
        raise StandingSetupRequired('caps, checkpoint stages or flex exceed standing setup')
    if not isinstance(setup['approved_source_sha256'], list) or not isinstance(setup['required_permissions'], list):
        raise StandingSetupRequired('explicit original-source and permission inventory required')
    for permission in setup['required_permissions']:
        retained = _bound_document(root, permission, 'required artist/source permission')
        if (retained.get('status') != 'approved' or retained.get('identity_id') != identity
                or retained.get('source_sha256') not in setup['approved_source_sha256']):
            raise StandingSetupRequired('required artist/source permission is unresolved')
    transfers = setup['source_transfer']
    if not isinstance(transfers, list):
        raise StandingSetupRequired('explicit bounded source transfer permission required')
    for provider in policy['providers']:
        routes = [term for term in transfers if term.get('provider') == provider['id']
                  and term.get('account') == accounts[provider['id']]]
        if len(routes) != 1:
            raise StandingSetupRequired('one exact source-transfer term required for each transport/account')
        term = routes[0]
        if (not isinstance(term.get('roles'), list) or type(term.get('generated_board_sources')) is not bool
                or any(type(term.get(key)) is not int or term[key] < 1
                       for key in ('max_files', 'max_file_bytes', 'max_batch_bytes', 'max_batches'))):
            raise StandingSetupRequired('source transfer needs roles and positive file/batch limits')
        for baseline in baselines.values():
            if check_first_pass_capacity:
                from lib import production_execution as execution
                def source_count(value, key=None):
                    # Match _paths occurrence counting: shared bytes submitted
                    # in two native fields consume two file slots, even before
                    # declared board/upstream bytes have been produced.
                    if key == 'input_assets':
                        return len(value)
                    if key in execution.INPUT_PATH_KEYS:
                        return sum(source_count(item, key) for item in value) if isinstance(value, list) else 1
                    if isinstance(value, dict):
                        return sum(source_count(item, name) for name, item in value.items())
                    if isinstance(value, list):
                        return sum(source_count(item) for item in value)
                    return 0
                if source_count(execution._clean(baseline['planned_request_template']['inputs'])) > term['max_files']:
                    raise StandingSetupRequired('planned source batch exceeds retained transfer max_files limit')
            for asset in baseline['projection']['assets']:
                if asset.get('upstream_source'):
                    if not term['generated_board_sources']:
                        raise StandingSetupRequired('selected run-generated source transfer is not permitted')
                    continue
                _validate_standing_source_role(asset, term, provider['id'])
                if asset.get('preparation_pending'):
                    if not term['generated_board_sources']:
                        raise StandingSetupRequired('declared generated board transfer not permitted')
                    from lib.production_boards import load_board_pack
                    pack = load_board_pack(root)
                    declared = [a for a in pack['specification']['motion_binding']['contract_template']['assets']
                                if a['id'] == asset['id']]
                    if len(declared) != 1 or asset['id'] not in pack['prompt_pack'] or any(
                            declared[0].get(key) != asset.get(key) for key in ('id', 'role', 'cast_ids')):
                        raise StandingSetupRequired('pending source has no exact same-run declared node')
                elif asset['sha256'] not in setup['approved_source_sha256']:
                    if not term['generated_board_sources']:
                        raise StandingSetupRequired('original source hash outside setup permission')
                    _validate_generated_board_source(root, asset, expected=asset)
    from lib import production_boards as boards, production_images as images
    if (Path(root) / boards.ARTIFACT).exists() and boards.load_board_pack(root)['specification']['image_nodes']:
        images._standing_image_definition(root, policy,
            check_first_pass_capacity=check_first_pass_capacity)
    return setup, command


def activate_standing_run(project_dir, *, policy, baselines, standing_setup, run_command, identity_id):
    """Bind actual standing media authority + actual run command to authored work.

    Uses existing policy/evidence/decision owners only. It supplies no human
    semantic verdict or checkpoint approval. Named Grok account context is
    configuration authority; its adapter exposes no runtime account attestation.
    """
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    with execution._lock(root):
        path = root / POLICY_ARTIFACT
        if path.exists():
            retained = _json(path)
            basis = retained.get('activation_basis', {})
            ignored = {'evidence', 'shots', 'activation_basis', 'planning_mode'}
            if (basis.get('identity_id') == identity_id and basis.get('standing_setup') == standing_setup
                    and basis.get('run_command') == run_command
                    and {k: v for k, v in retained.items() if k not in ignored}
                        == {k: v for k, v in policy.items() if k not in ignored}):
                active, sha, decision_id, reasons = load_active_policy(root)
                if active is not None:
                    # Resume uses the retained internally derived plan. Current
                    # observed board bytes never mint a replacement activation.
                    return {'policy_sha256': sha, 'decision_id': decision_id, 'replayed': True, 'provider_calls': 0}
        candidate = copy.deepcopy(policy)
        candidate['planning_mode'] = 'shot_local_v1'
        candidate['shots'] = {sid: baseline_descriptor(value, root) for sid, value in baselines.items()}
        candidate['activation_basis'] = {'version': '1.0', 'kind': 'standing_run', 'identity_id': identity_id,
            'standing_setup': copy.deepcopy(standing_setup), 'run_command': copy.deepcopy(run_command),
            'plan_sha256': digest(baselines)}
        candidate['evidence'] = {'path': 'approvals/autonomy-policy-' + '0' * 64 + '.json', 'sha256': '0' * 64}
        if schema_errors(candidate):
            raise StandingSetupRequired('standing policy schema is incomplete: ' + ', '.join(schema_errors(candidate)))
        _validate_checkpoint_stages(root, candidate['checkpoint_stages'])
        setup, _ = _validate_standing_basis(root, candidate, baselines)
        controls = _standing_episode_controls(root, setup, candidate['activation_basis'])
        _validate_local_protected_planning(root, candidate, baselines)
        if is_benchmark(root, _json(root / 'project.json')) or policy_conflicts(candidate, baselines):
            raise StandingSetupRequired('benchmark or contradictory standing scope cannot activate')
        for sid, baseline in baselines.items():
            if current_projection(root, sid) != baseline['projection']:
                raise StandingSetupRequired('activation baseline is not actual current authored planning')
        if set(baselines) != {s['id'] for s in execution.load_shot_contract(root)['shots']}:
            raise StandingSetupRequired('standing activation must bind every shot in the actual retained run plan')
        _validate_standing_route_treatments(candidate, baselines)
        counts = root_attempt_counts(root, candidate)
        if (counts['total'] > candidate['caps']['max_total_attempts']
                or any(value > candidate['caps']['max_attempts_per_shot'] for value in counts['per_shot'].values())):
            raise StandingSetupRequired('retained cumulative usage exceeds standing allowance')
        missing_shots = [sid for sid in baselines if counts['per_shot'].get(sid, 0) == 0]
        if counts['total'] + len(missing_shots) > candidate['caps']['max_total_attempts']:
            raise StandingSetupRequired('fixed motion allowance cannot cover retained usage plus planned first-pass shots')
        sha = policy_sha256(candidate, baselines)
        content = evidence_content(candidate, baselines)
        payload = json.dumps(content, indent=2, allow_nan=False).encode()
        candidate['evidence'] = {'path': f'approvals/autonomy-policy-{sha}.json', 'sha256': hashlib.sha256(payload).hexdigest()}
        path = root / POLICY_ARTIFACT
        replayed = path.exists()
        if path.exists():
            current = _json(path)
            if current != candidate:
                raise StandingSetupRequired('existing project policy differs; explicit amendment required')
            active, got, decision_id, reasons = load_active_policy(root)
            if active is not None and got == sha:
                return {'policy_sha256': sha, 'decision_id': decision_id, 'replayed': True, 'provider_calls': 0}
        log_path = execution._artifact_path(root, 'decision_log.json')
        log = _json(log_path) if log_path.exists() else {'version': '1.0', 'project_id': candidate['project_id'], 'decisions': []}
        if log['project_id'] != candidate['project_id'] or (root / 'decision_log.json').exists() and (root / 'artifacts/decision_log.json').exists():
            raise StandingSetupRequired('canonical decision log identity/alias conflict')
        if log_path.exists():
            _decision_entries(root)  # Canonical schema, identity and unique-ID check before publication.
        else:
            Draft202012Validator(load_schema('decision_log')).validate(log)
        decision_id = 'standing-' + sha[:24]
        choice = 'auto_continue:' + sha
        if any(d.get('decision_id') == decision_id or (d.get('category') == ACTIVATION_CATEGORY
                and d.get('subject') == ACTIVATION_SUBJECT and d.get('selected') == choice)
                for d in log['decisions']):
            raise StandingSetupRequired('existing activation decision is invalid or revoked')
        if controls is not None:
            from lib import episode_production_controls as episode
            record = _standing_controls_initial(root, candidate, controls)
            if record is None:
                if path.exists():
                    raise StandingSetupRequired('interrupted standing activation lacks its initial episode controls')
                episode._record_episode_controls_locked(root, controls[0], evidence=controls[1])
                record = _standing_controls_initial(root, candidate, controls)
            decision_id += '-controls-' + digest(record)
        if any(d.get('decision_id') == decision_id for d in log['decisions']):
            raise StandingSetupRequired('existing activation decision ID conflicts with retained episode controls')
        log['decisions'].append({'decision_id': decision_id, 'stage': 'proposal', 'category': ACTIVATION_CATEGORY,
            'subject': ACTIVATION_SUBJECT, 'selected': choice, 'user_approved': True,
            'reason': 'Exact media run authorized by retained creator standing setup and actual run command; no human plan or semantic review claimed.',
            'options_considered': [{'option_id': choice, 'label': 'Retained standing media run', 'score': 1,
                'reason': 'Within the actual configured account, source and cumulative allowance.'}]})
        Draft202012Validator(load_schema('decision_log')).validate(log)
        evidence_path = root / candidate['evidence']['path']
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        if evidence_path.exists():
            if evidence_path.read_bytes() != payload:
                raise StandingSetupRequired('retained activation evidence changed')
        else:
            execution._write_new(evidence_path, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            execution._write_new(path, candidate)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = log_path.with_suffix('.json.activation-tmp')
        temporary.write_text(json.dumps(log, indent=2)); temporary.replace(log_path)
        return {'policy_sha256': sha, 'decision_id': decision_id, 'replayed': replayed, 'provider_calls': 0}


def _standing_episode_controls(root, setup, basis):
    """Only the explicit complete snapshot authenticated in the standing setup."""
    if 'episode_controls' not in setup:
        return None
    from lib import episode_production_controls as episode
    try:
        # Retained standing authority with older broader controls activates
        # only the narrower one-shot runtime; the setup itself is never rewritten.
        payload = {**episode._check_controls(setup['episode_controls']),
                   'max_generations_per_shot': episode.ONE_SHOT_LIMIT,
                   'alternate_repair': 'disabled', 'access_fallback': False}
        authorization = _bound_document(root, setup['authorization'], 'standing creator authorization')
        evidence = episode._evidence(root, {**basis['standing_setup'],
            'approved_by': 'creator:' + basis['identity_id'] + ':' + authorization['message_id']})
        return payload, evidence
    except episode.EpisodeControlsError as exc:
        raise StandingSetupRequired(str(exc)) from exc


def _standing_controls_initial(root, policy, controls):
    """Exact first current-story record; later amendments/restrictions stay intact."""
    from lib import episode_production_controls as episode
    try:
        data = episode._load(root)
        if data is None:
            return None
        if data['project_id'] != policy['project_id']:
            raise StandingSetupRequired('standing episode controls project differs')
        records = [record for record in data['records'] if record['story_revision'] == policy['story_revision']]
        if not records:
            return None
        first = records[0]
        historical = {**first.get('controls', {}), **{key: controls[0][key] for key in
                      ('max_generations_per_shot', 'alternate_repair', 'access_fallback')}}
        # Older activations retain their recorded initial controls unchanged.
        if (first['kind'] != 'controls' or first['evidence'] != controls[1]
                or first['controls'] != controls[0] and historical != controls[0]):
            raise StandingSetupRequired('standing initial episode controls differ from authenticated setup')
        return first
    except episode.EpisodeControlsError as exc:
        raise StandingSetupRequired(str(exc)) from exc


def _validate_standing_controls_activation(root, policy, sha, decision, *, setup):
    """Check new activation's initial record, including a linked frame successor."""
    prefix = 'standing-' + sha[:24] + '-controls-'
    if not decision or not decision.get('decision_id', '').startswith(prefix):
        return  # Older activations never retrofit ignored optional setup fields.
    controls = _standing_episode_controls(root, setup, policy['activation_basis'])
    initial = _standing_controls_initial(root, policy, controls) if controls is not None else None
    if initial is None or decision['decision_id'] != prefix + digest(initial):
        raise StandingSetupRequired('standing activation initial episode controls missing or changed')


def _require_unstarted_standing_run(root):
    """No counter exclusions or absent public journal can hide a reservation."""
    from lib import production_images as images, provider_credit_ledger as ledger
    from tools import _openart_cli as cli
    if images._load(root)['calls']:
        raise StandingSetupRequired('checkpoint reconciliation requires zero native media calls/reservations')
    for directory in ('production_attempts', 'openart_mcp/attempts', 'openart_mcp/uploads'):
        if any(path.is_file() for path in (root / directory).rglob('*')):
            raise StandingSetupRequired('checkpoint reconciliation requires zero media attempts/uploads')
    path = root / 'production_scopes.json'
    if path.exists() and _json(path) != {'version': '1.0', 'scopes': []}:
        raise StandingSetupRequired('checkpoint reconciliation cannot replace existing media scopes/envelopes')
    if any(list((root / 'artifacts').glob(pattern)) for pattern in
           ('upload_approval-*.json', 'openart_mcp_upload_authorization-*.json')):
        raise StandingSetupRequired('checkpoint reconciliation cannot replace retained upload authority')
    snapshot = ledger.read_existing_snapshot()
    reservations = list(snapshot['reservations']) + list(snapshot['unpriced_reservations'])
    ids = set()
    for row in reservations:
        binding = json.loads(row['binding_json'])
        if not isinstance(binding.get('project_root'), str) or not Path(binding['project_root']).is_absolute():
            raise StandingSetupRequired('private reservation origin cannot be validated')
        ids.add(row['attempt_id'])
        if Path(binding['project_root']).resolve() == root:
            raise StandingSetupRequired('checkpoint reconciliation requires zero private media reservations')
    if any(row['attempt_id'] not in ids for key in ('outbox', 'unpriced_outbox', 'claims', 'unpriced_claims')
           for row in snapshot[key]):
        raise StandingSetupRequired('private media reservation/outbox origin is missing')
    private = cli.state_dir(create=False)
    # CLI uploads bind the project through retained local upload authority above.
    # The same cast/source may have unrelated uploads in another episode.
    for path in (private / 'mcp-uploads').glob('*.json'):
        if Path(_json(path)['project_root']).resolve() == root:
            raise StandingSetupRequired('checkpoint reconciliation requires zero retained MCP uploads')


def reconcile_standing_checkpoint_stages(project_dir, *, expected_policy_sha256):
    """Remove only ungated stages from one invalid, unstarted standing activation.

    This is not a general amendment. Original policy/evidence/decision bytes stay
    frozen, all other authority and planning checks remain binding, and no media
    allowance, route, account or source changes. Successful replay is read-only,
    including after production progress; stale/revoked successors fail closed.
    """
    from lib import production_execution as execution
    import os
    root = Path(project_dir).resolve()
    try:
        with execution._lock(root):
            if (not isinstance(expected_policy_sha256, str) or len(expected_policy_sha256) != 64
                    or any(c not in '0123456789abcdef' for c in expected_policy_sha256)):
                raise StandingSetupRequired('exact expected original policy SHA required')
            path = root / POLICY_ARTIFACT
            raw = path.read_bytes()
            current = json.loads(raw)
            log_path = execution._artifact_path(root, 'decision_log.json')
            if (root / 'decision_log.json').exists() and (root / 'artifacts/decision_log.json').exists():
                raise StandingSetupRequired('canonical decision log alias conflict')
            log_raw = log_path.read_bytes()
            log = json.loads(log_raw)
            _decision_entries(root)
            prefix = f'approvals/autonomy-checkpoint-reconciliation-{expected_policy_sha256}'
            record_path = root / (prefix + '.json')
            policy_snapshot = root / (prefix + '-policy.json')
            log_snapshot = root / (prefix + '-decision-log.json')
            replayed = record_path.exists()
            original_raw = policy_snapshot.read_bytes() if replayed else raw
            original_log_raw = log_snapshot.read_bytes() if replayed else log_raw
            original = json.loads(original_raw)
            original_log = json.loads(original_log_raw)
            if schema_errors(original) or not original.get('activation_basis'):
                raise StandingSetupRequired('exact original standing activation required')
            baselines = retained_baselines(root, original)
            old_sha = policy_sha256(original, baselines)
            if old_sha != expected_policy_sha256:
                raise StandingSetupRequired('current original policy differs from expected SHA')
            known, gated = _pipeline_checkpoint_stages(root)
            if not set(original['checkpoint_stages']).issubset(known):
                raise StandingSetupRequired('original checkpoint stage is absent from current pipeline')
            removed = [stage for stage in original['checkpoint_stages'] if stage not in gated]
            if not removed:
                raise StandingSetupRequired('original policy has no ungated checkpoint defect')
            decision = next((entry for entry in reversed(original_log['decisions'])
                if entry.get('category') == ACTIVATION_CATEGORY and entry.get('subject') == ACTIVATION_SUBJECT), None)
            if (not decision or decision.get('selected') != 'auto_continue:' + old_sha
                    or decision.get('user_approved') is not True
                    or decision.get('decision_id') != 'standing-' + old_sha[:24]):
                raise StandingSetupRequired('original standing activation is missing, revoked or superseded')
            candidate = copy.deepcopy(original)
            candidate['checkpoint_stages'] = [stage for stage in original['checkpoint_stages'] if stage in gated]
            content = evidence_content(candidate, baselines)
            sha = policy_sha256(candidate, baselines)
            payload = json.dumps(content, indent=2, allow_nan=False).encode()
            candidate['evidence'] = {'path': f'approvals/autonomy-policy-{sha}.json',
                'sha256': hashlib.sha256(payload).hexdigest()}
            decision_id = 'standing-checkpoints-' + sha[:24]
            record = {'version': '1.0', 'kind': 'standing_checkpoint_stage_reconciliation',
                'original_policy_sha256': old_sha, 'successor_policy_sha256': sha,
                'original_policy': {'path': str(policy_snapshot.relative_to(root)), 'sha256': hashlib.sha256(original_raw).hexdigest()},
                'original_decision_log': {'path': str(log_snapshot.relative_to(root)), 'sha256': hashlib.sha256(original_log_raw).hexdigest()},
                'original_evidence': copy.deepcopy(original['evidence']), 'original_decision': copy.deepcopy(decision),
                'successor_evidence': copy.deepcopy(candidate['evidence']), 'successor_decision_id': decision_id,
                'removed_checkpoint_stages': removed, 'provider_calls': 0, 'reservations': 0}
            record_payload = json.dumps(record, indent=2, allow_nan=False).encode()
            provenance = {'path': str(record_path.relative_to(root)), 'sha256': hashlib.sha256(record_payload).hexdigest()}
            choice = 'auto_continue:' + sha
            successor = {'decision_id': decision_id, 'stage': 'proposal', 'category': ACTIVATION_CATEGORY,
                'subject': ACTIVATION_SUBJECT, 'selected': choice, 'user_approved': True,
                'reason': f'Remove only ungated checkpoint stages from retained standing activation {decision["decision_id"]}; '
                    f'unchanged creator authority, plan and cumulative allowances. Provenance: {provenance["path"]} sha256:{provenance["sha256"]}.',
                'options_considered': [{'option_id': decision['selected'], 'label': 'Original standing activation',
                    'score': 0, 'reason': 'Retained original creator authority.',
                    'rejected_because': 'Checkpoint stages are not actual gated pipeline stages: ' + ', '.join(removed)},
                    {'option_id': choice, 'label': 'Standing activation with actual checkpoint gates', 'score': 1,
                     'reason': 'Only ungated checkpoint stages removed; every other term remains identical.'}]}
            result = {'policy_sha256': sha, 'decision_id': decision_id, 'replayed': replayed,
                'provider_calls': 0, 'reservations': 0, 'reconciliation': provenance}
            successor_log = copy.deepcopy(original_log)
            successor_log['decisions'].append(successor)
            partial = replayed and current == original and log == successor_log
            if replayed:
                if (record_path.read_bytes() != record_payload
                        or log['decisions'][:len(original_log['decisions'])] != original_log['decisions']
                        or current not in (original, candidate)):
                    raise StandingSetupRequired('retained reconciliation successor/provenance differs or is revoked')
                if current == candidate:
                    if activation_decision(root) != successor:
                        raise StandingSetupRequired('reconciled successor is revoked or superseded')
                    for sid in baselines:
                        active, got_sha, got_id, reasons = load_active_policy(root, shot_id=sid)
                        if active is None or (got_sha, got_id) != (sha, decision_id):
                            raise StandingSetupRequired('reconciled successor is inactive: ' + '; '.join(reasons))
                    return result
                if log not in (original_log, successor_log):
                    raise StandingSetupRequired('interrupted reconciliation decision was revoked or superseded')
            allowed_reasons = (['no current user approval (missing, stale or revoked)', CHECKPOINT_STAGES_ERROR]
                               if partial else [CHECKPOINT_STAGES_ERROR])
            _require_unstarted_standing_run(root)
            for sid in [None, *baselines]:
                if load_active_policy(root, shot_id=sid)[3] != allowed_reasons:
                    raise StandingSetupRequired('original activation has another authority or current planning defect')
            _validate_checkpoint_stages(root, candidate['checkpoint_stages'])
            if not partial and any(entry['decision_id'] == decision_id for entry in log['decisions']):
                raise StandingSetupRequired('reconciliation activation already exists or is superseded')
            Draft202012Validator(load_schema('decision_log')).validate(successor_log)
            for snapshot_path, source, expected in ((policy_snapshot, path, original_raw), (log_snapshot, log_path, original_log_raw)):
                if not snapshot_path.exists():
                    os.link(source, snapshot_path)
                    snapshot_path.chmod(0o444)
                if snapshot_path.read_bytes() != expected:
                    raise StandingSetupRequired('frozen reconciliation original bytes differ')
            for target, value, expected in ((root / candidate['evidence']['path'], content, payload),
                                            (record_path, record, record_payload)):
                if not target.exists():
                    execution._write_new(target, value)
                if target.read_bytes() != expected:
                    raise StandingSetupRequired('retained reconciliation evidence differs')
            # Cooperating admission shares the lock. Recheck bytes and authority
            # before publishing either mutable pointer; an interrupted publication
            # is Strict rather than permission to revive a later/revoked activation.
            if path.read_bytes() != raw or log_path.read_bytes() != log_raw:
                raise StandingSetupRequired('current policy/decision changed during reconciliation')
            _require_unstarted_standing_run(root)
            for sid in [None, *baselines]:
                if load_active_policy(root, shot_id=sid)[3] != allowed_reasons:
                    raise StandingSetupRequired('current authority changed during reconciliation')
            temporary_policy = path.with_suffix('.json.checkpoints-tmp')
            temporary_log = log_path.with_suffix('.json.checkpoints-tmp')
            for target, value in ((temporary_policy, candidate), (temporary_log, successor_log)):
                if not target.exists():
                    execution._write_new(target, value)
                if target.read_bytes() != json.dumps(value, indent=2, allow_nan=False).encode():
                    raise StandingSetupRequired('interrupted reconciliation pointer bytes differ')
            try:
                temporary_log.replace(log_path)
                temporary_policy.replace(path)
            finally:
                temporary_policy.unlink(missing_ok=True)
                temporary_log.unlink(missing_ok=True)
            return result
    except StandingSetupRequired:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StandingSetupRequired('checkpoint reconciliation cannot be validated: ' + str(exc)) from exc


def _standing_allowance_changes(previous, candidate, *, source_only=False, shot_ids=(), historical=False):
    """Only authenticated allowance increases; every other term stays exact.

    Named-shot (caps.max_attempts_by_shot) deltas are read only from already
    retained records (historical=True); the current writer refuses them.
    """
    if not isinstance(previous, dict) or not isinstance(candidate, dict):
        raise StandingSetupRequired('source batch amendment requires complete standing setups')
    before = copy.deepcopy(previous); after = copy.deepcopy(candidate)
    before.pop('authorization', None); after.pop('authorization', None)
    changes = []
    old_terms, new_terms = before.get('source_transfer'), after.get('source_transfer')
    if not isinstance(old_terms, list) or not isinstance(new_terms, list) or len(old_terms) != len(new_terms):
        raise StandingSetupRequired('source batch amendment cannot change transport terms')
    for old, new in zip(old_terms, new_terms):
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise StandingSetupRequired('source batch amendment requires exact transport terms')
        old_limit, new_limit = old.get('max_batches'), new.get('max_batches')
        if type(old_limit) is not int or type(new_limit) is not int or new_limit < old_limit or new_limit < 1:
            raise StandingSetupRequired('source batch amendment requires positive nondecreasing max_batches')
        if new_limit != old_limit:
            changes.append({'provider': old['provider'], 'account': old['account'],
                'before': old_limit, 'after': new_limit})
        new['max_batches'] = old_limit
    if not source_only:
        old_caps, new_caps = before.get('caps'), after.get('caps')
        if not isinstance(old_caps, dict) or not isinstance(new_caps, dict):
            raise StandingSetupRequired('standing allowance requires complete retained caps')
        old_limit, new_limit = old_caps.get('max_total_attempts'), new_caps.get('max_total_attempts')
        if type(old_limit) is not int or type(new_limit) is not int or new_limit < old_limit or new_limit < 1:
            raise StandingSetupRequired('standing allowance requires positive nondecreasing max_total_attempts')
        if new_limit != old_limit:
            changes.append({'field': 'caps.max_total_attempts', 'before': old_limit, 'after': new_limit})
        new_caps['max_total_attempts'] = old_limit
        old_shots = old_caps.get('max_attempts_by_shot', {})
        new_shots = new_caps.get('max_attempts_by_shot', {})
        if (not isinstance(old_shots, dict) or not isinstance(new_shots, dict)
                or not set(old_shots).issubset(new_shots)
                or not set(new_shots).issubset(shot_ids)):
            raise StandingSetupRequired('standing allowance requires retained named policy shots')
        for shot_id, new_value in sorted(new_shots.items()):
            old_value = old_shots.get(shot_id, old_caps['max_attempts_per_shot'])
            if (type(old_value) is not int or type(new_value) is not int
                    or new_value < old_value or new_value < 1
                    or new_value > new_limit
                    or shot_id not in old_shots and new_value == old_value):
                raise StandingSetupRequired('standing shot allowance requires positive increasing integer limits within total cap')
            if new_value != old_value:
                changes.append({'field': 'caps.max_attempts_by_shot', 'shot_id': shot_id,
                                'before': old_value, 'after': new_value})
        # Keep absent optional keys absent, preserving legacy source-only deltas.
        if 'max_attempts_by_shot' in old_caps:
            new_caps['max_attempts_by_shot'] = copy.deepcopy(old_shots)
        else:
            new_caps.pop('max_attempts_by_shot', None)
    if before != after or not changes:
        allowed = 'max_batches' if source_only else 'max_batches and max_total_attempts'
        raise StandingSetupRequired('standing allowance changes only ' + allowed + '; no other caps, sources, rights or protections')
    if not historical and any(c.get('field') == 'caps.max_attempts_by_shot' for c in changes):
        raise StandingSetupRequired('named-shot attempt expansion is retired; only retained history is readable')
    return changes


def _source_batch_changes(previous, candidate):
    """Retained source-only records/public diagnostics preserve their exact delta."""
    return _standing_allowance_changes(previous, candidate, source_only=True)


def _standing_setup_state(root, policy):
    """One authenticated chain, including unchanged retained source-only history."""
    root = Path(root)
    basis = policy['activation_basis']
    binding = basis['standing_setup']
    setup = _bound_document(root, binding, 'standing setup')
    path = root / 'production_scopes.json'
    data = _json(path) if path.exists() else {}
    legacy = data.get('source_batch_amendments', [])
    records = data.get('standing_allowance_amendments', [])
    if not isinstance(legacy, list) or not isinstance(records, list):
        raise StandingSetupRequired('invalid standing allowance amendments')
    retained_records = []
    if not legacy and not records:
        return setup, binding, retained_records
    sha = policy_sha256(policy, retained_baselines(root, policy))
    for index, retained in enumerate([*legacy, *records]):
        record = _bound_document(root, retained, 'standing allowance amendment')
        source_only = record.get('kind') == 'standing_source_max_batches_amendment'
        if (set(record) != {'version', 'kind', 'policy_sha256', 'predecessor_setup', 'standing_setup', 'changes'}
                or record['version'] != '1.0'
                or record['kind'] not in {'standing_source_max_batches_amendment', 'standing_allowance_amendment'}
                or index < len(legacy) and not source_only
                or record['policy_sha256'] != sha or record['predecessor_setup'] != binding):
            raise StandingSetupRequired('standing allowance predecessor differs from current activation/setup')
        candidate = _bound_document(root, record['standing_setup'], 'standing allowance successor setup')
        _bound_authorization(root, candidate, basis['identity_id'])
        if _standing_allowance_changes(setup, candidate, source_only=source_only, shot_ids=policy['shots'],
                                       historical=True) != record['changes']:
            raise StandingSetupRequired('standing allowance differs from exact creator setup')
        retained_records.append((retained, record))
        setup, binding = candidate, record['standing_setup']
    return setup, binding, retained_records


def effective_standing_setup(root, policy):
    """Current authenticated allowance; original activation and job bytes stay frozen."""
    return _standing_setup_state(root, policy)[0]


def effective_standing_caps(root, policy):
    """Authenticated successors expand only their exact total or named-shot caps."""
    caps = copy.deepcopy(policy['caps'])
    if policy.get('activation_basis'):
        for _, record in _standing_setup_state(root, policy)[2]:
            for change in record['changes']:
                if change.get('field') == 'caps.max_total_attempts':
                    caps['max_total_attempts'] = change['after']
                elif change.get('field') == 'caps.max_attempts_by_shot':
                    caps.setdefault('max_attempts_by_shot', {})[change['shot_id']] = change['after']
    return caps


def amend_standing_source_max_batches(project_dir, *, expected_policy_sha256,
                                     expected_setup_sha256, standing_setup):
    """Compatibility entry for already retained source-only producer references."""
    return _amend_standing_allowance(project_dir, expected_policy_sha256=expected_policy_sha256,
        expected_setup_sha256=expected_setup_sha256, standing_setup=standing_setup, source_only=True)


def amend_standing_allowance(project_dir, *, expected_policy_sha256,
                             expected_setup_sha256, standing_setup):
    """Append exact creator-approved source batch or total attempt increases.

    Fresh named-shot (caps.max_attempts_by_shot) expansion is retired; already
    retained named-shot records stay readable through _standing_setup_state.
    No other caps, account/route/rights/source/plan/control changes are admitted.
    Original activation, scopes, envelopes, jobs, selections and counts remain
    frozen. Merely preparing approval never activates an allowance. Exact replay
    after progress is read-only; stale or revoked authority fails closed.
    """
    return _amend_standing_allowance(project_dir, expected_policy_sha256=expected_policy_sha256,
        expected_setup_sha256=expected_setup_sha256, standing_setup=standing_setup, source_only=False)


def _amend_standing_allowance(project_dir, *, expected_policy_sha256,
                              expected_setup_sha256, standing_setup, source_only):
    from lib import production_execution as execution, production_images as images
    root = Path(project_dir).resolve()
    with no_status_reuse(), execution._lock(root):
        policy, sha, _ = require_active_policy(root)
        if sha != expected_policy_sha256 or not policy.get('activation_basis'):
            raise StandingSetupRequired('standing allowance requires exact active predecessor policy SHA')
        setup, predecessor, records = _standing_setup_state(root, policy)
        path = root / 'production_scopes.json'
        data = _json(path) if path.exists() else {'version': '1.0', 'scopes': []}
        if data.get('version') != '1.0' or not isinstance(data.get('scopes'), list):
            raise StandingSetupRequired('invalid production scopes record')
        if records:
            latest_binding, latest = records[-1]
            if (latest['standing_setup'] == standing_setup
                    and latest['predecessor_setup']['sha256'] == expected_setup_sha256):
                if source_only and any(c.get('field') in {'caps.max_total_attempts', 'caps.max_attempts_by_shot'}
                                       for c in latest['changes']):
                    raise StandingSetupRequired('source batch amendment changes only max_batches')
                if any(c.get('field') == 'caps.max_attempts_by_shot' for c in latest['changes']):
                    raise StandingSetupRequired('named-shot attempt expansion is retired; retained record is read-only history')
                return {'amendment': copy.deepcopy(latest_binding), 'policy_sha256': sha,
                    'standing_setup': copy.deepcopy(standing_setup), 'changes': latest['changes'],
                    'replayed': True, 'provider_calls': 0}
        if predecessor['sha256'] != expected_setup_sha256:
            raise StandingSetupRequired('standing allowance predecessor setup SHA is stale')
        candidate = _bound_document(root, standing_setup, 'standing allowance successor setup')
        _bound_authorization(root, candidate, policy['activation_basis']['identity_id'])
        changes = _standing_allowance_changes(setup, candidate, source_only=source_only, shot_ids=policy['shots'])
        record = {'version': '1.0', 'kind': ('standing_source_max_batches_amendment' if source_only else 'standing_allowance_amendment'),
            'policy_sha256': sha, 'predecessor_setup': copy.deepcopy(predecessor),
            'standing_setup': copy.deepcopy(standing_setup), 'changes': changes}
        prefix = 'source-max-batches-' if source_only else 'standing-allowance-'
        record_path = root / ('approvals/' + prefix + digest(record) + '.json')
        payload = json.dumps(record, indent=2, allow_nan=False).encode()
        if record_path.exists():
            if record_path.read_bytes() != payload:
                raise StandingSetupRequired('retained standing allowance changed')
        else:
            execution._write_new(record_path, record)
        binding = {'path': str(record_path.relative_to(root)), 'sha256': hashlib.sha256(payload).hexdigest()}
        key = ('source_batch_amendments' if source_only and not data.get('standing_allowance_amendments')
               else 'standing_allowance_amendments')
        data.setdefault(key, []).append(binding)
        images._write_image_scopes(root, data)
        return {'amendment': binding, 'policy_sha256': sha, 'standing_setup': copy.deepcopy(standing_setup),
            'changes': changes, 'replayed': False, 'provider_calls': 0}


def _validate_standing_transfer(root, policy, inputs, provider, *, exclude_scope_id=None):
    """Actual source bytes fit retained per-transport/account file and batch limits."""
    if not policy.get('activation_basis'):
        return
    from lib import production_execution as execution
    from lib.shot_contract import required_asset_ids
    setup = effective_standing_setup(root, policy)
    account = setup['provider_accounts'][provider]
    term = next(t for t in setup['source_transfer'] if t['provider'] == provider and t['account'] == account)
    contract = execution.load_shot_contract(root)
    shot = next(s for s in contract['shots'] if s['id'] == inputs['governance']['shot_id'])
    needed = required_asset_ids(contract, shot)
    assets = [a for a in contract['assets'] if a['id'] in needed]
    actual = []
    def bind(role, path):
        matches = [a for a in assets if a.get('path') and _inside(root, a['path']) == path
                   and a.get('sha256') == hashlib.sha256(path.read_bytes()).hexdigest()]
        if not matches or not any(a['role'] in term['roles'] for a in matches):
            raise StandingSetupRequired('actual transport source lies outside reviewed consumer roles')
        size = path.stat().st_size
        if size > term['max_file_bytes']:
            raise StandingSetupRequired('actual source exceeds retained file limit')
        actual.append((path, size))
        return str(path)
    execution._paths(execution._clean(inputs), Path(root).resolve(), bind)
    if len(actual) > term['max_files'] or sum(size for _, size in actual) > term['max_batch_bytes']:
        raise StandingSetupRequired('actual source batch exceeds retained transfer limit')
    # Grok transfers local bytes inline with each original motion call. OpenArt
    # reuses separately uploaded remote sources; its existing upload authorities
    # and owners count those batches, not every generation using their URLs.
    prior = []
    if provider == 'grok_cli':
        for attempt in execution._attempts(Path(root)):
            if (attempt.get('scope', attempt.get('scope_snapshot', {})).get('provider') != provider
                    or exclude_scope_id is not None and attempt.get('scope_id') == exclude_scope_id):
                continue
            bound = []
            execution._paths(execution._clean(attempt.get('submitted_inputs', {})), Path(root).resolve(),
                lambda role, path: bound.append(path) or str(path))
            if bound:
                prior.append(attempt)
    if actual and len(prior) >= term['max_batches']:
        raise StandingSetupRequired('retained source transfer batch allowance exhausted')


def _policy_fallback_sources(root, policy, call_id, files, *, shot_id):
    """Exact denied board-call references under the standing OpenArt MCP transfer term."""
    from lib import production_images as images, production_boards as boards
    standing = standing_image_policy_fallback(root)
    if standing is None:
        raise StandingSetupRequired('this run command grants no OpenArt image policy fallback')
    if 'image2image' not in standing[0]['forms']:
        raise StandingSetupRequired('reference transfer needs the run image2image fallback term')
    call = images._call(images._load(root), call_id)
    pack = boards.load_board_pack(root)
    if pack['prompt_pack'].get(call['slot_id'], {}).get('shot_id') != shot_id:
        raise StandingSetupRequired('denied board slot differs from the upload consumer shot')
    images._validate_packet(root, call['packet'])
    scope = images._scope(root, call['scope_id'])
    if (digest(scope) != call['scope_sha256'] or not scope.get('derived_from_image_envelope')
            or scope.get('requests', {}).get(call['slot_id']) != call['request_sha256']
            or images.image_request_digest(call['packet'], call['slot_id'], call['host_arguments'],
                output_path=call['output_path']) != call['request_sha256']):
        raise StandingSetupRequired('denied native request differs from its exact approved source scope')
    images._validate_derived_image_scope(root, scope, call['packet'], call['slot_id'],
        call['host_arguments'], call['output_path'])
    submitted = copy.deepcopy(call['host_arguments'])
    if submitted.get('referenced_image_paths'):
        submitted['referenced_image_paths'] = []
        for ref in call['packet']['references']:
            snapshot = Path(root) / 'production_images' / call_id / 'inputs' / (ref['sha256'] + Path(ref['path']).suffix)
            images._bound_file(root, {'path': str(snapshot), 'sha256': ref['sha256']}, 'native source snapshot')
            submitted['referenced_image_paths'].append(str(snapshot))
    if submitted != call.get('submitted_arguments') or call['tool_name'] != images.NATIVE_TOOL:
        raise StandingSetupRequired('denied native invocation differs from its retained source snapshots')
    receipt_sha = images._policy_denial(root, call)
    if receipt_sha is None:
        raise StandingSetupRequired('source transfer needs an explicit native content-policy denial')
    evidence = call.get('host_receipt') or call['host_exception']
    observation = images._read(images._bound_file(root, evidence, 'native policy denial'))
    if (observation.get('tool_name') != images.NATIVE_TOOL or observation.get('arguments') != submitted
            or call.get('host_exception') and observation.get('call_id') != call_id):
        raise StandingSetupRequired('native policy denial differs from the original invocation')
    setup = effective_standing_setup(root, policy)
    account = setup['provider_accounts'].get('openart_mcp')
    term = next((t for t in setup['source_transfer'] if t['provider'] == 'openart_mcp' and t['account'] == account), None)
    if term is None:
        raise StandingSetupRequired('standing OpenArt MCP source transfer term required')
    supplied = [str(_inside(root, f)) for f in files]
    references = [r for r in call['packet']['references'] if str(_inside(root, r['path'])) in supplied]
    if (len(set(supplied)) != len(supplied)
            or [str(_inside(root, r['path'])) for r in references] != supplied):
        raise StandingSetupRequired('upload sources must be an exact unique ordered subset of denied-call references')
    total = 0
    retained = images._image_envelope(root, images._read(Path(root) / 'production_scopes.json'))
    inventory = {ref['id']: ref for ref in retained['definition']['source_inventory']}
    for ref in references:
        path = _inside(root, ref['path'])
        size = path.stat().st_size
        if (ref['role'] not in term['roles'] or hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']
                or size > term['max_file_bytes']):
            raise StandingSetupRequired('denied-call reference lies outside the retained transfer term')
        if ref['sha256'] not in setup['approved_source_sha256']:
            # The derived-scope validator above proves the exact same-envelope
            # imported producer bytes for dynamic sources; static inventory is
            # never admitted merely because it has a transferable role.
            if not term['generated_board_sources'] or not inventory[ref['id']].get('source_slot_id'):
                raise StandingSetupRequired('denied-call source is outside approved original/generated board permission')
        total += size
    if len(references) > term['max_files'] or total > term['max_batch_bytes']:
        raise StandingSetupRequired('denied-call references exceed retained transfer limits')
    return references, {'call_id': call_id, 'receipt_sha256': receipt_sha}


def _standing_upload_material(root, *, shot_id, provider, files, account, openart_project_id=None,
                              policy_fallback_call_id=None):
    """Pure existing-format source-transfer authority from the real standing run."""
    import mimetypes
    from lib import production_execution as execution, production_request as preparation
    policy, sha, decision_id = require_active_policy(root, shot_id=shot_id)
    if not policy.get('activation_basis') or provider not in {'openart_cli', 'openart_mcp'}:
        raise StandingSetupRequired('standing OpenArt source transfer authority required')
    spec = _provider_spec(policy, provider)
    if spec is None:
        raise StandingSetupRequired('upload provider has no standing account authority')
    if provider == 'openart_cli':
        expected_account = {'account_id_sha256': spec['account_id_sha256'], 'workspace': spec['workspace']}
        if account != expected_account or openart_project_id is not None:
            raise StandingSetupRequired('CLI upload account/workspace differs from standing setup')
    elif (not isinstance(account, dict) or account.get('uid_sha256') != spec['uid_sha256']
            or openart_project_id != spec['project_id']):
        raise StandingSetupRequired('MCP upload account/project differs from standing setup')
    if (not isinstance(files, list) or not files or any(not isinstance(p, str) for p in files)
            or provider == 'openart_cli' and len(files) != 1):
        raise StandingSetupRequired('exact supported ordered source files required')
    fallback = None
    if policy_fallback_call_id is not None:
        if provider != 'openart_mcp':
            raise StandingSetupRequired('image policy fallback transfers only through OpenArt MCP')
        references, fallback = _policy_fallback_sources(root, policy, policy_fallback_call_id, files, shot_id=shot_id)
        packet = {'binding': {'references': copy.deepcopy(references)}}
    else:
        packet = preparation.source_packet(root, shot_id, provider=provider, check_native_controls=False)
        consumer_assets = {a['id']: a for a in execution.load_shot_contract(root)['assets']}
    manifest, assets = [], []
    for supplied in files:
        path = _inside(root, supplied)
        if Path(supplied).is_symlink() or not path.is_file():
            raise StandingSetupRequired('upload requires regular current consumer source bytes')
        source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if fallback:
            matches = [a for a in packet['binding']['references']
                       if _inside(root, a['path']) == path and a['sha256'] == source_sha]
        else:
            matches = [a for a in packet['binding']['references'] if consumer_assets[a['id']].get('path')
                and _inside(root, consumer_assets[a['id']]['path']) == path and a['sha256'] == source_sha]
        if not matches:
            raise StandingSetupRequired('upload source is outside exact reviewed consumer binding')
        media_type = (mimetypes.guess_type(str(path))[0] or '').split('/')[0]
        if media_type not in {'image', 'video', 'audio'}:
            raise StandingSetupRequired('upload source media type is unsupported')
        manifest.append({'path': str(path), 'sha256': source_sha, 'type': media_type}); assets.append(matches[0])
    if len({r['path'] for r in manifest}) != len(manifest):
        raise StandingSetupRequired('upload source paths must be unique')
    if not fallback:
        _validate_standing_transfer(root, policy, {'governance': {'shot_id': shot_id},
            'reference_image_paths': [r['path'] for r in manifest]}, provider)
    binding = {'policy_sha256': sha, 'decision_id': decision_id, 'shot_id': shot_id,
        'source_binding': packet['binding'], 'account': account, 'files': manifest,
        'openart_project_id': openart_project_id}
    if fallback:
        binding['policy_fallback'] = fallback
    upload_id = 'standing-' + digest(binding)[:32]
    approved_by = 'standing_media_authority:' + decision_id
    if provider == 'openart_cli':
        record = {'version': '1.0', 'upload_id': upload_id, 'asset_id': assets[0]['id'],
            'shot_id': shot_id, 'source_sha256': manifest[0]['sha256'], 'source_binding': packet['binding'],
            'approved_by': approved_by, 'evidence_path': policy['evidence']['path'], 'evidence_sha256': policy['evidence']['sha256']}
        name = 'upload_approval-' + upload_id + '.json'
    else:
        record = {'provider': provider, 'status': 'approved', 'purpose': 'source_transfer_only',
            'project_id': policy['project_id'], 'story_revision': policy['story_revision'],
            'account_uid_sha256': spec['uid_sha256'], 'openart_project_id': openart_project_id,
            'files': manifest, 'no_enforceable_credit_ceiling': True, 'delayed_charges_unknown': True,
            'max_upload_batches': 1, 'approved_by': approved_by, 'evidence': copy.deepcopy(policy['evidence']),
            'derived_from_standing_run': binding}
        name = 'openart_mcp_upload_authorization-' + upload_id + '.json'
    return policy, record, name


def _standing_upload_batch_count(root, provider):
    prefix = 'upload_approval-' if provider == 'openart_cli' else 'openart_mcp_upload_authorization-'
    count = len(list((Path(root) / 'artifacts').glob(prefix + '*.json')))
    if provider == 'openart_mcp':
        from lib import openart_mcp_jobs as jobs
        for path in (Path(root) / 'openart_mcp/uploads').glob('*.json'):
            batch = _json(path)
            if 'transport_retry' in batch:
                jobs._validate_upload_retry(Path(root), batch)
                count += 1
        for path in (jobs._state_root(create=False) / 'mcp-uploads').glob('*.json'):
            receipt = _json(path)
            if receipt.get('project_root') == str(Path(root).resolve()) and 'source_transfer_invocation' in receipt:
                if receipt.get('receipt_sha256') != jobs.mcp.digest({k:v for k,v in receipt.items() if k!='receipt_sha256'}):
                    raise StandingSetupRequired('source retry receipt digest changed')
                batch = _json(Path(root) / 'openart_mcp/uploads' / (jobs._id(receipt['upload_batch_id']) + '.json'))
                jobs._validate_upload_retry(Path(root), batch, receipt)
    return count


def derive_standing_upload_authority(project_dir, *, shot_id, provider, files, account, openart_project_id=None,
                                     policy_fallback_call_id=None):
    """Retain exact legacy-format upload authority; no upload/account discovery call."""
    from lib import production_execution as execution
    root = Path(project_dir).resolve()
    with execution._lock(root):
        policy, record, name = _standing_upload_material(root, shot_id=shot_id, provider=provider,
            files=files, account=account, openart_project_id=openart_project_id,
            policy_fallback_call_id=policy_fallback_call_id)
        path = root / 'artifacts' / name
        if path.exists():
            if _json(path) != record:
                raise StandingSetupRequired('retained upload authority differs from exact current source/account')
            return {'upload_id': name.removeprefix('upload_approval-').removeprefix('openart_mcp_upload_authorization-').removesuffix('.json'),
                'authority': record, 'replayed': True, 'provider_calls': 0}
        setup = effective_standing_setup(root, policy)
        term = next(t for t in setup['source_transfer'] if t['provider'] == provider
            and t['account'] == setup['provider_accounts'][provider])
        prefix = 'upload_approval-' if provider == 'openart_cli' else 'openart_mcp_upload_authorization-'
        if _standing_upload_batch_count(root, provider) >= term['max_batches']:
            raise StandingSetupRequired('retained upload batch allowance exhausted')
        execution._write_new(path, record)
        return {'upload_id': name.removeprefix(prefix).removesuffix('.json'), 'authority': record,
            'replayed': False, 'provider_calls': 0}


def validate_standing_mcp_upload(root, authority, *, files, account, openart_project_id):
    binding = authority.get('derived_from_standing_run', {})
    _, expected, _ = _standing_upload_material(Path(root).resolve(), shot_id=binding['shot_id'], provider='openart_mcp',
        files=[r['path'] for r in files], account=binding['account'], openart_project_id=openart_project_id,
        policy_fallback_call_id=(binding.get('policy_fallback') or {}).get('call_id'))
    if authority != expected or account.get('uid_sha256') != authority['account_uid_sha256'] or files != authority['files']:
        raise StandingSetupRequired('current upload account/source/run authority differs')


# ---------------------------------------------------------------- evidence

def evidence_content(policy, baselines):
    """Exact retained human approval document; never infer baselines from descriptors."""
    return {'kind': EVIDENCE_KIND,
            'policy': {k: copy.deepcopy(v) for k, v in policy.items() if k != 'evidence'},
            'baselines': copy.deepcopy(baselines)}


def policy_sha256(policy, baselines):
    return digest(evidence_content(policy, baselines))


def retained_baselines(root, policy):
    """Pure retained-evidence baselines for the exact supplied ``policy``.

    Inside one read-only :func:`status_reuse` operation a repeat call with the
    identical full policy value (digest) and an unchanged project stamp returns
    a deep copy; ``no_status_reuse``/writers and outside a scope always recompute.
    The body reads only in-project evidence bytes and pure descriptor helpers."""
    try:
        policy_digest = digest(policy)
    except (TypeError, ValueError):
        return _retained_baselines(root, policy)
    resolved = Path(root).resolve()
    return status_reused(('retained_baselines', str(resolved), policy_digest), (resolved,),
                         lambda: _retained_baselines(root, policy))


def _retained_baselines(root, policy):
    raw = _inside(root, policy['evidence']['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != policy['evidence']['sha256']:
        raise AutonomyError('evidence bytes changed')
    content = json.loads(raw)
    if set(content) != {'kind', 'policy', 'baselines'} or content['kind'] != EVIDENCE_KIND:
        raise AutonomyError('invalid retained evidence envelope')
    if content['policy'] != {k: v for k, v in policy.items() if k != 'evidence'}:
        raise AutonomyError('evidence policy differs')
    if policy['evidence']['path'] != f'approvals/autonomy-policy-{digest(content)}.json':
        raise AutonomyError('evidence path does not name canonical policy hash')
    baselines = content['baselines']
    if set(baselines) != set(policy['shots']):
        raise AutonomyError('retained shot set differs')
    for shot_id, retained in baselines.items():
        if baseline_descriptor(retained, root) != policy['shots'][shot_id]:
            raise AutonomyError(f'retained baseline descriptor differs: {shot_id}')
    return baselines


def schema_errors(policy):
    validator = Draft202012Validator(load_schema('autonomy_policy'))
    return sorted('/'.join(map(str, e.absolute_path)) or '$' for e in validator.iter_errors(policy))


def _inside(root, rel):
    base = Path(root).resolve()
    path = (base / rel).resolve()
    if path != base and base not in path.parents:
        raise AutonomyError(f'path escapes project root: {rel}')
    return path


def _json(path):
    return json.loads(Path(path).read_text())


def _decision_entries(root):
    from lib.production_execution import _artifact_path
    root = Path(root)
    path = _artifact_path(root, 'decision_log.json')
    data = _json(path)
    Draft202012Validator(load_schema('decision_log')).validate(data)
    if data['project_id'] != _json(root / 'project.json')['project_id']:
        raise AutonomyError('decision log project mismatch')
    ids = [d['decision_id'] for d in data['decisions']]
    if len(ids) != len(set(ids)):
        raise AutonomyError('duplicate decision IDs')
    return data['decisions']


def activation_decision(root):
    """Latest activation-subject approval_policy decision, or ``None``."""
    latest = None
    for entry in _decision_entries(root):
        if isinstance(entry, dict) and entry.get('category') == ACTIVATION_CATEGORY \
                and entry.get('subject') == ACTIVATION_SUBJECT:
            latest = entry
    return latest


def is_benchmark(root, marker):
    # TODO(phase2): bind to the actual U6 benchmark marker; fail closed meanwhile.
    root = Path(root)
    return bool(marker.get('benchmark')) or (root / 'provider_benchmark').exists() \
        or (root / 'artifacts' / 'provider_benchmark.json').exists()


_STATUS_REUSE = contextvars.ContextVar('production_status_reuse', default=None)
# Monotonic writer/lock/dispatch opt-out depth; no nested status scope re-enables reuse.
_STATUS_REUSE_DISABLED = contextvars.ContextVar('production_status_reuse_disabled', default=0)
_SCHEMAS = Path(__file__).resolve().parents[1] / 'schemas'
# Reuse needs POSIX inode-change ctime semantics (Windows st_ctime is creation
# time); elsewhere every call recomputes.
_REUSE_PLATFORM = os.name == 'posix'


def _reuse_owner():
    """(thread, asyncio task) owning a scope; copied contexts never inherit it."""
    try:
        import asyncio
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@contextlib.contextmanager
def status_reuse():
    """Bound one read-only status operation; nested same-owner scopes join it.

    The memo lives only for this ``with`` block, is never shared with another
    thread or asyncio task (even through a copied context) and is discarded on
    exit or exception. Every hit re-proves an unchanged stamp of its inputs."""
    if _STATUS_REUSE_DISABLED.get() > 0:
        yield
        return
    active = _STATUS_REUSE.get()
    owner = _reuse_owner()
    if active is not None and active['owner'][0] == owner[0] and active['owner'][1] is owner[1]:
        yield
        return
    token = _STATUS_REUSE.set({'owner': owner, 'entries': {}})
    try:
        yield
    finally:
        _STATUS_REUSE.reset(token)


@contextlib.contextmanager
def no_status_reuse():
    """Writers, locks and dispatch: always recompute, including any status scope
    opened (or joined) anywhere beneath this block."""
    token = _STATUS_REUSE_DISABLED.set(_STATUS_REUSE_DISABLED.get() + 1)
    try:
        yield
    finally:
        _STATUS_REUSE_DISABLED.reset(token)


def _active_reuse():
    if _STATUS_REUSE_DISABLED.get() > 0:
        return None
    active = _STATUS_REUSE.get()
    if active is None or not _REUSE_PLATFORM:
        return None
    owner = _reuse_owner()
    if active['owner'][0] != owner[0] or active['owner'][1] is not owner[1]:
        return None
    return active['entries']


def _reuse_stamp(*bases):
    """Every entry's device/inode, size, mode, mtime and ctime (ns), or ``None``.

    ``None`` (never reusable) on any unreadable entry or when any entry has only
    whole-second mtime and ctime (a coarse filesystem could hide a same-second
    rewrite of that entry; one fine filesystem never vouches for another)."""
    rows = []
    def coarse(s):
        return not (s.st_mtime_ns % 1_000_000_000 or s.st_ctime_ns % 1_000_000_000)
    try:
        for base in bases:
            top = os.stat(base)
            if coarse(top):
                return None
            rows.append((base, top.st_dev, top.st_ino, top.st_size, top.st_mode, top.st_mtime_ns, top.st_ctime_ns))
            stack = [base] if os.path.isdir(base) else []
            while stack:
                directory = stack.pop()
                with os.scandir(directory) as found:
                    entries = sorted(found, key=lambda e: e.name)
                for entry in entries:
                    s = entry.stat(follow_symlinks=False)
                    row = (entry.path, s.st_dev, s.st_ino, s.st_size, s.st_mode, s.st_mtime_ns, s.st_ctime_ns)
                    if coarse(s):
                        return None
                    if entry.is_symlink():
                        t = os.stat(entry.path)
                        if coarse(t):
                            return None
                        row += (t.st_dev, t.st_ino, t.st_size, t.st_mode, t.st_mtime_ns, t.st_ctime_ns)
                    rows.append(row)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
    except OSError:
        return None
    return tuple(rows)


def status_reused(key, bases, compute):
    """Inside the owning :func:`status_reuse` scope, return a deep copy of a prior
    successful ``compute()`` for the exact ``key`` only while every file under
    ``bases`` keeps the identical stamp. Outside a scope, under
    :func:`no_status_reuse`, on another thread/task or an unsupported platform or
    filesystem this always recomputes. Exceptions are never retained."""
    memo = _active_reuse()
    if memo is None:
        return compute()
    bases = tuple(str(Path(b).expanduser().resolve()) for b in bases)
    key = (key, bases)
    stamp = _reuse_stamp(*bases)
    hit = memo.pop(key, None)
    if stamp is not None and hit is not None and hit[0] == stamp:
        memo[key] = hit
        return copy.deepcopy(hit[1])
    result = compute()
    # Retain only a result computed entirely against one unchanged stamp.
    if stamp is not None and _reuse_stamp(*bases) == stamp:
        memo[key] = (stamp, copy.deepcopy(result))
    return result


def load_active_policy(root, *, shot_id=None):
    """Return ``(policy, sha, decision_id, reasons)``; policy ``None`` means Strict. Zero writes."""
    # Always fresh, global and per shot: both can reach upstream board provenance
    # (``_validate_delta``/``_validate_standing_basis_impl`` ->
    # ``_validate_generated_board_source``) and thereby MCP discovery, account
    # and environment state outside any project stamp. Only the pure
    # ``retained_baselines`` step beneath it is reused per operation.
    return _load_active_policy(root, shot_id=shot_id)


def _load_active_policy(root, *, shot_id=None):
    """Return ``(policy, sha, decision_id, reasons)``; policy ``None`` means Strict. Zero writes."""
    root = Path(root)
    reasons = []
    try:
        path = root / POLICY_ARTIFACT
        if not path.is_file():
            return None, None, None, ['no policy artifact']
        policy = _json(path)
        errors = schema_errors(policy)
        if errors:
            return None, None, None, [f'schema: {e}' for e in errors]
        baselines = retained_baselines(root, policy)
        sha = digest(evidence_content(policy, baselines))
        local = policy.get('planning_mode') == 'shot_local_v1'
        if local:
            if shot_id is not None and shot_id not in baselines:
                raise AutonomyError('shot outside retained policy')
            _validate_local_protected_planning(root, policy, baselines)
        if policy.get('activation_basis'):
            setup, _ = _validate_standing_basis(root, policy, baselines, check_first_pass_capacity=False)
            effective_standing_setup(root, policy)
        # Retained original sources remain immutable authority even if a legal
        # request drops a reference or chooses separate approved substitute bytes.
        source_baselines = ([baselines[shot_id]] if shot_id is not None else []) if local else baselines.values()
        for retained in source_baselines:
            for asset in retained['projection']['assets']:
                if not asset.get('upstream_source') and not asset.get('preparation_pending') and hashlib.sha256(_inside(root, asset['path']).read_bytes()).hexdigest() != asset['sha256']:
                    raise AutonomyError('retained original static source bytes changed')
        decision = activation_decision(root)
        if policy.get('activation_basis'):
            # This loader already proved the exact setup above. Intervening
            # reads do not mutate it; every independent load proves it afresh.
            _validate_standing_controls_activation(root, policy, sha, decision, setup=setup)
        if not decision or decision.get('selected') != f'auto_continue:{sha}' \
                or decision.get('user_approved') is not True:
            reasons.append('no current user approval (missing, stale or revoked)')
        marker_path = root / 'project.json'
        marker = _json(marker_path) if marker_path.is_file() else {}
        if not isinstance(marker, dict):
            marker = {}
        gov = marker.get('governance') or {}
        if gov.get('mode') != 'strict' or gov.get('version') != '1.0':
            reasons.append('strict governance marker missing')
        if marker.get('project_id') != policy['project_id'] or marker.get('story_revision') != policy['story_revision']:
            reasons.append('policy project/story revision is stale')
        if is_benchmark(root, marker):
            reasons.append('benchmark projects are always Strict')
        reasons.extend(policy_conflicts(policy, baselines))
        provider_ids = [p['id'] for p in policy['providers']]
        if len(provider_ids) != len(set(provider_ids)):
            reasons.append('duplicate provider IDs')
        if policy['checkpoint_stages']:
            try:
                _validate_checkpoint_stages(root, policy['checkpoint_stages'])
            except StandingSetupRequired as exc:
                reasons.append(str(exc))
        if not local:
            _validate_global_planning(root, policy, baselines)
        subjects = ([shot_id] if shot_id is not None else []) if local else baselines
        for subject in subjects:
            current = current_projection(root, subject)
            _validate_delta(policy, subject, current, baselines[subject]['projection'], allow_global_shift=True, root=root)
    except Exception as exc:
        return None, None, None, [f'unreadable policy: {exc}']
    if reasons:
        return None, None, None, reasons
    return policy, sha, decision.get('decision_id'), []


def require_active_policy(root, *, shot_id=None):
    policy, sha, decision_id, reasons = load_active_policy(root, shot_id=shot_id)
    if policy is None:
        raise AutonomyError('Strict: ' + '; '.join(reasons))
    return policy, sha, decision_id


def load_historical_policy(root, scope):
    """``(policy, sha, decision_id)`` that approved an already-derived scope.

    Historical validation needs the retained policy bytes and the user approval
    decision that existed when the scope was derived; it does not require that
    policy to still be current (a later revocation stops new admission only).
    Prospective derivation keeps using :func:`require_active_policy`."""
    return _load_historical_policy(root, scope)


def _load_historical_policy(root, scope, *, _source_shot_id=None):
    """Private replay after provenance proves current source-shot applicability.

    All immutable policy/baseline/approval bytes still bind; source-only footage
    does not depend on the current board bytes of unrelated retained shots.
    """
    root = Path(root)
    derived = scope.get('derived_from_policy') if isinstance(scope, dict) else None
    if not isinstance(derived, dict):
        raise AutonomyError('malformed historical derived policy scope')
    path = root / POLICY_ARTIFACT
    if not path.is_file():
        raise AutonomyError('historical policy artifact missing')
    policy = _json(path)
    errors = schema_errors(policy)
    if errors:
        raise AutonomyError('historical policy schema: ' + '; '.join(errors))
    baselines = retained_baselines(root, policy)
    sha = digest(evidence_content(policy, baselines))
    if policy.get('planning_mode') == 'shot_local_v1':
        subject = _source_shot_id or next(iter(scope.get('requests', {})), None)
        source_baselines = [baselines[subject]] if subject in baselines else []
    else:
        source_baselines = baselines.values() if _source_shot_id is None else [baselines[_source_shot_id]]
    for retained in source_baselines:
        for asset in retained['projection']['assets']:
            if not asset.get('upstream_source') and not asset.get('preparation_pending') and hashlib.sha256(_inside(root, asset['path']).read_bytes()).hexdigest() != asset['sha256']:
                raise AutonomyError('retained original static source bytes changed')
    if derived.get('policy_sha256') != sha:
        raise AutonomyError('historical attempt policy bytes differ')
    decision_id = derived.get('decision_id')
    approvals = [d for d in _decision_entries(root) if isinstance(d, dict) and d.get('decision_id') == decision_id
                 and d.get('category') == ACTIVATION_CATEGORY and d.get('subject') == ACTIVATION_SUBJECT
                 and d.get('selected') == f'auto_continue:{sha}' and d.get('user_approved') is True]
    if len(approvals) != 1:
        raise AutonomyError('historical policy approval decision missing')
    marker = _json(root / 'project.json')
    if not isinstance(marker, dict) or marker.get('project_id') != policy['project_id'] \
            or scope.get('story_revision', policy['story_revision']) != policy['story_revision']:
        raise AutonomyError('historical policy project/story differs')
    return policy, sha, decision_id


# ---------------------------------------------------------------- conflicts

UNKNOWN_BILLING = 'unknown_cost_no_ceiling'
UNKNOWN_ACKNOWLEDGEMENT = 'no_enforceable_credit_ceiling'
ROUTE_TOOLS = {'openart_cli': 'openart_cli_video', 'grok_cli': 'grok_cli_video', 'openart_mcp': 'openart_mcp_video'}


def _provider_spec(policy, provider):
    return next((p for p in policy['providers'] if p['id'] == provider), None)


def _openart_spec(policy):
    return next((p for p in policy['providers'] if p['id'] == 'openart_cli'), None)


def _is_unknown(spec):
    """Only the separately approved schema variant; generic flags never qualify."""
    return isinstance(spec, dict) and spec.get('billing') == UNKNOWN_BILLING \
        and spec.get('exposure_acknowledgement') == UNKNOWN_ACKNOWLEDGEMENT


def _is_mcp_quoted(spec):
    return isinstance(spec, dict) and spec.get("id") == "openart_mcp" and spec.get("billing") == "quoted_credit_budget"


def _mcp_billing_supported(spec):
    return _is_unknown(spec) or _is_mcp_quoted(spec)


def _openart_route_approved(spec, model, mode):
    if _is_unknown(spec) or _is_mcp_quoted(spec):
        return {'model': model, 'mode': mode} in spec['routes']
    return model in spec['models']


def _intent_allows(policy, shot_id, provider, model):
    intent = (policy.get('model_selection_intents') or {}).get(shot_id)
    if intent is None:
        return True
    from lib.video_model_selection import selection_intent_allows_route
    return selection_intent_allows_route(intent, provider, model, ROUTE_TOOLS.get(provider))


def _intent_conflicts(policy):
    intents = policy.get('model_selection_intents')
    if intents is None:
        return []
    from lib.video_model_selection import validate_model_selection_intent
    out = []
    providers = {p['id']: p for p in policy['providers']}
    for shot_id, intent in intents.items():
        if shot_id not in policy['shots']:
            out.append(f'conflict: model selection intent names unapproved shot {shot_id}')
            continue
        try:
            validate_model_selection_intent(intent)
        except (ValueError, TypeError) as exc:
            out.append(f'conflict: invalid model selection intent for {shot_id}: {exc}')
            continue
        for item in intent['approved_pool']:
            provider = item['provider']
            if 'tool' in item and item['tool'] != ROUTE_TOOLS.get(provider):
                out.append(f'conflict: intent pool tool outside policy route for {shot_id}')
            elif provider == 'grok_cli':
                if provider not in providers or 'model' in item:
                    out.append(f'conflict: intent pool Grok route outside policy for {shot_id}')
            elif provider in {'openart_cli', 'openart_mcp'}:
                spec = providers.get(provider)
                names = ({r['model'] for r in spec['routes']} if _is_unknown(spec) or _is_mcp_quoted(spec) else set(spec['models'])) if spec else set()
                if item.get('model') not in names:
                    out.append(f'conflict: intent pool OpenArt model outside policy for {shot_id}')
            else:
                out.append(f'conflict: intent pool provider outside policy for {shot_id}')
    return out


def policy_conflicts(policy, baselines=None):
    """Whole-policy conflicts. A model lock is not a conflict (it only excludes Grok)."""
    out = []
    out.extend(_intent_conflicts(policy))
    ids = [p['id'] for p in policy['providers']]
    if len(ids) != len(set(ids)):
        out.append('conflict: duplicate policy provider')
    mcp_spec = _provider_spec(policy, 'openart_mcp')
    if mcp_spec is not None and not _mcp_billing_supported(mcp_spec):
        out.append('conflict: MCP requires approved unknown-cost or quoted-budget terms')
    if _is_mcp_quoted(mcp_spec) and (policy['caps']['max_total_attempts'] != 1 or len(policy['shots']) != 1):
        out.append('conflict: quoted MCP budget covers exactly one original video')
    if _mcp_billing_supported(mcp_spec) and 'model' in policy['locked']['controls'] and not any(_model_lock_allows(policy['locked']['controls']['model'], route['model']) for route in mcp_spec['routes']):
        out.append('conflict: exact model lock excludes every approved MCP route')
    spec = _openart_spec(policy)
    if spec is not None and spec.get('billing') == UNKNOWN_BILLING and not _is_unknown(spec):
        out.append('conflict: unknown-cost billing lacks no-ceiling acknowledgement')
    if _is_unknown(spec) and 'model' in policy['locked']['controls'] and not any(
            _model_lock_allows(policy['locked']['controls']['model'], r['model']) for r in spec['routes']):
        out.append('conflict: exact model lock excludes every approved unknown-cost route')
    locked, flex = policy['locked'], policy['flex']
    controls = locked['controls']
    if flex['duration_s'] and 'duration' in controls:
        out.append('conflict: duration is both flex and locked')
    if flex['resolution'] and 'resolution' in controls:
        out.append('conflict: resolution is both flex and locked')
    locked_cast = {(m['sha256'], m['role']) for members in locked['cast'].values() for m in members}
    implicit = {(a['sha256'], a['role']) for b in policy['shots'].values()
                for a in b['static_input_assets'] if a['role'] in CAST_ROLES}
    if baselines is not None:
        implicit.update((a['sha256'], a['role']) for retained in baselines.values()
                        for a in retained['projection']['assets']
                        if not a.get('upstream_source') and not a.get('preparation_pending') and
                        set(a.get('cast_ids', [])) & set(retained['projection']['implicit_cast_ids']))
    cast_roles = set(CAST_ROLES) | {r for _, r in locked_cast | implicit}
    source_roles = {a['role'] for b in policy['shots'].values() for a in b['static_input_assets']
                    if a.get('sha256') in locked['sources']}
    for sub in flex['references']['substitutes']:
        if (sub['sha256'], sub['role']) in locked_cast | implicit or sub['role'] in cast_roles | source_roles or sub['sha256'] in locked['sources']:
            out.append(f'conflict: substitute {sub["role"]} touches locked/implicit cast')
    for role in flex['references']['droppable_roles']:
        if role in cast_roles | source_roles:
            out.append(f'conflict: droppable role {role} carries locked/implicit cast')
    return out


# ---------------------------------------------------------------- static baseline / retiming

def _validate_standing_route_treatments(policy, baselines):
    """Prospective guided feasibility only; retained activations keep replay."""
    from lib import production_request as preparation
    profiles, errors = {}, []
    for sid, baseline in baselines.items():
        projection = baseline['projection']
        treatment = projection['shot'].get('reference_mode', projection['contract_snapshot'].get('reference_mode'))
        if treatment not in {'reference_guided', 'frame_conditioned'}:
            continue
        rejected, compatible = [], False
        for spec in policy['providers']:
            provider = spec['id']
            routes = spec.get('routes') or [{'model': model, 'mode': None} for model in spec.get('models', [])] or [{'model': None, 'mode': None}]
            for route in routes:
                model, mode = route['model'], route['mode']
                prefix = f"shot {sid}, provider {provider}, model {model or 'unreported'}, mode {mode or 'unreported'}: "
                lock = policy['locked']['controls'].get('model')
                if lock is not None and not _model_lock_allows(lock, model):
                    rejected.append(prefix + 'exact model lock excludes route')
                    continue
                if not _intent_allows(policy, sid, provider, model):
                    rejected.append(prefix + 'per-shot model selection intent excludes route')
                    continue
                capabilities = None
                if provider == 'openart_mcp' and (mode in preparation.REFERENCE_GUIDED_MODES or treatment == 'frame_conditioned' and mode == 'image2video'):
                    from lib import openart_mcp as mcp
                    key = (model, mode)
                    if key not in profiles:
                        try:
                            if not mcp._in_current_catalog(model, mode):
                                raise ValueError('exact guided model/mode is absent from current native catalog')
                            profiles[key] = mcp.load_profile(model, mode, require='candidate')['native_capabilities']
                        except (ValueError, KeyError, OSError) as exc:
                            profiles[key] = str(exc)
                    capabilities = profiles[key]
                    if isinstance(capabilities, str):
                        rejected.append(prefix + 'native capabilities unavailable: ' + capabilities)
                        continue
                reason = preparation.route_treatment_error(projection, provider=provider, model=model,
                    mode=mode, native_capabilities=capabilities)
                if reason is None:
                    compatible = True
                    break
                rejected.append(reason)
            if compatible:
                break
        if not compatible:
            errors.extend(rejected)
    if errors:
        raise StandingSetupRequired('standing route/treatment incompatibility; no approved compatible guided route: ' + '; '.join(errors))


def static_planning_projection(contract, scene_plan, script, shot_id, manifest=None):
    """Static authority including implicit cast/payoff and ordered source declarations."""
    from lib.production_request import _manifest_requirement_binding
    matches = [s for s in contract['shots'] if s['id'] == shot_id]
    if len(matches) != 1:
        raise AutonomyError('shot not exactly once')
    shot = copy.deepcopy(matches[0])
    shot.pop('review', None)
    shot['upstream'] = [{k: copy.deepcopy(v) for k, v in u.items()
                         if k not in {'attempt_id', 'output_sha256', 'outgoing_frame_sha256', 'review_sha256'}}
                        for u in shot.get('upstream', [])]
    # An explicitly reference-free shot inside a boarded episode carries no implicit
    # cast/payoff bytes; the global contract snapshot below still freezes them.
    shot_reference_free = shot.get('reference_mode') == 'reference_free'
    if shot_reference_free and (shot['cast_ids'] or shot.get('dialogue') or shot['asset_ids'] or shot.get('upstream')):
        raise AutonomyError('reference-free shot carries cast, dialogue, assets or upstream')
    if shot_reference_free:
        cast, needed = set(), set()
    else:
        from lib.shot_contract import required_asset_ids, shot_local
        cast = set(shot['cast_ids'])
        if not shot_local(contract):
            cast |= set(contract['late_cast_ids']) | set(contract['payoff_speaker_ids'])
        needed = required_asset_ids(contract, shot)
    assets = []
    for source in contract['assets']:
        if source['id'] not in needed:
            continue
        asset = copy.deepcopy(source)
        asset.pop('review', None)
        if asset.get('upstream_source'):
            asset.pop('path', None)
            asset.pop('sha256', None)
        assets.append(asset)
    if needed != {a['id'] for a in assets}:
        raise AutonomyError('required asset missing')
    scenes = [s for s in scene_plan['scenes'] if s['id'] == shot_id]
    if len(scenes) != 1:
        raise AutonomyError('scene not exactly once')
    sections = [s for s in script['sections'] if s['id'] == scenes[0]['script_section_id']]
    if len(sections) != 1:
        raise AutonomyError('script section not exactly once')
    snapshot = copy.deepcopy(contract)
    snapshot.pop('project_review', None)
    for item in snapshot['assets']:
        item.pop('review', None)
        if item.get('upstream_source'):
            item.pop('path', None)
            item.pop('sha256', None)
    for item in snapshot['shots']:
        item.pop('review', None)
        item['upstream'] = [{k: v for k, v in u.items() if k not in
                            {'attempt_id', 'output_sha256', 'outgoing_frame_sha256', 'review_sha256'}}
                            for u in item.get('upstream', [])]
    cards = scene_plan.get('metadata', {}).get('visual_development', {}).get('shot_cards', {})
    from lib.shot_contract import shot_local
    local = shot_local(contract)
    if local:
        assets.sort(key=lambda asset: asset['id'])
    return copy.deepcopy({'story': contract['story'], 'shot': shot, 'assets': assets,
        'late_cast_ids': contract['late_cast_ids'], 'payoff_speaker_ids': contract['payoff_speaker_ids'],
        'payoff_asset_id': contract.get('payoff_asset_id'), 'implicit_cast_ids': sorted(cast),
        'order': {'shots': [s['id'] for s in contract['shots']],
                  'scenes': [s['id'] for s in scene_plan['scenes']],
                  'script_sections': [s['id'] for s in script['sections']]},
        'scene': local_timing(scenes[0]) if local else scenes[0],
        'script_section': local_timing(sections[0]) if local else sections[0],
        'shot_card': cards.get(shot_id) if isinstance(cards, dict) else cards,
        'manifest_requirements': _manifest_requirement_binding(manifest, shot_id),
        'contract_snapshot': snapshot, 'scene_snapshot': copy.deepcopy(scene_plan)})


def local_timing(record):
    """Clip planning binds relative timing; cut placement belongs to assembly."""
    value = copy.deepcopy(record)
    if 'start_seconds' in value:
        start = value['start_seconds']
        if 'end_seconds' in value:
            value['end_seconds'] = round(value['end_seconds'] - start, 6)
        value['start_seconds'] = 0
    return value


def current_projection(root, shot_id):
    from lib.production_execution import _artifact_path
    def read(name):
        value = _json(_artifact_path(Path(root), name + '.json'))
        Draft202012Validator(load_schema(name)).validate(value)
        return value
    manifest_path = _artifact_path(Path(root), 'asset_manifest.json')
    projection = static_planning_projection(read('shot_contract'), read('scene_plan'), read('script'),
                                          shot_id, _json(manifest_path) if manifest_path.exists() else None)
    for asset in projection['assets']:
        if not asset.get('upstream_source') and not asset.get('preparation_pending'):
            if hashlib.sha256(_inside(root, asset['path']).read_bytes()).hexdigest() != asset['sha256']:
                raise AutonomyError('static reference bytes changed')
    return projection


def dialogue_occurrences(projection):
    return [{'index': i, 'source_pointer': f'/shot/dialogue/{i}', 'line': copy.deepcopy(line)}
            for i, line in enumerate(projection['shot'].get('dialogue', []))]


def baseline_descriptor(retained, root=None):
    if set(retained) - {'export_profile'} != {'projection', 'planned_request_template', 'script_snapshot',
                         'dialogue_coverage', 'approval_plan_sha256'}:
        raise AutonomyError('incomplete retained baseline')
    if 'export_profile' in retained:
        from lib.media_profiles import get_profile
        if not isinstance(retained['export_profile'], str) or not retained['export_profile']:
            raise AutonomyError('retained export profile must be an explicit profile name')
        try:
            get_profile(retained['export_profile'])
        except ValueError as exc:
            raise AutonomyError('invalid retained export profile: ' + str(exc)) from exc
    projection = retained['projection']
    if retained['dialogue_coverage'] != dialogue_occurrences(projection):
        raise AutonomyError('ordered dialogue retention differs')
    template = retained['planned_request_template']
    if set(template) != {'inputs', 'static_input_assets'}:
        raise AutonomyError('invalid retained request template')
    _refuse_unknown_cost(template['inputs'])
    static = []
    for item in template['static_input_assets']:
        assets = [a for a in projection['assets'] if a.get('path') and
                  (_inside(root, a['path']) == _inside(root, item['path']) if root is not None
                   else a['path'] == item['path'])]
        if len(assets) != 1 or assets[0].get('sha256') != item['sha256']:
            raise AutonomyError('static template bytes differ from baseline')
        static.append({'binding': item['role'], 'role': assets[0]['role'], 'sha256': item['sha256']})
    upstream = []
    def visit(value, pointer=''):
        if isinstance(value, dict):
            if '$board' in value:
                from lib.production_execution import _board_binding
                binding = _board_binding(value)
                slots = [a for a in projection['assets'] if a['id'] == binding['asset_id']]
                if projection['contract_snapshot'].get('planning_mode') != 'shot_local_v1' or len(slots) != 1:
                    raise AutonomyError('declared board input lies outside prospective consumer baseline')
            elif '$upstream' in value:
                from lib.production_execution import _upstream_binding
                binding = _upstream_binding(value)
                upstream.append({'name': '$upstream' + pointer.replace('/', '.'), **binding})
            else:
                for k, v in value.items():
                    visit(v, pointer + '/' + k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                visit(v, pointer + '/' + str(i))
    visit(template['inputs'])
    from lib.production_execution import approval_plan_digest
    if approval_plan_digest(projection['contract_snapshot'],
            projection['shot']['id'] if projection['contract_snapshot'].get('planning_mode') == 'shot_local_v1' else None) != retained['approval_plan_sha256']:
        raise AutonomyError('retained full approval plan digest differs')
    return {'planned_request_template_sha256': digest(template), 'static_input_assets': static,
            'upstream': upstream, 'approval_plan_digest': retained['approval_plan_sha256'],
            'script_snapshot_sha256': digest(retained['script_snapshot']),
            'dialogue_coverage_sha256': digest(retained['dialogue_coverage']),
            'static_source_packet_sha256': digest(projection)}


def _canonical_global_planning(policy, baselines, durations):
    """Replay local scaling plus deterministic downstream shifts on retained bytes."""
    first = next(iter(baselines.values()))
    contract = copy.deepcopy(first['projection']['contract_snapshot'])
    scene_plan = copy.deepcopy(first['projection']['scene_snapshot'])
    script = copy.deepcopy(first['script_snapshot'])
    for baseline in baselines.values():
        if baseline['projection']['contract_snapshot'] != first['projection']['contract_snapshot'] or baseline['projection']['scene_snapshot'] != first['projection']['scene_snapshot'] or baseline['script_snapshot'] != first['script_snapshot']:
            raise AutonomyError('retained global planning snapshots disagree')
    for shot_id, duration in durations.items():
        old = baselines[shot_id]['projection']['shot']['duration_seconds']
        if duration != old and duration not in policy['flex']['duration_s']:
            raise AutonomyError('duration outside approved flex')
    if all(duration == baselines[shot_id]['projection']['shot']['duration_seconds']
           for shot_id, duration in durations.items()):
        # No retime: the deterministic transform is the identity on retained bytes.
        # Reference-free contracts may legitimately carry an empty assets list.
        return contract, scene_plan, script
    raise AutonomyError('retimed planning is retired; each original keeps its authored duration')


def _validate_global_planning(root, policy, baselines):
    from lib.production_execution import _artifact_path
    durations = {shot_id: current_projection(root, shot_id)['shot']['duration_seconds'] for shot_id in baselines}
    expected_contract, expected_scene, expected_script = _canonical_global_planning(policy, baselines, durations)
    current = current_projection(root, next(iter(baselines)))
    script = _json(_artifact_path(Path(root), 'script.json'))
    _apply_reference_planning_flex(policy, expected_contract, current['contract_snapshot'])
    if current['contract_snapshot'] != expected_contract or current['scene_snapshot'] != expected_scene or script != expected_script:
        raise AutonomyError('whole retained planning changed outside deterministic flex')


def _validate_local_protected_planning(root, policy, baselines):
    """Prospective policy keeps story/order global, live eligibility local."""
    from lib.production_execution import load_shot_contract, _artifact_path
    from lib.shot_contract import protected_planning, shot_local
    current = load_shot_contract(root)
    if not shot_local(current):
        raise AutonomyError('local policy requires explicitly prospective contract')
    first = next(iter(baselines.values()))
    protected = protected_planning(first['projection']['contract_snapshot'])
    if protected_planning(current) != protected:
        raise AutonomyError('protected story or shot order changed')
    scene = _json(_artifact_path(Path(root), 'scene_plan.json'))
    script = _json(_artifact_path(Path(root), 'script.json'))
    order = {'shots': [s['id'] for s in current['shots']], 'scenes': [s['id'] for s in scene['scenes']],
             'script_sections': [s['id'] for s in script['sections']]}
    for retained in baselines.values():
        if (protected_planning(retained['projection']['contract_snapshot']) != protected
                or retained['projection']['order'] != order):
            raise AutonomyError('retained protected story/order differs')


def _round(value):
    return round(value, 6)


def retime(projection, new_duration):
    """Deterministic proportional retiming; text/speakers/source/order/story/end unchanged.

    A measured line that no longer fits raises; speech is never fabricated to fit.
    """
    if isinstance(new_duration, bool) or not isinstance(new_duration, (int, float)) \
            or not math.isfinite(new_duration) or new_duration <= 0:
        raise AutonomyError('duration must be finite and positive')
    out = copy.deepcopy(projection)
    shot = out['shot']
    factor = new_duration / shot['duration_seconds']
    shot['duration_seconds'] = new_duration
    for line in shot.get('dialogue', []):
        for key in ('start_seconds', 'end_seconds'):
            if key in line:
                line[key] = _round(line[key] * factor)
        measured = line.get('measured_duration_seconds')
        if measured is not None and line['end_seconds'] - line['start_seconds'] + 1e-9 < measured:
            raise AutonomyError(f'measured line {line.get("id")} does not fit {new_duration}s')
    for asset in out['assets']:
        if 'time_seconds' in asset and asset['id'] in shot['asset_ids']:
            asset['time_seconds'] = _round(asset['time_seconds'] * factor)
    scene = out['scene']
    if 'end_seconds' in scene:
        scene['end_seconds'] = _round(scene.get('start_seconds', 0) + new_duration)
    section = out['script_section']
    if 'start_seconds' in section and 'end_seconds' in section:
        section['end_seconds'] = _round(section['start_seconds'] + (section['end_seconds'] - section['start_seconds']) * factor)
    return out


def _diff(a, b, path=''):
    if type(a) is not type(b):
        return [path or '$']
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a[k], b[k], f'{path}/{k}') if k in a and k in b else [f'{path}/{k}']
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [path or '$']
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in _diff(x, y, f'{path}/{i}')]
    return [] if a == b else [path or '$']


def contract_delta(root, shot_id, candidate, *, policy=None):
    """Compare only against retained evidence, never against a caller baseline."""
    if policy is None:
        policy, _, _ = require_active_policy(root, shot_id=shot_id)
    baseline = retained_baselines(root, policy)[shot_id]['projection']
    if candidate.get('contract_snapshot') != current_projection(root, shot_id)['contract_snapshot'] or candidate.get('scene_snapshot') != current_projection(root, shot_id)['scene_snapshot']:
        raise AutonomyError('candidate global planning differs from validated current bytes')
    current = current_projection(root, shot_id)
    duration = candidate.get('shot', {}).get('duration_seconds')
    if candidate != current and (duration not in policy['flex']['duration_s'] or candidate != retime(current, duration)):
        raise AutonomyError('candidate is not actual planning or deterministic prospective retime')
    return _validate_delta(policy, shot_id, candidate, baseline, allow_global_shift=True, root=root)


def _validate_delta(policy, shot_id, candidate, baseline_projection, *, allow_global_shift=False, root=None):
    """Return ``(flexed_duration_or_None, changed_paths)``; raise outside the envelope."""
    baseline = policy['shots'].get(shot_id)
    if baseline is None:
        raise AutonomyError(f'no frozen baseline for {shot_id}')
    if digest(baseline_projection) != baseline['static_source_packet_sha256']:
        raise AutonomyError('baseline projection is not the approved frozen packet')
    candidate = copy.deepcopy(candidate)
    baseline_projection = copy.deepcopy(baseline_projection)
    for value in (candidate, baseline_projection):
        value.pop('contract_snapshot', None)
        value.pop('scene_snapshot', None)
    if policy.get('planning_mode') == 'shot_local_v1':
        observed = {asset['id']: asset for asset in candidate['assets']}
        materialized = {}
        for index, asset in enumerate(baseline_projection['assets']):
            if not asset.get('preparation_pending'):
                continue
            current = observed.get(asset['id'])
            if current and not current.get('preparation_pending'):
                if root is None:
                    raise AutonomyError('pending source materialization needs canonical attribution')
                _validate_generated_board_source(root, current, expected=asset)
                baseline_projection['assets'][index] = copy.deepcopy(current)
                materialized[asset['id']] = current
        if materialized:
            expected, rows = _materialized_manifest_requirements(
                root, shot_id, baseline_projection['manifest_requirements'], materialized)
            observed_rows = candidate['manifest_requirements']['assets']
            if sorted(row['id'] for row in observed_rows) == sorted(rows):
                # Row order follows promotion order; ids and contents stay exact.
                expected['assets'] = [copy.deepcopy(rows[row['id']]) for row in observed_rows]
                baseline_projection['manifest_requirements'] = expected
    if allow_global_shift and policy.get('planning_mode') != 'shot_local_v1':
        baseline_projection['assets'] = copy.deepcopy(candidate['assets'])
        baseline_projection['shot']['asset_ids'] = copy.deepcopy(candidate['shot']['asset_ids'])
        for key in ('scene', 'script_section'):
            for timing_key in ('start_seconds', 'end_seconds'):
                if timing_key in candidate[key]:
                    candidate[key][timing_key] = baseline_projection[key][timing_key]
    if candidate == baseline_projection:
        return None, []
    duration = candidate.get('shot', {}).get('duration_seconds')
    if duration in policy['flex']['duration_s']:
        try:
            expected = retime(baseline_projection, duration)
            if allow_global_shift:
                for key in ('scene', 'script_section'):
                    for timing_key in ('start_seconds', 'end_seconds'):
                        expected[key][timing_key] = baseline_projection[key][timing_key]
            if expected == candidate:
                return duration, _diff(baseline_projection, candidate)
        except AutonomyError:
            pass
    raise AutonomyError('contract changed outside approved flex: ' + ', '.join(_diff(baseline_projection, candidate)))


# ---------------------------------------------------------------- route eligibility

def _visible_enum(control):
    """Only visible ``{'value': ...}`` members; redacted hashes are never supported values."""
    return [e['value'] for e in control.get('enum') or [] if isinstance(e, dict) and 'value' in e]


def _control_ok(lock, control):
    if not isinstance(control, dict) or control.get('observed_in_form') is not True \
            or control.get('qualified_in_exact_preview') is not True:
        return False
    preview = control.get('preview') or {}
    if 'value' in lock:
        enum = _visible_enum(control)
        if control.get('enum') is not None:
            return lock['value'] in enum
        return preview.get('value') == lock['value']
    return preview.get('value_sha256') == lock['value_sha256']


def _model_lock_allows(lock, name):
    if 'value' in lock:
        return lock['value'] == name
    return lock['value_sha256'] == hashlib.sha256(str(name).encode()).hexdigest()


def eligible_routes(policy, shot_id, menu):
    """Filter actual ``ToolRegistry.qualified_cli_video_routes()`` rows.

    Returns ``(eligible, excluded)``. Eligibility is necessary, not sufficient:
    Grok rows still need native-argument lock proof at preparation.
    """
    if shot_id not in policy['shots']:
        raise AutonomyError(f'{shot_id} has no approved baseline')
    providers = {p['id']: p for p in policy['providers']}
    locks = policy['locked']['controls']
    eligible, excluded = [], []
    for row in menu:
        provider = row.get('provider')
        if provider == 'openart_mcp':
            spec = providers.get(provider)
            if not _mcp_billing_supported(spec) or row.get('production_available') is not True:
                excluded.append({'provider': provider, 'reason': 'not approved or not production-ready'})
                continue
            from lib import openart_mcp as mcp
            for route in spec['routes']:
                name, mode = route['model'], route['mode']
                metadata = (row.get('model_catalog', {}).get(name, {}).get('modes') or {}).get(mode, {})
                try:
                    if metadata.get('production_ready') is not True:
                        raise AutonomyError('model/mode is not a production-ready native route')
                    profile = mcp.load_profile(name, mode, require='supported')
                    if profile.get('account_uid_sha256') != spec['uid_sha256']:
                        raise AutonomyError('account differs from approved account')
                    if 'model' in locks and not _model_lock_allows(locks['model'], name):
                        raise AutonomyError('exact model lock excludes model')
                    if not _intent_allows(policy, shot_id, provider, name):
                        raise AutonomyError('model selection intent excludes route')
                    # Value locks are proved against exact typed native params at preparation.
                    eligible.append({'provider': provider, 'model': name, 'mode': mode,
                        'profile_sha256': profile['profile_sha256'], 'pending_lock_proof': sorted(locks)})
                except (ValueError, KeyError) as exc:
                    excluded.append({'provider': provider, 'model': name, 'mode': mode, 'reason': str(exc)})
        elif provider == 'openart_cli':
            spec = providers.get(provider)
            if not spec:
                excluded.append({'provider': provider, 'reason': 'not in policy'})
                continue
            if row.get('status') != 'available' or row.get('production_available') is not True:
                excluded.append({'provider': provider, 'reason': 'not production available'})
                continue
            readiness = row.get('dispatch_readiness') or {}
            if readiness.get('ready') is not True:
                excluded.append({'provider': provider, 'reason': 'dispatch not ready',
                                 'blockers': list(readiness.get('blockers') or [])})
                continue
            shadow = {(m.get('model'), m.get('mode')) if isinstance(m, dict) else (m, None)
                      for key in ('qualification_candidates', 'not_live') for m in row.get(key) or []}
            for model in row.get('models') or []:
                name = model.get('model')
                native = (model.get('controls') or {}).get('native_controls')
                why = None
                if not _openart_route_approved(spec, name, model.get('mode')):
                    why = 'model not approved'
                elif (name, model.get('mode')) in shadow or (name, None) in shadow:
                    why = 'model also listed as candidate/not live'
                elif model.get('production_ready') is not True or model.get('level') not in ('pre_submit', 'full') \
                        or model.get('catalog_verified') is not True:
                    why = 'model not production-ready and catalog verified'
                elif model.get('account_id_sha256') != spec['account_id_sha256']:
                    why = 'account differs from approved account'
                elif 'model' in locks and not _model_lock_allows(locks['model'], name):
                    why = 'exact model lock excludes model'
                elif not _intent_allows(policy, shot_id, provider, name):
                    why = 'model selection intent excludes route'
                elif any(c != 'model' for c in locks) and not isinstance(native, dict):
                    why = 'native controls unverified'
                else:
                    bad = sorted(c for c, lock in locks.items() if c != 'model' and not _control_ok(lock, native.get(c)))
                    if bad:
                        why = 'locked controls not qualified: ' + ', '.join(bad)
                if why:
                    excluded.append({'provider': provider, 'model': name, 'reason': why})
                else:
                    eligible.append({'provider': provider, 'model': name, 'mode': model.get('mode'),
                                     'profile_sha256': model.get('profile_sha256')})
        elif provider == 'grok_cli':
            why = None
            if provider not in providers:
                why = 'not in policy'
            elif row.get('status') != 'available':
                why = 'not available'
            elif row.get('model_policy') != GROK_MODEL_POLICY or row.get('models') != [] \
                    or row.get('model_selection') != 'not_supported':
                why = 'grok media model disclosure mismatch'
            elif 'model' in locks:
                why = 'exact media-model lock: Grok media model is unreported'
            elif not _intent_allows(policy, shot_id, provider, None):
                why = 'model selection intent excludes route'
            if why:
                excluded.append({'provider': provider, 'reason': why})
            else:
                eligible.append({'provider': provider, 'model': None, 'model_policy': GROK_MODEL_POLICY,
                                 'pending_lock_proof': sorted(locks)})
        else:
            excluded.append({'provider': provider, 'reason': 'provider outside Auto-continue'})
    return eligible, excluded


# ---------------------------------------------------------------- lock proof

def lock_proof(root, shot_id, *, inputs, native, profile, policy_context=None):
    """Prospective callers omit ``policy_context`` and require the active policy.
    Only retained-authority replay passes its already-proved (policy, sha)."""
    return _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile,
                            policy_context=policy_context)


def _historical_current_preparation(root, inputs, native, profile, historical_replay=False):
    """Validate current named preparation for retained-authority replay only.

    Uses the original-review historical source packet and the shared evidence
    validator; prospective admission keeps ``preparation.validate_preparation``.
    ``historical_replay`` is set only by genuine retained collection; a bare
    frozen policy context keeps the current-cut creator override check.
    """
    from lib import production_request as preparation
    try:
        compiled = preparation._read(Path(root), 'compiled_request-' + preparation._id(inputs['compiled_request_id']) + '.json')
        review = preparation._read(Path(root), 'preparation_review-' + preparation._id(inputs['preparation_review_id']) + '.json')
        packet = preparation._historical_source_packet(root, inputs['governance']['shot_id'],
                                                       provider=native.get('provider', 'openart_cli'), native=native,
                                                       reference_bindings=compiled['source_binding']['references'])
        return preparation.validate_preparation_evidence(compiled, review, inputs, native, profile, packet,
                                                         historical=historical_replay)
    except (ValueError, KeyError) as exc:
        raise AutonomyError(f'historical named preparation is incomplete: {exc}') from exc


def _generated_carrier_sources(root, pack, frame):
    """Exact original-input ancestry of one current, declared generated carrier."""
    from lib import production_boards as boards, production_images as images
    current = pack['board_bindings'][frame['asset_id']]
    if (current['sha256'] != frame['sha256'] or _inside(root, current['path']) != _inside(root, frame['path'])
            or not boards._binding_current(root, pack, frame['asset_id'])):
        raise AutonomyError('generated carrier ancestry has stale bytes or a cycle')
    sources = {}
    visited = set()
    def visit(asset_id):
        if asset_id in visited:
            return
        binding = pack['board_bindings'][asset_id]
        node = pack['prompt_pack'][asset_id]
        roles = boards._roles(pack, asset_id)
        if not roles or any(boards._review_blockers(pack, asset_id, role) for role in roles):
            raise AutonomyError('generated carrier ancestor lacks current declared role evidence')
        if binding.get('origin') != 'canonical_image_import':
            raise AutonomyError('generated carrier ancestor lacks original image provenance')
        call = images._call(images._load(root), binding.get('call_id'))
        scope = images._scope(root, call['scope_id'])
        approved = scope.get('requests', {}).get(call['slot_id'])
        if isinstance(approved, list):
            index = call['scope_attempt_index']
            approved = approved[index] if type(index) is int and 0 <= index < len(approved) else None
        receipt = images._read(images._bound_file(root, call['host_receipt'], 'carrier original host return'))
        if (call['state'] != 'imported' or call['route'] != images.NATIVE_ROUTE
                or call['tool_name'] != images.NATIVE_TOOL or call['slot_id'] != asset_id
                or call['project_id'] != pack['project_id'] or call['story_revision'] != pack['story_revision']
                or call['packet']['project_id'] != pack['project_id'] or call['packet']['story_revision'] != pack['story_revision']
                or digest(scope) != call['scope_sha256']
                or approved != call['request_sha256']
                or call['packet']['packet_sha256'] != digest({k: v for k, v in call['packet'].items() if k != 'packet_sha256'})
                or images.image_request_digest(call['packet'], call['slot_id'], call['host_arguments'],
                    output_path=call['output_path']) != call['request_sha256']
                or call['output']['sha256'] != binding['sha256']
                or _inside(root, call['output']['path']) != _inside(root, binding['path'])
                or _inside(root, call['output_path']) != _inside(root, binding['path'])
                or receipt.get('tool_name') != images.NATIVE_TOOL
                or receipt.get('arguments') != call['submitted_arguments']
                or call['provenance'].get('kind') != 'agent_recorded_host_return'
                or call['provenance'].get('tool_name') != images.NATIVE_TOOL
                or call['provenance'].get('receipt_sha256') != call['host_receipt']['sha256']
                or call['provenance'].get('arguments_sha256') != digest(call['submitted_arguments'])):
            raise AutonomyError('generated starting carrier lacks exact original image provenance')
        images._bound_file(root, {'path': call['output']['preserved_path'], 'sha256': binding['sha256']},
                           'preserved starting carrier')
        reference_ids = node.get('reference_ids', [])
        dependencies = node.get('image_dependencies', [])
        declared = reference_ids + dependencies
        if len(set(declared)) != len(declared) or set(binding.get('source_bindings', {})) != set(declared):
            raise AutonomyError('generated carrier inputs differ from declared ancestry')
        originals = call['host_arguments'].get('referenced_image_paths', [])
        submitted = call['submitted_arguments'].get('referenced_image_paths', [])
        if len(originals) != len(declared) or len(originals) != len(submitted):
            raise AutonomyError('generated carrier input occurrences differ from declared ancestry')
        for source_id in declared:
            expected = binding['source_bindings'][source_id]
            source = pack['board_bindings'].get(source_id)
            refs = [r for r in call['packet']['references'] if r['id'] == source_id
                    and all(r[key] == expected[key] for key in ('path', 'sha256'))]
            indices = [i for i, path in enumerate(originals) if _inside(root, path) == _inside(root, expected['path'])]
            if (source is None or any(source[key] != expected[key] for key in ('path', 'sha256'))
                    or len(refs) != 1 or len(indices) != 1
                    or hashlib.sha256(_inside(root, submitted[indices[0]]).read_bytes()).hexdigest() != expected['sha256']):
                raise AutonomyError('generated carrier ancestor was not an actual original image input')
            if source_id in dependencies:
                visit(source_id)
                sources[source_id] = expected
            else:
                references = [r for r in pack['specification']['references'] if r['id'] == source_id]
                if len(references) != 1 or any(references[0][key] != expected[key] for key in ('path', 'sha256')):
                    raise AutonomyError('generated carrier ancestor source differs from declared reference')
                sources[source_id] = expected
        visited.add(asset_id)
    visit(frame['asset_id'])
    return sources


def _frame_carrier_members(root, frame, projection, composites):
    """A frame-mode composite names exact approved sources with truthful membership.

    Named semantic proof remains available for provided boards. For a generated
    board, also corroborate membership against its actual original image inputs.
    """
    from lib import production_boards as boards, production_images as images
    root = Path(root).resolve()
    pack = boards.load_board_pack(root)
    binding = pack['board_bindings'][frame['asset_id']]
    generated = binding.get('origin') == 'canonical_image_import'
    call = None
    if composites and generated:
        call = images._call(images._load(root), binding.get('call_id'))
        scope = images._scope(root, call['scope_id'])
        approved = scope.get('requests', {}).get(call['slot_id'])
        if isinstance(approved, list):
            index = call['scope_attempt_index']
            approved = approved[index] if type(index) is int and 0 <= index < len(approved) else None
        receipt = images._read(images._bound_file(root, call['host_receipt'], 'carrier original host return'))
        if (call['state'] != 'imported' or call['route'] != images.NATIVE_ROUTE
                or call['tool_name'] != images.NATIVE_TOOL or call['slot_id'] != frame['asset_id']
                or call['project_id'] != pack['project_id'] or call['story_revision'] != pack['story_revision']
                or digest(scope) != call['scope_sha256']
                or approved != call['request_sha256']
                or call['packet']['packet_sha256'] != digest({k: v for k, v in call['packet'].items() if k != 'packet_sha256'})
                or images.image_request_digest(call['packet'], call['slot_id'], call['host_arguments'],
                    output_path=call['output_path']) != call['request_sha256']
                or call['output']['sha256'] != frame['sha256']
                or _inside(root, call['output']['path']) != Path(frame['path'])
                or _inside(root, call['output_path']) != Path(frame['path'])
                or receipt.get('tool_name') != images.NATIVE_TOOL
                or receipt.get('arguments') != call['submitted_arguments']
                or call['provenance'].get('kind') != 'agent_recorded_host_return'
                or call['provenance'].get('tool_name') != images.NATIVE_TOOL
                or call['provenance'].get('receipt_sha256') != call['host_receipt']['sha256']
                or call['provenance'].get('arguments_sha256') != digest(call['submitted_arguments'])):
            raise AutonomyError('generated starting carrier lacks exact original image provenance')
        images._bound_file(root, {'path': call['output']['preserved_path'], 'sha256': frame['sha256']},
                           'preserved starting carrier')
    seen = set()
    for composite in composites:
        if composite['sha256'] != frame['sha256'] or composite['role'] != 'start_frame':
            raise AutonomyError('frame composite does not name the actual declared starting carrier')
        for member in composite['members']:
            # Pending (not yet materialized) assets legitimately lack sha256;
            # they can never match an exact approved member, so skip them
            # instead of crashing. Missing/ambiguous members still fail closed.
            matches = [a for a in projection['assets']
                if all(key in a and a[key] == member[key] for key in ('sha256', 'role', 'cast_ids'))]
            key = (member['sha256'], member['role'], tuple(member['cast_ids']))
            if len(matches) != 1 or key in seen:
                raise AutonomyError('frame composite member is not one exact approved source')
            seen.add(key)
            if not generated:
                continue
            asset = matches[0]
            expected = {key: asset[key] for key in ('path', 'sha256')}
            refs = [r for r in call['packet']['references'] if r['id'] == asset['id']
                    and all(r[key] == expected[key] for key in expected)]
            originals = call['host_arguments'].get('referenced_image_paths', [])
            submitted = call['submitted_arguments'].get('referenced_image_paths', [])
            indices = [i for i, path in enumerate(originals) if _inside(root, path) == _inside(root, asset['path'])]
            if (binding.get('source_bindings', {}).get(asset['id']) != expected or len(refs) != 1
                    or len(indices) != 1 or len(originals) != len(submitted)
                    or hashlib.sha256(_inside(root, submitted[indices[0]]).read_bytes()).hexdigest() != asset['sha256']):
                sources = _generated_carrier_sources(root, pack, frame)
                if sources.get(asset['id']) != expected:
                    raise AutonomyError('frame composite member was not an actual generated carrier input')


def _root_lock_proof(root, shot_id, *, inputs, native, profile, historical=None, policy_context=None,
                     retained_lock_digest=None, historical_replay=False, _legacy_frame=False,
                     _return_preparation=False):
    """Derive locks; fresh scope material may also consume the validated preparation result."""
    if _return_preparation and (historical is not None or policy_context is not None):
        raise AutonomyError('preparation result sharing requires fresh rooted preparation')
    from lib import production_request as preparation
    from lib.production_execution import _paths, _clean, _artifact_path
    # A retained historical payload is always genuine replay; otherwise only an
    # explicit retained-collection caller opts in (never policy_context alone).
    historical_replay = historical is not None or bool(historical_replay)
    if retained_lock_digest is not None and not _legacy_frame:
        if historical is None and policy_context is None:
            raise AutonomyError('legacy lock replay requires retained historical authority')
        # Most originals carry the modern proof. Validate it once before trying
        # the historical frame interpretation, preserving exact legacy recovery.
        error = None
        try:
            current = _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile,
                historical=historical, policy_context=policy_context, historical_replay=historical_replay)
        except (ValueError, KeyError, OSError) as exc:
            current, error = None, exc
        if current == retained_lock_digest:
            return current
        try:
            legacy = _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile,
                historical=historical, policy_context=policy_context, historical_replay=historical_replay,
                _legacy_frame=True)
        except (ValueError, KeyError, OSError):
            legacy = None
        if legacy == retained_lock_digest:
            return legacy
        if error is not None:
            raise error
        return current
    if historical is None and policy_context is None:
        policy, sha, _ = require_active_policy(root, shot_id=shot_id)
    elif historical is None:
        # Retained-authority replay: the caller already proved the historical policy.
        policy, sha = policy_context
    else:
        policy, sha = historical['policy'], historical['policy_sha256']
    retained = retained_baselines(root, policy)[shot_id]
    projection = retained['projection']
    if policy.get('planning_mode') == 'shot_local_v1':
        projection = _materialized_projection(root, projection)
    contract_delta(root, shot_id, current_projection(root, shot_id), policy=policy)
    if historical is None:
        if policy_context is None:
            preparation_result = preparation.validate_preparation(inputs, native, profile)
        else:
            _historical_current_preparation(root, inputs, native, profile, historical_replay)
        compiled = _json(_artifact_path(Path(root), 'compiled_request-' + preparation._id(inputs['compiled_request_id']) + '.json'))
        review = _json(_artifact_path(Path(root), 'preparation_review-' + preparation._id(inputs['preparation_review_id']) + '.json'))
    else:
        compiled, review = historical['compiled'], historical['review']
    actual = []
    def record(role, path):
        matched = [a for a in projection['assets'] if a.get('path') and _inside(root, a['path']) == path]
        actual.append({'binding': role, 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'role': matched[0]['role'] if len(matched) == 1 else None})
        return str(path)
    _paths(_clean(inputs), Path(root).resolve(), record)
    frozen = {(a['sha256'], a['role']) for a in actual}
    boards = review.get('composite_boards', [])
    frame = None
    carrier_frozen = frozen
    if not _legacy_frame and projection['shot'].get('reference_mode', projection['contract_snapshot'].get('reference_mode')) == 'frame_conditioned':
        from lib.production_execution import load_shot_contract
        from lib.shot_contract import frame_conditioned_start
        contract = load_shot_contract(root)
        shot = next(s for s in contract['shots'] if s['id'] == shot_id)
        frame = frame_conditioned_start(contract, shot, root)
        if (compiled['source_binding'].get('frame_input') != frame or not any(
                a['path'] == frame['path'] and a['sha256'] == frame['sha256'] for a in actual)):
            raise AutonomyError('actual inputs do not carry the exact declared starting frame')
        _frame_carrier_members(root, frame, projection, boards)
        # A payoff board may have an independently authored/observed start role.
        # Preserve its canonical primary role in actual inputs and direct locks.
        carrier_frozen = frozen | {(frame['sha256'], 'start_frame')}
    proof = {'policy_sha256': sha, 'baseline_sha256': digest(retained),
             'compiled_sha256': digest(compiled), 'review_sha256': digest(review),
             'inputs': actual, 'cast': [], 'controls': {}}
    ancestry_checked = False
    for asset in projection['assets']:
        if asset.get('upstream_source'):
            continue
        locked_cast = (asset['id'] == projection.get('payoff_asset_id') or
                       bool(set(asset.get('cast_ids', [])) & set(projection['implicit_cast_ids'])))
        if not locked_cast and asset['sha256'] not in policy['locked']['sources']:
            continue
        semantic_target = any(row['id'] == asset['id'] and row['role'] == 'payoff_board'
            and row['sha256'] == asset['sha256'] and row.get('native_required') is False
            for row in compiled['source_binding']['references'])
        if semantic_target and asset['role'] == 'payoff_board' and asset['sha256'] not in policy['locked']['sources']:
            # The validated compact package keeps this target in protected
            # semantics/source bytes without declaring a native image consumer.
            # Explicit source/cast locks below retain their normal obligations.
            proof['cast'].append([asset['id'], asset['sha256'], 'semantic_target'])
            continue
        if frame is not None and asset['role'] == 'end_frame' and (asset['sha256'], asset['role']) not in frozen:
            # Reviewed endpoint targets remain protected planning/preparation
            # bytes; explicit source/cast and required native pins run separately.
            continue
        if (asset['sha256'], asset['role']) in frozen:
            proof['cast'].append([asset['id'], asset['sha256'], 'direct'])
            continue
        carriers = [b for b in boards if (b['sha256'], 'start_frame') in carrier_frozen and b['role'] == 'start_frame'
                    and any(m['sha256'] == asset['sha256'] and m['role'] == asset['role']
                            and m['cast_ids'] == asset['cast_ids'] for m in b['members'])]
        if not carriers and frame is not None and not ancestry_checked:
            from lib import production_boards as board_pack
            pack = board_pack.load_board_pack(root)
            if pack['board_bindings'][frame['asset_id']].get('origin') == 'canonical_image_import':
                sources = _generated_carrier_sources(root, pack, frame)
                members = [{key: copy.deepcopy(a[key]) for key in ('sha256', 'role', 'cast_ids')}
                    for a in projection['assets'] if a.get('cast_ids')
                    and a['role'] in {'identity_reference', 'start_frame', 'end_frame', 'payoff_board'}
                    and sources.get(a['id']) == {key: a.get(key) for key in ('path', 'sha256')}]
                # Input ancestry is mechanical provenance, not an added review.
                # Retained preparation and direct-proof digests remain untouched.
                boards = boards + [{'sha256': frame['sha256'], 'role': 'start_frame', 'members': members}]
            ancestry_checked = True
            carriers = [b for b in boards if (b['sha256'], 'start_frame') in carrier_frozen and b['role'] == 'start_frame'
                        and any(m['sha256'] == asset['sha256'] and m['role'] == asset['role']
                                and m['cast_ids'] == asset['cast_ids'] for m in b['members'])]
        if not carriers:
            raise AutonomyError(f'implicit locked source/cast {asset["id"]} is absent from actual inputs and named board proof')
        proof['cast'].append([asset['id'], asset['sha256'], carriers[0]['sha256']])
    for cast_id, members in policy['locked']['cast'].items():
        if cast_id not in projection['implicit_cast_ids']:
            continue
        for member in members:
            direct = (member['sha256'], member['role']) in frozen
            carrier = any((b['sha256'], 'start_frame') in carrier_frozen and any(
                m['sha256'] == member['sha256'] and m['role'] == member['role'] and cast_id in m['cast_ids']
                for m in b['members']) for b in boards)
            if not direct and not carrier:
                raise AutonomyError('explicit cast lock is absent from actual input/board proof')
    for source_sha in policy['locked']['sources']:
        if not any(actual_sha == source_sha for actual_sha, _ in frozen):
            raise AutonomyError('explicit source lock is absent from actual input bytes')
    for predicate in policy['locked']['story_predicates']:
        if predicate['id'] not in projection['story'] or digest(projection['story'][predicate['id']]) != predicate['sha256']:
            raise AutonomyError('explicit story predicate is not rooted in retained story')
    for key, lock in policy['locked']['dialogue'].items():
        if not key.isdecimal() or int(key) >= len(projection['shot']['dialogue']):
            raise AutonomyError('explicit dialogue lock must name its ordered dialogue index')
        line = projection['shot']['dialogue'][int(key)]
        if hashlib.sha256(line['text'].encode()).hexdigest() != lock['text_sha256'] or line['speaker_id'] != lock['speaker_id']:
            raise AutonomyError('explicit ordered dialogue lock differs from baseline')
    # Full root canonical compilation preserves ordered duplicate speech/speaker/source,
    # including all implicit lines absent from explicit locked.dialogue maps.
    provider = native.get('provider', 'openart_cli')
    if historical is not None or policy_context is not None:
        canonical = preparation._compile_historical_provider_prompt(root, shot_id, provider=provider, native=native)
    else:
        canonical = preparation.compile_provider_prompt(root, shot_id, provider=provider,
            **({'model': native['model'], 'input_assets': inputs.get('input_assets'),
                'openart_project_id': native['body'].get('projectId')} if provider == 'openart_mcp' else {}))
    body = preparation._body(native)
    prompt = body['params'].get('sceneDescription' if provider == 'openart_mcp' and native.get('prompt_param_path') == '/params/sceneDescription' else 'prompt')
    exact = (preparation.historical_prompt_matches(prompt, canonical)
             if historical is not None or policy_context is not None else
             prompt in {canonical['prompt'], canonical.get('_mapping_free_prompt', canonical['prompt'])})
    if compiled.get('creator_prompt_override') is not None:
        # Retained creator prompt: read-only authentication of the frozen marker
        # against the exact submitted prompt, current historical source occurrences
        # and dialogue-only coverage. Fresh/prospective overrides stay refused.
        if not historical_replay:
            raise AutonomyError('submitted native prompt/coverage is not canonical root compilation')
        pointer = ('/arguments/prompt' if provider == 'grok_cli' else '/params/' + (
            'sceneDescription' if provider == 'openart_mcp' and native.get('prompt_param_path') == '/params/sceneDescription'
            else 'prompt'))
        try:
            packet = preparation._historical_source_packet(root, shot_id, provider=provider, native=native)
            preparation.historical_creator_prompt_coverage(compiled, prompt, packet, native_pointer=pointer)
        except (ValueError, KeyError, TypeError) as exc:
            raise AutonomyError(f'retained creator prompt override is not authenticated history: {exc}') from exc
    elif not exact or (not preparation.historical_preparation_matches(prompt, compiled['coverage'], canonical)
            if historical is not None or policy_context is not None else compiled['coverage'] != canonical['coverage']):
        raise AutonomyError('submitted native prompt/coverage is not canonical root compilation')
    if provider == 'grok_cli':
        actual_controls = dict(native['request']['arguments'])
        if 'resolution_name' in actual_controls:
            actual_controls['resolution'] = actual_controls['resolution_name']
        for key, alias in [('first_frame', 'start_frame'), ('last_frame', 'end_frame')]:
            if key in actual_controls:
                actual_controls[alias] = actual_controls[key]
    else:
        actual_controls = dict(body['params'], model=native['model'])
    if provider == 'openart_mcp':
        if 'aspectRatio' in actual_controls and 'videoAspectRatio' in actual_controls:
            raise AutonomyError('ambiguous duplicate native aspect ratio aliases')
        for public, field in [('duration', 'videoDuration'), ('resolution', 'videoResolution'), ('aspect_ratio', 'videoAspectRatio')]:
            if field in actual_controls:
                actual_controls[public] = actual_controls[field]
        if 'aspectRatio' in actual_controls:
            actual_controls['aspect_ratio'] = actual_controls['aspectRatio']
        for role, aliases in [('first_frame', ('first_frame', 'start_frame')), ('last_frame', ('last_frame', 'end_frame'))]:
            assets = [a for a in native['input_assets'] if a['role'] == role]
            if len(assets) > 1:
                raise AutonomyError('ambiguous MCP frame role lock')
            if assets:
                for alias in aliases:
                    actual_controls[alias] = assets[0]['source_path']
    for name, lock in policy['locked']['controls'].items():
        if name not in actual_controls:
            raise AutonomyError(f'native locked control absent: {name}')
        value = actual_controls[name]
        content_sha = hashlib.sha256(str(value).encode()).hexdigest()
        if name in {'start_frame', 'end_frame', 'first_frame', 'last_frame'}:
            if not isinstance(value, str) or not Path(value).is_file():
                raise AutonomyError('native frame control does not carry verified local content')
            content_sha = hashlib.sha256(Path(value).read_bytes()).hexdigest()
        if ('value' in lock and (digest(value) != digest(lock['value']) if provider == 'openart_mcp' else value != lock['value'])) or ('value_sha256' in lock and content_sha != lock['value_sha256']):
            raise AutonomyError(f'native locked control differs: {name}')
        proof['controls'][name] = digest(value)
    return (digest(proof), preparation_result) if _return_preparation else digest(proof)


# ---------------------------------------------------------------- caps

def attempt_counts(attempts, *, outbox_attempt_ids=(), open_scope_attempts=()):
    """Count every policy attempt regardless of state; unmapped outbox rows fail closed."""
    seen = {}
    for a in list(attempts) + list(open_scope_attempts):
        seen.setdefault(a['attempt_id'], (a['shot_id'], a.get('phase', 'first_pass')))
    unknown = sorted(set(outbox_attempt_ids) - set(seen))
    per_shot, repairs = {}, 0
    for shot, phase in seen.values():
        per_shot[shot] = per_shot.get(shot, 0) + 1
        repairs += phase == 'repair'
    return {'total': len(seen) + len(unknown), 'per_shot': per_shot, 'repair': repairs,
            'unmapped_outbox': unknown}


def check_caps(policy, shot_id, counts, phase, *, root=None):
    caps = effective_standing_caps(root, policy) if root is not None else policy['caps']
    if counts['unmapped_outbox']:
        raise AutonomyError('caps: unmapped outbox attempt (fail closed)')
    if counts['total'] + 1 > caps['max_total_attempts']:
        raise AutonomyError('caps: total attempts exhausted; deliver draft')
    if counts['per_shot'].get(shot_id, 0) + 1 > caps.get('max_attempts_by_shot', {}).get(shot_id, caps['max_attempts_per_shot']):
        raise AutonomyError(f'caps: attempts for {shot_id} exhausted; deliver draft')
    if phase != 'first_pass':
        raise AutonomyError('repair attempts are retired; deliver draft')


# ---------------------------------------------------------------- derived scope

def openart_allowance_id(sha):
    return f'policy:{sha}:openart'


def _retained_scope_record(policy, sha, decision_id, retained, **material):
    """Pure historical reader: reconstruct a retained scope for exact comparison.

    Retired repair scopes keep their recorded phase/replacement/basis fields so
    their counted originals stay collectable; no writer produces them."""
    scope = _scope_record(policy, sha, decision_id, **material)
    if retained.get('phase') == 'repair':
        if not retained.get('replaces_attempt_ids'):
            raise AutonomyError('historical repair scope must name replaced attempts')
        scope['phase'] = 'repair'
        scope['replaces_attempt_ids'] = list(retained['replaces_attempt_ids'])
        basis = retained.get('repair_basis', 'critical_review')
        if basis not in ('access_fallback', 'creator_batch', 'critical_review'):
            raise AutonomyError('unknown historical repair basis')
        if basis != 'critical_review':
            scope['repair_basis'] = basis
        if basis == 'creator_batch':
            if not isinstance(retained.get('creator_repair'), dict):
                raise AutonomyError('historical creator batch requires an exact item binding')
            scope['creator_repair'] = copy.deepcopy(retained['creator_repair'])
    elif retained.get('phase') != 'first_pass':
        raise AutonomyError('historical scope phase is invalid')
    return scope


def _scope_record(policy, sha, decision_id, *, shot_id, provider, request_digest, approval_plan_sha256,
                 derivation_index, lock_digest, compiled_request_sha256=None, preparation_review_id=None,
                 resolved_upstream=None, credit_authorization_sha256=None,
                 unknown_cost_authorization_sha256=None,
                 delivery_approval=None, native_audio_off_approval=None):
    """The v1.0 original (first_pass) production scope for one planned clip."""
    if shot_id not in policy['shots']:
        raise AutonomyError(f'{shot_id} has no approved baseline')
    if provider not in {p['id'] for p in policy['providers']}:
        raise AutonomyError(f'{provider} not approved by policy')
    scope = {
        'id': f'policy-{sha[:12]}-{shot_id}-{derivation_index}', 'status': 'approved',
        'approved_by': f'policy:{sha}', 'evidence': dict(policy['evidence']),
        'project_id': policy['project_id'], 'story_revision': policy['story_revision'],
        'phase': 'first_pass', 'provider': provider, 'requests': {shot_id: request_digest},
        'attempts_per_shot': {shot_id: 1}, 'approval_plan_sha256': approval_plan_sha256,
        'derived_from_policy': {
            'policy_sha256': sha, 'decision_id': decision_id, 'derivation_index': derivation_index,
            'baseline_sha256': digest(policy['shots'][shot_id]), 'lock_digest': lock_digest,
            'compiled_request_sha256': compiled_request_sha256,
            'preparation_review_id': preparation_review_id,
            'resolved_upstream': copy.deepcopy(resolved_upstream or {}),
        },
    }
    if delivery_approval is not None:
        proof_sha = digest(delivery_approval)
        scope['id'] += '-delivery-' + proof_sha[:12]
        scope['derived_from_policy'].update(delivery_approval=copy.deepcopy(delivery_approval),
            delivery_approval_sha256=proof_sha)
    if native_audio_off_approval is not None:
        scope['derived_from_policy']['native_audio_off_approval'] = copy.deepcopy(native_audio_off_approval)
    if policy.get('planning_mode') == 'shot_local_v1':
        scope['planning_mode'] = 'shot_local_v1'
    if provider == 'openart_mcp':
        if credit_authorization_sha256 is not None:
            raise AutonomyError('MCP exact-credit ceiling is not qualified')
        if unknown_cost_authorization_sha256 is not None:
            scope['unknown_cost_authorization_sha256'] = unknown_cost_authorization_sha256
    if provider == 'openart_cli':
        spec = next(p for p in policy['providers'] if p['id'] == 'openart_cli')
        if _is_unknown(spec):
            if credit_authorization_sha256 is not None:
                raise AutonomyError('unknown-cost policy scope cannot carry exact credit authority')
            if unknown_cost_authorization_sha256 is not None:
                scope['unknown_cost_authorization_sha256'] = unknown_cost_authorization_sha256
            return scope
        if unknown_cost_authorization_sha256 is not None:
            raise AutonomyError('priced policy scope cannot carry unknown-cost authority')
        if credit_authorization_sha256 is not None:
            scope['credit_authorization_sha256'] = credit_authorization_sha256
        scope['credit_terms'] = {'allowance_id': openart_allowance_id(sha), 'allowance': spec['ceiling'],
                                 'account_id_sha256': spec['account_id_sha256'], 'workspace': spec['workspace']}
    return scope


def _prepare_mcp_policy_native(root, policy, shot_id, inputs, *, _retained_input_assets=None):
    """Qualified connector evidence is separate from CLI account and preview proof."""
    from lib import openart_mcp as mcp, production_execution as execution
    spec = _provider_spec(policy, 'openart_mcp')
    if not _mcp_billing_supported(spec) or not _openart_route_approved(spec, inputs.get('model'), inputs.get('mode')):
        raise AutonomyError('exact MCP model/mode is outside freshly approved policy')
    if not _intent_allows(policy, shot_id, 'openart_mcp', inputs.get('model')):
        raise AutonomyError('MCP route violates retained model selection intent')
    profile = mcp.load_profile(inputs.get('model'), inputs.get('mode'), require='supported')
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile,
        **({'_retained_input_assets': _retained_input_assets} if _retained_input_assets is not None else {}))
    # The builder validates this fresh request; retained native requests are
    # rebuilt and compared at their later preparation/dispatch boundaries.
    _validate_mcp_policy_binding(spec, native, profile)
    if _is_mcp_quoted(spec):
        _mcp_quoted_budget_terms(root, spec, native)
    return profile, native


def _validate_mcp_policy_binding(spec, native, profile):
    if native.get('provider') != 'openart_mcp':
        raise AutonomyError('MCP policy cannot authorize another transport')
    uid = (native.get('account_binding') or {}).get('uid_sha256')
    body = native.get('body')
    if not isinstance(body, dict):
        raise AutonomyError('MCP native body absent')
    if (uid != spec['uid_sha256'] or profile.get('account_uid_sha256') != uid
            or body.get('projectId') != spec['project_id']):
        raise AutonomyError('MCP immutable account/project differs from fresh policy')
    if not _openart_route_approved(spec, body.get('model'), body.get('mode')):
        raise AutonomyError('MCP native exact model/mode outside fresh policy')


def _mcp_quoted_budget_terms(root, spec, native):
    """Validate a host-recorded exact cost reply; never promise a debit ceiling.

    A single undiscounted quote is conservative for the connector's optional
    plan discount. The existing policy and original-attempt journal own consent
    and consumption; this is admission evidence, not a second credit ledger.
    """
    path = _inside(root, spec['cost_observation_path'])
    if path.is_symlink() or not path.is_file():
        raise AutonomyError('exact MCP cost observation unavailable')
    raw = path.read_bytes()
    try:
        quote = json.loads(raw)
        observed = datetime.fromisoformat(quote['observed_at'].replace('Z', '+00:00'))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AutonomyError('invalid MCP cost observation') from None
    body = native['body']
    expected = {'model': body['model'], 'mode': body['mode'], 'params': body['params']}
    cost_controls = {'duration', 'resolution', 'aspectRatio', 'videoCount', 'generateAudio'} & body['params'].keys()
    expected_cost_arguments = {'model': body['model'], 'mode': body['mode'],
        'params': {key: body['params'][key] for key in cost_controls}}
    if (quote.get('version') != '1' or quote.get('transport') != 'openart_mcp'
            or quote.get('classification') != 'agent_recorded_connector_quote_not_backend_ceiling'
            or quote.get('tool') != 'openart_model_cost' or observed.tzinfo is None
            or digest(quote.get('account_binding')) != digest(native['account_binding'])
            or digest(quote.get('native_request_binding')) != digest(expected)
            or digest(quote.get('arguments')) != digest(expected_cost_arguments)):
        raise AutonomyError('MCP cost observation differs from exact native request')
    data = quote.get('data', {})
    if not isinstance(data, dict):
        raise AutonomyError('invalid MCP quote data')
    rows = data.get('items', [])
    if data.get('currency') != 'credits' or not isinstance(rows, list) or len(rows) != 1:
        raise AutonomyError('MCP quote requires one exact credit-priced video item')
    row = rows[0]
    if (not isinstance(row, dict) or row.get('model') != body['model']
            or row.get('mode') != body['mode'] or row.get('mediaType') != 'video'
            or row.get('affordable') is not True or type(row.get('quantity')) is not int
            or row['quantity'] != 1 or body['params'].get('videoCount', 1) != 1):
        raise AutonomyError('MCP quote item does not cover one affordable original video')
    config = row.get('config')
    if (not isinstance(config, dict) or not {'duration', 'resolution'} <= cost_controls
            or not cost_controls <= config.keys()
            or any(key not in body['params'] or digest(value) != digest(body['params'][key])
                   for key, value in config.items())):
        raise AutonomyError('MCP cost config differs from exact native parameters')
    amount, unit, balance = row.get('totalCredits'), row.get('unitCredits'), data.get('balance')
    if (any(type(value) is not int or value < 0 for value in (amount, unit, balance))
            or amount != unit * row['quantity']):
        raise AutonomyError('MCP quote has unknown or inconsistent credit amounts')
    if amount > balance or amount > spec['max_credit_budget']:
        raise AutonomyError('MCP quoted video cost exceeds approved budget or quoted balance')
    return {'kind': 'quoted_credit_budget', 'max_credit_budget': spec['max_credit_budget'],
        'quoted_credits': amount, 'quote_price_basis': 'undiscounted_conservative',
        'cost_observation': {'path': str(path.relative_to(Path(root).resolve())),
                             'sha256': hashlib.sha256(raw).hexdigest()},
        'cost_observed_at': quote['observed_at'], 'backend_credit_ceiling_guaranteed': False}


def _mcp_policy_terms(root, policy, sha, decision_id, scope, inputs, native):
    """Canonical derived terms; no caller assertion of unknown cost grants authority."""
    spec = _provider_spec(policy, 'openart_mcp')
    if not _mcp_billing_supported(spec):
        raise AutonomyError('MCP policy lacks approved billing terms')
    terms = {'kind': 'unknown_cost', 'provider': 'openart_mcp',
        'project_root': str(Path(root).resolve()), 'project_id': policy['project_id'],
        'story_revision': policy['story_revision'], 'policy_sha256': sha,
        'activation_decision_id': decision_id, 'scope_id': scope['id'],
        'shot_id': inputs['governance']['shot_id'], 'uid_sha256': spec['uid_sha256'],
        'projectId': spec['project_id'], 'model': inputs['model'], 'mode': inputs['mode'],
        'request_sha256': scope['requests'][inputs['governance']['shot_id']],
        'body_sha256': native['body_sha256'], 'source_binding_sha256': native['source_binding_sha256'],
        'profile_sha256': native['profile_sha256'], 'form_sha256': native['form_sha256'],
        'compiled_request_sha256': scope['derived_from_policy']['compiled_request_sha256'],
        'lock_digest': scope['derived_from_policy']['lock_digest'],
        'exposure_acknowledgement': UNKNOWN_ACKNOWLEDGEMENT,
        'enforceable_credit_ceiling': False, 'count': 1, 'purpose': 'generation'}
    if _is_mcp_quoted(spec):
        terms.pop('exposure_acknowledgement')
        terms.update(_mcp_quoted_budget_terms(root, spec, native))
    return terms


def rooted_policy_mcp_authority(root, inputs, scope, native, profile, *, exclude_attempt_id=None):
    """Rooted fresh billing authority. Invoke under the gateway's begin/project lock.

    An exclusion is never accepted from caller JSON. The execution adapter owns
    its private same-original revalidation context; all other attempts count.
    """
    if exclude_attempt_id is not None:
        raise AutonomyError('caller-controlled attempt exclusions are forbidden')
    if scope.get('provider') != 'openart_mcp' or 'derived_from_policy' not in scope:
        raise AutonomyError('MCP policy authority requires its retained derived scope')
    policy, sha, decision_id = validate_derived_scope(root, scope, inputs=inputs)
    current_profile, current_native = _prepare_mcp_policy_native(root, policy, inputs['governance']['shot_id'], inputs)
    if digest(current_native) != digest(native) or digest(current_profile) != digest(profile):
        raise AutonomyError('MCP prepared native/form/account/source evidence drifted')
    terms = _mcp_policy_terms(root, policy, sha, decision_id, scope, inputs, native)
    if scope.get('unknown_cost_authorization_sha256') != digest(terms):
        raise AutonomyError('MCP derived unknown-cost binding differs')
    return {**terms, 'sha256': digest(terms)}


def validate_policy_mcp_attempt(root, scope, *, inputs, native, profile, authority=None, return_authority=False):
    """Reprove a retained connector policy origin without editing its journal."""
    policy, sha, decision_id, material = _scope_material(root, inputs, 'openart_mcp', derived_unknown_id=scope['id'],
                                                         historical_scope=scope)
    expected = _retained_scope_record(policy, sha, decision_id, scope,
        derivation_index=scope['derived_from_policy']['derivation_index'], **material)
    scopes = _json(Path(root) / 'production_scopes.json')['scopes']
    if scope != expected or len([s for s in scopes if s == expected]) != 1:
        raise AutonomyError('historical MCP scope differs from retained rooted policy')
    current_profile, current_native = _prepare_mcp_policy_native(root, policy, inputs['governance']['shot_id'], inputs)
    if digest(current_native) != digest(native) or digest(current_profile) != digest(profile):
        raise AutonomyError('historical MCP native/account/form/source evidence changed')
    checked = _mcp_policy_terms(root, policy, sha, decision_id, scope, inputs, native)
    checked['sha256'] = digest(checked)
    if authority is not None and any(authority.get(key) != value for key, value in checked.items()):
        raise AutonomyError('historical MCP policy authority differs from current rooted origin')
    return checked if return_authority else sha


def _validate_retained_packet_policy_mcp_attempt(root, scope, *, inputs, native, profile,
                                                compiled, review, selected, authority, _original_contract=None):
    """Replay an immutable MCP policy origin after canonical packet provenance.
    The caller proves original compiled/review/selection snapshots; current
    relevant planning, rooted policy, native/account and billing still bind.
    """
    from lib import production_execution as execution
    shot_id = inputs['governance']['shot_id']
    policy, sha, decision_id = _load_historical_policy(root, scope,
        _source_shot_id=shot_id if _original_contract is not None else None)
    if (_original_contract is not None
            and execution.approval_plan_digest(_original_contract,
                inputs['governance']['shot_id'] if scope.get('planning_mode') == 'shot_local_v1' else None) != scope['approval_plan_sha256']):
        raise AutonomyError('historical MCP original planning differs from retained scope')
    current_profile, current_native = _prepare_mcp_policy_native(root, policy, shot_id, inputs,
        _retained_input_assets=native['input_assets'])
    if digest(current_native) != digest(native) or digest(current_profile) != digest(profile):
        raise AutonomyError('historical MCP native/account/form/source evidence changed')
    locks = _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile,
        historical={'compiled': compiled, 'review': review, 'policy': policy, 'policy_sha256': sha},
        retained_lock_digest=scope['derived_from_policy']['lock_digest'])
    material = {'shot_id': shot_id, 'provider': 'openart_mcp',
        'request_digest': execution.planned_request_digest(inputs, project_dir=root),
        'approval_plan_sha256': execution.approval_plan_digest(
            execution.load_shot_contract(root) if _original_contract is None else _original_contract,
            inputs['governance']['shot_id'] if scope.get('planning_mode') == 'shot_local_v1' else None),
        'lock_digest': locks, 'compiled_request_sha256': digest(compiled),
        'preparation_review_id': inputs['preparation_review_id'], 'resolved_upstream': selected}
    delivery = _retained_scope_delivery(root, policy, sha, decision_id, scope, inputs)
    if delivery is not None:
        material['delivery_approval'] = delivery
    audio_off = _native_audio_off_proof(root, policy, sha, shot_id, inputs, 'openart_mcp',
        proof=scope['derived_from_policy'].get('native_audio_off_approval'), historical=True)
    if audio_off is not None:
        material['native_audio_off_approval'] = audio_off
    material['unknown_cost_authorization_sha256'] = scope.get('unknown_cost_authorization_sha256')
    expected = _retained_scope_record(policy, sha, decision_id, scope,
        derivation_index=scope['derived_from_policy']['derivation_index'], **material)
    scopes = _json(Path(root) / 'production_scopes.json')['scopes']
    if scope != expected or len([s for s in scopes if s == expected]) != 1:
        raise AutonomyError('historical MCP source scope differs from retained rooted policy')
    checked = _mcp_policy_terms(root, policy, sha, decision_id, scope, inputs, native)
    if scope.get('unknown_cost_authorization_sha256') != digest(checked):
        raise AutonomyError('historical MCP source unknown-cost binding differs')
    checked['sha256'] = digest(checked)
    if any(authority.get(key) != value for key, value in checked.items()):
        raise AutonomyError('historical MCP policy authority differs from current rooted origin')
    return checked


def _refuse_unknown_cost(inputs, *, derived_unknown_id=None, allow_evidence=False):
    """Caller-supplied unknown-cost authority never grants anything.

    Only the authorization ID that ``derive_scope`` itself captured for the exact
    replayed scope is tolerated, and the evidence ID only once the active policy is
    the separately acknowledged unknown variant.
    """
    unavailable = 'unknown-cost Auto-continue is unavailable; use separate Strict approval'
    if any(key in inputs for key in ('unknown_cost_authorization', 'unknown_cost_authorization_sha256')):
        raise AutonomyError(unavailable)
    if 'unknown_cost_authorization_id' in inputs and (
            derived_unknown_id is None or inputs['unknown_cost_authorization_id'] != derived_unknown_id):
        raise AutonomyError(unavailable)
    if 'unknown_cost_evidence_id' in inputs and not allow_evidence:
        raise AutonomyError('unknown-cost Auto-continue is unavailable; use separate Strict approval')


def _scope_material(root, inputs, provider, observation=None, *, derived_unknown_id=None, historical_scope=None, replay_scope_id=None):
    _refuse_unknown_cost(inputs, derived_unknown_id=derived_unknown_id, allow_evidence=True)
    from lib import production_request as preparation, production_execution as execution
    # Historical replay proves the retained approving policy; only new admission needs an active one.
    policy, sha, decision_id = (load_historical_policy(root, historical_scope) if historical_scope is not None
                                else require_active_policy(root, shot_id=inputs['governance']['shot_id']))
    unknown = _is_unknown(_provider_spec(policy, provider))
    if provider not in {'openart_cli', 'openart_mcp'} and _is_unknown(_openart_spec(policy)) and any(key in inputs for key in ('unknown_cost_evidence_id', 'unknown_cost_authorization_id')):
        raise AutonomyError('unknown-cost authority is OpenArt-only')
    if not unknown:
        _refuse_unknown_cost(inputs)
    if unknown and provider == 'openart_cli':
        if any(key in inputs for key in ('credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256')):
            raise AutonomyError('unknown-cost policy refuses exact-credit fields')
        if not isinstance(inputs.get('unknown_cost_evidence_id'), str):
            raise AutonomyError('unknown-cost policy requires retained unknown_cost_evidence_id')
    elif provider == 'openart_mcp':
        if not _mcp_billing_supported(_provider_spec(policy, provider)):
            raise AutonomyError('MCP requires freshly approved billing terms')
        if _json(Path(root) / 'project.json').get('pipeline_type') == 'provider-qualification':
            raise AutonomyError('MCP qualification requires separate Strict approval')
        if any(key in inputs for key in ('credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256', 'unknown_cost_evidence_id', 'unknown_cost_authorization_id', 'mcp_billing_authorization_id', 'billing_authority')):
            raise AutonomyError('MCP policy refuses caller billing authority and CLI authority fields')
    elif unknown and any(key in inputs for key in ('unknown_cost_evidence_id', 'unknown_cost_authorization_id')):
        raise AutonomyError('unknown-cost authority is OpenArt-only')
    shot_id = inputs['governance']['shot_id']
    if provider not in {p['id'] for p in policy['providers']}:
        raise AutonomyError('route provider outside approved policy')
    origin = historical_scope
    if origin is None and replay_scope_id is not None:
        matches = [s for s in _json(Path(root) / 'production_scopes.json')['scopes'] if s['id'] == replay_scope_id]
        if len(matches) != 1:
            raise AutonomyError('delivery replay scope must be retained exactly once')
        origin = matches[0]
    if origin is not None:
        _retained_scope_delivery(root, policy, sha, decision_id, origin, inputs)
    audio_off = _native_audio_off_proof(root, policy, sha, shot_id, inputs, provider,
        proof=(origin or {}).get('derived_from_policy', {}).get('native_audio_off_approval'),
        historical=historical_scope is not None)
    delivery = _validate_request_delta(root, policy, shot_id, inputs, provider,
        policy_context=(sha, decision_id), delivery_approval=(origin or {}).get('derived_from_policy', {}).get('delivery_approval'),
        native_audio_off_approval=audio_off, historical=historical_scope is not None)
    if historical_scope is None:
        _validate_standing_transfer(root, policy, inputs, provider, exclude_scope_id=replay_scope_id)
    if provider == 'openart_cli':
        from lib import openart_jobs as jobs
        from tools.tool_registry import _openart_route
        from tools.video.openart_cli_video import OpenArtCLIVideo
        menu = _openart_route(OpenArtCLIVideo())
        eligible, _ = eligible_routes(policy, shot_id, [menu])
        if not any(r['model'] == inputs.get('model') and r['mode'] == inputs.get('mode') for r in eligible):
            raise AutonomyError('exact OpenArt route is not production-ready (native form, preview and account)')
        profile = jobs.load_qualification(model=inputs.get('model'), mode=inputs.get('mode'), require='production_ready')
        native = jobs.prepare_native_request(execution._openart_controls(inputs), profile)
        if unknown:
            jobs.native_reference_digest(inputs, profile, native=native)
    elif provider == 'openart_mcp':
        profile, native = _prepare_mcp_policy_native(root, policy, shot_id, inputs)
    elif provider == 'grok_cli':
        if 'model' in policy['locked']['controls']:
            raise AutonomyError('Grok media model is unreported and cannot meet exact lock')
        if observation is None:
            raise AutonomyError('fresh out-of-lock Grok CLI observation required')
        native = preparation.prepare_grok_native(inputs, observation)
        profile = {'source': 'real'}
    else:
        raise AutonomyError('route outside subscription CLI policy')
    if historical_scope is None:
        # The root proof validates this exact preparation; consume its result
        # within this invocation instead of rebuilding the same native twice.
        locks, proof = _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile,
            _return_preparation=True)
    else:
        proof = _historical_current_preparation(root, inputs, native, profile, True)
        locks = _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile,
            policy_context=(policy, sha), retained_lock_digest=historical_scope['derived_from_policy']['lock_digest'],
            historical_replay=True)
    contract = execution.load_shot_contract(root)
    own_shot = next(s for s in contract['shots'] if s['id'] == shot_id)
    selected = execution.load_selected_attempts(root)
    from lib.shot_contract import source_upstream_ids
    producers = source_upstream_ids(contract, own_shot, root)
    resolved = {producer: copy.deepcopy(selected[producer]) for producer in producers}
    material = {'shot_id': shot_id, 'provider': provider,
        'request_digest': execution.planned_request_digest(inputs, project_dir=root),
        'approval_plan_sha256': execution.approval_plan_digest(execution.load_shot_contract(root),
            shot_id if policy.get('planning_mode') == 'shot_local_v1' else None),
        'lock_digest': locks, 'compiled_request_sha256': proof['compiled_sha256'],
        'preparation_review_id': inputs['preparation_review_id'],
        'resolved_upstream': resolved}
    if delivery is not None:
        material['delivery_approval'] = delivery
    if audio_off is not None:
        material['native_audio_off_approval'] = audio_off
    if provider == 'openart_mcp' and derived_unknown_id is not None:
        fake_scope = {'id': derived_unknown_id, 'requests': {shot_id: material['request_digest']}, 'derived_from_policy': {'compiled_request_sha256': material['compiled_request_sha256'], 'lock_digest': material['lock_digest']}}
        material['unknown_cost_authorization_sha256'] = digest(_mcp_policy_terms(root, policy, sha, decision_id, fake_scope, inputs, native))
    elif provider == 'openart_cli' and unknown and inputs.get('unknown_cost_authorization_id'):
        from lib import openart_dispatch as dispatch, openart_credit as credit
        authorization = dispatch._authorization(Path(root), inputs)
        material['unknown_cost_authorization_sha256'] = credit.unknown_cost_authorization_digest(authorization)
    elif provider == 'openart_cli' and inputs.get('credit_authorization_id'):
        from lib import openart_dispatch as dispatch, openart_credit as credit
        authorization = dispatch._authorization(Path(root), inputs)
        material['credit_authorization_sha256'] = credit.credit_authorization_digest(authorization)
    return policy, sha, decision_id, material


def validate_derived_scope(root, scope, *, inputs, observation=None):
    """Rooted dispatch replay; caller recompute dictionaries never grant authority."""
    if 'derived_from_policy' not in scope:
        return None
    derived = scope['derived_from_policy']
    if not isinstance(derived, dict):
        raise AutonomyError('malformed derived_from_policy')
    policy, sha, decision_id, material = _scope_material(root, inputs, scope.get('provider'), observation,
                                                         derived_unknown_id=scope.get('id'), replay_scope_id=scope.get('id'))
    if derived.get('policy_sha256') != sha or derived.get('decision_id') != decision_id:
        raise AutonomyError('derived scope cites inactive policy')
    _require_route_decision(root, sha, decision_id, material['shot_id'], scope['provider'])
    from lib.production_execution import _read
    scopes = _read(Path(root) / 'production_scopes.json')['scopes']
    policy_scopes = [s for s in scopes if 'derived_from_policy' in s]
    indexes = [s['derived_from_policy'].get('derivation_index') if isinstance(s['derived_from_policy'], dict) else None for s in policy_scopes]
    if indexes != list(range(len(policy_scopes))):
        raise AutonomyError('policy scope derivation sequence is inconsistent')
    if not any(s == scope for s in policy_scopes):
        raise AutonomyError('derived scope is not retained exactly once')
    index = policy_scopes.index(scope)
    phase = scope.get('phase')
    basis = scope.get('repair_basis', 'critical_review')
    if phase != 'first_pass' or scope.get('replaces_attempt_ids') or basis != 'critical_review':
        raise AutonomyError('repair scopes are retired; each planned clip has one original request')
    counts = root_attempt_counts(root, policy, exclude_scope_id=scope['id'])
    check_caps(policy, material['shot_id'], counts, phase, root=root)
    expected = _scope_record(policy, sha, decision_id, derivation_index=index, **material)
    if expected != scope:
        raise AutonomyError('derived scope differs from authoritative current replay')
    if scope['provider'] == 'openart_cli':
        if _is_unknown(_openart_spec(policy)):
            _validate_policy_unknown(root, policy, sha, scope, inputs)
        else:
            _validate_policy_credit(root, policy, sha, scope, inputs)
    return policy, sha, decision_id


def _controls_admission(root, shot_id, provider, inputs):
    from lib import episode_production_controls as episode_controls
    if episode_controls.effective_controls(root) is None:
        return
    try:
        episode_controls.require_admission(root, shot_id=shot_id, provider=provider, model=inputs.get('model'),
                                           purpose='first_pass')
    except episode_controls.EpisodeControlsError as exc:
        raise AutonomyError(str(exc))


def derive_scope(root, inputs, *, provider, observation=None, reuse_undispatched=False):
    """Append the one exact original scope for a planned clip under the project lock.

    Each planned clip receives exactly one generation request; an existing
    counted original (known or unknown) is never resubmitted or replaced.
    """
    phase = 'first_pass'

    from lib import production_execution as execution
    root = Path(root).resolve()
    with execution._lock(root):
        policy, sha, decision_id, material = _scope_material(root, inputs, provider, observation)
        shot_id = material['shot_id']
        blockers = cross_provider_block(root, shot_id)
        if blockers:
            raise AutonomyError('; '.join(blockers))
        path = root / 'production_scopes.json'
        data = _json(path) if path.exists() else {'version': '1.0', 'scopes': []}
        if data.get('version') != '1.0':
            raise AutonomyError('invalid production scopes version')
        scopes = data['scopes']
        used_scopes = {a['scope_id'] for a in execution._attempts(root)}
        pending = [s for s in scopes if s['id'] not in used_scopes and shot_id in s.get('requests', {})
                   and 'derived_from_policy' in s and s.get('phase') == 'first_pass']
        if pending and reuse_undispatched:
            if (len(pending) != 1 or pending[0]['provider'] != provider or pending[0]['phase'] != phase
                    or pending[0]['requests'][shot_id] != material['request_digest']):
                raise AutonomyError('undispatched scope differs from exact original request/route/phase')
            validate_derived_scope(root, pending[0], inputs=inputs, observation=observation)
            return copy.deepcopy(pending[0])
        if pending:
            raise AutonomyError('an undispatched derived scope already exists for this shot')
        if any(a['shot_id'] == shot_id and production_kind(a) == 'motion' for a in execution._attempts(root)):
            raise AutonomyError('episode generation limit reached: shot already has its one original request')
        _controls_admission(root, shot_id, provider, inputs)
        check_caps(policy, shot_id, root_attempt_counts(root, policy), phase, root=root)
        index = sum('derived_from_policy' in s for s in scopes)
        _append_route_decisions_locked(root, policy, sha, decision_id, shot_id, provider, inputs)
        material.pop('credit_authorization_sha256', None)
        material.pop('unknown_cost_authorization_sha256', None)
        scope = _scope_record(policy, sha, decision_id, derivation_index=index, **material)
        if provider == 'openart_mcp':
            profile, native = _prepare_mcp_policy_native(root, policy, shot_id, inputs)
            scope['unknown_cost_authorization_sha256'] = digest(_mcp_policy_terms(root, policy, sha, decision_id, scope, inputs, native))
        elif provider == 'openart_cli' and _is_unknown(_openart_spec(policy)):
            scope['unknown_cost_authorization_sha256'] = _capture_policy_unknown_cost(root, policy, sha, scope, inputs)
        elif provider == 'openart_cli':
            authorization = _capture_policy_credit(root, policy, sha, scope, inputs)
            scope['credit_authorization_sha256'] = authorization
        if any(s['id'] == scope['id'] for s in scopes):
            raise AutonomyError('derived scope identity already exists')
        data['scopes'].append(scope)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
        return scope


# ---------------------------------------------------------------- checkpoint preauth

def validate_preauth(root, stage, approval_basis, preauth):
    """Pure check of a policy checkpoint preauth; raises on any defect, writes nothing.

    The review is re-read from current bytes; its own ``critical_findings`` must be
    ``[]``. A caller-supplied list is only cross-checked.
    """
    policy, sha, decision_id = require_active_policy(root)
    return _validate_preauth_bytes(root, stage, approval_basis, preauth, policy, sha, decision_id)


def _validate_preauth_bytes(root, stage, approval_basis, preauth, policy, sha, decision_id,
                            *, artifact_bytes=None, review_bytes=None):
    """Shared byte proof under an already authenticated current/original policy."""
    from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS
    if approval_basis != {'kind': 'policy', 'policy_sha256': sha, 'decision_id': decision_id}:
        raise AutonomyError('approval_basis does not cite active policy')
    if stage in NON_PREAUTH_STAGES or stage not in policy['checkpoint_stages']:
        raise AutonomyError(f'stage {stage} not preauthorized')
    if preauth.get('artifact_path') != f'artifacts/{CANONICAL_STAGE_ARTIFACTS[stage]}.json':
        raise AutonomyError('preauth artifact is not the canonical stage artifact')
    artifact_path = _inside(root, preauth['artifact_path'])
    current = hashlib.sha256(artifact_path.read_bytes() if artifact_bytes is None else artifact_bytes).hexdigest()
    if current != preauth.get('artifact_sha256'):
        raise AutonomyError('artifact bytes changed since review')
    review_path = _inside(root, preauth['review_path'])
    if review_bytes is None:
        review_bytes = review_path.read_bytes()
    if hashlib.sha256(review_bytes).hexdigest() != preauth.get('review_sha256'):
        raise AutonomyError('review bytes changed')
    review = json.loads(review_bytes)
    if review.get('stage') != stage or review.get('artifact_sha256') != current:
        raise AutonomyError('review does not bind current artifact')
    if review.get('critical_findings') != [] or preauth.get('critical_findings', []) != []:
        raise AutonomyError('review has critical findings or none recorded')
    return sha


# ---------------------------------------------------------------- writers

def append_decisions(root, sha, decision_id, entries):
    """Append schema-valid decisions citing the current retained activation."""
    from lib.production_execution import _artifact_path, _lock
    from uuid import uuid4
    root = Path(root)
    with _lock(root):
        _, actual_sha, actual_id = require_active_policy(root)
        if (sha, decision_id) != (actual_sha, actual_id):
            raise AutonomyError('decision cites inactive policy')
        if (root / 'decision_log.json').exists() and (root / 'artifacts/decision_log.json').exists():
            raise AutonomyError('dual decision log aliases cannot be appended')
        path = _artifact_path(root, 'decision_log.json')
        data = _json(path)
        existing = {e['decision_id'] for e in _decision_entries(root)}
        for e in entries:
            entry = copy.deepcopy(e)
            entry.setdefault('decision_id', 'auto-' + uuid4().hex)
            entry.setdefault('stage', 'assets')
            entry.setdefault('options_considered', [{'option_id': entry['selected'],
                'label': entry['selected'], 'score': 1, 'reason': entry.get('reason', '')}])
            if entry['decision_id'] in existing:
                raise AutonomyError('duplicate decision ID')
            existing.add(entry['decision_id'])
            entry['user_approved'] = False
            entry['reason'] = f'{entry.get("reason", "")} [policy:{sha} activation:{decision_id}]'.strip()
            data['decisions'].append(entry)
        Draft202012Validator(load_schema('decision_log')).validate(data)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
        return data


def completion_report(root, sha):
    """Report actual journals, private ledger and current eligibility; no caller totals."""
    from lib.production_autonomy_report import completion_report as build_report
    return build_report(root, sha)


# ---------------------------------------------------------------- R18 (delegated)

def cross_provider_block(project_dir, shot_id):
    """Thin wrapper over the owned R18 reader; no competing implementation here.

    Returns fail-closed reasons why another video for ``shot_id`` blocks dispatch on
    any provider. Empty list means no duplicate-video block.
    """
    from lib.production_video_guard import read_video_duplicate_blocks
    return list(read_video_duplicate_blocks(project_dir, shot_id))


def production_kind(request):
    """Explicit persisted media kind only (delegated); ``None`` must fail closed."""
    from lib.production_video_guard import classify_production_kind
    return classify_production_kind(request)


def root_attempt_counts(root, policy, *, exclude_scope_id=None):
    """Count journals, private original reservations and undispached policy scopes once."""
    from lib import production_execution as execution, openart_dispatch as dispatch
    from lib.provider_credit_ledger import Binding, UnpricedBinding, read_existing_snapshot
    root = Path(root).resolve()
    attempts = execution._attempts(root)
    path = root / 'production_scopes.json'
    scopes = _json(path).get('scopes', []) if path.exists() else []
    ids = [scope['id'] for scope in scopes]
    if len(ids) != len(set(ids)):
        raise AutonomyError('duplicate scope IDs')
    records = {}
    def add(request, selected=None):
        from lib.production_request import public_scope
        aid = request['attempt_id']
        shot = request['shot_id']
        phase = request.get('phase')
        embedded = request.get('scope')
        matches = [scope for scope in scopes if scope['id'] == request.get('scope_id')]
        if (phase not in {'first_pass', 'repair'} or not isinstance(embedded, dict) or
                embedded.get('phase') != phase or len(matches) != 1 or matches[0].get('phase') != phase):
            raise AutonomyError('attempt phase differs from exactly one retained scope')
        retained = matches[0]
        if embedded != (public_scope(retained) if retained.get('provider') == 'openart_cli' else retained):
            raise AutonomyError('attempt embedded scope differs from retained scope identity')
        approved = retained.get('requests', {}).get(shot)
        allowed = approved if isinstance(approved, list) else [approved]
        if selected is None:
            selected_path = root / 'production_attempts' / aid / 'selected_attempts.json'
            selected = _json(selected_path) if selected_path.exists() else {}
        if not any(execution.approved_request_digest(entry, project_dir=root, selected_attempts=selected) == request['request_sha256']
                   for entry in allowed):
            raise AutonomyError('attempt shot/request identity differs from retained scope')
        value = (shot, phase, request['scope_id'], request['request_sha256'])
        if aid in records and records[aid] != value:
            raise AutonomyError('inconsistent journal/private attempt identity')
        records[aid] = value
    for request in attempts:
        if request.get('provider') == 'openart_mcp':
            from lib import openart_mcp_jobs as mcp_jobs
            frozen = mcp_jobs.frozen_request(root, request['attempt_id'])
            authority = frozen['authority']
            original_scope = authority['scope']
            matching = [s for s in scopes if s['id'] == authority['scope_id']]
            if len(matching) != 1 or matching[0] != original_scope or digest(original_scope) != authority['scope_sha256']:
                raise AutonomyError('MCP private original scope identity differs')
            if request.get('shot_id') != authority['shot_id'] or request.get('scope_id') != authority['scope_id']:
                raise AutonomyError('MCP private original shot/scope identity differs')
            if authority['shot_id'] in policy['shots']:
                aid, shot = request['attempt_id'], authority['shot_id']
                request_sha = authority['request_sha256']
                if original_scope['requests'].get(shot) != request_sha:
                    raise AutonomyError('MCP original request differs from exact scope')
                if aid in records:
                    raise AutonomyError('duplicate cross-provider original attempt identity')
                records[aid] = (shot, original_scope['phase'], original_scope['id'], request_sha)
            continue
        if request.get('shot_id') in policy['shots'] and production_kind(request) is None:
            raise AutonomyError('policy-shot journal has unknown production kind')
        if production_kind(request) == 'motion' and request['shot_id'] in policy['shots']:
            add(request)
    snapshot = read_existing_snapshot()
    private_ids = set()
    rows = [(Binding, row) for row in snapshot['reservations']]
    rows += [(UnpricedBinding, row) for row in snapshot.get('unpriced_reservations', [])]
    for kind, row in rows:
        binding = kind(**json.loads(row['binding_json']))
        binding.validate()
        if Path(binding.project_root).resolve() != root:
            continue
        manifest, original, _ = dispatch._manifest(row['attempt_id'])
        if binding != original or binding.attempt_id != row['attempt_id']:
            raise AutonomyError('private reservation identity differs from retained origin')
        request = manifest['journal_records']['request.json']
        if request['attempt_id'] != binding.attempt_id or request['request_sha256'] != binding.request_sha256:
            raise AutonomyError('private request identity differs')
        if production_kind(request) != 'motion':
            raise AutonomyError('private reservation has unclassifiable motion kind')
        private_ids.add(binding.attempt_id)
        if request['shot_id'] in policy['shots']:
            add(request, manifest['journal_records'].get('selected_attempts.json', {}))
    reserved = {r['attempt_id'] for _, r in rows}
    if any(row['attempt_id'] not in private_ids
           for row in list(snapshot['outbox']) + list(snapshot.get('unpriced_outbox', []))
           if row['attempt_id'] not in reserved):
        raise AutonomyError('private outbox origin is missing')
    if exclude_scope_id is not None:
        # Revalidating one retained scope never counts its own original reservation.
        records = {aid: value for aid, value in records.items() if value[2] != exclude_scope_id}
    used = {value[2] for value in records.values()}
    open_shots = set()
    for scope in scopes:
        if 'derived_from_policy' not in scope or scope['id'] in used or scope['id'] == exclude_scope_id:
            continue
        if not isinstance(scope['derived_from_policy'], dict):
            raise AutonomyError('malformed open policy scope')
        if scope.get('phase') not in {'first_pass', 'repair'}:
            raise AutonomyError('open policy scope has invalid phase')
        shot_ids = set(scope['requests'])
        if len(shot_ids) != 1 or scope['attempts_per_shot'] != {next(iter(shot_ids)): 1}:
            raise AutonomyError('open policy scope is not exact singleton')
        shot = next(iter(shot_ids))
        if shot in open_shots:
            raise AutonomyError('multiple undispatched scopes for same shot')
        open_shots.add(shot)
        records['scope:' + scope['id']] = (shot, scope['phase'], scope['id'], scope['requests'][shot])
    from lib import episode_production_controls as episode_controls
    if episode_controls.effective_controls(root) is not None:
        # Opted-in episodes: canonical never-submitted preparations cost no slot.
        excluded = set(episode_controls.generation_usage(root)['excluded_never_submitted'])
        records = {aid: value for aid, value in records.items() if aid not in excluded}
    return attempt_counts([{'attempt_id': aid, 'shot_id': value[0], 'phase': value[1]}
                           for aid, value in records.items()])


def validate_policy_attempt(root, request, scope, frozen_contract, *, frozen_openart=None):
    """Reconcile immutable derived authority with current own-shot planning.

    Historical global timing may differ only while the active policy's complete
    retained planning replay proves those changes. Own-shot semantics stay exact.
    """
    from lib import production_request as preparation, production_execution as execution
    root = Path(root).resolve()
    if 'derived_from_policy' not in scope:
        return None
    derived = scope['derived_from_policy']
    if not isinstance(derived, dict):
        raise AutonomyError('malformed historical derived policy scope')
    policy, sha, decision_id = load_historical_policy(root, scope)
    shot_id = request['shot_id']
    if derived.get('policy_sha256') != sha or derived.get('decision_id') != decision_id:
        raise AutonomyError('historical attempt policy differs')
    if scope['approved_by'] != f'policy:{sha}' or scope['evidence'] != policy['evidence']:
        raise AutonomyError('historical policy approval authority differs')
    if scope['requests'] != {shot_id: request['request_sha256']} or scope['attempts_per_shot'] != {shot_id: 1}:
        raise AutonomyError('historical policy scope is not exact singleton')
    if scope['approval_plan_sha256'] != execution.approval_plan_digest(frozen_contract,
            shot_id if scope.get('planning_mode') == 'shot_local_v1' else None):
        raise AutonomyError('historical frozen planning differs')
    directory = root / 'production_attempts' / request['attempt_id']
    if scope['provider'] == 'openart_cli':
        if frozen_openart is None:
            raise AutonomyError('immutable OpenArt native snapshot required')
        preparation.validate_frozen_preparation_history(request, frozen_openart, root)
        from tools import _openart_cli as cli
        payload = _json(cli.state_dir() / 'preparation' / request['attempt_id'] / 'review.json')
        submitted = copy.deepcopy(frozen_openart['inputs'])
        native, profile = frozen_openart['native'], frozen_openart['profile']
    elif scope['provider'] == 'grok_cli':
        proof = request['autonomy_preparation']
        path = _inside(root, proof['path'])
        if path != directory / 'autonomy_preparation.json' or hashlib.sha256(path.read_bytes()).hexdigest() != proof['sha256']:
            raise AutonomyError('historical Grok preparation bytes changed')
        payload = _json(path)
        if set(payload) != {'compiled', 'review', 'source_packet', 'native', 'projection'}:
            raise AutonomyError('incomplete historical Grok preparation')
        submitted = copy.deepcopy(request['submitted_inputs'])
        native, profile = payload['native'], {'source': 'real'}
    else:
        raise AutonomyError('historical provider is outside policy')
    records = iter(request['input_assets'])
    def restore(role, path):
        item = next(records)
        if item['role'] != role or str(path) != item['path'] or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise AutonomyError('historical submitted input bytes differ')
        return item['original_path']
    restored = execution._paths(submitted, root, restore)
    if next(records, None) is not None:
        raise AutonomyError('historical extra submitted inputs')
    restored.pop('cli_session_id', None)
    restored['project_dir'] = str(root)
    restored['governance'] = {'scope_id': scope['id'], 'shot_id': shot_id}
    restored['compiled_request_id'] = payload['compiled'].get('compiled_request_id', request['scope']['derived_from_policy'].get('compiled_request_id', 'historical'))
    restored['preparation_review_id'] = derived['preparation_review_id']
    try:
        preparation.validate_preparation_evidence(payload['compiled'], payload['review'], restored, native, profile,
                                                  payload['source_packet'], historical=True)
    except ValueError as exc:
        raise AutonomyError(f'historical named preparation is incomplete: {exc}') from exc
    review = payload['review']
    current = current_projection(root, shot_id)
    frozen_shot = next(s for s in frozen_contract['shots'] if s['id'] == shot_id)
    own_shot = copy.deepcopy(frozen_shot)
    own_shot.pop('review', None)
    own_shot['upstream'] = [{k: v for k, v in u.items() if k not in {'attempt_id', 'output_sha256', 'outgoing_frame_sha256', 'review_sha256'}} for u in own_shot.get('upstream', [])]
    if own_shot != current['shot']:
        raise AutonomyError('historical own-shot semantics changed')
    locks = _root_lock_proof(root, shot_id, inputs=restored, native=native, profile=profile,
        historical={**payload, 'policy': policy, 'policy_sha256': sha}, retained_lock_digest=derived['lock_digest'])
    if locks != derived['lock_digest'] or digest(payload['compiled']) != derived['compiled_request_sha256']:
        raise AutonomyError('historical actual lock/compilation proof differs')
    selected = _json(directory / 'selected_attempts.json')
    from lib.shot_contract import source_upstream_ids, _standing_make_video
    producers = source_upstream_ids(frozen_contract, frozen_shot, root,
                                    factory=_standing_make_video(root, policy))
    material = {'shot_id': shot_id, 'provider': scope['provider'], 'request_digest': request['request_sha256'],
        'approval_plan_sha256': scope['approval_plan_sha256'], 'lock_digest': locks,
        'compiled_request_sha256': digest(payload['compiled']), 'preparation_review_id': review['review_id'],
        'resolved_upstream': {producer: selected[producer] for producer in producers}}
    delivery = _retained_scope_delivery(root, policy, sha, decision_id, scope, restored)
    if delivery is not None:
        material['delivery_approval'] = delivery
    if scope['provider'] == 'openart_cli':
        from lib import openart_credit as credit, openart_dispatch as dispatch
        manifest, binding, _ = dispatch._manifest(request['attempt_id'])
        unknown_manifest = manifest.get('authorization_kind') == 'unknown_cost'
        if unknown_manifest != _is_unknown(_openart_spec(policy)):
            raise AutonomyError('historical billing mode differs from active policy')
        key = 'unknown_cost_authorization_sha256' if unknown_manifest else 'credit_authorization_sha256'
        fn = credit.unknown_cost_authorization_digest if unknown_manifest else credit.credit_authorization_digest
        material[key] = fn(manifest['authorization'])
        if binding.authorization_sha256 != material[key]:
            raise AutonomyError('historical private credit authorization differs')
    expected = _retained_scope_record(policy, sha, decision_id, scope,
        derivation_index=derived['derivation_index'], **material)
    scopes = _json(root / 'production_scopes.json')['scopes']
    if scope != expected or len([s for s in scopes if s == expected]) != 1:
        raise AutonomyError('historical derived scope differs from rooted authority')
    return sha


def _capture_policy_credit(root, policy, sha, scope, inputs):
    """Emit ordinary U4 authorization/evidence for exactly one policy occurrence."""
    from lib import openart_credit as credit, openart_jobs as jobs, production_execution as execution
    spec = next(p for p in policy['providers'] if p['id'] == 'openart_cli')
    profile = jobs.load_qualification(model=inputs.get('model'), mode=inputs.get('mode'), require='production_ready')
    native = jobs.prepare_native_request(execution._openart_controls(inputs), profile)
    quote = credit.get_retained_quote(inputs, profile, inputs.get('credit_quote_id'))
    shot = next(iter(scope['requests']))
    ident = scope['id']
    terms = {'version': '1', 'status': 'approved', 'approved_by': f'policy:{sha}',
        'project_root': str(Path(root).resolve()), 'project_id': policy['project_id'],
        'story_revision': policy['story_revision'], 'scope_id': scope['id'], 'shot_id': shot,
        'account_id_sha256': spec['account_id_sha256'], 'workspace': spec['workspace'],
        'allowance_id': openart_allowance_id(sha), 'allowance': spec['ceiling'], 'ceiling': spec['ceiling'],
        'count': 1, 'purpose': 'generation', 'occurrences': [{'id': ident + '-0', 'index': 0,
        'request_sha256': scope['requests'][shot], 'native_sha256': native['native_body_sha256'],
        'profile_sha256': native['profile_sha256'], 'quote_sha256': quote['quote_sha256']}]}
    content = {'kind': 'openart_credit_authorization', 'terms': terms}
    raw = json.dumps(content, sort_keys=True, separators=(',', ':')).encode()
    rel = f'approvals/autonomy-credit-{ident}.json'
    authorization = {**terms, 'evidence': {'path': rel, 'sha256': hashlib.sha256(raw).hexdigest()}}
    Draft202012Validator(load_schema('credit_authorization')).validate(authorization)
    evidence_path = _inside(root, rel)
    auth_path = Path(root) / 'artifacts' / f'credit_authorization-{ident}.json'
    if evidence_path.exists() or auth_path.exists():
        raise AutonomyError('policy credit capture already exists')
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(raw)
    auth_path.write_text(json.dumps(authorization, indent=2))
    inputs['credit_authorization_id'] = ident
    inputs['governance']['scope_id'] = scope['id']
    return credit.credit_authorization_digest(authorization)


def _validate_policy_credit(root, policy, sha, scope, inputs):
    if _is_unknown(_openart_spec(policy)):
        raise AutonomyError('unknown-cost policy has no exact-credit allowance')
    return _validate_policy_credit_priced(root, policy, sha, scope, inputs)


_UNKNOWN_PROBE_ATTEMPT = '00000000-0000-4000-8000-000000000000'


def _capture_policy_unknown_cost(root, policy, sha, scope, inputs):
    """Emit one rooted policy-derived unknown-cost authorization; never human evidence.

    ``approved_by`` names the active policy digest. The retained capture is the
    exact canonical unknown-cost capture shape so ordinary dispatch/ledger
    validators bind it unchanged; no amount, quote, allowance or ceiling exists.
    """
    from lib import openart_credit as credit, openart_jobs as jobs, production_execution as execution
    spec = _openart_spec(policy)
    model, mode = inputs.get('model'), inputs.get('mode')
    if not _openart_route_approved(spec, model, mode):
        raise AutonomyError('unknown-cost route outside exact approved policy routes')
    profile = jobs.load_qualification(model=model, mode=mode, require='production_ready')
    native = jobs.prepare_native_request(execution._openart_controls(inputs), profile)
    references_sha256 = jobs.native_reference_digest(inputs, profile, native=native)
    evidence = credit.get_retained_unknown_evidence(inputs, profile, inputs['unknown_cost_evidence_id'])
    if (evidence['account_id_sha256'] != spec['account_id_sha256'] or
            profile.get('account_id_sha256') != spec['account_id_sha256']):
        raise AutonomyError('unknown-cost evidence/profile account differs from policy binding')
    if evidence['workspace'] != credit.UNOBSERVED_WORKSPACE or spec['workspace'] != credit.UNOBSERVED_WORKSPACE:
        raise AutonomyError('unknown-cost workspace must be explicitly unobserved')
    shot = next(iter(scope['requests']))
    ident = scope['id']
    terms = {'version': '1', 'kind': 'openart_unknown_cost', 'status': 'approved',
        'approved_by': f'policy:{sha}', 'exposure_acknowledgement': UNKNOWN_ACKNOWLEDGEMENT,
        'provider': 'openart_cli', 'project_root': str(Path(root).resolve()),
        'project_id': policy['project_id'], 'story_revision': policy['story_revision'],
        'scope_id': ident, 'shot_id': shot, 'account_id_sha256': spec['account_id_sha256'],
        'workspace': spec['workspace'], 'workspace_observed': False,
        'workspace_billing_guarantee': 'unverified', 'model': model, 'mode': mode,
        'count': 1, 'purpose': 'generation',
        'occurrences': [{'id': ident + '-0', 'index': 0, 'request_sha256': scope['requests'][shot],
                         'native_sha256': native['native_body_sha256'],
                         'profile_sha256': native['profile_sha256']}]}
    if references_sha256 is not None:
        terms['occurrences'][0]['references_sha256'] = references_sha256
    raw = json.dumps({'kind': credit.UNKNOWN_EVIDENCE_KIND, 'terms': terms},
                     sort_keys=True, separators=(',', ':')).encode()
    rel = f'approvals/autonomy-unknown-{ident}.json'
    authorization = {**terms, 'evidence': {'path': rel, 'sha256': hashlib.sha256(raw).hexdigest()}}
    Draft202012Validator(load_schema('unknown_cost_authorization')).validate(authorization)
    evidence_path = _inside(root, rel)
    auth_path = Path(root) / 'artifacts' / f'unknown_cost_authorization-{ident}.json'
    if evidence_path.exists() or auth_path.exists():
        raise AutonomyError('policy unknown-cost capture already exists')
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(raw)
    auth_path.write_text(json.dumps(authorization, indent=2))
    inputs['unknown_cost_authorization_id'] = ident
    inputs['governance']['scope_id'] = ident
    return credit.unknown_cost_authorization_digest(authorization)


def _validate_policy_unknown(root, policy, sha, scope, inputs):
    from lib import openart_credit as credit, openart_dispatch as dispatch, openart_jobs as jobs
    if inputs.get('unknown_cost_authorization_id') != scope['id']:
        raise AutonomyError('unknown-cost authorization is not this exact derived scope')
    authorization = dispatch._authorization(Path(root), inputs)
    spec = _openart_spec(policy)
    if (authorization.get('approved_by') != f'policy:{sha}' or authorization.get('count') != 1
            or authorization.get('purpose') != 'generation' or authorization.get('kind') != 'openart_unknown_cost'
            or authorization.get('exposure_acknowledgement') != UNKNOWN_ACKNOWLEDGEMENT
            or authorization.get('account_id_sha256') != spec['account_id_sha256']
            or authorization.get('workspace') != spec['workspace']
            or authorization.get('evidence', {}).get('path') != f'approvals/autonomy-unknown-{scope["id"]}.json'
            or not _openart_route_approved(spec, authorization.get('model'), authorization.get('mode'))):
        raise AutonomyError('unknown-cost capture differs from exact policy binding/count/purpose')
    profile = jobs.load_qualification(model=inputs.get('model'), mode=inputs.get('mode'), require='production_ready')
    marker = _json(Path(root) / 'project.json')
    shot = inputs['governance']['shot_id']
    return credit.validate_unknown_cost_authorization(root, authorization, scope=scope, marker=marker,
        inputs=inputs, profile=profile, evidence_id=inputs['unknown_cost_evidence_id'],
        request_sha256=scope['requests'][shot], occurrence_index=0, attempt_id=_UNKNOWN_PROBE_ATTEMPT)


def _validate_policy_credit_priced(root, policy, sha, scope, inputs):
    from lib import openart_credit as credit, openart_dispatch as dispatch, openart_jobs as jobs
    authorization = dispatch._authorization(Path(root), inputs)
    spec = next(p for p in policy['providers'] if p['id'] == 'openart_cli')
    if (authorization['approved_by'] != f'policy:{sha}' or authorization['purpose'] != 'generation'
            or authorization['count'] != 1 or authorization['allowance_id'] != openart_allowance_id(sha)
            or authorization['allowance'] != spec['ceiling'] or authorization['ceiling'] != spec['ceiling']):
        raise AutonomyError('credit capture differs from exact policy allowance/count/purpose')
    profile = jobs.load_qualification(model=inputs.get('model'), mode=inputs.get('mode'), require='production_ready')
    marker = _json(Path(root) / 'project.json')
    credit.validate_credit_authorization(root, authorization, scope=scope, marker=marker, inputs=inputs,
        profile=profile, quote_id=inputs.get('credit_quote_id'), request_sha256=scope['requests'][inputs['governance']['shot_id']],
        occurrence_index=0, attempt_id='00000000-0000-4000-8000-000000000000')


def _require_route_decision(root, sha, decision_id, shot_id, provider):
    subject = f'Production route for {shot_id}'
    entries = [e for e in _decision_entries(root) if e['category'] == 'provider_selection' and e['subject'] == subject]
    if not entries or entries[-1]['selected'] != provider or entries[-1].get('user_approved') is not False or f'[policy:{sha} activation:{decision_id}]' not in entries[-1]['reason']:
        raise AutonomyError('policy route decision is missing or stale')


def _append_route_decisions_locked(root, policy, sha, decision_id, shot_id, provider, inputs):
    """Caller holds project lock; avoid nested lock and preserve canonical history."""
    from lib.production_execution import _artifact_path
    from uuid import uuid4
    root = Path(root)
    if (root / 'artifacts/decision_log.json').exists() and (root / 'decision_log.json').exists():
        raise AutonomyError('dual decision log aliases cannot be appended')
    _decision_entries(root)
    path = _artifact_path(root, 'decision_log.json')
    data = _json(path)
    citation = f'[policy:{sha} activation:{decision_id}]'
    entries = [('provider_selection', f'Production route for {shot_id}', provider,
                'Chosen eligible exact approved route ' + citation)]
    if provider == 'openart_mcp':
        counts = root_attempt_counts(root, policy)
        caps = effective_standing_caps(root, policy)
        remaining = {'total': caps['max_total_attempts'] - counts['total'] - 1,
                     'per_shot': caps.get('max_attempts_by_shot', {}).get(shot_id, caps['max_attempts_per_shot']) - counts['per_shot'].get(shot_id, 0) - 1}
        spec = _provider_spec(policy, provider)
        exposure = ('exact connector quote admitted within creator budget '
                    + str(spec['max_credit_budget']) + ' credits; no backend debit ceiling guarantee'
                    if _is_mcp_quoted(spec) else 'unknown cost with no enforceable credit ceiling')
        reason = ('Exact production-ready MCP route ' + inputs['model'] + '/' + inputs['mode']
                  + '; phase first_pass; replaces []'
                  + '; ' + exposure + '; remaining attempt caps '
                  + json.dumps(remaining, sort_keys=True) + ' ' + citation)
        entries[0] = ('provider_selection', f'Production route for {shot_id}', provider, reason)
    baseline = retained_baselines(root, policy)[shot_id]['planned_request_template']['inputs']
    changes = {key: {'from': baseline.get(key), 'to': inputs.get(key)}
               for key in ('duration', 'resolution', 'model', 'operation', 'reference_image_paths', 'image_paths',
                           'images', 'first_frame', 'last_frame', 'image_path', 'last_image_path')
               if baseline.get(key) != inputs.get(key)}
    for key in ('duration', 'resolution'):
        native_alias = 'videoDuration' if key == 'duration' else 'videoResolution'
        before = baseline.get(key, baseline.get('native_params', {}).get(key, baseline.get('native_params', {}).get(native_alias)))
        after = inputs.get(key, inputs.get('native_params', {}).get(key, inputs.get('native_params', {}).get(native_alias)))
        if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
            changes[key] = {'from': before, 'to': after}
    if baseline.get('input_assets') != inputs.get('input_assets'):
        changes['input_assets'] = {'from': baseline.get('input_assets'), 'to': inputs.get('input_assets')}
    if changes:
        entries.append(('budget_tradeoff', f'Production compromises for {shot_id}', 'approved_flex',
                        'Approved deterministic adjustments: ' + json.dumps(changes, sort_keys=True) + ' ' + citation))
    for category, subject, selected, reason in entries:
        options = [{'option_id': selected, 'label': selected, 'score': 1, 'reason': reason}]
        previous = [d for d in data['decisions'] if d['category'] == category and d['subject'] == subject]
        if previous and previous[-1]['selected'] != selected:
            old = previous[-1]['selected']
            options.append({'option_id': old, 'label': old, 'score': 0,
                'reason': previous[-1]['reason'],
                'rejected_because': 'Superseded by the current eligible approved route/flex ' + citation})
        data['decisions'].append({'decision_id': 'auto-' + uuid4().hex, 'stage': 'assets',
            'category': category, 'subject': subject, 'selected': selected, 'reason': reason,
            'user_approved': False, 'options_considered': options})
    Draft202012Validator(load_schema('decision_log')).validate(data)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _explicit_aspect_ratio(inputs, provider):
    """One explicit transport control; aliases never create additional authority."""
    params = inputs.get('native_params', {})
    if not isinstance(params, dict):
        raise AutonomyError('native_params must be an exact controls object')
    keys = [key for key in ('aspect_ratio', 'aspectRatio', 'videoAspectRatio') if key in params]
    if len(keys) > 1 or 'aspect_ratio' in inputs and keys:
        raise AutonomyError('ambiguous duplicate native aspect ratio aliases')
    if keys and (provider not in {'openart_cli', 'openart_mcp'}
                 or keys[0] == 'aspect_ratio'
                 or keys[0] == 'videoAspectRatio' and provider != 'openart_mcp'):
        raise AutonomyError('aspect ratio alias is outside the exact transport')
    if 'aspect_ratio' not in inputs and not keys:
        return None
    ratio = inputs['aspect_ratio'] if 'aspect_ratio' in inputs else params[keys[0]]
    if not isinstance(ratio, str) or not ratio:
        raise AutonomyError('aspect ratio must be an explicit native string')
    return ratio


def _retained_scope_delivery(root, policy, sha, decision_id, scope, inputs):
    """Reprove only an original scope's bound delivery snapshot, not current boards."""
    derived = scope.get('derived_from_policy', {})
    if 'delivery_approval' not in derived and 'delivery_approval_sha256' not in derived:
        return None
    proof = derived.get('delivery_approval')
    if not isinstance(proof, dict) or derived.get('delivery_approval_sha256') != digest(proof):
        raise AutonomyError('retained scope delivery proof hash differs')
    shot_id = inputs['governance']['shot_id']
    retained = retained_baselines(root, policy)[shot_id]
    ratio, checked = _retained_delivery_aspect_ratio(root, policy, retained,
        policy_context=(sha, decision_id), delivery_approval=proof, historical=True, return_proof=True)
    if checked != proof or _explicit_aspect_ratio(inputs, scope['provider']) != ratio:
        raise AutonomyError('retained scope delivery ratio differs from original approval')
    return copy.deepcopy(proof)


def _policy_delivery_approval(root, policy, retained, binding, *, policy_context=None,
                              delivery_approval=None, historical=False):
    """Retain the existing delegated proposal checkpoint, never a request/default."""
    from lib import checkpoint
    from schemas.artifacts import validate_artifact
    basis = policy['activation_basis']
    setup = _bound_document(root, basis['standing_setup'], 'standing setup')
    command = _bound_document(root, basis['run_command'], 'run command')
    _bound_authorization(root, setup, basis['identity_id'])
    _bound_authorization(root, command, basis['identity_id'])
    if binding != command['authorization']:
        raise ValueError('delivery authority differs from exact standing command')
    if policy_context is None:
        active, sha, decision_id = require_active_policy(root)
        if active != policy:
            raise ValueError('delivery policy differs from active standing run')
    else:
        sha, decision_id = policy_context
    if not historical:
        def capture(path):
            raw = _inside(root, path).read_bytes()
            return {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'content': raw.decode('utf-8')}
        current = {'version': '1.0', 'kind': 'policy_proposal',
                   'checkpoint': capture('checkpoint_proposal.json')}
        saved = json.loads(current['checkpoint']['content'])
        preauth = saved.get('review', {}).get('preauth', {})
        current['artifact'] = capture('artifacts/proposal_packet.json')
        current['review'] = capture(preauth['review_path'])
        if delivery_approval is not None and delivery_approval != current:
            raise ValueError('current delivery approval differs from retained scope')
        delivery_approval = current
    if (not isinstance(delivery_approval, dict)
            or set(delivery_approval) != {'version', 'kind', 'checkpoint', 'artifact', 'review'}
            or delivery_approval['version'] != '1.0' or delivery_approval['kind'] != 'policy_proposal'):
        raise ValueError('exact retained delivery checkpoint proof required')
    records, raw = {}, {}
    for key in ('checkpoint', 'artifact', 'review'):
        item = delivery_approval[key]
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'content'} or not isinstance(item['content'], str):
            raise ValueError('delivery proof needs exact retained UTF-8 bytes')
        _inside(root, item['path'])
        raw[key] = item['content'].encode('utf-8')
        if hashlib.sha256(raw[key]).hexdigest() != item['sha256']:
            raise ValueError('retained delivery proof bytes changed')
        records[key] = json.loads(raw[key])
    saved, proposal, review = (records[k] for k in ('checkpoint', 'artifact', 'review'))
    checkpoint.validate_checkpoint(saved)
    validate_artifact('proposal_packet', proposal)
    validate_artifact('review', review)
    marker = _json(Path(root) / 'project.json')
    if (delivery_approval['checkpoint']['path'] != 'checkpoint_proposal.json'
            or saved['stage'] != 'proposal' or saved['status'] != 'completed'
            or saved['project_id'] != policy['project_id'] or saved['pipeline_type'] != marker['pipeline_type']
            or proposal['production_plan']['pipeline'] != saved['pipeline_type']
            or proposal['approval']['status'] == 'rejected'):
        raise ValueError('delivery proposal checkpoint is not the exact completed run stage')
    preauth = saved.get('review', {}).get('preauth')
    definitions = checkpoint._load_checkpoint_schema()['$defs']
    Draft202012Validator(definitions['preauth']).validate(preauth)
    approval = saved.get('metadata', {}).get('approval_basis')
    Draft202012Validator(definitions['approval_basis']).validate(approval)
    _validate_preauth_bytes(root, 'proposal', approval, preauth, policy, sha, decision_id,
        artifact_bytes=raw['artifact'], review_bytes=raw['review'])
    if (delivery_approval['artifact']['path'] != preauth['artifact_path']
            or delivery_approval['review']['path'] != preauth['review_path']
            or saved['artifacts'].get('proposal_packet') != proposal
            or review['artifact_path'] != preauth['artifact_path']):
        raise ValueError('delivery checkpoint artifact/review bindings disagree')
    metadata = proposal.get('metadata', {})
    if (metadata.get('identity_id') != basis['identity_id']
            or metadata.get('story_revision') != policy['story_revision']
            or 'project_id' in metadata and metadata['project_id'] != policy['project_id']):
        raise ValueError('delivery proposal identity/project/story differs from standing run')
    packages = [retained['projection']['scene_snapshot'].get('metadata', {}).get('package'),
                retained['script_snapshot'].get('metadata', {}).get('package')]
    if packages[0] != packages[1] or metadata.get('package') != packages[0]:
        raise ValueError('delivery proposal differs from retained package')
    if packages[0] is not None:
        _bound_document(root, packages[0], 'delivery production package')
    return metadata.get('delivery', {}), delivery_approval


def _retained_delivery_aspect_ratio(root, policy, retained, *, policy_context=None,
                                   delivery_approval=None, historical=False, return_proof=False):
    """Resolve existing exact standing-run approval, never mutable defaults/prose.

    Frozen scene/script bindings identify either the manual approved proposal or
    the standing command whose policy delegates the canonical proposal stage.
    Its byte-bound delivery proof changes neither the template nor the policy.
    """
    from fractions import Fraction
    from lib.media_profiles import get_profile
    try:
        basis = policy.get('activation_basis', {})
        if policy.get('planning_mode') != 'shot_local_v1' or basis.get('kind') != 'standing_run':
            raise ValueError('exact standing-run delivery approval required')
        metadata = [retained['projection']['scene_snapshot'].get('metadata', {}),
                    retained['script_snapshot'].get('metadata', {})]
        bindings = []
        for source in metadata:
            approval = source.get('approval')
            if (not isinstance(approval, dict) or set(approval) - {'path', 'sha256', 'approved_by'}
                    or not {'path', 'sha256'} <= set(approval)):
                raise ValueError('retained scene/script delivery approval missing')
            bindings.append({key: approval[key] for key in ('path', 'sha256')})
        if bindings[0] != bindings[1]:
            raise ValueError('retained scene/script delivery approvals disagree')
        approval = _bound_document(root, bindings[0], 'delivery approval')
        proof = None
        if (approval.get('kind') == 'creator_authorization' and approval.get('origin') == 'user_message'
                and not {'status', 'approved_proposal'}.intersection(approval)):
            delivery, proof = _policy_delivery_approval(root, policy, retained, bindings[0],
                policy_context=policy_context, delivery_approval=delivery_approval, historical=historical)
        else:
            if delivery_approval is not None or approval.get('version') != '1.0' or approval.get('status') != 'approved':
                raise ValueError('delivery proposal is not approved')
            proposal = _bound_document(root, approval.get('approved_proposal'), 'approved delivery proposal')
            if proposal.get('version') != '1.0' or proposal.get('identity_id') != basis.get('identity_id'):
                raise ValueError('approved delivery identity differs from standing run')
            delivery = proposal.get('delivery', {})
        profile = get_profile(delivery.get('profile'))
        ratio = profile.aspect_ratio.value
        width, height = map(int, ratio.split(':'))
        if (type(profile.width) is not int or type(profile.height) is not int
                or min(profile.width, profile.height, width, height) <= 0
                or Fraction(profile.width, profile.height) != Fraction(width, height)):
            raise ValueError('approved profile dimensions and aspect ratio disagree')
        expected = {'profile': profile.name, 'width': profile.width,
                    'height': profile.height, 'aspect_ratio': ratio}
        for declaration in [delivery, *(source.get('delivery', {}) for source in metadata)]:
            if not isinstance(declaration, dict) or any(
                    key in declaration and declaration[key] != value for key, value in expected.items()):
                raise ValueError('retained delivery format declarations disagree')
        lock = policy['locked']['controls'].get('aspect_ratio')
        if lock is not None and (('value' in lock and digest(lock['value']) != digest(ratio))
                or 'value_sha256' in lock and lock['value_sha256'] != hashlib.sha256(ratio.encode()).hexdigest()):
            raise ValueError('approved delivery differs from retained aspect ratio lock')
        return (ratio, proof) if return_proof else ratio
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise AutonomyError('retained approved delivery format required: ' + str(exc)) from exc


def _validate_request_delta(root, policy, shot_id, inputs, provider, *, policy_context=None,
                            delivery_approval=None, native_audio_off_approval=None, historical=False):
    """Compare rooted actual inputs with exact retained layout and approved flex."""
    from lib import production_execution as execution
    if not _intent_allows(policy, shot_id, provider, inputs.get('model') if provider in {'openart_cli', 'openart_mcp'} else None):
        raise AutonomyError('request route violates approved per-shot model selection intent')
    retained = retained_baselines(root, policy)[shot_id]
    template = retained['planned_request_template']
    baseline = execution._clean(template['inputs'])
    actual = execution._clean(inputs)
    sha = policy_context[0] if policy_context is not None else policy_sha256(policy, retained_baselines(root, policy))
    audio_off = _native_audio_off_proof(root, policy, sha, shot_id, inputs, provider,
        proof=native_audio_off_approval, historical=historical)
    if ('preferred_provider' in actual and actual['preferred_provider'] != provider) or ('allowed_providers' in actual and actual['allowed_providers'] != [provider]):
        raise AutonomyError('policy dispatch requires exact singleton route')
    # Planner route plumbing: the tool/host names must be the canonical ones for
    # the singleton provider; anything foreign or non-canonical stays blocked.
    if 'preferred_tool' in actual and actual['preferred_tool'] != ROUTE_TOOLS.get(provider):
        raise AutonomyError('preferred_tool is not the canonical tool for the policy route')
    if 'hosting_provider' in actual and actual['hosting_provider'] != provider:
        raise AutonomyError('hosting_provider is not the canonical host for the policy route')
    routing = {'model', 'mode', 'operation', 'image_upload_id', 'preferred_provider', 'allowed_providers',
               'preferred_tool', 'hosting_provider'}
    if provider == 'openart_mcp' and ('image_upload_id' in actual or 'image_upload_id' in baseline):
        if not any(field in actual for field in ('image_path', 'first_frame', 'reference_image_path')):
            raise AutonomyError('MCP start upload selector requires its exact local start-frame alias')
        if ('image_upload_id' in baseline
                and baseline['image_upload_id'] != actual.get('image_upload_id')):
            raise AutonomyError('MCP start upload selector differs from retained explicit selector')
    if provider == 'openart_mcp' and 'end_image_upload_id' in actual:
        if not any(field in actual for field in ('last_image_path', 'last_frame')):
            raise AutonomyError('MCP end upload selector requires its exact local end-frame alias')
        if ('end_image_upload_id' in baseline
                and baseline['end_image_upload_id'] != actual['end_image_upload_id']):
            raise AutonomyError('MCP end upload selector differs from retained explicit selector')
        # The ordinary source comparator below retains the exact root occurrence,
        # role and current hash; native preparation validates this receipt pin.
        routing.add('end_image_upload_id')
    plumbing = {'output_path', 'cli_session_id', 'timeout_seconds', 'cwd', 'allow_unknown_cost',
                'native_dry_run_receipt_id', 'native_dry_run_receipt_sha256'}
    assets = iter(template['static_input_assets'])
    selected = execution.load_selected_attempts(root)
    dynamic = []
    def resolve(value, pointer='', key=None):
        if isinstance(value, dict) and '$board' in value:
            binding = execution._board_binding(value)
            if key not in execution.INPUT_PATH_KEYS:
                raise AutonomyError('declared board must occupy exact asset input')
            record = execution.resolve_board_input(root, binding)
            slots = [a for a in retained['projection']['assets'] if a['id'] == binding['asset_id']]
            if policy.get('planning_mode') != 'shot_local_v1' or len(slots) != 1:
                raise AutonomyError('board input lies outside exact prospective consumer')
            slot = slots[0]
            from lib import production_boards as boards
            if slot.get('preparation_pending') or slot['id'] in boards.load_board_pack(root)['prompt_pack']:
                _validate_generated_board_source(root, {**slot, **record}, expected=slot)
            else:
                # Compact plans address both approved supplied references and
                # generated nodes through $board. Supplied sources remain exact
                # frozen bytes; the canonical resolver already rechecks reuse
                # attribution and role reviews, without pretending a source is
                # a same-run image node.
                current = next((a for a in execution.load_shot_contract(root)['assets']
                                if a['id'] == slot['id']), None)
                if (current is None or any(current.get(k) != slot.get(k)
                        for k in ('id', 'role', 'cast_ids', 'sha256'))
                        or record['sha256'] != slot.get('sha256')
                        or _inside(root, record['path']) != _inside(root, slot.get('path', ''))
                        or _inside(root, current['path']) != _inside(root, record['path'])):
                    raise AutonomyError('supplied board source differs from exact retained reference')
            dynamic.append((pointer, copy.deepcopy(binding), record['sha256']))
            return {'sha256': record['sha256']}
        if isinstance(value, dict) and '$upstream' in value:
            binding = execution._upstream_binding(value)
            record = selected.get(binding['shot_id'], {}).get(binding['role'], {})
            if not record.get('path') or not record.get('sha256') or hashlib.sha256(_inside(root, record['path']).read_bytes()).hexdigest() != record['sha256']:
                raise AutonomyError('upstream request lacks actual selected bytes')
            dynamic.append((pointer, copy.deepcopy(binding), record['sha256']))
            return {'sha256': record['sha256']}
        if key == 'input_assets':
            names = {'first_frame': 'first_frame', 'last_frame': 'last_frame',
                     'reference_image': 'reference_image_paths', 'reference_video': 'reference_video_paths',
                     'reference_audio': 'reference_audio_paths',
                     'environment_reference': 'reference_image_paths', 'character_reference': 'reference_image_paths'}
            if not isinstance(value, list):
                raise AutonomyError('input_assets must be ordered canonical role bindings')
            rows = []
            for index, item in enumerate(value):
                if not isinstance(item, dict) or item.get('role') not in names or not item.get('source_path'):
                    raise AutonomyError('retained input_assets requires explicit role and source_path')
                if isinstance(item['source_path'], dict) and '$board' in item['source_path']:
                    source = resolve(item['source_path'], pointer + '/' + str(index) + '/source_path', names[item['role']])
                    if 'source_sha256' in item and item['source_sha256'] != source['sha256']:
                        raise AutonomyError('retained canonical board source hash differs')
                    rows.append({**copy.deepcopy(item), 'source_path': source, 'source_sha256': source['sha256']})
                    continue
                record = next(assets, None)
                if (record is None or record['role'] != names[item['role']]
                        or _inside(root, record['path']) != _inside(root, item['source_path'])
                        or 'source_sha256' in item and record['sha256'] != item['source_sha256']):
                    raise AutonomyError('retained canonical role/source occurrence differs')
                rows.append({**copy.deepcopy(item), 'source_path': {'sha256': record['sha256']}, 'source_sha256': record['sha256']})
            return rows
        if key in execution.INPUT_PATH_KEYS:
            if isinstance(value, list):
                return [resolve(v, pointer + '/' + str(i), key) for i, v in enumerate(value)]
            record = next(assets, None)
            if record is None or record['role'] != key or _inside(root, record['path']) != _inside(root, value):
                raise AutonomyError('retained static template occurrence differs')
            return {'sha256': record['sha256']}
        if isinstance(value, dict):
            return {k: resolve(v, pointer + '/' + k, k) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, pointer + '/' + str(i)) for i, v in enumerate(value)]
        return value
    expected = resolve(baseline)
    if next(assets, None) is not None:
        raise AutonomyError('unused retained static template occurrence')
    bound_actual = execution._paths(actual, Path(root).resolve(),
                                    lambda key, path: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    aliases = {'image_path': 'start_frame', 'reference_image_path': 'start_frame', 'first_frame': 'start_frame',
               'last_image_path': 'end_frame', 'last_frame': 'end_frame', 'reference_image_paths': 'references', 'image_paths': 'references', 'images': 'references'}
    def normalize(value):
        result = {}
        for key, item in value.items():
            target = aliases.get(key, key)
            if target in result:
                raise AutonomyError('ambiguous duplicated semantic input aliases')
            result[target] = item
        return result
    expected, bound_actual = normalize(expected), normalize(bound_actual)
    # Dynamic sources keep their exact nested layout/order and current bytes.
    # Only the same established root aliases used by the semantic comparison
    # may rename their public input occurrence across an approved route switch.
    for pointer, binding, expected_sha in dynamic:
        parts = pointer.lstrip('/').split('/')
        parts[0] = aliases.get(parts[0], parts[0])
        value = bound_actual
        for part in parts:
            value = value[int(part)] if isinstance(value, list) else value.get(part)
        if value != {'sha256': expected_sha}:
            raise AutonomyError('exact upstream pointer/layout/order changed')
    for key in routing | plumbing | {'prompt', 'duration', 'resolution'}:
        expected.pop(key, None)
        bound_actual.pop(key, None)
    if execution.explicit_motion_duration(inputs, provider=provider) != current_projection(root, shot_id)['shot']['duration_seconds']:
        raise AutonomyError('request duration does not match approved deterministic planning')
    def explicit_resolution(value):
        params = value.get('native_params', {})
        if not isinstance(params, dict):
            raise AutonomyError('native_params must be an exact controls object')
        keys = [key for key in ('resolution', 'videoResolution') if key in params]
        if len(keys) > 1 or 'resolution' in value and keys:
            raise AutonomyError('ambiguous duplicate native resolution aliases')
        return value.get('resolution', params.get(keys[0]) if keys else None)
    actual_resolution, baseline_resolution = explicit_resolution(actual), explicit_resolution(baseline)
    if actual_resolution != baseline_resolution and actual_resolution not in policy['flex']['resolution']:
        raise AutonomyError('request resolution changed outside declared flex')
    actual_ratio = _explicit_aspect_ratio(actual, provider)
    baseline_ratio = _explicit_aspect_ratio(baseline, provider)
    delivery_proof = None
    if actual_ratio != baseline_ratio:
        if baseline_ratio is not None or actual_ratio is None:
            raise AutonomyError('request aspect ratio changed outside retained template')
        ratio, delivery_proof = _retained_delivery_aspect_ratio(root, policy, retained,
            policy_context=policy_context, delivery_approval=delivery_approval,
            historical=historical, return_proof=True)
        if actual_ratio != ratio:
            raise AutonomyError('request aspect ratio differs from retained approved delivery format')
    # The MCP project is already an exact immutable policy/native-body lock.
    # Admit its required public transport field when the input layout omits it;
    # an explicit retained declaration must independently agree with that lock.
    if provider == 'openart_mcp' and ('openart_project_id' in actual or 'openart_project_id' in baseline):
        project = _provider_spec(policy, provider)['project_id']
        if actual.get('openart_project_id') != project:
            raise AutonomyError('request MCP project differs from retained policy')
        if 'openart_project_id' in baseline and baseline['openart_project_id'] != project:
            raise AutonomyError('retained MCP project differs from policy')
        expected.pop('openart_project_id', None)
        bound_actual.pop('openart_project_id', None)
    # Normalize only established flex and the exact retained/projected ratio.
    # Native preparation independently checks the selected model's actual form;
    # unrelated controls remain in the retained-template equality below.
    if audio_off is not None:
        bound_actual['native_params'].pop('generateAudio')
    for value in (expected, bound_actual):
        value.pop('aspect_ratio', None)
        if isinstance(value.get('native_params'), dict):
            value['native_params'].pop('duration', None)
            value['native_params'].pop('resolution', None)
            value['native_params'].pop('aspectRatio', None)
            value['native_params'].pop('videoAspectRatio', None)
            if provider == 'openart_mcp':
                value['native_params'].pop('videoDuration', None)
                value['native_params'].pop('videoResolution', None)
            if not value['native_params']:
                value.pop('native_params')
    # Only explicitly approved reference roles may be dropped/substituted; cast
    # conflicts are rejected whole at policy activation and implicit locks reprove.
    projection = retained['projection']
    def role_for(sha):
        roles = {a['role'] for a in projection['assets'] if a.get('sha256') == sha}
        return roles
    if provider == 'openart_mcp' and expected.get('input_assets') != bound_actual.get('input_assets'):
        old_rows, new_rows = expected.get('input_assets', []), bound_actual.get('input_assets', [])
        position, accepted = 0, True
        substitutes = {(s['sha256'], s['role']) for s in policy['flex']['references']['substitutes']}
        current_assets = execution.load_shot_contract(root)['assets']
        for old in old_rows:
            new = new_rows[position] if position < len(new_rows) else None
            if digest(old) == digest(new):
                position += 1
                continue
            # Unplanned receipt selectors bind an approved source at preparation;
            # preserve explicit retained selectors and freshly validate receipts.
            without_selector = lambda row: {k: v for k, v in row.items() if k not in {'upload_id', 'reference_id'}}
            if (not {'upload_id', 'reference_id'}.intersection(old)
                    and isinstance(new, dict) and without_selector(old) == without_selector(new)):
                position += 1
                continue
            old_sha = old.get('source_sha256')
            roles = role_for(old_sha)
            new_sha = new.get('source_sha256') if isinstance(new, dict) else None
            new_roles = {a['role'] for a in current_assets if a.get('sha256') == new_sha}
            stripped = lambda row: {k: v for k, v in row.items() if k not in {'source_path', 'source_sha256', 'upload_id', 'reference_id'}}
            if (new is not None and stripped(old) == stripped(new) and new_sha != old_sha
                    and any((new_sha, role) in substitutes and role in new_roles for role in roles)):
                position += 1
                continue
            if roles and roles.issubset(set(policy['flex']['references']['droppable_roles'])):
                continue
            accepted = False
            break
        if accepted and position == len(new_rows):
            if 'input_assets' in bound_actual:
                expected['input_assets'] = copy.deepcopy(new_rows)
            else:
                expected.pop('input_assets', None)
    for key in set(expected) | set(bound_actual):
        if key not in {'references', 'start_frame', 'end_frame'}:
            continue
        old, new = expected.get(key), bound_actual.get(key)
        if old == new:
            continue
        old_list = old if isinstance(old, list) else ([] if old is None else [old])
        new_list = new if isinstance(new, list) else ([] if new is None else [new])
        substitutes = {(s['sha256'], s['role']) for s in policy['flex']['references']['substitutes']}
        current_assets = execution.load_shot_contract(root)['assets']
        position = 0
        accepted = True
        for old_item in old_list:
            new_item = new_list[position] if position < len(new_list) else None
            if old_item == new_item:
                position += 1
                continue
            roles = role_for(old_item['sha256'])
            new_roles = {a['role'] for a in current_assets if new_item and a.get('sha256') == new_item['sha256']}
            if new_item and any((new_item['sha256'], role) in substitutes and role in new_roles for role in roles):
                position += 1
                continue
            if roles and roles.issubset(set(policy['flex']['references']['droppable_roles'])):
                continue
            accepted = False
            break
        if accepted and position == len(new_list):
            if new is None:
                expected.pop(key, None)
            else:
                expected[key] = new
    if digest(expected) != digest(bound_actual):
        raise AutonomyError('request changed outside retained template/approved flex: ' + ', '.join(_diff(expected, bound_actual)))
    return delivery_proof


def _apply_reference_planning_flex(policy, expected, current):
    """Only declared noncast reference deletions or exact approved role/bytes edits."""
    droppable = set(policy['flex']['references']['droppable_roles'])
    substitutes = {(s['sha256'], s['role']) for s in policy['flex']['references']['substitutes']}
    actor_ids = set(expected['late_cast_ids']) | set(expected['payoff_speaker_ids'])
    actor_ids.update(cid for shot in expected['shots'] if shot['id'] in policy['shots'] for cid in shot['cast_ids'])
    actor_bound = {a['id'] for a in expected['assets'] if not a.get('upstream_source') and actor_ids.intersection(a['cast_ids'])}
    expected_assets = {a['id']: a for a in expected['assets']}
    current_assets = {a['id']: a for a in current['assets']}
    for asset_id, old in expected_assets.items():
        new = current_assets.get(asset_id)
        if new is None:
            continue
        if old == new:
            continue
        if old['id'] in actor_bound or old['role'] in CAST_ROLES or (new.get('sha256'), old['role']) not in substitutes:
            continue
        proposed = copy.deepcopy(old)
        for key in ('path', 'sha256'):
            if key in new:
                proposed[key] = new[key]
        if proposed == new:
            old.update(proposed)
    current_shots = {s['id']: s for s in current['shots']}
    for shot in expected['shots']:
        new = current_shots.get(shot['id'])
        if new is None:
            continue
        desired = new['asset_ids']
        remaining = [aid for aid in shot['asset_ids'] if aid in desired or aid in actor_bound or expected_assets[aid]['role'] not in droppable]
        if remaining == desired:
            shot['asset_ids'] = list(desired)
    needed = {aid for shot in current['shots'] for aid in shot['asset_ids']}
    if current.get('payoff_asset_id') is not None:
        needed.add(current['payoff_asset_id'])
    needed.update(actor_bound)
    needed.update(a['id'] for a in expected['assets'] if a['role'] == 'identity_reference')
    expected['assets'] = [a for a in expected['assets'] if a['id'] in current_assets or a['id'] in needed or a['role'] not in droppable]
