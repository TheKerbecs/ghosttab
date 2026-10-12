"""Selection assistant: select text anywhere, then ask the model to rewrite,
translate, explain or answer something about it.

Opened with the hotkey, or by clicking the small button that appears at the
end of a mouse selection. The answer streams into a compact popover and can
replace the selection (when the selected text is editable) or be copied.
Follow-up requests refine the latest answer.
"""

from __future__ import annotations

import logging
import math
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
from .hotkeys import Hotkey, hotkey_label
from .llm import Cancelled, LlamaClient
from .prompt import QuickAction, build_assistant_messages, panel_actions

log = logging.getLogger(__name__)

BUTTON_SIZE = 24
BUTTON_TIMEOUT_MS = 3500
DRAG_MIN_PX = 8
DOUBLE_CLICK_S = 0.5


def _sparkle(cx: float, cy: float, outer: float, inner: float) -> list[float]:
    points = []
    for i in range(8):
        r = outer if i % 2 == 0 else inner
        a = math.pi / 4 * i - math.pi / 2
        points += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return points


class SelectionButton:
    """A small rounded button with a sparkle, shown at the end of a selection.
    It never takes focus, so the selection stays intact when it is clicked."""

    def __init__(self, root: tk.Tk, on_click: Callable[[], None], hint: str):
        self.win = tk.Toplevel(root)
        self.win.configure(bg=ui.SURFACE)
        s = BUTTON_SIZE
        self.canvas = tk.Canvas(self.win, width=s, height=s, bg=ui.SURFACE, highlightthickness=0, bd=0,
                                cursor="hand2")
        self.canvas.pack()
        self.canvas.create_polygon(_sparkle(s / 2, s / 2, 7.5, 2.2), fill=ui.ACCENT, outline="")
        self.canvas.create_polygon(_sparkle(s / 2 + 6, s / 2 - 6, 3, 0.9), fill=ui.ACCENT, outline="")
        self.canvas.bind("<Button-1>", lambda e: on_click())
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)
        ui.Tooltip(self.canvas, hint)
        self.hwnd = ui.make_floating(self.win, click_through=False)
        ui.round_corners(self.hwnd, small=True)
        self.visible = False
        self.hovered = False
        self._timer = None

    def _enter(self, _event):
        self.hovered = True
        self.canvas.configure(bg=ui.SURFACE_HOVER)

    def _leave(self, _event):
        self.hovered = False
        self.canvas.configure(bg=ui.SURFACE)
        self._arm()

    def _arm(self):
        if self._timer:
            self.win.after_cancel(self._timer)
        self._timer = self.win.after(BUTTON_TIMEOUT_MS, self._timeout)

    def _timeout(self):
        self._timer = None
        if not self.hovered:
            self.hide()

    def show(self, x: int, y: int):
        x, y = ui.clamp_to_screen(self.win, x, y, BUTTON_SIZE, BUTTON_SIZE, margin=2)
        ui.show_floating(self.win, self.hwnd, x, y, BUTTON_SIZE, BUTTON_SIZE)
        self.visible = True
        self._arm()

    def hide(self):
        if self._timer:
            self.win.after_cancel(self._timer)
            self._timer = None
        if self.visible:
            ui.hide(self.hwnd)
            self.visible = False


class Panel:
    """Popover: request box, action chips, streamed result, Copy / Replace."""

    W = 420

    def __init__(self, root: tk.Tk, assistant: "Assistant"):
        self.assistant = assistant
        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=ui.BORDER)
        body = self.body = tk.Frame(win, bg=ui.BG, padx=10, pady=10)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        entry_box = tk.Frame(body, bg=ui.SURFACE, padx=10, pady=7)
        entry_box.pack(fill="x")
        self.entry = tk.Entry(entry_box, bg=ui.SURFACE, fg=ui.FG, insertbackground=ui.FG, relief="flat",
                              font=ui.font(10), highlightthickness=0, bd=0)
        self.entry.pack(fill="x")
        self.placeholder = tk.Label(entry_box, text="", bg=ui.SURFACE, fg=ui.FG_FAINT, font=ui.font(10),
                                    anchor="w", cursor="xterm")
        self.placeholder.place(x=0, y=0, relwidth=1, relheight=1)
        self.placeholder.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<KeyRelease>", lambda e: self._sync_placeholder())
        self.entry.bind("<Return>", lambda e: self._submit())

        self.chips = tk.Frame(body, bg=ui.BG)
        self.chips.pack(fill="x", pady=(8, 0))
        self.more = tk.Frame(body, bg=ui.BG)

        self.result = tk.Frame(body, bg=ui.SURFACE, padx=10, pady=8)
        self.output = tk.Text(self.result, height=1, wrap="word", bg=ui.SURFACE, fg=ui.FG, relief="flat",
                              font=ui.font(10), highlightthickness=0, padx=0, pady=0, bd=0,
                              state="disabled", cursor="arrow", spacing2=2)
        self.output.pack(fill="both", expand=True)

        self.footer = tk.Frame(body, bg=ui.BG)
        self.status = tk.Label(self.footer, text="", bg=ui.BG, fg=ui.FG_FAINT, font=ui.font(9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.primary = ui.Chip(self.footer, "Replace", lambda: self.assistant.replace(), accent=True)
        self.primary.pack(side="right")
        self.copy_chip = ui.Chip(self.footer, "Copy", lambda: self.assistant.copy())
        self.copy_chip.pack(side="right", padx=(0, 6))

        win.bind("<Escape>", lambda e: self.assistant.close())
        win.bind("<Control-Return>", lambda e: self.assistant.replace())
        win.bind("<Control-Shift-C>", lambda e: self.assistant.copy())
        win.bind("<FocusOut>", lambda e: win.after(150, self._check_focus))
        self.hwnd = ui.frame_hwnd(win)
        ui.round_corners(self.hwnd)
        self.visible = False
        self.can_replace = False
        self.actions: list[QuickAction] = []

    # Building ---------------------------------------------------------------------

    def _fill_chips(self, primary: list[QuickAction], more: list[QuickAction]):
        for frame in (self.chips, self.more):
            for child in frame.winfo_children():
                child.destroy()
        self.actions = primary + more
        for action in primary:
            ui.Chip(self.chips, action.label, lambda a=action: self.assistant.run(a.instruction)).pack(
                side="left", padx=(0, 6))
        toggle = ui.Chip(self.chips, "\u2026", self._toggle_more)
        toggle.pack(side="left")
        ui.Tooltip(toggle, "More actions")
        for action in more:
            ui.Chip(self.more, action.label, lambda a=action: self.assistant.run(a.instruction)).pack(
                side="left", padx=(0, 6))
        for i, action in enumerate(self.actions[:9]):
            self.win.bind(f"<Control-Key-{i + 1}>",
                          lambda e, a=action: (self.assistant.run(a.instruction), "break")[1])

    def _toggle_more(self):
        if self.more.winfo_ismapped():
            self.more.pack_forget()
        else:
            self.more.pack(fill="x", pady=(6, 0), after=self.chips)
        self._fit()

    def _sync_placeholder(self):
        if self.entry.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=0, y=0, relwidth=1, relheight=1)

    def _set_placeholder(self, text: str):
        self.placeholder.configure(text=text)
        self._sync_placeholder()

    def _submit(self):
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self._sync_placeholder()
            self.assistant.run(text)

    def _check_focus(self):
        # Clicking anywhere outside the popover closes it, like a menu.
        if self.visible and self.win.focus_get() is None:
            self.assistant.close(refocus=False)

    # States -----------------------------------------------------------------------

    def open(self, selection: str, can_replace: bool, anchor: tuple[int, int, int], focus: bool = True):
        self.can_replace = can_replace
        snippet = " ".join(selection.split())
        if snippet:
            short = snippet if len(snippet) <= 34 else snippet[:32].rstrip() + "\u2026"
            self._set_placeholder(f"Ask about \u201c{short}\u201d")
            self._fill_chips(*panel_actions(selection))
            self.chips.pack(fill="x", pady=(8, 0), after=self.entry.master)
        else:
            self._set_placeholder("Ask anything")
            self.chips.pack_forget()
        self.more.pack_forget()
        self.result.pack_forget()
        self.footer.pack_forget()
        self.primary.configure(text="Replace" if snippet else "Insert")
        self.entry.delete(0, "end")
        self._sync_placeholder()
        self._set_output("")
        self.win.update_idletasks()
        h = self.win.winfo_reqheight()
        x, top, bottom = anchor
        x, y = ui.place_near(self.win, x, top, bottom, self.W, h + 220)
        self.win.geometry(f"{self.W}x{h}+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        self.visible = True
        if focus:
            win32.force_foreground(self.hwnd)
            self.entry.focus_force()

    def start_answer(self):
        self._set_output("")
        self.result.pack(fill="both", expand=True, pady=(10, 0))
        self.footer.pack(fill="x", pady=(8, 0))
        self.status.configure(text="Writing\u2026")
        self._show_actions(False)
        self._fit()

    def append(self, text: str):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.configure(state="disabled")
        self.output.see("end")
        self._fit()

    def done(self):
        self._show_actions(True)
        if self.can_replace:
            verb = self.primary.cget("text").lower()
            self.status.configure(text=f"Ctrl+Enter to {verb}")
        else:
            self.status.configure(text="Read-only text")
        self._set_placeholder("Refine the result")

    def error(self, message: str):
        self.status.configure(text=message)
        self._show_actions(False)

    def copied(self):
        self.status.configure(text="Copied")

    def _show_actions(self, on: bool):
        if on and self.can_replace:
            self.primary.pack(side="right")
        else:
            self.primary.pack_forget()
        if on:
            self.copy_chip.pack(side="right", padx=(0, 6))
        else:
            self.copy_chip.pack_forget()

    def _set_output(self, text: str):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def _fit(self):
        self.output.update_idletasks()
        lines = self.output.count("1.0", "end", "displaylines")
        n = lines[0] if lines else 1
        self.output.configure(height=max(1, min(n, 14)))
        self.win.update_idletasks()
        self.win.geometry(f"{self.W}x{self.win.winfo_reqheight()}")

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False


class Assistant:
    def __init__(self, cfg: Config, client: LlamaClient, ui_post: Callable[[Callable], None], root: tk.Tk):
        self.cfg = cfg
        self.client = client
        self.ui_post = ui_post
        self.hotkey = Hotkey.parse(cfg.assistant_hotkey)
        self.own_pid = os.getpid()
        self.blocked = {e.lower() for e in cfg.blocked_exes}
        self.panel = Panel(root, self)
        self.button = SelectionButton(root, self._button_clicked,
                                      f"Ask AI about the selection  ({hotkey_label(cfg.assistant_hotkey)})")
        self.q: queue.Queue = queue.Queue()
        self.pending: FocusContext | None = None
        self.target_hwnd = 0
        self.can_replace = False
        self.selection = ""
        self.ctx: FocusContext | None = None
        self.answer = ""
        self.answer_done = False
        self.cancel = threading.Event()
        self.button_visible = False
        self.panel_open = False
        self._down = None
        self._last_up = (0.0, 0, 0)
        self.mouse_hook = win32.MouseHook(self.on_mouse) if cfg.assistant_selection_button else None
        threading.Thread(target=self._run, name="assistant", daemon=True).start()
        if self.mouse_hook:
            self.mouse_hook.start()

    # Hook threads -----------------------------------------------------------------

    def handle_key(self, vk, shift, ctrl, alt, win) -> bool:
        if self.hotkey.matches(vk, shift, ctrl, alt, win):
            self.q.put(("hotkey",))
            return True
        if self.button_visible and vk not in win32.MODIFIER_VKS:
            self._hide_button()
        return False

    def on_mouse(self, msg, x, y):
        if self._is_own_window(x, y):
            return
        now = time.monotonic()
        if msg == win32.WM_LBUTTONDOWN:
            self._down = (x, y)
            if self.button_visible:
                self._hide_button()
        elif msg == win32.WM_LBUTTONUP and self._down is not None:
            dx, dy = x - self._down[0], y - self._down[1]
            last_t, lx, ly = self._last_up
            dragged = dx * dx + dy * dy >= DRAG_MIN_PX * DRAG_MIN_PX
            double = now - last_t <= DOUBLE_CLICK_S and abs(x - lx) <= 4 and abs(y - ly) <= 4
            self._last_up = (now, x, y)
            if dragged or double:
                self.q.put(("maybe_selection", x, y))
        elif self.button_visible:
            self._hide_button()

    def _hide_button(self):
        self.button_visible = False
        self.ui_post(self.button.hide)

    def _is_own_window(self, x, y) -> bool:
        hwnd = win32.window_from_point(x, y)
        return bool(hwnd) and win32.window_pid(hwnd) == self.own_pid

    # Worker thread ----------------------------------------------------------------

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
        if ctx is None or len(ctx.selection.strip()) < 2 or not self._eligible(ctx):
            return
        self.pending = ctx
        self.button_visible = True
        end = ctx.selection_end
        if end is not None:
            bx, by = end.right + 6, end.y + (end.h - BUTTON_SIZE) // 2
        else:
            bx, by = x + 10, y + 8
        log.info("assistant: selection button for %d chars", len(ctx.selection))
        self.ui_post(lambda: self.button.show(bx, by))

    def _do_open_pending(self):
        ctx = self.pending
        self.pending = None
        if ctx is not None:
            self._open(ctx, ctx.selection, ctx.editable)

    def _open(self, ctx: FocusContext | None, selection: str, editable: bool):
        self.ctx = ctx
        self.selection = selection
        self.can_replace = editable
        self.target_hwnd = ctx.hwnd if ctx and ctx.hwnd else win32.foreground_window()[0]
        self.answer, self.answer_done = "", False
        if ctx is not None and ctx.selection_rect is not None:
            first, last = ctx.selection_rect, ctx.selection_end or ctx.selection_rect
            anchor = (first.x, first.y, last.y + last.h)
        elif ctx is not None:
            anchor = (ctx.caret.x, ctx.caret.y, ctx.caret.y + ctx.caret.h)
        else:
            cx, cy = win32.cursor_pos()
            anchor = (cx, cy, cy)
        self.panel_open = True
        log.info("assistant: opened in %s with %d chars selected", ctx.app if ctx else "?", len(selection))
        self.ui_post(lambda: self.panel.open(selection, editable, anchor))

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

    # UI thread --------------------------------------------------------------------

    def _button_clicked(self):
        self.button_visible = False
        self.button.hide()
        self.q.put(("open_pending",))

    def run(self, instruction: str):
        # Follow-ups refine the latest answer; the first request works on the selection.
        source = self.answer if self.answer_done and self.answer else self.selection
        self.cancel.set()
        cancel = self.cancel = threading.Event()
        self.answer, self.answer_done = "", False
        self.panel.start_answer()
        messages = build_assistant_messages(source, instruction, self.ctx.app if self.ctx else "")
        log.debug("assistant: request %r", instruction)

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
                self.ui_post(lambda: self.panel.error(message))
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
            self.panel.copied()

    def close(self, refocus: bool = True):
        self._hide()
        if refocus:
            self.q.put(("refocus",))

    def _hide(self):
        self.cancel.set()
        self.panel.hide()
        self.panel_open = False
