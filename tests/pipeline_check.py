"""Model-backed checks of the dictation and assistant pipelines against a
running llama-server (no UI, no microphone).

    python tests/pipeline_check.py [--server http://127.0.0.1:8089]
"""

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ghosttab.llm import LlamaClient  # noqa: E402
from ghosttab.prompt import (QUICK_ACTIONS, PromptInput, build_assistant_messages,  # noqa: E402
                             build_dictation_messages, build_messages)

AUDIO = Path(__file__).parent / "audio"
EXPECT = {
    "en_dictation.wav": ["9", "Tuesday", "signed contract"],
    "de_dictation.wav": ["9 Uhr", "Dienstag", "Vertrag"],
}
FORBIDDEN = {
    "en_dictation.wav": ["10", "Um,", "actually"],
    "de_dictation.wav": ["10 Uhr", "nein", "Nein"],
}
SAMPLES = {
    "en": "hey team, i think we should of moved the meeting to thursday because alot of people is on vacation",
    "de": "Hallo zusammen, ich glaube wir hätten das Meeting auf Donnerstag verschieben sollen, weil viele im Urlaub sind",
}


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8089")
    args = ap.parse_args()
    client = LlamaClient(args.server, 60)
    results = []

    for wav in sorted(AUDIO.glob("*.wav")):
        t0 = time.perf_counter()
        transcript = client.transcribe(wav.read_bytes())
        t1 = time.perf_counter()
        cleaned = client.chat(build_dictation_messages(transcript, []), max_tokens=512).strip()
        t2 = time.perf_counter()
        print(f"{wav.name}: transcribe {1000 * (t1 - t0):.0f} ms, clean-up {1000 * (t2 - t1):.0f} ms")
        print(f"   transcript: {transcript}")
        print(f"   cleaned:    {cleaned}")
        missing = [w for w in EXPECT.get(wav.name, []) if w not in cleaned]
        present = [w for w in FORBIDDEN.get(wav.name, []) if w in cleaned]
        results.append(check(f"dictation {wav.name}", not missing and not present,
                             f"missing {missing}, should be gone {present}" if missing or present else ""))

    for lang, text in SAMPLES.items():
        for action in QUICK_ACTIONS:
            pieces = []
            t0 = time.perf_counter()
            answer = client.chat_stream(build_assistant_messages(text, action.instruction, "Discord.exe"),
                                        pieces.append, cancel=threading.Event()).strip()
            ms = 1000 * (time.perf_counter() - t0)
            print(f"[{lang}] {action.label:9} {ms:5.0f} ms, {len(pieces):3} chunks: {answer[:150]}")
            ok = answer != "" and "".join(pieces).strip() == answer
            if action.label == "Explain":
                ok = ok and answer.lower() != text.lower() and "meeting" in answer.lower()
                ok = ok and ("writer" in answer.lower() or "schreiber" in answer.lower() or "autor" in answer.lower())
            results.append(ok)

    # Autocomplete's cached prompt must survive assistant requests (separate slot).
    messages = build_messages(PromptInput("Thanks for the update, I will", "", "Discord.exe", "#general"), [], 200)
    client.complete(messages, "Thanks for the update, I will", max_tokens=8, temperature=0.1,
                    stop=["\n"], cancel=threading.Event())
    client.chat_stream(build_assistant_messages(SAMPLES["en"], "Summarize it.", "x"), lambda p: None)
    body = {"prompt": client.render_prefix(messages) + "Thanks for the update, I will", "n_predict": 1,
            "cache_prompt": True, "id_slot": 0}
    cached = client.session.post(f"{args.server}/completion", json=body, timeout=30).json().get("tokens_cached", 0)
    results.append(check("autocomplete cache survives an assistant request", cached > 100, f"{cached} tokens cached"))

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
