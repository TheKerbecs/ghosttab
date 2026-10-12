"""Ghost-text overlay: an always-on-top, click-through window that never takes
focus, drawn at the caret.

Transparency is a chroma key. The key colour is the field's real background,
sampled next to the caret, so anti-aliased glyph edges blend into the right
colour instead of leaving a dark halo. The first line starts at the caret and
later lines at the field's text margin, like a continuation of the field."""

from __future__ import annotations

import ctypes
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

from .focus import Rect

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x20, 0x80, 0x80000
WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x8, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOMOVE = 0x10, 0x1, 0x2
FALLBACK_KEY = "#ffffff"
KEYCAP_PAD = 5

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD


def sample_screen_color(x: int, y: int) -> str | None:
    dc = user32.GetDC(None)
    try:
        c = gdi32.GetPixel(dc, x, y)
    finally:
        user32.ReleaseDC(None, dc)
    if c == 0xFFFFFFFF:  # CLR_INVALID (off-screen)
        return None
    return f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def wrap_lines(text: str, font: tkfont.Font, first_width: int, width: int) -> list[str]:
    """Greedy word wrap; the first line has less room (it starts at the caret)."""
    lines: list[str] = []
    current = ""
    avail = first_width
    for token in _tokens(text):
        candidate = current + token
        if current and font.measure(candidate) > avail:
            lines.append(current)
            current = token.lstrip()
            avail = width
        elif not current and font.measure(candidate) > avail and lines == [] and avail < width:
            # Not even one word fits after the caret: start on the next line.
            lines.append("")
            current = token.lstrip()
            avail = width
        else:
            current = candidate
    lines.append(current)
    return lines


def _tokens(text: str) -> list[str]:
    out, word = [], ""
    for ch in text:
        if ch == " " and word.strip():
            out.append(word)
            word = ""
        word += ch
    if word:
        out.append(word)
    return out


class Overlay:
    def __init__(self, root: tk.Tk, text_color: str):
        self.root = root
        self.text_color = text_color
        self.key = FALLBACK_KEY
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._set_key(FALLBACK_KEY)
        self.font = tkfont.Font(root=root, family="Segoe UI", size=-14)
        self.key_font = tkfont.Font(root=root, family="Segoe UI", size=-10)
        root.update_idletasks()
        self.hwnd = int(root.wm_frame(), 16)
        style = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        style |= WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TOPMOST
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, style)
        self.visible = False
        self._geom: tuple[Rect, Rect, int] | None = None
        user32.ShowWindow(self.hwnd, SW_HIDE)

    def _set_key(self, color: str):
        self.key = color
        self.root.configure(bg=color)
        self.canvas.configure(bg=color)
        self.root.attributes("-transparentcolor", color)

    def show(self, text: str, caret: Rect, field: Rect, line_left: int):
        if not self.visible:
            # Sample just right of the caret while our window is hidden.
            bg = sample_screen_color(caret.x + 3, caret.y + caret.h // 2) or FALLBACK_KEY
            if _hex_to_rgb(bg) == _hex_to_rgb(self.text_color):
                bg = FALLBACK_KEY
            if bg != self.key:
                self._set_key(bg)
        self._geom = (caret, field, line_left)
        self._render(text)

    def update_text(self, text: str):
        if self.visible and self._geom is not None:
            self._render(text)

    def _render(self, text: str):
        caret, field, line_left = self._geom
        font_px = max(9, min(48, round(caret.h * 0.74)))
        self.font.configure(size=-font_px)
        self.key_font.configure(size=-max(9, round(font_px * 0.6)))
        line_h = max(caret.h, self.font.metrics("linespace"))

        if field.w > 80 and field.x <= caret.x <= field.right:
            left = min(line_left, caret.x)
            right = field.right - 6
        else:  # no usable field geometry: one 600 px column from the caret
            left, right = caret.x, caret.x + 600
        keycap_w = self.key_font.measure("Tab") + 2 * KEYCAP_PAD + 6
        lines = wrap_lines(text, self.font, right - caret.x - keycap_w, right - left - keycap_w)

        c = self.canvas
        c.delete("all")
        text_dy = (line_h - self.font.metrics("linespace")) // 2
        for i, line in enumerate(lines):
            x = (caret.x - left + 1) if i == 0 else 0
            c.create_text(x, i * line_h + text_dy, text=line, anchor="nw", font=self.font, fill=self.text_color)
        last_x = ((caret.x - left + 1) if len(lines) == 1 else 0) + self.font.measure(lines[-1]) + 6
        kh = self.key_font.metrics("linespace") + 2
        ky = (len(lines) - 1) * line_h + (line_h - kh) // 2
        kw = keycap_w - 6
        # Outline only, in the ghost-text colour, so it suits light and dark fields alike.
        c.create_rectangle(last_x, ky, last_x + kw, ky + kh, outline=self.text_color, width=1)
        c.create_text(last_x + kw // 2, ky + kh // 2, text="Tab", font=self.key_font, fill=self.text_color)

        w = max(right - left, last_x + kw + 2)
        h = len(lines) * line_h + 2
        # Tk owns the geometry (it would undo a raw SetWindowPos move); Win32
        # is used only for z-order and showing without activation.
        self.root.geometry(f"{w}x{h}+{left}+{caret.y}")
        self.root.update_idletasks()
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
        if not self.visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True

    def hide(self):
        if self.visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False
