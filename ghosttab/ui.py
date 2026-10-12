"""Shared Tk helpers: windows that float above other apps without taking focus,
and the colours used by every ghosttab window."""

from __future__ import annotations

import ctypes
import tkinter as tk

user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x20, 0x80, 0x80000
WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x8, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOMOVE = 0x10, 0x1, 0x2

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

BG = "#1e1f22"
BG_RAISED = "#2b2d31"
BORDER = "#3f4147"
FG = "#e3e5e8"
FG_MUTED = "#949ba4"
ACCENT = "#5b8def"
RECORDING = "#e5484d"
BUSY = "#f0a020"
FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)


def frame_hwnd(win: tk.Misc) -> int:
    win.update_idletasks()
    return int(win.wm_frame(), 16)


def make_floating(win: tk.Toplevel | tk.Tk, click_through: bool) -> int:
    """Borderless, topmost, never activated; returns the window handle."""
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    hwnd = frame_hwnd(win)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
    if click_through:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    user32.ShowWindow(hwnd, SW_HIDE)
    return hwnd


def show_floating(win: tk.Misc, hwnd: int, x: int, y: int, w: int | None = None, h: int | None = None):
    """Place and show a floating window without activating it."""
    if w is None or h is None:
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    # Tk owns the geometry (it would undo a raw SetWindowPos move); Win32 is
    # used only for z-order and showing without activation.
    win.geometry(f"{w}x{h}+{x}+{y}")
    win.update_idletasks()
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def hide(hwnd: int):
    user32.ShowWindow(hwnd, SW_HIDE)


def screen_bounds(win: tk.Misc, x: int, y: int) -> tuple[int, int, int, int]:
    """Work area (left, top, right, bottom) of the monitor containing (x, y)."""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_ulong]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    mon = user32.MonitorFromPoint(POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def clamp_to_screen(win: tk.Misc, x: int, y: int, w: int, h: int, margin: int = 8) -> tuple[int, int]:
    left, top, right, bottom = screen_bounds(win, x, y)
    x = max(left + margin, min(x, right - w - margin))
    y = max(top + margin, min(y, bottom - h - margin))
    return x, y
