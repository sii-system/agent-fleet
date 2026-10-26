import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

OPENCLAW_DIR = Path(__file__).resolve().parents[1]


class OpenClawFleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name).resolve()
        self.repo = self.tmp_path / "fleet repo"
        self.project = self.repo / "Agents" / "Openclaw"
        shutil.copytree(OPENCLAW_DIR / "scripts", self.project / "scripts")
        shutil.copytree(OPENCLAW_DIR / "config", self.project / "config")
        self.bin_dir = self.tmp_path / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp_path / "docker.jsonl"
        docker = self.bin_dir / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args == ['info']:\n"
            "    sys.exit(0)\n"
            "if args[0] != 'compose':\n"
            "    sys.exit('unexpected Docker command: ' + repr(args))\n"
            "args.pop(0)\n"
            "files = {'-f': Path('docker-compose.yml'), '--env-file': Path('.env')}\n"
            "while args[0] in files:\n"
            "    option = args.pop(0)\n"
            "    files[option] = Path(args.pop(0))\n"
            "record = {'args': args, 'cwd': os.getcwd()}\n"
            "record.update({key: str(path.resolve()) for key, path in files.items()})\n"
            "with open(os.environ['DOCKER_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(record) + '\\n')\n"
            "if not all(path.is_file() for path in files.values()):\n"
            "    sys.exit('Compose file or token environment not found')\n"
            "if '${TOKEN_1}' not in files['-f'].read_text():\n"
            "    sys.exit('wrong Compose file')\n"
            "if 'TOKEN_1=' not in files['--env-file'].read_text():\n"
            "    sys.exit('missing gateway token')\n"
            "if args[0] == os.environ.get('DOCKER_FAIL_COMMAND'):\n"
            "    print('Docker Compose failed: ' + args[0], file=sys.stderr)\n"
            "    sys.exit(23)\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp_path / "home"),
            "PYTHON": sys.executable,
            "DOCKER_LOG": str(self.log),
            "BASE_URL": "https://api.example.invalid/v1",
            "API_KEY": "sk-fake-fleet-test",
            "CONFIG_BASE": str(self.tmp_path / "config"),
            "WORKSPACE_BASE": str(self.tmp_path / "workspace"),
            "OPENCLAW_CONFIG_DEFAULT_ACL": "false",
            "OPENCLAW_WORKSPACE_DEFAULT_ACL": "false",
        }
        subprocess.run(
            [str(self.project / "scripts" / "setup.sh"), "2"],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.unrelated = self.tmp_path / "unrelated"
        self.unrelated.mkdir()

    def run_fleet(self, *args, cwd=None, fail_command=""):
        return subprocess.run(
            [str(self.project / "scripts" / "openclaw-fleet.sh"), *args],
            cwd=cwd or self.repo,
            env={**self.env, "DOCKER_FAIL_COMMAND": fail_command},
            check=False,
            capture_output=True,
            text=True,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_generated_files(self, call, cwd):
        self.assertEqual(call["-f"], str(self.project / "docker-compose.yml"))
        self.assertEqual(call["--env-file"], str(self.project / ".env"))
        self.assertEqual(call["cwd"], str(cwd))

    def test_compose_commands_use_generated_files_from_repo_root(self):
        for command in ("start", "stop", "restart", "logs"):
            with self.subTest(command=command):
                options = ["--tail", "7"] if command == "logs" else []
                result = self.run_fleet(command, "2", *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                call = self.calls()[-1]
                self.assert_generated_files(call, self.repo)
                self.assertEqual(call["args"], [command, "openclaw-2", *options])

    def test_compose_paths_resolve_from_unrelated_directory(self):
        result = self.run_fleet("start", "1", cwd=self.unrelated)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_generated_files(self.calls()[0], self.unrelated)

    def test_scale_regenerates_and_reuses_generated_files(self):
        result = self.run_fleet("scale", "3", cwd=self.unrelated)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call["args"] for call in calls], [["down"], ["up", "-d"]])
        for call in calls:
            self.assert_generated_files(call, self.unrelated)
        self.assertIn("TOKEN_3=", (self.project / ".env").read_text())
        self.assertIn("openclaw-3:", (self.project / "docker-compose.yml").read_text())

    def test_lifecycle_failures_reach_the_caller(self):
        for command in ("start", "stop", "restart"):
            with self.subTest(command=command):
                self.log.write_text("")
                result = self.run_fleet(
                    command, "1,2", cwd=self.project, fail_command=command,
                )
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn(f"Docker Compose failed: {command}", result.stderr)
                self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
