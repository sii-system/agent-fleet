from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

HARBOR_DIR = Path(__file__).resolve().parents[1]
MODEL_FUSION_DIR = HARBOR_DIR / "model-fusion"
UTILS_PATH = MODEL_FUSION_DIR / "router_cli_utils.py"

spec = importlib.util.spec_from_file_location("router_cli_utils_test", UTILS_PATH)
assert spec is not None and spec.loader is not None
utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils)


class RouterSourceFingerprintTest(unittest.TestCase):
    def test_fingerprint_tracks_dirty_and_untracked_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            tracked = repo / "tracked.py"
            tracked.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.py"], check=True)
            baseline = utils.source_fingerprint(repo)

            tracked.write_text("value = 2\n", encoding="utf-8")
            dirty = utils.source_fingerprint(repo)
            self.assertNotEqual(dirty, baseline)

            untracked = repo / "new.py"
            untracked.write_text("new = True\n", encoding="utf-8")
            self.assertNotEqual(utils.source_fingerprint(repo), dirty)

    def test_ignored_build_outputs_do_not_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
            baseline = utils.source_fingerprint(repo)
            (repo / "dist").mkdir()
            (repo / "dist/router.whl").write_bytes(b"ignored")
            self.assertEqual(utils.source_fingerprint(repo), baseline)

    def test_fingerprint_accepts_a_git_worktree_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            worktree = root / "worktree"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-qb", "fixture", str(worktree)],
                check=True,
            )

            self.assertTrue((worktree / ".git").is_file())
            self.assertEqual(
                utils.source_fingerprint(worktree), utils.source_fingerprint(repo)
            )

    def test_fingerprint_streams_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            subprocess.run(["git", "-C", str(repo), "add", "large.bin"], check=True)

            with mock.patch.object(
                Path, "read_bytes", side_effect=AssertionError("whole-file read")
            ):
                self.assertEqual(len(utils.source_fingerprint(repo)), 64)

    def test_fingerprint_handles_populated_gitlinks_and_submodule_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "child"
            repo = root / "repo"
            subprocess.run(["git", "init", "-q", str(child)], check=True)
            (child / "module.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(child), "add", "module.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(child),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-C",
                    str(repo),
                    "submodule",
                    "add",
                    "-q",
                    str(child),
                    "vendor/child",
                ],
                check=True,
            )

            baseline = utils.source_fingerprint(repo)
            (repo / "vendor/child/module.py").write_text(
                "value = 2\n", encoding="utf-8"
            )
            self.assertNotEqual(utils.source_fingerprint(repo), baseline)


class RouterWheelBuildTest(unittest.TestCase):
    def test_build_is_cached_with_absolute_paths_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            bin_dir = root / "bin"
            cache = root / "relative-cache"
            repo.mkdir()
            bin_dir.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
            fake_uv = bin_dir / "uv"
            fake_uv.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  if [[ $1 == --out-dir ]]; then shift; out=$1; fi\n"
                "  shift\n"
                "done\n"
                "printf wheel > \"$out/sii_fusion_router-0.2.0-py3-none-any.whl\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            with mock.patch.dict(
                os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
            ):
                first = utils.build_wheel(repo, cache, "0.2.0")
                second = utils.build_wheel(repo, cache, "0.2.0")

            self.assertEqual(first, second)
            wheel = Path(first["wheel"])
            self.assertTrue(wheel.is_absolute())
            self.assertEqual(
                first["wheel_sha256"], hashlib.sha256(b"wheel").hexdigest()
            )
            self.assertEqual(
                wheel.with_suffix(".whl.sha256").read_text().strip(),
                first["wheel_sha256"],
            )


class ImmutableRouterConfigTest(unittest.TestCase):
    def test_config_path_is_content_addressed_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            source.write_text(
                '{"routing": {"strict_no_fallback": true}}\n', encoding="utf-8"
            )
            first = utils.derive_config(source, root / "out", "openrouter_fusion", 1)
            repeated = utils.derive_config(
                source, root / "out", "openrouter_fusion", 1
            )
            unlimited = utils.derive_config(
                source, root / "out", "openrouter_fusion", -1
            )

            self.assertEqual(first, repeated)
            self.assertNotEqual(first, unlimited)
            self.assertEqual(json.loads(first.read_text())["routing"]["max_fusions"], 1)
            self.assertEqual(first.stat().st_mode & 0o777, 0o444)

    def test_concurrent_publishers_never_observe_a_partial_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            source.write_text('{"routing": {"strict_no_fallback": true}}\n')
            with ThreadPoolExecutor(max_workers=16) as executor:
                targets = list(
                    executor.map(
                        lambda _: utils.derive_config(
                            source, root / "out", "mimo_max", -1
                        ),
                        range(64),
                    )
                )

            self.assertEqual(len(set(targets)), 1)
            payload = json.loads(targets[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["routing"]["max_fusions"], -1)

    def test_mimo_config_sets_all_model_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            source.write_text("{}\n", encoding="utf-8")
            target = utils.derive_config(source, root / "out", "mimo_max", -1)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["models"]["panels"], ["sonnet", "sonnet"])
            self.assertEqual(payload["mimo_max"]["selector_model"], "sonnet")


class WrapperInitializationOrderTest(unittest.TestCase):
    def test_claude_agent_is_forced_before_shared_env(self) -> None:
        for relative in ("mimo-code/run_tb21.sh", "openrouter/run_tb21.sh"):
            script = (MODEL_FUSION_DIR / relative).read_text(encoding="utf-8")
            with self.subTest(script=relative):
                self.assertLess(
                    script.index("AGENT=claude-code"),
                    script.index('. "$HARBOR_DIR/env.sh"'),
                )

    def test_launchers_support_worktrees_and_always_scope_registry_runs(self) -> None:
        for relative in ("mimo-code/run_tb21.sh", "openrouter/run_tb21.sh"):
            script = (MODEL_FUSION_DIR / relative).read_text(encoding="utf-8")
            with self.subTest(script=relative):
                self.assertIn("rev-parse --is-inside-work-tree", script)
                self.assertNotIn('[[ -d "$FUSION_ROUTER_DIR/.git" ]]', script)
                self.assertIn("harbor_terminalbench21_tasks.txt", script)
                self.assertIn('TB_INCLUDE_TASKS="$INCLUDE_TASKS"', script)
                self.assertIn("MODEL_FUSION_PROXY_RENDER_ONLY=1", script)


if __name__ == "__main__":
    unittest.main()
