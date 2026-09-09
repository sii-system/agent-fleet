import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


class HarborSummaryDisplayTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("e2e_harbor_summary", SCRIPTS / "e2e_harbor_summary.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_generated_fixer_link_becomes_downloadable_artifact_path(self):
        report = (SCRIPTS / "tests/fixtures/harbor-joint-summary.md").read_text()
        link = "[fix-report-latest.md](fixer/fix-report-latest.md)"
        expected = "`fixer/fix-report-latest.md` ([download run artifacts](https://example.test/artifact))"
        self.assertEqual(self.module.render_summary(report, "https://example.test/artifact"), report.replace(link, expected))

    def test_missing_upload_has_path_without_download_link(self):
        report = "Full Fixer report: [fix-report-latest.md](fixer/fix-report-latest.md)\n"
        rendered = self.module.render_summary(report, "")
        self.assertEqual(rendered, "Full Fixer report: `fixer/fix-report-latest.md` (artifact upload unavailable)\n")

    def test_preserves_external_links_and_recorded_code_blocks(self):
        line = "Full Fixer report: [fix-report-latest.md](fixer/fix-report-latest.md)\n"
        for fence in ("```", "~~~~"):
            report = fence + "text\n" + line + fence + "\n"
            self.assertEqual(self.module.render_summary(report, "https://example.test/artifact"), report)
        external = "Full Fixer report: [fix-report-latest.md](https://example.test/report.md)\n"
        self.assertEqual(self.module.render_summary(external, "https://example.test/artifact"), external)

    def test_report_without_fixer_is_unchanged(self):
        report = (SCRIPTS / "tests/fixtures/harbor-joint-summary.md").read_text().split("\n## Fixer Results\n")[0] + "\n"
        self.assertEqual(self.module.render_summary(report, "https://example.test/artifact"), report)


if __name__ == "__main__":
    unittest.main()
