import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class SkillDocsTest(unittest.TestCase):
    def test_skills_have_valid_frontmatter(self):
        skills = list((ROOT / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for path in skills:
            with self.subTest(skill=path.parent.name):
                opening, frontmatter, body = path.read_text(encoding="utf-8").split("---", 2)
                self.assertEqual(opening, "")
                fields = yaml.safe_load(frontmatter)
                self.assertEqual(fields["name"], path.parent.name)
                self.assertIsInstance(fields["description"], str)
                self.assertTrue(fields["description"].strip())
                self.assertLessEqual(len(fields["description"]), 500)
                self.assertTrue(body.strip())


if __name__ == "__main__":
    unittest.main()
