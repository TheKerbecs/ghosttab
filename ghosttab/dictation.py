"""Dictation: press the hotkey, speak, and the cleaned-up text is typed at the
cursor.

The model transcribes the audio itself (Gemma 4 E2B/E4B have an audio
encoder; llama-server needs the mmproj file loaded), then a second, text-only
request removes filler words, applies self-corrections and spoken
punctuation. Recording stops on the hotkey, after a pause in speech, or at
the time limit; Esc cancels.
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
from .audio import FileRecorder, Recorder
from .config import Config
from .focus import read_focus
from .hotkeys import Hotkey
from .llm import LlamaClient
from .prompt import build_dictation_messages

log = logging.getLogger(__name__)


class Indicator:
    """A small pill under the cursor: microphone, live level bars and a word
    of state. Never takes focus."""

    H = 32
    BARS = 5

    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.configure(bg=ui.BG)
        self.canvas = tk.Canvas(self.win, height=self.H, bg=ui.BG, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.mic = self.canvas.create_text(14, self.H // 2, text=ui.ICON_MIC, font=ui.icon_font(11),
                                           fill=ui.RECORDING, anchor="w")
        self.bars = [self.canvas.create_rectangle(0, 0, 0, 0, fill=ui.FG_MUTED, outline="")
                     for _ in range(self.BARS)]
        self.label = self.canvas.create_text(0, self.H // 2, text="", font=ui.font(9), fill=ui.FG_MUTED, anchor="w")
        self.levels = [0.0] * self.BARS
        self.hwnd = ui.make_floating(self.win, click_through=False)
        ui.round_corners(self.hwnd)
        self.visible = False
        self.mode = "idle"
        self._anim = None
        self._phase = 0

    def _layout(self, text: str, show_bars: bool):
        x = 36
        for bar in self.bars:
            self.canvas.itemconfigure(bar, state="normal" if show_bars else "hidden")
        if show_bars:
            x += self.BARS * 5 + 8
        self.canvas.coords(self.label, x, self.H // 2)
        self.canvas.itemconfigure(self.label, text=text)
        # Measure the drawn text: a separate Font object measures differently under DPI scaling.
        width = self.canvas.bbox(self.label)[2] + 14
        self.canvas.configure(width=width)
        return width

    def _draw_bars(self, heights: list[float]):
        for i, (bar, h) in enumerate(zip(self.bars, heights)):
            x0 = 36 + i * 5
            half = max(1.5, min(8.0, h * 8))
            self.canvas.coords(bar, x0, self.H / 2 - half, x0 + 2, self.H / 2 + half)

    def listening(self, x: int, y: int):
        self._stop_anim()
        self.mode = "listening"
        self.canvas.itemconfigure(self.mic, fill=ui.RECORDING)
        self.levels = [0.0] * self.BARS
        self._draw_bars(self.levels)
        self._show(x, y, self._layout("Listening", True))

    def set_level(self, level: float):
        if self.mode != "listening":
            return
        self.levels = self.levels[1:] + [min(1.0, level * 10)]
        # Mirror around the middle bar so it reads as a waveform.
        lv = self.levels
        self._draw_bars([lv[1], lv[3], lv[4], lv[2], lv[0]])

    def processing(self):
        self.mode = "processing"
        self.canvas.itemconfigure(self.mic, fill=ui.FG_MUTED)
        width = self._layout("Transcribing", True)
        if self.visible:
            self.win.geometry(f"{width}x{self.H}")
        self._phase = 0
        self._animate()

    def _animate(self):
        self._phase += 1
        heights = [0.25 + 0.45 * (1 if (self._phase + i) % self.BARS == 0 else 0) for i in range(self.BARS)]
        self._draw_bars(heights)
        self._anim = self.win.after(110, self._animate)

    def _stop_anim(self):
        if self._anim:
            self.win.after_cancel(self._anim)
            self._anim = None

    def message(self, text: str, x: int | None = None, y: int | None = None):
        self._stop_anim()
        self.mode = "message"
        self.canvas.itemconfigure(self.mic, fill=ui.FG_MUTED)
        width = self._layout(text, False)
        if self.visible:
            self.win.geometry(f"{width}x{self.H}")
        elif x is not None:
            self._show(x, y, width)

    def _show(self, x: int, y: int, width: int):
        x, y = ui.clamp_to_screen(self.win, x, y, width, self.H)
        ui.show_floating(self.win, self.hwnd, x, y, width, self.H)
        self.visible = True

    def hide(self):
        self._stop_anim()
        self.mode = "idle"
        if self.visible:
            ui.hide(self.hwnd)
            self.visible = False


class Dictation:
    def __init__(self, cfg: Config, client: LlamaClient, ui_post: Callable[[Callable], None], root: tk.Tk):
        self.cfg = cfg
        self.client = client
        self.ui_post = ui_post
        self.hotkey = Hotkey.parse(cfg.dictation_hotkey)
        self.indicator = Indicator(root)
        self.recorder = FileRecorder(cfg.dictation_input_file) if cfg.dictation_input_file else Recorder()
        self.q: queue.Queue = queue.Queue()
        self.state = "idle"  # idle | recording | processing
        self.anchor = (0, 0)
        self.own_pid = os.getpid()
        threading.Thread(target=self._run, name="dictation", daemon=True).start()

    def handle_key(self, vk, shift, ctrl, alt, win) -> bool:
        """Runs on the keyboard hook thread. True swallows the key."""
        if self.hotkey.matches(vk, shift, ctrl, alt, win):
            self.q.put("toggle")
            return True
        if self.state == "recording" and vk == win32.VK_ESCAPE:
            self.q.put("cancel")
            return True
        return False

    def _run(self):
        with uia.UIAutomationInitializerInThread():
            while True:
                cmd = self.q.get()
                try:
                    if cmd == "toggle" and self.state == "idle":
                        self._session()
                except Exception:
                    log.exception("dictation failed")
                    self._flash("Dictation failed")
                finally:
                    self.state = "idle"

    def _session(self):
        ctx = read_focus(self.own_pid)
        target_hwnd = win32.foreground_window()[0]
        if ctx is not None and ctx.caret_exact:
            x, y = ctx.caret.x - 14, ctx.caret.y + ctx.caret.h + 8
        elif ctx is not None and ctx.field.w > 0:
            x, y = ctx.field.x, ctx.field.y + ctx.field.h + 6
        else:
            cx, cy = win32.cursor_pos()
            x, y = cx - 40, cy + 24
        self.anchor = (x, y)
        try:
            self.recorder.start()
        except Exception as exc:
            log.warning("cannot open the microphone: %s", exc)
            self._flash("No microphone")
            return
        self.state = "recording"
        self.ui_post(lambda: self.indicator.listening(x, y))
        log.info("dictation: recording")

        cancelled = False
        silence_s = self.cfg.dictation_silence_ms / 1000
        while True:
            try:
                cmd = self.q.get(timeout=0.05)
            except queue.Empty:
                cmd = None
            if cmd == "toggle":
                break
            if cmd == "cancel":
                cancelled = True
                break
            level = self.recorder.level
            self.ui_post(lambda lv=level: self.indicator.set_level(lv))
            if silence_s > 0 and self.recorder.heard_speech and self.recorder.silence_for() >= silence_s:
                break
            if time.monotonic() - self.recorder.started_at >= self.cfg.dictation_max_s:
                break
        wav = self.recorder.stop()
        if cancelled:
            log.info("dictation: cancelled")
            self.ui_post(self.indicator.hide)
            return
        if not self.recorder.heard_speech:
            self._flash("Didn't hear anything")
            return

        self.state = "processing"
        self.ui_post(self.indicator.processing)
        t0 = time.perf_counter()
        transcript = self.client.transcribe(wav)
        t1 = time.perf_counter()
        log.info("dictation: transcript %r (%.0f ms)", transcript, (t1 - t0) * 1000)
        if not transcript:
            self._flash("Didn't catch that")
            return
        text = transcript
        if self.cfg.dictation_cleanup:
            cleaned = self.client.chat(build_dictation_messages(transcript, self.cfg.dictation_vocabulary),
                                       max_tokens=1024).strip()
            if cleaned:
                text = cleaned
            log.info("dictation: cleaned %r (%.0f ms)", text, (time.perf_counter() - t1) * 1000)

        self._deliver(text, ctx, target_hwnd)

    def _deliver(self, text: str, ctx, target_hwnd: int):
        if ctx is not None and ctx.preceding and not ctx.preceding[-1].isspace() and not text[:1].isspace():
            text = " " + text
        if target_hwnd and win32.foreground_window()[0] == target_hwnd and (ctx is None or ctx.editable):
            win32.send_text(text)
            self.ui_post(self.indicator.hide)
        else:
            win32.set_clipboard_text(text.strip())
            self._flash("Copied to clipboard")

    def _flash(self, message: str, seconds: float = 1.6):
        x, y = self.anchor
        self.ui_post(lambda: self.indicator.message(message, x, y))
        time.sleep(seconds)
        self.ui_post(self.indicator.hide)
