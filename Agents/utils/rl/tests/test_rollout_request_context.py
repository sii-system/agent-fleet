"""Tests for per-request rollout context propagation."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "rollout_remote_harbor.py"
SPEC = importlib.util.spec_from_file_location("rollout_remote_harbor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RolloutRequestContextTest(unittest.TestCase):
    def _handler(self, body: bytes):
        handler = object.__new__(MODULE.Handler)
        handler.path = "/run_trial"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._send_json = mock.Mock()
        return handler

    def test_invalid_json_bodies_return_bad_request(self) -> None:
        invalid_bodies = (
            (b"[1, 2, 3]", "TypeError"),
            (b"{", "JSONDecodeError"),
        )
        for body, exception_type in invalid_bodies:
            with self.subTest(body=body):
                handler = self._handler(body)
                with mock.patch.object(MODULE, "_append_trace"):
                    handler.do_POST()

                status, payload = handler._send_json.call_args.args
                self.assertEqual(status, MODULE.HTTPStatus.BAD_REQUEST)
                self.assertEqual(
                    payload["detail"]["exception_type"],
                    exception_type,
                )

    def test_invalid_timeout_is_rejected_before_enqueue(self) -> None:
        body = json.dumps({"request_timeout": "not-a-number"}).encode("utf-8")
        handler = self._handler(body)
        enqueue = mock.Mock(return_value=("request-1", Path("unused-result.json")))

        with (
            mock.patch.object(MODULE, "_enqueue_request", enqueue),
            mock.patch.object(MODULE, "_append_trace"),
        ):
            handler.do_POST()

        enqueue.assert_not_called()
        self.assertEqual(
            handler._send_json.call_args.args[0],
            MODULE.HTTPStatus.BAD_REQUEST,
        )

    def test_internal_type_error_returns_server_error(self) -> None:
        handler = self._handler(b"{}")

        with (
            mock.patch.object(
                MODULE,
                "_enqueue_request",
                side_effect=TypeError("internal failure"),
            ),
            mock.patch.object(MODULE, "_append_trace"),
        ):
            handler.do_POST()

        self.assertEqual(
            handler._send_json.call_args.args[0],
            MODULE.HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    def test_unexpected_request_parsing_error_returns_server_error(self) -> None:
        handler = self._handler(b"{}")
        handler._read_json = mock.Mock(side_effect=RecursionError("deep JSON"))

        with mock.patch.object(MODULE, "_append_trace"):
            handler.do_POST()

        self.assertEqual(
            handler._send_json.call_args.args[0],
            MODULE.HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    def test_only_top_level_ray_submission_id_is_used(self) -> None:
        self.assertEqual(
            MODULE._extract_ray_submission_id({
                "ray_submission_id": "canonical-submission",
                "ray_job_id": "wrong-job-id",
                "job_id": "wrong-job-id",
                "metadata": {"ray_submission_id": "wrong-metadata-id"},
                "trial_config": {"ray_submission_id": "wrong-trial-id"},
            }),
            "canonical-submission",
        )

    def test_missing_top_level_ray_submission_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "top-level ray_submission_id is required"):
            MODULE._extract_ray_submission_id({
                "ray_job_id": "fallback-must-not-be-used",
                "metadata": {"ray_submission_id": "fallback-must-not-be-used"},
                "trial_config": {"ray_submission_id": "fallback-must-not-be-used"},
            })

    def test_request_context_reaches_queue_zellij_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            dataset_root = root_path / "dataset"
            task_path = dataset_root / "task-1"
            task_path.mkdir(parents=True)
            (task_path / "task.yaml").write_text("instruction: test\n", encoding="utf-8")

            job_queue_root = root_path / "queue" / "jobs"
            trace_log = root_path / "trace.jsonl"
            ensure_zellij = mock.Mock(return_value="test-zellij-session")
            with (
                mock.patch.object(MODULE, "DEFAULT_DATASET_NAME", "seta"),
                mock.patch.object(MODULE, "DEFAULT_DATASET_ROOT", dataset_root),
                mock.patch.object(MODULE, "DEFAULT_DISABLED_TASK_IDS", ""),
                mock.patch.object(MODULE, "JOB_QUEUE_ROOT", job_queue_root),
                mock.patch.object(MODULE, "TRACE_LOG", trace_log),
                mock.patch.object(MODULE, "_ensure_submission_zellij", ensure_zellij),
                mock.patch.dict(os.environ, {"RL_DATASET_ROOTS": ""}),
            ):
                request_id, result_path = MODULE._enqueue_request({
                    "request_id": "request-1",
                    "task_id": "task-1",
                    "dataset_name": "seta",
                    "model_name": "model-from-request",
                    "ray_submission_id": "ray-submission-test",
                    "polar_task_id": "polar-task-test",
                })

            queue_dir = job_queue_root / "ray-submission-test"
            payload = json.loads(
                (queue_dir / "pending" / "request-1.json").read_text(encoding="utf-8")
            )
            trace = json.loads(trace_log.read_text(encoding="utf-8"))

            self.assertEqual(request_id, "request-1")
            self.assertEqual(result_path, queue_dir / "results" / "request-1.json")
            self.assertEqual(payload["model_name"], "model-from-request")
            self.assertEqual(payload["ray_submission_id"], "ray-submission-test")
            self.assertNotIn("ray_job_id", payload)
            self.assertEqual(payload["opik_project_name"], "ray-submission-test")
            self.assertEqual(trace["model_name"], "model-from-request")
            self.assertEqual(trace["ray_submission_id"], "ray-submission-test")
            self.assertNotIn("ray_job_id", trace)
            self.assertEqual(trace["opik_project_name"], "ray-submission-test")
            ensure_zellij.assert_called_once_with(
                "ray-submission-test",
                "seta",
                queue_dir,
                "model-from-request",
                "ray-submission-test",
            )

    def test_only_top_level_opik_project_name_overrides_submission(self) -> None:
        self.assertEqual(
            MODULE._extract_opik_project_name(
                {"opik_project_name": "project-from-request"},
                "ray-submission-test",
            ),
            "project-from-request",
        )
        self.assertEqual(
            MODULE._extract_opik_project_name(
                {
                    "metadata": {"opik_project_name": "project-from-metadata"},
                    "trial_config": {"opik_project_name": "project-from-trial"},
                },
                "ray-submission-test",
            ),
            "ray-submission-test",
        )


if __name__ == "__main__":
    unittest.main()
