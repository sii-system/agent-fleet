"""Offline coverage for benchmark selection, lifecycle, resumability and agents."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'Tasks/WindowsAgentArena/src'))
sys.path.insert(0, str(REPO / 'Agents/utils/common/Harbor'))

from waa_benchmark.dataset import digest, load_tasks
from waa_benchmark.desktop import GuestHTTP
from waa_benchmark.launch import command, parser
from waa_benchmark.source import lazy_exports, validate_runtime
from waa_benchmark.worker import NativeSession

from Agents.WindowsAgentArena.agent import load_agent


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.examples = self.root / 'evaluation_examples_windows'
        self.examples.mkdir()
        (self.examples / 'test_all.json').write_text(json.dumps({'chrome': ['one', 'two'], 'vlc': ['three']}))
        for domain, ids in {'chrome': ['one', 'two'], 'vlc': ['three']}.items():
            for difficulty in ('examples',):
                folder = self.examples / difficulty / domain
                folder.mkdir(parents=True)
                for task_id in ids:
                    (folder / (task_id + '.json')).write_text(json.dumps({
                        'id': task_id, 'instruction': 'Do the task', 'config': [],
                        'evaluator': {'func': 'exact_match', 'result': {'type': 'constant', 'value': 'yes'}}}))

    def tasks(self, **kwargs):
        return load_tasks(self.root, official=False, **kwargs)

    def test_selection_domain_and_task(self):
        self.assertEqual(len(self.tasks()), 3)
        self.assertEqual([t.key for t in self.tasks(domains=['vlc'])], ['vlc/three'])
        self.assertEqual([t.key for t in self.tasks(task_ids=['chrome/one', 'three'])], ['chrome/one', 'vlc/three'])

    def test_unknown_or_filtered_tasks_fail(self):
        for kwargs in ({'domains': ['missing']}, {'task_ids': ['missing']}, {'task_ids': ['one'], 'domains': ['vlc']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.tasks(**kwargs)

    def test_official_manifest_is_checked_before_filter(self):
        with self.assertRaisesRegex(ValueError, 'manifest'):
            load_tasks(self.root, task_ids=['one'])

    def test_missing_duplicate_and_traversal_fail(self):
        manifest = self.examples / 'test_all.json'
        for data in ({'chrome': ['one', 'one']}, {'chrome': ['../one']}, {'../chrome': ['one']}, {'chrome': ['missing']}):
            manifest.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.tasks()

    def test_unsafe_embedded_id_and_empty_instruction_fail(self):
        path = self.tasks()[0].path
        data = json.loads(path.read_text())
        for key, value in [('id', '../wrong'), ('instruction', '')]:
            changed = {**data, key: value}
            path.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.tasks()

    def test_host_environment_marker_rejects_changed_dependencies(self):
        from waa_benchmark.preflight import environment_marker
        lockfile = self.root / "uv.lock"
        lockfile.write_text("locked dependencies")
        marker = self.root / ".agent-fleet-waa.json"
        marker.write_text(json.dumps({"version": 1, "lock_sha256": digest(lockfile), "packages": {"fake-package": "1.0"}}))
        with patch("importlib.metadata.version", return_value="1.0"):
            environment_marker(self.root, lockfile)
        with patch("importlib.metadata.version", return_value="2.0"), self.assertRaisesRegex(ValueError, "package"):
            environment_marker(self.root, lockfile)
        lockfile.write_text("changed lock")
        with self.assertRaisesRegex(ValueError, "stale"):
            environment_marker(self.root, lockfile)

    def test_runtime_integrity_rejects_extra_files_and_modified_hashes(self):
        from waa_benchmark.dataset import UPSTREAM_REVISION
        (self.root / 'runtime.json').write_text(json.dumps({'version': 1, 'revision': UPSTREAM_REVISION,
            'files': {str(p.relative_to(self.root)): digest(p) for p in self.root.rglob('*') if p.is_file()}}))
        validate_runtime(self.root)
        (self.root / 'unexpected').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            validate_runtime(self.root)
        (self.root / 'unexpected').unlink()
        (self.examples / 'test_all.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            validate_runtime(self.root)

    def test_lazy_exports_preserve_last_import_and_defined_function(self):
        path = self.root / '__init__.py'
        path.write_text('from .first import same\nfrom .second import same\ndef infeasible():\n    return 1\n')
        lazy_exports(path)
        namespace = {'__name__': 'example'}
        exec(compile(path.read_text(), str(path), 'exec'), namespace)  # noqa: S102 -- inspect generated fixture exports
        self.assertEqual(namespace['infeasible'](), 1)
        self.assertEqual(namespace['_EXPORTS']['same'], ('second', 'same'))


class SourceTests(unittest.TestCase):
    def test_prepare_copies_only_tracked_client_files_and_preserves_source(self):
        from waa_benchmark.dataset import CLIENT_PATH
        from waa_benchmark.source import prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "source"
            checkout.mkdir()
            client = checkout / CLIENT_PATH
            metrics = client / "desktop_env/evaluators/metrics"
            getters = client / "desktop_env/evaluators/getters"
            metrics.mkdir(parents=True)
            getters.mkdir(parents=True)
            (metrics / "__init__.py").write_text("from .docs import compare_image_text\n")
            (getters / "__init__.py").write_text("def get_rule():\n    return 1\n")
            original = "import easyocr\ndef compare_image_text():\n    reader = easyocr.Reader(['en'])\n    return reader\n"
            (metrics / "docs.py").write_text(original)
            (checkout / "LICENSE").write_text("Fixture license")
            (checkout / ".gitignore").write_text("*.ignored\n")
            subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "-c", "user.name=WAA Test", "-c", "user.email=waa@example.invalid",
                            "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--no-gpg-sign", "-m", "fixture"], check=True)
            revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
            (metrics / "local-cache.ignored").write_text("local data")
            with patch.dict("waa_benchmark.source.BENCHMARKS", {"waa-v2": {"revision": revision, "url": "https://example.invalid/waa"}}), patch("waa_benchmark.source.load_tasks"):
                prepared = prepare(checkout, root / "prepared")
                self.assertEqual((metrics / "docs.py").read_text(), original)
                self.assertFalse((prepared / "client/desktop_env/evaluators/metrics/local-cache.ignored").exists())
                self.assertNotIn("import easyocr\ndef", (prepared / "client/desktop_env/evaluators/metrics/docs.py").read_text())


class CLIConfigTests(unittest.TestCase):
    def test_fleet_batch_run_id_selects_matching_output_directory(self):
        with patch.dict(os.environ, {"RUN_ID": "fleet-run-1", "OUTPUT_ROOT": "/tmp/fleet-runs", "OUTPUT_PATH": ""}):
            args = parser().parse_args(["--cache", ".", "--all"])
        self.assertEqual(args.output, Path("/tmp/fleet-runs/fleet-run-1"))

    def test_caller_output_path_and_explicit_cli_override(self):
        with patch.dict(os.environ, {"RUN_ID": "fleet-run-1", "OUTPUT_PATH": "/tmp/waa-custom"}):
            args = parser().parse_args(["--cache", ".", "--all"])
            explicit = parser().parse_args(["--cache", ".", "--all", "--output", "/tmp/explicit"])
        self.assertEqual(args.output, Path("/tmp/waa-custom"))
        self.assertEqual(explicit.output, Path("/tmp/explicit"))

    def test_unsafe_run_id_is_rejected(self):
        with patch.dict(os.environ, {"RUN_ID": "../escape"}), self.assertRaises(ValueError):
            parser()


class TransportTests(unittest.TestCase):
    def test_mapping_keeps_paths_queries_and_external_requests(self):
        http = GuestHTTP('guest', {5000: 'http://node:30001', 9222: 'http://node:30002', 8080: 'http://node:30003'})
        self.assertEqual(http.rewrite('http://guest:5000/file?path=C:/hello'), 'http://node:30001/file?path=C:/hello')
        self.assertEqual(http.rewrite('http://guest:9222/json/version'), 'http://node:30002/json/version')
        self.assertEqual(http.rewrite('http://guest:8080/requests/status.xml'), 'http://node:30003/requests/status.xml')
        self.assertEqual(http.rewrite('https://example.com:5000/file'), 'https://example.com:5000/file')

    def test_guest_disables_proxy_and_swallowed_http_errors_are_detected(self):
        import requests
        http = GuestHTTP('guest', {5000: 'http://node:30001'})
        response = Mock(status_code=500)
        with patch('requests.sessions.Session.request', return_value=response) as original:
            with http.installed():
                session = requests.Session()
                session.get('http://guest:5000/setup/upload')
                self.assertFalse(session.trust_env)
                self.assertEqual(original.call_args.args[2], 'http://node:30001/setup/upload')
                self.assertEqual(original.call_args.kwargs['proxies'], {})
                with self.assertRaisesRegex(RuntimeError, 'guest request failed'):
                    http.check()
            session.get('https://example.com')
            self.assertEqual(original.call_count, 2)

    def test_execution_failure_is_detected_and_missing_result_is_allowed(self):
        import requests
        http = GuestHTTP('guest', {5000: 'http://node:30001'})
        with patch('requests.sessions.Session.request', return_value=Mock(status_code=200, json=lambda: {'returncode': 1})):
            with http.installed():
                requests.post('http://guest:5000/execute')
            with self.assertRaises(RuntimeError):
                http.check()
        http.failures = [(404, '/file')]
        http.check(allow_missing_file=True)
        http.failures = [(500, '/file')]
        with self.assertRaises(RuntimeError):
            http.check(allow_missing_file=True)

    def test_probe_recovery_clears_transient_failure(self):
        import requests
        http = GuestHTTP('guest', {9222: 'http://node:30002'})
        with patch('requests.sessions.Session.request', side_effect=[Mock(status_code=503), Mock(status_code=200)]):
            with http.installed():
                requests.get('http://guest:9222/json/version')
                requests.get('http://guest:9222/json/version')
            http.check()


class AgentTests(unittest.TestCase):
    def test_custom_factory_receives_model_and_resolution(self):
        agent = Mock(reset=Mock(), predict=Mock())
        factory = Mock(return_value=agent)
        with patch.dict(sys.modules, {'custom_waa': types.SimpleNamespace(build=factory)}):
            self.assertIs(load_agent('custom_waa:build', model='fake-model', screenshot_size=(100, 80)), agent)
            factory.assert_called_once_with(model='fake-model', screenshot_size=(100, 80))

    def test_invalid_agent_contract_fails(self):
        with self.assertRaises(ValueError):
            load_agent('unknown', model='fake', screenshot_size=(100, 80))
        with patch.dict(sys.modules, {'custom_waa': types.SimpleNamespace(build=lambda **_: object())}), self.assertRaises(TypeError):
            load_agent('custom_waa:build', model='fake', screenshot_size=(100, 80))

    def test_prediction_budget_records_actions_and_leaves_grading_to_verifier(self):
        agent = Mock()
        agent.predict.return_value = ('response', ['WAIT', 'DONE'], {'thought': 'done'}, {'scale': (1, 1)})
        session = object.__new__(NativeSession)
        session.benchmark, session.steps, session.done = 'waa-v2', 0, False
        session.env, session.http, session.recorder = Mock(), Mock(), Mock()
        session.obs = {'screenshot': b'image'}
        session.env.step.side_effect = [({'screenshot': b'next'}, 0, False, {}), ({'screenshot': b'end'}, 0, True, {})]
        session._predict(agent, 'task', max_steps=1, pause=0)
        self.assertEqual(session.steps, 1)
        self.assertEqual(session.recorder.record_step.call_count, 2)
        session.env.controller.update_computer.assert_called_once_with(scale=(1, 1))
        session.env.evaluate.assert_not_called()


class HarborAdapterTests(unittest.TestCase):
    setUp = Fixture.setUp
    tasks = Fixture.tasks

    def test_generated_tasks_have_native_setup_and_separate_verifier_config(self):
        from harbor.models.task.task import Task as HarborTask
        from waa_benchmark.adapter import materialize
        dataset = self.root / 'harbor'
        (self.root / 'runtime.json').write_text('{}')
        with patch('waa_benchmark.adapter.validate_runtime'), patch('waa_benchmark.adapter.load_tasks', return_value=self.tasks()):
            tasks = materialize(self.root, dataset, benchmark='waa-v2')
            task = HarborTask(dataset / 'chrome--one')
            self.assertEqual(task.config.environment.os.value, 'windows')
            self.assertEqual(task.instruction, 'Do the task\n')
            self.assertNotIn('evaluator', task.instruction)
            self.assertEqual((task.paths.environment_dir / 'native-task.json').read_bytes(), tasks[0].path.read_bytes())
            materialize(self.root, dataset, benchmark='waa-v2')
            (task.paths.environment_dir / 'native-task.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                materialize(self.root, dataset, benchmark='waa-v2')

class HarborLauncherTests(unittest.TestCase):
    setUp = Fixture.setUp
    tasks = Fixture.tasks

    def test_docker_backend_selection_and_environment_default(self):
        with patch.dict(os.environ, {"HARBOR_WAA_BACKEND": "docker", "OPIK_URL": ""}):
            args = parser().parse_args(["--cache", ".", "--all"])
            cmd = command(args, self.root, self.tasks(), self.root, self.root)
            self.assertIn("waa_benchmark.docker_environment:WAADockerEnvironment", cmd)
            self.assertEqual(parser().parse_args(["--cache", ".", "--all", "--backend", "kubevirt"]).backend, "kubevirt")

    def test_command_uses_harbor_integrations_and_passes_native_options(self):
        args = parser().parse_args(['--cache', str(self.root), '--benchmark', 'waa', '--all',
                                    '--workers', '3', '--output', str(self.root / 'job'),
                                    '--', '--ak', 'max_steps=30', '--max-retries', '2'])
        with patch.dict(os.environ, {'OPIK_URL': ''}):
            cmd = command(args, self.root / 'dataset', self.tasks(), self.root / 'runtime', self.root / 'pc-runtime')
        self.assertIn('Agents.WindowsAgentArena.agent:WAAAgent', cmd)
        self.assertIn('waa_benchmark.environment:WAAEnvironment', cmd)
        self.assertIn('waa_benchmark.verifier:WAAVerifier', cmd)
        self.assertEqual(cmd[cmd.index('--n-concurrent') + 1], '3')
        self.assertEqual(cmd[cmd.index('--job-name') + 1], 'job')
        self.assertEqual(cmd[-4:], ['--ak', 'max_steps=30', '--max-retries', '2'])
        self.assertEqual(cmd.count('--include-task-name'), 3)

    def test_launcher_replaces_itself_with_harbor_and_dry_run_hides_options(self):
        import io
        from contextlib import redirect_stdout

        from waa_benchmark.launch import main
        argv = ['waa', '--cache', str(self.root), '--runtime', str(self.root), '--all', '--agent', 'custom:build']
        with patch.dict(os.environ, {'OPIK_URL': ''}), patch('sys.argv', argv), \
                patch('waa_benchmark.launch.environment_marker'), patch('waa_benchmark.launch.validate_runtime'), \
                patch('waa_benchmark.launch.load_tasks', return_value=self.tasks()), \
                patch('waa_benchmark.launch.materialize') as convert, patch('os.execvpe') as execute, \
                patch.dict(sys.modules, {'custom': types.SimpleNamespace(build=Mock())}):
            main()
            self.assertEqual(execute.call_args.args[1][1], 'run')
            self.assertIn('--path', execute.call_args.args[1])
            convert.assert_called_once()
            execute.reset_mock()
            stream = io.StringIO()
            with patch('sys.argv', [*argv, '--dry-run', '--', '--ae', 'API_KEY=sk-fake-private']), redirect_stdout(stream):
                main()
            self.assertNotIn('sk-fake-private', stream.getvalue())
            self.assertEqual(len(json.loads(stream.getvalue())['selected']), 3)
            execute.assert_not_called()
            self.assertEqual(convert.call_count, 1)

    def test_opik_endpoint_selects_shared_cli(self):
        args = parser().parse_args(['--cache', '.', '--all'])
        with patch.dict(os.environ, {'OPIK_URL': 'https://opik.example.invalid/api', 'HARBOR_OPIK_BIN': '/fake/opik'}):
            self.assertEqual(command(args, self.root, self.tasks(), self.root, self.root)[:2], ['/fake/opik', 'harbor'])


class HarborBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_verifier_returns_native_reward_and_writes_harbor_reward_file(self):
        from harbor.models.trial.paths import TrialPaths
        from waa_benchmark.environment import WAAEnvironment
        from waa_benchmark.verifier import WAAVerifier
        with tempfile.TemporaryDirectory() as tmp:
            paths = TrialPaths(Path(tmp))
            environment = Mock(spec=WAAEnvironment)
            environment.native_request = AsyncMock(return_value={'score': -.25, 'status': 'completed'})
            verifier = WAAVerifier(task=Mock(), trial_paths=paths, environment=environment)
            result = await verifier.verify()
            self.assertEqual(result.rewards, {'reward': -.25})
            self.assertEqual(float(paths.reward_text_path.read_text()), -.25)
            environment.native_request.assert_awaited_once_with('evaluate')

    async def test_verifier_transport_failure_is_not_a_zero_score(self):
        from harbor.models.trial.paths import TrialPaths
        from waa_benchmark.environment import WAAEnvironment
        from waa_benchmark.verifier import WAAVerifier
        with tempfile.TemporaryDirectory() as tmp:
            paths = TrialPaths(Path(tmp))
            environment = Mock(spec=WAAEnvironment)
            environment.native_request = AsyncMock(side_effect=RuntimeError('guest failed'))
            verifier = WAAVerifier(task=Mock(), trial_paths=paths, environment=environment)
            with self.assertRaises(RuntimeError):
                await verifier.verify()
            self.assertFalse(paths.reward_text_path.exists())

    async def test_harbor_agent_uses_trial_environment_and_records_context(self):
        from harbor.models.agent.context import AgentContext
        from waa_benchmark.environment import WAAEnvironment

        from Agents.WindowsAgentArena.agent import WAAAgent
        environment = Mock(spec=WAAEnvironment)
        environment.native_request = AsyncMock(return_value={'steps': 2, 'termination': 'agent'})
        agent = WAAAgent(logs_dir=Path('.'), model_name='fake', factory='custom:build', max_steps=5)
        await agent.setup(environment)
        context = AgentContext()
        await agent.run('Do task', environment, context)
        self.assertEqual(context.metadata['steps'], 2)
        self.assertEqual(environment.native_request.call_args.kwargs['factory'], 'custom:build')
        with self.assertRaises(TypeError):
            await agent.setup(Mock())

    async def test_cancelled_native_phase_kills_worker_before_other_phases(self):
        from waa_benchmark.environment import WAAEnvironment
        env = object.__new__(WAAEnvironment)
        env._phase_lock = asyncio.Lock()
        env.worker = Mock(pid=123456, returncode=None)
        env.worker.stdin.drain = AsyncMock()
        env.worker.stdout.readline = AsyncMock(side_effect=asyncio.CancelledError())
        env.worker.wait = AsyncMock()
        env.worker_log = Mock()
        with patch('waa_benchmark.environment.os.killpg') as kill, self.assertRaises(asyncio.CancelledError):
            await env.native_request('agent')
        kill.assert_called_once()
        self.assertIsNone(env.worker)
        with self.assertRaisesRegex(RuntimeError, 'not running'):
            await env.native_request('evaluate')

    async def test_worker_cleanup_failure_still_attempts_vm_cleanup(self):
        from kubevirt_windows.environment import KubeVirtWindowsEnvironment
        from waa_benchmark.environment import WAAEnvironment
        env = object.__new__(WAAEnvironment)
        with patch.object(WAAEnvironment, '_stop_worker', AsyncMock(side_effect=RuntimeError('worker cleanup'))), \
                patch.object(KubeVirtWindowsEnvironment, 'stop', AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                await env.stop(delete=True)
            stop.assert_awaited_once_with(delete=True)


if __name__ == '__main__':
    unittest.main()
