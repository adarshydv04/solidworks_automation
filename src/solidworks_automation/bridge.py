"""HTTP bridge that exposes SolidWorks operations to an MCP server on another machine.

Run this inside the Windows VM (or PC) where SolidWorks is installed::

    solidworks-automation bridge --host 0.0.0.0 --port 8765

Every request must carry ``Authorization: Bearer <token>``. Only the named
operations in ``operations.OPERATIONS`` are reachable; there is no way to run
arbitrary code or macros. Traffic is plain HTTP, so keep the bridge on a
host-only/local network or tunnel it over SSH/VPN.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
from collections.abc import Callable
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .com import ComThread, SolidWorksError
from .operations import OPERATIONS, SolidWorksOps
from .safety import PolicyError

MAX_BODY_BYTES = 1_000_000


def make_handler(token: str, submit: Callable[..., Future], ops: Any) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SolidWorksBridge/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"[bridge] {self.address_string()} {format % args}\n")

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"ok": False, "error": "Not found."})

        def do_POST(self) -> None:
            if self.path != "/call":
                self._send(404, {"ok": False, "error": "Not found."})
                return
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                self._send(401, {"ok": False, "error": "Invalid bridge token."})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(400, {"ok": False, "error": "Missing or oversized request body."})
                return
            try:
                request = json.loads(self.rfile.read(length))
                op, args = request["op"], request.get("args", {})
            except (ValueError, KeyError, TypeError):
                self._send(400, {"ok": False, "error": "Malformed request."})
                return
            if op not in OPERATIONS or not isinstance(args, dict):
                self._send(400, {"ok": False, "error": f"Unknown operation '{op}'."})
                return
            try:
                result = submit(getattr(ops, op), **args).result()
            except (SolidWorksError, PolicyError, TypeError) as exc:
                self._send(200, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - report, but keep the bridge alive
                self._send(500, {"ok": False, "error": f"Unexpected bridge error: {type(exc).__name__}: {exc}"})
                return
            self._send(200, {"ok": True, "result": result})

    return Handler


def run_bridge(host: str, port: int, token: str | None) -> None:
    token = token or os.environ.get("SW_BRIDGE_TOKEN") or secrets.token_urlsafe(32)
    thread = ComThread()
    ops = thread.submit(SolidWorksOps).result()
    server = HTTPServer((host, port), make_handler(token, thread.submit, ops))

    print(f"SolidWorks bridge listening on http://{host}:{port}", file=sys.stderr)
    print("Configure the MCP server with:", file=sys.stderr)
    print(f"  SW_BRIDGE_URL=http://<this-machine-ip>:{port}", file=sys.stderr)
    print(f"  SW_BRIDGE_TOKEN={token}", file=sys.stderr)
    if host not in ("127.0.0.1", "localhost"):
        print("Warning: traffic is unencrypted; use a host-only network or an SSH/VPN tunnel.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        thread.shutdown()
