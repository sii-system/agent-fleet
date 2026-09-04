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
    APT_SOURCE_RESTORE_AWK,
    DOCKER_CONFIG,
    DOCKER_LAYER_GZIP,
    DOCKER_MANIFEST,
    OCI_CONFIG,
    OCI_LAYER_GZIP,
    RegistryTarget,
    SkopeoPublisher,
    _compose_runtime,
    _service_manifest,
    apt_404_requires_cache_refresh,
    build_image_identity,
    check_task_repository,
    environment_content_hash,
    github_mirror_config_content,
    mirror_image_ref,
    normalize_oci_image_config,
    oci_archive_image_config,
    package_source_build_args,
    parse_apt_source_overrides,
    parse_args,
    prepare,
    prepare_bundle,
    proxy_build_args,
    render_build_dockerfile,
    run_build,
    schema2_manifest,
    source_override_sed,
    validate_github_mirror_url,
)


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def add_tar_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def injected_shell_runs(rendered: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for line in rendered.splitlines():
        if not line.startswith("RUN ["):
            continue
        argv = json.loads(line.removeprefix("RUN "))
        if argv[:2] == ["/bin/sh", "-c"]:
            result.append((line, argv[2]))
    return result


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
            args.apt_mirror, "http://mirrors.tuna.tsinghua.edu.cn"
        )
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

    def test_build_image_identity_includes_renderer_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            environment = self.make_task(Path(tmp)) / "environment"
            first = build_image_identity(environment)
            with patch(
                "opensandbox_image_manager.BUILD_RENDERER_VERSION",
                "next-renderer-contract",
            ):
                second = build_image_identity(environment)

        self.assertNotEqual(first, second)

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
                apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
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

    def test_render_uses_domestic_sources_and_preserves_stage_alias(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04 AS builder\n"
                "RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg "
                "&& apt-get update\n"
                "FROM builder\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
            package_build_args={
                "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
                "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
            },
        )
        injected = injected_shell_runs(rendered)

        self.assertIn(
            "FROM m.daocloud.io/docker.io/library/ubuntu:24.04 AS builder",
            rendered,
        )
        self.assertEqual(len(injected), 2)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/ubuntu/", injected[0][1])
        self.assertIn("original", injected[0][1])
        self.assertIn("adapted", injected[0][1])
        self.assertIn("archive.ubuntu.com/ubuntu/", injected[1][1])
        self.assertIn("FROM builder\n", rendered)
        self.assertNotIn("docker.io/library/builder", rendered)
        task_run = next(
            line for line in rendered.splitlines() if line.startswith("RUN curl")
        )
        self.assertIn("https://download.docker.com/linux/ubuntu/gpg", task_run)
        self.assertLess(
            rendered.index(injected[1][0]), rendered.index("FROM builder")
        )
        self.assertIn("ARG NPM_CONFIG_REGISTRY", rendered)
        self.assertIn("ARG PIP_INDEX_URL", rendered)

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

    def test_github_mirror_uses_transient_secret_mount_for_submodules(self) -> None:
        mirror = validate_github_mirror_url(
            "http://github-mirror.internal:8080/repos", "host"
        )
        config = github_mirror_config_content(mirror)
        rendered = render_build_dockerfile(
            "FROM ubuntu:24.04 AS builder\nRUN git submodule update --init --recursive\nFROM builder\n",
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
            github_mirror_config_mount_id="opensandbox-github-mirror-gitconfig",
        )

        self.assertEqual(mirror, "http://github-mirror.internal:8080/repos/")
        self.assertIn("insteadOf = https://github.com/", config)
        self.assertIn("insteadOf = git@github.com:", config)
        self.assertEqual(
            rendered.count("id=opensandbox-github-mirror-gitconfig"), 1
        )
        self.assertNotIn(mirror, rendered)

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

    def test_apt_source_overrides_accept_provider_neutral_url_mappings(self) -> None:
        overrides = parse_apt_source_overrides(
            json.dumps(
                {
                    "https://packages.example.com/repository/": (
                        "http://sources.internal/apt/vendor-repository/"
                    ),
                    "https://packages.example.com/signing-key.gpg": (
                        "http://sources.internal/objects/vendor-key.gpg"
                    ),
                }
            ),
            "host",
        )

        self.assertEqual(
            overrides,
            {
                "https://packages.example.com/repository": (
                    "http://sources.internal/apt/vendor-repository"
                ),
                "https://packages.example.com/signing-key.gpg": (
                    "http://sources.internal/objects/vendor-key.gpg"
                ),
            },
        )

    def test_apt_source_overrides_require_unique_reverse_mappings(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            parse_apt_source_overrides(
                json.dumps(
                    {
                        "https://one.example/repository": "http://mirror/apt/shared",
                        "https://two.example/repository": "http://mirror/apt/shared",
                    }
                ),
                "host",
            )

    def test_apt_source_overrides_reject_cross_prefix_cascades(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            parse_apt_source_overrides(
                json.dumps(
                    {
                        "https://upstream.example/repository": (
                            "https://mirror.example/vendor"
                        ),
                        "https://mirror.example/vendor/key": (
                            "https://other.example/key"
                        ),
                    }
                ),
                "host",
            )

    def test_apt_source_override_sed_prefers_the_longest_prefix(self) -> None:
        overrides = {
            "https://packages.example.com/repository": (
                "http://sources.internal/apt/vendor-repository"
            ),
            "https://packages.example.com/repository/signing-key.gpg": (
                "http://sources.internal/objects/vendor-key.gpg"
            ),
        }
        expression = source_override_sed(overrides)
        source = (
            "https://packages.example.com/repository/signing-key.gpg\n"
            "https://packages.example.com/repository/dists/stable\n"
            "https://packages.example.com/repository-v2\n"
        )
        completed = subprocess.run(
            ["sed", "-E", expression],
            input=source,
            capture_output=True,
            check=True,
            text=True,
        )

        self.assertEqual(
            completed.stdout,
            "http://sources.internal/objects/vendor-key.gpg\n"
            "http://sources.internal/apt/vendor-repository/dists/stable\n"
            "https://packages.example.com/repository-v2\n",
        )

    def test_render_preserves_debian_security_repository_path(self) -> None:
        rendered = render_build_dockerfile(
            "FROM python:3.13-slim-bookworm\nRUN apt-get update\n",
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://mirrors.tuna.tsinghua.edu.cn",
        )
        injected = injected_shell_runs(rendered)

        self.assertEqual(len(injected), 2)
        self.assertIn("debian\\.org/debian-security", injected[0][1])
        self.assertIn(
            "mirrors.tuna.tsinghua.edu.cn/debian-security/", injected[0][1]
        )
        self.assertIn("security.debian.org/debian-security/", injected[1][1])
        self.assertNotIn("debian/-security", injected[0][1])

    def test_render_routes_apt_for_internal_overlay_alias_stage(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/rebench/c@sha256:abc AS base\n"
                "FROM base AS task\n"
                "RUN <<-BUILD\n"
                "\tapt-get update -qq\n"
                "BUILD\n"
            ),
            dockerhub_mirror_prefix="registry.internal/public-mirror",
            apt_mirror="http://apt-mirror.internal/repos",
        )
        injected = injected_shell_runs(rendered)

        self.assertEqual(len(injected), 2)
        self.assertIn("apt-mirror.internal/repos/ubuntu/", injected[0][1])
        self.assertIn("apt-mirror.internal/repos/debian/", injected[0][1])
        self.assertLess(
            rendered.index(injected[0][0]), rendered.index("RUN <<-BUILD")
        )
        self.assertGreater(
            rendered.index(injected[1][0]), rendered.index("RUN <<-BUILD")
        )

    def test_render_detects_apt_on_a_run_continuation(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/rebench/c@sha256:abc\n"
                "RUN set -eux; \\\n"
                "    apt-get update\n"
            ),
            dockerhub_mirror_prefix="registry.internal/public-mirror",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        injected = injected_shell_runs(rendered)
        self.assertEqual(len(injected), 2)
        self.assertLess(rendered.index(injected[0][0]), rendered.index("RUN set"))
        self.assertGreater(
            rendered.index(injected[1][0]), rendered.index("apt-get update")
        )

    def test_render_detects_exec_form_apt_run(self) -> None:
        rendered = render_build_dockerfile(
            'FROM ubuntu:24.04\nRUN ["apt-get", "update"]\n',
            dockerhub_mirror_prefix="registry.internal/public-mirror",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        injected = injected_shell_runs(rendered)
        self.assertEqual(len(injected), 2)
        task_run_offset = rendered.index('RUN ["apt-get"')
        self.assertLess(rendered.index(injected[0][0]), task_run_offset)
        self.assertGreater(rendered.index(injected[1][0]), task_run_offset)

    def test_render_detects_shell_wrapped_apt_runs(self) -> None:
        for task_run in (
            "RUN sh -c 'apt-get update'",
            'RUN ["/bin/bash", "-lc", "apt-get update"]',
        ):
            with self.subTest(task_run=task_run):
                rendered = render_build_dockerfile(
                    f"FROM ubuntu:24.04\n{task_run}\n",
                    dockerhub_mirror_prefix="m.daocloud.io/docker.io",
                    apt_mirror="http://apt-mirror.internal/repos",
                )

                injected = injected_shell_runs(rendered)
                self.assertEqual(len(injected), 2)
                task_run_offset = rendered.index(task_run)
                self.assertLess(rendered.index(injected[0][0]), task_run_offset)
                self.assertGreater(rendered.index(injected[1][0]), task_run_offset)

    def test_render_refreshes_apt_sources_copied_after_stage_setup(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "COPY vendor.list /etc/apt/sources.list.d/vendor.list\n"
                "RUN apt-get update\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

        injected = injected_shell_runs(rendered)
        self.assertEqual(len(injected), 4)
        refresh_cleanup_line, refresh_cleanup = injected[1]
        refresh_setup_line, refresh_setup = injected[2]
        self.assertIn(r"sources\.internal/apt/vendor", refresh_cleanup)
        self.assertIn("packages.example/repository", refresh_cleanup)
        self.assertIn("sources.internal/apt/vendor", refresh_setup)
        self.assertIn(r"packages\.example/repository", refresh_setup)
        refresh_cleanup_offset = rendered.index(refresh_cleanup_line)
        refresh_setup_offset = rendered.index(
            refresh_setup_line,
            refresh_cleanup_offset + len(refresh_cleanup_line),
        )
        self.assertLess(
            rendered.index("COPY vendor.list"),
            refresh_cleanup_offset,
        )
        self.assertLess(refresh_cleanup_offset, refresh_setup_offset)
        self.assertLess(refresh_setup_offset, rendered.index("RUN apt-get"))
        for _line, command in injected:
            subprocess.run(["/bin/sh", "-n", "-c", command], check=True)

    def test_render_refreshes_apt_sources_changed_by_an_earlier_run(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN cp /tmp/vendor.list "
                "/etc/apt/sources.list.d/vendor.list\n"
                "RUN apt-get update\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        injected = injected_shell_runs(rendered)
        self.assertEqual(len(injected), 4)
        refresh_cleanup_offset = rendered.index(injected[1][0])
        refresh_setup_offset = rendered.index(
            injected[2][0], refresh_cleanup_offset + len(injected[1][0])
        )
        self.assertLess(rendered.index("RUN cp"), refresh_cleanup_offset)
        self.assertLess(refresh_setup_offset, rendered.index("RUN apt-get"))

    def test_render_does_not_refresh_after_unrelated_copy(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "COPY app /usr/local/bin/app\n"
                "RUN apt-get update\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        self.assertEqual(len(injected_shell_runs(rendered)), 2)

    def test_render_does_not_inject_apt_routing_into_scratch_stage(self) -> None:
        rendered = render_build_dockerfile(
            "FROM scratch\nCOPY app /app\n",
            dockerhub_mirror_prefix="registry.internal/public-mirror",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        self.assertEqual(injected_shell_runs(rendered), [])

    def test_render_restores_task_user_after_apt_cleanup(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN apt-get update\n"
                "USER node\n"
                "CMD [\"node\"]\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        lines = rendered.splitlines()
        self.assertEqual(lines[-1], "USER node")
        self.assertEqual(lines[-3], "USER root")
        self.assertIn("rm -rf", json.loads(lines[-2].removeprefix("RUN "))[2])

    def test_rendered_apt_setup_and_cleanup_are_valid_posix_shell(self) -> None:
        rendered = render_build_dockerfile(
            "FROM ubuntu:24.04\nRUN apt-get update\n",
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        for _line, command in injected_shell_runs(rendered):
            subprocess.run(["/bin/sh", "-n", "-c", command], check=True)
        cleanup = injected_shell_runs(rendered)[-1][1]
        self.assertIn("apt-get indextargets", cleanup)
        self.assertIn("target-moves", cleanup)

    def test_apt_source_restore_preserves_original_endpoints_after_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.sources"
            adapted = root / "adapted.sources"
            current = root / "current.sources"
            original.write_text(
                "deb https://archive.ubuntu.com/ubuntu noble main\n"
                "deb https://security.ubuntu.com/ubuntu noble-security main\n",
                encoding="utf-8",
            )
            adapted.write_text(
                "deb http://mirror.internal/ubuntu noble main\n"
                "deb http://mirror.internal/ubuntu noble-security main\n",
                encoding="utf-8",
            )
            current.write_text(
                "deb http://mirror.internal/ubuntu noble main universe\n"
                "deb http://mirror.internal/ubuntu noble-security main\n"
                + "URIs: https://packages.example/repository\n",
                encoding="utf-8",
            )
            expected = (
                "deb https://archive.ubuntu.com/ubuntu noble main universe\n"
                "deb https://security.ubuntu.com/ubuntu noble-security main\n"
                + "URIs: https://packages.example/repository\n"
            )
            completed = subprocess.run(
                [
                    "awk",
                    APT_SOURCE_RESTORE_AWK,
                    str(original),
                    str(adapted),
                    str(current),
                ],
                capture_output=True,
                check=True,
                text=True,
            )

        self.assertEqual(
            completed.stdout,
            expected,
        )

    def test_render_escapes_sed_replacement_characters_in_mirror_url(self) -> None:
        rendered = render_build_dockerfile(
            "FROM ubuntu:24.04\nRUN apt-get update\n",
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos/a&b",
        )
        injected = injected_shell_runs(rendered)

        self.assertIn(r"apt-mirror.internal/repos/a\&b/ubuntu", injected[0][1])

    def test_render_does_not_rewrite_configured_source_in_runtime_environment(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "ENV DOCKER_REPO=https://download.docker.com/linux/ubuntu\n"
                "RUN apt-get update\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://download.docker.com": "http://sources.internal/apt/docker"
            },
        )

        self.assertIn(
            "ENV DOCKER_REPO=https://download.docker.com/linux/ubuntu",
            rendered,
        )

    def test_render_routes_and_restores_configured_third_party_sources(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN curl -fsSL https://packages.example.com/signing-key.gpg "
                "-o /tmp/vendor.gpg && "
                "echo 'deb https://packages.example.com/repository stable main' "
                "> /etc/apt/sources.list.d/vendor.list && apt-get update\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example.com/repository": (
                    "http://sources.internal/apt/vendor-repository"
                ),
                "https://packages.example.com/signing-key.gpg": (
                    "http://sources.internal/objects/vendor-key.gpg"
                ),
            },
        )
        injected = injected_shell_runs(rendered)

        task_run = next(
            line for line in rendered.splitlines() if "vendor.list" in line
        )
        self.assertIn("sources.internal/apt/vendor-repository", task_run)
        self.assertIn("sources.internal/objects/vendor-key.gpg", task_run)
        self.assertNotIn("packages.example.com", task_run)
        self.assertIn("packages.example.com/repository", injected[-1][1])
        self.assertIn("packages.example.com/signing-key.gpg", injected[-1][1])

    def test_render_does_not_guess_unconfigured_third_party_sources(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN curl -fsSL https://packages.example.com/key.gpg\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        self.assertIn("https://packages.example.com/key.gpg", rendered)

    def test_render_routes_override_only_opaque_stage_without_apt_setup(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/custom/base:latest\n"
                "RUN curl -fsSL https://packages.example.com/setup.sh | sh\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example.com/setup.sh": (
                    "http://sources.internal/objects/vendor-setup.sh"
                )
            },
        )
        injected = injected_shell_runs(rendered)

        self.assertEqual(injected, [])
        self.assertIn("sources.internal/objects/vendor-setup.sh", rendered)

    def test_render_does_not_rewrite_url_persisted_outside_apt(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN curl -fsSL https://packages.example/repository "
                "-o /tmp/package && "
                "printf %s https://packages.example/repository "
                "> /app/runtime.conf\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

        task_run = next(
            line for line in rendered.splitlines() if line.startswith("RUN curl")
        )
        self.assertIn(
            "curl -fsSL http://sources.internal/apt/vendor", task_run
        )
        self.assertIn(
            "printf %s https://packages.example/repository", task_run
        )
        self.assertNotIn(
            "printf %s http://sources.internal/apt/vendor", task_run
        )
        self.assertEqual(injected_shell_runs(rendered), [])

    def test_render_rewrites_exec_form_fetch_source_override(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                'RUN ["/usr/bin/curl", "-fsSL", '
                '"https://packages.example/key", "-o", "/tmp/key"]\n'
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/key": (
                    "http://sources.internal/objects/key"
                )
            },
        )

        self.assertIn("http://sources.internal/objects/key", rendered)
        self.assertNotIn("https://packages.example/key", rendered)
        self.assertEqual(injected_shell_runs(rendered), [])

    def test_render_preserves_exec_form_non_fetch_url_data(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                'RUN ["printf", "%s", '
                '"https://packages.example/repository"]\n'
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

        self.assertIn("https://packages.example/repository", rendered)
        self.assertNotIn("http://sources.internal/apt/vendor", rendered)

    def test_render_does_not_rewrite_apt_source_without_apt_run(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN echo 'deb https://packages.example/repository stable main' "
                "> /etc/apt/sources.list.d/vendor.list\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

        self.assertIn("https://packages.example/repository", rendered)
        self.assertNotIn("http://sources.internal/apt/vendor", rendered)
        self.assertEqual(injected_shell_runs(rendered), [])

    def test_render_does_not_treat_other_apt_paths_as_restorable_sources(
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
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

        task_run = next(
            line for line in rendered.splitlines() if "runtime.conf" in line
        )
        self.assertIn("https://packages.example/repository", task_run)
        self.assertNotIn("http://sources.internal/apt/vendor", task_run)

    def test_render_rewrites_url_only_run_continuation(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN curl -fsSL \\\n"
                "    https://packages.example.com/signing-key.gpg\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example.com/signing-key.gpg": (
                    "http://sources.internal/objects/vendor-key.gpg"
                )
            },
        )

        self.assertIn(
            "http://sources.internal/objects/vendor-key.gpg", rendered
        )
        self.assertNotIn(
            "    https://packages.example.com/signing-key.gpg", rendered
        )

    def test_render_rewrites_configured_sources_inside_run_heredoc(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN <<-DOCKER_RUN_EOF\n"
                "\tcurl -fsSL https://bazel.example/signing-key.gpg "
                "-o /tmp/bazel.gpg\n"
                "\techo 'deb https://packages.example/bazel stable main' "
                "> /etc/apt/sources.list.d/bazel.list\n"
                "\tapt-get update\n"
                "DOCKER_RUN_EOF\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://bazel.example/signing-key.gpg": (
                    "http://sources.internal/objects/bazel-key.gpg"
                ),
                "https://packages.example/bazel": (
                    "http://sources.internal/apt/bazel"
                ),
            },
        )
        injected = injected_shell_runs(rendered)

        self.assertIn(
            "curl -fsSL http://sources.internal/objects/bazel-key.gpg",
            rendered,
        )
        self.assertIn(
            "deb http://sources.internal/apt/bazel stable main", rendered
        )
        self.assertIn("DOCKER_RUN_EOF\n", rendered)
        self.assertIn(
            "https://bazel.example/signing-key.gpg", injected[-1][1]
        )
        self.assertIn("https://packages.example/bazel", injected[-1][1])

    def test_render_routes_override_only_run_heredoc_without_apt_setup(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN <<'EOF'\n"
                "curl -fsSL https://packages.example/setup.sh | sh\n"
                "EOF\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/setup.sh": (
                    "http://sources.internal/objects/setup.sh"
                )
            },
        )

        self.assertIn(
            "curl -fsSL http://sources.internal/objects/setup.sh | sh",
            rendered,
        )
        self.assertEqual(injected_shell_runs(rendered), [])

    def test_render_preserves_source_override_in_persisted_run_heredoc(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN cat <<'EOF' > /usr/local/bin/fetch\n"
                "#!/bin/sh\n"
                "curl -fsSL https://packages.example/setup.sh | sh\n"
                "EOF\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/setup.sh": (
                    "http://sources.internal/objects/setup.sh"
                )
            },
        )

        self.assertIn(
            "curl -fsSL https://packages.example/setup.sh | sh", rendered
        )
        self.assertNotIn("sources.internal/objects/setup.sh", rendered)
        self.assertEqual(injected_shell_runs(rendered), [])

    def test_render_rewrites_source_override_in_shell_command_heredoc(
        self,
    ) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN /bin/bash <<'EOF'\n"
                "curl -fsSL https://packages.example/setup.sh | sh\n"
                "EOF\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/setup.sh": (
                    "http://sources.internal/objects/setup.sh"
                )
            },
        )

        self.assertIn(
            "curl -fsSL http://sources.internal/objects/setup.sh | sh",
            rendered,
        )
        self.assertNotIn("https://packages.example/setup.sh", rendered)

    def test_render_rewrites_apt_source_data_heredoc_for_build_only(
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
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

        task_heredoc = rendered[
            rendered.index("RUN cat <<'EOF'") : rendered.index("EOF\n")
        ]
        self.assertIn("sources.internal/apt/vendor", task_heredoc)
        self.assertNotIn("packages.example/repository", task_heredoc)
        self.assertIn(
            "https://packages.example/repository",
            injected_shell_runs(rendered)[-1][1],
        )

    def test_persisted_run_heredoc_does_not_mark_stage_as_apt(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM registry.internal/opaque:latest\n"
                "RUN cat <<'EOF' > /usr/local/bin/install-later\n"
                "#!/bin/sh\n"
                "apt-get update\n"
                "EOF\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://apt-mirror.internal/repos",
        )

        self.assertEqual(injected_shell_runs(rendered), [])

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
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
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
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
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
                )

        command = popen.call_args.args[0]
        self.assertIn("--provenance=false", command)

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
                apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
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
