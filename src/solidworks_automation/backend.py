"""Where SolidWorks operations actually run.

``LocalBackend`` drives SolidWorks on this Windows machine over COM.
``RemoteBackend`` forwards each operation to a bridge (``solidworks-automation
bridge``) running inside a Windows VM, which is how macOS users connect.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from .com import ComThread, SolidWorksError
from .operations import OPERATIONS, SolidWorksOps

REMOTE_TIMEOUT_S = 600  # first launch of SolidWorks can take minutes


class Backend(Protocol):
    description: str

    async def call(self, op: str, **kwargs: Any) -> dict: ...


class LocalBackend:
    """Runs operations on a dedicated COM thread in this process."""

    description = "local SolidWorks (COM)"

    def __init__(self, ops_factory: Callable[[], Any] = SolidWorksOps) -> None:
        self._thread = ComThread()
        self._ops = self._thread.submit(ops_factory).result()

    async def call(self, op: str, **kwargs: Any) -> dict:
        if op not in OPERATIONS:
            raise SolidWorksError(f"Unknown operation '{op}'.")
        future = self._thread.submit(getattr(self._ops, op), **kwargs)
        return await asyncio.wrap_future(future)


class RemoteBackend:
    """Sends operations to a bridge over HTTP, authenticated with a shared token."""

    def __init__(self, url: str, token: str) -> None:
        if not token:
            raise ValueError("SW_BRIDGE_TOKEN is required when SW_BRIDGE_URL is set.")
        self._url = url.rstrip("/") + "/call"
        self._token = token
        self.description = f"SolidWorks bridge at {url}"

    async def call(self, op: str, **kwargs: Any) -> dict:
        return await asyncio.to_thread(self._post, op, kwargs)

    def _post(self, op: str, args: dict) -> dict:
        body = json.dumps({"op": op, "args": args}).encode()
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REMOTE_TIMEOUT_S) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except ValueError:
                raise SolidWorksError(f"Bridge returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SolidWorksError(
                f"Cannot reach the SolidWorks bridge ({exc}). Is 'solidworks-automation bridge' running in the "
                "Windows VM, and is SW_BRIDGE_URL correct?"
            ) from exc
        if not payload.get("ok"):
            raise SolidWorksError(payload.get("error", "Bridge call failed."))
        return payload["result"]


def backend_from_env() -> Backend:
    url = os.environ.get("SW_BRIDGE_URL")
    if url:
        return RemoteBackend(url, os.environ.get("SW_BRIDGE_TOKEN", ""))
    return LocalBackend()
