"""Screen context: the visible text above the focused field.

1. Scope to the nearest Document ancestor of the field (web pages, Electron
   apps such as Discord or Slack), or else the top-level window.
2. Fetch every Text element with its name and bounding rectangle cached, in
   a single cross-process FindAllBuildCache call.
3. Keep text above the field that overlaps its column; in a chat app that is
   the message history, not the channel or member lists.
4. Scrub UI noise and keep the part nearest the field.

Must run on a thread that has initialised COM for UIA.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from uiautomation import uiautomation as _uia

from .focus import Rect

log = logging.getLogger(__name__)

UIA_NamePropertyId = 30005
UIA_BoundingRectanglePropertyId = 30001
UIA_ControlTypePropertyId = 30003
UIA_TextControlTypeId = 50020
UIA_DocumentControlTypeId = 50030
UIA_WindowControlTypeId = 50032
TreeScope_Descendants = 4
MAX_ELEMENTS = 4000
MAX_ANCESTOR_STEPS = 40


@dataclass
class Snippet:
    text: str
    rect: Rect


def _client():
    return _uia._AutomationClient.instance().IUIAutomation


def _scope_root(client, element):
    """Nearest Document ancestor (web content), else the top-level window."""
    walker = client.ControlViewWalker
    root = client.GetRootElement()
    node, last = element, element
    for _ in range(MAX_ANCESTOR_STEPS):
        if node is None or client.CompareElements(node, root):
            break
        if node.CurrentControlType == UIA_DocumentControlTypeId and not client.CompareElements(node, element):
            return node
        last = node
        node = walker.GetParentElement(node)
    return last  # child of the desktop root = top-level window


def collect_snippets() -> list[Snippet]:
    client = _client()
    focused = client.GetFocusedElement()
    if focused is None:
        return []
    root = _scope_root(client, focused)
    cache = client.CreateCacheRequest()
    cache.AddProperty(UIA_NamePropertyId)
    cache.AddProperty(UIA_BoundingRectanglePropertyId)
    cond = client.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_TextControlTypeId)
    found = root.FindAllBuildCache(TreeScope_Descendants, cond, cache)
    out: list[Snippet] = []
    if found is None:
        return out
    for i in range(min(found.Length, MAX_ELEMENTS)):
        el = found.GetElement(i)
        name = el.CachedName
        if not name or not name.strip():
            continue
        r = el.CachedBoundingRectangle
        out.append(Snippet(name, Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)))
    return out


_NOISE = [
    re.compile(r"\b(?:Today|Yesterday) at \d{1,2}:\d{2}(?:\s?[AP]M)?\b", re.I),
    re.compile(r"\b(?:Heute|Gestern) um \d{1,2}:\d{2}(?: Uhr)?", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}(?:\s?[AP]M)?\b", re.I),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4},? \d{1,2}:\d{2}(?: Uhr)?", re.I),
    re.compile(r"(?<![\w:])\d{1,2}:\d{2}(?:\s?[AP]M| Uhr)?\b\.?", re.I),
    re.compile(r"\b\d+\s(?:reactions?|links?|attachments?|replies|files?)\b,?\s?", re.I),
    re.compile(r"\b\d+\s(?:Reaktion(?:en)?|Antworten|Anh\u00e4nge?|Dateien)\b,?\s?", re.I),
    re.compile(r"Shift \+ (?:Return|Enter) to add a new line\.?", re.I),
]


def scrub(text: str) -> str:
    for pattern in _NOISE:
        text = pattern.sub("", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def assemble(snippets: list[Snippet], field: Rect, budget: int) -> str:
    """Text above the field and overlapping its column, nearest part last."""
    keep = []
    for s in snippets:
        r = s.rect
        if r.w <= 0 or r.h <= 0:
            continue
        if r.y + r.h > field.y + 2:          # not above the field (or inside it)
            continue
        if r.right <= field.x or r.x >= field.right:  # outside its column
            continue
        keep.append(s)
    keep.sort(key=lambda s: (s.rect.y, s.rect.x))
    lines: list[str] = []
    last_y = None
    for s in keep:
        text = " ".join(s.text.split())
        if lines and last_y is not None and abs(s.rect.y - last_y) < max(4, s.rect.h // 2):
            if lines[-1].endswith(text):
                continue  # duplicate run on the same line
            lines[-1] += " " + text
        elif not lines or lines[-1] != text:
            lines.append(text)
        last_y = s.rect.y
    text = scrub("\n".join(lines))
    if len(text) > budget:
        text = text[-budget:]
        cut = text.find("\n")
        if 0 <= cut < 200:
            text = text[cut + 1:]  # start on a whole line
    return text


class ScreenContext:
    """Per-field cache. Reusing the identical string keeps the prompt prefix,
    and with it llama-server's KV cache, stable across keystrokes."""

    def __init__(self, budget: int, ttl_s: float):
        self.budget = budget
        self.ttl_s = ttl_s
        self._key = None
        self._text = ""
        self._at = 0.0

    def get(self, pid: int, element_id: tuple, field: Rect) -> str:
        key = (pid, element_id)
        now = time.monotonic()
        # An empty result is not cached: a freshly opened Chromium window
        # reports little or nothing until its accessibility tree is built.
        if key == self._key and now - self._at < self.ttl_s and self._text:
            return self._text
        t0 = time.perf_counter()
        try:
            snippets = collect_snippets()
            text = assemble(snippets, field, self.budget)
        except Exception as exc:  # COM errors while the UI changes
            log.debug("screen context failed: %r", exc)
            snippets, text = [], ""
        log.debug("screen context: %d text elements -> %d chars in %.0f ms",
                  len(snippets), len(text), (time.perf_counter() - t0) * 1000)
        self._key, self._text, self._at = key, text, now
        return text

    def invalidate(self):
        self._key = None
