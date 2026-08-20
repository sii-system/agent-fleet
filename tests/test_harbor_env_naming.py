import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PATTERNS = (
    re.compile(r"\b" + "T" + r"B_[A-Z0-9_]+\b"),
    re.compile(r"\b" + "t" + r"b-(?:run|task|trial):"),
)


class HarborEnvironmentNamingTest(unittest.TestCase):
    def test_tracked_files_do_not_use_legacy_terminalbench_names(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        relative_paths = {Path(raw_path.decode()) for raw_path in tracked if raw_path}
        relative_paths.add(Path(__file__).resolve().relative_to(REPO_ROOT))
        violations: list[str] = []

        for relative in sorted(relative_paths):
            path = REPO_ROOT / relative
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if any(pattern.search(line) for pattern in LEGACY_PATTERNS):
                    violations.append(f"{relative}:{line_number}: {line.strip()}")

        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
