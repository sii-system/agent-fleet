import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from write_harbor_registry_summary import reward_rollup  # noqa: E402


class RegistrySummaryTest(unittest.TestCase):
    def test_rolls_up_named_reward_from_multi_key_metrics(self) -> None:
        stats = {
            "evals": {
                "deepsearchqa": {
                    "n_trials": 2,
                    "metrics": [
                        {
                            "f1_score": 0.75,
                            "fully_correct": 0.5,
                            "reward": 0.75,
                        }
                    ],
                    "reward_stats": {"reward": {"0.5": ["a"], "1.0": ["b"]}},
                }
            }
        }

        mean_reward, reward_counts = reward_rollup(stats)

        self.assertEqual(mean_reward, "0.75")
        self.assertEqual(reward_counts, {"0.5": 1, "1.0": 1})

    def test_keeps_legacy_mean_metric_support(self) -> None:
        stats = {
            "evals": {
                "legacy": {
                    "n_trials": 1,
                    "metrics": [{"mean": 0.5}],
                    "reward_stats": {"reward": {"0.5": ["a"]}},
                }
            }
        }

        mean_reward, reward_counts = reward_rollup(stats)

        self.assertEqual(mean_reward, "0.5")
        self.assertEqual(reward_counts, {"0.5": 1})


if __name__ == "__main__":
    unittest.main()
