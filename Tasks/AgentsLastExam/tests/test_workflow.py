"""Shared benchmark workflow: explicit setup, selection and Harbor handoff."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ale_adapter.launch import command, main, parser
from ale_adapter.source import REVISION
from ale_adapter.workflow import digest, environment_marker, select_tasks


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dataset = self.root / "tasks"
        self.definitions = []
        for domain, task, variant, os_type in (
            ("visual_media", "example", 0, "windows"),
            ("visual_media", "example", 1, "windows"),
            ("computing_math", "linux", 0, "linux"),
        ):
            path = self.dataset / f"{domain}--{task}--v{variant}" / "environment/ale.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"task": f"tasks/{domain}/{task}", "variant": variant,
                "os": os_type, "requires_gpu": False, "revision": REVISION, "source_sha256": "fixture"}))
            self.definitions.append(path)
        (self.dataset / "dataset.json").write_text(json.dumps({"scope": "cpu", "revision": REVISION,
            "source_sha256": "fixture", "tasks": 3}))
        self.images = self.root / "images.json"
        self.images.write_text("{}")

    def test_full_domain_task_variant_and_os_selection(self):
        self.assertEqual(select_tasks(self.definitions), self.definitions)
        self.assertEqual(select_tasks(self.definitions, domains=["visual_media"]), self.definitions[:2])
        self.assertEqual(select_tasks(self.definitions, task_ids=["visual_media/example"]), self.definitions[:2])
        self.assertEqual(select_tasks(self.definitions, task_ids=["visual_media--example--v1"]), self.definitions[1:2])
        self.assertEqual(select_tasks(self.definitions, task_ids=["example,computing_math/linux"]), self.definitions)
        self.assertEqual(select_tasks(self.definitions, os_type="linux"), self.definitions[2:])
        for filters in ({"domains": ["missing"]}, {"task_ids": ["missing"]},
                        {"task_ids": ["example"], "os_type": "linux"}):
            with self.assertRaises(ValueError):
                select_tasks(self.definitions, **filters)

    def arguments(self, *args):
        return parser().parse_args(["--dataset", str(self.dataset), "--source", str(self.root),
            "--native-python", sys.executable, "--image-map", str(self.images), *args])

    def test_harbor_handoff_has_workers_model_output_selection_and_custom_agent(self):
        args = self.arguments("--task", "example", "--workers", "4", "--model", "test-model",
                              "--output", str(self.root / "job"), "--agent", "custom:Agent",
                              "--", "--max-retries", "2")
        args.selected = self.definitions[:2]
        with patch.dict(os.environ, {"OPIK_URL": ""}):
            cmd = command(args)
        for flag, value in (("--n-concurrent", "4"), ("--model", "test-model"),
                            ("--job-name", "job"), ("--agent", "custom:Agent")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertEqual(cmd[-2:], ["--max-retries", "2"])
        self.assertEqual(cmd.count("--include-task-name"), 2)

    def launch(self, args, *, prepared=False):
        marker = {"source": str(self.root), "dataset": str(self.dataset), "native_python": sys.executable}
        argv = ["launch", "--cache", str(self.root), "--image-map", str(self.images)] if prepared else ["launch"] + [
            "--dataset", str(self.dataset), "--source", str(self.root), "--native-python", sys.executable,
            "--image-map", str(self.images)]
        with patch.object(sys, "argv", argv + args), \
                patch("ale_adapter.launch.validate_source", return_value=self.root), \
                patch("ale_adapter.launch.source_digest", return_value="fixture"), \
                patch("ale_adapter.launch.environment_marker", return_value=marker), \
                patch("ale_adapter.launch.select_image") as image, \
                patch("ale_adapter.launch.os.execvpe") as execute:
            main()
        return image, execute

    def test_prepared_launch_uses_defaults_and_only_requires_selected_images(self):
        image, execute = self.launch(["--all", "--os", "linux"], prepared=True)
        self.assertEqual(image.call_count, 1)
        self.assertEqual(image.call_args.args[0]["os"], "linux")
        cmd = execute.call_args.args[1]
        self.assertIn("Agents.AgentsLastExam.agent:ALECommandAgent", cmd)
        self.assertEqual(cmd[cmd.index("--include-task-name") + 1], "computing_math--linux--v0")

    def test_dry_run_does_not_exec_or_print_forwarded_secrets(self):
        import io
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            _, execute = self.launch(["--all", "--dry-run", "--", "--ae", "KEY=fake-secret"], prepared=True)
        execute.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["tasks"], 3)
        self.assertEqual(result["os"], {"linux": 1, "windows": 2})
        self.assertNotIn("fake-secret", output.getvalue())

    def test_gpu_or_incomplete_dataset_fails_before_harbor(self):
        record = json.loads(self.definitions[0].read_text())
        self.definitions[0].write_text(json.dumps({**record, "requires_gpu": True}))
        with self.assertRaises(SystemExit):
            self.launch(["--all"])
        self.definitions[0].unlink()
        with self.assertRaises(SystemExit):
            self.launch(["--all"])

    def test_environment_marker_rejects_host_and_native_dependency_changes(self):
        lock = self.root / "uv.lock"
        lock.write_text("generated lock")
        marker = {"version": 1, "lock_sha256": digest(lock), "packages": {"fake-host": "1"},
                  "native_python": sys.executable, "native_packages": {"fake-native": "2"}}
        (self.root / ".agent-fleet-ale.json").write_text(json.dumps(marker))
        with patch("importlib.metadata.version", return_value="1"), \
                patch("ale_adapter.workflow.native_packages", return_value={"fake-native": "2"}):
            self.assertEqual(environment_marker(self.root, lock), marker)
        with patch("importlib.metadata.version", return_value="changed"), self.assertRaisesRegex(ValueError, "host packages"):
            environment_marker(self.root, lock)
        with patch("importlib.metadata.version", return_value="1"), \
                patch("ale_adapter.workflow.native_packages", return_value={}), self.assertRaisesRegex(ValueError, "native packages"):
            environment_marker(self.root, lock)

    def test_setup_prepares_dataset_and_records_paths_but_reuses_matching_dataset(self):
        from ale_adapter.prepare import main as prepare
        source = self.root / "source"
        source.mkdir()
        env = self.root / "native-env"
        env.mkdir()
        lock = self.root / "uv.lock"
        lock.write_text("generated lock")
        with patch.object(sys, "argv", ["prepare", "--cache", str(self.root), "--lockfile", str(lock)]), \
                patch("ale_adapter.prepare.validate_source", return_value=source), \
                patch("ale_adapter.prepare.source_digest", return_value="fixture"), \
                patch("ale_adapter.prepare.subprocess.run") as run, \
                patch("ale_adapter.prepare.native_packages", return_value={"fake-native": "2"}), \
                patch.object(sys, "prefix", str(self.root)):
            prepare()
        self.assertEqual(run.call_count, 1)
        self.assertIn("--torch-backend", run.call_args.args[0])
        marker = json.loads((self.root / ".agent-fleet-ale.json").read_text())
        self.assertEqual(marker["dataset"], str(self.dataset))
        self.assertEqual(marker["native_python"], str(env / "bin/python"))
        fresh = self.root / "new-tasks"
        with patch.object(sys, "argv", ["prepare", "--cache", str(self.root), "--lockfile", str(lock),
                                      "--dataset", str(fresh)]), \
                patch("ale_adapter.prepare.validate_source", return_value=source), \
                patch("ale_adapter.prepare.source_digest", return_value="fixture"), \
                patch("ale_adapter.prepare.subprocess.run") as run, \
                patch("ale_adapter.prepare.native_packages", return_value={}), \
                patch.object(sys, "prefix", str(self.root)):
            prepare()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][:3], [str(env / "bin/python"), "-m", "ale_adapter.adapter"])
        self.assertEqual(run.call_args.args[0][-1], str(fresh))

    def test_run_help_and_missing_environment_do_not_install_dependencies(self):
        run = Path(__file__).resolve().parents[1] / "run.sh"
        environment = {**os.environ, "HARBOR_ALE_ENV_DIR": str(self.root / "missing")}
        help_result = subprocess.run(["bash", str(run), "--help"], env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--all", help_result.stdout)
        missing = subprocess.run(["bash", str(run), "--all"], env=environment, capture_output=True, text=True, check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("setup.sh", missing.stderr)
