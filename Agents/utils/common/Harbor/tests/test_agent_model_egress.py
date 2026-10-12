"""Check the launcher exception against pinned Harbor phase-policy resolution."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest

# Other backend tests replace Harbor modules with stubs. A fresh interpreter
# exercises the installed Harbor policy resolver independently of test order.
POLICY_PROBE = textwrap.dedent("""
    import json
    import sys
    import warnings
    from harbor.models.task.config import TaskConfig, VerifierEnvironmentMode
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig
    from harbor.trial.network_policy import resolve_trial_network_plan
    from Agents.utils.common.Harbor.harbor_shell_utils import agent_model_host

    task = TaskConfig.model_validate(json.loads(sys.argv[1]))
    original = task.model_dump()
    gateway = agent_model_host({
        "AGENT": "claude-code", "HARBOR_ANTHROPIC_BASE_URL": "https://gateway.example/v1",
    })
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = resolve_trial_network_plan(
            task, AgentConfig(extra_allowed_hosts=[gateway]), EnvironmentConfig(), None,
            verifier_mode=VerifierEnvironmentMode.SHARED,
        )
    print(json.dumps({
        "baseline": plan.agent_env_baseline.model_dump(mode="json"),
        "agent": plan.agent_phase.model_dump(mode="json"),
        "verifier": plan.verifier_phase.model_dump(mode="json"),
        "task_unchanged": task.model_dump() == original,
        "ignored": any("ignored" in str(item.message) for item in caught),
    }))
""")


class AgentModelEgressTest(unittest.TestCase):
    def plan(self, task_config):
        result = subprocess.run(
            [sys.executable, "-c", POLICY_PROBE, json.dumps(task_config)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_restricted_tasks_allow_gateway_only_during_agent_execution(self):
        for mode, hosts in (("no-network", []), ("allowlist", ["api.portkey.ai"])):
            with self.subTest(mode=mode):
                plan = self.plan({
                    "environment": {"network_mode": "no-network"},
                    "agent": {"network_mode": mode, "allowed_hosts": hosts or None},
                    "verifier": {"network_mode": "allowlist", "allowed_hosts": ["verify.example"]},
                })
                self.assertEqual(plan["baseline"], {"network_mode": "no-network", "allowed_hosts": []})
                self.assertEqual(plan["agent"], {
                    "network_mode": "allowlist", "allowed_hosts": [*hosts, "gateway.example"],
                })
                self.assertEqual(plan["verifier"]["allowed_hosts"], ["verify.example"])
                self.assertTrue(plan["task_unchanged"])

    def test_inherited_verifier_policy_does_not_receive_agent_exception(self):
        plan = self.plan({"environment": {"network_mode": "no-network"}})
        self.assertEqual(plan["agent"]["allowed_hosts"], ["gateway.example"])
        self.assertEqual(plan["verifier"], {"network_mode": "no-network", "allowed_hosts": []})

    def test_public_tasks_remain_public(self):
        plan = self.plan({})
        self.assertEqual(plan["agent"], {"network_mode": "public", "allowed_hosts": []})
        self.assertTrue(plan["ignored"])


if __name__ == "__main__":
    unittest.main()
