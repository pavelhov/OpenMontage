"""Frozen standing authority survives current package/approval drift; package-only binding never relaxes generic gates."""
import copy
import json

import pytest

from lib import production_autonomy as pa, production_boards as boards
from lib.shot_contract import factory_project, frozen_factory_authority, source_upstream_ids
from tests.lib.test_production_factory import standing_fixture, write

SHOT = {'asset_ids': [], 'upstream': [{'shot_id': 'S01'}]}


def _board(root):
    marker = json.loads((root / 'project.json').read_text())
    spec = {'image_nodes': [], 'story': write(root, 'test-story.json', {}),
            'approval': write(root, 'test-approval.json', {})}
    pack = {k: marker[k] for k in ('project_id', 'story_revision')}
    pack.update(specification=spec, specification_sha256=boards._digest(spec), prompt_pack={},
                package_binding=write(root, 'test-package.json', {}))
    write(root, boards.ARTIFACT, pack)
    return marker


def _activated(tmp_path):
    activated = pa.activate_standing_run(tmp_path, **standing_fixture(tmp_path))
    marker = _board(tmp_path)
    scope = {'story_revision': marker['story_revision'], 'requests': {'entry': {}},
             'derived_from_policy': {'policy_sha256': activated['policy_sha256'],
                                     'decision_id': activated['decision_id']}}
    return scope


@pytest.mark.parametrize('drifted', ['test-package.json', 'test-approval.json'])
def test_frozen_authority_survives_current_drift(tmp_path, drifted):
    scope = _activated(tmp_path)
    assert factory_project(tmp_path) is True
    assert frozen_factory_authority(tmp_path, scope) is True
    (tmp_path / drifted).write_text('changed current file')
    assert factory_project(tmp_path) is False
    assert frozen_factory_authority(tmp_path, scope) is True
    assert source_upstream_ids({'assets': []}, SHOT, tmp_path, factory=True) == []


@pytest.mark.parametrize('case', ['generic', 'empty', 'bad_decision', 'bad_policy'])
def test_unbound_scope_keeps_strict_upstream(tmp_path, case):
    scope = _activated(tmp_path)
    candidate = {'generic': {}, 'empty': {'derived_from_policy': {}}}.get(case)
    if candidate is None:
        candidate = copy.deepcopy(scope)
    if case == 'bad_decision':
        candidate['derived_from_policy']['decision_id'] = 'unknown'
    if case == 'bad_policy':
        candidate['derived_from_policy']['policy_sha256'] = '0' * 64
    assert frozen_factory_authority(tmp_path, candidate) is False
    assert source_upstream_ids({'assets': []}, SHOT, tmp_path, factory=False) == ['S01']


def test_package_binding_without_activation_is_not_factory(tmp_path):
    standing_fixture(tmp_path)
    _board(tmp_path)
    assert factory_project(tmp_path) is False
