#!/usr/bin/env python3
"""Provision BrowseComp's private runtime and assets in the Fleet cache."""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from common import atomic_write_json, default_cache_root, default_source_root

BENCHMARK_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = BENCHMARK_DIR / "runtime"
INDEX_COMPLETE_MARKER = ".agent-fleet-complete.json"


def log(message: str) -> None:
    print(f"[BrowseComp] {message}", flush=True)


def digest(paths: list[Path], suffix: str) -> str:
    value = hashlib.sha256()
    for path in paths:
        value.update(path.read_bytes())
    value.update(suffix.encode())
    return value.hexdigest()


def execute(command: list[str], env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def has_nvidia_gpu() -> bool:
    command = shutil.which("nvidia-smi")
    if not command:
        return False
    return subprocess.run(
        [command, "-L"], capture_output=True, check=False
    ).returncode == 0


def ensure_runtime(
    cache_root: Path, local_judge: bool, with_torch: bool = True
) -> Path:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is unavailable; run ./scripts/setup.sh once")
    runtime_root = cache_root / "runtime"
    venv = runtime_root / "venv"
    python = venv / "bin" / "python"
    base_requirements = RUNTIME_DIR / "requirements.lock"
    local_judge_requirements = RUNTIME_DIR / "requirements-local-judge.lock"
    gpu_available = has_nvidia_gpu()
    if local_judge and not gpu_available:
        raise RuntimeError(
            "BROWSECOMP_JUDGE_MODE=local requires a visible NVIDIA GPU; "
            "use BROWSECOMP_JUDGE_MODE=openai on CPU hosts"
        )
    # vLLM always needs torch even when query embeddings come from a remote API.
    with_torch = with_torch or local_judge
    torch_index = os.environ.get("BROWSECOMP_TORCH_INDEX", "")
    if not torch_index and not gpu_available:
        torch_index = "https://download.pytorch.org/whl/cpu"
    base_expected = digest(
        [base_requirements],
        f"torch={'2.7.1' if with_torch else 'disabled'}\nindex={torch_index if with_torch else ''}",
    )
    base_stamp = runtime_root / "requirements.sha256"
    base_installed = (
        base_stamp.read_text(encoding="utf-8").strip()
        if base_stamp.is_file()
        else ""
    )
    local_expected = digest([local_judge_requirements], base_expected)
    local_stamp = runtime_root / "requirements-local-judge.sha256"
    local_installed = (
        local_stamp.read_text(encoding="utf-8").strip()
        if local_stamp.is_file()
        else ""
    )
    if (
        python.is_file()
        and base_installed == base_expected
        and (not local_judge or local_installed == local_expected)
    ):
        log(f"runtime ready: {venv}")
        return python

    runtime_root.mkdir(parents=True, exist_ok=True)
    if not python.is_file():
        log("creating managed Python 3.10 runtime (first run only)")
        execute([uv, "venv", "--python", "3.10", str(venv)])
        base_installed = ""
        local_installed = ""
    if base_installed != base_expected:
        log("installing pinned BrowseComp dependencies (first run only)")
        if with_torch:
            torch_command = [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "torch==2.7.1",
            ]
            if torch_index:
                torch_command.extend(["--index-url", torch_index])
            execute(torch_command)
        execute(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "-r",
                str(base_requirements),
            ]
        )
        base_stamp.write_text(base_expected + "\n", encoding="utf-8")
    if local_judge and local_installed != local_expected:
        log("installing the optional local vLLM judge runtime")
        execute(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "-r",
                str(local_judge_requirements),
            ]
        )
        local_stamp.write_text(local_expected + "\n", encoding="utf-8")
    execute([uv, "pip", "check", "--python", str(python)])
    return python


def cache_environment(cache_root: Path, offline: bool) -> dict[str, str]:
    env = os.environ.copy()
    hf_home = Path(env.get("HF_HOME", cache_root / "huggingface")).expanduser().resolve()
    env["HF_HOME"] = str(hf_home)
    env.setdefault("HF_DATASETS_CACHE", str(hf_home / "datasets"))
    if offline:
        env.update(
            HF_HUB_OFFLINE="1",
            HF_DATASETS_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
        )
    return env


PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def without_proxy(env: dict[str, str]) -> dict[str, str]:
    direct = env.copy()
    for name in PROXY_VARIABLES:
        direct.pop(name, None)
    return direct


def probe_huggingface(python: Path, env: dict[str, str]) -> bool:
    endpoint = env.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    probe_url = f"{endpoint}/api/datasets/Tevatron/browsecomp-plus"
    command = [
        str(python),
        "-c",
        (
            "import sys,urllib.request; "
            "r=urllib.request.urlopen(sys.argv[1],timeout=20); "
            "raise SystemExit(0 if r.status == 200 else 1)"
        ),
        probe_url,
    ]
    try:
        return subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
            check=False,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def resolve_hf_network(
    cache_root: Path, python: Path, offline: bool
) -> tuple[str, dict[str, str]]:
    env = cache_environment(cache_root, offline)
    if offline:
        return "offline", env
    requested = os.environ.get("BROWSECOMP_HF_PROXY_MODE", "auto").strip().lower()
    if requested not in {"auto", "inherit", "direct"}:
        raise RuntimeError(
            "BROWSECOMP_HF_PROXY_MODE must be auto, inherit, or direct"
        )
    if requested == "inherit":
        return "inherit", env
    if requested == "direct":
        return "direct", without_proxy(env)

    manifest = cache_root / "bootstrap.json"
    if manifest.is_file():
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8")).get(
                "hf_proxy_mode"
            )
            if cached == "inherit":
                return "inherit", env
            if cached == "direct":
                return "direct", without_proxy(env)
        except (OSError, json.JSONDecodeError):
            pass

    has_proxy = any(env.get(name) for name in PROXY_VARIABLES)
    if not has_proxy or probe_huggingface(python, env):
        log("Hugging Face network: inherit host proxy policy")
        return "inherit", env
    direct = without_proxy(env)
    if probe_huggingface(python, direct):
        log("Hugging Face network: direct (host proxy probe failed)")
        return "direct", direct
    raise RuntimeError(
        "Hugging Face is unreachable with both inherited proxy settings and "
        "direct access; set BROWSECOMP_HF_PROXY_MODE after fixing connectivity"
    )


def ensure_dataset(
    source_root: Path,
    cache_root: Path,
    ground_truth: Path,
    python: Path,
    offline: bool,
    network_env: dict[str, str],
) -> None:
    private_root = cache_root / "private"
    private_root.mkdir(parents=True, exist_ok=True)
    private_root.chmod(0o700)
    if ground_truth.is_file():
        if private_root in ground_truth.parents:
            ground_truth.chmod(0o600)
        log(f"dataset ready: {ground_truth}")
        return
    if offline:
        raise FileNotFoundError(f"offline dataset cache is missing: {ground_truth}")
    prepare_dataset = RUNTIME_DIR / "prepare_dataset.py"
    if not prepare_dataset.is_file():
        raise FileNotFoundError(f"dataset adapter is missing: {prepare_dataset}")
    ground_truth.parent.mkdir(parents=True, exist_ok=True)
    log("preparing benchmark questions with projected downloads (first run only)")
    execute(
        [
            str(python),
            str(prepare_dataset),
            "--source-root",
            str(source_root),
            "--output",
            str(ground_truth),
            "--generate-tsv",
            str(cache_root / "private" / "queries.tsv"),
        ],
        network_env,
    )
    ground_truth.chmod(0o600)


def ensure_index(
    cache_root: Path,
    index_path: str,
    variant: str,
    python: Path,
    offline: bool,
    network_env: dict[str, str],
) -> None:
    index_root = (cache_root / "indexes").resolve()
    fixed_prefix = Path(os.path.expanduser(index_path.split("*")[0])).resolve()
    managed = index_root in (fixed_prefix, *fixed_prefix.parents)
    if not managed:
        if glob.glob(index_path):
            log(f"retrieval index ready: {index_path}")
            return
        raise FileNotFoundError(f"configured retrieval index is missing: {index_path}")
    if managed_index_complete(index_root, variant):
        log(f"retrieval index ready: {index_path}")
        return
    log(f"downloading {variant} retrieval index (first run only)")
    command = [
        str(python),
        str(RUNTIME_DIR / "download_assets.py"),
        "--variant",
        variant,
        "--output-root",
        str(index_root),
    ]
    if offline:
        command.append("--offline")
    execute(command, network_env)
    if not managed_index_complete(index_root, variant):
        raise FileNotFoundError(
            f"download did not produce a complete index marker for: {index_path}"
        )


def ensure_remote_tokenizer(
    cache_root: Path,
    python: Path,
    model: str,
    revision: str | None,
    network_env: dict[str, str],
) -> None:
    """Cache the tokenizer before the API-backed MCP service starts.

    This keeps Hugging Face proxy policy confined to bootstrap.  The MCP
    process may then use a separate proxy policy for its embedding endpoint.
    """

    cache_root.mkdir(parents=True, exist_ok=True)
    marker = cache_root / "remote-tokenizer.json"
    expected = {
        "schema_version": 1,
        "model": model,
        "revision": revision,
    }
    try:
        if json.loads(marker.read_text(encoding="utf-8")) == expected:
            log(f"remote embedding tokenizer ready: {model}")
            return
    except (OSError, json.JSONDecodeError):
        pass
    log("caching remote embedding tokenizer (first run only)")
    execute(
        [
            str(python),
            "-c",
            (
                "from transformers import AutoTokenizer; import os,sys; "
                "AutoTokenizer.from_pretrained("
                "sys.argv[1], revision=(sys.argv[2] or None), "
                "cache_dir=os.environ.get('HF_HOME'), padding_side='left', "
                "local_files_only=os.environ.get('TRANSFORMERS_OFFLINE') == '1'"
                ")"
            ),
            model,
            revision or "",
        ],
        network_env,
    )
    atomic_write_json(marker, expected)


def managed_index_complete(index_root: Path, variant: str) -> bool:
    variant_root = index_root / variant
    marker = variant_root / INDEX_COMPLETE_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        files = payload["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return False
    if payload.get("schema_version") != 1 or payload.get("variant") != variant:
        return False
    if not isinstance(files, list) or not files:
        return False
    for item in files:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        size = item.get("size")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith("corpus.shard")
            or not name.endswith(".pkl")
            or not isinstance(size, int)
            or size <= 0
        ):
            return False
        path = variant_root / name
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=default_source_root())
    parser.add_argument("--cache-root", type=Path, default=default_cache_root())
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--index-path")
    parser.add_argument("--index-variant", default="qwen3-embedding-0.6b")
    parser.add_argument("--with-local-judge", action="store_true")
    parser.add_argument("--data-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    cache_root = args.cache_root.expanduser().resolve()
    embedding_backend = os.environ.get(
        "BROWSECOMP_EMBEDDING_BACKEND", "local"
    ).strip().lower()
    if embedding_backend not in {"local", "openai"}:
        raise RuntimeError("BROWSECOMP_EMBEDDING_BACKEND must be local or openai")
    default_model = "Qwen/Qwen3-Embedding-0.6B"
    default_model_revision = "c54f2e6e80b2d7b7de06f51cec4959f6b3e03418"
    tokenizer_model = os.environ.get("BROWSECOMP_TOKENIZER_MODEL") or (
        default_model
        if embedding_backend == "openai"
        else os.environ.get("BROWSECOMP_EMBEDDING_MODEL", default_model)
    )
    tokenizer_revision = os.environ.get("BROWSECOMP_TOKENIZER_REVISION") or (
        default_model_revision if tokenizer_model == default_model else None
    )
    ground_truth = (
        args.ground_truth.expanduser().resolve()
        if args.ground_truth
        else cache_root / "private" / "browsecomp_plus_decrypted.jsonl"
    )
    index_path = args.index_path or str(
        cache_root / "indexes" / args.index_variant / "corpus.shard*.pkl"
    )
    if not (source_root / "LICENSE").is_file() or not (
        source_root / "scripts_build_index" / "decrypt_dataset.py"
    ).is_file():
        raise FileNotFoundError(
            f"vendored BrowseComp snapshot is missing at {source_root}"
        )

    cache_root.mkdir(parents=True, exist_ok=True)
    with (cache_root / "bootstrap.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        python = ensure_runtime(
            cache_root,
            args.with_local_judge,
            with_torch=embedding_backend == "local",
        )
        hf_proxy_mode, network_env = resolve_hf_network(
            cache_root, python, args.offline
        )
        ensure_dataset(
            source_root,
            cache_root,
            ground_truth,
            python,
            args.offline,
            network_env,
        )
        if not args.data_only:
            ensure_index(
                cache_root,
                index_path,
                args.index_variant,
                python,
                args.offline,
                network_env,
            )
        if embedding_backend == "openai" and not args.data_only:
            ensure_remote_tokenizer(
                cache_root,
                python,
                tokenizer_model,
                tokenizer_revision,
                network_env,
            )
        manifest = {
            "schema_version": 1,
            "source_root": str(source_root),
            "cache_root": str(cache_root),
            "python": str(python),
            "ground_truth": str(ground_truth),
            "index_path": index_path,
            "index_variant": args.index_variant,
            "embedding_backend": embedding_backend,
            "tokenizer_model": tokenizer_model,
            "tokenizer_revision": tokenizer_revision,
            "offline": args.offline,
            "hf_proxy_mode": hf_proxy_mode,
        }
        atomic_write_json(cache_root / "bootstrap.json", manifest)
    if args.json:
        print(json.dumps(manifest, separators=(",", ":"), sort_keys=True))
    else:
        log("assets prepared; subsequent runs reuse this cache")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[BrowseComp][ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
