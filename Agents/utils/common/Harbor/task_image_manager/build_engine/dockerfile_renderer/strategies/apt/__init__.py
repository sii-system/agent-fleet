"""APT runtime secret rendering."""

from __future__ import annotations

import hashlib
from pathlib import Path

APT_RUNTIME_ASSET_NAMES = (
    "apt-wrapper.sh",
    "source-rewriter.awk",
)
APT_GATEWAY_ROOT_SECRET_PREFIX = "opensandbox-apt-gateway-root"
APT_RUNTIME_ASSET_DIR = Path(__file__).resolve().parent


def apt_gateway_root_content(dynamic_gateway_root: str) -> str:
    """Render the one endpoint needed for deterministic APT route generation."""
    return f"{dynamic_gateway_root.rstrip('/')}\n" if dynamic_gateway_root else ""


def apt_gateway_root_secret_id(dynamic_gateway_root: str) -> str:
    digest = hashlib.sha256(
        apt_gateway_root_content(dynamic_gateway_root).encode()
    ).hexdigest()
    return f"{APT_GATEWAY_ROOT_SECRET_PREFIX}-{digest[:16]}"


def apt_runtime_asset_digest() -> str:
    """Hash runtime helpers for BuildKit cache invalidation only.

    This digest must never feed task image identity. Identity must come only
    from the original static task environment; violating that invariant is a
    P0 bug.
    """
    digest = hashlib.sha256()
    for name in APT_RUNTIME_ASSET_NAMES:
        path = APT_RUNTIME_ASSET_DIR / name
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"Harbor task image APT runtime asset is unavailable: {path}"
            ) from exc
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(len(content)).encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def apt_runtime_secret_ids() -> dict[str, str]:
    """Return content-addressed IDs so secret changes invalidate RUN cache."""
    suffix = apt_runtime_asset_digest()
    return {
        "wrapper": f"opensandbox-apt-wrapper-{suffix}",
        "rewriter": f"opensandbox-apt-source-rewriter-{suffix}",
    }


def materialize_apt_runtime_assets(
    destination: Path,
    dynamic_gateway_root: str,
) -> dict[str, Path]:
    """Write host-side BuildKit secrets; none are included in image layers."""
    destination.mkdir(parents=True, exist_ok=True)
    assets = {name: APT_RUNTIME_ASSET_DIR / name for name in APT_RUNTIME_ASSET_NAMES}
    secret_ids = apt_runtime_secret_ids()
    gateway_path = destination / "gateway-root"
    gateway_path.write_text(
        apt_gateway_root_content(dynamic_gateway_root),
        encoding="utf-8",
    )
    return {
        secret_ids["wrapper"]: assets["apt-wrapper.sh"],
        secret_ids["rewriter"]: assets["source-rewriter.awk"],
        apt_gateway_root_secret_id(dynamic_gateway_root): gateway_path,
    }
