import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "harbor-self-hosted-nightly.yml"
BENCHMARKS = ROOT / ".github" / "harbor-nightly-benchmarks.txt"
SELECTOR = ROOT / ".github" / "scripts" / "harbor_nightly_select.py"

SPEC = importlib.util.spec_from_file_location("harbor_nightly_select", SELECTOR)
assert SPEC and SPEC.loader
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)


class FakeTaskId:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class FakeClient:
    async def get_dataset_metadata(self, _benchmark):
        return SimpleNamespace(
            task_ids=[FakeTaskId("publisher/task-a"), FakeTaskId("publisher/task-b")]
        )


class FakeRandom:
    def sample(self, values, count):
        self.count = count
        return values[:count]


class SelectorTest(unittest.TestCase):
    def test_reads_local_task_lists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_list = root / selector.LOCAL_TASK_LISTS["seta"]
            task_list.parent.mkdir(parents=True)
            task_list.write_text("# comment\ntask-a\ntask-b\ntask-a\n", encoding="utf-8")
            self.assertEqual(selector.task_names(root, "seta"), ["task-a", "task-b"])

    def test_reads_harbor_machine_metadata(self):
        self.assertEqual(
            selector.registry_task_names("publisher/dataset", FakeClient),
            ["publisher/task-a", "publisher/task-b"],
        )

    def test_selects_twenty_unique_tasks(self):
        available = [f"task-{index}" for index in range(20)]
        randomizer = FakeRandom()
        with mock.patch.object(selector, "task_names", return_value=available):
            selected = selector.select_tasks(Path("."), "seta", randomizer)
        self.assertEqual(len(selected), 20)
        self.assertEqual(len(set(selected)), 20)
        self.assertEqual(randomizer.count, 20)

    def test_rejects_benchmarks_with_fewer_than_twenty_tasks(self):
        available = [f"task-{index}" for index in range(19)]
        with (
            mock.patch.object(selector, "task_names", return_value=available),
            self.assertRaisesRegex(RuntimeError, "need 20"),
        ):
            selector.select_tasks(Path("."), "seta", FakeRandom())


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.benchmarks = [
            line.strip()
            for line in BENCHMARKS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_targets_self_hosted_runner_on_a_schedule(self):
        self.assertIn("runs-on: [self-hosted, Linux, X64, bare-metal]", self.workflow)
        self.assertIn('- cron: "47 */4 * * *"', self.workflow)
        self.assertNotIn("pull_request", self.workflow)

    def test_catalog_excludes_terminal_bench_and_adds_harbor_hub(self):
        self.assertFalse(any("terminal-bench" in item.lower() for item in self.benchmarks))
        self.assertGreaterEqual(sum("/" in item for item in self.benchmarks), 10)
        self.assertIn("tmax/TMax-15K-Harbor", self.benchmarks)

    def test_randomizes_benchmark_and_selects_twenty_tasks(self):
        self.assertIn("shuf -n 1", self.workflow)
        self.assertIn("SAMPLE_SIZE = 20", SELECTOR.read_text())
        self.assertIn("harbor_nightly_select.py", self.workflow)

    def test_passes_the_sample_to_harbor(self):
        self.assertIn("INCLUDE_TASKS: ${{ steps.params.outputs.tasks }}", self.workflow)
        self.assertIn("HARBOR_INCLUDE_TASKS: ${{ steps.params.outputs.tasks }}", self.workflow)
        self.assertIn('scripts/run_fleet.sh --taskset "$BENCHMARK"', self.workflow)
        self.assertIn("Verify sampled tasks completed", self.workflow)
        self.assertIn("--expected-trials", self.workflow)

    def test_pins_environment_values_that_can_change_the_run(self):
        self.assertIn(
            "ANTHROPIC_AUTH_TOKEN: ${{ secrets.LLM_REVIEW_API_KEY }}", self.workflow
        )
        self.assertIn(
            "HARBOR_ANTHROPIC_AUTH_TOKEN: ${{ secrets.LLM_REVIEW_API_KEY }}",
            self.workflow,
        )
        self.assertIn('HARBOR_LIMIT: ""', self.workflow)
        self.assertIn('MIN_TEST: "0"', self.workflow)

    def test_smith_rejects_excess_harness_failures(self):
        self.assertIn('failed_count="$(grep -c . "$queue_dir/failed.txt"', self.workflow)
        self.assertIn("$4 != \"\" { count++ }", self.workflow)
        self.assertIn("errors * 100 > EXPECTED_TASKS * 10", self.workflow)

    def test_uses_least_privilege_and_pinned_checkout(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertRegex(self.workflow, r"actions/checkout@[0-9a-f]{40} # v")
        self.assertIn("persist-credentials: false", self.workflow)

    def test_cleans_up_without_pruning_the_shared_daemon(self):
        self.assertIn("zellij kill-session", self.workflow)
        self.assertNotIn("docker system prune", self.workflow)
        self.assertNotIn("docker image prune", self.workflow)


if __name__ == "__main__":
    unittest.main()
