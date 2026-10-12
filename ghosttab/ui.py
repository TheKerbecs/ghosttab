"""Shared Tk helpers: windows that float above other apps without taking focus,
Windows 11 rounded corners, tooltips, and the look shared by every window."""

from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import font as tkfont

user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x20, 0x80, 0x80000
WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x8, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOMOVE = 0x10, 0x1, 0x2
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND, DWMWCP_ROUNDSMALL = 2, 3

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

# Windows 11 dark flyout palette.
BG = "#2c2c2c"
SURFACE = "#383838"
SURFACE_HOVER = "#454545"
BORDER = "#454545"
FG = "#f3f3f3"
FG_MUTED = "#a8a8a8"
FG_FAINT = "#7a7a7a"
ACCENT = "#4cc2ff"
ACCENT_FG = "#1a1a1a"
ACCENT_HOVER = "#62cbff"
RECORDING = "#ff6b6b"

_fonts: dict[str, tuple] = {}


def font(size: int = 10, weight: str = "normal") -> tuple:
    """Segoe UI Variable on Windows 11, Segoe UI elsewhere."""
    if "family" not in _fonts:
        families = set(tkfont.families())
        _fonts["family"] = ("Segoe UI Variable Text",) if "Segoe UI Variable Text" in families else ("Segoe UI",)
        _fonts["icons"] = ("Segoe Fluent Icons",) if "Segoe Fluent Icons" in families else ("Segoe MDL2 Assets",)
    return (_fonts["family"][0], size, weight)


def icon_font(size: int = 10) -> tuple:
    font()
    return (_fonts["icons"][0], size)


ICON_MIC = "\ue720"


def frame_hwnd(win: tk.Misc) -> int:
    win.update_idletasks()
    return int(win.wm_frame(), 16)


def round_corners(hwnd: int, small: bool = False):
    """Native Windows 11 rounded corners and shadow; ignored on Windows 10."""
    value = ctypes.c_int(DWMWCP_ROUNDSMALL if small else DWMWCP_ROUND)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                                   ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        pass


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


def place_near(win: tk.Misc, anchor_x: int, anchor_top: int, anchor_bottom: int, w: int, h: int,
               gap: int = 6) -> tuple[int, int]:
    """Below the anchor if it fits on screen, otherwise above it."""
    left, top, right, bottom = screen_bounds(win, anchor_x, anchor_bottom)
    y = anchor_bottom + gap
    if y + h > bottom - 8 and anchor_top - gap - h >= top + 8:
        y = anchor_top - gap - h
    return clamp_to_screen(win, anchor_x, y, w, h)


class Tooltip:
    """A small hint shown after hovering a widget."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450):
        self.widget, self.text, self.delay_ms = widget, text, delay_ms
        self._after = None
        self.win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._cancel, add="+")
        widget.bind("<ButtonPress>", self._cancel, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _cancel(self, _event=None):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None
        if self.win is not None:
            self.win.destroy()
            self.win = None

    def _show(self):
        self.win = tk.Toplevel(self.widget)
        self.win.configure(bg=BORDER)
        tk.Label(self.win, text=self.text, bg=BG, fg=FG, font=font(9), padx=8, pady=4).pack(padx=1, pady=1)
        hwnd = make_floating(self.win, click_through=True)
        round_corners(hwnd, small=True)
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        show_floating(self.win, hwnd, x, y)


class Chip(tk.Label):
    """A flat button: a label with hover feedback."""

    def __init__(self, parent, text: str, command, accent: bool = False, **kw):
        self.accent = accent
        self.normal = ACCENT if accent else SURFACE
        self.hover = ACCENT_HOVER if accent else SURFACE_HOVER
        super().__init__(parent, text=text, bg=self.normal, fg=ACCENT_FG if accent else FG,
                         font=font(9, "bold" if accent else "normal"), padx=10, pady=3, cursor="hand2", **kw)
        self.command = command
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self.configure(bg=self.hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.normal))
