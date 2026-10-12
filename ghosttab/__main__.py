"""ghosttab: system-wide inline autocomplete for Windows, backed by llama.cpp.

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
from .llm import LlamaClient
from .overlay import Overlay


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ghosttab")
    ap.add_argument("--config", type=Path, default=Path("ghosttab.json"))
    ap.add_argument("--server", help="llama-server base URL (overrides config)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.server:
        cfg.server_url = args.server
    debug = args.debug or cfg.debug
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("ghosttab")

    client = LlamaClient(cfg.server_url, cfg.request_timeout_s)
    if not client.health():
        log.error("llama-server is not reachable at %s (start it with start-server.ps1)", cfg.server_url)
        return 1
    log.info("warming up llama-server (first run after a server start can take a while)...")
    try:
        log.info("warm-up done in %.1f s", client.warmup())
    except Exception as exc:
        log.warning("warm-up failed (%s); continuing", exc)

    win32.enable_dpi_awareness()
    root = tk.Tk()
    overlay = Overlay(root, cfg.text_color, cfg.keycap_color)

    ui_queue: queue.Queue = queue.Queue()

    def drain_ui():
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        root.after(8, drain_ui)

    engine = Engine(cfg, client, ui_queue.put, overlay)
    hook = win32.KeyboardHook(engine.on_key)
    threading.Thread(target=engine.run, name="engine", daemon=True).start()
    hook.start()
    root.after(8, drain_ui)
    log.info("ghosttab running against %s - Tab accepts, Shift+Tab accepts one word, Esc dismisses. Ctrl+C to quit.",
             cfg.server_url)

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
