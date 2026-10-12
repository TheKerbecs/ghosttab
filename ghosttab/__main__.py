"""ghosttab: inline autocomplete, dictation and a selection assistant for
Windows, backed by a local llama.cpp server.

    python -m ghosttab [--config ghosttab.json] [--debug]
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path

from . import win32
from .config import Config
from .engine import Engine
from .hotkeys import hotkey_label
from .llm import LlamaClient
from .overlay import Overlay


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ghosttab")
    ap.add_argument("--config", type=Path, default=Path("ghosttab.json"))
    ap.add_argument("--server", help="llama-server base URL (overrides config)")
    ap.add_argument("--debug", action="store_true", help="verbose log, including typed and dictated text")
    ap.add_argument("--log-file", type=Path, help="log to this file instead of the console")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.server:
        cfg.server_url = args.server
    debug = args.debug or cfg.debug
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname).1s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S" if args.log_file else "%H:%M:%S",
        **({"filename": args.log_file, "encoding": "utf-8"} if args.log_file else {}),
    )
    for noisy in ("urllib3", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("ghosttab")
    if not win32.single_instance("Local\\ghosttab"):
        log.error("ghosttab is already running")
        return 1

    client = LlamaClient(cfg.server_url, cfg.request_timeout_s)
    if not client.health():
        log.error("llama-server is not reachable at %s (start it with start-server.ps1)", cfg.server_url)
        return 1
    props = client.props()
    if props.get("total_slots", 1) < 2:
        log.warning("llama-server has one slot; dictation and the assistant will evict the autocomplete "
                    "cache (start it with -np 2)")
    if cfg.dictation and not (props.get("modalities") or {}).get("audio"):
        log.warning("the model was loaded without audio support (no --mmproj); dictation is disabled")
        cfg.dictation = False
    log.info("warming up llama-server (first run after a server start can take a while)...")
    try:
        log.info("warm-up done in %.1f s", client.warmup())
    except Exception as exc:
        log.warning("warm-up failed (%s); continuing", exc)

    win32.enable_dpi_awareness()
    root = tk.Tk()
    overlay = Overlay(root, cfg.text_color)

    ui_queue: queue.Queue = queue.Queue()

    def drain_ui():
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        root.after(8, drain_ui)

    engine = Engine(cfg, client, ui_queue.put, overlay)
    key_handlers = []
    features = []
    if cfg.dictation:
        from .dictation import Dictation
        dictation = Dictation(cfg, client, ui_queue.put, root)
        key_handlers.append(dictation.handle_key)
        features.append(f"dictation {hotkey_label(cfg.dictation_hotkey)}")
    if cfg.assistant:
        from .assistant import Assistant
        assistant = Assistant(cfg, client, ui_queue.put, root)
        key_handlers.append(assistant.handle_key)
        features.append(f"assistant {hotkey_label(cfg.assistant_hotkey)}")
    if cfg.autocomplete:
        key_handlers.append(engine.on_key)
        features.insert(0, "autocomplete Tab")

    def on_key(vk, shift, ctrl, alt, win):
        return any(handler(vk, shift, ctrl, alt, win) for handler in key_handlers)

    hook = win32.KeyboardHook(on_key)
    threading.Thread(target=engine.run, name="engine", daemon=True).start()
    hook.start()
    root.after(8, drain_ui)
    log.info("ghosttab running against %s: %s.%s", cfg.server_url, ", ".join(features),
             "" if args.log_file else " Ctrl+C to quit.")

    # Let Ctrl+C in the console reach Python while Tk's loop runs.
    def keepalive():
        root.after(200, keepalive)
    root.after(200, keepalive)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        hook.stop()
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
