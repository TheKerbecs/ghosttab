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
from .hotkeys import Hotkey, hotkey_label
from .llm import LlamaClient
from .prompt import build_dictation_messages

log = logging.getLogger(__name__)


class Indicator:
    """Small floating status pill: a state dot, a label and a level meter."""

    W, H = 300, 34

    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.configure(bg=ui.BORDER)
        self.canvas = tk.Canvas(self.win, width=self.W - 2, height=self.H - 2, bg=ui.BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(padx=1, pady=1)
        self.dot = self.canvas.create_oval(12, 11, 24, 23, fill=ui.RECORDING, outline="")
        self.label = self.canvas.create_text(34, 17, anchor="w", fill=ui.FG, font=ui.FONT, text="")
        self.meter_bg = self.canvas.create_rectangle(222, 14, 286, 20, fill=ui.BG_RAISED, outline="")
        self.meter = self.canvas.create_rectangle(222, 14, 222, 20, fill=ui.ACCENT, outline="")
        self.hwnd = ui.make_floating(self.win, click_through=True)
        self.visible = False

    def show(self, x: int, y: int):
        x, y = ui.clamp_to_screen(self.win, x, y, self.W, self.H)
        ui.show_floating(self.win, self.hwnd, x, y, self.W, self.H)
        self.visible = True

    def set_state(self, text: str, color: str, meter: bool):
        self.canvas.itemconfigure(self.label, text=text)
        self.canvas.itemconfigure(self.dot, fill=color)
        state = "normal" if meter else "hidden"
        self.canvas.itemconfigure(self.meter_bg, state=state)
        self.canvas.itemconfigure(self.meter, state=state)

    def set_level(self, level: float):
        width = int(64 * min(1.0, level * 8))
        self.canvas.coords(self.meter, 222, 14, 222 + width, 20)

    def hide(self):
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
                    self._flash("Dictation failed", ui.RECORDING)
                finally:
                    self.state = "idle"

    def _session(self):
        ctx = read_focus(self.own_pid)
        target_hwnd = win32.foreground_window()[0]
        if ctx is not None and ctx.field.w > 0:
            x, y = ctx.field.x, ctx.field.y + ctx.field.h + 6
        else:
            cx, cy = win32.cursor_pos()
            x, y = cx - Indicator.W // 2, cy + 24
        try:
            self.recorder.start()
        except Exception as exc:
            log.warning("cannot open the microphone: %s", exc)
            self._flash("No microphone available", ui.RECORDING)
            return
        self.state = "recording"
        stop_hint = hotkey_label(self.cfg.dictation_hotkey)
        self.ui_post(lambda: (self.indicator.set_state(f"Listening  ({stop_hint} to finish)", ui.RECORDING, True),
                              self.indicator.show(x, y)))
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
            self._flash("Didn't hear anything", ui.FG_MUTED)
            return

        self.state = "processing"
        self.ui_post(lambda: self.indicator.set_state("Transcribing...", ui.BUSY, False))
        t0 = time.perf_counter()
        transcript = self.client.transcribe(wav)
        t1 = time.perf_counter()
        log.info("dictation: transcript %r (%.0f ms)", transcript, (t1 - t0) * 1000)
        if not transcript:
            self._flash("Didn't catch that", ui.FG_MUTED)
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
            self._flash("Focus changed: copied to clipboard", ui.ACCENT)

    def _flash(self, message: str, color: str, seconds: float = 2.0):
        def show():
            self.indicator.set_state(message, color, False)
            if not self.indicator.visible:
                cx, cy = win32.cursor_pos()
                self.indicator.show(cx - Indicator.W // 2, cy + 24)
        self.ui_post(show)
        time.sleep(seconds)
        self.ui_post(self.indicator.hide)
