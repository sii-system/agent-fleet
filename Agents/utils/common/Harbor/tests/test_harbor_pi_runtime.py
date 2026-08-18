"""Tests for the small shared Harbor Pi subprocess helper."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harbor_analyzer.pi import dispatch_to_child
from harbor_pi_runtime import load_final_json_from_event_stream, run_pi_json_process
from harbor_pi_runtime.process import models_config


def write_fixture_pi(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import sys",
                "if sys.argv[1:] == ['--version']:",
                "    print('fixture-pi 1.0')",
                "    raise SystemExit(0)",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read() if args[-1] == 'system' else args[-1]",
                "payload = {'ok': True, 'prompt': prompt}",
                "events = [",
                "    {'type': 'session', 'id': 'fixture-session'},",
                "    {'type': 'agent_start'},",
                "    {'type': 'turn_start'},",
                "    {'type': 'message_update', 'message': {'role': 'assistant', 'content': 'partial'}},",
                "    {'type': 'message_end', 'message': {'role': 'assistant', 'content': json.dumps(payload), 'stopReason': 'stop'}},",
                "    {'type': 'turn_end'},",
                "    {'type': 'agent_end'},",
                "]",
                "for event in events:",
                "    print(json.dumps(event), flush=True)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def write_custom_pi(path: Path, body: str) -> Path:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import sys",
                "if sys.argv[1:] == ['--version']:",
                "    print('fixture-pi 1.0')",
                "    raise SystemExit(0)",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class HarborPiRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.pi_bin = write_fixture_pi(self.root / "fixture-pi")

    def run_runtime(self, prompt: str, **overrides: object):
        options = {
            "prompt": prompt,
            "events_path": self.root / "events.jsonl",
            "stderr_path": self.root / "stderr.txt",
            "runtime_home": self.root / "home",
            "runtime_workdir": self.root / "work",
            "pi_bin": str(self.pi_bin),
            "provider": "fixture",
            "model": "fixture-model",
            "base_url": "https://example.test/v1",
            "api_key_env": "FIXTURE_API_KEY",
            "agent_name": "fixture-agent",
            "display_name": "Fixture",
            "timeout_seconds": 5,
            "launch_mode": "fixture",
            "system_prompt": "system",
        }
        options.update(overrides)
        with mock.patch.dict(os.environ, {"FIXTURE_API_KEY": "fake"}, clear=True):
            return run_pi_json_process(**options)

    def test_models_config_preserves_explicit_and_omitted_auth_header(self) -> None:
        explicit = models_config(
            provider="analyzer",
            model="fixture",
            base_url="https://example.test/v1",
            api_key_env="FIXTURE_API_KEY",
            display_name="Analyzer",
            auth_header=True,
        )
        omitted = models_config(
            provider="fixer",
            model="fixture",
            base_url="https://example.test/v1",
            api_key_env="FIXTURE_API_KEY",
            display_name="Fixer",
        )

        self.assertTrue(explicit["providers"]["analyzer"]["authHeader"])
        self.assertNotIn("authHeader", omitted["providers"]["fixer"])

    def test_compact_events_and_stdin_prompt(self) -> None:
        result = self.run_runtime(
            "stdin-prompt",
            base_url="https://example.test",
            prompt_in_stdin=True,
            no_tools=True,
            thinking_level="off",
        )

        self.assertIsNone(result.block_reason)
        self.assertEqual(result.output_json, {"ok": True, "prompt": "stdin-prompt"})
        self.assertEqual(result.provenance["thinking_level"], "off")
        self.assertEqual(result.provenance["discarded_event_counts"], {"message_update": 1})
        self.assertNotIn(
            "message_update",
            (self.root / "events.jsonl").read_text(encoding="utf-8"),
        )

    def test_process_record_identifies_the_pi_process(self) -> None:
        process_record = self.root / "active-process.json"

        result = self.run_runtime("fixture", process_record_path=process_record)

        record = json.loads(process_record.read_text(encoding="utf-8"))
        self.assertIsNone(result.block_reason)
        self.assertEqual(record["status"], "running")
        self.assertGreater(record["pid"], 0)
        self.assertGreater(record["start_ticks"], 0)

    def test_rejects_streaming_stdin_combination(self) -> None:
        result = self.run_runtime(
            "fixture",
            prompt_in_stdin=True,
            stream_compaction=True,
        )

        self.assertEqual(result.block_reason, "pi_streaming_stdin_unsupported")
        self.assertFalse((self.root / "events.jsonl").exists())

    def test_empty_failed_final_message_does_not_reuse_earlier_json(self) -> None:
        self.pi_bin = write_custom_pi(
            self.root / "multi-turn-pi",
            "\n".join(
                [
                    (
                        "first = {'role': 'assistant', 'content': "
                        "json.dumps({'stale': True}), 'stopReason': 'stop'}"
                    ),
                    "final = {'role': 'assistant', 'content': [], 'stopReason': 'length'}",
                    "events = [",
                    "    {'type': 'session', 'id': 'fixture-session'},",
                    "    {'type': 'agent_start'},",
                    "    {'type': 'turn_start'},",
                    "    {'type': 'message_end', 'message': first},",
                    "    {'type': 'turn_end', 'message': first},",
                    "    {'type': 'turn_start'},",
                    "    {'type': 'message_end', 'message': final},",
                    "    {'type': 'turn_end', 'message': final},",
                    "    {'type': 'agent_end'},",
                    "]",
                    "for event in events:",
                    "    print(json.dumps(event), flush=True)",
                ]
            ),
        )

        result = self.run_runtime("fixture")

        self.assertIsNone(result.output_json)
        self.assertEqual(result.output_text, "")
        self.assertEqual(result.block_reason, "pi_final_message_truncated")

    def test_event_stream_loader_treats_non_utf8_as_invalid(self) -> None:
        events_path = self.root / "undecodable.jsonl"
        events_path.write_bytes(b"\xff\n")

        self.assertIsNone(load_final_json_from_event_stream(events_path))

    def test_runtime_provenance_overrides_conflicting_caller_metadata(self) -> None:
        result = self.run_runtime(
            "fixture",
            provenance={
                "provider": "spoofed",
                "events_path": "/tmp/spoofed.jsonl",
                "independent_pi_process": False,
                "caller_fact": "preserved",
            },
        )

        self.assertEqual(result.provenance["provider"], "fixture")
        self.assertEqual(result.provenance["events_path"], str(self.root / "events.jsonl"))
        self.assertTrue(result.provenance["independent_pi_process"])
        self.assertEqual(result.provenance["caller_fact"], "preserved")

    def test_compaction_failure_preserves_raw_events(self) -> None:
        with mock.patch(
            "harbor_pi_runtime.process.compact_jsonl_event_stream",
            side_effect=OSError("fixture compaction failure"),
        ):
            result = self.run_runtime("fixture")

        raw_path = self.root / ".events.jsonl.raw"
        self.assertIn("pi_event_compaction_error", result.block_reason or "")
        self.assertEqual(result.provenance["raw_events_path"], str(raw_path))
        self.assertTrue(raw_path.is_file())
        self.assertIn("message_update", raw_path.read_text(encoding="utf-8"))

    def test_analyzer_adapter_streams_compact_events_and_domain_provenance(self) -> None:
        evidence = self.root / "evidence.txt"
        evidence.write_text("fixture\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HARBOR_ANALYZER_API_KEY": "fake"}, clear=True):
            result = dispatch_to_child(
                prompt="analyzer-prompt",
                analysis_id="analysis-1",
                output_dir=self.root / "out",
                pi_bin=str(self.pi_bin),
                provider="harbor-analyzer",
                model="fixture-model",
                base_url="https://example.test/v1",
                api_key_env="HARBOR_ANALYZER_API_KEY",
                agent_name="harbor_analyzer_pi_subagent",
                timeout_seconds=5,
                allowed_paths=[evidence],
            )

        self.assertIsNone(result.block_reason)
        self.assertEqual(result.report, {"ok": True, "prompt": "analyzer-prompt"})
        self.assertNotIn("thinking_level", result.provenance)
        self.assertEqual(result.provenance["tools_allowlist"], ["read", "grep", "find", "ls"])
        self.assertIn(str(evidence.resolve()), result.provenance["allowed_paths"])
        self.assertEqual(result.provenance["message_updates_dropped"], 1)
        events_path = self.root / "out" / "analyzer-subagent-events" / "analysis-1.jsonl"
        self.assertNotIn("message_update", events_path.read_text(encoding="utf-8"))

    def test_analyzer_adapter_maps_shared_configuration_errors(self) -> None:
        common = {
            "prompt": "fixture",
            "analysis_id": "analysis-1",
            "output_dir": self.root / "out",
            "pi_bin": sys.executable,
            "provider": "harbor-analyzer",
            "model": "fixture-model",
            "agent_name": "harbor_analyzer_pi_subagent",
            "timeout_seconds": 5,
        }
        invalid_env = dispatch_to_child(
            **common,
            base_url="https://example.test/v1",
            api_key_env="INVALID-NAME",
        )
        with mock.patch.dict(os.environ, {"FIXTURE_API_KEY": "fake"}, clear=True):
            invalid_url = dispatch_to_child(
                **common,
                base_url="not-a-url",
                api_key_env="FIXTURE_API_KEY",
            )

        self.assertEqual(invalid_env.block_reason, "analyzer_api_key_env_invalid")
        self.assertEqual(invalid_url.block_reason, "analyzer_base_url_invalid")


if __name__ == "__main__":
    unittest.main()
