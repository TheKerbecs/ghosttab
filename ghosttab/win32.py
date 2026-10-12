"""ctypes bindings: low-level keyboard and mouse hooks, Unicode text
injection, the clipboard, and process and window lookups."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
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


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    ev = INPUT(type=INPUT_KEYBOARD)
    ev.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=INJECT_MARK)
    return ev


def send_text(text: str) -> bool:
    """Type `text` into whatever has keyboard focus (KEYEVENTF_UNICODE).

    Line breaks are sent as Shift+Enter, which starts a new line in editors
    and in chat inputs alike, where a plain Enter would send the message."""
    events = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            events += [_key_event(VK_SHIFT), _key_event(VK_RETURN),
                       _key_event(VK_RETURN, flags=KEYEVENTF_KEYUP), _key_event(VK_SHIFT, flags=KEYEVENTF_KEYUP)]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            events += [_key_event(scan=code, flags=KEYEVENTF_UNICODE),
                       _key_event(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return _send(events) if events else True


def send_key(vk: int) -> bool:
    """Press and release a virtual key (modifiers currently held still apply)."""
    return _send([_key_event(vk), _key_event(vk, flags=KEYEVENTF_KEYUP)])


def send_combo(*vks: int) -> bool:
    """Press keys in order and release them in reverse, e.g. send_combo(VK_CONTROL, ord("C"))."""
    return _send([_key_event(vk) for vk in vks] + [_key_event(vk, flags=KEYEVENTF_KEYUP) for vk in reversed(vks)])


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


user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND


def window_from_point(x: int, y: int) -> int:
    return user32.WindowFromPoint(wintypes.POINT(x, y)) or 0


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]


def force_foreground(hwnd: int) -> bool:
    """Bring `hwnd` to the foreground even when another process owns it."""
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_tid) and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_tid, False)
    return user32.GetForegroundWindow() == hwnd


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL


def _open_clipboard() -> bool:
    for _ in range(10):  # another process may hold it briefly
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, size)
    kernel32.GlobalUnlock(handle)
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def clipboard_sequence() -> int:
    return user32.GetClipboardSequenceNumber()


WH_MOUSE_LL = 14
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x201, 0x202, 0x204, 0x207


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MouseHook:
    """WH_MOUSE_LL hook reporting button events as on_event(message, x, y).
    It never swallows input, and the callback must return quickly."""

    def __init__(self, on_event: Callable[[int, int, int], None]):
        self.on_event = on_event
        self._thread_id = 0
        self._hook = None
        self._proc = HOOKPROC(self._callback)
        self._ready = threading.Event()

    def _callback(self, n_code, w_param, l_param):
        if n_code == 0 and w_param in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            try:
                self.on_event(w_param, info.pt.x, info.pt.y)
            except Exception:
                log.exception("mouse handler failed")
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            log.error("SetWindowsHookExW(WH_MOUSE_LL) failed: %s", ctypes.get_last_error())
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)

    def start(self):
        threading.Thread(target=self._run, name="mousehook", daemon=True).start()
        self._ready.wait(5)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
ERROR_ALREADY_EXISTS = 183
_instance_mutex = None


def single_instance(name: str) -> bool:
    """Hold a named mutex for the life of the process; False if another holds it."""
    global _instance_mutex
    _instance_mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
