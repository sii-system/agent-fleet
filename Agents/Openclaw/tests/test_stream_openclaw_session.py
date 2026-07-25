import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "stream_openclaw_session.py"
    )
    spec = importlib.util.spec_from_file_location("stream_openclaw_session", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_fixture(name: str):
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))

class StreamOpenClawSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_selects_latest_active_multi_turn_session(self):
        summary = self.module.summarize_sessions_payload(
            load_fixture("sessions_active.json"), instance=2, port=18809
        )
        self.assertEqual(summary["state"], "active")
        self.assertEqual(summary["session_id"], "sess-active-newest")
        self.assertEqual(summary["turn_count"], 4)
        self.assertIn("latest turn", summary["latest_turns_summary"].lower())

    def test_reports_idle_when_no_active_multi_turn_session(self):
        summary = self.module.summarize_sessions_payload(
            load_fixture("sessions_idle.json"), instance=1, port=18789
        )
        self.assertEqual(summary["state"], "recent")
        self.assertEqual(summary["instance_label"], "openclaw-1")

    def test_enriches_runtime_payload_with_session_events(self):
        event_lookup = {
            "fd22e4bb-d830-4478-8400-fa3ce3e54c5d": self.module.parse_session_events(
                (Path(__file__).parent / "fixtures" / "session_runtime.jsonl").read_text(encoding="utf-8")
            )
        }
        summary = self.module.summarize_sessions_payload(
            load_fixture("sessions_runtime.json"),
            instance=1,
            port=18789,
            event_lookup=event_lookup,
        )
        self.assertEqual(summary["state"], "idle")
        self.assertEqual(summary["turn_count"], 0)
        self.assertIn("no active non-heartbeat", summary["latest_turns_summary"].lower())

    def test_prefers_non_heartbeat_session_when_newer_heartbeat_exists(self):
        payload = {
            "sessions": [
                {
                    "sessionId": "heartbeat-session",
                    "updatedAt": 1776169727705,
                    "kind": "direct",
                },
                {
                    "sessionId": "real-session",
                    "updatedAt": 1776169700000,
                    "kind": "direct",
                    "turnCount": 2,
                    "latestTurnsSummary": "real user session",
                    "status": "active",
                },
            ]
        }
        event_lookup = {
            "heartbeat-session": self.module.parse_session_events(
                (Path(__file__).parent / "fixtures" / "session_runtime.jsonl").read_text(encoding="utf-8")
            )
        }
        summary = self.module.summarize_sessions_payload(
            payload,
            instance=1,
            port=18789,
            event_lookup=event_lookup,
        )
        self.assertEqual(summary["state"], "active")
        self.assertEqual(summary["session_id"], "real-session")
        self.assertIn("real user session", summary["latest_turns_summary"])

    def test_loads_sessions_from_store_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "agents" / "main" / "sessions"
            session_dir.mkdir(parents=True)
            session_file = session_dir / "real-session.jsonl"
            session_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message",
                                "timestamp": "2026-04-22T05:20:00Z",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "hello"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "message",
                                "timestamp": "2026-04-22T05:20:10Z",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "world"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "message",
                                "timestamp": "2026-04-22T05:21:00Z",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "follow up"}],
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            (session_dir / "sessions.json").write_text(
                json.dumps(
                    {
                        "agent:main:main": {
                            "sessionId": "real-session",
                            "updatedAt": 1776835260000,
                            "lastTo": "+123",
                            "sessionFile": "/home/node/.openclaw/agents/main/sessions/real-session.jsonl",
                        }
                    }
                ),
                encoding="utf-8",
            )
            payload, event_lookup = self.module.load_sessions_from_store_root(root)
            summary = self.module.summarize_sessions_payload(
                payload,
                instance=1,
                port=18789,
                event_lookup=event_lookup,
            )
            self.assertEqual(summary["state"], "active")
            self.assertEqual(summary["session_id"], "real-session")
            self.assertEqual(summary["turn_count"], 2)
            self.assertIn("follow up", summary["latest_turns_summary"])

    def test_summarize_instance_falls_back_to_container_when_store_root_unreadable(self):
        sessions_payload = {
            "sessions": [
                {
                    "sessionId": "sess-1",
                    "agentId": "main",
                    "updatedAt": 1776835260000,
                    "status": "active",
                    "kind": "direct",
                }
            ]
        }

        def fake_run(cmd, check, capture_output, text):
            joined = " ".join(cmd)
            if "find /home/node/.openclaw/agents -maxdepth 3 -path '*/sessions/sessions.json' | sort" in joined:
                return mock.Mock(
                    returncode=0,
                    stdout="/home/node/.openclaw/agents/main/sessions/sessions.json\n",
                    stderr="",
                )
            if "cat /home/node/.openclaw/agents/main/sessions/sessions.json" in joined:
                return mock.Mock(returncode=0, stdout=json.dumps(sessions_payload), stderr="")
            if "tail -n 200 /home/node/.openclaw/agents/main/sessions/sess-1.jsonl" in joined:
                return mock.Mock(
                    returncode=0,
                    stdout=(
                        '{"type":"message","timestamp":"2026-04-22T10:00:00Z","message":{"role":"user",'
                        '"content":[{"type":"text","text":"hello"}]}}\n'
                        '{"type":"message","timestamp":"2026-04-22T10:00:10Z","message":{"role":"assistant",'
                        '"content":[{"type":"text","text":"world"}]}}\n'
                        '{"type":"message","timestamp":"2026-04-22T10:01:00Z","message":{"role":"user",'
                        '"content":[{"type":"text","text":"follow up"}]}}\n'
                    ),
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(self.module.subprocess, "run", side_effect=fake_run):
            summary = self.module.summarize_instance(
                instance=1,
                port=18789,
                store_root="/unreadable",
                container_name="openclaw-1",
            )
        self.assertEqual(summary["state"], "active")
        self.assertEqual(summary["session_id"], "sess-1")
        self.assertEqual(summary["turn_count"], 2)

    def test_summarize_instance_prefers_readable_store_root_over_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "agents" / "main" / "sessions"
            session_dir.mkdir(parents=True)
            (session_dir / "sess-1.jsonl").write_text(
                "\n".join(
                    [
                        (
                            '{"type":"message","timestamp":"2026-04-22T10:00:00Z","message":{"role":"user",'
                            '"content":[{"type":"text","text":"hello"}]}}'
                        ),
                        (
                            '{"type":"message","timestamp":"2026-04-22T10:00:10Z","message":{"role":"assistant",'
                            '"content":[{"type":"text","text":"world"}]}}'
                        ),
                        (
                            '{"type":"message","timestamp":"2026-04-22T10:01:00Z","message":{"role":"user",'
                            '"content":[{"type":"text","text":"store root wins"}]}}'
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            (session_dir / "sessions.json").write_text(
                json.dumps(
                    {
                        "agent:main:main": {
                            "sessionId": "sess-1",
                            "status": "active",
                            "sessionFile": "/home/node/.openclaw/agents/main/sessions/sess-1.jsonl",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(self.module.subprocess, "run") as run:
                summary = self.module.summarize_instance(
                    instance=1,
                    port=18789,
                    store_root=root,
                    container_name="openclaw-1",
                )

            run.assert_not_called()
            self.assertEqual(summary["state"], "active")
            self.assertEqual(summary["session_id"], "sess-1")
            self.assertIn("store root wins", summary["latest_turns_summary"])

    def test_load_sessions_from_container_reads_full_sessions_json(self):
        def fake_run(cmd, check, capture_output, text):
            joined = " ".join(cmd)
            if "find /home/node/.openclaw/agents -maxdepth 3 -path '*/sessions/sessions.json' | sort" in joined:
                return mock.Mock(
                    returncode=0,
                    stdout="/home/node/.openclaw/agents/main/sessions/sessions.json\n",
                    stderr="",
                )
            if "cat /home/node/.openclaw/agents/main/sessions/sessions.json" in joined:
                return mock.Mock(
                    returncode=0,
                    stdout='{\n  "agent:main:main": {\n    "sessionId": "sess-1",\n    "updatedAt": 1776835260000,\n    "status": "active"\n  }\n}\n',
                    stderr="",
                )
            if "tail -n 200 /home/node/.openclaw/agents/main/sessions/sess-1.jsonl" in joined:
                return mock.Mock(
                    returncode=0,
                    stdout=(
                        '{"type":"message","timestamp":"2026-04-22T10:00:00Z","message":{"role":"user",'
                        '"content":[{"type":"text","text":"hello"}]}}\n'
                        '{"type":"message","timestamp":"2026-04-22T10:00:10Z","message":{"role":"assistant",'
                        '"content":[{"type":"text","text":"world"}]}}\n'
                    ),
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(self.module.subprocess, "run", side_effect=fake_run):
            payload, event_lookup = self.module.load_sessions_from_container("openclaw-1")
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertIn("sess-1", event_lookup)

    def test_selects_recent_single_turn_non_heartbeat_session(self):
        payload = {
            "sessions": [
                {
                    "sessionId": "heartbeat-session",
                    "updatedAt": 1776169727705,
                    "kind": "direct",
                },
                {
                    "sessionId": "recent-single",
                    "updatedAt": 1776835260000,
                    "kind": "direct",
                },
            ]
        }
        event_lookup = {
            "heartbeat-session": {
                "turn_count": 1,
                "latest_turns_summary": "Read HEARTBEAT.md if it exists",
                "updated_at": "2026-04-22T10:00:00Z",
                "is_heartbeat": True,
            },
            "recent-single": {
                "turn_count": 1,
                "latest_turns_summary": "User: run the benchmark now",
                "updated_at": "2026-04-22T10:01:00Z",
                "is_heartbeat": False,
            },
        }
        summary = self.module.summarize_sessions_payload(
            payload,
            instance=1,
            port=18789,
            event_lookup=event_lookup,
        )
        self.assertEqual(summary["state"], "recent")
        self.assertEqual(summary["session_id"], "recent-single")

    def test_selects_running_zero_turn_session(self):
        payload = {
            "sessions": [
                {
                    "sessionId": "task_blog_1776852827699",
                    "updatedAt": 1776853088623,
                    "status": "running",
                    "agentId": "bench-glm-5-1-fp8",
                }
            ]
        }
        summary = self.module.summarize_sessions_payload(
            payload,
            instance=2,
            port=18809,
            event_lookup={},
        )
        self.assertEqual(summary["state"], "active")
        self.assertEqual(summary["session_id"], "task_blog_1776852827699")
        self.assertEqual(summary["turn_count"], 0)
        self.assertIn("running", summary["latest_turns_summary"].lower())


if __name__ == "__main__":
    unittest.main()
