from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
PROXY = HARBOR_DIR / "model-fusion" / "harboropik.sh"


class ModelFusionHarborOpikTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.capture_file = self.root / "captured-args"
        self.real_opik = self.root / "real-opik"
        self.router_dir = self.root / "router-frontend"
        self.task_file = self.root / "task.md"

        (self.router_dir / "templates").mkdir(parents=True)
        (self.router_dir / "subagent_barrier_gate.py").write_text(
            "# fixture\n", encoding="utf-8"
        )
        self.task_file.write_text("fixture task\n", encoding="utf-8")
        self.real_opik.write_text(
            textwrap.dedent(
                r"""
                #!/usr/bin/env python3
                import os
                from pathlib import Path
                import sys

                capture = Path(os.environ["CAPTURE_FILE"])
                capture.write_bytes(
                    b"\0".join(arg.encode() for arg in sys.argv[1:]) + b"\0"
                )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.real_opik.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update(
            {
                "CAPTURE_FILE": str(self.capture_file),
                "MODEL_FUSION_REAL_HARBOR_OPIK_BIN": str(self.real_opik),
                "HARBOR_FUSION_ROUND_ROUTER_DIR": str(self.router_dir),
                "HARBOR_FUSION_ROUND_ROUTER_MOUNT_PATH": "/opt/fusion-router",
                "HARBOR_FUSION_TASK_FILE_SOURCE": str(self.task_file),
                "HARBOR_FUSION_TASK_FILE": "/opt/fusion-task/task.md",
                "HARBOR_CLAUDE_CODE_AGENTS_JSON": '{"reviewer": {}}',
                "HARBOR_FUSION_ROUND_GATE": "1",
                "HARBOR_FUSION_ROUND_GATE_PATH": "/opt/fusion-router/gate.py",
                "HARBOR_FUSION_ROUND_GATE_MODE": "mid-turn-fusion",
                "SPAN_FORCE_MODE": "mid-turn-fusion",
                "SPAN_FORCE_FUSION": "1",
                "SPAN_GATE_STATE_PATH": "/logs/agent/gate-state.json",
                "SPAN_MID_TURN_ARTIFACT_ROOT": "/logs/agent/artifacts",
                "SPAN_HOOK_REASON_MAX_BYTES": "4321",
                "SPAN_PANEL_MODELS": "panel-a,panel-b",
                "SPAN_PANEL_COUNT": "2",
                "HARBOR_FUSION_MAX_FUSIONS_PER_TASK": "3",
                "HARBOR_FUSION_PANEL_CALL_BUDGET": "4",
                "HARBOR_ANTHROPIC_BASE_URL": "https://gateway.internal",
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(PROXY), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _captured_args(self) -> list[str]:
        return [
            part.decode()
            for part in self.capture_file.read_bytes().split(b"\0")
            if part
        ]

    @staticmethod
    def _option_value(args: list[str], option: str) -> str:
        index = args.index(option)
        if index == len(args) - 1:
            raise AssertionError(f"{option} is missing its value: {args!r}")
        return args[index + 1]

    def test_run_preserves_existing_mount_and_injects_fusion_contract(self) -> None:
        existing_mount = {
            "type": "bind",
            "source": "/existing/source",
            "target": "/existing/target",
            "read_only": False,
        }
        result = self._run(
            "harbor",
            "run",
            "--dataset",
            "auto",
            "--mounts-json",
            json.dumps([existing_mount]),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        args = self._captured_args()
        mounts = json.loads(self._option_value(args, "--mounts-json"))
        self.assertEqual(
            mounts,
            [
                existing_mount,
                {
                    "type": "bind",
                    "source": str(self.router_dir),
                    "target": "/opt/fusion-router",
                    "read_only": True,
                },
                {
                    "type": "bind",
                    "source": str(self.task_file),
                    "target": "/opt/fusion-task/task.md",
                    "read_only": True,
                },
            ],
        )

        actual_ae = {
            args[index + 1]
            for index, arg in enumerate(args[:-1])
            if arg == "--ae"
        }
        expected_ae = {
            'HARBOR_CLAUDE_CODE_AGENTS_JSON={"reviewer": {}}',
            "HARBOR_FUSION_ROUND_GATE=1",
            "HARBOR_FUSION_ROUND_GATE_PATH=/opt/fusion-router/gate.py",
            "HARBOR_FUSION_ROUND_GATE_MODE=mid-turn-fusion",
            "HARBOR_FUSION_TASK_FILE=/opt/fusion-task/task.md",
            "FUSION_TASK_FILE=/opt/fusion-task/task.md",
            "SPAN_FORCE_MODE=mid-turn-fusion",
            "SPAN_FORCE_FUSION=1",
            "SPAN_GATE_STATE_PATH=/logs/agent/gate-state.json",
            "SPAN_MID_TURN_ARTIFACT_ROOT=/logs/agent/artifacts",
            "SPAN_HOOK_REASON_MAX_BYTES=4321",
            "SPAN_PANEL_MODELS=panel-a,panel-b",
            "SPAN_PANEL_COUNT=2",
            "SPAN_MID_TURN_MAX_FUSIONS_PER_TASK=3",
            "SPAN_MID_TURN_PANEL_CALL_BUDGET=4",
            "NO_PROXY=gateway.internal",
            "no_proxy=gateway.internal",
        }
        self.assertEqual(actual_ae, expected_ae)

    def test_run_creates_mounts_argument_when_caller_omits_it(self) -> None:
        result = self._run("harbor", "run", "--dataset", "auto")
        self.assertEqual(result.returncode, 0, result.stderr)

        args = self._captured_args()
        mounts = json.loads(self._option_value(args, "--mounts-json"))
        self.assertEqual(len(mounts), 2)
        self.assertEqual(
            {mount["source"] for mount in mounts},
            {str(self.router_dir), str(self.task_file)},
        )
        self.assertTrue(all(mount["read_only"] is True for mount in mounts))

    def test_help_probe_is_forwarded_without_injection(self) -> None:
        result = self._run("harbor", "run", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._captured_args(), ["harbor", "run", "--help"])


if __name__ == "__main__":
    unittest.main()
