"""Policy-denied board refs use the existing exact standing upload/receipt owner."""
import json
from pathlib import Path

import pytest

from lib import openart_mcp as mcp, openart_mcp_jobs as jobs, production_autonomy as autonomy
from lib import production_images as images
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_policy_image_fallback import ACCOUNT, TERM, POLICY, native_outcome, mcp_account  # noqa: F401
from tests.lib.test_production_factory import board_factory_fixture


def denied_sources(neutral_package):
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM)
    denied, _ = native_outcome(root, 'returned', POLICY)
    call = images._call(images._load(root), denied)
    from lib import production_boards as boards
    shot = boards.load_board_pack(root)['prompt_pack'][call['slot_id']]['shot_id']
    args = dict(shot_id=shot, provider='openart_mcp',
        files=call['host_arguments']['referenced_image_paths'], account=ACCOUNT,
        openart_project_id='fixture-mcp', policy_fallback_call_id=denied)
    return root, denied, call, args


def upload_result(files, *, prefix='fixture'):
    return {'projectId': 'fixture-mcp', 'results': [
        {'fileId': prefix + '-file-' + str(i), 'status': 'SUCCESS',
         'visualReference': {'id': prefix + '-ref-' + str(i), 'type': 'image',
          'url': 'https://fixture.invalid/ref-' + str(i) + '.png', 'label': 'SYNTHETIC reference',
          'imageUrl': 'https://fixture.invalid/ref-' + str(i) + '.png',
          'thumbnailUrl': 'https://fixture.invalid/thumb-' + str(i) + '.png', 'metadata': {}}}
        for i, _ in enumerate(files)]}


def test_exact_policy_denied_reference_upload_receipt_replays_without_motion_promotion(neutral_package, mcp_account):
    root, denied, call, args = denied_sources(neutral_package)
    authority = autonomy.derive_standing_upload_authority(root, **args)
    assert authority['authority']['derived_from_standing_run']['policy_fallback'] == {
        'call_id': denied, 'receipt_sha256': images._policy_denial(root, call)}
    with pytest.raises(mcp.OpenArtMCPError, match='upload_reference_unavailable'):
        jobs.resolve_input_asset(root, {'source_path': args['files'][0]}, account_binding=ACCOUNT,
            openart_project_id='fixture-mcp', policy_fallback_call_id=denied)
    declaration = {'upload_authorization_id': authority['upload_id']}
    upload = jobs.prepare_upload(root, files=args['files'], billing_declaration=declaration,
        openart_project_id=args['openart_project_id'])
    receipt = jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=upload_result(args['files']))
    assert receipt['status'] == 'recorded'
    assert autonomy.derive_standing_upload_authority(root, **args) == dict(authority, replayed=True)
    for file, ref in zip(args['files'], receipt['references']):
        bound = jobs.resolve_input_asset(root, {'source_path': file}, account_binding=ACCOUNT,
            openart_project_id='fixture-mcp', policy_fallback_call_id=denied)
        assert bound['receipt_sha256'] == ref['receipt_sha256']
    with pytest.raises(ValueError, match='already consumed'):
        jobs.prepare_upload(root, files=args['files'], billing_declaration=declaration,
            openart_project_id='fixture-mcp')
    assert images.image_call_status(root, denied)['state'] == 'failed'
    assert len(list((root / 'openart_mcp/uploads').glob('*.json'))) == 1


@pytest.mark.parametrize('damage', ['account', 'project', 'shot', 'foreign_path', 'changed_source', 'packet', 'denial_observation'])
def test_policy_transfer_refuses_changed_foreign_or_unbound_sources_before_upload(neutral_package, mcp_account, damage):
    root, denied, call, args = denied_sources(neutral_package)
    if damage == 'account':
        args['account'] = {'uid_sha256': 'f' * 64}
    elif damage == 'project':
        args['openart_project_id'] = 'foreign-project'
    elif damage == 'shot':
        args['shot_id'] = 'not-the-denied-board-shot'
    elif damage == 'foreign_path':
        foreign = root / 'foreign.svg'; foreign.write_bytes(Path(args['files'][0]).read_bytes())
        args['files'] = [str(foreign)]
    elif damage == 'changed_source':
        source = Path(args['files'][0]); source.chmod(0o600)
        source.write_bytes(source.read_bytes() + b'SYNTHETIC changed bytes')
    elif damage == 'packet':
        calls = images._load(root); forged = images._call(calls, denied)
        forged['packet']['references'][0]['role'] = 'end_frame'
        forged['packet']['packet_sha256'] = images.digest({k: v for k, v in forged['packet'].items() if k != 'packet_sha256'})
        forged['request_sha256'] = images.image_request_digest(forged['packet'], forged['slot_id'],
            forged['host_arguments'], output_path=forged['output_path'])
        scopes = json.loads((root / 'production_scopes.json').read_text())
        scope = next(s for s in scopes['scopes'] if s['id'] == forged['scope_id'])
        scope['requests'][forged['slot_id']] = forged['request_sha256']
        forged['scope_sha256'] = images.digest(scope)
        (root / 'production_scopes.json').write_text(json.dumps(scopes))
        images._write(root, calls)
    else:
        calls = images._load(root); forged = images._call(calls, denied)
        receipt_path = root / forged['host_receipt']['path']
        observation = json.loads(receipt_path.read_text())
        observation['arguments']['prompt'] += ' SYNTHETIC forged request'
        receipt_path.chmod(0o600); receipt_path.write_text(json.dumps(observation))
        forged['host_receipt']['sha256'] = images.file_sha256(receipt_path)
        images._write(root, calls)
    with pytest.raises(ValueError):
        autonomy.derive_standing_upload_authority(root, **args)
    assert not list((root / 'artifacts').glob('openart_mcp_upload_authorization-*.json'))
    assert not list((root / 'openart_mcp/uploads').glob('*.json'))


@pytest.mark.parametrize('phase,damage', [('prepare', 'account'), ('prepare', 'source'),
    ('receipt', 'account'), ('receipt', 'source'), ('resolve', 'account'), ('resolve', 'source'),
    ('resolve', 'foreign'), ('resolve', 'denial')])
def test_policy_upload_receipt_and_resolution_revalidate_current_source_account_denial(neutral_package, mcp_account, monkeypatch, phase, damage):
    root, denied, call, args = denied_sources(neutral_package)
    authority = autonomy.derive_standing_upload_authority(root, **args)
    kwargs = dict(files=args['files'], billing_declaration={'upload_authorization_id': authority['upload_id']},
        openart_project_id='fixture-mcp')
    upload = None
    if phase != 'prepare':
        upload = jobs.prepare_upload(root, **kwargs)
    if phase == 'resolve':
        jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=upload_result(args['files']))
    account = ACCOUNT
    file = args['files'][0]
    if damage == 'account':
        current = mcp.account_summary(); current['uid_sha256'] = 'f' * 64
        monkeypatch.setattr(mcp, 'account_summary', lambda: current)
        account = {'uid_sha256': 'f' * 64}
    elif damage == 'source':
        source = Path(file); source.chmod(0o600); source.write_bytes(source.read_bytes() + b'SYNTHETIC changed')
    elif damage == 'foreign':
        foreign = root / 'foreign.svg'; foreign.write_bytes(Path(file).read_bytes()); file = str(foreign)
    else:
        calls = images._load(root); forged = images._call(calls, denied)
        receipt_path = root / forged['host_receipt']['path']
        observation = json.loads(receipt_path.read_text()); observation['result']['error'] = 'http 403 Forbidden'
        receipt_path.chmod(0o600); receipt_path.write_text(json.dumps(observation))
        forged['host_receipt']['sha256'] = images.file_sha256(receipt_path); images._write(root, calls)
    with pytest.raises(ValueError):
        if phase == 'prepare':
            jobs.prepare_upload(root, **kwargs)
        elif phase == 'receipt':
            jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=upload_result(args['files']))
        else:
            jobs.resolve_input_asset(root, {'source_path': file}, account_binding=account,
                openart_project_id='fixture-mcp', policy_fallback_call_id=denied)
    if phase == 'prepare':
        assert not list((root / 'openart_mcp/uploads').glob('*.json'))
    if phase == 'receipt':
        assert not list((jobs._state_root(create=False) / 'mcp-uploads').glob('*.json'))


def test_same_envelope_generated_reference_is_transferable_without_inventing_original_hash(neutral_package, mcp_account):
    from tests.lib.test_production_factory import retain_factory_board
    from tests.lib.test_production_images import host_receipt
    from lib import production_boards as boards
    root = board_factory_fixture(neutral_package, image_policy_fallback=TERM)
    generated, _ = retain_factory_board(root, 'start')
    prepared = boards.prepare_image_request(root, 'end', images=images)
    scope = images.derive_native_image_scope(root, prepared['packet'], 'end', prepared['arguments'], output_path=prepared['output_path'])
    denied = boards.reserve_board_image(root, 'end', scope_id=scope, call_id='fixture-denied-end', images=images)['call_id']
    envelope = images.begin_native_image(root, denied)
    images.import_native_image(root, denied, host_receipt=host_receipt(root, envelope, name=denied,
        result={'isError': True, 'error': POLICY}))
    call = images._call(images._load(root), denied)
    policy, _, _ = autonomy.require_active_policy(root)
    assert generated['sha256'] not in autonomy.effective_standing_setup(root, policy)['approved_source_sha256']
    files = call['host_arguments']['referenced_image_paths']
    assert len(files) == 2
    for index, file in enumerate(files):
        # The original whole packet stays validated; only the still-missing
        # ordered subset is transferred by each existing upload batch.
        authority = autonomy.derive_standing_upload_authority(root, shot_id='s1', provider='openart_mcp',
            files=[file], account=ACCOUNT, openart_project_id='fixture-mcp', policy_fallback_call_id=denied)
        assert len(authority['authority']['files']) == 1
        assert [str((root / r['path']).resolve()) for r in authority['authority']['derived_from_standing_run']['source_binding']['references']] == [file]
        upload = jobs.prepare_upload(root, files=[file], billing_declaration={'upload_authorization_id': authority['upload_id']}, openart_project_id='fixture-mcp')
        receipt = jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=upload_result([file], prefix='fixture-' + str(index)))
        ref = receipt['references'][0]
        assert jobs.resolve_input_asset(root, {'source_path': file}, account_binding=ACCOUNT,
            openart_project_id='fixture-mcp', policy_fallback_call_id=denied)['receipt_sha256'] == ref['receipt_sha256']


@pytest.mark.parametrize('term_field,value', [('max_file_bytes', 1), ('max_batch_bytes', 1), ('max_batches', 1)])
def test_existing_standing_limits_bound_policy_fallback_transfers(neutral_package, mcp_account, monkeypatch, term_field, value):
    # Author narrowed OpenArt terms in the existing actual fixture creator setup;
    # native first-pass terms stay unchanged, so the original denial is real.
    import tests.lib.test_production_factory as factory
    original = factory.creator_fixture
    def bounded_setup(root, name, document):
        if name == 'standing-boards':
            next(t for t in document['source_transfer'] if t['provider'] == 'openart_mcp')[term_field] = value
        return original(root, name, document)
    monkeypatch.setattr(factory, 'creator_fixture', bounded_setup)
    root, denied, call, args = denied_sources(neutral_package)
    if term_field == 'max_batches':
        first_args = dict(args)
        first = autonomy.derive_standing_upload_authority(root, **args)
        # A different denial has a different exact binding even for shared refs.
        from lib import production_execution as execution
        from tests.lib.test_production_images import host_receipt
        action = execution.next_production_action(root)
        assert action['kind'] == 'receive_native_image'
        denied2 = action['subject']
        pending = images._call(images._load(root), denied2)
        images.import_native_image(root, denied2, host_receipt=host_receipt(root,
            {'arguments': pending['submitted_arguments']}, name=denied2,
            result={'isError': True, 'error': POLICY}))
        call2 = images._call(images._load(root), denied2)
        from lib import production_boards as boards
        args.update(policy_fallback_call_id=denied2,
            shot_id=boards.load_board_pack(root)['prompt_pack'][call2['slot_id']]['shot_id'],
            files=call2['host_arguments']['referenced_image_paths'])
        with pytest.raises(ValueError, match='batch allowance exhausted'):
            autonomy.derive_standing_upload_authority(root, **args)
        assert autonomy.derive_standing_upload_authority(root, **first_args)['upload_id'] == first['upload_id']
    else:
        with pytest.raises(ValueError, match='transfer'):
            autonomy.derive_standing_upload_authority(root, **args)
        assert not list((root / 'artifacts').glob('openart_mcp_upload_authorization-*.json'))


@pytest.mark.parametrize('max_batches', [1, 8])
def test_shared_uploaded_reference_reuses_original_authority_for_another_exact_denial(neutral_package, mcp_account, monkeypatch, max_batches):
    from lib import production_execution as execution
    from tests.lib.test_production_images import host_receipt
    import tests.lib.test_production_factory as factory
    original = factory.creator_fixture
    def bounded_setup(root, name, document):
        if name == 'standing-boards':
            next(t for t in document['source_transfer'] if t['provider'] == 'openart_mcp')['max_batches'] = max_batches
        return original(root, name, document)
    monkeypatch.setattr(factory, 'creator_fixture', bounded_setup)
    root, denied, call, args = denied_sources(neutral_package)
    authority = autonomy.derive_standing_upload_authority(root, **args)
    upload = jobs.prepare_upload(root, files=args['files'], billing_declaration={'upload_authorization_id': authority['upload_id']}, openart_project_id='fixture-mcp')
    receipt = jobs.record_upload_receipt(root, upload_id=upload['upload_id'], result=upload_result(args['files']))
    # Prior complete_action already emitted the next native original once.
    action = execution.next_production_action(root)
    denied2 = action['subject']; call2 = images._call(images._load(root), denied2)
    assert action['kind'] == 'receive_native_image' and call2['host_arguments']['referenced_image_paths'] == args['files']
    images.import_native_image(root, denied2, host_receipt=host_receipt(root,
        {'arguments': call2['submitted_arguments']}, name=denied2, result={'isError': True, 'error': POLICY}))
    frozen_batch = (root / 'openart_mcp/uploads' / (upload['upload_id'] + '.json')).read_bytes()
    bound = jobs.resolve_input_asset(root, {'source_path': args['files'][0]}, account_binding=ACCOUNT,
        openart_project_id='fixture-mcp', policy_fallback_call_id=denied2)
    assert bound['receipt_sha256'] == receipt['references'][0]['receipt_sha256']
    assert (root / 'openart_mcp/uploads' / (upload['upload_id'] + '.json')).read_bytes() == frozen_batch
    assert len(list((root / 'artifacts').glob('openart_mcp_upload_authorization-*.json'))) == 1
    assert len(list((root / 'openart_mcp/uploads').glob('*.json'))) == 1
