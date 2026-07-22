from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from string import Template
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[6]
WORKSPACE = ROOT.parent
HARBOR = ROOT / "Agents" / "utils" / "common" / "Harbor"
MODEL_FUSION = HARBOR / "model-fusion"
SITE_CUSTOMIZE = ROOT / "Agents" / "Harbor-claude-code" / "sitecustomize.py"
ROUTER = WORKSPACE / "sii-fusion-router"
BUILDER = (
    ROUTER
    / "src/sii_fusion_router/frontends/claude_code/task_subagent_prompt.py"
)


def load_sitecustomize():
    spec = importlib.util.spec_from_file_location("fleet_sitecustomize_test", SITE_CUSTOMIZE)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load sitecustomize")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MidTurnWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sitecustomize = load_sitecustomize()

    def test_agents_flag_is_injected_once(self):
        command = "claude --verbose --output-format=stream-json --print -- task"
        env = {"TB_CLAUDE_CODE_AGENTS_JSON": '{"span-panel-0":{}}'}
        once = self.sitecustomize._inject_claude_agents_flag(command, env)
        twice = self.sitecustomize._inject_claude_agents_flag(once, env)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(" --agents "), 1)
        self.assertLess(once.index(" --agents "), once.index(" --print --"))

    def test_opik_and_round_gate_hooks_are_composed(self):
        payload = json.loads(
            self.sitecustomize._build_hook_settings_json(
                "/opt/tb-opik/hook.py",
                opik_enabled=True,
                round_gate_enabled=True,
                round_gate_path="/opt/tb-fusion-round/subagent_barrier_gate.py",
                round_gate_mode="mid-turn-fusion",
            )
        )
        self.assertEqual(len(payload["hooks"]["PreToolUse"]), 2)
        self.assertEqual(len(payload["hooks"]["Stop"]), 2)
        commands = [
            hook["command"]
            for group in payload["hooks"]["PreToolUse"]
            for hook in group["hooks"]
        ]
        self.assertTrue(any("/opt/tb-opik/hook.py" in command for command in commands))
        self.assertTrue(
            any("subagent_barrier_gate.py" in command for command in commands)
        )
        self.assertTrue(any("mid-turn-fusion" in command for command in commands))

    def test_hook_settings_cover_independent_enablement(self):
        opik_only = json.loads(
            self.sitecustomize._build_hook_settings_json(
                "/opt/tb-opik/hook.py",
                opik_enabled=True,
                round_gate_enabled=False,
            )
        )
        self.assertNotIn("PreToolUse", opik_only["hooks"])
        self.assertEqual(len(opik_only["hooks"]["Stop"]), 1)

        gate_only = json.loads(
            self.sitecustomize._build_hook_settings_json(
                "/opt/tb-opik/hook.py",
                opik_enabled=False,
                round_gate_enabled=True,
            )
        )
        self.assertEqual(set(gate_only["hooks"]), {"PreToolUse", "Stop"})
        self.assertEqual(len(gate_only["hooks"]["PreToolUse"]), 1)
        self.assertEqual(len(gate_only["hooks"]["Stop"]), 1)

        disabled = json.loads(
            self.sitecustomize._build_hook_settings_json(
                "/opt/tb-opik/hook.py",
                opik_enabled=False,
                round_gate_enabled=False,
            )
        )
        self.assertEqual(disabled["hooks"], {})

    def test_round_gate_mounts_extend_existing_mounts(self):
        helper = MODEL_FUSION / "harbor_worker_utils.py"
        result = subprocess.run(
            [
                sys.executable,
                str(helper),
                "append-readonly-mounts",
                '[{"type":"bind","source":"base","target":"/base","read_only":true}]',
                "--mount",
                "/router/frontend",
                "/opt/tb-fusion-round",
                "--mount",
                "/tasks/task.md",
                "/opt/tb-fusion-task/task.md",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = json.loads(result.stdout)
        self.assertEqual(len(mounts), 3)
        self.assertEqual(mounts[1]["source"], "/router/frontend")
        self.assertEqual(mounts[1]["target"], "/opt/tb-fusion-round")
        self.assertTrue(mounts[1]["read_only"])
        self.assertEqual(mounts[2]["target"], "/opt/tb-fusion-task/task.md")

    def test_cross_repo_paths_and_mode_are_locked(self):
        env_text = (HARBOR / "env.sh").read_text(encoding="utf-8")
        fusion_env = (MODEL_FUSION / "env.sh").read_text(encoding="utf-8")
        wrapper = (MODEL_FUSION / "run_one_tb21_task.sh").read_text(
            encoding="utf-8"
        )
        harboropik = (HARBOR / "harboropik.sh").read_text(encoding="utf-8")
        fusion_harboropik = (MODEL_FUSION / "harboropik.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$MODEL_FUSION_DIR/env.sh"', env_text)
        self.assertIn("frontends/claude_code", fusion_env)
        self.assertNotIn("engine/pipelines/mid_turn_fusion", fusion_env)
        self.assertNotIn("FUSION_ROUTER_ENTRY", fusion_env)
        self.assertNotIn("FUSION_HARBOR_ADAPTER", fusion_env)
        self.assertIn("task_subagent_prompt.py", wrapper)
        self.assertIn("prompts/mid_turn_fusion/panel.md", wrapper)
        self.assertIn("prompts/mid_turn_fusion/outer.md", wrapper)
        self.assertNotIn("FUSION_ENABLED", wrapper)
        self.assertIn("/opt/tb-fusion-round/subagent_barrier_gate.py", wrapper)
        self.assertIn("mid-turn-fusion", fusion_env)
        self.assertIn("SPAN_MID_TURN_ARTIFACT_ROOT", fusion_harboropik)
        self.assertIn("SPAN_HOOK_REASON_MAX_BYTES", fusion_env)
        self.assertIn("SPAN_HOOK_REASON_MAX_BYTES", fusion_harboropik)
        self.assertIn("model_fusion_build_agent_env_args", harboropik)
        self.assertIn('--outer-model "$SPAN_OUTER_MODEL"', wrapper)
        self.assertNotIn("--judge-model", wrapper)
        self.assertFalse(
            (
                ROUTER
                / "src/sii_fusion_router/engine/pipelines/mid_turn_fusion"
            ).exists()
        )
        self.assertTrue((ROUTER / "prompts/mid_turn_fusion/panel.md").is_file())

    def test_claude_prompt_transport_is_file_backed(self):
        source = SITE_CUSTOMIZE.read_text(encoding="utf-8")
        self.assertIn("--append-system-prompt-file", source)
        self.assertIn('run_flags.pop("append_system_prompt", _MISSING)', source)
        self.assertIn("_remove_remote_append_system_prompt", source)
        self.assertNotIn("def _fix_unquoted_append_system_prompt", source)

    def test_shared_harbor_files_are_thin_hooks(self):
        env_text = (HARBOR / "env.sh").read_text(encoding="utf-8")
        worker = (HARBOR / "run_harbor_worker.sh").read_text(encoding="utf-8")
        harboropik = (HARBOR / "harboropik.sh").read_text(encoding="utf-8")
        helper = (HARBOR / "harbor_worker_utils.py").read_text(encoding="utf-8")

        self.assertIn('"$MODEL_FUSION_DIR/env.sh"', env_text)
        self.assertNotIn("FUSION_ROUTER_ENTRY=", env_text)
        self.assertNotIn("MODEL_FUSION_DIR", worker)
        self.assertNotIn("FUSION_ENABLED", worker)
        self.assertIn('"$MODEL_FUSION_DIR/harboropik.sh"', harboropik)
        self.assertNotIn("SPAN_MID_TURN_ARTIFACT_ROOT=", harboropik)
        self.assertNotIn("latest-outer-result", helper)
        self.assertNotIn("append-readonly-mounts", helper)

    def test_fleet_contains_no_fusion_core_copy(self):
        self.assertFalse((MODEL_FUSION / "subagent_barrier_gate.py").exists())
        self.assertFalse((MODEL_FUSION / "task_subagent_prompt.py").exists())
        wrapper = (MODEL_FUSION / "run_one_tb21_task.sh").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "span_" + "llm_adapter",
            "span_" + "harbor_subagent_router",
            "make_" + "span_steps",
            "fusion_" + "harbor_attempt",
            "panel_" + "run.sh",
            "outer_" + "run.sh",
        ):
            self.assertNotIn(forbidden, wrapper)

    def test_only_original_worker_path_exists(self):
        worker = (HARBOR / "run_harbor_worker.sh").read_text(encoding="utf-8")
        self.assertNotIn("FUSION_ENABLED", worker)
        self.assertNotIn("model_fusion_run_task", worker)
        self.assertFalse((MODEL_FUSION / "worker.sh").exists())
        self.assertIn(
            'run_claimed_task "$task_name" "$task_jobs_root" "$task_index"',
            worker,
        )

    def _wrapper_env(self, dataset: Path, output: Path, router: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "TASK_ID": "fixture-task",
                "DATASET_PATH": str(dataset),
                "OUTPUT_ROOT": str(output),
                "RUN_ID": "wiring-test",
                "FUSION_ROUTER_DIR": str(router),
                "MID_TURN_PREPARE_ONLY": "1",
                "MODEL": "main-model",
                "SPAN_PANEL_MODELS": "panel-a,panel-b",
                "SPAN_OUTER_MODEL": "outer-model",
            }
        )
        return env

    def test_missing_router_checkout_fails_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "dataset" / "fixture-task"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("fixture\n", encoding="utf-8")
            missing = root / "missing-router"
            result = subprocess.run(
                ["bash", str(MODEL_FUSION / "run_one_tb21_task.sh")],
                env=self._wrapper_env(root / "dataset", root / "output", missing),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(missing), result.stderr)
        self.assertIn("FUSION_ROUTER_DIR", result.stderr)

    def test_prepare_uses_router_builder_and_writes_contract(self):
        self.assertTrue(ROUTER.is_dir(), f"sibling Router checkout missing: {ROUTER}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "dataset" / "fixture-task"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("fixture task\n", encoding="utf-8")
            output = root / "output"
            result = subprocess.run(
                ["bash", str(MODEL_FUSION / "run_one_tb21_task.sh")],
                env=self._wrapper_env(root / "dataset", output, ROUTER),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = output / "wiring-test" / "mid-turn-fusion" / "fixture-task"
            agents = json.loads((artifact / "claude-agents.json").read_text())
            fusion = json.loads((artifact / "fusion.json").read_text())
            prompt = (artifact / "task-subagent-system-prompt.md").read_text()
            canonical_panel = (
                ROUTER / "prompts/mid_turn_fusion/panel.md"
            ).read_text(encoding="utf-8")
            canonical_outer = (
                ROUTER / "prompts/mid_turn_fusion/outer.md"
            ).read_text(encoding="utf-8")
            trial = root / "trial"
            agent_log = trial / "agent/claude-code.txt"
            agent_log.parent.mkdir(parents=True)
            agent_log.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "outer",
                                        "name": "Agent",
                                        "input": {"subagent_type": "span-outer"},
                                    }
                                ]
                            },
                        },
                        {
                            "type": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "outer",
                                        "content": "MID_TURN_MERGE_RESULT:\n"
                                        + json.dumps(
                                            {
                                                "status": "merged",
                                                "base_candidate": "span-panel-0",
                                                "grafts": [],
                                                "merged_patch": "",
                                                "rationale": "fixture merge",
                                                "checks_passed": True,
                                                "checks": [],
                                                "unverified": [],
                                                "risks": [],
                                            }
                                        ),
                                    }
                                ]
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            result_json = trial / "result.json"
            result_json.write_text(
                json.dumps({"trial_uri": f"file://{trial}"}), encoding="utf-8"
            )
            finalized = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "finalize",
                    "--task-id",
                    "fixture-task",
                    "--fusion-json",
                    str(artifact / "fusion.json"),
                    "--jobs-root",
                    str(root),
                    "--result-json",
                    str(result_json),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            fusion = json.loads((artifact / "fusion.json").read_text())
        self.assertEqual(set(agents), {"span-panel-0", "span-panel-1", "span-outer"})
        self.assertEqual(fusion["mode"], "mid_turn_fusion")
        self.assertEqual(fusion["config"]["outer_model"], "outer-model")
        self.assertIn("MID_TURN_OUTER_CONTEXT", fusion["config"]["outer_transport"])
        self.assertEqual(
            fusion["planned_fusion"]["outer_subagent"]["subagent_type"],
            "span-outer",
        )
        self.assertEqual(
            fusion["fusion_calls"][0]["outer_subagent"]["subagent_type"],
            "span-outer",
        )
        self.assertIn("MID_TURN_OUTER_CONTEXT", agents["span-outer"]["prompt"])
        self.assertEqual(
            agents["span-panel-0"]["prompt"],
            Template(canonical_panel)
            .substitute(panel_name="span-panel-0", model_label="panel-a")
            .strip(),
        )
        self.assertEqual(
            agents["span-outer"]["prompt"],
            Template(canonical_outer).substitute(model_label="outer-model").strip(),
        )
        self.assertIn("SPAN_MID_TURN_BOUNDARY_PANEL_REQUIRED", prompt)
        serialized = json.dumps(
            {"agents": agents, "fusion": fusion, "prompt": prompt},
            ensure_ascii=True,
        )
        self.assertNotIn("judge", serialized.lower())

    def test_wrapper_has_no_embedded_python(self):
        wrapper = (MODEL_FUSION / "run_one_tb21_task.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(wrapper, r"python3?\s+-\s+<<|python3?\s+-c\s")


if __name__ == "__main__":
    unittest.main()
