"""DSec Agent Toolkit startup preflight tests."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from harbor_environment_adapters.providers.dsec_controller.runtime_preflight import (
    validate_dsec_agent_runtime,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.payload


class _Opener:
    def __init__(self, payload):
        self.payload = payload

    def open(self, request, timeout):
        assert request.full_url == "http://controller/v1alpha1/status"
        assert timeout == 15
        return _Response(self.payload)


def _status(*runtimes):
    return {
        "api_version": "sandbox-lifecycle/v1alpha1",
        "status": "ok",
        "result": {
            "ready": True,
            "nodes": [
                {
                    "node_id": "node-a",
                    "reachable": True,
                    "status": {
                        "ready": True,
                        "capabilities": {"agent_runtimes": list(runtimes)},
                    },
                },
                {
                    "node_id": "node-offline",
                    "reachable": False,
                    "status": {"ready": False, "capabilities": {}},
                },
            ],
        },
    }


class DsecRuntimePreflightTests(unittest.TestCase):
    def test_requires_runtime_on_every_schedulable_node(self):
        config = {
            "AGENT": "opencode",
            "OPENCODE_VERSION": "1.19.5",
            "DSEC_SANDBOX_CONTROLLER_ENDPOINT": "http://controller",
        }
        message = validate_dsec_agent_runtime(
            config,
            opener=_Opener(_status({"agent": "opencode", "version": "1.19.5"})),
        )
        self.assertIn("opencode@1.19.5", message)

        with self.assertRaisesRegex(ValueError, "node-a"):
            validate_dsec_agent_runtime(config, opener=_Opener(_status()))

    def test_rejects_mutable_versions_before_network_access(self):
        config = {
            "AGENT": "opencode",
            "OPENCODE_VERSION": "latest",
            "DSEC_SANDBOX_CONTROLLER_ENDPOINT": "http://controller",
        }
        with self.assertRaisesRegex(ValueError, "exact OPENCODE_VERSION"):
            validate_dsec_agent_runtime(config, opener=object())


if __name__ == "__main__":
    unittest.main()
