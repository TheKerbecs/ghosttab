"""Manual probe for the UI Automation reader.

    python tests/uia_probe.py [--edit]

Spawns a child process with a RichEdit control (or a classic EDIT control with
--edit) containing known text, puts the caret at the end and reads it back
through ghosttab.focus.read_control, the code path read_focus uses for the
focused element. Only the probe window is read."""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

TEXT = "Hi team, thanks for the quick turnaround on the draft. I think we should"

if len(sys.argv) > 1 and sys.argv[1] == "--target":
    user32 = ctypes.windll.user32
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    WS_OVERLAPPEDWINDOW, WS_VISIBLE, ES_MULTILINE = 0x00CF0000, 0x10000000, 0x0004
    # RichEdit 4.1+ implements UIA TextPattern natively (caret geometry path);
    # pass --edit to test a classic EDIT control (ValuePattern fallback path).
    cls = "EDIT" if "--edit" in sys.argv else "RICHEDIT50W"
    if cls != "EDIT":
        ctypes.WinDLL("msftedit.dll")
    WS_CHILD = 0x40000000
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=wndproc, lpszClassName="GhosttabProbe")))
    parent = user32.CreateWindowExW(0, "GhosttabProbe", "ghosttab probe", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                    200, 200, 720, 240, None, None, None, None)
    hwnd = user32.CreateWindowExW(0, cls, "", WS_CHILD | WS_VISIBLE | ES_MULTILINE,
                                  10, 10, 680, 160, parent, None, None, None)
    user32.SetWindowTextW(hwnd, TEXT)
    print(hwnd, flush=True)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    user32.SendMessageW(hwnd, 0x00B1, len(TEXT), len(TEXT))  # EM_SETSEL: caret at end
    # Blocking message loop like a real app (a sleep-polling loop would add
    # its sleep to every cross-process UIA call); a timer ends it.
    deadline = time.time() + float(sys.argv[2])
    user32.SetTimer(None, 0, 250, None)  # wakes the loop to check the deadline
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if time.time() >= deadline:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    sys.exit(0)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import uiautomation as uia  # noqa: E402
from ghosttab import win32  # noqa: E402
from ghosttab.focus import read_control  # noqa: E402


win32.enable_dpi_awareness()
child = subprocess.Popen([sys.executable, __file__, "--target", "4", *sys.argv[1:]], stdout=subprocess.PIPE, text=True)
try:
    hwnd = int(child.stdout.readline())
    time.sleep(1.0)  # let the child finish creating and laying out its window
    with uia.UIAutomationInitializerInThread():
        ctrl = uia.ControlFromHandle(hwnd)
        t0 = time.perf_counter()
        ctx = read_control(ctrl, own_pid=-1)
        ms = (time.perf_counter() - t0) * 1000
    if ctx is None:  # (venv python.exe is a launcher, so child.pid is not the window owner)
        print("could not read the probe window")
        sys.exit(2)
    print(f"read_focus took {ms:.1f} ms")
    print(f"app={ctx.app} title={ctx.window_title!r} password={ctx.is_password}")
    print(f"preceding={ctx.preceding!r}")
    print(f"trailing={ctx.trailing!r}")
    print(f"caret={ctx.caret} exact={ctx.caret_exact} field={ctx.field}")
    ok = ctx.preceding == TEXT and ctx.trailing == "" and (
        "--edit" in sys.argv or (ctx.caret_exact and ctx.caret.x > ctx.field.x + 100))
    print("OK" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)
finally:
    child.wait()
