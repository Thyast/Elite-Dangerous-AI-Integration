from __future__ import annotations

import platform
import subprocess
import tempfile
from pathlib import Path
from time import sleep

from PIL import Image, ImageOps

from .Logger import log


WINDOWS_GAME_WINDOW_TITLE = "Elite - Dangerous (CLIENT)"
MACOS_STEAM_STREAMING_PROCESS_NAMES = ("steamstreamingclient", "streaming_client", "Steam Streaming Client")


def resize_and_crop_16_9(image: Image.Image, new_height: int) -> Image.Image:
    target_size = (round(new_height * (16 / 9)), new_height)
    return ImageOps.fit(
        image,
        target_size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def get_windows_game_window_handle():
    if platform.system() != "Windows":
        return None

    import win32gui

    return win32gui.FindWindow(0, WINDOWS_GAME_WINDOW_TITLE)


def set_windows_game_window_active(*, require_focus: bool = False) -> None:
    handle = get_windows_game_window_handle()
    if not handle:
        if require_focus:
            raise RuntimeError("Elite Dangerous is not running or its game window is unavailable.")
        log("info", "Unable to find Elite game window")
        return

    import win32gui

    try:
        if require_focus and win32gui.IsIconic(handle):
            import win32con
            win32gui.ShowWindow(handle, win32con.SW_RESTORE)
        if not require_focus or win32gui.GetForegroundWindow() != handle:
            win32gui.SetForegroundWindow(handle)
        sleep(0.15)
        log("debug", "Set game window as active")
    except Exception as error:
        if require_focus:
            raise RuntimeError(f"Unable to focus Elite Dangerous; no recenter input was sent: {error}") from error
        log("warn", "Failed to set game window as active")
    if require_focus and win32gui.GetForegroundWindow() != handle:
        raise RuntimeError("Unable to focus Elite Dangerous; no recenter input was sent. Activate the game window and retry.")


def screenshot_windows_game_window(new_height: int) -> Image.Image | None:
    handle = get_windows_game_window_handle()
    if not handle:
        log("warn", "Window not found!")
        return None

    import pyautogui
    import win32gui

    set_windows_game_window_active()
    x, y, x1, y1 = win32gui.GetClientRect(handle)
    x, y = win32gui.ClientToScreen(handle, (x, y))
    x1, y1 = win32gui.ClientToScreen(handle, (x1, y1))
    width = x1 - x
    height = y1 - y
    image = pyautogui.screenshot(region=(x, y, width, height)).convert("RGB")
    return resize_and_crop_16_9(image, new_height)


def _macos_steam_streaming_window_bounds() -> tuple[int, int, int, int] | None:
    try:
        import Quartz
    except ImportError:
        log("warn", "Quartz is not installed; macOS window screenshot is unavailable")
        return None

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
    )
    process_names = {name.lower() for name in MACOS_STEAM_STREAMING_PROCESS_NAMES}
    candidates = []
    for window in windows:
        owner = str(window.get("kCGWindowOwnerName", "")).lower()
        if owner not in process_names:
            continue

        bounds = window.get("kCGWindowBounds")
        if not bounds:
            continue

        width = int(bounds.get("Width", 0))
        height = int(bounds.get("Height", 0))
        if width <= 0 or height <= 0:
            continue

        candidates.append(
            (
                width * height,
                int(bounds.get("X", 0)),
                int(bounds.get("Y", 0)),
                width,
                height,
            )
        )

    if not candidates:
        log("warn", "Steam Streaming Client window not found")
        return None

    _, x, y, width, height = max(candidates)
    return x, y, width, height


def screenshot_macos_steam_streaming_client(new_height: int) -> Image.Image | None:
    bounds = _macos_steam_streaming_window_bounds()
    if bounds is None:
        return None

    x, y, width, height = bounds
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = Path(tmp.name)

    try:
        subprocess.run(
            ["screencapture", "-x", "-R", f"{x},{y},{width},{height}", str(path)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        with Image.open(path) as image:
            return resize_and_crop_16_9(image.convert("RGB"), new_height)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log("warn", f"Unable to capture Steam Streaming Client screenshot: {exc}")
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def screenshot_game_window(new_height: int) -> Image.Image | None:
    system = platform.system()
    if system == "Windows":
        return screenshot_windows_game_window(new_height)
    if system == "Darwin":
        return screenshot_macos_steam_streaming_client(new_height)

    return None


def set_game_window_active(*, require_focus: bool = False) -> None:
    if platform.system() == "Windows":
        set_windows_game_window_active(require_focus=require_focus)
    elif require_focus:
        raise RuntimeError("Verified Elite Dangerous window activation is currently supported on Windows only.")
