from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.ActionManager import ActionManager
from lib.PluginHelper import PluginHelper
from lib.actions import Actions
from lib.actions.Plotter import Plotter
from lib.projections.nav_info import NavInfoStateModel
from lib.projections.current_status import CurrentStatusState


@pytest.fixture
def navigation(monkeypatch):
    monkeypatch.setattr(ActionManager, "actions", {})
    monkeypatch.setattr("lib.ActionManager.KeyValueStore", lambda _name: Mock())
    manager = ActionManager()
    permissions = {key: True for key in (
        "plotToTarget", "galaxyMapOpenOrClose", "target_next_system_in_route",
    )}
    manager.set_allowed_actions(permissions)
    states = {
        "Location": {"StarSystem": "Muth"},
        "CurrentStatus": {"flags": {"InMainShip": True}, "flags2": {}, "GuiFocus": "NoFocus"},
        "NavInfo": {"NavRoute": [], "NextJumpTarget": None},
    }
    calls = []

    def plot(args, _states):
        calls.append(("plot", args))
        states["NavInfo"] = {
            "NavRoute": [{"StarSystem": "Intermediate", "Scoopable": True},
                         {"StarSystem": args["system"], "Scoopable": True}],
            "NextJumpTarget": None,
        }
        states["CurrentStatus"]["GuiFocus"] = "GalaxyMap"
        return "Route plotted"

    def close(args, _states):
        calls.append(("close", args))
        states["CurrentStatus"]["GuiFocus"] = "NoFocus"
        return "Galaxy map has been closed."

    def target(args, _states):
        calls.append(("target", args))
        states["NavInfo"]["NextJumpTarget"] = states["NavInfo"]["NavRoute"][0]["StarSystem"]
        return "Targeting next system in route"

    def wait(name, condition, timeout):
        model = (NavInfoStateModel if name == "NavInfo" else CurrentStatusState)(**states[name])
        if not condition(model):
            raise TimeoutError
        return model

    for name, method, kind in (
        ("plotToTarget", plot, "ship"),
        ("galaxyMapOpenOrClose", close, "ship"),
        ("target_next_system_in_route", target, "mainship"),
    ):
        manager.registerAction(name, name, {}, method, kind, permission=name)
    helper = PluginHelper.__new__(PluginHelper)
    helper._action_manager = manager
    helper._config = {"game_actions_var": True, "allowed_actions": permissions}
    helper._event_manager = SimpleNamespace(
        get_current_state=lambda: ([], states), wait_for_condition=wait,
    )
    return helper, states, calls


@pytest.mark.parametrize("ship_flags", [
    {},
    {"Docked": True},
    {"Landed": True},
    {"Docked": True, "Landed": True, "FsdMassLocked": True, "LandingGearDown": True},
])
def test_navigation_prepares_in_flight_hangar_or_on_surface_without_departing(navigation, ship_flags):
    helper, states, calls = navigation
    flags = {"InMainShip": True, **ship_flags}
    states["CurrentStatus"]["flags"] = flags.copy()
    result = helper.prepare_navigation("Wolf 397")
    assert result == {
        "status": "ready", "system": "Wolf 397",
        "active_destination": "Wolf 397", "next_jump": "Intermediate",
        "message": "Destination verified, galaxy map closed and next route system targeted. "
                   "FSD not engaged; no takeoff or throttle changes performed.",
    }
    assert calls == [
        ("plot", {"system": "Wolf 397"}), ("close", {"desired_state": "close"}), ("target", {}),
    ]
    assert states["CurrentStatus"]["GuiFocus"] == "NoFocus"
    assert states["CurrentStatus"]["flags"] == flags


def test_wrong_plotted_destination_is_reported_and_not_targeted(navigation):
    helper, states, calls = navigation

    def wrong_route(args, _states):
        calls.append(("plot", args))
        states["NavInfo"] = {
            "NavRoute": [{"StarSystem": "Tascheter Sector HM-M a7-2", "Scoopable": False}],
            "NextJumpTarget": "Tascheter Sector HM-M a7-2",
        }
        return "Failed to confirm a route to Wolf 397"

    helper._action_manager.actions["plotToTarget"]["method"] = wrong_route
    result = helper.prepare_navigation("Wolf 397")
    assert result["status"] == "failed"
    assert result["active_destination"] == "Tascheter Sector HM-M a7-2"
    assert "not evidence" in result["message"]
    assert [name for name, _ in calls] == ["plot"]


@pytest.mark.parametrize("docked", [False, True])
@pytest.mark.parametrize("permission", [
    "plotToTarget", "galaxyMapOpenOrClose", "target_next_system_in_route", "all_game_actions",
])
def test_disabled_navigation_actions_are_never_bypassed(navigation, permission, docked):
    helper, states, calls = navigation
    states["CurrentStatus"]["flags"]["Docked"] = docked
    if permission == "all_game_actions":
        helper._config["game_actions_var"] = False
    else:
        helper._config["allowed_actions"][permission] = False
    assert helper.prepare_navigation("Wolf 397")["status"] == "blocked"
    assert calls == []


@pytest.mark.parametrize("flags,flags2", [
    ({}, {}),
    ({"InSRV": True}, {}),
    ({"InFighter": True}, {}),
    ({"InMainShip": True, "FsdCharging": True}, {}),
    ({"InMainShip": True, "FsdJump": True}, {}),
    ({"InMainShip": True, "Docked": True, "FsdCharging": True}, {}),
    ({"InMainShip": True}, {"OnFoot": True}),
    ({"InMainShip": True, "Docked": True}, {"OnFoot": True, "OnFootInHangar": True}),
])
def test_navigation_requires_commander_aboard_main_ship_and_no_active_jump(navigation, flags, flags2):
    helper, states, calls = navigation
    states["CurrentStatus"].update(flags=flags, flags2=flags2)
    assert helper.prepare_navigation("Wolf 397")["status"] == "blocked"
    assert not calls


@pytest.mark.parametrize("docked", [False, True])
def test_existing_correct_route_is_reused_and_targeted(navigation, docked):
    helper, states, calls = navigation
    states["CurrentStatus"]["flags"]["Docked"] = docked
    states["NavInfo"] = {
        "NavRoute": [{"StarSystem": "Wolf 397", "Scoopable": True}],
        "NextJumpTarget": None,
    }
    assert helper.prepare_navigation("Wolf 397")["status"] == "ready"
    assert calls == [("target", {})]


def test_arrival_does_not_skip_local_work_or_prepare_another_jump(navigation):
    helper, states, calls = navigation
    states["Location"]["StarSystem"] = "Wolf 397"
    assert helper.prepare_navigation("Wolf 397")["status"] == "arrived"
    assert not calls


def test_next_target_must_be_confirmed(navigation):
    helper, states, _calls = navigation
    states["NavInfo"] = {
        "NavRoute": [{"StarSystem": "Wolf 397", "Scoopable": True}],
        "NextJumpTarget": "Wrong system",
    }
    helper._action_manager.actions["target_next_system_in_route"]["method"] = lambda *_: "Targeting"
    assert helper.prepare_navigation("Wolf 397")["status"] == "failed"


def test_map_closure_must_be_confirmed(navigation):
    helper, _states, calls = navigation
    helper._action_manager.actions["galaxyMapOpenOrClose"]["method"] = lambda *_: "Closing"
    assert helper.prepare_navigation("Wolf 397")["status"] == "failed"
    assert not any(name == "target" for name, _ in calls)


@pytest.mark.parametrize("action", [
    "plotToTarget", "galaxyMapOpenOrClose", "target_next_system_in_route",
])
def test_action_errors_are_not_reported_as_navigation_success(navigation, action):
    helper, _states, _calls = navigation

    def broken(*_):
        raise RuntimeError("missing key binding")

    helper._action_manager.actions[action]["method"] = broken
    result = helper.prepare_navigation("Wolf 397")
    assert result["status"] == "failed"
    assert "missing key binding" in result["message"]


def test_navigation_works_with_pydantic_projection_snapshots(navigation):
    helper, states, _calls = navigation
    states["NavInfo"] = NavInfoStateModel(NavRoute=[{"StarSystem": "Wolf 397", "Scoopable": True}],
                                         NextJumpTarget="Wolf 397")
    states["CurrentStatus"] = CurrentStatusState(flags={"InMainShip": True, "Docked": True}, GuiFocus="NoFocus")
    helper._action_manager.actions["target_next_system_in_route"]["method"] = lambda *_: "Targeting"
    helper._event_manager.wait_for_condition = lambda _name, condition, _timeout: (
        states["NavInfo"] if condition(states["NavInfo"]) else pytest.fail("Invalid target")
    )

    result = helper.prepare_navigation("Wolf 397")

    assert result["status"] == "ready"
    assert result["active_destination"] == result["next_jump"] == "Wolf 397"


def test_navigation_rechecks_destination_after_targeting(navigation):
    helper, states, _calls = navigation

    def changed_after_confirmation(_name, _condition, _timeout):
        states["NavInfo"]["NavRoute"] = [{"StarSystem": "Different", "Scoopable": True}]

    states["NavInfo"] = {
        "NavRoute": [{"StarSystem": "Wolf 397", "Scoopable": True}],
        "NextJumpTarget": "Wolf 397",
    }
    helper._event_manager.wait_for_condition = changed_after_confirmation
    assert helper.prepare_navigation("Wolf 397")["status"] == "failed"


@pytest.mark.parametrize("active_destination", [None, "Tascheter Sector HM-M a7-2"])
@pytest.mark.parametrize("system_name", ["Wolf 397", "Wolf 597", "BD+21 55"])
def test_plot_timeout_reports_actual_destination_and_clears_search_text(monkeypatch, active_destination, system_name):
    import lib.actions.Plotter as plotter_module

    states = {"NavInfo": {"NavRoute": (
        [{"StarSystem": active_destination}] if active_destination else []
    )}}
    event_manager = Mock()
    event_manager.get_current_state.return_value = ([], states)
    event_manager.wait_for_condition.side_effect = TimeoutError
    keys = Mock()
    keys.get_collisions.return_value = []
    plotter = Plotter(keys=keys, event_manager=event_manager)
    monkeypatch.setattr(plotter, "_ensure_galaxy_map_open", lambda *_: "NoFocus")
    monkeypatch.setattr(plotter, "calculate_navigation_distance_and_timing", lambda *_: (100, 3))
    monkeypatch.setattr(plotter_module, "sleep", lambda *_: None)
    hotkey = Mock()
    monkeypatch.setattr(plotter_module, "hotkey", hotkey)
    result = plotter._plot_galaxy_route(system_name, None, {"Location": {"StarSystem": "Muth"}})
    hotkey.assert_called_once_with("ctrl", "a")
    keys.send_text.assert_called_once_with(system_name)
    assert "does not establish" in result
    assert (active_destination or "No navigation route") in result


@pytest.mark.parametrize("docked", [False, True])
def test_host_target_action_selects_plotted_route_without_previous_target(navigation, monkeypatch, docked):
    helper, states, calls = navigation
    states["CurrentStatus"]["flags"]["Docked"] = docked
    keys = Mock()

    def send(key):
        calls.append(("key", key))
        states["NavInfo"]["NextJumpTarget"] = "Intermediate"

    keys.send.side_effect = send
    activate = Mock()
    monkeypatch.setattr(Actions, "keys", keys)
    monkeypatch.setattr(Actions, "setGameWindowActive", activate)
    helper._action_manager.actions["target_next_system_in_route"]["method"] = Actions.next_system_in_route

    assert helper.prepare_navigation("Wolf 397")["status"] == "ready"
    activate.assert_called_once_with()
    keys.send.assert_called_once_with("TargetNextRouteSystem")
    assert calls == [
        ("plot", {"system": "Wolf 397"}), ("close", {"desired_state": "close"}),
        ("key", "TargetNextRouteSystem"),
    ]


def test_host_target_action_does_not_reuse_stale_target_without_route(monkeypatch):
    keys = Mock()
    activate = Mock()
    monkeypatch.setattr(Actions, "keys", keys)
    monkeypatch.setattr(Actions, "setGameWindowActive", activate)
    result = Actions.next_system_in_route({}, {
        "NavInfo": {"NavRoute": [], "NextJumpTarget": "Old destination"},
    })
    assert "no navigation route" in result
    keys.send.assert_not_called()
    activate.assert_not_called()
