"""Run one ALE phase in its own dependency environment against Harbor's VM."""

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from .source import REVISION, source_digest, validate_source


async def set_windows_resolution(sandbox, resolution):
    """Preserve ALE's display requirement; opt-in diagnostics record deviations."""
    import base64

    from ale_run.environments.providers.gcloud import _SET_RES_PY

    width, height = resolution
    encoded = base64.b64encode(_SET_RES_PY.encode()).decode()
    result = await sandbox.run_command(
        f'"{sandbox.python}" -c "import base64,sys;sys.argv=[\'display\',\'{width}\',\'{height}\'];'
        f'exec(base64.b64decode(\'{encoded}\'))"', timeout=60,
    )
    applied = result.returncode == 0 and "set_ok" in result.stdout
    if not applied:
        if os.environ.get("ALE_WINDOWS_RESOLUTION_STRICT") != "0":
            raise RuntimeError("ALE desktop resolution setup failed")
        print(f"[ale] WARN: ALE desktop resolution {width}x{height} not applied; "
              "explicit diagnostic mode continues at the guest's current mode",
              file=sys.stderr, flush=True)
    return {"requested": list(resolution), "applied": applied}


async def run(spec, phase, sandbox=None):
    source = validate_source(spec["source"])
    if spec["revision"] != REVISION or source_digest(source) != spec["source_sha256"]:
        raise ValueError("ALE source changed since task conversion")
    sys.path.insert(0, str(source))
    # Native imports/judges inherit operator credentials; guest requests bypass
    # HTTP proxies while external judge calls retain their normal configuration.
    host = urlparse(spec["endpoint"]).hostname
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(filter(None, [os.environ.get(key), host]))
    from ale_run.base_interface import SandboxHandle
    from ale_run.environments.images import get as get_image
    from ale_run.environments.providers.static import StaticProvider
    from ale_run.environments.task_data import select
    from ale_run.tasks.driver import TaskDriver

    image = get_image(spec["image_family"])
    sandbox = sandbox or SandboxHandle(id=spec["vm"], endpoint=spec["endpoint"], os=spec["os"], **image.sandbox_paths())
    session = StaticProvider({"endpoint": spec["endpoint"], "image": image.name}).open_session(sandbox)
    driver = TaskDriver(str(source / spec["task"]), session, variant=spec["variant"], os_type=spec["os"])
    if driver.task_info["os_type"] != spec["os"]:
        raise ValueError("Native ALE task OS differs from its Harbor environment")
    data = driver.task_info["task_data"]
    backend = select(spec["task_data_source"])
    try:
        if phase == "setup":
            setup = {"setup": "complete"}
            if spec["os"] == "windows" and spec.get("resolution"):
                setup["resolution"] = await set_windows_resolution(sandbox, spec["resolution"])
            if data.reference_dir:
                # Golden images must have encrypted references only. Remove any
                # stale plaintext reference for this variant before agent setup.
                await sandbox.rm([data.reference_dir])
            if data.requires_task_data:
                await backend.stage_input(sandbox, data, source=spec["task_data_source"])
            await driver.setup()
            return setup
        if not callable(driver._task_loader.get_evaluate_fn()):
            raise TypeError("Missing native ALE evaluator")
        if data.requires_task_data:
            await backend.stage_reference(sandbox, data, source=spec["task_data_source"])
        return await driver.evaluate()
    finally:
        await driver.close()
        with contextlib.suppress(Exception):
            session.computer.interface.force_close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.close(), timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("setup", "evaluate"))
    parser.add_argument("spec", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(json.loads(args.spec.read_text()), args.phase))
    args.result.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
