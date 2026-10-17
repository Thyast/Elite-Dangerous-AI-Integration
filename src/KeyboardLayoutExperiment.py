"""Interactive harness for testing Windows keyboard-layout-aware input.

Run this with Elite Dangerous focused:
    python -m src.KeyboardLayoutExperiment

The harness does not change the normal EDKeys behaviour. It compares the
historical fixed scancode with the scancode resolved from the active Windows
keyboard layout and lets the user test each one manually.
"""

from __future__ import annotations

import json
import platform
import time

from .lib.directinput import (
    LAYOUT_DEPENDENT_KEY_CHARACTERS,
    PressKey,
    ReleaseKey,
    resolve_layout_key_name,
)
from .lib.Config import get_asset_path


with open(get_asset_path("keymap.json"), encoding="utf-8") as keymap_file:
    keymap = json.load(keymap_file)


def _print_resolution(key_name: str) -> None:
    resolved = resolve_layout_key_name(key_name)
    fixed_scancode = keymap[key_name]
    character = LAYOUT_DEPENDENT_KEY_CHARACTERS[key_name]
    print(
        f"{key_name} ({character!r}): "
        f"fixed scancode={fixed_scancode}, "
        f"layout VK=0x{resolved.virtual_key:02X}, "
        f"layout scancode={resolved.scan_code}, "
        f"implicit shift state={resolved.shift_state}"
    )


def _send_after_countdown(scancode: int) -> None:
    print("Switch to Elite Dangerous now.")
    for remaining in range(5, 0, -1):
        print(f"Sending in {remaining}...", flush=True)
        time.sleep(1)

    PressKey(scancode)
    time.sleep(0.1)
    ReleaseKey(scancode)
    print("Key sent. Observe the result in Elite Dangerous.")
    time.sleep(2)


def main() -> None:
    if platform.system() != "Windows":
        raise SystemExit("This experiment must be run on Windows.")

    key_names = ["Key_Comma", "Key_SemiColon"]
    print("Keep Elite Dangerous focused when testing a key.")
    print("Close the opened map before each subsequent test.")

    for key_name in key_names:
        _print_resolution(key_name)
        fixed_scancode = keymap[key_name]
        resolved = resolve_layout_key_name(key_name)

        print(f"\nTesting {key_name}:")
        print("[1] historical fixed scancode (switch to Elite during countdown)")
        print("[2] active-layout-resolved scancode (switch to Elite during countdown)")
        choice = input("Choose a method (1/2, or Enter to skip): ").strip()
        if choice == "1":
            _send_after_countdown(fixed_scancode)
        elif choice == "2":
            if resolved.shift_state:
                print(
                    "The character requires implicit modifiers; "
                    "this prototype does not inject them automatically."
                )
            else:
                _send_after_countdown(resolved.scan_code)


if __name__ == "__main__":
    main()
