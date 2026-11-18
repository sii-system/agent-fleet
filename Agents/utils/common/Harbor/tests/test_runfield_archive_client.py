"""Validate Runfield archive streams, bounds, and host file ownership."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.runfield_controller import (
    client as client_mod,
)
from harbor_environment_adapters.providers.runfield_controller.client import (
    RunfieldControllerClient,
    RunfieldControllerError,
)


class _StubSandboxError(Exception):
    def __init__(
        self,
        code: str,
        *,
        request_id: str | None = None,
        sandbox: object | None = None,
        http_status: int | None = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.request_id = request_id
        self.sandbox = sandbox
        self.http_status = http_status
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


class RunfieldArchiveClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = b"\0\xffarchive bytes" * 10000
        self.received = b""
        self.upload_calls: list[tuple[str, Path, float]] = []
        self.download_calls: list[tuple[str, Path, float]] = []
        self.stat_calls: list[tuple[str, float]] = []
        self.fail_upload: Exception | None = None
        self.fail_download_before_open: Exception | None = None
        self.fail_download_after_partial: Exception | None = None
        self.block_transfer = False
        self.transfer_started: asyncio.Event | None = None
        self.transfer_release: asyncio.Event | None = None
        owner = self

        class FakeSandbox:
            def __init__(self, sandbox_id: str) -> None:
                self.sandbox_id = sandbox_id
                self.state = "ready"
                self.record = {
                    "sandbox_id": sandbox_id,
                    "state": "ready",
                    "observation": {"reachable": True},
                }

            async def _maybe_block(self) -> None:
                if owner.block_transfer:
                    assert owner.transfer_started is not None
                    assert owner.transfer_release is not None
                    owner.transfer_started.set()
                    await owner.transfer_release.wait()

            async def stat(
                self, path: str, *, timeout_seconds: float = 15
            ) -> dict[str, object]:
                owner.stat_calls.append((path, timeout_seconds))
                return {"path": path, "exists": True, "type": "directory", "size": 4096}

            async def upload_archive(
                self,
                path: str,
                source: Path,
                *,
                timeout_seconds: float = 300,
            ) -> None:
                owner.upload_calls.append((path, Path(source), timeout_seconds))
                owner.received = Path(source).read_bytes()
                await self._maybe_block()
                if owner.fail_upload is not None:
                    raise owner.fail_upload

            async def download_archive(
                self,
                path: str,
                dest: Path | None = None,
                *,
                timeout_seconds: float = 300,
            ) -> int:
                assert dest is not None
                dest_path = Path(dest)
                owner.download_calls.append((path, dest_path, timeout_seconds))
                if owner.fail_download_before_open is not None:
                    raise owner.fail_download_before_open
                dest_path.write_bytes(owner.payload[:32])
                await self._maybe_block()
                if owner.fail_download_after_partial is not None:
                    raise owner.fail_download_after_partial
                dest_path.write_bytes(owner.payload)
                return len(owner.payload)

        class FakeSandboxClient:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def get(
                self, sandbox_id: str, *, timeout_seconds: float = 15
            ) -> FakeSandbox:
                _ = timeout_seconds
                return FakeSandbox(sandbox_id)

            async def aclose(self) -> None:
                return None

        stub = types.ModuleType("runfield")
        stub.ExecutionResult = object
        stub.Sandbox = FakeSandbox
        stub.SandboxClient = FakeSandboxClient
        stub.SandboxError = _StubSandboxError
        self._patcher = patch.dict(sys.modules, {"runfield": stub})
        self._patcher.start()
        client_mod._LOOP_SHARED_STATES.clear()
        self.client = RunfieldControllerClient(
            "http://127.0.0.1:19112", allow_insecure_http=True
        )

    def tearDown(self) -> None:
        client_mod._LOOP_SHARED_STATES.clear()
        self._patcher.stop()

    def test_binary_upload_and_download_and_stat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.tar"
            source.write_bytes(self.payload)
            asyncio.run(self.client.upload_archive("sandbox-one", "/tests", source))
            self.assertEqual(self.received, self.payload)
            target = root / "download.tar"
            asyncio.run(
                self.client.download_archive(
                    "sandbox-one", "/tests", target, len(self.payload)
                )
            )
            self.assertEqual(target.read_bytes(), self.payload)
            stat = asyncio.run(self.client.stat_path("sandbox-one", "/tests"))
            self.assertEqual(stat["exists"], True)

    def test_upload_is_single_attempt_and_rejects_empty_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty.tar"
            empty.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "exceeds protocol limit"):
                asyncio.run(self.client.upload_archive("sandbox-one", "/tests", empty))
            self.assertEqual(self.upload_calls, [])

            valid = Path(temporary) / "valid.tar"
            valid.write_bytes(b"tar-bytes")
            self.fail_upload = _StubSandboxError(
                "transport_error", retryable=True, http_status=503
            )
            with self.assertRaises(RunfieldControllerError) as ctx:
                asyncio.run(self.client.upload_archive("sandbox-one", "/tests", valid))
            self.assertEqual(ctx.exception.code, "transport_error")
            self.assertTrue(ctx.exception.retryable)
            self.assertEqual(len(self.upload_calls), 1)

    def test_truncated_and_oversized_downloads_remove_only_owned_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download.tar"
            with self.assertRaisesRegex(ValueError, "exceeds configured limit"):
                asyncio.run(
                    self.client.download_archive("sandbox-one", "/tests", target, 1)
                )
            self.assertFalse(target.exists())

            self.fail_download_after_partial = _StubSandboxError("transport_error")
            with self.assertRaises(RunfieldControllerError):
                asyncio.run(
                    self.client.download_archive(
                        "sandbox-one", "/tests", target, len(self.payload) + 1
                    )
                )
            self.assertFalse(target.exists())
            self.fail_download_after_partial = None

            self.fail_download_after_partial = _StubSandboxError(
                "resource_limit_exceeded", http_status=200
            )
            with self.assertRaisesRegex(ValueError, "exceeds configured limit"):
                asyncio.run(
                    self.client.download_archive(
                        "sandbox-one", "/tests", target, len(self.payload) + 1
                    )
                )
            self.assertFalse(target.exists())
            self.fail_download_after_partial = None

            target.write_bytes(b"keep existing file")
            with self.assertRaises(FileExistsError):
                asyncio.run(
                    self.client.download_archive(
                        "sandbox-one", "/tests", target, len(self.payload)
                    )
                )
            self.assertEqual(target.read_bytes(), b"keep existing file")

    def test_wrong_media_type_is_rejected_before_creating_staging(self) -> None:
        self.fail_download_before_open = _StubSandboxError(
            "invalid_response", http_status=200
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download.tar"
            with self.assertRaises(RunfieldControllerError) as ctx:
                asyncio.run(
                    self.client.download_archive(
                        "sandbox-one", "/tests", target, len(self.payload)
                    )
                )
            self.assertEqual(ctx.exception.code, "invalid_response")
            self.assertFalse(target.exists())

    def test_archive_timeout_is_independent_from_control_timeout(self) -> None:
        self.client.request_timeout_sec = 7
        self.client.transfer_timeout_sec = 93
        self.test_binary_upload_and_download_and_stat()
        self.assertEqual(self.upload_calls[-1][2], 93)
        self.assertEqual(self.download_calls[-1][2], 93)
        self.assertEqual(self.stat_calls[-1][1], 7)

    def test_cancelled_transfers_clean_staging_before_tempdir_exits(self) -> None:
        async def exercise(upload: bool) -> None:
            paths: list[Path] = []
            self.block_transfer = True
            self.transfer_started = asyncio.Event()
            self.transfer_release = asyncio.Event()

            async def transfer_with_owned_path() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    archive = Path(temporary) / "data.tar"
                    paths.append(archive)
                    if upload:
                        archive.write_bytes(self.payload)
                        await self.client.upload_archive("sandbox-one", "/tests", archive)
                    else:
                        await self.client.download_archive(
                            "sandbox-one", "/tests", archive, len(self.payload)
                        )

            operation = asyncio.create_task(transfer_with_owned_path())
            await asyncio.wait_for(self.transfer_started.wait(), timeout=2.0)
            self.assertTrue(paths[0].parent.exists())
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertFalse(paths[0].parent.exists())

        for upload in (True, False):
            with self.subTest(upload=upload):
                asyncio.run(exercise(upload))


if __name__ == "__main__":
    unittest.main()
