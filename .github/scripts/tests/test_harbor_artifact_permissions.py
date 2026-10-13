import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_helpers import step_script, workflow_step, workflow_steps

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = (
    ROOT / ".github/workflows/harbor-e2e-validation.yml",
    ROOT / ".github/workflows/harbor-self-hosted-nightly.yml",
)
RECOVERY = "Repair stale Harbor artifact permissions"
REPAIR = "Repair Harbor artifact permissions"


class ArtifactPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.script = step_script(WORKFLOWS[0], RECOVERY)

    def run_repair(self, path, **environment):
        return subprocess.run(
            ["bash", "-c", self.script],
            cwd=path.parent,
            env={**os.environ, "ARTIFACT_DIR": str(path), **environment},
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_repairs_run_before_checkout_and_artifact_consumers(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                names = [step.get("name") for step in workflow_steps(path)]
                self.assertLess(names.index(RECOVERY), names.index("Check out repository"))
                recovery = workflow_step(path, RECOVERY)
                self.assertEqual(recovery["working-directory"], "${{ runner.temp }}")
                self.assertEqual(recovery["env"]["ARTIFACT_DIR"], "${{ github.workspace }}/runs")
                repair = workflow_step(path, REPAIR)
                self.assertIn("always()", repair["if"])
                self.assertEqual(repair["env"]["ARTIFACT_DIR"], "${{ steps.params.outputs.output_path }}")
                self.assertLess(names.index(REPAIR), names.index("Stage and redact artifacts"))
                self.assertIn(
                    "steps.permissions.outcome == 'success'",
                    workflow_step(path, "Stage and redact artifacts")["if"],
                )
                self.assertLess(names.index("Clean up"), names.index(REPAIR))

    def test_missing_output_is_a_noop_even_without_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_repair(Path(tmp) / "absent")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_repairs_private_files_for_staging_and_next_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            sessions = runs / "agent/sessions/tool-results"
            sessions.mkdir(parents=True)
            result_file = sessions / "output.txt"
            result_file.write_text("benchmark output")
            result_file.chmod(0)
            executable = sessions / "tool.sh"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o500)
            sessions.chmod(0o500)
            result = self.run_repair(runs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(result_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(sessions.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o700)
            shutil.copytree(runs, root / "staged")
            self.assertEqual(
                (root / "staged/agent/sessions/tool-results/output.txt").read_text(),
                "benchmark output",
            )
            # Exercise checkout's actual cleanup on a disposable repository.
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "clean", "-ffdx", "-q"], check=True
            )
            self.assertFalse(runs.exists())
            self.assertTrue((root / ".git").is_dir())

    def test_preserves_existing_group_and_world_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            artifact = runs / "summary.txt"
            artifact.touch(mode=0o644)
            result = self.run_repair(runs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o644)

    def test_does_not_follow_artifact_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            target = outside / "private.txt"
            target.write_text("leave alone")
            target.chmod(0o400)
            original = target.stat()
            runs = root / "runs"
            runs.mkdir()
            (runs / "file-link").symlink_to(target)
            (runs / "directory-link").symlink_to(outside, target_is_directory=True)
            (runs / "broken-link").symlink_to(root / "missing")
            result = self.run_repair(runs)
            self.assertEqual(result.returncode, 0, result.stderr)
            after = target.stat()
            self.assertEqual(
                (after.st_mode, after.st_uid, after.st_gid, after.st_ctime_ns),
                (original.st_mode, original.st_uid, original.st_gid, original.st_ctime_ns),
            )

    def test_rejects_a_symlinked_artifact_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "runs"
            link.symlink_to(root, target_is_directory=True)
            result = self.run_repair(link)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to repair a symlinked artifact directory", result.stderr)

    def test_escalates_only_ownership_repair_and_stops_if_sudo_is_unavailable(self):
        for sudo_status in (0, 1):
            with self.subTest(sudo_status=sudo_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runs = root / "runs with spaces"
                runs.mkdir()
                artifact = runs / "private.txt"
                artifact.touch()
                artifact.chmod(0o400)
                commands = root / "bin"
                commands.mkdir()
                (commands / "chown").write_text("#!/bin/bash\nset -euo pipefail\nexit 1\n")
                (commands / "sudo").write_text(
                    "#!/bin/bash\nset -euo pipefail\n"
                    'printf "%s\\n" "$@" > "$SUDO_LOG"\n'
                    f"exit {sudo_status}\n"
                )
                for command in commands.iterdir():
                    command.chmod(0o755)
                log = root / "sudo.log"
                result = self.run_repair(
                    runs, PATH=f"{commands}:{os.environ['PATH']}", SUDO_LOG=str(log)
                )
                self.assertEqual(result.returncode, sudo_status, result.stderr)
                self.assertEqual(
                    log.read_text().splitlines(),
                    ["-n", "chown", "-hRP", "--", f"{os.getuid()}:{os.getgid()}", str(runs)],
                )
                self.assertEqual(
                    stat.S_IMODE(artifact.stat().st_mode),
                    0o600 if sudo_status == 0 else 0o400,
                )


if __name__ == "__main__":
    unittest.main()
