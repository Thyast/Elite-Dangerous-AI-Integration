import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginBase import PluginManifest
from lib.Event import GameEvent
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
    plugin.stop_session()
    plugin.start_session("PvE", "SHIP-3")
    plugin.stop_session()
    assert [item["ship_instance_id"] for item in plugin.list_session_history()] == [
        "SHIP-3",
        "SHIP-2",
        "SHIP-1",
    ]


def test_multiple_plans_and_ships_have_independent_sessions(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Python", "Combat", {"steps": [{"id": "cannon"}]})

    mining = plugin.start_session("Mining", "SHIP-1")
    combat = plugin.start_session("Combat", "SHIP-2")

    assert len(plugin.list_sessions()) == 2
    assert plugin.complete_step("laser", session_id=mining["id"])["completed_steps"] == ["laser"]
    assert plugin.get_session(combat["id"])["completed_steps"] == []


def test_events_complete_all_matching_sessions_for_the_same_ship(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Mining A", {"steps": [{"id": "laser", "item": "laser"}]})
    plugin.import_plan("Python", "Mining B", {"steps": [{"id": "laser", "item": "laser"}]})
    plugin.start_session("Mining A", "SHIP-1")
    plugin.start_session("Mining B", "SHIP-1")

    plugin._on_event(
        GameEvent(
            content={
                "event": "ModuleBuy",
                "ShipID": "SHIP-1",
                "Slot": "Hardpoint1",
                "BuyItem": "laser",
            },
            historic=False,
        ),
        {},
    )

    assert all(
        session["completed_steps"] == ["laser"] for session in plugin.list_sessions()
    )


def test_plan_update_migrates_all_matching_sessions(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser", "item": "laser"}]})
    first = plugin.start_session("Mining", "SHIP-1")
    second = plugin.start_session("Mining", "SHIP-2")
    plugin.complete_step("laser", session_id=first["id"])

    plugin.import_plan(
        "Python",
        "Mining",
        {"steps": [{"id": "laser", "item": "different-laser"}]},
    )

    assert plugin.get_session(first["id"])["completed_steps"] == []
    assert plugin.get_session(second["id"])["completed_steps"] == []


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


def _set_plan_input(plugin: ShipUpgradeManagerPlugin, payload: dict) -> None:
    plugin.settings["plan_input"] = json.dumps(payload)


def test_import_tunnel_analyzes_and_renders_diff_before_import(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "fsd", "item": "old_fsd", "slot": "FrameShiftDrive"}]},
    )
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [
                {"id": "fsd", "item": "new_fsd", "slot": "FrameShiftDrive"},
                {"id": "shield", "item": "shield", "slot": "Slot01_Size5"},
            ],
        },
    )

    plugin.on_settings_button("import_plan")
    assert plugin._import_state == "data"

    plugin.on_settings_button("analyze_plan")

    assert plugin._import_state == "diff"
    diff_content = plugin._field("import", "diff_preview")["content"]
    assert "Version 1" in diff_content
    assert "new_fsd" in diff_content
    assert "old_fsd" in diff_content
    assert plugin.list_plans()[0]["plan_version"] == 1


def test_import_tunnel_confirm_imports_migrates_and_keeps_diff_visible(tmp_path: Path):
    plugin = _plugin(tmp_path)
    # Import the baseline through the same normalization path the tunnel uses
    # so step signatures are comparable.
    initial = parse_plan_input(
        json.dumps(
            {
                "ship_model": "Python",
                "plan_name": "PvE",
                "steps": [{"id": "fsd", "item": "old"}],
            }
        )
    )
    plugin.import_plan(initial["ship_model"], initial["plan_name"], initial)
    plugin.start_session("PvE", "SHIP-1")
    plugin.complete_step("fsd")
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [{"id": "fsd", "item": "old"}, {"id": "shield", "item": "new"}],
        },
    )

    plugin.on_settings_button("import_plan")
    plugin.on_settings_button("analyze_plan")
    plugin.on_settings_button("confirm_import")

    assert plugin._import_state == "imported"
    assert plugin.list_plans()[0]["plan_version"] == 2
    session = plugin.get_session()
    assert "fsd" in session["completed_steps"]
    last_changes = plugin._field("import", "last_changes")["content"]
    assert "Version 1" in last_changes
    assert "new" in last_changes
    banner = plugin._field("import", "import_done")
    assert banner["content"] == "plugin.sum.msg.doneBanner"
    assert banner["params"]["plan"] == "PvE"
    assert banner["params"]["version"] == 2


def test_import_tunnel_reports_invalid_input_and_stays_in_data_state(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.on_settings_button("import_plan")
    plugin.settings["plan_input"] = "not json"

    plugin.on_settings_button("analyze_plan")

    assert plugin._import_state == "data"
    error_field = plugin._field("import", "import_error")
    assert error_field["content"] == "plugin.sum.errParse"
    assert "detail" in error_field["params"]


def test_import_tunnel_cancel_returns_to_idle_and_clears_input(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.on_settings_button("import_plan")
    plugin.settings["plan_input"] = "junk"

    plugin.on_settings_button("cancel_import")

    assert plugin._import_state == "idle"
    assert plugin.settings["plan_input"] == ""
    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys
    plans_grid = next(g for g in plugin.settings_config["grids"] if g["key"] == "plans")
    assert plans_grid["header_action"]["key"] == "import_plan"


def test_import_entry_point_moves_between_header_and_tunnel(tmp_path: Path):
    plugin = _plugin(tmp_path)

    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys
    plans_grid = next(g for g in plugin.settings_config["grids"] if g["key"] == "plans")
    assert plans_grid["header_action"]["key"] == "import_plan"
    assert plans_grid["header_action"]["label"] == "plugin.sum.btn.import"

    plugin.on_settings_button("import_plan")

    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert grid_keys[0] == "import"
    plans_grid = next(g for g in plugin.settings_config["grids"] if g["key"] == "plans")
    assert "header_action" not in plans_grid


def test_import_tunnel_before_chat_start_imports_from_config_state(tmp_path: Path):
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"not-started-test-guid","name":"Ship Upgrade","version":"1.0.0"}'
        )
    )

    plugin.on_settings_button("import_plan")
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "Config plan",
            "steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}],
        },
    )
    plugin.on_settings_button("analyze_plan")
    plugin.on_settings_button("confirm_import")

    assert plugin.list_plans()[0]["plan_name"] == "Config plan"
    assert plugin._import_state == "imported"


def test_plan_rows_expose_delete_action_and_delete_by_row(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plan_id = plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}]})
    plugin._publish_status()

    plans_field = plugin._field("plans", "available_plans")
    assert [row["key"] for row in plans_field["items"]] == [plan_id]
    assert plans_field["row_actions"][0]["action"] == "delete_plan"
    assert plans_field["row_actions"][0]["label"] == "plugin.sum.btn.delete"

    plugin.on_settings_button(f"delete_plan:{plan_id}")

    assert plugin.list_plans() == []
    assert plugin._field("plans", "delete_status")["content"] == "plugin.sum.msg.deleted"
    assert plugin._field("plans", "delete_status")["params"]["plan"] == "PvE"


def test_delete_unknown_plan_row_reports_error(tmp_path: Path):
    plugin = _plugin(tmp_path)

    plugin.on_settings_button("delete_plan:unknown-id")

    field = plugin._field("plans", "delete_status")
    assert field["content"] == "plugin.sum.errDelete"
    assert "detail" in field["params"]


def test_delete_last_imported_plan_resets_import_state(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.on_settings_button("import_plan")
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}],
        },
    )
    plugin.on_settings_button("analyze_plan")
    plugin.on_settings_button("confirm_import")
    plan_id = plugin.list_plans()[0]["id"]

    plugin.on_settings_button(f"delete_plan:{plan_id}")

    assert plugin._import_state == "idle"
    assert plugin._last_import is None
    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys


def test_last_import_persists_across_plugin_restart(tmp_path: Path):
    plugin = _plugin(tmp_path)
    initial = parse_plan_input(
        json.dumps(
            {
                "ship_model": "Python",
                "plan_name": "PvE",
                "steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}],
            }
        )
    )
    plugin.import_plan(initial["ship_model"], initial["plan_name"], initial)
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [
                {"id": "fsd", "item": "int_hyperdrive_size5_class5"},
                {"id": "shield", "item": "int_shieldgenerator_size5_class5"},
            ],
        },
    )
    plugin.on_settings_button("import_plan")
    plugin.on_settings_button("analyze_plan")
    plugin.on_settings_button("confirm_import")

    restarted = ShipUpgradeManagerPlugin(
        PluginManifest('{"guid":"test-guid","name":"Test","version":"1.0.0"}')
    )
    restarted.on_chat_start(_Helper(tmp_path))

    assert restarted._import_state == "imported"
    banner = restarted._field("import", "import_done")
    assert banner["content"] == "plugin.sum.msg.doneBanner"
    assert banner["params"]["plan"] == "PvE"
    assert banner["params"]["version"] == 2
    assert "int_shieldgenerator_size5_class5" in restarted._field("import", "last_changes")["content"]


def test_plan_filter_filters_rows_live_and_groups_by_ship(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Python", "Combat", {"steps": [{"id": "cannon"}]})
    plugin.import_plan("Krait", "Haul", {"steps": [{"id": "cargo"}]})

    # Simulate the settings update path for the search field.
    plugin.settings["plan_filter"] = "python"
    plugin.on_settings_changed()

    rows = plugin._field("plans", "available_plans")["items"]
    assert [(row["title"], row["group"]) for row in rows] == [
        ("Combat", "Python"),
        ("Mining", "Python"),
    ]

    plugin.settings["plan_filter"] = ""
    plugin.on_settings_changed()

    rows = plugin._field("plans", "available_plans")["items"]
    assert [row["group"] for row in rows] == ["Krait", "Python", "Python"]


def test_session_summary_is_published_as_i18n_key(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}, {"id": "shield"}]})
    plugin.start_session("PvE", "SHIP-1")
    # complete_step publishes the status itself.
    plugin.complete_step("fsd")

    summary = plugin._field("session", "session_summary")
    assert summary["content"] == "plugin.sum.msg.sessionActive"
    assert summary["params"]["total"] == 2
    assert summary["params"]["pct"] == 50


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


def test_parse_coriolis_compact_url():
    url = (
        "https://coriolis.io/outfit/kestrel?code="
        "A4pf7TFOl3dks8f47U7U0v212107070702B22b2b2927272Sm14F."
        "Iw18eQ%3D%3D.CwBgjKJQzLsEyMVFKg%3D%3D."
        "H4sIAAAAAAAAA42Qu0oDURCGJ2ZzPTGbjdm4kUS8bBQsgq2NpLMRsZHUtlYWghax8A1ExMrCwgewtBILsVLwAURSWlhYeokzzr%2BYg4XBPcXHMPPNnDOHOEtEXylF%2F0jhdfsiYc8nMm2NJMHLtr6vKD9kiNxei6h64RBl1z9VGuHASjsK9%2FpdJGiniSqBDvFf80Szh89qJnnCmrsYd2lUDz9Uf9RkpasXi8O5gVS%2BexJx9160NcVLtvVA0SywyNyxR5RDlEc0jWgGkaR5E3oSQ7bHiVqLbyK1qJTnjp00OXhEdH%2BtNK91wxu2bvsDLJ7q6M4NwIFJkV74pWf%2B10d5xeqn%2BCx8YXCjK3vn%2BmMhYAApxjbd2GaJmzAT9POc%2BmpDsx6v2f4t9J%2B52oohIZaRMi%2FY%2BglWRL04VVfpSpMhYAAZi21Whpu3MAEDiD%2FcvIcJGECqsU2hP843W1CQ%2Fg0DAAA%3D&bn=Imported%20Kestrel%20Mk%20II"
    )

    normalized = parse_plan_input(url)

    assert normalized["ship_model"] == "smallcombat01_nx"
    assert normalized["source_format"] == "coriolis"
    assert normalized["plan_name"] == "Imported Kestrel Mk II"
    assert normalized["steps"][0]["item"] == "int_powerplant_size5_class5"
    assert normalized["steps"][7]["slot"] == "LargeHardpoint1"
