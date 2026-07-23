import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = {
    "harbor-benchmark-runner": {
        "description": "Use when configuring, launching, monitoring, or debugging Harbor benchmark runs for Claude Code or OpenCode in this repository.",
        "paths": [
            "Agents/utils/common/Harbor/start.sh",
            "Agents/utils/common/Harbor/env.sh",
            "Tasks/",
        ],
    },
    "openclaw-fleet-operations": {
        "description": "Use when generating, scaling, operating, or debugging the Dockerized OpenClaw gateway fleet in this repository.",
        "paths": [
            "Agents/Openclaw/scripts/setup.sh",
            "Agents/Openclaw/scripts/openclaw-fleet.sh",
            "Agents/Openclaw/docker-compose.yml",
        ],
    },
    "openclaw-benchmark-runners": {
        "description": "Use when running PinchBench or ClawBio benchmarks against this repository's OpenClaw gateway fleet.",
        "paths": [
            "Tasks/Pinchbench/scripts/run-parallel-workers.py",
            "Tasks/clawBio/scripts/run-openclaw-clawbio.sh",
            "Agents/Openclaw/scripts/setup.sh",
        ],
    },
}


def parse_frontmatter(text):
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if not match:
        raise AssertionError("missing YAML frontmatter")

    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


class SkillDocsTest(unittest.TestCase):
    def test_expected_skills_have_valid_frontmatter_and_repo_paths(self):
        for skill_name, expected in EXPECTED_SKILLS.items():
            with self.subTest(skill=skill_name):
                skill_file = ROOT / "skills" / skill_name / "SKILL.md"
                text = skill_file.read_text(encoding="utf-8")
                fields = parse_frontmatter(text)

                self.assertEqual(fields, {"name": skill_name, "description": expected["description"]})
                self.assertLessEqual(len(fields["description"]), 500)
                self.assertTrue(fields["description"].startswith("Use when "))

                body = text.split("---\n", 2)[2]
                self.assertIn("## Workflow", body)
                self.assertIn("## Output Contract", body)
                for path in expected["paths"]:
                    self.assertIn(path, body)

    def test_readme_links_skills_guide_and_lists_all_skills(self):
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        skills_readme = (ROOT / "skills" / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("## Core Skills", root_readme)
        self.assertIn("- Skills: [skills/README.md](./skills/README.md)", root_readme)
        self.assertIn("## Install Skills", skills_readme)
        for skill_name in EXPECTED_SKILLS:
            with self.subTest(skill=skill_name):
                self.assertIn(skill_name, skills_readme)

    def test_root_readme_advertises_supported_runner_agents(self):
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agent_row = re.search(
            r"^\|\s*`--agent`\s*\|\s*`-a`\s*\|\s*(.+?)\s*\|$",
            root_readme,
            re.MULTILINE,
        )

        self.assertIsNotNone(agent_row)
        self.assertEqual(
            set(re.findall(r"`([^`]+)`", agent_row.group(1))),
            {"claude-code", "opencode", "openclaw"},
        )
        self.assertNotIn("Terminus-2", root_readme)

    def test_prompt_templates_read_local_config_and_use_expected_skills(self):
        prompt_expectations = {
            "e2e-harbor-benchmark.txt": [
                "config.local.env",
                "skills/harbor-benchmark-runner",
                "TOTAL_WORKERS=3",
                "TB_N_CONCURRENT=3",
            ],
            "e2e-openclaw-benchmark.txt": [
                "config.local.env",
                "skills/openclaw-fleet-operations",
                "skills/openclaw-benchmark-runners",
                "PinchBench",
                "ClawBio",
            ],
        }

        for prompt_name, expected_text in prompt_expectations.items():
            with self.subTest(prompt=prompt_name):
                prompt_file = ROOT / "skills" / prompt_name
                text = prompt_file.read_text(encoding="utf-8")
                for expected in expected_text:
                    self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
