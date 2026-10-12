"""Live end-to-end driver: types into a real window like a person and checks
what ghosttab does.

    python tests/live_drive.py --log ghosttab.log --target TARGET [--shots DIR]

TARGET: richedit, chromium, chat, german, assistant, dictation (the last one
needs ghosttab started with --config tests/live_dictation.json).

ghosttab must already be running with --debug and its output in --log.
Every keystroke is preceded by a check that the target window is still in
the foreground; if anything else has focus the run aborts, so keys never land
in an unrelated window."""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as uia

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghosttab import win32  # noqa: E402

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.FindWindowW.restype = wintypes.HWND
VK_SHIFT, VK_TAB, VK_ESCAPE, VK_MENU = 0x10, 0x09, 0x1B, 0x12
TITLE = "ghosttab live test"


class Abort(Exception):
    pass


def _send(events):
    arr = (win32.INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(win32.INPUT))


def _key(vk=0, scan=0, flags=0):
    ev = win32.INPUT(type=win32.INPUT_KEYBOARD)
    ev.u.ki = win32.KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return ev


class Driver:
    def __init__(self, hwnd: int, log_path: Path, shots: Path):
        self.hwnd = hwnd
        self.log_path = log_path
        self.shots = shots
        self.log_pos = log_path.stat().st_size
        self.results: list[tuple[str, bool, str]] = []
        self.log_buffer = ""

    def ensure_foreground(self):
        if user32.GetForegroundWindow() != self.hwnd:
            raise Abort("target window lost the foreground; stopping before sending keys")

    def type(self, text: str, delay=0.045):
        for ch in text:
            self.ensure_foreground()
            vk = 0x0D if ch == "\n" else 0
            if vk:
                _send([_key(vk), _key(vk, flags=win32.KEYEVENTF_KEYUP)])
            else:
                code = ord(ch)
                _send([_key(0, code, win32.KEYEVENTF_UNICODE),
                       _key(0, code, win32.KEYEVENTF_UNICODE | win32.KEYEVENTF_KEYUP)])
            time.sleep(delay)

    def press(self, vk, shift=False):
        self.ensure_foreground()
        evs = []
        if shift:
            evs.append(_key(VK_SHIFT))
        evs += [_key(vk), _key(vk, flags=win32.KEYEVENTF_KEYUP)]
        if shift:
            evs.append(_key(VK_SHIFT, flags=win32.KEYEVENTF_KEYUP))
        _send(evs)

    def combo(self, *vks, window: int | None = None):
        """Press a key combination; `window` must be in the foreground."""
        if user32.GetForegroundWindow() != (window or self.hwnd):
            raise Abort("expected window lost the foreground; stopping before sending keys")
        _send([_key(vk) for vk in vks] + [_key(vk, flags=win32.KEYEVENTF_KEYUP) for vk in reversed(vks)])

    def click(self, x: int, y: int, double: bool = False):
        self.ensure_foreground()
        user32.SetCursorPos(x, y)
        time.sleep(0.05)
        for _ in range(2 if double else 1):
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            time.sleep(0.06)

    def new_log(self) -> str:
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.log_pos)
            data = f.read()
            self.log_pos = f.tell()
        return data

    def wait_log(self, needle: str, timeout=15.0) -> str | None:
        buf = ""
        end = time.time() + timeout
        try:
            while time.time() < end:
                buf += self.new_log()
                for line in buf.splitlines():
                    if needle in line:
                        return line
                time.sleep(0.05)
            return None
        finally:
            self.log_buffer = buf

    def field_text(self) -> str:
        ctrl = uia.GetFocusedControl()
        tp = ctrl.GetPattern(uia.PatternId.TextPattern)
        if tp is not None:
            return tp.DocumentRange.GetText(-1).replace("\r\n", "\n").replace("\r", "\n")
        return ctrl.GetValuePattern().Value

    def shot(self, name: str):
        from PIL import ImageGrab
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
        path = self.shots / f"{name}.png"
        img.save(path)
        return path

    def check(self, name: str, ok: bool, detail: str):
        self.results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def suggestion_of(line: str | None) -> str | None:
    # log format: "... suggest [app.exe] ' text'"
    if not line:
        return None
    import ast
    return ast.literal_eval(line.split("] ", 1)[1])


def seconds_since_input() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000


def force_foreground(hwnd: int):
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(me, fg_tid, True)
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(me, fg_tid, False)
    time.sleep(0.3)
    if user32.GetForegroundWindow() != hwnd:
        # Fallback: a synthetic Alt tap grants foreground rights.
        _send([_key(VK_MENU), _key(VK_MENU, flags=win32.KEYEVENTF_KEYUP)])
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def open_richedit():
    code = (
        "import ctypes,sys,time\nfrom ctypes import wintypes\n"
        "u=ctypes.windll.user32; ctypes.windll.shcore.SetProcessDpiAwareness(2); ctypes.WinDLL('msftedit.dll')\n"
        "u.CreateWindowExW.restype=wintypes.HWND\n"
        "u.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,wintypes.LPVOID]\n"
        "WP=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)\n"
        "u.DefWindowProcW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]; u.DefWindowProcW.restype=ctypes.c_ssize_t\n"
        "wp=WP(lambda h,m,w,l: u.DefWindowProcW(h,m,w,l))\n"
        "class WC(ctypes.Structure): _fields_=[('style',wintypes.UINT),('lpfnWndProc',WP),('a',ctypes.c_int),('b',ctypes.c_int),('hInstance',wintypes.HINSTANCE),('hIcon',wintypes.HICON),('hCursor',wintypes.HANDLE),('hbr',wintypes.HBRUSH),('menu',wintypes.LPCWSTR),('cls',wintypes.LPCWSTR)]\n"
        "u.RegisterClassW(ctypes.byref(WC(lpfnWndProc=wp,cls='GTLive')))\n"
        f"p=u.CreateWindowExW(0,'GTLive','{TITLE}',0x00CF0000|0x10000000,300,250,900,320,None,None,None,None)\n"
        "e=u.CreateWindowExW(0,'RICHEDIT50W','',0x40000000|0x10000000|0x0004,12,12,860,250,p,None,None,None)\n"
        "f=ctypes.windll.gdi32.CreateFontW(-26,0,0,0,400,0,0,0,1,0,0,5,0,'Segoe UI'); u.SendMessageW(e,0x30,f,1)\n"
        "u.SetFocus(e)\n"
        "m=wintypes.MSG(); end=time.time()+float(sys.argv[1]); u.SetTimer(None,0,250,None)\n"
        "while u.GetMessageW(ctypes.byref(m),None,0,0)>0 and time.time()<end:\n"
        "    u.TranslateMessage(ctypes.byref(m)); u.DispatchMessageW(ctypes.byref(m))\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, "240"])
    for _ in range(50):
        hwnd = user32.FindWindowW("GTLive", TITLE)
        if hwnd:
            return proc, hwnd
        time.sleep(0.1)
    raise RuntimeError("test window did not appear")


TEXTAREA_PAGE = (f"<title>{TITLE}</title><body style='margin:0'>"
                 "<textarea autofocus style='width:100%;height:100vh;font:22px Segoe UI;border:0;padding:16px'>"
                 "</textarea>")

# A Discord-like layout: channel list on the left, history above the input.
CHAT_PAGE = f"""<title>{TITLE}</title>
<body style="margin:0;display:flex;height:100vh;font:16px Segoe UI;background:#313338;color:#dbdee1">
<nav style="width:170px;background:#2b2d31;color:#949ba4;padding:12px;line-height:1.9">
# general<br># climbing-crew<br># random<br># off-topic</nav>
<main style="flex:1;display:flex;flex-direction:column">
<div style="flex:1;padding:12px 16px;overflow:auto">
<p><b>Alex</b> <small style="color:#949ba4">Today at 10:42 AM</small><br>
anyone up for bouldering at the Kletterhalle on Thursday evening?</p>
<p><b>Sam</b> <small style="color:#949ba4">Today at 10:45 AM</small><br>
I'm in! 7pm works for me. Should we book shoe rental in advance?</p>
<p><b>Alex</b> <small style="color:#949ba4">Today at 10:47 AM</small><br>
Kim, are you coming too? We could grab dinner afterwards</p>
</div>
<textarea autofocus style="margin:0 16px 16px;height:56px;font:16px Segoe UI;background:#383a40;
color:#dbdee1;border:0;border-radius:8px;padding:12px"></textarea>
</main></body>"""


def open_chromium(html: str = TEXTAREA_PAGE, size: str = "900,360"):
    # Brave rather than Edge: Edge signs every new profile into the Windows
    # Microsoft account and starts syncing.
    from urllib.parse import quote
    exe = Path(os.environ["ProgramFiles"]) / "BraveSoftware/Brave-Browser/Application/brave.exe"
    profile = Path(tempfile.mkdtemp(prefix="ghosttab-chromium-"))
    proc = subprocess.Popen([str(exe), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--disable-sync", f"--window-size={size}", "--window-position=300,200",
                             f"--app=data:text/html;charset=utf-8,{quote(html)}"])
    for _ in range(100):
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            time.sleep(1.5)  # let the page load and focus the textarea
            return proc, hwnd, profile
        time.sleep(0.1)
    raise RuntimeError("browser window did not appear")


def run_chat(d: Driver, tag: str):
    """Screen context: the reply should be grounded in the visible chat."""
    d.type("yeah count me in! ")
    line = d.wait_log("suggest [")
    log = d.new_log()
    s = suggestion_of(line)
    d.check("suggestion appears", s is not None, repr(s))
    ctx_lines = [ln for ln in (d.log_buffer + log).splitlines() if "screen context" in ln]
    joined = "\n".join(ctx_lines)
    # The engine logs the last 160 chars of the context.
    d.check("context captured the conversation", "shoe rental" in joined and "dinner afterwards" in joined,
            ctx_lines[-1][-170:] if ctx_lines else "no screen context logged")
    d.check("context excluded the channel list", "climbing-crew" not in joined, "")
    time.sleep(0.2)
    print("   screenshot:", d.shot(f"{tag}-1-suggestion"))
    print(f"   (is the suggestion on topic? {s!r})")


ENGLISH = ("Hi Sarah, thanks for sending over the contract. I had a chance to review it and", " Could we")
GERMAN = ("Hallo J\u00fcrgen, danke f\u00fcr die schnelle R\u00fcckmeldung. Ich habe mir den Vertrag "
          "angesehen und", " K\u00f6nnen wir")


VK_CONTROL, VK_ALT, VK_RETURN, VK_SPACE = 0x11, 0x12, 0x0D, 0x20


def run_assistant(d: Driver, tag: str):
    """Selection assistant: hotkey, quick action, replace; then the selection dot."""
    text = "i has went to the store yesterday and buyed three apple"
    d.type(text, delay=0.02)
    time.sleep(0.3)
    d.press(VK_ESCAPE)
    d.combo(VK_CONTROL, ord("A"))
    time.sleep(0.2)
    d.new_log()
    d.combo(VK_CONTROL, VK_ALT, ord("A"))
    opened = d.wait_log("assistant: opened", timeout=5)
    d.check("hotkey opens the panel with the selection", bool(opened) and f"with {len(text)} chars" in opened,
            opened.split("assistant: ")[-1] if opened else "not opened")
    if not opened:
        return
    time.sleep(0.4)
    panel = user32.GetForegroundWindow()
    d.check("panel has keyboard focus", panel != d.hwnd, "")
    print("   screenshot:", d.shot(f"{tag}-1-panel"))
    d.combo(VK_CONTROL, ord("2"), window=panel)  # "Fix"
    answer = d.wait_log("assistant: answer", timeout=20)
    d.check("quick action streams an answer", bool(answer), answer.split("assistant: ")[-1] if answer else "")
    time.sleep(0.3)
    from PIL import ImageGrab
    r = wintypes.RECT()
    user32.GetWindowRect(panel, ctypes.byref(r))
    ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(d.shots / f"{tag}-2-answer.png")
    print("   screenshot:", d.shots / f"{tag}-2-answer.png")
    d.combo(VK_CONTROL, VK_RETURN, window=panel)
    inserted = d.wait_log("assistant: inserted", timeout=5)
    time.sleep(0.4)
    final = d.field_text()
    d.check("Ctrl+Enter replaces the selection", bool(inserted) and "buyed" not in final and "bought" in final,
            repr(final))

    # Mouse selection: double-click a word, click the dot, the panel opens.
    doc = uia.GetFocusedControl().GetPattern(uia.PatternId.TextPattern)
    rng = doc.DocumentRange.FindText("store", False, False) if doc else None
    rects = rng.GetBoundingRectangles() if rng else []
    if not rects:
        d.check("found a word to double-click", False, "")
        return
    rc = rects[0]
    x, y = rc.left + rc.width() // 2, rc.top + rc.height() // 2
    d.new_log()
    d.click(x, y, double=True)
    dot = d.wait_log("assistant: selection dot", timeout=3)
    d.check("double-click selection shows the dot", bool(dot), dot.split("assistant: ")[-1] if dot else "")
    if not dot:
        return
    time.sleep(0.2)
    print("   screenshot:", d.shot(f"{tag}-3-dot"))
    user32.SetCursorPos(x + 12 + 8, y + 10 + 8)
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    opened = d.wait_log("assistant: opened", timeout=5)
    d.check("clicking the dot opens the panel", bool(opened) and "with 5 chars" in opened,
            opened.split("assistant: ")[-1] if opened else "")
    time.sleep(0.4)
    d.combo(VK_ESCAPE, window=user32.GetForegroundWindow())
    time.sleep(0.4)
    d.check("Esc closes the panel and returns focus", user32.GetForegroundWindow() == d.hwnd, "")


def run_dictation(d: Driver, tag: str):
    """Needs ghosttab started with tests/live_dictation.json (audio from a file)."""
    d.type("Notiz:", delay=0.03)
    time.sleep(0.2)
    d.press(VK_ESCAPE)
    d.new_log()
    d.combo(VK_CONTROL, VK_ALT, VK_SPACE)
    started = d.wait_log("dictation: recording", timeout=5)
    d.check("hotkey starts dictation", bool(started), "")
    time.sleep(0.5)
    print("   screenshot:", d.shot(f"{tag}-1-listening"))
    cleaned = d.wait_log("dictation: cleaned", timeout=30)
    d.check("speech is transcribed and cleaned up", bool(cleaned), cleaned.split("dictation: ")[-1] if cleaned else "")
    time.sleep(0.6)
    final = d.field_text()
    d.check("text is typed at the cursor", final.startswith("Notiz: ") and "9 Uhr" in final and "Vertrag" in final,
            repr(final))


def run(d: Driver, tag: str, texts=ENGLISH):
    draft, follow_up = texts
    d.type(draft)
    line = d.wait_log("suggest [")
    s1 = suggestion_of(line)
    d.check("suggestion appears", s1 is not None, repr(s1))
    if s1 is None:
        return
    time.sleep(0.2)
    print("   screenshot:", d.shot(f"{tag}-1-suggestion"))
    t0 = time.time()
    d.press(VK_TAB)
    time.sleep(0.4)
    text = d.field_text()
    d.check("Tab inserts the whole suggestion", text == draft + s1, repr(text[-90:]))
    d.check("Tab was swallowed (no tab char)", "\t" not in text, f"{(time.time() - t0) * 1000:.0f} ms")

    # Shift+Tab: one word
    d.press(VK_ESCAPE)  # dismiss any chained suggestion
    time.sleep(0.2)
    d.type(follow_up)
    line = d.wait_log("suggest [")
    s2 = suggestion_of(line)
    d.check("second suggestion appears", s2 is not None, repr(s2))
    if s2:
        before = d.field_text()
        d.press(VK_TAB, shift=True)
        time.sleep(0.4)
        after = d.field_text()
        first_word = s2[: len(s2) - len(s2.lstrip())] + s2.lstrip().split(" ", 1)[0]
        d.check("Shift+Tab inserts one word", after == before + first_word, repr(after[len(before):]))
        print("   screenshot:", d.shot(f"{tag}-2-after-shift-tab"))

        # type-ahead: type the next two characters of what is still suggested
        remaining = s2[len(first_word):]
        ahead = remaining[:2]
        if ahead.strip():
            d.new_log()
            d.type(ahead, delay=0.08)
            time.sleep(0.6)
            log = d.new_log()
            regenerated = "suggest [" in log
            d.check("typing the suggestion keeps it (no new request)", "-> ready" in log and not regenerated,
                    f"typed {ahead!r}")
            print("   screenshot:", d.shot(f"{tag}-3-typeahead"))

        d.press(VK_ESCAPE)
        time.sleep(0.3)
        log = d.new_log()
        d.check("Esc dismisses", "cancel: dismissal" in log or "-> idle" in log, "")
        before = d.field_text()
        focus_before = tuple(uia.GetFocusedControl().GetRuntimeId())
        d.press(VK_TAB)
        time.sleep(0.3)
        # Reaching the app means a tab char (editors) or focus moving on (browsers).
        moved = tuple(uia.GetFocusedControl().GetRuntimeId()) != focus_before
        d.check("Tab without suggestion reaches the app", moved or d.field_text() != before,
                "focus moved" if moved else "tab inserted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--target", choices=["richedit", "chromium", "chat", "german", "assistant", "dictation"], default="richedit")
    ap.add_argument("--shots", type=Path, default=Path(tempfile.gettempdir()))
    args = ap.parse_args()
    win32.enable_dpi_awareness()
    args.shots.mkdir(parents=True, exist_ok=True)
    idle = seconds_since_input()
    if idle < 15:
        print(f"someone is using this PC (last input {idle:.1f}s ago); not taking focus. Rerun when idle.")
        return 4
    profile = None
    if args.target in ("richedit", "assistant", "dictation"):
        proc, hwnd = open_richedit()
    else:
        proc, hwnd, profile = open_chromium(CHAT_PAGE, "1100,560") if args.target == "chat" else open_chromium()
    try:
        if not force_foreground(hwnd):
            print("could not bring the test window to the foreground; nothing was typed")
            return 2
        with uia.UIAutomationInitializerInThread():
            d = Driver(hwnd, args.log, args.shots)
            try:
                if args.target == "chat":
                    run_chat(d, args.target)
                elif args.target == "german":
                    run(d, args.target, GERMAN)
                elif args.target == "assistant":
                    run_assistant(d, args.target)
                elif args.target == "dictation":
                    run_dictation(d, args.target)
                else:
                    run(d, args.target)
            except Abort as e:
                print("ABORTED:", e)
                return 3
        failed = [r for r in d.results if not r[1]]
        print(f"\n{len(d.results) - len(failed)}/{len(d.results)} checks passed")
        return 1 if failed else 0
    finally:
        proc.terminate()
        if profile is not None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            time.sleep(1)
            import shutil
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
