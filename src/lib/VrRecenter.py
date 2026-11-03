import ctypes
from functools import lru_cache
import os
from pathlib import Path
import platform

import psutil

from .EDKeys import EDKeys
from .Logger import log
from .Screenshot import set_game_window_active


def recenter_game(keys: EDKeys) -> None:
    if "HMDReset" not in keys.keys:
        raise ValueError("Assign a keyboard or mouse binding to Reset HMD Orientation (HMDReset) in Elite Dangerous controls.")
    set_game_window_active(require_focus=True)
    keys.send("HMDReset")


def get_openkneeboard_api_path() -> Path:
    if platform.system() != "Windows":
        raise RuntimeError("The OpenKneeboard recenter API requires Windows.")
    override = os.environ.get("OPENKNEEBOARD_CAPI_DLL")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"OPENKNEEBOARD_CAPI_DLL does not point to a file: {path}")
        return path
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Fred Emmott\OpenKneeboard") as key:
            directory, _ = winreg.QueryValueEx(key, "InstallationBinPath")
    except FileNotFoundError:
        directory = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OpenKneeboard" / "bin")
    suffix = 64 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32
    path = Path(directory) / f"OpenKneeboard_CAPI{suffix}.dll"
    if not path.is_file():
        raise FileNotFoundError(f"OpenKneeboard API not found: {path}. Start or install OpenKneeboard.")
    return path


@lru_cache(maxsize=1)
def load_openkneeboard_api(path: Path):
    library = ctypes.CDLL(str(path))
    send = library.OpenKneeboard_send_utf8
    send.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    send.restype = None
    return library


def recenter_openkneeboard() -> bool:
    if not any(
        process.info["name"] and process.info["name"].casefold() == "openkneeboardapp.exe"
        for process in psutil.process_iter(["name"])
    ):
        log("info", "OpenKneeboard is not running; skipping its optional recenter.")
        return False
    try:
        path = get_openkneeboard_api_path()
    except FileNotFoundError as error:
        log("warn", f"OpenKneeboard API is unavailable; skipping its optional recenter: {error}")
        return False
    library = load_openkneeboard_api(path)
    name, value = b"RemoteUserAction", b"RECENTER_VR"
    library.OpenKneeboard_send_utf8(name, len(name), value, len(value))
    return True


def recenter_game_and_kneeboard(keys: EDKeys) -> bool:
    """Return whether the optional OpenKneeboard command was sent; game errors still raise."""
    sent = []
    errors = []
    for name, operation in (
        ("Elite Dangerous", lambda: recenter_game(keys)),
        ("OpenKneeboard", recenter_openkneeboard),
    ):
        try:
            command_sent = operation()
        except Exception as error:
            log("error", f"{name} recenter failed: {error}")
            errors.append(f"{name}: {error}")
        else:
            if command_sent is not False:
                sent.append(name)
    if errors:
        dispatched = f" Commands sent to: {', '.join(sent)}." if sent else " No other recenter commands were sent."
        raise RuntimeError("; ".join(errors) + dispatched)
    return "OpenKneeboard" in sent
