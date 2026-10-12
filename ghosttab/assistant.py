"""Selection assistant: select text anywhere, then ask the model to rewrite,
translate, explain or answer something about it.

Opened with the hotkey, or by clicking the dot that appears next to a mouse
selection. The answer streams into a panel and can replace the selection
(when the selected text is editable) or be copied.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from typing import Callable

import uiautomation as uia

from . import ui, win32
from .config import Config
from .focus import FocusContext, read_focus
from .hotkeys import Hotkey
from .llm import Cancelled, LlamaClient
from .prompt import QUICK_ACTIONS, build_assistant_messages

log = logging.getLogger(__name__)

DOT_SIZE = 16
DOT_TIMEOUT_MS = 4000
DRAG_MIN_PX = 8
DOUBLE_CLICK_S = 0.5


class SelectionDot:
    def __init__(self, root: tk.Tk, on_click: Callable[[], None]):
        self.win = tk.Toplevel(root)
        self.win.configure(bg=ui.BG)
        self.canvas = tk.Canvas(self.win, width=DOT_SIZE, height=DOT_SIZE, bg=ui.BG, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.create_oval(2, 2, DOT_SIZE - 2, DOT_SIZE - 2, fill=ui.ACCENT, outline="")
        self.canvas.bind("<Button-1>", lambda e: on_click())
        self.canvas.configure(cursor="hand2")
        self.win.attributes("-transparentcolor", ui.BG)
        self.hwnd = ui.make_floating(self.win, click_through=False)
        self.visible = False
        self._timer = None

    def show(self, x: int, y: int):
        ui.show_floating(self.win, self.hwnd, x, y, DOT_SIZE, DOT_SIZE)
        self.visible = True
        if self._timer:
            self.win.after_cancel(self._timer)
        self._timer = self.win.after(DOT_TIMEOUT_MS, self.hide)

    def hide(self):
        if self._timer:
            self.win.after_cancel(self._timer)
            self._timer = None
        if self.visible:
            ui.hide(self.hwnd)
            self.visible = False


class Panel:
    W = 540

    def __init__(self, root: tk.Tk, assistant: "Assistant"):
        self.assistant = assistant
        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=ui.BORDER)
        body = tk.Frame(win, bg=ui.BG, padx=12, pady=10)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=ui.BG)
        header.pack(fill="x")
        self.title = tk.Label(header, text="", bg=ui.BG, fg=ui.FG_MUTED, font=ui.FONT_SMALL, anchor="w")
        self.title.pack(side="left", fill="x", expand=True)
        close = tk.Label(header, text="Esc", bg=ui.BG, fg=ui.FG_MUTED, font=ui.FONT_SMALL, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.assistant.close())
        for widget in (header, self.title):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.preview = tk.Label(body, text="", bg=ui.BG, fg=ui.FG, font=ui.FONT_SMALL, anchor="w",
                                justify="left", wraplength=self.W - 30)
        self.preview.pack(fill="x", pady=(4, 8))

        actions = tk.Frame(body, bg=ui.BG)
        actions.pack(fill="x")
        for i, action in enumerate(QUICK_ACTIONS):
            b = tk.Label(actions, text=action.label, bg=ui.BG_RAISED, fg=ui.FG, font=ui.FONT_SMALL,
                         padx=8, pady=3, cursor="hand2")
            b.grid(row=i // 5, column=i % 5, padx=(0, 6), pady=(0, 6), sticky="w")
            b.bind("<Button-1>", lambda e, a=action: self.assistant.run(a.instruction))
            b.bind("<Enter>", lambda e, w=b: w.configure(bg=ui.BORDER))
            b.bind("<Leave>", lambda e, w=b: w.configure(bg=ui.BG_RAISED))

        self.entry = tk.Entry(body, bg=ui.BG_RAISED, fg=ui.FG, insertbackground=ui.FG, relief="flat",
                              font=ui.FONT, highlightthickness=1, highlightbackground=ui.BORDER,
                              highlightcolor=ui.ACCENT)
        self.entry.pack(fill="x", ipady=5, pady=(2, 8))

        self.output = tk.Text(body, height=1, wrap="word", bg=ui.BG, fg=ui.FG, relief="flat", font=ui.FONT,
                              highlightthickness=0, padx=0, pady=0, state="disabled", cursor="arrow")
        self.output.pack(fill="both", expand=True)

        footer = tk.Frame(body, bg=ui.BG)
        footer.pack(fill="x", pady=(8, 0))
        self.status = tk.Label(footer, text="", bg=ui.BG, fg=ui.FG_MUTED, font=ui.FONT_SMALL, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.copy_btn = self._button(footer, "Copy", self.assistant.copy)
        self.replace_btn = self._button(footer, "Replace", self.assistant.replace, accent=True)

        win.bind("<Escape>", lambda e: self.assistant.close())
        self.entry.bind("<Return>", lambda e: self._submit())
        win.bind("<Control-Return>", lambda e: self.assistant.replace())
        win.bind("<Control-Shift-C>", lambda e: self.assistant.copy())
        for i, action in enumerate(QUICK_ACTIONS[:9]):
            win.bind(f"<Control-Key-{i + 1}>",
                     lambda e, a=action: (self.assistant.run(a.instruction), "break")[1])
        self.hwnd = ui.frame_hwnd(win)
        self.visible = False
        self._drag = (0, 0)
        self._can_replace = False

    def _button(self, parent, text, command, accent=False):
        bg = ui.ACCENT if accent else ui.BG_RAISED
        b = tk.Label(parent, text=text, bg=bg, fg=ui.FG, font=ui.FONT_SMALL, padx=10, pady=3, cursor="hand2")
        b.pack(side="right", padx=(6, 0))
        b.bind("<Button-1>", lambda e: command())
        return b

    def _drag_start(self, event):
        self._drag = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _drag_move(self, event):
        self.win.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _submit(self):
        text = self.entry.get().strip()
        if text:
            self.assistant.run(text)

    def open(self, ctx: FocusContext | None, selection: str, can_replace: bool, x: int, y: int):
        app = ctx.app if ctx else ""
        self.title.configure(text=f"ghosttab  {app}".rstrip())
        if selection.strip():
            preview = " ".join(selection.split())
            self.preview.configure(text=preview[:220] + ("..." if len(preview) > 220 else ""), fg=ui.FG)
        else:
            self.preview.configure(text="No text selected. Ask anything; the answer can be inserted at the cursor.",
                                   fg=ui.FG_MUTED)
        self._can_replace = can_replace
        self.replace_btn.configure(text="Replace" if selection.strip() else "Insert")
        self.entry.delete(0, "end")
        self.set_output("")
        hint = "Ctrl+1-9 quick action, Enter ask"
        self.status.configure(text=hint + (", Ctrl+Enter replace" if can_replace else ""))
        self._set_replace_enabled(False)
        self.win.update_idletasks()
        h = self.win.winfo_reqheight()
        x, y = ui.clamp_to_screen(self.win, x, y, self.W, h + 260)
        self.win.geometry(f"{self.W}x{h}+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        win32.force_foreground(self.hwnd)
        self.entry.focus_force()
        self.visible = True

    def _set_replace_enabled(self, on: bool):
        on = on and self._can_replace
        self.replace_btn.configure(bg=ui.ACCENT if on else ui.BG_RAISED, fg=ui.FG if on else ui.FG_MUTED)

    def set_output(self, text: str):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")
        self._fit()

    def append(self, text: str):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.configure(state="disabled")
        self.output.see("end")
        self._fit()

    def _fit(self):
        self.output.update_idletasks()
        lines = self.output.count("1.0", "end", "displaylines")
        n = lines[0] if lines else 1
        self.output.configure(height=max(1, min(n, 16)))
        self.win.update_idletasks()
        self.win.geometry(f"{self.W}x{self.win.winfo_reqheight()}")

    def busy(self, text: str):
        self.status.configure(text=text)
        self._set_replace_enabled(False)

    def done(self):
        if self._can_replace:
            verb = "replace" if self.replace_btn.cget("text") == "Replace" else "insert"
            self.status.configure(text=f"Ctrl+Enter {verb}, Ctrl+Shift+C copy, Esc close")
        else:
            self.status.configure(text="Read-only selection: Ctrl+Shift+C copy, Esc close")
        self._set_replace_enabled(True)

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False


class Assistant:
    def __init__(self, cfg: Config, client: LlamaClient, ui_post: Callable[[Callable], None], root: tk.Tk):
        self.cfg = cfg
        self.client = client
        self.ui_post = ui_post
        self.root = root
        self.hotkey = Hotkey.parse(cfg.assistant_hotkey)
        self.own_pid = os.getpid()
        self.blocked = {e.lower() for e in cfg.blocked_exes}
        self.panel = Panel(root, self)
        self.dot = SelectionDot(root, self._dot_clicked)
        self.q: queue.Queue = queue.Queue()
        self.pending: FocusContext | None = None
        self.target_hwnd = 0
        self.can_replace = False
        self.selection = ""
        self.ctx: FocusContext | None = None
        self.answer = ""
        self.answer_done = False
        self.cancel = threading.Event()
        self.dot_visible = False
        self.panel_open = False
        self._down = None
        self._last_up = (0.0, 0, 0)
        self.mouse_hook = win32.MouseHook(self.on_mouse) if cfg.assistant_selection_dot else None
        threading.Thread(target=self._run, name="assistant", daemon=True).start()
        if self.mouse_hook:
            self.mouse_hook.start()

    # Hook threads ---------------------------------------------------------------

    def handle_key(self, vk, shift, ctrl, alt, win) -> bool:
        if self.hotkey.matches(vk, shift, ctrl, alt, win):
            self.q.put(("hotkey",))
            return True
        if self.dot_visible and vk not in win32.MODIFIER_VKS:
            self.dot_visible = False
            self.ui_post(self.dot.hide)
        return False

    def on_mouse(self, msg, x, y):
        if self._is_own_window(x, y):
            return
        now = time.monotonic()
        if msg == win32.WM_LBUTTONDOWN:
            self._down = (x, y)
            if self.dot_visible:
                self.dot_visible = False
                self.ui_post(self.dot.hide)
        elif msg == win32.WM_LBUTTONUP and self._down is not None:
            dx, dy = x - self._down[0], y - self._down[1]
            last_t, lx, ly = self._last_up
            dragged = dx * dx + dy * dy >= DRAG_MIN_PX * DRAG_MIN_PX
            double = now - last_t <= DOUBLE_CLICK_S and abs(x - lx) <= 4 and abs(y - ly) <= 4
            self._last_up = (now, x, y)
            if dragged or double:
                self.q.put(("maybe_selection", x, y))
        elif self.dot_visible:
            self.dot_visible = False
            self.ui_post(self.dot.hide)

    def _is_own_window(self, x, y) -> bool:
        hwnd = win32.window_from_point(x, y)
        return bool(hwnd) and win32.window_pid(hwnd) == self.own_pid

    # Worker thread ---------------------------------------------------------------

    def _run(self):
        with uia.UIAutomationInitializerInThread():
            while True:
                cmd, *args = self.q.get()
                try:
                    getattr(self, f"_do_{cmd}")(*args)
                except Exception:
                    log.exception("assistant command %s failed", cmd)

    def _eligible(self, ctx: FocusContext) -> bool:
        return not ctx.is_password and ctx.app.lower() not in self.blocked

    def _do_hotkey(self):
        if self.panel_open:
            self.ui_post(self.close)
            return
        ctx = read_focus(self.own_pid)
        if ctx is not None and not self._eligible(ctx):
            return
        selection = ctx.selection if ctx else ""
        editable = bool(ctx and ctx.editable)
        if not selection.strip() and (ctx is None or not ctx.editable):
            # Nothing visible to UI Automation: fall back to copying it.
            selection = self._copy_selection()
        self._open(ctx, selection, editable)

    def _do_maybe_selection(self, x, y):
        time.sleep(0.08)  # let the app update its selection
        ctx = read_focus(self.own_pid)
        if ctx is None or not ctx.selection.strip() or not self._eligible(ctx):
            return
        self.pending = ctx
        self.dot_visible = True
        log.info("assistant: selection dot for %d chars", len(ctx.selection))
        self.ui_post(lambda: self.dot.show(x + 12, y + 10))

    def _do_open_pending(self):
        ctx = self.pending
        self.pending = None
        if ctx is not None:
            self._open(ctx, ctx.selection, ctx.editable)

    def _open(self, ctx: FocusContext | None, selection: str, editable: bool):
        self.ctx = ctx
        self.selection = selection
        self.can_replace = editable
        self.target_hwnd = (ctx.hwnd if ctx and ctx.hwnd else win32.foreground_window()[0])
        self.answer, self.answer_done = "", False
        if ctx is not None and ctx.selection_rect is not None:
            r = ctx.selection_rect
            x, y = r.x, r.y + r.h + 8
        elif ctx is not None:
            x, y = ctx.caret.x, ctx.caret.y + ctx.caret.h + 8
        else:
            x, y = win32.cursor_pos()
        self.panel_open = True
        log.info("assistant: opened in %s with %d chars selected", ctx.app if ctx else "?", len(selection))
        self.ui_post(lambda: self.panel.open(ctx, selection, editable, x, y))

    def _copy_selection(self) -> str:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(
                win32.key_down(vk) for vk in (win32.VK_CONTROL, win32.VK_MENU, win32.VK_SHIFT, win32.VK_LWIN)):
            time.sleep(0.02)
        before = win32.clipboard_sequence()
        saved = win32.get_clipboard_text()
        win32.send_combo(win32.VK_CONTROL, ord("C"))
        for _ in range(20):
            time.sleep(0.02)
            if win32.clipboard_sequence() != before:
                break
        else:
            return ""
        text = win32.get_clipboard_text() or ""
        if saved is not None:
            win32.set_clipboard_text(saved)
        return text

    def _do_insert(self, text: str):
        if not self.target_hwnd or not win32.force_foreground(self.target_hwnd):
            win32.set_clipboard_text(text)
            log.info("assistant: target window gone, answer copied")
            return
        time.sleep(0.15)
        if win32.foreground_window()[0] != self.target_hwnd:
            win32.set_clipboard_text(text)
            return
        win32.send_text(text)
        log.info("assistant: inserted %d chars", len(text))

    def _do_refocus(self):
        if self.target_hwnd:
            win32.force_foreground(self.target_hwnd)

    # UI thread -----------------------------------------------------------------

    def _dot_clicked(self):
        self.dot_visible = False
        self.dot.hide()
        self.q.put(("open_pending",))

    def run(self, instruction: str):
        self.cancel.set()
        cancel = self.cancel = threading.Event()
        self.answer, self.answer_done = "", False
        self.panel.set_output("")
        self.panel.busy("Generating...")
        messages = build_assistant_messages(self.selection, instruction, self.ctx.app if self.ctx else "")
        log.info("assistant: %s", instruction)

        def work():
            t0 = time.perf_counter()

            def on_delta(piece):
                if not cancel.is_set():
                    self.answer += piece
                    self.ui_post(lambda p=piece: self.panel.append(p))
            try:
                self.client.chat_stream(messages, on_delta, cancel=cancel)
            except Cancelled:
                return
            except Exception as exc:
                log.warning("assistant request failed: %s", exc)
                message = f"Request failed: {exc}"
                self.ui_post(lambda: self.panel.busy(message))
                return
            if cancel.is_set():
                return
            self.answer = self.answer.strip()
            self.answer_done = True
            log.info("assistant: answer %d chars in %.0f ms", len(self.answer), (time.perf_counter() - t0) * 1000)
            self.ui_post(self.panel.done)

        threading.Thread(target=work, name="assistant-request", daemon=True).start()

    def replace(self):
        if not (self.answer_done and self.answer and self.can_replace):
            return
        text = self.answer
        self._hide()
        self.q.put(("insert", text))

    def copy(self):
        if self.answer:
            win32.set_clipboard_text(self.answer)
            self.panel.status.configure(text="Copied")

    def close(self):
        self._hide()
        self.q.put(("refocus",))

    def _hide(self):
        self.cancel.set()
        self.panel.hide()
        self.panel_open = False

