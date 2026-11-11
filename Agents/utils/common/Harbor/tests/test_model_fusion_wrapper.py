from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
MODEL_FUSION_DIR = HARBOR_DIR / "model-fusion"
WRAPPER = MODEL_FUSION_DIR / "run_one_tb21_task.sh"
PROXY_COMMON = MODEL_FUSION_DIR / "proxy_common.sh"
PROXY_WRAPPERS = (
    MODEL_FUSION_DIR / "harboropik.sh",
    MODEL_FUSION_DIR / "mimo-code" / "harboropik.sh",
    MODEL_FUSION_DIR / "openrouter" / "harboropik.sh",
)


class ModelFusionWrapperTest(unittest.TestCase):
    def test_proxy_wrappers_share_common_shell_helpers(self) -> None:
        self.assertTrue(PROXY_COMMON.is_file())
        for wrapper in PROXY_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                script = wrapper.read_text(encoding="utf-8")
                self.assertRegex(
                    script,
                    re.compile(r'^\. ".*proxy_common\.sh"$', re.MULTILINE),
                )
                for helper in (
                    "model_fusion_proxy_is_injectable",
                    "model_fusion_proxy_append_gateway_no_proxy",
                    "model_fusion_proxy_merge_readonly_mounts",
                ):
                    self.assertIn(helper, script)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.dataset = self.root / "dataset"
        task_dir = self.dataset / "fixture-task"
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("fixture task\n", encoding="utf-8")

        self.output_root = self.root / "output"
        self.fake_harbor = self.root / "harbor"
        self.fake_router = self.root / "router"
        self._write_fake_harbor()
        self._write_fake_router()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_fake_harbor(self) -> None:
        self.fake_harbor.mkdir(parents=True)
        env_script = textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail

            MODEL="${MODEL:-shared-model}"
            HARBOR_MODEL="${HARBOR_MODEL:-$MODEL}"
            HARBOR_ANTHROPIC_MODEL="${HARBOR_ANTHROPIC_MODEL:-$MODEL}"
            HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL="${HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL:-$MODEL}"
            HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL="${HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL:-$MODEL}"
            HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL="${HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL:-$MODEL}"
            HARBOR_CLAUDE_CODE_SUBAGENT_MODEL="${HARBOR_CLAUDE_CODE_SUBAGENT_MODEL:-$MODEL}"
            HARBOR_OPIK_BIN="${HARBOR_OPIK_BIN:-/bin/true}"
            HARBOR_DISALLOWED_TOOLS="${HARBOR_DISALLOWED_TOOLS:-WebSearch Task Agent Bash}"

            TASK_FILE="${TASK_FILE:-${OUTPUT_PATH}/tasks.txt}"
            QUEUE_DIR="${QUEUE_DIR:-${OUTPUT_PATH}/queue/${AGENT}}"
            RUNTIME_DIR="${RUNTIME_DIR:-${OUTPUT_PATH}/runtime/${AGENT}}"
            JOBS_ROOT="${JOBS_ROOT:-${OUTPUT_PATH}/jobs/${AGENT}}"
            NEXT_INDEX_FILE="${NEXT_INDEX_FILE:-${QUEUE_DIR}/next-index}"

            export MODEL HARBOR_MODEL
            export HARBOR_ANTHROPIC_MODEL HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL
            export HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL
            export HARBOR_CLAUDE_CODE_SUBAGENT_MODEL HARBOR_OPIK_BIN HARBOR_DISALLOWED_TOOLS
            export TASK_FILE QUEUE_DIR RUNTIME_DIR JOBS_ROOT NEXT_INDEX_FILE

            harbor_init_run_dirs() {
              mkdir -p "$OUTPUT_PATH" "$QUEUE_DIR" "$RUNTIME_DIR" "$JOBS_ROOT"
              touch "$QUEUE_DIR/done.txt" "$QUEUE_DIR/failed.txt"
            }

            harbor_reset_run_state() {
              rm -f "$NEXT_INDEX_FILE"
              : > "$QUEUE_DIR/done.txt"
              : > "$QUEUE_DIR/failed.txt"
            }

            harbor_ensure_dataset() { :; }

            harbor_prepare_task_file() {
              mkdir -p "$(dirname "$TASK_FILE")"
              if [[ "${RESET_RUN:-0}" == "1" || ! -s "$TASK_FILE" ]]; then
                cp "$TASK_SOURCE_FILE" "$TASK_FILE"
              fi
              echo 1 > "$NEXT_INDEX_FILE"
            }

            harbor_prepare_or_select_wheels() { :; }
            """
        ).lstrip()
        (self.fake_harbor / "env.sh").write_text(env_script, encoding="utf-8")

        worker_script = textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            [[ ! -e "$JOBS_ROOT/stale.marker" ]]
            printf '%s\n' "$JOBS_ROOT" > "$OUTPUT_PATH/worker-jobs-root.txt"
            printf '%s\n' "$RESET_RUN" > "$OUTPUT_PATH/worker-reset-run.txt"
            cp "$TASK_FILE" "$OUTPUT_PATH/worker-task.txt"
            task_safe="$(printf '%s' "$HARBOR_TASK_ID" | tr '/[:space:]' '___' | tr -cd 'A-Za-z0-9._-')"
            task_dir="$JOBS_ROOT/worker-1/1-${task_safe}"
            mkdir -p "$task_dir"
            printf '%s\n' '{"verifier_result":{"rewards":{"reward":1}}}' > "$task_dir/result.json"
            """
        ).lstrip()
        worker = self.fake_harbor / "run_harbor_worker.sh"
        worker.write_text(worker_script, encoding="utf-8")
        worker.chmod(0o755)

    def _write_fake_router(self) -> None:
        frontend = (
            self.fake_router
            / "src"
            / "sii_fusion_router"
            / "frontends"
            / "claude_code"
        )
        (frontend / "templates").mkdir(parents=True)
        (frontend / "subagent_barrier_gate.py").write_text(
            "# fixture\n", encoding="utf-8"
        )
        prompts = self.fake_router / "prompts" / "mid_turn_fusion"
        prompts.mkdir(parents=True)
        (prompts / "panel.md").write_text("panel\n", encoding="utf-8")
        (prompts / "outer.md").write_text("outer\n", encoding="utf-8")

        builder = textwrap.dedent(
            r"""
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys

            def option(name):
                return sys.argv[sys.argv.index(name) + 1]

            command = sys.argv[1]
            if command == "prepare":
                capture = {
                    name: os.environ.get(name, "")
                    for name in (
                        "RUN_ID",
                        "DATASET_NAME",
                        "MODEL",
                        "HARBOR_MODEL",
                        "HARBOR_ANTHROPIC_MODEL",
                        "HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL",
                        "HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL",
                        "HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL",
                    )
                }
                Path(option("--output-agents")).write_text("{}\n", encoding="utf-8")
                Path(option("--output-prompt")).write_text(
                    "fixture prompt\n", encoding="utf-8"
                )
                Path(option("--output-fusion")).write_text(
                    json.dumps({"capture": capture}) + "\n", encoding="utf-8"
                )
            elif command == "finalize":
                fusion_path = Path(option("--fusion-json"))
                data = json.loads(fusion_path.read_text(encoding="utf-8"))
                data["finalize"] = {
                    "jobs_root": option("--jobs-root"),
                    "result_json": option("--result-json"),
                }
                fusion_path.write_text(json.dumps(data) + "\n", encoding="utf-8")
            else:
                raise SystemExit(f"unexpected command: {command}")
            """
        ).lstrip()
        builder_path = frontend / "task_subagent_prompt.py"
        builder_path.write_text(builder, encoding="utf-8")
        builder_path.chmod(0o755)

    def _base_env(self) -> dict[str, str]:
        return {
            "HOME": str(self.root / "home"),
            "PATH": os.environ["PATH"],
            "TASK_ID": "fixture-task",
            "DATASET_PATH": str(self.dataset),
            "OUTPUT_ROOT": str(self.output_root),
            "FUSION_ROUTER_DIR": str(self.fake_router),
            "HARBOR_DIR": str(self.fake_harbor),
            "MODEL": "shared-model",
            "MAIN_MODEL": "selected-main-model",
            "SPAN_PANEL_MODELS": "panel-a,panel-b",
            "SPAN_OUTER_MODEL": "outer-model",
        }

    def _run_wrapper(
        self, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = self._base_env()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(WRAPPER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _fusion_files(self) -> list[Path]:
        pattern = "*/mid-turn-fusion/fixture-task/fusion.json"
        return sorted(self.output_root.glob(pattern))

    def test_defaults_use_local_dataset_and_unique_task_run_ids(self) -> None:
        for _ in range(2):
            result = self._run_wrapper({"MID_TURN_PREPARE_ONLY": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)

        fusion_files = self._fusion_files()
        self.assertEqual(len(fusion_files), 2)
        captures = [json.loads(path.read_text())["capture"] for path in fusion_files]
        run_ids = {capture["RUN_ID"] for capture in captures}
        self.assertEqual(len(run_ids), 2)
        self.assertTrue(all("fixture-task" in run_id for run_id in run_ids))
        self.assertTrue(all(capture["DATASET_NAME"] == "auto" for capture in captures))

    def test_main_model_replaces_only_shared_derived_aliases(self) -> None:
        result = self._run_wrapper(
            {
                "RUN_ID": "derived-models",
                "MID_TURN_PREPARE_ONLY": "1",
                "HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL": "caller-sonnet",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        fusion = (
            self.output_root
            / "derived-models/mid-turn-fusion/fixture-task/fusion.json"
        )
        capture = json.loads(fusion.read_text())["capture"]
        self.assertEqual(capture["MODEL"], "selected-main-model")
        self.assertEqual(capture["HARBOR_MODEL"], "selected-main-model")
        self.assertEqual(capture["HARBOR_ANTHROPIC_MODEL"], "selected-main-model")
        self.assertEqual(
            capture["HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL"], "selected-main-model"
        )
        self.assertEqual(
            capture["HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL"], "caller-sonnet"
        )
        self.assertEqual(
            capture["HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL"], "selected-main-model"
        )

    def test_caller_explicit_anthropic_aliases_are_preserved(self) -> None:
        explicit_aliases = {
            "HARBOR_ANTHROPIC_MODEL": "caller-main",
            "HARBOR_ANTHROPIC_DEFAULT_OPUS_MODEL": "caller-opus",
            "HARBOR_ANTHROPIC_DEFAULT_SONNET_MODEL": "caller-sonnet",
            "HARBOR_ANTHROPIC_DEFAULT_HAIKU_MODEL": "caller-haiku",
        }
        result = self._run_wrapper(
            {
                "RUN_ID": "explicit-models",
                "MID_TURN_PREPARE_ONLY": "1",
                **explicit_aliases,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        fusion = (
            self.output_root
            / "explicit-models/mid-turn-fusion/fixture-task/fusion.json"
        )
        capture = json.loads(fusion.read_text())["capture"]
        for name, value in explicit_aliases.items():
            self.assertEqual(capture[name], value)

    def test_reused_output_refreshes_task_and_scoped_job_state(self) -> None:
        output_path = self.output_root / "reused-run"
        old_jobs_root = output_path / "jobs" / "claude-code"
        scoped_jobs_root = old_jobs_root / "model-fusion-fixture-task"
        scoped_jobs_root.mkdir(parents=True)
        (scoped_jobs_root / "stale.marker").write_text("stale\n", encoding="utf-8")
        old_result = old_jobs_root / "worker-9/9-first-task/trial/result.json"
        old_result.parent.mkdir(parents=True)
        old_result.write_text('{"verifier_result": {}}\n', encoding="utf-8")
        (output_path / "tasks.txt").write_text("first-task\n", encoding="utf-8")

        result = self._run_wrapper({"RUN_ID": "reused-run"})
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual((output_path / "tasks.txt").read_text(), "fixture-task\n")
        self.assertEqual(
            (output_path / "worker-task.txt").read_text(), "fixture-task\n"
        )
        self.assertEqual(
            (output_path / "worker-reset-run.txt").read_text(), "1\n"
        )
        self.assertFalse((scoped_jobs_root / "stale.marker").exists())
        self.assertTrue(old_result.exists(), "unrelated shared jobs should be retained")

        fusion_path = output_path / "mid-turn-fusion/fixture-task/fusion.json"
        finalized = json.loads(fusion_path.read_text())["finalize"]
        expected_result = scoped_jobs_root / "worker-1/1-fixture-task/result.json"
        self.assertEqual(finalized["jobs_root"], str(scoped_jobs_root))
        self.assertEqual(finalized["result_json"], str(expected_result))

    def test_task_id_is_sanitized_only_for_output_paths(self) -> None:
        nested_task = self.dataset / "nested" / "task"
        nested_task.mkdir(parents=True)
        (nested_task / "task.md").write_text("nested task\n", encoding="utf-8")

        result = self._run_wrapper(
            {
                "RUN_ID": "path-safe-run",
                "TASK_ID": "nested/task",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        output_path = self.output_root / "path-safe-run"
        safe_artifact_dir = output_path / "mid-turn-fusion" / "nested_task"
        self.assertTrue((safe_artifact_dir / "fusion.json").is_file())
        self.assertTrue((output_path / "nested_task.fusion.json").is_file())
        self.assertFalse((output_path / "mid-turn-fusion/nested/task").exists())

        fusion = json.loads((safe_artifact_dir / "fusion.json").read_text())
        self.assertEqual(
            fusion["finalize"]["result_json"],
            str(
                output_path
                / "jobs/claude-code/model-fusion-nested_task"
                / "worker-1/1-nested_task/result.json"
            ),
        )


if __name__ == "__main__":
    unittest.main()
