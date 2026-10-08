"""Process-isolated native desktop shared across Harbor's trial phases."""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

from .dataset import digest
from .desktop import GuestHTTP, create_desktop


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d@%H%M%S")


class NativeSession:
    def __init__(self, spec):
        self.spec = spec
        self.benchmark = spec.get("benchmark", "waa-v2")
        self.output = Path(spec["output"])
        self.output.mkdir(parents=True, exist_ok=True)
        task_path = Path(spec["task_path"])
        if digest(task_path) != spec["task_sha256"]:
            raise ValueError("Task changed after Harbor materialization")
        self.config = json.loads(task_path.read_text())
        self.http = GuestHTTP(spec["host"], spec["endpoints"])
        self.env = create_desktop(spec["host"], self.output / "cache", screen_size=tuple(spec["screen_size"]),
                                  action_space=spec["action_space"], require_a11y_tree=spec["a11y"], benchmark=self.benchmark)
        self.obs, self.steps, self.done, self.evaluated = None, 0, False, False
        self.started = datetime.datetime.now(datetime.timezone.utc)
        from trajectory_recorder import TrajectoryRecorder
        self.recorder = TrajectoryRecorder(str(self.output))

    def setup(self):
        if self.obs is not None:
            raise RuntimeError("Native task setup already ran")
        if self.benchmark == "waa":
            self.obs = self.env.reset(task_config=self.config)
        else:
            self.obs = self.env.reset(domain=self.spec["domain"], task_config=self.config)
        self.http.check()
        if self.obs is None:
            raise RuntimeError("WAA setup did not produce an observation")
        if self.benchmark == "waa":
            self.recorder.record_init(self.obs, self.config, timestamp())
        return {"benchmark": self.benchmark, "status": "ready"}

    def agent(self, *, factory, model, instruction, max_steps, pause, extra_env=None):
        from Agents.WindowsAgentArena.agent import load_agent
        if self.obs is None or self.evaluated:
            raise RuntimeError("Agent must run after setup and before verification")
        previous = {key: os.environ.get(key) for key in (extra_env or {})}
        os.environ.update(extra_env or {})
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(self.obs["screenshot"])) as screenshot:
                screen_size = screenshot.size
            agent = load_agent(factory, model=model, screenshot_size=screen_size,
                               pcagent_client=self.spec.get("pcagent_client"))
            agent.reset()
            self._predict(agent, instruction, max_steps=max_steps, pause=pause)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return {"steps": self.steps, "termination": "agent" if self.done else "max_steps"}

    def _predict(self, agent, instruction, *, max_steps, pause):
        while not self.done and self.steps < max_steps:
            prediction = agent.predict(instruction, self.obs)
            if not isinstance(prediction, (tuple, list)) or len(prediction) != 4:
                raise TypeError("predict() must return response, actions, logs, computer_update_args")
            _, actions, logs, update = prediction
            if not isinstance(actions, (tuple, list)) or not actions:
                raise TypeError("predict() must return a nonempty list of actions")
            if update:
                self.env.controller.update_computer(**update)
                self.http.check()
            for action in actions:
                if self.benchmark == "waa-v2":
                    self.recorder.record_step(self.obs, logs, self.steps, timestamp(), action)
                self.obs, reward, self.done, info = self.env.step(action, pause)
                self.http.check()
                if self.obs is None:
                    raise RuntimeError("WAA step did not produce an observation")
                if self.benchmark == "waa":
                    self.recorder.record_step(self.obs, logs, self.steps, timestamp(),
                                             str(datetime.datetime.now(datetime.timezone.utc) - self.started), action, reward, self.done, info)
                if self.done:
                    break
            self.steps += 1

    def evaluate(self):
        if self.obs is None or self.evaluated:
            raise RuntimeError("Verification requires setup and runs once per trial")
        self.http.check_execution = False
        score = float(self.env.evaluate())
        self.http.check(allow_missing_file=True)
        if not math.isfinite(score):
            raise ValueError("Native WAA evaluator returned an invalid score")
        self.evaluated = True
        if self.benchmark == "waa":
            self.recorder.record_end(score, self.started.astimezone().replace(tzinfo=None))
            (self.output / "result.txt").write_text(str(score) + "\n")
        else:
            self.recorder.record_end([(self.steps, score)], self.obs, self.steps, timestamp())
        return {"status": "completed", "score": score, "steps": self.steps,
                "termination": "agent" if self.done else "max_steps"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    sys.path.insert(0, str(Path(spec["runtime"]) / "client"))
    logging.basicConfig(level=logging.INFO)
    # Native code prints to stdout; reserve the original stream for the protocol.
    protocol = sys.stdout
    with redirect_stdout(sys.stderr):
        session = NativeSession(spec)
        with session.http.installed(), session.http.playwright():
            for line in sys.stdin:
                request = json.loads(line)
                try:
                    method = request.pop("method")
                    if method not in ("setup", "agent", "evaluate"):
                        raise ValueError("Unknown native phase")
                    response = {"ok": True, "result": getattr(session, method)(**request)}
                except Exception as exc:  # noqa: BLE001 -- report native failures to Harbor
                    traceback.print_exc()
                    response = {"ok": False, "error_type": type(exc).__name__}
                protocol.write(json.dumps(response) + "\n")
                protocol.flush()


if __name__ == "__main__":
    main()
