"""Select build sources from the configured dependency-gateway HTTP service."""

from __future__ import annotations

import argparse
import copy
import json
from http.client import HTTPException
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .registry import log
from .source_urls import validate_source_url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def gateway_urls(value: str, build_network: str) -> tuple[str, str]:
    """Accept the service origin or its documented /v1/cache root."""
    value = validate_source_url(value.rstrip("/"), "dependency gateway", build_network)
    parsed = urlsplit(value)
    if parsed.path not in {"", "/v1/cache"}:
        raise ValueError("dependency gateway URL must be an origin or end in /v1/cache")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin + "/v1/cache", origin


def gateway_available(origin: str, timeout: float) -> bool:
    """Bounded, direct liveness probe; never send internal traffic to a proxy."""
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(Request(origin + "/healthz"), timeout=timeout) as response:
            document = json.loads(response.read(4096))
            return (
                response.status == 200
                and isinstance(document, dict)
                and document.get("status") == "ok"
            )
    except (OSError, HTTPException, ValueError):
        return False


def gateway_source_overrides(cache: str, origin: str) -> dict[str, str]:
    """Routes from third_party/dependency-gateway's public client contract."""
    return {
        "pip_index_url": cache + "/pypi-simple",
        "npm_registry": cache + "/npm-registry",
        "goproxy": cache + "/go-proxy,direct",
        "gosumdb": "sum.golang.org " + cache + "/go-sumdb",
        "cargo_registry_url": "sparse+" + cache + "/cargo-index/",
        "rustup_dist_server": cache + "/rustup-dist",
        "rustup_update_root": cache + "/rustup-update",
        "rustup_init_url": cache + "/rustup-init/rustup-init.sh",
        "pytorch_index_url": cache + "/pytorch",
        "pub_hosted_url": cache + "/dart-pub",
        "julia_pkg_server": cache + "/julia-pkg",
        "conda_defaults_url": cache + "/conda-defaults",
        "conda_channels_url": cache + "/conda-channels",
        "github_mirror_url": origin + "/v1/git/github/",
        "download_source_url": cache,
    }


class GatewaySources:
    """Select Gateway routes before preparation without mutating caller settings."""

    def __init__(self, args: argparse.Namespace):
        self.timeout = getattr(args, "dependency_gateway_timeout_sec", 5.0)

    def select(self, args: argparse.Namespace) -> argparse.Namespace:
        value = getattr(args, "dependency_gateway_url", "").strip()
        if not value or args.dry_run:
            return args
        if not 0 < self.timeout <= 60:
            raise ValueError(
                "dependency gateway timeout must be greater than 0 and at most 60 seconds"
            )
        cache, origin = gateway_urls(
            value, getattr(args, "build_network", "default")
        )
        if not gateway_available(origin, self.timeout):
            log("dependency-gateway unavailable; retaining original build sources")
            return args
        selected = copy.deepcopy(args)
        for name, value in gateway_source_overrides(cache, origin).items():
            setattr(selected, name, value)
        log(
            "dependency-gateway reachable; using Gateway package, Git, APT and download sources"
        )
        return selected
