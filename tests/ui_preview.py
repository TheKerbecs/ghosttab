"""Renders every ghosttab popup in its states and saves a contact sheet.

    python tests/ui_preview.py [out.png]

Windows are shown without activation, so the app you are using keeps focus;
they appear briefly near the top-left of the primary screen.
"""

import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageGrab  # noqa: E402

from ghosttab import ui, win32  # noqa: E402
from ghosttab.assistant import Panel, SelectionButton  # noqa: E402
from ghosttab.dictation import Indicator  # noqa: E402
from ghosttab.focus import Rect  # noqa: E402
from ghosttab.overlay import Overlay  # noqa: E402

X0, Y0 = 120, 120
shots: list[tuple[str, Image.Image]] = []


def pump(root, seconds=0.25):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def grab(root, name, hwnd, margin=18):
    pump(root)
    r = win32.wintypes.RECT()
    win32.user32.GetWindowRect(hwnd, win32.ctypes.byref(r))
    shots.append((name, ImageGrab.grab(bbox=(r.left - margin, r.top - margin, r.right + margin, r.bottom + margin),
                                       all_screens=True)))


def backdrop(root, color, w, h, text, fg):
    win = tk.Toplevel(root)
    win.configure(bg=color)
    tk.Label(win, text=text, bg=color, fg=fg, font=("Segoe UI", 12), anchor="w").place(x=16, y=18)
    hwnd = ui.make_floating(win, click_through=True)
    ui.show_floating(win, hwnd, X0, Y0, w, h)
    return win, hwnd


class FakeAssistant:
    def run(self, instruction):
        pass

    def replace(self):
        pass

    def copy(self):
        pass

    def close(self, refocus=True):
        pass


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ui_preview.png")
    win32.enable_dpi_awareness()
    root = tk.Tk()
    overlay = Overlay(root, "#8c8c8c")

    for color, fg, name in (("#ffffff", "#1a1a1a", "autocomplete, light field"),
                            ("#1e1f22", "#e3e5e8", "autocomplete, dark field")):
        win, hwnd = backdrop(root, color, 640, 64, "Thanks for the update, I will", fg)
        pump(root)
        label = win.winfo_children()[0]
        caret_x = X0 + 16 + label.winfo_reqwidth()
        overlay.show(" send the revised draft by Friday.", Rect(caret_x, Y0 + 16, 1, 26), Rect(X0, Y0, 640, 64), X0 + 16)
        pump(root, 0.4)
        grab(root, name, hwnd, margin=4)
        overlay.hide()
        win.destroy()

    ind = Indicator(root)
    ind.listening(X0, Y0)
    for lv in (0.02, 0.06, 0.03, 0.08, 0.05, 0.07):
        ind.set_level(lv)
    grab(root, "dictation, listening", ind.hwnd)
    ind.processing()
    pump(root, 0.35)
    grab(root, "dictation, transcribing", ind.hwnd)
    ind.message("Didn't hear anything")
    grab(root, "dictation, message", ind.hwnd)
    ind.hide()

    button = SelectionButton(root, lambda: None, "Ask AI about the selection  (Ctrl+Alt+A)")
    button.show(X0, Y0)
    grab(root, "selection button", button.hwnd)
    tip = ui.Tooltip(button.canvas, "Ask AI about the selection  (Ctrl+Alt+A)")
    tip._show()
    pump(root)
    r1, r2 = win32.wintypes.RECT(), win32.wintypes.RECT()
    win32.user32.GetWindowRect(button.hwnd, win32.ctypes.byref(r1))
    win32.user32.GetWindowRect(ui.frame_hwnd(tip.win), win32.ctypes.byref(r2))
    shots.append(("selection button, hover", ImageGrab.grab(
        bbox=(r1.left - 18, r1.top - 18, max(r1.right, r2.right) + 18, r2.bottom + 18), all_screens=True)))
    tip._cancel()
    button.hide()

    panel = Panel(root, FakeAssistant())
    style = win32.user32.GetWindowLongPtrW(panel.hwnd, ui.GWL_EXSTYLE) if hasattr(win32.user32, "GetWindowLongPtrW") else 0
    ui.user32.SetWindowLongPtrW(panel.hwnd, ui.GWL_EXSTYLE, style | ui.WS_EX_NOACTIVATE)
    panel.win.deiconify = lambda: ui.user32.ShowWindow(panel.hwnd, ui.SW_SHOWNOACTIVATE)
    panel.win.lift = lambda: None
    selection = "i has went to the store yesterday and buyed three apple"
    anchor = (X0, Y0 - 40, Y0 - 10)

    panel.open(selection, True, anchor, focus=False)
    grab(root, "assistant, opened", panel.hwnd)
    panel._toggle_more()
    grab(root, "assistant, more actions", panel.hwnd)
    panel._toggle_more()
    panel.start_answer()
    panel.append("I went to the store yesterday and")
    grab(root, "assistant, streaming", panel.hwnd)
    panel.append(" bought three apples.")
    panel.done()
    grab(root, "assistant, done", panel.hwnd)
    panel.hide()
    panel.open("Die Seite erklärt, wie man den Server startet.", False, anchor, focus=False)
    panel.start_answer()
    panel.append("The page explains how to start the server.")
    panel.done()
    grab(root, "assistant, read-only selection", panel.hwnd)
    panel.hide()
    panel.open("", True, anchor, focus=False)
    grab(root, "assistant, nothing selected", panel.hwnd)
    panel.hide()
    root.destroy()

    pad, label_h = 16, 22
    width = max(img.width for _, img in shots) + 2 * pad
    height = sum(img.height + label_h + pad for _, img in shots) + pad
    sheet = Image.new("RGB", (width, height), "#808080")
    draw = ImageDraw.Draw(sheet)
    y = pad
    for name, img in shots:
        draw.text((pad, y), name, fill="#000000")
        sheet.paste(img, (pad, y + label_h))
        y += img.height + label_h + pad
    sheet.save(out)
    print(out)


if __name__ == "__main__":
    main()
