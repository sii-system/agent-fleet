from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

HARBOROPIK = Path(__file__).resolve().parents[1] / "harboropik.sh"


def function_source(script: str, name: str) -> str:
    start_match = re.search(rf"^{name}\(\) \{{\n", script, re.MULTILINE)
    if start_match is None:
        raise AssertionError(f"missing shell function: {name}")
    next_match = re.search(
        r"^[a-zA-Z_][a-zA-Z0-9_]*\(\) \{\n",
        script[start_match.end() :],
        re.MULTILINE,
    )
    end = len(script) if next_match is None else start_match.end() + next_match.start()
    return script[start_match.start() : end]


class HarborPackageEnvironmentTest(unittest.TestCase):
    def test_package_environment_arguments_use_one_shared_helper(self) -> None:
        script = HARBOROPIK.read_text(encoding="utf-8")

        self.assertEqual(
            len(
                re.findall(
                    r"^append_package_environment_args\(\) \{$",
                    script,
                    re.MULTILINE,
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                re.findall(
                    r"^\s+append_package_environment_args$",
                    script,
                    re.MULTILINE,
                )
            ),
            2,
        )

        definitions = "\n".join(
            function_source(script, name)
            for name in (
                "cargo_registry_env_suffix",
                "append_rust_package_mirror_env",
                "append_package_environment_args",
            )
        )
        command = (
            f"{definitions}\n"
            "cmd=()\n"
            "append_package_environment_args\n"
            "printf '%s\\n' \"${cmd[@]}\"\n"
        )
        environment = {
            "PATH": "/usr/bin:/bin",
            "PIP_INDEX_URL": "https://pip.example/simple",
            "PIP_EXTRA_INDEX_URL": "https://extra.example/simple",
            "PIP_TRUSTED_HOST": "pip.example",
            "UV_INDEX_URL": "https://uv.example/simple",
            "UV_DEFAULT_INDEX": "https://uv-default.example/simple",
            "NPM_CONFIG_REGISTRY": "https://npm.example",
            "HARBOR_CC_NODE_DIST_URL": "https://node.example",
            "GO111MODULE": "on",
            "GOPROXY": "https://go.example",
            "GOSUMDB": "off",
        }
        result = subprocess.run(
            ["bash", "-c", command],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.splitlines()
        for name, value in environment.items():
            if name == "PATH":
                continue
            expected_name = "CC_NODE_DIST_URL" if name == "HARBOR_CC_NODE_DIST_URL" else name
            expected = f"{expected_name}={value}"
            self.assertIn(expected, arguments)
            self.assertEqual(arguments[arguments.index(expected) - 1], "--ae")
            if name != "HARBOR_CC_NODE_DIST_URL":
                self.assertIn(
                    ["--ve", expected],
                    [arguments[index : index + 2] for index in range(len(arguments) - 1)],
                )


if __name__ == "__main__":
    unittest.main()
