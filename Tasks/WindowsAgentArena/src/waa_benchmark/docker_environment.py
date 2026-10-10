"""Native WAA and WAA-V2 sessions on isolated Dockur Windows guests."""

from docker_windows.environment import DockerWindowsEnvironment

from .environment import WAASession


class WAADockerEnvironment(WAASession, DockerWindowsEnvironment):
    @staticmethod
    def type():
        return "waa-docker-windows"

    def _validate_native_ports(self):
        # DockerControl always publishes all three guest ports dynamically.
        pass
