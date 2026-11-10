"""Offline neutral account/administrative contracts; all records and APIs synthetic."""
import argparse
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit


HERE = Path(__file__).resolve()
MODULE = HERE.with_name('spicy_account.py') if HERE.parent.name == 'media' else HERE.parents[2] / 'lib/spicy_account.py'
spec = importlib.util.spec_from_file_location('neutral_spicy_account', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

PRIVATE = 'PRIVATE PROMPT NEVER PRINT'
SIGNED = 'https://storage.example.invalid/file?signature=private-secret'


class Client:
    fingerprint = 'a' * 64

    def __init__(self):
        self.calls = []
        self.state = 'succeeded'
        self.content = 'present'
        self.lose_purge = False
        self.model = 'vendor/model/image-to-image'

    def api(self, method, path, body=None):
        self.calls.append((method, path, body))
        parsed = urlsplit(path)
        if parsed.path == '/api/v1/jobs/recordInfo':
            data = {'taskId': parse_qs(parsed.query)['taskId'][0], 'model': self.model,
                    'state': self.state, 'cost': '0.05', 'settled': True, 'contentState': self.content,
                    'input': {'prompt': PRIVATE, 'key': 'sk-spicy-private'},
                    'output': {'text': PRIVATE, 'assets': [{'key': 'output/file.png', 'mime': 'image/png', 'url': SIGNED}]},
                    'error': PRIVATE, 'opaque': SIGNED}
        elif parsed.path == '/api/v1/jobs/purge':
            self.content = 'purged'
            if self.lose_purge:
                raise RuntimeError(PRIVATE + SIGNED + 'sk-spicy-private')
            data = {'taskId': body['taskId'], 'contentState': 'purged', 'billingRetained': True,
                    'mediaDeletionPending': True, 'opaque': SIGNED}
        elif parsed.path == '/api/v1/common/download-url':
            data = {'key': body['key'], 'url': SIGNED, 'expiresAt': '2030-01-01T00:00:00Z'}
        elif parsed.path == '/api/v1/chat/credit':
            data = {'available': '-1.25', 'held': '0.05', 'total': '-1.20', 'key': 'sk-spicy-private'}
        elif parsed.path == '/api/v1/models':
            data = {'total': 1, 'items': [{'model': self.model, 'modality': 'image', 'provider': 'Vendor',
                                         'enabled': True, 'available': True, 'tasks': ['image-to-image'],
                                         'inputSchema': {'type': 'object', 'required': ['prompt'], 'properties': {
                                             'prompt': {'type': 'string', 'description': PRIVATE, 'default': PRIVATE},
                                             'image_urls': {'type': 'array', 'maxItems': 4, 'items': {'type': 'string'}}}},
                                         'examples': [{'input': {'prompt': PRIVATE}}],
                                         'pricing': [{'currency': 'USD', 'price': '0.05', 'unit': 'per_image', 'variant': 'resolution=1k'}]}]}
        elif parsed.path == '/api/v1/jobs':
            data = {'items': [{'taskId': 'job-one', 'model': self.model, 'state': 'failed', 'prompt': PRIVATE}],
                    'hasMore': True, 'nextCursor': 'opaque_cursor==' }
        elif parsed.path == '/api/v1/usage':
            data = {'from': '2026-10-01', 'to': '2026-10-08', 'currency': 'USD', 'totalCalls': 2,
                    'totalSpend': '0.05', 'days': [{'date': '2026-10-01', 'calls': 2, 'succeeded': 1, 'failed': 1, 'spend': '0.05'}],
                    'models': [{'model': self.model, 'calls': 2, 'spend': '0.05'}], 'private': PRIVATE}
        elif parsed.path == '/api/v1/assets':
            data = {'items': [{'id': 'asset-one', 'kind': 'image', 'source': 'upload', 'name': PRIVATE, 'previewUrl': SIGNED,
                               'favorite': True, 'bytes': 99}], 'nextCursor': 'cursor==' }
        elif parsed.path == '/api/v1/assets/usage':
            data = {'plan': {'name': PRIVATE}, 'usage': {'bytes': 99}, 'limits': {'bytes': 1000},
                    'scope': {'workspaceId': PRIVATE, 'canWrite': True}}
        elif parsed.path == '/api/v1/assets/entities':
            data = {'items': [{'id': 'character-one', 'kind': 'character', 'name': PRIVATE, 'note': PRIVATE,
                               'count': 2, 'cover': {'previewUrl': SIGNED}, 'assets': []}]}
        else:
            raise AssertionError('Unexpected synthetic endpoint')
        return data, 'request-synthetic'

    # Named SDK-backed client surface; shims record the equivalent wire path.
    def _named(self, method, path, query=None, body=None):
        names = {'include_schema': 'includeSchema', 'from_date': 'from', 'to_date': 'to',
                 'collection_id': 'collectionId', 'asset_id': 'assetId'}
        if query:
            pairs = {names.get(k, k): ('true' if v is True else 'false' if v is False else v)
                     for k, v in query.items() if v is not None}
            path += '?' + urlencode(pairs)
        data, request_id = self.api(method, path, body)
        return {'data': data, 'request_id': request_id, 'http_status': 200}

    def get_balance(self):
        return self._named('GET', '/api/v1/chat/credit')

    def get_model(self, model):
        return self._named('GET', '/api/v1/models', {'model': model})

    def list_models(self, **query):
        return self._named('GET', '/api/v1/models', query)

    def get_usage(self, **query):
        return self._named('GET', '/api/v1/usage', query)

    def list_tasks(self, **query):
        return self._named('GET', '/api/v1/jobs', query)

    def get_task(self, task_id):
        return self._named('GET', '/api/v1/jobs/recordInfo', {'taskId': task_id})

    def list_assets(self, **query):
        return self._named('GET', '/api/v1/assets', query)

    def get_asset(self, asset_id):
        return self._named('GET', '/api/v1/assets/' + asset_id)

    def asset_usage(self):
        return self._named('GET', '/api/v1/assets/usage')

    def list_characters(self, **query):
        return self._named('GET', '/api/v1/assets/entities', query)

    def get_character(self, entity_id):
        return self._named('GET', '/api/v1/assets/entities/' + entity_id)

    def create_download_url(self, task_id, key=None):
        return self._named('POST', '/api/v1/common/download-url', body={'taskId': task_id, 'key': key})

    def purge_task(self, task_id):
        return self._named('POST', '/api/v1/jobs/purge', body={'taskId': task_id})


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def safe(self, result):
        output = json.dumps(result)
        self.assertNotIn(PRIVATE, output)
        self.assertNotIn(SIGNED, output)
        self.assertNotIn('sk-spicy-private', output)

    def parser(self, *args):
        parser = argparse.ArgumentParser()
        m.register_cli(parser.add_subparsers(dest='command', required=True))
        return parser.parse_args(['account', *args])

    def test_public_docs_need_no_credentials_or_network(self):
        args = self.parser('docs', 'schema')
        self.assertFalse(m.needs_client(args))
        self.assertEqual(m.execute_cli(None, args)['items'][0]['slug'], 'models')
        self.assertFalse(m.needs_client(self.parser('status')))
        self.assertLessEqual(len(m.docs_search('', 25)['items']), 25)

    def test_status_404_readiness_is_not_false_outage(self):
        calls = []
        def probe(path):
            calls.append(path)
            return {'ok': path == '/healthz', 'status': 200 if path == '/healthz' else 404, 'private': PRIVATE}
        result = m.service_status(probe)
        self.assertTrue(result['operational'])
        self.assertFalse(result['authenticated'])
        self.assertEqual(calls, ['/healthz', '/readyz'])
        self.safe(result)

    def test_models_filters_schema_and_prices(self):
        result = m.models(self.client, modality='image', provider='Vendor', task='edit', search='image')
        self.assertEqual(parse_qs(urlsplit(self.client.calls[0][1]).query), {
            'modality': ['image'], 'provider': ['Vendor'], 'task': ['edit'], 'search': ['image'], 'includeSchema': ['true']})
        self.assertEqual(result['items'][0]['inputSchema']['properties']['image_urls']['maxItems'], 4)
        self.assertEqual(result['items'][0]['pricing'][0]['price'], '0.05')
        self.safe(result)
        m.models(self.client, provider='Black Forest Labs')
        self.assertEqual(parse_qs(urlsplit(self.client.calls[-1][1]).query)['provider'], ['Black Forest Labs'])

    def test_invalid_filters_do_not_request(self):
        for call in (lambda: m.models(self.client, modality='movie'), lambda: m.models(self.client, model='../other'),
                     lambda: m.tasks(self.client, limit=101), lambda: m.tasks(self.client, state='active'),
                     lambda: m.tasks(self.client, cursor=SIGNED), lambda: m.usage(self.client, '2026-01-01', '2026-10-01'),
                     lambda: m.assets(self.client, source='task_output,secret'), lambda: m.characters(self.client, limit=0)):
            with self.assertRaises(m.AccountError):
                call()
        self.assertEqual(self.client.calls, [])

    def test_model_detail_identity_must_match(self):
        class Wrong:
            def get_model(self, model):
                return {'data': {'model': 'other/model', 'modality': 'image'}}
        with self.assertRaises(m.AccountError):
            m.models(Wrong(), model='chosen/model')

    def test_engine_object_uses_named_sdk_method_only(self):
        class Engine:
            calls = []
            def get_balance(self):
                self.calls.append('get_balance')
                return {'data': {'available': '1', 'held': '0', 'total': '1'}, 'request_id': 'r'}
        engine = Engine()
        m.balance(engine)
        self.assertEqual(engine.calls, ['get_balance'])

    def test_neutral_helper_allowlist_precedes_attribute_lookup(self):
        class Spy:
            looked = []
            def __getattribute__(self, name):
                type(self).looked.append(name)
                raise AssertionError('lookup must not happen')
        spy = Spy()
        for operation in ('create_task', 'quote_task', 'retry_task', 'create_upload_url', 'commit_file',
                          'upload_bytes', 'upload_file', 'assets', 'api', 'request_json', '__class__', ''):
            with self.assertRaises(m.AccountError):
                m._sdk_result(spy, operation)
        self.assertEqual(Spy.looked, [])
        self.assertNotIn('create_task', m.ACCOUNT_OPERATIONS)

    def test_neutral_helper_sanitizes_descriptor_and_call_failures(self):
        class Broken:
            @property
            def get_balance(self):
                raise RuntimeError(PRIVATE + SIGNED)
            def get_task(self, task_id):
                raise RuntimeError(PRIVATE + 'sk-spicy-private')
            def list_models(self, **query):
                return {'data': ['not', 'object']}
        for call in (lambda: m.balance(Broken()), lambda: m.task(Broken(), 'job-one'), lambda: m.models(Broken())):
            with self.assertRaises(m.AccountError) as caught:
                call()
            error = caught.exception
            self.assertTrue(error.__suppress_context__ or error.__context__ is None)
            text = str(error) + repr(error.args)
            self.assertNotIn(PRIVATE, text)
            self.assertNotIn('sk-spicy-private', text)

    def test_negative_balance_and_usage_are_validated(self):
        self.assertEqual(m.balance(self.client)['available'], '-1.25')
        result = m.usage(self.client, '2026-10-01', '2026-10-08')
        self.assertEqual(result['days'][0]['date'], '2026-10-01')
        self.safe(result)
        with self.assertRaises(m.AccountError):
            m._usage_row({}, grouping='date')

    def test_key_history_is_metadata_only_and_cursor_paged(self):
        result = m.tasks(self.client, start='2026-10-01', end='2026-10-08', state='failed', model=self.client.model, limit=5)
        self.assertEqual(result['scope'], 'current-api-key')
        self.assertEqual(result['nextCursor'], 'opaque_cursor==')
        self.safe(result)
        next_page = m.tasks(self.client, start='2026-10-01', end='2026-10-08', state='failed', model=self.client.model, limit=5, cursor=result['nextCursor'])
        self.assertIn('cursor=opaque_cursor%3D%3D', self.client.calls[-1][1])
        self.safe(next_page)

    def test_task_inspection_never_returns_prompts_text_links_or_provider_errors(self):
        result = m.task(self.client, 'job-one')
        self.assertTrue(result['error_present'])
        self.assertEqual(result['assets'][0]['key'], 'output/file.png')
        self.safe(result)

    def test_wait_bounded_read_only_and_terminal_exit(self):
        self.client.state = 'running'
        now = [0]
        result = m.wait_task(self.client, 'job-one', timeout=12, interval=5, clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['polls'], 4)
        self.assertEqual(now[0], 12)
        self.assertTrue(all(call[0] == 'GET' for call in self.client.calls))
        self.client.state = 'succeeded'
        self.assertEqual(m.wait_task(self.client, 'job-one', timeout=0)['polls'], 1)
        for timeout in (-1, 301, float('nan')):
            with self.assertRaises(m.AccountError):
                m.wait_task(self.client, 'job-one', timeout=timeout)

    def test_wait_even_broken_clock_has_call_count_bound(self):
        self.client.state = 'running'
        result = m.wait_task(self.client, 'job-one', timeout=3, interval=1, clock=lambda: 0, sleep=lambda _: None)
        self.assertEqual(result['polls'], 4)

    def test_transport_error_is_sanitized(self):
        def bad(*args):
            raise RuntimeError(SIGNED + PRIVATE + 'sk-spicy-private')
        with self.assertRaises(m.AccountError) as caught:
            m.balance(bad)
        self.safe(str(caught.exception))
        self.safe(''.join(traceback.format_exception(type(caught.exception), caught.exception, caught.exception.__traceback__)))

    def test_pending_storage_with_empty_key_is_valid_but_not_downloadable(self):
        result = m._task_summary({'taskId': 'job-one', 'state': 'succeeded', 'output': {'assets': [{'pending': True, 'key': '', 'url': ''}]}})
        self.assertTrue(result['assets'][0]['pending'])
        self.assertNotIn('key', result['assets'][0])

    def test_asset_library_is_distinct_scope_and_redacted(self):
        result = m.assets(self.client, kind='image', source='upload,task_output', favorite=True, collection_id='collection-one', sort='name')
        self.assertEqual(result['scope'], 'asset-library-workspace-or-personal')
        self.assertTrue(result['items'][0]['favorite'])
        self.safe(result)
        self.safe(m.asset_usage(self.client))
        self.safe(m.characters(self.client, asset_id='asset-one'))

    def test_download_url_requires_explicit_private_file_and_no_download(self):
        directory = self.root / 'private'
        directory.mkdir(mode=0o700)
        target = directory / 'handoff.json'
        with self.assertRaises(m.AccountError):
            m.download_url(self.client, 'job-one', 'output/file.png', target)
        self.assertEqual(self.client.calls, [])
        result = m.download_url(self.client, 'job-one', 'output/file.png', target, allow_private_url=True)
        self.assertEqual(json.loads(target.read_text())['url'], SIGNED)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(result['media_downloaded'])
        self.safe(result)
        with self.assertRaises(m.AccountError):
            m.download_url(self.client, 'job-one', 'output/file.png', target, allow_private_url=True)
        self.assertEqual(len(self.client.calls), 2)

    def test_private_download_rejects_shared_parent_and_unknown_asset(self):
        self.root.chmod(0o755)
        with self.assertRaises(m.AccountError):
            m.download_url(self.client, 'job-one', 'output/file.png', self.root / 'unsafe.json', allow_private_url=True)
        self.root.chmod(0o700)
        with self.assertRaises(m.AccountError):
            m.download_url(self.client, 'job-one', 'not-an-output', self.root / 'safe.json', allow_private_url=True)
        self.assertTrue(all(call[0] == 'GET' for call in self.client.calls))

    def original(self, scope='studio-identity'):
        owner = self.root / 'identity'
        owner.mkdir()
        if scope == 'studio-identity':
            relative = 'assets/generated/spicy-images/' + 'a' * 32 + '/operation.json'
            record = {'identity': owner.name, 'operation': 'a' * 32, 'credential_fingerprint': self.client.fingerprint,
                      'task_id': 'job-one', 'state': 'failed', 'request': {'model': self.client.model, 'input': {'prompt': PRIVATE}}}
        else:
            relative = 'production_attempts/attempt-one/spicy_job.json'
            record = {'attempt_id': 'attempt-one', 'acceptance_state': 'accepted', 'task_id': 'job-one',
                      'task_state': 'failed', 'credential_binding': {'credential_digest': self.client.fingerprint},
                      'native': {'model': self.client.model, 'input': {'prompt': PRIVATE}}}
        path = owner / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record))
        return owner, relative

    def approved(self, scope='studio-identity'):
        owner, relative = self.original(scope)
        preview = m.purge_preview(owner, scope, [relative], ['job-one'])
        confirmation = self.root / 'human.json'
        confirmation.write_text(json.dumps({'actor': 'human', 'action': 'purge', 'binding': preview['binding'],
                                           'task_ids': ['job-one'], 'evidence': 'Human explicitly confirmed this exact remote task deletion.'}))
        approval = m.purge_authorize(owner, scope, preview['binding'], confirmation, allow_delete=True)
        return owner, relative, preview, approval

    def resolver(self, owner, scope, relative):
        return m._bound_original(owner, scope, relative)

    def test_purge_preview_is_immutable_exact_owner_bound_and_private(self):
        owner, relative = self.original()
        with self.assertRaises(m.AccountError):
            m.purge_preview(owner, 'studio-identity', [relative], ['other-job'])
        result = m.purge_preview(owner, 'studio-identity', [relative], ['job-one'])
        self.assertEqual(result['count'], 1)
        path = Path(result['preview_record'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.safe(json.loads(path.read_text()))
        with self.assertRaises(m.AccountError):
            m.purge_preview(owner, 'studio-identity', [relative], ['job-one'])
        self.assertEqual(self.client.calls, [])

    def test_purge_confirmation_needs_exact_binding_batch_and_delete_interlock(self):
        owner, relative = self.original()
        preview = m.purge_preview(owner, 'studio-identity', [relative], ['job-one'])
        confirmation = self.root / 'human.json'
        confirmation.write_text(json.dumps({'actor': 'human', 'action': 'purge', 'binding': preview['binding'],
                                           'task_ids': ['wrong-job'], 'evidence': 'Exact human confirmation'}))
        with self.assertRaises(m.AccountError):
            m.purge_authorize(owner, 'studio-identity', preview['binding'], confirmation)
        with self.assertRaises(m.AccountError):
            m.purge_authorize(owner, 'studio-identity', preview['binding'], confirmation, allow_delete=True)

    def test_purge_requires_owner_resolver_current_key_and_unchanged_original(self):
        owner, relative, preview, approval = self.approved()
        args = (self.client, owner, 'studio-identity', preview['binding'], approval['authorization_record'])
        with self.assertRaises(m.AccountError):
            m.purge_execute(*args, allow_delete=True)
        self.client.fingerprint = 'b' * 64
        with self.assertRaises(m.AccountError):
            m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.client.fingerprint = 'a' * 64
        with (owner / relative).open('a') as output:
            output.write(' ')
        with self.assertRaises(m.AccountError):
            m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertEqual(self.client.calls, [])

    def test_purge_dispatch_saves_intent_before_exact_post_and_never_repeats(self):
        owner, relative, preview, approval = self.approved()
        original_api = self.client.api
        def inspecting(method, path, body=None):
            if method == 'POST':
                self.assertEqual(len(list((owner / 'assets/generated/spicy-admin').glob('purge-intent-*'))), 1)
                self.assertEqual(body, {'taskId': 'job-one'})
            return original_api(method, path, body)
        self.client.api = inspecting
        args = (self.client, owner, 'studio-identity', preview['binding'], approval['authorization_record'])
        result = m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertTrue(result['all_logical_purges_observed'])
        self.assertEqual(result['physical_media_deletion'], 'unverified')
        self.safe(result)
        result = m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertTrue(result['all_logical_purges_observed'])
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.client.calls), 1)
        self.assertTrue((owner / relative).exists())

    def test_purge_lost_response_retains_uncertainty_then_reads_only(self):
        owner, relative, preview, approval = self.approved('openmontage-project')
        self.client.lose_purge = True
        args = (self.client, owner, 'openmontage-project', preview['binding'], approval['authorization_record'])
        result = m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertIn('acceptance-unknown', result['results'][0]['status'])
        self.safe(result)
        result = m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver, reconcile_only=True)
        self.assertTrue(result['all_logical_purges_observed'])
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.client.calls), 1)
        for path in (owner / 'artifacts/spicy-admin').glob('*.json'):
            self.safe(json.loads(path.read_text()))

    def test_durable_intent_directory_sync_precedes_post_and_failure_blocks(self):
        owner, relative, preview, approval = self.approved()
        args = (self.client, owner, 'studio-identity', preview['binding'], approval['authorization_record'])
        real_sync = os.fsync
        directory_synced = []
        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_synced.append(True)
            real_sync(descriptor)
        original_api = self.client.api
        def api(method, path, body=None):
            if method == 'POST':
                self.assertTrue(directory_synced)
            return original_api(method, path, body)
        self.client.api = api
        with patch.object(m.os, 'fsync', side_effect=sync):
            m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.client.calls), 1)

    def test_failed_intent_directory_sync_prevents_remote_purge(self):
        owner, relative, preview, approval = self.approved()
        args = (self.client, owner, 'studio-identity', preview['binding'], approval['authorization_record'])
        real_sync = os.fsync
        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError('synthetic sync failure')
            real_sync(descriptor)
        with patch.object(m.os, 'fsync', side_effect=sync), self.assertRaises(m.AccountError):
            m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))

    def test_purge_expiry_and_running_never_claim_purge(self):
        owner, relative, preview, approval = self.approved()
        args = (self.client, owner, 'studio-identity', preview['binding'], approval['authorization_record'])
        self.client.content = 'expired'
        result = m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver, reconcile_only=True)
        self.assertFalse(result['all_logical_purges_observed'])
        self.client.state = 'running'
        with self.assertRaises(m.AccountError):
            m.purge_execute(*args, allow_delete=True, original_resolver=self.resolver)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.client.calls))

    def test_retry_is_only_counted_owner_handoff_and_never_raw_vendor_retry(self):
        owner, relative = self.original()
        result = m.retry_handoff(owner, 'studio-identity', relative)
        self.assertFalse(result['provider_retry_supported'])
        self.assertFalse(result['generation_attempted'])
        self.assertFalse(result['provider_retry_contract']['exact_quote_binding_supported'])
        self.safe(result)
        self.assertEqual(self.client.calls, [])

    def test_cli_mutation_ownership_and_no_raw_task_create(self):
        for command in ('create', 'retry', 'upload', 'delete-asset'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.parser(command)
        self.assertTrue(m.needs_client(self.parser('task', 'job-one')))
        self.assertFalse(m.needs_client(self.parser('retry-handoff', '--owner-root', str(self.root), '--scope-kind', 'studio-identity', '--record', 'record.json')))


if __name__ == '__main__':
    unittest.main()
