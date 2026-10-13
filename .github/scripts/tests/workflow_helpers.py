"""Read workflow steps without depending on YAML indentation or key order."""

import yaml


def workflow_steps(path):
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def workflow_step(path, name):
    return next(step for step in workflow_steps(path) if step.get("name") == name)


def step_script(path, name):
    return workflow_step(path, name)["run"]
