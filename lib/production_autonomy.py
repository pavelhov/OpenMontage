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

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

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


class AutonomyError(ValueError):
    """A policy-derived action is not provably inside the approved envelope."""


# ---------------------------------------------------------------- evidence

def evidence_content(policy, baselines):
    """Exact retained human approval document; never infer baselines from descriptors."""
    return {'kind': EVIDENCE_KIND,
            'policy': {k: copy.deepcopy(v) for k, v in policy.items() if k != 'evidence'},
            'baselines': copy.deepcopy(baselines)}


def policy_sha256(policy, baselines):
    return digest(evidence_content(policy, baselines))


def retained_baselines(root, policy):
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


def load_active_policy(root):
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
        # Retained original sources remain immutable authority even if a legal
        # request drops a reference or chooses separate approved substitute bytes.
        for retained in baselines.values():
            for asset in retained['projection']['assets']:
                if not asset.get('upstream_source') and hashlib.sha256(_inside(root, asset['path']).read_bytes()).hexdigest() != asset['sha256']:
                    raise AutonomyError('retained original static source bytes changed')
        decision = activation_decision(root)
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
            from lib.pipeline_loader import load_pipeline_readonly
            manifest = load_pipeline_readonly(marker['pipeline_type'])
            gated = {s['name'] for s in manifest['stages']
                     if s.get('human_approval_default') is True and s['name'] != 'publish'}
            if not set(policy['checkpoint_stages']).issubset(gated):
                reasons.append('checkpoint stages are not actual gated pipeline stages')
        _validate_global_planning(root, policy, baselines)
        for shot_id, retained in baselines.items():
            current = current_projection(root, shot_id)
            _validate_delta(policy, shot_id, current, retained['projection'], allow_global_shift=True)
    except Exception as exc:
        return None, None, None, [f'unreadable policy: {exc}']
    if reasons:
        return None, None, None, reasons
    return policy, sha, decision.get('decision_id'), []


def require_active_policy(root):
    policy, sha, decision_id, reasons = load_active_policy(root)
    if policy is None:
        raise AutonomyError('Strict: ' + '; '.join(reasons))
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


def _openart_route_approved(spec, model, mode):
    if _is_unknown(spec):
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
                names = ({r['model'] for r in spec['routes']} if _is_unknown(spec) else set(spec['models'])) if spec else set()
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
    if mcp_spec is not None and not _is_unknown(mcp_spec):
        out.append('conflict: MCP requires fresh no-enforceable-ceiling acceptance')
    if _is_unknown(mcp_spec) and 'model' in policy['locked']['controls'] and not any(_model_lock_allows(policy['locked']['controls']['model'], route['model']) for route in mcp_spec['routes']):
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
                        if not a.get('upstream_source') and
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
        cast = set(shot['cast_ids']) | set(contract['late_cast_ids']) | set(contract['payoff_speaker_ids'])
        needed = set(shot['asset_ids'])
        if contract.get('payoff_asset_id') is not None:
            needed.add(contract['payoff_asset_id'])
        needed.update(a['id'] for a in contract['assets']
                      if not a.get('upstream_source') and cast.intersection(a['cast_ids']))
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
    return copy.deepcopy({'story': contract['story'], 'shot': shot, 'assets': assets,
        'late_cast_ids': contract['late_cast_ids'], 'payoff_speaker_ids': contract['payoff_speaker_ids'],
        'payoff_asset_id': contract.get('payoff_asset_id'), 'implicit_cast_ids': sorted(cast),
        'order': {'shots': [s['id'] for s in contract['shots']],
                  'scenes': [s['id'] for s in scene_plan['scenes']],
                  'script_sections': [s['id'] for s in script['sections']]},
        'scene': scenes[0], 'script_section': sections[0],
        'shot_card': cards.get(shot_id) if isinstance(cards, dict) else cards,
        'manifest_requirements': _manifest_requirement_binding(manifest, shot_id),
        'contract_snapshot': snapshot, 'scene_snapshot': copy.deepcopy(scene_plan)})


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
        if not asset.get('upstream_source'):
            if hashlib.sha256(_inside(root, asset['path']).read_bytes()).hexdigest() != asset['sha256']:
                raise AutonomyError('static reference bytes changed')
    return projection


def dialogue_occurrences(projection):
    return [{'index': i, 'source_pointer': f'/shot/dialogue/{i}', 'line': copy.deepcopy(line)}
            for i, line in enumerate(projection['shot'].get('dialogue', []))]


def baseline_descriptor(retained, root=None):
    if set(retained) != {'projection', 'planned_request_template', 'script_snapshot',
                         'dialogue_coverage', 'approval_plan_sha256'}:
        raise AutonomyError('incomplete retained baseline')
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
            if '$upstream' in value:
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
    if approval_plan_digest(projection['contract_snapshot']) != retained['approval_plan_sha256']:
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
    from lib.production_retime import retime_planning
    transformed = retime_planning(contract, scene_plan, script, durations)
    return transformed['contract'], transformed['scene_plan'], transformed['script']


def _validate_global_planning(root, policy, baselines):
    from lib.production_execution import _artifact_path
    durations = {shot_id: current_projection(root, shot_id)['shot']['duration_seconds'] for shot_id in baselines}
    expected_contract, expected_scene, expected_script = _canonical_global_planning(policy, baselines, durations)
    current = current_projection(root, next(iter(baselines)))
    script = _json(_artifact_path(Path(root), 'script.json'))
    _apply_reference_planning_flex(policy, expected_contract, current['contract_snapshot'])
    if current['contract_snapshot'] != expected_contract or current['scene_snapshot'] != expected_scene or script != expected_script:
        raise AutonomyError('whole retained planning changed outside deterministic flex')


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


def contract_delta(root, shot_id, candidate):
    """Compare only against retained evidence, never against a caller baseline."""
    policy, _, _ = require_active_policy(root)
    baseline = retained_baselines(root, policy)[shot_id]['projection']
    if candidate.get('contract_snapshot') != current_projection(root, shot_id)['contract_snapshot'] or candidate.get('scene_snapshot') != current_projection(root, shot_id)['scene_snapshot']:
        raise AutonomyError('candidate global planning differs from validated current bytes')
    current = current_projection(root, shot_id)
    duration = candidate.get('shot', {}).get('duration_seconds')
    if candidate != current and (duration not in policy['flex']['duration_s'] or candidate != retime(current, duration)):
        raise AutonomyError('candidate is not actual planning or deterministic prospective retime')
    return _validate_delta(policy, shot_id, candidate, baseline, allow_global_shift=True)


def _validate_delta(policy, shot_id, candidate, baseline_projection, *, allow_global_shift=False):
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
    if allow_global_shift:
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
            if not _is_unknown(spec) or row.get('production_available') is not True:
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

def lock_proof(root, shot_id, *, inputs, native, profile):
    return _root_lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile)


def _root_lock_proof(root, shot_id, *, inputs, native, profile, historical=None):
    """Derive locks from rooted actual submitted bytes and named preparation evidence."""
    from lib import production_request as preparation
    from lib.production_execution import _paths, _clean, _artifact_path
    policy, sha, _ = require_active_policy(root)
    retained = retained_baselines(root, policy)[shot_id]
    projection = retained['projection']
    contract_delta(root, shot_id, current_projection(root, shot_id))
    if historical is None:
        preparation.validate_preparation(inputs, native, profile)
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
    proof = {'policy_sha256': sha, 'baseline_sha256': digest(retained),
             'compiled_sha256': digest(compiled), 'review_sha256': digest(review),
             'inputs': actual, 'cast': [], 'controls': {}}
    for asset in projection['assets']:
        if asset.get('upstream_source'):
            continue
        locked_cast = (asset['id'] == projection.get('payoff_asset_id') or
                       bool(set(asset.get('cast_ids', [])) & set(projection['implicit_cast_ids'])))
        if not locked_cast and asset['sha256'] not in policy['locked']['sources']:
            continue
        if (asset['sha256'], asset['role']) in frozen:
            proof['cast'].append([asset['id'], asset['sha256'], 'direct'])
            continue
        carriers = [b for b in boards if (b['sha256'], 'start_frame') in frozen and b['role'] == 'start_frame'
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
            carrier = any((b['sha256'], 'start_frame') in frozen and any(
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
    canonical = preparation.compile_provider_prompt(root, shot_id, provider=provider, **({'model': native['model']} if provider == 'openart_mcp' else {}))
    body = preparation._body(native)
    prompt = body['params'].get('sceneDescription' if provider == 'openart_mcp' and native.get('prompt_param_path') == '/params/sceneDescription' else 'prompt')
    if prompt != canonical['prompt'] or compiled['coverage'] != canonical['coverage']:
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
        for public, field in [('duration', 'videoDuration'), ('resolution', 'videoResolution'), ('aspect_ratio', 'videoAspectRatio')]:
            if field in actual_controls:
                actual_controls[public] = actual_controls[field]
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
    return digest(proof)


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


def check_caps(policy, shot_id, counts, phase):
    caps = policy['caps']
    if counts['unmapped_outbox']:
        raise AutonomyError('caps: unmapped outbox attempt (fail closed)')
    if counts['total'] + 1 > caps['max_total_attempts']:
        raise AutonomyError('caps: total attempts exhausted; deliver draft')
    if counts['per_shot'].get(shot_id, 0) + 1 > caps['max_attempts_per_shot']:
        raise AutonomyError(f'caps: attempts for {shot_id} exhausted; deliver draft')
    if phase == 'repair' and counts['repair'] + 1 > caps['max_repair_attempts']:
        raise AutonomyError('caps: repair attempts exhausted; deliver draft')


# ---------------------------------------------------------------- derived scope

def openart_allowance_id(sha):
    return f'policy:{sha}:openart'


def _scope_record(policy, sha, decision_id, *, shot_id, provider, request_digest, approval_plan_sha256,
                 derivation_index, lock_digest, compiled_request_sha256=None, preparation_review_id=None,
                 resolved_upstream=None, phase='first_pass', replaces_attempt_ids=None, credit_authorization_sha256=None,
                 unknown_cost_authorization_sha256=None):
    """Normal v1.0 production scope for one attempt; exact single-shot scope."""
    if shot_id not in policy['shots']:
        raise AutonomyError(f'{shot_id} has no approved baseline')
    if provider not in {p['id'] for p in policy['providers']}:
        raise AutonomyError(f'{provider} not approved by policy')
    if phase not in ('first_pass', 'repair') or (phase == 'repair') != bool(replaces_attempt_ids):
        raise AutonomyError('repair scope must (only) name replaced attempts')
    scope = {
        'id': f'policy-{sha[:12]}-{shot_id}-{derivation_index}', 'status': 'approved',
        'approved_by': f'policy:{sha}', 'evidence': dict(policy['evidence']),
        'project_id': policy['project_id'], 'story_revision': policy['story_revision'],
        'phase': phase, 'provider': provider, 'requests': {shot_id: request_digest},
        'attempts_per_shot': {shot_id: 1}, 'approval_plan_sha256': approval_plan_sha256,
        'derived_from_policy': {
            'policy_sha256': sha, 'decision_id': decision_id, 'derivation_index': derivation_index,
            'baseline_sha256': digest(policy['shots'][shot_id]), 'lock_digest': lock_digest,
            'compiled_request_sha256': compiled_request_sha256,
            'preparation_review_id': preparation_review_id,
            'resolved_upstream': copy.deepcopy(resolved_upstream or {}),
        },
    }
    if phase == 'repair':
        scope['replaces_attempt_ids'] = list(replaces_attempt_ids)
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


def _prepare_mcp_policy_native(root, policy, shot_id, inputs):
    """Qualified connector evidence is separate from CLI account and preview proof."""
    from lib import openart_mcp as mcp, production_execution as execution
    spec = _provider_spec(policy, 'openart_mcp')
    if not _is_unknown(spec) or not _openart_route_approved(spec, inputs.get('model'), inputs.get('mode')):
        raise AutonomyError('exact MCP model/mode is outside freshly approved policy')
    if not _intent_allows(policy, shot_id, 'openart_mcp', inputs.get('model')):
        raise AutonomyError('MCP route violates retained model selection intent')
    profile = mcp.load_profile(inputs.get('model'), inputs.get('mode'), require='supported')
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    mcp.validate_native_request(native, profile)
    _validate_mcp_policy_binding(spec, native, profile)
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


def _mcp_policy_terms(root, policy, sha, decision_id, scope, inputs, native):
    """Canonical derived terms; no caller assertion of unknown cost grants authority."""
    spec = _provider_spec(policy, 'openart_mcp')
    if not _is_unknown(spec):
        raise AutonomyError('MCP policy lacks fresh no-enforceable-ceiling acknowledgement')
    return {'kind': 'unknown_cost', 'provider': 'openart_mcp',
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
    policy, sha, decision_id, material = _scope_material(root, inputs, 'openart_mcp', derived_unknown_id=scope['id'])
    expected = _scope_record(policy, sha, decision_id, derivation_index=scope['derived_from_policy']['derivation_index'],
        phase=scope['phase'], replaces_attempt_ids=scope.get('replaces_attempt_ids'), **material)
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


def _scope_material(root, inputs, provider, observation=None, *, derived_unknown_id=None):
    _refuse_unknown_cost(inputs, derived_unknown_id=derived_unknown_id, allow_evidence=True)
    from lib import production_request as preparation, production_execution as execution
    policy, sha, decision_id = require_active_policy(root)
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
        if not unknown:
            raise AutonomyError('MCP requires a freshly approved distinct unknown-cost policy')
        if _json(Path(root) / 'project.json').get('pipeline_type') == 'provider-qualification':
            raise AutonomyError('MCP qualification requires separate Strict approval')
        if any(key in inputs for key in ('credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256', 'unknown_cost_evidence_id', 'unknown_cost_authorization_id', 'mcp_billing_authorization_id', 'billing_authority')):
            raise AutonomyError('MCP policy refuses caller billing authority and CLI authority fields')
    elif unknown and any(key in inputs for key in ('unknown_cost_evidence_id', 'unknown_cost_authorization_id')):
        raise AutonomyError('unknown-cost authority is OpenArt-only')
    shot_id = inputs['governance']['shot_id']
    if provider not in {p['id'] for p in policy['providers']}:
        raise AutonomyError('route provider outside approved policy')
    _validate_request_delta(root, policy, shot_id, inputs, provider)
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
    proof = preparation.validate_preparation(inputs, native, profile)
    locks = lock_proof(root, shot_id, inputs=inputs, native=native, profile=profile)
    contract = execution.load_shot_contract(root)
    own_shot = next(s for s in contract['shots'] if s['id'] == shot_id)
    selected = execution.load_selected_attempts(root)
    producers = [u['shot_id'] for u in own_shot.get('upstream', [])]
    resolved = {producer: copy.deepcopy(selected[producer]) for producer in producers}
    material = {'shot_id': shot_id, 'provider': provider,
        'request_digest': execution.planned_request_digest(inputs, project_dir=root),
        'approval_plan_sha256': execution.approval_plan_digest(execution.load_shot_contract(root)),
        'lock_digest': locks, 'compiled_request_sha256': proof['compiled_sha256'],
        'preparation_review_id': inputs['preparation_review_id'],
        'resolved_upstream': resolved}
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
                                                         derived_unknown_id=scope.get('id'))
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
    if phase == 'repair':
        _validate_repair_evidence(root, material['shot_id'], scope.get('replaces_attempt_ids'))
    counts = root_attempt_counts(root, policy, exclude_scope_id=scope['id'])
    check_caps(policy, material['shot_id'], counts, phase)
    expected = _scope_record(policy, sha, decision_id, derivation_index=index, phase=phase,
                             replaces_attempt_ids=scope.get('replaces_attempt_ids'), **material)
    if expected != scope:
        raise AutonomyError('derived scope differs from authoritative current replay')
    if scope['provider'] == 'openart_cli':
        if _is_unknown(_openart_spec(policy)):
            _validate_policy_unknown(root, policy, sha, scope, inputs)
        else:
            _validate_policy_credit(root, policy, sha, scope, inputs)
    return policy, sha, decision_id


def _validate_repair_evidence(root, shot_id, replacement_ids):
    """A retry names actual motion attempts and independently verified failure facts."""
    from lib import production_execution as execution
    from lib.production_provenance import validate_attempt_provenance
    attempts = {a['attempt_id']: a for a in execution._attempts(root)}
    if (not isinstance(replacement_ids, list) or not replacement_ids or
            len(replacement_ids) != len(set(replacement_ids))):
        raise AutonomyError('repair requires unique actual replacement attempts')
    for aid in replacement_ids:
        request = attempts.get(aid)
        if not request or request['shot_id'] != shot_id or production_kind(request) != 'motion':
            raise AutonomyError('repair must name actual same-shot motion attempts')
        if request.get('provider') == 'openart_mcp':
            from lib import openart_mcp_jobs as mcp_jobs
            frozen = mcp_jobs.frozen_request(root, aid)
            original_scope = frozen['authority']['scope']
            if digest(original_scope) != frozen['authority']['scope_sha256'] or original_scope['provider'] != 'openart_mcp':
                raise AutonomyError('MCP repair original scope identity differs')
            state = execution.load_attempt_result(root, aid)
            directory = Path(root) / 'openart_mcp' / 'attempts' / aid
            request = {**request, 'scope': original_scope, 'story_revision': original_scope['story_revision']}
        else:
            directory = root / 'production_attempts' / aid
            state = execution._state(root, request)
        if state['status'] == 'generated':
            validate_attempt_provenance(root, aid, shot_id=shot_id,
                story_revision=request['story_revision'], expected_output=state['output'])
            schema = load_schema('shot_contract')
            validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/review'})
            reviews = list((directory / 'rejections').glob('*.json'))
            valid = False
            for path in reviews:
                review = _json(path)
                # Semantic repair authority needs a named failed critical predicate; cosmetic-only
                # or unknown-only findings never authorize a resubmit or alternate route.
                critical = any(isinstance(p, dict) and p.get('status') == 'fail' and
                               (p.get('name') in CRITICAL_PREDICATES or
                                p.get('severity', 'critical') == 'critical') and
                               not (p.get('name') in CRITICAL_PREDICATES and p.get('severity') == 'cosmetic')
                               for p in review.get('predicates') or [])
                if (not list(validator.iter_errors(review)) and review['status'] == 'fail' and critical and
                        review['subject_sha256'] == state['output']['sha256'] and
                        review['story_revision'] == request['story_revision']):
                    valid = True
            if not valid:
                raise AutonomyError('repair requires named failed critical review of actual generated output')
        elif request['scope']['provider'] == 'openart_mcp':
            if state['status'] != 'failed':
                raise AutonomyError('MCP original is pending or uncertain; never resubmit')
            terminal = mcp_jobs.terminal_failure_record(root, aid)
            review = _json(directory / 'rejection.json')
            if (set(review) != {'status', 'kind', 'attempt_id', 'terminal_failure_sha256', 'reviewer'} or
                    review['status'] != 'fail' or review['kind'] != 'generation_terminal_failure' or
                    review['attempt_id'] != aid or review['terminal_failure_sha256'] != terminal['terminal_failure_sha256'] or
                    not isinstance(review['reviewer'], str) or not review['reviewer'].strip()):
                raise AutonomyError('MCP terminal failure review differs from original provider evidence')
        elif request['scope']['provider'] == 'openart_cli':
            from lib import openart_jobs as jobs
            frozen = execution.load_openart_frozen(request)
            terminal = jobs.verify_terminal_failure(aid, frozen['profile'])
            review = _json(directory / 'rejection.json')
            if (set(review) != {'status', 'kind', 'attempt_id', 'terminal_failure_sha256', 'reviewer'} or
                    review['status'] != 'fail' or review['kind'] != 'generation_terminal_failure' or
                    review['attempt_id'] != aid or
                    review['terminal_failure_sha256'] != terminal['terminal_failure_sha256'] or
                    not isinstance(review['reviewer'], str) or not review['reviewer'].strip()):
                raise AutonomyError('repair terminal engineering review differs from actual failure')
        else:
            raise AutonomyError('repair requires verified generated-output or terminal-failure evidence')


def derive_scope(root, inputs, *, provider, observation=None, phase='first_pass', replaces_attempt_ids=None):
    """Append a normal exact one-attempt scope under the project lock, without CLI calls."""
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
        if any(s['id'] not in used_scopes and shot_id in s.get('requests', {}) and 'derived_from_policy' in s for s in scopes):
            raise AutonomyError('an undispatched derived scope already exists for this shot')
        check_caps(policy, shot_id, root_attempt_counts(root, policy), phase)
        index = sum('derived_from_policy' in s for s in scopes)
        if phase == 'repair':
            _validate_repair_evidence(root, shot_id, replaces_attempt_ids)
        elif any(a['shot_id'] == shot_id and production_kind(a) == 'motion' for a in execution._attempts(root)):
            raise AutonomyError('first_pass cannot authorize a corrective reroll')
        _append_route_decisions_locked(root, policy, sha, decision_id, shot_id, provider, inputs, phase=phase, replaces_attempt_ids=replaces_attempt_ids)
        material.pop('credit_authorization_sha256', None)
        material.pop('unknown_cost_authorization_sha256', None)
        scope = _scope_record(policy, sha, decision_id, derivation_index=index, phase=phase,
                              replaces_attempt_ids=replaces_attempt_ids, **material)
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
    from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS
    policy, sha, decision_id = require_active_policy(root)
    if approval_basis != {'kind': 'policy', 'policy_sha256': sha, 'decision_id': decision_id}:
        raise AutonomyError('approval_basis does not cite active policy')
    if stage in NON_PREAUTH_STAGES or stage not in policy['checkpoint_stages']:
        raise AutonomyError(f'stage {stage} not preauthorized')
    if preauth.get('artifact_path') != f'artifacts/{CANONICAL_STAGE_ARTIFACTS[stage]}.json':
        raise AutonomyError('preauth artifact is not the canonical stage artifact')
    current = hashlib.sha256(_inside(root, preauth['artifact_path']).read_bytes()).hexdigest()
    if current != preauth.get('artifact_sha256'):
        raise AutonomyError('artifact bytes changed since review')
    review_bytes = _inside(root, preauth['review_path']).read_bytes()
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
    policy, sha, decision_id = require_active_policy(root)
    shot_id = request['shot_id']
    if derived.get('policy_sha256') != sha or derived.get('decision_id') != decision_id:
        raise AutonomyError('historical attempt policy is inactive')
    if scope['approved_by'] != f'policy:{sha}' or scope['evidence'] != policy['evidence']:
        raise AutonomyError('historical policy approval authority differs')
    if scope['requests'] != {shot_id: request['request_sha256']} or scope['attempts_per_shot'] != {shot_id: 1}:
        raise AutonomyError('historical policy scope is not exact singleton')
    if scope['approval_plan_sha256'] != execution.approval_plan_digest(frozen_contract):
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
    preparation._validate_compiled(payload['compiled'], restored, native, profile, payload['source_packet'])
    preparation._schema('preparation_review', payload['review'])
    review = payload['review']
    if profile['source'] == 'real' and review['evidence_kind'] != 'reviewed':
        raise AutonomyError('historical fixture preparation cannot certify live provider')
    if review['review_id'] != derived['preparation_review_id'] or review['subject_sha256'] != digest(payload['compiled']) or review['status'] != 'pass' or {p['name'] for p in review['predicates']} != preparation.PREDICATES or len(review['predicates']) != len(preparation.PREDICATES) or any(p['status'] != 'pass' for p in review['predicates']):
        raise AutonomyError('historical named preparation is incomplete')
    current = current_projection(root, shot_id)
    frozen_shot = next(s for s in frozen_contract['shots'] if s['id'] == shot_id)
    own_shot = copy.deepcopy(frozen_shot)
    own_shot.pop('review', None)
    own_shot['upstream'] = [{k: v for k, v in u.items() if k not in {'attempt_id', 'output_sha256', 'outgoing_frame_sha256', 'review_sha256'}} for u in own_shot.get('upstream', [])]
    if own_shot != current['shot']:
        raise AutonomyError('historical own-shot semantics changed')
    locks = _root_lock_proof(root, shot_id, inputs=restored, native=native, profile=profile, historical=payload)
    if locks != derived['lock_digest'] or digest(payload['compiled']) != derived['compiled_request_sha256']:
        raise AutonomyError('historical actual lock/compilation proof differs')
    selected = _json(directory / 'selected_attempts.json')
    producers = [u['shot_id'] for u in frozen_shot.get('upstream', [])]
    material = {'shot_id': shot_id, 'provider': scope['provider'], 'request_digest': request['request_sha256'],
        'approval_plan_sha256': scope['approval_plan_sha256'], 'lock_digest': locks,
        'compiled_request_sha256': digest(payload['compiled']), 'preparation_review_id': review['review_id'],
        'resolved_upstream': {producer: selected[producer] for producer in producers}}
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
    expected = _scope_record(policy, sha, decision_id, derivation_index=derived['derivation_index'],
        phase=scope['phase'], replaces_attempt_ids=scope.get('replaces_attempt_ids'), **material)
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


def _append_route_decisions_locked(root, policy, sha, decision_id, shot_id, provider, inputs, *, phase='first_pass', replaces_attempt_ids=None):
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
        caps = policy['caps']
        remaining = {'total': caps['max_total_attempts'] - counts['total'] - 1,
                     'per_shot': caps['max_attempts_per_shot'] - counts['per_shot'].get(shot_id, 0) - 1,
                     'repair': caps['max_repair_attempts'] - counts['repair'] - int(phase == 'repair')}
        reason = ('Exact production-ready MCP route ' + inputs['model'] + '/' + inputs['mode']
                  + '; phase ' + phase + '; replaces ' + json.dumps(replaces_attempt_ids or [])
                  + '; unknown cost with no enforceable credit ceiling; remaining attempt caps '
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


def _validate_request_delta(root, policy, shot_id, inputs, provider):
    """Compare rooted actual inputs with exact retained layout and approved flex."""
    from lib import production_execution as execution
    if not _intent_allows(policy, shot_id, provider, inputs.get('model') if provider in {'openart_cli', 'openart_mcp'} else None):
        raise AutonomyError('request route violates approved per-shot model selection intent')
    retained = retained_baselines(root, policy)[shot_id]
    template = retained['planned_request_template']
    baseline = execution._clean(template['inputs'])
    actual = execution._clean(inputs)
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
    plumbing = {'output_path', 'cli_session_id', 'timeout_seconds', 'cwd', 'allow_unknown_cost',
                'native_dry_run_receipt_id', 'native_dry_run_receipt_sha256'}
    assets = iter(template['static_input_assets'])
    selected = execution.load_selected_attempts(root)
    dynamic = []
    def resolve(value, pointer='', key=None):
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
    for pointer, binding, expected_sha in dynamic:
        value = bound_actual
        for part in pointer.lstrip('/').split('/'):
            value = value[int(part)] if isinstance(value, list) else value.get(part)
        if value != {'sha256': expected_sha}:
            raise AutonomyError('exact upstream pointer/layout/order changed')
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
    for key in routing | plumbing | {'prompt', 'duration', 'resolution'}:
        expected.pop(key, None)
        bound_actual.pop(key, None)
    if execution.explicit_motion_duration(inputs) != current_projection(root, shot_id)['shot']['duration_seconds']:
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
    # Only the established duration/resolution flex aliases are normalized here.
    # Exact form preparation still checks all provider-native values; other
    # controls remain present for the retained-template equality below.
    for value in (expected, bound_actual):
        if isinstance(value.get('native_params'), dict):
            value['native_params'].pop('duration', None)
            value['native_params'].pop('resolution', None)
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
