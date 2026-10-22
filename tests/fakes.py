"""A stand-in for SolidWorksOps so the server and bridge can be tested without SolidWorks."""

from solidworks_automation.com import SolidWorksError
from solidworks_automation.operations import OPERATIONS

PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


class FakeOps:
    def __init__(self, running: bool = False, installed: bool = True) -> None:
        self.running = running
        self.installed = installed
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, op):
        if op not in OPERATIONS:
            raise AttributeError(op)

        def handler(**kwargs):
            self.calls.append((op, kwargs))
            return {"op": op, **kwargs}

        return handler

    def status(self):
        self.calls.append(("status", {}))
        return {"installed": self.installed, "running": self.running, "documents": []}

    def connect(self, launch):
        self.calls.append(("connect", {"launch": launch}))
        if not self.running and launch:
            self.running = True
            return {"connected": True, "launched": True}
        if self.running:
            return {"connected": True, "launched": False}
        return {"connected": False, "launched": False, "installed": self.installed}

    def extrude(self, **kwargs):
        self.calls.append(("extrude", kwargs))
        if kwargs["sketch"] == "Missing":
            raise SolidWorksError("No sketch named 'Missing'.")
        return {"feature": "Boss-Extrude1"}

    def screenshot(self, doc, view):
        return {"format": "png", "data_base64": PNG_1PX}
