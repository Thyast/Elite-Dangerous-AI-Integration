import json
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


def test_session_history_is_archived_when_stopped_or_replaced(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}, {"id": "shield"}]})
    plugin.start_session("PvE", "SHIP-1")
    plugin.complete_step("fsd")
    plugin.stop_session()

    history = plugin.list_session_history()
    assert len(history) == 1
    assert history[0]["ship_instance_id"] == "SHIP-1"
    assert history[0]["completed_steps"] == ["fsd"]
    assert history[0]["completion_percent"] == 50

    plugin.start_session("PvE", "SHIP-2")
    plugin.start_session("PvE", "SHIP-3")
    assert [item["ship_instance_id"] for item in plugin.list_session_history()] == [
        "SHIP-2",
        "SHIP-1",
    ]


def test_reimport_only_increments_version_when_source_changes(tmp_path: Path):
    plugin = _plugin(tmp_path)
    source = {"steps": [{"id": "fsd"}]}

    plan_id = plugin.import_plan("Python", "PvE", source)
    assert plugin.import_plan("Python", "PvE", source) == plan_id
    assert plugin.list_plans()[0]["plan_version"] == 1

    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}, {"id": "shield"}]})
    assert plugin.list_plans()[0]["plan_version"] == 2


def test_plan_diff_reports_added_removed_and_changed_modules(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "old"}, {"id": "shield", "item": "same"}]},
    )

    diff = plugin.diff_plan(
        "PvE",
        {
            "steps": [
                {"id": "fsd", "item": "new"},
                {"id": "cargo", "item": "added"},
            ]
        },
    )

    assert diff["current_version"] == 1
    assert diff["next_version"] == 2
    assert [step["id"] for step in diff["added"]] == ["cargo"]
    assert [step["id"] for step in diff["removed"]] == ["shield"]
    assert diff["changed"][0]["before"]["item"] == "old"
    assert diff["changed"][0]["after"]["item"] == "new"


def test_reimport_migrates_active_session_without_completing_changed_modules(
    tmp_path: Path,
):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "same"}, {"id": "shield", "item": "old"}]},
    )
    plugin.start_session("PvE", "SHIP-1")
    plugin.complete_step("fsd")

    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "fsd", "item": "same"},
                {"id": "shield", "item": "new"},
                {"id": "cargo", "item": "added"},
            ]
        },
    )

    session = plugin.get_session()
    assert session is not None
    assert session["completed_steps"] == ["fsd"]
    assert session["current_step"] == 1
    assert session["plan_version"] == 2
    assert session["plan_version_at_session_start"] == 2


def test_plan_crud_reads_and_updates_existing_record(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plan_id = plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "old"}]},
    )

    assert plugin.get_plan(plan_id)["modules"][0]["item"] == "old"
    assert plugin.update_plan(
        plan_id,
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "new"}]},
    ) == plan_id
    assert plugin.get_plan(plan_id)["modules"][0]["item"] == "new"
    assert plugin.list_plans()[0]["plan_version"] == 2


def test_plan_diff_preview_is_rendered_in_settings(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "old_fsd", "slot": "FrameShiftDrive"}]},
    )
    plugin.settings["plan_input"] = json.dumps(
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [
                {"id": "fsd", "item": "new_fsd", "slot": "FrameShiftDrive"},
                {"id": "shield", "item": "shield", "slot": "Slot01_Size5"},
            ],
        }
    )

    plugin.on_settings_button("preview_diff")

    assert "Version 1" in plugin.settings["diff_status"]
    assert "new_fsd" in plugin.settings["diff_status"]
    assert "shield" in plugin.settings["diff_status"]
    assert "old_fsd" in plugin.settings["diff_status"]


def test_voice_actions_preview_and_apply_plan_changes(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "old"}]},
    )
    updated = json.dumps(
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [{"id": "fsd", "item": "new"}, {"id": "shield", "item": "new"}],
        }
    )

    from plugins.ship_upgrade_manager.ship_upgrade_manager import PlanChangeParams

    assert "1 added" in plugin._preview_changes_action(PlanChangeParams(plan_input=updated), {})
    result = plugin._apply_changes_action(PlanChangeParams(plan_input=updated), {})
    assert "1 added" in result
    assert plugin.list_plans()[0]["plan_version"] == 2


def test_settings_apply_changes_imports_and_migrates_plan(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "old"}]},
    )
    plugin.start_session("PvE", "SHIP-1")
    plugin.settings["plan_input"] = json.dumps(
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [{"id": "fsd", "item": "new"}, {"id": "shield", "item": "new"}],
        }
    )

    plugin.on_settings_button("apply_changes")

    assert plugin.list_plans()[0]["plan_version"] == 2
    assert plugin.get_session()["completed_steps"] == []
    assert "Applied 'PvE'" in plugin.settings["import_status"]


def test_settings_apply_changes_reports_invalid_input(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.settings["plan_input"] = "not json"

    plugin.on_settings_button("apply_changes")

    assert plugin.settings["import_status"].startswith("Apply error:")


def test_loadout_and_module_events_auto_complete_matching_modules(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "fsd", "item": "int_hyperdrive_size5_class5", "slot": "FrameShiftDrive"},
                {
                    "id": "shield",
                    "item": "int_shieldgenerator_size5_class5",
                    "slot": "Slot01_Size5",
                    "engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5},
                },
            ]
        },
    )
    plugin.start_session("PvE", "42")

    from lib.Event import GameEvent

    plugin._on_event(
        GameEvent(
            content={
                "event": "ModuleBuy",
                "ShipID": 42,
                "BuyItem": "int_hyperdrive_size5_class5",
                "Slot": "FrameShiftDrive",
            },
            historic=False,
        ),
        {},
    )
    assert plugin.get_session()["completed_steps"] == ["fsd"]

    plugin._on_event(
        GameEvent(
            content={
                "event": "Loadout",
                "ShipID": 42,
                "Modules": [
                    {
                        "Item": "int_shieldgenerator_size5_class5",
                        "Slot": "Slot01_Size5",
                        "Engineering": {
                            "BlueprintName": "ShieldGenerator_Reinforced",
                            "Level": 5,
                        },
                    }
                ],
            },
            historic=False,
        ),
        {},
    )
    assert plugin.get_session()["completed_steps"] == ["fsd", "shield"]


def test_storage_and_retrieval_events_auto_complete_matching_modules(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "fsd", "item": "int_hyperdrive_size5_class5", "slot": "FrameShiftDrive"},
                {"id": "shield", "item": "int_shieldgenerator_size5_class5", "slot": "Slot01_Size5"},
            ]
        },
    )
    plugin.start_session("PvE", "SHIP-42")

    from lib.Event import GameEvent

    plugin._on_event(
        GameEvent(
            content={
                "event": "ModuleStore",
                "Ship": "SHIP-42",
                "StoredItem": "int_hyperdrive_size5_class5",
                "Slot": "FrameShiftDrive",
            },
            historic=False,
        ),
        {},
    )
    assert plugin.get_session()["completed_steps"] == ["fsd"]

    plugin._on_event(
        GameEvent(
            content={
                "event": "ModuleRetrieve",
                "ShipIdent": "SHIP-42",
                "RetrievedItem": "int_shieldgenerator_size5_class5",
                "Slot": "Slot01_Size5",
            },
            historic=False,
        ),
        {},
    )
    assert plugin.get_session()["completed_steps"] == ["fsd", "shield"]


def test_real_journal_module_events_use_ship_id_and_ignore_null_swap(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Kestrel Mk II",
        "Combat",
        {
            "steps": [
                {
                    "id": "beam",
                    "item": "hpt_beamlaser_gimbal_large",
                    "slot": "LargeHardpoint1",
                },
                {
                    "id": "frag",
                    "item": "hpt_slugshot_gimbal_small",
                    "slot": "SmallHardpoint1",
                },
            ],
        },
    )
    plugin.start_session("Combat", "15")

    from lib.Event import GameEvent

    plugin._on_event(
        GameEvent(
            content={
                "timestamp": "2026-07-05T18:31:28Z",
                "event": "ModuleBuy",
                "Slot": "LargeHardpoint1",
                "StoredItem": "$hpt_mkiiplasmashockautocannon_fixed_large_name;",
                "BuyItem": "$hpt_beamlaser_gimbal_large_name;",
                "Ship": "smallcombat01_nx",
                "ShipID": 15,
            },
            historic=False,
        ),
        {},
    )
    assert plugin.get_session()["completed_steps"] == ["beam"]

    plugin._on_event(
        GameEvent(
            content={
                "timestamp": "2026-07-05T20:28:44Z",
                "event": "ModuleSwap",
                "FromSlot": "TinyHardpoint1",
                "ToSlot": "TinyHardpoint4",
                "FromItem": "$hpt_heatsinklauncher_turret_tiny_name;",
                "ToItem": "Null",
                "Ship": "smallcombat01_nx",
                "ShipID": 15,
            },
            historic=False,
        ),
        {},
    )
    assert plugin.get_session()["completed_steps"] == ["beam"]


def test_module_info_snapshot_auto_completes_without_ship_id(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Kestrel Mk II",
        "Combat",
        {
            "steps": [
                {
                    "id": "beam",
                    "item": "hpt_beamlaser_gimbal_large",
                    "slot": "LargeHardpoint1",
                },
                {
                    "id": "fsd",
                    "item": "int_hyperdrive_overcharge_size4_class5",
                    "slot": "FrameShiftDrive",
                },
            ],
        },
    )
    plugin.start_session("Combat", "15")

    from lib.Event import GameEvent

    plugin._on_event(
        GameEvent(
            content={
                "timestamp": "2026-09-16T05:36:36Z",
                "event": "ModuleInfo",
                "Modules": [
                    {
                        "Slot": "LargeHardpoint1",
                        "Item": "hpt_beamlaser_gimbal_large",
                    },
                    {
                        "Slot": "FrameShiftDrive",
                        "Item": "int_hyperdrive_overcharge_size4_class5",
                    },
                ],
            },
            historic=False,
        ),
        {},
    )

    assert plugin.get_session()["completed_steps"] == ["beam", "fsd"]


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
