"""Local, source-addressed Dockerfile frontend preparation."""

import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent
CONTEXT_NAME = "opensandbox-instrumentation-frontend"
GITHUB_MIRROR_CONFIG_MOUNT_ID = "opensandbox-github-mirror-gitconfig"
DOWNLOAD_WRAPPER_SECRET_PREFIX = "opensandbox-download-wrapper"
DOWNLOAD_REWRITER_SECRET_PREFIX = "opensandbox-download-url-rewriter"
DOWNLOAD_SOURCE_SECRET_PREFIX = "opensandbox-download-source"
CONDA_CONFIG_SECRET_PREFIX = "opensandbox-conda-config"


def source_identity() -> str:
    digest = hashlib.sha256(platform.machine().encode())
    for name in (
        "upstream.version",
        "instrumentation.go",
        "Dockerfile",
        ".dockerignore",
        "scripts/build.sh",
        "patches/0001-opensandbox-run-instrumentation.patch",
    ):
        digest.update(name.encode() + b"\0" + (COMPONENT / name).read_bytes())
    return digest.hexdigest()


def layout_digest(layout: Path) -> str:
    """Check the local OCI graph before allowing a cache hit."""
    if json.loads((layout / "oci-layout").read_text())["imageLayoutVersion"] != "1.0.0":
        raise ValueError("unsupported local OCI layout version")
    index = json.loads((layout / "index.json").read_text())
    manifests = index["manifests"]
    if len(manifests) != 1:
        raise ValueError("expected one local frontend manifest")

    def verify(descriptor):
        algorithm, digest = descriptor["digest"].split(":", 1)
        if (
            algorithm != "sha256"
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid local frontend digest")
        data = (layout / "blobs" / algorithm / digest).read_bytes()
        if (
            len(data) != descriptor["size"]
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("corrupt local frontend blob")
        if "manifest" in descriptor["mediaType"] or "index" in descriptor["mediaType"]:
            document = json.loads(data)
            for child in document.get("manifests", []) + document.get("layers", []):
                verify(child)
            if "config" in document:
                verify(document["config"])

    verify(manifests[0])
    return manifests[0]["digest"]


class FrontendBuildError(RuntimeError):
    """Shared build-tool failure, not a dataset task failure."""


def ensure_frontend(github_mirror_url: str | None = None) -> tuple[Path, str]:
    try:
        return _ensure_frontend(github_mirror_url)
    except FrontendBuildError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        raise FrontendBuildError(f"local frontend preparation failed: {exc}") from exc


def _ensure_frontend(github_mirror_url: str | None) -> tuple[Path, str]:
    cache = (
        Path(
            os.environ.get("HARBOR_TASK_IMAGE_FRONTEND_CACHE")
            or "/data/harbor-runs/opensandbox-frontend"
        )
        .expanduser()
        .resolve()
    )
    cache.mkdir(parents=True, exist_ok=True)
    key = source_identity()
    work = cache / key
    with (cache / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return work / "layout", layout_digest(work / "layout")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        log = cache / (key + ".log")
        batch_dir = os.environ.get("HARBOR_TASK_IMAGE_PREBUILD_RUN_DIR")
        failure = (
            Path(batch_dir) / ("frontend-" + key + ".failed") if batch_dir else None
        )
        if failure is not None and failure.exists():
            raise FrontendBuildError(
                f"frontend preparation already failed in this batch; see {log}"
            )
        try:
            if work.exists():
                shutil.rmtree(work)
            print(
                f"Building local Harbor task image frontend (first build may take several minutes); log={log}",
                file=sys.stderr,
                flush=True,
            )
            env = {**os.environ, "FRONTEND_WORK_DIR": str(work)}
            if github_mirror_url is not None:
                env["HARBOR_TASK_IMAGE_GITHUB_MIRROR_URL"] = github_mirror_url
            with log.open("w") as output:
                process = subprocess.Popen(
                    ["bash", str(COMPONENT / "scripts/build.sh")],
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    code = process.wait(timeout=1500)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
            if code:
                raise FrontendBuildError(
                    f"local frontend build failed ({code}); see {log}"
                )
            return work / "layout", layout_digest(work / "layout")
        except Exception as exc:
            if failure is not None:
                failure.write_text(str(log) + "\n")
            if isinstance(exc, FrontendBuildError):
                raise
            raise FrontendBuildError(
                f"local frontend preparation failed; see {log}: {exc}"
            ) from exc


def prepare_frontend(
    secret_files: dict[str, Path],
    destination: Path,
    *,
    build_contexts: dict[str, str],
    github_mirror_url: str | None = None,
) -> dict[str, str]:
    args = {}
    for key, prefix in {
        "OPENSANDBOX_APT_WRAPPER": "opensandbox-apt-wrapper-",
        "OPENSANDBOX_APT_REWRITER": "opensandbox-apt-source-rewriter-",
        "OPENSANDBOX_APT_GATEWAY_ROOT": "opensandbox-apt-gateway-root-",
    }.items():
        matches = [name for name in secret_files if name.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"expected one content-addressed runtime secret for {key}")
        args[key] = matches[0]
    optional_runtime_args: dict[str, str] = {}
    optional_prefixes = {
        "OPENSANDBOX_DOWNLOAD_WRAPPER": DOWNLOAD_WRAPPER_SECRET_PREFIX + "-",
        "OPENSANDBOX_DOWNLOAD_REWRITER": DOWNLOAD_REWRITER_SECRET_PREFIX + "-",
        "OPENSANDBOX_DOWNLOAD_SOURCE": DOWNLOAD_SOURCE_SECRET_PREFIX + "-",
    }
    optional_matches = {
        key: [name for name in secret_files if name.startswith(prefix)]
        for key, prefix in optional_prefixes.items()
    }
    if any(optional_matches.values()):
        for key, matches in optional_matches.items():
            if len(matches) != 1:
                raise ValueError(
                    f"expected one content-addressed runtime secret for {key}"
                )
            optional_runtime_args[key] = matches[0]
    args.update(optional_runtime_args)
    layout, digest = ensure_frontend(github_mirror_url)
    # Docker's local frontend resolver can retain a previously loaded OCI
    # context behind a reused symbolic name even when the manifest digest has
    # changed. Make the context name itself content-addressed so a new frontend
    # implementation can never resolve through the old alias.
    context_name = f"{CONTEXT_NAME}-{digest.removeprefix('sha256:')}"
    build_contexts[context_name] = f"oci-layout://{layout}@{digest}"
    args["BUILDKIT_SYNTAX"] = context_name
    identity = "opensandbox-frontend-" + hashlib.sha256(digest.encode()).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    identity_path = destination / "frontend-identity"
    identity_path.write_text(digest + "\n")
    secret_files[identity] = identity_path
    args["OPENSANDBOX_FRONTEND_IDENTITY"] = identity
    git_config_matches = [
        name
        for name in secret_files
        if name.startswith(GITHUB_MIRROR_CONFIG_MOUNT_ID + "-")
    ]
    if len(git_config_matches) > 1:
        raise ValueError("expected at most one GitHub mirror runtime secret")
    if git_config_matches:
        args["OPENSANDBOX_GITHUB_MIRROR_CONFIG"] = git_config_matches[0]
    conda_config_matches = [
        name for name in secret_files if name.startswith(CONDA_CONFIG_SECRET_PREFIX + "-")
    ]
    if len(conda_config_matches) > 1:
        raise ValueError("expected at most one conda runtime config secret")
    if conda_config_matches:
        args["OPENSANDBOX_CONDA_CONFIG"] = conda_config_matches[0]
    return args
