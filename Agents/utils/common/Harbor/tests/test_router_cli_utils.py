from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
