from pathlib import Path
import sys
from typing import Any

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.lib.actions import Actions


class FakeKeys:
    def __init__(self, bound: set[str]) -> None:
        self.keys: dict[str, Any] = {name: {"key": 1, "mods": []} for name in bound}
        self.sent: list[str] = []

    def send(self, key_name: str, **_: Any) -> None:
        if key_name not in self.keys:
            raise Exception(f"Unable to retrieve keybinding for {key_name}.")
        self.sent.append(key_name)


@pytest.fixture
def projected_states() -> dict[str, Any]:
    return {"CurrentStatus": {"flags": {}}, "NavInfo": {}}


@pytest.fixture(autouse=True)
def no_window_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Actions, "setGameWindowActive", lambda: None)


def test_fsd_jump_succeeds_when_setspeed100_unbound(monkeypatch, projected_states) -> None:
    fake_keys = FakeKeys({"Supercruise"})
    monkeypatch.setattr(Actions, "keys", fake_keys)

    result = Actions.fsd_jump({"jump_type": "supercruise"}, projected_states)

    assert fake_keys.sent == ["Supercruise"]
    assert "Frame Shift Drive is now charging" in result
    assert "SetSpeed100" in result


def test_fsd_jump_sets_throttle_when_setspeed100_bound(monkeypatch, projected_states) -> None:
    fake_keys = FakeKeys({"HyperSuperCombination", "SetSpeed100"})
    monkeypatch.setattr(Actions, "keys", fake_keys)

    result = Actions.fsd_jump({}, projected_states)

    assert fake_keys.sent == ["HyperSuperCombination", "SetSpeed100"]
    assert result == "Frame Shift Drive is now charging for a jump"


def test_fsd_jump_still_fails_when_jump_key_unbound(monkeypatch, projected_states) -> None:
    monkeypatch.setattr(Actions, "keys", FakeKeys({"SetSpeed100"}))

    with pytest.raises(Exception, match="Supercruise"):
        Actions.fsd_jump({"jump_type": "supercruise"}, projected_states)
