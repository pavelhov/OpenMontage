"""Canonical board-image admission and original host-result import.

Native images are agent-invoked host tools. ``begin_native_image`` emits one
handoff; this module never invokes or queries the host. Local call IDs identify
our journal, not a provider job. Grok images retain their existing governed
scope/session journal; the same approved image allowance counts both routes.
"""
from __future__ import annotations

import base64
import copy
import datetime
import hashlib
import io
import json
import os
import re
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.production_request import digest
from lib.shot_contract import file_sha256

ARTIFACT = Path('artifacts/production_image_calls.json')
NATIVE_ROUTE = 'native_imagegen'
NATIVE_TOOL = 'image_gen.imagegen'
PENDING = {'reserved', 'submitted', 'uncertain', 'returned'}


def _ex():
    from lib import production_execution as execution
    return execution


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read(path):
    return json.loads(Path(path).read_text())


def _root(project_dir):
    root = Path(project_dir).expanduser().resolve()
    marker = _read(root / 'project.json')
    if (marker.get('governance', {}).get('version') != '1.0'
            or marker.get('governance', {}).get('mode') != 'strict'):
        raise ValueError('image lifecycle requires a strict governed project')
    if not marker.get('project_id') or not marker.get('story_revision'):
        raise ValueError('project lacks current story binding')
    return root, marker


def _bound_file(root, item, label):
    if not isinstance(item, dict) or not item.get('path') or not item.get('sha256'):
        raise ValueError(label + ' requires retained path and hash')
    path = _ex()._inside(item['path'], root)
    if not path.is_file() or file_sha256(path) != item['sha256']:
        raise ValueError(label + ' bytes changed or missing')
    return path


def _canonical_file(root, item, label):
    path = _bound_file(root, item, label)
    return {**copy.deepcopy(item), 'path':str(path.relative_to(root))}


def _validate_review(review, sha256, revision):
    schema = load_schema('shot_contract')
    Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/review'}).validate(review)
    if (review['subject_sha256'] != sha256 or review['story_revision'] != revision
            or review['status'] != 'pass' or any(p.get('severity') == 'critical'
                and p['status'] != 'pass' for p in review['predicates'])):
        raise ValueError('reuse needs a passing current-byte applicable review')


def validate_image_reuse(project_dir, reference, *, role):
    """Validate exact bytes, role/story applicability and retained observation.

    This reads evidence and consumes no generation. It never creates a review.
    """
    root, marker = _root(project_dir)
    path = _bound_file(root, reference, 'reference')
    applicability = reference.get('applicability') or {}
    if (applicability.get('story_revision') != marker['story_revision']
            or role not in applicability.get('roles', [])):
        raise ValueError('reference reuse lacks current story/role applicability')
    _validate_review(reference.get('review'), reference['sha256'], marker['story_revision'])
    return {'id':reference['id'], 'role':role, 'path':str(path.relative_to(root)),
            'sha256':reference['sha256'], 'review_sha256':digest(reference['review']),
            'story_revision':marker['story_revision']}


def build_preboard_packet(project_dir, *, story, approval, references, board_slots):
    """Prepare approved current story and references before boards exist.

    ``approval`` is retained human evidence {path, sha256, approved_by} for the
    story bytes. Slot declarations are explicit unresolved outputs, not motion
    eligibility. Exact generation authority is separately checked at reserve.
    """
    root, marker = _root(project_dir)
    if not approval.get('approved_by'):
        raise ValueError('story needs explicit approval provenance')
    packet = {'version':'1.0', 'kind':'pre_board', 'project_id':marker['project_id'],
        'story_revision':marker['story_revision'], 'project_dir':str(root),
        'story':_canonical_file(root, story, 'story'),
        'approval':_canonical_file(root, approval, 'story approval'),
        'references':[_canonical_file(root, ref, 'reference') for ref in references],
        'board_slots':copy.deepcopy(board_slots), 'prepared_at':_now()}
    packet['packet_sha256'] = digest(packet)
    _validate_packet(root, packet)
    return packet


def _validate_packet(root, packet):
    _, marker = _root(root)
    schema = load_schema('production_image_calls')
    Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/packet'}).validate(packet)
    if (packet['project_id'] != marker['project_id'] or packet['story_revision'] != marker['story_revision']
            or Path(packet['project_dir']).resolve() != root):
        raise ValueError('pre-board packet has stale project/story binding')
    if packet['packet_sha256'] != digest({k:v for k,v in packet.items() if k != 'packet_sha256'}):
        raise ValueError('pre-board packet digest changed')
    _bound_file(root, packet['story'], 'story')
    _bound_file(root, packet['approval'], 'story approval')
    ids = [ref['id'] for ref in packet['references']]
    slots = [slot['id'] for slot in packet['board_slots']]
    if len(set(ids)) != len(ids) or len(set(slots)) != len(slots):
        raise ValueError('duplicate reference or board slot')
    for ref in packet['references']:
        _bound_file(root, ref, 'reference')
        if ref['applicability']['story_revision'] != marker['story_revision']:
            raise ValueError('reference applicability has stale story binding')


def _native_arguments(packet, arguments):
    if (not isinstance(arguments, dict) or set(arguments) - {'prompt','referenced_image_paths','transparent_background'}
            or not isinstance(arguments.get('prompt'), str) or not arguments['prompt'].strip()):
        raise ValueError('unsupported native image arguments; one prompt per host call')
    if 'transparent_background' in arguments and type(arguments['transparent_background']) is not bool:
        raise ValueError('native transparent_background must be boolean')
    root = Path(packet['project_dir']).resolve()
    references = {_ex()._inside(ref['path'],root):ref for ref in packet['references']}
    native = copy.deepcopy(arguments)
    if 'referenced_image_paths' in arguments:
        paths = arguments['referenced_image_paths']
        if not isinstance(paths, list) or not paths:
            raise ValueError('native referenced_image_paths must be a nonempty ordered list')
        resolved = [_ex()._inside(path,root) for path in paths]
        if any(path not in references for path in resolved):
            raise ValueError('native arguments introduce an unbound reference')
        native['referenced_image_paths'] = [str(path) for path in resolved]
    return native


def image_request_digest(packet, slot_id, arguments, *, output_path):
    """Digest to retain in the approved image scope's exact requests map."""
    native = _native_arguments(packet, arguments)
    root = Path(packet['project_dir']).resolve()
    output = _ex()._inside(output_path, root)
    if output == root:
        raise ValueError('image output_path must name a project file')
    bound = copy.deepcopy(native)
    if 'referenced_image_paths' in bound:
        refs = {str(_ex()._inside(ref['path'],root)):ref for ref in packet['references']}
        bound['referenced_image_paths'] = [{'path':refs[p]['path'],'sha256':refs[p]['sha256']}
                                          for p in bound['referenced_image_paths']]
    return digest({'packet_sha256':packet['packet_sha256'], 'slot_id':slot_id,
                   'tool_name':NATIVE_TOOL, 'arguments':bound, 'output_path':str(output.relative_to(root))})


def _load(root):
    path = root / ARTIFACT
    value = _read(path) if path.exists() else {'version':'1.0','project_id':_root(root)[1]['project_id'],'calls':[]}
    Draft202012Validator(load_schema('production_image_calls')).validate(value)
    if value['project_id'] != _root(root)[1]['project_id']:
        raise ValueError('image artifact project differs')
    ids = [call['call_id'] for call in value['calls']]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate canonical image call ID')
    return value


def _write(root, value):
    Draft202012Validator(load_schema('production_image_calls')).validate(value)
    path = root/ARTIFACT; path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x') as stream:
            json.dump(value,stream,indent=2,allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def _call(value, call_id):
    for call in value['calls']:
        if call['call_id'] == call_id:
            return call
    raise ValueError('unknown canonical image call')


def _event(call, kind, **details):
    call['events'].append({'kind':kind,'observed_at':_now(), 'details':details})


def _scope(root, scope_id):
    marker = _root(root)[1]
    scopes = _read(root/'production_scopes.json')
    matches = [s for s in scopes.get('scopes',[]) if s.get('id') == scope_id]
    if scopes.get('version') != '1.0' or len(matches) != 1:
        raise ValueError('image approval scope must exist exactly once')
    scope = matches[0]
    if (scope.get('phase') != 'image' or scope.get('status') != 'approved' or not scope.get('approved_by')
            or scope.get('project_id') != marker['project_id'] or scope.get('story_revision') != marker['story_revision']):
        raise ValueError('image scope lacks current explicit approval')
    _bound_file(root,scope.get('evidence'), 'image approval')
    return scope


def _grok_never_submitted(root, request):
    """Qualify the real image adapter's retained pre-dispatch refusal receipt.

    A bare caller label or missing session log never supplies this proof.
    """
    directory = root/'production_attempts'/request['attempt_id']
    try:
        result = _read(directory/'result.json')
        raw = _read(directory/'raw_result.json')
        data = raw.get('data') or {}
        receipt = data.get('conditioning_receipt') or {}
        if (request.get('tool_name') != 'grok_cli_image' or request.get('cli_session_id') != request['attempt_id']
                or result.get('status') != 'failed' or result.get('result') != raw
                or result.get('exception') is not None or result.get('output') is not None
                or raw.get('success') is not False or data.get('provider') != 'grok_cli'
                or data.get('dispatch_status') != 'not_dispatched'
                or data.get('session_id') != request['attempt_id']
                or receipt.get('version') != '1.0' or receipt.get('provider') != 'grok_cli'
                or receipt.get('session_id') != request['attempt_id']
                or receipt.get('dispatch_status') != 'not_dispatched'
                or (directory/'provider_request.json').exists() or (directory/'provider_result.json').exists()
                or (directory/'reconciliation.json').exists()):
            return False
        inputs = request['submitted_inputs']
        arguments = _ex()._grok_native_arguments(inputs,'image')
        assets = [{'role':'reference','index':index,'path':path,'sha256':file_sha256(path)}
                  for index,path in enumerate(arguments.get('image',[]))]
        if (receipt.get('native_tool') != _ex()._grok_native_operation(inputs,'image')
                or receipt.get('submitted_arguments') != arguments or receipt.get('input_assets') != assets):
            return False
        original = {k:v for k,v in receipt.items() if k not in {
            'request_sha256','cli_version','session_id','dispatch_status','submission_evidence'}}
        return receipt.get('request_sha256') == _ex()._digest(original)
    except (KeyError,ValueError,OSError,TypeError):
        return False


def image_usage(project_dir):
    """One cumulative image occurrence per native call or existing Grok session.

    Missing original result counts conservatively. No status/collection operation
    adds an occurrence; motion journals are deliberately excluded.
    """
    root, _ = _root(project_dir)
    occurrences = {}
    for call in _load(root)['calls']:
        occurrences[call['call_id']] = {'call_id':call['call_id'],'slot_id':call['slot_id'],
            'route':NATIVE_ROUTE,'scope_id':call['scope_id'],'allowance_scope_id':call['scope_id'],
            'state':call['state'],'counted':call['state'] != 'never_submitted'}
    for path in sorted((root/'production_attempts').glob('*/request.json')):
        request = _read(path)
        if request.get('media_kind') != 'image':
            continue
        call_id = request['attempt_id']
        if call_id in occurrences:
            raise ValueError('image call ID collides with existing provider session')
        state = _ex()._state(root,request).get('status','uncertain')
        scope = request.get('scope') or {}
        # Only the qualified actual adapter refusal can release a generation
        # slot; generic failed/not_dispatched labels remain conservative.
        if state == 'generated': state = 'imported'
        if state not in {'imported','failed'}: state = 'uncertain'
        never_submitted = _grok_never_submitted(root,request)
        occurrences[call_id] = {'call_id':call_id,'slot_id':request['shot_id'],
            'route':scope.get('provider'),'scope_id':request['scope_id'],
            'allowance_scope_id':scope.get('image_allowance_scope_id'),
            'state':'never_submitted' if never_submitted else state,
            'counted':not never_submitted}
    rows = list(occurrences.values())
    return {'total':sum(row['counted'] for row in rows), 'occurrences':rows,
        'unresolved_originals':[row['call_id'] for row in rows if row['counted'] and row['state'] in PENDING],
        'excluded_never_submitted':[row['call_id'] for row in rows if not row['counted']]}


def image_call_status(project_dir, call_id):
    """Read native call history and current-byte availability; zero new calls."""
    root,_ = _root(project_dir)
    call = _call(_load(root),call_id)
    status = {key:copy.deepcopy(value) for key,value in call.items()
              if key not in {'packet','host_arguments','submitted_arguments'}}
    if call.get('host_exception') and call['state'] in PENDING:
        status['backend_submission_status'] = 'unknown'
        status['next_action'] = 'collect_original_or_authoritative_reconciliation'
    if call.get('output'):
        output = call['output']
        def current(path):
            path = _ex()._inside(path,root)
            return path.is_file() and file_sha256(path) == output['sha256']
        status['output_current'] = current(output['path'])
        status['preserved_output_valid'] = current(output['preserved_path'])
    return status


def _admission(root, scope, slot_id, route, *, exclude_call_id=None):
    limit = scope.get('image_allowance')
    if type(limit) is not int or limit < 1:
        raise ValueError('approved image scope lacks a positive image allowance')
    routes = scope.get('allowed_image_routes')
    if (not isinstance(routes,list) or not routes or set(routes)-{NATIVE_ROUTE,'grok_cli'}
            or route not in routes):
        raise ValueError('image route is outside the approved supported routes')
    rows = [r for r in image_usage(root)['occurrences'] if r['counted'] and r['call_id'] != exclude_call_id]
    if any(r['slot_id'] == slot_id and r['state'] in PENDING for r in rows):
        raise ValueError('an uncertain or pending original image blocks replacement')
    # The ceiling is episode-wide. Historic supported image journals count too;
    # moving a request to another exact scope cannot reset cumulative usage.
    if len(rows) >= limit:
        raise ValueError('approved episode image allowance exhausted')
    return {'used':len(rows),'limit':limit,'remaining':limit-len(rows),'scope_id':scope['id']}


def check_grok_image_admission(project_dir, *, scope, shot_id, request_sha256):
    """Read-only shared ceiling check AFTER normal Grok scope/session preflight.

    Call again under production_execution's project lock before its reservation.
    No shared approval artifact means unchanged legacy bootstrap behavior.
    """
    root, _ = _root(project_dir)
    available = _read(root/'production_scopes.json').get('scopes',[])
    shared = [s for s in available if s.get('phase') == 'image' and 'image_allowance' in s]
    if not shared:
        return None
    shared_id = scope.get('image_allowance_scope_id')
    if not shared_id:
        raise ValueError('Grok image scope must bind the shared image allowance')
    current = _scope(root,scope.get('id'))
    if current != scope or scope.get('provider') != 'grok_cli':
        raise ValueError('Grok exact scope/provider authority differs')
    approved = scope.get('requests',{}).get(shot_id)
    if not isinstance(approved,list): approved = [approved]
    if request_sha256 not in approved:
        raise ValueError('Grok image request differs from exact scope authority')
    return _admission(root,_scope(root,shared_id),shot_id,'grok_cli')


def reserve_native_image(project_dir, packet, slot_id, arguments, *, output_path, scope_id, call_id=None):
    """Atomically reserve one exact approved native call, without emitting args."""
    root, marker = _root(project_dir)
    call_id = call_id or 'image-' + uuid.uuid4().hex
    if not isinstance(call_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',call_id):
        raise ValueError('image call_id must be a safe local token')
    with _ex()._lock(root):
        _validate_packet(root,packet)
        matches = [s for s in packet['board_slots'] if s['id'] == slot_id and s['status'] == 'unresolved']
        if len(matches) != 1: raise ValueError('image request requires one explicit unresolved board slot')
        sha = image_request_digest(packet,slot_id,arguments,output_path=output_path)
        value = _load(root)
        existing = next((c for c in value['calls'] if c['call_id'] == call_id),None)
        if existing:
            if existing['request_sha256'] != sha or existing['scope_id'] != scope_id:
                raise ValueError('local call ID already binds another exact request')
            return {k:copy.deepcopy(v) for k,v in existing.items() if k not in {'host_arguments','packet'}}
        if (root/'production_attempts'/call_id/'request.json').exists():
            raise ValueError('local image call ID collides with provider session')
        scope = _scope(root,scope_id)
        if scope.get('provider') != NATIVE_ROUTE: raise ValueError('native scope locks a different provider route')
        admission = _admission(root,scope,slot_id,NATIVE_ROUTE)
        peers = [r for r in image_usage(root)['occurrences'] if r['counted'] and r['scope_id'] == scope_id and r['slot_id'] == slot_id]
        allowance = scope.get('attempts_per_shot',{}).get(slot_id)
        if type(allowance) is not int or allowance < 1 or len(peers) >= allowance:
            raise ValueError('native exact board-slot allowance exhausted or missing')
        approved = scope.get('requests',{}).get(slot_id)
        if isinstance(approved,list): approved = approved[len(peers)] if len(peers)<len(approved) else None
        if approved != sha: raise ValueError('native image request differs from exact approved request')
        call = {'call_id':call_id,'slot_id':slot_id,'route':NATIVE_ROUTE,'tool_name':NATIVE_TOOL,
            'state':'reserved','project_id':marker['project_id'],'story_revision':marker['story_revision'],
            'scope_id':scope_id,'scope_sha256':digest(scope),'scope_attempt_index':len(peers),
            'request_sha256':sha,'packet':copy.deepcopy(packet),
            'host_arguments':_native_arguments(packet,arguments),
            'output_path':str(_ex()._inside(output_path,root).relative_to(root)), 'events':[]}
        call['events'].append({'kind':'packet_ready','observed_at':packet['prepared_at'],'details':{'packet_sha256':packet['packet_sha256']}})
        _event(call,'reserved',used=admission['used'],limit=admission['limit'])
        value['calls'].append(call); _write(root,value)
        # Only begin grants tool invocation; reserve output does not expose args.
        return {k:copy.deepcopy(v) for k,v in call.items() if k not in {'host_arguments','packet'}}


def begin_native_image(project_dir, call_id):
    """Emit one real host-call envelope. Re-entry never emits another invocation."""
    root,_ = _root(project_dir)
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] != 'reserved' or 'submitted_arguments' in call:
            raise ValueError('native image invocation envelope already emitted or call closed')
        _validate_packet(root,call['packet'])
        scope = _scope(root,call['scope_id'])
        if digest(scope) != call['scope_sha256']:
            raise ValueError('native image approval changed after reservation')
        _admission(root,scope,call['slot_id'],NATIVE_ROUTE,exclude_call_id=call_id)
        arguments = copy.deepcopy(call['host_arguments'])
        snapshots = root/'production_images'/call_id/'inputs'
        if arguments.get('referenced_image_paths'):
            snapshots.mkdir(parents=True,exist_ok=True)
            paths = []
            for source in arguments['referenced_image_paths']:
                source = Path(source); original = source.read_bytes()
                sha = hashlib.sha256(original).hexdigest()
                expected = next(r['sha256'] for r in call['packet']['references'] if _ex()._inside(r['path'],root)==source)
                if sha != expected: raise ValueError('reference bytes changed during host handoff')
                target = snapshots/(sha+source.suffix)
                if not target.exists(): target.write_bytes(original); target.chmod(0o444)
                if file_sha256(target) != sha: raise ValueError('immutable image reference snapshot changed')
                paths.append(str(target))
            arguments['referenced_image_paths'] = paths
        call['submitted_arguments'] = arguments
        call['state'] = 'submitted'; _event(call,'submitted',arguments_sha256=digest(arguments))
        _write(root,value)
        return {'call_id':call_id,'tool_name':NATIVE_TOOL,'arguments':copy.deepcopy(arguments),
                'arguments_sha256':digest(arguments)}


def mark_image_uncertain(project_dir, call_id, *, reason):
    root,_ = _root(project_dir)
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('uncertainty requires a reason')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] not in PENDING: raise ValueError('terminal image call cannot become uncertain')
        if call['state'] != 'uncertain':
            call['state'] = 'uncertain'; _event(call,'uncertain',reason=reason); _write(root,value)
        return copy.deepcopy(call)


def record_native_image_host_exception(project_dir, call_id, *, host_exception):
    """Retain a raised host exception without manufacturing a tool result.

    Agent-recorded JSON must bind {call_id, tool_name, arguments, exception:
    {message, optional type}, optional tool_call_id} to the emitted envelope.
    This proves the observed exception, never backend acceptance or termination.
    The original stays counted and blocks replacement until an authentic return
    can be collected with import_native_image. Python never invokes the host.
    """
    root,_ = _root(project_dir)
    path = _bound_file(root,host_exception,'host exception')
    raw = path.read_bytes(); receipt = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != host_exception['sha256']:
        raise ValueError('host exception bytes changed during recording')
    allowed = {'call_id','tool_name','arguments','exception','tool_call_id'}
    if not isinstance(receipt,dict) or set(receipt) - allowed:
        raise ValueError('host exception requires observed exception evidence, not a result or terminal assertion')
    exception = receipt.get('exception')
    if (not isinstance(exception,dict) or set(exception) - {'message','type'}
            or not isinstance(exception.get('message'),str) or not exception['message'].strip()
            or ('type' in exception and (not isinstance(exception['type'],str) or not exception['type'].strip()))
            or ('tool_call_id' in receipt and (not isinstance(receipt['tool_call_id'],str)
                                               or not receipt['tool_call_id'].strip()))):
        raise ValueError('host exception requires a nonempty actual exception message and optional type/call ID')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if 'submitted_arguments' not in call:
            raise ValueError('host exception requires the original emitted invocation envelope')
        if (receipt.get('call_id') != call_id or receipt.get('tool_name') != NATIVE_TOOL
                or receipt.get('arguments') != call['submitted_arguments']):
            raise ValueError('host exception call, tool or exact submitted arguments differ')
        if call.get('host_exception'):
            if call['host_exception']['sha256'] != host_exception['sha256']:
                raise ValueError('image call already binds a different actual host exception')
            _bound_file(root,call['host_exception'],'retained host exception')
            return copy.deepcopy(call)
        if call.get('host_receipt'):
            raise ValueError('actual host return already retained; host exception cannot be recorded')
        if call['state'] not in PENDING:
            raise ValueError('terminal image call cannot record a new host exception')
        directory = root/'production_images'/call_id; directory.mkdir(parents=True,exist_ok=True)
        preserved = directory/'host-exception.json'
        if preserved.exists() and preserved.read_bytes() != raw:
            raise ValueError('retained original host exception differs')
        if not preserved.exists(): preserved.write_bytes(raw); preserved.chmod(0o444)
        call['host_exception'] = {'kind':'agent_recorded_host_exception',
            'path':str(preserved.relative_to(root)), 'sha256':host_exception['sha256'],
            'arguments_sha256':digest(call['submitted_arguments'])}
        _event(call,'host_exception',receipt_sha256=host_exception['sha256'],error=exception['message'])
        call['state'] = 'uncertain'
        _event(call,'uncertain',reason='Host raised an exception without a result; backend submission remains unknown.')
        _write(root,value)
        return copy.deepcopy(call)


def release_unsubmitted_image(project_dir, call_id, *, reason):
    """Close only a reservation whose canonical host envelope was never emitted.

    After begin, absence of a receipt is uncertainty, never no-submission proof.
    """
    root,_ = _root(project_dir)
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('release requires a reason')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] != 'reserved' or 'submitted_arguments' in call:
            raise ValueError('cannot prove never submitted after host envelope emission')
        call['state'] = 'never_submitted'
        call['never_submitted_proof'] = {'kind':'canonical_envelope_not_emitted','request_sha256':call['request_sha256'],'reason':reason}
        _event(call,'never_submitted',reason=reason); _write(root,value)
        return copy.deepcopy(call)


def _original_image(result):
    """Qualified single-image returns: data URI or explicit local artifact path.

    No prose parsing, URL download, caller-selected source or image re-encoding.
    """
    if not isinstance(result,dict): raise ValueError('host result must be retained structured return evidence')
    uri = result.get('image_url')
    if isinstance(uri,str) and uri.startswith('data:image/'):
        header, payload = uri.split(',',1)
        if ';base64' not in header: raise ValueError('host image data URI is not base64')
        original = base64.b64decode(payload,validate=True)
        source = {'kind':'data_uri','field':'image_url'}
    else:
        candidates = [(key,result[key]) for key in ('image_path','output_path','path')
                      if isinstance(result.get(key),str) and result[key]]
        if len(candidates)!=1: raise ValueError('host result lacks one attributable original local image artifact or data URI')
        field,path = candidates[0]; path = Path(path).expanduser()
        if not path.is_absolute() or not path.is_file(): raise ValueError('returned original image path is not a readable absolute local artifact')
        original = path.read_bytes(); source = {'kind':'local_artifact','field':field,'path':str(path)}
    from PIL import Image
    with Image.open(io.BytesIO(original)) as image:
        media_type = Image.MIME.get(image.format)
        image.verify()
    if media_type not in {'image/png','image/jpeg','image/webp','image/gif'}:
        raise ValueError('host return is not a supported original image')
    return original,source,media_type


def import_native_image(project_dir, call_id, *, host_receipt):
    """Import original bytes from retained actual tool return, once.

    Receipt JSON is {tool_name, arguments, result, optional tool_call_id}. Agent
    records the host event as observed; provenance is honestly agent_recorded,
    not provider-attested. The source path must occur inside that actual return.
    """
    root,_ = _root(project_dir)
    receipt_path = _bound_file(root,host_receipt,'host return')
    raw = receipt_path.read_bytes(); receipt = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != host_receipt['sha256']:
        raise ValueError('host return bytes changed during import')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] in {'imported','failed'}:
            if call.get('host_receipt',{}).get('sha256') != host_receipt['sha256']:
                raise ValueError('image call already binds a different actual return')
            _bound_file(root,call['host_receipt'],'retained host return')
            if call['state'] == 'imported':
                _bound_file(root,call['output'],'imported image')
                _bound_file(root,{'path':call['output']['preserved_path'],'sha256':call['output']['sha256']},'preserved image')
            return copy.deepcopy(call)
        if 'submitted_arguments' not in call:
            raise ValueError('host return requires the original emitted invocation envelope')
        if (receipt.get('tool_name') != NATIVE_TOOL or receipt.get('arguments') != call['submitted_arguments']):
            raise ValueError('host return tool or exact submitted arguments differ')
        result = receipt.get('result')
        directory = root/'production_images'/call_id; directory.mkdir(parents=True,exist_ok=True)
        preserved_receipt = directory/'host-return.json'
        if preserved_receipt.exists() and preserved_receipt.read_bytes() != raw:
            raise ValueError('retained original host return differs')
        if not preserved_receipt.exists(): preserved_receipt.write_bytes(raw); preserved_receipt.chmod(0o444)
        call['host_receipt'] = {'path':str(preserved_receipt.relative_to(root)),'sha256':host_receipt['sha256']}
        call['provenance'] = {'kind':'agent_recorded_host_return','tool_name':NATIVE_TOOL,
            'arguments_sha256':digest(call['submitted_arguments']), 'receipt_sha256':host_receipt['sha256']}
        if receipt.get('tool_call_id'):
            call['provenance']['host_tool_call_id'] = receipt['tool_call_id']
        if not any(event['kind'] == 'tool_return' for event in call['events']):
            _event(call,'tool_return',receipt_sha256=host_receipt['sha256'])
        call['state'] = 'returned'; _write(root,value)
        if isinstance(result,dict) and result.get('isError') is True:
            detail = result.get('error') or result.get('content') or 'Native host returned isError=true.'
            call['state'] = 'failed'
            call['error'] = detail if isinstance(detail,str) else json.dumps(detail,ensure_ascii=False)
            _event(call,'failed',error=call['error']); _write(root,value)
            return copy.deepcopy(call)
        try:
            original,source,media_type = _original_image(result)
        except (ValueError,OSError,SyntaxError) as exc:
            call['state'] = 'uncertain'
            _event(call,'uncertain',reason='Actual return could not bind usable original image bytes: '+str(exc))
            _write(root,value)
            raise ValueError('actual image return is unresolved: '+str(exc)) from exc
        sha = hashlib.sha256(original).hexdigest()
        target = _ex()._inside(call['output_path'],root)
        preserved = directory/('output'+target.suffix)
        for path in (preserved,target):
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and path.read_bytes() != original:
                raise ValueError('image import would overwrite different retained bytes')
            if not path.exists(): path.write_bytes(original)
            if file_sha256(path) != sha: raise ValueError('original image bytes changed during import')
        preserved.chmod(0o444)
        call['output'] = {'path':str(target.relative_to(root)),'preserved_path':str(preserved.relative_to(root)),
            'sha256':sha,'media_type':media_type,'source':source}
        call['state'] = 'imported'; _event(call,'imported',output_sha256=sha); _write(root,value)
        return copy.deepcopy(call)
