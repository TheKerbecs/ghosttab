import unittest

from ghosttab.engine import classify_key
from ghosttab.prompt import (PromptInput, advance_if_typed, build_messages, ends_sentence,
                             next_word, normalize)
from ghosttab import win32
from ghosttab.llm import _gbnf_literal, split_for_healing
from ghosttab.context import Snippet, assemble, scrub
from ghosttab.focus import Rect


class NormalizeTests(unittest.TestCase):
    def test_strips_echo_of_preceding_text(self):
        self.assertEqual(normalize("see you tomorrow at noon", "I will see you"), " tomorrow at noon")

    def test_drops_leading_space_after_space(self):
        self.assertEqual(normalize(" the meeting", "Let's move "), "the meeting")

    def test_cuts_at_newline_and_special_tokens(self):
        self.assertEqual(normalize("great idea<end_of_turn>\nmore", "That is a "), "great idea")
        self.assertEqual(normalize("great idea<turn|>", "That is a "), "great idea")
        self.assertEqual(normalize("first line\nsecond", "x y "), "first line")

    def test_smart_punctuation(self):
        self.assertEqual(normalize("it’s fine…", "I think "), "it's fine...")

    def test_internal_repetition(self):
        self.assertEqual(normalize("and then and then and then", "we ate "), "and then")

    def test_trailing_overlap_removed(self):
        self.assertEqual(normalize("the report by Friday.", "I'll send ", "Friday."), "the report by")

    def test_rejects_junk(self):
        self.assertIsNone(normalize("", "abc"))
        self.assertIsNone(normalize(" .", "abc def "))
        self.assertIsNone(normalize("x", "abc def "))

    def test_rejects_repetition_of_recent_text(self):
        pre = "Thanks for the update on the project status. Thanks"
        self.assertIsNone(normalize(" for the update", pre))

    def test_strips_think_block(self):
        self.assertEqual(normalize("<think>hmm</think>later today", "I can do it "), "later today")


class InteractionTests(unittest.TestCase):
    def test_next_word_keeps_leading_space(self):
        self.assertEqual(next_word(" quick brown fox"), (" quick", " brown fox"))
        self.assertEqual(next_word("fox"), ("fox", ""))

    def test_advance_if_typed(self):
        self.assertEqual(advance_if_typed("q", " quick fox"), "uick fox")
        self.assertEqual(advance_if_typed(" q", " quick fox"), "uick fox")
        self.assertIsNone(advance_if_typed("z", " quick fox"))

    def test_ends_sentence(self):
        self.assertTrue(ends_sentence("done."))
        self.assertTrue(ends_sentence('he said "ok!"'))
        self.assertFalse(ends_sentence("and then"))

    def test_classify_keys(self):
        self.assertEqual(classify_key(win32.VK_TAB, False, False, False, False), "accept_full")
        self.assertEqual(classify_key(win32.VK_TAB, True, False, False, False), "accept_word")
        self.assertEqual(classify_key(win32.VK_TAB, False, False, True, False), "dismissal")
        self.assertEqual(classify_key(ord("A"), True, False, False, False), "mutation")
        self.assertEqual(classify_key(ord("C"), False, True, False, False), "dismissal")
        self.assertEqual(classify_key(win32.VK_BACK, False, False, False, False), "mutation")
        self.assertEqual(classify_key(win32.VK_LEFT, False, False, False, False), "navigation")
        self.assertEqual(classify_key(win32.VK_SHIFT, True, False, False, False), "other")


class HealingTests(unittest.TestCase):
    def test_split_partial_word(self):
        self.assertEqual(split_for_healing("the delivery sc"), ("the delivery", " sc"))
        self.assertEqual(split_for_healing("the delivery "), ("the delivery", " "))
        self.assertEqual(split_for_healing("milk, eggs,"), ("milk, eggs,", ""))
        self.assertEqual(split_for_healing("Grüße an Jürg"), ("Grüße an", " Jürg"))

    def test_gbnf_literal_escapes(self):
        self.assertEqual(_gbnf_literal('a"b\\c'), '"a\\"b\\\\c"')


class ContextTests(unittest.TestCase):
    FIELD = Rect(300, 800, 900, 40)  # chat input box

    def test_keeps_text_above_field_in_its_column_in_order(self):
        snips = [
            Snippet("Alex", Rect(320, 600, 40, 20)),
            Snippet("Today at 10:42 AM", Rect(370, 600, 120, 20)),
            Snippet("are you coming bouldering on Thursday?", Rect(320, 622, 400, 20)),
            Snippet("Sam", Rect(320, 700, 40, 20)),
            Snippet("I'm in, 7pm works", Rect(320, 722, 300, 20)),
            Snippet("general", Rect(20, 300, 80, 20)),              # channel list: left of column
            Snippet("Online — 3", Rect(1250, 300, 80, 20)),    # member list: right of column
            Snippet("draft in the box", Rect(310, 805, 200, 20)),     # inside the field
            Snippet("Shift + Enter to add a new line", Rect(310, 850, 300, 20)),  # below
        ]
        text = assemble(snips, self.FIELD, 2400)
        self.assertEqual(text, "Alex\nare you coming bouldering on Thursday?\nSam\nI'm in, 7pm works")

    def test_budget_keeps_nearest_text_on_whole_lines(self):
        snips = [Snippet(f"message number {i}", Rect(320, 100 + 22 * i, 300, 20)) for i in range(30)]
        text = assemble(snips, self.FIELD, 120)
        self.assertTrue(text.endswith("message number 29"))
        self.assertTrue(text.startswith("message number"))
        self.assertLessEqual(len(text), 120)

    def test_scrub(self):
        self.assertEqual(scrub("Alex  Yesterday at 9:15 PM\n\n3 reactions, cool"), "Alex\ncool")


class PromptTests(unittest.TestCase):
    def test_messages_shape(self):
        msgs = build_messages(PromptInput("Hey, are we still", "", "Discord.exe", "#general"), ["I sign off as R."], 200)
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn("App: Discord.exe (#general)", msgs[1]["content"])
        self.assertIn("chat message", msgs[1]["content"])
        self.assertIn("I sign off as R.", msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
