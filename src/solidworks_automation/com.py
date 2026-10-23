"""Thin, version-independent COM access to SolidWorks (Windows only).

Every SolidWorks call goes through ``call``/``get``/``put``, which invoke the
member by name on the raw ``IDispatch``. This avoids pywin32's dynamic-dispatch
guessing (a no-argument method looked up as an attribute can be invoked as a
property) and needs no makepy/interop wrappers, so one build works with
SolidWorks 2016 and later.

COM objects are apartment-bound, so all SolidWorks work runs on a single
dedicated thread (``ComThread``).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import pythoncom
    import pywintypes
    import win32com.client

    _FLAGS_GET = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET


class SolidWorksError(Exception):
    """A SolidWorks call failed or returned a failure value."""


def require_windows() -> None:
    if not IS_WINDOWS:
        raise SolidWorksError(
            "SolidWorks only runs on Windows. On macOS, run SolidWorks in a Windows VM, start "
            "'solidworks-automation bridge' inside the VM and set SW_BRIDGE_URL / SW_BRIDGE_TOKEN "
            "for this server."
        )


def _unwrap(value: Any) -> Any:
    if isinstance(value, win32com.client.CDispatch):
        return value._oleobj_
    return value


def _dispid(obj: Any, name: str) -> int:
    try:
        return obj.GetIDsOfNames(name)
    except pywintypes.com_error as exc:
        raise SolidWorksError(f"SolidWorks object has no member '{name}'.") from exc


def call(obj: Any, name: str, *args: Any) -> Any:
    """Invoke method ``name`` (or read a parameterless property) and return the result."""
    if obj is None:
        raise SolidWorksError(f"Cannot call '{name}' on an empty SolidWorks object.")
    try:
        return obj.Invoke(_dispid(obj, name), 0, _FLAGS_GET, True, *args)
    except pywintypes.com_error as exc:
        raise SolidWorksError(f"SolidWorks call '{name}' failed: {_describe(exc)}") from exc


def get(obj: Any, name: str) -> Any:
    return call(obj, name)


def put(obj: Any, name: str, value: Any) -> None:
    try:
        obj.Invoke(_dispid(obj, name), 0, pythoncom.DISPATCH_PROPERTYPUT, False, _unwrap(value))
    except pywintypes.com_error as exc:
        raise SolidWorksError(f"Setting '{name}' failed: {_describe(exc)}") from exc


def nothing() -> Any:
    """A null object reference (VBA ``Nothing``) for optional object parameters."""
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def empty() -> Any:
    """An empty VARIANT for optional array parameters."""
    return win32com.client.VARIANT(pythoncom.VT_EMPTY, None)


def out_long() -> Any:
    """A by-reference ``long`` for SolidWorks error/warning out-parameters."""
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def out_bool() -> Any:
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)


def out_str() -> Any:
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")


def double_array(values: Any) -> Any:
    """A SAFEARRAY of doubles, as SolidWorks expects for point/vector data."""
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def as_list(value: Any) -> list[Any]:
    """SolidWorks returns arrays as tuples, or None when empty."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _describe(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 3 and args[2]:
        detail = args[2]
        if isinstance(detail, tuple) and len(detail) >= 3 and detail[2]:
            return str(detail[2])
    if len(args) >= 2:
        return str(args[1])
    return str(exc)


# --- application connection -------------------------------------------------

PROG_ID = "SldWorks.Application"


def find_running_app() -> Any | None:
    """Return the running SolidWorks instance's IDispatch, or None if not running."""
    require_windows()
    try:
        app = win32com.client.GetActiveObject(PROG_ID)
    except pywintypes.com_error:
        return None
    return _unwrap(app)


def is_installed() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        pywintypes.IID(PROG_ID)
    except pywintypes.com_error:
        return False
    return True


def launch_app(timeout_s: float = 300.0) -> Any:
    """Start SolidWorks, make it visible and wait until it accepts calls."""
    require_windows()
    if not is_installed():
        raise SolidWorksError("SolidWorks is not installed (COM class SldWorks.Application is not registered).")
    try:
        app = _unwrap(win32com.client.DispatchEx(PROG_ID))
    except pywintypes.com_error as exc:
        raise SolidWorksError(f"Could not start SolidWorks: {_describe(exc)}") from exc

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            put(app, "Visible", True)
            if _startup_complete(app):
                return app
        except SolidWorksError:
            pass  # still loading; the server rejects calls until startup finishes
        if time.monotonic() > deadline:
            raise SolidWorksError("SolidWorks did not finish starting in time.")
        time.sleep(2)


def _startup_complete(app: Any) -> bool:
    try:
        return bool(get(app, "StartupProcessCompleted"))
    except SolidWorksError:
        return True  # member missing on old releases; a successful Visible put is good enough


# --- threading ---------------------------------------------------------------


def _init_com_thread() -> None:
    if IS_WINDOWS:
        pythoncom.CoInitialize()


class ComThread:
    """Runs callables on one COM-initialised thread, in submission order."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="solidworks-com", initializer=_init_com_thread
        )

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
