"""Multi-monitor detection and window geometry management utilities.

Provides native Windows multi-monitor support (including negative coordinate spaces
for secondary monitors placed to the left or top of the primary monitor) and safe
fallback to the primary monitor when a previously used monitor is disconnected.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [
            ("x", wintypes.LONG),
            ("y", wintypes.LONG),
        ]

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.UINT),
            ("flags", wintypes.UINT),
            ("showCmd", wintypes.UINT),
            ("ptMinPosition", POINT),
            ("ptMaxPosition", POINT),
            ("rcNormalPosition", RECT),
        ]

    MONITOR_DEFAULTTONULL = 0
    MONITOR_DEFAULTTOPRIMARY = 1
    MONITOR_DEFAULTTONEAREST = 2
    SW_SHOWMAXIMIZED = 3
else:
    user32 = None  # type: ignore
    MONITOR_DEFAULTTONULL = 0
    MONITOR_DEFAULTTOPRIMARY = 1
    MONITOR_DEFAULTTONEAREST = 2
    SW_SHOWMAXIMIZED = 3


def get_monitors() -> list[dict[str, Any]]:
    """Enumerate all connected display monitors and return their rectangles.

    Returns:
        List of dicts: [
            {
                'handle': int,
                'is_primary': bool,
                'monitor_rect': (left, top, right, bottom),
                'work_rect': (left, top, right, bottom)
            },
            ...
        ]
    """
    if not IS_WINDOWS or user32 is None:
        return []

    monitors: list[dict[str, Any]] = []

    def _enum_proc(hMonitor, hdcMonitor, lprcMonitor, dwData):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
            m = info.rcMonitor
            w = info.rcWork
            monitors.append({
                "handle": int(hMonitor),
                "is_primary": bool(info.dwFlags & 1),
                "monitor_rect": (m.left, m.top, m.right, m.bottom),
                "work_rect": (w.left, w.top, w.right, w.bottom),
            })
        return True

    try:
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_enum_proc), 0)
    except Exception:
        pass

    return monitors


def get_primary_monitor_work_rect() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the primary monitor work area."""
    if IS_WINDOWS and user32 is not None:
        try:
            # MonitorFromPoint at (0, 0) gives the primary monitor
            pt = POINT(0, 0)
            h_mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTOPRIMARY)
            if h_mon:
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(h_mon, ctypes.byref(info)):
                    w = info.rcWork
                    return w.left, w.top, w.right - w.left, w.bottom - w.top
        except Exception:
            pass

        try:
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            return 0, 0, max(800, sw), max(600, sh)
        except Exception:
            pass

    return 0, 0, 1920, 1080


def is_rect_on_valid_monitor(
    x: int, y: int, w: int, h: int
) -> tuple[bool, Optional[tuple[int, int, int, int]]]:
    """Check if the given window rectangle falls within an actively connected monitor.

    Args:
        x: Window X coordinate (can be negative for left monitors).
        y: Window Y coordinate (can be negative for top monitors).
        w: Window width in pixels.
        h: Window height in pixels.

    Returns:
        (is_valid, work_area_rect):
            - is_valid: True if intersecting an active monitor, False if monitor no longer exists.
            - work_area_rect: (left, top, right, bottom) of the monitor's work area if valid.
    """
    if not IS_WINDOWS or user32 is None:
        # Fallback for non-windows: assume valid
        return True, (0, 0, 1920, 1080)

    try:
        rc = RECT(int(x), int(y), int(x + w), int(y + h))
        # MONITOR_DEFAULTTONULL returns 0 if no active monitor intersects
        h_mon = user32.MonitorFromRect(ctypes.byref(rc), MONITOR_DEFAULTTONULL)
        if not h_mon:
            return False, None

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(h_mon, ctypes.byref(info)):
            work = info.rcWork
            return True, (work.left, work.top, work.right, work.bottom)
    except Exception:
        pass

    return False, None


def clamp_to_monitor_work_area(
    x: int,
    y: int,
    w: int,
    h: int,
    work_rect: tuple[int, int, int, int],
    min_w: int = 980,
    min_h: int = 680,
) -> tuple[int, int, int, int]:
    """Clamp window coordinates and size so that the titlebar and body are accessible.

    Args:
        x, y, w, h: Candidate window geometry.
        work_rect: (left, top, right, bottom) of the target monitor's work area.
        min_w, min_h: Minimum allowed window dimensions.

    Returns:
        (clamped_x, clamped_y, clamped_w, clamped_h)
    """
    w_left, w_top, w_right, w_bottom = work_rect
    mon_w = max(min_w, w_right - w_left)
    mon_h = max(min_h, w_bottom - w_top)

    clamped_w = max(min_w, min(w, mon_w))
    clamped_h = max(min_h, min(h, mon_h))

    # Ensure title bar (top edge) is visible within work area
    clamped_x = max(w_left, min(x, w_right - 100))
    clamped_y = max(w_top, min(y, w_bottom - 100))

    return clamped_x, clamped_y, clamped_w, clamped_h


def get_window_placement_bounds(hwnd: int) -> Optional[dict[str, Any]]:
    """Retrieve normal (restored) window coordinates and maximized state via Win32 API.

    Args:
        hwnd: Native Windows HWND (root or parent window handle).

    Returns:
        Dict: {
            'x': int,
            'y': int,
            'width': int,
            'height': int,
            'is_maximized': bool
        } or None if failed.
    """
    if not IS_WINDOWS or user32 is None or not hwnd:
        return None

    try:
        # Check if hwnd needs parent handle (Tkinter toplevel container)
        parent = user32.GetParent(hwnd)
        target_hwnd = parent if parent else hwnd

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if user32.GetWindowPlacement(target_hwnd, ctypes.byref(wp)):
            rc = wp.rcNormalPosition
            return {
                "x": int(rc.left),
                "y": int(rc.top),
                "width": int(rc.right - rc.left),
                "height": int(rc.bottom - rc.top),
                "is_maximized": bool(wp.showCmd == SW_SHOWMAXIMIZED),
            }
    except Exception:
        pass

    return None

