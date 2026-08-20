"""Create and inspect QZ sandbox templates backed by existing images."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

DEFAULT_API_URL = "https://qz-sbx-api.sii.edu.cn"
API_VERSION_SUFFIX = "/v1"
DEFAULT_BUILD_TIMEOUT_SEC = 600.0
DEFAULT_REQUEST_TIMEOUT_SEC = 30.0
POLL_INTERVAL_SEC = 2.0
SPEC_CHOICES = ("g.c1", "g.c2", "g.c4")
FAILED_BUILD_STATUSES = frozenset({"error", "failed"})
TEMPLATE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")
BUILD_TIMESTAMP_KEYS = ("createdAt", "startedAt", "updatedAt", "created")


class QzTemplateError(RuntimeError):
    """Base error for QZ template operations."""


class QzTemplateApiError(QzTemplateError):
    """An HTTP error returned by the QZ template API."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"QZ template API returned HTTP {status}: {message}")


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the Sandbox API key on the configured origin."""

    def redirect_request(self, request, file, code, message, headers, new_url):
        raise QzTemplateError("QZ template API refused an HTTP redirect")


def _urlopen_without_redirects(request, *, timeout):
    opener = urllib.request.build_opener(_RejectRedirectHandler())
    return opener.open(request, timeout=timeout)


def normalize_api_url(value: str) -> str:
    """Normalize a QZ Sandbox API URL so it ends in exactly one ``/v1``."""
    base = value.strip().rstrip("/")
    if not base:
        base = DEFAULT_API_URL
    if base.endswith(API_VERSION_SUFFIX):
        return base
    return base + API_VERSION_SUFFIX


def resolve_api_key(environ: Mapping[str, str] = os.environ) -> str:
    """Resolve the Sandbox API key using the QZ adapter's precedence."""
    explicit_key = (
        environ.get("QZ_SANDBOX_API_KEY", "").strip()
        or environ.get("SBX_API_KEY", "").strip()
    )
    if explicit_key:
        return explicit_key

    legacy_key = environ.get("E2B_API_KEY", "").strip()
    if legacy_key.startswith("sbx_"):
        return legacy_key

    raise QzTemplateError(
        "set QZ_SANDBOX_API_KEY, SBX_API_KEY, or an sbx_-prefixed "
        "E2B_API_KEY before using the manager"
    )


def resolve_api_url(environ: Mapping[str, str] = os.environ) -> str:
    """Resolve the Sandbox API URL using the QZ adapter's precedence."""
    value = (
        environ.get("QZ_SANDBOX_API_URL", "").strip()
        or environ.get("SBX_API_URL", "").strip()
        or DEFAULT_API_URL
    )
    return normalize_api_url(value)


def _json_payload(raw: bytes, context: str) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QzTemplateError(f"{context} returned invalid JSON") from exc


def _api_error_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", errors="replace").strip()
        return text[:500] or "empty error response"
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return str(payload)[:500]


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QzTemplateError(f"{context} response is missing {key}")
    return value.strip()


class QzTemplateClient:
    """Small urllib client for QZ's E2B-compatible template routes."""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = normalize_api_url(api_url)
        self.request_timeout = request_timeout
        self._opener = opener or _urlopen_without_redirects

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.api_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.request_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise QzTemplateApiError(
                exc.code,
                _api_error_message(raw),
            ) from exc
        except urllib.error.URLError as exc:
            raise QzTemplateError(
                f"QZ template API request failed: {exc.reason}"
            ) from exc
        return _json_payload(raw, f"{method} {path}")

    def list_templates(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/templates")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise QzTemplateError("list templates response must be a JSON array")
        return payload

    def resolve_alias(self, name: str) -> dict[str, Any] | None:
        encoded_name = urllib.parse.quote(name, safe="")
        try:
            payload = self._request("GET", f"/templates/aliases/{encoded_name}")
        except QzTemplateApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(payload, dict):
            raise QzTemplateError("template alias response must be a JSON object")
        return payload

    def get_template(self, template_id: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(template_id, safe="")
        payload = self._request("GET", f"/templates/{encoded_id}")
        if not isinstance(payload, dict):
            raise QzTemplateError("get template response must be a JSON object")
        return payload

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        alias = self.resolve_alias(name)
        if alias is None:
            return None
        template_id = _required_string(alias, "templateID", "resolve alias")
        return self.get_template(template_id)

    def create_template(self, name: str, spec: str) -> tuple[str, str]:
        payload = self._request(
            "POST",
            "/templates",
            {"name": name, "sbxSpecCode": spec},
        )
        if not isinstance(payload, dict):
            raise QzTemplateError("create template response must be a JSON object")
        return (
            _required_string(payload, "templateID", "create template"),
            _required_string(payload, "buildID", "create template"),
        )

    def start_build(
        self,
        *,
        template_id: str,
        build_id: str,
        image: str,
        image_source: str,
    ) -> None:
        encoded_template_id = urllib.parse.quote(template_id, safe="")
        encoded_build_id = urllib.parse.quote(build_id, safe="")
        self._request(
            "POST",
            f"/templates/{encoded_template_id}/builds/{encoded_build_id}",
            {
                "fromImage": image,
                "imageSource": image_source,
                "steps": [],
            },
        )

    def get_build_status(
        self,
        *,
        template_id: str,
        build_id: str,
    ) -> dict[str, Any]:
        encoded_template_id = urllib.parse.quote(template_id, safe="")
        encoded_build_id = urllib.parse.quote(build_id, safe="")
        payload = self._request(
            "GET",
            f"/templates/{encoded_template_id}/builds/{encoded_build_id}/status",
        )
        if not isinstance(payload, dict):
            raise QzTemplateError("build status response must be a JSON object")
        return payload


def _status_value(payload: Mapping[str, Any], key: str) -> str:
    status = payload.get(key)
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    return ""


def _build_timestamp(build: Mapping[str, Any]) -> str:
    for key in BUILD_TIMESTAMP_KEYS:
        value = build.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _latest_build_state(
    template: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any] | None]:
    """Return one status and the build record that supports it, if any."""
    builds = template.get("builds")
    valid_builds = (
        [build for build in builds if isinstance(build, dict)]
        if isinstance(builds, list)
        else []
    )
    timestamped = [
        (_build_timestamp(build), build)
        for build in valid_builds
        if _build_timestamp(build)
    ]
    if timestamped:
        latest_build = max(timestamped, key=lambda item: item[0])[1]
        return _status_value(latest_build, "status") or "unknown", latest_build

    status = _status_value(template, "buildStatus")
    if status:
        return status, None
    if valid_builds:
        latest_build = valid_builds[0]
        return _status_value(latest_build, "status") or "unknown", latest_build
    return "unknown", None


def _latest_build_status(template: Mapping[str, Any]) -> str:
    return _latest_build_state(template)[0]


def _log(message: str, stream: TextIO) -> None:
    print(message, file=stream)


def create_template_from_image(
    client: QzTemplateClient,
    *,
    name: str,
    image: str,
    spec: str,
    image_source: str,
    timeout: float,
    exists_ok: bool,
    stderr: TextIO | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Create one new template and wait for its image build to finish."""
    stderr = stderr or sys.stderr
    clock = clock or time.monotonic
    sleep = sleep or time.sleep

    image = image.strip()
    image_source = image_source.strip()
    if not image:
        raise QzTemplateError("image must not be empty")
    if not image_source:
        raise QzTemplateError("image source must not be empty")
    if not math.isfinite(timeout) or timeout <= 0:
        raise QzTemplateError("timeout must be a finite number greater than zero")

    existing = client.get_by_name(name)
    if existing is not None:
        template_id = _required_string(existing, "templateID", "get template")
        status = _latest_build_status(existing)
        if exists_ok and status == "ready":
            _log(
                f"template {name!r} already exists and is ready: {template_id}",
                stderr,
            )
            return template_id
        raise QzTemplateError(
            f"template {name!r} already exists with status {status!r} "
            f"(templateID={template_id}); refusing to rebuild it"
        )

    template_id, build_id = client.create_template(name, spec)
    _log(
        f"allocated templateID={template_id} buildID={build_id}",
        stderr,
    )
    try:
        client.start_build(
            template_id=template_id,
            build_id=build_id,
            image=image,
            image_source=image_source,
        )
    except QzTemplateError as exc:
        raise QzTemplateError(
            f"failed to start build for templateID={template_id} "
            f"buildID={build_id}: {exc}"
        ) from exc

    deadline = clock() + timeout
    previous_status = ""
    while True:
        payload = client.get_build_status(
            template_id=template_id,
            build_id=build_id,
        )
        status = _required_string(payload, "status", "build status").lower()
        if status != previous_status:
            _log(f"buildID={build_id} status={status}", stderr)
            previous_status = status
        if status == "ready":
            return template_id
        if status in FAILED_BUILD_STATUSES:
            raise QzTemplateError(
                f"template build ended with status {status!r} "
                f"(templateID={template_id}, buildID={build_id})"
            )

        now = clock()
        if now >= deadline:
            raise QzTemplateError(
                f"template build timed out after {timeout:g}s "
                f"(templateID={template_id}, buildID={build_id}, "
                f"last_status={status})"
            )
        sleep(min(POLL_INTERVAL_SEC, deadline - now))


def client_from_environment(
    environ: Mapping[str, str] = os.environ,
) -> QzTemplateClient:
    return QzTemplateClient(
        api_key=resolve_api_key(environ),
        api_url=resolve_api_url(environ),
    )


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite number greater than zero"
        )
    return parsed


def _image_reference(value: str) -> str:
    image = value.strip()
    if not image:
        raise argparse.ArgumentTypeError("image must not be empty")
    return image


def _image_source(value: str) -> str:
    source = value.strip()
    if not source:
        raise argparse.ArgumentTypeError("image source must not be empty")
    return source


def _template_name(value: str) -> str:
    if TEMPLATE_NAME_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "template name must contain only ASCII letters, digits, and underscores"
        )
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and inspect QZ sandbox templates.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        help="create a template from an existing image and wait until ready",
    )
    create.add_argument(
        "--name",
        required=True,
        type=_template_name,
        help="new template name (ASCII letters, digits, and underscores only)",
    )
    create.add_argument(
        "--image",
        required=True,
        type=_image_reference,
        help="existing image reference",
    )
    create.add_argument(
        "--spec",
        choices=SPEC_CHOICES,
        default="g.c1",
        help="QZ sandbox specification (default: g.c1)",
    )
    create.add_argument(
        "--image-source",
        default="official",
        type=_image_source,
        help="QZ image source value (default: official)",
    )
    create.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=DEFAULT_BUILD_TIMEOUT_SEC,
        help="build polling timeout in seconds (default: 600)",
    )
    create.add_argument(
        "--exists-ok",
        action="store_true",
        help="return an existing ready template instead of failing",
    )

    subparsers.add_parser("list", help="list templates as JSON")
    get = subparsers.add_parser("get", help="get one template by name as JSON")
    get.add_argument(
        "--name",
        required=True,
        type=_template_name,
        help="template name (ASCII letters, digits, and underscores only)",
    )
    return parser.parse_args(argv)


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        client = client_from_environment()
        if args.command == "list":
            _print_json(client.list_templates())
            return 0
        if args.command == "get":
            template = client.get_by_name(args.name)
            if template is None:
                raise QzTemplateError(f"template {args.name!r} was not found")
            _print_json(template)
            return 0

        template_id = create_template_from_image(
            client,
            name=args.name,
            image=args.image,
            spec=args.spec,
            image_source=args.image_source,
            timeout=args.timeout,
            exists_ok=args.exists_ok,
        )
        print(template_id)
        return 0
    except QzTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
