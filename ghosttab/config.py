"""Runtime configuration, loaded from an optional JSON file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

# Apps where inline completion is never wanted: terminals, IDEs (which have
# their own completion), shell surfaces and credential prompts. Matched
# case-insensitively against the focused process' executable name.
DEFAULT_BLOCKED_EXES = [
    "WindowsTerminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
    "conhost.exe", "ConEmu64.exe", "ConEmu.exe", "mintty.exe", "Alacritty.exe",
    "Hyper.exe", "Code.exe", "Code - Insiders.exe", "Cursor.exe",
    "Windsurf.exe", "devenv.exe", "idea64.exe", "webstorm64.exe",
    "pycharm64.exe", "goland64.exe", "clion64.exe", "rider64.exe",
    "rubymine64.exe", "phpstorm64.exe", "sublime_text.exe", "notepad++.exe",
    "gvim.exe", "nvim-qt.exe", "Taskmgr.exe", "explorer.exe",
    "SearchHost.exe", "StartMenuExperienceHost.exe",
    "ShellExperienceHost.exe", "SystemSettings.exe", "control.exe",
    "regedit.exe", "mmc.exe", "SecHealthUI.exe", "CredentialUIBroker.exe",
    "consent.exe", "LockApp.exe", "osk.exe", "KeePass.exe", "KeePassXC.exe",
    "1Password.exe", "Bitwarden.exe",
]


@dataclass
class Config:
    server_url: str = "http://127.0.0.1:8089"
    max_tokens: int = 48
    temperature: float = 0.1
    request_timeout_s: float = 20.0
    debounce_ms: int = 400
    poll_ms: int = 250
    typeahead_check_ms: int = 40
    draft_budget: int = 920
    trailing_budget: int = 200
    # Visible text above the field, added to the prompt.
    screen_context: bool = True
    screen_context_budget: int = 2400
    screen_context_ttl_s: float = 4.0
    autocomplete: bool = True
    # Dictation: speak, and the cleaned-up text is typed at the cursor.
    dictation: bool = True
    dictation_hotkey: str = "ctrl+alt+space"
    dictation_cleanup: bool = True
    dictation_vocabulary: list[str] = field(default_factory=list)
    dictation_silence_ms: int = 1500  # stop after this long without speech; 0 = only by hotkey
    dictation_max_s: int = 120
    dictation_input_file: str = ""  # testing: a WAV file used instead of the microphone
    # Selection assistant.
    assistant: bool = True
    assistant_hotkey: str = "ctrl+alt+a"
    assistant_selection_dot: bool = True
    suggestion_chaining: bool = True
    swallow_tab_while_generating: bool = True
    personalization: list[str] = field(default_factory=list)
    ignored_apps: list[str] = field(default_factory=list)
    blocked_exes: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_EXES))
    text_color: str = "#8c8c8c"
    keycap_color: str = "#6f6f6f"
    debug: bool = False

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        cfg = cls()
        if path is None or not path.exists():
            return cfg
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys in {path}: {sorted(unknown)}")
        for key, value in data.items():
            setattr(cfg, key, value)
        return cfg
