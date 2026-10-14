"""Offline actual-observation shapes and narrowly ordered payoff preparation."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib import production_execution as execution, production_images as images, production_boards as boards
from lib import production_request as request
from lib.shot_contract import file_sha256, PROJECT_PREDICATES, SHOT_PREDICATES, project_review_subject, shot_review_subject
from tests.lib.test_production_images import board_package, reserve, host_receipt, PNG  # noqa: F401
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_production_package_authoring import packages
from tests.lib.test_production_factory import board_factory_fixture, retain_factory_board, file_bytes, write
from tests.integration.test_first_pass_workflow import attestation
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401


def observation(root, envelope, **kwargs):
    pointer = host_receipt(root, envelope, **kwargs)
    return json.loads((root / pointer['path']).read_text()), pointer


@pytest.mark.parametrize('inline', [False, True])
def test_receipt_action_pointer_inline_and_exact_replay(board_package, inline):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='observed')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    observed, pointer = observation(root, envelope)
    receipt = observed if inline else pointer
    result = execution.complete_action(root, action, outcome={'host_receipt': receipt})
    assert result['action']['kind'] != 'begin_native_image' and 'host_envelope' not in result
    imported = images.image_call_status(root, call['call_id'])
    assert imported['state'] == 'imported' and (root / target).read_bytes() == PNG
    retained = root / imported['host_receipt']['path']
    before = file_bytes(root); usage = images.image_usage(root)
    replay = execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert replay['replayed'] and replay['provider_calls'] == 0 and 'host_envelope' not in replay
    assert file_bytes(root) == before and images.image_usage(root) == usage
    assert retained.read_bytes() == before[retained.relative_to(root)]
    assert imported['provenance']['receipt_sha256'] == file_sha256(retained)
    if not inline:
        # Switching to inline replay never normalizes an existing pointer receipt.
        assert retained.read_bytes() == (root / pointer['path']).read_bytes()


@pytest.mark.parametrize('damage', ['tool', 'arguments', 'missing_arguments', 'wrapper', 'mixed', 'changed_replay', 'stale_action'])
def test_inline_receipt_rejects_without_retention(board_package, damage):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='observed')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    observed, _ = observation(root, envelope)
    if damage == 'tool': observed['tool_name'] = 'another.host'
    elif damage == 'arguments': observed['arguments']['prompt'] = 'Different observed invocation'
    elif damage == 'missing_arguments': del observed['arguments']
    elif damage == 'wrapper': observed = observed['result']
    elif damage == 'mixed': observed['path'] = 'receipts/unrelated.json'
    elif damage == 'changed_replay':
        execution.complete_action(root, action, outcome={'host_receipt': observed})
        observed['result']['output_hint'] = 'Changed actual return'
    elif damage == 'stale_action': action['binding']['request_sha256'] = '0'*64
    before = file_bytes(root); usage = images.image_usage(root)
    with pytest.raises(ValueError):
        execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert file_bytes(root) == before and images.image_usage(root) == usage


def test_inline_crash_after_receipt_retention_restores_original(board_package, monkeypatch):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='interrupted')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    observed, _ = observation(root, envelope)
    before_usage = images.image_usage(root)
    with monkeypatch.context() as patch:
        def interrupt(*args, **kwargs):
            raise RuntimeError('SYNTHETIC crash after receipt bytes, before journal write')
        patch.setattr(images, '_write', interrupt)
        with pytest.raises(RuntimeError):
            execution.complete_action(root, action, outcome={'host_receipt': observed})
    retained = root / 'production_images/interrupted/host-return.json'
    frozen = retained.read_bytes()
    assert images.image_call_status(root, call['call_id'])['state'] == 'submitted'
    assert images.image_usage(root) == before_usage
    resumed = execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert retained.read_bytes() == frozen and (root / target).read_bytes() == PNG
    assert 'host_envelope' not in resumed and images.image_usage(root)['total'] == 1
    assert len([e for e in images.image_call_status(root, call['call_id'])['events'] if e['kind'] == 'tool_return']) == 1


@pytest.mark.parametrize('interrupt_completion', [False, True])
def test_host_helper_actual_receipt_shape_completes_canonical_original(board_package, interrupt_completion):
    """Synthetic host only: real JS saver -> Python receipt admission/completion."""
    root, _, _, target = board_package
    call = reserve(board_package, call_id='host-context')
    step = execution.advance(root)
    assert step['host_envelope']['call_id'] == call['call_id']
    step_path = root / 'host-step.json'; step_path.write_text(json.dumps(step))
    image_path = root / '.codex/generated_images/synthetic-host.png'
    image_path.parent.mkdir(parents=True); image_path.write_bytes(PNG)
    engine = Path(__file__).resolve().parents[2]
    script = r"""
const fs = require('node:fs/promises'), path = require('node:path');
const {execFileSync} = require('node:child_process');
const [engine, python, root, stepPath, imagePath, interrupted] = process.argv.slice(1);
const {consumeNativeHostAction} = require(path.join(engine, 'scripts/native_host_receipt.js'));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  const step = JSON.parse(await fs.readFile(stepPath, 'utf8')), observations = new Map();
  let nativeCalls = 0, completed = 0;
  const tools = {mcp__node_repl__js: async request => {
    let output;
    await new AsyncFunction('nodeRepl', request.code)({write: value => output = value});
    return {content: [{type: 'text', text: output}]};
  }};
  const options = {
    nativeTool: {tool_name: 'image_gen.imagegen', invoke: async args => {
      nativeCalls++;
      return {image_url: 'data:image/png;base64,' + (await fs.readFile(imagePath)).toString('base64'),
        output_hint: `Synthetic fixture saved as ${imagePath} by default.`, additive_field: {kept: ['complete', 'return']}};
    }}, receiptPath: path.join(root, 'actual-host-return.json'),
    getObservation: key => observations.get(key), retainObservation: (key, value) => observations.set(key, value),
    completeAction: async (action, outcome) => {
      const code = "import json,sys; from lib import production_execution as e; p=json.loads(sys.argv[2]); print(json.dumps(e.complete_action(sys.argv[1],p['action'],outcome=p['outcome'])))";
      const result = JSON.parse(execFileSync(python, ['-c', code, root, JSON.stringify({action, outcome})], {cwd: engine, encoding: 'utf8'}));
      completed++;
      if (interrupted === 'true' && completed === 1) throw new Error('Synthetic lost completion response');
      return result;
    },
  };
  let result;
  try { result = await consumeNativeHostAction(tools, step, options); }
  catch (error) {
    if (error.message !== 'Synthetic lost completion response') throw error;
    result = await consumeNativeHostAction(tools, step, options);
  }
  console.log(JSON.stringify({nativeCalls, completed, result}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    observed = json.loads(subprocess.run(['node', '-e', script, str(engine), sys.executable,
        str(root), str(step_path), str(image_path), str(interrupt_completion).lower()],
        check=True, capture_output=True, text=True).stdout)
    assert observed['nativeCalls'] == 1
    assert observed['result']['provider_calls'] is None
    assert observed['completed'] == (2 if interrupt_completion else 1)
    assert 'host_envelope' not in observed['result']
    if interrupt_completion:
        assert observed['result']['replayed']
    imported = images.image_call_status(root, call['call_id'])
    assert imported['state'] == 'imported' and (root / target).read_bytes() == PNG
    receipt = json.loads((root / imported['host_receipt']['path']).read_text())
    assert receipt['arguments'] == step['host_envelope']['arguments']
    assert receipt['result']['additive_field'] == {'kept': ['complete', 'return']}
    assert images.image_usage(root)['total'] == 1
    resumed = execution.advance(root)
    assert 'host_envelope' not in resumed


def test_host_helper_openart_structured_return_completes_canonical_original(lifecycle):
    """Actual connector wrapper shape, synthetic callback, canonical original."""
    from lib import production_autonomy as autonomy
    root, policy, _, inputs, _, _, jobs = lifecycle
    jobs.prepare(root, attempt_id='host-openart', generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    step = execution.advance(root)
    assert step['action']['kind'] == 'receive_openart_original'
    step_path = root / 'host-step.json'; step_path.write_text(json.dumps(step))
    engine = Path(__file__).resolve().parents[2]
    script = r"""
const fs = require('node:fs/promises'), path = require('node:path');
const {execFileSync} = require('node:child_process');
const [engine, python, root, stepPath] = process.argv.slice(1);
const {consumeNativeHostAction} = require(path.join(engine, 'scripts/native_host_receipt.js'));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  const step = JSON.parse(await fs.readFile(stepPath, 'utf8')), observations = new Map();
  const raw = {content: [{type: 'text', text: 'Synthetic exact connector return'}],
    structuredContent: {historyId: 'synthetic-actual-original', status: 'PENDING', additive: {kept: true}},
    additive_host_field: ['retained', 'losslessly']};
  let nativeCalls = 0;
  const tools = {mcp__node_repl__js: async request => {
    let output; await new AsyncFunction('nodeRepl', request.code)({write: value => output = value});
    return {content: [{type: 'text', text: output}]};
  }};
  const receiptPath = path.join(root, 'actual-openart-return.json');
  const result = await consumeNativeHostAction(tools, step, {
    nativeTool: {tool_name: step.host_envelope.tool, invoke: async () => {nativeCalls++; return raw;}},
    receiptPath, getObservation: key => observations.get(key),
    retainObservation: (key, value) => observations.set(key, value),
    completeAction: async (action, outcome) => {
      const payload = outcome.result.structuredContent ?? outcome.result;
      if (typeof payload?.historyId !== 'string' || !payload.historyId) throw new Error('No actual original historyId');
      const code = "import json,sys; from lib import production_execution as e, openart_mcp as m; m._ALLOW_FIXTURE_PRODUCTION=True; p=json.loads(sys.argv[2]); print(json.dumps(e.complete_action(sys.argv[1],p['action'],outcome=p['outcome'])))";
      return JSON.parse(execFileSync(python, ['-c', code, root,
        JSON.stringify({action, outcome: {result: payload}})], {cwd: engine, encoding: 'utf8'}));
    },
  });
  const retained = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
  console.log(JSON.stringify({nativeCalls, result, retained, raw}));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    actual = json.loads(subprocess.run(['node', '-e', script, str(engine), sys.executable,
        str(root), str(step_path)], check=True, capture_output=True, text=True).stdout)
    assert actual['nativeCalls'] == 1 and actual['result']['provider_calls'] is None
    assert actual['retained']['result'] == actual['raw']
    assert actual['retained']['arguments'] == step['host_envelope']['arguments']
    state = jobs._load(root, 'host-openart')
    assert state['receipt'] == actual['raw']['structuredContent']
    assert autonomy.root_attempt_counts(root, policy)['total'] == 1
    assert jobs.poll_args(root, 'host-openart')['arguments'] == {'historyId': 'synthetic-actual-original'}
    assert 'host_envelope' not in execution.advance(root)

@pytest.mark.parametrize('original_inline', [False, True])
def test_inline_unknown_return_and_additive_supplement(board_package, original_inline):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='supplement')
    envelope = images.begin_native_image(root, call['call_id'])
    action = execution.next_production_action(root)
    partial, partial_pointer = observation(root, envelope, name='partial', result={'output_hint': 'Synthetic fixture'})
    with pytest.raises(ValueError, match='unresolved'):
        execution.complete_action(root, action, outcome={'host_receipt': partial if original_inline else partial_pointer})
    original = images.image_call_status(root, call['call_id'])
    original_raw = (root / original['host_receipt']['path']).read_bytes()
    complete, _ = observation(root, envelope, name='complete')
    receive = execution.next_production_action(root)
    outcome = {'host_receipt': complete, 'supplemental_to': original['host_receipt']['sha256']}
    result = execution.complete_action(root, receive, outcome=outcome)
    imported = images.image_call_status(root, call['call_id'])
    assert imported['host_receipt'] == original['host_receipt'] and imported['provenance'] == original['provenance']
    assert (root / original['host_receipt']['path']).read_bytes() == original_raw
    assert (root / target).read_bytes() == PNG and images.image_usage(root)['total'] == 1
    assert execution.complete_action(root, receive, outcome=outcome)['replayed']
    assert 'host_envelope' not in result


def payoff_fixture(package_fixture, *, ancestor=False):
    root, base, bind = package_fixture
    _, full = packages(base)
    def rename(value):
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rename(v) for v in value]
        return {'s1-start': 'start', 's2-start': 'payoff'}.get(value, value) if isinstance(value, str) else value
    full = rename(full)
    for node in full['boards']['image_nodes']:
        node['required_predicates'].append('cast_count')
    # This independent unrelated slot deliberately remains unmaterialized.
    unrelated = copy.deepcopy(full['boards']['image_nodes'][0])
    unrelated.update(slot_id='unrelated', output_path='assets/images/candidates/unrelated.png')
    full['boards']['image_nodes'].insert(0, unrelated)
    if ancestor:
        parent = copy.deepcopy(unrelated)
        parent.update(slot_id='payoff-ancestor', shot_id='s2', role='identity',
                      output_path='assets/images/candidates/payoff-ancestor.png')
        full['boards']['image_nodes'].append(parent)
        next(n for n in full['boards']['image_nodes'] if n['slot_id'] == 'payoff')['image_dependencies'] = ['payoff-ancestor']
    board_factory_fixture((root, full, bind))
    retain_factory_board(root, 'start')
    return root


def test_ready_first_shot_has_no_project_review_gate(neutral_package):
    root = payoff_fixture(neutral_package)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert action['kind'] not in {'review_planning', 'review_board', 'review_selection'}
    assert file_bytes(root) == before
    contract = execution.load_shot_contract(root)
    assert contract['project_review']['status'] == 'unknown'
    retain_factory_board(root, 'payoff')
    assert execution.next_production_action(root)['kind'] == 'prepare_motion'


def test_prepared_first_shot_proceeds_while_payoff_dependency_graph_stays_ordered(neutral_package):
    root = payoff_fixture(neutral_package, ancestor=True)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's1')
    schedule = boards.board_schedule(root, images=images)
    assert 'payoff-ancestor' in schedule['ready_images'] and 'payoff' not in schedule['ready_images']
    assert file_bytes(root) == before
    retain_factory_board(root, 'payoff-ancestor')
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's1')
    assert 'payoff' in boards.board_schedule(root, images=images)['ready_images']


def test_ready_motion_preserves_pending_board_original_without_reemission(neutral_package):
    root = payoff_fixture(neutral_package)
    retain_factory_board(root, 'payoff', predicates={'cast_identity': 'fail'})
    prepared = boards.prepare_image_request(root, 'unrelated', images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], 'unrelated', prepared['arguments'], output_path=prepared['output_path'])
    call = boards.reserve_board_image(root, 'unrelated', scope_id=scope, call_id='existing-original', images=images)
    images.begin_native_image(root, call['call_id'])
    before = file_bytes(root); usage = images.image_usage(root)
    result = execution.advance(root)
    assert (result['action']['kind'], result['action']['subject']) == ('prepare_motion', 's1')
    assert images.image_call_status(root, call['call_id'])['state'] == 'submitted'
    assert 'host_envelope' not in result and file_bytes(root) == before and images.image_usage(root) == usage


@pytest.mark.parametrize('shot_review_current', [False, True])
def test_current_project_review_has_no_global_board_gate(neutral_package, shot_review_current):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    if shot_review_current:
        contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's1')
    assert file_bytes(root) == before
    assert 'payoff' not in boards.load_board_pack(root)['board_bindings']
    assert 'unrelated' not in boards.load_board_pack(root)['board_bindings']


@pytest.mark.parametrize('severity', ['critical', 'cosmetic'])
def test_failed_project_review_does_not_add_factory_gate(neutral_package, severity):
    root = payoff_fixture(neutral_package)
    contract = execution.load_shot_contract(root)
    contract['project_review']['status'] = 'fail'
    contract['project_review']['predicates'][0]['status'] = 'fail'
    contract['project_review']['predicates'][0]['severity'] = severity
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert action['kind'] not in {'review_planning', 'review_board', 'review_selection'}
    assert file_bytes(root) == before


def pending_board(root, slot, call_id):
    prepared = boards.prepare_image_request(root, slot, images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], slot, prepared['arguments'], output_path=prepared['output_path'])
    call = boards.reserve_board_image(root, slot, scope_id=scope, call_id=call_id, images=images)
    return images.begin_native_image(root, call['call_id'])


def test_ready_motion_keeps_multiple_pending_board_originals_collectable(neutral_package):
    root = payoff_fixture(neutral_package)
    first = pending_board(root, 'unrelated', 'first-original')
    second = pending_board(root, 'payoff', 'payoff-original')
    before = file_bytes(root); usage = images.image_usage(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's1')
    assert execution.advance(root)['action'] == action
    assert file_bytes(root) == before and images.image_usage(root) == usage
    observed, pointer = observation(root, first, name='first-original')
    images.import_native_image(root, 'first-original', host_receipt=pointer)
    boards.bind_imported_board(root, 'unrelated', 'first-original', images=images)
    assert images.image_usage(root)['total'] == usage['total']
    assert images.image_usage(root)['unresolved_originals'] == ['payoff-original']
    assert images.image_call_status(root, 'first-original')['state'] == 'imported'
    assert images.image_call_status(root, 'payoff-original')['state'] == 'submitted'
    assert images._call(images._load(root), 'payoff-original')['submitted_arguments'] == second['arguments']


def test_inline_output_collision_preserves_original_receipt_and_media(board_package):
    root, _, _, target = board_package
    call = reserve(board_package, call_id='collision')
    envelope = images.begin_native_image(root, call['call_id'])
    observed, _ = observation(root, envelope)
    output = root / target; output.write_bytes(b'SYNTHETIC occupied output bytes')
    with pytest.raises(ValueError, match='overwrite different retained bytes'):
        images.import_native_image(root, call['call_id'], host_receipt=observed)
    current = images._call(images._load(root), call['call_id'])
    assert current['state'] == 'returned' and current['host_receipt']['sha256'] == request.digest(observed)
    assert output.read_bytes() == b'SYNTHETIC occupied output bytes'
    assert images.image_usage(root)['total'] == 1
    assert json.loads((root / current['host_receipt']['path']).read_text()) == observed


@pytest.mark.parametrize('original, changed', [(True, 1), (1, 1.0)])
def test_inline_replay_rejects_changed_json_value_types(board_package, original, changed):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='strict-types')
    envelope = images.begin_native_image(root, call['call_id'])
    observed, _ = observation(root, envelope)
    observed['result']['observed_marker'] = original
    action = execution.next_production_action(root)
    execution.complete_action(root, action, outcome={'host_receipt': observed})
    before = file_bytes(root)
    observed['result']['observed_marker'] = changed
    with pytest.raises(ValueError, match='different actual return'):
        execution.complete_action(root, action, outcome={'host_receipt': observed})
    assert file_bytes(root) == before


def test_inline_rejects_numeric_boolean_argument_substitution(board_package):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='strict-arguments')
    envelope = images.begin_native_image(root, call['call_id'])
    observed, _ = observation(root, envelope)
    observed['arguments']['transparent_background'] = 0
    before = file_bytes(root)
    with pytest.raises(ValueError, match='exact submitted arguments differ'):
        execution.complete_action(root, execution.next_production_action(root), outcome={'host_receipt': observed})
    assert file_bytes(root) == before


@pytest.mark.parametrize('original, changed', [(True, 1), (1, 1.0), (None, 'removed')])
def test_inline_supplement_preserves_exact_retained_json_fields(board_package, original, changed):
    root, _, _, _ = board_package
    call = reserve(board_package, call_id='strict-supplement')
    envelope = images.begin_native_image(root, call['call_id'])
    partial, _ = observation(root, envelope, name='partial',
        result={'output_hint': 'Synthetic fixture', 'observed_marker': original})
    with pytest.raises(ValueError, match='unresolved'):
        images.import_native_image(root, call['call_id'], host_receipt=partial)
    retained = images.image_call_status(root, call['call_id'])['host_receipt']
    complete, _ = observation(root, envelope, name='complete')
    if changed != 'removed': complete['result']['observed_marker'] = changed
    before = file_bytes(root)
    with pytest.raises(ValueError, match='without changing retained fields'):
        images.import_native_image(root, call['call_id'], host_receipt=complete, supplemental_to=retained['sha256'])
    assert file_bytes(root) == before


def test_independent_motion_stays_ahead_of_unrelated_pending_image(neutral_package):
    root = payoff_fixture(neutral_package)
    retain_factory_board(root, 'payoff', predicates={'cast_identity': 'fail'})
    pending_board(root, 'unrelated', 'unrelated-original')
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    before = file_bytes(root); usage = images.image_usage(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_motion', 's1')
    assert file_bytes(root) == before and images.image_usage(root) == usage


def pending_openart_with_boards(lifecycle, *, accepted=True):
    """Real canonical MCP admission/journal; every returned job/media is synthetic.

    The independent static pack leaves all original motion inputs unchanged.
    Its exact image authority is separate from the frozen motion approval.
    """
    root, _, _, inputs, _, _, jobs = lifecycle
    jobs.prepare(root, attempt_id='accepted-original', generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'accepted-original', authority_fn=execution.prepare_openart_mcp_handoff)
    if accepted:
        jobs.receive(root, 'accepted-original', outcome={'historyId': 'actual-synthetic-job', 'status': 'PENDING'})
    approval = root / 'notes/SYNTHETIC-image-authority.txt'
    approval.parent.mkdir(exist_ok=True)
    approval.write_text('SYNTHETIC fixture approval of these exact two independent board requests.')
    spec = {'version': '1.0', 'reviewer': 'SYNTHETIC persistent reviewer',
        'story': {'path': 'artifacts/script.json', 'sha256': file_sha256(root / 'artifacts/script.json')},
        'approval': {'path': str(approval.relative_to(root)), 'sha256': file_sha256(approval), 'approved_by': 'SYNTHETIC fixture'},
        'references': [], 'shared_roles': [],
        'image_nodes': [{'slot_id': slot, 'shot_id': sid, 'role': 'start_frame',
            'prompt': 'SYNTHETIC independent static board ' + slot, 'reference_ids': [], 'image_dependencies': [],
            'transparent_background': False, 'required_predicates': ['asset_quality', 'cast_identity'],
            'output_path': 'assets/images/candidates/' + slot + '.png'}
            for slot, sid in [('future1', 'future'), ('later1', 'later')]],
        'shots': [{'shot_id': 'entry', 'roles': [], 'video_dependencies': []},
                  {'shot_id': 'future', 'roles': [{'asset_id': 'future1', 'role': 'start_frame'}], 'video_dependencies': ['entry']},
                  {'shot_id': 'later', 'roles': [{'asset_id': 'later1', 'role': 'start_frame'}], 'video_dependencies': []}]}
    boards.prepare_board_pack(root, spec, images=images)
    return root, jobs


def reserve_overlap_board(root, slot):
    prepared = boards.prepare_image_request(root, slot, images=images)
    approval = boards.load_board_pack(root)['specification']['approval']
    scopes = json.loads((root / 'production_scopes.json').read_text())
    scope = {'id': 'synthetic-image-' + slot, 'status': 'approved', 'approved_by': 'SYNTHETIC fixture',
        'project_id': json.loads((root / 'project.json').read_text())['project_id'], 'story_revision': 'story-1',
        'phase': 'image', 'provider': 'native_imagegen', 'evidence': {k: approval[k] for k in ('path', 'sha256')},
        'image_allowance': 2, 'allowed_image_routes': ['native_imagegen'],
        'requests': {slot: prepared['request_sha256']}, 'attempts_per_shot': {slot: 1}}
    scopes['scopes'].append(scope); write(root, 'production_scopes.json', scopes)
    return boards.reserve_board_image(root, slot, scope_id=scope['id'], call_id='synthetic-' + slot, images=images)


def test_accepted_pending_original_and_explicit_board_scopes_keep_counts_and_refresh_status(lifecycle):
    root, jobs = pending_openart_with_boards(lifecycle)
    original = jobs.frozen_request(root, 'accepted-original')
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'future1')
    assert file_bytes(root) == before
    for slot in ('future1', 'later1'):
        call = reserve_overlap_board(root, slot)
        step = execution.advance(root)
        assert step['host_envelope']['call_id'] == call['call_id']
        assert step['action']['kind'] == 'receive_native_image'
        assert 'host_envelope' not in execution.advance(root)
        step = execution.complete_action(root, step['action'], outcome={
            'host_receipt': host_receipt(root, step['host_envelope'], name='overlap-' + slot)})
        assert step['action']['kind'] not in {'review_selection', 'review_board', 'review_planning'}
        assert slot in boards.load_board_pack(root)['board_bindings']
        assert jobs.frozen_request(root, 'accepted-original') == original
    assert images.image_usage(root)['total'] == 2
    assert step['action']['kind'] == 'poll_openart_original'
    assert execution.next_production_action(root)['kind'] == 'poll_openart_original'
    step = execution.complete_action(root, step['action'], outcome={
        'result': {'historyId': 'actual-synthetic-job', 'status': 'RUNNING'}})
    assert step.get('host_envelope', {}).get('call_id') is None
    assert jobs.resume_original(root, 'accepted-original')['next_action']['action'] == 'poll_original'
    assert execution.next_production_action(root)['kind'] == 'poll_openart_original'
    assert jobs.frozen_request(root, 'accepted-original') == original
    assert len(jobs.list_attempts(root)) == 1 and images.image_usage(root)['total'] == 2


def test_unaccepted_original_keeps_board_ready_and_completed_original_downloads(lifecycle):
    root, jobs = pending_openart_with_boards(lifecycle, accepted=False)
    original = jobs.frozen_request(root, 'accepted-original')
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'future1')
    assert file_bytes(root) == before
    assert jobs.frozen_request(root, 'accepted-original') == original
    recovery = jobs.resume_original(root, 'accepted-original')
    assert recovery['next_action']['action'] == 'blocked_original_host_reply_unavailable'
    assert recovery['provider_calls'] == 0 and recovery['reservations'] == 0
    assert len(jobs.list_attempts(root)) == 1 and images.image_usage(root)['total'] == 0
    jobs.resume_original(root, 'accepted-original', request_sha256=recovery['request_sha256'],
        outcome={'historyId': 'actual-synthetic-job', 'status': 'PENDING'})
    jobs.record_status(root, 'accepted-original', result={'historyId': 'actual-synthetic-job', 'status': 'COMPLETED',
        'resources': [{'id': 'actual-synthetic-resource', 'mediaType': 'video', 'status': 'COMPLETED',
                       'url': 'https://fixture.invalid/original.mp4'}]})
    assert execution.next_production_action(root)['kind'] == 'download_openart_original'
    completed = jobs.frozen_request(root, 'accepted-original')
    assert completed['status'] == 'completed'
    assert {k: v for k, v in completed.items() if k != 'status'} == {
        k: v for k, v in original.items() if k != 'status'}
    assert len(jobs.list_attempts(root)) == 1 and images.image_usage(root)['total'] == 0


def test_collected_original_advances_without_selection_review(lifecycle, monkeypatch, tmp_path):
    from tests.integration.test_openart_mcp_governance import install_fake_download
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    root, jobs = pending_openart_with_boards(lifecycle)
    jobs.record_status(root, 'accepted-original', result={'historyId': 'actual-synthetic-job', 'status': 'COMPLETED',
        'resources': [{'id': 'actual-synthetic-resource', 'mediaType': 'video', 'status': 'COMPLETED',
                       'url': 'https://fixture.invalid/original.mp4'}]})
    media = real_av_clip(tmp_path / 'SYNTHETIC-original.mp4', seconds=1)
    install_fake_download(monkeypatch, jobs, media)
    downloaded = jobs.download_original(root, 'accepted-original')
    jobs.collect(root, 'accepted-original', downloaded_path=downloaded['downloaded_path'])
    before = file_bytes(root)
    assert execution.next_production_action(root)['kind'] not in {'review_selection', 'review_board', 'review_planning'}
    assert file_bytes(root) == before


def test_pending_poll_does_not_hide_a_later_uncertain_original(lifecycle, monkeypatch):
    root, _ = pending_openart_with_boards(lifecycle)
    write(root, 'production_attempts/uncertain-original/request.json', {
        'attempt_id': 'uncertain-original', 'cli_session_id': 'retained-local-original',
        'shot_id': 'future', 'media_kind': 'motion', 'story_revision': 'story-1',
        'request_sha256': 'a' * 64, 'scope': {'provider': 'grok_cli'}})
    originals = execution._attempts(root)
    # Same exact canonical originals in the order that exposed the old poll-first
    # choice; changing iteration order grants no new submission or authority.
    monkeypatch.setattr(execution, '_attempts', lambda root: sorted(originals,
        key=lambda row: row['provider'] != 'openart_mcp' if 'provider' in row else True))
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('collect_grok_original', 'uncertain-original')
    assert action['inputs']['original_id'] == 'retained-local-original'
    assert file_bytes(root) == before


@pytest.mark.parametrize('status', [None, 'COMPLETED', 'unrecognized'])
def test_unknown_status_keeps_original_recoverable_while_preparing_declared_board(lifecycle, status):
    root, jobs = pending_openart_with_boards(lifecycle, accepted=False)
    reply = {'historyId': 'actual-synthetic-job'}
    if status is not None:
        reply['status'] = status
    jobs.receive(root, 'accepted-original', outcome=reply)
    original = jobs.frozen_request(root, 'accepted-original')
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'future1')
    assert file_bytes(root) == before and images.image_usage(root)['total'] == 0
    assert jobs.resume_original(root, 'accepted-original')['next_action']['action'] == 'poll_original'
    assert jobs.frozen_request(root, 'accepted-original') == original


@pytest.mark.parametrize('initial', [True,False])
def test_incomplete_completed_original_details_do_not_block_board_and_then_become_downloadable(lifecycle,initial):
    root,jobs=pending_openart_with_boards(lifecycle,accepted=False)
    partial={'historyId':'actual-synthetic-job','status':'COMPLETED'}
    if initial:jobs.receive(root,'accepted-original',outcome=partial)
    else:
        jobs.receive(root,'accepted-original',outcome={'historyId':'actual-synthetic-job','status':'PENDING'})
        jobs.record_status(root,'accepted-original',result=partial)
    frozen=jobs.frozen_request(root,'accepted-original')
    before=file_bytes(root)
    assert execution.next_production_action(root)['kind']=='prepare_board_image'
    assert jobs.resume_original(root,'accepted-original')['next_action']['arguments']=={'historyId':'actual-synthetic-job'}
    assert file_bytes(root)==before
    jobs.record_status(root,'accepted-original',result={'historyId':'actual-synthetic-job','status':'COMPLETED',
        'resources':[{'id':'actual-synthetic-resource','mediaType':'video','status':'COMPLETED','url':'https://fixture.invalid/original.mp4'}]})
    assert execution.next_production_action(root)['kind']=='download_openart_original'
    assert jobs.frozen_request(root,'accepted-original')==frozen
    assert len(jobs.list_attempts(root))==1 and images.image_usage(root)['total']==0



@pytest.mark.parametrize('invalid', [None, 'not-a-local-time', '2026-10-12T01:02:03'])
def test_invalid_status_timestamp_does_not_mutate_original_or_block_declared_board(lifecycle, invalid):
    root, jobs = pending_openart_with_boards(lifecycle)
    state = jobs._load(root, 'accepted-original')
    event = next(e for e in state['events'] if e['kind'] == 'submission_receipt_retained')
    if invalid is None:
        del event['observed_at']
    else:
        event['observed_at'] = invalid
    jobs._save(root, 'accepted-original', state)
    original = jobs.frozen_request(root, 'accepted-original')
    before = file_bytes(root)
    action = execution.next_production_action(root)
    assert (action['kind'], action['subject']) == ('prepare_board_image', 'future1')
    assert file_bytes(root) == before and images.image_usage(root)['total'] == 0
    assert jobs.resume_original(root, 'accepted-original')['next_action']['action'] == 'poll_original'
    assert jobs.frozen_request(root, 'accepted-original') == original
