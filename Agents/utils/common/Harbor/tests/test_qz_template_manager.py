import argparse
import contextlib
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import qz_template_manager as manager


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.raw


class QueueOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, tuple) and response[0] == "error":
            _, status, payload = response
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                str(payload),
                hdrs=None,
                fp=io.BytesIO(json.dumps(payload).encode()),
            )
        return FakeResponse(response)


def request_body(call):
    request, _ = call
    if request.data is None:
        return None
    return json.loads(request.data.decode())


class RedirectSecurityTest(unittest.TestCase):
    def test_default_opener_installs_redirect_rejection(self):
        director = Mock()
        response = FakeResponse({})
        director.open.return_value = response
        request = manager.urllib.request.Request(
            "https://qz.example/v1/templates",
            headers={"X-API-Key": "sbx-secret"},
        )

        with patch.object(
            manager.urllib.request,
            "build_opener",
            return_value=director,
        ) as build_opener:
            actual = manager._urlopen_without_redirects(request, timeout=30)

        self.assertIs(actual, response)
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, manager._RejectRedirectHandler)
        director.open.assert_called_once_with(request, timeout=30)

    def test_redirect_handler_stops_cross_origin_key_forwarding(self):
        request = manager.urllib.request.Request(
            "https://qz.example/v1/templates",
            headers={"X-API-Key": "sbx-secret"},
        )

        with self.assertRaisesRegex(manager.QzTemplateError, "refused"):
            manager._RejectRedirectHandler().redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example/collect",
            )


class ConfigurationTest(unittest.TestCase):
    def test_qz_variables_take_precedence(self):
        environ = {
            "QZ_SANDBOX_API_KEY": "qz-key",
            "SBX_API_KEY": "sbx-key",
            "QZ_SANDBOX_API_URL": "https://qz.example/v1/",
            "SBX_API_URL": "https://sbx.example",
        }
        self.assertEqual(manager.resolve_api_key(environ), "qz-key")
        self.assertEqual(manager.resolve_api_url(environ), "https://qz.example/v1")

    def test_platform_aliases_and_default_url(self):
        self.assertEqual(
            manager.resolve_api_url({"SBX_API_URL": "https://sbx.example"}),
            "https://sbx.example/v1",
        )
        self.assertEqual(
            manager.resolve_api_url({}),
            "https://qz-sbx-api.sii.edu.cn/v1",
        )
        self.assertEqual(manager.resolve_api_key({"SBX_API_KEY": "sbx-key"}), "sbx-key")

    def test_legacy_e2b_key_is_accepted_only_for_qz(self):
        self.assertEqual(
            manager.resolve_api_key({"E2B_API_KEY": "sbx_legacy"}),
            "sbx_legacy",
        )
        with self.assertRaisesRegex(manager.QzTemplateError, "sbx_-prefixed"):
            manager.resolve_api_key({"E2B_API_KEY": "e2b-cloud"})

    def test_missing_key_fails(self):
        with self.assertRaisesRegex(manager.QzTemplateError, "SBX_API_KEY"):
            manager.resolve_api_key({})

    def test_timeout_rejects_non_finite_values(self):
        for value in ("nan", "inf", "-inf", "0"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    argparse.ArgumentTypeError,
                    "finite number greater than zero",
                ),
            ):
                manager._positive_timeout(value)

    def test_create_args_reject_blank_build_inputs(self):
        cases = (
            (["--image", ""], "image must not be empty"),
            (
                ["--image", "registry.example/task:tag", "--image-source", " "],
                "image source must not be empty",
            ),
        )
        for options, message in cases:
            stderr = io.StringIO()
            with (
                self.subTest(options=options),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                manager.parse_args(["create", "--name", "harbor_task_demo", *options])
            self.assertIn(message, stderr.getvalue())

    def test_latest_build_status_uses_timestamps_and_top_level_fallback(self):
        timestamped = {
            "builds": [
                {
                    "status": "ready",
                    "createdAt": "2026-08-18T10:00:00+08:00",
                },
                {
                    "status": "building",
                    "createdAt": "2026-08-18T10:01:00+08:00",
                },
            ]
        }
        self.assertEqual(
            manager._latest_build_status(timestamped),
            "building",
        )
        status, build = manager._latest_build_state(timestamped)
        self.assertEqual(status, "building")
        self.assertEqual(build["createdAt"], "2026-08-18T10:01:00+08:00")
        self.assertEqual(
            manager._latest_build_status({"buildStatus": "READY"}),
            "ready",
        )
        self.assertEqual(
            manager._latest_build_state(
                {
                    "buildStatus": "READY",
                    "builds": [{"status": "building", "sbxSpecCode": "g.c2"}],
                }
            ),
            ("ready", None),
        )

    def test_template_name_accepts_platform_format(self):
        self.assertEqual(
            manager._template_name("harbor_task_123"),
            "harbor_task_123",
        )

    def test_template_name_rejects_unsupported_characters(self):
        for name in ("harbor-task", "harbor task", "模板"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    argparse.ArgumentTypeError,
                    "ASCII letters, digits, and underscores",
                ),
            ):
                manager._template_name(name)


class CreateTemplateTest(unittest.TestCase):
    def client(self, opener):
        return manager.QzTemplateClient(
            api_key="sbx-test",
            api_url="https://qz.example",
            opener=opener,
        )

    def create(self, opener, **overrides):
        options = {
            "name": "harbor_task_demo",
            "image": "registry.example/task:tag",
            "spec": "g.c1",
            "image_source": "official",
            "timeout": 10.0,
            "exists_ok": False,
            "stderr": io.StringIO(),
            "clock": lambda: 0.0,
            "sleep": lambda _seconds: None,
        }
        options.update(overrides)
        return manager.create_template_from_image(self.client(opener), **options)

    def test_create_binds_image_and_polls_until_ready(self):
        opener = QueueOpener(
            ("error", 404, {"message": "not found"}),
            {"templateID": "template-1", "buildID": "build-1"},
            {},
            {"status": "waiting"},
            {"status": "ready"},
        )
        clock = iter([0.0, 1.0]).__next__
        sleeps = []
        stderr = io.StringIO()

        template_id = self.create(
            opener,
            stderr=stderr,
            clock=clock,
            sleep=sleeps.append,
        )

        self.assertEqual(template_id, "template-1")
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(
            [call[0].get_method() for call in opener.calls],
            ["GET", "POST", "POST", "GET", "GET"],
        )
        self.assertEqual(
            request_body(opener.calls[1]),
            {"name": "harbor_task_demo", "sbxSpecCode": "g.c1"},
        )
        self.assertEqual(
            request_body(opener.calls[2]),
            {
                "fromImage": "registry.example/task:tag",
                "imageSource": "official",
                "steps": [],
            },
        )
        self.assertIn("templateID=template-1 buildID=build-1", stderr.getvalue())
        self.assertIn("status=ready", stderr.getvalue())
        headers = dict(opener.calls[1][0].header_items())
        self.assertEqual(headers["X-api-key"], "sbx-test")

    def test_existing_template_is_rejected_before_create(self):
        opener = QueueOpener(
            {"templateID": "template-1"},
            {
                "templateID": "template-1",
                "builds": [{"status": "ready"}],
            },
        )

        with self.assertRaisesRegex(manager.QzTemplateError, "refusing to rebuild"):
            self.create(opener)

        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(
            opener.calls[0][0].full_url.endswith(
                "/v1/templates/aliases/harbor_task_demo"
            )
        )

    def test_exists_ok_returns_only_an_existing_ready_template(self):
        opener = QueueOpener(
            {"templateID": "template-1"},
            {
                "templateID": "template-1",
                "builds": [{"status": "ready"}],
            },
        )

        template_id = self.create(opener, exists_ok=True)

        self.assertEqual(template_id, "template-1")
        self.assertEqual(len(opener.calls), 2)

    def test_exists_ok_rejects_non_ready_template(self):
        opener = QueueOpener(
            {"templateID": "template-1"},
            {
                "templateID": "template-1",
                "builds": [{"status": "building"}],
            },
        )

        with self.assertRaisesRegex(manager.QzTemplateError, "status 'building'"):
            self.create(opener, exists_ok=True)

        self.assertEqual(len(opener.calls), 2)

    def test_invalid_build_inputs_fail_before_api_calls(self):
        cases = (
            ({"image": " "}, "image must not be empty"),
            ({"image_source": " "}, "image source must not be empty"),
            ({"timeout": float("nan")}, "finite number greater than zero"),
            ({"timeout": float("inf")}, "finite number greater than zero"),
        )
        for overrides, message in cases:
            opener = QueueOpener()
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(manager.QzTemplateError, message),
            ):
                self.create(opener, **overrides)
            self.assertEqual(opener.calls, [])

    def test_build_error_reports_allocated_ids(self):
        opener = QueueOpener(
            ("error", 404, {"message": "not found"}),
            {"templateID": "template-1", "buildID": "build-1"},
            {},
            {"status": "error"},
        )

        with self.assertRaisesRegex(
            manager.QzTemplateError,
            r"templateID=template-1, buildID=build-1",
        ):
            self.create(opener)

    def test_polling_timeout_reports_last_status(self):
        opener = QueueOpener(
            ("error", 404, {"message": "not found"}),
            {"templateID": "template-1", "buildID": "build-1"},
            {},
            {"status": "building"},
        )
        clock = iter([0.0, 11.0]).__next__

        with self.assertRaisesRegex(
            manager.QzTemplateError,
            r"timed out.*last_status=building",
        ):
            self.create(opener, clock=clock)


class InspectTemplateTest(unittest.TestCase):
    def test_list_and_get_by_name(self):
        opener = QueueOpener(
            [{"templateID": "template-1", "names": ["harbor_task_demo"]}],
            {"templateID": "template-1"},
            {
                "templateID": "template-1",
                "builds": [{"status": "ready"}],
            },
        )
        client = manager.QzTemplateClient(
            api_key="sbx-test",
            api_url="https://qz.example",
            opener=opener,
        )

        templates = client.list_templates()
        template = client.get_by_name("harbor_task_demo")

        self.assertEqual(templates[0]["templateID"], "template-1")
        self.assertEqual(template["templateID"], "template-1")
        self.assertEqual(
            [call[0].full_url for call in opener.calls],
            [
                "https://qz.example/v1/templates",
                "https://qz.example/v1/templates/aliases/harbor_task_demo",
                "https://qz.example/v1/templates/template-1",
            ],
        )


class CliOutputTest(unittest.TestCase):
    def test_create_writes_only_template_id_to_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"SBX_API_KEY": "sbx-test"}, clear=True),
            patch.object(manager, "client_from_environment", return_value=object()),
            patch.object(
                manager,
                "create_template_from_image",
                return_value="template-1",
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = manager.main(
                [
                    "create",
                    "--name",
                    "harbor_task_demo",
                    "--image",
                    "registry.example/task:tag",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "template-1\n")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
