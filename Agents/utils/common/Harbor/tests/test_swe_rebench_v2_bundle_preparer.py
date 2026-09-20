import io
import json
import os
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.verifier_runtime import (
    python_runtime,
    swe_rebench_v2_bundle_preparer,
)


class SweRebenchV2BundlePreparerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.python_runtime = self.root / "python3.12-runtime.tar.gz"
        with tarfile.open(self.python_runtime, "w:gz") as archive:
            wrapper = python_runtime.STATIC_WRAPPER.encode()
            wrapper_info = tarfile.TarInfo("python3.12-runtime/bin/python3.12")
            wrapper_info.mode = 0o755
            wrapper_info.size = len(wrapper)
            archive.addfile(wrapper_info, io.BytesIO(wrapper))

            elf = bytearray(64 + 56)
            elf[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<H", elf, 18, 62)
            struct.pack_into("<Q", elf, 32, 64)
            struct.pack_into("<HH", elf, 54, 56, 1)
            real_info = tarfile.TarInfo("python3.12-runtime/bin/python3.12.real")
            real_info.mode = 0o755
            real_info.size = len(elf)
            archive.addfile(real_info, io.BytesIO(elf))

            for relative in (
                "encodings/__init__.py",
                "json/__init__.py",
                "xml/etree/ElementTree.py",
            ):
                name = f"python3.12-runtime/lib/python3.12/{relative}"
                payload = b"# fixture\n"
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            manifest = json.dumps(python_runtime.static_manifest()).encode() + b"\n"
            manifest_info = tarfile.TarInfo("python3.12-runtime/static-runtime.json")
            manifest_info.size = len(manifest)
            archive.addfile(manifest_info, io.BytesIO(manifest))

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_rebench_bundle_around_runtime_primitive(self):
        output = self.root / "bundle.tar.gz"

        swe_rebench_v2_bundle_preparer.build(self.python_runtime, output)

        self.assertTrue(swe_rebench_v2_bundle_preparer.archive_ready(output))
        root = swe_rebench_v2_bundle_preparer.BUNDLE_ID
        with tarfile.open(output) as archive:
            members = {member.name.rstrip("/"): member for member in archive}
            self.assertEqual(
                archive.extractfile(f"{root}/bin/self_check.py").read(),
                swe_rebench_v2_bundle_preparer.SELF_CHECK_PY_SOURCE.read_bytes(),
            )
        self.assertIn(f"{root}/bin/python3.12.real", members)
        self.assertEqual(members[f"{root}/bin/python3"].linkname, "python3.12")
        self.assertEqual(members[f"{root}/bin/python"].linkname, "python3.12")
        self.assertTrue(
            members[f"{root}/bin/harbor-verifier-bundle-check"].mode & 0o111
        )
        self.assertIn(f"{root}/static-runtime.json", members)

    def test_preparer_owns_runtime_source_selection(self):
        cache_dir = self.root / "cache"
        cache_dir.mkdir()
        cached_runtime = cache_dir / "python3.12-static-runtime.tar.gz"
        self.python_runtime.replace(cached_runtime)
        output = self.root / "bundle.tar.gz"

        swe_rebench_v2_bundle_preparer.prepare(cache_dir, output)

        self.assertTrue(swe_rebench_v2_bundle_preparer.archive_ready(output))

    def test_standalone_self_check_with_real_parser(self):
        repository = Path(__file__).resolve().parents[5]
        parser = repository / (
            "Tasks/SWE-rebench-v2/src/swe_rebench_v2/task-template/tests/parser.py"
        )
        completed = subprocess.run(
            [sys.executable, str(swe_rebench_v2_bundle_preparer.SELF_CHECK_PY_SOURCE),
             str(parser)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("parser_fixture=passed", completed.stdout)

    def test_shell_builder_uses_configured_runner_python(self):
        env_sh = Path(__file__).parents[1] / "env" / "dependencies.sh"
        runner_python = self.root / "runner-python"
        invocation_log = self.root / "runner-invocation.txt"
        preparer = self.root / "preparer.py"
        preparer.touch()
        runner_python.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$PYTHON_BIN" "$@" > "$INVOCATION_LOG"\n'
            "exit 1\n",
            encoding="utf-8",
        )
        runner_python.chmod(0o755)
        script = r'''
set -euo pipefail
eval "$(sed -n '/^harbor_build_verifier_runtime_bundle()/,/^}/p' "$1")"
verifier_runtime_bundle_required() { return 0; }
validate_verifier_runtime_bundle_transport() { return 0; }
verifier_runtime_bundle_ready() { return 1; }
HARBOR_OPIK_PYTHON="$2"
VERIFIER_RUNTIME_BUNDLE_PREPARER="$3"
VERIFIER_RUNTIME_BUNDLE_ID=test-bundle
VERIFIER_RUNTIME_BUNDLE_CACHE_DIR="$4"
VERIFIER_RUNTIME_BUNDLE_ARCHIVE_SOURCE="$4/bundle.tar.gz"
if harbor_build_verifier_runtime_bundle; then
  exit 90
fi
'''
        environment = os.environ.copy()
        environment["INVOCATION_LOG"] = str(invocation_log)

        completed = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "bash",
                str(env_sh),
                str(runner_python),
                str(preparer),
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        invocation = invocation_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(invocation[0], str(runner_python))
        self.assertEqual(invocation[1:3], [str(preparer), "build"])
        self.assertNotIn("python3.12", invocation)

    def test_remote_agent_cache_remains_eligible_with_verifier_bundle(self):
        env_sh = Path(__file__).parents[1] / "env" / "dependencies.sh"
        event_log = self.root / "events.txt"
        runtime_dir = self.root / "runtime"
        script = r'''
set -euo pipefail
eval "$(sed -n '/^harbor_pick_remote_wheel_url()/,/^}/p' "$1")"
eval "$(sed -n '/^harbor_prepare_or_select_wheels()/,/^}/p' "$1")"
verifier_runtime_bundle_required() { return 0; }
harbor_agent_is_pi() { return 1; }
harbor_agent_is_claude_code() { return 1; }
harbor_agent_is_opencode() { return 0; }
harbor_agent_tgz_basename() { printf '%s\n' "$OPENCODE_TGZ_BASENAME"; }
harbor_manifest_url_ready() { return 0; }
harbor_url_is_reachable() { return 0; }
validate_verifier_runtime_bundle_transport() { return 0; }
harbor_local_cache_ready() { return 1; }
harbor_prepare_web_mcp() { return 0; }
harbor_build_verifier_runtime_bundle() { printf '%s\n' bundle >> "$EVENT_LOG"; }
harbor_write_effective_wheel_source() { printf 'remote=%s\n' "$1" >> "$EVENT_LOG"; }
harbor_mark_workers_ready() { printf '%s\n' ready >> "$EVENT_LOG"; }
harbor_ensure_local_wheels_server() { return 90; }
harbor_prewarm_s3_upload_cache() { return 91; }
HARBOR_ENVIRONMENT_TYPE=docker
YICLOUD_SANDBOX_UPLOAD_BACKEND=auto
HARBOR_REMOTE_WHEEL_SERVER_URLS=https://cache.example
HARBOR_LOCAL_WHEEL_SERVER_URL=
OPENCODE_TGZ_BASENAME=opencode-ai-test.tgz
OPENCODE_LINUX_X64_TGZ_BASENAME=opencode-linux-x64-test.tgz
RUNTIME_DIR="$2"
WORKERS_READY_FILE="$2/workers.ready"
WORKERS_FAILED_FILE="$2/workers.failed"
EFFECTIVE_WHEEL_URL_FILE="$2/effective-wheel-url"
EFFECTIVE_CLAUDE_TGZ_URL_FILE="$2/effective-claude-tgz-url"
HARBOR_RUNNER_PREPARE_STATUS_FILE="$2/runner-prepare.status"
LOCAL_DEPS_LOG_FILE="$2/local-deps.log"
EVENT_LOG="$3"
harbor_prepare_or_select_wheels
'''

        completed = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "bash",
                str(env_sh),
                str(runtime_dir),
                str(event_log),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            event_log.read_text(encoding="utf-8").splitlines(),
            ["bundle", "remote=https://cache.example", "ready"],
        )
        self.assertEqual(
            (runtime_dir / "local-deps-prepare.status").read_text(
                encoding="utf-8"
            ).strip(),
            "remote",
        )

    def test_rejects_invalid_runtime_primitive(self):
        invalid = self.root / "invalid.tar.gz"
        invalid.write_text("not an archive", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "invalid Python runtime archive"):
            swe_rebench_v2_bundle_preparer.build(
                invalid, self.root / "bundle.tar.gz"
            )


if __name__ == "__main__":
    unittest.main()
