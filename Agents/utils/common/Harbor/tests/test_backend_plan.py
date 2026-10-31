"""BackendPlan compatibility, process handoff, and request isolation."""

import dataclasses
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.harbor_environment_adapters.core.config import (
    PLAN_ENV,
    BackendPlan,
    configured_backend_plan,
    resolve_backend_plan,
    rollout_backend_plan,
    runtime_backend_plan,
)

HARBOR_DIR = Path(__file__).resolve().parents[1]
LEGACY_SELECTION = HARBOR_DIR / "tests/fixtures/backend_plan/legacy_selection.sh"


class BackendPlanTests(unittest.TestCase):
    def test_selection_matches_frozen_shell_baseline(self):
        for backend, spec, template in itertools.product(
            ("", "docker", "e2b", "qz", "opensandbox", "custom:Environment"),
            ("", "custom:Env"),
            ("", "fake-template"),
        ):
            with self.subTest(backend=backend, spec=spec, template=template):
                config = {
                    "HARBOR_ENVIRONMENT_TYPE": backend,
                    "RL_ENVIRONMENT_TYPE": "qz",
                    "HARBOR_ENVIRONMENT_SPEC": spec,
                    "HARBOR_E2B_PREBUILT_TEMPLATE": template,
                    "HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC": "custom:Prebuilt",
                    "HARBOR_CC_WEB_MCP_ENABLED": "0",
                }
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; printf "%s\\n%s\\n" "$HARBOR_ENVIRONMENT_TYPE" "$HARBOR_ENVIRONMENT_SPEC"',
                        "bash",
                        str(LEGACY_SELECTION),
                    ],
                    env={"PATH": os.defpath, **config},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                plan = resolve_backend_plan(config)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [plan.environment_type, plan.environment_spec],
                )
        self.assertEqual(resolve_backend_plan({}).environment_spec, "docker")
        self.assertEqual(
            resolve_backend_plan(
                {
                    "HARBOR_ENVIRONMENT_TYPE": "e2b",
                    "HARBOR_E2B_PREBUILT_TEMPLATE": "fake",
                }
            ).environment_spec,
            "e2b_prebuilt:PrebuiltE2BEnvironment",
        )

    def test_rollout_matches_legacy_request_policy(self):
        # Freeze the worker's old branch policy independently of the resolver.
        legacy_worker = """
environment_type="$1"
export HARBOR_ENVIRONMENT_TYPE="$environment_type"
if [[ "$environment_type" == "e2b" && -n "${HARBOR_E2B_PREBUILT_TEMPLATE:-}" ]]; then
  export HARBOR_ENVIRONMENT_SPEC="$HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC"
elif [[ "$environment_type" == "opensandbox" ]]; then
  export HARBOR_ENVIRONMENT_SPEC="yicloud_opensandbox:YiCloudOpenSandboxEnvironment"
elif [[ "$environment_type" == "qz" ]]; then
  export HARBOR_ENVIRONMENT_SPEC="qz_e2b_sandbox:QzSandboxEnvironment"
elif [[ "$environment_type" != "${RL_ENVIRONMENT_TYPE:-docker}" ]]; then
  export HARBOR_ENVIRONMENT_SPEC="$environment_type"
fi
source "$2"
printf "%s\\n%s\\n" "$HARBOR_ENVIRONMENT_TYPE" "$HARBOR_ENVIRONMENT_SPEC"
"""
        for default, requested, template in itertools.product(
            ("docker", "e2b", "qz", "opensandbox"),
            ("docker", "e2b", "qz", "opensandbox"),
            ("", "fake-template"),
        ):
            with self.subTest(default=default, requested=requested, template=template):
                config = {
                    "RL_ENVIRONMENT_TYPE": default,
                    "HARBOR_ENVIRONMENT_TYPE": default,
                    "HARBOR_ENVIRONMENT_SPEC": "host:CustomEnvironment",
                    "HARBOR_E2B_PREBUILT_TEMPLATE": template,
                    "HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC": "custom:Prebuilt",
                    "HARBOR_CC_WEB_MCP_ENABLED": "0",
                }
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        legacy_worker,
                        "bash",
                        requested,
                        str(LEGACY_SELECTION),
                    ],
                    env={"PATH": os.defpath, **config},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                plan = rollout_backend_plan(config, requested)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [plan.environment_type, plan.environment_spec],
                )
                self.assertEqual(config["HARBOR_ENVIRONMENT_TYPE"], default)

    def test_plan_is_immutable_and_serializes_only_selection(self):
        config = {"HARBOR_ENVIRONMENT_TYPE": "qz", "SBX_API_KEY": "fake-secret"}
        plan = resolve_backend_plan(config)
        config["HARBOR_ENVIRONMENT_TYPE"] = "docker"
        self.assertEqual(plan.environment_type, "qz")
        self.assertEqual(
            set(json.loads(plan.to_json())),
            {"schema", "environment_type", "environment_spec"},
        )
        self.assertNotIn("fake-secret", plan.to_json())
        self.assertEqual(BackendPlan.from_json(plan.to_json()), plan)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.environment_type = "docker"

    def test_invalid_serialized_plan_fails_without_echoing_values(self):
        valid = json.loads(resolve_backend_plan({}).to_json())
        for value in (
            "fake-secret",
            "[]",
            json.dumps({**valid, "schema": 2}),
            json.dumps({**valid, "schema": True}),
            json.dumps({**valid, "environment_type": 5}),
            json.dumps({**valid, "secret": "fake-secret"}),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                BackendPlan.from_json(value)
            self.assertNotIn("fake-secret", str(error.exception))

    def test_inherited_plan_is_reused_and_legacy_changes_invalidate_it(self):
        plan = resolve_backend_plan({"HARBOR_ENVIRONMENT_TYPE": "qz"})
        config = {
            PLAN_ENV: plan.to_json(),
            "HARBOR_ENVIRONMENT_TYPE": "qz",
            "HARBOR_ENVIRONMENT_SPEC": plan.environment_spec,
        }
        self.assertEqual(configured_backend_plan(config), plan)
        config["HARBOR_ENVIRONMENT_SPEC"] = "host:Custom"
        self.assertEqual(
            configured_backend_plan(config).environment_spec, "host:Custom"
        )
        config.update(HARBOR_ENVIRONMENT_TYPE="docker", HARBOR_ENVIRONMENT_SPEC="")
        self.assertEqual(configured_backend_plan(config), resolve_backend_plan({}))

    def test_runtime_consumes_plan_and_preserves_direct_python_fallback(self):
        plan = resolve_backend_plan({"HARBOR_ENVIRONMENT_TYPE": "qz"})
        self.assertEqual(runtime_backend_plan({PLAN_ENV: plan.to_json()}), plan)
        self.assertEqual(
            runtime_backend_plan({"RL_ENVIRONMENT_TYPE": "qz"}).environment_type,
            "docker",
        )

    def test_dsec_uses_the_controller_adapter_for_fixed_and_rollout_runs(self):
        expected = "dsec_sandbox:DsecSandboxEnvironment"
        self.assertEqual(
            resolve_backend_plan({"HARBOR_ENVIRONMENT_TYPE": "dsec"}).environment_spec,
            expected,
        )
        self.assertEqual(
            rollout_backend_plan(
                {"RL_ENVIRONMENT_TYPE": "docker"}, "dsec"
            ).environment_spec,
            expected,
        )

    def run_shell(self, script, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                ["bash", "-c", script, "bash", str(HARBOR_DIR)],
                env={
                    "PATH": f"{Path(sys.executable).parent}:{os.defpath}",
                    "HOME": tmp,
                    "AGENT_FLEET_CONFIG_LOADED_ROOT": str(HARBOR_DIR.parents[3]),
                    "AGENT_FLEET_PATHS_FILE": f"{tmp}/missing.env",
                    "AGENT_FLEET_RUNTIME_DIR": f"{tmp}/runtime",
                    "OPIK_URL": "",
                    **overrides,
                },
                capture_output=True,
                text=True,
                check=True,
                cwd=tmp,
            )

    def test_env_resourcing_and_rollout_subshell_share_plan(self):
        result = self.run_shell(
            """
set -euo pipefail
source "$1/env.sh"
initial_plan="$HARBOR_BACKEND_PLAN"
source "$1/env.sh"
[[ "$initial_plan" == "$HARBOR_BACKEND_PLAN" ]]
(
  harbor_resolve_backend_plan --rollout-environment-type docker
  source "$1/env.sh"
  printf '%s\\n' "$HARBOR_BACKEND_PLAN"
)
[[ "$initial_plan" == "$HARBOR_BACKEND_PLAN" ]]
printf '%s\\n' "$HARBOR_BACKEND_PLAN"
""",
            RL_ENVIRONMENT_TYPE="qz",
        )
        request, listener = map(BackendPlan.from_json, result.stdout.splitlines())
        self.assertEqual(request.environment_spec, "docker")
        self.assertEqual(
            listener.environment_spec, "qz_e2b_sandbox:QzSandboxEnvironment"
        )

    def test_cli_quotes_custom_specs_and_propagates_failure(self):
        spec = "custom:Env'$(touch unexpected-command)`echo injected`\nsecond-line"
        result = self.run_shell(
            """
set -euo pipefail
source "$1/env.sh"
printf '%s' "$HARBOR_BACKEND_PLAN"
[[ ! -e unexpected-command ]]
""",
            HARBOR_ENVIRONMENT_SPEC=spec,
        )
        self.assertEqual(BackendPlan.from_json(result.stdout).environment_spec, spec)
        result = subprocess.run(
            [sys.executable, "-S", str(HARBOR_DIR / "backend_plan.py")],
            env={PLAN_ENV: "fake-secret"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("fake-secret", result.stderr)
