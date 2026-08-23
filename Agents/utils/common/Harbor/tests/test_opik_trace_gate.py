from __future__ import annotations

import unittest

from Agents.utils.common.Harbor.opik_trace_gate import (
    _is_true,
    opik_tracing_enabled,
)


class OpikTraceGateTests(unittest.TestCase):
    def test_is_true_uses_one_boolean_truth_table(self) -> None:
        for value in ("1", "true", "TRUE", " yes ", "On"):
            with self.subTest(value=value):
                self.assertTrue(_is_true(value))
        for value in (None, "", "0", "false", "no", "off", "unexpected"):
            with self.subTest(value=value):
                self.assertFalse(_is_true(value))

    def test_url_enables_tracing_unless_disable_is_truthy(self) -> None:
        endpoint = "https://opik.example.invalid/api"
        self.assertFalse(opik_tracing_enabled(environ={}))
        self.assertTrue(opik_tracing_enabled(environ={"OPIK_URL": endpoint}))
        for value in ("1", "true", "TRUE", " yes ", "On"):
            with self.subTest(value=value):
                self.assertFalse(
                    opik_tracing_enabled(
                        environ={
                            "OPIK_URL": endpoint,
                            "OPIK_TRACK_DISABLE": value,
                        }
                    )
                )

    def test_process_disable_cannot_be_overridden_by_agent_environment(self) -> None:
        endpoint = "https://opik.example.invalid/api"
        self.assertFalse(
            opik_tracing_enabled(
                {"OPIK_URL": endpoint, "OPIK_TRACK_DISABLE": "false"},
                environ={"OPIK_TRACK_DISABLE": "true"},
            )
        )


if __name__ == "__main__":
    unittest.main()
