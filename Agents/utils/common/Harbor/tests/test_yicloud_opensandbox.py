import asyncio
import base64
import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = HARBOR_DIR / "yicloud_opensandbox.py"
sys.path.insert(0, str(HARBOR_DIR))


def install_harbor_stubs() -> None:
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    base = types.ModuleType("harbor.environments.base")
    capabilities = types.ModuleType("harbor.environments.capabilities")

    class BaseEnvironment:
        default_user = None

        def _resolve_user(self, user):
            return user if user is not None else self.default_user

    class ExecResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Capability:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    base.BaseEnvironment = BaseEnvironment
    base.ExecResult = ExecResult
    capabilities.EnvironmentCapabilities = Capability
    capabilities.EnvironmentResourceCapabilities = Capability
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": environments,
            "harbor.environments.base": base,
            "harbor.environments.capabilities": capabilities,
        }
    )


install_harbor_stubs()
spec = importlib.util.spec_from_file_location("yicloud_opensandbox", MODULE_PATH)
assert spec is not None and spec.loader is not None
yicloud_opensandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yicloud_opensandbox)


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def make_healthcheck_environment(ready_timeout_sec: int):
    instance = object.__new__(
        yicloud_opensandbox.YiCloudOpenSandboxEnvironment
    )
    runtime = yicloud_opensandbox.ServiceRuntime(
        "worker",
        {
            "runtime": {
                "readiness": {
                    "type": "healthcheck",
                    "healthcheck": {"test": "check-ready"},
                }
            }
        },
    )
    instance._services = {runtime.name: runtime}
    instance._main_service = runtime.name
    instance._ready_timeout_sec = ready_timeout_sec
    return instance


class FakeSandbox:
    def __init__(self, environments):
        self.environments = environments
        self.models = SimpleNamespace(
            ListSandboxEnvironmentsReq=Request,
            GetSandboxEnvironmentReq=Request,
        )

    def list_sandbox_environments(self, _context, _request):
        return SimpleNamespace(Items=self.environments)

    def get_sandbox_environment(self, _context, request):
        return next(
            (
                item
                for item in self.environments
                if item.Id == request.EnvironmentId
            ),
            SimpleNamespace(Id="", Name=""),
        )


class YiCloudOpenSandboxTest(unittest.TestCase):
    def test_v2_bundle_loads_digest_refs_and_rejects_unsupported_capabilities(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "bundle.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "main": "main",
                        "services": {
                            "main": {
                                "image": {
                                    "digest_ref": "registry/seta/0@sha256:" + "a" * 64
                                }
                            },
                            "worker": {
                                "image": {
                                    "digest_ref": "registry/seta/0@sha256:" + "b" * 64
                                }
                            },
                        },
                        "requirements": {"multi_service": True},
                    }
                ),
                encoding="utf-8",
            )
            instance._bundle_manifest_path = str(manifest)
            instance._bundle = None
            instance._services = {}
            instance._main_service = "main"
            instance._load_bundle()

        self.assertEqual(instance._main_service, "main")
        self.assertEqual(set(instance._services), {"main", "worker"})

        instance._bundle["requirements"] = {"fixed_ip": True}
        with self.assertRaisesRegex(RuntimeError, "capability gate rejected"):
            instance._capability_gate()

    def test_service_port_parsing_uses_compose_container_ports(self) -> None:
        self.assertEqual(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_ports(
                {"ports": ["127.0.0.1:8080:80/tcp", "5432", {"target": 22}]}
            ),
            [80, 5432, 22],
        )

    def test_service_start_command_preserves_compose_argv(self) -> None:
        command = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_start_command
        self.assertEqual(command({"command": ["sh", "-c", "sleep infinity"]}), ["sh", "-c", "sleep infinity"])
        self.assertEqual(command({"entrypoint": ["/entry"], "command": ["--serve"]}), ["/entry", "--serve"])
        self.assertIsNone(command({}))

    def test_runtime_contract_is_preferred_over_legacy_compose_fields(self) -> None:
        start = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_start_command
        ports = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._service_ports
        spec = {
            "command": ["incorrect", "legacy"],
            "runtime": {
                "start_argv": ["/usr/sbin/sshd", "-D"],
                "internal_ports": [
                    {"port": 22, "protocol": "tcp", "source": "image-config.exposed-ports"}
                ],
            },
        }
        self.assertEqual(start(spec), ["/usr/sbin/sshd", "-D"])
        self.assertEqual(ports(spec), [22])
        with self.assertRaisesRegex(RuntimeError, "non-empty string list"):
            start({"runtime": {"start_argv": []}})

    def test_all_composite_entrypoints_wait_for_alias_wiring(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        worker = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {
                "aliases": ["database"],
                "depends_on": {},
                "runtime": {"start_argv": ["worker"]},
            },
        )
        main = yicloud_opensandbox.ServiceRuntime(
            "main",
            {
                "depends_on": {},
                "runtime": {"start_argv": ["main"]},
            },
        )
        main.sandbox_name = "test-main"
        worker.sandbox_name = "test-worker"
        instance._services = {"main": main, "worker": worker}

        for runtime in (main, worker):
            entrypoint = instance._service_entrypoint(runtime)
            assert entrypoint is not None
            self.assertEqual(entrypoint[:2], ["sh", "-c"])
            self.assertIn(
                yicloud_opensandbox.COMPOSE_START_MARKER_PREFIX
                + runtime.sandbox_name,
                entrypoint[2],
            )
            self.assertIn('exec "$@"', entrypoint[2])
            self.assertEqual(entrypoint[-1], runtime.name)

    def test_release_service_entrypoint_touches_its_marker(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_name = "test-worker"
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        asyncio.run(instance._release_service_entrypoint(runtime))

        instance._run_service_command.assert_awaited_once_with(
            runtime,
            "touch /tmp/.harbor-compose-start-test-worker",
            timeout_sec=60,
        )

    def test_service_hosts_block_uses_real_newlines(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        main = yicloud_opensandbox.ServiceRuntime(
            "main", {"aliases": ["api", "main"]}
        )
        worker = yicloud_opensandbox.ServiceRuntime(
            "worker", {"aliases": ["database"]}
        )
        main.internal_address = "10.0.0.1"
        worker.internal_address = "10.0.0.2"
        instance._services = {"main": main, "worker": worker}
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        asyncio.run(instance._wire_service_aliases())

        command = instance._run_service_command.await_args_list[0].args[1]
        encoded = command.split("printf %s ", 1)[1].split(" | base64", 1)[0]
        block = base64.b64decode(encoded).decode("utf-8")
        self.assertEqual(
            block,
            "10.0.0.1 main api\n10.0.0.2 worker database",
        )
        self.assertNotIn("\\n", block)
        self.assertIn(
            f"printf '\\n{yicloud_opensandbox.HOSTS_BLOCK_END}\\n'",
            command,
        )
        with tempfile.TemporaryDirectory() as tmp:
            hosts_path = Path(tmp) / "hosts"
            hosts_path.write_text("127.0.0.1 localhost\n", encoding="utf-8")
            test_command = command.replace(
                "/etc/hosts", shlex.quote(str(hosts_path))
            )
            rendered_hosts = []
            for _ in range(2):
                subprocess.run(
                    ["sh", "-c", test_command],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                rendered_hosts.append(hosts_path.read_text(encoding="utf-8"))
            self.assertEqual(rendered_hosts[0], rendered_hosts[1])
            self.assertEqual(
                rendered_hosts[-1],
                "127.0.0.1 localhost\n"
                f"{yicloud_opensandbox.HOSTS_BLOCK_BEGIN}\n"
                "10.0.0.1 main api\n"
                "10.0.0.2 worker database\n"
                f"{yicloud_opensandbox.HOSTS_BLOCK_END}\n",
            )

    def test_bundle_healthcheck_retries_until_success(self) -> None:
        instance = make_healthcheck_environment(300)
        instance._run_service_command = AsyncMock(
            side_effect=[
                SimpleNamespace(return_code=1, stdout="", stderr="starting"),
                SimpleNamespace(return_code=0, stdout="ready", stderr=""),
            ]
        )
        sleep = AsyncMock()

        with patch.object(yicloud_opensandbox.asyncio, "sleep", sleep):
            asyncio.run(instance._wait_bundle_ready())

        self.assertEqual(instance._run_service_command.await_count, 2)
        sleep.assert_awaited_once_with(2)

    def test_bundle_healthcheck_respects_global_ready_deadline(self) -> None:
        instance = make_healthcheck_environment(3)
        instance._run_service_command = AsyncMock(
            return_value=SimpleNamespace(return_code=1, stdout="", stderr="")
        )
        clock = SimpleNamespace(value=0.0)

        async def advance(seconds: float) -> None:
            clock.value += seconds

        fake_time = SimpleNamespace(monotonic=lambda: clock.value)
        with (
            patch.object(yicloud_opensandbox, "time", fake_time),
            patch.object(
                yicloud_opensandbox.asyncio,
                "sleep",
                side_effect=advance,
            ) as sleep,
            self.assertRaisesRegex(RuntimeError, "failed.*within 3s"),
        ):
            asyncio.run(instance._wait_bundle_ready())

        self.assertEqual(
            instance._run_service_command.await_count,
            2,
        )
        self.assertEqual(
            [item.args[0] for item in sleep.await_args_list],
            [2, 1.0],
        )

    def test_composite_starts_dependency_before_dependent_health_gate(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        events = []
        entrypoints = {}
        worker = yicloud_opensandbox.ServiceRuntime(
            "worker",
            {
                "image": {"digest_ref": "seta/973@sha256:" + "b" * 64},
                "depends_on": {},
                "runtime": {
                    "start_argv": ["worker"],
                    "internal_ports": [
                        {"port": 8080, "protocol": "tcp"}
                    ],
                    "readiness": {
                        "type": "healthcheck",
                        "healthcheck": {"test": "check-worker"},
                    },
                },
            },
        )
        main = yicloud_opensandbox.ServiceRuntime(
            "main",
            {
                "image": {"digest_ref": "seta/973@sha256:" + "a" * 64},
                "depends_on": {
                    "worker": {
                        "condition": "service_healthy",
                        "required": True,
                    }
                },
                "runtime": {
                    "start_argv": ["main"],
                    "internal_ports": [],
                    "readiness": None,
                },
            },
        )

        def create_sandbox(_context, request):
            service = request.Name.rsplit("-", 1)[-1]
            events.append(f"create:{service}")
            entrypoints[service] = request.Entrypoint
            return SimpleNamespace(
                Id=f"sbx-{service}", AccessToken=f"token-{service}"
            )

        async def wait_running(runtime, _created):
            events.append(f"running:{runtime.name}")
            port = 8080 if runtime.name == "worker" else 44772
            return SimpleNamespace(
                EnvironmentId="env-test",
                Image=SimpleNamespace(
                    Ref=instance._service_image_ref(runtime)
                ),
                Endpoints=SimpleNamespace(
                    Endpoints={
                        "exec": SimpleNamespace(
                            ProxyUrl=(
                                f"https://sandbox.example/{runtime.name}/ping"
                            ),
                            InternalUrl=f"tcp://10.0.0.2:{port}",
                        )
                    }
                ),
            )

        async def run_service_command(runtime, command, **_kwargs):
            events.append(f"health:{runtime.name}:{command}")
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        instance._services = {"main": main, "worker": worker}
        instance._main_service = "main"
        instance._sandbox_service = SimpleNamespace(
            create_sandbox=create_sandbox,
            models=SimpleNamespace(
                CreateSandboxReq=Request,
                CreateSandboxReqImageInput=Request,
                CreateSandboxReqResources=Request,
                CreateSandboxReqPort=Request,
            ),
        )
        instance._project_name = "test-project"
        instance._environment_id = "env-test"
        instance._persistent_env = {}
        instance._request_cpu = "2"
        instance._request_memory = "8Gi"
        instance._lifecycle_minutes = 120
        instance._request_timeout_sec = 180
        instance._ready_timeout_sec = 300
        instance.session_id = "test-session"
        instance._wait_service_running = wait_running

        async def wire_aliases(*_args, **_kwargs):
            events.append("wire:all")

        async def release(runtime):
            events.append(f"release:{runtime.name}")

        instance._wire_service_aliases = AsyncMock(side_effect=wire_aliases)
        instance._release_service_entrypoint = AsyncMock(side_effect=release)
        instance._run_service_command = AsyncMock(
            side_effect=run_service_command
        )

        asyncio.run(instance._start_composite())

        self.assertEqual(
            events,
            [
                "create:worker",
                "running:worker",
                "create:main",
                "running:main",
                "wire:all",
                "release:worker",
                "health:worker:check-worker",
                "release:main",
            ],
        )
        self.assertEqual(instance._sandbox_id, "sbx-main")
        self.assertEqual(main.state, "READY")
        self.assertEqual(worker.state, "READY")
        self.assertEqual(entrypoints["worker"][-1], "worker")
        self.assertEqual(entrypoints["main"][-1], "main")
        self.assertIn(".harbor-compose-start-", entrypoints["worker"][2])
        self.assertIn(".harbor-compose-start-", entrypoints["main"][2])
        self.assertEqual(worker.internal_address, "10.0.0.2")
        self.assertEqual(main.internal_address, "10.0.0.2")

    def test_composite_rejects_unsupported_dependency_before_create(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._services = {
            "main": yicloud_opensandbox.ServiceRuntime(
                "main",
                {
                    "depends_on": {
                        "worker": {
                            "condition": "service_completed_successfully",
                            "required": True,
                        }
                    }
                },
            ),
            "worker": yicloud_opensandbox.ServiceRuntime(
                "worker", {"depends_on": {}}
            ),
        }
        create_sandbox = Mock()
        instance._sandbox_service = SimpleNamespace(
            create_sandbox=create_sandbox
        )

        with self.assertRaisesRegex(
            RuntimeError, "dependency condition is unsupported"
        ):
            asyncio.run(instance._start_composite())

        create_sandbox.assert_not_called()

    def test_stop_service_deletes_sidecar_and_invalidates_connection(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_id = "sbx-worker"
        runtime.command_url = "https://sandbox.example/command"
        runtime.access_token = "sandbox-token"
        runtime.internal_address = "10.0.0.2"
        runtime.state = "READY"
        instance._services = {"worker": runtime}
        instance._main_service = "main"
        instance._cleanup_wait_sec = 30
        instance.logger = Mock()
        instance._delete_single_sandbox = AsyncMock(return_value=True)

        asyncio.run(instance.stop_service("worker"))

        instance._delete_single_sandbox.assert_awaited_once_with("sbx-worker")
        self.assertEqual(runtime.state, "DELETED")
        self.assertEqual(runtime.sandbox_id, "")
        self.assertEqual(runtime.command_url, "")
        self.assertEqual(runtime.access_token, "")
        self.assertEqual(runtime.internal_address, "")

    def test_stop_service_preserves_id_when_deletion_is_unconfirmed(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        runtime = yicloud_opensandbox.ServiceRuntime("worker", {})
        runtime.sandbox_id = "sbx-worker"
        runtime.command_url = "https://sandbox.example/command"
        runtime.access_token = "sandbox-token"
        runtime.internal_address = "10.0.0.2"
        runtime.state = "READY"
        instance._services = {"worker": runtime}
        instance._main_service = "main"
        instance._cleanup_wait_sec = 30
        instance.logger = Mock()
        instance._delete_single_sandbox = AsyncMock(return_value=False)

        asyncio.run(instance.stop_service("worker"))

        self.assertEqual(runtime.state, "DELETE_UNCONFIRMED")
        self.assertEqual(runtime.sandbox_id, "sbx-worker")
        self.assertEqual(runtime.command_url, "")
        self.assertEqual(runtime.access_token, "")
        self.assertEqual(runtime.internal_address, "")
        instance.logger.warning.assert_called_once()

    def test_single_sandbox_delete_uses_dedicated_provider_api(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        delete_sandbox = Mock()
        instance._sandbox_service = SimpleNamespace(
            delete_sandbox=delete_sandbox,
            models=SimpleNamespace(DeleteSandboxReq=Request),
        )
        instance._project_name = "test-project"
        instance._wait_for_sandbox_ids_absent = AsyncMock(return_value=True)

        deleted = asyncio.run(instance._delete_single_sandbox("sbx-worker"))

        self.assertTrue(deleted)
        delete_sandbox.assert_called_once()
        request = delete_sandbox.call_args.args[1]
        self.assertEqual(request.ProjectName, "test-project")
        self.assertEqual(request.SandboxId, "sbx-worker")
        instance._wait_for_sandbox_ids_absent.assert_awaited_once_with(
            {"sbx-worker"}
        )

    def test_service_group_uses_batch_only_for_multiple_ids(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        main = yicloud_opensandbox.ServiceRuntime("main", {})
        worker = yicloud_opensandbox.ServiceRuntime("worker", {})
        main.sandbox_id = "sbx-main"
        worker.sandbox_id = "sbx-worker"
        instance._services = {"main": main, "worker": worker}
        instance._delete_single_sandbox = AsyncMock(return_value=True)
        instance._batch_delete_sandboxes = AsyncMock(return_value=True)
        instance._detach_sandbox = Mock()

        asyncio.run(instance._delete_service_group())

        instance._batch_delete_sandboxes.assert_awaited_once_with(
            {"sbx-main", "sbx-worker"}
        )
        instance._delete_single_sandbox.assert_not_awaited()

        worker.sandbox_id = ""
        instance._batch_delete_sandboxes.reset_mock()
        asyncio.run(instance._delete_service_group())

        instance._delete_single_sandbox.assert_awaited_once_with("sbx-main")
        instance._batch_delete_sandboxes.assert_not_awaited()

    def test_yicloud_image_ref_strips_only_registry_host(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            yicloud_opensandbox._yicloud_image_ref(
                f"harbor.example/seta/973@{digest}"
            ),
            f"seta/973@{digest}",
        )
        self.assertEqual(
            yicloud_opensandbox._yicloud_image_ref(f"seta/973@{digest}"),
            f"seta/973@{digest}",
        )

    def test_full_oci_ref_is_sent_via_yicloud_uri(self) -> None:
        fake_sandbox = SimpleNamespace(
            models=SimpleNamespace(CreateSandboxReqImageInput=Request)
        )
        image = yicloud_opensandbox.YiCloudOpenSandboxEnvironment._create_image_input(
            fake_sandbox,
            "harbor-sandbox.example/seta/973@sha256:" + "a" * 64,
        )
        self.assertTrue(image.Uri.startswith("harbor-sandbox.example/"))
        self.assertFalse(hasattr(image, "Ref"))

    def test_control_plane_auth_failure_is_retried(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.logger = Mock()

        class TransientAuthError(RuntimeError):
            code = 101

        operation = Mock(side_effect=[TransientAuthError("auth"), "created"])
        with patch.object(
            yicloud_opensandbox.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            result = asyncio.run(
                instance._retry_control_plane_auth(
                    "create sandbox",
                    operation,
                    "request",
                )
            )

        self.assertEqual(result, "created")
        self.assertEqual(operation.call_count, 2)
        sleep.assert_awaited_once_with(1)
        instance.logger.warning.assert_called_once()

    def test_wait_until_running_retries_transient_status_error(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        running = SimpleNamespace(
            Status=SimpleNamespace(State="Running", Reason="")
        )
        get_sandbox = Mock(
            side_effect=[RuntimeError("temporary auth failure"), running]
        )
        instance._sandbox_service = SimpleNamespace(
            get_sandbox=get_sandbox,
            models=SimpleNamespace(GetSandboxReq=Request),
        )
        instance._project_name = "test-project"
        instance._sandbox_id = "sbx-test"
        instance._ready_timeout_sec = 30
        instance._status_log_interval_sec = 30
        instance.logger = Mock()
        created = SimpleNamespace(
            Status=SimpleNamespace(State="Pending", Reason="")
        )

        with patch.object(
            yicloud_opensandbox.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            result = asyncio.run(instance._wait_until_running(created))

        self.assertIs(result, running)
        self.assertEqual(get_sandbox.call_count, 2)
        sleep.assert_awaited_once_with(1)
        instance.logger.warning.assert_called_once()

    def test_wait_until_execd_ready_retries_transient_gateway_error(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._ping_execd_sync = Mock(
            side_effect=[
                yicloud_opensandbox.requests.ConnectionError(
                    "temporary gateway failure"
                ),
                None,
            ]
        )
        instance.logger = Mock()

        with patch.object(
            yicloud_opensandbox.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            asyncio.run(instance._wait_until_execd_ready())

        self.assertEqual(instance._ping_execd_sync.call_count, 2)
        sleep.assert_awaited_once_with(3)
        instance.logger.warning.assert_called_once()

    def test_proxy_origin_can_be_replaced_without_changing_instance_path(self) -> None:
        proxy_url = (
            "https://sandbox.yicloud.com.cn/v1/sandboxes/sbx-1/proxy/44772/ping"
        )
        with patch.dict(
            "os.environ",
            {
                "YICLOUD_SANDBOX_PROXY_ORIGIN": (
                    "https://gate.yicloud.com.cn/sandbox-connect"
                )
            },
        ):
            result = yicloud_opensandbox._host_routable_proxy_url(proxy_url)

        self.assertEqual(
            result,
            "https://gate.yicloud.com.cn/sandbox-connect/v1/sandboxes/"
            "sbx-1/proxy/44772/ping",
        )

    def test_proxy_url_is_unchanged_without_an_origin_override(self) -> None:
        proxy_url = "https://sandbox.example/v1/sandboxes/sbx-1/ping"
        with patch.dict("os.environ", {}, clear=True):
            result = yicloud_opensandbox._host_routable_proxy_url(proxy_url)
        self.assertEqual(result, proxy_url)

    def test_s3_download_url_is_passed_as_environment_not_command_text(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._s3_download_timeout_sec = 1800
        instance._s3_downloader_ready = True
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )
        artifact = yicloud_opensandbox.S3UploadArtifact(
            kind="file",
            logical_digest="a" * 64,
            payload_digest="b" * 64,
            payload_size=12,
            compression="none",
            local_payload_path="/cache/payload",
            object_key="objects/payload",
            object_uri="s3://cache/objects/payload",
            signed_url="http://ceph.example/cache/object?secret=signature",
        )

        asyncio.run(
            instance._materialize_s3_file(
                artifact,
                "/tmp/agent.tgz",
                "755",
            )
        )

        call = instance.exec.await_args
        self.assertNotIn("secret=signature", call.args[0])
        self.assertIn("chmod 755", call.args[0])
        self.assertEqual(
            call.kwargs["env"]["HARBOR_S3_URL"],
            artifact.signed_url,
        )

    def test_s3_bootstrap_is_uploaded_once_only_when_native_tools_are_missing(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._s3_download_timeout_sec = 1800
        instance._s3_downloader_ready = False
        instance._s3_downloader_lock = None
        instance._sandbox_id = "sbx-test"
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._access_token = "sandbox-token"
        instance.logger = Mock()
        instance.exec = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    return_code=0, stdout="bootstrap", stderr=""
                ),
                SimpleNamespace(return_code=0, stdout="", stderr=""),
            ]
        )
        uploaded = {}

        def capture_upload(source, target_path, _upload_url):
            uploaded["count"] = uploaded.get("count", 0) + 1
            uploaded["payload"] = source.read_bytes()
            uploaded["target_path"] = target_path

        instance._upload_file_fast_sync = capture_upload

        async def ensure_twice() -> None:
            signed_url = "http://ceph.example/cache/object?signature=test"
            await instance._ensure_s3_downloader(signed_url)
            await instance._ensure_s3_downloader(signed_url)

        async def run_inline(function, *args):
            return function(*args)

        with patch.object(
            yicloud_opensandbox.asyncio,
            "to_thread",
            side_effect=run_inline,
        ):
            asyncio.run(ensure_twice())

        self.assertEqual(uploaded["count"], 1)
        self.assertEqual(
            uploaded["target_path"],
            yicloud_opensandbox.S3_HTTP_BOOTSTRAP_PATH,
        )
        self.assertIn(b"/dev/tcp/", uploaded["payload"])
        self.assertLess(len(uploaded["payload"]), 2048)
        self.assertIn(
            yicloud_opensandbox.S3_HTTP_BOOTSTRAP_PATH,
            instance._s3_download_command(
                SimpleNamespace(payload_size=12, payload_digest="b" * 64),
                "/tmp/payload",
            ),
        )

    def test_environment_and_image_bindings_are_enforced(self) -> None:
        sandbox = FakeSandbox(
            [
                SimpleNamespace(Id="env-other", Name="other"),
                SimpleNamespace(
                    Id="env-dedicated",
                    Name="dedicated-test-environment",
                ),
            ]
        )
        environment_id = yicloud_opensandbox._environment_id_by_exact_name(
            sandbox,
            "test-project",
            "dedicated-test-environment",
        )
        self.assertEqual(environment_id, "env-dedicated")

        running = SimpleNamespace(
            EnvironmentId=environment_id,
            Image=SimpleNamespace(Ref="project/task:image"),
        )
        yicloud_opensandbox._validate_sandbox_binding(
            running,
            "env-dedicated",
            "project/task:image",
        )
        running.EnvironmentId = "env-other"
        with self.assertRaisesRegex(RuntimeError, "environment binding mismatch"):
            yicloud_opensandbox._validate_sandbox_binding(
                running,
                "env-dedicated",
                "project/task:image",
            )

        running.EnvironmentId = "env-dedicated"
        running.Image = SimpleNamespace(
            Ref="", Uri="harbor.example/project/task:image"
        )
        yicloud_opensandbox._validate_sandbox_binding(
            running,
            "env-dedicated",
            "harbor.example/project/task:image",
        )
        running.Image = SimpleNamespace(Ref="project/task:image", Uri="")
        yicloud_opensandbox._validate_sandbox_binding(
            running,
            "env-dedicated",
            "harbor.example/project/task:image",
        )

    def test_external_registry_images_use_uri(self) -> None:
        self.assertEqual(
            yicloud_opensandbox._image_request_fields(
                "harbor-sandbox.example/project/task:image"
            ),
            {"Uri": "harbor-sandbox.example/project/task:image"},
        )
        self.assertEqual(
            yicloud_opensandbox._image_request_fields("project/task:image"),
            {"Ref": "project/task:image"},
        )

    def test_root_exec_payload_uses_uid_zero(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        captured = {}

        class StopAfterCapture(RuntimeError):
            pass

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                raise StopAfterCapture

        with (
            patch.object(
                yicloud_opensandbox.requests,
                "Session",
                return_value=FakeSession(),
            ),
            self.assertRaises(StopAfterCapture),
        ):
            instance._run_command_sync(
                "id -u",
                "/",
                {},
                30,
                uid=instance._resolve_exec_uid("root"),
            )

        self.assertEqual(captured["payload"]["uid"], 0)

    def test_wrapped_command_preserves_exit_code_after_exit_or_exec(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})

        class StopAfterCapture(RuntimeError):
            pass

        for command, expected_code in (
            ("exit 0", 0),
            ("exit 7", 7),
            ("exec sh -c 'exit 9'", 9),
        ):
            with self.subTest(command=command):
                captured = {}

                class FakeSession:
                    trust_env = True

                    def __init__(self, request_capture):
                        self._request_capture = request_capture

                    def post(self, _url, *, headers, data, timeout):
                        self._request_capture["payload"] = json.loads(data)
                        raise StopAfterCapture

                with (
                    patch.object(
                        yicloud_opensandbox.requests,
                        "Session",
                        return_value=FakeSession(captured),
                    ),
                    self.assertRaises(StopAfterCapture),
                ):
                    instance._run_command_sync(command, "/", {}, 30)

                completed = subprocess.run(
                    ["sh", "-c", captured["payload"]["command"]],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected_code)
                self.assertIn(
                    f"{yicloud_opensandbox.EXIT_MARKER}{expected_code}",
                    completed.stdout,
                )

    def test_exec_retries_chunked_response_with_same_idempotent_command(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        instance.logger = Mock()
        bodies = []

        class InterruptedResponse:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @property
            def text(self):
                raise yicloud_opensandbox.requests.exceptions.ChunkedEncodingError(
                    "response ended prematurely"
                )

        class SuccessResponse:
            text = (
                '{"type":"stdout","text":"ok"}\n'
                '{"type":"stdout","text":"'
                f"{yicloud_opensandbox.EXIT_MARKER}0"
                '"}\n'
            )

            @staticmethod
            def raise_for_status() -> None:
                return None

        class FakeSession:
            trust_env = True

            def __init__(self, response):
                self._response = response

            def post(self, _url, *, headers, data, timeout):
                bodies.append(data)
                return self._response

        with (
            patch.object(
                yicloud_opensandbox.requests,
                "Session",
                side_effect=[
                    FakeSession(InterruptedResponse()),
                    FakeSession(SuccessResponse()),
                ],
            ),
            patch.object(yicloud_opensandbox.time, "sleep") as sleep,
        ):
            result = instance._run_command_sync("echo ok", "/", {}, 30)

        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])
        self.assertIn("flock -x 9", json.loads(bodies[0])["command"])
        sleep.assert_called_once_with(1)

    def test_long_exec_launches_detached_and_polls_for_result(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._run_command_direct_sync = Mock(
            side_effect=[
                SimpleNamespace(return_code=0, stdout="", stderr=""),
                SimpleNamespace(
                    return_code=0,
                    stdout=yicloud_opensandbox.DETACHED_PENDING_MARKER,
                    stderr="",
                ),
                SimpleNamespace(return_code=7, stdout="done", stderr="warn"),
            ]
        )

        with patch.object(yicloud_opensandbox.time, "sleep") as sleep:
            result = instance._run_command_sync(
                "sleep 10; exit 7",
                "/work",
                {"A": "B"},
                600,
                1234,
            )

        self.assertEqual(result.return_code, 7)
        self.assertEqual(result.stdout, "done")
        self.assertEqual(result.stderr, "warn")
        calls = instance._run_command_direct_sync.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("nohup", calls[0].args[0])
        self.assertIn("command -v bash", calls[0].args[0])
        self.assertEqual(calls[0].args[1:], ("/work", {"A": "B"}, 60, 1234))
        self.assertIn("kill -0", calls[1].args[0])
        sleep.assert_called_once()

    def test_exec_without_explicit_timeout_stays_direct(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        expected = SimpleNamespace(return_code=0, stdout="ok", stderr="")
        instance._run_command_direct_sync = Mock(return_value=expected)
        instance._run_command_detached_sync = Mock()

        result = instance._run_command_sync("echo ok", "/", {}, None, 0)

        self.assertIs(result, expected)
        instance._run_command_direct_sync.assert_called_once_with(
            "echo ok", "/", {}, None, 0
        )
        instance._run_command_detached_sync.assert_not_called()

    def test_exec_uses_harbor_default_user_when_user_is_unset(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.default_user = "1234"
        instance.task_env_config = SimpleNamespace(workdir="/app")
        instance._merge_env = Mock(return_value={})
        instance._run_command_sync = Mock(
            return_value=SimpleNamespace(
                stdout="",
                stderr="",
                return_code=0,
            )
        )
        instance._output_callback = Mock(return_value=None)

        async def run_inline(function, *args):
            return function(*args)

        with patch.object(
            yicloud_opensandbox.asyncio,
            "to_thread",
            side_effect=run_inline,
        ):
            asyncio.run(instance.exec("id -u"))

        self.assertEqual(instance._run_command_sync.call_args.args[1], "/app")
        self.assertEqual(instance._run_command_sync.call_args.args[-1], 1234)

    def test_exec_omits_cwd_when_no_cwd_or_task_workdir_is_set(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance.default_user = None
        instance.task_env_config = SimpleNamespace(workdir=None)
        instance._merge_env = Mock(return_value={})
        instance._output_callback = Mock(return_value=None)
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        captured = {}

        class FakeResponse:
            text = ""

            @staticmethod
            def raise_for_status() -> None:
                return None

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                return FakeResponse()

        async def run_inline(function, *args):
            return function(*args)

        with (
            patch.object(
                yicloud_opensandbox.requests,
                "Session",
                return_value=FakeSession(),
            ),
            patch.object(
                yicloud_opensandbox.asyncio,
                "to_thread",
                side_effect=run_inline,
            ),
        ):
            result = asyncio.run(instance.exec("pwd"))

        self.assertNotIn("cwd", captured["payload"])
        self.assertEqual(result.return_code, 1)

    def test_fast_upload_keeps_access_token_out_of_argv(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._access_token = "secret-sandbox-token"
        instance.logger = Mock()
        captured = {}

        def fake_run(command, **_kwargs):
            captured["command"] = command
            header_path = Path(command[command.index("--header") + 1][1:])
            captured["headers"] = header_path.read_text(encoding="utf-8")
            metadata_form = next(
                value
                for value in command
                if value.startswith("metadata=@")
            )
            metadata_path = Path(
                metadata_form.removeprefix("metadata=@").split(";", 1)[0]
            )
            captured["metadata"] = json.loads(metadata_path.read_text())
            return SimpleNamespace(returncode=0, stdout="200", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent-package")
            source.chmod(0o755)
            with patch.object(
                yicloud_opensandbox.subprocess,
                "run",
                side_effect=fake_run,
            ):
                instance._upload_file_fast_sync(
                    source,
                    "/opt/tb-opik/agent.tgz",
                    instance._fast_upload_url(),
                )

        self.assertNotIn(
            "secret-sandbox-token",
            " ".join(captured["command"]),
        )
        self.assertIn(
            "X-Sandbox-Access-Token: secret-sandbox-token",
            captured["headers"],
        )
        self.assertEqual(captured["metadata"]["mode"], 755)

    def test_fast_upload_reuses_host_routable_execd_endpoint(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )

        with patch.dict(
            yicloud_opensandbox.os.environ,
            {"YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN": ""},
        ):
            upload_url = instance._fast_upload_url()

        self.assertEqual(
            upload_url,
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/files/upload",
        )

    def test_fast_upload_failure_without_body_reports_curl_error(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._access_token = "secret-sandbox-token"
        instance.logger = Mock()

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent-package")
            with patch.object(
                yicloud_opensandbox.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=28,
                    stdout="000",
                    stderr="connection timed out",
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "connection timed out"
                ):
                    instance._upload_file_fast_sync(
                        source,
                        "/opt/tb-opik/agent.tgz",
                        "https://gate.example/files/upload",
                    )

    def test_large_fast_upload_is_chunked_and_reassembled(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._uses_s3_upload = Mock(return_value=False)
        instance._fast_upload_url = Mock(
            return_value="https://gate.example/files/upload"
        )
        uploaded = []

        def capture_upload(content, target_path, _filename):
            uploaded.append((content, target_path))

        instance._upload_chunk_sync = capture_upload
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runtime.tgz"
            source.write_bytes(b"abcdefghi")
            source.chmod(0o755)
            with (
                patch.object(
                    yicloud_opensandbox, "FAST_UPLOAD_CHUNK_BYTES", 4
                ),
                patch.dict(
                    yicloud_opensandbox.os.environ,
                    {"YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN": ""},
                ),
            ):
                asyncio.run(
                    instance.upload_file(source, "/opt/tools/runtime.tgz")
                )

        self.assertEqual(
            [part[0] for part in uploaded], [b"abcd", b"efgh", b"i"]
        )
        self.assertEqual(len({part[1] for part in uploaded}), 3)
        commands = [call.args[0] for call in instance.exec.await_args_list]
        self.assertTrue(any("base64 -d" in command for command in commands))
        self.assertTrue(any("chmod 755" in command for command in commands))

    def test_chunked_upload_restores_source_mode(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._uses_s3_upload = Mock(return_value=False)
        instance._fast_upload_url = Mock(return_value="")
        instance._upload_chunk_sync = Mock()
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )

        async def run_inline(function, *args):
            return function(*args)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tool"
            source.write_bytes(b"#!/bin/sh\n")
            source.chmod(0o755)
            with patch.object(
                yicloud_opensandbox.asyncio,
                "to_thread",
                side_effect=run_inline,
            ):
                asyncio.run(instance.upload_file(source, "/opt/tools/tool"))

        commands = [call.args[0] for call in instance.exec.await_args_list]
        self.assertIn("chmod 755 /opt/tools/tool", commands)

    def test_delete_disconnect_does_not_replace_completed_trial(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._project_name = "test-project"
        instance._environment_id = "env-test"
        instance._sandbox_name = "trial-test"
        instance._cleanup_wait_sec = 1
        instance.logger = Mock()
        instance._detach_sandbox = Mock()
        sandbox = Mock()
        sandbox.models = SimpleNamespace(
            DeleteSandboxReq=Request,
            ListSandboxesReq=Request,
        )
        sandbox.delete_sandbox.side_effect = ConnectionError(
            "response disconnected"
        )
        # The request can have succeeded even when its response was lost.
        sandbox.list_sandboxes.return_value = SimpleNamespace(Items=[])
        instance._sandbox_service = sandbox

        asyncio.run(instance._delete_sandbox())

        sandbox.delete_sandbox.assert_called_once()
        sandbox.list_sandboxes.assert_called_once()
        instance._detach_sandbox.assert_called_once()
        instance.logger.warning.assert_called_once()

    def test_execd_upload_uses_binary_multipart_metadata(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._request_timeout_sec = 180
        instance._signed_headers = Mock(
            return_value={
                "X-OGW-SIGN": "signed",
                "X-Sandbox-Access-Token": "token",
            }
        )
        sent = {}

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

        class FakeSession:
            trust_env = True

            def prepare_request(self, request):
                return (
                    yicloud_opensandbox.requests.sessions.Session()
                    .prepare_request(request)
                )

            def send(self, prepared, timeout):
                sent["prepared"] = prepared
                sent["timeout"] = timeout
                return FakeResponse()

        with patch.object(
            yicloud_opensandbox.requests,
            "Session",
            FakeSession,
        ):
            instance._upload_chunk_sync(
                b"\x00\xffagent-package",
                "/tmp/harbor-upload.chunk",
                "agent.tgz",
            )

        prepared = sent["prepared"]
        self.assertTrue(prepared.url.endswith("/files/upload"))
        self.assertIn(
            base64.b64encode(b"\x00\xffagent-package"),
            prepared.body,
        )
        self.assertIn(
            b'name="metadata"; filename="metadata.json"',
            prepared.body,
        )
        self.assertIn(b'"mode":600', prepared.body)
        self.assertEqual(prepared.headers["X-OGW-SIGN"], "signed")

    def test_execd_upload_retries_transient_disconnect(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._request_timeout_sec = 180
        instance._signed_headers = Mock(return_value={})
        instance.logger = Mock()
        sends = []

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

        class FakeSession:
            trust_env = True

            def prepare_request(self, request):
                return (
                    yicloud_opensandbox.requests.sessions.Session()
                    .prepare_request(request)
                )

            def send(self, prepared, timeout):
                sends.append((prepared, timeout))
                if len(sends) == 1:
                    raise yicloud_opensandbox.requests.ConnectionError(
                        "response disconnected"
                    )
                return FakeResponse()

        with (
            patch.object(
                yicloud_opensandbox.requests,
                "Session",
                FakeSession,
            ),
            patch.object(yicloud_opensandbox.time, "sleep") as sleep,
        ):
            instance._upload_chunk_sync(
                b"agent-package",
                "/tmp/harbor-upload.chunk",
                "agent.tgz",
            )

        self.assertEqual(len(sends), 2)
        sleep.assert_called_once_with(1)
        instance.logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
