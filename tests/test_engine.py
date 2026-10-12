"""Headless tests of the engine state machine: a fake text field replaces UI
Automation, a fake client replaces llama-server, a fake overlay replaces Tk."""

import threading
import time
import unittest
from unittest import mock

from ghosttab import engine as engine_mod
from ghosttab import win32
from ghosttab.config import Config
from ghosttab.focus import FocusContext, Rect


class FakeField:
    def __init__(self, text="", app="Discord.exe", password=False, element_id=(1,)):
        self.text, self.app, self.password, self.element_id = text, app, password, element_id
        self.lock = threading.Lock()

    def ctx(self):
        with self.lock:
            return FocusContext(
                element_id=self.element_id, pid=1234, app=self.app, window_title="#general",
                preceding=self.text, trailing="", caret=Rect(100 + len(self.text), 50, 1, 20),
                caret_exact=True, field=Rect(90, 45, 600, 30), is_password=self.password,
            )

    def type(self, s):
        with self.lock:
            self.text += s


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, messages, draft, **kw):
        self.calls.append(draft)
        self.messages = messages
        return self.reply


class FakeOverlay:
    def __init__(self):
        self.shown = None
        self.visible = False

    def show(self, text, caret, field, line_left):
        self.shown, self.visible = text, True

    def update_text(self, text):
        self.shown = text

    def hide(self):
        self.visible = False


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.field = FakeField()
        self.sent = []
        self.keys = []
        patches = [
            mock.patch.object(engine_mod, "read_focus", lambda pid: self.field.ctx()),
            mock.patch.object(win32, "send_text", self._send_text),
            mock.patch.object(win32, "send_key", lambda vk: self.keys.append(vk)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _send_text(self, s):
        self.sent.append(s)
        self.field.type(s)
        return True

    def start(self, reply, **cfg):
        cfg.setdefault("screen_context", False)
        config = Config(debounce_ms=10, typeahead_check_ms=5, poll_ms=1000, **cfg)
        self.client = FakeClient(reply)
        self.overlay = FakeOverlay()
        self.eng = engine_mod.Engine(config, self.client, lambda fn: fn(), self.overlay)
        t = threading.Thread(target=self.eng.run, daemon=True)
        t.start()
        self.addCleanup(self.eng.stop)

    def press(self, vk=ord("A"), shift=False):
        return self.eng.on_key(vk, shift, False, False, False)

    def type_and_key(self, s):
        self.field.type(s)
        self.press()

    def test_suggest_then_tab_accepts_and_chains(self):
        self.start(" tonight at the usual place")
        self.type_and_key("hey, are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.assertEqual(self.overlay.shown, " tonight at the usual place")
        self.assertTrue(self.press(win32.VK_TAB))  # swallowed
        self.assertTrue(wait_for(lambda: self.sent == [" tonight at the usual place"]))
        self.assertEqual(self.field.text, "hey, are we still on for tonight at the usual place")
        # no sentence end -> a chained prediction runs
        self.assertTrue(wait_for(lambda: len(self.client.calls) == 2))

    def test_no_chain_after_sentence_end(self):
        self.start(" tonight?")
        self.type_and_key("hey, are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.press(win32.VK_TAB)
        self.assertTrue(wait_for(lambda: self.eng.state == "idle" and self.sent))
        time.sleep(0.1)
        self.assertEqual(len(self.client.calls), 1)

    def test_shift_tab_accepts_one_word(self):
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.assertTrue(self.press(win32.VK_TAB, shift=True))
        self.assertTrue(wait_for(lambda: self.sent == [" tonight"]))
        self.assertTrue(wait_for(lambda: self.overlay.shown == " at eight"))
        self.assertEqual(self.eng.state, "ready")

    def test_typing_the_suggestion_keeps_it(self):
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.type_and_key(" to")
        self.assertTrue(wait_for(lambda: self.overlay.shown == "night at eight" and self.eng.state == "ready"))
        self.assertEqual(len(self.client.calls), 1)

    def test_diverging_text_repredicts(self):
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.type_and_key(" lunch")
        self.assertTrue(wait_for(lambda: len(self.client.calls) == 2))
        self.assertEqual(self.client.calls[-1], "are we still on for lunch")

    def test_tab_right_after_typing_waits_for_publish(self):
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.press()  # key event arrives before the field shows the char...
        self.assertTrue(self.press(win32.VK_TAB))
        time.sleep(0.05)
        self.field.type(" t")  # ...which the app publishes a little later
        self.assertTrue(wait_for(lambda: self.sent == ["onight at eight"]))
        self.assertEqual(self.keys, [])

    def test_reidentified_element_with_same_text_is_same_field(self):
        # Chromium changes the element's runtime id while building its tree.
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.field.element_id = (99,)
        self.assertTrue(self.press(win32.VK_TAB))
        self.assertTrue(wait_for(lambda: self.sent == [" tonight at eight"]))

    def test_screen_context_reaches_prompt(self):
        self.start(" climbing", screen_context=True)
        self.eng.screen.get = lambda pid, eid, field: "Alex: bouldering on Thursday?"
        self.type_and_key("yes I'm in for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        user = self.client.messages[1]["content"]
        self.assertIn("Conversation so far", user)
        self.assertIn("Alex: bouldering on Thursday?", user)

    def test_tab_passes_through_without_suggestion(self):
        self.start(" x y z")
        self.assertFalse(self.press(win32.VK_TAB))

    def test_escape_dismisses(self):
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.assertFalse(self.press(win32.VK_ESCAPE))
        self.assertTrue(wait_for(lambda: self.eng.state == "idle" and not self.overlay.visible))
        self.assertFalse(self.press(win32.VK_TAB))

    def test_password_field_never_completes(self):
        self.field.password = True
        self.start(" hunter2")
        self.type_and_key("my password is")
        time.sleep(0.15)
        self.assertEqual(self.client.calls, [])

    def test_blocked_app_never_completes(self):
        self.field.app = "WindowsTerminal.exe"
        self.start(" status")
        self.type_and_key("git commit -m fix")
        time.sleep(0.15)
        self.assertEqual(self.client.calls, [])

    def test_focus_change_before_accept_returns_tab(self):
        self.start(" tonight at eight")
        self.type_and_key("are we still on for")
        self.assertTrue(wait_for(lambda: self.eng.state == "ready"))
        self.field.element_id = (2,)  # user clicked into another (empty) field
        self.field.text = ""
        self.assertTrue(self.press(win32.VK_TAB))
        self.assertTrue(wait_for(lambda: self.keys == [win32.VK_TAB]))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
