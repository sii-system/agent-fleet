"""Attach the native WAA desktop environment to owned guest endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import requests


class GuestHTTP:
    """Process-local URL mapping for native controllers and nested getters.

    Each benchmark worker is a separate process. Keeping the mapping here also
    covers upstream functions which construct controllers or URLs internally.
    """

    def __init__(self, host, endpoints, timeout=120):
        self.host = host
        self.endpoints = {int(port): url.rstrip("/") for port, url in endpoints.items()}
        self.timeout = timeout
        self.failures = []
        self.check_execution = True

    def rewrite(self, url):
        parsed = urlsplit(url)
        if parsed.hostname != self.host or parsed.port not in self.endpoints:
            return url
        target = urlsplit(self.endpoints[parsed.port])
        return urlunsplit((target.scheme, target.netloc, parsed.path, parsed.query, parsed.fragment))

    def check(self, *, allow_missing_file=False):
        failures, self.failures = self.failures, []
        if allow_missing_file:
            failures = [f for f in failures if not (f[0] == 404 and f[1] == "/file")]
        if failures:
            raise RuntimeError("WAA guest request failed; see task logs")

    @contextmanager
    def installed(self):
        original = requests.sessions.Session.request

        def request(session, method, url, **kwargs):
            rewritten = self.rewrite(url)
            parsed = urlsplit(rewritten)
            guest = any(parsed.netloc == urlsplit(endpoint).netloc for endpoint in self.endpoints.values())
            kwargs.setdefault("timeout", self.timeout)
            if guest:
                # Do not forward controller proxies or ambient netrc credentials.
                session.trust_env = False
                kwargs["proxies"] = {}
            try:
                response = original(session, method, rewritten, **kwargs)
            except requests.RequestException:
                if guest:
                    self.failures.append((0, parsed.path))
                raise
            if guest and response.status_code >= 400:
                self.failures.append((response.status_code, parsed.path))
            elif guest and parsed.path in ("/json/version", "/probe", "/screenshot", "/obs_winagent"):
                # Native startup probes can recover after a failed request.
                self.failures = [f for f in self.failures if f[1] != parsed.path]
            # Native controllers log and swallow some execution errors. Preserve
            # their return contracts, but fail the phase after it completes.
            if guest and parsed.path in ("/execute", "/setup/execute", "/execute_windows") and response.status_code == 200:
                try:
                    body = response.json()
                    if body.get("status") == "error" or (self.check_execution and body.get("returncode", 0) != 0):
                        self.failures.append((200, parsed.path))
                except (ValueError, AttributeError):
                    self.failures.append((200, parsed.path))
            return response

        requests.sessions.Session.request = request
        try:
            yield self
        finally:
            requests.sessions.Session.request = original

    @contextmanager
    def playwright(self):
        from playwright.sync_api import BrowserType, Error

        original = BrowserType.connect_over_cdp

        def connect(browser_type, endpoint_url, **kwargs):
            mapped = self.rewrite(endpoint_url)
            if mapped != endpoint_url:
                # Chrome can advertise a guest-local websocket URL. Resolve it
                # on the exposed HTTP endpoint and replace only its authority.
                response = requests.get(mapped.rstrip("/") + "/json/version", timeout=self.timeout)
                response.raise_for_status()
                websocket = urlsplit(response.json()["webSocketDebuggerUrl"])
                target = urlsplit(mapped)
                mapped = urlunsplit(("ws", target.netloc, websocket.path, websocket.query, ""))
            kwargs.setdefault("timeout", self.timeout * 1000)
            try:
                return original(browser_type, mapped, **kwargs)
            except Error:
                if mapped != endpoint_url:
                    self.failures.append((0, "/json/version"))
                raise

        BrowserType.connect_over_cdp = connect
        try:
            yield
        finally:
            BrowserType.connect_over_cdp = original


def create_desktop(host, cache_dir, *, screen_size, action_space, require_a11y_tree=False, benchmark="waa-v2"):
    from desktop_env.controllers.python import PythonController
    from desktop_env.controllers.setup import SetupController
    from desktop_env.envs.desktop_env import DesktopEnv

    class AttachedDesktop(DesktopEnv):
        def __init__(self):
            # Mirror the pinned native initialization without starting QEMU or
            # opening its QMP port. Native reset/step/evaluate remain unchanged.
            self.snapshot_name = "init_state"
            self.cache_dir_base = str(cache_dir)
            self.headless = True
            self.a11y_backend = "uia"
            self.require_a11y_tree = require_a11y_tree
            self.require_terminal = False
            self.not_navi = not require_a11y_tree
            self.is_azure = False
            self.screen_size = screen_size
            self.remote_vm = True
            self.vm_ip = host
            self.controller = PythonController(vm_ip=host)
            self.setup_controller = SetupController(vm_ip=host, cache_dir=str(cache_dir))
            self.instruction = None
            self.action_space = action_space
            self._traj_no = -1
            self._step_no = 0
            self.action_history = []

        def _get_screenshot(self):
            from io import BytesIO

            from PIL import Image

            raw = self.controller.get_screenshot()
            if raw is None:
                raise RuntimeError("WAA guest screenshot is unavailable")
            # Original WAA executes pixel coordinates directly. Preserve its
            # native screenshot size; only V2 resizes screenshots and actions.
            if benchmark == "waa":
                return raw
            with Image.open(BytesIO(raw)) as image:
                screenshot = BytesIO()
                image.resize(self.screen_size, Image.Resampling.LANCZOS).save(screenshot, format="PNG")
                return screenshot.getvalue()

    return AttachedDesktop()
