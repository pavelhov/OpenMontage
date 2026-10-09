"""Durable exact creator repair intent over canonical scopes and original jobs.

This is not a dispatch ledger. Singleton scope bindings retain planned work;
submitted, uncertain, completed and failed states come from production_execution
and openart_mcp_jobs. No counters, provider submissions or host tools are copied.
"""
from __future__ import annotations

import copy
import datetime
import json
import re
from pathlib import Path

from lib import production_execution as execution
from lib.shot_contract import file_sha256

DIRECTORY = 'production_repair_batches'


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise ValueError('repair batch/item ID must be an opaque safe ID')
    return value


def _root(project_dir):
    root = Path(project_dir).expanduser().resolve()
    marker = execution._read(root / 'project.json')
    # Marker may carry other governance metadata; only these fields bind.
    if (marker.get('governance') or {}).get('version') != '1.0' or (marker.get('governance') or {}).get('mode') != 'strict':
        raise ValueError('creator repair batches require a strict project')
    return root, marker


def _directory(root, batch_id):
    return root / DIRECTORY / _id(batch_id)


def _binding(root, value):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
        raise ValueError('repair binding must be exactly {path, sha256}')
    path = execution._inside(value['path'], root)
    if not path.is_file() or file_sha256(path) != value['sha256']:
        raise ValueError('repair binding bytes missing or changed')
    return {'path': str(path.relative_to(root)), 'sha256': value['sha256']}


def _successor_sources(root, accepted_cut, batch):
    """Prove this batch's source/replacement ancestry in an accepted successor.
    No global acceptance closes another cut's unrelated or future repair intent.
    """
    source = execution._read(root / _binding(root, batch['cut'])['path'])
    successor = execution._read(root / _binding(root, accepted_cut)['path'])
    if (successor['project_id'] != source['project_id'] or successor['story_revision'] != source['story_revision']
            or successor['sequence'] <= source['sequence']):
        return False
    clips = {c['shot_id']: c for c in successor['clips']}
    items = {i['shot_id']: i for i in batch['items']}
    attempts = execution._attempts(root)
    scopes = execution._read(root / 'production_scopes.json')['scopes']
    for old in source['clips']:
        clip = clips.get(old['shot_id'])
        if clip is None:
            return False
        if clip['attempt_id'] == old['attempt_id'] and clip['output'] == old['output']:
            continue
        item = items.get(old['shot_id'])
        if item is None:
            return False
        actual = [a for a in attempts if a['attempt_id'] == clip['attempt_id'] and a['shot_id'] == old['shot_id']]
        if len(actual) != 1:
            return False
        scope = next((s for s in scopes if s['id'] == actual[0]['scope_id']), {})
        if scope.get('creator_repair') != {'batch_id': batch['batch_id'], 'item_id': item['item_id'],
                                         'intent_sha256': batch['intent_sha256']}:
            return False
        try:
            _source(root, old['shot_id'], clip['attempt_id'], batch['story_revision'], expected_output=clip['output'])
        except (ValueError, OSError):
            return False
    return True


def _acceptances(root, cut, *, batch=None):
    matches = []
    for path in (root / 'production_first_cut_acceptances').glob('*.json'):
        value = execution._read(path)
        if value.get('first_cut') == cut or (batch is not None and _successor_sources(root, value['first_cut'], batch)):
            # An acceptance of this cut closes its old intent even after a
            # successor export. Its retained bytes/evidence must remain exact.
            if file_sha256(path) != path.stem:
                raise ValueError('first-cut acceptance bytes changed')
            _binding(root, value['evidence'])
            matches.append(path.name)
    return sorted(matches)


def _load(root, batch_id):
    path = _directory(root, batch_id) / 'intent.json'
    batch = execution._read(path)
    sha = batch.get('intent_sha256')
    if sha != execution._digest({k: v for k, v in batch.items() if k != 'intent_sha256'}):
        raise ValueError('creator repair intent bytes changed')
    if batch.get('batch_id') != batch_id:
        raise ValueError('creator repair batch identity differs')
    return batch


def _original_inputs(root, request):
    if request.get('provider') == 'openart_mcp':
        from lib import openart_mcp_jobs as jobs
        return jobs.frozen_request(root, request['attempt_id'])['generation_inputs']
    inputs = copy.deepcopy(request['submitted_inputs'])
    records = iter(request['input_assets'])
    def restore(role, path):
        item = next(records)
        if item['role'] != role or str(path) != item['path']:
            raise ValueError('original repair source input layout differs')
        return item['original_path']
    return execution._paths(inputs, root, restore)


def _source(root, shot_id, attempt_id, revision, *, expected_output=None):
    from lib.production_provenance import validate_creator_repair_source_provenance
    rows = [row for row in execution._attempts(root) if row['attempt_id'] == attempt_id]
    if len(rows) != 1 or rows[0]['shot_id'] != shot_id:
        raise ValueError('creator repair source must be the actual same-shot attempt')
    row = rows[0]
    state = execution.load_attempt_result(root, attempt_id)
    if state.get('status') != 'generated':
        raise ValueError('creator repair original is pending, uncertain or lacks footage; reconcile the original')
    expected = expected_output or state['output']
    expected = {**expected, 'path': str(execution._inside(expected['path'], root))}
    validate_creator_repair_source_provenance(root, attempt_id, shot_id=shot_id, story_revision=revision, expected_output=expected)
    return row, state


def _has_upstream(value):
    if isinstance(value, dict):
        return '$upstream' in value or any(_has_upstream(v) for v in value.values())
    return isinstance(value, list) and any(_has_upstream(v) for v in value)


def _item_inputs(root, item):
    """Resolve only the existing canonical $upstream language, then reprove its
    exact canonical template digest. Static bytes remain frozen in that template.
    """
    selected = execution.load_selected_attempts(root)
    def resolve(value, key=None):
        if isinstance(value, dict) and '$upstream' in value:
            if key not in execution.INPUT_PATH_KEYS:
                raise ValueError('dynamic upstream must be a canonical asset input')
            binding = execution._upstream_binding(value)
            source = selected.get(binding['shot_id'], {}).get(binding['role'], {})
            bound = _binding(root, source)
            return str(root / bound['path'])
        if isinstance(value, dict):
            return {k: resolve(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, key) for v in value]
        return value
    inputs = resolve(copy.deepcopy(item['inputs']))
    expected = execution.approved_request_digest(item['request_template'], project_dir=root)
    if execution.planned_request_digest(inputs, project_dir=root) != expected:
        raise ValueError('creator repair static/dynamic request differs from canonical template')
    return inputs


def record_creator_repair_batch(project_dir, *, batch_id, cut, items, evidence):
    """Retain creator-selected exact items; returns status, grants no new route.

    items: [{item_id, shot_id, source_attempt_id, selected_route:
             {provider,tool,model,mode}, changes:str, inputs:exact dict}].
    Authority comes later from an existing approved singleton repair scope or
    canonical Auto-continue derivation. Duplicate batch IDs must match exactly.
    """
    from lib import production_draft as draft, episode_production_controls as controls
    from lib.video_model_selection import validate_creator_repair_decision, request_delta
    root, marker = _root(project_dir)
    _id(batch_id)
    if not isinstance(items, list) or not items:
        raise ValueError('creator repair batch requires selected items')
    evidence = controls._evidence(root, evidence)
    cut = _binding(root, cut)
    if (root / cut['path']).parent != root / 'production_first_cuts':
        raise ValueError('creator repair must bind a canonical first-cut record')
    normalized = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'item_id', 'shot_id', 'source_attempt_id', 'selected_route', 'changes', 'inputs'}:
            raise ValueError('creator repair item fields differ from the exact item contract')
        _id(item['item_id'])
        if not isinstance(item['inputs'], dict) or not item['inputs']:
            raise ValueError('creator repair requires exact proposed inputs')
        validate_creator_repair_decision(item, proposed_request=item['inputs'], intent=item['inputs'].get('model_selection_intent'))
        inputs = copy.deepcopy(item['inputs'])
        inputs['project_dir'] = str(root)
        inputs['governance'] = {'shot_id': item['shot_id']}
        normalized.append({**copy.deepcopy(item), 'inputs': inputs,
                           'request_template': execution.planned_request_template(inputs, project_dir=root),
                           'request_sha256': None if _has_upstream(inputs) else execution.planned_request_digest(inputs, project_dir=root)})
    if len({i['item_id'] for i in normalized}) != len(items) or len({i['shot_id'] for i in normalized}) != len(items):
        raise ValueError('creator repair batch items and shots must be unique')
    with execution._lock(root):
        path = _directory(root, batch_id) / 'intent.json'
        if path.exists():
            old = _load(root, batch_id)
            # Repeated human commands preserve their retained identity, including
            # after acceptance; no new scope or attempt is created on replay.
            comparison = [{k: v for k, v in i.items() if k not in {'source_output', 'request_delta'}} for i in old['items']]
            if old['cut'] != cut or old['evidence'] != evidence or comparison != normalized:
                raise ValueError('repair batch ID already names a different exact intent')
            return repair_batch_status(root, batch_id)
        if draft.first_cut_status(root, cut)['status'] != 'current':
            raise ValueError('new creator repair must bind the current cut and disclosed findings')
        record = execution._read(root / cut['path'])
        if record.get('export') is None:
            raise ValueError('creator repair requires a delivered first-cut export')
        clips = {clip['shot_id']: clip for clip in record['clips']}
        for item in normalized:
            clip = clips.get(item['shot_id'])
            if clip is None or clip['attempt_id'] != item['source_attempt_id']:
                raise ValueError('creator repair source differs from the selected cut clip')
            source, state = _source(root, item['shot_id'], item['source_attempt_id'], marker['story_revision'],
                                    expected_output=clip['output'])
            item['source_output'] = copy.deepcopy(clip['output'])
            item['request_delta'] = request_delta(_original_inputs(root, source), item['inputs'])
        batch = {'version': '1.0', 'kind': 'creator_repair_batch', 'batch_id': batch_id,
                 'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
                 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'cut': cut, 'export': record['export'], 'evidence': evidence,
                 'accepted_before': _acceptances(root, cut), 'items': normalized}
        batch['intent_sha256'] = execution._digest(batch)
        path.parent.mkdir(parents=True, exist_ok=True)
        execution._write_new(path, batch)
    return repair_batch_status(root, batch_id)


def creator_repair_binding(project_dir, batch_id, item_id):
    root, _ = _root(project_dir)
    batch = _load(root, _id(batch_id))
    if item_id not in {i['item_id'] for i in batch['items']}:
        raise ValueError('creator repair item is not selected in this batch')
    return {'batch_id': batch_id, 'item_id': item_id, 'intent_sha256': batch['intent_sha256']}


def validate_creator_repair_intent(project_dir, binding, *, shot_id, provider, model,
                                   replaces_attempt_ids, inputs=None, historical=False):
    """Canonical dispatch replay of the exact item. Never a generic cached grant."""
    from lib import episode_production_controls as controls
    from lib.video_model_selection import validate_creator_repair_decision
    root, marker = _root(project_dir)
    if not isinstance(binding, dict) or set(binding) != {'batch_id', 'item_id', 'intent_sha256'}:
        raise ValueError('creator repair requires its retained batch/item binding')
    batch = _load(root, _id(binding['batch_id']))
    matches = [i for i in batch['items'] if i['item_id'] == binding['item_id']]
    if len(matches) != 1 or binding['intent_sha256'] != batch['intent_sha256']:
        raise ValueError('creator repair binding differs from retained intent')
    item = matches[0]
    route = item['selected_route']
    if (batch['project_id'] != marker['project_id'] or batch['story_revision'] != marker['story_revision']
            or item['shot_id'] != shot_id or route['provider'] != provider
            or controls._media_model(provider, route['model']) != controls._media_model(provider, model)
            or replaces_attempt_ids != [item['source_attempt_id']]):
        raise ValueError('creator repair scope/source/route differs from the exact selected item')
    controls._evidence(root, batch['evidence'])
    _binding(root, batch['cut']); _binding(root, batch['export']); _binding(root, item['source_output'])
    if not historical:
        if set(_acceptances(root, batch['cut'], batch=batch)) - set(batch['accepted_before']):
            raise ValueError('creator accepted this cut; pending creative repair intent is closed')
        _source(root, shot_id, item['source_attempt_id'], marker['story_revision'],
                expected_output=item['source_output'])
    if inputs is not None:
        validate_creator_repair_decision(item, proposed_request=inputs, intent=inputs.get('model_selection_intent'))
        if execution.planned_request_digest(inputs, project_dir=root) != execution.approved_request_digest(item['request_template'], project_dir=root):
            raise ValueError('creator repair request differs from its exact selected item')
    return copy.deepcopy(item)


def _scope_binding(root, batch_id, item_id):
    path = _directory(root, batch_id) / (_id(item_id) + '.scope.json')
    return execution._read(path) if path.exists() else None


def _scope(root, scope_id):
    matches = [s for s in execution._read(root / 'production_scopes.json')['scopes'] if s['id'] == scope_id]
    if len(matches) != 1:
        raise ValueError('creator repair needs one retained exact approved scope')
    return matches[0]


def prepare_repair_item(project_dir, batch_id, item_id, *, scope_id=None, observation=None):
    """Bind one exact existing scope, or derive through existing active policy.
    Replays return the original job/state. Preparation does not submit a call.
    """
    from lib import production_autonomy as autonomy
    root, _ = _root(project_dir)
    binding = creator_repair_binding(root, batch_id, item_id)
    item = next(i for i in _load(root, batch_id)['items'] if i['item_id'] == item_id)
    retained = _scope_binding(root, batch_id, item_id)
    if retained is not None:
        if scope_id is not None and scope_id != retained['scope_id']:
            raise ValueError('creator repair item already bound to another scope')
        return next(i for i in repair_batch_status(root, batch_id)['items'] if i['item_id'] == item_id)
    current = next(i for i in repair_batch_status(root, batch_id)['items'] if i['item_id'] == item_id)
    if current['blocked_by'] and scope_id is None:
        return current
    inputs = _item_inputs(root, item)
    validate_creator_repair_intent(root, binding, shot_id=item['shot_id'], provider=item['selected_route']['provider'],
        model=item['selected_route']['model'], replaces_attempt_ids=[item['source_attempt_id']], inputs=inputs)
    if scope_id is None:
        # Recover an interrupted derivation before the per-item binding write.
        # The exact retained canonical scope, not a local submission flag, wins.
        path = root / 'production_scopes.json'
        scopes = execution._read(path)['scopes'] if path.exists() else []
        matches = [s for s in scopes if s.get('creator_repair') == binding]
        if len(matches) > 1:
            raise ValueError('creator repair has multiple canonical scopes')
        if matches:
            scope_id = matches[0]['id']
    if scope_id is None:
        scope = autonomy.derive_scope(root, inputs, provider=item['selected_route']['provider'], observation=observation,
            phase='repair', replaces_attempt_ids=[item['source_attempt_id']], repair_basis='creator_batch', creator_repair=binding)
        scope_id = scope['id']
    with execution._lock(root):
        scope = copy.deepcopy(_scope(root, scope_id))
        if (scope.get('status') != 'approved' or not scope.get('approved_by') or scope.get('phase') != 'repair'
                or scope.get('provider') != item['selected_route']['provider']
                or (scope.get('requests') != {item['shot_id']: item['request_template']}
                    and scope.get('requests') != {item['shot_id']: execution.approved_request_digest(item['request_template'], project_dir=root)})
                or scope.get('attempts_per_shot') != {item['shot_id']: 1}
                or scope.get('replaces_attempt_ids') != [item['source_attempt_id']]):
            raise ValueError('creator repair scope must independently authorize this exact singleton request/source/route')
        evidence = scope.get('evidence') or {}
        _binding(root, {'path': evidence.get('path'), 'sha256': evidence.get('sha256')})
        marker = execution._read(root / 'project.json')
        if scope.get('project_id') != marker['project_id'] or scope.get('story_revision') != marker['story_revision']:
            raise ValueError('creator repair scope has stale project/story authority')
        if 'derived_from_policy' not in scope and scope.get('creator_repair') != binding:
            # The exact scope already grants generation authority. Retain this
            # item exception in the canonical scope, without editing its request,
            # evidence, allowance, account or billing fields.
            if _scope_has_original(root, scope_id):
                raise ValueError('creator repair cannot claim an already consumed scope')
            scope['repair_basis'] = 'creator_batch'; scope['creator_repair'] = binding
            data = execution._read(root / 'production_scopes.json')
            data['scopes'] = [scope if s['id'] == scope_id else s for s in data['scopes']]
            temp = root / 'production_scopes.json.creator.tmp'
            temp.write_text(json.dumps(data, indent=2)); temp.replace(root / 'production_scopes.json')
        if scope.get('creator_repair') != binding:
            raise ValueError('creator repair scope item binding differs')
        path = _directory(root, batch_id) / (item_id + '.scope.json')
        value = {'scope_id': scope_id, 'scope_sha256': execution._digest(scope)}
        if path.exists() and execution._read(path) != value:
            raise ValueError('creator repair scope binding changed')
        if not path.exists(): execution._write_new(path, value)
    return next(i for i in repair_batch_status(root, batch_id)['items'] if i['item_id'] == item_id)


def repair_batch_status(project_dir, batch_id):
    """Read-only projection of actual canonical originals; no invented submission."""
    root, marker = _root(project_dir)
    batch = _load(root, _id(batch_id))
    closed = bool(set(_acceptances(root, batch['cut'], batch=batch)) - set(batch['accepted_before']))
    attempts = execution._attempts(root)
    items = []
    for item in batch['items']:
        retained = _scope_binding(root, batch_id, item['item_id'])
        rows = [a for a in attempts if retained and a['scope_id'] == retained['scope_id']]
        if len(rows) > 1:
            raise ValueError('creator repair singleton scope contains multiple originals')
        row = {'item_id': item['item_id'], 'shot_id': item['shot_id'], 'source_attempt_id': item['source_attempt_id'],
               'selected_route': item['selected_route'], 'changes': item['changes'], 'scope_id': None,
               'attempt_id': None, 'state': 'planned', 'original_status': None, 'output': None}
        if retained:
            scope = _scope(root, retained['scope_id'])
            if execution._digest(scope) != retained['scope_sha256']:
                raise ValueError('retained creator repair scope changed')
            row['scope_id'] = scope['id']
        if rows:
            attempt = rows[0]
            original = execution.load_attempt_result(root, attempt['attempt_id'])
            state = original['status']
            row.update(attempt_id=attempt['attempt_id'], original_status=state, output=original.get('output'))
            if state == 'generated':
                row['state'] = 'completed'
            elif state == 'failed':
                row['state'] = 'failed'
            elif state == 'prepared':
                row['state'] = 'planned'
            elif state == 'uncertain':
                row['state'] = 'uncertain'
            else:
                row['state'] = 'submitted'
        row['closed'] = closed and row['state'] == 'planned'
        items.append(row)
    selected = {i['shot_id'] for i in batch['items']}
    affected = set()
    contract = execution.load_shot_contract(root)
    selections = execution.load_selected_attempts(root)
    by_shot = {i['shot_id']: i for i in items}
    shots = {s['id']: s for s in contract['shots']}
    for row in items:
        row['blocked_by'] = [u['shot_id'] for u in shots[row['shot_id']].get('upstream', [])
            if u['shot_id'] in by_shot and (not by_shot[u['shot_id']]['attempt_id']
                or by_shot[u['shot_id']]['state'] != 'completed'
                or selections.get(u['shot_id'], {}).get('attempt_id') != by_shot[u['shot_id']]['attempt_id'])]
        if row['blocked_by'] or row['state'] != 'planned':
            continue
        # Fresh canonical upstream validation remains the final gate; the batch
        # does not turn its completed candidate into a selected continuity source.
        if any(u['shot_id'] in by_shot for u in shots[row['shot_id']].get('upstream', [])):
            from lib.shot_contract import validate_shot_contract
            checked = validate_shot_contract(contract, project_dir=root, shot_id=row['shot_id'],
                story_revision=marker['story_revision'], selected_upstream=selections)
            if not checked['eligible']:
                row['blocked_by'] = [u['shot_id'] for u in shots[row['shot_id']].get('upstream', []) if u['shot_id'] in by_shot]
    changed = selected
    while changed:
        following = {s['id'] for s in contract['shots'] if any(u['shot_id'] in changed for u in s.get('upstream', []))} - affected - selected
        affected.update(following); changed = following
    return {'batch_id': batch_id, 'intent_sha256': batch['intent_sha256'], 'cut': batch['cut'],
            'closed': closed, 'items': items, 'affected_dependency_shots': sorted(affected)}


def resume_repair_batch(project_dir, batch_id, *, tools=None, observation=None):
    """Run only unsubmitted items through canonical tools; emit MCP handoffs.

    tools maps (provider, tool name) -> BaseTool for exact native routes. Existing
    provider -> BaseTool entries remain supported when the exact key is absent;
    an invalid exact entry never falls back to the provider entry. Host connector
    envelopes are returned to the agent, never invoked here. Submitted/uncertain
    originals are returned for status/collection with original attempt identities.
    """
    from lib import openart_mcp_jobs as jobs
    root, _ = _root(project_dir)
    tools = tools or {}
    actions, errors = [], []
    batch = _load(root, _id(batch_id))
    for item in batch['items']:
        try:
            state = next(i for i in repair_batch_status(root, batch_id)['items'] if i['item_id'] == item['item_id'])
            if state['state'] != 'planned' or state['closed'] or state['blocked_by']:
                continue
            if state['scope_id'] is None:
                state = prepare_repair_item(root, batch_id, item['item_id'], observation=observation)
            if state['blocked_by']:
                continue
            inputs = _item_inputs(root, item); inputs['governance']['scope_id'] = state['scope_id']
            provider = item['selected_route']['provider']
            if provider == 'openart_mcp':
                aid = state['attempt_id'] or ('repair-' + execution._digest([batch_id, item['item_id']])[:32])
                if state['attempt_id'] is None:
                    jobs.prepare(root, attempt_id=aid, generation_inputs=inputs, authority_fn=execution.prepare_openart_mcp_handoff)
                envelope = jobs.begin(root, aid, authority_fn=execution.prepare_openart_mcp_handoff)
                actions.append({'item_id': item['item_id'], 'attempt_id': aid, 'operation': 'host_handoff', 'envelope': envelope})
            else:
                key = (provider, item['selected_route']['tool'])
                tool = tools[key] if key in tools else tools.get(provider)
                if tool is None or not ((tool.name == item['selected_route']['tool'] and tool.provider == provider)
                        or (tool.name == 'video_selector' and tool.provider == 'selector'
                            and inputs.get('preferred_provider') == provider and inputs.get('allowed_providers') == [provider])):
                    raise ValueError('exact canonical tool required to execute this repair item')
                result = tool.execute(inputs)
                actions.append({'item_id': item['item_id'], 'operation': 'executed', 'success': result.success,
                                'attempt_id': (result.data or {}).get('production_attempt_id'), 'error': result.error})
        except Exception as exc:
            errors.append({'item_id': item['item_id'], 'error': str(exc)})
    return {**repair_batch_status(root, batch_id), 'actions': actions, 'errors': errors}


def creator_repair_candidates(project_dir):
    """Latest completed creator replacements for preview, never strict selection.
    Accepted-cut intents stop influencing successor previews; submitted originals
    stay collectible. A later explicit batch carries the acceptance in its basis.
    """
    from lib.production_provenance import validate_attempt_provenance
    if not (Path(project_dir).expanduser().resolve() / DIRECTORY).exists():
        return {}
    root, marker = _root(project_dir)
    candidates = {}
    batches = []
    for path in (root / DIRECTORY).glob('*/intent.json'):
        batch = _load(root, path.parent.name)
        if batch['story_revision'] == marker['story_revision']:
            batches.append(batch)
    for batch in sorted(batches, key=lambda b: b['created_at']):
        status = repair_batch_status(root, batch['batch_id'])
        if status['closed']:
            continue
        for row in status['items']:
            if row['state'] != 'completed':
                continue
            try:
                validate_attempt_provenance(root, row['attempt_id'], shot_id=row['shot_id'],
                    story_revision=marker['story_revision'], expected_output=row['output'])
            except (ValueError, OSError):
                continue
            candidates[row['shot_id']] = row['attempt_id']
    return candidates


def suppressed_creator_repair_attempts(project_dir):
    """Original jobs remain collectible after acceptance but do not silently
    substitute footage into the accepted cut. Later explicit batches are distinct.
    """
    if not (Path(project_dir).expanduser().resolve() / DIRECTORY).exists():
        return set()
    root, marker = _root(project_dir)
    suppressed = set()
    accepted = {clip['attempt_id'] for clip in accepted_first_cut_candidates(root).values()}
    for path in (root / DIRECTORY).glob('*/intent.json'):
        batch = _load(root, path.parent.name)
        if batch['story_revision'] != marker['story_revision']:
            continue
        status = repair_batch_status(root, batch['batch_id'])
        if status['closed']:
            suppressed.update(i['attempt_id'] for i in status['items'] if i['attempt_id'] and i['attempt_id'] not in accepted)
    return suppressed


def accepted_first_cut_candidates(project_dir):
    """Exact latest accepted clips remain the preview baseline, never selection.
    Consumers reprove current candidate/source provenance and current bytes.
    """
    root, marker = _root(project_dir)
    records = []
    for path in (root / 'production_first_cut_acceptances').glob('*.json'):
        value = execution._read(path)
        if value.get('project_id') != marker['project_id'] or value.get('story_revision') != marker['story_revision']:
            continue
        if file_sha256(path) != path.stem:
            raise ValueError('first-cut acceptance bytes changed')
        _binding(root, value['evidence'])
        cut = execution._read(root / _binding(root, value['first_cut'])['path'])
        if (cut['export'] != value['export'] or {c['shot_id']: c['output']['sha256'] for c in cut['clips']} != value['sources']):
            raise ValueError('first-cut acceptance source/export binding differs')
        _binding(root, cut['export'])
        records.append(cut)
    if not records:
        return {}
    current = max(records, key=lambda cut: cut['sequence'])
    return {clip['shot_id']: {'attempt_id': clip['attempt_id'], 'output': clip['output']} for clip in current['clips']}


def _scope_has_original(root, scope_id):
    if any(a['scope_id'] == scope_id for a in execution._attempts(root)):
        return True
    # Private reservations can precede publication of the public journal. Failure
    # to read them never proves that no request was submitted.
    try:
        from lib.provider_credit_ledger import read_existing_snapshot
        from lib import openart_dispatch as dispatch
        snapshot = read_existing_snapshot()
        for row in list(snapshot.get('reservations', [])) + list(snapshot.get('unpriced_reservations', [])):
            private = json.loads(row['binding_json'])
            if Path(private['project_root']).resolve() != root:
                continue
            manifest, _, _ = dispatch._manifest(row['attempt_id'])
            if manifest['journal_records']['request.json']['scope_id'] == scope_id:
                return True
    except Exception:
        return True
    return False


def closed_unsubmitted_creator_scope(project_dir, scope):
    """Only a closed exact item with no public or private original releases its
    unused scope hold. Retain scope/history; real and uncertain calls still count.
    """
    if scope.get('repair_basis') != 'creator_batch' or not isinstance(scope.get('creator_repair'), dict):
        return False
    root, _ = _root(project_dir)
    binding = scope['creator_repair']
    batch = _load(root, _id(binding['batch_id']))
    if not set(_acceptances(root, batch['cut'], batch=batch)) - set(batch['accepted_before']):
        return False
    matches = [i for i in batch['items'] if i['item_id'] == binding.get('item_id')]
    if len(matches) != 1 or binding.get('intent_sha256') != batch['intent_sha256']:
        raise ValueError('closed creator repair scope has changed item intent')
    item = matches[0]
    retained = _scope_binding(root, batch['batch_id'], item['item_id'])
    if retained is not None and (retained['scope_id'] != scope['id'] or retained['scope_sha256'] != execution._digest(scope)):
        raise ValueError('closed creator repair retained scope changed')
    if (scope.get('phase') != 'repair' or scope.get('provider') != item['selected_route']['provider']
            or scope.get('replaces_attempt_ids') != [item['source_attempt_id']]
            or scope.get('attempts_per_shot') != {item['shot_id']: 1}
            or (scope.get('requests') != {item['shot_id']: item['request_template']}
                and scope.get('requests') != {item['shot_id']: execution.approved_request_digest(item['request_template'],
                    project_dir=root, selected_attempts=scope.get('derived_from_policy', {}).get('resolved_upstream'))})):
        raise ValueError('closed creator repair scope no longer matches exact selected item')
    return not _scope_has_original(root, scope['id'])


def creator_repair_source_candidates(project_dir):
    """Exact disclosed-cut sources retained during partial selected repairs.
    Consumers must use canonical source-only provenance and disclose changed
    continuity; these sources are never new strict selections or dispatch inputs.
    """
    if not (Path(project_dir).expanduser().resolve() / DIRECTORY).exists():
        return {}
    root, marker = _root(project_dir)
    batches = [_load(root, p.parent.name) for p in (root / DIRECTORY).glob('*/intent.json')]
    sources = {}
    for batch in sorted(batches, key=lambda b: b['created_at']):
        if batch['story_revision'] != marker['story_revision']:
            continue
        cut = execution._read(root / _binding(root, batch['cut'])['path'])
        for clip in cut['clips']:
            sources[clip['shot_id']] = {'attempt_id': clip['attempt_id'], 'output': clip['output']}
    sources.update(accepted_first_cut_candidates(root))
    return sources
