"""Global hotkeys, matched inside the low-level keyboard hook."""

from __future__ import annotations

from dataclasses import dataclass

NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
NAMED_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})
MODIFIERS = {"ctrl", "alt", "shift", "win"}


@dataclass(frozen=True)
class Hotkey:
    vk: int
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    win: bool = False

    @classmethod
    def parse(cls, text: str) -> "Hotkey":
        parts = [p.strip().lower() for p in text.split("+") if p.strip()]
        mods = {p for p in parts if p in MODIFIERS}
        keys = [p for p in parts if p not in MODIFIERS]
        if len(keys) != 1:
            raise ValueError(f"hotkey {text!r} must name exactly one non-modifier key")
        key = keys[0]
        if key in NAMED_KEYS:
            vk = NAMED_KEYS[key]
        elif len(key) == 1 and key.isalnum() and key.isascii():
            vk = ord(key.upper())
        else:
            raise ValueError(f"unknown key {key!r} in hotkey {text!r}")
        return cls(vk, "ctrl" in mods, "alt" in mods, "shift" in mods, "win" in mods)

    def matches(self, vk: int, shift: bool, ctrl: bool, alt: bool, win: bool) -> bool:
        return (vk == self.vk and ctrl == self.ctrl and alt == self.alt
                and shift == self.shift and win == self.win)


def hotkey_label(text: str) -> str:
    """'ctrl+alt+space' -> 'Ctrl+Alt+Space'."""
    return "+".join(p.strip().capitalize() for p in text.split("+") if p.strip())
