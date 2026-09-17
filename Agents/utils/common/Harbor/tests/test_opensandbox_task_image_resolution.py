import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import opensandbox_image_manager as manager


class TaskImageResolutionTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.task = self.root / 'task_002432_cfe8954b'
        self.environment = self.task / 'environment'
        self.environment.mkdir(parents=True)
        (self.task / 'task.toml').write_text('[environment]\nbuild_timeout_sec = 60\n')
        (self.environment / 'Dockerfile').write_text('FROM ubuntu:24.04\nRUN echo local\n')
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.args = manager.parse_args([
            '--task-dir', str(self.task), '--registry', 'registry.example',
            '--project', 'test-project', '--cache-root', str(self.root / 'cache'),
            '--no-use-proxy', '--no-validate-image-hash',
        ])
        self.opener = Mock()
        self.stack.enter_context(patch.object(manager, 'build_opener', return_value=self.opener))
        self.real_inspect_config = manager.SkopeoPublisher.inspect_config
        self.config = self.stack.enter_context(patch.object(
            manager.SkopeoPublisher, 'inspect_config', return_value={
                'entrypoint': None, 'cmd': ['/bin/bash'], 'working_dir': None,
                'exposed_ports': [], 'healthcheck': None,
            },
        ))
        self.real_identity = manager.image_identity
        self.identity = self.stack.enter_context(patch.object(
            manager, 'image_identity', side_effect=AssertionError('consumer must not hash'),
        ))
        self.credentials = self.stack.enter_context(patch.object(
            manager, 'registry_credentials', side_effect=AssertionError('consumer must be anonymous'),
        ))
        self.build = self.stack.enter_context(patch.object(
            manager, 'run_build', side_effect=AssertionError('consumer must not build'),
        ))
        self.copy = self.stack.enter_context(patch.object(
            manager.SkopeoPublisher, 'copy', side_effect=AssertionError('consumer must not push'),
        ))
        self.inspect = self.stack.enter_context(patch.object(
            manager.SkopeoPublisher, 'inspect', side_effect=AssertionError('no hash-derived manifest lookup'),
        ))
        self.stack.enter_context(patch.object(
            manager, 'inspect_external_image', side_effect=AssertionError('no upstream image lookup'),
        ))

    def artifact(self, tag, pushed='2026-09-01T01:00:00Z', digest='a'):
        return {'digest': 'sha256:' + digest * 64, 'push_time': pushed,
                'tags': [{'name': tag}]}

    def responses(self, *pages):
        self.opener.open.side_effect = [
            io.BytesIO(json.dumps(page).encode()) for page in pages
        ]

    def prepare_image(self):
        prepared = manager.prepare_bundle(self.args)
        return prepared.manifest['services']['main']['image']

    def test_single_tag_reused_despite_changed_local_content_without_hash(self):
        for content in ('FROM ubuntu:24.04\n', 'FROM debian:bookworm\nRUN echo changed\n'):
            with self.subTest(content=content):
                (self.environment / 'Dockerfile').write_text(content)
                self.responses([self.artifact('uploaded-tag')])
                image = self.prepare_image()
                self.assertEqual(image['tag'], 'uploaded-tag')
                self.assertIsNone(image['input_hash'])
                self.assertEqual(image['digest_ref'],
                                 'registry.example/test-project/task_002432_cfe8954b@sha256:' + 'a' * 64)
                self.config.assert_called_with(image['digest_ref'])
        self.identity.assert_not_called()
        self.credentials.assert_not_called()
        self.inspect.assert_not_called()
        self.build.assert_not_called()
        self.copy.assert_not_called()
        self.opener.open.assert_called_with(
            'https://registry.example/api/v2.0/projects/test-project/repositories/'
            'task_002432_cfe8954b/artifacts?page_size=100&with_tag=true&page=1', timeout=30,
        )

    def test_validation_setting_defaults_on_and_cli_overrides_environment(self):
        argv = ['--task-dir', str(self.task), '--project', 'test-project']
        self.assertTrue(manager.parse_args(argv).validate_image_hash)
        self.assertTrue(manager.parse_args(argv + ['--validate-image-hash']).validate_image_hash)
        for value, enabled in [('0', False), ('1', True), ('true', True), ('TRUE', True)]:
            with self.subTest(value=value), patch.dict(
                os.environ, {'HARBOR_OPENSANDBOX_VALIDATE_IMAGE_HASH': value}
            ):
                self.assertEqual(manager.parse_args(argv).validate_image_hash, enabled)
                self.assertFalse(manager.parse_args(argv + ['--no-validate-image-hash']).validate_image_hash)
                self.assertTrue(manager.parse_args(argv + ['--validate-image-hash']).validate_image_hash)

    def test_skipped_validation_warns_on_stderr_without_hashing(self):
        self.responses([self.artifact('uploaded-tag')])
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.prepare_image()
        warning = stderr.getvalue()
        self.assertIn('WARNING: skipping task image hash validation', warning)
        self.assertIn('local dataset task definition may be inconsistent', warning)
        self.assertIn('remote repository', warning)
        self.assertIn('uploaded-tag', warning)
        self.identity.assert_not_called()

    def test_environment_opt_out_reuses_without_hashing_and_warns(self):
        with patch.dict(os.environ, {'HARBOR_OPENSANDBOX_VALIDATE_IMAGE_HASH': '0'}):
            self.args = manager.parse_args([
                '--task-dir', str(self.task), '--registry', 'registry.example',
                '--project', 'test-project', '--cache-root', str(self.root / 'cache'),
                '--no-use-proxy',
            ])
        self.responses([self.artifact('uploaded-tag')])
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(self.prepare_image()['tag'], 'uploaded-tag')
        self.assertIn('WARNING: skipping task image hash validation', stderr.getvalue())
        self.identity.assert_not_called()
        self.build.assert_not_called()

    def test_default_validation_accepts_matching_tag_without_building(self):
        self.args = manager.parse_args([
            '--task-dir', str(self.task), '--registry', 'registry.example',
            '--project', 'test-project', '--cache-root', str(self.root / 'cache'),
            '--no-use-proxy',
        ])
        self.identity.side_effect = self.real_identity
        identity = self.real_identity(self.environment)
        self.responses([self.artifact('main-' + identity[:20])])
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            image = self.prepare_image()
        self.assertEqual(image['tag'], 'main-' + identity[:20])
        # The remote tag carries only a prefix, not an authoritative full hash.
        self.assertIsNone(image['input_hash'])
        self.assertNotIn('WARNING', stderr.getvalue())
        self.identity.assert_called_once_with(self.environment)
        self.build.assert_not_called()
        self.copy.assert_not_called()
        self.credentials.assert_not_called()
        self.inspect.assert_not_called()

    def test_default_validation_rejects_stale_latest_image_without_falling_back(self):
        self.args = manager.parse_args([
            '--task-dir', str(self.task), '--registry', 'registry.example',
            '--project', 'test-project', '--cache-root', str(self.root / 'cache'),
            '--no-use-proxy',
        ])
        self.identity.side_effect = self.real_identity
        old_hash = self.real_identity(self.environment)
        (self.environment / 'Dockerfile').write_text('FROM debian:bookworm\nRUN echo changed\n')
        current_hash = self.real_identity(self.environment)
        self.assertNotEqual(old_hash[:20], current_hash[:20])
        self.responses([
            self.artifact('main-' + current_hash[:20], '2026-09-01T00:00:00Z'),
            self.artifact('main-' + old_hash[:20], '2026-09-02T00:00:00Z', 'b'),
        ])
        with self.assertRaisesRegex(RuntimeError, 'hash validation failed'):
            self.prepare_image()
        self.config.assert_not_called()
        self.build.assert_not_called()
        self.copy.assert_not_called()
        self.credentials.assert_not_called()
        self.inspect.assert_not_called()

    def test_enabled_validation_rejects_tags_without_hash_encoding(self):
        self.args.validate_image_hash = True
        self.identity.side_effect = self.real_identity
        self.responses([self.artifact('uploaded-tag')])
        with self.assertRaisesRegex(RuntimeError, 'tag may not encode the content hash'):
            self.prepare_image()
        self.build.assert_not_called()
        self.copy.assert_not_called()

    def test_enabled_validation_uses_declared_source_image_identity(self):
        self.args.validate_image_hash = True
        self.identity.side_effect = self.real_identity
        (self.environment / 'Dockerfile').unlink()
        (self.environment / 'docker-compose.yaml').write_text(
            'services:\n  main:\n    image: ubuntu:24.04\n  worker:\n    image: redis:7\n'
        )
        artifacts = [self.artifact(name + '-' + self.real_identity(
            self.environment, docker_image=source
        )[:20]) for name, source in [('main', 'ubuntu:24.04'), ('worker', 'redis:7')]]
        self.responses(artifacts)
        prepared = manager.prepare_bundle(self.args)
        self.assertEqual(set(prepared.manifest['services']), {'main', 'worker'})
        self.assertEqual(self.identity.call_count, 2)
        self.identity.assert_any_call(self.environment, docker_image='ubuntu:24.04')
        self.identity.assert_any_call(self.environment, docker_image='redis:7')
        self.build.assert_not_called()
        self.copy.assert_not_called()

    def test_enabled_validation_keeps_empty_repository_build_flow(self):
        self.args.validate_image_hash = True
        self.test_empty_repository_uses_existing_hash_build_and_push_flow()

    def test_multiple_tags_choose_newest_push_time_independent_of_listing_order(self):
        older = self.artifact('z-older', '2026-09-01T10:00:00+08:00', 'a')
        newer = self.artifact('a-newer', '2026-09-01T03:00:00Z', 'b')
        for items in ([older, newer], [newer, older]):
            with self.subTest(items=items):
                self.responses(items)
                image = self.prepare_image()
                self.assertEqual(image['tag'], 'a-newer')
                self.assertEqual(image['artifact_digest'], 'sha256:' + 'b' * 64)
        self.identity.assert_not_called()
        self.build.assert_not_called()

    def test_tag_push_time_distinguishes_tags_on_same_artifact(self):
        artifact = self.artifact('unused')
        artifact['tags'] = [
            {'name': 'z-older', 'push_time': '2026-09-01T03:00:00Z'},
            {'name': 'a-newer', 'push_time': '2026-09-01T04:00:00Z'},
        ]
        self.responses([artifact])
        self.assertEqual(self.prepare_image()['tag'], 'a-newer')

    def test_equal_push_times_have_deterministic_tag_tiebreaker(self):
        a, z = self.artifact('a'), self.artifact('z')
        for items in ([a, z], [z, a]):
            self.responses(items)
            self.assertEqual(self.prepare_image()['tag'], 'z')

    def test_lists_all_pages_before_selecting(self):
        self.responses([self.artifact('old')] * 100,
                       [self.artifact('new', '2026-09-02T00:00:00Z')])
        self.assertEqual(self.prepare_image()['tag'], 'new')
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertIn('page=2', self.opener.open.call_args.args[0])

    def allow_build(self):
        self.identity.side_effect = None
        self.identity.return_value = 'c' * 64
        self.credentials.side_effect = None
        self.credentials.return_value = ('fake-user', 'fake-password')
        self.inspect.side_effect = None
        self.inspect.return_value = None
        self.build.side_effect = None
        self.copy.side_effect = None
        self.copy.return_value = {'artifact_digest': 'sha256:' + 'd' * 64,
                                  'media_type': manager.DOCKER_MANIFEST}
        self.stack.enter_context(patch.object(manager, 'oci_archive_image_config', return_value=self.config.return_value))
        self.stack.enter_context(patch.object(manager, 'prepare_frontend', return_value={}))

    def test_empty_repository_uses_existing_hash_build_and_push_flow(self):
        self.responses([])
        self.allow_build()
        image = self.prepare_image()
        self.identity.assert_called_once_with(self.environment)
        self.credentials.assert_called_once()
        self.build.assert_called_once()
        self.copy.assert_called_once()
        self.assertTrue(self.copy.call_args.kwargs['source_is_archive'])
        self.assertEqual(image['tag'], 'main-' + 'c' * 20)
        self.assertEqual(image['input_hash'], 'sha256:' + 'c' * 64)
        self.assertEqual(image['artifact_digest'], 'sha256:' + 'd' * 64)

    def test_missing_repository_is_empty(self):
        self.opener.open.side_effect = HTTPError('https://registry.example', 404, 'missing', {}, None)
        target = manager.RegistryTarget('registry.example', 'test-project', self.task.name)
        client = manager.RegistryClient(target, Mock(tls_verify=True, username='', password=''))
        self.assertIsNone(client.latest_image('main', single_service=True))

    def test_query_errors_never_fall_back_to_build(self):
        self.credentials.side_effect = None
        self.credentials.return_value = ('fake-user', 'fake-password')
        for status in (401, 403, 500):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.credentials.reset_mock()
                self.opener.open.side_effect = HTTPError('https://registry.example', status, 'error', {}, None)
                with self.assertRaises(HTTPError):
                    self.prepare_image()
                self.assertEqual(self.opener.open.call_count, 2 if status in (401, 403) else 1)
                self.assertEqual(self.credentials.call_count, 1 if status in (401, 403) else 0)
        self.identity.assert_not_called()
        self.build.assert_not_called()
        self.copy.assert_not_called()
        self.config.assert_not_called()

    def test_private_repository_retries_with_credentials_for_all_pages_and_inspection(self):
        self.args.validate_image_hash = True
        self.identity.side_effect = self.real_identity
        identity = self.real_identity(self.environment)
        self.credentials.side_effect = None
        self.credentials.return_value = ('fake-user', 'fake-password')
        expected_auth = 'Basic ' + base64.b64encode(b'fake-user:fake-password').decode()
        for status in (401, 403):
            with self.subTest(status=status):
                self.credentials.reset_mock()
                self.opener.open.reset_mock()
                self.opener.open.side_effect = [
                    HTTPError('https://registry.example', status, 'denied', {}, None),
                    io.BytesIO(json.dumps([self.artifact('older')] * 100).encode()),
                    io.BytesIO(json.dumps([self.artifact(
                        'main-' + identity[:20], '2026-09-02T00:00:00Z', 'b'
                    )]).encode()),
                ]
                with patch.object(manager.SkopeoPublisher, 'inspect_config', self.real_inspect_config), patch.object(
                    manager.SkopeoPublisher, '_run', return_value='{"config": {}}'
                ) as run, patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    image = self.prepare_image()
                self.assertEqual(image['artifact_digest'], 'sha256:' + 'b' * 64)
                self.credentials.assert_called_once_with(self.args.docker_config, self.args.registry)
                calls = self.opener.open.call_args_list
                self.assertIsInstance(calls[0].args[0], str)  # Anonymous first attempt.
                self.assertEqual(len(calls), 3)
                for page, call in enumerate(calls[1:], 1):
                    request = call.args[0]
                    self.assertEqual(request.get_header('Authorization'), expected_auth)
                    self.assertIn(f'page={page}', request.full_url)
                    redirected = HTTPRedirectHandler().redirect_request(
                        request, None, 302, 'redirect', {}, 'https://other.example/artifacts'
                    )
                    self.assertIsNone(redirected.get_header('Authorization'))
                login, inspect = run.call_args_list
                self.assertEqual(login.args[0][:2], ['skopeo', 'login'])
                self.assertIn('fake-user', login.args[0])
                self.assertEqual(login.kwargs['input_text'], 'fake-password')
                self.assertEqual(inspect.args[0][:2], ['skopeo', 'inspect'])
                self.assertIn('--config', inspect.args[0])
                self.assertIn(image['digest_ref'], inspect.args[0][-1])
                for secret in ('fake-user', 'fake-password', expected_auth):
                    self.assertNotIn(secret, stderr.getvalue())
                    self.assertNotIn(secret, json.dumps(image))
        self.build.assert_not_called()
        self.copy.assert_not_called()

    def test_private_repository_without_credentials_fails_without_building(self):
        self.credentials.side_effect = RuntimeError('missing configured credentials')
        self.opener.open.side_effect = HTTPError('https://registry.example', 401, 'denied', {}, None)
        with self.assertRaisesRegex(RuntimeError, 'missing configured credentials'):
            self.prepare_image()
        self.opener.open.assert_called_once()
        self.identity.assert_not_called()
        self.build.assert_not_called()
        self.copy.assert_not_called()

    def test_nonempty_repository_without_usable_tags_does_not_build(self):
        artifact = self.artifact('unused')
        artifact['tags'] = None
        self.responses([artifact])
        with self.assertRaisesRegex(RuntimeError, 'no usable tag'):
            self.prepare_image()
        self.identity.assert_not_called()
        self.build.assert_not_called()

    def test_invalid_response_is_not_an_empty_repository(self):
        self.responses({'errors': [{'message': 'failure'}]})
        with self.assertRaisesRegex(ValueError, 'list of objects'):
            self.prepare_image()
        self.build.assert_not_called()

    def test_compose_services_keep_their_own_images(self):
        (self.environment / 'Dockerfile').unlink()
        (self.environment / 'docker-compose.yaml').write_text(
            'services:\n  main:\n    image: ubuntu:24.04\n  worker:\n    image: redis:7\n'
        )
        artifacts = [self.artifact('main-' + 'a' * 20),
                     self.artifact('worker-' + 'b' * 20, '2026-09-02T00:00:00Z', 'b')]
        self.responses(artifacts)
        prepared = manager.prepare_bundle(self.args)
        services = prepared.manifest['services']
        self.assertEqual(services['main']['image']['artifact_digest'], 'sha256:' + 'a' * 64)
        self.assertEqual(services['worker']['image']['artifact_digest'], 'sha256:' + 'b' * 64)
        self.identity.assert_not_called()

    def test_empty_compose_repository_builds_all_services(self):
        (self.environment / 'docker-compose.yaml').write_text(
            'services:\n  main:\n    build: .\n  worker:\n    build: .\n'
        )
        # The initial empty listing applies to the entire task, even after
        # the first service is published to the repository.
        self.responses([])
        self.allow_build()
        prepared = manager.prepare_bundle(self.args)
        self.assertEqual(set(prepared.manifest['services']), {'main', 'worker'})
        self.assertEqual(self.build.call_count, 2)
        self.assertEqual(self.copy.call_count, 2)
        self.opener.open.assert_called_once()

    def test_anonymous_login_uses_empty_private_authfile(self):
        target = manager.RegistryTarget('registry.example', 'test-project', self.task.name)
        publisher = manager.SkopeoPublisher(target, '', '', tls_verify=True)
        self.addCleanup(publisher.close)
        with patch.object(publisher, '_run', return_value='{"config": {}}') as run:
            publisher.login()
            run.assert_not_called()
            self.assertEqual(json.loads(Path(publisher._authfile).read_text()), {'auths': {}})
