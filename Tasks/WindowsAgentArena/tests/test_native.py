"""Pinned-client integration tests; enabled by explicit WAA_TEST_RUNTIME."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'Tasks/WindowsAgentArena/src'))
RUNTIME = os.environ.get('WAA_TEST_RUNTIME')
BENCHMARK = os.environ.get('WAA_TEST_BENCHMARK', 'waa-v2')
PCAGENT_RUNTIME = os.environ.get('WAA_TEST_PCAGENT_RUNTIME', RUNTIME)


@unittest.skipUnless(RUNTIME, 'Set WAA_TEST_RUNTIME to an explicitly prepared pinned client')
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = Path(RUNTIME).resolve()
        sys.path.insert(0, str(cls.runtime / 'client'))
        from waa_benchmark.source import validate_runtime
        validate_runtime(cls.runtime, benchmark=BENCHMARK)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_every_official_task_imports_native_functions(self):
        from waa_benchmark.preflight import check_client
        coverage = check_client(self.runtime / 'client', self.root, benchmark=BENCHMARK,
                                pcagent_client=Path(PCAGENT_RUNTIME) / 'client')
        self.assertEqual(coverage['tasks'], 154 if BENCHMARK == 'waa' else 141)
        self.assertEqual(coverage['domains'], 12)
        self.assertIn('chrome_open_tabs', coverage['setups'])
        self.assertIn('vlc_config', coverage['getters'])
        self.assertGreater(len(coverage['metrics']), 35)

    def test_all_official_tasks_are_valid_harbor_tasks(self):
        from harbor.models.task.task import Task as HarborTask
        from waa_benchmark.adapter import materialize, task_name
        tasks = materialize(self.runtime, self.root / 'harbor', benchmark=BENCHMARK)
        self.assertEqual(len(tasks), 154 if BENCHMARK == 'waa' else 141)
        for native in tasks:
            task = HarborTask(self.root / 'harbor' / task_name(native))
            self.assertEqual(task.config.environment.os.value, 'windows')
            self.assertEqual(task.config.metadata['native_task_id'], native.id)
            self.assertEqual(json.loads((task.paths.environment_dir / 'native-task.json').read_text()),
                             json.loads(native.path.read_text()))

    def test_dry_run_validates_without_model_or_cluster_config(self):
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('HARBOR_KUBEVIRT_', 'OPENAI_', 'API_KEY'))}
        environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(REPO) + ':' + str(REPO / 'Tasks/WindowsAgentArena/src'))
        output = self.root / 'dry-run'
        for backend in ("kubevirt", "docker"):
            with self.subTest(backend=backend):
                completed = subprocess.run([sys.executable, '-m', 'waa_benchmark.launch', '--runtime', str(self.runtime),
                                    '--benchmark', BENCHMARK, '--cache', str(self.runtime.parent.parent),
                                    '--backend', backend, '--all', '--dry-run', '--output', str(output)],
                                   env=environment, capture_output=True, text=True, check=True)
                plan = json.loads(completed.stdout)
                self.assertEqual(len(plan['selected']), 154 if BENCHMARK == 'waa' else 141)
                self.assertEqual(plan['agent'], 'pcagent')
                self.assertEqual(plan['backend'], backend)
        self.assertFalse(output.exists())

    def test_native_metric_composition_and_infeasible_scoring(self):
        from waa_benchmark.desktop import create_desktop
        env = create_desktop('guest', self.root, screen_size=(1280, 720), action_space='pyautogui')
        env.setup_controller.setup = Mock()
        # Use actual native metrics/getters with deterministic state; exercise
        # upstream list composition rather than copying its semantics.
        base = {'id': 'test', 'instruction': 'task', 'config': [], 'evaluator': {
            'func': ['exact_match', 'exact_match'], 'result': [
                {'type': 'rule', 'rules': 'yes'}, {'type': 'rule', 'rules': 'no'}],
            'expected': [{'type': 'rule', 'rules': {'expected': 'yes'}},
                         {'type': 'rule', 'rules': {'expected': 'yes'}}]}}
        for conjunction, expected in [('and', 0), ('or', 1)]:
            base['evaluator']['conj'] = conjunction
            env._set_task_info(base)
            self.assertEqual(env.evaluate(), expected)
        base['evaluator'] = {'func': 'infeasible'}
        env._set_task_info(base)
        env.action_history = ['FAIL']
        self.assertEqual(env.evaluate(), 1)
        env.action_history = ['DONE']
        self.assertEqual(env.evaluate(), 0)

    def test_screenshot_coordinate_space_matches_selected_release(self):
        from PIL import Image
        from waa_benchmark.desktop import create_desktop
        raw = io.BytesIO()
        Image.new('RGB', (640, 480), 'white').save(raw, format='PNG')
        env = create_desktop('guest', self.root, screen_size=(1280, 720),
                             action_space='pyautogui', benchmark=BENCHMARK)
        env.controller.get_screenshot = Mock(return_value=raw.getvalue())
        with Image.open(io.BytesIO(env._get_screenshot())) as screenshot:
            self.assertEqual(screenshot.size, (640, 480) if BENCHMARK == 'waa' else (1280, 720))

    def test_cdp_rewrites_advertised_guest_websocket(self):
        from playwright.sync_api import BrowserType
        from waa_benchmark.desktop import GuestHTTP
        http = GuestHTTP('guest', {9222: 'http://node:30002'})
        response = Mock()
        response.json.return_value = {'webSocketDebuggerUrl': 'ws://localhost:9222/devtools/browser/id'}
        with patch.object(BrowserType, 'connect_over_cdp', return_value='browser') as original, \
                patch('requests.get', return_value=response), http.playwright():
            self.assertEqual(BrowserType.connect_over_cdp(object(), 'http://guest:9222'), 'browser')
        self.assertEqual(original.call_args.args[1], 'ws://node:30002/devtools/browser/id')


if __name__ == '__main__':
    unittest.main()
