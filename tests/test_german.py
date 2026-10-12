import json
import threading
import unittest

from ghosttab.context import scrub
from ghosttab.engine import classify_key
from ghosttab.llm import LlamaClient, split_for_healing
from ghosttab.prompt import normalize

VK_OEM_3 = 0xC0  # the "ö" key on a German layout


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


class FakeSession:
    def __init__(self, chunks):
        self.chunks = chunks
        self.body = None

    def post(self, url, json=None, **kw):
        self.body = json
        return FakeResponse(self.chunks)


def sse(content, stop=False):
    return b"data: " + json.dumps({"content": content, "stop": stop}, ensure_ascii=False).encode("utf-8")


class GermanTests(unittest.TestCase):
    def test_altgr_types_characters(self):
        # AltGr+Q (@) arrives as Ctrl+Alt+Q
        self.assertEqual(classify_key(ord("Q"), False, True, True, False), "mutation")
        self.assertEqual(classify_key(VK_OEM_3, False, False, False, False), "mutation")
        self.assertEqual(classify_key(ord("C"), False, True, False, False), "dismissal")

    def test_stream_is_decoded_as_utf8(self):
        client = LlamaClient("http://x", 5)
        client.render_prefix = lambda messages: "PROMPT"
        client.session = FakeSession([sse(" mit dir "), sse("über Ostern"), sse("", stop=True)])
        out = client.complete([], "Ich würde gerne,", max_tokens=8, temperature=0.1,
                              stop=["\n"], cancel=threading.Event())
        self.assertEqual(out, " mit dir über Ostern")

    def test_healing_with_umlaut_prefix(self):
        self.assertEqual(split_for_healing("Schöne Grüße aus Mü"), ("Schöne Grüße aus", " Mü"))
        client = LlamaClient("http://x", 5)
        client.render_prefix = lambda messages: "PROMPT"
        client.session = FakeSession([sse(" Mü"), sse("nchen"), sse("", stop=True)])
        out = client.complete([], "Schöne Grüße aus Mü", max_tokens=8, temperature=0.1,
                              stop=["\n"], cancel=threading.Event())
        self.assertEqual(out, "nchen")
        self.assertEqual(client.session.body["prompt"], "PROMPTSchöne Grüße aus")
        self.assertIn('" Mü"', client.session.body["grammar"])

    def test_german_quotes_kept_when_user_uses_them(self):
        pre = "Er sagte: „Das ist "
        self.assertEqual(normalize("super“ – wirklich.", pre), "super“ - wirklich.")
        self.assertEqual(normalize("super – wirklich.", "Gut – "), "super – wirklich.")
        self.assertEqual(normalize("super“ – wirklich.", "Er sagte: Das ist "), 'super" - wirklich.')

    def test_umlauts_survive_normalize(self):
        self.assertEqual(normalize(" Gemüse, Äpfel und Brötchen", "Milch, Käse,"), " Gemüse, Äpfel und Brötchen")

    def test_scrub_german_discord(self):
        self.assertEqual(scrub("Alex  Heute um 10:42 Uhr\n3 Reaktionen, bin dabei"), "Alex\nbin dabei")
        self.assertEqual(scrub("Sam 12.10.2026 21:15 klingt gut"), "Sam klingt gut")


if __name__ == "__main__":
    unittest.main()
