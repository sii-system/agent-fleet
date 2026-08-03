"""Tests for per-request rollout context propagation."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "rollout_remote_harbor.py"
SPEC = importlib.util.spec_from_file_location("rollout_remote_harbor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RolloutRequestContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def _handler(self, body: bytes):
        handler = object.__new__(MODULE.Handler)
        handler.path = "/run_trial"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._send_json = mock.Mock()
        return handler

    def _enqueue_with_temp_context(
        self,
        request: dict[str, object],
        *,
        task_name: str = "task-1",
    ) -> tuple[str, Path, Path, mock.Mock]:
        root_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        dataset_root = root_path / "dataset"
        task_path = dataset_root / task_name
        task_path.mkdir(parents=True)
        (task_path / "task.yaml").write_text("instruction: test\n", encoding="utf-8")

        job_queue_root = root_path / "queue" / "jobs"
        trace_log = root_path / "trace.jsonl"
        ensure_zellij = mock.Mock(return_value="test-zellij-session")
        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DATASET_NAME", "seta"))
        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DATASET_ROOT", dataset_root))
        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DISABLED_TASK_IDS", ""))
        self.stack.enter_context(mock.patch.object(MODULE, "JOB_QUEUE_ROOT", job_queue_root))
        self.stack.enter_context(mock.patch.object(MODULE, "TRACE_LOG", trace_log))
        self.stack.enter_context(mock.patch.object(MODULE, "_ensure_submission_zellij", ensure_zellij))
        self.stack.enter_context(mock.patch.dict(os.environ, {"RL_DATASET_ROOTS": ""}))

        full_request = {
            "request_id": "request-1",
            "task_id": task_name,
            "dataset_name": "seta",
            "model_name": "model-from-request",
            "ray_submission_id": "ray-submission-test",
            "polar_task_id": "polar-task-test",
        }
        full_request.update(request)
        request_id, result_path = MODULE._enqueue_request(full_request)
        return request_id, result_path, job_queue_root, ensure_zellij

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

            queue_dir = job_queue_root / MODULE._storage_id("ray-submission-test", prefix="submission")
            request_file_id = MODULE._storage_id("request-1", prefix="request")
            payload = json.loads(
                (queue_dir / "pending" / f"{request_file_id}.json").read_text(encoding="utf-8")
            )
            trace = json.loads(trace_log.read_text(encoding="utf-8"))

            self.assertEqual(request_id, "request-1")
            self.assertEqual(result_path, queue_dir / "results" / f"{request_file_id}.json")
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

    def test_enqueue_accepts_space_in_task_path_but_not_shell_chars_in_command_args(self) -> None:
        request_id, result_path, job_queue_root, ensure_zellij = self._enqueue_with_temp_context(
            {
                "request_id": "req_1.2-3",
                "task_id": "task with spaces",
                "ray_submission_id": "ray_1.2-3",
            },
            task_name="task with spaces",
        )

        queue_dir = job_queue_root / MODULE._storage_id("ray_1.2-3", prefix="submission")
        request_file_id = MODULE._storage_id("req_1.2-3", prefix="request")
        payload = json.loads((queue_dir / "pending" / f"{request_file_id}.json").read_text(encoding="utf-8"))

        self.assertEqual(request_id, "req_1.2-3")
        self.assertEqual(result_path, queue_dir / "results" / f"{request_file_id}.json")
        self.assertEqual(payload["request_file_id"], request_file_id)
        self.assertEqual(payload["task_id"], "task with spaces")
        ensure_zellij.assert_called_once_with(
            "ray_1.2-3",
            "seta",
            queue_dir,
            "model-from-request",
            "ray_1.2-3",
        )

    def test_submission_zellij_helper_receives_hashed_storage_identifier(self) -> None:
        root_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        queue_dir = root_path / "queue" / MODULE._storage_id("ray-submission-test", prefix="submission")
        runtime_root = root_path / "runtime"
        storage_id = MODULE._storage_id("ray-submission-test", prefix="submission")
        expected_session = MODULE._submission_session_name("ray-submission-test", "seta")
        helper = mock.Mock(return_value=(0, f"{expected_session}\n", ""))

        self.stack.enter_context(mock.patch.object(MODULE, "JOB_RUNTIME_ROOT", runtime_root))
        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_LOCKS", {}))
        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_READY", {}))
        self.stack.enter_context(mock.patch.object(MODULE, "_zellij_session_exists", mock.Mock(return_value=False)))
        self.stack.enter_context(mock.patch.object(MODULE, "_run_helper", helper))

        session = MODULE._ensure_submission_zellij(
            "ray-submission-test",
            "seta",
            queue_dir,
            "model-from-request",
            "ray-submission-test",
        )

        self.assertEqual(session, expected_session)
        env = helper.call_args.kwargs["env"]
        self.assertEqual(env["RL_ZELLIJ_SUBMISSION_ID"], "ray-submission-test")
        self.assertEqual(env["RL_ZELLIJ_SUBMISSION_STORAGE_ID"], storage_id)
        self.assertEqual(env["RL_ZELLIJ_JOB_QUEUE_DIR"], str(queue_dir))

    def test_existing_hashed_zellij_session_is_reused_without_helper(self) -> None:
        storage_id = MODULE._storage_id("ray-submission-test", prefix="submission")
        expected_session = MODULE._submission_session_name("ray-submission-test", "seta")

        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_LOCKS", {}))
        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_READY", {storage_id: expected_session}))
        self.stack.enter_context(mock.patch.object(MODULE, "_zellij_session_exists", mock.Mock(return_value=True)))
        helper = self.stack.enter_context(mock.patch.object(MODULE, "_run_helper"))

        session = MODULE._ensure_submission_zellij(
            "ray-submission-test",
            "seta",
            Path("/tmp/queue"),
            "model-from-request",
            "ray-submission-test",
        )

        self.assertEqual(session, expected_session)
        helper.assert_not_called()

    def test_legacy_raw_zellij_session_is_not_reused_for_hashed_queue(self) -> None:
        legacy_session = "harbor-rollout-claude-code-seta-ray-submission-test"
        hashed_session = MODULE._submission_session_name("ray-submission-test", "seta")
        helper = mock.Mock(return_value=(0, f"{hashed_session}\n", ""))

        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_LOCKS", {}))
        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_READY", {"ray-submission-test": legacy_session}))
        exists = self.stack.enter_context(mock.patch.object(MODULE, "_zellij_session_exists", mock.Mock(return_value=True)))
        self.stack.enter_context(mock.patch.object(MODULE, "_run_helper", helper))

        session = MODULE._ensure_submission_zellij(
            "ray-submission-test",
            "seta",
            Path("/tmp/queue"),
            "model-from-request",
            "ray-submission-test",
        )

        self.assertEqual(session, hashed_session)
        self.assertNotIn(mock.call(legacy_session), exists.call_args_list)
        helper.assert_called_once()

    def test_different_submission_cached_session_is_not_reused(self) -> None:
        other_storage_id = MODULE._storage_id("other-submission", prefix="submission")
        other_session = MODULE._submission_session_name("other-submission", "seta")
        current_session = MODULE._submission_session_name("ray-submission-test", "seta")
        helper = mock.Mock(return_value=(0, f"{current_session}\n", ""))

        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_LOCKS", {}))
        self.stack.enter_context(mock.patch.object(MODULE, "JOB_ZELLIJ_READY", {other_storage_id: other_session}))
        exists = self.stack.enter_context(mock.patch.object(MODULE, "_zellij_session_exists", mock.Mock(return_value=True)))
        self.stack.enter_context(mock.patch.object(MODULE, "_run_helper", helper))

        session = MODULE._ensure_submission_zellij(
            "ray-submission-test",
            "seta",
            Path("/tmp/queue"),
            "model-from-request",
            "ray-submission-test",
        )

        self.assertEqual(session, current_session)
        self.assertNotIn(mock.call(other_session), exists.call_args_list)
        helper.assert_called_once()

    def test_rejects_command_arg_injection_in_ray_submission_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "ray_submission_id may contain only"):
            self._enqueue_with_temp_context({"ray_submission_id": "ray; touch /tmp/pwned"})

    def test_rejects_command_arg_injection_in_dataset_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "dataset_name may contain only"):
            self._enqueue_with_temp_context({"dataset_name": "seta; touch /tmp/pwned"})

    def test_rejects_request_id_path_traversal_and_shell_metacharacters(self) -> None:
        for bad_request_id in ("../escape", "req/escape", "req with spaces", "req;touch"):
            with (
                self.subTest(bad_request_id=bad_request_id),
                self.assertRaisesRegex(ValueError, "request_id may contain only"),
            ):
                self._enqueue_with_temp_context({"request_id": bad_request_id})

    def test_rejects_task_path_outside_dataset_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "relative path inside"):
            self._enqueue_with_temp_context({"task_path": "../outside"})

        with self.assertRaisesRegex(ValueError, "relative path inside"):
            self._enqueue_with_temp_context({"task_path": "/tmp/outside"})

    def test_rejects_symlink_escape_from_dataset_root(self) -> None:
        root_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        dataset_root = root_path / "dataset"
        outside_task = root_path / "outside-task"
        dataset_root.mkdir()
        outside_task.mkdir()
        (dataset_root / "linked-task").symlink_to(outside_task, target_is_directory=True)

        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DATASET_NAME", "seta"))
        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DATASET_ROOT", dataset_root))
        self.stack.enter_context(mock.patch.dict(os.environ, {"RL_DATASET_ROOTS": ""}))

        with self.assertRaisesRegex(ValueError, "outside trusted root"):
            MODULE.resolve_task_path({"dataset_name": "seta", "task_id": "linked-task"})

    def test_accepts_request_dataset_root_only_when_configured(self) -> None:
        root_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        dataset_root = root_path / "dataset"
        (dataset_root / "task-1").mkdir(parents=True)

        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DATASET_NAME", "seta"))
        self.stack.enter_context(mock.patch.object(MODULE, "DEFAULT_DATASET_ROOT", dataset_root))
        self.stack.enter_context(mock.patch.dict(os.environ, {"RL_DATASET_ROOTS": ""}))

        self.assertEqual(MODULE._dataset_root("seta", str(dataset_root)), dataset_root.resolve())
        with self.assertRaisesRegex(ValueError, "dataset_root must match a configured dataset root"):
            MODULE._dataset_root("seta", str(root_path / "other"))

    def test_secret_like_request_fields_are_not_persisted_to_queue(self) -> None:
        _, _, job_queue_root, _ = self._enqueue_with_temp_context({
            "trial_config": {
                "agent": {
                    "kwargs": {
                        "api_base": "https://example.test/v1",
                        "llm_kwargs": {
                            "api_key": "request-secret",
                        },
                    },
                },
            },
            "metadata": {"access_token": "request-token"},
        })

        payload = self._read_default_payload(job_queue_root)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("api_key", payload)
        self.assertNotIn("trial_config", payload)
        self.assertNotIn("metadata", payload)
        self.assertNotIn("request-secret", serialized)
        self.assertNotIn("request-token", serialized)

    def _read_default_payload(self, job_queue_root: Path) -> dict[str, object]:
        return json.loads(
            (
                job_queue_root
                / MODULE._storage_id("ray-submission-test", prefix="submission")
                / "pending"
                / f"{MODULE._storage_id('request-1', prefix='request')}.json"
            ).read_text(encoding="utf-8")
        )

    def test_enqueued_payload_preserves_nested_api_base(self) -> None:
        _, _, job_queue_root, _ = self._enqueue_with_temp_context({
            "trial_config": {
                "agent": {
                    "kwargs": {
                        "api_base": "https://polar.example.test/v1/chat/completions",
                    },
                },
            },
        })

        payload = self._read_default_payload(job_queue_root)
        self.assertEqual(payload["api_base"], "https://polar.example.test/v1/chat/completions")
        self.assertNotIn("trial_config", payload)

    def test_top_level_api_base_takes_precedence_over_nested_value(self) -> None:
        _, _, job_queue_root, _ = self._enqueue_with_temp_context({
            "api_base": "https://top.example.test/v1",
            "trial_config": {
                "agent": {
                    "kwargs": {
                        "api_base": "https://nested.example.test/v1",
                    },
                },
            },
        })

        payload = self._read_default_payload(job_queue_root)
        self.assertEqual(payload["api_base"], "https://top.example.test/v1")

    def test_rejects_invalid_api_base_format(self) -> None:
        for api_base in ("/relative", "ftp://example.test/v1", "https://example.test/evil\nheader"):
            with (
                self.subTest(api_base=api_base),
                self.assertRaisesRegex(ValueError, "api_base"),
            ):
                self._enqueue_with_temp_context({"api_base": api_base})

    def test_enqueued_payload_does_not_store_per_request_api_key(self) -> None:
        _, _, job_queue_root, _ = self._enqueue_with_temp_context({"api_base": "https://example.test/v1"})

        payload = self._read_default_payload(job_queue_root)
        self.assertNotIn("api_key", payload)
        self.assertEqual(payload["api_base"], "https://example.test/v1")

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
