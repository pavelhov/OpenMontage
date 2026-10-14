"""Durable agent-recorded connector originals; this module never calls a connector."""
from __future__ import annotations
import copy
import base64
import fcntl
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from lib import openart_mcp as mcp

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}
_ALLOW_FIXTURE_QUALIFICATION = False

def _event(state, kind, **details):
    """Local operation observations, never inferred provider service clocks."""
    state.setdefault('events', []).append({'kind': kind, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'attempt_id': state['attempt_id'], **details})

def _fail(kind, message):
    raise mcp.OpenArtMCPError(kind, message)

def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value): _fail('invalid_argument', 'invalid retained original ID')
    return value

def _private_dir(path):
    path = Path(path)
    if path.is_symlink(): _fail('state_invalid', 'private state directory cannot be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o700:
        _fail('state_invalid', 'private state directory requires owner-only permissions')
    return path

def _root(root):
    root = Path(root).resolve()
    if not root.is_dir() or not (root / 'project.json').is_file(): _fail('project_invalid', 'rooted project marker required')
    return root

def _state_root(*, create=True):
    path = Path(os.environ.get('OPENMONTAGE_OPENART_STATE_DIR', '~/.openmontage/openart')).expanduser()
    if create or path.exists(): return _private_dir(path)
    return path

def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): _fail('state_unavailable', 'retained private evidence unavailable')
    s = path.stat()
    if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600: _fail('state_invalid', 'private evidence requires owner-only permissions')
    try: return json.loads(path.read_text())
    except (OSError, ValueError): _fail('state_invalid', 'private evidence is not valid JSON')

def _write(path, value):
    path = Path(path); _private_dir(path.parent)
    if path.is_symlink(): _fail('state_invalid', 'private evidence cannot be a symlink')
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    fd, name = tempfile.mkstemp(prefix='.atomic-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(name): os.unlink(name)

@contextmanager
def _lock(root):
    directory = _private_dir(_root(root) / 'openart_mcp')
    lock = directory / '.lock'
    if lock.is_symlink(): _fail('state_invalid', 'connector lock cannot be a symlink')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600: _fail('state_invalid', 'connector lock requires private permissions')
        fcntl.flock(fd, fcntl.LOCK_EX)
        from lib import production_autonomy as _autonomy
        with _autonomy.no_status_reuse():   # fresh rechecks under the lock
            yield
    finally: os.close(fd)

def _path(root, aid):
    return _root(root) / 'openart_mcp' / 'attempts' / _id(aid) / 'state.json'

def _load(root, aid):
    state = _read(_path(root, aid))
    frozen = state['frozen']
    if state.get('attempt_id') != aid or state.get('frozen_sha256') != mcp.digest(frozen):
        _fail('state_invalid', 'frozen connector original changed')
    if state.get('receipt') is not None and state.get('receipt_sha256') != mcp.digest(state['receipt']):
        _fail('state_invalid', 'original receipt changed')
    reply_path=_path(root,aid).parent/'original-host-reply.json'
    if reply_path.exists():
        reply=_read(reply_path)
        if (reply.get('attempt_id')!=aid or reply.get('frozen_sha256')!=state['frozen_sha256']
                or reply.get('request_sha256')!=frozen['authority']['request_sha256']
                or reply.get('begin_envelope_sha256')!=mcp.digest(state['begin_envelope'])
                or reply.get('outcome_sha256')!=mcp.digest(reply.get('outcome'))
                or state.get('receipt') is not None and not state.get('receipt_correction_sha256')
                    and state['receipt_sha256']!=reply.get('outcome_sha256')):
            _fail('original_reply_mismatch','original receipt differs from immutable retained host reply')
    if state.get('download_receipt'):
        retained_download = _read(_path(root, aid).parent/'download-receipt.json')
        if mcp.digest(retained_download) != state.get('download_receipt_sha256') or mcp.digest(state['download_receipt']) != mcp.digest(retained_download):
            _fail('download_changed','original download receipt differs from retained independent byte proof')
    if state.get('receipt_correction_sha256') is not None:
        correction=_read(_path(root,aid).parent/'receipt-correction.json')
        _check_receipt_correction(root,state,correction)
        if (mcp.digest(correction)!=state['receipt_correction_sha256']
                or mcp.digest(state['receipt'])!=correction['outcome_sha256']):
            _fail('original_reply_mismatch','audited original receipt correction changed')
    return state

def _save(root, aid, state): _write(_path(root, aid), state)

def _validate_original_metadata(state,result,*,require_binding=False):
    """Compare actual returned fields; an audited correction needs full proof."""
    native=state['frozen']['native'];body=native['body'];params=body['params']
    expected={'projectId':body.get('projectId'),'capabilityId':native['model']+':'+native['mode'],
        'model':native['model'],'mode':native['mode'],
        'resolution':params.get('videoResolution',params.get('resolution')),
        'aspectRatio':params.get('videoAspectRatio',params.get('aspectRatio'))}
    if require_binding and any(key not in result or expected[key] is None
            for key in ('projectId','capabilityId','resolution','aspectRatio')):
        _fail('original_binding_unavailable','full actual project/capability/format evidence required for correction')
    if require_binding and result.get('status') not in {'PENDING','RUNNING',*TERMINAL}:
        _fail('original_binding_unavailable','recognized actual creation status required for correction')
    for key,value in expected.items():
        if key in result and value is not None and result[key]!=value:
            _fail('original_identity_mismatch','returned '+key+' contradicts the original native request')
    prompt=params.get('sceneDescription',params.get('prompt'))
    if 'prompt' in result or require_binding:
        if not isinstance(result.get('prompt'),str) or not isinstance(prompt,str):
            _fail('original_binding_unavailable','actual prompt evidence required')
        if result['prompt'].strip()!=prompt.strip():
            _fail('original_identity_mismatch','returned prompt contradicts the original native request')
    if 'inputImages' in result or require_binding:
        # The existing factory's frame routes have exact ordered start/end URLs.
        # Other media roles have no established full-get projection here.
        if any(row['role'] not in {'first_frame','last_frame'} for row in native.get('input_assets',[])):
            if require_binding:_fail('original_binding_unavailable','complete returned media-role proof unavailable')
            return
        frames=[{'role':role,'url':params[field]['url']} for field,role in
            (('startFrame','start'),('endFrame','end')) if field in params]
        rows=result.get('inputImages')
        if not isinstance(rows,list) or any(not isinstance(row,dict) or not {'role','url'}<=set(row) for row in rows):
            _fail('original_binding_unavailable','actual ordered input image evidence required')
        if [{'role':row.get('role'),'url':row.get('url')} for row in rows]!=frames:
            _fail('original_identity_mismatch','returned ordered frame URLs contradict the original native request')
    for key,value in params.items():
        if key not in {'prompt','sceneDescription','resolution','aspectRatio','videoResolution','videoAspectRatio'} and key in result and result[key]!=value:
            _fail('original_identity_mismatch','returned '+key+' contradicts original native controls')

def _history_owners(root,history,exclude):
    owners=[]
    for path in sorted((root/'openart_mcp'/'attempts').glob('*/state.json')):
        aid=path.parent.name
        if aid==exclude:continue
        state=_load(root,aid);receipts=[state.get('receipt'),*state['status_observations']]
        reply_path=path.parent/'original-host-reply.json'
        if reply_path.exists() and not state.get('receipt_correction_sha256'):
            reply=_read(reply_path)
            if (reply.get('attempt_id')!=aid or reply.get('frozen_sha256')!=state['frozen_sha256']
                    or reply.get('request_sha256')!=state['frozen']['authority']['request_sha256']
                    or reply.get('begin_envelope_sha256')!=mcp.digest(state['begin_envelope'])
                    or mcp.digest(reply.get('outcome'))!=reply.get('outcome_sha256')):
                _fail('original_reply_mismatch','other retained original host reply changed')
            receipts.append(reply.get('outcome'))
        correction_path=path.parent/'receipt-correction.json'
        if correction_path.exists():
            correction=_read(correction_path);_check_receipt_correction(root,state,correction)
            receipts.append(correction['outcome'])
        if any(receipt and receipt.get('historyId')==history for receipt in receipts):
            owners.append({'attempt_id':aid,'frozen_sha256':state['frozen_sha256'],
                'receipt_sha256':state.get('receipt_sha256')})
    return owners

def _check_receipt_correction(root,state,correction):
    reply=_read(_path(root,state['attempt_id']).parent/'original-host-reply.json')
    if (correction.get('attempt_id')!=state['attempt_id']
            or correction.get('frozen_sha256')!=state['frozen_sha256']
            or correction.get('request_sha256')!=state['frozen']['authority']['request_sha256']
            or correction.get('begin_envelope_sha256')!=mcp.digest(state['begin_envelope'])
            or correction.get('original_host_reply_sha256')!=mcp.digest(reply)
            or reply.get('attempt_id')!=state['attempt_id']
            or reply.get('frozen_sha256')!=state['frozen_sha256']
            or reply.get('request_sha256')!=correction.get('request_sha256')
            or reply.get('begin_envelope_sha256')!=correction.get('begin_envelope_sha256')
            or reply.get('outcome_sha256')!=correction.get('original_receipt_sha256')
            or correction.get('original_receipt_sha256')!=mcp.digest(correction.get('original_receipt'))
            or mcp.digest(reply.get('outcome'))!=correction.get('original_receipt_sha256')
            or correction.get('outcome_sha256')!=mcp.digest(correction.get('outcome'))
            or correction.get('kind')!='wrong_cross_attempt_receipt_correction'
            or not correction.get('original_owners')
            or correction.get('target_begin_event_sha256') not in {mcp.digest(e) for e in state['events'] if e['kind']=='begin_consumed_before_handoff'}):
        _fail('original_reply_mismatch','audited correction differs from immutable original evidence')
    for owner in correction['original_owners']:
        if owner['attempt_id']==state['attempt_id']:
            _fail('original_reply_mismatch','correction cannot claim its own wrong receipt')
        if _read(_path(root,_id(owner['attempt_id']))).get('receipt_correction_sha256'):
            _fail('original_reply_mismatch','foreign original ownership must be uncorrected')
        original=_load(root,_id(owner['attempt_id']))
        receipt=original.get('receipt')
        if (owner['attempt_id']==state['attempt_id'] or original.get('receipt_correction_sha256')
                or original['frozen_sha256']!=owner['frozen_sha256'] or not receipt
                or mcp.digest(receipt)!=owner['receipt_sha256']
                or receipt.get('historyId')!=correction['original_receipt'].get('historyId')):
            _fail('original_reply_mismatch','wrong original history ownership proof changed')
        events=[e for e in original['events'] if e['kind']=='submission_receipt_retained' and e.get('history_id')==receipt['historyId']]
        begins=[e for e in state['events'] if e['kind']=='begin_consumed_before_handoff']
        if (not events or len(begins)!=1 or mcp.digest(events[0])!=owner.get('submission_event_sha256')
                or begins[0].get('envelope_sha256')!=mcp.digest(state['begin_envelope'])
                or datetime.fromisoformat(events[0]['observed_at'])>=datetime.fromisoformat(begins[0]['observed_at'])):
            _fail('original_reply_mismatch','foreign original was not retained before target begin')
        _validate_original_metadata(original,receipt)
    _validate_original_metadata(state,correction['outcome'],require_binding=True)

def _authority(inputs, aid, fn):
    if not callable(fn): _fail('authority_invalid', 'rooted authority callback required')
    authority = fn(copy.deepcopy(inputs), attempt_id=aid)
    if not isinstance(authority, dict): _fail('authority_invalid', 'rooted authority callback did not return bindings')
    profile = mcp.load_profile(inputs.get('model'), inputs.get('mode'), require='candidate' if authority.get('purpose') == 'qualification' else 'supported')
    from lib.production_execution import _openart_mcp_controls
    native = mcp.prepare_native_request(_openart_mcp_controls(inputs), profile)
    required = {'provider':'openart_mcp', 'model':native['model'], 'mode':native['mode'],
        'native_body_sha256':native['body_sha256'], 'source_binding_sha256':native['source_binding_sha256'],
        'account_uid_sha256':native['account_binding']['uid_sha256']}
    if any(authority.get(k) != v for k,v in required.items()): _fail('authority_invalid', 'rooted authority differs from exact native request')
    if authority.get('purpose') not in {'qualification','production'} or not authority.get('scope_sha256') or not authority.get('request_sha256'):
        _fail('authority_invalid', 'rooted purpose/scope/request bindings missing')
    if mcp.digest(authority.get('scope')) != authority['scope_sha256']: _fail('authority_invalid', 'immutable rooted scope snapshot missing')
    return authority, native

def prepare(project_dir, *, attempt_id, generation_inputs, authority_fn):
    root = _root(project_dir); aid = _id(attempt_id)
    with _lock(root):
        if _path(root, aid).exists(): _fail('original_exists', 'original attempt cannot be replaced or reused')
        started_at = datetime.now(timezone.utc).isoformat()
        if Path(generation_inputs.get('project_dir','')).resolve() != root: _fail('project_invalid', 'generation root differs')
        authority, native = _authority(generation_inputs, None, authority_fn)
        evidence = _capture_evidence(root,generation_inputs,native,authority)
        evidence_path = _path(root,aid).parent/'evidence.json'
        _write(evidence_path,evidence)
        frozen = {'evidence_snapshot_sha256':mcp.digest(evidence), 'generation_inputs':copy.deepcopy(generation_inputs), 'native':native,
            'authority':authority, 'purpose':authority['purpose']}
        state = {'version':'1.0', 'provider':'openart_mcp', 'attempt_id':aid, 'status':'prepared',
            'frozen':frozen, 'frozen_sha256':mcp.digest(frozen), 'authority_sha256':mcp.digest(authority),
            'begin_envelope':None, 'receipt':None, 'status_observations':[], 'events':[], 'output':None}
        state['events'].append({'kind': 'prepare_started', 'observed_at': started_at, 'attempt_id': aid})
        _event(state, 'prepared')
        _save(root, aid, state)
        return {'attempt_id':aid, 'status':'prepared', 'authority_sha256':state['authority_sha256'],
            'transport':'agent_mediated_connector', 'dispatch_status':'prepared_not_submitted'}

def begin(project_dir, attempt_id, *, authority_fn):
    root = _root(project_dir); aid = _id(attempt_id)
    with _lock(root):
        state = _load(root, aid)
        if state['status'] != 'prepared' or state['begin_envelope'] is not None:
            _fail('original_consumed', 'original connector begin already consumed; never resubmit')
        authority, native = _authority(state['frozen']['generation_inputs'], aid, authority_fn)
        if mcp.digest(authority) != state['authority_sha256'] or mcp.digest(native) != mcp.digest(state['frozen']['native']):
            _fail('authority_changed', 'approval/account/native/source drifted after preparation')
        envelope = {'tool':'mcp__codex_apps__openart_openart_generate_video', 'arguments':copy.deepcopy(native['body'])}
        state['begin_envelope'] = envelope; state['status'] = 'uncertain'
        _event(state, 'begin_consumed_before_handoff', envelope_sha256=mcp.digest(envelope))
        _save(root, aid, state)  # consumed durably before the agent can call the connector
        return envelope

def receive(project_dir, attempt_id, *, outcome=None, error=None):
    root = _root(project_dir); aid = _id(attempt_id)
    with _lock(root):
        state = _load(root, aid)
        if state['begin_envelope'] is None: _fail('not_begun', 'connector original was not begun')
        if (outcome is None) == (error is None): _fail('invalid_argument', 'receive requires exactly one original result or error')
        reply_path = _path(root, aid).parent / 'original-host-reply.json'
        if state['receipt'] is not None:
            if error is None and mcp.digest(outcome) == state['receipt_sha256']:
                return attempt_state(root, aid)
            _fail('receipt_exists', 'original connector receipt cannot be replaced')
        if error is not None or not isinstance(outcome, dict) or not isinstance(outcome.get('historyId'), str) or not outcome.get('historyId'):
            state['status']='uncertain'; _event(state, 'uncertain', error=str(error) if error is not None else 'missing original historyId')
        else:
            if _history_owners(root,outcome['historyId'],aid):
                _fail('original_history_reused','original historyId already belongs to another retained attempt')
            _validate_original_metadata(state,outcome)
            # Retain the exact original host reply before updating state. On a
            # receive interruption the same reply can be replayed without begin.
            reply = {'attempt_id': aid, 'frozen_sha256': state['frozen_sha256'],
                     'request_sha256': state['frozen']['authority']['request_sha256'],
                     'begin_envelope_sha256': mcp.digest(state['begin_envelope']),
                     'outcome': copy.deepcopy(outcome), 'outcome_sha256': mcp.digest(outcome)}
            if reply_path.exists():
                if _read(reply_path) != reply:
                    _fail('receipt_exists', 'retained original host reply cannot be replaced')
            else:
                _write(reply_path, reply)
            state['receipt']=copy.deepcopy(outcome); state['receipt_sha256']=mcp.digest(outcome); state['status']='submitted'
            _event(state, 'submission_receipt_retained', history_id=outcome['historyId'])
        _save(root, aid, state)
        return attempt_state(root, aid)

def resume_original(project_dir, attempt_id, *, outcome=None, request_sha256=None, correct_receipt=False):
    """Inspect the original handoff, or replay its exact retrievable host reply.

    No plain inspection writes, connector calls, polling, new reservations or
    assumption that a consumed begin was unsubmitted. ``outcome`` must be the
    original host response for this exact frozen handoff, never a replacement.
    """
    root = _root(project_dir); aid = _id(attempt_id)
    if correct_receipt:
        with _lock(root):
            state=_load(root,aid);original_request=state['frozen']['authority']['request_sha256']
            if request_sha256!=original_request or not isinstance(outcome,dict) or not isinstance(outcome.get('historyId'),str) or not outcome['historyId']:
                _fail('original_request_mismatch','correction requires exact original request and actual history')
            if state.get('receipt_correction_sha256'):
                if mcp.digest(outcome)!=state['receipt_sha256']:
                    _fail('receipt_exists','audited correction cannot be replaced')
                return attempt_state(root,aid)
            if (state['status']!='submitted' or not state['receipt'] or state['status_observations']
                    or state.get('download_receipt') or state.get('output')
                    or any(p.name.startswith(('download','output')) or p.suffix=='.part' for p in _path(root,aid).parent.iterdir())
                    or _inside(root,state['frozen']['generation_inputs'].get('output_path','')).is_file()):
                _fail('original_correction_unavailable','correction requires an untouched consumed original with no observation or media')
            owners=_history_owners(root,state['receipt']['historyId'],aid)
            if not owners:
                _fail('original_correction_unavailable','old history must demonstrably belong to another retained original')
            if _history_owners(root,outcome['historyId'],aid):
                _fail('original_history_reused','corrected history already belongs to another retained attempt')
            _validate_original_metadata(state,outcome,require_binding=True)
            reply_path=_path(root,aid).parent/'original-host-reply.json'
            reply=_read(reply_path)
            for owner in owners:
                original=_load(root,owner['attempt_id'])
                events=[e for e in original['events'] if e['kind']=='submission_receipt_retained' and e.get('history_id')==state['receipt']['historyId']]
                if not events:_fail('original_correction_unavailable','foreign original submission evidence unavailable')
                owner['submission_event_sha256']=mcp.digest(events[0])
            begins=[e for e in state['events'] if e['kind']=='begin_consumed_before_handoff']
            if len(begins)!=1:_fail('original_correction_unavailable','exact target begin observation required')
            params=state['frozen']['native']['body']['params']
            correction={'version':'1.0','kind':'wrong_cross_attempt_receipt_correction','attempt_id':aid,
                'frozen_sha256':state['frozen_sha256'],'request_sha256':original_request,
                'begin_envelope_sha256':mcp.digest(state['begin_envelope']),
                'target_begin_event_sha256':mcp.digest(begins[0]),
                'original_host_reply_sha256':mcp.digest(reply),'original_receipt':copy.deepcopy(state['receipt']),
                'original_receipt_sha256':state['receipt_sha256'],'original_owners':owners,
                'outcome':copy.deepcopy(outcome),'outcome_sha256':mcp.digest(outcome),
                'unavailable_native_controls':sorted(k for k in params if k not in outcome and k not in
                    {'prompt','sceneDescription','resolution','aspectRatio','videoResolution','videoAspectRatio','startFrame','endFrame'})}
            _check_receipt_correction(root,state,correction)
            correction_path=_path(root,aid).parent/'receipt-correction.json'
            if correction_path.exists():
                if _read(correction_path)!=correction:_fail('receipt_exists','retained correction evidence cannot be replaced')
            else:_write(correction_path,correction)
            state.update(receipt=copy.deepcopy(outcome),receipt_sha256=mcp.digest(outcome),
                receipt_correction_sha256=mcp.digest(correction))
            _event(state,'wrong_cross_attempt_receipt_corrected',old_history_id=correction['original_receipt']['historyId'],
                history_id=outcome['historyId'],correction_sha256=state['receipt_correction_sha256'])
            _save(root,aid,state)
            return attempt_state(root,aid)
    state = _load(root, aid)
    original_request = state['frozen']['authority']['request_sha256']
    if request_sha256 is not None and request_sha256 != original_request:
        _fail('original_request_mismatch', 'recovery request differs from original handoff')
    reply_path = _path(root, aid).parent / 'original-host-reply.json'
    reply = None
    if reply_path.exists():
        reply = _read(reply_path)
        if (reply.get('attempt_id') != aid or reply.get('frozen_sha256') != state['frozen_sha256']
                or reply.get('request_sha256') != original_request
                or reply.get('begin_envelope_sha256') != mcp.digest(state['begin_envelope'])
                or not isinstance(reply.get('outcome'), dict)
                or reply.get('outcome_sha256') != mcp.digest(reply['outcome'])):
            _fail('original_reply_mismatch', 'retained host reply differs from exact original handoff')
    if state.get('receipt_correction_sha256'):
        correction=_read(_path(root,aid).parent/'receipt-correction.json')
        reply={**reply,'outcome':correction['outcome'],'outcome_sha256':correction['outcome_sha256']}
    elif (_path(root,aid).parent/'receipt-correction.json').exists():
        correction=_read(_path(root,aid).parent/'receipt-correction.json')
        _check_receipt_correction(root,state,correction)
        if outcome is not None:
            _fail('original_correction_pending','replay audited correction explicitly with correct_receipt=True')
        return {**attempt_state(root,aid),'request_sha256':original_request,'provider_calls':0,'reservations':0,
            'next_action':{'action':'correct_original_receipt','function':'lib.openart_mcp_jobs.resume_original',
                'attempt_id':aid,'request_sha256':original_request,'correct_receipt':True,'outcome':copy.deepcopy(correction['outcome'])}}
    if outcome is not None:
        if request_sha256 != original_request:
            _fail('original_request_mismatch', 'original reply replay requires exact request digest')
        if reply is not None and mcp.digest(outcome) != reply['outcome_sha256']:
            _fail('original_reply_mismatch', 'retrieved reply differs from retained original reply')
        return receive(root, aid, outcome=outcome)
    result = attempt_state(root, aid)
    result.update(request_sha256=original_request, provider_calls=0, reservations=0)
    if state['status'] == 'prepared' and state['begin_envelope'] is None:
        result['next_action'] = {'action': 'begin_original', 'function': 'lib.openart_mcp_jobs.begin',
                                 'attempt_id': aid, 'authority_revalidation_required': True}
    elif state['receipt'] is None:
        result['next_action'] = ({'action': 'receive_original', 'function': 'lib.openart_mcp_jobs.resume_original',
                                  'attempt_id': aid, 'request_sha256': original_request,
                                  'outcome': copy.deepcopy(reply['outcome'])} if reply else
                                 {'action': 'blocked_original_host_reply_unavailable',
                                  'reason': 'Consumed original handoff has no retrievable historyId; retrieve its exact host reply and replay receive. Never begin again.',
                                  'attempt_id': aid, 'request_sha256': original_request})
    elif state['status'] == 'submitted':
        result['next_action'] = {'action': 'poll_original', **poll_args(root, aid)}
    elif state['status'] == 'completed':
        result['next_action'] = {'action': 'download_original', 'function': 'lib.openart_mcp_jobs.download_original', 'attempt_id': aid}
    else:
        result['next_action'] = {'action': 'inspect_original', 'status': state['status']}
    return result


def poll_args(project_dir, attempt_id):
    state = _load(project_dir, _id(attempt_id))
    if (_path(project_dir,attempt_id).parent/'receipt-correction.json').exists() and not state.get('receipt_correction_sha256'):
        _fail('original_correction_pending','replay audited correction before polling any original')
    if state['receipt'] is None: _fail('original_uncertain', 'original historyId unavailable; never replace it')
    return {'tool':'mcp__codex_apps__openart_openart_creation_get', 'arguments':{'historyId':state['receipt']['historyId']}}

def record_status(project_dir, attempt_id, *, result):
    root = _root(project_dir); aid = _id(attempt_id)
    with _lock(root):
        state = _load(root, aid); receipt = state['receipt']
        if (_path(root,aid).parent/'receipt-correction.json').exists() and not state.get('receipt_correction_sha256'):
            _fail('original_correction_pending','replay audited correction before recording original status')
        if not receipt or not isinstance(result, dict) or result.get('historyId') != receipt['historyId']:
            _fail('original_id_mismatch', 'status must be for the exact original historyId')
        if result.get('status') not in {'PENDING','RUNNING',*TERMINAL}: _fail('status_invalid', 'unrecognized original creation status')
        _validate_original_metadata(state,result)
        previous = state['status_observations']
        if previous and previous[-1]['status'] in TERMINAL:
            if mcp.digest(previous[-1]) != mcp.digest(result): _fail('terminal_changed', 'terminal original status cannot be replaced')
            return attempt_state(root, aid)
        state['status_observations'].append(copy.deepcopy(result))
        _event(state, 'status_observed', history_id=receipt['historyId'], status=result['status'])
        state['status'] = {'COMPLETED':'completed','FAILED':'failed','CANCELLED':'failed'}.get(result['status'],'submitted')
        _save(root, aid, state); return attempt_state(root, aid)

def _terminal(state, statuses):
    receipt = state.get('receipt'); observations = state['status_observations']
    if not receipt or not observations or observations[-1].get('historyId') != receipt['historyId'] or observations[-1].get('status') not in statuses:
        _fail('original_not_terminal', 'terminal evidence must belong to the original historyId')
    return observations[-1]

def _video_resource(terminal):
    rows=terminal.get('resources')
    if terminal.get('warning') or not isinstance(rows,list) or not rows:
        _fail('output_ambiguous','completed original must contain every promised resource')
    count=terminal.get('resourceCount')
    if count is not None and (type(count) is not int or count!=len(rows)):
        _fail('output_ambiguous','original completed resource count is incomplete')
    for row in rows:
        if (not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or not row.get('url')
            or str(row.get('status','COMPLETED')).upper() not in {'COMPLETED','SUCCESS','SUCCEEDED'}):
            _fail('output_ambiguous','one or more promised original resources failed, pending, or missing')
    videos=[r for r in rows if r.get('mediaType')=='video']
    if len(videos)!=1:_fail('output_ambiguous','one unambiguous original video resource required; paid nextStep is never called')
    return videos[0]


def collect(project_dir, attempt_id, *, downloaded_path):
    root = _root(project_dir); aid = _id(attempt_id)
    with _lock(root):
        state = _load(root, aid)
        if state['status'] == 'collected':
            if _sha(state['output']['path']) != state['output']['sha256']:
                _fail('output_changed','collected original output changed')
            return attempt_state(root, aid)
        if state['status'] != 'completed': _fail('original_not_completed', 'only the completed original can be collected')
        _event(state, 'collection_started', history_id=state['receipt']['historyId'])
        _save(root, aid, state)
        terminal = _terminal(state, {'COMPLETED'})
        videos = [_video_resource(terminal)]
        supplied = Path(downloaded_path).expanduser()
        if any(p.is_symlink() for p in [supplied, *supplied.parents]):
            _fail('output_invalid', 'downloaded original cannot use symlink aliases')
        source = supplied.resolve()
        if not source.is_file(): _fail('output_invalid', 'agent-downloaded original media file required')
        try:
            probe = subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type','-of','json',str(source)], capture_output=True, check=True, text=True)
            if not any(s.get('codec_type') == 'video' for s in json.loads(probe.stdout)['streams']): raise ValueError()
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError): _fail('output_invalid', 'collected original is not a readable video')
        destination = Path(state['frozen']['authority']['output_path']).resolve()
        if not destination.is_relative_to(root): _fail('output_invalid', 'approved output path must be rooted')
        sha = __import__('hashlib').sha256(source.read_bytes()).hexdigest()
        receipt = state.get('download_receipt')
        if (not receipt or receipt.get('sha256') != sha or receipt.get('path') != str(source)
                or receipt.get('history_id') != state['receipt']['historyId']
                or receipt.get('resource_sha256') != mcp.digest(videos[0])
                or state.get('download_receipt_sha256') != mcp.digest(receipt)):
            _fail('download_receipt_required', 'collect requires bytes downloaded from the retained original resource')
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open('rb') as source_stream, destination.open('xb') as output_stream:
                shutil.copyfileobj(source_stream, output_stream)
                output_stream.flush(); os.fsync(output_stream.fileno())
        except FileExistsError:
            if destination.is_symlink() or not destination.is_file() or _sha(destination) != sha:
                _fail('output_exists', 'approved output contains foreign bytes; cannot overwrite')
            # Crash recovery: finalize only the identical already-preserved original.
        if __import__('hashlib').sha256(destination.read_bytes()).hexdigest() != sha: _fail('output_changed', 'collected output bytes differ')
        state['output']={'path':str(destination),'sha256':sha}; state['resource']=copy.deepcopy(videos[0]); state['status']='collected'
        _event(state, 'collected', history_id=state['receipt']['historyId'], output_sha256=sha)
        _save(root, aid, state); return attempt_state(root, aid)

def attempt_state(project_dir, attempt_id):
    s = _load(project_dir, _id(attempt_id))
    return {'attempt_id':s['attempt_id'],'status':s['status'],'history_id':(s['receipt'] or {}).get('historyId'),
        'output':copy.deepcopy(s['output']), 'transport':'agent_mediated_connector'}

def frozen_request(project_dir, attempt_id):
    s = _load(project_dir, _id(attempt_id)); return {**copy.deepcopy(s['frozen']), 'status':s['status']}

def list_attempts(project_dir):
    root = _root(project_dir); directory = root / 'openart_mcp' / 'attempts'
    if not directory.exists(): return []
    rows=[]
    for path in sorted(directory.glob('*/state.json')):
        s = _load(root, path.parent.name); a=s['frozen']['authority']; scope=a['scope']
        rows.append({'attempt_id':s['attempt_id'],'provider':'openart_mcp','scope_id':a['scope_id'],
            'shot_id':a['shot_id'],'scope_attempt_index':a['scope_attempt_index'],'media_kind':'motion',
            'phase':scope['phase'],'scope_snapshot':copy.deepcopy(scope),'request_sha256':a['request_sha256'],
            'authority_sha256':s['authority_sha256'],'submitted_inputs':copy.deepcopy(s['frozen']['generation_inputs']), 'status':s['status']})
    return rows

def provenance_record(project_dir, attempt_id):
    s=_load(project_dir, _id(attempt_id)); _terminal(s, {'COMPLETED'})
    if s['status'] != 'collected' or not s['output']: _fail('not_collected', 'original media not collected')
    resource=_video_resource(s['status_observations'][-1]); receipt=s.get('download_receipt')
    if (not receipt or receipt['history_id']!=s['receipt']['historyId']
            or receipt['resource_sha256']!=mcp.digest(resource)
            or receipt['terminal_sha256']!=mcp.digest(s['status_observations'][-1])
            or receipt['sha256']!=s['output']['sha256']):
        _fail('download_changed','original resource/download/output chain differs')
    return {**copy.deepcopy(s['frozen']), 'provenance':'agent_recorded_connector','attempt_id':s['attempt_id'],
        'begin_envelope':copy.deepcopy(s['begin_envelope']),'receipt':copy.deepcopy(s['receipt']),
        'status_observations':copy.deepcopy(s['status_observations']), 'output':copy.deepcopy(s['output']),
        'download_receipt':copy.deepcopy(s.get('download_receipt')), 'download_receipt_sha256':s.get('download_receipt_sha256'),
        **({'receipt_correction':_read(_path(project_dir,attempt_id).parent/'receipt-correction.json'),
            'receipt_correction_sha256':s['receipt_correction_sha256']} if s.get('receipt_correction_sha256') else {})}

def terminal_failure_record(project_dir, attempt_id):
    s=_load(project_dir, _id(attempt_id)); terminal=_terminal(s, {'FAILED','CANCELLED'})
    return {'attempt_id':s['attempt_id'],'history_id':s['receipt']['historyId'],
        'terminal':copy.deepcopy(terminal),'terminal_failure_sha256':mcp.digest(terminal)}

def _capture_evidence(root,inputs,native,authority):
    from lib.production_request import source_packet
    from lib.production_execution import _artifact_path
    from lib.shot_contract import file_sha256, review_digest, shot_review_subject
    compiled_path=_artifact_path(root,'compiled_request-'+_id(inputs['compiled_request_id'])+'.json')
    review_path=_artifact_path(root,'preparation_review-'+_id(inputs['preparation_review_id'])+'.json')
    compiled=json.loads(compiled_path.read_text());review=json.loads(review_path.read_text())
    packet=source_packet(root,authority['shot_id'],provider='openart_mcp',native=native)
    if authority['preparation']!={'compiled_sha256':mcp.digest(compiled),'review_sha256':mcp.digest(review)} or mcp.digest(packet['binding'])!=mcp.digest(compiled['source_binding']):
        _fail('evidence_changed','preparation changed while capturing its immutable proof')
    paths=[compiled_path,review_path,root/'project.json',root/'checkpoint_prepare.json',root/'artifacts'/'provider_qualification_packet.json']
    paths += [_artifact_path(root,name+'.json') for name in ('shot_contract','script','scene_plan')]
    paths += [_inside(root,authority['scope']['evidence']['path'])]
    if inputs.get('unknown_cost_authorization_id'):
        paths.append(_artifact_path(root,'openart_mcp_unknown_cost-'+_id(inputs['unknown_cost_authorization_id'])+'.json'))
    # Source bindings identify reviewed assets; paths belong to the authoritative
    # contract, not the compiled reference projection.
    contract=json.loads(_artifact_path(root,'shot_contract.json').read_text())
    subject = shot_review_subject(contract, authority['shot_id'])
    if subject != packet['binding']['contract_sha256']:
        _fail('evidence_changed','contract changed while capturing reference evidence')
    reference_hashes={}
    consumer_roles = []
    board_path = root / 'artifacts/board_preparation.json'
    if board_path.exists():
        from lib import production_boards as boards
        pack = boards.load_board_pack(root)
        consumer_roles = [role for shot in pack['specification']['shots']
                          if shot['shot_id'] == authority['shot_id'] for role in shot['roles']]
        if pack.get('package_binding'):
            from lib import production_images as images
            paths.append(images._bound_file(root, pack['package_binding'], 'production package'))
    for row in packet['binding'].get('references',[]):
        matches=[asset for asset in contract['assets'] if asset['id']==row['id']]
        if len(matches)!=1:
            _fail('evidence_changed','source reference asset missing or ambiguous')
        asset=matches[0];path=_inside(root,asset['path'])
        role_bound = asset['role'] == row['role'] or any(
            role['asset_id'] == asset['id'] and role['role'] == row['role']
            and ('time_seconds' not in row or role.get('time_seconds') == row['time_seconds'])
            for role in consumer_roles)
        if (not role_bound or any(asset[key]!=row[key] for key in ('cast_ids','sha256'))
                or review_digest(asset['review'])!=row['review_sha256']
                or not path.is_file() or file_sha256(path)!=row['sha256']):
            _fail('evidence_changed','source reference binding or bytes changed')
        paths.append(path);reference_hashes[path]=row['sha256']
    paths += [_inside(root,row['source_path']) for row in native['input_assets']]
    blobs={}
    for path in paths:
        if path in reference_hashes and not path.is_file():
            _fail('evidence_changed','source reference disappeared during immutable capture')
        if path.is_file():
            raw=path.read_bytes();sha256=__import__('hashlib').sha256(raw).hexdigest()
            if path in reference_hashes and sha256!=reference_hashes[path]:
                _fail('evidence_changed','source reference changed during immutable capture')
            blobs[str(path.relative_to(root))]={'sha256':sha256,'bytes_base64':base64.b64encode(raw).decode()}
    return {'compiled':compiled,'review':review,'source_packet':packet,'scope':copy.deepcopy(authority['scope']),'files':blobs}

def _check_qualification_archive(archive,candidate):
    from lib.production_request import _schema
    record=archive['provenance'];native=record['native'];authority=record['authority'];evidence=archive['evidence']
    if (record['purpose']!='qualification' or (native['source']!='real' and not (_ALLOW_FIXTURE_QUALIFICATION and native['source']=='fixture')) or authority['purpose']!='qualification'
        or native['provider']!='openart_mcp' or native['model']!=candidate['model'] or native['mode']!=candidate['mode']
        or native['account_binding']['uid_sha256']!=candidate['account_uid_sha256']
        or native['profile_sha256']!=candidate['profile_sha256'] or native['form_sha256']!=candidate['form_sha256']
        or native['schema_observation_sha256']!=candidate['schema_observation_sha256']
        or native['body_sha256']!=mcp.digest(native['body']) or native['source_binding_sha256']!=mcp.digest(native['input_assets'])
        or authority['native_body_sha256']!=native['body_sha256'] or authority['source_binding_sha256']!=native['source_binding_sha256']
        or mcp.digest(authority['scope'])!=authority['scope_sha256'] or mcp.digest(evidence['scope'])!=authority['scope_sha256']
        or mcp.digest(record['begin_envelope'])!=mcp.digest({'tool':'mcp__codex_apps__openart_openart_generate_video','arguments':native['body']})):
        _fail('qualification_invalid','archived original account/model/form/native/scope chain differs')
    schema=candidate['form']['jsonSchema']
    if not mcp._validator(schema).is_valid(native['body']['params']):_fail('qualification_invalid','archived original body violates qualified form')
    receipt=record['receipt'];terminal=record['status_observations'][-1];credits=terminal.get('creditsCharged')
    if terminal.get('historyId')!=receipt.get('historyId') or not receipt.get('historyId') or terminal.get('status')!='COMPLETED' or isinstance(credits,bool) or not isinstance(credits,(int,float)) or not math.isfinite(credits) or credits<0:
        _fail('qualification_invalid','archived original terminal/known billing evidence missing')
    download=record.get('download_receipt')
    resources=[r for r in terminal.get('resources',[]) if r.get('mediaType')=='video' and r.get('url')]
    if (len(resources)!=1 or not download or mcp.digest(download)!=record.get('download_receipt_sha256')
        or download['history_id']!=receipt['historyId'] or download['resource_sha256']!=mcp.digest(resources[0])
        or download['sha256']!=record['output']['sha256'] or _sha(archive['output_snapshot_path'])!=record['output']['sha256']):
        _fail('qualification_invalid','archived original resource/download/output byte proof differs')
    for binding in evidence['files'].values():
        raw=base64.b64decode(binding['bytes_base64'],validate=True)
        if __import__('hashlib').sha256(raw).hexdigest()!=binding['sha256']:_fail('qualification_invalid','archived approved source/evidence bytes changed')
    compiled=evidence['compiled'];review=evidence['review'];packet=evidence['source_packet']
    _schema('compiled_request',compiled);_schema('preparation_review',review)
    if (authority['preparation']!={'compiled_sha256':mcp.digest(compiled),'review_sha256':mcp.digest(review)}
        or review['status']!='pass' or review['subject_sha256']!=mcp.digest(compiled)
        or mcp.digest(compiled['source_binding'])!=mcp.digest(packet['binding'])
        or compiled['request_sha256']!=authority['request_sha256']):
        _fail('qualification_invalid','archived compiled/source/review proof differs')
    binding=compiled['native_binding']
    for key,value in {'provider':'openart_mcp','transport':mcp.TRANSPORT,'native_body_sha256':native['body_sha256'],
        'native_controls_sha256':mcp.digest(native['body']['params']),'profile_sha256':candidate['profile_sha256'],
        'form_sha256':candidate['form_sha256'],'schema_observation_sha256':candidate['schema_observation_sha256'],
        'account_id_sha256':candidate['account_uid_sha256'],'references_sha256':native['source_binding_sha256']}.items():
        if binding.get(key)!=value:_fail('qualification_invalid','archived compiled native binding differs')
    scope=evidence['scope'];approval_path=_inside(Path(archive['original_project_root']),scope['evidence']['path'])
    approval=evidence['files'].get(str(approval_path.relative_to(Path(archive['original_project_root']))))
    if not approval or approval['sha256']!=scope['evidence']['sha256'] or scope.get('status')!='approved' or scope.get('provider')!='openart_mcp':
        _fail('qualification_invalid','archived original human approval proof differs')
    source_root=Path(archive['original_project_root'])
    for row in native['input_assets']:
        path=str(Path(row['source_path']).relative_to(source_root));blob=evidence['files'].get(path)
        if not blob or blob['sha256']!=row['source_sha256']:_fail('qualification_invalid','archived exact native source bytes missing')
    return True

def qualified_profile(model, mode, candidate):
    fixture = candidate.get('source') == 'fixture'
    if fixture and not _ALLOW_FIXTURE_QUALIFICATION:return None
    namespace = 'mcp-fixture-qualifications' if fixture else 'mcp-qualifications'
    proofs_namespace = 'mcp-fixture-qualification-proofs' if fixture else 'mcp-qualification-proofs'
    path=_state_root(create=False)/namespace/(model+'__'+mode+'.json')
    if not path.exists():return None
    q=_read(path)
    if q.get('candidate_sha256')!=candidate['profile_sha256']:return None
    if q.get('qualification_sha256')!=mcp.digest({k:v for k,v in q.items() if k!='qualification_sha256'}):_fail('qualification_invalid','retained qualification changed')
    if not q.get('archive_path') or not q.get('attempt_id') or not q.get('original_project_root'):_fail('qualification_invalid','bare qualification marker has no original transport proof')
    archive_path=Path(q['archive_path'])
    if not archive_path.resolve().is_relative_to((_state_root()/proofs_namespace).resolve()):_fail('qualification_invalid','qualification archive must be privately retained')
    archive=_read(archive_path)
    if q.get('source')!=candidate['source'] or archive.get('source')!=candidate['source'] or (fixture and (q.get('evidence_kind')!='fixture_only' or archive.get('evidence_kind')!='fixture_only')):
        _fail('qualification_invalid','qualification evidence classification cannot change')
    original=_load(q['original_project_root'],q['attempt_id'])
    if (original['status']!='collected' or original['frozen_sha256']!=q['original_frozen_sha256']
        or mcp.digest(archive)!=q['archive_sha256'] or mcp.digest(archive['provenance'])!=q['original_provenance_sha256']
        or mcp.digest(provenance_record(q['original_project_root'],q['attempt_id']))!=q['original_provenance_sha256']
        or mcp.digest(archive['evidence'])!=original['frozen']['evidence_snapshot_sha256']):
        _fail('qualification_invalid','qualification archive no longer matches the original consumed journal')
    _check_qualification_archive(archive,candidate)
    return {**copy.deepcopy(candidate),'status':'qualified','profile_status':'qualified','production_ready':not fixture,'qualification_sha256':q['qualification_sha256'],'evidence_kind':q['evidence_kind']}

def qualify_result(project_dir, attempt_id):
    with _lock(project_dir):
        record=provenance_record(project_dir,attempt_id);native=record['native']
        fixture = native['source']=='fixture'
        if record['purpose']!='qualification' or (fixture and not _ALLOW_FIXTURE_QUALIFICATION):
            _fail('qualification_invalid','only a real original qualification sample can qualify MCP')
        if fixture and (not os.environ.get('OPENMONTAGE_OPENART_STATE_DIR') or not os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR')
                or _state_root().resolve()==Path('~/.openmontage/openart').expanduser().resolve()
                or not str(mcp.account_summary().get('classification','')).startswith('synthetic_fixture')):
            _fail('qualification_invalid','fixture qualification tests require isolated synthetic private state')
        namespace='mcp-fixture-qualifications' if fixture else 'mcp-qualifications'
        proofs_namespace='mcp-fixture-qualification-proofs' if fixture else 'mcp-qualification-proofs'
        evidence_kind='fixture_only' if fixture else 'original_result_qualification'
        from lib.production_provenance import validate_attempt_provenance
        root=_root(project_dir);marker=json.loads((root/'project.json').read_text())
        validate_attempt_provenance(root,attempt_id,shot_id=record['authority']['shot_id'],story_revision=marker['story_revision'],expected_output=record['output'])
        candidate=mcp.load_profile(native['model'],native['mode'],require='candidate');mcp.validate_native_request(native,candidate)
        original=_load(root,attempt_id);evidence=_read(_path(root,attempt_id).parent/'evidence.json')
        directory=_private_dir(_state_root()/proofs_namespace/candidate['profile_sha256']/(attempt_id+'-'+mcp.digest(record)[:16]))
        output=directory/'original-output.mp4'
        if not output.exists():
            with Path(record['output']['path']).open('rb') as src,output.open('xb') as dst:
                os.chmod(output,0o600);shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
        archive={'source':native['source'],'evidence_kind':evidence_kind,'provenance':record,'evidence':evidence,'original_project_root':str(root),'output_snapshot_path':str(output)}
        _check_qualification_archive(archive,candidate)
        archive_path=directory/'original-proof.json';_write(archive_path,archive)
        q={'provider':'openart_mcp','source':native['source'],'evidence_kind':evidence_kind,'candidate_sha256':candidate['profile_sha256'],'attempt_id':attempt_id,'original_project_root':str(root),
            'original_frozen_sha256':original['frozen_sha256'],'archive_path':str(archive_path),'archive_sha256':mcp.digest(archive),
            'original_provenance_sha256':mcp.digest(record)}
        q['qualification_sha256']=mcp.digest(q)
        _write(_state_root()/namespace/(native['model']+'__'+native['mode']+'.json'),q)
        return qualified_profile(native['model'],native['mode'],candidate)

# Upload authority is a separate fresh source-transfer acknowledgement. It never
# authorizes generation and does not claim uploads are free or cannot bill later.
def _sha(path): return __import__('hashlib').sha256(Path(path).read_bytes()).hexdigest()

def _inside(root, path):
    resolved=(root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not resolved.is_relative_to(root): _fail('source_invalid','source/evidence must belong to approved project')
    return resolved

def _reviewed_source(root, path, sha):
    """Current physical source and every alias review, independent of shot eligibility."""
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    from lib.production_execution import _artifact_path
    from lib.shot_contract import ASSET_PREDICATES, CRITICAL_PREDICATES
    try: contract=json.loads(_artifact_path(root,'shot_contract.json').read_text())
    except (OSError,ValueError): _fail('source_unapproved','retained source contract required')
    marker=json.loads((root/'project.json').read_text())
    if contract.get('project_id')!=marker.get('project_id') or contract.get('story_revision')!=marker.get('story_revision'):
        _fail('source_unapproved','reviewed source contract project/story is stale')
    if not Draft202012Validator(load_schema('shot_contract')).is_valid(contract):
        _fail('source_unapproved','reviewed source contract schema is invalid')
    if len({a['id'] for a in contract['assets']})!=len(contract['assets']):
        _fail('source_unapproved','reviewed source contract asset IDs are ambiguous')
    # Uploads bind one physical file; the contract can give its bytes multiple
    # roles. Every alias must bind those current bytes and its own current review.
    assets=[a for a in contract['assets'] if a.get('path') and _inside(root,a['path'])==path]
    if not assets or any(a.get('sha256')!=sha for a in assets):
        _fail('source_unapproved','exact source bytes require current reviewed project assets')
    from lib.shot_contract import factory_project
    if factory_project(root):
        # One-shot factory: exact alias bytes bind; semantic review is report-only.
        return contract,marker,assets
    for asset in assets:
        review=asset.get('review',{});predicates=review.get('predicates',[])
        names={p['name'] for p in predicates}
        if (review.get('story_revision')!=marker['story_revision'] or review.get('subject_sha256')!=sha
                or review.get('status')!='pass' or len(names)!=len(predicates) or not ASSET_PREDICATES<=names
                or any((p['name'] in CRITICAL_PREDICATES and p.get('severity')=='cosmetic')
                    or (p['status']!='pass' and (p['name'] in CRITICAL_PREDICATES or p.get('severity','critical')=='critical'))
                    for p in predicates)):
            _fail('source_unapproved','current critical source review does not approve every exact source alias')
    return contract,marker,assets

def _approved_source(root, path, sha):
    from lib.production_execution import load_selected_attempts
    from lib.shot_contract import validate_shot_contract
    contract,marker,assets=_reviewed_source(root,path,sha)
    # New transfers require actual required-source coverage in eligible shots;
    # sharing a cast never supplies that authority.
    pending={a['id'] for a in assets}; selected=load_selected_attempts(root)
    for shot in contract['shots']:
        required=set(shot['asset_ids'])
        if contract.get('reference_mode')!='reference_free':required.add(contract.get('payoff_asset_id'))
        cast=set(contract['late_cast_ids']) | set(shot['cast_ids']) | set(contract['payoff_speaker_ids'])
        required.update(a['id'] for a in assets if a['role']=='identity_reference' and cast.intersection(a['cast_ids']))
        applicable=pending.intersection(required)
        if applicable:
            checked=validate_shot_contract(contract,project_dir=root,shot_id=shot['id'],story_revision=marker['story_revision'],selected_upstream=selected)
            if checked['eligible']:
                pending.difference_update(applicable)
                if not pending:return contract
    _fail('source_unapproved','current critical source/shot review does not approve exact source bytes')

def _media_type(path):
    import mimetypes
    mime=mimetypes.guess_type(str(path))[0] or ''
    kind=mime.split('/')[0]
    if kind not in {'image','video','audio'}: _fail('source_invalid','source must be an identified image, video, or audio file')
    return kind

def _upload_authority(root, files, declaration, account, project_id):
    if not isinstance(declaration,dict) or set(declaration)!={'upload_authorization_id'}:
        _fail('upload_authority_required','select one retained fresh upload authorization ID; caller declarations cannot grant authority')
    aid=_id(declaration['upload_authorization_id'])
    path=_inside(root,Path('artifacts')/('openart_mcp_upload_authorization-'+aid+'.json'))
    try: authority=json.loads(path.read_text())
    except (OSError,ValueError): _fail('upload_authority_required','retained upload authorization unavailable')
    marker=json.loads((root/'project.json').read_text())
    exact={'provider':'openart_mcp','status':'approved','purpose':'source_transfer_only',
        'project_id':marker['project_id'],'story_revision':marker['story_revision'],
        'account_uid_sha256':account['uid_sha256'],'openart_project_id':project_id,
        'files':files,'no_enforceable_credit_ceiling':True,'delayed_charges_unknown':True,'max_upload_batches':1}
    if any(mcp.digest(authority.get(k))!=mcp.digest(v) for k,v in exact.items()) or not isinstance(authority.get('approved_by'),str) or not authority['approved_by'].strip():
        _fail('upload_authority_invalid','exact fresh upload source/account/project/exposure terms differ')
    evidence=authority.get('evidence',{})
    if not isinstance(evidence,dict) or not evidence.get('path') or _sha(_inside(root,evidence['path']))!=evidence.get('sha256'):
        _fail('upload_authority_invalid','source-transfer approval evidence changed')
    if authority.get('derived_from_standing_run') is not None:
        from lib.production_autonomy import validate_standing_mcp_upload
        try:
            validate_standing_mcp_upload(root, authority, files=files, account=account, openart_project_id=project_id)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            _fail('upload_authority_invalid', 'current standing source-transfer authority invalid: ' + str(exc))
    return authority,mcp.digest(authority)

def prepare_upload(project_dir, *, files, billing_declaration, openart_project_id=None):
    import uuid
    root=_root(project_dir)
    if not isinstance(files,list) or not 1<=len(files)<=10 or any(not isinstance(p,str) for p in files): _fail('invalid_argument','upload requires one to ten ordered source paths')
    with _lock(root):
        account=mcp.account_summary(); manifest=[]
        for supplied in files:
            path=_inside(root,supplied)
            if not path.is_file() or Path(supplied).is_symlink(): _fail('source_invalid','regular approved source file required')
            sha=_sha(path)
            manifest.append({'path':str(path),'sha256':sha,'type':_media_type(path)})
        if len({r['path'] for r in manifest})!=len(manifest): _fail('source_invalid','duplicate upload source paths are ambiguous')
        authority,authority_sha=_upload_authority(root,manifest,billing_declaration,account,openart_project_id)
        if not authority.get('derived_from_standing_run', {}).get('policy_fallback'):
            for row in manifest:
                _approved_source(root,Path(row['path']),row['sha256'])
        directory=_private_dir(root/'openart_mcp'/'uploads')
        for retained in directory.glob('*.json'):
            if _read(retained).get('authorization_sha256')==authority_sha: _fail('upload_consumed','approved upload batch already consumed; never silently retry')
        uid='upload-'+uuid.uuid4().hex
        snapshot_dir=_private_dir(directory/uid/'sources'); snapshots=[]
        for index,row in enumerate(manifest):
            target=snapshot_dir/(str(index)+Path(row['path']).suffix)
            with Path(row['path']).open('rb') as src,target.open('xb') as dst:
                os.chmod(target,0o600); shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
            if _sha(target)!=row['sha256']:_fail('source_changed','source changed while preserving upload snapshot')
            snapshots.append({'path':str(target),'sha256':row['sha256']})
        arguments={'files':[row['path'] for row in snapshots]}
        if openart_project_id is not None: arguments['projectId']=openart_project_id
        envelope={'tool':'mcp__codex_apps__openart_openart_upload_import','arguments':arguments}
        frozen={'root':str(root),'manifest':manifest,'source_snapshots':snapshots,'account':account,'project_id':openart_project_id,
            'billing_declaration':copy.deepcopy(billing_declaration),'authorization_sha256':authority_sha,'envelope':envelope}
        _write(directory/(uid+'.json'),{'upload_id':uid,'status':'consumed_awaiting_original_receipt',
            'frozen':frozen,'frozen_sha256':mcp.digest(frozen),'authorization_sha256':authority_sha,'result':None})
        return {'upload_id':uid,**envelope,'billing':{'kind':'unknown_upload_cost','enforceable_credit_ceiling':False,'delayed_charges_unknown':True},'generation_enabled':False}

def _validate_upload_retry(root, batch, receipt=None):
    retry = batch.get('transport_retry')
    if 'transport_retry' in batch:
        if (not isinstance(retry, dict) or retry.get('source_transfer_invocation') != 2
                or retry.get('state') not in {'reserved_awaiting_receipt', 'recorded'}
                or (batch.get('status') == 'recorded') != (retry['state'] == 'recorded')
                or _sha(_inside(root, retry['observed_error']['path'])) != retry['observed_error']['sha256']
                or batch.get('status') == 'recorded' and (retry['state'] != 'recorded'
                    or batch.get('result_provenance') != 'actual_source_transport_retry')):
            _fail('upload_receipt_invalid', 'reserved source retry error evidence changed')
    if receipt is not None and (('source_transfer_invocation' in receipt) != (retry is not None)
            or retry is not None and (receipt.get('source_transfer_invocation') != 2
                or receipt.get('result_provenance') != 'actual_source_transport_retry')):
        _fail('upload_receipt_invalid', 'source retry receipt differs from retained invocation')


def retry_upload_after_transport_error(project_dir, *, upload_id, observed_error):
    """Reserve one same-envelope source retry after an evidenced HTTP send error.

    This neither proves the first transfer absent nor promises backend dedup.
    The extra invocation consumes standing transfer allowance before handoff.
    """
    from lib import production_autonomy as autonomy, production_execution as execution
    root = _root(project_dir); uid = _id(upload_id)
    with execution._lock(root), _lock(root):
        path = root/'openart_mcp/uploads'/(uid+'.json')
        retained = _read(path); frozen = retained['frozen']
        if 'transport_retry' in retained:
            _fail('upload_consumed', 'source transport retry already reserved; recover its actual receipt')
        if (retained.get('upload_id') != uid or retained.get('status') != 'consumed_awaiting_original_receipt'
                or retained.get('result') is not None or retained['frozen_sha256'] != mcp.digest(frozen)
                or frozen['root'] != str(root)):
            _fail('upload_receipt_invalid', 'exact unresolved frozen upload required')
        evidence = _inside(root, observed_error['path'])
        if _sha(evidence) != observed_error['sha256']:
            _fail('upload_retry_invalid', 'observed transport error bytes changed')
        observation = json.loads(evidence.read_text()); error = observation.get('result', {})
        content = error.get('content', []) if isinstance(error, dict) else []
        text = '\n'.join(row.get('text', '') for row in content if isinstance(row, dict) and row.get('type') == 'text')
        envelope = frozen['envelope']
        if (observation.get('tool_name') != envelope['tool'] or observation.get('arguments') != envelope['arguments']
                or envelope['tool'] != 'mcp__codex_apps__openart_openart_upload_import'
                or error.get('isError') is not True or set(error) != {'isError', 'content'}
                or not content or any(not isinstance(row, dict) or row.get('type') != 'text' for row in content)
                or not all(term in text for term in ('Transport send error', 'HTTP request failed',
                    'error sending request for url'))
                or any(term in text.lower() for term in ('denied', 'policy', 'rejected', 'forbidden', 'unauthorized', 'permission', '401', '403'))):
            _fail('upload_retry_invalid', 'only the exact source-upload HTTP send failure permits this retry')
        account = mcp.account_summary()
        if account['uid_sha256'] != frozen['account']['uid_sha256']:
            _fail('account_changed', 'source retry account changed')
        authority, sha = _upload_authority(root, frozen['manifest'], frozen['billing_declaration'], account, frozen['project_id'])
        binding = authority.get('derived_from_standing_run')
        if (not binding or sha != frozen['authorization_sha256'] or sha != retained['authorization_sha256']
                or not frozen['project_id'] or envelope['arguments'] != {'files': [s['path'] for s in frozen['source_snapshots']],
                    'projectId': frozen['project_id']}):
            _fail('upload_authority_invalid', 'source retry requires unchanged standing account/project authority')
        if len(frozen['source_snapshots']) != len(frozen['manifest']):
            _fail('source_changed', 'ordered upload snapshots changed')
        for source, snapshot in zip(frozen['manifest'], frozen['source_snapshots']):
            if (snapshot['sha256'] != source['sha256'] or _sha(_inside(root, snapshot['path'])) != source['sha256']
                    or _sha(_inside(root, source['path'])) != source['sha256']):
                _fail('source_changed', 'source retry bytes or snapshot changed')
            _approved_source(root, _inside(root, source['path']), source['sha256'])
        policy, _, _ = autonomy.require_active_policy(root, shot_id=binding['shot_id'])
        setup = autonomy.effective_standing_setup(root, policy)
        term = next(t for t in setup['source_transfer'] if t['provider'] == 'openart_mcp'
            and t['account'] == setup['provider_accounts']['openart_mcp'])
        if autonomy._standing_upload_batch_count(root, 'openart_mcp') >= term['max_batches']:
            _fail('upload_consumed', 'standing source transfer batch allowance exhausted')
        retained['transport_retry'] = {'source_transfer_invocation': 2, 'state': 'reserved_awaiting_receipt',
            'observed_error': copy.deepcopy(observed_error), 'original_outcome': 'uncertain_transport_error'}
        _write(path, retained)
        return {'upload_id': uid, **copy.deepcopy(envelope), 'source_transfer_invocation': 2,
            'generation_enabled': False, 'billing': {'kind': 'unknown_upload_cost', 'enforceable_credit_ceiling': False,
                'delayed_charges_unknown': True}}


def record_upload_receipt(project_dir, *, upload_id, result):
    root=_root(project_dir); uid=_id(upload_id)
    with _lock(root):
        path=root/'openart_mcp'/'uploads'/(uid+'.json'); retained=_read(path); frozen=retained['frozen']
        if retained['frozen_sha256']!=mcp.digest(frozen) or retained['result'] is not None: _fail('upload_receipt_invalid','original upload evidence changed or already retained')
        _validate_upload_retry(root, retained)
        account=mcp.account_summary()
        if account['uid_sha256']!=frozen['account']['uid_sha256']: _fail('account_changed','upload receipt belongs to a different current account')
        authority, current_authority_sha = _upload_authority(root,frozen['manifest'],frozen['billing_declaration'],account,frozen['project_id'])
        if current_authority_sha != frozen['authorization_sha256']:
            _fail('upload_authority_invalid', 'source-transfer authority changed after original upload')
        if not isinstance(result,dict) or not isinstance(result.get('projectId'),str) or not result['projectId'] or (frozen['project_id'] is not None and result['projectId']!=frozen['project_id']):
            _fail('upload_receipt_invalid','actual upload project differs')
        rows=result.get('results')
        if not isinstance(rows,list) or len(rows)!=len(frozen['manifest']): _fail('upload_receipt_invalid','every ordered source requires an original upload result')
        ids=set(); file_ids=set(); receipts=[]
        for source,snapshot,row in zip(frozen['manifest'],frozen['source_snapshots'],rows):
            if _sha(snapshot['path'])!=source['sha256']:
                _fail('source_changed','private approved upload snapshot changed')
            file=Path(source['path'])
            if _sha(file)!=source['sha256']: _fail('source_changed','uploaded source bytes changed')
            if not authority.get('derived_from_standing_run', {}).get('policy_fallback'):
                _approved_source(root,file,source['sha256'])
            ref=row.get('visualReference',{}) if isinstance(row,dict) else {}
            if row.get('status')!='SUCCESS' or not isinstance(row.get('fileId'),str) or not row['fileId'] or row['fileId'] in file_ids:
                _fail('upload_receipt_invalid','all ordered original upload results must succeed with unique actual file IDs')
            if not isinstance(ref,dict) or not isinstance(ref.get('id'),str) or not ref['id'] or ref['id'] in ids or ref.get('type')!=source['type'] or not isinstance(ref.get('url'),str) or not ref['url'].startswith('https://') or not isinstance(ref.get('metadata'),dict):
                _fail('upload_receipt_invalid','ready actual typed reference with native metadata required')
            ids.add(ref['id']);file_ids.add(row['fileId'])
            receipt={'provider':'openart_mcp','project_root':str(root),'source_path':source['path'],'source_sha256':source['sha256'],
                'account_uid_sha256':account['uid_sha256'],'account_observation_sha256':frozen['account']['account_observation_sha256'],
                'project_id':result['projectId'],'requested_project_id':frozen['project_id'],'upload_batch_id':uid,
                'source_index':len(receipts),'raw_result':copy.deepcopy(row),'visual_reference':copy.deepcopy(ref),
                'authorization_sha256':frozen['authorization_sha256'],'source': 'fixture' if str(account.get('classification','')).startswith('synthetic_fixture') else 'real'}
            if 'transport_retry' in retained:
                receipt['source_transfer_invocation'] = 2
                receipt['result_provenance'] = 'actual_source_transport_retry'
            receipt_sha=mcp.digest(receipt);receipt['receipt_sha256']=receipt_sha
            receipts.append(receipt)
        # Validate the whole batch before publishing any reference binding.
        store=_private_dir(_state_root()/'mcp-uploads')
        for receipt in receipts:_write(store/(receipt['receipt_sha256']+'.json'),receipt)
        retained.update(status='recorded',result=copy.deepcopy(result),result_sha256=mcp.digest(result),receipt_sha256s=[r['receipt_sha256'] for r in receipts])
        if 'transport_retry' in retained:
            retained['transport_retry']['state'] = 'recorded'
            retained['result_provenance'] = 'actual_source_transport_retry'
        _write(path,retained)
        return {'upload_id':uid,'status':'recorded','project_id':result['projectId'],
            **({'source_transfer_invocation': 2} if 'transport_retry' in retained else {}),
            'references':[{'upload_id':r['visual_reference']['id'],'reference_id':r['visual_reference']['id'],'receipt_sha256':r['receipt_sha256'],'source_sha256':r['source_sha256'],'source_path':r['source_path'],'type':r['visual_reference']['type']} for r in receipts]}

def reuse_uploaded_input_asset(project_dir, asset, *, account_binding, openart_project_id):
    """Read-only exact receipt discovery for fresh preparation; never transfer.

    Explicit selectors retain their meaning. Otherwise choose a stable receipt
    SHA from exact project/path/current-byte/account matches and validate it with
    the ordinary resolver, including original batch and upload authority. A
    consumed upload with no receipt requires original recovery, never reupload.
    """
    root = _root(project_dir); path = _inside(root, asset['source_path']); sha = _sha(path)
    if asset.get('source_sha256', sha) != sha:
        _fail('source_changed', 'caller source hash differs from actual bytes')
    if {'upload_id', 'reference_id'}.intersection(asset):
        return resolve_input_asset(root, asset, account_binding=account_binding,
            openart_project_id=openart_project_id)
    if not isinstance(openart_project_id, str) or not openart_project_id:
        _fail('upload_reference_unavailable', 'receipt reuse requires an exact OpenArt project')
    store = _state_root(create=False) / 'mcp-uploads'
    candidates = []
    if store.exists():
        for item in store.glob('*.json'):
            receipt = _read(item)
            if (receipt.get('project_root') == str(root) and receipt.get('source_path') == str(path)
                    and receipt.get('source_sha256') == sha
                    and receipt.get('account_uid_sha256') == account_binding['uid_sha256']
                    and receipt.get('project_id') == openart_project_id):
                candidates.append(receipt['receipt_sha256'])
    if candidates:
        return resolve_input_asset(root, {**asset, 'upload_id': sorted(candidates)[0]},
            account_binding=account_binding, openart_project_id=openart_project_id)
    directory = root / 'openart_mcp' / 'uploads'
    if directory.exists():
        for item in directory.glob('*.json'):
            batch = _read(item); frozen = batch.get('frozen', {})
            if (frozen.get('root') == str(root)
                    and frozen.get('account', {}).get('uid_sha256') == account_binding['uid_sha256']
                    and frozen.get('project_id') == openart_project_id
                    and any(row.get('path') == str(path) and row.get('sha256') == sha
                            for row in frozen.get('manifest', []))):
                _fail('upload_uncertain', 'exact source upload already consumed as ' + str(batch.get('upload_id', item.stem))
                    + '; recover its original receipt from ' + str(item)
                    + (' (recorded original result retained)' if batch.get('result') is not None else ' (original result unresolved)'))
    return None


def resolve_input_asset(project_dir, asset, *, account_binding, openart_project_id=None,
                        policy_fallback_call_id=None):
    root=_root(project_dir);path=_inside(root,asset['source_path']);sha=_sha(path)
    if asset.get('source_sha256',sha)!=sha:_fail('source_changed','caller source hash differs from actual bytes')
    # The original upload proved transfer eligibility. Native rebuilds recheck
    # current source reviews/bytes and immutable receipts; target-shot eligibility
    # belongs to governed preparation, including historical source-packet replay.
    if policy_fallback_call_id is None:
        _reviewed_source(root,path,sha)
    else:
        from lib import production_autonomy as autonomy, production_images as images, production_boards as boards
        try:
            call=images._call(images._load(root),policy_fallback_call_id)
            shot=boards.load_board_pack(root)['prompt_pack'][call['slot_id']]['shot_id']
            _, expected, _=autonomy._standing_upload_material(root,shot_id=shot,provider='openart_mcp',
                files=[str(path)],account=account_binding,
                openart_project_id=openart_project_id,policy_fallback_call_id=policy_fallback_call_id)
            if not any(row['path']==str(path) and row['sha256']==sha for row in expected['files']):
                _fail('source_unapproved','fallback source differs from the exact denied-call references')
        except (ValueError,OSError,KeyError,TypeError) as exc:
            _fail('source_unapproved','current denied-call source-transfer authority invalid: '+str(exc))
    identifier=asset.get('upload_id',asset.get('reference_id'));matches=[]
    store=_state_root(create=False)/'mcp-uploads'
    if store.exists():
        for item in store.glob('*.json'):
            receipt=_read(item);ref=receipt['visual_reference']
            if receipt.get('receipt_sha256')!=mcp.digest({k:v for k,v in receipt.items() if k!='receipt_sha256'}):_fail('upload_receipt_invalid','retained source reference changed')
            if identifier is not None and identifier not in {ref['id'],receipt['receipt_sha256']}:continue
            if (receipt['project_root']==str(root) and receipt['source_path']==str(path) and receipt['source_sha256']==sha
                and receipt['account_uid_sha256']==account_binding['uid_sha256'] and (openart_project_id is None or receipt['project_id']==openart_project_id)):
                matches.append(receipt)
    if len(matches)!=1:_fail('upload_reference_unavailable','exact approved source/account/project requires one retained actual upload reference')
    r=matches[0];ref=r['visual_reference']
    batch=_read(root/'openart_mcp'/'uploads'/(_id(r['upload_batch_id'])+'.json'))
    _validate_upload_retry(root, batch, r)
    frozen=batch['frozen']; index=r['source_index']
    if (batch.get('status')!='recorded' or batch['frozen_sha256']!=mcp.digest(frozen)
            or batch.get('result_sha256')!=mcp.digest(batch.get('result'))
            or type(index) is not int or index<0 or index>=len(frozen['manifest'])
            or batch['receipt_sha256s'][index]!=r['receipt_sha256']
            or mcp.digest(batch['result']['results'][index])!=mcp.digest(r['raw_result'])
            or mcp.digest(r['visual_reference'])!=mcp.digest(r['raw_result']['visualReference'])
            or frozen['manifest'][index]!= {'path':str(path),'sha256':sha,'type':ref['type']}
            or frozen['source_snapshots'][index]['sha256']!=sha or _sha(frozen['source_snapshots'][index]['path'])!=sha
            or frozen['account']['uid_sha256']!=account_binding['uid_sha256']
            or batch['result']['projectId']!=r['project_id']
            or frozen['authorization_sha256']!=r['authorization_sha256']):
        _fail('upload_receipt_invalid','reference differs from the immutable original ordered upload batch')
    authority,authsha=_upload_authority(root,frozen['manifest'],frozen['billing_declaration'],account_binding,frozen['project_id'])
    if authsha!=frozen['authorization_sha256']:
        _fail('upload_authority_invalid','original source-transfer authority changed')
    if policy_fallback_call_id is not None:
        original_refs=authority.get('derived_from_standing_run',{}).get('source_binding',{}).get('references',[])
        needed=expected['derived_from_standing_run']['source_binding']['references'][0]
        if not any(ref.get('role')==needed['role'] and ref.get('sha256')==sha
                   and ref.get('path') is not None and _inside(root,ref['path'])==path for ref in original_refs):
            _fail('upload_authority_invalid','reference receipt does not retain the same exact source role')
    return {'source_path':str(path),'source_sha256':sha,'upload_id':ref['id'],'reference_id':ref['id'],
        'reference_type':ref['type'],'receipt_sha256':r['receipt_sha256'],'visual_reference':copy.deepcopy(ref),
        'upload_project_id':r['project_id'],'upload_authorization_sha256':r['authorization_sha256']}

def download_original(project_dir, attempt_id):
    """Read-only original resource GET and concrete byte receipt; never generate."""
    root=_root(project_dir);aid=_id(attempt_id)
    with _lock(root):
        state=_load(root,aid)
        if (_path(root,aid).parent/'receipt-correction.json').exists() and not state.get('receipt_correction_sha256'):
            _fail('original_correction_pending','replay audited correction before downloading any original')
        if state['status']!='completed':_fail('original_not_completed','only completed original resource can be downloaded')
        terminal=_terminal(state,{'COMPLETED'})
        resource=_video_resource(terminal);url=resource['url']
        if not isinstance(url,str) or not url.startswith('https://'):_fail('output_invalid','original resource requires HTTPS')
        if state.get('download_receipt'):
            receipt=state['download_receipt']
            if state['download_receipt_sha256']!=mcp.digest(receipt) or _sha(receipt['path'])!=receipt['sha256']:_fail('download_changed','retained original download changed')
            return {'downloaded_path':receipt['path'],'sha256':receipt['sha256'],'history_id':receipt['history_id']}
        directory=_path(root,aid).parent;path=directory/'download-original.mp4'
        retained_path = directory/'download-receipt.json'
        if retained_path.exists():
            receipt = _read(retained_path)
            if not path.exists() and receipt.get('publication_source'):
                import re
                staged = Path(receipt['publication_source'])
                # Only the exact verified stream retained by collect_output is
                # eligible; arbitrary nearby mp4s and caller paths never are.
                if (receipt.get('history_id') != state['receipt']['historyId']
                        or receipt.get('resource_url') != url
                        or receipt.get('resource_id') != resource['id']
                        or receipt.get('resource_sha256') != mcp.digest(resource)
                        or receipt.get('terminal_sha256') != mcp.digest(terminal)
                        or receipt.get('path') != str(path)
                        or receipt.get('provenance') != 'read_only_original_resource_download'
                        or staged.parent != directory
                        or not re.fullmatch(r'\.openart-download-[a-f0-9]{32}', staged.name)
                        or staged.is_symlink() or not staged.is_file()
                        or _sha(staged) != receipt.get('sha256')
                        or staged.stat().st_size != receipt.get('bytes')):
                    _fail('download_changed', 'interrupted publication staging does not bind verified original bytes')
                try:
                    os.link(staged, path, follow_symlinks=False)
                except FileExistsError:
                    pass  # Recheck the exact canonical bytes below.
                descriptor = os.open(directory, os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
            if (receipt.get('provenance') != 'read_only_original_resource_download'
                    or receipt.get('history_id') != state['receipt']['historyId']
                    or receipt.get('resource_id') != resource['id']
                    or receipt.get('resource_url') != url
                    or receipt.get('resource_sha256') != mcp.digest(resource)
                    or receipt.get('terminal_sha256') != mcp.digest(terminal)
                    or receipt.get('path') != str(path)
                    or path.is_symlink() or not path.is_file()
                    or receipt.get('sha256') != _sha(path)
                    or receipt.get('bytes') != path.stat().st_size):
                _fail('download_changed', 'interrupted original download receipt does not bind current original bytes')
            state['download_receipt'] = receipt
            state['download_receipt_sha256'] = mcp.digest(receipt)
            _event(state, 'download_receipt_recovered', history_id=receipt['history_id'],
                   resource_id=resource['id'], output_sha256=receipt['sha256'])
            _save(root, aid, state)
            return {'downloaded_path':str(path),'sha256':receipt['sha256'],'history_id':receipt['history_id']}
        if path.exists():_fail('download_uncertain','unreceipted original download exists; review it before another download')
        from lib import openart_download
        from urllib.parse import urlsplit
        _event(state, 'download_started', history_id=state['receipt']['historyId'], resource_id=resource['id'])
        _save(root, aid, state)
        def retain_verified_download(downloaded):
            receipt = {'history_id': state['receipt']['historyId'], 'resource_id': resource['id'],
                'resource_url': url, 'final_url': downloaded['final_url'],
                'redirect_chain': downloaded['redirect_chain'], 'resource_sha256': mcp.digest(resource),
                'terminal_sha256': mcp.digest(terminal), 'path': str(path),
                'sha256': downloaded['sha256'], 'bytes': downloaded['size'],
                'provenance': 'read_only_original_resource_download',
                'publication_source': downloaded['publication_source']}
            _write(retained_path, receipt)
        try:
            downloaded=openart_download.collect_output(url,path,allowed_hosts={urlsplit(url).hostname},
                output_root=directory,max_bytes=512*1024*1024,timeout=60,
                allow_public_redirects=True,retain_private_trace=True,
                before_publish=retain_verified_download)
        except openart_download.OpenArtDownloadError as exc:
            _fail('download_failed',str(exc))
        receipt = _read(retained_path)
        if receipt['sha256'] != _sha(path) or receipt['bytes'] != path.stat().st_size:
            _fail('download_changed', 'published original differs from verified download stream')
        state['download_receipt']=receipt;state['download_receipt_sha256']=mcp.digest(receipt)
        _event(state, 'download_retained', history_id=receipt['history_id'], resource_id=resource['id'], output_sha256=receipt['sha256'])
        _save(root, aid, state)
        return {'downloaded_path':str(path),'sha256':receipt['sha256'],'history_id':receipt['history_id']}
