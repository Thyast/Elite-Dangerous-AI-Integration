import ctypes
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib import Screenshot, VrRecenter


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "OpenKneeboard_CAPI64.dll"
    path.write_bytes(b"test")
    monkeypatch.setenv("OPENKNEEBOARD_CAPI_DLL", str(path))
    monkeypatch.setattr(VrRecenter.platform, "system", lambda: "Windows")
    monkeypatch.setattr(VrRecenter.psutil, "process_iter", lambda _attrs: [
        SimpleNamespace(info={"name": "OpenKneeboardApp.exe"}),
    ])
    library = SimpleNamespace(OpenKneeboard_send_utf8=Mock())
    loader = Mock(return_value=library)
    monkeypatch.setattr(VrRecenter.ctypes, "CDLL", loader)
    VrRecenter.load_openkneeboard_api.cache_clear()
    yield SimpleNamespace(path=path, library=library, loader=loader)
    VrRecenter.load_openkneeboard_api.cache_clear()


def test_openkneeboard_uses_the_installed_c_api_and_exact_utf8_event(api):
    assert VrRecenter.recenter_openkneeboard() is True
    assert VrRecenter.recenter_openkneeboard() is True
    api.loader.assert_called_once_with(str(api.path))
    send = api.library.OpenKneeboard_send_utf8
    send.assert_called_with(b"RemoteUserAction", 16, b"RECENTER_VR", 11)
    assert send.argtypes == [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    assert send.restype is None


def test_openkneeboard_registry_location_supports_installs_outside_program_files(api, monkeypatch):
    monkeypatch.delenv("OPENKNEEBOARD_CAPI_DLL")
    key = Mock()
    key.__enter__ = Mock(return_value=key)
    key.__exit__ = Mock(return_value=False)
    registry = SimpleNamespace(
        HKEY_CURRENT_USER=1, OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=(str(api.path.parent), 1)),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    assert VrRecenter.get_openkneeboard_api_path() == api.path


def test_closed_or_uninstalled_openkneeboard_is_skipped_without_looking_for_its_api(api, monkeypatch):
    monkeypatch.setattr(VrRecenter.psutil, "process_iter", lambda _attrs: [])
    lookup = Mock(side_effect=AssertionError("must not look for the API of an absent application"))
    monkeypatch.setattr(VrRecenter, "get_openkneeboard_api_path", lookup)
    assert VrRecenter.recenter_openkneeboard() is False
    lookup.assert_not_called()
    api.loader.assert_not_called()
    api.library.OpenKneeboard_send_utf8.assert_not_called()


def test_running_openkneeboard_without_an_available_api_is_skipped(api, monkeypatch):
    monkeypatch.setenv("OPENKNEEBOARD_CAPI_DLL", str(api.path.parent / "missing.dll"))
    assert VrRecenter.recenter_openkneeboard() is False
    api.loader.assert_not_called()
    api.library.OpenKneeboard_send_utf8.assert_not_called()


def test_openkneeboard_loader_errors_are_not_success_shaped(api):
    api.loader.side_effect = OSError("DLL architecture mismatch")
    with pytest.raises(OSError, match="architecture"):
        VrRecenter.recenter_openkneeboard()


@pytest.mark.parametrize("failed_component", ["game", "kneeboard", "both"])
def test_partial_recenter_attempts_both_components_and_names_each_failure(monkeypatch, failed_component):
    game = Mock(side_effect=RuntimeError("game focus failed") if failed_component in ("game", "both") else None)
    kneeboard = Mock(return_value=True, side_effect=RuntimeError("kneeboard API failed") if failed_component in ("kneeboard", "both") else None)
    monkeypatch.setattr(VrRecenter, "recenter_game", game)
    monkeypatch.setattr(VrRecenter, "recenter_openkneeboard", kneeboard)
    keys = Mock()
    with pytest.raises(RuntimeError) as error:
        VrRecenter.recenter_game_and_kneeboard(keys)
    game.assert_called_once_with(keys)
    kneeboard.assert_called_once_with()
    if failed_component == "game":
        assert "game focus failed" in str(error.value) and "Commands sent to: OpenKneeboard" in str(error.value)
    elif failed_component == "kneeboard":
        assert "kneeboard API failed" in str(error.value) and "Commands sent to: Elite Dangerous" in str(error.value)
    else:
        assert "game focus failed" in str(error.value) and "kneeboard API failed" in str(error.value)
        assert "No other" in str(error.value)


@pytest.mark.parametrize("available", [False, True])
def test_game_recenter_succeeds_independently_of_optional_openkneeboard(monkeypatch, available):
    game = Mock()
    monkeypatch.setattr(VrRecenter, "recenter_game", game)
    monkeypatch.setattr(VrRecenter, "recenter_openkneeboard", Mock(return_value=available))
    keys = Mock()
    assert VrRecenter.recenter_game_and_kneeboard(keys) is available
    game.assert_called_once_with(keys)


def test_game_errors_are_not_hidden_when_openkneeboard_is_absent(monkeypatch):
    monkeypatch.setattr(VrRecenter, "recenter_game", Mock(side_effect=RuntimeError("game focus failed")))
    monkeypatch.setattr(VrRecenter, "recenter_openkneeboard", Mock(return_value=False))
    with pytest.raises(RuntimeError, match="game focus failed") as error:
        VrRecenter.recenter_game_and_kneeboard(Mock())
    assert "No other recenter commands were sent" in str(error.value)


@pytest.fixture
def window(monkeypatch):
    calls = []
    state = {"foreground": 456, "minimized": True}
    gui = SimpleNamespace(
        IsIconic=lambda _handle: state["minimized"],
        ShowWindow=lambda handle, mode: calls.append(("restore", handle, mode)),
        GetForegroundWindow=lambda: state["foreground"],
        SetForegroundWindow=lambda handle: (calls.append(("focus", handle)), state.update(foreground=handle)),
    )
    monkeypatch.setattr(Screenshot, "get_windows_game_window_handle", lambda: 123)
    monkeypatch.setattr(Screenshot, "sleep", lambda _time: None)
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(SW_RESTORE=9))
    return SimpleNamespace(gui=gui, state=state, calls=calls)


def test_strict_game_focus_restores_a_minimized_window_before_activating_it(window):
    Screenshot.set_windows_game_window_active(require_focus=True)
    assert window.calls == [("restore", 123, 9), ("focus", 123)]


def test_strict_game_focus_checks_the_real_foreground_even_without_a_win32_exception(window):
    window.gui.SetForegroundWindow = lambda _handle: None
    with pytest.raises(RuntimeError, match="focus"):
        Screenshot.set_windows_game_window_active(require_focus=True)


def test_strict_game_focus_does_not_fail_when_the_game_is_already_active(window):
    window.state.update(foreground=123, minimized=False)
    window.gui.SetForegroundWindow = Mock(side_effect=RuntimeError("must not run"))
    Screenshot.set_windows_game_window_active(require_focus=True)
    window.gui.SetForegroundWindow.assert_not_called()


def test_missing_game_and_denied_foreground_are_explicit(window, monkeypatch):
    window.gui.SetForegroundWindow = Mock(side_effect=OSError("Windows denied activation"))
    with pytest.raises(RuntimeError, match="Windows denied"):
        Screenshot.set_windows_game_window_active(require_focus=True)
    monkeypatch.setattr(Screenshot, "get_windows_game_window_handle", lambda: 0)
    with pytest.raises(RuntimeError, match="not running"):
        Screenshot.set_windows_game_window_active(require_focus=True)
