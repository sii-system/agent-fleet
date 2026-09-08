import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.verifier_runtime import (
    swe_rebench_v2_bundle_preparer,
)


class SweRebenchV2BundlePreparerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.python_runtime = self.root / "python3.12-runtime.tar.gz"
        with tarfile.open(self.python_runtime, "w:gz") as archive:
            for name in (
                "python3.12-runtime/bin/python3.12",
                "python3.12-runtime/bin/python3.12.real",
            ):
                payload = b"#!/bin/sh\nexit 0\n"
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            stdlib = tarfile.TarInfo("python3.12-runtime/lib/python3.12")
            stdlib.type = tarfile.DIRTYPE
            archive.addfile(stdlib)
            marker_payload = b"target-system-libraries\n"
            marker = tarfile.TarInfo("python3.12-runtime/.harbor-python-runtime-v2")
            marker.size = len(marker_payload)
            archive.addfile(marker, io.BytesIO(marker_payload))

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_rebench_bundle_around_runtime_primitive(self):
        output = self.root / "bundle.tar.gz"

        swe_rebench_v2_bundle_preparer.build(self.python_runtime, output)

        self.assertTrue(swe_rebench_v2_bundle_preparer.archive_ready(output))
        root = swe_rebench_v2_bundle_preparer.BUNDLE_ID
        with tarfile.open(output) as archive:
            members = {member.name.rstrip("/"): member for member in archive}
        self.assertIn(f"{root}/bin/python3.12.real", members)
        self.assertEqual(members[f"{root}/bin/python3"].linkname, "python3.12")
        self.assertEqual(members[f"{root}/bin/python"].linkname, "python3.12")
        self.assertTrue(
            members[f"{root}/bin/harbor-verifier-bundle-check"].mode & 0o111
        )
        self.assertIn(f"{root}/.harbor-python-runtime-v2", members)

    def test_preparer_owns_runtime_source_selection(self):
        cache_dir = self.root / "cache"
        cache_dir.mkdir()
        cached_runtime = cache_dir / "python3.12-runtime.tar.gz"
        self.python_runtime.replace(cached_runtime)
        output = self.root / "bundle.tar.gz"

        swe_rebench_v2_bundle_preparer.prepare(cache_dir, output)

        self.assertTrue(swe_rebench_v2_bundle_preparer.archive_ready(output))

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
HARBOR_CC_PY_WHEEL_DIR_SOURCE="$4"
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

    def test_rejects_invalid_runtime_primitive(self):
        invalid = self.root / "invalid.tar.gz"
        invalid.write_text("not an archive", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "invalid Python runtime archive"):
            swe_rebench_v2_bundle_preparer.build(
                invalid, self.root / "bundle.tar.gz"
            )


if __name__ == "__main__":
    unittest.main()
