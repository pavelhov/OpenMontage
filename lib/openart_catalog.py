"""Pure observed OpenArt video inventory from verified retained transport receipts.

An observation is never qualification or dispatch authority. This module never
calls a CLI, initializes private state, writes files, or constructs a ledger.
"""
from __future__ import annotations

import hashlib
import json
from lib import openart_jobs as jobs
from lib.openart_controls import mode_capabilities, public_capabilities
from tools import _openart_cli as cli


def _record(binding, argv):
    record = jobs._qual_record(binding, 'discovery', 'catalog_invalid')
    if record['argv'] != argv + cli.GLOBAL_FLAGS:
        raise cli.OpenArtCLIError('catalog_invalid', 'discovery receipt argv differs')
    return record['parsed']


def _public_caps(caps):
    # Discovery menus publish schema structure and bindings, not arbitrary
    # provider defaults/examples/enums which can contain secret URL query bytes.
    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, child in value.items():
            if key in ('enum', 'default', 'const', 'examples', 'example', 'description', 'title', 'pattern', 'url'):
                if child is not None:
                    result[key + '_sha256'] = hashlib.sha256(json.dumps(child, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            else:
                result[key] = walk(child)
        return result
    return walk(public_capabilities(caps))


def read_observed_catalog():
    """Return model→mode candidates; missing/invalid discovery index returns empty.

    Each catalog-advertised pair remains visible even when its form is absent or
    unsupported. Only validated form receipts establish `inspected`. Even a
    transport-supported candidate has `production_ready=False`.
    """
    try:
        root = cli.state_dir(create=False)
        index_path = root / 'discovery' / 'index.json'
        if (not root.exists() or not index_path.is_file()
                or not (root / 'receipts').is_dir() or not (root / 'streams').is_dir()):
            return {}
        cli.verify_private_state()
        cli._check_private(index_path, False)
        index = json.loads(index_path.read_text())
        if index.get('version') != '1':
            return {}
        catalog = _record(index['catalog'], ['model', 'list'])
        if not isinstance(catalog, list):
            return {}
        observed_version = None
        if index.get('cli_version_receipt'):
            version = _record(index['cli_version_receipt'], ['version'])
            if isinstance(version, dict) and isinstance(version.get('version'), str):
                observed_version = version['version']
        forms = {}
        for item in index.get('forms', []):
            pair = (item['model'], item['mode'])
            if pair in forms:
                return {}  # ambiguity cannot select a current form silently
            forms[pair] = item
        result = {}
        for row in catalog:
            model = row.get('id')
            modes = row.get('modes', {}).get('video', [])
            if not isinstance(model, str) or not isinstance(modes, list):
                continue
            cli._safe_part(model)
            if model in result:
                return {}
            entry = {'provider': 'openart_cli', 'catalog_receipt': index['catalog'], 'modes': {}}
            for advertised in modes:
                mode = advertised.get('mode')
                if not isinstance(mode, str):
                    continue
                cli._safe_part(mode)
                if mode in entry['modes']:
                    return {}
                candidate = {'model': model, 'mode': mode, 'advertised': True,
                    'inspected': False, 'transport_supported': None, 'production_ready': False,
                    'qualification_required': True, 'cli_version': observed_version,
                    'reason': 'form_receipt_missing', 'native_capabilities': None}
                ref = forms.get((model, mode))
                if ref is not None:
                    try:
                        form = _record(ref, ['model', 'form', model, mode])
                        # Parse exact form metadata even if transport version is unknown.
                        cli.form_root_schema(form, model=model, mode=mode)
                        candidate.update(inspected=True, form_receipt={k: ref[k] for k in ('receipt_id', 'receipt_sha256')})
                        if observed_version is None:
                            candidate['reason'] = 'version_receipt_missing'
                        else:
                            caps = mode_capabilities(form, model=model, mode=mode, cli_version=observed_version,
                                                     element_types=advertised.get('elementTypes'))
                            candidate.update(native_capabilities=_public_caps(caps),
                                transport_supported=not caps['unreachable'],
                                reason=caps.get('unreachable_reason') or 'observed_not_production_qualified')
                    except (cli.OpenArtCLIError, ValueError, KeyError, TypeError) as exc:
                        candidate['reason'] = getattr(exc, 'kind', 'form_receipt_invalid')
                entry['modes'][mode] = candidate
            if entry['modes']:
                result[model] = entry
        return result
    except (OSError, ValueError, KeyError, TypeError, cli.OpenArtCLIError):
        return {}


def retain_discovery_observation(*, catalog=None, form=None, cli_version_receipt=None):
    """Publish a private receipt index after registered read-only account calls.

    This explicit writer is separate from metadata discovery. It validates every
    supplied reference, serializes publication under the transport lock and does
    not create qualification, authorization, a ledger or generation reservation.
    """
    import os
    import uuid
    def ref(value):
        return {key: value[key] for key in ('receipt_id', 'receipt_sha256')}
    if catalog is not None:
        parsed = _record(catalog, ['model', 'list'])
        if not isinstance(parsed, list):
            raise cli.OpenArtCLIError('catalog_invalid', 'catalog must be an observed model list')
    if cli_version_receipt is not None:
        observed = _record(cli_version_receipt, ['version'])
        if not isinstance(observed, dict) or not isinstance(observed.get('version'), str):
            raise cli.OpenArtCLIError('catalog_invalid', 'version receipt shape is unsupported')
    if form is not None:
        model, mode = cli._ident(form.get('model'), 'model'), cli._ident(form.get('mode'), 'mode')
        parsed = _record(form, ['model', 'form', model, mode])
        cli.form_root_schema(parsed, model=model, mode=mode)
    with cli.transport_lock():
        directory = cli.private_dir('discovery')
        target = directory / 'index.json'
        value = {'version': '1', 'forms': []}
        if target.exists():
            cli._check_private(target, False)
            value = json.loads(target.read_text())
            if value.get('version') != '1' or not isinstance(value.get('forms'), list):
                raise cli.OpenArtCLIError('catalog_invalid', 'retained discovery index is malformed')
        if catalog is not None:
            value['catalog'] = ref(catalog)
        if cli_version_receipt is not None:
            value['cli_version_receipt'] = ref(cli_version_receipt)
        if form is not None:
            value['forms'] = [item for item in value['forms'] if (item['model'], item['mode']) != (model, mode)]
            value['forms'].append({'model': model, 'mode': mode, **ref(form)})
            value['forms'].sort(key=lambda item: (item['model'], item['mode']))
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        temporary = directory / ('index-' + uuid.uuid4().hex + '.tmp')
        try:
            cli.write_private(temporary, raw)
            os.replace(temporary, target)
            cli.fsync_dir(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return {'index_sha256': hashlib.sha256(raw).hexdigest(), 'qualification': 'observed_not_dispatch_authority'}


def observed_element_types(model, mode):
    """Join only verified catalog-advertised element types for the exact pair."""
    try:
        root = cli.state_dir(create=False)
        if not (root / 'discovery/index.json').is_file() or not (root / 'receipts').is_dir() or not (root / 'streams').is_dir():
            return None
        cli.verify_private_state()
        path = root / 'discovery/index.json'; cli._check_private(path, False)
        index = json.loads(path.read_text())
        catalog = _record(index['catalog'], ['model', 'list'])
        matches = [m for row in catalog if row.get('id') == model
                   for m in row.get('modes', {}).get('video', []) if m.get('mode') == mode]
        if len(matches) != 1:
            return None
        types = matches[0].get('elementTypes')
        return types if isinstance(types, list) and all(t in {'image', 'video', 'audio'} for t in types) else None
    except (cli.OpenArtCLIError, OSError, ValueError, KeyError, TypeError):
        return None
