"""Offline candidate discovery never establishes live production readiness."""
import hashlib
import json
from pathlib import Path

from lib.openart_catalog import read_observed_catalog
from lib import openart_jobs as jobs
from tools import _openart_cli as cli
from tests.lib.test_openart_jobs import captured


def test_missing_catalog_root_does_not_initialize_state(tmp_path, monkeypatch):
    state = tmp_path / 'absent'
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(state))
    monkeypatch.setattr(cli, '_run_checked', lambda *a, **k: (_ for _ in ()).throw(AssertionError('CLI forbidden')))
    assert read_observed_catalog() == {}
    assert not state.exists()


def retain_index(tmp_path, monkeypatch, *, argv_model='synthetic-video', form_model='synthetic-video'):
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'private'))
    version = captured('version', ['version'], {'version': '0.1.1'})
    catalog = captured('catalog', ['model', 'list'], [{'id': 'synthetic-video', 'modes': {
        'video': [{'mode': 'image2video'}, {'mode': 'element2video'}]}}])
    form = captured('form', ['model', 'form', argv_model, 'image2video'], {
        'model': form_model, 'mode': 'image2video', 'media': 'video', 'schema': {'type': 'object',
        'properties': {'prompt': {'type': 'string'}, 'startFrame': {'type': 'string'},
                       'resolution': {'type': 'string', 'default': 'https://private.example/?secret=PRIVATE'}},
        'required': ['prompt', 'startFrame']}})
    index = {'version': '1', 'cli_version_receipt': version, 'catalog': catalog,
             'forms': [{'model': 'synthetic-video', 'mode': 'image2video', **form}]}
    target = cli.private_dir('discovery') / 'index.json'
    cli.write_private(target, json.dumps(index).encode())
    monkeypatch.setattr(cli, '_run_checked', lambda *a, **k: (_ for _ in ()).throw(AssertionError('CLI forbidden')))
    return target, index


def test_observed_candidates_keep_missing_forms_visible_and_never_ready(tmp_path, monkeypatch):
    target, _ = retain_index(tmp_path, monkeypatch)
    before = sorted((p.relative_to(target.parent.parent), p.read_bytes()) for p in target.parent.parent.rglob('*') if p.is_file())
    result = read_observed_catalog()
    modes = result['synthetic-video']['modes']
    assert set(modes) == {'image2video', 'element2video'}
    assert modes['image2video']['inspected'] is True
    assert modes['image2video']['transport_supported'] is False
    assert 'CLI_wire_type_incompatible_startFrame' in modes['image2video']['reason']
    assert modes['element2video']['reason'] == 'form_receipt_missing'
    assert all(mode['production_ready'] is False for mode in modes.values())
    assert 'PRIVATE' not in json.dumps(result) and 'https://' not in json.dumps(result)
    assert before == sorted((p.relative_to(target.parent.parent), p.read_bytes()) for p in target.parent.parent.rglob('*') if p.is_file())


def test_unmatched_form_metadata_and_argv_cannot_establish_inspection(tmp_path, monkeypatch):
    retain_index(tmp_path, monkeypatch, argv_model='other')
    assert read_observed_catalog()['synthetic-video']['modes']['image2video']['inspected'] is False


def test_drifted_catalog_receipt_fails_closed(tmp_path, monkeypatch):
    _, index = retain_index(tmp_path, monkeypatch)
    path = cli.receipt_path(index['catalog']['receipt_id'])
    path.write_bytes(path.read_bytes() + b' ')
    assert read_observed_catalog() == {}
