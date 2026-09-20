"""Run a minimal parser and standard-library check inside a verifier bundle."""

from __future__ import annotations

import json
import pathlib
import re
import runpy
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    """Validate the bundled interpreter and the task parser fixture.

    The parser path defaults to the task contract at /tests/parser.py.  A
    custom path is accepted for local bundle tests without changing task-image
    files.
    """
    assert json.loads('{"ok": true}')["ok"]
    assert re.fullmatch("ok", ET.fromstring("<r>ok</r>").text)
    parser_path = pathlib.Path(
        sys.argv[1] if len(sys.argv) > 1 else "/tests/parser.py"
    )
    sys.path.insert(0, str(parser_path.parent))
    try:
        grade = runpy.run_path(str(parser_path))["grade"]
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - task parser
        print(f"verifier runtime error: parser_import_failed: {exc}", file=sys.stderr)
        return 127

    config = {
        "install_config": {"log_parser": "parse_log_pytest"},
        "FAIL_TO_PASS": ["test_a.py::test_fix"],
        "PASS_TO_PASS": [],
    }
    try:
        assert grade(config, "PASSED test_a.py::test_fix\n")["resolved"] is True
        assert grade(config, "FAILED test_a.py::test_fix\n")["resolved"] is False
        assert (
            grade(
                config,
                "PASSED test_a.py::test_fix\nPASSED test_extra\n",
            )["resolved"]
            is False
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - task parser
        print(f"verifier runtime error: parser_fixture_failed: {exc}", file=sys.stderr)
        return 127
    print(
        "verifier runtime ready: "
        f"python={sys.version.split()[0]} target=x86_64-linux "
        "linkage=static-musl parser_fixture=passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
