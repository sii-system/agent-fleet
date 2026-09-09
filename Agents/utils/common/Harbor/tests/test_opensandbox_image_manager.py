import hashlib
import io
import json
import os
import shutil
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
    apt_runtime_asset_digest,
    apt_runtime_mounts,
    apt_runtime_secret_ids,
    apt_runtime_source_overrides,
    apt_source_map_content,
    check_task_repository,
    environment_content_hash,
    github_mirror_config_content,
    materialize_apt_runtime_assets,
    mirror_image_ref,
    normalize_oci_image_config,
    oci_archive_image_config,
    package_source_build_args,
    package_source_hosts,
    parse_apt_source_overrides,
    parse_args,
    prepare,
    prepare_bundle,
    proxy_build_args,
    render_build_dockerfile,
    run_build,
    schema2_manifest,
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

    def test_runtime_asset_content_changes_digest_mounts_and_secret_ids(
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
                first_mounts = apt_runtime_mounts({})
                first_secret_ids = apt_runtime_secret_ids()
                (asset_dir / "apt-wrapper.sh").write_text(
                    "apt-wrapper.sh:second\n", encoding="utf-8"
                )
                second_digest = apt_runtime_asset_digest()
                second_mounts = apt_runtime_mounts({})
                second_secret_ids = apt_runtime_secret_ids()

        self.assertNotEqual(first_digest, second_digest)
        self.assertNotEqual(first_mounts, second_mounts)
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

    def test_render_uses_runtime_path_and_preserves_stage_alias(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04 AS builder\n"
                "RUN apt-get update\n"
                "FROM builder\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
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
            "export PATH=/run/opensandbox-apt/bin:$PATH; apt-get update",
            rendered,
        )
        self.assertIn("target=/run/opensandbox-apt/bin/apt-get", rendered)
        self.assertNotIn("target=/usr/bin/apt-get", rendered)
        self.assertNotIn("type=bind", rendered)
        self.assertIn("FROM builder\n", rendered)
        self.assertNotIn("docker.io/library/builder", rendered)
        self.assertEqual(rendered.count("ARG NPM_CONFIG_REGISTRY"), 2)
        self.assertEqual(rendered.count("ARG PIP_INDEX_URL"), 2)

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
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
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

    def test_github_mirror_mount_remains_on_shell_form_run(self) -> None:
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
        self.assertIn("target=/etc/gitconfig,mode=0444,required=true", rendered)
        self.assertIn("target=/run/opensandbox-apt/bin/apt-get", rendered)
        self.assertNotIn(mirror, rendered)

    def test_github_mirror_mount_preserves_exec_form_without_apt_runtime(
        self,
    ) -> None:
        source = (
            "FROM ubuntu:24.04\n"
            'RUN ["git", "clone", "https://github.com/foo/bar.git"]\n'
        )
        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
            github_mirror_config_mount_id="opensandbox-github-mirror-gitconfig",
        )

        self.assertIn(
            "RUN --mount=type=secret,"
            "id=opensandbox-github-mirror-gitconfig,"
            "target=/etc/gitconfig,mode=0444,required=true "
            '["git", "clone", "https://github.com/foo/bar.git"]',
            rendered,
        )
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
            apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
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
        self, source: str, source_kind: str, overrides: dict[str, str]
    ) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / f"source.{source_kind}"
            map_path = root / "map.tsv"
            seen_path = root / "seen"
            source_path.write_text(source, encoding="utf-8")
            map_path.write_text(apt_source_map_content(overrides), encoding="utf-8")
            seen_path.touch()
            completed = subprocess.run(
                [
                    "awk",
                    "-v",
                    f"map_file={map_path}",
                    "-v",
                    f"seen_file={seen_path}",
                    "-v",
                    f"source_kind={source_kind}",
                    "-v",
                    "display_file=/etc/apt/sources.list.d/test",
                    "-f",
                    str(APT_RUNTIME_ASSET_DIR / "source-rewriter.awk"),
                    str(source_path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
        return completed.stdout, completed.stderr

    def test_apt_source_overrides_normalize_trailing_slashes(self) -> None:
        overrides = parse_apt_source_overrides(
            json.dumps(
                {
                    "https://packages.example.com/repository/": (
                        "http://sources.internal/apt/vendor/"
                    )
                }
            ),
            "host",
        )

        self.assertEqual(
            overrides,
            {
                "https://packages.example.com/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
        )

    def test_runtime_list_mapping_uses_longest_boundary_safe_prefix(self) -> None:
        overrides = {
            "https://apt.postgresql.org/pub/repos/apt": (
                "http://gateway/apt/postgresql"
            ),
            "https://download.docker.com": "http://gateway/apt/docker",
            "https://download.docker.com/linux": (
                "http://gateway/apt/docker-linux"
            ),
            "https://packages.example/repo": "http://gateway/apt/packages",
        }
        source = (
            "deb https://apt.postgresql.org/pub/repos/apt stable main\n"
            "deb [arch=amd64 signed-by=/keys/docker.gpg] "
            "https://download.docker.com/linux/ubuntu noble stable\n"
            "deb https://download.docker.com/repository-other stable main\n"
            "deb https://packages.example/repository-other stable main\n"
            "deb http://gateway/apt/docker/linux/debian stable main\n"
            "deb https://unmapped.example/repo stable main\n"
            "deb https://unmapped.example/repo stable main\n"
            "deb https://download.docker.com.evil/linux stable main\n"
            "deb https://download.docker.com:8443/linux stable main\n"
            "deb file:/srv/packages stable main\n"
            "deb cdrom:[Debian GNU/Linux] stable main\n"
            "# deb https://commented.example/repo stable main\n"
        )

        rewritten, warnings = self.rewrite_apt_source(source, "list", overrides)

        self.assertIn("deb http://gateway/apt/postgresql stable main", rewritten)
        self.assertIn(
            "[arch=amd64 signed-by=/keys/docker.gpg] "
            "http://gateway/apt/docker-linux/ubuntu noble stable",
            rewritten,
        )
        self.assertIn("http://gateway/apt/docker/repository-other", rewritten)
        self.assertIn("https://packages.example/repository-other", rewritten)
        self.assertIn("https://download.docker.com.evil/linux", rewritten)
        self.assertIn("https://download.docker.com:8443/linux", rewritten)
        self.assertIn("http://gateway/apt/docker/linux/debian", rewritten)
        self.assertEqual(warnings.count("source=https://unmapped.example/repo"), 1)
        self.assertIn(
            "source=https://packages.example/repository-other", warnings
        )
        self.assertIn("file=/etc/apt/sources.list.d/test", warnings)
        self.assertIn("source=https://download.docker.com.evil/linux", warnings)
        self.assertIn("source=https://download.docker.com:8443/linux", warnings)
        self.assertNotIn("commented.example", warnings)
        self.assertNotIn("source=file:", warnings)
        self.assertNotIn("source=cdrom:", warnings)
        self.assertNotIn("source=http://gateway", warnings)

    def test_runtime_deb822_maps_multiple_uris_and_skips_disabled_stanza(self) -> None:
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
            {
                "https://one.example/repo": "http://gateway/apt/one",
                "https://two.example/base": "http://gateway/apt/two",
            },
        )

        self.assertIn(
            "URIs: http://gateway/apt/one http://gateway/apt/two/child",
            rewritten,
        )
        self.assertIn("Suites: noble noble-updates", rewritten)
        self.assertIn("Signed-By: /keys/example.gpg", rewritten)
        self.assertIn("file:/srv/packages", rewritten)
        self.assertNotIn("source=file:", warnings)
        self.assertIn("URIs: https://disabled.example/repo", rewritten)
        self.assertNotIn("disabled.example", warnings)

    def test_renderer_gives_shell_runs_the_same_path_runtime(self) -> None:
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
            apt_mirror="http://gateway/apt",
        )

        self.assertEqual(rendered.count("target=/run/opensandbox-apt/bin/apt,"), 5)
        self.assertEqual(
            rendered.count("target=/run/opensandbox-apt/bin/apt-get,"), 5
        )
        self.assertEqual(
            rendered.count("export PATH=/run/opensandbox-apt/bin:$PATH;"), 5
        )
        self.assertIn('RUN ["apt-get", "update"]', rendered)
        self.assertIn('RUN ["/usr/bin/apt-get", "update"]', rendered)
        self.assertNotIn("run-with-apt-path\", \"apt-get", rendered)
        self.assertNotIn("target=/usr/bin/apt", rendered)
        self.assertNotIn("type=bind", rendered)

    def test_renderer_preserves_multiline_exec_run_as_bypass(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                'RUN ["apt-get", \\\n'
                '     "update"]\n'
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://gateway/apt",
        )

        self.assertIn('RUN ["apt-get", \\\n     "update"]', rendered)
        self.assertNotIn("target=/run/opensandbox-apt", rendered)
        self.assertNotIn("run-with-apt-path", rendered)
        self.assertNotIn("export PATH=", rendered)

    def test_renderer_preserves_all_run_options_before_shell_runtime(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM ubuntu:24.04\n"
                "RUN --mount=type=cache,target=/tmp/cache "
                "--network=none --security=sandbox "
                "--device=nvidia.com/gpu=all echo hello\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://gateway/apt",
            github_mirror_config_mount_id="opensandbox-github-mirror-gitconfig",
        )
        run_line = next(
            line for line in rendered.splitlines() if line.startswith("RUN ")
        )

        for option in (
            "--mount=type=cache,target=/tmp/cache",
            "--network=none",
            "--security=sandbox",
            "--device=nvidia.com/gpu=all",
            "target=/etc/gitconfig,mode=0444,required=true",
            "target=/run/opensandbox-apt/shadow",
        ):
            self.assertLess(run_line.index(option), run_line.index("export PATH="))
        self.assertIn(
            "export PATH=/run/opensandbox-apt/bin:$PATH; echo hello", run_line
        )
        self.assertNotIn("; --device=", run_line)

    def test_runtime_path_is_mechanical_not_apt_detection(self) -> None:
        rendered = render_build_dockerfile(
            (
                "FROM alpine:3.20\n"
                "RUN echo no-package-manager-use\n"
            ),
            dockerhub_mirror_prefix="m.daocloud.io/docker.io",
            apt_mirror="http://gateway/apt",
        )

        self.assertIn(
            "export PATH=/run/opensandbox-apt/bin:$PATH; "
            "echo no-package-manager-use",
            rendered,
        )

    def test_runtime_wrapper_assets_are_standalone_and_use_current_rootfs_apt(
        self,
    ) -> None:
        wrapper = (APT_RUNTIME_ASSET_DIR / "apt-wrapper.sh").read_text(
            encoding="utf-8"
        )
        runner = (APT_RUNTIME_ASSET_DIR / "run-with-apt-path.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("real=/usr/bin/$command_name", wrapper)
        self.assertIn("Dir::Etc::SourceList=$source_view/sources.list", wrapper)
        self.assertIn("event=index-reconciliation-failed", wrapper)
        self.assertIn("indextargets --no-release-info", wrapper)
        self.assertIn("event=unmapped-source", (
            APT_RUNTIME_ASSET_DIR / "source-rewriter.awk"
        ).read_text(encoding="utf-8"))
        self.assertIn('${APT_CONFIG:-}', wrapper)
        self.assertIn("*Dir::Etc=*", wrapper)
        self.assertIn("PATH=/run/opensandbox-apt/bin:$PATH", runner)
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
            apt_mirror="http://gateway/apt",
            apt_source_overrides={
                "https://packages.example/repo": "http://gateway/apt/vendor"
            },
        )

        self.assertIn(
            "deb https://packages.example/repo stable main", rendered
        )
        self.assertIn(
            "export PATH=/run/opensandbox-apt/bin:$PATH; "
            "/usr/bin/apt-get --version",
            rendered,
        )
        self.assertNotIn(
            "deb http://gateway/apt/vendor stable main", rendered
        )

    @unittest.skipUnless(
        os.environ.get("RUN_OPENSANDBOX_APT_BUILD_TEST") == "1"
        and shutil.which("docker"),
        "set RUN_OPENSANDBOX_APT_BUILD_TEST=1 for the real BuildKit integration",
    )
    def test_runtime_path_wrapper_real_build(self) -> None:
        base_image = os.environ.get(
            "OPENSANDBOX_APT_TEST_BASE_IMAGE",
            "harbor-sandbox.lg.shzhisuan.com/public-mirror/library/python:3.11-slim",
        )
        source = fr'''FROM {base_image}
RUN cat <<'FAKE_APT' > /tmp/fake-apt
#!/bin/sh
shadow=
parts=
for argument in "$@"; do
    case "$argument" in
        Dir::Etc::SourceList=*) shadow=${{argument#*=}} ;;
        Dir::Etc::SourceParts=*) parts=${{argument#*=}} ;;
    esac
done
if [ -n "$parts" ] && grep -q 'deb http://cache.test/apt/vendor stable main' "$parts/runtime.list"; then
    state=shadow-cache
    index_path=/var/lib/apt/lists/cache.test_apt_vendor_dists_stable_main_binary-amd64_Packages
    release_path=/var/lib/apt/lists/cache.test_apt_vendor_dists_stable
else
    state=no-shadow
    index_path=/var/lib/apt/lists/example.test_repo_dists_stable_main_binary-amd64_Packages
    release_path=/var/lib/apt/lists/example.test_repo_dists_stable
fi
case " $* " in
    *" indextargets "*)
        key="$parts/runtime.list:1~Packages~stable~main~amd64~Packages~main/binary-amd64/Packages"
        printf '%s|main|%s\n' "$key" "$index_path"
        exit 0
        ;;
    *" update "*)
        mkdir -p /var/lib/apt/lists
        printf 'package-index\n' > "$index_path.lz4"
        for release_file in InRelease Release Release.gpg; do
            printf '%s\n' "$release_file" > "${{release_path}}_${{release_file}}"
        done
        ;;
esac
if [ -n "$parts" ]; then cat "$parts/runtime.list" >> /tmp/apt-shadow-observed; fi
printf '%s %s args=%s\n' "${{0##*/}}" "$state" "$*" >> /tmp/apt-calls
FAKE_APT
RUN chmod +x /tmp/fake-apt && cp /tmp/fake-apt /usr/bin/apt-get && cp /tmp/fake-apt /usr/bin/apt
RUN printf '%s\n' 'deb https://example.test/repo stable main' > /etc/apt/sources.list.d/runtime.list && apt-get update
RUN apt update
RUN apt install package
RUN sh -c 'apt-get install package'
RUN printf '#!/bin/sh\napt-get update\n' > /tmp/install.sh && chmod +x /tmp/install.sh && /tmp/install.sh
RUN python3 -c 'import subprocess; subprocess.run(["apt-get", "update"], check=True)'
RUN APT_CONFIG=/tmp/custom-apt.conf apt-get env-config-bypass
RUN apt-get -o Dir::Etc=/tmp/custom-apt cli-layout-bypass
RUN ["apt-get", "exec-form-bypass"]
RUN /usr/bin/apt-get absolute-path
RUN cat /tmp/apt-calls && cat /tmp/apt-shadow-observed && test "$(grep -c 'shadow-cache' /tmp/apt-calls)" -eq 6 && test "$(grep -c 'no-shadow' /tmp/apt-calls)" -eq 4 && grep -q 'args=env-config-bypass' /tmp/apt-calls && grep -q 'args=-o Dir::Etc=/tmp/custom-apt cli-layout-bypass' /tmp/apt-calls && grep -q 'args=exec-form-bypass' /tmp/apt-calls && grep -q 'args=absolute-path' /tmp/apt-calls && grep -q 'deb https://example.test/repo stable main' /etc/apt/sources.list.d/runtime.list && test -f /var/lib/apt/lists/example.test_repo_dists_stable_main_binary-amd64_Packages.lz4 && test -f /var/lib/apt/lists/example.test_repo_dists_stable_InRelease && test -f /var/lib/apt/lists/example.test_repo_dists_stable_Release && test -f /var/lib/apt/lists/example.test_repo_dists_stable_Release.gpg
'''
        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="",
            apt_mirror="http://cache.test/apt",
            apt_source_overrides={
                "https://example.test/repo": "http://cache.test/apt/vendor"
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text(rendered, encoding="utf-8")
            runtime_overrides = apt_runtime_source_overrides(
                "http://cache.test/apt",
                {
                    "https://example.test/repo": (
                        "http://cache.test/apt/vendor"
                    )
                },
            )
            log_path = root / "build.log"
            try:
                run_build(
                    environment_dir=root,
                    dockerfile=dockerfile,
                    archive_path=root / "image.oci.tar",
                    log_path=log_path,
                    platform="linux/amd64",
                    timeout_sec=180,
                    build_args={},
                    secret_files=materialize_apt_runtime_assets(
                        root / "apt-runtime", runtime_overrides
                    ),
                )
            except RuntimeError as exc:
                self.fail(f"{exc}\n{log_path.read_text(encoding='utf-8')}")

            with tarfile.open(root / "image.oci.tar", "r") as archive:
                _manifest, descriptors = schema2_manifest(archive)
                for descriptor in descriptors[1:]:
                    digest = str(descriptor["digest"]).split(":", 1)[1]
                    layer = archive.extractfile(f"blobs/sha256/{digest}")
                    self.assertIsNotNone(layer)
                    with tarfile.open(fileobj=layer, mode="r|*") as layer_archive:
                        self.assertFalse(
                            any(
                                member.name.lstrip("./").startswith(
                                    "run/opensandbox-apt"
                                )
                                for member in layer_archive
                            )
                        )

    @unittest.skipUnless(
        os.environ.get("RUN_OPENSANDBOX_APT_BUILD_TEST") == "1"
        and shutil.which("docker"),
        "set RUN_OPENSANDBOX_APT_BUILD_TEST=1 for the real BuildKit integration",
    )
    def test_runtime_update_indexes_work_with_original_sources(self) -> None:
        base_image = os.environ.get(
            "OPENSANDBOX_APT_TEST_BASE_IMAGE",
            "harbor-sandbox.lg.shzhisuan.com/public-mirror/library/python:3.11-slim",
        )
        original_repo = "http://original.invalid:18765/repo"
        runtime_repo = "http://127.0.0.1:18765/repo"
        source = f"FROM {base_image}\n" + r'''RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources && printf '%s\n' 'deb [trusted=yes] http://original.invalid:18765/repo ./' > /etc/apt/sources.list.d/probe.list
RUN mkdir -p /tmp/apt-repo/repo && printf 'Package: opensandbox-index-probe\nVersion: 1.0\nArchitecture: all\nMaintainer: OpenSandbox Test <noreply@example.invalid>\nFilename: pool/probe.deb\nSize: 0\nMD5sum: d41d8cd98f00b204e9800998ecf8427e\nSHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\nDescription: index reconciliation probe\n\n' > /tmp/apt-repo/repo/Packages; python3 -m http.server 18765 --bind 127.0.0.1 --directory /tmp/apt-repo >/tmp/apt-http.log 2>&1 & server=$!; trap 'kill "$server" 2>/dev/null || :' EXIT; ready=0; for attempt in 1 2 3 4 5 6 7 8 9 10; do if python3 -c 'import socket; socket.create_connection(("127.0.0.1", 18765), 0.2).close()'; then ready=1; break; fi; sleep 0.2; done; test "$ready" -eq 1; apt-get update
RUN ["/bin/sh", "-c", "grep -F 'deb [trusted=yes] http://original.invalid:18765/repo ./' /etc/apt/sources.list.d/probe.list && test ! -e /run/opensandbox-apt/bin/apt-get && apt-cache policy opensandbox-index-probe | grep -F '1.0'"]
'''
        rendered = render_build_dockerfile(
            source,
            dockerhub_mirror_prefix="",
            apt_mirror="http://cache.test/apt",
            apt_source_overrides={original_repo: runtime_repo},
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text(rendered, encoding="utf-8")
            log_path = root / "build.log"
            try:
                run_build(
                    environment_dir=root,
                    dockerfile=dockerfile,
                    archive_path=root / "image.oci.tar",
                    log_path=log_path,
                    platform="linux/amd64",
                    timeout_sec=180,
                    build_args={},
                    secret_files=materialize_apt_runtime_assets(
                        root / "apt-runtime",
                        apt_runtime_source_overrides(
                            "http://cache.test/apt",
                            {original_repo: runtime_repo},
                        ),
                    ),
                )
            except RuntimeError as exc:
                self.fail(f"{exc}\n{log_path.read_text(encoding='utf-8')}")


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

    def test_render_rewrites_object_fetch_in_opaque_stage(
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

        task_run = next(line for line in rendered.splitlines() if "curl" in line)
        self.assertIn(
            "curl -fsSL http://sources.internal/apt/vendor", task_run
        )
        self.assertIn(
            "printf %s https://packages.example/repository", task_run
        )
        self.assertNotIn(
            "printf %s http://sources.internal/apt/vendor", task_run
        )

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

    def test_render_never_rewrites_authored_apt_source(self) -> None:
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
        self.assertIn(
            "curl -fsSL http://sources.internal/objects/bazel-key.gpg",
            rendered,
        )
        self.assertIn(
            "deb https://packages.example/bazel stable main", rendered
        )
        self.assertIn("DOCKER_RUN_EOF\n", rendered)
        self.assertNotIn("target=/usr/bin/apt", rendered)

    def test_render_rewrites_object_fetch_in_command_heredoc(
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
            apt_mirror="http://apt-mirror.internal/repos",
            apt_source_overrides={
                "https://packages.example/repository": (
                    "http://sources.internal/apt/vendor"
                )
            },
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
            apt_mirror="http://apt-mirror.internal/repos",
        )

        self.assertIn("apt-get update", rendered)
        self.assertIn("export PATH=/run/opensandbox-apt/bin:$PATH;", rendered)

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
