import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_module(name: str):
    module_path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MonitorOpenClawSessionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module("monitor_openclaw_sessions")

    def test_summarize_fleet_counts_active_and_idle(self):
        with mock.patch.object(self.module.stream, "summarize_instance") as summarize_instance:
            summarize_instance.side_effect = [
                {
                    "state": "active",
                    "updated_at": "2026-04-22T10:00:00Z",
                    "instance_label": "openclaw-1",
                    "turn_count": 3,
                    "status": "active",
                    "session_id": "sess-1",
                    "latest_turns_summary": "active summary",
                },
                {
                    "state": "idle",
                    "updated_at": "-",
                    "instance_label": "openclaw-2",
                    "turn_count": 0,
                    "status": "idle",
                    "session_id": "-",
                    "latest_turns_summary": "No active multi-turn session.",
                },
            ]
            fleet = self.module.summarize_fleet(
                total_workers=2,
                base_port=18789,
                port_step=20,
                port_offset=0,
                config_base="/tmp/openclaw",
            )
        self.assertEqual(fleet["active_workers"], 1)
        self.assertEqual(fleet["idle_workers"], 1)
        self.assertEqual(fleet["active_instances"][0]["instance_label"], "openclaw-1")
        self.assertEqual(fleet["visible_instances"][0]["instance_label"], "openclaw-1")

    def test_render_fleet_summary_lists_active_session(self):
        rendered = self.module.render_fleet_summary(
            {
                "total_workers": 3,
                "active_workers": 1,
                "idle_workers": 2,
                "visible_instances": [
                    {
                        "instance_label": "openclaw-2",
                        "state": "recent",
                        "turn_count": 4,
                        "status": "active",
                        "session_id": "sess-2",
                        "latest_turns_summary": "user asked for help with deployment",
                    }
                ],
            },
            columns=70,
            lines=10,
        )
        self.assertIn("OpenClaw Session Monitor", rendered)
        self.assertIn("active:    1", rendered)
        self.assertIn("openclaw-2 state=recent turns=4", rendered)

    def test_summarize_fleet_passes_container_prefix(self):
        with mock.patch.object(self.module.stream, "summarize_instance") as summarize_instance:
            summarize_instance.side_effect = [
                {"state": "active", "updated_at": "2026-04-22T10:00:00Z", "instance_label": "openclaw-1"},
                {"state": "idle", "updated_at": "-", "instance_label": "openclaw-2"},
            ]
            fleet = self.module.summarize_fleet(
                total_workers=2,
                base_port=18789,
                port_step=20,
                port_offset=0,
                config_base="/tmp/openclaw",
                container_prefix="fleet",
            )
        self.assertEqual(fleet["active_workers"], 1)
        summarize_instance.assert_any_call(
            instance=1,
            port=18789,
            store_root=Path("/tmp/openclaw/1"),
            container_name="fleet-1",
        )


if __name__ == "__main__":
    unittest.main()
