"""Neutral Spicy account inspection and exact confirmed administrative purge.

Kept standalone in each native application. Authenticated calls go through a
fixed allowlist of named, read-only (plus confirmed download-link and purge)
methods on the application's official-SDK-backed client; there is no generic
method/path dispatcher.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import http.client
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


class AccountError(ValueError):
    """A fixed, sanitized administrative error."""


STATES = frozenset(('queued', 'running', 'succeeded', 'failed', 'expired', 'canceled'))
TERMINAL = STATES - {'queued', 'running'}
_TOKEN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,199}\Z')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*){0,7}\Z')
_MONEY = re.compile(r'-?\d{1,32}(?:\.\d{1,18})?\Z')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
MAX_JSON = 4 * 1024 * 1024
DOCS = (
    ('api-reference', 'API endpoint index', 'Native HTTP endpoints, authentication and response envelopes.', 'endpoints api openapi'),
    ('models', 'Model catalog', 'Model filters, native input schemas, availability and account pricing.', 'model schema image video audio text'),
    ('billing', 'Billing and usage', 'USD balance, key spending and date-based usage reports.', 'balance credit usage costs'),
    ('tasks', 'Asynchronous tasks', 'Key-scoped task history, states, polling and settlement.', 'task jobs history wait'),
    ('task-actions', 'Task actions', 'Retry creates a new paid original; task actions require explicit authority.', 'retry new paid original'),
    ('media', 'Media', 'Uploads, output links, saved assets and read-only characters.', 'download upload assets library characters'),
    ('retention', 'Retention and destruction', 'Retention expiry and permanent removal of task content.', 'purge delete retention'),
    ('idempotency', 'Idempotency', 'Same-key replay and uncertain original task acceptance.', 'duplicate retry recovery'),
    ('quotes-and-compatibility', 'Quotes and compatibility', 'Exact native request quotes and compatibility checks.', 'quote price expectedCost'),
    ('errors', 'Errors', 'Response codes and safe handling of failed or uncertain requests.', 'error refusal'),
    ('mcp', 'MCP tool reference', 'Vendor tool capabilities; native application governance still applies.', 'tools parity status docs'),
)


def _fail(message='Spicy administrative response is malformed'):
    raise AccountError(message) from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        _fail('Invalid administrative identifier')
    return value


def _model(value):
    if not isinstance(value, str) or len(value) > 256 or not _MODEL.fullmatch(value):
        _fail('Invalid model identifier')
    return value


def _text(value, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail('Invalid administrative filter')
    return value


def _limit(value, maximum=100):
    if type(value) is not int or not 1 <= value <= maximum:
        _fail('Administrative limit is outside its supported range')
    return value


def _cursor(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,2048}', value):
        _fail('Invalid pagination cursor')
    return value


def _dates(start, end):
    values = {}
    for key, value in (('from', start), ('to', end)):
        if value is not None:
            try:
                if not isinstance(value, str) or not _DATE.fullmatch(value):
                    raise ValueError()
                values[key] = date.fromisoformat(value)
            except ValueError:
                _fail('Dates must use YYYY-MM-DD in UTC')
    if len(values) == 2 and not 0 < (values['to'] - values['from']).days <= 92:
        _fail('Date range must be positive and no longer than 92 days')
    return {key: value.isoformat() for key, value in values.items()}


# Fixed neutral SDK surface. Generation, upload and asset writes are deliberately absent;
# the allowlist is checked before any attribute lookup on the client.
ACCOUNT_OPERATIONS = frozenset({
    'list_models', 'get_model', 'get_balance', 'get_usage', 'list_tasks', 'get_task',
    'list_assets', 'get_asset', 'asset_usage', 'list_characters', 'get_character',
    'create_download_url', 'purge_task',
})


def _sdk_result(client, operation, *args, **kwargs):
    """Call one named SDK-backed client operation; never echo exceptions or envelopes."""
    if operation not in ACCOUNT_OPERATIONS:
        _fail('Neutral account utilities cannot mutate provider content')
    try:
        method = getattr(client, operation, None)
        callable_method = callable(method)
    except Exception:
        _fail('Spicy administrative request failed; no automatic retry')
    if not callable_method:
        _fail('An authenticated native client is required')
    try:
        result = method(*args, **kwargs)
        data = result['data'] if isinstance(result, dict) else None
    except Exception:
        _fail('Spicy administrative request failed; no automatic retry')
    if not isinstance(data, dict):
        _fail()
    return data


def _sdk_dates(start, end):
    window = _dates(start, end)
    return {name: window[key] for key, name in (('from', 'from_date'), ('to', 'to_date')) if key in window}


def docs_search(search='', limit=10):
    """Search a small local index of first-party public guides, without a key."""
    _limit(limit, 25)
    words = _text(search).casefold().split() if search else []
    matches = []
    for slug, title, summary, keywords in DOCS:
        if all(word in ' '.join((title, summary, keywords)).casefold() for word in words):
            matches.append({'slug': slug, 'title': title, 'summary': summary,
                            'url': 'https://docs.spicyapi.ai/docs/' + slug})
    return {'source': 'local-first-party-guide-index', 'snapshot_date': '2026-10-22',
            'total': len(matches), 'items': matches[:limit]}


def _public_probe(path):
    if path not in ('/healthz', '/readyz'):
        _fail('Unsupported public status probe')
    connection = http.client.HTTPSConnection('api.spicyapi.ai', timeout=10)
    try:
        # Fixed origin/path, no redirect, no environment proxy and no credential.
        connection.request('GET', path, headers={'Accept': 'application/json'})
        response = connection.getresponse()
        status = response.status
        response.read(65537)
        return {'ok': 200 <= status < 300, 'status': status}
    except Exception:
        return {'ok': False, 'status': None}
    finally:
        connection.close()


def service_status(probe=None):
    probe = probe or _public_probe
    health, readiness = probe('/healthz'), probe('/readyz')
    safe = []
    for item in (health, readiness):
        if not isinstance(item, dict) or type(item.get('ok')) is not bool or (item.get('status') is not None and (type(item['status']) is not int or not 100 <= item['status'] <= 599)):
            _fail('Malformed public status probe')
        safe.append({'ok': item['ok'], 'status': item['status']})
    return {'operational': safe[0]['ok'] and (safe[1]['ok'] or safe[1]['status'] == 404),
            'health': safe[0], 'readiness': safe[1], 'authenticated': False}


def _safe_literal(value):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= 256 and not re.search(r'(?i)(https?://|spicy://|data:|bearer|sk-spicy-|[?&](?:token|signature|key)=)', value) and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        return value
    _fail('Unsupported catalog metadata')


def schema_summary(schema, depth=0):
    """Retain machine capability constraints; omit prose, examples and defaults.

    This is discovery metadata, never a replacement for owners' exact-schema guard.
    """
    if not isinstance(schema, dict) or depth > 20 or len(schema) > 256:
        _fail('Unsupported catalog schema')
    result = {}
    scalars = ('type', 'format', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
               'minLength', 'maxLength', 'minItems', 'maxItems', 'uniqueItems', 'multipleOf',
               'additionalProperties', 'contentEncoding', 'pattern', '$ref', 'const')
    for key in scalars:
        if key in schema and not isinstance(schema[key], (dict, list)):
            result[key] = _safe_literal(schema[key])
    for key in ('enum', 'required', 'contentMediaType', 'type'):
        if key in schema:
            value = schema[key]
            result[key] = [_safe_literal(item) for item in value] if isinstance(value, list) and len(value) <= 4096 else _safe_literal(value)
    for key in ('properties', '$defs', 'definitions'):
        if key in schema:
            if not isinstance(schema[key], dict) or len(schema[key]) > 256:
                _fail('Unsupported catalog schema')
            result[key] = {_text(name, 128): schema_summary(child, depth + 1) for name, child in schema[key].items()}
    for key in ('items', 'not', 'if', 'then', 'else', 'additionalProperties'):
        if key in schema and isinstance(schema[key], dict):
            result[key] = schema_summary(schema[key], depth + 1)
    for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
        if key in schema:
            if not isinstance(schema[key], list) or len(schema[key]) > 64:
                _fail('Unsupported catalog schema')
            result[key] = [schema_summary(child, depth + 1) for child in schema[key]]
    ui = schema.get('x-ui')
    if isinstance(ui, dict):
        result['x-ui'] = {key: _safe_literal(ui[key]) for key in ('widget', 'max_size_mb', 'max_duration_seconds', 'accept') if key in ui and not isinstance(ui[key], (dict, list))}
    return result


def _model_summary(data, include_schema=False):
    if not isinstance(data, dict):
        _fail()
    result = {'model': _model(data.get('model')), 'modality': data.get('modality')}
    if result['modality'] not in ('image', 'video', 'audio', 'text'):
        _fail()
    for key in ('enabled', 'available'):
        if key in data:
            if type(data[key]) is not bool:
                _fail()
            result[key] = data[key]
    for key in ('provider', 'version', 'availability', 'quantityField', 'toolkit'):
        if key in data:
            result[key] = _safe_literal(data[key])
    tasks = data.get('tasks', [])
    if not isinstance(tasks, list) or len(tasks) > 64:
        _fail()
    result['tasks'] = [_token(item) for item in tasks]
    for key in ('async',):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for key in ('taskTimeoutSeconds', 'maxOutputDurationSeconds'):
        if type(data.get(key)) is int and data[key] >= 0:
            result[key] = data[key]
    for key in ('badges', 'relatedModels'):
        if isinstance(data.get(key), list) and len(data[key]) <= 256:
            result[key] = [_safe_literal(value) for value in data[key]]
    # Prices can have nested native tiers; only validated numeric USD fields are
    # emitted. Arbitrary provider text and example prompts never reach stdout.
    for key in ('price', 'unitPrice', 'minPrice', 'maxPrice'):
        if isinstance(data.get(key), str) and _MONEY.fullmatch(data[key]):
            result[key] = data[key]
    if 'pricing' in data:
        if not isinstance(data['pricing'], list) or len(data['pricing']) > 4096:
            _fail()
        result['pricing'] = [_price_tier(tier) for tier in data['pricing']]
    if isinstance(data.get('startingPrice'), dict):
        result['startingPrice'] = _price_tier(data['startingPrice'])
    result['has_input_schema'] = isinstance(data.get('inputSchema'), dict) and bool(data['inputSchema'])
    if include_schema and result['has_input_schema']:
        result['inputSchema'] = schema_summary(data['inputSchema'])
        result['schema_view'] = 'constraints-only; prose/examples/defaults omitted; owner qualification required'
    return result


def _price_tier(tier):
    if not isinstance(tier, dict) or tier.get('currency') != 'USD' or not isinstance(tier.get('price'), str) or not _MONEY.fullmatch(tier['price']):
        _fail('Malformed catalog price tier')
    if tier.get('unit') not in ('per_image', 'per_second', 'per_request', 'per_1k_tokens'):
        _fail('Unsupported catalog billing unit')
    variant = tier.get('variant', '')
    if not isinstance(variant, str) or len(variant) > 1024 or not re.fullmatch(r'[A-Za-z0-9_.=;:/,+ -]*', variant) or '://' in variant:
        _fail('Unsupported catalog price variant')
    result = {'currency': 'USD', 'price': tier['price'], 'unit': tier['unit'], 'variant': variant}
    if isinstance(tier.get('regularPrice'), str) and _MONEY.fullmatch(tier['regularPrice']):
        result['regularPrice'] = tier['regularPrice']
    return result


def models(client, *, model=None, modality=None, provider=None, task=None, search=None, include_schema=True):
    if model is not None:
        chosen = _model(model)
        data = _sdk_result(client, 'get_model', chosen)
        result = _model_summary(data, include_schema)
        if result['model'] != chosen:
            _fail('Model response differs from requested model')
        return result
    query = {'include_schema': bool(include_schema)}
    if modality is not None:
        if modality not in ('image', 'video', 'audio', 'text'):
            _fail('Invalid catalog modality')
        query['modality'] = modality
    for key, value, validate in (('provider', provider, _text), ('task', task, _token), ('search', search, _text)):
        if value is not None:
            query[key] = validate(value)
    data = _sdk_result(client, 'list_models', **query)
    items = data.get('items')
    if not isinstance(items, list) or len(items) > 10000 or type(data.get('total')) is not int or data['total'] < 0:
        _fail()
    return {'total': data['total'], 'items': [_model_summary(item, include_schema) for item in items]}


def balance(client):
    data = _sdk_result(client, 'get_balance')
    result = {}
    for key in ('available', 'held', 'total'):
        value = data.get(key)
        if not isinstance(value, str) or not _MONEY.fullmatch(value) or key == 'held' and value.startswith('-'):
            _fail()
        result[key] = value
    return {'currency': 'USD', **result}


def _usage_row(row, grouping=None):
    if not isinstance(row, dict):
        _fail()
    result = {}
    for key in ('calls', 'succeeded', 'failed', 'totalCalls'):
        if key in row:
            if type(row[key]) is not int or row[key] < 0:
                _fail()
            result[key] = row[key]
    for key in ('spend', 'totalSpend'):
        if key in row:
            if not isinstance(row[key], str) or not _MONEY.fullmatch(row[key]):
                _fail()
            result[key] = row[key]
    if grouping == 'model':
        result['model'] = _model(row.get('model'))
    if grouping == 'date':
        if not isinstance(row.get('date'), str):
            _fail('Usage day requires a UTC date')
        result.update(_dates(row.get('date'), None))
        result['date'] = result.pop('from')
    return result


def usage(client, start=None, end=None):
    data = _sdk_result(client, 'get_usage', **_sdk_dates(start, end))
    if data.get('currency') != 'USD':
        _fail()
    result = {'scope': 'current-api-key', 'currency': 'USD', **_dates(data.get('from'), data.get('to')), **_usage_row(data)}
    for key, grouping in (('days', 'date'), ('models', 'model')):
        rows = data.get(key, [])
        if not isinstance(rows, list) or len(rows) > 10000:
            _fail()
        result[key] = [_usage_row(row, grouping) for row in rows]
    return result


def _task_summary(data):
    if not isinstance(data, dict) or data.get('state') not in STATES:
        _fail()
    result = {'taskId': _token(data.get('taskId')), 'state': data['state']}
    if 'model' in data:
        result['model'] = _model(data['model'])
    for key in ('cost', 'estimatedCost'):
        if key in data and data[key] is not None:
            if not isinstance(data[key], str) or not _MONEY.fullmatch(data[key]):
                _fail()
            result[key] = data[key]
    if 'settled' in data:
        if type(data['settled']) is not bool:
            _fail()
        result['settled'] = data['settled']
    for key in ('createdAt', 'deadlineAt', 'completedAt'):
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.+-]{8,24}Z?', value):
            result[key] = value
    assets = data.get('output', {}).get('assets', []) if isinstance(data.get('output'), dict) else []
    if not isinstance(assets, list) or len(assets) > 1000:
        _fail()
    result['assets'] = [_asset_summary(item, task_output=True) for item in assets]
    result['error_present'] = bool(data.get('error') or data.get('errorCode'))
    if data.get('contentState') in ('present', 'expired', 'purged'):
        result['contentState'] = data['contentState']
    result['private_content_omitted'] = True
    return result


def tasks(client, *, start=None, end=None, state=None, model=None, limit=20, cursor=None):
    query = {**_sdk_dates(start, end), 'limit': _limit(limit)}
    if state is not None:
        if state not in STATES:
            _fail('Invalid task state filter')
        query['state'] = state
    if model is not None:
        query['model'] = _model(model)
    if cursor is not None:
        query['cursor'] = _cursor(cursor)
    data = _sdk_result(client, 'list_tasks', **query)
    if not isinstance(data.get('items'), list) or len(data['items']) > limit or type(data.get('hasMore')) is not bool:
        _fail()
    result = {'scope': 'current-api-key', 'items': [_task_summary(item) for item in data['items']], 'hasMore': data['hasMore']}
    if data['hasMore']:
        result['nextCursor'] = _cursor(data.get('nextCursor'))
        result['pagination'] = 'reuse the exact same filters and dates with nextCursor'
    return result


def task(client, task_id):
    chosen = _token(task_id)
    data = _sdk_result(client, 'get_task', chosen)
    result = _task_summary(data)
    if result['taskId'] != chosen:
        _fail('Task response differs from requested original')
    return result


def wait_task(client, task_id, *, timeout=60, interval=5, clock=None, sleep=None):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 300:
        _fail('Wait timeout must be between zero and 300 seconds')
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 30:
        _fail('Wait interval must be between one and 30 seconds')
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    started = clock()
    maximum = math.ceil(timeout / interval) + 1
    for poll in range(maximum):
        result = task(client, task_id)
        remaining = timeout - (clock() - started)
        if result['state'] in TERMINAL or remaining <= 0 or poll == maximum - 1:
            return {'task': result, 'polls': poll + 1, 'timed_out': result['state'] not in TERMINAL}
        sleep(min(interval, remaining))
    _fail('Bounded wait exhausted')


def _asset_summary(data, task_output=False):
    if not isinstance(data, dict):
        _fail()
    result = {}
    for key in ('id', 'assetId', 'kind', 'source', 'contentType', 'mime', 'status'):
        if key in data and data[key] is not None:
            result[key] = _safe_literal(data[key])
    if 'key' in data:
        key = data['key']
        if key == '' and data.get('pending') is True:
            pass
        elif not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,2048}', key) or '..' in key.split('/'):
            _fail()
        else:
            result['key'] = key
    for key in ('bytes', 'width', 'height', 'count'):
        if type(data.get(key)) is int and data[key] >= 0:
            result[key] = data[key]
    for key in ('favorite', 'pending', 'unavailable'):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    result['private_content_omitted'] = True
    return result


def assets(client, *, asset_id=None, kind=None, source=None, favorite=None, collection_id=None, search=None, sort='recent', limit=100, cursor=None):
    if asset_id is not None:
        return {'scope': 'asset-library-workspace-or-personal', **_asset_summary(_sdk_result(client, 'get_asset', _token(asset_id)))}
    if sort not in ('recent', 'oldest', 'name', 'size') or kind not in (None, 'image', 'video', 'audio') or favorite is not None and type(favorite) is not bool:
        _fail('Invalid asset filter')
    query = {'sort': sort, 'limit': _limit(limit)}
    if kind:
        query['kind'] = kind
    if source is not None:
        values = source.split(',') if isinstance(source, str) else []
        if not values or set(values) - {'task_output', 'upload', 'url'}:
            _fail('Invalid asset source filter')
        query['source'] = source
    for key, value in (('collection_id', collection_id), ('cursor', cursor), ('q', search)):
        if value is not None:
            query[key] = _cursor(value) if key == 'cursor' else _text(value, 128)
    if favorite is not None:
        query['favorite'] = favorite
    data = _sdk_result(client, 'list_assets', **query)
    if not isinstance(data.get('items'), list) or len(data['items']) > limit:
        _fail()
    result = {'scope': 'asset-library-workspace-or-personal', 'items': [_asset_summary(row) for row in data['items']]}
    if data.get('nextCursor') is not None:
        result['nextCursor'] = _cursor(data['nextCursor'])
        result['pagination'] = 'reuse the exact same filters and sort with nextCursor'
    return result


def _numeric_tree(data, depth=0):
    if depth > 8:
        _fail()
    if isinstance(data, dict):
        return {_token(key): _numeric_tree(value, depth + 1) for key, value in data.items()
                if isinstance(key, str) and _TOKEN.fullmatch(key) and isinstance(value, (dict, int, float, bool))}
    if type(data) in (bool, int) or type(data) is float and math.isfinite(data):
        return data
    _fail()


def asset_usage(client):
    data = _sdk_result(client, 'asset_usage')
    # Names, workspace identifiers and arbitrary strings are deliberately absent.
    return {'scope': 'asset-library-workspace-or-personal', 'numeric_usage': _numeric_tree(data),
            'private_content_omitted': True}


def _character(data):
    if not isinstance(data, dict) or data.get('kind') not in (None, 'character'):
        _fail()
    result = {'id': _token(data.get('id')), 'kind': 'character', 'private_content_omitted': True}
    if type(data.get('count')) is int and data['count'] >= 0:
        result['count'] = data['count']
    if isinstance(data.get('assets'), list):
        result['assets'] = [_asset_summary(item) for item in data['assets']]
    return result


def characters(client, *, entity_id=None, asset_id=None, limit=50, cursor=None):
    if entity_id is not None:
        return _character(_sdk_result(client, 'get_character', _token(entity_id)))
    query = {'limit': _limit(limit)}
    if asset_id is not None:
        query['asset_id'] = _token(asset_id)
    if cursor is not None:
        query['cursor'] = _cursor(cursor)
    data = _sdk_result(client, 'list_characters', **query)
    if not isinstance(data.get('items'), list) or len(data['items']) > limit:
        _fail()
    result = {'scope': 'asset-library-workspace-or-personal', 'items': [_character(item) for item in data['items']]}
    if data.get('nextCursor') is not None:
        result['nextCursor'] = _cursor(data['nextCursor'])
    return result


def _plain_path(path, *, exists=True):
    path = Path(path).absolute()
    if '..' in path.parts:
        _fail('Administrative path traversal is forbidden')
    for item in (path, *path.parents):
        if item.is_symlink():
            _fail('Administrative paths cannot contain symlinks')
    if exists and not path.is_file():
        _fail('Administrative record must be an existing regular file')
    return path


def _exclusive_json(path, value, mode=0o400):
    path = _plain_path(path, exists=False)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            descriptor = None
            output.write(_canonical(value) + b'\n')
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        _fail('Administrative record already exists; immutable records cannot be overwritten')
    except OSError:
        _fail('Cannot retain private administrative record')
    finally:
        if descriptor is not None:
            os.close(descriptor)


def download_url(client, task_id, key, private_file, *, allow_private_url=False):
    if not allow_private_url:
        _fail('Explicit --allow-private-url is required for a private signed URL handoff')
    chosen = _token(task_id)
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,2048}', key) or '..' in key.split('/'):
        _fail('Invalid output asset key')
    path = _plain_path(private_file, exists=False)
    if path.exists() or not path.parent.is_dir():
        _fail('Private URL file must be new and have an existing owner-only parent')
    metadata = path.parent.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        _fail('Private URL handoff directory must be user-owned and owner-only')
    # Identity match is checked before minting a new secret-bearing URL.
    original = task(client, chosen)
    if original['state'] != 'succeeded' or key not in {item.get('key') for item in original['assets']}:
        _fail('Download URL requires an exact succeeded original and observed asset key')
    data = _sdk_result(client, 'create_download_url', chosen, key)
    try:
        url = data['url']
        parsed = urlsplit(url)
        valid = (isinstance(url, str) and len(url) <= 16384 and parsed.scheme == 'https' and parsed.hostname
                 and not parsed.username and not parsed.password and parsed.port in (None, 443) and not parsed.fragment
                 and not any(ord(c) < 32 or ord(c) == 127 for c in url) and data.get('key') == key)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        _fail('Malformed private download URL response')
    _exclusive_json(path, {'taskId': chosen, 'key': key, 'url': url}, 0o600)
    return {'private_url_written': True, 'taskId': chosen, 'key': key,
            'media_downloaded': False, 'handoff': 'Use the selected identity/project collector with qualified hosts, byte validation and provenance.'}


def _read_record(path):
    path = _plain_path(path)
    if path.stat().st_size > MAX_JSON:
        _fail('Administrative record exceeds its byte limit')
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError):
        _fail('Administrative record is unreadable')
    if not isinstance(value, dict):
        _fail('Administrative record must be an object')
    return value, hashlib.sha256(raw).hexdigest()


def _bound_original(owner_root, scope_kind, relative):
    root = _plain_path(owner_root, exists=False)
    if not root.is_dir() or scope_kind not in ('studio-identity', 'openmontage-project'):
        _fail('Select an existing identity or project root')
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        _fail('Original record must be relative to its selected owner')
    path = _plain_path(root / relative)
    if scope_kind == 'studio-identity':
        if not re.fullmatch(r'assets/generated/spicy-images/[a-f0-9]{32}/operation\.json', relative):
            _fail('Purge requires a canonical Studio original record')
    elif not re.fullmatch(r'production_attempts/[A-Za-z0-9_.:-]+/spicy_job\.json', relative):
        _fail('Purge requires a canonical OpenMontage original record')
    record, source_hash = _read_record(path)
    task_id = _token(record.get('task_id'))
    if scope_kind == 'studio-identity':
        if record.get('identity') != root.name or record.get('operation') != path.parent.name:
            _fail('Original identity binding differs from selected owner')
        credential = record.get('credential_fingerprint')
        model = _model(record.get('request', {}).get('model'))
        state = record.get('state')
    else:
        if record.get('attempt_id') != path.parent.name or record.get('acceptance_state') != 'accepted':
            _fail('Original attempt acceptance binding is missing')
        credential = record.get('credential_binding', {}).get('credential_digest')
        model = _model(record.get('native', {}).get('model'))
        state = record.get('task_state')
    if not isinstance(credential, str) or not _SHA.fullmatch(credential) or state not in TERMINAL:
        _fail('Purge preview requires a known terminal original with its credential binding')
    return {'record': relative, 'record_sha256': source_hash, 'taskId': task_id, 'model': model,
            'state': state, 'credential_binding': credential}


def _admin_dir(root, scope_kind):
    relative = 'assets/generated/spicy-admin' if scope_kind == 'studio-identity' else 'artifacts/spicy-admin'
    directory = _plain_path(Path(root) / relative, exists=False)
    # Persist each newly created path component before any remote mutation can
    # rely on the journal's name surviving power loss.
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for current in reversed(missing):
        current.mkdir(mode=0o700)
        parent_fd = os.open(current.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    metadata = directory.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        _fail('Administrative journal directory must be user-owned and owner-only')
    return directory


def purge_preview(owner_root, scope_kind, records, task_ids):
    if not isinstance(records, list) or not 1 <= len(records) <= 100 or len(records) != len(set(records)):
        _fail('Select one to 100 distinct canonical original records')
    originals = sorted([_bound_original(owner_root, scope_kind, record) for record in records], key=lambda row: row['taskId'])
    selected = sorted([_token(item) for item in task_ids])
    if selected != [row['taskId'] for row in originals] or len(selected) != len(set(selected)):
        _fail('Exact task batch differs from selected local originals')
    root = _plain_path(owner_root, exists=False)
    body = {'version': 1, 'action': 'purge', 'scope_kind': scope_kind,
            'owner_root_sha256': hashlib.sha256(str(root).encode()).hexdigest(), 'originals': originals,
            'effect': 'Permanently removes remote prompt, result and artifacts; billing retained; no refund; local originals retained.',
            'requests': [{'method': 'POST', 'path': '/api/v1/jobs/purge', 'body': {'taskId': item}} for item in selected],
            'remote_execution_supported': True}
    binding = _hash(body)
    preview = {**body, 'binding': binding}
    path = _admin_dir(root, scope_kind) / ('purge-preview-' + binding + '.json')
    _exclusive_json(path, preview)
    return {'binding': binding, 'task_ids': selected, 'count': len(selected), 'preview_record': str(path),
            'effect': body['effect'], 'remote_execution_supported': True}


def purge_authorize(owner_root, scope_kind, binding, confirmation_file, *, allow_delete=False):
    if not allow_delete:
        _fail('Explicit --allow-delete is required to retain exact-batch purge confirmation')
    if not isinstance(binding, str) or not _SHA.fullmatch(binding):
        _fail('Invalid purge preview binding')
    root = _plain_path(owner_root, exists=False)
    path = _admin_dir(root, scope_kind) / ('purge-preview-' + binding + '.json')
    preview, _ = _read_record(path)
    if preview.get('binding') != binding or _hash({key: value for key, value in preview.items() if key != 'binding'}) != binding or preview.get('scope_kind') != scope_kind or preview.get('owner_root_sha256') != hashlib.sha256(str(root).encode()).hexdigest():
        _fail('Purge preview binding changed')
    for original in preview['originals']:
        if _bound_original(root, scope_kind, original['record']) != original:
            _fail('Purge original changed since preview; retain a new exact preview')
    confirmation, confirmation_hash = _read_record(confirmation_file)
    task_ids = [row['taskId'] for row in preview['originals']]
    evidence = confirmation.get('evidence')
    if (confirmation.get('actor') != 'human' or confirmation.get('action') != 'purge' or confirmation.get('binding') != binding
            or confirmation.get('task_ids') != task_ids or not isinstance(evidence, str) or len(evidence.strip()) < 8
            or len(evidence) > 2000 or re.search(r'(?i)(://|bearer|sk-spicy-)', evidence)):
        _fail('Retain human confirmation of this exact purge binding and ordered task batch')
    authorization = {'version': 1, 'action': 'purge-confirmation-retained', 'binding': binding,
                     'task_ids': task_ids, 'confirmation_file_sha256': confirmation_hash,
                     'actor': 'human', 'evidence': evidence.strip(), 'allow_delete_interlock': True,
                     'remote_execution_supported': True}
    target = path.parent / ('purge-confirmation-' + binding + '-' + _hash(authorization) + '.json')
    _exclusive_json(target, authorization)
    return {'binding': binding, 'task_ids': task_ids, 'confirmation_retained': True,
            'authorization_record': str(target), 'remote_execution_supported': True, 'remote_deletion_attempted': False}


def _purge_context(client, owner_root, scope_kind, binding, authorization_record, original_resolver):
    if not callable(original_resolver):
        _fail('Destructive actions require the owning workflow canonical-original resolver')
    root = _plain_path(owner_root, exists=False)
    directory = _admin_dir(root, scope_kind)
    if not isinstance(binding, str) or not _SHA.fullmatch(binding):
        _fail('Invalid purge preview binding')
    preview, _ = _read_record(directory / ('purge-preview-' + binding + '.json'))
    if (preview.get('binding') != binding or _hash({key: value for key, value in preview.items() if key != 'binding'}) != binding
            or preview.get('scope_kind') != scope_kind or preview.get('owner_root_sha256') != hashlib.sha256(str(root).encode()).hexdigest()):
        _fail('Purge preview binding changed')
    authorization_path = _plain_path(authorization_record)
    authorization, _ = _read_record(authorization_path)
    if (authorization_path.parent != directory or authorization_path.name != 'purge-confirmation-' + binding + '-' + _hash(authorization) + '.json'
            or authorization.get('binding') != binding or authorization.get('actor') != 'human'
            or authorization.get('action') != 'purge-confirmation-retained' or authorization.get('allow_delete_interlock') is not True
            or authorization.get('task_ids') != [row['taskId'] for row in preview['originals']]):
        _fail('Exact retained human purge confirmation is missing or changed')
    credential = getattr(client, 'fingerprint', None)
    if hasattr(client, 'binding') and isinstance(client.binding, dict):
        credential = client.binding.get('credential_digest')
    for original in preview['originals']:
        if _bound_original(root, scope_kind, original['record']) != original or credential != original['credential_binding']:
            _fail('Purge original or original API key binding changed')
        try:
            proof = original_resolver(root, scope_kind, original['record'])
        except Exception:
            _fail('Owning workflow refused the canonical original binding')
        if not isinstance(proof, dict) or any(proof.get(key) != original[key] for key in ('taskId', 'model', 'state', 'credential_binding', 'record_sha256')):
            _fail('Canonical original proof differs from exact purge preview')
    return directory, preview, authorization


@contextmanager
def _admin_lock(directory):
    descriptor = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _purge_observation(client, original):
    observed = task(client, original['taskId'])
    if observed.get('model') != original['model'] or observed['state'] not in TERMINAL:
        _fail('Remote purge task/model/terminal state differs from selected original')
    return observed


def purge_execute(client, owner_root, scope_kind, binding, authorization_record, *, allow_delete=False, original_resolver=None, reconcile_only=False):
    """Delete only exact confirmed originals, once; lost POSTs use GET reconciliation.

    Immutable per-task intent precedes POST. An existing intent is never sent again,
    even after an uncertain response or a partial batch. Repeated calls inspect
    the same originals; logical purge does not prove physical media erasure.
    """
    if not allow_delete:
        _fail('Explicit --allow-delete is required for each purge execution or reconciliation')
    directory, preview, authorization = _purge_context(client, owner_root, scope_kind, binding, authorization_record, original_resolver)
    results = []
    with _admin_lock(directory):
        # Recheck all current canonical records inside the serialized boundary.
        _purge_context(client, owner_root, scope_kind, binding, authorization_record, original_resolver)
        for original in preview['originals']:
            task_id = original['taskId']
            suffix = binding + '-' + hashlib.sha256(task_id.encode()).hexdigest()
            intent_path = directory / ('purge-intent-' + suffix + '.json')
            observed = _purge_observation(client, original)
            if intent_path.exists() or reconcile_only:
                result = {'taskId': task_id, 'logical_purge_observed': observed.get('contentState') == 'purged',
                          'remote_deletion_attempted': False, 'physical_media_deletion': 'unverified',
                          'status': 'purge-observed' if observed.get('contentState') == 'purged' else 'requires-reconciliation'}
                event = {'action': 'purge-read-reconciliation', 'binding': binding, 'observation': result}
                event_path = directory / ('purge-observation-' + suffix + '-' + _hash(event) + '.json')
                if not event_path.exists():
                    _exclusive_json(event_path, event)
                results.append(result)
                if not result['logical_purge_observed'] and not reconcile_only:
                    break
                continue
            if observed.get('contentState') == 'purged':
                results.append({'taskId': task_id, 'logical_purge_observed': True, 'remote_deletion_attempted': False,
                                'physical_media_deletion': 'unverified', 'status': 'already-purged'})
                continue
            # A separate original/key/authority-bound durable intent for each task
            # prevents resubmission after a crash and preserves partial batches.
            intent = {'action': 'purge-dispatch-intent', 'binding': binding, 'original': original,
                      'authorization_sha256': _hash(authorization), 'method': 'POST',
                      'path': '/api/v1/jobs/purge', 'body': {'taskId': task_id}}
            _exclusive_json(intent_path, intent)
            try:
                data = _sdk_result(client, 'purge_task', task_id)
                if (data.get('taskId') != task_id or data.get('contentState') not in ('present', 'expired', 'purged')
                        or data.get('billingRetained') is not True or type(data.get('mediaDeletionPending')) is not bool):
                    _fail('Malformed purge response; reconcile the same original')
                result = {'taskId': task_id, 'logical_purge_observed': data['contentState'] == 'purged',
                          'billingRetained': True, 'mediaDeletionPending': data['mediaDeletionPending'],
                          'remote_deletion_attempted': True, 'physical_media_deletion': 'unverified',
                          'status': 'purge-observed' if data['contentState'] == 'purged' else 'requires-reconciliation'}
            except Exception:
                result = {'taskId': task_id, 'logical_purge_observed': False, 'remote_deletion_attempted': True,
                          'physical_media_deletion': 'unverified', 'status': 'acceptance-unknown-use-read-reconciliation'}
            _exclusive_json(directory / ('purge-result-' + suffix + '.json'), {'action': 'purge-dispatch-result', 'binding': binding, 'result': result})
            results.append(result)
            if result['status'] != 'purge-observed':
                # A partial/uncertain batch stops at its first unresolved task.
                break
    return {'binding': binding, 'results': results, 'original_count': len(preview['originals']),
            'all_logical_purges_observed': len(results) == len(preview['originals']) and all(row['logical_purge_observed'] for row in results),
            'physical_media_deletion': 'unverified', 'automatic_retry': False}


def retry_handoff(owner_root, scope_kind, record):
    original = _bound_original(owner_root, scope_kind, record)
    if original['state'] not in ('failed', 'expired', 'canceled'):
        _fail('Retry handoff requires a known failed or expired original')
    return {'action': 'new-paid-original-required', 'original': original,
            'binding': _hash(original), 'provider_retry_supported': False,
            'provider_retry_contract': {'method': 'POST', 'path': '/api/v1/jobs/retry', 'body_fields': ['taskId'], 'exact_quote_binding_supported': False},
            'requirements': ['Reconcile the existing original first.', 'Use the image or video owner to prepare a separately counted exact native request, fresh quote and retained paid authority.',
                             'Never use a new idempotency key to recover uncertain acceptance.'],
            'generation_attempted': False}


def register_cli(subparsers):
    account = subparsers.add_parser('account', help='account inspection and exact confirmed purge; no generation or upload')
    sub = account.add_subparsers(dest='account_command', required=True)
    sub.add_parser('status', help='unauthenticated public health/readiness probes')
    docs = sub.add_parser('docs', help='search local index of first-party public guides')
    docs.add_argument('search', nargs='?', default='')
    docs.add_argument('--limit', type=int, default=10)
    catalog = sub.add_parser('models')
    catalog.add_argument('--model')
    for flag in ('modality', 'provider', 'task', 'search'):
        catalog.add_argument('--' + flag)
    catalog.add_argument('--no-schema', action='store_true')
    sub.add_parser('balance')
    report = sub.add_parser('usage')
    history = sub.add_parser('tasks')
    for parser in (report, history):
        parser.add_argument('--from', dest='start')
        parser.add_argument('--to', dest='end')
    for flag in ('state', 'model', 'cursor'):
        history.add_argument('--' + flag)
    history.add_argument('--limit', type=int, default=20)
    for command in ('task', 'wait', 'download-url'):
        parser = sub.add_parser(command)
        parser.add_argument('task_id')
        if command == 'wait':
            parser.add_argument('--timeout', type=float, default=60)
            parser.add_argument('--interval', type=float, default=5)
        if command == 'download-url':
            parser.add_argument('--key', required=True)
            parser.add_argument('--private-file', type=Path, required=True)
            parser.add_argument('--allow-private-url', action='store_true')
    library = sub.add_parser('assets')
    for flag in ('asset-id', 'kind', 'source', 'collection-id', 'search', 'cursor'):
        library.add_argument('--' + flag)
    library.add_argument('--sort', default='recent')
    library.add_argument('--favorite', choices=('true', 'false'))
    library.add_argument('--limit', type=int, default=100)
    sub.add_parser('asset-usage')
    people = sub.add_parser('characters')
    for flag in ('entity-id', 'asset-id', 'cursor'):
        people.add_argument('--' + flag)
    people.add_argument('--limit', type=int, default=50)
    for command in ('purge-preview', 'purge-authorize', 'purge-execute', 'purge-reconcile', 'retry-handoff'):
        parser = sub.add_parser(command)
        parser.add_argument('--owner-root', type=Path, required=True)
        parser.add_argument('--scope-kind', choices=('studio-identity', 'openmontage-project'), required=True)
        if command == 'purge-preview':
            parser.add_argument('--record', action='append', required=True)
            parser.add_argument('--task-id', action='append', required=True)
        elif command in ('purge-authorize', 'purge-execute', 'purge-reconcile'):
            parser.add_argument('--binding', required=True)
            parser.add_argument('--confirmation-file' if command == 'purge-authorize' else '--authorization-record', type=Path, required=True)
            parser.add_argument('--allow-delete', action='store_true')
        else:
            parser.add_argument('--record', required=True)
    return account


def needs_client(args):
    return args.account_command not in ('status', 'docs', 'purge-preview', 'purge-authorize', 'retry-handoff')


def execute_cli(client, args, *, original_resolver=None):
    command = args.account_command
    if command == 'status':
        return service_status()
    if command == 'docs':
        return docs_search(args.search, args.limit)
    if command == 'models':
        return models(client, model=args.model, modality=args.modality, provider=args.provider, task=args.task, search=args.search, include_schema=not args.no_schema)
    if command == 'balance':
        return balance(client)
    if command == 'usage':
        return usage(client, args.start, args.end)
    if command == 'tasks':
        return tasks(client, start=args.start, end=args.end, state=args.state, model=args.model, limit=args.limit, cursor=args.cursor)
    if command == 'task':
        return task(client, args.task_id)
    if command == 'wait':
        return wait_task(client, args.task_id, timeout=args.timeout, interval=args.interval)
    if command == 'download-url':
        return download_url(client, args.task_id, args.key, args.private_file, allow_private_url=args.allow_private_url)
    if command == 'assets':
        return assets(client, asset_id=args.asset_id, kind=args.kind, source=args.source, collection_id=args.collection_id, search=args.search, sort=args.sort, favorite=None if args.favorite is None else args.favorite == 'true', limit=args.limit, cursor=args.cursor)
    if command == 'asset-usage':
        return asset_usage(client)
    if command == 'characters':
        return characters(client, entity_id=args.entity_id, asset_id=args.asset_id, limit=args.limit, cursor=args.cursor)
    if command == 'purge-preview':
        return purge_preview(args.owner_root, args.scope_kind, args.record, args.task_id)
    if command == 'purge-authorize':
        return purge_authorize(args.owner_root, args.scope_kind, args.binding, args.confirmation_file, allow_delete=args.allow_delete)
    if command in ('purge-execute', 'purge-reconcile'):
        return purge_execute(client, args.owner_root, args.scope_kind, args.binding, args.authorization_record,
                             allow_delete=args.allow_delete, original_resolver=original_resolver,
                             reconcile_only=command == 'purge-reconcile')
    if command == 'retry-handoff':
        return retry_handoff(args.owner_root, args.scope_kind, args.record)
    _fail('Unsupported account command')
