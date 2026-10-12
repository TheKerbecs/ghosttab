"""ctypes bindings: a low-level keyboard hook that can swallow keys, Unicode
text injection, and process and window lookups."""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_QUIT = 0x12
# Tag on our own SendInput events so the hook can skip exactly those (other
# injected input, e.g. the on-screen keyboard or AutoHotkey, is still handled).
INJECT_MARK = 0x67547462

VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 8, 9, 13, 16, 17, 18
VK_ESCAPE, VK_PRIOR, VK_NEXT, VK_END, VK_HOME = 27, 33, 34, 35, 36
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_DELETE = 37, 38, 39, 40, 46
VK_LWIN, VK_RWIN = 0x5B, 0x5C
MODIFIER_VKS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x14}

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class KeyboardHook:
    """WH_KEYBOARD_LL hook on its own message-loop thread.

    `on_key(vk, shift, ctrl, alt, win) -> bool` runs inside the hook and must
    return quickly; returning True swallows the key (and its key-up).
    Events injected by ghosttab itself (tagged with INJECT_MARK) are ignored."""

    def __init__(self, on_key: Callable[[int, bool, bool, bool, bool], bool]):
        self.on_key = on_key
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = None
        self._proc = HOOKPROC(self._callback)  # keep a reference alive
        self._swallowed: set[int] = set()
        self._ready = threading.Event()

    def _callback(self, n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            if kb.dwExtraInfo != INJECT_MARK:
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    try:
                        consume = self.on_key(
                            vk, key_down(VK_SHIFT), key_down(VK_CONTROL),
                            key_down(VK_MENU), key_down(VK_LWIN) or key_down(VK_RWIN),
                        )
                    except Exception:  # never let an exception kill the hook
                        log.exception("key handler failed")
                        consume = False
                    if consume:
                        self._swallowed.add(vk)
                        return 1
                elif w_param in (WM_KEYUP, WM_SYSKEYUP) and vk in self._swallowed:
                    self._swallowed.discard(vk)
                    return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            log.error("SetWindowsHookExW failed: %s", ctypes.get_last_error())
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="kbhook", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)



INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x2
KEYEVENTF_UNICODE = 0x4


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; pad so sizeof(INPUT) matches Win32.
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def send_text(text: str) -> bool:
    """Type `text` into whatever has keyboard focus (KEYEVENTF_UNICODE)."""
    units = text.encode("utf-16-le")
    codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
    events = []
    for code in codes:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            ev = INPUT(type=INPUT_KEYBOARD)
            ev.u.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=INJECT_MARK)
            events.append(ev)
    return _send(events)


def send_key(vk: int) -> bool:
    """Press and release a virtual key (modifiers currently held still apply)."""
    events = []
    for flags in (0, KEYEVENTF_KEYUP):
        ev = INPUT(type=INPUT_KEYBOARD)
        ev.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=INJECT_MARK)
        events.append(ev)
    return _send(events)


def _send(events: list[INPUT]) -> bool:
    arr = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    return sent == len(events)



PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_exe_cache: dict[int, str] = {}


def process_exe(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(h)
    if len(_exe_cache) > 256:
        _exe_cache.clear()
    _exe_cache[pid] = name
    return name


def foreground_window() -> tuple[int, str, int]:
    """(hwnd, title, thread_id) of the foreground window."""
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    return hwnd or 0, buf.value, tid


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]


def win32_caret_rect(thread_id: int) -> tuple[int, int, int, int] | None:
    """Classic system caret (Win32 edit controls). (x, y, w, h) in screen px."""
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) or not info.hwndCaret:
        return None
    rc = info.rcCaret
    pt = wintypes.POINT(rc.left, rc.top)
    user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
    h = rc.bottom - rc.top
    if h <= 0:
        return None
    return pt.x, pt.y, max(1, rc.right - rc.left), h


def enable_dpi_awareness():
    """Per-monitor DPI awareness, so UIA rects and Tk geometry share physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        user32.SetProcessDPIAware()
