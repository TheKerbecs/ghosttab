"""Prompt construction and clean-up of model output."""

from __future__ import annotations

import re
from dataclasses import dataclass

SYSTEM_PROMPT = (
    "You are a text-prediction engine built into a keyboard. The user is in "
    "the middle of typing. Continue their text from exactly where it stops "
    "and finish the current sentence or clause. Write as the user, in the "
    "first person; you are completing their words, not replying to them. "
    "Output only the continuation: no quotes, no commentary, no greetings, "
    "no refusals. Do not begin a new sentence after the current one ends. "
    "Always write in the language the user is writing in, with its correct "
    "spelling and special characters (for German: \u00e4, \u00f6, \u00fc, \u00df). "
    "If the text already looks complete, output nothing."
)

APP_KINDS = [
    ("chat", re.compile(r"slack|discord|telegram|whatsapp|signal|messenger|teams|element|fluxer", re.I)),
    ("email", re.compile(r"mail|outlook|thunderbird|protonmail|superhuman", re.I)),
    ("notes", re.compile(r"notes|notion|obsidian|onenote|evernote|joplin", re.I)),
]


@dataclass
class PromptInput:
    preceding: str
    trailing: str
    app: str
    window_title: str
    screen: str = ""  # visible text above the field (see context.py)


def classify_app(app: str, title: str) -> str:
    haystack = f"{app} {title}"
    for kind, pattern in APP_KINDS:
        if pattern.search(haystack):
            return kind
    return "generic"


def build_messages(inp: PromptInput, personalization: list[str], trailing_budget: int) -> list[dict]:
    """System + user turns. The draft itself is appended after templating."""
    kind = classify_app(inp.app, inp.window_title)
    parts = []
    title = f" ({inp.window_title[:100]})" if inp.window_title else ""
    parts.append(f"App: {inp.app or 'an app'}{title}")
    if kind != "generic":
        parts.append(f"Kind of text: {kind} message")
    if inp.screen:
        heading = "Conversation so far" if kind == "chat" else "Text on screen above the field"
        parts.append(
            f"{heading} (most recent last; use it to stay on topic, do not copy it):\n{inp.screen}"
        )
    trailing = inp.trailing.strip()
    if trailing:
        parts.append(
            "Text that already follows the cursor (do not repeat it):\n"
            + trailing[:trailing_budget]
        )
    if personalization:
        parts.append(
            "Additional instructions or context from the user:\n"
            + "\n".join(f"- {p}" for p in personalization)
        )
    parts.append("Predict the next few words the user is about to type.")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


MIN_LENGTH = 2
# <|im_end|>, Gemma 4's <|turn> / <turn|> / <|think|>, Gemma 3's <end_of_turn>, </s>
_SPECIAL_TOKEN = re.compile(r"<\|[^<>\s]*>|<[^<>\s]*\|>|<(?:end_of_turn|start_of_turn|eos|/s)>")
_PLAIN = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2013": "-", "\u2014": "--", "\u2026": "...", "\u00a0": " ",
}
# Typographic characters are kept when the user's own text already uses the
# same family (German quotes from autocorrect, for example).
_FAMILIES = ["\u2018\u2019\u201a", "\u201c\u201d\u201e", "\u2013\u2014", "\u2026"]


def _plain_punctuation(text: str, preceding: str) -> str:
    keep = set()
    for family in _FAMILIES:
        if any(c in preceding for c in family):
            keep.update(family)
    return "".join(c if c in keep else _PLAIN.get(c, c) for c in text)


def _remove_context_echo(text: str, preceding: str) -> str:
    """Drop a leading echo of the end of what the user already typed."""
    if not preceding:
        return text
    low, prev = text.lower(), preceding.lower()
    for n in range(min(150, len(preceding), len(text)), 2, -1):
        if low.startswith(prev[-n:]):
            return text[n:]
    return text


def _trim_trailing_overlap(text: str, trailing: str) -> str:
    """Drop the end of the suggestion if it duplicates text after the caret."""
    head = trailing[:80].lower()
    if not head.strip():
        return text
    low = text.lower()
    for n in range(min(len(text), 80), 2, -1):
        if head.startswith(low[-n:]):
            return text[:-n]
    return text


def _truncate_internal_repetition(text: str) -> str:
    """'and then and then and then' -> 'and then'."""
    words = text.split()
    for n in range(1, min(8, len(words) // 3) + 1):
        unit = words[:n]
        repeats, pos = 1, n
        while words[pos:pos + n] == unit:
            repeats += 1
            pos += n
        if repeats >= 3:
            lead = text[: len(text) - len(text.lstrip())]
            return lead + " ".join(unit)
    return text


def _is_repetition(text: str, preceding: str) -> bool:
    if len(preceding) < 20:
        return False
    return text.lower().strip() in preceding[-100:].lower()


def normalize(raw: str, preceding: str, trailing: str = "") -> str | None:
    if not raw or not raw.strip():
        return None
    text = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.S)
    text = text.lstrip("\n")
    text = _remove_context_echo(text, preceding)
    if preceding.endswith((" ", "\t")) and text.startswith(" "):
        text = text[1:]
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = _SPECIAL_TOKEN.split(text, maxsplit=1)[0]
    text = text.split("\n", 1)[0]  # typing path is single-line (stop: ["\n"])
    text = _plain_punctuation(text, preceding)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    text = _truncate_internal_repetition(text)
    if trailing:
        text = _trim_trailing_overlap(text, trailing)
    text = text.rstrip()
    if len(text.strip()) < MIN_LENGTH:
        return None
    if re.fullmatch(r"[\s\W_]+", text):
        return None
    if _is_repetition(text, preceding):
        return None
    return text


def next_word(text: str) -> tuple[str, str]:
    """Split off the next word, keeping its leading whitespace."""
    m = re.match(r"\s*\S+", text)
    if not m:
        return text, ""
    return m.group(0), text[m.end():]


def advance_if_typed(typed: str, remaining: str) -> str | None:
    """If the user typed the start of the suggestion, return what is left."""
    if not typed:
        return None
    if remaining.startswith(typed):
        return remaining[len(typed):]
    stripped = remaining.lstrip()
    if stripped.startswith(typed):
        return stripped[len(typed):]
    return None


def ends_sentence(text: str) -> bool:
    return re.search(r"[.!?\u2026][\"')\]]*\s*$", text) is not None
