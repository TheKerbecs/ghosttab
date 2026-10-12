"""llama-server client.

The [system, user] messages are rendered once with the model's chat template
(/apply-template) and the user's draft is appended to the raw prompt, so the
model continues the draft as its own reply. Requests go to /completion with
cache_prompt, so consecutive keystrokes only evaluate the new tokens, and are
streamed so that closing the connection cancels generation on the server.

Token healing: a draft ending mid-word ("...the delivery sc") tokenises
unnaturally and the model then produces nothing or a broken UTF-8 fragment.
The partial word is cut from the prompt and a GBNF grammar forces the output
to start with it, letting the model pick a natural token such as " schedule".
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections import OrderedDict

import requests

log = logging.getLogger(__name__)

_HEAL = re.compile(r"[ \t]*\w{0,24}$")


def split_for_healing(draft: str) -> tuple[str, str]:
    """('...the delivery', ' sc') for '...the delivery sc'."""
    m = _HEAL.search(draft)
    tail = m.group(0) if m else ""
    return draft[: len(draft) - len(tail)], tail


def _gbnf_literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") + '"'


class Cancelled(Exception):
    pass


# Autocomplete keeps its own server slot so that dictation and assistant
# requests do not evict its cached prompt (start the server with -np 2).
COMPLETION_SLOT = 0
TASK_SLOT = 1

TRANSCRIBE_PROMPT = (
    "Transcribe the following speech segment in its original language. Follow these specific "
    "instructions for formatting the answer:\n"
    "* Only output the transcription, with no newlines.\n"
    "* When transcribing numbers, write the digits, i.e. write 1.7 and not one point seven, "
    "and write 3 instead of three."
)


class LlamaClient:
    def __init__(self, base_url: str, timeout_s: float):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.task_session = requests.Session()
        self._templates: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def chat(self, messages: list[dict], *, max_tokens: int = 512, temperature: float = 0.0,
             timeout_s: float = 120.0) -> str:
        body = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "id_slot": TASK_SLOT,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        r = self.task_session.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=timeout_s)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""

    def chat_stream(self, messages: list[dict], on_delta, *, max_tokens: int = 1024,
                    temperature: float = 0.3, cancel: threading.Event | None = None) -> str:
        """Stream a chat reply, calling on_delta(text) for every piece."""
        body = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "id_slot": TASK_SLOT,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        out: list[str] = []
        with self.task_session.post(f"{self.base_url}/v1/chat/completions", json=body,
                                    stream=True, timeout=self.timeout_s) as r:
            r.raise_for_status()
            for raw_line in r.iter_lines():
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                if not raw_line.startswith(b"data: "):
                    continue
                data = raw_line[6:]
                if data.strip() == b"[DONE]":
                    break
                choices = json.loads(data.decode("utf-8")).get("choices") or []
                delta = (choices[0].get("delta") or {}).get("content") if choices else None
                if delta:
                    out.append(delta)
                    on_delta(delta)
        return "".join(out)

    def transcribe(self, wav: bytes, timeout_s: float = 120.0) -> str:
        """Speech to text with the model's own audio encoder (needs --mmproj)."""
        content = [
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}},
            {"type": "text", "text": TRANSCRIBE_PROMPT},
        ]
        return self.chat([{"role": "user", "content": content}], max_tokens=1024, timeout_s=timeout_s).strip()

    def props(self) -> dict:
        try:
            r = self.session.get(f"{self.base_url}/props", timeout=5)
            return r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            return {}

    def health(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=2)
            return r.ok
        except requests.RequestException:
            return False

    def warmup(self, timeout_s: float = 300.0) -> float:
        """One throwaway completion with and without a grammar. The first
        requests after llama-server starts can take several seconds (Vulkan
        compiles its shader pipelines lazily); pay that before the user
        types. Returns seconds taken."""
        import time

        t0 = time.perf_counter()
        for grammar in (None, 'root ::= " w" [^\\n]*'):
            body = {"prompt": "Hello w", "n_predict": 4, "cache_prompt": False}
            if grammar:
                body["grammar"] = grammar
            self.session.post(f"{self.base_url}/completion", json=body, timeout=timeout_s).raise_for_status()
        return time.perf_counter() - t0

    def render_prefix(self, messages: list[dict]) -> str:
        key = json.dumps(messages, sort_keys=True)
        with self._lock:
            if key in self._templates:
                self._templates.move_to_end(key)
                return self._templates[key]
        r = self.session.post(
            f"{self.base_url}/apply-template",
            json={"messages": messages, "chat_template_kwargs": {"enable_thinking": False}},
            timeout=self.timeout_s,
        )
        r.raise_for_status()
        prompt = r.json()["prompt"]
        with self._lock:
            self._templates[key] = prompt
            while len(self._templates) > 32:
                self._templates.popitem(last=False)
        return prompt

    def complete(
        self,
        messages: list[dict],
        draft: str,
        *,
        max_tokens: int,
        temperature: float,
        stop: list[str],
        cancel: threading.Event,
    ) -> str:
        head, heal = split_for_healing(draft)
        prompt = self.render_prefix(messages) + head
        if cancel.is_set():
            raise Cancelled
        body = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "top_k": 40,
            "top_p": 0.9,
            "stop": stop,
            "cache_prompt": True,
            "id_slot": COMPLETION_SLOT,
            "stream": True,
        }
        if heal:
            body["grammar"] = f"root ::= {_gbnf_literal(heal)} [^\\n]*"
        out: list[str] = []
        with self.session.post(
            f"{self.base_url}/completion", json=body, stream=True, timeout=self.timeout_s
        ) as r:
            r.raise_for_status()
            # Decode bytes as UTF-8 ourselves: the event stream carries no
            # charset, and requests would fall back to ISO-8859-1.
            for raw_line in r.iter_lines():
                if cancel.is_set():
                    # Leaving the with-block closes the socket; llama-server
                    # notices the disconnect and frees the slot.
                    raise Cancelled
                if not raw_line.startswith(b"data: "):
                    continue
                chunk = json.loads(raw_line[6:].decode("utf-8"))
                out.append(chunk.get("content", ""))
                if chunk.get("stop"):
                    timings = chunk.get("timings") or {}
                    log.debug(
                        "llama: prompt %s tok (%.0f ms, %s cached), gen %s tok (%.0f ms)",
                        timings.get("prompt_n"), timings.get("prompt_ms", 0),
                        chunk.get("tokens_cached"), timings.get("predicted_n"),
                        timings.get("predicted_ms", 0),
                    )
                    break
        text = "".join(out)
        if heal:
            # The grammar guarantees the forced prefix; return only what follows.
            text = text[len(heal):] if text.startswith(heal) else ""
        return text
