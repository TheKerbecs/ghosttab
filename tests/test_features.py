import io
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from ghosttab import win32
from ghosttab.audio import FileRecorder, to_wav
from ghosttab.hotkeys import Hotkey, hotkey_label
from ghosttab.llm import LlamaClient
from ghosttab.prompt import build_assistant_messages, build_dictation_messages


class HotkeyTests(unittest.TestCase):
    def test_parse_and_match(self):
        hk = Hotkey.parse("Ctrl+Alt+Space")
        self.assertTrue(hk.matches(0x20, False, True, True, False))
        self.assertFalse(hk.matches(0x20, True, True, True, False))   # extra Shift
        self.assertFalse(hk.matches(0x20, False, True, False, False))  # missing Alt
        self.assertEqual(Hotkey.parse("win+shift+f9"), Hotkey(0x78, shift=True, win=True))
        self.assertEqual(Hotkey.parse("ctrl+alt+a").vk, ord("A"))

    def test_rejects_bad_hotkeys(self):
        for bad in ("ctrl+alt", "ctrl+a+b", "ctrl+ä"):
            with self.assertRaises(ValueError):
                Hotkey.parse(bad)

    def test_label(self):
        self.assertEqual(hotkey_label("ctrl+alt+space"), "Ctrl+Alt+Space")


class AudioTests(unittest.TestCase):
    def test_to_wav_resamples_to_16k_mono(self):
        tone = np.sin(np.linspace(0, 2 * np.pi * 440, 44100)).astype("float32")
        with wave.open(io.BytesIO(to_wav(tone, 44100))) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertAlmostEqual(w.getnframes(), 16000, delta=2)

    def test_file_recorder_stops_after_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.wav"
            path.write_bytes(to_wav(np.zeros(1600, dtype="float32"), 16000))  # 0.1 s
            rec = FileRecorder(str(path))
            rec.start()
            self.assertTrue(rec.heard_speech)
            self.assertEqual(rec.silence_for(), 0.0)
            self.assertEqual(rec.stop(), path.read_bytes())


class SendTextTests(unittest.TestCase):
    def test_newlines_become_shift_enter_and_text_is_unicode(self):
        sent = []
        with mock.patch.object(win32, "_send", lambda events: sent.extend(events) or True):
            win32.send_text("aä\nb")
        kinds = [(e.u.ki.wVk, e.u.ki.wScan, e.u.ki.dwFlags) for e in sent]
        up = win32.KEYEVENTF_KEYUP
        uni = win32.KEYEVENTF_UNICODE
        self.assertEqual(kinds, [
            (0, ord("a"), uni), (0, ord("a"), uni | up),
            (0, 0xE4, uni), (0, 0xE4, uni | up),
            (win32.VK_SHIFT, 0, 0), (win32.VK_RETURN, 0, 0), (win32.VK_RETURN, 0, up), (win32.VK_SHIFT, 0, up),
            (0, ord("b"), uni), (0, ord("b"), uni | up),
        ])
        self.assertTrue(all(e.u.ki.dwExtraInfo == win32.INJECT_MARK for e in sent))


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self.lines)


class ChatStreamTests(unittest.TestCase):
    def test_parses_deltas_until_done(self):
        def chunk(text):
            return b"data: " + json.dumps({"choices": [{"delta": {"content": text}}]},
                                          ensure_ascii=False).encode("utf-8")
        lines = [chunk("Grü"), b"", chunk("ße"), b"data: [DONE]", chunk("ignored")]
        client = LlamaClient("http://x", 5)
        client.task_session = mock.Mock(post=lambda *a, **k: FakeResponse(lines))
        pieces = []
        out = client.chat_stream([{"role": "user", "content": "x"}], pieces.append, cancel=threading.Event())
        self.assertEqual(out, "Grüße")
        self.assertEqual(pieces, ["Grü", "ße"])


class PromptTests(unittest.TestCase):
    def test_assistant_messages(self):
        msgs = build_assistant_messages("hello wrold", "Fix spelling.", "notepad.exe")
        self.assertIn("notepad.exe", msgs[0]["content"])
        self.assertIn("hello wrold", msgs[1]["content"])
        self.assertIn("Fix spelling.", msgs[1]["content"])
        free = build_assistant_messages("  ", "Write a haiku", "")
        self.assertEqual(free[1]["content"], "Write a haiku")

    def test_dictation_messages_include_vocabulary(self):
        msgs = build_dictation_messages("raw text", ["ghosttab", "Kletterhalle"])
        self.assertIn("ghosttab, Kletterhalle", msgs[0]["content"])
        self.assertEqual(msgs[-1], {"role": "user", "content": "raw text"})
        self.assertEqual(len(msgs) % 2, 0)  # system + example pairs + final user turn


if __name__ == "__main__":
    unittest.main()
