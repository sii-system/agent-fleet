from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import tomllib
from swe_rebench_v2.adapter import SWERebenchV2Adapter
from swe_rebench_v2.environment import CLONE_REPOSITORY_OVERRIDES
from swe_rebench_v2.loader import LocalDatasetLoader


def _load_verifier_parser():
    tests_dir = Path(__file__).parents[1] / "src/swe_rebench_v2/task-template/tests"
    sys.path.insert(0, str(tests_dir))
    spec = importlib.util.spec_from_file_location(
        "generated_verifier_parser", tests_dir / "parser.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record() -> dict[str, Any]:
    return {
        "instance_id": "owner__project-1",
        "repo": "owner/project",
        "base_commit": "0123456789abcdef",
        "problem_statement": "Fix the bug without seeing the gold patch.",
        "patch": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
        "test_patch": "diff --git a/test_a.py b/test_a.py\n--- a/test_a.py\n+++ b/test_a.py\n@@ -1 +1 @@\n-old\n+new",
        "FAIL_TO_PASS": ["test_a.py::test_fix"],
        "PASS_TO_PASS": [],
        "language": "Python",
        "image_name": "docker.io/swerebenchv2/final-image:latest",
        "install_config": {
            "base_image_name": "python_3.11_base:latest",
            "install": ["echo install", "false"],
            "test_cmd": ["pytest"],
            "log_parser": "parse_log_pytest",
        },
        "meta": {"llm_metadata": {"difficulty": "hard"}},
    }


def test_json_loader_preserves_requested_order(tmp_path: Path) -> None:
    first = _record()
    second = {**_record(), "instance_id": "owner__project-2"}
    dataset = tmp_path / "sample.json"
    dataset.write_text(json.dumps([first, second]), encoding="utf-8")

    selected = list(
        LocalDatasetLoader(dataset).select(
            instance_ids=["owner__project-2", "owner__project-1"]
        )
    )

    assert [record["instance_id"] for record in selected] == [
        "owner__project-2",
        "owner__project-1",
    ]


def test_generated_task_uses_builder_base_image(tmp_path: Path) -> None:
    output = tmp_path / "tasks"
    adapter = SWERebenchV2Adapter(
        output,
        source="sample.json",
        base_image_registry="registry.example/base",
    )

    task_dir, base_image = adapter.generate_task(_record())

    assert base_image == "registry.example/base/python_3.11_base:latest"
    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert (
        "FROM --platform=linux/amd64 registry.example/base/python_3.11_base:latest"
        in dockerfile
    )
    assert dockerfile.count("FROM ") == 1
    assert " AS base" not in dockerfile
    assert "AS owner__project-1" not in dockerfile
    assert "docker.io/swerebenchv2/final-image" not in dockerfile
    assert (
        "git clone -o origin https://github.com/owner/project /project;" in dockerfile
    )
    assert (
        "git cat-file -e '0123456789abcdef^{commit}' || "
        "git fetch --no-tags origin '0123456789abcdef';" in dockerfile
    )
    assert dockerfile.index("git fetch --no-tags origin") < dockerfile.index(
        "git reset --hard"
    )
    assert "git reset --hard 0123456789abcdef;" in dockerfile
    assert "( echo install ) || true;" in dockerfile
    assert "( false ) || true;" in dockerfile
    assert dockerfile.endswith("WORKDIR /project\n")

    instruction = (task_dir / "instruction.md").read_text()
    assert instruction == "Fix the bug without seeing the gold patch.\n"
    assert "+new" not in instruction

    config = tomllib.loads((task_dir / "task.toml").read_text())
    assert config["task"]["name"] == "swe-rebench-v2/owner__project-1"
    assert config["verifier"]["timeout_sec"] == 3000

    test_script = (task_dir / "tests" / "test.sh").read_text()
    assert "echo 0 > /logs/verifier/reward.txt" not in test_script
    assert "echo 1 > /logs/verifier/reward.txt" not in test_script
    assert "rm -f /logs/verifier/reward.txt /logs/verifier/report.json" in test_script
    assert 'if [ "${exit_code}" -eq 0 ]' not in test_script
    official_apply = (
        "git apply -v --3way --recount --ignore-space-change --whitespace=nowarn"
    )
    assert official_apply in test_script
    assert "patch --fuzz" not in test_script
    assert "local repo_dir=/project" in test_script
    assert "find / -maxdepth" not in test_script
    assert 'if ! REPO_DIR="$(resolve_repo_dir)"; then' in test_script
    assert 'tee_pid="$!"' in test_script
    assert 'wait "$tee_pid"' in test_script
    assert test_script.index('exec > >(tee "$LOG_FILE") 2>&1') < test_script.index(
        'tee_pid="$!"'
    )
    assert test_script.index("exec 1>&3 2>&4") < test_script.index(
        'wait "$tee_pid"'
    )
    assert test_script.index('wait "$tee_pid"') < test_script.index(
        '"$PARSER_PYTHON" /tests/parser.py'
    )
    assert 'PARSER_PYTHON="${HARBOR_VERIFIER_PYTHON:-}"' in test_script
    assert 'HARBOR_VERIFIER_RUNTIME_BUNDLE_ARCHIVE' in test_script
    assert "no interpreter is available" in test_script
    assert (task_dir / "tests" / "parser.py").is_file()
    assert (task_dir / "tests" / "log_parsers.py").is_file()
    assert (task_dir / "tests" / "swe_constants.py").is_file()
    assert (task_dir / "solution" / "solve.sh").stat().st_mode & 0o111
    solution_script = (task_dir / "solution" / "solve.sh").read_text()
    assert official_apply in solution_script
    assert "patch --fuzz" not in solution_script
    assert "REPO_DIR=/project" in solution_script
    assert "find / -maxdepth" not in solution_script


def test_oracle_patch_preserves_trailing_blank_context_line(tmp_path: Path) -> None:
    record = _record()
    record["patch"] = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+new\n"
        " \n"
    )
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    solution_script = (task_dir / "solution" / "solve.sh").read_text()
    rendered_patch = solution_script.split(
        "cat > /tmp/solution_patch.diff << '__SOLUTION__'\n", 1
    )[1].split("\n__SOLUTION__\n", 1)[0]
    assert rendered_patch.endswith("+new\n \n")


def test_php_base_typo_uses_upstream_builder_artifact(tmp_path: Path) -> None:
    record = _record()
    record["install_config"]["base_image_name"] = "php:8.3.16"
    adapter = SWERebenchV2Adapter(
        tmp_path,
        source="sample.json",
        base_image_registry="registry.example/base",
    )

    task_dir, base_image = adapter.generate_task(record)

    assert base_image == "registry.example/base/php_8.3.16"
    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert "FROM --platform=linux/amd64 registry.example/base/php_8.3.16" in dockerfile
    metadata = json.loads((task_dir / "tests" / "config.json").read_text())
    assert metadata["install_config"]["base_image_name"] == "php:8.3.16"


def test_generated_task_does_not_use_instance_id_as_stage_name(
    tmp_path: Path,
) -> None:
    record = _record()
    record["instance_id"] = "11ty__eleventy-1876"
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert dockerfile.count("FROM ") == 1
    assert " AS base" not in dockerfile
    assert "AS 11ty__eleventy-1876" not in dockerfile


def test_archived_supermq_repository_clones_merged_magistrala_history(
    tmp_path: Path,
) -> None:
    record = _record()
    record.update(
        {
            "instance_id": "absmach__supermq-3004",
            "repo": "absmach/supermq",
            "base_commit": "1c0400d3a58409d4148d2f3cd7befd279dd97a42",
        }
    )
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert (
        "git clone -o origin https://github.com/absmach/magistrala /supermq;"
        in dockerfile
    )
    assert (
        "git fetch --no-tags origin "
        "'1c0400d3a58409d4148d2f3cd7befd279dd97a42';" in dockerfile
    )
    assert dockerfile.endswith("WORKDIR /supermq\n")

    metadata = json.loads((task_dir / "tests" / "config.json").read_text())
    assert metadata["repo"] == "absmach/supermq"
    assert metadata["base_commit"] == record["base_commit"]


@pytest.mark.parametrize(
    ("source_repo", "base_commit", "clone_repo"),
    [(*key, clone_repo) for key, clone_repo in CLONE_REPOSITORY_OVERRIDES.items()],
    ids=lambda value: value[:12] if len(value) == 40 else value,
)
def test_repository_overrides_preserve_task_identity_and_verified_commit(
    tmp_path: Path,
    source_repo: str,
    base_commit: str,
    clone_repo: str,
) -> None:
    project = source_repo.rsplit("/", 1)[-1]
    record = _record()
    record.update(
        {
            "instance_id": f"{source_repo.replace('/', '__')}-verified",
            "repo": source_repo,
            "base_commit": base_commit,
        }
    )
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    expected_clone = (
        f"git clone -o origin https://github.com/{clone_repo} /{project};"
    )
    assert expected_clone in dockerfile
    assert f"git cat-file -e '{base_commit}^{{commit}}'" in dockerfile
    assert f"git fetch --no-tags origin '{base_commit}';" in dockerfile
    assert f"git reset --hard {base_commit};" in dockerfile
    assert dockerfile.endswith(f"WORKDIR /{project}\n")

    metadata = json.loads((task_dir / "tests" / "config.json").read_text())
    assert metadata["repo"] == source_repo
    assert metadata["base_commit"] == base_commit


@pytest.mark.parametrize(
    ("source_repo", "clone_repo"),
    [
        ("absmach/supermq", "absmach/magistrala"),
        ("petermattis/pebble", "cockroachdb/pebble"),
        ("rickbergfalk/sqlpad", "sqlpad/sqlpad"),
    ],
)
def test_repository_override_is_limited_to_verified_commit(
    tmp_path: Path,
    source_repo: str,
    clone_repo: str,
) -> None:
    project = source_repo.rsplit("/", 1)[-1]
    record = _record()
    record.update(
        {
            "instance_id": f"{source_repo.replace('/', '__')}-other",
            "repo": source_repo,
        }
    )
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert f"https://github.com/{source_repo} /{project};" in dockerfile
    assert f"https://github.com/{clone_repo}" not in dockerfile


def test_task_config_matches_current_harbor_schema(tmp_path: Path) -> None:
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")
    task_dir, _ = adapter.generate_task(_record())

    from harbor.models.task.config import TaskConfig

    config = TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())

    assert config.schema_version == "1.3"
    assert config.task is not None
    assert config.task.name == "swe-rebench-v2/owner__project-1"
    assert config.environment.memory_mb == 8192
    assert config.environment.storage_mb == 16384


def test_missing_base_image_fails_without_partial_task(tmp_path: Path) -> None:
    record = _record()
    del record["install_config"]["base_image_name"]
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    with pytest.raises(ValueError, match="builder base image"):
        adapter.generate_task(record)

    assert not (tmp_path / record["instance_id"]).exists()


def test_empty_install_commands_follow_official_template_semantics(
    tmp_path: Path,
) -> None:
    record = _record()
    record["install_config"]["install"] = []
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert "git remote remove origin;" in dockerfile
    assert "echo install" not in dockerfile


def test_pr_description_is_safe_instruction_fallback(tmp_path: Path) -> None:
    record = _record()
    record["problem_statement"] = None
    record["pr_description"] = "Implement the requested OCaml behavior."
    adapter = SWERebenchV2Adapter(tmp_path, source="sample.json")

    task_dir, _ = adapter.generate_task(record)

    assert (task_dir / "instruction.md").read_text() == (
        "Implement the requested OCaml behavior.\n"
    )
    assert "+new" not in (task_dir / "instruction.md").read_text()


def test_verifier_uses_official_parser_and_exact_passed_set() -> None:
    parser = _load_verifier_parser()
    config = {
        "install_config": {"log_parser": "parse_log_pytest"},
        "FAIL_TO_PASS": ["test_a.py::test_fix"],
        "PASS_TO_PASS": [],
    }

    passing = parser.grade(config, "PASSED test_a.py::test_fix\n")
    extra_pass = parser.grade(
        config,
        "PASSED test_a.py::test_fix\nPASSED test_a.py::test_unexpected\n",
    )

    assert passing["resolved"] is True
    assert extra_pass["resolved"] is False
    assert extra_pass["unexpected_passed"] == ["test_a.py::test_unexpected"]


def test_verifier_writes_reward_only_after_completed_grading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _load_verifier_parser()
    config_path = tmp_path / "config.json"
    log_path = tmp_path / "test.log"
    report_path = tmp_path / "report.json"
    reward_path = tmp_path / "reward.txt"
    config_path.write_text(
        json.dumps(
            {
                "install_config": {"log_parser": "parse_log_pytest"},
                "FAIL_TO_PASS": ["test_a.py::test_fix"],
                "PASS_TO_PASS": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(parser, "CONFIG_PATH", config_path)
    monkeypatch.setattr(parser, "REPORT_PATH", report_path)
    monkeypatch.setattr(parser, "REWARD_PATH", reward_path)
    monkeypatch.setenv("LOG_FILE", str(log_path))

    log_path.write_text("PASSED test_a.py::test_fix\n", encoding="utf-8")
    assert parser.main() == 0
    assert reward_path.read_text(encoding="utf-8") == "1\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["resolved"] is True

    log_path.write_text("FAILED test_a.py::test_fix\n", encoding="utf-8")
    assert parser.main() == 0
    assert reward_path.read_text(encoding="utf-8") == "0\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["resolved"] is False


def test_missing_verifier_config_does_not_create_a_reward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _load_verifier_parser()
    config_path = tmp_path / "missing-config.json"
    log_path = tmp_path / "test.log"
    report_path = tmp_path / "report.json"
    reward_path = tmp_path / "reward.txt"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(parser, "CONFIG_PATH", config_path)
    monkeypatch.setattr(parser, "REPORT_PATH", report_path)
    monkeypatch.setattr(parser, "REWARD_PATH", reward_path)
    monkeypatch.setenv("LOG_FILE", str(log_path))

    with pytest.raises(FileNotFoundError):
        parser.main()

    assert not reward_path.exists()
    assert not report_path.exists()


def test_log_parser_exception_does_not_create_a_reward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = _load_verifier_parser()
    config_path = tmp_path / "config.json"
    log_path = tmp_path / "test.log"
    report_path = tmp_path / "report.json"
    reward_path = tmp_path / "reward.txt"
    config_path.write_text(
        json.dumps(
            {
                "install_config": {"log_parser": "broken_parser"},
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
            }
        ),
        encoding="utf-8",
    )
    log_path.write_text("test output\n", encoding="utf-8")
    monkeypatch.setattr(parser, "CONFIG_PATH", config_path)
    monkeypatch.setattr(parser, "REPORT_PATH", report_path)
    monkeypatch.setattr(parser, "REWARD_PATH", reward_path)
    monkeypatch.setenv("LOG_FILE", str(log_path))

    def fail_parser(_: str) -> dict[str, str]:
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setitem(parser.NAME_TO_PARSER, "broken_parser", fail_parser)

    with pytest.raises(RuntimeError, match="synthetic parser failure"):
        parser.main()

    assert not reward_path.exists()
    assert not report_path.exists()


def test_official_parser_map_covers_all_v2_schema_values() -> None:
    parser = _load_verifier_parser()
    expected_parser_names = {
        "parse_java_mvn",
        "parse_java_mvn_v2",
        "parse_log_ant",
        "parse_log_cargo",
        "parse_log_cpp",
        "parse_log_cpp_v3",
        "parse_log_csharp",
        "parse_log_dart",
        "parse_log_dart_v3",
        "parse_log_elixir",
        "parse_log_gotest",
        "parse_log_gradle_custom",
        "parse_log_gradlew_v1",
        "parse_log_jq",
        "parse_log_js",
        "parse_log_js_2",
        "parse_log_js_3",
        "parse_log_js_4",
        "parse_log_julia",
        "parse_log_junit",
        "parse_log_lein",
        "parse_log_maven",
        "parse_log_ocaml",
        "parse_log_ocaml_v2",
        "parse_log_ocaml_v3",
        "parse_log_php_v1",
        "parse_log_phpunit",
        "parse_log_pytest",
        "parse_log_r",
        "parse_log_scala_v2",
        "parse_log_scala_v3",
        "parse_log_swift",
        "parse_logs_r_junit",
        "parse_lue_nvim",
    }

    assert expected_parser_names <= parser.NAME_TO_PARSER.keys()
