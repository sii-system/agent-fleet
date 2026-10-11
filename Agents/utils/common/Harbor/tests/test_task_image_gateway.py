"""Gateway source selection and fallback without external network access."""

import argparse
import copy
import json
import sys
import tempfile
import unittest
from http.client import HTTPException
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))
from task_image_manager import dependency_gateway as gateway
from task_image_manager import task_preparation
from task_image_manager.build_engine import package_source_build_args, proxy_build_args
from task_image_manager.task_cli import parse_args


class GatewaySourcesTest(unittest.TestCase):
    def args(self, **values):
        fields = {
            "dependency_gateway_url": "https://gateway.example/v1/cache/",
            "dependency_gateway_timeout_sec": 2.0,
            "dry_run": False,
            "build_network": "host",
            "pip_index_url": "https://original.example/simple",
            "download_source_url": "https://original.example/cache",
            "dockerhub_mirror_prefix": "mirror.example/docker.io",
            "base_image_registry": "registry.example/base",
        }
        fields.update(values)
        return argparse.Namespace(**fields)

    def test_healthy_gateway_sets_supported_routes_without_mutating_original(self):
        args = self.args()
        original = copy.deepcopy(vars(args))
        with patch.object(gateway, "gateway_available", return_value=True) as probe:
            selected = gateway.GatewaySources(args).select(args)
        probe.assert_called_once_with("https://gateway.example", 2.0)
        self.assertEqual(vars(args), original)
        self.assertEqual(
            selected.pip_index_url, "https://gateway.example/v1/cache/pypi-simple"
        )
        self.assertEqual(
            selected.github_mirror_url, "https://gateway.example/v1/git/github/"
        )
        self.assertEqual(
            selected.download_source_url, "https://gateway.example/v1/cache"
        )
        self.assertEqual(
            selected.cargo_registry_url,
            "sparse+https://gateway.example/v1/cache/cargo-index/",
        )
        self.assertEqual(selected.dockerhub_mirror_prefix, args.dockerhub_mirror_prefix)
        self.assertEqual(selected.base_image_registry, args.base_image_registry)
        build_args = package_source_build_args(selected, "host")
        self.assertEqual(
            build_args["GOSUMDB"],
            "sum.golang.org https://gateway.example/v1/cache/go-sumdb",
        )
        self.assertEqual(
            build_args["PUB_HOSTED_URL"], "https://gateway.example/v1/cache/dart-pub"
        )
        self.assertEqual(
            build_args["JULIA_PKG_SERVER"], "https://gateway.example/v1/cache/julia-pkg"
        )
        self.assertEqual(
            build_args["CONDA_DEFAULTS_URL"], "https://gateway.example/v1/cache/conda-defaults"
        )
        self.assertEqual(
            build_args["CONDA_CHANNELS_URL"], "https://gateway.example/v1/cache/conda-channels"
        )

    def test_unreachable_gateway_keeps_every_original_source(self):
        args = self.args()
        with patch.object(gateway, "gateway_available", return_value=False):
            selected = gateway.GatewaySources(args).select(args)
        self.assertEqual(vars(selected), vars(args))

    def test_disabled_and_dry_run_do_not_probe(self):
        for args in (self.args(dependency_gateway_url=""), self.args(dry_run=True)):
            with patch.object(gateway, "gateway_available") as probe:
                selected = gateway.GatewaySources(args).select(args)
            probe.assert_not_called()
            self.assertIs(selected, args)

    def test_origin_and_cache_root_are_equivalent(self):
        self.assertEqual(
            gateway.gateway_urls("https://gateway.example", "host"),
            gateway.gateway_urls("https://gateway.example/v1/cache/", "host"),
        )

    def test_invalid_or_build_unreachable_gateway_is_rejected_before_probe(self):
        for value, network in (
            ("https://user:secret@gateway.example/v1/cache", "host"),
            ("https://gateway.example/v1/cache?secret=value", "host"),
            ("https://gateway.example/unrecognized", "host"),
            ("http://127.0.0.1:8080/v1/cache", "default"),
        ):
            with (
                self.subTest(value=value),
                patch.object(gateway, "gateway_available") as probe,
            ):
                with self.assertRaises(ValueError):
                    gateway.GatewaySources(
                        self.args(dependency_gateway_url=value, build_network=network)
                    ).select(
                        self.args(dependency_gateway_url=value, build_network=network)
                    )
                probe.assert_not_called()

    def test_probe_requires_gateway_health_response_and_bypasses_proxies(self):
        with patch.object(gateway, "build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"status":"ok"}'
            self.assertTrue(gateway.gateway_available("https://gateway.example", 2))
            self.assertEqual(build.call_args.args[0].proxies, {})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://gateway.example/healthz")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 2)
            response.read.return_value = b"<html>not gateway</html>"
            self.assertFalse(gateway.gateway_available("https://gateway.example", 2))
            response.read.return_value = b'{"status":"error"}'
            self.assertFalse(gateway.gateway_available("https://gateway.example", 2))
            for error in (URLError("offline"), TimeoutError(), HTTPException("invalid response")):
                build.return_value.open.side_effect = error
                self.assertFalse(gateway.gateway_available("https://gateway.example", 2))

    def test_build_proxy_excludes_selected_gateway(self):
        from task_image_manager.build_engine import package_source_hosts

        with patch.object(gateway, "gateway_available", return_value=True):
            selected = gateway.GatewaySources(self.args()).select(self.args())
        hosts = package_source_hosts(
            package_source_build_args(selected, "host"), selected.download_source_url
        )
        with patch.dict(
            "os.environ", {"HTTP_PROXY": "http://proxy.example:8080"}, clear=True
        ):
            proxy = proxy_build_args(True, "host", hosts)
        self.assertIn("gateway.example", proxy["NO_PROXY"])

    def test_local_uploaded_bundle_hit_does_not_probe_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "task.toml").write_text("")
            args = parse_args(
                [
                    "--task-dir",
                    str(root),
                    "--registry",
                    "registry.example",
                    "--project",
                    "test",
                    "--task-repository",
                    "example",
                    "--dependency-gateway-url",
                    "https://gateway.example/v1/cache",
                    "--reuse-local-upload-cache",
                    "--skip-hash-verification",
                ]
            )
            with (
                patch.object(
                    task_preparation.UploadedBundleCache, "try_restore", return_value="cached"
                ),
                patch.object(gateway, "gateway_available") as probe,
            ):
                self.assertEqual(task_preparation.prepare_task_images(args), "cached")
            probe.assert_not_called()

    def test_mapped_package_routes_exist_in_vendored_gateway_config(self):
        config = (
            HARBOR_DIR.parents[3] / "third_party/dependency-gateway/config/sources.json"
        )
        if not config.is_file():
            self.skipTest("dependency-gateway submodule not initialized")
        names = {source["name"] for source in json.loads(config.read_text())["sources"]}
        overrides = gateway.gateway_source_overrides(
            "https://gateway.example/v1/cache", "https://gateway.example"
        )
        for value in overrides.values():
            if "/v1/cache/" in value:
                name = value.split("/v1/cache/", 1)[1].split("/", 1)[0].split(",", 1)[0]
                self.assertIn(name, names)
