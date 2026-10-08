"""Prepare a private, integrity-checked copy of the official WAA client."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

from .dataset import BENCHMARKS, CLIENT_PATH, digest, load_tasks


def lazy_exports(path):
    """Keep official export precedence; import only the modules a task uses."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    exports = {}
    retained = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            for alias in node.names:
                exports[alias.asname or alias.name] = (node.module, alias.name)
        else:
            retained.append(ast.get_source_segment(source, node))
    path.write_text(
        "from importlib import import_module\n"
        + "\n".join(retained) + "\n"
        + "_EXPORTS = " + repr(exports) + "\n"
        + "def __getattr__(name):\n"
        + "    if name not in _EXPORTS:\n        raise AttributeError(name)\n"
        + "    module, symbol = _EXPORTS[name]\n"
        + "    value = getattr(import_module('.' + module, __name__), symbol)\n"
        + "    globals()[name] = value\n    return value\n",
        encoding="utf-8",
    )


def prepare(source, destination, *, benchmark="waa-v2"):
    release = BENCHMARKS[benchmark]
    source, destination = Path(source).resolve(), Path(destination).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    if revision != release["revision"]:
        raise ValueError(f"WAA source must be pinned at {release['revision']}")
    changes = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all",
         "--", CLIENT_PATH, "LICENSE"], text=True,
    )
    if changes.strip():
        raise ValueError("WAA source contains modified or untracked client files")
    if destination.exists():
        validate_runtime(destination, benchmark=benchmark)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".preparing")
    staging.mkdir()
    try:
        client = staging / "client"
        client.mkdir()
        tracked = subprocess.check_output([
            "git", "-C", str(source), "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", CLIENT_PATH,
        ]).decode().split("\0")
        allowed = {"desktop_env", "evaluation_examples_windows", "mm_agents", "trajectory_recorder.py"}
        for name in filter(None, tracked):
            relative = Path(name).relative_to(CLIENT_PATH)
            if relative.parts[0] not in allowed:
                continue
            dst = client / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, dst)
        shutil.copyfile(source / "LICENSE", staging / "UPSTREAM_LICENSE")
        for package in ("metrics", "getters"):
            lazy_exports(client / "desktop_env/evaluators" / package / "__init__.py")
        # OCR is used by compare_image_text, which is outside test_all.json.
        # Defer the import to that unchanged evaluator instead of requiring a
        # CUDA/PyTorch stack just to import Writer's document metrics.
        docs = client / "desktop_env/evaluators/metrics/docs.py"
        text = docs.read_text(encoding="utf-8")
        if text.count("import easyocr\n") != 1 or text.count("    reader = easyocr.Reader") != 1:
            raise ValueError("Pinned WAA OCR import layout changed")
        docs.write_text(text.replace("import easyocr\n", "").replace(
            "    reader = easyocr.Reader", "    import easyocr\n    reader = easyocr.Reader",
        ), encoding="utf-8")
        load_tasks(client, benchmark=benchmark)
        files = {str(p.relative_to(staging)): digest(p) for p in sorted(staging.rglob("*")) if p.is_file()}
        (staging / "runtime.json").write_text(json.dumps({
            "version": 1, "benchmark": benchmark, "upstream_url": release["url"], "revision": revision,
            "patches": ["lazy-evaluator-exports", "defer-unused-ocr-import"], "files": files,
        }, indent=2) + "\n", encoding="utf-8")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def validate_runtime(root, *, benchmark="waa-v2"):
    root = Path(root).resolve()
    manifest_path = root / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("revision") != BENCHMARKS[benchmark]["revision"]:
        raise ValueError("Invalid or unpinned WAA runtime; run WAA setup")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Invalid WAA runtime checksums")
    for name, expected in files.items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or digest(path) != expected:
            raise ValueError(f"WAA runtime checksum mismatch: {name}")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p != manifest_path}
    if actual != set(files):
        raise ValueError("WAA runtime contains unexpected files")
    return manifest
