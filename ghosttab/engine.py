"""The completion state machine.

  idle --keystroke--> debouncing --timer--> generating --result--> ready
  ready --Tab--> insert whole suggestion (chain another prediction unless the
                 suggestion ended a sentence)
  ready --Shift+Tab--> insert next word, keep the rest visible
  ready --typed char--> checking: if the user typed the start of the
                 suggestion keep showing the remainder, else re-predict
  any   --navigation / Esc / Enter / Ctrl-combo / focus change--> idle

Threads: the keyboard hook thread only classifies keys and decides whether to
swallow them; everything that touches UI Automation runs on the engine
thread; llama.cpp calls run on short-lived worker threads; the overlay is
updated on the Tk main thread through `ui_post`.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from typing import Callable

import uiautomation as uia

from . import win32
from .config import Config
from .context import ScreenContext
from .focus import FocusContext, read_focus
from .llm import Cancelled, LlamaClient
from .prompt import (PromptInput, advance_if_typed, build_messages, ends_sentence,
                     next_word, normalize)

log = logging.getLogger(__name__)

NAV_KEYS = {win32.VK_LEFT, win32.VK_RIGHT, win32.VK_UP, win32.VK_DOWN,
            win32.VK_HOME, win32.VK_END, win32.VK_PRIOR, win32.VK_NEXT}
DISMISS_KEYS = {win32.VK_ESCAPE, win32.VK_RETURN}
# Only complete at the end of a line (closing punctuation may follow).
END_OF_LINE = re.compile(r"[\s)\]}\"'.,!?;:]*")


def classify_key(vk: int, shift: bool, ctrl: bool, alt: bool, win: bool) -> str:
    if vk == win32.VK_TAB and not (ctrl or alt or win):
        return "accept_word" if shift else "accept_full"
    if vk in win32.MODIFIER_VKS or 0x70 <= vk <= 0x87:  # modifiers, F1-F24
        return "other"
    if vk in NAV_KEYS:
        return "navigation"
    if ctrl and alt and not win:
        # AltGr arrives as Ctrl+Alt; on German and other layouts it types
        # characters such as @, euro sign, braces and backslash.
        return "mutation"
    if vk in DISMISS_KEYS or ctrl or alt or win:
        return "dismissal"
    return "mutation"


class Engine:
    def __init__(self, cfg: Config, client: LlamaClient, ui_post: Callable[[Callable], None], overlay):
        self.cfg = cfg
        self.client = client
        self.ui_post = ui_post
        self.overlay = overlay
        self.own_pid = os.getpid()
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self._state = "idle"
        self.running = True
        # Timers are monotonic deadlines, or None when not armed.
        self.debounce_at: float | None = None
        self.check_at: float | None = None
        self.reposition_at: float | None = None
        self.poll_at: float | None = None
        self.check_attempts = 0
        self.gen_id = 0
        self.gen_ctx: FocusContext | None = None
        self.cancel_event = threading.Event()
        # The field state the visible suggestion belongs to.
        self.ctx: FocusContext | None = None
        self.current = ""
        self.remaining = ""
        self.blocked = {e.lower() for e in cfg.blocked_exes}
        self.screen = ScreenContext(cfg.screen_context_budget, cfg.screen_context_ttl_s)

    @property
    def state(self) -> str:
        with self.lock:
            return self._state

    def _set_state(self, value: str):
        with self.lock:
            if self._state != value:
                log.debug("state %s -> %s", self._state, value)
            self._state = value

    def on_key(self, vk, shift, ctrl, alt, win) -> bool:
        """Runs on the hook thread. Return True to swallow the key."""
        kind = classify_key(vk, shift, ctrl, alt, win)
        with self.lock:
            st = self._state
        if kind in ("accept_full", "accept_word"):
            if st in ("ready", "checking"):
                self.q.put(("accept", kind))
                return True
            # A Tab pressed just before the suggestion appears should not
            # move focus out of the field.
            return st == "generating" and self.cfg.swallow_tab_while_generating
        self.q.put(("key", kind))
        return False

    def run(self):
        with uia.UIAutomationInitializerInThread():
            while self.running:
                timeout = self._next_timeout()
                try:
                    ev = self.q.get(timeout=timeout)
                except queue.Empty:
                    ev = None
                try:
                    if ev is not None:
                        self._handle(ev)
                    self._fire_timers()
                except Exception:
                    log.exception("engine error")
                    self.cancel_and_hide("error")

    def stop(self):
        self.running = False
        self.cancel_event.set()
        self.q.put(("noop",))

    def _next_timeout(self) -> float:
        deadlines = [d for d in (self.debounce_at, self.check_at, self.reposition_at, self.poll_at) if d]
        if not deadlines:
            return 1.0
        return max(0.0, min(deadlines) - time.monotonic())

    def _fire_timers(self):
        now = time.monotonic()
        if self.check_at and now >= self.check_at:
            self.check_at = None
            self._typeahead_check()
        if self.debounce_at and now >= self.debounce_at:
            self.debounce_at = None
            self._generate()
        if self.reposition_at and now >= self.reposition_at:
            self.reposition_at = None
            self._reposition()
        if self.poll_at and now >= self.poll_at:
            self.poll_at = None
            self._poll()

    def _handle(self, ev):
        kind = ev[0]
        if kind == "key":
            self._on_key(ev[1])
        elif kind == "accept":
            self._accept(ev[1])
        elif kind == "result":
            self._on_result(ev[1], ev[2])

    def _on_key(self, kind: str):
        st = self.state
        if kind == "mutation":
            if st in ("ready", "checking"):
                self._set_state("checking")
                self.check_attempts = 0
                self.check_at = time.monotonic() + self.cfg.typeahead_check_ms / 1000
            else:
                self._cancel_generation()
                self.schedule_prediction()
        elif kind in ("navigation", "dismissal"):
            if st != "idle":
                self.cancel_and_hide(kind)

    def eligible(self, ctx: FocusContext) -> bool:
        if ctx.is_password:
            return False
        app = ctx.app.lower()
        if app in self.blocked:
            return False
        return not any(i.lower() in app or i.lower() in ctx.window_title.lower()
                       for i in self.cfg.ignored_apps)

    def schedule_prediction(self):
        self._clear_timers()
        self._set_state("debouncing")
        self.debounce_at = time.monotonic() + self.cfg.debounce_ms / 1000

    def _generate(self):
        ctx = read_focus(self.own_pid)
        for _ in range(3):
            if ctx is not None:
                break
            # Chromium and Electron build their accessibility tree only after a
            # UIA client asks; the first query sees just the window pane.
            time.sleep(0.2)
            ctx = read_focus(self.own_pid)
        if ctx is None:
            log.debug("skip: no readable text field focused")
            self._set_state("idle")
            return
        if not self.eligible(ctx):
            log.debug("skip: field not eligible (app=%s password=%s)", ctx.app, ctx.is_password)
            self._set_state("idle")
            return
        stripped = ctx.preceding.strip()
        if not stripped or (len(stripped) < 12 and not re.search(r"\s", stripped)):
            log.debug("skip: too little text before the caret (%r)", ctx.preceding[-20:])
            self._set_state("idle")
            return
        if not END_OF_LINE.fullmatch(ctx.trailing.split("\n", 1)[0]):
            log.debug("skip: caret is mid-line (trailing=%r)", ctx.trailing[:30])
            self._set_state("idle")
            return
        screen = self.screen.get(ctx.pid, ctx.element_id, ctx.field) if self.cfg.screen_context else ""
        if screen:
            log.debug("screen context tail: %r", screen[-160:])
        self._cancel_generation()
        self.gen_id += 1
        self.gen_ctx = ctx
        self.cancel_event = threading.Event()
        self._set_state("generating")
        self.poll_at = time.monotonic() + self.cfg.poll_ms / 1000
        threading.Thread(target=self._worker, args=(self.gen_id, ctx, screen, self.cancel_event),
                         name=f"gen-{self.gen_id}", daemon=True).start()

    def _worker(self, gen_id: int, ctx: FocusContext, screen: str, cancel: threading.Event):
        messages = build_messages(
            PromptInput(ctx.preceding, ctx.trailing, ctx.app, ctx.window_title, screen),
            self.cfg.personalization, self.cfg.trailing_budget,
        )
        draft = ctx.preceding[-self.cfg.draft_budget:]
        t0 = time.perf_counter()
        try:
            raw = self.client.complete(
                messages, draft, max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature, stop=["\n"], cancel=cancel,
            )
        except Cancelled:
            log.debug("gen %d cancelled", gen_id)
            return
        except Exception as exc:
            log.warning("llama.cpp request failed: %s", exc)
            raw = None
        log.debug("gen %d: %.0f ms raw=%r", gen_id, (time.perf_counter() - t0) * 1000, raw)
        self.q.put(("result", gen_id, raw))

    def _on_result(self, gen_id: int, raw: str | None):
        if gen_id != self.gen_id or self.state != "generating":
            return
        ctx = self.gen_ctx
        text = normalize(raw or "", ctx.preceding, ctx.trailing)
        if not text:
            self.cancel_and_hide("empty completion")
            return
        now = read_focus(self.own_pid)
        if not self._same_field(now, ctx) or now.preceding != ctx.preceding:
            self.cancel_and_hide("text changed during generation")
            return
        self.ctx, self.current, self.remaining = now, text, text
        self._set_state("ready")
        self._show(text, now)
        self.poll_at = time.monotonic() + self.cfg.poll_ms / 1000
        log.info("suggest [%s] %r", now.app, text)

    def _typeahead_check(self):
        if self.state != "checking" or self.ctx is None:
            return
        now = read_focus(self.own_pid)
        base = self.ctx
        if self._same_field(now, base, grown=True):
            typed = now.preceding[len(base.preceding):]
            if not typed and self.check_attempts < 10:
                self.check_attempts += 1  # keystroke not published to UIA yet
                self.check_at = time.monotonic() + 0.03
                return
            rest = advance_if_typed(typed, self.remaining)
            if rest is not None:
                self.ctx = now
                if rest.strip():
                    self.remaining = rest
                    self._set_state("ready")
                    self._show(rest, now)
                    self.poll_at = time.monotonic() + self.cfg.poll_ms / 1000
                else:
                    self._finish()
                return
        self.cancel_and_hide("typed text diverged")
        self.schedule_prediction()

    def _accept(self, kind: str):
        # A character typed just before Tab may not be visible through UIA
        # yet. Wait for it (bounded by the retry limit in _typeahead_check)
        # instead of handing a literal Tab back to the field.
        while self.state == "checking":
            self.check_at = None
            self._typeahead_check()
            if self.state == "checking":
                time.sleep(0.03)
        if self.state != "ready" or self.ctx is None:
            win32.send_key(win32.VK_TAB)  # the hook swallowed it; pass it on
            return
        if not self._same_field(read_focus(self.own_pid), self.ctx):
            self.cancel_and_hide("focus moved before accept")
            win32.send_key(win32.VK_TAB)
            return
        if kind == "accept_full":
            chunk, rest = self.remaining, ""
        else:
            chunk, rest = next_word(self.remaining)
        win32.send_text(chunk)
        self.ctx.preceding += chunk
        log.info("accepted %r", chunk)
        if rest.strip():
            self.remaining = rest
            self.ui_post(lambda r=rest: self.overlay.update_text(r))
            self.reposition_at = time.monotonic() + 0.06
            # The insertion moved the caret; keep the poll from reading that
            # as the user clicking elsewhere before the reposition runs.
            self.poll_at = time.monotonic() + self.cfg.poll_ms / 1000
        else:
            self._finish()

    def _finish(self):
        current = self.current
        self.cancel_and_hide("suggestion consumed")
        if self.cfg.suggestion_chaining and not ends_sentence(current):
            self.schedule_prediction()

    def _reposition(self):
        if self.state != "ready" or self.ctx is None:
            return
        now = read_focus(self.own_pid)
        if not self._same_field(now, self.ctx, grown=True):
            self.cancel_and_hide("focus lost")
            return
        self.ctx = now
        self._show(self.remaining, now)

    def _poll(self):
        st = self.state
        if st == "generating" and self.gen_ctx is not None:
            if not self._same_field(read_focus(self.own_pid), self.gen_ctx, grown=True):
                self.cancel_and_hide("focus changed")
                return
        elif st == "ready" and self.ctx is not None:
            now = read_focus(self.own_pid)
            if (not self._same_field(now, self.ctx)
                    or abs(now.caret.x - self.ctx.caret.x) > 2 or abs(now.caret.y - self.ctx.caret.y) > 2):
                self.cancel_and_hide("context changed")
                return
        else:
            return
        self.poll_at = time.monotonic() + self.cfg.poll_ms / 1000

    @staticmethod
    def _same_field(now: FocusContext | None, ref: FocusContext, grown: bool = False) -> bool:
        """Whether `now` is the field `ref` was read from, with the same text
        before the caret (with grown=True: that text plus more).

        Runtime ids alone are not enough, because Chromium re-identifies the
        focused element while it builds its accessibility tree, so the same
        process with the same text also counts as the same field."""
        if now is None:
            return False
        if now.element_id != ref.element_id and now.pid != ref.pid:
            return False
        if grown:
            return now.preceding.startswith(ref.preceding)
        return now.preceding == ref.preceding

    def _show(self, text: str, ctx: FocusContext):
        caret, field, line_left = ctx.caret, ctx.field, ctx.line_left
        self.ui_post(lambda: self.overlay.show(text, caret, field, line_left))

    def _cancel_generation(self):
        self.cancel_event.set()
        self.gen_id += 1

    def _clear_timers(self):
        self.debounce_at = self.check_at = self.reposition_at = self.poll_at = None

    def cancel_and_hide(self, reason: str):
        log.debug("cancel: %s", reason)
        self._clear_timers()
        self._cancel_generation()
        self.ctx = None
        self.current = self.remaining = ""
        self._set_state("idle")
        self.ui_post(self.overlay.hide)
