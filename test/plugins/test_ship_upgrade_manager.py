import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginBase import PluginManifest
from plugins.ship_upgrade_manager.ship_upgrade_manager import (
    ShipUpgradeManagerPlugin,
)
from plugins.ship_upgrade_manager.parsers import PlanParseError, parse_plan_input


class _PluginManager:
    def update_plugin_setting(self, *_args):
        return True


class _Helper:
    _plugin_manager = _PluginManager()

    def __init__(self, data_path: Path):
        self.data_path = data_path

    def get_plugin_data_path(self, _manifest):
        return str(self.data_path)

    def register_projection(self, _projection):
        pass

    def register_action(self, **_kwargs):
        pass

    def register_sideeffect(self, _sideeffect):
        pass

    def register_status_generator(self, _generator):
        pass


def _plugin(data_path: Path) -> ShipUpgradeManagerPlugin:
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"test-guid","name":"Test","version":"1.0.0"}'
        )
    )
    plugin.on_chat_start(_Helper(data_path))
    return plugin


def test_plan_import_and_session_progression(tmp_path: Path):
    plugin = _plugin(tmp_path)

    plan_id = plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd"}, {"id": "shield"}]},
    )

    assert plan_id
    assert plugin.start_session("PvE", "SHIP-1")["total_steps"] == 2
    assert plugin.complete_step("fsd")["current_step"] == 1

    plugin.set_session_paused(True)
    try:
        plugin.complete_step("shield")
    except ValueError as error:
        assert str(error) == "The active upgrade session is paused"
    else:
        raise AssertionError("A paused session accepted a completed step")

    plugin.set_session_paused(False)
    assert len(plugin.complete_step("shield")["completed_steps"]) == 2


def test_reimport_only_increments_version_when_source_changes(tmp_path: Path):
    plugin = _plugin(tmp_path)
    source = {"steps": [{"id": "fsd"}]}

    plan_id = plugin.import_plan("Python", "PvE", source)
    assert plugin.import_plan("Python", "PvE", source) == plan_id
    assert plugin.list_plans()[0]["plan_version"] == 1

    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}, {"id": "shield"}]})
    assert plugin.list_plans()[0]["plan_version"] == 2


def test_parse_coriolis_components_to_normalized_steps():
    plan = parse_plan_input(
        {
            "name": "Mining",
            "ship": {"name": "Python"},
            "components": {
                "standard": {
                    "frame_shift_drive": {"name": "Frame Shift Drive", "slot": "FrameShiftDrive"}
                }
            },
        }
    )

    assert plan["ship_model"] == "Python"
    assert plan["source_format"] == "coriolis"
    assert plan["steps"][0]["item"] == "Frame Shift Drive"


def test_parse_coriolis_nested_component_groups():
    plan = parse_plan_input(
        {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "ship": "python",
            "components": {
                "hardpoints": {
                    "medium_1": {
                        "name": "2D Beam Laser",
                        "slot": "MediumHardpoint1",
                    }
                },
                "standard": {
                    "powerplant": {
                        "module": "5A Power Plant",
                        "slot": "PowerPlant",
                    }
                },
            },
        }
    )

    assert plan["source_format"] == "coriolis"
    assert {step["slot"] for step in plan["steps"]} == {
        "MediumHardpoint1",
        "PowerPlant",
    }


def test_parse_inara_loadout():
    plan = parse_plan_input(
        {
            "name": "Exploration",
            "ship": {"shipType": "diamondbackexplorer", "name": "DBX"},
            "loadout": {
                "core": {
                    "frame_shift_drive": {
                        "module_id": "int_hyperdrive_size5_class5",
                        "slot_id": "FrameShiftDrive",
                    }
                },
                "optional": [
                    {
                        "item": "int_detailedsurfacescanner_tiny",
                        "position": "Slot01_Size1",
                    }
                ],
            },
        }
    )

    assert plan["source_format"] == "inara"
    assert plan["ship_model"] == "diamondbackexplorer"
    assert [step["item"] for step in plan["steps"]] == [
        "int_hyperdrive_size5_class5",
        "int_detailedsurfacescanner_tiny",
    ]


def test_parse_rejects_missing_ship():
    try:
        parse_plan_input({"name": "Invalid", "steps": []})
    except PlanParseError as error:
        assert "ship model" in str(error)
    else:
        raise AssertionError("Missing ship model was accepted")


def test_parse_slef_and_delete_plan(tmp_path: Path):
    plugin = _plugin(tmp_path)
    slef = [
        {
            "header": {"appName": "EDSY"},
            "data": {
                "event": "Loadout",
                "Ship": "panthermkii",
                "ShipName": "Cargo",
                "Modules": [
                    {"Slot": "PowerPlant", "Item": "int_powerplant_size7_class5"}
                ],
            },
        }
    ]
    normalized = parse_plan_input(__import__("json").dumps(slef))
    assert normalized["ship_model"] == "panthermkii"
    assert normalized["source_format"] == "slef"
    assert normalized["source_header"]["appName"] == "EDSY"
    plugin.import_plan(normalized["ship_model"], "Cargo", normalized)
    assert len(plugin.list_plans()) == 1
    assert plugin.delete_plan("Cargo")
    assert plugin.list_plans() == []


def test_parse_edsy_compact_url():
    url = (
        "https://edsy.org/#/L=J-00000H4C0S00,,"
        "CzYG05G_W0mpUDBwG05L_W0DBwG05L_W0DBwG0BL_W0,"
        "9on10ABkH04q_W0ASwGD5I_W0Ag-G-bJ0060upD6upD8qpDE_PcGzcQKsPcAtyGD3G_W0"
        "B8gG07L_W0BNCGD3G_W0Bfo1D,,"
        "0D81D0DI1D0Ba1D0Bk1D0AA1D0AA1D7UIH07K_W008c1D34a10072104xo2002M20,,"
        "NO_D22P"
    )

    normalized = parse_plan_input(url)

    assert normalized["ship_model"] == "panthermkii"
    assert normalized["source_format"] == "edsy"
    assert normalized["steps"][0]["slot"] == "CargoHatch"
    assert any(
        step["item"] == "int_hyperdrive_overcharge_size7_class5"
        for step in normalized["steps"]
    )
