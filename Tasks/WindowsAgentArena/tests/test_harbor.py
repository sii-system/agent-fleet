"""Actual Harbor trials with native clients and loopback Windows/model services."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'Tasks/WindowsAgentArena/src'))
sys.path.insert(0, str(REPO / 'Agents/utils/common/Harbor'))
RUNTIME = os.environ.get('WAA_TEST_RUNTIME')
BENCHMARK = os.environ.get('WAA_TEST_BENCHMARK', 'waa-v2')
PCAGENT_RUNTIME = os.environ.get('WAA_TEST_PCAGENT_RUNTIME', RUNTIME)


@unittest.skipUnless(RUNTIME, 'Prepare the pinned native client to exercise Harbor trials')
class HarborTrialTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_harbor_trial_runs_setup_agent_native_verifier_and_cleanup(self):
        from harbor.models.trial.config import TrialConfig
        from harbor.trial.trial import Trial
        from kubevirt_windows.environment import KubeVirtWindowsEnvironment
        from PIL import Image
        from waa_benchmark.adapter import materialize
        from waa_benchmark.dataset import BENCHMARKS, digest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Parse every generated task using Harbor's own schema elsewhere;
            # here a controlled task runs the complete production phase bridge.
            dataset = root / 'dataset'
            tasks = materialize(RUNTIME, dataset, benchmark=BENCHMARK)
            task = dataset / f'{tasks[0].domain}--{tasks[0].id}'
            config = {'id': 'fixture', 'instruction': 'Click and finish',
                      'config': [{'type': 'create_folder', 'parameters': {'path': 'C:/fixture'}}],
                      'evaluator': {'func': 'exact_match',
                                    'result': {'type': 'vm_command_line', 'command': ['cmd', '/c', 'echo finished']},
                                    'expected': {'type': 'rule', 'rules': {'expected': 'finished'}}}}
            native = task / 'environment/native-task.json'
            native.write_text(json.dumps(config))
            (task / 'instruction.md').write_text(config['instruction'])
            (task / 'environment/waa.json').write_text(json.dumps({
                'benchmark': BENCHMARK, 'revision': BENCHMARKS[BENCHMARK]['revision'],
                'domain': 'fixture', 'id': 'fixture', 'task_sha256': digest(native)}))
            png = io.BytesIO()
            Image.new('RGB', (1280, 720), 'white').save(png, format='PNG')
            png = png.getvalue()
            seen, predictions = [], []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def send(self, value, *, binary=False):
                    body = value if binary else json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Content-Type', 'image/png' if binary else 'application/json')
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    seen.append(self.path)
                    if self.path == '/screenshot':
                        self.send(png, binary=True)
                    elif self.path == '/obs_winagent':
                        self.send({'image': base64.b64encode(png).decode(), 'window_title': 'Desktop',
                                   'rect': [0, 0, 1280, 720], 'window_names_str': 'Desktop',
                                   'computer_clipboard': '', 'human_input': None})
                    else:
                        self.send({'status': 'success'})

                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    seen.append(self.path)
                    if self.path == '/v1/chat/completions':
                        predictions.append(json.loads(body))
                        action = 'click (12, 18)' if len(predictions) == 1 else 'finish'
                        self.send({'id': 'fake', 'object': 'chat.completion', 'created': 0, 'model': 'fake-model',
                                   'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Plan: Test\nAction: ' + action},
                                                'finish_reason': 'stop'}]})
                    elif self.path == '/screen_size':
                        self.send({'width': 1280, 'height': 720})
                    else:
                        self.send({'status': 'success', 'output': 'finished', 'error': '', 'returncode': 0})

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            endpoint = f'http://127.0.0.1:{server.server_port}'
            control = Mock()
            control.guest_endpoints = AsyncMock(return_value={5000: endpoint, 9222: endpoint, 8080: endpoint})
            settings = Mock(guest_protocol='waa', guest_port=5000, guest_node_port=0, extra_guest_ports=(9222, 8080),
                            namespace='bench', image='golden', command_timeout=30)
            transport = Mock()
            transport.execute = AsyncMock(return_value={'stdout': '', 'stderr': '', 'return_code': 0})
            transport.list_files = AsyncMock(return_value=[])

            async def start(environment, force_build=False):
                environment._started = True
                environment.transport = transport

            async def stop(environment, delete=True):
                environment._started = False

            real_spawn = asyncio.create_subprocess_exec

            async def spawn(*command, **kwargs):
                # Native stabilization waits remain in production. Only this
                # subprocess fixture replaces them to keep CI deterministic.
                bootstrap = ('import sys; sys.path.insert(0, ' + repr(str(Path(RUNTIME) / 'client')) + '); '
                             'import desktop_env.envs.desktop_env as d; d.time.sleep=lambda _: None; '
                             'from waa_benchmark.worker import main; main()')
                return await real_spawn(command[0], '-c', bootstrap, command[-1], **kwargs)

            trial_config = TrialConfig.model_validate({
                'trial_name': 'waa-native-fixture', 'trials_dir': str(root / 'trials'), 'task': {'path': str(task)},
                'environment': {'import_path': 'waa_benchmark.environment:WAAEnvironment', 'kwargs': {
                    'runtime': RUNTIME, 'pcagent_runtime': PCAGENT_RUNTIME, 'screen_size': [1280, 720]}},
                'agent': {'import_path': 'Agents.WindowsAgentArena.agent:WAAAgent', 'model_name': 'fake-model',
                          'kwargs': {'max_steps': 3, 'pause': 0}},
                'verifier': {'import_path': 'waa_benchmark.verifier:WAAVerifier'},
            })
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-fake', 'OPENAI_BASE_URL': endpoint + '/v1',
                         'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
                         'ALL_PROXY': '', 'all_proxy': '', 'HTTP_PROXY': '', 'http_proxy': '', 'HTTPS_PROXY': '', 'https_proxy': '',
                         'PYTHONDONTWRITEBYTECODE': '1'}), \
                    patch('kubevirt_windows.environment.Settings.from_env', return_value=settings), \
                    patch('kubevirt_windows.environment.KubeVirtControl', return_value=control), \
                    patch.object(KubeVirtWindowsEnvironment, 'start', start), \
                    patch.object(KubeVirtWindowsEnvironment, 'stop', autospec=True, side_effect=stop) as cleanup, \
                    patch('waa_benchmark.environment.asyncio.create_subprocess_exec', side_effect=spawn):
                trial = await Trial.create(trial_config)
                result = await trial.run()
                self.assertIsNone(result.exception_info, result.exception_info)
                self.assertEqual(result.verifier_result.rewards, {'reward': 1})
                self.assertEqual(result.agent_result.metadata['steps'], 2)
                cleanup.assert_awaited_once()
            self.assertEqual(len(predictions), 2)
            for path in ['/setup/create_folder', '/update_computer', '/execute']:
                self.assertIn(path, seen)
            result_dir = root / 'trials/waa-native-fixture'
            self.assertTrue((result_dir / 'result.json').is_file())
            self.assertEqual(float((result_dir / 'verifier/reward.txt').read_text()), 1)
            self.assertTrue((result_dir / 'agent/native/traj.jsonl').is_file())
            self.assertTrue((result_dir / 'waa-worker.log').is_file())
