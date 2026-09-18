import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from opensandbox_image_manager import (
    APT_RUNTIME_ASSET_DIR,
    APT_RUNTIME_ASSET_NAMES,
    DOCKER_CONFIG,
    DOCKER_LAYER_GZIP,
    DOCKER_MANIFEST,
    OCI_CONFIG,
    OCI_LAYER_GZIP,
    RegistryTarget,
    SkopeoPublisher,
    _compose_runtime,
    _service_image_inputs,
    _service_manifest,
    apt_404_requires_cache_refresh,
    apt_gateway_root_content,
    apt_runtime_asset_digest,
    apt_runtime_secret_ids,
    base_image_lookup_ref,
    base_image_registry_host,
    check_task_repository,
    dockerfile_external_base_images,
    environment_content_hash,
    github_mirror_config_content,
    mirror_image_ref,
    normalize_base_image_registry,
    normalize_oci_image_config,
    oci_archive_image_config,
    package_source_build_args,
    package_source_hosts,
    parse_args,
    prepare,
    prepare_bundle,
    proxy_build_args,
    render_build_dockerfile,
    resolve_base_image_contexts,
    run_build,
    schema2_manifest,
    validate_download_source_url,
    validate_github_mirror_url,
)


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def add_tar_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


class OpenSandboxImageManagerTest(unittest.TestCase):
    def make_task(self, root: Path, name: str = "0") -> Path:
        task = root / name
        environment = task / "environment"
        environment.mkdir(parents=True)
        (task / "task.toml").write_text(
            "[environment]\nbuild_timeout_sec = 60\n", encoding="utf-8"
        )
        (environment / "Dockerfile").write_text(
            "FROM ubuntu:24.04\nRUN echo ok\n", encoding="utf-8"
        )
        return task

    def test_cli_accepts_prebuild_timeout_override(self) -> None:
        args = parse_args(
            [
                "--task-dir",
                "/tmp/example-task",
                "--project",
                "test-project",
                "--build-timeout-sec",
                "7200",
                "--retry-no-cache-on-apt-404",
                "--dry-run",
            ]
        )

        self.assertEqual(args.build_timeout_sec, 7200.0)
        self.assertTrue(args.retry_no_cache_on_apt_404)

    def test_cli_accepts_single_base_image_registry(self) -> None:
        args = parse_args(
            [
                "--task-dir",
                "/tmp/example-task",
                "--project",
                "test-project",
                "--base-image-registry",
                "registry.example/base/",
                "--dry-run",
            ]
        )

        self.assertEqual(args.base_image_registry, "registry.example/base/")
        self.assertEqual(
            normalize_base_image_registry(args.base_image_registry),
            "registry.example/base",
        )

    def test_cli_defaults_base_image_registry_to_empty(self) -> None:
        args = parse_args(
            [
                "--task-dir",
                "/tmp/example-task",
                "--project",
                "test-project",
                "--dry-run",
            ]
        )

        self.assertEqual(args.base_image_registry, "")

    def test_skip_hash_verification_requires_local_upload_cache(self) -> None:
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-dir",
                    "/tmp/example-task",
                    "--project",
                    "test-project",
                    "--skip-hash-verification",
                ]
            )

    def test_cli_enables_host_proxy_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            args = parse_args(
                [
                    "--task-dir",
                    "/tmp/example-task",
                    "--registry",
                    "harbor.example.internal",
                    "--project",
                    "test-project",
                    "--dry-run",
                ]
            )

        self.assertTrue(args.use_proxy)
        self.assertEqual(
            args.registry, "harbor.example.internal"
        )
        self.assertEqual(args.build_network, "host")
        self.assertEqual(
            args.pip_index_url, "https://pypi.tuna.tsinghua.edu.cn/simple"
        )
        self.assertEqual(args.npm_registry, "https://registry.npmmirror.com")
        self.assertEqual(args.goproxy, "https://goproxy.cn,direct")
        self.assertEqual(args.gosumdb, "sum.golang.google.cn")
        self.assertEqual(
            args.cargo_registry_url,
            "sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/",
        )
        self.assertEqual(args.pub_hosted_url, "")
        self.assertEqual(args.julia_pkg_server, "")

    def test_cli_reads_dart_and_julia_sources_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HARBOR_OPENSANDBOX_PUB_HOSTED_URL": (
                    "https://third-party.example/dart/pub"
                ),
                "HARBOR_OPENSANDBOX_JULIA_PKG_SERVER": (
                    "https://third-party.example/julia/pkg"
                ),
            },
            clear=True,
        ):
            args = parse_args(
                [
                    "--task-dir",
                    "/tmp/example-task",
                    "--project",
                    "test-project",
                    "--dry-run",
                ]
            )

        self.assertEqual(
            args.pub_hosted_url, "https://third-party.example/dart/pub"
        )
        self.assertEqual(
            args.julia_pkg_server, "https://third-party.example/julia/pkg"
        )

    def test_cli_reads_download_source_from_environment(self) -> None:
        source = "http://third-party-source.internal/v1/cache"
        with patch.dict(
            os.environ,
            {"HARBOR_OPENSANDBOX_DOWNLOAD_SOURCE_URL": source},
            clear=True,
        ):
            args = parse_args(
                [
                    "--task-dir",
                    "/tmp/example-task",
                    "--project",
                    "test-project",
                    "--dry-run",
                ]
            )

        self.assertEqual(args.download_source_url, source)

    def test_download_source_root_is_provider_neutral(self) -> None:
        self.assertEqual(
            validate_download_source_url(
                "https://third-party-source.example/cache/", "host"
            ),
            "https://third-party-source.example/cache",
        )

    def test_prepare_requires_registry_from_cli_or_environment(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "--registry or YICLOUD_HARBOR_HOST is required",
        ):
            prepare_bundle(Namespace(platform="linux/amd64", registry=""))
        for registry in (
            "https://harbor.example",
            "harbor.example/project",
        ):
            with self.subTest(registry=registry), self.assertRaisesRegex(
                ValueError,
                "bare OCI registry host",
            ):
                prepare_bundle(Namespace(registry=registry))

    def test_prepare_rejects_unimplemented_platform_before_other_work(self) -> None:
        with self.assertRaisesRegex(
            NotImplementedError,
            "platform 'linux/arm64' is not implemented",
        ):
            prepare_bundle(Namespace(platform="linux/arm64"))

    def test_loopback_proxy_requires_host_build_network(self) -> None:
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:7890"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "build-network=host"):
                proxy_build_args(True)
            proxy_args = proxy_build_args(True, "host")

        self.assertEqual(proxy_args["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(proxy_args["https_proxy"], "http://127.0.0.1:7890")

    def test_configured_proxy_takes_precedence_over_shell_proxy(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HARBOR_OPENSANDBOX_BUILD_PROXY_URL": "http://127.0.0.1:7890",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
            },
            clear=True,
        ):
            proxy_args = proxy_build_args(True, "host")

        self.assertEqual(proxy_args["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(proxy_args["HTTPS_PROXY"], "http://127.0.0.1:7890")

    def test_apt_404_cache_refresh_requires_failed_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "build.log"
            log_path.write_text(
                "404  Not Found\nE: Failed to fetch package.deb\n",
                encoding="utf-8",
            )
            self.assertTrue(apt_404_requires_cache_refresh(log_path))

            log_path.write_text(
                "timed out building task image after 600s\n", encoding="utf-8"
            )
            self.assertFalse(apt_404_requires_cache_refresh(log_path))

    def test_content_hash_is_stable_and_ignores_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            environment = self.make_task(Path(tmp)) / "environment"
            first = environment_content_hash(environment)
            (environment / "__pycache__").mkdir()
            (environment / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
            self.assertEqual(first, environment_content_hash(environment))
            (environment / "payload.txt").write_text("changed", encoding="utf-8")
            self.assertNotEqual(first, environment_content_hash(environment))

    def test_build_service_identity_excludes_renderer_and_runtime_assets(
        self,
    ) -> None:
        # P0 regression guard: only the original static task environment may
        # influence image identity; renderer/runtime mutations must not.
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp))
            environment = task / "environment"
            from compose_bundle import resolve_bundle_spec

            bundle = resolve_bundle_spec(task)
            service = bundle.services["main"]
            expected = environment_content_hash(environment, truncate=64)

            def service_identity() -> str:
                return _service_image_inputs(
                    service,
                    bundle=bundle,
                    dockerhub_mirror_prefix="",
                    package_build_args={},
                    platform="linux/amd64",
                    explicit_build_args={},
                    dry_run=True,
                )[0]

            first = service_identity()
            with patch(
                "opensandbox_image_manager.apt_runtime_asset_digest",
                return_value="changed-runtime-assets",
            ):
                second = service_identity()
            with patch(
                "opensandbox_image_manager.render_build_dockerfile",
                return_value="different generated Dockerfile",
            ):
                third = service_identity()

        self.assertEqual(first, expected)
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_runtime_asset_content_changes_digest_and_secret_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asset_dir = Path(tmp)
            for name in APT_RUNTIME_ASSET_NAMES:
                (asset_dir / name).write_text(f"{name}:first\n", encoding="utf-8")
            with patch(
                "opensandbox_image_manager.APT_RUNTIME_ASSET_DIR", asset_dir
            ):
                first_digest = apt_runtime_asset_digest()
                first_secret_ids = apt_runtime_secret_ids()
                (asset_dir / "apt-wrapper.sh").write_text(
                    "apt-wrapper.sh:second\n", encoding="utf-8"
                )
                second_digest = apt_runtime_asset_digest()
                second_secret_ids = apt_runtime_secret_ids()

        self.assertNotEqual(first_digest, second_digest)
        self.assertNotEqual(first_secret_ids, second_secret_ids)

    def test_oci_config_normalizes_process_ports_and_healthcheck(self) -> None:
        config = normalize_oci_image_config(
            {
                "config": {
                    "Entrypoint": ["/entry"],
                    "Cmd": ["--serve"],
                    "WorkingDir": "/app",
                    "ExposedPorts": {"8080/tcp": {}, "22/tcp": {}},
                    "Healthcheck": {"Test": ["CMD", "true"]},
                }
            }
        )
        self.assertEqual(config["entrypoint"], ["/entry"])
        self.assertEqual(config["cmd"], ["--serve"])
        self.assertEqual(config["working_dir"], "/app")
        self.assertEqual(
            config["exposed_ports"],
            [{"port": 22, "protocol": "tcp"}, {"port": 8080, "protocol": "tcp"}],
        )
        self.assertEqual(config["healthcheck"], {"test": ["CMD", "true"]})

    def test_task_973_runtime_uses_compose_command_and_oci_worker_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self.make_task(root, "973")
            environment = task / "environment"
            (environment / "Dockerfile.worker").write_text("FROM alpine:3.20\n", encoding="utf-8")
            (environment / "docker-compose.yaml").write_text(
                """
services:
  main:
    build: .
    command: [sh, -c, 'sleep infinity']
    working_dir: /workspace
  worker:
    build:
      dockerfile: Dockerfile.worker
""",
                encoding="utf-8",
            )
            from compose_bundle import resolve_bundle_spec

            bundle = resolve_bundle_spec(task)
        main = _compose_runtime(
            bundle.services["main"],
            {
                "entrypoint": None,
                "cmd": None,
                "working_dir": "/image-main",
                "exposed_ports": [],
                "healthcheck": None,
            },
            benchmark="seta",
            task_identity="973",
        )
        worker = _compose_runtime(
            bundle.services["worker"],
            {
                "entrypoint": None,
                "cmd": ["/usr/sbin/sshd", "-D"],
                "working_dir": "/opt/worker",
                "exposed_ports": [{"port": 22, "protocol": "tcp"}],
                "healthcheck": None,
            },
            benchmark="seta",
            task_identity="973",
        )
        self.assertEqual(main["start_argv"], ["sh", "-c", "sleep infinity"])
        self.assertEqual(main["start_argv_source"], "compose.command")
        self.assertEqual(main["workdir"], "/workspace")
        self.assertEqual(main["workdir_source"], "compose.working_dir")
        self.assertEqual(worker["start_argv"], ["/usr/sbin/sshd", "-D"])
        self.assertEqual(worker["start_argv_source"], "image-config.cmd")
        self.assertEqual(worker["workdir"], "/opt/worker")
        self.assertEqual(worker["workdir_source"], "image-config.working-dir")
        self.assertEqual(
            worker["internal_ports"],
            [{"port": 22, "protocol": "tcp", "source": "image-config.exposed-ports"}],
        )
        self.assertEqual(worker["readiness"]["source"], "adapter-metadata:seta/973/worker")

    def test_compose_scalar_command_is_appended_as_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), "scalar-command")
            (task / "environment" / "docker-compose.yaml").write_text(
                "services:\n  main:\n    build: .\n    command: --message 'hello world'\n",
                encoding="utf-8",
            )
            from compose_bundle import resolve_bundle_spec

            service = resolve_bundle_spec(task).services["main"]

        runtime = _compose_runtime(
            service,
            {
                "entrypoint": ["python", "server.py"],
                "cmd": ["--message", "default"],
                "exposed_ports": [],
                "healthcheck": None,
            },
            benchmark="seta",
            task_identity="scalar-command",
        )

        self.assertEqual(
            runtime["start_argv"],
            ["python", "server.py", "--message", "hello world"],
        )
        self.assertEqual(
            runtime["start_argv_source"],
            "image-config.entrypoint+compose.command",
        )

    def test_implicit_dockerfile_overrides_image_cmd_with_keepalive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), "commandless")
            from compose_bundle import resolve_bundle_spec

            bundle = resolve_bundle_spec(task)
            service = bundle.services["main"]
            artifact = {
                "config": {
                    "entrypoint": None,
                    "cmd": ["python3"],
                    "exposed_ports": [],
                    "healthcheck": None,
                },
                "config_resolved": True,
                "build_arg_names": [],
            }
            implicit = _service_manifest(
                service,
                artifact,
                bundle.environment_dir,
                benchmark="seta",
                task_identity="commandless",
                definition_kind="dockerfile",
            )
            explicit_compose = _service_manifest(
                service,
                artifact,
                bundle.environment_dir,
                benchmark="seta",
                task_identity="commandless",
                definition_kind="compose",
            )

        self.assertEqual(
            implicit["runtime"]["start_argv"],
            ["sh", "-c", "while :; do sleep 60; done"],
        )
        self.assertEqual(
            implicit["runtime"]["start_argv_source"],
            "adapter.legacy-keepalive",
        )
        self.assertEqual(explicit_compose["runtime"]["start_argv"], ["python3"])
        self.assertEqual(
            explicit_compose["runtime"]["start_argv_source"],
            "image-config.cmd",
        )

    def test_compose_dry_run_writes_versioned_bundle_and_keeps_main_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self.make_task(root, "973")
            environment = task / "environment"
            (environment / "Dockerfile.worker").write_text(
                "FROM alpine:3.20\n", encoding="utf-8"
            )
            (environment / "docker-compose.yaml").write_text(
                """
services:
  main:
    build:
      context: ${CONTEXT_DIR}
    depends_on: [worker]
  worker:
    build:
      dockerfile: Dockerfile.worker
networks:
  default:
    driver: bridge
""",
                encoding="utf-8",
            )
            manifest_path = root / "runtime" / "bundle.json"
            args = Namespace(
                task_dir=task,
                dataset_root=None,
                include="",
                registry="harbor.example.internal",
                project="test-project",
                task_repository="",
                benchmark_name="seta",
                docker_config=root / "missing-config.json",
                cache_root=root / "cache",
                platform="linux/amd64",
                tag_prefix="harbor",
                dockerhub_mirror_prefix="m.daocloud.io/docker.io",
                build_args_json="{}",
                bundle_manifest_output=manifest_path,
                force=False,
                registry_tls_verify=False,
                dry_run=True,
            )
            prepared = prepare_bundle(args)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(prepared.manifest_path, manifest_path)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["main"], "main")
        self.assertEqual(set(manifest["services"]), {"main", "worker"})
        self.assertEqual(
            prepared.main_image_ref,
            manifest["services"]["main"]["image"]["digest_ref"],
        )
        self.assertRegex(
            prepared.main_image_ref,
            r"^harbor\.example\.internal/test-project/973@sha256:[0-9a-f]{64}$",
        )
        self.assertTrue(manifest["requirements"]["multi_service"])

    def test_render_preserves_run_and_stage_alias(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04 AS builder\n"
                "RUN apt-get update\n"
                "FROM builder\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            package_build_args={
                "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
                "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
            },
        )

        self.assertIn(
            "FROM m.daocloud.io/docker.io/library/ubuntu:24.04 AS builder",
            rendered,
        )
        self.assertIn(
            "RUN apt-get update",
            rendered,
        )
        self.assertNotIn("/run/opensandbox-apt", rendered)
        self.assertNotIn("target=/usr/bin/apt-get", rendered)
        self.assertNotIn("type=bind", rendered)
        self.assertIn("FROM builder\n", rendered)
        self.assertNotIn("docker.io/library/builder", rendered)
        self.assertEqual(rendered.count("ARG NPM_CONFIG_REGISTRY"), 2)
        self.assertEqual(rendered.count("ARG PIP_INDEX_URL"), 2)

    def test_logical_base_image_uses_immutable_named_context(self) -> None:
        source = (
            "FROM --platform=linux/amd64 go_1.19.13 AS builder\n"
            "RUN cat <<'EOF' >/tmp/example\n"
            "FROM ignored_1.0\n"
            "EOF\n"
            "FROM builder\n"
        )
        with patch(
            "opensandbox_image_manager.inspect_external_image",
            return_value=(
                "registry.example/base/go_1.19.13",
                "sha256:" + "a" * 64,
            ),
        ) as inspect:
            replacements, contexts = resolve_base_image_contexts(
                source,
                "registry.example/base/",
                "linux/amd64",
            )

        self.assertEqual(dockerfile_external_base_images(source), ("go_1.19.13",))
        self.assertEqual(
            base_image_registry_host("registry.example/base"), "registry.example"
        )
        inspect.assert_called_once_with(
            "registry.example/base/go_1.19.13",
            dockerhub_mirror_prefix="",
            platform="linux/amd64",
            dry_run=False,
            direct_host="registry.example",
        )
        context_name = replacements["go_1.19.13"]
        self.assertRegex(context_name, r"^opensandbox-base-[0-9a-f]{20}$")
        self.assertEqual(
            contexts[context_name],
            "docker-image://registry.example/base/go_1.19.13@sha256:"
            + "a" * 64,
        )

        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            base_image_replacements=replacements,
        )

        self.assertIn(
            f"FROM --platform=linux/amd64 {context_name} AS builder", rendered
        )
        self.assertIn("FROM ignored_1.0\n", rendered)
        self.assertIn("FROM builder\n", rendered)
        self.assertNotIn("m.daocloud.io/docker.io/library/go_1.19.13", rendered)

    def test_base_image_lookup_normalization(self) -> None:
        cases = {
            "go_1.19.13": "registry.example/base/go_1.19.13",
            "ubuntu:22.04": "registry.example/base/ubuntu:22.04",
            "docker.io/library/ubuntu:22.04": (
                "registry.example/base/ubuntu:22.04"
            ),
            "index.docker.io/library/ubuntu:22.04": (
                "registry.example/base/ubuntu:22.04"
            ),
            "docker.io/rocker/r-ver:4.4.1": (
                "registry.example/base/rocker/r-ver:4.4.1"
            ),
            "rocker/r-ver:4.4.1": "registry.example/base/rocker/r-ver:4.4.1",
        }
        for image, expected in cases.items():
            with self.subTest(image=image):
                self.assertEqual(
                    base_image_lookup_ref(image, "registry.example/base"),
                    expected,
                )

    def test_docker_io_qualified_from_enters_resolution_channel(self) -> None:
        source = (
            "FROM ubuntu:22.04\n"
            "FROM docker.io/library/ubuntu:22.04\n"
            "FROM docker.io/rocker/r-ver:4.4.1\n"
            "FROM rocker/r-ver:4.4.1 AS rstage\n"
            "FROM mcr.microsoft.com/dotnet/sdk:8.0\n"
            "FROM scratch\n"
            "FROM $BASE_IMAGE\n"
            "FROM rstage\n"
        )

        self.assertEqual(
            dockerfile_external_base_images(source),
            ("ubuntu:22.04", "rocker/r-ver:4.4.1"),
        )
        self.assertEqual(
            dockerfile_external_base_images(source, include_docker_io=True),
            (
                "ubuntu:22.04",
                "docker.io/library/ubuntu:22.04",
                "docker.io/rocker/r-ver:4.4.1",
                "rocker/r-ver:4.4.1",
            ),
        )

        inspected: list[str] = []

        def fake_inspect(image_ref: str, **kwargs: object) -> tuple[str, str]:
            inspected.append(image_ref)
            return image_ref, "sha256:" + "b" * 64

        with patch(
            "opensandbox_image_manager.inspect_external_image",
            side_effect=fake_inspect,
        ):
            replacements, contexts = resolve_base_image_contexts(
                source,
                "registry.example/base",
                "linux/amd64",
            )

        self.assertEqual(
            inspected,
            [
                "registry.example/base/ubuntu:22.04",
                "registry.example/base/ubuntu:22.04",
                "registry.example/base/rocker/r-ver:4.4.1",
                "registry.example/base/rocker/r-ver:4.4.1",
            ],
        )
        self.assertEqual(
            sorted(replacements),
            [
                "docker.io/library/ubuntu:22.04",
                "docker.io/rocker/r-ver:4.4.1",
                "rocker/r-ver:4.4.1",
                "ubuntu:22.04",
            ],
        )
        self.assertNotEqual(
            replacements["ubuntu:22.04"],
            replacements["docker.io/library/ubuntu:22.04"],
        )
        self.assertEqual(len(contexts), 4)
        for context_ref in contexts.values():
            self.assertTrue(context_ref.startswith("docker-image://"))
            self.assertIn("@sha256:" + "b" * 64, context_ref)

        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            base_image_replacements=replacements,
        )

        self.assertIn(f"FROM {replacements['ubuntu:22.04']}\n", rendered)
        self.assertIn(
            f"FROM {replacements['docker.io/library/ubuntu:22.04']}\n", rendered
        )
        self.assertIn("FROM mcr.microsoft.com/dotnet/sdk:8.0\n", rendered)
        self.assertIn("FROM m.daocloud.io/docker.io/library/scratch\n", rendered)
        self.assertIn("FROM $BASE_IMAGE\n", rendered)
        self.assertIn("FROM rstage\n", rendered)

    def test_missing_base_image_fails_without_fallback(self) -> None:
        source = "FROM go_1.19.13\n"
        with (
            patch(
                "opensandbox_image_manager.inspect_external_image",
                side_effect=RuntimeError(
                    "failed to inspect external image "
                    "'registry.example/base/go_1.19.13'"
                ),
            ),
            self.assertRaises(RuntimeError),
        ):
            resolve_base_image_contexts(
                source,
                "registry.example/base",
                "linux/amd64",
            )

    def test_empty_base_image_registry_keeps_mirror_behavior(self) -> None:
        source = (
            "FROM ubuntu:22.04\n"
            "FROM go_1.19.13 AS builder\n"
            "FROM docker.io/library/golang:1.22\n"
        )

        replacements, contexts = resolve_base_image_contexts(
            source,
            "",
            "linux/amd64",
        )

        self.assertEqual(replacements, {})
        self.assertEqual(contexts, {})

        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            "FROM m.daocloud.io/docker.io/library/ubuntu:22.04\n", rendered
        )
        self.assertIn(
            "FROM m.daocloud.io/docker.io/library/go_1.19.13 AS builder\n",
            rendered,
        )
        self.assertIn("FROM m.daocloud.io/docker.io/library/golang:1.22\n", rendered)

    def test_http_pip_index_is_explicitly_trusted(self) -> None:
        http_args = package_source_build_args(
            Namespace(pip_index_url="http://packages.internal:8080/simple"),
            "host",
        )
        https_args = package_source_build_args(
            Namespace(pip_index_url="https://pypi.tuna.tsinghua.edu.cn/simple"),
            "host",
        )

        self.assertEqual(http_args["PIP_TRUSTED_HOST"], "packages.internal")
        self.assertNotIn("PIP_TRUSTED_HOST", https_args)

    def test_dart_and_julia_sources_accept_provider_neutral_urls(self) -> None:
        build_args = package_source_build_args(
            Namespace(
                pub_hosted_url="https://third-party.example/dart/pub",
                julia_pkg_server="http://cache.internal:8080/julia/pkg",
            ),
            "host",
        )
        rendered = render_build_dockerfile(
            "FROM dart:stable\nRUN dart pub get\n"
            "FROM julia:1\nRUN julia -e 'using Pkg; Pkg.instantiate()'\n",
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            package_build_args=build_args,
        )

        self.assertEqual(
            build_args["PUB_HOSTED_URL"],
            "https://third-party.example/dart/pub",
        )
        self.assertEqual(
            build_args["JULIA_PKG_SERVER"],
            "http://cache.internal:8080/julia/pkg",
        )
        self.assertEqual(rendered.count("ARG PUB_HOSTED_URL"), 2)
        self.assertEqual(rendered.count("ARG JULIA_PKG_SERVER"), 2)
        self.assertNotIn("ENV PUB_HOSTED_URL", rendered)
        self.assertNotIn("ENV JULIA_PKG_SERVER", rendered)
        self.assertEqual(
            package_source_hosts(build_args),
            {
                "cache.internal",
                "goproxy.cn",
                "mirrors.tuna.tsinghua.edu.cn",
                "pypi.tuna.tsinghua.edu.cn",
                "registry.npmmirror.com",
                "sum.golang.google.cn",
                "third-party.example",
            },
        )

    def test_dart_and_julia_sources_are_omitted_when_unconfigured(self) -> None:
        build_args = package_source_build_args(Namespace(), "host")

        self.assertNotIn("PUB_HOSTED_URL", build_args)
        self.assertNotIn("JULIA_PKG_SERVER", build_args)

    def test_dart_and_julia_sources_reject_embedded_credentials(self) -> None:
        for field, value in (
            ("pub_hosted_url", "https://user:secret@packages.example/dart"),
            ("julia_pkg_server", "https://user:secret@packages.example/julia"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "without credentials"
            ):
                package_source_build_args(Namespace(**{field: value}), "host")

    def test_github_mirror_config_is_not_injected_by_renderer(self) -> None:
        mirror = validate_github_mirror_url(
            "http://github-mirror.internal:8080/repos", "host"
        )
        config = github_mirror_config_content(mirror)
        rendered = render_build_dockerfile(
            "FROM ubuntu:24.04 AS builder\nRUN git submodule update --init --recursive\nFROM builder\n",
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertEqual(mirror, "http://github-mirror.internal:8080/repos/")
        self.assertIn("insteadOf = https://github.com/", config)
        self.assertIn("insteadOf = git@github.com:", config)
        self.assertIn("RUN git submodule update --init --recursive", rendered)
        self.assertNotIn("opensandbox-github-mirror-gitconfig", rendered)
        self.assertNotIn("/run/opensandbox-apt", rendered)
        self.assertNotIn(mirror, rendered)

    def test_renderer_preserves_exec_form_for_frontend_runtime(
        self,
    ) -> None:
        source = (
            "FROM ubuntu:24.04\n"
            'RUN ["git", "clone", "https://github.com/foo/bar.git"]\n'
        )
        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            '["git", "clone", "https://github.com/foo/bar.git"]',
            rendered,
        )
        self.assertNotIn("/etc/gitconfig", rendered)
        self.assertNotIn("run-with-apt-path", rendered)
        self.assertNotIn("/run/opensandbox-apt/bin/apt", rendered)
        self.assertNotIn("target=/run/opensandbox-apt/shadow", rendered)

    def test_exec_form_has_no_gitconfig_mount_when_mirror_is_unconfigured(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                'RUN ["git", "clone", "https://github.com/foo/bar.git"]\n'
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            'RUN ["git", "clone", "https://github.com/foo/bar.git"]',
            rendered,
        )
        self.assertNotIn("/etc/gitconfig", rendered)

    def test_dockerhub_mirror_matches_registry_component_exactly(self) -> None:
        mirror = "mirror.example/docker.io"

        self.assertEqual(
            mirror_image_ref("docker.io/library/python:3.13", mirror, set()),
            "mirror.example/docker.io/library/python:3.13",
        )
        self.assertEqual(
            mirror_image_ref("docker.io.evil/library/python:3.13", mirror, set()),
            "docker.io.evil/library/python:3.13",
        )

    def test_github_mirror_accepts_provider_neutral_prefix(self) -> None:
        self.assertEqual(
            validate_github_mirror_url(
                "https://mirror.example.internal/custom/github", "host"
            ),
            "https://mirror.example.internal/custom/github/",
        )

    def test_github_mirror_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "without credentials"):
            validate_github_mirror_url(
                "https://user:password@mirror.example.internal/github", "host"
            )

    def rewrite_apt_source(
        self,
        source: str,
        source_kind: str,
        dynamic_gateway_root: str = "",
        auth_conf: str = "",
    ) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / f"source.{source_kind}"
            gateway_path = root / "gateway-root"
            seen_path = root / "seen"
            source_path.write_text(source, encoding="utf-8")
            gateway_path.write_text(
                apt_gateway_root_content(dynamic_gateway_root),
                encoding="utf-8",
            )
            seen_path.touch()
            completed = subprocess.run(
                [
                    "awk",
                    "-v",
                    f"gateway_file={gateway_path}",
                    "-v",
                    f"seen_file={seen_path}",
                    "-v",
                    f"source_kind={source_kind}",
                    "-v",
                    "display_file=/etc/apt/sources.list.d/test",
                    "-v",
                    f"auth_conf={auth_conf}",
                    "-f",
                    str(APT_RUNTIME_ASSET_DIR / "source-rewriter.awk"),
                    str(source_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
        return completed.stdout, completed.stderr

    def test_manager_supports_package_import(self) -> None:
        # A new process prevents this suite's top-level sys.path entry from
        # masking a broken package-relative import.
        repo = HARBOR_DIR.parents[3]
        completed = subprocess.run(
            [sys.executable, "-I", "-c",
             (f"import sys; sys.path.insert(0, {str(repo)!r}); "
              "from Agents.utils.common.Harbor import opensandbox_image_manager")],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_runtime_deb822_derives_routes_and_skips_disabled_stanza(self) -> None:
        source = (
            "Types: deb\n"
            "URIs: https://one.example/repo https://two.example/base/child "
            "file:/srv/packages\n"
            "Suites: noble noble-updates\n"
            "Components: main universe\n"
            "Signed-By: /keys/example.gpg\n"
            "\n"
            "Types: deb\n"
            "URIs: https://disabled.example/repo\n"
            "Enabled: no\n"
        )
        rewritten, warnings = self.rewrite_apt_source(
            source,
            "sources",
            "http://gateway/v1/cache",
        )
        one = (
            "http://gateway/v1/cache/apt/v1/https/"
            f"{b'one.example'.hex()}/base/{b'/repo'.hex()}"
        )
        two = (
            "http://gateway/v1/cache/apt/v1/https/"
            f"{b'two.example'.hex()}/base/"
            f"{b'/base/child'.hex()}"
        )
        self.assertIn(f"URIs: {one} {two} file:/srv/packages", rewritten)
        self.assertIn("Suites: noble noble-updates", rewritten)
        self.assertIn("Signed-By: /keys/example.gpg", rewritten)
        self.assertIn("file:/srv/packages", rewritten)
        self.assertNotIn("source=file:", warnings)
        self.assertIn("URIs: https://disabled.example/repo", rewritten)
        self.assertNotIn("disabled.example", warnings)

    def test_runtime_derives_dynamic_gateway_route_for_unmapped_apt_source(
        self,
    ) -> None:
        source = (
            "deb https://repo.example:8443/linux/ubuntu/ noble main\n"
            "deb HTTPS://UPPER.example/repo stable main\n"
            "deb http://gateway/v1/cache/debian trixie main\n"
        )
        rewritten, warnings = self.rewrite_apt_source(
            source,
            "list",
            "http://gateway/v1/cache",
        )
        expected = (
            "http://gateway/v1/cache/apt/v1/https/"
            f"{b'repo.example:8443'.hex()}/base/"
            f"{b'/linux/ubuntu'.hex()}"
        )
        self.assertIn(f"deb {expected} noble main", rewritten)
        uppercase = (
            "http://gateway/v1/cache/apt/v1/https/"
            f"{b'upper.example'.hex()}/base/{b'/repo'.hex()}"
        )
        self.assertIn(f"deb {uppercase} stable main", rewritten)
        self.assertIn(
            "deb http://gateway/v1/cache/debian trixie main",
            rewritten,
        )
        self.assertEqual(warnings, "")

    def test_runtime_preserves_authored_apt_source_without_gateway(self) -> None:
        source = (
            "deb http://archive.ubuntu.com/ubuntu noble main\n"
            "deb https://deb.debian.org/debian-security trixie-security main\n"
        )
        rewritten, warnings = self.rewrite_apt_source(source, "list")
        self.assertIn(
            "deb http://archive.ubuntu.com/ubuntu noble main", rewritten
        )
        self.assertIn(
            "deb https://deb.debian.org/debian-security trixie-security main",
            rewritten,
        )
        self.assertIn("event=source-bypass", warnings)
        self.assertIn("source=http://archive.ubuntu.com/ubuntu", warnings)

    def test_runtime_preserves_loopback_apt_source_with_gateway(self) -> None:
        source = (
            "deb http://127.0.0.1:3142/ubuntu noble main\n"
            "deb http://localhost:8080/repo stable main\n"
        )
        rewritten, warnings = self.rewrite_apt_source(
            source, "list", "http://gateway/v1/cache"
        )
        self.assertIn(
            "deb http://127.0.0.1:3142/ubuntu noble main", rewritten
        )
        self.assertIn("deb http://localhost:8080/repo stable main", rewritten)
        self.assertIn("event=source-bypass", warnings)
        self.assertIn("source=http://127.0.0.1:3142/ubuntu", warnings)

    def test_runtime_preserves_non_public_apt_source_with_gateway(self) -> None:
        source = (
            "deb http://10.0.0.5/ubuntu noble main\n"
            "deb http://repo.corp.internal/debian stable main\n"
        )
        rewritten, warnings = self.rewrite_apt_source(
            source, "list", "http://gateway/v1/cache"
        )
        self.assertIn("deb http://10.0.0.5/ubuntu noble main", rewritten)
        self.assertIn(
            "deb http://repo.corp.internal/debian stable main", rewritten
        )
        self.assertIn("event=source-bypass", warnings)
        self.assertIn("source=http://10.0.0.5/ubuntu", warnings)

    def test_runtime_preserves_all_sources_when_auth_conf_exists(self) -> None:
        source = "deb http://archive.ubuntu.com/ubuntu noble main\n"
        rewritten, warnings = self.rewrite_apt_source(
            source, "list", "http://gateway/v1/cache", auth_conf="1"
        )
        self.assertIn(
            "deb http://archive.ubuntu.com/ubuntu noble main", rewritten
        )
        self.assertIn("event=source-bypass", warnings)
        self.assertIn("source=http://archive.ubuntu.com/ubuntu", warnings)

    def test_runtime_warning_redacts_credentials_from_list_source(self) -> None:
        source = (
            "deb https://ci-user:FAKE_LIST_TOKEN@repo.invalid/private "
            "noble main\n"
        )

        rewritten, warnings = self.rewrite_apt_source(
            source, "list", "http://gateway/v1/cache"
        )

        self.assertIn(
            "deb https://ci-user:FAKE_LIST_TOKEN@repo.invalid/private",
            rewritten,
        )
        self.assertIn("source=https://repo.invalid/private", warnings)
        self.assertNotIn("FAKE_LIST_TOKEN", warnings)
        self.assertNotIn("ci-user", warnings)

    def test_runtime_warning_redacts_userinfo_through_last_at(self) -> None:
        source = (
            "deb https://ci-user:FAKE_TOKEN@SECRET_SUFFIX@repo.invalid/private "
            "noble main\n"
        )

        rewritten, warnings = self.rewrite_apt_source(
            source, "list", "http://gateway/v1/cache"
        )

        self.assertIn(
            "https://ci-user:FAKE_TOKEN@SECRET_SUFFIX@repo.invalid/private",
            rewritten,
        )
        self.assertIn("source=https://repo.invalid/private", warnings)
        self.assertNotIn("FAKE_TOKEN", warnings)
        self.assertNotIn("SECRET_SUFFIX", warnings)
        self.assertNotIn("ci-user", warnings)

    def test_runtime_warning_redacts_query_token_from_deb822_source(self) -> None:
        source = (
            "Types: deb\n"
            "URIs: https://repo.invalid/private?access_token=FAKE_QUERY_TOKEN\n"
            "Suites: noble\n"
            "Components: main\n"
        )

        rewritten, warnings = self.rewrite_apt_source(
            source, "sources", "http://gateway/v1/cache"
        )

        self.assertIn(
            "URIs: https://repo.invalid/private?access_token=FAKE_QUERY_TOKEN",
            rewritten,
        )
        self.assertIn("source=https://repo.invalid/private", warnings)
        self.assertNotIn("FAKE_QUERY_TOKEN", warnings)

    def test_renderer_preserves_shell_and_exec_runs_for_frontend(self) -> None:
        source = (
            "FROM ubuntu:24.04\n"
            "RUN apt-get update\n"
            "RUN apt install -y curl\n"
            "RUN sh -c 'apt-get install -y git'\n"
            "RUN printf '#!/bin/sh\\napt-get update\\n' >/tmp/install.sh "
            "&& sh /tmp/install.sh\n"
            "RUN python3 -c 'import subprocess; "
            "subprocess.run([\"apt-get\", \"update\"], check=True)'\n"
            "RUN [\"apt-get\", \"update\"]\n"
            "RUN [\"/usr/bin/apt-get\", \"update\"]\n"
        )
        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertEqual(rendered.count("target=/run/opensandbox-apt/bin/apt,"), 0)
        self.assertEqual(
            rendered.count("target=/run/opensandbox-apt/bin/apt-get,"), 0
        )
        self.assertEqual(
            rendered.count("export PATH=/run/opensandbox-apt/bin:$PATH;"), 0
        )
        self.assertIn('RUN ["apt-get", "update"]', rendered)
        self.assertIn('RUN ["/usr/bin/apt-get", "update"]', rendered)
        self.assertNotIn("run-with-apt-path\", \"apt-get", rendered)
        self.assertNotIn("target=/usr/bin/apt", rendered)
        self.assertNotIn("type=bind", rendered)

    def test_renderer_preserves_multiline_exec_run_for_frontend(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                'RUN ["apt-get", \\\n'
                '     "update"]\n'
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn('RUN ["apt-get", \\\n     "update"]', rendered)
        self.assertNotIn("target=/run/opensandbox-apt", rendered)
        self.assertNotIn("run-with-apt-path", rendered)
        self.assertNotIn("export PATH=", rendered)

    def test_renderer_preserves_all_run_options_for_frontend(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN --mount=type=cache,target=/tmp/cache "
                "--network=none --security=sandbox "
                "--device=nvidia.com/gpu=all echo hello\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )
        run_line = next(
            line for line in rendered.splitlines() if line.startswith("RUN ")
        )

        for option in (
            "--mount=type=cache,target=/tmp/cache",
            "--network=none",
            "--security=sandbox",
            "--device=nvidia.com/gpu=all",
        ):
            self.assertLess(run_line.index(option), run_line.index("echo hello"))
        self.assertIn(
            "echo hello", run_line
        )
        self.assertNotIn("; --device=", run_line)

    def test_runtime_path_is_mechanical_not_apt_detection(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM alpine:3.20\n"
                "RUN echo no-package-manager-use\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            "RUN echo no-package-manager-use",
            rendered,
        )

    def test_runtime_wrapper_assets_are_standalone_and_use_current_rootfs_apt(
        self,
    ) -> None:
        wrapper = (APT_RUNTIME_ASSET_DIR / "apt-wrapper.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("real=/usr/bin/$command_name", wrapper)
        self.assertIn("Dir::Etc::SourceList=$source_view/sources.list", wrapper)
        self.assertIn("event=index-reconciliation-failed", wrapper)
        self.assertIn("indextargets --no-release-info", wrapper)
        self.assertIn("event=source-bypass", (
            APT_RUNTIME_ASSET_DIR / "source-rewriter.awk"
        ).read_text(encoding="utf-8"))
        self.assertIn('${APT_CONFIG:-}', wrapper)
        self.assertIn("*dir::etc=*", wrapper)
        self.assertNotIn("base-root", wrapper)

    def test_runtime_preserves_authored_source_and_absolute_path_limitation(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN echo 'deb https://packages.example/repo stable main' "
                "> /etc/apt/sources.list.d/vendor.list && apt-get update\n"
                "RUN /usr/bin/apt-get --version\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            "deb https://packages.example/repo stable main", rendered
        )
        self.assertIn(
            "RUN /usr/bin/apt-get --version",
            rendered,
        )
        self.assertNotIn(
            "deb http://gateway/apt/vendor stable main", rendered
        )

    def test_render_does_not_guess_unconfigured_third_party_sources(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN curl -fsSL https://packages.example.com/key.gpg\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn("https://packages.example.com/key.gpg", rendered)

    def test_renderer_leaves_download_commands_for_runtime_interception(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN curl -fsSL https://packages.example/setup.sh | sh\n"
                "RUN wget -O /tmp/tool https://downloads.example/tool.tar.gz\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn("curl -fsSL https://packages.example/setup.sh | sh", rendered)
        self.assertIn(
            "wget -O /tmp/tool https://downloads.example/tool.tar.gz", rendered
        )

    def test_render_never_rewrites_authored_apt_source(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN echo 'deb https://packages.example/repository stable main' "
                "> /etc/apt/sources.list.d/vendor.list\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn("https://packages.example/repository", rendered)
        self.assertNotIn("http://sources.internal/apt/vendor", rendered)

    def test_render_does_not_rewrite_non_source_apt_files(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN apt-get update && "
                "printf %s https://packages.example/repository "
                "> /etc/apt/trusted.gpg.d/runtime.conf\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        task_run = next(
            line for line in rendered.splitlines() if "runtime.conf" in line
        )
        self.assertIn("https://packages.example/repository", task_run)
        self.assertNotIn("http://sources.internal/apt/vendor", task_run)

    def test_render_preserves_apt_source_data_heredoc(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN cat <<'EOF' > /etc/apt/sources.list.d/vendor.list\n"
                "deb https://packages.example/repository stable main\n"
                "EOF\n"
                "RUN apt-get update\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        task_heredoc = rendered[
            rendered.index("cat <<'EOF'") : rendered.index("EOF\n")
        ]
        self.assertIn("packages.example/repository", task_heredoc)
        self.assertNotIn("sources.internal/apt/vendor", task_heredoc)

    def test_persisted_script_content_is_not_statically_inspected(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN cat <<'EOF' > /usr/local/bin/install-later\n"
                "#!/bin/sh\n"
                "apt-get update\n"
                "EOF\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn("apt-get update", rendered)
        self.assertNotIn("/run/opensandbox-apt", rendered)

    def test_render_does_not_rewrite_configured_source_inside_copy_heredoc(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "COPY <<'SOURCES' /tmp/vendor.list\n"
                "deb https://packages.example/repository stable main\n"
                "SOURCES\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            "deb https://packages.example/repository stable main", rendered
        )
        self.assertNotIn("sources.internal/apt/vendor", rendered)

    def test_render_does_not_rewrite_from_inside_dockerfile_heredocs(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN python3 << 'PYEOF'\n"
                "from PIL import Image\n"
                "PYEOF\n"
                "RUN python3 -m venv /tmp/setup && \\\n"
                "    /tmp/setup/bin/python3 << 'CONTINUED'\n"
                "from pathlib import Path\n"
                "CONTINUED\n"
                "COPY <<'APP' /app/app.py\n"
                "from flask import Flask\n"
                "APP\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
        )

        self.assertIn(
            "FROM m.daocloud.io/docker.io/library/ubuntu:24.04", rendered
        )
        self.assertIn("from PIL import Image", rendered)
        self.assertIn("from pathlib import Path", rendered)
        self.assertIn("from flask import Flask", rendered)
        self.assertEqual(rendered.count("m.daocloud.io/docker.io"), 1)

    def test_oci_build_disables_default_provenance_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock()
            process.wait.return_value = 0
            with (
                patch(
                    "opensandbox_image_manager.subprocess.run",
                    return_value=Mock(returncode=0),
                ),
                patch(
                    "opensandbox_image_manager.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                run_build(
                    environment_dir=root,
                    dockerfile=root / "Dockerfile",
                    archive_path=root / "image.oci.tar",
                    log_path=root / "build.log",
                    platform="linux/amd64",
                    timeout_sec=60,
                    build_args={},
                    build_contexts={"local-frontend": "oci-layout:///cache/frontend@sha256:" + "0" * 64},
                )

        command = popen.call_args.args[0]
        self.assertIn("--provenance=false", command)
        self.assertEqual(command[command.index("--build-context") + 1],
                         "local-frontend=oci-layout:///cache/frontend@sha256:" + "0" * 64)

    def test_interrupted_build_terminates_detached_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock(pid=4242)
            process.wait.side_effect = [KeyboardInterrupt, 0]
            with (
                patch(
                    "opensandbox_image_manager.subprocess.run",
                    return_value=Mock(returncode=0),
                ),
                patch(
                    "opensandbox_image_manager.subprocess.Popen",
                    return_value=process,
                ),
                patch("opensandbox_image_manager.os.killpg") as killpg,
                self.assertRaises(KeyboardInterrupt),
            ):
                run_build(
                    environment_dir=root,
                    dockerfile=root / "Dockerfile",
                    archive_path=root / "image.oci.tar",
                    log_path=root / "build.log",
                    platform="linux/amd64",
                    timeout_sec=60,
                    build_args={},
                )

        killpg.assert_called_once_with(4242, signal.SIGTERM)

    def test_no_cache_build_flag_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock()
            process.wait.return_value = 0
            with (
                patch(
                    "opensandbox_image_manager.subprocess.run",
                    return_value=Mock(returncode=0),
                ),
                patch(
                    "opensandbox_image_manager.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                run_build(
                    environment_dir=root,
                    dockerfile=root / "Dockerfile",
                    archive_path=root / "image.oci.tar",
                    log_path=root / "build.log",
                    platform="linux/amd64",
                    timeout_sec=60,
                    build_args={},
                    no_cache=True,
                )

        self.assertIn("--no-cache", popen.call_args.args[0])

    def test_host_build_network_flag_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock()
            process.wait.return_value = 0
            with (
                patch(
                    "opensandbox_image_manager.subprocess.run",
                    return_value=Mock(returncode=0),
                ),
                patch(
                    "opensandbox_image_manager.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                run_build(
                    environment_dir=root,
                    dockerfile=root / "Dockerfile",
                    archive_path=root / "image.oci.tar",
                    log_path=root / "build.log",
                    platform="linux/amd64",
                    timeout_sec=60,
                    build_args={},
                    build_network="host",
                )

        self.assertIn("--network=host", popen.call_args.args[0])

    def test_schema2_conversion_keeps_blob_digests(self) -> None:
        config = b'{"architecture":"amd64","os":"linux"}'
        layer = b"compressed-layer-placeholder"
        source_manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": OCI_CONFIG,
                    "digest": sha256(config),
                    "size": len(config),
                },
                "layers": [
                    {
                        "mediaType": OCI_LAYER_GZIP,
                        "digest": sha256(layer),
                        "size": len(layer),
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        index = json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": sha256(source_manifest),
                        "size": len(source_manifest),
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()

        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "image.tar"
            with tarfile.open(archive_path, "w") as archive:
                add_tar_bytes(archive, "index.json", index)
                for data in (source_manifest, config, layer):
                    add_tar_bytes(archive, f"blobs/sha256/{sha256(data).split(':')[1]}", data)
            with tarfile.open(archive_path, "r") as archive:
                manifest, descriptors = schema2_manifest(archive)

        self.assertEqual(manifest["mediaType"], DOCKER_MANIFEST)
        self.assertEqual(manifest["config"]["mediaType"], DOCKER_CONFIG)
        self.assertEqual(manifest["layers"][0]["mediaType"], DOCKER_LAYER_GZIP)
        self.assertEqual([item["digest"] for item in descriptors], [sha256(config), sha256(layer)])

    def test_oci_archive_config_is_read_before_archive_is_discarded(self) -> None:
        config = json.dumps(
            {"config": {"Cmd": ["/usr/sbin/sshd", "-D"], "ExposedPorts": {"22/tcp": {}}}}
        ).encode()
        source_manifest = json.dumps(
            {"schemaVersion": 2, "config": {"digest": sha256(config)}}
        ).encode()
        index = json.dumps(
            {"manifests": [{"digest": sha256(source_manifest)}]}
        ).encode()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "image.tar"
            with tarfile.open(archive_path, "w") as archive:
                add_tar_bytes(archive, "index.json", index)
                add_tar_bytes(archive, f"blobs/sha256/{sha256(source_manifest).split(':')[1]}", source_manifest)
                add_tar_bytes(archive, f"blobs/sha256/{sha256(config).split(':')[1]}", config)
            image_config = oci_archive_image_config(archive_path)
        self.assertEqual(image_config["cmd"], ["/usr/sbin/sshd", "-D"])
        self.assertEqual(image_config["exposed_ports"], [{"port": 22, "protocol": "tcp"}])

    def test_dry_run_returns_platform_image_ref_without_external_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_task(root, "0")
            args = Namespace(
                task_dir=None,
                dataset_root=root,
                include="0",
                registry="harbor.example.internal",
                project="test-project",
                task_repository="",
                benchmark_name="seta",
                docker_config=root / "missing-config.json",
                cache_root=root / "cache",
                platform="linux/amd64",
                tag_prefix="harbor",
                dockerhub_mirror_prefix="m.daocloud.io/docker.io",
                build_args_json="{}",
                force=False,
                registry_tls_verify=False,
                dry_run=True,
            )
            image_ref = prepare(args)
        self.assertRegex(
            image_ref,
            r"^harbor\.example\.internal/test-project/0@sha256:[0-9a-f]{64}$",
        )

    def test_local_uploaded_bundle_avoids_registry_and_can_skip_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self.make_task(root, "0")
            cache_root = root / "cache"

            def make_args(output: Path, *, skip_hash: bool = False) -> Namespace:
                argv = [
                    "--task-dir",
                    str(task),
                    "--registry",
                    "harbor.example.internal",
                    "--project",
                    "test-project",
                    "--benchmark-name",
                    "seta",
                    "--cache-root",
                    str(cache_root),
                    "--bundle-manifest-output",
                    str(output),
                    "--reuse-local-upload-cache",
                    "--no-use-proxy",
                ]
                if skip_hash:
                    argv.append("--skip-hash-verification")
                with patch.dict(os.environ, {}, clear=True):
                    return parse_args(argv)

            publisher = Mock()
            publisher.inspect_config.return_value = {
                "entrypoint": None,
                "cmd": None,
                "working_dir": None,
                "exposed_ports": [],
                "healthcheck": None,
            }
            registry = Mock()
            registry.manifest.return_value = {
                "artifact_digest": "sha256:" + "a" * 64,
                "media_type": DOCKER_MANIFEST,
            }
            first_output = root / "first.json"
            with (
                patch(
                    "opensandbox_image_manager.registry_credentials",
                    return_value=("user", "password"),
                ),
                patch(
                    "opensandbox_image_manager.SkopeoPublisher",
                    return_value=publisher,
                ),
                patch(
                    "opensandbox_image_manager.RegistryClient",
                    return_value=registry,
                ),
            ):
                first = prepare_bundle(make_args(first_output))

            self.assertTrue(first_output.is_file())
            self.assertEqual(registry.manifest.call_count, 1)
            uploaded = list((cache_root / "uploaded-bundles").glob("*/*.json"))
            self.assertEqual(len(uploaded), 1)

            verified_output = root / "verified.json"
            with (
                patch(
                    "opensandbox_image_manager.registry_credentials",
                    side_effect=AssertionError("Registry access is not expected"),
                ),
                patch(
                    "opensandbox_image_manager.environment_content_hash",
                    wraps=environment_content_hash,
                ) as content_hash,
            ):
                verified = prepare_bundle(make_args(verified_output))

            self.assertGreater(content_hash.call_count, 0)
            self.assertEqual(verified.main_image_ref, first.main_image_ref)
            self.assertEqual(
                json.loads(verified_output.read_text(encoding="utf-8")),
                first.manifest,
            )

            (task / "environment" / "Dockerfile").write_text(
                "FROM ubuntu:24.04\nRUN echo changed\n", encoding="utf-8"
            )
            with (
                patch(
                    "opensandbox_image_manager.registry_credentials",
                    side_effect=AssertionError("stale cache reached Registry fallback"),
                ),
                self.assertRaisesRegex(AssertionError, "Registry fallback"),
            ):
                prepare_bundle(make_args(root / "stale.json"))

            skipped_output = root / "skipped.json"
            with (
                patch(
                    "opensandbox_image_manager.resolve_bundle_spec",
                    side_effect=AssertionError("task content must not be resolved"),
                ),
                patch(
                    "opensandbox_image_manager.registry_credentials",
                    side_effect=AssertionError("Registry access is not expected"),
                ),
            ):
                skipped = prepare_bundle(
                    make_args(skipped_output, skip_hash=True)
                )

            self.assertEqual(skipped.main_image_ref, first.main_image_ref)
            self.assertTrue(skipped_output.is_file())

    def test_registry_target_keeps_project_and_task_repository_separate(self) -> None:
        target = RegistryTarget("registry.example", "seta", "973")
        self.assertEqual(target.repository, "seta/973")
        self.assertEqual(target.tag("worker", "sha256:" + "a" * 64), "worker-" + "a" * 20)
        self.assertEqual(
            target.digest_ref("sha256:" + "b" * 64),
            "registry.example/seta/973@sha256:" + "b" * 64,
        )

    def test_task_repository_preserves_valid_task_identity_verbatim(self) -> None:
        self.assertEqual(
            check_task_repository("aeon-toolkit__aeon-2822"),
            "aeon-toolkit__aeon-2822",
        )
        self.assertEqual(
            check_task_repository("task_000002_8be5378a"),
            "task_000002_8be5378a",
        )
        long_identity = f"owner__{'repository-' * 10}123"
        self.assertEqual(check_task_repository(long_identity), long_identity)

    def test_task_repository_rejects_identity_that_requires_renaming(self) -> None:
        for identity in ("Owner__Repo-1", "owner/repo-1", "owner repo-1"):
            with self.subTest(identity=identity), self.assertRaisesRegex(
                ValueError, "fix the dataset adapter"
            ):
                check_task_repository(identity)

        with self.assertRaisesRegex(ValueError, "exceeds the 8-character"):
            check_task_repository("valid-name", maximum_length=8)

    def test_skopeo_login_password_uses_subprocess_input_only(self) -> None:
        target = RegistryTarget("registry.example", "seta", "973")
        publisher = SkopeoPublisher(target, "user", "password", tls_verify=False)
        with patch(
            "opensandbox_image_manager.subprocess.run",
            return_value=Mock(returncode=0, stdout="", stderr=""),
        ) as run:
            publisher.login()

        self.assertEqual(run.call_args.kwargs["input"], "password")
        self.assertIsNone(run.call_args.kwargs["stdin"])
        command = run.call_args.args[0]
        self.assertIn("--authfile", command)
        self.assertEqual(command[command.index("--authfile") + 1], publisher._authfile)
        self.assertTrue(Path(publisher._authfile).parent.is_dir())
        publisher.close()
        self.assertFalse(Path(publisher._authfile).parent.exists())

    def test_skopeo_inspect_treats_first_repository_lookup_as_cache_miss(self) -> None:
        publisher = SkopeoPublisher(
            RegistryTarget("registry.example", "seta", "973"),
            "user",
            "password",
            tls_verify=False,
        )
        publisher.login = Mock()
        publisher._run = Mock(
            side_effect=RuntimeError("repository seta/973 not found")
        )
        try:
            self.assertIsNone(publisher.inspect("registry.example/seta/973:main-hash"))
        finally:
            publisher.close()


if __name__ == "__main__":
    unittest.main()
