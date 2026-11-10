"""Bounded, fail-closed native video JSON Schema and source-pointer controls.

This is deliberately an application subset, not a general JSON Schema engine.
Unknown assertions fail qualification. No schema code, remote refs or defaults run.
"""
from __future__ import annotations
from decimal import Decimal
from fractions import Fraction
import json
import math
import re


class SchemaError(ValueError):
    pass


ANNOTATIONS = {'title', 'description', 'default', 'examples', '$schema', '$id', '$comment',
               'readOnly', 'writeOnly', 'deprecated', 'contentMediaType', 'contentEncoding'}
ASSERTIONS = {'type', 'properties', 'required', 'additionalProperties', 'items', 'prefixItems',
    'minItems', 'maxItems', 'uniqueItems', 'minProperties', 'maxProperties', 'minLength', 'maxLength',
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'enum', 'const',
    'pattern', 'format', 'allOf', 'anyOf', 'oneOf', 'not', 'if', 'then', 'else', 'dependentRequired',
    'dependentSchemas', '$ref', '$defs', 'definitions', 'contains', 'minContains', 'maxContains'}
TYPES = {'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}
MAX_DEPTH = 32
MAX_NODES = 20000
MAX_INPUT_BYTES = 2 * 1024 * 1024
VIDEO_OUTPUT_TASKS = {'image-to-video', 'text-to-video', 'reference-to-video', 'video-edit', 'video-extend', 'upscale', 'character-animation', 'talking-avatar', 'video-translate', 'lip-sync'}
URI = re.compile(r'(?i)(?:[a-z][a-z0-9+.-]*://|data:|file:|javascript:|^//)')


def fail(reason='native schema/input is unsupported or invalid'):
    raise SchemaError(reason)


def parts(pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 2048:
        fail('source pointer must be a bounded RFC6901 pointer')
    tokens = pointer[1:].split('/')
    if any(re.search(r'~(?![01])', token) for token in tokens):
        fail('source pointer has invalid escaping')
    return [token.replace('~1', '/').replace('~0', '~') for token in tokens]


def escape(token):
    return str(token).replace('~', '~0').replace('/', '~1')


def pointer_get(value, pointer):
    for token in parts(pointer):
        if isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', token) or int(token) >= len(value):
                fail('source pointer does not identify an existing array slot')
            value = value[int(token)]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            fail('source pointer does not identify a declared input slot')
    return value


def pointer_set(value, pointer, replacement):
    tokens = parts(pointer)
    parent = value
    for token in tokens[:-1]:
        if isinstance(parent, list) and re.fullmatch(r'0|[1-9][0-9]*', token) and int(token) < len(parent):
            parent = parent[int(token)]
        elif isinstance(parent, dict) and token in parent:
            parent = parent[token]
        else:
            fail('source pointer container must already exist')
    token = tokens[-1]
    if isinstance(parent, list) and re.fullmatch(r'0|[1-9][0-9]*', token) and int(token) < len(parent):
        parent[int(token)] = replacement
    elif isinstance(parent, dict):
        parent[token] = replacement
    else:
        fail('source pointer cannot replace this input slot')


def _ref(schema, root):
    if '$ref' not in schema:
        return None
    reference = schema['$ref']
    if not isinstance(reference, str) or not reference.startswith('#/'):
        fail('only local schema references are supported')
    return pointer_get(root, reference[1:])


def qualify(schema):
    count = [0]
    def walk(node, depth=0, refs=()):
        count[0] += 1
        if depth > MAX_DEPTH or count[0] > MAX_NODES:
            fail('native schema exceeds structural limits')
        if type(node) is bool:
            return
        if not isinstance(node, dict):
            fail('native schema must contain schema objects')
        if '$id' in node:
            fail('schema resource identifiers and changed reference scopes are unsupported')
        unknown = set(node) - ANNOTATIONS - ASSERTIONS
        if any(not key.startswith('x-') for key in unknown):
            fail('native schema has unsupported assertion keywords')
        kind = node.get('type')
        if kind is not None and (not isinstance(kind, (str, list)) or kind == [] or
                any(item not in TYPES for item in (kind if isinstance(kind, list) else [kind]))):
            fail('native schema has unsupported types')
        if 'contentMediaType' in node:
            media = node['contentMediaType']
            choices = media if isinstance(media, list) else [media]
            if not choices or any(not isinstance(item, str) or not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', item) for item in choices):
                fail('native schema media declarations are invalid')
        if 'x-ui' in node and not isinstance(node['x-ui'], dict):
            fail('native schema UI annotations are invalid')
        if 'uniqueItems' in node and type(node['uniqueItems']) is not bool:
            fail('native schema uniqueItems must be Boolean')
        for key in ('minItems', 'maxItems', 'minProperties', 'maxProperties', 'minLength', 'maxLength', 'minContains', 'maxContains'):
            if key in node and (type(node[key]) is not int or node[key] < 0):
                fail('native schema has invalid bounds')
        for key in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'):
            if key in node and (type(node[key]) not in (int, float) or not math.isfinite(node[key]) or (key == 'multipleOf' and node[key] <= 0)):
                fail('native schema has invalid numeric bounds')
        for key in ('required',):
            if key in node and (not isinstance(node[key], list) or any(not isinstance(v, str) for v in node[key]) or len(set(node[key])) != len(node[key])):
                fail('native schema has invalid required fields')
        if 'enum' in node and (not isinstance(node['enum'], list) or not node['enum']):
            fail('native schema has invalid enum')
        if 'pattern' in node:
            try:
                if not isinstance(node['pattern'], str) or len(node['pattern']) > 2048:
                    fail('native schema pattern is unsupported')
                re.compile(node['pattern'])
            except re.error:
                fail('native schema pattern is unsupported')
        if node.get('format') not in (None, 'uri', 'uri-reference', 'url'):
            fail('native schema format is unsupported')
        if kind == 'array' and 'items' not in node and 'prefixItems' not in node:
            fail('native arrays require declared item schemas')
        if node.get('contentEncoding'):
            fail('inline encoded media is unsupported')
        for key in ('properties', '$defs', 'definitions', 'dependentSchemas'):
            if key in node:
                if not isinstance(node[key], dict):
                    fail('native schema has invalid property declarations')
                for child in node[key].values():
                    walk(child, depth + 1, refs)
        if 'dependentRequired' in node and (not isinstance(node['dependentRequired'], dict) or
                any(not isinstance(v, list) or any(not isinstance(k, str) for k in v) for v in node['dependentRequired'].values())):
            fail('native schema has invalid dependent requirements')
        for key in ('additionalProperties', 'items', 'contains', 'not', 'if', 'then', 'else'):
            if key in node:
                walk(node[key], depth + 1, refs)
        for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            if key in node:
                if not isinstance(node[key], list) or (key != 'prefixItems' and not node[key]):
                    fail('native schema has invalid conditional branches')
                for child in node[key]:
                    walk(child, depth + 1, refs)
        target = _ref(node, schema)
        if target is not None:
            if node['$ref'] in refs:
                fail('recursive native schemas are unsupported')
            walk(target, depth + 1, refs + (node['$ref'],))
    walk(schema)
    if not isinstance(schema, dict) or schema.get('type', 'object') != 'object':
        fail('native video input must be an object')
    return True


def _same(a, b):
    return json.dumps(a, sort_keys=True, separators=(',', ':'), allow_nan=False) == json.dumps(b, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate(schema, value):
    qualify(schema)
    def check(node, val, depth=0):
        if depth > MAX_DEPTH:
            fail('native input exceeds nesting limit')
        if type(node) is bool:
            if not node:
                fail()
            return
        target = _ref(node, schema)
        if target is not None:
            check(target, val, depth + 1)
        kind = node.get('type')
        kinds = kind if isinstance(kind, list) else [kind] if kind else []
        matches = {'object': isinstance(val, dict), 'array': isinstance(val, list), 'string': isinstance(val, str),
            'boolean': type(val) is bool, 'integer': type(val) is int,
            'number': type(val) in (int, float) and math.isfinite(val), 'null': val is None}
        if kinds and not any(matches[item] for item in kinds):
            fail('native input has wrong type')
        if 'enum' in node and not any(_same(val, option) for option in node['enum']):
            fail('native input is outside declared enum')
        if 'const' in node and not _same(val, node['const']):
            fail('native input differs from declared constant')
        if isinstance(val, dict):
            props = node.get('properties', {})
            if set(node.get('required', [])) - set(val):
                fail('native input is missing required fields')
            if len(val) < node.get('minProperties', 0) or len(val) > node.get('maxProperties', MAX_NODES):
                fail('native object count is outside bounds')
            # Undeclared input keys fail even when JSON Schema would permit them.
            # Conditional branches may add declarations; root application check handles that.
            for key, child in val.items():
                if key in props:
                    check(props[key], child, depth + 1)
                elif node.get('additionalProperties') is False:
                    fail('native input contains unknown fields')
                elif isinstance(node.get('additionalProperties'), dict):
                    check(node['additionalProperties'], child, depth + 1)
            for key, required in node.get('dependentRequired', {}).items():
                if key in val and set(required) - set(val):
                    fail('native dependent fields are missing')
            for key, child in node.get('dependentSchemas', {}).items():
                if key in val:
                    check(child, val, depth + 1)
        elif isinstance(val, list):
            if len(val) < node.get('minItems', 0) or len(val) > node.get('maxItems', MAX_NODES):
                fail('native array count is outside bounds')
            if node.get('uniqueItems') and len({json.dumps(item, sort_keys=True, allow_nan=False) for item in val}) != len(val):
                fail('native array items must be unique')
            prefix = node.get('prefixItems', [])
            for index, child in enumerate(val):
                if index < len(prefix):
                    check(prefix[index], child, depth + 1)
                elif 'items' in node:
                    check(node['items'], child, depth + 1)
            if 'contains' in node:
                count = sum(valid(node['contains'], child, depth + 1) for child in val)
                if count < node.get('minContains', 1) or count > node.get('maxContains', MAX_NODES):
                    fail('native array does not satisfy contains')
        elif isinstance(val, str):
            if len(val) < node.get('minLength', 0) or len(val) > node.get('maxLength', MAX_INPUT_BYTES):
                fail('native text length is outside bounds')
            if 'pattern' in node and not re.search(node['pattern'], val):
                fail('native text does not match declared pattern')
        elif type(val) in (int, float):
            if not math.isfinite(val):
                fail('native number must be finite')
            if val < node.get('minimum', val) or val > node.get('maximum', val):
                fail('native number is outside bounds')
            if ('exclusiveMinimum' in node and val <= node['exclusiveMinimum']) or ('exclusiveMaximum' in node and val >= node['exclusiveMaximum']):
                fail('native number is outside exclusive bounds')
            if 'multipleOf' in node and (Fraction(str(val)) / Fraction(str(node['multipleOf']))).denominator != 1:
                fail('native number is outside declared step')
        for child in node.get('allOf', []):
            check(child, val, depth + 1)
        for keyword, exact in (('anyOf', False), ('oneOf', True)):
            if keyword in node:
                count = sum(valid(child, val, depth + 1) for child in node[keyword])
                if count == 0 or (exact and count != 1):
                    fail('native conditional alternatives do not match')
        if 'not' in node and valid(node['not'], val, depth + 1):
            fail('native input matches a forbidden condition')
        if 'if' in node:
            branch = 'then' if valid(node['if'], val, depth + 1) else 'else'
            if branch in node:
                check(node[branch], val, depth + 1)
    def valid(node, val, depth):
        try:
            check(node, val, depth)
            return True
        except SchemaError:
            return False
    encoded = json.dumps(value, allow_nan=False).encode()
    if len(encoded) > MAX_INPUT_BYTES:
        fail('native input exceeds byte limit')
    check(schema, value)


def schemas_at(schema, pointer):
    """Return every declared schema for an input path, including conditional branches."""
    tokens = parts(pointer)
    def descend(node, remaining, depth=0):
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return []
        candidates = []
        target = _ref(node, schema)
        if target is not None:
            candidates += descend(target, remaining, depth + 1)
        if not remaining:
            candidates.append(node)
        else:
            token = remaining[0]
            child = node.get('properties', {}).get(token)
            if child is not None:
                candidates += descend(child, remaining[1:], depth + 1)
            if re.fullmatch(r'0|[1-9][0-9]*', token):
                index = int(token)
                prefix = node.get('prefixItems', [])
                child = prefix[index] if index < len(prefix) else node.get('items')
                if child is not None:
                    candidates += descend(child, remaining[1:], depth + 1)
        for key in ('allOf', 'anyOf', 'oneOf'):
            for child in node.get(key, []):
                candidates += descend(child, remaining, depth + 1)
        for key in ('then', 'else'):
            if key in node:
                candidates += descend(node[key], remaining, depth + 1)
        return candidates
    return descend(schema, tokens)


def materialize_defaults(schema, value, *, array_item_defaults_only=False):
    """Freeze declared defaults into the exact request; never select an alternative."""
    qualify(schema)
    result = json.loads(json.dumps(value, allow_nan=False))
    def apply(node, val, depth=0, in_array=False):
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return
        target = _ref(node, schema)
        if target is not None:
            apply(target, val, depth + 1, in_array)
        if isinstance(val, dict):
            for key, child in node.get('properties', {}).items():
                if key not in val and isinstance(child, dict) and 'default' in child and (not array_item_defaults_only or in_array):
                    val[key] = json.loads(json.dumps(child['default'], allow_nan=False))
                if key in val:
                    apply(child, val[key], depth + 1, in_array)
        elif isinstance(val, list):
            for index, child in enumerate(val):
                prefix = node.get('prefixItems', [])
                apply(prefix[index] if index < len(prefix) else node.get('items', {}), child, depth + 1, True)
        for child in node.get('allOf', []):
            apply(child, val, depth + 1, in_array)
    apply(schema, result)
    return result



MEDIA_MIMES = {
    'image': {'image/png', 'image/jpeg', 'image/webp', 'image/gif'},
    'video': {'video/mp4', 'video/webm'},
    'audio': {'audio/mpeg', 'audio/wav'},
}
SOURCE_ROLES = frozenset({'first_frame', 'last_frame', 'reference_image', 'reference_video',
    'reference_audio', 'source_video', 'source_audio', 'mask', 'keyframe',
    'character_reference', 'environment_reference'})


def media_types(node):
    value = node.get('contentMediaType', [])
    return value if isinstance(value, list) else [value] if isinstance(value, str) else []


def _walk_declarations(schema):
    """Visit input declarations, following qualified local refs without retrieval."""
    def walk(node, pointer='', required=False, array=None, depth=0):
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return
        target = _ref(node, schema)
        if target is not None:
            yield from walk(target, pointer, required, array, depth + 1)
        if pointer:
            yield pointer, node, required, array
        for key, child in node.get('properties', {}).items():
            yield from walk(child, pointer + '/' + escape(key), required and key in node.get('required', [])
                if pointer else key in node.get('required', []), None, depth + 1)
        if 'items' in node:
            yield from walk(node['items'], pointer + '/*', required, node, depth + 1)
        for index, child in enumerate(node.get('prefixItems', [])):
            yield from walk(child, pointer + '/' + str(index), required, node, depth + 1)
        for keyword in ('allOf', 'anyOf', 'oneOf'):
            for child in node.get(keyword, []):
                yield from walk(child, pointer, required, array, depth + 1)
        for keyword in ('then', 'else'):
            if keyword in node:
                yield from walk(node[keyword], pointer, False, array, depth + 1)
    yield from walk(schema)


def _role(field, kind):
    if 'mask' in field:
        return 'mask'
    if 'keyframe' in field:
        return 'keyframe'
    if 'last_' in field or 'end_' in field:
        return 'last_frame'
    if 'character' in field:
        return 'character_reference'
    if 'environment' in field:
        return 'environment_reference'
    if 'reference' in field:
        return 'reference_' + kind
    return {'image': 'first_frame', 'video': 'source_video', 'audio': 'source_audio'}[kind]



def _media_limits(*annotations):
    limits = {}
    for ui in annotations:
        for key in ('max_size_mb', 'max_duration_seconds', 'max_total_duration_seconds'):
            value = ui.get(key)
            if value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                fail('source UI limits must be finite positive numbers')
            limits[key] = min(value, limits.get(key, value))
    return limits


def analyze_schema(schema):
    """Describe finite media slots and controls; unsupported optional links stay isolated."""
    qualify(schema)
    media, links, controls = {}, {}, {}
    for pointer, node, required, array in _walk_declarations(schema):
        if node.get('type') not in (None, 'string'):
            continue
        field = parts(pointer)[-2] if pointer.endswith('/*') else parts(pointer)[-1]
        ui = node.get('x-ui', {})
        parent_ui = array.get('x-ui', {}) if array else {}
        mimes = media_types(node) or (media_types(array) if array else [])
        upload = ui.get('widget') in ('upload', 'multi-upload') or parent_ui.get('widget') in ('upload', 'multi-upload')
        # MIME annotations identify real file fields; arbitrary format:uri does not.
        kinds = {mime.split('/')[0] for mime in mimes if mime.split('/')[0] in MEDIA_MIMES}
        if upload and not kinds:
            accepted = ui.get('accept', parent_ui.get('accept', []))
            if isinstance(accepted, list):
                kinds = {mime.split('/')[0] for mime in accepted if isinstance(mime, str) and mime.split('/')[0] in MEDIA_MIMES}
            if not kinds:
                kinds = {'video' if 'video' in field else 'audio' if 'audio' in field else 'image'}
        if (upload or mimes) and len(kinds) == 1:
            kind = next(iter(kinds))
            allowed = set(mimes) & MEDIA_MIMES[kind] if mimes else set(MEDIA_MIMES[kind])
            accepted = ui.get('accept', parent_ui.get('accept'))
            if isinstance(accepted, list) and accepted:
                allowed &= set(accepted)
            if allowed:
                declaration = {'pointer': pointer, 'pointer_template': pointer, 'field': field,
                    'kind': kind, 'media_kind': kind, 'mimes': sorted(allowed), 'role': _role(field, kind),
                    'required': bool(required), 'min_items': (array or {}).get('minItems', 1 if required else 0),
                    'max_items': (array or {}).get('maxItems', 1 if array is None else None),
                    'limits': _media_limits(ui, parent_ui)}
                if pointer in media:
                    previous = media[pointer]
                    if previous['media_kind'] != kind or previous['role'] != declaration['role']:
                        fail('conditional media slot has ambiguous kind or role')
                    declaration['mimes'] = sorted(set(previous['mimes']) & set(declaration['mimes']))
                    if not declaration['mimes']:
                        fail('conditional media slot has incompatible MIME declarations')
                    declaration['required'] = declaration['required'] or previous['required']
                    declaration['min_items'] = max(previous['min_items'], declaration['min_items'])
                    maxima = [value for value in (previous['max_items'], declaration['max_items']) if value is not None]
                    declaration['max_items'] = min(maxima) if maxima else None
                    declaration['limits'] = {key: min(value for value in (previous['limits'].get(key), declaration['limits'].get(key)) if value is not None) for key in set(previous['limits']) | set(declaration['limits'])}
                media[pointer] = declaration
                continue
        external = (node.get('format') in ('uri', 'uri-reference', 'url') or bool(mimes)
                    or field.endswith(('_url', '_urls', '_uri', '_uris'))
                    or field in ('analysis_task_id', 'document_id', 'document_ids'))
        if external and not node.get('enum'):
            links[pointer] = {'pointer': pointer, 'field': field, 'required': bool(required),
                'reason': 'analysis provenance unavailable' if field == 'analysis_task_id' else 'external source workflow unsupported'}
        else:
            controls[pointer] = {'pointer': pointer, 'field': field, 'schema': node}
    blockers = [{'code': 'required_external_source', **item} for item in links.values() if item['required']]
    return {'media': list(media.values()), 'links': list(links.values()), 'controls': list(controls.values()),
        'blockers': blockers, 'timed_keyframes_supported': False}


def describe_route(schema):
    try:
        result = analyze_schema(schema)
        result['status'] = 'blocked' if result['blockers'] else 'partial' if result['links'] else 'supported'
        return result
    except (SchemaError, TypeError, ValueError, RecursionError):
        return {'status': 'blocked', 'media': [], 'links': [], 'controls': [],
            'blockers': [{'code': 'unsupported_schema', 'message': 'native schema cannot be safely represented'}]}


def qualify_route(item):
    from lib.spicy_catalog import digest
    schema = item.get('inputSchema') if isinstance(item, dict) else None
    tasks = item.get('tasks', []) if isinstance(item, dict) else []
    result = describe_route(schema)
    blockers = list(result['blockers'])
    if not isinstance(item, dict) or item.get('modality') != 'video' or len(tasks) != 1 or tasks[0] not in VIDEO_OUTPUT_TASKS:
        blockers.append({'code': 'nonvideo_output', 'message': 'operation has no supported video-output contract'})
    if not isinstance(item, dict) or item.get('enabled') is not True or item.get('available') is not True:
        blockers.append({'code': 'route_unavailable', 'message': 'route is not currently enabled and available'})
    return {**result, 'model': item.get('model') if isinstance(item, dict) else None,
        'mode': tasks[0].replace('-', '_') if len(tasks) == 1 else None,
        'task': tasks[0] if len(tasks) == 1 else None,
        'schema_sha256': digest(schema), 'roles': result['media'],
        'status': 'blocked' if blockers else result['status'], 'blockers': blockers, 'authority': 'none'}


def route_coverage(rows):
    return [qualify_route(row) for row in rows if row.get('modality') == 'video']


def media_declaration(schema, pointer):
    parts(pointer)
    matches = [row for row in analyze_schema(schema)['media'] if len(parts(row['pointer'])) == len(parts(pointer))
        and all(a == b or a == '*' and re.fullmatch(r'0|[1-9][0-9]*', b)
            for a, b in zip(parts(row['pointer']), parts(pointer)))]
    if not matches:
        fail('source pointer is not an explicitly declared media slot')
    # A duplicated conditional declaration must not widen MIME support.
    result = dict(matches[0])
    result['mimes'] = sorted(set.intersection(*(set(row['mimes']) for row in matches)))
    return result


def validate_payload(schema, payload, media_bindings=(), *, allow_placeholders=False):
    """Validate controls and bind every media URI to an explicit exact source slot."""
    qualify(schema)
    if not isinstance(payload, dict):
        fail('native input must be an object')
    bindings = {row['pointer']: row for row in media_bindings}
    if len(bindings) != len(media_bindings):
        fail('source pointer is bound more than once')
    analysis = analyze_schema(schema)
    forbidden = [row['pointer'] for row in analysis['links']]
    seen = set()
    def template_matches(template, pointer):
        left, right = parts(template), parts(pointer)
        return len(left) == len(right) and all(a == b or a == '*' and b.isdigit() for a, b in zip(left, right))
    def walk(value, pointer=''):
        if isinstance(value, dict):
            for key, child in value.items():
                slot = pointer + '/' + escape(key)
                declared = schemas_at(schema, slot)
                if not declared:
                    fail('native input contains undeclared fields')
                if any(node.get('x-ui', {}).get('widget') in ('hidden', 'chat-messages') for node in declared):
                    fail('hidden execution fields are unsupported')
                walk(child, slot)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, pointer + '/' + str(index))
        elif isinstance(value, str):
            if any(template_matches(template, pointer) for template in forbidden):
                fail('external links, weights and analysis IDs require an unsupported provenance workflow')
            if pointer in bindings:
                declaration = media_declaration(schema, pointer)
                binding = bindings[pointer]
                if binding.get('role') != declaration['role'] or binding.get('media_kind') != declaration['media_kind'] or binding.get('mime') not in declaration['mimes']:
                    fail('source role/type/MIME differs from its declared slot')
                expected = binding.get('uri')
                placeholder = '$upload:' + str(binding.get('source_sha256', binding.get('sha256', '')))
                if not (value == expected and isinstance(expected, str) and re.fullmatch(r'spicy://f/[A-Za-z0-9._/-]{1,500}', expected)
                    or allow_placeholders and value == placeholder and re.fullmatch(r'\$upload:[0-9a-f]{64}', value)):
                    fail('source URI differs from its journaled binding')
                seen.add(pointer)
            elif URI.search(value) or value.startswith('$upload:') or any(node.get('format') in ('uri', 'uri-reference', 'url') for node in schemas_at(schema, pointer)):
                fail('native URIs require hash-bound local source mappings')
            else:
                try:
                    media_declaration(schema, pointer)
                except SchemaError:
                    pass
                else:
                    fail('media slots must be filled by local source bindings')
    walk(payload)
    if seen != set(bindings):
        fail('source bindings do not match native input media slots')
    validate(schema, payload)
    return True
